"""Kiểm thử LLM Pool (reference/llm_pool.py): token-bucket, hạn mức ngày, cooldown, circuit breaker,
riêng tư/ToS/cổng triển khai, chuyển tầng, đa dạng hoá, phân loại lỗi, dung lượng.
Cùng ngữ nghĩa được so khớp với hàm SQL trong tests/pg_smoke.py."""
import collections
import json
import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from reference.llm_pool import (  # noqa: E402
    Credential, Deployment, Group, Impossible, Lease, Limits, MemoryState, Model, Needs, Outcome, Policy, PoolModel,
    Profile, Request, Router, Tier, Wait, classify_http, deployment_daily_capacity, load_pool_model, next_day_boundary,
)

CFG = json.loads((ROOT / "examples" / "pool_config.example.json").read_text(encoding="utf-8"))
T0 = 1_790_000_000.0  # một thời điểm cố định


def mk_dep(did="d1", gid="g1", tier="free", rpm=10, tpm=None, rpd=None, tpd=None, conc=100, basis="total", policy="may_train",
           tz="UTC", margin=0.85, day_margin=0.95, creds=("c1",), flags=(), gates=("dev", "A", "B", "C"), price="free", tags=("free",)):
    g = Group(gid, "p", tier, policy, tz, frozenset(flags), frozenset(gates), margin, day_margin, Limits(),
              [Credential(c) for c in creds])
    return Deployment(did, g, Model(f"p:{did}", "p", ctx_in=100_000, max_out=8_000, structured="json_object"),
                      Limits(rpm, tpm, rpd, tpd, conc), basis, price, 1.0, frozenset(tags))


def res(st, dep, tin=1000, tout=500, prio="normal", now=T0):
    return st.try_reserve(dep, tin, tout, prio, now)


# --------------------------------------------------------------------------- token bucket
def test_rpm_bucket_has_burst_then_refills():
    st, d = MemoryState(), mk_dep(rpm=10)  # cap = 8.5
    ok = [res(st, d, now=T0).ok for _ in range(8)]
    assert all(ok)
    r = res(st, d, now=T0)
    assert not r.ok and r.reason == "deployment_rpm"
    assert r.wait_s == pytest.approx(0.5 / (8.5 / 60), rel=1e-6)
    assert res(st, d, now=T0 + r.wait_s + 0.01).ok


def test_rpm_floor_prevents_deadlock_for_tiny_limits():
    st, d = MemoryState(), mk_dep(rpm=1)  # 1 * 0.85 < 1 -> sàn 1.0
    assert res(st, d).ok
    r = res(st, d)
    assert not r.ok and r.wait_s == pytest.approx(60.0, rel=1e-6)


def test_tpm_input_basis_counts_only_input_tokens():
    st, d = MemoryState(), mk_dep(rpm=None, tpm=100_000, basis="input")  # cap 85k
    assert res(st, d, tin=80_000, tout=60_000).ok  # tout không tính
    r = res(st, d, tin=10_000, tout=0)
    assert not r.ok and r.reason == "deployment_tpm"
    assert r.wait_s == pytest.approx((10_000 - 5_000) / (85_000 / 60), rel=1e-6)


def test_request_larger_than_bucket_is_too_large_not_wait():
    st, d = MemoryState(), mk_dep(rpm=None, tpm=10_000, basis="input")
    r = res(st, d, tin=9_000)
    assert not r.ok and r.reason == "too_large" and math.isinf(r.wait_s)


def test_settle_refunds_unused_reserved_tokens():
    st, d = MemoryState(), mk_dep(rpm=None, tpm=100_000, basis="total")  # cap 85k
    r = res(st, d, tin=40_000, tout=40_000)
    assert r.ok
    st.settle(r.lease_id, Outcome("ok", tokens_in=10_000, tokens_out=5_000, latency_ms=900), T0)
    snap = st.snapshot(d, T0)
    assert snap["headroom"] == pytest.approx((85_000 - 15_000) / 85_000, rel=1e-6)
    assert snap["inflight"] == 0


