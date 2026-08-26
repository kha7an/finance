# Repository Guidelines

## Project Structure & Module Organization

This repository is a local Python app for parsing bank screenshots and writing budget operations to Postgres with Excel export on demand. Application code lives in `src/budget_bot/`. Key modules include `cli.py` for command-line entry points, `server.py` for FastAPI upload handling, `telegram_bot.py` for Telegram polling, `processor.py` for operation decisions, `excel_exporter.py` for workbook exports, and `storage.py` for Postgres state.

Runtime data is under `data/`, including saved images and Excel exports. Root `.xlsx` files and timestamped `.before-*` copies are user data and should be handled carefully.

## Build, Test, and Development Commands

Create and activate a virtual environment before working:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Check local configuration and category catalog:

```bash
PYTHONPATH=src python -m budget_bot.cli check
```

Run services locally with `PYTHONPATH=src python -m budget_bot.cli telegram` for Telegram polling, or `PYTHONPATH=src uvicorn budget_bot.server:app --reload` for FastAPI.

Docker Compose is the default runtime: `docker compose up --build bot`. Postgres is not published to the host unless you add a port mapping explicitly.

## Coding Style & Naming Conventions

Use Python 3.9+ and follow the existing standard-library-first style. Keep modules small and purpose-specific. Use 4-space indentation, type hints, `dataclass` models where appropriate, and explicit enum values for operation states and types. Prefer snake_case for functions, variables, modules, and test names; use PascalCase for classes.

## Commit & Pull Request Guidelines

Use clear, imperative commit subjects such as `Add Telegram date confirmation test` or `Fix duplicate operation handling`. Pull requests should summarize behavior changes, list verification commands run, mention database migration risks, and include screenshots only when Telegram or HTTP user-facing flows change.

## Security & Configuration Tips

Keep secrets in `.env`, never in source or docs. Required local settings include `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, `OPENAI_API_KEY` (when `LLM_PROVIDER=openai`), `DATABASE_URL`, and `BUDGET_API_TOKEN` for HTTP upload. Set `LLM_PROVIDER=mock` only for explicit local dev without a real LLM. Treat Postgres data, `.xlsx` exports, and `data/images/` as personal financial data; avoid committing real records or screenshots.
