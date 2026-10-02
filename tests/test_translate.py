"""Giai đoạn `translate` — mức `full_translation` (M1, SPEC §6.10/§6.11).

Ba nhóm:

* **Phép kiểm tất định** — `visynth.pipeline.translate.check_chunk` phải bắt đúng: căn pid 1:1, đoạn rỗng,
  tỷ lệ độ dài ngoài [0.6, 2.2], số liệu không có trong nguồn, thuật ngữ sai glossary và đoạn nghi
  chưa dịch (nguồn không phải tiếng Việt mà bản dịch gần như không có dấu). Đây là hàng rào duy nhất
  giữa "model trả JSON hợp lệ" và "bản dịch dùng được", nên mỗi ngưỡng đều được kiểm ở cả hai phía.
* **Đường chạy đầu-cuối** — `run_document(level="full_translation")` chỉ gọi P0, P1, P9 (không P2–P7),
  sinh `translation_items` căn 1:1 và Markdown bản dịch.
* **Worker + API trên PostgreSQL thật** — job dịch đầy đủ sinh đúng ba task `profile/glossary/translate`,
  ghi `translation_items`, và `POST /jobs` đánh dấu `translate` là `pending` (không `skipped`).

Không mạng, không token thật: mọi lời gọi LLM đi qua `DemoProducer`.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visynth.checks.glossary import GlossaryEntry
from visynth.eval import DemoProducer, demo_client, demo_extraction
from visynth.llm.fake import FakeReply
from visynth.pipeline.models import JobOptions
from visynth.pipeline.run import run_document
from visynth.pipeline.stages import Pipeline
from visynth.pipeline.translate import (
    Item,
    build_markdown,
    check_chunk,
    input_pids,
    merge_items,
)
from visynth.segment import Segment
from visynth.worker import JobWorker, WorkerStore

SRC_A = "The reactor reached a peak temperature of 1200 degrees Celsius during the test run."
SRC_B = "Engineers recorded the pressure drop across the cooling loop every ten seconds."
#: Hai câu trên dịch sẵn (bản dịch mẫu): có dấu tiếng Việt, giữ nguyên số 1200, dài xấp xỉ nguồn.
VI_A = "Lò phản ứng đạt nhiệt độ đỉnh 1200 độ C trong lần chạy thử nghiệm."
VI_B = "Các kỹ sư ghi lại độ giảm áp qua vòng làm mát sau mỗi mười giây."
FIXTURE = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "demo_lecture.txt"


def _segment(*texts: str) -> Segment:
    paragraphs = [types.SimpleNamespace(pid=f"P00000{i + 1}", content=text, part=None) for i, text in enumerate(texts)]
    return Segment(
        segment_id="S0001",
        idx=0,
        first_pid=paragraphs[0].pid,
        last_pid=paragraphs[-1].pid,
        token_count=len(" ".join(texts).split()),
        section_id="S01",
        paragraphs=paragraphs,
    )


def _pair(*pairs: tuple[str, str]) -> tuple[Segment, list[dict]]:
    seg = _segment(*[src for src, _ in pairs])
    return seg, [{"pid": f"P00000{i + 1}", "vi": vi} for i, (_, vi) in enumerate(pairs)]


# ------------------------------------------------------------------ căn pid 1:1


def test_happy_path_has_no_problems():
    seg, items = _pair((SRC_A, VI_A), (SRC_B, VI_B))
    checked, problems = check_chunk(seg, {"items": items, "notes": []})
    assert problems == []
    assert [i.pid for i in checked] == ["P000001", "P000002"]
    assert all(not i.flagged for i in checked)
    # bản dịch mẫu phải nằm trong khoảng tỷ lệ độ dài, nếu không phép kiểm ở trên là vô nghĩa
    assert 0.6 <= len(VI_A) / len(SRC_A) <= 2.2


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda items: items[:-1], "thiếu pid"),
        (lambda items: [*items, {"pid": "P000009", "vi": "đoạn lạ"}], "pid lạ"),
        (lambda items: [{**items[0]}, *items], "pid trùng"),
        (lambda items: [{"pid": items[0]["pid"], "vi": "   "}, items[1]], "empty"),
    ],
)
def test_cardinality_and_empty_are_caught(mutate, expected):
    _seg, items = _pair((SRC_A, VI_A), (SRC_B, VI_B))
    checked, problems = check_chunk(_seg, {"items": mutate(items), "notes": []})
    assert any(expected in p for p in problems), problems
    # mục bị đánh cờ vẫn giữ pid để người đọc đối chiếu ngược về nguồn
    assert all(i.pid.startswith("P") for i in checked)


def test_notes_are_carried_to_items():
    seg, items = _pair((SRC_A, VI_A))
    checked, _ = check_chunk(seg, {"items": items, "notes": [{"pid": "P000001", "note_vi": "giữ nguyên đơn vị °C"}]})
    assert checked[0].note_vi == "giữ nguyên đơn vị °C"


# ------------------------------------------------------------------ tỷ lệ độ dài


def test_length_ratio_bounds_hold_on_both_sides():
    seg, _ = _pair((SRC_A, VI_A))  # nguồn 87 ký tự ⇒ có kiểm tỷ lệ
    # giữ nguyên số 1200 trong mọi biến thể để cô lập phép kiểm tỷ lệ độ dài
    _, too_short = check_chunk(seg, {"items": [{"pid": "P000001", "vi": "a" * int(len(SRC_A) * 0.3) + " 1200"}]})
    _, too_long = check_chunk(seg, {"items": [{"pid": "P000001", "vi": "a" * int(len(SRC_A) * 2.5) + " 1200"}]})
    assert any("too_short" in p for p in too_short), too_short
    assert any("too_long" in p for p in too_long), too_long
    # sát ngưỡng dưới nhưng vẫn trong khoảng ⇒ không cờ
    _, ok = check_chunk(seg, {"items": [{"pid": "P000001", "vi": "a" * int(len(SRC_A) * 0.62) + " 1200"}]})
    assert ok == [], ok


def test_short_source_paragraph_is_exempt_from_ratio():
    seg, _ = _pair(("Yes.", "Vâng, đúng vậy, theo tài liệu."))
    _, problems = check_chunk(seg, {"items": [{"pid": "P000001", "vi": "Vâng, đúng vậy, theo tài liệu."}]})
    assert problems == [], problems


# ------------------------------------------------------------------ số liệu & thuật ngữ


def test_numbers_must_exist_in_the_source():
    seg, _ = _pair((SRC_A, VI_A))
    _, clean = check_chunk(seg, {"items": [{"pid": "P000001", "vi": VI_A}]})
    assert clean == []
    _, problems = check_chunk(seg, {"items": [{"pid": "P000001", "vi": VI_A.replace("1200", "1500")}]})
    assert any("numbers:1500" in p for p in problems), problems


def test_numbers_dropped_by_the_translation_are_caught():
    seg, _ = _pair((SRC_A, VI_A))
    _, problems = check_chunk(
        seg, {"items": [{"pid": "P000001", "vi": "Lò phản ứng đạt nhiệt độ đỉnh rất cao trong lần chạy thử."}]}
    )
    assert any("missing_numbers:1200" in p for p in problems), problems


def test_glossary_forbidden_variant_and_untranslated_source_term():
    seg, _ = _pair(("The PID controller keeps the loop stable.", "PID controller giữ ổn định."))
    entries = [
        GlossaryEntry(source_term="PID", target_term="bộ điều khiển PID", forbidden_variants=("PID controller",))
    ]
    _, problems = check_chunk(seg, {"items": [{"pid": "P000001", "vi": "PID controller giữ ổn định."}]}, entries)
    assert any("forbidden_variant" in p for p in problems), problems
    assert any("untranslated_source_term" in p for p in problems), problems


def test_first_use_of_keep_original_term_needs_source_in_parentheses():
    src = "The control loop keeps the system stable and safe."
    seg, items = _pair((src, "Vòng điều khiển giữ hệ thống ổn định."))
    entries = [GlossaryEntry(source_term="loop", target_term="vòng điều khiển", keep_original=True)]
    _, missing = check_chunk(seg, {"items": items}, entries, first_use_terms={"loop"})
    assert any("missing_original_on_first_use" in p for p in missing), missing
    _, full = check_chunk(
        seg,
        {"items": [{"pid": "P000001", "vi": "Vòng điều khiển (loop) giữ hệ thống ổn định."}]},
        entries,
        first_use_terms={"loop"},
    )
    assert full == [], full


def test_suspected_untranslated_only_when_source_is_not_vietnamese():
    seg, items = _pair((SRC_A, SRC_A))  # "bản dịch" để nguyên tiếng Anh
    _, problems = check_chunk(seg, {"items": items}, source_lang="en")
    assert any("untranslated" in p for p in problems), problems
    # cùng văn bản đó nhưng nguồn đã là tiếng Việt (tự dịch) thì không đánh cờ
    _, vi_source = check_chunk(seg, {"items": items}, source_lang="vi")
    assert vi_source == [], vi_source


# ------------------------------------------------------------------ chạy lại & gộp


def test_merge_keeps_translation_when_retry_still_fails():
    seg, _ = _pair((SRC_A, VI_A))
    previous = [Item(pid="P000001", vi=VI_A, flagged=False)]
    _checked, problems = check_chunk(seg, {"items": [{"pid": "P000001", "vi": ""}]})
    assert problems
    merged = merge_items(previous, [Item(pid="P000001", vi="", flagged=True, issues=["empty"])])
    assert merged[0].vi == VI_A and merged[0].flagged, "lần chạy lại hỏng không được xoá bản dịch tốt"


# ------------------------------------------------------------------ Markdown & hạng


def test_build_markdown_contains_every_pid_and_marks_flagged():
    ext = demo_extraction()
    paragraphs = list(ext.paragraphs[:10])
    seg = Segment(
        segment_id="S0001",
        idx=0,
        first_pid=paragraphs[0].pid,
        last_pid=paragraphs[-1].pid,
        token_count=10,
        section_id="S01",
        paragraphs=paragraphs,
    )
    items = {
        p.pid: Item(pid=p.pid, vi=f"Bản dịch của {p.pid}.", flagged=(idx == 3)) for idx, p in enumerate(paragraphs)
    }
    md = build_markdown(ext, [seg], items)
    for pid in input_pids(seg):
        assert f"Nguồn: **{pid}**" in md
    assert "⚠️" in md
    assert "hạng C" not in md  # 1/10 = 10% ⇒ chưa tới ngưỡng hạng C
    assert "Nguồn · **" in build_markdown(ext, [seg], items, bilingual=True)


def test_more_than_15_percent_flagged_is_grade_c():
    pipeline = Pipeline(demo_extraction(), demo_client(), JobOptions(level="full_translation"))
    pipeline.result.translation_items = [Item(pid=f"P{i:06d}", vi="câu", flagged=i < 2) for i in range(10)]
    assert pipeline.metrics()["grade"] == "C"  # 2/10 = 20% > 15%
    pipeline.result.translation_items = [Item(pid=f"P{i:06d}", vi="câu", flagged=i < 1) for i in range(10)]
    assert pipeline.metrics()["grade"] == "A"  # 1/10 = 10% ≤ 15%


# ------------------------------------------------------------------ đầu-cuối, không cần CSDL


def test_run_document_full_translation_never_calls_synthesis_prompts():
    ext = demo_extraction()
    client = demo_client(ext)
    result = run_document(ext, client, JobOptions(level="full_translation"))
    assert [c.prompt_id for c in client.calls] == ["P0", "P1", "P9"]
    assert result.grade == "A"
    assert [i.pid for i in result.translation_items] == [p.pid for p in ext.paragraphs]
    assert result.stats["translation_items"] == len(ext.paragraphs)
    assert result.stats["translation_flagged"] == 0
    assert result.markdown.startswith(f"# {ext.title}") and "Nguồn: **P000001**" in result.markdown
    # fixture là tiếng Việt nên bản dịch trùng nguồn — mọi phép kiểm tất định vẫn chạy thật trên đó
    assert result.translation_items[0].vi.strip() == ext.paragraphs[0].content.strip()


# ------------------------------------------------------------------ worker + API (PostgreSQL thật)


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


def _register(client: TestClient, email: str = "nguoidich@example.com") -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "matkhau-du-manh",
            "display_name": "Người dịch thử",
            "tos_version": "2026-10-01",
            "consent_cross_border": True,
            "consent_shared_processing": True,
            "age_confirmed": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _run_until_done(worker: JobWorker, *, max_stages: int = 8) -> list[dict]:
    outcomes: list[dict] = []
    for _ in range(max_stages):
        out = worker.run_once()
        if not out["claimed"]:
            break
        outcomes.extend(out["results"])
        if any(r.get("finished") or r.get("failed") or r.get("canceled") for r in out["results"]):
            break
    return outcomes


def test_job_stages_marked_pending_for_translate(client, db):
    _register(client)
    document = client.post(
        "/api/v1/documents",
        files={"file": ("bai-giang.txt", FIXTURE.read_bytes(), "text/plain")},
        data={"rights_attested": "true", "title": "Bài giảng EN"},
    ).json()["document"]
    created = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "full_translation"},
        headers={"Idempotency-Key": "job-dich-day-du-0001"},
    )
    assert created.status_code == 202, created.text
    stages = {
        row["stage"]: row["status"]
        for row in db.all("SELECT stage, status FROM job_stages WHERE job_id = %s", (created.json()["job"]["id"],))
    }
    assert stages["translate"] == "pending"
    assert stages["map"] == "skipped" and stages["write"] == "skipped" and stages["repair"] == "skipped"
    assert stages["profile"] == "pending" and stages["glossary"] == "pending"


def test_worker_runs_translate_end_to_end(client, db):
    _register(client)
    document = client.post(
        "/api/v1/documents",
        files={"file": ("bai-giang.txt", FIXTURE.read_bytes(), "text/plain")},
        data={"rights_attested": "true", "title": "Bài giảng EN"},
    ).json()["document"]
    job = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "full_translation"},
        headers={"Idempotency-Key": "job-dich-day-du-0002"},
    ).json()["job"]

    store = WorkerStore(db.dsn, worker_id="worker-dich")
    try:
        worker = JobWorker(store, lambda j: demo_client(store.load_extraction(j)), limit=1)
        outcomes = _run_until_done(worker)
        assert outcomes and outcomes[-1].get("report_id"), outcomes
    finally:
        store.close()

    detail = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert detail["job"]["status"] == "succeeded"
    stages = {row["stage"]: row["status"] for row in detail["stages"]}
    assert stages["translate"] == "succeeded" and stages["map"] == "skipped"

    rows = db.all("SELECT pid, vi, flagged FROM translation_items WHERE job_id = %s ORDER BY pid", (job["id"],))
    assert len(rows) == 17 and rows[0]["pid"] == "P000001" and rows[0]["flagged"] is False
    metrics = db.one("SELECT metrics FROM job_stages WHERE job_id = %s AND stage = 'translate'", (job["id"],))
    assert metrics["metrics"]["items"] == 17 and metrics["metrics"]["flagged"] == 0

    report = client.get(f"/api/v1/reports/{detail['report_id']}").json()
    assert "Nguồn: **P000001**" in report["markdown"]
    exported = client.get(f"/api/v1/reports/{detail['report_id']}/export?format=md")
    assert exported.status_code == 200 and "Nguồn: **P000017**" in exported.text


def test_worker_translate_retries_once_then_flags(client, db):
    """Segment trả JSON hỏng ở lần đầu ⇒ chạy lại đúng MỘT lần; vẫn hỏng thì đánh cờ, không mất bản dịch."""

    class _Blind(DemoProducer):
        """Lần gọi P9 đầu tiên trả THIẾU một pid; lần thứ hai trả đủ (giống model sửa theo phản hồi)."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.rounds = 0

        def _p9(self, request):
            self.rounds += 1
            reply = super()._p9(request)
            if self.rounds == 1:
                payload = dict(reply.parsed or {})
                payload["items"] = list(payload.get("items") or [])[:-1]
                return FakeReply.json(payload)
            return reply

    _register(client, "nguoidich2@example.com")
    document = client.post(
        "/api/v1/documents",
        files={"file": ("bai-giang.txt", FIXTURE.read_bytes(), "text/plain")},
        data={"rights_attested": "true", "title": "Bài giảng EN"},
    ).json()["document"]
    job = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "full_translation"},
        headers={"Idempotency-Key": "job-dich-day-du-0003"},
    ).json()["job"]

    created: dict = {}

    def factory(job):
        from visynth.llm.fake import FakeLLMClient

        producer = _Blind(store.load_extraction(job))
        created["producer"] = producer
        return FakeLLMClient(handler=producer)

    store = WorkerStore(db.dsn, worker_id="worker-dich-2")
    try:
        worker = JobWorker(store, factory, limit=1)
        outcomes = _run_until_done(worker)
        assert outcomes and outcomes[-1].get("report_id"), outcomes
    finally:
        store.close()

    assert created["producer"].rounds >= 2, "segment phải được chạy lại đúng một lần với phản hồi lỗi"
    rows = db.all("SELECT flagged FROM translation_items WHERE job_id = %s", (job["id"],))
    assert len(rows) == 17 and all(row["flagged"] is False for row in rows), "chạy lại một lần phải sửa được"


