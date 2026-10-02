"""Bóc tách tài liệu (SPEC §6.1). M0-W1 hỗ trợ TXT/MD và DOCX; PDF/EPUB/SRT ở W2-M1."""

from __future__ import annotations

import zipfile
from pathlib import Path

from visynth.extract.docx import ExtractionError, check_zip_safety, extract_docx
from visynth.extract.model import (
    PARAGRAPH_KINDS,
    Extraction,
    Paragraph,
    RawBlock,
    Section,
    build_document,
    pid_for,
)
from visynth.extract.text import decode_bytes, extract_text, normalize_text, parse_blocks

MAX_FILE_BYTES = 50 * 1024 * 1024  # §6.1: ≤ 50 MB

__all__ = [
    "MAX_FILE_BYTES",
    "PARAGRAPH_KINDS",
    "Extraction",
    "ExtractionError",
    "Paragraph",
    "RawBlock",
    "Section",
    "build_document",
    "check_zip_safety",
    "decode_bytes",
    "extract",
    "extract_docx",
    "extract_text",
    "normalize_text",
    "parse_blocks",
    "pid_for",
    "sniff_kind",
]


def sniff_kind(head: bytes, name: str | None = None) -> str:
    """Nhận loại tệp bằng magic bytes (KHÔNG tin đuôi tệp — §6.1). Trả 'pdf' | 'zip' | 'text'."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    return "text"


def _zip_kind(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        check_zip_safety(zf)
        names = set(zf.namelist())
    if "word/document.xml" in names:
        return "docx"
    if "META-INF/container.xml" in names or "mimetype" in names:
        return "epub"
    return "unknown_zip"


def extract(
    source: str | Path | bytes,
    *,
    kind: str | None = None,
    title: str | None = None,
    source_type: str | None = None,
    filename: str | None = None,
    max_bytes: int = MAX_FILE_BYTES,
) -> Extraction:
    """Bóc tách một tệp hoặc văn bản.

    `source` là đường dẫn (str/Path) hoặc bytes. Với bytes, truyền `filename`/`title` để đặt tên.
    Ném `ExtractionError` với `code` theo OpenAPI khi không hỗ trợ hoặc tệp quá lớn.
    """
    if isinstance(source, (str, Path)):
        path = Path(source)
        raw = path.read_bytes()
        filename = filename or path.name
        base_title = title or path.stem
        st = source_type or "upload"
    else:
        raw = source
        base_title = title or (Path(filename).stem if filename else "Văn bản dán")
        st = source_type or "paste"

    if len(raw) > max_bytes:
        raise ExtractionError("file_too_large", f"Tệp lớn hơn {max_bytes // (1024 * 1024)} MB")

    resolved = kind or sniff_kind(raw[:8], filename)
    if resolved == "pdf":
        raise ExtractionError(
            "unsupported_file_type",
            "PDF chưa hỗ trợ ở M0-W1 (pypdfium2 + OCR theo kế hoạch W2) — hãy dùng TXT/DOCX/MD",
        )
    if resolved == "zip":
        if isinstance(source, bytes):
            raise ExtractionError("unsupported_file_type", "DOCX cần đường dẫn tệp (python-docx cần seek)")
        zip_kind = _zip_kind(Path(source))
        if zip_kind == "docx":
            return extract_docx(Path(source), title=base_title, source_type=st)
        raise ExtractionError("unsupported_file_type", f"Tệp nén loại '{zip_kind}' chưa hỗ trợ ở M0-W1")
    if resolved == "text":
        return extract_text(raw, title=base_title, source_type=st, filename=filename)
    raise ExtractionError("unsupported_file_type", f"Loại tệp '{resolved}' chưa hỗ trợ")