# --------------------------------------------------------------------------- hạn mức ngày
def test_daily_quota_resets_at_provider_midnight_not_utc():
    tz = "America/Los_Angeles"
    st, d = MemoryState(), mk_dep(rpm=None, rpd=10, tz=tz, day_margin=0.95)  # lim = 9
    for _ in range(9):
        r = res(st, d)
        assert r.ok
        st.settle(r.lease_id, Outcome("ok", tokens_in=10, tokens_out=10), T0)
    r = res(st, d)
    assert not r.ok and r.reason == "deployment_rpd"
    assert r.wait_s == pytest.approx(next_day_boundary(T0, tz) - T0, abs=1e-6)
    assert res(st, d, now=T0 + r.wait_s + 1).ok  # sang ngày mới: bộ đếm về 0


def test_low_priority_cannot_use_reserved_share():
    st, d = MemoryState(), mk_dep(rpm=None, rpd=100, day_margin=1.0)  # lim 100, low chỉ được 80
    for i in range(80):
        r = st.try_reserve(d, 10, 10, "low", T0)
        assert r.ok, i
        st.settle(r.lease_id, Outcome("ok", tokens_in=10, tokens_out=10), T0)
    assert not st.try_reserve(d, 10, 10, "low", T0).ok
    assert st.try_reserve(d, 10, 10, "normal", T0).ok  # phần dành riêng vẫn còn cho tác vụ thường/cao


# --------------------------------------------------------------------------- đồng thời, lease
def test_concurrency_limit_and_settle_frees_slot():
    st, d = MemoryState(), mk_dep(rpm=None, conc=2)
    a, b = res(st, d), res(st, d)
    assert a.ok and b.ok
    c = res(st, d)
    assert not c.ok and c.reason == "deployment_concurrency"
    st.settle(a.lease_id, Outcome("ok", tokens_in=1, tokens_out=1), T0)
    assert res(st, d).ok


def test_expired_leases_are_reaped():
    st, d = MemoryState(), mk_dep(rpm=None, conc=1)
    assert st.try_reserve(d, 10, 10, "normal", T0, ttl=60).ok
    assert not res(st, d, now=T0 + 30).ok
    assert st.reap(T0 + 61) == 1
    assert res(st, d, now=T0 + 61).ok


# --------------------------------------------------------------------------- 429, cooldown, circuit breaker
def test_429_minute_sets_cooldown_and_shrinks_limit_then_recovers():
    st, d = MemoryState(), mk_dep(rpm=10)
    r = res(st, d)
    st.settle(r.lease_id, Outcome("rate_limited_minute", retry_after_s=20.0), T0)
    blocked = res(st, d, now=T0 + 5)
    assert not blocked.ok and blocked.reason == "deployment_cooldown"
    assert blocked.wait_s == pytest.approx(15.0, abs=1e-6)
    assert st._scope("deployment", "d1").limit_scale == pytest.approx(0.85)
    t = T0 + 21
    for _ in range(6):  # thành công liên tiếp: giới hạn hiệu dụng hồi dần về 1.0
        r = res(st, d, now=t)
        assert r.ok
        st.settle(r.lease_id, Outcome("ok", tokens_in=1, tokens_out=1), t)
        t += 8
    assert st._scope("deployment", "d1").limit_scale == pytest.approx(1.0)


def test_429_day_blocks_until_after_provider_midnight():
    tz = "America/Los_Angeles"
    st, d = MemoryState(), mk_dep(rpm=None, tz=tz)
    r = res(st, d)
    st.settle(r.lease_id, Outcome("rate_limited_day"), T0)
    b = res(st, d, now=T0 + 3600)
    assert not b.ok and b.wait_s == pytest.approx(next_day_boundary(T0, tz) + 30 - (T0 + 3600), abs=1e-6)


