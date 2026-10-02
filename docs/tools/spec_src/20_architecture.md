
---

## 5. Kiến trúc và công nghệ

### 5.1 Sơ đồ tổng thể

```
Internet ──▶ (tuỳ chọn) Cloudflare: ẩn IP máy chủ, WAF, Turnstile
                 │ HTTPS
┌────────────────▼──────────── MỘT VPS cá nhân ở nước ngoài (docker compose, §20) ─────────────┐
│  Caddy: TLS tự động, giới hạn thô                                                            │
│    ├─▶ web (Next.js, SSR + i18n)                                                             │
│    └─▶ api (FastAPI) ── authz, tín dụng, job, admin, SSE ──┬──▶ PostgreSQL ◀── trạng thái    │
│                                                            │    dữ liệu, hàng đợi task,      │
│                                                            │    pool (bucket, lease), Lõi    │
│                                                            └──▶ Redis (tuỳ chọn: giới hạn    │
│                                                                 tốc độ API, pub/sub SSE)     │
│  worker ×N (pipeline §6) ── claim_tasks ──▶ PostgreSQL                                       │
│     ├─ LLM Pool (thư viện trong worker, §17): chọn → đặt chỗ → gọi → ghi nhận ───────────────┼──▶ Gemini API
│     ├─ khâu bản dịch thô (tuỳ chọn, §18) ────────────────────────────────────────────────────┼──▶ NVIDIA Riva Translate
│     └─ parser sandbox: container --network none, tạo theo yêu cầu                            │──▶ Nhà cung cấp chuẩn OpenAI ×N
│  cron: pg_dump mã hoá ──▶ kho đối tượng của nhà cung cấp KHÁC (sao lưu ngoài máy)            │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

Trạng thái bền (hạn mức LLM, lease, hàng đợi task, bản phát hành glossary, phiên bản Lõi văn phong) nằm ở **PostgreSQL**; Redis chỉ giữ thứ tạm (giới hạn tốc độ API, thông báo SSE) và có thể bỏ trên một VPS nhỏ.

### 5.2 Lựa chọn công nghệ

| Tầng | Lựa chọn | Lý do | Phương án thay thế |
|---|---|---|---|
| Web | Next.js (App Router) + TypeScript + Tailwind + `next-intl` (vi/en) | Trang giới thiệu SSR, UI phong phú, i18n | Remix, SvelteKit |
| API | FastAPI (Python ≥ 3.12) + Pydantic v2 + SQLAlchemy 2 + Alembic | Python có hệ sinh thái đọc file/NLP tốt nhất; Pydantic sinh JSON Schema; SDK `google-genai` | NestJS + dịch vụ Python riêng cho parser |
| Worker | Tiến trình Python, nhận việc bằng hàm SQL `claim_tasks` (hàng đợi trong PostgreSQL) | Không thêm hạ tầng; giao dịch cùng dữ liệu; `SKIP LOCKED` + giới hạn theo job đã có test | Celery/Arq/Temporal khi quy mô lớn hoặc cần workflow phức tạp |
| CSDL | PostgreSQL ≥ 16 | JSONB, giao dịch, `SKIP LOCKED` | — |
| Redis (tuỳ chọn) | Giới hạn tốc độ API, pub/sub cho SSE. Hạn mức LLM và hàng đợi KHÔNG phụ thuộc Redis (nằm ở PostgreSQL, §17.6) | Nhẹ; trên một VPS có thể bỏ | `LISTEN/NOTIFY` + bảng giới hạn tốc độ nếu muốn chỉ một CSDL |
| Lưu trữ file | Mặc định thư mục trên đĩa VPS, mã hoá từng tệp ở tầng ứng dụng (khoá dẫn xuất từ khoá chủ); tuỳ chọn S3-tương-thích khi cần tách khỏi máy | Một máy, không thêm dịch vụ; file gốc chỉ giữ 14 ngày và KHÔNG nằm trong bản sao lưu | R2/S3/MinIO/GCS |
| Xác thực | Google OAuth + email OTP (Auth.js hoặc dịch vụ quản lý) | Ít ma sát | Supabase Auth, Clerk |
| LLM | **Pool** (§17): Gemini (`gemini-3.8-flash` trả phí làm chỗ dựa) + các nhà cung cấp chuẩn OpenAI Chat Completions + nhóm miễn phí theo cổng triển khai | Giảm chi phí và tăng độ sẵn sàng; chất lượng được kiểm bằng bài kiểm định và bake-off (§16.4, §17.9) | Gateway có sẵn (LiteLLM...) làm tầng truyền tải, xem §17.13 |
| Đọc PDF (chữ) | `pypdfium2` (Apache-2.0/BSD-3), `pdfplumber` (MIT), `pypdf` (BSD-3) | **Giấy phép cho phép SaaS đóng mã** | **Tránh PyMuPDF**: AGPL-3.0, dùng trong SaaS phải mở mã hoặc mua giấy phép thương mại Artifex |
| DOCX / DOC | `python-docx` (MIT); `.doc` qua LibreOffice headless trong sandbox | | Pandoc (tiến trình riêng) |
| EPUB | tự đọc bằng `zipfile` + `lxml` (OPF/spine) | Tránh phụ thuộc thư viện copyleft | |
| Phụ đề | tự phân tích SRT/VTT (đơn giản) | | |
| Mã hoá ký tự | `charset-normalizer` | | |
| Phát hiện ngôn ngữ | `lingua-py` hoặc fastText `lid` + xác nhận ở P0 | | |
| Xuất PDF | HTML → PDF (Playwright/Chromium hoặc WeasyPrint), **nhúng font hỗ trợ tiếng Việt** (Be Vietnam Pro / Noto Serif) | Dấu tiếng Việt hiển thị đúng | |
| Xuất DOCX | `python-docx` | | |
| Quan sát | Nhẹ cho một VPS: log JSON + giám sát ngoài máy (Uptime Kuma hoặc healthchecks.io) + node exporter; Sentry (đã lọc nội dung). Prometheus/Grafana khi cần | Giám sát phải sống ngoài máy cần giám sát | OpenTelemetry |
| Triển khai | Docker compose trên **một VPS cá nhân ở nước ngoài** (§20) | Đơn giản, rẻ; điểm lỗi duy nhất được giảm nhẹ bằng sao lưu ngoài máy và runbook | Máy thứ hai khi cần (tách worker); Kubernetes chỉ khi thật sự cần |

**Kiểm toán giấy phép (PHẢI):** CI chạy `pip-licenses` và `license-checker`, chặn AGPL/GPL (trừ khi chạy như tiến trình riêng, không liên kết). Đã biết rõ trường hợp PyMuPDF (AGPL-3.0 hoặc giấy phép thương mại).

### 5.3 LLM Gateway và Pool

Mọi lời gọi LLM đi qua **một** giao diện; không module nào gọi SDK trực tiếp. Giao diện che sự khác biệt giữa nhà cung cấp; việc chọn nhà cung cấp, khoá và model nằm hoàn toàn trong **LLM Pool** (§17). Pipeline chỉ nói "tôi cần profile `writer` cho job riêng tư".

```python
class LLMClient(Protocol):
    def generate(self, *, prompt_id: str, version: str, system: str, user: str,
                 schema: dict | None,            # JSON Schema đầy đủ (kiểm tra phía server)
                 profile: str,                   # fast | writer | verifier | ocr | curator
                 thinking: str,                  # low | medium | high; pool đổi sang tham số của từng nhà cung cấp
                 max_output_tokens: int,
                 privacy_class: str,             # standard | private (lấy từ job): quyết định nhóm hạn mức nào được dùng
                 priority: str = "normal",       # high (tương tác) | normal | low (curation, kiểm định)
                 avoid_groups: frozenset = frozenset(),   # đa dạng hoá: người kiểm tránh nhóm đã dùng cho người viết
                 files: list[FileRef] | None = None,      # PDF cho OCR: cần deployment có pdf = true
                 job_id: UUID | None = None, task_id: int | None = None) -> LLMResult | Deferred: ...

