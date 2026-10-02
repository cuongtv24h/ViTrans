"""Kiểm thử bộ chấm điểm golden (M0-W4, SPEC §16.2, §2.2).

Hai tầng:

* **Toán của bộ chấm** — dựng `run.json` tổng hợp trong test rồi khẳng định từng chỉ số, các bẫy đã gặp thật
  (`5% nguồn` không được khớp trong `35% nguồn`; dòng `**Mức:** deep_synthesis` không phải văn xuôi để lint);
* **Đường chạy đầu-cuối** — `visynth run --demo --artifacts` ghi artifact, `eval/score.py` chấm nó với meta của
  tài liệu demo; khẳng định kết luận ĐẠT và các khoá JSON ổn định cho tầng so baseline.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCORE_PATH = ROOT / "eval" / "score.py"
GOLDEN = ROOT / "eval" / "golden"


def _load_scorer():
    spec = importlib.util.spec_from_file_location("visynth_eval_score", SCORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


score = _load_scorer()


def _block(block_id: str, text: str, *, cites=("u1",), verdict="supported", flagged=False, issues=(), removed=False):
    return {
        "block_id": block_id,
        "section_id": "S01",
        "markdown_vi": text,
        "cites": list(cites),
        "verdict": verdict,
        "flagged": flagged,
        "removed": removed,
        "check": {"issues": [dict(i) for i in issues]},
    }


def _run(markdown: str, blocks: list[dict], **extra) -> dict:
    return {
        "title": "t",
        "level": "deep_synthesis",
        "grade": "A",
        "markdown": markdown,
        "blocks_full": blocks,
        "stats": {},
        "warnings": [],
        **extra,
    }


META = {
    "doc_id": "t",
    "title": "t",
    "kind": "smoke",
    "level": "deep_synthesis",
    "source_path": "x.txt",
    "target_words": 10,
    "must_cover": [
        {"concept": "quang hợp", "variants": ["photosynthesis"]},
        {"concept": "diệp lục", "variants": []},
        {"concept": "chu trình Calvin", "core": False},
    ],
    "trap_facts": [
        {"fact": "35%", "kind": "number", "wrong_variants": ["5% nguồn"]},
        {"fact": "1972", "kind": "number", "wrong_variants": ["1971"]},
    ],
    "glossary": [{"source_term": "stomata", "target_term": "khí khổng", "forbidden_variants": ["lỗ khí"]}],
}


# ------------------------------------------------------------------ từng chỉ số


def test_coverage_counts_yes_partial_no():
    blocks = [
        _block("S01.b01", "Quá trình quang hợp diễn ra ở lục lạp."),
        _block("S01.b02", "Diệp lục hấp thụ ánh sáng.", cites=(), flagged=True),
    ]
    report = "Quá trình quang hợp diễn ra ở lục lạp. Diệp lục hấp thụ ánh sáng."
    value, detail = score.coverage_core(_run(report, blocks), META)
    assert detail["quang hợp"]["status"] == "yes"
    assert detail["diệp lục"]["status"] == "partial"  # chỉ có ở khối bị đánh cờ
    assert "chu trình Calvin" not in detail  # core: false không tính
    assert value == pytest.approx(0.75)  # (1 + 0.5) / 2


def test_coverage_core_ignores_non_core_entries():
    text = "Quang hợp và diệp lục và chu trình Calvin."
    blocks = [_block("S01.b01", text)]
    value, detail = score.coverage_core(_run(text, blocks), META)
    assert set(detail) == {"quang hợp", "diệp lục"}
    assert value == 1.0


def test_faithfulness_and_fabricated_remaining():
    blocks = [
        _block("S01.b01", "ok"),
        _block("S01.b02", "sai", verdict="fabricated"),
        _block("S01.b03", "bị xoá", verdict="fabricated", removed=True),
        _block("S01.b04", "lỗi số", issues=[{"type": "number_mismatch", "hard": True, "detail": "12% vs 15%"}]),
    ]
    rate, fabricated, detail = score.faithfulness(_run("x", blocks))
    assert fabricated == 1  # khối đã xoá không tính
    assert rate == pytest.approx(1 / 3, abs=1e-4)  # bộ chấm làm tròn 4 chữ số
    assert set(detail["not_ok"]) == {"S01.b02", "S01.b04"}


def test_soft_issues_do_not_break_faithfulness():
    blocks = [_block("S01.b01", "ổn", issues=[{"type": "other", "hard": False}])]
    rate, fabricated, _ = score.faithfulness(_run("x", blocks))
    assert (rate, fabricated) == (1.0, 0)


def test_terminology_lints_body_only_and_counts_occurrences():
    markdown = (
        "# Báo cáo\n"
        "*phụ đề in nghiêng*\n"
        "**Nguồn:** demo · **Mức:** deep_synthesis · **Hạng chất lượng:** A\n"
        "\n"
        "Khí khổng (stomata) đóng lại. Lỗ khí cũng đóng.\n"
    )
    value, detail = score.terminology(_run(markdown, []), META)
    kinds = {v["kind"] for v in detail["violations"]}
    assert "forbidden_variant" in kinds  # 'lỗ khí'
    assert "untranslated_source_term" not in kinds  # đã có dạng 'khí khổng (stomata)'
    assert value < 1.0


def test_trap_facts_boundary_does_not_match_inside_a_longer_number():
    """`5% nguồn` là biến thể sai của trap fact `35%`, nhưng KHÔNG được khớp trong 'khoảng 35% nguồn'."""
    good = _run("Tổng hợp chi tiết khoảng 35% nguồn và năm 1972.", [])
    accuracy, errors, detail = score.trap_facts(good, META)
    assert errors == 0, detail
    assert accuracy == 1.0

    bad = _run("Tổng hợp chi tiết khoảng 5% nguồn và năm 1971.", [])
    accuracy, errors, _ = score.trap_facts(bad, META)
    assert errors == 2
    assert accuracy == 0.0

    missing = _run("Không nhắc gì.", [])
    accuracy, errors, detail = score.trap_facts(missing, META)
    assert errors == 0 and accuracy == 0.0
    assert {r["status"] for r in detail["detail"]} == {"missing"}


def test_length_and_cost():
    ratio, detail = score.length(_run("một hai ba bốn năm", []), {"target_words": 10})
    assert (detail["report_words"], ratio) == (5, 0.5)
    assert (
        score.cost(_run("x", []), score.pick_price(score.STARTER_PRICES, "gemini-3.8-flash", score.date(2026, 10, 2)))[
            "applicable"
        ]
        is False
    )
    priced = score.cost(
        _run("x", [], llm_calls={"calls": 2, "tokens_in": 1_000_000, "tokens_out": 0}),
        score.pick_price(score.STARTER_PRICES, "gemini-3.8-flash", score.date(2026, 10, 2)),
    )
    assert priced["applicable"] is True and priced["cost_usd"] == pytest.approx(0.75)


# ------------------------------------------------------------------ kết luận


def test_verdict_fails_when_a_trap_fact_is_wrong_and_names_the_blocking_gate():
    run = _run(
        "Khoảng 35% nguồn, năm 1971, khí khổng (stomata), quang hợp, diệp lục.",
        [_block("S01.b01", "Khoảng 35% nguồn, năm 1971, quang hợp, diệp lục, khí khổng (stomata).")],
    )
    report = score.score_run(run, META)
    assert report["verdict"] == "fail"
    assert "trap_fact_errors" in report["blocking"]
    assert report["gates"]["cost_usd"]["applicable"] is False  # không có sổ llm_calls ở kịch bản giả


def test_verdict_passes_on_clean_run_and_markdown_lists_metrics():
    blocks = [_block("S01.b01", "Quang hợp, diệp lục, khí khổng (stomata), khoảng 35% nguồn, năm 1972.")]
    run = _run("Quang hợp, diệp lục, khí khổng (stomata), khoảng 35% nguồn, năm 1972.", blocks)
    run["llm_calls"] = {"calls": 1, "tokens_in": 1000, "tokens_out": 200}
    report = score.score_run(run, META)
    assert report["verdict"] == "pass", report["blocking"]
    md = score.score_markdown(report)
    assert "ĐẠT" in md and "`coverage_core`" in md and "200" in md
    json.dumps(report)  # phải tuần tự hoá được để lưu baseline
    assert set(score.REGRESSION_LIMITS) >= {"coverage_core", "faithfulness_rate", "trap_fact_errors", "cost_usd"}


# ------------------------------------------------------------------ schema và đầu-cuối


def test_golden_schema_accepts_repo_metadata_and_rejects_junk():
    schema = json.loads((GOLDEN / "schema.json").read_text(encoding="utf-8"))
    meta = json.loads((GOLDEN / "demo_lecture" / "meta.json").read_text(encoding="utf-8"))
    jsonschema.validate(meta, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"doc_id": "x", "must_cover": [], "trap_facts": []}, schema)


def test_validate_golden_reports_coverage_targets():
    report = score.validate_golden(GOLDEN)
    assert report["ok"] is True
    assert report["counts_by_kind"] == {}  # hiện chỉ có tài liệu "smoke"
    assert report["targets"]["lecture_transcript"] == 6


def test_end_to_end_demo_run_scores_pass(tmp_path):
    artifacts = tmp_path / "demo"
    proc = subprocess.run(
        [sys.executable, "-m", "visynth.cli", "run", "--demo", "--artifacts", str(artifacts)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    run = json.loads((artifacts / "run.json").read_text(encoding="utf-8"))
    assert run["units"] and run["blocks_full"] and run["coverage"] and run["params"]["demo"] is True

    meta = json.loads((GOLDEN / "demo_lecture" / "meta.json").read_text(encoding="utf-8"))
    report = score.score_run(run, meta)
    assert report["verdict"] == "pass", report["blocking"]
    assert report["metrics"]["coverage_core"] >= 0.9
    assert report["metrics"]["trap_fact_errors"] == 0
    assert report["metrics"]["fabricated_remaining"] == 0
    assert (artifacts / "report.md").read_text(encoding="utf-8").startswith("#")
