
---

## 9. Hợp đồng dữ liệu (JSON Schema)

Mọi đầu ra có cấu trúc của LLM và các đối tượng trao đổi giữa các giai đoạn đều có JSON Schema (Draft 2020-12) trong `schemas/`, mỗi schema có ví dụ trong `examples/`, và **tất cả ví dụ cùng kể một câu chuyện nhất quán** trên một tài liệu giả lập (`fixture_document.json`) để dev có thể chạy thử từ đầu đến cuối mà chưa cần gọi LLM.

<!-- SCHEMA_TABLE -->

**Quy tắc làm việc với schema:**

1. **Hai lớp schema.** *Schema đầy đủ* (`schemas/`) dùng để kiểm tra phía server (có `pattern`, `minLength`, `maxLength`...). *Wire schema* gửi cho Gemini được sinh bằng `reference/gemini_schema.py`: Gemini chỉ hỗ trợ một tập con JSON Schema (không `pattern`, `minLength`, `maxLength`, `oneOf/anyOf`...), nên các từ khoá này bị loại và các `$ref` được trải phẳng. Vì vậy các schema của spec **không dùng `oneOf/anyOf`**; trường nullable dùng `"type": ["string", "null"]`.
2. **Luôn kiểm tra lại đầu ra** bằng schema đầy đủ sau khi nhận (cú pháp đúng chưa chắc giá trị đúng; tài liệu Gemini cũng nêu điều này).
3. **Mã nguồn là Pydantic** ở backend; JSON Schema trong `schemas/` là hợp đồng. CI PHẢI so khớp `Model.model_json_schema()` với `schemas/` (contract test) và sinh kiểu TypeScript cho frontend từ cùng nguồn.
4. **ID do hệ thống cấp, không do model tự đặt**, trừ `local_id` (u1, u2...) trong một segment. `U-xxxx`, `S01.b02`, `SEG-001` đều được cấp/ánh xạ sau khi kiểm chứng.
5. **Enum phải khớp ở ba nơi:** JSON Schema, DDL (CHECK) và OpenAPI. `tools/validate_spec.py` kiểm tra tự động; thêm giá trị enum mới phải sửa cả ba.
6. Mọi schema đều có `additionalProperties: false`: model trả thừa trường thì bị từ chối và thử lại (§6.13).
7. **Cấu hình pool không bao giờ chứa khoá API.** `pool_config.schema.json` chỉ nhận `secret_ref` dạng `env:`, `file:` hoặc `enc:` (có test đột biến: dán khoá thật vào `secret_ref` bị từ chối). Cột `llm_credentials.secret_ref` có CHECK tương ứng.
8. **Đầu ra của prompt curation (P12, P13) không được tin về bằng chứng.** Model chỉ trả ID (`source_ref`, `ctx_ids`); code tra lại nội dung từ đầu vào đã cấp và loại ID lạ. Code cũng ép `origin = ai` và `reviewed = false` cho mọi mục do AI sinh ra.

---

## 10. Mô hình dữ liệu

Toàn bộ DDL: `db/schema.sql` ([[N_TABLES]] bảng; PostgreSQL ≥ 16, **không cần extension**, đã nạp và kiểm thử hành vi trên PostgreSQL thật).

### 10.1 Quan hệ chính

```
users ─┬─< documents ─┬─< doc_paragraphs
       │              ├─< doc_sections
       │              └─< jobs ─┬─< job_stages ─< job_tasks         (hàng đợi, §12)
       │                        ├─< job_events                      (SSE)
       │                        ├─< job_glossary_entries            (ảnh chụp glossary)
       │                        ├─< segments ─< knowledge_units
       │                        ├─< translation_items               (mức full_translation)
       │                        └─1 reports ─┬─< report_sections ─< report_blocks
       │                                     ├─< exports
       │                                     └─< feedback
       ├─< credit_ledger  (job_id → jobs)
       ├─< glossaries (owner) ─< glossary_entries      [glossary chuẩn: owner NULL, scope = shared]
       ├─< invite_redemptions >─ invite_codes
       └─< audit_log
recipes (chính thức hoặc của người dùng) ← jobs.recipe_id
llm_prices, llm_calls, app_settings, spend_daily, prompt_versions, takedown_requests

LLM Pool (§17)
llm_providers ─┬─< llm_models
               └─< llm_quota_groups ─┬─< llm_credentials
                                     └─< llm_deployments >─ llm_models     (đơn vị bộ định tuyến chọn: nhóm hạn mức x model)
llm_profiles ─< llm_profile_tiers          llm_scope_state (bucket, bộ đếm ngày, cooldown, circuit: theo group và deployment)
llm_leases (chỗ đã đặt)   llm_incidents   llm_probe_runs        llm_calls >─ llm_deployments

Lõi văn phong và curation (§19)
style_cores ─< style_core_versions ─< style_core_reviews         jobs ─> style_core_versions (lõi đã ghim)
curation_runs (P12, P13, thử lõi)   glossaries ─< glossary_entries (status: suggested → confirmed)   glossaries ─< glossary_releases
```

