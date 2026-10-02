# `infra` — hạ tầng một VPS (M1)

Chưa có mã. Việc của M1 (SPEC §20):

- `docker-compose.yml` (api, worker, PostgreSQL, Caddy), Caddyfile, Dockerfile.
- Script sao lưu mã hoá ra nhà cung cấp khác + script khôi phục; **diễn tập khôi phục ≤ 4 giờ** (AC-25).
- `POOL_MASTER_KEY` và khoá nhà cung cấp **không** nằm trong bản sao lưu; giám sát ngoài máy.
- Runbook sự cố: chạm trần chi tiêu, hết hạn mức ngày, khoá bị từ chối (401), nhà cung cấp sập, VPS chết.
