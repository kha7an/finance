from datetime import date
from decimal import Decimal
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from budget_bot.app_factory import AppContext
from budget_bot.categories import validate_category_name
from budget_bot.models import OperationStatus, OperationType, ParsedOperation
from budget_bot.storage import Storage


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "a/b", "a\nb", "a\tb"])
def test_category_name_rejects_invalid_input(name):
    with pytest.raises(ValueError):
        validate_category_name(name)


def test_category_name_preserves_unicode_and_trims_spaces():
    assert validate_category_name("  Обучение  ") == "Обучение"


def test_cached_processor_refreshes_catalog():
    context = AppContext.__new__(AppContext)
    processor = SimpleNamespace(category_book="old")
    context._runtime_cache = {"test": SimpleNamespace(processor=processor)}
    context.storage = SimpleNamespace(owner_id="test", category_book=lambda: "new")
    assert context.processor is processor
    assert processor.category_book == "new"


@pytest.fixture
def category_storage():
    url = os.getenv("CATEGORY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("CATEGORY_TEST_DATABASE_URL is required (use an isolated test database)")
    storage = Storage(url)
    owners = [f"category-test:{uuid4()}", f"category-test:{uuid4()}"]
    try:
        with storage.owner_scope(owners[0]):
            yield storage, owners
    finally:
        with storage._connect() as connection:
            connection.execute("DELETE FROM users WHERE owner_id = ANY(%s)", (owners,))
        storage.close()


def test_category_crud_and_owner_isolation(category_storage):
    storage, owners = category_storage
    category_id = storage.create_category("expense", "Обучение")
    storage.create_subcategory(category_id, "Курсы")
    category = next(row for row in storage.category_catalog() if row["id"] == category_id)
    assert [row["name"] for row in category["subcategories"]] == ["Разное", "Курсы"]
    sub_id = category["subcategories"][1]["id"]
    with pytest.raises(ValueError, match="уже есть"):
        storage.create_category("expense", "обучение")
    with pytest.raises(ValueError, match="уже есть"):
        storage.create_subcategory(category_id, "курсы")
    with storage.owner_scope(owners[1]):
        assert category_id not in {row["id"] for row in storage.category_catalog()}
        for change in [
            lambda: storage.create_subcategory(category_id, "Secret"),
            lambda: storage.rename_category(category_id, "Secret"),
            lambda: storage.delete_category(category_id),
            lambda: storage.rename_category(category_id, "Secret", sub_id),
            lambda: storage.delete_category(category_id, sub_id),
        ]:
            with pytest.raises(ValueError, match="не найдена"):
                change()
    storage.rename_category(category_id, "Занятия")
    storage.rename_category(category_id, "Лекции", sub_id)
    assert storage.category_book().is_valid_expense("Занятия", "Лекции")
    storage.delete_category(category_id, sub_id)
    with pytest.raises(ValueError, match="последнюю подкатегорию"):
        storage.delete_category(category_id, category["subcategories"][0]["id"])
    storage.delete_category(category_id)
    assert "Занятия" not in storage.category_book().expense_categories


def test_rename_updates_history_rules_pending_and_analytics_atomically(category_storage):
    storage, owners = category_storage
    category_id = storage.create_category("expense", "Обучение")
    operation = ParsedOperation(date(2026, 10, 1), "Course", -100, OperationType.EXPENSE,
                                category="Обучение", subcategory="Разное")
    storage.append_budget_entry("test", "course", operation, "tbank")
    storage.record_image("image", telegram_file_id=None, bank="tbank", status="processed")
    storage.record_operation("course", "image", "tbank", operation, OperationStatus.PENDING)
    storage.save_expense_category_rule("Course", "Обучение", "Разное")
    storage.toggle_mandatory_rule("Обучение", "Разное")
    storage.set_analytics_limit("Обучение", Decimal("1000"))
    storage.add_pending_action("manual-1", 1, None, "manual:" + json.dumps({"kind": "manual_entry", "type": "expense", "category": "Обучение", "subcategory": "Разное"}))
    with pytest.raises(ValueError, match="используется"):
        storage.delete_category(category_id)
    storage.rename_category(category_id, "Занятия")
    sub_id = next(row for row in storage.category_catalog() if row["id"] == category_id)["subcategories"][0]["id"]
    storage.rename_category(category_id, "Лекции", sub_id)
    entry = storage.all_budget_entries(date.min, date.max)[0]
    assert (entry["category"], entry["subcategory"]) == ("Занятия", "Лекции")
    assert storage.get_expense_category_rule("Course") == ("Занятия", "Лекции")
    payload = storage.get_operation("course")["operation_json"]
    assert (payload["category"], payload["subcategory"]) == ("Занятия", "Лекции")
    pending = json.loads(storage.get_pending_action("manual-1", 1)["prompt"][7:])
    assert (pending["category"], pending["subcategory"]) == ("Занятия", "Лекции")
    settings = storage.analytics_settings()
    assert settings["mandatory_rules"] == [["Занятия", "Лекции"]]
    assert "Обучение" not in settings["limits"]
    assert Decimal(settings["limits"]["Занятия"]) == Decimal("1000")
    with pytest.raises(ValueError, match="используется"):
        storage.delete_category(category_id, sub_id)


def test_income_rename_does_not_change_expenses_with_same_name(category_storage):
    storage, _ = category_storage
    expense_id = storage.create_category("expense", "Общее")
    income_id = storage.create_category("income", "Общее")
    for kind, amount in [(OperationType.EXPENSE, -100), (OperationType.INCOME, 200)]:
        storage.append_budget_entry("test", kind.value, ParsedOperation(date(2026, 10, 1), "Test", amount, kind, category="Общее", subcategory="Разное" if kind == OperationType.EXPENSE else None), "test")
    storage.rename_category(income_id, "Поступления")
    rows = storage.all_budget_entries(date.min, date.max)
    assert {row["operation_type"]: row["category"] for row in rows} == {"expense": "Общее", "income": "Поступления"}
    with pytest.raises(ValueError, match="нет подкатегорий"):
        storage.create_subcategory(income_id, "Test")
    with pytest.raises(ValueError, match="уже есть"):
        storage.rename_category(expense_id, "Еда")
    assert storage.category_book().is_valid_expense("Общее", "Разное")


def test_deletion_waits_for_active_pickers(category_storage):
    storage, _ = category_storage
    category_id = storage.create_category("expense", "Обучение")
    storage.add_pending_action("manual-1", 1, None, "manual:{}")
    with pytest.raises(ValueError, match="ручной ввод"):
        storage.delete_category(category_id)
    storage.delete_pending_action("manual-1")
    storage.delete_category(category_id)


def _category_bot(storage):
    from budget_bot.telegram_bot import TelegramBot

    bot = TelegramBot.__new__(TelegramBot)
    bot.context = SimpleNamespace(storage=storage, category_book=storage.category_book())
    bot.sent_messages = []
    bot.answers = []
    bot._send_message = lambda chat_id, text, reply_markup=None: bot.sent_messages.append((text, reply_markup))
    bot._answer_callback = lambda callback_id, text: bot.answers.append(text)
    bot._delete_callback_message = lambda callback: None
    bot._is_allowed = lambda user_id: user_id == 1
    return bot


def _catalog_click(bot, payload, user_id=1):
    bot._handle_callback({"id": "callback", "from": {"id": user_id}, "message": {"chat": {"id": 1}}, "data": "catalog:" + payload})


def test_telegram_category_lifecycle_and_validation(category_storage):
    storage, _ = category_storage
    bot = _category_bot(storage)
    bot._handle_text({"chat": {"id": 1}, "text": "/categories"})
    assert bot.sent_messages[-1][0] == "Категории расходов"
    _catalog_click(bot, "add:expense")
    bot._handle_text({"chat": {"id": 1}, "text": "Обучение / Курсы"})
    assert "Название должно" in bot.sent_messages[-1][0]
    assert storage.get_pending_action("catalog-1", 1) is not None
    bot._handle_text({"chat": {"id": 1}, "text": "Обучение"})
    category = next(row for row in storage.category_catalog() if row["name"] == "Обучение")
    category_id = category["id"]
    _catalog_click(bot, f"addsub:{category_id}")
    bot._handle_text({"chat": {"id": 1}, "text": "Курсы"})
    _catalog_click(bot, f"rename:{category_id}")
    bot._handle_text({"chat": {"id": 1}, "text": "Занятия"})
    assert storage.category_book().is_valid_expense("Занятия", "Курсы")
    _catalog_click(bot, f"delete:{category_id}")
    assert "Удалить «Занятия»?" in bot.sent_messages[-1][0]
    assert "Занятия" in storage.category_book().expense_categories
    _catalog_click(bot, f"confirm:{category_id}")
    assert "Занятия" not in storage.category_book().expense_categories
    _catalog_click(bot, f"show:{category_id}")
    assert "не найдена" in bot.sent_messages[-1][0]
    _catalog_click(bot, "add:income")
    bot._handle_text({"chat": {"id": 1}, "text": "Стипендия"})
    assert storage.category_book().is_valid_income("Стипендия")


def test_telegram_rejects_unauthorized_callback_and_cancels_input(category_storage):
    storage, _ = category_storage
    bot = _category_bot(storage)
    _catalog_click(bot, "add:expense", user_id=2)
    assert bot.answers[-1] == "Нет доступа"
    assert storage.get_pending_action("catalog-1", 1) is None
    _catalog_click(bot, "add:expense")
    bot._handle_text({"chat": {"id": 1}, "text": "Отмена"})
    assert storage.get_pending_action("catalog-1", 1) is None
    _catalog_click(bot, "add:expense")
    bot._handle_text({"chat": {"id": 1}, "text": "/menu"})
    assert storage.get_pending_action("catalog-1", 1) is None
    assert bot.sent_messages[-1][0] == "Главное меню:"


def test_new_category_works_in_manual_input_cached_processor_and_excel(category_storage, tmp_path):
    from openpyxl import load_workbook
    from budget_bot.excel_exporter import ExcelExporter
    from budget_bot.models import ParsedScreenshot
    from budget_bot.telegram_manual import parse_manual_operation_text

    storage, _ = category_storage
    context = AppContext.__new__(AppContext)
    context.storage = storage
    context._runtime_cache = {}
    processor = context.processor
    category_id = storage.create_category("expense", "Обучение")
    storage.create_subcategory(category_id, "Курсы")
    operation = parse_manual_operation_text("-250 31.10 Обучение / Курсы", 2026, context.category_book)
    assert operation is not None
    assert context.processor is processor
    result = processor.process(b"new-category", ParsedScreenshot("mock", [operation], {}))
    assert result.decisions[0].status == OperationStatus.AUTO_WRITTEN
    path = ExcelExporter(storage, tmp_path).export(storage.owner_id, date(2026, 10, 1), date(2026, 10, 31))
    workbook = load_workbook(path)
    assert workbook["Учет расходов"].cell(2, 4).value == "Обучение"
    assert "Обучение" in [cell.value for cell in workbook["Справочники"][1]]
    workbook.close()


def test_stale_category_button_is_refreshed_without_changing_entry(category_storage):
    from budget_bot.telegram_entries import TelegramEntryEditor

    storage, _ = category_storage
    first = storage.create_category("expense", "Первое")
    storage.create_category("expense", "Второе")
    bot = _category_bot(storage)
    editor = TelegramEntryEditor(bot)
    operation = ParsedOperation(date(2026, 10, 1), "Shop", -100, OperationType.EXPENSE, category="Еда", subcategory="Супермаркеты")
    entry_id = storage.append_budget_entry("test", "stale", operation, "test")
    editor.send_entry_category_picker(1, entry_id)
    buttons = [button for row in bot.sent_messages[-1][1]["inline_keyboard"] for button in row]
    old_data = next(button["callback_data"] for button in buttons if button["text"] == "Первое")
    storage.delete_category(first)
    bot.context.category_book = storage.category_book()
    bot._handle_callback({"id": "callback", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": old_data})
    assert "Справочник изменился" in bot.answers[-1]
    assert storage.get_budget_entry(entry_id)["category"] == "Еда"
    assert bot.sent_messages[-1][0] == "Выбери новую категорию:"
    refreshed = [button for row in bot.sent_messages[-1][1]["inline_keyboard"] for button in row]
    current_data = next(button["callback_data"] for button in refreshed if button["text"] == "Второе")
    assert len(current_data.encode()) <= 64
    bot._handle_callback({"id": "next", "from": {"id": 1}, "message": {"chat": {"id": 1}}, "data": current_data})
    assert "Второе" in bot.sent_messages[-1][0]


def test_malformed_category_state_does_not_expose_internal_error(category_storage):
    storage, _ = category_storage
    bot = _category_bot(storage)
    storage.add_pending_action("catalog-1", 1, None, "catalog:{}")
    bot._handle_text({"chat": {"id": 1}, "text": "Название"})
    assert bot.sent_messages[-1][0] == "Ввод устарел. Открой категории заново."
    assert storage.get_pending_action("catalog-1", 1) is None


def test_category_edits_wait_for_running_parser(category_storage):
    storage, _ = category_storage
    category_id = storage.create_category("expense", "Обучение")
    job_id = storage.enqueue_parse_job(1, "single", {"file_ids": ["test"]})
    with storage._connect() as connection:
        connection.execute("UPDATE parse_jobs SET status = 'running' WHERE id = %s AND owner_id = %s", (job_id, storage.owner_id))
    for change in [lambda: storage.rename_category(category_id, "Занятия"), lambda: storage.delete_category(category_id)]:
        with pytest.raises(ValueError, match="Дождись окончания"):
            change()
    storage.finish_parse_job(job_id, "done")
    storage.rename_category(category_id, "Занятия")
    assert storage.category_book().is_valid_expense("Занятия", "Разное")
