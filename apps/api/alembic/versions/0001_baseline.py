"""baseline: docs/db/schema.sql

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-02

Lược đồ ViSynth có MỘT nguồn sự thật là `docs/db/schema.sql` (khớp `docs/tests/pg_smoke.py`).
Baseline vì vậy áp nguyên tệp đó thay vì chép lại thành mã Python — tránh hai bản lệch nhau.
Từ migration sau mới viết thay đổi nhỏ bằng `op.execute`.

Ghi chú: `op.execute()` của Alembic chỉ chạy MỘT câu lệnh, nên ở đây gọi thẳng driver psycopg
của kết nối hiện tại (`connection.connection.driver_connection`).
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_SQL = Path(__file__).resolve().parents[4] / "docs" / "db" / "schema.sql"


def upgrade() -> None:
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    if op.get_context().as_sql:  # chế độ --sql: in ra để psql chạy
        op.execute(sql)
        return
    bind = op.get_bind()
    raw = bind.connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(sql)


def downgrade() -> None:
    """Xoá toàn bộ lược đồ ViSynth (chỉ dùng khi dựng lại từ đầu)."""
    op.execute("DROP SCHEMA public CASCADE")
    op.execute("CREATE SCHEMA public")
