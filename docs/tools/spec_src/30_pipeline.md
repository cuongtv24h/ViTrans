
---

## 6. Pipeline xử lý

### 6.0 Tổng quan

```
tải lên / dán
    │
[extract] → [profile] → [segment] → [glossary] ──(cổng: người dùng duyệt thuật ngữ)──┐
                                                                                      ▼
 mức full_translation :  [translate = dịch trực tiếp P9] ──────────────────────────────▶ [assemble]
 mức 2-4 (tổng hợp)   :  [map] → [consolidate] → [write] → [verify] ⇄ [repair] → [assemble]
```

Nguyên tắc xuyên suốt:

1. **Tách "giữ gì" khỏi "viết thế nào".** `map` kê khai *đơn vị tri thức* có bằng chứng nguyên văn; `write` chỉ được viết từ các đơn vị đó. Nhờ vậy "không sót ý" kiểm tra được bằng đếm, không bằng cảm tính.
2. **Mọi thứ model sinh ra đều bị nghi ngờ cho đến khi code kiểm tra:** trích đoạn có thật trong nguồn (`reference/quote_verify.py`), con số có trong nguồn (`reference/numbers_check.py`), thuật ngữ đúng glossary (`reference/glossary_lint.py`).
3. **Mỗi giai đoạn là điểm lưu (checkpoint):** kết quả ghi vào CSDL; job chết giữa chừng chạy lại từ giai đoạn dở dang, không làm lại từ đầu, không tính tiền hai lần (§12).
4. **Dữ liệu người dùng luôn là DỮ LIỆU, không phải chỉ thị:** đặt trong thẻ, schema đầu ra cứng, không cấp công cụ cho model, kiểm tra đầu ra (§14.2).
5. **Khâu phụ không được làm chết khâu chính.** Khâu OCR cho trang thường và mọi bước chỉ nhằm tiết kiệm hay nâng chất lượng đều phải có đường quay về cách làm cơ bản; hạn mức nhà cung cấp hết thì task được hoãn (`defer_task`), không bị tính là lỗi (§17.8).

| Giai đoạn | Đầu vào | Đầu ra | Prompt | Song song | `task_key` |
|---|---|---|---|---|---|
| `extract` | file / văn bản | `documents`, `doc_paragraphs`, `doc_sections` | P10 (chỉ OCR) | theo cụm 10-15 trang OCR | `OCR:12-26` |
| `profile` | mẫu văn bản + thống kê | `documents.profile` | P0 | 1 | `profile` |
| `segment` | đoạn văn, hồ sơ | `segments` | — (tất định) | — | `segment` |
| `glossary` | toàn văn (hoặc cửa sổ 300k token) | `job_glossary_entries` (gợi ý) | P1 | theo cửa sổ | `W01` |
| `map` | segment + glossary đã lọc | `knowledge_units`, nhãn đoạn | P2 | theo segment | `SEG-007` |
| `consolidate` | các unit | kế hoạch báo cáo (`reports.plan`) | P3 | 1 (hoặc phân cấp) | `plan` |
| `write` | kế hoạch + unit | `report_blocks` | P4 | theo mục | `S03` |
| `verify` | khối + nguồn | verdict, issues, coverage | P5, P6 + kiểm tra tất định | theo mục | `S03:v1` |
| `repair` | issues | bản vá | P7 | theo mục | `S03:r1` |
| `translate` | segment (nhỏ hơn) | `translation_items` | P9 | theo segment | `SEG-007` |
| `assemble` | tất cả | `reports.markdown`, ghi chú phạm vi | P8 | 1 | `assemble` |

### 6.1 `extract` — bóc tách và chuẩn hoá

**Nhận file (PHẢI):** kiểm tra kích thước (≤ 50 MB), loại file bằng *magic bytes* (không tin đuôi file), từ chối PDF mã hoá không mở được (`encrypted_pdf`), chống zip-bomb cho DOCX/EPUB (giới hạn dung lượng giải nén 200 MB và tỷ lệ nén ≤ 100), chạy parser trong **sandbox** (container không mạng, hệ tệp chỉ đọc, giới hạn CPU/RAM, timeout 120 giây, chạy non-root).

| Loại | Cách xử lý |
|---|---|
| TXT / MD | Phát hiện mã hoá (`charset-normalizer`), chuẩn hoá xuống dòng; tách đoạn theo dòng trống; tiêu đề Markdown → `heading` |
| DOCX | `python-docx`: style Heading → `heading`, danh sách → `list_item`, bảng → `table` (Markdown); bỏ ảnh (ghi cảnh báo `images_ignored`), nhận bản cuối của tracked-changes |
| DOC (cũ) | LibreOffice headless → DOCX (trong sandbox, timeout 60 giây); lỗi thì trả hướng dẫn "hãy lưu thành DOCX" |
| PDF có chữ | `pypdfium2` lấy chữ theo trang; bỏ header/footer lặp (dòng xuất hiện ở > 40% số trang cùng vùng); nối từ bị ngắt dòng; ghép dòng thành đoạn theo khoảng cách dọc và thụt lề; ghi `page_start/page_end` |
| PDF scan hoặc chữ hỏng | Phát hiện: median ký tự/trang < 200 hoặc tỷ lệ ký tự lạ cao → OCR bằng **P10** theo cụm 10-15 trang (Gemini nhận PDF tối đa 50 MB hoặc 1000 trang/tài liệu; PDF có chữ nhúng sẵn thì phần chữ không tính phí, trang quét tính theo token ảnh); cảnh báo `scanned_pdf_ocr_used` |
| PDF nhiều cột / bảng phức tạp | Nếu phát hiện, chuyển sang P10 cho các trang đó (cảnh báo `multi_column_detected`) |
| SRT / VTT | Gộp các cue thành đoạn lời thoại (gộp đến hết câu hoặc khoảng lặng > 2 giây); giữ `timecode_start_ms/end_ms`; bỏ nhãn thời gian, ký hiệu `(music)`; **khử trùng lặp** phụ đề cuốn chiếu (YouTube auto-caption lặp dòng) |
| EPUB | `zipfile` + `lxml`: đọc OPF/spine theo thứ tự, XHTML → đoạn; TOC → `doc_sections` |
| Dán văn bản | Như TXT |

