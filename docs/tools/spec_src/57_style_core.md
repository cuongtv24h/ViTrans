
---

## 19. Lõi văn phong và Glossary chuẩn: AI đề xuất, người duyệt

> Chương này trả lời quyết định D9: "spec chỉ cung cấp cơ chế, không tự đặt cách dịch; có thể xây các lõi văn phong chuyên biệt với AI đề xuất ban đầu và người duyệt, sửa đổi". Mọi nội dung văn phong và thuật ngữ cụ thể là **dữ liệu** do người duyệt quyết định. Bản 0.1 của spec có một hướng dẫn văn phong tiếng Việt cố định; bản này đã bỏ nó (§0.1).

### 19.1 Nguyên tắc: cơ chế, không phải nội dung

| | Nội dung |
|---|---|
| **Spec cung cấp** | Hình dạng dữ liệu (`schemas/style_core.schema.json`); vòng đời và tính bất biến; cách biên dịch vào prompt; hai prompt đề xuất (P12 cho lõi, P13 cho glossary); hàng đợi duyệt; bản phát hành glossary; yêu cầu giao diện; test |
| **Spec KHÔNG cung cấp** | Bất kỳ quy tắc văn phong cụ thể nào: giọng, cách xưng hô, độ dài câu, cách xử lý thuật ngữ lạ, quy ước định dạng. Lõi mặc định (`prompts/00_style_core_neutral.json`) để trống mọi lựa chọn; `examples/style_core.example.json` chỉ minh hoạ hình dạng và **không phải khuyến nghị** |
| **Ai quyết định nội dung** | Người duyệt (vai trò `curator` hoặc `admin`), dựa trên đề xuất có bằng chứng của AI |
| **Điều không thương lượng** | Quy tắc bất biến nằm trong prompt hệ thống (§8.4): trung thực, giữ nguyên số liệu/tên/ID, quy gán, độ phủ, schema. Lõi **chỉ điều chỉnh cách diễn đạt** và không thể ghi đè các quy tắc này |

### 19.2 Mô hình dữ liệu của một Lõi văn phong

| Trường | Ý nghĩa | Mặc định của lõi trung tính |
|---|---|---|
| `name_vi`, `summary_vi`, `domain`, `locale` | Nhận diện và mục đích | "Mặc định trung tính", `general`, `vi` |
| `voice` | `register` (`unspecified`, `formal`, `neutral`, `conversational`, `academic`) và ghi chú | `unspecified` |
| `terminology_policy` | Cách trình bày thuật ngữ: `first_use` (`target_with_original`, `target_only`, `original_only`), `unknown_terms` (`flag_for_review`, `translate_with_original`, `keep_original`), `proper_names` (`keep_original`, `translate_known_forms`) | tất cả `unspecified`: để glossary quyết định từng mục |
| `formatting` | Dấu ngoặc kép, danh sách, cách viết số | tất cả `unspecified` |
| `rules[]` | Quy tắc ngắn (≤ 400 ký tự): `severity` (`must`/`should`/`may`), `applies_to` (giai đoạn; rỗng = tất cả), `origin` (`human`/`ai`), `reviewed`, `confidence`, lý do | rỗng |
| `exemplars[]` | Cặp nguồn → đích mẫu do người chọn hoặc xác nhận; làm ví dụ trong prompt (không còn dùng làm few-shot cho model dịch máy: khâu bản dịch thô đã bỏ, §18) | rỗng |
| `glossary_refs[]` | Glossary gắn kèm và số hiệu bản phát hành đã ghim (`null` = mới nhất) | rỗng |
| `limits.compiled_max_chars` | Trần độ dài khối biên dịch (mặc định 6.000) | 6.000 |

**Kế thừa:** lõi con có thể trỏ `parent_id`; quy tắc và ví dụ cùng `id` của lõi con ghi đè lõi cha, giá trị `unspecified` của con **không** xoá giá trị của cha (`resolve()`); tối đa 5 cấp; vòng lặp bị từ chối (`resolve_chain()`). Cho phép dựng "lõi chung cho tiếng Việt" rồi lõi riêng từng lĩnh vực mà không lặp lại.

**Bảng dữ liệu:** `style_cores` (định danh, lõi cha), `style_core_versions` (nội dung, trạng thái, `content_sha256`, quyết định mở, tham chiếu `curation_runs`), `style_core_reviews` (mọi lần sửa/trả lời/duyệt, kèm `before`/`after`).

### 19.3 Vòng đời và tính bất biến

