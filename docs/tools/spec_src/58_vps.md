
---

## 20. Triển khai trên một VPS cá nhân ở nước ngoài

> Chương này cụ thể hoá quyết định D10. Mọi con số về RAM, đĩa và chi phí là **ước tính để lập kế hoạch**, phải đo lại ở mốc M1 trên máy thật. Phần pháp lý (§20.8) là danh sách việc cần làm và câu hỏi cho luật sư, không phải tư vấn pháp lý.

### 20.1 Quyết định và hệ quả

| Thuộc tính | Hệ quả |
|---|---|
| Một máy, docker compose, không dịch vụ quản lý | Rẻ, dễ hiểu, dễ khôi phục; không có tính sẵn sàng cao: **máy chết thì dịch vụ chết** |
| Điểm lỗi duy nhất | Giảm nhẹ bằng sao lưu mã hoá ngoài máy, giám sát ngoài máy, runbook, và việc mọi trạng thái bền nằm ở một nơi (PostgreSQL) |
| Mục tiêu khôi phục (đề xuất) | RPO 24 giờ ở cổng A-B với sao lưu hằng ngày; **cổng C (có tiền) cần RPO tính bằng phút** nên phải lưu trữ nhật ký ghi trước (WAL) ra ngoài máy (§20.6). RTO mục tiêu 4 giờ, đo bằng diễn tập |
| Đặt ở nước ngoài, người vận hành là cá nhân | Dữ liệu của người dùng Việt rời lãnh thổ ngay từ khâu lưu trữ chứ không chỉ khâu gọi LLM: nghĩa vụ PDPL (§14.4, §20.8) |
| Phù hợp tới đâu | Closed beta và đăng ký mở quy mô nhỏ. Khi cần SLA, tải cao hoặc không chịu được một điểm lỗi: máy thứ hai cho worker, rồi CSDL quản lý |

### 20.2 Thành phần và ngân sách tài nguyên

| Dịch vụ | Vai trò | RAM đỉnh ước tính | Ghi chú |
|---|---|---|---|
| `caddy` | TLS tự động, định tuyến, giới hạn thô | 50 MB | Cổng 80/443 là cổng duy nhất mở ra ngoài |
| `web` | Next.js | 300 MB | |
| `api` | FastAPI | 300-500 MB | |
| `worker` ×2 | Pipeline + LLM Pool | 500 MB mỗi cái | Số bản = số tác vụ song song tổng, bị chặn bởi hạn mức nhà cung cấp chứ không bởi CPU |
| `postgres` | Dữ liệu, hàng đợi, trạng thái pool | 1-2 GB | `shared_buffers` khoảng 25% RAM dành cho nó |
| `redis` (tuỳ chọn) | Giới hạn tốc độ API, SSE | 50-100 MB | Bỏ được (§5.2) |
| `parser` | Bóc tách file, OCR cục bộ nếu cần | 1-1,5 GB khi chạy LibreOffice | **Không mount `docker.sock`** vào api hay worker (tương đương quyền root). Hai cách: service `parser` riêng trong mạng `internal: true` (không có đường ra internet), chạy non-root, rootfs chỉ đọc, `mem_limit`, `pids_limit`, nhận việc qua hàng đợi PostgreSQL; hoặc `nsjail`/`bubblewrap` trong container worker |
| Xuất PDF | Chromium hoặc WeasyPrint | 0,5-1 GB khi chạy | Tạo theo yêu cầu |
| `cron` | `purge_expired_documents` (mỗi giờ), `reclaim_stale_tasks` và `pool_reap_leases` (mỗi phút), sao lưu | | |

**Kích thước khởi điểm:** 4 vCPU, 8 GB RAM, 80-160 GB NVMe (đo lại ở M1). Đĩa chủ yếu là PostgreSQL và tệp gốc giữ 14 ngày.

### 20.3 Chọn vùng và lớp trước mặt

| Vấn đề | Khuyến nghị |
|---|---|
| Độ trễ tới người dùng ở Hà Nội | Ưu tiên các vùng châu Á (Singapore, Tokyo, Hồng Kông thường gần hơn châu Âu và Mỹ); **đo bằng `mtr` từ Hà Nội** trước khi thuê, không cam kết con số |
| Nhà cung cấp LLM | Gemini chỉ cho truy cập từ **vùng được hỗ trợ**; kiểm tra danh sách vùng của từng nhà cung cấp trước khi chọn nơi đặt máy |
| Luật nơi đặt máy | Nên chọn vùng ngoài EEA và Anh để không kéo thêm chế độ bảo vệ dữ liệu của nơi đặt máy khi người vận hành là cá nhân; luật sư xác nhận |
| Cloudflare ở phía trước | Ẩn IP gốc, WAF, hạn chế tấn công; **nhưng Cloudflare kết thúc TLS nên thấy nội dung ở dạng rõ**: thêm một bên xử lý dữ liệu cần nêu trong chính sách riêng tư. Muốn tránh: chỉ dùng DNS và Caddy, vẫn dùng Turnstile (không cần proxy) |

