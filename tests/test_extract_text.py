"""Bóc tách TXT/Markdown — SPEC §6.1."""

from __future__ import annotations

import unicodedata

from visynth.extract import PARAGRAPH_KINDS, extract_text, parse_blocks
from visynth.extract.text import normalize_text

SAMPLE = """# Tiêu đề chính

Đoạn mở đầu bằng tiếng Việt có dấu.

## Mục một

Nội dung mục một.

- ý thứ nhất
- ý thứ hai

| Cột A | Cột B |
|---|---|
| 1 | 2 |

```python
x = 1
```

> Câu trích dẫn

### Mục con

Đoạn kết.
"""


def test_parse_blocks_kinds_in_order():
    kinds = [b.kind for b in parse_blocks(SAMPLE)]
    assert kinds == [
        "heading",
        "body",
        "heading",
        "body",
        "list_item",
        "list_item",
        "table",
        "code",
        "quote",
        "heading",
        "body",
    ]


def test_heading_levels_from_hashes():
    levels = [b.level for b in parse_blocks(SAMPLE) if b.kind == "heading"]
    assert levels == [1, 2, 3]


def test_pids_are_stable_and_well_formed():
    ext = extract_text(SAMPLE, title="Bài giảng")
    assert [p.pid for p in ext.paragraphs] == [f"P{i:06d}" for i in range(1, 12)]
    assert [p.idx for p in ext.paragraphs] == list(range(1, 12))
    assert all(p.pid for p in ext.paragraphs)


def test_sections_tree_and_ranges():
    ext = extract_text(SAMPLE, title="Bài giảng")
    by_id = {s.section_id: s for s in ext.sections}
    assert list(by_id) == ["S01", "S02", "S03"]
    assert by_id["S01"].parent_section_id is None
    assert by_id["S02"].parent_section_id == "S01"
    assert by_id["S03"].parent_section_id == "S02"
    assert by_id["S01"].title == "Tiêu đề chính"
    assert by_id["S01"].first_pid == "P000001" and by_id["S01"].last_pid == "P000002"
    assert by_id["S02"].first_pid == "P000003" and by_id["S02"].last_pid == "P000009"
    assert by_id["S03"].level == 3


def test_paragraphs_carry_section_id():
    ext = extract_text(SAMPLE, title="x")
    assert ext.by_pid("P000004").section_id == "S02"
    assert ext.by_pid("P000011").section_id == "S03"


def test_all_kinds_are_in_contract_enum():
    ext = extract_text(SAMPLE, title="x")
    assert {p.kind for p in ext.paragraphs} <= set(PARAGRAPH_KINDS)


def test_normalize_nfc_newlines_and_control_chars():
    raw = "A\r\n\r\nB\u0000\u200b C"
    assert normalize_text(raw) == "A\n\nB C"
    decomposed = unicodedata.normalize("NFD", "Việt Nam")
    assert normalize_text(decomposed) == "Việt Nam"


def test_extract_from_bytes_with_bom():
    raw = "\ufeff# Tiêu đề\n\nNội dung.".encode("utf-8-sig")
    ext = extract_text(raw, title="Có BOM")
    assert ext.paragraphs[0].content == "Tiêu đề"
    assert "\ufeff" not in ext.full_text()


def test_extract_detects_vietnamese_and_counts_words():
    ext = extract_text(SAMPLE, title="x")
    assert ext.language_code == "vi"
    assert ext.word_count == sum(len(p.content.split()) for p in ext.paragraphs)
    assert ext.word_count > 40
    assert ext.token_estimate > 0


def test_empty_and_whitespace_input():
    ext = extract_text("   \n\n  \n", title="Rỗng")
    assert ext.paragraphs == []
    assert ext.sections == []
    assert ext.word_count == 0


def test_summary_is_readable():
    ext = extract_text(SAMPLE, title="Bài giảng")
    out = ext.summary()
    assert "Bài giảng" in out and "mục" in out and "token" in out
