"""Kiểm thử công cụ bake-off (§16.4).

Trọng tâm:

* nạp cấu hình phải **kiểm schema trước khi chạy** (đừng đốt token cho `pool_config` sai) và lấy id theo tên tệp;
* phát hiện đa dạng người viết/người kiểm (§16.4) theo họ model;
* tổng hợp và **chọn cấu hình rẻ nhất đạt ngưỡng §2.2**, kể cả khi có cấu hình hỏng/thiếu tài liệu;
* chạy đầu-cuối ở chế độ khô (`--demo`) sinh `bakeoff.json` + `bakeoff.md`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "pool_config.example.json").read_text(encoding="utf-8"))


def _load():
    spec = importlib.util.spec_from_file_location("visynth_eval_bakeoff", ROOT / "eval" / "bakeoff.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bakeoff = _load()


def _config_on_disk(tmp_path: Path, cfg: dict, name: str) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _variant(*, verifier_group_tier: str) -> dict:
    """Bản sao `pool_config` mẫu với tầng người kiểm đổi sang nhóm khác (§16.4: thử verifier khác họ)."""
    cfg = json.loads(json.dumps(CFG))
    for profile in cfg["profiles"]:
        if profile["name"] == "verifier":
            profile["tiers"] = [
                {
                    "name": "probe",
                    "select": {"tags": [], "group_tiers": [verifier_group_tier]},
                    "strategy": "weighted",
                    "max_wait_s": 120,
                }
            ]
    return cfg


# ------------------------------------------------------------------ nạp cấu hình


def test_load_configs_derives_id_from_filename_and_reads_probe(tmp_path):
    path = _config_on_disk(tmp_path, CFG, "pool_config.gemini-only.json")
    (tmp_path / "pool_config.gemini-only.probe.json").write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "deployment_id": "gemini-main/flash",
                        "json": {"ok": True},
                        "vi_write": {"ok": True, "words": 61},
                        "tokenizer_factor": 1.42,
                        "latency_p50_ms": 900,
                        "errors": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    entry = bakeoff.load_configs([str(path)])[0]
    assert entry["id"] == "gemini-only"
    digest = bakeoff.probe_digest(entry["probe"])
    assert digest["available"] is True
    assert digest["deployments"]["gemini-main/flash"]["tokenizer_factor"] == 1.42


def test_load_configs_rejects_invalid_config_before_spending_money(tmp_path):
    cfg = json.loads(json.dumps(CFG))
    cfg["profiles"][0]["tiers"][0]["max_wait_s"] = -5  # vi phạm schema
    path = _config_on_disk(tmp_path, cfg, "pool_config.bad.json")
    with pytest.raises(ValueError) as excinfo:
        bakeoff.load_configs([str(path)])
    assert "KHÔNG hợp lệ" in str(excinfo.value)
    # strict=False: giữ lại để báo cáo thấy, không chạy
    entry = bakeoff.load_configs([str(path)], strict=False)[0]
    assert entry["invalid"] and "bad" in entry["id"]


# ------------------------------------------------------------------ họ model và đa dạng người kiểm


def test_model_family_groups_by_provider_and_name_head():
    assert bakeoff.model_family("gemini:gemini-3.8-flash") == "gemini/gemini"
    assert bakeoff.model_family("nvidia:meta/llama-3.3-70b-instruct") == "nvidia/meta"
    assert bakeoff.model_family("gemini:gemini-3.1-flash-lite") == "gemini/gemini"


def test_diversity_note_flags_same_family_and_accepts_different_family():
    same = bakeoff.diversity_note(CFG)
    assert same["diverse"] is False
    assert "CÙNG họ" in same["note"]

    other = bakeoff.diversity_note(_variant(verifier_group_tier="trial"))
    assert other["diverse"] is True
    assert other["overlap"] == []
    assert "KHÁC họ" in other["note"]


# ------------------------------------------------------------------ tổng hợp và chọn


def _row(config: str, doc: str, *, coverage=1.0, faithfulness=1.0, traps=0, cost=0.1, seconds=1.0, status="ok"):
    row = {
        "config": config,
        "doc": doc,
        "status": status,
        "grade": "A",
        "verdict": "pass",
        "blocking": [],
        "seconds": seconds,
    }
    if status == "ok":
        row["metrics"] = {
            "coverage_core": coverage,
            "faithfulness_rate": faithfulness,
            "trap_fact_errors": traps,
            "fabricated_remaining": 0,
            "cost_usd": cost,
            "length_ratio": 1.0,
        }
    else:
        row["error"] = "bom"
    return row


def _entries(tmp_path: Path, *names: str) -> list[dict]:
    return [
        bakeoff.load_configs([str(_config_on_disk(tmp_path, _variant(verifier_group_tier="paid"), f"{n}.json"))])[0]
        for n in names
    ]


def test_summarize_picks_cheapest_passing_config(tmp_path):
    configs = _entries(tmp_path, "dat-tot", "dat-re", "hong")
    docs = [{"doc_id": "d1", "meta": {"level": "deep_synthesis"}}]
    rows = [
        _row("dat-tot", "d1", cost=0.30),
        _row("dat-re", "d1", cost=0.10),
        _row("hong", "d1", status="error"),
    ]
    report = bakeoff.summarize(rows, configs, docs, demo=False)
    assert set(report["passing"]) == {"dat-tot", "dat-re"}  # 'hong' lỗi nên không đạt
    assert report["winner"] == "dat-re"  # rẻ hơn: $0.10 so với $0.30
    assert report["configs"]["hong"]["docs_failed"] == ["d1"]
    assert report["configs"]["dat-re"]["total_cost_usd"] == 0.1


def test_summarize_blocks_config_that_misses_thresholds(tmp_path):
    configs = _entries(tmp_path, "thap")
    docs = [{"doc_id": "d1", "meta": {"level": "deep_synthesis"}}]
    rows = [_row("thap", "d1", coverage=0.80, faithfulness=0.90, traps=1)]
    report = bakeoff.summarize(rows, configs, docs, demo=False)
    assert report["winner"] is None
    assert "KHÔNG cấu hình nào đạt" in report["verdict"]
    assert report["configs"]["thap"]["passes"] is False
    assert bakeoff.to_markdown(report).count("❌") == 1


def test_summarize_requires_every_document_to_have_a_result(tmp_path):
    configs = _entries(tmp_path, "thieu-tai-lieu")
    docs = [{"doc_id": "d1", "meta": {}}, {"doc_id": "d2", "meta": {}}]
    rows = [_row("thieu-tai-lieu", "d1")]
    report = bakeoff.summarize(rows, configs, docs, demo=False)
    assert report["configs"]["thieu-tai-lieu"]["passes"] is False  # chưa chạy đủ tài liệu thì chưa kết luận
    assert report["winner"] is None


# ------------------------------------------------------------------ chạy đầu-cuối (chế độ khô)


def test_run_one_demo_writes_artifact_and_score(tmp_path):
    doc = {
        "doc_id": "demo_lecture",
        "meta": json.loads((ROOT / "eval" / "golden" / "demo_lecture" / "meta.json").read_text(encoding="utf-8")),
        "source": (ROOT / "eval" / "fixtures" / "demo_lecture.txt").resolve(),
    }
    entry = bakeoff.load_configs([str(ROOT / "pool_config.example.json")])[0]
    row = bakeoff.run_one(entry, doc, tmp_path / "out", demo=True, seed=7, gate="dev", privacy="standard")
    assert row["status"] == "ok"
    assert row["verdict"] == "pass" and row["metrics"]["coverage_core"] >= 0.9
    assert (tmp_path / "out" / "run.json").exists()
    assert (tmp_path / "out" / "score.json").exists()


def test_run_one_records_error_instead_of_raising(tmp_path):
    doc = {"doc_id": "thieu", "meta": {"level": "deep_synthesis"}, "source": tmp_path / "khong-ton-tai.txt"}
    entry = bakeoff.load_configs([str(ROOT / "pool_config.example.json")])[0]
    row = bakeoff.run_one(entry, doc, tmp_path / "out", demo=True, seed=7, gate="dev", privacy="standard")
    assert row["status"] == "error"
    assert "FileNotFoundError" in row["error"] or "không tìm thấy" in row["error"].lower()


def test_cli_demo_run_writes_report_and_exits_zero(tmp_path, capsys):
    a = _config_on_disk(tmp_path, CFG, "pool_config.a.json")
    b = _config_on_disk(tmp_path, _variant(verifier_group_tier="trial"), "pool_config.b.json")
    out = tmp_path / "bakeoff"
    code = bakeoff.main(
        [
            "--configs",
            str(a),
            str(b),
            "--golden",
            str(ROOT / "eval" / "golden"),
            "--demo",
            "--limit-docs",
            "1",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    report = json.loads((out / "bakeoff.json").read_text(encoding="utf-8"))
    assert report["winner"] in {"a", "b"}
    assert report["verdict"].startswith("chọn được")
    assert report["configs"]["b"]["diversity"]["diverse"] is True
    assert (out / "bakeoff.md").read_text(encoding="utf-8").startswith("# Bake-off")
    assert "Bake-off" in capsys.readouterr().out


def test_cli_fails_clearly_on_invalid_config(tmp_path, capsys):
    bad = json.loads(json.dumps(CFG))
    bad["providers"][0].pop("base_url")
    path = _config_on_disk(tmp_path, bad, "pool_config.bad.json")
    code = bakeoff.main(
        ["--configs", str(path), "--golden", str(ROOT / "eval" / "golden"), "--demo", "--out", str(tmp_path / "o")]
    )
    assert code == 2
    assert "KHÔNG hợp lệ" in capsys.readouterr().err
