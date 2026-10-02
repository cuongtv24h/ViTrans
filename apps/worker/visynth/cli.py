"""CLI của M0 — prototype chạy trên máy, chưa cần CSDL và chưa cần pool thật.

visynth extract  <tệp>                       # bóc tách → tóm tắt hoặc JSON
visynth segment  <tệp> --mode map|translate  # chia segment
visynth estimate <tệp> --level <mức>         # ước tính tín dụng/chi phí/thời gian
visynth run      --demo [--tamper <biến thể>]  # chạy trọn P0–P8 trên kịch bản giả (M0-W2)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from visynth.estimate import (
    LEVELS,
    STARTER_PRICES,
    docs_per_day,
    estimate,
    estimate_calls,
    pick_price,
    report_budget_words,
)
from visynth.eval import TAMPER_MODES
from visynth.extract import ExtractionError, extract
from visynth.segment import segment_extraction


def _load(path: str) -> Any:
    from visynth.extract import Extraction

    ext: Extraction = extract(path)
    return ext


def _vn(n: int | float) -> str:
    """Số theo cách viết tiếng Việt: 1.234.567."""
    return f"{n:,}".replace(",", ".")


def _dump(obj: Any, *, as_json: bool, out: str | None, human: str) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2) if (as_json or out) else human
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"Đã ghi {out}")
    else:
        print(text)


def cmd_extract(args: argparse.Namespace) -> int:
    ext = _load(args.path)
    payload = {
        "title": ext.title,
        "source_type": ext.source_type,
        "language_code": ext.language_code,
        "language_confidence": ext.language_confidence,
        "word_count": ext.word_count,
        "token_estimate": ext.token_estimate,
        "extraction_quality": ext.extraction_quality,
        "warnings": ext.warnings,
        "paragraphs": [p.as_row() for p in ext.paragraphs],
        "sections": [s.as_row() for s in ext.sections],
    }
    lines = [ext.summary(), "", "Mục:"]
    for s in ext.sections:
        lines.append(f"  {'  ' * (s.level - 1)}{s.section_id} {s.title} ({s.first_pid}→{s.last_pid})")
    _dump(payload, as_json=args.json, out=args.out, human="\n".join(lines))
    return 0


def cmd_segment(args: argparse.Namespace) -> int:
    ext = _load(args.path)
    segs = segment_extraction(ext, mode=args.mode, target_tokens=args.target_tokens, window_scale=args.window_scale)
    payload = {
        "title": ext.title,
        "mode": args.mode,
        "segments": [
            {
                **s.as_row(),
                "context_pids": [p.pid for p in s.context_before],
                "paragraphs": [p.pid for p in s.paragraphs],
            }
            for s in segs
        ],
    }
    total = sum(s.token_count for s in segs)
    lines = [f"{ext.title}: {len(segs)} segment ({args.mode}), tổng ~{total} token"]
    for s in segs:
        ctx = f", ngữ cảnh trước: {', '.join(p.pid for p in s.context_before)}" if s.context_before else ""
        lines.append(
            f"  {s.segment_id} {s.first_pid}→{s.last_pid} ~{s.token_count} token, {len(s.paragraphs)} đoạn{ctx}"
        )
    _dump(payload, as_json=args.json, out=args.out, human="\n".join(lines))
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    ext = _load(args.path)
    on = date.fromisoformat(args.on)
    price = pick_price(STARTER_PRICES, args.model, on)
    lang = ext.language_code or "en"
    est = estimate(ext.word_count, args.level, price, lang=lang)
    calls = estimate_calls(ext.word_count, args.level, lang=lang)
    payload = {
        "level": args.level,
        "model": price.model,
        "price_effective_from": price.effective_from.isoformat(),
        "lang": lang,
        "words": ext.word_count,
        "report_budget_words": report_budget_words(ext.word_count, args.level),
        "tokens_in": est.tokens_in,
        "tokens_out": est.tokens_out,
        "cost_usd": est.cost_usd,
        "credits": est.credits,
        "minutes_low": est.minutes_low,
        "minutes_high": est.minutes_high,
        "llm_calls": calls,
        "docs_per_day_on_one_free_project": round(docs_per_day(237, None, calls, est.tokens_in), 2),
    }
    cost_txt = f"${est.cost_usd:.4f}" if est.cost_usd < 0.01 else f"${est.cost_usd:.2f}"
    human = (
        f"{ext.title} — {_vn(ext.word_count)} từ ({lang}), mức {args.level}, model {price.model} "
        f"(giá từ {price.effective_from:%d/%m/%Y})\n"
        f"  tín dụng: {_vn(est.credits)}\n"
        f"  token: vào {_vn(est.tokens_in)} / ra {_vn(est.tokens_out)}\n"
        f"  chi phí LLM: {cost_txt}; ngân sách báo cáo: {_vn(payload['report_budget_words'])} từ\n"
        f"  thời gian ước tính: {est.minutes_low}–{est.minutes_high} phút\n"
        f"  số lời gọi LLM: {_vn(calls)}; một dự án free (237 lời gọi/ngày) đủ cho ~"
        f"{payload['docs_per_day_on_one_free_project']} tài liệu/ngày"
    )
    _dump(payload, as_json=args.json, out=args.out, human=human)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Chạy trọn pipeline P0–P8 trên kịch bản giả (M0-W2)."""
    from visynth.eval import demo_client, demo_extraction
    from visynth.pipeline.models import JobOptions
    from visynth.pipeline.run import run_document

    options = JobOptions(level=args.level)
    client_note = ""
    ledger = None
    if args.path and not args.demo:
        if not args.pool_config:
            print(
                "LỖI: chạy tài liệu thật cần `--pool-config <pool_config.json>` (LLM Pool thật, M0-W3). "
                "Chạy `visynth run --demo` để thử đường P0–P8 với client giả.",
                file=sys.stderr,
            )
            return 2
        from visynth.pool.client import Ledger, PooledLLMClient

        cfg = json.loads(Path(args.pool_config).read_text(encoding="utf-8"))
        ledger = Ledger(Path(args.ledger) if args.ledger else None)
        client = PooledLLMClient.from_config(
            cfg,
            ledger=ledger,
            gate=args.gate,
            privacy=args.privacy,
            priority=args.priority,
            region_restricted=args.region_restricted,
            allow_metered=not args.no_metered,
        )
        ext = extract(args.path)
        client_note = f" (pool: {args.pool_config})"
        if args.ledger:
            client_note += f"; sổ llm_calls: {args.ledger}"
        result = run_document(ext, client, options, seed=args.seed)
    else:
        ext = demo_extraction()
        result = run_document(ext, demo_client(ext, tamper=args.tamper, level=args.level), options, seed=args.seed)
    if args.out:
        Path(args.out).write_text(result.markdown + "\n", encoding="utf-8")
    payload = {
        "title": result.title,
        "level": result.level,
        "grade": result.grade,
        "metrics": result.metrics,
        "stats": result.stats,
        "warnings": result.warnings,
        "sections": [
            {"id": s.id, "title_vi": s.title_vi, "unit_ids": s.unit_ids, "target_words": s.target_words}
            for s in result.sections
        ],
        "blocks": [
            {"block_id": b.block_id, "cites": b.cites, "verdict": b.verdict, "flagged": b.flagged, "removed": b.removed}
            for b in result.blocks
        ],
        "markdown": result.markdown,
        "llm_calls": ledger.totals() if ledger else None,
    }
    human = result.summary() + client_note
    if args.out:
        human += f"\n\nĐã ghi báo cáo Markdown: {args.out}"
    else:
        human += "\n\n" + result.markdown
    _dump(payload, as_json=args.json, out=None, human=human)
    return 0


