"""OCR trang PDF bằng P10 (SPEC §6.1, §6.0 bảng giai đoạn `extract`).

Luồng: trang bị coi là "quét" (trung vị ký tự/trang < 200) hoặc trang nhiều cột → gửi **P10** theo
**cụm 10–15 trang** (mặc định 12) kèm chính tệp PDF; P10 trả về văn bản có mốc `<<<PAGE n>>>`.

Module này chỉ chứa phần **tất định** (lập kế hoạch cụm + đọc kết quả P10 + dựng lại `Extraction`),
phần gọi LLM nằm ở `worker/ocr.py` để `extract/` không phụ thuộc client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from visynth.extract.model import Extraction, RawBlock, build_document
from visynth.extract.text import parse_blocks

#: §6.1: cụm 10–15 trang mỗi lời gọi P10 (để đầu ra không chạm trần và chạy song song được).
DEFAULT_CHUNK_SIZE = 12
#: Mốc phân trang do P10 in ra (quy tắc 6 của prompt).
PAGE_MARKER = re.compile(r"^\s*<<<PAGE\s+(\d{1,5})>>>\s*$", re.MULTILINE)
#: Số từ mỗi trang quét, dùng để ƯỚC LƯỢNG tín dụng trước khi OCR xong (sau OCR lấy số thật).
#: Một trang A4 tiếng Việt cỡ 11-12pt chứa khoảng 400 từ; đây là giá trị khởi điểm, sẽ hiệu chỉnh
#: cùng bảng giá khi chạy trên dữ liệu thật (M0-W4).
SCANNED_WORDS_PER_PAGE = 400


def estimate_scanned_words(page_count: int | None) -> int:
    """Ước lượng số từ của tài liệu quét khi chưa có kết quả OCR (để báo giá trước, không để trống)."""
    return max(1, int(page_count or 0)) * SCANNED_WORDS_PER_PAGE


@dataclass
class OcrChunk:
    """Một cụm trang cần OCR: `page_range` dùng làm `{{page_range}}` cho P10, `task_key` ghi `job_tasks`."""

    start: int
    end: int

    @property
    def page_range(self) -> str:
        return f"{self.start}-{self.end}"

    @property
    def task_key(self) -> str:
        return f"OCR:{self.page_range}"

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "task_key": self.task_key}


@dataclass
class OcrPlan:
    chunks: list[OcrChunk] = field(default_factory=list)
    #: Trang KHÔNG cần OCR nhưng vẫn nằm trong tài liệu (giữ nguyên chữ có sẵn).
    keep_pages: list[int] = field(default_factory=list)
    reason: str = ""

    @property
    def task_keys(self) -> list[str]:
        return [c.task_key for c in self.chunks]


def plan_ocr(
    page_count: int,
    *,
    pages_needing_ocr: list[int] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> OcrPlan:
    """Lập kế hoạch cụm OCR.

    `pages_needing_ocr = None` nghĩa là **toàn bộ** tài liệu bị quét; truyền danh sách trang cụ thể khi
    chỉ một số trang hỏng (ví dụ trang nhiều cột) — các trang còn lại giữ chữ có sẵn (`keep_pages`).
    """
    if page_count <= 0:
        return OcrPlan(reason="empty_document")
    wanted = sorted(set(pages_needing_ocr)) if pages_needing_ocr else list(range(1, page_count + 1))
    wanted = [p for p in wanted if 1 <= p <= page_count]
    if not wanted:
        return OcrPlan(keep_pages=list(range(1, page_count + 1)), reason="no_pages_need_ocr")
    chunks: list[OcrChunk] = []
    start = end = wanted[0]
    for page in wanted[1:]:
        if page == end + 1 and page - start + 1 <= chunk_size:
            end = page
            continue
        chunks.append(OcrChunk(start, end))
        start = end = page
    chunks.append(OcrChunk(start, end))
    keep = [p for p in range(1, page_count + 1) if p not in set(wanted)]
    return OcrPlan(chunks=chunks, keep_pages=keep, reason="scanned" if pages_needing_ocr is None else "partial")


def split_pages(text: str) -> dict[int, str]:
    """Tách đầu ra P10 thành `{số trang: văn bản}` theo mốc `<<<PAGE n>>>`."""
    out: dict[int, str] = {}
    matches = list(PAGE_MARKER.finditer(text))
    for index, match in enumerate(matches):
        page = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            out[page] = body
    return out


def pages_to_blocks(pages: dict[int, str]) -> list[RawBlock]:
    """Mỗi trang → các khối thô (dùng chung bộ tách đoạn của TXT/MD), gắn `page_start/page_end`."""
    blocks: list[RawBlock] = []
    for page in sorted(pages):
        for block in parse_blocks(pages[page]):
            blocks.append(
                RawBlock(
                    kind=block.kind,
                    content=block.content,
                    level=block.level,
                    page_start=page,
                    page_end=page,
                )
            )
    return blocks


def merge_ocr(
    *,
    title: str,
    source_type: str,
    ocr_pages: dict[int, str],
    text_layer_pages: dict[int, str] | None = None,
    page_count: int,
    warnings: list[str] | None = None,
    pages_ocr: int | None = None,
) -> Extraction:
    """Ghép trang OCR với trang có sẵn chữ (giữ nguyên thứ tự trang) thành `Extraction` hoàn chỉnh."""
    text_layer_pages = text_layer_pages or {}
    merged: dict[int, str] = {**text_layer_pages, **{p: t for p, t in ocr_pages.items() if t.strip()}}
    return build_document(
        title,
        pages_to_blocks(merged),
        source_type=source_type,
        warnings=sorted(set(warnings or [])),
        page_count=page_count,
        pages_ocr=pages_ocr if pages_ocr is not None else len(ocr_pages),
    )