### 10.2 Danh sách bảng

| Nhóm | Bảng | Vai trò |
|---|---|---|
| Người dùng | `users`, `invite_codes`, `invite_redemptions` | Tài khoản, vai trò, đồng ý ToS và chuyển dữ liệu; mã mời |
| Tài liệu | `documents`, `doc_sections`, `doc_paragraphs` | Siêu dữ liệu, cấu trúc, **đoạn văn nguồn theo `pid`** (nguồn cho trích dẫn) |
| Thuật ngữ, công thức | `glossaries`, `glossary_entries`, `recipes` | Glossary cá nhân/chuẩn; công thức = tham số đóng gói |
| Job | `jobs`, `job_stages`, `job_tasks`, `job_events`, `job_glossary_entries` | Trạng thái, hàng đợi task, sự kiện, ảnh chụp glossary |
| Kết quả trung gian | `segments`, `knowledge_units` | Nhãn đoạn, bản kê khai khái niệm có bằng chứng |
| Kết quả cuối | `reports`, `report_sections`, `report_blocks`, `translation_items`, `exports` | Báo cáo theo khối (để hiển thị, đánh cờ, vá), bản dịch căn theo `pid`, file xuất |
| Tiền và chi phí | `credit_ledger` (+ view `v_credit_balance`), `llm_prices`, `llm_calls`, `app_settings`, `spend_daily` | Sổ tín dụng chỉ-ghi-thêm, bảng giá có ngày hiệu lực, nhật ký gọi LLM (không lưu nội dung; có deployment, tier, data_policy, tiền thật và chi phí bóng), cấu hình, trần chi tiêu |
| LLM Pool | `llm_providers`, `llm_models`, `llm_quota_groups`, `llm_credentials`, `llm_deployments`, `llm_profiles`, `llm_profile_tiers`, `llm_scope_state`, `llm_leases`, `llm_incidents`, `llm_probe_runs` | Cấu hình (nhà cung cấp, nhóm hạn mức, khoá dạng tham chiếu, deployment, profile và tầng) và trạng thái động (bucket, bộ đếm ngày, circuit, lease); sự cố; lịch sử kiểm định |
| Lõi văn phong, curation | `style_cores`, `style_core_versions`, `style_core_reviews`, `curation_runs`, `glossary_releases` | Lõi có phiên bản bất biến sau khi duyệt, nhật ký duyệt, các lần chạy AI đề xuất, bản phát hành glossary |
| Khác | `feedback`, `audit_log`, `takedown_requests`, `prompt_versions` | Phản hồi, kiểm toán, khiếu nại bản quyền, hash prompt |

### 10.3 Quyết định thiết kế quan trọng

