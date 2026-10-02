"""Glossary (SPEC §7, §19.5): CRUD cá nhân, mục thuật ngữ, nhập/xuất CSV.

Glossary `shared` (chuẩn của hệ thống, `owner_id IS NULL`) chỉ đọc với người dùng thường;
phiên bản phát hành bất biến nằm ở `glossary_releases`, việc phát hành thuộc API quản trị.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, Depends, File, Request, Response, UploadFile

from visynth_api.db import Database
from visynth_api.errors import Problem, sql_problem
from visynth_api.models import GlossaryCreate, GlossaryEntryIn, GlossaryPatch
from visynth_api.security import current_user

router = APIRouter()

GLOSSARY_COLUMNS = (
    "id, owner_id, scope, name, description, domain, source_lang, target_lang, version, created_at, updated_at"
)
ENTRY_COLUMNS = (
    "id, source_term, target_term, keep_original, case_sensitive, forbidden_variants, term_type, note, status, "
    "proposed_by, needs_human, question_vi, confidence, evidence, created_at, updated_at"
)
CSV_COLUMNS = (
    "source_term",
    "target_term",
    "keep_original",
    "case_sensitive",
    "forbidden_variants",
    "term_type",
    "note",
)


def _db(request: Request) -> Database:
    return request.app.state.db


def _shape(row: dict, *, user: dict) -> dict:
    row = dict(row)
    row["read_only"] = row.get("scope") == "shared" or row.get("owner_id") not in (None, user["id"])
    row.pop("owner_id", None)
    return row


def _glossary_or_404(db: Database, glossary_id: str, user: dict) -> dict:
    """Đọc được nếu là glossary `shared` hoặc của chính người dùng; ngược lại coi như không tồn tại."""
    row = db.one(
        f"SELECT {GLOSSARY_COLUMNS}, (SELECT count(*) FROM glossary_entries e WHERE e.glossary_id = g.id) AS entry_count "
        "FROM glossaries g WHERE id = %s AND (scope = 'shared' OR owner_id = %s)",
        (glossary_id, user["id"]),
    )
    if row is None:
        raise Problem(404, "not_found", "không có glossary này")
    return row


def _writable(row: dict) -> None:
    if row["scope"] == "shared":
        raise Problem(403, "read_only_glossary", "glossary chuẩn của hệ thống chỉ đọc")


@router.get("/glossaries", tags=["glossaries"])
def list_glossaries(request: Request, q: str | None = None, user: dict = Depends(current_user)) -> dict:
    """Glossary cá nhân của tôi và các glossary chuẩn (shared, chỉ đọc)."""
    db = _db(request)
    rows = db.all(
        f"""SELECT {GLOSSARY_COLUMNS}, (SELECT count(*) FROM glossary_entries e WHERE e.glossary_id = g.id) AS entry_count
              FROM glossaries g
             WHERE (owner_id = %s OR scope = 'shared') AND (%s::text IS NULL OR name ILIKE '%%' || %s::text || '%%')
             ORDER BY scope, name""",
        (user["id"], q, q),
    )
    return {"items": [_shape(row, user=user) for row in rows]}


@router.post("/glossaries", status_code=201, tags=["glossaries"])
def create_glossary(body: GlossaryCreate, request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    row = db.one(
        f"""INSERT INTO glossaries (owner_id, scope, name, description, domain, source_lang, target_lang)
            VALUES (%s, 'personal', %s, %s, %s, %s, %s) RETURNING {GLOSSARY_COLUMNS}""",
        (user["id"], body.name.strip(), body.description, body.domain, body.source_lang, body.target_lang),
    )
    return _shape(row, user=user) | {"entry_count": 0}


@router.get("/glossaries/{glossary_id}", tags=["glossaries"])
def get_glossary(glossary_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    return _shape(_glossary_or_404(db, glossary_id, user), user=user)


@router.patch("/glossaries/{glossary_id}", tags=["glossaries"])
def patch_glossary(glossary_id: str, body: GlossaryPatch, request: Request, user: dict = Depends(current_user)) -> dict:
    """Sửa tên, mô tả, lĩnh vực của glossary cá nhân."""
    db = _db(request)
    row = _glossary_or_404(db, glossary_id, user)
    _writable(row)
    updated = db.one(
        f"""UPDATE glossaries SET name = COALESCE(%s::text, name), description = COALESCE(%s::text, description),
                domain = COALESCE(%s::text, domain), updated_at = now()
            WHERE id = %s RETURNING {GLOSSARY_COLUMNS}""",
        (body.name, body.description, body.domain, glossary_id),
    )
    return _shape(updated, user=user) | {
        "entry_count": db.scalar("SELECT count(*) FROM glossary_entries WHERE glossary_id = %s", (glossary_id,))
    }


@router.delete("/glossaries/{glossary_id}", status_code=204, tags=["glossaries"])
def delete_glossary(glossary_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    """Xoá glossary cá nhân; glossary đã ghim vào job đang chạy thì bị chặn (409)."""
    db = _db(request)
    row = _glossary_or_404(db, glossary_id, user)
    _writable(row)
    try:
        with db.tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM jobs WHERE glossary_releases @> %s::jsonb "
                "AND status IN ('queued','running','awaiting_glossary')",
                (f'[{{"glossary_id": "{glossary_id}"}}]',),
            )
            if cur.fetchone()["n"]:
                raise Problem(409, "glossary_in_use", "glossary đang được job chưa xong dùng")
            cur.execute("DELETE FROM glossaries WHERE id = %s", (glossary_id,))
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    return Response(status_code=204)


# ------------------------------------------------------------------ mục thuật ngữ


@router.get("/glossaries/{glossary_id}/entries", tags=["glossaries"])
def list_entries(
    glossary_id: str,
    request: Request,
    q: str | None = None,
    status: str | None = None,
    user: dict = Depends(current_user),
) -> dict:
    db = _db(request)
    _glossary_or_404(db, glossary_id, user)
    rows = db.all(
        f"""SELECT {ENTRY_COLUMNS} FROM glossary_entries
             WHERE glossary_id = %s
               AND (%s::text IS NULL OR source_term ILIKE '%%' || %s::text || '%%' OR target_term ILIKE '%%' || %s::text || '%%')
               AND (%s::text IS NULL OR status = %s::text)
             ORDER BY needs_human DESC, lower(source_term)""",
        (glossary_id, q, q, q, status, status),
    )
    return {"items": rows}


@router.post("/glossaries/{glossary_id}/entries", status_code=201, tags=["glossaries"])
def add_entry(glossary_id: str, body: GlossaryEntryIn, request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    row = _glossary_or_404(db, glossary_id, user)
    _writable(row)
    try:
        with db.tx() as cur:
            cur.execute(
                f"""INSERT INTO glossary_entries (glossary_id, source_term, target_term, keep_original, case_sensitive,
                        forbidden_variants, term_type, note, status, proposed_by, reviewed_by, reviewed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', 'user', %s, now())
                    RETURNING {ENTRY_COLUMNS}""",
                (
                    glossary_id,
                    body.source_term.strip(),
                    body.target_term.strip(),
                    body.keep_original,
                    body.case_sensitive,
                    body.forbidden_variants,
                    body.term_type,
                    body.note,
                    user["id"],
                ),
            )
            return cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise Problem(409, "term_exists", "thuật ngữ này đã có trong glossary") from exc
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc


@router.patch("/glossaries/{glossary_id}/entries/{entry_id}", tags=["glossaries"])
def patch_entry(
    glossary_id: str, entry_id: str, body: GlossaryEntryIn, request: Request, user: dict = Depends(current_user)
) -> dict:
    db = _db(request)
    row = _glossary_or_404(db, glossary_id, user)
    _writable(row)
    updated = db.one(
        f"""UPDATE glossary_entries SET source_term = %s, target_term = %s, keep_original = %s, case_sensitive = %s,
                forbidden_variants = %s, term_type = %s, note = %s, reviewed_by = %s, reviewed_at = now(),
                needs_human = false, status = 'confirmed'
            WHERE id = %s AND glossary_id = %s RETURNING {ENTRY_COLUMNS}""",
        (
            body.source_term.strip(),
            body.target_term.strip(),
            body.keep_original,
            body.case_sensitive,
            body.forbidden_variants,
            body.term_type,
            body.note,
            user["id"],
            entry_id,
            glossary_id,
        ),
    )
    if updated is None:
        raise Problem(404, "not_found", "không có thuật ngữ này")
    return updated


@router.delete("/glossaries/{glossary_id}/entries/{entry_id}", status_code=204, tags=["glossaries"])
def delete_entry(glossary_id: str, entry_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    db = _db(request)
    row = _glossary_or_404(db, glossary_id, user)
    _writable(row)
    deleted = db.execute("DELETE FROM glossary_entries WHERE id = %s AND glossary_id = %s", (entry_id, glossary_id))
    if not deleted:
        raise Problem(404, "not_found", "không có thuật ngữ này")
    return Response(status_code=204)


# ------------------------------------------------------------------ CSV


def _ascii_slug(name: str) -> str:
    """Tên tệp an toàn: bỏ dấu tiếng Việt (header `Content-Disposition` chỉ nhận latin-1)."""
    folded = unicodedata.normalize("NFKD", name.strip().replace("đ", "d").replace("Đ", "D"))
    ascii_name = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^A-Za-z0-9-]+", "-", ascii_name).strip("-")[:60].lower()


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@router.get("/glossaries/{glossary_id}/export", tags=["glossaries"])
def export_glossary(glossary_id: str, request: Request, user: dict = Depends(current_user)) -> Response:
    """Xuất CSV UTF-8 (kèm BOM để Excel mở đúng tiếng Việt)."""
    db = _db(request)
    glossary = _glossary_or_404(db, glossary_id, user)
    rows = db.all(
        f"SELECT {', '.join(CSV_COLUMNS)} FROM glossary_entries WHERE glossary_id = %s AND status = 'confirmed' "
        "ORDER BY lower(source_term)",
        (glossary_id,),
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_cell(row[key]) for key in CSV_COLUMNS})
    filename = f"{_ascii_slug(glossary['name']) or 'glossary'}.csv"
    quoted = quote(filename, safe="")
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quoted}"},
    )


@router.post("/glossaries/{glossary_id}/import", status_code=200, tags=["glossaries"])
def import_glossary(
    glossary_id: str, request: Request, file: UploadFile = File(...), user: dict = Depends(current_user)
) -> dict:
    """Nhập CSV UTF-8: `source_term,target_term,keep_original,case_sensitive,forbidden_variants,term_type,note`.

    `forbidden_variants` phân tách bằng `|`. Mục trùng nguồn (không phân biệt hoa/thường) được cập nhật.
    """
    db = _db(request)
    row = _glossary_or_404(db, glossary_id, user)
    _writable(row)
    raw = file.file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Problem(422, "csv_not_utf8", "tệp CSV phải là UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "source_term" not in reader.fieldnames or "target_term" not in reader.fieldnames:
        raise Problem(422, "csv_missing_columns", "CSV cần có cột source_term và target_term")
    added = updated = skipped = 0
    with db.tx() as cur:
        for raw_row in reader:
            source = (raw_row.get("source_term") or "").strip()
            target = (raw_row.get("target_term") or "").strip()
            if not source or not target:
                skipped += 1
                continue
            variants = [v.strip() for v in (raw_row.get("forbidden_variants") or "").split("|") if v.strip()][:10]
            term_type = (raw_row.get("term_type") or "concept").strip() or "concept"
            if term_type not in ("concept", "proper_name", "acronym", "title", "unit", "other"):
                term_type = "other"
            params = (
                glossary_id,
                source,
                target,
                (raw_row.get("keep_original") or "").strip().lower() in ("1", "true", "yes", "x"),
                (raw_row.get("case_sensitive") or "").strip().lower() in ("1", "true", "yes", "x"),
                variants,
                term_type,
                (raw_row.get("note") or "").strip() or None,
                user["id"],
            )
            cur.execute(
                """INSERT INTO glossary_entries (glossary_id, source_term, target_term, keep_original, case_sensitive,
                        forbidden_variants, term_type, note, status, proposed_by, reviewed_by, reviewed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', 'import', %s, now())
                   ON CONFLICT (glossary_id, lower(source_term))
                   DO UPDATE SET target_term = EXCLUDED.target_term, keep_original = EXCLUDED.keep_original,
                                 case_sensitive = EXCLUDED.case_sensitive, forbidden_variants = EXCLUDED.forbidden_variants,
                                 term_type = EXCLUDED.term_type, note = EXCLUDED.note, status = 'confirmed',
                                 proposed_by = 'import', reviewed_by = EXCLUDED.reviewed_by, reviewed_at = now()
                   RETURNING (xmax = 0) AS inserted""",
                params,
            )
            if cur.fetchone()["inserted"]:
                added += 1
            else:
                updated += 1
    return {"added": added, "updated": updated, "skipped": skipped}
