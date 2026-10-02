
---

## 21. Lộ trình, ước lượng, rủi ro

### 21.1 Cột mốc (giả định 1-2 dev)

| Mốc | Thời gian | Nội dung | Tiêu chí hoàn thành |
|---|---|---|---|
| **M0 Prototype** | 2-3 tuần | CLI chạy trọn pipeline trên TXT/DOCX/PDF có chữ; P0-P8 + P9/P11 + kiểm tra tất định; chạy trên golden subset; bake-off model. **Thêm cho bản 0.2:** chạy `probe` trên từng nhà cung cấp/khoá thật và sửa `classify_http` theo phản hồi thật; đo hạn mức thật và `tokenizer_factor`; thí nghiệm bản dịch thô (§18.8); khởi tạo glossary và Lõi văn phong cho lĩnh vực đầu tiên (§19.9); hiệu chỉnh hằng số chi phí; chọn nhà cung cấp VPS và vùng bằng `mtr` | Đạt ngưỡng §2.2 trên ≥ 5 tài liệu; `estimate` lệch ≤ 35% so với chi phí thật; `pool_config` thật nạp và `probe` đạt; quyết định bật/tắt bản dịch thô theo quy tắc §18.8; lõi lĩnh vực đầu tiên thắng lõi trung tính trên golden set (§19.9) hoặc bị loại |
| **M1 Backend lõi** | 3-4 tuần | CSDL, đăng nhập, upload, orchestration theo §12, SSE, sổ tín dụng, mã mời, trần chi tiêu, OCR, parser sandbox. **Thêm:** LLM Pool (hai adapter, Router, nối hàm SQL, kho khoá mã hoá, Admin API), curation runs (P12, P13), API Lõi văn phong và glossary, VPS (compose, Caddy, sao lưu ra ngoài máy, giám sát ngoài máy) | Kịch bản AC-01..AC-26 (Phụ lục B) đạt trên môi trường staging; diễn tập khôi phục đạt AC-25 |
| **M2 Frontend MVP** | 3-4 tuần | Wizard 3 bước (kèm chế độ riêng tư và đồng ý), cổng glossary, trang đọc có trích dẫn, xuất MD/DOCX/PDF, quản lý glossary, Admin (kèm `/admin/pool`), giao diện duyệt Lõi văn phong và hàng đợi thuật ngữ (§19.6) | Người dùng thử hoàn thành F2 không cần hướng dẫn; curator duyệt được một lõi và một bản glossary từ đầu đến cuối |
| **M3 Làm cứng và beta** | 2 tuần | Giới hạn tốc độ, bảo mật máy chủ (§20.4), quan sát, trang pháp lý, diễn tập sự cố (kể cả hết hạn mức, khoá bị từ chối, nhà cung cấp sập, VPS chết); **cổng A** với 20-30 người | Checklist §14.5 (phần cổng A) đạt; KPI §2.2 trên job thật |
| **M4 Mở rộng** | 1-2 tuần | **Cổng B**: đăng ký mở, Turnstile, hàng chờ | Chi phí/người dùng ổn định 2 tuần |
| **P2** | Sau | Hỏi đáp có trích dẫn (File Search của Gemini hoặc RAG tự dựng), đối chiếu nguồn song song, URL/YouTube, công thức do người dùng tạo, thanh toán (cổng C) | |
| **P3** | Sau | Nhiều tài liệu một báo cáo, API công khai | |

Tổng MVP đến cổng A: khoảng **11-14 tuần** với 1-2 dev (bản 0.1 ước 8-10 tuần; phần thêm là pool, Lõi văn phong, curation và hạ tầng một VPS). M0 là cổng quyết định "đi tiếp hay chỉnh hướng" rẻ nhất: nếu không đạt chất lượng, chưa cần xây giao diện.

### 21.2 Chi phí vận hành tham khảo mỗi tháng

| Hạng mục | Ước tính |
|---|---|
| LLM | `số tài liệu × chi phí mỗi tài liệu` (xem §13.2): ví dụ 300 tài liệu 300 trang, mức mặc định ≈ 220 USD với giá khuyến mãi, ≈ 440 USD từ 01/01/2027 **nếu toàn bộ chạy trên tầng trả phí**; phần do nhóm miễn phí phục vụ thì không tốn tiền (§17.12) nhưng bị giới hạn bởi hạn mức và điều kiện riêng tư (§17.3) |
| Hạ tầng (một VPS 8 GB ở nước ngoài, kho đối tượng cho sao lưu, tên miền, email, giám sát; §20.9) | 30-100 USD |
| Giám sát, chống bot | 0-50 USD (nhiều gói miễn phí) |

