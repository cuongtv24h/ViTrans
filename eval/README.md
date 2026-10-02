# `eval` — đo chất lượng (M0-W4)

Bộ đo chạy **hoàn toàn offline**: `score.py` chấm các artifact của `visynth run`, không gọi mạng, không tốn token.
Chạy thật (có LLM) hay chạy kịch bản giả (`--demo`) đều cho cùng định dạng đầu ra.

| Đường dẫn | Nội dung |
|---|---|
| `golden/` | Bộ golden set (§16.1): mỗi tài liệu một thư mục có `meta.json` (danh sách "phải phủ", trap facts, glossary, ngân sách độ dài). Định dạng ở `golden/schema.json` + `golden/README.md` |
| `golden/demo_lecture/` | Ví dụ chạy thử (`kind: "smoke"`) trên tài liệu demo — không thuộc bộ 24 |
| `score.py` | Bộ chấm tự động §16.2/§2.2: `coverage_core`, `faithfulness_rate`, `fabricated_remaining`, `terminology_consistency`, trap facts, `unresolved_numbers`, `length_ratio`, `cost_usd` → JSON + Markdown kèm kết luận ĐẠT/KHÔNG |
| `rubric.md` | Thang chấm người 1–5 (§16.2), ngưỡng §2.2 và hằng số chặn hồi quy §16.3 |
| `compare_baseline.py` | So hai lần chạy theo §16.3 và **chặn** khi coverage −0.03, faithfulness −0.02, trap fact mới, chi phí +20%, p95 thời gian +30% (mã thoát 1 khi bị chặn) |
| `runs/<ngày>/<doc_id>/` | Kết quả từng lần: `run.json` (artifact của pipeline), `report.md`, `score.json`, `score.md` — dùng làm baseline để so |
| `fixtures/demo_lecture.txt` | Tài liệu mẫu do nhóm tự viết (kiểm tra luồng, **không phải** kết quả chất lượng) |
| `demo_report.md` | Báo cáo sinh bởi `visynth run --demo` — chỉ để xem luồng |

## Chạy một vòng chấm

```bash
# 1) chạy pipeline và ghi artifact (thêm --pool-config … để chạy LLM thật)
visynth run eval/fixtures/demo_lecture.txt --demo --level deep_synthesis --artifacts eval/runs/demo

# 2) chấm tự động
python eval/score.py --run eval/runs/demo/run.json --golden eval/golden/demo_lecture/meta.json \
                     --out eval/runs/demo/score.json --markdown eval/runs/demo/score.md

# 3) kiểm tra cả bộ golden (schema, tệp nguồn, độ phủ §16.1)
python eval/score.py --validate-golden
```

So với baseline trước khi phát hành thay đổi prompt/model/pool (§16.3):

```bash
python eval/compare_baseline.py --baseline eval/runs/2026-10-05 --candidate eval/runs/2026-11-01 \
    --markdown eval/runs/so-sanh.md        # mã thoát 1 = có chỉ số chặn
```

Kết quả mẫu (tài liệu demo, kịch bản giả): `coverage_core 1.00`, `faithfulness 1.00`, trap facts 0 lỗi,
`length_ratio 0.99` — chi phí **không áp dụng** vì kịch bản giả không có sổ `llm_calls`.

## Điều kiện thoát W4 (M0)

Đạt ngưỡng §2.2 trên ≥ 5 tài liệu thật, `estimate` lệch ≤ 35% so với chi phí thật, và Lõi văn phong lĩnh vực
đầu tiên thắng lõi trung tính trên golden set (hoặc bị loại). Còn thiếu: **tài liệu thật + khoá thật** (§16.1
cần 6 transcript, 6 chương sách, 4 bài báo, 3 PDF scan, 3 DOCX lộn xộn, 2 tài liệu tiếng Việt — hiện có 1 mục `smoke`).
