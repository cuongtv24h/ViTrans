# ViSynth spec pack (v0.2)

Bộ đặc tả kỹ thuật & sản phẩm cho ứng dụng web **chuyển ngữ và tổng hợp tài liệu sang tiếng Việt bằng LLM** (tên tạm ViSynth).

**Bắt đầu ở đây: [`SPEC.md`](SPEC.md)** (tài liệu chính, có mục lục, Phụ lục A chứa toàn văn các prompt). Mục **§0.1** liệt kê những gì đã đổi so với bản 0.1 theo các quyết định của bạn.

| Muốn xem | Ở đâu |
|---|---|
| Pool nhiều nhà cung cấp, nhiều khoá, free tier, riêng tư, mô phỏng | `SPEC.md` §17; `reference/llm_pool.py`, `tools/simulate_pool.py`, `examples/pool_config.example.json` |
| Bản dịch thô của NVIDIA Riva Translate rồi LLM hiệu đính, có fallback | `SPEC.md` §18; `reference/mt_draft.py`, `prompts/P11_full_postedit.md` |
| Lõi văn phong và glossary chuẩn (AI đề xuất, người duyệt) | `SPEC.md` §19; `reference/style_core.py`, `prompts/P12_*`, `prompts/P13_*` |
| Triển khai một VPS cá nhân ở nước ngoài | `SPEC.md` §20 |

| Thư mục | Nội dung |
|---|---|
| `prompts/` | 14 prompt (P0-P13) + Lõi văn phong mặc định trung tính (`00_style_core_neutral.json`) |
| `schemas/`, `examples/` | 18 JSON Schema và ví dụ khớp từng schema (gồm `pool_config`, `style_core`, `style_core_proposal`, `glossary_proposals`, `postedit_chunk`) |
| `db/schema.sql` | PostgreSQL: 46 bảng + hàm nghiệp vụ (tín dụng, hàng đợi, giá LLM, trần chi tiêu, **đặt chỗ pool nguyên tử**, phát hành glossary, tính bất biến của Lõi văn phong) |
| `api/openapi.yaml` | OpenAPI 3.1 (61 đường dẫn) |
| `reference/`, `tests/` | Code tham chiếu và test (trích đoạn, thuật ngữ, số liệu, chi phí, **LLM Pool**, **bản dịch thô**, **Lõi văn phong**, structured output) |
| `tools/` | `validate_spec.py` (kiểm tra nhất quán), `build_spec.py` (dựng SPEC.md), `simulate_pool.py` (mô phỏng pool) |

Kiểm tra nhanh:

```bash
pip install jsonschema pyyaml openapi-spec-validator pytest rapidfuzz pglast "psycopg[binary]"
pytest tests --ignore=tests/pg_smoke.py
python tools/validate_spec.py
python tools/validate_spec.py --pg-dsn postgresql://user@host/db   # thêm: DDL trên PostgreSQL thật + so khớp hàm SQL của pool với bản tham chiếu
python tools/simulate_pool.py
```

Sửa prompt trong `prompts/` hoặc nội dung trong `tools/spec_src/`, rồi chạy `python tools/build_spec.py` để dựng lại `SPEC.md`. **Đừng sửa `SPEC.md` trực tiếp.**

Lưu ý: các bài kiểm tra chứng minh **logic và ngữ nghĩa** của spec. Chúng không chứng minh chất lượng đầu ra của LLM thật hay hạn mức thật của nhà cung cấp; các con số hạn mức trong ví dụ là giả định minh hoạ.
