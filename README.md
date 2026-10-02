# ViSynth

Ứng dụng web **chuyển ngữ & tổng hợp tài liệu sang tiếng Việt bằng LLM** (tên tạm). Kho này chứa **bộ đặc tả** (trong [`docs/`](docs/)) và **mã sản phẩm** đang được dựng theo lộ trình M0→M4.

- **Đặc tả:** [`docs/SPEC.md`](docs/SPEC.md) — nguồn sự thật của sản phẩm. Không sửa trực tiếp; sửa nguồn trong `docs/tools/spec_src/` rồi chạy `python tools/build_spec.py`.
- **Lộ trình thi công:** [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md) — M0 (prototype CLI), M1 (backend), M2 (frontend), M3 (cổng A), M4 (cổng B).
- **Trạng thái hiện tại:** **M0 — W3 xong phần mã, đang chờ đo thật** (W1: khung + extract/segment/estimate;
  W2: pipeline P0–P8 trên `FakeLLMClient`, kiểm tra tất định D1–D9, `visynth run --demo`; W3: **LLM Pool thật** —
  `pool_config`, kho khoá mã hoá ngoài repo, `declare`, `pool models`, `probe`, `simulate`, chuyển dự phòng 429/401,
  sổ `llm_calls`; **217 test**, trong đó 26 test chốt định dạng HTTP của hai adapter qua máy chủ giả cục bộ).
  Việc còn lại của W3 cần **mạng tới nhà cung cấp** (sandbox phát triển không ra được Internet): chạy
  `pool models` → sửa id model → `pool probe` để đo hạn mức/`tokenizer_factor` → báo cáo `deep_synthesis` đầu tiên.

## Cấu trúc kho

```
docs/                    # bộ đặc tả: SPEC.md, BUILD_PLAN.md, schemas/, prompts/, db/schema.sql, api/openapi.yaml, reference/, tests/
apps/worker/visynth/     # mã pipeline (M0 chạy ở đây)
apps/api/                # FastAPI                        (M1)
apps/web/                # Next.js                        (M2)
packages/contracts/      # trỏ tới hợp đồng dữ liệu trong docs/ (xem README trong thư mục)
eval/                    # golden set, script chấm điểm   (M0-W4)
infra/                   # docker compose, Caddy, sao lưu  (M1)
tests/                   # test của mã sản phẩm
```

**Nguồn sự thật duy nhất:** schema (`docs/schemas/`), prompt (`docs/prompts/`), DDL (`docs/db/schema.sql`) và OpenAPI (`docs/api/openapi.yaml`) vẫn nằm trong bộ đặc tả. Mã sản phẩm đọc chúng qua biến môi trường (`VISYNTH_SCHEMAS_DIR`, `VISYNTH_PROMPTS_DIR`, …) với mặc định là `docs/…`; chỉ tách bản sao khi cần phát hành gói riêng.

## Chạy thử (M0)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

visynth extract  eval/fixtures/demo_lecture.txt            # bóc tách → JSON
visynth segment  eval/fixtures/demo_lecture.txt --mode map  # chia segment
visynth estimate eval/fixtures/demo_lecture.txt --level deep_synthesis

# Chạy trọn pipeline P0–P8 trên kịch bản giả (không tốn token; pool thật thuộc M0-W3)
visynth run --demo --out /tmp/bao-cao.md      # báo cáo Markdown + hạng chất lượng
visynth run --demo --json                     # kèm số liệu, sự kiện, cảnh báo
visynth run --demo --tamper number            # thử đường sửa lỗi P7 (number|glossary|fabricated|plan)
```

## Chạy thật với LLM Pool (M0-W3)

Một cách nhập khoá duy nhất — qua tệp `.env` **không bao giờ commit** (`.gitignore` đã chặn; `git status` phải sạch):

```bash
cp .env.example .env            # rồi dán khoá vào: GEMINI_API_KEY=…, NVIDIA_API_KEY=…, RESERVE_API_KEY=…
cp pool_config.example.json pool_config.json   # hạn mức/điểm chất lượng là số GIẢ ĐỊNH, sẽ đo lại

visynth pool validate --config pool_config.json          # kiểm tra schema + ràng buộc cấu trúc (không gọi mạng)
visynth pool simulate --docs 3                           # mô phỏng 5 kịch bản hạn mức, không gọi mạng
visynth pool models  --config pool_config.json           # liệt kê model THẬT của từng provider (không tốn token)
                                                         # → sửa models[].id cho khớp, rồi mới probe
visynth pool probe   --config pool_config.json --out eval/runs/probe.json   # đo thật: JSON, tiếng Việt, độ trễ, tokenizer_factor
                                                         # → dán số đo được vào limits/quality của pool_config.json

visynth run eval/fixtures/demo_lecture.txt --pool-config pool_config.json \
    --level deep_synthesis --out /tmp/bao-cao-that.md --ledger /tmp/llm_calls.jsonl
```

`declare` (khai báo nhà cung cấp/khoá + `risk_ack`) mặc định chỉ **xem trước**:

```bash
visynth pool declare --declaration docs/examples/pool_declaration.example.json \
    --config pool_config.json                 # dry-run: in ra nhóm/ cổng/ cờ rủi ro, KHÔNG in khoá
# thêm --store-secrets (lưu khoá mã hoá vào ~/.config/visynth/secrets.json) và --write (áp dụng vào pool_config.json)
```

Ba lệnh gọi mạng (`models`, `probe`, `run --pool-config`) cần chạy ở nơi **ra được** tới
`generativelanguage.googleapis.com` / `integrate.api.nvidia.com`; `validate` và `simulate` chạy được ở mọi nơi.
Bộ kiểm thử không cần mạng: `tests/test_pool_http.py` dựng máy chủ giả cục bộ mô phỏng đúng hình dạng phản hồi
của hai họ API để chốt định dạng request và ánh xạ lỗi (kể cả ca Google trả 400 cho khoá sai).

Khoá nằm ở một trong hai nơi, `pool_config` chỉ giữ `secret_ref` (`env:TÊN` hoặc `enc:id`):
`visynth pool keygen` sinh khoá chủ (một lần cho mỗi máy) để dùng kho mã hoá thay vì `.env`.
Mọi lời gọi được ghi vào `~/.local/state/visynth/llm_calls.jsonl` (deployment, tầng, `data_policy`, outcome,
token, độ trễ — **không** có nội dung tài liệu, không có khoá).

## Kiểm tra

```bash
make check          # ruff + pytest + kiểm tra bộ đặc tả
make test           # pytest tests/
make spec           # validate_spec.py + openapi-spec-validator trên docs/
```

## Nguyên tắc

1. **Hợp đồng trước, code sau** — mọi thay đổi schema/API đi kèm cập nhật `docs/` và `tools/validate_spec.py` phải xanh.
2. **Fake-first** — `FakeLLMClient` cho phép chạy cả pipeline không tốn token; LLM thật chỉ dùng khi đo.
3. **Tất định trước, LLM sau** — phần kiểm tra bằng code (trích đoạn, số liệu, thuật ngữ) có test từ ngày đầu.
4. **Không khoá trong log, bản sao lưu hay ảnh chụp màn hình.**
