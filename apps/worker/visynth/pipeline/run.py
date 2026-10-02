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


def run_document(
    extraction: Extraction,
    client: LLMClient,
    options: JobOptions | None = None,
    *,
    prompts_dir: str | os.PathLike[str] | None = None,
    schemas_dir: str | os.PathLike[str] | None = None,
    seed: int = 7,
) -> JobResult:
    """Chạy P0→P8 rồi dựng Markdown (P8 = scope note + assemble)."""
    pipeline = Pipeline(extraction, client, options, prompts_dir=prompts_dir, schemas_dir=schemas_dir, seed=seed)
    pipeline.result.emit(
        "job_started",
        title=extraction.title,
        level=pipeline.options.level,
        source_type=extraction.source_type,
        paragraphs=len(extraction.paragraphs),
        words=extraction.word_count,
    )
    for stage in STAGE_SEQUENCE:
        getattr(pipeline, f"stage_{stage}")()
    return finalize(pipeline)


def run_job(
    source: Extraction | str | os.PathLike[str],
    client: LLMClient,
    options: JobOptions | None = None,
    **kwargs,
) -> JobResult:
    """Như `run_document`, nhưng nhận đường dẫn tệp (TXT/MD/DOCX) và tự bóc tách."""
    extraction = source if isinstance(source, Extraction) else extract(Path(source))
    return run_document(extraction, client, options, **kwargs)
