# Lộ trình build sản phẩm ViSynth — MVP đến cổng A

> Tài liệu này **chi tiết hoá §21.1 của [`SPEC.md`](SPEC.md)** thành kế hoạch thi công: dựng kho mã thế nào, mốc nào làm gì, tiêu chí thoát mốc, kiểm thử và việc cần chốt. Khi hai tài liệu lệch nhau, **SPEC.md là nguồn sự thật** — sửa SPEC.md trước, rồi cập nhật tài liệu này.
>
> Giả định nhân lực: **1–2 dev**; tổng MVP đến cổng A: **11–14 tuần**.

---

## 0. Đã đủ điều kiện bắt đầu chưa?

**Đủ. Không còn câu hỏi nào chặn việc bắt đầu M0.** Mọi câu hỏi ở §22 của SPEC.md đều đã có mặc định; các mặc định đều an toàn để đi tiếp và đổi muộn không cần viết lại kiến trúc.

**4 việc nên chốt sớm** (đều có mặc định, không chặn tuần 1):

| Việc (câu hỏi §22) | Cần trước | Nếu chưa chốt thì dùng | Đổi muộn ảnh hưởng gì |
|---|---|---|---|
| Q12, Q13 — danh sách nhà cung cấp/khoá thật và số dự án Gemini free | W3 của M0 (chạy pool) | 1 khoá Gemini free + 1 dự án trả phí | Kết quả bake-off và `limits` chỉ là giả định; phải đo lại |
| Q11 — curator đầu tiên, lĩnh vực đầu tiên, tài liệu mẫu + cặp tham chiếu | W4 của M0 (Lõi văn phong) | 5 tài liệu công khai cùng một lĩnh vực; chính bạn duyệt | Lõi văn phong phải làm lại ở lĩnh vực khác |
| Q4 — vùng/nhà cung cấp VPS | Trước M1 (hạ tầng) | Một vùng châu Á ngoài EEA/Anh; đo `mtr` từ Hà Nội | Chỉ là cấu hình, không đổi code |
| Q9 — dự án Gemini trả phí riêng + xác minh tham số lưu trữ của Interactions API | Trước M1 | Dự án riêng, tắt lưu trữ nếu có | Ảnh hưởng hồ sơ PDPL, không ảnh hưởng code |

Các câu còn lại (Q1 tên/pháp nhân, Q2 tín dụng, Q5 chia sẻ, Q6 thời hạn lưu, Q7 ngôn ngữ, Q8 thanh toán, Q10 `/takedown`, Q15 tư cách vận hành, Q16 riêng tư mặc định, Q18 ngưỡng lõi) chốt trước mốc tương ứng ở §3. **Q17 là câu duy nhất còn thiếu câu trả lời của bạn** (mục 6 trong bảng trả lời để trống) — không chặn build nhưng nên bổ sung.

---

## 1. Nguyên tắc thi công

1. **Hợp đồng trước, code sau.** `schemas/`, `api/openapi.yaml`, `db/schema.sql` là nguồn sự thật; mọi thay đổi đi kèm cập nhật spec và `tools/validate_spec.py` phải xanh.
2. **Fake-first.** Dựng `FakeLLMClient` trước adapter thật: toàn bộ pipeline, orchestration và test chạy được **không tốn token**; mọi test hiện có trong `tests/` là bản mẫu.
3. **Tất định trước, LLM sau.** Các bước tất định (trích đoạn, số liệu, thuật ngữ, chi phí, hàng đợi) có test từ ngày đầu; phần LLM chỉ được tin qua golden set.
4. **Không mở cổng công khai trước checklist §14.5.** Cổng A/B là *quyết định vận hành*, không phải cờ trong code.
5. **Không bao giờ để khoá trong log, bản sao lưu hay ảnh chụp màn hình.** Quy tắc này được kiểm tự động ngay từ M0.

---

## 2. Kho mã — cấu trúc đích và cách chuyển từ bộ spec

Cấu trúc theo §5.6 của SPEC.md (monorepo, tên tạm `visynth/`):

```
visynth/
├─ apps/web/            # SPA không bước build: ES modules + CSS thuần (M2)
├─ apps/api/            # FastAPI (M1)
├─ apps/worker/         # pipeline, parser sandbox, pool/ (Router + 2 adapter)
├─ packages/contracts/  # schemas/, openapi.yaml, kiểu TS sinh tự động
├─ prompts/             # *.md có front matter, semver
├─ db/                  # Alembic migrations; schema.sql làm bản gốc đối chiếu
├─ eval/                # golden set, rubric, script chấm điểm (§16)
├─ infra/               # docker-compose, Caddyfile, Dockerfile, sao lưu/khôi phục, runbook (§20)
└─ docs/                # bộ spec này (SPEC.md + BUILD_PLAN.md + runbooks)
```

Chuyển từ bộ spec sang kho sản phẩm — **không viết lại từ đầu**:

| Trong bộ spec | Sang kho sản phẩm | Cách dùng |
|---|---|---|
| `schemas/` (18 schema) | `packages/contracts/schemas/` | Nguồn sinh kiểu TS/Pydantic; validate mọi đầu ra LLM |
| `api/openapi.yaml` (61 đường dẫn) | `packages/contracts/openapi.yaml` | Sinh client; lint trong CI |
| `prompts/` (13 prompt + lõi trung tính) | `prompts/` | Nguồn sự thật của prompt; front matter giữ nguyên |
| `db/schema.sql` (46 bảng + hàm) | db/migrations (Alembic) | Migration baseline từ `schema.sql`; `pg_smoke.py` thành test hành vi CSDL |
| `reference/*.py` (15 module) | `apps/worker/` + `apps/api/` | **Port logic**, giữ nguyên test; đây là bản mẫu ngữ nghĩa, không phải code production |
| `tests/*.py` | `tests/` | Chạy lại trên code đã port; CI chặn nếu đỏ |
| `tools/validate_spec.py`, `build_spec.py` | `docs/tools/` (giữ nguyên) | Chạy trong CI của cả hai kho |

