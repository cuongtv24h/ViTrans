
---

## 17. LLM Pool: dùng nhiều nhà cung cấp và nhiều khoá

> Chương này trả lời câu hỏi "tôi có nhiều nhà cung cấp chuẩn OpenAI và nhiều khoá Gemini từ nhiều tài khoản, làm sao tận dụng một cách hợp lý". Phần lõi (đặt chỗ nguyên tử, định tuyến) có **mã tham chiếu và bản SQL đã so khớp từng bước** (`reference/llm_pool.py`, `db/schema.sql`, `tests/pg_smoke.py`); phần mô phỏng ở §17.12 chạy được ngay (`python tools/simulate_pool.py`). Các con số hạn mức trong ví dụ là **giả định minh hoạ**, không phải số đo của nhà cung cấp.

### 17.1 Mục tiêu và nguyên tắc

**Mục tiêu:** hạ chi phí và tăng độ sẵn sàng bằng cách dùng nhiều nhà cung cấp và hạn mức miễn phí, **mà không** (a) đưa dữ liệu người dùng cho nhà cung cấp dùng dữ liệu để huấn luyện ngoài ý muốn của họ, (b) vi phạm điều khoản đến mức mất khoá hay tài khoản, (c) hạ chất lượng bản dịch/báo cáo.

| # | Nguyên tắc | Hệ quả thiết kế |
|---|---|---|
| 1 | Pipeline chỉ biết **profile** (`fast`, `writer`, `verifier`, `ocr`, `curator`), không biết nhà cung cấp | Thêm/bớt khoá hay đổi model không đụng prompt và pipeline |
| 2 | Hạn mức là **cấu hình**, không phải hằng số | Nhà cung cấp có thể đổi bất cứ lúc nào; chủ hệ thống nhập từ bảng điều khiển, pool học thêm từ lỗi 429 |
| 3 | Giữ **đúng hạn mức phía client** thay vì "bắn rồi chờ 429" | Token-bucket có hệ số an toàn; 429 là ngoại lệ được xử lý chứ không phải đường chạy chính |
| 4 | Mặc định an toàn: **không rõ thì coi như nhà cung cấp dùng dữ liệu** | `data_policy = unknown` bị xử như `may_train` |
| 5 | Hết hạn mức thì **chờ hoặc chuyển tầng**, không bao giờ làm job lỗi vì hạn mức | `Wait(until)` + `defer_task`; tầng trả phí làm chỗ dựa, có trần chi tiêu |
| 6 | Mọi quyết định để lại **dấu vết** | `llm_calls` lưu deployment, tầng, `data_policy` tại thời điểm gọi, kết quả |

### 17.2 Mô hình khái niệm

| Khái niệm | Là gì | Ví dụ | Bảng |
|---|---|---|---|
| Provider (nhà cung cấp) | Điểm cuối API và giao thức: `openai_compat` (Chat Completions chuẩn OpenAI) hoặc `gemini_native` | Gemini API; một nhà cung cấp chuẩn OpenAI; NVIDIA API Catalog | `llm_providers` |
| Quota group (nhóm hạn mức) | **Đơn vị mà nhà cung cấp ĐẾM hạn mức**, cùng chính sách dữ liệu và cờ ToS. Gemini: một dự án Google Cloud. NVIDIA: một tài khoản | `gemini-free-a`, `gemini-paid` | `llm_quota_groups` |
| Credential (khoá) | Một khoá API thuộc một nhóm. **Nhiều khoá cùng nhóm dùng chung hạn mức**; chỉ có tác dụng dự phòng khi một khoá bị thu hồi | `gemini-free-a-k1` | `llm_credentials` |
| Model | Tên model và năng lực: ngữ cảnh, mức hỗ trợ JSON, thị giác, PDF, `tokenizer_factor`, điểm kiểm định | `gemini:gemini-3.8-flash` | `llm_models` |
| Deployment | Nhóm hạn mức × model: **đơn vị bộ định tuyến chọn**. Có `limits` (rpm, tpm, rpd, tpd, concurrency), `tpm_basis`, `price_mode`, `weight`, `tags` | `gemini-free-a/flash` | `llm_deployments` |
| Profile và tầng | Vai trò logic mà prompt yêu cầu, gồm `needs` (năng lực tối thiểu) và các **tầng** theo thứ tự: chọn deployment theo thẻ và tier nhóm, chiến lược chọn, thời gian chờ tối đa trước khi sang tầng sau | `writer`: tầng `free-strong` rồi `paid` | `llm_profiles`, `llm_profile_tiers` |
| Chế độ riêng tư | `standard` hoặc `private`, thuộc về job | | `jobs.privacy_class` |
| Lease | Chỗ đã đặt cho một lời gọi đang bay; hết hạn tự thu hồi | | `llm_leases` |

```
profile writer
  ├─ tầng 1 "free-strong"  (chiến lược headroom, chờ tối đa 120 giây)
  │     ├─ deployment gemini-free-a/flash ─▶ nhóm gemini-free-a (dự án Google A) ─▶ khoá k1 (k2 dự phòng: CHUNG hạn mức)
  │     └─ deployment gemini-free-b/flash ─▶ nhóm gemini-free-b (dự án Google B) ─▶ khoá k1
  └─ tầng 2 "paid"         (chiến lược ordered, chờ vô hạn)
        └─ deployment gemini-paid/flash   ─▶ nhóm gemini-paid (dự án có thanh toán) ─▶ khoá k1
```

> **Điểm dễ nhầm nhất:** thêm khoá thứ hai vào **cùng một dự án** Google không tăng hạn mức. Chỉ thêm **dự án** (tài khoản) mới thêm hạn mức, và đó chính là chỗ rủi ro điều khoản của §17.3.

### 17.3 Những sự thật cần biết về free tier (kiểm tra 02/10/2026)

