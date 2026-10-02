"""Lõi văn phong (§19): cơ chế — kiểm tra, kế thừa, biên dịch, vòng đời duyệt; KHÔNG chứa nội dung văn phong."""

from __future__ import annotations

from visynth.stylecore.core import (
    NEUTRAL_TEXT,
    STAGES,
    Problem,
    approval_problems,
    canonical_json,
    compile_style_core,
    content_sha256,
    decisions_open,
    lint,
    resolve,
    resolve_chain,
)
from visynth.stylecore.store import ApprovalBlocked, StyleCoreError, StyleCoreStore, default_store_path

__all__ = [
    "NEUTRAL_TEXT",
    "STAGES",
    "ApprovalBlocked",
    "Problem",
    "StyleCoreError",
    "StyleCoreStore",
    "approval_problems",
    "canonical_json",
    "compile_style_core",
    "content_sha256",
    "decisions_open",
    "default_store_path",
    "lint",
    "resolve",
    "resolve_chain",
]