```
   tạo mới / AI đề xuất / sao chép từ phiên bản cũ (copy-on-write)
                              │
                           [draft] ── sửa trực tiếp, trả lời quyết định mở, thử trên đoạn mẫu ──┐
                              │ submit                                                          │
                         [in_review] ── request_changes ────────────────────────────────────────┘
                          │         │
                 approve  ▼         ▼  reject
                   [approved]    [rejected]
                      │  BẤT BIẾN (trigger CSDL)
                      ▼ deprecate
                  [deprecated]
```

| Quy tắc | Cưỡng chế bởi |
|---|---|
| Phiên bản `approved` không sửa nội dung, không xoá, không mở lại; chỉ chuyển sang `deprecated` | Trigger `style_core_version_guard` (có test) |
| Không duyệt khi còn quyết định mở | CHECK `status <> 'approved' OR open_decisions = []` |
| Duyệt phải có người duyệt và thời điểm | CHECK trong DDL |
| Không duyệt khi còn quy tắc/ví dụ `origin = ai` mà `reviewed = false`, hoặc lint có lỗi | `approval_problems()` (API trả `approval_blocked` kèm danh sách) |
| Sửa lõi đã duyệt = tạo phiên bản mới (`bump` patch/minor/major) | API, `POST .../versions` |
| Job chụp lại phiên bản đã dùng (`jobs.style_core_version_id`), nên kết quả tái lập được dù lõi đổi sau đó | Lúc tạo job (§11.3) |

### 19.4 AI đề xuất: P12

P12 chạy trong một `curation_run` (profile `curator`, ưu tiên `low`, mặc định chế độ riêng tư `private` vì tài liệu mẫu có thể nhạy cảm; không thuộc job của người dùng và không trừ tín dụng người dùng).

| Đầu vào | Từ đâu | Ghi chú |
|---|---|---|
| `mode` | Người duyệt | `bootstrap` (bắt đầu từ lõi trung tính) hoặc `refine` (bắt đầu từ phiên bản hiện có) |
| `domain_brief` | Người duyệt, ≤ 3.000 ký tự | Lĩnh vực, người đọc, mục đích. Là **yêu cầu**, không phải chỉ thị cho model |
| `sample_excerpts` | Tối đa 10 tài liệu mẫu | Trích đầu, giữa, cuối; mỗi đoạn có ID `S01`... |
| `reference_pairs_json` | Người duyệt, tối đa 20 cặp | Bản dịch do người làm hoặc đã duyệt; ID `REF01`... Là bằng chứng mạnh nhất |
| `existing_core_json`, `glossary_digest_json`, `feedback_digest_json` | Hệ thống | Lõi hiện có (refine), thuật ngữ thường gặp, cờ và nhận xét của người dùng gom lại (ID `FB01`...) |

**Kỷ luật bằng chứng (đây là điểm khiến đề xuất đáng tin hơn một lời khuyên chung chung):**

1. Mỗi quy tắc và ví dụ AI thêm vào phải có ít nhất một mục `evidence` với `target_id`, `kind`, `source_ref` (ID đúng như đầu vào). Model **không tự viết trích dẫn**: code tra lại nội dung từ `source_ref` và loại mọi ID lạ.
2. **Không khẳng định điều không có căn cứ.** Nếu đầu vào không quyết định được một vấn đề, AI đưa nó vào `decisions_needed` với 2-5 phương án (nhãn và hệ quả), chỉ ghi `recommended` khi bằng chứng nghiêng về một phía; trường tương ứng giữ `unspecified`. **AI nêu câu hỏi, người chọn.**
3. Ví dụ (`exemplars`) chỉ được **chép nguyên văn** từ cặp tham chiếu; không có cặp thì để trống.
4. Tối đa 12 quy tắc; `must` chỉ cho điều bản tóm tắt hoặc cặp tham chiếu cho thấy là bất khả thương lượng.
5. **Code ép** `origin = ai` và `reviewed = false` cho mọi mục AI tạo ra, chạy `lint`, và tạo một phiên bản **nháp** (`origin = ai_proposal`, `open_decisions` = `decisions_needed`). AI không bao giờ tự duyệt được cái gì.
6. `risks_vi` nêu giới hạn của bằng chứng (ít mẫu, một diễn giả, không có cặp tham chiếu) và `confidence` hiển thị cạnh đề xuất.

Ví dụ đầu ra: `examples/style_core_proposal.example.json`.

### 19.5 Glossary chuẩn: đề xuất, duyệt, phát hành

