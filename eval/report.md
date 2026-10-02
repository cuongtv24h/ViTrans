# Báo cáo M0-W4 — hạ tầng đo, bake-off, Lõi văn phong: quyết định đi tiếp / chỉnh hướng

> Điều kiện thoát W4 của `docs/BUILD_PLAN.md`: (1) hạ tầng đo §2.2 chạy được trên **≥ 5 tài liệu thật**;
> (2) `estimate` lệch **≤ 35 %** so chi phí thật sau khi hiệu chỉnh `pool_config`; (3) **Lõi văn phong thắng
> lõi trung tính** ở điểm Văn phong mà không làm hỏng chỉ số khác (hoặc bị loại).
> Báo cáo này tách rõ **phần đã chứng minh offline** và **phần chờ dữ liệu/khoá của chủ hệ thống** — không
> gộp hai loại bằng chứng làm một.

## 1. Trạng thái điều kiện thoát

| # | Điều kiện thoát | Trạng thái | Bằng chứng |
|---|---|---|---|
| 1 | Hạ tầng đo §2.2 chạy trên ≥ 5 tài liệu **thật** | 🔴 **chưa đủ** | `python eval/score.py --validate-golden` → 0/6, 0/6, 0/4, 0/3, 0/3, 0/2 (chỉ có `demo_lecture` loại `smoke` — KHÔNG tính vào §16.1). Cần chủ hệ thống cấp 24 tài liệu theo tỉ lệ §16.1; **5 tài liệu là mức tối thiểu để gỡ chặn** |
| 2 | `estimate` lệch ≤ 35 % so chi phí thật | 🔴 **chưa đo được** | Cần `visynth pool probe` thật ở máy có mạng + khoá (sandbox không có egress). Số hạn mức/model giữa các nguồn công khai **mâu thuẫn**, nên không chốt bằng suy đoán — `pool probe` + `pool apply-probe` đã sẵn sàng để nhập số đo |
| 3 | Lõi văn phong thắng lõi trung tính | 🔴 **chưa chứng minh** (cơ chế đã xong) | Chưa có nội dung lĩnh vực đầu (§19.9 bước 1–2 cần 3–10 tài liệu mẫu + 5–20 cặp tham chiếu do người làm). `visynth style compare` đã chạy được và **tự nói "chưa chứng minh được lợi ích"** khi chỉ số đo được không đổi — đúng tinh thần "lõi phải kiếm được chỗ của nó" |

**Kết luận W4:** toàn bộ **phần mã** của W4 đã xong và xanh (`make check` EXIT=0: **339 test** mã nguồn, 184 test
tài liệu, "TẤT CẢ ĐẠT"). Ba điều kiện thoát còn lại đều **phụ thuộc dữ liệu/khoá thật**, không phải việc viết
thêm mã. Quyết định: **đi tiếp M0→M1 với điều kiện**, không đổi hướng — nhưng **không tuyên bố chất lượng** cho
tới khi có ≥ 5 tài liệu thật.

## 2. Đã chứng minh offline (đo được, lặp lại được)

| Hạng mục | Kết quả | Lệnh |
|---|---|---|
| Bộ chấm §2.2 trên kịch bản giả | ĐẠT — `coverage_core = 1.0000`, `faithfulness = 1.0000`, `terminology = 1.0000`, `trap_fact_errors = 0`, `unresolved_numbers = 0`, `length_ratio = 0.9944` | `python eval/score.py --run eval/runs/demo/run.json --golden eval/golden/demo_lecture/meta.json` |
| Cổng chặn hồi quy §16.3 | Hoạt động: chặn khi coverage −0.03, faithfulness −0.02, trap fact mới, chi phí +20 %, p95 +30 % (mã thoát 1) | `python eval/compare_baseline.py --baseline … --candidate …` |
| Bake-off §16.4 | Chọn **cấu hình rẻ nhất đạt ngưỡng**; cảnh báo khi người viết/người kiểm **cùng họ model**; nhập điểm `probe` cạnh cấu hình | `python eval/bakeoff.py --configs pool_config.a.json pool_config.b.json --golden eval/golden --demo` |
| Vòng đời Lõi văn phong §19.3 | Bản `approved` bất biến; duyệt bị **chặn** khi còn quyết định mở / quy tắc AI chưa xem / lint có lỗi; job chỉ dùng bản đã duyệt | `visynth style …` (49 test: đối chiếu từng hàm với `docs/reference/style_core.py` + vòng đời duyệt + CLI) |
| Kỷ luật bằng chứng P12 §19.4 | ID lạ bị loại; quy tắc thiếu bằng chứng bị loại; ví dụ phải chép nguyên văn; `origin = ai`, `reviewed = false` bị ép; trường nhận diện không lấy từ model | `visynth style propose … --demo` (20 test) |
| Glossary chuẩn §19.5 | Chỉ mục `confirmed` vào bản phát hành; bản phát hành bất biến có `content_sha256`; mục `rejected` được giữ để không đề xuất lại; bằng chứng do code ghép từ `ctx_ids` | `visynth glossary …` (13 test) |

