"""Bốn kịch bản diễn tập sự cố (M3) — chạy được trong sandbox, không cần VPS hay token thật.

`docs/BUILD_PLAN.md` §M3 yêu cầu diễn tập: **hết hạn mức**, **khoá bị từ chối**, **nhà cung cấp sập**,
**VPS chết**. Diễn tập bằng tay trên VPS thì mỗi quý mới làm một lần và không ai nhớ kết quả; ở đây mỗi
kịch bản là một test chạy trong CI:

1. *Hết hạn mức*: nhà cung cấp trả 429 → pool chuyển sang deployment khác, ghi sổ từng lần, và khi hết
   đường thì báo lỗi có `retry_after_s` — kèm bằng chứng hạn mức được ghi ở CSDL dùng chung.
2. *Khoá bị từ chối*: 401/403 → khoá bị CÁCH LY Ở CSDL (không phải chỉ trong RAM), `ops health` thấy,
   cảnh báo `credential_quarantined` bật, và tiến trình khác không thử lại khoá chết đó.
3. *Nhà cung cấp sập*: 503 liên tiếp → cầu dao mở, ghi `llm_incidents`, và job không quay vòng vô hạn;
   sau cooldown thì deployment được thử lại (half-open).
4. *VPS chết*: worker chết giữa lời gọi để lại lease + task mồ côi → `ops reap` thu hồi, task về hàng đợi,
   job chạy tiếp được, và khoá chưa dùng hết thì không bị cách ly oan.

Điều đáng kiểm nhất không phải "mã chạy được" mà là **trạng thái có dùng chung giữa các tiến trình hay
không**: nếu chỉ nằm trong RAM thì worker thứ hai vẫn đốt tiếp ngân sách đang bị chặn.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from test_pool_http import FAKE_KEY, MockAPI, _Handler  # máy chủ nhà cung cấp giả đã có sẵn
from test_pool_http import SCHEMA as POOL_SCHEMA
from visynth.llm.base import LLMError, LLMRequest
from visynth.pool import dbstore
from visynth.pool.client import PooledLLMClient
from visynth.pool.dbstate import DbPoolState
from visynth.pool.pgstore import PgStore
from visynth.pool.secrets import encrypt_secret, load_master_key, new_master_key


@pytest.fixture
def master():
    return load_master_key(new_master_key())


@pytest.fixture
def mock_api():
    """Bản sao fixture của `test_pool_http` (fixture không tự đi sang tệp khác).

    Dùng lại đúng máy chủ giả đó để diễn tập chạy trên HTTP thật, không phải transport giả — nhờ vậy
    kịch bản 401/429/503 đi qua đúng đoạn mã phân loại lỗi sẽ chạy trên VPS.
    """
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


@pytest.fixture
def store(pg_schema):
    store = PgStore(pg_schema)
    yield store
    store.close()


class Clock:
    """Đồng hồ giả: `sleep` làm thời gian trôi ngay lập tức.

    Vì sao không dùng `time.sleep`: cooldown 30 giây × nhiều lần thử làm mỗi test chạy hàng chục giây,
    và thời gian thật làm kết quả phập phù (test "flaky" là test sẽ bị ai đó tắt đi). Ở đây mọi thứ
    tất định: cooldown vẫn được tôn trọng, chỉ là không ai phải chờ.
    """

    def __init__(self, start: float):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class Pool:
    """Cấu hình pool tối thiểu trong CSDL: 2 nhà cung cấp × 1 nhóm × 1 deployment, hạn mức nhỏ để dễ đụng trần."""

    def __init__(self, db, mock_api, master, *, rpm: int = 60, rpd: int = 1000):
        self.db = db
        self.mock = mock_api
        self.master = master
        # `base_url` phải là https theo ràng buộc của CSDL (không cho http trần — §20.4), nên cấu hình
        # giống hệt production; chỉ có `transport` được chuyển hướng sang máy chủ giả ở dưới.
        db.execute(
            """INSERT INTO llm_providers (id, kind, base_url, display_name)
               VALUES ('gemini', 'gemini_native', 'https://generativelanguage.googleapis.com', 'Gemini (giả)'),
                      ('nvidia', 'openai_compat', 'https://integrate.api.nvidia.com', 'NVIDIA (giả)')"""
        )
        db.execute(
            """INSERT INTO llm_models (id, provider_id, model_id, ctx_in, max_out, structured, quality)
               VALUES ('gemini:flash', 'gemini', 'gemini-3.8-flash', 1000000, 65536, 'json_schema',
                       '{"json": 0.9, "vi_write": 0.9}'::jsonb),
                      ('nvidia:llama', 'nvidia', 'meta/llama-3.3-70b-instruct', 128000, 8192, 'json_object',
                       '{"json": 0.7, "vi_write": 0.6}'::jsonb)"""
        )
        # `trial` = nhóm dùng khoá trial; `no_training` = không dùng dữ liệu huấn luyện (giữ đúng §17)
        db.execute(
            """INSERT INTO llm_quota_groups (id, provider_id, label, tier, data_policy, rpm, rpd, safety_margin, day_margin)
               VALUES ('g-gemini', 'gemini', 'Gemini trial', 'trial', 'no_training', %s, %s, 1.0, 1.0),
                      ('g-nvidia', 'nvidia', 'NVIDIA trial', 'trial', 'no_training', %s, %s, 1.0, 1.0)""",
            (rpm, rpd, rpm, rpd),
        )
        db.execute(
            """INSERT INTO llm_deployments (id, group_id, model_id)
               VALUES ('g-gemini/flash', 'g-gemini', 'gemini:flash'),
                      ('g-nvidia/llama', 'g-nvidia', 'nvidia:llama')"""
        )
        for credential_id, group, secret in (
            ("g-gemini-k1", "g-gemini", FAKE_KEY),
            ("g-nvidia-k1", "g-nvidia", FAKE_KEY),
        ):
            db.execute(
                """INSERT INTO llm_credentials (id, group_id, label, secret_enc, last4)
                   VALUES (%s, %s, %s, %s, %s)""",
                (credential_id, group, "khoá giả", encrypt_secret(master, credential_id, secret), secret[-4:]),
            )
        # Một job THẬT: sổ `llm_calls` có khoá ngoại tới `jobs`, nên muốn kiểm sổ thì phải có job
        # (và như vậy cũng kiểm luôn ràng buộc dữ liệu tiền − chi phí gắn đúng người dùng/job).
        self.user_id = str(
            db.scalar(
                "INSERT INTO users (email, role, status) VALUES ('diễn-tập@example.com','user','active') RETURNING id"
            )
        )
        db.execute(
            """INSERT INTO documents (id, user_id, title, source_type, status)
               VALUES ('66666666-6666-6666-6666-666666666666', %s, 'tài liệu diễn tập', 'upload', 'ready')""",
            (self.user_id,),
        )
        self.job_id = "77777777-7777-7777-7777-777777777777"
        db.execute(
            """INSERT INTO jobs (id, user_id, document_id, level, model_profile, prompt_versions, status, est_credits)
               VALUES (%s, %s, '66666666-6666-6666-6666-666666666666', 'detailed_synthesis',
                       '{"name": "balanced"}', '{}'::jsonb, 'running', 3)""",
            (self.job_id, self.user_id),
        )
        db.execute(
            """INSERT INTO llm_profiles (name, needs)
               VALUES ('fast', '{"structured": "json_object", "min_ctx_in": 1000, "vision": false,
                                  "pdf": false, "min_quality": {"json": 0.5}}'::jsonb)"""
        )
        db.execute(
            """INSERT INTO llm_profile_tiers (profile_name, tier_no, name, select_group_tiers, strategy)
               VALUES ('fast', 1, 'trial', '{trial}', 'ordered')"""
        )
        self.clock = Clock(float(db.scalar("SELECT extract(epoch FROM now())")))

    def client(self, store, **kw) -> PooledLLMClient:
        kw.setdefault("transport", _redirect(self.mock))
        kw.setdefault("clock", self.clock)
        kw.setdefault("sleep", self.clock.sleep)
        ledger = dbstore.DbLedger(store, user_id=self.user_id, stage="map")
        return PooledLLMClient.from_db(store, self.master, ledger=ledger, **kw)

    def ok_reply(self, kind: str = "gemini") -> None:
        self.mock.set(kind, status=200, body=_ok_body(kind))