**Chuẩn hoá chung (PHẢI):** Unicode **NFC** cho mọi văn bản lưu; loại ký tự điều khiển; không đổi nội dung (không sửa chính tả). Mỗi đoạn có `pid` ổn định dạng `P000001` tăng dần theo thứ tự đọc; `kind` theo `paragraphKind`.

**Điểm chất lượng bóc tách `extraction_quality` ∈ [0,1]:** trung bình có trọng số của (a) tỷ lệ ký tự in được, (b) độ dài đoạn hợp lý, (c) tỷ lệ trang phải OCR (trừ điểm), (d) tỷ lệ "từ lạ" (dùng từ điển nhỏ hoặc n-gram). < 0.6 thì cảnh báo `low_text_quality` ngay ở bước ước tính.

**Ngôn ngữ nguồn bằng tiếng Việt:** vẫn chạy được mức 2-4 (tổng hợp bằng tiếng Việt, bỏ bước dịch thuật ngữ; glossary dùng để chuẩn hoá thuật ngữ). Mức 1 bị từ chối (`source_equals_target`).

### 6.2 `profile` — hồ sơ tài liệu (P0)

Đầu vào: thống kê (số từ, số trang, có timecode/lời thoại/chú thích/bảng), dàn ý tiêu đề (nếu có) và **mẫu** văn bản: 6k token đầu + 3 đoạn 2k token ở giữa + 2k token cuối (hoặc toàn văn nếu < 15k token). Đầu ra: `DocProfile`. Quan trọng nhất: `attribution_mode`, `recommended_segmentation`, `doc_type`. Lỗi schema thì dùng hồ sơ mặc định bảo thủ (`attribute_to_author`, `by_tokens`, 8000) và ghi cảnh báo.

### 6.3 `segment` — chia đoạn xử lý (tất định)

```python
def segment(paragraphs, sections, profile, mode):                 # mode: "map" hoặc "translate"
    target = 4000 if mode == "translate" else profile.recommended_segmentation.target_tokens   # mặc định 8000
    MAX, MIN, CTX = 12000, 2500, 2
    units = base_units(profile.strategy)      # by_headings: mỗi mục; by_timecodes: cửa sổ thời gian; by_tokens: toàn bộ
    segs, cur, cur_tok = [], [], 0
    for u in units:
        t = tokens(u)
        if t > MAX:                                            # mục quá dài: cắt theo đoạn
            flush(cur); segs += split_by_paragraphs(u, target); cur, cur_tok = [], 0; continue
        if cur and cur_tok + t > target and cur_tok >= MIN:
            flush(cur); cur, cur_tok = [], 0
        cur += u; cur_tok += t
    flush(cur)
    merge_last_if_smaller_than(MIN)                            # gộp segment cuối quá nhỏ vào segment trước
    for i, s in enumerate(segs):
        s.context_before = last_paragraphs(segs[i-1], CTX) if i else []   # CHỈ ĐỌC, không trích unit, không trích dẫn
    return segs

def split_by_paragraphs(unit, target):
    # cắt tham lam tại RANH GIỚI ĐOẠN, ưu tiên sau đoạn kết thúc bằng dấu kết câu hoặc sau khoảng lặng timecode > 2 s;
    # một đoạn đơn lẻ > MAX thì tách theo câu. Không bao giờ cắt giữa câu trừ trường hợp cuối cùng này.
```

**Cửa sổ theo năng lực của profile.** Pool có thể định tuyến một tác vụ tới bất kỳ deployment nào thoả `needs` của profile, nên segment PHẢI vừa với deployment *yếu nhất* hợp lệ: `target_tokens ≤ 0.2 × needs.min_ctx_in` của profile (mặc định `writer` = 100.000 → tối đa 20.000 token; mặc định hiện hành 8.000 vẫn thoả). Khi chủ yếu dùng free tier, thứ khan hiếm là số lời gọi mỗi ngày chứ không phải token (§17.10), nên có thể đặt `pipeline.window_scale` = 2-4 để có ít segment hơn, đổi lại mỗi lời gọi lớn hơn và rủi ro chạm TPM/ngữ cảnh cao hơn; với `translate` cửa sổ bị chặn ở 8.000 token vì đầu ra xấp xỉ 1,5 lần đầu vào và phải nằm dưới trần đầu ra (20.000 token).

