"""Đọc/ghi cấu hình LLM Pool trong PostgreSQL (M1 — Admin API).

Bản tham chiếu (M0-W3) làm việc trên tệp `pool_config.json`; production lấy cấu hình từ CSDL để
nhiều tiến trình cùng thấy một sự thật và để khoá API nằm ở dạng mã hoá (`llm_credentials.secret_enc`).

Giao diện `db` cần thiết: `one(sql, params)`, `all(sql, params)`, `scalar(sql, params)`, `execute(sql, params)`
— đúng lớp `visynth_api.db.Database` (và tương thích mọi lớp bọc psycopg có cùng tên hàm).

Bất biến giữ nguyên như bản tệp:
  * khoá API KHÔNG bao giờ rời khỏi tiến trình ở dạng rõ: chỉ ghi vào `secret_enc`, chỉ đọc ra khi
    thật sự gọi nhà cung cấp; mọi phản hồi API chỉ có `last4` + `fingerprint`;
  * nhóm có cờ rủi ro (`multi_account_risk`, `trial_only`) không bật được nếu chưa `risk_ack`
    (ràng buộc CHECK trong `docs/db/schema.sql` — tầng này chỉ dịch lỗi cho dễ hiểu);
  * mọi thay đổi cấu hình đều ghi `audit_log` và tăng `app_settings.pool_version`;
  * bản xuất che khoá (`secret_ref = enc:<id>`); nhập lại chính bản đó thì bí mật đang có trong CSDL
    được khôi phục nguyên vẹn, còn `enc:<id>` lạ (chưa từng có trong CSDL) bị TỪ CHỐI thay vì
    tạo ra một khoá rỗng.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from visynth.pool import secrets as pool_secrets
from visynth.pool.policy_io import DeclareResult, compile_all, merge_fragment
from visynth.pool.registry import Registry
from visynth.pool.validate import validate_config

#: Khoá `app_settings` giữ phần `policy` của PoolConfig.
#: Ghi chú mặc định cho bản xuất cấu hình (trường `note` bắt buộc theo schema).
DEFAULT_NOTE_VI = "Bản xuất từ CSDL — khoá API chỉ ở dạng tham chiếu đã che (enc:<id>), không có bí mật nào trong tệp."

POLICY_KEYS = {
    "priority_reserve": "pool_priority_reserve",
    "lease_ttl_s": "pool_lease_ttl_s",
    "diversity_max_wait_s": "pool_diversity_max_wait_s",
    "allow_risk_at_public_gates": "pool_allow_risk_at_public_gates",
}

_GROUP_LIMIT_COLUMNS = ("rpm", "tpm", "rpd", "tpd", "concurrency")
_DEPLOY_LIMIT_COLUMNS = ("rpm", "tpm", "rpd", "tpd", "concurrency")


# --------------------------------------------------------------------------- đọc cấu hình


def _limits(row: dict, columns: tuple[str, ...]) -> dict:
    return {name: row.get(name) for name in columns}


def load_config(db: Any) -> dict:
    """PoolConfig từ CSDL. Khoá chỉ ở dạng tham chiếu `enc:<id>` (không có bí mật nào trong kết quả)."""
    providers = [
        {
            "id": row["id"],
            "kind": row["kind"],
            "base_url": row["base_url"],
            "display_name": row["display_name"],
            "quirks": dict(row["quirks"] or {}),
        }
        for row in db.all("SELECT * FROM llm_providers ORDER BY id")
    ]
    models = [
        {
            "id": row["id"],
            "provider": row["provider_id"],
            "model_id": row["model_id"],
            "ctx_in": row["ctx_in"],
            "max_out": row["max_out"],
            "structured": row["structured"],
            "vision": row["vision"],
            "pdf": row["pdf"],
            "tokenizer_factor": row["tokenizer_factor"],
            "price_key": row["price_key"],
            "shadow_price_key": row["shadow_price_key"],
            "quality": dict(row["quality"] or {}),
            "adapter_options": dict(row["adapter_options"] or {}),
        }
        for row in db.all("SELECT * FROM llm_models ORDER BY id")
    ]
    credentials: dict[str, list[dict]] = {}
    for row in db.all(
        "SELECT id, group_id, label, last4, status, quarantined_reason, last_used_at, secret_ref, "
        "       (secret_enc IS NOT NULL) AS encrypted "
        "FROM llm_credentials ORDER BY group_id, id"
    ):
        ref = row["secret_ref"] or (f"enc:{row['id']}" if row["encrypted"] else None)
        credentials.setdefault(row["group_id"], []).append(
            {
                "id": row["id"],
                "label": row["label"],
                "secret_ref": ref or f"enc:{row['id']}",
                "last4": row["last4"],
                "status": row["status"],
                "quarantined_reason": row["quarantined_reason"],
                "last_used_at": row["last_used_at"],
            }
        )
    groups = [
        {
            "id": row["id"],
            "provider": row["provider_id"],
            "label": row["label"],
            "tier": row["tier"],
            "data_policy": row["data_policy"],
            "reset_tz": row["reset_tz"],
            "tos_flags": list(row["tos_flags"] or []),
            "allowed_gates": list(row["allowed_gates"] or []),
            "safety_margin": row["safety_margin"],
            "day_margin": row["day_margin"],
            "limits": _limits(row, _GROUP_LIMIT_COLUMNS),
            "credentials": credentials.get(row["id"], []),
            "enabled": row["enabled"],
            "risk_ack": list(row["risk_ack_flags"] or []),
        }
        for row in db.all("SELECT * FROM llm_quota_groups ORDER BY id")
    ]
    deployments = [
        {
            "id": row["id"],
            "group": row["group_id"],
            "model": row["model_id"],
            "limits": _limits(row, _DEPLOY_LIMIT_COLUMNS),
            "tpm_basis": row["tpm_basis"],
            "price_mode": row["price_mode"],
            "weight": row["weight"],
            "tags": list(row["tags"] or []),
            "enabled": row["enabled"],
        }
        for row in db.all("SELECT * FROM llm_deployments ORDER BY id")
    ]
    profiles = []
    for row in db.all("SELECT * FROM llm_profiles ORDER BY name"):
        tiers = db.all("SELECT * FROM llm_profile_tiers WHERE profile_name = %s ORDER BY tier_no", (row["name"],))
        profiles.append(
            {
                "name": row["name"],
                "needs": dict(row["needs"] or {}),
                "tiers": [
                    {
                        "name": tier["name"],
                        "select": {
                            "tags": list(tier["select_tags"] or []),
                            "group_tiers": list(tier["select_group_tiers"] or []),
                        },
                        "strategy": tier["strategy"],
                        "max_wait_s": tier["max_wait_s"],
                    }
                    for tier in tiers
                ],
            }
        )
    settings = {
        row["key"]: row["value"]
        for row in db.all(
            "SELECT key, value FROM app_settings WHERE key = ANY(%s::text[])", ([*POLICY_KEYS.values(), "pool_note"],)
        )
    }
    policy = {
        "priority_reserve": float(settings.get("pool_priority_reserve", 0.2)),
        "lease_ttl_s": float(settings.get("pool_lease_ttl_s", 120)),
        "diversity_max_wait_s": float(settings.get("pool_diversity_max_wait_s", 30)),
        "allow_risk_at_public_gates": bool(settings.get("pool_allow_risk_at_public_gates", False)),
    }
    version = db.one("SELECT value FROM app_settings WHERE key = 'pool_version'")
    return {
        "version": int((version or {}).get("value") or 1),
        # Bản xuất PHẢI đọc lại được bằng chính API nhập (`PUT /admin/pool/config`) — `note` là bắt buộc.
        "note": str(settings.get("pool_note") or DEFAULT_NOTE_VI)[:500],
        "providers": providers,
        "models": models,
        "groups": groups,
        "deployments": deployments,
        "profiles": profiles,
        "policy": policy,
    }


#: Trường chỉ để HIỂN THỊ mà `GET /admin/pool/config` trả thêm (schema không cho phép) —
#: khi nhập lại chính bản xuất đó thì bỏ qua chứ không báo lỗi, nhưng luôn nói rõ đã bỏ gì.
_DISPLAY_FIELDS = {
    "credentials": ("last4", "status", "quarantined_reason", "last_used_at"),
    "groups": ("notes",),
    "providers": ("enabled",),
    "profiles": ("version",),
    "tiers": ("tier_no",),
}
_SECTION_KEYS = {
    "providers": ("id", "kind", "base_url", "display_name", "quirks"),
    "models": (
        "id",
        "provider",
        "model_id",
        "ctx_in",
        "max_out",
        "structured",
        "vision",
        "pdf",
        "tokenizer_factor",
        "price_key",
        "shadow_price_key",
        "quality",
        "adapter_options",
    ),
    "groups": (
        "id",
        "provider",
        "label",
        "tier",
        "data_policy",
        "reset_tz",
        "tos_flags",
        "allowed_gates",
        "safety_margin",
        "day_margin",
        "limits",
        "credentials",
        "enabled",
        "risk_ack",
    ),
    "deployments": ("id", "group", "model", "limits", "tpm_basis", "price_mode", "weight", "tags", "enabled"),
    "profiles": ("name", "needs", "tiers"),
}
_ROOT_KEYS = ("version", "note", "policy", "providers", "models", "groups", "deployments", "profiles")


def sanitize_config(cfg: dict) -> tuple[dict, list[str]]:
    """Bỏ các trường chỉ để hiển thị khỏi một tài liệu cấu hình. Trả `(cấu hình sạch, danh sách đã bỏ)`.

    Nhờ vậy `GET /admin/pool/config` → `PUT /admin/pool/config` là vòng tròn khép kín: bản xuất có thêm
    `last4`/`status`/… cho Admin nhìn, còn lúc nhập thì chúng bị bỏ qua một cách minh bạch.
    """
    clean = {key: cfg[key] for key in _ROOT_KEYS if key in cfg}
    dropped: list[str] = []
    for section, allowed in _SECTION_KEYS.items():
        items = []
        for index, item in enumerate(cfg.get(section) or []):
            item = dict(item)
            for key in list(item):
                if key not in allowed:
                    item.pop(key)
                    dropped.append(f"{section}[{index}].{key}")
            if section == "groups":
                creds = []
                for cindex, cred in enumerate(item.get("credentials") or []):
                    cred = dict(cred)
                    for key in list(cred):
                        if key not in ("id", "label", "secret_ref"):
                            cred.pop(key)
                            dropped.append(f"{section}[{index}].credentials[{cindex}].{key}")
                    creds.append(cred)
                item["credentials"] = creds
            if section == "profiles":
                tiers = []
                for tindex, tier in enumerate(item.get("tiers") or []):
                    tier = dict(tier)
                    for key in list(tier):
                        if key not in ("name", "select", "strategy", "max_wait_s"):
                            tier.pop(key)
                            dropped.append(f"{section}[{index}].tiers[{tindex}].{key}")
                    tiers.append(tier)
                item["tiers"] = tiers
            items.append(item)
        if section in cfg:
            clean[section] = items
    return clean, dropped


def config_diff(old: dict, new: dict) -> dict:
    """So sánh hai PoolConfig theo `giường/khóa` (ví dụ `groups/free-gemini`)."""
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    for section in ("providers", "models", "groups", "deployments", "profiles"):
        old_map = {str(item.get("id") or item.get("name")): item for item in old.get(section) or []}
        new_map = {str(item.get("id") or item.get("name")): item for item in new.get(section) or []}
        for key, value in new_map.items():
            item = f"{section}/{key}"
            if key not in old_map:
                added.append(item)
            elif _strip(value) != _strip(old_map[key]):
                changed.append(item)
        removed += [f"{section}/{key}" for key in old_map if key not in new_map]
    if old.get("policy") != new.get("policy"):
        changed.append("policy")
    return {"added": sorted(added), "removed": sorted(removed), "changed": sorted(changed)}


def _strip(item: dict) -> dict:
    """Bỏ các trường chỉ để hiển thị (last4, trạng thái khoá) trước khi so sánh."""
    out = copy.deepcopy(item)
    if "credentials" in out:
        for cred in out["credentials"]:
            for field in ("last4", "status", "quarantined_reason", "last_used_at"):
                cred.pop(field, None)
    out.pop("version", None)
    return out


# --------------------------------------------------------------------------- ghi cấu hình


def _audit(db: Any, actor_id: str | None, action: str, target: str, detail: dict) -> None:
    entity, _, entity_id = target.partition("/")
    db.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, meta) VALUES (%s, %s, %s, %s, %s::jsonb)",
        (actor_id, action, entity, entity_id, json.dumps(detail, ensure_ascii=False)),
    )


def _bump_version(db: Any) -> int:
    row = db.one(
        """INSERT INTO app_settings (key, value, updated_at) VALUES ('pool_version', '1'::jsonb, now())
           ON CONFLICT (key) DO UPDATE SET value = to_jsonb(coalesce((app_settings.value #>> '{}')::int, 1) + 1),
                                            updated_at = now()
           RETURNING (value #>> '{}')::int AS version"""
    )
    return int((row or {}).get("version") or 1)


def apply_config(db: Any, cfg: dict, *, actor_id: str | None = None, dry_run: bool = False) -> dict:
    """Áp PoolConfig vào CSDL. Trả `{valid, applied, errors, diff, version}` (khớp `PoolImportResult`)."""
    cfg, dropped = sanitize_config(cfg)
    report = validate_config(cfg)
    if dropped:
        report = {
            **report,
            "warnings": [*report["warnings"], f"bỏ qua trường chỉ để hiển thị: {', '.join(dropped[:8])}"],
        }
    current = load_config(db)
    diff = config_diff(current, cfg)
    if not report["valid"]:
        return {
            "valid": False,
            "applied": False,
            "errors": report["errors"],
            "warnings": report["warnings"],
            "diff": diff,
            "version": current["version"],
        }
    if dry_run:
        return {
            "valid": True,
            "applied": False,
            "errors": [],
            "warnings": report["warnings"],
            "diff": diff,
            "version": current["version"],
        }

    kept = _kept_secrets(db)
    try:
        with db.tx() as cur:
            for table in ("llm_providers", "llm_models", "llm_quota_groups", "llm_deployments"):
                cur.execute(f"DELETE FROM {table}")  # CASCADE dọn model/group/deployment con
            cur.execute("DELETE FROM llm_profile_tiers")
            cur.execute("DELETE FROM llm_profiles")
            _insert_all(cur, cfg, kept_secrets=kept)
    except ValueError as exc:  # cấu hình đọc được nhưng không khôi phục được khoá → KHÔNG ghi gì
        return {
            "valid": False,
            "applied": False,
            "errors": [str(exc)],
            "warnings": report["warnings"],
            "diff": diff,
            "version": current["version"],
        }
    version = _bump_version(db)
    _audit(db, actor_id, "pool_config_import", "pool/config", {"diff": diff, "version": version})
    return {
        "valid": True,
        "applied": True,
        "errors": [],
        "warnings": report["warnings"],
        "diff": diff,
        "version": version,
    }


def _insert_all(
    cur: Any,
    cfg: dict,
    *,
    kept_secrets: dict[str, dict] | None = None,
    new_secrets: dict[str, str] | None = None,
    master_key: bytes | None = None,
) -> None:
    """Ghi toàn bộ PoolConfig. `kept_secrets` giữ bí mật đã có (bản xuất `enc:<id>`), `new_secrets` là
    khoá thật vừa nhận từ khai báo — cả hai đường đều đi qua câu INSERT duy nhất nên ràng buộc
    `llm_credentials` (đúng một trong `secret_ref`/`secret_enc`) luôn đúng tại mọi thời điểm."""
    kept_secrets = kept_secrets or {}
    for provider in cfg.get("providers") or []:
        cur.execute(
            """INSERT INTO llm_providers (id, kind, base_url, display_name, quirks, enabled)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                provider["id"],
                provider["kind"],
                provider["base_url"],
                provider["display_name"],
                json.dumps(provider.get("quirks") or {}),
                bool(provider.get("enabled", True)),
            ),
        )
    for model in cfg.get("models") or []:
        cur.execute(
            """INSERT INTO llm_models (id, provider_id, model_id, ctx_in, max_out, structured, vision, pdf,
                    tokenizer_factor, price_key, shadow_price_key, quality, adapter_options)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                model["id"],
                model["provider"],
                model["model_id"],
                model["ctx_in"],
                model["max_out"],
                model.get("structured", "none"),
                bool(model.get("vision")),
                bool(model.get("pdf")),
                float(model.get("tokenizer_factor", 1.0)),
                model.get("price_key"),
                model.get("shadow_price_key"),
                json.dumps(model.get("quality") or {}),
                json.dumps(model.get("adapter_options") or {}),
            ),
        )
    for group in cfg.get("groups") or []:
        limits = group.get("limits") or {}
        cur.execute(
            """INSERT INTO llm_quota_groups (id, provider_id, label, tier, data_policy, reset_tz, tos_flags,
                    allowed_gates, safety_margin, day_margin, rpm, tpm, rpd, tpd, concurrency, enabled, notes,
                    risk_ack_flags, risk_ack_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       CASE WHEN cardinality(%s::text[]) > 0 THEN now() ELSE NULL END)""",
            (
                group["id"],
                group["provider"],
                group["label"],
                group["tier"],
                group.get("data_policy", "unknown"),
                group.get("reset_tz", "UTC"),
                list(group.get("tos_flags") or []),
                list(group.get("allowed_gates") or ["dev", "A", "B", "C"]),
                float(group.get("safety_margin", 0.85)),
                float(group.get("day_margin", 0.95)),
                limits.get("rpm"),
                limits.get("tpm"),
                limits.get("rpd"),
                limits.get("tpd"),
                limits.get("concurrency"),
                bool(group.get("enabled", True)),
                group.get("notes"),
                list(group.get("risk_ack") or []),
                list(group.get("risk_ack") or []),
            ),
        )
        for cred in group.get("credentials") or []:
            ref = cred.get("secret_ref")
            secret = (new_secrets or {}).get(cred["id"])
            if secret is not None:
                cur.execute(
                    """INSERT INTO llm_credentials (id, group_id, label, secret_enc, last4, fingerprint, status)
                       VALUES (%s, %s, %s, %s, %s, %s, 'active')""",
                    (
                        cred["id"],
                        group["id"],
                        cred["label"],
                        pool_secrets.encrypt_secret(master_key, cred["id"], secret),
                        pool_secrets.last4(secret),
                        pool_secrets.fingerprint(master_key, secret),
                    ),
                )
            elif ref and ref.startswith("enc:"):
                # Bản xuất đã che khoá (`enc:<id>`): giữ nguyên bí mật đang có trong CSDL. Nếu id này
                # chưa từng tồn tại thì không có gì để giữ — báo lỗi thay vì tạo khoá rỗng.
                kept = kept_secrets.get(cred["id"])
                if kept is None:
                    raise ValueError(
                        f"khoá {cred['id']}: cấu hình chỉ có bản che `{ref}` nhưng CSDL chưa có khoá này — "
                        "hãy thêm khoá thật qua /admin/pool/declare hoặc /admin/pool/groups/{id}/credentials"
                    )
                cur.execute(
                    """INSERT INTO llm_credentials (id, group_id, label, secret_enc, last4, fingerprint, status)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        cred["id"],
                        group["id"],
                        cred["label"],
                        kept["secret_enc"],
                        kept["last4"],
                        kept["fingerprint"],
                        kept["status"],
                    ),
                )
            elif ref:
                cur.execute(
                    """INSERT INTO llm_credentials (id, group_id, label, secret_ref, status)
                       VALUES (%s, %s, %s, %s, 'active')""",
                    (cred["id"], group["id"], cred["label"], ref),
                )
            else:  # schema bắt buộc `secret_ref`, tới đây là dữ liệu đã hỏng
                raise ValueError(f"khoá {cred['id']}: thiếu `secret_ref`")
    for dep in cfg.get("deployments") or []:
        limits = dep.get("limits") or {}
        cur.execute(
            """INSERT INTO llm_deployments (id, group_id, model_id, rpm, tpm, rpd, tpd, concurrency, tpm_basis,
                    price_mode, weight, tags, enabled)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                dep["id"],
                dep["group"],
                dep["model"],
                limits.get("rpm"),
                limits.get("tpm"),
                limits.get("rpd"),
                limits.get("tpd"),
                int(limits.get("concurrency") or 4),
                dep.get("tpm_basis", "total"),
                dep.get("price_mode", "free"),
                float(dep.get("weight", 1.0)),
                list(dep.get("tags") or []),
                bool(dep.get("enabled", True)),
            ),
        )
    for profile in cfg.get("profiles") or []:
        cur.execute(
            "INSERT INTO llm_profiles (name, needs, version) VALUES (%s, %s, %s)",
            (profile["name"], json.dumps(profile.get("needs") or {}), int(profile.get("version", 1))),
        )
        for index, tier in enumerate(profile.get("tiers") or [], 1):
            select = tier.get("select") or {}
            cur.execute(
                """INSERT INTO llm_profile_tiers (profile_name, tier_no, name, select_tags, select_group_tiers,
                        strategy, max_wait_s)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    profile["name"],
                    int(tier.get("tier_no") or index),
                    tier["name"],
                    list(select.get("tags") or []),
                    list(select.get("group_tiers") or []),
                    tier.get("strategy", "headroom"),
                    tier.get("max_wait_s"),
                ),
            )
    for key, setting in POLICY_KEYS.items():
        if key in (cfg.get("policy") or {}):
            cur.execute(
                """INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s::jsonb, now())
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                (setting, json.dumps(cfg["policy"][key])),
            )