def test_429_with_account_scope_cools_down_every_deployment_in_group():
    st = MemoryState()
    a, b = mk_dep("a", "g1"), mk_dep("b", "g1")
    b = Deployment("b", a.group, b.model, b.limits, "total", "free", 1.0, frozenset({"free"}))
    r = res(st, a)
    st.settle(r.lease_id, Outcome("rate_limited_unknown", scope="group"), T0)
    assert not res(st, b, now=T0 + 1).ok and res(st, b, now=T0 + 1).reason == "group_cooldown"


def test_circuit_opens_after_three_failures_then_half_open_probe():
    st, d = MemoryState(), mk_dep(rpm=None)
    for i in range(3):
        r = res(st, d, now=T0 + i)
        st.settle(r.lease_id, Outcome("server_error"), T0 + i)
    s = st._scope("deployment", "d1")
    assert s.circuit == "open" and s.cooldown_until == pytest.approx(T0 + 2 + 15.0)
    assert not res(st, d, now=T0 + 5).ok
    p = res(st, d, now=T0 + 18)  # hết cooldown -> half_open, đúng MỘT lời gọi thăm dò
    assert p.ok and s.circuit == "half_open"
    assert res(st, d, now=T0 + 18).reason == "probing"
    st.settle(p.lease_id, Outcome("server_error"), T0 + 19)  # thăm dò thất bại -> mở lại, backoff gấp đôi
    assert s.circuit == "open" and s.cooldown_until == pytest.approx(T0 + 19 + 30.0)
    p2 = res(st, d, now=T0 + 50)
    assert p2.ok
    st.settle(p2.lease_id, Outcome("ok", tokens_in=1, tokens_out=1), T0 + 51)
    assert s.circuit == "closed" and s.consecutive_failures == 0


def test_non_health_outcomes_do_not_trip_circuit():
    st, d = MemoryState(), mk_dep(rpm=None)
    for k in ("bad_request", "context_exceeded", "safety_blocked", "truncated", "invalid_output", "canceled"):
        r = res(st, d)
        st.settle(r.lease_id, Outcome(k), T0)
    s = st._scope("deployment", "d1")
    assert s.circuit == "closed" and s.consecutive_failures == 0 and s.inflight == 0


def test_auth_error_quarantines_credential_and_removes_deployment():
    st, d = MemoryState(), mk_dep(rpm=None, creds=("c1",))
    r = res(st, d)
    st.settle(r.lease_id, Outcome("auth_error"), T0)
    assert d.group.credentials[0].status == "quarantined"
    x = res(st, d)
    assert not x.ok and x.reason == "no_credential"


def test_credentials_rotate_least_recently_used():
    st, d = MemoryState(), mk_dep(rpm=None, creds=("c1", "c2", "c3"), conc=10)
    used = []
    for i in range(6):
        r = res(st, d, now=T0 + i)
        used.append(r.credential_id)
        st.settle(r.lease_id, Outcome("ok", tokens_in=1, tokens_out=1), T0 + i)
    assert used == ["c1", "c2", "c3", "c1", "c2", "c3"]


# --------------------------------------------------------------------------- Router trên cấu hình mẫu
@pytest.fixture()
def router():
    return Router(load_pool_model(CFG), seed=7)


def test_standard_traffic_prefers_free_tier_and_spreads_over_projects(router):
    used = []
    for i in range(10):
        out = router.acquire(Request("fast", 20_000, 2_000, gate="A"), T0 + i * 0.1)
        assert isinstance(out, Lease), out
        used.append(out.deployment.id)
        router.settle(out, Outcome("ok", tokens_in=20_000, tokens_out=1_000), T0 + i * 0.1)
    assert "gemini-paid/flash" not in used
    assert {"gemini-free-a/flash", "gemini-free-b/flash"} <= set(used)