| # | Điều cần biết | Nguồn chính thức | Hệ quả thiết kế |
|---|---|---|---|
| 1 | Hạn mức Gemini API áp **theo dự án, không theo khoá**; RPD đặt lại lúc **nửa đêm giờ Thái Bình Dương**; tài liệu không công bố bảng số tĩnh (xem trong AI Studio) và nói rõ "hạn mức nêu ra không được đảm bảo" | Gemini API: Rate limits | Nhóm hạn mức = dự án; `reset_tz = America/Los_Angeles`; `limits` là cấu hình chủ nhập, pool học thêm từ 429 (`limit_scale`) |
| 2 | Tầng miễn phí (Unpaid Services): Google dùng nội dung gửi lên và phản hồi để cải thiện sản phẩm; **người thật có thể đọc, chú thích**; "đừng gửi thông tin nhạy cảm, bí mật hay cá nhân" | Gemini API Additional Terms, mục Unpaid Services | `data_policy = may_train`; chỉ job `standard` có đồng ý riêng được đi qua |
| 3 | Chỉ được dùng **Paid Services** khi cung cấp API Client cho người dùng ở **EEA, Thụy Sĩ hoặc Anh** | Gemini API Additional Terms, mục Use Restrictions | Cờ `no_eea_uk_ch`; lưu `users.country_code`; danh sách `restricted_free_tier_countries` |
| 4 | Người dùng phải từ **18 tuổi**; API Client không được hướng tới hay có khả năng bị người dưới 18 truy cập | Gemini API Additional Terms, mục Age Requirements | `age_confirmed_at` ở onboarding; ToS của ViSynth ghi rõ 18+ |
| 5 | Google APIs ToS mục 2.d: "bạn đồng ý và sẽ **không cố lách** các giới hạn" đã công bố; muốn vượt giới hạn phải xin sự đồng ý của Google | Google APIs Terms of Service, mục 2.d | Gom nhiều dự án/tài khoản miễn phí để cộng hạn mức **có nguy cơ vi phạm** (đây là cách đọc của spec, không phải tư vấn pháp lý; Google không công bố cách thực thi). Hậu quả có thể là bị khoá dự án hoặc tài khoản, kể cả tài khoản cá nhân của bạn. Cờ `multi_account_risk` và `allowed_gates` để không dùng ở cổng công khai |
| 6 | NVIDIA API Catalog (nơi có endpoint miễn phí cho nhiều model): dịch vụ **chỉ để thử nghiệm**, "không dùng dịch vụ hoặc nội dung sinh ra trong production" (mục 1.2); nếu không mua gói thì "chỉ dùng để thử nghiệm và đánh giá nội bộ, không production" (mục 1.4); cấm gửi dữ liệu bí mật, nhạy cảm, **dữ liệu cá nhân** (mục 2.6a). Trang model ghi "có thể bị giới hạn tốc độ, lưu lượng của người khác có thể gây nghẽn". Con số khoảng 40 yêu cầu/phút do cộng đồng ghi nhận, **không phải số công bố** | NVIDIA API Trial Terms of Service; trang model trên danh mục | `tier = trial`; cờ `trial_only`, `no_personal_data`; `allowed_gates = [dev]` theo mặc định (chủ hệ thống tự nới sang `A` bằng `risk_ack`, §17.14) |
| 7 | Các nhà cung cấp chuẩn OpenAI khác: mỗi nơi một chính sách dữ liệu, hạn mức và điều khoản | (tự xác minh từng nơi) | `data_policy` mặc định `unknown`; khi nhập nhóm PHẢI ghi ngày đã xác minh vào `notes` |
| 8 | Endpoint tương thích OpenAI của Gemini hỗ trợ structured output và `reasoning_effort` nhưng tài liệu ghi còn **beta** | Gemini API: OpenAI compatibility | Có thể viết MỘT adapter cho mọi nhà cung cấp, đổi lại mất bộ nhớ đệm và đọc PDF gốc; spec giữ `gemini_native` cho profile `ocr` |
| 9 | **LiteLLM** (thư viện/gateway phổ biến để gộp nhiều nhà cung cấp) bị chèn mã độc lên PyPI ngày 24/03/2026 (bản 1.82.7 và 1.82.8, đánh cắp thông tin xác thực; tồn tại trên PyPI từ khoảng 40 phút đến 3 giờ tuỳ nguồn trước khi bị cách ly) | Báo cáo của Datadog Security Labs và các hãng bảo mật | Thành phần giữ mọi khoá API là mục tiêu tấn công; xem §17.13 và §14.1 |

**Kết luận cho người vận hành:** (1) free tier là **khoản trợ cấp**, không phải nền móng; (2) mọi thứ rẻ ở trên đi kèm điều kiện riêng tư hoặc điều khoản, nên pool phải **cưỡng chế bằng code** chứ không bằng thói quen; (3) việc gom nhiều dự án miễn phí là quyết định của bạn (D13: đã chấp nhận) và rủi ro nằm ở tài khoản Google của bạn; pool hỗ trợ, nhưng đòi bạn **khai báo xác nhận** (`risk_ack`, §17.14) cho từng nhóm có cờ rủi ro để có dấu vết, và mặc định tự tắt các nhóm đó ở cổng công khai (bạn có thể chủ động bật, §17.4).

### 17.4 Chính sách riêng tư, điều khoản và cổng triển khai

**Mọi quy tắc dưới đây được cưỡng chế ở MỘT chỗ** (`Router.eligible()`, có test) trước khi hỏi hạn mức, nên không có đường nào "lỡ" gửi nhầm:

| Điều kiện | Quy tắc |
|---|---|
| Job `private` | Chỉ nhóm có `data_policy = no_training` **và** không gắn `trial_only` hay `no_personal_data`. `unknown` bị coi như `may_train` |
| Job `standard` | Mọi nhóm đang bật (người dùng đã đồng ý riêng, §14.3) |
| Cổng triển khai | `app_settings.deploy_gate` phải nằm trong `allowed_gates` của nhóm. **Chốt chặn thứ hai:** ở cổng công khai (`B`, `C`) nhóm gắn `multi_account_risk` hoặc `trial_only` luôn bị loại, kể cả khi `allowed_gates` cấu hình sai |
| Vùng | Nhóm gắn `no_eea_uk_ch` bị loại khi người dùng thuộc `restricted_free_tier_countries` (hoặc chưa biết nước: coi là bị hạn chế) |
| Trần chi tiêu | Deployment `metered` bị loại khi `spend_daily.paused` |
| Năng lực | Mức JSON, ngữ cảnh đủ cho đầu vào + đầu ra (đã nhân `tokenizer_factor`), thị giác, PDF, điểm kiểm định ≥ `needs.min_quality` |
| Khoá | Còn ít nhất một khoá `active` |
| Loại trừ | Deployment vừa lỗi trong chuỗi thử lại này (`exclude`); nhóm cần tránh (`avoid_groups`, mềm) |

**Mặc định theo cổng:**

| Cổng | Chế độ mặc định của người dùng | Nhóm miễn phí | Nhóm `multi_account_risk` | Nhóm `trial_only` |
|---|---|---|---|---|
| `dev` (phát triển, bake-off) | `standard` | Dùng | Dùng | Dùng |
| `A` (closed beta, bạn bè) | `standard` kèm đồng ý riêng | Dùng | Dùng, chủ hệ thống chịu rủi ro tài khoản | Mặc định không (nới bằng `allowed_gates`, xem điều khoản NVIDIA ở §17.3) |
| `B` (đăng ký mở) | `private` cho người mới; `standard` là lựa chọn có đồng ý | Chỉ cho `standard` | **Không** | **Không** |
| `C` (bán tín dụng) | `private`; `standard` giá thấp hơn nếu muốn (§13.1) | Chỉ cho `standard` | **Không** | **Không** |

**Xác nhận rủi ro và quyền quyết định của chủ hệ thống (D13).** Nhóm gắn cờ `multi_account_risk` hoặc `trial_only` chỉ được dùng khi chủ hệ thống đã **xác nhận chấp nhận** đúng cờ đó (`risk_ack`, ghi người xác nhận và thời điểm). Ba lớp, từ chắc nhất:

1. **CSDL:** CHECK không cho `enabled = true` nếu nhóm có cờ mà thiếu xác nhận tương ứng (`risk_ack_required`).
2. **Router:** nhóm có cờ chưa xác nhận không bao giờ được chọn, dù cấu hình sai.
3. **Cổng công khai:** ở `B` và `C`, nhóm gắn các cờ này bị loại **kể cả đã xác nhận**, trừ khi chủ hệ thống chủ động đặt `app_settings.pool_allow_risk_at_public_gates = true`. Đây là công tắc có chủ đích: bật nghĩa là bạn nhận rủi ro khoá tài khoản ngay cả khi có người lạ dùng dịch vụ. Dù bật, nhóm `trial_only` và `no_personal_data` **vẫn không bao giờ** phục vụ job `private`.

