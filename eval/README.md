# `eval` — đo chất lượng (M0-W4)

Chưa có bộ golden set thật. Kế hoạch (SPEC §16):

| Mục | Nội dung |
|---|---|
| `fixtures/` | Tài liệu mẫu để chạy thử (hiện có `demo_lecture.txt`, do nhóm tự viết) |
| `golden/` | Bộ golden set ≥ 24 tài liệu theo §16.1 (6 transcript bài giảng, 6 chương sách, 4 bài báo, 3 PDF scan, 3 DOCX/EPUB lộn xộn, 2 tài liệu tiếng Việt) — **cần bạn cung cấp/tự chọn** |
| `rubric.md` | Thang chấm §16.2 và các chỉ số §2.2 (`coverage_core`, `faithfulness_rate`, thuật ngữ, số liệu, thời gian, chi phí) |
| `runs/` | Kết quả từng lần chấm, có ngày và phiên bản prompt/model; dùng làm baseline để so |
| `demo_report.md` | Báo cáo sinh bởi `visynth run --demo` (kịch bản giả M0-W2) trên tài liệu demo — chỉ để kiểm tra luồng, **không phải** kết quả chất lượng |

Điều kiện thoát M0 (W4): đạt ngưỡng §2.2 trên ≥ 5 tài liệu, `estimate` lệch ≤ 35% so với chi phí thật,
và lõi văn phong lĩnh vực đầu tiên thắng lõi trung tính trên golden set (hoặc bị loại).