def test_private_job_only_reaches_no_training_groups(router):
    for _ in range(5):
        out = router.acquire(Request("writer", 30_000, 4_000, privacy="private", gate="B"), T0)
        assert isinstance(out, Lease) and out.deployment.id == "gemini-paid/flash"
        router.settle(out, Outcome("ok", tokens_in=30_000, tokens_out=3_000), T0)
    out = router.acquire(Request("fast", 1_000, 100, privacy="private", gate="B", allow_metered=False), T0)
    assert isinstance(out, Impossible)  # không có nơi nào hợp lệ khi cấm trả phí: KHÔNG được rò sang free tier


def test_trial_only_group_is_limited_to_dev_gate(router):
    trial = next(d for d in router.m.deployments if d.id == "nvidia-trial/chat")
    first = router.acquire(Request("fast", 5_000, 500, gate="dev"), T0)
    assert isinstance(first, Lease) and first.deployment.id == "nvidia-trial/chat"  # tầng thử nghiệm chỉ có ở cổng dev
    router.settle(first, Outcome("ok", tokens_in=5_000, tokens_out=300), T0)
    for gate in ("A", "B", "C"):
        out = router.acquire(Request("fast", 5_000, 500, gate=gate), T0)
        assert isinstance(out, Lease) and out.deployment.id != "nvidia-trial/chat", gate
        router.settle(out, Outcome("ok", tokens_in=5_000, tokens_out=300), T0)
        assert not router.eligible(trial, Request("fast", 5_000, 500, gate=gate), router.m.profiles["fast"]), gate


def test_trial_group_never_serves_private_jobs(router):
    out = router.acquire(Request("fast", 5_000, 500, privacy="private", gate="dev"), T0)
    assert isinstance(out, Lease) and out.deployment.group.tier == "paid"  # không rò sang nhóm thử nghiệm hay nhóm free


def test_region_restricted_users_skip_free_gemini(router):
    out = router.acquire(Request("writer", 30_000, 4_000, gate="B", region_restricted=True), T0)
    assert isinstance(out, Lease) and out.deployment.group.tier == "paid"


def test_quality_floor_excludes_weak_models_from_writer_but_not_from_fast():
    r = Router(load_pool_model(CFG), seed=1)
    prof_w, prof_f = r.m.profiles["writer"], r.m.profiles["fast"]
    weak = next(d for d in r.m.deployments if d.id == "provider-a-free/large")
    req = Request("writer", 10_000, 1_000, gate="A")
    assert not r.eligible(weak, req, prof_w)
    assert r.eligible(weak, Request("fast", 10_000, 1_000, gate="A"), prof_f)


def test_context_window_excludes_small_models_via_tokenizer_factor():
    r = Router(load_pool_model(CFG), seed=1)
    weak = next(d for d in r.m.deployments if d.id == "provider-a-free/large")
    big = Request("fast", 110_000, 4_000, gate="A")  # 110k * 1.25 = 137.5k > 131k
    assert not r.eligible(weak, big, r.m.profiles["fast"])
    assert r.eligible(weak, Request("fast", 60_000, 4_000, gate="A"), r.m.profiles["fast"])


def test_short_rpm_exhaustion_waits_for_free_tier_but_daily_exhaustion_spills_to_paid():
    r = Router(load_pool_model(CFG), seed=3)
    # tiêu hết RPM của cả hai dự án free (cap 8.5 mỗi dự án => 8 lời gọi) bằng yêu cầu nhỏ
    for _ in range(16):
        out = r.acquire(Request("writer", 1_000, 100, gate="A"), T0)
        assert isinstance(out, Lease)
        assert out.deployment.group.tier == "free"
        r.settle(out, Outcome("ok", tokens_in=1_000, tokens_out=100), T0)
    nxt = r.acquire(Request("writer", 1_000, 100, gate="A"), T0)
    assert isinstance(nxt, Wait) and nxt.tier == "free-strong" and nxt.until - T0 < 120  # chờ ngắn -> chờ chứ không tốn tiền
    # hết hạn mức NGÀY của free (chờ hàng giờ) -> chuyển sang tầng trả phí
    for gid in ("gemini-free-a/flash", "gemini-free-b/flash"):
        dep = next(d for d in r.m.deployments if d.id == gid)
        s = r.state._scope("deployment", gid)
        s.day_key = "x"  # buộc _touch đặt lại, rồi nạp đầy bộ đếm ngày
        r.state._touch(s, dep.limits, dep.group, T0 + 60)
        s.rpd_used = 10_000
    out = r.acquire(Request("writer", 1_000, 100, gate="A"), T0 + 60)
    assert isinstance(out, Lease) and out.deployment.id == "gemini-paid/flash"


