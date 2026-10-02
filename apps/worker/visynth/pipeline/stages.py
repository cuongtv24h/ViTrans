"""Các giai đoạn LLM của pipeline (P0–P8) — SPEC §6.2–§6.9.

Mỗi giai đoạn ghi kết quả vào `Pipeline` (tương đương điểm lưu trong CSDL ở M1) và phát sự kiện.
Các nhánh hiếm chưa làm ở M0 được đánh dấu `TODO(M1)` kèm lý do.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

from visynth.checks.glossary import GlossaryEntry, first_use_by_section
from visynth.checks.quotes import QuoteMatch, bad_quote_ratio, verify_quote
from visynth.checks.report import (
    block_unresolved,
    check_block,
    coverage_core,
    faithful_block,
    grade_quality,
)
from visynth.estimate import report_budget_words
from visynth.extract.model import Extraction, Paragraph
from visynth.levelpolicy import level_policy
from visynth.llm.base import LLMClient, LLMError, LLMRequest
from visynth.pipeline.models import (
    Block,
    JobOptions,
    JobResult,
    Section,
    Unit,
)
from visynth.prompts import (
    Prompt,
    default_prompts_dir,
    default_schemas_dir,
    filter_glossary,
    format_segment_text,
    format_units_compact,
    neutral_style_core,
    render_prompt,
)
from visynth.segment import DEFAULT_TARGET_MAP, Segment, segment_extraction
from visynth.structured import SchemaError, SchemaStore

#: Nhu cầu tối thiểu của từng profile model (SPEC §17.6) — pool thật thay bằng `pool_config`.
PROFILE_NEEDS: dict[str, dict[str, Any]] = {
    "fast": {"structured": "json_schema", "min_ctx_in": 100_000},
    "writer": {"structured": "json_schema", "min_ctx_in": 100_000},
    "verifier": {"structured": "json_schema", "min_ctx_in": 100_000},
    "curator": {"structured": "json_schema", "min_ctx_in": 100_000},
    "ocr": {"structured": "json_schema", "min_ctx_in": 8_000, "vision": True, "pdf": True},
}

#: Mật độ đơn vị tri thức tối thiểu trên 1000 từ nguồn (§6.5 bước 6).
MIN_UNIT_DENSITY_PER_1K = 2.0
#: Tỷ lệ bằng chứng hỏng buộc chạy lại P2 (§6.5 bước 4).
BAD_QUOTE_RETRY_RATIO = 0.30
#: Số đoạn nguồn tối đa mỗi lời gọi P5 (§6.8).
MAX_SOURCE_PARAGRAPHS_PER_CALL = 60


class PipelineError(RuntimeError):
    """Lỗi không tự phục hồi được ở tầng pipeline (M1 sẽ đổi sang `defer_task`/đổi deployment)."""


class Pipeline:
    """Trạng thái và các bước của một job tổng hợp.

    M0 chạy các giai đoạn TUẦN TỰ trong một tiến trình (kết quả không đổi so với chạy song song);
    `per_job_concurrency`, điểm lưu và chạy lại theo giai đoạn thuộc M1.
    """

    def __init__(
        self,
        extraction: Extraction,
        client: LLMClient,
        options: JobOptions | None = None,
        *,
        prompts_dir: str | Path | None = None,
        schemas_dir: str | Path | None = None,
        seed: int = 7,
    ):
        self.ext = extraction
        self.client = client
        self.options = options or JobOptions()
        self.prompts_dir = Path(prompts_dir) if prompts_dir else default_prompts_dir()
        self.store = SchemaStore(Path(schemas_dir) if schemas_dir else default_schemas_dir())
        self.result = JobResult(title=extraction.title, level=self.options.level)
        self.profile: dict = {}
        self.segments: list[Segment] = []
        self.glossary: list[dict] = []  # mục đã xác nhận (ràng buộc cứng)
        self.glossary_entries: list[GlossaryEntry] = []
        self.first_use: dict[str, set[str]] = {}
        self._rng = random.Random(seed)
        self._scope_text = ""  # P8 ghi vào đây; `assemble` ghép vào Markdown
        self.paragraph_text = {p.pid: p.content for p in extraction.paragraphs}

    # ------------------------------------------------------------------ lời gọi LLM

    def call(
        self,
        prompt_id: str,
        variables: dict,
        *,
        append: str = "",
        schema: str | None = None,
        what: str = "",
    ) -> tuple[Any, Prompt]:
        """Render prompt, gọi client, (tuỳ chọn) kiểm tra schema. `append` là phản hồi cho lần chạy lại."""
        prompt = render_prompt(self.prompts_dir, prompt_id, variables)
        user = prompt.user + ("\n\n" + append if append else "")
        expected = self.store.load(schema) if schema else None
        req = LLMRequest(
            prompt_id=prompt.prompt_id,
            system=prompt.system,
            user=user,
            schema=expected,
            max_output_tokens=prompt.max_output_tokens,
            thinking=prompt.thinking,
            needs=PROFILE_NEEDS.get(prompt.model_profile, {"structured": "none"}),
            metadata={"stage": prompt.stage},
        )
        try:
            resp = self.client.complete(req)
        except LLMError as exc:
            self.result.emit("warning", code="llm_error", prompt=prompt.prompt_id, kind=exc.outcome.kind)
            raise PipelineError(f"{prompt.prompt_id}: {exc}") from exc
        self.result.stats_llm.add(prompt.prompt_id, resp.outcome.tokens_in, resp.outcome.tokens_out)
        if expected is None:
            return resp.text.strip(), prompt
        try:
            return self.store.validate(
                resp.parsed if resp.parsed is not None else resp.text, expected, what=what or prompt.prompt_id
            ), prompt
        except SchemaError as exc:
            raise _SchemaFailure(exc) from exc

    def call_retry_once(self, prompt_id: str, variables: dict, schema: str, *, what: str = "") -> tuple[Any, Prompt]:
        """Chạy một lần; nếu schema sai thì chạy lại MỘT lần kèm danh sách lỗi (§6.13)."""
        try:
            return self.call(prompt_id, variables, schema=schema, what=what)
        except _SchemaFailure as first:
            self.result.stats_llm.retries += 1
            self.result.emit("warning", code="invalid_output", prompt=prompt_id, detail=first.error.errors[:3])
            feedback = (
                "Your previous output was rejected. Fix exactly these problems and return valid JSON:\n"
                + "\n".join(f"- {e}" for e in first.error.errors)
            )
            return self.call(prompt_id, variables, append=feedback, schema=schema, what=what)

    def _style(self) -> str:
        return self.options.style_core_text or neutral_style_core("write")

    # ------------------------------------------------------------------ P0 — hồ sơ tài liệu

    def stage_profile(self) -> dict:
        sample = self._sample_paragraphs()
        stats = {
            "word_count": self.ext.word_count,
            "page_count": self.ext.page_count,
            "token_estimate": self.ext.token_estimate,
            "language_guess": self.ext.language_code,
            "paragraph_count": len(self.ext.paragraphs),
            "section_count": len(self.ext.sections),
            "extraction_quality": self.ext.extraction_quality,
        }
        variables = {
            "filename": self.ext.title,
            "stats_json": stats,
            "headings_outline": "\n".join(f"{s.section_id} {'  ' * (s.level - 1)}{s.title}" for s in self.ext.sections)
            or "(không có tiêu đề)",
            "sample_text": format_segment_text([{"pid": p.pid, "text": p.content} for p in sample]),
        }
        try:
            profile, _ = self.call_retry_once("P0", variables, "doc_profile.schema.json", what="hồ sơ tài liệu")
        except (
            Exception
        ) as exc:  # schema sai cả hai lần -> dùng hồ sơ bảo thủ (SPEC §6.13: còn cách xử lý theo giai đoạn)
            self.result.warnings.append("profile_fallback")
            self.result.emit("warning", code="profile_fallback", detail=str(exc)[:200])
            profile = self._fallback_profile()
        self.profile = profile
        self.result.profile = profile
        self.result.emit("stage_done", stage="profile", doc_type=profile.get("doc_type"))
        return profile

    def _fallback_profile(self) -> dict:
        """Hồ sơ bảo thủ khi P0 hỏng cả hai lần — đúng hình dạng `doc_profile.schema.json` (§6.13)."""
        risks = []
        if any(p.kind == "table" for p in self.ext.paragraphs):
            risks.append("tables")
        if any(p.kind == "code" for p in self.ext.paragraphs):
            risks.append("code")
        if self.ext.word_count and len(_doc_numbers(self.ext)) / self.ext.word_count > 0.02:
            risks.append("many_numbers")
        return {
            "language_code": self.ext.language_code or "en",
            "language_confidence": self.ext.language_confidence,
            "doc_type": "other",
            "domain": "general",
            "domain_tags": [],
            "title_guess": self.ext.title,
            "authors_or_speakers": [],
            "register": "mixed",
            "attribution_mode": "attribute_to_author",
            "structure": {
                "has_toc": False,
                "has_headings": bool(self.ext.sections),
                "has_speaker_turns": any(p.kind == "speaker_turn" for p in self.ext.paragraphs),
                "has_timecodes": any(p.timecode_start_ms is not None for p in self.ext.paragraphs),
                "has_footnotes": any(p.kind == "footnote" for p in self.ext.paragraphs),
                "has_tables": any(p.kind == "table" for p in self.ext.paragraphs),
            },
            "content_risks": risks,
            "recommended_segmentation": {
                "strategy": "by_headings" if self.ext.sections else "by_tokens",
                "target_tokens": DEFAULT_TARGET_MAP,
            },
            "glossary_hint": "Các thuật ngữ chuyên ngành cần dịch nhất quán trong toàn tài liệu.",
            "notes_vi": "Hồ sơ bảo thủ do lỗi P0; pipeline dùng cấu hình mặc định an toàn.",
        }

    def _sample_paragraphs(self, max_paragraphs: int = 24, max_chars: int = 6000) -> list[Paragraph]:
        """Mẫu đại diện: đoạn đầu, đoạn giữa và đoạn cuối, trong giới hạn ký tự của P0."""
        paras = [p for p in self.ext.paragraphs if p.kind != "heading"] or self.ext.paragraphs
        if not paras:
            return []
        step = max(1, len(paras) // max_paragraphs)
        picked = paras[::step][:max_paragraphs]
        out, used = [], 0
        for p in picked:
            if used + p.char_count > max_chars and out:
                break
            out.append(p)
            used += p.char_count
        return out

    # ------------------------------------------------------------------ P1 — glossary và cổng duyệt

    def stage_glossary(self, input_entries: list[dict] | None = None) -> list[dict]:
        existing = list(input_entries or [])
        text = self.ext.full_text()
        max_chars = 700_000 * 4  # ≤ 700k token trong một lời gọi (ước lượng thô khi chưa có count_tokens)
        windows = [text] if len(text) <= max_chars else _windows(text, 300_000 * 4, overlap=2_000)
        candidates: list[dict] = []
        for i, chunk in enumerate(windows, 1):
            variables = {
                "profile_json": self.profile,
                "existing_glossary_json": existing,
                "max_candidates": self.options.max_candidates,
                "document_text": _limit_by_chars(chunk, max_chars),
                "style_core": self._style(),
            }
            data, _ = self.call_retry_once(
                "P1", variables, "glossary_candidates.schema.json", what=f"glossary cửa sổ {i}"
            )
            candidates.extend(data["candidates"])
            self.result.emit("stage_progress", stage="glossary", window=i, of=len(windows))
        merged = _merge_candidates(candidates)
        entries = [
            {
                "source_term": c["source_term"],
                "target_term": c["target_term"],
                "keep_original": c["keep_original"],
                "case_sensitive": False,
                "forbidden_variants": [],
                "confidence": c["confidence"],
                "occurrences": c["occurrences"],
                "first_pid": c["first_pid"],
                "origin": "suggested",
                "status": "suggested",
            }
            for c in merged
        ]
        self.result.glossary = entries
        self._apply_glossary_gate(entries, existing)
        self.result.emit("stage_done", stage="glossary", candidates=len(entries), confirmed=len(self.glossary))
        return self.glossary

    def _apply_glossary_gate(self, entries: list[dict], existing: list[dict]) -> None:
        """Cổng duyệt §6.4: tài liệu dài thì chờ người dùng; hết hạn (hoặc CLI M0) tự xác nhận theo quy tắc."""
        confirmed_existing = [dict(e, origin=e.get("origin", "shared"), status="confirmed") for e in existing]
        if self.ext.word_count < self.options.gate_min_words or self.options.skip_glossary_review:
            auto = [e for e in entries if e["confidence"] >= self.options.auto_confirm_min_confidence]
            self.glossary = confirmed_existing + [dict(e, status="confirmed") for e in auto]
            self.result.glossary_auto_confirmed = True
            self.result.emit(
                "glossary_auto_confirmed",
                kept=len(auto),
                dropped=len(entries) - len(auto),
                reason="short_doc" if self.ext.word_count < self.options.gate_min_words else "user_skipped",
            )
        else:
            # M1: UI chờ người dùng `POST /jobs/{id}/glossary/confirm`; ở M0 CLI không có UI nên áp quy tắc hết hạn 15 phút.
            auto = [e for e in entries if e["confidence"] >= self.options.auto_confirm_min_confidence]
            self.glossary = confirmed_existing + [dict(e, status="confirmed") for e in auto]
            self.result.glossary_auto_confirmed = True
            self.result.emit(
                "glossary_auto_confirmed",
                kept=len(auto),
                dropped=len(entries) - len(auto),
                reason="deadline_m0_cli",
            )
        self.glossary_entries = [
            GlossaryEntry(
                source_term=e["source_term"],
                target_term=e["target_term"],
                keep_original=bool(e.get("keep_original")),
                case_sensitive=bool(e.get("case_sensitive")),
                forbidden_variants=tuple(e.get("forbidden_variants", [])),
            )
            for e in self.glossary
        ]

    # ------------------------------------------------------------------ P2 — kê khai khuyến thức

    def stage_map(self) -> list[Unit]:
        seg_rec = self.profile.get("recommended_segmentation") or {}
        strategy = seg_rec.get("strategy") or "by_headings"
        target = seg_rec.get("target_tokens") or DEFAULT_TARGET_MAP
        self.segments = segment_extraction(self.ext, mode="map", target_tokens=target, strategy=strategy)
        allow_fuzzy = bool(self.options.allow_fuzzy_quotes)
        units: list[Unit] = []
        unverified = 0
        for seg in self.segments:
            seg_units, stats = self._map_one_segment(seg, allow_fuzzy=allow_fuzzy)
            units.extend(seg_units)
            unverified += stats["unverified_units"]
            if stats["degraded"]:
                self.result.warnings.append(f"extraction_degraded:{seg.segment_id}")
        # Cấp ID toàn cục theo (segment, local_id) — §6.5 bước 6
        units.sort(key=lambda u: (u.segment_id, int(re.sub(r"\D", "", u.local_id) or 0)))
        remap: dict[tuple[str, str], str] = {}
        for i, u in enumerate(units, 1):
            remap[(u.segment_id, u.local_id)] = f"U-{i:04d}"
        for u in units:
            u.id = remap[(u.segment_id, u.local_id)]
        for u in units:
            u.relations = [dict(r, target=_resolve_relation(u, r, remap)) for r in u.relations]
        self.result.units = units
        core_unverified = sum(1 for u in units if u.state == "unverified" and u.importance == "core")
        core_total = sum(1 for u in units if u.importance == "core")
        density = len(units) / max(1, self.ext.word_count) * 1000
        if density < MIN_UNIT_DENSITY_PER_1K and (
            self.profile.get("doc_type") in ("book", "lecture_transcript", "paper")
        ):
            self.result.warnings.append(f"unit_density_low:{density:.2f}/1000 từ")
        if core_total and core_unverified / core_total >= 0.03:
            self.result.warnings.append("core_unverified_over_3pct")
        self.result.emit("stage_done", stage="map", segments=len(self.segments), units=len(units))
        return units

    def _map_one_segment(self, seg: Segment, *, allow_fuzzy: bool) -> tuple[list[Unit], dict]:
        """Chạy P2 cho một segment, xác minh bằng chứng, chạy lại khi cần (§6.5 bước 2-4)."""
        glossary = filter_glossary(self.glossary, seg.render())
        base_vars = {
            "segment_id": seg.segment_id,
            "profile_json": self.profile,
            "glossary_json": glossary,
            "context_before": seg.render_context() or "(không có)",
            "segment_text": seg.render(),
            "style_core": self._style(),
        }
        attempts = 0
        feedback = ""
        while True:
            attempts += 1
            try:
                data, _ = self.call(
                    "P2", base_vars, append=feedback, schema="segment_analysis.schema.json", what="kê khai"
                )
            except _SchemaFailure as exc:
                if attempts > 1:
                    self.result.warnings.append(f"map_schema_failed:{seg.segment_id}")
                    return [], {"unverified_units": 0, "degraded": True}
                self.result.stats_llm.retries += 1
                feedback = (
                    "Your previous output was rejected. Fix exactly these problems and return valid JSON:\n"
                    + "\n".join(f"- {e}" for e in exc.error.errors)
                )
                continue
            units, stats = self._verify_map_output(data, seg, allow_fuzzy=allow_fuzzy)
            bad = stats["bad_quote_ratio"]
            need_retry = bad > BAD_QUOTE_RETRY_RATIO or (not units and _words(seg) > 300)
            if need_retry and attempts <= 1:
                self.result.stats_llm.retries += 1
                feedback = (
                    "Kiểm tra bằng code đã bác các bằng chứng sau (trích đoạn phải NGUYÊN VĂN trong đoạn có pid đó):\n"
                )
                feedback += "\n".join(f"- {b}" for b in stats["bad_detail"][:20])
                if not units:
                    feedback += "\n- Không có đơn vị tri thức nào cho segment > 300 từ: hãy kê khai đầy đủ."
                self.result.emit("warning", code="map_retry", segment=seg.segment_id, bad_ratio=round(bad, 3))
                continue
            stats["degraded"] = need_retry
            return units, stats

    def _verify_map_output(self, data: dict, seg: Segment, *, allow_fuzzy: bool) -> tuple[list[Unit], dict]:
        seg_pids = {p.pid for p in seg.paragraphs}
        units: list[Unit] = []
        all_matches: list[list[QuoteMatch]] = []
        bad_detail: list[str] = []
        unverified = 0
        for raw in data["units"]:
            ev, ev_matches, detail = [], [], []
            for e in raw.get("evidence", []):
                if e["pid"] not in seg_pids:
                    detail.append(f"{e['pid']}: pid không thuộc segment {seg.segment_id}")
                    ev_matches.append(QuoteMatch("missing", 0.0))
                    continue
                m = verify_quote(e["quote"], self.paragraph_text.get(e["pid"], ""), allow_fuzzy=allow_fuzzy)
                ev_matches.append(m)
                if m.ok:
                    ev.append({"pid": e["pid"], "quote": e["quote"]})
                else:
                    detail.append(f"{e['pid']}: {e['quote'][:80]!r} không khớp nguồn ({m.status})")
            all_matches.append(ev_matches or [QuoteMatch("missing", 0.0)])
            bad_detail.extend(detail)
            state = "active" if ev else "unverified"
            if state == "unverified":
                unverified += 1
            units.append(
                Unit(
                    id="",  # cấp sau khi gộp toàn job
                    segment_id=seg.segment_id,
                    local_id=raw["local_id"],
                    type=raw["type"],
                    importance=raw["importance"],
                    title_vi=raw["title_vi"],
                    statement_vi=raw["statement_vi"],
                    topics=list(raw.get("topics", [])),
                    evidence=ev,
                    numbers=[n["source_text"] for n in raw.get("numbers", [])],
                    terms=list(raw.get("terms", [])),
                    relations=[dict(r, target=_local_relation_target(r)) for r in raw.get("relations", [])],
                    attribution=raw.get("attribution", "author"),
                    state=state,
                )
            )
        # Nhãn đoạn phải phủ kín mọi pid; khoảng trống tự gán `core` (bảo thủ), chồng lấn giữ khoảng đầu (§6.5 bước 3)
        coverage = _label_coverage([f"{p.pid}" for p in seg.paragraphs], data["paragraph_labels"])
        seg.labels = coverage
        seg.summary_vi = data["segment_summary_vi"]
        stats = {
            "bad_quote_ratio": bad_quote_ratio(all_matches),
            "bad_detail": bad_detail,
            "unverified_units": unverified,
            "new_terms": data.get("new_terms", []),
            "quality_flags": data.get("quality_flags", []),
        }
        return units, stats

    # ------------------------------------------------------------------ P3 — dàn ý và phân bổ

    def stage_consolidate(self) -> list[Section]:
        budget = report_budget_words(self.ext.word_count, self.options.level)
        active = self._plannable_units()
        if len(active) > 1500:
            # TODO(M1): phân cấp theo `topics` thành cụm ≤ 1500 rồi một lời gọi P3 sắp thứ tự/đặt tiêu đề.
            self.result.warnings.append("plan_hierarchy_deferred")
            active = active[:1500]
        variables = {
            "profile_json": self.profile,
            "level": self.options.level,
            "level_policy": level_policy(self.options.level),
            "budget_words": budget,
            "units_compact": format_units_compact([u.as_dict() for u in active]),
            "recipe_hints": self.options.recipe_hints or "(không có)",
            "custom_instructions": self.options.custom_instructions,
            "style_core": self._style(),
        }
        plan, problems = None, []
        for attempt in (1, 2):
            append = (
                ""
                if attempt == 1
                else "Kế hoạch bị từ chối. Sửa đúng các lỗi sau rồi trả lại JSON:\n"
                + "\n".join(f"- {p}" for p in problems)
            )
            try:
                plan, _ = self.call("P3", variables, append=append, schema="report_plan.schema.json", what="kế hoạch")
            except _SchemaFailure as exc:
                if attempt == 2:
                    raise PipelineError(f"P3 không trả được kế hoạch hợp lệ: {exc.error}") from exc
                problems = exc.error.errors
                self.result.stats_llm.retries += 1
                continue
            problems = check_plan(plan, active, budget)
            if not problems:
                break
            self.result.stats_llm.retries += 1
            self.result.emit("warning", code="plan_invalid", problems=problems[:5])
        if problems:
            plan = autofix_plan(plan, active, budget)
            self.result.warnings.append("plan_autofixed")
            self.result.emit("warning", code="plan_autofixed", remaining=problems[:5])
        self.result.plan = plan
        self.sections = [
            Section(
                id=s["id"],
                kind=s["kind"],
                title_vi=s["title_vi"],
                purpose_vi=s["purpose_vi"],
                unit_ids=list(s["unit_ids"]),
                target_words=int(s["target_words"]),
                format_hint=s.get("format_hint", "narrative"),
            )
            for s in plan["sections"]
        ]
        self._mark_unit_fates(plan, active)
        self.result.sections = self.sections
        self.result.emit("stage_done", stage="consolidate", sections=len(self.sections), budget=budget)
        return self.sections

    def _plannable_units(self) -> list[Unit]:
        """Đơn vị đưa vào P3: bỏ `minor` ở mức 3-4, bỏ unit `unverified` (§6.5, §6.6)."""
        out = []
        for u in self.result.units:
            if u.state != "active":
                continue
            if self.options.level in ("deep_synthesis", "executive_brief") and u.importance == "minor":
                continue
            out.append(u)
        return out

    def _mark_unit_fates(self, plan: dict, active: list[Unit]) -> None:
        assigned: dict[str, str] = {}
        for s in plan["sections"]:
            for uid in s["unit_ids"]:
                assigned.setdefault(uid, s["id"])
        merged: dict[str, str] = {}
        for g in plan.get("merged_groups", []):
            for uid in g["merged_unit_ids"]:
                merged[uid] = g["keep_unit_id"]
        omitted = {o["unit_id"]: o["reason"] for o in plan.get("omitted", [])}
        for u in self.result.units:
            if u.id in assigned:
                u.section_id = assigned[u.id]
            elif u.id in merged:
                u.state, u.merged_into = "merged", merged[u.id]
            elif u.id in omitted:
                u.state, u.omitted_reason = "omitted", omitted[u.id]
            elif u.importance == "minor" and self.options.level in ("deep_synthesis", "executive_brief"):
                u.state, u.omitted_reason = "omitted", "level_policy"
        for u in active:
            if u.state == "active" and not u.section_id:
                u.state, u.omitted_reason = "omitted", "level_policy"

    # ------------------------------------------------------------------ P4 — viết từng mục

    def stage_write(self) -> list[Block]:
        order = [s.id for s in self.sections]
        units_by_id = {u.id: u for u in self.result.units}
        terms_by_section: dict[str, set[str]] = {}
        for s in self.sections:
            terms: set[str] = set()
            for uid in s.unit_ids:
                u = units_by_id.get(uid)
                if u:
                    terms.update(t for t in u.terms)
                    terms.update(_glossary_terms_in(u.statement_vi + " " + u.title_vi, self.glossary))
            terms_by_section[s.id] = terms
        self.first_use = first_use_by_section(order, terms_by_section, self.glossary_entries)
        blocks: list[Block] = []
        outline = [{"id": s.id, "title_vi": s.title_vi, "purpose_vi": s.purpose_vi} for s in self.sections]
        for s in self.sections:
            section_units = [units_by_id[uid].as_dict() for uid in s.unit_ids if uid in units_by_id]
            variables = {
                "profile_json": self.profile,
                "level": self.options.level,
                "level_policy": level_policy(self.options.level),
                "section_json": s.as_prompt_json(),
                "first_use_terms": sorted(self.first_use.get(s.id, set())),
                "glossary_json": filter_glossary(self.glossary, " ".join(u["statement_vi"] for u in section_units)),
                "outline_digest": outline,
                "custom_instructions": self.options.custom_instructions,
                "units_json": section_units,
                "style_core": self._style(),
            }
            append = ""
            for attempt in (1, 2):
                try:
                    data, _ = self.call(
                        "P4", variables, append=append, schema="section_output.schema.json", what=f"mục {s.id}"
                    )
                except _SchemaFailure as exc:
                    if attempt == 2:
                        raise PipelineError(f"P4 mục {s.id} thất bại: {exc.error}") from exc
                    self.result.stats_llm.retries += 1
                    append = "Đầu ra trước bị từ chối. Sửa đúng các lỗi:\n" + "\n".join(
                        f"- {e}" for e in exc.error.errors
                    )
                    continue
                section_blocks = self._blocks_from_p4(data, s)
                problem = _write_problems(
                    section_blocks, s, section_units, self.first_use.get(s.id, set()), self.glossary_entries
                )
                if not problem or attempt == 2:
                    if problem:
                        self.result.warnings.append(f"write_flagged:{s.id}")
                        for b in section_blocks:
                            b.flagged = True
                            b.issues.append({"type": "other", "detail_vi": "; ".join(problem[:3])})
                    blocks.extend(section_blocks)
                    break
                self.result.stats_llm.retries += 1
                append = "Kiểm tra bằng code thấy các vấn đề sau, hãy viết lại mục này cho đúng:\n" + "\n".join(
                    f"- {p}" for p in problem
                )
            self.result.emit("stage_progress", stage="write", section=s.id)
        self.result.blocks = blocks
        self.result.emit("stage_done", stage="write", blocks=len(blocks))
        return blocks

    def _blocks_from_p4(self, data: dict, section: Section) -> list[Block]:
        out = []
        for i, b in enumerate(data["blocks"], 1):
            out.append(
                Block(
                    block_id=f"{section.id}.b{i:02d}",
                    section_id=section.id,
                    type=b["type"],
                    markdown_vi=b["markdown_vi"],
                    cites=list(b["cites"]),
                )
            )
        return out

    # ------------------------------------------------------------------ P5/P6 + D1–D9 — kiểm chứng

    def stage_verify(self) -> dict:
        units_by_id = {u.id: u for u in self.result.units}
        doc_numbers = _doc_numbers(self.ext)
        verdicts: dict[str, dict] = {}
        checks: dict[str, dict] = {}
        for s in self.sections:
            section_units = {uid for uid in s.unit_ids}
            for b in self.result.blocks_of(s.id):
                check = check_block(
                    block_id=b.block_id,
                    section_id=s.id,
                    markdown_vi=b.markdown_vi,
                    cites=b.cites,
                    section_unit_ids=section_units,
                    unit_by_id={uid: units_by_id[uid].as_dict() for uid in section_units if uid in units_by_id},
                    paragraphs=self.paragraph_text,
                    doc_numbers=doc_numbers,
                    glossary=self.glossary_entries,
                    first_use_terms=self.first_use.get(s.id, set()),
                    target_words=round(s.target_words / max(1, len(self.result.blocks_of(s.id)))),
                    level=self.options.level,
                )
                checks[b.block_id] = {
                    "issues": [i.__dict__ for i in check.issues],
                    "cites_ok": check.cites_ok,
                    "unverified_numbers": check.unverified_numbers,
                    "diacritic_ratio": check.diacritic_ratio,
                    "length_ratio": check.length_ratio,
                }
                b.issues = [i.__dict__ for i in check.issues]
            # P5 cho mọi khối (§6.8: sample_rate = 1.0 ở MVP)
            for payload in self._p5_payloads(s):
                data, _ = self.call_retry_once("P5", payload, "faithfulness.schema.json", what=f"trung thực {s.id}")
                for r in data["results"]:
                    verdicts[r["block_id"]] = {"verdict": r["verdict"], "issues": r["issues"], "section_id": s.id}
                self.result.emit("stage_progress", stage="verify", section=s.id, part="P5")
        # Gộp: verdict từ P5, issue gộp từ D + P5
        for b in self.result.blocks:
            v = verdicts.get(b.block_id, {"verdict": "supported", "issues": []})
            b.verdict = v["verdict"]
            b.issues = _merge_issues(b.issues, v["issues"])
        # P6 cho (a) core chưa được trích dẫn và (b) mẫu 10% core đã được trích dẫn
        cited = {c for b in self.result.blocks if not b.removed for c in b.cites}
        core_ids = [u.id for u in self.result.units if u.importance == "core" and u.state == "active"]
        uncited = [uid for uid in core_ids if uid not in cited]
        sample = (
            self._rng.sample(
                sorted(cited & set(core_ids)),
                k=max(1, round(len(cited & set(core_ids)) * self.options.verify_coverage_sample_rate)),
            )
            if (cited & set(core_ids))
            else []
        )
        to_check = sorted(set(uncited) | set(sample))
        if to_check:
            for payload in self._p6_payloads(to_check):
                data, _ = self.call_retry_once("P6", payload, "coverage.schema.json", what="độ phủ")
                for r in data["results"]:
                    self.result.coverage[r["unit_id"]] = r["covered"]
        for uid in core_ids:
            # Khối sống nào trích dẫn unit này thì mặc định là đã phủ; P6 chỉ kiểm tra mẫu 10% và các unit
            # không được trích dẫn ở đâu (§6.8) — nếu không, mọi unit không nằm trong mẫu sẽ bị tính là thiếu.
            self.result.coverage.setdefault(uid, "yes" if uid in cited else "no")
        self.result.verdicts = verdicts
        self.result.checks = checks
        self.result.emit("stage_done", stage="verify", blocks=len(self.result.blocks), p6_units=len(to_check))
        return checks

    def _p5_payloads(self, section: Section) -> list[dict]:
        """Chia lời gọi P5 sao cho mỗi lời gọi ≤ 60 đoạn nguồn (§6.8)."""
        units_by_id = {u.id: u for u in self.result.units}
        payloads: list[dict] = []
        pending_blocks: list[Block] = []
        pending_pids: set[str] = set()
        for b in self.result.blocks_of(section.id):
            pids = {ev["pid"] for c in b.cites if c in units_by_id for ev in units_by_id[c].evidence}
            if pending_blocks and len(pending_pids | pids) > MAX_SOURCE_PARAGRAPHS_PER_CALL:
                payloads.append(self._p5_variables(section, pending_blocks, pending_pids))
                pending_blocks, pending_pids = [], set()
            pending_blocks.append(b)
            pending_pids |= pids
        if pending_blocks:
            payloads.append(self._p5_variables(section, pending_blocks, pending_pids))
        return payloads

    def _p5_variables(self, section: Section, blocks: list[Block], pids: set[str]) -> dict:
        units_by_id = {u.id: u for u in self.result.units}
        evidence = [
            {
                "unit_id": c,
                "statement_vi": units_by_id[c].statement_vi,
                "evidence": units_by_id[c].evidence,
            }
            for b in blocks
            for c in b.cites
            if c in units_by_id
        ]
        passages = _neighbour_passages(pids, self.ext.paragraphs)
        return {
            "attribution_mode": (self.profile.get("attribution_mode") or "unclear"),
            "glossary_json": self.glossary,
            "section_id": section.id,
            "blocks_json": [b.as_prompt_json() for b in blocks],
            "evidence_json": evidence,
            "source_passages": passages,
        }

    def _p6_payloads(self, unit_ids: list[str]) -> list[dict]:
        units_by_id = {u.id: u for u in self.result.units}
        out = []
        chunk_size = 40
        for i in range(0, len(unit_ids), chunk_size):
            chunk = unit_ids[i : i + chunk_size]
            out.append(
                {
                    "units_json": [units_by_id[uid].as_dict() for uid in chunk if uid in units_by_id],
                    "report_blocks_json": [b.as_prompt_json() for b in self.result.blocks if not b.removed],
                }
            )
        return out

    # ------------------------------------------------------------------ P7 — sửa tối thiểu

    def stage_repair(self) -> int:
        rounds = 0
        for round_no in range(1, self.options.repair_max_rounds + 1):
            targets = self._repair_targets()
            if not targets:
                break
            rounds = round_no
            for s in self.sections:
                section_blocks = [b for b in self.result.blocks_of(s.id) if b.block_id in targets]
                if not section_blocks:
                    continue
                missing = [
                    u
                    for u in self.result.units
                    if u.section_id == s.id
                    and u.importance == "core"
                    and self.result.coverage.get(u.id) in (None, "no", "partial")
                ]
                variables = {
                    "profile_json": self.profile,
                    "section_json": s.as_prompt_json(),
                    "first_use_terms": sorted(self.first_use.get(s.id, set())),
                    "glossary_json": self.glossary,
                    "blocks_json": [b.as_prompt_json() for b in section_blocks],
                    "issues_json": {b.block_id: b.issues for b in section_blocks},
                    "missing_units_json": [u.as_dict() for u in missing][:20],
                    "source_passages": _neighbour_passages(
                        {
                            ev["pid"]
                            for b in section_blocks
                            for c in b.cites
                            for ev in _evidence_of(c, self.result.units)
                        },
                        self.ext.paragraphs,
                    ),
                    "style_core": self._style(),
                }
                try:
                    data, _ = self.call_retry_once("P7", variables, "repair_output.schema.json", what=f"sửa {s.id}")
                except Exception as exc:
                    self.result.warnings.append(f"repair_failed:{s.id}")
                    self.result.emit("warning", code="repair_failed", section=s.id, detail=str(exc)[:160])
                    continue
                self._apply_patches(s, data, round_no)
                self._recheck_section(s, changed=[p.get("block_id") for p in data.get("replace", [])])
            self.result.emit("stage_progress", stage="repair", round=round_no, blocks=len(targets))
        # Sau vòng cuối: xoá khối `fabricated`/`contradicted`, đánh cờ phần còn lại (§6.9)
        for b in self.result.blocks:
            v = self.result.verdicts.get(b.block_id, {}).get("verdict", b.verdict)
            if v in ("fabricated", "contradicted") or any(i.get("type") == "fabricated" for i in b.issues):
                b.removed = True
                self.result.emit("block_removed", block=b.block_id, reason="fabricated")
            elif block_unresolved(v, b.issues):
                b.flagged = True
        for s in self.sections:
            if not self.result.blocks_of(s.id):
                self.result.warnings.append(f"section_empty:{s.id}")
        self.result.emit("stage_done", stage="repair", rounds=rounds)
        return rounds

    def _repair_targets(self) -> set[str]:
        out = set()
        for b in self.result.blocks:
            if b.removed:
                continue
            v = self.result.verdicts.get(b.block_id, {}).get("verdict", b.verdict)
            if v in ("unsupported", "contradicted") or block_unresolved(v, b.issues):
                out.add(b.block_id)
        # unit core chưa được trích dẫn ở đâu cũng cần sửa (§6.9)
        cited = {c for b in self.result.blocks if not b.removed for c in b.cites}
        if any(u.importance == "core" and u.state == "active" and u.id not in cited for u in self.result.units):
            out.update(b.block_id for s in self.sections for b in self.result.blocks_of(s.id))
        return out

    def _apply_patches(self, section: Section, data: dict, round_no: int) -> None:
        by_id = {b.block_id: b for b in self.result.blocks}
        section_unit_ids = set(section.unit_ids)
        for patch in data.get("replace", []):
            b = by_id.get(patch["block_id"])
            if not b:
                continue
            block = patch["block"]
            new_cites = [c for c in block["cites"] if c in section_unit_ids]
            b.markdown_vi = block["markdown_vi"]
            b.type = block["type"]
            b.cites = new_cites or b.cites
            b.repair_round = round_no
        for patch in data.get("insert_after", []):
            anchor = by_id.get(patch["after_block_id"])
            block = patch["block"]
            new = Block(
                block_id=f"{section.id}.b{len([x for x in self.result.blocks if x.section_id == section.id]) + 1:02d}",
                section_id=section.id,
                type=block["type"],
                markdown_vi=block["markdown_vi"],
                cites=[c for c in block["cites"] if c in section_unit_ids],
                repair_round=round_no,
            )
            if anchor:
                self.result.blocks.insert(self.result.blocks.index(anchor) + 1, new)
        for patch in data.get("delete", []):
            b = by_id.get(patch["block_id"])
            if b:
                b.removed = True

    def _recheck_section(self, section: Section, changed: list[str | None]) -> None:
        """Kiểm tra lại tất định + P5 chỉ cho khối đã đổi (§6.9)."""
        units_by_id = {u.id: u for u in self.result.units}
        doc_numbers = _doc_numbers(self.ext)
        for b in self.result.blocks_of(section.id):
            check = check_block(
                block_id=b.block_id,
                section_id=section.id,
                markdown_vi=b.markdown_vi,
                cites=b.cites,
                section_unit_ids=set(section.unit_ids),
                unit_by_id={uid: units_by_id[uid].as_dict() for uid in section.unit_ids if uid in units_by_id},
                paragraphs=self.paragraph_text,
                doc_numbers=doc_numbers,
                glossary=self.glossary_entries,
                first_use_terms=self.first_use.get(section.id, set()),
                level=self.options.level,
            )
            det = [i.__dict__ for i in check.issues]
            b.issues = _merge_issues(
                det, [] if b.block_id in changed else self.result.verdicts.get(b.block_id, {}).get("issues", [])
            )
        if changed:
            payloads = self._p5_payloads(section)
            for payload in payloads:
                blocks_in_call = {b["block_id"] for b in payload["blocks_json"]}
                if not (blocks_in_call & set(changed)):
                    continue
                try:
                    data, _ = self.call_retry_once(
                        "P5", payload, "faithfulness.schema.json", what=f"trung thực lại {section.id}"
                    )
                except Exception:
                    continue
                for r in data["results"]:
                    if r["block_id"] in changed:
                        self.result.verdicts[r["block_id"]] = {
                            "verdict": r["verdict"],
                            "issues": r["issues"],
                            "section_id": section.id,
                        }
                        b = next((x for x in self.result.blocks if x.block_id == r["block_id"]), None)
                        if b:
                            b.verdict = r["verdict"]
                            b.issues = _merge_issues(
                                [i for i in b.issues if not i.get("from_llm")],
                                [dict(i, from_llm=True) for i in r["issues"]],
                            )

    # ------------------------------------------------------------------ D9 + thống kê (dùng ở assemble)

    def metrics(self) -> dict:
        core = [u for u in self.result.units if u.importance == "core" and u.state == "active"]
        blocks = [b for b in self.result.blocks if not b.removed]
        cited = {c for b in blocks for c in b.cites}
        # unit không còn được khối nào sống trích dẫn thì không thể coi là đã phủ (§6.8)
        coverage = {u.id: ("no" if u.id not in cited else self.result.coverage.get(u.id, "yes")) for u in core}
        cov = coverage_core([u.as_dict() for u in core], coverage)
        faithful = sum(1 for b in blocks if faithful_block(b.verdict, b.issues))
        unresolved = sum(1 for b in blocks if block_unresolved(b.verdict, b.issues))
        bad = sum(1 for b in blocks if b.verdict in ("contradicted", "fabricated"))
        grades = grade_quality(
            coverage_core=cov, n_blocks=len(blocks), unresolved_blocks=unresolved, fabricated_or_contradicted=bad
        )
        return {
            "coverage_core": cov,
            "faithfulness_rate": round(faithful / len(blocks), 4) if blocks else 1.0,
            "unresolved_blocks": unresolved,
            "blocks_total": len(blocks),
            "blocks_removed": len(self.result.blocks) - len(blocks),
            "units_total": len(self.result.units),
            "units_core": len(core),
            "units_unverified": sum(1 for u in self.result.units if u.state == "unverified"),
            "grade": grades,
        }


# ---------------------------------------------------------------------- tiện ích dùng chung


class _SchemaFailure(Exception):
    """Bọc `SchemaError` để phân biệt với lỗi mạng trong `call_retry_once`."""

    def __init__(self, error: SchemaError):
        super().__init__(str(error))
        self.error = error


def _words(seg: Segment) -> int:
    return sum(len(p.content.split()) for p in seg.paragraphs)


def _windows(text: str, size: int, overlap: int) -> list[str]:
    out, start = [], 0
    while start < len(text):
        out.append(text[start : start + size])
        start += size - overlap
        if start + overlap >= len(text):
            break
    return out


def _limit_by_chars(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars]


def _merge_candidates(cands: list[dict]) -> list[dict]:
    """Gộp kết quả nhiều cửa sổ: trùng `source_term` thì giữ mục có `occurrences` lớn hơn (§6.4 bước 2)."""
    best: dict[str, dict] = {}
    for c in cands:
        k = c["source_term"].casefold().strip()
        if k not in best or c["occurrences"] > best[k]["occurrences"]:
            best[k] = c
    # sắp theo độ tin cậy TĂNG dần: mục cần người quyết định nằm trên (§6.4 bước 3)
    return sorted(best.values(), key=lambda c: (c["confidence"], -c["occurrences"]))


def _label_coverage(pids: list[str], labels: list[dict]) -> dict[str, str]:
    """Nhãn cho từng pid, phủ kín: khoảng trống → `core`, chồng lấn → giữ khoảng đầu (§6.5 bước 3)."""
    index = {pid: i for i, pid in enumerate(pids)}
    out: dict[str, str] = {}
    for lab in labels:
        a, b = index.get(lab["from_pid"]), index.get(lab["to_pid"])
        if a is None or b is None:
            continue
        for i in range(min(a, b), max(a, b) + 1):
            out.setdefault(pids[i], lab["label"])
    return {pid: out.get(pid, "core") for pid in pids}


def _local_relation_target(rel: dict) -> str:
    return rel.get("target_local_id", "")


def _resolve_relation(unit: Unit, rel: dict, remap: dict[tuple[str, str], str]) -> str:
    local = _local_relation_target(rel)
    return remap.get((unit.segment_id, local), local)


def _glossary_terms_in(text: str, glossary: list[dict]) -> set[str]:
    lowered = text.casefold()
    return {e["source_term"] for e in glossary if e["source_term"].casefold() in lowered}


def check_plan(plan: dict, active: list[Unit], budget: int) -> list[str]:
    """Kiểm tra kế hoạch tất định (SPEC §6.6 bước 3). Trả danh sách lỗi bằng tiếng Việt."""
    problems: list[str] = []
    ids = {u.id for u in active}
    core = {u.id for u in active if u.importance == "core"}
    assigned: dict[str, str] = {}
    merged_keep: set[str] = set()
    merged_all: set[str] = set()
    for g in plan.get("merged_groups", []):
        merged_keep.add(g["keep_unit_id"])
        merged_all.update(g["merged_unit_ids"])
    seen: dict[str, str] = {}
    for s in plan["sections"]:
        for uid in s["unit_ids"]:
            if uid not in ids:
                problems.append(f"{s['id']}: unit {uid} không tồn tại")
            if uid in seen and s["kind"] == "body" and seen[uid] != s["id"]:
                problems.append(f"unit {uid} nằm ở hai mục body ({seen[uid]}, {s['id']})")
            seen[uid] = s["id"]
            assigned[uid] = s["id"]
    for uid in sorted(core):
        if uid not in assigned and uid not in merged_keep:
            problems.append(f"unit core {uid} không được gán vào mục nào và không nằm trong merged_groups")
    for g in plan.get("merged_groups", []):
        if g["keep_unit_id"] and g["keep_unit_id"] not in assigned and g["keep_unit_id"] not in merged_all:
            problems.append(f"unit giữ lại {g['keep_unit_id']} của merged_groups chưa được gán")
    if not plan["sections"] or plan["sections"][0]["kind"] != "summary":
        problems.append("mục đầu tiên phải có kind = summary")
    if not (4 <= len(plan["sections"]) <= 20):
        problems.append(f"số mục phải trong 4-20, đang có {len(plan['sections'])}")
    total = sum(int(s["target_words"]) for s in plan["sections"])
    if budget and not (0.85 * budget <= total <= 1.15 * budget):
        problems.append(f"tổng target_words = {total}, phải trong ±15% ngân sách {budget}")
    for o in plan.get("omitted", []):
        if o["unit_id"] not in ids:
            problems.append(f"omitted có unit không tồn tại: {o['unit_id']}")
    return problems


def autofix_plan(plan: dict, active: list[Unit], budget: int) -> dict:
    """Sửa tất định khi P3 vẫn sai sau một lần chạy lại (§6.6 bước 3)."""
    plan = json.loads(json.dumps(plan))  # bản sao sâu
    ids = {u.id for u in active}
    core = {u.id for u in active if u.importance == "core"}
    topics = {u.id: set(u.topics) for u in active}
    assigned = {uid for s in plan["sections"] for uid in s["unit_ids"] if uid in ids}
    merged_keep = {g["keep_unit_id"] for g in plan.get("merged_groups", [])}
    merged_all = {uid for g in plan.get("merged_groups", []) for uid in g["merged_unit_ids"]}
    body = [s for s in plan["sections"] if s["kind"] == "body"] or plan["sections"]
    for uid in sorted(core - assigned - merged_keep):
        best, best_score = body[-1], -1.0
        for s in body:
            overlap = len(topics.get(uid, set()) & {t for other in s["unit_ids"] for t in topics.get(other, set())})
            score = overlap + (1.0 if s is body[-1] else 0.0)
            if score > best_score:
                best, best_score = s, score
        best["unit_ids"].append(uid)
        assigned.add(uid)
    for uid in sorted(merged_keep - assigned - merged_all):
        body[-1]["unit_ids"].append(uid)
    for s in plan["sections"]:
        s["unit_ids"] = [u for u in s["unit_ids"] if u in ids]
    if plan["sections"] and plan["sections"][0]["kind"] != "summary":
        plan["sections"][0]["kind"] = "summary"
    targets = _fit_target_words([int(s["target_words"]) for s in plan["sections"]], budget)
    for s, tw in zip(plan["sections"], targets, strict=True):
        s["target_words"] = tw
    return plan


def _fit_target_words(targets: list[int], budget: int) -> int:  # noqa: D401 - trả về danh sách, xem docstring
    """Nắn tổng `target_words` vào dải ±15% ngân sách mà mỗi mục vẫn ≥ 40 (sàn của `report_plan.schema.json`).

    `autofix_plan` phải cho ra kế hoạch qua được `check_plan` (§6.6 bước 3), nên không được để sàn 60 cũ
    đẩy tổng vượt trần khi ngân sách nhỏ và có nhiều mục.
    """
    n = max(1, len(targets))
    weights = [max(1, t) for t in targets] or [1]
    wsum = sum(weights)
    lo, hi = 0.85 * budget, 1.15 * budget
    out = [max(40, int(round(budget * w / wsum))) for w in weights]
    total = sum(out)
    while total > hi and any(t > 40 for t in out):
        i = max(range(n), key=lambda k: (out[k], -k))
        out[i] -= 1
        total -= 1
    if total < lo:
        out[max(range(n), key=lambda k: (weights[k], -k))] += int(lo - total) + 1
    return out


def _write_problems(
    blocks: list[Block], section: Section, units: list[dict], first_use: set[str], glossary: list[GlossaryEntry]
) -> list[str]:
    """Kiểm tra sau P4 (SPEC §6.7): cites ⊆ unit của mục, core được trích dẫn, HTML thô, độ dài."""
    problems: list[str] = []
    unit_ids = {u["id"] for u in units}
    core_ids = {u["id"] for u in units if u["importance"] == "core"}
    cited: set[str] = set()
    for b in blocks:
        bad = [c for c in b.cites if c not in unit_ids]
        if bad:
            problems.append(f"{b.block_id}: trích dẫn ngoài mục: {', '.join(bad)}")
        cited.update(b.cites)
        if re.search(r"</?(?:div|span|script|style)\b", b.markdown_vi, re.I):
            problems.append(f"{b.block_id}: chứa HTML thô")
    missing_core = sorted(core_ids - cited)
    if missing_core:
        problems.append(f"unit core chưa được trích dẫn: {', '.join(missing_core[:6])}")
    if not blocks:
        problems.append(f"{section.id}: không có khối nào")
    words = sum(len(b.markdown_vi.split()) for b in blocks)
    if section.target_words and not (0.65 <= words / section.target_words <= 1.35):
        problems.append(f"độ dài {words} từ lệch mục tiêu {section.target_words} từ quá ±35%")
    return problems


def _merge_issues(det: list[dict], llm: list[dict]) -> list[dict]:
    out = [dict(i, source="code") for i in det]
    out += [dict(i, from_llm=True, source="llm") for i in llm]
    return out


def _doc_numbers(ext: Extraction) -> set[str]:
    from visynth.checks.numbers import extract_numbers

    return set(extract_numbers(ext.full_text()))


def _neighbour_passages(pids: set[str], paragraphs: list[Paragraph]) -> str:
    """Các đoạn nguồn được trích dẫn ± 1 đoạn lân cận (§6.8 P5)."""
    index = {p.pid: i for i, p in enumerate(paragraphs)}
    keep: set[int] = set()
    for pid in pids:
        i = index.get(pid)
        if i is None:
            continue
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(paragraphs):
                keep.add(j)
    return format_segment_text([{"pid": paragraphs[i].pid, "text": paragraphs[i].content} for i in sorted(keep)])


def _evidence_of(unit_id: str, units: list[Unit]) -> list[dict]:
    for u in units:
        if u.id == unit_id:
            return u.evidence
    return []
