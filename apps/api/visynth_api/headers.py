"""Header bảo mật ở tầng ứng dụng (M3, §20.4).

`infra/Caddyfile` đã đặt các header này, nhưng Caddy chỉ có trên VPS. Khi chạy API trực tiếp (máy phát
triển, hoặc ai đó gọi cổng 8000 sau khi proxy hỏng), trình duyệt vẫn phải nhận được các chỉ dẫn an toàn.
Đặt ở đây là lớp thứ hai — nguyên tắc phòng thủ nhiều lớp: **không phụ thuộc một tầng duy nhất**.

Lưu ý CSP: SPA là ES module tĩnh + CSS thuần, không có script nội tuyến và không nạp từ CDN nào, nên
`default-src 'self'` là đủ. Font/hình đều phục vụ cùng gốc.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

#: Áp cho mọi câu trả lời. `X-Frame-Options: DENY` + `frame-ancestors 'none'` chặn bị nhúng vào iframe
#: (chống clickjacking lên nút "Xoá tài liệu"/"Duyệt thuật ngữ").
BASE_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "geolocation=(), microphone=(), camera=()",
    "cross-origin-opener-policy": "same-origin",
}

#: CSP cho SPA. `connect-src 'self'` cho phép SSE cùng gốc; `img-src 'self' data:` cho icon nhỏ.
CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Gắn header bảo mật. HSTS chỉ gắn khi chạy HTTPS thật (đặt HSTS ở HTTP vô nghĩa nhưng vô hại)."""

    def __init__(self, app: ASGIApp, *, hsts_max_age_s: int = 0) -> None:
        super().__init__(app)
        self.hsts_max_age_s = max(0, int(hsts_max_age_s))

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for name, value in BASE_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers.setdefault("content-security-policy", CSP)
        if self.hsts_max_age_s:
            response.headers.setdefault(
                "strict-transport-security", f"max-age={self.hsts_max_age_s}; includeSubDomains"
            )
        return response
