"""Cầu nối PostgreSQL cho LLM Pool: PgState (cùng giao diện MemoryState nhưng gọi hàm SQL nguyên tử) và seed_pool()
nạp PoolConfig (schemas/pool_config.schema.json) vào các bảng llm_*. Cần psycopg 3; import muộn để các module khác không phụ thuộc.
Dùng trong tests/pg_smoke.py để chứng minh SQL và MemoryState có CÙNG ngữ nghĩa (so khớp từng bước trên kịch bản ngẫu nhiên).
"""
from __future__ import annotations

from .llm_pool import Deployment, Outcome, ReserveResult


class PgState:
    def __init__(self, conn):
        self.conn = conn  # psycopg.Connection, autocommit=True

    def try_reserve(self, dep: Deployment, tin: int, tout: int, priority: str, now: float, reserve: float = 0.2, ttl: float = 300.0) -> ReserveResult:
        r = self.conn.execute(
            "SELECT ok, lease_id, credential_id, wait_s, reason, basis_tokens FROM pool_try_reserve(%s, %s, %s, %s, %s, %s, %s)",
            (dep.id, tin, tout, priority, now, reserve, ttl),
        ).fetchone()
        return ReserveResult(r[0], r[1], r[2], r[3], r[4], r[5])

    def settle(self, lease_id: int, o: Outcome, now: float) -> None:
        self.conn.execute(
            "SELECT pool_settle(%s, %s, %s, %s, %s, %s, %s, %s)",
            (lease_id, o.kind, o.tokens_in, o.tokens_out, o.latency_ms, o.retry_after_s, o.scope, now),
        )

    def reap(self, now: float) -> int:
        return self.conn.execute("SELECT pool_reap_leases(%s)", (now,)).fetchone()[0]

    def snapshot(self, dep: Deployment, now: float) -> dict:
        r = self.conn.execute("SELECT headroom, ewma_success, inflight, cooldown_until, circuit, last_used_at FROM pool_snapshot(%s, %s)", (dep.id, now)).fetchone()
        return dict(headroom=r[0], ewma_success=r[1], inflight=r[2], cooldown_until=r[3], circuit=r[4], last_used_at=r[5])


def _lim(d: dict | None) -> tuple:
    d = d or {}
    return d.get("rpm"), d.get("tpm"), d.get("rpd"), d.get("tpd"), d.get("concurrency")


