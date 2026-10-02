"""Kiểm thử tất định của M0-W2/M0-W3: kế hoạch P3, D1–D9, schema, prompt và mô hình dữ liệu."""

from __future__ import annotations

import json

import pytest

from visynth.checks import (
    GlossaryEntry,
    block_unresolved,
    canon_number,
    check_block,
    count_term_usage,
    coverage_core,
    diacritic_ratio,
    extract_numbers,
    facts_table,
    faithful_block,
    first_use_by_section,
    glossary_appendix,
    grade_quality,
    lint,
    merge_candidates,
    unverified_numbers,
    verify_evidence,
    verify_quote,
)
from visynth.pipeline.models import ATTRIBUTION, IMPORTANCE, UNIT_TYPES, VERDICTS, Block, Section, Unit
from visynth.pipeline.stages import autofix_plan, check_plan
from visynth.prompts import (
    default_prompts_dir,
    default_schemas_dir,
    format_segment_text,
    format_units_compact,
    render_prompt,
)
from visynth.structured import SchemaError, SchemaStore, parse_and_validate

P000001 = "Hệ thống lưu chỉ mục vector cho mỗi đoạn tài liệu và trả lời trong 45 mili-giây."
P000002 = "Bộ đệm làm độ trễ trung vị giảm từ 900 mili-giây xuống 45 mili-giây khi câu hỏi lặp lại."


def _unit(uid: str, *, importance: str = "core", state: str = "active", topics: tuple[str, ...] = ()) -> Unit:
    return Unit(
        id=uid,
        segment_id="SEG-001",
        local_id=f"u{uid[-1]}",
        type="argument",
        importance=importance,
        title_vi="tiêu đề",
        statement_vi="câu",
        topics=list(topics),
        state=state,
    )


def _section(sid: str, unit_ids: list[str], target: int = 40, kind: str = "body") -> dict:
    return {"id": sid, "kind": kind, "unit_ids": unit_ids, "target_words": target}


def _plan(sections: list[dict], omitted: list[dict] | None = None) -> dict:
    return {"sections": sections, "merged_groups": [], "omitted": omitted or []}


# ------------------------------------------------------------------ D1 trích dẫn (§6.8)
def test_verify_quote_exact_and_wrong() -> None:
    assert verify_quote("Hệ thống lưu chỉ mục vector", P000001).ok
    assert not verify_quote("Hệ thống lưu chỉ mục ngược", P000001).ok


def test_verify_quote_normalises_whitespace_and_nfc() -> None:
    para = "Câu hỏi   trùng lặp\n được đệm từ bộ nhớ đệm."
    assert verify_quote("Câu hỏi trùng lặp được đệm", para).ok
    assert verify_quote("C\u00c2U HỎI", "CÂU HỎI").ok


def test_verify_evidence_reports_each_item_status() -> None:
    matches = verify_evidence(
        [{"pid": "P000001", "quote": "chỉ mục vector"}, {"pid": "P000002", "quote": "không có trong đoạn"}],
        {"P000001": P000001, "P000002": P000002},
    )
    assert [m.status for m in matches] == ["exact", "missing"]


# ------------------------------------------------------------------ D3 con số (§6.8)
def test_canon_number_forms() -> None:
    assert canon_number("1.234") == "1234"
    assert canon_number("1,234.5") == "1234.5"
    assert canon_number("3,50") == "3.5"  # dấu phẩy = thập phân, "3,50" -> 3.5
    assert canon_number("45") == "45"


def test_extract_numbers_skips_list_markers_and_ids() -> None:
    text = "1. Bước một\n2. Bước hai có U-0001, P000002, S03.b01 và SEG-001"
    assert extract_numbers(text) == []


def test_unverified_numbers_only_flags_absent_values() -> None:
    assert unverified_numbers("Độ trễ là 900 mili-giây.", [P000002]) == []
    assert unverified_numbers("Độ trễ là 1.200 mili-giây.", [P000002]) == ["1200"]


# ------------------------------------------------------------------ D4 thuật ngữ (§6.8)
def _entry(**kw: object) -> GlossaryEntry:
    base = {"source_term": "retrieval", "target_term": "truy hồi", "keep_original": False}
    base.update(kw)
    return GlossaryEntry(**base)  # type: ignore[arg-type]


def test_glossary_lint_flags_forbidden_variant_and_untranslated_source() -> None:
    entries = [_entry(forbidden_variants=("truy vấn",))]
    kinds = {i.kind for i in lint("Hệ thống dùng truy vấn để tìm tài liệu retrieval.", entries)}
    assert kinds == {"forbidden_variant", "untranslated_source_term"}


