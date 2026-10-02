"""Kiểm tra tất định trên báo cáo (SPEC §6.8, mã D1–D9) và tính chỉ số/hạng chất lượng.

Mọi kiểm tra ở đây chạy bằng code, không tốn token; chúng là lớp chặn thứ hai sau khi model trả lời.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from visynth.checks.glossary import GlossaryEntry, LintIssue, lint
from visynth.checks.numbers import extract_numbers, unverified_numbers
from visynth.textnorm import nfc

#: Loại issue làm khối "chưa giải quyết" nếu còn sau vòng sửa cuối (§6.8).
UNRESOLVED_TYPES = frozenset(
    {"number_mismatch", "date_mismatch", "name_mismatch", "fabricated", "overreach", "attribution_missing"}
)
#: Loại issue vẫn được coi là trung thực nếu verdict là `partially_supported` (§6.8).
TOLERATED_TYPES = frozenset({"missing_nuance", "term_inconsistency", "other"})
#: Loại issue mà `repair` phải thử sửa và bị tính là "còn lỗi" nếu vẫn còn sau vòng cuối (§6.9).
REPAIR_TYPES = UNRESOLVED_TYPES | {"term_inconsistency"}

_ID_LEAK = re.compile(r"\b(?:U-\d{4,}|P\d{6,}|S\d{2,}\.b\d{2,}|SEG-\d{3,})\b")
_ASSISTANT = re.compile(
    r"(dưới đây là|tất nhiên|trên đây là|là một (?:AI|mô hình)|tôi không thể|as an ai|"
    r"here is the|i cannot|i'm sorry)",
    re.I,
)
_HTML = re.compile(r"</?\s*(?:script|style|div|span|iframe|table|tr|td|p|br|img|a)\b[^>]*>", re.I)
_URL = re.compile(r"https?://[^\s)\]\"'>]+")


@dataclass
class BlockIssue:
    """Một vấn đề của khối. `hard=True` là lỗi cứng (phải sửa hoặc đánh cờ)."""

    type: str  # phải thuộc issueType của common.schema.json
    detail_vi: str
    hard: bool = False
    source_quote: str | None = None
    suggested_fix_vi: str | None = None


@dataclass
class BlockCheck:
    block_id: str
    section_id: str
    issues: list[BlockIssue] = field(default_factory=list)
    cites_ok: bool = True
    unverified_numbers: list[str] = field(default_factory=list)
    diacritic_ratio: float = 1.0
    length_ratio: float | None = None


def diacritic_ratio(text: str) -> float:
    """Tỷ lệ ký tự chữ có dấu tiếng Việt trên tổng ký tự chữ (§6.8 mã D9).

    Đếm theo NFD (ký tự tổ hợp) nên mọi chữ có dấu đều được tính, kể cả dạng dựng sẵn như 'ế', 'ệ';
    thêm 'đ' vì chữ này không tách được thành dấu tổ hợp.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 1.0

    def _marked(ch: str) -> bool:
        if ch in "đĐ":
            return True
        return any(unicodedata.category(c) == "Mn" for c in unicodedata.normalize("NFD", ch))

    return sum(1 for ch in letters if _marked(ch)) / len(letters)


def check_block(
    *,
    block_id: str,
    section_id: str,
    markdown_vi: str,
    cites: list[str],
    section_unit_ids: set[str],
    unit_by_id: dict[str, dict],
    paragraphs: dict[str, str],
    doc_numbers: set[str],
    glossary: list[GlossaryEntry],
    first_use_terms: set[str] | frozenset[str] = frozenset(),
    target_words: int | None = None,
    level: str = "deep_synthesis",
) -> BlockCheck:
    """Áp dụng D1–D9 cho một khối (D2 và D3 phần "có ở nơi khác" trả thông tin cho `verify`)."""
    out = BlockCheck(block_id=block_id, section_id=section_id)
    text = nfc(markdown_vi)

    # D1 — cites phải thuộc unit được giao cho mục
    unknown = [c for c in cites if c not in section_unit_ids]
    if unknown:
        out.cites_ok = False
        out.issues.append(BlockIssue("other", f"trích dẫn ngoài mục: {', '.join(sorted(unknown))}", hard=True))

    # D3 — số trong khối không có trong nguồn của các unit được trích dẫn
    cited = [c for c in cites if c in unit_by_id]
    src_texts = [paragraphs.get(ev.get("pid", ""), "") for c in cited for ev in unit_by_id[c].get("evidence", [])]
    missing = unverified_numbers(text, src_texts)
    if missing:
        # nếu số có ở nơi khác trong tài liệu thì chỉ cảnh báo (số đúng nhưng trích dẫn thiếu)
        elsewhere = {n for n in missing if n in doc_numbers}
        real = [n for n in missing if n not in elsewhere]
        for n in real:
            out.issues.append(BlockIssue("number_mismatch", f"số '{n}' không có trong đoạn nguồn được trích dẫn"))
        for n in sorted(elsewhere - set(real)):
            out.issues.append(
                BlockIssue("other", f"số '{n}' có trong tài liệu nhưng không ở đoạn được trích dẫn (cảnh báo)")
            )
    out.unverified_numbers = missing

    # D4 — lint glossary
    for issue in lint(text, glossary, frozenset(first_use_terms)):
        out.issues.append(BlockIssue("term_inconsistency", _lint_detail(issue)))

    # D5 — rò ID nội bộ
    if leak := _ID_LEAK.search(text):
        out.issues.append(BlockIssue("other", f"rò ID nội bộ: {leak.group(0)}", hard=True))

    # D6 — câu dẫn kiểu trợ lý
    if m := _ASSISTANT.search(text):
        out.issues.append(BlockIssue("other", f"câu dẫn kiểu trợ lý: '{m.group(0)}'", hard=True))

    # D7 — thẻ HTML thô, URL không có trong nguồn
    if m := _HTML.search(text):
        out.issues.append(BlockIssue("other", f"thẻ HTML thô: {m.group(0)}", hard=True))
    src_all = " ".join(paragraphs.values())
    for url in set(_URL.findall(text)):
        if url not in src_all:
            out.issues.append(BlockIssue("other", f"URL không có trong nguồn: {url}", hard=True))

    # D8 — độ dài so với target_words (cảnh báo; ngoài ±35% là mềm, chỉ đánh dấu để P7 siết)
    words = len(text.split())
    if target_words:
        ratio = words / target_words if target_words else 1.0
        out.length_ratio = round(ratio, 3)
        if not (0.65 <= ratio <= 1.35):
            out.issues.append(
                BlockIssue("other", f"độ dài {words} từ lệch {ratio:.0%} so với mục tiêu {target_words} từ")
            )

    # D9 — nghi chưa dịch (chỉ áp với báo cáo tiếng Việt, không áp mức full_translation của nguồn tiếng Việt)
    # D9 chỉ áp cho khối dài (> 200 ký tự), nhưng số đo thì luôn ghi lại (hữu ích khi soi báo cáo).
    ratio = diacritic_ratio(text)
    out.diacritic_ratio = round(ratio, 3)
    if level != "full_translation" and len(text) > 200 and ratio < 0.12:
        out.issues.append(BlockIssue("other", f"nghi chưa dịch: tỷ lệ dấu tiếng Việt {ratio:.1%} < 12%"))

    return out


