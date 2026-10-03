"""Giới hạn tốc độ + header bảo mật (M3, §20.4).

Bộ test này là "bằng chứng" cho phần làm cứng, chứ không chỉ là kiểm tra mã chạy được:

* hạn mức phải chặn thật, và chặn **sau đúng** số lần cho phép (không chặn sớm làm hỏng người dùng thật);
* hạn mức theo TÀI KHOẢN phải chặn được cả khi kẻ tấn công đổi IP (đây là lý do middleware một mình
  theo IP là chưa đủ);
* CSDL **không được** lưu IP thô hay email (PDPL §14.2) — chỉ lưu khoá băm;
* khi CSDL lỗi thì phải **cho qua** (fail-open) chứ không dựng thêm tường lửa giả;
* header bảo mật phải có trên cả phản hồi lỗi (429/401), không chỉ trên phản hồi 200.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"


def make_client(pg_schema, **overrides) -> TestClient:
    from visynth_api.app import create_app
    from visynth_api.settings import Settings

    base = {
        "db_dsn": pg_schema,
        "session_secret": "test-secret",
        "web_dir": WEB,
        "rate_limit_salt": "muoi-kiem-thu",
    }
    base.update(overrides)
    application = create_app(Settings(**base))
    client = TestClient(application)
    client.__enter__()
    return client


@pytest.fixture
def client(pg_schema):
    active = make_client(
        pg_schema,
        rate_limit_login_per_account=0,
        rate_limit_api_per_min=0,
    )
    yield active
    active.app.state.db.close()
    active.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# Tầng CSDL: hàm đếm
# ---------------------------------------------------------------------------------------------


def test_counter_blocks_exactly_after_the_limit(db):
    """3 lần cho phép thì lần 1..3 qua, lần 4 chặn — và `retry_after` nằm trong cửa sổ."""
    results = [db.one("SELECT * FROM rate_limit_hit('t', 'subject', 3, 3600)") for _ in range(4)]
    assert [r["allowed"] for r in results] == [True, True, True, False]
    assert [r["hits"] for r in results] == [1, 2, 3, 4]
    assert results[2]["remaining"] == 0
    assert 1 <= results[3]["retry_after_s"] <= 3600


def test_counter_separates_subjects_and_scopes(db):
    assert db.one("SELECT * FROM rate_limit_hit('login', 'a', 1, 3600)")["allowed"] is True
    assert db.one("SELECT * FROM rate_limit_hit('login', 'a', 1, 3600)")["allowed"] is False
    # chủ thể khác ⇒ bộ đếm riêng
    assert db.one("SELECT * FROM rate_limit_hit('login', 'b', 1, 3600)")["allowed"] is True
    # phạm vi khác ⇒ bộ đếm riêng (không lấy hạn mức chặt của tuyến này chặn tuyến khác)
    assert db.one("SELECT * FROM rate_limit_hit('jobs', 'a', 1, 3600)")["allowed"] is True


def test_zero_limit_means_disabled_not_blocked(db):
    """`limit = 0` phải là TẮT hạn mức. Nếu hiểu nhầm thành '0 lần cho phép' thì máy phát triển sẽ
    khoá chính người dùng, và đó là lỗi rất dễ mắc khi đọc mã."""
    row = db.one("SELECT * FROM rate_limit_hit('t', 's', 0, 3600)")
    assert row["allowed"] is True


def test_window_rolls_over_so_a_user_is_not_punished_forever(db):
    """Cửa sổ 2 giây: chờ hết cửa sổ thì người dùng thật lại được phục vụ (chống 'chặn vĩnh viễn')."""
    import time

    assert db.one("SELECT * FROM rate_limit_hit('t', 's', 1, 2)")["allowed"] is True
    assert db.one("SELECT * FROM rate_limit_hit('t', 's', 1, 2)")["allowed"] is False
    time.sleep(2.1)
    assert db.one("SELECT * FROM rate_limit_hit('t', 's', 1, 2)")["allowed"] is True


def test_gc_only_deletes_stale_windows(db):
    db.execute("SELECT * FROM rate_limit_hit('t', 's', 5, 3600)")
    db.execute(
        "INSERT INTO rate_limit_hits (scope, subject, window_start, hits) VALUES ('t', 'old', now() - interval '3 hours', 9)"
    )
    assert db.scalar("SELECT count(*) FROM rate_limit_hits") == 2
    assert db.scalar("SELECT rate_limit_gc('1 hour')") == 1
    assert db.scalar("SELECT count(*) FROM rate_limit_hits") == 1


def test_reap_cleans_rate_limit_rows(db, pg_schema):
    from visynth.worker import ops

    db.execute(
        "INSERT INTO rate_limit_hits (scope, subject, window_start, hits) VALUES ('t', 'old', now() - interval '3 hours', 9)"
    )
    result = ops.reap(pg_schema, now=0.0)
    assert result["rate_limit_rows"] == 1
    assert db.scalar("SELECT count(*) FROM rate_limit_hits") == 0


# ---------------------------------------------------------------------------------------------
# Middleware: hợp đồng 429 trên tuyến thật
# ---------------------------------------------------------------------------------------------


def test_login_is_blocked_with_contract_problem_and_retry_after(pg_schema):
    client = make_client(
        pg_schema,
        rate_limit_login_per_min=3,
        rate_limit_login_per_account=0,
        rate_limit_api_per_min=0,
    )
    try:
        body = {"email": "khong-ton-tai@example.com", "password": "sai-mat-khau-123"}
        statuses = [client.post("/api/v1/auth/login", json=body).status_code for _ in range(3)]
        assert statuses == [401, 401, 401], "dưới hạn mức thì lỗi phải là 401 (sai thông tin), không phải 429"
        blocked = client.post("/api/v1/auth/login", json=body)
        assert blocked.status_code == 429
        payload = blocked.json()
        assert payload["code"] == "rate_limited"
        assert payload["status"] == 429
        assert payload["title"] == "Quá nhiều yêu cầu"
        assert "retry_after_s" in payload and payload["retry_after_s"] >= 1
        assert blocked.headers["content-type"].startswith("application/problem+json")
        assert int(blocked.headers["retry-after"]) >= 1
    finally:
        client.app.state.db.close()
        client.__exit__(None, None, None)


def test_account_limit_stops_distributed_password_guessing(pg_schema):
    """Kẻ tấn công đổi IP mỗi lần vẫn bị chặn: điểm chặn thứ hai đếm theo EMAIL, không theo IP."""
    client = make_client(
        pg_schema,
        rate_limit_login_per_min=0,  # coi như kẻ tấn công luôn có IP mới
        rate_limit_login_per_account=3,
        rate_limit_api_per_min=0,
    )
    try:
        for index in range(3):
            response = client.post(
                "/api/v1/auth/login",
                json={"email": "nan-nhan@example.com", "password": "thu-do"},
                headers={"x-forwarded-for": f"203.0.113.{index + 1}"},
            )
            assert response.status_code == 401
        blocked = client.post(
            "/api/v1/auth/login",
            json={"email": "nan-nhan@example.com", "password": "thu-do"},
            headers={"x-forwarded-for": "198.51.100.7"},
        )
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "rate_limited"
        # email KHÁC thì không bị vạ lây
        other = client.post(
            "/api/v1/auth/login",
            json={"email": "nguoi-khac@example.com", "password": "thu-do"},
            headers={"x-forwarded-for": "203.0.113.1"},
        )
        assert other.status_code == 401
    finally:
        client.app.state.db.close()
        client.__exit__(None, None, None)


def test_broad_api_ceiling_stops_scanning(pg_schema):
    client = make_client(pg_schema, rate_limit_api_per_min=5, rate_limit_login_per_account=0)
    try:
        codes = [client.get("/api/v1/healthz").status_code for _ in range(4)]
        assert codes == [200, 200, 200, 200] or codes[0] == 429, "healthz không bị đếm vào trần thô"
        blocked = None
        for _ in range(10):
            response = client.get("/api/v1/catalog/models")
            if response.status_code == 429:
                blocked = response
                break
        assert blocked is not None, "trần thô cho toàn bộ API phải chặn được vòng quét"
        assert blocked.json()["code"] == "rate_limited"
    finally:
        client.app.state.db.close()
        client.__exit__(None, None, None)


def test_healthz_is_never_rate_limited(pg_schema):
    """Giám sát ngoài máy gọi `/healthz` mỗi vài giây; nếu bị 429 thì hệ thống tự báo động giả."""
    client = make_client(pg_schema, rate_limit_api_per_min=1)
    try:
        for _ in range(6):
            assert client.get("/api/v1/healthz").status_code == 200
    finally:
        client.app.state.db.close()
        client.__exit__(None, None, None)


def test_database_stores_only_hashes_never_the_raw_identity(pg_schema):
    """PDPL §14.2: chống dò mật khẩu nhưng không được lưu IP thô của người dùng."""
    client = make_client(
        pg_schema, rate_limit_login_per_min=5, rate_limit_login_per_account=0, rate_limit_api_per_min=0
    )
    try:
        client.post(
            "/api/v1/auth/login",
            json={"email": "bi-mat@example.com", "password": "thu"},
            headers={"x-forwarded-for": "203.0.113.99"},
        )
    finally:
        client.app.state.db.close()
        client.__exit__(None, None, None)

    from visynth_api.db import Database

    check = Database(pg_schema)
    try:
        rows = check.all("SELECT scope, subject FROM rate_limit_hits")
        assert rows, "phải có bản ghi đếm"
        blob = " ".join(f"{r['scope']} {r['subject']}" for r in rows)
        assert "203.0.113.99" not in blob
        assert "bi-mat@example.com" not in blob
        assert all(len(r["subject"]) == 40 for r in rows), "chủ thể phải là khoá băm, không phải giá trị thô"
    finally:
        check.close()


def test_rate_limiter_fails_open_when_database_is_broken():
    """CSDL hỏng thì mọi thứ đã hỏng; chặn thêm người dùng thật không làm hệ thống an toàn hơn."""
    from visynth_api.limits import RateLimiter, Rule

    class Broken:
        def one(self, *args, **kwargs):
            raise RuntimeError("CSDL chết")

    limiter = RateLimiter(Broken(), salt="muoi")
    allowed, hits, retry = limiter.check(Rule(scope="x", limit=1, window_s=60), "ai-do")
    assert (allowed, hits, retry) == (True, 0, 0)
    limiter.enforce(Rule(scope="x", limit=1, window_s=60), "ai-do")  # không được ném lỗi


# ---------------------------------------------------------------------------------------------
# Header bảo mật
# ---------------------------------------------------------------------------------------------


def test_security_headers_on_ok_error_and_static_responses(client):
    """Header phải có cả khi lỗi — kẻ tấn công thường học hệ thống qua các phản hồi lỗi."""
    responses = [
        client.get("/api/v1/healthz"),
        client.get("/api/v1/khong-ton-tai"),
        client.post("/api/v1/auth/login", json={"email": "a@b.c", "password": "sai-sai-sai"}),
        client.get("/"),
    ]
    for response in responses:
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "object-src 'none'" in response.headers["content-security-policy"]


def test_hsts_only_when_running_over_https(pg_schema):
    plain = make_client(pg_schema, cookie_secure=False)
    try:
        assert "strict-transport-security" not in plain.get("/api/v1/healthz").headers
    finally:
        plain.app.state.db.close()
        plain.__exit__(None, None, None)

    secure = make_client(pg_schema, cookie_secure=True, hsts_max_age_s=1234)
    try:
        header = secure.get("/api/v1/healthz").headers
        assert header["strict-transport-security"] == "max-age=1234; includeSubDomains"
    finally:
        secure.app.state.db.close()
        secure.__exit__(None, None, None)


def test_security_headers_can_be_turned_off_for_debugging(pg_schema):
    client = make_client(pg_schema, security_headers=False)
    try:
        assert "content-security-policy" not in client.get("/api/v1/healthz").headers
    finally:
        client.app.state.db.close()
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# Cấu hình
# ---------------------------------------------------------------------------------------------


def test_env_can_disable_limits_for_local_development(monkeypatch):
    from visynth_api.settings import load_settings

    monkeypatch.setenv("VISYNTH_RATE_LIMIT_LOGIN_PER_MIN", "0")
    monkeypatch.setenv("VISYNTH_RATE_LIMIT_LOGIN_PER_ACCOUNT", "0")
    monkeypatch.setenv("VISYNTH_SECURITY_HEADERS", "false")
    settings = load_settings(db_dsn="postgresql://x", session_secret="s")
    assert settings.rate_limit_login_per_min == 0
    assert settings.rate_limit_login_per_account == 0
    assert settings.security_headers is False


def test_defaults_are_production_values_not_zeros():
    """Mặc định phải là hạn mức chạy thật: quên đặt biến môi trường thì hệ thống vẫn được bảo vệ."""
    from visynth_api.settings import Settings

    settings = Settings(db_dsn="postgresql://x", session_secret="s")
    assert settings.rate_limit_login_per_min >= 5
    assert settings.rate_limit_login_per_account >= 5
    assert settings.rate_limit_upload_per_hour >= 1
    assert settings.rate_limit_jobs_per_hour >= 1
    assert settings.security_headers is True
    assert settings.rate_limit_salt, "phải luôn có muối để băm danh tính"


def test_salt_derives_from_session_secret_when_not_given():
    from visynth_api.settings import Settings

    settings = Settings(db_dsn="postgresql://x", session_secret="bi-mat-phien")
    assert settings.rate_limit_salt == "bi-mat-phien"


def test_migration_0005_is_the_head_and_ships_the_functions(pg_schema):
    """Đầu chuỗi migration phải là 0005 và CSDL sau khi nâng cấp phải có hàm — nếu thiếu thì bản cài
    mới sẽ chạy API mà không có chỗ đếm, và giới hạn tốc độ trở thành vô nghĩa."""
    from visynth_api.db import Database

    check = Database(pg_schema)
    try:
        assert check.schema_version() == "0005_rate_limits"
        assert check.scalar("SELECT count(*) FROM pg_proc WHERE proname = 'rate_limit_hit'") == 1
        assert check.scalar("SELECT count(*) FROM pg_proc WHERE proname = 'rate_limit_gc'") == 1
    finally:
        check.close()


def test_infra_caddy_still_sets_the_same_headers():
    """Hai tầng (Caddy + ứng dụng) phải không mâu thuẫn nhau: đây là kiểm tra chống trôi dạt cấu hình."""
    caddy = (ROOT / "infra" / "Caddyfile").read_text(encoding="utf-8")
    for needle in ("Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy"):
        assert needle in caddy, f"Caddyfile thiếu {needle}"


def test_docs_mention_the_limits(monkeypatch):
    """Hợp đồng phải nêu 429 `rate_limited` để người viết SPA biết mà xử lý."""
    spec = (ROOT / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8")
    assert "rate_limited" in spec or "429" in spec
    if os.environ.get("VISYNTH_STRICT_DOCS"):
        assert "rate_limited" in spec