---

## 3. Các mốc

### M0 — Prototype CLI (2–3 tuần) — **cổng quyết định rẻ nhất**

**Trạng thái: W3 xong phần mã + kiểm chứng offline (còn `probe` thật); W4 xong toàn bộ phần MÃ (hạ tầng đo, bake-off, Lõi văn phong + glossary) — còn phần cần dữ liệu/khoá thật** (mã trong `apps/worker/visynth/`, test trong `tests/`, CI ở `.github/workflows/ci.yml`).
W3 đã có: **LLM Pool thật** (`visynth/pool/`: `model`/`state`/`router` port nguyên ngữ nghĩa từ
`docs/reference/llm_pool.py`, `secrets` + `registry` cho kho khoá mã hoá NGOÀI repo, `policy_io` port
`declare`, `adapters` HTTP thật cho `gemini_native` và `openai_compat`, `client` = `PooledLLMClient` với
chuyển dự phòng 429/5xx, cách ly khoá khi 401, hoãn khi `Wait` dài, đa dạng người viết/người kiểm, sổ
`llm_calls` JSONL không chứa nội dung), `probe` (§17.9), `simulate` (mô phỏng rời rạc), lệnh
`visynth pool {keygen,validate,models,preview,declare,probe,simulate}` và `visynth run <tệp> --pool-config …`.

Hai bổ sung sau khi soát lại với tài liệu nhà cung cấp (02/10/2026): (1) lệnh `pool models` liệt kê model thật
của từng provider — không tốn token, để chọn `models[].id` trước khi probe; (2) `classify_http` nhận diện
**Google trả 400 `INVALID_ARGUMENT` cho khoá sai** (`API_KEY_INVALID`) và xử như 401 (cách ly khoá) thay vì
`bad_request` — sửa đồng thời ở `visynth/llm/base.py`, `docs/reference/llm_pool.py`, SPEC §17.7 và cả hai bộ test.
`probe` cũng đo `tokenizer_factor` ngay từ phần JSON (trước đây chỉ đo khi bật `vi_write`).
W2 đã có: **pipeline P0–P8 chạy trọn trong một tiến trình** (`visynth/pipeline/`: profile → glossary → map →
consolidate → write → verify → repair → assemble) với kiểm tra schema bắt buộc ở mọi giai đoạn (`structured.py`,
registry `referencing` cho `$ref` chéo tệp), kiểm tra tất định D1–D9 (`checks/report.py`) cùng các port
`checks/{quotes,numbers,glossary,merge}.py`, khối `level_policy` khớp từng ký tự §7.3 (`levelpolicy.py`),
kịch bản giả E2E kèm 5 biến thể phá hoại để thử P7 (`visynth/eval/demo.py`), lệnh `visynth run --demo
[--tamper …]`. Kết quả trên tài liệu demo 315 từ: 12 đơn vị tri thức (8 cốt lõi), 4 mục, 9 khối, hạng A,
`coverage_core = 1.0`, 14 lời gọi LLM, không cảnh báo; **339 test** (trong đó 22 test pipeline E2E, 38 test
pool — gồm 4 test đối chiếu router/loader/secrets/`classify_http` với `docs/reference/` — và 26 test HTTP
`tests/test_pool_http.py` chốt định dạng request/ánh xạ lỗi qua máy chủ giả cục bộ).
W1 đã có: khung monorepo + CI; hợp đồng LLM client (`LLMClient`, `LLMRequest/Response`, `classify_http` port
từ spec, `FakeLLMClient`); bóc tách TXT/MD (`parse_blocks`, NFC, charset-normalizer, tiêu đề/list/code/quote/bảng)
và DOCX (style tiêu đề, danh sách, bảng → Markdown, bản cuối của tracked-changes, chống zip-bomb, cảnh báo
`images_ignored`); chia segment theo §6.3 (`visynth segment`); ước tính chi phí/tín dụng/thời gian
(`visynth estimate`, port từ `reference/estimator.py`); 98 test, trong đó có test **đối chiếu với bộ đặc tả**
(số của estimator và `classify_http` phải khớp `docs/reference/`, enum `paragraphKind` phải khớp schema).

Hai điều chỉnh nhỏ so với kế hoạch: (a) PDF (cần OCR/P10) để sang M1; (b) hợp đồng dữ liệu **không** sao chép
vào `packages/contracts` mà đọc thẳng từ `docs/` — xem README thư mục đó.

Mục tiêu: chứng minh chất lượng và chi phí trên dữ liệu thật **trước khi** xây giao diện. Kết quả là một CLI chạy trọn pipeline trên TXT/DOCX/PDF có chữ.

