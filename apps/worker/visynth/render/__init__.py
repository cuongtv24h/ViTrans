"""Kết xuất báo cáo/dịch thành tệp tải về: PDF, và bản **song ngữ** (SPEC §6.10, §13).

Ở đây chỉ có phần TẤT ĐỊNH (Markdown → PDF, ghép song ngữ), không phụ thuộc FastAPI, để cả API,
worker và CLI dùng chung một cách trình bày. Tệp xuất luôn kèm thông báo "nội dung do AI" (§14.4).

Markdown đầu vào là bản do pipeline sinh (`reports.markdown`) hoặc bản dịch (`build_markdown`), nên
bộ chuyển chỉ cần hiểu **tập con** đã dùng: tiêu đề `#`, danh sách `- `/`1. `, trích dẫn `> `, chữ
đậm/nghiêng, và khối mã trong dòng. Cố ý không kéo thêm thư viện Markdown vào ảnh chạy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Font Unicode để in tiếng Việt. fpdf2 chỉ có sẵn font latin-1 nên PHẢI có TTF Unicode trong ảnh.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


class PdfRenderError(RuntimeError):
    """Thiếu thư viện/font để kết xuất PDF — API đổi thành `pdf_not_available`."""


@dataclass
class _Line:
    text: str
    size: int = 11
    bold: bool = False
    indent: float = 0
    space_before: float = 1.0


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def strip_inline(text: str) -> str:
    """Bỏ đánh dấu trong dòng (đậm/nghiêng/mã/liên kết) nhưng GIỮ nguyên chữ."""
    out = _BOLD.sub(r"\1", text)
    out = _ITALIC.sub(r"\1", out)
    out = _CODE.sub(r"\1", out)
    out = _LINK.sub(r"\1", out)
    return out.replace("\\", "").strip()


def markdown_lines(markdown: str) -> list[_Line]:
    """Đổi Markdown (tập con) thành danh sách dòng có cỡ chữ/thụt lề cho bộ vẽ PDF."""
    lines: list[_Line] = []
    in_code = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            lines.append(_Line(text=line, size=9, indent=12, space_before=0.4))
            continue
        if not line.strip():
            continue
        if _HR.match(line):
            lines.append(_Line(text="—" * 24, size=9, space_before=2.0))
            continue
        heading = _HEADING.match(line)
        if heading:
            level = min(6, len(heading.group(1)))
            sizes = {1: 18, 2: 15, 3: 13, 4: 12, 5: 11.5, 6: 11}
            lines.append(_Line(text=strip_inline(heading.group(2)), size=sizes[level], bold=True, space_before=3.0))
            continue
        quote = _QUOTE.match(line)
        if quote:
            lines.append(_Line(text="│ " + strip_inline(quote.group(1)), size=10, indent=6, space_before=1.2))
            continue
        if _TABLE_SEP.match(line):
            continue
        bullet = _BULLET.match(line)
        if bullet:
            depth = len(bullet.group(1)) // 2
            lines.append(
                _Line(
                    text="• " + strip_inline(bullet.group(2).split("|")[0] if "|" in line else bullet.group(2)),
                    size=11,
                    indent=6 + depth * 10,
                )
            )
            continue
        if line.strip().startswith("|"):
            # bảng dữ kiện: giữ nội dung các ô, ngăn bằng dấu chấm giữa
            cells = [strip_inline(c) for c in line.strip().strip("|").split("|")]
            lines.append(_Line(text=" · ".join(c for c in cells if c), size=10))
            continue
        lines.append(_Line(text=strip_inline(line), size=11))
    return lines


def render_pdf(markdown: str, *, title: str = "Báo cáo", notice: str = "", footer: str = "") -> bytes:
    """Markdown → PDF bằng fpdf2. Ném `PdfRenderError` khi thiếu thư viện/font Unicode."""
    try:
        from fpdf import FPDF
    except ImportError as exc:  # pragma: no cover - phụ thuộc ảnh chạy
        raise PdfRenderError("cần fpdf2 để kết xuất PDF") from exc

    font = next((Path(p) for p in FONT_CANDIDATES if Path(p).exists()), None)
    if font is None:
        raise PdfRenderError("không tìm thấy font Unicode (cài fonts-dejavu-core)")

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_font("main", "", str(font))
    bold = next((Path(p) for p in FONT_BOLD_CANDIDATES if Path(p).exists()), None)
    pdf.add_font("main", "B", str(bold or font))
    pdf.set_title(title)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    pdf.set_font("main", size=11)

    if notice:
        pdf.set_font("main", "B", size=9)
        pdf.set_text_color(150, 60, 0)
        pdf.multi_cell(0, 5, notice)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    for line in markdown_lines(markdown):
        pdf.ln(line.space_before)
        pdf.set_x(pdf.l_margin + line.indent)
        pdf.set_font("main", "B" if line.bold else "", size=line.size)
        width = pdf.w - pdf.r_margin - pdf.x
        pdf.multi_cell(width, line.size * 0.5, line.text or " ")
    if footer:
        pdf.ln(4)
        pdf.set_font("main", size=8)
        pdf.set_text_color(110, 110, 110)
        pdf.multi_cell(0, 4, footer)
    out = pdf.output()
    return bytes(out)


# ------------------------------------------------------------------ bản song ngữ


def bilingual_markdown(
    *,
    title: str,
    source_title: str,
    pairs: list[tuple[str, str, str]],
    notes: dict[str, str] | None = None,
) -> str:
    """Ghép Markdown song ngữ: mỗi khối là (pid, văn bản nguồn, bản dịch) — §6.10.

    `pairs` giữ ĐÚNG thứ tự đọc của tài liệu; đoạn thiếu bản dịch vẫn in phần nguồn kèm cảnh báo.
    """
    notes = notes or {}
    out = [
        f"# {title}",
        "",
        f"> **Bản song ngữ** — nguồn: *{source_title}*. Cột trên là nguyên văn nguồn, "
        "cột dưới là bản dịch; mọi đoạn đều giữ mã `pid` để đối chiếu.",
        "",
    ]
    for pid, source, vietnamese in pairs:
        out += [
            f"### {pid}",
            "",
            f"> {source.strip() or '*(đoạn nguồn trống)*'}",
            "",
        ]
        if vietnamese.strip():
            out += [vietnamese.strip(), ""]
        else:
            out += ["⚠️ *Chưa có bản dịch cho đoạn này.*", ""]
        if notes.get(pid):
            out += [f"<sub>Ghi chú: {notes[pid]}</sub>", ""]
    return "\n".join(out)


def bilingual_pairs(source_paragraphs, items) -> list[tuple[str, str, str]]:
    """Ghép `doc_paragraphs` với `translation_items` theo `pid` (thiếu bản dịch ⇒ chuỗi rỗng)."""
    translated = {item.pid: item for item in items}
    pairs: list[tuple[str, str, str]] = []
    for paragraph in source_paragraphs:
        item = translated.get(paragraph.pid)
        pairs.append((paragraph.pid, paragraph.content, item.vi if item else ""))
    return pairs


def translation_notes(items) -> dict[str, str]:
    return {item.pid: item.note_vi for item in items if getattr(item, "note_vi", None)}
