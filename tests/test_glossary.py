"""Glossary chuẩn (SPEC §19.5): P13 hài hoà, bằng chứng do code ghép, hàng đợi duyệt, phát hành bất biến."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from visynth import cli as cli_mod
from visynth.checks.merge import merge_candidates
from visynth.glossary import GlossaryError, GlossaryStore, harmonise, term_key
from visynth.llm.fake import FakeLLMClient, FakeReply

PER_DOC = {
    "doc_a": [
        {
            "source_term": "Solar Plexus",
            "target_term": "Trung tâm Thái dương",
            "term_type": "concept",
            "occurrences": 4,
            "confidence": 0.8,
            "keep_original": True,
            "alternatives": ["luân xa mặt trời"],
        },
        {
            "source_term": "Gate 55",
            "target_term": "Cổng 55",
            "term_type": "concept",
            "occurrences": 2,
            "confidence": 0.7,
            "keep_original": False,
            "alternatives": [],
        },
    ],
    "doc_b": [
        {
            "source_term": "Solar Plexus",
            "target_term": "Luân xa mặt trời",
            "term_type": "concept",
            "occurrences": 2,
            "confidence": 0.5,
            "keep_original": False,
            "alternatives": [],
        },
    ],
}
CONTEXTS = {("doc_a", "Solar Plexus"): ["The Solar Plexus is the center of emotional awareness."]}

ENTRY = {
    "source_term": "Solar Plexus",
    "target_term": "Trung tâm Thái dương",
    "keep_original": True,
    "case_sensitive": False,
    "forbidden_variants": ["Luân xa mặt trời"],
    "term_type": "concept",
    "note": "ghi chú",
    "confidence": 0.8,
    "needs_human": False,
    "question_vi": None,
    "evidence": [{"doc_ref": "doc_a", "snippet": "The Solar Plexus is the center"}],
}


def _store(tmp_path: Path, name: str = "g.json") -> GlossaryStore:
    return GlossaryStore(path=tmp_path / name)


# ------------------------------------------------------------------ khoá và đề xuất


def test_term_key_normalises_case_whitespace_and_unicode():
    assert term_key("  Solar   Plexus ") == term_key("solar plexus")
    assert term_key("Cổng 55") == term_key("CỔNG 55")


def test_suggest_skips_confirmed_and_rejected_and_merges_suggested(tmp_path):
    store = _store(tmp_path)
    assert store.suggest(dict(ENTRY)) == "added"
    assert store.suggest({**ENTRY, "evidence": [{"doc_ref": "doc_b", "snippet": "khác"}]}) == "merged"
    assert len(store.entry("solar plexus")["evidence"]) == 2

    store.approve("Solar Plexus", by="curator")
    assert store.suggest(dict(ENTRY)) == "skipped_confirmed"

    store.suggest(dict(ENTRY, source_term="Gate 55", target_term="Cổng 55"))
    store.reject("Gate 55", by="curator", reason="không cần cố định")
    assert store.suggest(dict(ENTRY, source_term="gate 55", target_term="Cổng 55")) == "skipped_rejected"
    assert store.data["entries"][term_key("Gate 55")]["status"] == "rejected"  # vẫn giữ để không đề xuất lại


def test_approve_can_fix_the_target_and_reject_keeps_the_reason(tmp_path):
    store = _store(tmp_path)
    store.suggest(dict(ENTRY, needs_human=True, question_vi="chọn cách nào?"))
    entry = store.approve("Solar Plexus", by="curator", target_term="Trung tâm Thái dương (Solar Plexus)")
    assert entry["target_term"] == "Trung tâm Thái dương (Solar Plexus)"
    assert entry["reviewed_by"] == "curator" and entry["needs_human"] is False and entry["question_vi"] is None

    store.suggest({**ENTRY, "source_term": "Gate 55", "target_term": "Cổng 55"})
    assert "không cần cố định" in store.reject("Gate 55", by="curator", reason="không cần cố định")["note"]
    with pytest.raises(GlossaryError, match="đã bị từ chối"):
        store.approve("Gate 55", by="curator")


def test_approve_requires_a_reviewer(tmp_path):
    store = _store(tmp_path)
    store.suggest(dict(ENTRY))
    with pytest.raises(GlossaryError, match="phải có người duyệt"):
        store.approve("Solar Plexus", by="")
    with pytest.raises(GlossaryError, match="không có mục"):
        store.approve("không tồn tại", by="curator")


# ------------------------------------------------------------------ phát hành


def test_publish_only_confirmed_and_is_immutable(tmp_path):
    store = _store(tmp_path)
    store.suggest(dict(ENTRY))
    store.suggest({**ENTRY, "source_term": "Gate 55", "target_term": "Cổng 55"})
    store.approve("Solar Plexus", by="curator")
    store.reject("Gate 55", by="curator", reason="bỏ")

    release = store.publish(by="curator", summary="v1")
    assert release["release"] == "v1" and release["count"] == 1
    assert [e["source_term"] for e in release["entries"]] == ["Solar Plexus"]
    assert store.entry("Solar Plexus")["released"] == ["v1"]
    assert "released" not in release["entries"][0]

    # ảnh chụp không đổi dù mục gốc có đổi sau đó
    store.entry("Solar Plexus")["target_term"] = "Đổi sau khi phát hành"
    assert store.release("v1")["entries"][0]["target_term"] == "Trung tâm Thái dương"
    store.entry("Solar Plexus")["target_term"] = "Trung tâm Thái dương"

    store.suggest({**ENTRY, "source_term": "Gate 49", "target_term": "Cổng 49"})
    store.approve("Gate 49", by="curator")
    second = store.publish(by="curator")
    assert second["release"] == "v2" and store.diff("v2") == {"added": ["Gate 49"], "removed": [], "changed": []}
    assert store.diff("v1")["added"] == ["Solar Plexus"]


def test_publish_needs_confirmed_entries(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(GlossaryError, match="không có mục"):
        store.publish(by="curator")
    store.suggest(dict(ENTRY))
    with pytest.raises(GlossaryError, match="không có mục"):
        store.publish(by="curator")


def test_store_round_trips_and_keeps_needs_human_first(tmp_path):
    store = _store(tmp_path)
    store.suggest(dict(ENTRY, needs_human=True))
    store.suggest({**ENTRY, "source_term": "Gate 55", "target_term": "Cổng 55"})
    path = store.save()
    rows = GlossaryStore.load(path).rows()
    assert rows[0]["source_term"] == "Solar Plexus" and rows[0]["needs_human"] is True
    assert not list(tmp_path.glob(".glossary.tmp.*"))


# ------------------------------------------------------------------ P13: bằng chứng do code ghép


def test_harmonise_attaches_real_snippets_and_drops_bogus_items():
    real_ctx = next(
        c["ctx_id"] for m in merge_candidates(PER_DOC, CONTEXTS) if m["source_term"] == "Solar Plexus" for c in m["ctx"]
    )
    reply = {
        "entries": [
            {**{k: ENTRY[k] for k in ENTRY if k != "evidence"}, "ctx_ids": [real_ctx, "C999.9"]},
            {**{k: ENTRY[k] for k in ENTRY if k != "evidence"}, "source_term": "Thuật ngữ bịa", "ctx_ids": []},
        ],
        "dropped": [{"source_term": "gì đó", "reason_vi": "từ thường"}],
    }
    client = FakeLLMClient(replies={"P13": [FakeReply.json(reply)]})
    entries, dropped, merged = harmonise(client, per_doc=PER_DOC, contexts=CONTEXTS, max_entries=10)

    assert [m["source_term"] for m in merged] == ["Solar Plexus", "Gate 55"]
    assert len(entries) == 1  # thuật ngữ không có trong danh sách gộp bị loại
    assert entries[0]["evidence"] == [
        {"doc_ref": "doc_a", "snippet": "The Solar Plexus is the center of emotional awareness."}
    ]
    assert any("không có trong danh sách gộp" in d["reason_vi"] for d in dropped)
    assert client.calls[0].prompt_id == "P13"


def test_harmonise_without_client_still_produces_schema_shaped_entries():
    entries, _dropped, merged = harmonise(None, per_doc=PER_DOC, contexts=CONTEXTS, max_entries=10)
    assert len(entries) == len(merged) == 2
    conflict = next(e for e in entries if e["source_term"] == "Solar Plexus")
    assert conflict["needs_human"] is True and conflict["question_vi"]
    assert {v for v in conflict["forbidden_variants"]}
    assert conflict["evidence"]


# ------------------------------------------------------------------ CLI đầu-cuối


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setenv("VISYNTH_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "a.json").write_text(json.dumps({"doc_ref": "doc_a", "candidates": PER_DOC["doc_a"]}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"doc_ref": "doc_b", "candidates": PER_DOC["doc_b"]}), encoding="utf-8")
    (tmp_path / "ctx.json").write_text(
        json.dumps({"doc_a|Solar Plexus": CONTEXTS[("doc_a", "Solar Plexus")]}), encoding="utf-8"
    )
    return tmp_path


def _cli(*argv: str) -> int:
    return cli_mod.main(list(argv))


def test_cli_glossary_lifecycle(files, capsys):
    assert (
        _cli(
            "glossary",
            "propose",
            "--candidates",
            str(files / "a.json"),
            str(files / "b.json"),
            "--contexts",
            str(files / "ctx.json"),
            "--demo",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "3 đề xuất" in out or "→" in out

    assert _cli("glossary", "status", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["status"] for r in rows} == {"suggested"}
    assert rows[0]["needs_human"] is True  # xung đột lên đầu hàng đợi

    assert _cli("glossary", "approve", "Solar Plexus", "--target", "Trung tâm Thái dương", "--by", "curator") == 0
    assert _cli("glossary", "reject", "Gate 55", "--by", "curator", "--reason", "từ thường") == 0
    assert _cli("glossary", "publish", "--by", "curator", "--summary", "v1") == 0
    assert "v1" in capsys.readouterr().out

    assert _cli("glossary", "status", "--json") == 0
    rows = {r["source_term"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["Solar Plexus"]["released"] == ["v1"]
    assert rows["Gate 55"]["status"] == "rejected"


def test_cli_glossary_errors_are_clean(files, capsys):
    assert _cli("glossary", "approve", "không có", "--by", "curator") == 1
    assert capsys.readouterr().err.startswith("LỖI:")
    assert _cli("glossary", "publish", "--by", "curator") == 1
    assert "không có mục" in capsys.readouterr().err


def test_cli_glossary_dry_run_does_not_write(files, capsys):
    assert _cli("glossary", "propose", "--candidates", str(files / "a.json"), "--demo", "--dry-run", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["proposed"] >= 1 and "store" not in payload
    assert not (files / "state" / "glossary.json").exists()


def test_cli_glossary_propose_reports_p13_failure(files, capsys):
    bad = files / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    assert _cli("glossary", "propose", "--candidates", str(bad), "--demo", "--json") == 0
    capsys.readouterr()
    assert _cli("glossary", "propose", "--candidates", str(files / "khong-co.json"), "--demo") == 1
    err = capsys.readouterr().err
    assert err.startswith("LỖI:") and "khong-co.json" in err  # lỗi gọn, không traceback
