"""Khai báo nhà cung cấp và khoá cho LLM Pool: phương pháp nhập thông tin (SPEC §17.14). Thuần logic, không gọi mạng.

Người vận hành dán MỘT chuỗi khoá (cách nhau bằng dấu phẩy, chấm phẩy, xuống dòng hoặc khoảng trắng; có thể kèm nhãn `nhãn|khoá` để chỉ khoá
nào cùng một tài khoản/dự án) và khai thông tin chung của nhà cung cấp MỘT lần. Hàm compile_declaration() biến khai báo rút gọn đó thành các
phần của pool_config (nhà cung cấp, model, nhóm hạn mức, khoá dạng tham chiếu, deployment) và danh sách bí mật cần mã hoá. Mọi thứ trả về cho
client (preview, errors, warnings, fragment) KHÔNG chứa khoá thật; chỉ `secrets` chứa khoá và chỉ tồn tại trong bộ nhớ cho tới lúc mã hoá.
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from .llm_pool import PUBLIC_BLOCKING_FLAGS, RISK_FLAGS
from .pool_secrets import last4

CONSERVATIVE_LIMITS = {"rpm": 5, "tpm": None, "rpd": None, "tpd": None, "concurrency": 1}
REFERENCE_PRICE_KEY = "gemini-3.8-flash"  # giá tham chiếu mặc định để tính chi phí bóng (SPEC §17.11)
ALL_GATES = ["dev", "A", "B", "C"]
MODEL_DEFAULTS = dict(ctx_in=32768, max_out=4096, structured="none", vision=False, pdf=False, tokenizer_factor=1.0, strong=False)

PRESETS: dict[str, dict] = {
    "gemini": dict(
        kind="gemini_native", base_url="https://generativelanguage.googleapis.com/v1beta", display_name="Google Gemini API",
        quirks={"quota_scope": "deployment", "auth_header": "x-goog-api-key"}, key_regex=r"AIza[0-9A-Za-z_-]{30,60}",
        reset_tz="America/Los_Angeles", tpm_basis="input", limits_scope="deployment",
        data_policy_by_tier={"free": "may_train", "paid": "no_training"}, auto_flags_by_tier={"free": ["no_eea_uk_ch"]},
        default_models=[dict(model_id="gemini-3.8-flash", ctx_in=1_048_576, max_out=65_536, structured="json_schema", vision=True, pdf=True,
                             tokenizer_factor=1.0, strong=True, price_key="gemini-3.8-flash", shadow_price_key="gemini-3.8-flash")],
    ),
    "custom": dict(
        kind="openai_compat", base_url=None, display_name=None, quirks={"quota_scope": "group", "auth_header": "bearer"}, key_regex=None,
        reset_tz="UTC", tpm_basis="total", limits_scope="group", data_policy_by_tier={}, auto_flags_by_tier={}, default_models=[],
    ),
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,60}$")
_MODEL_SLUG = re.compile(r"^[a-z0-9][a-z0-9_./:-]{1,80}$")
_QUOTES = "\"'`\u201c\u201d\u2018\u2019[](){}<>"
_PLACEHOLDER = re.compile(
    r"(?i)^(x+|\*+|\.+|\u2026.*|your[-_ ]?(api[-_ ]?)?key.*|api[-_ ]?key|key\d*|todo|changeme|d\u00e1n.*|paste.*|<.*>|\{.*\}|.*(\.\.\.|\u2026).*)$"
)


# ------------------------------------------------------------------------------------------ tách và kiểm tra khoá


def slugify(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-")


def split_keys(text) -> list[tuple[str | None, str]]:
    """Tách chuỗi khoá thành [(nhãn | None, khoá)]. Chấp nhận dấu phẩy, chấm phẩy, xuống dòng, khoảng trắng; bỏ dấu nháy và ngoặc bao quanh,
    tiền tố 'Bearer'; hiểu `nhãn|khoá` và dòng kiểu .env (`TEN_BIEN=khoá`). Không kiểm tra định dạng (xem classify_secret)."""
    if isinstance(text, (list, tuple)):
        text = ",".join(str(x) for x in text)
    t = (text or "").replace("\ufeff", "")
    t = re.sub(r"\s*\|\s*", "|", t)
    t = re.sub(r"\s*=\s*", "=", t)
    out: list[tuple[str | None, str]] = []
    for tok in re.split(r"[,;\s]+", t):
        tok = tok.strip().strip(_QUOTES).strip()
        if not tok or tok.lower() == "bearer":
            continue
        label = None
        if "|" in tok:
            label, tok = tok.split("|", 1)
        else:
            m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.{8,})", tok)  # TEN_BIEN=khoá; vế phải >= 8 ký tự để không nhầm dấu '=' đệm của base64
            if m and set(m.group(2)) != {"="}:
                label, tok = m.group(1), m.group(2)
        out.append((label or None, tok.strip(_QUOTES).strip()))
    return out


def classify_secret(secret: str, key_regex: str | None = None, min_len: int = 16) -> tuple[str, str]:
    """('ok'|'too_short'|'invalid'|'placeholder', lý do)."""
    if len(secret) < min_len:
        return "too_short", f"ngắn hơn {min_len} ký tự"
    if not secret.isascii() or not secret.isprintable() or any(c.isspace() for c in secret):
        return "invalid", "chứa ký tự không hợp lệ (khoảng trắng, dấu tiếng Việt hoặc ký tự điều khiển)"
    if _PLACEHOLDER.match(secret) or len(set(secret)) <= 3 or Counter(secret).most_common(1)[0][1] / len(secret) > 0.6:
        return "placeholder", "trông như chuỗi giữ chỗ chưa được thay bằng khoá thật (một ký tự chiếm gần hết chuỗi)"
    if key_regex and not re.fullmatch(key_regex, secret):
        return "invalid", "không đúng định dạng khoá của nhà cung cấp này"
    return "ok", ""


@dataclass(frozen=True)
class KeyEntry:
    label: str | None
    secret: str = field(repr=False)  # repr ẩn khoá để không vô tình in ra log
    status: str  # ok | duplicate_in_request | duplicate_existing | too_short | invalid | placeholder
    reason: str = ""

    @property
    def last4(self) -> str:
        return last4(self.secret)


def parse_keys(text, *, key_regex: str | None = None, fingerprint_fn: Callable[[str], str] | None = None,
               existing_fingerprints: frozenset | set = frozenset(), min_len: int = 16) -> list[KeyEntry]:
    out: list[KeyEntry] = []
    seen: set[str] = set()
    for label, secret in split_keys(text):
        lab = None
        if label is not None:
            lab = slugify(label)
            if not _SLUG.match(lab):
                out.append(KeyEntry(label, secret, "invalid", f"nhãn '{label}' không hợp lệ (dùng chữ, số, gạch ngang; 2-60 ký tự)"))
                continue
        status, reason = classify_secret(secret, key_regex, min_len)
        if status == "ok" and secret in seen:
            status, reason = "duplicate_in_request", "khoá bị dán hai lần"
        elif status == "ok" and fingerprint_fn is not None and fingerprint_fn(secret) in existing_fingerprints:
            status, reason = "duplicate_existing", "khoá này đã có trong hệ thống"
        if status == "ok":
            seen.add(secret)
        out.append(KeyEntry(lab, secret, status, reason))
    return out


# ------------------------------------------------------------------------------------------ biên dịch khai báo


@dataclass
class DeclareResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fragment: dict = field(default_factory=lambda: {"providers": [], "models": [], "groups": [], "deployments": []})
    secrets: dict[str, str] = field(default_factory=dict, repr=False)  # credential_id -> khoá thật. CHỈ trong bộ nhớ, mã hoá rồi bỏ
    preview: dict = field(default_factory=lambda: {"groups": [], "skipped_keys": [], "counts": {}})

    @property
    def valid(self) -> bool:
        return not self.errors


def _next_index(existing_ids: set[str], stem: str) -> int:
    pat = re.compile(rf"^{re.escape(stem)}-(\d+)$")
    nums = [int(m.group(1)) for i in existing_ids if (m := pat.match(i))]
    return (max(nums) + 1) if nums else 1


def _model_slug(model_id: str) -> str:
    s = re.sub(r"[^a-z0-9_./:-]+", "-", model_id.lower()).strip("-./:_")
    return s[:60].strip("-./:_")


def _limits(d: dict | None) -> dict:
    base = {"rpm": None, "tpm": None, "rpd": None, "tpd": None, "concurrency": None}
    if d:
        base.update({k: d.get(k) for k in base})
    return base


def compile_declaration(decl: dict, cfg: dict | None = None, *, fingerprint_fn: Callable[[str], str] | None = None,
                        existing_fingerprints: frozenset | set = frozenset()) -> DeclareResult:
    """Biên dịch MỘT khai báo. cfg: pool_config hiện có (để dùng lại nhà cung cấp/model, tránh trùng id, nhân bản nhóm). Không sửa cfg."""
    cfg = cfg or {"providers": [], "models": [], "groups": [], "deployments": []}
    res = DeclareResult()
    err, warn = res.errors.append, res.warnings.append
    groups_by_id = {g["id"]: g for g in cfg.get("groups", [])}
    provs = {p["id"]: p for p in cfg.get("providers", [])}
    models_by_id = {m["id"]: m for m in cfg.get("models", [])}
    deps_by_group: dict[str, list[dict]] = {}
    for d in cfg.get("deployments", []):
        deps_by_group.setdefault(d["group"], []).append(d)

    # ---- 1. nguồn cấu hình: nhân bản từ một nhóm có sẵn, hoặc khai mới
    src = None
    if decl.get("clone_from_group"):
        src = groups_by_id.get(decl["clone_from_group"])
        if src is None:
            err(f"clone_from_group: không có nhóm '{decl['clone_from_group']}'")
            return res
    pdecl = decl.get("provider") or {}
    if src is not None:
        pid = src["provider"]
        provider = provs[pid]
        preset = "custom" if provider["kind"] == "openai_compat" else "gemini"
        preset_cfg = PRESETS["gemini"] if provider["kind"] == "gemini_native" else PRESETS["custom"]
    else:
        preset = pdecl.get("preset", "custom")
        if preset not in PRESETS:
            err(f"provider.preset '{preset}' không hợp lệ (chọn: {', '.join(PRESETS)})")
            return res
        preset_cfg = PRESETS[preset]
        pid = pdecl.get("id") or (preset if preset != "custom" else "")
        if not pid or not _SLUG.match(pid):
            err("provider.id bắt buộc và chỉ gồm chữ thường, số, gạch ngang (2-60 ký tự)")
            return res
        if pid in provs:
            provider = provs[pid]
        else:
            kind = pdecl.get("kind") or preset_cfg["kind"]
            base_url = pdecl.get("base_url") or preset_cfg["base_url"]
            if not base_url or not base_url.startswith("https://"):
                err("provider.base_url bắt buộc và phải bắt đầu bằng https://")
                return res
            provider = dict(id=pid, kind=kind, base_url=base_url, display_name=pdecl.get("display_name") or preset_cfg["display_name"] or pid,
                            quirks=copy.deepcopy(preset_cfg["quirks"]))
            res.fragment["providers"].append(provider)

    tier = decl.get("tier") or (src["tier"] if src else None)
    if tier not in ("free", "trial", "paid", "self_hosted"):
        err("tier bắt buộc: free, trial, paid hoặc self_hosted")
        return res
    data_policy = decl.get("data_policy") or (src["data_policy"] if src else preset_cfg["data_policy_by_tier"].get(tier, "unknown"))
    if src is None and "data_policy" not in decl and data_policy == "unknown":
        warn("data_policy chưa khai: để 'unknown' (bị coi như nhà cung cấp dùng dữ liệu, nên KHÔNG vào chế độ riêng tư). Hãy đọc điều khoản của nhà cung cấp và khai no_training hoặc may_train")
    reset_tz = decl.get("reset_tz") or (src["reset_tz"] if src else preset_cfg["reset_tz"])
    margins = dict(safety_margin=decl.get("safety_margin") or (src["safety_margin"] if src else 0.85), day_margin=decl.get("day_margin") or (src["day_margin"] if src else 0.95))

    # ---- 2. khoá
    fn = fingerprint_fn
    entries = parse_keys(decl.get("keys", ""), key_regex=preset_cfg["key_regex"], fingerprint_fn=fn, existing_fingerprints=existing_fingerprints)
    good = [e for e in entries if e.status == "ok"]
    for e in entries:
        if e.status != "ok":
            res.preview["skipped_keys"].append({"label": e.label, "last4": e.last4 if e.status != "too_short" else "****", "status": e.status, "reason": e.reason})
    if not entries:
        err("keys: chưa có khoá nào (dán các khoá cách nhau bằng dấu phẩy, chấm phẩy hoặc xuống dòng)")
    elif not good:
        err("keys: không có khoá nào hợp lệ để thêm (xem skipped_keys)")
    if res.errors:
        return res

    # ---- 3. gom khoá thành nhóm hạn mức
    stem = decl.get("group_prefix") or f"{pid}-{tier.replace('_', '-')}"
    if not _SLUG.match(stem):
        err("group_prefix không hợp lệ")
        return res
    mode = decl.get("group_mode", "per_key")
    plan: dict[str, list[KeyEntry]] = {}
    used = set(groups_by_id)
    if mode == "single_group":
        plan[stem] = good
    else:
        nxt = _next_index(used, stem)
        for e in good:
            if e.label:
                gid = f"{stem}-{e.label}"
            else:
                gid = f"{stem}-{nxt}"
                nxt += 1
                while gid in used or gid in plan:
                    gid = f"{stem}-{nxt}"
                    nxt += 1
            plan.setdefault(gid, []).append(e)

    # ---- 4. model và deployment dùng chung cho mọi nhóm mới
    if src is not None and not decl.get("models"):
        model_specs = None  # sao chép deployment của nhóm nguồn
    else:
        raw = copy.deepcopy(decl.get("models") or [])
        defaults = {m["model_id"]: m for m in preset_cfg["default_models"]}
        if not raw:
            raw = [dict(m) for m in preset_cfg["default_models"]]
        model_specs = []
        for m in raw:
            base = dict(MODEL_DEFAULTS, price_key=None, shadow_price_key=REFERENCE_PRICE_KEY, tags=[], quality={})
            base.update(defaults.get(m["model_id"], {}))
            base.update({k: v for k, v in m.items() if v is not None})
            used_defaults = [k for k in ("ctx_in", "max_out", "structured") if k not in m and m["model_id"] not in defaults]
            if used_defaults:
                warn(f"model {m['model_id']}: dùng mặc định bảo thủ cho {', '.join(used_defaults)}; hãy khai đúng theo trang model của nhà cung cấp")
            model_specs.append(base)
        if not model_specs:
            err("models: bắt buộc với nhà cung cấp tuỳ chỉnh (khai ít nhất model_id)")
            return res
    scope = decl.get("limits_scope") or (preset_cfg["limits_scope"])
    declared_limits = decl.get("limits")
    if declared_limits is None and src is None:
        warn("limits chưa khai: dùng hạn mức BẢO THỦ khởi điểm (5 yêu cầu/phút, 1 lời gọi đồng thời). Hãy chép số thật từ bảng điều khiển của nhà cung cấp rồi sửa")
    group_limits_default = _limits(declared_limits or CONSERVATIVE_LIMITS)

    # ---- 5. cờ ToS, cổng, xác nhận rủi ro
    n_same = sum(1 for g in cfg.get("groups", []) if g["provider"] == pid and g["tier"] == tier and g["id"] not in plan)
    will_have = n_same + len(plan)
    acked = set(decl.get("risk_ack") or [])
    new_group_ids = [gid for gid in plan if gid not in groups_by_id]
    for gid in plan:
        existing = groups_by_id.get(gid)
        flags = set(decl.get("tos_flags") or [])
        flags |= set(preset_cfg["auto_flags_by_tier"].get(tier, []))
        if src is not None:
            flags |= set(src["tos_flags"])
        if tier == "trial":
            flags |= {"trial_only", "no_personal_data"}
        if tier in ("free", "trial") and will_have >= 2:
            flags.add("multi_account_risk")
        if existing is not None:
            flags |= set(existing["tos_flags"])
        need = flags & RISK_FLAGS
        have = acked | (set(src["risk_ack"]) if src else set()) | (set(existing["risk_ack"]) if existing else set())
        if need - have:
            err(f"nhóm '{gid}' có cờ rủi ro điều khoản {sorted(need - have)}: cần xác nhận chấp nhận bằng risk_ack: {sorted(need - have)}")
        gates = decl.get("allowed_gates") or (list(src["allowed_gates"]) if src else None)
        if gates is None:
            gates = ["dev"] if "trial_only" in flags else (["dev", "A"] if flags & PUBLIC_BLOCKING_FLAGS else list(ALL_GATES))
        if existing is None:
            res.fragment["groups"].append(dict(
                id=gid, provider=pid, label=f"{provider['display_name']} / {tier} / {gid}", tier=tier, data_policy=data_policy, reset_tz=reset_tz,
                tos_flags=sorted(flags), allowed_gates=gates, **margins,
                limits=group_limits_default if scope == "group" else _limits(None), credentials=[], enabled=True,
                risk_ack=sorted(need & have)))
    if will_have >= 2 and tier in ("free", "trial"):
        for g in cfg.get("groups", []):
            if g["provider"] == pid and g["tier"] == tier and "multi_account_risk" not in g["tos_flags"] and g["id"] not in plan:
                warn(f"nhóm hiện có '{g['id']}' cùng nhà cung cấp và gói nhưng chưa gắn cờ multi_account_risk: nên bổ sung để cổng công khai chặn đúng")
    if res.errors:
        return res

    # ---- 6. dựng khoá, model, deployment, bí mật, xem trước
    frag = res.fragment
    frag_groups = {g["id"]: g for g in frag["groups"]}
    for gid, ents in plan.items():
        is_new = gid in frag_groups
        if is_new:
            g, have_ids = frag_groups[gid], set()
        else:  # nhóm đã có: chỉ đưa khoá MỚI vào fragment (không sửa cfg của người gọi); merge_fragment sẽ gộp vào nhóm cũ
            base = groups_by_id[gid]
            have_ids = {c["id"] for c in base["credentials"]}
            g = copy.deepcopy(base)
            g["credentials"] = []
            frag["groups"].append(g)
            frag_groups[gid] = g
        k = 0
        pcreds = []
        for e in ents:
            k += 1
            cid = f"{gid}-k{k}"
            while cid in have_ids:
                k += 1
                cid = f"{gid}-k{k}"
            have_ids.add(cid)
            g["credentials"].append(dict(id=cid, label=f"Khoá {cid[len(gid) + 2:]} (\u2026{e.last4})", secret_ref=f"enc:{cid}"))
            res.secrets[cid] = e.secret
            pcreds.append({"id": cid, "last4": e.last4, "status": "new"})
        deps_preview = []
        if is_new:
            if model_specs is None:
                for d in deps_by_group.get(src["id"], []):
                    base_model = d["model"]
                    nd = dict(copy.deepcopy(d), id=f"{gid}/{d['id'].split('/', 1)[-1]}", group=gid, model=base_model)
                    frag["deployments"].append(nd)
                    deps_preview.append({"id": nd["id"], "model": nd["model"], "limits": nd["limits"], "limits_source": "nhân bản từ " + src["id"]})
            else:
                for spec in model_specs:
                    slug = _model_slug(spec["model_id"])
                    mid = f"{pid}:{slug}"
                    if not _MODEL_SLUG.match(mid):
                        err(f"model_id '{spec['model_id']}' không tạo được id hợp lệ")
                        continue
                    if mid not in models_by_id and all(m["id"] != mid for m in frag["models"]):
                        frag["models"].append(dict(
                            id=mid, provider=pid, model_id=spec["model_id"], ctx_in=spec["ctx_in"], max_out=spec["max_out"], structured=spec["structured"],
                            vision=spec["vision"], pdf=spec["pdf"], tokenizer_factor=spec["tokenizer_factor"], price_key=spec.get("price_key"),
                            shadow_price_key=spec.get("shadow_price_key"), quality=spec.get("quality") or {},
                            adapter_options={"max_tokens_param": "max_tokens", "reasoning_param": "thinking_level" if preset == "gemini" else "none", "strip_think_tags": preset != "gemini"}))
                    tags = sorted({tier.replace("_", "-"), *(["strong"] if spec["strong"] else []), *spec.get("tags", [])})
                    own_limits = spec.get("limits")
                    lim = _limits(own_limits) if own_limits else (group_limits_default if scope == "deployment" else _limits(None))
                    if tier == "paid" and not spec.get("price_key"):
                        warn(f"model {spec['model_id']}: tier paid nhưng chưa có price_key: tiền thật sẽ tính 0 cho tới khi thêm bảng giá vào llm_prices")
                    did = f"{gid}/{slug}"
                    frag["deployments"].append(dict(
                        id=did, group=gid, model=mid, limits=lim, tpm_basis=preset_cfg["tpm_basis"], price_mode="metered" if tier == "paid" else "free",
                        weight=1.0, tags=tags, enabled=True))
                    src_txt = "khai báo" if (own_limits or declared_limits) else "mặc định bảo thủ"
                    deps_preview.append({"id": did, "model": mid, "limits": lim, "limits_source": src_txt if (own_limits or scope == "deployment") else "cấp tài khoản: " + src_txt})
        res.preview["groups"].append(dict(
            id=gid, existing=not is_new, tier=g["tier"], data_policy=g["data_policy"], tos_flags=g["tos_flags"],
            allowed_gates=g["allowed_gates"], risk_ack=g["risk_ack"], account_limits=g["limits"] if scope == "group" else None,
            credentials=pcreds, deployments=deps_preview))
    qless = [d["id"] for d in frag["deployments"] if not next((m for m in frag["models"] + cfg.get("models", []) if m["id"] == d["model"]), {}).get("quality")]
    if qless:
        warn(f"{len(qless)} deployment chưa có điểm chất lượng: chỉ phục vụ các profile không đòi chất lượng cho tới khi chạy kiểm định (probe) hoặc khai quality")
    if decl.get("validate_keys"):
        res.preview["validate_keys"] = "pending"
    res.preview["counts"] = dict(groups=len(plan), new_groups=len(new_group_ids), keys=len(good), skipped_keys=len(entries) - len(good),
                                 deployments=len(frag["deployments"]), models=len(frag["models"]), providers=len(frag["providers"]))
    return res


def merge_fragment(cfg: dict, fragment: dict) -> dict:
    """Gộp các phần của một lần khai báo vào pool_config (nhà cung cấp/model trùng id thì giữ bản cũ; credential gộp vào nhóm đã có)."""
    out = copy.deepcopy(cfg)
    for sect in ("providers", "models", "groups", "deployments"):
        out.setdefault(sect, [])
    have = {sect: {x["id"] for x in out[sect]} for sect in ("providers", "models", "groups", "deployments")}
    for sect in ("providers", "models", "deployments"):
        for x in fragment.get(sect, []):
            if x["id"] not in have[sect]:
                out[sect].append(copy.deepcopy(x))
                have[sect].add(x["id"])
    for g in fragment.get("groups", []):
        if g["id"] in have["groups"]:
            tgt = next(x for x in out["groups"] if x["id"] == g["id"])
            known = {c["id"] for c in tgt["credentials"]}
            tgt["credentials"] += [copy.deepcopy(c) for c in g["credentials"] if c["id"] not in known]
        else:
            out["groups"].append(copy.deepcopy(g))
            have["groups"].add(g["id"])
    return out


def compile_all(doc: dict, cfg: dict | None = None, **kw) -> DeclareResult:
    """Biên dịch tài liệu khai báo gồm nhiều khai báo, lần lượt, mỗi khai báo thấy kết quả của các khai báo trước (đánh số nhóm và phát hiện khoá trùng nhất quán)."""
    total = DeclareResult()
    working = copy.deepcopy(cfg) if cfg else {"providers": [], "models": [], "groups": [], "deployments": []}
    seen_secrets: dict[str, str] = {}
    for i, d in enumerate(doc.get("declarations", []), 1):
        r = compile_declaration(d, working, **kw)
        total.errors += [f"khai báo #{i}: {e}" for e in r.errors]
        total.warnings += [f"khai báo #{i}: {w}" for w in r.warnings]
        for secret_id, secret in r.secrets.items():
            if secret in seen_secrets.values():
                total.errors.append(f"khai báo #{i}: một khoá bị dùng ở hai khai báo khác nhau")
            seen_secrets[secret_id] = secret
        if r.valid:
            for sect in ("providers", "models", "groups", "deployments"):
                total.fragment[sect] += r.fragment[sect]
            total.secrets.update(r.secrets)
            total.preview["groups"] += r.preview["groups"]
            total.preview["skipped_keys"] += r.preview["skipped_keys"]
            working = merge_fragment(working, r.fragment)
    total.preview["counts"] = dict(groups=len(total.preview["groups"]), keys=len(total.secrets), skipped_keys=len(total.preview["skipped_keys"]),
                                   deployments=len(total.fragment["deployments"]), models=len(total.fragment["models"]), providers=len(total.fragment["providers"]))
    return total
