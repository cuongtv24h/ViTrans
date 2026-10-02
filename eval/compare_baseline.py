#!/usr/bin/env python3
"""So baseline và chặn hồi quy (SPEC §16.3).

Đổi prompt, model, cấu hình pool hoặc Lõi văn phong thì phải chạy lại tập con 6 tài liệu và **chặn phát hành**
nếu chỉ số tụt quá ngưỡng. Tệp này so hai thư mục `eval/runs/<ngày>/` (mỗi tài liệu một `score.json` sinh bởi
`eval/score.py`) và trả về mã thoát 1 khi có chỉ số chặn.

    python eval/compare_baseline.py --baseline eval/runs/2026-10-05 --candidate eval/runs/2026-11-01
    python eval/compare_baseline.py --baseline A --candidate B --json --out eval/runs/so-sanh.json

Ngưỡng lấy từ `REGRESSION_LIMITS` trong `eval/score.py` (§16.3): coverage_core giảm > 0.03, faithfulness_rate
giảm > 0.02, có trap fact sai mới, chi phí tăng > 20%, p95 thời gian tăng > 30%.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_scorer():
    spec = importlib.util.spec_from_file_location("visynth_eval_score", REPO / "eval" / "score.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


score = _load_scorer()
LIMITS = score.REGRESSION_LIMITS


def load_scores(root: Path) -> dict[str, dict]:
    """Đọc mọi `**/score.json` dưới `root`, khoá theo `doc_id` (tệp sau ghi đè tệp trước)."""
    out: dict[str, dict] = {}
    for path in sorted(root.rglob("score.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        doc_id = data.get("doc_id") or path.parent.name
        data["_path"] = str(path.relative_to(root))
        out[doc_id] = data
    return out


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    # nội suy tuyến tính, cùng quy ước với numpy.percentile
    xs = sorted(values)
    k = 0.95 * (len(xs) - 1)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def compare(baseline: dict[str, dict], candidate: dict[str, dict]) -> dict:
    """So từng tài liệu và tính chỉ số tổng hợp; trả `blocking` theo §16.3."""
    rows: list[dict] = []
    for doc_id in sorted(set(baseline) | set(candidate)):
        base, cand = baseline.get(doc_id), candidate.get(doc_id)
        if base is None or cand is None:
            rows.append(
                {
                    "doc_id": doc_id,
                    "status": "thiếu",
                    "detail": "chỉ có ở " + ("baseline" if cand is None else "candidate"),
                }
            )
            continue
        bm, cm = base.get("metrics", {}), cand.get("metrics", {})
        deltas: dict[str, dict] = {}
        for key in (
            "coverage_core",
            "faithfulness_rate",
            "terminology_consistency",
            "trap_facts_accuracy",
            "length_ratio",
        ):
            if bm.get(key) is None or cm.get(key) is None:
                continue
            deltas[key] = {"baseline": bm[key], "candidate": cm[key], "delta": round(cm[key] - bm[key], 4)}
        for key in ("trap_fact_errors", "fabricated_remaining", "unresolved_numbers"):
            if bm.get(key) is None or cm.get(key) is None:
                continue
            deltas[key] = {"baseline": bm[key], "candidate": cm[key], "delta": cm[key] - bm[key]}
        for key in ("cost_usd", "seconds"):
            if bm.get(key) is None or cm.get(key) is None:
                continue
            ratio = (cm[key] / bm[key]) if bm[key] else None
            deltas[key] = {"baseline": bm[key], "candidate": cm[key], "ratio": round(ratio, 3) if ratio else None}
        rows.append({"doc_id": doc_id, "status": "so được", "deltas": deltas})

    comparable = [r for r in rows if r["status"] == "so được"]
    blocking: list[dict] = []

    def _pair(key: str) -> list[tuple[str, float, float]]:
        return [
            (r["doc_id"], r["deltas"][key]["baseline"], r["deltas"][key]["candidate"])
            for r in comparable
            if key in r["deltas"]
        ]

    for doc_id, base_v, cand_v in _pair("coverage_core"):
        if base_v - cand_v > LIMITS["coverage_core"]["drop"]:
            blocking.append({"doc_id": doc_id, "metric": "coverage_core", "drop": round(base_v - cand_v, 4)})
    for doc_id, base_v, cand_v in _pair("faithfulness_rate"):
        if base_v - cand_v > LIMITS["faithfulness_rate"]["drop"]:
            blocking.append({"doc_id": doc_id, "metric": "faithfulness_rate", "drop": round(base_v - cand_v, 4)})
    for doc_id, base_v, cand_v in _pair("trap_fact_errors"):
        if cand_v > base_v:
            blocking.append({"doc_id": doc_id, "metric": "trap_fact_errors", "rise": cand_v - base_v})
    for doc_id, base_v, cand_v in _pair("cost_usd"):
        if base_v and cand_v / base_v - 1 > LIMITS["cost_usd"]["rise_pct"]:
            blocking.append(
                {
                    "doc_id": doc_id,
                    "metric": "cost_usd",
                    "rise_pct": round(cand_v / base_v - 1, 3),
                }
            )

    base_p95 = _p95([v for _, v, _ in _pair("seconds")])
    cand_p95 = _p95([v for _, _, v in _pair("seconds")])
    if base_p95 and cand_p95 and cand_p95 / base_p95 - 1 > LIMITS["seconds_p95"]["rise_pct"]:
        blocking.append({"doc_id": "(tập)", "metric": "seconds_p95", "rise_pct": round(cand_p95 / base_p95 - 1, 3)})

    summary = {
        "documents_baseline": len(baseline),
        "documents_candidate": len(candidate),
        "documents_compared": len(comparable),
        "missing": [r["doc_id"] for r in rows if r["status"] == "thiếu"],
        "p95_seconds": {"baseline": base_p95, "candidate": cand_p95},
        "blocking": blocking,
        "verdict": "block" if blocking else "pass",
    }
    return {"rows": rows, "summary": summary}


def to_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        "# So baseline (§16.3)",
        "",
        f"- Tài liệu: baseline {s['documents_baseline']}, candidate {s['documents_candidate']}, so được {s['documents_compared']}",
        f"- p95 thời gian: {s['p95_seconds']['baseline']} → {s['p95_seconds']['candidate']} giây",
        f"- Kết luận: **{'CHẶN' if s['verdict'] == 'block' else 'CHO QUA'}**",
        "",
        "| Tài liệu | coverage_core | faithfulness | trap errors | chi phí | giây |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["rows"]:
        if row["status"] != "so được":
            lines.append(f"| {row['doc_id']} | — | — | — | — | — | (thiếu: {row['detail']})")
            continue
        d = row["deltas"]

        def cell(key: str, fmt: str = "{:+.4f}", deltas=d) -> str:
            if key not in deltas:
                return "—"
            return fmt.format(deltas[key]["delta"])

        cost = d.get("cost_usd", {}).get("ratio")
        sec = d.get("seconds", {}).get("ratio")
        lines.append(
            f"| {row['doc_id']} | {cell('coverage_core')} | {cell('faithfulness_rate')} | "
            f"{cell('trap_fact_errors', '{:+d}') if 'trap_fact_errors' in d else '—'} | "
            f"{f'×{cost}' if cost else '—'} | {f'×{sec}' if sec else '—'} |"
        )
    if s["blocking"]:
        lines += ["", "**Chặn bởi:**"]
        for b in s["blocking"]:
            detail = {k: v for k, v in b.items() if k not in ("doc_id", "metric")}
            lines.append(f"- `{b['metric']}` ở `{b['doc_id']}`: {detail}")
    if s["missing"]:
        lines += ["", f"Lưu ý: thiếu kết quả cho {', '.join(s['missing'])} — chạy lại tập con đủ 6 tài liệu (§16.3)."]
    lines += ["", "Ngưỡng: coverage −0.03, faithfulness −0.02, trap fact mới, chi phí +20%, p95 thời gian +30%."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="So hai lần chạy golden và chặn hồi quy (SPEC §16.3)")
    parser.add_argument("--baseline", required=True, help="thư mục chứa score.json của lần chạy gốc")
    parser.add_argument("--candidate", required=True, help="thư mục chứa score.json của lần chạy mới")
    parser.add_argument("--out", help="ghi JSON kết quả so sánh")
    parser.add_argument("--markdown", help="ghi báo cáo Markdown")
    parser.add_argument("--json", action="store_true", help="in JSON thay vì bảng người đọc")
    args = parser.parse_args(argv)

    base, cand = load_scores(Path(args.baseline)), load_scores(Path(args.candidate))
    if not base or not cand:
        print(
            f"LỖI: không thấy score.json — baseline {len(base)} tệp, candidate {len(cand)} tệp. "
            "Chạy `python eval/score.py` cho từng tài liệu trước.",
            file=sys.stderr,
        )
        return 2
    report = compare(base, cand)
    md = to_markdown(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(md, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else md)
    return 1 if report["summary"]["verdict"] == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
