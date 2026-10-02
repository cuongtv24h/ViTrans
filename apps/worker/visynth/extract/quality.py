"""Chất lượng bóc tách và đoán ngôn ngữ nguồn (§6.1).

Các trọng số và ngưỡng ở đây là GIÁ TRỊ KHỞI ĐIỂM, phải hiệu chỉnh trên golden set ở M0-W4
(so điểm với đánh giá của người trên `eval/`).
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # tránh vòng import
    from visynth.extract.model import Paragraph

#: Chữ cái chỉ có ở tiếng Việt (đã kể cả dạng tổ hợp sau NFC).
VIETNAMESE_LETTERS = frozenset("ăâđêôơưĂÂĐÊÔƠƯ")
#: Dấu thanh + dấu mũ ở dạng tổ hợp (NFD) và dựng sẵn (NFC).
VIETNAMESE_MARKS = frozenset("\u0300\u0301\u0303\u0309\u0323\u0302\u0306\u031b")

_WEIGHTS = {"printable": 0.35, "length": 0.25, "ocr": 0.20, "garbage": 0.20}


def guess_language(text: str) -> tuple[str, float]:
    """Đoán ngôn ngữ nguồn: 'vi' khi mật độ chữ/dấu đặc trưng tiếng Việt đủ cao, còn lại 'en'.

    Chỉ dùng để chọn hệ số token (chars/2.8 vs chars/4) — hồ sơ đầy đủ do P0 quyết định (§6.2).
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return "en", 0.0
    vi_letters = sum(1 for ch in letters if ch in VIETNAMESE_LETTERS or unicodedata.combining(ch))
    # Chữ có dấu Latin ngoài tiếng Việt (é, ü, ñ…) không tính là tiếng Việt, nhưng vẫn khác tiếng Anh;
    # ở đây chỉ cần phân biệt vi/en cho hệ số token.
    ratio = vi_letters / len(letters)
    if ratio >= 0.02:
        return "vi", min(1.0, 0.5 + ratio * 4)
    return "en", min(1.0, 0.5 + (1 - ratio))


def printable_ratio(text: str) -> float:
    """Tỷ lệ ký tự in được trên tổng ký tự không phải khoảng trắng."""
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return 1.0
    good = sum(1 for ch in chars if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs", "Cn"))
    return good / len(chars)


def length_score(paragraphs: list[Paragraph]) -> float:
    """Tỷ lệ đoạn văn xuôi có độ dài hợp lý (40–4000 ký tự); tiêu đề/bảng không tính."""
    prose = [p for p in paragraphs if p.kind in ("body", "list_item", "quote", "speaker_turn")]
    if not prose:
        return 1.0
    ok = sum(1 for p in prose if 40 <= p.char_count <= 4000)
    return ok / len(prose)


def garbage_ratio(text: str) -> float:
    """Tỷ lệ 'từ lạ' — proxy thô khi chưa có từ điển: token không có nguyên âm, hoặc trộn nhiều hệ chữ.

    Phải thay bằng từ điển nhỏ/n-gram ở M0-W4 (SPEC §6.1 điểm (d)).
    """
    words = text.split()
    if not words:
        return 0.0
    bad = 0
    for w in words:
        letters = [ch for ch in w if ch.isalpha()]
        if not letters:
            continue
        vowels = sum(
            1
            for ch in letters
            if ch.lower() in "aeiouyàáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ"
        )
        scripts = {_script_of(ch) for ch in letters}
        if vowels == 0 or len(scripts) > 1:
            bad += 1
    return bad / len(words)


def _script_of(ch: str) -> str:
    name = unicodedata.name(ch, "")
    return name.split(" ")[0] if name else "UNKNOWN"


def extraction_quality(
    paragraphs: list[Paragraph],
    *,
    page_count: int | None = None,
    pages_ocr: int = 0,
) -> float:
    """Điểm chất lượng bóc tách ∈ [0,1] theo §6.1: trung bình có trọng số của 4 thành phần."""
    text = "\n\n".join(p.content for p in paragraphs)
    ocr_score = 1.0
    if page_count:
        ocr_score = max(0.0, 1.0 - pages_ocr / page_count)
    score = (
        _WEIGHTS["printable"] * printable_ratio(text)
        + _WEIGHTS["length"] * length_score(paragraphs)
        + _WEIGHTS["ocr"] * ocr_score
        + _WEIGHTS["garbage"] * (1.0 - garbage_ratio(text))
    )
    return round(max(0.0, min(1.0, score)), 3)
