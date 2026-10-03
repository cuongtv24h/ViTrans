"""Kiểu dữ liệu của kết quả bóc tách — khớp `doc_paragraphs`/`doc_sections` trong `docs/db/schema.sql`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from visynth.extract.quality import extraction_quality, guess_language

#: Khớp đúng enum `paragraphKind` của `docs/schemas/common.schema.json` (có test đối chiếu).
PARAGRAPH_KINDS: tuple[str, ...] = (
    "heading",
    "body",
    "list_item",
    "table",
    "footnote",
    "caption",
    "quote",
    "code",
    "speaker_turn",
    "other",
)

PID_RE = re.compile(r"^P[0-9]{6,}$")
SECTION_RE = re.compile(r"^S[0-9]{2,}$")


def pid_for(idx: int) -> str:
    """`pid` ổn định theo thứ tự đọc (§6.1): P000001, P000002, …"""
    return f"P{idx:06d}"


def section_id_for(idx: int) -> str:
    return f"S{idx:02d}"


@dataclass(frozen=True)
class RawBlock:
    """Một khối văn bản thô do bộ đọc (TXT/MD, DOCX…) trả về, chưa có `pid`/`section_id`."""

    kind: str
    content: str
    level: int | None = None  # cấp tiêu đề, chỉ với kind='heading'
    speaker: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    timecode_start_ms: int | None = None
    timecode_end_ms: int | None = None


@dataclass(frozen=True)
class Paragraph:
    pid: str
    idx: int
    kind: str
    content: str
    section_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    timecode_start_ms: int | None = None
    timecode_end_ms: int | None = None
    speaker: str | None = None
    part: int | None = None  # khi một đoạn quá dài bị tách theo câu ở bước chia segment (§6.3)

    def __post_init__(self) -> None:
        if self.kind not in PARAGRAPH_KINDS:
            raise ValueError(f"kind không hợp lệ: {self.kind}")
        if not PID_RE.match(self.pid):
            raise ValueError(f"pid không hợp lệ: {self.pid}")

    @property
    def char_count(self) -> int:
        return len(self.content)

    def as_row(self) -> dict[str, Any]:
        """Dạng hàng để ghi `doc_paragraphs`."""
        return {
            "pid": self.pid,
            "idx": self.idx,
            "kind": self.kind,
            "content": self.content,
            "section_id": self.section_id,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "timecode_start_ms": self.timecode_start_ms,
            "timecode_end_ms": self.timecode_end_ms,
            "speaker": self.speaker,
            "char_count": self.char_count,
        }


@dataclass(frozen=True)
class Section:
    section_id: str
    parent_section_id: str | None
    title: str
    level: int
    idx: int
    first_pid: str
    last_pid: str

    def as_row(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "parent_section_id": self.parent_section_id,
            "title": self.title,
            "level": self.level,
            "idx": self.idx,
            "first_pid": self.first_pid,
            "last_pid": self.last_pid,
        }


@dataclass
class Extraction:
    """Kết quả một lần bóc tách."""

    title: str
    source_type: str  # 'upload' | 'paste'
    paragraphs: list[Paragraph]
    sections: list[Section] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    language_code: str | None = None
    language_confidence: float = 0.0
    page_count: int | None = None
    extraction_quality: float = 1.0
    #: PDF quét (hoặc trang chữ hỏng) cần OCR bằng P10 — worker chạy giai đoạn `extract` trước `profile`.
    needs_ocr: bool = False
    #: Số trang cần OCR (rỗng = không trang nào); dùng để lập cụm 10–15 trang khi chạy P10.
    ocr_pages: list[int] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(len(p.content.split()) for p in self.paragraphs)

    @property
    def token_estimate(self) -> int:
        """Ước lượng thô của §6.3 (chars/4 tiếng Anh, chars/2.8 tiếng Việt) — chỉ để chia đoạn."""
        chars = sum(p.char_count for p in self.paragraphs)
        return max(1, round(chars / (2.8 if self.language_code == "vi" else 4.0)))

    def by_pid(self, pid: str) -> Paragraph:
        for p in self.paragraphs:
            if p.pid == pid:
                return p
        raise KeyError(pid)

    def of_section(self, section_id: str) -> list[Paragraph]:
        return [p for p in self.paragraphs if p.section_id == section_id]

    def full_text(self) -> str:
        return "\n\n".join(p.content for p in self.paragraphs)

    def summary(self) -> str:
        kinds: dict[str, int] = {}
        for p in self.paragraphs:
            kinds[p.kind] = kinds.get(p.kind, 0) + 1
        kinds_txt = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        return (
            f"{self.title}: {len(self.paragraphs)} đoạn, {len(self.sections)} mục, "
            f"{self.word_count} từ, ~{self.token_estimate} token, "
            f"ngôn ngữ {self.language_code or '?'} ({self.language_confidence:.2f}), "
            f"chất lượng {self.extraction_quality:.2f}"
            + (f", cảnh báo: {', '.join(self.warnings)}" if self.warnings else "")
            + f"\n  loại đoạn: {kinds_txt}"
        )


def _build_sections(pids_with_levels: list[tuple[str, int]]) -> tuple[list[Section], dict[str, str]]:
    """Trả về (danh sách mục, ánh xạ pid → section_id) theo quy tắc cha = heading gần nhất cấp nhỏ hơn."""
    sections: list[Section] = []
    stack: list[tuple[int, Section]] = []
    for i, (pid, level) in enumerate(pids_with_levels, start=1):
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1].section_id if stack else None
        section = Section(section_id_for(i), parent, "", level, i, pid, pid)
        sections.append(section)
        stack.append((level, section))
    return sections, {pid: s.section_id for (pid, _), s in zip(pids_with_levels, sections, strict=True)}


def build_document(
    title: str,
    blocks: list[RawBlock],
    *,
    source_type: str = "upload",
    warnings: list[str] | None = None,
    page_count: int | None = None,
    pages_ocr: int = 0,
    needs_ocr: bool = False,
    ocr_pages: list[int] | None = None,
) -> Extraction:
    """Gán `pid`, dựng mục, tính ngôn ngữ và chất lượng bóc tách cho một danh sách khối thô."""
    warnings = list(warnings or [])
    blocks = [b for b in blocks if b.content.strip()]
    paragraphs: list[Paragraph] = []
    headings: list[tuple[str, int]] = []
    for i, b in enumerate(blocks, start=1):
        pid = pid_for(i)
        paragraphs.append(
            Paragraph(
                pid=pid,
                idx=i,
                kind=b.kind,
                content=b.content,
                page_start=b.page_start,
                page_end=b.page_end,
                timecode_start_ms=b.timecode_start_ms,
                timecode_end_ms=b.timecode_end_ms,
                speaker=b.speaker,
            )
        )
        if b.kind == "heading":
            headings.append((pid, max(1, min(6, b.level or 1))))

    sections, section_of = _build_sections(headings)
    if sections:
        last_of: dict[str, str] = {}
        out: list[Paragraph] = []
        current: str | None = None
        for p in paragraphs:
            current = section_of.get(p.pid, current)
            out.append(
                Paragraph(
                    pid=p.pid,
                    idx=p.idx,
                    kind=p.kind,
                    content=p.content,
                    section_id=current,
                    page_start=p.page_start,
                    page_end=p.page_end,
                    timecode_start_ms=p.timecode_start_ms,
                    timecode_end_ms=p.timecode_end_ms,
                    speaker=p.speaker,
                )
            )
            if current:
                last_of[current] = p.pid
        paragraphs = out
        sections = [
            Section(
                s.section_id,
                s.parent_section_id,
                title_of(s.section_id, paragraphs),
                s.level,
                s.idx,
                s.first_pid,
                last_of.get(s.section_id, s.first_pid),
            )
            for s in sections
        ]

    text = "\n\n".join(p.content for p in paragraphs)
    lang, conf = guess_language(text)
    quality = extraction_quality(paragraphs, page_count=page_count, pages_ocr=pages_ocr)
    if quality < 0.6 and "low_text_quality" not in warnings:
        warnings.append("low_text_quality")
    return Extraction(
        title=title,
        source_type=source_type,
        paragraphs=paragraphs,
        sections=sections,
        warnings=warnings,
        language_code=lang,
        language_confidence=conf,
        page_count=page_count,
        extraction_quality=quality,
        needs_ocr=needs_ocr,
        ocr_pages=list(ocr_pages or []),
    )


def title_of(section_id: str, paragraphs: list[Paragraph]) -> str:
    for p in paragraphs:
        if p.section_id == section_id and p.kind == "heading":
            return p.content.strip()
    return ""
