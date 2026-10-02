"""Chạy trọn pipeline P0–P8 trên một tài liệu (M0: tuần tự, một tiến trình).

`run_document` nhận `Extraction` đã có; `run_job` nhận đường dẫn tệp và tự trích xuất trước.
Cả hai đều KHÔNG gọi mạng — `client` do tầng trên cấp: `FakeLLMClient` khi demo/test, `PooledLLMClient` khi chạy thật
(`visynth.pool.client`, bật ở M0-W3). Nhờ vậy cùng một đường chạy P0→P8 phục vụ cả kiểm thử lẫn chạy thật.
"""

from __future__ import annotations

import os
from pathlib import Path

from visynth.extract import Extraction, extract
from visynth.llm.base import LLMClient
from visynth.pipeline.assemble import finalize
from visynth.pipeline.models import JobOptions, JobResult
from visynth.pipeline.stages import Pipeline

STAGE_SEQUENCE = ("profile", "glossary", "map", "consolidate", "write", "verify", "repair")

#: Mức dịch đầy đủ đi đường thẳng: P0 → glossary → P9 (`translate`) → ghép bản dịch (§6.10, §6.11).
#: Điểm chung với đường tổng hợp: P0 hồ sơ tài liệu, cổng glossary chờ người duyệt, rồi `assemble`.
TRANSLATE_SEQUENCE = ("profile", "glossary", "translate")


def run_document(
    extraction: Extraction,
    client: LLMClient,
    options: JobOptions | None = None,
    *,
    prompts_dir: str | os.PathLike[str] | None = None,
    schemas_dir: str | os.PathLike[str] | None = None,
    seed: int = 7,
) -> JobResult:
    """Chạy P0→P8 (hoặc P0→P9 cho `full_translation`) rồi dựng Markdown."""
    pipeline = Pipeline(extraction, client, options, prompts_dir=prompts_dir, schemas_dir=schemas_dir, seed=seed)
    pipeline.result.emit(
        "job_started",
        title=extraction.title,
        level=pipeline.options.level,
        source_type=extraction.source_type,
        paragraphs=len(extraction.paragraphs),
        words=extraction.word_count,
    )
    sequence = TRANSLATE_SEQUENCE if pipeline.options.level == "full_translation" else STAGE_SEQUENCE
    for stage in sequence:
        getattr(pipeline, f"stage_{stage}")()
    result = finalize(pipeline)
    if pipeline.options.level == "full_translation":
        build_translation_markdown(pipeline, result, extraction)
    return result


def build_translation_markdown(pipeline, result, extraction) -> str:
    """Ghép Markdown bản dịch đầy đủ (song ngữ nếu job bật) và ghi số đo vào `result.stats`."""
    from visynth.pipeline.translate import build_markdown

    # Xuất SONG NGỮ (`?bilingual=true`) là việc của API ở M2; ở đây mặc định là bản dịch thuần.
    result.markdown = build_markdown(
        extraction, pipeline.segments, {item.pid: item for item in result.translation_items}
    )
    result.stats["report_words"] = len(result.markdown.split())
    result.stats["translation_items"] = len(result.translation_items)
    result.stats["translation_flagged"] = sum(1 for item in result.translation_items if item.flagged)
    return result.markdown


def run_job(
    source: Extraction | str | os.PathLike[str],
    client: LLMClient,
    options: JobOptions | None = None,
    **kwargs,
) -> JobResult:
    """Như `run_document`, nhưng nhận đường dẫn tệp (TXT/MD/DOCX) và tự bóc tách."""
    extraction = source if isinstance(source, Extraction) else extract(Path(source))
    return run_document(extraction, client, options, **kwargs)
