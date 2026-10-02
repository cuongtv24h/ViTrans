
---

## 12. Điều phối job

### 12.1 Máy trạng thái của job

```
queued ──▶ running ──▶ awaiting_glossary ──▶ running ──▶ succeeded
   │          │                │                │
   │          └───────▶ failed ◀────────────────┘
   └─────────────▶ canceled   (từ queued / running / awaiting_glossary)
succeeded | failed | canceled ──▶ expired   (khi quá hạn lưu trữ)
```

`awaiting_glossary` chỉ xuất hiện giữa giai đoạn `glossary` và `map`/`translate`. `job_stages` ghi trạng thái từng giai đoạn (`pending|running|succeeded|failed|skipped`); các giai đoạn không dùng ở mức đã chọn tạo sẵn với trạng thái `skipped`.

### 12.2 Giai đoạn và task

- Khi một giai đoạn bắt đầu, tạo task bằng `INSERT ... ON CONFLICT (job_id, stage, task_key) DO NOTHING` (idempotent). Giai đoạn kết thúc khi mọi task `succeeded|skipped`; sau đó khởi tạo giai đoạn kế tiếp.
- **Chính sách suy giảm có kiểm soát** (thay vì thất bại cả job):

| Giai đoạn | Khi task thất bại sau `max_attempts` |
|---|---|
| `extract` (cụm OCR) | Job `failed` |
| `profile` | Dùng hồ sơ mặc định bảo thủ + cảnh báo |
| `glossary` (P1) | Tiếp tục không có gợi ý + cảnh báo |
| `map` (một segment) | Đánh `extraction_degraded`, tiếp tục; **> 20% segment hỏng → job `failed`** |
| `consolidate` | Job `failed` |
| `write` (một mục) | **Phương án dự phòng tất định:** hiển thị `statement_vi` của các unit trong mục dưới dạng danh sách gạch đầu dòng (các câu này đã có bằng chứng nguyên văn), đánh `degraded_section` |
| `verify` (LLM lỗi) | Bỏ bước LLM cho mục đó, đánh "chưa kiểm chứng đầy đủ", **hạng tối đa B** |
| `repair` | Giữ nguyên, đánh cờ khối còn lỗi |
| `translate` (một segment) | LLM dịch lỗi → chạy lại; vẫn hỏng → đánh `flagged`; > 15% → hạng C |
| `assemble` | Job `failed` (thường do lỗi hệ thống; hoàn tín dụng đầy đủ) |

### 12.3 Vòng nhận việc của worker

```python
while True:
    reclaim_stale_tasks()                                   # task mồ côi (mất heartbeat > 3 phút)
    for t in claim_tasks(WORKER_ID, limit=WORKER_CONCURRENCY, per_job_limit=PER_JOB):
        spawn(run_task, t)                                  # heartbeat mỗi 30 giây
    sleep(0.5 if got_any else 2.0)

def run_task(t):
    try:
        ensure_not_canceled(t.job_id); guard_cost(t.job_id)
        result = HANDLERS[t.stage](t)                       # IDEMPOTENT: ghi kết quả bằng UPSERT theo khoá tự nhiên
        mark_succeeded(t, result); advance_stage_if_done(t.job_id)
    except RetryableError:
        schedule_retry(t, delay=min(5 * 2**t.attempt, 300) + jitter()) if t.attempt < t.max_attempts else mark_failed(t)
    except FatalError as e:
        mark_failed(t, e)
```

Ghi chú: `claim_tasks` đã chứa `FOR UPDATE SKIP LOCKED`, lọc job `running` chưa yêu cầu huỷ và giới hạn đồng thời theo job bằng advisory lock; có test hành vi trong `tests/pg_smoke.py`.

### 12.4 Hai tầng thử lại

| Tầng | Phạm vi | Chính sách |
|---|---|---|
| Trong pool | Một lời gọi | Lỗi tạm thời (5xx, timeout, 429): chuyển NGAY sang deployment khác của profile (`exclude`), tối đa 3 deployment khác nhau mỗi lần; cooldown, circuit breaker và cách ly khoá thay cho backoff mù (§17.7); tôn trọng `Retry-After`; JSON sai: 1 lần kèm phản hồi rồi leo deployment khác |
| Hoãn do hạn mức | Một task | Pool trả `Wait(until)`: `defer_task(until)`, **không** tính lần thử, không phát lỗi; tổng thời gian chờ của job bị chặn bởi `max_pool_wait_hours` |
| Task | Một task của pipeline | `max_attempts = 3`; chờ `5s·2^attempt` (tối đa 300 giây) cho lỗi thật (không áp cho chờ hạn mức) |

### 12.5 Tiến độ

`progress.pct = Σ trọng số giai đoạn đã xong + trọng số giai đoạn hiện tại × (task xong / tổng task)`.

