"""Kiểm tra trích đoạn bằng chứng có THẬT SỰ nằm trong đoạn nguồn (chốt chặn chống bịa)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .textnorm import norm_for_match, norm_loose

try:  # rapidfuzz nhanh; không có thì dùng difflib (chậm hơn, chỉ để chạy được)
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None

MIN_FUZZY_LEN = 25  # trích đoạn ngắn hơn ngưỡng này bắt buộc khớp chính xác (tránh khớp mờ sai)
_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class QuoteMatch:
    status: str  # exact | loose | fuzzy | missing | unknown_pid
    score: float  # 0..100


def _partial_ratio(a: str, b: str) -> float:
    if fuzz is not None:
        return float(fuzz.partial_ratio(a, b))
    import difflib  # pragma: no cover

    if len(a) > len(b):
        a, b = b, a
    best, n = 0.0, len(a)
    for i in range(max(1, len(b) - n + 1)):
        best = max(best, difflib.SequenceMatcher(None, a, b[i : i + n]).ratio())
        if best >= 1.0:
            break
    return best * 100.0


def verify_quote(quote: str, paragraph: str, *, allow_fuzzy: bool = False, fuzzy_threshold: float = 95.0) -> QuoteMatch:
    """Trả về mức khớp của `quote` với `paragraph`.

    exact  : khớp sau chuẩn hoá (NFC, dấu nháy, khoảng trắng, hoa/thường)
    loose  : khớp khi chỉ xét chữ+số (khác dấu câu/gạch nối)
    fuzzy  : CHỈ khi allow_fuzzy=True (tài liệu OCR); gần giống >= ngưỡng, đủ dài, và KHÔNG có chữ số lạ
    missing: không khớp -> bằng chứng KHÔNG hợp lệ

    Vì sao fuzzy mặc định tắt và có 'chốt chặn chữ số': khớp mờ coi 'begins in 2031' ~ 'begins in 2027' là
    giống 97%, tức là chấp nhận đúng loại bịa nguy hiểm nhất (sai con số). Test test_fabricated_digit_* bảo vệ điều này.
    """
    nq, np_ = norm_for_match(quote), norm_for_match(paragraph)
    if not nq:
        return QuoteMatch("missing", 0.0)
    if nq in np_:
        return QuoteMatch("exact", 100.0)
    lq, lp = norm_loose(quote), norm_loose(paragraph)
    if lq and lq in lp:
        return QuoteMatch("loose", 100.0)
    if allow_fuzzy and len(lq) >= MIN_FUZZY_LEN:
        if set(_DIGITS.findall(quote)) - set(_DIGITS.findall(paragraph)):
            return QuoteMatch("missing", 0.0)  # có chữ số không tồn tại trong đoạn nguồn -> nghi bịa
        score = _partial_ratio(lq, lp)
        if score >= fuzzy_threshold:
            return QuoteMatch("fuzzy", score)
        return QuoteMatch("missing", score)
    return QuoteMatch("missing", 0.0)


def verify_evidence(evidence: list[dict], paragraphs: dict[str, str], *, allow_fuzzy: bool = False) -> list[QuoteMatch]:
    """Kiểm tra danh sách {pid, quote} so với {pid: text}. allow_fuzzy=True chỉ cho tài liệu có OCR nhiễu."""
    out: list[QuoteMatch] = []
    for ev in evidence:
        text = paragraphs.get(ev["pid"])
        out.append(QuoteMatch("unknown_pid", 0.0) if text is None else verify_quote(ev["quote"], text, allow_fuzzy=allow_fuzzy))
    return out


def unit_is_verified(results: list[QuoteMatch]) -> bool:
    """Một đơn vị tri thức hợp lệ nếu có ÍT NHẤT một bằng chứng không phải missing/unknown_pid."""
    return any(r.status in ("exact", "loose", "fuzzy") for r in results)


def bad_quote_ratio(all_results: list[list[QuoteMatch]]) -> float:
    """Tỷ lệ bằng chứng hỏng trên toàn segment; > 0.3 thì chạy lại P2 với phản hồi."""
    flat = [r for rs in all_results for r in rs]
    return 0.0 if not flat else sum(r.status in ("missing", "unknown_pid") for r in flat) / len(flat)
