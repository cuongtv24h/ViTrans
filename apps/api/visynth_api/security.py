"""Phiên đăng nhập (M1): mật khẩu scrypt + token ký HMAC trong cookie `HttpOnly`.

Token tự chứa (không lưu phiên trong CSDL) vì M1 chỉ cần một tiến trình API; khi tách nhiều
instance thì thay bằng bảng `sessions` — ghi ở `docs/BUILD_PLAN.md`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import Depends, Request

from visynth_api.db import Database
from visynth_api.errors import Problem

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


# ------------------------------------------------------------------ mật khẩu
def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash` — tham số ghi kèm để đổi tham số sau này vẫn xác thực được."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        scheme, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# ------------------------------------------------------------------ token phiên
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_token(user: dict, secret: str, ttl_s: int) -> str:
    body = _b64(json.dumps({"uid": str(user["id"]), "role": user["role"], "exp": int(time.time()) + ttl_s}).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token: str, secret: str) -> dict[str, Any] | None:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload


def set_session_cookie(response, token: str, settings) -> None:
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=settings.session_ttl_s,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response, settings) -> None:
    response.delete_cookie(settings.cookie_name, path="/")


# ------------------------------------------------------------------ phụ thuộc
def current_user(request: Request) -> dict:
    """Người dùng của yêu cầu: ưu tiên cookie phiên, sau đó `Authorization: Bearer`."""
    settings = request.app.state.settings
    db: Database = request.app.state.db
    token = request.cookies.get(settings.cookie_name) or ""
    header = request.headers.get("authorization", "")
    if not token and header.lower().startswith("bearer "):
        token = header[7:].strip()
    payload = read_token(token, settings.session_secret) if token else None
    if payload is None:
        raise Problem(401, "unauthorized", "cần đăng nhập")
    user = db.one(
        "SELECT id, email, display_name, role, status, locale, tos_version FROM users "
        "WHERE id = %s AND deleted_at IS NULL",
        (payload["uid"],),
    )
    if user is None or user["status"] != "active":
        raise Problem(401, "unauthorized", "phiên không còn hiệu lực")
    return user


def require_role(role: str):
    allowed = {"curator": {"curator", "admin"}, "admin": {"admin"}}[role]

    def dependency(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in allowed:
            raise Problem(403, "forbidden", f"cần vai trò {role}")
        return user

    return dependency
