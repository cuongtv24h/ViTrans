"""Job: tạo (trừ tín dụng nguyên tử), trạng thái, danh sách, huỷ, luồng SSE (SPEC §11, §13)."""

from __future__ import annotations

import json
import time
from decimal import Decimal

import psycopg
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from visynth.estimate import Price, estimate
from visynth_api.db import Database
from visynth_api.errors import Problem, sql_problem
from visynth_api.models import JobCreate, JobGlossaryConfirm
from visynth_api.security import current_user

router = APIRouter(tags=["jobs"])

JOB_COLUMNS = (
    "id, document_id, level, source_lang, target_lang, status, current_stage, progress, est_credits, charged_credits, "
    "refunded_credits, est_cost_usd, actual_cost_usd, actual_shadow_usd, max_cost_usd, privacy_class, quality_grade, "
    "style_core_version_id, glossary_releases, cancel_requested, error_code, error_message, created_at, started_at, "
    "finished_at, expires_at, glossary_review_deadline, glossary_auto_confirmed, options"
)
STAGES = (
    "extract",
    "profile",
    "segment",
    "glossary",
    "map",
    "consolidate",
    "write",
    "verify",
    "repair",
    "translate",
    "assemble",
)
TERMINAL = ("succeeded", "failed", "canceled", "expired")
#: Giai đoạn worker thực sự chạy (4 giai đoạn còn lại được đánh `skipped` ở M1).
PIPELINE_STAGES = ("profile", "glossary", "map", "consolidate", "write", "verify", "repair")
#: Mức dịch đầy đủ đi đường P0 → glossary → P9 (§6.10) nên `translate` chạy thật, phần tổng hợp thì bỏ.
TRANSLATE_STAGES = ("profile", "glossary", "translate")


def pipeline_stages(level: str) -> tuple[str, ...]:
    """Giai đoạn worker sẽ chạy với mức này (dùng để đánh `pending`/`skipped` cho `job_stages`)."""
    return TRANSLATE_STAGES if level == "full_translation" else PIPELINE_STAGES


PRICE_SQL = (
    "SELECT model, effective_from, input_per_mtok, output_per_mtok, cached_input_per_mtok FROM llm_prices "
    "WHERE effective_from <= current_date ORDER BY effective_from DESC, model LIMIT 1"
)


def _db(request: Request) -> Database:
    return request.app.state.db


#: Cột `numeric` trong PostgreSQL trả `Decimal`; hợp đồng OpenAPI khai `number` nên đổi sang float khi trả.
_MONEY_FIELDS = ("est_cost_usd", "actual_cost_usd", "actual_shadow_usd", "max_cost_usd", "progress")


def shape_job(job: dict) -> dict:
    """Chuẩn hoá hàng `jobs` cho JSON: `Decimal` → `float` (giữ đúng kiểu của hợp đồng)."""
    shaped = dict(job)
    for field in _MONEY_FIELDS:
        value = shaped.get(field)
        if isinstance(value, Decimal):
            shaped[field] = float(value)
    return shaped


