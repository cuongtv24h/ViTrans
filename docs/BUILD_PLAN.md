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
├─ apps/web/            # Next.js (M2)
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
| Pipeline production | Orchestration 11 giai đoạn, OCR cho PDF scan, parser sandbox (`--network none`) | §6, §12 |
| Pool | Router + 2 adapter nối vào hàm SQL (đặt chỗ nguyên tử), kho khoá mã hoá, Admin API, probe, kiểm toán riêng tư | §17 |
| Lõi văn phong & glossary | Curation runs (P12/P13), API duyệt, phát hành phiên bản | §19 |
| Hạ tầng | VPS: compose, Caddy, sao lưu mã hoá ra ngoài máy, giám sát ngoài máy, `POOL_MASTER_KEY` ngoài bản sao lưu | §20 |

**Thoát mốc:** AC-01…AC-26 (Phụ lục B) đạt trên staging; **diễn tập khôi phục đạt AC-25** (≤ 4 giờ); truy vấn kiểm toán "job `private` chạm nhóm không `no_training`" trả 0 hàng.

#### Tiến độ M1 (cập nhật 2026-10-02)

| Hạng mục | Trạng thái | Ghi chú |
|---|---|---|
| CSDL & hàng đợi | 🟡 phần lớn | Alembic `0001_baseline` (áp `docs/db/schema.sql`) + `0002_local_auth`; `claim_tasks`/`reclaim_stale_tasks`/`defer_task`/hàm tín dụng/trần chi tiêu đã chạy trên PostgreSQL thật; test `tests/test_db_schema.py` (8 ca) + `tests/test_api_m1.py` (7 ca). Còn thiếu: chạy lại job theo từng giai đoạn (hiện chạy lại cả pipeline khi task bị thu hồi) |
| API & SSE | 🟢 lõi xong | `/auth/*`, `/me`, `/me/consents`, `/credits`, `/invites/redeem`, `/documents*`, `/jobs*` (gồm `Idempotency-Key`, SSE + `Last-Event-ID`), `/glossaries*` (CRUD + CSV), `/reports*` (xem/xoá/xuất/flag/feedback), `/recipes`, `/style-cores`, `/takedown`, `/admin/*` |
| Pipeline production | 🟡 | `visynth worker` chạy 7 giai đoạn trên CSDL, ghi `job_stages`/`job_tasks`/`job_events`/`reports`; **còn thiếu**: OCR PDF (P10), parser sandbox `--network none`, `full_translation` (P9) |
| Pool | 🟡 | Router + 2 adapter + kho khoá mã hoá đã có (M0-W3) và dùng được qua `visynth worker --pool-config`; **còn thiếu**: nối sổ `llm_calls` vào CSDL mỗi lời gọi, Admin API HTTP cho `/admin/pool/*` |
| Lõi văn phong & glossary | 🟡 | Vòng đời + kỷ luật bằng chứng đã có ở CLI (`visynth style …`, `visynth glossary …`); **còn thiếu**: bề mặt HTTP `/admin/style-cores*` và `/admin/glossary-review` |
| Hạ tầng | 🔴 chưa | Khối VPS (compose, Caddy, sao lưu, giám sát) làm ngay trước khi lên staging |

**Sai lệch có chủ ý (theo dõi để gỡ):** (a) xác thực M1 dùng email + mật khẩu (bảng `local_credentials`, scrypt) vì
SPEC §20.2 chọn Google OAuth + email OTP — sẽ thay ở M2; (b) `/me` trả thêm `jobs`/`limits` so với `Me` trong
OpenAPI (tiện cho giao diện, không phá hợp đồng); (c) `/auth/*` chưa có trong `openapi.yaml`, cần bổ sung trước M2.

### M2 — Frontend MVP (3–4 tuần)

Wizard 3 bước (kèm chế độ riêng tư và đồng ý), cổng glossary, trang đọc có trích dẫn, xuất MD/DOCX/PDF, quản lý glossary, Admin (kèm `/admin/pool` — một cách nhập khoá duy nhất, §17.14), giao diện duyệt Lõi văn phong và hàng đợi thuật ngữ (§19.6).

**Thoát mốc:** 3–5 người thử hoàn thành F2 không cần hướng dẫn; curator duyệt trọn một lõi và một bản glossary từ đầu đến cuối.

### M3 — Làm cứng và beta (2 tuần) → **cổng A**

Giới hạn tốc độ, bảo mật máy chủ (§20.4), quan sát và cảnh báo, trang pháp lý, diễn tập sự cố (hết hạn mức, khoá bị từ chối, nhà cung cấp sập, VPS chết).

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

*Cập nhật tài liệu này khi: đổi phạm vi mốc, đổi nhân lực, hoặc sau mỗi lần chấm golden set.*
