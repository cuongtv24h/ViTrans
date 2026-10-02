"""Kiểm thử khai báo nhà cung cấp và khoá (SPEC §17.14): tách danh sách khoá, nhóm hạn mức, xác nhận rủi ro, biên dịch thành pool_config,
mã hoá khoá, và bảo đảm khoá thật không bao giờ lọt vào phần trả cho client."""
import copy
import json
import pathlib
import sys

import pytest
import yaml
from cryptography.exceptions import InvalidTag
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reference.llm_pool import Lease, Request, Router, load_pool_model  # noqa: E402
from reference.pool_declare import (  # noqa: E402
    CONSERVATIVE_LIMITS, KeyEntry, classify_secret, compile_all, compile_declaration, merge_fragment, parse_keys, split_keys,
)
from reference.pool_secrets import (  # noqa: E402
    decrypt_secret, encrypt_secret, fingerprint, last4, load_master_key, mask, new_master_key,
)

SCHEMAS = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "schemas").glob("*.schema.json")}
REG = Registry()
for _s in SCHEMAS.values():
    REG = REG.with_resource(_s["$id"], Resource.from_contents(_s, default_specification=DRAFT202012))
POOL_VALID = Draft202012Validator(SCHEMAS["pool_config.schema.json"], registry=REG)
DECL_VALID = Draft202012Validator(SCHEMAS["pool_declaration.schema.json"], registry=REG)
CFG = json.loads((ROOT / "examples" / "pool_config.example.json").read_text(encoding="utf-8"))
EXAMPLE = json.loads((ROOT / "examples" / "pool_declaration.example.json").read_text(encoding="utf-8"))
TEMPLATE = yaml.safe_load((ROOT / "examples" / "pool_declaration.template.yaml").read_text(encoding="utf-8"))
MASTER = bytes(range(32))
T0 = 1_790_000_000.0


def gk(n: int) -> str:
    return "AIza" + "EXAMPLEexampleEXAMPLEexample" + f"{n:04d}"


EMPTY = {k: CFG[k] for k in ("version", "note", "policy", "profiles")} | {"providers": [], "models": [], "groups": [], "deployments": []}


def leaked(obj, secrets) -> list[str]:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    return [s for s in secrets if s in text]


# --------------------------------------------------------------------------- tách danh sách khoá
@pytest.mark.parametrize("text", [
    "k1AAAAAAAAAAAAAAAA,k2BBBBBBBBBBBBBBBB,k3CCCCCCCCCCCCCCCC",
    "k1AAAAAAAAAAAAAAAA; k2BBBBBBBBBBBBBBBB;k3CCCCCCCCCCCCCCCC",
    "k1AAAAAAAAAAAAAAAA\nk2BBBBBBBBBBBBBBBB\r\nk3CCCCCCCCCCCCCCCC\n",
    "  k1AAAAAAAAAAAAAAAA   k2BBBBBBBBBBBBBBBB\tk3CCCCCCCCCCCCCCCC ,",
    '"k1AAAAAAAAAAAAAAAA", \'k2BBBBBBBBBBBBBBBB\', [k3CCCCCCCCCCCCCCCC]',
    "\ufeffk1AAAAAAAAAAAAAAAA,,, k2BBBBBBBBBBBBBBBB ;; k3CCCCCCCCCCCCCCCC",
    ["k1AAAAAAAAAAAAAAAA", "k2BBBBBBBBBBBBBBBB", "k3CCCCCCCCCCCCCCCC"],
])
def test_split_keys_accepts_every_common_separator_and_quoting(text):
    assert [k for _, k in split_keys(text)] == ["k1AAAAAAAAAAAAAAAA", "k2BBBBBBBBBBBBBBBB", "k3CCCCCCCCCCCCCCCC"]


