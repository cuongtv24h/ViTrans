import json
import pathlib
import sys
from datetime import date

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reference.estimator import Price, estimate, pick_price, report_budget_words  # noqa: E402
from reference.gemini_schema import load_store, to_wire_schema  # noqa: E402
from reference.glossary_lint import GlossaryEntry, first_use_by_section, lint  # noqa: E402
from reference.numbers_check import canon_number, extract_numbers, unverified_numbers  # noqa: E402
from reference.quote_verify import bad_quote_ratio, unit_is_verified, verify_evidence, verify_quote  # noqa: E402
from reference.textnorm import norm_for_match, strip_diacritics  # noqa: E402

FIXTURE = json.loads((ROOT / "examples" / "fixture_document.json").read_text(encoding="utf-8"))
PARAS = {p["pid"]: p["text"] for p in FIXTURE["paragraphs"]}


# ------------------------------------------------------------------ textnorm
def test_norm_handles_nfd_and_typographic_quotes():
    nfd = "Trung tâm Thái dương"  # có thể ở dạng NFD hay NFC
    import unicodedata

    decomposed = unicodedata.normalize("NFD", nfd)
    assert norm_for_match(decomposed) == norm_for_match(nfd)
    assert norm_for_match("It\u2019s \u201cfine\u201d") == norm_for_match("It's \"fine\"")


def test_norm_joins_hyphenated_line_breaks():
    assert norm_for_match("trans-\nlation of text") == "translation of text"


def test_strip_diacritics_vietnamese():
    assert strip_diacritics("Đường Trung tâm Thái dương") == "Duong Trung tam Thai duong"


# ------------------------------------------------------------------ quote_verify
def test_exact_quote_from_fixture():
    r = verify_quote("The Solar Plexus is the center of emotional awareness.", PARAS["P000102"])
    assert r.status == "exact"


def test_quote_with_different_case_quotes_and_spacing_is_exact():
    r = verify_quote("the  solar plexus is the CENTER of emotional awareness", PARAS["P000102"])
    assert r.status == "exact"


def test_quote_differing_only_in_punctuation_is_loose():
    r = verify_quote("Gate 55 and Gate 49 are connected through genetic transmission;", PARAS["P000103"])
    assert r.status in ("exact", "loose")


def test_fuzzy_quote_with_ocr_noise_only_when_allowed():
    q = "Gate 55 and Gate 49 are connectad through genetic transmisslon: what is carried in Gate 55"
    assert verify_quote(q, PARAS["P000103"]).status == "missing"  # mặc định nghiêm ngặt
    assert verify_quote(q, PARAS["P000103"], allow_fuzzy=True).status == "fuzzy"  # tài liệu OCR


def test_short_wrong_quote_is_missing_not_fuzzy():
    assert verify_quote("Gate 56", PARAS["P000103"]).status == "missing"


def test_fabricated_quote_is_missing():
    r = verify_quote("The Sleeping Phoenix cycle begins in 2031", PARAS["P000105"])
    assert r.status == "missing"


def test_fabricated_digit_is_rejected_even_when_fuzzy_allowed():
    # Khớp mờ thuần tuý sẽ chấp nhận (độ giống ~97%) -> phải có chốt chặn chữ số.
    r = verify_quote("The Sleeping Phoenix cycle begins in 2031", PARAS["P000105"], allow_fuzzy=True)
    assert r.status == "missing"
    r2 = verify_quote("Gate 55 and Gate 99 are connected through genetic transmission", PARAS["P000103"], allow_fuzzy=True)
    assert r2.status == "missing"


