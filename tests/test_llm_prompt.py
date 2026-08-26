from __future__ import annotations

from datetime import date

from budget_bot.categories import CategoryBook
from budget_bot.llm import build_prompt


def test_prompt_keeps_date_headers_directional() -> None:
    prompt = build_prompt(
        CategoryBook(expense_categories={"Еда": ["Супермаркеты"]}, income_categories=["Зарплата"]),
        date(2026, 8, 20),
    )

    assert "Заголовок дня никогда не относится к операциям выше него" in prompt
    assert "не назначай им дату первого заголовка ниже" in prompt
    assert "предыдущая дата больше не действует" in prompt
    assert "Зеленые входящие поступления от людей" in prompt
