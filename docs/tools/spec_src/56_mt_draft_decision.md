
---

## 18. Quyết định: không đưa bản dịch thô của model dịch máy vào phạm vi

> Bạn đề xuất dùng `riva-translate-4b-instruct-v2` của NVIDIA tạo bản dịch thô cho LLM hiệu đính, với fallback sang LLM khi khâu này chết, và nói "nếu lợi ích không cao thì bỏ". Bản 0.2 của spec đã thiết kế đầy đủ khâu này (có mã và test). Sau khi tính chi phí và đọc điều khoản, **kết luận là lợi ích không cao nên bỏ khỏi phạm vi**. Chương này giữ lại bằng chứng và điều kiện để xem xét lại, để quyết định này không phải làm lại từ đầu.

### 18.1 Quyết định và lý do

| # | Lý do | Bằng chứng |
|---|---|---|
| 1 | **Không rẻ hơn.** Nếu LLM vẫn viết lại toàn bộ bản dịch thì tốn hơn dịch trực tiếp khoảng 14% (thêm bản thô vào đầu vào, đầu ra không giảm). Chỉ khi LLM *chỉ sửa đoạn cần sửa* mới rẻ hơn, và phải giữ nguyên từ khoảng 20% số đoạn trở lên | Bảng ở §18.3 |
| 2 | **Số tiền có thể tiết kiệm nhỏ.** Ngay cả giả định lạc quan (giữ nguyên 70% bản thô) cũng chỉ giảm khoảng một phần ba chi phí dịch, tức vài chục xu mỗi cuốn 300 trang theo giá khuyến mãi; và với pool dùng tầng miễn phí (§17) chi phí LLM của closed beta vốn đã có thể bằng 0 | §18.3, §17.12 |
| 3 | **Không giảm số lời gọi LLM.** Vẫn một lời gọi hiệu đính mỗi segment, nên không giúp gì cho hạn mức "request mỗi ngày" vốn là thứ khan hiếm của free tier | §17.10 |
| 4 | **Điều khoản.** Endpoint miễn phí của NVIDIA chỉ để thử nghiệm: không dùng trong production, cấm gửi dữ liệu cá nhân. Muốn dùng ở production phải tự chạy trọng số mở trên GPU thuê (VPS cá nhân thường không có GPU) hoặc mua gói: thêm chi phí và việc vận hành | §17.3 dòng 6, §18.2 |
| 5 | **Chất lượng chưa kiểm chứng và không áp được thuật ngữ.** Điểm công bố là benchmark câu đơn tổng hợp, không nói về văn nói của bài giảng hay thuật ngữ chuyên ngành; model NMT 4B không làm theo chỉ dẫn nên glossary và văn phong vẫn phải áp ở bước LLM | §18.2 |
| 6 | **Độ phức tạp không tương xứng.** Thêm một nhà cung cấp, một cầu dao, một prompt, hai chế độ hiệu đính, bốn cột dữ liệu và một thí nghiệm riêng, đổi lấy lợi ích tối đa vài chục phần trăm trên một mức duy nhất | Bản 0.2 |

Vì vậy mức dịch đầy đủ chỉ dùng **dịch trực tiếp bằng LLM (P9)** qua pool; mọi giá trị còn lại của pool (chuyển tầng, chờ, riêng tư, tận dụng free tier) vẫn áp dụng cho mức này như các mức khác.

### 18.2 Riva-Translate-4B-Instruct-v2: sự thật đã kiểm tra (để tham chiếu)

| Thuộc tính | Giá trị | Nguồn |
|---|---|---|
| Kiến trúc | Decoder-only, khoảng 4,18 tỷ tham số, chưng cất từ Mistral-NeMo-Minitron-8B | NVIDIA API reference |
| Ngôn ngữ | Tiếng Anh và 36 ngôn ngữ khác, **gồm tiếng Việt** (`en-vi`, `vi-en`); mọi cặp đều đi qua tiếng Anh | Model card |
| Ngữ cảnh | 8K token cho cả vào và ra | Model card |
| Giao diện | `system` = mã cặp ngôn ngữ; `user` = văn bản; hỗ trợ few-shot; mức câu và mức tài liệu | Model card |
| Điểm công bố, FLORES-101 `en→vi` | sacreBLEU 37,1; COMET-DA 0,70; XCOMET-XXL 0,89 (trung bình 36 ngôn ngữ: 30,36; 0,78; 0,90) | Model card |
| Giấy phép trọng số | NVIDIA Open Model License; trang API ghi "sẵn sàng cho mục đích thương mại và phi thương mại" | Model card, trang API |
| Endpoint miễn phí | build.nvidia.com theo **NVIDIA API Trial Terms** (chỉ thử nghiệm, không production, không dữ liệu cá nhân); hạn mức không công bố | Trang model, điều khoản |

> FLORES-101 là câu đơn, thể loại tổng hợp: điểm số này **không** cho biết model dịch văn nói của bài giảng hay thuật ngữ chuyên ngành tốt đến đâu.

### 18.3 Chi phí so sánh

LLM hiệu đính đọc nguồn **và** bản thô. Bảng dưới (90.000 từ, `estimate_translation()` trong `reference/estimator.py`, có test):

<!-- MT_COST_TABLE -->

Hoà vốn của chế độ "chỉ sửa đoạn cần sửa" nằm quanh mức giữ nguyên 20%; bản thô quá tệ (giữ dưới 20%) thì còn tốn hơn dịch thẳng.

### 18.4 Điều kiện để xem xét lại

Chỉ quay lại khi **một** trong các điều kiện sau thành hiện thực:

1. Có cách chạy trọng số mở với chi phí cố định thấp và điều khoản rõ ràng (GPU thuê hoặc serverless bạn đã kiểm tra), hoặc nhà cung cấp cấp quyền dùng production.
2. Đo trên ít nhất 3 tài liệu thật cho thấy tỷ lệ giữ nguyên bản thô từ 60% trở lên, chất lượng không thua dịch trực tiếp (chấm mù theo cặp, từ 45% cặp ngang bằng hoặc tốt hơn), và chi phí hoặc thời gian của LLM giảm từ 25%.
3. Khối lượng dịch đầy đủ lớn đến mức tiết kiệm vài chục phần trăm có ý nghĩa tài chính.

Khi đó lấy lại thiết kế từ bản 0.2 (`visynth-spec-v0.2-with-mt-draft.zip`): bước phụ trong task `translate`, fallback từng đoạn, cầu dao riêng, prompt hiệu đính, test fuzz "không bao giờ chết".

### 18.5 Đã bỏ gì, giữ gì

| | Nội dung |
|---|---|
| **Đã bỏ** | Prompt hiệu đính (số hiệu **P11 bị loại và không dùng lại** để khỏi nhầm với bản 0.2), profile `mt_draft`, loại model dịch máy trong pool, `schemas/postedit_chunk.schema.json`, các cột `translation_items.draft_*` và `edit_level`, cờ `draft_mt_mode`, trường `draft_mt` và `postedit_mode` của công thức, `reference/mt_draft.py` và test |
| **Giữ lại** | `estimate_translation()` (công cụ đánh giá) và bảng ở §18.3; mục NVIDIA API Trial Terms ở §17.3 (vẫn áp dụng nếu bạn dùng endpoint miễn phí của NVIDIA cho model chat trong pool); bản lưu 0.2 |
