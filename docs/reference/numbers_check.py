"""Kiểm tra con số/ngày/mã số trong báo cáo có xuất hiện trong nguồn không (best-effort, tất định)."""
from __future__ import annotations

import re
from typing import Iterable

_NUM = re.compile(r"(?<![\w])(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(?![\w])")
_LIST_MARK = re.compile(r"^[ \t]*\d+[.)][ \t]+", re.M)  # '1. ' ở đầu dòng (danh sách Markdown)
_ID_TOKENS = re.compile(r"\b(?:U-\d{4,}|P\d{6,}|S\d{2,}(?:\.b\d{2,})?|SEG-\d{3,})\b")

_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
_WORD_RE = re.compile(r"\b(" + "|".join(_WORDS) + r")\b", re.I)


def canon_number(s: str) -> str:
    """Chuẩn hoá '1,000' / '1.000' / '3,5' / '3.50' về dạng so sánh được ('1000', '3.5')."""
    s = s.strip().replace("\u202f", "")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) > 2 and all(len(p) == 3 for p in parts[1:]):
            s = "".join(parts)  # 1.234.567
        elif len(parts) == 2 and len(parts[1]) == 3 and 1 <= len(parts[0]) <= 3 and parts[0] != "0":
            s = "".join(parts)  # 1.234 hoặc 1,234 -> nghìn
        else:
            s = s.replace(sep, ".")
    if "." in s:
        ip, fp = s.split(".", 1)
        fp = fp.rstrip("0")
        ip = ip.lstrip("0") or "0"
        return f"{ip}.{fp}" if fp else ip
    return s.lstrip("0") or "0"


def extract_numbers(text: str, *, words_to_digits: bool = False) -> list[str]:
    """Trích mọi số trong văn bản (đã bỏ số thứ tự danh sách và ID nội bộ)."""
    t = _LIST_MARK.sub("", text)
    t = _ID_TOKENS.sub(" ", t)
    if words_to_digits:
        t = _WORD_RE.sub(lambda m: str(_WORDS[m.group(1).lower()]), t)
    return [canon_number(m.group(1)) for m in _NUM.finditer(t)]


def unverified_numbers(report_text: str, source_texts: Iterable[str], *, min_value: float = 0) -> list[str]:
    """Các số xuất hiện trong báo cáo nhưng KHÔNG có trong bất kỳ đoạn nguồn nào.

    Kết quả không đồng nghĩa với sai (số có thể được suy ra), nên chuyển cho P5 hoặc đánh cờ 'chưa kiểm chứng'.
    Nguồn được đổi chữ số viết bằng chữ tiếng Anh (two -> 2) để giảm báo động giả.
    """
    src: set[str] = set()
    for s in source_texts:
        src.update(extract_numbers(s, words_to_digits=True))
        src.update(extract_numbers(s))
    out = set()
    for n in extract_numbers(report_text):
        try:
            if float(n) < min_value:
                continue
        except ValueError:
            continue
        if n not in src:
            out.add(n)
    return sorted(out, key=lambda x: (len(x), x))
