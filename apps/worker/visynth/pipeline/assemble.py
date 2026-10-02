"""Giai đoạn `assemble` (P8) — SPEC §6.11: thống kê, ghi chú phạm vi, ghép Markdown, xếp hạng."""

from __future__ import annotations

from collections import Counter

from visynth.checks.report import count_term_usage, facts_table, glossary_appendix
from visynth.pipeline.models import JobResult

P8_FALLBACK_NOTE = (
    "## Phạm vi & cách xử lý\n\n"
    "Đây là báo cáo tổng hợp và phân tích, **không phải bản dịch nguyên văn** của tài liệu nguồn. "
    "Các ý cốt lõi được giữ đầy đủ; phần ví dụ, hỏi-đáp và lan man được cô đọng hoặc lược bớt. "
    "Mọi trích dẫn được đánh số theo thứ tự xuất hiện và tra được ở mục *Nguồn trích dẫn*."
)


def build_stats(result: JobResult, metrics: dict, source_words: int, labels: dict[str, int]) -> dict:
    """Số liệu do CODE tính, không để model bịa (SPEC §6.11 bước 1)."""
    omitted = Counter(u.omitted_reason or "level_policy" for u in result.units if u.state == "omitted")
    return {
        **metrics,
        "source_words": source_words,
        "report_words": len(result.markdown.split()) if result.markdown else 0,
        "units_supporting": sum(1 for u in result.units if u.importance == "supporting"),
        "units_minor": sum(1 for u in result.units if u.importance == "minor"),
        "units_merged": sum(1 for u in result.units if u.state == "merged"),
        "units_omitted": sum(1 for u in result.units if u.state == "omitted"),
        "omitted_by_reason": dict(omitted),
        "sections": len(result.sections),
        "blocks_flagged": sum(1 for b in result.blocks if b.flagged),
        "paragraph_labels": labels,
        "llm_calls": result.stats_llm.calls,
        "llm_tokens_in": result.stats_llm.tokens_in,
        "llm_tokens_out": result.stats_llm.tokens_out,
    }


def core_topics(result: JobResult, limit: int = 10) -> list[str]:
    """10 chủ đề có nhiều unit core nhất (§6.11 bước 2)."""
    c = Counter(t for u in result.units if u.importance == "core" and u.state == "active" for t in u.topics)
    return [t for t, _ in c.most_common(limit)]


def condensed_kinds(result: JobResult) -> list[dict]:
    """Loại nội dung bị cô đọng/lược bỏ, kèm số unit và cách xử lý."""
    kinds = Counter(u.type for u in result.units if u.state in ("omitted", "merged"))
    return [
        {
            "kind": k,
            "units": n,
            "treatment": "cô đọng" if any(u.type == k and u.state == "merged" for u in result.units) else "lược bỏ",
        }
        for k, n in kinds.most_common()
    ]


def scope_note(pipeline, stats: dict, topics: list[str], kinds: list[dict]) -> str:
    """P8 — văn bản tự do, mọi con số lấy từ `stats` do code tính; lỗi thì dùng đoạn dự phòng."""
    variables = {
        "level": pipeline.options.level,
        "stats_json": stats,
        "core_topics": topics,
        "condensed_kinds": kinds,
        "style_core": pipeline._style(),
    }
    try:
        text, _ = pipeline.call("P8", variables)
        return str(text).strip()
    except Exception as exc:  # LLM lỗi -> không làm hỏng job, dùng ghi chú dự phòng
        pipeline.result.warnings.append("scope_note_fallback")
        pipeline.result.emit("warning", code="scope_note_fallback", detail=str(exc)[:160])
        return P8_FALLBACK_NOTE


def finalize(pipeline) -> JobResult:
    """Ghép báo cáo, tính thống kê và hạng; trả `JobResult` hoàn chỉnh."""
    result = pipeline.result
    metrics = pipeline.metrics()
    labels = Counter()
    for seg in pipeline.segments:
        labels.update(getattr(seg, "labels", {}).values())
    stats = build_stats(result, metrics, source_words=pipeline.ext.word_count, labels=dict(labels))
    pipeline._scope_text = scope_note(pipeline, stats, core_topics(result), condensed_kinds(result))
    cites = _number_citations(pipeline)
    result.markdown = _build_markdown(pipeline, stats, cites)
    # P8 cũng là một lời gọi LLM — cập nhật sổ sau khi gọi xong
    stats["report_words"] = len(result.markdown.split())
    stats["llm_calls"] = result.stats_llm.calls
    stats["llm_tokens_in"] = result.stats_llm.tokens_in
    stats["llm_tokens_out"] = result.stats_llm.tokens_out
    result.stats = stats
    result.grade = metrics["grade"]
    result.metrics = metrics
    result.emit("job_succeeded", grade=result.grade, coverage_core=metrics["coverage_core"])
    return result


