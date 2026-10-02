"""Hợp đồng LLM client: bản giả và phân loại HTTP (đối chiếu với bộ đặc tả)."""

from __future__ import annotations

import pytest
from reference.llm_pool import classify_http as spec_classify

from visynth.llm import FakeLLMClient, FakeReply, LLMError, LLMRequest, classify_http, scripted


def req(pid: str = "P0", text: str = "xin chào thế giới") -> LLMRequest:
    return LLMRequest(prompt_id=pid, system="system", user=text, schema={"type": "object"})


# ------------------------------------------------------------------ bản giả


def test_fake_records_calls_and_returns_parsed_json():
    client = scripted({"P0": [{"language_code": "vi", "doc_type": "book"}]})
    resp = client.complete(req())
    assert resp.parsed == {"language_code": "vi", "doc_type": "book"}
    assert resp.ok and resp.outcome.kind == "ok"
    assert len(client.calls) == 1 and client.calls[0].prompt_id == "P0"
    assert client.calls_of["P0"][0].user == "xin chào thế giới"


def test_fake_raises_llm_error_with_retryable_flag():
    client = scripted({"P4": [FakeReply.error("rate_limited_minute", retry_after_s=12.0)]})
    with pytest.raises(LLMError) as excinfo:
        client.complete(req("P4"))
    assert excinfo.value.retryable is True
    assert excinfo.value.outcome.retry_after_s == 12.0

    client2 = scripted({"P4": [FakeReply.error("safety_blocked")]})
    with pytest.raises(LLMError) as excinfo2:
        client2.complete(req("P4"))
    assert excinfo2.value.retryable is False


def test_fake_uses_default_when_script_exhausted():
    client = FakeLLMClient(default=FakeReply(text="mặc định"))
    assert client.complete(req()).text == "mặc định"
    assert client.complete(req()).text == "mặc định"


def test_fake_handler_can_react_to_request():
    def handler(r: LLMRequest) -> FakeReply:
        return FakeReply.json({"echo": r.user[:4]})

    client = FakeLLMClient(handler=handler)
    assert client.complete(req(text="abcd")).parsed == {"echo": "abcd"}


# ------------------------------------------------------------------ phân loại HTTP (đối chiếu spec)

CASES = [
    {"kind": "openai_compat", "status": 200},
    {"kind": "openai_compat", "status": 200, "finish_reason": "length"},
    {"kind": "openai_compat", "status": 200, "finish_reason": "content_filter"},
    {"kind": "openai_compat", "status": 401},
    {"kind": "openai_compat", "status": 403},
    {
        "kind": "openai_compat",
        "status": 429,
        "headers": {"Retry-After": "12"},
        "body": {"error": {"message": "Rate limit reached per minute"}},
    },
    {"kind": "openai_compat", "status": 429, "body": {"error": {"message": "insufficient_quota"}}},
    {"kind": "openai_compat", "status": 429, "body": {"error": {"message": "slow down"}}},
    {
        "kind": "gemini_native",
        "status": 429,
        "body": {
            "error": {
                "details": [
                    {"retryDelay": "34s"},
                    {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]},
                ]
            }
        },
    },
    {
        "kind": "gemini_native",
        "status": 429,
        "body": {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerMinute"}]}]}},
    },
    {"kind": "openai_compat", "status": 400, "body": {"error": {"message": "context length exceeded"}}},
    {"kind": "openai_compat", "status": 413},
    {"kind": "openai_compat", "status": 404},
    {"kind": "openai_compat", "status": 408},
    {"kind": "openai_compat", "status": 504},
    {"kind": "openai_compat", "status": 500, "headers": {"retry-after": "3"}},
    {"kind": "openai_compat", "status": 418},
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['kind']}-{c['status']}-{c.get('finish_reason', '')}")
def test_classify_http_matches_spec_reference(case):
    ours = classify_http(**case)
    spec = spec_classify(**case)
    assert ours.kind == spec.kind
    assert ours.retry_after_s == spec.retry_after_s