Ước lượng token: dùng `count_tokens` của API khi có, nếu không thì `ký_tự / 4` cho tiếng Anh và `ký_tự / 2.8` cho tiếng Việt (ước lượng thô, chỉ để chia đoạn). Sách 300 trang (~120k token) ra khoảng 15 segment.

### 6.4 `glossary` — nhận diện thuật ngữ và cổng duyệt

1. **Gộp glossary đầu vào** vào `job_glossary_entries`: mục chuẩn lấy từ **bản phát hành đã ghim** (`glossary_releases`, không phải bản đang soạn; `origin=shared`, `priority=10`), mục cá nhân (`personal`, 20); trùng `source_term` (không phân biệt hoa/thường) thì mục ưu tiên cao thắng. Chụp ảnh tại thời điểm tạo job (kèm `jobs.glossary_releases` và `jobs.style_core_version_id`) để kết quả tái lập được dù glossary hoặc Lõi văn phong đổi sau đó.
2. **P1** chạy trên toàn văn (≤ 700k token trong một lời gọi; dài hơn thì cửa sổ 300k token có chồng lấn rồi gộp, trùng thì giữ mục có `occurrences` lớn hơn). Đầu vào gồm glossary hiện có để P1 **không đề xuất lại** thuật ngữ đã có. Ghi các gợi ý với `origin=suggested`, `status=suggested`.
3. **Cổng duyệt** (`awaiting_glossary`) khi tài liệu ≥ 2000 từ và người dùng không chọn `skip_glossary_review`: UI hiện các gợi ý (sắp theo độ tin cậy tăng dần để mục cần quyết định nằm trên). Người dùng sửa/chấp nhận/loại, rồi `POST /jobs/{id}/glossary/confirm`. **Hết hạn 15 phút** (`glossary_review_deadline`) thì tự xác nhận các gợi ý có `confidence ≥ 0.7`, loại phần còn lại, ghi `glossary_auto_confirmed = true`. Tài liệu ngắn hơn 2000 từ bỏ cổng và tự xác nhận theo cùng quy tắc.
4. Sau khi xác nhận, các mục `confirmed` là **ràng buộc cứng** cho các prompt sau. Cho phép lưu vào glossary cá nhân (`save_to_glossary_id`) hoặc **đề xuất lên glossary chuẩn** (vào hàng đợi duyệt của curator với `proposed_by = user`, §19.5).

Lý do cổng này đáng giá: thuật ngữ là chỗ khó chịu nhất trong tài liệu chuyên ngành; hai phút của người dùng ở đây cải thiện chất lượng nhiều hơn việc đổi model.

### 6.5 `map` — kê khai khái niệm (P2)

Với mỗi segment (song song, tối đa `per_job_concurrency`):

1. Dựng biến: `segment_text` (định dạng `[Pxxxxxx] ...`), `context_before`, glossary **đã lọc** theo thuật ngữ thực sự xuất hiện trong segment (`filter_glossary`), `profile_json`.
2. Gọi P2 với wire schema; kiểm tra schema đầy đủ.
3. **Kiểm chứng đầu ra (tất định):**
   - mọi `evidence.pid` PHẢI thuộc segment;
   - mọi `quote` PHẢI qua `verify_quote` (mặc định nghiêm ngặt; chỉ cho phép khớp mờ khi tài liệu có `ocr_noise`, và khớp mờ luôn bị chặn nếu quote có chữ số không có trong đoạn nguồn);
   - unit có **ít nhất một** bằng chứng hợp lệ mới được giữ; bằng chứng hỏng bị loại khỏi unit; unit mất hết bằng chứng → `state = unverified` (không đưa vào kế hoạch, tính vào thống kê);
   - `paragraph_labels` phải phủ kín mọi pid: khoảng trống tự gán `core` (bảo thủ, không làm mất nội dung); chồng lấn giữ khoảng đầu.
4. Nếu **> 30% bằng chứng hỏng**, hoặc schema không hợp lệ, hoặc segment > 300 từ mà không có unit: chạy lại **một lần**, nối vào cuối thông điệp người dùng phần phản hồi (danh sách pid không hợp lệ và 80 ký tự đầu của mỗi quote hỏng). Vẫn hỏng: giữ phần đã xác minh, thêm cờ `extraction_degraded`, phát sự kiện `warning`; nếu số unit xác minh dưới nửa mật độ kỳ vọng thì **chia đôi segment** và chạy lại (tối đa một lần chia).
5. `OutputTruncated` → chia đôi segment ngay và chạy lại.
6. Sau khi mọi segment xong: cấp ID toàn cục `U-0001...` theo thứ tự (segment, local_id); ánh xạ lại `relations`; kiểm tra mật độ (unit/1000 từ; thấp hơn 2 với sách/bài giảng thì cảnh báo). Nếu unit `core` bị loại vì `unverified` chiếm ≥ 3% tổng core thì cảnh báo người dùng "một số ý không thể xác minh với nguồn".
7. Lưu `knowledge_units`, `segments.labels`, `segments.summary_vi`; gom `new_terms` đưa vào glossary job với `origin=suggested` (chờ duyệt ở lần sau, không chặn job).

### 6.6 `consolidate` — dàn ý và phân bổ (P3)

