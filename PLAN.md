# План: Budget Bot

## Текущее состояние

Локальное приложение на Python: Telegram-бот и опциональный FastAPI upload принимают скриншоты банковских операций, vision LLM извлекает JSON, backend валидирует и пишет в **Postgres**. Excel формируется по запросу из Postgres, а не является хранилищем.

- **Хранилище:** Postgres (`budget_entries`, `operations`, `source_images`, `pending_actions`, категории per-owner).
- **LLM:** OpenAI (default) или Gemini; `mock` — только явный dev-режим.
- **Интерфейсы:** Telegram long polling, HTTP `/parse-image` с bearer-токеном.
- **Multi-user:** изоляция по `owner_id` (Telegram user id).

## Следующие приоритеты

### Архитектура

- Разбить `telegram_bot.py`:
  - `telegram_polling.py` — loop, API, dispatch
  - `telegram_media.py` — фото и альбомы
  - `telegram_callbacks.py` — callback routing
  - `telegram_reports.py` — stats/analytics/export
  - `telegram_reminders.py` — reminders
  - `telegram_messages.py` — keyboards/text
- Разбить `storage.py`:
  - schema / migrations
  - `operations_repo`, `budget_entries_repo`, `categories_repo`, `telegram_state_repo`, `reminders_repo`

### База

- Alembic вместо inline `CREATE TABLE IF NOT EXISTS` — `python -m budget_bot.cli migrate`.
- Таблица `parse_jobs` для очереди LLM-обработки.

### Надёжность

- Очередь LLM-задач в Postgres (`queued` / `running` / `done` / `failed`): long polling не блокируется на распознавании, есть retry.

### Observability

- `logging` вместо `print` в pipeline, parse jobs и FastAPI.

### Безопасность / API

- FastAPI: rate limiting, `lifespan` + dependency injection для `AppContext`.

## Правила домена (актуальные)

- Уверенные расходы с валидной категорией → автозапись в Postgres.
- Доходы, переводы, `needs_review`, дубли, неполные данные → pending в Telegram.
- Кэшбэк и внутренние переводы между своими счетами → ignore.
- Дедупликация по hash изображения и операции.
- Выученные категории по названию мерчанта.
- Несколько скринов одним альбомом → один LLM batch.

## Assumptions

- Запуск локально или в Docker на личной машине / VPS.
- Postgres не публикуется наружу по умолчанию.
- Скриншоты истории операций, не PDF и не банковские API.
