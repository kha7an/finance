#!/usr/bin/env python3
"""Read-only Linux VLESS diagnostics. Reports contain allowlisted fields only."""
import argparse
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request


SIGNALS = (
    "invalid user", "invalid uuid", "invalid request", "invalid account",
    "not found", "rejected", "not allowed", "account disabled", "expired",
    "failed to decode request header", "failed to decode response header",
    "failed to read request", "failed to read response", "failed to dial",
    "failed to find an available destination", "connection refused",
    "connection reset", "broken pipe", "no route to host", "network is unreachable",
    "no such host", "lookup", "i/o timeout", "context deadline exceeded",
    "unexpected eof", "eof", "invalid connection", "received real certificate",
    "handshake", "certificate", "shortid", "short id", "flow", "vision",
    "reality", "vless", "accepted", "permission denied", "address already in use",
)
KNOWN = {
    "protocol": {"vless", "vmess", "trojan", "http", "socks", "dokodemo-door"},
    "network": {"tcp", "raw", "ws", "grpc", "xhttp", "httpupgrade", "kcp", "quic"},
    "security": {"reality", "tls", "none"},
    "flow": {"", "xtls-rprx-vision", "xtls-rprx-vision-udp443"},
}
CONFIG_PATHS = (
    "/usr/local/etc/xray/config.json", "/etc/xray/config.json",
    "/usr/local/x-ui/bin/config.json", "/opt/x-ui/bin/config.json",
    "/etc/sing-box/config.json",
)
DB_PATHS = ("/etc/x-ui/x-ui.db", "/usr/local/x-ui/x-ui.db", "/usr/local/x-ui/db/x-ui.db")


def run(args, timeout=15):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False)
        return p.returncode, p.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def log_events(text):
    events = []
    omitted = 0
    for line in text.splitlines():
        # Never return the original line: logs may contain arbitrary credentials.
        lower = line.lower()
        signals = [s for s in SIGNALS if re.search(r"(?<![a-z])" + re.escape(s) + r"(?![a-z])", lower)]
        if not signals:
            omitted += 1
            continue
        stamp = re.search(r"\b20\d{2}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}", line)
        events.append((stamp.group(0) if stamp else "time unavailable") + " | " + ", ".join(signals))
    return events, omitted


def safe_value(key, value):
    return value if isinstance(value, str) and value in KNOWN[key] else "other/unspecified"


def client_state(client, traffic=None):
    state = {"enabled_in_panel": client.get("enable", "unspecified") is True}
    if "enable" not in client:
        state["enabled_in_panel"] = "unspecified"
    expiry = (traffic or {}).get("expiry_time", client.get("expiryTime", 0))
    if isinstance(expiry, (int, float)):
        state["expiry"] = ("expired" if expiry < time.time() * 1000 else "future") if expiry > 0 else (
            "starts_on_first_use" if expiry < 0 else "unlimited/unspecified")
    if traffic:
        if "enable" in traffic:
            state["enabled_in_traffic_record"] = bool(traffic["enable"])
        total, up, down = (traffic.get(k) for k in ("total", "up", "down"))
        if all(isinstance(x, (int, float)) for x in (total, up, down)):
            state["traffic_limit"] = "unlimited" if total <= 0 else ("exhausted" if up + down >= total else "available")
    return state


def config_summary(config, target):
    result = []
    for index, inbound in enumerate(config.get("inbounds", []), 1):
        protocol = inbound.get("protocol", inbound.get("type"))
        if protocol != "vless":
            continue
        clients = inbound.get("settings", {}).get("clients", inbound.get("users", []))
        matches = [c for c in clients if str(c.get("id", c.get("uuid", ""))).lower() == target]
        stream = inbound.get("streamSettings", {})
        tls = inbound.get("tls", {})
        port = inbound.get("port", inbound.get("listen_port"))
        result.append({"inbound_number": index, "port": port if type(port) is int else "other",
                       "uuid_found": bool(matches), "network": safe_value("network", stream.get("network")),
                       "security": safe_value("security", stream.get("security", "reality" if tls.get("reality", {}).get("enabled") else None)),
                       "matching_clients": [{"flow": safe_value("flow", c.get("flow", "")), **client_state(c)} for c in matches]})
    return result