def test_verify_evidence_unknown_pid_and_unit_verification():
    ev = [{"pid": "P999999", "quote": "anything at all here"}, {"pid": "P000105", "quote": "The Sleeping Phoenix cycle begins in 2027"}]
    rs = verify_evidence(ev, PARAS)
    assert [r.status for r in rs] == ["unknown_pid", "exact"]
    assert unit_is_verified(rs)
    assert not unit_is_verified(verify_evidence(ev[:1], PARAS))
    assert bad_quote_ratio([rs]) == 0.5
    rs2 = verify_evidence([{"pid": "P000103", "quote": "Gate 55 and Gate 49 are connectad through genetic transmisslon: what is carried"}], PARAS, allow_fuzzy=True)
    assert rs2[0].status == "fuzzy"


# ------------------------------------------------------------------ glossary_lint
SOLAR = GlossaryEntry("Solar Plexus", "Trung tâm Thái dương", keep_original=True, forbidden_variants=("Đám rối thần kinh mặt trời",))
GATE = GlossaryEntry("Gate", "Cổng")
PHOENIX = GlossaryEntry("Sleeping Phoenix", "Sleeping Phoenix")


def kinds(issues):
    return sorted(i.kind for i in issues)


def test_lint_clean_text_first_use_ok():
    text = "Trung tâm Thái dương (Solar Plexus) vận hành theo sóng. Cổng 55 nối với Cổng 49."
    assert lint(text, [SOLAR, GATE], first_use_terms={"Solar Plexus"}) == []


def test_lint_missing_original_on_first_use():
    text = "Trung tâm Thái dương vận hành theo sóng."
    assert kinds(lint(text, [SOLAR], first_use_terms={"Solar Plexus"})) == ["missing_original_on_first_use"]


def test_lint_not_first_use_does_not_require_parenthesis():
    assert lint("Trung tâm Thái dương vận hành theo sóng.", [SOLAR], first_use_terms=set()) == []


def test_lint_forbidden_variant():
    text = "Đám rối thần kinh mặt trời vận hành theo sóng."
    assert "forbidden_variant" in kinds(lint(text, [SOLAR]))


def test_lint_untranslated_source_term_but_parenthetical_allowed():
    assert kinds(lint("Solar Plexus vận hành theo sóng.", [SOLAR])) == ["untranslated_source_term"]
    assert lint("Trung tâm Thái dương (Solar Plexus) vận hành.", [SOLAR], first_use_terms={"Solar Plexus"}) == []


def test_lint_same_source_and_target_is_ok():
    assert lint("Chu kỳ Sleeping Phoenix bắt đầu năm 2027.", [PHOENIX]) == []


def test_lint_handles_nfd_input():
    import unicodedata

    text = unicodedata.normalize("NFD", "Đám rối thần kinh mặt trời vận hành.")
    assert "forbidden_variant" in kinds(lint(text, [SOLAR]))


def test_first_use_by_section_assigns_only_first_section():
    order = ["S01", "S02", "S03"]
    terms = {"S01": {"Solar Plexus"}, "S02": {"Solar Plexus", "Gate"}, "S03": {"Gate"}}
    res = first_use_by_section(order, terms, [SOLAR, GlossaryEntry("Gate", "Cổng", keep_original=True)])
    assert res == {"S01": {"Solar Plexus"}, "S02": {"Gate"}, "S03": set()}


# ------------------------------------------------------------------ numbers_check
@pytest.mark.parametrize(
    "raw,expected",
    [("1,000", "1000"), ("1.000", "1000"), ("3,5", "3.5"), ("3.50", "3.5"), ("1.234.567", "1234567"), ("0.5", "0.5"), ("2027", "2027"), ("1,234.56", "1234.56"), ("1.234,56", "1234.56")],
)
def test_canon_number(raw, expected):
    assert canon_number(raw) == expected


def test_extract_numbers_ignores_list_markers_and_internal_ids():
    text = "1. Cổng 55 [U-0042] nối với Cổng 49 vào năm 2027.\n2. Ghi chú S03.b01"
    assert extract_numbers(text) == ["55", "49", "2027"]


def test_unverified_numbers_flags_wrong_year():
    report = "Chu kỳ Sleeping Phoenix bắt đầu vào năm 2031; Cổng 55 và Cổng 49."
    assert unverified_numbers(report, PARAS.values()) == ["2031"]


