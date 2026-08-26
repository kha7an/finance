from __future__ import annotations

from datetime import date

from budget_bot.categories import CategoryBook
from budget_bot.llm import build_prompt


def _category_book() -> CategoryBook:
    return CategoryBook(expense_categories={"Еда": ["Супермаркеты"]}, income_categories=["Зарплата"])


def test_prompt_keeps_date_headers_directional() -> None:
    prompt = build_prompt(
        _category_book(),
        date(2026, 8, 20),
    )

    assert "Заголовок дня никогда не относится к операциям выше него" in prompt
    assert "предыдущая дата больше не действует" in prompt
    assert "Зеленые входящие поступления от людей" in prompt


def test_single_screenshot_prompt_skips_operations_above_first_date_header() -> None:
    prompt = build_prompt(_category_book(), date(2026, 8, 24), screenshot_count=1)

    assert "Это один отдельный скриншот, не альбом" in prompt
    assert "не добавляй эти верхние операции в operations" in prompt


def test_album_prompt_continues_date_before_next_header() -> None:
    prompt = build_prompt(_category_book(), date(2026, 8, 24), screenshot_count=2)

    assert "Это пачка из 2 скриншотов" in prompt
    assert "продолжай дату с предыдущего скриншота пачки" in prompt
    assert "первый скриншот пачки" in prompt