1. **Sổ tín dụng chỉ-ghi-thêm:** số dư là tổng `delta`. `charge_credits` khoá hàng `users` (chống đua), idempotent theo `(user_id, idempotency_key)`; `refund_credits` không bao giờ hoàn quá số đã trừ cho job.
2. **Job chụp ảnh mọi thứ ảnh hưởng kết quả:** `model_profile`, `prompt_versions`, glossary (`job_glossary_entries`), `recipe_version`. Nhờ đó kết quả tái lập và điều tra được.
3. **Neo vị trí bằng `pid`:** báo cáo trích dẫn *unit*; unit giữ bằng chứng `{pid, quote}`. Khi tài liệu gốc hết hạn, `doc_paragraphs` bị xoá nhưng trích đoạn ngắn trong unit vẫn còn nên trích dẫn vẫn hiển thị được.
4. **Enum bằng `text + CHECK`** (dễ migrate hơn kiểu ENUM); đối chiếu chéo với schema/OpenAPI bằng script.
5. **Không lưu nội dung người dùng ở nơi không cần:** `llm_calls`, `job_events`, `audit_log` không chứa văn bản tài liệu.
6. **Khả năng mở rộng:** `doc_paragraphs` có thể phân vùng hash theo `document_id` khi vượt ~50 triệu dòng; `job_events` xoá sau 30 ngày; `credit_ledger` thêm bảng ảnh chụp số dư khi tổng quá lớn.
7. **RLS (tuỳ chọn):** ở cuối `schema.sql` có mẫu Row-Level Security làm lớp phòng thủ thứ hai (API đặt `app.user_id` mỗi giao dịch).
8. **Trạng thái pool nằm ở PostgreSQL, thời gian là epoch (giây, `double precision`).** Hàm SQL nhận `p_now` thay vì đọc đồng hồ để kết quả tái lập được và so khớp từng bước với `MemoryState`; production truyền `extract(epoch from clock_timestamp())`. Khoá được lấy theo thứ tự cố định (nhóm rồi deployment) để không deadlock.
9. **Tính bất biến do CSDL bảo đảm, không chỉ do ứng dụng:** trigger chặn sửa/xoá `style_core_versions` đã `approved` (chỉ cho chuyển sang `deprecated`), CHECK không cho `approved` khi còn quyết định mở hoặc thiếu người duyệt; `glossary_releases` là bản chụp, không sửa tại chỗ.
10. **Hai loại chi phí tách bạch:** `cost_usd` là tiền thật (0 với deployment miễn phí) và chỉ nó vào `spend_daily`; `shadow_cost_usd` tính theo giá tham chiếu cho mọi lời gọi, dùng cho trần chi phí job và hiệu chỉnh ước tính (§12.7, §17.11).
11. **Khoá API không nằm ở dạng rõ trong CSDL:** `secret_ref` (`env:`/`file:`) hoặc `secret_enc` (AES-GCM ở tầng ứng dụng, khoá chủ ngoài CSDL). API chỉ trả `last4`.

### 10.4 Hàm nghiệp vụ trong CSDL (đều có test hành vi)

| Hàm | Ngữ nghĩa | Lỗi → mã API |
|---|---|---|
| `credit_balance(user)` | Số dư hiện tại | — |
| `charge_credits(user, n, job, idem)` | Trừ n tín dụng nguyên tử; gọi lặp cùng `idem` không trừ thêm; cập nhật `jobs.charged_credits` | `insufficient_credits` → 402 |
| `refund_credits(user, n, job, idem)` | Hoàn tối đa số đã trừ cho job; idempotent | — |
| `redeem_invite(code, user)` | Đổi mã (không phân biệt hoa/thường), cộng tín dụng, tăng `used_count` | `invite_invalid` → 404; `invite_exhausted` → 409; đã đổi rồi (vi phạm PK) → 409 `invite_already_redeemed` |
| `llm_cost_usd(model, day, in, cached, out, think, batch)` | Chi phí theo bảng giá **có hiệu lực tại `day`** (cached, thinking tính như output, batch giảm theo `batch_multiplier`) | `no_price_for_model` → 500 + báo động |
| `add_spend(cost)` | Cộng vào sổ ngày (UTC); bật `paused` khi chạm trần | — |
| `claim_tasks(worker, limit, per_job_limit)` | Nhận task: `SKIP LOCKED`, chỉ job `running` và chưa yêu cầu huỷ, **giới hạn đồng thời theo job** bằng advisory lock | — |
| `reclaim_stale_tasks(stale)` | Task mất heartbeat → `pending`, hoặc `failed` nếu hết `max_attempts` | — |
| `purge_expired_documents()` | Xoá `doc_paragraphs`, `doc_sections`, đánh dấu tài liệu `deleted` | — |
| `pool_try_reserve(dep, in, out, priority, now, reserve, ttl)` | Đặt chỗ nguyên tử cho MỘT lời gọi vào MỘT deployment: token-bucket RPM/TPM, bộ đếm ngày theo múi giờ nhà cung cấp, đồng thời, cooldown, circuit, phần dành riêng cho ưu tiên cao; chọn khoá ít dùng gần đây nhất; trả `ok`, `lease_id`, hoặc `wait_s` và lý do | `too_large`/`no_credential` → không bao giờ vừa (Router bỏ ứng viên) |
| `pool_settle(lease, kind, in, out, latency, retry_after, scope, now)` | Ghi nhận kết quả: hoàn/bù token, EWMA sức khoẻ, cooldown 429 theo loại (phút/ngày/không rõ), thu hẹp-phục hồi `limit_scale`, mở/đóng circuit, cách ly khoá bị từ chối, ghi `llm_incidents` | — |
| `pool_reap_leases(now)` | Thu hồi lease quá hạn (worker chết) để không kẹt `inflight`; chạy mỗi phút | — |
| `pool_snapshot(dep, now)` | `headroom` 0..1 (phần hạn mức còn lại ít nhất trong mọi chiều), sức khoẻ, cooldown, circuit: dùng để chấm điểm và cho Admin | — |
| `pool_call_cost(dep, day, in, cached, out, think, batch)` | Tiền thật (chỉ khi deployment `metered`) và chi phí bóng (giá tham chiếu, kể cả deployment miễn phí) | `no_price_for_model` |
| `defer_task(task, until)` | Hoãn task đang chạy: về `pending`, đặt `run_after`, **hoàn lại** lần thử đã tính khi nhận | — |
| `publish_glossary_release(glossary, by, note)` | Chụp các mục `confirmed` thành bản phát hành bất biến, tăng số hiệu, ghi hash nội dung | `glossary_not_found` |
| trigger `style_core_version_guard` | Chặn sửa nội dung/xoá phiên bản đã duyệt, chặn mở lại; chỉ cho `approved → deprecated` | `approved_version_is_immutable`, `approved_version_cannot_be_reopened` |