1. **Tính ngân sách** `budget_words = report_budget_words(source_words, level)` (§7.2) và chọn khối `level_policy` (§7.3).
2. Dựng `units_compact` từ các unit `active` (bỏ `minor` ở mức 3-4; vẫn gồm ở mức 2 nếu ngắn). Nếu số unit > **1500** thì **phân cấp**: nhóm theo `topics` thành các cụm ≤ 1500, chạy P3 cho từng cụm lấy dàn ý cấp mục, rồi một lần P3 nữa để sắp thứ tự và đặt tiêu đề.
3. **Kiểm tra kế hoạch (tất định):** mọi unit `core` được gán vào đúng một mục `body` hoặc nằm trong `merged_groups` (và unit giữ lại đã được gán); không unit nào ở hai mục `body`; mục đầu có `kind = summary`; số mục 4-20; tổng `target_words` trong ±15% ngân sách; mọi ID tồn tại. Vi phạm: chạy lại P3 **một lần** kèm danh sách lỗi; vẫn lỗi thì **sửa tự động** (gán unit core bị bỏ sót vào mục gần nhất theo `topics`) và ghi cờ.
4. Ghi `reports.plan`; các unit không được gán và không phải core được đánh `omitted` với lý do (`minor` do hệ thống, còn lại theo `plan.omitted`); unit đã gộp → `merged` + `merged_into`.
5. **Bảng dữ kiện** (nếu `include_facts_table`): dựng *tất định* từ các unit `fact_data` (cột: dữ kiện, giá trị, nguồn), không để model viết lại.

### 6.7 `write` — viết từng mục (P4)

- **Song song theo mục**; sự liên tục mạch lạc đến từ `outline_digest` (tiêu đề + `purpose_vi` của mọi mục), không phụ thuộc bản nháp mục khác.
- **`first_use_terms` tính tất định** vì các mục viết song song: duyệt các mục theo thứ tự kế hoạch, thuật ngữ `keep_original` thuộc về mục *đầu tiên* có unit chứa thuật ngữ đó (hàm `first_use_by_section` trong `reference/glossary_lint.py`).
- Đầu vào `units_json`: mọi unit được gán cho mục (id, type, importance, `title_vi`, `statement_vi`, bằng chứng đã xác minh, `numbers`, `attribution`).
- Sau khi nhận kết quả: cấp `block_id` (`S03.b01...`); kiểm tra `cites ⊆ unit của mục`, mọi unit core đã được trích dẫn ít nhất một lần, độ dài trong ±35% (ngoài ngưỡng cứng thì chạy lại một lần), không có HTML thô, markdown hợp lệ.
- Mục `summary` không cần viết sau các mục khác: nó chỉ dựa vào các unit core đã được chọn nên chạy song song như mọi mục.

### 6.8 `verify` — kiểm chứng

**Kiểm tra tất định** (cho mọi khối, chi phí gần bằng 0):

| Mã | Kiểm tra | Hành động khi vi phạm |
|---|---|---|
| D1 | `cites` ⊆ unit được giao | lỗi cứng → `repair` |
| D2 | Mọi unit core được trích dẫn ≥ 1 lần | đưa vào danh sách "cần P6" |
| D3 | Số trong khối có trong đoạn nguồn của các unit được trích dẫn (`unverified_numbers`; nguồn đổi chữ số viết bằng chữ tiếng Anh) | issue `number_mismatch`; nếu số có ở nơi khác trong tài liệu nhưng không ở đoạn được trích dẫn thì chỉ cảnh báo |
| D4 | Lint glossary (`forbidden_variant`, `missing_original_on_first_use`, `untranslated_source_term`) | issue `term_inconsistency` |
| D5 | Rò ID nội bộ (`U-\d{4}`, `P\d{6}`, `S\d{2}\.b\d{2}`) | lỗi cứng |
| D6 | Câu dẫn kiểu trợ lý ("Dưới đây là", "Tất nhiên", "Là một AI", "Tôi không thể") | lỗi cứng |
| D7 | Thẻ HTML, URL không có trong nguồn | lỗi cứng |
| D8 | Độ dài so với `target_words` (ngoài ±35%) | cảnh báo; chạy lại ở `write` nếu quá cứng |
| D9 | Tỷ lệ ký tự chữ có dấu tiếng Việt < 12% trong khối dài (> 200 ký tự) | nghi ngờ chưa dịch → `repair` (ngưỡng khởi điểm, hiệu chỉnh bằng golden set) |

**Kiểm tra bằng LLM:**

- **P5 (trung thực)** cho 100% khối ở MVP (`verify.sample_rate = 1.0`; có thể giảm khi cần tiết kiệm chi phí). Đầu vào gồm `evidence_json` (các unit được trích dẫn) và `source_passages` (các đoạn nguồn được trích dẫn ± 1 đoạn lân cận, tối đa 60 đoạn mỗi lời gọi; nhiều hơn thì chia các khối ra nhiều lời gọi). **Đối chiếu với đoạn nguồn chứ không chỉ với statement của unit** vì unit cũng do model sinh.
- **P6 (độ phủ)** cho (a) unit core không được trích dẫn ở đâu (D2) và (b) mẫu ngẫu nhiên 10% unit core đã được trích dẫn, để xác nhận khối đó thật sự diễn đạt ý.
- Dùng **model/họ model khác** với profile `writer` cho `verifier` nếu bake-off cho thấy đạt chất lượng (giảm lỗi tương quan giữa người viết và người kiểm).

