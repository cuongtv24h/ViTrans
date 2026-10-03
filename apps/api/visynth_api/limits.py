"""Giới hạn tốc độ ở tầng API (M3, §20.4).

Vì sao cần: trước M3, `/auth/login` không có gì chặn người thử mật khẩu hàng nghìn lần, và `POST /jobs`
cũng không chặn một tài khoản xếp hàng trăm job. Đây là hai lỗ hổng rẻ tiền nhưng nguy hiểm nhất của
một dịch vụ có đăng nhập + tiêu tiền.

Cách làm:

* bộ đếm nằm ở PostgreSQL (hàm `rate_limit_hit` — cửa sổ cố định), nên **mọi tiến trình API dùng chung**
  một hạn mức; bộ đếm trong RAM sẽ bị chia nhỏ khi `uvicorn` chạy nhiều worker;
* danh tính được **băm** trước khi lưu (`sha256(salt + loại + giá trị)`): không lưu IP thô, không lưu
  email, mà vẫn chặn được dò mật khẩu (PDPL §14.2 — tối thiểu hoá dữ liệu);
* **hỏng CSDL thì cho qua** (fail-open) nhưng ghi log: khi CSDL hỏng thì mọi thứ đã hỏng rồi, chặn thêm
  người dùng thật không làm hệ thống an toàn hơn;
* hạn mức đặt trong `Settings` để máy phát triển tắt được, nhưng **giá trị mặc định là giá trị chạy thật**.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from visynth_api.errors import Problem

log = logging.getLogger("visynth.api.limits")


@dataclass(frozen=True)
class Rule:
    """Một hạn mức: `limit` lần trong `window_s` giây, đếm theo `kind` danh tính."""

    scope: str
    limit: int
    window_s: int
    kind: str = "identity"  # identity | ip

    @property
    def window_label(self) -> str:
        if self.window_s % 3600 == 0:
            return f"{self.window_s // 3600} giờ"
        if self.window_s % 60 == 0:
            return f"{self.window_s // 60} phút"
        return f"{self.window_s} giây"


def hash_subject(value: str, salt: str) -> str:
    """Băm danh tính trước khi lưu — CSDL không bao giờ thấy IP/email thô."""
    digest = hashlib.sha256(f"{salt}|{value}".encode()).hexdigest()
    return digest[:40]


def anonymous_id(request: Request) -> str:
    """Danh tính cho người chưa đăng nhập: ưu tiên IP do proxy tin cậy đặt, sau đó là IP kết nối."""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    client = getattr(request.client, "host", "") or ""
    return forwarded or client or "unknown"


def identity_of(request: Request, salt: str) -> str:
    """Danh tính để đếm: người đã đăng nhập (theo token) hoặc IP — cả hai đều băm trước khi lưu."""
    token = request.cookies.get("visynth_session") or ""
    if not token:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if token:
        return hash_subject(f"token:{token}", salt)
    return hash_subject(f"ip:{anonymous_id(request)}", salt)


class RateLimiter:
    """Gọi hàm SQL `rate_limit_hit` và biến kết quả thành câu trả lời 429 có hợp đồng."""

    def __init__(self, db, salt: str) -> None:
        self.db = db
        self.salt = salt

    def check(self, rule: Rule, subject_value: str) -> tuple[bool, int, int]:
        """Trả `(cho_qua, số_lần_đã_dùng, retry_after)`. Lỗi CSDL ⇒ cho qua (fail-open)."""
        subject = hash_subject(f"{rule.kind}:{subject_value}", self.salt)
        try:
            row = self.db.one(
                "SELECT * FROM rate_limit_hit(%s, %s, %s, %s)",
                (rule.scope, subject, rule.limit, rule.window_s),
            )
        except Exception as exc:  # noqa: BLE001 - không để giới hạn tốc độ làm sập API
            log.warning("bỏ qua giới hạn tốc độ (%s) vì CSDL lỗi: %s", rule.scope, exc)
            return True, 0, 0
        if row is None:
            return True, 0, 0
        allowed = bool(row["allowed"])
        if not allowed:
            self._note_block(rule, subject)
        return allowed, int(row["hits"]), int(row["retry_after_s"])

    def _note_block(self, rule: Rule, subject: str) -> None:
        """Ghi lại một lần BỊ CHẶN để `ops health` phát hiện "đang bị dò" (cảnh báo §20.7).

        Đây là ghi thêm trên đường đã bị chặn (hiếm), và hỏng thì bỏ qua — không để việc ghi số liệu
        làm hỏng câu trả lời 429 mà người dùng đang chờ.
        """
        try:
            self.db.one("SELECT * FROM rate_limit_hit(%s, %s, %s, %s)", (f"blocked:{rule.scope}", subject, 1, 3600))
        except Exception as exc:  # noqa: BLE001 - số liệu là việc phụ
            log.debug("không ghi được dấu vết chặn (%s): %s", rule.scope, exc)

    def enforce(self, rule: Rule, subject_value: str, *, message: str | None = None) -> None:
        allowed, hits, retry_after = self.check(rule, subject_value)
        if allowed:
            return
        raise Problem(
            429,
            "rate_limited",
            message or f"quá nhiều yêu cầu cho thao tác này; thử lại sau {retry_after} giây",
            rule=rule.scope,
            limit=rule.limit,
            window=rule.window_label,
            hits=hits,
            retry_after_s=retry_after,
        )


#: Đường dẫn API + phương thức cần chặn (khớp theo biểu thức chính quy trên `path`), và hạn mức.
#: Đặt ở đây thay vì rải trong từng router để nhìn một chỗ là biết toàn bộ bề mặt bị giới hạn.
ROUTE_RULES: tuple[tuple[str, str, str, str], ...] = (
    # (phương thức, mẫu đường dẫn, tên hạn mức trong Settings, nhãn cho nhật ký)
    ("POST", r"^/api/v1/auth/login$", "rate_limit_login_per_min", "đăng nhập"),
    ("POST", r"^/api/v1/auth/register$", "rate_limit_register_per_hour", "đăng ký"),
    ("POST", r"^/api/v1/invites/redeem$", "rate_limit_redeem_per_hour", "nhập mã mời"),
    ("POST", r"^/api/v1/takedown$", "rate_limit_takedown_per_hour", "yêu cầu gỡ bỏ"),
    ("POST", r"^/api/v1/documents$", "rate_limit_upload_per_hour", "tải tài liệu"),
    ("POST", r"^/api/v1/jobs$", "rate_limit_jobs_per_hour", "tạo job"),
    ("POST", r"^/api/v1/jobs/estimate$", "rate_limit_estimate_per_hour", "báo giá"),
    ("POST", r"^/api/v1/jobs/[0-9a-fA-F-]{36}/glossary/confirm$", "rate_limit_gate_per_hour", "duyệt thuật ngữ"),
)

#: Trần thô cho toàn bộ API còn lại (chống quét/lạm dụng), đếm theo danh tính.
BROAD_RULE = Rule(scope="api", limit=0, window_s=60)  # `limit` lấy từ Settings khi dựng middleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Chặn theo hạn mức đã cấu hình. Chỉ áp cho `/api/v1/*`; tệp tĩnh và `/docs` không đếm."""

    def __init__(self, app, settings) -> None:
        super().__init__(app)
        self.settings = settings
        self.rules: list[tuple[re.Pattern[str], Rule]] = []
        for method, pattern, attr, _label in ROUTE_RULES:
            limit = int(getattr(settings, attr, 0) or 0)
            rule = Rule(scope=f"{method.lower()}:{pattern}", limit=limit, window_s=_window_of(attr, settings))
            self.rules.append((re.compile(pattern), rule))
        self.rule_methods = [method for method, *_ in ROUTE_RULES]
        self.broad = Rule(
            scope="api:broad", limit=int(getattr(settings, "rate_limit_api_per_min", 0) or 0), window_s=60
        )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/v1") or path == "/api/v1/healthz":
            return await call_next(request)
        limiter: RateLimiter = request.app.state.rate_limiter
        identity = identity_of(request, limiter.salt)
        ip = anonymous_id(request)

        for index, (pattern, rule) in enumerate(self.rules):
            if self.rule_methods[index] == request.method and pattern.match(path):
                allowed, _hits, retry_after = limiter.check(rule, identity if rule.kind == "identity" else ip)
                if not allowed:
                    return _too_many(rule, retry_after)
                break

        if self.broad.limit > 0:
            allowed, _hits, retry_after = limiter.check(self.broad, identity)
            if not allowed:
                return _too_many(self.broad, retry_after)
        return await call_next(request)


