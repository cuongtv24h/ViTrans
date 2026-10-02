"""Khởi tạo Lõi văn phong bằng đề xuất của AI (§19.4, §19.9 bước 1–3).

Điểm mấu chốt: **code cưỡng chế kỷ luật bằng chứng, không tin lời model** (§19.4 điểm 5).

* Đầu vào (trích mẫu, cặp tham chiếu, phản hồi, brief) được gán ID `S01…`, `REF01…`, `FB01…`, `B01`;
  model chỉ được **trỏ tới ID**, tự viết trích dẫn là vô hiệu — `resolve_evidence()` tra lại nội dung thật và
  **loại mọi ID lạ**.
* Quy tắc/ví dụ không có bằng chứng hợp lệ bị **loại**; ví dụ không chép nguyên văn từ một cặp tham chiếu bị loại.
* Mọi mục do AI tạo bị ép `origin = "ai"`, `reviewed = false`; `lint` chạy lại và mục mắc lỗi bị loại.
* Trường nhận diện (`name_vi`, `domain`, `glossary_refs`, `limits`) do NGƯỜI đặt, không lấy từ model.
* Kết quả luôn là **bản nháp** với `open_decisions` = `decisions_needed` — AI không bao giờ tự duyệt được gì.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from visynth.prompts import Prompt, default_prompts_dir, default_schemas_dir, render_prompt
from visynth.structured import SchemaStore
from visynth.stylecore.core import lint

PROMPT_ID = "P12"
PROPOSAL_PROMPT = "P12_style_core_proposer"
DEFAULT_LIMITS = {"compiled_max_chars": 6000}
_ID_RE = re.compile(r"^(?:S|REF|FB)[0-9]{2}$")
_CORE_IDENTITY = ("schema_version", "locale")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _clip(text: str, n: int) -> str:
    text = _clean(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------ đầu vào


def excerpts_from_texts(docs: dict[str, str], *, per_doc: int = 3, chars: int = 1200) -> list[dict]:
    """Trích đầu/giữa/cuối mỗi tài liệu mẫu (P12 nhận tối đa 10 tài liệu, mỗi đoạn có ID `S..`).

    `docs`: {doc_ref: nội dung}. Trả `[{'id': 'S01', 'doc_ref': ..., 'text': ...}]` — ID do CODE cấp,
    model không tự đặt, nên `source_ref` luôn tra ngược được.
    """
    out: list[dict] = []
    for ref, text in docs.items():
        text = text.strip()
        if not text:
            continue
        if per_doc <= 1 or len(text) <= chars:
            windows = [text]
        else:
            span = max(1, len(text) // per_doc)
            windows = []
            for i in range(per_doc):
                start = min(i * span, max(0, len(text) - chars))
                windows.append(text[start : start + chars].strip())
        for window in windows:
            if window:
                out.append({"id": f"S{len(out) + 1:02d}", "doc_ref": ref, "text": window})
        if len(out) >= 30:
            break
    return out


def normalise_pairs(pairs: list[dict]) -> list[dict]:
    """Cặp tham chiếu của người duyệt → `[{'id': 'REF01', 'source':…, 'target':…, 'note_vi':…}]`."""
    out = []
    for p in pairs:
        source, target = _clean(p.get("source", "")), _clean(p.get("target", ""))
        if not source or not target:
            continue
        out.append({"source": source, "target": target, "note_vi": _clip(p.get("note_vi", ""), 200)})
    for i, item in enumerate(out, 1):
        item["id"] = f"REF{i:02d}"
    return out


def build_index(
    *, brief: str, samples: list[dict], pairs: list[dict], feedback: list[dict] | None = None
) -> dict[str, dict]:
    """Chỉ mục ID hợp lệ → bằng chứng thật, dùng để tra lại và loại ID lạ."""
    index: dict[str, dict] = {}
    if brief.strip():
        index["B01"] = {"kind": "brief", "text": _clip(brief, 400), "id": "B01"}
    for s in samples:
        index[s["id"]] = {
            "kind": "sample_excerpt",
            "text": _clip(s["text"], 400),
            "id": s["id"],
            "doc_ref": s.get("doc_ref"),
        }
    for p in pairs:
        index[p["id"]] = {
            "kind": "reference_pair",
            "text": _clip(f"{p['source']} => {p['target']}", 400),
            "id": p["id"],
            "source": p["source"],
            "target": p["target"],
        }
    for f in feedback or []:
        index[f["id"]] = {"kind": "feedback", "text": _clip(f.get("text", ""), 400), "id": f["id"]}
    return index


def resolve_evidence(raw: list[dict], index: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """Tra lại nội dung từ `source_ref`; loại mọi mục có ID lạ (§19.4 điểm 1)."""
    resolved, dropped = [], []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        target, ref = str(e.get("target_id", "")), str(e.get("source_ref", ""))
        kind = e.get("kind")
        if kind == "brief" and "B01" in index:
            entry = index["B01"]
        elif _ID_RE.match(ref) and ref in index and index[ref]["kind"] == kind:
            entry = index[ref]
        else:
            dropped.append(f"{target or '?'}: bằng chứng trỏ tới '{ref}' không có trong đầu vào")
            continue
        resolved.append(
            {
                "target_id": target,
                "kind": kind,
                "source_ref": entry["id"],
                "note_vi": _clip(e.get("note_vi", ""), 200),
                "excerpt": entry["text"],
            }
        )
    return resolved, dropped


# ------------------------------------------------------------------ làm sạch đề xuất


@dataclass
class Proposal:
    """Kết quả đã qua kiểm duyệt của code; `notes` nói rõ đã loại gì và vì sao."""

    content: dict
    decisions: list[dict]
    evidence: list[dict]
    summary_vi: str
    risks_vi: list[str]
    confidence: float
    notes: list[str] = field(default_factory=list)
    raw: dict | None = None


def _numbered(items: list[dict], prefix: str) -> list[dict]:
    out = []
    for i, item in enumerate(items, 1):
        item = copy.deepcopy(item)
        item["id"] = f"{prefix}{i:02d}"
        out.append(item)
    return out


def sanitize_proposal(
    raw: dict,
    *,
    index: dict[str, dict],
    name_vi: str,
    domain: str,
    glossary_refs: list[dict] | None = None,
    limits: dict | None = None,
) -> Proposal:
    """Áp kỷ luật §19.4 lên đầu ra thô của P12 và dựng nội dung `style_core` hợp lệ để lưu nháp."""
    notes: list[str] = []
    resolved, dropped = resolve_evidence(raw.get("evidence") or [], index)
    notes += [f"loại bằng chứng — {d}" for d in dropped]
    by_target: dict[str, list[dict]] = {}
    for e in resolved:
        by_target.setdefault(e["target_id"], []).append(e)

    proposal = raw.get("proposal") or {}
    pairs = {p["id"]: p for p in index.values() if p["kind"] == "reference_pair"}

    def ok_rule(r: dict, new_id: str) -> bool:
        text = _clean(r.get("text", ""))
        if not text or len(text) > 400:
            notes.append(f"loại quy tắc {r.get('id')}: văn bản rỗng hoặc quá 400 ký tự")
            return False
        if not by_target.get(str(r.get("id")), []):
            notes.append(f"loại quy tắc {r.get('id')}: không có bằng chứng hợp lệ")
            return False
        return True

    rules = []
    for r in proposal.get("rules") or []:
        if r.get("id") in rules:
            continue
        if ok_rule(r, str(r.get("id"))):
            rule = {
                "id": str(r.get("id")),
                "text": _clip(r.get("text", ""), 400),
                "severity": r.get("severity") if r.get("severity") in ("must", "should", "may") else "may",
                "applies_to": [
                    s
                    for s in (r.get("applies_to") or [])
                    if s in ("glossary", "map", "consolidate", "write", "translate", "repair", "assemble")
                ],
                "origin": "ai",
                "reviewed": False,
                "confidence": _clamp(r.get("confidence")),
                "rationale_vi": _clip(r.get("rationale_vi", ""), 400),
            }
            rules.append(rule)
    rules = _numbered(rules, "R")[:12]

    exemplars = []
    for e in proposal.get("exemplars") or []:
        pair_id = next(
            (b["source_ref"] for b in by_target.get(str(e.get("id")), []) if b["kind"] == "reference_pair"), None
        )
        pair = pairs.get(pair_id or "")
        if pair is None:
            notes.append(f"loại ví dụ {e.get('id')}: không có cặp tham chiếu làm bằng chứng")
            continue
        if _clean(e.get("source", "")) != pair["source"] or _clean(e.get("target", "")) != pair["target"]:
            notes.append(
                f"loại ví dụ {e.get('id')}: không chép nguyên văn cặp {pair['id']} (model không được tự viết ví dụ)"
            )
            continue
        exemplars.append(
            {
                "id": str(e.get("id")),
                "source": pair["source"],
                "target": pair["target"],
                "note_vi": _clip(e.get("note_vi", ""), 300),
                "applies_to": [
                    s for s in (e.get("applies_to") or []) if s in ("write", "translate", "repair", "assemble")
                ],
                "origin": "ai",
                "reviewed": False,
            }
        )
    exemplars = _numbered(exemplars, "E")

    voice = {
        "register": (proposal.get("voice") or {}).get("register", "unspecified"),
        "notes_vi": _clip((proposal.get("voice") or {}).get("notes_vi", ""), 300),
    }
    if voice["register"] not in ("unspecified", "formal", "neutral", "conversational", "academic"):
        voice["register"] = "unspecified"
    content = {
        "schema_version": "1",
        "name_vi": name_vi,
        "summary_vi": _clip(raw.get("summary_vi", ""), 800),
        "domain": domain,
        "locale": "vi",
        "voice": voice,
        "terminology_policy": _policy(proposal.get("terminology_policy") or {}),
        "formatting": _formatting(proposal.get("formatting") or {}),
        "rules": rules,
        "exemplars": exemplars,
        "glossary_refs": glossary_refs or [],
        "limits": dict(limits or DEFAULT_LIMITS),
    }

    content, lint_notes = _drop_lint_errors(content)
    notes += lint_notes

    decisions = []
    for d in raw.get("decisions_needed") or []:
        item = _decision(d)
        if item is None:
            notes.append(
                f"loại quyết định {d.get('id') if isinstance(d, dict) else d!r}: thiếu câu hỏi hoặc không đủ 2 phương án"
            )
            continue
        decisions.append(item)

    return Proposal(
        content=content,
        decisions=decisions,
        evidence=[{k: e[k] for k in ("target_id", "kind", "source_ref", "note_vi", "excerpt")} for e in resolved],
        summary_vi=_clip(raw.get("summary_vi", ""), 800),
        risks_vi=[_clip(r, 200) for r in (raw.get("risks_vi") or [])][:8],
        confidence=_clamp(raw.get("confidence")) or 0.0,
        notes=notes,
        raw=raw,
    )


def _clamp(value: Any) -> float | None:
    try:
        return round(min(1.0, max(0.0, float(value))), 3)
    except (TypeError, ValueError):
        return None


def _policy(raw: dict) -> dict:
    return {
        "first_use": raw.get("first_use")
        if raw.get("first_use") in ("target_with_original", "target_only", "original_only")
        else "unspecified",
        "unknown_terms": raw.get("unknown_terms")
        if raw.get("unknown_terms") in ("flag_for_review", "translate_with_original", "keep_original")
        else "unspecified",
        "proper_names": raw.get("proper_names")
        if raw.get("proper_names") in ("keep_original", "translate_known_forms")
        else "unspecified",
        "notes_vi": _clip(raw.get("notes_vi", ""), 300),
    }


def _formatting(raw: dict) -> dict:
    return {
        "quotes": raw.get("quotes") if raw.get("quotes") in ("straight", "curly", "unspecified") else "unspecified",
        "lists": raw.get("lists")
        if raw.get("lists") in ("bullets", "numbers", "prose", "unspecified")
        else "unspecified",
        "numbers": raw.get("numbers")
        if raw.get("numbers") in ("keep_source", "vietnamese_words", "unspecified")
        else "unspecified",
        "notes_vi": _clip(raw.get("notes_vi", ""), 300),
    }


def _decision(raw: dict) -> dict | None:
    question = _clip(raw.get("question_vi", ""), 300)
    options = [
        {"label": _clip(o.get("label", ""), 80), "effect_vi": _clip(o.get("effect_vi", ""), 200)}
        for o in (raw.get("options") or [])
        if isinstance(o, dict) and _clean(o.get("label", ""))
    ]
    if not question or len(options) < 2:
        return None
    labels = [o["label"] for o in options]
    return {
        "id": str(raw.get("id", "")),
        "question_vi": question,
        "question": question,
        "options": options[:5],
        "recommended": raw.get("recommended") if raw.get("recommended") in labels else None,
        "why_vi": _clip(raw.get("why_vi", ""), 300),
    }


def _drop_lint_errors(content: dict) -> tuple[dict, list[str]]:
    """Lint nội dung; mục nào mắc lỗi thì loại mục đó (cảnh báo thì giữ) — §19.4 điểm 5."""
    notes: list[str] = []
    for _ in range(3):  # vài vòng vì loại mục này có thể lộ lỗi ở mục khác
        problems = [p for p in lint(content) if p.severity == "error"]
        if not problems:
            break
        for p in problems:
            m = re.match(r"(rules|exemplars)\[([^\]]+)\]", p.path)
            if m:
                coll, item_id = m.group(1), m.group(2)
                content[coll] = [i for i in content[coll] if str(i.get("id")) != item_id]
                notes.append(f"loại {coll}[{item_id}]: lint báo lỗi ({p.message})")
            elif p.path.endswith("notes_vi"):
                for holder in (
                    content.get("voice", {}),
                    content.get("terminology_policy", {}),
                    content.get("formatting", {}),
                ):
                    holder["notes_vi"] = ""
                notes.append(f"xoá {p.path}: lint báo lỗi ({p.message})")
            else:
                notes.append(f"lint còn lỗi ở {p.path}: {p.message}")
    return content, notes


# ------------------------------------------------------------------ gọi P12


def render_p12(
    *,
    brief: str,
    samples: list[dict],
    pairs: list[dict],
    existing_core: dict | None = None,
    glossary_digest: list[dict] | None = None,
    feedback: list[dict] | None = None,
    prompts_dir: Path | None = None,
) -> Prompt:
    """Render P12 — dữ liệu không tin cậy nằm trong thẻ, biến khớp đúng khai báo của prompt."""
    sample_text = "\n\n".join(f"[{s['id']}] ({s.get('doc_ref', '')}) {s['text']}" for s in samples)
    variables = {
        "mode": "refine" if existing_core else "bootstrap",
        "domain_brief": brief,
        "sample_excerpts": sample_text,
        "reference_pairs_json": json.dumps(
            [{"id": p["id"], "source": p["source"], "target": p["target"], "note_vi": p["note_vi"]} for p in pairs],
            ensure_ascii=False,
            indent=1,
        ),
        "existing_core_json": json.dumps(existing_core or {}, ensure_ascii=False, indent=1),
        "glossary_digest_json": json.dumps(glossary_digest or [], ensure_ascii=False, indent=1),
        "feedback_digest_json": json.dumps(feedback or [], ensure_ascii=False, indent=1),
    }
    return render_prompt(prompts_dir or default_prompts_dir(), PROMPT_ID, variables)


def empty_digest(items: list[dict] | None) -> list[dict]:
    return items or []


def proposal_from_reply(reply: Any) -> dict:
    if isinstance(reply, dict):
        return reply
    return json.loads(reply)


def demo_proposal(samples: list[dict], pairs: list[dict], brief: str) -> dict:
    """Đề xuất giả để chạy khô offline: chỉ dùng bằng chứng CÓ THẬT trong đầu vào, phần còn lại là câu hỏi."""
    evidence = [{"target_id": "R01", "kind": "brief", "source_ref": "B01", "note_vi": "Brief của người phụ trách."}]
    if pairs:
        evidence.append(
            {
                "target_id": "R02",
                "kind": "reference_pair",
                "source_ref": pairs[0]["id"],
                "note_vi": "Cặp tham chiếu đầu tiên.",
            }
        )
    if samples:
        evidence.append(
            {
                "target_id": "R02",
                "kind": "sample_excerpt",
                "source_ref": samples[0]["id"],
                "note_vi": "Đoạn mẫu đầu tiên.",
            }
        )
    exemplars = []
    if pairs:
        exemplars.append(
            {
                "id": "E01",
                "source": pairs[0]["source"],
                "target": pairs[0]["target"],
                "note_vi": "Chép nguyên văn từ cặp tham chiếu.",
                "applies_to": ["write", "translate"],
            }
        )
        evidence.append(
            {"target_id": "E01", "kind": "reference_pair", "source_ref": pairs[0]["id"], "note_vi": "Nguồn của ví dụ."}
        )
    return {
        "mode": "refine" if False else "bootstrap",
        "summary_vi": "ĐỀ XUẤT GIẢ (chạy khô, không gọi LLM): minh hoạ đường ống kỷ luật bằng chứng.",
        "proposal": {
            "voice": {"register": "unspecified", "notes_vi": ""},
            "terminology_policy": {
                "first_use": "unspecified",
                "unknown_terms": "unspecified",
                "proper_names": "unspecified",
                "notes_vi": "",
            },
            "formatting": {"quotes": "unspecified", "lists": "unspecified", "numbers": "unspecified", "notes_vi": ""},
            "rules": [
                {
                    "id": "R01",
                    "text": _clip(_first_sentence(brief) or "Giữ giọng phù hợp với người đọc đã nêu trong brief.", 400),
                    "severity": "should",
                    "applies_to": ["write", "translate"],
                    "confidence": 0.5,
                    "rationale_vi": "Suy ra từ brief của người phụ trách.",
                },
                {
                    "id": "R02",
                    "text": "Thuật ngữ lĩnh vực trình bày nhất quán với cách các cặp tham chiếu đang dùng.",
                    "severity": "should",
                    "applies_to": ["write", "translate"],
                    "confidence": 0.55,
                    "rationale_vi": "Suy ra từ mẫu và cặp tham chiếu.",
                },
            ],
            "exemplars": exemplars,
        },
        "decisions_needed": [
            {
                "id": "D01",
                "question_vi": "Cách xưng hô với người đọc nên chọn thế nào?",
                "options": [
                    {"label": "Trung tính (không xưng)", "effect_vi": "An toàn cho mọi đối tượng, ít thân mật."},
                    {"label": "Gọi 'bạn'", "effect_vi": "Gần gũi hơn, hợp văn nói."},
                ],
                "recommended": None,
                "why_vi": "Đầu vào không đủ căn cứ để chọn; cần người duyệt quyết định.",
            }
        ],
        "evidence": evidence,
        "risks_vi": ["Chạy khô nên bằng chứng chỉ để minh hoạ đường ống, không phải đề xuất thật."],
        "confidence": 0.2,
    }


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s+", _clean(text))[0] if text.strip() else ""


def propose(
    client,
    *,
    brief: str,
    samples: list[dict],
    pairs: list[dict],
    feedback: list[dict] | None = None,
    glossary_digest: list[dict] | None = None,
    existing_core: dict | None = None,
    name_vi: str,
    domain: str,
    glossary_refs: list[dict] | None = None,
    limits: dict | None = None,
    schemas: SchemaStore | None = None,
    prompts_dir: Path | None = None,
) -> Proposal:
    """Gọi P12 (hoặc dựng đề xuất giả nếu `client is None`), rồi áp kỷ luật bằng chứng của code."""
    from visynth.llm.base import LLMRequest

    index = build_index(brief=brief, samples=samples, pairs=pairs, feedback=feedback)
    if client is None:
        raw = demo_proposal(samples, pairs, brief)
    else:
        prompt = render_p12(
            brief=brief,
            samples=samples,
            pairs=pairs,
            existing_core=existing_core,
            glossary_digest=glossary_digest,
            feedback=feedback,
            prompts_dir=prompts_dir,
        )
        store = schemas or SchemaStore(default_schemas_dir())
        req = LLMRequest(
            prompt_id="P12",
            system=prompt.system,
            user=prompt.user,
            schema=store.load("style_core_proposal.schema.json"),
            max_output_tokens=prompt.max_output_tokens,
            thinking=prompt.thinking,
            needs={"structured": "required"},
            metadata={"stage": "curate"},
        )
        resp = client.complete(req)
        raw = resp.parsed if resp.parsed is not None else json.loads(resp.text)
        store.validate(raw, req.schema or {}, what="P12")
    result = sanitize_proposal(
        raw, index=index, name_vi=name_vi, domain=domain, glossary_refs=glossary_refs, limits=limits
    )
    # kiểm tra hình dạng cuối cùng trước khi cho vào kho
    store = schemas or SchemaStore(default_schemas_dir())
    store.validate(result.content, store.load("style_core.schema.json"), what="style_core (P12)")
    return result


__all__ = [
    "PROMPT_ID",
    "PROPOSAL_PROMPT",
    "Proposal",
    "build_index",
    "demo_proposal",
    "excerpts_from_texts",
    "normalise_pairs",
    "propose",
    "proposal_from_reply",
    "render_p12",
    "resolve_evidence",
    "sanitize_proposal",
]