**Chỉ số và hạng chất lượng:**

- `coverage_core = (#core "yes" + 0.5·#core "partial") / #core` (core đã được trích dẫn và không bị P6 bác tính là `yes`).
- `faithful_block` = verdict `supported`, hoặc `partially_supported` mà mọi issue thuộc {`missing_nuance`, `term_inconsistency`, `other`}. `faithfulness_rate = #faithful_block / #block`.
- `unresolved` = khối còn `unsupported`/`contradicted` hoặc còn issue thuộc {`number_mismatch`, `date_mismatch`, `name_mismatch`, `fabricated`, `overreach`, `attribution_missing`} sau vòng sửa cuối.
- **Hạng A:** `coverage_core ≥ 0.95` và `unresolved = 0`. **Hạng B:** `coverage_core ≥ 0.90`, `unresolved ≤ 3%` số khối, không còn `fabricated`/`contradicted`. **Hạng C:** còn lại (hoàn 50% tín dụng, hiển thị cảnh báo).

### 6.9 `repair` — sửa tối thiểu (P7)

- Tối đa **2 vòng** mỗi mục. Mỗi vòng: gom issue (tất định + P5) và unit core thiếu (P6/D2) → P7 → áp dụng bản vá (`replace`, `insert_after`, `delete`) → kiểm tra tất định lại + P5 **chỉ cho khối đã đổi** + P6 cho khối mới thêm.
- Sau vòng 2: khối còn `fabricated`/`contradicted` bị **xoá**; khối còn lỗi khác được đánh `flagged` (UI hiện cảnh báo, tính vào `unresolved`). Mục mất hết khối thì đánh cờ và thông báo.
- Mọi bản vá được kiểm tra `cites` ⊆ unit hợp lệ.

### 6.10 `translate` — dịch đầy đủ (mức `full_translation`)

Một task `translate` xử lý MỘT segment nhỏ (mục tiêu 4000 token, để đầu ra không chạm trần và dễ căn 1:1) và chạy song song giữa các segment. Liên tục mạch lạc nhờ `context_before` (2 đoạn nguồn trước đó, chỉ đọc), glossary, Lõi văn phong; `first_use_terms` tính tất định theo thứ tự đoạn.

Mức dịch đầy đủ **chỉ dùng LLM dịch trực tiếp (P9)**. Khâu bản dịch thô của model dịch máy đã được cân nhắc và loại khỏi phạm vi (§18): không rẻ hơn khi LLM vẫn viết lại toàn bộ, không giảm số lời gọi, và điều khoản endpoint miễn phí chỉ cho thử nghiệm.

**Các bước trong một task (mỗi bước ghi điểm lưu để chạy lại không làm lại):**

1. **Gọi LLM qua pool** (profile `writer`, `privacy_class` của job). Pool chưa có chỗ thì task được hoãn bằng `defer_task` (không tính lần thử).
2. **Kiểm tra tất định lên kết quả:** mỗi pid đầu vào xuất hiện đúng một lần; không rỗng; tỷ lệ độ dài `len(vi)/len(src)` ∈ [0.6, 2.2] với đoạn ≥ 40 ký tự (dưới 0.6 nghi cắt bớt, trên 2.2 nghi thêm nội dung); số liệu (`unverified_numbers` và số nguồn bị mất); lint glossary; đoạn dài không có dấu tiếng Việt thì nghi chưa dịch. Vi phạm: chạy lại segment **một lần** kèm phản hồi; vẫn lỗi thì đánh `flagged` từng đoạn.
3. Lưu `translation_items` và số liệu vào `job_stages.metrics` của giai đoạn `translate`.

> **15% đoạn bị `flagged` sau chạy lại → hạng C.** Bản dịch lưu ở `translation_items`; xuất ra Markdown theo cấu trúc `doc_sections`; chế độ song ngữ ghép với `doc_paragraphs` khi còn.

### 6.11 `assemble` — hoàn thiện

1. Tính `stats`: `coverage_core`, `faithfulness_rate`, `flagged_blocks`, số unit theo `importance`/`state`, số unit bị lược theo lý do, `source_words`, `report_words`, tỷ lệ từ nguồn theo nhãn đoạn (`core/example/qa/anecdote/aside/admin`).
2. **P8** nhận `stats_json`, `core_topics` (10 chủ đề có nhiều unit core nhất), `condensed_kinds` (ví dụ: hỏi-đáp, ví dụ, giai thoại, lan man, hành chính, kèm số đoạn và tỷ lệ từ nguồn, cách xử lý "cô đọng" hay "lược bỏ") → mục **"Phạm vi & cách xử lý"**. Mọi con số do code tính, model không được bịa số.
3. Ghép Markdown: tiêu đề → dòng thông tin (tên tài liệu nguồn, mức, ngày) → **hộp thông báo AI** → mục lục → mục tóm tắt → các mục chính → bảng dữ kiện → phụ lục thuật ngữ (các thuật ngữ thực sự được dùng, kèm số lần) → ghi chú phạm vi → danh sách nguồn trích dẫn. Trích dẫn đánh số theo thứ tự xuất hiện; mỗi số ánh xạ tới trang/timecode và trích đoạn nguồn.
4. Lưu `reports`, `report_sections`, `report_blocks`; cấp `quality_grade`; chốt chi phí; áp chính sách hoàn tín dụng (§12.6); phát `job_succeeded`.

