#!/usr/bin/env bash
# Run from the finance checkout after its separate certificate has been issued.
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
domain=${1:?Usage: bash deploy/enable-miniapp.sh finance.example.com}
if [[ ! "$domain" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]+[a-zA-Z0-9]$ ]]; then
    printf '%s\n' 'Invalid hostname' >&2
    exit 1
fi
test -f .env
test -f /etc/letsencrypt/live/finance-miniapp/fullchain.pem
test -f /etc/letsencrypt/live/finance-miniapp/privkey.pem
openssl x509 -in /etc/letsencrypt/live/finance-miniapp/fullchain.pem -checkhost "$domain" -noout
openssl x509 -in /etc/letsencrypt/live/finance-miniapp/fullchain.pem -checkend 86400 -noout

compose=(docker compose --project-name finance --profile api)
"${compose[@]}" config --services | grep -qx api
"${compose[@]}" exec -T postgres pg_isready -U budget_bot -d budget_bot

backup="data/backups/miniapp-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
chmod 700 data/backups "$backup"
cp -p .env "$backup/env"
chmod 600 "$backup/env"
printf 'Backup: %s\n' "$backup"

env_tmp=$(mktemp .env.miniapp.XXXXXX)
nginx_tmp=''
nginx_changed=false
nginx_config=/etc/nginx/sites-available/zz-finance-miniapp.conf
nginx_link=/etc/nginx/sites-enabled/zz-finance-miniapp.conf

finish() {
    status=$?
    trap - EXIT
    if [[ -n "$env_tmp" && -f "$env_tmp" ]]; then unlink "$env_tmp"; fi
    if [[ -n "$nginx_tmp" && -f "$nginx_tmp" ]]; then unlink "$nginx_tmp"; fi
    if [[ $status -ne 0 ]]; then
        cp -p "$backup/env" .env
        chmod 600 .env
        if $nginx_changed; then
            cp "$backup/nginx.conf" "$nginx_config"
            if nginx -t; then systemctl reload nginx; fi
        fi
        printf 'Setup stopped. Backup: %s\nCheck finance services with: docker compose --profile api ps\n' "$backup" >&2
    fi
    exit "$status"
}
trap finish EXIT

awk -v url="https://$domain/dashboard" '
    /^BUDGET_WEB_URL=/ { print "BUDGET_WEB_URL=" url; found=1; next }
    { print }
    END { if (!found) print "BUDGET_WEB_URL=" url }
' .env > "$env_tmp"
chmod 600 "$env_tmp"
mv "$env_tmp" .env
env_tmp=''

# Build before stopping the existing bot. No other Compose project is addressed.
"${compose[@]}" build bot api
"${compose[@]}" stop bot
"${compose[@]}" exec -T postgres pg_dump -U budget_bot -d budget_bot -Fc > "$backup/database.dump"
"${compose[@]}" exec -T postgres pg_restore --list < "$backup/database.dump" > "$backup/database-catalog.txt"

fingerprint_sql="SELECT COUNT(*) || ':' || md5(COALESCE(string_agg((to_jsonb(e) - 'author_id' - 'updated_by' - 'expense_entry_id')::text, ',' ORDER BY id), '')) FROM budget_entries e;"
before=$("${compose[@]}" exec -T postgres psql -U budget_bot -d budget_bot -At -c "$fingerprint_sql")
"${compose[@]}" run --rm --no-deps bot python -m budget_bot.cli migrate
after=$("${compose[@]}" exec -T postgres psql -U budget_bot -d budget_bot -At -c "$fingerprint_sql")
if [[ "$before" != "$after" ]]; then
    printf '%s\n' 'Ledger changed during migration; bot remains stopped. Check the backup before proceeding.' >&2
    exit 1
fi
printf 'Ledger preserved: %s\n' "$after"
"${compose[@]}" up -d --no-build --no-deps api
api_ready=false
printf '%s\n' 'Waiting for API startup...'
for ((attempt=1; attempt<=30; attempt++)); do
    if curl --fail --silent --show-error --connect-timeout 2 --max-time 3 http://127.0.0.1:8000/health > "$backup/health.json" 2> "$backup/health-error.txt"; then
        api_ready=true
        break
    fi
    sleep 1
done
if ! $api_ready; then
    cat "$backup/health-error.txt" >&2
    "${compose[@]}" logs --tail=40 api >&2
    exit 1
fi
cat "$backup/health.json"

# Replace only the finance virtual host created during certificate setup.
test -f "$nginx_config"
grep -qx '# Budget Bot Mini App' "$nginx_config"
test -L "$nginx_link"
test "$(readlink -f "$nginx_link")" = "$nginx_config"
cp -p "$nginx_config" "$backup/nginx.conf"
nginx_tmp=$(mktemp /etc/nginx/sites-available/.finance-miniapp.XXXXXX)
sed "s/__DOMAIN__/$domain/g" deploy/finance-miniapp.nginx.conf > "$nginx_tmp"
chmod 644 "$nginx_tmp"
mv "$nginx_tmp" "$nginx_config"
nginx_tmp=''
nginx_changed=true
nginx -t
systemctl reload nginx
# Reload signals the master; new workers may not be accepting connections yet.
https_ready=false
printf '\n%s\n' 'Waiting for HTTPS after Nginx reload...'
for ((attempt=1; attempt<=30; attempt++)); do
    if curl --fail --silent --show-error --connect-timeout 2 --max-time 3 --noproxy '*' --resolve "$domain:443:127.0.0.1" "https://$domain/dashboard" -o /dev/null 2> "$backup/https-error.txt"; then
        https_ready=true
        break
    fi
    sleep 1
done
if ! $https_ready; then
    cat "$backup/https-error.txt" >&2
    exit 1
fi
code=$(curl --silent --show-error --connect-timeout 2 --max-time 3 --noproxy '*' --resolve "$domain:443:127.0.0.1" -o /dev/null -w '%{http_code}' "https://$domain/api/dashboard")
if [[ "$code" != 401 ]]; then
    printf 'Unexpected unauthenticated API status: %s\n' "$code" >&2
    exit 1
fi
"${compose[@]}" up -d --no-build --no-deps bot
nginx_changed=false
"${compose[@]}" ps
printf '\nMini App URL: https://%s/dashboard\nBackup: %s\nSend /web to the bot to enable its Mini App button.\n' "$domain" "$backup"
