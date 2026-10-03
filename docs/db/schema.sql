-- =====================================================================================
-- ViSynth - PostgreSQL schema v0.3 (LLM Pool, Lõi văn phong, phát hành glossary, curation; khai báo khoá hàng loạt và xác nhận rủi ro)
-- Tương thích PostgreSQL 16+ (đã chạy thử trên 18.4). Không cần extension nào.
-- Quy ước: id = uuid (gen_random_uuid()), thời gian = timestamptz, enum = text + CHECK
-- (dễ migrate hơn kiểu ENUM). Giá trị enum PHẢI khớp schemas/common.schema.json;
-- tools/validate_spec.py kiểm tra tự động.
-- Mọi bảng chứa dữ liệu người dùng đều truy ngược được về users.id (phục vụ xoá dữ liệu & RLS).
-- =====================================================================================

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END $$;

-- ------------------------------------------------------------------ người dùng & mời
CREATE TABLE users (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email                   text NOT NULL,
  email_verified_at       timestamptz,
  display_name            text,
  avatar_url              text,
  google_sub              text UNIQUE,
  role                    text NOT NULL DEFAULT 'user' CHECK (role IN ('user','curator','admin')),  -- curator: duyệt glossary chuẩn và Lõi văn phong
  status                  text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','suspended','deleted')),
  locale                  text NOT NULL DEFAULT 'vi',
  tos_version             text,
  tos_accepted_at         timestamptz,
  consent_cross_border_at timestamptz,  -- đồng ý gửi nội dung tài liệu cho nhà cung cấp LLM ở nước ngoài (PDPL)
  consent_shared_processing_at timestamptz,  -- đồng ý chế độ 'standard': nội dung có thể đi qua nhà cung cấp dùng dữ liệu để cải thiện sản phẩm (free tier)
  age_confirmed_at        timestamptz,  -- xác nhận từ 18 tuổi (điều khoản Gemini API yêu cầu API Client không hướng tới người dưới 18)
  country_code            text CHECK (country_code ~ '^[A-Z]{2}$'),  -- từ IP lúc đăng ký (ví dụ CF-IPCountry); dùng cho quy tắc EEA/Anh/Thụy Sĩ của free tier
  created_at              timestamptz NOT NULL DEFAULT now(),
  last_login_at           timestamptz,
  deleted_at              timestamptz
);
CREATE UNIQUE INDEX users_email_lower_uq ON users (lower(email));

CREATE TABLE invite_codes (
  code          text PRIMARY KEY CHECK (code ~ '^[A-Z0-9-]{6,32}$'),
  created_by    uuid REFERENCES users(id) ON DELETE SET NULL,
  credits_grant integer NOT NULL CHECK (credits_grant > 0),
  max_uses      integer NOT NULL DEFAULT 1 CHECK (max_uses > 0),
  used_count    integer NOT NULL DEFAULT 0 CHECK (used_count >= 0),
  expires_at    timestamptz,
  revoked_at    timestamptz,
  note          text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (used_count <= max_uses)
);

CREATE TABLE invite_redemptions (
  code        text NOT NULL REFERENCES invite_codes(code),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  redeemed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (code, user_id)
);

-- ------------------------------------------------------------------ tài liệu
CREATE TABLE documents (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title               text NOT NULL,
  source_type         text NOT NULL CHECK (source_type IN ('upload','paste','url')),
  original_filename   text,
  mime_type           text,
  byte_size           bigint CHECK (byte_size >= 0),
  sha256              text,
  storage_key         text,                    -- khoá object storage của file gốc (xoá theo expires_at)
  language_code       text,
  language_confidence real,
  word_count          integer CHECK (word_count >= 0),
  page_count          integer CHECK (page_count >= 0),
  token_estimate      integer CHECK (token_estimate >= 0),
  extraction_quality  real CHECK (extraction_quality BETWEEN 0 AND 1),
  extraction_warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_kind         text CHECK (source_kind IS NULL OR source_kind IN ('text','ocr')),  -- chữ có sẵn hay do P10 OCR
  ocr_pages           jsonb NOT NULL DEFAULT '[]'::jsonb,  -- trang cần OCR (worker lập cụm 10-15 trang, §6.1)
  profile             jsonb,                   -- DocProfile (schemas/doc_profile.schema.json)
  rights_attested_at  timestamptz,             -- người dùng xác nhận có quyền sử dụng tài liệu
  status              text NOT NULL DEFAULT 'uploaded' CHECK (status IN ('uploaded','extracting','ready','failed','deleted')),
  error_code          text,
  error_message       text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  expires_at          timestamptz,             -- hết hạn: xoá file gốc + đoạn văn bóc tách
  deleted_at          timestamptz
);
CREATE INDEX documents_user_idx ON documents (user_id, created_at DESC);
CREATE INDEX documents_sha_idx ON documents (user_id, sha256);
CREATE INDEX documents_expiry_idx ON documents (expires_at) WHERE status <> 'deleted';

CREATE TABLE doc_sections (
  document_id       uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  section_id        text NOT NULL,
  parent_section_id text,
  title             text NOT NULL,
  level             integer NOT NULL CHECK (level BETWEEN 1 AND 6),
  idx               integer NOT NULL,
  first_pid         text NOT NULL,
  last_pid          text NOT NULL,
  PRIMARY KEY (document_id, section_id)
);

CREATE TABLE doc_paragraphs (
  document_id       uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  pid               text NOT NULL CHECK (pid ~ '^P[0-9]{6,}$'),
  idx               integer NOT NULL,
  kind              text NOT NULL CHECK (kind IN ('heading','body','list_item','table','footnote','caption','quote','code','speaker_turn','other')),
  content           text NOT NULL,
  section_id        text,
  page_start        integer,
  page_end          integer,
  timecode_start_ms integer,
  timecode_end_ms   integer,
  speaker           text,
  char_count        integer NOT NULL CHECK (char_count >= 0),
  PRIMARY KEY (document_id, pid)
);
CREATE UNIQUE INDEX doc_paragraphs_idx_uq ON doc_paragraphs (document_id, idx);

-- ------------------------------------------------------------------ thuật ngữ & công thức
CREATE TABLE glossaries (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid REFERENCES users(id) ON DELETE CASCADE,   -- NULL = glossary chuẩn của hệ thống (Admin quản lý)
  scope       text NOT NULL CHECK (scope IN ('personal','shared')),
  name        text NOT NULL,
  description text,
  domain      text,
  source_lang text NOT NULL DEFAULT 'en',
  target_lang text NOT NULL DEFAULT 'vi',
  version     integer NOT NULL DEFAULT 0,          -- số hiệu bản phát hành mới nhất (glossary_releases); 0 = chưa phát hành
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CHECK ((scope = 'personal' AND owner_id IS NOT NULL) OR (scope = 'shared' AND owner_id IS NULL))
);
CREATE TRIGGER glossaries_updated BEFORE UPDATE ON glossaries FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE glossary_entries (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  glossary_id         uuid NOT NULL REFERENCES glossaries(id) ON DELETE CASCADE,
  source_term         text NOT NULL,
  target_term         text NOT NULL,
  keep_original       boolean NOT NULL DEFAULT false,
  case_sensitive      boolean NOT NULL DEFAULT false,
  forbidden_variants  text[] NOT NULL DEFAULT '{}',
  term_type           text NOT NULL DEFAULT 'concept' CHECK (term_type IN ('concept','proper_name','acronym','title','unit','other')),
  note                text,
  status              text NOT NULL DEFAULT 'confirmed' CHECK (status IN ('suggested','confirmed','rejected')),
  confidence          real CHECK (confidence BETWEEN 0 AND 1),
  created_from_job_id uuid,
  proposed_by         text NOT NULL DEFAULT 'curator' CHECK (proposed_by IN ('ai','user','curator','import')),  -- ai: đề xuất của P1/P13; luôn cần người duyệt trước khi vào glossary chuẩn
  needs_human         boolean NOT NULL DEFAULT false,   -- AI đánh dấu mục cần người quyết định (xếp lên đầu hàng duyệt)
  question_vi         text,                             -- câu hỏi AI nêu cho người duyệt
  evidence            jsonb NOT NULL DEFAULT '[]'::jsonb, -- [{doc_ref, snippet}] do code ghép từ ctx_ids, không phải model tự viết
  reviewed_by         uuid REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at         timestamptz,
  review_note         text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX glossary_entries_term_uq ON glossary_entries (glossary_id, lower(source_term));
CREATE TRIGGER glossary_entries_updated BEFORE UPDATE ON glossary_entries FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX glossary_entries_review_idx ON glossary_entries (glossary_id, needs_human DESC, status) WHERE status = 'suggested';

-- Bản phát hành bất biến của glossary (công thức/job ghim theo số hiệu để kết quả tái lập được). Xem hàm publish_glossary_release().
CREATE TABLE glossary_releases (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  glossary_id    uuid NOT NULL REFERENCES glossaries(id) ON DELETE CASCADE,
  version        integer NOT NULL CHECK (version >= 1),
  entries        jsonb NOT NULL,                 -- ảnh chụp các mục 'confirmed' tại thời điểm phát hành
  entry_count    integer NOT NULL CHECK (entry_count >= 0),
  content_sha256 text NOT NULL,
  released_by    uuid REFERENCES users(id) ON DELETE SET NULL,
  released_at    timestamptz NOT NULL DEFAULT now(),
  note           text,
  UNIQUE (glossary_id, version)
);

CREATE TABLE recipes (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,60}$'),
  owner_id    uuid REFERENCES users(id) ON DELETE SET NULL,
  name        text NOT NULL,
  description text,
  visibility  text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','unlisted','public')),
  is_official boolean NOT NULL DEFAULT false,
  version     integer NOT NULL DEFAULT 1,
  config      jsonb NOT NULL,                   -- RecipeConfig (schemas/recipe_config.schema.json)
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER recipes_updated BEFORE UPDATE ON recipes FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ------------------------------------------------------------------ curation: AI đề xuất, người duyệt (HITL)
-- Một lần chạy P12 (đề xuất Lõi văn phong) hoặc P13 (khởi tạo glossary chuẩn từ tài liệu mẫu). Không thuộc job của người dùng.
CREATE TABLE curation_runs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind          text NOT NULL CHECK (kind IN ('style_core_proposal','style_core_test_drive','glossary_bootstrap')),
  requested_by  uuid REFERENCES users(id) ON DELETE SET NULL,
  status        text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','canceled')),
  params        jsonb NOT NULL,                  -- {style_core_id?, glossary_id?, sample_document_ids[], mode, brief_vi...}
  result        jsonb,                           -- tóm tắt: số đề xuất, id phiên bản nháp được tạo...
  cost_usd      numeric(10,4) NOT NULL DEFAULT 0,
  shadow_usd    numeric(10,4) NOT NULL DEFAULT 0,
  error_code    text,
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  finished_at   timestamptz
);
CREATE INDEX curation_runs_pending_idx ON curation_runs (created_at) WHERE status IN ('queued','running');

