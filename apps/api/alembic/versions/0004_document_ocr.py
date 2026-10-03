"""0004: `documents.source_kind` + `documents.ocr_pages` (M1 — OCR PDF bằng P10).

`source_kind` cho biết nguồn đã bóc tách được là chữ có sẵn (`text`) hay do OCR (`ocr`) — dùng cho
điểm chất lượng bóc tách và cho giao diện cảnh báo "kết quả do OCR". `ocr_pages` là danh sách trang
cần OCR (worker lập cụm 10–15 trang từ danh sách này, §6.0/§6.1).

Hai cột cũng đã nằm trong `docs/db/schema.sql` (hợp đồng) nên migration phải chịu được trường hợp
baseline vừa tạo cột (cài mới) — vì vậy dùng `IF NOT EXISTS`.
"""

from __future__ import annotations

from alembic import op

revision = "0004_document_ocr"
down_revision = "0003_job_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_kind text;
        ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_source_kind_check;
        ALTER TABLE documents ADD CONSTRAINT documents_source_kind_check
            CHECK (source_kind IS NULL OR source_kind IN ('text','ocr'));
        ALTER TABLE documents ADD COLUMN IF NOT EXISTS ocr_pages jsonb NOT NULL DEFAULT '[]'::jsonb;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_source_kind_check;
        ALTER TABLE documents DROP COLUMN IF EXISTS ocr_pages;
        ALTER TABLE documents DROP COLUMN IF EXISTS source_kind;
        """
    )