| Tuần | Việc | Kết quả |
|---|---|---|
| W1 ✅ | Skeleton monorepo; CI (ruff, pytest, openapi, validator spec); `FakeLLMClient`; hợp đồng LLM client; extract TXT + DOCX; `segment`; ước tính chi phí | CLI `extract` + `segment` + `estimate` chạy trên tài liệu mẫu; CI xanh; còn thiếu: quét giấy phép tự động (thêm trước M1) |
| W2 ✅ | `glossary` (P1, cổng duyệt theo quy tắc); `map`/`consolidate`/`write`/`verify`/`repair` (P2–P7); kiểm tra tất định D1–D9 (§6.13); render prompt theo §8.2; `assemble` (P8) | Pipeline P0–P8 chạy trọn trên `FakeLLMClient` (139 test lúc đó; cả bộ hiện **339 test** xanh, schema kiểm tra mọi giai đoạn, 4 biến thể phá hoại đi qua P7); **báo cáo từ LLM thật chuyển sang W3** (cần pool) |
| W3 🟡 (mã xong, chờ mạng) | Pool: nạp `pool_config` thật; `declare` + dry-run + `risk_ack`; `pool models`; `probe`; đo `limits`/`tokenizer_factor`; `defer_task`, cooldown, circuit | Đã kiểm chứng offline: định dạng request/ánh xạ lỗi của hai adapter qua máy chủ giả (`tests/test_pool_http.py`, 26 test), `llm_calls` không chứa khoá, ca Google 400 = lỗi khoá. **Còn lại (chặn bởi mạng):** sandbox phát triển không ra được tới nhà cung cấp → chủ hệ thống chạy ở máy mình: `pool models` → sửa id → `pool probe --out eval/runs/probe.json` → nhập `limits`/`quality`/`tokenizer_factor` → `run --pool-config …` để có báo cáo thật đầu tiên |
| W4 🟡 (4/4 phần mã) | **Đã xong hạ tầng đo:** `eval/score.py` chấm `run.json` theo §16.2/§2.2 (coverage_core, faithfulness, trap facts, thuật ngữ, độ dài, chi phí; kết luận ĐẠT/KHÔNG + JSON làm baseline), định dạng golden `eval/golden/schema.json` + `--validate-golden` đếm độ phủ §16.1, thang người `eval/rubric.md`, `visynth run --artifacts` (kèm `duration_ms`), `eval/compare_baseline.py` chặn hồi quy theo §16.3, `eval/bakeoff.py` chọn cấu hình rẻ nhất đạt ngưỡng (kèm kiểm đa dạng người viết/người kiểm và nhập điểm `probe`), `visynth pool apply-probe` nhập số đo có duyệt vào `pool_config`, **Lõi văn phong + glossary chuẩn §19**: `visynth.stylecore` (port cơ chế, kho có vòng đời duyệt) và `visynth style {init,propose,edit,confirm,lint,compile,decide,approve,bump,deprecate,status,compare}` — P12 đề xuất có kỷ luật bằng chứng do code cưỡng chế (ID lạ bị loại, ví dụ phải chép nguyên văn, mục AI bị ép `reviewed = false`, lint lỗi thì loại mục), `visynth run --style-core` chỉ dùng bản đã duyệt, lỗi vòng đời in gọn không traceback; `visynth.glossary` + `visynth glossary {propose,status,approve,reject,publish}` — P13 hài hoà nhiều tài liệu, bằng chứng do code ghép từ `ctx_ids`, bản phát hành bất biến chỉ chứa mục `confirmed`, mục `rejected` được giữ; ví dụ đầu-cuối `eval/runs/demo/`, 339 test. **Còn lại (cần dữ liệu/khoá của chủ hệ thống):** (2) tài liệu thật ≥ 5 + bản tham chiếu (P12/P13 đã sẵn sàng chạy thật); (3) chạy bake-off thật (cần khoá) + hiệu chỉnh hằng số chi phí; (4) NỘI DUNG lõi văn phong + glossary lĩnh vực đầu (§19.9 bước 1–2, 6–7) và so với lõi trung tính trên golden set | `eval/report.md` (đã có): quyết định **đi tiếp có điều kiện**; 3 điều kiện còn lại chờ dữ liệu/khoá; `pool_config` thật đã hiệu chỉnh |

Ba việc dời có chủ ý: (a) P9 `full_translation`, P10 OCR, P11, P12/P13 (Lõi văn phong) thuộc M1 như bảng dưới;
(b) PDF vẫn để sau (đúng dòng W1); (c) phạm vi giả ở W2 chỉ có 3 mức văn bản
(`detailed_synthesis`/`deep_synthesis`/`executive_brief`) — mức `full_translation` cần P9 nên chuyển sang M1.

**Thoát mốc (theo §21.1):** đạt ngưỡng §2.2 trên ≥ 5 tài liệu; `estimate` lệch ≤ 35% so với chi phí thật; `pool_config` thật nạp và `probe` đạt; lõi lĩnh vực đầu thắng lõi trung tính trên golden set hoặc bị loại.

**Nếu chậm:** cắt PDF scan (OCR để M1), cắt EPUB; chỉ giữ TXT/DOCX + 5 tài liệu golden. **Không cắt** kiểm tra tất định và đo chi phí thật.

### M1 — Backend lõi (3–4 tuần)

| Nhóm việc | Nội dung chính | Tham chiếu |
|---|---|---|
| CSDL & hàng đợi | Alembic từ `schema.sql`; task/lease/checkpoint/reclaim; hàm tín dụng, trần chi tiêu; `pg_smoke` chạy trên CI với PostgreSQL thật | §10, §12 |
| API & SSE | Đăng ký/đăng nhập, mã mời, upload, `POST /jobs`, `Idempotency-Key`, SSE + `Last-Event-ID`, ví tín dụng | §11, §13 |
| Pipeline production | Orchestration 11 giai đoạn, OCR cho PDF scan, parser sandbox (`--network none`), export PDF | §6, §12 |
| Pool | Router + 2 adapter nối vào hàm SQL (đặt chỗ nguyên tử), kho khoá mã hoá, Admin API, probe, kiểm toán riêng tư | §17 |
| Lõi văn phong & glossary | Curation runs (P12/P13), API duyệt, phát hành phiên bản | §19 |
| Hạ tầng | VPS: compose, Caddy, sao lưu mã hoá ra ngoài máy, giám sát ngoài máy, `POOL_MASTER_KEY` ngoài bản sao lưu | §20 |

**Thoát mốc:** AC-01…AC-26 (Phụ lục B) đạt trên staging; **diễn tập khôi phục đạt AC-25** (≤ 4 giờ); truy vấn kiểm toán "job `private` chạm nhóm không `no_training`" trả 0 hàng.

#### Tiến độ M1 (cập nhật 2026-10-03)

