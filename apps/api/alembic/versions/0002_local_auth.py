"""0002: bảng `local_credentials` cho đăng nhập nội bộ (M1)

Revision ID: 0002_local_auth
Revises: 0001_baseline

SPEC §20.2 chốt xác thực bằng **Google OAuth + email OTP** (dịch vụ quản lý), nên `schema.sql`
không có cột mật khẩu. Trong lúc chưa nối nhà cung cấp xác thực (M2), bản chạy trên VPS thử nghiệm
cần một đường đăng nhập nội bộ để vận hành khép kín: mật khẩu băm bằng scrypt, tách bảng riêng.

Khi bật OAuth/OTP thật: chuyển người dùng sang nhà cung cấp ngoài rồi xoá bảng này (migration sau).
"""

from __future__ import annotations

from alembic import op

revision = "0002_local_auth"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS local_credentials (
          user_id       uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          password_hash text NOT NULL,
          created_at    timestamptz NOT NULL DEFAULT now(),
          updated_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE local_credentials IS "
        "'M1: đăng nhập nội bộ tạm thời; thay bằng Google OAuth + email OTP (SPEC §20.2) ở M2'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS local_credentials")