def test_split_keys_understands_labels_bearer_prefix_and_env_lines():
    got = split_keys("acc1 | AIzaKEY0000000000000000, Bearer AIzaKEY1111111111111111\nGEMINI_KEY_4=AIzaKEY2222222222222222")
    assert got == [("acc1", "AIzaKEY0000000000000000"), (None, "AIzaKEY1111111111111111"), ("GEMINI_KEY_4", "AIzaKEY2222222222222222")]
    assert split_keys("abcdEFGH12345678==") == [(None, "abcdEFGH12345678==")]  # dấu '=' đệm của base64 không bị nhầm là nhãn
    assert split_keys("") == [] and split_keys(None) == [] and split_keys(" ,; \n") == []


def test_classify_secret_catches_typical_paste_mistakes():
    assert classify_secret("AIza" + "x1" * 20, r"AIza[0-9A-Za-z_-]{30,60}")[0] == "ok"
    assert classify_secret("short")[0] == "too_short"
    assert classify_secret("DÁN_KHOÁ_VÀO_ĐÂY_NHÉ_BẠN")[0] == "invalid"
    for ph in ("your_api_key_here_123", "xxxxxxxxxxxxxxxxxxxxxxxx", "AIzaSy...............", "<paste-your-key-here>", "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"):
        assert classify_secret(ph)[0] == "placeholder", ph
    assert classify_secret("sk-" + "aB3" * 10, r"AIza[0-9A-Za-z_-]{30,60}")[0] == "invalid"  # sai định dạng của nhà cung cấp
    assert classify_secret("has space in the key 1234567890")[0] == "invalid"


def test_parse_keys_flags_duplicates_in_request_and_existing_without_decrypting():
    fp = lambda s: fingerprint(MASTER, s)  # noqa: E731
    existing = {fp(gk(7))}
    es = parse_keys(f"{gk(1)}, {gk(1)}, {gk(7)}, {gk(2)}", key_regex=r"AIza[0-9A-Za-z_-]{30,60}", fingerprint_fn=fp, existing_fingerprints=existing)
    assert [e.status for e in es] == ["ok", "duplicate_in_request", "duplicate_existing", "ok"]


def test_key_entry_repr_never_prints_the_secret():
    e = KeyEntry(None, gk(1), "ok")
    assert gk(1) not in repr(e) and gk(1) not in str(e)
    assert e.last4 == "0001"


def test_invalid_label_is_reported_not_silently_dropped():
    es = parse_keys("***|" + gk(1))
    assert es[0].status == "invalid" and "nhãn" in es[0].reason


# --------------------------------------------------------------------------- biên dịch khai báo
def test_example_declaration_is_schema_valid_and_compiles_from_an_empty_pool():
    assert not list(DECL_VALID.iter_errors(EXAMPLE))
    r = compile_all(EXAMPLE, EMPTY)
    assert r.valid, r.errors
    ids = [g["id"] for g in r.fragment["groups"]]
    assert ids == ["gemini-free-acc1", "gemini-free-acc2", "gemini-free-acc3", "gemini-paid", "provider-c-free-1", "provider-c-free-2"]
    assert r.preview["counts"]["keys"] == 6 and len(r.secrets) == 6
    cfg = merge_fragment(EMPTY, r.fragment)
    assert not list(POOL_VALID.iter_errors(cfg)), [e.message for e in POOL_VALID.iter_errors(cfg)][:3]
    model = load_pool_model(cfg)
    assert len(model.deployments) == 6 and set(model.profiles) == {"fast", "writer", "verifier", "ocr", "curator"}


def test_nothing_the_client_receives_contains_a_real_key():
    r = compile_all(EXAMPLE, EMPTY)
    secrets = list(r.secrets.values())
    assert secrets and all(len(s) >= 16 for s in secrets)
    assert leaked(r.fragment, secrets) == [] and leaked(r.preview, secrets) == [] and leaked(r.warnings + r.errors, secrets) == []
    assert leaked(merge_fragment(EMPTY, r.fragment), secrets) == []
    assert all(c["secret_ref"].startswith("enc:") for g in r.fragment["groups"] for c in g["credentials"])
    assert "secrets" not in json.dumps(r.preview)