@dataclass
class LLMResult:
    text: str; parsed: dict | None
    tokens_in: int; tokens_cached: int; tokens_out: int; tokens_thinking: int
    finish_reason: str; latency_ms: int
    deployment_id: str; model: str; diversity_degraded: bool

@dataclass
class Deferred:            # pool chưa có chỗ: handler gọi defer_task(task_id, until), KHÔNG tính vào số lần thử
    until: float; reason: str
```

Hành vi bắt buộc:

1. **Structured output theo bậc thang:** `reference/structured_output.py` chọn `json_schema` → `json_object` → `prompt_only` theo năng lực của model được định tuyến (Gemini dùng wire schema qua `reference/gemini_schema.py`; nhà cung cấp chuẩn OpenAI dùng `response_format` không strict hoặc nhúng schema vào prompt). **Luôn kiểm tra lại** kết quả bằng schema đầy đủ: JSON đúng cú pháp chưa chắc đúng giá trị. Phản hồi lộn xộn (rào code, khối `<think>`, lời dẫn) được `extract_json()` làm sạch trước khi kiểm tra (§17.9).
2. **Phân loại lỗi và hành động** theo bảng §17.7: 429 phút/ngày → cooldown đúng loại; 5xx/timeout → circuit breaker; 401/403 → cách ly khoá; context quá dài → chia nhỏ đầu vào; JSON/schema sai → thử lại **một lần** kèm phản hồi lỗi rồi leo lên deployment khác; `finish_reason = MAX_TOKENS` → `OutputTruncated` (stage tự chia nhỏ); bị chặn an toàn → thử **một lần** ở nhà cung cấp khác (bộ lọc mỗi nơi một khác), nếu vẫn bị chặn thì `ContentBlocked`.
3. **Chi phí:** mỗi lời gọi ghi vào `llm_calls` kèm `deployment_id`, `group_tier`, `data_policy` (bằng chứng tuân thủ), `outcome`; `pool_call_cost()` trả **tiền thật** (0 với deployment miễn phí) và **chi phí bóng** theo giá tham chiếu; chỉ tiền thật vào `add_spend()`; trần chi phí job so với chi phí bóng (§12.7). Nếu `spend_daily.paused` thì deployment trả phí bị loại khỏi ứng viên.
4. **Hạn mức và đồng thời** do pool đảm nhận: token-bucket RPM/TPM, bộ đếm ngày theo múi giờ của nhà cung cấp, số lời gọi đồng thời, cooldown (§17.6). Không còn semaphore Redis toàn hệ thống; giới hạn đồng thời theo job vẫn ở `claim_tasks`.
5. **Riêng tư:** không ghi nội dung prompt/response vào log hay CSDL. `LLM_DEBUG_PAYLOADS=false` mặc định; nếu bật chỉ để debug job do Admin chỉ định, lưu mã hoá, TTL 24 giờ. Kiểm tra tham số lưu trữ (retention) của API tương tác và tắt lưu nếu có (§22).
6. **Test:** `FakeLLMClient` phát lại fixture và chèn lỗi (JSON hỏng, trích đoạn bịa, 429 phút và ngày, 5xx, khoá bị từ chối, chặn an toàn, cắt cụt, hết hạn mức) trên `MemoryState` của pool, để kiểm thử toàn pipeline không tốn tiền.

**Ánh xạ prompt → profile (mặc định):**

| Profile | Dùng bởi | Yêu cầu năng lực mặc định (`needs`) |
|---|---|---|
| `fast` | P0, P1, P2, P8 | ngữ cảnh ≥ 30k, JSON object, điểm `json` ≥ 0.85 |
| `writer` | P3, P4, P7, P9, P11 | ngữ cảnh ≥ 100k, JSON, điểm `json` ≥ 0.95 và `vi_write` ≥ 0.80 |
| `verifier` | P5, P6 | như `writer`; yêu cầu `avoid_groups` = nhóm của người viết khi có thể |
| `ocr` | P10 | thị giác + PDF |
| `curator` | P12, P13, thử lõi | `vi_write` ≥ 0.85 |
| `mt_draft` | khâu bản dịch thô (không có prompt) | `kind = mt`, điểm `mt_en_vi` ≥ 0.7 |

Mỗi profile gồm các **tầng** theo thứ tự (ví dụ: nhóm miễn phí mạnh → trả phí) với chiến lược chọn và thời gian chờ tối đa; cấu hình đầy đủ ở §17.5 và `examples/pool_config.example.json`.

### 5.4 Lưu trữ và vòng đời dữ liệu

- File gốc: object storage, khoá `u/{user_id}/d/{document_id}/original`; xoá theo `documents.expires_at` (mặc định 14 ngày) bằng lifecycle rule + job nền.
- `doc_paragraphs`, `doc_sections`: xoá cùng thời điểm (`purge_expired_documents()`); giữ `knowledge_units.evidence` (trích đoạn ≤ 400 ký tự) để báo cáo vẫn hiển thị trích dẫn.
- Báo cáo, glossary: thuộc người dùng, xoá khi người dùng xoá hoặc sau 180 ngày không hoạt động (thông báo trước 30 ngày).
- Xuất file: tạo theo yêu cầu, cache ở `exports`, hết hạn 7 ngày.

### 5.5 Biến môi trường

| Biến | Ý nghĩa |
|---|---|
| `DATABASE_URL`, `REDIS_URL` | Kết nối CSDL, Redis |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | Object storage |
| `POOL_MASTER_KEY` | Khoá chủ mã hoá khoá API (`secret_enc`) và file gốc. **Lưu ngoài bản sao lưu CSDL** để lộ CSDL không kéo theo lộ khoá (§20.5) |
| `POOL_SEED_CONFIG` | Đường dẫn `pool_config` nạp vào CSDL ở lần khởi tạo đầu (sau đó quản trị qua Admin API) |
| `GEMINI_KEY_*`, `PROVIDER_*_KEY`, `NVIDIA_API_KEY`... | Chỉ cần khi `secret_ref` của `pool_config` trỏ tới biến môi trường (`env:TEN_BIEN`); tên do cấu hình quyết định. Khoá của dự án trả phí PHẢI tách khỏi khoá free |
| `LLM_PER_JOB_CONCURRENCY` | Đồng thời theo job (mặc định 6); hạn mức theo nhà cung cấp nằm trong pool, không phải biến môi trường |
| `LLM_DEBUG_PAYLOADS` | `false` mặc định |
| `APP_BASE_URL`, `SESSION_SECRET` | URL gốc, khoá ký phiên |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | Đăng nhập Google |
| `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET` | Chống bot |
| `EMAIL_PROVIDER_API_KEY`, `EMAIL_FROM` | Email giao dịch (OTP, báo xong) |
| `SENTRY_DSN` | Giám sát lỗi (đã lọc nội dung) |
| `MAX_UPLOAD_MB` | 50 |
| `PARSER_TIMEOUT_SECONDS`, `PARSER_MAX_UNCOMPRESSED_MB` | 120; 200 |
| `ADMIN_EMAILS` | Danh sách email được cấp vai trò admin lúc khởi tạo |
| `APP_ENV` | `dev` hoặc `prod`; cổng triển khai hiện hành (`dev`, `A`, `B`, `C`) nằm ở `app_settings.deploy_gate` |

Các giới hạn nghiệp vụ (trần chi tiêu, tín dụng tặng, số từ tối đa, mức đang bật, cổng triển khai...) và cấu hình pool nằm trong CSDL (`app_settings`, bảng `llm_*`), **không** phải biến môi trường, để Admin chỉnh không cần triển khai lại.

### 5.6 Cấu trúc kho mã

```
visynth/
├─ apps/web/            # Next.js
├─ apps/api/            # FastAPI
├─ apps/worker/         # pipeline + parser sandbox + pool/ (Router, adapter openai_compat và gemini_native)
├─ packages/contracts/  # schemas/, openapi.yaml, kiểu TS sinh tự động
├─ prompts/             # *.md có front matter, version theo semver
├─ db/                  # migrations (Alembic), schema.sql làm bản gốc đối chiếu
├─ eval/                # golden set, rubric, script chạy đánh giá (§16)
├─ infra/               # docker-compose, Caddyfile, Dockerfile, script sao lưu/khôi phục, runbook (§20)
└─ docs/                # SPEC.md, runbooks, bản nháp ToS/Privacy
```