def _redirect(mock):
    """Chuyển hướng mọi lời gọi HTTPS của pool về máy chủ giả, giữ nguyên đường dẫn và khoá gửi kèm."""
    from visynth.pool.adapters import HTTPReply, urllib_transport

    def transport(method: str, url: str, headers: dict, data: bytes, timeout_s: float) -> HTTPReply:
        for real, local in (
            ("https://generativelanguage.googleapis.com", f"{mock.base}/v1beta"),
            ("https://integrate.api.nvidia.com", f"{mock.base}/v1"),
        ):
            if url.startswith(real):
                url = local + url[len(real) :]
                break
        return urllib_transport(method, url, headers, data, timeout_s)

    return transport


#: Job thật của bộ diễn tập (fixture `Pool` đặt lại giá trị này).
JOB_ID = "77777777-7777-7777-7777-777777777777"


def _request(prompt_id: str = "P4", **meta) -> LLMRequest:
    """Yêu cầu gửi tới pool, dùng profile `fast` (profile duy nhất khai trong CSDL của bộ diễn tập)."""
    return LLMRequest(
        prompt_id=prompt_id,
        system="SYS",
        user="USER",
        schema=POOL_SCHEMA,
        max_output_tokens=256,
        temperature=0.2,
        metadata={"job_id": JOB_ID, "task_id": 1, "profile": "fast", **meta},
    )


