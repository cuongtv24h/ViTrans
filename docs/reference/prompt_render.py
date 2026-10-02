"""Render prompt từ prompts/*.md và định dạng các biến đầu vào.

Điểm an toàn quan trọng: việc thay biến {{...}} được làm MỘT LƯỢT bằng re.sub với hàm callback,
nên nội dung do người dùng cung cấp (ví dụ văn bản tài liệu chứa chuỗi '{{style_core}}')
không bao giờ bị mở rộng lần nữa (chống template injection). Test: tests/test_prompt_render.py.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from .textnorm import nfc

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def load_prompt(path) -> tuple[dict, str, str]:
    """Trả về (front_matter, system, user)."""
    text = Path(path).read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path}: thiếu front matter")
    meta, body = yaml.safe_load(m.group(1)), m.group(2)
    sys_part, sep, user_part = body.partition("## USER")
    if not sep:
        raise ValueError(f"{path}: thiếu mục '## USER'")
    return meta, sys_part.replace("## SYSTEM", "", 1).strip(), user_part.strip()


def load_default_style_core(prompts_dir, stage: str = "write") -> str:
    """Khối văn bản của Lõi văn phong MẶC ĐỊNH trung tính (prompts/00_style_core_neutral.json) cho một giai đoạn.
    Công thức có gắn Lõi văn phong thì dùng reference.style_core.compile_style_core() trên nội dung phiên bản đã ghim."""
    import json

    from .style_core import compile_style_core

    content = json.loads(Path(prompts_dir, "00_style_core_neutral.json").read_text(encoding="utf-8"))
    return compile_style_core(content, stage)


def _stringify(value) -> str:
    if isinstance(value, str):
        return nfc(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render(path, variables: dict) -> tuple[dict, str, str]:
    """Điền biến. Thiếu hoặc thừa biến so với front matter đều là lỗi (bắt sớm sai sót khi code pipeline)."""
    meta, system, user = load_prompt(path)
    declared = set(meta["variables"])
    missing = declared - set(variables)
    extra = set(variables) - declared
    if missing:
        raise KeyError(f"thiếu biến: {sorted(missing)}")
    if extra:
        raise KeyError(f"biến thừa: {sorted(extra)}")

    def sub(m: re.Match) -> str:
        return _stringify(variables[m.group(1)])

    return meta, _PLACEHOLDER.sub(sub, system), _PLACEHOLDER.sub(sub, user)


# ----------------------------------------------------------------------------- định dạng biến
def format_segment_text(paragraphs: list[dict]) -> str:
    """'[P000123] nội dung' cách nhau một dòng trống. Dùng cho segment_text, context_before, source_passages."""
    return "\n\n".join(f"[{p['pid']}] {p['text']}" for p in paragraphs)


def _cell(s) -> str:
    return " ".join(str(s).replace("|", "/").split())


def format_units_compact(units: list[dict]) -> str:
    """Một dòng mỗi unit: 'U-0001 | importance | type | title | statement | topics' (dấu | trong nội dung đổi thành /)."""
    return "\n".join(
        " | ".join([u["id"], u["importance"], u["type"], _cell(u["title_vi"]), _cell(u["statement_vi"]), _cell(", ".join(u.get("topics", [])))])
        for u in units
    )


def filter_glossary(entries: list[dict], text: str, allow_plural: bool = True) -> list[dict]:
    """Chỉ giữ mục glossary có source_term xuất hiện trong `text` (tiết kiệm token, giảm nhiễu cho model)."""
    t = nfc(text)
    out = []
    for e in entries:
        flags = 0 if e.get("case_sensitive") else re.IGNORECASE
        plural = r"(?:s|es)?" if allow_plural else ""
        if re.search(rf"(?<!\w){re.escape(nfc(e['source_term']))}{plural}(?!\w)", t, flags):
            out.append(e)
    return out
