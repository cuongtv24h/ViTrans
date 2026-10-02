"""Lint thuật ngữ: bắt biến thể bị cấm, thiếu thuật ngữ gốc ở lần dùng đầu, thuật ngữ nguồn bị bỏ trần."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textnorm import nfc


@dataclass(frozen=True)
class GlossaryEntry:
    source_term: str
    target_term: str
    keep_original: bool = False
    case_sensitive: bool = False
    forbidden_variants: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LintIssue:
    kind: str  # forbidden_variant | missing_original_on_first_use | untranslated_source_term
    term: str
    detail: str


def _pat(s: str) -> str:
    return rf"(?<!\w){re.escape(nfc(s))}(?!\w)"


def lint(text_vi: str, entries: list[GlossaryEntry], first_use_terms: frozenset[str] | set[str] = frozenset()) -> list[LintIssue]:
    """Lint một đoạn/mục tiếng Việt theo glossary.

    first_use_terms: tập source_term mà MỤC NÀY là nơi dùng đầu tiên trong toàn báo cáo
    (tính tất định từ thứ tự mục trong ReportPlan) -> với keep_original và target khác source phải có dạng 'target (source)'.
    """
    t = nfc(text_vi)
    issues: list[LintIssue] = []
    for e in entries:
        flags = 0 if e.case_sensitive else re.IGNORECASE
        for fv in e.forbidden_variants:
            if re.search(_pat(fv), t, flags):
                issues.append(LintIssue("forbidden_variant", e.source_term, f"dùng '{fv}' thay vì '{e.target_term}'"))

        target_present = re.search(_pat(e.target_term), t, flags) is not None
        paren = rf"{re.escape(nfc(e.target_term))}\s*\(\s*{re.escape(nfc(e.source_term))}\s*\)"
        # nguồn và đích trùng nhau (ví dụ 'pid' -> 'pid') thì dạng 'x (x)' là vô nghĩa, không bắt buộc
        same_form = nfc(e.source_term).casefold() == nfc(e.target_term).casefold()
        if (
            e.keep_original
            and not same_form
            and e.source_term in first_use_terms
            and target_present
            and not re.search(paren, t, flags)
        ):
            issues.append(
                LintIssue("missing_original_on_first_use", e.source_term, f"lần dùng đầu phải là '{e.target_term} ({e.source_term})'")
            )

        if nfc(e.source_term).casefold() != nfc(e.target_term).casefold():
            # xoá các dạng hợp lệ 'target (source)' rồi xem còn source_term trần nào không
            stripped = re.sub(paren, " ", t, flags=flags)
            if re.search(_pat(e.source_term), stripped, flags):
                issues.append(LintIssue("untranslated_source_term", e.source_term, f"thuật ngữ nguồn '{e.source_term}' xuất hiện chưa dịch"))
    return issues


def first_use_by_section(
    section_order: list[str],
    section_texts_or_terms: dict[str, set[str]],
    entries: list[GlossaryEntry],
) -> dict[str, set[str]]:
    """Với mỗi thuật ngữ keep_original, tìm MỤC ĐẦU TIÊN (theo thứ tự kế hoạch) có dùng nó.

    section_texts_or_terms: {section_id: tập source_term xuất hiện trong các unit được giao cho mục}.
    Trả về {section_id: {source_term, ...}} để truyền vào prompt P4 (biến first_use_terms) và vào lint().
    """
    result: dict[str, set[str]] = {sid: set() for sid in section_order}
    seen: set[str] = set()
    wanted = {e.source_term for e in entries if e.keep_original}
    for sid in section_order:
        for term in sorted(section_texts_or_terms.get(sid, set()) & wanted):
            if term not in seen:
                result[sid].add(term)
                seen.add(term)
    return result