-- Lõi văn phong: NỘI DUNG (voice, quy tắc, chính sách thuật ngữ, ví dụ mẫu) là dữ liệu có phiên bản, KHÔNG nằm trong spec.
CREATE TABLE style_cores (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,60}$'),
  scope       text NOT NULL DEFAULT 'system' CHECK (scope IN ('system','personal')),
  owner_id    uuid REFERENCES users(id) ON DELETE CASCADE,   -- NULL với scope = 'system'
  parent_id   uuid REFERENCES style_cores(id) ON DELETE SET NULL,  -- kế thừa: lõi con ghi đè quy tắc cùng id của lõi cha
  name        text NOT NULL,
  domain      text,
  locale      text NOT NULL DEFAULT 'vi',
  created_at  timestamptz NOT NULL DEFAULT now(),
  CHECK ((scope = 'system' AND owner_id IS NULL) OR (scope = 'personal' AND owner_id IS NOT NULL))
);

CREATE TABLE style_core_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  style_core_id   uuid NOT NULL REFERENCES style_cores(id) ON DELETE CASCADE,
  version         text NOT NULL CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  status          text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','in_review','approved','rejected','deprecated')),
  origin          text NOT NULL DEFAULT 'human' CHECK (origin IN ('human','ai_proposal','import')),
  content         jsonb NOT NULL,                -- StyleCore (schemas/style_core.schema.json)
  content_sha256  text NOT NULL,
  open_decisions  jsonb NOT NULL DEFAULT '[]'::jsonb,  -- 'decisions_needed' của P12 chưa được người duyệt trả lời
  proposal_run_id uuid REFERENCES curation_runs(id) ON DELETE SET NULL,
  created_by      uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  submitted_at    timestamptz,
  approved_by     uuid REFERENCES users(id) ON DELETE SET NULL,
  approved_at     timestamptz,
  note            text,
  UNIQUE (style_core_id, version),
  CHECK (status <> 'approved' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL AND jsonb_array_length(open_decisions) = 0))
);
CREATE INDEX style_core_versions_status_idx ON style_core_versions (style_core_id, status);