**Mẫu thông báo đồng ý chế độ "Tiết kiệm" (cần luật sư rà soát, §14.4):** "Ở chế độ Tiết kiệm, nội dung tài liệu của bạn có thể được gửi tới nhà cung cấp AI áp dụng điều kiện miễn phí. Nhà cung cấp có thể dùng nội dung đó để cải thiện sản phẩm của họ và nhân viên của họ có thể xem. **Đừng dùng chế độ này cho tài liệu chứa thông tin cá nhân hoặc bí mật.** Chế độ Riêng tư chỉ dùng nhà cung cấp cam kết không dùng dữ liệu của bạn để huấn luyện."

**Truy vấn kiểm toán** (PHẢI trả về 0 hàng; chạy hằng ngày và là một chỉ số cảnh báo khẩn, §15):

```sql
SELECT j.id, c.deployment_id, c.data_policy
  FROM jobs j JOIN llm_calls c ON c.job_id = j.id
 WHERE j.privacy_class = 'private' AND c.data_policy IS DISTINCT FROM 'no_training';
```

### 17.5 Cấu hình

Cấu hình đầy đủ nằm trong CSDL (bảng `llm_*`) và được nhập/xuất dưới dạng một tài liệu `pool_config` (`schemas/pool_config.schema.json`, ví dụ chạy được ở `examples/pool_config.example.json`). Khoá **không bao giờ** nằm trong tài liệu này: chỉ có `secret_ref`. **Bạn không cần tự viết tài liệu này:** cách nhập thông tin hằng ngày là khai báo rút gọn ở §17.14 (khai thông tin chung một lần rồi dán cả chuỗi khoá cách nhau bằng dấu phẩy), hệ thống sinh ra phần cấu hình. Trích rút gọn của cấu hình đầy đủ:

```json
{
  "groups": [{
    "id": "gemini-free-a", "provider": "gemini", "tier": "free", "data_policy": "may_train",
    "reset_tz": "America/Los_Angeles", "tos_flags": ["no_eea_uk_ch"], "allowed_gates": ["dev", "A", "B", "C"],
    "safety_margin": 0.85, "day_margin": 0.95,
    "credentials": [{ "id": "gemini-free-a-k1", "label": "Khoá 1 của dự án A", "secret_ref": "env:GEMINI_KEY_FREE_A1" }]
  }],
  "deployments": [{
    "id": "gemini-free-a/flash", "group": "gemini-free-a", "model": "gemini:gemini-3.8-flash",
    "limits": { "rpm": 10, "tpm": 250000, "rpd": 250, "tpd": null, "concurrency": 3 },
    "tpm_basis": "input", "price_mode": "free", "weight": 1.0, "tags": ["free", "strong", "flash"]
  }],
  "profiles": [{
    "name": "writer",
    "needs": { "structured": "json_object", "min_ctx_in": 100000, "vision": false, "pdf": false, "min_quality": { "json": 0.95, "vi_write": 0.80 } },
    "tiers": [
      { "name": "free-strong", "select": { "tags": ["strong"], "group_tiers": ["free"] }, "strategy": "headroom", "max_wait_s": 120 },
      { "name": "paid", "select": { "tags": [], "group_tiers": ["paid"] }, "strategy": "ordered", "max_wait_s": null }
    ]
  }]
}
```

| Trường | Ý nghĩa |
|---|---|
| `limits.*` | Số do nhà cung cấp công bố trong bảng điều khiển; `null` = chưa biết hoặc không có. **Không có hằng số nào được ghi cứng trong code** |
| `limits` ở nhóm | Hạn mức **cả tài khoản** chung cho mọi model trong nhóm (NVIDIA, dịch vụ tổng hợp); `limits` ở deployment là theo model (Gemini). Cả hai được kiểm tra |
| `safety_margin` (0,85), `day_margin` (0,95) | Đặt dưới hạn mức thật để lệch đồng hồ, lời gọi đang bay và ước lượng token sai không gây 429 |
| `tpm_basis` | `input` nếu nhà cung cấp chỉ đếm token vào (Gemini), `total` nếu đếm cả ra |
| `weight`, `tags` | Trọng số khi chọn; thẻ để tầng chọn deployment (`strong`, `free`, `trial`...) |
| `tiers[].strategy` | `headroom` (chọn phần hạn mức còn lại nhiều nhất, có xáo trộn hai ứng viên tốt nhất để nhiều worker không dồn về một chỗ), `weighted` (ngẫu nhiên theo điểm), `ordered` (theo `weight` giảm dần: dùng cho tầng trả phí và mt) |
| `tiers[].max_wait_s` | Chờ tối đa ở tầng này trước khi thử tầng sau; `null` = chờ vô hạn (tầng cuối) |
| `needs.min_quality` | Điểm tối thiểu từ bài kiểm định (§17.9); deployment chưa kiểm định coi như 0 nên **không vào** profile đòi chất lượng |

**Quy trình đổi cấu hình:** `PUT /admin/pool/config?dry_run=true` (kiểm tra schema, ràng buộc chéo, trả bản so sánh) → xem → `dry_run=false` (áp dụng, tăng `app_settings.pool_version`, ghi `audit_log`). Khoá thêm riêng bằng `POST /admin/pool/groups/{id}/credentials` (chỉ ghi). Job chụp lại phiên bản pool lúc tạo (`jobs.model_profile`) để điều tra.

### 17.6 Thuật toán: chọn, đặt chỗ, gọi, ghi nhận

```
acquire(request) ──▶ lọc ứng viên (§17.4) ──▶ chấm điểm ──▶ try_reserve (NGUYÊN TỬ, SQL) ──▶ Lease
                                                                 │ không đủ chỗ
                                                                 ▼
                              Wait(until) nếu chờ ≤ max_wait_s của tầng, hoặc thử tầng sau
gọi nhà cung cấp bằng khoá của lease ──▶ settle(lease, kết quả) ──▶ hoàn/bù token, sức khoẻ, cooldown, circuit
```

**Phần đúng đắn (đặt chỗ nguyên tử):** hàm SQL `pool_try_reserve` kiểm tra và trừ **trong một giao dịch** mọi chiều hạn mức ở cả hai phạm vi (nhóm và deployment). Hai bản (`MemoryState` Python và SQL) có cùng ngữ nghĩa và được so khớp từng bước trên các kịch bản có hạt giống (§17.16).

