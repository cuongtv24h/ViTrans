"""Nhập kết quả `probe` và hạn mức đo được vào `pool_config` (SPEC §16.4b/c/d, §17.14).

`probe` KHÔNG tự sửa cấu hình — chủ hệ thống duyệt rồi mới nhập. Module này là bước duyệt đó: tính ra
danh sách thay đổi từ `probe.json` (điểm `json`/`vi_write`, `tokenizer_factor`) và từ tệp hạn mức đo tay
(`--limits`, số đọc từ bảng điều khiển nhà cung cấp, đối chiếu với lỗi 429 thật), rồi:

* mặc định **chỉ xem trước** (dry-run) — không ghi gì;
* `--write` mới ghi, và ghi theo kiểu nguyên tử sau khi cấu hình mới đã **qua `validate_config`**;
* ghi ngày kiểm tra vào `label` của nhóm (schema không cho thêm trường tự do, `label` là chỗ hợp lệ).

    visynth pool apply-probe --config pool_config.json --probe eval/runs/probe.json --limits eval/runs/limits.json
    visynth pool apply-probe … --write
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from visynth.pool.validate import validate_config

__all__ = ["plan_updates", "apply_updates"]


def _model_of(cfg: dict, deployment_id: str) -> dict | None:
    dep = next((d for d in cfg.get("deployments", []) if d.get("id") == deployment_id), None)
    if dep is None:
        return None
    return next((m for m in cfg.get("models", []) if m.get("id") == dep.get("model")), None)


def _group_of(cfg: dict, deployment_id: str) -> dict | None:
    dep = next((d for d in cfg.get("deployments", []) if d.get("id") == deployment_id), None)
    return next((g for g in cfg.get("groups", []) if g.get("id") == (dep or {}).get("group")), None) if dep else None


def plan_updates(cfg: dict, probe: dict, limits: dict | None = None, *, today: str | None = None) -> dict:
    """Tính danh sách thay đổi (không sửa `cfg`). Trả về `{changes, skipped, notes}`."""
    today = today or time.strftime("%d/%m/%Y")
    changes: list[dict] = []
    skipped: list[str] = []

    for run in probe.get("runs") or []:
        dep_id = run.get("deployment_id")
        model = _model_of(cfg, dep_id or "")
        if model is None:
            skipped.append(f"{dep_id}: không có trong pool_config")
            continue
        quality = model.get("quality") or {}
        if run.get("json") is not None:
            new_json = 1.0 if (run.get("json") or {}).get("ok") else 0.0
            if quality.get("json") != new_json:
                changes.append(
                    {"where": f"models[{model['id']}].quality.json", "from": quality.get("json"), "to": new_json}
                )
        vi = run.get("vi_write")
        if vi is not None:
            new_vi = 1.0 if vi.get("ok") else 0.0
            if quality.get("vi_write") != new_vi:
                changes.append(
                    {"where": f"models[{model['id']}].quality.vi_write", "from": quality.get("vi_write"), "to": new_vi}
                )
        factor = run.get("tokenizer_factor")
        if factor and model.get("tokenizer_factor") != factor:
            changes.append(
                {
                    "where": f"models[{model['id']}].tokenizer_factor",
                    "from": model.get("tokenizer_factor"),
                    "to": factor,
                    "source": run.get("tokenizer_source"),
                }
            )
        group = _group_of(cfg, dep_id or "")
        if group is not None:
            label = str(group.get("label") or group.get("id"))
            stamp = f"probe {today}"
            if stamp not in label:
                new_label = (label[: max(0, 118 - len(stamp) - 2)] + " — " + stamp)[:120]
                changes.append(
                    {"where": f"groups[{group['id']}].label", "from": label, "to": new_label, "kind": "probe_date"}
                )

    if limits:
        for dep_id, values in (limits.get("deployments") or {}).items():
            dep = next((d for d in cfg.get("deployments", []) if d.get("id") == dep_id), None)
            if dep is None:
                skipped.append(f"limits/{dep_id}: không có deployment này")
                continue
            current = dict(dep.get("limits") or {})
            for key, value in values.items():
                if current.get(key) != value:
                    changes.append(
                        {"where": f"deployments[{dep_id}].limits.{key}", "from": current.get(key), "to": value}
                    )
                    current[key] = value
        for group_id, values in (limits.get("groups") or {}).items():
            group = next((g for g in cfg.get("groups", []) if g.get("id") == group_id), None)
            if group is None:
                skipped.append(f"limits/{group_id}: không có nhóm này")
                continue
            current = dict(group.get("limits") or {})
            for key, value in values.items():
                if current.get(key) != value:
                    changes.append({"where": f"groups[{group_id}].limits.{key}", "from": current.get(key), "to": value})
                    current[key] = value

    notes = [
        "Điểm chất lượng và tokenizer_factor phải đến từ `probe` chạy trên chính deployment đó.",
        "Hạn mức (`--limits`) nên đọc từ bảng điều khiển nhà cung cấp và đối chiếu với lỗi 429 thật (§16.4b).",
    ]
    return {"changes": changes, "skipped": skipped, "notes": notes, "today": today}


def apply_updates(cfg: dict, plan: dict) -> tuple[dict, list[str]]:
    """Áp `plan` lên bản sao của `cfg`; kiểm `validate_config` trước khi trả về. Trả `(cfg_mới, lỗi)`."""
    new = json.loads(json.dumps(cfg))
    for change in plan["changes"]:
        where = change["where"]
        kind = change.get("kind")
        if where.endswith(".label") and kind == "probe_date":
            group_id = where[len("groups[") : -len("].label")]
            for group in new.get("groups", []):
                if group.get("id") == group_id:
                    group["label"] = change["to"]
            continue
        if where.startswith("models[") and ".quality." in where:
            prefix, _, field = where.partition("].quality.")
            model_id = prefix[len("models[") :]
            for model in new.get("models", []):
                if model.get("id") == model_id:
                    model.setdefault("quality", {})[field] = change["to"]
            continue
        if where.startswith("models[") and where.endswith("].tokenizer_factor"):
            model_id = where[len("models[") : -len("].tokenizer_factor")]
            for model in new.get("models", []):
                if model.get("id") == model_id:
                    model["tokenizer_factor"] = change["to"]
            continue
        if where.startswith("deployments[") and ".limits." in where:
            prefix, _, key = where.partition("].limits.")
            dep_id = prefix[len("deployments[") :]
            for dep in new.get("deployments", []):
                if dep.get("id") == dep_id:
                    dep.setdefault("limits", {})[key] = change["to"]
            continue
        if where.startswith("groups[") and ".limits." in where:
            prefix, _, key = where.partition("].limits.")
            group_id = prefix[len("groups[") :]
            for group in new.get("groups", []):
                if group.get("id") == group_id:
                    group.setdefault("limits", {})[key] = change["to"]
            continue
    report = validate_config(new)
    return new, ([] if report["valid"] else report["errors"])


def write_config(cfg: dict, path: str | Path) -> Path:
    """Ghi nguyên tử: tệp tạm cùng thư mục rồi `os.replace` (không để lại tệp hỏng nếu ghi lỗi)."""
    import os
    import tempfile

    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".pool_config.tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target
