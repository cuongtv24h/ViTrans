"""Adapter HTTP thật cho hai họ API (SPEC §17.6, §17.7): `gemini_native` và `openai_compat`.

Không dùng SDK riêng của nhà cung cấp: chỉ `urllib` của thư viện chuẩn, để mọi lỗi mạng/HTTP đi qua đúng một
đường và được `classify_http` phân loại giống nhau. Adapter KHÔNG quyết định chính sách (chọn deployment,
cooldown, chuyển dự phòng) — việc đó thuộc `Router`/`PooledLLMClient`.

Khoá API được truyền vào từng lời gọi (`api_key`) chứ không lưu trong adapter; không log khoá, không log
nội dung người dùng.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from visynth.llm.base import LLMError, LLMRequest, LLMResponse, Outcome, classify_http


@dataclass(frozen=True)
class HTTPReply:
    """Phản hồi HTTP thô. `body` là dict đã parse (hoặc None nếu không phải JSON)."""

    status: int
    headers: dict
    body: Any = None
    elapsed_ms: int = 0


class NetworkError(RuntimeError):
    """Không kết nối được (DNS, TLS, timeout mạng) — retry được như 'server_error'."""


Transport = Callable[[str, str, dict, bytes, float], HTTPReply]


def urllib_transport(method: str, url: str, headers: dict, data: bytes, timeout_s: float) -> HTTPReply:
    """Transport mặc định. Ném `NetworkError` cho mọi lỗi không có mã HTTP."""
    req = urllib.request.Request(url, data=data or None, headers=headers, method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - URL do pool_config quy định
            raw = resp.read()
            status, hdrs = resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:  # phản hồi có mã lỗi: vẫn đọc body để phân loại hạn mức
        raw, status, hdrs = e.read(), e.code, dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NetworkError(str(e)) from e
    elapsed = int((time.monotonic() - started) * 1000)
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        body = None
    return HTTPReply(status, hdrs, body, elapsed)


def _finish_reason(kind: str, body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    if kind == "gemini_native":
        cands = body.get("candidates") or [{}]
        return cands[0].get("finishReason")
    choices = body.get("choices") or [{}]
    return choices[0].get("finish_reason")


def _first_text(kind: str, body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    if kind == "gemini_native":
        parts = ((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    return msg.get("content") or ""


def _usage(kind: str, body: Any) -> tuple[int, int]:
    if not isinstance(body, dict):
        return 0, 0
    if kind == "gemini_native":
        u = body.get("usageMetadata") or {}
        return int(u.get("promptTokenCount") or 0), int(u.get("candidatesTokenCount") or 0)
    u = body.get("usage") or {}
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _result(kind: str, reply: HTTPReply, request: LLMRequest, model: str, deployment_id: str) -> LLMResponse:
    finish = _finish_reason(kind, reply.body)
    outcome = classify_http(kind, reply.status, reply.headers, reply.body, finish)
    tin, tout = _usage(kind, reply.body)
    text = _first_text(kind, reply.body)
    if reply.status == 200 and not text:
        outcome = Outcome("invalid_output", tokens_in=tin, tokens_out=tout)
    if not outcome.tokens_in:
        outcome = Outcome(
            outcome.kind,
            tokens_in=tin,
            tokens_out=tout,
            latency_ms=reply.elapsed_ms,
            retry_after_s=outcome.retry_after_s,
            scope=outcome.scope,
        )
    return LLMResponse(
        text=text,
        outcome=outcome,
        parsed=None,
        model=model,
        deployment_id=deployment_id,
        latency_ms=reply.elapsed_ms,
    )


@dataclass
class GeminiAdapter:
    """`POST {base_url}/models/{model}:generateContent` (Google Generative Language API)."""

    base_url: str
    model_uri: str  # tên model phía nhà cung cấp, ví dụ 'gemini-3.8-flash'
    deployment_id: str = ""
    transport: Transport = urllib_transport
    timeout_s: float = 180.0
    structured: str = "json_schema"
    kind: str = "gemini_native"

    def _body(self, request: LLMRequest) -> dict:
        gen: dict[str, Any] = {
            "temperature": request.temperature,
            "maxOutputTokens": request.max_output_tokens,
        }
        if request.schema is not None and self.structured != "none":
            gen["responseMimeType"] = "application/json"
            if self.structured == "json_schema":
                gen["responseJsonSchema"] = request.schema
        if request.thinking in ("off", "low"):
            gen["thinkingConfig"] = {"thinkingBudget": 0 if request.thinking == "off" else 512}
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": gen,
        }
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        return body

    def complete(self, request: LLMRequest, api_key: str) -> LLMResponse:
        url = f"{self.base_url.rstrip('/')}/models/{self.model_uri}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        data = json.dumps(self._body(request), ensure_ascii=False).encode("utf-8")
        reply = self.transport("POST", url, headers, data, self.timeout_s)
        return _result(self.kind, reply, request, self.model_uri, self.deployment_id)


@dataclass
class OpenAICompatAdapter:
    """`POST {base_url}/chat/completions` (OpenAI Chat Completions, dùng cho NVIDIA/OpenRouter/...)."""

    base_url: str
    model_uri: str
    deployment_id: str = ""
    transport: Transport = urllib_transport
    timeout_s: float = 180.0
    structured: str = "json_schema"
    kind: str = "openai_compat"

    def _body(self, request: LLMRequest) -> dict:
        body: dict[str, Any] = {
            "model": self.model_uri,
            "messages": ([{"role": "system", "content": request.system}] if request.system else [])
            + [{"role": "user", "content": request.user}],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.schema is not None and self.structured != "none":
            if self.structured == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": f"visynth_{request.prompt_id}", "schema": request.schema, "strict": False},
                }
            else:
                body["response_format"] = {"type": "json_object"}
        return body

    def complete(self, request: LLMRequest, api_key: str) -> LLMResponse:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        data = json.dumps(self._body(request), ensure_ascii=False).encode("utf-8")
        reply = self.transport("POST", url, headers, data, self.timeout_s)
        return _result(self.kind, reply, request, self.model_uri, self.deployment_id)


def list_models(
    provider: dict,
    api_key: str,
    *,
    transport: Transport | None = None,
    timeout_s: float = 30.0,
) -> list[str]:
    """Liệt kê model khả dụng của một provider — KHÔNG tốn token, dùng để chọn id thật cho `pool_config`.

    Google: `GET {base_url}/models`, chỉ lấy model có `generateContent`.
    Chuẩn OpenAI: `GET {base_url}/models` (NVIDIA, OpenRouter, ...).
    Khoá chỉ nằm trong header; không in, không log. Lỗi khoá/quyền ném `LLMError` để CLI in thông điệp rõ ràng.
    """
    kind = provider.get("kind", "openai_compat")
    base = str(provider["base_url"]).rstrip("/")
    headers = {"x-goog-api-key": api_key} if kind == "gemini_native" else {"Authorization": f"Bearer {api_key}"}
    url = f"{base}/models?pageSize=200" if kind == "gemini_native" else f"{base}/models"
    call = transport or urllib_transport
    try:
        reply = call("GET", url, headers, b"", timeout_s)
    except NetworkError as e:
        raise LLMError(Outcome("network_error"), f"không kết nối được '{provider.get('id')}': {e}") from e
    if reply.status != 200:
        outcome = classify_http(kind, reply.status, reply.headers, reply.body)
        raise LLMError(outcome, f"'{provider.get('id')}': HTTP {reply.status} ({outcome.kind})")
    body = reply.body if isinstance(reply.body, dict) else {}
    if kind == "gemini_native":
        names = [
            str(m.get("name", "")).removeprefix("models/")
            for m in body.get("models") or []
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
        ]
    else:
        names = [str(m.get("id", "")) for m in body.get("data") or []]
    return sorted({n for n in names if n})


def make_adapter(provider: dict, deployment, *, transport: Transport | None = None, timeout_s: float = 180.0):
    """Dựng adapter cho một deployment theo `provider.kind` trong `pool_config`."""
    kind = provider.get("kind", "openai_compat")
    base_url = provider["base_url"]
    model_uri = deployment.model.id.split(":", 1)[-1]
    common = dict(
        base_url=base_url,
        model_uri=model_uri,
        deployment_id=deployment.id,
        transport=transport or urllib_transport,
        timeout_s=timeout_s,
        structured=deployment.model.structured,
    )
    if kind == "gemini_native":
        return GeminiAdapter(**common)
    return OpenAICompatAdapter(**common)