def _window_of(attr: str, settings) -> int:
    """Cửa sổ suy ra từ tên hạn mức: `…_per_min` = 60 giây, `…_per_hour` = 3600 giây."""
    del settings
    return 3600 if attr.endswith("_per_hour") else 60


def _too_many(rule: Rule, retry_after: int) -> JSONResponse:
    problem = Problem(
        429,
        "rate_limited",
        f"quá nhiều yêu cầu; thử lại sau {retry_after} giây",
        retry_after_s=retry_after,
        limit=rule.limit,
        window=rule.window_label,
    )
    response = JSONResponse(status_code=429, content=problem.as_payload(), media_type="application/problem+json")
    response.headers["Retry-After"] = str(retry_after)
    response.headers["X-RateLimit-Limit"] = str(rule.limit)
    response.headers["X-RateLimit-Window"] = str(rule.window_s)
    return response


def login_subject(email: str) -> str:
    """Danh tính cho hạn mức theo TÀI KHOẢN (chống dò mật khẩu phân tán qua nhiều IP)."""
    return f"email:{email.strip().lower()}"


#: Hạn mức theo TÀI KHOẢN: middleware chỉ đếm được theo IP (không đọc thân yêu cầu), nên kẻ tấn công
#: đổi IP liên tục vẫn dò được mật khẩu. Tuyến `/auth/login` gọi thêm hạn mức này theo email.
ACCOUNT_LOGIN_WINDOW_S = 900


def account_login_rule(settings) -> Rule:
    return Rule(
        scope="login:account",
        limit=int(getattr(settings, "rate_limit_login_per_account", 0) or 0),
        window_s=ACCOUNT_LOGIN_WINDOW_S,
    )


def now_epoch() -> int:  # pragma: no cover - chỉ để đọc dễ hơn ở nơi khác
    return int(time.time())
