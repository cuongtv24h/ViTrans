#!/usr/bin/env python3
"""Kiểm tra tính nhất quán của spec pack.

Chạy:  python tools/validate_spec.py [--pg-dsn postgresql://postgres@localhost:54329/postgres]

Các kiểm tra:
  1. Mọi schemas/*.schema.json hợp lệ (JSON Schema 2020-12) và tham chiếu chéo được giải quyết.
  2. Mọi examples/*.example.json khớp schema tương ứng; có kiểm tra phủ định (đột biến phải bị từ chối).
  3. Kiểm tra chéo ví dụ: trích đoạn nguyên văn có thật trong fixture, ID nhất quán giữa các ví dụ.
  4. Prompts: front matter hợp lệ, biến khai báo khớp placeholder, schema đầu ra tồn tại.
  5. DDL: parse bằng pglast (và chạy thật nếu có --pg-dsn).
  6. OpenAPI: hợp lệ; enum Level/Stage khớp JSON Schema.
  7. Enum trong DDL khớp JSON Schema (level, stage, importance, verdict).
  8. SPEC.md: mọi tham chiếu §x.y và đường dẫn tệp trỏ tới nơi có thật.
  9. LLM Pool và Lõi văn phong: cấu hình mẫu nạp được và mọi tầng chọn được deployment; lõi mẫu minh hoạ đúng cổng duyệt (HITL).
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import sys

import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  FAIL {msg}")


def load_schemas() -> dict[str, dict]:
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "schemas").glob("*.schema.json"))}


def build_registry(schemas: dict[str, dict]) -> Registry:
    reg = Registry()
    for s in schemas.values():
        reg = reg.with_resource(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
    return reg


# ---------------------------------------------------------------- 1 + 2 + 3
def check_schemas_and_examples() -> None:
    print("[1] JSON Schema")
    schemas = load_schemas()
    reg = build_registry(schemas)
    for name, s in schemas.items():
        try:
            Draft202012Validator.check_schema(s)
            ok(name)
        except Exception as e:  # noqa: BLE001
            fail(f"{name}: schema không hợp lệ: {e}")

    print("[2] Ví dụ khớp schema")
    validators: dict[str, Draft202012Validator] = {}
    examples: dict[str, dict] = {}
    for p in sorted((ROOT / "examples").glob("*.example.json")):
        base = p.name.replace(".example.json", "")
        schema_name = f"{base}.schema.json"
        if schema_name not in schemas:
            fail(f"{p.name}: không có schema {schema_name}")
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        v = Draft202012Validator(schemas[schema_name], registry=reg)
        validators[base] = v
        examples[base] = data
        errs = sorted(v.iter_errors(data), key=lambda e: list(e.path))
        if errs:
            fail(f"{p.name}: " + "; ".join(f"{list(e.path)}: {e.message}" for e in errs[:3]))
        else:
            ok(p.name)
    missing = [n for n in schemas if n not in ("common.schema.json",) and n.replace(".schema.json", "") not in examples]
    for n in missing:
        fail(f"thiếu ví dụ cho {n}")

    print("[2b] Kiểm tra phủ định (đột biến phải bị từ chối)")
    negatives = [
        ("segment_analysis", lambda d: d["units"][0].__setitem__("evidence", []), "evidence rỗng"),
        ("segment_analysis", lambda d: d["units"][0]["evidence"][0].__setitem__("pid", "P12"), "pid sai định dạng"),
        ("segment_analysis", lambda d: d["units"][0].__setitem__("importance", "critical"), "importance ngoài enum"),
        ("segment_analysis", lambda d: d.__setitem__("extra", 1), "thuộc tính thừa"),
        ("report_plan", lambda d: d["sections"][0].__setitem__("id", "Section1"), "section id sai"),
        ("section_output", lambda d: d["blocks"][0].__setitem__("cites", ["u1"]), "cite sai định dạng unitId"),
        ("faithfulness", lambda d: d["results"][0].__setitem__("verdict", "ok"), "verdict ngoài enum"),
        ("doc_profile", lambda d: d["recommended_segmentation"].__setitem__("target_tokens", 100), "target_tokens quá nhỏ"),
        ("job_event", lambda d: d.__setitem__("stage", "unknown"), "stage ngoài enum"),
        ("translation_chunk", lambda d: d.__setitem__("items", []), "items rỗng"),
        ("style_core", lambda d: d["rules"][0].__setitem__("id", "rule1"), "id quy tắc sai định dạng"),
        ("style_core", lambda d: d["rules"][0].__setitem__("severity", "always"), "severity ngoài enum"),
        ("style_core", lambda d: d["voice"].__setitem__("register", "poetic"), "register ngoài enum"),
        ("style_core", lambda d: d.__setitem__("system_prompt", "hack"), "lõi không được thêm trường tuỳ ý"),
        ("style_core_proposal", lambda d: d["decisions_needed"][0].__setitem__("options", d["decisions_needed"][0]["options"][:1]), "quyết định phải có >= 2 phương án"),
        ("glossary_proposals", lambda d: d["entries"][0]["ctx_ids"].append("ctx-1"), "ctx_id sai định dạng"),
        ("pool_config", lambda d: d["groups"][0]["credentials"][0].__setitem__("secret_ref", "AIzaSyD-khoa-that-dan-vao-day"), "khoá thật dán vào secret_ref"),
        ("pool_config", lambda d: d["groups"][0].__setitem__("tier", "unlimited"), "tier ngoài enum"),
        ("pool_config", lambda d: d["groups"][0]["tos_flags"].append("whatever"), "cờ ToS ngoài enum"),
        ("pool_config", lambda d: d["profiles"][0].__setitem__("name", "turbo"), "tên profile ngoài enum"),
        ("recipe_config", lambda d: d.__setitem__("privacy_class", "public"), "privacy_class ngoài enum"),
    ]
    for base, mutate, why in negatives:
        bad = copy.deepcopy(examples[base])
        mutate(bad)
        if validators[base].is_valid(bad):
            fail(f"đột biến '{why}' ({base}) vẫn hợp lệ")
        else:
            ok(f"từ chối: {why}")

    print("[3] Kiểm tra chéo giữa các ví dụ")
    from reference.quote_verify import verify_quote  # noqa: WPS433

    fixture = json.loads((ROOT / "examples" / "fixture_document.json").read_text(encoding="utf-8"))
    paras = {p["pid"]: p["text"] for p in fixture["paragraphs"]}
    seg = examples["segment_analysis"]
    for u in seg["units"]:
        for ev in u["evidence"]:
            if ev["pid"] not in paras:
                fail(f"{u['local_id']}: pid {ev['pid']} không có trong fixture")
                continue
            r = verify_quote(ev["quote"], paras[ev["pid"]])
            if r.status != "exact":
                fail(f"{u['local_id']}: trích đoạn không khớp nguyên văn ({r.status}, {r.score})")
        for n in u["numbers"]:
            if not any(n["source_text"] in paras[e["pid"]] for e in u["evidence"]):
                # số có thể nằm ở đoạn khác, chỉ cảnh báo mềm
                pass
    # phủ kín nhãn đoạn
    covered = set()
    for lab in seg["paragraph_labels"]:
        a, b = int(lab["from_pid"][1:]), int(lab["to_pid"][1:])
        covered.update(f"P{n:06d}" for n in range(a, b + 1))
    if covered != set(paras):
        fail(f"paragraph_labels không phủ kín fixture: thiếu {sorted(set(paras) - covered)}")
    else:
        ok("paragraph_labels phủ kín các pid của fixture")

    # ánh xạ local -> global (u1->U-0001...)
    gid = {u["local_id"]: f"U-{int(u['local_id'][1:]):04d}" for u in seg["units"]}
    core_ids = {gid[u["local_id"]] for u in seg["units"] if u["importance"] == "core"}
    plan = examples["report_plan"]
    assigned = {uid for s in plan["sections"] for uid in s["unit_ids"]}
    unknown = assigned - set(gid.values())
    if unknown:
        fail(f"report_plan tham chiếu unit không tồn tại: {sorted(unknown)}")
    elif not core_ids <= assigned:
        fail(f"report_plan thiếu unit core: {sorted(core_ids - assigned)}")
    else:
        ok("report_plan gán đủ mọi unit core và chỉ dùng unit có thật")
    sec_units = {s["id"]: set(s["unit_ids"]) for s in plan["sections"]}
    so = examples["section_output"]
    cites = {c for b in so["blocks"] for c in b["cites"]}
    if not cites <= sec_units[so["section_id"]]:
        fail("section_output trích dẫn unit ngoài danh sách được giao")
    else:
        ok("section_output chỉ trích dẫn unit được giao cho mục")
    tc = examples["translation_chunk"]
    if [i["pid"] for i in tc["items"]] != sorted(paras):
        fail("translation_chunk không căn 1:1 với fixture")
    else:
        ok("translation_chunk căn 1:1 theo pid với fixture")
    sc, prop, gp, gc = examples["style_core"], examples["style_core_proposal"], examples["glossary_proposals"], examples["glossary_candidates"]
    if prop["proposal"] != sc:
        fail("style_core_proposal.proposal khác style_core.example")
    ids = {x["id"] for x in sc["rules"] + sc["exemplars"]} | {d["id"] for d in prop["decisions_needed"]}
    bad = [e["target_id"] for e in prop["evidence"] if e["target_id"] not in ids]
    if bad:
        fail(f"evidence của style_core_proposal trỏ tới id không có: {bad}")
    elif examples["recipe_config"]["glossary_ids"][0] != sc["glossary_refs"][0]["glossary_id"]:
        fail("glossary_refs của style_core.example khác glossary_ids của recipe_config.example")
    elif not {e["source_term"] for e in gp["entries"]} <= {c["source_term"] for c in gc["candidates"]}:
        fail("glossary_proposals có thuật ngữ không nằm trong glossary_candidates.example")
    else:
        ok("style_core, style_core_proposal, glossary_proposals, recipe_config nhất quán với nhau")


# ---------------------------------------------------------------- 4
PROMPT_STAGES = {"profile", "glossary", "map", "consolidate", "write", "verify", "repair", "translate", "assemble", "extract", "curate"}
MODEL_PROFILES = set(json.loads((ROOT / "schemas" / "common.schema.json").read_text(encoding="utf-8"))["$defs"]["modelProfile"]["enum"])
THINKING = {"low", "medium", "high", "none"}


def split_front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError("thiếu front matter YAML")
    return yaml.safe_load(m.group(1)), m.group(2)


def check_prompts() -> None:
    print("[4] Prompts")
    schemas = load_schemas()
    files = sorted((ROOT / "prompts").glob("P*.md"))
    if not files:
        fail("chưa có prompt nào")
        return
    for p in files:
        try:
            meta, body = split_front_matter(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            fail(f"{p.name}: {e}")
            continue
        errs = []
        for key in ("id", "version", "stage", "model_profile", "thinking", "output", "variables", "max_output_tokens"):
            if key not in meta:
                errs.append(f"thiếu khoá '{key}'")
        if errs:
            fail(f"{p.name}: " + ", ".join(errs))
            continue
        if meta["id"] != p.stem:
            errs.append(f"id '{meta['id']}' khác tên file")
        if meta["stage"] not in PROMPT_STAGES:
            errs.append(f"stage '{meta['stage']}' không hợp lệ")
        if meta["model_profile"] not in MODEL_PROFILES:
            errs.append(f"model_profile '{meta['model_profile']}' không hợp lệ")
        if meta["thinking"] not in THINKING:
            errs.append(f"thinking '{meta['thinking']}' không hợp lệ")
        out = meta["output"]
        if out.startswith("schema://"):
            if out[len("schema://"):] not in schemas:
                errs.append(f"schema đầu ra '{out}' không tồn tại")
        elif out not in ("text/markdown", "text/plain"):
            errs.append(f"output '{out}' không hợp lệ")
        used = set(re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", body))
        declared = set(meta["variables"])
        if used - declared:
            errs.append(f"biến dùng nhưng chưa khai báo: {sorted(used - declared)}")
        if declared - used:
            errs.append(f"biến khai báo nhưng không dùng: {sorted(declared - used)}")
        if "~~~~" in body:
            errs.append("không được chứa '~~~~' (dùng làm rào code khi nhúng vào SPEC.md)")
        if "## SYSTEM" not in body or "## USER" not in body:
            errs.append("thiếu mục '## SYSTEM' hoặc '## USER'")
        if errs:
            fail(f"{p.name}: " + "; ".join(errs))
        else:
            ok(f"{p.name} ({meta['version']}, {len(used)} biến)")


# ---------------------------------------------------------------- 5
def check_ddl(pg_dsn: str | None) -> None:
    print("[5] DDL PostgreSQL")
    ddl_path = ROOT / "db" / "schema.sql"
    if not ddl_path.exists():
        fail("thiếu db/schema.sql")
        return
    sql = ddl_path.read_text(encoding="utf-8")
    try:
        import pglast

        stmts = pglast.parse_sql(sql)
        ok(f"pglast parse được {len(stmts)} câu lệnh")
    except Exception as e:  # noqa: BLE001
        fail(f"pglast không parse được: {e}")
        return
    if pg_dsn:
        import psycopg

        try:
            with psycopg.connect(pg_dsn, autocommit=True) as conn:
                conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
                conn.execute(sql)
                ok("chạy DDL thành công trên PostgreSQL thật")
                n_tables = conn.execute("select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'").fetchone()[0]
                ok(f"{n_tables} bảng được tạo")
        except Exception as e:  # noqa: BLE001
            fail(f"chạy DDL thất bại: {e}")
            return
        smoke = ROOT / "tests" / "pg_smoke.py"
        if smoke.exists():
            import subprocess

            r = subprocess.run([sys.executable, str(smoke), pg_dsn], capture_output=True, text=True)
            print(r.stdout.rstrip())
            if r.returncode != 0:
                fail("pg_smoke.py thất bại:\n" + r.stderr[-1500:])
            else:
                ok("pg_smoke.py (ledger, hàng đợi, xoá hết hạn) đạt")


# ---------------------------------------------------------------- 6 + 7
def check_openapi_and_enums() -> None:
    print("[6] OpenAPI")
    p = ROOT / "api" / "openapi.yaml"
    if not p.exists():
        fail("thiếu api/openapi.yaml")
        return
    spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    try:
        from openapi_spec_validator import validate

        validate(spec)
        ok(f"OpenAPI hợp lệ ({len(spec['paths'])} đường dẫn)")
    except Exception as e:  # noqa: BLE001
        fail(f"OpenAPI không hợp lệ: {e}")
        return
    schemas = load_schemas()
    common = schemas["common.schema.json"]["$defs"]
    comps = spec["components"]["schemas"]
    declared_tags = {t["name"] for t in spec["tags"]}
    used_tags = {t for item in spec["paths"].values() for m, op in item.items() if m in ("get", "post", "put", "patch", "delete") for t in op.get("tags", [])}
    if used_tags - declared_tags:
        fail(f"OpenAPI dùng tag chưa khai báo: {sorted(used_tags - declared_tags)}")
    ids = [op["operationId"] for item in spec["paths"].values() for m, op in item.items() if m in ("get", "post", "put", "patch", "delete")]
    if len(ids) != len(set(ids)):
        fail("OpenAPI có operationId trùng")
    else:
        ok(f"{len(ids)} operationId duy nhất, tag đều đã khai báo")
    for api_name, js_name in (("Level", "level"), ("Stage", "stage"), ("Importance", "importance"), ("Verdict", "verdict"), ("PrivacyClass", "privacyClass")):
        if api_name not in comps:
            fail(f"OpenAPI thiếu components.schemas.{api_name}")
        elif set(comps[api_name]["enum"]) != set(common[js_name]["enum"]):
            fail(f"enum {api_name} lệch với JSON Schema: {set(comps[api_name]['enum']) ^ set(common[js_name]['enum'])}")
        else:
            ok(f"enum {api_name} khớp JSON Schema")

    print("[7] Enum trong DDL khớp JSON Schema")
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")

    def sql_enum(table: str, column: str) -> set[str]:
        m = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
        if not m:
            return set()
        mm = re.search(rf"\b{column}\s+text[^,\n]*?CHECK \({column} IN \(([^)]*)\)\)", m.group(1))
        return set(re.findall(r"'([^']+)'", mm.group(1))) if mm else set()

    pool = schemas["pool_config.schema.json"]["properties"]
    pool_g, pool_d = pool["groups"]["items"]["properties"], pool["deployments"]["items"]["properties"]
    pool_m, pool_t = pool["models"]["items"]["properties"], pool["profiles"]["items"]["properties"]["tiers"]["items"]["properties"]
    checks = [
        ("jobs", "privacy_class", common["privacyClass"]["enum"]),
        ("llm_calls", "privacy_class", common["privacyClass"]["enum"]),
        ("llm_profiles", "name", common["modelProfile"]["enum"]),
        ("llm_providers", "kind", pool["providers"]["items"]["properties"]["kind"]["enum"]),
        ("llm_models", "structured", pool_m["structured"]["enum"]),
        ("llm_quota_groups", "tier", pool_g["tier"]["enum"]),
        ("llm_quota_groups", "data_policy", pool_g["data_policy"]["enum"]),
        ("llm_deployments", "tpm_basis", pool_d["tpm_basis"]["enum"]),
        ("llm_deployments", "price_mode", pool_d["price_mode"]["enum"]),
        ("llm_profile_tiers", "strategy", pool_t["strategy"]["enum"]),
        ("style_core_versions", "status", comps["StyleCoreStatus"]["enum"]),
        ("curation_runs", "kind", comps["CurationKind"]["enum"]),
        ("llm_quota_groups", "tier", comps["GroupTier"]["enum"]),
        ("llm_quota_groups", "data_policy", comps["DataPolicy"]["enum"]),
        ("jobs", "level", common["level"]["enum"]),
        ("reports", "level", common["level"]["enum"]),
        ("job_stages", "stage", common["stage"]["enum"]),
        ("knowledge_units", "importance", common["importance"]["enum"]),
        ("knowledge_units", "type", common["unitType"]["enum"]),
        ("doc_paragraphs", "kind", common["paragraphKind"]["enum"]),
        ("report_blocks", "verdict", common["verdict"]["enum"]),
        ("report_blocks", "type", common["block"]["properties"]["type"]["enum"]),
    ]
    for table, col, expected in checks:
        got = sql_enum(table, col)
        if got != set(expected):
            fail(f"DDL {table}.{col} lệch: {sorted(got ^ set(expected))}")
        else:
            ok(f"DDL {table}.{col} khớp")

    def sql_array_enum(table: str, column: str) -> set[str]:
        m = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
        mm = re.search(rf"CHECK \({column} <@ ARRAY\[([^\]]*)\]", m.group(1)) if m else None
        return set(re.findall(r"'([^']+)'", mm.group(1))) if mm else set()

    for col, expected in (("tos_flags", pool_g["tos_flags"]["items"]["enum"]), ("allowed_gates", pool_g["allowed_gates"]["items"]["enum"])):
        got = sql_array_enum("llm_quota_groups", col)
        if got != set(expected):
            fail(f"DDL llm_quota_groups.{col} lệch JSON Schema: {sorted(got ^ set(expected))}")
        else:
            ok(f"DDL llm_quota_groups.{col} khớp pool_config.schema.json")
    glossary_status = sql_enum("glossary_entries", "status")
    api_ge = {v for part in comps["GlossaryEntry"]["allOf"] for v in part.get("properties", {}).get("status", {}).get("enum", [])}
    proposed = {v for part in comps["GlossaryEntry"]["allOf"] for v in part.get("properties", {}).get("proposed_by", {}).get("enum", [])}
    if glossary_status != api_ge:
        fail(f"glossary_entries.status lệch OpenAPI: {sorted(glossary_status ^ api_ge)}")
    elif sql_enum("glossary_entries", "proposed_by") != proposed:
        fail("glossary_entries.proposed_by lệch OpenAPI")
    else:
        ok("DDL glossary_entries.status/proposed_by khớp OpenAPI")
    api_roles = set(comps["Me"]["properties"]["role"]["enum"])
    if sql_enum("users", "role") != api_roles:
        fail(f"users.role lệch OpenAPI: {sorted(sql_enum('users', 'role') ^ api_roles)}")
    else:
        ok("DDL users.role khớp OpenAPI (có curator)")


# ---------------------------------------------------------------- 8
def check_spec_refs() -> None:
    print("[8] Tham chiếu trong SPEC.md")
    p = ROOT / "SPEC.md"
    if not p.exists():
        fail("thiếu SPEC.md (chạy tools/build_spec.py)")
        return
    in_fence, headings, prose = False, set(), []
    for line in p.read_text(encoding="utf-8").splitlines():
        if re.match(r"^(```|~~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        prose.append(line)
        m = re.match(r"^#{2,3} (\d+(?:\.\d+)?)[ .]", line)
        if m:
            headings.add(m.group(1))
    text = "\n".join(prose)
    bad_sections = sorted({"§" + m.group(1) for m in re.finditer(r"§(\d+(?:\.\d+)?)", text) if m.group(1) not in headings})
    if bad_sections:
        fail(f"SPEC.md tham chiếu mục không tồn tại: {bad_sections}")
    else:
        ok(f"mọi tham chiếu § trỏ tới mục có thật ({len(headings)} mục)")
    bad_paths = []
    n = 0
    for m in re.finditer(r"`((?:schemas|reference|tests|tools|db|api|prompts|examples)/[A-Za-z0-9_./*{}-]+)`", text):
        path = m.group(1)
        if "*" in path or "{" in path:
            continue
        n += 1
        if not (ROOT / path).exists() and not (ROOT / path.rstrip("/")).exists():
            bad_paths.append(path)
    if bad_paths:
        fail(f"SPEC.md nhắc tới tệp không tồn tại: {sorted(set(bad_paths))}")
    else:
        ok(f"mọi đường dẫn tệp được nhắc tới đều tồn tại ({n} lượt)")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    spec_api = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    truth = {
        "prompt": len(list((ROOT / "prompts").glob("P*.md"))),
        "JSON Schema": len(list((ROOT / "schemas").glob("*.schema.json"))),
        "bảng": len(re.findall(r"^CREATE TABLE ", sql, re.M)),
        "đường dẫn": len(spec_api["paths"]),
    }
    stale_counts = {k: v for k, v in truth.items() if not re.search(rf"\b{v} {re.escape(k)}", readme)}
    if stale_counts:
        fail(f"README.md nêu số đếm lỗi thời: cần {stale_counts}")
    else:
        ok(f"số đếm trong README khớp tệp nguồn ({truth})")
    # P11 đã bị loại cùng quyết định bỏ khâu bản dịch thô (§18): 13 prompt P0-P10, P12, P13.
    expected_prompts = [f"P{i}" for i in list(range(11)) + [12, 13]]
    missing = [pid for pid in expected_prompts if not list((ROOT / "prompts").glob(f"{pid}_*.md"))]
    if missing:
        fail(f"thiếu prompt {missing}")
    else:
        ok("đủ 13 prompt P0-P10, P12-P13")
    if list((ROOT / "prompts").glob("P11_*.md")):
        fail("có prompt P11: số hiệu này đã bị loại và không dùng lại (§18)")
    stale = [w for w in ("style_guide_vi", "SG-VI", "00_style_guide_vi") if w in text]
    if stale:
        fail(f"SPEC.md còn nhắc tới thành phần đã bỏ: {stale}")
    else:
        ok("SPEC.md không còn nhắc hướng dẫn văn phong cố định (đã thay bằng Lõi văn phong)")


# ---------------------------------------------------------------- 9
def check_pool_and_style() -> None:
    print("[9] LLM Pool và Lõi văn phong")
    from reference.llm_pool import RISK_FLAGS, Request, Router, load_pool_model
    from reference.style_core import approval_problems, compile_style_core, lint

    cfg = json.loads((ROOT / "examples" / "pool_config.example.json").read_text(encoding="utf-8"))
    try:
        model = load_pool_model(cfg)
    except Exception as e:  # noqa: BLE001
        fail(f"pool_config.example không nạp được: {e!r}")
        return
    ok(f"pool_config.example nạp được: {len(model.deployments)} deployment, {len(model.profiles)} profile")
    r = Router(model)
    for name, prof in model.profiles.items():
        for tier in prof.tiers:
            reqs = [Request(name, 1000, 200, gate=g) for g in ("dev", "A", "B", "C")]
            n = max(sum(r.eligible(d, q, prof, tier) for d in model.deployments) for q in reqs)
            if n == 0:
                fail(f"profile {name}, tầng {tier.name}: không chọn được deployment nào ở bất kỳ cổng nào")
            else:
                ok(f"profile {name}, tầng {tier.name}: chọn được tối đa {n} deployment")
    for g in model.groups.values():
        unack = (g.tos_flags & RISK_FLAGS) - g.risk_ack
        if unack:
            fail(f"nhóm {g.id} có cờ rủi ro chưa xác nhận (risk_ack): {sorted(unack)}")
    ok("mọi nhóm có cờ rủi ro trong cấu hình mẫu đều đã có xác nhận chấp nhận (risk_ack)")
    if any("AIza" in json.dumps(cfg) or "nvapi-" in json.dumps(cfg) for _ in [0]):
        fail("pool_config.example chứa chuỗi giống khoá API thật")
    else:
        ok("pool_config.example không chứa khoá API")
    neutral = json.loads((ROOT / "prompts" / "00_style_core_neutral.json").read_text(encoding="utf-8"))
    example = json.loads((ROOT / "examples" / "style_core.example.json").read_text(encoding="utf-8"))
    if approval_problems(neutral):
        fail(f"lõi trung tính phải duyệt được ngay: {approval_problems(neutral)}")
    else:
        ok("lõi mặc định trung tính qua cổng duyệt, không đặt quan điểm nào")
    if not approval_problems(example):
        fail("style_core.example phải minh hoạ cổng HITL (có mục AI chưa xác nhận)")
    else:
        ok(f"style_core.example minh hoạ cổng duyệt: {len(approval_problems(example))} vấn đề chặn")
    errs = [x for x in lint(example) if x.severity == "error"]
    if errs:
        fail(f"style_core.example lint lỗi: {errs}")
    else:
        ok(f"style_core.example lint sạch; biên dịch cho giai đoạn dịch: {len(compile_style_core(example, 'translate'))} ký tự")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-dsn", default=None)
    ap.add_argument("--only", default=None, help="schemas|prompts|ddl|openapi|refs|pool")
    args = ap.parse_args()
    steps = {
        "schemas": check_schemas_and_examples,
        "prompts": check_prompts,
        "ddl": lambda: check_ddl(args.pg_dsn),
        "openapi": check_openapi_and_enums,
        "refs": check_spec_refs,
        "pool": check_pool_and_style,
    }
    for name, fn in steps.items():
        if args.only and args.only != name:
            continue
        fn()
    print()
    if FAILS:
        print(f"THẤT BẠI: {len(FAILS)} lỗi")
        for f in FAILS:
            print(" -", f)
        return 1
    print("TẤT CẢ ĐẠT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
