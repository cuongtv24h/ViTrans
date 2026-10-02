"""Kiểm tra `pool_config` trước khi dùng (SPEC §17.3, §17.14): schema + ràng buộc cấu trúc + quét rò khoá.

`validate_config` không gọi mạng và không cần khoá thật: nó chỉ đọc tệp cấu hình và trả về báo cáo. Nhờ vậy
chạy được trong CI và trước mỗi lần `declare --write`.
"""

from __future__ import annotations

import re
from pathlib import Path

from visynth.pool.model import PRIVATE_BLOCKING_FLAGS, PUBLIC_BLOCKING_FLAGS, RISK_FLAGS, load_pool_model

#: mẫu khoá trông giống khoá thật — dùng để phát hiện dán khoá vào pool_config
_KEY_LIKE = re.compile(
    r"(AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_\-]{20,}|Bearer\s+[A-Za-z0-9._\-]{20,})"
)


def validate_config(cfg: dict, *, schemas_dir: str | Path | None = None) -> dict:
    """Trả về `{valid, errors, warnings, counts, summary_vi}`. Không ném lỗi vì cấu hình sai."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        from visynth.prompts import default_schemas_dir
        from visynth.structured import SchemaStore

        store = SchemaStore(Path(schemas_dir) if schemas_dir else default_schemas_dir())
        store.validate(cfg, store.load("pool_config.schema.json"), what="pool_config")
    except Exception as e:  # SchemaError hoặc lỗi I/O
        extra = getattr(e, "errors", None)
        if extra:
            errors += [f"schema: {m}" for m in extra]
        else:
            errors.append(f"schema: {e}")

    if _KEY_LIKE.search(_config_text(cfg)):
        errors.append("cấu hình chứa chuỗi trông như khoá API thật — chỉ dùng `secret_ref` (env:TÊN hoặc enc:id)")

    try:
        model = load_pool_model(cfg)
    except Exception as e:
        errors.append(f"không dựng được PoolModel: {e}")
        return _report(errors, warnings, cfg)

    if not model.profiles:
        errors.append("thiếu `profiles`: pipeline chỉ nói profile, không nói model")
    if not model.deployments:
        errors.append("không có deployment nào đang bật")
    for g in model.groups.values():
        missing = sorted((g.tos_flags & RISK_FLAGS) - g.risk_ack)
        if missing:
            errors.append(f"nhóm {g.id}: cờ rủi ro {missing} chưa có xác nhận `risk_ack` (SPEC §17.14)")
        if g.data_policy == "may_train":
            warnings.append(f"nhóm {g.id}: dữ liệu có thể được dùng để huấn luyện — chỉ dùng cho job `standard`")
        if not g.credentials:
            warnings.append(f"nhóm {g.id}: chưa có credential nào")
        for c in g.credentials:
            if c.status != "active":
                warnings.append(f"credential {c.id}: trạng thái '{c.status}' — sẽ bị bỏ qua")
    for d in model.deployments:
        if d.price_mode == "metered":
            warnings.append(f"deployment {d.id}: tính phí — cần trần chi tiêu ngày")
        if d.model.structured == "none":
            warnings.append(f"deployment {d.id}: model không khai structured output — chỉ dùng bậc thang prompt-only")
        if not d.model.quality:
            warnings.append(
                f"deployment {d.id}: chưa có điểm kiểm định (`probe`) — chỉ dùng cho profile không đòi quality"
            )
    for name, prof in model.profiles.items():
        if not prof.tiers:
            errors.append(f"profile {name}: không có tầng nào")
        if not any(t.select_group_tiers or t.select_tags for t in prof.tiers):
            warnings.append(f"profile {name}: tầng đầu không lọc gì — dễ dùng nhầm tầng trả phí trước")
    for d, tags in ((d, d.tags) for d in model.deployments):
        if not tags:
            warnings.append(f"deployment {d.id}: không có tag — tầng chọn theo tag sẽ bỏ qua nó")

    if (model.policy.allow_risk_at_public_gates) and any(
        g.tos_flags & PUBLIC_BLOCKING_FLAGS for g in model.groups.values()
    ):
        warnings.append("policy.allow_risk_at_public_gates = true: nhóm gắn cờ rủi ro được dùng cả ở cổng B/C")
    if any(g.tos_flags & PRIVATE_BLOCKING_FLAGS and g.data_policy == "no_training" for g in model.groups.values()):
        warnings.append("có nhóm vừa `no_training` vừa gắn cờ chặn job riêng tư — job `private` sẽ không dùng được")
    return _report(errors, warnings, cfg)


def _config_text(cfg: dict) -> str:
    import json

    return json.dumps(cfg, ensure_ascii=False)


def _report(errors: list[str], warnings: list[str], cfg: dict) -> dict:
    counts = {
        "providers": len(cfg.get("providers", [])),
        "models": len(cfg.get("models", [])),
        "groups": len(cfg.get("groups", [])),
        "deployments": len(cfg.get("deployments", [])),
        "profiles": len(cfg.get("profiles", [])),
        "credentials": sum(len(g.get("credentials", [])) for g in cfg.get("groups", [])),
    }
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": counts,
        "deployments": counts["deployments"],
        "summary_vi": (
            f"pool_config {'HỢP LỆ' if not errors else 'KHÔNG hợp lệ'}: "
            f"{counts['providers']} nhà cung cấp, {counts['models']} model, {counts['groups']} nhóm, "
            f"{counts['credentials']} credential, {counts['deployments']} deployment, {counts['profiles']} profile."
        ),
    }
