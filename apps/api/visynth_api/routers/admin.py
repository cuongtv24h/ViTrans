"""Quản trị (SPEC §13, §14.5): mã mời, người dùng, mức dùng, trần chi tiêu, nhật ký kiểm toán.

Endpoint ở đây KHÔNG bao giờ trả nội dung tài liệu — chỉ số liệu vận hành.
"""

from __future__ import annotations

import secrets

import psycopg
from fastapi import APIRouter, Depends, Request, Response

from visynth_api.db import Database
from visynth_api.errors import Problem, sql_problem
from visynth_api.models import InviteCreate, SpendCapIn, UserPatch
from visynth_api.security import require_role

router = APIRouter(prefix="/admin", tags=["admin"])

INVITE_COLUMNS = "code, credits_grant, max_uses, used_count, expires_at, revoked_at, note, created_at"


def _db(request: Request) -> Database:
    return request.app.state.db


def _new_code() -> str:
    """Mã mời dạng `XXXX-XXXX` (chữ hoa + số), khớp CHECK `^[A-Z0-9-]{6,32}$`."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # bỏ ký tự dễ nhầm
    pick = lambda: "".join(secrets.choice(alphabet) for _ in range(4))  # noqa: E731
    return f"{pick()}-{pick()}"


def _audit(
    db: Database, actor: dict, action: str, *, entity: str | None = None, entity_id: str | None = None, **meta
) -> None:
    db.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, meta) VALUES (%s, %s, %s, %s, %s)",
        (actor["id"], action, entity, entity_id, psycopg.types.json.Jsonb(meta)),
    )


@router.get("/invites")
def list_invites(
    request: Request, limit: int = 100, revoked: bool | None = None, actor: dict = Depends(require_role("admin"))
) -> list[dict]:
    """Danh sách mã mời (mảng `InviteCode` theo hợp đồng)."""
    db = _db(request)
    return db.all(
        f"""SELECT {INVITE_COLUMNS}, (SELECT count(*) FROM invite_redemptions r WHERE r.code = invite_codes.code) AS redeemed
              FROM invite_codes
             WHERE (%s::boolean IS NULL OR (revoked_at IS NOT NULL) = %s::boolean)
             ORDER BY created_at DESC LIMIT %s""",
        (revoked, revoked, max(1, min(limit, 500))),
    )


@router.post("/invites", status_code=201)
def create_invites(body: InviteCreate, request: Request, actor: dict = Depends(require_role("admin"))) -> list[dict]:
    """Tạo `count` mã mời trong một giao dịch."""
    db = _db(request)
    count = body.count
    codes: list[dict] = []
    try:
        with db.tx() as cur:
            for _ in range(count):
                for _attempt in range(5):
                    candidate = _new_code()
                    cur.execute(f"SELECT {INVITE_COLUMNS} FROM invite_codes WHERE code = %s", (candidate,))
                    if cur.fetchone() is None:
                        break
                else:  # pragma: no cover - xác suất trùng cực thấp
                    raise Problem(500, "code_collision", "không sinh được mã mời mới")
                cur.execute(
                    f"""INSERT INTO invite_codes (code, created_by, credits_grant, max_uses, expires_at, note)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING {INVITE_COLUMNS}, 0 AS redeemed""",
                    (candidate, actor["id"], body.credits_grant, body.max_uses, body.expires_at, body.note),
                )
                codes.append(cur.fetchone())
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    _audit(
        db,
        actor,
        "invite.create",
        entity="invite",
        meta={"count": len(codes), "credits_grant": body.credits_grant},
    )
    return codes


@router.delete("/invites/{code}", status_code=204)
def revoke_invite(code: str, request: Request, actor: dict = Depends(require_role("admin"))) -> Response:
    db = _db(request)
    updated = db.execute(
        "UPDATE invite_codes SET revoked_at = now() WHERE code = upper(%s) AND revoked_at IS NULL", (code,)
    )
    if not updated:
        raise Problem(404, "not_found", "không có mã mời này hoặc đã bị thu hồi")
    _audit(db, actor, "invite.revoke", entity="invite", entity_id=code.upper())
    return Response(status_code=204)


@router.get("/users")
def list_users(
    request: Request, q: str | None = None, limit: int = 50, actor: dict = Depends(require_role("admin"))
) -> list[dict]:
    db = _db(request)
    rows = db.all(
        """SELECT u.id, u.email, u.display_name, u.role, u.status, u.locale, u.created_at, u.last_login_at,
                  credit_balance(u.id) AS credits,
                  (SELECT count(*) FROM jobs j WHERE j.user_id = u.id) AS jobs_total
             FROM users u
            WHERE (%s::text IS NULL OR u.email ILIKE '%%' || %s::text || '%%')
              AND u.deleted_at IS NULL
            ORDER BY u.created_at DESC LIMIT %s""",
        (q, q, max(1, min(limit, 200))),
    )
    return rows


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, request: Request, actor: dict = Depends(require_role("admin"))) -> dict:
    db = _db(request)
    if body.role is None and body.status is None:
        raise Problem(422, "nothing_to_change", "cần ít nhất một trường role/status")
    if user_id == str(actor["id"]) and body.status and body.status != "active":
        raise Problem(409, "cannot_disable_self", "không thể tự khoá tài khoản đang dùng")
    row = db.one(
        """UPDATE users SET role = COALESCE(%s::text, role), status = COALESCE(%s::text, status)
            WHERE id = %s AND deleted_at IS NULL
            RETURNING id, email, display_name, role, status, locale""",
        (body.role, body.status, user_id),
    )
    if row is None:
        raise Problem(404, "not_found", "không có người dùng này")
    _audit(db, actor, "user.patch", entity="user", entity_id=user_id, role=body.role, status=body.status)
    return row


@router.get("/usage")
def usage(request: Request, days: int = 30, actor: dict = Depends(require_role("admin"))) -> list[dict]:
    """Mức dùng theo ngày (mảng `UsageDay`). KHÔNG chứa nội dung tài liệu (SPEC §14.5)."""
    db = _db(request)
    days = max(1, min(days, 180))
    return db.all(
        """SELECT s.day::date AS day, s.cost_usd, s.cap_usd, s.paused,
                  COALESCE(j.started, 0) AS jobs_started,
                  COALESCE(j.succeeded, 0) AS jobs_succeeded,
                  COALESCE(j.failed, 0) AS jobs_failed,
                  COALESCE(j.grade_a, 0) AS grade_a,
                  COALESCE(j.grade_b, 0) AS grade_b,
                  COALESCE(j.grade_c, 0) AS grade_c
             FROM spend_daily s
             LEFT JOIN (
               SELECT created_at::date AS day,
                      count(*) AS started,
                      count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
                      count(*) FILTER (WHERE status IN ('failed','canceled')) AS failed,
                      count(*) FILTER (WHERE quality_grade = 'A') AS grade_a,
                      count(*) FILTER (WHERE quality_grade = 'B') AS grade_b,
                      count(*) FILTER (WHERE quality_grade = 'C') AS grade_c
                 FROM jobs GROUP BY 1
             ) j ON j.day = s.day
            WHERE s.day > current_date - make_interval(days => %s)
            ORDER BY s.day DESC""",
        (days,),
    )


@router.get("/spend-cap")
def get_spend_cap(request: Request, actor: dict = Depends(require_role("admin"))) -> dict:
    db = _db(request)
    cap = db.scalar("SELECT (value #>> '{}')::numeric FROM app_settings WHERE key = 'daily_spend_cap_usd'")
    paused = db.scalar("SELECT COALESCE((SELECT paused FROM spend_daily WHERE day = current_date), false)")
    spent = db.scalar("SELECT COALESCE((SELECT cost_usd FROM spend_daily WHERE day = current_date), 0)")
    return {"daily_spend_cap_usd": float(cap or 0), "paused_today": bool(paused), "today_cost_usd": float(spent or 0)}


@router.put("/spend-cap")
def set_spend_cap(body: SpendCapIn, request: Request, actor: dict = Depends(require_role("admin"))) -> dict:
    db = _db(request)
    with db.tx() as cur:
        cur.execute(
            """INSERT INTO app_settings (key, value) VALUES ('daily_spend_cap_usd', to_jsonb(%s::numeric))
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            (body.daily_spend_cap_usd,),
        )
        cur.execute(
            "UPDATE spend_daily SET paused = false WHERE day = current_date AND cost_usd < %s",
            (body.daily_spend_cap_usd,),
        )
    _audit(db, actor, "spend_cap.set", entity="setting", entity_id="daily_spend_cap_usd", usd=body.daily_spend_cap_usd)
    return {"ok": True, "daily_spend_cap_usd": body.daily_spend_cap_usd}


@router.get("/audit")
def audit_tail(request: Request, limit: int = 100, actor: dict = Depends(require_role("admin"))) -> dict:
    db = _db(request)
    rows = db.all(
        "SELECT id, user_id, action, entity, entity_id, meta, created_at FROM audit_log ORDER BY id DESC LIMIT %s",
        (max(1, min(limit, 500)),),
    )
    return {"items": rows}
