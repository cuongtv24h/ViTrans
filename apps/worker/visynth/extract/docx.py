"""Đọc DOCX — SPEC §6.1 (dòng "DOCX").

Khác biệt so với `python-docx` thuần:
  - chống zip-bomb trước khi mở (giới hạn 200 MB giải nén, tỷ lệ nén ≤ 100);
  - lấy **bản cuối** của tracked-changes: giữ nội dung trong `w:ins`, bỏ `w:del`;
  - bảng → Markdown; ảnh bị bỏ và ghi cảnh báo `images_ignored`.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

from visynth.extract.model import Extraction, RawBlock, build_document

MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100


class ExtractionError(Exception):
    """Lỗi bóc tách có mã theo OpenAPI (`unsupported_file_type`, `file_too_large`, `extraction_failed`)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def check_zip_safety(zf: zipfile.ZipFile) -> None:
    """Chặn zip-bomb: tổng dung lượng giải nén và tỷ lệ nén (SPEC §6.1)."""
    total = 0
    for info in zf.infolist():
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ExtractionError("file_too_large", "Tệp nén giải ra quá 200 MB — từ chối vì an toàn")
        if info.compress_size > 0 and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise ExtractionError("extraction_failed", f"Tỷ lệ nén bất thường ở {info.filename} — nghi zip-bomb")


def _is_deleted(el: Any, qn: Any) -> bool:
    return any(a.tag == qn("w:del") for a in el.iterancestors())


def paragraph_text(p_el: Any) -> str:
    """Văn bản hiển thị của một `w:p`: giữ `w:ins`, bỏ `w:del`, đổi tab/ngắt dòng đúng chỗ."""
    from docx.oxml.ns import qn

    out: list[str] = []
    for node in p_el.iter():
        if node.tag == qn("w:t"):
            if not _is_deleted(node, qn):
                out.append(node.text or "")
        elif node.tag == qn("w:tab"):
            out.append("\t")
        elif node.tag in (qn("w:br"), qn("w:cr")):
            out.append("\n")
    return "".join(out).strip()


def heading_level(paragraph: Any) -> int | None:
    """Cấp tiêu đề từ style ('Heading 1'…, 'Title' → 1, 'Subtitle' → 2); None nếu là thân bài."""
    name = (getattr(paragraph.style, "name", "") or "").strip()
    m = re.match(r"^Heading\s+(\d+)$", name, re.I)
    if m:
        return int(m.group(1))
    sid = getattr(paragraph.style, "style_id", "") or ""
    m = re.match(r"^Heading(\d+)$", sid, re.I)
    if m:
        return int(m.group(1))
    if name.lower() == "title":
        return 1
    if name.lower() == "subtitle":
        return 2
    return None


def is_list_item(paragraph: Any) -> bool:
    name = (getattr(paragraph.style, "name", "") or "").strip().lower()
    if name.startswith(("list bullet", "list number", "list continue")):
        return True
    pPr = paragraph._p.pPr  # noqa: N806 - thuộc tính của python-docx
    return pPr is not None and pPr.numPr is not None


def table_markdown(table: Any) -> str:
    """Bảng → Markdown; ô đa dòng gộp thành một dòng, dấu `|` được thoát."""
    rows: list[str] = []
    for row in table.rows:
        cells = [c.text.strip().replace("|", "\\|").replace("\n", " ") for c in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if rows:
        rows.insert(1, "|" + "---|" * len(table.columns))
    return "\n".join(rows)


def _blocks_from_document(document: Any) -> list[RawBlock]:
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph as DocxParagraph

    blocks: list[RawBlock] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = DocxParagraph(child, document)
            text = paragraph_text(child)
            if not text:
                continue
            level = heading_level(para)
            if level is not None:
                blocks.append(RawBlock("heading", text, level=level))
            elif is_list_item(para):
                blocks.append(RawBlock("list_item", text))
            else:
                blocks.append(RawBlock("body", text))
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            md = table_markdown(table)
            if md:
                blocks.append(RawBlock("table", md))
    return blocks


def extract_docx(
    path: str | Path,
    *,
    title: str | None = None,
    source_type: str = "upload",
) -> Extraction:
    """Bóc tách một tệp DOCX. `path` phải là tệp trên đĩa (python-docx cần seek)."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as zf:
            check_zip_safety(zf)
            if "word/document.xml" not in zf.namelist():
                raise ExtractionError("unsupported_file_type", "Tệp nén không phải DOCX (thiếu word/document.xml)")
    except zipfile.BadZipFile as exc:  # pragma: no cover - hiếm
        raise ExtractionError("extraction_failed", f"Tệp DOCX hỏng: {exc}") from exc

    try:
        import docx

        document = docx.Document(str(path))
    except ImportError as exc:  # pragma: no cover - thiếu thư viện
        raise ExtractionError("extraction_failed", "Cần cài python-docx để đọc DOCX") from exc
    except Exception as exc:
        raise ExtractionError("extraction_failed", f"Không mở được DOCX: {exc}") from exc

    warnings: list[str] = []
    if getattr(document, "inline_shapes", None):
        warnings.append("images_ignored")
    blocks = _blocks_from_document(document)
    return build_document(title or path.stem, blocks, source_type=source_type, warnings=warnings)