### 6.12 Cấu hình mặc định (một chỗ)

```yaml
pipeline:
  segment:    { target_tokens_map: 8000, target_tokens_translate: 4000, max_tokens: 12000, min_tokens: 2500, context_paragraphs: 2 }
  glossary:   { gate_min_words: 2000, auto_confirm_minutes: 15, auto_confirm_min_confidence: 0.7, max_candidates: 150, window_tokens: 300000 }
  map:        { bad_quote_ratio_retry: 0.30, density_warn_low_per_1k_words: 2, allow_fuzzy_only_if_ocr: true, fuzzy_threshold: 95 }
  consolidate: { max_units_single_call: 1500, sections_min: 4, sections_max: 20, budget_tolerance: 0.15 }
  write:      { length_soft_tolerance: 0.20, length_hard_tolerance: 0.35 }
  verify:     { sample_rate: 1.0, coverage_sample_rate: 0.10, max_source_paragraphs_per_call: 60, diacritic_ratio_min: 0.12 }
  repair:     { max_rounds: 2 }
  translate:  { length_ratio_range: [0.6, 2.2], flagged_ratio_for_grade_c: 0.15 }
  pool:       # xem §17; ở đây chỉ phần pipeline cần biết
    window_scale: 1.0
    verify_batch: 1
    max_pool_wait_hours: 12        # tổng thời gian chờ hạn mức của một job trước khi chuyển tầng trả phí hoặc thất bại
  grade:      { A: { coverage_core: 0.95, unresolved: 0 }, B: { coverage_core: 0.90, unresolved_ratio: 0.03 } }
  job:        { max_cost_multiplier: 1.5, glossary_snapshot: true }
```

### 6.13 Phân loại lỗi và hành động

| Tình huống | Hành động |
|---|---|
| JSON hoặc schema sai | Thử lại **một lần** kèm lỗi (ghi `outcome = invalid_output`, làm giảm điểm hợp lệ của deployment); vẫn sai → leo lên deployment khác của profile, rồi xử lý theo giai đoạn (map: giữ phần xác minh được; write: chạy lại mục; plan: tự sửa) |
| Trích đoạn bịa (`missing`) | Loại bằng chứng; >30% → chạy lại P2 (§6.5) |
| Cắt cụt (`MAX_TOKENS`) | Chia đôi đầu vào và chạy lại |
| Bị chặn an toàn | Không thử lại; segment/mục đánh `blocked`, cảnh báo; >20% segment bị chặn → job `failed` (hoàn tín dụng đầy đủ) |
| 429, 5xx, timeout, khoá bị từ chối | Do **pool** xử lý (§17.7): cooldown đúng loại, circuit breaker, cách ly khoá, chuyển ngay sang deployment khác (`exclude`); không còn `fallback_model` đơn lẻ |
| Pool chưa có chỗ (hết RPM/TPM/RPD hoặc đang cooldown) | Hoãn task bằng `defer_task` tới `Wait.until`, **không** tính lần thử; chờ quá `max_pool_wait_hours` thì chuyển tầng trả phí (nếu được phép và chưa chạm trần) hoặc job `failed` mã `pool_wait_exceeded` kèm hoàn tín dụng |
| Không deployment nào đủ điều kiện (ví dụ job `private` nhưng không còn nhóm `no_training`) | Job `failed` mã `pool_capacity_exceeded` ngay ở lúc tạo nếu phát hiện được (ước tính), hoàn tín dụng đầy đủ; **không bao giờ** rò sang nhóm không đủ điều kiện để cứu job |
| Chạm trần chi phí job (×1.5 ước tính) | Dừng job (`failed`, mã `job_cost_cap`), hoàn tín dụng phần chưa dùng theo §12.6, báo Admin |
| Chạm trần chi tiêu ngày | Job đang chạy xong bình thường; job mới nhận 503 `spend_cap_reached` |
| Worker chết giữa chừng | Mất heartbeat > 3 phút → `reclaim_stale_tasks`; chạy lại task (idempotent); quá `max_attempts` → `failed` |

---

## 7. Mức cô đọng và chính sách theo loại nội dung

### 7.1 Bốn mức

| Mức (`level`) | Tên hiển thị | Dùng khi | Pipeline |
|---|---|---|---|
| `full_translation` | Dịch đầy đủ | Cần đọc đủ từng ý của nguồn | `translate` (P9) |
| `detailed_synthesis` | Tổng hợp chi tiết | Muốn nắm gần hết ý, bỏ lặp và lan man | `map`→`verify` |
| `deep_synthesis` | **Báo cáo chuyên sâu** (mặc định) | Hiểu hệ thống ý tưởng và cơ chế; đúng tinh thần "Deep Synthesis Report" | `map`→`verify` |
| `executive_brief` | Tóm lược điều hành | Chỉ cần ý trung tâm | `map`→`verify` |

### 7.2 Ngân sách độ dài (`report_budget_words`)

<!-- LEVEL_BUDGET_TABLE -->

Ngân sách là *mục tiêu cho P3*, không phải ràng buộc cứng với nội dung: các unit core luôn có chỗ (§6.6). Báo cáo không bao giờ dài quá 80% bản gốc.