def test_glossary_lint_accepts_target_with_source_parenthesis_on_first_use() -> None:
    entries = [_entry(source_term="vector index", target_term="chỉ mục vector", keep_original=True)]
    first = frozenset({"vector index"})
    assert lint("Chỉ mục vector (vector index) là thành phần chính.", entries, first) == []
    assert [i.kind for i in lint("Chỉ mục vector là thành phần chính.", entries, first)] == [
        "missing_original_on_first_use"
    ]


def test_first_use_by_section_picks_earliest_section() -> None:
    entries = [_entry(source_term="RAG", target_term="RAG", keep_original=True)]
    assert first_use_by_section(["S01", "S02"], {"S01": set(), "S02": {"RAG"}}, entries) == {
        "S01": set(),
        "S02": {"RAG"},
    }


# ------------------------------------------------------------------ D5 gộp trùng (§6.8)
def test_merge_candidates_keeps_higher_confidence_and_merges_alternatives() -> None:
    def _cand(source: str, conf: float, alt: str) -> dict:
        return {
            "source_term": source,
            "target_term": "chỉ mục vector",
            "keep_original": False,
            "alternatives": [alt],
            "term_type": "concept",
            "rationale_vi": "vì",
            "confidence": conf,
            "occurrences": 2,
            "first_pid": "P000001",
        }

    merged = merge_candidates({"a.md": [_cand("vector index", 0.6, "a"), _cand("Vector Index", 0.9, "b")]})
    assert len(merged) == 1  # gộp theo source_term, không phân biệt hoa/thường
    assert merged[0]["occurrences"] == 4
    assert merged[0]["avg_confidence"] == 0.75
    assert set(merged[0]["alternatives"]) == {"a", "b"}
    assert merged[0]["conflict"] is False


# ------------------------------------------------------------------ D6–D9 trên một khối
def _block_check(markdown: str, *, cites: list[str] | None = None, glossary: list[GlossaryEntry] | None = None):
    units = {
        "U-0001": {
            "id": "U-0001",
            "importance": "core",
            "evidence": [{"pid": "P000001", "quote": "chỉ mục vector"}],
            "statement_vi": "Hệ thống lưu chỉ mục vector cho mỗi đoạn tài liệu.",
            "title_vi": "Chỉ mục vector",
        }
    }
    return check_block(
        block_id="S01.b01",
        section_id="S01",
        markdown_vi=markdown,
        cites=["U-0001"] if cites is None else cites,
        section_unit_ids={"U-0001"},
        unit_by_id=units,
        paragraphs={"P000001": P000001},
        doc_numbers={"45"},
        glossary=glossary or [],
        target_words=20,
    )


def test_check_block_clean_text_has_no_issues() -> None:
    # ~25 từ so với mục tiêu 20: nằm trong dải ±35% của D8 nên không bị đánh cờ độ dài
    got = _block_check(
        "Hệ thống lưu chỉ mục vector cho mỗi đoạn tài liệu và dùng nó để truy hồi ngữ cảnh khi người dùng đặt câu hỏi."
    )
    assert got.issues == []
    assert got.cites_ok and got.unverified_numbers == []


def test_check_block_catches_each_code() -> None:
    assert _block_check("Hệ thống lưu chỉ mục vector cho mỗi đoạn tài liệu.", cites=["U-9999"]).cites_ok is False
    assert _block_check("Hệ thống xử lý 999 yêu cầu mỗi giây.").unverified_numbers == ["999"]
    assert _block_check("Hệ thống lưu chỉ mục vector <script>alert(1)</script>.").issues
    glossary = [_entry(source_term="retrieval", target_term="truy hồi", forbidden_variants=("truy vấn",))]
    issues = _block_check("Hệ thống dùng truy vấn để tìm tài liệu retrieval.", glossary=glossary).issues
    assert "term_inconsistency" in {i.type for i in issues}
    assert _block_check("Lorem ipsum dolor sit amet consectetur.").diacritic_ratio < 0.12  # số đo luôn được ghi
    long_latin = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt. " * 3
    kinds = {i.type for i in _block_check(long_latin).issues}
    assert "other" in kinds  # D9: khối dài không có dấu tiếng Việt bị nghi chưa dịch


def test_diacritic_ratio_marks_vietnamese_and_ignores_short_text() -> None:
    assert diacritic_ratio("Hệ thống lưu chỉ mục vector cho mỗi đoạn tài liệu.") > 0.2
    assert diacritic_ratio("abc") == 0.0