### 21.3 Sổ rủi ro

| # | Rủi ro | Xác suất / tác động | Giảm thiểu |
|---|---|---|---|
| R1 | Chi phí vượt kiểm soát (công khai + chủ trả phí) | Cao / Cao | Ba lớp §1.2; trần chi tiêu; diễn tập chạm trần |
| R2 | Ảo giác hoặc sai thuật ngữ làm mất uy tín | Trung bình / Cao | Pipeline kiểm chứng, cổng glossary, hạng chất lượng, thông báo AI |
| R3 | Khiếu nại bản quyền | Trung bình / Cao | ToS, xác nhận quyền, `/takedown`, hạn lưu; **`full_translation` bật từ cổng A (D8)** nên thêm rào chắn: 120.000 từ, 3 job/ngày, cảnh báo "chỉ dùng cá nhân", không chia sẻ công khai |
| R4 | Tuân thủ PDPL và Luật AI | Trung bình / Cao | Luật sư; đồng ý riêng; banner AI; theo dõi nghị định hướng dẫn |
| R5 | Phụ thuộc nhà cung cấp LLM (giá đổi 01/01/2027, đổi model/API) | Cao / Trung bình | **Pool nhiều nhà cung cấp** (§17), bảng giá có ngày hiệu lực, hồi quy golden set khi đổi cấu hình pool |
| R6 | PDF scan/bố cục xấu | Cao / Trung bình | OCR dự phòng, điểm chất lượng, cảnh báo sớm ở bước ước tính |
| R7 | Job dài thất bại giữa chừng | Trung bình / Trung bình | Checkpoint, task idempotent, reclaim, hoàn tín dụng |
| R8 | Lạm dụng/tấn công (tệp độc hại, đăng ký rác) | Trung bình / Cao | Sandbox, Turnstile, giới hạn tốc độ, mã mời |
| R9 | Giấy phép thư viện (AGPL) | Trung bình / Cao | Kiểm toán CI; tránh PyMuPDF |
| R10 | Thời gian chờ lâu làm người dùng bỏ đi (nay còn thêm chờ hạn mức) | Trung bình / Trung bình | Thanh tiến độ, email báo xong, hiển thị `pool_wait_until` và ETA ở bước ước tính, (P2) hiển thị từng mục khi viết xong |
| R11 | Khoá hoặc tài khoản nhà cung cấp bị khoá vì vi phạm điều khoản (gom nhiều tài khoản free, endpoint thử nghiệm trong production) | Trung bình / Cao | Cờ `multi_account_risk`, `trial_only`, `allowed_gates`; chỉ dùng ở `dev` và `A`; luôn có tầng trả phí; không dùng tài khoản Google chính cho dự án free (§17.3) |
| R12 | Nội dung người dùng đi tới nhà cung cấp dùng dữ liệu để huấn luyện ngoài ý muốn | Thấp khi cưỡng chế / Cao | Chế độ riêng tư cưỡng chế trong code, đồng ý riêng, `llm_calls.data_policy` làm bằng chứng, truy vấn kiểm toán hằng ngày, cảnh báo khẩn (§17.4) |
| R13 | Hạn mức free tier đổi bất ngờ hoặc cạn nhanh hơn dự kiến | Cao / Trung bình | `limits` là cấu hình, học từ 429, tầng trả phí, cảnh báo tỷ lệ chuyển tầng (§17.10, §15) |
| R14 | Chuỗi cung ứng thư viện giữ khoá API bị tấn công (LiteLLM 03/2026) | Thấp / Cao | Lõi mỏng tự viết, ghim phiên bản và hash, chặn egress theo tên miền, khoá chủ ngoài bản sao lưu (§17.13, §20.4) |
| R15 | VPS là điểm lỗi duy nhất; mất dữ liệu hoặc ngừng dịch vụ lâu | Trung bình / Cao | Sao lưu mã hoá ngoài máy và diễn tập khôi phục hằng quý; WAL ngoài máy ở cổng C; giám sát ngoài máy (§20) |
| R16 | Nghĩa vụ PDPL của cá nhân vận hành máy chủ ngoài lãnh thổ chưa rõ (hồ sơ chuyển dữ liệu ra nước ngoài, miễn trừ) | Trung bình / Cao | Luật sư trước cổng B; cân nhắc đăng ký hộ kinh doanh/doanh nghiệp siêu nhỏ; chính sách quyền riêng tư đầy đủ (§14.4, §20.8) |
| R17 | Khâu bản dịch thô làm giảm chất lượng hoặc không tiết kiệm như kỳ vọng | Trung bình / Thấp | Mặc định tắt; quy tắc bật dựa trên bake-off; fallback từng đoạn; giám sát `edit_level` (§18) |
| R18 | Lõi văn phong do AI đề xuất làm lệch văn phong hoặc mang quan điểm không có căn cứ | Trung bình / Trung bình | Bằng chứng bắt buộc, AI nêu câu hỏi thay vì khẳng định, người duyệt, phiên bản bất biến, phải thắng lõi trung tính trên golden set, quy tắc bất biến nằm ngoài lõi (§19) |

