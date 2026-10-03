"""Xuất tệp: `md`, `html`, `docx`, `pdf` và bản **song ngữ** (M1/M2, SPEC §6.10, §13, §14.4).

Điều quan trọng được kiểm:

* PDF **đọc lại được** và giữ dấu tiếng Việt (nếu rơi về font latin-1 thì chữ có dấu biến mất — đúng
  lỗi mà người dùng sẽ gặp, nên phải chặn bằng test);
* mọi tệp xuất đều kèm thông báo "nội dung do AI" (§14.4 — yêu cầu pháp lý, không phải trang trí);
* bản song ngữ chỉ mở cho `full_translation`, giữ ĐÚNG `pid` và giữ cả phần nguồn khi thiếu bản dịch;
* tệp xuất được cache trong `exports` và ghi đè khi xuất lại.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from visynth.render import bilingual_markdown, render_pdf

pytest.importorskip("fpdf")


def _pdf_text(data: bytes) -> str:
    """Đọc lại chữ trong PDF vừa sinh (vòng tròn đóng: kết xuất → đọc)."""
    import pypdfium2 as pdfium

    with open("/tmp/_export_thu.pdf", "wb") as handle:  # noqa: PTH123 - pdfium cần đường dẫn tệp
        handle.write(data)
    doc = pdfium.PdfDocument("/tmp/_export_thu.pdf")
    try:
        return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    finally:
        doc.close()


# ------------------------------------------------------------------ bộ kết xuất PDF


def test_pdf_keeps_vietnamese_diacritics_and_structure():
    markdown = (
        "# Tiêu đề báo cáo\n\n"
        "> Đoạn tóm tắt có **chữ đậm** và *nghiêng*.\n\n"
        "## Mục một\n\n"
        "- ý thứ nhất về trí tuệ nhân tạo\n"
        "- ý thứ hai\n\n"
        "| Chỉ số | Giá trị |\n| --- | --- |\n| độ phủ | 0,95 |\n"
    )
    data = render_pdf(markdown, title="Báo cáo thử", notice="Nội dung do AI tổng hợp.")
    assert data.startswith(b"%PDF-")
    text = _pdf_text(data)
    for needle in ("Tiêu đề báo cáo", "chữ đậm", "Mục một", "trí tuệ nhân tạo", "độ phủ", "0,95"):
        assert needle in text, f"PDF thiếu {needle!r} — có thể đang dùng font không có dấu tiếng Việt"


def test_bilingual_markdown_marks_missing_translation():
    md = bilingual_markdown(
        title="Bản dịch",
        source_title="Nguồn",
        pairs=[("P000001", "The first paragraph.", "Đoạn thứ nhất."), ("P000002", "Second one.", "")],
        notes={"P000001": "giữ nguyên đơn vị"},
    )
    assert "P000001" in md and "P000002" in md
    assert "Đoạn thứ nhất." in md and "The first paragraph." in md
    assert "Chưa có bản dịch" in md  # đoạn thiếu bản dịch vẫn hiện nguyên văn nguồn
    assert "giữ nguyên đơn vị" in md


# ------------------------------------------------------------------ API xuất tệp (PostgreSQL thật)


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


def _seed_report(db) -> tuple[str, str]:
    """Tạo tay một báo cáo + bản dịch để thử đường xuất (không cần chạy worker)."""
    user_id = str(
        db.scalar("INSERT INTO users (email, role, status) VALUES ('xuat@example.com', 'user', 'active') RETURNING id")
    )
    document_id = str(
        db.scalar(
            """INSERT INTO documents (user_id, title, source_type, status, word_count, language_code)
               VALUES (%s, 'Bài giảng EN', 'upload', 'ready', 120, 'en') RETURNING id""",
            (user_id,),
        )
    )
    db.execute(
        """INSERT INTO doc_paragraphs (document_id, pid, idx, kind, content, char_count)
           VALUES (%s, 'P000001', 1, 'body', 'The reactor reached 1200 degrees.', 33),
                  (%s, 'P000002', 2, 'body', 'Engineers recorded the pressure drop.', 34)""",
        (document_id, document_id),
    )
    job_id = str(
        db.scalar(
            """INSERT INTO jobs (user_id, document_id, level, source_lang, model_profile, prompt_versions, status,
                    est_credits, charged_credits)
               VALUES (%s, %s, 'full_translation', 'en', '{"name":"balanced"}', '{}'::jsonb, 'succeeded', 4, 4)
               RETURNING id""",
            (user_id, document_id),
        )
    )
    db.execute(
        """INSERT INTO translation_items (job_id, pid, vi, flagged)
           VALUES (%s, 'P000001', 'Lò phản ứng đạt 1200 độ.', false),
                  (%s, 'P000002', 'Các kỹ sư ghi lại độ giảm áp.', true)""",
        (job_id, job_id),
    )
    report_id = str(
        db.scalar(
            """INSERT INTO reports (job_id, document_id, user_id, title, level, plan, stats, markdown, quality_grade)
               VALUES (%s, %s, %s, 'Bản dịch bài giảng', 'full_translation', '{}'::jsonb, '{}'::jsonb,
                       '# Bản dịch\n\n> Cảnh báo: 1 đoạn bị đánh cờ.\n\nLò phản ứng đạt 1200 độ.\n', 'B')
               RETURNING id""",
            (job_id, document_id, user_id),
        )
    )
    return user_id, report_id


def _login(client: TestClient, email: str = "xuat2@example.com") -> dict:
    created = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "matkhau-du-manh",
            "display_name": "Người xuất",
            "tos_version": "2026-10-01",
            "consent_cross_border": True,
            "consent_shared_processing": True,
            "age_confirmed": True,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["user"]


@pytest.mark.parametrize("fmt, media", [("md", "text/markdown"), ("html", "text/html"), ("pdf", "application/pdf")])
def test_export_formats_carry_ai_notice(client, db, fmt, media):
    """Báo cáo phải thuộc CHÍNH người dùng đang đăng nhập — nên tạo user trước rồi gán báo cáo cho họ."""
    me = _login(client)
    user_id, report_id = _seed_report(db)
    db.execute("UPDATE reports SET user_id = %s WHERE id = %s", (me["id"], report_id))
    db.execute(
        "UPDATE jobs SET user_id = %s WHERE id = (SELECT job_id FROM reports WHERE id = %s)", (me["id"], report_id)
    )

    response = client.get(f"/api/v1/reports/{report_id}/export?format={fmt}")
    assert response.status_code == 200, response.text
    assert media in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    if fmt == "pdf":
        assert response.content.startswith(b"%PDF-")
        text = _pdf_text(response.content)
        assert "Nội dung do AI tổng hợp" in text, "tệp xuất PHẢI kèm thông báo AI (§14.4)"
        assert "1200" in text, "số liệu trong báo cáo phải còn nguyên trong PDF"
    else:
        assert "Nội dung do AI tổng hợp" in response.text

    row = db.one("SELECT format, byte_size FROM exports WHERE report_id = %s AND format = %s", (report_id, fmt))
    assert row and row["byte_size"] == len(response.content)


def test_bilingual_export_pairs_source_with_translation(client, db):
    me = _login(client, "xuat3@example.com")
    user_id, report_id = _seed_report(db)
    db.execute("UPDATE reports SET user_id = %s WHERE id = %s", (me["id"], report_id))
    db.execute(
        "UPDATE jobs SET user_id = %s WHERE id = (SELECT job_id FROM reports WHERE id = %s)", (me["id"], report_id)
    )

    response = client.get(f"/api/v1/reports/{report_id}/export?format=md&bilingual=true")
    assert response.status_code == 200, response.text
    body = response.text
    assert "The reactor reached 1200 degrees." in body, "bản song ngữ phải giữ nguyên văn nguồn"
    assert "Lò phản ứng đạt 1200 độ." in body
    assert body.count("### P000001") == 1 and body.count("### P000002") == 1

    # đoạn bị đánh cờ không biến mất khỏi tệp xuất và tệp vẫn kèm thông báo AI
    item = db.one("SELECT flagged FROM translation_items WHERE pid = 'P000002'")
    assert item["flagged"] is True
    assert "Nội dung do AI tổng hợp" in body


def test_bilingual_rejected_for_synthesis_level(client, db):
    me = _login(client, "xuat4@example.com")
    user_id, report_id = _seed_report(db)
    db.execute("UPDATE reports SET user_id = %s WHERE id = %s", (me["id"], report_id))
    db.execute(
        "UPDATE reports SET level = 'deep_synthesis' WHERE id = %s",
        (report_id,),
    )
    response = client.get(f"/api/v1/reports/{report_id}/export?format=md&bilingual=true")
    assert response.status_code == 422 and response.json()["code"] == "bilingual_unsupported"