| Mức | Trọng số |
|---|---|
| Tổng hợp (2-4) | extract 5 · profile 2 · segment 1 · glossary 6 · map 30 · consolidate 8 · write 25 · verify 15 · repair 5 · assemble 3 (= 100) |
| Dịch đầy đủ (1) | extract 5 · profile 2 · segment 1 · glossary 7 · translate 80 · assemble 5 (= 100) |

Giai đoạn `repair` có thể không chạy; khi bị bỏ qua, trọng số của nó dồn vào `assemble` để thanh tiến độ không "nhảy lùi".

### 12.6 Vòng đời tín dụng

| Sự kiện | Ghi sổ (`credit_ledger`) |
|---|---|
| Tạo job | `job_charge` −`est_credits` (idempotent, khoá `charge:{job_id}`) |
| Thành công, hạng A hoặc B | Giữ nguyên |
| Thành công, hạng C | `job_refund` +50% số đã trừ (`refund:{job_id}:grade_c`) |
| Thất bại do hệ thống (mọi giai đoạn) | Hoàn 100% (`refund:{job_id}:failed`) |
| Huỷ khi `queued` hoặc trước khi vào `map`/`translate` | Hoàn 100% |
| Huỷ sau khi đã vào `map`/`translate` | Hoàn 50% |
| Chạm trần chi phí job (`job_cost_cap`) | Hoàn theo tỷ lệ tiến độ chưa dùng |
| Chạm trần chi tiêu ngày | Job mới bị từ chối **trước** khi trừ tiền |

### 12.7 Chốt chặn chi phí theo job

Trước mỗi task: `if jobs.actual_shadow_usd > jobs.max_cost_usd: raise FatalError("job_cost_cap")` với `max_cost_usd = 1.5 × est_cost_usd`. Sau mỗi lời gọi LLM, `pool_call_cost()` trả hai số: **chi phí bóng** (giá tham chiếu, kể cả khi deployment miễn phí) cộng vào `jobs.actual_shadow_usd`, và **tiền thật** (0 với deployment miễn phí) cộng vào `jobs.actual_cost_usd` và `add_spend()`. Trần job so với chi phí *bóng* vì một vòng lặp lỗi trên deployment miễn phí không tốn tiền nhưng đốt hạn mức khan hiếm và làm chậm mọi người khác; còn trần ngày (§13.6) chỉ đếm tiền thật. Điều này giới hạn thiệt hại của bất kỳ lỗi lặp vô hạn hay tài liệu "bệnh" nào.

### 12.8 Huỷ, thời gian chờ, tiếp tục

- **Huỷ hợp tác:** `cancel_requested = true`; task chưa nhận sẽ không được nhận; lời gọi LLM đang chạy tự kết thúc và kết quả bị bỏ; job chuyển `canceled` rồi hoàn tín dụng theo §12.6.
- **Thời gian chờ:** lời gọi LLM 240 giây; task 15 phút; job 3 giờ (quá thì `failed` mã `job_timeout`, hoàn tín dụng).
- **Tiếp tục sau sự cố:** do mọi task idempotent và UNIQUE `(job_id, stage, task_key)`, khởi động lại worker không làm lặp kết quả hay tính tiền hai lần; task có `input_hash` không đổi có thể bỏ qua.

---

## 13. Hạn mức, chi phí, chống lạm dụng

### 13.1 Tín dụng

**1 tín dụng = 1 "trang quy đổi" = 300 từ nguồn × hệ số mức.** Hệ số xấp xỉ tỷ lệ chi phí thật (làm tròn 0.05): `full_translation` 1.20 · `detailed_synthesis` 1.35 · `deep_synthesis` **1.00** · `executive_brief` 0.70. Hằng số khởi điểm, hiệu chỉnh ở Giai đoạn 0 (`reference/estimator.py`). Người dùng được **giá cố định theo ước tính** (không phụ thuộc chi phí thật, cũng không phụ thuộc deployment nào thực sự phục vụ: free hay trả phí); chênh lệch do hệ thống chịu và được giám sát. Có thể đặt giá thấp hơn cho chế độ "Tiết kiệm" (cần thêm một hệ số vào `reference/estimator.py`); mặc định giá hai chế độ như nhau để đơn giản.

### 13.2 Chi phí dự kiến

<!-- COST_TABLE -->

**Bảng trên là chi phí theo giá tham chiếu (một model trả phí).** Chi phí tiền thật thấp hơn tuỳ tỷ lệ lời gọi do nhóm miễn phí phục vụ: mô phỏng ở §17.12 cho thấy cùng 10 tài liệu 300 trang tốn khoảng 3 USD thay vì 7,4 USD khi có hai dự án free làm tầng đầu, và 0 USD khi đủ dự án free, nhưng free tier có hạn mức nhỏ, đổi được và kèm điều kiện riêng tư (§17.3). Mức dịch đầy đủ dùng LLM dịch trực tiếp (P9), cùng một bảng giá.

