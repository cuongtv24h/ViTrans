#!/usr/bin/env python3
"""Bake-off chọn cấu hình model (SPEC §16.4, Giai đoạn 0).

So **N cấu hình `pool_config` trên cùng một tập tài liệu golden**, chấm từng lần chạy bằng `eval/score.py`,
rồi chọn **cấu hình rẻ nhất đạt ngưỡng §2.2** — thay vì chọn theo cảm giác. Kèm hai việc §16.4 yêu cầu:

* **Đa dạng người kiểm** — báo cáo `writer` và `verifier` của mỗi cấu hình có thuộc cùng một họ model không
  (kiểm bằng `verifier` khác họ để giảm lỗi tương quan);
* **Điểm `probe`** — nếu cạnh mỗi `pool_config` có `probe.json` (§17.9) thì nhập thêm điểm `json`, `vi_write`,
  `long_context` và `tokenizer_factor` đo được vào báo cáo.

Chạy thật cần khoá trong môi trường (`.env`) và mạng tới nhà cung cấp; `--demo` chạy toàn bộ đường ống bằng
`FakeLLMClient` để kiểm tra công cụ (khi đó mọi cấu hình cho cùng kết quả — chỉ để thử đường ống).

    python eval/bakeoff.py --configs pool_config.a.json pool_config.b.json \\
        --golden eval/golden --out eval/runs/bakeoff-2026-10-05
    python eval/bakeoff.py --configs … --golden … --demo        # kiểm tra đường ống, không tốn token

Mã thoát: 0 = có ít nhất một cấu hình đạt; 1 = không cấu hình nào đạt (tín hiệu chỉnh hướng).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if (REPO / "apps" / "worker").is_dir():
    sys.path.insert(0, str(REPO / "apps" / "worker"))

from visynth.extract import extract  # noqa: E402
from visynth.pipeline.artifacts import to_artifact, write_artifact  # noqa: E402
from visynth.pipeline.models import JobOptions  # noqa: E402
from visynth.pipeline.run import run_document  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


score = _load("visynth_eval_score", REPO / "eval" / "score.py")


# ------------------------------------------------------------------ đọc vào


def load_configs(paths: list[str], *, strict: bool = True) -> list[dict]:
    """Nạp các `pool_config` ứng viên. Mặc định kiểm schema + ràng buộc trước khi chạy (đừng đốt token cho
    một cấu hình sai); `strict=False` thì giữ lại và ghi lỗi để báo cáo thấy."""
    from visynth.pool.validate import validate_config

    out = []
    for raw in paths:
        path = Path(raw)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        report = validate_config(cfg)
        if not report["valid"]:
            message = f"pool_config KHÔNG hợp lệ ({path}): " + "; ".join(report["errors"][:3])
            if strict:
                raise ValueError(message)
            out.append(
                {"id": cfg.get("id") or path.stem, "path": str(path), "config": cfg, "probe": None, "invalid": message}
            )
            continue
        cfg_id = cfg.get("id") or path.stem.replace("pool_config.", "").replace("pool_config", "default")
        probe_path = path.with_name(path.stem + ".probe.json")
        if not probe_path.exists():
            probe_path = path.with_name("probe.json")
        out.append(
            {
                "id": cfg_id,
                "path": str(path),
                "config": cfg,
                "probe": json.loads(probe_path.read_text(encoding="utf-8")) if probe_path.exists() else None,
                "invalid": None,
                "warnings": report["warnings"],
            }
        )
    return out


def load_docs(paths: list[str], limit: int | None = None) -> list[dict]:
    """`paths` là các thư mục golden hoặc các `meta.json`; trả về meta + đường dẫn nguồn tuyệt đối."""
    metas: list[Path] = []
    for raw in paths:
        path = Path(raw)
        metas += sorted(path.glob("*/meta.json")) if path.is_dir() else [path]
    docs = []
    for meta_path in metas:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        source = (meta_path.parent / str(meta.get("source_path", ""))).resolve()
        docs.append(
            {
                "meta": meta,
                "meta_path": str(meta_path),
                "source": source,
                "doc_id": meta.get("doc_id") or meta_path.parent.name,
            }
        )
    return docs[:limit] if limit else docs


# ------------------------------------------------------------------ họ model (đa dạng người kiểm)


def model_family(model_id: str) -> str:
    """`nvidia:meta/llama-3.3-70b-instruct` → `nvidia/meta`; `gemini:gemini-3.8-flash` → `gemini/gemini`."""
    provider, _, name = str(model_id).partition(":")
    head = name.split("/", 1)[0]
    return f"{provider}/{head.split('-')[0]}"


def profile_families(config: dict, profile_name: str) -> dict:
    """Tập họ model mà một profile có thể chọn (theo mọi tầng) + deployment tương ứng."""
    from visynth.pool.model import load_pool_model

    model = load_pool_model(config)
    profile = model.profiles.get(profile_name)
    if profile is None:
        return {"families": [], "deployments": []}
    deployments = [d for d in model.deployments if d.enabled and d.group.enabled]
    picked: list[str] = []
    for tier in profile.tiers:
        for d in deployments:
            if tier.select_group_tiers and d.group.tier not in tier.select_group_tiers:
                continue
            if tier.select_tags and not (tier.select_tags & d.tags):
                continue
            picked.append(d.id)
    by_id = {d.id: d for d in model.deployments}
    return {
        "families": sorted({model_family(by_id[d_id].model.id) for d_id in picked if d_id in by_id}),
        "deployments": sorted(set(picked)),
    }


def diversity_note(config: dict) -> dict:
    """§16.4: người viết và người kiểm khác họ model là một biến thể cần thử riêng."""
    writer = profile_families(config, "writer")
    verifier = profile_families(config, "verifier")
    overlap = sorted(set(writer["families"]) & set(verifier["families"]))
    return {
        "writer": writer,
        "verifier": verifier,
        "overlap": overlap,
        "diverse": bool(verifier["families"]) and not overlap,
        "note": (
            "người kiểm KHÁC họ người viết — giảm lỗi tương quan"
            if verifier["families"] and not overlap
            else ("người kiểm CÙNG họ người viết (kém đa dạng)" if overlap else "không xác định được họ")
        ),
    }


def probe_digest(probe: dict | None) -> dict:
    """§16.4(a)(c): điểm `json`, `vi_write`, `long_context`, `tokenizer_factor` đo được cho từng deployment."""
    if not probe:
        return {"available": False}
    rows = {}
    for run in probe.get("runs") or []:
        vi = run.get("vi_write") or {}
        rows[run.get("deployment_id")] = {
            "json_ok": bool((run.get("json") or {}).get("ok")),
            "vi_write_ok": bool(vi.get("ok")),
            "vi_write_words": vi.get("words"),
            "tokenizer_factor": run.get("tokenizer_factor"),
            "latency_p50_ms": run.get("latency_p50_ms"),
            "errors": run.get("errors") or [],
        }
    return {"available": bool(rows), "deployments": rows, "suggest_updates": probe.get("suggest_updates") or {}}


# ------------------------------------------------------------------ chạy một cặp (cấu hình, tài liệu)


def run_one(entry: dict, doc: dict, out_dir: Path, *, demo: bool, seed: int, gate: str, privacy: str) -> dict:
    """Chạy một tài liệu với một cấu hình; luôn trả về dict (kể cả khi lỗi) để báo cáo không đứt."""
    level = doc["meta"].get("level") or "deep_synthesis"
    started = time.monotonic()
    row: dict = {"config": entry["id"], "doc": doc["doc_id"], "level": level}
    try:
        if demo:
            from visynth.eval import demo_client

            ext = extract(doc["source"])
            client = demo_client(ext, level=level)
        else:
            from visynth.pool.client import Ledger, PooledLLMClient

            cfg = entry["config"]
            ledger = Ledger(out_dir / "llm_calls.jsonl")
            client = PooledLLMClient.from_config(cfg, ledger=ledger, gate=gate, privacy=privacy)
            ext = extract(doc["source"])
        result = run_document(ext, client, JobOptions(level=level), seed=seed)
        duration_ms = int((time.monotonic() - started) * 1000)
        totals = ledger.totals() if not demo else None
        payload = to_artifact(
            result,
            source={"path": str(doc["source"]), "words": result.stats.get("source_words"), "title": result.title},
            params={"level": level, "seed": seed, "demo": demo},
            duration_ms=duration_ms,
            llm_calls=totals,
            extra={"config_id": entry["id"], "config_path": entry["path"], "doc_id": doc["doc_id"]},
        )
        artifact_path = write_artifact(payload, out_dir)
        report = score.score_run(payload, doc["meta"])
        (out_dir / "score.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        row.update(
            {
                "status": "ok",
                "artifact": str(artifact_path),
                "grade": report["grade"],
                "verdict": report["verdict"],
                "blocking": report["blocking"],
                "metrics": report["metrics"],
                "seconds": round(duration_ms / 1000, 1),
            }
        )
    except Exception as exc:  # noqa: BLE001 — lỗi của MỘT cặp không được làm hỏng cả bake-off
        row.update(
            {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.monotonic() - started, 1),
            }
        )
    return row


# ------------------------------------------------------------------ tổng hợp và chọn


def summarize(rows: list[dict], configs: list[dict], docs: list[dict], *, demo: bool) -> dict:
    per_config: dict[str, dict] = {}
    for entry in configs:
        mine = [r for r in rows if r["config"] == entry["id"]]
        ok = [r for r in mine if r["status"] == "ok"]
        failed = [r for r in mine if r["status"] != "ok"]
        metrics = [r["metrics"] for r in ok]
        costs = [m.get("cost_usd") for m in metrics if m.get("cost_usd") is not None]
        seconds = [r["seconds"] for r in ok]

        def _mean(key: str, rows_metrics=metrics) -> float | None:
            vals = [m[key] for m in rows_metrics if m.get(key) is not None]
            return round(statistics.fmean(vals), 4) if vals else None

        blocked: dict[str, int] = {}
        for r in ok:
            for metric in r["blocking"]:
                blocked[metric] = blocked.get(metric, 0) + 1
        passing = (
            bool(ok)
            and not failed
            and len(ok) == len(docs)
            and all(m.get("coverage_core", 0) >= score.THRESHOLDS["coverage_core"]["min"] for m in metrics)
            and all(m.get("faithfulness_rate", 0) >= score.THRESHOLDS["faithfulness_rate"]["min"] for m in metrics)
            and all((m.get("trap_fact_errors") or 0) == 0 for m in metrics)
            and all((m.get("fabricated_remaining") or 0) == 0 for m in metrics)
        )
        per_config[entry["id"]] = {
            "config_path": entry["path"],
            "config_warnings": entry.get("warnings") or [],
            "docs_run": len(mine),
            "docs_ok": len(ok),
            "docs_failed": [r["doc"] for r in failed],
            "coverage_core_mean": _mean("coverage_core"),
            "coverage_core_min": min((m["coverage_core"] for m in metrics), default=None),
            "faithfulness_mean": _mean("faithfulness_rate"),
            "faithfulness_min": min((m["faithfulness_rate"] for m in metrics), default=None),
            "trap_fact_errors_total": sum(m.get("trap_fact_errors") or 0 for m in metrics),
            "fabricated_remaining_total": sum(m.get("fabricated_remaining") or 0 for m in metrics),
            "length_ratio_mean": _mean("length_ratio"),
            "total_cost_usd": round(sum(costs), 4) if costs else None,
            "cost_applicable": bool(costs),
            "seconds_total": round(sum(seconds), 1) if seconds else None,
            "seconds_p95": score._p95(seconds) if hasattr(score, "_p95") else None,
            "blocked_by": blocked,
            "grade_distribution": score._counts(r["grade"] for r in ok),
            "passes": passing,
            "diversity": diversity_note(entry["config"]),
            "probe": probe_digest(entry.get("probe")),
        }

    candidates = [cid for cid, s in per_config.items() if s["passes"]]

    # §16.4: chọn cấu hình RẺ NHẤT đạt ngưỡng; không có chi phí thật (chế độ khô) thì chọn theo tên để ổn định
    def _rank(cid: str) -> tuple:
        s = per_config[cid]
        return (s["total_cost_usd"] if s["total_cost_usd"] is not None else float("inf"), cid)

    winner = min(candidates, key=_rank) if candidates else None
    return {
        "mode": "demo (FakeLLMClient — không tốn token, không phản ánh chất lượng)" if demo else "thật",
        "documents": [
            {"doc_id": d["doc_id"], "kind": d["meta"].get("kind"), "level": d["meta"].get("level")} for d in docs
        ],
        "configs": per_config,
        "winner": winner,
        "passing": candidates,
        "verdict": ("chọn được cấu hình đạt" if winner else "KHÔNG cấu hình nào đạt — chỉnh hướng"),
    }


def to_markdown(report: dict) -> str:
    lines = [
        "# Bake-off chọn cấu hình model (§16.4)",
        "",
        f"- Chế độ: **{report['mode']}**",
        f"- Tài liệu: {len(report['documents'])} · cấu hình: {len(report['configs'])}",
        f"- Kết luận: **{report['verdict']}**" + (f" → `{report['winner']}`" if report["winner"] else ""),
        "",
        "| Cấu hình | Tài liệu OK | coverage (min / TB) | faithfulness (min / TB) | trap lỗi | chi phí | giây | người kiểm | Đạt |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cid, s in report["configs"].items():
        cov = (
            f"{s['coverage_core_min']:.2f} / {s['coverage_core_mean']:.2f}"
            if s["coverage_core_min"] is not None
            else "—"
        )
        faith = (
            f"{s['faithfulness_min']:.2f} / {s['faithfulness_mean']:.2f}" if s["faithfulness_min"] is not None else "—"
        )
        cost = f"${s['total_cost_usd']}" if s["cost_applicable"] else "—"
        div = s["diversity"]
        lines.append(
            f"| `{cid}` | {s['docs_ok']}/{s['docs_run']} | {cov} | {faith} | {s['trap_fact_errors_total']} | {cost} "
            f"| {s['seconds_total'] if s['seconds_total'] is not None else '—'} | {div['note']} | {'✅' if s['passes'] else '❌'} |"
        )
    for cid, s in report["configs"].items():
        if s["docs_failed"]:
            lines += ["", f"- `{cid}` lỗi ở: {', '.join(s['docs_failed'])}"]
        if s["blocked_by"]:
            lines += ["", f"- `{cid}` bị chặn bởi: " + ", ".join(f"{k} × {v}" for k, v in s["blocked_by"].items())]
        probe = s["probe"]
        if probe.get("available"):
            lines += ["", f"**`probe` của `{cid}`** (đo thật, §16.4a):"]
            for dep, d in probe["deployments"].items():
                lines.append(
                    f"- `{dep}`: json {'đạt' if d['json_ok'] else 'KHÔNG'}, vi_write {'đạt' if d['vi_write_ok'] else 'KHÔNG'}"
                    f" ({d['vi_write_words']} từ), tokenizer_factor {d['tokenizer_factor']}, p50 {d['latency_p50_ms']} ms"
                )
    lines += [
        "",
        "Ghi chú: chọn cấu hình **rẻ nhất đạt ngưỡng §2.2**; cấm chọn theo cảm giác. Ở chế độ khô (`--demo`)",
        "mọi cấu hình cho cùng kết quả và chi phí không có — chỉ dùng để kiểm tra đường ống.",
        "Sau khi chọn: cập nhật `pool_config` (`quality`, `limits`, `tiers`) rồi chạy `eval/compare_baseline.py`",
        "cho tập con 6 tài liệu trước khi phát hành thay đổi (§16.3).",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bake-off chọn cấu hình model (SPEC §16.4)")
    parser.add_argument("--configs", nargs="+", required=True, help="các pool_config.json ứng viên")
    parser.add_argument("--golden", nargs="+", required=True, help="thư mục golden hoặc các meta.json")
    parser.add_argument("--out", required=True, help="thư mục kết quả (mỗi cặp một thư mục con)")
    parser.add_argument("--demo", action="store_true", help="chạy kịch bản giả (không tốn token, kiểm tra đường ống)")
    parser.add_argument("--limit-docs", type=int, help="chỉ chạy N tài liệu đầu (thử nhanh)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--gate", choices=["dev", "A", "B", "C"], default="dev")
    parser.add_argument("--privacy", choices=["standard", "private"], default="standard")
    parser.add_argument("--json", action="store_true", help="in JSON thay vì báo cáo Markdown")
    args = parser.parse_args(argv)

    try:
        configs = load_configs(args.configs)
    except ValueError as exc:
        print(f"LỖI: {exc}\nChạy `visynth pool validate --config <tệp>` để xem đủ danh sách lỗi.", file=sys.stderr)
        return 2
    docs = load_docs(args.golden, limit=args.limit_docs)
    if not docs:
        print("LỖI: không tìm thấy tài liệu golden nào.", file=sys.stderr)
        return 2
    out_root = Path(args.out)
    rows: list[dict] = []
    for entry in configs:
        for doc in docs:
            target = out_root / f"{doc['doc_id']}__{entry['id']}"
            print(f"→ {entry['id']} × {doc['doc_id']} ({doc['meta'].get('level')})", flush=True)
            row = run_one(entry, doc, target, demo=args.demo, seed=args.seed, gate=args.gate, privacy=args.privacy)
            rows.append(row)
            print(f"   {row['status']}: {row.get('verdict') or row.get('error')}", flush=True)

    report = {"rows": rows, **summarize(rows, configs, docs, demo=args.demo)}
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "bakeoff.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = to_markdown(report)
    (out_root / "bakeoff.md").write_text(md, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else md)
    return 0 if report["winner"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
