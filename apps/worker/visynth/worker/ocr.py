"""Giai đoạn `extract` cho PDF quét — gọi P10 theo cụm trang (SPEC §6.0, §6.1, §6.13).

Vì sao tách khỏi `extract/`: `extract/` là bóc tách tất định (không cần LLM), còn ở đây có lời gọi
mạng nên phải nằm cùng chỗ với worker để dùng chung pool/ledger/checkpoint.

Nguyên tắc:

* **Khâu phụ không làm chết khâu chính** (§6.0 nguyên tắc 5): lỗi một cụm trang thì ghi cảnh báo
  `ocr_chunk_failed:12-26` và đi tiếp với phần chữ còn lại; pool hết chỗ thì hoãn task (`DeferredError`).
* **Chạy lại không làm lại** (§6.0 nguyên tắc 3): mỗi cụm `OCR:12-26` là một `job_tasks` riêng
  (`task_key`), kết quả lưu vào `documents.ocr_pages`/`job_checkpoints` nên cụm đã xong không gọi lại.
* **P10 nhận PDF đính kèm**: cụm trang gửi kèm đúng tệp PDF gốc + `page_range`; model trả về mốc
  `<<<PAGE n>>>` cho từng trang.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from visynth.extract.model import Extraction
from visynth.extract.ocr import OcrChunk, OcrPlan, merge_ocr, plan_ocr, split_pages
from visynth.extract.pdf import read_pages
from visynth.llm.base import LLMClient, LLMError, LLMRequest, Media
from visynth.pipeline.stages import PROFILE_NEEDS, DeferredError, PipelineError
from visynth.prompts import default_prompts_dir, render_prompt

log = logging.getLogger("visynth.worker.ocr")

#: PDF tối đa nhà cung cấp nhận cho một lời gọi (Gemini: 50 MB) — §6.1.
MAX_MEDIA_BYTES = 50 * 1024 * 1024


@dataclass
class OcrOutcome:
    """Kết quả OCR một tài liệu: `Extraction` mới (nếu có thay đổi) + số đo để ghi `job_stages.metrics`."""

    extraction: Extraction | None = None
    chunks_done: int = 0
    pages_ocr: int = 0
    failed_chunks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def metrics(self) -> dict:
        return {
            "stage": "extract",
            "chunks": self.chunks_done,
            "pages_ocr": self.pages_ocr,
            "failed_chunks": len(self.failed_chunks),
            "chunks_failed": list(self.failed_chunks),
        }


def pdf_page_texts(path: str | Path) -> dict[int, str]:
    """Lấy lớp chữ có sẵn của từng trang (để giữ lại phần chữ tốt khi chỉ OCR một số trang)."""
    read = read_pages(path)
    out: dict[int, str] = {}
    for index, lines in enumerate(read.pages, start=1):
        text = "\n".join(lines).strip()
        if text:
            out[index] = text
    return out


class PdfOcr:
    """Chạy P10 cho từng cụm trang. Không giữ trạng thái trong CSDL — việc đó thuộc `JobWorker`."""

    def __init__(
        self,
        client: LLMClient,
        *,
        prompts_dir: str | None = None,
        language_hint: str = "",
        chunk_size: int | None = None,
    ) -> None:
        self.client = client
        self.prompts_dir = prompts_dir or str(default_prompts_dir())
        self.language_hint = language_hint
        self.chunk_size = chunk_size

    # ------------------------------------------------------------------ lập kế hoạch
    def plan(self, extraction: Extraction) -> OcrPlan:
        """Cụm trang cần OCR của một tài liệu (dựa vào `extraction.ocr_pages`)."""
        page_count = int(extraction.page_count or 0)
        if page_count <= 0 or not extraction.ocr_pages:
            return OcrPlan(keep_pages=list(range(1, page_count + 1)), reason="no_pages_need_ocr")
        full = sorted(extraction.ocr_pages) == list(range(1, page_count + 1))
        return plan_ocr(
            page_count,
            pages_needing_ocr=None if full else extraction.ocr_pages,
            chunk_size=self.chunk_size or 12,
        )

    # ------------------------------------------------------------------ một cụm
    def run_chunk(self, pdf_path: str | Path, chunk: OcrChunk, *, max_bytes: int = MAX_MEDIA_BYTES) -> str:
        """Gọi P10 cho một cụm trang; trả văn bản thô (có mốc `<<<PAGE n>>>`)."""
        data = Path(pdf_path).read_bytes()
        if len(data) > max_bytes:
            raise PipelineError(f"pdf_qua_lon_cho_ocr:{len(data)}")
        prompt = render_prompt(
            self.prompts_dir,
            "P10",
            {"page_range": chunk.page_range, "language_hint": self.language_hint or "(không rõ)"},
        )
        request = LLMRequest(
            prompt_id=prompt.prompt_id,
            system=prompt.system,
            user=prompt.user,
            schema=None,
            max_output_tokens=prompt.max_output_tokens,
            thinking=prompt.thinking,
            needs=dict(PROFILE_NEEDS.get(prompt.model_profile, {})),
            metadata={"stage": "extract", "page_range": chunk.page_range},
            media=(Media(mime_type="application/pdf", data=data, name=f"OCR-{chunk.page_range}.pdf"),),
        )
        try:
            response = self.client.complete(request)
        except LLMError as exc:
            if exc.outcome.kind == "deferred":
                raise DeferredError(
                    f"P10 {chunk.page_range}: {exc}", wait_s=float(exc.outcome.retry_after_s or 60.0)
                ) from exc
            raise PipelineError(f"P10 {chunk.page_range}: {exc}") from exc
        return response.text

    # ------------------------------------------------------------------ cả tài liệu
    def run(
        self,
        extraction: Extraction,
        pdf_path: str | Path,
        *,
        done_chunks: set[str] | None = None,
        on_chunk=None,
    ) -> OcrOutcome:
        """Chạy các cụm còn thiếu; trả `Extraction` đã ghép.

        `done_chunks`: `task_key` của các cụm đã xong ở lần chạy trước (chạy lại không làm lại).
        `on_chunk(chunk, text)`: hook để worker ghi checkpoint ngay sau mỗi cụm.
        """
        plan = self.plan(extraction)
        outcome = OcrOutcome()
        if not plan.chunks:
            return outcome
        done_chunks = set(done_chunks or ())
        ocr_pages: dict[int, str] = {}
        for chunk in plan.chunks:
            if chunk.task_key in done_chunks:
                continue
            try:
                text = self.run_chunk(pdf_path, chunk)
            except DeferredError:
                raise
            except PipelineError as exc:
                outcome.failed_chunks.append(chunk.task_key)
                outcome.warnings.append(f"ocr_chunk_failed:{chunk.page_range}")
                log.warning("cụm OCR %s hỏng: %s", chunk.page_range, exc)
                continue
            pages = split_pages(text)
            if not pages:
                outcome.failed_chunks.append(chunk.task_key)
                outcome.warnings.append(f"ocr_chunk_empty:{chunk.page_range}")
                log.warning("cụm OCR %s không có mốc <<<PAGE n>>>", chunk.page_range)
                continue
            ocr_pages.update(pages)
            outcome.chunks_done += 1
            if on_chunk is not None:
                on_chunk(chunk, pages)

        if not ocr_pages:
            return outcome
        text_layer = {} if plan.reason == "scanned" else pdf_page_texts(pdf_path)
        warnings = list(extraction.warnings) + outcome.warnings
        merged = merge_ocr(
            title=extraction.title,
            source_type=extraction.source_type,
            ocr_pages=ocr_pages,
            text_layer_pages=text_layer,
            page_count=int(extraction.page_count or len(ocr_pages)),
            warnings=warnings,
            pages_ocr=len(ocr_pages),
        )
        merged.needs_ocr = False
        merged.ocr_pages = []
        outcome.extraction = merged
        outcome.pages_ocr = len(ocr_pages)
        return outcome