def test_flags_gates_and_policy_are_derived_from_tier_and_account_count():
    r = compile_all(EXAMPLE, EMPTY)
    g = {x["id"]: x for x in r.fragment["groups"]}
    assert g["gemini-free-acc1"]["tos_flags"] == ["multi_account_risk", "no_eea_uk_ch"]
    assert g["gemini-free-acc1"]["data_policy"] == "may_train" and g["gemini-free-acc1"]["allowed_gates"] == ["dev", "A"]
    assert g["gemini-free-acc1"]["reset_tz"] == "America/Los_Angeles" and g["gemini-free-acc1"]["risk_ack"] == ["multi_account_risk"]
    assert g["gemini-paid"]["tos_flags"] == [] and g["gemini-paid"]["data_policy"] == "no_training" and g["gemini-paid"]["allowed_gates"] == ["dev", "A", "B", "C"]
    assert g["provider-c-free-1"]["data_policy"] == "unknown" and "multi_account_risk" in g["provider-c-free-1"]["tos_flags"]
    d = {x["id"]: x for x in r.fragment["deployments"]}
    assert d["gemini-free-acc1/gemini-3.8-flash"]["tpm_basis"] == "input" and d["gemini-free-acc1/gemini-3.8-flash"]["price_mode"] == "free"
    assert d["gemini-paid/gemini-3.8-flash"]["price_mode"] == "metered" and "strong" in d["gemini-paid/gemini-3.8-flash"]["tags"]
    assert d["provider-c-free-1/large-chat-v1"]["limits"]["rpm"] is None and g["provider-c-free-1"]["limits"]["rpm"] == 20  # hạn mức tính cả tài khoản


def test_risk_flags_without_acknowledgement_are_rejected_and_the_message_says_what_to_add():
    decl = {"provider": {"preset": "gemini"}, "tier": "free", "keys": f"{gk(1)}, {gk(2)}"}
    r = compile_declaration(decl, EMPTY)
    assert not r.valid and "multi_account_risk" in r.errors[0] and "risk_ack" in r.errors[0]
    assert r.secrets == {} or leaked(r.errors, list(r.secrets.values())) == []
    ok = compile_declaration(dict(decl, risk_ack=["multi_account_risk"]), EMPTY)
    assert ok.valid and len(ok.fragment["groups"]) == 2


def test_a_single_free_gemini_account_needs_no_risk_acknowledgement():
    r = compile_declaration({"provider": {"preset": "gemini"}, "tier": "free", "keys": gk(1), "limits": {"rpm": 10}}, EMPTY)
    assert r.valid and r.fragment["groups"][0]["tos_flags"] == ["no_eea_uk_ch"] and r.fragment["groups"][0]["allowed_gates"] == ["dev", "A", "B", "C"]


def test_trial_tier_is_dev_only_and_needs_trial_only_acknowledgement():
    decl = {"provider": {"preset": "custom", "id": "provider-t", "base_url": "https://api.t.example/v1"}, "tier": "trial", "keys": "ak_example_trial_0000000001",
            "models": [{"model_id": "m1", "ctx_in": 32768, "max_out": 4096, "structured": "json_object"}], "limits": {"rpm": 40}}
    bad = compile_declaration(decl, EMPTY)
    assert not bad.valid and "trial_only" in bad.errors[0]
    ok = compile_declaration(dict(decl, risk_ack=["trial_only"]), EMPTY)
    g = ok.fragment["groups"][0]
    assert ok.valid and g["allowed_gates"] == ["dev"] and {"trial_only", "no_personal_data"} <= set(g["tos_flags"]) and g["tier"] == "trial"


def test_group_numbering_continues_after_existing_groups_and_never_touches_the_input_config():
    base = merge_fragment(EMPTY, compile_declaration({"provider": {"preset": "gemini"}, "tier": "free", "keys": f"{gk(1)}, {gk(2)}", "risk_ack": ["multi_account_risk"]}, EMPTY).fragment)
    before = copy.deepcopy(base)
    r = compile_declaration({"provider": {"preset": "gemini"}, "tier": "free", "keys": f"{gk(3)}, {gk(4)}", "risk_ack": ["multi_account_risk"]}, base)
    assert [g["id"] for g in r.fragment["groups"]] == ["gemini-free-3", "gemini-free-4"] and base == before
    assert any("multi_account_risk" not in g["tos_flags"] for g in base["groups"]) is False  # nhóm cũ đã có cờ nên không bị cảnh báo thêm


