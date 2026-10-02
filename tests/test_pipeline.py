"""Tích hợp pipeline P0–P8 trên kịch bản giả (M0-W2) — không mạng, không token thật.

Kịch bản và biến thể phá hoại nằm trong `visynth.eval.demo`; ở đây kiểm tra luồng, hợp đồng schema,
đường sửa lỗi P7 và CLI `visynth run --demo`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visynth.cli import main
from visynth.eval import TAMPER_MODES, DemoProducer, demo_client, demo_extraction
from visynth.pipeline.models import JobOptions
from visynth.pipeline.run import run_document
from visynth.prompts import default_schemas_dir
from visynth.structured import SchemaStore

#: prompt → schema mà pipeline bắt buộc phải kiểm tra (bảng đầy đủ ở SPEC §8.2).
STRUCTURED_REPLIES = {
    "P0": "doc_profile.schema.json",
    "P1": "glossary_candidates.schema.json",
    "P2": "segment_analysis.schema.json",
    "P3": "report_plan.schema.json",
    "P4": "section_output.schema.json",
    "P5": "faithfulness.schema.json",
    "P6": "coverage.schema.json",
    "P7": "repair_output.schema.json",
}


def _run(tamper: str | None = None, level: str = "deep_synthesis"):
    ext = demo_extraction()
    client = demo_client(ext, tamper=tamper, level=level)
    result = run_document(ext, client, JobOptions(level=level))
    return ext, client, result


@pytest.fixture(scope="module")
def happy():
    return _run()


@pytest.fixture(scope="module")
def runs():
    return {mode: _run(mode) for mode in TAMPER_MODES}


def _validate_structured_replies(ext, client) -> set[str]:
    """Phát lại kịch bản đúng thứ tự và đối chiếu từng đầu ra JSON với schema trong `docs/`."""
    store = SchemaStore(default_schemas_dir())
    producer = DemoProducer(ext)
    checked: set[str] = set()
    for request in client.calls:
        schema = STRUCTURED_REPLIES.get(request.prompt_id)
        if not schema:
            continue
        reply = producer(request)  # phát lại cùng trạng thái, cùng thứ tự
        payload = reply.parsed if reply.parsed is not None else json.loads(reply.text)
        store.validate(payload, store.load(schema), what=request.prompt_id)
        checked.add(request.prompt_id)
    return checked


# ------------------------------------------------------------------ đường hạnh phúc


def test_happy_path_is_green(happy):
    _ext, _client, r = happy
    assert r.grade == "A"
    assert r.metrics["coverage_core"] == 1.0
    assert r.metrics["unresolved_blocks"] == 0
    assert r.metrics["blocks_removed"] == 0
    assert r.warnings == []
    assert len(r.sections) == 4
    assert [s.id for s in r.sections] == ["S01", "S02", "S03", "S04"]
    assert len(r.blocks) == 9
    assert all(not b.removed and not b.flagged for b in r.blocks)
    assert r.glossary_auto_confirmed and len(r.glossary) == 2
    assert r.units and all(u.state == "active" for u in r.units if u.importance != "minor")
    assert sum(1 for u in r.units if u.state == "omitted") == 3
    assert r.stats["omitted_by_reason"] == {"level_policy": 3}
    assert r.stats["units_core"] == 8
    assert r.stats["sections"] == 4
    assert r.stats["llm_calls"] == r.stats_llm.calls > 0


def test_report_markdown_has_required_parts(happy):
    _ext, _client, r = happy
    md = r.markdown
    assert md.startswith("# ")
    assert "Báo cáo do AI tổng hợp" in md
    for s in r.sections:
        assert f"## {s.title_vi}" in md
    assert "## Bảng dữ kiện" in md
    assert "## Phụ lục thuật ngữ" in md
    assert "## Phạm vi & cách xử lý" in md
    assert "## Nguồn trích dẫn" in md
    assert "quy trình" in md  # thuật ngữ đích của glossary có trong báo cáo
    # thuật ngữ nguồn chỉ được xuất hiện ở phụ lục glossary, không bị bỏ trần trong thân bài
    body = " ".join(b.markdown_vi for b in r.blocks)
    assert "pipeline" not in body
    assert md.count("[1]") >= 1 and "P000002" in md


def test_summary_is_single_line_per_part(happy):
    _ext, _client, r = happy
    text = r.summary()
    assert "mức deep_synthesis" in text and "hạng A" in text
    assert "coverage_core: 1.00" in text


def test_every_structured_reply_validates_against_docs_schemas(happy):
    ext, client, _r = happy
    # đường hạnh phúc không cần P7 (không có gì phải sửa)
    assert _validate_structured_replies(ext, client) == set(STRUCTURED_REPLIES) - {"P7"}


@pytest.mark.parametrize("tamper", ["number", "glossary", "fabricated", "plan"])
def test_repair_replies_also_validate(runs, tamper):
    ext, client, _r = runs[tamper]
    checked = _validate_structured_replies(ext, client)
    assert "P7" in checked if tamper != "plan" else "P3" in checked


def test_pipeline_is_deterministic():
    _e1, _c1, a = _run()
    _e2, _c2, b = _run()
    assert a.markdown == b.markdown
    assert a.metrics == b.metrics
    assert [u.as_dict() for u in a.units] == [u.as_dict() for u in b.units]


@pytest.mark.parametrize("level", ["detailed_synthesis", "deep_synthesis", "executive_brief"])
def test_text_levels_run(level):
    _ext, _client, r = _run(level=level)
    assert r.level == level
    assert r.grade in ("A", "B")
    assert r.sections and r.blocks
    assert r.markdown


def test_events_cover_all_stages(happy):
    _ext, _client, r = happy
    stages = [e["data"].get("stage") for e in r.events if e["type"] == "stage_done"]
    assert stages == ["profile", "glossary", "map", "consolidate", "write", "verify", "repair"]
    assert any(e["type"] == "job_started" for e in r.events)
    assert r.events[-1]["type"] == "job_succeeded" and r.events[-1]["data"]["grade"] == "A"


# ------------------------------------------------------------------ các biến thể phá hoại


def test_number_tamper_is_repaired(runs):
    r = runs["number"][2]
    assert r.metrics["coverage_core"] == 1.0 and r.grade == "A"
    assert r.warnings == []
    assert all(not b.removed and not b.flagged for b in r.blocks)
    assert "99%" not in r.markdown
    assert "12%" in r.markdown
    rounds = [e["data"]["rounds"] for e in r.events if e["type"] == "stage_done" and e["data"].get("stage") == "repair"]
    assert rounds == [1]


def test_glossary_tamper_is_repaired(runs):
    r = runs["glossary"][2]
    assert all(not b.removed and not b.flagged for b in r.blocks)
    assert "segment" not in " ".join(b.markdown_vi for b in r.blocks)
    assert all(i.get("type") != "term_inconsistency" for b in r.blocks for i in b.issues)


def test_fabricated_block_is_removed(runs):
    r = runs["fabricated"][2]
    removed = [b.block_id for b in r.blocks if b.removed]
    assert removed == ["S02.b01"]
    assert r.metrics["coverage_core"] < 0.9 and r.grade == "C"
    assert r.metrics["blocks_total"] == 8
    assert "40 ngôn ngữ" not in r.markdown
    assert any(e["type"] == "block_removed" and e["data"]["block"] == "S02.b01" for e in r.events)


def test_plan_tamper_is_autofixed(runs):
    r = runs["plan"][2]
    assert "plan_autofixed" in r.warnings
    assert r.metrics["coverage_core"] == 1.0
    core = {u.id for u in r.units if u.importance == "core"}
    cited = {c for b in r.blocks if not b.removed for c in b.cites}
    assert core <= cited
    assert all(s.unit_ids for s in r.sections)


# ------------------------------------------------------------------ CLI


def test_cli_run_demo_writes_markdown(tmp_path, capsys):
    out = tmp_path / "bao-cao.md"
    assert main(["run", "--demo", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# ") and "## Nguồn trích dẫn" in text
    captured = capsys.readouterr().out
    assert "hạng A" in captured and "Đã ghi báo cáo Markdown" in captured


def test_cli_run_demo_json(capsys):
    assert main(["run", "--demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["grade"] == "A"
    assert payload["stats"]["units_core"] == 8
    assert payload["markdown"].startswith("# ")
    assert len(payload["sections"]) == 4


def test_cli_run_requires_demo_for_now(capsys, tmp_path):
    path = tmp_path / "tai-lieu.txt"
    path.write_text("Nội dung bất kỳ.", encoding="utf-8")
    assert main(["run", str(path)]) == 2
    assert "M0-W3" in capsys.readouterr().err


def test_cli_run_tamper_choice_is_validated():
    with pytest.raises(SystemExit):
        main(["run", "--demo", "--tamper", "khong-ton-tai"])


def test_no_artifacts_written_by_demo_run():
    """Chạy demo không tạo tệp trong cây mã nguồn (chỉ trả kết quả trong bộ nhớ)."""
    root = Path(__file__).resolve().parents[1]
    before = {p.name for p in (root / "apps" / "worker" / "visynth").glob("*.md")}
    _run()
    after = {p.name for p in (root / "apps" / "worker" / "visynth").glob("*.md")}
    assert before == after
