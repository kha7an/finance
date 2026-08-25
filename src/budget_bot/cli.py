from __future__ import annotations

import argparse
import mimetypes
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .app_factory import build_context
from .categories import missing_required_subcategories
from .excel_exporter import ExcelExporter
from .mock_data import sample_image_content, sample_screenshot_payload
from .models import ParsedScreenshot
from .storage import operation_from_json, telegram_owner_id
from .telegram_bot import _expense_report_lines, _parse_stats_period
from .telegram_bot import TelegramBot


def main() -> None:
    parser = argparse.ArgumentParser(prog="budget-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Check workbook categories and configuration.")
    subparsers.add_parser("openai-check", help="Check OpenAI key and selected model access.")
    subparsers.add_parser("telegram-check", help="Check Telegram token and bot identity.")
    subparsers.add_parser("telegram", help="Run Telegram long polling bot.")
    subparsers.add_parser("sync", help="Deprecated: use export-excel.")

    export_excel = subparsers.add_parser("export-excel", help="Generate Excel export from Postgres.")
    export_excel.add_argument("--period", help="Optional period like 01.08-24.08.")
    export_excel.add_argument("--owner", default="default", help="Owner id, e.g. default or telegram:123.")

    stats = subparsers.add_parser("stats", help="Show expense stats from Postgres.")
    stats.add_argument("period", nargs="?", help="Optional period like 01.08-24.08.")

    debug_entries = subparsers.add_parser("debug-entries", help="Print recent operations and budget entries.")
    debug_entries.add_argument("--owner", default="default", help="Owner id, e.g. default or telegram:123.")
    debug_entries.add_argument("--telegram-user-id", type=int, help="Use owner id for this Telegram user.")
    debug_entries.add_argument("--limit", type=int, default=10)

    parse_image = subparsers.add_parser("parse-image", help="Parse one local image through configured LLM.")
    parse_image.add_argument("path", type=Path)

    mock_run = subparsers.add_parser("mock-run", help="Process built-in mock operations without Telegram or LLM.")
    mock_run.add_argument("--tag", default="demo", help="Use a unique tag to avoid image and operation duplicates.")

    args = parser.parse_args()
    context = build_context()

    if args.command == "check":
        missing = missing_required_subcategories(context.category_book)
        print(f"Database: {context.settings.database_url.split('@')[-1]}")
        print(f"LLM provider: {context.settings.llm_provider}")
        for warning in _config_warnings(context.settings):
            print(f"Config warning: {warning}")
        if missing:
            print("Missing recommended subcategories:")
            for item in missing:
                print(f"- {item}")
        else:
            print("Category check: OK")
        return

    if args.command == "telegram":
        TelegramBot(context).run_polling()
        return

    if args.command == "sync":
        print("sync is deprecated; use export-excel")
        return

    if args.command == "export-excel":
        today = date.today()
        if args.period:
            period = _parse_stats_period(args.period, today.year)
            if period is None:
                raise RuntimeError("Use period format like 01.08-24.08")
            start_date, end_date = period
        else:
            start_date, end_date = today.replace(day=1), today
        path = ExcelExporter(context.storage, context.settings.export_dir).export(args.owner, start_date, end_date)
        print(f"Excel export: {path}")
        return

    if args.command == "openai-check":
        if context.settings.llm_provider != "openai":
            print(f"LLM provider is {context.settings.llm_provider}; set LLM_PROVIDER=openai to use OpenAI.")
            return
        if context.settings.openai_proxy_url:
            print(f"OpenAI proxy: {_mask_url_secret(context.settings.openai_proxy_url)}")
        else:
            print("OpenAI proxy: none")
        client = context.vision_client
        if not hasattr(client, "check_model_access"):
            raise RuntimeError("Configured OpenAI client does not support model checks")
        model = client.check_model_access()
        print(f"OpenAI model access OK: {model.get('id')}")
        return

    if args.command == "telegram-check":
        print(f"Telegram API base: {context.settings.telegram_api_base_url}")
        if context.settings.telegram_proxy_url:
            print(f"Telegram proxy: {_mask_url_secret(context.settings.telegram_proxy_url)}")
        else:
            print(f"Use env proxy: {context.settings.use_env_proxy}")
        me = TelegramBot(context).check_connection()
        print(f"Telegram bot: @{me.get('username')} ({me.get('first_name')})")
        print(f"Allowed user ids: {sorted(context.settings.telegram_allowed_user_ids)}")
        if context.settings.telegram_allow_all:
            print("Access: TELEGRAM_ALLOW_ALL=true")
        return

    if args.command == "parse-image":
        content = args.path.read_bytes()
        mime_type = mimetypes.guess_type(args.path.name)[0] or "image/jpeg"
        result = context.parse_and_process(content, mime_type, date.today())
        _print_processing_result(result)
        return

    if args.command == "stats":
        today = date.today()
        if args.period:
            period = _parse_stats_period(args.period, today.year)
            if period is None:
                raise RuntimeError("Use period format like 01.08-24.08")
            start_date, end_date = period
        else:
            start_date, end_date = today.replace(day=1), today
        for line in _expense_report_lines(context.storage.expense_summary(start_date, end_date)):
            print(line)
        return

    if args.command == "debug-entries":
        owner_id = telegram_owner_id(args.telegram_user_id) if args.telegram_user_id is not None else args.owner
        with context.owner_scope(owner_id):
            print(f"Owner: {context.owner_id}")
            print("Recent budget entries:")
            for row in context.storage.recent_budget_entries(args.limit):
                print(
                    f"- #{row['id']} {row['operation_date']} {row['operation_type']} "
                    f"{row['amount']:.2f} {row.get('category') or '-'} / {row.get('subcategory') or '-'} "
                    f"{row['name']}"
                )
            print("Recent operations:")
            for row in context.storage.recent_operations(args.limit):
                operation = operation_from_json(row["operation_json"])
                print(
                    f"- {row['status']} row={row.get('workbook_row')} "
                    f"{operation.date} {operation.type.value} {operation.amount:.2f} {operation.name} "
                    f"note={row.get('status_note') or '-'}"
                )
        return

    if args.command == "mock-run":
        run_date = date.today()
        parsed = ParsedScreenshot.from_json(sample_screenshot_payload(run_date, args.tag))
        result = context.processor.process(
            image_content=sample_image_content(args.tag),
            parsed=parsed,
            telegram_file_id=f"mock:{args.tag}",
        )
        _print_processing_result(result)
        return


def _config_warnings(settings) -> list[str]:
    warnings: list[str] = []
    if settings.llm_provider == "openai":
        key = settings.openai_api_key.strip()
        if not key:
            warnings.append("OPENAI_API_KEY is empty")
        elif not key.startswith("sk-") or len(key) < 40:
            warnings.append("OPENAI_API_KEY looks too short or has an unexpected prefix")
    if settings.llm_provider == "gemini" and not settings.gemini_api_key.strip():
        warnings.append("GEMINI_API_KEY is empty")
    return warnings


def _print_processing_result(result) -> None:
    print(f"Bank: {result.bank}")
    print(f"Image hash: {result.image_hash}")
    if not result.decisions:
        print("No decisions: image already processed")
        return
    for decision in result.decisions:
        operation = decision.operation
        row = f" row={decision.workbook_row}" if decision.workbook_row is not None else ""
        print(
            f"{decision.status.value}: {operation.date} {operation.name} "
            f"{operation.amount} {operation.category}/{operation.subcategory} "
            f"({decision.reason}){row}"
        )


def _mask_url_secret(url: str) -> str:
    parts = urlsplit(url)
    if not parts.password:
        return url
    username = parts.username or ""
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:<password>@{hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
