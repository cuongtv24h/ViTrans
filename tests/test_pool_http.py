"""Adapter HTTP thật chạy qua máy chủ giả cục bộ (SPEC §17.6–§17.9).

Vì sao có tệp này: bài kiểm định `probe` (§17.9) chỉ đo được khi có mạng tới nhà cung cấp thật, mà
máy phát triển/CI thì không có. Ở đây dựng một máy chủ HTTP cục bộ mô phỏng **đúng** hình dạng phản hồi
của Google Generative Language API và chuẩn OpenAI Chat Completions, để chốt ba thứ trước khi tốn tiền:

* định dạng request mà adapter gửi đi (đường dẫn, header xác thực, thân JSON);
* ánh xạ mã lỗi thật → `Outcome` (§17.7), gồm cả ca Google trả **400** cho khoá sai;
* đường đi trọn vẹn `probe_deployment` và `PooledLLMClient` trên HTTP thật (không phải transport giả),
  kèm ràng buộc: khoá không bao giờ lọt vào sổ `llm_calls` hay phản hồi trả về tầng trên.

Máy chủ chỉ nghe `127.0.0.1`, không cần mạng ngoài, không gọi API nào của thật.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from visynth.llm.base import LLMError, LLMRequest
from visynth.pool import Ledger, PooledLLMClient, load_pool_model
from visynth.pool.adapters import GeminiAdapter, NetworkError, OpenAICompatAdapter, list_models
from visynth.pool.probe import probe_deployment
from visynth.pool.registry import Registry

ROOT = Path(__file__).resolve().parents[1]
DOC_CFG = json.loads((ROOT / "docs" / "examples" / "pool_config.example.json").read_text(encoding="utf-8"))
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
FAKE_KEY = "FAKE-KEY-for-local-mock-0123456789"


# ------------------------------------------------------------------ máy chủ giả


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ThreadingHTTPServer

    def log_message(self, fmt: str, *args: object) -> None:  # im lặng
        pass

    def do_GET(self) -> None:  # noqa: N802
        kind = "gemini" if "/v1beta/" in self.path else "openai_compat"
        self.server.requests.append(  # type: ignore[attr-defined]
            {"kind": kind, "path": self.path, "headers": dict(self.headers), "body": None}
        )
        spec = self.server.responses.get(f"{kind}:get", self.server.responses.get(kind, {"status": 200, "body": {}}))
        payload = json.dumps(spec.get("body") or {}, ensure_ascii=False).encode("utf-8")
        self.send_response(spec.get("status", 200))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = None
        kind = "gemini" if ":generateContent" in self.path else "openai_compat"
        self.server.requests.append(  # type: ignore[attr-defined]
            {"kind": kind, "path": self.path, "headers": dict(self.headers), "body": body}
        )
        spec = self.server.responses.get(kind, {"status": 200, "body": {}})  # type: ignore[attr-defined]
        if spec.get("delay_s"):
            time.sleep(spec["delay_s"])
        payload = spec.get("raw")
        if payload is None:
            payload = json.dumps(spec.get("body") or {}, ensure_ascii=False).encode("utf-8")
        self.send_response(spec.get("status", 200))
        for name, value in (spec.get("headers") or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _header(headers: dict, name: str) -> str | None:
    """Tra header không phân biệt hoa/thường — `urllib` viết hoa chữ đầu khi gửi."""
    return next((v for k, v in headers.items() if k.lower() == name), None)


@dataclass
class MockAPI:
    """Máy chủ giả: ghi lại mọi request, trả phản hồi theo kịch bản đặt trước."""

    base: str
    requests: list = field(default_factory=list)
    responses: dict = field(default_factory=dict)

    def set(self, kind: str, *, status: int = 200, body=None, headers=None, raw=None, delay_s: float = 0.0) -> None:
        self.responses[kind] = {
            "status": status,
            "body": body,
            "headers": headers or {},
            "raw": raw,
            "delay_s": delay_s,
        }

    @property
    def last(self) -> dict:
        return self.requests[-1]


@pytest.fixture
def mock_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []  # type: ignore[attr-defined]
    server.responses = {}  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    api = MockAPI(base=f"http://{host}:{port}", requests=server.requests, responses=server.responses)
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(prompt_id: str = "P4") -> LLMRequest:
    return LLMRequest(
        prompt_id=prompt_id,
        system="SYS",
        user="USER",
        schema=SCHEMA,
        max_output_tokens=256,
        temperature=0.2,
        metadata={"job_id": "j1", "task_id": 1, "profile": "writer"},
    )


def _cfg_for(mock: MockAPI) -> dict:
    """Bản sao `pool_config` mẫu với mọi `base_url` trỏ về máy chủ giả."""
    cfg = json.loads(json.dumps(DOC_CFG))
    for provider in cfg["providers"]:
        provider["base_url"] = f"{mock.base}/v1beta" if provider["kind"] == "gemini_native" else f"{mock.base}/v1"
    return cfg


def _credential_refs(cfg: dict) -> dict[str, str]:
    return {c["id"]: c.get("secret_ref", "") for g in cfg["groups"] for c in g.get("credentials", [])}


def _arm_env(cfg: dict, monkeypatch) -> None:
    for ref in _credential_refs(cfg).values():
        if ref.startswith("env:"):
            monkeypatch.setenv(ref.split(":", 1)[1], FAKE_KEY)


# ------------------------------------------------------------------ định dạng request


def test_gemini_adapter_sends_documented_request_and_parses_reply(mock_api):
    mock_api.set(
        "gemini",
        status=200,
        body={
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"ok": true}'}]}}],
            "usageMetadata": {"promptTokenCount": 21, "candidatesTokenCount": 7},
        },
    )
    adapter = GeminiAdapter(
        base_url=f"{mock_api.base}/v1beta", model_uri="gemini-3.8-flash", deployment_id="gemini-main/flash"
    )
    resp = adapter.complete(_request(), FAKE_KEY)

    sent = mock_api.last
    assert sent["path"] == "/v1beta/models/gemini-3.8-flash:generateContent"
    assert _header(sent["headers"], "x-goog-api-key") == FAKE_KEY
    assert "authorization" not in {k.lower() for k in sent["headers"]}  # Gemini dùng header riêng
    assert sent["body"]["contents"] == [{"role": "user", "parts": [{"text": "USER"}]}]
    assert sent["body"]["systemInstruction"]["parts"][0]["text"] == "SYS"
    gen = sent["body"]["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseJsonSchema"] == SCHEMA
    assert gen["maxOutputTokens"] == 256
    assert gen["temperature"] == 0.2

    assert resp.text == '{"ok": true}'
    assert resp.outcome.kind == "ok"
    assert (resp.outcome.tokens_in, resp.outcome.tokens_out) == (21, 7)
    assert resp.latency_ms >= 0
    assert resp.deployment_id == "gemini-main/flash"


def test_gemini_adapter_omits_schema_when_structured_is_none(mock_api):
    mock_api.set(
        "gemini", status=200, body={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "x"}]}}]}
    )
    adapter = GeminiAdapter(base_url=f"{mock_api.base}/v1beta", model_uri="m", structured="none")
    adapter.complete(_request(), FAKE_KEY)
    assert "responseJsonSchema" not in mock_api.last["body"]["generationConfig"]
    assert "responseMimeType" not in mock_api.last["body"]["generationConfig"]


def test_openai_adapter_sends_documented_request_and_parses_reply(mock_api):
    mock_api.set(
        "openai_compat",
        status=200,
        body={
            "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 33, "completion_tokens": 5},
        },
    )
    adapter = OpenAICompatAdapter(
        base_url=f"{mock_api.base}/v1", model_uri="meta/llama-3.3-70b-instruct", deployment_id="nvidia-trial/chat"
    )
    resp = adapter.complete(_request("P5"), FAKE_KEY)

    sent = mock_api.last
    assert sent["path"] == "/v1/chat/completions"
    assert _header(sent["headers"], "authorization") == f"Bearer {FAKE_KEY}"
    assert [m["role"] for m in sent["body"]["messages"]] == ["system", "user"]
    assert sent["body"]["model"] == "meta/llama-3.3-70b-instruct"
    assert sent["body"]["max_tokens"] == 256
    assert sent["body"]["response_format"]["type"] == "json_schema"
    assert sent["body"]["response_format"]["json_schema"]["name"] == "visynth_P5"
    assert sent["body"]["response_format"]["json_schema"]["strict"] is False

    assert resp.text == '{"ok": true}'
    assert (resp.outcome.tokens_in, resp.outcome.tokens_out) == (33, 5)


# ------------------------------------------------------------------ ánh xạ lỗi thật


GEMINI_DAY = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "Quota exceeded",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"},
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}],
            },
        ],
    }
}
GEMINI_MINUTE = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "Quota exceeded",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3.5s"},
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}],
            },
        ],
    }
}
GEMINI_BAD_KEY = {
    "error": {
        "code": 400,
        "status": "INVALID_ARGUMENT",
        "message": "API key not valid. Please pass a valid API key.",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}],
    }
}

ERROR_CASES = [
    # (kind, status, headers, body, outcome, retry_after)
    ("gemini", 429, {}, GEMINI_DAY, "rate_limited_day", 12.0),
    ("gemini", 429, {}, GEMINI_MINUTE, "rate_limited_minute", 3.5),
    ("gemini", 400, {}, GEMINI_BAD_KEY, "auth_error", None),
    ("gemini", 403, {}, {"error": {"message": "PERMISSION_DENIED"}}, "auth_error", None),
    (
        "gemini",
        404,
        {},
        {"error": {"message": "models/khong-co is not found", "status": "NOT_FOUND"}},
        "bad_request",
        None,
    ),
    ("gemini", 500, {}, {"error": {"message": "internal"}}, "server_error", None),
    (
        "openai_compat",
        429,
        {"Retry-After": "30"},
        {"error": {"message": "insufficient_quota"}},
        "rate_limited_day",
        30.0,
    ),
    (
        "openai_compat",
        429,
        {},
        {"error": {"message": "Rate limit reached for requests per minute (RPM)"}},
        "rate_limited_minute",
        None,
    ),
    (
        "openai_compat",
        400,
        {},
        {"error": {"message": "This model's maximum context length is 8192 tokens"}},
        "context_exceeded",
        None,
    ),
    ("openai_compat", 401, {}, {"error": {"message": "invalid api key"}}, "auth_error", None),
    ("openai_compat", 503, {}, {"error": {"message": "upstream"}}, "server_error", None),
]


@pytest.mark.parametrize("kind,status,headers,body,expected,retry_after", ERROR_CASES)
def test_error_payloads_map_to_expected_outcome(mock_api, kind, status, headers, body, expected, retry_after):
    mock_api.set(kind, status=status, body=body, headers=headers)
    adapter = _adapter_for(mock_api, kind)
    resp = adapter.complete(_request(), FAKE_KEY)
    assert resp.outcome.kind == expected
    if retry_after is not None:
        assert resp.outcome.retry_after_s == retry_after


def _adapter_for(mock_api: MockAPI, kind: str):
    if kind == "gemini":
        return GeminiAdapter(base_url=f"{mock_api.base}/v1beta", model_uri="gemini-3.8-flash")
    return OpenAICompatAdapter(base_url=f"{mock_api.base}/v1", model_uri="example-chat")


def test_message_without_quota_hints_is_unknown_rate_limit(mock_api):
    mock_api.set("openai_compat", status=429, body={"error": {"message": "slow down"}})
    resp = _adapter_for(mock_api, "openai_compat").complete(_request(), FAKE_KEY)
    assert resp.outcome.kind == "rate_limited_unknown"


def test_non_json_body_does_not_crash(mock_api):
    mock_api.set("gemini", status=502, raw=b"<html>bad gateway</html>")
    resp = _adapter_for(mock_api, "gemini").complete(_request(), FAKE_KEY)
    assert resp.outcome.kind == "server_error"
    assert resp.text == ""


def test_empty_200_body_is_invalid_output(mock_api):
    mock_api.set("gemini", status=200, body={"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]})
    resp = _adapter_for(mock_api, "gemini").complete(_request(), FAKE_KEY)
    assert resp.outcome.kind == "invalid_output"


def test_truncated_and_safety_are_not_errors(mock_api):
    mock_api.set(
        "gemini",
        status=200,
        body={"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "x"}]}}]},
    )
    assert _adapter_for(mock_api, "gemini").complete(_request(), FAKE_KEY).outcome.kind == "truncated"
    mock_api.set(
        "openai_compat",
        status=200,
        body={"choices": [{"finish_reason": "content_filter", "message": {"content": "x"}}]},
    )
    assert _adapter_for(mock_api, "openai_compat").complete(_request(), FAKE_KEY).outcome.kind == "safety_blocked"


def test_network_failure_becomes_network_error():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # cổng đóng: mọi kết nối bị từ chối
    adapter = GeminiAdapter(base_url=f"http://127.0.0.1:{port}/v1beta", model_uri="m", timeout_s=1.0)
    with pytest.raises(NetworkError):
        adapter.complete(_request(), FAKE_KEY)


# ------------------------------------------------------------------ probe và client trên HTTP thật


def test_probe_over_http_fills_json_and_tokenizer_factor(mock_api, monkeypatch):
    mock_api.set(
        "gemini",
        status=200,
        body={
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": '{"ok": true, "chao": "Xin chào"}'}]}}
            ],
            "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 12},
        },
    )
    cfg = _cfg_for(mock_api)
    _arm_env(cfg, monkeypatch)
    model = load_pool_model(cfg)
    deployment = model.deployments[0]
    provider = next(p for p in cfg["providers"] if p["id"] == deployment.group.provider)

    run = probe_deployment(
        deployment,
        provider,
        credential_refs=_credential_refs(cfg),
        registry=Registry(),
        with_vi_write=False,
    )

    assert run["errors"] == []
    assert run["json"]["ok"] is True
    assert run["json"]["ladder"][0]["valid"] is True
    assert run["tokenizer_factor"] and run["tokenizer_factor"] > 0
    assert run["tokenizer_source"] == "json"  # đo được ngay cả khi bỏ phần vi_write
    assert run["latency_ms"]
    assert FAKE_KEY not in json.dumps(run, ensure_ascii=False)  # kết quả probe không mang khoá


def test_pooled_client_calls_over_http_and_ledger_has_no_key(mock_api, monkeypatch, tmp_path):
    mock_api.set(
        "gemini",
        status=200,
        body={
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"ok": true}'}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
        },
    )
    mock_api.set(
        "openai_compat",
        status=200,
        body={
            "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )
    cfg = _cfg_for(mock_api)
    _arm_env(cfg, monkeypatch)
    ledger = Ledger(path=tmp_path / "llm_calls.jsonl")
    client = PooledLLMClient.from_config(cfg, ledger=ledger, gate="dev")

    resp = client.complete(_request("P4"))

    assert resp.outcome.kind == "ok"
    assert resp.text == '{"ok": true}'
    assert resp.deployment_id
    assert ledger.rows and ledger.rows[0]["outcome"] == "ok"
    assert ledger.rows[0]["tokens_in"] == 100 and ledger.rows[0]["tokens_out"] == 20
    # khoá chỉ nằm ở header gửi nhà cung cấp; không lọt vào sổ hay phản hồi
    written = (tmp_path / "llm_calls.jsonl").read_text(encoding="utf-8")
    assert FAKE_KEY not in written
    assert FAKE_KEY not in json.dumps(ledger.rows, ensure_ascii=False)
    auth_headers = [_header(r["headers"], "x-goog-api-key") for r in mock_api.requests] + [
        _header(r["headers"], "authorization") for r in mock_api.requests
    ]
    assert any(v == FAKE_KEY or v == f"Bearer {FAKE_KEY}" for v in auth_headers if v)
    assert client.ledger_totals()["calls"] == 1


def test_pooled_client_switches_deployment_on_429(mock_api, monkeypatch, tmp_path):
    """429 ở mọi deployment → client thử lần lượt, ghi sổ từng lần, rồi bỏ cuộc với lỗi rõ ràng."""
    mock_api.set("gemini", status=429, body=GEMINI_MINUTE)
    mock_api.set("openai_compat", status=429, body=GEMINI_MINUTE)
    cfg = _cfg_for(mock_api)
    _arm_env(cfg, monkeypatch)
    clock = [1_790_000_000.0]

    def sleep(seconds: float) -> None:  # cooldown trôi qua thay vì ngủ thật
        clock[0] += seconds

    client = PooledLLMClient.from_config(
        cfg, ledger=Ledger(path=tmp_path / "s.jsonl"), gate="dev", clock=lambda: clock[0], sleep=sleep
    )
    with pytest.raises(LLMError) as excinfo:
        client.complete(_request("P4"))

    assert excinfo.value.outcome.kind in {"rate_limited_minute", "rate_limited_day", "deferred"}
    rows = client.ledger.rows
    assert len({r["deployment_id"] for r in rows}) >= 2  # đã chuyển sang deployment khác
    assert all(r["outcome"].startswith("rate_limited") or r["outcome"] == "deferred" for r in rows)
    assert any(r["retry_after_s"] for r in rows)


# ------------------------------------------------------------------ liệt kê model


def test_list_models_gemini_filters_to_generate_content(mock_api):
    mock_api.responses["gemini:get"] = {
        "status": 200,
        "body": {
            "models": [
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent", "countTokens"]},
            ]
        },
    }
    provider = {"id": "gemini", "kind": "gemini_native", "base_url": f"{mock_api.base}/v1beta"}
    assert list_models(provider, FAKE_KEY) == ["gemini-2.5-pro", "gemini-3.8-flash"]
    sent = mock_api.last
    assert sent["path"].startswith("/v1beta/models")
    assert _header(sent["headers"], "x-goog-api-key") == FAKE_KEY


def test_list_models_openai_shape(mock_api):
    mock_api.responses["openai_compat"] = {
        "status": 200,
        "body": {"data": [{"id": "meta/llama-3.3-70b-instruct"}, {"id": "nvidia/nemotron-3-ultra-550b-a55b"}]},
    }
    provider = {"id": "nvidia", "kind": "openai_compat", "base_url": f"{mock_api.base}/v1"}
    assert list_models(provider, FAKE_KEY) == ["meta/llama-3.3-70b-instruct", "nvidia/nemotron-3-ultra-550b-a55b"]


def test_list_models_bad_key_raises_clear_error(mock_api):
    mock_api.responses["gemini:get"] = {"status": 400, "body": GEMINI_BAD_KEY}
    provider = {"id": "gemini", "kind": "gemini_native", "base_url": f"{mock_api.base}/v1beta"}
    with pytest.raises(LLMError) as excinfo:
        list_models(provider, FAKE_KEY)
    assert excinfo.value.outcome.kind == "auth_error"
    assert FAKE_KEY not in str(excinfo.value)


def test_list_models_network_failure_is_wrapped(mock_api):
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    provider = {"id": "gemini", "kind": "gemini_native", "base_url": f"http://127.0.0.1:{port}/v1beta"}
    with pytest.raises(LLMError) as excinfo:
        list_models(provider, FAKE_KEY, timeout_s=1.0)
    assert excinfo.value.outcome.kind == "network_error"