# ------------------------------------------------------------------ dựng Markdown


def _number_citations(pipeline) -> dict[str, int]:
    """Đánh số pid theo thứ tự xuất hiện trong báo cáo (§6.11 bước 3)."""
    units = {u.id: u for u in pipeline.result.units}
    numbers: dict[str, int] = {}
    for b in pipeline.result.blocks:
        if b.removed:
            continue
        for c in b.cites:
            u = units.get(c)
            if not u:
                continue
            for ev in u.evidence:
                numbers.setdefault(ev["pid"], len(numbers) + 1)
    return numbers


def _block_cites(block, pipeline, numbers: dict[str, int]) -> str:
    units = {u.id: u for u in pipeline.result.units}
    pids: list[str] = []
    for c in block.cites:
        u = units.get(c)
        if u:
            pids.extend(ev["pid"] for ev in u.evidence)
    seen: list[int] = []
    for pid in pids:
        n = numbers.get(pid)
        if n and n not in seen:
            seen.append(n)
    return ", ".join(f"[{n}]" for n in sorted(seen))


def _build_markdown(pipeline, stats: dict, numbers: dict[str, int]) -> str:
    result = pipeline.result
    plan = result.plan or {}
    lines: list[str] = []
    lines.append(f"# {plan.get('report_title_vi') or result.title}")
    if plan.get("report_subtitle_vi"):
        lines.append(f"*{plan['report_subtitle_vi']}*")
    lines += [
        "",
        "> **Báo cáo do AI tổng hợp.** Hãy kiểm tra lại ở nguồn gốc trước khi dùng cho quyết định quan trọng. "
        "Đây là bản tổng hợp/đọc hiểu, không phải bản dịch nguyên văn.",
        "",
        f"**Nguồn:** {result.title} · **Mức:** {result.level} · **Hạng chất lượng:** {stats.get('grade', 'C')}",
        "",
        "## Mục lục",
    ]
    lines += [f"- {s.title_vi}" for s in result.sections]
    lines.append("")
    for s in result.sections:
        lines += [f"## {s.title_vi}", ""]
        blocks = result.blocks_of(s.id)
        if not blocks:
            lines += ["*(mục này không còn nội dung sau khi kiểm chứng)*", ""]
            continue
        for b in blocks:
            if b.flagged:
                lines.append("> ⚠️ *Khối này còn điểm chưa kiểm chứng được với nguồn.*")
            lines += [b.markdown_vi.strip(), ""]
            cite = _block_cites(b, pipeline, numbers)
            if cite:
                lines += [f"<sub>Nguồn: {cite}</sub>", ""]
    if facts := _facts_section(pipeline):
        lines += ["## Bảng dữ kiện", "", facts, ""]
    used = count_term_usage("\n".join(b.markdown_vi for b in result.blocks if not b.removed), pipeline.glossary_entries)
    if used and plan.get("include_glossary_appendix", True):
        lines += ["## Phụ lục thuật ngữ", "", glossary_appendix(used), ""]
    lines += [pipeline._scope_text.strip(), "", "## Nguồn trích dẫn", ""]
    for pid, n in sorted(numbers.items(), key=lambda kv: kv[1]):
        text = pipeline.paragraph_text.get(pid, "")
        lines.append(f"{n}. **{pid}** — {_snippet(text)}")
    lines.append("")
    return "\n".join(lines)


def _facts_section(pipeline) -> str:
    """Bảng dữ kiện dựng TẤT ĐỊNH từ unit `fact_data` (§6.6 bước 5)."""
    if pipeline.result.plan.get("include_facts_table") is False:
        return ""
    wanted = set(pipeline.result.plan.get("facts_unit_ids") or [])
    facts = [
        u.as_dict() for u in pipeline.result.units if u.state == "active" and (u.type == "fact_data" or u.id in wanted)
    ]
    return facts_table(facts[:40]) if facts else ""


def _snippet(text: str, limit: int = 180) -> str:
    t = " ".join(text.split())
    return t if len(t) <= limit else t[: limit - 1] + "…"
