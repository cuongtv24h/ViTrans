"""Đổi JSON Schema đầy đủ (dùng để kiểm tra phía server) sang 'wire schema' gửi cho Gemini.

Cơ sở: tài liệu Structured outputs của Gemini API (cập nhật 2026-09-23) nêu rõ chỉ hỗ trợ MỘT TẬP CON JSON Schema:
  type (string|number|integer|boolean|object|array|null, cho phép mảng type để nullable), title, description,
  object: properties/required/additionalProperties; string: enum/format(date,date-time,time);
  number/integer: enum/minimum/maximum; array: items/prefixItems/minItems/maxItems.
Do đó: pattern, minLength, maxLength, uniqueItems, oneOf/anyOf/allOf... KHÔNG được gửi. Sau khi model trả kết quả,
LUÔN kiểm tra lại bằng schema đầy đủ (pattern, độ dài...) vì cú pháp đúng chưa chắc giá trị đúng.
"""
from __future__ import annotations

from urllib.parse import urljoin

KEEP = {"type", "properties", "required", "additionalProperties", "enum", "items", "prefixItems",
        "minimum", "maximum", "minItems", "maxItems", "title", "description"}
ALLOWED_FORMATS = {"date", "date-time", "time"}
FORBIDDEN_COMBINATORS = {"oneOf", "anyOf", "allOf", "not", "if", "then", "else", "patternProperties", "dependentRequired"}
MAX_DEPTH = 40


def _resolve(ref: str, base_id: str, store: dict[str, dict]) -> tuple[dict, str]:
    uri, _, frag = ref.partition("#")
    target = urljoin(base_id, uri) if uri else base_id
    node = store[target]
    for part in [p for p in frag.split("/") if p]:
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node, target


def _convert(node, base_id: str, store: dict[str, dict], depth: int):
    if depth > MAX_DEPTH:
        raise ValueError("schema đệ quy/quá sâu; Gemini có thể từ chối schema quá phức tạp")
    if not isinstance(node, dict):
        return node
    bad = FORBIDDEN_COMBINATORS & node.keys()
    if bad:
        raise ValueError(f"từ khoá không được hỗ trợ bởi Gemini structured output: {sorted(bad)}")
    if "$ref" in node:
        sub, tid = _resolve(node["$ref"], base_id, store)
        out = _convert(sub, tid, store, depth + 1)
        for k in ("title", "description"):  # phần mô tả ở nơi tham chiếu được ưu tiên
            if k in node:
                out[k] = node[k]
        return out
    out: dict = {}
    if "const" in node:
        out["enum"] = [node["const"]]
    for k, v in node.items():
        if k == "format":
            if v in ALLOWED_FORMATS:
                out["format"] = v
        elif k == "properties":
            out["properties"] = {pk: _convert(pv, base_id, store, depth + 1) for pk, pv in v.items()}
        elif k in ("items", "additionalProperties"):
            out[k] = _convert(v, base_id, store, depth + 1) if isinstance(v, dict) else v
        elif k == "prefixItems":
            out[k] = [_convert(x, base_id, store, depth + 1) for x in v]
        elif k in KEEP:
            out[k] = v
    return out


def to_wire_schema(schema: dict, store: dict[str, dict]) -> dict:
    """schema: tài liệu schema gốc (có $id); store: {$id: schema} của TẤT CẢ file trong schemas/."""
    return _convert(schema, schema["$id"], store, 0)


def load_store(schemas_dir) -> dict[str, dict]:
    import json
    import pathlib

    store = {}
    for p in sorted(pathlib.Path(schemas_dir).glob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        store[s["$id"]] = s
    return store
