"""Kiểm tra trích đoạn bằng chứng có THẬT SỰ nằm trong đoạn nguồn — port của `docs/reference/quote_verify.py`.

Đây là chốt chặn chống bịa quan trọng nhất của pipeline (SPEC §6.0 nguyên tắc 2, §6.5 bước 3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from visynth.textnorm import norm_for_match, norm_loose

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

    @property
    def ok(self) -> bool:
        return self.status in ("exact", "loose", "fuzzy")


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
    """Mức khớp của `quote` với `paragraph`.

    exact  : khớp sau chuẩn hoá (NFC, dấu nháy, khoảng trắng, hoa/thường)
    loose  : khớp khi chỉ xét chữ+số (khác dấu câu/gạch nối)
    fuzzy  : CHỈ khi allow_fuzzy=True (tài liệu OCR); gần giống >= ngưỡng, đủ dài, KHÔNG có chữ số lạ
    missing: không khớp -> bằng chứng KHÔNG hợp lệ
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
            return QuoteMatch("missing", 0.0)  # chữ số không có trong nguồn -> nghi bịa
        score = _partial_ratio(lq, lp)
        if score >= fuzzy_threshold:
            return QuoteMatch("fuzzy", score)
        return QuoteMatch("missing", score)
    return QuoteMatch("missing", 0.0)


def verify_evidence(evidence: list[dict], paragraphs: dict[str, str], *, allow_fuzzy: bool = False) -> list[QuoteMatch]:
    """Kiểm tra danh sách `{pid, quote}` so với `{pid: text}`."""
    out: list[QuoteMatch] = []
    for ev in evidence:
        text = paragraphs.get(ev.get("pid", ""))
        out.append(
            QuoteMatch("unknown_pid", 0.0)
            if text is None
            else verify_quote(ev.get("quote", ""), text, allow_fuzzy=allow_fuzzy)
        )
    return out


def bad_quote_ratio(all_results: list[list[QuoteMatch]]) -> float:
    """Tỷ lệ bằng chứng hỏng trên toàn segment; > 0.3 thì chạy lại P2 kèm phản hồi (§6.5 bước 4)."""
    flat = [r for rs in all_results for r in rs]
    return 0.0 if not flat else sum(r.status in ("missing", "unknown_pid") for r in flat) / len(flat)
