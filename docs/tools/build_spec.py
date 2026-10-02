#!/usr/bin/env python3
"""Dựng SPEC.md từ tools/spec_src/*.md + các tệp nguồn của pack.

Các bảng được SINH TỪ tệp nguồn (schema, prompt, OpenAPI, bộ ước tính) nên không thể lệch với chúng:
  <!-- TOC -->  <!-- FILE_TREE -->  <!-- SCHEMA_TABLE -->  <!-- PROMPT_TABLE -->  <!-- COST_TABLE -->
  <!-- LEVEL_BUDGET_TABLE -->  <!-- ENDPOINT_TABLE -->  <!-- PROMPTS_APPENDIX -->  <!-- VALIDATION_SUMMARY -->
  <!-- MT_COST_TABLE -->  <!-- POOL_CAPACITY_TABLE -->  <!-- POOL_SIM_TABLE -->  <!-- STYLE_COMPILE_DEMO -->  <!-- DECLARE_PREVIEW_TABLE -->
Thẻ [[N_PATHS]], [[N_TABLES]], [[N_PROMPTS]], [[N_SCHEMAS]] được thay bằng số đếm thật từ các tệp nguồn.

Chạy:  python tools/build_spec.py [--run-checks] [--pg-dsn postgresql://...]
  --run-checks : chạy pytest + validate_spec và nhúng kết quả vào Phụ lục E
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import unicodedata

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reference.estimator import LEVELS, REPORT_BUDGET, Price, estimate, estimate_calls, estimate_translation, report_budget_words  # noqa: E402
from reference.llm_pool import deployment_daily_capacity, load_pool_model  # noqa: E402
from reference.prompt_render import load_prompt  # noqa: E402
from reference.style_core import compile_style_core  # noqa: E402

LEVEL_NAMES = {
    "full_translation": "Dịch đầy đủ",
    "detailed_synthesis": "Tổng hợp chi tiết",
    "deep_synthesis": "Báo cáo chuyên sâu, mặc định",
    "executive_brief": "Tóm lược điều hành",
}

TREE_DESC = {
    "SPEC.md": "Tài liệu chính (đọc trước). Dựng tự động từ tools/spec_src + prompts",
    "README.md": "Mục lục nhanh",
    "prompts/": "Thư viện prompt P0-P13 + Lõi văn phong mặc định trung tính (nguồn sự thật của prompt)",
    "prompts/00_style_core_neutral.json": "Lõi văn phong mặc định: không đặt quan điểm nào, thay được",
    "schemas/": "JSON Schema 2020-12 cho mọi đầu ra có cấu trúc (nguồn sự thật của hợp đồng dữ liệu)",
    "examples/": "Ví dụ khớp từng schema, cùng kể một câu chuyện trên tài liệu giả lập",
    "db/": "PostgreSQL DDL + hàm nghiệp vụ",
    "db/schema.sql": "[[N_TABLES]] bảng, view, hàm: tín dụng, hàng đợi, LLM Pool (đặt chỗ nguyên tử), Lõi văn phong, phát hành glossary",
    "api/": "Đặc tả API",
    "api/openapi.yaml": "OpenAPI 3.1",
    "reference/": "Code tham chiếu cho phần tất định (không gọi LLM)",
    "reference/quote_verify.py": "Kiểm tra trích đoạn bằng chứng có thật trong nguồn",
    "reference/glossary_lint.py": "Lint thuật ngữ, tính mục dùng-đầu-tiên",
    "reference/numbers_check.py": "Kiểm tra số liệu/ngày tháng so với nguồn",
    "reference/estimator.py": "Ước tính token, chi phí, tín dụng, thời gian; ngân sách độ dài",
    "reference/gemini_schema.py": "Đổi schema đầy đủ sang wire schema cho Gemini",
    "reference/prompt_render.py": "Render prompt, định dạng biến, chống template injection",
    "reference/textnorm.py": "Chuẩn hoá NFC và so khớp",
    "reference/llm_pool.py": "Lõi định tuyến LLM Pool: token-bucket, hạn mức ngày, circuit breaker, riêng tư/ToS/cổng, chuyển tầng",
    "reference/llm_pool_pg.py": "Cầu nối PostgreSQL của pool (PgState, nhập cấu hình) dùng để so khớp SQL với bản tham chiếu",
    "reference/structured_output.py": "Bậc thang structured output cho nhà cung cấp không đồng đều, trích JSON từ phản hồi lộn xộn",
    "reference/style_core.py": "Kiểm tra, kế thừa, biên dịch Lõi văn phong thành khối prompt; cổng duyệt",
    "reference/pool_declare.py": "Khai báo nhà cung cấp và khoá: tách danh sách khoá, nhóm hạn mức, biên dịch khai báo rút gọn thành cấu hình pool",
    "reference/pool_secrets.py": "Mã hoá khoá API (AES-256-GCM), dấu vân tay chống trùng, che khoá",
    "reference/glossary_merge.py": "Gộp đề xuất thuật ngữ từ nhiều tài liệu mẫu (đầu vào của P13)",
    "tests/": "Kiểm thử tự động",
    "tests/test_reference.py": "Test cho các module tham chiếu",
    "tests/test_prompt_render.py": "Test render prompt, chống template injection",
    "tests/pg_smoke.py": "Kiểm thử hành vi DDL trên PostgreSQL thật (gồm so khớp từng bước SQL với MemoryState của pool)",
    "tests/test_pool.py": "Test pool: bucket, hạn mức ngày, cooldown, circuit, riêng tư, chuyển tầng, mô phỏng",
    "tests/pool_scenarios.py": "Kịch bản có hạt giống dùng chung cho test độ phủ và so khớp SQL",
    "tests/test_pool_declare.py": "Test khai báo khoá: tách danh sách, nhóm hạn mức, biên dịch, mã hoá, chống lộ khoá",
    "tests/test_style_core.py": "Test Lõi văn phong: biên dịch, lint chống chèn chỉ thị, kế thừa, cổng duyệt",
    "tests/test_structured_output.py": "Test bậc thang structured output và trích JSON",
    "tools/": "Công cụ dựng và kiểm tra",
    "tools/validate_spec.py": "Kiểm tra nhất quán toàn pack",
    "tools/build_spec.py": "Dựng SPEC.md",
    "tools/simulate_pool.py": "Mô phỏng LLM Pool (cùng Router như production) để thử chính sách trước khi tốn tiền",
    "tools/spec_src/": "Các phần nguồn của SPEC.md",
}

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", "node_modules"}


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- bảng sinh tự động
def file_tree() -> str:
    lines = ["```", f"{ROOT.name}/"]
    WIDTH = 46

    def walk(d: pathlib.Path, prefix: str, rel: str):
        entries = sorted([e for e in d.iterdir() if e.name not in SKIP_DIRS and not e.name.endswith(".pyc")],
                         key=lambda e: (e.is_file(), e.name))
        for i, e in enumerate(entries):
            last = i == len(entries) - 1
            r = f"{rel}{e.name}" + ("/" if e.is_dir() else "")
            name = e.name + ("/" if e.is_dir() else "")
            left = f"{prefix}{'└── ' if last else '├── '}{name}"
            desc = TREE_DESC.get(r)
            lines.append(f"{left:<{WIDTH}}# {desc}" if desc else left)
            if e.is_dir() and e.name != "spec_src":
                walk(e, prefix + ("    " if last else "│   "), r)

    walk(ROOT, "", "")
    lines.append("```")
    return "\n".join(lines)


def schema_table() -> str:
    used_by: dict[str, list[str]] = {}
    for p in sorted((ROOT / "prompts").glob("P*.md")):
        meta, _, _ = load_prompt(p)
        if str(meta["output"]).startswith("schema://"):
            used_by.setdefault(meta["output"][len("schema://"):], []).append(meta["id"].split("_")[0])
    rows = ["| Schema | Tên | Vai trò | Dùng bởi |", "|---|---|---|---|"]
    for p in sorted((ROOT / "schemas").glob("*.schema.json")):
        s = json.loads(read(p))
        desc = s.get("description", "").split(". ")[0].rstrip(".")
        by = ", ".join(used_by.get(p.name, [])) or ("(dùng chung)" if p.name.startswith("common") else "API / hệ thống")
        rows.append(f"| `schemas/{p.name}` | {s.get('title', '')} | {desc}. | {by} |")
    return "\n".join(rows)


def _pnum(p: pathlib.Path) -> int:
    return int(re.match(r"P(\d+)_", p.name).group(1))


def prompt_table() -> str:
    rows = ["| ID | Giai đoạn | Việc | Profile | Thinking | Đầu ra | Trần token ra |", "|---|---|---|---|---|---|---|"]
    jobs = {
        "P0": "Hồ sơ tài liệu", "P1": "Gợi ý thuật ngữ", "P2": "Kê khai đơn vị tri thức", "P3": "Dàn ý và phân bổ",
        "P4": "Viết một mục", "P5": "Kiểm chứng trung thực", "P6": "Kiểm tra độ phủ", "P7": "Sửa tối thiểu",
        "P8": "Ghi chú phạm vi", "P9": "Dịch đầy đủ", "P10": "OCR trang scan",
        "P12": "Đề xuất Lõi văn phong (curation)", "P13": "Hài hoà thuật ngữ (curation)",
    }
    for p in sorted((ROOT / "prompts").glob("P*.md"), key=_pnum):
        meta, _, _ = load_prompt(p)
        pid = meta["id"].split("_")[0]
        out = meta["output"].replace("schema://", "`") + ("`" if meta["output"].startswith("schema://") else "")
        if not meta["output"].startswith("schema://"):
            out = f"`{meta['output']}`"
        rows.append(f"| **{pid}** `v{meta['version']}` | `{meta['stage']}` | {jobs.get(pid, '')} | `{meta['model_profile']}` | `{meta['thinking']}` | {out} | {meta['max_output_tokens']:,} |".replace(",", "."))
    return "\n".join(rows)


def _fmt_usd(x: float) -> str:
    return f"{x:.2f}" if x >= 0.1 else f"{x:.3f}"


def cost_table() -> str:
    from datetime import date

    promo = Price("gemini-3.8-flash", date(2026, 9, 2), 0.75, 3.75, 0.075)
    after = Price("gemini-3.8-flash", date(2027, 1, 1), 1.50, 7.50, 0.15)
    sizes = [(3_000, "~10 trang"), (30_000, "~100 trang"), (90_000, "~300 trang"), (200_000, "~670 trang")]
    head = "| Mức | Tín dụng/300 từ | " + " | ".join(f"{w:,} từ ({n})".replace(",", ".") for w, n in sizes) + " |"
    rows = [head, "|---|---|" + "---|" * len(sizes)]
    from reference.estimator import LEVEL_CREDIT_FACTOR

    for lv in LEVELS:
        cells = []
        for w, _ in sizes:
            a, b = estimate(w, lv, promo), estimate(w, lv, after)
            cells.append(f"{a.credits} tín dụng · ${_fmt_usd(a.cost_usd)} / ${_fmt_usd(b.cost_usd)}")
        rows.append(f"| `{lv}` ({LEVEL_NAMES[lv]}) | ×{LEVEL_CREDIT_FACTOR[lv]:.2f} | " + " | ".join(cells) + " |")
    note = ("\n\nMỗi ô: **số tín dụng bị trừ** · chi phí LLM ước tính **giá khuyến mãi (đến 31/12/2026) / giá từ 01/01/2027** (USD, "
            "Gemini 3.8 Flash 0.75/3.75 rồi 1.50/7.50 USD mỗi 1M token vào/ra). Sinh bởi `reference/estimator.py` (có test); "
            "hằng số là khởi điểm, phải hiệu chỉnh ở Giai đoạn 0.")
    return "\n".join(rows) + note


def level_budget_table() -> str:
    rows = ["| Mức | Tỷ lệ so với nguồn | Tối thiểu | Tối đa | 3.000 từ | 30.000 từ | 90.000 từ |", "|---|---|---|---|---|---|---|"]
    rows.append("| `full_translation` | ≈ 100% (độ dài bản dịch) | — | — | 3.000 | 30.000 | 90.000 |")
    for lv in ("detailed_synthesis", "deep_synthesis", "executive_brief"):
        ratio, lo, hi = REPORT_BUDGET[lv]
        vals = " | ".join(f"{report_budget_words(w, lv):,}".replace(",", ".") for w in (3_000, 30_000, 90_000))
        rows.append(f"| `{lv}` | {ratio*100:.0f}% | {lo:,} | {hi:,} | {vals} |".replace(",", "."))
    return "\n".join(rows) + "\n\nĐơn vị: từ tiếng Việt. Hàm `report_budget_words()` kẹp trong [tối thiểu, tối đa] và không vượt 80% số từ nguồn."


def mt_cost_table() -> str:
    from datetime import date

    promo = Price("gemini-3.8-flash", date(2026, 9, 2), 0.75, 3.75, 0.075)
    after = Price("gemini-3.8-flash", date(2027, 1, 1), 1.50, 7.50, 0.15)
    modes = [("Dịch trực tiếp (P9), phương án được chọn", "direct", {}), ("Bản thô + LLM viết lại toàn bộ", "postedit_full", {}),
             ("Bản thô + LLM chỉ sửa đoạn cần sửa, giữ 30% bản thô", "postedit_selective", {"keep_rate": 0.3}),
             ("Bản thô + LLM chỉ sửa đoạn cần sửa, giữ 50% bản thô", "postedit_selective", {"keep_rate": 0.5}),
             ("Bản thô + LLM chỉ sửa đoạn cần sửa, giữ 70% bản thô", "postedit_selective", {"keep_rate": 0.7}),
             ("Bản thô + LLM chỉ sửa đoạn cần sửa, giữ 90% bản thô", "postedit_selective", {"keep_rate": 0.9})]
    base = estimate_translation(90_000, "direct", promo).cost_usd
    rows = ["| Chế độ (90.000 từ, ~300 trang) | Token vào | Token ra | Chi phí LLM: khuyến mãi / từ 01/01/2027 | So với dịch trực tiếp |", "|---|---|---|---|---|"]
    for name, mode, kw in modes:
        a, b = estimate_translation(90_000, mode, promo, **kw), estimate_translation(90_000, mode, after, **kw)
        d = (a.cost_usd / base - 1) * 100
        vn = lambda n: f"{n:,}".replace(",", ".")  # noqa: E731
        rows.append(f"| {name} | {vn(a.tokens_in)} | {vn(a.tokens_out)} | ${_fmt_usd(a.cost_usd)} / ${_fmt_usd(b.cost_usd)} | {'chuẩn' if mode == 'direct' else f'{d:+.0f}%'} |")
    return "\n".join(rows) + ("\n\nChi phí của chính model dịch máy không tính (miễn phí ở bản thử nghiệm hoặc tự dựng). Sinh bởi `estimate_translation()` trong `reference/estimator.py` "
                              "(có test); `keep_rate` là tỷ lệ đoạn mà LLM chỉ xác nhận giữ nguyên bản thô, phải đo thật ở Giai đoạn 0.")


def pool_capacity_table() -> str:
    cfg = json.loads(read(ROOT / "examples" / "pool_config.example.json"))
    model = load_pool_model(cfg)
    dep = next(d for d in model.deployments if d.id == "gemini-free-a/flash")
    per = deployment_daily_capacity(dep)["calls"]
    rows = [f"| Mức | Cỡ tài liệu | Lời gọi LLM / tài liệu | Sau khi nới cửa sổ x4 và gộp kiểm chứng x3 | Tài liệu/ngày: 1 dự án | 2 dự án | 5 dự án | 5 dự án + tối ưu |", "|---|---|---|---|---|---|---|---|"]
    for lv in LEVELS:
        for words in (30_000, 90_000):
            c0 = estimate_calls(words, lv)
            c1 = estimate_calls(words, lv, window_scale=4, verify_batch=3)
            rows.append(f"| `{lv}` | {f'{words:,}'.replace(',', '.')} từ | {c0} | {c1} | {per / c0:.1f} | {2 * per / c0:.1f} | {5 * per / c0:.1f} | {5 * per / c1:.1f} |")
    return "\n".join(rows) + (f"\n\nGiả định minh hoạ: mỗi dự án free có RPD = 250, tức {per} lời gọi/ngày sau hệ số an toàn 0.95 (`examples/pool_config.example.json`); "
                              "thay bằng số thật trong bảng điều khiển của nhà cung cấp. Cột cuối cho thấy thứ cần tiết kiệm là SỐ LỜI GỌI, không phải token. "
                              "Sinh bởi `estimate_calls()` và `deployment_daily_capacity()`.")


def declare_preview_table() -> str:
    """Kết quả biên dịch THẬT của examples/pool_declaration.example.json (khoá chỉ hiện 4 ký tự cuối)."""
    from reference.pool_declare import compile_all

    cfg = json.loads(read(ROOT / "examples" / "pool_config.example.json"))
    base = {k: cfg[k] for k in ("version", "note", "policy", "profiles")} | {"providers": [], "models": [], "groups": [], "deployments": []}
    r = compile_all(json.loads(read(ROOT / "examples" / "pool_declaration.example.json")), base)
    assert r.valid, r.errors

    def lim(l):
        if not l:
            return "chưa khai"
        parts = []
        if l.get("rpm"): parts.append(f"{l['rpm']} yêu cầu/phút")
        if l.get("tpm"): parts.append(f"{l['tpm']:,} token/phút".replace(",", "."))
        if l.get("rpd"): parts.append(f"{l['rpd']} yêu cầu/ngày")
        if l.get("concurrency"): parts.append(f"{l['concurrency']} đồng thời")
        return ", ".join(parts) or "chưa khai"

    rows = ["| Nhóm hạn mức | Gói | Dữ liệu | Cờ ToS | Cổng cho phép | Khoá | Deployment và hạn mức |", "|---|---|---|---|---|---|---|"]
    for g in r.preview["groups"]:
        keys = ", ".join(f"\u2026{c['last4']}" for c in g["credentials"])
        scope = f"cả tài khoản: {lim(g['account_limits'])}" if g.get("account_limits") else ""
        deps = "; ".join(f"`{d['id'].split('/', 1)[1]}`: {lim(d['limits']) if any((d['limits'] or {}).values()) else scope}" for d in g["deployments"])
        rows.append(f"| `{g['id']}` | {g['tier']} | {g['data_policy']} | {', '.join(g['tos_flags']) or '-'} | {', '.join(g['allowed_gates'])} | {keys} | {deps} |")
    c = r.preview["counts"]
    out = "\n".join(rows) + f"\n\n{c['groups']} nhóm, {c['keys']} khoá, {c['deployments']} deployment, {c['models']} model, {c['providers']} nhà cung cấp. "
    out += "Ví dụ trên khai đủ hạn mức và điểm chất lượng nên không có cảnh báo. Nếu khai **tối thiểu** (chỉ gói, khoá và tên model của một nhà cung cấp tuỳ chỉnh), bước xem trước vẫn chạy với mặc định bảo thủ và nói rõ điều đó:\n\n"
    mini = compile_all({"version": 1, "declarations": [{"provider": {"preset": "custom", "id": "provider-d", "base_url": "https://api.provider-d.example/v1"}, "tier": "free",
                                                        "keys": "ak_example_account_d_0000000001", "models": [{"model_id": "some-model"}]}]}, base)
    assert mini.valid, mini.errors
    out += "\n".join(f"- {w.split(': ', 1)[1] if w.startswith('khai báo #') else w}" for w in mini.warnings)
    return out


def pool_sim_table() -> str:
    sys.path.insert(0, str(ROOT / "tools"))
    import simulate_pool

    return simulate_pool.table_md(simulate_pool.run_all(10)) + ("\n\nMô phỏng 10 tài liệu 90.000 từ mức `deep_synthesis` gửi cùng lúc lúc 09:00 giờ Thái Bình Dương, 12 worker, nhà cung cấp giả có bộ giới hạn thật riêng và 2% lỗi 5xx. "
                                                                  "Sinh bởi `tools/simulate_pool.py` (cùng Router như production). **Là mô phỏng thuật toán với hạn mức GIẢ ĐỊNH, không phải số đo của nhà cung cấp thật.**")


def endpoint_table() -> str:
    spec = yaml.safe_load(read(ROOT / "api" / "openapi.yaml"))
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for path, item in spec["paths"].items():
        for method in ("get", "post", "put", "patch", "delete"):
            op = item.get(method)
            if not op:
                continue
            summ = (op.get("summary") or "").strip().split(". ")[0]
            groups.setdefault(op["tags"][0], []).append((method.upper(), path, summ))
    names = {"account": "Tài khoản", "documents": "Tài liệu", "jobs": "Job", "glossaries": "Glossary", "recipes": "Công thức",
             "reports": "Báo cáo", "public": "Công khai", "admin": "Admin", "pool": "LLM Pool (admin)", "style-cores": "Lõi văn phong",
             "curation": "Curation (admin, curator)"}
    out = ["| Nhóm | Phương thức | Đường dẫn | Mô tả |", "|---|---|---|---|"]
    for tag in ("account", "documents", "jobs", "glossaries", "recipes", "reports", "public", "admin", "pool", "style-cores", "curation"):
        for m, p, s in groups.get(tag, []):
            out.append(f"| {names[tag]} | `{m}` | `{p}` | {s} |")
    return "\n".join(out)


def style_compile_demo() -> str:
    """Minh hoạ cách Lõi văn phong được biên dịch thành khối chèn vào prompt (đúng hàm reference/style_core.py)."""
    neutral = json.loads(read(ROOT / "prompts" / "00_style_core_neutral.json"))
    example = json.loads(read(ROOT / "examples" / "style_core.example.json"))
    return ("**Lõi trung tính (mặc định)**, giai đoạn `write`:\n\n~~~~text\n" + compile_style_core(neutral, "write") + "\n~~~~\n\n"
            "**Lõi ví dụ `examples/style_core.example.json`**, giai đoạn `translate`:\n\n~~~~text\n" + compile_style_core(example, "translate") + "\n~~~~\n")


def prompts_appendix() -> str:
    out = []
    neutral = read(ROOT / "prompts" / "00_style_core_neutral.json")
    out.append("### A.0 Lõi văn phong mặc định (trung tính) và cách biên dịch\n")
    out.append("Biến `style_core` của P1, P2, P3, P4, P7, P8, P9 là văn bản biên dịch từ MỘT phiên bản Lõi văn phong đã duyệt (§19); "
               "khi công thức chưa gắn lõi nào thì dùng lõi trung tính dưới đây, vốn không đặt quan điểm nào về văn phong. "
               "Spec **không** kèm hướng dẫn văn phong cố định: nội dung đó là dữ liệu do AI đề xuất và người duyệt quyết định.\n")
    out.append("~~~~json\n" + neutral.strip() + "\n~~~~\n")
    out.append("Ví dụ khối biên dịch của một lõi có nội dung: §19.7.\n")
    for p in sorted((ROOT / "prompts").glob("P*.md"), key=_pnum):
        meta, system, user = load_prompt(p)
        pid = meta["id"].split("_")[0]
        out.append(f"### A.{_pnum(p) + 1} {pid} — `{meta['id']}` (v{meta['version']})\n")
        out.append(f"Giai đoạn `{meta['stage']}` · profile `{meta['model_profile']}` · thinking `{meta['thinking']}` · đầu ra `{meta['output']}` · "
                   f"trần {meta['max_output_tokens']} token · biến: " + ", ".join(f"`{v}`" for v in meta["variables"]) + "\n")
        if meta.get("notes"):
            out.append(f"> Ghi chú: {meta['notes']}\n")
        out.append("~~~~text\n## SYSTEM\n" + system.strip() + "\n\n## USER\n" + user.strip() + "\n~~~~\n")
    return "\n".join(out)


# ----------------------------------------------------------------------------- TOC
def slugify(h: str, seen: dict[str, int]) -> str:
    h = unicodedata.normalize("NFC", re.sub(r"[`*]", "", h)).lower()
    s = re.sub(r"[^\w\- ]", "", h, flags=re.UNICODE).strip().replace(" ", "-")
    n = seen.get(s, 0)
    seen[s] = n + 1
    return s if n == 0 else f"{s}-{n}"


def build_toc(md: str) -> str:
    lines, in_fence, fence = [], False, ""
    seen: dict[str, int] = {}
    for line in md.splitlines():
        m = re.match(r"^(```|~~~~)", line)
        if m:
            if not in_fence:
                in_fence, fence = True, m.group(1)
            elif line.startswith(fence):
                in_fence = False
            continue
        if in_fence:
            continue
        h = re.match(r"^(##|###) (.+)$", line)
        if h:
            level, title = len(h.group(1)), h.group(2).strip()
            anchor = slugify(title, seen)
            if level == 2:
                lines.append(f"- [{title}](#{anchor})")
            elif level == 3 and re.match(r"^(\d+\.\d+|A\.\d+)", title):
                lines.append(f"  - [{title}](#{anchor})")
    return "**Mục lục**\n\n" + "\n".join(lines)


# ----------------------------------------------------------------------------- kiểm tra
def run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


def validation_summary(pg_dsn: str | None) -> str:
    today = dt.date(2026, 10, 2).isoformat()
    rc1, out1 = run([sys.executable, "-m", "pytest", "tests", "--ignore=tests/pg_smoke.py", "-q"])
    m1 = re.search(r"(\d+ passed[^\n]*)", out1)
    args = [sys.executable, "tools/validate_spec.py"] + (["--pg-dsn", pg_dsn] if pg_dsn else [])
    rc2, out2 = run(args)
    oks = out2.count("  ok   ")
    smoke = re.search(r"(\d+) kiểm tra đạt", out2)
    conf = re.search(r"tổng cộng (\d+) lần đặt chỗ và (\d+) ảnh chụp", out2)
    pg_ver = ""
    if pg_dsn:
        try:
            import psycopg

            with psycopg.connect(pg_dsn) as c:
                pg_ver = c.execute("show server_version").fetchone()[0]
        except Exception:  # noqa: BLE001
            pg_ver = "?"
    rows = [
        "| Kiểm tra | Kết quả |", "|---|---|",
        f"| `pytest tests` (so khớp trích đoạn, lint thuật ngữ, số liệu, ước tính chi phí, wire schema, render prompt, **LLM Pool**, **khai báo và mã hoá khoá**, **Lõi văn phong**, structured output) | {'ĐẠT' if rc1 == 0 else 'THẤT BẠI'}: {m1.group(1) if m1 else '?'} |",
        f"| `validate_spec.py` (schema, ví dụ, kiểm tra phủ định, chéo ví dụ, prompt, OpenAPI, enum chéo DDL/schema/OpenAPI, cấu hình pool, lõi mẫu) | {'ĐẠT' if rc2 == 0 else 'THẤT BẠI'}: {oks} kiểm tra |",
    ]
    if pg_dsn:
        rows.append(f"| DDL trên **PostgreSQL {pg_ver} thật** + hành vi (sổ tín dụng idempotent, mã mời, giá LLM theo ngày hiệu lực, trần chi tiêu, hàng đợi có giới hạn theo job, thu hồi và hoãn task, xoá hết hạn, bản phát hành glossary, tính bất biến của Lõi văn phong, ràng buộc) | {'ĐẠT' if rc2 == 0 else 'THẤT BẠI'}: {smoke.group(1) if smoke else '?'} kiểm tra hành vi |")
        if conf:
            rows.append(f"| **Hàm SQL của pool so với MemoryState**: {conf.group(1)} lần đặt chỗ và {conf.group(2)} ảnh chụp trạng thái so khớp từng bước trên các kịch bản có hạt giống (đủ các nhánh từ chối và ba trạng thái circuit breaker), cộng bài tranh chấp 48 lời gọi từ 8 kết nối | {'ĐẠT' if rc2 == 0 else 'THẤT BẠI'} |")
    else:
        rows.append("| DDL | Chỉ parse cú pháp (chưa chạy trên PostgreSQL thật) |")
    rows.append(f"\nChạy ngày {today}, Python {sys.version.split()[0]}. Kiểm tra lại bằng lệnh ở §0. Các bài kiểm tra này chứng minh **logic và ngữ nghĩa** của spec; chúng KHÔNG chứng minh chất lượng đầu ra của LLM thật hay hạn mức thật của nhà cung cấp (xem §17.9, §21).")
    if rc1 != 0 or rc2 != 0:
        print(out1[-2000:], out2[-2000:])
    return "\n".join(rows)


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-checks", action="store_true")
    ap.add_argument("--pg-dsn", default=None)
    args = ap.parse_args()

    parts = sorted((ROOT / "tools" / "spec_src").glob("*.md"))
    md = "\n".join(read(p).rstrip() + "\n" for p in parts)

    repl = {
        "<!-- FILE_TREE -->": file_tree,
        "<!-- SCHEMA_TABLE -->": schema_table,
        "<!-- PROMPT_TABLE -->": prompt_table,
        "<!-- COST_TABLE -->": cost_table,
        "<!-- LEVEL_BUDGET_TABLE -->": level_budget_table,
        "<!-- ENDPOINT_TABLE -->": endpoint_table,
        "<!-- PROMPTS_APPENDIX -->": prompts_appendix,
        "<!-- STYLE_COMPILE_DEMO -->": style_compile_demo,
        "<!-- MT_COST_TABLE -->": mt_cost_table,
        "<!-- POOL_CAPACITY_TABLE -->": pool_capacity_table,
        "<!-- POOL_SIM_TABLE -->": pool_sim_table,
        "<!-- DECLARE_PREVIEW_TABLE -->": declare_preview_table,
    }
    for marker, fn in repl.items():
        if marker not in md:
            print(f"CẢNH BÁO: không thấy marker {marker}")
        md = md.replace(marker, fn())
    counts = {
        "[[N_PATHS]]": str(len(yaml.safe_load(read(ROOT / "api" / "openapi.yaml"))["paths"])),
        "[[N_TABLES]]": str(len(re.findall(r"^CREATE TABLE ", read(ROOT / "db" / "schema.sql"), re.M))),
        "[[N_PROMPTS]]": str(len(list((ROOT / "prompts").glob("P*.md")))),
        "[[N_SCHEMAS]]": str(len(list((ROOT / "schemas").glob("*.schema.json")))),
    }
    for k, v in counts.items():
        md = md.replace(k, v)
    md = md.replace("<!-- VALIDATION_SUMMARY -->", validation_summary(args.pg_dsn) if args.run_checks else "_Chạy `python tools/build_spec.py --run-checks` để điền._")
    md = md.replace("<!-- TOC -->", build_toc(md))
    left = re.findall(r"<!-- [A-Z_]+ -->", md)
    if left:
        print("CẢNH BÁO: còn marker chưa thay:", left)
    (ROOT / "SPEC.md").write_text(md, encoding="utf-8")
    words = len(md.split())
    print(f"Đã ghi SPEC.md: {len(md.splitlines())} dòng, ~{words} từ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