---

## 22. Câu hỏi mở cần chủ sản phẩm quyết định

| # | Câu hỏi | Mặc định đề xuất |
|---|---|---|
| Q1 | Tên sản phẩm, tên miền, pháp nhân vận hành | Tên tạm ViSynth; cần pháp nhân trước cổng C |
| Q2 | Số tín dụng tặng và mã mời | 100 tín dụng đăng ký (cổng B); 300-1000 tín dụng mỗi mã mời |
| Q3 | **Đã chốt: bật đầy đủ các mức dịch.** Còn lại: bạn nhắc "3 mức" nhưng spec có **4 mức** (dịch đầy đủ, tổng hợp chi tiết, báo cáo chuyên sâu, tóm lược điều hành). Có muốn tắt mức nào không? | Bật cả bốn; muốn tắt thì bỏ mức khỏi `app_settings.enabled_levels` (không cần triển khai lại) |
| Q4 | **Đã chốt: VPS cá nhân ở nước ngoài.** Còn lại: nước/vùng cụ thể và nhà cung cấp VPS | Một vùng châu Á, ngoài EEA/Anh; đo `mtr` từ Hà Nội; vùng được Gemini hỗ trợ (§20.3) |
| Q5 | Cho phép chia sẻ báo cáo bằng link công khai? | Không ở MVP |
| Q6 | Thời hạn lưu | Tài liệu gốc 14 ngày; báo cáo 180 ngày không hoạt động; sao lưu ≤ 30 ngày và không chứa nội dung tài liệu (§20.6) |
| Q7 | Ngôn ngữ đích khác tiếng Việt | Không ở MVP |
| Q8 | Thanh toán | PayOS/VNPay/MoMo + Stripe ở cổng C |
| Q9 | Gemini API: dự án trả phí riêng, vùng, và **xác minh tham số lưu trữ tương tác** của Interactions API (tắt nếu có) | Dự án riêng; tắt lưu |
| Q10 | Ai xử lý `/takedown` và SLA | Admin, 72 giờ |
| Q11 | **Đã chốt về cơ chế: AI đề xuất, người duyệt** (§19). Còn lại: ai là người duyệt đầu tiên và lĩnh vực đầu tiên có những tài liệu mẫu và cặp dịch tham chiếu nào | Bạn là curator đầu tiên; tối thiểu 3 tài liệu mẫu và 5 cặp tham chiếu cho lĩnh vực đầu tiên |
| Q12 | Danh sách nhà cung cấp chuẩn OpenAI bạn đang có: tên, model, chính sách dữ liệu và hạn mức (đọc từ bảng điều khiển của họ) | Cần để điền `pool_config` thật; chưa rõ thì `data_policy = unknown` (bị coi như dùng dữ liệu, không vào chế độ `private`) |
| Q13 | Bạn thật sự sở hữu bao nhiêu dự án Gemini miễn phí; chấp nhận rủi ro tài khoản (§17.3 dòng 5) ở cổng A không; đã có dự án trả phí chưa | Chỉ dùng số dự án bạn tự dùng cho chính ứng dụng này; luôn có ít nhất một dự án trả phí làm chỗ dựa |
| Q14 | Khâu bản dịch thô: dùng endpoint miễn phí của NVIDIA ở closed beta (chấp nhận rủi ro điều khoản), tự chạy trọng số mở trên GPU thuê, hay bỏ? | Chỉ thí nghiệm ở `dev`; quyết định theo kết quả §18.8 |
| Q15 | Tư cách người vận hành: giữ cá nhân hay đăng ký hộ kinh doanh/doanh nghiệp siêu nhỏ (ảnh hưởng miễn trừ hồ sơ PDPL, §20.8) | Hỏi luật sư trước cổng B |
| Q16 | Chế độ riêng tư mặc định ở cổng A và B, và có giá tín dụng thấp hơn cho `standard` không | A: `standard` có đồng ý; B và C: `private`; chưa giảm giá |
| Q17 | **Mục 6 trong câu trả lời của bạn để trống.** Bạn muốn bổ sung điều gì? | (không có mặc định) |
| Q18 | Đồng ý với ngưỡng bật bản dịch thô (§18.8) và ngưỡng "lõi phải thắng lõi trung tính" (§19.9) không | Như spec |