def test_metered_cap_blocks_paid_and_returns_wait_when_free_is_only_temporarily_busy():
    r = Router(load_pool_model(CFG), seed=3)
    for _ in range(16):
        l = r.acquire(Request("writer", 1_000, 100, gate="A", allow_metered=False), T0)
        r.settle(l, Outcome("ok", tokens_in=1_000, tokens_out=100), T0)
    out = r.acquire(Request("writer", 1_000, 100, gate="A", allow_metered=False), T0)
    assert isinstance(out, Wait)


def test_diversity_prefers_other_group_and_degrades_only_when_waiting_too_long():
    r = Router(load_pool_model(CFG), seed=5)
    first = r.acquire(Request("writer", 5_000, 1_000, gate="A"), T0)
    gid = first.deployment.group.id
    for _ in range(6):
        out = r.acquire(Request("verifier", 5_000, 1_000, gate="A", avoid_groups=frozenset({gid})), T0)
        assert isinstance(out, Lease) and out.deployment.group.id != gid and not out.diversity_degraded
        r.settle(out, Outcome("ok", tokens_in=5_000, tokens_out=500), T0)
    # Chỉ còn một nhóm hợp lệ (loại nhóm paid bằng allow_metered=False và nhóm free còn lại bằng exclude) -> nới đa dạng
    other = next(d.id for d in r.m.deployments if d.id.startswith("gemini-free") and d.group.id != gid)
    out = r.acquire(Request("verifier", 5_000, 1_000, gate="A", allow_metered=False, avoid_groups=frozenset({gid}), exclude=frozenset({other})), T0 + 1000)
    assert isinstance(out, Lease) and out.diversity_degraded and out.deployment.group.id == gid


def test_headroom_strategy_spreads_load_evenly_between_equal_deployments():
    cfg = json.loads(json.dumps(CFG))
    for d in cfg["deployments"]:
        if d["id"].startswith("gemini-free"):
            d["limits"]["rpm"], d["limits"]["rpd"] = 60, None
    r = Router(load_pool_model(cfg), seed=11)
    counts = {"gemini-free-a/flash": 0, "gemini-free-b/flash": 0}
    for i in range(40):
        out = r.acquire(Request("fast", 5_000, 500, gate="A"), T0 + i * 0.5)
        counts[out.deployment.id] += 1
        r.settle(out, Outcome("ok", tokens_in=5_000, tokens_out=400), T0 + i * 0.5)
    assert abs(counts["gemini-free-a/flash"] - counts["gemini-free-b/flash"]) <= 8, counts


def test_failover_exclude_skips_failed_deployment():
    r = Router(load_pool_model(CFG), seed=2)
    out = r.acquire(Request("fast", 5_000, 500, gate="A", exclude=frozenset({"gemini-free-a/flash", "gemini-free-b/flash"})), T0)
    assert isinstance(out, Lease) and out.deployment.id == "provider-a-free/large"


def test_impossible_when_request_cannot_fit_anywhere():
    r = Router(load_pool_model(CFG), seed=2)
    out = r.acquire(Request("writer", 2_000_000, 1_000, gate="A"), T0)
    assert isinstance(out, Impossible)