```
tài liệu mẫu (≤ 10) ── P1 trên từng tài liệu ──▶ ứng viên theo tài liệu
      ── merge_candidates (tất định: gộp không phân biệt hoa/thường, đếm tài liệu, phát hiện xung đột, cấp ctx_id) ──▶ danh sách gộp
      ── P13 hài hoà (chọn MỘT cách dịch, loại biến thể, nêu câu hỏi khi chưa rõ) ──▶ glossary_proposals
      ── attach_evidence (code ghép đoạn trích từ ctx_ids) ──▶ glossary_entries: status = suggested, proposed_by = ai
      ── hàng đợi duyệt: approve | edit_approve | reject ──▶ confirmed | rejected (kèm reviewed_by, reviewed_at)
      ── publish_glossary_release ──▶ glossary_releases v1, v2... (bất biến) ──▶ công thức và job ghim theo số hiệu
```

| Nguồn đề xuất | Đường đi |
|---|---|
| Khởi tạo từ tài liệu mẫu (curation) | Như sơ đồ trên |
| P1 trong job của người dùng (`new_terms`, mục người dùng sửa) | Người dùng bấm "đề xuất lên glossary chuẩn" → vào hàng đợi với `proposed_by = user` |
| Curator nhập tay hoặc CSV | `proposed_by = curator` hoặc `import`, vẫn qua `suggested`/`confirmed` |

Quy tắc: (1) chỉ mục `confirmed` vào bản phát hành; (2) mục `rejected` được **giữ lại** để P1/P13 không đề xuất lại; (3) job dùng **bản phát hành đã ghim**, không dùng bản đang soạn (§6.4), nên sửa glossary không làm thay đổi kết quả job cũ; (4) AI đánh dấu `needs_human` cho xung đột và độ tin cậy dưới 0,6, và hàng đợi xếp các mục này lên đầu; (5) bằng chứng là đoạn trích thật do code ghép, không phải lời của model.

### 19.6 Yêu cầu giao diện duyệt (HITL)

**Trình soạn Lõi văn phong (`/admin/style-cores/[id]`):**

1. Danh sách quy tắc với nhãn **nguồn gốc** (AI hoặc người), mức, độ tin cậy, đã xem hay chưa; xem xong bấm xác nhận (`reviewed = true`), sửa trực tiếp, hoặc xoá.
2. Panel **bằng chứng**: bấm một quy tắc để thấy đoạn mẫu, cặp tham chiếu hay phản hồi đã dẫn (tra từ `source_ref`).
3. Panel **quyết định cần trả lời**: câu hỏi, các phương án và hệ quả, phương án khuyến nghị (nếu có) và lý do; chọn thì áp vào nội dung và đóng quyết định (`answer-decision`).
4. **Chạy thử** (`test-drive`): chọn tối đa 20 đoạn, xem song song kết quả của phiên bản hiện hành và bản nháp.
5. Cảnh báo lint và độ dài biên dịch; so sánh với phiên bản trước (diff).
6. Nút **Duyệt** bị khoá cho tới khi `approval_problems()` rỗng và hiển thị từng lý do chặn.
7. Lịch sử đầy đủ (`style_core_reviews`).

**Hàng đợi duyệt thuật ngữ (`/admin/glossary-review`):** bảng mục `suggested` với `needs_human` trước; cột thuật ngữ gốc, đề xuất, biến thể bị loại, độ tin cậy, câu hỏi của AI, **đoạn trích bằng chứng**, nguồn (AI hay người dùng); duyệt nhanh bằng bàn phím, sửa rồi duyệt, loại; lọc theo glossary; nút **Phát hành bản mới** kèm tóm tắt thay đổi so với bản trước (thêm, sửa, xoá).

Không yêu cầu cộng tác thời gian thực: hai curator sửa cùng một nháp thì người sau thấy cảnh báo xung đột (so sánh `content_sha256`).

### 19.7 Biên dịch vào prompt

`compile_style_core(content, stage)` tạo khối chèn vào biến `style_core` của prompt cho **đúng giai đoạn**:

1. Chỉ lấy quy tắc và ví dụ có `applies_to` rỗng hoặc chứa giai đoạn đó; bỏ giá trị `unspecified`.
2. Thứ tự: giọng, chính sách thuật ngữ, định dạng, quy tắc `MUST`, `SHOULD`, `MAY`, ví dụ.
3. Nếu vượt `compiled_max_chars`, **lược theo thứ tự cố định**: quy tắc `may` → quy tắc `should` → ví dụ (giữ tối đa 2) → ví dụ còn lại → ghi chú. Quy tắc `must` và chính sách thuật ngữ không bao giờ bị lược.
4. Thoát `<` và `>` để văn bản trong lõi không đóng được thẻ `<style_core>`; prompt đặt thêm câu chốt "chỉ chỉnh cách diễn đạt, không ghi đè quy tắc của prompt".
5. `lint` chặn văn bản có dấu hiệu điều khiển prompt (ví dụ "bỏ qua mọi chỉ dẫn", "chỉ xuất JSON", URL, thẻ hệ thống, `{{...}}`); đây là heuristic bổ trợ, không thay cho việc chỉ dùng phiên bản đã được người duyệt.

