"""Bản port `docs/reference/llm_pool.py` cho mã sản phẩm (SPEC §17) — chính sách định tuyến, chuyển tầng, chuyển dự phòng.

Giữ nguyên ngữ nghĩa của tệp tham chiếu; `tests/test_spec_conformance.py` chạy lại cùng kịch bản trên cả hai
bản và so khớp từng bước. Lớp này KHÔNG gọi mạng: thời gian luôn được truyền vào, khoá do tầng trên cấp.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace

from visynth.llm.base import Outcome
from visynth.pool.model import (
    INF,
    PRIVATE_BLOCKING_FLAGS,
    PUBLIC_BLOCKING_FLAGS,
    PUBLIC_GATES,
    RISK_FLAGS,
    STRUCT_RANK,
    Deployment,
    Impossible,
    Lease,
    PoolModel,
    Profile,
    Request,
    Tier,
    Wait,
)
from visynth.pool.state import MemoryState

# ------------------------------------------------------------------------------------------ định tuyến


class Router:
    def __init__(self, model: PoolModel, state=None, seed: int = 0):
        self.m = model
        self.state = state or MemoryState()
        self.rng = random.Random(seed)

    # --- điều kiện đủ để một deployment phục vụ yêu cầu (tất định, không phụ thuộc trạng thái hạn mức)
    def eligible(
        self, d: Deployment, req: Request, prof: Profile, tier: Tier | None = None, avoid: bool = True
    ) -> bool:
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
        if (
            req.gate in PUBLIC_GATES
            and (g.tos_flags & PUBLIC_BLOCKING_FLAGS)
            and not self.m.policy.allow_risk_at_public_gates
        ):
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
                    for i, (s, _d) in enumerate(pool):
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
                r = self.state.try_reserve(
                    d, tin, tout, req.priority, now, self.m.policy.priority_reserve, self.m.policy.lease_ttl_s
                )
                if r.ok:
                    return (
                        Lease(r.lease_id, d, r.credential_id, r.basis_tokens, tout, now + self.m.policy.lease_ttl_s),
                        None,
                        None,
                        True,
                    )
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
                return Lease(
                    out2.id,
                    out2.deployment,
                    out2.credential_id,
                    out2.basis_tokens,
                    out2.out_tokens,
                    out2.expires_at,
                    True,
                )
            if out2 is not None:
                return out2
            if bw2 < bw:
                bw, br = bw2, br2
            any_cand = any_cand or any2
        if bw < INF:
            return Wait(now + bw, br)
        return Impossible("no_eligible_deployment" if not any_cand else "all_deployments_unusable")

    def acquire_failover(
        self, req: Request, now: float, max_exclude_wait_s: float = 30.0, retry_backoff_s: float = 5.0
    ) -> Lease | Wait | Impossible:
        """Giao thức chuyển dự phòng sau một lỗi (SPEC §17.8): `exclude` chỉ để thử NGAY deployment khác. Nếu không còn ứng viên khác, hoặc
        các ứng viên khác phải chờ lâu hơn max_exclude_wait_s, bỏ danh sách loại trừ và thử lại chính deployment đã lỗi (lỗi thường là tạm thời),
        nhưng không sớm hơn retry_backoff_s. Tránh kẹt hàng giờ chỉ vì một lỗi 5xx đơn lẻ trên deployment duy nhất còn lại."""
        out = self.acquire(req, now)
        if req.exclude and (
            isinstance(out, Impossible) or (isinstance(out, Wait) and out.until - now > max_exclude_wait_s)
        ):
            out2 = self.acquire(replace(req, exclude=frozenset()), now)
            if isinstance(out2, Lease):
                return out2
            if isinstance(out2, Wait):
                return Wait(max(out2.until, now + retry_backoff_s), out2.reason, out2.tier)
            return out2
        return out

    def settle(self, lease: Lease, outcome: Outcome, now: float) -> None:
        self.state.settle(lease.id, outcome, now)