def cmd_pool_keygen(args: argparse.Namespace) -> int:
    """Sinh khoá chủ cho kho khoá mã hoá (một lần cho mỗi máy)."""
    from visynth.pool.registry import Registry

    reg = Registry()
    if reg.master_file.exists() and not args.force:
        print(f"Đã có khoá chủ: {reg.master_file} (dùng --force để ghi đè; khoá cũ sẽ không giải mã được nữa)")
        return 1
    key = reg.keygen()
    print(f"Đã ghi khoá chủ vào {reg.master_file} (quyền 600).")
    print("Hãy sao lưu khoá này ở nơi an toàn (trình quản lý mật khẩu). Khoá chỉ hiển thị MỘT lần dưới đây:")
    print(key)
    print("Nếu mất khoá chủ, mọi khoá API đã lưu sẽ không giải mã được và phải nhập lại.")
    return 0


def cmd_pool_validate(args: argparse.Namespace) -> int:
    """Kiểm tra `pool_config` hợp lệ theo schema + các ràng buộc cấu trúc."""
    from visynth.pool.validate import validate_config

    payload = json.loads(Path(args.config).read_text(encoding="utf-8"))
    report = validate_config(payload)
    human = report["summary_vi"]
    if not report["valid"]:
        human += "\n" + "\n".join(f"  - {e}" for e in report["errors"])
    if report["warnings"]:
        human += "\nCảnh báo:\n" + "\n".join(f"  - {w}" for w in report["warnings"])
    _dump(report, as_json=args.json, out=None, human=human)
    return 0 if report["valid"] else 1


