"""API + worker trên PostgreSQL thật (M1): đăng ký → tải lên → tạo job → worker → báo cáo.

Kiểm luôn các bất biến quan trọng: Idempotency-Key không trừ tiền hai lần, lease/backoff, hoàn
tín dụng khi huỷ/hỏng, sự kiện SSE khớp `job_event.schema.json`, và hàng đợi không rò nội dung.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visynth.llm.base import LLMError, Outcome
from visynth.worker import JobWorker, WorkerStore

FIXTURE = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "demo_lecture.txt"


def demo_text() -> str:
    """Nội dung tài liệu mẫu trong repo (đọc một lần cho mỗi test)."""
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def app(pg_schema, tmp_path):
    from visynth_api.app import create_app
    from visynth_api.settings import Settings

    settings = Settings(
        db_dsn=pg_schema,
        session_secret="test-secret",
        upload_dir=tmp_path / "uploads",
        open_signup=True,
        signup_credits=5,
    )
    application = create_app(settings)
    yield application
    application.state.db.close()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _register(client: TestClient, email: str = "nguoidung@example.com") -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "matkhau-du-manh",
            "display_name": "Người dùng thử",
            "tos_version": "2026-10-01",
            "consent_cross_border": True,
            "consent_shared_processing": True,
            "age_confirmed": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client: TestClient, sample_text: str) -> dict:
    response = client.post(
        "/api/v1/documents",
        files={"file": ("bai-giang.txt", sample_text.encode("utf-8"), "text/plain")},
        data={"rights_attested": "true", "title": "Bài giảng thử"},
    )
    assert response.status_code == 201, response.text
    return response.json()["document"]


def _make_admin(db, email: str = "quantri@example.com") -> None:
    db.execute(
        "INSERT INTO users (email, role, status) VALUES (%s, 'admin', 'active')",
        (email,),
    )


def _client_factory_ok(store: WorkerStore):
    from visynth.eval import demo_client

    return lambda job: demo_client(store.load_extraction(job))


# --------------------------------------------------------------------------- luồng chính


def test_end_to_end_job_runs_to_report(client, db):
    me = _register(client)
    assert me["user"]["credits"] == 5

    document = _upload(client, demo_text())
    assert document["status"] == "ready"
    assert document["word_count"] > 100
    assert db.scalar("SELECT count(*) FROM doc_paragraphs WHERE document_id = %s", (document["id"],)) > 0
    assert db.scalar("SELECT count(*) FROM doc_sections WHERE document_id = %s", (document["id"],)) >= 1

    quote = client.post("/api/v1/jobs/estimate", json={"document_id": document["id"], "level": "deep_synthesis"})
    assert quote.status_code == 200, quote.text
    credits = quote.json()["credits"]

    created = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "deep_synthesis"},
        headers={"Idempotency-Key": "lan-dau-12345678"},
    )
    assert created.status_code == 202, created.text
    job = created.json()["job"]
    assert created.json()["balance"] == 5 - credits
    assert job["status"] == "queued"

    # Gọi lặp cùng khoá: trả lại đúng job đó, KHÔNG trừ thêm tín dụng.
    replay = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "deep_synthesis"},
        headers={"Idempotency-Key": "lan-dau-12345678"},
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["job"]["id"] == job["id"]
    assert replay.json()["balance"] == 5 - credits

    store = WorkerStore(db.dsn, worker_id="worker-test")
    try:
        worker = JobWorker(store, _client_factory_ok(store), limit=1)
        outcome = worker.run_once()
        assert outcome["claimed"] == 1
        assert outcome["results"][0]["report_id"]
    finally:
        store.close()

    detail = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert detail["job"]["status"] == "succeeded"
    assert detail["job"]["quality_grade"] in ("A", "B")
    assert detail["job"]["progress"] == 100
    assert detail["report_id"]
    stages = {row["stage"]: row["status"] for row in detail["stages"]}
    assert stages["profile"] == "succeeded"
    assert stages["repair"] == "succeeded"
    assert stages["extract"] == "skipped"
    assert stages["translate"] == "skipped"

    report = client.get(f"/api/v1/reports/{detail['report_id']}").json()
    assert report["title"] and report["sections"] and report["markdown"].startswith("#")
    assert report["ai_notice"].startswith("Nội dung do AI tổng hợp")
    assert report["sections"][0]["blocks"], "phải có khối nội dung"
    assert client.get("/api/v1/reports").json()["items"]

    # Sự kiện: khớp schema + có đủ mốc chính.
    from visynth.prompts import default_schemas_dir
    from visynth.structured import SchemaStore

    schema_store = SchemaStore(default_schemas_dir())
    schema = schema_store.load("job_event.schema.json")
    events = client.get(f"/api/v1/jobs/{job['id']}/events").text
    kinds = [line.split(": ", 1)[1] for line in events.splitlines() if line.startswith("event: ")]
    assert "job_queued" in kinds
    assert "stage_started" in kinds
    assert "stage_completed" in kinds
    assert kinds[-1] == "job_succeeded"
    rows = db.all(
        "SELECT id, type, stage, payload, created_at FROM job_events WHERE job_id = %s ORDER BY id", (job["id"],)
    )
    for row in rows:
        payload = dict(row["payload"])
        payload.setdefault("id", row["id"])
        payload.setdefault("type", row["type"])
        payload.setdefault("ts", row["created_at"].isoformat())
        payload.setdefault("job_id", str(job["id"]))
        schema_store.validate(payload, schema, what="job_event")
        # Không rò nội dung tài liệu vào hàng đợi sự kiện (SPEC §13.3, §14).
        assert "Trí tuệ" not in json.dumps(payload, ensure_ascii=False) or "data" not in payload

    ledger = client.get("/api/v1/credits").json()
    assert ledger["balance"] == 5 - credits
    assert {row["reason"] for row in ledger["items"]} == {"job_charge", "grant_signup"}

    # Chi phí & trần chi tiêu: job ghi chi phí "bóng" theo bảng giá, `spend_daily` được cộng.
    assert detail["job"]["actual_shadow_usd"] > 0
    assert float(db.scalar("SELECT cost_usd FROM spend_daily WHERE day = current_date") or 0) > 0


def test_upload_and_job_guards(client, db):
    _register(client)
    # Thiếu xác nhận quyền sử dụng → 422
    denied = client.post(
        "/api/v1/documents",
        files={"file": ("a.txt", b"noi dung", "text/plain")},
        data={"rights_attested": "false"},
    )
    assert denied.status_code == 422 and denied.json()["code"] == "rights_not_attested"
    # PDF scan cần OCR (M2)
    pdf = client.post(
        "/api/v1/documents",
        files={"file": ("a.pdf", b"%PDF-1.4 fake", "application/pdf")},
        data={"rights_attested": "true"},
    )
    assert pdf.status_code == 415 and pdf.json()["code"] == "pdf_requires_ocr"

    document = _upload(client, "Một đoạn ngắn. " * 40)
    # Thiếu Idempotency-Key → 422 của FastAPI
    assert (
        client.post("/api/v1/jobs", json={"document_id": document["id"], "level": "executive_brief"}).status_code == 422
    )
    # Không đủ tín dụng: tài liệu lớn hơn số dư
    big = _upload(client, "Câu ví dụ về trí tuệ nhân tạo. " * 900)
    too_big = client.post(
        "/api/v1/jobs",
        json={"document_id": big["id"], "level": "deep_synthesis"},
        headers={"Idempotency-Key": "khong-du-tien-0001"},
    )
    assert too_big.status_code == 402 and too_big.json()["code"] == "insufficient_credits"
    assert db.scalar("SELECT count(*) FROM jobs WHERE document_id = %s", (big["id"],)) == 0


def test_cancel_before_start_refunds(client, db):
    _register(client)
    document = _upload(client, demo_text())
    created = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "deep_synthesis"},
        headers={"Idempotency-Key": "huy-job-00000001"},
    ).json()
    job = created["job"]

    canceled = client.post(f"/api/v1/jobs/{job['id']}/cancel")
    assert canceled.status_code == 202, canceled.text
    assert canceled.json()["job"]["status"] == "canceled"
    assert canceled.json()["refunded_credits"] == job["charged_credits"]
    assert client.get("/api/v1/me").json()["credits"] == 5
    events = client.get(f"/api/v1/jobs/{job['id']}/events").text
    assert "event: job_canceled" in events


# --------------------------------------------------------------------------- worker


def test_retryable_error_keeps_task_pending_then_fails_job(client, db):
    _register(client)
    document = _upload(client, demo_text())
    job = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "deep_synthesis"},
        headers={"Idempotency-Key": "loi-tam-thoi-0001"},
    ).json()["job"]

    class Hỏng:
        def complete(self, request):
            raise LLMError(Outcome("server_error"), detail="nhà cung cấp sập")

    store = WorkerStore(db.dsn, worker_id="worker-loi")
    try:
        worker = JobWorker(store, lambda job: Hỏng(), limit=1)
        worker.run_once()
        task = db.one(
            "SELECT status, attempt, run_after > now() AS waiting, error_code, stage FROM job_tasks "
            "WHERE job_id = %s ORDER BY id LIMIT 1",
            (job["id"],),
        )
        assert task["status"] == "pending", "lỗi tạm thời phải được thử lại"
        assert task["attempt"] == 1
        assert task["waiting"] is True
        assert client.get(f"/api/v1/jobs/{job['id']}").json()["job"]["status"] == "running"

        # Hết lượt thử → job `failed` + hoàn đủ tín dụng
        db.execute("UPDATE job_tasks SET max_attempts = 1, run_after = now() WHERE job_id = %s", (job["id"],))
        db.execute("UPDATE jobs SET error_code = NULL")
        worker.run_once()
        final = client.get(f"/api/v1/jobs/{job['id']}").json()["job"]
        assert final["status"] == "failed"
        assert final["error_code"] in ("PipelineError", "LLMError")
        assert "server_error" in (final["error_message"] or "")
        assert final["refunded_credits"] == final["charged_credits"]
        assert client.get("/api/v1/me").json()["credits"] == 5
        assert db.scalar("SELECT count(*) FROM job_tasks WHERE job_id = %s AND status = 'failed'", (job["id"],)) >= 1
    finally:
        store.close()


def test_worker_reclaims_expired_lease(client, db):
    _register(client)
    document = _upload(client, demo_text())
    job = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "deep_synthesis"},
        headers={"Idempotency-Key": "lease-het-han-0001"},
    ).json()["job"]
    store = WorkerStore(db.dsn, worker_id="worker-1")
    try:
        store.promote_queued(1)
        claimed = store.claim(limit=1, per_job_limit=1)
        assert len(claimed) == 1
        # Worker "chết": heartbeat cũ → lần chạy sau thu hồi và chạy lại
        db.execute(
            "UPDATE job_tasks SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (claimed[0]["id"],)
        )
        assert store.reclaim_stale("3 minutes") == 1
        assert db.scalar("SELECT status FROM job_tasks WHERE id = %s", (claimed[0]["id"],)) == "pending"
        store.reclaim_stale("3 minutes")  # không còn gì để thu hồi
        assert store.claim(limit=1, per_job_limit=1)[0]["attempt"] == 2
    finally:
        store.close()
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["job"]["status"] == "running"


# --------------------------------------------------------------------------- glossary + quản trị


def test_glossary_crud_and_csv_roundtrip(client, db):
    _register(client)
    created = client.post(
        "/api/v1/glossaries",
        json={"name": "Thuật ngữ AI", "description": "Từ điển cá nhân", "domain": "ai"},
    )
    assert created.status_code == 201, created.text
    glossary_id = created.json()["id"]
    assert client.get("/api/v1/glossaries").json()["items"][0]["read_only"] is False

    added = client.post(
        f"/api/v1/glossaries/{glossary_id}/entries",
        json={"source_term": "transformer", "target_term": "máy biến áp", "forbidden_variants": ["transfomer"]},
    )
    assert added.status_code == 201, added.text
    duplicate = client.post(
        f"/api/v1/glossaries/{glossary_id}/entries",
        json={"source_term": "Transformer", "target_term": "bộ chuyển đổi"},
    )
    assert duplicate.status_code == 409

    exported = client.get(f"/api/v1/glossaries/{glossary_id}/export")
    assert exported.status_code == 200 and "transformer" in exported.text
    csv_body = "source_term,target_term,keep_original,forbidden_variants,term_type\nattention,chú ý,false,attencion|attention,concept\n"
    imported = client.post(
        f"/api/v1/glossaries/{glossary_id}/import",
        files={"file": ("glossary.csv", csv_body.encode("utf-8"), "text/csv")},
    )
    assert imported.status_code == 200 and imported.json()["added"] == 1
    entries = client.get(f"/api/v1/glossaries/{glossary_id}/entries").json()["items"]
    assert {entry["source_term"] for entry in entries} == {"transformer", "attention"}

    # đổi tên + xoá
    assert client.patch(f"/api/v1/glossaries/{glossary_id}", json={"name": "Từ điển AI"}).json()["name"] == "Từ điển AI"
    assert client.delete(f"/api/v1/glossaries/{glossary_id}").status_code == 204
    assert client.get(f"/api/v1/glossaries/{glossary_id}").status_code == 404


def test_admin_endpoints_and_contract_shapes(client, db):
    _make_admin(db)
    login = client.post("/api/v1/auth/login", json={"email": "quantri@example.com", "password": "khong-quan-trong"})
    assert login.status_code == 401  # admin tạo tay chưa có mật khẩu → không đăng nhập được

    from visynth_api.security import issue_token

    admin = db.one("SELECT id, email, role FROM users WHERE email = 'quantri@example.com'")
    token = issue_token(admin, "test-secret", 3600)
    headers = {"Authorization": f"Bearer {token}"}

    invites = client.post(
        "/api/v1/admin/invites", json={"count": 2, "credits_grant": 3, "max_uses": 1}, headers=headers
    )
    assert invites.status_code == 201, invites.text
    assert isinstance(invites.json(), list) and len(invites.json()) == 2
    assert [row["code"] for row in invites.json()]
    listed = client.get("/api/v1/admin/invites", headers=headers)
    assert isinstance(listed.json(), list) and len(listed.json()) == 2

    usage = client.get("/api/v1/admin/usage?days=7", headers=headers)
    assert usage.status_code == 200 and isinstance(usage.json(), list)
    assert client.get("/api/v1/admin/users", headers=headers).status_code == 200
    assert client.get("/api/v1/admin/audit", headers=headers).json()["items"][0]["action"] == "invite.create"

    # người dùng thường không vào được khu quản trị
    _register(client, email="thuong@example.com")
    assert client.get("/api/v1/admin/users").status_code == 403
    # takedown công khai (không cần đăng nhập)
    assert (
        client.post(
            "/api/v1/takedown", json={"reporter_email": "a@b.com", "description": "Nội dung của tôi bị dùng sai"}
        ).status_code
        == 202
    )
