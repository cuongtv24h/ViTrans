"""CSDL thật (M1): baseline Alembic + các quy tắc nghiệp vụ phải đúng dưới tranh chấp.

Chạy trên PostgreSQL thật (fixture `db` trong `conftest.py`). Đây là tầng cưỡng chế cuối cùng: dù API hay
worker có lỗi thì các ràng buộc/hàm ở đây vẫn phải giữ đúng tín dụng, hàng đợi và tính bất biến của lõi.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("db")


def _user(db, email="a@example.com", role="user", status="active") -> str:
    return db.scalar(
        "INSERT INTO users (email, role, status) VALUES (%s, %s, %s) RETURNING id",
        (email, role, status),
    )


def test_baseline_migration_applied_everything(db):
    tables = db.scalar(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    assert tables >= 40
    assert (
        db.scalar(
            "SELECT count(*) FROM pg_proc WHERE proname IN ('charge_credits','refund_credits','redeem_invite','claim_tasks','reclaim_stale_tasks','pool_try_reserve','publish_glossary_release')"
        )
        == 7
    )
    assert float(db.scalar("SELECT count(*) FROM llm_prices")) >= 1


def test_credit_charge_refund_and_idempotency(db):
    user = _user(db)
    db.execute("INSERT INTO invite_codes (code, credits_grant, max_uses) VALUES ('WELCOME-2026', 5, 1)")
    assert db.scalar("SELECT redeem_invite('welcome-2026', %s)", (user,)) == 5
    assert db.scalar("SELECT credit_balance(%s)", (user,)) == 5
    with pytest.raises(Exception, match="invite_exhausted"):
        other = _user(db, "b@example.com")
        db.scalar("SELECT redeem_invite('WELCOME-2026', %s)", (other,))

    job = _make_job(db, user, credits=3)
    assert db.scalar("SELECT charge_credits(%s, 3, %s, 'job:x')", (user, job)) == 2
    # gọi lặp cùng khoá idempotency: KHÔNG trừ thêm
    assert db.scalar("SELECT charge_credits(%s, 3, %s, 'job:x')", (user, job)) == 2
    with pytest.raises(Exception, match="insufficient_credits"):
        db.scalar("SELECT charge_credits(%s, 99, %s, 'job:y')", (user, job))
    assert db.scalar("SELECT refund_credits(%s, 3, %s, 'refund:x')", (user, job)) == 3
    assert db.scalar("SELECT refund_credits(%s, 3, %s, 'refund:x')", (user, job)) == 0  # idempotent
    assert db.scalar("SELECT credit_balance(%s)", (user,)) == 5
    # không bao giờ hoàn quá số đã trừ
    assert db.scalar("SELECT refund_credits(%s, 99, %s, 'refund:z')", (user, job)) == 0


def test_claim_tasks_is_exclusive_and_respects_per_job_limit(db):
    user = _user(db)
    db.execute("INSERT INTO invite_codes (code, credits_grant, max_uses) VALUES ('INVITE-AAAAA', 10, 1)")
    job = _make_job(db, user, credits=1)
    assert db.scalar("SELECT redeem_invite('INVITE-AAAAA', %s)", (user,)) == 10
    assert db.scalar("SELECT charge_credits(%s, 1, %s, 'job:1')", (user, job)) == 9
    db.execute("UPDATE jobs SET status = 'running' WHERE id = %s", (job,))
    for stage in ("profile", "glossary", "map"):
        db.execute("INSERT INTO job_stages (job_id, stage) VALUES (%s, %s)", (job, stage))
        db.execute("INSERT INTO job_tasks (job_id, stage, task_key) VALUES (%s, %s, %s)", (job, stage, stage))

    first = db.all("SELECT * FROM claim_tasks('w1', 5, 1)")
    assert [t["stage"] for t in first] == ["profile"] and first[0]["attempt"] == 1
    second = db.all("SELECT * FROM claim_tasks('w2', 5, 1)")
    assert second == []  # mỗi job tối đa 1 task đang chạy
    db.execute("UPDATE job_tasks SET status = 'succeeded' WHERE id = %s", (first[0]["id"],))
    assert [t["stage"] for t in db.all("SELECT * FROM claim_tasks('w2', 5, 1)")] == ["glossary"]


def test_reclaim_returns_orphan_tasks_and_fails_exhausted_ones(db):
    user = _user(db)
    job = _make_job(db, user, credits=1)
    db.execute("UPDATE jobs SET status = 'running' WHERE id = %s", (job,))
    db.execute("INSERT INTO job_stages (job_id, stage) VALUES (%s, 'profile')", (job,))
    db.execute("INSERT INTO job_tasks (job_id, stage, task_key) VALUES (%s, 'profile', 'profile')", (job,))
    task = db.one("SELECT * FROM claim_tasks('w1', 1, 1)")
    db.execute("UPDATE job_tasks SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (task["id"],))

    assert db.scalar("SELECT reclaim_stale_tasks()") == 1
    row = db.one("SELECT status, error_code, attempt FROM job_tasks WHERE id = %s", (task["id"],))
    assert row == {"status": "pending", "error_code": "lease_expired", "attempt": 1}

    db.execute(
        "UPDATE job_tasks SET status = 'running', attempt = max_attempts, heartbeat_at = now() - interval '10 minutes' WHERE id = %s",
        (task["id"],),
    )
    db.scalar("SELECT reclaim_stale_tasks()")
    assert db.one("SELECT status FROM job_tasks WHERE id = %s", (task["id"],))["status"] == "failed"


def test_approved_style_core_version_is_immutable_in_the_database(db):
    """Trigger `style_core_version_guard` (§19.3) — bản đã duyệt không sửa, không xoá, không mở lại."""
    curator = _user(db, "curator@example.com", role="curator")
    core = db.scalar(
        "INSERT INTO style_cores (slug, scope, name, domain) VALUES ('loi-thu', 'system', 'Lõi thử', 'education') RETURNING id"
    )
    version = db.scalar(
        """INSERT INTO style_core_versions (style_core_id, version, content, content_sha256, created_by, status, approved_by, approved_at)
           VALUES (%s, '0.1.0', '{}'::jsonb, 'sha', %s, 'approved', %s, now()) RETURNING id""",
        (core, curator, curator),
    )
    with pytest.raises(Exception, match="approved_version_cannot_be_reopened"):
        db.execute("UPDATE style_core_versions SET status = 'draft' WHERE id = %s", (version,))
    with pytest.raises(Exception, match="approved_version_is_immutable"):
        db.execute("UPDATE style_core_versions SET content = '{\"x\": 1}'::jsonb WHERE id = %s", (version,))
    with pytest.raises(Exception, match="approved_version_is_immutable"):
        db.execute("DELETE FROM style_core_versions WHERE id = %s", (version,))
    # ngừng dùng thì được
    db.execute("UPDATE style_core_versions SET status = 'deprecated' WHERE id = %s", (version,))
    assert db.scalar("SELECT status FROM style_core_versions WHERE id = %s", (version,)) == "deprecated"


def test_glossary_release_contains_only_confirmed_entries(db):
    curator = _user(db, "curator2@example.com", role="curator")
    glossary = db.scalar("INSERT INTO glossaries (scope, name) VALUES ('shared', 'Chuẩn') RETURNING id")
    for term, status in (("Cổng 55", "confirmed"), ("Gate 55", "rejected"), ("Solar Plexus", "suggested")):
        db.execute(
            "INSERT INTO glossary_entries (glossary_id, source_term, target_term, status) VALUES (%s, %s, %s, %s)",
            (glossary, term, term, status),
        )
    assert db.scalar("SELECT publish_glossary_release(%s, %s, 'v1')", (glossary, curator)) >= 1
    release = db.one("SELECT entries, entry_count FROM glossary_releases WHERE glossary_id = %s", (glossary,))
    assert release["entry_count"] == 1
    assert [e["source_term"] for e in release["entries"]] == ["Cổng 55"]
    assert db.scalar("SELECT version FROM glossaries WHERE id = %s", (glossary,)) == 1


def test_uploaded_document_expiry_purges_paragraphs(db):
    user = _user(db)
    doc = db.scalar(
        "INSERT INTO documents (user_id, title, source_type, expires_at) VALUES (%s, 'T', 'upload', now() - interval '1 day') RETURNING id",
        (user,),
    )
    db.execute(
        "INSERT INTO doc_paragraphs (document_id, pid, idx, kind, content, char_count) VALUES (%s, 'P000001', 1, 'body', 'x', 1)",
        (doc,),
    )
    assert db.scalar("SELECT purge_expired_documents()") == 1
    assert db.scalar("SELECT count(*) FROM doc_paragraphs WHERE document_id = %s", (doc,)) == 0
    assert db.scalar("SELECT status FROM documents WHERE id = %s", (doc,)) == "deleted"


def test_daily_spend_cap_pauses_when_reached(db):
    cap = db.scalar("SELECT (value #>> '{}')::numeric FROM app_settings WHERE key = 'daily_spend_cap_usd'")
    row = db.one("SELECT * FROM add_spend(%s)", (cap + 1,))
    assert row["paused"] is True
    assert db.scalar("SELECT paused FROM spend_daily WHERE day = current_date") is True


def _make_job(db, user: str, *, credits: int = 1) -> str:
    doc = db.scalar(
        "INSERT INTO documents (user_id, title, source_type, status, word_count) VALUES (%s, 'Tài liệu thử', 'upload', 'ready', 300) RETURNING id",
        (user,),
    )
    return db.scalar(
        """INSERT INTO jobs (user_id, document_id, level, est_credits, model_profile, prompt_versions)
           VALUES (%s, %s, 'deep_synthesis', %s, '{}'::jsonb, '{}'::jsonb) RETURNING id""",
        (user, doc, credits),
    )
