"""Bậc thang structured output cho pool nhiều nhà cung cấp (SPEC §17.9).

Mỗi model hỗ trợ JSON ở mức khác nhau (json_schema > json_object > không có). Pipeline luôn yêu cầu đầu ra theo schema đầy đủ; adapter
chọn cách gửi phù hợp với model được định tuyến, rồi code LUÔN kiểm tra lại bằng schema đầy đủ. Không bao giờ tin vào 'cú pháp JSON đúng'.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .gemini_schema import to_wire_schema

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", re.S)


@dataclass(frozen=True)
class StructuredPlan:
    mode: str  # json_schema | json_object | prompt_only
    response_format: dict | None  # trường response_format gửi cho API kiểu OpenAI (None nếu không dùng)
    wire_schema: dict  # schema đã rút gọn (tập con JSON Schema phổ biến)
    prompt_suffix: str  # thêm vào cuối thông điệp người dùng khi nhà cung cấp không ép schema ở phía server


_SUFFIX = (
    "\n\nReturn ONLY one JSON object that conforms to this JSON Schema. No prose, no code fences, no comments.\n"
    "<json_schema>{schema}</json_schema>"
)


def plan_request(native: str, schema: dict, store: dict[str, dict], name: str, provider_kind: str = "openai_compat") -> StructuredPlan:
    """native: mức hỗ trợ ASYNC của model ('json_schema' | 'json_object' | 'none')."""
    wire = to_wire_schema(schema, store)
    compact = json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
    if provider_kind == "gemini_native":
        # adapter đặt wire_schema vào cấu hình sinh (responseSchema / response_format của Interactions API)
        return StructuredPlan("json_schema", None, wire, "")
    if native == "json_schema":
        # strict=False: schema của spec có trường tuỳ chọn; chế độ strict của một số nhà cung cấp đòi mọi trường đều required
        rf = {"type": "json_schema", "json_schema": {"name": re.sub(r"\W", "_", name)[:60], "schema": wire, "strict": False}}
        return StructuredPlan("json_schema", rf, wire, "")
    if native == "json_object":
        return StructuredPlan("json_object", {"type": "json_object"}, wire, _SUFFIX.format(schema=compact))
    return StructuredPlan("prompt_only", None, wire, _SUFFIX.format(schema=compact))


def extract_json(text: str):
    """Lấy giá trị JSON đầu tiên từ phản hồi 'lộn xộn': bỏ khối <think>, rào ```, lời dẫn thừa. Ném ValueError nếu không có JSON hợp lệ."""
    t = _THINK.sub("", text).strip().lstrip("\ufeff")
    m = _FENCE.match(t)
    if m:
        t = m.group(1).strip()
    start = next((i for i, ch in enumerate(t) if ch in "{["), None)
    if start is None:
        raise ValueError("không có JSON trong phản hồi")
    open_ch = t[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    raise ValueError("JSON bị cắt cụt (ngoặc không khớp)")