def test_faithful_and_unresolved_semantics() -> None:
    assert faithful_block("supported", [])
    assert not faithful_block("partially_supported", [{"type": "number_mismatch", "hard": True}])
    assert block_unresolved("unsupported", [])
    assert block_unresolved("supported", [{"type": "number_mismatch", "hard": True}])
    assert block_unresolved("supported", [{"type": "term_inconsistency", "hard": False}])  # thuộc REPAIR_TYPES
    assert not block_unresolved("supported", [{"type": "other", "hard": False}])


# ------------------------------------------------------------------ độ phủ và xếp hạng (§6.8)
def test_coverage_and_grades() -> None:
    units = [{"id": "U-0001", "importance": "core"}, {"id": "U-0002", "importance": "core"}]
    assert coverage_core(units, {"U-0001": "yes", "U-0002": "partial"}) == 0.75
    assert coverage_core(units, {}) == 0.0
    assert coverage_core([{"id": "U-0003", "importance": "minor"}], {}) == 1.0
    assert grade_quality(coverage_core=1.0, n_blocks=10, unresolved_blocks=0, fabricated_or_contradicted=0) == "A"
    assert grade_quality(coverage_core=1.0, n_blocks=10, unresolved_blocks=1, fabricated_or_contradicted=0) == "C"
    assert grade_quality(coverage_core=0.5, n_blocks=10, unresolved_blocks=0, fabricated_or_contradicted=0) == "C"
    assert grade_quality(coverage_core=0.95, n_blocks=10, unresolved_blocks=0, fabricated_or_contradicted=1) == "A"


def test_report_helpers() -> None:
    facts = [
        {
            "title_vi": "Độ trễ",
            "numbers": ["45", "900"],
            "evidence": [{"pid": "P000001", "quote": "x"}],
        }
    ]
    table = facts_table(facts)
    assert table.startswith("| Dữ kiện | Giá trị | Nguồn |")
    assert "| Độ trễ | 45, 900 | P000001 |" in table
    assert glossary_appendix([("retrieval", "truy hồi", 3)]).splitlines()[2].endswith("| 3 |")
    usage = count_term_usage("Truy hồi và truy hồi, thêm retrieval.", [_entry()])
    assert usage == [("retrieval", "truy hồi", 2)]


# ------------------------------------------------------------------ kế hoạch P3 (§6.6 bước 3)
def test_check_plan_accepts_valid_plan() -> None:
    units = [_unit("U-0001"), _unit("U-0002")]
    plan = _plan(
        [
            _section("S01", ["U-0001"], 40, kind="summary"),
            _section("S02", ["U-0002"], 40),
            _section("S03", [], 40),
            _section("S04", [], 40),
        ]
    )
    assert check_plan(plan, units, budget=160) == []


def test_check_plan_rejects_broken_plans() -> None:
    units = [_unit("U-0001"), _unit("U-0002"), _unit("U-0003")]
    missing_core = _plan([_section("S01", ["U-0001"], 40, kind="summary")])
    assert any("U-0002" in p for p in check_plan(missing_core, units, budget=40))
    bad_kind = _plan([_section("S01", ["U-0001"], 40), *_plan([_section("S02", [], 40)])["sections"]])
    assert any("summary" in p for p in check_plan(bad_kind, units, budget=80))
    few_sections = _plan([_section("S01", ["U-0001"], 40, kind="summary")])
    assert any("4-20" in p for p in check_plan(few_sections, units, budget=40))
    bad_budget = _plan(
        [
            _section("S01", ["U-0001"], 40, kind="summary"),
            _section("S02", ["U-0002"], 40),
            _section("S03", ["U-0003"], 40),
            _section("S04", [], 400),
        ]
    )
    assert any("±15%" in p for p in check_plan(bad_budget, units, budget=160))
    unknown_omitted = _plan(
        [_section("S01", ["U-0001"], 40, kind="summary")], [{"unit_id": "U-9999", "reason": "redundant"}]
    )
    assert any("U-9999" in p for p in check_plan(unknown_omitted, units, budget=40))


def test_autofix_plan_assigns_missing_core_and_scales_budget() -> None:
    units = [_unit("U-0001"), _unit("U-0002", topics=("độ trễ",)), _unit("U-0003")]
    plan = _plan(
        [
            _section("S01", ["U-0001"], 40, kind="summary"),
            _section("S02", [], 40),
            _section("S03", [], 40),
            _section("S04", [], 40),
        ]
    )
    fixed = autofix_plan(plan, units, budget=200)
    assert check_plan(fixed, units, budget=200) == []
    assert {"U-0002", "U-0003"} <= {u for s in fixed["sections"] for u in s["unit_ids"]}