| Chiều | Công thức | Ghi chú |
|---|---|---|
| RPM | Token-bucket dung lượng `max(1, rpm × m × s)`, nạp `dung lượng / 60` mỗi giây, mỗi lời gọi lấy 1 | `m` = `safety_margin`; `s` = `limit_scale` ∈ [0,3; 1]; sàn 1 để `rpm` nhỏ không bao giờ kẹt vĩnh viễn |
| TPM | Token-bucket dung lượng `tpm × m × s`, lấy `basis` token | `basis` = token vào nếu `tpm_basis = input`, ngược lại vào + ra dự kiến; khi ghi nhận, hoàn hoặc bù phần chênh theo usage thật |
| RPD, TPD | Bộ đếm tới `floor(giới hạn × day_margin)`; đặt lại khi sang ngày theo `reset_tz` của nhóm | Xử lý đúng múi giờ Thái Bình Dương của Gemini, không phải UTC |
| Đồng thời | `inflight < concurrency` | Lease quá hạn tự thu hồi (`pool_reap_leases`), worker chết không làm kẹt |
| Dành riêng | Tác vụ `low` chỉ được dùng phần `(1 − reserve)` đầu của mọi chiều; `reserve` = 0,2 | Đảm bảo glossary gate và tài liệu nhỏ không bị một job khổng lồ chiếm hết |
| Không bao giờ vừa | `basis > dung lượng TPM` hoặc `> TPD` → `too_large`; không có khoá `active` → `no_credential` | Router bỏ ứng viên thay vì chờ vô hạn |
| Hai phạm vi | Cùng bộ kiểm tra ở nhóm và ở deployment; thời gian chờ là giá trị lớn nhất | Đếm theo model (Gemini) hay cả tài khoản (NVIDIA...) đều mô hình hoá được |

**Phần chính sách (Router):**

1. **Lọc** ứng viên theo bảng §17.4. Ước lượng token = `ceil(est_in × tokenizer_factor)`; hệ số được hiệu chỉnh từ `usage` thực tế (EWMA) vì mỗi họ model đếm token khác nhau, và tiếng Việt tốn token hơn tiếng Anh.
2. **Chấm điểm:** `điểm = headroom × (0,5 + 0,5 × tỷ lệ thành công EWMA) × weight`, với `headroom` ∈ [0, 1] là phần hạn mức còn lại ít nhất trong mọi chiều (`pool_snapshot`). Chiến lược `headroom` lấy hai ứng viên điểm cao nhất và chọn ngẫu nhiên có trọng số giữa hai (power of two choices) để nhiều worker không đổ dồn.
3. **Đặt chỗ:** thử lần lượt theo thứ tự đã chấm; `try_reserve` quyết định ai thắng khi tranh chấp. Khoá được chọn là khoá `active` **dùng lâu nhất** (xoay vòng).
4. **Kết quả:** `Lease` (đi gọi), `Wait(until)` (hoãn task), hoặc `Impossible` (không có deployment nào đủ điều kiện: chia nhỏ đầu vào hoặc thất bại rõ ràng).
5. **Tầng:** nếu cả tầng chưa có chỗ mà thời gian chờ ngắn nhất ≤ `max_wait_s` thì trả `Wait` (ưu tiên chờ cái rẻ hơn); nếu lâu hơn thì thử tầng kế tiếp. Ví dụ: hết RPM chờ vài giây thì chờ, hết RPD phải chờ hàng giờ thì sang tầng trả phí.

```python
router = Router(load_pool_model(cfg), PgState(conn))          # PgState gọi các hàm SQL; MemoryState dùng cho test
out = router.acquire_failover(Request("writer", est_in, est_out, privacy=job.privacy_class, gate=gate,
                                      region_restricted=user_restricted, allow_metered=metered_allowed,   # §17.8
                                      avoid_groups=writer_groups, exclude=failed_here), now)
if isinstance(out, Lease):   # gọi nhà cung cấp rồi ghi nhận kết quả
    res = call(out.deployment, out.credential_id, ...)
    router.settle(out, classify_http(provider_kind, status, headers, body, finish_reason), now)
elif isinstance(out, Wait):  # pool chưa có chỗ: KHÔNG phải lỗi, KHÔNG tính lần thử
    defer_task(task_id, out.until)
else:                        # Impossible
    split_input_or_fail()
```

### 17.7 Phân loại lỗi và hành động

`classify_http()` ánh xạ phản hồi của adapter thành một `Outcome`; `pool_settle` áp hiệu ứng lên trạng thái.

| Tình huống | Nhận diện | Hiệu ứng trên pool | Hành động của pipeline |
|---|---|---|---|
| 429 theo phút | Gemini: `quotaId` chứa `PerMinute` và `retryDelay`; chuẩn OpenAI: "per minute", RPM, TPM trong thông điệp, header `Retry-After` | Cooldown `retry_after` (mặc định 30 giây); bucket về 0; `limit_scale × 0,85` (sàn 0,3) | Thử ngay deployment khác |
| 429 theo ngày | `quotaId` chứa `PerDay`; `insufficient_quota`; "per day", TPD, RPD | Cooldown tới nửa đêm `reset_tz` + 30 giây; `limit_scale × 0,85` | Sang tầng sau (trả phí) hoặc chờ tới lúc đặt lại |
| 429 không rõ | Còn lại | Cooldown mũ `min(600, 30 × 2^k)` giây | Như trên |
| 5xx, timeout, lỗi mạng | | Sức khoẻ giảm; **3 lỗi liên tiếp mở circuit** `min(600, 15 × 2^(lần mở−1))` giây; sau đó half-open cho đúng MỘT lời gọi thăm dò, thành công thì đóng | Thử ngay deployment khác |
| 401, 403 | | Khoá bị **cách ly** (`quarantined`) tới khi Admin gỡ; ghi sự cố | Deployment khác; cảnh báo Admin |
| 400 vượt ngữ cảnh | "context", "too many tokens"... | Không phạt | Chia nhỏ đầu vào |
| 400 khác | | Không phạt | Lỗi lập trình: không thử lại |
| Cắt cụt (`length`, `MAX_TOKENS`) | `finish_reason` | Không phạt | `OutputTruncated`: chia đôi đầu vào |
| Bị chặn an toàn | `content_filter`, `SAFETY` | Không phạt | Thử một lần ở nhà cung cấp khác (bộ lọc mỗi nơi một khác), sau đó `ContentBlocked` |
| JSON/schema sai | Sau khi kiểm tra bằng schema đầy đủ | Điểm hợp lệ × 0,95 | Thử lại một lần kèm lỗi, rồi leo deployment khác |
| Huỷ | | Trả chỗ | |

> **Cảnh báo trung thực:** các chuỗi nhận diện ở cột "Nhận diện" là **heuristic** dựa trên dạng lỗi phổ biến; mỗi nhà cung cấp phải được **kiểm chứng bằng thực nghiệm ở Giai đoạn 0** (`probe` ghi lại phản hồi thật của 429, hạn mức ngày, vượt ngữ cảnh) rồi chỉnh `classify_http`. Đừng tin các chuỗi này cho tới khi đã thấy phản hồi thật.

### 17.8 Chuyển dự phòng, chờ và suy giảm

**Giao thức chuyển dự phòng (`Router.acquire_failover`).** Sau một lỗi, tác vụ thử lại với `exclude = {deployment vừa lỗi}`. Nếu không còn ứng viên khác, hoặc ứng viên khác phải chờ lâu hơn 30 giây, bỏ danh sách loại trừ và thử lại (chính deployment vừa lỗi nếu pool cho phép ngay; nếu không thì chờ tới lúc pool cho phép, không sớm hơn 5 giây). Lỗi 5xx thường là tạm thời, còn nếu đó là sự cố thật thì circuit breaker (§17.7) đã chặn deployment ấy. Giao thức này tránh kẹt hàng giờ chỉ vì một lỗi đơn lẻ trên deployment duy nhất còn lại (mô phỏng §17.12 đã từng lộ lỗi này khi chưa có giao thức).