def _lint_detail(issue: LintIssue) -> str:
    return f"{issue.kind}: {issue.detail}"


def faithful_block(verdict: str, issues: list[dict]) -> bool:
    """`supported`, hoặc `partially_supported` mà mọi issue thuộc nhóm dung thứ (§6.8).

    Lỗi cứng (D1/D5/D6/D7) phá vỡ tính trung thực kể cả khi P5 chấm `supported`.
    """
    if any(i.get("hard") for i in issues):
        return False
    if verdict == "supported":
        return True
    if verdict != "partially_supported":
        return False
    types = {i.get("type", "other") for i in issues}
    return not (types & UNRESOLVED_TYPES)


def block_unresolved(verdict: str, issues: list[dict]) -> bool:
    """Khối còn phải sửa/đánh cờ sau vòng sửa cuối (§6.9): verdict xấu, lỗi cứng, hoặc lỗi thuộc `REPAIR_TYPES`."""
    if verdict in ("unsupported", "contradicted"):
        return True
    return any(i.get("hard") or i.get("type") in REPAIR_TYPES for i in issues)


def grade_quality(
    *, coverage_core: float, n_blocks: int, unresolved_blocks: int, fabricated_or_contradicted: int
) -> str:
    """Hạng chất lượng theo §6.8. `unresolved_ratio` tính trên số khối."""
    if coverage_core >= 0.95 and unresolved_blocks == 0:
        return "A"
    ratio = (unresolved_blocks / n_blocks) if n_blocks else 0.0
    if coverage_core >= 0.90 and ratio <= 0.03 and fabricated_or_contradicted == 0:
        return "B"
    return "C"


def coverage_core(units: list[dict], coverage: dict[str, str]) -> float:
    """(#core 'yes' + 0.5·#core 'partial') / #core, chỉ tính core đã được trích dẫn (§6.8)."""
    core_ids = [u["id"] for u in units if u.get("importance") == "core"]
    if not core_ids:
        return 1.0
    score = 0.0
    for uid in core_ids:
        verdict = coverage.get(uid, "no")
        score += 1.0 if verdict == "yes" else 0.5 if verdict == "partial" else 0.0
    return round(score / len(core_ids), 4)


def facts_table(facts_units: list[dict]) -> str:
    """Bảng dữ kiện dựng TẤT ĐỊNH từ unit `fact_data` (§6.6 bước 5) — model không viết lại."""
    rows = ["| Dữ kiện | Giá trị | Nguồn |", "|---|---|---|"]
    for u in facts_units:
        pids = ", ".join(sorted({ev["pid"] for ev in u.get("evidence", [])})) or "—"
        value = ", ".join(n for n in u.get("numbers", [])) or "—"
        rows.append(f"| {u['title_vi']} | {value} | {pids} |")
    return "\n".join(rows)


def glossary_appendix(terms: list[tuple[str, str, int]]) -> str:
    """Phụ lục thuật ngữ: chỉ các thuật ngữ THỰC SỰ được dùng, kèm số lần (§6.11 bước 3)."""
    rows = ["| Thuật ngữ nguồn | Cách dùng trong báo cáo | Số lần |", "|---|---|---|"]
    rows += [f"| {src} | {tgt} | {n} |" for src, tgt, n in terms]
    return "\n".join(rows)


def count_term_usage(text: str, entries: list[GlossaryEntry]) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    body = nfc(text)
    for e in entries:
        n = len(re.findall(re.escape(nfc(e.target_term)), body, 0 if e.case_sensitive else re.IGNORECASE))
        if n:
            out.append((e.source_term, e.target_term, n))
    return sorted(out, key=lambda x: (-x[2], x[0].casefold()))


def _extract_all_numbers(text: str) -> set[str]:
    return set(extract_numbers(text))