| Hạng mục | Trạng thái | Ghi chú |
|---|---|---|
| CSDL & hàng đợi | 🟢 lõi xong | Alembic `0001_baseline` (áp `docs/db/schema.sql`) + `0002_local_auth` + `0003_job_checkpoints`; `claim_tasks`/`reclaim_stale_tasks`/`defer_task`/hàm tín dụng/trần chi tiêu trên PostgreSQL thật. **Mỗi task = một giai đoạn**, sau mỗi giai đoạn worker ghi `job_checkpoints.state`; task bị thu hồi hoặc lỗi tạm thời chỉ chạy lại ĐÚNG giai đoạn đó (không gọi lại LLM cho phần đã xong, không trừ tiền hai lần). Ghi thêm `segments` + `knowledge_units` để trang đọc tra được trích dẫn. Test: `tests/test_db_schema.py` (8 ca) + `tests/test_api_m1.py` (10 ca, gồm cổng glossary và mức tắt) |
| API & SSE | 🟢 lõi xong | `/auth/*`, `/me`, `/me/consents`, `/credits`, `/invites/redeem`, `/documents*`, `/jobs*` (gồm `Idempotency-Key`, SSE + `Last-Event-ID`), `/glossaries*` (CRUD + CSV), `/reports*` (xem/xoá/xuất/flag/feedback), `/recipes`, `/style-cores`, `/takedown`, `/admin/*` |
| Pipeline production | 🟡 | `visynth worker` chạy 7 giai đoạn trên CSDL theo từng task + điểm lưu, ghi `job_stages`/`job_tasks`/`job_events`/`reports`/`segments`/`knowledge_units`; **cổng glossary đã nối CSDL**: tài liệu ≥ 2000 từ dừng ở `awaiting_glossary`, `POST /jobs/{id}/glossary/confirm` chạy tiếp (chạy lại giai đoạn `glossary` bằng danh sách đã duyệt, **không gọi lại P1**), quá `glossary_review_deadline` thì `sweep_glossary_gates` tự xác nhận mục `confidence ≥ 0.7`; **`full_translation` đã có** (`pipeline/translate.py`: mức này đi đường P0 → cổng glossary → **P9 `translate`**, không chạy map/write/verify/repair): chia segment ~4000 token, `first_use_terms` tính tất định theo thứ tự đoạn, kiểm tất định (pid đúng một lần, tỷ lệ độ dài ∈ [0.6, 2.2], số liệu thêm/mất, lint glossary, nghi chưa dịch), vi phạm ⇒ chạy lại segment **một lần** kèm phản hồi rồi mới đánh `flagged`, **>15% đoạn bị cờ ⇒ hạng C**; ghi `translation_items` + metrics vào `job_stages`; Markdown ghép theo `doc_sections` (`build_markdown`); pool hết chỗ ⇒ `defer_task` (không tính lần thử). **P10 OCR đã có**: PDF có lớp chữ đọc bằng pypdfium2 theo trang (bỏ header/footer lặp, nối từ ngắt dòng), trang quét xếp thành **cụm 10–15 trang, mỗi cụm một `job_tasks` (`OCR:12-26`)** chạy song song/chạy lại không làm lại, gộp vào `doc_paragraphs`/`doc_sections`; cụm hỏng chỉ ghi cảnh báo (`ocr_chunk_failed`) chứ không làm chết job; tài liệu quét báo giá trước bằng `SCANNED_WORDS_PER_PAGE`. **Parser sandbox đã có**: `visynth parse-server` (HTTP nội bộ, byte thô vào/JSON ra, trần 50 MB, timeout 120 giây) chạy trong service `parser` chỉ nối mạng `internal` (không tuyến ra internet), `read_only`, `cap_drop: [ALL]`, **không giữ khoá nào**; API gọi qua `VISYNTH_PARSER_URL`, parser chết ⇒ 503 `parser_unavailable` chứ không treo. **Export PDF + song ngữ đã có**: `visynth.render` (fpdf2 + font Unicode giữ dấu tiếng Việt) và `GET /reports/{id}/export?format=pdf|md|docx|html&bilingual=true` (song ngữ chỉ cho `full_translation`, ghép theo `pid`, luôn kèm thông báo AI §14.4). Test: `tests/test_ocr.py` (10 ca), `tests/test_parser_sandbox.py` (11 ca), `tests/test_export.py` (7 ca) |
| Pool | 🟢 lõi xong | Router + 2 adapter + kho khoá mã hoá (M0-W3); **Admin API HTTP** `/admin/pool/*` (cấu hình GET/PUT có `dry_run`, khai báo nhanh JSON/YAML, khoá, nhóm, deployment, `status`/`capacity`/`incidents`/`probe`) và cấu hình pool đọc/ghi **trong CSDL** (`pool/dbstore.py`: bản xuất che khoá `enc:<id>`, nhập lại khôi phục bí mật, `enc:<id>` lạ bị từ chối); worker chạy đường VPS bằng `--pool-from-db` (khoá chủ qua Docker secret) và **mỗi lời gọi ghi `llm_calls`** (`DbLedger`, không lưu nội dung). Test: `tests/test_api_admin_m1.py` (11 ca) + `tests/test_ops_m1.py` (4 ca) |
| Lõi văn phong & glossary | 🟢 lõi xong | Vòng đời + kỷ luật bằng chứng ở CLI (`visynth style …`, `visynth glossary …`) **và HTTP**: `/admin/style-cores*` (tạo, phiên bản, sửa nháp, submit, trả lời quyết định P12, duyệt/từ chối, propose, test-drive), `/admin/curation-runs`, `/admin/glossary-review` (hàng đợi keyset + quyết định), `/admin/glossaries/{id}/releases`, `/admin/glossaries/bootstrap`. P12/P13 chỉ **xếp hàng** cho worker `visynth curate` — API không giữ khoá nhà cung cấp; bản đã duyệt bất biến (khớp trigger `P0001`) |
| Hạ tầng | 🟡 mã xong, chưa chạy trên VPS thật | `infra/`: `docker-compose.yml` (caddy + api + worker ×2 + **parser sandbox** + postgres + scheduler + backup + egress), `Dockerfile` non-root, `Caddyfile`, `.env.example`, `egress/squid.conf` (danh sách tên miền cho phép — worker **không** có tuyến ra internet trực tiếp), `backup.sh`/`backup-loop.sh` (pg_dump -Fc → `age` → nhà cung cấp khác; loại trừ `doc_paragraphs`/`doc_sections`/`job_events`; từ chối chạy nếu thấy khoá chủ; dead man's switch), `restore.sh` (đo thời gian thật cho AC-25), `scheduler.sh`; việc định kỳ gọi `visynth ops reap|purge|health` (`worker/ops.py`). `POOL_MASTER_KEY` vào bằng Docker secret (`VISYNTH_POOL_MASTER_KEY_FILE`). **Chưa `docker compose up` lần nào** (sandbox không có mạng): bất biến cấu trúc được kiểm bằng `tests/test_infra.py` (13 ca) + `sh -n`; lần chạy đầu phải theo `infra/README.md` |

**Sai lệch có chủ ý (theo dõi để gỡ):** (0) cổng glossary lấy gợi ý từ P1 ngay trong giai đoạn `glossary` (đúng §6.4); khi người dùng xác nhận, giai đoạn này **chạy lại bằng danh sách đã duyệt và không gọi P1 lần nữa** — P1 chạy đúng một lần cho mỗi job; (a) xác thực M1 dùng email + mật khẩu (bảng `local_credentials`, scrypt) vì
SPEC §20.2 chọn Google OAuth + email OTP — sẽ thay ở M2; (b) `/me` trả thêm `jobs`/`limits` so với `Me` trong
OpenAPI (tiện cho giao diện, không phá hợp đồng); (c) ~~`/auth/*` chưa có trong `openapi.yaml`~~ **đã bổ sung**
(`/auth/register`, `/auth/login`, `/auth/logout` + schema `AuthSession`), cùng điểm cuối mới
`GET /documents/{documentId}/paragraphs` cho trích dẫn; (d) **giao diện M2 là SPA không bước build** (ES
modules + CSS thuần, phục vụ tĩnh) thay cho Next.js của SPEC §20.3 — lý do: trên VPS một tiến trình ít hơn,
không cần Node trong image, không có bước sinh mã để lệch với bản đã kiểm; chuyển sang Next.js khi cần SEO
hoặc trang giới thiệu công khai.

### M2 — Frontend MVP (3–4 tuần)

Wizard 3 bước (kèm chế độ riêng tư và đồng ý), cổng glossary, trang đọc có trích dẫn, xuất MD/DOCX/PDF, quản lý glossary, Admin (kèm `/admin/pool` — một cách nhập khoá duy nhất, §17.14), giao diện duyệt Lõi văn phong và hàng đợi thuật ngữ (§19.6).

**Thoát mốc:** 3–5 người thử hoàn thành F2 không cần hướng dẫn; curator duyệt trọn một lõi và một bản glossary từ đầu đến cuối.

#### Tiến độ M2 (cập nhật 2026-10-03)

| Hạng mục | Trạng thái | Ghi chú |
|---|---|---|
| Vỏ SPA + định tuyến | 🟢 mã xong | `apps/web/`: `index.html` + `app.js` (hash routing, phiên, điều hướng theo vai trò) + `lib/api.js`, `lib/md.js`, `lib/ui.js`. Không bước build, không phụ thuộc CDN, không dùng `localStorage` cho token. Phục vụ tĩnh bằng FastAPI (`StaticFiles` gắn sau cùng) khi chạy một tiến trình; trên VPS Caddy chuyển tiếp toàn bộ về `api` (cùng gốc ⇒ không CORS, cookie `HttpOnly` tự gửi, SSE không qua hai tầng proxy). Test: `tests/test_web_spa.py` (8 ca, gồm `node --check` và kiểm đồ thị import) |
| Wizard 3 bước (F2) | 🟢 mã xong | Tải tài liệu + xác nhận quyền → mức/glossary/lõi văn phong/chế độ riêng tư + **báo giá từ `POST /jobs/estimate`** → xác nhận và tạo job (`Idempotency-Key` sinh ở client, bấm lại không trừ tiền hai lần) |
| Cổng glossary | 🟢 mã xong | `GET/POST /jobs/{id}/glossary*`: bảng duyệt sửa được cách dịch, giữ nguyên/bỏ qua từng mục, lưu vào glossary cá nhân; hạn tự động xác nhận hiển thị rõ |
| Trang đọc có trích dẫn (F3) | 🟢 mã xong | `GET /reports/{id}` → mục/khối; mỗi khối hiện `pid` nguồn, bấm mở **nguyên văn đoạn nguồn** qua điểm cuối mới `GET /documents/{id}/paragraphs?pids=…`; đánh cờ đoạn sai (`POST /reports/{id}/blocks/{id}/flag`), chấm sao + nhận xét |
| Xuất MD/DOCX/PDF | 🟢 mã xong | Nút xuất cho `md|docx|pdf|html` và **bản song ngữ** (`?bilingual=true`, chỉ `full_translation`); tải tệp qua blob, giữ tên tệp; mọi tệp kèm thông báo AI |
| Quản lý glossary | 🟢 mã xong | Tạo glossary, thêm từng mục hoặc **dán nhanh nhiều dòng** (`nguồn = đích`), sửa/xoá, xuất CSV (UTF-8 BOM) |
| Admin `/admin/pool` (§17.14) | 🟢 mã xong | Deployment (bật/tắt, thử kiểm định), **nhập khoá duy nhất** (chọn nhóm + dán khoá; chỉ hiện `••••last4` và tham chiếu `enc:<id>`), khai báo nhanh YAML/JSON có `dry_run`, sự cố, dung lượng |
| Duyệt Lõi văn phong + hàng đợi thuật ngữ (§19.6) | 🟢 mã xong | Tab riêng: phiên bản (gửi → P12 chất vấn → trả lời → duyệt/từ chối, kèm chạy thử), hàng đợi `glossary-review` với quyết định duyệt/sửa-rồi-duyệt/loại |
| Vận hành | 🟢 mã xong | Trần chi tiêu, mã mời (tạo + hiện mã một lần), người dùng (vai trò/trạng thái), sử dụng 14 ngày, nhật ký kiểm toán |
| **Kiểm thử với người thật (thoát mốc)** | 🔴 chưa | Cần 3–5 người thử hoàn thành F2 không cần hướng dẫn; làm sau khi dựng VPS (theo chỉ đạo: kiểm chứng để sau) |
| Vá lệch hợp đồng SPA ↔ API | 🟢 xong (M3) | Trang Vận hành đọc sai tên trường (`max_cost_usd`/`spent_today_usd` thay vì `daily_spend_cap_usd`/`today_cost_usd`), coi `/admin/users`, `/admin/usage`, `/admin/invites` là `{items}` trong khi chúng trả MẢNG, đọc `target_type`/`actor_email` không tồn tại, và gửi `action: "confirm"` cho quyết định glossary (hợp đồng chỉ nhận `approve|reject|edit_approve`). Nay có helper `asItems` dùng chung và test đối chiếu hai đầu (`test_web_admin_contract.py`). |
| SPA trong image Docker | 🟢 xong (M3) | `pip install .` đặt gói vào `site-packages` nên đường dẫn SPA suy từ `__file__` sai ⇒ Caddy chuyển mọi thứ vào API nhưng API không có tệp tĩnh ⇒ **404 trang chủ trên VPS**. Nay `Settings` tự tìm theo danh sách ứng viên (kho mã → `/app/apps/web` → `apps/web` trong thư mục làm việc) và compose đặt `VISYNTH_WEB_DIR: /app/apps/web` tường minh. |

### M3 — Làm cứng và beta (2 tuần) → **cổng A**

Giới hạn tốc độ, bảo mật máy chủ (§20.4), quan sát và cảnh báo, trang pháp lý, diễn tập sự cố (hết hạn mức, khoá bị từ chối, nhà cung cấp sập, VPS chết).

Tiến độ (làm trong sandbox; phần cần VPS/token thật để lại):

- [x] Giới hạn tốc độ (§20.4.1): bộ đếm trong PostgreSQL dùng chung mọi tiến trình, chủ thể băm (không lưu IP/email thô), 429 `rate_limited` + `Retry-After`; hạn mức theo tuyến và theo tài khoản cho `/auth/login`; `rate_limit_gc` gọi trong `ops reap`; SPA đọc `retry_after_s` để hiện đếm ngược.
- [x] Header bảo mật ở tầng ứng dụng (`visynth_api/headers.py`) song song Caddy: CSP, nosniff, frame-deny, referrer, HSTS khi có TLS.
- [x] Quan sát và cảnh báo: `/healthz` trả `ok|degraded|critical` + mã cảnh báo (`?detail=1` kèm số), `ops health` thoát mã 3 khi có việc, ngưỡng ở `visynth.worker.alerts` (đổi bằng `VISYNTH_ALERT_*`); thêm tín hiệu `deployments_open`, `rate_limit_blocks_1h`, `oldest_waiting_min`, `tasks_stale_running`.
- [x] Bốn kịch bản diễn tập sự cố (`tests/test_drills_m3.py`) + sửa lỗi lộ ra khi diễn tập: trạng thái pool trên VPS nay ở CSDL (`DbPoolState` — trước đó cách ly khoá/cầu dao/cooldown chỉ nằm trong RAM); `DbLedger` không còn ném lỗi ra ngoài khi ghi sổ hỏng (đếm `failed` + log).
- [ ] Trang pháp lý (ToS/privacy) và phần §14.5 còn lại — chờ nội dung pháp lý thật.
- [ ] 20–30 người dùng mã mời, KPI §2.2 trên job thật — cần VPS.

**Thoát mốc:** checklist §14.5 (phần cổng A) đạt; KPI §2.2 trên job thật; 20–30 người dùng mã mời.

### M4 — Mở rộng (1–2 tuần) → **cổng B**

Đăng ký mở, Turnstile, hàng chờ; hoàn tất các mục pháp lý còn lại của §14.5 (luật sư PDPL, ToS/Privacy, hồ sơ chuyển dữ liệu ra nước ngoài).

**Thoát mốc:** chi phí/người dùng ổn định 2 tuần.

### Sau MVP (P2, P3)

Hỏi đáp có trích dẫn, đối chiếu nguồn song song, URL/YouTube, công thức người dùng, thanh toán (cổng C); nhiều tài liệu một báo cáo, API công khai.

---

## 4. Chất lượng, kiểm thử và CI

| Lớp kiểm tra | Công cụ | Chạy khi nào |
|---|---|---|
| Logic tất định (trích đoạn, số liệu, thuật ngữ, chi phí, prompt render) | `pytest` từ bộ spec, port dần sang code sản phẩm | Mỗi lần push |
| Hành vi CSDL (tín dụng, hàng đợi, đặt chỗ nguyên tử, bất biến lõi) | `pg_smoke.py --pg-dsn` với PostgreSQL trong CI | Mỗi lần push |
| Hợp đồng dữ liệu & API | `jsonschema`, `openapi-spec-validator`, enum chéo DDL/schema/OpenAPI trong `validate_spec.py` | Mỗi lần push |
| Chất lượng đầu ra (chấm điểm §2.2) | Bộ chạy golden set trong `eval/` | Trước mỗi cổng, mỗi lần đổi prompt/model/lõi |
| An toàn | Quét giấy phép (chặn AGPL — tránh PyMuPDF), test không lộ khoá trong log/response | Mỗi lần push |
| Sự cố | Diễn tập: chạm trần, hết hạn mức, khoá 401, nhà cung cấp sập, khôi phục VPS | Trước cổng A và B |

**Chặn merge:** `validate_spec.py` xanh · `pytest` xanh · OpenAPI hợp lệ · không khoá trong log · không giấy phép mới thuộc nhóm cấm.

---

## 5. Ngân sách giai đoạn đầu

| Hạng mục | Ước tính | Ghi chú |
|---|---|---|
| M0 (bake-off, golden subset, probe) | **5–15 USD** | Gần như toàn bộ chạy trên tầng miễn phí; phần trả phí chỉ cho vài tài liệu golden |
| VPS 8 GB + kho sao lưu + tên miền + email | 30–100 USD/tháng | §21.2 |
| Vận hành khi có người dùng thật | theo §13.2 và §21.2 | Có trần chi tiêu ba lớp ngay từ M1 |

---

## 6. Việc làm ngay

1. Tạo kho sản phẩm theo §2 và bật CI xanh (scaffold + chuyển `reference/`, `tests/`, `schemas/`, `prompts/`).
2. Chốt Q12/Q13 (khoá thật) và Q11 (tài liệu mẫu + curator lĩnh vực đầu).
3. Bắt đầu W1 của M0; kết thúc W4 bằng báo cáo `eval/` + quyết định đi tiếp.

---

## 7. Việc còn lại để hoàn thiện sản phẩm (danh sách kiểm)

*Trạng thái chốt: 2026-10-03, commit `d76eab3`. Mỗi việc ghi rõ **xong khi nào** để không có chuyện
"đã làm gần xong". Việc nào không cần VPS/khoá thật thì nằm ở nhóm A và làm được ngay.*

**Đã có đường chạy ngay (2026-10-03):** `tools/dev.py` dựng PostgreSQL nhúng (`pgserver`) + dữ liệu mẫu +
API + worker trong một lệnh (`up`), chạy trọn luồng qua HTTP (`flow`, 13 bước) và nhập khoá thật khi cần
(`keys`); không có khoá thì worker chạy chế độ giả nên **kiểm được đường ống mà không tốn token**. Nhân
dịp này vá một lỗi đầu-cuối thật của M2: trích dẫn trong khối là **id đơn vị tri thức** (`U-0001`) nhưng
trang đọc gửi thẳng vào `/documents/{id}/paragraphs?pids=` nên **mọi trích dẫn đều không mở được đoạn
nguồn**; nay API trả kèm `unit_sources` (đơn vị → `pid`), SPA đánh số trích dẫn `[1]` theo §13.4 và tra
đúng đoạn nguồn. Khoá lại bằng `test_end_to_end_job_runs_to_report` và `tests/js/cites_test.mjs`.
Các việc dưới đây vẫn nguyên.

### A. Làm được ngay trong sandbox (không cần VPS, không cần khoá thật)

| # | Việc | Vì sao còn thiếu | Xong khi |
|---|---|---|---|
| A1 | **Chỉ số Prometheus §15** — thêm `/metrics` (job, token, chi phí, lỗi, hàng đợi, OCR, hoàn tín dụng; nhóm pool: headroom, wait, circuit, spill, shadow, private violations) | Chưa có điểm cuối chỉ số nào; `/healthz` chỉ có vài con số tổng | `curl /metrics` trả đúng định dạng Prometheus, số liệu lấy từ CSDL/sổ `llm_calls`, có test; `visynth_pool_private_violations_total` luôn bằng 0 và tách riêng |
| A2 | **Cảnh báo theo TỶ LỆ của §15** — chi tiêu ≥ 80% trần ngày (khẩn), job hỏng > 10% trong 15 phút (khẩn), p95 chờ hàng đợi > 5 phút, 429 của LLM > 5%, hạng C > 20%/ngày, private violation > 0 (khẩn) | `alerts.py` mới có ngưỡng tuyệt đối trên số đếm | Mỗi điều kiện có cách tính + test + hiện ở `/healthz?detail=1` và `ops health`; có ca thử "hệ thống khoẻ thì im" |
| A3 | **2FA cho admin (TOTP + mã dự phòng)** | §20.4 yêu cầu 2FA cho Admin; mã chưa có gì | Bật TOTP, đăng nhập admin bắt buộc mã thứ hai, mã dự phòng dùng một lần, có test |
| A4 | **Hàng đợi xử lý takedown cho admin** | `POST /takedown` đã có (catalog) nhưng không có màn hình/điểm cuối để xem và xử lý, cũng không thông báo cho người gửi | Admin xem danh sách, đổi trạng thái, ghi `audit_log`; người gửi nhận xác nhận (khi đã có tầng email A10) |
| A5 | **Đóng bớt bề mặt tài liệu**: `/docs` (Swagger) và `/api/v1/openapi.json` | Đang mở công khai; trên VPS Caddy chuyển mọi thứ vào API nên chúng ra Internet | Tắt mặc định khi chạy thật (hoặc chỉ mở cho admin), có test |
| A6 | **CI chặt hơn**: (a) kiểm toán giấy phép (chặn AGPL, ví dụ PyMuPDF); (b) chạy test CSDL trên PostgreSQL của CI và **fail nếu ca CSDL bị BỎ QUA** thay vì im lặng xanh | §4/§14.5 yêu cầu; hiện CI chỉ chạy `pytest` và một ca bỏ qua sẽ không ai biết | Có bước CI mới; thử phá (bỏ `pgserver`) thì CI đỏ |
| A7 | **Bộ kiểm an toàn hệ thống** (§14.5 bullet 3): zip-bomb/tệp độc hại qua parser sandbox, zip-slip/XXE trong DOCX/EPUB, IDOR xuyên người dùng, XSS trong báo cáo/glossary, không lộ khoá trong log/phản hồi | Hiện mới rải rác trong vài tệp test, chưa phải bằng chứng chạy lại được cho cổng | Một tệp test an toàn, mỗi mục là một ca; đỏ khi cố tình phá |
| A8 | **Diễn tập "chạm trần chi tiêu"** (còn thiếu trong 4 kịch bản đã có) | §14.5 yêu cầu diễn tập chạm trần; hiện có logic `spend_cap_reached` + test CSDL nhưng chưa có diễn tập đầu-cuối | Thêm kịch bản: chạm trần → `POST /jobs` trả 503 → gỡ trần → job chạy lại được |
| A9 | **Nợ giao diện M2**: nhập CSV glossary trên UI (API `POST /glossaries/{id}/import` đã có), phân trang hàng đợi duyệt glossary (đang chỉ in con trỏ dạng chữ) | Người dùng phải gọi API thủ công | Nhập tệp trong trang glossary; có nút "trang sau" cho hàng đợi |
| A10 | **Tầng gửi email giao dịch** (OTP, xác minh email, mã mời, "job xong") | Chưa có mã gửi thư nào; đây là điều kiện của M4 và của thông báo A4 | Chọn nhà cung cấp, lớp gửi thư + mẫu tiếng Việt, chạy được ở chế độ thử (ghi ra tệp) khi chưa có khoá |

### B. Phải làm trên VPS / máy bạn (không làm được trong sandbox) — đường tới **cổng A**

| # | Việc | Xong khi |
|---|---|---|
| B1 | Dựng máy theo `infra/README.md` mục 0→7 (SSH chỉ khoá, `ufw` 80/443, cập nhật tự động, Docker, bí mật, `.env`, `docker compose up`, migration, tạo admin) | `https://<tên miền>` mở SPA, `/api/v1/healthz` trả 200, chứng chỉ TLS hợp lệ |
| B2 | **Nạp khoá thật + probe thật**: chạy `visynth pool probe`, điền số đo thật (rpm/rpd/tpm/tpd/concurrency) vào `pool_config`, `risk_ack`, `allowed_gates` | Mọi deployment bật đều đã qua probe; `ops health` không còn cảnh báo về pool |
| B3 | Bật giám sát ngoài máy (Uptime Kuma/healthchecks) + "dead man's switch" cho cron sao lưu | Cố tình tắt API → có tin báo trong vài phút; `/healthz?detail=1` đổi màu đúng khi có sự cố thật |
| B4 | **Diễn tập khôi phục ≤ 4 giờ (AC-25)**: backup → thuê VPS mới → khôi phục → `ops reap` → kiểm sổ tín dụng | Thời gian thật được ghi vào `infra/README.md`; cột mốc là ≤ 4 giờ |
| B5 | **Kiểm chứng chất lượng (đang hoãn theo chỉ đạo)**: golden set, bake-off/probe thật, hiệu chỉnh hằng số chi phí, chọn lĩnh vực đầu + Lõi văn phong theo lĩnh vực | `eval/report.md` có số thật; KPI §2.2 đo được trên job thật |
| B6 | **Beta 20–30 người bằng mã mời**; đo "hoàn thành F2 không cần hướng dẫn" (điều kiện thoát M2 chưa đạt) + điểm beta ≥ 4/5 | Có bảng phản hồi và danh sách lỗi ưu tiên sửa |
| B7 | Kiểm §14.5 phần cổng A: khoá trả phí thuộc dự án riêng + tách biến môi trường; kiểm toán "job `private` chạm nhóm không `no_training`" = **0 hàng** | Truy vấn kiểm toán trả 0; ảnh chụp màn hình/kết quả lưu vào `docs/` |

### C. M4 → **cổng B**

| # | Việc | Ghi chú |
|---|---|---|
| C1 | Đăng ký mở: xác minh email + **Turnstile** + giới hạn số đăng ký mới/ngày + hàng chờ khi chạm trần + **mặc định `private`** cho người mới | §13.4, §14.5 dòng B |
| C2 | **Google OAuth** (FR-01 xếp vào MVP; hiện dùng email + mật khẩu nội bộ làm chỗ đứng) | Cần cùng lúc với A10 |
| C3 | Pháp lý: ToS/Privacy/Cookie bản luật sư duyệt; hồ sơ PDPL (đánh giá tác động xử lý + chuyển dữ liệu ra nước ngoài); thoả thuận chuyển dữ liệu với từng bên nhận; phân loại rủi ro Luật AI; xác minh thời hạn lưu phía API LLM và tắt lưu nếu có | §20.8, §14.5. Trang `/legal` đã có bản nháp mô tả đúng luồng dữ liệu — cần luật sư soát và bổ sung tên/liên hệ bên kiểm soát |
| C4 | Đo chi phí/người dùng ổn định 2 tuần | Điều kiện thoát M4 |

### D. Sau MVP → **cổng C** (P2/P3)

| # | Việc |
|---|---|
| D1 | Hỏi đáp có trích dẫn; đối chiếu nguồn song song; URL/YouTube; công thức người dùng; nhiều tài liệu trong một báo cáo; API công khai |
| D2 | Thanh toán (cần pháp nhân + hoá đơn), WAL/PITR cho sổ tín dụng (RPO tính bằng phút), thêm máy thứ hai khi CPU > 80% kéo dài |

### E. Sai lệch có chủ ý đang giữ (ghi để không quên)

- SPA không bước build thay cho Next.js của §20.3 (đã ghi ở mục "sai lệch có chủ ý" của M2).
- Bỏ Redis: giới hạn tốc độ dùng PostgreSQL, SSE dùng tiến trình API (§5.2 cho phép).
- Ba việc M0 được hoãn theo chỉ đạo "kiểm chứng để sau": golden set thật, probe/bake-off thật, hiệu chỉnh hằng số chi phí — nay nằm ở B5.

*Cập nhật tài liệu này khi: đổi phạm vi mốc, đổi nhân lực, hoặc sau mỗi lần chấm golden set.*