**Chờ không phải lỗi.** `Wait(until)` đưa task về `pending` với `run_after = until` bằng `defer_task` và hoàn lại lần thử đã tính. Worker không bị giữ trong lúc chờ. Tổng thời gian chờ của một job bị chặn bởi `max_pool_wait_hours`.

| Tình huống | Quyết định |
|---|---|
| Hết RPM/TPM của tầng miễn phí, chờ ngắn (≤ `max_wait_s`) | Chờ; không tốn tiền |
| Hết RPD của mọi deployment miễn phí | Nếu được phép dùng tầng trả phí (xem dưới bảng) thì sang tầng trả phí; nếu không: chờ tới lúc đặt lại hạn mức (nửa đêm giờ Thái Bình Dương: khoảng 14:00-15:00 giờ Việt Nam tuỳ mùa) |
| Job `private` và pool không có nhóm `no_training` còn chỗ | Chờ; **không bao giờ** sang nhóm không đủ điều kiện. Nếu ước tính cho thấy quá `max_pool_wait_hours`: từ chối ngay lúc tạo (`pool_capacity_exceeded`) |
| Chạm trần chi tiêu ngày | Deployment trả phí bị loại; job `standard` vẫn chạy trên phần miễn phí; job `private` chờ hoặc bị từ chối (§13.6) |
| Mọi deployment của profile circuit mở | `Wait` tới lúc thử lại; cảnh báo Admin |

Ứng dụng đặt `allow_metered` cho từng yêu cầu: `allow_metered = (không chạm trần chi tiêu ngày) và (job là `private` hoặc `paid_spill_enabled` đang bật)`. Job `private` luôn được dùng tầng trả phí vì đó là nơi duy nhất nó hợp lệ; `paid_spill_enabled` chỉ chặn việc job `standard` tràn sang trả phí khi tầng miễn phí hết.

**Kiểm soát nhận job (admission control).** Khi ước tính, hệ thống tính số lời gọi dự kiến (`estimate_calls`) so với dung lượng còn lại trong ngày của các deployment đủ điều kiện (`deployment_daily_capacity` trừ đã dùng); phần thiếu được quy ra thời gian chờ tới lần đặt lại gần nhất hoặc chuyển trả phí; kết quả trả trong `Estimate.queue` và cảnh báo `pool_delay`. Vượt `max_pool_wait_hours` thì từ chối nhận job với thời gian dự kiến, thay vì nhận rồi để kẹt.

**Đa dạng người viết và người kiểm.** Prompt kiểm chứng (P5, P6) truyền `avoid_groups` = nhóm đã viết mục đó: lỗi của một họ model ít bị chính họ model đó bỏ sót. Nếu không còn lựa chọn khác trong 60 giây, Router nới ràng buộc và đánh dấu `diversity_degraded` trong `llm_calls` (không phải lỗi, nhưng đáng theo dõi).

### 17.9 Chất lượng: không phải model nào cũng "cắm vào được"

**Bậc thang structured output** (`reference/structured_output.py`): pipeline luôn yêu cầu đầu ra theo schema đầy đủ, adapter chọn cách gửi theo năng lực của model được định tuyến.

| Mức hỗ trợ của model | Cách gửi | Ghi chú |
|---|---|---|
| `json_schema` | `response_format` loại `json_schema` với wire schema, **không strict** | Schema của spec có trường tuỳ chọn; chế độ strict của một số nhà cung cấp đòi mọi trường đều bắt buộc |
| `json_object` | `response_format: json_object` và nhúng schema rút gọn vào cuối prompt | Model đảm bảo JSON hợp lệ nhưng không đảm bảo đúng schema |
| `none` | Chỉ nhúng schema vào prompt, dặn "không văn xuôi, không rào code" | Mức thấp nhất; `extract_json()` làm sạch phản hồi |
| `gemini_native` | Wire schema trong cấu hình sinh | Gemini chỉ hỗ trợ một tập con JSON Schema (`reference/gemini_schema.py`) |

Ở mọi mức, `extract_json()` bỏ khối `<think>...</think>` (một số model suy luận in ra), rào code và lời dẫn, rồi mới kiểm tra bằng schema đầy đủ. Kết quả đúng cú pháp nhưng sai giá trị vẫn bị bắt bởi kiểm tra tất định (§6.5, §6.8).

**Bài kiểm định nhận deployment vào pool** (`POST /admin/pool/deployments/{id}/probe`, ghi `llm_probe_runs` và điểm vào `llm_models.quality`). Deployment chưa kiểm định không vào profile có `min_quality`.

| Điểm | Cách đo (khởi điểm, hiệu chỉnh ở Giai đoạn 0) | Dùng cho |
|---|---|---|
| `json` | 30 lời gọi trên fixture của P2, P5, P9 theo bậc thang mà model hỗ trợ; tỷ lệ hợp lệ sau tối đa một lần thử lại | `fast`, `writer`, `verifier`, `curator` |
| `vi_write` | Dịch 10 đoạn fixture có glossary: tỷ lệ qua lint thuật ngữ, số liệu, "có dấu"; điểm của một bộ chấm (model mạnh nhất, chấm mù so với bản tham chiếu của người) | `writer`, `verifier`, `curator` |
| `long_context` | Tìm "kim" ở 25%, 50%, 75% ngữ cảnh khai báo (tối đa 100k token) | Profile cần cửa sổ lớn |
| Phụ | Độ trễ p50, `tokenizer_factor` thật, phản hồi 429 thật (đối chiếu `classify_http`) | Cấu hình, §17.7 |

**Giám sát liên tục và tự hạ cấp.** Mỗi deployment có điểm hợp lệ EWMA (`ewma_valid`, giảm khi `invalid_output`) và sức khoẻ EWMA. Khi điểm hợp lệ dưới 0,8 hoặc tỷ lệ khối bị P5 đánh `fabricated` của deployment đó cao gấp đôi mức trung bình của profile, cảnh báo Admin và đề xuất hạ `weight` hoặc loại khỏi tầng. Không tự động xoá: quyết định cuối do người.

**Ngữ cảnh khác nhau giữa các deployment.** Profile khai báo `needs.min_ctx_in` và pipeline cắt segment sao cho vừa với deployment yếu nhất hợp lệ (`target_tokens ≤ 0,2 × min_ctx_in`, §6.3). Model có cửa sổ nhỏ hơn mức đó **không vào** profile.

### 17.10 Dung lượng, hàng chờ và đòn bẩy tiết kiệm request

Với free tier, thứ khan hiếm thường là **số request mỗi ngày** chứ không phải token. Bảng dưới tính số tài liệu mỗi ngày mà các dự án free có thể nuôi, theo hạn mức giả định (RPD 250):

<!-- POOL_CAPACITY_TABLE -->

Đọc bảng: một dự án free nuôi được vài tài liệu 300 trang mỗi ngày ở các mức tổng hợp; muốn phục vụ công khai phải có nhiều dự án (tức rủi ro điều khoản ở §17.3) hoặc, rẻ và an toàn hơn, **tầng trả phí**. Hai đòn bẩy: nới cửa sổ xử lý (`pool.window_scale`) và gộp kiểm chứng nhiều mục vào một lời gọi (`pool.verify_batch`), đổi lại mỗi lời gọi lớn hơn nên cần chắc ngữ cảnh và TPM cho phép.

