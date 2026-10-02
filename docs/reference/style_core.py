"""Lõi văn phong (Style Core): kiểm tra, kế thừa, biên dịch thành khối prompt, và cổng duyệt (SPEC §19).

Nguyên tắc: spec chỉ cung cấp CƠ CHẾ. Nội dung văn phong/thuật ngữ do AI đề xuất và người duyệt (HITL) quyết định, lưu thành dữ liệu có
phiên bản. Khối prompt biên dịch ra chỉ điều chỉnh CÁCH DIỄN ĐẠT; các quy tắc bất biến (trung thực, giữ nguyên số liệu/tên/ID, schema)
nằm trong prompt hệ thống và KHÔNG thể bị lõi ghi đè.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass

STAGES = ("glossary", "map", "consolidate", "write", "translate", "repair", "assemble")
SEVERITY_ORDER = {"must": 0, "should": 1, "may": 2}
NEUTRAL_TEXT = "No stylistic preferences are configured. Write natural, idiomatic Vietnamese in the register of the source. Apply the glossary exactly."

# Heuristic chặn văn bản trong lõi cố điều khiển hành vi ngoài phạm vi diễn đạt (prompt injection qua lõi do người dùng tạo).
_FORBIDDEN = re.compile(
    r"ignore (all|any|the|previous|above)|disregard|system prompt|reveal|jailbreak|"
    r"output (only|format)|json|schema|tool call|function call|https?://|</?\s*(style_core|system|user|segment|document)\b|\{\{|\}\}",
    re.I,
)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class Problem:
    severity: str  # error | warning
    path: str
    message: str


def canonical_json(content: dict) -> str:
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha256(content: dict) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def lint(content: dict) -> list[Problem]:
    """Kiểm tra ngoài JSON Schema: id duy nhất, độ dài, ký tự lạ, văn bản có dấu hiệu cố điều khiển prompt, độ dài biên dịch."""
    out: list[Problem] = []
    for coll, key in (("rules", "text"), ("exemplars", "source")):
        ids = [x["id"] for x in content.get(coll, [])]
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            out.append(Problem("error", f"{coll}[{dup}]", "id bị trùng"))
    texts = []
    for r in content.get("rules", []):
        texts.append((f"rules[{r['id']}].text", r["text"]))
        for st in r.get("applies_to", []):
            if st not in STAGES:
                out.append(Problem("error", f"rules[{r['id']}].applies_to", f"giai đoạn không hợp lệ: {st}"))
    for e in content.get("exemplars", []):
        texts += [(f"exemplars[{e['id']}].source", e["source"]), (f"exemplars[{e['id']}].target", e["target"])]
    for k in ("summary_vi",):
        texts.append((k, content.get(k, "")))
    for sec in ("voice", "terminology_policy", "formatting"):
        texts.append((f"{sec}.notes_vi", content.get(sec, {}).get("notes_vi", "")))
    for path, t in texts:
        if _CTRL.search(t):
            out.append(Problem("error", path, "chứa ký tự điều khiển"))
        if path.startswith("rules") or path.endswith("notes_vi"):
            if _FORBIDDEN.search(t):
                out.append(Problem("error", path, "chứa cụm có dấu hiệu điều khiển prompt/định dạng đầu ra; lõi chỉ được chỉnh cách diễn đạt"))
    n = len(compile_style_core(content, "write", max_chars=10**6))
    cap = content.get("limits", {}).get("compiled_max_chars", 6000)
    if n > cap:
        out.append(Problem("warning", "limits.compiled_max_chars", f"bản biên dịch dài {n} > {cap} ký tự: một phần sẽ bị lược khi dùng"))
    return out


def approval_problems(content: dict, open_decisions: list | None = None) -> list[str]:
    """Điều kiện để duyệt (approve): mọi quy tắc/ví dụ do AI đề xuất phải được người xem (reviewed = true), không còn quyết định mở, lint sạch lỗi."""
    probs = []
    if open_decisions:
        probs.append(f"còn {len(open_decisions)} quyết định mở chưa trả lời")
    for coll in ("rules", "exemplars"):
        for x in content.get(coll, []):
            if x.get("origin") == "ai" and not x.get("reviewed"):
                probs.append(f"{coll}[{x['id']}] do AI đề xuất, chưa được người duyệt xác nhận")
    probs += [f"{p.path}: {p.message}" for p in lint(content) if p.severity == "error"]
    if not content.get("name_vi", "").strip():
        probs.append("thiếu name_vi")
    return probs


def resolve(child: dict, parent: dict | None) -> dict:
    """Kế thừa: lõi con ghi đè quy tắc/ví dụ cùng id của lõi cha; trường 'unspecified'/rỗng của con không ghi đè giá trị của cha."""
    if parent is None:
        return copy.deepcopy(child)
    out = copy.deepcopy(parent)
    out["name_vi"], out["summary_vi"], out["domain"], out["locale"] = child["name_vi"], child["summary_vi"], child["domain"], child["locale"]
    for sec in ("voice", "terminology_policy", "formatting"):
        for k, v in child.get(sec, {}).items():
            if v not in ("unspecified", "", None):
                out[sec][k] = v
    for coll in ("rules", "exemplars"):
        merged = {x["id"]: x for x in parent.get(coll, [])}
        merged.update({x["id"]: x for x in child.get(coll, [])})
        out[coll] = list(merged.values())
    refs = {r["glossary_id"]: r for r in parent.get("glossary_refs", [])}
    refs.update({r["glossary_id"]: r for r in child.get("glossary_refs", [])})
    out["glossary_refs"] = list(refs.values())
    out["limits"] = child.get("limits", parent.get("limits"))
    return out


def resolve_chain(core_id: str, lookup: dict[str, dict], max_depth: int = 5) -> dict:
    """lookup: {id: {'content': {...}, 'parent_id': id|None}}. Ném ValueError khi có vòng lặp hoặc quá sâu."""
    chain, seen, cur = [], set(), core_id
    while cur is not None:
        if cur in seen:
            raise ValueError("vòng lặp kế thừa Lõi văn phong")
        if len(chain) >= max_depth:
            raise ValueError("kế thừa quá sâu")
        seen.add(cur)
        chain.append(lookup[cur]["content"])
        cur = lookup[cur].get("parent_id")
    acc = None
    for c in reversed(chain):  # từ gốc xuống
        acc = resolve(c, acc)
    return acc


def _esc(s: str) -> str:
    return s.replace("<", "\u2039").replace(">", "\u203a").strip()


def compile_style_core(content: dict, stage: str, max_chars: int | None = None) -> str:
    """Biên dịch lõi thành văn bản chèn vào biến {{style_core}} của prompt cho một giai đoạn.

    Thứ tự LƯỢC khi vượt max_chars: quy tắc 'may' -> quy tắc 'should' -> ví dụ (giữ tối đa 2) -> ví dụ còn lại. Quy tắc 'must' và chính sách
    thuật ngữ không bao giờ bị lược; nếu vẫn vượt thì cắt các dòng ghi chú.
    """
    cap = max_chars if max_chars is not None else content.get("limits", {}).get("compiled_max_chars", 6000)
    rules = [r for r in content.get("rules", []) if not r.get("applies_to") or stage in r["applies_to"]]
    exs = [e for e in content.get("exemplars", []) if not e.get("applies_to") or stage in e["applies_to"]]

    def render(rs: list, es: list, with_notes: bool = True) -> str:
        lines: list[str] = []
        v, tp, fm = content.get("voice", {}), content.get("terminology_policy", {}), content.get("formatting", {})
        head = []
        if v.get("register", "unspecified") != "unspecified":
            head.append(f"Register: {v['register']}.")
        if with_notes and v.get("notes_vi"):
            head.append(_esc(v["notes_vi"]))
        if head:
            lines.append(" ".join(head))
        pol = [f"{k}={tp[k]}" for k in ("first_use", "unknown_terms", "proper_names") if tp.get(k) and tp[k] != "unspecified"]
        if pol:
            lines.append("Terminology policy: " + "; ".join(pol) + "." + (" " + _esc(tp["notes_vi"]) if with_notes and tp.get("notes_vi") else ""))
        fmt = [f"{k}={fm[k]}" for k in ("quotes", "lists", "numbers") if fm.get(k) and fm[k] != "unspecified"]
        if fmt:
            lines.append("Formatting: " + "; ".join(fmt) + "." + (" " + _esc(fm["notes_vi"]) if with_notes and fm.get("notes_vi") else ""))
        for sev, title in (("must", "MUST"), ("should", "SHOULD"), ("may", "MAY")):
            rr = sorted((r for r in rs if r["severity"] == sev), key=lambda r: r["id"])
            if rr:
                lines.append(f"Rules ({title}):")
                lines += [f"- [{r['id']}] {_esc(r['text'])}" for r in rr]
        if es:
            lines.append("Examples (source => target):")
            lines += [f"[{e['id']}] {_esc(e['source'])} => {_esc(e['target'])}" + (f" ({_esc(e['note_vi'])})" if with_notes and e.get("note_vi") else "") for e in es]
        return "\n".join(lines) if lines else NEUTRAL_TEXT

    text = render(rules, exs)
    if len(text) <= cap:
        return text
    steps = [
        lambda: ([r for r in rules if r["severity"] != "may"], exs),
        lambda: ([r for r in rules if r["severity"] == "must"], exs),
        lambda: ([r for r in rules if r["severity"] == "must"], exs[:2]),
        lambda: ([r for r in rules if r["severity"] == "must"], []),
    ]
    for step in steps:
        rs, es = step()
        text = render(rs, es)
        if len(text) <= cap:
            return text
    return render([r for r in rules if r["severity"] == "must"], [], with_notes=False)


def decisions_open(proposal_decisions: list, answers: dict[str, str]) -> list:
    """Quyết định (decisions_needed) của P12 chưa được người duyệt trả lời."""
    return [d for d in proposal_decisions if d["id"] not in answers]
