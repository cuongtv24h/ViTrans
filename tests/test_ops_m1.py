"""Bảo trì định kỳ trên PostgreSQL thật (M1, SPEC §20.2/§20.7): `visynth ops …`.

Đây là các việc mà cron trên VPS gọi mỗi phút/mỗi giờ, nên bất biến quan trọng là: chúng **không
bao giờ** làm mất dữ liệu còn hạn, luôn trả mã thoát dễ thấy khi CSDL hỏng, và `health` phát hiện
được các dấu hiệu cần người xem (job chờ lâu, ví âm, khoá bị cách ly).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from visynth.llm.base import Outcome
from visynth.worker import ops


@pytest.fixture
def dsn(pg_schema):
    return pg_schema


def _user(db, email: str = "nguoidung@example.com") -> str:
    return str(
        db.scalar(
            "INSERT INTO users (email, role, status) VALUES (%s, 'user', 'active') RETURNING id",
            (email,),
        )
    )


def test_reap_returns_orphan_task_to_queue_and_frees_lease(dsn, db):
    user_id = _user(db)
    db.execute(
        """INSERT INTO documents (id, user_id, title, source_type, status)
           VALUES ('11111111-1111-1111-1111-111111111111', %s, 'tài liệu', 'upload', 'ready')""",
        (user_id,),
    )
    db.execute(
        """INSERT INTO jobs (id, user_id, document_id, level, model_profile, prompt_versions, status, est_credits)
           VALUES ('22222222-2222-2222-2222-222222222222', %s,
                   '11111111-1111-1111-1111-111111111111', 'detailed_synthesis', '{"name": "balanced"}',
                   '{}'::jsonb, 'running', 1)""",
        (user_id,),
    )
    # `job_tasks` có khoá ngoại tới `job_stages` nên phải khai giai đoạn trước
    db.execute(
        """INSERT INTO job_stages (job_id, stage, status)
           VALUES ('22222222-2222-2222-2222-222222222222', 'map', 'running'),
                  ('22222222-2222-2222-2222-222222222222', 'write', 'running')"""
    )
    db.execute(
        """INSERT INTO job_tasks (job_id, stage, task_key, status, attempt, max_attempts, locked_by, heartbeat_at)
           VALUES ('22222222-2222-2222-2222-222222222222', 'map', '0:4', 'running', 1, 3, 'worker-đã-chết',
                   now() - interval '10 minutes')"""
    )
    # task đã hết lượt thử → phải thành `failed` chứ không quay lại hàng đợi vô hạn
    db.execute(
        """INSERT INTO job_tasks (job_id, stage, task_key, status, attempt, max_attempts, locked_by, heartbeat_at)
           VALUES ('22222222-2222-2222-2222-222222222222', 'write', '0:1', 'running', 3, 3, 'worker-đã-chết',
                   now() - interval '10 minutes')"""
    )
    # chỗ đặt (lease) trỏ tới deployment thật nên phải có chuỗi provider → model → nhóm → deployment
    db.execute(
        """INSERT INTO llm_providers (id, kind, base_url, display_name)
           VALUES ('gemini', 'gemini_native', 'https://generativelanguage.googleapis.com', 'Gemini')"""
    )
    db.execute(
        """INSERT INTO llm_models (id, provider_id, model_id, ctx_in, max_out)
           VALUES ('gemini:flash', 'gemini', 'gemini-3.8-flash', 1000000, 65536)"""
    )
    db.execute(
        """INSERT INTO llm_quota_groups (id, provider_id, label, tier)
           VALUES ('free-gemini', 'gemini', 'nhóm miễn phí', 'free')"""
    )
    db.execute(
        """INSERT INTO llm_deployments (id, group_id, model_id)
           VALUES ('free-gemini/flash', 'free-gemini', 'gemini:flash')"""
    )
    db.execute(
        """INSERT INTO llm_credentials (id, group_id, label, secret_ref)
           VALUES ('free-gemini-k1', 'free-gemini', 'khoá 1', 'env:VISYNTH_TEST_KEY')"""
    )
    db.execute(
        """INSERT INTO llm_leases (deployment_id, credential_id, basis_tokens, out_tokens, priority, created_at, expires_at)
           VALUES ('free-gemini/flash', 'free-gemini-k1', 100, 10, 'normal', 0, %s)""",
        (0.0,),
    )

    preview = ops.reap(dsn, dry_run=True)
    assert preview == {"tasks": 2, "leases": 1, "rate_limit_rows": 0, "applied": 0}
    assert db.scalar("SELECT count(*) FROM job_tasks WHERE status = 'pending'") == 0, "xem trước không ghi gì"

    result = ops.reap(dsn, now=0.0)
    assert result["tasks"] == 2 and result["leases"] == 1
    assert db.scalar("SELECT status FROM job_tasks WHERE stage = 'map'") == "pending"
    assert db.scalar("SELECT status FROM job_tasks WHERE stage = 'write'") == "failed"
    assert db.scalar("SELECT count(*) FROM llm_leases") == 0

    # Chạy lại không còn gì để thu hồi (idempotent)
    assert ops.reap(dsn, now=0.0) == {"tasks": 0, "leases": 0, "rate_limit_rows": 0, "applied": 1}


def test_purge_only_removes_expired_content(dsn, db):
    user_id = _user(db)
    for key, expires in (("tài-liệu-hết-hạn.txt", "now() - interval '1 hour'"), ("tài-liệu-còn-hạn.txt", None)):
        db.execute(
            f"""INSERT INTO documents (id, user_id, title, source_type, status, storage_key, expires_at)
                VALUES (gen_random_uuid(), %s, 'tài liệu', 'upload', 'ready', %s,
                        {expires if expires else "now() + interval '1 day'"})""",
            (user_id, key),
        )
    db.execute(
        """INSERT INTO doc_paragraphs (document_id, pid, idx, kind, content, char_count)
           SELECT id, 'P000001', 0, 'body', 'nội dung gốc', 12 FROM documents WHERE storage_key = 'tài-liệu-hết-hạn.txt'"""
    )

    preview = ops.purge(dsn, dry_run=True)
    assert preview["documents"] == 1 and preview["applied"] == 0
    assert preview["storage_keys"] == ["tài-liệu-hết-hạn.txt"] or preview["storage_keys"] == []
    assert db.scalar("SELECT count(*) FROM doc_paragraphs") == 1, "xem trước không xoá gì"

    result = ops.purge(dsn)
    assert result == {"documents": 1, "applied": 1}
    assert db.scalar("SELECT count(*) FROM doc_paragraphs") == 0, "nội dung gốc hết hạn phải bị xoá"
    assert db.scalar("SELECT count(*) FROM documents WHERE status = 'deleted'") == 1
    assert db.scalar("SELECT count(*) FROM documents WHERE status <> 'deleted'") == 1, "bản còn hạn giữ nguyên"


def test_health_reports_signals_and_never_leaks_content(dsn, db):
    user_id = _user(db)
    # Ví âm: số dư là tổng `credit_ledger` (không có cột `credit_balance` trên `users`)
    db.execute("INSERT INTO credit_ledger (user_id, delta, reason) VALUES (%s, -2, 'admin_adjust')", (user_id,))
    db.execute(
        """INSERT INTO glossaries (id, owner_id, scope, name) VALUES (gen_random_uuid(), NULL, 'shared', 'Chuẩn')"""
    )
    db.execute(
        """INSERT INTO llm_providers (id, kind, base_url, display_name)
           VALUES ('gemini', 'gemini_native', 'https://generativelanguage.googleapis.com', 'Gemini')
           ON CONFLICT DO NOTHING"""
    )
    db.execute(
        """INSERT INTO llm_quota_groups (id, provider_id, label, tier) VALUES ('g1', 'gemini', 'nhóm', 'free')
           ON CONFLICT DO NOTHING"""
    )
    db.execute(
        """INSERT INTO llm_credentials (id, group_id, label, secret_enc, status)
           VALUES ('g1-k1', 'g1', 'khoá', '\\x00'::bytea, 'quarantined') ON CONFLICT DO NOTHING"""
    )

    payload = ops.health(dsn)
    assert payload["users_negative_credits"] == 1
    assert payload["credentials_quarantined"] == 1
    assert payload["tasks_pending"] == 0 and payload["jobs_active"] == 0
    assert payload["database_bytes"] > 0 and payload["server_version"]
    text = json.dumps(payload, ensure_ascii=False, default=str)
    assert "nội dung gốc" not in text and "tài-liệu" not in text, "health không được lộ nội dung tài liệu"


def test_pool_from_db_resolves_encrypted_keys_and_writes_llm_calls(dsn, db):
    """Đường chạy của VPS: worker đọc pool TỪ CSDL, khoá giải mã tại chỗ, mọi lời gọi vào `llm_calls`."""
    from visynth.pool import dbstore
    from visynth.pool.client import PooledLLMClient
    from visynth.pool.pgstore import PgStore
    from visynth.pool.secrets import encrypt_secret, fingerprint, load_master_key, new_master_key

    key_text = new_master_key()  # 32 byte ngẫu nhiên, chỉ tồn tại trong test
    master = load_master_key(key_text)
    secret = "AIzaSyTEST-KHONG-THAAT-0000000000000000"

    db.execute(
        """INSERT INTO llm_providers (id, kind, base_url, display_name)
           VALUES ('gemini', 'gemini_native', 'https://generativelanguage.googleapis.com', 'Gemini')"""
    )
    db.execute(
        """INSERT INTO llm_models (id, provider_id, model_id, ctx_in, max_out, structured, quality)
           VALUES ('gemini:flash', 'gemini', 'gemini-3.8-flash', 1000000, 65536, 'json_schema',
                   '{"json": 0.9, "vi_write": 0.9, "long_context": 0.9}'::jsonb)"""
    )
    db.execute(
        """INSERT INTO llm_quota_groups (id, provider_id, label, tier, data_policy)
           VALUES ('tra-phi', 'gemini', 'Gemini trả phí', 'paid', 'no_training')"""
    )
    db.execute(
        """INSERT INTO llm_deployments (id, group_id, model_id) VALUES ('tra-phi/flash', 'tra-phi', 'gemini:flash')"""
    )
    db.execute(
        """INSERT INTO llm_credentials (id, group_id, label, secret_enc, last4, fingerprint)
           VALUES ('tra-phi-k1', 'tra-phi', 'khoá chính', %s, %s, %s)""",
        (encrypt_secret(master, "tra-phi-k1", secret), secret[-4:], fingerprint(master, secret)),
    )
    db.execute(
        """INSERT INTO llm_profiles (name, needs) VALUES ('fast', '{"structured": "json_object",
           "min_ctx_in": 30000, "vision": false, "pdf": false, "min_quality": {"json": 0.5}}'::jsonb)"""
    )
    db.execute(
        """INSERT INTO llm_profile_tiers (profile_name, tier_no, name, select_group_tiers, strategy)
           VALUES ('fast', 1, 'paid', '{paid}', 'ordered')"""
    )

    store = PgStore(dsn)
    try:
        client = PooledLLMClient.from_db(store, master, gate="A")
        assert client.credential_refs == {"tra-phi-k1": "enc:tra-phi-k1"}
        # Khoá được giải mã ĐÚNG từ `secret_enc` của CSDL, không lấy từ biến môi trường
        assert client.registry.resolve("enc:tra-phi-k1") == secret
        assert "tra-phi/flash" in {d.id for d in client.model.deployments}

        request = SimpleNamespace(prompt_id="P3_write", metadata={})
        row = client._row(None, request, Outcome("rate_limited_minute", tokens_in=10))
    finally:
        store.close()

    ledger = dbstore.DbLedger(db)
    ledger.append({**row, "detail": "NỘI DUNG PHẢN HỒI KHÔNG ĐƯỢC GHI", "model": "gemini-3.8-flash"})
    saved = db.one("SELECT * FROM llm_calls")
    assert saved["status"] == "retryable_error" and saved["outcome"] == "rate_limited_minute"
    assert saved["prompt_id"] == "P3_write" and saved["tokens_in"] == 10
    assert "NỘI DUNG" not in str(saved.values()), "sổ llm_calls tuyệt đối không lưu nội dung"
    assert ledger.written == 1 and ledger.totals()["calls"] == 1
