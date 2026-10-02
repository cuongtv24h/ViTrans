"""Lõi định tuyến của LLM Pool (tham chiếu, KHÔNG gọi mạng). Đặc tả: SPEC §17.

Hai lớp tách bạch:
  * PoolState  - phần ĐÚNG ĐẮN cần nguyên tử: token-bucket RPM/TPM, bộ đếm ngày RPD/TPD, đồng thời, cooldown,
                 circuit breaker, lease. Hai hiện thực có CÙNG ngữ nghĩa và được so khớp bằng test:
                 MemoryState (file này) và hàm SQL pool_try_reserve / pool_settle trong db/schema.sql.
  * Router     - phần CHÍNH SÁCH: lọc ứng viên (riêng tư, ToS, cổng triển khai, năng lực), chọn theo tier,
                 xử lý chờ / chuyển sang tier sau, đa dạng hoá người viết và người kiểm.

Thời gian luôn được TRUYỀN VÀO (now: giây epoch) để test tất định; không module nào đọc đồng hồ hệ thống.
Mọi con số giới hạn (rpm, tpm, rpd...) là CẤU HÌNH do chủ hệ thống nhập từ bảng điều khiển của nhà cung cấp,
không phải hằng số được ghi cứng trong code: nhà cung cấp có thể đổi bất cứ lúc nào (SPEC §17.3).
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

INF = float("inf")
STRUCT_RANK = {"none": 0, "json_object": 1, "json_schema": 2}
TIERS = ("free", "trial", "paid", "self_hosted")
DATA_POLICIES = ("no_training", "may_train", "unknown")
PRIVACY = ("standard", "private")
PRIORITIES = ("high", "normal", "low")
GATES = ("dev", "A", "B", "C")
OUTCOMES = (
    "ok", "rate_limited_minute", "rate_limited_day", "rate_limited_unknown", "server_error", "timeout",
    "network_error", "auth_error", "bad_request", "context_exceeded", "safety_blocked", "invalid_output",
    "truncated", "canceled",
)
# cờ ToS khiến một nhóm KHÔNG đủ điều kiện cho job riêng tư (xem SPEC §17.4)
PRIVATE_BLOCKING_FLAGS = frozenset({"trial_only", "no_personal_data"})
# cổng công khai: nhóm gắn các cờ này luôn bị loại, KHÔNG phụ thuộc allowed_gates (chốt chặn thứ hai nếu cấu hình sai)
PUBLIC_GATES = frozenset({"B", "C"})
PUBLIC_BLOCKING_FLAGS = frozenset({"multi_account_risk", "trial_only"})
# cờ rủi ro điều khoản: nhóm gắn cờ nào thì PHẢI có xác nhận (risk_ack) của chủ hệ thống cho đúng cờ đó mới được dùng (SPEC §17.14)
RISK_FLAGS = frozenset({"multi_account_risk", "trial_only"})

# ------------------------------------------------------------------------------------------ cấu hình


@dataclass(frozen=True)
class Limits:
    rpm: int | None = None
    tpm: int | None = None
    rpd: int | None = None
    tpd: int | None = None
    concurrency: int | None = None


@dataclass
class Credential:
    id: str
    status: str = "active"  # active | quarantined | disabled
    last_used_at: float | None = None


@dataclass
class Group:
    """Đơn vị mà nhà cung cấp ĐẾM hạn mức (Gemini: dự án Google Cloud; NVIDIA/OpenRouter: tài khoản)."""

    id: str
    provider: str
    tier: str = "free"
    data_policy: str = "unknown"
    reset_tz: str = "UTC"
    tos_flags: frozenset = frozenset()
    allowed_gates: frozenset = frozenset(GATES)
    safety_margin: float = 0.85
    day_margin: float = 0.95
    limits: Limits = Limits()  # giới hạn CẢ TÀI KHOẢN (chung cho mọi model trong nhóm), tuỳ chọn
    credentials: list[Credential] = field(default_factory=list)
    enabled: bool = True
    risk_ack: frozenset = frozenset()  # các cờ rủi ro mà chủ hệ thống đã xác nhận chấp nhận cho nhóm này


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    ctx_in: int = 128_000
    max_out: int = 8_192
    structured: str = "none"
    vision: bool = False
    pdf: bool = False
    tokenizer_factor: float = 1.0  # số token của model này / số token tham chiếu (Gemini)
    quality: tuple = ()  # ((khoá, điểm), ...) bất biến để hash được


@dataclass
class Deployment:
    id: str
    group: Group
    model: Model
    limits: Limits = Limits(concurrency=4)
    tpm_basis: str = "total"  # 'input': nhà cung cấp chỉ đếm token vào (Gemini); 'total': vào + ra
    price_mode: str = "free"  # free | metered
    weight: float = 1.0
    tags: frozenset = frozenset()
    enabled: bool = True

    @property
    def concurrency(self) -> int:
        return self.limits.concurrency or 4


@dataclass(frozen=True)
class Needs:
    structured: str = "none"  # mức NATIVE tối thiểu; 'none' = chấp nhận bậc thang prompt-only (SPEC §17.9)
    min_ctx_in: int = 0
    vision: bool = False
    pdf: bool = False
    min_quality: tuple = ()


@dataclass(frozen=True)
class Tier:
    name: str
    select_tags: frozenset = frozenset()
    select_group_tiers: frozenset = frozenset()
    strategy: str = "headroom"  # headroom | weighted | ordered
    max_wait_s: float | None = 90.0  # chờ tối đa ở tier này trước khi thử tier sau; None = chờ vô hạn


@dataclass(frozen=True)
class Profile:
    name: str
    needs: Needs
    tiers: tuple


@dataclass(frozen=True)
class Policy:
    priority_reserve: float = 0.2  # phần hạn mức dành riêng cho tác vụ ưu tiên cao/thường (job 'low' không được dùng)
    lease_ttl_s: float = 300.0
    diversity_max_wait_s: float = 60.0  # chờ tối đa để giữ đa dạng người viết/người kiểm trước khi nới ràng buộc
    allow_risk_at_public_gates: bool = False  # chủ hệ thống chấp nhận dùng nhóm gắn cờ rủi ro ngay cả ở cổng B/C (mặc định: không)


@dataclass
class PoolModel:
    policy: Policy
    deployments: list[Deployment]
    profiles: dict[str, Profile]
    groups: dict[str, Group]


# ------------------------------------------------------------------------------------------ yêu cầu / kết quả


@dataclass(frozen=True)
class Request:
    profile: str
    est_in: int  # token vào theo thước đo THAM CHIẾU (nhân tokenizer_factor của từng model)
    est_out: int
    privacy: str = "standard"
    priority: str = "normal"
    gate: str = "dev"
    region_restricted: bool = False  # người dùng ở EEA/Thụy Sĩ/Anh -> nhóm gắn cờ no_eea_uk_ch bị loại
    allow_metered: bool = True  # false khi đã chạm trần chi tiêu ngày
    needs_vision: bool = False
    needs_pdf: bool = False
    exclude: frozenset = frozenset()  # deployment id đã thất bại cho tác vụ này
    avoid_groups: frozenset = frozenset()  # đa dạng hoá: tránh nhóm đã dùng cho người viết khi chọn người kiểm
    job_id: str | None = None
    task_id: int | None = None


@dataclass(frozen=True)
class Lease:
    id: int
    deployment: Deployment
    credential_id: str
    basis_tokens: int
    out_tokens: int
    expires_at: float
    diversity_degraded: bool = False


@dataclass(frozen=True)
class Wait:
    until: float
    reason: str
    tier: str | None = None


@dataclass(frozen=True)
class Impossible:
    reason: str


@dataclass(frozen=True)
class Outcome:
    kind: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int | None = None
    retry_after_s: float | None = None
    scope: str = "deployment"  # 429 áp cho 'deployment' hay cả 'group' (tài khoản)


@dataclass(frozen=True)
class ReserveResult:
    ok: bool
    lease_id: int | None = None
    credential_id: str | None = None
    wait_s: float = 0.0
    reason: str = ""
    basis_tokens: int = 0


# ------------------------------------------------------------------------------------------ thời gian


def day_key(now: float, tz: str) -> str:
    return datetime.fromtimestamp(now, ZoneInfo(tz)).date().isoformat()


def next_day_boundary(now: float, tz: str) -> float:
    """Epoch của 00:00 ngày kế tiếp theo múi giờ tz (Gemini đặt lại RPD lúc nửa đêm giờ Thái Bình Dương)."""
    z = ZoneInfo(tz)
    d = datetime.fromtimestamp(now, z).date() + timedelta(days=1)
    # dựng 00:00 theo giờ địa phương, rồi đổi sang epoch (xử lý DST bằng zoneinfo)
    return datetime(d.year, d.month, d.day, tzinfo=z).timestamp()


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
    def try_reserve(self, dep: Deployment, tin: int, tout: int, priority: str, now: float,
                    reserve: float = 0.2, ttl: float = 300.0) -> ReserveResult:
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
                sd.ewma_latency_ms = o.latency_ms if sd.ewma_latency_ms is None else 0.8 * sd.ewma_latency_ms + 0.2 * o.latency_ms
            sd.limit_scale = min(1.0, sd.limit_scale + 0.05)
            sg.limit_scale = min(1.0, sg.limit_scale + 0.05)
        elif kind in ("rate_limited_minute", "rate_limited_day", "rate_limited_unknown"):
            target.limit_scale = max(0.3, target.limit_scale * 0.85)
            if kind == "rate_limited_minute":
                target.consecutive_429 = 0
                target.cooldown_until = max(target.cooldown_until, now + (o.retry_after_s if o.retry_after_s is not None else 30.0))
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
        dead = [i for i, l in self.leases.items() if l["expires"] <= now]
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
        return dict(headroom=max(0.0, min(ratios)), ewma_success=sd.ewma_success, inflight=sd.inflight,
                    cooldown_until=cooldown, circuit=sd.circuit, last_used_at=sd.last_used_at)


# ------------------------------------------------------------------------------------------ định tuyến


class Router:
    def __init__(self, model: PoolModel, state=None, seed: int = 0):
        self.m = model
        self.state = state or MemoryState()
        self.rng = random.Random(seed)

    # --- điều kiện đủ để một deployment phục vụ yêu cầu (tất định, không phụ thuộc trạng thái hạn mức)
    def eligible(self, d: Deployment, req: Request, prof: Profile, tier: Tier | None = None, avoid: bool = True) -> bool:
        g, m, n = d.group, d.model, prof.needs
        if not (d.enabled and g.enabled) or d.id in req.exclude:
            return False
        if not any(c.status == "active" for c in g.credentials):
            return False
        if STRUCT_RANK[m.structured] < STRUCT_RANK[n.structured]:
            return False
        if (g.tos_flags & RISK_FLAGS) - g.risk_ack:
            return False  # có cờ rủi ro mà chưa được chủ hệ thống xác nhận chấp nhận
        if (n.vision or req.needs_vision) and not m.vision:
            return False
        if (n.pdf or req.needs_pdf) and not m.pdf:
            return False
        q = dict(m.quality)
        if any(q.get(k, 0.0) < v for k, v in n.min_quality):
            return False
        if req.gate not in g.allowed_gates:
            return False
        if req.gate in PUBLIC_GATES and (g.tos_flags & PUBLIC_BLOCKING_FLAGS) and not self.m.policy.allow_risk_at_public_gates:
            return False
        if req.privacy == "private" and (g.data_policy != "no_training" or (g.tos_flags & PRIVATE_BLOCKING_FLAGS)):
            return False
        if req.region_restricted and "no_eea_uk_ch" in g.tos_flags:
            return False
        if d.price_mode == "metered" and not req.allow_metered:
            return False
        tin, tout = self._tokens(d, req)
        if tin + tout + 256 > m.ctx_in or m.ctx_in < n.min_ctx_in or tout > m.max_out:
            return False
        if avoid and g.id in req.avoid_groups:
            return False
        if tier is not None:
            if tier.select_tags and not tier.select_tags <= d.tags:
                return False
            if tier.select_group_tiers and g.tier not in tier.select_group_tiers:
                return False
        return True

    @staticmethod
    def _tokens(d: Deployment, req: Request) -> tuple[int, int]:
        f = d.model.tokenizer_factor
        return math.ceil(req.est_in * f), math.ceil(req.est_out * f)

    def _score(self, d: Deployment, now: float) -> float:
        s = self.state.snapshot(d, now)
        return s["headroom"] * (0.5 + 0.5 * s["ewma_success"]) * d.weight

    def _order(self, tier: Tier, cands: list[Deployment], now: float) -> list[Deployment]:
        if tier.strategy == "ordered":
            return sorted(cands, key=lambda d: (-d.weight, d.id))
        scored = sorted(((self._score(d, now), d) for d in cands), key=lambda x: (-x[0], x[1].id))
        if tier.strategy == "weighted" or len(scored) <= 2:
            pool, out = list(scored), []
            if tier.strategy == "weighted":
                while pool:
                    total = sum(max(s, 1e-9) for s, _ in pool)
                    r, acc = self.rng.random() * total, 0.0
                    for i, (s, d) in enumerate(pool):
                        acc += max(s, 1e-9)
                        if r <= acc:
                            out.append(pool.pop(i)[1])
                            break
                return out
            return [d for _, d in scored]
        # headroom: 'power of two choices' - lấy 2 ứng viên tốt nhất, xáo trộn theo điểm để nhiều worker không dồn về một chỗ
        top, rest = scored[:2], scored[2:]
        total = sum(max(s, 1e-9) for s, _ in top)
        first = 0 if self.rng.random() * total <= max(top[0][0], 1e-9) else 1
        head = [top[first][1], top[1 - first][1]]
        return head + [d for _, d in rest]

    def _pass(self, req: Request, prof: Profile, now: float, avoid: bool):
        best_wait, best_reason, any_cand = INF, "", False
        for tier in prof.tiers:
            cands = [d for d in self.m.deployments if self.eligible(d, req, prof, tier, avoid)]
            if not cands:
                continue
            any_cand = True
            tier_wait, tier_reason = INF, ""
            for d in self._order(tier, cands, now):
                tin, tout = self._tokens(d, req)
                r = self.state.try_reserve(d, tin, tout, req.priority, now, self.m.policy.priority_reserve, self.m.policy.lease_ttl_s)
                if r.ok:
                    return Lease(r.lease_id, d, r.credential_id, r.basis_tokens, tout, now + self.m.policy.lease_ttl_s), None, None, True
                if r.wait_s < tier_wait:
                    tier_wait, tier_reason = r.wait_s, r.reason
            if tier_wait < INF:
                limit = INF if tier.max_wait_s is None else tier.max_wait_s
                if tier_wait <= limit:
                    return Wait(now + tier_wait, tier_reason, tier.name), None, None, True
                if tier_wait < best_wait:
                    best_wait, best_reason = tier_wait, f"{tier.name}:{tier_reason}"
        return None, best_wait, best_reason, any_cand

    def acquire(self, req: Request, now: float) -> Lease | Wait | Impossible:
        prof = self.m.profiles[req.profile]
        out, bw, br, any_cand = self._pass(req, prof, now, avoid=True)
        if out is not None:
            return out
        if req.avoid_groups and (not any_cand or bw > self.m.policy.diversity_max_wait_s):
            out2, bw2, br2, any2 = self._pass(req, prof, now, avoid=False)
            if isinstance(out2, Lease):
                return Lease(out2.id, out2.deployment, out2.credential_id, out2.basis_tokens, out2.out_tokens, out2.expires_at, True)
            if out2 is not None:
                return out2
            if bw2 < bw:
                bw, br = bw2, br2
            any_cand = any_cand or any2
        if bw < INF:
            return Wait(now + bw, br)
        return Impossible("no_eligible_deployment" if not any_cand else "all_deployments_unusable")

    def acquire_failover(self, req: Request, now: float, max_exclude_wait_s: float = 30.0, retry_backoff_s: float = 5.0) -> Lease | Wait | Impossible:
        """Giao thức chuyển dự phòng sau một lỗi (SPEC §17.8): `exclude` chỉ để thử NGAY deployment khác. Nếu không còn ứng viên khác, hoặc
        các ứng viên khác phải chờ lâu hơn max_exclude_wait_s, bỏ danh sách loại trừ và thử lại chính deployment đã lỗi (lỗi thường là tạm thời),
        nhưng không sớm hơn retry_backoff_s. Tránh kẹt hàng giờ chỉ vì một lỗi 5xx đơn lẻ trên deployment duy nhất còn lại."""
        out = self.acquire(req, now)
        if req.exclude and (isinstance(out, Impossible) or (isinstance(out, Wait) and out.until - now > max_exclude_wait_s)):
            out2 = self.acquire(replace(req, exclude=frozenset()), now)
            if isinstance(out2, Lease):
                return out2
            if isinstance(out2, Wait):
                return Wait(max(out2.until, now + retry_backoff_s), out2.reason, out2.tier)
            return out2
        return out

    def settle(self, lease: Lease, outcome: Outcome, now: float) -> None:
        self.state.settle(lease.id, outcome, now)


# ------------------------------------------------------------------------------------------ phân loại lỗi HTTP


def _retry_delay(body) -> float | None:
    """Gemini: details[].retryDelay = '34s' hoặc '34.5s'."""
    for d in (body or {}).get("error", {}).get("details", []) or []:
        rd = d.get("retryDelay")
        if isinstance(rd, str) and rd.endswith("s"):
            try:
                return float(rd[:-1])
            except ValueError:
                return None
    return None


def _looks_like_bad_key(err: dict, msg: str) -> bool:
    """Khoá Google không hợp lệ: `API_KEY_INVALID` trong `details` hoặc "API key not valid" trong thông điệp."""
    blob = f"{msg} {err.get('details', '')}".lower()
    return "api_key_invalid" in blob or "api key not valid" in blob or "api key expired" in blob


def classify_http(kind: str, status: int, headers: dict | None = None, body=None, finish_reason: str | None = None) -> Outcome:
    """Ánh xạ phản hồi của adapter -> Outcome (SPEC §17.7). kind: 'openai_compat' | 'gemini_native'.

    body: dict đã parse JSON (hoặc None). headers: khoá viết thường.
    Các chuỗi nhận diện hạn mức ngày/phút là HEURISTIC, phải kiểm chứng bằng thực nghiệm ở Giai đoạn 0 với từng nhà cung cấp.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    body = body if isinstance(body, dict) else {}
    err = body.get("error", {}) if isinstance(body.get("error"), dict) else {}
    msg = f"{err.get('message', '')} {err.get('code', '')} {err.get('status', '')}"
    if status == 200:
        if finish_reason in ("length", "MAX_TOKENS"):
            return Outcome("truncated")
        if finish_reason in ("content_filter", "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            return Outcome("safety_blocked")
        return Outcome("ok")
    ra = None
    if "retry-after" in headers:
        try:
            ra = float(headers["retry-after"])
        except ValueError:
            ra = None
    if status == 429:
        if kind == "gemini_native":
            ra = _retry_delay(body) or ra
            quota_ids = " ".join(v.get("quotaId", "") for d in err.get("details", []) or [] for v in d.get("violations", []) or [])
            if "PerDay" in quota_ids:
                return Outcome("rate_limited_day", retry_after_s=ra)
            if "PerMinute" in quota_ids:
                return Outcome("rate_limited_minute", retry_after_s=ra)
        low = msg.lower()
        if "insufficient_quota" in low or re.search(r"per day|\btpd\b|\brpd\b|daily|credits? (are )?exhausted", low):
            return Outcome("rate_limited_day", retry_after_s=ra)
        if re.search(r"per minute|\btpm\b|\brpm\b", low):
            return Outcome("rate_limited_minute", retry_after_s=ra)
        return Outcome("rate_limited_unknown", retry_after_s=ra)
    if status in (401, 403):
        return Outcome("auth_error")
    if status == 400 and kind == "gemini_native" and _looks_like_bad_key(err, msg):
        # Google trả 400 INVALID_ARGUMENT ("API key not valid"/API_KEY_INVALID) cho khoá sai — không phải 401.
        return Outcome("auth_error")
    if status == 413 or (status == 400 and re.search(r"context|too many tokens|token count|maximum.*length|too long", msg, re.I)):
        return Outcome("context_exceeded")
    if status in (400, 404, 422):
        return Outcome("bad_request")
    if status == 408 or status == 504:
        return Outcome("timeout")
    if status >= 500:
        return Outcome("server_error", retry_after_s=ra)
    return Outcome("bad_request")


# ------------------------------------------------------------------------------------------ nạp cấu hình


def _limits(d: dict | None, default_conc: int | None = None) -> Limits:
    d = d or {}
    return Limits(d.get("rpm"), d.get("tpm"), d.get("rpd"), d.get("tpd"), d.get("concurrency", default_conc))


def load_pool_model(cfg: dict) -> PoolModel:
    """Dựng PoolModel từ JSON hợp lệ theo schemas/pool_config.schema.json."""
    pol = cfg.get("policy", {})
    policy = Policy(pol.get("priority_reserve", 0.2), pol.get("lease_ttl_s", 300.0), pol.get("diversity_max_wait_s", 60.0),
                    pol.get("allow_risk_at_public_gates", False))
    models = {
        m["id"]: Model(m["id"], m["provider"], m["ctx_in"], m["max_out"], m.get("structured", "none"),
                       m.get("vision", False), m.get("pdf", False), m.get("tokenizer_factor", 1.0), tuple(sorted((m.get("quality") or {}).items())))
        for m in cfg["models"]
    }
    groups: dict[str, Group] = {}
    for g in cfg["groups"]:
        groups[g["id"]] = Group(
            g["id"], g["provider"], g["tier"], g.get("data_policy", "unknown"), g.get("reset_tz", "UTC"), frozenset(g.get("tos_flags", [])),
            frozenset(g.get("allowed_gates", GATES)), g.get("safety_margin", 0.85), g.get("day_margin", 0.95), _limits(g.get("limits")),
            [Credential(c["id"], c.get("status", "active")) for c in g.get("credentials", [])], g.get("enabled", True),
            frozenset(g.get("risk_ack", [])),
        )
    deps = [
        Deployment(d["id"], groups[d["group"]], models[d["model"]], _limits(d.get("limits"), 4), d.get("tpm_basis", "total"),
                   d.get("price_mode", "free"), d.get("weight", 1.0), frozenset(d.get("tags", [])), d.get("enabled", True))
        for d in cfg["deployments"]
    ]
    profiles: dict[str, Profile] = {}
    for p in cfg["profiles"]:
        name = p["name"]
        n = p.get("needs", {})
        needs = Needs(n.get("structured", "none"), n.get("min_ctx_in", 0), n.get("vision", False),
                      n.get("pdf", False), tuple(sorted((n.get("min_quality") or {}).items())))
        tiers = tuple(
            Tier(t["name"], frozenset(t.get("select", {}).get("tags", [])), frozenset(t.get("select", {}).get("group_tiers", [])),
                 t.get("strategy", "headroom"), t.get("max_wait_s"))
            for t in p["tiers"]
        )
        profiles[name] = Profile(name, needs, tiers)
    return PoolModel(policy, deps, profiles, groups)


# ------------------------------------------------------------------------------------------ dung lượng dự kiến


def deployment_daily_capacity(d: Deployment) -> dict:
    """Số lời gọi và token VÀO tối đa mỗi ngày mà deployment có thể phục vụ theo cấu hình (trần thấp nhất của các giới hạn)."""
    g = d.group
    calls, toks = [], []
    for lim in (d.limits, g.limits):
        if lim.rpd:
            calls.append(math.floor(lim.rpd * g.day_margin))
        if lim.rpm:
            calls.append(math.floor(max(1.0, lim.rpm * g.safety_margin) * 1440))
        if lim.tpd:
            toks.append(math.floor(lim.tpd * g.day_margin))
        if lim.tpm:
            toks.append(math.floor(lim.tpm * g.safety_margin * 1440))
    return dict(calls=min(calls) if calls else None, tokens=min(toks) if toks else None)