# --------------------------------------------------------------------------- khai báo & khoá


def _kept_secrets(db: Any) -> dict[str, dict]:
    """Bí mật đã mã hoá đang có trong CSDL, theo `id` — dùng để khôi phục khi nhập lại bản đã che khoá."""
    rows = db.all("SELECT id, secret_enc, last4, fingerprint, status FROM llm_credentials WHERE secret_enc IS NOT NULL")
    return {row["id"]: dict(row) for row in rows}


def _existing_fingerprints(db: Any) -> frozenset[str]:
    rows = db.all("SELECT fingerprint FROM llm_credentials WHERE fingerprint IS NOT NULL")
    return frozenset(row["fingerprint"] for row in rows)


def declare(db: Any, doc: dict, master_key: bytes, *, actor_id: str | None = None, dry_run: bool = True) -> dict:
    """Biên dịch một tài liệu khai báo rồi (tuỳ chọn) ghi vào CSDL trong MỘT giao dịch.

    Khoá được mã hoá ngay khi ghi (`secret_enc`); phản hồi chỉ có 4 ký tự cuối và số khoá đã bỏ.
    """
    cfg = load_config(db)
    result = compile_all(
        doc,
        cfg,
        fingerprint_fn=lambda secret: pool_secrets.fingerprint(master_key, secret),
        existing_fingerprints=_existing_fingerprints(db),
    )
    fragment = result.fragment
    preview = {
        "counts": dict(result.preview.get("counts") or {}),
        "groups": result.preview.get("groups") or [],
        "skipped_keys": result.preview.get("skipped_keys") or [],
        "warnings": result.warnings,
    }
    out = {
        "valid": result.valid,
        "applied": False,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "preview": preview,
        "version": cfg["version"],
    }
    if not result.valid:
        return out
    merged = merge_fragment(cfg, fragment)
    if dry_run:
        return out

    kept = _kept_secrets(db)
    with db.tx() as cur:
        cur.execute("DELETE FROM llm_providers")
        cur.execute("DELETE FROM llm_profile_tiers")
        cur.execute("DELETE FROM llm_profiles")
        _insert_all(
            cur,
            {**merged, "profiles": cfg.get("profiles") or merged.get("profiles") or []},
            kept_secrets=kept,
            new_secrets=dict(result.secrets),
            master_key=master_key,
        )
    out["applied"] = True
    out["version"] = _bump_version(db)
    _audit(db, actor_id, "pool_declare", "pool/declare", {"counts": preview["counts"], "version": out["version"]})
    return out


