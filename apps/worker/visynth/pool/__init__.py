"""LLM Pool (SPEC §17): hạn mức, đặt chỗ nguyên tử, định tuyến theo tầng, chuyển dự phòng, khai báo khoá.

Bản port của `docs/reference/llm_pool.py` + `docs/reference/pool_declare.py` + `docs/reference/pool_secrets.py`:
`model` (cấu hình), `state` (đặt chỗ), `router` (định tuyến), `policy_io` (khai báo), `secrets` (mã hoá khoá),
`registry` (kho khoá ngoài repo), `adapters` + `client` (gọi HTTP thật), `simulate` (mô phỏng rời rạc).

`Outcome`/`classify_http` nằm ở `visynth.llm.base` (W2) và được tái dùng, không định nghĩa lại.
"""

from __future__ import annotations

from visynth.llm.base import RETRYABLE, Outcome, classify_http
from visynth.pool.client import PROFILE_BY_PROMPT, Ledger, PooledLLMClient
from visynth.pool.model import (
    GATES,
    INF,
    PRIVATE_BLOCKING_FLAGS,
    PUBLIC_BLOCKING_FLAGS,
    PUBLIC_GATES,
    RISK_FLAGS,
    Credential,
    Deployment,
    Group,
    Impossible,
    Lease,
    Limits,
    Model,
    Needs,
    Policy,
    PoolModel,
    Profile,
    Request,
    ReserveResult,
    Tier,
    Wait,
    day_key,
    deployment_daily_capacity,
    load_pool_model,
    next_day_boundary,
)
from visynth.pool.registry import Registry, resolve_key
from visynth.pool.router import Router
from visynth.pool.state import MemoryState

__all__ = [
    "GATES",
    "PROFILE_BY_PROMPT",
    "INF",
    "PRIVATE_BLOCKING_FLAGS",
    "PUBLIC_BLOCKING_FLAGS",
    "PUBLIC_GATES",
    "RETRYABLE",
    "RISK_FLAGS",
    "Credential",
    "Deployment",
    "Group",
    "Impossible",
    "Ledger",
    "Lease",
    "Limits",
    "MemoryState",
    "Model",
    "Needs",
    "Outcome",
    "Policy",
    "PoolModel",
    "PooledLLMClient",
    "Profile",
    "Registry",
    "Request",
    "ReserveResult",
    "Router",
    "Tier",
    "Wait",
    "classify_http",
    "day_key",
    "deployment_daily_capacity",
    "load_pool_model",
    "resolve_key",
    "next_day_boundary",
]
