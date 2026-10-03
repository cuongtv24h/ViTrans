"""Client gọi dịch vụ parser sandbox (SPEC §6.1): API gửi byte thô, nhận JSON đã bóc tách.

Cố ý chỉ dùng `urllib` (không thêm phụ thuộc) và **không bao giờ** gửi kèm nội dung nào khác.
Timeout mặc định 120 giây khớp trần của sandbox; parser không trả lời thì API trả lỗi rõ ràng thay vì treo.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from visynth.extract.model import Extraction

#: Mã lỗi tạm thời (parser bận/chết) — API đổi thành 503 để người dùng thử lại.
RETRYABLE_CODES = ("parse_timeout",)


class ParserUnavailable(RuntimeError):
    """Không gọi được dịch vụ parser (chưa bật, mạng nội bộ hỏng, quá thời gian)."""


class ParserRejected(RuntimeError):
    """Parser từ chối tệp (mã lỗi + thông điệp từ sandbox)."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def parse_via_service(
    base_url: str,
    raw: bytes,
    *,
    filename: str | None = None,
    title: str | None = None,
    timeout_s: float = 120.0,
) -> Extraction:
    """Gửi tệp cho sandbox và dựng lại `Extraction`. Ném `ParserRejected`/`ParserUnavailable`."""
    query = urllib.parse.urlencode({k: v for k, v in (("filename", filename), ("title", title)) if v})
    url = f"{base_url.rstrip('/')}/parse" + (f"?{query}" if query else "")
    request = urllib.request.Request(  # noqa: S310 - URL do cấu hình hệ thống quy định
        url, data=raw, method="POST", headers={"Content-Type": "application/octet-stream"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"code": "parse_failed", "message": body[:200]}
        raise ParserRejected(parsed.get("code", "parse_failed"), parsed.get("message", ""), exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ParserUnavailable(str(exc)) from exc
    return Extraction.from_dict(payload)
