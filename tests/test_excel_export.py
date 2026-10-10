from datetime import date
from types import SimpleNamespace

import pytest

from budget_bot import app_factory, cli, excel_exporter
from budget_bot.telegram_bot import TelegramBot


@pytest.mark.parametrize("entrypoint", ["cli", "telegram"])
@pytest.mark.parametrize("period", [None, "01.08-24.08"])
def test_export_defaults_to_all_dates_and_preserves_explicit_period(
    monkeypatch, tmp_path, entrypoint, period
) -> None:
    context = SimpleNamespace(
        storage=object(),
        settings=SimpleNamespace(export_dir=tmp_path),
        owner_id="telegram:123",
    )
    exports = []
    path = tmp_path / "budget.xlsx"

    def export(self, owner_id, start_date, end_date):
        exports.append((owner_id, start_date, end_date))
        return path

    monkeypatch.setattr(excel_exporter.ExcelExporter, "export", export)
    if entrypoint == "cli":
        monkeypatch.setattr(app_factory, "build_context", lambda: context)
        args = ["budget-bot", "export-excel", "--owner", context.owner_id]
        if period:
            args.extend(["--period", period])
        monkeypatch.setattr("sys.argv", args)
        cli.main()
    else:
        bot = object.__new__(TelegramBot)
        bot.context = context
        documents = []
        bot._send_document = lambda chat_id, file: documents.append((chat_id, file)) or True
        bot._handle_export_text(123, f"/export {period}" if period else "/export")
        assert documents == [(123, path)]

    expected = (
        (date(date.today().year, 8, 1), date(date.today().year, 8, 24))
        if period else (date.min, date.max)
    )
    assert exports == [(context.owner_id, *expected)]
