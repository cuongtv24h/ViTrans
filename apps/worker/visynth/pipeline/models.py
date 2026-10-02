"""Kiểu dữ liệu của một job tổng hợp: đơn vị tri thức, mục, khối, kết quả."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Từ vựng lấy từ `docs/schemas/common.schema.json` (có test đối chiếu).
IMPORTANCE = ("core", "supporting", "minor")
UNIT_TYPES = (
    "definition",
    "mechanism",
    "argument",
    "procedure",
    "fact_data",
    "prediction",
    "example",
    "qa",
    "anecdote",
    "aside",
    "admin",
)
ATTRIBUTION = ("author", "third_party", "unclear")
VERDICTS = ("supported", "partially_supported", "unsupported", "contradicted")
SECTION_KINDS = ("summary", "body")
FORMAT_HINTS = ("narrative", "bullets", "table", "mixed")


@dataclass
class LLMStats:
    """Sổ theo dõi lời gọi để đối chiếu `estimate` với chi phí thật (điều kiện thoát M0)."""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    retries: int = 0
    by_prompt: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "retries": self.retries,
            "by_prompt": dict(self.by_prompt),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, d: dict) -> LLMStats:
        stats = cls()
        stats.calls = int(d.get("calls") or 0)
        stats.tokens_in = int(d.get("tokens_in") or 0)
        stats.tokens_out = int(d.get("tokens_out") or 0)
        stats.retries = int(d.get("retries") or 0)
        stats.by_prompt = dict(d.get("by_prompt") or {})
        stats.extra = dict(d.get("extra") or {})
        return stats

    #: Số liệu riêng của từng giai đoạn (ví dụ `translate`: số đoạn bị đánh cờ) — đi vào `job_stages.metrics`.
    extra: dict = field(default_factory=dict)

    def add(self, prompt_id: str, tokens_in: int, tokens_out: int) -> None:
        self.calls += 1
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.by_prompt[prompt_id] = self.by_prompt.get(prompt_id, 0) + 1


@dataclass
class Unit:
    """Đơn vị tri thức (SPEC §6.5) — hàng của `knowledge_units`."""

    id: str
    segment_id: str
    local_id: str
    type: str
    importance: str
    title_vi: str
    statement_vi: str
    topics: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    attribution: str = "author"
    state: str = "active"  # active | unverified | omitted | merged
    merged_into: str | None = None
    section_id: str | None = None
    omitted_reason: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Unit:
        """Dựng lại từ `as_dict()` — dùng khi chạy tiếp pipeline từ điểm lưu."""
        return cls(
            id=d["id"],
            segment_id=d.get("segment_id", ""),
            local_id=d.get("local_id", ""),
            type=d.get("type", "fact_data"),
            importance=d.get("importance", "supporting"),
            title_vi=d.get("title_vi", ""),
            statement_vi=d.get("statement_vi", ""),
            topics=list(d.get("topics") or []),
            evidence=[dict(e) for e in (d.get("evidence") or [])],
            numbers=list(d.get("numbers") or []),
            terms=list(d.get("terms") or []),
            relations=[dict(r) for r in (d.get("relations") or [])],
            attribution=d.get("attribution", "author"),
            state=d.get("state", "active"),
            merged_into=d.get("merged_into"),
            section_id=d.get("section_id"),
            omitted_reason=d.get("omitted_reason"),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "segment_id": self.segment_id,
            "local_id": self.local_id,
            "type": self.type,
            "importance": self.importance,
            "title_vi": self.title_vi,
            "statement_vi": self.statement_vi,
            "topics": self.topics,
            "evidence": self.evidence,
            "numbers": self.numbers,
            "terms": self.terms,
            "relations": self.relations,
            "attribution": self.attribution,
            "state": self.state,
            "merged_into": self.merged_into,
            "section_id": self.section_id,
            "omitted_reason": self.omitted_reason,
        }


@dataclass
class Section:
    """Mục của báo cáo (SPEC §6.6) — hàng của `report_sections`."""

    id: str
    kind: str
    title_vi: str
    purpose_vi: str
    unit_ids: list[str]
    target_words: int
    format_hint: str = "narrative"

    @classmethod
    def from_dict(cls, d: dict) -> Section:
        return cls(
            id=d["id"],
            kind=d.get("kind", "body"),
            title_vi=d.get("title_vi", ""),
            purpose_vi=d.get("purpose_vi", ""),
            unit_ids=list(d.get("unit_ids") or []),
            target_words=int(d.get("target_words") or 0),
            format_hint=d.get("format_hint", "narrative"),
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title_vi": self.title_vi,
            "purpose_vi": self.purpose_vi,
            "unit_ids": self.unit_ids,
            "target_words": self.target_words,
            "format_hint": self.format_hint,
        }

    def as_prompt_json(self) -> dict:
        """Biến `section_json` của P4/P7 (không cần `unit_ids`)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "title_vi": self.title_vi,
            "purpose_vi": self.purpose_vi,
            "target_words": self.target_words,
            "format_hint": self.format_hint,
        }


