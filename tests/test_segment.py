"""Chia segment — bám giả mã SPEC §6.3."""

from __future__ import annotations

import pytest

from visynth.extract.model import Paragraph, pid_for
from visynth.segment import (
    CONTEXT_PARAGRAPHS,
    check_target_fits,
    estimate_tokens,
    render_paragraph,
    segment_paragraphs,
    split_long_paragraph,
)


def para(i: int, chars: int, section: str | None = None, *, prefix: str = "Word ") -> Paragraph:
    text = (prefix * ((chars // len(prefix)) + 1))[:chars]
    return Paragraph(pid=pid_for(i), idx=i, kind="body", content=text, section_id=section)


def test_target_must_fit_smallest_deployment():
    check_target_fits(8000, 100_000)  # 8.000 ≤ 20.000
    with pytest.raises(ValueError):
        check_target_fits(25_000, 100_000)


def test_tokens_estimate_by_language():
    assert estimate_tokens("a" * 400, "en") == 100
    assert estimate_tokens("a" * 280, "vi") == 100
    assert estimate_tokens("", "en") == 1


def test_segments_respect_target_and_min():
    # 20 mục, mỗi mục 1 đoạn ~500 token → gom 4 mục mỗi segment theo target 2000
    paragraphs = [para(i, 2000, f"S{i:02d}") for i in range(1, 21)]
    segs = segment_paragraphs(paragraphs, mode="map", target_tokens=2000, min_tokens=500, lang="en")
    assert len(segs) == 5
    for s in segs:
        assert 500 <= s.token_count <= 2500  # target + 1 đơn vị
    assert all(s.segment_id == f"SEG-{i:03d}" for i, s in enumerate(segs, start=1))
    # không mất đoạn nào
    assert sum(len(s.paragraphs) for s in segs) == len(paragraphs)


def test_unit_under_max_stays_atomic():
    """Đơn vị theo mục là nguyên tử khi ≤ MAX_TOKENS (12.000), dù lớn hơn target (§6.3)."""
    paragraphs = [para(i, 2000, "S01") for i in range(1, 21)]  # 10.000 token, một mục
    segs = segment_paragraphs(paragraphs, mode="map", target_tokens=2000, min_tokens=500, lang="en")
    assert len(segs) == 1 and segs[0].token_count == 10_000


def test_unit_over_max_is_split_at_paragraph_boundaries():
    paragraphs = [para(i, 2000, "S01") for i in range(1, 41)]  # 20.000 token > MAX
    segs = segment_paragraphs(paragraphs, mode="map", target_tokens=2000, min_tokens=500, lang="en")
    assert len(segs) > 1
    assert all(s.token_count <= 12_000 for s in segs)
    assert sum(len(s.paragraphs) for s in segs) == 40


def test_last_small_segment_is_merged():
    paragraphs = [para(i, 2000, "S01") for i in range(1, 13)] + [para(13, 100, "S01")]
    segs = segment_paragraphs(paragraphs, mode="map", target_tokens=2000, min_tokens=400, lang="en")
    assert segs[-1].token_count >= 400  # đoạn nhỏ cuối đã gộp vào segment trước


def test_context_before_is_previous_two_paragraphs_read_only():
    paragraphs = [para(i, 800, "S01") for i in range(1, 31)]
    segs = segment_paragraphs(paragraphs, mode="map", target_tokens=1000, min_tokens=200, lang="en")
    assert segs[0].context_before == []
    for prev, cur in zip(segs, segs[1:], strict=False):
        assert [p.pid for p in cur.context_before] == [p.pid for p in prev.paragraphs[-CONTEXT_PARAGRAPHS:]]
        # ngữ cảnh chỉ để đọc: không nằm trong danh sách đoạn dịch/kê khai của segment
        assert all(p.pid not in {q.pid for q in cur.paragraphs} for p in cur.context_before)


def test_units_by_headings_split_when_over_target():
    paragraphs = [para(i, 800, "S01") for i in range(1, 6)] + [para(i, 800, "S02") for i in range(6, 11)]
    segs = segment_paragraphs(paragraphs, mode="map", target_tokens=1000, min_tokens=1, lang="en")
    assert len(segs) == 2
    assert segs[0].section_id == "S01" and segs[1].section_id == "S02"
    assert {p.section_id for p in segs[1].paragraphs} == {"S02"}


def test_small_sections_are_packed_into_one_segment():
    paragraphs = [para(i, 800, "S01") for i in range(1, 6)] + [para(i, 800, "S02") for i in range(6, 11)]
    segs = segment_paragraphs(paragraphs, mode="map", target_tokens=20_000, min_tokens=1, lang="en")
    assert len(segs) == 1
    assert segs[0].section_id is None  # segment trải qua nhiều mục


def test_long_paragraph_is_split_by_sentences_with_same_pid():
    sentence = "This is a sentence about translation quality. "  # ~48 ký tự ≈ 12 token
    long_text = sentence * 2000  # ~24.000 token > MAX
    p = Paragraph(pid=pid_for(1), idx=1, kind="body", content=long_text, section_id="S01")
    pieces = split_long_paragraph(p, target=4000, lang="en")
    assert len(pieces) > 1
    assert {x.pid for x in pieces} == {p.pid}
    assert [x.part for x in pieces] == list(range(1, len(pieces) + 1))
    assert all(x.kind == "body" and x.section_id == "S01" for x in pieces)
    assert "".join(x.content.replace(" ", "") for x in pieces) == long_text.replace(" ", "")


def test_segment_split_long_paragraph_keeps_content():
    long_text = ("Một câu dài về chất lượng dịch thuật. " * 2000).strip()
    paragraphs = [Paragraph(pid=pid_for(1), idx=1, kind="body", content=long_text, section_id="S01")]
    segs = segment_paragraphs(paragraphs, mode="translate", target_tokens=3000, min_tokens=100, lang="vi")
    joined = "".join(x.content.replace(" ", "") for s in segs for x in s.paragraphs)
    assert joined == long_text.replace(" ", "")
    assert sum(len({x.pid for x in s.paragraphs}) for s in segs) >= 1


def test_translate_default_target_and_mode_validation():
    paragraphs = [para(i, 2000, "S01") for i in range(1, 6)]
    segs = segment_paragraphs(paragraphs, mode="translate", min_tokens=100, lang="en")
    assert segs  # target mặc định 4000 cho translate
    with pytest.raises(ValueError):
        segment_paragraphs(paragraphs, mode="sai")


def test_render_format_matches_prompt_convention():
    p = para(1, 100, "S01")
    assert render_paragraph(p) == f"[{p.pid}] {p.content}"