---

## 11. API và sự kiện thời gian thực

Đặc tả đầy đủ ([[N_PATHS]] đường dẫn, kiểm tra bằng `openapi-spec-validator`): `api/openapi.yaml`. Nhóm `pool`, `style-cores`, `curation` chỉ dành cho admin và curator (vai trò `curator` được duyệt Lõi văn phong và glossary chuẩn nhưng không đụng tới pool, khoá hay tiền).

### 11.1 Quy ước

- Gốc `/api/v1`; JSON UTF-8; thời gian ISO-8601 UTC; ID là UUID; phân trang bằng `cursor` + `limit` (≤ 100).
- **Xác thực:** cookie phiên HttpOnly, `SameSite=Lax`, `Secure`; yêu cầu thay đổi dữ liệu kèm `X-CSRF-Token` (double-submit). Bearer token ở giai đoạn 2.
- **Lỗi:** RFC 9457 (`application/problem+json`) với trường `code` ổn định để client xử lý. Tài nguyên của người khác trả **404** (không 403) để chống dò ID.
- **Idempotency:** `POST /jobs` bắt buộc `Idempotency-Key`; trùng khoá trả lại job cũ, không trừ tín dụng lần hai.
- **Giới hạn tốc độ:** trả 429 kèm `Retry-After`; hạn mức ở §13.4.

<!-- ENDPOINT_TABLE -->

### 11.2 Mã lỗi