def cmd_pool_preview(args: argparse.Namespace) -> int:
    """Xem trước danh sách khoá dán vào (KHÔNG in khoá, KHÔNG lưu gì)."""
    from visynth.pool.policy_io import parse_keys

    text = Path(args.keys).read_text(encoding="utf-8") if Path(args.keys).exists() else args.keys
    entries = parse_keys(text)
    rows = [{"label": e.label, "last4": e.last4, "status": e.status, "reason": e.reason} for e in entries]
    human = "\n".join(
        f"  {r['label'] or '(không nhãn)'}: ••••{r['last4']} — {r['status']} {r['reason']}".rstrip() for r in rows
    )
    _dump(
        {"count": len(rows), "usable": sum(1 for r in rows if r["status"] == "ok"), "keys": rows},
        as_json=args.json,
        out=None,
        human=human or "Không nhận ra khoá nào.",
    )
    return 0


def cmd_pool_declare(args: argparse.Namespace) -> int:
    """Biên dịch khai báo nhà cung cấp/khoá (`declare`) — mặc định chỉ xem trước (dry-run)."""
    from visynth.pool.policy_io import compile_all, merge_fragment
    from visynth.pool.registry import Registry

    doc = _load_declaration(Path(args.declaration))
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else None
    reg = Registry()
    existing = frozenset(rec.get("fingerprint", "") for rec in reg.credentials.values())
    res = compile_all(doc, cfg, existing_fingerprints=existing)
    counts = res.preview["counts"]
    print(
        f"Khai báo: {counts.get('groups', 0)} nhóm, {counts.get('keys', 0)} khoá dùng được, "
        f"{counts.get('skipped_keys', 0)} khoá bị bỏ, {counts.get('deployments', 0)} deployment."
    )
    for g in res.preview["groups"]:
        print(
            f"  - nhóm {g['id']} ({g['tier']}, {g['data_policy']}): cổng {', '.join(g['allowed_gates'])}, "
            f"cờ {', '.join(g['tos_flags']) or 'không'}"
        )
    for k in res.preview["skipped_keys"]:
        print(f"  - bỏ khoá {k['label'] or '(không nhãn)'} ••••{k['last4']}: {k['reason']}")
    for w in res.warnings:
        print(f"  ! {w}")
    if res.errors:
        print("LỖI:")
        for e in res.errors:
            print(f"  - {e}")
        return 1
    if args.store_secrets:
        for cid, secret in res.secrets.items():
            reg.add(cid, secret, replace=args.force)
        print(f"Đã lưu {len(res.secrets)} khoá vào kho mã hoá {reg.path} (khoá chủ ở {reg.master_file}).")
        print("Đặt secret_ref của các credential thành 'enc:<id>' trong pool_config.")
    else:
        print(f"(chưa lưu khoá nào — thêm --store-secrets để lưu {len(res.secrets)} khoá vào kho mã hoá)")
    if args.write:
        if not args.config:
            print("LỖI: --write cần --config để biết ghi vào đâu", file=sys.stderr)
            return 2
        merged = merge_fragment(cfg, res.fragment)
        Path(args.config).write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Đã ghi {args.config} ({len(res.fragment['deployments'])} deployment mới).")
    else:
        print("(dry-run: chưa ghi pool_config — thêm --write để áp dụng)")
    return 0


