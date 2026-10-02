"""Kiểm định deployment (`probe`, SPEC §17.9): đo thật thay vì tin lời khai trong `pool_config`.

Bài kiểm định bản M0 gồm hai phần chạy được không cần tài liệu người dùng:

* `json` — một lời gọi structured output nhỏ, chấm theo bậc thang hỗ trợ của model (`json_schema` → `json_object`
  → prompt-only) và tỷ lệ JSON hợp lệ sau tối đa một lần thử lại;
* `vi_write` — một đoạn tiếng Việt ~60 từ, chấm bằng chính các kiểm tra tất định của sản phẩm
  (tỷ lệ chữ có dấu, độ dài, không có câu dẫn kiểu trợ lý).

Kèm theo đó là số đo dùng để hiệu chỉnh cấu hình: `tokenizer_factor` thật, độ trễ p50, và phản hồi lỗi thật
(để đối chiếu `classify_http`). Kết quả ghi ra JSON; KHÔNG tự sửa `pool_config` (chủ hệ thống duyệt rồi mới nhập),
nhưng có kèm `suggest_updates` để dán tay.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from visynth.checks.report import diacritic_ratio
from visynth.llm.base import LLMRequest
from visynth.pool.adapters import NetworkError, make_adapter
from visynth.pool.client import CHARS_PER_TOKEN
from visynth.pool.model import PoolModel, load_pool_model
from visynth.pool.registry import Registry, resolve_key

JSON_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "chao": {"type": "string"}},
    "required": ["ok", "chao"],
    "additionalProperties": False,
}
JSON_SYSTEM = "Bạn trả về JSON đúng schema, không thêm chữ nào ngoài JSON."
JSON_USER = 'Trả về đúng một đối tượng JSON: {"ok": true, "chao": "<một câu chào ngắn bằng tiếng Việt>"}.'
VI_SYSTEM = "Bạn viết tiếng Việt tự nhiên, văn phong trung tính, không mở bài kiểu trợ lý."
VI_USER = (
    "Viết một đoạn khoảng 60 từ bằng tiếng Việt giải thích vì sao báo cáo tổng hợp phải kiểm chứng số liệu "
    "trước khi công bố. Chỉ trả về đoạn văn."
)
#: câu dẫn kiểu trợ lý bị coi là lỗi ở D6 (§6.8) — probe dùng lại đúng danh sách đó.
ASSISTANT_OPENERS = ("dưới đây là", "tất nhiên", "là một ai", "tôi không thể", "chắc chắn rồi")


def _guess_reference_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def _factor_from(tokens_in: int, system: str, user: str) -> float | None:
    """`tokenizer_factor` đo được = token thật nhà cung cấp báo / ước lượng theo `CHARS_PER_TOKEN`.

    Dùng ĐÚNG công thức ước lượng của `client._estimate_in` (`request.text`) để con số đo được có thể
    dán thẳng vào `pool_config[].tokenizer_factor`.
    """
    if not tokens_in:
        return None
    return round(tokens_in / _guess_reference_tokens(f"{system}\n\n{user}"), 3)


def probe_deployment(
    deployment,
    provider: dict,
    *,
    credential_refs: dict[str, str] | None = None,
    credential_id: str | None = None,
    registry: Registry | None = None,
    transport=None,
    timeout_s: float = 90.0,
    with_vi_write: bool = True,
) -> dict:
    """Chạy bài kiểm định trên MỘT deployment. Không bao giờ ném lỗi vì lỗi nhà cung cấp — ghi lại rồi trả về."""
    reg = registry or Registry()
    ref = next((c for c in deployment.group.credentials if c.id == credential_id), None) if credential_id else None
    if ref is None:
        ref = next((c for c in deployment.group.credentials if c.status == "active"), None)
    ref = ref or next((c for c in deployment.group.credentials), None)
    run: dict[str, Any] = {
        "deployment_id": deployment.id,
        "model": deployment.model.id,
        "group": deployment.group.id,
        "group_tier": deployment.group.tier,
        "data_policy": deployment.group.data_policy,
        "credential_id": ref.id if ref else None,
        "checked_at": time.time(),
        "structured_declared": deployment.model.structured,
        "json": {"ok": False, "ladder": [], "detail": ""},
        "vi_write": None,
        "latency_ms": [],
        "tokenizer_factor": None,
        "tokenizer_source": None,
        "errors": [],
    }
    refs = credential_refs or {}
    secret_ref = refs.get(ref.id) if ref else None
    if not secret_ref:
        run["errors"].append(f"credential {run['credential_id']} thiếu secret_ref")
        return run
    try:
        api_key = resolve_key(secret_ref, reg)
    except Exception as e:
        run["errors"].append(f"không phân giải được khoá: {e}")
        return run

    # ---- json: thử theo bậc thang hỗ trợ thật của model
    ladder = [deployment.model.structured] if deployment.model.structured != "none" else ["json_object"]
    ladder = ladder + [lv for lv in ("json_object", "none") if lv not in ladder]
    for level in ladder:
        adapter = make_adapter(
            provider,
            replace(deployment, model=replace(deployment.model, structured=level)),
            transport=transport,
            timeout_s=timeout_s,
        )
        req = LLMRequest(
            prompt_id="PROBE",
            system=JSON_SYSTEM,
            user=JSON_USER,
            schema=JSON_SCHEMA,
            max_output_tokens=128,
            temperature=0.0,
        )
        try:
            resp = adapter.complete(req, api_key)
        except NetworkError as e:
            run["errors"].append(f"{level}: {e}")
            break
        entry = {
            "level": level,
            "outcome": resp.outcome.kind,
            "latency_ms": resp.latency_ms,
            "tokens_in": resp.outcome.tokens_in,
            "tokens_out": resp.outcome.tokens_out,
        }
        run["latency_ms"].append(resp.latency_ms)
        if run["tokenizer_factor"] is None:
            factor = _factor_from(resp.outcome.tokens_in, JSON_SYSTEM, req.user)
            if factor:
                run["tokenizer_factor"], run["tokenizer_source"] = factor, "json"
        if resp.outcome.kind == "ok":
            try:
                payload = json.loads(resp.text)
                entry["valid"] = isinstance(payload, dict) and "ok" in payload and "chao" in payload
            except ValueError:
                entry["valid"] = False
            if entry["valid"]:
                run["json"] = {"ok": True, "ladder": run["json"]["ladder"] + [entry], "detail": f"đạt ở mức {level}"}
                break
            entry["retry"] = True
            req2 = replace(req, user=JSON_USER + " Chỉ JSON, không kèm giải thích.")
            try:
                resp2 = adapter.complete(req2, api_key)
                run["latency_ms"].append(resp2.latency_ms)
                if run["tokenizer_factor"] is None:
                    factor2 = _factor_from(resp2.outcome.tokens_in, JSON_SYSTEM, req2.user)
                    if factor2:
                        run["tokenizer_factor"], run["tokenizer_source"] = factor2, "json"
                payload2 = json.loads(resp2.text) if resp2.outcome.kind == "ok" else None
                entry["retry_valid"] = isinstance(payload2, dict) and "ok" in payload2
                if entry["retry_valid"]:
                    run["json"] = {
                        "ok": True,
                        "ladder": run["json"]["ladder"] + [entry],
                        "detail": f"đạt sau 1 lần nhắc ở mức {level}",
                    }
                    break
            except (ValueError, NetworkError) as e:
                entry["retry_error"] = str(e)
        elif resp.outcome.kind in ("auth_error", "context_exceeded", "bad_request"):
            run["json"] = {"ok": False, "ladder": run["json"]["ladder"] + [entry], "detail": resp.outcome.kind}
            run["errors"].append(f"{level}: {resp.outcome.kind}")
            break  # lỗi khoá/quyền thì các mức sau cũng vậy
        run["json"] = {
            "ok": False,
            "ladder": run["json"]["ladder"] + [entry],
            "detail": f"mức {level}: {resp.outcome.kind}",
        }

    # ---- vi_write: đo chất lượng tiếng Việt bằng kiểm tra tất định của sản phẩm
    if with_vi_write:
        level = deployment.model.structured if deployment.model.structured != "none" else "none"
        adapter = make_adapter(
            provider,
            replace(deployment, model=replace(deployment.model, structured=level)),
            transport=transport,
            timeout_s=timeout_s,
        )
        req = LLMRequest(
            prompt_id="PROBE", system=VI_SYSTEM, user=VI_USER, schema=None, max_output_tokens=256, temperature=0.4
        )
        try:
            resp = adapter.complete(req, api_key)
        except NetworkError as e:
            run["errors"].append(f"vi_write: {e}")
        else:
            run["latency_ms"].append(resp.latency_ms)
            text = resp.text.strip()
            words = text.split()
            ratio = diacritic_ratio(text)
            opener = next((o for o in ASSISTANT_OPENERS if text.lower().lstrip().startswith(o)), None)
            run["vi_write"] = {
                "outcome": resp.outcome.kind,
                "words": len(words),
                "diacritic_ratio": round(ratio, 4),
                "assistant_opener": opener,
                "ok": resp.outcome.kind == "ok" and len(words) >= 40 and ratio >= 0.12 and opener is None,
            }
            if run["tokenizer_factor"] is None:
                factor3 = _factor_from(resp.outcome.tokens_in, VI_SYSTEM, VI_USER)
                if factor3:
                    run["tokenizer_factor"], run["tokenizer_source"] = factor3, "vi_write"

    lat = [x for x in run["latency_ms"] if x]
    run["latency_p50_ms"] = int(statistics.median(lat)) if lat else None
    return run


def probe_pool(
    cfg: dict,
    *,
    only: list[str] | None = None,
    registry: Registry | None = None,
    transport=None,
    with_vi_write: bool = True,
    timeout_s: float = 90.0,
) -> dict:
    """Chạy `probe` trên mọi deployment đang bật (hoặc `only`). Trả về báo cáo + gợi ý cập nhật."""
    model: PoolModel = load_pool_model(cfg)
    providers = {p["id"]: p for p in cfg.get("providers", [])}
    refs = {c["id"]: c.get("secret_ref", "") for g in cfg.get("groups", []) for c in g.get("credentials", [])}
    runs = []
    for d in model.deployments:
        if not (d.enabled and d.group.enabled):
            continue
        if only and d.id not in only:
            continue
        runs.append(
            probe_deployment(
                d,
                providers[d.group.provider],
                credential_refs=refs,
                registry=registry,
                transport=transport,
                timeout_s=timeout_s,
                with_vi_write=with_vi_write,
            )
        )
    return {"runs": runs, "suggest_updates": suggest_updates(runs), "totals": _totals(runs)}


def _totals(runs: list[dict]) -> dict:
    return {
        "deployments": len(runs),
        "json_ok": sum(1 for r in runs if r["json"]["ok"]),
        "vi_write_ok": sum(1 for r in runs if (r.get("vi_write") or {}).get("ok")),
        "with_errors": sum(1 for r in runs if r["errors"]),
    }


def suggest_updates(runs: list[dict]) -> dict:
    """Gợi ý dán vào `pool_config` (chủ hệ thống tự quyết): điểm `json`, `vi_write`, `tokenizer_factor`, ghi chú."""
    out: dict[str, dict] = {}
    for r in runs:
        if not r.get("json") and not r.get("vi_write"):
            continue
        entry: dict[str, Any] = {}
        entry["quality"] = {
            "json": 1.0 if r["json"]["ok"] else (0.0 if r["json"]["ladder"] else None),
            "vi_write": 1.0 if (r.get("vi_write") or {}).get("ok") else 0.0,
        }
        if r.get("tokenizer_factor"):
            entry["tokenizer_factor"] = r["tokenizer_factor"]
        if r["errors"]:
            entry["notes"] = "probe lỗi: " + "; ".join(r["errors"])
        out[r["deployment_id"]] = entry
    return out


def write_report(report: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


__all__ = ["ASSISTANT_OPENERS", "JSON_SCHEMA", "probe_deployment", "probe_pool", "suggest_updates", "write_report"]