---

## Phụ lục A. Thư viện prompt (toàn văn)

> Các prompt dưới đây được nhúng tự động từ `prompts/` khi chạy `python tools/build_spec.py`. **Sửa trong `prompts/`, không sửa ở đây.**

<!-- PROMPTS_APPENDIX -->

---

## Phụ lục B. Kịch bản nghiệm thu

| ID | Kịch bản | Kết quả mong đợi |
|---|---|---|
| AC-01 | Tải DOCX 10 trang, mức `deep_synthesis` | Báo cáo trong ≤ 3 phút; có tóm tắt, trích dẫn, ghi chú phạm vi, thông báo AI |
| AC-02 | Dán văn bản 1500 từ (dưới ngưỡng 2000 từ) | Không có cổng glossary (tự xác nhận gợi ý có độ tin cậy ≥ 0.7); báo cáo đúng định dạng. Dán 3000 từ thì **có** cổng |
| AC-03 | PDF scan 80 trang | Có cảnh báo `scanned_pdf_ocr_used`; kết quả đọc được; chi phí OCR ghi vào `llm_calls` |
| AC-04 | Cổng glossary: để hết 15 phút | Tự xác nhận gợi ý có độ tin cậy ≥ 0.7; `glossary_auto_confirmed = true` |
| AC-05 | Huỷ job trong giai đoạn `map` | Hoàn tín dụng 50%; không còn task chạy; trạng thái `canceled` |
| AC-06 | Giết worker giữa chừng | Task được thu hồi, job chạy tiếp, **không trừ tín dụng hai lần**, không trùng kết quả |
| AC-07 | Không đủ tín dụng | `402 insufficient_credits`; không tạo job; không trừ tiền |
| AC-08 | Chạm trần chi tiêu ngày | Job mới `503 spend_cap_reached`; job đang chạy hoàn tất |
| AC-09 | Xoá tài liệu gốc | `doc_paragraphs` và file gốc biến mất ≤ 24 giờ; báo cáo vẫn hiển thị trích dẫn bằng trích đoạn ngắn |
| AC-10 | Người dùng B truy cập job/báo cáo của A | 404 ở mọi endpoint |
| AC-11 | Tài liệu chứa `<script>` và `{{style_core}}` | Hiển thị vô hại; không bị mở rộng biến |
| AC-12 | `FakeLLMClient` trả trích đoạn bịa và số sai | Bằng chứng bịa bị loại; khối sai số bị bắt ở D3/P5 và được sửa hoặc đánh cờ; không bao giờ xuất hiện trong báo cáo hạng A |
| AC-13 | Gọi `POST /jobs` hai lần cùng `Idempotency-Key` | Chỉ một job, một lần trừ tín dụng |
| AC-14 | Đóng và mở lại trang khi job đang chạy | SSE nối lại bằng `Last-Event-ID`, tiến độ không mất |
| AC-15 | Xuất DOCX/PDF | Dấu tiếng Việt đúng; có cảnh báo AI; có danh sách nguồn trích dẫn |
| AC-16 | Job `private` khi pool chỉ còn nhóm miễn phí | Không có lời gọi nào tới nhóm miễn phí; job chờ hoặc bị từ chối (`privacy_mode_unavailable` hoặc `pool_capacity_exceeded`); truy vấn kiểm toán §17.4 trả 0 hàng |
| AC-17 | Job `standard`, hết hạn mức ngày của mọi nhóm miễn phí | Sang tầng trả phí nếu `paid_spill_enabled` và chưa chạm trần; nếu không thì job chờ với `pool_wait_until`; **không lỗi** |
| AC-18 | Nhà cung cấp trả 429 theo phút, rồi 3 lỗi 5xx liên tiếp | Cooldown đúng thời gian; task chuyển sang deployment khác không mất; circuit mở rồi đóng sau lời gọi thăm dò thành công |
| AC-19 | Khoá bị từ chối (401) | Khoá chuyển `quarantined`, cảnh báo Admin; các khoá khác tiếp tục phục vụ |
| AC-20 | Người dùng ở nước thuộc `restricted_free_tier_countries` | Không bao giờ được phục vụ bởi nhóm gắn `no_eea_uk_ch` |
| AC-21 | Mức dịch đầy đủ, `draft_mt_mode = auto`, endpoint dịch máy chết giữa chừng | Job vẫn hoàn tất, mọi pid có bản dịch; `reports.stats.draft` ghi số đoạn quay về dịch trực tiếp; hạng chất lượng không bị hạ vì khâu này; không segment nào chậm quá `segment_deadline_s` vì khâu này |
| AC-22 | Dịch máy trả sai số đoạn hoặc rác | Chia nhỏ rồi fallback; không đoạn nào mất hay trùng |
| AC-23 | Sửa hoặc xoá phiên bản Lõi văn phong đã duyệt; duyệt khi còn quyết định mở hoặc mục AI chưa xác nhận | Bị CSDL từ chối (sửa, xoá) hoặc API trả `approval_blocked` (duyệt) |
| AC-24 | P12 trả quy tắc có `source_ref` không tồn tại | Bằng chứng bị loại; mọi mục AI sinh ra có `origin = ai` và `reviewed = false` |
| AC-25 | Khôi phục VPS từ sao lưu (diễn tập) | Hoàn tất ≤ 4 giờ; sổ tín dụng khớp; job dở được hoàn tín dụng; nội dung tài liệu gốc không có trong sao lưu |
| AC-26 | Bỏ `full_translation` khỏi `enabled_levels`, rồi thêm lại | `POST /jobs` trả `level_disabled`, rồi hoạt động lại, đều không cần triển khai |