### 20.4 Bảo mật máy chủ (bắt buộc)

| Hạng mục | Yêu cầu |
|---|---|
| SSH | Chỉ khoá; tắt mật khẩu và đăng nhập root; `fail2ban` hoặc tương đương; tài khoản triển khai riêng |
| Tường lửa | Chỉ 80/443 ra ngoài (SSH giới hạn theo IP nếu được). **Cẩn thận:** cổng Docker publish có thể vượt qua `ufw`; PostgreSQL và Redis **không publish** ra host, chỉ nằm trong mạng Docker nội bộ |
| Cập nhật | `unattended-upgrades` cho bản vá bảo mật hệ điều hành; image ghim theo digest; rà bản vá hằng tuần |
| Container | Không chạy root; `read_only`, `cap_drop: [ALL]`, `no-new-privileges`; giới hạn `mem_limit`, `pids_limit`, CPU để một tài liệu xấu không làm sập máy |
| Egress | Worker chỉ ra được tới tên miền của các nhà cung cấp đã khai báo, qua một forward proxy có danh sách cho phép theo tên miền (biện pháp chính chống đánh cắp khoá nếu một thư viện bị chèn mã độc, §17.3 dòng 9) |
| Ứng dụng | Làm sạch Markdown, CSP, giới hạn tốc độ, 2FA cho Admin (§14.2) |
| Nhật ký | Xoay vòng; không chứa nội dung tài liệu; che các chuỗi giống khoá API (`AIza...`, `nvapi-...`, `sk-...`) ở lớp logger |

### 20.5 Bí mật và khoá của pool

- **`POOL_MASTER_KEY`** (32 byte ngẫu nhiên) nằm trong tệp chỉ root đọc được (`0400`) hoặc Docker secret, và có một bản ở nơi tách biệt (trình quản lý mật khẩu của bạn). **Không** nằm trong bản sao lưu CSDL, **không** nằm trong kho mã.
- Khoá API lưu trong CSDL dưới dạng `secret_enc` = AES-256-GCM (nonce 12 byte ngẫu nhiên, AAD là id khoá, khoá con dẫn xuất bằng HKDF từ khoá chủ). Mục đích: **lộ CSDL hoặc bản sao lưu không kéo theo lộ khoá API**. Mã hoá này không cứu được trường hợp kẻ tấn công chiếm được quyền root trên máy đang chạy (khoá nằm trong RAM của worker): vì vậy hạn chế thiệt hại ở phía nhà cung cấp (đặt hạn mức chi tiêu hoặc cảnh báo ngân sách cho khoá trả phí; khoá free vốn giá trị thấp).
- Cách khác: `secret_ref = env:TEN_BIEN` với biến lấy từ `.env` quyền `0600` hoặc Docker secrets; đơn giản hơn, phù hợp ở cổng A.
- Khoá free và khoá trả phí ở các biến khác nhau; khoá không bao giờ gửi tới trình duyệt; xoay khoá định kỳ và ngay khi nghi ngờ (§17.14).

### 20.6 Sao lưu và khôi phục

| Đối tượng | Cách làm | Ghi chú |
|---|---|---|
| PostgreSQL | `pg_dump -Fc` hằng ngày, mã hoá bằng `age` (khoá công khai trên máy, khoá riêng ngoài máy), đẩy ra kho đối tượng của **nhà cung cấp khác ở vùng khác** (bật versioning, khoá ghi nếu có) | Giữ 7 bản ngày và 4 bản tuần. Cổng C: thêm lưu trữ WAL (pgBackRest hoặc WAL-G) để RPO tính bằng phút vì sổ tín dụng là dữ liệu tiền |
| **Loại trừ khỏi sao lưu** | Dữ liệu của `doc_paragraphs`, `doc_sections`, `job_events` (`pg_dump --exclude-table-data=...`) và tệp gốc | Nếu không loại trừ, nội dung tài liệu tồn tại trong sao lưu quá 14 ngày và **phá lời hứa xoá tài liệu trong 24 giờ** (§5.4). Đổi lại, khôi phục xong thì job đang dở phải chạy lại (hoàn tín dụng) và người dùng tải lại tài liệu |
| Báo cáo, glossary, Lõi văn phong, sổ tín dụng, cấu hình pool | Có trong sao lưu | Hạn lưu sao lưu ≤ 30 ngày để lời hứa "xoá tài khoản trong 30 ngày" còn đúng |
| Cấu hình triển khai | `docker-compose.yml`, `Caddyfile`, `.env.example` (không bí mật) trong git | |