def test_unverified_numbers_accepts_number_words_in_source():
    assert unverified_numbers("Có 2 trung tâm.", ["There are two centers."]) == []


def test_unverified_numbers_equivalent_formats():
    assert unverified_numbers("Doanh thu 1.000 USD", ["Revenue was 1,000 USD"]) == []


# ------------------------------------------------------------------ estimator
PRICES = [
    Price("gemini-3.8-flash", date(2026, 9, 2), 0.75, 3.75, 0.075),
    Price("gemini-3.8-flash", date(2027, 1, 1), 1.50, 7.50, 0.15),
]


def test_pick_price_is_effective_dated():
    assert pick_price(PRICES, "gemini-3.8-flash", date(2026, 12, 31)).input_per_mtok == 0.75
    assert pick_price(PRICES, "gemini-3.8-flash", date(2027, 1, 1)).input_per_mtok == 1.50
    with pytest.raises(LookupError):
        pick_price(PRICES, "gemini-3.8-flash", date(2026, 1, 1))


def test_estimate_deep_synthesis_300_pages_is_about_a_dollar():
    e = estimate(90_000, "deep_synthesis", PRICES[0])
    assert 0.6 <= e.cost_usd <= 1.2
    assert e.credits == 300
    assert 4 <= e.minutes_low < e.minutes_high <= 30


def test_estimate_price_doubling_doubles_cost():
    a = estimate(90_000, "deep_synthesis", PRICES[0]).cost_usd
    b = estimate(90_000, "deep_synthesis", PRICES[1]).cost_usd
    assert b == pytest.approx(2 * a, rel=0.01)


def test_report_budget_words_clamps_and_never_exceeds_source():
    assert report_budget_words(90_000, "deep_synthesis") == 9000      # kẹp trên
    assert report_budget_words(90_000, "detailed_synthesis") == 24000
    assert report_budget_words(90_000, "executive_brief") == 1500
    assert report_budget_words(3_000, "deep_synthesis") == 800        # kẹp dưới
    assert report_budget_words(300, "deep_synthesis") == 240          # không dài hơn 80% nguồn
    assert report_budget_words(50, "executive_brief") == 100 or report_budget_words(50, "executive_brief") <= 100
    assert report_budget_words(12_345, "full_translation") == 12_345


def test_estimate_level_ordering_and_credits():
    costs = {lv: estimate(90_000, lv, PRICES[0]) for lv in ("full_translation", "detailed_synthesis", "deep_synthesis", "executive_brief")}
    assert costs["executive_brief"].cost_usd < costs["deep_synthesis"].cost_usd < costs["detailed_synthesis"].cost_usd
    assert costs["executive_brief"].credits < costs["deep_synthesis"].credits < costs["full_translation"].credits < costs["detailed_synthesis"].credits
    assert estimate(10, "deep_synthesis", PRICES[0]).credits == 1


# ------------------------------------------------------------------ gemini_schema
STORE = load_store(ROOT / "schemas")
BANNED = {"$ref", "$defs", "$schema", "$id", "pattern", "minLength", "maxLength", "oneOf", "anyOf", "allOf", "uniqueItems", "const"}