def add_credential(db: Any, group_id: str, label: str, secret: str, master_key: bytes) -> dict:
    """Thêm một khoá vào nhóm: mã hoá ngay, chống trùng bằng dấu vân tay."""
    from visynth_api.errors import Problem  # vòng import: routers dùng chung module này

    credential_id = _next_credential_id(db, group_id)
    fingerprint = pool_secrets.fingerprint(master_key, secret)
    duplicate = db.one("SELECT id FROM llm_credentials WHERE fingerprint = %s", (fingerprint,))
    if duplicate:
        raise Problem(409, "conflict", f"khoá này đã có trong pool (credential {duplicate['id']})")
    db.execute(
        """INSERT INTO llm_credentials (id, group_id, label, secret_enc, last4, fingerprint, status)
           VALUES (%s, %s, %s, %s, %s, %s, 'active')""",
        (
            credential_id,
            group_id,
            label,
            pool_secrets.encrypt_secret(master_key, credential_id, secret),
            pool_secrets.last4(secret),
            fingerprint,
        ),
    )
    _audit(db, None, "pool_credential_add", f"credentials/{credential_id}", {"group": group_id, "label": label})
    return get_credential(db, credential_id)


def _next_credential_id(db: Any, group_id: str) -> str:
    rows = db.all("SELECT id FROM llm_credentials WHERE group_id = %s", (group_id,))
    used = {row["id"] for row in rows}
    for index in range(1, 1000):
        candidate = f"{group_id}-k{index}"
        if candidate not in used:
            return candidate
    raise RuntimeError("nhóm đã có quá nhiều khoá")