Ghi chú: ước tính thô của spec (token tiếng Việt ≈ 1.8 token/từ nguồn khi dịch, số lần đọc nguồn theo từng mức, thinking +10-30%), chưa tính OCR. **Cloud Translation NMT** tính 20 USD/1M ký tự (500k ký tự đầu mỗi tháng miễn phí): một cuốn ~90k từ ≈ 540k ký tự ≈ 11 USD nếu đã hết hạn mức miễn phí, trong khi LLM Flash dịch toàn văn cùng khối lượng chỉ khoảng 0.9 USD theo bảng trên. OCR bằng Gemini cho PDF scan: mỗi trang quét ≈ 258 token ảnh đầu vào cộng chữ đầu ra, cỡ 0.2-0.5 USD cho 300 trang (ước tính).

### 13.3 Gợi ý định giá khi bán (cổng C)

Chi phí LLM mỗi tín dụng ở mức mặc định khoảng 0.0025 USD (giá khuyến mãi) và 0.005 USD (từ 01/01/2027). Giá bán NÊN ≥ 3× chi phí để chừa chỗ cho hạ tầng, thanh toán, hỗ trợ, hoàn tiền hạng C. Mọi bảng giá bán PHẢI tính theo giá LLM **sau 01/01/2027**.

### 13.4 Giới hạn và tốc độ (khởi điểm)

| Hạng mục | Giá trị | Lưu ở |
|---|---|---|
| Số từ tối đa / tài liệu (mức 2-4) | 250.000 | `app_settings.max_words_per_document` |
| Số từ tối đa / tài liệu (mức dịch đầy đủ) | 120.000 | `max_words_full_translation` |
| Dung lượng file | 50 MB | `MAX_UPLOAD_MB` |
| Job chạy đồng thời / người dùng | 1 | `max_active_jobs_per_user` |
| Lưu tài liệu gốc | 14 ngày | `document_retention_days` |
| Tín dụng tặng khi đăng ký (cổng B) | 100 | `free_signup_credits` |
| Trần chi tiêu ngày (chỉ tiền thật) | 20 USD | `daily_spend_cap_usd` |
| Các mức đang bật | cả bốn mức | `enabled_levels` |
| Job dịch đầy đủ / người dùng / ngày | 3 | `full_translation_daily_limit` |
| Phần hạn mức dành riêng cho tác vụ ưu tiên cao/thường | 20% | `pool_priority_reserve` |
| Cho phép chuyển sang tầng trả phí khi tầng miễn phí hết hạn mức | bật | `paid_spill_enabled` |
| Tổng thời gian chờ hạn mức tối đa của một job | 12 giờ | `max_pool_wait_hours` |
| Cổng triển khai hiện hành | `dev` | `deploy_gate` |
| Cho phép dùng nhóm gắn cờ rủi ro (`multi_account_risk`, `trial_only`) ở cổng công khai (B/C) | tắt | `pool_allow_risk_at_public_gates` |
| Nước bị hạn chế dùng free tier của Gemini | EEA, Thụy Sĩ, Anh | `restricted_free_tier_countries` |
| Đăng nhập | 10 lần / 10 phút / IP | Redis |
| Đổi mã mời | 5 lần / giờ / người dùng; 20 lần / giờ / IP | Redis |
| Tải lên | 10 / giờ / người dùng | Redis |
| Tạo job | 20 / ngày / người dùng | Redis |
| API chung | 120 / phút / người dùng | Redis |
| Kết nối SSE | 3 / người dùng | Redis |

### 13.5 Ba cổng triển khai

| Cổng | Cách vào | Cài đặt | Điều kiện sang cổng sau |
|---|---|---|---|
| **A. Closed beta** (20-50 người) | Chỉ có mã mời (300-500 tín dụng/mã) | Trần 10-20 USD/ngày; **cả bốn mức bật**; nhóm free được dùng theo `allowed_gates` (kể cả khoá gắn `multi_account_risk`, chủ hệ thống tự chịu rủi ro tài khoản); mặc định `standard` kèm đồng ý riêng; nhóm miễn phí gắn cờ rủi ro chỉ được bật khi có xác nhận `risk_ack` (§17.14) | Đạt KPI §2.2 trên ≥ 100 job thật; chi phí thật/ước tính trong ±35%; tải hỗ trợ chấp nhận được |
| **B. Đăng ký mở** | Google/email + xác minh email + Turnstile | 100 tín dụng tặng; trần 20-50 USD/ngày; giới hạn số đăng ký mới/ngày; hàng chờ nếu chạm trần; **mặc định `private`** cho người mới (`standard` là lựa chọn có đồng ý riêng); nhóm gắn `multi_account_risk` và `trial_only` tự bị loại (`allowed_gates`) | Chi phí/người dùng ổn định; không có sự cố pháp lý; checklist §14.5 đạt |
| **C. Bán tín dụng** | Thanh toán (PayOS/VNPay/MoMo cho người dùng Việt Nam, Stripe quốc tế) | Giá bán ≥ 3× chi phí (§13.3); hoá đơn; chính sách hoàn tiền; pool chủ yếu trả phí, free chỉ cho `standard` có đồng ý | Có pháp nhân, tài khoản thanh toán, điều khoản bán hàng |