**Kết luận thực dụng:** dùng free tier để (a) giảm chi phí của closed beta, (b) chạy bake-off và thử nghiệm, (c) làm bộ đệm giờ thấp điểm; **đừng** coi nó là năng lực phục vụ công khai. Luôn có tầng trả phí với trần chi tiêu (§13.6).

### 17.11 Kế toán chi phí và tín dụng

| | Tiền thật (`cost_usd`) | Chi phí bóng (`shadow_cost_usd`) |
|---|---|---|
| Định nghĩa | Chi phí của deployment `metered` theo `llm_prices` (0 với deployment miễn phí) | Cùng khối lượng token tính theo giá tham chiếu (`shadow_price_key`), kể cả khi deployment miễn phí |
| Dùng cho | `spend_daily`, trần chi tiêu ngày, báo cáo chi phí thật | Trần chi phí job (§12.7), hiệu chỉnh ước tính (§13.1), so sánh "nếu trả phí hết thì tốn bao nhiêu" |
| Hàm | `pool_call_cost()` trả cả hai | |

Tín dụng của người dùng **không phụ thuộc** deployment nào phục vụ: giá cố định theo ước tính (§13.1). Chênh lệch do hệ thống chịu và hiện trong dashboard (tiền thật, chi phí bóng, tỷ lệ phục vụ bởi tầng miễn phí).

### 17.12 Mô phỏng chính sách

`tools/simulate_pool.py` chạy cùng `Router` và `MemoryState` như production với một nhà cung cấp giả có bộ giới hạn thật riêng (có thể thấp hơn cấu hình để thử khả năng thích nghi) và 2% lỗi 5xx. Mỗi tài liệu 90.000 từ mức `deep_synthesis` sinh số lời gọi theo `estimate_calls`, token theo `estimate`.

<!-- POOL_SIM_TABLE -->

Đọc kết quả (so sánh các dòng, vì con số tuyệt đối phụ thuộc giả định):

- **A so với C:** thêm hai dự án miễn phí làm tầng đầu giảm mạnh tiền thật mà tài liệu vẫn xong trong vài chục phút; phần còn lại tràn sang tầng trả phí khi hạn mức ngày của free cạn.
- **B:** chỉ dùng free thì không tốn tiền nhưng phần vượt hạn mức ngày phải **chờ tới lúc đặt lại** (nửa đêm giờ Thái Bình Dương): đây là cái giá của "miễn phí".
- **D:** đủ dự án free thì cả đợt xong mà không tốn tiền; nút cổ chai khi đó là số worker và thời gian sinh, không phải nhà cung cấp.
- **E:** khi cấu hình `limits` cao hơn thực tế, vẫn có một số lỗi 429 ban đầu nhưng cooldown và `limit_scale` thu hẹp giúp pool tự thích nghi và hoàn tất mọi tài liệu. Cấu hình đúng (dòng A) thì không có 429 nào.

> Đây là mô phỏng thuật toán với hạn mức giả định. Nó trả lời câu "chính sách này hành xử thế nào", không trả lời câu "nhà cung cấp X cho bao nhiêu"; câu sau chỉ đo được bằng `probe` và bảng điều khiển của nhà cung cấp (§17.9).

### 17.13 Tự viết hay dùng gateway có sẵn

| Phương án | Ưu | Nhược | Kết luận |
|---|---|---|---|
| **Tự viết lõi mỏng** (Router + hàm SQL, hai adapter) | Chính sách riêng tư, ToS, cổng, lease gắn job và sổ chi phí là của riêng sản phẩm; ít phụ thuộc; đã có test so khớp SQL | Tự bảo trì hai adapter và tự học dạng lỗi từng nhà cung cấp | **Chọn** (D11) |
| **LiteLLM** (Router hoặc Proxy) | Có sẵn cân bằng tải (chọn theo trọng số, rpm/tpm, ít bận nhất, độ trễ, chi phí), cooldown, fallback, thử lại, Redis để chia sẻ trạng thái, nhiều nhà cung cấp | Không có khái niệm chế độ riêng tư, cờ ToS, cổng, lease gắn job; thêm một thành phần giữ **mọi** khoá; sự cố chuỗi cung ứng 24/03/2026 (§17.3) | Chỉ dùng được làm **tầng truyền tải** nếu ghim phiên bản và hash, chặn egress, cách ly thành phần; chính sách vẫn ở Router của ta |
| **Dịch vụ tổng hợp qua một khoá** (ví dụ OpenRouter) | Một khoá, nhiều model | Chính sách dữ liệu tuỳ nhà cung cấp phía sau từng model: khó chứng minh `no_training` | Có thể là MỘT nhóm trong pool; `data_policy` đặt theo kết quả xác minh từng model (cần kiểm tra) |

**Biện pháp chuỗi cung ứng cho thành phần giữ khoá (bắt buộc dù chọn phương án nào):** ghim phiên bản và hash (`pip install --require-hashes`); không `pip install` không ghim trong build; bản sao lưu và log không chứa khoá; egress của worker chỉ tới tên miền các nhà cung cấp đã khai báo; quét lỗ hổng định kỳ; khoá chủ tách khỏi bản sao lưu (§20.5).

### 17.14 Khai báo nhà cung cấp và khoá (phương pháp nhập thông tin)

Câu hỏi của bạn là "chỉ cần có chỗ khai báo, các khoá cách nhau bằng dấu phẩy". Thiết kế dưới đây đúng như vậy: **khai thông tin chung của một nhà cung cấp một lần, rồi dán cả chuỗi khoá**; hệ thống tự tạo nhóm hạn mức, khoá, model, deployment và gắn cờ điều khoản. Có mã tham chiếu và test (`reference/pool_declare.py`, `reference/pool_secrets.py`, `tests/test_pool_declare.py`, `tests/pg_smoke.py`).

**Ba cách nhập (cùng một cơ chế phía sau):**

| Cách | Dùng khi | Ở đâu |
|---|---|---|
| **A. Khai báo nhanh** (biểu mẫu từng bước) | Thêm hoặc sửa thường ngày | `/admin/pool`, nút "Khai báo nhanh" |
| **B. Tệp khai báo YAML** (mẫu có chú thích tiếng Việt) | Nhiều nhà cung cấp một lúc; muốn lưu bản khai báo (đã bỏ khoá) để lặp lại | `examples/pool_declaration.template.yaml`; `POST /admin/pool/declare` |
| **C. Nhập/xuất `pool_config` đầy đủ** | Sao lưu, chuyển máy, chỉnh sâu tầng và profile | §17.5, `PUT /admin/pool/config` |

**Phiếu thông tin cần có cho mỗi nhà cung cấp** (đây là toàn bộ "phương pháp nhập"; điền theo bảng, chỗ nào chưa biết thì dùng mặc định bảo thủ):