def test_single_group_mode_adds_credentials_to_the_existing_group_without_mutating_it():
    first = compile_declaration({"provider": {"preset": "gemini"}, "tier": "paid", "group_mode": "single_group", "group_prefix": "gemini-paid", "keys": gk(1)}, EMPTY)
    base = merge_fragment(EMPTY, first.fragment)
    before = copy.deepcopy(base)
    r = compile_declaration({"provider": {"preset": "gemini"}, "tier": "paid", "group_mode": "single_group", "group_prefix": "gemini-paid", "keys": gk(2)}, base)
    assert r.valid and base == before
    assert r.preview["groups"][0]["existing"] is True and r.preview["groups"][0]["deployments"] == []
    merged = merge_fragment(base, r.fragment)
    cred_ids = [c["id"] for c in merged["groups"][0]["credentials"]]
    assert cred_ids == ["gemini-paid-k1", "gemini-paid-k2"] and len(merged["deployments"]) == len(base["deployments"])


def test_clone_from_group_copies_policy_limits_and_models_and_only_needs_new_keys():
    r = compile_declaration({"clone_from_group": "gemini-free-b", "keys": f"{gk(11)}, {gk(12)}"}, CFG)
    assert r.valid, r.errors  # gemini-free-b đã có risk_ack trong cấu hình mẫu nên bản sao thừa hưởng xác nhận
    g = r.fragment["groups"][0]
    assert g["tier"] == "free" and g["data_policy"] == "may_train" and g["id"].startswith("gemini-free-")
    assert {"multi_account_risk", "no_eea_uk_ch"} <= set(g["tos_flags"]) and g["allowed_gates"] == ["dev", "A"]
    src_dep = next(d for d in CFG["deployments"] if d["group"] == "gemini-free-b")
    new_dep = r.fragment["deployments"][0]
    assert new_dep["model"] == src_dep["model"] and new_dep["limits"] == src_dep["limits"] and new_dep["group"] == g["id"] and r.fragment["models"] == []
    assert not list(POOL_VALID.iter_errors(merge_fragment(CFG, r.fragment)))
    assert not compile_declaration({"clone_from_group": "khong-co", "keys": gk(1)}, CFG).valid


def test_conservative_defaults_and_warnings_when_the_operator_does_not_know_the_numbers():
    decl = {"provider": {"preset": "custom", "id": "provider-d", "base_url": "https://api.d.example/v1"}, "tier": "free", "keys": "ak_example_d_00000000001",
            "models": [{"model_id": "some-model"}]}
    r = compile_declaration(decl, EMPTY)
    assert r.valid
    g = r.fragment["groups"][0]
    assert g["limits"] == {**CONSERVATIVE_LIMITS} and g["data_policy"] == "unknown"
    m = r.fragment["models"][0]
    assert (m["ctx_in"], m["max_out"], m["structured"]) == (32768, 4096, "none")
    text = " | ".join(r.warnings)
    assert "bảo thủ" in text and "data_policy" in text and "ctx_in" in text and "probe" in text
    assert not list(POOL_VALID.iter_errors(merge_fragment(EMPTY, r.fragment)))


def test_paid_tier_without_price_key_warns_that_real_cost_would_be_zero():
    r = compile_declaration({"provider": {"preset": "custom", "id": "provider-p", "base_url": "https://api.p.example/v1"}, "tier": "paid", "data_policy": "no_training",
                             "keys": "ak_example_paid_000000001", "models": [{"model_id": "m", "ctx_in": 65536, "max_out": 4096}]}, EMPTY)
    assert r.valid and any("price_key" in w for w in r.warnings)


