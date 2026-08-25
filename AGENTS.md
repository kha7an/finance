# Repository Guidelines

## Project Structure & Module Organization

This repository is a local Python MVP for parsing bank screenshots and writing budget operations to Excel. Application code lives in `src/budget_bot/`. Key modules include `cli.py` for command-line entry points, `server.py` for FastAPI upload handling, `telegram_bot.py` for Telegram polling, `processor.py` for operation decisions, `excel_writer.py` for workbook writes, and `storage.py` for SQLite state.

Tests are in `tests/`, currently centered on `tests/test_core.py`. Runtime data is under `data/`, including `budget_bot.sqlite3` and saved images. The root workbook files, such as `Копия 0.xlsx` and timestamped `.before-*` copies, are user data and should be handled carefully.

## Build, Test, and Development Commands

Create and activate a virtual environment before working:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run tests with:

```bash
PYTHONPATH=src pytest
```

Check local configuration and workbook categories:

```bash
PYTHONPATH=src python -m budget_bot.cli check
```

Run a no-LLM smoke test that writes mock operations:

```bash
PYTHONPATH=src python -m budget_bot.cli mock-run --tag smoke-1
```

Run services locally with `PYTHONPATH=src python -m budget_bot.cli telegram` for Telegram polling, or `PYTHONPATH=src uvicorn budget_bot.server:app --reload` for FastAPI.

## Coding Style & Naming Conventions

Use Python 3.9+ and follow the existing standard-library-first style. Keep modules small and purpose-specific. Use 4-space indentation, type hints, `dataclass` models where appropriate, and explicit enum values for operation states and types. Prefer snake_case for functions, variables, modules, and test names; use PascalCase for classes.

## Testing Guidelines

The project uses `pytest`, configured in `pyproject.toml` with `pythonpath = ["src"]` and `testpaths = ["tests"]`. Add focused tests near related behavior in `tests/test_core.py`, or split into new `test_*.py` files as coverage grows. Prefer temporary directories and generated workbooks over modifying real files in the repository.

## Commit & Pull Request Guidelines

No Git history is available from this checkout, so use clear, imperative commit subjects such as `Add Telegram date confirmation test` or `Fix duplicate operation handling`. Pull requests should summarize behavior changes, list verification commands run, mention workbook or database migration risks, and include screenshots only when Telegram or HTTP user-facing flows change.

## Security & Configuration Tips

Keep secrets in `.env`, never in source or docs. Required local settings include `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, `OPENAI_API_KEY`, `BUDGET_WORKBOOK_PATH`, and `BUDGET_API_TOKEN`. Treat `.xlsx` budget files and `data/budget_bot.sqlite3` as personal financial data; avoid committing real records or screenshots.
