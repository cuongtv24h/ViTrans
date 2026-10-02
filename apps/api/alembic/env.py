"""Môi trường Alembic: chuỗi kết nối lấy từ `VISYNTH_DB_DSN` (không ghi vào repo).

Baseline `0001` áp nguyên `docs/db/schema.sql` (một nguồn sự thật cho lược đồ, khớp `docs/tests/pg_smoke.py`).
Các migration sau chỉ bổ sung thay đổi nhỏ.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import pool as sa_pool

from alembic import context

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:  # cho phép `alembic -c apps/api/alembic.ini upgrade head` từ gốc repo
    sys.path.insert(0, str(ROOT))

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def _url() -> str:
    dsn = os.environ.get("VISYNTH_DB_DSN") or config.get_main_option("sqlalchemy.url") or ""
    if not dsn:
        raise RuntimeError("thiếu VISYNTH_DB_DSN (hoặc sqlalchemy.url) để chạy migration")
    # SQLAlchemy cần URL có driver tường minh cho psycopg3.
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return dsn


def run_migrations_offline() -> None:
    """Sinh SQL (`alembic upgrade head --sql`) — không kết nối CSDL."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Chạy migration trên CSDL thật."""
    engine = create_engine(_url(), poolclass=sa_pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
