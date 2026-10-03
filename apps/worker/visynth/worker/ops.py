"""Việc bảo trì định kỳ trên VPS (SPEC §20.2, §20.7) — chạy bằng `visynth ops …`.

Ba việc, đều gọi hàm SQL có sẵn trong `docs/db/schema.sql` (không viết lại logic ở Python):

* `reap`   — `reclaim_stale_tasks(3 minutes)` + `pool_reap_leases(now())`: task mồ côi của worker chết
  trở về hàng đợi, chỗ đã đặt cho lời gọi LLM đang bay được thu hồi. Chạy **mỗi phút**.
* `purge`  — `purge_expired_documents()`: xoá nội dung gốc quá hạn (giữ bản ghi + trích dẫn ngắn).
  Chạy **mỗi giờ**; nhớ xoá cả tệp trong kho đối tượng theo `storage_key` (xem `infra/backup.sh`).
* `health` — đọc vài con số để giám sát ngoài máy (hàng đợi, chỗ đặt, tín dụng âm bất thường).

Mọi hàm đều idempotent nên chạy lại không hại; CLI trả mã thoát khác 0 khi CSDL không gọi được để
`cron`/hệ giám sát nhìn thấy ngay.
"""

from __future__ import annotations

import json
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


def reap(dsn: str, *, stale: str = "3 minutes", now: float | None = None, dry_run: bool = False) -> dict[str, int]:
    """Thu hồi task mồ côi, chỗ đặt hết hạn và dọn bộ đếm giới hạn tốc độ. Trả `{tasks, leases, rate_limit_rows}`."""
    moment = time.time() if now is None else float(now)
    with _connect(dsn) as conn, conn.cursor() as cur:
        if dry_run:
            cur.execute(
                "SELECT count(*) AS n FROM job_tasks WHERE status = 'running' AND heartbeat_at < now() - %s::interval",
                (stale,),
            )
            tasks = int(cur.fetchone()["n"])
            cur.execute("SELECT count(*) AS n FROM llm_leases WHERE expires_at < %s", (moment,))
            leases = int(cur.fetchone()["n"])
            cur.execute("SELECT count(*) AS n FROM rate_limit_hits WHERE window_start < now() - '1 hour'::interval")
            return {"tasks": tasks, "leases": leases, "rate_limit_rows": int(cur.fetchone()["n"]), "applied": 0}
        cur.execute("SELECT reclaim_stale_tasks(%s::interval) AS n", (stale,))
        tasks = int(cur.fetchone()["n"])
        cur.execute("SELECT pool_reap_leases(%s) AS n", (moment,))
        leases = int(cur.fetchone()["n"])
        cur.execute("SELECT rate_limit_gc('1 hour') AS n")
        rate_rows = int(cur.fetchone()["n"])
    return {"tasks": tasks, "leases": leases, "rate_limit_rows": rate_rows, "applied": 1}


def purge(dsn: str, *, dry_run: bool = False) -> dict[str, int]:
    """Xoá nội dung gốc quá hạn lưu. Trả `{documents}` (kèm `storage_keys` nếu chỉ xem trước)."""
    with _connect(dsn) as conn, conn.cursor() as cur:
        if dry_run:
            cur.execute(
                "SELECT count(*) AS n FROM documents "
                "WHERE expires_at IS NOT NULL AND expires_at < now() AND status <> 'deleted'"
            )
            documents = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT coalesce(array_agg(storage_key) FILTER (WHERE storage_key IS NOT NULL), '{}') AS keys "
                "FROM documents WHERE expires_at IS NOT NULL AND expires_at < now() AND status <> 'deleted'"
            )
            return {"documents": documents, "applied": 0, "storage_keys": list(cur.fetchone()["keys"])}
        cur.execute("SELECT purge_expired_documents() AS n")
        return {"documents": int(cur.fetchone()["n"]), "applied": 1}


def health(dsn: str) -> dict[str, Any]:
    """Vài con số cho giám sát ngoài máy; không chứa nội dung tài liệu."""
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT count(*) FROM job_tasks WHERE status = 'pending')                        AS tasks_pending,
              (SELECT count(*) FROM job_tasks WHERE status = 'running')                        AS tasks_running,
              (SELECT count(*) FROM job_tasks WHERE status = 'failed'
                 AND finished_at > now() - interval '1 hour')                                  AS tasks_failed_1h,
              (SELECT count(*) FROM jobs WHERE status IN ('queued','running'))                  AS jobs_active,
              (SELECT count(*) FROM jobs WHERE status = 'queued'
                 AND created_at < now() - interval '30 minutes')                                AS jobs_waiting_30m,
              (SELECT count(*) FROM llm_leases WHERE expires_at < extract(epoch FROM now()))     AS leases_expired,
              (SELECT count(*) FROM llm_credentials WHERE status = 'quarantined')               AS credentials_quarantined,
              (SELECT count(*) FROM v_credit_balance WHERE balance < 0)                        AS users_negative_credits,
              (SELECT coalesce(max(created_at), now()) FROM jobs)                               AS newest_job
            """
        )
        row = dict(cur.fetchone())
        cur.execute("SELECT current_setting('server_version') AS version")
        row["server_version"] = cur.fetchone()["version"]
        cur.execute("SELECT pg_database_size(current_database()) AS bytes")
        row["database_bytes"] = int(cur.fetchone()["bytes"])
        row["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return row


def as_json(payload: dict) -> str:
    """JSON một dòng (nhật ký VPS đọc bằng `jq`/hệ giám sát)."""
    return json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
