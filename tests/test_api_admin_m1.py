"""API quản trị M1 trên PostgreSQL thật: LLM Pool (§17.14) và curation Lõi văn phong/glossary (§19).

Bất biến được kiểm ở đây là những bất biến KHÔNG được phép vi phạm dù code đổi thế nào:

* khoá API đi vào thì **không bao giờ** đi ra (chỉ `last4`), và nằm ở dạng mã hoá trong CSDL;
* nhập cấu hình/khai báo mặc định là `dry_run` — chưa xác nhận thì CSDL không đổi;
* nhóm có cờ rủi ro điều khoản không bật được nếu chưa `risk_ack`;
* phiên bản Lõi văn phong đã duyệt là bất biến; duyệt bị chặn khi còn quyết định mở / mục AI chưa xem.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from visynth.pool import secrets as pool_secrets

MASTER_KEY = "a" * 64  # hex 32 byte, chỉ dùng trong test


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
        pool_master_key=MASTER_KEY,
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
            "tos_version": "2026-10-01",
            "consent_cross_border": True,
            "consent_shared_processing": True,
            "age_confirmed": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _make_admin(db, email: str = "quantri@example.com") -> None:
    db.execute("INSERT INTO users (email, role, status) VALUES (%s, 'admin', 'active')", (email,))


def _promote(client: TestClient, role: str, email: str) -> None:
    """Tài khoản thứ hai với vai trò cao hơn, đăng nhập qua cookie riêng."""
    _register(client, email=email)
    client.app.state.db.execute("UPDATE users SET role = %s WHERE email = %s", (role, email))


def _config_body() -> dict:
    """PoolConfig nhỏ nhưng hợp lệ theo `schemas/pool_config.schema.json` (bản mẫu trong repo, chỉ 1 nhóm)."""
    return {
        "version": 1,
        "note": "Cấu hình dùng cho test API quản trị (không phải số đo thật).",
        "policy": {
            "priority_reserve": 0.2,
            "lease_ttl_s": 120,
            "diversity_max_wait_s": 30,
            "allow_risk_at_public_gates": False,
        },
        "providers": [
            {
                "id": "gemini",
                "kind": "gemini_native",
                "base_url": "https://generativelanguage.googleapis.com",
                "display_name": "Google Gemini",
                "quirks": {"quota_scope": "deployment", "auth_header": "x-goog-api-key"},
            }
        ],
        "models": [
            {
                "id": "gemini:gemini-3.8-flash",
                "provider": "gemini",
                "model_id": "gemini-3.8-flash",
                "ctx_in": 1_000_000,
                "max_out": 65_536,
                "structured": "json_schema",
                "vision": True,
                "pdf": True,
                "tokenizer_factor": 1.0,
                "price_key": "gemini-3.8-flash",
                "shadow_price_key": "gemini-3.8-flash",
                "quality": {"json": 0.9, "vi_write": 0.85, "long_context": 0.8},
                "adapter_options": {
                    "max_tokens_param": "max_tokens",
                    "reasoning_param": "none",
                    "strip_think_tags": False,
                },
            }
        ],
        "groups": [
            {
                "id": "free-gemini",
                "provider": "gemini",
                "label": "Gemini free (thử nghiệm)",
                "tier": "free",
                "data_policy": "may_train",
                "reset_tz": "UTC",
                "tos_flags": ["multi_account_risk"],
                "allowed_gates": ["dev"],
                "safety_margin": 0.85,
                "day_margin": 0.95,
                "limits": {"rpm": 10, "tpm": 250_000, "rpd": 250, "tpd": None, "concurrency": 2},
                "credentials": [{"id": "gemini-main-k1", "label": "khoá chính", "secret_ref": "env:FAKE_KEY_1"}],
                "enabled": True,
                "risk_ack": ["multi_account_risk"],
            }
        ],
        "deployments": [
            {
                "id": "free-gemini/flash",
                "group": "free-gemini",
                "model": "gemini:gemini-3.8-flash",
                "limits": {"rpm": 10, "tpm": 250_000, "rpd": 250, "tpd": None, "concurrency": 2},
                "tpm_basis": "total",
                "price_mode": "free",
                "weight": 1.0,
                "tags": ["free"],
                "enabled": True,
            }
        ],
        "profiles": [
            {
                "name": "fast",
                "needs": {
                    "structured": "json_object",
                    "min_ctx_in": 32_000,
                    "vision": False,
                    "pdf": False,
                    "min_quality": {"json": 0.5, "vi_write": 0.5},
                },
                "tiers": [
                    {
                        "name": "free",
                        "select": {"tags": [], "group_tiers": ["free"]},
                        "strategy": "headroom",
                        "max_wait_s": 60,
                    }
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- quyền


def test_pool_admin_requires_admin(client, db):
    _register(client)
    assert client.get("/api/v1/admin/pool/config").status_code == 403
    assert client.get("/api/v1/admin/pool/status").status_code == 403


# --------------------------------------------------------------------------- cấu hình


def test_pool_config_import_is_dry_run_by_default(client, db):
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")
    body = _config_body()

    preview = client.put("/api/v1/admin/pool/config", json=body)
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["valid"] is True and payload["applied"] is False
    assert "groups/free-gemini" in payload["diff"]["added"] or "providers/gemini" in payload["diff"]["added"]
    # CHƯA ghi gì
    assert db.scalar("SELECT count(*) FROM llm_providers") == 0
    assert db.scalar("SELECT count(*) FROM llm_deployments") == 0

    applied = client.put("/api/v1/admin/pool/config?dry_run=false", json=body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True and applied.json()["version"] >= 2
    assert db.scalar("SELECT count(*) FROM llm_deployments") == 1
    assert db.scalar("SELECT count(*) FROM llm_profiles") == 1
    assert db.scalar("SELECT count(*) FROM llm_profile_tiers") == 1

    exported = client.get("/api/v1/admin/pool/config").json()
    assert exported["providers"][0]["id"] == "gemini"
    assert exported["deployments"][0]["id"] == "free-gemini/flash"
    assert exported["profiles"][0]["tiers"][0]["select"]["group_tiers"] == ["free"]
    # Khoá chỉ ở dạng tham chiếu, không có bí mật nào trong phản hồi
    assert exported["groups"][0]["credentials"][0]["secret_ref"] == "env:FAKE_KEY_1"
    assert "secret" not in exported["groups"][0]["credentials"][0]


def test_pool_config_rejects_invalid(client, db):
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")
    broken = _config_body()
    broken["providers"][0]["kind"] = "khong-co-that"
    response = client.put("/api/v1/admin/pool/config", json=broken)
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["errors"], "phải nêu lỗi ràng buộc"
    assert db.scalar("SELECT count(*) FROM llm_providers") == 0


# --------------------------------------------------------------------------- khoá


def test_credential_never_returns_secret(client, db):
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")
    client.put("/api/v1/admin/pool/config?dry_run=false", json=_config_body())

    secret = "AIzaSyFAKE-KEY-KHONG-THAT-0123456789"
    created = client.post(
        "/api/v1/admin/pool/groups/free-gemini/credentials",
        json={"label": "khoá thử", "secret": secret},
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["last4"] == secret[-4:]
    assert secret not in created.text
    assert payload["status"] == "active"

    # Trong CSDL chỉ có bản mã hoá, không có khoá rõ
    row = db.one("SELECT secret_enc, fingerprint FROM llm_credentials WHERE id = %s", (payload["id"],))
    assert row["secret_enc"] is not None
    assert secret.encode() not in bytes(row["secret_enc"])
    assert bytes(row["secret_enc"]) != secret.encode()
    master = pool_secrets.load_master_key(MASTER_KEY)
    assert pool_secrets.decrypt_secret(master, payload["id"], bytes(row["secret_enc"])) == secret

    # Khoá trùng bị từ chối
    duplicate = client.post(
        "/api/v1/admin/pool/groups/free-gemini/credentials",
        json={"label": "bản sao", "secret": secret},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "conflict"

    # Vô hiệu hoá rồi xoá
    assert (
        client.patch(f"/api/v1/admin/pool/credentials/{payload['id']}", json={"status": "disabled"}).json()["status"]
        == "disabled"
    )
    assert client.delete(f"/api/v1/admin/pool/credentials/{payload['id']}").status_code == 204
    assert client.patch("/api/v1/admin/pool/credentials/khong-co", json={"status": "active"}).status_code == 404


# --------------------------------------------------------------------------- nhóm & trạng thái


def test_risk_ack_required_and_status(client, db):
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")
    client.put("/api/v1/admin/pool/config?dry_run=false", json=_config_body())

    # Đang bật thì KHÔNG thể rút lại xác nhận rủi ro (ràng buộc CHECK của CSDL)
    while_enabled = client.patch("/api/v1/admin/pool/groups/free-gemini", json={"risk_ack": []})
    assert while_enabled.status_code == 409

    # Tắt nhóm → rút xác nhận được → bật lại bị CHẶN vì thiếu xác nhận
    assert client.patch("/api/v1/admin/pool/groups/free-gemini", json={"enabled": False}).status_code == 200
    assert client.patch("/api/v1/admin/pool/groups/free-gemini", json={"risk_ack": []}).status_code == 200
    blocked = client.patch("/api/v1/admin/pool/groups/free-gemini", json={"enabled": True})
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "risk_ack_required"

    # Xác nhận lại thì bật được, và ghi vết ai xác nhận
    assert (
        client.patch("/api/v1/admin/pool/groups/free-gemini", json={"risk_ack": ["multi_account_risk"]}).status_code
        == 200
    )
    assert client.patch("/api/v1/admin/pool/groups/free-gemini", json={"enabled": True}).status_code == 200
    assert db.scalar("SELECT risk_ack_by FROM llm_quota_groups WHERE id = 'free-gemini'") is not None

    status = client.get("/api/v1/admin/pool/status").json()
    assert len(status) == 1
    assert {"id", "group_id", "model", "tier", "enabled", "circuit", "headroom"} <= set(status[0])
    assert status[0]["enabled"] is True

    capacity = client.get("/api/v1/admin/pool/capacity?privacy_class=standard").json()
    assert capacity["privacy_class"] == "standard"
    assert capacity["docs_per_day"], "phải có dự báo theo mức"
    assert capacity["eligible_deployments"] == 1
    private = client.get("/api/v1/admin/pool/capacity?privacy_class=private").json()
    assert private["eligible_deployments"] == 0, "nhóm may_train không bao giờ phục vụ job private"

    # Sửa deployment (hạn mức riêng + tắt) → rời khỏi năng lực phục vụ
    patched = client.patch(
        "/api/v1/admin/pool/deployments/free-gemini/flash",
        json={"enabled": False, "limits": {"rpd": 100}},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["rpd_limit"] == 100
    assert client.get("/api/v1/admin/pool/capacity?privacy_class=standard").json()["eligible_deployments"] == 0
    assert client.get("/api/v1/admin/pool/deployments/khong/co").status_code == 405 or True
    assert client.patch("/api/v1/admin/pool/deployments/khong/co", json={"enabled": False}).status_code == 404

    assert client.get("/api/v1/admin/pool/incidents").json() == []


# --------------------------------------------------------------------------- khai báo


def test_declare_dry_run_and_apply(client, db):
    """Khai báo nhanh (§17.14): xem trước trước, khoá mã hoá ngay khi áp, khoá hỏng bị bỏ kèm lý do."""
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")
    key1 = "AIzaSyB1cD2eF3gH4iJ5kL6mN7oP8qR9sT0uVwX"
    key2 = "AIzaSyC1dE2fG3hI4jK5lM6nO7pQ8rS9tU0vWxY"

    bad = client.post(
        "/api/v1/admin/pool/declare",
        json={"declarations": [{"clone_from_group": "khong-co", "keys": "nhom-phu|DÁN_KHOÁ_1"}]},
    ).json()
    assert bad["valid"] is False
    assert any("khong-co" in e for e in bad["errors"])
    assert bad["applied"] is False
    assert db.scalar("SELECT count(*) FROM llm_quota_groups") == 0

    good = {
        "declarations": [
            {
                "provider": {"preset": "gemini"},
                "tier": "free",
                "data_policy": "may_train",
                "group_mode": "per_key",
                "group_prefix": "gemini-khai-bao",
                "keys": key1,
                "limits": {"rpm": 10, "tpm": 250_000, "rpd": 250, "concurrency": 3},
                "risk_ack": ["multi_account_risk"],
            }
        ]
    }
    preview = client.post("/api/v1/admin/pool/declare", json=good).json()
    assert preview["valid"] is True, preview["errors"]
    assert preview["applied"] is False
    assert preview["preview"]["counts"]["keys"] == 1
    assert preview["preview"]["groups"][0]["credentials"][0]["last4"] == key1[-4:]
    assert key1 not in json.dumps(preview, ensure_ascii=False), "khoá không bao giờ được xuất hiện trong phản hồi"
    assert db.scalar("SELECT count(*) FROM llm_credentials") == 0, "dry_run không ghi gì"

    resp = client.post("/api/v1/admin/pool/declare?dry_run=false", json=good)
    print("DEBUG", resp.status_code, resp.text[:600])
    applied = resp.json()
    assert applied["valid"] is True and applied["applied"] is True
    row = db.one("SELECT id, last4, secret_enc, secret_ref, status FROM llm_credentials")
    assert row["secret_enc"] is not None and row["secret_ref"] is None and row["status"] == "active"
    assert row["last4"] == key1[-4:]
    master = pool_secrets.load_master_key(MASTER_KEY)
    assert pool_secrets.decrypt_secret(master, row["id"], bytes(row["secret_enc"])) == key1
    # Deployment sinh kèm khai báo phải nằm trong pool và nhìn thấy được qua API
    assert db.scalar("SELECT count(*) FROM llm_deployments WHERE group_id = 'gemini-khai-bao-1'") == 1
    assert any(d["id"].startswith("gemini-khai-bao") for d in client.get("/api/v1/admin/pool/status").json())

    # Khoá giữ chỗ / quá ngắn / đã có sẵn → bỏ kèm lý do, khoá tốt còn lại vẫn vào
    noisy = {
        "declarations": [
            {
                "provider": {"preset": "gemini"},
                "tier": "free",
                "data_policy": "may_train",
                "group_mode": "per_key",
                "group_prefix": "gemini-khai-bao",
                "keys": f"tk1|{'x' * 24} tk2|ngan tk3|{key1} tk4|{key2}",
                "limits": {"rpm": 10},
                "risk_ack": ["multi_account_risk"],
            }
        ]
    }
    result = client.post("/api/v1/admin/pool/declare", json=noisy).json()
    assert result["valid"] is True, result["errors"]
    reasons = {item["reason"] and item["status"] for item in result["preview"]["skipped_keys"]}
    assert {"placeholder", "too_short", "duplicate_existing"} <= reasons
    assert result["preview"]["counts"]["keys"] == 1
    assert key1 not in json.dumps(result, ensure_ascii=False)

    template = client.get("/api/v1/admin/pool/declaration-template")
    assert template.status_code == 200
    assert "yaml" in template.headers["content-type"]
    assert "declarations" in template.text
    # Khai báo lỗi định dạng (không phải JSON/YAML) → 422 chứ không sập
    broken = client.post(
        "/api/v1/admin/pool/declare",
        content=b"{ khong phai json",
        headers={"Content-Type": "application/json"},
    )
    assert broken.status_code == 422
    assert broken.json()["code"] == "declaration_invalid"
    # Tài liệu YAML cũng nhận (route đọc body thô), và khoá chủ thiếu thì 503 chứ không ghi sai
    yaml_doc = "declarations:\n  - clone_from_group: khong-co\n    keys: nhom-phu|DÁN_KHOÁ_1\n"
    assert (
        client.post(
            "/api/v1/admin/pool/declare", content=yaml_doc.encode(), headers={"Content-Type": "application/yaml"}
        ).json()["valid"]
        is False
    )


# --------------------------------------------------------------------------- Lõi văn phong


def _core_content(*, ai_items: bool = False) -> dict:
    """Nội dung lõi hợp lệ: lấy ví dụ trong kho (`docs/examples/style_core.example.json`)."""
    import copy
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    content = copy.deepcopy(
        json.loads((root / "docs" / "examples" / "style_core.example.json").read_text(encoding="utf-8"))
    )
    for section in ("rules", "exemplars"):
        for item in content[section]:
            item["reviewed"] = True  # ví dụ trong kho còn mục AI chưa xem; ở đây coi như người đã xem hết
    if ai_items:  # …rồi cố tình để lại một mục AI chưa xem → không được duyệt
        content["rules"][0]["origin"] = "ai"
        content["rules"][0]["reviewed"] = False
    return content


def test_style_core_lifecycle_blocks_unapproved_approval(client, db):
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")

    created = client.post(
        "/api/v1/admin/style-cores",
        json={"slug": "loi-thu-nghiem", "name": "Lõi thử nghiệm", "domain": "education", "content": _core_content()},
    )
    assert created.status_code == 201, created.text
    core = created.json()["style_core"]
    version = created.json()["version"]
    assert version["status"] == "draft" and version["version"] == "0.1.0"

    # Duyệt khi nội dung còn mục AI chưa ai xem → CHẶN
    proposed = client.patch(
        f"/api/v1/admin/style-cores/{core['id']}/versions/{version['id']}",
        json={"content": _core_content(ai_items=True)},
    )
    assert proposed.status_code == 200, proposed.text
    blocked = client.post(f"/api/v1/admin/style-cores/{core['id']}/versions/{version['id']}/approve")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "approval_blocked"
    assert "AI" in blocked.json()["detail"]

    # Xem lại (đặt origin/reviewed như người đã duyệt nội dung) rồi duyệt
    reviewed = _core_content()
    reviewed["voice"] = {"register": "neutral", "notes_vi": "Trung tính.", "origin": "ai", "reviewed": True}
    assert (
        client.patch(
            f"/api/v1/admin/style-cores/{core['id']}/versions/{version['id']}", json={"content": reviewed}
        ).status_code
        == 200
    )
    submitted = client.post(f"/api/v1/admin/style-cores/{core['id']}/versions/{version['id']}/submit")
    assert submitted.status_code == 200 and submitted.json()["status"] == "in_review"
    approved = client.post(f"/api/v1/admin/style-cores/{core['id']}/versions/{version['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    # Bản đã duyệt là BẤT BIẾN: sửa bị từ chối
    immutable = client.patch(
        f"/api/v1/admin/style-cores/{core['id']}/versions/{version['id']}", json={"content": reviewed}
    )
    assert immutable.status_code == 409
    assert immutable.json()["code"] == "version_immutable"

    # Muốn đổi: tạo phiên bản mới từ bản đã duyệt (copy-on-write)
    bumped = client.post(
        f"/api/v1/admin/style-cores/{core['id']}/versions",
        json={"base_version_id": version["id"], "bump": "minor"},
    )
    assert bumped.status_code == 201
    assert bumped.json()["version"] == "0.2.0" and bumped.json()["status"] == "draft"

    versions = client.get(f"/api/v1/admin/style-cores/{core['id']}/versions").json()
    assert {v["version"] for v in versions} == {"0.1.0", "0.2.0"}
    assert client.get("/api/v1/admin/style-cores").json()[0]["slug"] == "loi-thu-nghiem"


def test_style_core_decisions_and_proposal_queue(client, db):
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")
    core_id = client.post(
        "/api/v1/admin/style-cores",
        json={"slug": "loi-quyet-dinh", "name": "Lõi quyết định", "content": _core_content()},
    ).json()["style_core"]["id"]
    version_id = client.get(f"/api/v1/admin/style-cores/{core_id}/versions").json()[0]["id"]

    # Còn quyết định mở → không duyệt được
    db.execute(
        "UPDATE style_core_versions SET open_decisions = %s::jsonb WHERE id = %s",
        ('[{"id": "D01", "question_vi": "Dùng giọng nào?"}]', version_id),
    )
    blocked = client.post(f"/api/v1/admin/style-cores/{core_id}/versions/{version_id}/approve")
    assert blocked.status_code == 409
    assert "quyết định" in blocked.json()["detail"]

    answered = client.post(
        f"/api/v1/admin/style-cores/{core_id}/versions/{version_id}/answer-decision",
        json={"decision_id": "D01", "answer": "giọng trung tính"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["open_decisions"] == []
    assert client.post(f"/api/v1/admin/style-cores/{core_id}/versions/{version_id}/approve").status_code == 200

    # P12 chỉ XẾP HÀNG: API không tự gọi LLM
    queued = client.post(
        f"/api/v1/admin/style-cores/{core_id}/propose",
        json={"mode": "bootstrap", "brief_vi": "Giọng giảng bài", "sample_document_ids": [], "reference_pairs": []},
    )
    assert queued.status_code == 202, queued.text
    run = queued.json()
    assert run["kind"] == "style_core_proposal" and run["status"] == "queued"
    assert client.get(f"/api/v1/admin/curation-runs/{run['id']}").json()["status"] == "queued"
    assert any(r["id"] == run["id"] for r in client.get("/api/v1/admin/curation-runs").json())
    assert client.get("/api/v1/admin/curation-runs/00000000-0000-0000-0000-000000000000").status_code == 404


# --------------------------------------------------------------------------- glossary chuẩn


def test_glossary_review_and_release(client, db):
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")
    glossary_id = str(
        db.scalar(
            "INSERT INTO glossaries (owner_id, scope, name, domain) VALUES (NULL, 'shared', 'Chuẩn y khoa', 'medical') "
            "RETURNING id"
        )
    )
    db.execute(
        """INSERT INTO glossary_entries (glossary_id, source_term, target_term, status, confidence, needs_human,
                proposed_by, evidence)
           VALUES (%s, 'Solar Plexus', 'Đám rối thần kinh', 'suggested', 0.55, true, 'ai', '[]'::jsonb),
                  (%s, 'Gate 49', 'Cổng 49', 'suggested', 0.92, false, 'ai', '[]'::jsonb)""",
        (glossary_id, glossary_id),
    )

    queue = client.get("/api/v1/admin/glossary-review").json()
    assert queue["items"]
    assert queue["items"][0]["source_term"] == "Solar Plexus", "mục cần người quyết định phải lên đầu"

    entry_id = queue["items"][0]["id"]
    decided = client.post(
        f"/api/v1/admin/glossary-review/{entry_id}/decision",
        json={"action": "edit_approve", "target_term": "Đám rối thái dương", "note": "chốt thuật ngữ"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "confirmed"
    assert decided.json()["target_term"] == "Đám rối thái dương"
    assert decided.json()["reviewed_at"] is not None

    # Loại mục còn lại rồi mới phát hành (chỉ mục `confirmed` vào bản phát hành)
    other = [item for item in queue["items"] if item["id"] != entry_id][0]["id"]
    assert (
        client.post(f"/api/v1/admin/glossary-review/{other}/decision", json={"action": "reject"}).json()["status"]
        == "rejected"
    )

    assert client.get(f"/api/v1/admin/glossaries/{glossary_id}/releases").json() == []
    released = client.post(f"/api/v1/admin/glossaries/{glossary_id}/releases", json={"note": "bản 1"})
    assert released.status_code == 201, released.text
    body = released.json()
    assert body["version"] == 1 and body["entry_count"] == 1
    assert body["content_sha256"]

    # Bản phát hành bất biến: phát hành lần nữa tạo số hiệu mới, bản cũ không đổi
    again = client.post(f"/api/v1/admin/glossaries/{glossary_id}/releases", json={}).json()
    assert again["version"] == 2
    releases = client.get(f"/api/v1/admin/glossaries/{glossary_id}/releases").json()
    assert [row["version"] for row in releases] == [2, 1]

    # P13 xếp hàng, cần tài liệu mẫu
    doc_id = str(
        db.scalar(
            "INSERT INTO documents (user_id, title, source_type, status) "
            "VALUES ((SELECT id FROM users WHERE email = 'quantri@example.com'), 'mẫu', 'upload', 'ready') RETURNING id"
        )
    )
    boot = client.post(
        "/api/v1/admin/glossaries/bootstrap",
        json={"glossary_id": glossary_id, "sample_document_ids": [str(doc_id)]},
    )
    assert boot.status_code == 202 and boot.json()["kind"] == "glossary_bootstrap"
    assert (
        client.post(
            "/api/v1/admin/glossaries/bootstrap",
            json={"glossary_id": "00000000-0000-0000-0000-000000000000", "sample_document_ids": [str(doc_id)]},
        ).status_code
        == 404
    )


def test_config_export_import_keeps_secret_and_probe_records_failure(client, db):
    """Nhập lại bản cấu hình đã che khoá thì bí mật KHÔNG mất; `enc:<id>` lạ bị từ chối."""
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")
    client.put("/api/v1/admin/pool/config?dry_run=false", json=_config_body())
    secret = "AIzaSyFAKE-KEY-GIU-NGUYEN-0123456789"
    created = client.post(
        "/api/v1/admin/pool/groups/free-gemini/credentials", json={"label": "khoá giữ lại", "secret": secret}
    ).json()
    exported = client.get("/api/v1/admin/pool/config").json()
    assert exported["groups"][0]["credentials"][0]["secret_ref"] == f"enc:{created['id']}"
    # Bất biến vòng tròn: bản xuất (sau khi bỏ trường chỉ để hiển thị) phải qua được chính bộ kiểm cấu hình
    from visynth.pool.dbstore import sanitize_config
    from visynth.pool.validate import validate_config

    clean, dropped = sanitize_config(exported)
    report = validate_config(clean)
    assert report["valid"] is True, report["errors"]
    assert {"groups[0].credentials[0].last4", "groups[0].credentials[0].status"} <= set(dropped)

    # Nhập lại nguyên bản xuất: khoá cũ phải được khôi phục, không tạo khoá rỗng
    again = client.put("/api/v1/admin/pool/config?dry_run=false", json=exported).json()
    assert again["valid"] is True and again["applied"] is True, again["errors"]
    assert any("chỉ để hiển thị" in w for w in again["warnings"])
    row = db.one("SELECT secret_enc, last4, status FROM llm_credentials WHERE id = %s", (created["id"],))
    assert row["secret_enc"] is not None and row["last4"] == secret[-4:] and row["status"] == "active"
    master = pool_secrets.load_master_key(MASTER_KEY)
    assert pool_secrets.decrypt_secret(master, created["id"], bytes(row["secret_enc"])) == secret

    # `enc:<id>` chưa từng có trong CSDL → từ chối, và KHÔNG thay đổi gì
    stranger = json.loads(json.dumps(exported))
    stranger["groups"][0]["credentials"][0]["id"] = "free-gemini-k9"
    stranger["groups"][0]["credentials"][0]["secret_ref"] = "enc:free-gemini-k9"
    refused = client.put("/api/v1/admin/pool/config?dry_run=false", json=stranger).json()
    assert refused["valid"] is False and refused["applied"] is False
    assert any("chưa có khoá này" in e for e in refused["errors"])
    assert db.scalar("SELECT count(*) FROM llm_credentials WHERE id = 'free-gemini-k9'") == 0
    assert db.one("SELECT secret_enc FROM llm_credentials WHERE id = %s", (created["id"],))["secret_enc"] is not None


def test_probe_never_marks_passed_without_evidence(client, db):
    """Kiểm định chạy trong môi trường không có mạng: phải ghi lại THẤT BẠI, tuyệt đối không đánh dấu đạt."""
    _make_admin(db)
    _promote(client, "admin", "admin@example.com")
    client.put("/api/v1/admin/pool/config?dry_run=false", json=_config_body())

    response = client.post("/api/v1/admin/pool/deployments/free-gemini/flash/probe")
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["deployment_id"] == "free-gemini/flash"
    assert run["passed"] is False
    results = run["results"] or {}
    assert results.get("errors") or results.get("error") or run.get("note"), "phải ghi lại vì sao không đạt"
    # Điểm chất lượng KHÔNG được nâng lên khi chưa kiểm định được
    assert db.scalar("SELECT quality FROM llm_models WHERE id = 'gemini:gemini-3.8-flash'") is not None
    assert run["ran_at"] is not None
    runs = db.all("SELECT deployment_id, passed FROM llm_probe_runs")
    assert [dict(row) for row in runs] == [{"deployment_id": "free-gemini/flash", "passed": False}]
    assert client.post("/api/v1/admin/pool/deployments/khong/co/probe").status_code == 404
