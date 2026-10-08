from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from io import BytesIO

from pypdf import PdfReader

from .models import OperationType, ParsedOperation, ParsedScreenshot


MONEY = r"[+-]?\d[\d ]*[.,]\d{2}"
ROW = re.compile(
    rf"(?P<date>\d{{2}}\.\d{{2}}\.\d{{4}})\s+\d{{2}}:\d{{2}}\s+"
    rf"\d{{2}}\.\d{{2}}\.\d{{4}}\s+\d{{2}}:\d{{2}}\s+"
    rf"{MONEY}\s+[^\s\d]+\s+(?P<amount>{MONEY})\s+₽\s+"
    r"(?P<description>.+?)\s+\d{4}(?=\s*\n|\s*$)",
    re.DOTALL,
)


def _money(text: str) -> Decimal:
    return Decimal(text.replace(" ", "").replace(",", "."))


def parse_bank_statement(content: bytes) -> ParsedScreenshot:
    if not content.startswith(b"%PDF-"):
        raise ValueError("Файл не является PDF.")
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("PDF защищён паролем. Пришли выписку без пароля.")
        if not 1 <= len(reader.pages) <= 50:
            raise ValueError("Выписка должна содержать от 1 до 50 страниц.")
        pages = [(page.extract_text() or "").replace("\xa0", " ") for page in reader.pages]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Не удалось прочитать PDF. Пришли исходную выписку из банка.") from exc

    text = "\n".join(pages)
    if "Справка о движении средств" not in text or "ТБАНК" not in text.upper():
        raise ValueError("Поддерживается текстовая справка о движении средств Т-Банка.")
    period = re.search(r"Движение средств за период с (\d{2}\.\d{2}\.\d{4}) по (\d{2}\.\d{2}\.\d{4})", text)
    if period is None:
        raise ValueError("Не удалось определить период выписки.")
    start, end = [datetime.strptime(value, "%d.%m.%Y").date() for value in period.groups()]
    if start > end:
        raise ValueError("Некорректный период выписки.")

    operations = []
    amounts = []
    for page in pages:
        table = re.search(r"Номер\s+карты\s+(.*?)АО «ТБанк»", page, re.DOTALL)
        if table is None:
            raise ValueError("Не удалось прочитать таблицу выписки целиком.")
        body = re.split(rf"{MONEY}\s+₽\s*Пополнения:", table.group(1), maxsplit=1)[0].strip()
        if ROW.sub("", body).strip():
            raise ValueError("Не удалось прочитать все строки выписки. Ничего не записано.")
        for row in ROW.finditer(body):
            operation_date = datetime.strptime(row["date"], "%d.%m.%Y").date()
            if not start <= operation_date <= end:
                raise ValueError("Дата операции выходит за период выписки.")
            amount = _money(row["amount"])
            description = " ".join(row["description"].split())
            name = re.sub(r"^Оплата (?:в|услуг)\s+", "", description)
            if "перевод" in description.casefold():
                operation_type = OperationType.TRANSFER
            elif amount > 0:
                operation_type = OperationType.INCOME
            elif description.startswith("Оплата "):
                operation_type = OperationType.EXPENSE
            else:
                operation_type = OperationType.IGNORE
            amounts.append(amount)
            operations.append(ParsedOperation(
                date=operation_date,
                name=name,
                amount=float(amount),
                type=operation_type,
                note=description,
            ))

    # Validate bank totals before the processor can write any operations.
    for label, total in (
        ("Пополнения", sum((value for value in amounts if value > 0), Decimal(0))),
        ("Расходы", -sum((value for value in amounts if value < 0), Decimal(0))),
    ):
        summary = re.search(rf"({MONEY})\s+₽\s*{label}:", text)
        if summary is None or _money(summary[1]) != total:
            raise ValueError("Суммы операций не совпадают с итогами выписки. Ничего не записано.")
    if not operations:
        raise ValueError("В выписке нет операций.")
    return ParsedScreenshot(bank="tbank", operations=operations, raw={"source": "tbank_pdf", "operations": len(operations)})
