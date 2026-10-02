"""Mức `full_translation` — giai đoạn `translate` (P9) và bản Markdown của nó (SPEC §6.10).

Chỉ dùng LLM dịch trực tiếp (P9); không có khâu "bản dịch thô" của model dịch máy (§18 đã bỏ).

Hai bất biến quan trọng, và cả hai đều được KIỂM TRA BẰNG CODE, không tin vào lời model:

1. **Căn 1:1 theo pid** — mỗi pid đầu vào xuất hiện **đúng một lần** ở đầu ra. Thiếu/trùng/lạ ⇒ chạy lại
   segment MỘT lần kèm danh sách lỗi; vẫn sai thì đánh `flagged` từng đoạn (không im lặng bỏ qua).
2. **Không thêm, không bớt** — tỷ lệ độ dài `len(vi)/len(src)` ∈ [0.6, 2.2] với đoạn nguồn ≥ 40 ký tự,
   số liệu trong bản dịch phải có trong nguồn, thuật ngữ phải khớp glossary đã duyệt, và (khi nguồn
   KHÔNG phải tiếng Việt) đoạn dài mà không có dấu tiếng Việt bị coi là nghi chưa dịch.

Ngưỡng ở đây khớp `docs/tools/spec_src/30_pipeline.md` §6.10 và bộ tham chiếu; `tests/test_translate.py`
đối chiếu hai bản trên cùng dữ liệu.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from visynth.checks.glossary import GlossaryEntry, lint
from visynth.checks.numbers import unverified_numbers
from visynth.checks.report import diacritic_ratio
from visynth.segment import Segment

#: Ngưỡng tỷ lệ độ dài (SPEC §6.10 bước 2).
RATIO_MIN = 0.6
RATIO_MAX = 2.2
#: Chỉ kiểm tỷ lệ độ dài với đoạn nguồn đủ dài — đoạn ngắn ("Vâng.", "Hình 3") dao động rất rộng.
RATIO_MIN_CHARS = 40
#: Đoạn dài hơn mức này mà gần như không có dấu tiếng Việt thì nghi chưa dịch.
UNTRANSLATED_MIN_CHARS = 80
UNTRANSLATED_DIACRITIC = 0.02
#: Hạng C khi quá tỷ lệ đoạn này bị đánh cờ sau khi chạy lại (§6.10).
FLAGGED_GRADE_C_RATIO = 0.15


@dataclass
class Item:
    """Một dòng của `translation_items`."""

    pid: str
    vi: str
    note_vi: str | None = None
    flagged: bool = False
    issues: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return {"pid": self.pid, "vi": self.vi, "note_vi": self.note_vi, "flagged": self.flagged}


def input_pids(seg: Segment) -> list[str]:
    """`pid` đầu vào đúng như prompt nhìn thấy (đoạn bị tách có hậu tố `#n`)."""
    out: list[str] = []
    for p in seg.paragraphs:
        part = getattr(p, "part", None)
        out.append(f"{p.pid}#{part}" if part else p.pid)
    return out


def source_text_of(seg: Segment, pid: str) -> str:
    base, _, part = pid.partition("#")
    for p in seg.paragraphs:
        if p.pid == base and ((getattr(p, "part", None) is None and not part) or str(getattr(p, "part", None)) == part):
            return p.content
    return ""


def check_chunk(
    seg: Segment,
    data: dict,
    entries: list[GlossaryEntry] | None = None,
    *,
    first_use_terms: set[str] | frozenset[str] = frozenset(),
    source_lang: str = "en",
) -> tuple[list[Item], list[str]]:
    """Kiểm tra tất định đầu ra P9 của MỘT segment. Trả `(items, problems)`.

    `problems` là lý do để chạy lại segment: căn pid sai, rỗng, tỷ lệ độ dài lệch, số lạ, thuật ngữ sai.
    """
    entries = entries or []
    expected = input_pids(seg)
    raw_items = list(data.get("items") or [])
    notes = {str(n.get("pid")): str(n.get("note_vi") or "") for n in (data.get("notes") or [])}

    problems: list[str] = []
    got: dict[str, str] = {}
    for item in raw_items:
        pid = str(item.get("pid") or "")
        if pid in got:
            problems.append(f"pid trùng: {pid}")
            continue
        got[pid] = str(item.get("vi") or "")

    missing = [pid for pid in expected if pid not in got]
    extra = [pid for pid in got if pid not in expected]
    for pid in missing[:10]:
        problems.append(f"thiếu pid: {pid}")
    for pid in extra[:10]:
        problems.append(f"pid lạ: {pid}")

    items: list[Item] = []
    for pid in expected:
        vi = got.get(pid, "")
        src = source_text_of(seg, pid)
        issues: list[str] = []
        if not vi.strip():
            issues.append("empty")
        if len(src) >= RATIO_MIN_CHARS and vi:
            ratio = len(vi) / max(1, len(src))
            if ratio < RATIO_MIN:
                issues.append(f"too_short:{ratio:.2f}")
            elif ratio > RATIO_MAX:
                issues.append(f"too_long:{ratio:.2f}")
        if vi:
            unknown = unverified_numbers(vi, [src])
            if unknown:
                issues.append("numbers:" + ",".join(unknown[:5]))
            # Chiều ngược lại: số CÓ trong nguồn mà mất ở bản dịch (§6.10 bước 2).
            lost = unverified_numbers(src, [vi])
            if lost:
                issues.append("missing_numbers:" + ",".join(lost[:5]))
            if (
                source_lang
                and source_lang != "vi"
                and len(vi) >= UNTRANSLATED_MIN_CHARS
                and diacritic_ratio(vi) < UNTRANSLATED_DIACRITIC
            ):
                issues.append("untranslated")
            if entries:
                for issue in lint(vi, entries, set(first_use_terms)):
                    issues.append(f"{issue.kind}:{issue.term}")
        items.append(Item(pid=pid, vi=vi, note_vi=(notes.get(pid) or None), flagged=bool(issues), issues=issues))
        if issues:
            problems.append(f"{pid}: {', '.join(issues)}")
    return items, problems


def merge_items(previous: list[Item], fresh: list[Item]) -> list[Item]:
    """Kết quả lần chạy lại: đoạn đã sạch thì giữ, đoạn còn lỗi thì đánh cờ nhưng KHÔNG mất bản dịch."""
    before = {item.pid: item for item in previous}
    out: list[Item] = []
    for item in fresh:
        old = before.get(item.pid)
        if item.issues and old and old.vi.strip():
            item = Item(
                pid=item.pid,
                vi=old.vi,
                note_vi=item.note_vi or old.note_vi,
                flagged=True,
                issues=old.issues or item.issues,
            )
        out.append(item)
    return out


def build_markdown(
    extraction, segments: list[Segment], items_by_pid: dict[str, Item], *, bilingual: bool = False
) -> str:
    """Ghép Markdown bản dịch theo cấu trúc `doc_sections`; chế độ song ngữ chèn đoạn nguồn (§6.10)."""
    title = extraction.title
    lines: list[str] = [
        f"# {title}",
        "",
        "> **Bản dịch do AI thực hiện.** Đã kiểm tra căn 1:1 theo mã đoạn, tỷ lệ độ dài, số liệu và thuật ngữ.",
        "",
        f"**Nguồn:** {extraction.title} · **Mức:** full_translation · **Đoạn:** {len(items_by_pid)}",
        "",
    ]
    flagged = 0
    section_titles: dict[str, str] = {}
    for paragraph in extraction.paragraphs:
        if paragraph.section_id:
            section_titles.setdefault(paragraph.section_id, paragraph.content if paragraph.kind == "heading" else "")
    for seg in segments:
        pids = input_pids(seg)
        section_id = seg.section_id or "__preamble__"
        heading = section_titles.get(section_id) if section_id in section_titles else None
        if heading and (not lines or lines[-1] != f"## {heading}"):
            lines += [f"## {heading}", ""]
        for pid in pids:
            item = items_by_pid.get(pid)
            src = source_text_of(seg, pid)
            base_pid = pid.split("#", 1)[0]
            if item is None:
                continue
            if item.flagged:
                flagged += 1
                lines.append(f"> ⚠️ *Đoạn {base_pid} còn điểm chưa kiểm chứng được với nguồn.*")
            if bilingual and src:
                lines += [f"<sub>Nguồn · **{base_pid}**</sub>", "", f"> {src.strip()}", ""]
            lines += [item.vi.strip(), ""]
            if item.note_vi:
                lines += [f"<sub>Ghi chú · **{base_pid}**: {item.note_vi}</sub>", ""]
            lines += [f"<sub>Nguồn: **{base_pid}**</sub>", ""]
    lines += ["## Ghi chú kiểm tra", ""]
    lines.append(
        f"- Số đoạn: {len(items_by_pid)}; số đoạn bị đánh cờ: {flagged}"
        + (
            " (tỷ lệ ≥ 15% ⇒ hạng C, hoàn 50% tín dụng)"
            if items_by_pid and flagged / len(items_by_pid) >= FLAGGED_GRADE_C_RATIO
            else ""
        )
    )
    lines.append("- Mọi đoạn đều giữ đúng mã `pid` của nguồn để đối chiếu ngược.")
    lines.append("")
    return "\n".join(lines)
