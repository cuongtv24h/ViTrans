# `packages/contracts` — hợp đồng dữ liệu

Trong M0, hợp đồng dữ liệu **không được sao chép** vào đây: nguồn sự thật duy nhất vẫn là bộ đặc tả.

| Hợp đồng | Nguồn sự thật | Mã sản phẩm đọc qua |
|---|---|---|
| JSON Schema (18 tệp) | [`docs/schemas/`](../../docs/schemas) | `VISYNTH_SCHEMAS_DIR` (mặc định `docs/schemas`) |
| OpenAPI 3.1 (61 đường dẫn) | [`docs/api/openapi.yaml`](../../docs/api/openapi.yaml) | `VISYNTH_OPENAPI` |
| Prompt (13 prompt + lõi trung tính) | [`docs/prompts/`](../../docs/prompts) | `VISYNTH_PROMPTS_DIR` |
| DDL PostgreSQL (46 bảng + hàm) | [`docs/db/schema.sql`](../../docs/db/schema.sql) | Alembic baseline ở M1 |

**Khi nào tách bản sao:** chỉ khi cần phát hành gói `contracts` riêng (sinh kiểu TypeScript/Pydantic cho
`apps/web`/`apps/api`). Lúc đó thêm bước sinh tự động trong CI và test đối chiếu với `docs/`, không viết tay.