def get_credential(db: Any, credential_id: str) -> dict | None:
    return db.one(
        "SELECT id, group_id, label, last4, status, quarantined_reason, last_used_at "
        "FROM llm_credentials WHERE id = %s",
        (credential_id,),
    )


def list_credentials(db: Any, group_id: str) -> list[dict]:
    return db.all(
        "SELECT id, group_id, label, last4, status, quarantined_reason, last_used_at "
        "FROM llm_credentials WHERE group_id = %s ORDER BY id",
        (group_id,),
    )


def set_credential_status(db: Any, credential_id: str, status: str, *, actor_id: str | None = None) -> dict | None:
    reason = None if status == "active" else "admin_disabled"
    db.execute(
        "UPDATE llm_credentials SET status = %s, quarantined_reason = %s WHERE id = %s",
        (status, reason, credential_id),
    )
    _audit(db, actor_id, "pool_credential_status", f"credentials/{credential_id}", {"status": status})
    return get_credential(db, credential_id)


def delete_credential(db: Any, credential_id: str, *, actor_id: str | None = None) -> bool:
    removed = db.execute("DELETE FROM llm_credentials WHERE id = %s", (credential_id,))
    if removed:
        _audit(db, actor_id, "pool_credential_delete", f"credentials/{credential_id}", {})
    return bool(removed)