---

## Phụ lục C. Giả mã các thuật toán quan trọng

**Khớp trích đoạn (`reference/quote_verify.py`):** chuẩn hoá NFC, thống nhất dấu nháy/gạch, bỏ soft-hyphen, nối từ ngắt dòng, gộp khoảng trắng, hạ chữ hoa → tìm chuỗi con (exact) → tìm sau khi chỉ giữ chữ-số (loose) → **chỉ khi tài liệu có OCR**: khớp mờ (≥ 95, quote ≥ 25 ký tự, **bị chặn nếu quote có chữ số không tồn tại trong đoạn**).

**Số liệu (`reference/numbers_check.py`):** trích mọi số (bỏ số thứ tự danh sách và ID nội bộ) → chuẩn hoá (`1,000`=`1.000`=`1000`; `3,5`=`3.5`) → đổi chữ số viết bằng chữ tiếng Anh ở phía nguồn (`two`→`2`) → số có trong báo cáo mà không có trong nguồn là "chưa kiểm chứng".

**Lint thuật ngữ (`reference/glossary_lint.py`):** NFC → với mỗi mục: biến thể bị cấm; dạng `target (source)` ở lần dùng đầu nếu `keep_original`; thuật ngữ nguồn bị bỏ trần (sau khi loại các dạng hợp lệ).

**Hàng đợi (`claim_tasks`):** duyệt tối đa 500 task `pending` đến hạn của job `running` (khoá `SKIP LOCKED`); với mỗi job thử advisory lock, đếm task `running`, chỉ nhận nếu dưới giới hạn.

**Chốt chặn chi phí:** xem §12.7.

**Đặt chỗ pool (`pool_try_reserve`, `reference/llm_pool.py: MemoryState.try_reserve`):** khoá trạng thái nhóm rồi deployment; nạp đầy hai bucket theo thời gian trôi qua, đặt lại bộ đếm nếu sang ngày theo `reset_tz`; không có khoá `active` → `no_credential`; `open` hết cooldown → `half_open`; gom các lý do phải chờ (cooldown nhóm, cooldown deployment, đang thăm dò, rồi RPM, TPM, RPD, TPD, đồng thời của nhóm và của deployment) và lấy cái **lớn nhất, hoà thì cái xuất hiện trước**; `basis` lớn hơn dung lượng TPM/TPD → `too_large`; đủ chỗ thì trừ cả hai phạm vi, chọn khoá dùng lâu nhất, tạo lease.