def test_autofix_plan_keeps_merged_keep_unit() -> None:
    units = [_unit("U-0001"), _unit("U-0002"), _unit("U-0003")]
    plan = _plan(
        [
            _section("S01", ["U-0001"], 40, kind="summary"),
            _section("S02", ["U-0002"], 40),
            _section("S03", [], 40),
            _section("S04", [], 40),
        ]
    )
    plan["merged_groups"] = [{"keep_unit_id": "U-0003", "merged_unit_ids": ["U-0002"], "reason_vi": "trùng ý"}]
    fixed = autofix_plan(plan, units, budget=200)
    assert "U-0003" in {u for s in fixed["sections"] for u in s["unit_ids"]}


# ------------------------------------------------------------------ tiện ích §8.2
def test_format_segment_text_and_units_compact() -> None:
    assert format_segment_text([{"pid": "P000001", "text": "a"}]) == "[P000001] a"
    line = format_units_compact(
        [
            {
                "id": "U-0001",
                "importance": "core",
                "type": "argument",
                "title_vi": "t",
                "statement_vi": "s",
                "topics": ["a", "b"],
            }
        ]
    )
    assert line == "U-0001 | core | argument | t | s | a, b"


def test_render_prompt_is_single_pass_against_injection() -> None:
    """Giá trị biến chứa `{{...}}` không được render lần hai (§8.2)."""
    prompt = render_prompt(
        default_prompts_dir(),
        "P0",
        {
            "filename": "{{stats_json}}",
            "stats_json": {"word_count": 1},
            "headings_outline": "-",
            "sample_text": "x",
        },
    )
    assert "{{stats_json}}" in prompt.user
    assert prompt.system and prompt.meta["id"] == "P0_doc_profiler"
    with pytest.raises(KeyError):
        render_prompt(default_prompts_dir(), "P0", {"filename": "x"})


def test_model_vocabs_match_common_schema() -> None:
    common = json.loads((default_schemas_dir() / "common.schema.json").read_text(encoding="utf-8"))
    defs = common["$defs"]
    assert set(IMPORTANCE) == set(defs["importance"]["enum"])
    assert set(UNIT_TYPES) == set(defs["unitType"]["enum"])
    assert set(VERDICTS) == set(defs["verdict"]["enum"])
    # `attribution` của unit nằm trong $defs.unit của segment_analysis, không có trong common (§6.12)
    seg = json.loads((default_schemas_dir() / "segment_analysis.schema.json").read_text(encoding="utf-8"))
    assert set(ATTRIBUTION) == set(seg["$defs"]["unit"]["properties"]["attribution"]["enum"])


def test_unit_and_block_serialisation_round_trip() -> None:
    unit = _unit("U-0001")
    assert Unit(**unit.as_dict()) == unit
    block = Block(block_id="S01.b01", section_id="S01", type="paragraph", markdown_vi="x", cites=["U-0001"])
    assert Block(**block.as_dict()) == block
    assert "block_id" not in block.as_prompt_json() or block.as_prompt_json()["block_id"] == "S01.b01"
    section = Section(id="S01", kind="summary", title_vi="t", purpose_vi="p", unit_ids=["U-0001"], target_words=40)
    assert Section(**section.as_dict()) == section


# ------------------------------------------------------------------ schema (§6.12)
def test_schema_store_loads_and_validates_docs_schemas() -> None:
    store = SchemaStore(default_schemas_dir())
    assert "section_output.schema.json" in store.names()
    schema = store.load("coverage.schema.json")
    ok = {"results": [{"unit_id": "U-0001", "covered": "yes", "block_id": "S01.b01", "note_vi": "x"}]}
    assert store.validate(ok, schema) == ok
    bad = {"results": [{"unit_id": "U-0001", "covered": "maybe", "block_id": "S01.b01", "note_vi": "x"}]}
    with pytest.raises(SchemaError):
        store.validate(bad, schema)


def test_parse_and_validate_handles_messy_replies() -> None:
    store = SchemaStore(default_schemas_dir())
    schema = store.load("coverage.schema.json")
    payload = {"results": [{"unit_id": "U-0001", "covered": "yes", "block_id": "S01.b01", "note_vi": "x"}]}
    messy = "Đây là kết quả:\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\nHết."
    assert parse_and_validate(messy, schema, store=store) == payload
    with pytest.raises(SchemaError):
        parse_and_validate("không có JSON nào ở đây", schema, store=store)