def resolve_secret(db: Any, credential_id: str, master_key: bytes | None, environ: dict | None = None) -> str:
    """Lấy khoá thật để gọi nhà cung cấp: giải mã `secret_enc`, hoặc đọc `env:` / `file:`."""
    import os

    row = db.one("SELECT secret_ref, secret_enc FROM llm_credentials WHERE id = %s", (credential_id,))
    if row is None:
        raise KeyError(f"không có credential {credential_id}")
    if row["secret_enc"] is not None:
        if master_key is None:
            raise ValueError("thiếu POOL_MASTER_KEY để giải mã khoá")
        return pool_secrets.decrypt_secret(master_key, credential_id, bytes(row["secret_enc"]))
    ref = row["secret_ref"] or ""
    env = environ if environ is not None else os.environ
    if ref.startswith("env:"):
        return env.get(ref[4:], "")
    if ref.startswith("file:"):
        with open(ref[5:], encoding="utf-8") as handle:
            return handle.read().strip()
    raise ValueError(f"không đọc được tham chiếu khoá {ref!r}")


# --------------------------------------------------------------------------- vá cấu hình


def patch_group(db: Any, group_id: str, patch: dict, *, actor_id: str | None = None) -> dict | None:
    from visynth_api.errors import Problem

    group = db.one("SELECT * FROM llm_quota_groups WHERE id = %s", (group_id,))
    if group is None:
        return None
    fields: dict[str, Any] = {}
    for key in ("enabled", "data_policy", "safety_margin", "day_margin"):
        if key in patch:
            fields[key] = patch[key]
    for key, column in (("tos_flags", "tos_flags"), ("allowed_gates", "allowed_gates"), ("risk_ack", "risk_ack_flags")):
        if key in patch:
            fields[column] = list(patch[key])
    limits = patch.get("limits") or {}
    for key in _GROUP_LIMIT_COLUMNS:
        if key in limits:
            fields[key] = limits[key]

    enabled = fields.get("enabled", group["enabled"])
    tos = set(fields.get("tos_flags", group["tos_flags"] or []))
    ack = set(fields.get("risk_ack_flags", group["risk_ack_flags"] or []))
    if enabled:
        missing = sorted((tos & {"multi_account_risk", "trial_only"}) - ack)
        if missing:
            raise Problem(
                409,
                "risk_ack_required",
                f"nhóm có cờ rủi ro {missing} nhưng chưa được xác nhận — thêm vào `risk_ack` rồi bật lại",
            )
    if fields:
        columns = ", ".join(f"{name} = %s" for name in fields)
        db.execute(f"UPDATE llm_quota_groups SET {columns} WHERE id = %s", (*fields.values(), group_id))
    if "risk_ack_flags" in fields:
        db.execute(
            "UPDATE llm_quota_groups SET risk_ack_at = now(), risk_ack_by = %s WHERE id = %s",
            (actor_id, group_id),
        )
    _audit(db, actor_id, "pool_group_patch", f"groups/{group_id}", {"fields": sorted(fields)})
    return group_payload(db, group_id)


