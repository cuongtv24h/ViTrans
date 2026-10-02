# ViSynth — Đặc tả kỹ thuật & sản phẩm (v0.3)

> **Tên tạm:** ViSynth. Ứng dụng web "Chuyển ngữ & Tổng hợp" tài liệu sang tiếng Việt bằng LLM: người dùng tải PDF/DOCX/TXT... hoặc dán văn bản, hệ thống bóc tách rồi tạo **Báo cáo tổng hợp chuyên sâu** (hoặc bản dịch đầy đủ) bằng tiếng Việt, có thuật ngữ nhất quán, có trích dẫn nguồn và có bước kiểm chứng.
>
> **Phiên bản:** 0.3, ngày 02/10/2026 (bản 0.1 và 0.2 cùng ngày; thay đổi ở §0.1). **Trạng thái:** bản nháp để chốt với chủ sản phẩm. Mọi số liệu về giá, giới hạn, pháp lý của bên thứ ba được kiểm tra ngày 02/10/2026 và **PHẢI được kiểm tra lại trước khi build** (nguồn ở Phụ lục D).

<!-- TOC -->

---

## 0. Cách dùng bộ tài liệu này

Bộ tài liệu ("spec pack") gồm tài liệu chính này và các tệp máy đọc được. **Tài liệu chính là nguồn sự thật cho ý định và luồng xử lý; các tệp trong `schemas/`, `db/`, `api/`, `prompts/` là nguồn sự thật cho hợp đồng dữ liệu.** Nếu hai nơi mâu thuẫn, sửa tài liệu cho khớp tệp (đã có kiểm tra tự động).

<!-- FILE_TREE -->

| Bạn là... | Đọc theo thứ tự |
|---|---|
| Chủ sản phẩm | §0.1 → §1 → §2 → §3 → §4 → §13 → §14 → §17.1-17.4 → §17.10 → §17.14 → §18.1-18.3 → §19.1 → §21 → §22 |
| Dev backend / hạ tầng | §5 → §6 → §10 → §12 → §17 → §18 → §20 → `db/schema.sql` → `api/openapi.yaml` |
| Dev frontend | §3 → §4 → §11 → §19.6 → `api/openapi.yaml` → `examples/` |
| Kỹ sư prompt / AI | §6 → §7 → §8 → §17.9 → §18 → §19 → Phụ lục A → `schemas/` → `reference/` → §16 |

**Quy ước từ khoá:** PHẢI = bắt buộc; NÊN = mặc định hợp lý, chỉ bỏ khi có lý do; CÓ THỂ = tuỳ chọn.

**Kiểm tra tự động** (chạy ngay được, không cần khoá API):

```bash
pip install jsonschema pyyaml openapi-spec-validator pytest rapidfuzz pglast "psycopg[binary]"
pytest tests --ignore=tests/pg_smoke.py                           # logic tất định: trích đoạn, thuật ngữ, chi phí, LLM Pool, khai báo và mã hoá khoá, Lõi văn phong...
python tools/validate_spec.py                                      # schema, ví dụ, prompt, OpenAPI, enum chéo, cấu hình pool, lõi mẫu
python tools/validate_spec.py --pg-dsn postgresql://user@host/db  # thêm: nạp DDL + hành vi trên PostgreSQL thật + so khớp hàm SQL của pool với bản tham chiếu
python tools/simulate_pool.py                                      # mô phỏng pool (chờ, chuyển sang trả phí, 429) trước khi tốn tiền
python tools/build_spec.py                                         # dựng lại SPEC.md từ tools/spec_src + prompts
```

### 0.1 Thay đổi so với các bản trước (0.1 → 0.3)

**Từ 0.1 lên 0.2 (đối chiếu với các quyết định của bạn):**

