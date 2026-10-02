"""Ước tính token, chi phí, tín dụng và thời gian — bản port trung thành của `docs/reference/estimator.py`.

Bộ đặc tả là nguồn sự thật: `tests/test_spec_conformance.py` đối chiếu hai bản trên cùng đầu vào,
nên mọi thay đổi công thức phải sửa ở CẢ HAI nơi (và chạy lại `docs/tools/build_spec.py`).

Các hằng số là GIÁ TRỊ KHỞI ĐIỂM, hiệu chỉnh bằng số đo thật ở M0-W4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

LEVELS = ("full_translation", "detailed_synthesis", "deep_synthesis", "executive_brief")

WORDS_PER_PAGE = 300  # 1 credit = 300 từ nguồn (hệ số theo mức: LEVEL_CREDIT_FACTOR)
TOK_PER_SRC_WORD = {"en": 1.35, "vi": 1.6, "default": 1.4}
VI_TOK_PER_SRC_WORD = 1.8

LEVEL_PARAMS = {
    "full_translation": {"input_x": 2.15, "out_fixed_x": 0.02, "think": 1.10},
    "detailed_synthesis": {"input_x": 4.60, "out_fixed_x": 0.62, "think": 1.30},
    "deep_synthesis": {"input_x": 4.05, "out_fixed_x": 0.49, "think": 1.30},
    "executive_brief": {"input_x": 3.00, "out_fixed_x": 0.40, "think": 1.30},
}
LEVEL_CREDIT_FACTOR = {
    "full_translation": 1.20,
    "detailed_synthesis": 1.35,
    "deep_synthesis": 1.00,
    "executive_brief": 0.70,
}

REPORT_BUDGET = {
    "detailed_synthesis": (0.35, 1500, 24000),
    "deep_synthesis": (0.12, 800, 9000),
    "executive_brief": (0.03, 250, 1500),
}
SERIAL_MINUTES = {"full_translation": 1.0, "detailed_synthesis": 2.5, "deep_synthesis": 2.5, "executive_brief": 2.0}

GEN_TOK_PER_SEC = 120.0
CONCURRENCY = 6


@dataclass(frozen=True)
class Price:
    model: str
    effective_from: date
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float = 0.0


@dataclass(frozen=True)
class Estimate:
    tokens_in: int
    tokens_out: int
    cost_usd: float
    credits: int
    minutes_low: float
    minutes_high: float


def report_budget_words(source_words: int, level: str) -> int:
    """Số từ tiếng Việt mục tiêu của báo cáo (đưa vào biến `budget_words` của P3)."""
    if level == "full_translation":
        return int(source_words)
    ratio, lo, hi = REPORT_BUDGET[level]
    cap = max(100, int(0.8 * source_words))
    return int(min(hi, max(lo, round(source_words * ratio)), cap))


def pick_price(prices: list[Price] | tuple[Price, ...], model: str, on: date) -> Price:
    """Giá có hiệu lực tại ngày `on` (bản ghi `effective_from` mới nhất không vượt quá `on`)."""
    rows = sorted((p for p in prices if p.model == model and p.effective_from <= on), key=lambda p: p.effective_from)
    if not rows:
        raise LookupError(f"không có giá cho {model} tại {on}")
    return rows[-1]


def estimate(words: int, level: str, price: Price, lang: str = "en") -> Estimate:
    if level not in LEVEL_PARAMS:
        raise ValueError(f"level không hợp lệ: {level}")
    p = LEVEL_PARAMS[level]
    t = words * TOK_PER_SRC_WORD.get(lang, TOK_PER_SRC_WORD["default"])
    tokens_in = p["input_x"] * t
    write_words = report_budget_words(words, level)
    tokens_out = p["think"] * (p["out_fixed_x"] * t + write_words * VI_TOK_PER_SRC_WORD)
    cost = tokens_in / 1e6 * price.input_per_mtok + tokens_out / 1e6 * price.output_per_mtok
    credits = max(1, math.ceil(words / WORDS_PER_PAGE * LEVEL_CREDIT_FACTOR[level]))
    base_min = SERIAL_MINUTES[level] + 2.0 * tokens_out / (GEN_TOK_PER_SEC * CONCURRENCY * 60.0)
    return Estimate(
        tokens_in=round(tokens_in),
        tokens_out=round(tokens_out),
        cost_usd=round(cost, 4),
        credits=credits,
        minutes_low=round(0.7 * base_min, 1),
        minutes_high=round(1.8 * base_min, 1),
    )


# ------------------------------------------------------------------ số lời gọi LLM và dung lượng pool (§17.10)

MAP_SEGMENT_TOKENS = 8000
GLOSSARY_WINDOW_TOKENS = 300_000
WORDS_PER_SECTION = 500


def estimate_calls(words: int, level: str, lang: str = "en", window_scale: float = 1.0, verify_batch: int = 1) -> int:
    """Số lời gọi LLM ước tính của một job (để tính dung lượng pool, §17.10)."""
    t = words * TOK_PER_SRC_WORD.get(lang, TOK_PER_SRC_WORD["default"])
    gloss = max(1, math.ceil(t / (GLOSSARY_WINDOW_TOKENS * window_scale)))
    if level == "full_translation":
        return math.ceil(1 + gloss + 1.1 * math.ceil(t / min(4000 * window_scale, 8000)))
    n_map = math.ceil(t / (MAP_SEGMENT_TOKENS * window_scale))
    n_sec = min(20, max(4, round(report_budget_words(words, level) / WORDS_PER_SECTION)))
    p5 = math.ceil(n_sec / verify_batch)
    p6 = math.ceil(0.5 * n_sec / verify_batch)
    return math.ceil(1 + gloss + 1.1 * n_map + 1 + n_sec + p5 + p6 + 0.6 * n_sec + 1)


def docs_per_day(calls_cap: int | None, tokens_cap: int | None, calls_per_doc: int, tokens_in_per_doc: int) -> float:
    """Số tài liệu/ngày dung lượng cho phép; `None` = không giới hạn ở chiều đó."""
    caps = []
    if calls_cap is not None:
        caps.append(calls_cap / calls_per_doc)
    if tokens_cap is not None:
        caps.append(tokens_cap / tokens_in_per_doc)
    return min(caps) if caps else float("inf")


#: Bảng giá khởi điểm (Gemini 3.8 Flash, USD/1M token). Bảng thật nằm ở `db/schema.sql` (`llm_prices`).
STARTER_PRICES = (
    Price("gemini-3.8-flash", date(2026, 9, 2), 0.75, 3.75, 0.075),
    Price("gemini-3.8-flash", date(2027, 1, 1), 1.50, 7.50, 0.15),
)
