"""Fixture dùng chung cho test CSDL/API (M1).

Chạy trên PostgreSQL THẬT do `pgserver` dựng tạm (nhị phân PG nhúng). Nếu thiếu `pgserver`
thì các test CSDL bị BỎ QUA (không đánh dấu là đạt) — xem `docs/BUILD_PLAN.md`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

try:  # pragma: no cover - phụ thuộc môi trường
    import pgserver

    HAVE_PGSERVER = True
except ImportError:  # pragma: no cover
    pgserver = None
    HAVE_PGSERVER = False


def _external_dsn() -> str | None:
    return os.environ.get("VISYNTH_TEST_DSN") or None


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """DSN của một cụm PostgreSQL dùng riêng cho test (hoặc `VISYNTH_TEST_DSN` nếu có)."""
    external = _external_dsn()
    if external:
        yield external
        return
    if not HAVE_PGSERVER:
        pytest.skip("cần `pgserver` (hoặc VISYNTH_TEST_DSN) để chạy test CSDL")
    data_dir = tempfile.mkdtemp(prefix="visynth-pg-")
    server = pgserver.get_server(data_dir)
    try:
        yield server.get_uri()
    finally:
        try:
            server.cleanup()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - dọn dẹp là việc phụ
            shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def pg_schema(pg_dsn: str) -> str:
    """Áp migration Alembic một lần cho cả phiên test (mỗi test dọn bảng trước khi chạy)."""
    from visynth_api.migrate import upgrade

    upgrade(pg_dsn)
    return pg_dsn


@pytest.fixture
def db(pg_schema: str):
    """Kết nối CSDL đã sạch dữ liệu (giữ nguyên bảng + hàm)."""
    from visynth_api.db import Database

    database = Database(pg_schema)
    # Bảng do schema.sql seed sẵn (giá, cấu hình) không xoá để test dùng được ngay.
    keep = {"alembic_version", "llm_prices", "app_settings"}
    tables = [
        row["tablename"]
        for row in database.all("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        if row["tablename"] not in keep
    ]
    if tables:
        database.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
    yield database
    database.close()
