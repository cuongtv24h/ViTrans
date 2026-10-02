"""Bản port `docs/reference/llm_pool.py` cho mã sản phẩm (SPEC §17) — cấu hình, yêu cầu, nạp pool_config.

Giữ nguyên ngữ nghĩa của tệp tham chiếu; `tests/test_spec_conformance.py` chạy lại cùng kịch bản trên cả hai
bản và so khớp từng bước. Lớp này KHÔNG gọi mạng: thời gian luôn được truyền vào, khoá do tầng trên cấp.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

INF = float("inf")
STRUCT_RANK = {"none": 0, "json_object": 1, "json_schema": 2}
TIERS = ("free", "trial", "paid", "self_hosted")
DATA_POLICIES = ("no_training", "may_train", "unknown")
PRIVACY = ("standard", "private")
PRIORITIES = ("high", "normal", "low")
GATES = ("dev", "A", "B", "C")
OUTCOMES = (
    "ok",
    "rate_limited_minute",
    "rate_limited_day",
    "rate_limited_unknown",
    "server_error",
    "timeout",
    "network_error",
    "auth_error",
    "bad_request",
    "context_exceeded",
    "safety_blocked",
    "invalid_output",
    "truncated",
    "canceled",
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
    allow_risk_at_public_gates: bool = (
        False  # chủ hệ thống chấp nhận dùng nhóm gắn cờ rủi ro ngay cả ở cổng B/C (mặc định: không)
    )


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


# ------------------------------------------------------------------------------------------ nạp cấu hình


def _limits(d: dict | None, default_conc: int | None = None) -> Limits:
    d = d or {}
    return Limits(d.get("rpm"), d.get("tpm"), d.get("rpd"), d.get("tpd"), d.get("concurrency", default_conc))


def load_pool_model(cfg: dict) -> PoolModel:
    """Dựng PoolModel từ JSON hợp lệ theo schemas/pool_config.schema.json."""
    pol = cfg.get("policy", {})
    policy = Policy(
        pol.get("priority_reserve", 0.2),
        pol.get("lease_ttl_s", 300.0),
        pol.get("diversity_max_wait_s", 60.0),
        pol.get("allow_risk_at_public_gates", False),
    )
    models = {
        m["id"]: Model(
            m["id"],
            m["provider"],
            m["ctx_in"],
            m["max_out"],
            m.get("structured", "none"),
            m.get("vision", False),
            m.get("pdf", False),
            m.get("tokenizer_factor", 1.0),
            tuple(sorted((m.get("quality") or {}).items())),
        )
        for m in cfg["models"]
    }
    groups: dict[str, Group] = {}
    for g in cfg["groups"]:
        groups[g["id"]] = Group(
            g["id"],
            g["provider"],
            g["tier"],
            g.get("data_policy", "unknown"),
            g.get("reset_tz", "UTC"),
            frozenset(g.get("tos_flags", [])),
            frozenset(g.get("allowed_gates", GATES)),
            g.get("safety_margin", 0.85),
            g.get("day_margin", 0.95),
            _limits(g.get("limits")),
            [Credential(c["id"], c.get("status", "active")) for c in g.get("credentials", [])],
            g.get("enabled", True),
            frozenset(g.get("risk_ack", [])),
        )
    deps = [
        Deployment(
            d["id"],
            groups[d["group"]],
            models[d["model"]],
            _limits(d.get("limits"), 4),
            d.get("tpm_basis", "total"),
            d.get("price_mode", "free"),
            d.get("weight", 1.0),
            frozenset(d.get("tags", [])),
            d.get("enabled", True),
        )
        for d in cfg["deployments"]
    ]
    profiles: dict[str, Profile] = {}
    for p in cfg["profiles"]:
        name = p["name"]
        n = p.get("needs", {})
        needs = Needs(
            n.get("structured", "none"),
            n.get("min_ctx_in", 0),
            n.get("vision", False),
            n.get("pdf", False),
            tuple(sorted((n.get("min_quality") or {}).items())),
        )
        tiers = tuple(
            Tier(
                t["name"],
                frozenset(t.get("select", {}).get("tags", [])),
                frozenset(t.get("select", {}).get("group_tiers", [])),
                t.get("strategy", "headroom"),
                t.get("max_wait_s"),
            )
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
