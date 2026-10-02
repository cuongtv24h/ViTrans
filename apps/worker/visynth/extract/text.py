"""Đọc TXT / Markdown / văn bản dán — SPEC §6.1 (dòng "TXT / MD" và "Dán văn bản")."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from visynth.extract.model import Extraction, RawBlock, build_document

_HEADING_ATX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^\s{0,8}(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,8}>\s?(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

_FALLBACK_ENCODINGS = ("utf-8-sig", "utf-8", "cp1258", "cp1252", "latin-1")


def decode_bytes(raw: bytes) -> tuple[str, str | None]:
    """Giải mã byte → văn bản bằng charset-normalizer; không chắc thì thử lần lượt (trả về mã đã dùng)."""
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None and best.encoding:
            return str(best), best.encoding
    except ImportError:  # pragma: no cover - thiếu thư viện thì vẫn chạy được
        pass
    for enc in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), None


def normalize_text(text: str) -> str:
    """Chuẩn hoá chung của §6.1: NFC, xuống dòng `\\n`, bỏ ký tự điều khiển, không sửa nội dung."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    out: list[str] = []
    for ch in text:
        if ch in ("\n", "\t"):
            out.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cf"):
            continue  # ký tự điều khiển / định dạng (gồm BOM, ZWSP)
        else:
            out.append(ch)
    lines = [ln.rstrip() for ln in "".join(out).split("\n")]
    return "\n".join(lines).strip("\n")


def parse_blocks(text: str) -> list[RawBlock]:
    """Tách khối: tiêu đề Markdown → `heading`, danh sách → `list_item`, hàng rào mã → `code`, còn lại → `body`."""
    blocks: list[RawBlock] = []
    lines = text.split("\n")
    i = 0
    fence: str | None = None
    code_buf: list[str] = []
    para_buf: list[str] = []

    def flush_para() -> None:
        if para_buf:
            blocks.append(RawBlock("body", "\n".join(para_buf).strip()))
            para_buf.clear()

    while i < len(lines):
        line = lines[i]
        m_fence = _FENCE.match(line)
        if fence is None and m_fence:
            flush_para()
            fence = m_fence.group(1)
            code_buf = []
            i += 1
            continue
        if fence is not None:
            if m_fence and m_fence.group(1) == fence:
                blocks.append(RawBlock("code", "\n".join(code_buf).strip("\n")))
                fence = None
            else:
                code_buf.append(line)
            i += 1
            continue
        if not line.strip():
            flush_para()
            i += 1
            continue
        m_head = _HEADING_ATX.match(line)
        if m_head:
            flush_para()
            blocks.append(RawBlock("heading", m_head.group(2).strip(), level=len(m_head.group(1))))
            i += 1
            continue
        if _MD_TABLE_ROW.match(line) and i + 1 < len(lines) and _MD_TABLE_SEP.match(lines[i + 1]):
            flush_para()
            rows = [line.strip()]
            i += 2  # bỏ dòng phân cách
            while i < len(lines) and _MD_TABLE_ROW.match(lines[i]):
                rows.append(lines[i].strip())
                i += 1
            blocks.append(RawBlock("table", "\n".join(rows)))
            continue
        m_list = _LIST_ITEM.match(line)
        if m_list:
            flush_para()
            blocks.append(RawBlock("list_item", m_list.group(1).strip()))
            i += 1
            continue
        m_quote = _QUOTE.match(line)
        if m_quote:
            flush_para()
            blocks.append(RawBlock("quote", m_quote.group(1).strip()))
            i += 1
            continue
        para_buf.append(line)
        i += 1

    if fence is not None:  # hàng rào chưa đóng: coi phần còn lại là mã
        blocks.append(RawBlock("code", "\n".join(code_buf).strip("\n")))
    flush_para()
    return blocks


def extract_text(
    source: str | bytes,
    *,
    title: str | None = None,
    source_type: str = "paste",
    filename: str | None = None,
) -> Extraction:
    """Bóc tách TXT/MD từ chuỗi hoặc byte."""
    if isinstance(source, bytes):
        text, encoding = decode_bytes(source)
    else:
        text, encoding = source, None
    text = normalize_text(text)
    warnings: list[str] = []
    if "\ufffd" in text:
        warnings.append("replacement_chars")
    if encoding and encoding.lower().startswith("utf") is False:
        warnings.append(f"encoding_guessed:{encoding}")
    name = title or (Path(filename).stem if filename else None) or "Văn bản dán"
    blocks = parse_blocks(text)
    return build_document(name, blocks, source_type=source_type, warnings=warnings)
