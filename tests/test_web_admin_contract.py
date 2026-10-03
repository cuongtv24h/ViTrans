"""SPA quản trị phải khớp hợp đồng API (M2 vá lỗi, kiểm ở M3).

Vì sao cần lớp test này: SPA không có bước build và không có kiểu tĩnh, nên khi API trả mảng trần mà
giao diện lại đọc `.items` thì **không có gì báo lỗi** — trang chỉ hiện bảng rỗng hoặc nút không làm gì.
Đã xảy ra thật: trang Vận hành đọc `max_cost_usd` trong khi API dùng `daily_spend_cap_usd` (lưu trần
chi tiêu luôn 422), đọc `{items}` cho `/admin/users` (API trả mảng), và gửi `action: "confirm"` trong
khi hợp đồng chỉ nhận `approve | reject | edit_approve`.

Cách kiểm: gọi API THẬT bằng quyền admin rồi so với những gì giao diện đọc (kiểm tĩnh trên mã SPA).
Hai đầu phải gặp nhau ở cùng một tên trường.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
ADMIN_JS = (ROOT / "apps" / "web" / "views" / "admin.js").read_text(encoding="utf-8")
OPENAPI = (ROOT / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8")


@pytest.fixture
def admin_client(pg_schema, db, tmp_path):
    """Client đã đăng nhập bằng một tài khoản admin (dùng lại đường đăng ký thật của API).

    Phụ thuộc `db` để mỗi ca bắt đầu từ CSDL sạch (fixture đó xoá dữ liệu trước khi chạy).
    """
    from visynth_api.app import create_app
    from visynth_api.settings import Settings

    settings = Settings(
        db_dsn=pg_schema,
        session_secret="test-secret",
        upload_dir=tmp_path / "uploads",
        open_signup=True,
        signup_credits=10,
    )
    application = create_app(settings)
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": "quantri@example.com",
                "password": "matkhau-du-manh",
                "tos_version": "2026-10-01",
                "consent_cross_border": True,
                "consent_shared_processing": True,
                "age_confirmed": True,
            },
        )
        assert response.status_code == 201, response.text
        application.state.db.execute("UPDATE users SET role = 'admin' WHERE email = 'quantri@example.com'")
        client.post("/api/v1/auth/login", json={"email": "quantri@example.com", "password": "matkhau-du-manh"})
        yield client
    application.state.db.close()


def test_users_usage_and_invites_return_bare_arrays_that_the_spa_must_handle(admin_client):
    """Ba tuyến này trả MẢNG TRẦN — giao diện phải dùng `asItems`, không được đọc `.items`."""
    users = admin_client.get("/api/v1/admin/users?limit=20")
    assert users.status_code == 200 and isinstance(users.json(), list)

    usage = admin_client.get("/api/v1/admin/usage?days=14")
    assert usage.status_code == 200 and isinstance(usage.json(), list)
    if usage.json():  # hôm nay chưa chi đồng nào thì mảng rỗng
        assert {"day", "cost_usd", "cap_usd", "paused", "jobs_started"} <= set(usage.json()[0])

    invites = admin_client.post("/api/v1/admin/invites", json={"count": 2, "credits_grant": 50, "max_uses": 1})
    assert invites.status_code == 201
    assert isinstance(invites.json(), list) and len(invites.json()) == 2
    assert all("code" in row for row in invites.json()), "giao diện đọc `row.code`"

    # Và giao diện phải xử lý được cả mảng trần lẫn `{items}` bằng ĐÚNG một helper.
    assert "export const asItems" in ADMIN_JS
    for endpoint in ("/admin/users", "/admin/usage", "/admin/invites", "/admin/audit"):
        # mỗi lời gọi tới các tuyến này phải nằm trong ngữ cảnh có `asItems`
        assert endpoint in ADMIN_JS
    assert "asItems(users)" in ADMIN_JS and "asItems(usage)" in ADMIN_JS
    assert "asItems(created)" in ADMIN_JS and "asItems(audit)" in ADMIN_JS


def test_audit_endpoint_is_an_items_object_and_the_spa_uses_its_real_fields(admin_client):
    audit = admin_client.get("/api/v1/admin/audit?limit=20")
    assert audit.status_code == 200
    payload = audit.json()
    assert isinstance(payload, dict) and "items" in payload

    # Tên trường của API: action, entity, entity_id, user_id, created_at (KHÔNG có target_type/actor_email)
    assert (ROOT / "apps" / "api" / "visynth_api" / "routers" / "admin.py").read_text(encoding="utf-8").count(
        "SELECT id, user_id, action, entity, entity_id, meta, created_at FROM audit_log"
    ) == 1
    assert "target_type" not in ADMIN_JS, "giao diện còn đọc trường không tồn tại"
    assert "actor_email" not in ADMIN_JS
    assert "row.entity" in ADMIN_JS and "row.user_id" in ADMIN_JS


def test_spend_cap_field_names_match_both_ends(admin_client):
    """Trần chi tiêu: API đọc/ghi `daily_spend_cap_usd`. Giao diện từng gửi `max_cost_usd` ⇒ 422."""
    before = admin_client.get("/api/v1/admin/spend-cap").json()
    assert set(before) == {"daily_spend_cap_usd", "paused_today", "today_cost_usd"}

    updated = admin_client.put("/api/v1/admin/spend-cap", json={"daily_spend_cap_usd": 7.5})
    assert updated.status_code in (200, 204), updated.text
    after = admin_client.get("/api/v1/admin/spend-cap").json()
    assert after["daily_spend_cap_usd"] == 7.5

    # Hợp đồng OpenAPI cũng phải nói đúng tên trường đó (để người viết SPA sau này không đoán)
    assert "daily_spend_cap_usd" in OPENAPI
    # Và SPA phải dùng đúng tên API, không còn tên cũ
    assert "daily_spend_cap_usd" in ADMIN_JS
    assert "max_cost_usd" not in ADMIN_JS and "spent_today_usd" not in ADMIN_JS


def test_glossary_decision_actions_match_the_contract(admin_client, pg_schema):
    """Hợp đồng chỉ nhận approve | reject | edit_approve — SPA từng gửi `confirm`."""
    allowed = {"approve", "reject", "edit_approve"}
    for action in allowed:
        assert f'"{action}"' in ADMIN_JS, f"giao diện không bao giờ gửi `{action}`"
    assert '"confirm"' not in ADMIN_JS, "SPA còn gửi action không có trong hợp đồng"

    from visynth_api.db import Database

    db = Database(pg_schema)
    try:
        glossary_id = db.scalar(
            "INSERT INTO glossaries (id, owner_id, scope, name) VALUES (gen_random_uuid(), NULL, 'shared', 'Chuẩn') RETURNING id"
        )
        entry_id = db.scalar(
            """INSERT INTO glossary_entries (id, glossary_id, source_term, target_term, status, confidence)
               VALUES (gen_random_uuid(), %s, 'throughput', 'thông lượng', 'suggested', 0.9) RETURNING id""",
            (glossary_id,),
        )
    finally:
        db.close()

    # Duyệt nguyên: `approve`
    ok = admin_client.post(f"/api/v1/admin/glossary-review/{entry_id}/decision", json={"action": "approve"})
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"

    # `confirm` (giá trị SPA từng gửi) phải bị từ chối — đây là lý do của bản vá này
    rejected = admin_client.post(f"/api/v1/admin/glossary-review/{entry_id}/decision", json={"action": "confirm"})
    assert rejected.status_code == 422


def test_glossary_review_queue_shape_matches_the_ui(admin_client):
    queue = admin_client.get("/api/v1/admin/glossary-review?limit=30")
    assert queue.status_code == 200
    payload = queue.json()
    assert set(payload) == {"items", "next_cursor"}
    assert "data.items" in ADMIN_JS, "giao diện đọc `items` cho hàng đợi duyệt — đúng hợp đồng"
