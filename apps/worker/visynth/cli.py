"""CLI của M0 — prototype chạy trên máy, chưa cần CSDL và chưa cần pool thật.

visynth extract  <tệp>                       # bóc tách → tóm tắt hoặc JSON
visynth segment  <tệp> --mode map|translate  # chia segment
visynth estimate <tệp> --level <mức>         # ước tính tín dụng/chi phí/thời gian
visynth run      --demo [--tamper <biến thể>]  # chạy trọn P0–P8 trên kịch bản giả (M0-W2)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
    from visynth.pipeline.artifacts import to_artifact, write_artifact
    from visynth.pipeline.models import JobOptions
    from visynth.pipeline.run import run_document

    style_text = ""
    if getattr(args, "style_core", None):
        from visynth.stylecore import StyleCoreError, StyleCoreStore

        store = StyleCoreStore.load(args.store) if args.store else StyleCoreStore.load()
        try:
            style_text = store.compile(args.style_core, "write")
        except StyleCoreError as exc:
            print(f"LỖI: {exc}", file=sys.stderr)
            return 1
    options = JobOptions(level=args.level, style_core_text=style_text)
    started = time.monotonic()
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
    payload = to_artifact(
        result,
        source={"path": args.path, "words": result.stats.get("source_words"), "title": result.title},
        params={
            "level": result.level,
            "seed": args.seed,
            "demo": bool(args.demo or not args.path),
            "tamper": getattr(args, "tamper", None),
        },
        duration_ms=int((time.monotonic() - started) * 1000),
        llm_calls=ledger.totals() if ledger else None,
    )
    if args.artifacts:
        write_artifact(payload, args.artifacts)
    human = result.summary() + client_note
    if args.artifacts:
        human += f"\nĐã ghi bộ tệp cho bộ chấm điểm: {args.artifacts}/run.json, {args.artifacts}/report.md"
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


def cmd_pool_apply_probe(args: argparse.Namespace) -> int:
    """Nhập số đo của `probe` (và hạn mức đo tay) vào `pool_config` — mặc định chỉ xem trước."""
    from visynth.pool.apply_probe import apply_updates, plan_updates, write_config

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    probe = json.loads(Path(args.probe).read_text(encoding="utf-8"))
    limits = json.loads(Path(args.limits).read_text(encoding="utf-8")) if args.limits else None
    plan = plan_updates(cfg, probe, limits, today=args.today)
    if not plan["changes"]:
        print("Không có thay đổi nào — cấu hình đã khớp số đo (hoặc probe rỗng).")
        for note in plan["notes"]:
            print(f"  lưu ý: {note}")
        return 0
    new_cfg, errors = apply_updates(cfg, plan)
    if errors:
        print("LỖI: cấu hình sau khi áp không hợp lệ, KHÔNG ghi:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(("Đã ghi " if args.write else "[xem trước] sẽ áp ") + f"{len(plan['changes'])} thay đổi vào {args.config}:")
    for change in plan["changes"]:
        src = f" ({change['source']})" if change.get("source") else ""
        print(f"  - {change['where']}: {change['from']} → {change['to']}{src}")
    for item in plan["skipped"]:
        print(f"  bỏ qua: {item}")
    if args.write:
        write_config(new_cfg, args.config)
        print(f"Đã ghi {args.config} (đã qua validate_config).")
    else:
        print("Thêm --write để ghi. Gợi ý: chạy `visynth pool validate --config " + args.config + "` sau khi ghi.")
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


def _store(args: argparse.Namespace):
    from visynth.stylecore import StyleCoreStore

    return StyleCoreStore.load(args.store) if args.store else StyleCoreStore.load()


def _style_error(exc: Exception) -> int:
    """Lỗi vòng đời lõi là lỗi NGƯỜI DÙNG (trạng thái chưa cho phép), không phải sự cố — in gọn, đừng traceback."""
    print(f"LỖI: {exc}", file=sys.stderr)
    return 1


def _core_content(args: argparse.Namespace) -> dict:
    return json.loads(Path(args.core).read_text(encoding="utf-8"))


def cmd_style_lint(args: argparse.Namespace) -> int:
    """Lint nội dung lõi (tệp JSON) — không cần kho."""
    from visynth.stylecore import lint

    problems = lint(_core_content(args))
    if args.json:
        _dump([p.__dict__ for p in problems], as_json=True, out=None, human="")
        return 0
    if not problems:
        print("Lint sạch: không có vấn đề nào.")
        return 0
    for p in problems:
        print(f"  {p.severity}: {p.path} — {p.message}")
    errors = [p for p in problems if p.severity == "error"]
    print(f"Tổng: {len(errors)} lỗi, {len(problems) - len(errors)} cảnh báo.")
    return 1 if errors else 0


def cmd_style_compile(args: argparse.Namespace) -> int:
    """Biên dịch nội dung lõi thành khối `{{style_core}}` cho một giai đoạn."""
    from visynth.stylecore import compile_style_core

    text = compile_style_core(_core_content(args), args.stage, args.max_chars)
    if args.json:
        _dump({"stage": args.stage, "chars": len(text), "text": text}, as_json=True, out=None, human="")
    else:
        print(text)
    return 0


def cmd_style_init(args: argparse.Namespace) -> int:
    """Tạo lõi mới trong kho từ lõi TRUNG TÍNH (không đặt quan điểm sẵn) — điểm bắt đầu §19.9 bước 1."""
    import copy

    from visynth.prompts import default_prompts_dir

    store = _store(args)
    neutral_path = default_prompts_dir() / "00_style_core_neutral.json"
    content = copy.deepcopy(json.loads(neutral_path.read_text(encoding="utf-8")))
    content["name_vi"] = args.name_vi
    content["domain"] = args.domain
    content["summary_vi"] = args.summary or ""
    if args.parent:
        parent = store.snapshot(args.parent)
        if parent["status"] != "approved":
            print(f"LỖI: lõi cha {args.parent} chưa được duyệt ({parent['status']}).", file=sys.stderr)
            return 1
        content["parent_id"] = parent["core_id"]
    from visynth.stylecore import StyleCoreError

    try:
        store.create(args.core_id, content, parent_id=args.parent, by=args.by, version=args.version)
        store.save()
    except StyleCoreError as exc:
        return _style_error(exc)
    print(f"Đã tạo lõi '{args.core_id}' phiên bản {args.version} (draft) trong {store.path}")
    print("Bước tiếp: thêm quy tắc/ví dụ (người hoặc P12), `style decide` trả lời quyết định mở, `style approve`.")
    return 0


def cmd_style_status(args: argparse.Namespace) -> int:
    store = _store(args)
    rows = store.status_rows()
    if args.json:
        _dump(rows, as_json=True, out=None, human="")
        return 0
    if not rows:
        print(f"Kho trống: {store.path}")
        return 0
    print(f"Kho: {store.path}")
    for r in rows:
        parent = f" (cha {r['parent_id']})" if r["parent_id"] else ""
        print(
            f"  {r['core_id']}@{r['version']} [{r['status']}]{parent} — {r['rules']} quy tắc, {r['exemplars']} ví dụ, "
            f"{r['open_decisions']} quyết định mở, sha {r['sha']}"
            + (f", duyệt bởi {r['approved_by']}" if r["approved_by"] else "")
        )
    return 0


def cmd_style_show(args: argparse.Namespace) -> int:
    store = _store(args)
    found = store.find(args.version_id)
    if found is None:
        print(f"LỖI: không tìm thấy lõi '{args.version_id}' trong {store.path}", file=sys.stderr)
        return 1

    core, v = found
    payload = {
        "core_id": core["id"],
        **{
            k: v[k]
            for k in (
                "version",
                "status",
                "content_sha256",
                "open_decisions",
                "answers",
                "proposal_evidence",
                "proposal_risks",
            )
        },
        "content": v["content"],
    }
    if args.json:
        _dump(payload, as_json=True, out=None, human="")
        return 0
    print(f"{core['id']}@{v['version']} — {v['status']}, sha {v['content_sha256'][:12]}")
    if v.get("proposal_evidence"):
        print("Bằng chứng của đề xuất:")
        for e in v["proposal_evidence"]:
            print(f"  {e['target_id']} ← {e['source_ref']} ({e['kind']}): {e.get('excerpt', '')[:140]}")
    if v.get("proposal_risks"):
        print("Rủi ro P12 nêu: " + "; ".join(v["proposal_risks"]))
    if v["open_decisions"]:
        print("Quyết định mở:")
        for d in v["open_decisions"]:
            mark = "✔" if d["id"] in v["answers"] else "…"
            print(f"  {mark} {d['id']}: {d.get('question', '')} → {v['answers'].get(d['id'], '(chưa trả lời)')}")
    print(json.dumps(v["content"], ensure_ascii=False, indent=2))
    return 0


def cmd_style_decide(args: argparse.Namespace) -> int:
    """Trả lời một quyết định mở của P12 (§19.4)."""
    from visynth.stylecore import StyleCoreError

    store = _store(args)
    core_id, _, version = args.version_id.partition("@")
    decision_id, _, answer = args.answer.partition("=")
    try:
        store.answer(core_id, decision_id, answer, version=version or None, by=args.by)
        store.save()
    except StyleCoreError as exc:
        return _style_error(exc)
    print(f"Đã ghi {decision_id} = {answer!r} cho {args.version_id}.")
    return 0


def cmd_style_approve(args: argparse.Namespace) -> int:
    """Duyệt một phiên bản — bị chặn nếu còn quyết định mở / quy tắc AI chưa xem / lint có lỗi (§19.3)."""
    from visynth.stylecore import ApprovalBlocked, StyleCoreError

    store = _store(args)
    core_id, _, version = args.version_id.partition("@")
    try:
        v = store.approve(core_id, by=args.by, version=version or None)
    except ApprovalBlocked as exc:
        print("KHÔNG duyệt được — còn vướng:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    except StyleCoreError as exc:
        return _style_error(exc)
    store.save()
    print(f"Đã duyệt {core_id}@{v['version']} bởi {v['approved_by']} (sha {v['content_sha256'][:12]}).")
    print("Phiên bản đã duyệt là BẤT BIẾN: muốn sửa thì `style bump` rồi duyệt bản mới.")
    return 0


def cmd_style_bump(args: argparse.Namespace) -> int:
    from visynth.stylecore import StyleCoreError

    store = _store(args)
    core_id, _, version = args.version_id.partition("@")
    try:
        v = store.bump(core_id, args.kind, by=args.by)
        store.save()
    except StyleCoreError as exc:
        return _style_error(exc)
    print(f"Đã tạo bản nháp {core_id}@{v['version']} từ bản trước ({args.kind}).")
    return 0


def cmd_style_deprecate(args: argparse.Namespace) -> int:
    from visynth.stylecore import StyleCoreError

    store = _store(args)
    core_id, _, version = args.version_id.partition("@")
    try:
        v = store.deprecate(core_id, by=args.by, version=version or None, reason=args.reason or "")
    except StyleCoreError as exc:
        return _style_error(exc)
    store.save()
    print(f"Đã ngừng dùng {core_id}@{v['version']}.")
    return 0


def cmd_style_compare(args: argparse.Namespace) -> int:
    """Test-drive §19.9 bước 5/7: chạy cùng tài liệu với lõi trung tính và lõi ứng viên, so chỉ số.

    Chỉ số chất lượng văn phong là việc của người chấm (§16.2) — ở đây so phần ĐO ĐƯỢC và cảnh báo nếu lõi
    không chứng minh được lợi ích (mọi chỉ số ngang nhau) để tránh dùng lõi chỉ vì đã viết ra.
    """
    from visynth.eval import demo_client, demo_extraction
    from visynth.pipeline.models import JobOptions
    from visynth.pipeline.run import run_document
    from visynth.prompts import neutral_style_core
    from visynth.stylecore import StyleCoreError

    store = _store(args)
    try:
        candidate = store.compile(args.version_id, "write")
    except StyleCoreError as exc:
        return _style_error(exc)

    def _run(style_text: str | None):
        if args.pool_config:
            from visynth.pool.client import Ledger, PooledLLMClient

            cfg = json.loads(Path(args.pool_config).read_text(encoding="utf-8"))
            client = PooledLLMClient.from_config(
                cfg, ledger=Ledger(Path(args.ledger) if args.ledger else None), gate=args.gate
            )
            ext = extract(args.path)
        else:
            ext = demo_extraction()
            client = demo_client(ext, level=args.level)
        options = JobOptions(level=args.level, style_core_text=style_text or neutral_style_core("write"))
        return run_document(ext, client, options, seed=args.seed)

    neutral, with_core = _run(None), _run(candidate)
    keys = ("coverage_core", "faithfulness_rate", "unresolved_blocks")
    rows = []
    for key in keys:
        a, b = neutral.metrics.get(key), with_core.metrics.get(key)
        rows.append((key, a, b, (b - a) if (isinstance(a, (int, float)) and isinstance(b, (int, float))) else None))
    payload = {
        "core": args.version_id,
        "level": args.level,
        "compiled_chars": len(candidate),
        "metrics": [{"metric": k, "neutral": a, "with_core": b, "delta": d} for k, a, b, d in rows],
        "grade": {"neutral": neutral.grade, "with_core": with_core.grade},
        "verdict": (
            "chưa chứng minh được lợi ích (chỉ số đo được không đổi) — cần chấm người ở hạng mục Văn phong"
            if all(d == 0 for _, _, _, d in rows if d is not None)
            else "có khác biệt ở chỉ số đo được — chấm người trước khi kết luận"
        ),
    }
    human = (
        f"So lõi trung tính với {args.version_id} trên {args.path or 'kịch bản giả'} (mức {args.level}):\n"
        + "\n".join(f"  {k}: {a} → {b}" + (f" ({d:+.3f})" if d is not None else "") for k, a, b, d in rows)
        + f"\n  khối biên dịch: {len(candidate)} ký tự"
        + f"\nKết luận: {payload['verdict']}"
        + "\nNhắc §19.9 bước 7: lõi chỉ được dùng nếu THẮNG lõi trung tính ở điểm Văn phong mà không làm hỏng chỉ số khác."
    )
    _dump(payload, as_json=args.json, out=args.out, human=human)
    return 0


def _curate_client(args: argparse.Namespace):
    """Client cho đường curation: `None` = chạy khô bằng đề xuất giả (không tốn token)."""
    if getattr(args, "demo", False) or not getattr(args, "pool_config", None):
        return None, "chạy khô (đề xuất giả, không gọi LLM)"
    from visynth.pool.client import Ledger, PooledLLMClient

    cfg = json.loads(Path(args.pool_config).read_text(encoding="utf-8"))
    ledger = Ledger(Path(args.ledger) if getattr(args, "ledger", None) else None)
    client = PooledLLMClient.from_config(cfg, ledger=ledger, gate=args.gate, privacy="private")
    return client, f"pool: {args.pool_config}"


def _read_samples(directory: str) -> dict[str, str]:
    root = Path(directory)
    if not root.is_dir():
        return {}
    docs = {}
    for path in sorted(root.iterdir()):
        if path.suffix.lower() in (".txt", ".md") and path.is_file():
            docs[path.stem] = path.read_text(encoding="utf-8")
    return docs


def _read_json(path: str | None, default):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else default


def cmd_style_propose(args: argparse.Namespace) -> int:
    """§19.9 bước 1-3: P12 đề xuất từ tài liệu mẫu + cặp tham chiếu → bản NHÁP có quyết định mở."""
    from visynth.stylecore import StyleCoreError, StyleCoreStore
    from visynth.stylecore.propose import excerpts_from_texts, normalise_pairs, propose

    docs = _read_samples(args.samples)
    if not docs:
        print(f"LỖI: không có tệp .txt/.md nào trong {args.samples}", file=sys.stderr)
        return 2
    brief = Path(args.brief).read_text(encoding="utf-8")
    samples = excerpts_from_texts(docs)
    pairs = normalise_pairs(_read_json(args.pairs, []))
    feedback = [
        {"id": f"FB{i:02d}", "text": f if isinstance(f, str) else f.get("text", "")}
        for i, f in enumerate(_read_json(args.feedback, []), 1)
    ]
    client, note = _curate_client(args)
    store = StyleCoreStore.load(args.store) if args.store else StyleCoreStore.load()
    existing = None
    if args.refine:
        found = store.find(args.refine)
        if found is None:
            print(f"LỖI: không tìm thấy lõi '{args.refine}' để tinh chỉnh", file=sys.stderr)
            return 1
        existing = found[1]["content"]
    try:
        result = propose(
            client,
            brief=brief,
            samples=samples,
            pairs=pairs,
            feedback=feedback,
            existing_core=existing,
            name_vi=args.name_vi,
            domain=args.domain,
        )
    except Exception as exc:  # lỗi mạng/schema của P12: báo gọn
        print(f"LỖI: P12 thất bại — {exc}", file=sys.stderr)
        return 1
    payload = {
        "core_id": args.core_id,
        "mode": "refine" if existing else "bootstrap",
        "client": note,
        "samples": len(samples),
        "pairs": len(pairs),
        "summary_vi": result.summary_vi,
        "confidence": result.confidence,
        "risks_vi": result.risks_vi,
        "notes": result.notes,
        "rules": len(result.content["rules"]),
        "exemplars": len(result.content["exemplars"]),
        "decisions_needed": result.decisions,
        "evidence": result.evidence,
        "content": result.content,
    }
    if not args.dry_run:
        try:
            store.create(
                args.core_id,
                result.content,
                parent_id=args.parent,
                open_decisions=result.decisions,
                proposal_evidence=result.evidence,
                proposal_risks=result.risks_vi,
                by=args.by,
            )
            store.save()
        except StyleCoreError as exc:
            return _style_error(exc)
        payload["store"] = str(store.path)
        payload["next"] = [
            f"visynth style show {args.core_id}",
            f'visynth style decide {args.core_id} --answer D01="…" --by {args.by or "ban"}',
            f"visynth style confirm {args.core_id} --all --by {args.by or 'ban'}",
            f"visynth style approve {args.core_id} --by {args.by or 'ban'}",
        ]
    human = (
        f"P12 ({'tinh chỉnh' if existing else 'khởi tạo'}) — {note}\n"
        f"  đầu vào: {len(samples)} đoạn mẫu, {len(pairs)} cặp tham chiếu, {len(feedback)} phản hồi\n"
        f"  đề xuất: {len(result.content['rules'])} quy tắc, {len(result.content['exemplars'])} ví dụ, "
        f"{len(result.decisions)} quyết định mở (độ tin cậy {result.confidence})\n"
        + (f"  tóm tắt: {result.summary_vi}\n" if result.summary_vi else "")
        + ("  đã loại (kỷ luật bằng chứng):\n" + "\n".join(f"    - {n}" for n in result.notes) if result.notes else "")
        + ("\n  " + "\n  ".join(payload.get("next", [])) if not args.dry_run else "\n(chạy khô: KHÔNG ghi vào kho)")
    )
    _dump(payload, as_json=args.json, out=args.out, human=human)
    return 0


def cmd_style_edit(args: argparse.Namespace) -> int:
    """Người duyệt sửa trực tiếp nội dung một bản nháp (bản đã duyệt là bất biến)."""
    from visynth.stylecore import StyleCoreError

    store = _store(args)
    core_id, _, version = args.version_id.partition("@")
    content = json.loads(Path(args.core).read_text(encoding="utf-8"))
    try:
        v = store.edit(core_id, content, version=version or None, by=args.by)
        store.save()
    except StyleCoreError as exc:
        return _style_error(exc)
    print(
        f"Đã sửa {core_id}@{v['version']} (sha mới {v['content_sha256'][:12]}). Lint: {len(store.lint_version(core_id + '@' + v['version']))} vấn đề."
    )
    return 0


def cmd_style_confirm(args: argparse.Namespace) -> int:
    """Xác nhận đã xem mục do AI đề xuất (`reviewed = true`) — điều kiện để duyệt được."""
    from visynth.stylecore import StyleCoreError

    store = _store(args)
    core_id, _, version = args.version_id.partition("@")
    if not args.all and not (args.ids or "").strip():
        print("LỖI: cần --all (xác nhận tất cả) hoặc --ids R01,E02", file=sys.stderr)
        return 2
    ids = None if args.all else [i.strip() for i in args.ids.split(",") if i.strip()]
    try:
        v = store.confirm_ai(core_id, ids, version=version or None, by=args.by)
        store.save()
    except StyleCoreError as exc:
        return _style_error(exc)
    print(
        f"Đã xác nhận trên {core_id}@{v['version']}: {', '.join(i['id'] for i in v['content']['rules'] + v['content']['exemplars'] if i.get('origin') == 'ai' and i.get('reviewed')) or '(không có)'}"
    )
    return 0


def _glossary_store(args: argparse.Namespace):
    from visynth.glossary import GlossaryStore

    return GlossaryStore.load(args.store) if args.store else GlossaryStore.load()


def cmd_glossary_propose(args: argparse.Namespace) -> int:
    """§19.5: ứng viên theo tài liệu → merge_candidates → P13 → bằng chứng do code ghép → mục `suggested`."""
    from visynth.glossary import harmonise

    per_doc: dict[str, list[dict]] = {}
    for path in args.candidates:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, list):
            per_doc[Path(path).stem] = data
        else:
            per_doc[data.get("doc_ref") or Path(path).stem] = data.get("candidates", [])
    raw_ctx = _read_json(args.contexts, {})
    contexts = {tuple(k.split("|", 1)): v for k, v in raw_ctx.items()}
    client, note = _curate_client(args)
    try:
        entries, dropped, merged = harmonise(
            client,
            per_doc=per_doc,
            contexts=contexts,
            policy=_read_json(args.policy, {}),
            existing=[
                {"source_term": e["source_term"], "target_term": e["target_term"]} for e in _glossary_store(args).rows()
            ]
            if args.existing
            else [],
            pairs=_read_json(args.pairs, []),
            max_entries=args.max_entries,
        )
    except Exception as exc:
        print(f"LỖI: P13 thất bại — {exc}", file=sys.stderr)
        return 1
    payload = {
        "client": note,
        "documents": len(per_doc),
        "merged": len(merged),
        "proposed": len(entries),
        "entries": entries,
        "dropped": dropped,
    }
    if not args.dry_run:
        store = _glossary_store(args)
        payload["suggested"] = store.suggest_many(entries, by="ai")
        store.save()
        payload["store"] = str(store.path)
        payload["next"] = [
            "visynth glossary status",
            "visynth glossary approve <source_term> --by <ban>",
            "visynth glossary publish --by <ban>",
        ]
    human = (
        f"P13 — {note}\n"
        f"  {len(per_doc)} tài liệu → {len(merged)} thuật ngữ gộp → {len(entries)} đề xuất, {len(dropped)} bị loại\n"
        + "\n".join(
            f"    {e['source_term']} → {e['target_term']} ({e['confidence']})"
            + (" [cần người]" if e.get("needs_human") else "")
            for e in entries[:20]
        )
        + (f"\n  kho: {payload.get('store')}" if payload.get("store") else "\n(chạy khô: KHÔNG ghi vào kho)")
    )
    _dump(payload, as_json=args.json, out=args.out, human=human)
    return 0


def cmd_glossary_status(args: argparse.Namespace) -> int:
    store = _glossary_store(args)
    rows = store.rows(args.status)
    if args.json:
        _dump(rows, as_json=True, out=None, human="")
        return 0
    if not rows:
        print(f"Glossary trống: {store.path}")
        return 0
    print(
        f"Glossary {store.data.get('glossary_id')}: {store.path} — {len(store.data['entries'])} mục, {len(store.data['releases'])} bản phát hành"
    )
    for r in rows:
        flags = " [CẦN NGƯỜI]" if r.get("needs_human") else ""
        rel = ",".join(r.get("released", []))
        print(
            f"  [{r['status']}]{flags} {r['source_term']} → {r['target_term']} (tin cậy {r['confidence']}, {len(r['evidence'])} bằng chứng{', đã phát hành ' + rel if rel else ''})"
        )
    return 0


def cmd_glossary_approve(args: argparse.Namespace) -> int:
    from visynth.glossary import GlossaryError

    store = _glossary_store(args)
    try:
        e = store.approve(args.source_term, by=args.by, target_term=args.target)
        store.save()
    except GlossaryError as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 1
    print(f"Đã duyệt '{e['source_term']}' → '{e['target_term']}'.")
    return 0


def cmd_glossary_reject(args: argparse.Namespace) -> int:
    from visynth.glossary import GlossaryError

    store = _glossary_store(args)
    try:
        e = store.reject(args.source_term, by=args.by, reason=args.reason or "")
        store.save()
    except GlossaryError as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 1
    print(f"Đã từ chối '{e['source_term']}' (mục vẫn được giữ để P1/P13 không đề xuất lại).")
    return 0


def cmd_glossary_publish(args: argparse.Namespace) -> int:
    from visynth.glossary import GlossaryError

    store = _glossary_store(args)
    try:
        release = store.publish(by=args.by, summary=args.summary or "")
        store.save()
    except GlossaryError as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 1
    diff = store.diff(release["release"])
    print(
        f"Đã phát hành {release['release']} ({release['count']} mục, sha {release['content_sha256'][:12]}) "
        f"— thêm {len(diff['added'])}, xoá {len(diff['removed'])}, đổi {len(diff['changed'])}."
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Worker production (M1): nhận task từ PostgreSQL rồi chạy pipeline P0–P8."""
    from visynth.worker.runner import JobWorker
    from visynth.worker.store import WorkerStore

    if not args.db_dsn:
        print("LỖI: cần --db-dsn (hoặc VISYNTH_DB_DSN)", file=sys.stderr)
        return 2
    style_text = ""
    if args.style_core:
        from visynth.stylecore import StyleCoreError, StyleCoreStore

        store = StyleCoreStore.load(args.store) if args.store else StyleCoreStore.load()
        try:
            style_text = store.compile(args.style_core, "write")
        except StyleCoreError as exc:
            print(f"LỖI: {exc}", file=sys.stderr)
            return 1

    def factory(job: dict):
        if args.fake or (not args.pool_config and not args.pool_from_db):
            from visynth.eval import demo_client, demo_extraction

            return demo_client(demo_extraction())
        from visynth.pool.client import Ledger, PooledLLMClient

        if args.pool_from_db:  # đường chạy của VPS: cấu hình + khoá nằm trong CSDL, khoá chủ ở ngoài
            from visynth.pool import dbstore
            from visynth.pool.pgstore import PgStore
            from visynth.pool.secrets import load_master_key

            key_text = os.environ.get("VISYNTH_POOL_MASTER_KEY") or os.environ.get("POOL_MASTER_KEY") or ""
            key_file = os.environ.get("VISYNTH_POOL_MASTER_KEY_FILE") or os.environ.get("POOL_MASTER_KEY_FILE") or ""
            if not key_text and key_file and Path(key_file).is_file():
                key_text = Path(key_file).read_text(encoding="utf-8").strip()
            if not key_text:
                print(
                    "LỖI: --pool-from-db cần POOL_MASTER_KEY hoặc POOL_MASTER_KEY_FILE để giải mã khoá API",
                    file=sys.stderr,
                )
                return None
            store = PgStore(args.db_dsn)
            return PooledLLMClient.from_db(
                store,
                load_master_key(key_text),
                user_id=(job or {}).get("user_id"),
                stage=(job or {}).get("current_stage"),
                ledger=Ledger(Path(args.ledger) if args.ledger else None)
                if args.ledger
                else dbstore.DbLedger(store, user_id=(job or {}).get("user_id")),
                gate=args.gate,
            )
        cfg = json.loads(Path(args.pool_config).read_text(encoding="utf-8"))
        return PooledLLMClient.from_config(
            cfg, ledger=Ledger(Path(args.ledger) if args.ledger else None), gate=args.gate
        )

    store = WorkerStore(args.db_dsn, worker_id=args.worker_id)
    worker = JobWorker(store, factory, limit=args.limit, per_job_limit=args.per_job, style_core_text=style_text)
    mode = "một lượt" if args.once else "vòng lặp"
    if args.fake or not (args.pool_config or args.pool_from_db):
        source = "client giả"
    elif args.pool_from_db:
        source = "pool trong CSDL"
    else:
        source = f"pool từ tệp {args.pool_config}"
    print(f"worker {args.worker_id}: {mode} (limit {args.limit}, mỗi job {args.per_job}) — {source}")
    if args.once:
        report = worker.run_once()
        print(
            f"đã xử lý {report['claimed']} task" + (f", thu hồi {report['reclaimed']}" if report["reclaimed"] else "")
        )
        for item in report["results"]:
            print("  ", item)
        return 0
    worker.run_forever(poll_s=args.poll, idle_exit_after=args.idle_exit or None)
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    """Tiện ích CSDL: áp baseline Alembic (`migrate`) hoặc chạy schema.sql trực tiếp (`init`)."""
    if args.db_cmd == "migrate":
        if not args.db_dsn:
            print("LỖI: cần --db-dsn (hoặc VISYNTH_DB_DSN)", file=sys.stderr)
            return 2
        from visynth_api.migrate import upgrade

        upgrade(args.db_dsn)
        print("đã chạy Alembic tới head")
        return 0
    if args.db_cmd == "init":
        from pathlib import Path as _Path

        from visynth_api.db import Database, apply_schema

        root = _Path(__file__).resolve().parents[3]
        db = Database(args.db_dsn)
        apply_schema(db, str(root / "docs" / "db" / "schema.sql"))
        db.close()
        print("đã áp docs/db/schema.sql")
        return 0
    if args.db_cmd == "ping":
        from visynth_api.db import Database

        db = Database(args.db_dsn, min_size=0, max_size=1)
        print("server_version:", db.scalar("SELECT current_setting('server_version')"))
        db.close()
        return 0
    print("LỖI: chưa hỗ trợ", file=sys.stderr)
    return 2