### 13.6 Chính sách trần chi tiêu ngày

| Mức đã chi so với trần | Hành động |
|---|---|
| ≥ 50% | Cảnh báo Admin (Slack/email) |
| ≥ 80% | Cảnh báo khẩn; ngừng tặng tín dụng miễn phí mới |
| ≥ 100% | `spend_daily.paused = true`: deployment trả phí bị loại khỏi ứng viên; job `private` mới trả 503 `spend_cap_reached`; job `standard` vẫn nhận được nếu nhóm miễn phí còn chỗ; job đang chạy **được chạy nốt** trên phần miễn phí hoặc chờ; Admin có thể nâng trần thủ công |

### 13.7 Chống lạm dụng

- Cổng đăng ký nhiều lớp (mã mời → xác minh email → Turnstile); chặn email dùng một lần; giới hạn theo IP và theo tài khoản.
- Một job chạy đồng thời/người dùng; kích thước và số từ có trần; số job dịch đầy đủ mỗi ngày có trần; tín dụng trừ trước.
- Phát hiện bất thường: người dùng tiêu quá 5× trung bình trong 24 giờ → cờ tự động cho Admin xem.
- Báo cáo lạm dụng/vi phạm qua `/takedown`; Admin tạm khoá tài khoản (`status = suspended`).

---

## 14. Bảo mật, riêng tư, pháp lý

> Phần 14.4 là tổng hợp kỹ thuật dựa trên các nguồn công khai ở Phụ lục D, **không phải tư vấn pháp lý**. PHẢI nhờ luật sư xác nhận trước cổng B.

### 14.1 Mô hình mối đe doạ

| Mối đe doạ | Ví dụ | Biện pháp |
|---|---|---|
| File tải lên độc hại | PDF/DOCX khai thác lỗi, zip-bomb | Parser trong sandbox không mạng, giới hạn giải nén/tỷ lệ nén/thời gian, không chạy macro, kiểm tra magic bytes |
| XSS qua nội dung | Markdown chứa script | Không cho HTML thô; làm sạch theo allowlist; CSP chặt; không tải ảnh ngoài |
| Prompt injection trong tài liệu | "Bỏ qua chỉ dẫn trước đó..." | Dữ liệu đặt trong thẻ và khai báo là DỮ LIỆU; schema cứng; không cấp công cụ; kiểm tra đầu ra; hậu quả chỉ ảnh hưởng chính người tải lên |
| Template injection | Văn bản chứa `{{style_core}}` | Thay biến một lượt (có test) |
| Chèn chỉ thị qua Lõi văn phong | Lõi (nhất là lõi do người dùng tạo, P2) chứa "bỏ qua mọi quy tắc", yêu cầu đổi định dạng đầu ra hoặc URL | `lint_style_core` chặn cụm chỉ thị; chỉ phiên bản đã duyệt được dùng; thẻ `<style_core>` kèm câu chốt "chỉ chỉnh cách diễn đạt"; thoát `<` `>`; quy tắc bất biến nằm ngoài lõi |
| IDOR | Đoán UUID của job/báo cáo | Mọi truy vấn lọc theo `user_id`; trả 404; RLS làm lớp thứ hai |
| Đốt ngân sách | Bot đăng ký hàng loạt | Mã mời, Turnstile, xác minh email, tín dụng, trần chi tiêu, giới hạn tốc độ |
| Lộ khoá LLM | Khoá nằm ở client, log, bản sao lưu | Mã hoá ở tầng ứng dụng với khoá chủ **ngoài** bản sao lưu; API chỉ trả 4 ký tự cuối; không log; khoá bị từ chối tự bị cách ly; xoay khoá; ràng buộc CHECK chặn dán khoá rõ vào cột tham chiếu |
| Khoá hoặc tài khoản nhà cung cấp bị khoá | Gom nhiều tài khoản free để cộng hạn mức; dùng endpoint thử nghiệm trong production | Cờ `multi_account_risk`, `trial_only`, `no_personal_data` kèm `allowed_gates` (chỉ ở `dev`/`A`); luôn có tầng trả phí dự phòng; Admin thấy cảnh báo trước khi bật cờ rủi ro (§17.3) |
| Chuỗi cung ứng thư viện giữ khoá | Gói bị chèn mã độc (LiteLLM 1.82.7 và 1.82.8 trên PyPI, 24/03/2026) | Tự viết lõi mỏng thay vì kéo cả gateway; ghim phiên bản và hash (`pip --require-hashes`); chặn egress của worker chỉ tới các nhà cung cấp đã khai báo; không chạy `pip install` không ghim trong build |
| Rò dữ liệu qua log | Log chứa văn bản tài liệu | Không log nội dung; `llm_calls` không lưu nội dung; Sentry bật lọc dữ liệu |
| Lộ dữ liệu qua nhà cung cấp LLM | Dữ liệu dùng để huấn luyện, người thật đọc | Job `private` chỉ tới nhóm `no_training`; job `standard` cần đồng ý riêng và cảnh báo "đừng dùng cho tài liệu nhạy cảm"; `llm_calls.data_policy` làm bằng chứng kiểm toán (§17.4); xem xét DPA của nhà cung cấp trả phí |
| Bản quyền | Tải sách có bản quyền | Xác nhận quyền, ToS, `/takedown`, hạn lưu, không thư viện công khai |
| Phụ thuộc/giấy phép | Thư viện AGPL | Kiểm toán giấy phép trong CI |