## 3. Việc còn lại — cần chủ hệ thống, kèm lệnh chính xác

1. **Chọn lĩnh vực đầu + tài liệu mẫu** (§19.9 bước 1): 3–10 tài liệu `.txt/.md` + 5–20 cặp dịch tham chiếu
   (`[{"source": …, "target": …}]`) + một tệp brief (lĩnh vực, người đọc, mục đích ≤ 3.000 ký tự). Cặp tham
   chiếu là phần giá trị nhất: **không có chúng, P12 chủ yếu sinh câu hỏi** (đúng như thiết kế).
   ```bash
   visynth pool models                       # tên model thật, sửa models[].id nếu cần
   visynth pool probe --out eval/runs/probe.json
   visynth pool apply-probe --config pool_config.json --probe eval/runs/probe.json [--write]
   visynth style propose --id <lĩnh-vực> --name-vi "…" --domain <lĩnh-vực> \
       --brief brief.md --samples thu_muc_mau --pairs cap_tham_chieu.json --pool-config pool_config.json
   visynth style decide  <id> --answer D01="…" --by ban
   visynth style confirm <id> --all --by ban
   visynth style approve <id> --by ban        # chỉ qua được khi sạch mọi lý do chặn
   visynth glossary propose --candidates ung_vien_*.json --contexts ngu_canh.json --pool-config pool_config.json
   visynth glossary status && visynth glossary approve … && visynth glossary publish --by ban
   ```
2. **Golden set thật** (§16.1): 6 transcript, 6 chương sách, 4 bài báo, 3 PDF scan, 3 DOCX lộn xộn,
   2 nguồn tiếng Việt — mỗi tài liệu một thư mục `eval/golden/<id>/` theo `golden/schema.json`
   (kiểm tra: `python eval/score.py --validate-golden`). **5 tài liệu là mức tối thiểu để gỡ chặn W4.**
3. **Trả lời câu hỏi §19.9 bước 7** trên golden set: `visynth style compare <id> --path …` cho từng tài liệu,
   rồi chấm điểm Văn phong bằng thang người (`eval/rubric.md`). Chỉ dùng lõi nếu **thắng** lõi trung tính mà
   không làm hỏng chỉ số khác.

## 4. Rủi ro đã biết

* `--demo` chỉ kiểm **đường ống**, không phản ánh chất lượng — mọi kết luận chất lượng phải chạy lại với LLM thật.
* Số hạn mức/giá của nhà cung cấp thay đổi và mâu thuẫn giữa các nguồn công khai ⇒ `pool_config` chỉ đúng sau
  `pool probe` thật; hằng số chi phí cần hiệu chỉnh lại định kỳ.
* Bằng chứng P12/P13 gắn với mẫu: lĩnh vực chỉ có vài tài liệu hoặc một diễn giả thì đề xuất phải thừa nhận
  giới hạn đó trong `risks_vi` (đã nằm trong prompt, nhưng người duyệt vẫn phải đọc).
* Chạy bake-off thật tốn token; chạy trên ≥ 5 tài liệu golden **trước**, rồi mở rộng dần.

## 5. Quyết định

**Đi tiếp** (không chỉnh hướng): W4 phần mã đóng tại đây; ba điều kiện còn lại là việc **cấp dữ liệu + khoá**
chứ không phải việc thiết kế lại. Việc kế tiếp khi có dữ liệu, theo thứ tự: (1) `pool probe` + hiệu chỉnh
`pool_config`; (2) 5 tài liệu golden đầu tiên + `score.py`; (3) `style propose` cho lĩnh vực đầu + `style compare`
trên golden; (4) bake-off thật để chốt cấu hình model.

*Cập nhật lần cuối: 2026-10-02, sau commit `5441efc`.*
