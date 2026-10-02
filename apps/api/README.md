# `apps/api` — FastAPI (M1)

Chưa có mã. Việc của M1 (theo `docs/BUILD_PLAN.md`):

- Alembic từ `docs/db/schema.sql`; hàng đợi task/lease/checkpoint/reclaim.
- Đăng ký/đăng nhập, mã mời, upload, `POST /jobs` (kèm `Idempotency-Key`), SSE + `Last-Event-ID`, ví tín dụng.
- Admin API cho pool (`/admin/pool`, `declare`, `probe`), curation runs (P12/P13), duyệt Lõi văn phong và glossary.
- Kiểm tra hợp đồng: `docs/api/openapi.yaml` là nguồn sự thật; mọi endpoint phải khớp và có test.

Điều kiện vào M1: **M0-W4 đạt tiêu chí thoát mốc** (KPI §2.2 trên ≥ 5 tài liệu, `estimate` lệch ≤ 35%).