**Chọn deployment (`Router.acquire`):** với mỗi tầng: lọc theo §17.4 → chấm điểm `headroom × (0,5 + 0,5 × thành công) × weight` → thử lần lượt (hai ứng viên đầu được xáo trộn có trọng số) → `Lease` đầu tiên thắng; không ai nhận: nếu thời gian chờ ngắn nhất ≤ `max_wait_s` thì trả `Wait`, ngược lại thử tầng sau; hết tầng thì `Wait` ngắn nhất hoặc `Impossible`.

**Giao thức chuyển dự phòng (`acquire_failover`):** thử với `exclude`; nếu `Impossible` hoặc phải chờ quá 30 giây thì thử lại không `exclude`, lấy lease nếu có, nếu không thì `Wait` không sớm hơn 5 giây.

**Bước bản thô (`draft_paragraphs`):** lọc đoạn dịch máy được → gói cửa sổ ≤ 900 token → với mỗi cửa sổ: quá hạn segment hoặc breaker đóng → `skipped`; gọi; `MtSkip` → `skipped`; `MtFailure` → ghi breaker, chia đôi nếu `splittable`, ngược lại `failed`; sai số đoạn → chia đôi tới đoạn đơn; kiểm tra từng đoạn → `ok` hoặc `failed`.

**Biên dịch lõi (`compile_style_core`):** lọc theo giai đoạn → dựng khối → nếu vượt trần thì lược `may`, `should`, ví dụ (giữ 2), ví dụ còn lại, ghi chú; `must` và chính sách thuật ngữ không bao giờ bị lược.

---

## Phụ lục D. Nguồn tham khảo (kiểm tra ngày 02/10/2026)

**Gemini API (nhà cung cấp LLM mặc định)**

- Giá: https://ai.google.dev/gemini-api/docs/pricing (Gemini 3.8 Flash: 0.75 / 3.75 USD mỗi 1M token vào/ra đến hết 31/12/2026, 1.50 / 7.50 từ 01/01/2027; tầng miễn phí dùng nội dung để cải thiện sản phẩm, tầng trả phí thì không; Batch giảm 50%)
- Model: https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash (vào tối đa 1.048.576 token, ra tối đa 65.536 token; thinking low/medium/high; structured outputs, caching, Batch, File Search)
- Structured outputs: https://ai.google.dev/gemini-api/docs/structured-output (tập con JSON Schema được hỗ trợ; luôn kiểm tra lại giá trị)
- Đọc tài liệu/PDF: https://ai.google.dev/gemini-api/docs/document-processing (PDF tối đa 50 MB hoặc 1000 trang; mỗi trang tương đương 258 token; với Gemini 3, chữ nhúng sẵn trong PDF không tính phí token)

**Gemini Notebook (trước là NotebookLM)**

- Đổi tên ngày 16/7/2026: https://blog.google/innovation-and-ai/products/gemini-notebook/notebooklm-gemini-notebook/
- Chia sẻ: https://geekflare.com/news/you-can-now-share-notebooklm-notes-with-anyone/ ; giới hạn miễn phí: https://elephas.app/blog/notebooklm-free-vs-plus

**Dịch máy**

- Cloud Translation (NMT 20 USD/1M ký tự, 500k ký tự đầu mỗi tháng miễn phí; Translation LLM 10+10 USD/1M ký tự; dịch tài liệu 0.08 USD/trang): https://chatscontrol.com/blog/google-cloud-translation-api-pricing-limits-2026
- TranslateGemma (4B/12B/27B, 55 ngôn ngữ; ngữ cảnh đầu vào 2K token): https://blog.google/innovation-and-ai/technology/developers-tools/translategemma/ ; https://huggingface.co/google/translategemma-12b-it

**Pháp lý Việt Nam**

- Luật Bảo vệ dữ liệu cá nhân 91/2025/QH15 (hiệu lực 01/01/2026): https://rouse.com/insights/news/2025/vietnam-s-new-personal-data-protection-law-what-businesses-need-to-know ; https://measuredcollective.com/vietnams-new-data-protection-law-what-you-need-to-know-before-january-2026/
- Luật Trí tuệ nhân tạo 134/2025/QH15 (hiệu lực 01/03/2026), Điều 11: https://english.luatvietnam.vn/law-no-134-2025-qh15-dated-december-10-2025-of-the-national-assembly-on-artificial-intelligence-422299-doc1.html ; https://blogs.duanemorris.com/vietnam/2026/03/03/vietnam-the-first-law-on-artificial-intelligence-what-you-must-know/

