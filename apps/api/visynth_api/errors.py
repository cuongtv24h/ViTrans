"""Lỗi API theo RFC 9457 (`application/problem+json`) — khớp mục `Problem` của `docs/api/openapi.yaml`."""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import Request
from fastapi.responses import JSONResponse

#: Thông điệp `RAISE EXCEPTION` trong `docs/db/schema.sql` → mã lỗi hợp đồng + HTTP status.
SQL_ERRORS: dict[str, tuple[int, str]] = {
    "insufficient_credits": (402, "insufficient_credits"),
    "amount_must_be_positive": (422, "invalid_amount"),
    "user_not_found": (400, "unknown_user"),
    "invite_invalid": (404, "invite_invalid"),
    "invite_exhausted": (409, "invite_exhausted"),
    "deployment_not_available": (409, "no_deployment"),
    "no_price_for_model": (422, "no_price"),
    "privacy_class_violation": (409, "privacy_violation"),
    "spend_cap_reached": (503, "spend_cap_reached"),
    "quota_group_exhausted": (429, "quota_exhausted"),
    "lease_held": (409, "lease_held"),
}
#: `CHECK`/ràng buộc vi phạm — lỗi phía người gọi.
SQLSTATES: dict[str, tuple[int, str]] = {
    "23505": (409, "conflict"),
    "23503": (409, "reference_missing"),
    "23514": (422, "constraint_violation"),
    "22P02": (400, "invalid_input"),
    "42501": (403, "forbidden"),
}


class Problem(Exception):
    """Lỗi có hợp đồng: mã máy đọc được + thông điệp tiếng Việt cho người dùng."""

    def __init__(self, status: int, code: str, detail: str, *, title: str | None = None, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.title = title or _TITLES.get(status, "Lỗi")
        self.extra = extra

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "about:blank",
            "title": self.title,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
        }
        payload.update(self.extra)
        return payload


_TITLES = {
    400: "Yêu cầu không hợp lệ",
    401: "Cần đăng nhập",
    402: "Không đủ tín dụng",
    403: "Không có quyền",
    404: "Không tìm thấy",
    409: "Xung đột trạng thái",
    413: "Tệp quá lớn",
    415: "Định dạng không hỗ trợ",
    422: "Dữ liệu không hợp lệ",
    429: "Quá nhiều yêu cầu",
    503: "Tạm thời không phục vụ",
}


def sql_problem(exc: psycopg.Error) -> Problem:
    """Dịch lỗi PostgreSQL thành `Problem` theo hợp đồng (SQLSTATE + thông điệp nghiệp vụ)."""
    message = (exc.diag.message_primary or "").strip()
    if message in SQL_ERRORS:
        status, code = SQL_ERRORS[message]
        return Problem(status, code, _VI_MESSAGES.get(message, message))
    state = exc.sqlstate or ""
    if state in SQLSTATES:
        status, code = SQLSTATES[state]
        detail = message or "ràng buộc dữ liệu bị vi phạm"
        return Problem(status, code, detail)
    return Problem(500, "internal_error", "lỗi máy chủ")


_VI_MESSAGES = {
    "insufficient_credits": "không đủ tín dụng cho job này",
    "amount_must_be_positive": "số tín dụng phải lớn hơn 0",
    "user_not_found": "không có người dùng này",
    "invite_invalid": "mã mời không hợp lệ hoặc đã hết hạn",
    "invite_exhausted": "mã mời đã hết lượt",
    "no_price_for_model": "chưa có giá cho model này trong bảng `llm_prices`",
    "spend_cap_reached": "đã chạm trần chi tiêu trong ngày",
    "deployment_not_available": "không còn deployment phù hợp cho chế độ này",
    "privacy_class_violation": "chế độ riêng tư không cho phép dùng deployment này",
    "quota_group_exhausted": "nhóm tài khoản đã hết hạn mức",
    "lease_held": "task đang được worker khác giữ",
}


async def problem_handler(request: Request, exc: Problem) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content=exc.as_payload(),
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"} if exc.status == 401 else None,
    )


async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - phòng thủ
    problem = Problem(500, "internal_error", "lỗi máy chủ")
    return JSONResponse(status_code=500, content=problem.as_payload(), media_type="application/problem+json")
