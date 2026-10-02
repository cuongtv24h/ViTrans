# Ghi chú bài giảng: tổng hợp và kiểm chứng
*Tài liệu mẫu cho M0 — bản tổng hợp có trích dẫn nguồn*

> **Báo cáo do AI tổng hợp.** Hãy kiểm tra lại ở nguồn gốc trước khi dùng cho quyết định quan trọng. Đây là bản tổng hợp/đọc hiểu, không phải bản dịch nguyên văn.

**Nguồn:** demo_lecture · **Mức:** deep_synthesis · **Hạng chất lượng:** A

## Mục lục
- Tóm tắt điều hành
- Ba câu hỏi trước khi viết báo cáo
- Các mức đầu ra theo tỷ lệ độ dài
- Kiểm tra tất định và nguyên tắc kết luận

## Tóm tắt điều hành

Tài liệu này là ghi chú bài giảng do nhóm ViSynth tự viết để thử quy trình tổng hợp, không trích từ nguồn có bản quyền và cũng không nhằm dùng làm dẫn chứng học thuật cho bất kỳ kết luận nào.

<sub>Nguồn: [1]</sub>

Một bài giảng 90 phút thường lặp lại phần giới thiệu, phần nhắc lại, ví dụ và hỏi đáp, nên dịch nguyên văn buộc người đọc tự lọc bỏ; báo cáo tổng hợp giữ ý cốt lõi kèm trích dẫn nguồn.

<sub>Nguồn: [2]</sub>

## Ba câu hỏi trước khi viết báo cáo

Câu hỏi thứ nhất trước khi viết: đơn vị tri thức nào là cốt lõi và đơn vị nào chỉ là ví dụ minh hoạ?

<sub>Nguồn: [3]</sub>

Câu hỏi thứ hai: số liệu, ngày tháng và tên riêng có xuất hiện đúng như nguồn hay không, và có bị làm tròn?

<sub>Nguồn: [4]</sub>

Câu hỏi thứ ba: câu nào là ý của tác giả và câu nào là phần diễn giải do hệ thống tự thêm vào?

<sub>Nguồn: [5]</sub>

## Các mức đầu ra theo tỷ lệ độ dài

Tài liệu nêu bốn mức đầu ra kèm tỷ lệ độ dài: dịch đầy đủ khoảng 100% nguồn, tổng hợp chi tiết khoảng 35%, báo cáo chuyên sâu khoảng 12%, tóm lược điều hành khoảng 3%; mức đầu tiên dùng khi cần đọc từng ý, mức cuối chỉ giữ ý trung tâm.

<sub>Nguồn: [6]</sub>

## Kiểm tra tất định và nguyên tắc kết luận

Tài liệu gợi ý kiểm tra bằng code: mọi pid đầu vào phải xuất hiện đúng một lần ở đầu ra, không bị lặp lại.

<sub>Nguồn: [7]</sub>

Ba nhóm kiểm tra tất định thường dùng gồm: trích đoạn bằng chứng phải có thật trong nguồn, con số phải tìm thấy trong nguồn, và thuật ngữ phải khớp bảng đã duyệt.

<sub>Nguồn: [8]</sub>

Kết luận của tài liệu: giữ nguyên số liệu, không thêm kiến thức ngoài nguồn, và đánh cờ khi không chắc thay vì tự đoán.

<sub>Nguồn: [9]</sub>

## Bảng dữ kiện

| Dữ kiện | Giá trị | Nguồn |
|---|---|---|
| Bốn mức đầu ra theo tỷ lệ độ dài | 100%, 35%, 12%, 3% | P000010 |
| Ba nhóm kiểm tra tất định | — | P000013 |

## Phụ lục thuật ngữ

| Thuật ngữ nguồn | Cách dùng trong báo cáo | Số lần |
|---|---|---|
| pid | pid | 1 |
| pipeline | quy trình | 1 |

## Phạm vi & cách xử lý

Đây là báo cáo tổng hợp và phân tích tài liệu mẫu, KHÔNG phải bản dịch nguyên văn từng câu.

**Được giữ đầy đủ:** các ý cốt lõi về lý do tổng hợp thay vì dịch thô, ba câu hỏi phải trả lời, các mức đầu ra và các nhóm kiểm tra tất định.

**Được cô đọng hoặc lược bớt:** các ví dụ minh hoạ và phần lặp lại đã được rút gọn; mọi con số và thuật ngữ giữ nguyên theo nguồn.

**Lưu ý về độ tin cậy:** báo cáo do AI tạo, đã qua kiểm tra tất định và bước kiểm chứng. Thống kê lượt gọi: {"coverage_core":1.0,"faithfulness_rate":1.0,"unresolved_blocks":0,"blocks_total":9,"blocks_removed":0,"units_total":12,"units_core":8,"units_unverified":0,"grade":"A","source_words":315,"report_words":0,"units_supporting":1,"units_minor":3,"units_merged":0,"units_omitted":3,"omitted_by_reason":{"level_policy":3},"sections":4,"blocks_flagged":0,"paragraph_labels":{"core":13,"admin":1,"qa":3},"llm_calls":13,"llm_tokens_in":18812,"llm_tokens_out":3174} Hãy đối chiếu đoạn nguồn được trích dẫn trước khi dùng cho quyết định quan trọng.

## Nguồn trích dẫn

1. **P000002** — Tài liệu này do nhóm ViSynth tự viết để thử pipeline; không phải trích từ nguồn có bản quyền.
2. **P000004** — Một bài giảng 90 phút thường chứa rất nhiều phần lặp lại: giới thiệu, nhắc lại, ví dụ, hỏi đáp. Nếu dịch nguyên văn, người đọc phải tự bỏ qua những phần đó. Báo cáo tổng hợp giữ l…
3. **P000006** — Đơn vị tri thức nào là cốt lõi, đơn vị nào chỉ là ví dụ?
4. **P000007** — Số liệu, ngày tháng và tên riêng có xuất hiện đúng như nguồn không?
5. **P000008** — Câu nào là ý của tác giả, câu nào là diễn giải của hệ thống?
6. **P000010** — | Mức | Mục tiêu độ dài | Khi nào dùng | | Dịch đầy đủ | ~100% nguồn | Cần đọc từng ý | | Tổng hợp chi tiết | ~35% nguồn | Muốn nắm gần hết ý | | Báo cáo chuyên sâu | ~12% nguồn |…
7. **P000012** — # Ví dụ: mọi pid đầu vào phải xuất hiện đúng một lần ở đầu ra assert sorted(in_pids) == sorted(out_pids)
8. **P000013** — Ba nhóm kiểm tra tất định thường dùng:
9. **P000017** — Kết luận: giữ nguyên số liệu, không thêm kiến thức ngoài nguồn, và luôn có đường thoát an toàn khi không chắc (đánh cờ thay vì đoán).

