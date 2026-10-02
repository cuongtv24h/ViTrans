"""Chuẩn hoá văn bản phục vụ SO KHỚP (không dùng để hiển thị)."""
from __future__ import annotations

import re
import unicodedata

_TRANSLATE = str.maketrans(
    {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u00ab": '"', "\u00bb": '"',
        "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2010": "-", "\u2011": "-",
        "\u00a0": " ", "\u2009": " ", "\u202f": " ",
        "\u2026": "...",
    }
)
_SOFT_HYPHEN = "\u00ad"
_HYPHEN_BREAK = re.compile(r"(\w)-[ \t]*\n[ \t]*(\w)")
_WS = re.compile(r"\s+")


def nfc(s: str) -> str:
    """Unicode NFC. Bắt buộc với tiếng Việt: dấu có thể ở dạng dựng sẵn hoặc tổ hợp."""
    return unicodedata.normalize("NFC", s)


def norm_for_match(s: str) -> str:
    """NFC, thống nhất dấu nháy/gạch ngang, bỏ soft-hyphen, nối từ bị ngắt dòng, gộp khoảng trắng, casefold."""
    s = nfc(s).replace(_SOFT_HYPHEN, "")
    s = _HYPHEN_BREAK.sub(r"\1\2", s)
    s = s.translate(_TRANSLATE)
    return _WS.sub(" ", s).strip().casefold()


def norm_loose(s: str) -> str:
    """Chỉ giữ chữ và số (casefold): chịu được khác biệt dấu câu, gạch nối, khoảng trắng."""
    return "".join(ch for ch in nfc(s).casefold() if ch.isalnum())


def strip_diacritics(s: str) -> str:
    """Bỏ dấu tiếng Việt để tìm kiếm không phân biệt dấu (đ -> d)."""
    s = unicodedata.normalize("NFD", s).replace("đ", "d").replace("Đ", "D")
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
