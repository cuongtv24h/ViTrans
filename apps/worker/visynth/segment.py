"""Chia segment tất định — bản port của giả mã SPEC §6.3.

Quy tắc quan trọng:
  - cắt tại RANH GIỚI ĐOẠN, ưu tiên đoạn kết thúc bằng dấu kết câu; chỉ tách giữa câu khi
    một đoạn đơn lẻ vượt `MAX_TOKENS` (đoạn đó giữ nguyên `pid`, các mảnh đánh số `#2`, `#3`…);
  - `target_tokens ≤ 0.2 × min_ctx_in` của profile (§6.3) — kiểm tra bằng `check_target_fits`;
  - `context_before` của segment là 2 đoạn cuối của segment trước, **chỉ đọc**, không trích dẫn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from visynth.extract.model import Extraction, Paragraph

DEFAULT_TARGET_MAP = 8000
DEFAULT_TARGET_TRANSLATE = 4000
MAX_TOKENS = 12000
MIN_TOKENS = 2500
CONTEXT_PARAGRAPHS = 2
MIN_CTX_SHARE = 0.2  # §6.3: target ≤ 0.2 × needs.min_ctx_in

MODES = ("map", "translate")
STRATEGIES = ("by_headings", "by_tokens")
SENTENCE_END = re.compile(r"[.!?…:;»”\")\]]\s*$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def estimate_tokens(text: str, lang: str = "en") -> int:
    """Ước lượng thô của §6.3: chars/4 (tiếng Anh), chars/2.8 (tiếng Việt)."""
    return max(1, round(len(text) / (2.8 if lang == "vi" else 4.0)))


def check_target_fits(target_tokens: int, min_ctx_in: int) -> None:
    """Segment PHẢI vừa với deployment yếu nhất hợp lệ (§6.3)."""
    limit = int(MIN_CTX_SHARE * min_ctx_in)
    if target_tokens > limit and limit > 0:
        raise ValueError(
            f"target_tokens={target_tokens} vượt 0.2 × min_ctx_in ({limit}); "
            "giảm target hoặc nâng ngưỡng ngữ cảnh của profile"
        )


@dataclass
class Segment:
    segment_id: str
    idx: int
    first_pid: str
    last_pid: str
    token_count: int
    section_id: str | None
    paragraphs: list[Paragraph]
    context_before: list[Paragraph] = field(default_factory=list)

    def render(self) -> str:
        """Định dạng `[P000123] nội dung`, các đoạn cách nhau một dòng trống (§8.2)."""
        return "\n\n".join(render_paragraph(p) for p in self.paragraphs)

    def render_context(self) -> str:
        return "\n\n".join(render_paragraph(p) for p in self.context_before)

    def as_row(self) -> dict[str, Any]:
        """Dạng hàng để ghi `segments`."""
        return {
            "segment_id": self.segment_id,
            "idx": self.idx,
            "first_pid": self.first_pid,
            "last_pid": self.last_pid,
            "token_count": self.token_count,
            "section_id": self.section_id,
        }


def render_paragraph(p: Paragraph) -> str:
    pid = f"{p.pid}#{p.part}" if getattr(p, "part", None) else p.pid
    return f"[{pid}] {p.content}"


def _copy(p: Paragraph, *, content: str, part: int) -> Paragraph:
    return Paragraph(
        pid=p.pid,
        idx=p.idx,
        kind=p.kind,
        content=content,
        section_id=p.section_id,
        page_start=p.page_start,
        page_end=p.page_end,
        timecode_start_ms=p.timecode_start_ms,
        timecode_end_ms=p.timecode_end_ms,
        speaker=p.speaker,
        part=part,
    )


def base_units(paragraphs: list[Paragraph], strategy: str = "by_headings") -> list[list[Paragraph]]:
    """Đơn vị chia cơ sở: theo mục (heading) hoặc toàn bộ tài liệu (§6.3)."""
    if strategy == "by_tokens":
        return [list(paragraphs)] if paragraphs else []
    if strategy != "by_headings":
        raise ValueError(f"strategy không hợp lệ: {strategy}")
    units: list[list[Paragraph]] = []
    key: str | None = None
    for p in paragraphs:
        k = p.section_id or "__preamble__"
        if k != key:
            units.append([])
            key = k
        units[-1].append(p)
    return units


def split_long_paragraph(p: Paragraph, target: int, lang: str) -> list[Paragraph]:
    """Đoạn đơn lẻ > MAX_TOKENS: tách theo câu (trường hợp duy nhất được cắt giữa đoạn — §6.3)."""
    pieces: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for sent in (s.strip() for s in _SENTENCE_SPLIT.split(p.content) if s.strip()):
        t = estimate_tokens(sent, lang)
        if buf and buf_tok + t > target:
            pieces.append(" ".join(buf))
            buf, buf_tok = [], 0
        buf.append(sent)
        buf_tok += t
    if buf:
        pieces.append(" ".join(buf))
    if len(pieces) <= 1:
        return [p]  # không có dấu câu để tách → giữ nguyên (chấp nhận vượt MAX, luồng gọi ghi cảnh báo)
    return [_copy(p, content=text, part=i + 1) for i, text in enumerate(pieces)]


def _should_cut(cur: list[Paragraph], cur_tok: int, t: int, target: int, min_tokens: int) -> bool:
    return bool(cur) and cur_tok + t > target and cur_tok >= min_tokens


def split_by_paragraphs(
    unit: list[Paragraph], target: int, lang: str, *, min_tokens: int = MIN_TOKENS
) -> list[list[Paragraph]]:
    """Cắt tham lam tại ranh giới đoạn; đoạn đơn lẻ > MAX thì tách theo câu (§6.3)."""
    out: list[list[Paragraph]] = []
    cur: list[Paragraph] = []
    cur_tok = 0
    for p in unit:
        t = estimate_tokens(p.content, lang)
        if _should_cut(cur, cur_tok, t, target, min_tokens):
            out.append(cur)
            cur, cur_tok = [], 0
        if t > MAX_TOKENS:
            for piece in split_long_paragraph(p, target, lang):
                pt = estimate_tokens(piece.content, lang)
                if _should_cut(cur, cur_tok, pt, target, min_tokens):
                    out.append(cur)
                    cur, cur_tok = [], 0
                cur.append(piece)
                cur_tok += pt
            continue
        cur.append(p)
        cur_tok += t
    if cur:
        out.append(cur)
    return out


def _section_of(chunk: list[Paragraph]) -> str | None:
    ids = {p.section_id for p in chunk}
    return chunk[0].section_id if len(ids) == 1 else None


def segment_paragraphs(
    paragraphs: list[Paragraph],
    *,
    mode: str,
    target_tokens: int | None = None,
    min_tokens: int = MIN_TOKENS,
    strategy: str = "by_headings",
    window_scale: float = 1.0,
    min_ctx_in: int = 100_000,
    lang: str = "en",
    context_paragraphs: int = CONTEXT_PARAGRAPHS,
) -> list[Segment]:
    """Chia danh sách đoạn thành `Segment` theo §6.3. `mode`: 'map' | 'translate'."""
    if mode not in MODES:
        raise ValueError(f"mode không hợp lệ: {mode} (chọn {MODES})")
    target = int(
        (target_tokens or (DEFAULT_TARGET_TRANSLATE if mode == "translate" else DEFAULT_TARGET_MAP)) * window_scale
    )
    check_target_fits(target, min_ctx_in)

    chunks: list[list[Paragraph]] = []
    cur: list[Paragraph] = []
    cur_tok = 0
    for unit in base_units(paragraphs, strategy):
        unit_tok = sum(estimate_tokens(p.content, lang) for p in unit)
        if unit_tok > MAX_TOKENS:
            if cur:
                chunks.append(cur)
                cur, cur_tok = [], 0
            chunks.extend(split_by_paragraphs(unit, target, lang, min_tokens=min_tokens))
            continue
        if cur and cur_tok + unit_tok > target and cur_tok >= min_tokens:
            chunks.append(cur)
            cur, cur_tok = [], 0
        cur.extend(unit)
        cur_tok += unit_tok
    if cur:
        chunks.append(cur)

    # Gộp segment cuối quá nhỏ vào segment trước (nếu có ≥ 2 segment).
    if len(chunks) >= 2:
        last_tok = sum(estimate_tokens(p.content, lang) for p in chunks[-1])
        if last_tok < min_tokens:
            chunks[-2].extend(chunks.pop())

    segments: list[Segment] = []
    for i, chunk in enumerate(chunks, start=1):
        tok = sum(estimate_tokens(p.content, lang) for p in chunk)
        ctx = chunks[i - 2][-context_paragraphs:] if i > 1 else []
        segments.append(
            Segment(
                segment_id=f"SEG-{i:03d}",
                idx=i,
                first_pid=chunk[0].pid,
                last_pid=chunk[-1].pid,
                token_count=tok,
                section_id=_section_of(chunk),
                paragraphs=chunk,
                context_before=list(ctx),
            )
        )
    return segments


def segment_extraction(
    extraction: Extraction,
    *,
    mode: str = "map",
    **kw: Any,
) -> list[Segment]:
    """Tiện dụng: chia một `Extraction` với ngôn ngữ nguồn đã đoán."""
    return segment_paragraphs(
        extraction.paragraphs,
        mode=mode,
        lang=extraction.language_code or "en",
        **kw,
    )
