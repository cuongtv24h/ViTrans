"""Bóc tách PDF có lớp chữ (SPEC §6.1 dòng "PDF có chữ").

Quy tắc lấy từ §6.1:

* lấy chữ **theo trang** bằng `pypdfium2`, giữ `page_start/page_end` cho từng đoạn;
* **bỏ header/footer lặp** — dòng xuất hiện ở > 40% số trang *cùng vùng* (đầu trang / cuối trang);
* **nối từ bị ngắt dòng** (gạch nối ở cuối dòng);
* **ghép dòng thành đoạn** theo dòng trống và thụt lề;
* phát hiện **trang quét** (trung vị ký tự/trang < 200) và **nhiều cột** → đánh dấu để P10 OCR (`extract/ocr.py`).

Không tự sửa nội dung: chỉ chuẩn hoá khoảng trắng và Unicode (NFC do `build_document` lo).
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from visynth.extract.model import Extraction, RawBlock, build_document

#: §6.1: trung vị ký tự/trang dưới mức này ⇒ coi là trang quét, phải OCR.
SCANNED_MEDIAN_CHARS = 200
#: §6.1: dòng lặp ở hơn tỷ lệ này số trang (cùng vùng) là header/footer.
RUNNING_LINE_RATIO = 0.40
#: §6.1: tối đa 1000 trang mỗi tài liệu (giới hạn của nhà cung cấp cho OCR).
MAX_PAGES = 1000

_HYPHEN_END = re.compile(r"(?<=[^\W\d_])-\s*$", re.UNICODE)
_PAGE_NUMBER_LINE = re.compile(r"^\s*(?:trang\s*)?\d{1,4}\s*(?:/\s*\d{1,4})?\s*$", re.IGNORECASE)
_MULTISPACE = re.compile(r"[ \t\u00a0\u2000-\u200a]{2,}")


class PdfUnavailable(RuntimeError):
    """Thiếu thư viện đọc PDF (pypdfium2) — cài `visynth[pdf]`."""


class PdfUnreadable(RuntimeError):
    """PDF hỏng, sai định dạng hoặc đặt mật khẩu — mã lỗi `unreadable_pdf`/`encrypted_pdf` (§6.1)."""

    def __init__(self, code: str = "unreadable_pdf") -> None:
        super().__init__(code)
        self.code = code


@dataclass
class PdfPages:
    """Kết quả đọc thô một PDF: danh sách dòng theo trang (đã bỏ ký tự điều khiển)."""

    pages: list[list[str]]
    page_chars: list[int]
    warnings: list[str]
    multi_column_pages: list[int]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def median_chars(self) -> float:
        return float(statistics.median(self.page_chars)) if self.page_chars else 0.0

    @property
    def needs_ocr(self) -> bool:
        return bool(self.page_chars) and self.median_chars < SCANNED_MEDIAN_CHARS


def _open_pdf(path: str | Path):
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - phụ thuộc môi trường
        raise PdfUnavailable("cần pypdfium2 để đọc PDF (pip install pypdfium2)") from exc
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:  # noqa: BLE001 - pdfium ném nhiều loại lỗi khác nhau
        raise PdfUnreadable("unreadable_pdf") from exc
    if getattr(pdf, "is_encrypted", False):
        pdf.close()
        raise PdfUnreadable("encrypted_pdf")
    return pdf


def _clean_lines(text: str) -> list[str]:
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _MULTISPACE.sub(" ", raw.replace("\t", " ")).rstrip()
        if line.strip():
            lines.append(line)
    return lines


def read_pages(path: str | Path, *, max_pages: int = MAX_PAGES) -> PdfPages:
    """Đọc PDF thành các dòng theo trang + cờ trang quét / nhiều cột."""
    pdf = _open_pdf(path)
    try:
        total = min(len(pdf), max_pages)
        pages: list[list[str]] = []
        chars: list[int] = []
        multi_column: list[int] = []
        for index in range(total):
            try:
                page = pdf[index]
                text = page.get_textpage().get_text_range()
            except Exception:  # noqa: BLE001 - trang hỏng: coi như trang trắng, đánh dấu để OCR
                pages.append([])
                chars.append(0)
                multi_column.append(index + 1)
                continue
            lines = _clean_lines(text)
            pages.append(lines)
            chars.append(sum(len(line) for line in lines))
            if _looks_multi_column(page, lines):
                multi_column.append(index + 1)
        warnings: list[str] = []
        if len(pdf) > max_pages:
            warnings.append("pdf_page_limit")
        return PdfPages(pages=pages, page_chars=chars, warnings=warnings, multi_column_pages=multi_column)
    finally:
        pdf.close()


def _looks_multi_column(page, lines: list[str]) -> bool:
    """Heuristic nhiều cột: các dòng có điểm bắt đầu lệch nhau thành 2 cụm rõ rệt.

    Chỉ là dấu hiệu để chuyển sang P10 (theo §6.1) — không can thiệp nội dung.
    """
    try:
        width = float(page.get_width())
        boxes = []
        for line in lines[:60]:
            x = _first_x(page, line)
            if x is not None:
                boxes.append(x / width / 0.02)  # làm tròn 2% chiều rộng trang
        if len(boxes) < 20:
            return False
        left = sum(1 for b in boxes if b <= 3)
        right = sum(1 for b in boxes if b >= 22)
        return left >= 8 and right >= 8
    except Exception:  # noqa: BLE001 - heuristic phụ, lỗi thì bỏ qua
        return False


def _first_x(page, _line: str) -> float | None:
    textpage = page.get_textpage()
    try:
        box = textpage.get_charbox(0)
    except Exception:  # noqa: BLE001
        return None
    return float(box[0]) if box else None


def drop_running_headers(pages: list[list[str]], *, ratio: float = RUNNING_LINE_RATIO) -> int:
    """Bỏ dòng lặp ở **đầu trang** hoặc **cuối trang** (header/footer) — trả số dòng đã bỏ.

    Sửa danh sách tại chỗ. Chỉ xét 3 dòng đầu và 3 dòng cuối của mỗi trang, và chỉ bỏ khi dòng đó
    (đã bỏ số trang) xuất hiện ở hơn `ratio` số trang. Số trang thuần ("12", "Trang 12") luôn bị bỏ.
    """
    if len(pages) < 3:
        return 0
    removed = 0
    for zone in ("head", "tail"):
        counts: dict[str, int] = {}
        for page_lines in pages:
            zone_lines = page_lines[:3] if zone == "head" else page_lines[-3:]
            for line in set(zone_lines):
                counts[line] = counts.get(line, 0) + 1
        threshold = max(3, int(len(pages) * ratio))
        candidates = {line for line, count in counts.items() if count > threshold}
        for page_lines in pages:
            if zone == "head":
                keep = [ln for ln in page_lines if ln not in candidates]
            else:
                keep = [ln for ln in page_lines if ln not in candidates]
            removed += len(page_lines) - len(keep)
            page_lines[:] = keep
    for page_lines in pages:
        keep = [ln for ln in page_lines if not _PAGE_NUMBER_LINE.match(ln)]
        removed += len(page_lines) - len(keep)
        page_lines[:] = keep
    return removed


def join_hyphenation(lines: list[str]) -> list[str]:
    """Nối từ bị ngắt dòng bằng gạch nối ở cuối dòng ("transla-\\ntion" → "translation")."""
    out: list[str] = []
    for line in lines:
        if out and _HYPHEN_END.search(out[-1]) and line[:1].islower():
            out[-1] = _HYPHEN_END.sub("", out[-1]) + line.lstrip()
        else:
            out.append(line)
    return out


def lines_to_blocks(lines: list[str]) -> list[str]:
    """Ghép dòng thành đoạn: dòng trống kết thúc đoạn; dòng gạch đầu dòng/đánh số đứng riêng."""
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line.strip():
            if current:
                blocks.append(" ".join(current).strip())
                current = []
            continue
        if _is_list_start(line) and current:
            blocks.append(" ".join(current).strip())
            current = [line.strip()]
            continue
        current.append(line.strip())
    if current:
        blocks.append(" ".join(current).strip())
    return [b for b in blocks if b]


_LIST_START = re.compile(r"^\s*(?:[-*•‣·]|\(?\d{1,3}[.)]|[a-z][.)])\s+")


def _is_list_start(line: str) -> bool:
    return bool(_LIST_START.match(line))


_HEADING_LIKE = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?\s+\S|CHƯƠNG|BÀI|PHẦN|CHAPTER|PART|SECTION)\b")


def _block_kind(text: str) -> str:
    """Đoán `kind` cho PDF: tiêu đề ngắn không kết thúc bằng dấu câu; còn lại là văn xuôi/danh sách."""
    if _is_list_start(text):
        return "list_item"
    stripped = text.strip()
    short_no_punct = len(stripped) <= 90 and not stripped.endswith((".", "!", "?", ":", ";", ","))
    if short_no_punct and (_HEADING_LIKE.match(stripped) or (stripped.isupper() and len(stripped) > 3)):
        return "heading"
    return "body"


def extract_pdf(
    path: str | Path,
    *,
    title: str,
    source_type: str = "upload",
    pages_ocr: int = 0,
    warnings: list[str] | None = None,
) -> Extraction:
    """Bóc tách một PDF có lớp chữ thành `Extraction` (dùng chung `build_document`)."""
    read = read_pages(path)
    warnings = list(warnings or []) + list(read.warnings)
    pages = [list(page) for page in read.pages]
    removed = drop_running_headers(pages)
    if removed:
        warnings.append("running_headers_removed")
    if read.multi_column_pages:
        warnings.append("multi_column_detected")
    if read.needs_ocr:
        warnings.append("scanned_pdf_ocr_used")

    blocks: list[RawBlock] = []
    for index, page_lines in enumerate(pages, start=1):
        lines = join_hyphenation(page_lines)
        for text in lines_to_blocks(lines):
            blocks.append(RawBlock(kind=_block_kind(text), content=text, page_start=index, page_end=index))
    # Trang cần OCR: cả tài liệu nếu bị quét, hoặc chỉ các trang nhiều cột (giữ chữ của trang còn lại).
    ocr_pages = list(range(1, read.page_count + 1)) if read.needs_ocr else list(read.multi_column_pages)
    return build_document(
        title,
        blocks,
        source_type=source_type,
        warnings=sorted(set(warnings)),
        page_count=read.page_count,
        pages_ocr=pages_ocr,
        needs_ocr=bool(ocr_pages),
        ocr_pages=ocr_pages,
    )
