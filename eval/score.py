#!/usr/bin/env python3
"""Bộ chấm điểm tự động cho golden set (SPEC §16.2, §2.2) — chạy được hoàn toàn offline.

Đầu vào: một `run.json` do `visynth run … --artifacts <thư mục>` ghi ra, cộng một `meta.json` của tài liệu
trong `eval/golden/<doc_id>/`. Đầu ra: JSON + Markdown với từng chỉ số, ngưỡng tương ứng và **kết luận đạt/không**.

Sáu chỉ số §16.2 (phần tự động):

| Khoá | Cách đo |
|---|---|
| `coverage_core` | danh sách "phải phủ" của người làm → có mặt trong báo cáo và có khối trích dẫn nguồn (`yes`), chỉ xuất hiện ở khối bị đánh cờ/thiếu trích dẫn (`partial`), hoặc vắng (`no`) |
| `faithfulness_rate` | khối chưa xoá có `verdict` `supported`/`partially_supported` **và** không còn lỗi cứng chưa giải quyết / tổng khối |
| `terminology_consistency` | 1 − vi phạm lint glossary / số lần thuật ngữ xuất hiện |
| `trap_facts_accuracy` | mỗi trap fact: có trong báo cáo và không có biến thể sai; lỗi = 0 theo §2.2 |
| `length_ratio` | số từ báo cáo / `target_words` trong meta |
| `cost_usd`, `seconds` | từ sổ `llm_calls` của lần chạy và thời lượng job (chỉ có khi chạy thật) |

Ngưỡng lấy từ §2.2 và §16.3. Chỉ số nào thiếu dữ liệu (ví dụ chạy kịch bản giả không có chi phí thật) thì
đánh dấu `applicable: false` chứ không tính là đạt.

    python eval/score.py --run eval/runs/demo/run.json --golden eval/golden/demo_lecture/meta.json
    python eval/score.py --run … --golden … --out eval/runs/demo/score.json --markdown eval/runs/demo/score.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if (REPO / "apps" / "worker").is_dir():  # chạy trực tiếp không cần cài đặt
    sys.path.insert(0, str(REPO / "apps" / "worker"))

from visynth.checks.glossary import GlossaryEntry, lint  # noqa: E402
from visynth.checks.report import UNRESOLVED_TYPES  # noqa: E402
from visynth.estimate import STARTER_PRICES, pick_price  # noqa: E402

#: Ngưỡng §2.2 / §16.3. `min` = càng cao càng tốt, `max` = càng thấp càng tốt.
THRESHOLDS = {
    "coverage_core": {"min": 0.90, "note": "§2.2; hạng A cần ≥ 0.95"},
    "faithfulness_rate": {"min": 0.97, "note": "§2.2"},
    "fabricated_remaining": {"max": 0, "integer": True, "note": "§2.2: số khối fabricated còn sót = 0"},
    "terminology_consistency": {"min": 0.98, "note": "§2.2"},
    "trap_fact_errors": {"max": 0, "integer": True, "note": "§2.2: số liệu sai chưa giải quyết = 0"},
    "unresolved_numbers": {"max": 0, "integer": True, "note": "§2.2 (số/ngày không tìm thấy trong nguồn)"},
    "length_ratio": {"min": 0.80, "max": 1.25, "note": "lệch quá ±20% so với ngân sách độ dài"},
}
#: Chỉ số của §16.3 dùng để chặn hồi quy khi so baseline (giảm/tăng quá mức).
REGRESSION_LIMITS = {
    "coverage_core": {"drop": 0.03},
    "faithfulness_rate": {"drop": 0.02},
    "trap_fact_errors": {"rise": 0},
    "cost_usd": {"rise_pct": 0.20},
    "seconds_p95": {"rise_pct": 0.30},
}


# ------------------------------------------------------------------ tiện ích văn bản


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def norm(text: str) -> str:
    """Chuẩn hoá để so khớp: NFC, thường, gộp khoảng trắng, bỏ dấu câu."""

    def _clean(ch: str) -> str:
        if ch.isalnum() or ch.isspace() or ch in "-_%":
            return ch
        return " "

    return re.sub(r"\s+", " ", "".join(_clean(c) for c in nfc(text).casefold())).strip()


def contains(haystack_norm: str, needle: str) -> bool:
    """`needle` có xuất hiện trong `haystack_norm` như một đơn vị trọn vẹn không.

    Bắt buộc ranh giới để `5% nguồn` KHÔNG khớp bên trong `35% nguồn` (lỗi thật gặp khi chấm lần đầu).
    """
    n = norm(needle)
    if not n:
        return False
    return re.search(rf"(?<![\w%]){re.escape(n)}(?![\w%])", haystack_norm) is not None


def report_body(markdown: str) -> str:
    """Phần thân báo cáo: bỏ dòng tiêu đề/siêu dữ liệu (`*Nguồn:* … **Mức:** …`) trước khi lint thuật ngữ.

    Lint trên toàn văn sẽ bắt nhầm slug như `deep_synthesis` trong dòng "Mức:" — đó là nhãn hệ thống,
    không phải văn xuôi của báo cáo.
    """
    keep = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("*") and not stripped.startswith("**"):
            continue  # phụ đề in nghiêng
        if "**Nguồn:**" in stripped or "**Mức:**" in stripped:
            continue
        keep.append(line)
    return "\n".join(keep)


def _missing_tokens(concept: str, haystack: str) -> float:
    """Tỷ lệ token nội dung của `concept` KHÔNG có trong `haystack` (0 = có đủ)."""
    toks = [t for t in norm(concept).split() if len(t) >= 2]
    if not toks:
        return 1.0
    present = sum(1 for t in toks if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", haystack))
    return 1.0 - present / len(toks)


def mentions(entry: dict, haystack_norm: str, *, fuzzy: float = 0.75) -> bool:
    """Khái niệm có xuất hiện trong `haystack_norm` không (khớp thẳng hoặc đủ token, có kiểm tra mờ)."""
    for form in [entry["concept"], *(entry.get("variants") or [])]:
        f = norm(form)
        if not f:
            continue
        if contains(haystack_norm, form):
            return True
        if _missing_tokens(form, haystack_norm) <= (1.0 - fuzzy):
            return True
    return False


def _blocks(run: dict) -> list[dict]:
    blocks = run.get("blocks_full") or run.get("blocks") or []
    return [b for b in blocks if not b.get("removed")]


def _hard_unresolved(block: dict) -> list[dict]:
    issues = (block.get("check") or {}).get("issues") or block.get("issues") or []
    return [i for i in issues if i.get("type") in UNRESOLVED_TYPES and i.get("hard", False)]


# ------------------------------------------------------------------ từng chỉ số


def coverage_core(run: dict, meta: dict) -> tuple[float, dict]:
    """§2.2: (yes + 0.5 × partial) / tổng unit cốt lõi của danh sách "phải phủ"."""
    report = norm(run.get("markdown") or "")
    blocks = _blocks(run)
    detail: dict[str, dict] = {}
    score = 0.0
    core = [e for e in meta.get("must_cover") or [] if e.get("core", True)]
    for entry in core:
        if not mentions(entry, report):
            status = "no"
        else:
            supported = any(
                mentions(entry, norm(b.get("markdown_vi") or ""))
                and b.get("cites")
                and b.get("verdict") in ("supported", "partially_supported")
                and not b.get("flagged")
                for b in blocks
            )
            status = "yes" if supported else "partial"
        detail[entry["concept"]] = {"status": status, "variants": entry.get("variants") or []}
        score += {"yes": 1.0, "partial": 0.5, "no": 0.0}[status]
    total = len(core) or 1
    return round(score / total, 4), detail


def faithfulness(run: dict) -> tuple[float, int, dict]:
    blocks = _blocks(run)
    bad: list[str] = []
    fabricated = 0
    for b in blocks:
        if b.get("verdict") == "fabricated":
            fabricated += 1
            bad.append(b["block_id"])
        elif b.get("verdict") not in ("supported", "partially_supported") or _hard_unresolved(b):
            bad.append(b["block_id"])
    total = len(blocks) or 1
    rate = round((total - len(bad)) / total, 4)
    detail = {"blocks": len(blocks), "not_ok": bad, "verdicts": _counts(b.get("verdict") for b in blocks)}
    return rate, fabricated, detail


def terminology(run: dict, meta: dict) -> tuple[float, dict]:
    entries = [
        GlossaryEntry(
            source_term=e["source_term"],
            target_term=e["target_term"],
            keep_original=bool(e.get("keep_original", False)),
            case_sensitive=bool(e.get("case_sensitive", False)),
            forbidden_variants=tuple(e.get("forbidden_variants") or ()),
        )
        for e in meta.get("glossary") or []
    ]
    markdown = report_body(run.get("markdown") or "")
    issues = lint(markdown, entries) if entries else []
    text_norm = norm(markdown)
    occurrences = 0
    for e in entries:
        for form in (e.target_term, e.source_term):
            f = norm(form)
            if f:
                occurrences += len(re.findall(rf"(?<!\w){re.escape(f)}(?!\w)", text_norm))
    occurrences = max(occurrences, 1)
    rate = round(max(0.0, 1.0 - len(issues) / occurrences), 4)
    detail = {
        "terms": len(entries),
        "occurrences": occurrences,
        "violations": [{"kind": i.kind, "term": i.term, "detail": i.detail} for i in issues],
    }
    return rate, detail


def trap_facts(run: dict, meta: dict) -> tuple[float, int, dict]:
    report = norm(run.get("markdown") or "")
    rows: list[dict] = []
    errors = 0
    for fact in meta.get("trap_facts") or []:
        forms = [fact["fact"], *(fact.get("accept") or [])]
        present = any(contains(report, f) for f in forms)
        wrong = next((w for w in fact.get("wrong_variants") or [] if contains(report, w)), None)
        if wrong:
            status = "wrong"
            errors += 1
        elif present:
            status = "ok"
        else:
            status = "missing"
        rows.append({"fact": fact["fact"], "kind": fact.get("kind", "number"), "status": status, "wrong": wrong})
    total = len(rows) or 1
    accuracy = round(sum(1 for r in rows if r["status"] == "ok") / total, 4)
    return accuracy, errors, {"facts": total, "detail": rows}


def unresolved_numbers(run: dict) -> tuple[int, dict]:
    rows = []
    for b in _blocks(run):
        for issue in _hard_unresolved(b):
            if issue.get("type") in ("number_mismatch", "date_mismatch"):
                rows.append({"block_id": b["block_id"], "type": issue["type"], "detail": issue.get("detail", "")})
    return len(rows), {"detail": rows}


def length(run: dict, meta: dict) -> tuple[float, dict]:
    words = len((run.get("markdown") or "").split())
    target = int(meta.get("target_words") or 0)
    ratio = round(words / target, 4) if target else 0.0
    return ratio, {"report_words": words, "target_words": target}


def cost(run: dict, price: Any) -> dict:
    calls = run.get("llm_calls") or {}
    if not calls:
        return {"applicable": False, "reason": "không có sổ llm_calls (kịch bản giả)", "cost_usd": None}
    tin, tout = int(calls.get("tokens_in") or 0), int(calls.get("tokens_out") or 0)
    usd = (tin * price.input_per_mtok + tout * price.output_per_mtok) / 1_000_000
    return {
        "applicable": True,
        "cost_usd": round(usd, 4),
        "calls": calls.get("calls"),
        "tokens_in": tin,
        "tokens_out": tout,
        "price": {"model": price.model, "from": price.effective_from.isoformat()},
    }


def _counts(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


# ------------------------------------------------------------------ ghép và kết luận


def score_run(run: dict, meta: dict, *, model: str = "gemini-3.8-flash", on: date | None = None) -> dict[str, Any]:
    """Chấm một lần chạy. Trả về dict có `metrics`, `gates`, `verdict`, `blocking`."""
    price = pick_price(STARTER_PRICES, model, on or date(2026, 10, 2))
    cov, cov_detail = coverage_core(run, meta)
    faith, fabricated, faith_detail = faithfulness(run)
    term, term_detail = terminology(run, meta)
    traps, trap_errors, trap_detail = trap_facts(run, meta)
    unresolved, unresolved_detail = unresolved_numbers(run)
    length_ratio, length_detail = length(run, meta)
    cost_info = cost(run, price)

    metrics = {
        "coverage_core": cov,
        "faithfulness_rate": faith,
        "fabricated_remaining": fabricated,
        "terminology_consistency": term,
        "trap_facts_accuracy": traps,
        "trap_fact_errors": trap_errors,
        "unresolved_numbers": unresolved,
        "length_ratio": length_ratio,
        "cost_usd": cost_info.get("cost_usd"),
    }
    gates: dict[str, dict] = {}
    for key, rule in THRESHOLDS.items():
        value = metrics.get(key)
        if value is None:
            gates[key] = {"applicable": False, "value": None, "note": rule["note"]}
            continue
        ok = True
        if "min" in rule and value < rule["min"]:
            ok = False
        if "max" in rule and value > rule["max"]:
            ok = False
        gates[key] = {
            "applicable": True,
            "value": value,
            "min": rule.get("min"),
            "max": rule.get("max"),
            "pass": ok,
            "note": rule["note"],
        }
    if not cost_info.get("applicable"):
        gates["cost_usd"] = {"applicable": False, "value": None, "note": "§2.2 — cần chạy thật (có sổ llm_calls)"}
    blocking = [k for k, g in gates.items() if g.get("applicable") and not g.get("pass")]
    return {
        "doc_id": meta.get("doc_id"),
        "title": meta.get("title"),
        "level": run.get("level") or meta.get("level"),
        "grade": run.get("grade"),
        "metrics": metrics,
        "gates": gates,
        "blocking": blocking,
        "verdict": "pass" if not blocking else "fail",
        "detail": {
            "coverage_core": cov_detail,
            "faithfulness": faith_detail,
            "terminology": term_detail,
            "trap_facts": trap_detail,
            "unresolved_numbers": unresolved_detail,
            "length": length_detail,
            "cost": cost_info,
            "stats": run.get("stats") or {},
            "warnings": run.get("warnings") or [],
        },
    }


def score_markdown(report: dict) -> str:
    lines = [
        f"# Chấm điểm golden — {report['doc_id']}",
        "",
        f"- Mức: `{report['level']}` · hạng chất lượng (pipeline): **{report['grade']}**",
        f"- Kết luận: **{'ĐẠT' if report['verdict'] == 'pass' else 'KHÔNG ĐẠT'}**"
        + (f" — chặn bởi: {', '.join(report['blocking'])}" if report["blocking"] else ""),
        "",
        "| Chỉ số | Giá trị | Ngưỡng | Kết quả |",
        "|---|---|---|---|",
    ]
    for key, gate in report["gates"].items():
        if not gate.get("applicable"):
            lines.append(f"| `{key}` | — | {gate.get('note', '')} | không áp dụng |")
            continue
        value = gate["value"]
        value_txt = f"{value:.4f}" if isinstance(value, float) else str(value)
        bound = []
        if gate.get("min") is not None:
            bound.append(f"≥ {gate['min']}")
        if gate.get("max") is not None:
            bound.append(f"≤ {gate['max']}")
        lines.append(f"| `{key}` | {value_txt} | {' và '.join(bound)} | {'✅' if gate['pass'] else '❌'} |")
    cov = report["detail"]["coverage_core"]
    missing = [k for k, v in cov.items() if v["status"] == "no"]
    partial = [k for k, v in cov.items() if v["status"] == "partial"]
    if missing or partial:
        lines += [
            "",
            f"- Phải phủ còn thiếu: {', '.join(missing) or '—'}",
            f"- Chỉ đạt một phần: {', '.join(partial) or '—'}",
        ]
    traps = [r for r in report["detail"]["trap_facts"]["detail"] if r["status"] != "ok"]
    if traps:
        lines += ["", "Trap fact chưa đạt:"]
        lines += [
            f"- `{r['fact']}` ({r['kind']}): {r['status']}" + (f" — thấy `{r['wrong']}`" if r["wrong"] else "")
            for r in traps
        ]
    violations = report["detail"]["terminology"]["violations"]
    if violations:
        lines += ["", "Vi phạm thuật ngữ:"]
        lines += [f"- {v['kind']} `{v['term']}`: {v['detail']}" for v in violations[:20]]
    cost = report["detail"]["cost"]
    if cost.get("applicable"):
        lines += [
            "",
            f"- Chi phí thật (theo giá {cost['price']['model']}): **${cost['cost_usd']}** cho {cost['calls']} lời gọi",
        ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ CLI


GOLDEN_TARGETS = {  # §16.1
    "lecture_transcript": 6,
    "book_chapter": 6,
    "article": 4,
    "pdf_scan": 3,
    "messy_docx": 3,
    "vietnamese_source": 2,
}


def validate_golden(root: Path) -> dict:
    """Kiểm tra mọi `eval/golden/*/meta.json` theo schema, sự tồn tại của tệp nguồn và độ phủ §16.1."""
    import jsonschema

    schema = json.loads((root / "schema.json").read_text(encoding="utf-8"))
    rows: list[dict] = []
    for meta_path in sorted(root.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            rows.append({"doc": meta_path.parent.name, "ok": False, "errors": [f"JSON hỏng: {exc}"]})
            continue
        errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(meta)]
        source = (meta_path.parent / str(meta.get("source_path", ""))).resolve()
        if not source.exists():
            errors.append(f"không thấy tệp nguồn: {meta.get('source_path')}")
        if not meta.get("demo"):
            core = sum(1 for e in meta.get("must_cover") or [] if e.get("core", True))
            if core < 30:
                errors.append(f"chỉ {core} mục 'phải phủ' cốt lõi (tài liệu thật cần 30-60 theo §16.1)")
            if len(meta.get("trap_facts") or []) < 10:
                errors.append(f"chỉ {len(meta.get('trap_facts') or [])} trap fact (cần ≥ 10)")
        rows.append({"doc": meta_path.parent.name, "ok": not errors, "errors": errors, "kind": meta.get("kind")})
    counts: dict[str, int] = {}
    for r in rows:
        if r.get("kind") and r.get("kind") != "smoke":
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    return {
        "root": str(root),
        "documents": rows,
        "ok": all(r["ok"] for r in rows),
        "counts_by_kind": counts,
        "targets": GOLDEN_TARGETS,
        "missing": {k: v - counts.get(k, 0) for k, v in GOLDEN_TARGETS.items() if counts.get(k, 0) < v},
    }


def _load_meta(path: Path) -> dict:
    if path.is_dir():
        path = path / "meta.json"
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chấm điểm một lần chạy theo golden set (SPEC §16.2, §2.2)")
    parser.add_argument("--run", help="run.json do `visynth run --artifacts` ghi ra")
    parser.add_argument("--golden", help="meta.json (hoặc thư mục golden) của tài liệu")
    parser.add_argument(
        "--validate-golden",
        nargs="?",
        const=str(REPO / "eval" / "golden"),
        help="kiểm tra toàn bộ bộ golden (schema, tệp nguồn, độ phủ §16.1) rồi thoát",
    )
    parser.add_argument("--out", help="ghi JSON kết quả")
    parser.add_argument("--markdown", help="ghi báo cáo Markdown")
    parser.add_argument("--model", default="gemini-3.8-flash", help="model để tra giá khi tính chi phí")
    parser.add_argument("--json", action="store_true", help="in JSON thay vì bảng người đọc")
    args = parser.parse_args(argv)

    if args.validate_golden:
        report = validate_golden(Path(args.validate_golden))
        for row in report["documents"]:
            mark = "ok  " if row["ok"] else "LỖI "
            print(f"  {mark} {row['doc']} ({row.get('kind', '?')})")
            for err in row["errors"]:
                print(f"        - {err}")
        counts = ", ".join(f"{k}: {report['counts_by_kind'].get(k, 0)}/{v}" for k, v in GOLDEN_TARGETS.items())
        print(f"Độ phủ §16.1 — {counts}")
        if report["missing"]:
            print("Còn thiếu: " + ", ".join(f"{k} × {v}" for k, v in report["missing"].items()))
        print("Kết luận: " + ("HỢP LỆ" if report["ok"] else "CÓ LỖI"))
        return 0 if report["ok"] else 1

    if not args.run or not args.golden:
        parser.error("cần --run và --golden (hoặc --validate-golden)")
    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    meta = _load_meta(Path(args.golden))
    report = score_run(run, meta, model=args.model)
    md = score_markdown(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(md, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else md)
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