def cmd_pool_models(args: argparse.Namespace) -> int:
    """Liệt kê model khả dụng của từng provider — KHÔNG tốn token, dùng để chọn id thật cho `pool_config`."""
    from visynth.pool.adapters import list_models
    from visynth.pool.registry import resolve_key

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    wanted = set(args.provider or [])
    report: dict[str, Any] = {}
    failed = 0
    for provider in cfg.get("providers", []):
        if wanted and provider.get("id") not in wanted:
            continue
        ref = next(
            (
                c.get("secret_ref")
                for g in cfg.get("groups", [])
                if g.get("provider") == provider.get("id")
                for c in g.get("credentials", [])
                if c.get("secret_ref")
            ),
            None,
        )
        if not ref:
            report[provider["id"]] = {"models": [], "error": "không có secret_ref cho provider này"}
            failed += 1
            continue
        try:
            report[provider["id"]] = {
                "base_url": provider["base_url"],
                "models": list_models(provider, resolve_key(ref), timeout_s=args.timeout),
            }
        except Exception as exc:  # lỗi mạng/khoá: in ra, không làm hỏng các provider khác
            report[provider["id"]] = {"models": [], "error": str(exc)}
            failed += 1
    lines: list[str] = []
    for pid, info in report.items():
        if info.get("error"):
            lines.append(f"  - {pid}: LỖI — {info['error']}")
            continue
        models = info["models"]
        lines.append(f"  - {pid}: {len(models)} model khả dụng")
        for name in models:
            lines.append(f"      {name}")
    human = "Model theo provider (không tốn token, không in khoá):\n" + "\n".join(lines)
    if not report:
        human += "\n  (không có provider nào khớp --provider)"
    human += "\nChọn id thật rồi sửa `models[].id` trong pool_config; sau đó chạy `visynth pool probe`."
    _dump(report, as_json=args.json, out=None, human=human)
    return 1 if failed else 0


def cmd_pool_probe(args: argparse.Namespace) -> int:
    """Chạy `probe` trên từng deployment (cần khoá thật trong môi trường hoặc kho mã hoá)."""
    from visynth.pool.probe import probe_pool, write_report

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    report = probe_pool(cfg, only=args.only or None, with_vi_write=not args.no_vi_write, timeout_s=args.timeout)
    for r in report["runs"]:
        flags = ["json " + ("đạt" if r["json"]["ok"] else "KHÔNG đạt")]
        if r["vi_write"] is not None:
            flags.append("vi_write " + ("đạt" if r["vi_write"]["ok"] else "KHÔNG đạt"))
        if r["errors"]:
            flags.append("lỗi: " + "; ".join(r["errors"])[:120])
        print(f"  - {r['deployment_id']}: " + " | ".join(flags))
    print(
        f"Tổng: {report['totals']['json_ok']}/{report['totals']['deployments']} đạt JSON, "
        f"{report['totals']['vi_write_ok']} đạt vi_write."
    )
    if report["suggest_updates"]:
        print("Gợi ý cập nhật pool_config (dán tay, hệ thống không tự sửa):")
        print(json.dumps(report["suggest_updates"], ensure_ascii=False, indent=2))
    if args.out:
        write_report(report, args.out)
        print(f"Đã ghi báo cáo kiểm định: {args.out}")
    return 0


def cmd_pool_simulate(args: argparse.Namespace) -> int:
    """Mô phỏng rời rạc LLM Pool (SPEC §17.12) — không gọi mạng."""
    from visynth.pool.simulate import main as simulate_main

    argv = ["--docs", str(args.docs)]
    if args.config:
        argv += ["--config", args.config]
    if args.json:
        argv.append("--json")
    return int(simulate_main(argv) or 0)


