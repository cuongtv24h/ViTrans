"""Bọc `psycopg` cho phía WORKER (không phụ thuộc FastAPI).

`dbstore` chỉ cần bốn hàm `one/all/scalar/execute` và một `tx()`; API dùng lớp `Database` của
`visynth_api.db`, còn worker (container riêng, không cài FastAPI) dùng lớp mỏng này — nhờ vậy cùng
một đoạn mã đọc/ghi pool chạy được ở cả hai nơi, không có hai bản logic lệch nhau.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class PgStore:
    """Giao diện CSDL tối thiểu dùng chung với `visynth_api.db.Database` (one/all/scalar/execute/tx)."""

    def __init__(self, dsn: str, *, min_size: int = 0, max_size: int = 4) -> None:
        self.dsn = dsn
        self.pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=True, kwargs={"row_factory": dict_row}, name="visynth-pool"
        )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def tx(self) -> Iterator[psycopg.Cursor]:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            yield cur

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def jsonb(value: Any) -> Jsonb:
    """`Jsonb` nhận UUID/date/Decimal (JSONB của PostgreSQL chỉ nhận chuỗi)."""
    return Jsonb(value, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))