| Thông tin | Tìm ở đâu | Nếu chưa biết (mặc định bảo thủ) |
|---|---|---|
| Địa chỉ gốc API (`base_url`, bắt buộc https) | Mục "OpenAI compatibility" trong tài liệu nhà cung cấp | Bắt buộc, không có mặc định (preset `gemini` đã điền sẵn) |
| Tên model (`model_id`) | Trang danh sách model, hoặc lời gọi liệt kê model bằng chính khoá của bạn | Bắt buộc |
| Cửa sổ ngữ cảnh và số token ra tối đa | Trang của từng model | 32.768 và 4.096 (kèm cảnh báo) |
| Mức hỗ trợ JSON (`json_schema`, `json_object`, không) | Mục structured outputs trong tài liệu | `none` (an toàn: pool dùng bậc thang nhúng schema vào prompt, §17.9) |
| Hạn mức: yêu cầu/phút, token/phút, yêu cầu/ngày, đồng thời; **tính theo dự án hay cả tài khoản** | Bảng điều khiển của nhà cung cấp (mục rate limit) | 5 yêu cầu/phút, 1 lời gọi đồng thời, tính cả tài khoản |
| Chính sách dữ liệu (có dùng nội dung để huấn luyện không) | Điều khoản và trang quyền riêng tư của nhà cung cấp | `unknown` (bị coi như có dùng; không vào chế độ riêng tư) |
| Gói (`free`, `trial`, `paid`) | Gói tài khoản của bạn | Bắt buộc |
| Giờ đặt lại hạn mức ngày | Tài liệu nhà cung cấp | UTC (preset `gemini`: giờ Thái Bình Dương) |

**Cách nhập danh sách khoá.** Dán vào một ô, ngăn cách bằng dấu phẩy, chấm phẩy, xuống dòng hoặc khoảng trắng đều được; bỏ dấu nháy và ngoặc bao quanh; chấp nhận tiền tố `Bearer` và dòng kiểu `.env` (`TEN_BIEN=khoá`). Dạng `nhãn|khoá` để chỉ rõ **khoá nào thuộc cùng một tài khoản hay dự án**:

| Bạn dán | `group_mode` | Kết quả |
|---|---|---|
| `AIza…A, AIza…B, AIza…C` (ba tài khoản khác nhau, không nhãn) | `per_key` (mặc định) | Ba nhóm hạn mức riêng `gemini-free-1`, `-2`, `-3`: mỗi khoá cộng thêm một phần hạn mức |
| `acc1\|AIza…A, acc2\|AIza…B` | `per_key` | Hai nhóm `gemini-free-acc1`, `gemini-free-acc2` (nhãn dễ nhận diện hơn số thứ tự) |
| `duan\|AIza…A, duan\|AIza…B` (hai khoá cùng một dự án) | `per_key` | **Một** nhóm `gemini-free-duan` có hai khoá: chung hạn mức, chỉ để dự phòng |
| `AIza…A, AIza…B` (cùng một dự án, không nhãn) | `single_group` | Một nhóm có hai khoá |

Mỗi khoá được kiểm tra trước khi nhận; khoá bị bỏ qua được báo cùng lý do và **chỉ hiện 4 ký tự cuối**, các khoá tốt vẫn được thêm:

| Trạng thái | Nghĩa |
|---|---|
| `ok` | Nhận |
| `duplicate_in_request` | Dán hai lần trong cùng lần khai báo |
| `duplicate_existing` | Đã có trong hệ thống (so bằng dấu vân tay HMAC, không giải mã khoá nào); chỉ mục duy nhất của CSDL chặn tiếp ở tầng cuối |
| `too_short` | Ngắn hơn 16 ký tự |
| `invalid` | Có khoảng trắng, dấu tiếng Việt hoặc ký tự điều khiển; hoặc sai định dạng khoá của nhà cung cấp (preset `gemini`: bắt đầu bằng `AIza`) |
| `placeholder` | Trông như chuỗi giữ chỗ (`DÁN_KHOÁ_1`, `your_api_key`, `xxxx…`, `AIza...`, một ký tự chiếm gần hết chuỗi): ngăn việc áp dụng nhầm bản mẫu chưa điền |

**Hệ thống tự suy ra gì (bạn không phải khai):**

| Điều kiện | Hệ quả |
|---|---|
| Preset `gemini` | Địa chỉ, giao thức `gemini_native`, `reset_tz = America/Los_Angeles`, `tpm_basis = input`, hạn mức tính theo model, model mặc định `gemini-3.8-flash` (JSON schema, thị giác, PDF, ngữ cảnh 1M) |
| Gói `free` của Gemini | `data_policy = may_train`, cờ `no_eea_uk_ch` |
| Gói `paid` | Deployment `metered`, Gemini `data_policy = no_training`; nhà cung cấp tuỳ chỉnh vẫn `unknown` cho tới khi bạn khai |
| Từ **hai** nhóm `free` hoặc `trial` trở lên của cùng một nhà cung cấp | Cờ `multi_account_risk` và cổng cho phép mặc định `[dev, A]`; **cần `risk_ack`** |
| Gói `trial` | Cờ `trial_only`, `no_personal_data`, cổng `[dev]`; **cần `risk_ack: [trial_only]`** |
| Hạn mức tính cả tài khoản (`limits_scope = group`, mặc định của nhà cung cấp tuỳ chỉnh) | Hạn mức đặt ở **nhóm**, áp cho mọi model; `deployment` thì theo từng model |
| Thiếu `limits`, `ctx_in`, `max_out`, `structured` | Mặc định bảo thủ kèm cảnh báo |
| Giá tham chiếu cho chi phí bóng | `gemini-3.8-flash` nếu không khai |

**Chỗ khai báo chấp nhận rủi ro.** Trong khai báo có trường `risk_ack: [multi_account_risk]` (hoặc ô tick trong biểu mẫu). Thiếu xác nhận cho cờ mà hệ thống suy ra thì khai báo bị từ chối, kèm thông báo nêu đúng giá trị phải thêm. Xác nhận được ghi cùng người và thời điểm (§17.4 giải thích ba lớp cưỡng chế). Đây là thiết kế cho quyết định D13 của bạn: bạn **chấp nhận** rủi ro, spec chỉ giữ dấu vết để bạn (hoặc người khác dùng chung hệ thống) biết mình đã chấp nhận điều gì.

**Ví dụ chạy được** (`examples/pool_declaration.example.json`; dạng YAML ở mẫu). Kết quả biên dịch của đúng ví dụ đó (khoá chỉ hiện 4 ký tự cuối):

<!-- DECLARE_PREVIEW_TABLE -->

```yaml
declarations:
  - provider: { preset: gemini }
    tier: free
    keys: "acc1|AIza…, acc2|AIza…, acc3|AIza…"          # ba tài khoản Google
    limits: { rpm: 10, tpm: 250000, rpd: 250, concurrency: 3 }   # chép số thật từ AI Studio
    risk_ack: [multi_account_risk]
  - provider: { preset: custom, id: provider-c, base_url: "https://api.provider-c.example/v1" }
    tier: free
    keys: "khoá1, khoá2"
    limits: { rpm: 20, tpm: 60000, rpd: 1000, concurrency: 2 }
    models: [ { model_id: large-chat-v1, ctx_in: 131072, max_out: 8192, structured: json_object } ]
    risk_ack: [multi_account_risk]
```