**Khôi phục (runbook):** thuê VPS mới → cài Docker → lấy `POOL_MASTER_KEY` và khoá giải mã `age` từ nơi cất → kéo cấu hình từ git → khôi phục dump → chạy `reclaim_stale_tasks` và `pool_reap_leases` → kiểm tra sổ tín dụng → bật dịch vụ. **Diễn tập hằng quý**, đo thời gian thật và ghi vào runbook; một bản sao lưu chưa từng được khôi phục thử thì chưa phải bản sao lưu.

### 20.7 Vận hành

- **Giám sát ngoài máy** (Uptime Kuma ở máy khác hoặc dịch vụ như healthchecks.io): ping `/healthz`; kiểm tra "dead man's switch" cho cron sao lưu (không thấy tín hiệu thành công trong 26 giờ thì báo); cảnh báo qua Telegram hoặc email.
- **Đĩa:** cảnh báo ở 80%; `docker system prune` định kỳ; xoay vòng nhật ký; `purge_expired_documents` chạy mỗi giờ.
- **Nâng cấp:** sao lưu → kéo image mới → `docker compose up -d` → chạy migration → kiểm thử khói (AC-01) → có sẵn đường lui về image cũ. Chấp nhận ngừng ngắn; báo trước trên trang trạng thái.
- **Runbook:** (a) máy không phản hồi: kiểm tra qua bảng điều khiển nhà cung cấp VPS, khởi động lại, nếu hỏng đĩa thì khôi phục (§20.6); (b) đĩa đầy; (c) CSDL hỏng: khôi phục từ sao lưu; (d) nghi bị xâm nhập: ngắt, **xoay mọi khoá** (API LLM, OAuth, khoá chủ), phân tích `llm_calls` và `audit_log`, thông báo người dùng nếu cần theo luật.
- **Khi nào cần máy thứ hai:** CPU duy trì trên 80%, hàng đợi task dài kéo dài dù hạn mức nhà cung cấp còn, hoặc cần SLA.

### 20.8 Hệ quả pháp lý của "cá nhân + máy chủ ở nước ngoài"

Danh sách để hỏi luật sư trước cổng B (nguồn ở §14.4 và Phụ lục D):

1. **Tư cách người vận hành.** Nghị định 356/2025 miễn hồ sơ đánh giá tác động cho hộ kinh doanh và doanh nghiệp siêu nhỏ (miễn hoàn toàn) và cho doanh nghiệp nhỏ, khởi nghiệp (5 năm từ 01/01/2026), nhưng **không áp dụng** nếu cung cấp dịch vụ xử lý dữ liệu cá nhân, trực tiếp xử lý dữ liệu cá nhân nhạy cảm, hoặc đạt 100.000 chủ thể dữ liệu. Một cá nhân chưa đăng ký có thể không thuộc nhóm được miễn. Câu hỏi: có nên đăng ký hộ kinh doanh hay doanh nghiệp siêu nhỏ; và nếu người dùng tải lên tài liệu chứa dữ liệu nhạy cảm (sức khoẻ, tín ngưỡng...) thì người vận hành có bị coi là "trực tiếp xử lý dữ liệu nhạy cảm" không.
2. **Hồ sơ đánh giá tác động chuyển dữ liệu ra nước ngoài** (nộp trong 60 ngày kể từ lần chuyển đầu tiên; cơ quan chuyên trách thẩm định trong 15 ngày): áp dụng khi dữ liệu thu thập hoặc lưu ở Việt Nam được chuyển tới máy chủ ngoài Việt Nam hoặc nhà cung cấp đám mây nước ngoài. Sơ đồ luồng cần mô tả: trình duyệt người dùng → VPS ở nước nào → các nhà cung cấp LLM nào → nhà cung cấp email, thanh toán, Cloudflare.
3. **Thoả thuận chuyển dữ liệu bằng văn bản** với từng bên nhận (Nghị định 356 yêu cầu): với nhà cung cấp LLM trả phí có thoả thuận xử lý dữ liệu (DPA) kèm điều khoản; với nhà cung cấp VPS và các bên khác dùng điều khoản dịch vụ của họ và lưu bản đã chấp nhận.
4. **Chính sách quyền riêng tư** nêu rõ: bên kiểm soát dữ liệu (tên và liên hệ của cá nhân hoặc pháp nhân), nước đặt máy chủ, danh sách bên xử lý, từng mục đích, thời hạn lưu, quyền của người dùng, quy trình xoá. Bản xuất bản trước cổng B.
5. **Cổng C:** cần pháp nhân, tài khoản thanh toán và hoá đơn (§13.5).

### 20.9 Chi phí hạ tầng

VPS 8 GB, kho đối tượng cho sao lưu, tên miền, email giao dịch, giám sát: cộng lại ở mức vài chục đến khoảng một trăm USD mỗi tháng (§21.2), phần lớn là VPS. **Kiểm tra giá tại thời điểm thuê**; spec không kèm giá của nhà cung cấp nào.
