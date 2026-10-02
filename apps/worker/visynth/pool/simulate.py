"""Bản port `docs/reference/../tools/simulate_pool.py` cho mã sản phẩm (SPEC §17).

Giữ nguyên ngữ nghĩa tệp tham chiếu; `tests/test_spec_conformance.py` so khớp hai bản trên cùng kịch bản.

Mô phỏng rời rạc LLM Pool (SPEC §17.12): cùng Router + MemoryState như production, nhà cung cấp giả có hạn mức THẬT riêng.

Mục đích: thấy trước hành vi chính sách (chờ hay chuyển sang trả phí, 429, thời gian hoàn tất, chi phí) trước khi tốn tiền,
và cho phép sửa cấu hình rồi chạy lại. Đây là MÔ PHỎNG thuật toán với hạn mức GIẢ ĐỊNH trong `pool_config.example.json`;
KHÔNG phải số đo của nhà cung cấp thật — số thật lấy từ `visynth pool probe`.
"""

from __future__ import annotations

import copy
import heapq
import json
import os
import pathlib
import random
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from visynth.estimate import Price, estimate, estimate_calls
from visynth.llm.base import Outcome
from visynth.pool.model import Lease, Request, Wait, load_pool_model
from visynth.pool.router import Router

PRICE = Price("gemini-3.8-flash", datetime(2026, 9, 2).date(), 0.75, 3.75, 0.075)
#: Bản mẫu trong repo — phân giải theo vị trí tệp để lệnh chạy được từ bất kỳ thư mục nào.
DEFAULT_CONFIG = str(Path(__file__).resolve().parents[4] / "docs" / "examples" / "pool_config.example.json")


def base_config(path: str | os.PathLike[str] = DEFAULT_CONFIG) -> dict:
    """Nạp `pool_config` mẫu (hạn mức GIẢ ĐỊNH) để mô phỏng."""
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def with_free_projects(cfg: dict, n: int) -> dict:
    """Nhân bản dự án Gemini miễn phí lên n dự án (mỗi dự án một nhóm hạn mức riêng)."""
    c = copy.deepcopy(cfg)
    g0 = next(g for g in c["groups"] if g["id"] == "gemini-free-a")
    d0 = next(d for d in c["deployments"] if d["id"] == "gemini-free-a/flash")
    c["groups"] = [g for g in c["groups"] if not g["id"].startswith("gemini-free")]
    c["deployments"] = [d for d in c["deployments"] if not d["id"].startswith("gemini-free")]
    for i in range(n):
        g = copy.deepcopy(g0)
        g["id"] = f"gemini-free-{i + 1}"
        g["credentials"] = [{"id": f"{g['id']}-k1", "label": "khoá", "secret_ref": f"env:KEY_{i + 1}"}]
        d = copy.deepcopy(d0)
        d["id"], d["group"] = f"{g['id']}/flash", g["id"]
        c["groups"].append(g)
        c["deployments"].append(d)
    return c