| Bạn quyết định | Thay đổi trong spec | Ở đâu |
|---|---|---|
| Bật đầy đủ các mức dịch | Mọi mức (kể cả dịch đầy đủ) bật từ cổng A qua `app_settings.enabled_levels`. Rủi ro bản quyền không biến mất nên thêm rào chắn: xác nhận quyền mỗi lần tải, giới hạn số từ, giới hạn số job dịch đầy đủ mỗi ngày, không chia sẻ công khai, `/takedown` | D8, §13.4, §13.5, §14.4 |
| Spec chỉ cung cấp cơ chế, không tự đặt cách dịch; có thể xây các lõi văn phong chuyên biệt do AI đề xuất, người duyệt và sửa | **Đã bỏ hướng dẫn văn phong tiếng Việt cố định**. Thay bằng **Lõi văn phong** (Style Core): dữ liệu có phiên bản, AI đề xuất (P12), người duyệt, bất biến sau khi duyệt; quy tắc bất biến (trung thực, giữ nguyên số liệu/tên/ID) tách khỏi văn phong và không bị lõi ghi đè. Glossary chuẩn cũng theo vòng AI đề xuất (P1, P13), hàng đợi duyệt, bản phát hành | §19, D9, Phụ lục A.0 |
| VPS cá nhân, ở nước ngoài | Kiến trúc một máy (docker compose), không dịch vụ quản lý; sao lưu ngoài máy; khoá pool mã hoá tách khỏi bản sao lưu; hệ quả PDPL khi lưu dữ liệu người Việt ở ngoài lãnh thổ | §5.1, §20, §14.4, D10 |
| Dùng nhiều nhà cung cấp chuẩn OpenAI và nhiều khoá Gemini (tận dụng free tier) theo dạng pool | **LLM Pool**: mô hình khái niệm, thuật toán đặt chỗ nguyên tử (có bản SQL và bản tham chiếu, đã so khớp từng bước), chế độ riêng tư, cổng triển khai, cờ ToS, chuyển tầng, mô phỏng. Kèm các sự thật cần biết về free tier và rủi ro tài khoản | §17, D4, D11 |
| Dùng `riva-translate-4b-instruct-v2` của NVIDIA làm bản dịch thô cho LLM hiệu đính; nếu khâu này chết thì gọi LLM thay thế | **Khâu bản dịch thô** là bước phụ trong tác vụ `translate`, luôn có fallback từng đoạn sang dịch trực tiếp; kèm kết luận về chi phí, điều khoản NVIDIA (bản miễn phí chỉ để thử nghiệm) và quy tắc quyết định bật. **Bản 0.3 đã bỏ hẳn hướng này (§18).** | §18, D6 |
| Mục 6 | Câu trả lời trống; xem §22 (Q17) | §22 |

**Từ 0.2 lên 0.3:**

| Thay đổi | Nội dung | Ở đâu |
|---|---|---|
| **Bỏ khâu bản dịch thô của model dịch máy** | Mức `full_translation` chỉ dùng LLM dịch trực tiếp (P9). Bỏ prompt hiệu đính (số hiệu P11 bị loại, không dùng lại), profile `mt_draft`, schema `postedit_chunk`, cờ `draft_mt_mode`, các cột `draft_*` và `edit_level`, mã, test và cả công cụ ước tính so sánh lẫn bảng chi phí. Chỉ giữ một chương ngắn ghi quyết định và điều kiện xem xét lại | §18, D6 |
| **Khai báo nhà cung cấp và khoá cho pool** | Khai thông tin chung của một nhà cung cấp một lần rồi dán cả chuỗi khoá (cách nhau bằng dấu phẩy, chấm phẩy, xuống dòng hoặc khoảng trắng); hệ thống tự tạo nhóm hạn mức, khoá, model, deployment; có xem trước (dry-run) và `risk_ack` cho cờ rủi ro | §17.14, D12 |

---

## 1. Tóm tắt điều hành và quyết định

**Sản phẩm trong một câu:** người dùng đưa tài liệu (PDF/DOCX/TXT/SRT...) vào, nhận lại một bản tiếng Việt **đáng tin**, theo bốn mức cô đọng, từ *dịch đầy đủ* đến *tóm lược điều hành*; mức mặc định là **Báo cáo chuyên sâu** (Deep Synthesis Report): giữ đủ khái niệm cốt lõi, cơ chế then chốt và hệ thống luận điểm; cô đọng lời dẫn nhập, trao đổi phụ, đoạn lặp; kèm mục "Phạm vi & phần đã cô đọng" tự sinh.

**Vì sao không chỉ là "một prompt dài":** NotebookLM (nay là Gemini Notebook) làm tốt vì có ngữ cảnh dài, ràng buộc vào nguồn và prompt báo cáo được tinh chỉnh. Muốn có cùng chất lượng **trong một sản phẩm của riêng mình, nhiều người dùng, kiểm soát được thuật ngữ và chi phí**, cần thêm ba thứ mà một prompt đơn lẻ không bảo đảm: (1) **kê khai** mọi ý quan trọng trước khi viết để không sót; (2) **thuật ngữ** cố định theo glossary; (3) **kiểm chứng** để bắt số liệu sai và ý bịa.

