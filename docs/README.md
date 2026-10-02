# ViSynth spec pack (v0.3)

Bộ đặc tả kỹ thuật & sản phẩm cho ứng dụng web **chuyển ngữ và tổng hợp tài liệu sang tiếng Việt bằng LLM** (tên tạm ViSynth).

**Bắt đầu ở đây: [`SPEC.md`](SPEC.md)** (tài liệu chính, có mục lục, Phụ lục A chứa toàn văn các prompt). Mục **§0.1** liệt kê những gì đã đổi so với các bản trước (0.1 → 0.3).

**Kế hoạch thi công:** [`BUILD_PLAN.md`](BUILD_PLAN.md) — chi tiết hoá §21.1 thành lộ trình M0→M4 (cấu trúc kho mã, việc theo tuần, tiêu chí thoát mốc, CI, ngân sách).

| Muốn xem | Ở đâu |
|---|---|
| Pool nhiều nhà cung cấp, nhiều khoá, free tier, riêng tư, mô phỏng | `SPEC.md` §17; `reference/llm_pool.py`, `tools/simulate_pool.py`, `examples/pool_config.example.json` |
| Khai báo nhà cung cấp và khoá cho pool (dán chuỗi khoá, xem trước, `risk_ack`) | `SPEC.md` §17.14; `reference/pool_declare.py`, `reference/pool_secrets.py`, `examples/pool_declaration.template.yaml` |
| Lõi văn phong và glossary chuẩn (AI đề xuất, người duyệt) | `SPEC.md` §19; `reference/style_core.py`, `prompts/P12_*`, `prompts/P13_*` |
| Triển khai một VPS cá nhân ở nước ngoài | `SPEC.md` §20 |
| Quyết định bỏ khâu bản dịch thô của model dịch máy (bằng chứng và điều kiện xem xét lại) | `SPEC.md` §18 |

| Thư mục | Nội dung |
|---|---|
| `prompts/` | 13 prompt (P0-P10, P12-P13) + Lõi văn phong mặc định trung tính (`00_style_core_neutral.json`) |
| `schemas/`, `examples/` | 18 JSON Schema và ví dụ khớp từng schema (gồm `pool_config`, `pool_declaration`, `style_core`, `style_core_proposal`, `glossary_proposals`) |
| `db/schema.sql` | PostgreSQL: 46 bảng + hàm nghiệp vụ (tín dụng, hàng đợi, giá LLM, trần chi tiêu, **đặt chỗ pool nguyên tử**, phát hành glossary, tính bất biến của Lõi văn phong) |
| `api/openapi.yaml` | OpenAPI 3.1 (61 đường dẫn) |
| `reference/`, `tests/` | Code tham chiếu và test (trích đoạn, thuật ngữ, số liệu, chi phí, **LLM Pool**, **khai báo và mã hoá khoá**, **Lõi văn phong**, structured output) |
| `tools/` | `validate_spec.py` (kiểm tra nhất quán), `build_spec.py` (dựng SPEC.md), `simulate_pool.py` (mô phỏng pool) |

Kiểm tra nhanh:

```bash
pip install -r requirements.txt          # hoặc: pip install jsonschema pyyaml openapi-spec-validator pytest rapidfuzz pglast "psycopg[binary]" cryptography
pytest tests --ignore=tests/pg_smoke.py
python tools/validate_spec.py
python tools/validate_spec.py --pg-dsn postgresql://user@host/db   # thêm: DDL trên PostgreSQL thật + so khớp hàm SQL của pool với bản tham chiếu
python tools/simulate_pool.py
python tools/check_spec_sync.py          # SPEC.md có khớp tools/spec_src không (CI chạy lệnh này; bỏ qua Phụ lục E)
```

Sửa prompt trong `prompts/` hoặc nội dung trong `tools/spec_src/`, rồi chạy `python tools/build_spec.py --run-checks` (điền cả Phụ lục E) và commit lại `SPEC.md`. **Đừng sửa `SPEC.md` trực tiếp.**

Lưu ý: các bài kiểm tra chứng minh **logic và ngữ nghĩa** của spec. Chúng không chứng minh chất lượng đầu ra của LLM thật hay hạn mức thật của nhà cung cấp; các con số hạn mức trong ví dụ là giả định minh hoạ.
