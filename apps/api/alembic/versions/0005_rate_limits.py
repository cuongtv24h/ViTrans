"""0005: bảng + hàm `rate_limit_*` cho giới hạn tốc độ (M3 — làm cứng, §20.4).

Vì sao làm ở tầng CSDL thay vì trong tiến trình: VPS chạy nhiều bản API/worker và `uvicorn` có thể
nhiều tiến trình, nên bộ đếm trong RAM sẽ bị "chia nhỏ" và kẻ tấn công chỉ cần gõ nhiều kết nối hơn là
vượt được hạn mức. PostgreSQL đã có sẵn, giao dịch ngắn, không thêm dịch vụ.

Bảng chỉ lưu **khoá băm** của danh tính (IP hoặc token) chứ không lưu IP thô — người dùng không bị
lưu địa chỉ mạng chỉ vì gõ sai mật khẩu vài lần (§14.2).

Hai hàm dùng cùng một dạng cửa sổ cố định (fixed window) canh theo mốc thời gian tuyệt đối, nên nhiều
tiến trình gọi song song vẫn đếm chung một chỗ. Dọn bảng bằng `rate_limit_gc()` (gọi trong `visynth ops reap`).
"""

from __future__ import annotations

from alembic import op

revision = "0005_rate_limits"
down_revision = "0004_document_ocr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limit_hits (
          scope        text NOT NULL,
          subject      text NOT NULL,
          window_start timestamptz NOT NULL,
          hits         integer NOT NULL DEFAULT 0,
          PRIMARY KEY (scope, subject, window_start)
        );
        CREATE INDEX IF NOT EXISTS rate_limit_window_idx ON rate_limit_hits (window_start);
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION rate_limit_hit(
          p_scope text, p_subject text, p_limit integer, p_window_s integer
        ) RETURNS TABLE(allowed boolean, hits integer, remaining integer, retry_after_s integer)
        LANGUAGE plpgsql AS $$
        DECLARE
          v_start timestamptz;
          v_hits integer;
          v_retry integer;
        BEGIN
          -- `p_limit <= 0` = TẮT hạn mức (máy phát triển); luôn cho qua nhưng vẫn nói rõ là đang tắt.
          IF p_limit <= 0 OR p_window_s <= 0 THEN
            RETURN QUERY SELECT true, 0, 0, 0;
            RETURN;
          END IF;
          v_start := to_timestamp(floor(extract(epoch FROM now()) / p_window_s) * p_window_s);
          v_retry := GREATEST(1, CEIL(p_window_s - (extract(epoch FROM now()) - extract(epoch FROM v_start))))::integer;
          INSERT INTO rate_limit_hits (scope, subject, window_start, hits)
               VALUES (p_scope, p_subject, v_start, 1)
          ON CONFLICT (scope, subject, window_start)
            DO UPDATE SET hits = rate_limit_hits.hits + 1
            RETURNING rate_limit_hits.hits INTO v_hits;
          RETURN QUERY SELECT v_hits <= p_limit, v_hits, GREATEST(0, p_limit - v_hits), v_retry;
        END $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION rate_limit_gc(p_keep interval DEFAULT '1 hour')
        RETURNS integer LANGUAGE plpgsql AS $$
        DECLARE v_deleted integer;
        BEGIN
          DELETE FROM rate_limit_hits WHERE window_start < now() - p_keep;
          GET DIAGNOSTICS v_deleted = ROW_COUNT;
          RETURN v_deleted;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS rate_limit_gc(interval);
        DROP FUNCTION IF EXISTS rate_limit_hit(text, text, integer, integer);
        DROP TABLE IF EXISTS rate_limit_hits;
        """
    )