### 1.1 Quyết định đã chốt và đề xuất

| # | Quyết định | Trạng thái | Lý do / hệ quả |
|---|---|---|---|
| D1 | Vấn đề cần giải quyết: *mỗi người tự tải tài liệu của mình theo cùng một "công thức"* (định dạng báo cáo + thuật ngữ Việt hoá chuẩn + giao diện tiếng Việt) | Đã chốt | Chia sẻ một notebook không đáp ứng được. Do đó **Công thức (Recipe)** và **Glossary chuẩn** là trung tâm sản phẩm (§4, §10) |
| D2 | Mở công khai, có thể thu phí về sau | Đã chốt | Phải có sẵn tài khoản, tín dụng, pháp lý, chống lạm dụng ngay từ đầu (§13, §14) |
| D3 | Chủ sản phẩm trả chi phí LLM; kiểm soát bằng mã mời, tín dụng, trần chi tiêu | Đã chốt | Đây là rủi ro tài chính lớn nhất (xem 1.2) |
| D4 | LLM là một **pool** nhiều nhà cung cấp và nhiều khoá (chuẩn OpenAI Chat Completions và Gemini), chọn theo profile, chế độ riêng tư, cổng triển khai và hạn mức; Gemini **trả phí** là chỗ dựa về chất lượng, riêng tư và độ sẵn sàng | Đề xuất (§17) | Free tier chỉ là phần trợ cấp: hạn mức nhỏ, có thể đổi bất cứ lúc nào, dùng nội dung để cải thiện sản phẩm của nhà cung cấp, và gom nhiều tài khoản để cộng hạn mức có rủi ro điều khoản. Job `private` không bao giờ chạm nhóm không cam kết `no_training` |
| D5 | Lõi xử lý: pipeline **kê khai khái niệm → viết → kiểm chứng** | Đề xuất | Không sót ý, có trích dẫn, bắt được số liệu sai (§6) |
| D6 | Model dịch máy (NMT) không làm lõi. Khâu bản dịch thô cho mức dịch đầy đủ **đã được cân nhắc và loại khỏi phạm vi**: khi LLM vẫn viết lại toàn bộ thì không rẻ hơn dịch trực tiếp (còn đắt hơn khoảng 14%), số lời gọi LLM không giảm, và endpoint miễn phí của NVIDIA chỉ được dùng để thử nghiệm | Đã chốt (§18) | Mức `full_translation` dùng LLM dịch trực tiếp (P9); lý do, danh mục đã xoá và điều kiện xem xét lại ghi ngắn ở §18. NMT dịch từng câu, không biết "giữ gì, nén gì", nên cũng không dùng cho các mức tổng hợp |
| D7 | Triển khai theo 3 cổng: A closed beta (mã mời) → B đăng ký mở (tín dụng nhỏ + trần) → C bán tín dụng. Cổng hiện hành (`dev`, `A`, `B`, `C`) quyết định nhóm hạn mức nào được dùng | Đề xuất | Học về chất lượng và chi phí trước khi mở rộng (§13.5); gắn cổng với `allowed_gates` để các nguồn rủi ro (khoá free gom nhiều tài khoản, endpoint thử nghiệm) tự bị loại khi mở công khai |
| D8 | **Bật cả bốn mức** (kể cả `full_translation`) ngay từ cổng A, kèm rào chắn riêng cho dịch đầy đủ | Đã chốt [bạn] | Rủi ro bản quyền của dịch toàn văn vẫn cao hơn tổng hợp, nên: xác nhận quyền ở mỗi lần tải, tối đa 120.000 từ/tài liệu và 3 job dịch đầy đủ/người dùng/ngày, không chia sẻ công khai, `/takedown` 72 giờ, hạn lưu ngắn (§13.4, §14.4). Mỗi mức là một cờ trong `app_settings.enabled_levels` nên tắt lại không cần triển khai |
| D9 | **Lõi văn phong**: spec chỉ cung cấp cơ chế; nội dung do AI đề xuất, người duyệt quyết định, lưu thành dữ liệu có phiên bản và bất biến sau khi duyệt | Đã chốt [bạn] (§19) | Cùng một pipeline phục vụ nhiều lĩnh vực mà không nhúng quan điểm văn phong của spec. Quy tắc trung thực luôn nằm trong prompt hệ thống, không nằm trong lõi |
| D10 | Hạ tầng: **một VPS cá nhân ở nước ngoài**, docker compose, sao lưu mã hoá ra nhà cung cấp khác | Đã chốt [bạn] (§20) | Rẻ và đơn giản, đổi lại có điểm lỗi duy nhất và nghĩa vụ PDPL về chuyển dữ liệu ra nước ngoài (§14.4) |
| D11 | Lõi pool **tự viết, mỏng** (Router chính sách + hàm SQL nguyên tử), chỉ hai loại adapter (`openai_compat`, `gemini_native`); trạng thái ở PostgreSQL | Đề xuất (§17.13) | Chính sách riêng tư/ToS/cổng và đặt chỗ nguyên tử là đặc thù của sản phẩm; gateway ngoài chỉ nên dùng làm tầng truyền tải nếu ghim phiên bản (sự cố chuỗi cung ứng LiteLLM 03/2026) |
| D12 | Chủ hệ thống khai báo nhà cung cấp và khoá bằng biểu mẫu/dán chuỗi; **chấp nhận rủi ro điều khoản** khi gom nhiều tài khoản miễn phí, có `risk_ack` giữ dấu vết; cổng công khai B/C tự tắt nhóm gắn cờ rủi ro (có công tắc chủ động của chủ hệ thống) | Đã chốt [bạn] (§17.14) | Giảm ma sát khi thêm khoá/nhà cung cấp; rủi ro nằm ở tài khoản của chủ hệ thống nên cần xác nhận từng nhóm và không bật mặc định ở cổng công khai |

