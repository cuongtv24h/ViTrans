"""Đăng ký/đăng nhập, phiên, ví tín dụng, mã mời (SPEC §11, §13, PDPL §14.3)."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, Request, Response

from visynth_api.db import Database
from visynth_api.errors import Problem, sql_problem
from visynth_api.models import ConsentsIn, LoginIn, RedeemIn, RegisterIn
from visynth_api.security import (
    clear_session_cookie,
    current_user,
    hash_password,
    issue_token,
    set_session_cookie,
    verify_password,
)

router = APIRouter(tags=["account"])

ME_COLUMNS = "id, email, display_name, role, status, locale, tos_version, created_at"


def _db(request: Request) -> Database:
    return request.app.state.db


def _settings(request: Request):
    return request.app.state.settings


def _me_payload(db: Database, user: dict, *, credits: int | None = None) -> dict:
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user.get("display_name"),
        "role": user["role"],
        "status": user["status"],
        "locale": user.get("locale", "vi"),
        "credits": db.scalar("SELECT credit_balance(%s)", (user["id"],)) if credits is None else credits,
    }


@router.post("/auth/register", status_code=201)
def register(body: RegisterIn, request: Request, response: Response) -> dict:
    """Tạo tài khoản. Cần mã mời hợp lệ, trừ khi `VISYNTH_OPEN_SIGNUP=1` (mặc định đóng)."""
    db, settings = _db(request), _settings(request)
    email = body.email.strip().lower()
    try:
        with db.tx() as cur:
            cur.execute("SELECT id FROM users WHERE lower(email) = %s", (email,))
            if cur.fetchone():
                raise Problem(409, "email_taken", "email đã được đăng ký")
            code = (body.invite_code or "").strip().upper()
            if not settings.open_signup and not code:
                raise Problem(422, "invite_required", "cần mã mời để đăng ký")
            if code:
                cur.execute(
                    """SELECT code, used_count, max_uses,
                              (revoked_at IS NOT NULL OR (expires_at IS NOT NULL AND expires_at < now())) AS dead
                         FROM invite_codes WHERE code = %s""",
                    (code,),
                )
                invite = cur.fetchone()
                if not invite or invite["dead"]:
                    raise Problem(404, "invite_invalid", "mã mời không hợp lệ")
                if invite["used_count"] >= invite["max_uses"]:
                    raise Problem(409, "invite_exhausted", "mã mời đã hết lượt")
            role = "admin" if settings.admin_email and email == settings.admin_email else "user"
            cur.execute(
                f"""INSERT INTO users (email, display_name, role, status, locale, tos_version, tos_accepted_at,
                        consent_cross_border_at, consent_shared_processing_at, age_confirmed_at)
                    VALUES (%s, %s, %s, 'active', %s, %s::text,
                            CASE WHEN %s::text IS NULL THEN NULL ELSE now() END,
                            CASE WHEN %s::boolean THEN now() END,
                            CASE WHEN %s::boolean THEN now() END,
                            CASE WHEN %s::boolean THEN now() END)
                    RETURNING {ME_COLUMNS}""",
                (
                    email,
                    body.display_name,
                    role,
                    body.locale,
                    body.tos_version,
                    body.tos_version,
                    body.consent_cross_border,
                    body.consent_shared_processing,
                    body.age_confirmed,
                ),
            )
            user = cur.fetchone()
            granted = 0
            if code:
                cur.execute("SELECT redeem_invite(%s, %s) AS granted", (code, user["id"]))
                granted = cur.fetchone()["granted"]
            if settings.signup_credits > 0:
                cur.execute(
                    "INSERT INTO credit_ledger (user_id, delta, reason, idempotency_key) VALUES (%s, %s, 'grant_signup', 'signup')",
                    (user["id"], settings.signup_credits),
                )
            cur.execute(
                "INSERT INTO local_credentials (user_id, password_hash) VALUES (%s, %s)",
                (user["id"], hash_password(body.password)),
            )
            cur.execute("SELECT credit_balance(%s) AS balance", (user["id"],))
            balance = cur.fetchone()["balance"]
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    token = issue_token(user, settings.session_secret, settings.session_ttl_s)
    set_session_cookie(response, token, settings)
    return {"user": _me_payload(db, user, credits=balance), "credits_granted": granted, "token": token}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    db, settings = _db(request), _settings(request)
    row = db.one(
        """SELECT u.id, u.status, c.password_hash
             FROM users u LEFT JOIN local_credentials c ON c.user_id = u.id
            WHERE lower(u.email) = %s AND u.deleted_at IS NULL""",
        (body.email.strip().lower(),),
    )
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise Problem(401, "invalid_credentials", "email hoặc mật khẩu không đúng")
    if row["status"] != "active":
        raise Problem(403, "account_inactive", f"tài khoản đang ở trạng thái {row['status']}")
    user = db.one(f"UPDATE users SET last_login_at = now() WHERE id = %s RETURNING {ME_COLUMNS}", (row["id"],))
    token = issue_token(user, settings.session_secret, settings.session_ttl_s)
    set_session_cookie(response, token, settings)
    return {"user": _me_payload(db, user), "token": token}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    clear_session_cookie(response, _settings(request))
    return {"ok": True}


@router.get("/me")
def me(request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    full = db.one(
        "SELECT id, email, display_name, role, status, locale, tos_version, consent_cross_border_at, "
        "consent_shared_processing_at, age_confirmed_at, created_at FROM users WHERE id = %s",
        (user["id"],),
    )
    jobs = db.one(
        "SELECT count(*) FILTER (WHERE status IN ('queued','running','awaiting_glossary')) AS active, count(*) AS total FROM jobs WHERE user_id = %s",
        (user["id"],),
    )
    settings = _settings(request)
    enabled = db.scalar("SELECT value FROM app_settings WHERE key = 'enabled_levels'") or [
        "full_translation",
        "detailed_synthesis",
        "deep_synthesis",
        "executive_brief",
    ]
    private_ok = bool(db.scalar("SELECT value FROM app_settings WHERE key = 'private_enabled'") or False)
    return {
        **_me_payload(db, full),
        "consent_cross_border": full["consent_cross_border_at"] is not None,
        "consent_shared_processing": full["consent_shared_processing_at"] is not None,
        "age_confirmed": full["age_confirmed_at"] is not None,
        "enabled_levels": enabled,
        "available_privacy_classes": ["standard", "private"] if private_ok else ["standard"],
        "jobs": jobs,
        "limits": {"max_upload_bytes": settings.max_upload_bytes},
    }


@router.post("/me/consents")
def record_consents(body: ConsentsIn, request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    row = db.one(
        f"""UPDATE users SET
              tos_version = COALESCE(%s::text, tos_version),
              tos_accepted_at = CASE WHEN %s::text IS NULL THEN tos_accepted_at ELSE now() END,
              consent_cross_border_at = CASE WHEN %s::boolean THEN now() ELSE consent_cross_border_at END,
              consent_shared_processing_at = CASE WHEN %s::boolean THEN now() ELSE consent_shared_processing_at END,
              age_confirmed_at = CASE WHEN %s::boolean THEN now() ELSE age_confirmed_at END
            WHERE id = %s RETURNING {ME_COLUMNS}""",
        (
            body.tos_version,
            body.tos_version,
            body.consent_cross_border,
            body.consent_shared_processing,
            body.age_confirmed,
            user["id"],
        ),
    )
    return _me_payload(db, row)


@router.get("/credits")
def list_credits(
    request: Request, limit: int = 50, cursor: int | None = None, user: dict = Depends(current_user)
) -> dict:
    db = _db(request)
    limit = max(1, min(limit, 200))
    rows = db.all(
        "SELECT id, delta, reason, job_id, meta, created_at FROM credit_ledger "
        "WHERE user_id = %s AND (%s::bigint IS NULL OR id < %s::bigint) ORDER BY id DESC LIMIT %s",
        (user["id"], cursor, cursor, limit + 1),
    )
    items = rows[:limit]
    return {
        "items": items,
        "next_cursor": str(items[-1]["id"]) if len(rows) > limit else None,
        "balance": db.scalar("SELECT credit_balance(%s)", (user["id"],)),
    }


@router.post("/invites/redeem")
def redeem(body: RedeemIn, request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    try:
        with db.tx() as cur:
            cur.execute("SELECT redeem_invite(%s, %s) AS granted", (body.code, user["id"]))
            granted = cur.fetchone()["granted"]
            cur.execute("SELECT credit_balance(%s) AS balance", (user["id"],))
            balance = cur.fetchone()["balance"]
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    return {"credits_granted": granted, "balance": balance}
