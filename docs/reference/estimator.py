"""Ước tính token, chi phí, tín dụng (credit) và thời gian cho một job TRƯỚC khi chạy.

Các hằng số là GIÁ TRỊ KHỞI ĐIỂM, phải hiệu chỉnh bằng số đo thật ở Giai đoạn 0
(so sánh estimate với tổng llm_calls thực tế trên bộ golden set).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

LEVELS = ("full_translation", "detailed_synthesis", "deep_synthesis", "executive_brief")

WORDS_PER_PAGE = 300  # 1 credit = 1 'trang quy đổi' = 300 từ nguồn (hệ số theo mức xem LEVEL_CREDIT_FACTOR)
TOK_PER_SRC_WORD = {"en": 1.35, "vi": 1.6, "default": 1.4}  # token đầu vào / từ nguồn
VI_TOK_PER_SRC_WORD = 1.8  # token tiếng Việt sinh ra / từ nguồn khi dịch (ước lượng)

# input_x: số lần 'đọc' nguồn quy đổi (nhân với T = token nguồn)
# out_fixed_x: token sinh ra không phụ thuộc độ dài báo cáo (JSON kê khai, kế hoạch, kiểm chứng...) tính theo T
# think: hệ số chi phí token suy luận
# Độ dài phần viết tiếng Việt lấy từ report_budget_words() (cùng hàm cấp budget_words cho prompt P3).
LEVEL_PARAMS = {
    "full_translation":   dict(input_x=2.15, out_fixed_x=0.02, think=1.10),
    "detailed_synthesis": dict(input_x=4.60, out_fixed_x=0.62, think=1.30),
    "deep_synthesis":     dict(input_x=4.05, out_fixed_x=0.49, think=1.30),
    "executive_brief":    dict(input_x=3.00, out_fixed_x=0.40, think=1.30),
}
# Hệ số tín dụng ~ tỷ lệ chi phí thật so với mức deep_synthesis (làm tròn 0.05), hiệu chỉnh ở Giai đoạn 0.
LEVEL_CREDIT_FACTOR = {"full_translation": 1.20, "detailed_synthesis": 1.35, "deep_synthesis": 1.00, "executive_brief": 0.70}

# Ngân sách độ dài báo cáo (từ tiếng Việt): (tỷ lệ so với số từ nguồn, tối thiểu, tối đa).
REPORT_BUDGET = {
    "detailed_synthesis": (0.35, 1500, 24000),
    "deep_synthesis":     (0.12, 800, 9000),
    "executive_brief":    (0.03, 250, 1500),
}
SERIAL_MINUTES = {"full_translation": 1.0, "detailed_synthesis": 2.5, "deep_synthesis": 2.5, "executive_brief": 2.0}

GEN_TOK_PER_SEC = 120.0  # tốc độ sinh trên MỘT luồng (bảo thủ)
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
    """Số từ tiếng Việt mục tiêu của báo cáo (đưa vào biến budget_words của prompt P3).

    full_translation: xấp xỉ số từ nguồn. Các mức tổng hợp: tỷ lệ có kẹp [min, max], và không bao giờ dài hơn 80% nguồn.
    """
    if level == "full_translation":
        return int(source_words)
    ratio, lo, hi = REPORT_BUDGET[level]
    cap = max(100, int(0.8 * source_words))
    return int(min(hi, max(lo, round(source_words * ratio)), cap))


def pick_price(prices: list[Price], model: str, on: date) -> Price:
    """Giá có hiệu lực tại ngày `on` (bản ghi effective_from mới nhất không vượt quá `on`)."""
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


# ----------------------------------------------------------------------------- dịch đầy đủ: so sánh phương án bản thô (SPEC §18; phương án ĐÃ LOẠI, giữ lại làm bằng chứng)
TRANSLATION_MODES = ("direct", "postedit_full", "postedit_selective")
WORDS_PER_PARAGRAPH = 60  # trung bình, để ước lượng số dòng 'giữ nguyên' của chế độ chọn lọc
KEEP_MARKER_TOKENS = 14  # token cho mỗi đoạn giữ nguyên bản thô (pid + action) trong đầu ra chọn lọc


@dataclass(frozen=True)
class TranslationEstimate:
    mode: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    llm_calls: int


def estimate_translation(words: int, mode: str, price: Price, lang: str = "en", keep_rate: float = 0.7, segment_tokens: int = 4000) -> TranslationEstimate:
    """So sánh chi phí LLM của dịch trực tiếp (P9) với phương án "bản thô của model dịch máy rồi LLM hiệu đính" (đã loại khỏi phạm vi, SPEC §18).

    - direct: LLM đọc nguồn, viết toàn bộ bản dịch.
    - postedit_full: LLM đọc nguồn + bản thô, VẪN viết toàn bộ bản dịch -> đầu vào tăng, đầu ra không giảm: KHÔNG rẻ hơn dịch trực tiếp.
    - postedit_selective: LLM chỉ viết lại những đoạn cần sửa (1 - keep_rate); đoạn giữ nguyên chỉ tốn vài token đánh dấu.
      Chỉ có lợi nếu keep_rate đủ cao VÀ chất lượng không giảm (điều kiện xem xét lại: SPEC §18.4).
    Chi phí của chính model dịch máy không tính ở đây (miễn phí hoặc tự dựng).
    """
    if mode not in TRANSLATION_MODES:
        raise ValueError(f"mode không hợp lệ: {mode}")
    p = LEVEL_PARAMS["full_translation"]
    t = words * TOK_PER_SRC_WORD.get(lang, TOK_PER_SRC_WORD["default"])
    v = words * VI_TOK_PER_SRC_WORD  # token bản dịch tiếng Việt
    tin = p["input_x"] * t
    tout_full = p["think"] * (p["out_fixed_x"] * t + v)
    if mode == "direct":
        tout = tout_full
    else:
        tin += v  # bản thô được đưa vào prompt
        if mode == "postedit_full":
            tout = tout_full
        else:
            paras = words / WORDS_PER_PARAGRAPH
            tout = p["think"] * (p["out_fixed_x"] * t + v * (1 - keep_rate) + paras * keep_rate * KEEP_MARKER_TOKENS)
    calls = max(1, math.ceil(t / segment_tokens)) * 1.1
    cost = tin / 1e6 * price.input_per_mtok + tout / 1e6 * price.output_per_mtok
    return TranslationEstimate(mode, round(tin), round(tout), round(cost, 4), math.ceil(calls))


# ----------------------------------------------------------------------------- số lời gọi LLM và dung lượng pool (SPEC §17.10)
MAP_SEGMENT_TOKENS = 8000
GLOSSARY_WINDOW_TOKENS = 300_000
WORDS_PER_SECTION = 500


def estimate_calls(words: int, level: str, lang: str = "en", window_scale: float = 1.0, verify_batch: int = 1) -> int:
    """Số lời gọi LLM ước tính của một job (để tính dung lượng pool; thứ khan hiếm của free tier thường là SỐ REQUEST/NGÀY, không phải token).

    Hai đòn bẩy tiết kiệm request/ngày khi dùng free tier (đổi lại mỗi lời gọi lớn hơn, nên chạm TPM/ngữ cảnh sớm hơn):
      window_scale > 1: nới cửa sổ kê khai/dịch/thuật ngữ (ít segment hơn);
      verify_batch > 1: kiểm chứng nhiều mục trong MỘT lời gọi (P5/P6).
    Số lời gọi viết (P4) và sửa (P7) gần như không đổi vì chúng theo số mục của báo cáo.
    """
    t = words * TOK_PER_SRC_WORD.get(lang, TOK_PER_SRC_WORD["default"])
    gloss = max(1, math.ceil(t / (GLOSSARY_WINDOW_TOKENS * window_scale)))
    if level == "full_translation":
        # đầu ra của một segment dịch xấp xỉ 1.5 lần đầu vào nên segment dịch bị chặn ở 8000 token (đầu ra < 20000 token của P9/P11)
        return math.ceil(1 + gloss + 1.1 * math.ceil(t / min(4000 * window_scale, 8000)))
    n_map = math.ceil(t / (MAP_SEGMENT_TOKENS * window_scale))
    n_sec = min(20, max(4, round(report_budget_words(words, level) / WORDS_PER_SECTION)))
    p5 = math.ceil(n_sec / verify_batch)
    p6 = math.ceil(0.5 * n_sec / verify_batch)
    # P0 + P1 + P2(+10% chạy lại) + P3 + P4 + P5 + P6 (một nửa số mục) + P7 (30% mục x 2 vòng, tính 0.6) + P8
    return math.ceil(1 + gloss + 1.1 * n_map + 1 + n_sec + p5 + p6 + 0.6 * n_sec + 1)


def docs_per_day(calls_cap: int | None, tokens_cap: int | None, calls_per_doc: int, tokens_in_per_doc: int) -> float:
    """Số tài liệu/ngày mà dung lượng (lời gọi, token vào) cho phép; None = không có giới hạn đã biết ở chiều đó."""
    caps = []
    if calls_cap is not None:
        caps.append(calls_cap / calls_per_doc)
    if tokens_cap is not None:
        caps.append(tokens_cap / tokens_in_per_doc)
    return min(caps) if caps else float("inf")