### 1.2 Rủi ro nổi bật: "công khai" + "chủ trả phí" + "tận dụng free tier"

Hai quyết định D2 và D3 cộng lại nghĩa là bất kỳ ai cũng có thể làm tốn tiền của bạn. Spec xử lý bằng **ba lớp**: (1) cổng đăng ký (mã mời → xác minh email → Turnstile); (2) **tín dụng** tính theo "trang quy đổi", trừ trước khi chạy; (3) **trần chi tiêu toàn hệ thống** (circuit breaker, mặc định 20 USD/ngày) chặn mọi kịch bản xấu. Ngoài ra, **giá Gemini 3.8 Flash tăng gấp đôi từ 01/01/2027** (từ 0.75/3.75 lên 1.50/7.50 USD mỗi 1M token vào/ra), nên mọi mô hình tài chính PHẢI tính theo giá mới.

Việc dùng pool với free tier (D4) thêm ba rủi ro mà §17.3 trình bày bằng nguồn chính thức: **(a) riêng tư**: nội dung gửi vào tầng miễn phí của Gemini được Google dùng để cải thiện sản phẩm và người thật có thể đọc, nên chỉ job `standard` có đồng ý riêng mới được đi qua đó; **(b) điều khoản**: Google cấm cố lách giới hạn sử dụng, gom nhiều dự án/tài khoản miễn phí để cộng hạn mức có nguy cơ bị khoá, và bản NVIDIA miễn phí cấm dùng trong production; **(c) dung lượng**: một dự án free chỉ nuôi được vài tài liệu 300 trang mỗi ngày (§17.10), nên free tier là khoản trợ cấp cho closed beta chứ không phải nền móng của dịch vụ công khai. Vì vậy pool luôn có tầng trả phí làm chỗ dựa, có trần chi tiêu.

---

## 2. Mục tiêu, phạm vi, chỉ số thành công

### 2.1 Mục tiêu (MVP)

| ID | Mục tiêu |
|---|---|
| G1 | Báo cáo tiếng Việt chất lượng ngang hoặc hơn bản Studio của Gemini Notebook cho cùng tài liệu |
| G2 | Thuật ngữ nhất quán và dùng chung được qua **Glossary chuẩn** (AI đề xuất, người duyệt, có bản phát hành) + glossary cá nhân + **Lõi văn phong** theo lĩnh vực |
| G3 | Độ trung thực **kiểm chứng được**: mỗi luận điểm có trích dẫn; số liệu, ngày tháng, tên được kiểm tra bằng code |
| G4 | Trải nghiệm đơn giản bằng tiếng Việt: tải lên → (duyệt thuật ngữ) → chờ → đọc → xuất file |
| G5 | Chi phí kiểm soát được từng job, từng người dùng, toàn hệ thống |
| G6 | Riêng tư theo người dùng, tuân thủ pháp luật Việt Nam hiện hành (§14) |
| G7 | Chi phí LLM giảm nhờ tận dụng nhiều nhà cung cấp và hạn mức miễn phí **mà không** làm lộ dữ liệu cho nhà cung cấp dùng dữ liệu huấn luyện và không làm hỏng chất lượng (§17) |
| G8 | Thêm nhà cung cấp hoặc khoá mới chỉ bằng một khai báo rút gọn: xem trước kế hoạch, xác nhận rủi ro có dấu vết, khoá được mã hoá ngay và không bao giờ hiện đầy đủ (§17.14) |

