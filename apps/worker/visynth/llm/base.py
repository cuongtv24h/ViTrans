"""Hợp đồng LLM client — bản port trung thành từ `docs/reference/llm_pool.py` (SPEC §17.6, §17.7).

Chỉ chứa phần *hợp đồng* (request/response/outcome/phân loại HTTP). Router, hạn mức, đặt chỗ
nguyên tử thuộc M0-W3 và sẽ nằm trong `visynth/pool/`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ------------------------------------------------------------------ kết quả một lời gọi (§17.7)

#: Lỗi/trạng thái được phép thử lại ở deployment khác hoặc sau khi chờ.
RETRYABLE = frozenset(
    {"rate_limited_minute", "rate_limited_day", "rate_limited_unknown", "server_error", "timeout", "truncated"}
)
#: Lỗi không thử lại nguyên trạng (thử lại vô ích, phải đổi cách xử lý).
FATAL = frozenset({"auth_error", "bad_request", "context_exceeded", "safety_blocked"})


@dataclass(frozen=True)
class Outcome:
    """Kết quả một lời gọi theo từ vựng của SPEC §17.7 (khớp `reference/llm_pool.py`)."""

    kind: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int | None = None
    retry_after_s: float | None = None
    scope: str = "deployment"  # 429 áp cho 'deployment' hay cả 'group' (tài khoản)


# ------------------------------------------------------------------ request / response


@dataclass(frozen=True)
class Media:
    """Tệp đính kèm cho lời gọi (hiện chỉ dùng cho P10 OCR: PDF gửi kèm theo cụm trang).

    `data` là byte thô; KHÔNG bao giờ ghi vào sổ `llm_calls` (§17.11: sổ chỉ ghi số đo, không ghi nội dung).
    """

    mime_type: str
    data: bytes
    name: str = ""

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class LLMRequest:
    """Một lời gọi LLM đã được render đầy đủ (SPEC §8.2, §17.6)."""

    prompt_id: str  # 'P0'..'P13'
    system: str
    user: str
    schema: dict | None = None  # JSON Schema của đầu ra (structured output)
    max_output_tokens: int = 4096
    temperature: float = 0.2
    thinking: str = "default"  # 'off' | 'low' | 'default'
    needs: dict[str, Any] = field(default_factory=dict)  # {'structured','min_ctx_in','vision','pdf'}
    metadata: dict[str, Any] = field(default_factory=dict)  # job/segment/section để ghi sổ
    media: tuple[Media, ...] = ()  # tệp đính kèm (P10 OCR); rỗng với mọi prompt khác

    @property
    def text(self) -> str:
        return f"{self.system}\n\n{self.user}"


@dataclass(frozen=True)
class LLMResponse:
    text: str
    outcome: Outcome
    parsed: Any | None = None
    model: str = ""
    deployment_id: str = ""
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome.kind == "ok"


class LLMError(RuntimeError):
    """Lời gọi thất bại. Pipeline bắt lỗi này để quyết định hoãn/đổi deployment/đánh cờ."""

    def __init__(self, outcome: Outcome, detail: str = "", *, deployment_id: str = ""):
        super().__init__(f"{outcome.kind}{': ' + detail if detail else ''}")
        self.outcome = outcome
        self.detail = detail
        self.deployment_id = deployment_id

    @property
    def retryable(self) -> bool:
        return self.outcome.kind in RETRYABLE


@runtime_checkable
class LLMClient(Protocol):
    """Mọi adapter (openai_compat, gemini_native, fake) đều có đúng một phương thức này."""

    def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover - protocol
        ...


# ------------------------------------------------------------------ phân loại phản hồi HTTP


def _retry_delay(body: Any) -> float | None:
    """Gemini: `error.details[].retryDelay` = '34s' hoặc '34.5s'."""
    for d in (body or {}).get("error", {}).get("details", []) or []:
        rd = d.get("retryDelay")
        if isinstance(rd, str) and rd.endswith("s"):
            try:
                return float(rd[:-1])
            except ValueError:
                return None
    return None


def _looks_like_bad_key(err: dict, msg: str) -> bool:
    """Khoá Google không hợp lệ: `API_KEY_INVALID` trong `details` hoặc "API key not valid" trong thông điệp."""
    blob = f"{msg} {err.get('details', '')}".lower()
    return "api_key_invalid" in blob or "api key not valid" in blob or "api key expired" in blob


def classify_http(
    kind: str,
    status: int,
    headers: dict | None = None,
    body: Any = None,
    finish_reason: str | None = None,
) -> Outcome:
    """Ánh xạ phản hồi của adapter → Outcome (SPEC §17.7). `kind`: 'openai_compat' | 'gemini_native'.

    `body` là dict đã parse JSON (hoặc None); `headers` khoá viết thường.
    Các chuỗi nhận diện hạn mức ngày/phút là HEURISTIC — phải kiểm chứng bằng thực nghiệm ở M0-W3
    với từng nhà cung cấp, rồi sửa cùng lúc ở đây và ở `docs/reference/llm_pool.py`.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    body = body if isinstance(body, dict) else {}
    err = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
    msg = f"{err.get('message', '')} {err.get('code', '')} {err.get('status', '')}"
    if status == 200:
        if finish_reason in ("length", "MAX_TOKENS"):
            return Outcome("truncated")
        if finish_reason in ("content_filter", "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            return Outcome("safety_blocked")
        return Outcome("ok")
    ra = None
    if "retry-after" in headers:
        try:
            ra = float(headers["retry-after"])
        except ValueError:
            ra = None
    if status == 429:
        if kind == "gemini_native":
            ra = _retry_delay(body) or ra
            quota_ids = " ".join(
                v.get("quotaId", "") for d in err.get("details", []) or [] for v in d.get("violations", []) or []
            )
            if "PerDay" in quota_ids:
                return Outcome("rate_limited_day", retry_after_s=ra)
            if "PerMinute" in quota_ids:
                return Outcome("rate_limited_minute", retry_after_s=ra)
        low = msg.lower()
        if "insufficient_quota" in low or re.search(r"per day|\btpd\b|\brpd\b|daily|credits? (are )?exhausted", low):
            return Outcome("rate_limited_day", retry_after_s=ra)
        if re.search(r"per minute|\btpm\b|\brpm\b", low):
            return Outcome("rate_limited_minute", retry_after_s=ra)
        return Outcome("rate_limited_unknown", retry_after_s=ra)
    if status in (401, 403):
        return Outcome("auth_error")
    if status == 400 and kind == "gemini_native" and _looks_like_bad_key(err, msg):
        # Google trả 400 INVALID_ARGUMENT ("API key not valid"/API_KEY_INVALID) cho khoá sai — không phải 401.
        return Outcome("auth_error")
    if status == 413 or (
        status == 400 and re.search(r"context|too many tokens|token count|maximum.*length|too long", msg, re.I)
    ):
        return Outcome("context_exceeded")
    if status in (400, 404, 422):
        return Outcome("bad_request")
    if status in (408, 504):
        return Outcome("timeout")
    if status >= 500:
        return Outcome("server_error", retry_after_s=ra)
    return Outcome("bad_request")
