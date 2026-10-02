"""Kiểm thử vòng nhập số đo `probe` vào `pool_config` (§16.4b/c/d, §17.14).

Quy tắc quan trọng nhất: `probe` **không** tự sửa cấu hình. Bước nhập phải (1) chỉ tính thay đổi,
(2) xem trước được, (3) chỉ ghi khi cấu hình mới đã qua `validate_config`, (4) không phá tệp nếu lỗi.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visynth.pool.apply_probe import apply_updates, plan_updates, write_config
from visynth.pool.validate import validate_config

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "pool_config.example.json").read_text(encoding="utf-8"))


def _probe(**overrides) -> dict:
    run = {
        "deployment_id": "gemini-main/flash",
        "json": {"ok": False, "ladder": [{"valid": False}]},
        "vi_write": {"ok": True, "words": 61},
        "tokenizer_factor": 1.42,
        "tokenizer_source": "json",
        "errors": [],
    }
    run.update(overrides)
    return {"runs": [run], "suggest_updates": {}, "totals": {}}


# ------------------------------------------------------------------ tính thay đổi


def test_plan_updates_reports_quality_factor_and_probe_date():
    plan = plan_updates(CFG, _probe(), today="05/10/2026")
    wheres = {c["where"] for c in plan["changes"]}
    assert wheres == {
        "models[gemini:gemini-3.8-flash].quality.json",
        "models[gemini:gemini-3.8-flash].quality.vi_write",
        "models[gemini:gemini-3.8-flash].tokenizer_factor",
        "groups[gemini-main].label",
    }
    factor = next(c for c in plan["changes"] if c["where"].endswith("tokenizer_factor"))
    assert (factor["from"], factor["to"], factor["source"]) == (1.0, 1.42, "json")
    json_change = next(c for c in plan["changes"] if c["where"].endswith("quality.json"))
    assert json_change["to"] == 0.0  # probe nói JSON KHÔNG đạt


def test_plan_updates_does_not_mutate_the_config():
    before = json.dumps(CFG, sort_keys=True)
    plan_updates(CFG, _probe(), today="05/10/2026")
    assert json.dumps(CFG, sort_keys=True) == before  # chỉ tính, không sửa


def test_plan_updates_is_idempotent_when_values_already_match():
    once = apply_updates(CFG, plan_updates(CFG, _probe(), today="05/10/2026"))[0]
    again = plan_updates(once, _probe(), today="05/10/2026")
    assert [c["where"] for c in again["changes"]] == []  # đã khớp thì không còn thay đổi


def test_plan_updates_skips_unknown_deployment_and_reports_it():
    plan = plan_updates(CFG, _probe(deployment_id="khong-co/thật"), today="05/10/2026")
    assert plan["changes"] == []
    assert any("không có trong pool_config" in s for s in plan["skipped"])


def test_plan_updates_applies_measured_limits():
    limits = {
        "deployments": {"gemini-main/flash": {"rpm": 120, "rpd": 900}},
        "groups": {"gemini-main": {"tpd": 4_000_000}},
    }
    plan = plan_updates(CFG, {"runs": []}, limits, today="05/10/2026")
    wheres = {c["where"]: c["to"] for c in plan["changes"]}
    assert wheres["deployments[gemini-main/flash].limits.rpm"] == 120
    assert wheres["groups[gemini-main].limits.tpd"] == 4_000_000


# ------------------------------------------------------------------ áp và ghi


def test_apply_updates_yields_a_valid_config_and_keeps_other_fields():
    new_cfg, errors = apply_updates(CFG, plan_updates(CFG, _probe(), today="05/10/2026"))
    assert errors == []
    assert validate_config(new_cfg)["valid"] is True
    model = next(m for m in new_cfg["models"] if m["id"] == "gemini:gemini-3.8-flash")
    assert model["quality"]["json"] == 0.0 and model["quality"]["vi_write"] == 1.0
    assert model["quality"]["long_context"] == 0.95  # giữ nguyên điểm khác
    assert model["tokenizer_factor"] == 1.42
    group = next(g for g in new_cfg["groups"] if g["id"] == "gemini-main")
    assert group["label"].endswith("probe 05/10/2026")
    original = next(g for g in CFG["groups"] if g["id"] == "gemini-main")
    assert group["data_policy"] == original["data_policy"]  # không đụng trường khác


def test_apply_updates_refuses_out_of_range_factor():
    plan = plan_updates(CFG, _probe(tokenizer_factor=99.0), today="05/10/2026")
    _, errors = apply_updates(CFG, plan)
    assert errors and any("tokenizer_factor" in e for e in errors)


def test_write_config_is_atomic_and_leaves_valid_json(tmp_path):
    path = tmp_path / "pool_config.json"
    path.write_text(json.dumps(CFG, ensure_ascii=False), encoding="utf-8")
    new_cfg, errors = apply_updates(CFG, plan_updates(CFG, _probe(), today="05/10/2026"))
    assert errors == []
    write_config(new_cfg, path)
    assert validate_config(json.loads(path.read_text(encoding="utf-8")))["valid"] is True
    assert not list(tmp_path.glob(".pool_config.tmp.*"))  # không để lại tệp tạm


# ------------------------------------------------------------------ CLI


@pytest.fixture
def cli(tmp_path):
    from visynth import cli as cli_mod

    def _run(*argv: str):
        return cli_mod.main(["pool", "apply-probe", *argv])

    return _run


def test_cli_dry_run_does_not_touch_the_file(tmp_path, cli, capsys):
    path = tmp_path / "pool_config.json"
    path.write_text(json.dumps(CFG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps(_probe(), ensure_ascii=False), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    assert cli("--config", str(path), "--probe", str(probe_path), "--today", "05/10/2026") == 0
    out = capsys.readouterr().out
    assert "[xem trước]" in out and "tokenizer_factor" in out
    assert path.read_text(encoding="utf-8") == before


def test_cli_write_applies_and_validates(tmp_path, cli, capsys):
    path = tmp_path / "pool_config.json"
    path.write_text(json.dumps(CFG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps(_probe(), ensure_ascii=False), encoding="utf-8")
    limits_path = tmp_path / "limits.json"
    limits_path.write_text(json.dumps({"deployments": {"gemini-main/flash": {"rpd": 900}}}), encoding="utf-8")

    code = cli("--config", str(path), "--probe", str(probe_path), "--limits", str(limits_path), "--write")
    assert code == 0
    assert "Đã ghi" in capsys.readouterr().out
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert validate_config(cfg)["valid"] is True
    assert next(d for d in cfg["deployments"] if d["id"] == "gemini-main/flash")["limits"]["rpd"] == 900


def test_cli_reports_no_change_when_already_synced(tmp_path, cli, capsys):
    path = tmp_path / "pool_config.json"
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps(_probe(), ensure_ascii=False), encoding="utf-8")
    synced, errors = apply_updates(CFG, plan_updates(CFG, _probe(), today="05/10/2026"))
    assert errors == []
    path.write_text(json.dumps(synced, ensure_ascii=False), encoding="utf-8")
    assert cli("--config", str(path), "--probe", str(probe_path), "--today", "05/10/2026") == 0
    assert "Không có thay đổi nào" in capsys.readouterr().out


def test_cli_refuses_to_write_invalid_result(tmp_path, cli, capsys):
    path = tmp_path / "pool_config.json"
    path.write_text(json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    probe_path = tmp_path / "probe.json"
    probe_path.write_text(json.dumps(_probe(tokenizer_factor=99.0), ensure_ascii=False), encoding="utf-8")
    assert cli("--config", str(path), "--probe", str(probe_path), "--write") == 1
    err = capsys.readouterr().err
    assert "KHÔNG ghi" in err
    assert json.loads(path.read_text(encoding="utf-8"))["models"][0]["tokenizer_factor"] == 1.0  # nguyên vẹn