def simulate(
    cfg: dict,
    n_docs: int = 10,
    words: int = 90_000,
    level: str = "deep_synthesis",
    allow_metered: bool = True,
    region_restricted: bool = False,
    true_scale: float = 1.0,
    seed: int = 1,
    workers: int = 12,
    horizon_h: float = 60.0,
) -> dict:
    rng = random.Random(seed)
    model = load_pool_model(cfg)
    router = Router(model, seed=seed)
    est = estimate(words, level, PRICE)
    n_calls = estimate_calls(words, level)
    tin_avg, tout_avg = est.tokens_in / n_calls, est.tokens_out / n_calls
    start = datetime(
        2026, 10, 2, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles")
    ).timestamp()  # 09:00 giờ Thái Bình Dương: còn 15 giờ tới lúc đặt lại hạn mức ngày
    tasks = deque(
        {
            "id": f"{d}-{i}",
            "doc": d,
            "tin": max(500, int(tin_avg * rng.uniform(0.6, 1.4))),
            "tout": max(100, int(tout_avg * rng.uniform(0.6, 1.4))),
            "exclude": frozenset(),
            "after": start,
        }
        for d in range(n_docs)
        for i in range(n_calls)
    )
    left_in_doc = Counter({d: n_calls for d in range(n_docs)})
    doc_done: dict[int, float] = {}
    truth: dict[str, dict] = {}  # bộ giới hạn THẬT của nhà cung cấp giả (có thể thấp hơn cấu hình: true_scale)
    stat = Counter()
    paid_cost = shadow_cost = 0.0
    running: list = []
    seq, now, free = 0, start, workers

    def true_call(dep, now_):
        t = truth.setdefault(dep.id, {"lvl": None, "at": now_, "day": None, "used": 0})
        rpm = dep.limits.rpm or 10**9
        cap = max(1.0, rpm * true_scale)
        t["lvl"] = cap if t["lvl"] is None else min(cap, t["lvl"] + cap / 60.0 * (now_ - t["at"]))
        t["at"] = now_
        day = datetime.fromtimestamp(now_, ZoneInfo(dep.group.reset_tz)).date()
        if t["day"] != day:
            t["day"], t["used"] = day, 0
        if dep.limits.rpd and t["used"] >= dep.limits.rpd * true_scale:
            return "rate_limited_day", None
        if t["lvl"] < 1.0:
            return "rate_limited_minute", 20.0
        t["lvl"] -= 1.0
        t["used"] += 1
        if rng.random() < 0.02:
            return "server_error", None
        return "ok", None

    horizon = start + horizon_h * 3600
    blocked_until = (
        0.0  # pool báo phải chờ tới giờ này: các tác vụ cùng profile sẽ gặp cùng kết quả, không thử lại vô ích
    )
    while (tasks or running) and now < horizon:
        while free > 0 and blocked_until <= now:
            t = next((x for x in tasks if x["after"] <= now), None)
            if t is None:
                break
            out = router.acquire_failover(
                Request(
                    "writer",
                    t["tin"],
                    t["tout"],
                    gate="A",
                    allow_metered=allow_metered,
                    region_restricted=region_restricted,
                    exclude=t["exclude"],
                ),
                now,
            )
            if isinstance(out, Lease):
                tasks.remove(t)
                kind, ra = true_call(out.deployment, now)
                dur = 2.0 + t["tout"] / 80.0 + rng.lognormvariate(0, 0.35)
                seq += 1
                heapq.heappush(running, (now + dur, seq, t, out, kind, ra))
                free -= 1
            elif isinstance(out, Wait):
                blocked_until = out.until + 0.5
                stat["waits"] += 1
            else:
                tasks.remove(t)
                stat["impossible"] += 1
        nxt = []
        if running:
            nxt.append(running[0][0])
        if blocked_until > now and any(x["after"] <= now for x in tasks):
            nxt.append(blocked_until)
        nxt += [x["after"] for x in tasks if x["after"] > now]
        if not nxt:
            break
        now = max(now, min(nxt))
        while running and running[0][0] <= now:
            fin, _, t, lease, kind, ra = heapq.heappop(running)
            free += 1
            blocked_until = 0.0  # có chỗ trống (đồng thời, khoá) -> thử lại
            o = Outcome(
                kind,
                tokens_in=t["tin"],
                tokens_out=t["tout"],
                latency_ms=int((fin - t["started"]) * 1000) if "started" in t else None,
                retry_after_s=ra,
            )
            router.settle(lease, o, fin)
            dep = lease.deployment
            if kind == "ok":
                stat[f"ok_{dep.group.tier}"] += 1
                c_in, c_out = t["tin"] * PRICE.input_per_mtok / 1e6, t["tout"] * PRICE.output_per_mtok / 1e6
                shadow_cost += c_in + c_out
                if dep.price_mode == "metered":
                    paid_cost += c_in + c_out
                left_in_doc[t["doc"]] -= 1
                if left_in_doc[t["doc"]] == 0:
                    doc_done[t["doc"]] = fin
            else:
                stat["429" if kind.startswith("rate_limited") else "5xx"] += 1
                t["after"] = fin
                if kind == "server_error":
                    t["exclude"] = t["exclude"] | {dep.id}
                tasks.append(t)
    return dict(
        docs=len(doc_done),
        n_docs=n_docs,
        hours=((max(doc_done.values()) - start) / 3600 if doc_done else None),
        calls=n_calls * n_docs,
        free=stat["ok_free"],
        paid=stat["ok_paid"],
        r429=stat["429"],
        r5xx=stat["5xx"],
        waits=stat["waits"],
        paid_usd=paid_cost,
        shadow_usd=shadow_cost,
    )


def scenarios(base: dict) -> list[tuple[str, callable]]:
    return [
        ("A. 2 dự án free + trả phí dự phòng", lambda: (base, {})),
        ("B. 2 dự án free, KHÔNG có trả phí", lambda: (base, {"allow_metered": False})),
        ("C. Chỉ trả phí (không dùng free)", lambda: (base, {"region_restricted": True})),
        ("D. 5 dự án free + trả phí dự phòng", lambda: (with_free_projects(base, 5), {})),
        ("E. Như A, nhưng hạn mức THẬT thấp hơn cấu hình 40%", lambda: (base, {"true_scale": 0.6})),
    ]


def run_all(n_docs: int = 10, base: dict | None = None) -> list[dict]:
    base = base if base is not None else base_config()
    rows = []
    for name, fn in scenarios(base):
        cfg, kw = fn()
        r = simulate(cfg, n_docs=n_docs, **kw)
        r["name"] = name
        rows.append(r)
    return rows


def table_md(rows: list[dict]) -> str:
    out = [
        "| Kịch bản | Tài liệu xong | Thời gian hoàn tất | Lời gọi free / trả phí | Lỗi 429 | Lỗi 5xx | Lần phải chờ | Chi phí thật |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        done = f"{r['docs']}/{r['n_docs']}"
        hrs = (
            "chưa xong trong 60 giờ"
            if r["hours"] is None or r["docs"] < r["n_docs"]
            else (f"{r['hours'] * 60:.0f} phút" if r["hours"] < 3 else f"{r['hours']:.1f} giờ")
        )
        out.append(
            f"| {r['name']} | {done} | {hrs} | {r['free']} / {r['paid']} | {r['r429']} | {r['r5xx']} | {r['waits']} | ${r['paid_usd']:.2f} |"
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """`visynth pool simulate [--config path] [--docs N] [--json]`."""
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="visynth pool simulate", description="Mô phỏng rời rạc LLM Pool (SPEC §17.12)")
    ap.add_argument(
        "--config", default=DEFAULT_CONFIG, help="tệp pool_config JSON (mặc định: bản mẫu trong docs/examples)"
    )
    ap.add_argument("--docs", type=int, default=10, help="số tài liệu mô phỏng")
    ap.add_argument("--json", action="store_true", help="in JSON thay vì bảng Markdown")
    args = ap.parse_args(argv)
    if args.docs < 1:
        print("LỖI: --docs phải >= 1", file=sys.stderr)
        return 2
    try:
        cfg = base_config(args.config)
    except FileNotFoundError:
        print(f"LỖI: không tìm thấy {args.config}", file=sys.stderr)
        return 1
    rows = run_all(n_docs=args.docs, base=cfg)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(table_md(rows))
    for r in rows:
        print(f"  {r['name']}: chi phí bóng ${r['shadow_usd']:.2f}")
    return 0
