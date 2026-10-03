"""Ngưỡng cảnh báo (M3, §20.7): biến con số vận hành thành việc cần làm.

Cảnh báo là thứ dễ "trang trí": viết ra rồi không ai kiểm, hoặc ngưỡng đặt sai đến mức kêu liên tục
rồi mọi người học cách bỏ qua. Vì vậy bộ test này kiểm cả hai chiều: kêu khi đáng kêu, và **im** khi
mọi thứ bình thường — cùng với việc mỗi cảnh báo phải có câu "cần làm gì".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visynth.worker import alerts

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"

HEALTHY = {
    "tasks_pending": 0,
    "tasks_running": 2,
    "tasks_failed_1h": 0,
    "tasks_stale_running": 0,
    "jobs_active": 3,
    "jobs_waiting_30m": 0,
    "oldest_waiting_min": 2,
    "leases_expired": 0,
    "credentials_quarantined": 0,
    "deployments_open": 0,
    "users_negative_credits": 0,
    "rate_limit_blocks_1h": 0,
    "pool_incidents_1h": 0,
    "database_bytes": 100 * 1024 * 1024,
}


def test_healthy_system_makes_no_noise():
    assert alerts.evaluate(HEALTHY) == []
    assert alerts.status([]) == "ok"
    assert alerts.summarize([]) == "không có cảnh báo"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("credentials_quarantined", 1, "credential_quarantined"),
        ("users_negative_credits", 1, "negative_credit_balance"),
        ("tasks_stale_running", 1, "stale_running_tasks"),
        ("leases_expired", 3, "expired_leases"),
        ("deployments_open", 2, "circuit_open"),
        ("jobs_waiting_30m", 3, "jobs_waiting"),
        ("oldest_waiting_min", 90, "queue_stalled"),
        ("tasks_failed_1h", 9, "tasks_failing"),
        ("rate_limit_blocks_1h", 500, "rate_limit_pressure"),
        ("pool_incidents_1h", 40, "pool_unstable"),
        ("database_bytes", 2 * 1024**3, "database_growing"),
    ],
)
def test_each_signal_has_a_rule(field, value, code):
    found = alerts.evaluate({**HEALTHY, field: value})
    assert [item["code"] for item in found] == [code], f"{field} phải kêu đúng một cảnh báo"
    assert found[0]["value"] == value
    assert found[0]["action"], "mỗi cảnh báo phải nói CẦN LÀM GÌ, không chỉ nói có vấn đề"


def test_credentials_quarantined_is_critical_and_dominates_status():
    found = alerts.evaluate({**HEALTHY, "credentials_quarantined": 1, "tasks_failed_1h": 6})
    assert [item["severity"] for item in found] == ["critical", "warn"], "mức nặng phải xếp trước"
    assert alerts.status(found) == "critical"
    assert alerts.status(alerts.evaluate({**HEALTHY, "jobs_waiting_30m": 5})) == "degraded"


def test_thresholds_can_be_tuned_and_disabled_by_environment(monkeypatch):
    monkeypatch.setenv("VISYNTH_ALERT_TASKS_FAILED_1H", "50")
    monkeypatch.setenv("VISYNTH_ALERT_JOBS_WAITING_30M", "-1")
    found = alerts.evaluate({**HEALTHY, "tasks_failed_1h": 9, "jobs_waiting_30m": 7})
    assert found == [], "siết ngưỡng thì không kêu, tắt bằng -1 thì im hẳn"


def test_negative_one_disables_only_that_rule(monkeypatch):
    limits = alerts.thresholds_from_env()
    limits["tasks_failed_1h"] = -1
    found = alerts.evaluate({**HEALTHY, "tasks_failed_1h": 99, "users_negative_credits": 1}, limits)
    assert [item["code"] for item in found] == ["negative_credit_balance"]


def test_missing_signals_are_skipped_not_guessed():
    """Bản cũ/khác môi trường có thể thiếu khoá — không được vì thế mà kêu bừa."""
    assert alerts.evaluate({"jobs_waiting_30m": 0}) == []
    assert alerts.evaluate({}) == []


def test_rule_list_matches_health_fields():
    """Mọi ngưỡng phải trỏ vào một khoá CÓ THẬT trong `ops.health` — nếu không thì cảnh báo chết lặng."""
    source = (ROOT / "apps" / "worker" / "visynth" / "worker" / "ops.py").read_text(encoding="utf-8")
    health_block = source.split("def health(")[1].split("def as_json")[0]
    for rule in alerts.RULES:
        # Hai cách `ops.health` đưa một khoá vào payload: alias SQL (`AS x`) hoặc gán trong Python.
        present = f"AS {rule.field}" in health_block or f'"{rule.field}"]' in health_block
        assert present, f"ops.health không trả `{rule.field}` cho cảnh báo `{rule.code}`"


def test_healthz_exposes_alerts_without_leaking_numbers_by_default(pg_schema):
    from visynth_api.app import create_app
    from visynth_api.settings import Settings

    settings = Settings(db_dsn=pg_schema, session_secret="test-secret", web_dir=WEB)
    application = create_app(settings)
    with TestClient(application) as client:
        response = client.get("/api/v1/healthz")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok", "hệ thống vừa cài đặt không được báo động"
        assert payload["alerts"] == []
        assert "signals" not in payload, "mặc định không lộ số liệu nội bộ"

        detailed = client.get("/api/v1/healthz?detail=1").json()
        assert detailed["signals"]["jobs_waiting_30m"] == 0
        assert detailed["signals"]["database_bytes"] > 0
    application.state.db.close()


def test_healthz_turns_red_when_a_credential_is_dead(pg_schema, db):
    """Đây là bài kiểm quan trọng nhất của phần quan sát: có sự cố thật thì `/healthz` phải đổi màu."""
    from visynth_api.app import create_app
    from visynth_api.settings import Settings

    db.execute(
        """INSERT INTO llm_providers (id, kind, base_url, display_name)
           VALUES ('gemini', 'gemini_native', 'https://generativelanguage.googleapis.com', 'Gemini')"""
    )
    db.execute(
        """INSERT INTO llm_quota_groups (id, provider_id, label, tier) VALUES ('g1', 'gemini', 'nhóm', 'free')"""
    )
    db.execute(
        """INSERT INTO llm_credentials (id, group_id, label, secret_enc, status, quarantined_reason)
           VALUES ('g1-k1', 'g1', 'khoá chết', '\\x00'::bytea, 'quarantined', 'auth_error')"""
    )

    settings = Settings(db_dsn=pg_schema, session_secret="test-secret", web_dir=WEB)
    application = create_app(settings)
    with TestClient(application) as client:
        payload = client.get("/api/v1/healthz").json()
        assert payload["status"] == "critical"
        assert "credential_quarantined" in payload["alerts"]
    application.state.db.close()


def test_cli_health_exits_three_when_alerts_fire(pg_schema, db, capsys):
    """Cron/giám sát chỉ cần nhìn mã thoát: 0 = yên, 3 = có việc phải xem."""
    from visynth.cli import cmd_ops

    class Args:
        db_dsn = pg_schema
        ops_cmd = "health"
        json = False
        dry_run = False
        stale = "3 minutes"

    assert cmd_ops(Args()) == 0
    assert "không có cảnh báo" not in capsys.readouterr().out  # yên thì không in gì thừa

    user_id = str(db.scalar("INSERT INTO users (email, role, status) VALUES ('a@b.c','user','active') RETURNING id"))
    db.execute("INSERT INTO credit_ledger (user_id, delta, reason) VALUES (%s, -5, 'admin_adjust')", (user_id,))
    assert cmd_ops(Args()) == alerts.ALERT_EXIT_CODE
    output = capsys.readouterr().out
    assert "negative_credit_balance" in output
    assert "cần" in output or "kiểm" in output or "rá soát" in output, "phải in việc cần làm"