**Ngoài phạm vi MVP:** hỏi đáp nhiều tài liệu, Audio/Video Overview, cộng tác thời gian thực, app mobile native, ngôn ngữ đích khác tiếng Việt (kiến trúc hỗ trợ, nhưng prompt và UI chỉ tối ưu cho `vi`), OCR chữ viết tay, chia sẻ báo cáo công khai bằng link.

### 2.2 Chỉ số nghiệm thu MVP

| Chỉ số | Định nghĩa | Ngưỡng |
|---|---|---|
| `coverage_core` | (số unit core `yes` + 0.5 × `partial`) / tổng unit core | ≥ 0.90 (hạng A cần ≥ 0.95) |
| `faithfulness_rate` | khối `supported` hoặc `partially_supported` chỉ có lỗi nhẹ / tổng khối, sau sửa | ≥ 0.97; số khối `fabricated` còn sót = 0 |
| Nhất quán thuật ngữ | vi phạm lint glossary / số lần xuất hiện thuật ngữ | ≥ 98% đúng |
| Số liệu | số trong báo cáo không tìm thấy trong nguồn, chưa giải quyết, trên tập "trap facts" | = 0 |
| Thời gian | p50 cho 100 trang mức `deep_synthesis`; p95 cho 400 trang | ≤ 6 phút; ≤ 20 phút |
| Chi phí | 300 trang, `deep_synthesis` (giá khuyến mãi / giá từ 1/2027) | ≤ 1.5 USD / ≤ 3 USD |
| Độ ổn định | tỷ lệ job thành công (không tính tài liệu bị từ chối ở khâu nhận) | ≥ 97% |
| Cảm nhận người dùng | chấm mù theo cặp so với báo cáo Gemini Notebook (5 tài liệu × 3 người chấm) | ≥ 60% cặp "ngang bằng hoặc tốt hơn"; điểm beta ≥ 4/5 |

---

## 3. Người dùng và luồng sử dụng

**Persona chính — Người học/nghiên cứu cá nhân:** có tài liệu nước ngoài dài (sách, bài giảng, transcript) và muốn nắm ý chính bằng tiếng Việt, không có thời gian đọc nguyên bản, quan tâm thuật ngữ đúng chuẩn cộng đồng của lĩnh vực. **Persona phụ — Admin (chủ sản phẩm):** tạo mã mời, glossary chuẩn, công thức chính thức, theo dõi chi phí và xử lý khiếu nại.

### 3.1 Luồng chính F2 — Tạo bản chuyển ngữ

1. **Nguồn.** Kéo-thả file hoặc dán văn bản; tick bắt buộc "Tôi có quyền sử dụng tài liệu này" (lưu `rights_attested_at`). Hiện tiến trình bóc tách. Kết quả: tên, số trang/số từ, ngôn ngữ phát hiện, điểm chất lượng bóc tách, cảnh báo (ví dụ "PDF scan, đã dùng OCR; có thể còn lỗi chính tả").
2. **Thiết lập.** Chọn **Công thức** (mặc định "Báo cáo chuyên sâu"; công thức có thể gắn một **Lõi văn phong** và glossary chuẩn của lĩnh vực), mức cô đọng (4 mức), glossary (cá nhân + chuẩn), **chế độ riêng tư** ("Tiết kiệm": có thể dùng nhóm miễn phí, nội dung có thể được nhà cung cấp dùng để cải thiện sản phẩm; "Riêng tư": chỉ nhà cung cấp cam kết không dùng dữ liệu), ghi chú tuỳ chỉnh (≤ 1000 ký tự), tuỳ chọn "Báo qua email khi xong".
3. **Xác nhận.** Hiện ước tính: tín dụng, khoảng thời gian (kể cả thời gian chờ hạn mức nếu pool đang bận), số dư sau khi trừ, cảnh báo. Nút "Bắt đầu" gọi `POST /jobs` kèm `Idempotency-Key`.
4. **Tiến độ.** Các giai đoạn bằng tiếng Việt: Đọc tài liệu → Nhận diện thuật ngữ → *Duyệt thuật ngữ* → Kê khai khái niệm (6/15) → Lập dàn ý → Viết (3/9) → Kiểm chứng → Hoàn thiện. Tải lại trang không mất tiến độ.
5. **Cổng duyệt thuật ngữ** (nếu tài liệu đủ dài): bảng sửa nhanh, đếm ngược 15 phút (hết giờ tự dùng gợi ý có độ tin cậy ≥ 0.7). Có "Lưu vào glossary của tôi".
6. **Kết quả.** Trang đọc (§3.3). Xuất MD/DOCX/PDF. Chấm sao + báo lỗi từng đoạn.