CREATE TABLE style_core_reviews (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  version_id  uuid NOT NULL REFERENCES style_core_versions(id) ON DELETE CASCADE,
  reviewer_id uuid REFERENCES users(id) ON DELETE SET NULL,
  action      text NOT NULL CHECK (action IN ('comment','edit','answer_decision','test_drive','submit','approve','reject','request_changes')),
  field_path  text,                              -- ví dụ rules[R03].text
  before      jsonb,
  after       jsonb,
  comment     text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX style_core_reviews_version_idx ON style_core_reviews (version_id, id);

-- ------------------------------------------------------------------ job & điều phối
CREATE TABLE jobs (
  id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  document_id              uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  recipe_id                uuid REFERENCES recipes(id) ON DELETE SET NULL,
  recipe_version           integer,
  level                    text NOT NULL CHECK (level IN ('full_translation','detailed_synthesis','deep_synthesis','executive_brief')),
  source_lang              text,
  target_lang              text NOT NULL DEFAULT 'vi',
  options                  jsonb NOT NULL DEFAULT '{}'::jsonb,
  status                   text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','awaiting_glossary','succeeded','failed','canceled','expired')),
  current_stage            text CHECK (current_stage IN ('extract','profile','segment','glossary','map','consolidate','write','verify','repair','translate','assemble')),
  progress                 real NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  est_credits              integer NOT NULL CHECK (est_credits > 0),
  charged_credits          integer NOT NULL DEFAULT 0,
  refunded_credits         integer NOT NULL DEFAULT 0,
  est_cost_usd             numeric(10,4),        -- ước tính theo GIÁ THAM CHIẾU (llm_prices), không phụ thuộc deployment thực tế
  actual_cost_usd          numeric(10,4) NOT NULL DEFAULT 0,   -- tiền thật đã chi (deployment price_mode = metered)
  actual_shadow_usd        numeric(10,4) NOT NULL DEFAULT 0,   -- chi phí 'bóng': cùng khối lượng tính theo giá tham chiếu, kể cả deployment miễn phí
  max_cost_usd             numeric(10,4),        -- trần cứng, so với actual_shadow_usd: dừng job nếu vượt (SPEC §12.7)
  privacy_class            text NOT NULL DEFAULT 'standard' CHECK (privacy_class IN ('standard','private')),  -- quyết định deployment nào được dùng (SPEC §17.4)
  style_core_version_id    uuid REFERENCES style_core_versions(id) ON DELETE SET NULL,   -- Lõi văn phong đã ghim cho job
  glossary_releases        jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{glossary_id, version, sha256}] ảnh chụp bản phát hành đã dùng
  model_profile            jsonb NOT NULL,       -- ảnh chụp cấu hình pool lúc tạo job: {"pool_version":7,"profiles":{"fast":["free","paid"],...}}
  prompt_versions          jsonb NOT NULL,       -- ảnh chụp {"P2_unit_extractor":"1.0.0",...} để tái lập kết quả
  quality_grade            text CHECK (quality_grade IN ('A','B','C')),
  idempotency_key          text,
  glossary_review_deadline timestamptz,
  glossary_auto_confirmed  boolean NOT NULL DEFAULT false,
  cancel_requested         boolean NOT NULL DEFAULT false,
  error_code               text,
  error_message            text,
  created_at               timestamptz NOT NULL DEFAULT now(),
  started_at               timestamptz,
  finished_at              timestamptz,
  expires_at               timestamptz
);
CREATE UNIQUE INDEX jobs_idem_uq ON jobs (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX jobs_user_idx ON jobs (user_id, created_at DESC);
CREATE INDEX jobs_active_idx ON jobs (status) WHERE status IN ('queued','running','awaiting_glossary');

CREATE TABLE job_glossary_entries (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id             uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  source_term        text NOT NULL,
  target_term        text NOT NULL,
  keep_original      boolean NOT NULL DEFAULT false,
  case_sensitive     boolean NOT NULL DEFAULT false,
  forbidden_variants text[] NOT NULL DEFAULT '{}',
  term_type          text NOT NULL DEFAULT 'concept' CHECK (term_type IN ('concept','proper_name','acronym','title','unit','other')),
  note               text,
  origin             text NOT NULL CHECK (origin IN ('shared','personal','suggested','user_edit')),
  status             text NOT NULL DEFAULT 'confirmed' CHECK (status IN ('suggested','confirmed','rejected')),
  confidence         real CHECK (confidence BETWEEN 0 AND 1),
  priority           integer NOT NULL DEFAULT 0   -- lớn hơn = ưu tiên hơn khi trùng source_term giữa các glossary
);
CREATE UNIQUE INDEX job_glossary_term_uq ON job_glossary_entries (job_id, lower(source_term));

CREATE TABLE job_events (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id     uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  type       text NOT NULL,
  stage      text,
  payload    jsonb NOT NULL DEFAULT '{}'::jsonb,   -- JobEvent (schemas/job_event.schema.json), không chứa nội dung tài liệu
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX job_events_job_idx ON job_events (job_id, id);

CREATE TABLE job_stages (
  job_id      uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  stage       text NOT NULL CHECK (stage IN ('extract','profile','segment','glossary','map','consolidate','write','verify','repair','translate','assemble')),
  status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','succeeded','failed','skipped')),
  attempt     integer NOT NULL DEFAULT 0,
  started_at  timestamptz,
  finished_at timestamptz,
  metrics     jsonb NOT NULL DEFAULT '{}'::jsonb,
  error       text,
  PRIMARY KEY (job_id, stage)
);

-- Điểm lưu trạng thái pipeline sau mỗi giai đoạn: worker chết giữa chừng thì task được thu hồi và
-- chạy tiếp từ giai đoạn kế tiếp, KHÔNG chạy lại (và không tính tiền lại) các giai đoạn đã xong.
CREATE TABLE job_checkpoints (
  job_id     uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  stage      text NOT NULL,
  state      jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, stage)
);

CREATE TABLE job_tasks (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id       uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  stage        text NOT NULL,
  task_key     text NOT NULL,                        -- ví dụ SEG-007, S03, S03:round2
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','succeeded','failed','skipped')),
  attempt      integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  run_after    timestamptz NOT NULL DEFAULT now(),   -- backoff: đặt lại khi retry
  locked_by    text,
  locked_at    timestamptz,
  heartbeat_at timestamptz,
  input_hash   text,
  result       jsonb,
  error_code   text,
  error        text,
  tokens_in    integer NOT NULL DEFAULT 0,
  tokens_out   integer NOT NULL DEFAULT 0,
  cost_usd     numeric(10,6) NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  UNIQUE (job_id, stage, task_key),
  FOREIGN KEY (job_id, stage) REFERENCES job_stages(job_id, stage) ON DELETE CASCADE
);
CREATE INDEX job_tasks_pending_idx ON job_tasks (created_at, id) WHERE status = 'pending';
CREATE INDEX job_tasks_running_idx ON job_tasks (job_id) WHERE status = 'running';

CREATE TABLE segments (
  job_id        uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  segment_id    text NOT NULL CHECK (segment_id ~ '^SEG-[0-9]{3,}$'),
  idx           integer NOT NULL,
  first_pid     text NOT NULL,
  last_pid      text NOT NULL,
  token_count   integer NOT NULL CHECK (token_count > 0),
  section_id    text,
  summary_vi    text,
  labels        jsonb NOT NULL DEFAULT '[]'::jsonb,
  quality_flags text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (job_id, segment_id)
);

CREATE TABLE knowledge_units (
  job_id        uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  id            text NOT NULL CHECK (id ~ '^U-[0-9]{4,}$'),
  segment_id    text NOT NULL,
  local_id      text NOT NULL,
  type          text NOT NULL CHECK (type IN ('definition','mechanism','argument','procedure','fact_data','prediction','example','qa','anecdote','aside','admin')),
  importance    text NOT NULL CHECK (importance IN ('core','supporting','minor')),
  title_vi      text NOT NULL,
  statement_vi  text NOT NULL,
  topics        text[] NOT NULL DEFAULT '{}',
  terms         text[] NOT NULL DEFAULT '{}',
  evidence      jsonb NOT NULL,                       -- [{pid, quote, match}] đã qua reference/quote_verify.py
  numbers       jsonb NOT NULL DEFAULT '[]'::jsonb,
  relations     jsonb NOT NULL DEFAULT '[]'::jsonb,
  attribution   text NOT NULL DEFAULT 'author' CHECK (attribution IN ('author','third_party','unclear')),
  state         text NOT NULL DEFAULT 'active' CHECK (state IN ('active','merged','omitted','unverified')),
  merged_into   text,
  omit_reason   text,
  PRIMARY KEY (job_id, id),
  FOREIGN KEY (job_id, segment_id) REFERENCES segments(job_id, segment_id) ON DELETE CASCADE
);
CREATE INDEX knowledge_units_pick_idx ON knowledge_units (job_id, importance, state);

-- ------------------------------------------------------------------ kết quả
CREATE TABLE reports (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id        uuid NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
  document_id   uuid REFERENCES documents(id) ON DELETE SET NULL,
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title         text NOT NULL,
  level         text NOT NULL CHECK (level IN ('full_translation','detailed_synthesis','deep_synthesis','executive_brief')),
  plan          jsonb,                                 -- ReportPlan
  stats         jsonb NOT NULL DEFAULT '{}'::jsonb,    -- coverage_core, faithfulness_rate, flagged_blocks, ...
  scope_note_md text,
  markdown      text,                                  -- bản ghép hoàn chỉnh (nguồn sự thật để xuất file)
  quality_grade text CHECK (quality_grade IN ('A','B','C')),
  version       integer NOT NULL DEFAULT 1,
  created_at    timestamptz NOT NULL DEFAULT now(),
  deleted_at    timestamptz
);
CREATE INDEX reports_user_idx ON reports (user_id, created_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE report_sections (
  report_id uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  section_id text NOT NULL,
  idx       integer NOT NULL,
  kind      text NOT NULL CHECK (kind IN ('summary','body','facts_table','glossary_appendix','scope_note')),
  title_vi  text NOT NULL,
  PRIMARY KEY (report_id, section_id)
);

CREATE TABLE report_blocks (
  report_id   uuid NOT NULL,
  section_id  text NOT NULL,
  block_id    text NOT NULL CHECK (block_id ~ '^S[0-9]{2,}\.b[0-9]{2,}$'),
  idx         integer NOT NULL,
  type        text NOT NULL CHECK (type IN ('paragraph','bullet_list','numbered_list','table','callout','subheading')),
  markdown_vi text NOT NULL,
  cites       text[] NOT NULL DEFAULT '{}',
  verdict     text CHECK (verdict IN ('supported','partially_supported','unsupported','contradicted')),
  issues      jsonb NOT NULL DEFAULT '[]'::jsonb,
  status      text NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','repaired','flagged','removed')),
  PRIMARY KEY (report_id, block_id),
  FOREIGN KEY (report_id, section_id) REFERENCES report_sections(report_id, section_id) ON DELETE CASCADE
);

CREATE TABLE translation_items (          -- chỉ dùng cho level = full_translation
  job_id       uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  pid          text NOT NULL CHECK (pid ~ '^P[0-9]{6,}$'),
  vi           text NOT NULL,
  note_vi      text,
  flagged      boolean NOT NULL DEFAULT false,
  PRIMARY KEY (job_id, pid)
);

CREATE TABLE exports (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  report_id  uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  format     text NOT NULL CHECK (format IN ('md','docx','pdf','html')),
  storage_key text NOT NULL,
  byte_size  bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  UNIQUE (report_id, format)
);

-- ------------------------------------------------------------------ tín dụng (sổ cái chỉ-ghi-thêm)
CREATE TABLE credit_ledger (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  delta           integer NOT NULL CHECK (delta <> 0),
  reason          text NOT NULL CHECK (reason IN ('grant_signup','grant_invite','grant_monthly','purchase','job_charge','job_refund','admin_adjust','expire')),
  job_id          uuid REFERENCES jobs(id) ON DELETE SET NULL,
  idempotency_key text,
  meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX credit_ledger_idem_uq ON credit_ledger (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX credit_ledger_user_idx ON credit_ledger (user_id, created_at DESC);
CREATE VIEW v_credit_balance AS
  SELECT user_id, COALESCE(SUM(delta), 0)::integer AS balance FROM credit_ledger GROUP BY user_id;

-- ------------------------------------------------------------------ LLM: bảng giá, nhật ký gọi, trần chi tiêu
CREATE TABLE llm_prices (
  model                  text NOT NULL,
  effective_from         date NOT NULL,
  input_per_mtok         numeric(10,4) NOT NULL,
  output_per_mtok        numeric(10,4) NOT NULL,
  cached_input_per_mtok  numeric(10,4),
  batch_multiplier       numeric(4,3) NOT NULL DEFAULT 1.0,
  note                   text,
  PRIMARY KEY (model, effective_from)
);
-- Nguồn: trang giá Gemini API (ai.google.dev/gemini-api/docs/pricing), kiểm tra ngày 2026-10-02.
-- Giá khuyến mãi đến hết 2026-12-31, từ 2027-01-01 tăng gấp đôi. LUÔN dùng bản ghi có hiệu lực theo ngày.
INSERT INTO llm_prices (model, effective_from, input_per_mtok, output_per_mtok, cached_input_per_mtok, batch_multiplier, note) VALUES
  ('gemini-3.8-flash', '2026-09-02', 0.75, 3.75, 0.075, 0.5, 'Gia khuyen mai den het 2026-12-31; Batch = 50%'),
  ('gemini-3.8-flash', '2027-01-01', 1.50, 7.50, 0.15,  0.5, 'Gia tu 2027-01-01 theo trang gia Gemini API');


-- ------------------------------------------------------------------ LLM POOL (SPEC §17)
-- Khoá API KHÔNG nằm ở đây dưới dạng rõ: secret_ref (env:/file:) hoặc secret_enc (mã hoá AES-GCM ở tầng ứng dụng, khoá chủ POOL_MASTER_KEY
-- nằm ngoài bản sao lưu CSDL). Mọi con số giới hạn là CẤU HÌNH do chủ hệ thống nhập từ bảng điều khiển của nhà cung cấp.
CREATE TABLE llm_providers (
  id           text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{1,39}$'),
  kind         text NOT NULL CHECK (kind IN ('openai_compat','gemini_native')),
  base_url     text NOT NULL CHECK (base_url ~ '^https://'),
  display_name text NOT NULL,
  quirks       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"quota_scope":"deployment|group","auth_header":"bearer|x-goog-api-key"}
  enabled      boolean NOT NULL DEFAULT true
);

CREATE TABLE llm_models (
  id                text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_./:-]{1,80}$'),
  provider_id       text NOT NULL REFERENCES llm_providers(id) ON DELETE CASCADE,
  model_id          text NOT NULL,                    -- tên model đúng như nhà cung cấp yêu cầu
  ctx_in            integer NOT NULL CHECK (ctx_in >= 1024),
  max_out           integer NOT NULL CHECK (max_out >= 256),
  structured        text NOT NULL DEFAULT 'none' CHECK (structured IN ('none','json_object','json_schema')),
  vision            boolean NOT NULL DEFAULT false,
  pdf               boolean NOT NULL DEFAULT false,
  tokenizer_factor  double precision NOT NULL DEFAULT 1.0 CHECK (tokenizer_factor BETWEEN 0.3 AND 4),
  price_key         text,                             -- khoá trong llm_prices (tiền thật khi deployment metered)
  shadow_price_key  text,                             -- khoá giá tham chiếu để tính chi phí bóng cho deployment miễn phí
  quality           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- điểm kiểm định {json, vi_write, long_context, mt_en_vi}
  adapter_options   jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (provider_id, model_id)
);

CREATE TABLE llm_quota_groups (
  id            text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{1,60}$'),
  provider_id   text NOT NULL REFERENCES llm_providers(id) ON DELETE CASCADE,
  label         text NOT NULL,
  tier          text NOT NULL CHECK (tier IN ('free','trial','paid','self_hosted')),
  data_policy   text NOT NULL DEFAULT 'unknown' CHECK (data_policy IN ('no_training','may_train','unknown')),
  reset_tz      text NOT NULL DEFAULT 'UTC',
  tos_flags     text[] NOT NULL DEFAULT '{}' CHECK (tos_flags <@ ARRAY['trial_only','no_personal_data','multi_account_risk','no_eea_uk_ch']),
  allowed_gates text[] NOT NULL DEFAULT '{dev,A,B,C}' CHECK (allowed_gates <@ ARRAY['dev','A','B','C'] AND cardinality(allowed_gates) >= 1),
  safety_margin double precision NOT NULL DEFAULT 0.85 CHECK (safety_margin BETWEEN 0.3 AND 1),
  day_margin    double precision NOT NULL DEFAULT 0.95 CHECK (day_margin BETWEEN 0.5 AND 1),
  rpm integer CHECK (rpm >= 1), tpm integer CHECK (tpm >= 1), rpd integer CHECK (rpd >= 1), tpd bigint CHECK (tpd >= 1), concurrency integer CHECK (concurrency >= 1),  -- giới hạn CẢ TÀI KHOẢN (NULL = không biết/không có)
  enabled       boolean NOT NULL DEFAULT true,
  notes         text,
  risk_ack_flags text[] NOT NULL DEFAULT '{}' CHECK (risk_ack_flags <@ ARRAY['multi_account_risk','trial_only']),  -- cờ rủi ro điều khoản mà chủ hệ thống đã xác nhận chấp nhận
  risk_ack_at   timestamptz,
  risk_ack_by   uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (NOT enabled OR NOT ('multi_account_risk' = ANY (tos_flags)) OR 'multi_account_risk' = ANY (risk_ack_flags)),  -- không bật được nhóm có cờ rủi ro nếu chưa xác nhận
  CHECK (NOT enabled OR NOT ('trial_only' = ANY (tos_flags)) OR 'trial_only' = ANY (risk_ack_flags))
);

CREATE TABLE llm_credentials (
  id                 text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{1,80}$'),
  group_id           text NOT NULL REFERENCES llm_quota_groups(id) ON DELETE CASCADE,
  label              text NOT NULL,
  last4              text,                             -- 4 ký tự cuối để Admin nhận diện; không bao giờ trả khoá đầy đủ qua API
  fingerprint        text,                             -- HMAC-SHA256(khoá chủ, khoá API) rút gọn: phát hiện khoá trùng mà không cần giải mã (SPEC §17.14)
  secret_ref         text CHECK (secret_ref ~ '^(env|file):[^\s]{1,200}$'),
  secret_enc         bytea,
  status             text NOT NULL DEFAULT 'active' CHECK (status IN ('active','quarantined','disabled')),
  quarantined_reason text,
  last_used_at       timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK ((secret_ref IS NOT NULL) <> (secret_enc IS NOT NULL))
);
CREATE INDEX llm_credentials_group_idx ON llm_credentials (group_id, status);
CREATE UNIQUE INDEX llm_credentials_fingerprint_uq ON llm_credentials (fingerprint) WHERE fingerprint IS NOT NULL;

CREATE TABLE llm_deployments (                          -- deployment = nhóm hạn mức x model: đơn vị mà bộ định tuyến chọn
  id          text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_./:-]{1,100}$'),
  group_id    text NOT NULL REFERENCES llm_quota_groups(id) ON DELETE CASCADE,
  model_id    text NOT NULL REFERENCES llm_models(id) ON DELETE CASCADE,
  rpm integer CHECK (rpm >= 1), tpm integer CHECK (tpm >= 1), rpd integer CHECK (rpd >= 1), tpd bigint CHECK (tpd >= 1),
  concurrency integer NOT NULL DEFAULT 4 CHECK (concurrency >= 1),
  tpm_basis   text NOT NULL DEFAULT 'total' CHECK (tpm_basis IN ('input','total')),
  price_mode  text NOT NULL DEFAULT 'free' CHECK (price_mode IN ('free','metered')),
  weight      double precision NOT NULL DEFAULT 1.0 CHECK (weight > 0),
  tags        text[] NOT NULL DEFAULT '{}',
  enabled     boolean NOT NULL DEFAULT true,
  UNIQUE (group_id, model_id)
);

CREATE TABLE llm_profiles (
  name       text PRIMARY KEY CHECK (name IN ('fast','writer','verifier','ocr','curator')),
  needs      jsonb NOT NULL,        -- {structured, min_ctx_in, vision, pdf, min_quality{}}
  version    integer NOT NULL DEFAULT 1,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE llm_profile_tiers (
  profile_name      text NOT NULL REFERENCES llm_profiles(name) ON DELETE CASCADE,
  tier_no           smallint NOT NULL CHECK (tier_no >= 1),
  name              text NOT NULL,
  select_tags       text[] NOT NULL DEFAULT '{}',       -- deployment phải có TẤT CẢ các tag này
  select_group_tiers text[] NOT NULL DEFAULT '{}',      -- tier của nhóm hạn mức; rỗng = bất kỳ
  strategy          text NOT NULL DEFAULT 'headroom' CHECK (strategy IN ('headroom','weighted','ordered')),
  max_wait_s        double precision CHECK (max_wait_s >= 0),   -- NULL = chờ vô hạn
  PRIMARY KEY (profile_name, tier_no)
);

-- Trạng thái động. scope 'group' = đếm chung cả tài khoản, 'deployment' = đếm riêng theo model. Thời gian là epoch (giây, double)
-- để hàm SQL và MemoryState (reference/llm_pool.py) cho CÙNG kết quả; production truyền extract(epoch from clock_timestamp()).
CREATE TABLE llm_scope_state (
  scope                text NOT NULL CHECK (scope IN ('group','deployment')),
  scope_id             text NOT NULL,
  rpm_level            double precision,
  tpm_level            double precision,
  bucket_at            double precision,
  day_key              date,
  rpd_used             integer NOT NULL DEFAULT 0,
  tpd_used             bigint NOT NULL DEFAULT 0,
  inflight             integer NOT NULL DEFAULT 0 CHECK (inflight >= 0),
  limit_scale          double precision NOT NULL DEFAULT 1.0,
  cooldown_until       double precision NOT NULL DEFAULT 0,
  circuit              text NOT NULL DEFAULT 'closed' CHECK (circuit IN ('closed','open','half_open')),
  consecutive_failures integer NOT NULL DEFAULT 0,
  consecutive_429      integer NOT NULL DEFAULT 0,
  trips                integer NOT NULL DEFAULT 0,
  ewma_success         double precision NOT NULL DEFAULT 1.0,
  ewma_valid           double precision NOT NULL DEFAULT 1.0,
  ewma_latency_ms      double precision,
  last_used_at         double precision NOT NULL DEFAULT 0,
  PRIMARY KEY (scope, scope_id)
);

CREATE TABLE llm_leases (                                -- chỗ đã đặt cho một lời gọi đang bay; hết hạn thì pool_reap_leases() thu hồi
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  deployment_id  text NOT NULL REFERENCES llm_deployments(id) ON DELETE CASCADE,
  credential_id  text NOT NULL,
  basis_tokens   integer NOT NULL,
  out_tokens     integer NOT NULL,
  priority       text NOT NULL,
  job_id         uuid,
  task_id        bigint,
  created_at     double precision NOT NULL,
  expires_at     double precision NOT NULL
);
CREATE INDEX llm_leases_expiry_idx ON llm_leases (expires_at);

CREATE TABLE llm_incidents (                             -- 429, mở circuit, khoá bị từ chối...: để Admin điều tra và cảnh báo
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at            timestamptz NOT NULL DEFAULT now(),
  deployment_id text,
  credential_id text,
  kind          text NOT NULL,
  detail        jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX llm_incidents_at_idx ON llm_incidents (at DESC);

CREATE TABLE llm_probe_runs (                            -- bài kiểm định nhận deployment vào pool (SPEC §17.9)
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  deployment_id text NOT NULL REFERENCES llm_deployments(id) ON DELETE CASCADE,
  ran_at        timestamptz NOT NULL DEFAULT now(),
  passed        boolean NOT NULL,
  results       jsonb NOT NULL,                            -- {json, vi_write, long_context, mt_en_vi, latency_ms_p50, observed_limits}
  note          text
);

CREATE TABLE llm_calls (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id          uuid REFERENCES jobs(id) ON DELETE SET NULL,
  task_id         bigint REFERENCES job_tasks(id) ON DELETE SET NULL,
  user_id         uuid REFERENCES users(id) ON DELETE SET NULL,
  stage           text,
  prompt_id       text,
  prompt_version  text,
  model           text NOT NULL,
  tokens_in       integer NOT NULL DEFAULT 0,    -- TỔNG token đầu vào, đã gồm phần cached
  tokens_cached   integer NOT NULL DEFAULT 0,
  tokens_out      integer NOT NULL DEFAULT 0,    -- token hiển thị
  tokens_thinking integer NOT NULL DEFAULT 0,    -- tính giá như token ra
  latency_ms      integer,
  status          text NOT NULL CHECK (status IN ('ok','retryable_error','fatal_error','blocked')),
  error_code      text,
  cost_usd        numeric(10,6) NOT NULL DEFAULT 0,          -- tiền thật (0 với deployment miễn phí)
  shadow_cost_usd numeric(10,6) NOT NULL DEFAULT 0,          -- chi phí bóng theo giá tham chiếu (dùng cho trần chi phí job và hiệu chỉnh)
  batch           boolean NOT NULL DEFAULT false,
  deployment_id   text REFERENCES llm_deployments(id) ON DELETE SET NULL,
  credential_id   text,
  provider_id     text,
  group_tier      text,                                      -- ảnh chụp tier của nhóm tại thời điểm gọi
  data_policy     text,                                      -- ảnh chụp data_policy của nhóm tại thời điểm gọi (bằng chứng tuân thủ)
  privacy_class   text CHECK (privacy_class IN ('standard','private')),
  outcome         text,                                      -- Outcome.kind của pool (ok, rate_limited_minute, ...)
  pool_wait_ms    integer NOT NULL DEFAULT 0,
  diversity_degraded boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
  -- KHÔNG lưu nội dung prompt/response ở đây (riêng tư). Nếu cần debug: bảng riêng, mã hoá, TTL 24h, mặc định tắt.
);
CREATE INDEX llm_calls_job_idx ON llm_calls (job_id);
CREATE INDEX llm_calls_day_idx ON llm_calls (created_at);
CREATE INDEX llm_calls_deployment_idx ON llm_calls (deployment_id, created_at);

CREATE TABLE app_settings (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO app_settings (key, value) VALUES
  ('daily_spend_cap_usd',        '20'),
  ('free_signup_credits',        '100'),
  ('max_words_per_document',     '250000'),
  ('max_words_full_translation', '120000'),
  ('max_active_jobs_per_user',   '1'),
  ('document_retention_days',    '14'),
  ('deploy_gate',                '"dev"'),                   -- dev | A | B | C: quyết định nhóm hạn mức nào được dùng (allowed_gates)
  ('enabled_levels',             '["full_translation","detailed_synthesis","deep_synthesis","executive_brief"]'),
  ('full_translation_daily_limit','3'),                      -- số job dịch đầy đủ / người dùng / ngày (giảm rủi ro sao chép hàng loạt)
  ('pool_priority_reserve',      '0.2'),
  ('paid_spill_enabled',         'true'),                    -- cho phép chuyển sang deployment trả phí khi tầng miễn phí hết hạn mức
  ('max_pool_wait_hours',        '12'),                      -- tổng thời gian chờ hạn mức tối đa của một job trước khi chuyển tầng trả phí hoặc thất bại
  ('restricted_free_tier_countries', '["AT","BE","BG","HR","CY","CZ","DK","EE","FI","FR","DE","GR","HU","IE","IT","LV","LT","LU","MT","NL","PL","PT","RO","SK","SI","ES","SE","IS","LI","NO","CH","GB"]'),
  ('pool_allow_risk_at_public_gates', 'false'),             -- true: chấp nhận dùng nhóm gắn multi_account_risk/trial_only ngay cả ở cổng B/C (SPEC §17.4)
  ('pool_lease_ttl_s',           '120'),                    -- thời gian sống của một chỗ đặt trước (llm_leases)
  ('pool_diversity_max_wait_s',  '30'),                     -- chờ tối đa để đa dạng hoá deployment trước khi nhận trùng
  ('pool_version',               '1');

CREATE TABLE spend_daily (
  day          date PRIMARY KEY,
  cost_usd     numeric(12,4) NOT NULL DEFAULT 0,
  jobs_started integer NOT NULL DEFAULT 0,
  cap_usd      numeric(12,4) NOT NULL,
  paused       boolean NOT NULL DEFAULT false,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ phản hồi, kiểm toán, khiếu nại, phiên bản prompt
CREATE TABLE feedback (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  report_id  uuid NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind       text NOT NULL DEFAULT 'report' CHECK (kind IN ('report','block_flag')),
  rating     smallint CHECK (rating BETWEEN 1 AND 5),
  tags       text[] NOT NULL DEFAULT '{}',
  block_id   text,
  comment    text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id    uuid REFERENCES users(id) ON DELETE SET NULL,
  action     text NOT NULL,
  entity     text,
  entity_id  text,
  ip         inet,
  user_agent text,
  meta       jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_user_idx ON audit_log (user_id, created_at DESC);

CREATE TABLE takedown_requests (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  reporter_name   text,
  reporter_email  text NOT NULL,
  report_id       uuid REFERENCES reports(id) ON DELETE SET NULL,
  description     text NOT NULL,
  status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open','investigating','actioned','rejected')),
  resolution_note text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  resolved_at     timestamptz
);

CREATE TABLE prompt_versions (
  prompt_id      text NOT NULL,
  version        text NOT NULL,
  content_sha256 text NOT NULL,
  note           text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (prompt_id, version)
);

-- =====================================================================================
-- HÀM NGHIỆP VỤ (đều có test trong tests/pg_smoke.py)
-- =====================================================================================

-- Số dư tín dụng
CREATE FUNCTION credit_balance(p_user uuid) RETURNS integer LANGUAGE sql STABLE AS $$
  SELECT COALESCE(SUM(delta), 0)::integer FROM credit_ledger WHERE user_id = p_user
$$;

-- Trừ tín dụng khi tạo job. Khoá hàng users để tránh đua; idempotent theo (user, p_idem).
CREATE FUNCTION charge_credits(p_user uuid, p_amount integer, p_job uuid, p_idem text) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  v_balance integer;
BEGIN
  IF p_amount <= 0 THEN
    RAISE EXCEPTION 'amount_must_be_positive' USING ERRCODE = 'P0001';
  END IF;
  PERFORM 1 FROM users WHERE id = p_user FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'user_not_found' USING ERRCODE = 'P0001';
  END IF;
  v_balance := credit_balance(p_user);
  IF p_idem IS NOT NULL AND EXISTS (SELECT 1 FROM credit_ledger WHERE user_id = p_user AND idempotency_key = p_idem) THEN
    RETURN v_balance;  -- lần gọi lặp: không trừ thêm
  END IF;
  IF v_balance < p_amount THEN
    RAISE EXCEPTION 'insufficient_credits' USING ERRCODE = 'P0001';
  END IF;
  INSERT INTO credit_ledger (user_id, delta, reason, job_id, idempotency_key)
  VALUES (p_user, -p_amount, 'job_charge', p_job, p_idem);
  IF p_job IS NOT NULL THEN
    UPDATE jobs SET charged_credits = charged_credits + p_amount WHERE id = p_job;
  END IF;
  RETURN v_balance - p_amount;
END $$;

-- Hoàn tín dụng (thất bại/huỷ/chất lượng hạng C). Không bao giờ hoàn quá số đã trừ cho job.
CREATE FUNCTION refund_credits(p_user uuid, p_amount integer, p_job uuid, p_idem text) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  v_refundable integer;
  v_refund integer;
BEGIN
  PERFORM 1 FROM users WHERE id = p_user FOR UPDATE;
  IF p_idem IS NOT NULL AND EXISTS (SELECT 1 FROM credit_ledger WHERE user_id = p_user AND idempotency_key = p_idem) THEN
    RETURN 0;
  END IF;
  SELECT COALESCE(-SUM(delta), 0)::integer INTO v_refundable
    FROM credit_ledger WHERE job_id = p_job AND reason IN ('job_charge','job_refund');
  v_refund := LEAST(p_amount, v_refundable);
  IF v_refund <= 0 THEN
    RETURN 0;
  END IF;
  INSERT INTO credit_ledger (user_id, delta, reason, job_id, idempotency_key)
  VALUES (p_user, v_refund, 'job_refund', p_job, p_idem);
  UPDATE jobs SET refunded_credits = refunded_credits + v_refund WHERE id = p_job;
  RETURN v_refund;
END $$;

-- Đổi mã mời lấy tín dụng (nguyên tử; mỗi người dùng đổi một mã tối đa một lần)
CREATE FUNCTION redeem_invite(p_code text, p_user uuid) RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  c invite_codes%ROWTYPE;
BEGIN
  SELECT * INTO c FROM invite_codes WHERE code = upper(p_code) FOR UPDATE;
  IF NOT FOUND OR c.revoked_at IS NOT NULL OR (c.expires_at IS NOT NULL AND c.expires_at < now()) THEN
    RAISE EXCEPTION 'invite_invalid' USING ERRCODE = 'P0001';
  END IF;
  IF c.used_count >= c.max_uses THEN
    RAISE EXCEPTION 'invite_exhausted' USING ERRCODE = 'P0001';
  END IF;
  INSERT INTO invite_redemptions (code, user_id) VALUES (c.code, p_user);  -- vi phạm PK nếu đã đổi
  UPDATE invite_codes SET used_count = used_count + 1 WHERE code = c.code;
  INSERT INTO credit_ledger (user_id, delta, reason, idempotency_key, meta)
  VALUES (p_user, c.credits_grant, 'grant_invite', 'invite:' || c.code, jsonb_build_object('code', c.code));
  RETURN c.credits_grant;
END $$;

-- Chi phí một lần gọi LLM theo bảng giá có hiệu lực tại p_day. tokens_in đã gồm tokens_cached.
CREATE FUNCTION llm_cost_usd(p_model text, p_day date, p_in bigint, p_cached bigint, p_out bigint, p_think bigint, p_batch boolean DEFAULT false)
RETURNS numeric LANGUAGE plpgsql STABLE AS $$
DECLARE
  r llm_prices%ROWTYPE;
  mult numeric;
BEGIN
  SELECT * INTO r FROM llm_prices WHERE model = p_model AND effective_from <= p_day ORDER BY effective_from DESC LIMIT 1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no_price_for_model' USING ERRCODE = 'P0001';
  END IF;
  mult := CASE WHEN p_batch THEN r.batch_multiplier ELSE 1 END;
  RETURN round(
    ((GREATEST(p_in - p_cached, 0) * r.input_per_mtok
      + p_cached * COALESCE(r.cached_input_per_mtok, r.input_per_mtok)
      + (p_out + p_think) * r.output_per_mtok) / 1000000.0) * mult, 6);
END $$;

-- Cộng chi phí vào sổ theo ngày (UTC) và bật cờ paused khi chạm trần. Trả về dòng sau cập nhật.
CREATE FUNCTION add_spend(p_cost numeric) RETURNS spend_daily LANGUAGE plpgsql AS $$
DECLARE
  v_cap numeric;
  r spend_daily;
BEGIN
  SELECT (value #>> '{}')::numeric INTO v_cap FROM app_settings WHERE key = 'daily_spend_cap_usd';
  INSERT INTO spend_daily AS s (day, cost_usd, cap_usd, paused)
  VALUES ((now() AT TIME ZONE 'UTC')::date, p_cost, COALESCE(v_cap, 20), p_cost >= COALESCE(v_cap, 20))
  ON CONFLICT (day) DO UPDATE
    SET cost_usd = s.cost_usd + EXCLUDED.cost_usd,
        paused = (s.cost_usd + EXCLUDED.cost_usd) >= s.cap_usd,
        updated_at = now()
  RETURNING * INTO r;
  RETURN r;
END $$;

-- Nhận task từ hàng đợi: SKIP LOCKED + giới hạn đồng thời theo job. Khoá advisory theo job
-- để việc đếm 'running' và gán 'running' không bị đua giữa nhiều worker.
CREATE FUNCTION claim_tasks(p_worker text, p_limit integer, p_per_job_limit integer) RETURNS SETOF job_tasks
LANGUAGE plpgsql AS $$
DECLARE
  r job_tasks%ROWTYPE;
  v_running integer;
  v_claimed integer := 0;
BEGIN
  FOR r IN
    SELECT t.* FROM job_tasks t
      JOIN jobs j ON j.id = t.job_id AND j.status = 'running' AND NOT j.cancel_requested
     WHERE t.status = 'pending' AND t.run_after <= now()
     ORDER BY t.created_at, t.id
       FOR UPDATE OF t SKIP LOCKED
     LIMIT 500
  LOOP
    EXIT WHEN v_claimed >= p_limit;
    IF pg_try_advisory_xact_lock(hashtextextended(r.job_id::text, 0)) THEN
      SELECT count(*) INTO v_running FROM job_tasks WHERE job_id = r.job_id AND status = 'running';
      IF v_running < p_per_job_limit THEN
        UPDATE job_tasks
           SET status = 'running', attempt = attempt + 1, locked_by = p_worker, locked_at = now(), heartbeat_at = now()
         WHERE id = r.id
        RETURNING * INTO r;
        v_claimed := v_claimed + 1;
        RETURN NEXT r;
      END IF;
    END IF;
  END LOOP;
  RETURN;
END $$;

-- Thu hồi task bị 'mồ côi' (worker chết): quá hạn heartbeat thì trả về pending, hoặc failed nếu hết lượt thử.
CREATE FUNCTION reclaim_stale_tasks(p_stale interval DEFAULT interval '3 minutes') RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  n integer;
BEGIN
  UPDATE job_tasks
     SET status = CASE WHEN attempt >= max_attempts THEN 'failed' ELSE 'pending' END,
         error_code = 'lease_expired',
         locked_by = NULL,
         finished_at = CASE WHEN attempt >= max_attempts THEN now() ELSE NULL END
   WHERE status = 'running' AND heartbeat_at < now() - p_stale;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Xoá nội dung gốc của tài liệu hết hạn (giữ bản ghi + trích đoạn bằng chứng ngắn trong knowledge_units
-- để báo cáo vẫn hiển thị được trích dẫn). Worker xoá thêm file trong object storage theo storage_key.
CREATE FUNCTION purge_expired_documents() RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  n integer;
BEGIN
  WITH d AS (
    UPDATE documents SET status = 'deleted', deleted_at = now()
     WHERE expires_at IS NOT NULL AND expires_at < now() AND status <> 'deleted'
    RETURNING id
  ), s AS (
    DELETE FROM doc_sections ds USING d WHERE ds.document_id = d.id
  ), p AS (
    DELETE FROM doc_paragraphs dp USING d WHERE dp.document_id = d.id
  )
  SELECT count(*) INTO n FROM d;
  RETURN n;
END $$;

-- =====================================================================================
-- LLM POOL: hàm đặt chỗ/ghi nhận NGUYÊN TỬ (SPEC §17.6). Chính sách chọn deployment nằm ở ứng dụng (reference/llm_pool.py: Router);
-- các hàm dưới đây chỉ giữ phần phải đúng dưới tranh chấp: token-bucket, bộ đếm ngày, đồng ý lock theo thứ tự (group -> deployment),
-- cooldown, circuit breaker, lease. Ngữ nghĩa trùng khít MemoryState (test so khớp trong tests/pg_smoke.py).
-- =====================================================================================

-- Mốc 00:00 ngày kế tiếp theo múi giờ p_tz (epoch giây).
CREATE FUNCTION pool_next_day_boundary(p_now double precision, p_tz text) RETURNS double precision
LANGUAGE sql STABLE AS $$
  SELECT extract(epoch FROM (((to_timestamp(p_now) AT TIME ZONE p_tz)::date + 1)::timestamp AT TIME ZONE p_tz))::double precision
$$;

-- Giữ hàm hợp nhất 'lấy lớn nhất, hoà thì lấy cái xuất hiện trước' dùng khi gom các lý do phải chờ.
CREATE FUNCTION pool__upd(INOUT w double precision, INOUT r text, nw double precision, nr text)
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN nw > w THEN nw ELSE w END, CASE WHEN nw > w THEN nr ELSE r END
$$;

-- Khoá hàng trạng thái, nạp đầy theo thời gian trôi qua, đặt lại bộ đếm khi sang ngày mới (theo múi giờ của nhà cung cấp).
CREATE FUNCTION pool_touch_scope(p_scope text, p_id text, p_rpm integer, p_tpm integer, p_margin double precision, p_tz text, p_now double precision)
RETURNS llm_scope_state LANGUAGE plpgsql AS $$
DECLARE
  s llm_scope_state;
  cap_rpm double precision;
  cap_tpm double precision;
  dt double precision;
  dk date;
BEGIN
  INSERT INTO llm_scope_state (scope, scope_id, bucket_at, day_key)
  VALUES (p_scope, p_id, p_now, (to_timestamp(p_now) AT TIME ZONE p_tz)::date)
  ON CONFLICT (scope, scope_id) DO NOTHING;
  SELECT * INTO s FROM llm_scope_state WHERE scope = p_scope AND scope_id = p_id FOR UPDATE;
  cap_rpm := CASE WHEN p_rpm IS NULL THEN NULL ELSE greatest(1.0, p_rpm * p_margin * s.limit_scale) END;
  cap_tpm := CASE WHEN p_tpm IS NULL THEN NULL ELSE p_tpm * p_margin * s.limit_scale END;
  dt := greatest(p_now - s.bucket_at, 0.0);
  IF cap_rpm IS NOT NULL THEN
    s.rpm_level := CASE WHEN s.rpm_level IS NULL THEN cap_rpm ELSE least(cap_rpm, s.rpm_level + cap_rpm / 60.0 * dt) END;
  END IF;
  IF cap_tpm IS NOT NULL THEN
    s.tpm_level := CASE WHEN s.tpm_level IS NULL THEN cap_tpm ELSE least(cap_tpm, s.tpm_level + cap_tpm / 60.0 * dt) END;
  END IF;
  s.bucket_at := p_now;
  dk := (to_timestamp(p_now) AT TIME ZONE p_tz)::date;
  IF s.day_key IS DISTINCT FROM dk THEN
    s.day_key := dk; s.rpd_used := 0; s.tpd_used := 0;
  END IF;
  UPDATE llm_scope_state SET rpm_level = s.rpm_level, tpm_level = s.tpm_level, bucket_at = s.bucket_at,
         day_key = s.day_key, rpd_used = s.rpd_used, tpd_used = s.tpd_used
   WHERE scope = p_scope AND scope_id = p_id;
  RETURN s;
END $$;

-- Ghi lại toàn bộ trạng thái của một scope sau khi sửa trong bộ nhớ.
CREATE FUNCTION pool__save(s llm_scope_state) RETURNS void LANGUAGE sql AS $$
  UPDATE llm_scope_state SET rpm_level = s.rpm_level, tpm_level = s.tpm_level, bucket_at = s.bucket_at, day_key = s.day_key,
         rpd_used = s.rpd_used, tpd_used = s.tpd_used, inflight = s.inflight, limit_scale = s.limit_scale,
         cooldown_until = s.cooldown_until, circuit = s.circuit, consecutive_failures = s.consecutive_failures,
         consecutive_429 = s.consecutive_429, trips = s.trips, ewma_success = s.ewma_success, ewma_valid = s.ewma_valid,
         ewma_latency_ms = s.ewma_latency_ms, last_used_at = s.last_used_at
   WHERE scope = s.scope AND scope_id = s.scope_id
$$;

-- Kiểm tra các giới hạn của MỘT scope; trả về thời gian chờ lớn nhất (-1 nếu đủ chỗ), lý do, và cờ 'too_large'.
CREATE FUNCTION pool__check_scope(s llm_scope_state, p_label text, p_rpm integer, p_tpm integer, p_rpd integer, p_tpd bigint, p_conc integer,
                                  p_margin double precision, p_day_margin double precision, p_tz text, p_basis integer,
                                  p_res double precision, p_now double precision)
RETURNS TABLE (wait double precision, reason text, too_large boolean) LANGUAGE plpgsql AS $$
DECLARE
  cap_rpm double precision; cap_tpm double precision; lim_rpd bigint; lim_tpd bigint;
  nxt double precision; need double precision; w double precision := -1; r text := '';
BEGIN
  cap_rpm := CASE WHEN p_rpm IS NULL THEN NULL ELSE greatest(1.0, p_rpm * p_margin * s.limit_scale) END;
  cap_tpm := CASE WHEN p_tpm IS NULL THEN NULL ELSE p_tpm * p_margin * s.limit_scale END;
  lim_rpd := CASE WHEN p_rpd IS NULL THEN NULL ELSE greatest(1, floor(p_rpd * p_day_margin)) END;
  lim_tpd := CASE WHEN p_tpd IS NULL THEN NULL ELSE greatest(1, floor(p_tpd * p_day_margin)) END;
  IF cap_tpm IS NOT NULL AND p_basis > cap_tpm THEN
    RETURN QUERY SELECT 'Infinity'::double precision, 'too_large'::text, true; RETURN;
  END IF;
  IF lim_tpd IS NOT NULL AND p_basis > lim_tpd THEN
    RETURN QUERY SELECT 'Infinity'::double precision, 'too_large'::text, true; RETURN;
  END IF;
  nxt := pool_next_day_boundary(p_now, p_tz);
  IF cap_rpm IS NOT NULL THEN
    need := 1.0 + p_res * cap_rpm;
    IF s.rpm_level < need THEN SELECT * INTO w, r FROM pool__upd(w, r, (need - s.rpm_level) / (cap_rpm / 60.0), p_label || '_rpm'); END IF;
  END IF;
  IF cap_tpm IS NOT NULL THEN
    need := p_basis + p_res * cap_tpm;
    IF s.tpm_level < need THEN SELECT * INTO w, r FROM pool__upd(w, r, (need - s.tpm_level) / (cap_tpm / 60.0), p_label || '_tpm'); END IF;
  END IF;
  IF lim_rpd IS NOT NULL AND s.rpd_used + 1 > floor(lim_rpd * (1 - p_res)) THEN
    SELECT * INTO w, r FROM pool__upd(w, r, nxt - p_now, p_label || '_rpd');
  END IF;
  IF lim_tpd IS NOT NULL AND s.tpd_used + p_basis > floor(lim_tpd * (1 - p_res)) THEN
    SELECT * INTO w, r FROM pool__upd(w, r, nxt - p_now, p_label || '_tpd');
  END IF;
  IF p_conc IS NOT NULL AND s.inflight >= p_conc THEN
    SELECT * INTO w, r FROM pool__upd(w, r, 1.0, p_label || '_concurrency');
  END IF;
  RETURN QUERY SELECT w, r, false;
END $$;

-- Đặt chỗ cho MỘT lời gọi vào MỘT deployment. Trả ok = false kèm wait_s (giây, 'Infinity' nếu không bao giờ vừa) và lý do.
-- p_priority: 'high' | 'normal' | 'low'. Tác vụ 'low' không được dùng p_reserve phần hạn mức cuối cùng.
CREATE FUNCTION pool_try_reserve(p_dep text, p_in integer, p_out integer, p_priority text, p_now double precision,
                                 p_reserve double precision DEFAULT 0.2, p_ttl double precision DEFAULT 300)
RETURNS TABLE (ok boolean, lease_id bigint, credential_id text, wait_s double precision, reason text, basis_tokens integer)
LANGUAGE plpgsql AS $$
DECLARE
  d llm_deployments%ROWTYPE;
  g llm_quota_groups%ROWTYPE;
  sg llm_scope_state;
  sd llm_scope_state;
  v_basis integer;
  v_res double precision;
  v_cred text;
  v_wait double precision := -1;
  v_reason text := '';
  c record;
  v_id bigint;
BEGIN
  SELECT * INTO d FROM llm_deployments WHERE id = p_dep;
  IF NOT FOUND THEN RAISE EXCEPTION 'deployment_not_found' USING ERRCODE = 'P0001'; END IF;
  SELECT * INTO g FROM llm_quota_groups WHERE id = d.group_id;
  -- Thứ tự khoá cố định: group trước, deployment sau (tránh deadlock giữa các worker).
  sg := pool_touch_scope('group', g.id, g.rpm, g.tpm, g.safety_margin, g.reset_tz, p_now);
  sd := pool_touch_scope('deployment', d.id, d.rpm, d.tpm, g.safety_margin, g.reset_tz, p_now);
  v_basis := CASE WHEN d.tpm_basis = 'input' THEN p_in ELSE p_in + p_out END;
  v_res := CASE WHEN p_priority = 'low' THEN p_reserve ELSE 0 END;

  SELECT cr.id INTO v_cred FROM llm_credentials cr WHERE cr.group_id = g.id AND cr.status = 'active'
   ORDER BY cr.last_used_at NULLS FIRST, cr.id LIMIT 1;
  IF v_cred IS NULL THEN
    RETURN QUERY SELECT false, NULL::bigint, NULL::text, 'Infinity'::double precision, 'no_credential'::text, 0; RETURN;
  END IF;

  IF sd.circuit = 'open' AND sd.cooldown_until <= p_now THEN
    sd.circuit := 'half_open';
    PERFORM pool__save(sd);
  END IF;
  IF sg.cooldown_until > p_now THEN SELECT * INTO v_wait, v_reason FROM pool__upd(v_wait, v_reason, sg.cooldown_until - p_now, 'group_cooldown'); END IF;
  IF sd.cooldown_until > p_now THEN SELECT * INTO v_wait, v_reason FROM pool__upd(v_wait, v_reason, sd.cooldown_until - p_now, 'deployment_cooldown'); END IF;
  IF sd.circuit = 'half_open' AND sd.inflight > 0 THEN SELECT * INTO v_wait, v_reason FROM pool__upd(v_wait, v_reason, 1.0, 'probing'); END IF;

  SELECT * INTO c FROM pool__check_scope(sg, 'group', g.rpm, g.tpm, g.rpd, g.tpd, g.concurrency, g.safety_margin, g.day_margin, g.reset_tz, v_basis, v_res, p_now);
  IF c.too_large THEN RETURN QUERY SELECT false, NULL::bigint, NULL::text, 'Infinity'::double precision, 'too_large'::text, 0; RETURN; END IF;
  SELECT * INTO v_wait, v_reason FROM pool__upd(v_wait, v_reason, c.wait, c.reason);
  SELECT * INTO c FROM pool__check_scope(sd, 'deployment', d.rpm, d.tpm, d.rpd, d.tpd, d.concurrency, g.safety_margin, g.day_margin, g.reset_tz, v_basis, v_res, p_now);
  IF c.too_large THEN RETURN QUERY SELECT false, NULL::bigint, NULL::text, 'Infinity'::double precision, 'too_large'::text, 0; RETURN; END IF;
  SELECT * INTO v_wait, v_reason FROM pool__upd(v_wait, v_reason, c.wait, c.reason);

  IF v_wait >= 0 THEN
    RETURN QUERY SELECT false, NULL::bigint, NULL::text, v_wait, v_reason, 0; RETURN;
  END IF;

  UPDATE llm_scope_state SET rpm_level = rpm_level - 1.0, tpm_level = tpm_level - v_basis, rpd_used = rpd_used + 1,
         tpd_used = tpd_used + v_basis, inflight = inflight + 1, last_used_at = p_now
   WHERE (scope = 'group' AND scope_id = g.id) OR (scope = 'deployment' AND scope_id = d.id);
  UPDATE llm_credentials SET last_used_at = to_timestamp(p_now) WHERE id = v_cred;
  INSERT INTO llm_leases (deployment_id, credential_id, basis_tokens, out_tokens, priority, created_at, expires_at)
  VALUES (d.id, v_cred, v_basis, p_out, p_priority, p_now, p_now + p_ttl) RETURNING id INTO v_id;
  RETURN QUERY SELECT true, v_id, v_cred, 0::double precision, ''::text, v_basis;
END $$;

-- Ghi nhận kết quả một lời gọi: trả chỗ, hoàn/bù token, cập nhật sức khoẻ, cooldown, circuit breaker, khoá bị từ chối.
-- p_kind: ok | rate_limited_minute | rate_limited_day | rate_limited_unknown | server_error | timeout | network_error | auth_error |
--         bad_request | context_exceeded | safety_blocked | invalid_output | truncated | canceled
CREATE FUNCTION pool_settle(p_lease bigint, p_kind text, p_in integer, p_out integer, p_latency_ms integer,
                            p_retry_after double precision, p_scope text, p_now double precision) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  l llm_leases%ROWTYPE;
  d llm_deployments%ROWTYPE;
  g llm_quota_groups%ROWTYPE;
  sg llm_scope_state;
  sd llm_scope_state;
  t llm_scope_state;
  v_actual integer;
  v_diff integer;
  cap_tpm double precision;
BEGIN
  DELETE FROM llm_leases WHERE id = p_lease RETURNING * INTO l;
  IF NOT FOUND THEN RETURN; END IF;
  SELECT * INTO d FROM llm_deployments WHERE id = l.deployment_id;
  SELECT * INTO g FROM llm_quota_groups WHERE id = d.group_id;
  sg := pool_touch_scope('group', g.id, g.rpm, g.tpm, g.safety_margin, g.reset_tz, p_now);
  sd := pool_touch_scope('deployment', d.id, d.rpm, d.tpm, g.safety_margin, g.reset_tz, p_now);
  sg.inflight := greatest(0, sg.inflight - 1);
  sd.inflight := greatest(0, sd.inflight - 1);

  v_actual := CASE WHEN p_kind = 'ok' THEN (CASE WHEN d.tpm_basis = 'input' THEN p_in ELSE p_in + p_out END) ELSE l.basis_tokens END;
  v_diff := l.basis_tokens - v_actual;
  cap_tpm := CASE WHEN g.tpm IS NULL THEN NULL ELSE g.tpm * g.safety_margin * sg.limit_scale END;
  IF cap_tpm IS NOT NULL AND v_diff <> 0 THEN sg.tpm_level := greatest(0.0, least(cap_tpm, sg.tpm_level + v_diff)); END IF;
  cap_tpm := CASE WHEN d.tpm IS NULL THEN NULL ELSE d.tpm * g.safety_margin * sd.limit_scale END;
  IF cap_tpm IS NOT NULL AND v_diff <> 0 THEN sd.tpm_level := greatest(0.0, least(cap_tpm, sd.tpm_level + v_diff)); END IF;
  sg.tpd_used := greatest(0, sg.tpd_used - v_diff);
  sd.tpd_used := greatest(0, sd.tpd_used - v_diff);

  IF p_kind = 'ok' THEN
    sd.consecutive_failures := 0;
    sd.consecutive_429 := 0;
    IF sd.circuit = 'half_open' THEN sd.trips := 0; sd.circuit := 'closed'; END IF;
    sd.ewma_success := 0.9 * sd.ewma_success + 0.1;
    IF p_latency_ms IS NOT NULL THEN
      sd.ewma_latency_ms := CASE WHEN sd.ewma_latency_ms IS NULL THEN p_latency_ms ELSE 0.8 * sd.ewma_latency_ms + 0.2 * p_latency_ms END;
    END IF;
    sd.limit_scale := least(1.0, sd.limit_scale + 0.05);
    sg.limit_scale := least(1.0, sg.limit_scale + 0.05);
  ELSIF p_kind IN ('rate_limited_minute', 'rate_limited_day', 'rate_limited_unknown') THEN
    t := CASE WHEN p_scope = 'group' THEN sg ELSE sd END;
    t.limit_scale := greatest(0.3, t.limit_scale * 0.85);
    IF p_kind = 'rate_limited_minute' THEN
      t.consecutive_429 := 0;
      t.cooldown_until := greatest(t.cooldown_until, p_now + coalesce(p_retry_after, 30.0));
    ELSIF p_kind = 'rate_limited_day' THEN
      t.cooldown_until := greatest(t.cooldown_until, pool_next_day_boundary(p_now, g.reset_tz) + 30.0);
    ELSE
      t.consecutive_429 := t.consecutive_429 + 1;
      t.cooldown_until := greatest(t.cooldown_until, p_now + greatest(least(600.0, 30.0 * power(2, t.consecutive_429 - 1)), coalesce(p_retry_after, 0.0)));
    END IF;
    IF t.rpm_level IS NOT NULL THEN t.rpm_level := 0.0; END IF;
    IF t.tpm_level IS NOT NULL THEN t.tpm_level := 0.0; END IF;
    IF p_scope = 'group' THEN sg := t; ELSE sd := t; END IF;
    INSERT INTO llm_incidents (deployment_id, credential_id, kind, detail) VALUES (d.id, l.credential_id, p_kind, jsonb_build_object('scope', p_scope));
  ELSIF p_kind IN ('server_error', 'timeout', 'network_error') THEN
    sd.consecutive_failures := sd.consecutive_failures + 1;
    sd.ewma_success := 0.9 * sd.ewma_success;
    IF sd.circuit = 'half_open' OR sd.consecutive_failures >= 3 THEN
      sd.trips := sd.trips + 1;
      sd.circuit := 'open';
      sd.cooldown_until := greatest(sd.cooldown_until, p_now + least(600.0, 15.0 * power(2, sd.trips - 1)));
      INSERT INTO llm_incidents (deployment_id, kind, detail) VALUES (d.id, 'circuit_open', jsonb_build_object('cause', p_kind));
    END IF;
  ELSIF p_kind = 'auth_error' THEN
    UPDATE llm_credentials SET status = 'quarantined', quarantined_reason = 'auth_error' WHERE id = l.credential_id;
    INSERT INTO llm_incidents (deployment_id, credential_id, kind) VALUES (d.id, l.credential_id, 'auth_error');
  ELSIF p_kind = 'invalid_output' THEN
    sd.ewma_valid := 0.95 * sd.ewma_valid;
  END IF;
  PERFORM pool__save(sg);
  PERFORM pool__save(sd);
END $$;

-- Thu hồi lease quá hạn (worker chết giữa chừng) để không bị kẹt 'inflight'. Chạy định kỳ (mỗi phút) cùng reclaim_stale_tasks().
CREATE FUNCTION pool_reap_leases(p_now double precision) RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  l llm_leases%ROWTYPE;
  n integer := 0;
  v_group text;
BEGIN
  FOR l IN SELECT * FROM llm_leases WHERE expires_at <= p_now ORDER BY id FOR UPDATE SKIP LOCKED LOOP
    SELECT group_id INTO v_group FROM llm_deployments WHERE id = l.deployment_id;
    UPDATE llm_scope_state SET inflight = greatest(0, inflight - 1)
     WHERE (scope = 'group' AND scope_id = v_group) OR (scope = 'deployment' AND scope_id = l.deployment_id);
    DELETE FROM llm_leases WHERE id = l.id;
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- Ảnh chụp để chấm điểm/hiển thị: headroom 0..1 = phần hạn mức còn lại ít nhất trong mọi chiều (cả hai scope).
CREATE FUNCTION pool_snapshot(p_dep text, p_now double precision)
RETURNS TABLE (headroom double precision, ewma_success double precision, inflight integer, cooldown_until double precision, circuit text, last_used_at double precision)
LANGUAGE plpgsql AS $$
DECLARE
  d llm_deployments%ROWTYPE;
  g llm_quota_groups%ROWTYPE;
  sg llm_scope_state;
  sd llm_scope_state;
  h double precision := 1.0;
  cap double precision;
  lim bigint;
  i integer;
  s llm_scope_state;
  rpm integer; tpm integer; rpd integer; tpd bigint; conc integer;
BEGIN
  SELECT * INTO d FROM llm_deployments WHERE id = p_dep;
  SELECT * INTO g FROM llm_quota_groups WHERE id = d.group_id;
  sg := pool_touch_scope('group', g.id, g.rpm, g.tpm, g.safety_margin, g.reset_tz, p_now);
  sd := pool_touch_scope('deployment', d.id, d.rpm, d.tpm, g.safety_margin, g.reset_tz, p_now);
  FOR i IN 1..2 LOOP
    IF i = 1 THEN s := sg; rpm := g.rpm; tpm := g.tpm; rpd := g.rpd; tpd := g.tpd; conc := g.concurrency;
    ELSE s := sd; rpm := d.rpm; tpm := d.tpm; rpd := d.rpd; tpd := d.tpd; conc := d.concurrency; END IF;
    IF rpm IS NOT NULL THEN cap := greatest(1.0, rpm * g.safety_margin * s.limit_scale); h := least(h, s.rpm_level / cap); END IF;
    IF tpm IS NOT NULL THEN cap := tpm * g.safety_margin * s.limit_scale; h := least(h, s.tpm_level / cap); END IF;
    IF rpd IS NOT NULL THEN lim := greatest(1, floor(rpd * g.day_margin)); h := least(h, 1 - s.rpd_used::double precision / lim); END IF;
    IF tpd IS NOT NULL THEN lim := greatest(1, floor(tpd * g.day_margin)); h := least(h, 1 - s.tpd_used::double precision / lim); END IF;
    IF conc IS NOT NULL THEN h := least(h, 1 - s.inflight::double precision / conc); END IF;
  END LOOP;
  RETURN QUERY SELECT greatest(0.0, h), sd.ewma_success, sd.inflight, greatest(sg.cooldown_until, sd.cooldown_until), sd.circuit, sd.last_used_at;
END $$;

-- Chi phí một lời gọi theo deployment: tiền thật (chỉ khi metered) và chi phí bóng (giá tham chiếu, kể cả deployment miễn phí).
CREATE FUNCTION pool_call_cost(p_dep text, p_day date, p_in bigint, p_cached bigint, p_out bigint, p_think bigint, p_batch boolean DEFAULT false)
RETURNS TABLE (cost_usd numeric, shadow_usd numeric) LANGUAGE plpgsql STABLE AS $$
DECLARE
  d llm_deployments%ROWTYPE;
  m llm_models%ROWTYPE;
  real_c numeric := 0;
  shadow numeric := 0;
BEGIN
  SELECT * INTO d FROM llm_deployments WHERE id = p_dep;
  SELECT * INTO m FROM llm_models WHERE id = d.model_id;
  IF d.price_mode = 'metered' AND m.price_key IS NOT NULL THEN
    real_c := llm_cost_usd(m.price_key, p_day, p_in, p_cached, p_out, p_think, p_batch);
  END IF;
  IF m.shadow_price_key IS NOT NULL THEN
    shadow := llm_cost_usd(m.shadow_price_key, p_day, p_in, p_cached, p_out, p_think, p_batch);
  ELSE
    shadow := real_c;
  END IF;
  RETURN QUERY SELECT real_c, shadow;
END $$;

-- Hoãn một task đang chạy vì pool chưa có chỗ: trả về pending với run_after, KHÔNG tính vào số lần thử (attempt - 1).
CREATE FUNCTION defer_task(p_task bigint, p_until timestamptz) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE
  n integer;
BEGIN
  UPDATE job_tasks
     SET status = 'pending', run_after = p_until, attempt = greatest(attempt - 1, 0),
         locked_by = NULL, locked_at = NULL, heartbeat_at = NULL
   WHERE id = p_task AND status = 'running';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n = 1;
END $$;

-- Phát hành glossary: chụp các mục 'confirmed' thành bản bất biến, tăng số hiệu. Người duyệt (curator/admin) gọi sau khi xử lý hàng đợi.
CREATE FUNCTION publish_glossary_release(p_glossary uuid, p_by uuid, p_note text DEFAULT NULL) RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  v_next integer;
  v_entries jsonb;
BEGIN
  PERFORM 1 FROM glossaries WHERE id = p_glossary FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'glossary_not_found' USING ERRCODE = 'P0001'; END IF;
  SELECT COALESCE(max(version), 0) + 1 INTO v_next FROM glossary_releases WHERE glossary_id = p_glossary;
  SELECT COALESCE(jsonb_agg(jsonb_build_object(
           'source_term', source_term, 'target_term', target_term, 'keep_original', keep_original,
           'case_sensitive', case_sensitive, 'forbidden_variants', to_jsonb(forbidden_variants),
           'term_type', term_type, 'note', COALESCE(note, '')) ORDER BY lower(source_term)), '[]'::jsonb)
    INTO v_entries FROM glossary_entries WHERE glossary_id = p_glossary AND status = 'confirmed';
  INSERT INTO glossary_releases (glossary_id, version, entries, entry_count, content_sha256, released_by, note)
  VALUES (p_glossary, v_next, v_entries, jsonb_array_length(v_entries), encode(sha256(convert_to(v_entries::text, 'UTF8')), 'hex'), p_by, p_note);
  UPDATE glossaries SET version = v_next WHERE id = p_glossary;
  RETURN v_next;
END $$;

-- Phiên bản Lõi văn phong đã duyệt là BẤT BIẾN: muốn sửa phải tạo phiên bản mới (copy-on-write). Chỉ được chuyển approved -> deprecated.
CREATE FUNCTION style_core_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status = 'approved' THEN RAISE EXCEPTION 'approved_version_is_immutable' USING ERRCODE = 'P0001'; END IF;
    RETURN OLD;
  END IF;
  IF OLD.status = 'approved' THEN
    IF NEW.content IS DISTINCT FROM OLD.content OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
       OR NEW.version IS DISTINCT FROM OLD.version OR NEW.style_core_id IS DISTINCT FROM OLD.style_core_id THEN
      RAISE EXCEPTION 'approved_version_is_immutable' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status NOT IN ('approved', 'deprecated') THEN
      RAISE EXCEPTION 'approved_version_cannot_be_reopened' USING ERRCODE = 'P0001';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER style_core_versions_guard BEFORE UPDATE OR DELETE ON style_core_versions FOR EACH ROW EXECUTE FUNCTION style_core_version_guard();

-- -------------------------------------------------------------------------------------
-- Giới hạn tốc độ (M3, §20.4): bộ đếm cửa sổ cố định dùng chung cho mọi tiến trình API.
-- Chỉ lưu KHOÁ BĂM của danh tính (không lưu IP thô): chống dò mật khẩu mà không lưu địa chỉ mạng.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rate_limit_hits (
  scope        text NOT NULL,
  subject      text NOT NULL,
  window_start timestamptz NOT NULL,
  hits         integer NOT NULL DEFAULT 0,
  PRIMARY KEY (scope, subject, window_start)
);
CREATE INDEX IF NOT EXISTS rate_limit_window_idx ON rate_limit_hits (window_start);

CREATE FUNCTION rate_limit_hit(
  p_scope text, p_subject text, p_limit integer, p_window_s integer
) RETURNS TABLE(allowed boolean, hits integer, remaining integer, retry_after_s integer)
LANGUAGE plpgsql AS $$
DECLARE
  v_start timestamptz;
  v_hits integer;
  v_retry integer;
BEGIN
  IF p_limit <= 0 OR p_window_s <= 0 THEN
    RETURN QUERY SELECT true, 0, 0, 0;
    RETURN;
  END IF;
  v_start := to_timestamp(floor(extract(epoch FROM now()) / p_window_s) * p_window_s);
  v_retry := GREATEST(1, CEIL(p_window_s - (extract(epoch FROM now()) - extract(epoch FROM v_start))))::integer;
  INSERT INTO rate_limit_hits (scope, subject, window_start, hits)
       VALUES (p_scope, p_subject, v_start, 1)
  ON CONFLICT (scope, subject, window_start)
    DO UPDATE SET hits = rate_limit_hits.hits + 1
    RETURNING rate_limit_hits.hits INTO v_hits;
  RETURN QUERY SELECT v_hits <= p_limit, v_hits, GREATEST(0, p_limit - v_hits), v_retry;
END $$;

CREATE FUNCTION rate_limit_gc(p_keep interval DEFAULT '1 hour')
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE v_deleted integer;
BEGIN
  DELETE FROM rate_limit_hits WHERE window_start < now() - p_keep;
  GET DIAGNOSTICS v_deleted = ROW_COUNT;
  RETURN v_deleted;
END $$;

-- =====================================================================================
-- (Tuỳ chọn) Row-Level Security như lớp phòng thủ thứ hai, nếu dùng role riêng cho API:
--   ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
--   CREATE POLICY documents_owner ON documents USING (user_id = current_setting('app.user_id')::uuid);
--   (lặp lại cho jobs, reports, glossaries(owner_id), credit_ledger...). API đặt app.user_id bằng SET LOCAL mỗi giao dịch.
-- =====================================================================================
