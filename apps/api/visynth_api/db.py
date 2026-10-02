"""Kết nối PostgreSQL + giao dịch ngắn (M1).

Quy ước:
- Mỗi thao tác mở một giao dịch ngắn qua `Database.tx()`; không giữ giao dịch qua lời gọi mạng.
- Các bất biến nghiệp vụ (trừ tín dụng, đặt chỗ) nằm ở hàm SQL trong `docs/db/schema.sql`, không viết lại ở Python.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_FILE = Path(__file__).resolve().parents[3] / "docs" / "db" / "schema.sql"


class Database:
    """Bọc `psycopg_pool.ConnectionPool` với các tiện ích ngắn gọn."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        self.dsn = dsn
        self.pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            kwargs={"row_factory": dict_row},
            name="visynth-api",
        )

    # ------------------------------------------------------------------ tiện ích
    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def tx(self) -> Iterator[psycopg.Cursor]:
        """Giao dịch ngắn: commit khi thoát bình thường, rollback khi có lỗi."""
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

    # ------------------------------------------------------------------ lược đồ
    def schema_version(self) -> str | None:
        """Phiên bản lược đồ (Alembic) đang áp trên CSDL; `None` nếu chưa chạy migration."""
        row = self.one("SELECT version_num FROM alembic_version LIMIT 1")
        return row["version_num"] if row else None

    def table_count(self) -> int:
        return int(
            self.scalar(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            or 0
        )


def apply_schema(dsn: str, schema_file: Path | None = None) -> None:
    """Áp `docs/db/schema.sql` (idempotent nhờ `IF NOT EXISTS`/`CREATE OR REPLACE` chưa có — dùng Alembic khi đã có bảng)."""
    path = schema_file or SCHEMA_FILE
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
