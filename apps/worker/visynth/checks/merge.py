"""Gộp đề xuất thuật ngữ từ nhiều tài liệu mẫu — port của `docs/reference/glossary_merge.py` (SPEC §19.5).

Dùng ở đường curation (P13) — chưa nằm trong pipeline M0-W2 nhưng cần sẵn cho M1.
"""

from __future__ import annotations

import re
from collections import defaultdict

from visynth.textnorm import nfc


def _key(term: str) -> str:
    return re.sub(r"\s+", " ", nfc(term)).strip().casefold()


def merge_candidates(
    per_doc: dict[str, list[dict]], contexts: dict[tuple[str, str], list[str]] | None = None, max_ctx: int = 2
) -> list[dict]:
    """per_doc: {doc_ref: [candidate P1,...]} (glossary_candidates.schema.json). contexts: {(doc_ref, source_term): [đoạn nguồn ngắn,...]}.

    Trả về danh sách đã gộp theo source_term (không phân biệt hoa/thường), mỗi mục có: docs_count, occurrences, variants
    (các target_term khác nhau kèm số tài liệu đề xuất), avg_confidence, conflict (>= 2 target khác nhau), ctx (ctx_id -> đoạn).
    Sắp xếp: xung đột và độ tin cậy thấp lên trước (người duyệt cần quyết định những mục này đầu tiên)."""
    contexts = contexts or {}
    by: dict[str, dict] = {}
    for doc, cands in per_doc.items():
        for c in cands:
            k = _key(c["source_term"])
            m = by.setdefault(
                k,
                dict(
                    source_term=c["source_term"],
                    term_type=c["term_type"],
                    docs=set(),
                    occurrences=0,
                    conf=[],
                    variants=defaultdict(set),
                    keep_original=[],
                    alternatives=set(),
                    ctx=[],
                ),
            )
            m["docs"].add(doc)
            m["occurrences"] += c["occurrences"]
            m["conf"].append(c["confidence"])
            m["variants"][nfc(c["target_term"]).strip()].add(doc)
            m["keep_original"].append(c["keep_original"])
            m["alternatives"].update(nfc(a).strip() for a in c.get("alternatives", []))
            for snip in contexts.get((doc, c["source_term"]), [])[:max_ctx]:
                if len(m["ctx"]) < max_ctx * 2:
                    m["ctx"].append((doc, snip))
    out = []
    for i, (_k, m) in enumerate(sorted(by.items()), 1):
        variants = sorted(((t, len(d)) for t, d in m["variants"].items()), key=lambda x: (-x[1], x[0]))
        out.append(
            dict(
                source_term=m["source_term"],
                term_type=m["term_type"],
                docs_count=len(m["docs"]),
                occurrences=m["occurrences"],
                avg_confidence=round(sum(m["conf"]) / len(m["conf"]), 3),
                variants=[dict(target_term=t, docs=n) for t, n in variants],
                keep_original_votes=dict(
                    true=sum(m["keep_original"]), false=len(m["keep_original"]) - sum(m["keep_original"])
                ),
                alternatives=sorted(m["alternatives"] - {v[0] for v in variants})[:5],
                conflict=len(variants) > 1,
                ctx=[dict(ctx_id=f"C{i:03d}.{j + 1}", doc_ref=d, snippet=s) for j, (d, s) in enumerate(m["ctx"])],
            )
        )
    out.sort(key=lambda e: (not e["conflict"], e["avg_confidence"], -e["docs_count"], e["source_term"].casefold()))
    return out


def attach_evidence(entries: list[dict], merged: list[dict]) -> list[dict]:
    """Ghép bằng chứng thật vào đề xuất của P13: model chỉ trả ctx_ids; code tra lại đoạn trích. ctx_id lạ bị loại (không tin model)."""
    ctx = {c["ctx_id"]: c for m in merged for c in m["ctx"]}
    out = []
    for e in entries:
        ev = [dict(doc_ref=ctx[i]["doc_ref"], snippet=ctx[i]["snippet"]) for i in e.get("ctx_ids", []) if i in ctx]
        x = {k: v for k, v in e.items() if k != "ctx_ids"}
        x["evidence"] = ev
        out.append(x)
    return out
