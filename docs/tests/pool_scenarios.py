"""Kịch bản ngẫu nhiên có hạt giống cố định để (1) chứng minh MemoryState chạm MỌI nhánh từ chối/trạng thái của pool (tests/test_pool.py)
và (2) chạy SONG SONG MemoryState với hàm SQL pool_try_reserve/pool_settle trên PostgreSQL thật, so khớp từng bước (tests/pg_smoke.py).
Cấu hình được 'siết' lại để hạn mức bị chạm thường xuyên."""
from __future__ import annotations

import collections
import copy
import random
from dataclasses import dataclass, field

from reference.llm_pool import MemoryState, Outcome, load_pool_model

CALM = ["ok"] * 90 + ["rate_limited_minute"] * 3 + ["rate_limited_unknown"] * 2 + ["invalid_output", "bad_request", "context_exceeded", "safety_blocked", "truncated", "canceled"]
STORM = ["server_error"] * 6 + ["timeout"] * 2 + ["ok"] * 2
T0 = 1_790_000_000.0

REQUIRED_REASONS = {
    "deployment_rpm", "deployment_tpm", "deployment_rpd", "deployment_tpd", "deployment_concurrency", "deployment_cooldown",
    "group_rpm", "group_rpd", "group_tpm", "group_concurrency", "group_cooldown", "probing", "too_large",
}


def stress(cfg: dict, **patch) -> dict:
    """Siết giới hạn của cấu hình mẫu. patch: {'dep:<id>': {...limits}, 'grp:<id>': {...limits}} (None xoá giới hạn)."""
    c = copy.deepcopy(cfg)
    for k, v in patch.items():
        kind, _, ident = k.partition(":")
        target = next(x for x in (c["deployments"] if kind == "dep" else c["groups"]) if x["id"] == ident)
        target["limits"].update(v)
    return c


@dataclass
class Scenario:
    name: str
    seed: int
    steps: int
    deps: list
    tins: list
    touts: list
    dts: list
    p_reserve: float = 0.5
    day_p: float = 0.0
    storm: bool = False
    patch: dict = field(default_factory=dict)


SCENARIOS = [
    Scenario("rpm-burst", 1, 300, ["gemini-free-b/flash"], [500, 1000], [100], [0, 0, 0, 0.05], 0.5, 0.0,
             patch={"dep:gemini-free-b/flash": dict(rpm=10, rpd=None, tpm=None, concurrency=2)}),
    Scenario("rpd-rollover", 2, 500, ["gemini-free-b/flash", "gemini-paid/flash"], [1000], [100], [3, 6, 9, 20], 0.6, 0.01,
             patch={"dep:gemini-free-b/flash": dict(rpm=10, rpd=15, tpm=None, concurrency=2)}),
    Scenario("tpm-tpd", 3, 400, ["gemini-free-a/flash"], [60000, 90000, 20000], [800], [0.5, 2, 5, 30], 0.5, 0.02,
             patch={"dep:gemini-free-a/flash": dict(rpm=6, tpm=120000, rpd=None, tpd=400_000, concurrency=3)}),
    Scenario("group-rpm", 4, 400, ["provider-a-free/large"], [500, 2000, 9000], [100, 800], [0, 0.1, 1, 4, 10], 0.55, 0.0,
             patch={"grp:provider-a-free": dict(rpm=6, rpd=None), "dep:provider-a-free/large": dict(tpm=None, concurrency=4)}),
    Scenario("group-rpd-conc", 5, 500, ["provider-a-free/large"], [500, 2000, 9000], [100, 800], [0, 0.1, 1, 4, 10], 0.55, 0.01,
             patch={"grp:provider-a-free": dict(rpm=None, rpd=10, tpm=None, concurrency=2), "dep:provider-a-free/large": dict(tpm=None, concurrency=4)}),
    Scenario("group-tpm", 9, 300, ["provider-a-free/large"], [9000, 12000], [100, 800], [0, 0.1, 1], 0.55, 0.0,
             patch={"grp:provider-a-free": dict(rpm=None, rpd=None, tpm=40000, concurrency=None), "dep:provider-a-free/large": dict(tpm=None, concurrency=8)}),
    Scenario("mixed-concurrency", 6, 600, ["gemini-free-b/flash", "gemini-free-a/flash", "nvidia-trial/chat"], [500, 5000], [100], [0.2, 1, 5, 20, 40], 0.5, 0.0),
    Scenario("storms", 7, 700, ["gemini-paid/flash", "provider-a-free/large"], [500, 2000], [100, 500], [0.5, 1, 3, 10, 30], 0.5, 0.0, storm=True,
             patch={"dep:provider-a-free/large": dict(tpm=60000, concurrency=2)}),
    Scenario("oversize", 8, 150, ["gemini-free-a/flash", "provider-a-free/large"], [500, 130000, 300000], [100, 4000], [1, 5], 0.6, 0.0,
             patch={"dep:gemini-free-a/flash": dict(tpm=120000, rpd=None)}),
]


