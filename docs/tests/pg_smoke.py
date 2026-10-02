#!/usr/bin/env python3
"""Kiểm thử khói cho db/schema.sql trên PostgreSQL thật.

Chạy:  python tests/pg_smoke.py postgresql://postgres@127.0.0.1:54329/postgres
(Giả định schema đã được nạp; tools/validate_spec.py --pg-dsn ... nạp rồi gọi file này.)
"""
import json
import math
import pathlib
import sys
import threading
import uuid

import psycopg
from psycopg import errors

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reference.llm_pool_pg import PgState, apply_declaration, existing_fingerprints, seed_pool  # noqa: E402
from reference.pool_declare import compile_all, merge_fragment  # noqa: E402
from reference.pool_secrets import decrypt_secret, fingerprint  # noqa: E402

dsn = sys.argv[1]
conn = psycopg.connect(dsn, autocommit=True)
passed = 0


def check(cond: bool, msg: str) -> None:
    global passed
    if not cond:
        raise AssertionError(msg)
    passed += 1
    print(f"  ok   {msg}")


def q1(sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def new_user(email: str) -> uuid.UUID:
    return q1("INSERT INTO users (email, status) VALUES (%s, 'active') RETURNING id", email)


def new_doc(user) -> uuid.UUID:
    return q1("INSERT INTO documents (user_id, title, source_type) VALUES (%s, 't', 'paste') RETURNING id", user)


def new_job(user, doc, status="running", credits=10) -> uuid.UUID:
    jid = q1(
        """INSERT INTO jobs (user_id, document_id, level, status, est_credits, model_profile, prompt_versions)
           VALUES (%s, %s, 'deep_synthesis', %s, %s, '{}', '{}') RETURNING id""",
        user, doc, status, credits,
    )
    for st in ("map", "write"):
        conn.execute("INSERT INTO job_stages (job_id, stage) VALUES (%s, %s)", (jid, st))
    return jid


def expect_error(sql, args, needle):
    try:
        conn.execute(sql, args)
    except psycopg.Error as e:
        return needle in str(e)
    return False


# ---------------------------------------------------------------- sổ tín dụng
print("[ledger]")
u = new_user("a@example.com")
conn.execute("INSERT INTO credit_ledger (user_id, delta, reason) VALUES (%s, 100, 'grant_signup')", (u,))
check(q1("SELECT credit_balance(%s)", u) == 100, "số dư ban đầu = 100")
d = new_doc(u)
j = new_job(u, d)
check(q1("SELECT charge_credits(%s, 30, %s, %s)", u, j, f"charge:{j}") == 70, "trừ 30 -> còn 70")
check(q1("SELECT charge_credits(%s, 30, %s, %s)", u, j, f"charge:{j}") == 70, "gọi lặp cùng idempotency key không trừ thêm")
check(q1("SELECT charged_credits FROM jobs WHERE id = %s", j) == 30, "jobs.charged_credits = 30")
check(expect_error("SELECT charge_credits(%s, 500, NULL, 'x')", (u,), "insufficient_credits"), "không đủ tín dụng -> lỗi insufficient_credits")
check(q1("SELECT balance FROM v_credit_balance WHERE user_id = %s", u) == 70, "view v_credit_balance khớp")
check(q1("SELECT refund_credits(%s, 100, %s, %s)", u, j, f"refund:{j}") == 30, "hoàn tối đa bằng số đã trừ (yêu cầu 100 -> 30)")
check(q1("SELECT refund_credits(%s, 5, %s, %s)", u, j, f"refund:{j}") == 0, "hoàn lặp cùng key = 0")
check(q1("SELECT credit_balance(%s)", u) == 100, "sau hoàn: số dư về 100")
check(expect_error("INSERT INTO credit_ledger (user_id, delta, reason) VALUES (%s, 0, 'admin_adjust')", (u,), "check"), "delta = 0 bị từ chối")

# ---------------------------------------------------------------- mã mời
print("[invite]")
conn.execute("INSERT INTO invite_codes (code, credits_grant, max_uses) VALUES ('BETA-0001', 200, 1)")
u2 = new_user("b@example.com")
check(q1("SELECT redeem_invite('beta-0001', %s)", u2) == 200, "đổi mã (không phân biệt hoa/thường) -> +200")
check(q1("SELECT credit_balance(%s)", u2) == 200, "tín dụng đã vào sổ")
u3 = new_user("c@example.com")
check(expect_error("SELECT redeem_invite('BETA-0001', %s)", (u3,), "invite_exhausted"), "mã hết lượt -> invite_exhausted")
check(expect_error("SELECT redeem_invite('KHONG-CO-MA', %s)", (u3,), "invite_invalid"), "mã không tồn tại -> invite_invalid")

# ---------------------------------------------------------------- giá LLM theo ngày hiệu lực
print("[llm cost]")
c1 = q1("SELECT llm_cost_usd('gemini-3.8-flash', DATE '2026-10-02', 1000000, 0, 1000000, 0)")
c2 = q1("SELECT llm_cost_usd('gemini-3.8-flash', DATE '2027-01-02', 1000000, 0, 1000000, 0)")
check(float(c1) == 4.5, f"1M vào + 1M ra ngày 2026-10-02 = $4.50 (được {c1})")
check(float(c2) == 9.0, f"cùng khối lượng ngày 2027-01-02 = $9.00 (được {c2})")
c3 = q1("SELECT llm_cost_usd('gemini-3.8-flash', DATE '2026-10-02', 1000000, 1000000, 0, 0)")
check(float(c3) == 0.075, f"1M token cached = $0.075 (được {c3})")
c4 = q1("SELECT llm_cost_usd('gemini-3.8-flash', DATE '2026-10-02', 1000000, 0, 1000000, 0, true)")
check(float(c4) == 2.25, f"Batch = 50% (được {c4})")
check(expect_error("SELECT llm_cost_usd('model-la', DATE '2026-10-02', 1, 0, 1, 0)", (), "no_price_for_model"), "model không có giá -> lỗi")
check(expect_error("SELECT llm_cost_usd('gemini-3.8-flash', DATE '2026-01-01', 1, 0, 1, 0)", (), "no_price_for_model"), "ngày trước hiệu lực -> lỗi")

# ---------------------------------------------------------------- trần chi tiêu
print("[spend guard]")
conn.execute("UPDATE app_settings SET value = '10' WHERE key = 'daily_spend_cap_usd'")
r = conn.execute("SELECT * FROM add_spend(6.0)").fetchone()
check(r[4] is False and float(r[1]) == 6.0, "chi 6.0 < trần 10: chưa paused")
r = conn.execute("SELECT * FROM add_spend(4.0)").fetchone()
check(r[4] is True and float(r[1]) == 10.0, "chi đủ 10.0: paused = true")

# ---------------------------------------------------------------- hàng đợi
print("[queue]")
ud = new_user("q@example.com")
dq = new_doc(ud)
jq = new_job(ud, dq)
for i in range(10):
    conn.execute("INSERT INTO job_tasks (job_id, stage, task_key) VALUES (%s, 'map', %s)", (jq, f"SEG-{i+1:03d}"))
jq2 = new_job(ud, dq)
for i in range(2):
    conn.execute("INSERT INTO job_tasks (job_id, stage, task_key) VALUES (%s, 'map', %s)", (jq2, f"SEG-{i+1:03d}"))
jq3 = new_job(ud, dq, status="queued")
conn.execute("INSERT INTO job_tasks (job_id, stage, task_key) VALUES (%s, 'map', 'SEG-001')", (jq3,))
got = conn.execute("SELECT job_id FROM claim_tasks('w1', 100, 3)").fetchall()
by_job = {}
for (jid,) in got:
    by_job[jid] = by_job.get(jid, 0) + 1
check(by_job.get(jq) == 3 and by_job.get(jq2) == 2, f"giới hạn 3 task đồng thời/job; job nhỏ lấy đủ 2 (được {sorted(by_job.values())})")
check(jq3 not in by_job, "job chưa 'running' không bị nhận task")
check(len(conn.execute("SELECT 1 FROM claim_tasks('w2', 100, 3)").fetchall()) == 0, "lần nhận thứ hai: không còn chỗ trống")
done_id = q1("SELECT id FROM job_tasks WHERE job_id = %s AND status = 'running' ORDER BY id LIMIT 1", jq)
conn.execute("UPDATE job_tasks SET status = 'succeeded', finished_at = now() WHERE id = %s", (done_id,))
check(len(conn.execute("SELECT 1 FROM claim_tasks('w2', 100, 3)").fetchall()) == 1, "xong 1 task -> nhận thêm đúng 1")
conn.execute("UPDATE jobs SET cancel_requested = true WHERE id = %s", (jq,))
conn.execute("UPDATE job_tasks SET status = 'succeeded' WHERE job_id = %s AND status = 'running'", (jq,))
check(len(conn.execute("SELECT 1 FROM claim_tasks('w3', 100, 3) WHERE job_id = %s", (jq,)).fetchall()) == 0, "job đã yêu cầu huỷ: không nhận thêm task")

print("[queue: reclaim]")
conn.execute("UPDATE job_tasks SET heartbeat_at = now() - interval '10 minutes' WHERE job_id = %s AND status = 'running'", (jq2,))
n = q1("SELECT reclaim_stale_tasks(interval '3 minutes')")
check(n == 2, f"thu hồi 2 task mất heartbeat (được {n})")
check(q1("SELECT count(*) FROM job_tasks WHERE job_id = %s AND status = 'pending'", jq2) == 2, "task thu hồi quay về pending")
conn.execute("UPDATE job_tasks SET status='running', attempt = max_attempts, heartbeat_at = now() - interval '10 minutes' WHERE job_id = %s AND task_key = 'SEG-001'", (jq2,))
q1("SELECT reclaim_stale_tasks(interval '3 minutes')")
check(q1("SELECT status FROM job_tasks WHERE job_id = %s AND task_key = 'SEG-001'", jq2) == "failed", "hết lượt thử -> failed (không lặp vô hạn)")

# ---------------------------------------------------------------- hết hạn dữ liệu
print("[retention]")
ue = new_user("e@example.com")
de = q1("INSERT INTO documents (user_id, title, source_type, expires_at, status) VALUES (%s, 'cu', 'paste', now() - interval '1 day', 'ready') RETURNING id", ue)
conn.execute("INSERT INTO doc_paragraphs (document_id, pid, idx, kind, content, char_count) VALUES (%s, 'P000001', 1, 'body', 'xin chao', 8)", (de,))
conn.execute("INSERT INTO doc_sections (document_id, section_id, title, level, idx, first_pid, last_pid) VALUES (%s, 'D01', 'Muc', 1, 1, 'P000001', 'P000001')", (de,))
check(q1("SELECT purge_expired_documents()") >= 1, "purge_expired_documents trả về số tài liệu hết hạn")
check(q1("SELECT count(*) FROM doc_paragraphs WHERE document_id = %s", de) == 0, "đoạn văn của tài liệu hết hạn đã bị xoá")
check(q1("SELECT status FROM documents WHERE id = %s", de) == "deleted", "tài liệu chuyển trạng thái deleted")

# ---------------------------------------------------------------- ràng buộc dữ liệu
print("[constraints]")
check(expect_error("INSERT INTO doc_paragraphs (document_id, pid, idx, kind, content, char_count) VALUES (%s, 'P12', 1, 'body', 'x', 1)", (d,), "check"), "pid sai định dạng bị từ chối")
check(expect_error("INSERT INTO jobs (user_id, document_id, level, est_credits, model_profile, prompt_versions) VALUES (%s, %s, 'turbo', 1, '{}', '{}')", (u, d), "check"), "level ngoài enum bị từ chối")
check(expect_error("INSERT INTO glossaries (scope, name) VALUES ('personal', 'x')", (), "check"), "glossary cá nhân bắt buộc có owner")
check(expect_error("INSERT INTO glossaries (scope, name, owner_id) VALUES ('shared', 'x', %s)", (u,), "check"), "glossary chuẩn không được có owner")
g = q1("INSERT INTO glossaries (scope, name) VALUES ('shared', 'Chuan') RETURNING id")
conn.execute("INSERT INTO glossary_entries (glossary_id, source_term, target_term) VALUES (%s, 'Solar Plexus', 'Trung tam Thai duong')", (g,))
check(expect_error("INSERT INTO glossary_entries (glossary_id, source_term, target_term) VALUES (%s, 'solar plexus', 'khac')", (g,), "unique"), "thuật ngữ trùng (không phân biệt hoa/thường) bị từ chối")
check(expect_error("INSERT INTO users (email) VALUES ('A@Example.com')", (), "unique"), "email trùng (không phân biệt hoa/thường) bị từ chối")
check(expect_error("INSERT INTO invite_codes (code, credits_grant) VALUES ('abc', 10)", (), "check"), "mã mời sai định dạng bị từ chối")

# ---------------------------------------------------------------- hoãn task khi pool chưa có chỗ
print("[defer_task]")
ud2 = new_user("defer@example.com")
dd2 = new_doc(ud2)
jd = new_job(ud2, dd2)
conn.execute("INSERT INTO job_tasks (job_id, stage, task_key) VALUES (%s, 'map', 'SEG-001')", (jd,))
tid = conn.execute("SELECT id FROM claim_tasks('wd', 10, 3) WHERE job_id = %s", (jd,)).fetchone()[0]
check(q1("SELECT attempt FROM job_tasks WHERE id = %s", tid) == 1, "nhận task: attempt = 1")
check(q1("SELECT defer_task(%s, now() + interval '1 hour')", tid) is True, "defer_task trả true cho task đang chạy")
row = conn.execute("SELECT status, attempt, locked_by, run_after > now() FROM job_tasks WHERE id = %s", (tid,)).fetchone()
check(row[0] == "pending" and row[1] == 0 and row[2] is None and row[3] is True, "task về pending, attempt hoàn về 0 (chờ hạn mức không phải lỗi), run_after ở tương lai")
check(len(conn.execute("SELECT 1 FROM claim_tasks('wd', 10, 3) WHERE job_id = %s", (jd,)).fetchall()) == 0, "chưa tới run_after thì không bị nhận lại")
check(q1("SELECT defer_task(%s, now())", tid) is False, "defer task không ở trạng thái running -> false")

# ---------------------------------------------------------------- glossary: hàng đợi duyệt và bản phát hành bất biến
print("[glossary release]")
gs = q1("INSERT INTO glossaries (scope, name) VALUES ('shared', 'Chuan-HITL') RETURNING id")
conn.execute("INSERT INTO glossary_entries (glossary_id, source_term, target_term, status, proposed_by) VALUES (%s, 'Gate', 'Cổng', 'confirmed', 'curator')", (gs,))
conn.execute("INSERT INTO glossary_entries (glossary_id, source_term, target_term, status, proposed_by, needs_human, question_vi, confidence) VALUES (%s, 'Authority', 'Thẩm quyền', 'suggested', 'ai', true, 'Dịch là thẩm quyền hay quyền năng?', 0.45)", (gs,))
conn.execute("INSERT INTO glossary_entries (glossary_id, source_term, target_term, status, proposed_by) VALUES (%s, 'Channel', 'Kênh', 'confirmed', 'ai')", (gs,))
admin = new_user("admin@example.com")
check(q1("SELECT publish_glossary_release(%s, %s, 'bản đầu')", gs, admin) == 1, "phát hành lần đầu = v1")
r1 = conn.execute("SELECT entry_count, content_sha256 FROM glossary_releases WHERE glossary_id = %s AND version = 1", (gs,)).fetchone()
check(r1[0] == 2, "bản phát hành chỉ gồm mục đã 'confirmed' (mục AI đang 'suggested' không lọt vào)")
check(q1("SELECT version FROM glossaries WHERE id = %s", gs) == 1, "glossaries.version = số hiệu phát hành mới nhất")
conn.execute("UPDATE glossary_entries SET status = 'confirmed', reviewed_by = %s, reviewed_at = now(), target_term = 'Quyền năng' WHERE glossary_id = %s AND source_term = 'Authority'", (admin, gs))
check(q1("SELECT publish_glossary_release(%s, %s)", gs, admin) == 2, "sau khi người duyệt chấp nhận: v2")
r2 = conn.execute("SELECT entry_count, content_sha256 FROM glossary_releases WHERE glossary_id = %s AND version = 2", (gs,)).fetchone()
check(r2[0] == 3 and r2[1] != r1[1], "v2 có 3 mục và hash khác v1")
check(q1("SELECT entries->0->>'source_term' FROM glossary_releases WHERE glossary_id = %s AND version = 1", gs) == "Channel", "mục trong bản phát hành được sắp ổn định theo source_term")
check(expect_error("SELECT publish_glossary_release(%s, NULL)", (uuid.uuid4(),), "glossary_not_found"), "phát hành glossary không tồn tại -> lỗi")

# ---------------------------------------------------------------- Lõi văn phong: vòng đời và tính bất biến
print("[style core]")
sc = q1("INSERT INTO style_cores (slug, name, domain) VALUES ('human-design-vi', 'Human Design (ví dụ)', 'human-design') RETURNING id")
content = json.dumps({"schema_version": "1", "name_vi": "x"})
v1 = q1("INSERT INTO style_core_versions (style_core_id, version, content, content_sha256, origin, open_decisions) VALUES (%s, '1.0.0', %s, 'h1', 'ai_proposal', '[{\"id\":\"D1\"}]') RETURNING id", sc, content)
check(expect_error("UPDATE style_core_versions SET status = 'approved', approved_by = %s, approved_at = now() WHERE id = %s", (admin, v1), "check"), "không duyệt được khi còn quyết định mở (open_decisions)")
conn.execute("UPDATE style_core_versions SET open_decisions = '[]'::jsonb WHERE id = %s", (v1,))
check(expect_error("UPDATE style_core_versions SET status = 'approved' WHERE id = %s", (v1,), "check"), "duyệt phải ghi người duyệt và thời điểm")
conn.execute("UPDATE style_core_versions SET status = 'approved', approved_by = %s, approved_at = now() WHERE id = %s", (admin, v1))
check(expect_error("UPDATE style_core_versions SET content = '{\"schema_version\":\"1\",\"name_vi\":\"sua\"}' WHERE id = %s", (v1,), "approved_version_is_immutable"), "phiên bản đã duyệt không sửa được nội dung")
check(expect_error("UPDATE style_core_versions SET status = 'draft' WHERE id = %s", (v1,), "approved_version_cannot_be_reopened"), "phiên bản đã duyệt không mở lại được")
check(expect_error("DELETE FROM style_core_versions WHERE id = %s", (v1,), "approved_version_is_immutable"), "phiên bản đã duyệt không xoá được")
v2 = q1("INSERT INTO style_core_versions (style_core_id, version, content, content_sha256) VALUES (%s, '1.1.0', %s, 'h2') RETURNING id", sc, content)
check(q1("SELECT status FROM style_core_versions WHERE id = %s", v2) == "draft", "phiên bản mới (copy-on-write) bắt đầu ở draft")
conn.execute("UPDATE style_core_versions SET status = 'deprecated' WHERE id = %s", (v1,))
check(q1("SELECT status FROM style_core_versions WHERE id = %s", v1) == "deprecated", "approved -> deprecated được phép")
check(expect_error("INSERT INTO style_core_versions (style_core_id, version, content, content_sha256) VALUES (%s, 'v1', '{}', 'x')", (sc,), "check"), "version phải đúng semver")
check(expect_error("INSERT INTO style_cores (slug, name, scope) VALUES ('ca-nhan-x', 'x', 'personal')", (), "check"), "Lõi cá nhân bắt buộc có owner")
check(expect_error("INSERT INTO style_core_reviews (version_id, action) VALUES (%s, 'hack')", (v2,), "check"), "hành động review ngoài enum bị từ chối")
conn.execute("INSERT INTO style_core_reviews (version_id, reviewer_id, action, field_path, comment) VALUES (%s, %s, 'comment', 'rules[R01].text', 'ok')", (v2, admin))
cr = q1("INSERT INTO curation_runs (kind, requested_by, params) VALUES ('style_core_proposal', %s, '{\"mode\":\"bootstrap\"}') RETURNING id", admin)
check(q1("SELECT status FROM curation_runs WHERE id = %s", cr) == "queued", "curation_run mới ở trạng thái queued")
check(expect_error("INSERT INTO curation_runs (kind, params) VALUES ('khac', '{}')", (), "check"), "curation_run.kind ngoài enum bị từ chối")

# ---------------------------------------------------------------- LLM Pool: ràng buộc dữ liệu và nhập cấu hình
print("[pool: seed + constraints]")
CFG = json.loads((ROOT / "examples" / "pool_config.example.json").read_text(encoding="utf-8"))
seed_pool(conn, CFG)
check(q1("SELECT count(*) FROM llm_deployments") == 5 and q1("SELECT count(*) FROM llm_profile_tiers") == 11, "nhập PoolConfig mẫu: 5 deployment, 11 tầng định tuyến")
check(q1("SELECT count(*) FROM llm_credentials WHERE secret_ref IS NOT NULL") == 5, "khoá chỉ lưu dạng tham chiếu secret_ref (không có khoá rõ)")
check(expect_error("INSERT INTO llm_credentials (id, group_id, label, secret_ref) VALUES ('raw-key', 'gemini-paid', 'x', 'AIzaSyDUMMYKEY')", (), "check"), "secret_ref phải có tiền tố env:/file: (chặn dán khoá rõ vào cột tham chiếu)")
check(expect_error("INSERT INTO llm_credentials (id, group_id, label) VALUES ('no-secret', 'gemini-paid', 'x')", (), "check"), "credential phải có đúng một nguồn bí mật")
check(expect_error("UPDATE llm_quota_groups SET tos_flags = '{khong_co}' WHERE id = 'gemini-paid'", (), "check"), "tos_flags ngoài danh sách bị từ chối")
check(expect_error("UPDATE llm_quota_groups SET allowed_gates = '{}' WHERE id = 'gemini-paid'", (), "check"), "allowed_gates không được rỗng")
check(expect_error("INSERT INTO llm_profiles (name, needs) VALUES ('tuy-y', '{}')", (), "check"), "tên profile ngoài danh sách bị từ chối")
check(expect_error("INSERT INTO llm_providers (id, kind, base_url, display_name) VALUES ('http-x', 'openai_compat', 'http://insecure.example', 'x')", (), "check"), "base_url bắt buộc https")

print("[pool: chi phí thật và chi phí bóng]")
c_paid = conn.execute("SELECT cost_usd, shadow_usd FROM pool_call_cost('gemini-paid/flash', DATE '2026-10-02', 1000000, 0, 1000000, 0)").fetchone()
c_free = conn.execute("SELECT cost_usd, shadow_usd FROM pool_call_cost('gemini-free-a/flash', DATE '2026-10-02', 1000000, 0, 1000000, 0)").fetchone()
c_trial = conn.execute("SELECT cost_usd, shadow_usd FROM pool_call_cost('nvidia-trial/chat', DATE '2026-10-02', 1000000, 0, 1000000, 0)").fetchone()
c_a = conn.execute("SELECT cost_usd, shadow_usd FROM pool_call_cost('provider-a-free/large', DATE '2026-10-02', 1000000, 0, 1000000, 0)").fetchone()
check(float(c_paid[0]) == 4.5 and float(c_paid[1]) == 4.5, "deployment trả phí: tiền thật = chi phí bóng = $4.50")
check(float(c_free[0]) == 0.0 and float(c_free[1]) == 4.5, "deployment miễn phí: tiền thật $0, chi phí bóng $4.50 (trần chi phí job vẫn có nghĩa)")
check(float(c_a[0]) == 0.0 and float(c_a[1]) == 4.5, "model nhà cung cấp khác dùng giá tham chiếu Gemini làm chi phí bóng")
check(float(c_trial[0]) == 0.0 and float(c_trial[1]) == 4.5, "deployment thử nghiệm: tiền thật $0, chi phí bóng theo giá tham chiếu")

# ---------------------------------------------------------------- LLM Pool: SQL và MemoryState có CÙNG ngữ nghĩa
print("[pool: SQL == MemoryState (kịch bản có hạt giống, so từng bước)]")
sys.path.insert(0, str(ROOT / "tests"))
import collections  # noqa: E402

import pool_scenarios as ps  # noqa: E402


def make_pg(cfg):
    conn.execute("DELETE FROM llm_scope_state")
    conn.execute("DELETE FROM llm_providers")  # cascade: model, nhóm, khoá, deployment, lease
    conn.execute("DELETE FROM llm_profiles")
    seed_pool(conn, cfg)
    return PgState(conn)


tot = ps.Result()
for scn in ps.SCENARIOS:
    r = ps.run(scn, CFG, make_pg)
    tot.reasons.update(r.reasons)
    tot.circuits |= r.circuits
    tot.mismatches += r.mismatches
    tot.reserves += r.reserves
    tot.granted += r.granted
    tot.settles += r.settles
    tot.snapshots += r.snapshots
    check(not r.mismatches, f"kịch bản '{scn.name}': SQL khớp MemoryState ({r.reserves} đặt chỗ, {r.granted} cấp, {r.settles} ghi nhận)" + (f"; LỆCH đầu tiên: {r.mismatches[0]}" if r.mismatches else ""))
check(ps.REQUIRED_REASONS <= set(tot.reasons), f"các kịch bản chạm mọi nhánh từ chối trên SQL ({len(tot.reasons)} lý do khác nhau)")
check({"open", "half_open", "closed"} <= tot.circuits, "đã đi qua đủ ba trạng thái circuit breaker (closed, open, half_open)")
check(tot.reserves > 2500 and tot.snapshots > 800, f"tổng cộng {tot.reserves} lần đặt chỗ và {tot.snapshots} ảnh chụp được so khớp từng bước")
check(q1("SELECT count(*) FROM llm_incidents") > 0, "các sự cố 429/circuit được ghi vào llm_incidents")

print("[pool: tranh chấp đồng thời]")
conn.execute("DELETE FROM llm_scope_state")
conn.execute("DELETE FROM llm_leases")
conn.execute("UPDATE llm_deployments SET concurrency = 100 WHERE id = 'gemini-free-a/flash'")
conn.execute("UPDATE llm_deployments SET rpm = 10, tpm = NULL, rpd = NULL WHERE id = 'gemini-free-a/flash'")
results, errs = [], []
NOW = 1_800_000_000.0


def worker():
    try:
        with psycopg.connect(dsn, autocommit=True) as c2:
            for _ in range(6):
                r = c2.execute("SELECT ok FROM pool_try_reserve('gemini-free-a/flash', 100, 10, 'normal', %s, 0.2, 300)", (NOW,)).fetchone()[0]
                results.append(r)
    except Exception as e:  # noqa: BLE001
        errs.append(repr(e))


ths = [threading.Thread(target=worker) for _ in range(8)]
[t.start() for t in ths]
[t.join() for t in ths]
check(not errs, f"48 lời gọi đồng thời từ 8 kết nối không lỗi/deadlock ({errs[:1]})")
check(sum(results) == 8 and len(results) == 48, f"bucket RPM cap 8.5 chỉ cấp đúng 8 chỗ dù 48 yêu cầu tranh nhau (được {sum(results)})")
check(q1("SELECT inflight FROM llm_scope_state WHERE scope = 'deployment' AND scope_id = 'gemini-free-a/flash'") == 8, "inflight = 8 khớp số chỗ đã cấp")
check(q1("SELECT pool_reap_leases(%s)", NOW + 301) == 8, "thu hồi 8 lease quá hạn")
check(q1("SELECT inflight FROM llm_scope_state WHERE scope = 'deployment' AND scope_id = 'gemini-free-a/flash'") == 0, "inflight về 0 sau khi thu hồi")

# ---------------------------------------------------------------- khai báo nhà cung cấp và khoá hàng loạt (SPEC §17.14)
print("[pool: khai báo khoá, mã hoá, xác nhận rủi ro]")
conn.execute("DELETE FROM llm_scope_state")
conn.execute("DELETE FROM llm_providers")
conn.execute("DELETE FROM llm_profiles")
DECL = json.loads((ROOT / "examples" / "pool_declaration.example.json").read_text(encoding="utf-8"))
BASE = {k: CFG[k] for k in ("version", "note", "policy", "profiles")} | {"providers": [], "models": [], "groups": [], "deployments": []}
MASTER = bytes(range(32))
fpf = lambda sec: fingerprint(MASTER, sec)  # noqa: E731
res = compile_all(DECL, BASE, fingerprint_fn=fpf, existing_fingerprints=existing_fingerprints(conn))
check(res.valid and len(res.secrets) == 6, "khai báo mẫu biên dịch hợp lệ: 6 khoá")
counts = apply_declaration(conn, res, MASTER)
check(counts == dict(providers=2, models=2, groups=6, credentials=6, deployments=6), f"áp dụng ghi đúng số bản ghi: {counts}")
check(q1("SELECT count(*) FROM llm_credentials WHERE secret_enc IS NOT NULL AND secret_ref IS NULL") == 6, "cả 6 khoá lưu dạng mã hoá (secret_enc), không có secret_ref")
plain_in_db = 0
for cid, secret in res.secrets.items():
    blob = conn.execute("SELECT secret_enc FROM llm_credentials WHERE id = %s", (cid,)).fetchone()[0]
    plain_in_db += secret.encode() in bytes(blob)
    assert decrypt_secret(MASTER, cid, bytes(blob)) == secret
check(plain_in_db == 0, "không bản mã nào chứa khoá ở dạng rõ; giải mã bằng khoá chủ cho lại đúng khoá")
row = conn.execute("SELECT last4, length(fingerprint) FROM llm_credentials WHERE id = 'gemini-paid-k1'").fetchone()
check(row == (res.secrets["gemini-paid-k1"][-4:], 32), "lưu 4 ký tự cuối và dấu vân tay 32 ký tự")
check(len(existing_fingerprints(conn)) == 6, "existing_fingerprints trả đủ 6 dấu vân tay")
again = compile_all(DECL, merge_fragment(BASE, res.fragment), fingerprint_fn=fpf, existing_fingerprints=existing_fingerprints(conn))
check(not again.valid and again.secrets == {}, "khai báo lại cùng các khoá: bị từ chối vì trùng (không thêm gì)")
check(expect_error("INSERT INTO llm_credentials (id, group_id, label, fingerprint, secret_enc) SELECT 'dup-k9', group_id, 'x', fingerprint, secret_enc FROM llm_credentials WHERE id = 'gemini-paid-k1'", (), "unique"),
      "CSDL chặn khoá trùng ngay cả khi ứng dụng bỏ qua bước kiểm tra (chỉ mục duy nhất trên dấu vân tay)")
r0 = q1("SELECT count(*) FROM llm_providers"), q1("SELECT count(*) FROM llm_quota_groups")
bad = compile_all({"version": 1, "declarations": [{"provider": {"preset": "custom", "id": "provider-z", "base_url": "https://z.example/v1"}, "tier": "paid", "data_policy": "no_training",
                                                     "keys": "ak_example_zeta_00000001", "models": [{"model_id": "zm", "ctx_in": 65536, "max_out": 4096}]}]}, BASE, fingerprint_fn=fpf)
bad.fragment["deployments"][0]["model"] = "provider-z:khong-co-model"  # tham chiếu hỏng: giao dịch phải huỷ toàn bộ
try:
    apply_declaration(conn, bad, MASTER)
    rolled_back = False
except psycopg.Error:
    rolled_back = True
check(rolled_back and (q1("SELECT count(*) FROM llm_providers"), q1("SELECT count(*) FROM llm_quota_groups")) == r0, "lỗi giữa chừng: giao dịch huỷ toàn bộ, không ghi nửa vời")
check(expect_error("UPDATE llm_quota_groups SET risk_ack_flags = '{}' WHERE id = 'gemini-free-acc1'", (), "check"), "không gỡ được xác nhận rủi ro của nhóm đang bật có cờ multi_account_risk")
conn.execute("UPDATE llm_quota_groups SET enabled = false WHERE id = 'gemini-free-acc1'")
conn.execute("UPDATE llm_quota_groups SET risk_ack_flags = '{}' WHERE id = 'gemini-free-acc1'")
check(expect_error("UPDATE llm_quota_groups SET enabled = true WHERE id = 'gemini-free-acc1'", (), "check"), "bật lại nhóm có cờ rủi ro mà thiếu xác nhận: CSDL từ chối")
conn.execute("UPDATE llm_quota_groups SET risk_ack_flags = '{multi_account_risk}', risk_ack_at = now(), enabled = true WHERE id = 'gemini-free-acc1'")
check(q1("SELECT enabled FROM llm_quota_groups WHERE id = 'gemini-free-acc1'") is True, "xác nhận lại rồi bật được")
check(expect_error("INSERT INTO llm_quota_groups (id, provider_id, label, tier, tos_flags) VALUES ('trial-noack', 'gemini', 'x', 'trial', '{trial_only}')", (), "check"), "tạo nhóm trial_only đang bật mà chưa xác nhận: bị từ chối")
conn.execute("INSERT INTO llm_quota_groups (id, provider_id, label, tier, tos_flags, enabled) VALUES ('trial-off', 'gemini', 'x', 'trial', '{trial_only}', false)")
check(q1("SELECT enabled FROM llm_quota_groups WHERE id = 'trial-off'") is False, "nhóm có cờ rủi ro tạo ở trạng thái tắt thì được (chờ xác nhận)")
check(q1("SELECT value FROM app_settings WHERE key = 'pool_allow_risk_at_public_gates'") is False, "mặc định KHÔNG cho nhóm rủi ro ra cổng công khai (app_settings)")

print(f"\n{passed} kiểm tra đạt")