@pytest.mark.parametrize("decl,needle", [
    ({"provider": {"preset": "custom", "id": "p-x"}, "tier": "free", "keys": "ak_example_x_0000000001", "models": [{"model_id": "m"}]}, "base_url"),
    ({"provider": {"preset": "custom", "id": "p-x", "base_url": "http://insecure.example"}, "tier": "free", "keys": "ak_example_x_0000000001", "models": [{"model_id": "m"}]}, "https"),
    ({"provider": {"preset": "custom", "base_url": "https://a.example/v1"}, "tier": "free", "keys": "ak_example_x_0000000001", "models": [{"model_id": "m"}]}, "provider.id"),
    ({"provider": {"preset": "custom", "id": "p-x", "base_url": "https://a.example/v1"}, "tier": "free", "keys": "ak_example_x_0000000001"}, "models"),
    ({"provider": {"preset": "gemini"}, "tier": "unlimited", "keys": gk(1)}, "tier"),
    ({"provider": {"preset": "gemini"}, "tier": "free", "keys": ""}, "chưa có khoá"),
    ({"provider": {"preset": "gemini"}, "tier": "free", "keys": "DÁN_KHOÁ_1, your_api_key_here_000"}, "không có khoá nào hợp lệ"),
    ({"provider": {"preset": "gemini"}, "tier": "free", "keys": "sk-" + "aB3" * 12}, "không có khoá nào hợp lệ"),
])
def test_invalid_declarations_fail_with_a_clear_reason_and_no_secrets(decl, needle):
    r = compile_declaration(decl, EMPTY)
    assert not r.valid and needle in " ".join(r.errors), r.errors
    assert r.secrets == {}


def test_skipped_keys_are_reported_with_reason_but_good_keys_still_go_through():
    r = compile_declaration({"provider": {"preset": "gemini"}, "tier": "free", "keys": f"{gk(1)}, bad key, tooshort, {gk(1)}"}, EMPTY)
    assert r.valid and len(r.secrets) == 1
    st = [k["status"] for k in r.preview["skipped_keys"]]
    assert "too_short" in st and "duplicate_in_request" in st
    assert leaked(r.preview, [gk(1)]) == []


def test_duplicate_of_a_key_already_in_the_system_is_skipped():
    fp = lambda s: fingerprint(MASTER, s)  # noqa: E731
    r = compile_declaration({"provider": {"preset": "gemini"}, "tier": "free", "keys": f"{gk(1)}, {gk(2)}"}, EMPTY, fingerprint_fn=fp, existing_fingerprints={fp(gk(1))})
    assert r.valid and list(r.secrets.values()) == [gk(2)]
    assert [k["status"] for k in r.preview["skipped_keys"]] == ["duplicate_existing"]


def test_the_same_key_in_two_declarations_is_an_error():
    doc = {"version": 1, "declarations": [
        {"provider": {"preset": "gemini"}, "tier": "free", "keys": gk(1)},
        {"provider": {"preset": "gemini"}, "tier": "paid", "keys": gk(1), "group_prefix": "gemini-paid", "group_mode": "single_group"}]}
    r = compile_all(doc, EMPTY)
    assert not r.valid and any("hai khai báo" in e for e in r.errors)


def test_deployments_from_a_declaration_serve_only_after_quality_is_known():
    doc = {"version": 1, "declarations": [{"provider": {"preset": "gemini"}, "tier": "paid", "group_mode": "single_group", "group_prefix": "gemini-paid", "keys": gk(1)}]}
    r = compile_all(doc, EMPTY)
    assert r.valid and any("probe" in w for w in r.warnings)
    cfg = merge_fragment(EMPTY, r.fragment)
    router = Router(load_pool_model(cfg), seed=1)
    out = router.acquire(Request("writer", 5_000, 500, gate="A"), T0)
    assert not isinstance(out, Lease)  # chưa kiểm định: không vào profile đòi chất lượng
    cfg["models"][0]["quality"] = {"json": 0.96, "vi_write": 0.85, "long_context": 0.9}  # sau probe (hoặc người vận hành tự khai)
    out = Router(load_pool_model(cfg), seed=1).acquire(Request("writer", 5_000, 500, gate="A"), T0)
    assert isinstance(out, Lease) and out.deployment.group.id == "gemini-paid"