| `code` | HTTP | Khi nào |
|---|---|---|
| `unauthenticated` / `forbidden` / `not_found` | 401 / 403 / 404 | Chưa đăng nhập / không đủ quyền / không tồn tại hoặc của người khác |
| `validation_error` | 400, 422 | Dữ liệu vào sai |
| `unsupported_file_type`, `file_too_large`, `encrypted_pdf`, `extraction_failed` | 415, 413, 422, 422 | Khâu nhận/bóc tách |
| `document_too_large` | 422 | Vượt `max_words_per_document` hoặc `max_words_full_translation` |
| `rights_not_attested`, `consent_required` | 422, 403 | Chưa xác nhận quyền sử dụng / chưa đồng ý chuyển dữ liệu |
| `insufficient_credits` | 402 | Không đủ tín dụng |
| `active_job_limit` | 409 | Vượt số job chạy đồng thời của người dùng |
| `invite_invalid`, `invite_exhausted`, `invite_already_redeemed` | 404, 409, 409 | Mã mời |
| `job_not_cancelable`, `glossary_not_ready` | 409 | Sai trạng thái |
| `document_expired` | 410 | Tài liệu gốc đã bị xoá |
| `rate_limited` | 429 | Vượt giới hạn tốc độ |
| `level_disabled` | 422 | Mức không nằm trong `app_settings.enabled_levels` |
| `full_translation_daily_limit` | 422 | Vượt số job dịch đầy đủ mỗi ngày |
| `shared_processing_consent_required` | 403 | Job `standard` nhưng người dùng chưa đồng ý chế độ xử lý chung |
| `age_confirmation_required` | 403 | Chưa xác nhận từ 18 tuổi |
| `privacy_mode_unavailable` | 422 | Chế độ `private` nhưng pool không có deployment `no_training` đủ điều kiện ở cổng hiện tại |
| `pool_capacity_exceeded` | 503 | Ước tính cho thấy không thể phục vụ trong `max_pool_wait_hours`; kèm thời gian dự kiến |
| `approval_blocked` | 409 | Duyệt Lõi văn phong bị chặn (quyết định mở, mục AI chưa xác nhận, lint lỗi); kèm danh sách vấn đề |
| `version_immutable`, `draft_not_editable`, `style_core_not_approved` | 409 | Sửa phiên bản đã duyệt / sửa phiên bản không còn là bản nháp / chọn lõi chưa có phiên bản được duyệt |
| `spend_cap_reached` | 503 | Chạm trần chi tiêu ngày; thử lại sau |
| `internal_error` | 500 | Lỗi hệ thống |

### 11.3 Tạo job: chuỗi bước phải nguyên tử

1. Kiểm tra: tài liệu `ready` và thuộc người dùng; mức nằm trong `enabled_levels` (`level_disabled`) và, với `full_translation`, chưa vượt `full_translation_daily_limit`; số từ ≤ giới hạn; có `consent_cross_border` và `age_confirmed_at`; nếu `privacy_class = standard` thì có `consent_shared_processing_at`; nếu `private` thì pool phải có ít nhất một deployment `no_training` đủ điều kiện ở cổng hiện tại (`privacy_mode_unavailable`); `style_core_id` (nếu có) phải có phiên bản đã duyệt; số job đang chạy < giới hạn; `spend_daily.paused = false` hoặc job chỉ cần deployment miễn phí; ước tính hàng chờ ≤ `max_pool_wait_hours` (`pool_capacity_exceeded`).
2. Tính ước tính (`reference/estimator.py`) → `est_credits`, `est_cost_usd`, `max_cost_usd = 1.5 × est_cost_usd`.
3. **Một giao dịch:** chèn `jobs` (trạng thái `queued`, kèm ảnh chụp `model_profile` = phiên bản cấu hình pool và các tầng của profile, `prompt_versions`, `privacy_class`, `style_core_version_id`, `glossary_releases`), chèn `job_stages` cho mọi giai đoạn (giai đoạn không dùng ở mức này = `skipped`), chụp glossary vào `job_glossary_entries`, gọi `charge_credits(user, est_credits, job_id, 'charge:'||job_id)`, chèn sự kiện `job_queued`. Lỗi `insufficient_credits` thì giao dịch huỷ toàn bộ.
4. Sau commit: đặt job `running` và tạo task đầu tiên của giai đoạn đầu tiên chưa xong (tài liệu đã bóc tách thì bắt đầu từ `profile`).

### 11.4 SSE: `GET /jobs/{id}/events`

- Mỗi sự kiện: `id: <số tăng dần>`, `event: <type>`, `data: <JobEvent JSON>` (schema `job_event.schema.json`).
- Client kết nối lại bằng `Last-Event-ID`; server **phát lại** các sự kiện có `id` lớn hơn từ bảng `job_events`, rồi chuyển sang luồng trực tiếp (Redis pub/sub).
- Gửi dòng chú thích `: ping` mỗi 15 giây; đặt `X-Accel-Buffering: no`, `Cache-Control: no-cache`; đóng luồng sau `job_succeeded` / `job_failed` / `job_canceled`.
- Sự kiện **không chứa nội dung tài liệu**, chỉ trạng thái, bộ đếm, thông điệp tiếng Việt ngắn. Khi pool bắt job chờ hạn mức, phát `warning` với `data.code = pool_wait` và `data.until`; khi đoạn quay về dịch trực tiếp, `data.code = draft_fallback` (gộp theo segment, không phát từng đoạn).
- Tiến độ `progress.pct` tính theo trọng số giai đoạn (§12.5).
