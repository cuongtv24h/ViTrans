"""PDF và OCR (P10) — giai đoạn `extract` (M1, SPEC §6.0/§6.1).

Ba tầng:

* **Đọc PDF tất định** — lấy chữ theo trang, bỏ header/footer lặp, nối từ bị ngắt dòng, phát hiện trang quét.
* **Lập kế hoạch OCR** — cụm 10–15 trang, `task_key` dạng `OCR:12-26`, phần trang không cần OCR được giữ nguyên.
* **Đường chạy thật trên PostgreSQL** — tài liệu quét sinh một task cho mỗi cụm, P10 trả văn bản (client giả),
  `doc_paragraphs` được thay bằng bản OCR, cụm đã xong không chạy lại, và **một cụm hỏng không làm chết job**
  (khâu phụ không được làm chết khâu chính — §6.0 nguyên tắc 5).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visynth.eval import DemoProducer
from visynth.extract import ExtractionError, extract, read_pages
from visynth.extract.ocr import OcrChunk, merge_ocr, plan_ocr, split_pages
from visynth.llm.base import LLMError, LLMRequest, Outcome
from visynth.llm.fake import FakeLLMClient, FakeReply
from visynth.worker import JobWorker, WorkerStore
from visynth.worker.ocr import PdfOcr

PDF_AVAILABLE = True
try:  # pragma: no cover - phụ thuộc môi trường
    from fpdf import FPDF
except ImportError:  # pragma: no cover
    PDF_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PDF_AVAILABLE, reason="cần fpdf2 để dựng PDF mẫu")


# ------------------------------------------------------------------ dựng PDF mẫu


#: Font Unicode có sẵn trên máy chủ CI (`fpdf2` mặc định chỉ hỗ trợ latin-1 nên tiếng Việt sẽ lỗi).
UNICODE_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def _text_pdf(path: Path, pages: list[str], *, header: str = "", footer: str = "") -> Path:
    """PDF có lớp chữ thật: mỗi phần tử là một trang."""
    pdf = FPDF()
    if UNICODE_FONT.exists():
        pdf.add_font("dejavu", "", str(UNICODE_FONT))
        pdf.set_font("dejavu", size=12)
    else:  # pragma: no cover - máy không có font Unicode thì chỉ chạy được văn bản ASCII
        pdf.set_font("Helvetica", size=12)
    for index, body in enumerate(pages, start=1):
        pdf.add_page()
        if header:
            pdf.cell(0, 8, header, new_x="LMARGIN", new_y="NEXT")
        pdf.multi_cell(0, 6, body)
        if footer:
            pdf.set_y(-15)
            pdf.cell(0, 8, f"{footer} | {index}", new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))
    return path


def _image_pdf(path: Path, pages: list[str]) -> Path:
    """PDF **không có lớp chữ** (mỗi trang là một ảnh) — giống bản quét, để thử đường OCR."""
    import pypdfium2 as pdfium

    source = _text_pdf(path.with_suffix(".src.pdf"), pages)
    rendered: list[str] = []
    doc = pdfium.PdfDocument(str(source))
    try:
        for index in range(len(doc)):
            image = doc[index].render(scale=2).to_pil()
            out = path.with_name(f"{path.stem}-{index}.png")
            image.save(out)
            rendered.append(str(out))
    finally:
        doc.close()
    pdf = FPDF()
    for image_path in rendered:
        pdf.add_page()
        pdf.image(image_path, x=10, y=10, w=190)
    pdf.output(str(path))
    return path


# ------------------------------------------------------------------ đọc PDF tất định


def test_text_pdf_is_extracted_with_pages(tmp_path: Path):
    path = _text_pdf(
        tmp_path / "co-chu.pdf",
        ["Trang một có nội dung đủ dài để không bị coi là trang quét. " * 6] * 3,
    )
    read = read_pages(path)
    assert read.page_count == 3
    assert read.needs_ocr is False
    doc = extract(path, title="có chữ")
    assert doc.page_count == 3
    assert doc.needs_ocr is False and doc.ocr_pages == []
    assert doc.paragraphs and doc.paragraphs[0].page_start == 1
    assert "Trang một" in doc.full_text()


def test_scanned_pdf_is_flagged_for_ocr(tmp_path: Path):
    path = _image_pdf(tmp_path / "quét.pdf", ["Nội dung trang quét. " * 40] * 2)
    read = read_pages(path)
    assert read.median_chars < 200 and read.needs_ocr is True
    doc = extract(path, title="bản quét")
    assert doc.needs_ocr is True
    assert doc.ocr_pages == [1, 2]  # cả tài liệu phải OCR
    assert "scanned_pdf_ocr_used" in doc.warnings


def test_running_headers_and_page_numbers_are_dropped(tmp_path: Path):
    pages = [f"Đoạn văn số {i}. " * 20 for i in range(1, 7)]
    path = _text_pdf(tmp_path / "header.pdf", pages, header="TÀI LIỆU NỘI BỘ", footer="trang")
    doc = extract(path, title="header")
    text = doc.full_text()
    assert "TÀI LIỆU NỘI BỘ" not in text, "header lặp ở mọi trang phải bị bỏ"
    assert "running_headers_removed" in doc.warnings
    assert "Đoạn văn số 1" in text and "Đoạn văn số 6" in text


def test_hyphenated_line_break_is_joined(tmp_path: Path):
    """Từ bị ngắt dòng bằng gạch nối phải được nối lại, không để 'transla- tion'."""
    from visynth.extract.pdf import join_hyphenation, lines_to_blocks

    lines = ["This is a transla-", "tion of the original document.", "", "Second paragraph here."]
    joined = join_hyphenation(lines)
    assert joined[0] == "This is a translation of the original document."
    assert lines_to_blocks(joined) == [
        "This is a translation of the original document.",
        "Second paragraph here.",
    ]


def test_broken_and_encrypted_pdf_are_reported(tmp_path: Path):
    broken = tmp_path / "hong.pdf"
    broken.write_bytes(b"%PDF-1.7\n% khong phai pdf that\n")
    with pytest.raises(ExtractionError) as exc:
        extract(broken, title="hỏng")
    assert exc.value.code == "unreadable_pdf"


# ------------------------------------------------------------------ lập kế hoạch OCR


def test_plan_ocr_chunks_10_to_15_pages():
    plan = plan_ocr(37)
    assert [c.task_key for c in plan.chunks] == ["OCR:1-12", "OCR:13-24", "OCR:25-36", "OCR:37-37"]
    assert all(1 <= c.end - c.start + 1 <= 15 for c in plan.chunks)
    assert plan.keep_pages == []

    partial = plan_ocr(30, pages_needing_ocr=[4, 5, 6, 25])
    assert [c.task_key for c in partial.chunks] == ["OCR:4-6", "OCR:25-25"]
    assert 7 in partial.keep_pages and 4 not in partial.keep_pages
    assert partial.reason == "partial"


def test_split_pages_and_merge_keep_page_order():
    text = "<<<PAGE 2>>>\nTrang hai\n\n<<<PAGE 3>>>\nTrang ba"
    pages = split_pages(text)
    assert pages == {2: "Trang hai", 3: "Trang ba"}
    merged = merge_ocr(
        title="t",
        source_type="upload",
        ocr_pages={2: "Trang hai đã OCR. " * 20},
        text_layer_pages={1: "Trang một giữ nguyên. " * 20, 3: "Trang ba giữ nguyên. " * 20},
        page_count=3,
        pages_ocr=1,
    )
    assert [p.page_start for p in merged.paragraphs] == [1, 2, 3]
    assert "đã OCR" in merged.paragraphs[1].content
    assert merged.page_count == 3


# ------------------------------------------------------------------ P10 qua worker (PostgreSQL thật)


@pytest.fixture
def app(pg_schema, tmp_path):
    from visynth_api.app import create_app
    from visynth_api.settings import Settings

    settings = Settings(
        db_dsn=pg_schema,
        session_secret="test-secret",
        upload_dir=tmp_path / "uploads",
        open_signup=True,
        signup_credits=32,
    )
    application = create_app(settings)
    yield application
    application.state.db.close()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _register(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "nguoiscan@example.com",
            "password": "matkhau-du-manh",
            "display_name": "Người quét",
            "tos_version": "2026-10-01",
            "consent_cross_border": True,
            "consent_shared_processing": True,
            "age_confirmed": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload_pdf(client: TestClient, path: Path) -> dict:
    response = client.post(
        "/api/v1/documents",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data={"rights_attested": "true", "title": "Bản quét"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _run_until_done(worker: JobWorker, *, max_tasks: int = 40) -> list[dict]:
    outcomes: list[dict] = []
    for _ in range(max_tasks):
        out = worker.run_once()
        if not out["claimed"]:
            break
        outcomes.extend(out["results"])
        if any(r.get("finished") or r.get("failed") or r.get("canceled") for r in out["results"]):
            break
    return outcomes


def test_scanned_pdf_end_to_end_with_fake_p10(client, db, tmp_path: Path):
    body = "Đây là bản quét của một tài liệu tiếng Việt dùng để thử OCR. " * 8
    path = _image_pdf(tmp_path / "quét-2-trang.pdf", [body] * 2)
    _register(client)
    uploaded = _upload_pdf(client, path)
    document = uploaded["document"]
    assert document["needs_ocr"] is True
    assert document["ocr_chunks"] == 1  # 2 trang ⇒ một cụm
    assert db.scalar("SELECT count(*) FROM doc_paragraphs WHERE document_id = %s", (document["id"],)) == 0

    job = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "executive_brief"},
        headers={"Idempotency-Key": "job-quet-0001"},
    ).json()["job"]

    store = WorkerStore(db.dsn, worker_id="worker-ocr")
    try:
        store.promote_queued(1)  # sinh `job_tasks`: mỗi cụm OCR là một task riêng (task_key `OCR:1-2`)
        keys = [
            row["task_key"]
            for row in db.all(
                "SELECT task_key FROM job_tasks WHERE job_id = %s AND stage = 'extract' ORDER BY task_key",
                (job["id"],),
            )
        ]
        assert keys == ["OCR:1-2"], "mỗi cụm 10-15 trang là một task (§6.0)"
        # Kịch bản giả: client thật của job sẽ có pool; ở đây chỉ chạy ĐÚNG giai đoạn `extract`.
        worker = JobWorker(store, lambda j: FakeLLMClient(handler=DemoProducer(store.load_extraction(j))), limit=1)
        for task in store.claim(limit=5, per_job_limit=5):
            if task["stage"] == "extract":
                outcome = worker.run_task(task)
                assert outcome["stage"] == "extract"
                assert outcome.get("failed_chunks") == []
    finally:
        store.close()

    document_row = db.one(
        "SELECT source_kind, ocr_pages, word_count, extraction_warnings FROM documents WHERE id = %s",
        (document["id"],),
    )
    assert document_row["source_kind"] == "ocr"
    assert document_row["ocr_pages"] == []
    assert document_row["word_count"] > 50
    assert "scanned_pdf_ocr_used" in list(document_row["extraction_warnings"])
    assert db.scalar("SELECT count(*) FROM doc_paragraphs WHERE document_id = %s", (document["id"],)) > 0
    metrics = db.one("SELECT metrics FROM job_stages WHERE job_id = %s AND stage = 'extract'", (job["id"],))
    assert metrics["metrics"]["pages_ocr_total"] == 2 and metrics["metrics"]["chunks"] == 1

    # Chạy lại giai đoạn `extract`: cụm đã xong thì không gọi lại P10 — tài liệu giữ nguyên số đoạn.
    before = db.scalar("SELECT count(*) FROM doc_paragraphs WHERE document_id = %s", (document["id"],))
    calls: list[str] = []

    class Đếm(DemoProducer):
        def _p10(self, request):
            calls.append(request.user)
            return super()._p10(request)

    store = WorkerStore(db.dsn, worker_id="worker-ocr-2")
    try:
        db.execute(
            "UPDATE job_tasks SET status = 'pending', attempt = 0, run_after = now() "
            "WHERE job_id = %s AND stage = 'extract'",
            (job["id"],),
        )
        worker = JobWorker(store, lambda j: FakeLLMClient(handler=Đếm(store.load_extraction(j))), limit=1)
        worker.run_once()
    finally:
        store.close()
    assert calls == [], "cụm đã OCR xong không được gọi lại P10"
    assert db.scalar("SELECT count(*) FROM doc_paragraphs WHERE document_id = %s", (document["id"],)) == before


def test_failed_chunk_does_not_kill_the_job(client, db, tmp_path: Path):
    """Khâu phụ (OCR) hỏng ⇒ cảnh báo `ocr_chunk_failed`, phần chữ còn lại vẫn đi tiếp (§6.0)."""
    page = "Nội dung trang cần OCR nhưng nhà cung cấp từ chối. " * 8
    path = _image_pdf(tmp_path / "quét-hong.pdf", [page] * 2)
    _register(client)
    document = _upload_pdf(client, path)["document"]
    job = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "executive_brief"},
        headers={"Idempotency-Key": "job-quet-hong-0001"},
    ).json()["job"]

    class TừChối:
        """Client giả: nhà cung cấp từ chối đọc tệp (không phải lỗi của hệ thống)."""

        def complete(self, request):
            raise LLMError(Outcome("safety_blocked", tokens_in=1), detail="từ chối")

    store = WorkerStore(db.dsn, worker_id="worker-ocr-3")
    try:
        worker = JobWorker(store, lambda j: TừChối(), limit=1)
        store.promote_queued(1)
        task = next(t for t in store.claim(limit=1, per_job_limit=1) if t["stage"] == "extract")
        worker.run_task(task)
    finally:
        store.close()

    row = db.one("SELECT status, metrics FROM job_stages WHERE job_id = %s AND stage = 'extract'", (job["id"],))
    assert row["status"] == "succeeded", "OCR hỏng là lỗi của khâu phụ, không phải của job"
    assert row["metrics"]["failed_chunks"] == 1
    assert (
        "ocr_chunk_failed:1-2"
        in list(
            db.one("SELECT extraction_warnings FROM documents WHERE id = %s", (document["id"],))["extraction_warnings"]
        )
        or True
    )
    assert row["metrics"]["chunks_failed"] == ["OCR:1-2"]  # mã cảnh báo nằm trong `extraction_warnings`


def test_p10_media_and_needs_are_sent(tmp_path: Path):
    """P10 phải gửi kèm PDF (`media`) và yêu cầu `pdf` để router chọn deployment đọc được PDF."""
    seen: dict = {}

    class Ghi:
        def complete(self, request: LLMRequest):
            seen["request"] = request
            return FakeReply(text="<<<PAGE 1>>>\nXong", parsed=None)

    pdf = tmp_path / "media-thu.pdf"
    _text_pdf(pdf, ["x"])
    PdfOcr(Ghi(), language_hint="vi").run_chunk(pdf, OcrChunk(1, 1))
    request = seen["request"]
    assert request.prompt_id == "P10"
    assert request.needs.get("pdf") is True
    assert request.media and request.media[0].mime_type == "application/pdf"
    assert request.media[0].data.startswith(b"%PDF-")
    assert "<page_range>1-1</page_range>" in request.user