### 3.2 Các luồng khác

| Luồng | Tóm tắt |
|---|---|
| F1 Onboarding | Đăng nhập Google/email OTP → đọc và đồng ý Điều khoản; **xác nhận từ 18 tuổi** (`age_confirmed_at`); **đồng ý riêng** việc gửi nội dung tài liệu cho nhà cung cấp LLM ở nước ngoài (`consent_cross_border_at`); **đồng ý riêng** chế độ "Tiết kiệm" nếu muốn dùng (`consent_shared_processing_at`) → nhập mã mời (tuỳ chọn) → nhận tín dụng |
| F3 Glossary | Tạo/sửa thuật ngữ, nhập/xuất CSV, xem glossary chuẩn (chỉ đọc), "ghi đè cục bộ" bằng mục cá nhân cùng `source_term` |
| F4 Lịch sử & xoá | Danh sách tài liệu/báo cáo; xoá tài liệu gốc ngay; xoá báo cáo; xoá tài khoản (xoá hết trong 30 ngày) |
| F5 Huỷ job | Nút Huỷ; hoàn tín dụng theo §12.6 |
| F6 Admin | Mã mời, người dùng, chi phí/ngày, trần chi tiêu, công thức, khiếu nại bản quyền; **pool LLM** (nhóm hạn mức, khoá chỉ ghi, deployment, profile, kiểm định, sự cố, dự báo dung lượng) |
| F7 Curation (admin, curator) | Nhờ AI đề xuất **Lõi văn phong** (P12) và **glossary chuẩn** (P1 + P13) từ tài liệu mẫu và cặp dịch tham chiếu; duyệt hàng đợi, trả lời các câu hỏi AI nêu, chạy thử lõi trên đoạn mẫu, duyệt (bất biến), phát hành bản glossary |

### 3.3 Trang đọc báo cáo (yêu cầu giao diện)

- Cột trái: mục lục theo `sections`; cột giữa: nội dung; cột phải (mở khi bấm trích dẫn): trích đoạn nguồn nguyên văn, trang hoặc timecode, câu tóm ý tiếng Việt.
- Trích dẫn hiển thị dạng chỉ số nhỏ `[1]`, số theo thứ tự xuất hiện đầu tiên; **không bao giờ** hiển thị ID nội bộ (`U-0001`, `P000123`).
- Khối bị đánh cờ (`flagged`) có biểu tượng cảnh báo + giải thích; hạng chất lượng (A/B/C) hiển thị đầu trang; hạng C có banner "Chất lượng dưới chuẩn, hãy đối chiếu nguồn".
- **Thông báo AI luôn hiển thị**: "Nội dung do AI tổng hợp, có thể chứa sai sót. Hãy đối chiếu nguồn cho các quyết định quan trọng." (§14.4).
- Mục cuối: "Phạm vi & cách xử lý" (P8), bảng dữ kiện, phụ lục thuật ngữ.

### 3.4 Danh sách màn hình

| Route | Mục đích |
|---|---|
| `/` | Giới thiệu, báo cáo mẫu, đăng nhập |
| `/login`, `/onboarding` | Đăng nhập, điều khoản, đồng ý chuyển dữ liệu, mã mời |
| `/app` | Lịch sử, số dư tín dụng, nút tạo mới |
| `/app/new` | Wizard 3 bước (§3.1) |
| `/app/jobs/[id]` | Tiến độ + cổng duyệt thuật ngữ |
| `/app/reports/[id]` | Trang đọc báo cáo |
| `/app/glossaries`, `/app/glossaries/[id]` | Quản lý glossary |
| `/app/settings` | Tài khoản, ngôn ngữ giao diện, xoá dữ liệu |
| `/admin/*` | Mã mời, người dùng, usage, trần chi tiêu, công thức, khiếu nại |
| `/admin/pool` | Trạng thái sống, dự báo dung lượng, nhóm hạn mức, khoá, deployment, profile, kiểm định, sự cố, nhập/xuất cấu hình |
| `/admin/style-cores`, `/admin/glossary-review` | Soạn/duyệt Lõi văn phong (§19.6), hàng đợi duyệt thuật ngữ, bản phát hành glossary (admin và curator) |
| `/terms`, `/privacy`, `/takedown` | Điều khoản, quyền riêng tư, biểu mẫu báo vi phạm bản quyền |