def test_example_profiles_cover_every_prompt_model_profile():
    prompts = sorted((ROOT / "prompts").glob("P*.md"))
    import yaml
    need = set()
    for p in prompts:
        text = p.read_text(encoding="utf-8")
        meta = yaml.safe_load(text.split("---")[1])
        need.add(meta["model_profile"])
    assert need <= {p["name"] for p in CFG["profiles"]}, need


# --------------------------------------------------------------------------- phân loại lỗi
def test_classify_gemini_429_day_vs_minute():
    day = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}}
    o = classify_http("gemini_native", 429, {}, day)
    assert o.kind == "rate_limited_day" and o.retry_after_s == 34.0
    minute = json.loads(json.dumps(day))
    minute["error"]["details"][0]["violations"][0]["quotaId"] = "GenerateContentRequestsPerMinutePerProjectPerModel-FreeTier"
    assert classify_http("gemini_native", 429, {}, minute).kind == "rate_limited_minute"


def test_classify_openai_compat_errors():
    assert classify_http("openai_compat", 429, {"Retry-After": "12"}, {"error": {"message": "Rate limit reached for requests per minute (RPM)"}}) == Outcome("rate_limited_minute", retry_after_s=12.0)
    assert classify_http("openai_compat", 429, {}, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}).kind == "rate_limited_day"
    assert classify_http("openai_compat", 429, {}, {"error": {"message": "Rate limit reached on tokens per day (TPD)"}}).kind == "rate_limited_day"
    assert classify_http("openai_compat", 429, {}, {}).kind == "rate_limited_unknown"
    assert classify_http("openai_compat", 401).kind == "auth_error"
    assert classify_http("openai_compat", 400, {}, {"error": {"message": "This model's maximum context length is 8192 tokens"}}).kind == "context_exceeded"
    assert classify_http("openai_compat", 400, {}, {"error": {"message": "unknown parameter"}}).kind == "bad_request"
    # Google: khoá sai trả 400 INVALID_ARGUMENT (không phải 401) — phải cách ly khoá, không coi là lỗi lập trình
    google_bad_key = {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key.", "details": [{"reason": "API_KEY_INVALID"}]}}
    assert classify_http("gemini_native", 400, {}, google_bad_key).kind == "auth_error"
    assert classify_http("gemini_native", 400, {}, {"error": {"message": "Invalid JSON payload received"}}).kind == "bad_request"
    assert classify_http("openai_compat", 503).kind == "server_error"
    assert classify_http("openai_compat", 504).kind == "timeout"
    assert classify_http("openai_compat", 200, finish_reason="length").kind == "truncated"
    assert classify_http("openai_compat", 200, finish_reason="content_filter").kind == "safety_blocked"
    assert classify_http("gemini_native", 200, finish_reason="SAFETY").kind == "safety_blocked"
    assert classify_http("gemini_native", 200, finish_reason="STOP").kind == "ok"


# --------------------------------------------------------------------------- dung lượng
def test_daily_capacity_uses_the_tightest_limit():
    d = mk_dep(rpm=10, tpm=250_000, rpd=250, basis="input")
    c = deployment_daily_capacity(d)
    assert c["calls"] == min(math.floor(250 * 0.95), math.floor(8.5 * 1440)) == 237
    assert c["tokens"] == math.floor(250_000 * 0.85 * 1440)
    assert deployment_daily_capacity(mk_dep(rpm=None))["calls"] is None


# --------------------------------------------------------------------------- độ phủ của kịch bản so khớp SQL
def test_conformance_scenarios_reach_every_denial_reason_and_circuit_state():
    """Bảo đảm bài so khớp SQL == MemoryState (tests/pg_smoke.py) không rỗng: các kịch bản phải chạm mọi nhánh."""
    import pool_scenarios as ps

    reasons, circuits, granted = collections.Counter(), set(), 0
    for scn in ps.SCENARIOS:
        r = ps.run(scn, CFG)
        reasons.update(r.reasons)
        circuits |= r.circuits
        granted += r.granted
    missing = ps.REQUIRED_REASONS - set(reasons)
    assert not missing, f"kịch bản chưa chạm các nhánh: {sorted(missing)}"
    assert {"closed", "open", "half_open"} <= circuits
    assert granted > 300


# --------------------------------------------------------------------------- chuyển dự phòng sau lỗi
def test_failover_prefers_other_deployments_but_never_hangs_on_a_single_survivor():
    cfg = json.loads(json.dumps(CFG))
    only = [d for d in cfg["deployments"] if d["id"] == "gemini-paid/flash"]
    cfg["deployments"], cfg["groups"] = only, [g for g in cfg["groups"] if g["id"] == "gemini-paid"]
    r = Router(load_pool_model(cfg), seed=1)
    ex = Request("writer", 5_000, 500, gate="A", exclude=frozenset({"gemini-paid/flash"}))
    assert isinstance(r.acquire(ex, T0), Impossible)  # loại trừ cứng: không còn ứng viên
    out = r.acquire_failover(ex, T0)  # giao thức dự phòng: bỏ loại trừ và thử lại deployment duy nhất
    assert isinstance(out, Lease) and out.deployment.id == "gemini-paid/flash"


def test_failover_waits_instead_of_taking_a_long_wait_when_the_failed_one_is_available():
    r = Router(load_pool_model(CFG), seed=3)
    for dep in ("gemini-free-a/flash", "gemini-free-b/flash"):  # hết hạn mức ngày của cả hai dự án free
        d = next(x for x in r.m.deployments if x.id == dep)
        s = r.state._scope("deployment", dep)
        r.state._touch(s, d.limits, d.group, T0)
        s.rpd_used = 10_000
    req = Request("writer", 1_000, 100, gate="A", exclude=frozenset({"gemini-paid/flash"}), allow_metered=True)
    out = r.acquire_failover(req, T0)
    assert isinstance(out, Lease) and out.deployment.id == "gemini-paid/flash"  # thay vì chờ tới nửa đêm Thái Bình Dương


# --------------------------------------------------------------------------- mô phỏng
def test_simulation_shows_free_tier_saves_money_and_adapts_to_wrong_limits():
    sys.path.insert(0, str(ROOT / "tools"))
    import simulate_pool as sim

    rows = {r["name"][0]: r for r in sim.run_all(n_docs=4)}
    assert all(r["docs"] == r["n_docs"] for r in rows.values()), rows  # mọi kịch bản đều hoàn tất, không tài liệu nào bị bỏ rơi
    assert rows["B"]["paid"] == 0 and rows["B"]["paid_usd"] == 0
    assert rows["A"]["paid_usd"] < rows["C"]["paid_usd"]  # có free tier thì rẻ hơn chỉ trả phí
    assert rows["D"]["paid"] == 0 and rows["C"]["free"] == 0  # đủ dự án free thì không tốn tiền; vùng bị hạn chế thì không dùng free
    assert rows["E"]["r429"] > 0 and rows["E"]["docs"] == rows["E"]["n_docs"]  # hạn mức thật thấp hơn cấu hình: có 429 nhưng pool tự thích nghi
    assert rows["A"]["r429"] == 0  # cấu hình đúng thì giới hạn phía client chặn được 429


def test_numbers_quoted_in_the_spec_prose_hold_for_the_ten_document_simulation():
    """SPEC §17.12 và §13.2 nêu: có hai dự án free thì ~3 USD thay vì ~7,4 USD cho 10 tài liệu 300 trang, chỉ-free tốn 0 nhưng chờ qua nửa đêm."""
    sys.path.insert(0, str(ROOT / "tools"))
    import simulate_pool as sim

    rows = {r["name"][0]: r for r in sim.run_all(n_docs=10)}
    assert 7.0 < rows["C"]["paid_usd"] < 7.9
    assert 2.4 < rows["A"]["paid_usd"] < 3.7 and rows["A"]["hours"] < 2
    assert rows["B"]["paid_usd"] == 0 and rows["B"]["hours"] > 12  # phải chờ qua lần đặt lại hạn mức lúc nửa đêm giờ Thái Bình Dương
    assert rows["D"]["paid_usd"] == 0 and rows["D"]["hours"] < 1


def test_public_gates_exclude_risk_flagged_groups_even_if_allowed_gates_says_otherwise():
    """Chốt chặn thứ hai: cấu hình sai allowed_gates cũng không đưa nhóm multi_account_risk hay trial_only ra cổng công khai."""
    cfg = json.loads(json.dumps(CFG))
    for g in cfg["groups"]:
        g["allowed_gates"] = ["dev", "A", "B", "C"]
    r = Router(load_pool_model(cfg), seed=1)
    prof = r.m.profiles["writer"]
    free_b = next(d for d in r.m.deployments if d.id == "gemini-free-b/flash")  # gắn multi_account_risk
    free_a = next(d for d in r.m.deployments if d.id == "gemini-free-a/flash")
    trial = next(d for d in r.m.deployments if d.id == "nvidia-trial/chat")  # gắn trial_only
    for gate, expect_risky in (("dev", True), ("A", True), ("B", False), ("C", False)):
        assert r.eligible(free_b, Request("writer", 5_000, 500, gate=gate), prof) is expect_risky, gate
        assert r.eligible(free_a, Request("writer", 5_000, 500, gate=gate), prof) is True, gate
        assert r.eligible(trial, Request("fast", 5_000, 500, gate=gate), r.m.profiles["fast"]) is expect_risky, gate


def test_owner_can_accept_risk_at_public_gates_but_trial_groups_still_never_serve_private_jobs():
    cfg = json.loads(json.dumps(CFG))
    cfg["policy"]["allow_risk_at_public_gates"] = True
    for g in cfg["groups"]:
        g["allowed_gates"] = ["dev", "A", "B", "C"]
    r = Router(load_pool_model(cfg), seed=1)
    free_b = next(d for d in r.m.deployments if d.id == "gemini-free-b/flash")
    trial = next(d for d in r.m.deployments if d.id == "nvidia-trial/chat")
    assert r.eligible(free_b, Request("writer", 5_000, 500, gate="B"), r.m.profiles["writer"])
    assert r.eligible(trial, Request("fast", 5_000, 500, gate="B"), r.m.profiles["fast"])
    assert not r.eligible(trial, Request("fast", 5_000, 500, gate="B", privacy="private"), r.m.profiles["fast"])  # riêng tư không đổi theo lựa chọn rủi ro


def test_group_with_risk_flag_but_no_acknowledgement_is_never_selected():
    cfg = json.loads(json.dumps(CFG))
    next(g for g in cfg["groups"] if g["id"] == "gemini-free-b")["risk_ack"] = []
    next(g for g in cfg["groups"] if g["id"] == "nvidia-trial")["risk_ack"] = []
    r = Router(load_pool_model(cfg), seed=1)
    free_b = next(d for d in r.m.deployments if d.id == "gemini-free-b/flash")
    trial = next(d for d in r.m.deployments if d.id == "nvidia-trial/chat")
    for gate in ("dev", "A"):
        assert not r.eligible(free_b, Request("writer", 5_000, 500, gate=gate), r.m.profiles["writer"]), gate
        assert not r.eligible(trial, Request("fast", 5_000, 500, gate=gate), r.m.profiles["fast"]), gate
    ok = r.acquire(Request("writer", 5_000, 500, gate="A"), T0)
    assert isinstance(ok, Lease) and ok.deployment.id == "gemini-free-a/flash"  # nhóm đã xác nhận (hoặc không có cờ) vẫn phục vụ bình thường
