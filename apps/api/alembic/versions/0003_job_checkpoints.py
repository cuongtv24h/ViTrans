"""0003: bảng `job_checkpoints` (điểm lưu trạng thái pipeline theo giai đoạn, M1).

Bảng này đã nằm trong `docs/db/schema.sql` (hợp đồng), nên migration phải chịu được
trường hợp baseline vừa tạo bảng (cài mới) — vì vậy dùng `IF NOT EXISTS`.
"""

from __future__ import annotations

from alembic import op

revision = "0003_job_checkpoints"
down_revision = "0002_local_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS job_checkpoints (
          job_id     uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          stage      text NOT NULL,
          state      jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (job_id, stage)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_checkpoints;")
