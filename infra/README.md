# `infra` — chạy ViSynth trên một VPS cá nhân (M1, SPEC §20)

Bộ tệp ở đây dựng cả hệ thống trên **một máy**: `caddy` (TLS, cổng duy nhất), `api`, `worker` ×2,
`postgres`, `scheduler` (việc định kỳ), `backup` (sao lưu mã hoá ra nhà cung cấp khác) và `egress`
(proxy ra ngoài có danh sách tên miền cho phép).

Câu hỏi thường gặp về mức độ kiểm chứng: **sandbox phát triển không có mạng nên các tệp này chưa
được `docker compose up` lần nào.** Thay vào đó, `python -m pytest tests/test_infra.py` kiểm những
bất biến cấu trúc của §20 (cổng nào mở, mạng nào ra internet, container nào non-root, khoá chủ đi
đường nào, bản sao lưu loại trừ bảng nào). Lần chạy đầu tiên trên VPS hãy làm theo thứ tự dưới đây
và đối chiếu với §20.4.

## 0. Chuẩn bị máy (làm TRƯỚC khi mở cổng 80/443)

| Việc | Lệnh / ghi chú |
|---|---|
| Tạo tài khoản triển khai riêng, SSH chỉ bằng khoá | `adduser deploy && ssh-copy-id`, sửa `/etc/ssh/sshd_config`: `PasswordAuthentication no`, `PermitRootLogin no` |
| `fail2ban` | `apt install fail2ban` |
| Cập nhật bảo mật tự động | `apt install unattended-upgrades && dpkg-reconfigure -plow unattended-upgrades` |
| Tường lửa | `ufw default deny incoming && ufw allow 80,443/tcp && ufw allow from <IP của bạn> to any port 22 && ufw enable` — **nhớ:** cổng Docker publish có thể đi vòng qua ufw, nên compose không publish cổng nào ngoài 80/443 (§20.4) |
| Docker + compose plugin | `curl -fsSL https://get.docker.com \| sh` |
| Kiểm tra độ trễ trước khi thuê | `mtr -rwzbc 50 <IP VPS>` từ Hà Nội (§20.3) |

Chọn vùng theo §20.3: ưu tiên Singapore/Tokyo/Hồng Kông; kiểm tra nhà cung cấp LLM có phục vụ vùng
đó; nên chọn vùng ngoài EEA/Anh; nếu đặt Cloudflare phía trước thì Cloudflare thấy nội dung ở dạng
rõ — phải nêu trong chính sách riêng tư, hoặc chỉ dùng DNS + Caddy.

## 1. Bí mật (làm trên VPS, không dán vào chat/email)

```sh
cd /opt/visynth/infra
cp .env.example .env && chmod 600 .env      # rồi điền: domain, email ACME, POSTGRES_PASSWORD, session secret…

# Khoá chủ mã hoá khoá API: sinh MỘT lần, cất một bản ở trình quản lý mật khẩu của bạn.
mkdir -p secrets && umask 077
python -m visynth.cli pool keygen --out secrets/pool_master_key   # hoặc: head -c 32 /dev/urandom | base64
chmod 400 secrets/pool_master_key

# Khoá age cho bản sao lưu: sinh Ở MÁY CỦA BẠN, chỉ dán khoá CÔNG KHAI vào .env
age-keygen -o visynth-backup-key.txt        # khoá riêng (file này) KHÔNG bao giờ lên VPS
#   → AGE_PUBLIC_KEY=age1... trong .env
```

`POOL_MASTER_KEY` **không** nằm trong `.env`, bản sao lưu, hay `docker inspect`: compose đưa nó vào
container qua Docker secret (`/run/secrets/pool_master_key`, quyền 0400) và ứng dụng đọc bằng
`VISYNTH_POOL_MASTER_KEY_FILE` (SPEC §20.5). Mất khoá này thì mọi khoá API trong CSDL không giải mã
được nữa — chúng phải được nhập lại.

## 2. Dựng

```sh
docker compose --env-file .env up -d --build
docker compose ps                         # api: healthy, postgres: healthy
curl -fsS https://$VISYNTH_DOMAIN/api/v1/healthz
```

`api` chạy `db migrate` trước khi mở cổng (`infra/entrypoint-api.sh`); migration idempotent nên
khởi động lại nhiều lần cũng an toàn.

## 3. Nạp LLM Pool (khoá API đi vào ĐÚNG một đường)

1. Đăng nhập bằng email trong `VISYNTH_ADMIN_EMAIL` (tài khoản này tự nhận vai trò admin).
2. `Admin > Pool > Khai báo nhanh`: dán khối YAML mẫu (`GET /api/v1/admin/pool/declaration-template`),
   điền khoá, bấm **xem trước** (`dry_run` mặc định BẬT — chưa ghi gì), rồi xác nhận.
   Khoá được mã hoá ngay khi ghi (`llm_credentials.secret_enc`); mọi phản hồi chỉ có `last4`.
3. Kiểm định: `POST /api/v1/admin/pool/deployments/{id}/probe` cho từng deployment. Chỉ deployment
   **đã kiểm định đạt** mới được bộ định tuyến dùng cho cổng công khai.
