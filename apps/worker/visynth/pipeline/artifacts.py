"""Bộ tệp của một lần chạy (`run.json`) — hợp đồng giữa pipeline và bộ chấm điểm (SPEC §16.2).

Một nguồn sự thật duy nhất: `visynth run --artifacts` và `eval/bakeoff.py` cùng dùng `to_artifact`, nên
artifact luôn có đúng các trường mà `eval/score.py` đọc (đơn vị tri thức, khối kèm kết quả kiểm tra,
coverage, sổ `llm_calls`, thời lượng).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ARTIFACT_NAME = "run.json"
REPORT_NAME = "report.md"


def to_artifact(
    result,
    *,
    source: dict | None = None,
    params: dict | None = None,
    duration_ms: int | None = None,
    llm_calls: dict | None = None,
    extra: dict | None = None,
) -> dict:
    """Dựng `run.json` từ `JobResult`. `source`/`params` mô tả lần chạy để tái lập về sau."""
    payload: dict[str, Any] = {
        "title": result.title,
        "level": result.level,
        "grade": result.grade,
        "metrics": result.metrics,
        "stats": result.stats,
        "warnings": result.warnings,
        "sections": [
            {"id": s.id, "title_vi": s.title_vi, "unit_ids": s.unit_ids, "target_words": s.target_words}
            for s in result.sections
        ],
        "blocks": [
            {"block_id": b.block_id, "cites": b.cites, "verdict": b.verdict, "flagged": b.flagged, "removed": b.removed}
            for b in result.blocks
        ],
        "units": [u.as_dict() for u in result.units],
        "blocks_full": [{**b.as_dict(), "check": result.checks.get(b.block_id, {})} for b in result.blocks],
        "coverage": result.coverage,
        "glossary": result.glossary,
        "profile": result.profile,
        "markdown": result.markdown,
        "llm_calls": llm_calls,
        "source": source or {"path": None, "words": result.stats.get("source_words"), "title": result.title},
        "duration_ms": duration_ms,
        "params": params or {"level": result.level},
    }
    if extra:
        payload.update(extra)
    return payload


def write_artifact(payload: dict, out_dir: str | Path) -> Path:
    """Ghi `run.json` + `report.md` vào `out_dir`; trả về đường dẫn `run.json`."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "artifacts": str(directory)}
    path = directory / ARTIFACT_NAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / REPORT_NAME).write_text(str(payload.get("markdown") or "") + "\n", encoding="utf-8")
    return path


__all__ = ["ARTIFACT_NAME", "REPORT_NAME", "to_artifact", "write_artifact"]