def cmd_parse_server(args: argparse.Namespace) -> int:
    """Chạy dịch vụ parser sandbox (§6.1/§20.4) — container riêng, không mạng ra ngoài, read-only."""
    from visynth.extract import MAX_FILE_BYTES
    from visynth.worker.parser_service import serve

    serve(
        args.host,
        args.port,
        max_bytes=args.max_bytes or MAX_FILE_BYTES,
        timeout_s=args.timeout,
    )
    return 0


def cmd_ops(args: argparse.Namespace) -> int:
    """Bảo trì định kỳ (SPEC §20): thu hồi task/chỗ đặt, xoá nội dung hết hạn, đọc số liệu sức khoẻ."""
    from visynth.worker import ops

    if not args.db_dsn:
        print("LỖI: cần --db-dsn (hoặc VISYNTH_DB_DSN)", file=sys.stderr)
        return 2
    try:
        if args.ops_cmd == "reap":
            payload = ops.reap(args.db_dsn, stale=args.stale, dry_run=args.dry_run)
        elif args.ops_cmd == "purge":
            payload = ops.purge(args.db_dsn, dry_run=args.dry_run)
        else:
            payload = ops.health(args.db_dsn)
    except Exception as exc:  # noqa: BLE001 - cron/giám sát phải thấy mã thoát khác 0
        print(f"LỖI: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(ops.as_json(payload))
    elif args.ops_cmd == "reap":
        print(
            f"thu hồi {payload['tasks']} task và {payload['leases']} chỗ đặt"
            + ("" if payload["applied"] else " (chỉ xem trước)")
        )
    elif args.ops_cmd == "purge":
        print(
            f"xoá nội dung gốc của {payload['documents']} tài liệu" + ("" if payload["applied"] else " (chỉ xem trước)")
        )
        for key in payload.get("storage_keys") or []:
            print(f"  cần xoá tệp: {key}")
    else:
        print(
            f"hàng đợi: {payload['tasks_pending']} chờ / {payload['tasks_running']} đang chạy"
            f" | job đang chạy: {payload['jobs_active']} | job chờ > 30 phút: {payload['jobs_waiting_30m']}"
        )
        print(
            f"lỗi 1 giờ: {payload['tasks_failed_1h']} | chỗ đặt hết hạn: {payload['leases_expired']}"
            f" | khoá bị cách ly: {payload['credentials_quarantined']} | ví âm: {payload['users_negative_credits']}"
        )
    # Mã thoát 3 = có dấu hiệu cần người xem (giám sát ngoài máy bắt được mà không phải đọc log).
    # Ngưỡng nằm ở `visynth.worker.alerts` để một chỗ duy nhất quyết định "bao nhiêu là đáng lo".
    if args.ops_cmd == "health":
        from visynth.worker import alerts as alert_rules

        found = alert_rules.evaluate(payload)
        if found:
            print(f"cảnh báo: {alert_rules.summarize(found)}")
            for item in found:
                if item["severity"] != "info":
                    print(f"  → {item['message']}: {item['action']}")
            return alert_rules.ALERT_EXIT_CODE
        return 0
    return 0


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

    p = sub.add_parser("run", help="chạy trọn P0–P8 (hoặc P0–P9 với full_translation) trên kịch bản giả")
    p.add_argument("path", nargs="?", help="bỏ trống hoặc dùng tệp mẫu (kịch bản giả chỉ có tài liệu demo)")
    p.add_argument("--demo", action="store_true", help="chạy tài liệu demo có sẵn")
    p.add_argument("--tamper", choices=TAMPER_MODES, default=None, help="biến thể phá hoại để thử P7")
    p.add_argument("--level", choices=list(LEVELS), default="deep_synthesis")
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
    p.add_argument("--style-core", help="dùng Lõi văn phong ĐÃ DUYỆT (core_id[@version]) thay lõi trung tính")
    p.add_argument("--store", help="tệp kho lõi (mặc định ~/.local/state/visynth/style_cores.json)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", help="ghi báo cáo Markdown ra tệp")
    p.add_argument(
        "--artifacts",
        help="ghi bộ tệp cho bộ chấm điểm vào thư mục này: run.json (đủ đơn vị/khối/coverage) + report.md",
    )
    p.set_defaults(func=cmd_run)

    style = sub.add_parser("style", help="Lõi văn phong (SPEC §19): lint, biên dịch, vòng đời duyệt")
    ss = style.add_subparsers(dest="style_cmd", required=True)

    q = ss.add_parser("lint", help="lint một tệp nội dung lõi (không cần kho)")
    q.add_argument("--core", required=True)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_style_lint)

    q = ss.add_parser("compile", help="biên dịch nội dung lõi thành khối {{style_core}} cho một giai đoạn")
    q.add_argument("--core", required=True)
    q.add_argument(
        "--stage",
        choices=["glossary", "map", "consolidate", "write", "translate", "repair", "assemble"],
        default="write",
    )
    q.add_argument("--max-chars", type=int)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_style_compile)

    for name, func, helptext in (
        ("init", cmd_style_init, "tạo lõi mới (draft) từ lõi trung tính — bắt đầu §19.9"),
        ("status", cmd_style_status, "liệt kê lõi và phiên bản trong kho"),
        ("show", cmd_style_show, "xem nội dung, quyết định mở và câu trả lời"),
        ("decide", cmd_style_decide, "trả lời một quyết định mở của P12"),
        ("approve", cmd_style_approve, "duyệt một phiên bản (bị chặn nếu chưa đủ điều kiện)"),
        ("bump", cmd_style_bump, "tạo bản nháp mới từ phiên bản trước (patch|minor|major)"),
        ("deprecate", cmd_style_deprecate, "ngừng dùng một phiên bản đã duyệt"),
    ):
        q = ss.add_parser(name, help=helptext)
        q.add_argument("--store", help="tệp kho lõi (mặc định ~/.local/state/visynth/style_cores.json)")
        q.add_argument("--json", action="store_true")
        if name == "init":
            q.add_argument("--id", dest="core_id", required=True)
            q.add_argument("--name-vi", required=True)
            q.add_argument("--domain", required=True)
            q.add_argument("--summary")
            q.add_argument("--parent", help="lõi cha (phải đã duyệt)")
            q.add_argument("--version", default="0.1.0")
            q.add_argument("--by")
        elif name in ("show", "decide", "approve", "bump", "deprecate"):
            q.add_argument("version_id", help="core_id hoặc core_id@version")
            q.add_argument("--by")
        if name == "decide":
            q.add_argument("--answer", required=True, help="dạng d1=trả lời")
        if name == "bump":
            q.add_argument("--kind", choices=["patch", "minor", "major"], default="patch")
        if name == "deprecate":
            q.add_argument("--reason")
        q.set_defaults(func=func)
    q = ss.add_parser("propose", help="P12 đề xuất lõi từ tài liệu mẫu + cặp tham chiếu (§19.9 bước 1-3)")
    q.add_argument("--id", dest="core_id", required=True)
    q.add_argument("--name-vi", required=True)
    q.add_argument("--domain", required=True)
    q.add_argument("--brief", required=True, help="tệp mô tả lĩnh vực/người đọc/mục đích (≤ 3.000 ký tự)")
    q.add_argument("--samples", required=True, help="thư mục tài liệu mẫu (.txt/.md), 3-10 tài liệu")
    q.add_argument("--pairs", help="tệp JSON cặp dịch tham chiếu [{'source','target',…}] — bằng chứng mạnh nhất")
    q.add_argument("--feedback", help="tệp JSON phản hồi/cờ của người dùng")
    q.add_argument("--refine", help="tinh chỉnh từ lõi hiện có (core_id[@version]) thay vì khởi tạo")
    q.add_argument("--parent", help="lõi cha trong kho (kế thừa, mặc định không)")
    q.add_argument("--pool-config", help="chạy P12 thật; bỏ trống = chạy khô bằng đề xuất giả")
    q.add_argument("--ledger")
    q.add_argument("--gate", choices=["dev", "A", "B", "C"], default="dev")
    q.add_argument("--demo", action="store_true", help="buộc dùng đề xuất giả")
    q.add_argument("--dry-run", action="store_true", help="không ghi vào kho")
    q.add_argument("--by")
    q.add_argument("--store")
    q.add_argument("--json", action="store_true")
    q.add_argument("--out")
    q.set_defaults(func=cmd_style_propose)

    q = ss.add_parser("edit", help="sửa trực tiếp nội dung một bản nháp (bản đã duyệt là bất biến)")
    q.add_argument("version_id")
    q.add_argument("--core", required=True, help="tệp JSON nội dung mới")
    q.add_argument("--by")
    q.add_argument("--store")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_style_edit)

    q = ss.add_parser("confirm", help="xác nhận đã xem mục AI đề xuất (reviewed = true)")
    q.add_argument("version_id")
    q.add_argument("--ids", help="danh sách id, ví dụ R01,E02 (bỏ trống cần --all)")
    q.add_argument("--all", action="store_true", help="xác nhận tất cả mục AI chưa xem")
    q.add_argument("--by")
    q.add_argument("--store")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_style_confirm)

    q = ss.add_parser("compare", help="test-drive §19.9: lõi trung tính vs lõi ứng viên trên cùng tài liệu")
    q.add_argument("version_id")
    q.add_argument("--path", help="tài liệu để chạy (bỏ trống = kịch bản giả)")
    q.add_argument("--pool-config", help="chạy LLM thật bằng pool_config này")
    q.add_argument("--ledger")
    q.add_argument("--gate", choices=["dev", "A", "B", "C"], default="dev")
    q.add_argument("--level", choices=list(LEVELS), default="deep_synthesis")
    q.add_argument("--seed", type=int, default=7)
    q.add_argument("--store")
    q.add_argument("--json", action="store_true")
    q.add_argument("--out")
    q.set_defaults(func=cmd_style_compare)

    w = sub.add_parser("worker", help="Worker production (M1): hàng đợi task PostgreSQL + pipeline P0–P8")
    w.add_argument(
        "--db-dsn", default=os.environ.get("VISYNTH_DB_DSN", ""), help="chuỗi kết nối PostgreSQL (hoặc VISYNTH_DB_DSN)"
    )
    w.add_argument("--worker-id", default=os.environ.get("VISYNTH_WORKER_ID", "worker-1"))
    w.add_argument("--limit", type=int, default=1, help="số task nhận mỗi lượt")
    w.add_argument("--per-job", type=int, default=1, help="số task chạy song song cho một job")
    w.add_argument("--once", action="store_true", help="chạy một lượt rồi thoát (dùng cho cron/test)")
    w.add_argument("--poll", type=float, default=2.0)
    w.add_argument("--idle-exit", type=float, default=0.0, help="tự thoát sau N giây hàng đợi trống (0 = chạy mãi)")
    w.add_argument("--fake", action="store_true", help="dùng client giả (dev/test)")
    w.add_argument("--pool-config", help="chạy LLM thật bằng pool_config này (tệp; tiện cho dev/CLI)")
    w.add_argument(
        "--pool-from-db",
        action="store_true",
        help="đọc pool + khoá mã hoá từ CSDL (đường chạy của VPS; cần POOL_MASTER_KEY/POOL_MASTER_KEY_FILE)",
    )
    w.add_argument("--ledger")
    w.add_argument("--gate", choices=["dev", "A", "B", "C"], default="dev")
    w.add_argument("--style-core", help="biên dịch lõi văn phong đã duyệt cho mọi job")
    w.add_argument("--store", help="tệp kho lõi văn phong")
    w.set_defaults(func=cmd_worker)

    d = sub.add_parser("db", help="CSDL (M1): baseline Alembic, áp schema, kiểm tra kết nối")
    ds = d.add_subparsers(dest="db_cmd", required=True)
    for name, helptext in (
        ("migrate", "chạy Alembic tới head (baseline = docs/db/schema.sql)"),
        ("init", "áp thẳng docs/db/schema.sql"),
        ("ping", "kiểm tra kết nối"),
    ):
        q = ds.add_parser(name, help=helptext)
        q.add_argument("--db-dsn", default=os.environ.get("VISYNTH_DB_DSN", ""))
        q.set_defaults(func=cmd_db)

    q = sub.add_parser(
        "parse-server",
        help="dịch vụ bóc tách trong sandbox (SPEC §6.1): container không mạng, hệ tệp chỉ đọc",
    )
    q.add_argument("--host", default="0.0.0.0")  # noqa: S104 - chỉ mạng `internal` của compose nhìn thấy
    q.add_argument("--port", type=int, default=8080)
    q.add_argument("--max-bytes", type=int, default=0, help="trần dung lượng (mặc định 50 MB theo §6.1)")
    q.add_argument("--timeout", type=int, default=120, help="timeout mỗi lần bóc tách, giây (§6.1)")
    q.set_defaults(func=cmd_parse_server)

    ops_p = sub.add_parser("ops", help="bảo trì định kỳ (SPEC §20): thu hồi task/chỗ đặt, xoá nội dung hết hạn")
    os_sub = ops_p.add_subparsers(dest="ops_cmd", required=True)
    q = os_sub.add_parser("reap", help="thu hồi task mồ côi và chỗ đặt hết hạn (chạy mỗi phút)")
    q.add_argument("--stale", default="3 minutes", help="task 'running' quá hạn heartbeat này thì thu hồi")
    q.add_argument("--dry-run", action="store_true", help="chỉ đếm, không ghi")
    q.add_argument("--json", action="store_true", help="in JSON một dòng (cho cron/giám sát)")
    q.add_argument("--db-dsn", default=os.environ.get("VISYNTH_DB_DSN", ""))
    q.set_defaults(func=cmd_ops)
    q = os_sub.add_parser("purge", help="xoá nội dung gốc quá hạn lưu, giữ bản ghi + trích dẫn (chạy mỗi giờ)")
    q.add_argument("--dry-run", action="store_true", help="chỉ liệt kê, không xoá")
    q.add_argument("--json", action="store_true", help="in JSON một dòng (cho cron/giám sát)")
    q.add_argument("--db-dsn", default=os.environ.get("VISYNTH_DB_DSN", ""))
    q.set_defaults(func=cmd_ops)
    q = os_sub.add_parser("health", help="in số liệu sức khoẻ cho giám sát ngoài máy (mã thoát 3 = cần xem)")
    q.add_argument("--json", action="store_true", help="in JSON một dòng (cho cron/giám sát)")
    q.add_argument("--db-dsn", default=os.environ.get("VISYNTH_DB_DSN", ""))
    q.set_defaults(func=cmd_ops)

    gl = sub.add_parser("glossary", help="Glossary chuẩn (SPEC §19.5): P13, hàng đợi duyệt, phát hành")
    gs = gl.add_subparsers(dest="glossary_cmd", required=True)

    q = gs.add_parser("propose", help="hài hoà ứng viên từ nhiều tài liệu mẫu bằng P13 rồi vào hàng đợi `suggested`")
    q.add_argument("--candidates", nargs="+", required=True, help="tệp JSON ứng viên P1 theo tài liệu")
    q.add_argument("--contexts", help="tệp JSON {'doc_ref|source_term': [đoạn trích,…]}")
    q.add_argument("--policy", help="tệp JSON terminology_policy (lấy từ lõi văn phong)")
    q.add_argument("--pairs", help="tệp JSON cặp dịch tham chiếu (ưu tiên khi chọn cách dịch)")
    q.add_argument("--existing", action="store_true", help="đưa các mục đã có trong kho vào để P13 không đề xuất trùng")
    q.add_argument("--max-entries", type=int, default=60)
    q.add_argument("--pool-config")
    q.add_argument("--ledger")
    q.add_argument("--gate", choices=["dev", "A", "B", "C"], default="dev")
    q.add_argument("--demo", action="store_true")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--store")
    q.add_argument("--json", action="store_true")
    q.add_argument("--out")
    q.set_defaults(func=cmd_glossary_propose)

    q = gs.add_parser("status", help="hàng đợi duyệt, mục cần người quyết định lên đầu")
    q.add_argument("--status", choices=["suggested", "confirmed", "rejected"])
    q.add_argument("--store")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_glossary_status)

    q = gs.add_parser("approve", help="duyệt một mục (kèm sửa cách dịch nếu cần)")
    q.add_argument("source_term")
    q.add_argument("--target", help="cách dịch chốt lại nếu khác đề xuất")
    q.add_argument("--by", required=True)
    q.add_argument("--store")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_glossary_approve)

    q = gs.add_parser("reject", help="từ chối một mục (mục được giữ lại để không bị đề xuất lại)")
    q.add_argument("source_term")
    q.add_argument("--reason")
    q.add_argument("--by", required=True)
    q.add_argument("--store")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_glossary_reject)

    q = gs.add_parser("publish", help="phát hành bản mới (chỉ mục confirmed; bất biến)")
    q.add_argument("--by", required=True)
    q.add_argument("--summary")
    q.add_argument("--store")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_glossary_publish)

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

    q = ps.add_parser("apply-probe", help="nhập số đo của probe vào pool_config (mặc định chỉ xem trước)")
    q.add_argument("--config", required=True)
    q.add_argument("--probe", required=True, help="tệp probe.json do `visynth pool probe --out` ghi")
    q.add_argument("--limits", help="JSON hạn mức đo tay: {deployments:{id:{rpm,tpm,rpd,tpd}}, groups:{id:{...}}}")
    q.add_argument("--today", help="ngày ghi vào label của nhóm (mặc định: hôm nay)")
    q.add_argument("--write", action="store_true", help="ghi thật (mặc định chỉ in thay đổi)")
    q.set_defaults(func=cmd_pool_apply_probe)

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