<!-- STYLE_COMPILE_DEMO -->

**Gắn vào công thức và job:** `recipe_config.style_core_id` và `style_core_pin` (`null` = phiên bản đã duyệt mới nhất tại thời điểm tạo job). Job lưu `style_core_version_id` và `glossary_releases`; cùng cặp này quyết định kết quả nên tái lập được.

### 19.8 Vai trò và quyền

| Vai trò | Được làm |
|---|---|
| `user` | Chọn công thức và lõi đã duyệt; đề xuất thuật ngữ lên glossary chuẩn |
| `curator` | Soạn, duyệt Lõi văn phong; duyệt hàng đợi thuật ngữ; phát hành glossary; chạy P12/P13 và thử lõi. **Không** đụng pool, khoá, tiền, người dùng |
| `admin` | Tất cả, kể cả cấp vai trò `curator` |

Lõi cá nhân của người dùng (`scope = personal`, FR-24, giai đoạn P2) chỉ do chủ sở hữu thấy và dùng, phải qua `lint`, không bao giờ nằm trong danh sách chung và không được dùng làm đầu vào của P12/P13. Mọi thao tác duyệt ghi `audit_log`.

### 19.9 Khởi tạo lõi cho lĩnh vực đầu tiên (Giai đoạn 0)

Không cần biết trước nội dung văn phong; quy trình tự sinh ra nó:

1. Thu 3-10 tài liệu mẫu của lĩnh vực và 5-20 **cặp dịch tham chiếu** (do người làm hoặc đã duyệt). Cặp tham chiếu là phần giá trị nhất: không có chúng, đề xuất chủ yếu là câu hỏi.
2. `POST /admin/glossaries/bootstrap` → P1, gộp, P13 → các mục `suggested`.
3. `POST /admin/style-cores/{id}/propose` (`bootstrap`) → phiên bản nháp có quyết định mở.
4. Người duyệt: trả lời quyết định, sửa, xoá quy tắc không cần, xác nhận phần còn lại; duyệt hàng đợi thuật ngữ.
5. `test-drive` trên 10-20 đoạn; điều chỉnh.
6. Duyệt `1.0.0` và phát hành glossary `v1`.
7. Chạy golden set (§16) với lõi mới **và** với lõi trung tính. **Lõi phải kiếm được chỗ của nó**: nếu không tốt hơn lõi trung tính ở điểm "Văn phong" mà không làm hỏng chỉ số khác thì đừng dùng.
8. Về sau, mỗi khi gom đủ phản hồi (cờ đoạn, nhận xét), chạy P12 chế độ `refine` để có phiên bản mới.

### 19.9b Công cụ bản M0

`visynth style {init,lint,compile,show,decide,approve,bump,deprecate,status,compare}` giữ ĐÚNG quy tắc §19.3
trên một kho JSON ngoài repo (M0 chưa có PostgreSQL): bản `approved` bất biến, chỉ tăng phiên bản mới khi sửa;
`approve` bị chặn khi còn quyết định mở, còn quy tắc/ví dụ `origin = ai` mà `reviewed = false`, hoặc lint có lỗi;
job chỉ biên dịch được từ phiên bản **đã duyệt** (`visynth run --style-core <id>[@version]`), và job ghi lại
`content_sha256` để tái lập. Lỗi vòng đời là lỗi người dùng: CLI in một dòng `LỖI: …`, thoát mã 1, không ném traceback. `style compare` là `test-drive` bước 5/7 (chạy lõi trung tính và lõi ứng viên trên
cùng tài liệu rồi so chỉ số đo được, nhắc rằng điểm "Văn phong" phải do người chấm).

### 19.10 Đã kiểm thử gì

`tests/test_style_core.py`: lõi trung tính và lõi mẫu hợp lệ theo schema; lõi trung tính không đặt quan điểm nào; biên dịch theo giai đoạn; thứ tự lược và việc `must` không bị lược; thoát dấu `<`; lint chặn các kiểu chèn chỉ thị; mục AI chưa xem chặn việc duyệt; kế thừa (ghi đè theo id, `unspecified` không xoá cha, phát hiện vòng lặp và độ sâu); hash ổn định. `tests/test_prompt_render.py`: lõi đi vào đúng chỗ, bọc như dữ liệu, mọi prompt dùng lõi có câu chốt bất biến. `tests/pg_smoke.py`: tính bất biến của phiên bản đã duyệt, các CHECK, bản phát hành glossary chỉ chứa mục `confirmed`.
