"""Đọc JSON có cấu trúc từ phản hồi LLM và kiểm tra bằng schema — port của `docs/reference/structured_output.py`.

Nguyên tắc của SPEC §17.9: pipeline LUÔN kiểm tra lại đầu ra bằng schema đầy đủ, không bao giờ tin
vào "cú pháp JSON đúng". Schema nạp từ `docs/schemas/` (nguồn sự thật) qua registry `referencing`,
nên `$ref` chéo tệp (`common.schema.json#/$defs/...`) phân giải đúng.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", re.S)
SCHEMA_REF = re.compile(r"^schema://(.+\.json)$")


class SchemaError(ValueError):
    """Đầu ra không khớp schema — pipeline chạy lại một lần kèm lỗi (§6.13)."""

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass
class SchemaStore:
    """Nạp schema từ một thư mục (mặc định `docs/schemas`) và kiểm tra đầu ra."""

    root: Path
    _docs: dict[str, dict] = field(default_factory=dict, init=False)
    _registry: Registry = field(default=None, init=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self._docs = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("*.json"))}
        resources = [(d["$id"], Resource.from_contents(d)) for d in self._docs.values() if "$id" in d]
        self._registry = Registry().with_resources(resources)

    def names(self) -> list[str]:
        return sorted(self._docs)

    def load(self, name: str) -> dict:
        if name not in self._docs:
            raise SchemaError(f"không có schema {name}")
        return self._docs[name]

    def by_output(self, meta: dict) -> dict | None:
        """`meta['output']` dạng `schema://x.schema.json` → schema; `text/markdown` → None (P8)."""
        m = SCHEMA_REF.match(str(meta.get("output", "")))
        return self.load(m.group(1)) if m else None

    def validate(self, obj: Any, schema: dict, *, what: str = "đầu ra") -> Any:
        validator = jsonschema.Draft202012Validator(schema, registry=self._registry)
        errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.absolute_path))
        if errors:
            detail = [f"{'/'.join(map(str, e.absolute_path)) or '<gốc>'}: {e.message}" for e in errors[:8]]
            raise SchemaError(f"{what} không khớp schema ({len(errors)} lỗi)", detail)
        return obj


def extract_json(text: str) -> Any:
    """Lấy giá trị JSON đầu tiên từ phản hồi 'lộn xộn': bỏ `<think>`, rào ```, lời dẫn thừa."""
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


def parse_and_validate(text: str, schema: dict, *, store: SchemaStore | None = None, what: str = "đầu ra") -> Any:
    """Trích JSON và kiểm tra bằng schema đầy đủ; ném `SchemaError` kèm danh sách lỗi dễ đọc."""
    try:
        obj = extract_json(text)
    except ValueError as exc:
        raise SchemaError(f"{what}: {exc}") from exc
    if store is None:  # không có registry: kiểm tra bằng chính schema (ref chéo tệp sẽ lỗi)
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.absolute_path))
        if errors:
            detail = [f"{'/'.join(map(str, e.absolute_path)) or '<gốc>'}: {e.message}" for e in errors[:8]]
            raise SchemaError(f"{what} không khớp schema ({len(errors)} lỗi)", detail)
        return obj
    return store.validate(obj, schema, what=what)