def test_example_pool_is_usable_end_to_end_and_risky_groups_drop_out_at_public_gates():
    cfg = merge_fragment(EMPTY, compile_all(EXAMPLE, EMPTY).fragment)
    router = Router(load_pool_model(cfg), seed=3)
    used_a = {router.acquire(Request("writer", 5_000, 500, gate="A"), T0 + i).deployment.group.id for i in range(0, 6)}
    assert used_a and all(g.startswith("gemini-free-acc") for g in used_a)  # cổng A: ưu tiên các tài khoản miễn phí
    out = router.acquire(Request("writer", 5_000, 500, gate="B", privacy="private"), T0)
    assert isinstance(out, Lease) and out.deployment.group.id == "gemini-paid"  # cổng B và riêng tư: chỉ nhóm trả phí


def test_template_is_schema_valid_but_refuses_to_run_until_placeholders_are_replaced():
    assert not list(DECL_VALID.iter_errors(TEMPLATE)), [e.message for e in DECL_VALID.iter_errors(TEMPLATE)][:3]
    assert len(TEMPLATE["declarations"]) == 4
    r = compile_all(TEMPLATE, EMPTY)
    assert not r.valid and r.secrets == {}
    assert sum("không có khoá nào hợp lệ" in e for e in r.errors) >= 3  # mẫu chưa điền: mọi khối đều bị từ chối vì khoá giữ chỗ


def test_declaration_schema_rejects_typos_and_unknown_fields():
    bad = copy.deepcopy(EXAMPLE)
    bad["declarations"][0]["tier"] = "mien-phi"
    assert list(DECL_VALID.iter_errors(bad))
    bad = copy.deepcopy(EXAMPLE)
    bad["declarations"][0]["api_keys"] = "x"
    assert list(DECL_VALID.iter_errors(bad))
    bad = copy.deepcopy(EXAMPLE)
    del bad["declarations"][0]["keys"]
    assert list(DECL_VALID.iter_errors(bad))
    bad = copy.deepcopy(EXAMPLE)
    bad["declarations"][1]["provider"]["base_url"] = "http://x.example"
    assert list(DECL_VALID.iter_errors(bad))


# --------------------------------------------------------------------------- mã hoá khoá
def test_encrypt_decrypt_roundtrip_and_ciphertext_hides_the_key():
    blob = encrypt_secret(MASTER, "gemini-free-acc1-k1", gk(1))
    assert gk(1).encode() not in blob and decrypt_secret(MASTER, "gemini-free-acc1-k1", blob) == gk(1)
    assert encrypt_secret(MASTER, "x-k1", gk(1)) != encrypt_secret(MASTER, "x-k1", gk(1))  # nonce ngẫu nhiên


def test_ciphertext_is_bound_to_its_credential_id_master_key_and_integrity():
    blob = encrypt_secret(MASTER, "a-k1", gk(1))
    with pytest.raises(InvalidTag):
        decrypt_secret(MASTER, "b-k1", blob)  # gắn bản mã sang bản ghi khác bị từ chối (AAD)
    with pytest.raises(InvalidTag):
        decrypt_secret(bytes(reversed(range(32))), "a-k1", blob)
    tampered = bytearray(blob)
    tampered[-1] ^= 1
    with pytest.raises(InvalidTag):
        decrypt_secret(MASTER, "a-k1", bytes(tampered))


def test_fingerprint_is_stable_keyed_and_does_not_reveal_the_key():
    f = fingerprint(MASTER, gk(1))
    assert f == fingerprint(MASTER, gk(1)) and f != fingerprint(MASTER, gk(2)) and f != fingerprint(bytes(reversed(range(32))), gk(1))
    assert len(f) == 32 and gk(1) not in f


def test_last4_and_mask_never_reveal_short_secrets():
    assert last4(gk(1)) == "0001" and mask(gk(1)) == "\u20260001"
    assert last4("abc") == "****" and last4("abcdefg") == "****"


def test_master_key_formats_and_minimum_length():
    k = new_master_key()
    assert len(load_master_key(k)) == 32 and load_master_key(bytes(32).hex()) == bytes(32)
    with pytest.raises(ValueError):
        load_master_key("dai-qua-ngan")
    with pytest.raises(ValueError):
        encrypt_secret(b"short", "id", "secret-value-123456")
