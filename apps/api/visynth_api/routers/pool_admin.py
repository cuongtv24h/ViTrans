"""Admin API cho LLM Pool (SPEC §17, §17.14) — cấu hình, khoá, trạng thái, dung lượng, sự cố, kiểm định.

Nguyên tắc bất di bất dịch:
  * **Khoá API chỉ ghi, không bao giờ đọc ra**: mọi phản hồi chỉ có `last4` và trạng thái; khoá được mã hoá
    bằng `POOL_MASTER_KEY` (ngoài CSDL, ngoài bản sao lưu) trước khi chạm đĩa;
  * `dry_run` mặc định **bật** cho nhập cấu hình và khai báo — chưa ghi gì khi chưa xác nhận;
  * mọi thay đổi ghi `audit_log` và tăng `app_settings.pool_version`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import yaml
from fastapi import APIRouter, Body, Depends, Query, Request, Response

from visynth.pool import dbstore
from visynth_api.db import Database
from visynth_api.errors import Problem, sql_problem
from visynth_api.models import (
    PoolCredentialCreate,
    PoolCredentialPatch,
    PoolDeploymentPatch,
    PoolGroupPatch,
)
from visynth_api.security import require_role

router = APIRouter(prefix="/admin/pool", tags=["pool"])

admin_only = require_role("admin")


def _db(request: Request) -> Database:
    return request.app.state.db


def _master_key(request: Request) -> bytes:
    key = request.app.state.settings.master_key()
    if key is None:
        raise Problem(
            503,
            "pool_master_key_missing",
            "chưa cấu hình POOL_MASTER_KEY nên không thể mã hoá/giải mã khoá API",
        )
    return key


# --------------------------------------------------------------------------- cấu hình


@router.get("/config")
def export_config(request: Request, admin: dict = Depends(admin_only)) -> dict:
    """Xuất PoolConfig từ CSDL. Khoá chỉ ở dạng tham chiếu `enc:<id>`, kèm `last4` để nhận diện."""
    return dbstore.load_config(_db(request))


@router.put("/config")
def import_config(
    request: Request,
    body: dict[str, Any] = Body(..., description="PoolConfig (schemas/pool_config.schema.json)"),
    dry_run: bool = Query(default=True),
    admin: dict = Depends(admin_only),
) -> dict:
    db = _db(request)
    try:
        return dbstore.apply_config(db, body, actor_id=str(admin["id"]), dry_run=dry_run)
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc


# --------------------------------------------------------------------------- khai báo


@router.post("/declare")
async def declare_pool(
    request: Request,
    dry_run: bool = Query(default=True),
    admin: dict = Depends(admin_only),
) -> dict:
    """Khai báo hàng loạt (SPEC §17.14). Nhận JSON hoặc YAML; **không ghi log nội dung yêu cầu**."""
    db = _db(request)
    raw = await request.body()
    content_type = request.headers.get("content-type", "application/json")
    try:
        doc = yaml.safe_load(raw.decode("utf-8")) if "yaml" in content_type else _json(raw)
    except (yaml.YAMLError, ValueError, UnicodeDecodeError) as exc:
        raise Problem(422, "declaration_invalid", f"tài liệu khai báo không đọc được: {exc}") from exc
    if not isinstance(doc, dict):
        raise Problem(422, "declaration_invalid", "tài liệu khai báo phải là một đối tượng")
    # Bước xem trước cũng cần khoá chủ: dấu vân tay khoá là HMAC(khoá chủ, khoá API) nên phải
    # dùng đúng khoá chủ thật, nếu không thì "trùng khoá" ở bước xem trước sẽ khác lúc áp dụng.
    try:
        return dbstore.declare(
            db,
            doc,
            _master_key(request),
            actor_id=str(admin["id"]),
            dry_run=dry_run,
        )
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc


@router.get("/declaration-template")
def declaration_template(admin: dict = Depends(admin_only)) -> Response:
    """Mẫu YAML có chú thích tiếng Việt (cùng nội dung `docs/examples/pool_declaration.template.yaml`)."""
    path = _template_path()
    text = path.read_text(encoding="utf-8") if path else _FALLBACK_TEMPLATE
    return Response(text, media_type="application/yaml; charset=utf-8")


def _template_path() -> Path | None:
    """Tệp mẫu trong kho (khi chạy từ mã nguồn) — nếu thiếu thì dùng bản dựng sẵn trong mã."""
    import os

    env = os.environ.get("VISYNTH_DECLARATION_TEMPLATE")
    if env and Path(env).is_file():
        return Path(env)
    for base in (Path(__file__).resolve().parents[4], Path.cwd()):
        candidate = base / "docs" / "examples" / "pool_declaration.template.yaml"
        if candidate.is_file():
            return candidate
    return None


def _json(raw: bytes) -> dict:
    import json

    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------- trạng thái & dung lượng


@router.get("/status")
def pool_status(request: Request, admin: dict = Depends(admin_only)) -> list[dict]:
    return dbstore.pool_status(_db(request))


@router.get("/capacity")
def pool_capacity(
    request: Request,
    privacy_class: str = Query(default="standard", pattern="^(standard|private)$"),
    admin: dict = Depends(admin_only),
) -> dict:
    return dbstore.pool_capacity(_db(request), privacy_class)


@router.get("/incidents")
def pool_incidents(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    deployment_id: str | None = None,
    admin: dict = Depends(admin_only),
) -> list[dict]:
    return dbstore.pool_incidents(_db(request), cursor=cursor, limit=limit, deployment_id=deployment_id)


# --------------------------------------------------------------------------- khoá


@router.get("/groups/{group_id}")
def get_group(group_id: str, request: Request, admin: dict = Depends(admin_only)) -> dict:
    payload = dbstore.group_payload(_db(request), group_id)
    if payload is None:
        raise Problem(404, "not_found", "không có nhóm hạn mức này")
    return payload


@router.patch("/groups/{group_id}")
def patch_group(group_id: str, body: PoolGroupPatch, request: Request, admin: dict = Depends(admin_only)) -> dict:
    db = _db(request)
    try:
        payload = dbstore.patch_group(db, group_id, body.model_dump(exclude_unset=True), actor_id=str(admin["id"]))
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    if payload is None:
        raise Problem(404, "not_found", "không có nhóm hạn mức này")
    return payload


@router.post("/groups/{group_id}/credentials", status_code=201)
def add_credential(
    group_id: str, body: PoolCredentialCreate, request: Request, admin: dict = Depends(admin_only)
) -> dict:
    db = _db(request)
    if db.one("SELECT id FROM llm_quota_groups WHERE id = %s", (group_id,)) is None:
        raise Problem(404, "not_found", "không có nhóm hạn mức này")
    try:
        return dbstore.add_credential(db, group_id, body.label, body.secret, _master_key(request))
    except psycopg.errors.UniqueViolation as exc:  # dấu vân tay trùng ở tầng CSDL
        raise Problem(409, "conflict", "khoá này đã có trong pool") from exc
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc


@router.patch("/credentials/{credential_id}")
def patch_credential(
    credential_id: str, body: PoolCredentialPatch, request: Request, admin: dict = Depends(admin_only)
) -> dict:
    db = _db(request)
    payload = dbstore.set_credential_status(db, credential_id, body.status, actor_id=str(admin["id"]))
    if payload is None:
        raise Problem(404, "not_found", "không có khoá này")
    return payload


@router.delete("/credentials/{credential_id}", status_code=204)
def delete_credential(credential_id: str, request: Request, admin: dict = Depends(admin_only)) -> Response:
    if not dbstore.delete_credential(_db(request), credential_id, actor_id=str(admin["id"])):
        raise Problem(404, "not_found", "không có khoá này")
    return Response(status_code=204)


# --------------------------------------------------------------------------- deployment


@router.post("/deployments/{deployment_id:path}/probe", status_code=202)
def probe_deployment(deployment_id: str, request: Request, admin: dict = Depends(admin_only)) -> dict:
    """Chạy bài kiểm định nhận vào pool (SPEC §17.9) rồi ghi `llm_probe_runs` + cập nhật `llm_models.quality`.

    Không có mạng hoặc thiếu khoá thì ghi lại đúng sự thật đó (`passed = false`, `note`) để Admin biết
    là *chưa* kiểm định — không bao giờ đánh dấu đạt thay.
    """
    db = _db(request)
    if db.one("SELECT id FROM llm_deployments WHERE id = %s", (deployment_id,)) is None:
        raise Problem(404, "not_found", "không có deployment này")
    try:
        results = _run_probe(request, deployment_id)
    except Exception as exc:  # noqa: BLE001 - kiểm định lỗi thì ghi lại lỗi, không làm sập API
        results = dbstore.probe_template()
        results["error"] = f"{type(exc).__name__}: {exc}"[:300]
        results["note"] = "kiểm định không chạy được"
    passed = bool(results.pop("passed", False))
    note = str(results.pop("note", "") or "") or None
    run = dbstore.record_probe(db, deployment_id, results, passed=passed, note=note)
    if passed:
        dbstore.sync_model_quality(db, deployment_id, results)
    return run


# `deploymentId` chứa dấu "/" (vd `gemini-main/flash`) nên phải dùng bộ chuyển `:path`;
# vì vậy route `/probe` được khai TRƯỚC route này để không bị nuốt mất.
@router.patch("/deployments/{deployment_id:path}")
def patch_deployment(
    deployment_id: str, body: PoolDeploymentPatch, request: Request, admin: dict = Depends(admin_only)
) -> dict:
    db = _db(request)
    payload = dbstore.patch_deployment(
        db, deployment_id, body.model_dump(exclude_unset=True), actor_id=str(admin["id"])
    )
    if payload is None:
        raise Problem(404, "not_found", "không có deployment này")
    return payload


def _run_probe(request: Request, deployment_id: str) -> dict:
    """Gọi bộ kiểm định thật (`visynth.pool.probe`) với khoá giải mã từ CSDL."""
    from visynth.pool.model import load_pool_model
    from visynth.pool.probe import probe_deployment

    db = _db(request)
    master = _master_key(request)
    cfg = dbstore.load_config(db)
    model = load_pool_model(cfg)
    deployment = next((d for d in model.deployments if d.id == deployment_id), None)
    if deployment is None:
        raise RuntimeError("deployment không nạp được từ cấu hình")
    providers = {p["id"]: p for p in cfg["providers"]}
    refs = {cred["id"]: cred.get("secret_ref", "") for group in cfg["groups"] for cred in group.get("credentials", [])}
    run = probe_deployment(
        deployment,
        providers[deployment.group.provider],
        credential_refs=refs,
        registry=dbstore.DbRegistry(db, master),
    )
    json_ok = bool((run.get("json") or {}).get("ok"))
    vi_ok = bool((run.get("vi_write") or {}).get("ok")) if run.get("vi_write") is not None else None
    errors = list(run.get("errors") or [])
    return {
        "json": json_ok,
        "vi_write": vi_ok,
        "latency_ms_p50": run.get("latency_ms") or None,
        "tokenizer_factor": run.get("tokenizer_factor"),
        "checked_at": run.get("checked_at"),
        "credential_id": run.get("credential_id"),
        "errors": errors,
        "passed": json_ok and (vi_ok is not False),
        "note": "; ".join(errors)[:300] if errors else "kiểm định qua API quản trị",
    }


_FALLBACK_TEMPLATE = """# Mẫu khai báo pool (rút gọn) — bản đầy đủ: docs/examples/pool_declaration.template.yaml
declarations:
  - preset: gemini
    group:
      label: "Gemini free"
      tier: free
      data_policy: may_train
    limits: {rpm: 10, tpm: 250000, rpd: 250}
    keys: |
      nhãn-tài-khoản-1|DÁN_KHOÁ_1
    risk_ack: [multi_account_risk]
"""
