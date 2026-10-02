"""Pipeline tổng hợp M0: P0–P8 chạy trên `FakeLLMClient` (SPEC §6)."""

from __future__ import annotations

from visynth.pipeline.models import IMPORTANCE, UNIT_TYPES, Block, JobOptions, JobResult, Section, Unit
from visynth.pipeline.run import run_document, run_job
from visynth.pipeline.stages import Pipeline, PipelineError

__all__ = [
    "IMPORTANCE",
    "UNIT_TYPES",
    "Block",
    "JobOptions",
    "JobResult",
    "Pipeline",
    "PipelineError",
    "Section",
    "Unit",
    "run_document",
    "run_job",
]