@dataclass
class Result:
    reasons: collections.Counter = field(default_factory=collections.Counter)
    circuits: set = field(default_factory=set)
    mismatches: list = field(default_factory=list)
    reserves: int = 0
    granted: int = 0
    settles: int = 0
    snapshots: int = 0


def _close(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        if a == float("inf") or b == float("inf"):
            return a == b
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    return a == b


def run(scn: Scenario, base_cfg: dict, make_pg=None) -> Result:
    """Chạy một kịch bản. make_pg(cfg) -> PgState (đã nạp cấu hình vào CSDL); None: chỉ chạy MemoryState."""
    cfg = stress(base_cfg, **scn.patch)
    model = load_pool_model(cfg)
    mem = MemoryState()
    pg = make_pg(cfg) if make_pg else None
    rng, now, res = random.Random(scn.seed), T0, Result()
    deps = [d for d in model.deployments if d.id in scn.deps]
    open_leases: list[tuple] = []
    for step in range(scn.steps):
        now += rng.choice(scn.dts)
        if rng.random() < scn.day_p:
            now += 86400
        in_storm = scn.storm and (step % 100) < 30
        r = rng.random()
        if r < scn.p_reserve or not open_leases:
            d = rng.choice(deps)
            tin, tout, prio = rng.choice(scn.tins), rng.choice(scn.touts), rng.choice(["high", "normal", "normal", "low"])
            a = mem.try_reserve(d, tin, tout, prio, now)
            b = pg.try_reserve(d, tin, tout, prio, now) if pg else None
            res.reserves += 1
            res.reasons[a.reason or "OK"] += 1
            if b is not None and not (a.ok == b.ok and a.reason == b.reason and a.credential_id == b.credential_id
                                      and a.basis_tokens == b.basis_tokens and _close(float(a.wait_s), float(b.wait_s))):
                res.mismatches.append((scn.name, step, "reserve", d.id, tin, tout, prio, a, b))
            if a.ok:
                res.granted += 1
                open_leases.append((a.lease_id, b.lease_id if b else None, tin, tout))
        elif r < 0.97:
            ma, pb, lin, lout = open_leases.pop(rng.randrange(len(open_leases)))
            kind = rng.choice(STORM if in_storm else CALM)
            if kind != "server_error" and rng.random() < 0.004:
                kind = "rate_limited_day"
            is429 = kind.startswith("rate_limited")
            o = Outcome(kind, tokens_in=int(lin * rng.choice([0.5, 1.0, 1.0])), tokens_out=int(lout * rng.choice([0.3, 0.8, 1.0])), latency_ms=rng.choice([300, 1200, 4000]),
                        retry_after_s=rng.choice([None, 7.0, 25.0]) if (is429 or kind == "server_error") else None,
                        scope=rng.choice(["deployment", "deployment", "group"]) if is429 else "deployment")
            mem.settle(ma, o, now)
            if pg:
                pg.settle(pb, o, now)
            res.settles += 1
        else:
            x = mem.reap(now)
            y = pg.reap(now) if pg else x
            if x != y:
                res.mismatches.append((scn.name, step, "reap", x, y))
        for dep in deps:
            res.circuits.add(mem._scope("deployment", dep.id).circuit)
        if step % 7 == 0:
            for dep in deps:
                sa = mem.snapshot(dep, now)
                res.snapshots += 1
                if pg:
                    sb = pg.snapshot(dep, now)
                    if not all(_close(float(sa[k]), float(sb[k])) if isinstance(sa[k], (int, float)) else sa[k] == sb[k] for k in sa):
                        res.mismatches.append((scn.name, step, "snapshot", dep.id, sa, sb))
    return res