### 14.2 Biện pháp kỹ thuật bắt buộc

1. **Làm sạch hiển thị:** Markdown → HTML bằng allowlist (`p, ul, ol, li, strong, em, blockquote, table, thead, tbody, tr, th, td, h2-h4, a[href^=https]` với `rel="noopener nofollow"`), không ảnh, CSP `default-src 'self'`.
2. **Bí mật:** trên một VPS: tệp `.env` quyền 0600 hoặc Docker secrets; `POOL_MASTER_KEY` tách riêng và không nằm trong bản sao lưu; xoay khoá định kỳ và ngay khi nghi ngờ; khoá free và khoá trả phí ở các biến khác nhau; không commit.
3. **Mã hoá:** TLS mọi nơi; mã hoá từng tệp gốc và khoá API ở tầng ứng dụng (VPS thường không có mã hoá đĩa do người dùng kiểm soát); sao lưu mã hoá ngoài máy tại nhà cung cấp khác, khôi phục thử hằng quý (§20.6).
4. **Quyền:** Admin bắt buộc 2FA; ghi `audit_log` cho mọi thao tác Admin và thao tác nhạy cảm (đổi tín dụng, xoá dữ liệu).
5. **Xoá dữ liệu:** xoá tài liệu ≤ 24 giờ; xoá tài khoản ≤ 30 ngày (xoá cứng, chỉ giữ dữ liệu kế toán tối thiểu nếu luật yêu cầu); báo cáo/glossary theo yêu cầu.
6. **Phụ thuộc:** quét lỗ hổng tự động (Dependabot/Renovate + `pip-audit`/`npm audit`), ghim phiên bản.

### 14.3 Dữ liệu gửi cho nhà cung cấp LLM

Quy tắc theo **chế độ riêng tư của job** (chi tiết và nguồn: §17.3-17.4):

- **`private`:** chỉ nhóm hạn mức có `data_policy = no_training` và không gắn `trial_only`/`no_personal_data`. Với Gemini đó là dự án có thanh toán (Paid Services: Google không dùng prompt và phản hồi để cải thiện sản phẩm, chỉ ghi log có thời hạn để chống vi phạm chính sách). Mọi nhóm `unknown` bị coi như dùng dữ liệu.
- **`standard`:** được dùng cả nhóm miễn phí; người dùng PHẢI đã **đồng ý riêng** (`consent_shared_processing_at`, không gộp với ToS) sau khi đọc cảnh báo: nội dung có thể được nhà cung cấp dùng để cải thiện sản phẩm và có thể được người thật xem; **không dùng cho tài liệu chứa thông tin cá nhân hay bí mật**. Người dùng ở EEA, Thụy Sĩ, Anh không bao giờ rơi vào nhóm miễn phí của Gemini (điều khoản của Google, §17.3).
- Người dùng PHẢI **đồng ý riêng** việc gửi nội dung tài liệu cho nhà cung cấp LLM ở nước ngoài (`consent_cross_border_at`); xác nhận từ 18 tuổi (`age_confirmed_at`).
- Dùng dự án riêng cho production; không gửi email, ID người dùng hay thông tin định danh vào prompt (chỉ nội dung tài liệu).
- Mỗi lời gọi lưu `data_policy` và `group_tier` của nhóm **tại thời điểm gọi** vào `llm_calls`: truy vấn kiểm toán "mọi job `private` có 0 lời gọi tới nhóm không `no_training`" phải chạy được và là một kiểm tra định kỳ (§17.4).
- Kiểm tra thời hạn lưu giữ của API và tắt lưu trữ tương tác nếu có tuỳ chọn (§22).

### 14.4 Pháp lý (Việt Nam)

