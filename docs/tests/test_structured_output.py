import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reference.gemini_schema import load_store  # noqa: E402
from reference.structured_output import extract_json, plan_request  # noqa: E402

STORE = load_store(ROOT / "schemas")
TC = STORE["https://visynth.example/schemas/translation_chunk.schema.json"]
BANNED = ("pattern", "minLength", "maxLength", "$ref", "oneOf", "anyOf")


def test_native_json_schema_uses_response_format_without_prompt_suffix():
    p = plan_request("json_schema", TC, STORE, "translation_chunk")
    assert p.mode == "json_schema" and p.prompt_suffix == ""
    assert p.response_format["type"] == "json_schema" and p.response_format["json_schema"]["strict"] is False
    assert not any(b in json.dumps(p.wire_schema) for b in BANNED)


def test_json_object_models_get_schema_in_prompt():
    p = plan_request("json_object", TC, STORE, "translation_chunk")
    assert p.mode == "json_object" and p.response_format == {"type": "json_object"}
    inner = p.prompt_suffix.split("<json_schema>")[1].split("</json_schema>")[0]
    assert json.loads(inner) == p.wire_schema  # schema nhúng vào prompt là JSON hợp lệ và đúng bằng wire schema


def test_models_without_json_support_fall_back_to_prompt_only():
    p = plan_request("none", TC, STORE, "x")
    assert p.mode == "prompt_only" and p.response_format is None and "No prose, no code fences" in p.prompt_suffix


def test_gemini_native_keeps_schema_for_the_adapter():
    p = plan_request("json_schema", TC, STORE, "x", provider_kind="gemini_native")
    assert p.response_format is None and p.wire_schema["type"] == "object" and p.prompt_suffix == ""


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Sure! Here is the JSON:\n{"a": 1}\nHope this helps.',
        '<think>I should output {"a": 2} maybe</think>\n{"a": 1}',
        '\ufeff  {"a": 1}',
    ],
)
def test_extract_json_handles_messy_responses(text):
    assert extract_json(text) == {"a": 1}


def test_extract_json_ignores_braces_inside_strings_and_supports_arrays():
    assert extract_json('{"s": "a } b { c", "n": {"x": [1, 2]}}') == {"s": "a } b { c", "n": {"x": [1, 2]}}
    assert extract_json("Result: [1, 2, 3]") == [1, 2, 3]


@pytest.mark.parametrize("bad", ["", "no json here", '{"a": [1, 2', '{"a": 1,}'])
def test_extract_json_rejects_truncated_or_invalid(bad):
    with pytest.raises(ValueError):
        extract_json(bad)