def walk(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            if k in ("properties",):
                for pv in v.values():
                    yield from walk(pv)
            else:
                yield from walk(v)
    elif isinstance(node, list):
        for x in node:
            yield from walk(x)


@pytest.mark.parametrize("sid", sorted(s for s in STORE if not s.endswith("common.schema.json")))
def test_every_schema_converts_to_a_clean_wire_schema(sid):
    wire = to_wire_schema(STORE[sid], STORE)
    keys = set(walk(wire))
    assert not (keys & BANNED), keys & BANNED
    assert wire["type"] == "object" and "required" in wire


def test_wire_schema_keeps_enums_required_and_array_bounds():
    wire = to_wire_schema(STORE["https://visynth.example/schemas/segment_analysis.schema.json"], STORE)
    unit = wire["properties"]["units"]["items"]
    assert unit["properties"]["importance"]["enum"] == ["core", "supporting", "minor"]
    assert unit["properties"]["evidence"]["minItems"] == 1
    assert "pattern" not in unit["properties"]["local_id"]
    assert "evidence" in unit["required"]


def test_wire_schema_rejects_oneof():
    bad = {"$id": "https://x.example/a.json", "type": "object", "properties": {"a": {"oneOf": [{"type": "string"}, {"type": "null"}]}}}
    with pytest.raises(ValueError):
        to_wire_schema(bad, {bad["$id"]: bad})


# ------------------------------------------------------------------ dịch đầy đủ: ba chế độ, số lời gọi, dung lượng (SPEC §17-18)
from reference.estimator import docs_per_day, estimate_calls, estimate_translation  # noqa: E402


def test_postedit_full_is_not_cheaper_than_direct_translation():
    d = estimate_translation(90_000, "direct", PRICES[0])
    f = estimate_translation(90_000, "postedit_full", PRICES[0])
    assert f.tokens_out == d.tokens_out and f.tokens_in > d.tokens_in and f.cost_usd > d.cost_usd  # bản thô làm vào tăng, ra không giảm


def test_selective_postedit_saves_only_when_most_drafts_are_kept():
    d = estimate_translation(90_000, "direct", PRICES[0]).cost_usd
    assert estimate_translation(90_000, "postedit_selective", PRICES[0], keep_rate=0.7).cost_usd < 0.8 * d
    assert estimate_translation(90_000, "postedit_selective", PRICES[0], keep_rate=0.5).cost_usd < d
    assert estimate_translation(90_000, "postedit_selective", PRICES[0], keep_rate=0.05).cost_usd > d  # bản thô quá tệ: tốn hơn cả dịch thẳng
    with pytest.raises(ValueError):
        estimate_translation(100, "magic", PRICES[0])


def test_estimate_calls_scales_with_size_and_window_scale():
    a, b = estimate_calls(90_000, "deep_synthesis"), estimate_calls(9_000, "deep_synthesis")
    assert 60 <= a <= 100 and b < a
    assert estimate_calls(90_000, "deep_synthesis", window_scale=4) < a  # cửa sổ lớn hơn -> ít request hơn, nhưng chỉ giảm ít vì P4/P5 theo số mục
    assert estimate_calls(90_000, "deep_synthesis", window_scale=4, verify_batch=3) < 0.65 * a  # gộp kiểm chứng mới là đòn bẩy lớn
    assert estimate_calls(90_000, "executive_brief") < a
    assert estimate_calls(90_000, "full_translation") > 25


def test_docs_per_day_takes_the_binding_constraint():
    assert docs_per_day(237, None, 77, 500_000) == pytest.approx(237 / 77)
    assert docs_per_day(None, 9_000_000, 77, 500_000) == pytest.approx(18)
    assert docs_per_day(237, 9_000_000, 77, 500_000) == pytest.approx(237 / 77)
    assert docs_per_day(None, None, 77, 1) == float("inf")


def test_postedit_full_premium_quoted_in_the_spec_is_about_fourteen_percent():
    d = estimate_translation(90_000, "direct", PRICES[0]).cost_usd
    f = estimate_translation(90_000, "postedit_full", PRICES[0]).cost_usd
    assert 1.10 < f / d < 1.20  # SPEC §18.1 nêu "đắt hơn khoảng 14%"
    # hoà vốn của chế độ chọn lọc quanh mức giữ 20% (SPEC §18.3)
    lo = estimate_translation(90_000, "postedit_selective", PRICES[0], keep_rate=0.15).cost_usd
    hi = estimate_translation(90_000, "postedit_selective", PRICES[0], keep_rate=0.25).cost_usd
    assert lo > d > hi
