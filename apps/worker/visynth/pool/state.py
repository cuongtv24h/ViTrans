"""Bản port `docs/reference/llm_pool.py` cho mã sản phẩm (SPEC §17) — trạng thái hạn mức (đặt chỗ nguyên tử).

Giữ nguyên ngữ nghĩa của tệp tham chiếu; `tests/test_spec_conformance.py` chạy lại cùng kịch bản trên cả hai
bản và so khớp từng bước. Lớp này KHÔNG gọi mạng: thời gian luôn được truyền vào, khoá do tầng trên cấp.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from visynth.llm.base import Outcome
from visynth.pool.model import (
    INF,
    Deployment,
    Group,
    Limits,
    ReserveResult,
    day_key,
    next_day_boundary,
)

# ------------------------------------------------------------------------------------------ trạng thái


@dataclass
class ScopeState:
    rpm_level: float | None = None
    tpm_level: float | None = None
    bucket_at: float | None = None
    day_key: str | None = None
    rpd_used: int = 0
    tpd_used: int = 0
    inflight: int = 0
    limit_scale: float = 1.0
    cooldown_until: float = 0.0
    circuit: str = "closed"  # closed | open | half_open
    consecutive_failures: int = 0
    consecutive_429: int = 0
    trips: int = 0
    ewma_success: float = 1.0
    ewma_valid: float = 1.0
    ewma_latency_ms: float | None = None
    last_used_at: float = 0.0


def _caps(limits: Limits, margin: float, day_margin: float, scale: float):
    """(cap_rpm, cap_tpm, lim_rpd, lim_tpd). RPM có sàn 1.0 để không bao giờ kẹt vĩnh viễn khi rpm nhỏ."""
    cap_rpm = None if limits.rpm is None else max(1.0, limits.rpm * margin * scale)
    cap_tpm = None if limits.tpm is None else limits.tpm * margin * scale
    lim_rpd = None if limits.rpd is None else max(1, math.floor(limits.rpd * day_margin))
    lim_tpd = None if limits.tpd is None else max(1, math.floor(limits.tpd * day_margin))
    return cap_rpm, cap_tpm, lim_rpd, lim_tpd


class MemoryState:
    """Hiện thực tham chiếu của trạng thái pool. Ngữ nghĩa PHẢI trùng với pool_try_reserve/pool_settle (SQL)."""

    def __init__(self):
        self.scopes: dict[tuple[str, str], ScopeState] = {}
        self.leases: dict[int, dict] = {}
        self._next_lease = 1
        self.incidents: list[tuple[str, str, str]] = []

    # --- nội bộ
    def _scope(self, kind: str, sid: str) -> ScopeState:
        return self.scopes.setdefault((kind, sid), ScopeState())

    def _touch(self, s: ScopeState, limits: Limits, g: Group, now: float) -> None:
        cap_rpm, cap_tpm, _, _ = _caps(limits, g.safety_margin, g.day_margin, s.limit_scale)
        if s.bucket_at is None:
            s.bucket_at = now
        dt = max(now - s.bucket_at, 0.0)
        if cap_rpm is not None:
            s.rpm_level = cap_rpm if s.rpm_level is None else min(cap_rpm, s.rpm_level + cap_rpm / 60.0 * dt)
        if cap_tpm is not None:
            s.tpm_level = cap_tpm if s.tpm_level is None else min(cap_tpm, s.tpm_level + cap_tpm / 60.0 * dt)
        s.bucket_at = now
        dk = day_key(now, g.reset_tz)
        if s.day_key != dk:
            s.day_key, s.rpd_used, s.tpd_used = dk, 0, 0

    @staticmethod
    def _basis(dep: Deployment, tin: int, tout: int) -> int:
        return tin if dep.tpm_basis == "input" else tin + tout

    # --- giao diện công khai
    def try_reserve(
        self, dep: Deployment, tin: int, tout: int, priority: str, now: float, reserve: float = 0.2, ttl: float = 300.0
    ) -> ReserveResult:
        g = dep.group
        sg, sd = self._scope("group", g.id), self._scope("deployment", dep.id)
        self._touch(sg, g.limits, g, now)
        self._touch(sd, dep.limits, g, now)
        basis = self._basis(dep, tin, tout)
        low = priority == "low"
        res = reserve if low else 0.0

        active = [c for c in g.credentials if c.status == "active"]
        if not active:
            return ReserveResult(False, wait_s=INF, reason="no_credential")

        waits: list[tuple[float, str]] = []
        if sd.circuit == "open" and sd.cooldown_until <= now:
            sd.circuit = "half_open"
        for s, label in ((sg, "group"), (sd, "deployment")):
            if s.cooldown_until > now:
                waits.append((s.cooldown_until - now, f"{label}_cooldown"))
        if sd.circuit == "half_open" and sd.inflight > 0:
            waits.append((1.0, "probing"))

        for s, lim, label in ((sg, g.limits, "group"), (sd, dep.limits, "deployment")):
            cap_rpm, cap_tpm, lim_rpd, lim_tpd = _caps(lim, g.safety_margin, g.day_margin, s.limit_scale)
            if cap_tpm is not None and basis > cap_tpm:
                return ReserveResult(False, wait_s=INF, reason="too_large")
            if lim_tpd is not None and basis > lim_tpd:
                return ReserveResult(False, wait_s=INF, reason="too_large")
            nxt = next_day_boundary(now, g.reset_tz)
            if cap_rpm is not None:
                need = 1.0 + res * cap_rpm
                if s.rpm_level < need:
                    waits.append(((need - s.rpm_level) / (cap_rpm / 60.0), f"{label}_rpm"))
            if cap_tpm is not None:
                need = basis + res * cap_tpm
                if s.tpm_level < need:
                    waits.append(((need - s.tpm_level) / (cap_tpm / 60.0), f"{label}_tpm"))
            if lim_rpd is not None and s.rpd_used + 1 > math.floor(lim_rpd * (1 - res)):
                waits.append((nxt - now, f"{label}_rpd"))
            if lim_tpd is not None and s.tpd_used + basis > math.floor(lim_tpd * (1 - res)):
                waits.append((nxt - now, f"{label}_tpd"))
            conc = lim.concurrency
            if conc is not None and s.inflight >= conc:
                waits.append((1.0, f"{label}_concurrency"))

        if waits:
            w, why = max(waits, key=lambda x: x[0])  # hoà: lấy cái xuất hiện trước (SQL làm giống hệt)
            return ReserveResult(False, wait_s=w, reason=why)

        for s, lim in ((sg, g.limits), (sd, dep.limits)):
            if lim.rpm is not None:
                s.rpm_level -= 1.0
            if lim.tpm is not None:
                s.tpm_level -= basis
            s.rpd_used += 1
            s.tpd_used += basis
            s.inflight += 1
            s.last_used_at = now
        cred = min(active, key=lambda c: (c.last_used_at if c.last_used_at is not None else -INF, c.id))
        cred.last_used_at = now
        lid = self._next_lease
        self._next_lease += 1
        self.leases[lid] = dict(dep=dep, cred=cred.id, basis=basis, tout=tout, expires=now + ttl)
        return ReserveResult(True, lease_id=lid, credential_id=cred.id, basis_tokens=basis)

    def settle(self, lease_id: int, o: Outcome, now: float) -> None:
        lease = self.leases.pop(lease_id, None)
        if lease is None:
            return
        dep: Deployment = lease["dep"]
        g = dep.group
        sg, sd = self._scope("group", g.id), self._scope("deployment", dep.id)
        self._touch(sg, g.limits, g, now)
        self._touch(sd, dep.limits, g, now)
        for s in (sg, sd):
            s.inflight = max(0, s.inflight - 1)

        actual = self._basis(dep, o.tokens_in, o.tokens_out) if o.kind == "ok" else lease["basis"]
        diff = lease["basis"] - actual
        for s, lim in ((sg, g.limits), (sd, dep.limits)):
            cap_rpm, cap_tpm, _, _ = _caps(lim, g.safety_margin, g.day_margin, s.limit_scale)
            if cap_tpm is not None and diff != 0:
                s.tpm_level = max(0.0, min(cap_tpm, s.tpm_level + diff))
            s.tpd_used = max(0, s.tpd_used - diff)

        target = sg if o.scope == "group" else sd
        kind = o.kind
        if kind == "ok":
            sd.consecutive_failures = 0
            sd.consecutive_429 = 0
            sd.trips = 0 if sd.circuit == "half_open" else sd.trips
            if sd.circuit == "half_open":
                sd.circuit = "closed"
            sd.ewma_success = 0.9 * sd.ewma_success + 0.1
            if o.latency_ms is not None:
                sd.ewma_latency_ms = (
                    o.latency_ms if sd.ewma_latency_ms is None else 0.8 * sd.ewma_latency_ms + 0.2 * o.latency_ms
                )
            sd.limit_scale = min(1.0, sd.limit_scale + 0.05)
            sg.limit_scale = min(1.0, sg.limit_scale + 0.05)
        elif kind in ("rate_limited_minute", "rate_limited_day", "rate_limited_unknown"):
            target.limit_scale = max(0.3, target.limit_scale * 0.85)
            if kind == "rate_limited_minute":
                target.consecutive_429 = 0
                target.cooldown_until = max(
                    target.cooldown_until, now + (o.retry_after_s if o.retry_after_s is not None else 30.0)
                )
            elif kind == "rate_limited_day":
                target.cooldown_until = max(target.cooldown_until, next_day_boundary(now, g.reset_tz) + 30.0)
            else:
                target.consecutive_429 += 1
                back = min(600.0, 30.0 * 2 ** (target.consecutive_429 - 1))
                target.cooldown_until = max(target.cooldown_until, now + max(back, o.retry_after_s or 0.0))
            if target.rpm_level is not None:
                target.rpm_level = 0.0
            if target.tpm_level is not None:
                target.tpm_level = 0.0
            self.incidents.append((dep.id, kind, "429"))
        elif kind in ("server_error", "timeout", "network_error"):
            sd.consecutive_failures += 1
            sd.ewma_success = 0.9 * sd.ewma_success
            if sd.circuit == "half_open" or sd.consecutive_failures >= 3:
                sd.trips += 1
                sd.circuit = "open"
                sd.cooldown_until = max(sd.cooldown_until, now + min(600.0, 15.0 * 2 ** (sd.trips - 1)))
                self.incidents.append((dep.id, "circuit_open", kind))
        elif kind == "auth_error":
            for c in g.credentials:
                if c.id == lease["cred"]:
                    c.status = "quarantined"
            self.incidents.append((dep.id, "auth_error", lease["cred"]))
        elif kind == "invalid_output":
            sd.ewma_valid = 0.95 * sd.ewma_valid
        # bad_request | context_exceeded | safety_blocked | truncated | canceled: không phạt sức khoẻ

    def reap(self, now: float) -> int:
        dead = [i for i, lease in self.leases.items() if lease["expires"] <= now]
        for i in dead:
            dep = self.leases.pop(i)["dep"]
            for s in (self._scope("group", dep.group.id), self._scope("deployment", dep.id)):
                s.inflight = max(0, s.inflight - 1)
        return len(dead)

    def snapshot(self, dep: Deployment, now: float) -> dict:
        g = dep.group
        sg, sd = self._scope("group", g.id), self._scope("deployment", dep.id)
        self._touch(sg, g.limits, g, now)
        self._touch(sd, dep.limits, g, now)
        ratios = [1.0]
        for s, lim in ((sg, g.limits), (sd, dep.limits)):
            cap_rpm, cap_tpm, lim_rpd, lim_tpd = _caps(lim, g.safety_margin, g.day_margin, s.limit_scale)
            if cap_rpm is not None:
                ratios.append(s.rpm_level / cap_rpm)
            if cap_tpm is not None:
                ratios.append(s.tpm_level / cap_tpm)
            if lim_rpd is not None:
                ratios.append(1 - s.rpd_used / lim_rpd)
            if lim_tpd is not None:
                ratios.append(1 - s.tpd_used / lim_tpd)
            if lim.concurrency is not None:
                ratios.append(1 - s.inflight / lim.concurrency)
        cooldown = max(sg.cooldown_until, sd.cooldown_until)
        return dict(
            headroom=max(0.0, min(ratios)),
            ewma_success=sd.ewma_success,
            inflight=sd.inflight,
            cooldown_until=cooldown,
            circuit=sd.circuit,
            last_used_at=sd.last_used_at,
        )