def test_untranslated_tamper_is_retried_then_flagged_but_keeps_text():
    """Kịch bản xấu: bản dịch không áp glossary ⇒ chạy lại một lần rồi đánh cờ, KHÔNG xoá bản dịch."""
    ext = demo_extraction()
    client = demo_client(ext, tamper="untranslated")
    result = run_document(ext, client, JobOptions(level="full_translation"))
    assert [c.prompt_id for c in client.calls] == ["P0", "P1", "P9", "P9"], "phải chạy lại đúng một lần"
    flagged = [i for i in result.translation_items if i.flagged]
    assert flagged, "bản dịch sai thuật ngữ phải bị đánh cờ"
    assert all(i.vi.strip() for i in flagged), "đoạn bị đánh cờ vẫn giữ bản dịch để người đọc dùng được"
    assert any(w.startswith("translate_flagged:") for w in result.warnings)
    assert result.stats["translation_flagged"] == len(flagged)
    # 1/17 đoạn < 15% ⇒ chưa rơi xuống hạng C, nhưng cảnh báo vẫn hiển thị trong Markdown
    assert len(flagged) / len(result.translation_items) < 0.15
    assert result.grade == "A" and "⚠️" in result.markdown


def test_worker_defers_task_when_pool_has_no_room(client, db):
    """Pool hết chỗ ⇒ hoãn task, KHÔNG tính vào số lần thử (§6.10 bước 1)."""
    from visynth.llm.base import LLMError, Outcome

    class _Busy:
        def complete(self, _request):
            raise LLMError(Outcome("deferred", retry_after_s=120), detail="pool chưa có chỗ")

    _register(client, "nguoidich3@example.com")
    document = client.post(
        "/api/v1/documents",
        files={"file": ("bai-giang.txt", FIXTURE.read_bytes(), "text/plain")},
        data={"rights_attested": "true", "title": "Bài giảng EN"},
    ).json()["document"]
    job = client.post(
        "/api/v1/jobs",
        json={"document_id": document["id"], "level": "full_translation"},
        headers={"Idempotency-Key": "job-dich-day-du-0004"},
    ).json()["job"]

    store = WorkerStore(db.dsn, worker_id="worker-dich-3")
    try:
        worker = JobWorker(store, lambda _j: _Busy(), limit=1)
        outcome = worker.run_once()["results"][0]
    finally:
        store.close()

    assert outcome["deferred"] is True and outcome["wait_s"] == 120.0
    row = db.one("SELECT status, attempt FROM job_tasks WHERE job_id = %s AND stage = 'profile'", (job["id"],))
    assert row["status"] == "pending" and row["attempt"] == 0, "hoãn không được tính là lần thử thất bại"
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["job"]["status"] == "running"
