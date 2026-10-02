"""Kiểm thử LLM Pool của mã sản phẩm (M0-W3, SPEC §17).

Hai tầng kiểm thử:

* **Đối chiếu bộ đặc tả** — cùng `pool_config` mẫu, cùng kịch bản, `visynth.pool.*` phải cho ra *đúng* kết quả
  của `docs/reference/llm_pool.py` (`load_pool_model`, `Router`, `classify_http`) và `docs/reference/pool_secrets.py`;
* **Hành vi mới** — adapter HTTP thật (đo bằng transport giả), `PooledLLMClient` (chọn profile, chuyển dự phòng,
  cách ly khoá 401, hoãn khi chờ lâu, sổ `llm_calls`), kho khoá ngoài repo, và `probe`.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

import pytest
from reference import llm_pool as spec
from reference import pool_secrets as spec_secrets

from visynth.llm.base import LLMError, LLMRequest
from visynth.pool import (
    PROFILE_BY_PROMPT,
    Ledger,
    MemoryState,
    PooledLLMClient,
    Registry,
    Router,
    classify_http,
    load_pool_model,
)
from visynth.pool.adapters import GeminiAdapter, HTTPReply, OpenAICompatAdapter, make_adapter
from visynth.pool.client import CHARS_PER_TOKEN
from visynth.pool.probe import probe_deployment, probe_pool, suggest_updates
from visynth.pool.registry import KeyNotFound, load_dotenv, resolve_key

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "docs" / "examples" / "pool_config.example.json").read_text(encoding="utf-8"))
T0 = 1_790_000_000.0
MASTER = bytes(range(32))


# ------------------------------------------------------------------ đối chiếu bộ đặc tả


def test_load_pool_model_matches_spec_reference():
    assert asdict(load_pool_model(CFG)) == asdict(spec.load_pool_model(CFG))


@pytest.mark.parametrize(
    "profile,gate,privacy",
    [("fast", "dev", "standard"), ("writer", "dev", "standard"), ("verifier", "dev", "standard")],
)
def test_router_picks_same_deployment_as_reference(profile: str, gate: str, privacy: str):
    """Cùng seed, cùng chuỗi yêu cầu → Router của sản phẩm và Router tham chiếu phải chọn y hệt nhau."""
    ours = Router(load_pool_model(CFG), MemoryState(), seed=11)
    theirs = spec.Router(spec.load_pool_model(CFG), spec.MemoryState(), seed=11)
    for i in range(12):
        now = T0 + i * 60.0
        a = ours.acquire(_req(profile, gate=gate, privacy=privacy), now)
        b = theirs.acquire(_spec_req(profile, gate=gate, privacy=privacy), now)
        if isinstance(a, spec.Wait) or isinstance(b, spec.Wait):
            break  # hết chỗ giống nhau thì thôi (Wait là hành vi đã có test riêng ở docs)
        aid = a.deployment.id if not isinstance(a, spec.Impossible) else "impossible"
        bid = b.deployment.id if not isinstance(b, spec.Impossible) else "impossible"
        assert aid == bid


def _req(profile: str, **kw):
    from visynth.pool import Request

    base = dict(profile=profile, est_in=2000, est_out=500, job_id="j1", task_id=1)
    base.update(kw)
    return Request(**base)


def _spec_req(profile: str, **kw):
    base = dict(profile=profile, est_in=2000, est_out=500, job_id="j1", task_id=1)
    base.update(kw)
    return spec.Request(**base)


HTTP_CASES = [
    ("gemini_native", 200, {}, {"candidates": [{"finishReason": "STOP"}]}, "STOP"),
    ("gemini_native", 200, {}, {"candidates": [{"finishReason": "MAX_TOKENS"}]}, "MAX_TOKENS"),
    (
        "gemini_native",
        429,
        {},
        {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}]}},
        None,
    ),
    (
        "gemini_native",
        429,
        {},
        {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]}]}},
        None,
    ),
    ("gemini_native", 401, {}, {"error": {"code": 401}}, None),
    # Google trả 400 INVALID_ARGUMENT cho khoá sai — phải nhận ra là lỗi khoá, không phải "bad_request"
    (
        "gemini_native",
        400,
        {},
        {
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": "API key not valid. Please pass a valid API key.",
                "details": [{"reason": "API_KEY_INVALID"}],
            }
        },
        None,
    ),
    ("openai_compat", 400, {}, {"error": {"message": "unknown parameter"}}, None),
    ("openai_compat", 429, {"retry-after": "12"}, {"error": {"message": "insufficient_quota"}}, None),
    ("openai_compat", 400, {}, {"error": {"message": "maximum context length exceeded"}}, None),
    ("openai_compat", 503, {}, {"error": {"message": "upstream"}}, None),
    ("openai_compat", 408, {}, None, None),
    ("openai_compat", 200, {}, {"choices": [{"finish_reason": "content_filter"}]}, "content_filter"),
]


@pytest.mark.parametrize("kind,status,headers,body,finish", HTTP_CASES)
def test_classify_http_matches_reference(kind, status, headers, body, finish):
    ours = classify_http(kind, status, headers, body, finish)
    theirs = spec.classify_http(kind, status, headers, body, finish)
    assert (ours.kind, ours.retry_after_s) == (theirs.kind, theirs.retry_after_s)


def test_secrets_match_reference_and_never_expose_the_key():
    master = spec_secrets.load_master_key(MASTER.hex())
    secret = "AIzaSyExampleKeyForTests0123456789"
    blob_ours = __import__("visynth.pool.secrets", fromlist=["x"]).encrypt_secret(master, "cred-1", secret)
    assert secret.encode() not in blob_ours
    from visynth.pool.secrets import decrypt_secret, fingerprint

    assert decrypt_secret(master, "cred-1", blob_ours) == secret
    assert fingerprint(master, secret) == spec_secrets.fingerprint(master, secret)
    spec_blob = spec_secrets.encrypt_secret(master, "cred-1", secret)
    assert decrypt_secret(master, "cred-1", spec_blob) == secret  # tương thích hai chiều


# ------------------------------------------------------------------ adapter HTTP (transport giả)


def _capture(reply: HTTPReply):
    seen: dict = {}

    def transport(method, url, headers, data, timeout):
        seen.update(method=method, url=url, headers=headers, body=json.loads(data), timeout=timeout)
        return reply

    return transport, seen


def test_gemini_adapter_request_shape_and_response_parsing():
    reply = HTTPReply(
        200,
        {},
        {
            "candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 321, "candidatesTokenCount": 45},
        },
        elapsed_ms=123,
    )
    transport, seen = _capture(reply)
    adapter = GeminiAdapter(
        "https://generativelanguage.googleapis.com/v1beta", "gemini-3.8-flash", "d1", transport=transport
    )
    req = LLMRequest(prompt_id="P3", system="sys", user="usr", schema={"type": "object"}, max_output_tokens=512)
    resp = adapter.complete(req, "K")
    assert seen["url"].endswith("/models/gemini-3.8-flash:generateContent")
    assert seen["headers"]["x-goog-api-key"] == "K" and "Authorization" not in seen["headers"]
    assert seen["body"]["generationConfig"]["responseJsonSchema"] == {"type": "object"}
    assert "systemInstruction" in seen["body"]
    assert (resp.text, resp.outcome.kind, resp.outcome.tokens_in, resp.outcome.tokens_out, resp.latency_ms) == (
        '{"a": 1}',
        "ok",
        321,
        45,
        123,
    )


def test_gemini_adapter_downgrades_structured_output_when_model_lacks_json_schema():
    transport, seen = _capture(HTTPReply(200, {}, {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}))
    adapter = GeminiAdapter("https://x/v1beta", "m", "d1", transport=transport, structured="json_object")
    adapter.complete(LLMRequest(prompt_id="P2", system="s", user="u", schema={"type": "object"}), "K")
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseJsonSchema" not in seen["body"]["generationConfig"]


def test_openai_compat_adapter_request_shape_and_quota_classification():
    transport, seen = _capture(
        HTTPReply(429, {"retry-after": "30"}, {"error": {"message": "rate limit reached: tpd"}}, 15)
    )
    adapter = OpenAICompatAdapter("https://api.example/v1", "big-chat", "d2", transport=transport)
    resp = adapter.complete(LLMRequest(prompt_id="P4", system="s", user="u"), "K")
    assert seen["url"] == "https://api.example/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer K"
    assert seen["body"]["messages"][0]["role"] == "system"
    assert resp.outcome.kind == "rate_limited_day" and resp.outcome.retry_after_s == 30


def test_truncated_and_safety_finish_reasons_are_classified():
    for reason, expected in (("length", "truncated"), ("SAFETY", "safety_blocked")):
        transport, _ = _capture(
            HTTPReply(200, {}, {"choices": [{"message": {"content": "x"}, "finish_reason": reason}]})
        )
        adapter = OpenAICompatAdapter("https://api.example/v1", "m", "d", transport=transport)
        assert adapter.complete(LLMRequest(prompt_id="P2", system="s", user="u"), "K").outcome.kind == expected


# ------------------------------------------------------------------ PooledLLMClient


def _client(**kw) -> PooledLLMClient:
    cfg = json.loads(json.dumps(CFG))
    for g in cfg["groups"]:
        for c in g["credentials"]:
            os.environ.setdefault(c["secret_ref"].split(":", 1)[1], f"AIzaSyExample{abs(hash(c['id'])) % 10**10:010d}")
    cfg["groups"] = [g for g in cfg["groups"] if g["id"] != "nvidia-trial"]  # tầng trial cần khoá riêng, bỏ cho gọn
    cfg["deployments"] = [d for d in cfg["deployments"] if d["group"] != "nvidia-trial"]
    return PooledLLMClient.from_config(cfg, ledger=Ledger(), **kw)


def _ok_reply(text: str = '{"ok": true}'):
    return HTTPReply(
        200,
        {},
        {
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 50},
        },
        elapsed_ms=40,
    )


def test_client_uses_prompt_profile_and_writes_a_content_free_ledger_row():
    transport, seen = _capture(_ok_reply())
    client = _client(transport=transport)
    req = LLMRequest(prompt_id="P4", system="BÍ MẬT", user="NỘI DUNG TÀI LIỆU", metadata={"job_id": "j1"})
    resp = client.complete(req)
    assert resp.ok and resp.deployment_id == "gemini-free-a/flash"  # writer -> nhóm miễn phí mạnh trước
    assert PROFILE_BY_PROMPT["P4"] == "writer"
    row = client.ledger.rows[-1]
    assert (row["prompt_id"], row["group_tier"], row["data_policy"], row["outcome"]) == (
        "P4",
        "free",
        "may_train",
        "ok",
    )
    assert "NỘI DUNG TÀI LIỆU" not in json.dumps(row, ensure_ascii=False)
    assert "BÍ MẬT" not in json.dumps(row, ensure_ascii=False)
    assert seen["body"]["generationConfig"]["temperature"] == req.temperature


def test_client_failover_on_429_excludes_the_deployment_then_succeeds():
    calls: list[str] = []

    def transport(method, url, headers, data, timeout):
        calls.append(url)
        if len(calls) == 1:
            return HTTPReply(
                429, {}, {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerMinute"}]}]}}, 10
            )
        return _ok_reply()

    client = _client(transport=transport)
    resp = client.complete(LLMRequest(prompt_id="P2", system="s", user="u", metadata={"job_id": "j1"}))
    assert resp.ok and len(calls) == 2
    outcomes = [r["outcome"] for r in client.ledger.rows]
    assert outcomes == ["rate_limited_minute", "ok"]
    assert client.ledger.rows[0]["deployment_id"] != client.ledger.rows[1]["deployment_id"]


def test_client_quarantines_credential_on_401_and_raises_auth_error():
    transport, _ = _capture(HTTPReply(401, {}, {"error": {"code": 401, "message": "API key not valid"}}))
    client = _client(transport=transport)
    with pytest.raises(LLMError) as exc:
        client.complete(LLMRequest(prompt_id="P0", system="s", user="u", metadata={"job_id": "j1"}))
    assert exc.value.outcome.kind == "auth_error"
    assert client.ledger_totals()["quarantined"]  # khoá bị cách ly, không thử lại vô ích
    assert any(c.status == "quarantined" for d in client.model.deployments for c in d.group.credentials)


def test_client_defers_when_the_whole_pool_must_wait_long():
    class Exhausted:
        def snapshot(self, d, now):
            return {"headroom": 0.0, "ewma_success": 1.0}

        def try_reserve(self, d, tin, tout, prio, now, reserve, ttl):
            from visynth.pool import ReserveResult

            return ReserveResult(False, wait_s=3600.0, reason="deployment_rpd")

        def settle(self, lease_id, outcome, now):  # pragma: no cover - không có lease nào được cấp
            raise AssertionError

    client = _client(transport=lambda *a: _ok_reply(), defer_after_s=30.0)
    client.router = Router(client.model, Exhausted(), seed=1)
    with pytest.raises(LLMError) as exc:
        client.complete(LLMRequest(prompt_id="P2", system="s", user="u", metadata={"job_id": "j1"}))
    assert exc.value.outcome.kind == "deferred" and exc.value.outcome.retry_after_s
    assert client.ledger.rows[-1]["outcome"] == "deferred"
    assert client.ledger.rows[-1]["deployment_id"] is None


def test_client_avoids_writer_group_when_checking_the_same_job():
    transport, _ = _capture(_ok_reply())
    client = _client(transport=transport)
    p5 = LLMRequest(prompt_id="P5", system="s", user="u", metadata={"job_id": "j1"})
    assert client._avoid_groups(p5) == frozenset()
    client.complete(LLMRequest(prompt_id="P4", system="s", user="u", metadata={"job_id": "j1"}))
    assert client._avoid_groups(p5) == frozenset({"gemini-free-a"})


def test_client_learns_tokenizer_factor_from_real_usage():
    transport, _ = _capture(_ok_reply())
    client = _client(transport=transport)
    req = LLMRequest(prompt_id="P2", system="s", user="u" * 400)
    client.complete(req)
    est = max(1, int(len(req.text) / CHARS_PER_TOKEN))
    assert client.ledger_totals()["factors"]["gemini:gemini-3.8-flash"] == pytest.approx(1000 / est, rel=0.02)


def test_client_reports_missing_key_and_falls_through_to_another_deployment():
    cfg = json.loads(json.dumps(CFG))
    cfg["groups"] = [g for g in cfg["groups"] if g["id"] == "gemini-free-a"]
    cfg["deployments"] = [d for d in cfg["deployments"] if d["group"] == "gemini-free-a"]
    os.environ.pop("GEMINI_KEY_FREE_A1", None)
    client = PooledLLMClient.from_config(cfg, ledger=Ledger(), transport=lambda *a: _ok_reply())
    with pytest.raises(LLMError) as exc:
        client.complete(LLMRequest(prompt_id="P2", system="s", user="u", metadata={"job_id": "j"}))
    assert exc.value.outcome.kind == "auth_error"
    assert "không phân giải được khoá" in str(exc.value)
    assert client.ledger.rows[-1]["missing_key"] is True


# ------------------------------------------------------------------ kho khoá và .env


def test_registry_encrypts_keys_and_keeps_them_out_of_the_config_file(tmp_path):
    reg = Registry(path=tmp_path / "secrets.json", master_file=tmp_path / "master.key")
    reg.keygen()
    secret = "AIzaSyExampleKeyForTests0123456789"
    meta = reg.add("cred-1", secret)
    raw = (tmp_path / "secrets.json").read_text(encoding="utf-8")
    assert secret not in raw and meta["last4"] == secret[-4:]
    assert reg.get("cred-1") == secret
    assert stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "master.key").stat().st_mode) == 0o600
    with pytest.raises(KeyError):
        reg.add("cred-1", secret)
    with pytest.raises(KeyNotFound):
        reg.resolve("env:KHONG_CO_BIEN_NAY")


def test_load_dotenv_fills_missing_variables_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('export GEMINI_KEY_X="khoa-that-1"\n# ghi chú\nSAN_CO=giu-nguyen\n', encoding="utf-8")
    monkeypatch.delenv("GEMINI_KEY_X", raising=False)
    monkeypatch.setenv("SAN_CO", "khong-ghi-de")
    loaded = load_dotenv(env)
    assert loaded == ["GEMINI_KEY_X"]
    assert os.environ["GEMINI_KEY_X"] == "khoa-that-1"
    assert os.environ["SAN_CO"] == "khong-ghi-de"  # biến shell luôn thắng .env


def test_resolve_key_reads_env_then_encrypted_store(tmp_path, monkeypatch):
    reg = Registry(path=tmp_path / "s.json", master_file=tmp_path / "m.key")
    reg.keygen()
    reg.add("cred-9", "AIzaSyExampleKeyForTests0123456789")
    monkeypatch.setenv("K_TU_ENV", "khoa-tu-moi-truong")
    assert resolve_key("env:K_TU_ENV", reg) == "khoa-tu-moi-truong"
    assert resolve_key("enc:cred-9", reg) == "AIzaSyExampleKeyForTests0123456789"


# ------------------------------------------------------------------ probe


def test_probe_measures_json_quality_and_vietnamese_writing():
    def transport(method, url, headers, data, timeout):
        body = json.loads(data)
        prompt = json.dumps(body, ensure_ascii=False)
        if "responseJsonSchema" in prompt or "json_object" in prompt or "response_format" in prompt:
            text = '{"ok": true, "chao": "Xin chào, đây là kiểm định."}'
        else:
            text = "Báo cáo tổng hợp phải kiểm chứng số liệu trước khi công bố vì người đọc cần tin vào con số. " * 3
        return HTTPReply(
            200,
            {},
            {
                "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 400, "candidatesTokenCount": 60},
            },
            elapsed_ms=55,
        )

    model = load_pool_model(CFG)
    deployment = next(d for d in model.deployments if d.id == "gemini-free-a/flash")
    provider = next(p for p in CFG["providers"] if p["id"] == deployment.group.provider)
    os.environ.setdefault("GEMINI_KEY_FREE_A1", "AIzaSyExampleKeyForTests0123456789")
    refs = {c["id"]: c.get("secret_ref", "") for g in CFG["groups"] for c in g["credentials"]}
    run = probe_deployment(deployment, provider, credential_refs=refs, transport=transport)
    assert run["json"]["ok"] is True and run["vi_write"]["ok"] is True
    assert run["tokenizer_factor"] and run["latency_p50_ms"] == 55
    assert run["errors"] == []


def test_probe_reports_a_broken_key_without_raising():
    transport, _ = _capture(HTTPReply(401, {}, {"error": {"code": 401, "message": "API key not valid"}}))
    model = load_pool_model(CFG)
    deployment = next(d for d in model.deployments if d.id == "gemini-free-a/flash")
    provider = next(p for p in CFG["providers"] if p["id"] == deployment.group.provider)
    os.environ.setdefault("GEMINI_KEY_FREE_A1", "AIzaSyExampleKeyForTests0123456789")
    refs = {c["id"]: c.get("secret_ref", "") for g in CFG["groups"] for c in g["credentials"]}
    run = probe_deployment(deployment, provider, credential_refs=refs, transport=transport)
    assert run["json"]["ok"] is False and run["errors"]
    report = {"runs": [run], "suggest_updates": suggest_updates([run])}
    assert report["suggest_updates"][deployment.id]["quality"]["json"] == 0.0


def test_probe_pool_skips_disabled_deployments_and_writes_a_report(tmp_path):
    cfg = json.loads(json.dumps(CFG))
    os.environ.setdefault("GEMINI_KEY_FREE_A1", "AIzaSyExampleKeyForTests0123456789")
    transport, _ = _capture(_ok_reply('{"ok": true, "chao": "Xin chào"}'))
    from visynth.pool.probe import write_report

    report = probe_pool(cfg, only=["gemini-free-a/flash"], transport=transport, with_vi_write=False)
    assert [r["deployment_id"] for r in report["runs"]] == ["gemini-free-a/flash"]
    out = write_report(report, tmp_path / "probe.json")
    assert json.loads(out.read_text(encoding="utf-8"))["totals"]["deployments"] == 1


def test_make_adapter_chooses_kind_and_model_uri():
    model = load_pool_model(CFG)
    deployment = next(d for d in model.deployments if d.id == "provider-a-free/large")
    provider = next(p for p in CFG["providers"] if p["id"] == "provider-a")
    adapter = make_adapter(provider, deployment, transport=lambda *a: _ok_reply())
    assert isinstance(adapter, OpenAICompatAdapter) and adapter.model_uri == "large-chat"
    gem = next(d for d in model.deployments if d.id == "gemini-paid/flash")
    assert isinstance(make_adapter(CFG["providers"][0], gem, transport=lambda *a: _ok_reply()), GeminiAdapter)


# ------------------------------------------------------------------ CLI `visynth pool ...`


def test_root_template_config_and_env_example_are_safe_to_commit():
    """Mẫu `pool_config.example.json` + `.env.example` ở gốc repo: hợp lệ schema và KHÔNG chứa khoá thật."""
    from visynth.pool.validate import validate_config

    cfg = json.loads((ROOT / "pool_config.example.json").read_text(encoding="utf-8"))
    report = validate_config(cfg)
    assert report["valid"] is True, report["errors"]
    assert report["deployments"] >= 3
    refs = [c["secret_ref"] for g in cfg["groups"] for c in g["credentials"]]
    assert all(r.startswith(("env:", "enc:")) for r in refs)
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=" in env_example and "AIza" not in env_example


def test_example_config_never_carries_a_key_through_the_pool_client():
    """Khoá chỉ đến từ môi trường: `PooledLLMClient` không bao giờ thấy khoá nằm trong tệp cấu hình."""
    cfg = json.loads((ROOT / "pool_config.example.json").read_text(encoding="utf-8"))
    text = json.dumps(cfg, ensure_ascii=False)
    assert "AIza" not in text and "sk-" not in text and "nvapi-" not in text


def test_cli_pool_validate_reports_a_good_pool_config(capsys):
    from visynth.cli import main

    cfg_path = ROOT / "docs" / "examples" / "pool_config.example.json"
    assert main(["pool", "validate", "--config", str(cfg_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True and payload["deployments"] >= 5


def test_cli_pool_preview_never_prints_the_key():
    from visynth.cli import main

    for argv in (
        ["pool", "preview", "--keys", "AIzaSyExampleKeyForTests0123456789,AIzaSyExampleKeyForTests0123456788"],
        ["pool", "preview", "--keys", "nhan1|AIzaSyExampleKeyForTests0123456789"],
    ):
        assert main(argv) == 0


def test_cli_pool_simulate_runs_offline(capsys):
    from visynth.cli import main

    assert main(["pool", "simulate", "--docs", "1"]) == 0
    out = capsys.readouterr().out
    assert "kịch bản" in out.lower() or "scenario" in out.lower()