def patch_deployment(db: Any, deployment_id: str, patch: dict, *, actor_id: str | None = None) -> dict | None:
    row = db.one("SELECT id FROM llm_deployments WHERE id = %s", (deployment_id,))
    if row is None:
        return None
    fields: dict[str, Any] = {}
    if "enabled" in patch:
        fields["enabled"] = patch["enabled"]
    if "weight" in patch:
        fields["weight"] = patch["weight"]
    if "tags" in patch:
        fields["tags"] = list(patch["tags"])
    for key in _DEPLOY_LIMIT_COLUMNS:
        if key in (patch.get("limits") or {}):
            fields[key] = patch["limits"][key]
    if fields:
        columns = ", ".join(f"{name} = %s" for name in fields)
        db.execute(f"UPDATE llm_deployments SET {columns} WHERE id = %s", (*fields.values(), deployment_id))
    _audit(db, actor_id, "pool_deployment_patch", f"deployments/{deployment_id}", {"fields": sorted(fields)})
    return deployment_payload(db, deployment_id)


# --------------------------------------------------------------------------- trạng thái & dung lượng


def group_payload(db: Any, group_id: str) -> dict | None:
    row = db.one("SELECT * FROM llm_quota_groups WHERE id = %s", (group_id,))
    if row is None:
        return None
    return {
        "id": row["id"],
        "provider": row["provider_id"],
        "label": row["label"],
        "tier": row["tier"],
        "data_policy": row["data_policy"],
        "tos_flags": list(row["tos_flags"] or []),
        "allowed_gates": list(row["allowed_gates"] or []),
        "safety_margin": row["safety_margin"],
        "day_margin": row["day_margin"],
        "limits": _limits(row, _GROUP_LIMIT_COLUMNS),
        "enabled": row["enabled"],
        "risk_ack": list(row["risk_ack_flags"] or []),
        "credentials": list_credentials(db, group_id),
    }


def deployment_payload(db: Any, deployment_id: str) -> dict | None:
    row = db.one("SELECT * FROM llm_deployments WHERE id = %s", (deployment_id,))
    if row is None:
        return None
    group = db.one(
        "SELECT tier, data_policy, safety_margin, day_margin FROM llm_quota_groups WHERE id = %s", (row["group_id"],)
    )
    scope = db.one("SELECT * FROM llm_scope_state WHERE scope = 'deployment' AND scope_id = %s", (deployment_id,))
    group_state = db.one("SELECT * FROM llm_scope_state WHERE scope = 'group' AND scope_id = %s", (row["group_id"],))
    active_credentials = _count(
        db, "SELECT count(*) AS n FROM llm_credentials WHERE group_id = %s AND status = 'active'", (row["group_id"],)
    )
    return {
        "id": row["id"],
        "group_id": row["group_id"],
        "model": row["model_id"],
        "tier": group["tier"] if group else "free",
        "data_policy": group["data_policy"] if group else "unknown",
        "enabled": row["enabled"],
        "circuit": (scope or {}).get("circuit", "closed"),
        "cooldown_until": _epoch_to_iso((scope or {}).get("cooldown_until")),
        "headroom": headroom(row, group, scope, group_state),
        "inflight": int((scope or {}).get("inflight") or 0),
        "rpd_used": int((scope or {}).get("rpd_used") or 0),
        "rpd_limit": row["rpd"] if row["rpd"] is not None else (group or {}).get("rpd"),
        "ewma_success": float((scope or {}).get("ewma_success") or 1.0),
        "ewma_latency_ms": (scope or {}).get("ewma_latency_ms"),
        "active_credentials": int(active_credentials or 0),
        "limits": {
            **_limits(row, _DEPLOY_LIMIT_COLUMNS),
            "tpm_basis": row["tpm_basis"],
            "price_mode": row["price_mode"],
            "weight": row["weight"],
            "tags": list(row["tags"] or []),
        },
    }


