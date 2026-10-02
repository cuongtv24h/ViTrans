
---

## 18. Quyết định: bỏ hẳn bản dịch thô của model dịch máy

> Bạn đề xuất dùng `riva-translate-4b-instruct-v2` của NVIDIA tạo bản thô rồi cho LLM hiệu đính, kèm điều kiện "nếu lợi ích không cao thì bỏ". Kết luận: **lợi ích không tương xứng nên bỏ hẳn** — xoá cả thiết kế, mã, test, cấu hình và bằng chứng khỏi bộ tài liệu; chỉ giữ chương này làm dấu vết quyết định (và bản lưu 0.2 để tra cứu).

### 18.1 Lý do

| # | Lý do |
|---|---|
| 1 | **Không rẻ hơn.** LLM vẫn phải viết lại toàn bộ bản dịch, nên bản thô chỉ làm đầu vào tăng: phân tích ở bản 0.2 cho thấy đắt hơn dịch trực tiếp khoảng 14%. Chỉ rẻ hơn nếu LLM *chỉ sửa đoạn cần sửa* và tỷ lệ giữ nguyên rất cao — chưa có bằng chứng thực tế nào. |
| 2 | **Tiết kiệm nhỏ.** Kịch bản lạc quan nhất cũng chỉ giảm vài chục xu mỗi cuốn 300 trang, trong khi pool có tầng miễn phí vốn đã đưa chi phí LLM của closed beta về gần 0 (§17). |
| 3 | **Không giảm số lời gọi.** Vẫn một lời gọi mỗi segment, nên không giúp hạn mức "request/ngày" — thứ khan hiếm nhất của free tier (§17.10). |
| 4 | **Điều khoản.** Endpoint miễn phí của NVIDIA chỉ để thử nghiệm: cấm dùng production và dữ liệu cá nhân; muốn dùng thật phải tự chạy trọng số mở trên GPU thuê, thêm chi phí và việc vận hành (§17.3 dòng 6). |
| 5 | **Chất lượng chưa kiểm chứng.** Điểm công bố là benchmark câu đơn tổng hợp, không nói gì về văn nói bài giảng hay thuật ngữ chuyên ngành; model NMT không làm theo chỉ dẫn nên glossary và văn phong vẫn phải áp ở bước LLM. |
| 6 | **Độ phức tạp không tương xứng.** Thêm một nhà cung cấp, một cầu dao, một prompt, hai chế độ hiệu đính, bốn cột dữ liệu và một thí nghiệm riêng, đổi lấy lợi ích tối đa vài chục phần trăm trên một mức duy nhất. |

Mức `full_translation` vì vậy chỉ dùng **dịch trực tiếp bằng LLM (P9)** qua pool, như mọi mức khác.

### 18.2 Đã xoá khỏi bộ tài liệu

| Hạng mục | Ghi chú |
|---|---|
| Prompt hiệu đính P11 | Số hiệu P11 **bị loại, không dùng lại** (tránh nhầm với bản 0.2); `tools/validate_spec.py` chặn nếu tệp quay lại |
| Profile `mt_draft`, loại model dịch máy trong pool, module `mt_draft` và test của nó | Không còn deployment loại `mt` |
| JSON Schema `postedit_chunk` | Không còn hợp đồng dữ liệu nào cho bản thô |
| Cột `translation_items.draft_*` và `edit_level`, cờ `draft_mt_mode`, trường `draft_mt`/`postedit_mode`, metric `visynth_draft_*` | CSDL, cấu hình, công thức và giám sát đều đã sạch |
| Công cụ ước tính so sánh và bảng chi phí ba chế độ | Không còn giá trị sử dụng khi phương án đã bị loại; bảng chi phí dịch trực tiếp vẫn ở §15 |

Bản lưu đầy đủ của thiết kế cũ nằm ở gói 0.2 (`visynth-spec-v0.2-with-mt-draft.zip`). Muốn xem lại thì đọc bản lưu, **không** khôi phục vào spec đang dùng.

### 18.3 Điều kiện để xem xét lại

Quay lại chỉ khi **một** trong các điều kiện sau thành hiện thực:

1. Chạy được trọng số mở với chi phí cố định thấp và điều khoản rõ ràng, hoặc nhà cung cấp cho dùng production.
2. Đo trên ít nhất 3 tài liệu thật: tỷ lệ giữ nguyên bản thô từ 60% trở lên, chất lượng không thua dịch trực tiếp (từ 45% cặp ngang bằng hoặc tốt hơn khi chấm mù), chi phí hoặc thời gian LLM giảm từ 25%.
3. Khối lượng dịch đầy đủ đủ lớn để vài chục phần trăm tiết kiệm có ý nghĩa tài chính.

Khi đó dựng lại từ bản 0.2: bước phụ trong tác vụ `translate`, fallback từng đoạn, cầu dao riêng, prompt hiệu đính, test fuzz "không bao giờ chết".
