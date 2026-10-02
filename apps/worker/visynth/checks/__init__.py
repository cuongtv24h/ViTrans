"""Các lớp kiểm tra TẤT ĐỊNH của pipeline (SPEC §6.0 nguyên tắc 2, §6.5, §6.8).

Tất cả đều là port của `docs/reference/*` (trừ `report.py` là kiểm tra D1–D9 phía sản phẩm);
`tests/test_spec_conformance.py` đối chiếu kết quả với bộ đặc tả trên cùng đầu vào.
"""

from visynth.checks.glossary import GlossaryEntry, LintIssue, first_use_by_section, lint
from visynth.checks.merge import attach_evidence, merge_candidates
from visynth.checks.numbers import canon_number, extract_numbers, unverified_numbers
from visynth.checks.quotes import QuoteMatch, bad_quote_ratio, verify_evidence, verify_quote
from visynth.checks.report import (
    REPAIR_TYPES,
    UNRESOLVED_TYPES,
    BlockCheck,
    BlockIssue,
    block_unresolved,
    check_block,
    count_term_usage,
    coverage_core,
    diacritic_ratio,
    facts_table,
    faithful_block,
    glossary_appendix,
    grade_quality,
)

__all__ = [
    "REPAIR_TYPES",
    "UNRESOLVED_TYPES",
    "BlockCheck",
    "BlockIssue",
    "GlossaryEntry",
    "LintIssue",
    "QuoteMatch",
    "attach_evidence",
    "bad_quote_ratio",
    "block_unresolved",
    "canon_number",
    "check_block",
    "count_term_usage",
    "coverage_core",
    "diacritic_ratio",
    "extract_numbers",
    "facts_table",
    "faithful_block",
    "first_use_by_section",
    "glossary_appendix",
    "grade_quality",
    "lint",
    "merge_candidates",
    "unverified_numbers",
    "verify_evidence",
    "verify_quote",
]