### 7.3 Ma trận xử lý và khối `level_policy`

| Loại unit \ Mức | `detailed_synthesis` | `deep_synthesis` | `executive_brief` |
|---|---|---|---|
| core | Đầy đủ chi tiết | Đủ ý, điều kiện, con số, tên; diễn đạt cô đọng | Chỉ các ý trung tâm, 1-2 câu |
| supporting | Giữ chi tiết chính (1-2 câu) | 1 câu hoặc gộp vào câu của unit core | Bỏ (trừ dữ kiện quyết định) |
| minor | Bỏ trừ khi rất ngắn | Bỏ (ghi vào "đã lược" của ghi chú phạm vi) | Bỏ |
| `qa` | Giữ nếu thêm thông tin mới | Chỉ câu trả lời có thông tin mới, 1 câu | Bỏ |
| `example` | Giữ ý chính + dữ kiện thiết yếu | Tối đa 1 câu ngắn, chỉ khi làm rõ unit core | Bỏ |
| `anecdote`, `aside`, `admin` | Bỏ trừ khi chứa dữ kiện | Bỏ | Bỏ |
| `fact_data` có số/ngày/tên | Luôn giữ | **Luôn giữ** (và đưa vào bảng dữ kiện) | Giữ nếu gắn với ý trung tâm |

Hệ thống chèn đúng một trong các khối sau vào biến `level_policy` của P3 và P4:

```text
[detailed_synthesis]
Level: detailed_synthesis (about 35% of the source length, at most 24000 words). Keep core units in full detail (all conditions, numbers, names and reasoning). Keep supporting units with their key detail (one or two sentences each). Examples: keep the point and the essential facts in two sentences. Q&A: keep when it adds information not stated elsewhere. Anecdotes and asides: omit unless they carry a fact. Minor units are not provided.

[deep_synthesis]
Level: deep_synthesis (about 12% of the source length, between 800 and 9000 words). Keep EVERY core unit with its conditions, numbers, names and reasoning, in condensed wording. Condense supporting units to one sentence each, or merge them into the sentence of the core unit they support. Examples: at most one short sentence, only when they clarify a core unit. Q&A: keep only answers that add information, as one sentence. Anecdotes, asides and admin: omit. Never drop a number, date or name that belongs to a core unit.

[executive_brief]
Level: executive_brief (about 3% of the source length, between 250 and 1500 words). Convey only the central claims: the most important core units, one or two sentences each, grouped by theme. Omit supporting units except decisive facts (numbers, dates) attached to a central claim. No examples, no Q&A, no anecdotes.
```

### 7.4 Quy tắc chung cho mọi mức

1. **Con số, ngày tháng, tên, mã số luôn được giữ chính xác**; không làm tròn, không đổi đơn vị.
2. **Chế độ quy gán** (`attribution_mode`): tài liệu trình bày hệ thống/niềm tin/lý thuyết thì mọi khẳng định được viết là của tác giả/diễn giả ("theo diễn giả..."), không khẳng định như sự thật khách quan, không bác bỏ. Đây là chốt chặn trung thực và cũng bảo vệ sản phẩm khỏi bị hiểu là xác nhận nội dung.
3. **Không thêm kiến thức ngoài nguồn**, không "sửa" tác giả, không kết luận hộ tác giả.
4. **Ghi chú phạm vi** (P8) luôn có ở mức 2-4 và nói thẳng: đây là báo cáo tổng hợp, **không phải bản dịch nguyên văn**.

---

## 8. Thư viện prompt

Toàn văn ở **Phụ lục A** và trong `prompts/`. Mỗi prompt là một tệp có front matter (id, version, giai đoạn, profile model, mức thinking, schema đầu ra, biến, trần token) và hai mục `## SYSTEM`, `## USER`.

<!-- PROMPT_TABLE -->

### 8.1 Nguyên tắc thiết kế prompt

1. **Dữ liệu ≠ chỉ thị.** Mọi nội dung do người dùng hoặc tài liệu cung cấp nằm trong thẻ (`<segment>`, `<document>`, `<user_preferences>`...) và được khai báo là DỮ LIỆU không tin cậy. Thêm vào đó: schema đầu ra cứng, không cấp công cụ, kiểm tra đầu ra bằng code.
2. **Prompt viết bằng tiếng Anh, đầu ra hiển thị bằng tiếng Việt.** Các quy tắc bất biến (trung thực, giữ nguyên số liệu/tên/ID, phạm vi, schema) nằm trong prompt hệ thống; phần **văn phong và thuật ngữ** do **Lõi văn phong** đã duyệt cung cấp qua biến `style_core` (§19), có thể viết bằng tiếng Việt hoặc tiếng Anh tuỳ điều gì rõ hơn với model. Muốn đổi sang prompt tiếng Việt thì phải chạy lại bake-off (§16.4).
3. **Bằng chứng nguyên văn + kiểm tra bằng code** thay cho niềm tin vào model.
4. **Thà thiếu còn hơn bịa:** mọi prompt có đường thoát an toàn (`flags`, `notes`, `confidence`, `[illegible]`).
5. **Một prompt một việc**, đầu ra có schema, trần token riêng.
6. **Thay biến một lượt** (`reference/prompt_render.py`) để nội dung tài liệu chứa `{{...}}` không bao giờ bị mở rộng thành biến (chống template injection; có test).
7. Văn phong và thuật ngữ nằm ở **một nơi** (Lõi văn phong và glossary, đều là dữ liệu có phiên bản), không rải trong từng prompt. Spec không đặt quan điểm văn phong nào: lõi mặc định (`prompts/00_style_core_neutral.json`) để trống mọi lựa chọn. Lõi **chỉ điều chỉnh cách diễn đạt** và không thể ghi đè quy tắc bất biến; mọi prompt dùng `style_core` có câu chốt nêu điều này và có test kiểm tra.

