"""Nạp và render prompt từ `docs/prompts/` — port của `docs/reference/prompt_render.py` (SPEC §8.1, §8.2).

An toàn quan trọng: việc thay biến `{{...}}` được làm MỘT LƯỢT bằng `re.sub` với hàm callback, nên
nội dung tài liệu chứa chuỗi `{{style_core}}` không bao giờ bị mở rộng lần nữa (chống template injection).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from visynth.textnorm import nfc

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

#: Lõi văn phong mặc định trung tính (SPEC §8.1 điểm 7). Biên dịch lõi do người dùng tạo thuộc M1 (§19).
NEUTRAL_STYLE_TEXT = (
    "No stylistic preferences are configured. Write natural, idiomatic Vietnamese in the register of the "
    "source. Apply the glossary exactly."
)


def default_prompts_dir() -> Path:
    """`VISYNTH_PROMPTS_DIR` hoặc `docs/prompts` của kho (nguồn sự thật duy nhất)."""
    env = os.environ.get("VISYNTH_PROMPTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "docs" / "prompts"


def default_schemas_dir() -> Path:
    env = os.environ.get("VISYNTH_SCHEMAS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "docs" / "schemas"


@dataclass(frozen=True)
class Prompt:
    meta: dict
    system: str
    user: str

    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def prompt_id(self) -> str:
        """'P0', 'P13'… dùng cho kịch bản của `FakeLLMClient` và ghi sổ."""
        return self.meta["id"].split("_")[0]

    @property
    def stage(self) -> str:
        return self.meta["stage"]

    @property
    def model_profile(self) -> str:
        return self.meta["model_profile"]

    @property
    def max_output_tokens(self) -> int:
        return int(self.meta.get("max_output_tokens", 4096))

    @property
    def thinking(self) -> str:
        return str(self.meta.get("thinking", "default"))


def load_prompt(path: str | Path) -> tuple[dict, str, str]:
    """Trả về `(front_matter, system, user)`."""
    text = Path(path).read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path}: thiếu front matter")
    meta, body = yaml.safe_load(m.group(1)), m.group(2)
    sys_part, sep, user_part = body.partition("## USER")
    if not sep:
        raise ValueError(f"{path}: thiếu mục '## USER'")
    return meta, sys_part.replace("## SYSTEM", "", 1).strip(), user_part.strip()


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return nfc(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_prompt(prompts_dir: str | Path, prompt_id: str, variables: dict) -> Prompt:
    """Tìm tệp `Pxx_*.md`, kiểm tra biến khớp front matter rồi điền biến (một lượt)."""
    directory = Path(prompts_dir)
    matches = sorted(directory.glob(f"{prompt_id}_*.md"))
    if not matches:
        raise FileNotFoundError(f"không có prompt {prompt_id} trong {directory}")
    meta, system, user = load_prompt(matches[0])
    declared = set(meta["variables"])
    missing = declared - set(variables)
    extra = set(variables) - declared
    if missing:
        raise KeyError(f"{prompt_id}: thiếu biến {sorted(missing)}")
    if extra:
        raise KeyError(f"{prompt_id}: biến thừa {sorted(extra)}")

    def sub(m: re.Match) -> str:
        return _stringify(variables[m.group(1)])

    return Prompt(meta, _PLACEHOLDER.sub(sub, system), _PLACEHOLDER.sub(sub, user))


def neutral_style_core(_stage: str = "write") -> str:
    """Khối văn bản của lõi MẶC ĐỊNH trung tính. Lõi đã duyệt của lõi lĩnh vực truyền vào `style_core_text`."""
    return NEUTRAL_STYLE_TEXT


# ------------------------------------------------------------------ định dạng biến (§8.2)


def format_segment_text(paragraphs: list[dict]) -> str:
    """`[P000123] nội dung` cách nhau một dòng trống."""
    return "\n\n".join(f"[{p['pid']}] {p['text']}" for p in paragraphs)


def _cell(value: Any) -> str:
    return " ".join(str(value).replace("|", "/").split())


def format_units_compact(units: list[dict]) -> str:
    """Một dòng mỗi unit: `U-0001 | importance | type | title | statement | topics`."""
    return "\n".join(
        " | ".join(
            [
                u["id"],
                u["importance"],
                u["type"],
                _cell(u["title_vi"]),
                _cell(u["statement_vi"]),
                _cell(", ".join(u.get("topics", []))),
            ]
        )
        for u in units
    )


def filter_glossary(entries: list[dict], text: str, allow_plural: bool = True) -> list[dict]:
    """Chỉ giữ mục glossary có `source_term` xuất hiện trong `text` (tiết kiệm token, giảm nhiễu)."""
    t = nfc(text)
    out = []
    for e in entries:
        flags = 0 if e.get("case_sensitive") else re.IGNORECASE
        plural = r"(?:s|es)?" if allow_plural else ""
        if re.search(rf"(?<!\w){re.escape(nfc(e['source_term']))}{plural}(?!\w)", t, flags):
            out.append(e)
    return out
