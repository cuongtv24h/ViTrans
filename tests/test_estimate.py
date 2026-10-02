"""Ước tính tín dụng/chi phí/thời gian — các bất biến của SPEC §13.2 và §21.2."""

from __future__ import annotations

from datetime import date

import pytest

from visynth.estimate import (
    LEVELS,
    STARTER_PRICES,
    docs_per_day,
    estimate,
    estimate_calls,
    pick_price,
    report_budget_words,
)


def test_pick_price_is_effective_dated():
    assert pick_price(STARTER_PRICES, "gemini-3.8-flash", date(2026, 12, 31)).input_per_mtok == 0.75
    assert pick_price(STARTER_PRICES, "gemini-3.8-flash", date(2027, 1, 1)).input_per_mtok == 1.50
    with pytest.raises(LookupError):
        pick_price(STARTER_PRICES, "khong-co-model", date(2026, 10, 2))


def test_report_budget_bounds_and_cap():
    assert report_budget_words(90_000, "deep_synthesis") == 9000  # kẹp trần
    assert report_budget_words(90_000, "executive_brief") == 1500
    assert report_budget_words(1000, "deep_synthesis") == 800  # kẹp sàn
    assert report_budget_words(90_000, "full_translation") == 90_000
    # không bao giờ dài quá 80% nguồn (§7.2) — với nguồn nhỏ, trần 80% thấp hơn sàn của mức
    assert report_budget_words(1000, "detailed_synthesis") == 800


def test_credits_are_pages_times_level_factor():
    assert estimate(300, "deep_synthesis", STARTER_PRICES[0]).credits == 1
    assert estimate(90_000, "deep_synthesis", STARTER_PRICES[0]).credits == 300
    assert estimate(90_000, "detailed_synthesis", STARTER_PRICES[0]).credits == 405


def test_cost_and_time_are_sane_for_300_pages():
    est = estimate(90_000, "deep_synthesis", STARTER_PRICES[0])
    assert 0.5 < est.cost_usd < 1.5  # ngưỡng MVP §2.2: ≤ 1.5 USD
    assert 0 < est.minutes_low < est.minutes_high
    assert est.tokens_in > 0 and est.tokens_out > 0


def test_deep_synthesis_is_cheaper_than_detailed():
    deep = estimate(90_000, "deep_synthesis", STARTER_PRICES[0]).cost_usd
    detailed = estimate(90_000, "detailed_synthesis", STARTER_PRICES[0]).cost_usd
    assert deep < detailed


def test_all_levels_estimate():
    for level in LEVELS:
        est = estimate(50_000, level, STARTER_PRICES[0], lang="en")
        assert est.credits >= 1 and est.cost_usd > 0
    with pytest.raises(ValueError):
        estimate(1000, "khong-co-muc", STARTER_PRICES[0])


def test_calls_decreases_when_windows_widen():
    a = estimate_calls(90_000, "deep_synthesis")
    assert estimate_calls(90_000, "deep_synthesis", window_scale=4) < a
    assert estimate_calls(90_000, "full_translation") > 25
    assert estimate_calls(90_000, "executive_brief") < a


def test_docs_per_day_takes_the_binding_constraint():
    assert docs_per_day(237, None, 77, 500_000) == pytest.approx(237 / 77)
    assert docs_per_day(None, 9_000_000, 77, 500_000) == pytest.approx(18)
    assert docs_per_day(237, 9_000_000, 77, 500_000) == pytest.approx(237 / 77)
    assert docs_per_day(None, None, 77, 1) == float("inf")