@dataclass
class Block:
    """Khối nội dung (SPEC §6.8) — hàng của `report_blocks`."""

    block_id: str
    section_id: str
    type: str
    markdown_vi: str
    cites: list[str] = field(default_factory=list)
    verdict: str = "supported"
    issues: list[dict] = field(default_factory=list)
    flagged: bool = False
    removed: bool = False
    repair_round: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> Block:
        return cls(
            block_id=d["block_id"],
            section_id=d.get("section_id", ""),
            type=d.get("type", "paragraph"),
            markdown_vi=d.get("markdown_vi", ""),
            cites=list(d.get("cites") or []),
            verdict=d.get("verdict", "supported"),
            issues=[dict(i) for i in (d.get("issues") or [])],
            flagged=bool(d.get("flagged")),
            removed=bool(d.get("removed")),
            repair_round=int(d.get("repair_round") or 0),
        )

    def as_dict(self) -> dict:
        return {
            "block_id": self.block_id,
            "section_id": self.section_id,
            "type": self.type,
            "markdown_vi": self.markdown_vi,
            "cites": self.cites,
            "verdict": self.verdict,
            "issues": self.issues,
            "flagged": self.flagged,
            "removed": self.removed,
        }

    def as_prompt_json(self) -> dict:
        return {
            "block_id": self.block_id,
            "type": self.type,
            "markdown_vi": self.markdown_vi,
            "cites": self.cites,
        }


@dataclass
class JobOptions:
    """Lựa chọn của người dùng/người chạy cho một job."""

    level: str = "deep_synthesis"
    custom_instructions: str = ""
    skip_glossary_review: bool = False
    glossary_review_minutes: float = 15.0
    auto_confirm_min_confidence: float = 0.7
    gate_min_words: int = 2000
    max_candidates: int = 150
    style_core_text: str = ""
    allow_fuzzy_quotes: bool = False
    include_facts_table: bool | None = None
    recipe_hints: str = ""
    repair_max_rounds: int = 2
    verify_coverage_sample_rate: float = 0.10


@dataclass
class JobResult:
    """Kết quả một job: đủ để ghi `documents`/`reports`/`report_blocks` và để chấm điểm."""

    title: str
    level: str
    units: list[Unit] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    profile: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    glossary: list[dict] = field(default_factory=list)
    glossary_auto_confirmed: bool = False
    verdicts: dict[str, dict] = field(default_factory=dict)  # block_id -> {verdict, issues}
    coverage: dict[str, str] = field(default_factory=dict)  # unit_id -> yes|partial|no
    checks: dict[str, dict] = field(default_factory=dict)  # block_id -> BlockCheck.as dict
    stats: dict = field(default_factory=dict)
    grade: str = "C"
    markdown: str = ""
    events: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats_llm: LLMStats = field(default_factory=LLMStats)
    metrics: dict = field(default_factory=dict)
    #: Mức `full_translation`: kết quả P9 căn 1:1 theo pid (rỗng với các mức tổng hợp).
    translation_items: list = field(default_factory=list)

    def active_units(self) -> list[Unit]:
        return [u for u in self.units if u.state == "active"]

    def blocks_of(self, section_id: str) -> list[Block]:
        return [b for b in self.blocks if b.section_id == section_id and not b.removed]

    def emit(self, kind: str, **data: Any) -> None:
        self.events.append({"type": kind, "data": data})

    def summary(self) -> str:
        s = self.stats
        lines = [
            f"{self.title} — mức {self.level}, hạng {self.grade}",
            f"  đơn vị tri thức: {s.get('units_total', 0)} (core {s.get('units_core', 0)}, "
            f"unverified {s.get('units_unverified', 0)})",
            f"  mục: {len(self.sections)}, khối: {s.get('blocks_total', 0)}"
            + (f", đã xoá {s.get('blocks_removed', 0)}" if s.get("blocks_removed") else ""),
            f"  coverage_core: {s.get('coverage_core', 0):.2f}, faithfulness: {s.get('faithfulness_rate', 0):.2f}, "
            f"unresolved: {s.get('unresolved_blocks', 0)}",
            f"  từ nguồn/báo cáo: {s.get('source_words', 0)} / {s.get('report_words', 0)}",
            f"  lời gọi LLM: {self.stats_llm.calls} (vào {self.stats_llm.tokens_in}, ra {self.stats_llm.tokens_out})",
        ]
        if self.warnings:
            lines.append("  cảnh báo: " + "; ".join(self.warnings))
        return "\n".join(lines)