---

## 4. Yêu cầu chức năng

Ưu tiên: **MVP** (bắt buộc cho cổng A), **P2** (trước cổng B/C), **P3** (sau).

| ID | Yêu cầu | Ưu tiên | Tiêu chí nghiệm thu |
|---|---|---|---|
| FR-01 | Đăng nhập Google hoặc email OTP; xác minh email | MVP | Không dùng được tính năng AI khi chưa đồng ý ToS và `consent_cross_border` |
| FR-02 | Mã mời và tín dụng | MVP | `redeem_invite` nguyên tử; mã hết lượt/hết hạn/không tồn tại trả đúng mã lỗi |
| FR-03 | Tải file: `pdf, docx, doc, txt, md, srt, vtt, epub`; ≤ 50 MB; kiểm tra magic bytes; bắt buộc xác nhận quyền | MVP | File sai định dạng/đổi đuôi bị từ chối (`unsupported_file_type`); `.doc` được chuyển qua LibreOffice |
| FR-04 | Dán văn bản (≤ 1.500.000 ký tự) | MVP | Giữ nguyên xuống dòng; tách đoạn đúng |
| FR-05 | Bóc tách + điểm chất lượng + OCR dự phòng | MVP | `extraction_quality` ∈ [0,1]; PDF scan tự dùng OCR và có cảnh báo |
| FR-06 | Hồ sơ tài liệu (P0): ngôn ngữ, loại, miền, chế độ quy gán | MVP | Lưu `documents.profile` hợp lệ theo `doc_profile.schema.json` |
| FR-07 | Chọn Công thức (gắn Lõi văn phong, glossary), mức cô đọng, chế độ riêng tư; ghi chú tuỳ chỉnh ≤ 1000 ký tự | MVP | Công thức chính thức do Admin tạo; người dùng chỉ chọn; job chụp lại phiên bản lõi và bản phát hành glossary đã dùng |
| FR-08 | Ước tính trước khi chạy (không trừ tín dụng) | MVP | Trả tín dụng, khoảng phút, cảnh báo; sai số chi phí thật so với ước tính trong ±35% trên golden set |
| FR-09 | Cổng duyệt thuật ngữ (glossary gate) | MVP | Job dừng ở `awaiting_glossary`; xác nhận hoặc hết 15 phút thì chạy tiếp; lưu được vào glossary cá nhân |
| FR-10 | Tiến độ thời gian thực (SSE), tải lại không mất; email khi xong | MVP | Kết nối lại với `Last-Event-ID` nhận đủ sự kiện bị lỡ |
| FR-11 | Trang đọc: mục lục, trích dẫn, cờ, hạng chất lượng, ghi chú phạm vi, thông báo AI | MVP | Mọi trích dẫn mở được trích đoạn nguồn (hoặc trích đoạn ngắn đã lưu nếu tài liệu gốc hết hạn) |
| FR-12 | Đối chiếu nguồn song song; chế độ song ngữ cho `full_translation` | P2 | Chỉ khi tài liệu gốc còn tồn tại |
| FR-13 | Xuất MD, DOCX, PDF | MVP | Tiếng Việt hiển thị đúng dấu; mọi file có cảnh báo AI ở đầu hoặc chân trang |
| FR-14 | Báo lỗi từng đoạn + chấm sao | MVP | Lưu vào `feedback`; Admin duyệt để đưa vào golden set |
| FR-15 | Quản lý glossary: CRUD, CSV nhập/xuất, glossary chuẩn chỉ đọc | MVP | Trùng `source_term` (không phân biệt hoa/thường) bị từ chối; CSV có BOM mở đúng trong Excel |
| FR-16 | Lịch sử và xoá dữ liệu (tài liệu, báo cáo, tài khoản) | MVP | Xoá tài liệu gốc xong, `doc_paragraphs` và file gốc biến mất ≤ 24 giờ |
| FR-17 | Huỷ job và hoàn tín dụng | MVP | Theo bảng §12.6; không hoàn quá số đã trừ |
| FR-18 | Admin: mã mời, người dùng, usage, trần chi tiêu, công thức, khiếu nại | MVP | Mọi hành động Admin ghi `audit_log` |
| FR-19 | Giới hạn tốc độ, giới hạn job đồng thời, Turnstile | MVP | Số liệu ở §13.4; vượt giới hạn trả 429 kèm `Retry-After` |
| FR-20 | Giao diện tiếng Việt (mặc định), tiếng Anh tuỳ chọn | MVP | Mọi chuỗi qua i18n; không có chuỗi cứng |
| FR-21 | Hỏi đáp có trích dẫn trên tài liệu | P2 | Dùng File Search của Gemini hoặc RAG tự dựng (§21) |
| FR-22 | Nhiều tài liệu trong một báo cáo | P3 | |
| FR-23 | Mua tín dụng (PayOS/VNPay/MoMo + Stripe) | P2 (cổng C) | Webhook idempotent, ghi `credit_ledger` lý do `purchase` |
| FR-24 | Người dùng tự tạo và chia sẻ công thức | P2 | Công thức chỉ chứa tham số, không chứa prompt hệ thống |
| FR-25 | API công khai + token | P3 | |
| FR-26 | Nhập từ URL/YouTube (transcript) | P2 | |
| FR-27 | Quản trị pool: nhóm hạn mức, khoá (chỉ ghi, chỉ hiện 4 ký tự cuối), deployment, profile, nhập/xuất `pool_config`, kiểm định, sự cố | MVP | Khoá không bao giờ có trong phản hồi API, log, hay bản xuất; `secret_ref` chỉ nhận `env:`/`file:`; mọi thay đổi ghi `audit_log` |
| FR-28 | Định tuyến theo profile với riêng tư, cổng, cờ ToS, năng lực, hạn mức; chuyển dự phòng; chờ thay vì lỗi | MVP | Test: không job `private` nào được phục vụ bởi nhóm không `no_training`; không bao giờ vượt hạn mức đã cấu hình khi cấu hình đúng; 429 kích hoạt cooldown rồi tự phục hồi |
| FR-29 | Chế độ riêng tư và đồng ý xử lý chung (`POST /me/consents`); xác nhận 18+; quy tắc EEA/Anh/Thụy Sĩ cho free tier | MVP | Job `standard` bị từ chối nếu thiếu `consent_shared_processing_at`; người dùng thuộc nước bị hạn chế không bao giờ rơi vào nhóm gắn `no_eea_uk_ch` |
| FR-30 | Ước tính hàng chờ và dự báo dung lượng pool trước khi nhận job | P2 | `Estimate.queue`; `/admin/pool/capacity`; từ chối `pool_capacity_exceeded` kèm thời gian dự kiến thay vì nhận job rồi để kẹt |
| FR-31 | Khai báo nhà cung cấp và khoá cho pool: dán chuỗi khoá (dấu phẩy/chấm phẩy/xuống dòng), nhãn tuỳ chọn `nhãn\|khoá`, dry-run xem trước, xác nhận rủi ro `risk_ack` | MVP | Dry-run trả kế hoạch nhóm/khoá/deployment **không chứa khoá** (chỉ 4 ký tự cuối); thiếu `risk_ack` cho cờ rủi ro trả `risk_ack_required`; khoá trùng hoặc giả bị chặn kèm lý do, khoá tốt vẫn được thêm |
| FR-32 | Lõi văn phong có phiên bản: AI đề xuất, người duyệt, trả lời quyết định mở, thử trên đoạn mẫu, duyệt bất biến | MVP | Phiên bản đã duyệt không sửa/xoá được (trigger CSDL); không duyệt được khi còn quyết định mở hoặc mục AI chưa xác nhận |
| FR-33 | Glossary chuẩn: đề xuất của AI/người dùng vào hàng đợi, duyệt, bản phát hành bất biến; công thức và job ghim theo bản | MVP | Bản phát hành chỉ chứa mục `confirmed`; job lưu `glossary_releases` đã dùng |
| FR-34 | Giới hạn số job dịch đầy đủ mỗi người mỗi ngày | MVP | Vượt `full_translation_daily_limit` trả 422 `full_translation_daily_limit` |