def _count(db: Any, sql: str, params: tuple) -> int:
    row = db.one(sql, params)
    return int(next(iter(row.values())) or 0) if row else 0


def headroom(dep: dict, group: dict | None, scope: dict | None, group_state: dict | None) -> float:
    """Phần hạn mức NGÀY còn lại (0..1) — thô hơn bộ định tuyến nhưng đủ để Admin nhìn."""
    used = int((scope or {}).get("rpd_used") or (group_state or {}).get("rpd_used") or 0)
    limits = [dep.get("rpd"), (group or {}).get("rpd")]
    known = [value for value in limits if value]
    if not known:
        return 1.0
    limit = min(known)
    return round(max(0.0, 1 - used / limit), 4)


def _epoch_to_iso(value: float | None) -> str | None:
    if not value:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(float(value), tz=UTC).isoformat()


def pool_status(db: Any) -> list[dict]:
    rows = db.all("SELECT id FROM llm_deployments ORDER BY id")
    return [payload for payload in (deployment_payload(db, row["id"]) for row in rows) if payload]


def pool_capacity(db: Any, privacy_class: str = "standard") -> dict:
    """Dự báo dung lượng: dùng hạn mức đã cấu hình + `estimate_calls`/`docs_per_day` của bộ ước tính."""
    from visynth.estimate import docs_per_day, estimate_calls

    if privacy_class == "private":
        eligible = db.all(
            """SELECT d.id, d.rpd, g.rpd AS group_rpd, d.tpd, g.tpd AS group_tpd
                 FROM llm_deployments d JOIN llm_quota_groups g ON g.id = d.group_id
                WHERE d.enabled AND g.enabled AND g.data_policy = 'no_training'
                  AND NOT ('no_personal_data' = ANY (g.tos_flags))"""
        )
    else:
        eligible = db.all(
            """SELECT d.id, d.rpd, g.rpd AS group_rpd, d.tpd, g.tpd AS group_tpd
                 FROM llm_deployments d JOIN llm_quota_groups g ON g.id = d.group_id
                WHERE d.enabled AND g.enabled"""
        )

    def total(column: str, group_column: str) -> int | None:
        values = [row[column] if row[column] is not None else row[group_column] for row in eligible]
        if not values or any(value is None for value in values):
            return None
        return int(sum(values))

    calls_cap = total("rpd", "group_rpd")
    tokens_cap = total("tpd", "group_tpd")
    rows = []
    for level in ("detailed_synthesis", "deep_synthesis", "executive_brief", "full_translation"):
        for words in (2000, 8000, 30000):
            calls = estimate_calls(words, level)
            tokens_in = int(words * 1.4)
            per_day = docs_per_day(calls_cap, tokens_cap, calls, tokens_in)
            rows.append(
                {
                    "level": level,
                    "words": words,
                    "docs": None if per_day == float("inf") else round(per_day, 1),
                }
            )
    return {
        "privacy_class": privacy_class,
        "eligible_deployments": len(eligible),
        "calls_per_day": calls_cap,
        "tokens_in_per_day": tokens_cap,
        "docs_per_day": rows,
    }


def pool_incidents(
    db: Any, *, cursor: str | None = None, limit: int = 20, deployment_id: str | None = None
) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    before = int(cursor) if cursor and str(cursor).isdigit() else None
    return db.all(
        """SELECT id, at, deployment_id, credential_id, kind, detail FROM llm_incidents
            WHERE (%s::text IS NULL OR deployment_id = %s::text)
              AND (%s::bigint IS NULL OR id < %s::bigint)
            ORDER BY id DESC LIMIT %s""",
        (deployment_id, deployment_id, before, before, limit),
    )


def record_probe(db: Any, deployment_id: str, results: dict, *, passed: bool, note: str | None = None) -> dict:
    row = db.one(
        """INSERT INTO llm_probe_runs (deployment_id, passed, results, note)
           VALUES (%s, %s, %s, %s) RETURNING id, deployment_id, ran_at, passed, results, note""",
        (deployment_id, passed, json.dumps(results, ensure_ascii=False), note),
    )
    return row


#: Ánh xạ `Outcome.kind` (§17.7) sang từ vựng `llm_calls.status`.
CALL_STATUS = {
    "ok": "ok",
    "deferred": "blocked",
    "impossible": "blocked",
    "privacy_blocked": "blocked",
    "gate_blocked": "blocked",
    "auth_error": "fatal_error",
    "bad_request": "fatal_error",
    "context_exceeded": "fatal_error",
    "safety_blocked": "fatal_error",
    "rate_limited_minute": "retryable_error",
    "rate_limited_day": "retryable_error",
    "rate_limited_unknown": "retryable_error",
    "server_error": "retryable_error",
    "timeout": "retryable_error",
    "network_error": "retryable_error",
    "truncated": "retryable_error",
}