def _ok_body(kind: str) -> dict:
    if kind == "gemini":
        return {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"ok": true}'}]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
        }
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _state(db, deployment_id: str) -> dict:
    return db.one("SELECT * FROM llm_scope_state WHERE scope = 'deployment' AND scope_id = %s", (deployment_id,))


# ---------------------------------------------------------------------------------------------
# Diễn tập 1 — hết hạn mức
# ---------------------------------------------------------------------------------------------


def test_drill_quota_exhausted_switches_deployment_and_fails_clearly(db, store, mock_api, master):
    pool = Pool(db, mock_api, master)
    mock_api.set(
        "gemini",
        status=429,
        body={"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}},
        headers={"retry-after": "37"},
    )
    mock_api.set("openai_compat", status=429, body={"error": {"message": "rate limit"}}, headers={"retry-after": "12"})

    client = pool.client(store)
    with pytest.raises(LLMError) as excinfo:
        client.complete(_request("P4"))

    # Bằng chứng ở phía nhà cung cấp: cả hai deployment đều ĐÃ được gọi rồi mới bỏ cuộc,
    # chứ không thử lại một chỗ đến vô hạn.
    paths = [request["path"] for request in mock_api.requests]
    assert any(path.startswith("/v1beta/") for path in paths), f"phải thử Gemini: {paths}"
    assert any(path.startswith("/v1/chat") or path.startswith("/v1/completions") for path in paths), (
        f"phải thử NVIDIA: {paths}"
    )
    assert "rate_limited" in excinfo.value.outcome.kind
    assert db.scalar("SELECT count(*) FROM llm_incidents WHERE kind LIKE 'rate_limited%%'") >= 2

    # Hạn mức/cooldown phải nằm ở CSDL (dùng chung cho mọi tiến trình), không chỉ trong RAM client này
    state = _state(db, "g-gemini/flash")
    assert state["cooldown_until"] > 0, "cooldown phải được ghi xuống CSDL"
    assert db.scalar("SELECT count(*) FROM llm_incidents WHERE kind LIKE 'rate_limited%%'") >= 1

    # Tiến trình KHÁC (client mới, không chia sẻ RAM) phải thấy ngay là đang bị chặn
    second = pool.client(store)
    outcome = second.router.state.try_reserve(second.model.deployments[0], 100, 50, "normal", pool.clock.t)
    assert not outcome.ok and "cooldown" in outcome.reason

    # Hết cooldown: nhà cung cấp khoẻ lại thì job chạy được — không kẹt vĩnh viễn
    pool.ok_reply("gemini")
    pool.ok_reply("openai_compat")
    pool.clock.advance(4000)
    third = pool.client(store)
    response = third.complete(_request("P4"))
    assert response.outcome.kind == "ok"
    assert db.scalar("SELECT count(*) FROM llm_leases") == 0, "mọi chỗ đặt phải được trả lại"


def test_drill_quota_state_is_shared_between_two_workers(db, mock_api, master):
    """Hai worker dùng CHUNG một ngân sách: worker thứ hai bị chặn ngay khi worker đầu tiêu hết rpm."""
    # `rpd = 1`: chỉ cần MỘT lời gọi là hết ngân sách ngày → thấy ngay tác dụng của việc dùng chung.
    pool = Pool(db, mock_api, master, rpm=60, rpd=1)
    pool.ok_reply("gemini")
    pool.ok_reply("openai_compat")
    first_store, second_store = PgStore(db.dsn), PgStore(db.dsn)
    try:
        first = pool.client(first_store)
        response = first.complete(_request("P4"))
        assert response.outcome.kind == "ok"
        state = _state(db, "g-gemini/flash")
        assert state["rpd_used"] == 1, "lượt gọi phải được đếm vào ngân sách chung"

        second = pool.client(second_store)
        lease = second.router.state.try_reserve(second.model.deployments[0], 100, 50, "normal", pool.clock.t + 1)
        assert not lease.ok and lease.reason.endswith("rpd"), f"worker 2 phải thấy hạn mức đã dùng: {lease.reason}"
    finally:
        first_store.close()
        second_store.close()


# ---------------------------------------------------------------------------------------------
# Diễn tập 2 — khoá bị từ chối
# ---------------------------------------------------------------------------------------------


def test_drill_rejected_key_is_quarantined_in_the_database_and_raises_alert(db, store, mock_api, master):
    from visynth.worker import alerts, ops

    pool = Pool(db, mock_api, master)
    mock_api.set(
        "gemini",
        status=400,
        body={
            "error": {
                "code": 400,
                "message": "API key not valid. Please pass a valid API key.",
                "status": "INVALID_ARGUMENT",
            }
        },
    )
    pool.ok_reply("openai_compat")  # deployment còn lại khoẻ → job không phải hỏng

    client = pool.client(store)
    with pytest.raises(LLMError) as excinfo:
        client.complete(_request("P4", exclude_groups=["g-gemini"]))
    assert "cách ly khoá" in str(excinfo.value)

    # Khoá bị cách ly Ở CSDL — đây là điều làm cảnh báo vận hành có ý nghĩa
    assert db.scalar("SELECT status FROM llm_credentials WHERE id = 'g-gemini-k1'") == "quarantined"
    assert db.scalar("SELECT quarantined_reason FROM llm_credentials WHERE id = 'g-gemini-k1'") == "auth_error"
    assert db.scalar("SELECT count(*) FROM llm_incidents WHERE kind = 'auth_error'") == 1

    health = ops.health(db.dsn)
    assert health["credentials_quarantined"] == 1
    found = alerts.evaluate(health)
    assert any(item["code"] == "credential_quarantined" and item["severity"] == "critical" for item in found)
    assert alerts.status(found) == "critical", "khoá chết phải làm trạng thái chuyển 'critical' cho giám sát"

    # Tiến trình mới không dùng lại khoá chết, và nói rõ vì sao
    fresh = pool.client(store)
    remaining = {c.id for c in fresh.model.deployments[0].group.credentials if c.status == "active"}
    assert "g-gemini-k1" not in remaining
    after_quarantine = fresh.complete(_request("P4"))
    assert after_quarantine.outcome.kind == "ok", "vẫn còn deployment khoẻ thì job phải chạy tiếp được"
    assert after_quarantine.deployment_id == "g-nvidia/llama"

    # Và khi mọi khoá đều chết thì lỗi phải nói rõ cần xoay khoá, không phải 'impossible' khó hiểu
    db.execute("UPDATE llm_credentials SET status = 'quarantined'")
    dead_end = pool.client(store)
    with pytest.raises(LLMError) as excinfo2:
        dead_end.complete(_request("P4"))
    assert excinfo2.value.outcome.kind in {"impossible", "no_capacity", "auth_error"}
    assert db.scalar("SELECT count(*) FROM llm_leases") == 0


# ---------------------------------------------------------------------------------------------
# Diễn tập 3 — nhà cung cấp sập
# ---------------------------------------------------------------------------------------------


def test_drill_provider_down_trips_circuit_then_recovers(db, store, mock_api, master):
    pool = Pool(db, mock_api, master)
    mock_api.set("gemini", status=503, raw=b"<html>service unavailable</html>")
    mock_api.set("openai_compat", status=503, raw=b"<html>service unavailable</html>")

    # Mỗi lời gọi thử vài deployment rồi bỏ cuộc; cầu dao mở khi MỘT deployment lỗi 3 lần liên tiếp,
    # nên cần vài lời gọi — đúng như thực tế: nhà cung cấp sập thì job này nối tiếp job khác cùng hỏng.
    for _ in range(4):
        with pytest.raises(LLMError):
            pool.client(store).complete(_request("P4"))
        if db.scalar("SELECT count(*) FROM llm_incidents WHERE kind = 'circuit_open'"):
            break

    incidents = db.scalar("SELECT count(*) FROM llm_incidents WHERE kind = 'circuit_open'")
    assert incidents >= 1, "3 lỗi liên tiếp phải mở cầu dao và ghi sự cố"
    opened = db.all("SELECT scope_id FROM llm_scope_state WHERE scope = 'deployment' AND circuit = 'open'")
    assert opened, "trạng thái cầu dao phải nằm ở CSDL để worker khác thấy"
    assert all(
        row["cooldown_until"] > 0
        for row in db.all("SELECT cooldown_until FROM llm_scope_state WHERE scope='deployment'")
    )

    health = db.all("SELECT scope_id, circuit FROM llm_scope_state WHERE scope = 'deployment' AND circuit <> 'closed'")
    assert health, "ops health dùng bảng này để cảnh báo circuit_open"

    # Nhà cung cấp khoẻ lại: sau cooldown deployment được thử lại (half_open → closed)
    pool.ok_reply("gemini")
    pool.ok_reply("openai_compat")
    pool.clock.advance(4000)
    response = pool.client(store).complete(_request("P4"))
    assert response.outcome.kind == "ok"
    assert db.scalar("SELECT circuit FROM llm_scope_state WHERE scope='deployment' AND scope_id='g-gemini/flash'") in {
        "closed",
        "half_open",
    }


# ---------------------------------------------------------------------------------------------
# Diễn tập 4 — VPS chết
# ---------------------------------------------------------------------------------------------


def test_drill_dead_worker_leaves_recoverable_state(db, store, mock_api, master):
    """Worker bị kill giữa lời gọi: lease còn treo, task mồ côi. `ops reap` phải dọn sạch và không phạt oan."""
    from visynth.worker import ops

    pool = Pool(db, mock_api, master)
    pool.ok_reply("gemini")
    now = pool.clock.t

    # Một lời gọi đang bay thì worker chết (không settle) → lease nằm lại trong CSDL
    state = DbPoolState(store)
    deployment = pool.client(store).model.deployments[0]
    reservation = state.try_reserve(deployment, 100, 50, "normal", now, ttl=300)
    assert reservation.ok
    assert db.scalar("SELECT count(*) FROM llm_leases") == 1
    assert db.scalar("SELECT inflight FROM llm_scope_state WHERE scope='deployment' AND scope_id='g-gemini/flash'") == 1

    # Task mồ côi của worker đã chết
    user_id = str(
        db.scalar("INSERT INTO users (email, role, status) VALUES ('drill@example.com','user','active') RETURNING id")
    )
    db.execute(
        """INSERT INTO documents (id, user_id, title, source_type, status)
           VALUES ('33333333-3333-3333-3333-333333333333', %s, 'tài liệu diễn tập', 'upload', 'ready')""",
        (user_id,),
    )
    db.execute(
        """INSERT INTO jobs (id, user_id, document_id, level, model_profile, prompt_versions, status, est_credits)
           VALUES ('44444444-4444-4444-4444-444444444444', %s, '33333333-3333-3333-3333-333333333333',
                   'detailed_synthesis', '{"name": "balanced"}', '{}'::jsonb, 'running', 3)""",
        (user_id,),
    )
    db.execute(
        """INSERT INTO job_stages (job_id, stage, status)
           VALUES ('44444444-4444-4444-4444-444444444444', 'map', 'running')"""
    )
    db.execute(
        """INSERT INTO job_tasks (job_id, stage, task_key, status, attempt, max_attempts, locked_by, heartbeat_at)
           VALUES ('44444444-4444-4444-4444-444444444444', 'map', '0:4', 'running', 1, 3, 'vps-đã-chết',
                   now() - interval '10 minutes')"""
    )

    # Trước khi reap: báo động đúng (task mồ côi + lease quá hạn khi thời gian trôi)
    health = ops.health(db.dsn)
    assert health["tasks_stale_running"] == 1

    result = ops.reap(db.dsn, stale="3 minutes", now=now + 4000)
    assert result["tasks"] == 1 and result["leases"] == 1
    assert db.scalar("SELECT status FROM job_tasks") == "pending"
    assert db.scalar("SELECT locked_by FROM job_tasks") is None
    assert db.scalar("SELECT count(*) FROM llm_leases") == 0
    assert db.scalar("SELECT inflight FROM llm_scope_state WHERE scope='deployment' AND scope_id='g-gemini/flash'") == 0
    # Worker chết KHÔNG làm cách ly khoá (không phải lỗi xác thực) — nếu cách ly thì mất oan một khoá tốt
    assert db.scalar("SELECT count(*) FROM llm_credentials WHERE status = 'quarantined'") == 0
    assert ops.health(db.dsn)["credentials_quarantined"] == 0

    # Job chạy tiếp được trên worker mới
    follow_up = pool.client(store)
    assert follow_up.complete(_request("P4")).outcome.kind == "ok"
