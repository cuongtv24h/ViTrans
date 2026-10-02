"""Tài liệu: tải lên, bóc tách, danh sách, xoá (SPEC §6.1, §6.13).

Bóc tách ghi thẳng vào `doc_paragraphs`/`doc_sections` để worker không phải đọc lại tệp gốc.
PDF scan cần OCR (P10) — M2; ở M1 PDF trả 415 kèm hướng dẫn.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import psycopg
from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile

from visynth.extract import Extraction, ExtractionError, extract
from visynth_api.db import Database
from visynth_api.errors import Problem
from visynth_api.security import current_user

router = APIRouter(tags=["documents"])

DOC_COLUMNS = (
    "id, title, source_type, original_filename, mime_type, byte_size, sha256, language_code, language_confidence, "
    "word_count, page_count, token_estimate, extraction_quality, extraction_warnings, status, created_at, expires_at"
)
ALLOWED_SUFFIXES = (".txt", ".md", ".docx")


def _db(request: Request) -> Database:
    return request.app.state.db


def persist_extraction(db: Database, document_id: str, user_id: str, extraction: Extraction) -> None:
    """Ghi `doc_paragraphs`/`doc_sections` — dùng chung cho API (upload) và test."""
    with db.tx() as cur:
        for paragraph in extraction.paragraphs:
            row = paragraph.as_row()
            cur.execute(
                """INSERT INTO doc_paragraphs (document_id, pid, idx, kind, content, section_id, page_start, page_end,
                        timecode_start_ms, timecode_end_ms, speaker, char_count)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (document_id, pid) DO NOTHING""",
                (
                    document_id,
                    row["pid"],
                    row["idx"],
                    row["kind"],
                    row["content"],
                    row["section_id"],
                    row["page_start"],
                    row["page_end"],
                    row["timecode_start_ms"],
                    row["timecode_end_ms"],
                    row["speaker"],
                    row["char_count"],
                ),
            )
        for section in extraction.sections:
            row = section.as_row()
            cur.execute(
                """INSERT INTO doc_sections (document_id, section_id, parent_section_id, title, level, idx, first_pid, last_pid)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (document_id, section_id) DO NOTHING""",
                (
                    document_id,
                    row["section_id"],
                    row["parent_section_id"],
                    row["title"],
                    row["level"],
                    row["idx"],
                    row["first_pid"],
                    row["last_pid"],
                ),
            )


@router.post("/documents", status_code=201)
def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    rights_attested: bool = Form(default=False),
    user: dict = Depends(current_user),
) -> dict:
    """Tải tài liệu lên. Bắt buộc xác nhận quyền sử dụng (`rights_attested`) theo §14.6."""
    db, settings = _db(request), request.app.state.settings
    if not rights_attested:
        raise Problem(422, "rights_not_attested", "cần xác nhận bạn có quyền sử dụng tài liệu này")
    filename = Path(file.filename or "tai-lieu.txt").name
    if filename.lower().endswith(".pdf"):
        raise Problem(415, "pdf_requires_ocr", "PDF scan cần OCR (M2); hãy dán văn bản hoặc dùng .txt/.md/.docx")
    if not filename.lower().endswith(ALLOWED_SUFFIXES):
        raise Problem(415, "unsupported_type", f"chỉ nhận {', '.join(ALLOWED_SUFFIXES)} ở M1")
    raw = file.file.read()
    if len(raw) > settings.max_upload_bytes:
        raise Problem(413, "file_too_large", f"tệp vượt {settings.max_upload_bytes // (1024 * 1024)} MB")
    try:
        parsed = extract(raw, filename=filename, title=title)
    except ExtractionError as exc:
        raise Problem(422, exc.code, str(exc)) from exc

    storage_dir = settings.upload_dir / "documents"
    storage_dir.mkdir(parents=True, exist_ok=True)
    try:
        with db.tx() as cur:
            cur.execute(
                """INSERT INTO documents (user_id, title, source_type, original_filename, mime_type, byte_size, sha256,
                        language_code, language_confidence, word_count, page_count, token_estimate, extraction_quality,
                        extraction_warnings, rights_attested_at, status)
                   VALUES (%s, %s, 'upload', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), 'ready')
                   RETURNING id""",
                (
                    user["id"],
                    parsed.title,
                    filename,
                    file.content_type or "application/octet-stream",
                    len(raw),
                    hashlib.sha256(raw).hexdigest(),
                    parsed.language_code,
                    parsed.language_confidence,
                    parsed.word_count,
                    parsed.page_count,
                    parsed.token_estimate,
                    parsed.extraction_quality,
                    psycopg.types.json.Jsonb(parsed.warnings),
                ),
            )
            document_id = cur.fetchone()["id"]
            cur.execute("UPDATE documents SET storage_key = %s WHERE id = %s", (str(storage_dir), document_id))
            for paragraph in parsed.paragraphs:
                row = paragraph.as_row()
                cur.execute(
                    """INSERT INTO doc_paragraphs (document_id, pid, idx, kind, content, section_id, page_start, page_end,
                            timecode_start_ms, timecode_end_ms, speaker, char_count)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        document_id,
                        row["pid"],
                        row["idx"],
                        row["kind"],
                        row["content"],
                        row["section_id"],
                        row["page_start"],
                        row["page_end"],
                        row["timecode_start_ms"],
                        row["timecode_end_ms"],
                        row["speaker"],
                        row["char_count"],
                    ),
                )
            for section in parsed.sections:
                row = section.as_row()
                cur.execute(
                    """INSERT INTO doc_sections (document_id, section_id, parent_section_id, title, level, idx, first_pid, last_pid)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        document_id,
                        row["section_id"],
                        row["parent_section_id"],
                        row["title"],
                        row["level"],
                        row["idx"],
                        row["first_pid"],
                        row["last_pid"],
                    ),
                )
            cur.execute(
                "SELECT id, title, word_count, language_code, page_count, status, created_at, expires_at FROM documents WHERE id = %s",
                (document_id,),
            )
            document = cur.fetchone()
    except psycopg.Error as exc:
        raise Problem(500, "internal_error", f"không lưu được tài liệu: {exc.diag.message_primary}") from exc
    return {"document": {**document, "warnings": parsed.warnings}}


@router.get("/documents")
def list_documents(
    request: Request, limit: int = 20, cursor: str | None = None, user: dict = Depends(current_user)
) -> dict:
    db = _db(request)
    limit = max(1, min(limit, 100))
    rows = db.all(
        f"""SELECT {DOC_COLUMNS} FROM documents
             WHERE user_id = %s AND deleted_at IS NULL
               AND (%s::timestamptz IS NULL OR created_at < %s::timestamptz)
             ORDER BY created_at DESC LIMIT %s""",
        (user["id"], cursor, cursor, limit + 1),
    )
    items = rows[:limit]
    return {"items": items, "next_cursor": items[-1]["created_at"].isoformat() if len(rows) > limit else None}


@router.get("/documents/{document_id}")
def get_document(document_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    row = db.one(
        f"SELECT {DOC_COLUMNS} FROM documents WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
        (document_id, user["id"]),
    )
    if row is None:
        raise Problem(404, "not_found", "không có tài liệu này")
    return row


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    """Xoá mềm + xoá đoạn văn đã bóc tách (giữ hàng `documents` cho kiểm toán)."""
    db = _db(request)
    row = db.one(
        "SELECT id FROM documents WHERE id = %s AND user_id = %s AND deleted_at IS NULL", (document_id, user["id"])
    )
    if row is None:
        raise Problem(404, "not_found", "không có tài liệu này")
    active = db.scalar(
        "SELECT count(*) FROM jobs WHERE document_id = %s AND status IN ('queued','running','awaiting_glossary')",
        (document_id,),
    )
    if active:
        raise Problem(409, "document_in_use", "tài liệu đang được job chưa xong dùng")
    with db.tx() as cur:
        cur.execute("DELETE FROM doc_sections WHERE document_id = %s", (document_id,))
        cur.execute("DELETE FROM doc_paragraphs WHERE document_id = %s", (document_id,))
        cur.execute("UPDATE documents SET deleted_at = now(), status = 'deleted' WHERE id = %s", (document_id,))
    return Response(status_code=204)