### 8.2 Định dạng các biến

| Biến | Định dạng |
|---|---|
| `profile_json` | JSON gọn của `DocProfile` |
| `glossary_json` | Mảng `GlossaryEntry`, **đã lọc** theo thuật ngữ xuất hiện trong đầu vào của lời gọi (cho P4/P7: lọc theo `terms` và `target_term` có trong unit) |
| `first_use_terms` | Mảng chuỗi `source_term` (tính tất định, §6.7) |
| `segment_text`, `context_before`, `source_passages` | `[P000123] nội dung`, các đoạn cách nhau một dòng trống |
| `units_compact` (P3) | Mỗi dòng `U-0001 \| importance \| type \| title \| statement \| topics` (ký tự `\|` trong nội dung đổi thành `/`) |
| `units_json` (P4) | Mảng unit `{id, type, importance, title_vi, statement_vi, topics, evidence:[{pid, quote}], numbers, attribution}` của mục |
| `section_json` | `{id, kind, title_vi, purpose_vi, target_words, format_hint}` |
| `outline_digest` | Mảng `{id, title_vi, purpose_vi}` của mọi mục |
| `blocks_json` (P5, P7) | Mảng `{block_id, type, markdown_vi, cites}` |
| `evidence_json` (P5) | Mảng `{unit_id, statement_vi, evidence:[{pid, quote}]}` của các unit được trích dẫn |
| `issues_json`, `missing_units_json` (P7) | Mảng issue theo `block_id`; mảng unit core thiếu |
| `stats_json`, `core_topics`, `condensed_kinds` (P8) | Số liệu do code tính |
| `level_policy` | Khối văn bản ở §7.3 theo mức |
| `style_core` | Văn bản biên dịch từ phiên bản Lõi văn phong đã ghim cho job, đúng giai đoạn của prompt (`compile_style_core(content, stage)`, §19.7); mặc định lõi trung tính |
| `mode`, `domain_brief`, `sample_excerpts`, `reference_pairs_json`, `existing_core_json`, `glossary_digest_json`, `feedback_digest_json` (P12) | Đầu vào của đề xuất Lõi văn phong; `sample_excerpts` đánh ID `[S01]`, cặp tham chiếu có ID `REF01`, phản hồi có ID `FB01` (§19.4) |
| `merged_candidates_json`, `existing_glossary_json`, `terminology_policy_json`, `max_entries` (P13) | Đầu ra tất định của `merge_candidates()` kèm ngữ cảnh có `ctx_id` (§19.5) |
| `custom_instructions` | Chuỗi người dùng nhập (≤ 1000 ký tự), đặt trong `<user_preferences>` |

### 8.3 Quản lý phiên bản prompt

- Mỗi prompt có `version` (semver). Đổi nội dung = tăng version; đổi cấu trúc đầu ra = tăng MAJOR **và** schema tương ứng.
- Mỗi job lưu `prompt_versions` (ảnh chụp) và `model_profile`; bảng `prompt_versions` lưu hash nội dung để phát hiện chỉnh sửa lén.
- **Quy trình thay đổi:** sửa prompt → chạy test tất định → chạy golden subset (§16.3) → so với baseline → duyệt → triển khai. Có thể **hoàn tác** bằng cách trỏ lại phiên bản trước (không cần build lại).
- **Lõi văn phong có phiên bản riêng** (semver trong `style_core_versions`), bất biến sau khi duyệt, và job chụp lại phiên bản đã dùng; đổi lõi không cần đổi prompt, nhưng thay đổi lớn của lõi cũng phải qua golden subset (§16.3).

### 8.4 Quy tắc bất biến và Lõi văn phong

| | Quy tắc bất biến (nằm trong prompt hệ thống, không thể ghi đè) | Lõi văn phong (dữ liệu, có thể thay) |
|---|---|---|
| Nội dung | Trung thực, không thêm/bớt/suy diễn; giữ nguyên số liệu, ngày, tên, mã, ID; quy gán nguồn; độ phủ; schema đầu ra; không lời dẫn kiểu trợ lý; không lộ ID nội bộ | Giọng văn, cách trình bày thuật ngữ, quy ước định dạng, ưu tiên ở mức câu, ví dụ mẫu |
| Ai quyết định | Kỹ sư prompt, qua golden set | AI đề xuất, người duyệt quyết định (§19) |
| Đổi như thế nào | Đổi prompt và tăng phiên bản prompt | Tạo phiên bản lõi mới và duyệt |
| Có thể sai thế nào | Làm hỏng độ tin cậy của mọi báo cáo | Làm văn phong lệch; không làm sai sự thật vì bị chặn ở cột bên trái |
