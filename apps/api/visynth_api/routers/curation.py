"""API quản trị cho Lõi văn phong và glossary chuẩn (SPEC §19.3–§19.6, §19.9).

Hai nguyên tắc của §19 được mã hoá ở đây:

* **Máy đề xuất, người duyệt**: nội dung do AI sinh ra luôn vào ở trạng thái `draft`/`in_review` với
  `origin = ai_proposal`; duyệt là hành động của người có vai trò `curator`/`admin` và bị CSDL chặn
  nếu còn quyết định mở (`open_decisions`) hoặc còn mục AI chưa ai xem.
* **Bản đã duyệt là bất biến**: sửa/xoá phiên bản `approved` do trigger `style_core_version_guard`
  từ chối; muốn đổi phải tạo phiên bản mới (`copy-on-write`).

Việc CHẠY các tiến trình tốn token (P12/P13) không xảy ra trong tiến trình API: API chỉ tạo
`curation_runs` ở trạng thái `queued` rồi để worker (`visynth curate`) nhặt và chạy — nhờ vậy API
không giữ khoá nhà cung cấp và không bị treo theo thời gian chạy của LLM.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query, Request

from visynth.stylecore.core import content_sha256, decisions_open, lint
from visynth_api.db import Database
from visynth_api.errors import Problem, sql_problem
from visynth_api.models import (
    DecisionAnswerIn,
    GlossaryBootstrapIn,
    GlossaryDecisionIn,
    GlossaryReleaseIn,
    StyleCoreCreateIn,
    StyleCoreEditIn,
    StyleCoreVersionCreateIn,
    StyleProposeIn,
    TestDriveIn,
)
from visynth_api.security import require_role

router = APIRouter(prefix="/admin", tags=["curation"])

curator = require_role("curator")

VERSION_COLUMNS = (
    "id, style_core_id, version, status, origin, content, content_sha256, open_decisions, "
    "created_by, created_at, submitted_at, approved_by, approved_at, note, proposal_run_id"
)


def _db(request: Request) -> Database:
    return request.app.state.db


def _audit(db: Database, actor: dict, action: str, entity: str, entity_id: str | None, **meta: Any) -> None:
    db.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, meta) VALUES (%s, %s, %s, %s, %s::jsonb)",
        (actor["id"], action, entity, entity_id, psycopg.types.json.Jsonb(meta)),
    )


def _lint_payload(content: dict) -> list[dict]:
    return [
        {"severity": problem.severity, "path": problem.path, "message": problem.message} for problem in lint(content)
    ]


def _pending_ai_decisions(content: dict) -> list[dict]:
    """Mục do AI sinh chưa được người xem (`origin = ai`, `reviewed` khác true) — SPEC §19.6."""
    pending: list[dict] = []
    for section in ("rules", "exemplars"):  # hai mục duy nhất mang `origin`/`reviewed` trong schema
        for index, item in enumerate(content.get(section) or []):
            if isinstance(item, dict) and item.get("origin") == "ai" and item.get("reviewed") is not True:
                pending.append({"field": f"{section}[{index}]", "id": item.get("id")})
    return pending


def _version_payload(db: Database, version_id: str) -> dict | None:
    row = db.one(f"SELECT {VERSION_COLUMNS} FROM style_core_versions WHERE id = %s", (version_id,))
    if row is None:
        return None
    pending = _pending_ai_decisions(row["content"] or {})
    row["problems"] = _lint_payload(row["content"] or {})
    row["pending_ai_reviews"] = pending
    return row


# --------------------------------------------------------------------------- Lõi văn phong


@router.get("/style-cores")
def list_style_cores(request: Request, curator_user: dict = Depends(curator)) -> list[dict]:
    db = _db(request)
    return db.all(
        """SELECT c.id, c.slug, c.name, c.scope, c.domain, c.locale, c.parent_id,
                  (SELECT count(*) FROM style_core_versions v WHERE v.style_core_id = c.id) AS versions,
                  (SELECT v.status FROM style_core_versions v WHERE v.style_core_id = c.id
                    ORDER BY v.created_at DESC LIMIT 1) AS latest_status
             FROM style_cores c ORDER BY c.scope, c.name"""
    )


@router.post("/style-cores", status_code=201)
def create_style_core(body: StyleCoreCreateIn, request: Request, curator_user: dict = Depends(curator)) -> dict:
    """Tạo lõi + phiên bản `0.1.0` đầu tiên (nội dung có thể là lõi trung tính để sửa dần)."""
    db = _db(request)
    content = body.content or {}
    if content:
        errors = [problem for problem in lint(content) if problem.severity == "error"]
        if errors:
            raise Problem(422, "validation_error", f"nội dung lõi có {len(errors)} lỗi: {errors[0].message}")
    try:
        with db.tx() as cur:
            cur.execute(
                """INSERT INTO style_cores (slug, scope, owner_id, parent_id, name, domain, locale)
                   VALUES (%s, 'system', NULL, %s, %s, %s, %s) RETURNING id, slug, name, scope, domain, locale""",
                (body.slug, body.parent_id, body.name, body.domain, body.locale),
            )
            core = cur.fetchone()
            cur.execute(
                """INSERT INTO style_core_versions (style_core_id, version, status, origin, content, content_sha256, created_by)
                   VALUES (%s, '0.1.0', 'draft', %s, %s, %s, %s)
                   RETURNING id""",
                (
                    core["id"],
                    body.origin,
                    psycopg.types.json.Jsonb(content),
                    content_sha256(content),
                    curator_user["id"],
                ),
            )
            version_id = cur.fetchone()["id"]
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    _audit(db, curator_user, "style_core_create", "style_cores", str(core["id"]), slug=body.slug)
    return {"style_core": core, "version": _version_payload(db, str(version_id))}


@router.get("/style-cores/{style_core_id}/versions")
def list_versions(style_core_id: str, request: Request, curator_user: dict = Depends(curator)) -> list[dict]:
    db = _db(request)
    return db.all(
        f"SELECT {VERSION_COLUMNS} FROM style_core_versions WHERE style_core_id = %s ORDER BY created_at DESC",
        (style_core_id,),
    )


@router.post("/style-cores/{style_core_id}/versions", status_code=201)
def create_version(
    style_core_id: str,
    body: StyleCoreVersionCreateIn,
    request: Request,
    curator_user: dict = Depends(curator),
) -> dict:
    """Tạo phiên bản mới từ một bản có sẵn (copy-on-write; bản `approved` không sửa được)."""
    db = _db(request)
    base = db.one(
        "SELECT id, version, content, style_core_id FROM style_core_versions WHERE id = %s", (body.base_version_id,)
    )
    if base is None or str(base["style_core_id"]) != style_core_id:
        raise Problem(404, "not_found", "không có phiên bản gốc này trong lõi đã nêu")
    major, minor, patch = (int(part) for part in base["version"].split("."))
    bump = {"patch": (major, minor, patch + 1), "minor": (major, minor + 1, 0), "major": (major + 1, 0, 0)}[body.bump]
    new_version = f"{bump[0]}.{bump[1]}.{bump[2]}"
    content = base["content"] or {}
    try:
        with db.tx() as cur:
            cur.execute(
                """INSERT INTO style_core_versions (style_core_id, version, status, origin, content, content_sha256, created_by)
                   VALUES (%s, %s, 'draft', %s, %s, %s, %s) RETURNING id""",
                (
                    style_core_id,
                    new_version,
                    body.origin,
                    psycopg.types.json.Jsonb(content),
                    content_sha256(content),
                    curator_user["id"],
                ),
            )
            version_id = cur.fetchone()["id"]
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    _audit(db, curator_user, "style_core_version_create", "style_core_versions", str(version_id), version=new_version)
    return _version_payload(db, str(version_id))


@router.get("/style-cores/{style_core_id}/versions/{version_id}")
def get_version(style_core_id: str, version_id: str, request: Request, curator_user: dict = Depends(curator)) -> dict:
    payload = _version_payload(_db(request), version_id)
    if payload is None or str(payload["style_core_id"]) != style_core_id:
        raise Problem(404, "not_found", "không có phiên bản này")
    return payload


@router.patch("/style-cores/{style_core_id}/versions/{version_id}")
def edit_version(
    style_core_id: str,
    version_id: str,
    body: StyleCoreEditIn,
    request: Request,
    curator_user: dict = Depends(curator),
) -> dict:
    """Sửa nội dung bản nháp. Bản `approved` bị CSDL từ chối (`version_immutable`)."""
    db = _db(request)
    row = db.one(
        "SELECT status, content FROM style_core_versions WHERE id = %s AND style_core_id = %s",
        (version_id, style_core_id),
    )
    if row is None:
        raise Problem(404, "not_found", "không có phiên bản này")
    if row["status"] == "approved":
        raise Problem(409, "version_immutable", "phiên bản đã duyệt là bất biến — tạo phiên bản mới rồi sửa")
    if row["status"] not in ("draft", "rejected", "in_review"):
        raise Problem(409, "draft_not_editable", f"không sửa được phiên bản ở trạng thái {row['status']}")
    errors = [problem for problem in lint(body.content) if problem.severity == "error"]
    sha = content_sha256(body.content)
    try:
        with db.tx() as cur:
            cur.execute(
                "UPDATE style_core_versions SET content = %s, content_sha256 = %s WHERE id = %s",
                (psycopg.types.json.Jsonb(body.content), sha, version_id),
            )
            cur.execute(
                """INSERT INTO style_core_reviews (version_id, reviewer_id, action, before, after, comment)
                   VALUES (%s, %s, 'edit', %s, %s, %s)""",
                (
                    version_id,
                    curator_user["id"],
                    psycopg.types.json.Jsonb(row["content"] or {}),
                    psycopg.types.json.Jsonb(body.content),
                    body.comment,
                ),
            )
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    payload = _version_payload(db, version_id)
    payload["lint_errors"] = [problem.message for problem in errors]
    return payload


@router.post("/style-cores/{style_core_id}/versions/{version_id}/submit")
def submit_version(
    style_core_id: str, version_id: str, request: Request, curator_user: dict = Depends(curator)
) -> dict:
    db = _db(request)
    return _transition(db, style_core_id, version_id, to="in_review", action="submit", actor=curator_user)


@router.post("/style-cores/{style_core_id}/versions/{version_id}/answer-decision")
def answer_decision(
    style_core_id: str,
    version_id: str,
    body: DecisionAnswerIn,
    request: Request,
    curator_user: dict = Depends(curator),
) -> dict:
    """Trả lời một quyết định mở của P12; duyệt chỉ được khi không còn quyết định nào mở."""
    db = _db(request)
    row = db.one(
        "SELECT open_decisions FROM style_core_versions WHERE id = %s AND style_core_id = %s",
        (version_id, style_core_id),
    )
    if row is None:
        raise Problem(404, "not_found", "không có phiên bản này")
    decisions = [dict(item) for item in (row["open_decisions"] or [])]
    answers = {str(item.get("id")): str(item.get("answer")) for item in decisions if item.get("answer")}
    if body.decision_id not in {str(item.get("id")) for item in decisions}:
        raise Problem(404, "not_found", f"không có quyết định {body.decision_id}")
    answers[body.decision_id] = body.answer
    for decision in decisions:
        if str(decision.get("id")) == body.decision_id:
            decision["answer"] = body.answer
            decision["answered_by"] = str(curator_user["id"])
    # Cột `open_decisions` CHỈ giữ các quyết định CHƯA trả lời — nhờ vậy điều kiện duyệt chỉ là
    # "không còn phần tử nào", và vết trả lời nằm ở `style_core_reviews`.
    remaining = decisions_open([dict(item, id=str(item.get("id"))) for item in decisions], answers)
    try:
        with db.tx() as cur:
            cur.execute(
                "UPDATE style_core_versions SET open_decisions = %s WHERE id = %s",
                (psycopg.types.json.Jsonb(remaining), version_id),
            )
            cur.execute(
                """INSERT INTO style_core_reviews (version_id, reviewer_id, action, field_path, after)
                   VALUES (%s, %s, 'answer_decision', %s, %s)""",
                (version_id, curator_user["id"], body.decision_id, psycopg.types.json.Jsonb({"answer": body.answer})),
            )
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    payload = _version_payload(db, version_id)
    payload["open_decisions"] = remaining
    return payload


@router.post("/style-cores/{style_core_id}/versions/{version_id}/approve")
def approve_version(
    style_core_id: str, version_id: str, request: Request, curator_user: dict = Depends(curator)
) -> dict:
    """Duyệt bản nháp. Bị CHẶN khi còn quyết định mở hoặc còn mục AI chưa được người xem (§19.6)."""
    db = _db(request)
    row = db.one(
        f"SELECT {VERSION_COLUMNS} FROM style_core_versions WHERE id = %s AND style_core_id = %s",
        (version_id, style_core_id),
    )
    if row is None:
        raise Problem(404, "not_found", "không có phiên bản này")
    if row["status"] == "approved":
        return _version_payload(db, version_id)
    blocked: list[str] = []
    if row["open_decisions"]:
        blocked.append(f"{len(row['open_decisions'])} quyết định mở chưa trả lời")
    pending = _pending_ai_decisions(row["content"] or {})
    if pending:
        blocked.append(f"{len(pending)} mục do AI đề xuất chưa được người xem (`reviewed = true`)")
    errors = [problem for problem in lint(row["content"] or {}) if problem.severity == "error"]
    if errors:
        blocked.append(f"{len(errors)} lỗi lint: {errors[0].message}")
    if blocked:
        raise Problem(409, "approval_blocked", "; ".join(blocked))
    return _transition(db, style_core_id, version_id, to="approved", action="approve", actor=curator_user)


@router.post("/style-cores/{style_core_id}/versions/{version_id}/reject")
def reject_version(
    style_core_id: str,
    version_id: str,
    body: dict[str, Any],
    request: Request,
    curator_user: dict = Depends(curator),
) -> dict:
    comment = str(body.get("comment") or "").strip()
    if not comment:
        raise Problem(422, "validation_error", "cần nêu lý do từ chối")
    db = _db(request)
    payload = _transition(
        db, style_core_id, version_id, to="rejected", action="reject", actor=curator_user, comment=comment
    )
    db.execute("UPDATE style_core_versions SET note = %s WHERE id = %s", (comment[:1000], version_id))
    return payload


def _transition(
    db: Database,
    style_core_id: str,
    version_id: str,
    *,
    to: str,
    action: str,
    actor: dict,
    comment: str | None = None,
) -> dict:
    row = db.one(
        "SELECT status FROM style_core_versions WHERE id = %s AND style_core_id = %s", (version_id, style_core_id)
    )
    if row is None:
        raise Problem(404, "not_found", "không có phiên bản này")
    if row["status"] == "approved" and to != "deprecated":
        raise Problem(409, "version_immutable", "phiên bản đã duyệt là bất biến")
    extra = ", approved_by = %s, approved_at = now()" if to == "approved" else ""
    params: list[Any] = [to]
    if to == "approved":
        params.append(actor["id"])
    if action == "submit":
        extra += ", submitted_at = now()"
    params.append(version_id)
    try:
        with db.tx() as cur:
            cur.execute(f"UPDATE style_core_versions SET status = %s{extra} WHERE id = %s", tuple(params))
            cur.execute(
                """INSERT INTO style_core_reviews (version_id, reviewer_id, action, comment)
                   VALUES (%s, %s, %s, %s)""",
                (version_id, actor["id"], action, comment),
            )
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    _audit(db, actor, f"style_core_{action}", "style_core_versions", version_id, status=to)
    return _version_payload(db, version_id)


@router.post("/style-cores/{style_core_id}/propose", status_code=202)
def propose_style_core(
    style_core_id: str, body: StyleProposeIn, request: Request, curator_user: dict = Depends(curator)
) -> dict:
    """Xếp hàng P12 (`curation_runs`); worker `visynth curate` sẽ chạy và tạo phiên bản nháp."""
    db = _db(request)
    if db.one("SELECT id FROM style_cores WHERE id = %s", (style_core_id,)) is None:
        raise Problem(404, "not_found", "không có Lõi văn phong này")
    params = {
        "style_core_id": style_core_id,
        "mode": body.mode,
        "brief_vi": body.brief_vi,
        "sample_document_ids": list(body.sample_document_ids),
        "reference_pairs": list(body.reference_pairs),
        "base_version_id": body.base_version_id,
    }
    return _enqueue_run(db, curator_user, "style_core_proposal", params)


@router.post("/style-cores/{style_core_id}/versions/{version_id}/test-drive", status_code=202)
def test_drive(
    style_core_id: str, version_id: str, body: TestDriveIn, request: Request, curator_user: dict = Depends(curator)
) -> dict:
    db = _db(request)
    if (
        db.one("SELECT id FROM style_core_versions WHERE id = %s AND style_core_id = %s", (version_id, style_core_id))
        is None
    ):
        raise Problem(404, "not_found", "không có phiên bản này")
    params = {
        "style_core_id": style_core_id,
        "version_id": version_id,
        "paragraphs": [para.model_dump() for para in body.paragraphs],
        "compare_to_version_id": body.compare_to_version_id,
    }
    return _enqueue_run(db, curator_user, "style_core_test_drive", params)


# --------------------------------------------------------------------------- curation runs


def _enqueue_run(db: Database, actor: dict, kind: str, params: dict) -> dict:
    # `default=str` để UUID/`datetime` trong tham số trở thành chuỗi thay vì làm sập JSONB.
    payload = psycopg.types.json.Jsonb(params, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))
    row = db.one(
        """INSERT INTO curation_runs (kind, requested_by, status, params)
           VALUES (%s, %s, 'queued', %s)
           RETURNING id, kind, status, params, result, cost_usd, error_code, created_at, started_at, finished_at""",
        (kind, actor["id"], payload),
    )
    _audit(db, actor, "curation_run_enqueue", "curation_runs", str(row["id"]), kind=kind)
    return row


@router.get("/curation-runs/{run_id}")
def get_run(run_id: str, request: Request, curator_user: dict = Depends(curator)) -> dict:
    row = _db(request).one(
        "SELECT id, kind, status, params, result, cost_usd, shadow_usd, error_code, error, "
        "created_at, started_at, finished_at FROM curation_runs WHERE id = %s",
        (run_id,),
    )
    if row is None:
        raise Problem(404, "not_found", "không có lượt chạy này")
    return row


# --------------------------------------------------------------------------- glossary chuẩn


@router.get("/glossary-review")
def glossary_review_queue(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    curator_user: dict = Depends(curator),
) -> dict:
    """Hàng đợi thuật ngữ cần người quyết định: `needs_human` trước, sau đó theo độ tin cậy tăng dần."""
    db = _db(request)
    # Con trỏ kiểu keyset `"<created_at ISO>|<uuid>"` — ổn định khi có mục mới xen vào giữa hai lần gọi.
    after: tuple[str, str] | None = None
    if cursor:
        created, _, entry_id = str(cursor).partition("|")
        if not created or not entry_id:
            raise Problem(400, "cursor_invalid", "con trỏ phải có dạng `<created_at>|<uuid>`")
        after = (created, entry_id)
    rows = db.all(
        """SELECT e.*, g.name AS glossary_name, g.scope FROM glossary_entries e
             JOIN glossaries g ON g.id = e.glossary_id
            WHERE e.status = 'suggested'
              AND (%s::timestamptz IS NULL OR (e.created_at, e.id) > (%s::timestamptz, %s::uuid))
            ORDER BY e.created_at ASC, e.id ASC
            LIMIT %s""",
        (after[0] if after else None, after[0] if after else None, after[1] if after else None, limit),
    )
    # Mục `needs_human` lên đầu trong CÙNG một trang (giữ tính ổn định của con trỏ), không đổi thứ tự phân trang.
    rows.sort(key=lambda row: (not row["needs_human"], row["confidence"] if row["confidence"] is not None else -1))
    next_cursor = f"{rows[-1]['created_at'].isoformat()}|{rows[-1]['id']}" if len(rows) == limit else None
    return {"items": rows, "next_cursor": next_cursor}


@router.post("/glossary-review/{entry_id}/decision")
def decide_glossary_entry(
    entry_id: str, body: GlossaryDecisionIn, request: Request, curator_user: dict = Depends(curator)
) -> dict:
    """Duyệt / sửa-rồi-duyệt / loại một mục thuật ngữ đề xuất (SPEC §19.5)."""
    db = _db(request)
    row = db.one("SELECT * FROM glossary_entries WHERE id = %s", (entry_id,))
    if row is None:
        raise Problem(404, "not_found", "không có mục thuật ngữ này")
    if body.action == "reject":
        status, target = "rejected", row["target_term"]
    else:
        status = "confirmed"
        target = body.target_term or row["target_term"]
    try:
        with db.tx() as cur:
            cur.execute(
                """UPDATE glossary_entries
                      SET status = %s, target_term = %s, keep_original = %s,
                          forbidden_variants = %s, needs_human = false, question_vi = NULL,
                          reviewed_by = %s, reviewed_at = now(), review_note = %s
                    WHERE id = %s
                    RETURNING *""",
                (
                    status,
                    target,
                    row["keep_original"] if body.keep_original is None else body.keep_original,
                    list(body.forbidden_variants) or list(row["forbidden_variants"] or []),
                    curator_user["id"],
                    body.note,
                    entry_id,
                ),
            )
            updated = cur.fetchone()
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    _audit(db, curator_user, "glossary_decision", "glossary_entries", entry_id, decision=body.action)
    return updated


@router.get("/glossaries/{glossary_id}/releases")
def list_releases(glossary_id: str, request: Request, curator_user: dict = Depends(curator)) -> list[dict]:
    return _db(request).all(
        "SELECT id, glossary_id, version, entry_count, content_sha256, released_by, released_at, note "
        "FROM glossary_releases WHERE glossary_id = %s ORDER BY version DESC",
        (glossary_id,),
    )


@router.post("/glossaries/{glossary_id}/releases", status_code=201)
def publish_release(
    glossary_id: str, body: GlossaryReleaseIn, request: Request, curator_user: dict = Depends(curator)
) -> dict:
    """Phát hành bản bất biến từ các mục `confirmed` (hàm SQL `publish_glossary_release`)."""
    db = _db(request)
    try:
        version = db.scalar("SELECT publish_glossary_release(%s, %s, %s)", (glossary_id, curator_user["id"], body.note))
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    _audit(db, curator_user, "glossary_publish", "glossaries", glossary_id, version=version)
    return db.one(
        "SELECT id, glossary_id, version, entry_count, content_sha256, released_at, note "
        "FROM glossary_releases WHERE glossary_id = %s AND version = %s",
        (glossary_id, version),
    )


@router.post("/glossaries/bootstrap", status_code=202)
def bootstrap_glossary(body: GlossaryBootstrapIn, request: Request, curator_user: dict = Depends(curator)) -> dict:
    """Xếp hàng P13: sinh ứng viên thuật ngữ cho glossary chuẩn từ tài liệu mẫu."""
    db = _db(request)
    if db.one("SELECT id FROM glossaries WHERE id = %s", (body.glossary_id,)) is None:
        raise Problem(404, "not_found", "không có glossary này")
    params = {
        "glossary_id": body.glossary_id,
        "sample_document_ids": list(body.sample_document_ids),
        "reference_pairs": list(body.reference_pairs),
        "style_core_id": body.style_core_id,
    }
    return _enqueue_run(db, curator_user, "glossary_bootstrap", params)


# --------------------------------------------------------------------------- tiện ích chung


@router.get("/curation-runs")
def list_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    status: str | None = None,
    curator_user: dict = Depends(curator),
) -> list[dict]:
    return _db(request).all(
        """SELECT id, kind, status, cost_usd, error_code, created_at, started_at, finished_at
             FROM curation_runs
            WHERE (%s::text IS NULL OR status = %s::text)
            ORDER BY created_at DESC LIMIT %s""",
        (status, status, limit),
    )


def user_visible_style_cores(db: Database, user: dict) -> list[dict]:  # pragma: no cover - dùng ở M2
    return db.all(
        """SELECT c.id, c.slug, c.name, c.domain, c.locale, c.scope,
                  (SELECT v.id FROM style_core_versions v WHERE v.style_core_id = c.id AND v.status = 'approved'
                    ORDER BY v.approved_at DESC NULLS LAST LIMIT 1) AS latest_approved_version
             FROM style_cores c WHERE c.scope = 'system' OR c.owner_id = %s ORDER BY c.name""",
        (user["id"],),
    )
