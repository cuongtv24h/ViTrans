"""Khối `level_policy` chèn vào P3/P4 theo từng mức — SPEC §7.3.

Văn bản được TRÍCH NGUYÊN VĂN từ `docs/tools/spec_src/30_pipeline.md`; `tests/test_spec_conformance.py`
kiểm tra lại từng ký tự, nên sửa mức ở spec mà không cập nhật đây sẽ làm test đỏ.
"""

from __future__ import annotations

LEVEL_POLICY: dict[str, str] = {
    "detailed_synthesis": "[detailed_synthesis]\nLevel: detailed_synthesis (about 35% of the source length, at most 24000 words). Keep core units in full detail (all conditions, numbers, names and reasoning). Keep supporting units with their key detail (one or two sentences each). Examples: keep the point and the essential facts in two sentences. Q&A: keep when it adds information not stated elsewhere. Anecdotes and asides: omit unless they carry a fact. Minor units are not provided.",
    "deep_synthesis": "[deep_synthesis]\nLevel: deep_synthesis (about 12% of the source length, between 800 and 9000 words). Keep EVERY core unit with its conditions, numbers, names and reasoning, in condensed wording. Condense supporting units to one sentence each, or merge them into the sentence of the core unit they support. Examples: at most one short sentence, only when they clarify a core unit. Q&A: keep only answers that add information, as one sentence. Anecdotes, asides and admin: omit. Never drop a number, date or name that belongs to a core unit.",
    "executive_brief": "[executive_brief]\nLevel: executive_brief (about 3% of the source length, between 250 and 1500 words). Convey only the central claims: the most important core units, one or two sentences each, grouped by theme. Omit supporting units except decisive facts (numbers, dates) attached to a central claim. No examples, no Q&A, no anecdotes.",
}


def level_policy(level: str) -> str:
    """Khối văn bản cho một mức; mức `full_translation` không dùng (không có P3/P4)."""
    if level not in LEVEL_POLICY:
        raise ValueError(f"mức không dùng level_policy: {level}")
    return LEVEL_POLICY[level]
