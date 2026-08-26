from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .helpers import DEFAULT_OWNER_ID, _current_owner_id, normalize_owner_id


class DbConnection:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        self.database_url = database_url
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            open=True,
        )

    @property
    def owner_id(self) -> str:
        return _current_owner_id.get()

    @contextmanager
    def owner_scope(self, owner_id: str) -> Iterator[None]:
        token = _current_owner_id.set(normalize_owner_id(owner_id))
        try:
            yield
        finally:
            _current_owner_id.reset(token)

    @contextmanager
    def connect(self):
        with self._pool.connection() as connection:
            yield connection

    def close(self) -> None:
        self._pool.close()
