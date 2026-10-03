"""Dịch vụ parser trong sandbox (SPEC §6.1, §20.4).

Ba tầng được kiểm:

1. **Hợp đồng HTTP** — byte thô vào, JSON bóc tách ra; tệp quá lớn bị chặn ở 413 trước khi nạp vào RAM;
   lỗi bóc tách trả mã máy đọc được, không lộ stack trace.
2. **Bộ client của API** — dựng lại `Extraction` giữ nguyên `pid`/số đo; parser chết ⇒ `ParserUnavailable`
   (API đổi thành 503, người dùng thử lại) chứ không treo.
3. **Ranh giới cấu hình** — `POST /documents` đi qua sandbox khi có `VISYNTH_PARSER_URL`, và trả 503 khi
   sandbox không gọi được; compose giữ parser trong mạng `internal` (không tuyến ra internet).
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visynth.extract.model import Extraction
from visynth.worker.parser_client import ParserRejected, ParserUnavailable, parse_via_service
from visynth.worker.parser_service import serve_in_thread

SAMPLE = (
    "Chương 1. Giới thiệu\n\n"
    "Trí tuệ nhân tạo đang thay đổi cách chúng ta làm việc.\n\n"
    "Trong báo cáo này, chúng tôi trình bày ba kết quả chính.\n"
)


@pytest.fixture
def parser_server():
    server, base_url = serve_in_thread()
    yield base_url
    server.shutdown()
    server.server_close()


def _post(base_url: str, data: bytes, *, filename: str = "bai.txt", title: str = "Bài") -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base_url}/parse?" + urllib.parse.urlencode({"filename": filename, "title": title}),
        data=data,
        method="POST",
        headers={"Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ------------------------------------------------------------------ hợp đồng HTTP


def test_parse_endpoint_returns_contract_json(parser_server):
    status, payload = _post(parser_server, SAMPLE.encode("utf-8"), title="Bài giảng")
    assert status == 200, payload
    assert payload["title"] == "Bài giảng"
    assert payload["paragraphs"] and payload["paragraphs"][0]["pid"].startswith("P")
    # dựng lại được ở phía API và giữ nguyên số đo
    rebuilt = Extraction.from_dict(payload)
    assert rebuilt.full_text().strip().startswith("Chương 1")


def test_oversized_body_is_rejected_before_reading_it_all():
    server, base_url = serve_in_thread(max_bytes=2048)
    try:
        status, payload = _post(base_url, b"x" * 4096)
        assert status == 413 and payload["code"] == "file_too_large"
    finally:
        server.shutdown()
        server.server_close()


def test_broken_pdf_reports_machine_readable_code(parser_server):
    status, payload = _post(parser_server, b"%PDF-1.4 khong-phai-pdf-that", filename="hong.pdf")
    assert status == 422
    assert payload["code"] == "unreadable_pdf"
    assert "Traceback" not in payload.get("message", "")


def test_healthz_and_unknown_path(parser_server):
    with urllib.request.urlopen(f"{parser_server}/healthz", timeout=10) as response:  # noqa: S310
        assert response.status == 200 and json.loads(response.read())["status"] == "ok"
    status, _ = _post(parser_server, SAMPLE.encode("utf-8"))
    assert status == 200  # /parse vẫn sống sau healthcheck
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(f"{parser_server}/khac", timeout=10)  # noqa: S310
    assert err.value.code == 404


def test_timeout_is_enforced_and_reported():
    """Parser không được phép chạy mãi: quá hạn ⇒ mã `parse_timeout` (API trả 503 để thử lại)."""
    from visynth.worker import parser_service

    def _slow(_raw, **_kwargs):
        raise parser_service._Timeout

    server, base_url = serve_in_thread(timeout_s=1)
    try:
        monkey = pytest.MonkeyPatch()
        monkey.setattr(parser_service, "extract", _slow, raising=True)
        try:
            status, payload = _post(base_url, SAMPLE.encode("utf-8"))
        finally:
            monkey.undo()
        assert status == 422 and payload["code"] == "parse_timeout"
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------ client phía API


def test_client_rebuilds_extraction(parser_server):
    parsed = parse_via_service(parser_server, SAMPLE.encode("utf-8"), filename="bai.txt", title="Bài")
    assert [p.pid for p in parsed.paragraphs][0].startswith("P")
    assert parsed.word_count > 10
    assert parsed.as_dict()["title"] == "Bài"


def test_client_reports_unavailable_when_service_is_down():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # cổng vừa đóng lại ⇒ chắc chắn không ai nghe
    with pytest.raises(ParserUnavailable):
        parse_via_service(f"http://127.0.0.1:{port}", SAMPLE.encode("utf-8"), timeout_s=2)


def test_client_maps_rejection_to_exception(parser_server):
    with pytest.raises(ParserRejected) as err:
        parse_via_service(parser_server, b"%PDF-1.4 hong", filename="hong.pdf")
    assert err.value.code == "unreadable_pdf" and err.value.status == 422


# ------------------------------------------------------------------ ranh giới API


@pytest.fixture
def api_client(pg_schema, db, tmp_path):
    from visynth_api.app import create_app
    from visynth_api.settings import Settings

    server, base_url = serve_in_thread()
    settings = Settings(
        db_dsn=pg_schema,
        session_secret="test-secret",
        upload_dir=tmp_path / "uploads",
        open_signup=True,
        signup_credits=5,
        parser_url=base_url,
    )
    application = create_app(settings)
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        application.state.db.close()
        server.shutdown()
        server.server_close()


def _token(client: TestClient) -> dict:
    created = client.post(
        "/api/v1/auth/register",
        json={
            "email": "parser@example.com",
            "password": "matkhau-du-manh",
            "display_name": "Người thử",
            "tos_version": "2026-10-01",
            "consent_cross_border": True,
            "consent_shared_processing": True,
            "age_confirmed": True,
        },
    )
    assert created.status_code == 201, created.text
    return {"Authorization": f"Bearer {created.json()['token']}"}


def test_upload_goes_through_sandbox_and_keeps_pids(api_client, db):
    headers = _token(api_client)
    response = api_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("bai.txt", SAMPLE.encode("utf-8"), "text/plain")},
        data={"title": "Bài qua sandbox", "rights_attested": "true"},
    )
    assert response.status_code == 201, response.text
    document = response.json()["document"]
    assert document["word_count"] > 10 and document["needs_ocr"] is False

    detail = api_client.get(f"/api/v1/documents/{document['id']}", headers=headers)
    assert detail.status_code == 200 and detail.json()["status"] == "ready"
    rows = db.all("SELECT pid, content FROM doc_paragraphs WHERE document_id = %s ORDER BY idx", (document["id"],))
    assert rows and rows[0]["pid"].startswith("P"), "pid phải do parser sinh và được API giữ nguyên"
    assert "Trí tuệ nhân tạo" in " ".join(row["content"] for row in rows)


def test_broken_file_error_comes_from_sandbox(api_client):
    headers = _token(api_client)
    response = api_client.post(
        "/api/v1/documents",
        headers=headers,
        files={"file": ("hong.pdf", b"%PDF-1.4 khong-phai-pdf", "application/pdf")},
        data={"rights_attested": "true"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unreadable_pdf"


def test_parser_down_returns_503_not_hang(pg_schema, db, tmp_path):
    from visynth_api.app import create_app
    from visynth_api.settings import Settings

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_url = f"http://127.0.0.1:{probe.getsockname()[1]}"

    settings = Settings(
        db_dsn=pg_schema,
        session_secret="test-secret",
        upload_dir=tmp_path / "uploads",
        open_signup=True,
        signup_credits=5,
        parser_url=dead_url,
        parser_timeout_s=2.0,
    )
    application = create_app(settings)
    try:
        with TestClient(application) as client:
            headers = _token(client)
            response = client.post(
                "/api/v1/documents",
                headers=headers,
                files={"file": ("bai.txt", SAMPLE.encode("utf-8"), "text/plain")},
                data={"rights_attested": "true"},
            )
            assert response.status_code == 503 and response.json()["code"] == "parser_unavailable"
            # tệp tải lên THẤT BẠI không được để lại rác trong thư mục lưu trữ
            stored = [p for p in Path(settings.upload_dir).rglob("*") if p.is_file()]
            assert stored == [], f"tải lên thất bại mà còn để lại tệp: {stored}"
    finally:
        application.state.db.close()
