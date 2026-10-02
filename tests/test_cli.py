"""CLI của M0: ba lệnh extract / segment / estimate chạy được trên tài liệu mẫu."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visynth.cli import main

FIXTURE = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "demo_lecture.txt"


def test_fixture_exists():
    assert FIXTURE.exists() and FIXTURE.stat().st_size > 500


def test_extract_summary(capsys):
    assert main(["extract", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "demo_lecture" in out and "mục" in out


def test_extract_json(capsys):
    assert main(["extract", str(FIXTURE), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["paragraphs"] and data["sections"]
    assert data["language_code"] == "vi"
    assert data["paragraphs"][0]["pid"] == "P000001"
    assert data["extraction_quality"] > 0.6


def test_segment_json(capsys):
    assert main(["segment", str(FIXTURE), "--mode", "translate", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "translate"
    segs = data["segments"]
    assert segs and all(s["segment_id"].startswith("SEG-") for s in segs)
    pids = [pid for s in segs for pid in s["paragraphs"]]
    assert len(pids) == len(set(pids))  # mỗi đoạn chỉ nằm ở một segment
    assert pids[0] == "P000001"


def test_estimate_json_and_human(capsys):
    assert main(["estimate", str(FIXTURE), "--level", "deep_synthesis", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["credits"] >= 1 and data["cost_usd"] > 0 and data["llm_calls"] >= 1
    assert data["report_budget_words"] >= 250

    assert main(["estimate", str(FIXTURE), "--level", "executive_brief", "--on", "2027-01-01"]) == 0
    out = capsys.readouterr().out
    assert "01/01/2027" in out and "tín dụng" in out


def test_out_writes_file(tmp_path: Path, capsys):
    target = tmp_path / "ket_qua.json"
    assert main(["segment", str(FIXTURE), "--out", str(target)]) == 0
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["segments"]


def test_missing_file_is_reported(capsys):
    rc = main(["extract", "/khong/co/tai/lieu.txt"])
    assert rc == 1
    assert "LỖI" in capsys.readouterr().err


def test_pdf_is_reported_as_unsupported(tmp_path: Path, capsys):
    pdf = tmp_path / "tai_lieu.pdf"
    pdf.write_bytes(b"%PDF-1.7\n% fake pdf\n")
    assert main(["extract", str(pdf)]) == 1
    assert "unsupported_file_type" in capsys.readouterr().err


def test_bad_level_is_rejected():
    with pytest.raises(SystemExit):
        main(["estimate", str(FIXTURE), "--level", "khong-co"])