**Giấy phép phần mềm**

- PyMuPDF AGPL-3.0 hoặc giấy phép thương mại Artifex: https://pymupdf.io/pymupdf ; https://www.file2markdown.ai/blog/pypdf-vs-pymupdf

**LLM Pool: điều khoản và hạn mức của nhà cung cấp (kiểm tra ngày 02/10/2026)**

- Giới hạn tốc độ Gemini (theo dự án, RPD đặt lại lúc nửa đêm giờ Thái Bình Dương, không công bố bảng tĩnh): https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini API Additional Terms (hiệu lực 23/03/2026: Unpaid/Paid Services, EEA-Thụy Sĩ-Anh chỉ dùng Paid Services, từ 18 tuổi): https://ai.google.dev/gemini-api/terms
- Google APIs Terms of Service, mục 2.d "API Limitations" (không cố lách giới hạn): https://developers.google.com/terms
- Endpoint tương thích OpenAI của Gemini (structured output, `reasoning_effort`, còn beta): https://ai.google.dev/gemini-api/docs/openai
- NVIDIA API Trial Terms of Service (mục 1.2, 1.4, 2.6: chỉ thử nghiệm, không production, không dữ liệu cá nhân): https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf
- LiteLLM Router (cân bằng tải, cooldown, fallback, Redis): https://docs.litellm.ai/docs/routing
- Sự cố chuỗi cung ứng LiteLLM trên PyPI, 24/03/2026: https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-teampcp-supply-chain-campaign/ ; https://www.bitsight.com/blog/litellm-versions-1-82-7-1-82-8-supply-chain-compromise
- Con số "khoảng 40 yêu cầu/phút" của endpoint miễn phí NVIDIA là báo cáo của cộng đồng, **không phải số công bố**: https://learningaiworld.com/blog-nvidia-build-endpoints-2026-en/

**Model dịch máy NVIDIA Riva Translate (kiểm tra ngày 02/10/2026)**

- Model card (ngôn ngữ, chat template, few-shot, FLORES-101, giấy phép): https://huggingface.co/nvidia/Riva-Translate-4B-Instruct-v2
- Trang API (kiến trúc, ngữ cảnh 8K, "sẵn sàng cho mục đích thương mại"): https://docs.api.nvidia.com/nim/reference/nvidia-riva-translate-4b-instruct-v2
- Trang model trên danh mục (Free Endpoint, giới hạn tốc độ, điều khoản): https://build.nvidia.com/nvidia/riva-translate-4b-instruct-v2

**Pháp lý: hướng dẫn thi hành Luật Bảo vệ dữ liệu cá nhân**

- Nghị định 356/2025/NĐ-CP (31/12/2025): hồ sơ đánh giá tác động chuyển dữ liệu ra nước ngoài, miễn trừ cho hộ kinh doanh, doanh nghiệp nhỏ và khởi nghiệp: https://english.luatvietnam.vn/decree-no-356-2025-nd-cp-dated-december-31-2025-of-the-government-detailing-a-number-of-articles-and-measures-for-the-implementation-of-the-law-on-p-422896-doc1.html ; https://www.ey.com/en_vn/technical/tax/tax-and-law-updates/legal-alert-march-2026-decree-no-356-2025-nd-cp-providing-detailed-guidance-for-implementation-of-personal-data-protection-law ; https://www.vilaf.com.vn/blog/vietnams-new-personal-data-protection-decree-key-compliance-requirements-effective-immediately/ ; https://www.tilleke.com/insights/vietnam-issues-personal-data-protection-law/

**Giải pháp mã nguồn mở tham khảo (đã xem xét ở bước thảo luận)**

- Open Notebook (MIT; mật khẩu chung, không quản lý người dùng): https://github.com/lfnovo/open-notebook ; https://github.com/lfnovo/open-notebook/blob/main/docs/5-CONFIGURATION/security.md
- SurfSense (Apache-2.0; bản Docker do cộng đồng hỗ trợ): https://github.com/MODSetter/SurfSense

---

## Phụ lục E. Kết quả kiểm tra tại thời điểm phát hành

<!-- VALIDATION_SUMMARY -->