**Thêm khoá về sau: nhân bản một nhóm có sẵn.** Chỉ cần dán khoá mới; tất cả còn lại (nhà cung cấp, gói, chính sách dữ liệu, cờ, xác nhận, hạn mức, model) sao chép từ nhóm nguồn:

```yaml
declarations:
  - clone_from_group: gemini-free-acc1
    keys: "AIza…, AIza…"
```

**Sau khi áp dụng:**

1. `validate_keys: true` thì hệ thống kiểm tra từng khoá bằng lời gọi **liệt kê model** (không tốn token; OpenAI-compatible: `GET {base_url}/models`, Gemini: liệt kê model). Khoá bị từ chối (401/403) chuyển ngay sang `quarantined` (§17.7).
2. Deployment mới chưa có điểm chất lượng nên chỉ phục vụ các profile không đòi chất lượng. Chạy `probe` (§17.9) để có điểm, hoặc **tự khai `quality`** trong khai báo nếu bạn tự đo và tự chịu trách nhiệm.
3. Nên bắt đầu ở cổng `dev` hoặc `A`, theo dõi `/admin/pool` vài ngày (§17.15) trước khi tăng `weight`.

**Bảo mật khi nhập khoá (bắt buộc):**

| Biện pháp | Chi tiết |
|---|---|
| Kênh và quyền | Chỉ HTTPS, chỉ vai trò `admin`, 2FA; giới hạn tốc độ riêng cho endpoint |
| Không ghi vết nội dung | `POST /admin/pool/declare` và các endpoint khoá nằm trong danh sách **không ghi nội dung yêu cầu** của log, Sentry và `audit_log` (chỉ ghi số lượng khoá và id nhóm) |
| Chỉ trả 4 ký tự cuối | Phản hồi, xem trước, lỗi và cảnh báo không bao giờ chứa khoá đầy đủ (có test dò từng khoá trong mọi phần trả cho client) |
| Mã hoá ngay | AES-256-GCM, AAD là id khoá, khoá con HKDF từ `POOL_MASTER_KEY` nằm ngoài CSDL và bản sao lưu; dấu vân tay HMAC để chống trùng (`reference/pool_secrets.py`) |
| Giao dịch nguyên tử | Lỗi giữa chừng thì không ghi gì (có test) |
| Không có khoá chủ | `dry_run` vẫn chạy được, ghi thật bị từ chối |
| Phía trình duyệt | Xoá ô nhập ngay sau khi áp dụng; không lưu nháp có khoá vào `localStorage`; không điền sẵn lại |
| Thói quen | Đừng dán khoá vào chat, email, issue hay kho mã; đừng lưu tệp YAML đã điền khoá; cấu hình xuất ra chỉ có `enc:<id>` |

### 17.15 Quản trị và vận hành pool

**Thêm một nhà cung cấp hay khoá mới (quy trình):** (1) Khai báo nhanh với `dry_run` (§17.14), đọc bản xem trước và các cảnh báo; (2) áp dụng; (3) sửa `limits` cho đúng số trong bảng điều khiển của nhà cung cấp (nếu đang dùng mức bảo thủ); (4) chạy `probe` và xem điểm; (5) thêm vào tầng của profile thích hợp qua `dry_run` rồi áp dụng (nếu cần ngoài các profile mặc định); (6) theo dõi `status` và `incidents` vài ngày trước khi tăng `weight`.

**Màn hình `/admin/pool`:** bảng deployment (headroom, circuit, cooldown, đã dùng hôm nay trên giới hạn, tỷ lệ thành công, độ trễ, số khoá hoạt động); dự báo dung lượng theo mức và chế độ riêng tư; sự cố gần đây; nút Khai báo nhanh, kiểm định, bật/tắt, đổi `weight`; nhập/xuất cấu hình. Không có chỗ nào hiển thị khoá đầy đủ.

**Cảnh báo và runbook:** §15. **Xoay khoá:** thêm khoá mới vào nhóm (khai báo `single_group` với đúng id nhóm hoặc nhãn trùng), kiểm tra, rồi xoá khoá cũ; không cần dừng hệ thống.

### 17.16 Đã kiểm thử gì, chưa kiểm thử gì

| Đã kiểm thử (tự động) | Nơi |
|---|---|
| Token-bucket, bộ đếm ngày theo múi giờ Thái Bình Dương, đồng thời, lease, dành riêng cho ưu tiên | `tests/test_pool.py` |
| Cooldown 429, circuit breaker (đóng, mở, half-open), cách ly khoá, xoay khoá | `tests/test_pool.py` |
| Riêng tư, cổng, cờ ToS, xác nhận rủi ro, công tắc cổng công khai, vùng hạn chế, trần chi tiêu, năng lực, ngữ cảnh, chất lượng | `tests/test_pool.py` |
| Chuyển tầng, chờ ngắn so với chuyển trả phí, đa dạng hoá, giao thức chuyển dự phòng, cân bằng tải | `tests/test_pool.py` |
| Hàm SQL trả đúng như bản tham chiếu **từng bước** trên các kịch bản có hạt giống (đủ mọi nhánh từ chối và ba trạng thái circuit) và không deadlock/vượt hạn mức dưới tranh chấp 48 lời gọi từ 8 kết nối | `tests/pg_smoke.py`, `tests/pool_scenarios.py` |
| Khai báo khoá: tách danh sách mọi kiểu ngăn cách, nhãn, `.env`, khoá trùng/ngắn/sai/giữ chỗ, đánh số nhóm, nhân bản nhóm, cờ suy ra, xác nhận rủi ro, mặc định bảo thủ, mẫu YAML chưa điền bị từ chối | `tests/test_pool_declare.py` |
| **Không có khoá thật trong bất kỳ thứ gì trả cho client** (cấu hình, xem trước, lỗi, cảnh báo, `repr`) | `tests/test_pool_declare.py` |
| Mã hoá: khứ hồi, bản mã không chứa khoá, gắn bản mã sang bản ghi khác bị từ chối, sửa bản mã bị từ chối, dấu vân tay có khoá | `tests/test_pool_declare.py` |
| Ghi nhận trong một giao dịch, huỷ toàn bộ khi lỗi, chỉ mục duy nhất chống trùng khoá, CHECK xác nhận rủi ro | `tests/pg_smoke.py` |
| Cấu hình mẫu hợp lệ, không chứa khoá, mọi tầng chọn được deployment, đột biến bị từ chối | `tools/validate_spec.py` |
| Mô phỏng: tiết kiệm tiền, thích nghi khi cấu hình sai, mọi tài liệu hoàn tất | `tests/test_pool.py` |

| **Chưa** kiểm thử (cần Giai đoạn 0 với khoá thật) | Vì sao |
|---|---|
| Dạng lỗi 429 và hạn mức ngày thật của từng nhà cung cấp | `classify_http` là heuristic (§17.7) |
| Lời gọi liệt kê model để kiểm tra khoá của từng nhà cung cấp | Cần khoá và mạng thật |
| Hạn mức thật của free tier hiện hành | Nhà cung cấp không công bố bảng tĩnh và có thể đổi |
| Chất lượng tiếng Việt và độ tuân thủ JSON của từng model | Chỉ đo được bằng `probe` và bake-off (§17.9, §16.4) |
| `tokenizer_factor` thật | Cần `usage` thật |