def seed_pool(conn, cfg: dict) -> None:
    """Nhập PoolConfig vào CSDL (tham chiếu cho endpoint POST /admin/pool/import). Khoá chỉ nhận dạng tham chiếu env:/file:."""
    import json

    for p in cfg["providers"]:
        conn.execute("INSERT INTO llm_providers (id, kind, base_url, display_name, quirks) VALUES (%s,%s,%s,%s,%s)",
                     (p["id"], p["kind"], p["base_url"], p["display_name"], json.dumps(p["quirks"])))
    for m in cfg["models"]:
        conn.execute(
            """INSERT INTO llm_models (id, provider_id, model_id, ctx_in, max_out, structured, vision, pdf, tokenizer_factor,
                                       price_key, shadow_price_key, quality, adapter_options)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (m["id"], m["provider"], m["model_id"], m["ctx_in"], m["max_out"], m["structured"], m["vision"], m["pdf"],
             m["tokenizer_factor"], m["price_key"], m["shadow_price_key"], json.dumps(m["quality"]), json.dumps(m["adapter_options"])))
    for g in cfg["groups"]:
        rpm, tpm, rpd, tpd, conc = _lim(g["limits"])
        conn.execute(
            """INSERT INTO llm_quota_groups (id, provider_id, label, tier, data_policy, reset_tz, tos_flags, allowed_gates, safety_margin,
                                             day_margin, rpm, tpm, rpd, tpd, concurrency, enabled, risk_ack_flags, risk_ack_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN cardinality(%s::text[]) > 0 THEN now() END)""",
            (g["id"], g["provider"], g["label"], g["tier"], g["data_policy"], g["reset_tz"], g["tos_flags"], g["allowed_gates"],
             g["safety_margin"], g["day_margin"], rpm, tpm, rpd, tpd, conc, g["enabled"], g["risk_ack"], g["risk_ack"]))
        for c in g["credentials"]:
            if not c["secret_ref"].startswith(("env:", "file:")):
                continue  # enc:<id> được giải quyết ở tầng ứng dụng (secret_enc)
            conn.execute("INSERT INTO llm_credentials (id, group_id, label, secret_ref) VALUES (%s,%s,%s,%s)", (c["id"], g["id"], c["label"], c["secret_ref"]))
    for d in cfg["deployments"]:
        rpm, tpm, rpd, tpd, conc = _lim(d["limits"])
        conn.execute(
            """INSERT INTO llm_deployments (id, group_id, model_id, rpm, tpm, rpd, tpd, concurrency, tpm_basis, price_mode, weight, tags, enabled)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (d["id"], d["group"], d["model"], rpm, tpm, rpd, tpd, conc or 4, d["tpm_basis"], d["price_mode"], d["weight"], d["tags"], d["enabled"]))
    for pr in cfg["profiles"]:
        conn.execute("INSERT INTO llm_profiles (name, needs) VALUES (%s,%s)", (pr["name"], json.dumps(pr["needs"])))
        for i, t in enumerate(pr["tiers"], 1):
            conn.execute(
                "INSERT INTO llm_profile_tiers (profile_name, tier_no, name, select_tags, select_group_tiers, strategy, max_wait_s) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (pr["name"], i, t["name"], t["select"]["tags"], t["select"]["group_tiers"], t["strategy"], t["max_wait_s"]))


def existing_fingerprints(conn) -> set:
    """Dấu vân tay của mọi khoá đã có: dùng để phát hiện khoá trùng khi khai báo mà không giải mã khoá nào."""
    return {r[0] for r in conn.execute("SELECT fingerprint FROM llm_credentials WHERE fingerprint IS NOT NULL")}


def apply_declaration(conn, result, master: bytes, by_user=None) -> dict:
    """Ghi kết quả của compile_declaration/compile_all vào CSDL TRONG MỘT GIAO DỊCH (lỗi giữa chừng thì không ghi gì).

    Khoá thật chỉ đi qua hàm này một lần: mã hoá AES-256-GCM (AAD = id khoá), lưu dấu vân tay và 4 ký tự cuối, rồi bỏ.
    Nhà cung cấp và model trùng id được giữ nguyên; nhóm đã có chỉ nhận thêm khoá mới."""
    import json

    from .pool_secrets import encrypt_secret, fingerprint, last4

    if not result.valid:
        raise ValueError("khai báo không hợp lệ: " + "; ".join(result.errors))
    frag, counts = result.fragment, dict(providers=0, models=0, groups=0, credentials=0, deployments=0)
    with conn.transaction():
        for p in frag["providers"]:
            r = conn.execute("INSERT INTO llm_providers (id, kind, base_url, display_name, quirks) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                             (p["id"], p["kind"], p["base_url"], p["display_name"], json.dumps(p["quirks"])))
            counts["providers"] += r.rowcount
        for m in frag["models"]:
            r = conn.execute(
                """INSERT INTO llm_models (id, provider_id, model_id, ctx_in, max_out, structured, vision, pdf, tokenizer_factor, price_key,
                                           shadow_price_key, quality, adapter_options)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                (m["id"], m["provider"], m["model_id"], m["ctx_in"], m["max_out"], m["structured"], m["vision"], m["pdf"], m["tokenizer_factor"],
                 m["price_key"], m["shadow_price_key"], json.dumps(m["quality"]), json.dumps(m["adapter_options"])))
            counts["models"] += r.rowcount
        for g in frag["groups"]:
            rpm, tpm, rpd, tpd, conc = _lim(g["limits"])
            r = conn.execute(
                """INSERT INTO llm_quota_groups (id, provider_id, label, tier, data_policy, reset_tz, tos_flags, allowed_gates, safety_margin, day_margin,
                                                 rpm, tpm, rpd, tpd, concurrency, enabled, risk_ack_flags, risk_ack_at, risk_ack_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN cardinality(%s::text[]) > 0 THEN now() END,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (g["id"], g["provider"], g["label"], g["tier"], g["data_policy"], g["reset_tz"], g["tos_flags"], g["allowed_gates"], g["safety_margin"],
                 g["day_margin"], rpm, tpm, rpd, tpd, conc, g["enabled"], g["risk_ack"], g["risk_ack"], by_user))
            counts["groups"] += r.rowcount
            for c in g["credentials"]:
                secret = result.secrets[c["id"]]
                conn.execute("INSERT INTO llm_credentials (id, group_id, label, last4, fingerprint, secret_enc) VALUES (%s,%s,%s,%s,%s,%s)",
                             (c["id"], g["id"], c["label"], last4(secret), fingerprint(master, secret), encrypt_secret(master, c["id"], secret)))
                counts["credentials"] += 1
        for d in frag["deployments"]:
            rpm, tpm, rpd, tpd, conc = _lim(d["limits"])
            r = conn.execute(
                """INSERT INTO llm_deployments (id, group_id, model_id, rpm, tpm, rpd, tpd, concurrency, tpm_basis, price_mode, weight, tags, enabled)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                (d["id"], d["group"], d["model"], rpm, tpm, rpd, tpd, conc or 4, d["tpm_basis"], d["price_mode"], d["weight"], d["tags"], d["enabled"]))
            counts["deployments"] += r.rowcount
    return counts
