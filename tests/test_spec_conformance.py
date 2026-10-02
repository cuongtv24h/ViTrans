"""Đối chiếu mã sản phẩm với bộ đặc tả — chống lệch giữa `visynth/` và `docs/`."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from reference import estimator as spec_est

from visynth import estimate as ours

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _prices(cls):
    return [
        cls("gemini-3.8-flash", date(2026, 9, 2), 0.75, 3.75, 0.075),
        cls("gemini-3.8-flash", date(2027, 1, 1), 1.50, 7.50, 0.15),
    ]


@pytest.mark.parametrize("words", [1_000, 90_000, 300_000])
@pytest.mark.parametrize("level", ["full_translation", "detailed_synthesis", "deep_synthesis", "executive_brief"])
@pytest.mark.parametrize("on", [date(2026, 10, 2), date(2027, 3, 1)])
def test_estimate_matches_spec_reference(words: int, level: str, on: date):
    a = ours.estimate(words, level, ours.pick_price(_prices(ours.Price), "gemini-3.8-flash", on))
    b = spec_est.estimate(words, level, spec_est.pick_price(_prices(spec_est.Price), "gemini-3.8-flash", on))
    assert (a.tokens_in, a.tokens_out, a.cost_usd, a.credits, a.minutes_low, a.minutes_high) == (
        b.tokens_in,
        b.tokens_out,
        b.cost_usd,
        b.credits,
        b.minutes_low,
        b.minutes_high,
    )


@pytest.mark.parametrize("words", [1_000, 90_000])
def test_calls_and_budget_match_spec_reference(words: int):
    for level in ("full_translation", "detailed_synthesis", "deep_synthesis", "executive_brief"):
        assert ours.estimate_calls(words, level) == spec_est.estimate_calls(words, level)
        assert ours.report_budget_words(words, level) == spec_est.report_budget_words(words, level)
    assert ours.docs_per_day(237, None, 77, 500_000) == spec_est.docs_per_day(237, None, 77, 500_000)


def test_paragraph_kinds_match_contract_enum():
    from visynth.extract import PARAGRAPH_KINDS

    common = json.loads((DOCS / "schemas" / "common.schema.json").read_text(encoding="utf-8"))
    assert list(PARAGRAPH_KINDS) == common["$defs"]["paragraphKind"]["enum"]


def test_identifiers_match_ddl_patterns():
    from visynth.extract.model import PID_RE
    from visynth.segment import Segment

    ddl = (DOCS / "db" / "schema.sql").read_text(encoding="utf-8")
    assert PID_RE.pattern.strip("^$") in ddl  # doc_paragraphs.pid ~ '^P[0-9]{6,}$'
    assert "SEG-[0-9]{3,}" in ddl  # segments.segment_id ~ '^SEG-[0-9]{3,}$'
    assert Segment("SEG-001", 1, "P000001", "P000002", 10, None, []).segment_id == "SEG-001"


def test_contract_files_are_present():
    for rel in (
        "schemas/common.schema.json",
        "api/openapi.yaml",
        "db/schema.sql",
        "prompts/00_style_core_neutral.json",
    ):
        assert (DOCS / rel).exists(), rel


# ------------------------------------------------------------------ M0-W2: pipeline và các lớp kiểm tra


def test_level_policy_matches_spec_block():
    """Khối `level_policy` trong mã phải khớp TỪNG KÝ TỰ khối ```text của SPEC §7.3."""
    import re as _re

    from visynth.levelpolicy import LEVEL_POLICY

    spec = (DOCS / "tools" / "spec_src" / "30_pipeline.md").read_text(encoding="utf-8")
    block = _re.search(r"```text\n(\[detailed_synthesis\].*?)```", spec, _re.S)
    assert block, "không tìm thấy khối level_policy trong spec_src/30_pipeline.md"
    parsed = {chunk[1 : chunk.index("]")]: chunk for chunk in _re.split(r"\n\n(?=\[)", block.group(1).strip())}
    assert parsed == LEVEL_POLICY


def test_numbers_check_matches_spec_reference():
    from reference import numbers_check as spec

    from visynth.checks import numbers as ours

    for name in ("canon_number", "extract_numbers", "unverified_numbers"):
        assert callable(getattr(ours, name))
    for text in ("1.200 đơn vị", "năm 2025, tăng 3,5%", "P000123 và U-0042", "1) mục thứ nhất"):
        assert ours.extract_numbers(text) == spec.extract_numbers(text)
    for raw in ("1.200", "1,200", "1 200", "3,5%", "12/03/2026", "hai trăm"):
        assert ours.canon_number(raw) == spec.canon_number(raw)
    cases = [
        ("Báo cáo nói 1.200 đơn vị.", ["Nguồn có 1.200 đơn vị."]),
        ("Báo cáo nói 9.999 đơn vị.", ["Nguồn có 1.200 đơn vị."]),
        ("Báo cáo nói 1.200 và 45 ms.", ["Nguồn có 1.200 đơn vị."]),
        ("Số 0 không đáng kể.", ["Nguồn nói 0."]),
    ]
    for report, sources in cases:
        assert ours.unverified_numbers(report, sources) == spec.unverified_numbers(report, sources)
        assert ours.unverified_numbers(report, sources, min_value=5) == spec.unverified_numbers(
            report, sources, min_value=5
        )


def test_glossary_lint_matches_spec_reference():
    from reference import glossary_lint as spec

    from visynth.checks import glossary as ours

    def entries(mod):
        return [
            mod.GlossaryEntry(source_term="vector index", target_term="chỉ mục vector"),
            mod.GlossaryEntry(
                source_term="latency budget",
                target_term="ngân sách độ trễ",
                keep_original=True,
                forbidden_variants=("ngân sách trễ",),
            ),
            mod.GlossaryEntry(source_term="RAG", target_term="RAG", case_sensitive=True),
        ]

    texts = [
        "Chỉ mục vector giúp tra cứu; vector index không nên bỏ trần.",
        "Ngân sách độ trễ (latency budget) là tổng thời gian chờ.",
        "Ngân sách độ trễ là tổng thời gian chờ.",
        "Dùng ngân sách trễ thay vì ngân sách độ trễ.",
        "rag viết thường không tính, RAG viết hoa thì tính.",
    ]
    for text in texts:
        for first_use in ((), {"latency budget"}):
            a = ours.lint(text, entries(ours), set(first_use))
            b = spec.lint(text, entries(spec), set(first_use))
            assert [(i.kind, i.term, i.detail) for i in a] == [(i.kind, i.term, i.detail) for i in b]
    order = ["S01", "S02", "S03"]
    terms_by_section = {"S01": {"vector index"}, "S02": {"latency budget"}, "S03": {"latency budget"}}
    assert ours.first_use_by_section(order, terms_by_section, entries(ours)) == spec.first_use_by_section(
        order, terms_by_section, entries(spec)
    )


def test_quote_verify_matches_spec_reference():
    from reference import quote_verify as spec

    from visynth.checks import quotes as ours

    paragraph = (
        "Retrieval-augmented generation combines a language model with a search index. "
        "The model writes the answer, but the facts come from retrieved documents."
    )
    cases = [
        (paragraph, paragraph, False),
        ("Retrieval-augmented generation combines a language model", paragraph, False),
        ("Retrieval-augmented generation combines a language model", paragraph, True),
        ("retrieval augmented generation combines language model", paragraph, True),
        ("hoàn toàn khác nội dung nguồn", paragraph, True),
    ]
    for quote, para, fuzzy in cases:
        a = ours.verify_quote(quote, para, allow_fuzzy=fuzzy)
        b = spec.verify_quote(quote, para, allow_fuzzy=fuzzy)
        assert (a.status, round(a.score, 6)) == (b.status, round(b.score, 6)), (quote, fuzzy)
    evidence = [{"pid": "P000001", "quote": "The model writes the answer"}]
    paragraphs = {"P000001": paragraph}
    a = ours.verify_evidence(evidence, paragraphs)
    b = spec.verify_evidence(evidence, paragraphs)
    assert [(m.status, round(m.score, 6)) for m in a] == [(m.status, round(m.score, 6)) for m in b]
    lazy = [[ours.QuoteMatch("missing", 0.0)], [ours.QuoteMatch("exact", 100.0)]]
    lazy_spec = [[spec.QuoteMatch("missing", 0.0)], [spec.QuoteMatch("exact", 100.0)]]
    assert ours.bad_quote_ratio(lazy) == spec.bad_quote_ratio(lazy_spec)


def test_glossary_merge_matches_spec_reference():
    from reference import glossary_merge as spec

    from visynth.checks import merge as ours

    per_doc = {
        "doc-a": [
            {
                "source_term": "vector index",
                "target_term": "chỉ mục vector",
                "term_type": "concept",
                "keep_original": False,
                "confidence": 0.9,
                "occurrences": 3,
            },
            {
                "source_term": "RAG",
                "target_term": "RAG",
                "term_type": "acronym",
                "keep_original": True,
                "confidence": 0.8,
                "occurrences": 5,
            },
        ],
        "doc-b": [
            {
                "source_term": "vector index",
                "target_term": "chỉ mục vec-tơ",
                "term_type": "concept",
                "keep_original": True,
                "confidence": 0.7,
                "occurrences": 1,
            },
            {
                "source_term": "latency budget",
                "target_term": "ngân sách độ trễ",
                "term_type": "concept",
                "keep_original": False,
                "confidence": 0.95,
                "occurrences": 2,
            },
        ],
    }
    contexts = {("doc-a", "vector index"): ["câu ngữ cảnh A"], ("doc-b", "latency budget"): ["câu ngữ cảnh B"]}
    assert ours.merge_candidates(per_doc, contexts) == spec.merge_candidates(per_doc, contexts)
    merged = ours.merge_candidates(per_doc, contexts)
    entries = [
        {"source_term": "vector index", "target_term": "chỉ mục vector", "provenance": {"doc-a": 3}},
    ]
    assert ours.attach_evidence(entries, merged) == spec.attach_evidence(entries, merged)