| Chủ đề | Tình trạng (tại 02/10/2026) | Hành động cho sản phẩm |
|---|---|---|
| **Luật Bảo vệ dữ liệu cá nhân** (Luật 91/2025/QH15) và **Nghị định 356/2025/NĐ-CP** hướng dẫn thi hành | Luật thông qua 26/6/2025, **hiệu lực 01/01/2026**; áp dụng cả với tổ chức nước ngoài xử lý dữ liệu của người Việt; đồng ý phải tự nguyện, cụ thể, theo từng mục đích. Nghị định 356 (31/12/2025): phải lập hồ sơ đánh giá tác động xử lý dữ liệu và **hồ sơ đánh giá tác động chuyển dữ liệu ra nước ngoài**, nộp trong 60 ngày; hồ sơ chuyển ra nước ngoài áp dụng khi dữ liệu thu thập hoặc lưu ở Việt Nam được chuyển tới **máy chủ ngoài Việt Nam hoặc nhà cung cấp đám mây nước ngoài**, hoặc được xử lý trên nền tảng ngoài Việt Nam; cơ quan chuyên trách (A05, Bộ Công an) thẩm định trong 15 ngày. Miễn trừ: hộ kinh doanh và doanh nghiệp siêu nhỏ miễn hoàn toàn; doanh nghiệp nhỏ và khởi nghiệp được 5 năm kể từ 01/01/2026; **không áp dụng** nếu cung cấp dịch vụ xử lý dữ liệu cá nhân, trực tiếp xử lý dữ liệu cá nhân nhạy cảm, hoặc đạt 100.000 chủ thể dữ liệu | **Với VPS đặt ở nước ngoài và LLM nước ngoài, mọi dữ liệu của người dùng Việt đều đi ra ngoài lãnh thổ** (lưu trữ lẫn xử lý). Một cá nhân vận hành chưa đăng ký hộ kinh doanh hay doanh nghiệp có thể **không** thuộc nhóm được miễn: luật sư xác nhận (có nên đăng ký hộ kinh doanh/doanh nghiệp siêu nhỏ để được miễn không, và còn miễn không nếu người dùng tải tài liệu có dữ liệu nhạy cảm); đồng ý riêng, chi tiết từng mục đích; chính sách quyền riêng tư nêu rõ ai là bên kiểm soát dữ liệu, máy chủ ở đâu, nhà cung cấp LLM nào; quy trình quyền của chủ thể dữ liệu; hạn lưu; thoả thuận chuyển dữ liệu bằng văn bản với bên nhận (Nghị định 356 yêu cầu); xem §20.8 |
| **Luật Trí tuệ nhân tạo** (Luật 134/2025/QH15) | Thông qua 10/12/2025, **hiệu lực 01/03/2026**; Điều 11 về minh bạch: thông báo khi người dùng tương tác với AI; gắn nhãn nội dung AI tạo/chỉnh sửa có thể gây nhầm lẫn về tính xác thực; Chính phủ sẽ quy định chi tiết hình thức thông báo/gắn nhãn | Banner "nội dung do AI tổng hợp" luôn hiển thị, nhắc ở đầu/cuối mọi file xuất, ghi vào metadata; **theo dõi nghị định hướng dẫn**; xác nhận phân loại rủi ro (giả định: thấp) |
| **Bản quyền** | Tài liệu người dùng tải lên thường có bản quyền của bên thứ ba; bản dịch toàn văn là tác phẩm phái sinh nên rủi ro cao hơn bản tổng hợp | Xác nhận quyền ở mỗi lần tải (`rights_attested_at`); ToS cấm tải nội dung vi phạm; `/takedown` xử lý ≤ 72 giờ; không có thư viện/chia sẻ công khai; hạn lưu ngắn; **mức `full_translation` được bật từ cổng A (D8)** nên rào chắn thay vì tắt: 120.000 từ/tài liệu, 3 job dịch đầy đủ/người dùng/ngày, bản dịch gắn cảnh báo "chỉ dùng cá nhân, không phân phối lại", không xuất bản công khai |
| **Điều khoản của nhà cung cấp LLM** | Google: chỉ dùng Paid Services khi phục vụ người dùng ở EEA/Thụy Sĩ/Anh; từ 18 tuổi; không cố lách giới hạn (Google APIs ToS mục 2.d). NVIDIA API Trial: chỉ để thử nghiệm và đánh giá, không dùng trong production, không gửi dữ liệu cá nhân hay nhạy cảm | Cờ ToS trong cấu hình pool và `allowed_gates` (§17.4); xác nhận 18+; kiểm tra lại điều khoản mỗi quý vì nhà cung cấp đổi điều khoản (Gemini cập nhật 23/03/2026); lưu ngày kiểm tra trong `notes` của nhóm |
| **Giấy phép phần mềm** | PyMuPDF dùng AGPL-3.0 (hoặc giấy phép thương mại) | Kiểm toán giấy phép trong CI; dùng thư viện cho phép SaaS đóng mã (§5.2) |

**Trang pháp lý cần có trước cổng B:** Điều khoản sử dụng, Chính sách quyền riêng tư (mục đích, bên nhận dữ liệu, chuyển ra nước ngoài, thời hạn lưu, quyền người dùng, liên hệ), Chính sách cookie, Biểu mẫu `/takedown`.

### 14.5 Checklist trước khi mở cổng B