def _job_or_404(db: Database, job_id: str, user: dict) -> dict:
    job = db.one(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s AND user_id = %s", (job_id, user["id"]))
    if job is None:
        raise Problem(404, "not_found", "không có job này")
    return job


def _price(db: Database) -> Price:
    """Giá tham chiếu đang hiệu lực (bảng `llm_prices` do Alembic seed)."""
    row = db.one(PRICE_SQL)
    if row is None:
        raise Problem(500, "price_table_empty", "bảng `llm_prices` trống — chạy migration trước")
    return Price(
        model=row["model"],
        effective_from=row["effective_from"],
        input_per_mtok=float(row["input_per_mtok"]),
        output_per_mtok=float(row["output_per_mtok"]),
        cached_input_per_mtok=float(row["cached_input_per_mtok"] or 0),
    )


def _quote(db: Database, words: int, level: str, lang: str) -> dict:
    price = _price(db)
    est = estimate(words, level, price, lang=lang)
    return {
        "credits": est.credits,
        "cost_usd": round(est.cost_usd, 4),
        "tokens_in": est.tokens_in,
        "tokens_out": est.tokens_out,
        "minutes_low": est.minutes_low,
        "minutes_high": est.minutes_high,
        "price_model": price.model,
    }


@router.post("/jobs/estimate")
def estimate_job(body: JobCreate, request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    document = db.one(
        "SELECT word_count, language_code FROM documents WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
        (body.document_id, user["id"]),
    )
    if document is None:
        raise Problem(404, "not_found", "không có tài liệu này")
    if not document["word_count"]:
        raise Problem(409, "document_not_ready", "tài liệu chưa bóc tách xong")
    return _quote(db, document["word_count"], body.level, document["language_code"] or "vi")


@router.post("/jobs", status_code=202)
def create_job(
    body: JobCreate,
    request: Request,
    response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=80),
    user: dict = Depends(current_user),
) -> dict:
    """Tạo job: kiểm tra trần chi tiêu, chốt giá, trừ tín dụng và xếp hàng trong MỘT giao dịch."""
    db: Database = _db(request)
    try:
        with db.tx() as cur:
            cur.execute(
                "SELECT id FROM jobs WHERE user_id = %s AND idempotency_key = %s",
                (user["id"], idempotency_key),
            )
            replay = cur.fetchone()
            if replay:
                response.status_code = 200
                cur.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s", (replay["id"],))
                job = cur.fetchone()
                cur.execute("SELECT credit_balance(%s) AS balance", (user["id"],))
                balance = cur.fetchone()["balance"]
                return {"job": shape_job(job), "balance": balance, "idempotent_replay": True}

            cur.execute(
                "SELECT paused FROM spend_daily WHERE day = current_date",
            )
            spend = cur.fetchone()
            if spend and spend["paused"]:
                raise Problem(503, "spend_cap_reached", "hệ thống đang tạm dừng nhận job mới để giữ trần chi tiêu ngày")

            # Mức có được bật không (AC-26: bỏ/thêm trong `app_settings.enabled_levels`, không cần triển khai lại).
            cur.execute(
                "SELECT key, value FROM app_settings WHERE key IN ('enabled_levels', 'full_translation_daily_limit')"
            )
            settings_map = {row["key"]: row["value"] for row in cur.fetchall()}
            levels = settings_map.get("enabled_levels")
            if isinstance(levels, str):  # phòng khi driver trả jsonb dạng chuỗi
                levels = json.loads(levels)
            if isinstance(levels, list) and body.level not in levels:
                raise Problem(422, "level_disabled", f"mức {body.level} đang tắt")
            if body.level == "full_translation":
                limit = settings_map.get("full_translation_daily_limit")
                cur.execute(
                    "SELECT count(*) AS used FROM jobs WHERE user_id = %s AND level = 'full_translation' "
                    "AND created_at >= date_trunc('day', now()) AND status NOT IN ('canceled', 'expired')",
                    (user["id"],),
                )
                used = cur.fetchone()["used"]
                if limit is not None and int(used or 0) >= int(limit):
                    raise Problem(422, "full_translation_daily_limit", "đã dùng hết hạn mức dịch đầy đủ hôm nay")

            cur.execute(
                "SELECT word_count, language_code, status FROM documents WHERE id = %s AND user_id = %s "
                "AND deleted_at IS NULL FOR UPDATE",
                (body.document_id, user["id"]),
            )
            document = cur.fetchone()
            if document is None:
                raise Problem(404, "not_found", "không có tài liệu này")
            if document["status"] != "ready" or not document["word_count"]:
                raise Problem(409, "document_not_ready", f"tài liệu đang ở trạng thái {document['status']}")

            quote = _quote(db, document["word_count"], body.level, document["language_code"] or "vi")

            # Lõi văn phong: chỉ nhận bản ĐÃ DUYỆT (§19.6); `style_core_id` là cách gọi theo hợp đồng.
            style_version_id = body.style_core_version_id
            if body.style_core_id and not style_version_id:
                cur.execute(
                    "SELECT id FROM style_core_versions WHERE style_core_id = %s AND status = 'approved' "
                    "ORDER BY approved_at DESC NULLS LAST LIMIT 1",
                    (body.style_core_id,),
                )
                approved = cur.fetchone()
                if approved is None:
                    raise Problem(409, "style_core_not_approved", "Lõi văn phong chưa có phiên bản nào được duyệt")
                style_version_id = approved["id"]

            options = {
                **body.options,
                "skip_glossary_review": body.skip_glossary_review,
                "custom_instructions": body.custom_instructions,
                "notify_by_email": body.notify_by_email,
                "glossary_ids": list(body.glossary_ids),
            }
            if body.recipe_id:
                options["recipe_id"] = body.recipe_id
            cur.execute(
                f"""INSERT INTO jobs (user_id, document_id, level, source_lang, target_lang, options, status,
                        est_credits, est_cost_usd, max_cost_usd, privacy_class, style_core_version_id,
                        model_profile, prompt_versions, idempotency_key)
                    VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {JOB_COLUMNS}""",
                (
                    user["id"],
                    body.document_id,
                    body.level,
                    document["language_code"],
                    body.target_lang,
                    psycopg.types.json.Jsonb(options),
                    quote["credits"],
                    quote["cost_usd"],
                    body.max_cost_usd,
                    body.privacy_class,
                    style_version_id,
                    psycopg.types.json.Jsonb({"pool_version": None, "profiles": {}}),
                    psycopg.types.json.Jsonb({}),
                    idempotency_key,
                ),
            )
            job = cur.fetchone()
            for stage in STAGES:
                cur.execute(
                    "INSERT INTO job_stages (job_id, stage, status) VALUES (%s, %s, %s)",
                    (job["id"], stage, "pending" if stage in pipeline_stages(body.level) else "skipped"),
                )
            cur.execute(
                "SELECT charge_credits(%s, %s, %s, %s) AS balance",
                (user["id"], quote["credits"], job["id"], f"job:{job['id']}"),
            )
            balance = cur.fetchone()["balance"]
            # đọc lại sau khi trừ tín dụng: `charged_credits` vừa được hàm SQL cập nhật
            cur.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s", (job["id"],))
            job = cur.fetchone()
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    return {"job": shape_job(job), "balance": balance, "quote": quote, "idempotent_replay": False}


@router.get("/jobs")
def list_jobs(
    request: Request,
    limit: int = 20,
    status: str | None = None,
    cursor: str | None = None,
    user: dict = Depends(current_user),
) -> dict:
    db = _db(request)
    limit = max(1, min(limit, 100))
    rows = db.all(
        f"""SELECT {JOB_COLUMNS} FROM jobs
             WHERE user_id = %s
               AND (%s::text IS NULL OR status = %s::text)
               AND (%s::timestamptz IS NULL OR created_at < %s::timestamptz)
             ORDER BY created_at DESC LIMIT %s""",
        (user["id"], status, status, cursor, cursor, limit + 1),
    )
    items = [shape_job(row) for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["created_at"].isoformat() if len(rows) > limit else None}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    job = _job_or_404(db, job_id, user)
    return {
        "job": shape_job(job),
        "document": db.one("SELECT id, title, word_count FROM documents WHERE id = %s", (job["document_id"],)),
        "stages": db.all(
            "SELECT stage, status, attempt, started_at, finished_at, metrics, error FROM job_stages WHERE job_id = %s ORDER BY stage",
            (job_id,),
        ),
        "report_id": db.scalar("SELECT id FROM reports WHERE job_id = %s AND deleted_at IS NULL", (job_id,)),
    }


@router.post("/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """Yêu cầu huỷ. Job đang chạy dừng ở ranh giới giai đoạn; job chưa chạy huỷ ngay và hoàn đủ tín dụng."""
    db: Database = _db(request)
    try:
        with db.tx() as cur:
            cur.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s AND user_id = %s FOR UPDATE",
                (job_id, user["id"]),
            )
            job = cur.fetchone()
            if job is None:
                raise Problem(404, "not_found", "không có job này")
            if job["status"] in TERMINAL:
                raise Problem(409, "already_finished", f"job đã ở trạng thái {job['status']}")
            refunded = 0
            if job["status"] == "queued":
                cur.execute(
                    "UPDATE jobs SET status = 'canceled', cancel_requested = true, finished_at = now() WHERE id = %s",
                    (job_id,),
                )
                cur.execute(
                    "SELECT refund_credits(%s, %s, %s, %s) AS refunded",
                    (user["id"], job["charged_credits"], job_id, f"cancel:{job_id}"),
                )
                refunded = cur.fetchone()["refunded"]
            else:
                cur.execute("UPDATE jobs SET cancel_requested = true WHERE id = %s", (job_id,))
            cur.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()
            cancel_payload = {
                "type": "job_canceled",
                "job_id": str(job_id),
                "data": {"refunded_credits": refunded, "requested": job["status"] != "canceled"},
            }
            cur.execute(
                "INSERT INTO job_events (job_id, type, payload) VALUES (%s, 'job_canceled', %s)",
                (job_id, psycopg.types.json.Jsonb(cancel_payload)),
            )
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    return {"job": shape_job(job), "refunded_credits": refunded}


# ------------------------------------------------------------------ cổng duyệt glossary (SPEC §6.4)

JOB_GLOSSARY_COLUMNS = (
    "id, source_term, target_term, keep_original, case_sensitive, forbidden_variants, term_type, note, "
    "origin, status, confidence, priority"
)


@router.get("/jobs/{job_id}/glossary")
def get_job_glossary(job_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """Glossary của job để người dùng duyệt ở trạng thái `awaiting_glossary` (gợi ý P1 xếp độ tin cậy tăng dần)."""
    db = _db(request)
    job = _job_or_404(db, job_id, user)
    entries = db.all(
        f"""SELECT {JOB_GLOSSARY_COLUMNS} FROM job_glossary_entries WHERE job_id = %s
             ORDER BY (status = 'suggested') DESC, confidence ASC NULLS LAST, lower(source_term)""",
        (job_id,),
    )
    return {
        "entries": entries,
        "review_deadline": job["glossary_review_deadline"],
        "auto_confirmed": job["glossary_auto_confirmed"],
        "job": shape_job(job),
    }


@router.post("/jobs/{job_id}/glossary/confirm", status_code=202)
def confirm_job_glossary(
    job_id: str,
    body: JobGlossaryConfirm,
    request: Request,
    user: dict = Depends(current_user),
) -> dict:
    """Ghi quyết định của người dùng rồi cho job chạy tiếp (`awaiting_glossary` → `queued`)."""
    db: Database = _db(request)
    try:
        with db.tx() as cur:
            cur.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s AND user_id = %s FOR UPDATE", (job_id, user["id"])
            )
            job = cur.fetchone()
            if job is None:
                raise Problem(404, "not_found", "không có job này")
            if job["status"] not in ("awaiting_glossary", "queued", "running"):
                raise Problem(409, "glossary_not_ready", f"job đang ở trạng thái {job['status']}")
            for entry in body.entries:
                origin = entry.origin or ("user_edit" if entry.status != "suggested" else "suggested")
                cur.execute(
                    """INSERT INTO job_glossary_entries (job_id, source_term, target_term, keep_original,
                            case_sensitive, forbidden_variants, term_type, note, origin, status, confidence)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (job_id, lower(source_term)) DO UPDATE SET
                            target_term = EXCLUDED.target_term,
                            keep_original = EXCLUDED.keep_original,
                            case_sensitive = EXCLUDED.case_sensitive,
                            forbidden_variants = EXCLUDED.forbidden_variants,
                            term_type = EXCLUDED.term_type,
                            note = EXCLUDED.note,
                            origin = EXCLUDED.origin,
                            status = EXCLUDED.status,
                            confidence = EXCLUDED.confidence""",
                    (
                        job_id,
                        entry.source_term,
                        entry.target_term,
                        entry.keep_original,
                        entry.case_sensitive,
                        list(entry.forbidden_variants),
                        entry.term_type,
                        entry.note,
                        origin,
                        entry.status,
                        entry.confidence,
                    ),
                )
            # Gợi ý không được nhắc tới trong yêu cầu: coi như bị loại (người dùng đã chốt danh sách).
            cur.execute(
                "UPDATE job_glossary_entries SET status = 'rejected' WHERE job_id = %s AND status = 'suggested'",
                (job_id,),
            )
            if body.save_to_glossary_id:
                cur.execute("SELECT kind, owner_id FROM glossaries WHERE id = %s", (body.save_to_glossary_id,))
                glossary = cur.fetchone()
                if glossary is None or glossary["owner_id"] != user["id"] or glossary["kind"] != "personal":
                    raise Problem(404, "not_found", "không có glossary cá nhân này")
                for entry in body.entries:
                    if entry.status != "confirmed":
                        continue
                    cur.execute(
                        """INSERT INTO glossary_entries (glossary_id, source_term, target_term, keep_original,
                                case_sensitive, forbidden_variants, term_type, note, origin, status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'user_edit', 'confirmed')
                           ON CONFLICT (glossary_id, lower(source_term)) DO UPDATE SET
                                target_term = EXCLUDED.target_term, status = 'confirmed', updated_at = now()""",
                        (
                            body.save_to_glossary_id,
                            entry.source_term,
                            entry.target_term,
                            entry.keep_original,
                            entry.case_sensitive,
                            list(entry.forbidden_variants),
                            entry.term_type,
                            entry.note,
                        ),
                    )
            if job["status"] == "awaiting_glossary":
                # Chạy tiếp từ giai đoạn `glossary`: dùng danh sách đã duyệt (không gọi lại P1) rồi sang `map`.
                cur.execute(
                    "UPDATE jobs SET status = 'queued', glossary_review_deadline = NULL, "
                    "options = options || '{\"glossary_confirmed\": true}'::jsonb WHERE id = %s",
                    (job_id,),
                )
                cur.execute("DELETE FROM job_tasks WHERE job_id = %s AND stage <> 'glossary'", (job_id,))
                cur.execute("DELETE FROM job_checkpoints WHERE job_id = %s AND stage <> 'profile'", (job_id,))
                cur.execute(
                    "UPDATE job_stages SET status = 'pending', attempt = 0, started_at = NULL, finished_at = NULL, "
                    "metrics = '{}'::jsonb, error = NULL WHERE job_id = %s AND status <> 'skipped' AND stage <> 'profile'",
                    (job_id,),
                )
                cur.execute(
                    """INSERT INTO job_tasks (job_id, stage, task_key, max_attempts)
                       VALUES (%s, 'glossary', 'glossary:confirmed', 3)
                       ON CONFLICT (job_id, stage, task_key) DO NOTHING""",
                    (job_id,),
                )
                confirmed = sum(1 for e in body.entries if e.status == "confirmed")
                cur.execute(
                    "INSERT INTO job_events (job_id, type, payload) VALUES (%s, 'glossary_confirmed', %s)",
                    (
                        job_id,
                        psycopg.types.json.Jsonb(
                            {
                                "type": "glossary_confirmed",
                                "job_id": str(job_id),
                                "data": {"confirmed": confirmed, "rejected": max(0, len(body.entries) - confirmed)},
                            }
                        ),
                    ),
                )
            cur.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    return {"job": shape_job(job), "entries": len(body.entries)}


# ------------------------------------------------------------------ SSE


def _event_payload(row: dict, job_id: str) -> dict:
    payload = row["payload"] or {}
    event = {
        "id": row["id"],
        "type": payload.get("type", row["type"]),
        "job_id": str(job_id),
        "ts": row["created_at"].isoformat(),
    }
    if payload.get("stage"):
        event["stage"] = payload["stage"]
    if payload.get("message_vi"):
        event["message_vi"] = payload["message_vi"][:300]
    if isinstance(payload.get("progress"), dict):
        event["progress"] = payload["progress"]
    data = payload.get("data")
    event["data"] = data if isinstance(data, dict) else {}
    return event


@router.get("/jobs/{job_id}/events")
def stream_events(
    job_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    since: int | None = Query(default=None, ge=0),
    user: dict = Depends(current_user),
) -> StreamingResponse:
    """SSE: phát lại sự kiện sau `Last-Event-ID` rồi theo dõi tới khi job kết thúc (SPEC §13.3)."""
    db: Database = _db(request)
    job = _job_or_404(db, job_id, user)
    cursor = max(int(last_event_id or 0), int(since or 0))
    job_key = str(job["id"])

    def generate():
        nonlocal cursor
        idle_s = 0.0
        while True:
            rows = db.all(
                "SELECT id, type, payload, created_at FROM job_events WHERE job_id = %s AND id > %s ORDER BY id LIMIT 200",
                (job_key, cursor),
            )
            for row in rows:
                cursor = row["id"]
                event = _event_payload(row, job_key)
                yield f"id: {row['id']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            status = db.scalar("SELECT status FROM jobs WHERE id = %s", (job_key,))
            if status in TERMINAL:
                leftover = db.scalar("SELECT count(*) FROM job_events WHERE job_id = %s AND id > %s", (job_key, cursor))
                if not leftover:
                    return
            if not rows:
                idle_s += 0.5
                if idle_s >= 15:
                    idle_s = 0.0
                    yield ": ping\n\n"
            time.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
