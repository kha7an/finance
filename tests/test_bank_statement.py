from datetime import date
from types import SimpleNamespace

import pytest

from budget_bot.app_factory import AppContext
from budget_bot.bank_statement import parse_bank_statement
from budget_bot.models import OperationType


HEADER = "ТБАНК\nСправка о движении средств\nДвижение средств за период с 01.10.2026 по 08.10.2026\nНомер\nкарты\n"
FOOTER = "\nАО «ТБанк» универсальная лицензия\n"
PAYMENT = "07.10.2026\n23:05\n08.10.2026\n03:26\n-10.00 USD -1 000.00 ₽ Оплата в TEST\nSHOP RUS\n1234\n"
TRANSFER = "06.10.2026\n12:00\n06.10.2026\n12:01\n-200.00 ₽ -200.00 ₽ Внутренний перевод на\nдоговор 1234567890\n1234\n"
INCOME = "05.10.2026\n12:00\n05.10.2026\n12:01\n+300.00 ₽ +300.00 ₽ Пополнение\n1234\n"


def _pdf(monkeypatch, pages):
    reader = SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda text=text: text) for text in pages])
    monkeypatch.setattr("budget_bot.bank_statement.PdfReader", lambda _content: reader)


def test_statement_uses_operation_date_and_card_amount_across_pages(monkeypatch):
    _pdf(monkeypatch, [
        HEADER + PAYMENT + FOOTER,
        "Номер\nкарты\n" + TRANSFER + INCOME + "300,00 ₽Пополнения:\n1 200,00 ₽Расходы:" + FOOTER,
    ])

    parsed = parse_bank_statement(b"%PDF-test")

    assert parsed.bank == "tbank"
    assert [(op.type, op.amount) for op in parsed.operations] == [
        (OperationType.EXPENSE, -1000), (OperationType.TRANSFER, -200), (OperationType.INCOME, 300),
    ]
    assert parsed.operations[0].date == date(2026, 10, 7)
    assert parsed.operations[0].name == "TEST SHOP RUS"
    assert parsed.operations[1].name.startswith("Внутренний перевод")


def test_statement_preserves_repeated_payments(monkeypatch):
    _pdf(monkeypatch, [HEADER + PAYMENT * 2 + "0,00 ₽Пополнения:\n2 000,00 ₽Расходы:" + FOOTER])
    assert len(parse_bank_statement(b"%PDF-test").operations) == 2


@pytest.mark.parametrize("body, error", [
    (PAYMENT + "0,00 ₽Пополнения:\n999,00 ₽Расходы:", "не совпадают"),
    (PAYMENT + "07.10.2026\n23:00\nнеполная строка\n0,00 ₽Пополнения:\n1 000,00 ₽Расходы:", "все строки"),
    (PAYMENT.replace("07.10.2026", "07.09.2026") + "0,00 ₽Пополнения:\n1 000,00 ₽Расходы:", "выходит за период"),
])
def test_statement_rejects_incomplete_or_inconsistent_data(monkeypatch, body, error):
    _pdf(monkeypatch, [HEADER + body + FOOTER])
    with pytest.raises(ValueError, match=error):
        parse_bank_statement(b"%PDF-test")


def test_statement_rejects_non_pdf_and_scans(monkeypatch):
    with pytest.raises(ValueError, match="не является PDF"):
        parse_bank_statement(b"not a pdf")
    _pdf(monkeypatch, [""])
    with pytest.raises(ValueError, match="текстовая справка"):
        parse_bank_statement(b"%PDF-test")


def test_pdf_pipeline_does_not_call_llm_or_write_unvalidated_statement(monkeypatch):
    _pdf(monkeypatch, [HEADER + PAYMENT + "0,00 ₽Пополнения:\n999,00 ₽Расходы:" + FOOTER])
    context = object.__new__(AppContext)
    context.settings = SimpleNamespace(max_upload_bytes=1000)
    # No vision client or storage: invalid data must fail before either is used.
    with pytest.raises(ValueError, match="не совпадают"):
        context.parse_and_process(b"%PDF-test", "application/pdf", date(2026, 10, 8))


def test_pdf_pipeline_passes_validated_operations_to_existing_processor(monkeypatch):
    _pdf(monkeypatch, [HEADER + PAYMENT + "0,00 ₽Пополнения:\n1 000,00 ₽Расходы:" + FOOTER])
    calls = []
    processor = SimpleNamespace(process=lambda *args: calls.append(args) or "result")
    context = object.__new__(AppContext)
    context.settings = SimpleNamespace(max_upload_bytes=1000)
    context._runtime = lambda: SimpleNamespace(processor=processor)
    assert context.parse_and_process(b"%PDF-test", "application/pdf", date(2026, 10, 8), "file-id") == "result"
    assert calls[0][2] == "file-id"
    assert calls[0][1].operations[0].amount == -1000
