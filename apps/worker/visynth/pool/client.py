"""`PooledLLMClient`: biến `LLMRequest` của pipeline thành lời gọi thật qua LLM Pool (SPEC §17.6–§17.9).

Trách nhiệm của lớp này, theo đúng thứ tự:

1. Chọn profile theo prompt (`fast`/`writer`/`verifier`/`ocr`/`curator`) và dựng `Request` cho Router.
2. Xin chỗ (`acquire_failover`); gặp `Wait` thì chờ nếu ngắn, quá lâu thì báo hoãn (`deferred`) để tầng trên
   gọi `defer_task` — chờ không phải lỗi (§17.8).
3. Gọi adapter HTTP thật với khoá phân giải từ `secret_ref` (`env:` hoặc `enc:`), rồi `settle` kết quả để
   cooldown/circuit/EWMA hoạt động.
4. Ghi sổ mỗi lời gọi: deployment, nhóm, tầng, `data_policy`, outcome, token, độ trễ — KHÔNG ghi nội dung.
5. Xử lý 429/401: 429 -> cooldown theo `Router` rồi thử deployment khác; 401 -> cách ly khoá + tránh cả nhóm.

Mặc định ghi sổ ra JSONL (`--ledger`), mặc định nằm ngoài repo (`~/.local/state/visynth/llm_calls.jsonl`).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from visynth.llm.base import RETRYABLE, LLMError, LLMRequest, LLMResponse, Outcome
from visynth.pool.adapters import NetworkError, make_adapter
from visynth.pool.model import Impossible, Lease, PoolModel, Request, Wait, load_pool_model
from visynth.pool.registry import Registry, resolve_key
from visynth.pool.router import Router

#: prompt -> profile mặc định (SPEC §5.3 bảng "Ánh xạ prompt → profile").
PROFILE_BY_PROMPT = {
    "P0": "fast",
    "P1": "fast",
    "P2": "fast",
    "P8": "fast",
    "P3": "writer",
    "P4": "writer",
    "P7": "writer",
    "P9": "writer",
    "P5": "verifier",
    "P6": "verifier",
    "P10": "ocr",
    "P12": "curator",
    "P13": "curator",
}
#: ký tự/tham chiếu token (tiếng Việt tốn token hơn tiếng Anh) — hiệu chỉnh dần bằng EWMA từ `usage` thật.
CHARS_PER_TOKEN = 3.2
#: prompt của người VIẾT: nhóm dùng ở đây sẽ bị người kiểm tránh (§17.8, đa dạng người viết/người kiểm).
WRITER_PROMPTS = frozenset({"P3", "P4", "P7", "P9"})
DEFAULT_LEDGER = Path(
    os.environ.get("VISYNTH_LLM_LEDGER") or (Path.home() / ".local" / "state" / "visynth" / "llm_calls.jsonl")
)


@dataclass
class Ledger:
    """Sổ `llm_calls` bản M0 (chưa có CSDL): một dòng JSON cho mỗi lời gọi, không chứa nội dung người dùng."""

    path: Path | None = None
    rows: list[dict] | None = None

    def __post_init__(self) -> None:
        if self.rows is None:
            self.rows = []

    def append(self, row: dict) -> None:
        self.rows.append(row)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def totals(self) -> dict:
        ok = [r for r in self.rows if r["outcome"] == "ok"]
        return {
            "calls": len(self.rows),
            "calls_ok": len(ok),
            "tokens_in": sum(r.get("tokens_in", 0) for r in self.rows),
            "tokens_out": sum(r.get("tokens_out", 0) for r in self.rows),
            "groups": sorted({r["group_id"] for r in self.rows}),
            "deployments": sorted({r["deployment_id"] for r in self.rows}),
            "by_outcome": _counts(r["outcome"] for r in self.rows),
        }


def _counts(items) -> dict:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


class PooledLLMClient:
    """`LLMClient` thật: Router + adapter + khoá + sổ ghi."""

    def __init__(
        self,
        model: PoolModel,
        providers: dict[str, dict],
        credential_refs: dict[str, str],
        *,
        registry: Registry | None = None,
        transport=None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        ledger: Ledger | None = None,
        state: object | None = None,
        gate: str = "dev",
        privacy: str = "standard",
        priority: str = "normal",
        region_restricted: bool = False,
        allow_metered: bool = True,
        seed: int = 7,
        max_attempts: int = 3,
        defer_after_s: float = 60.0,
        max_wait_s: float = 600.0,
        timeout_s: float = 180.0,
    ) -> None:
        self.model = model
        self.providers = providers
        self.credential_refs = credential_refs
        self.registry = registry or Registry()
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.ledger = ledger or Ledger()
        self.gate = gate
        self.privacy = privacy
        self.priority = priority
        self.region_restricted = region_restricted
        self.allow_metered = allow_metered
        self.router = Router(model, state=state, seed=seed)
        self.max_attempts = max_attempts
        self.defer_after_s = defer_after_s
        self.max_wait_s = max_wait_s
        self.timeout_s = timeout_s
        self.factors: dict[str, float] = {}  # model_id -> EWMA tokenizer_factor đo được
        self._group_of_deployment = {d.id: d.group.id for d in model.deployments}
        self._written_by_job: dict[str, set[str]] = {}
        self._quarantined: list[str] = []
        self._adapters: dict[str, Any] = {}

    # ------------------------------------------------------------------ dựng từ pool_config
    @classmethod
    def from_config(cls, cfg: dict, **kw) -> PooledLLMClient:
        providers = {p["id"]: p for p in cfg.get("providers", [])}
        refs = {c["id"]: c.get("secret_ref", "") for g in cfg.get("groups", []) for c in g.get("credentials", [])}
        return cls(load_pool_model(cfg), providers, refs, **kw)

    @classmethod
    def from_db(
        cls,
        db,
        master_key: bytes,
        *,
        ledger: object | None = None,
        user_id: str | None = None,
        stage: str | None = None,
        **kw,
    ) -> PooledLLMClient:
        """Dựng client từ cấu hình pool TRONG CSDL (đường chạy của VPS, SPEC §20).

        Khoá API nằm ở `llm_credentials.secret_enc` và chỉ được giải mã khi thật sự gọi nhà cung cấp;
        `secret_ref = enc:<id>` được phân giải bởi `DbRegistry`. Sổ ghi mặc định là `DbLedger`
        (`llm_calls`) — hàng đợi và sổ sách ở cùng một CSDL nên không có trạng thái thứ hai để lệch.
        """
        from visynth.pool import dbstore

        cfg = dbstore.load_config(db)
        providers = {p["id"]: p for p in cfg.get("providers", [])}
        refs = {c["id"]: c.get("secret_ref", "") for g in cfg.get("groups", []) for c in g.get("credentials", [])}
        if ledger is None:
            ledger = dbstore.DbLedger(db, user_id=user_id, stage=stage)
        if kw.get("state") is None:
            # Trạng thái pool ở CSDL: hạn mức/cooldown/cầu dao/khoá bị cách ly dùng CHUNG cho mọi tiến trình.
            # Để trong RAM thì hai worker cùng tiêu một ngân sách RPM và `ops health` không thấy khoá chết (§20.7).
            from visynth.pool.dbstate import DbPoolState

            kw["state"] = DbPoolState(db)
        return cls(
            load_pool_model(cfg),
            providers,
            refs,
            registry=dbstore.DbRegistry(db, master_key),
            ledger=ledger,
            **kw,
        )

    # ------------------------------------------------------------------ nội bộ
    def _estimate_in(self, request: LLMRequest) -> int:
        return max(1, int(len(request.text) / CHARS_PER_TOKEN))

    def _adapter(self, deployment):
        if deployment.id not in self._adapters:
            provider = self.providers[deployment.group.provider]
            self._adapters[deployment.id] = make_adapter(
                provider, deployment, transport=self.transport, timeout_s=self.timeout_s
            )
        return self._adapters[deployment.id]

    def _key_for(self, lease: Lease) -> str:
        ref = self.credential_refs.get(lease.credential_id)
        if not ref:
            raise KeyError(f"credential '{lease.credential_id}' thiếu secret_ref trong pool_config")
        return resolve_key(ref, self.registry)

    def _row(self, lease: Lease | None, request: LLMRequest, outcome: Outcome, **extra) -> dict:
        d = lease.deployment if lease else None
        row = {
            "ts": self.clock(),
            "job_id": request.metadata.get("job_id"),
            "task_id": request.metadata.get("task_id"),
            "prompt_id": request.prompt_id,
            "profile": request.metadata.get("profile") or PROFILE_BY_PROMPT.get(request.prompt_id, "fast"),
            "deployment_id": d.id if d else None,
            "model": d.model.id if d else None,
            "group_id": d.group.id if d else None,
            "group_tier": d.group.tier if d else None,
            "data_policy": d.group.data_policy if d else None,
            "price_mode": d.price_mode if d else None,
            "credential_id": lease.credential_id if lease else None,
            "diversity_degraded": bool(lease.diversity_degraded) if lease else False,
            "outcome": outcome.kind,
            "tokens_in": int(outcome.tokens_in),
            "tokens_out": int(outcome.tokens_out),
            "latency_ms": outcome.latency_ms,
            "retry_after_s": outcome.retry_after_s,
        }
        row.update(extra)
        return row

    def _learn_factor(self, deployment, est_in: int, tokens_in: int) -> None:
        if not tokens_in or not est_in:
            return
        observed = tokens_in / est_in
        old = self.factors.get(deployment.model.id)
        self.factors[deployment.model.id] = observed if old is None else 0.7 * old + 0.3 * observed

    def _quarantine(self, lease: Lease) -> None:
        """401/403: khoá sai hoặc đã bị thu hồi — cách ly để không thử lại, và tránh cả nhóm ở lượt sau."""
        for c in lease.deployment.group.credentials:
            if c.id == lease.credential_id:
                c.status = "quarantined"
        self._quarantined.append(lease.credential_id)

    def _avoid_groups(self, request: LLMRequest) -> frozenset:
        job = str(request.metadata.get("job_id") or "")
        if request.prompt_id in ("P5", "P6") and job:
            return frozenset(self._written_by_job.get(job, set()))
        return frozenset()

    def _note_writer(self, request: LLMRequest, lease: Lease) -> None:
        if request.prompt_id in WRITER_PROMPTS:
            job = str(request.metadata.get("job_id") or "")
            self._written_by_job.setdefault(job, set()).add(lease.deployment.group.id)

    # ------------------------------------------------------------------ giao diện LLMClient
    def complete(self, request: LLMRequest) -> LLMResponse:
        prof_name = request.metadata.get("profile") or PROFILE_BY_PROMPT.get(request.prompt_id, "fast")
        if prof_name not in self.model.profiles:
            raise LLMError(Outcome("impossible"), f"pool_config thiếu profile '{prof_name}'")
        est_in = self._estimate_in(request)
        base = Request(
            profile=prof_name,
            est_in=est_in,
            est_out=request.max_output_tokens,
            privacy=request.metadata.get("privacy", self.privacy),
            priority=request.metadata.get("priority", self.priority),
            gate=request.metadata.get("gate", self.gate),
            region_restricted=bool(request.metadata.get("region_restricted", self.region_restricted)),
            allow_metered=bool(request.metadata.get("allow_metered", self.allow_metered)),
            needs_vision=bool(request.needs.get("vision")),
            needs_pdf=bool(request.needs.get("pdf")),
            avoid_groups=self._avoid_groups(request),
            job_id=request.metadata.get("job_id"),
            task_id=request.metadata.get("task_id"),
        )
        exclude: set[str] = set()
        waited = 0.0
        attempts = 0
        while True:
            now = self.clock()
            pick = self.router.acquire_failover(replace(base, exclude=frozenset(exclude)), now)
            if isinstance(pick, Impossible):
                self.ledger.append(self._row(None, request, Outcome("impossible"), detail=pick.reason))
                raise LLMError(Outcome("impossible"), pick.reason)
            if isinstance(pick, Wait):
                wait_s = max(0.0, pick.until - now)
                if wait_s > self.defer_after_s or waited + wait_s > self.max_wait_s:
                    self.ledger.append(
                        self._row(None, request, Outcome("deferred"), detail=pick.reason, wait_s=round(wait_s, 1))
                    )
                    raise LLMError(
                        Outcome("deferred", retry_after_s=wait_s),
                        f"pool hết chỗ ({pick.reason}); hoãn tác vụ tới {time.strftime('%H:%M:%S', time.localtime(pick.until))}",
                    )
                self.sleep(wait_s)
                waited += wait_s
                continue

            lease = pick
            deployment = lease.deployment
            try:
                api_key = self._key_for(lease)
            except Exception as e:
                # Khoá chưa có trong môi trường/kho: deployment này không dùng được. Thử chỗ khác thay vì
                # làm hỏng cả job; chỉ báo lỗi khi hết đường.
                attempts += 1
                exclude.add(deployment.id)
                self.ledger.append(
                    self._row(lease, request, Outcome("auth_error"), detail=f"thiếu khoá: {e}", missing_key=True)
                )
                if attempts >= self.max_attempts:
                    raise LLMError(Outcome("auth_error"), f"không phân giải được khoá cho {deployment.id}: {e}") from e
                continue
            try:
                response = self._adapter(deployment).complete(request, api_key)
            except NetworkError as e:
                response = LLMResponse(
                    text="",
                    outcome=Outcome("network_error", latency_ms=None),
                    model=deployment.model.id,
                    deployment_id=deployment.id,
                )
                detail = str(e)
            else:
                detail = response.text[:200]
            settle_now = self.clock()
            self.router.settle(lease, response.outcome, settle_now)
            self._learn_factor(deployment, est_in, response.outcome.tokens_in)
            self.ledger.append(self._row(lease, request, response.outcome, est_in=est_in))
            if response.outcome.kind == "ok":
                self._note_writer(request, lease)
                return response
            if response.outcome.kind == "auth_error":
                self._quarantine(lease)
                raise LLMError(
                    response.outcome,
                    f"khoá bị từ chối ở {deployment.id} ({detail}); đã cách ly khoá — kiểm tra và xoay khoá",
                    deployment_id=deployment.id,
                )
            if response.outcome.kind in RETRYABLE or response.outcome.kind == "invalid_output":
                attempts += 1
                exclude.add(deployment.id)
                if attempts >= self.max_attempts:
                    raise LLMError(
                        response.outcome,
                        f"{attempts} deployment thất bại ({response.outcome.kind}) cho {request.prompt_id}",
                        deployment_id=deployment.id,
                    )
                if response.outcome.kind in ("server_error", "timeout", "network_error"):
                    self.sleep(min(2.0**attempts, 10.0))
                continue
            raise LLMError(response.outcome, detail, deployment_id=deployment.id)

    # ------------------------------------------------------------------ tiện ích
    def ledger_totals(self) -> dict:
        totals = self.ledger.totals()
        totals["quarantined"] = sorted(set(self._quarantined))
        totals["factors"] = {k: round(v, 3) for k, v in sorted(self.factors.items())}
        return totals