def _load_declaration(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:  # pragma: no cover - pyyaml là phụ thuộc dev
            raise ValueError("cần pyyaml để đọc khai báo YAML (hoặc dùng bản .json)") from None
        return yaml.safe_load(text)
    return json.loads(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="visynth", description="ViSynth — prototype CLI (M0)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", help="bóc tách tài liệu (TXT/MD/DOCX)")
    p.add_argument("path")
    p.add_argument("--json", action="store_true", help="in JSON đầy đủ")
    p.add_argument("--out", help="ghi kết quả ra tệp JSON")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("segment", help="chia segment tất định (SPEC §6.3)")
    p.add_argument("path")
    p.add_argument("--mode", choices=["map", "translate"], default="map")
    p.add_argument("--target-tokens", type=int, default=None)
    p.add_argument("--window-scale", type=float, default=1.0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_segment)

    p = sub.add_parser("estimate", help="ước tính tín dụng/chi phí/thời gian")
    p.add_argument("path")
    p.add_argument("--level", choices=list(LEVELS), default="deep_synthesis")
    p.add_argument("--model", default="gemini-3.8-flash")
    p.add_argument("--on", default=date.today().isoformat(), help="ngày áp giá (YYYY-MM-DD)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("run", help="chạy trọn P0–P8 trên kịch bản giả (M0-W2)")
    p.add_argument("path", nargs="?", help="bỏ trống hoặc dùng tệp mẫu (kịch bản giả chỉ có tài liệu demo)")
    p.add_argument("--demo", action="store_true", help="chạy tài liệu demo có sẵn")
    p.add_argument("--tamper", choices=TAMPER_MODES, default=None, help="biến thể phá hoại để thử P7")
    p.add_argument(
        "--level", choices=["detailed_synthesis", "deep_synthesis", "executive_brief"], default="deep_synthesis"
    )
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--pool-config", help="chạy tài liệu thật qua LLM Pool (M0-W3) với tệp pool_config JSON này")
    p.add_argument("--ledger", help="ghi sổ llm_calls ra JSONL (mặc định: chỉ giữ trong bộ nhớ)")
    p.add_argument("--gate", choices=["dev", "A", "B", "C"], default="dev")
    p.add_argument("--privacy", choices=["standard", "private"], default="standard")
    p.add_argument("--priority", choices=["high", "normal", "low"], default="normal")
    p.add_argument(
        "--region-restricted", action="store_true", help="người dùng ở EEA/CH/UK: tránh nhóm gắn cờ no_eea_uk_ch"
    )
    p.add_argument("--no-metered", action="store_true", help="không dùng deployment trả phí")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", help="ghi báo cáo Markdown ra tệp")
    p.set_defaults(func=cmd_run)

    pool = sub.add_parser("pool", help="LLM Pool thật (SPEC §17): khoá, khai báo, kiểm định, mô phỏng")
    ps = pool.add_subparsers(dest="pool_cmd", required=True)

    q = ps.add_parser("keygen", help="sinh khoá chủ cho kho khoá mã hoá (một lần cho mỗi máy)")
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=cmd_pool_keygen)

    q = ps.add_parser("validate", help="kiểm tra pool_config theo schema và ràng buộc cấu trúc")
    q.add_argument("--config", required=True)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_pool_validate)

    q = ps.add_parser("models", help="liệt kê model khả dụng của từng provider (không tốn token)")
    q.add_argument("--config", required=True)
    q.add_argument("--provider", action="append", help="chỉ hỏi provider này (lặp lại được)")
    q.add_argument("--timeout", type=float, default=30.0)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_pool_models)

    q = ps.add_parser("preview", help="xem trước danh sách khoá dán vào (không in khoá)")
    q.add_argument("--keys", required=True, help="chuỗi khoá hoặc đường dẫn tệp chứa khoá")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_pool_preview)

    q = ps.add_parser("declare", help="biên dịch khai báo nhà cung cấp/khoá (mặc định dry-run)")
    q.add_argument("--declaration", required=True, help="tệp khai báo JSON/YAML")
    q.add_argument("--config", help="pool_config hiện có để nối thêm (bỏ trống = khai mới từ đầu)")
    q.add_argument("--write", action="store_true", help="ghi fragment vào --config")
    q.add_argument("--store-secrets", action="store_true", help="lưu khoá vào kho mã hoá (cần --config để dùng tiếp)")
    q.add_argument("--force", action="store_true", help="ghi đè khoá đã có trong kho")
    q.set_defaults(func=cmd_pool_declare)

    q = ps.add_parser("probe", help="chạy bài kiểm định trên từng deployment")
    q.add_argument("--config", required=True)
    q.add_argument("--only", action="append", help="chỉ kiểm định deployment này (lặp lại được)")
    q.add_argument("--no-vi-write", action="store_true", help="bỏ phần kiểm tra viết tiếng Việt")
    q.add_argument("--timeout", type=float, default=90.0)
    q.add_argument("--out", help="ghi báo cáo kiểm định JSON")
    q.set_defaults(func=cmd_pool_probe)

    q = ps.add_parser("simulate", help="mô phỏng rời rạc pool (không gọi mạng)")
    q.add_argument("--config", help="pool_config JSON (mặc định: bản mẫu trong docs/examples)")
    q.add_argument("--docs", type=int, default=3, help="số tài liệu mô phỏng")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_pool_simulate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ExtractionError as exc:
        print(f"LỖI [{exc.code}] {exc.message}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"LỖI: không tìm thấy tệp {exc.filename}", file=sys.stderr)
        return 1
    except (ValueError, LookupError) as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