- [ ] Luật sư xác nhận hồ sơ PDPL (đánh giá tác động xử lý và chuyển dữ liệu) và phân loại rủi ro theo Luật AI
- [ ] ToS, Privacy, Cookie, `/takedown` đã xuất bản; đồng ý riêng về chuyển dữ liệu ra nước ngoài đã hoạt động
- [ ] Parser sandbox đã kiểm thử với tệp độc hại và zip-bomb; kiểm tra XSS/IDOR đã chạy
- [ ] Trần chi tiêu, cảnh báo, giới hạn tốc độ, Turnstile đã bật; đã diễn tập "chạm trần"
- [ ] Sao lưu + khôi phục đã diễn tập; runbook sự cố đã có
- [ ] Kiểm toán giấy phép sạch; khoá trả phí thuộc dự án riêng, tách biến môi trường khỏi khoá free
- [ ] Pool: cấu hình `allowed_gates` đúng (nhóm `multi_account_risk` và `trial_only` không có `B`/`C`); truy vấn kiểm toán "job `private` chạm nhóm không `no_training`" trả 0; đã diễn tập chạm trần, hết hạn mức ngày, khoá bị từ chối, nhà cung cấp sập
- [ ] VPS: SSH chỉ khoá, tường lửa 80/443, cập nhật tự động, sao lưu ngoài máy đã khôi phục thử, `POOL_MASTER_KEY` không nằm trong bản sao lưu, giám sát ngoài máy hoạt động (§20)
- [ ] Hồ sơ PDPL cho chuyển dữ liệu ra nước ngoài (hoặc căn cứ miễn trừ) đã được luật sư xác nhận cho đúng tư cách pháp lý của người vận hành
- [ ] Đã xác minh thời hạn lưu giữ dữ liệu của API LLM và tắt lưu nếu có

---

## 15. Quan sát và vận hành

**Log:** JSON có `job_id`, `stage`, `task_id`, `prompt_id`, `model`; **không** chứa nội dung tài liệu.

**Chỉ số (Prometheus/OpenTelemetry):** `visynth_job_duration_seconds{level,stage}`, `visynth_llm_tokens_total{model,prompt_id,direction}`, `visynth_llm_cost_usd_total{prompt_id}`, `visynth_llm_errors_total{code}`, `visynth_queue_wait_seconds`, `visynth_quality_grade_total{grade}`, `visynth_spend_cap_ratio`, `visynth_ocr_pages_total`, `visynth_credit_refund_total{reason}`; **pool:** `visynth_pool_headroom{deployment}`, `visynth_pool_calls_total{deployment,outcome}`, `visynth_pool_wait_seconds`, `visynth_pool_circuit_open{deployment}`, `visynth_pool_spill_total{from_tier,to_tier}`, `visynth_pool_shadow_usd_total{tier}`, `visynth_pool_private_violations_total` (phải luôn bằng 0).

**Bảng điều khiển:** chi phí/ngày và theo prompt; số job và tỷ lệ thành công; phân bố hạng A/B/C; p50/p95 thời gian theo mức; hàng đợi; lỗi theo mã; phần trăm tài liệu cần OCR.

**Cảnh báo:**

| Điều kiện | Mức |
|---|---|
| Chi tiêu ≥ 80% trần ngày | Khẩn |
| Tỷ lệ job thất bại > 10% trong 15 phút | Khẩn |
| p95 chờ hàng đợi > 5 phút | Cảnh báo |
| Tỷ lệ 429 từ LLM > 5% | Cảnh báo |
| Tỷ lệ hạng C > 20% trong ngày | Cảnh báo (nghi prompt/model hỏng) |
| Số task `lease_expired` tăng đột biến | Cảnh báo (worker không ổn định) |
| `visynth_pool_private_violations_total` > 0 | **Khẩn** (job riêng tư chạm nhóm không đủ điều kiện: dừng ngay, điều tra) |
| Một khoá bị cách ly (`auth_error`) | Cảnh báo (thường do khoá hết hạn hoặc bị nhà cung cấp thu hồi) |
| Toàn bộ nhóm của một tier không còn khoá hoạt động hoặc circuit mở > 15 phút | Cảnh báo |
| Tỷ lệ lời gọi phải chuyển sang trả phí > 50% trong ngày | Cảnh báo (free tier đã cạn hoặc hạn mức đã đổi: xem lại `limits`) |
| Số lỗi 429 từ nhà cung cấp > 2% lời gọi | Cảnh báo (cấu hình `limits` đang cao hơn thực tế, `limit_scale` đang tự thu hẹp) |

**Runbook cần có:** (1) một nhà cung cấp sập hoặc 429 kéo dài → pool tự chuyển tầng; kiểm tra `llm_incidents`, hạ `weight` hoặc tắt deployment, thông báo trạng thái; (2) chạm trần chi tiêu; (3) job kẹt (kiểm tra `reclaim_stale_tasks`); (4) phát hành prompt xấu → hoàn tác về phiên bản trước (§8.3); (5) xử lý `/takedown`; (6) nghi lộ khoá → xoay khoá, rà `llm_calls`; (7) khôi phục CSDL; (8) khoá bị cách ly → xác minh với nhà cung cấp, thay khoá, gỡ `quarantined`; (9) nhà cung cấp đổi hạn mức hoặc điều khoản → cập nhật `limits`/`tos_flags`/`data_policy`, chạy lại kiểm định (`probe`), ghi ngày kiểm tra; (10) VPS chết hoặc đĩa đầy (§20.7).

