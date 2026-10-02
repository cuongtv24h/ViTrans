# Bộ golden set (SPEC §16.1)

Mỗi tài liệu là **một thư mục** `eval/golden/<doc_id>/`:

```
eval/golden/
├─ schema.json                 # JSON Schema cho meta.json (nguồn sự thật của định dạng)
├─ demo_lecture/meta.json      # ví dụ chạy thử (kind: "smoke"), không thuộc bộ 24
└─ <doc_id>/
   ├─ meta.json                # danh sách "phải phủ", trap facts, glossary, ngân sách độ dài
   ├─ reference.md             # (tuỳ chọn, ≥ 5 tài liệu) bản tham chiếu: Gemini Notebook hoặc người biên tập
   └─ nguồn gốc …              # tệp nguồn có thể nằm ở đây hoặc trỏ ra ngoài bằng source_path
```

Tài liệu nguồn **không** cần nằm trong repo nếu bản quyền không cho phép: `source_path` là đường dẫn tương đối
từ `meta.json`, có thể trỏ tới thư mục ngoài repo (ví dụ `../../../../tai-lieu/sach-chuong-3.pdf`).

## Định dạng `meta.json`

Đọc `schema.json` để có đủ ràng buộc; các trường quan trọng:

| Trường | Ý nghĩa |
|---|---|
| `kind` | Loại theo bảng §16.1: `lecture_transcript` (6), `book_chapter` (6), `article` (4), `pdf_scan` (3), `messy_docx` (3), `vietnamese_source` (2). `smoke` = ví dụ chạy thử, không tính vào 24 |
| `must_cover` | **Danh sách "phải phủ"**: 30–60 khái niệm cốt lõi do người làm xác lập. Mỗi mục có `concept` và tuỳ chọn `variants` (cách viết khác được coi là cùng khái niệm). `core: false` = chỉ theo dõi |
| `trap_facts` | ≥ 10 con số/ngày/tên để bắt lỗi số liệu. `accept` = dạng viết tương đương; `wrong_variants` = dạng sai (làm tròn, nhầm đơn vị) — thấy trong báo cáo là tính lỗi |
| `glossary` | Bảng thuật ngữ của tài liệu (`source_term`, `target_term`, `forbidden_variants`) để chấm nhất quán thuật ngữ |
| `target_words` | Ngân sách độ dài kỳ vọng (lấy từ `visynth estimate <nguồn> --level deep_synthesis`) |
| `reference_path` | Bản tham chiếu để chấm mù (§16.2) |
| `human_scores` | Điểm người chấm 1–5, xem `eval/rubric.md` |

## Thêm một tài liệu

```bash
mkdir -p eval/golden/<doc_id>
$EDITOR eval/golden/<doc_id>/meta.json          # theo schema.json
python eval/score.py --validate-golden          # kiểm schema + tệp nguồn + đếm độ phủ §16.1
```

`--validate-golden` báo cả số tài liệu từng loại so với mục tiêu §16.1, nên dùng nó để biết còn thiếu gì.

## Gợi ý cách lập danh sách "phải phủ" và trap facts

1. Đọc tài liệu một lượt, ghi ra các **khái niệm mà người đọc phải nắm** (không phải mọi danh từ riêng) — 30–60 mục.
2. Với mỗi khái niệm, ghi thêm các cách viết khác mà một bản dịch tốt có thể dùng (`variants`).
3. Lọc ra **mọi con số/ngày/tên riêng** quan trọng (≥ 10) làm `trap_facts`; thêm `wrong_variants` là các lỗi
   người dịch hay mắc (làm tròn, đổi đơn vị, nhầm năm).
4. Nếu có bản tham chiếu, để riêng thành `reference.md` — nó dùng cho chấm mù, không dùng cho chấm tự động.
