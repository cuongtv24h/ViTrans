# Thang chấm và ngưỡng nghiệm thu (M0-W4)

Nguồn: SPEC §16.2 (chỉ số + thang người chấm), §2.2 (ngưỡng nghiệm thu MVP), §16.3 (chặn hồi quy).
Tệp này là bản làm việc: người chấm đọc ở đây, bộ chấm tự động đọc `score.py`.

## 1. Chỉ số tự động — `python eval/score.py`

| Chỉ số | Định nghĩa | Ngưỡng §2.2 |
|---|---|---|
| `coverage_core` | (số mục "phải phủ" cốt lõi đạt `yes` + 0.5 × `partial`) / tổng số mục cốt lõi. `yes` = có trong báo cáo **và** có khối trích dẫn nguồn chưa bị đánh cờ; `partial` = chỉ xuất hiện ở khối thiếu trích dẫn/bị đánh cờ | ≥ 0.90 (hạng A ≥ 0.95) |
| `faithfulness_rate` | khối chưa xoá có `verdict` `supported`/`partially_supported` **và** không còn lỗi cứng chưa giải quyết / tổng khối chưa xoá | ≥ 0.97 |
| `fabricated_remaining` | số khối còn sót có `verdict = fabricated` | = 0 |
| `terminology_consistency` | 1 − vi phạm lint glossary / số lần thuật ngữ xuất hiện trong **thân** báo cáo | ≥ 0.98 |
| `trap_facts_accuracy` | trap fact có trong báo cáo và không có biến thể sai | lỗi (`trap_fact_errors`) = 0 |
| `unresolved_numbers` | số/ngày trong báo cáo không tìm thấy trong nguồn mà chưa giải quyết | = 0 |
| `length_ratio` | số từ báo cáo / `target_words` | trong khoảng 0.80–1.25 (cảnh báo, không chặn) |
| `cost_usd` | token thật trong sổ `llm_calls` × giá của model | §2.2: 300 trang `deep_synthesis` ≤ 1.5 USD (giá khuyến mãi) |
| `seconds` | thời lượng job (từ artifact) | p50 ≤ 6 phút / 100 trang; p95 ≤ 20 phút / 400 trang |

Chỉ số nào thiếu dữ liệu (ví dụ chạy kịch bản giả không có sổ `llm_calls`) thì bộ chấm ghi `applicable: false`
chứ **không** tính là đạt — nên phải chạy thật mới có kết luận đầy đủ.

## 2. Chấm người (§16.2) — 1–5, hai người chấm độc lập

Chấm mù với bản tham chiếu (báo cáo Gemini Notebook hoặc bản người biên tập). Lệch ≥ 2 điểm thì hai người thảo luận
rồi chốt; ghi lại cả hai điểm gốc.

| Tiêu chí | 1 | 3 | 5 |
|---|---|---|---|
| **Đầy đủ** | thiếu nhiều ý cốt lõi | đủ ý chính, thiếu vài chi tiết | phủ đủ ý cốt lõi như danh sách "phải phủ", không thừa |
| **Chính xác** | có ý sai/số sai so với nguồn | đúng nhưng vài chỗ diễn giải xa nguồn | mọi số/ngày/tên đúng nguồn; phần diễn giải được tách bạch |
| **Văn phong tiếng Việt** | dịch máy, hán–việt nặng, câu lủng củng | đọc được, đôi chỗ thô | tự nhiên như người Việt viết cùng lĩnh vực, thuật ngữ chuẩn |
| **Cấu trúc** | khối rời rạc, mục lục vô nghĩa | mục rõ, vài chỗ lệch mạch | mạch đọc tốt, phân mục theo ý chứ không theo trang nguồn |
| **Hữu ích** | đọc xong vẫn phải quay lại nguồn | nắm được ý chính | đọc xong là dùng được; biết chỗ cần kiểm chứng |

Ngưỡng §2.2 cho phần người chấm: **≥ 60% cặp** đạt "ngang bằng hoặc tốt hơn" bản tham chiếu; điểm beta ≥ 4/5.

### Biểu mẫu ghi điểm (điền vào `meta.json` của tài liệu)

```json
"human_scores": {
  "2026-10-05_nguoicham-A": {"day_du": 4, "chinh_xac": 5, "van_phong": 3, "cau_truc": 4, "huu_ich": 4},
  "2026-10-05_nguoicham-B": {"day_du": 4, "chinh_xac": 4, "van_phong": 4, "cau_truc": 4, "huu_ich": 3}
}
```

## 3. Chặn hồi quy (§16.3)

Chạy tập con 6 tài liệu trước/sau khi đổi prompt, model, cấu hình pool hoặc Lõi văn phong. **Chặn phát hành** khi:

| Chỉ số | Ngưỡng chặn |
|---|---|
| `coverage_core` | giảm > 0.03 |
| `faithfulness_rate` | giảm > 0.02 |
| `trap_fact_errors` | tăng (bất kỳ lỗi mới) |
| `cost_usd` | tăng > 20% |
| `seconds_p95` | tăng > 30% |

Hằng số nằm trong `score.py` (`REGRESSION_LIMITS`) để bộ so baseline dùng lại; xem `eval/runs/` để lấy baseline.

## 4. Quy trình một vòng đánh giá

```bash
# 1) chạy từng tài liệu golden (thật: cần pool_config + khoá; thử: --demo)
visynth run <nguồn> --pool-config pool_config.json --level deep_synthesis \
    --artifacts eval/runs/2026-10-05/<doc_id>

# 2) chấm tự động
python eval/score.py --run eval/runs/2026-10-05/<doc_id>/run.json \
                     --golden eval/golden/<doc_id>/meta.json \
                     --out eval/runs/2026-10-05/<doc_id>/score.json \
                     --markdown eval/runs/2026-10-05/<doc_id>/score.md

# 3) chấm người: điền human_scores vào meta.json
# 4) kiểm tra bộ golden còn hợp lệ và đủ độ phủ §16.1
python eval/score.py --validate-golden
```