---

## 16. Đánh giá chất lượng

### 16.1 Bộ golden set (≥ 24 tài liệu)

| Loại | Số lượng | Mục đích |
|---|---|---|
| Transcript bài giảng (văn nói, có hỏi đáp, nhiều thuật ngữ riêng) | 6 | Đúng tinh thần mục tiêu ban đầu của sản phẩm |
| Chương sách | 6 | Cấu trúc dài, nhiều tầng ý |
| Bài báo khoa học / bài viết | 4 | `neutral_facts`, nhiều số liệu |
| PDF scan | 3 | OCR, `ocr_noise` |
| DOCX/EPUB lộn xộn | 3 | Cấu trúc xấu, bảng, chú thích |
| Tài liệu nguồn tiếng Việt | 2 | Nhánh nguồn = đích |

Mỗi tài liệu đi kèm: **danh sách "phải phủ"** (30-60 khái niệm cốt lõi do người xác lập), **"trap facts"** (≥ 10 con số/ngày/tên để bắt lỗi số liệu), glossary, và với ≥ 5 tài liệu có **bản tham chiếu** (báo cáo của Gemini Notebook + bản người biên tập).

### 16.2 Chỉ số và thang chấm

**Tự động:** `coverage_core` (so với danh sách phải phủ, bằng bộ chấm hiệu chỉnh trên 50 ca người chấm), `faithfulness_rate`, độ chính xác trap facts, nhất quán thuật ngữ, tỷ lệ độ dài, chi phí, thời gian.

**Người chấm (1-5, hai người, lệch ≥ 2 thì thảo luận):** Đầy đủ · Chính xác · Văn phong tiếng Việt · Cấu trúc · Hữu ích.

Bộ chấm tự động của M0 nằm ở `eval/score.py` (chấm `run.json` do `visynth run --artifacts` ghi ra, chạy offline, không tốn token); thang người chấm và hằng số chặn hồi quy ở `eval/rubric.md`; định dạng tài liệu golden ở `eval/golden/schema.json`.

### 16.3 Kiểm thử hồi quy khi đổi prompt, model hoặc cấu hình

Chạy tập con 6 tài liệu và **chặn phát hành** khi đổi prompt, model, cấu hình pool (thêm/bỏ deployment, đổi `needs.min_quality`, đổi thứ tự tầng) hoặc phiên bản Lõi văn phong, nếu: `coverage_core` giảm > 0.03, `faithfulness_rate` giảm > 0.02, có lỗi trap fact, chi phí tăng > 20%, hoặc p95 thời gian tăng > 30% so với baseline.

Công cụ M0: `eval/compare_baseline.py --baseline … --candidate …` so hai thư mục kết quả trong `eval/runs/` theo đúng các ngưỡng trên (mã thoát 1 khi chặn).

### 16.4 Bake-off chọn model (Giai đoạn 0)

3 cấu hình profile × 5 tài liệu; chấm mù theo cặp so với bản tham chiếu; chọn cấu hình **rẻ nhất đạt ngưỡng** §2.2. Thử riêng một `verifier` thuộc họ model khác `writer` để xem có giảm lỗi tương quan không. Với pool, bài này gồm thêm: (a) chạy bài kiểm định (`probe`, §17.9) cho từng deployment ứng viên và ghi điểm `json`, `vi_write`, `long_context`; (b) đo hạn mức thật (đọc bảng điều khiển, đối chiếu với lỗi 429) và nhập vào `limits`; (c) đo `tokenizer_factor` thật; (d) chạy `declare` + `probe` cho các khoá thật, ghi ngày kiểm tra vào `notes` của nhóm (§17.14); (e) khởi tạo Lõi văn phong và glossary cho lĩnh vực đầu tiên bằng quy trình §19.9 rồi chạy golden set. Ghi kết quả vào `eval/` và cập nhật `pool_config` (`quality`, `limits`, `tiers`).

Công cụ M0: `eval/bakeoff.py --configs … --golden …` chạy N cấu hình trên cùng tập tài liệu, chấm bằng `eval/score.py` rồi chọn cấu hình **rẻ nhất đạt ngưỡng**; báo cáo kèm họ model của người viết/người kiểm (cảnh báo khi cùng họ) và điểm `probe` nếu có `*.probe.json` cạnh cấu hình. Việc nhập số đo vào cấu hình làm bằng `visynth pool apply-probe --config … --probe … [--limits …]` — mặc định chỉ xem trước, chỉ ghi khi cấu hình mới đã qua `validate_config`.

### 16.5 Vòng phản hồi trực tuyến

Chấm sao và "báo lỗi đoạn" (`feedback`); phân loại hằng tuần; ca đáng giá đưa vào golden set; theo dõi phân bố hạng A/B/C và tỷ lệ báo lỗi theo mức và loại tài liệu.
