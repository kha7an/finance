"""Calendar rules and money validation shared by every interface."""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


def amount_decimal(value) -> Decimal:
    try:
        amount = Decimal(str(value).replace(" ", "").replace(",", "."))
        if (
            not amount.is_finite()
            or amount <= 0
            or amount > Decimal("999999999999.99")
            or amount != amount.quantize(Decimal("0.01"))
        ):
            raise ValueError
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Нужна положительная сумма с точностью до копейки.") from None


def month_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, monthrange(year, month)[1]))


def financial_period(today: date, day: int = 1) -> tuple[date, date]:
    if not 1 <= day <= 31:
        raise ValueError("День начала периода: от 1 до 31.")
    index = today.year * 12 + today.month - 1
    start = month_day(today.year, today.month, day)
    if today < start:
        index -= 1
        year, month = divmod(index, 12)
        start = month_day(year, month + 1, day)
    year, month = divmod(index + 1, 12)
    return start, month_day(year, month + 1, day) - timedelta(days=1)


def recurring_dates(start: date, end: date, frequency: str):
    current, offset = start, 0
    while current <= end:
        yield current
        offset += 1
        if frequency == "week":
            current = start + timedelta(weeks=offset)
        else:
            index = start.year * 12 + start.month - 1 + offset
            year, month = divmod(index, 12)
            current = month_day(year, month + 1, start.day)