def inspect_database(path, target):
    # Do not copy the database or use immutable mode: live WAL updates matter.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        found = []
        for row in db.execute("SELECT id, enable, settings FROM inbounds"):
            settings = json.loads(row["settings"])
            for client in settings.get("clients", []):
                if str(client.get("id", "")).lower() != target:
                    continue
                traffic = None
                try:
                    traffic_row = db.execute(
                        "SELECT * FROM client_traffics WHERE inbound_id=? AND email=?",
                        (row["id"], client.get("email", ""))).fetchone()
                    traffic = dict(traffic_row) if traffic_row else None
                except sqlite3.Error:
                    pass
                found.append({"inbound_enabled": bool(row["enable"]), **client_state(client, traffic)})
        return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Additional server JSON configuration")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("Self-test passed")
        return
    if not sys.stdin.isatty():
        raise SystemExit("Run from an interactive terminal; UUID must be entered privately.")
    target = getpass.getpass("UUID from client profile (hidden): ").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", target):
        raise SystemExit("Invalid UUID format")
    report = ["VLESS diagnostics, UTC " + dt.datetime.now(dt.timezone.utc).isoformat(),
              "Read-only. No raw logs, UUIDs, keys, addresses or client names are included.",
              "root=" + str(os.geteuid() == 0)]
    configs = set(Path(p) for p in CONFIG_PATHS)
    databases = set(Path(p) for p in DB_PATHS)
    if args.config:
        configs.add(args.config)
    log_files = set()
    services = []
    if shutil.which("systemctl"):
        _, units = run(["systemctl", "list-units", "--all", "--type=service", "--no-legend", "--plain"])
        for line in units.splitlines():
            fields = line.split()
            name = fields[0] if fields else ""
            if re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", name) and any(s in name.lower() for s in ("xray", "sing-box", "x-ui")):
                services.append(name)
                _, status = run(["systemctl", "show", name, "-p", "ActiveState", "-p", "SubState"])
                states = re.findall(r"(?:ActiveState|SubState)=(active|inactive|failed|running|dead|exited|auto-restart)\b", status)
                report.append("service-%d states=%s" % (len(services), states))
    # Discover active Xray configs without reporting process command lines.
    for entry in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            parts = entry.read_bytes().decode(errors="replace").strip("\0").split("\0")
            if not parts or not re.fullmatch(r"(?:xray[^/]*|sing-box)", Path(parts[0]).name):
                continue
            for i, arg in enumerate(parts[:-1]):
                if arg in ("-c", "-config", "--config"):
                    path = Path(parts[i + 1])
                    if path.is_absolute():
                        configs.add(path)
                elif arg in ("-confdir", "--confdir", "-C"):
                    path = Path(parts[i + 1])
                    if path.is_absolute():
                        configs.update(path.glob("*.json"))
        except OSError:
            continue
    containers = []
    docker_configs = []
    if shutil.which("docker"):
        _, listing = run(["docker", "ps", "-a", "--format", "{{json .}}"])
        for line in listing.splitlines():
            try:
                item = json.loads(line)
                if not any(s in (item.get("Image", "") + " " + item.get("Names", "")).lower() for s in ("xray", "sing-box", "x-ui", "3x-ui")):
                    continue
                cid = item["ID"]
                if not re.fullmatch(r"[0-9a-f]{12,64}", cid):
                    continue
                containers.append(cid)
                _, raw = run(["docker", "inspect", cid])
                info = json.loads(raw)[0]
                report.append("container-%d running=%s" % (len(containers), info.get("State", {}).get("Running") is True))
                for value in info.get("Config", {}).get("Env", []) or []:
                    if value.startswith("XRAY_CONFIG_JSON="):
                        docker_configs.append(json.loads(value.partition("=")[2]))
                for mount in info.get("Mounts", []):
                    source = Path(mount["Source"])
                    if source.is_file() and source.suffix == ".json":
                        configs.add(source)
                    elif source.is_file() and source.name == "x-ui.db":
                        databases.add(source)
                    elif source.is_dir():
                        configs.update(source.glob("*.json"))
                        databases.update(source.glob("x-ui.db"))
                if info.get("State", {}).get("Running"):
                    for path in CONFIG_PATHS:
                        code, raw = run(["docker", "exec", cid, "cat", path])
                        if code == 0:
                            try:
                                docker_configs.append(json.loads(raw))
                            except ValueError:
                                pass
            except (ValueError, KeyError, IndexError, TypeError, OSError):
                report.append("Docker metadata could not be inspected (details omitted)")
    index = 0
    for path in sorted(configs):
        if not path.exists():
            continue
        index += 1
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError()
            config = json.loads(path.read_text())
            report.append("config-%d %s" % (index, json.dumps(config_summary(config, target))))
            for key in ("access", "error"):
                value = config.get("log", {}).get(key)
                if value and value != "none" and Path(value).is_absolute():
                    log_files.add(Path(value))
        except (OSError, ValueError, TypeError, AttributeError):
            report.append("config-%d unreadable or unsupported JSON" % index)
    for config in docker_configs:
        index += 1
        try:
            report.append("container-config-%d %s" % (index, json.dumps(config_summary(config, target))))
        except (ValueError, TypeError, AttributeError):
            report.append("container-config-%d unsupported configuration structure" % index)
    if not index:
        report.append("No readable config found; rerun with --config /absolute/path/config.json")
    for index, path in enumerate(sorted(databases), 1):
        if path.exists():
            try:
                report.append("panel-db-%d %s" % (index, json.dumps(inspect_database(path, target))))
            except (sqlite3.Error, OSError, ValueError, TypeError):
                report.append("panel-db-%d unreadable or unsupported schema" % index)
    print("Repeat the failing connection from the client now. Collecting logs in 30 seconds...", flush=True)
    time.sleep(30)
    sources = []
    for index, service in enumerate(services, 1):
        code, raw = run(["journalctl", "-u", service, "--since", "15 minutes ago", "-n", "300", "--no-pager", "-o", "short-iso"])
        sources.append(("service-%d logs, command_ok=%s" % (index, code == 0), raw))
    for index, cid in enumerate(containers, 1):
        # docker logs writes some log streams to stderr; capture both privately.
        try:
            p = subprocess.run(["docker", "logs", "--since", "15m", "--tail", "300", "--timestamps", cid],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
            sources.append(("container-%d logs, command_ok=%s" % (index, p.returncode == 0), p.stdout.decode(errors="replace")))
        except (OSError, subprocess.TimeoutExpired):
            report.append("container-%d logs unavailable" % index)
    for index, path in enumerate(sorted(log_files), 1):
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, os.fstat(handle.fileno()).st_size - 128 * 1024))
                sources.append(("log-file-%d tail (may include older events)" % index, handle.read().decode(errors="replace")))
        except OSError:
            report.append("log-file-%d unavailable" % index)
    for label, raw in sources:
        events, omitted = log_events(raw)
        report.append(label + ":\n" + "\n".join(events[-100:]) + "\nunclassified lines omitted=" + str(omitted))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPRedirectHandler())
    for host in ("example.com", "api.telegram.org"):
        try:
            with opener.open(urllib.request.Request("https://" + host, method="HEAD"), timeout=10) as response:
                report.append("server egress " + host + ": HTTP " + str(response.status))
        except urllib.error.HTTPError as exc:
            report.append("server egress " + host + ": HTTP " + str(exc.code))
        except Exception:
            report.append("server egress " + host + ": failed (details omitted)")
    report.append("Limits: config presence does not prove a running process loaded it; API-managed users may differ. "
                  "Container-internal log files and unmounted panel databases are not collected. "
                  "Egress probes run on the host, not inside the VLESS container.")
    filename = "vless-diagnostics-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".txt"
    with os.fdopen(os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        handle.write("\n".join(report) + "\n")
    print("Report saved: " + filename)


def self_test():
    uuid = "12345678-1234-1234-1234-123456789abc"
    secret = "UnpredictableSecretKeyThatMustNeverBePrinted_123456"
    raw = '2026-11-04T12:00:00 invalid user ' + uuid + ' privateKey=' + secret
    events, _ = log_events(raw)
    assert events == ["2026-11-04T12:00:00 | invalid user"]
    assert log_events(secret + " " + uuid) == ([], 1)
    config = {"inbounds": [{"protocol": "vless", "port": 443, "settings": {
        "clients": [{"id": uuid, "email": secret, "flow": "xtls-rprx-vision"}]}}]}
    summary = config_summary(config, uuid)
    assert summary[0]["uuid_found"] and secret not in json.dumps(summary) and uuid not in json.dumps(summary)
    assert client_state({"enable": False, "expiryTime": 1})["expiry"] == "expired"
    assert client_state({}, {"total": 10, "up": 5, "down": 5})["traffic_limit"] == "exhausted"
    assert safe_value("flow", secret) == "other/unspecified"


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        sys.exit(130)
    except Exception:
        # Tracebacks can contain paths or data from secret-bearing configuration.
        print("Diagnostics failed; raw error details suppressed to protect secrets.", file=sys.stderr)
        sys.exit(1)