class DbLedger:
    """Sổ `llm_calls` (§17.8) — bản thay cho `Ledger` ghi tệp khi chạy trên VPS.

    Bất biến quan trọng nhất: **KHÔNG ghi nội dung** prompt/phản hồi vào CSDL. Hàng của pool có trường
    `detail` (tới 200 ký tự của phản hồi) — nó bị bỏ ở đây; chỉ giữ số đo, khoá ngoại và `outcome`.
    """

    #: Cột của `llm_calls` mà sổ này ghi (mọi cột khác dùng giá trị mặc định của CSDL).
    COLUMNS = (
        "job_id",
        "task_id",
        "stage",
        "prompt_id",
        "prompt_version",
        "model",
        "tokens_in",
        "tokens_cached",
        "tokens_out",
        "tokens_thinking",
        "latency_ms",
        "status",
        "error_code",
        "cost_usd",
        "shadow_cost_usd",
        "deployment_id",
        "credential_id",
        "group_tier",
        "data_policy",
        "privacy_class",
        "outcome",
        "pool_wait_ms",
        "diversity_degraded",
    )

    def __init__(
        self, db: Any, *, user_id: str | None = None, stage: str | None = None, privacy_class: str = "standard"
    ):
        self.db = db
        self.user_id = user_id
        self.stage = stage
        self.privacy_class = privacy_class
        self.written = 0

    def append(self, row: dict) -> None:
        """Ghi MỘT dòng sổ. Lỗi ghi sổ không được làm hỏng job đang chạy (chỉ đếm là mất)."""
        kind = str(row.get("outcome") or "")
        values: dict[str, Any] = {
            "job_id": row.get("job_id"),
            "task_id": row.get("task_id"),
            "stage": self.stage,
            "prompt_id": row.get("prompt_id"),
            "prompt_version": row.get("prompt_version"),
            "model": row.get("model") or "unknown",
            "tokens_in": int(row.get("tokens_in") or 0),
            "tokens_cached": int(row.get("tokens_cached") or 0),
            "tokens_out": int(row.get("tokens_out") or 0),
            "tokens_thinking": int(row.get("tokens_thinking") or 0),
            "latency_ms": row.get("latency_ms"),
            "status": CALL_STATUS.get(kind, "fatal_error" if kind in ("auth_error",) else "retryable_error"),
            "error_code": (row.get("error_code") or (kind if kind and kind != "ok" else None)),
            "cost_usd": float(row.get("cost_usd") or 0),
            "shadow_cost_usd": float(row.get("shadow_cost_usd") or 0),
            "deployment_id": row.get("deployment_id"),
            "credential_id": row.get("credential_id"),
            "group_tier": row.get("group_tier"),
            "data_policy": row.get("data_policy"),
            "privacy_class": self.privacy_class,
            "outcome": kind or None,
            "pool_wait_ms": int(row.get("pool_wait_ms") or 0),
            "diversity_degraded": bool(row.get("diversity_degraded")),
        }
        if self.user_id:
            values["user_id"] = self.user_id
        columns = [*self.COLUMNS, *(["user_id"] if self.user_id else [])]
        placeholders = ", ".join(["%s"] * len(columns))
        self.db.execute(
            f"INSERT INTO llm_calls ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(values[column] for column in columns),
        )
        self.written += 1

    def totals(self) -> dict:
        """Tổng hợp nhanh từ chính CSDL (dùng cho báo cáo/kiểm toán, không giữ trong RAM)."""
        row = self.db.one(
            """SELECT count(*) AS calls,
                      count(*) FILTER (WHERE status = 'ok') AS calls_ok,
                      coalesce(sum(tokens_in), 0)::bigint AS tokens_in,
                      coalesce(sum(tokens_out), 0)::bigint AS tokens_out,
                      coalesce(sum(cost_usd), 0)::numeric AS cost_usd,
                      coalesce(sum(shadow_cost_usd), 0)::numeric AS shadow_cost_usd
                 FROM llm_calls"""
        )
        return {key: (float(value) if key.endswith("_usd") else int(value)) for key, value in (row or {}).items()}


def sync_model_quality(db: Any, deployment_id: str, results: dict) -> None:
    """Cập nhật `llm_models.quality` từ kết quả kiểm định (chỉ các chỉ số có đo)."""
    keys = {"json": "json", "vi_write": "vi_write", "long_context": "long_context", "mt_en_vi": "mt_en_vi"}
    measured = {keys[key]: float(results[key]) for key in keys if isinstance(results.get(key), (int, float))}
    if not measured:
        return
    db.execute(
        """UPDATE llm_models SET quality = quality || %s::jsonb
            WHERE id = (SELECT model_id FROM llm_deployments WHERE id = %s)""",
        (json.dumps(measured), deployment_id),
    )


def probe_template() -> dict:
    """Kết quả kiểm định rỗng để ghi khi chưa chạy được (không có mạng/khoá)."""
    return {"json": None, "vi_write": None, "long_context": None, "mt_en_vi": None, "note": "chưa chạy"}


class DbRegistry(Registry):
    """Kho khoá lấy từ CSDL (`llm_credentials.secret_enc`) — dùng cho `probe` và cho worker.

    Thừa hưởng cách phân giải `env:TÊN` của `Registry`; `enc:<id>` đọc và giải mã từ CSDL.
    """

    def __init__(self, db: Any, master: bytes) -> None:
        super().__init__()
        self._db = db
        self._master = master

    def master(self) -> bytes:  # type: ignore[override]
        return self._master

    def get(self, credential_id: str) -> str:  # type: ignore[override]
        return resolve_secret(self._db, credential_id, self._master)

    def has(self, credential_id: str) -> bool:  # type: ignore[override]
        return self._db.one("SELECT 1 AS ok FROM llm_credentials WHERE id = %s", (credential_id,)) is not None


def declare_result_to_json(result: DeclareResult) -> dict:  # pragma: no cover - tiện dụng cho CLI
    return {
        "valid": result.valid,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "preview": result.preview,
    }