4. Worker đọc pool từ CSDL (`--pool-from-db`) và ghi mọi lời gọi vào `llm_calls`.

Thay khoá định kỳ hoặc khi nghi lộ: `POST /admin/pool/groups/{id}/credentials` (khoá mới),
`PATCH /admin/pool/credentials/{id}` (`disabled`), rồi rà `llm_calls` (§20.7 runbook 6).

## 4. Giám sát và việc định kỳ

| Việc | Ở đâu |
|---|---|
| Sức khoẻ API | Giám sát ngoài máy ping `https://<domain>/api/v1/healthz` (§20.7) |
| Việc định kỳ | `scheduler`: `ops reap` mỗi phút, `ops purge` mỗi giờ, `ops health --json` mỗi 5 phút |
| Hàng đợi / ví âm / khoá bị cách ly | `docker compose logs scheduler` hoặc `docker compose exec scheduler python -m visynth.cli ops health` |
| Sao lưu | `backup`: 1 lần/ngày, mã hoá `age`, đẩy sang nhà cung cấp khác; ping dead man's switch |
| Cảnh báo | Uptime Kuma/healthchecks.io + Telegram/email; đĩa > 80% (§20.7) |

## 5. Sao lưu và diễn tập khôi phục (AC-25 ≤ 4 giờ)

- Bản sao lưu **loại trừ nội dung tài liệu** (`doc_paragraphs`, `doc_sections`, `job_events`) và
  không chứa tệp gốc — nhờ vậy lời hứa "xoá tài liệu trong 24 giờ" vẫn đúng sau khi khôi phục (§5.4).
- Diễn tập **hằng quý**, đo thời gian thật, ghi vào runbook:

```sh
# trên VPS mới
docker compose run --rm -e AGE_IDENTITY=/tmp/khoa-rieng.txt \
  -v "$PWD/visynth-backup-key.txt:/tmp/khoa-rieng.txt:ro" backup /app/infra/restore.sh /backup/daily/<ngày>.dump.age
```

Script in ra số phút đã dùng, đối chiếu sổ tín dụng và chạy `ops reap` trước khi mở cổng.

## 6. Nâng cấp (§20.7)

```sh
docker compose exec backup /app/infra/backup.sh      # 1. sao lưu trước
git pull                                          # 2. cấu hình mới
VISYNTH_TAG=$(git rev-parse --short HEAD) docker compose --env-file .env up -d --build   # 3. image mới (ghim theo tag)
docker compose logs -f api                        # 4. migration đã chạy trong entrypoint
curl -fsS https://$VISYNTH_DOMAIN/api/v1/healthz  # 5. kiểm thử khói (AC-01)
```

Đường lui: `docker compose down && VISYNTH_TAG=<tag cũ> docker compose up -d` (dữ liệu nằm ở volume
`pgdata`, không bị ảnh hưởng bởi việc đổi image).

## 7. Runbook sự cố ngắn

| Triệu chứng | Việc làm |
|---|---|
| Một nhà cung cấp sập/429 kéo dài | Xem `/admin/pool/status` + `/admin/pool/incidents`; pool tự hạ tầng; nếu cần: `PATCH /admin/pool/deployments/{id}` (`enabled=false`) |
| Chạm trần chi tiêu | Job tự dừng (`max_cost_usd`); kiểm `jobs.actual_shadow_usd`; nâng trần có chủ đích, đừng tắt kiểm |
| Khoá bị từ chối (401/400 `API_KEY_INVALID`) | Khoá vào `llm_credentials.status='quarantined'`; thay khoá mới qua Admin API, gỡ cách ly (§20.7 runbook 8) |
| Job kẹt | `visynth ops reap --json`; xem `jobs.status='awaiting_glossary'` (cổng chờ người duyệt) |
| Tải tài liệu trả 503 `parser_unavailable` | `docker compose ps parser` + `docker compose logs parser`; parser bóc tách trong sandbox nên có thể chết vì tệp xấu — khởi động lại, người dùng thử lại |
| VPS chết | Thuê máy mới → mục 0 → mục 5 (khôi phục) → mục 2 |
| Đĩa đầy | `docker system prune -f`, xoay log, kiểm bản sao lưu cũ trong `/backup` |
| Nghi bị xâm nhập | Ngắt cổng 443, **xoay mọi khoá** (LLM, phiên, khoá chủ), rà `llm_calls`/`audit_log`, thông báo người dùng nếu cần theo luật |

## 8. Chưa có trong M1 (biết để không hẫng)

- **`web` SPA** thay cho Next.js của §20.3: M2 phục vụ tệp tĩnh bằng FastAPI (dev) và Caddy (VPS) —
  ghi ở `docs/BUILD_PLAN.md` mục "sai lệch có chủ ý".
- **`web`** (Next.js) chỉ có ở mốc M2 — Caddy đã chừa chỗ ở nhánh `handle { … }`.
- **WAL lưu ngoài máy** (RPO tính bằng phút) cần cho cổng C, khi sổ tín dụng đã có tiền thật (§20.6).
- **Hồ sơ PDPL** (đánh giá tác động, thoả thuận chuyển dữ liệu) là việc giấy tờ ở §20.8, không phải việc mã.
