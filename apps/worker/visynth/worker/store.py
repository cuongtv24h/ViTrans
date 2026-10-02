"""Truy cập CSDL của worker (M1): hàng đợi, lease, checkpoint theo giai đoạn, sự kiện, báo cáo.

Mọi bất biến dưới tranh chấp nằm ở hàm SQL của `docs/db/schema.sql`
(`claim_tasks`, `reclaim_stale_tasks`, `defer_task`, `charge_credits`, `refund_credits`);
lớp này chỉ gọi đúng thứ tự và giữ payload sự kiện khớp `job_event.schema.json`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from visynth.extract.model import Extraction, Paragraph, Section


def to_jsonb(value: Any) -> Jsonb:
    """`Jsonb` chịu được UUID/date/Decimal (JSONB của PostgreSQL nhận chuỗi) — dùng cho mọi cột jsonb."""
    return Jsonb(value, dumps=lambda v: json.dumps(v, ensure_ascii=False, default=str))


#: Thứ tự giai đoạn worker chạy cho các mức tổng hợp (khớp `pipeline.run.STAGE_SEQUENCE`).
STAGE_SEQUENCE = ("profile", "glossary", "map", "consolidate", "write", "verify", "repair")
#: Mức `full_translation` đi đường dịch trực tiếp: P0 → cổng glossary → P9 (`translate`).
TRANSLATE_SEQUENCE = ("profile", "glossary", "translate")


def stage_sequence(level: str) -> tuple[str, ...]:
    """Trình tự giai đoạn của một mức (`full_translation` không chạy map/write/verify/repair)."""
    return TRANSLATE_SEQUENCE if level == "full_translation" else STAGE_SEQUENCE


#: Tiến độ hiển thị sau khi xong mỗi giai đoạn (%).
STAGE_PROGRESS = {
    "profile": 8,
    "glossary": 20,
    "map": 45,
    "consolidate": 55,
    "write": 75,
    "verify": 88,
    "repair": 100,
}
ALL_STAGES = (
    "extract",
    "profile",
    "segment",
    "glossary",
    "map",
    "consolidate",
    "write",
    "verify",
    "repair",
    "translate",
    "assemble",
)


def _half(credits: int) -> int:
    """Một nửa tín dụng, làm tròn LÊN để người dùng không bị thiệt (§12.6)."""
    return (int(credits) + 1) // 2


class WorkerStore:
    """Kết nối CSDL của worker (một pool nhỏ, giao dịch ngắn)."""

    def __init__(self, dsn: str, *, worker_id: str, min_size: int = 1, max_size: int = 4) -> None:
        self.worker_id = worker_id
        self.pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            kwargs={"row_factory": dict_row},
            name=f"visynth-worker-{worker_id}",
        )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def tx(self) -> Iterator[psycopg.Cursor]:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            yield cur

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    # ------------------------------------------------------------------ hàng đợi
    def reclaim_stale(self, stale: str = "3 minutes") -> int:
        """Thu hồi task của worker đã chết (lease quá hạn heartbeat)."""
        return int(self.scalar("SELECT reclaim_stale_tasks(%s::interval)", (stale,)) or 0)

    def promote_queued(self, limit: int = 1) -> list[dict]:
        """`queued` → `running` + sinh `job_tasks` cho từng giai đoạn (một task/khối lượng)."""
        with self.tx() as cur:
            cur.execute(
                """SELECT id, level FROM jobs
                    WHERE status = 'queued' AND NOT cancel_requested
                    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s""",
                (limit,),
            )
            jobs = cur.fetchall()
            for job in jobs:
                cur.execute(
                    "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, now()) WHERE id = %s",
                    (job["id"],),
                )
                for stage in stage_sequence(str(job["level"])):
                    cur.execute(
                        """INSERT INTO job_tasks (job_id, stage, task_key, max_attempts)
                           VALUES (%s, %s, %s, 3) ON CONFLICT (job_id, stage, task_key) DO NOTHING""",
                        (job["id"], stage, stage),
                    )
                self._event(cur, job["id"], "job_queued", data={"level": job["level"]})
            return jobs

    # ------------------------------------------------------------- điểm lưu theo giai đoạn
    def save_checkpoint(self, job_id: str, stage: str, state: dict) -> None:
        """Lưu trạng thái pipeline sau một giai đoạn để chạy tiếp mà không chạy lại."""
        self.tx_execute(
            """INSERT INTO job_checkpoints (job_id, stage, state) VALUES (%s, %s, %s)
               ON CONFLICT (job_id, stage) DO UPDATE SET state = EXCLUDED.state, updated_at = now()""",
            (job_id, stage, to_jsonb(state)),
        )

    def load_checkpoint(self, job_id: str, stage: str) -> dict | None:
        row = self.one("SELECT state FROM job_checkpoints WHERE job_id = %s AND stage = %s", (job_id, stage))
        return dict(row["state"]) if row and row["state"] else None

    def latest_checkpoint_before(self, job_id: str, stage: str) -> tuple[str | None, dict | None]:
        """Điểm lưu gần nhất TRƯỚC giai đoạn `stage` (dùng khi task bị chạy lại).

        Thứ tự lấy theo MỨC của job: `full_translation` không có giai đoạn `map`…`repair` nên
        `translate` phải khôi phục đúng điểm lưu của `glossary`.
        """
        level = str(self.scalar("SELECT level FROM jobs WHERE id = %s", (job_id,)) or "")
        order = list(stage_sequence(level))
        row = self.one(
            """SELECT stage, state FROM job_checkpoints
                WHERE job_id = %s AND array_position(%s::text[], stage) < array_position(%s::text[], %s::text)
                ORDER BY array_position(%s::text[], stage) DESC LIMIT 1""",
            (job_id, order, order, stage, order),
        )
        return (row["stage"], dict(row["state"])) if row else (None, None)

    # ------------------------------------------------------------- cổng duyệt glossary
    def save_job_glossary(self, job_id: str, entries: list[dict], *, status: str) -> int:
        """Ghi glossary của job: `pending` (gợi ý P1 chờ duyệt) hoặc `confirmed` (đã chốt)."""
        if status not in ("pending", "confirmed"):
            raise ValueError("status phải là pending hoặc confirmed")
        written = 0
        with self.tx() as cur:
            for entry in entries:
                cur.execute(
                    """INSERT INTO job_glossary_entries (job_id, source_term, target_term, keep_original,
                            case_sensitive, forbidden_variants, term_type, note, origin, status, confidence)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (job_id, lower(source_term)) DO UPDATE SET
                            target_term = CASE WHEN job_glossary_entries.status = 'confirmed'
                                               THEN job_glossary_entries.target_term ELSE EXCLUDED.target_term END,
                            keep_original = EXCLUDED.keep_original,
                            case_sensitive = EXCLUDED.case_sensitive,
                            forbidden_variants = EXCLUDED.forbidden_variants,
                            term_type = EXCLUDED.term_type,
                            note = COALESCE(job_glossary_entries.note, EXCLUDED.note),
                            origin = CASE WHEN job_glossary_entries.status = 'confirmed'
                                          THEN job_glossary_entries.origin ELSE EXCLUDED.origin END,
                            confidence = EXCLUDED.confidence""",
                    (
                        job_id,
                        entry["source_term"],
                        entry["target_term"],
                        bool(entry.get("keep_original")),
                        bool(entry.get("case_sensitive")),
                        list(entry.get("forbidden_variants") or []),
                        entry.get("term_type") or "concept",
                        entry.get("note"),
                        entry.get("origin") or "suggested",
                        entry.get("status") or ("confirmed" if status == "confirmed" else "suggested"),
                        entry.get("confidence"),
                    ),
                )
                written += cur.rowcount
        return written

    def open_glossary_gate(self, job_id: str, *, candidates: int, minutes: float) -> None:
        """Dừng job ở `awaiting_glossary` cho người dùng duyệt (SPEC §6.4); hết hạn thì tự xác nhận."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'awaiting_glossary', glossary_review_deadline = "
                "now() + make_interval(mins => %s::int) WHERE id = %s",
                (int(round(minutes)), job_id),
            )
            self._event(
                cur,
                job_id,
                "awaiting_glossary",
                message_vi="Cần bạn duyệt thuật ngữ trước khi viết báo cáo",
                data={"candidates": candidates, "minutes": int(round(minutes))},
                progress=self.progress(job_id),
            )

    def load_shared_glossary(self, glossary_ids: list[str]) -> list[dict]:
        """Ảnh chụp bản phát hành mới nhất của các glossary người dùng chọn (`glossary_ids`)."""
        if not glossary_ids:
            return []
        rows = self.all(
            """SELECT DISTINCT ON (r.glossary_id) r.entries, g.scope
                 FROM glossary_releases r JOIN glossaries g ON g.id = r.glossary_id
                WHERE r.glossary_id = ANY(%s::uuid[])
                ORDER BY r.glossary_id, r.version DESC""",
            ([str(g) for g in glossary_ids],),
        )
        out: list[dict] = []
        for row in rows:
            for entry in row["entries"] or []:
                if entry.get("status") not in (None, "confirmed"):
                    continue
                out.append(
                    {**entry, "origin": "personal" if row["scope"] == "personal" else "shared", "status": "confirmed"}
                )
        return out

    def load_job_glossary(self, job_id: str) -> list[dict]:
        """Các mục của job đã được chốt (hoặc người dùng sửa) để pipeline dùng làm ràng buộc cứng."""
        return self.all(
            """SELECT source_term, target_term, keep_original, case_sensitive, forbidden_variants,
                      term_type, note, origin, status, confidence
                 FROM job_glossary_entries WHERE job_id = %s AND status = 'confirmed'
                 ORDER BY priority DESC, lower(source_term)""",
            (job_id,),
        )

    def sweep_glossary_gates(self) -> int:
        """Cổng quá hạn 15 phút: tự xác nhận mục `confidence >= 0.7`, loại phần còn lại (AC-04)."""
        with self.tx() as cur:
            cur.execute(
                """SELECT id, COALESCE((options ->> 'auto_confirm_min_confidence')::real, 0.7) AS min_confidence
                     FROM jobs WHERE status = 'awaiting_glossary' AND glossary_review_deadline <= now()
                     ORDER BY glossary_review_deadline FOR UPDATE SKIP LOCKED LIMIT 50"""
            )
            rows = cur.fetchall()
            for row in rows:
                cur.execute(
                    """UPDATE job_glossary_entries
                          SET status = CASE WHEN coalesce(confidence, 0) >= %s THEN 'confirmed' ELSE 'rejected' END
                        WHERE job_id = %s AND status = 'suggested'""",
                    (row["min_confidence"], row["id"]),
                )
                cur.execute(
                    """UPDATE jobs SET status = 'queued', glossary_auto_confirmed = true,
                              glossary_review_deadline = NULL WHERE id = %s""",
                    (row["id"],),
                )
                self._event(
                    cur,
                    row["id"],
                    "glossary_confirmed",
                    message_vi="Hết thời gian duyệt — hệ thống tự xác nhận thuật ngữ đủ tin cậy",
                    data={"auto": True},
                )
                self._event(cur, row["id"], "job_queued", message_vi="Đã xếp hàng, chờ worker", data={"level": None})
        return len(rows)

    def claim(self, *, limit: int = 1, per_job_limit: int = 1) -> list[dict]:
        """Nhận task bằng `claim_tasks` (FOR UPDATE SKIP LOCKED + advisory lock theo job)."""
        with self.tx() as cur:
            cur.execute("SELECT * FROM claim_tasks(%s, %s, %s)", (self.worker_id, limit, per_job_limit))
            return list(cur.fetchall())

    def heartbeat(self, task_id: int) -> None:
        self.tx_execute("UPDATE job_tasks SET heartbeat_at = now() WHERE id = %s", (task_id,))

    def defer(self, task_id: int, seconds: float) -> bool:
        """Hoãn task trong `seconds` giây (chờ deployment rảnh) — KHÔNG tính là lần thử thất bại.

        Hàm SQL `defer_task` trả task về `pending` với `run_after` mới và `attempt - 1` (§6.10 bước 1).
        """
        return bool(
            self.scalar("SELECT defer_task(%s, now() + make_interval(secs => %s))", (task_id, max(1.0, float(seconds))))
        )

    def tx_execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.tx() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def task_succeeded(
        self, task_id: int, *, result: dict, tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0
    ) -> None:
        self.tx_execute(
            """UPDATE job_tasks SET status = 'succeeded', finished_at = now(), result = %s,
                    tokens_in = %s, tokens_out = %s, cost_usd = %s, locked_by = NULL
                WHERE id = %s""",
            (to_jsonb(result), tokens_in, tokens_out, cost_usd, task_id),
        )

    def task_failed(self, task_id: int, *, code: str, message: str, retryable: bool = False) -> str:
        """Đánh dấu task lỗi; `pending` (còn lượt) hoặc `failed`. Trả trạng thái mới."""
        with self.tx() as cur:
            cur.execute("SELECT attempt, max_attempts FROM job_tasks WHERE id = %s FOR UPDATE", (task_id,))
            row = cur.fetchone()
            if row is None:
                return "missing"
            if retryable and row["attempt"] < row["max_attempts"]:
                backoff = min(300, 15 * 2 ** max(0, row["attempt"] - 1))
                cur.execute(
                    """UPDATE job_tasks SET status = 'pending', run_after = now() + make_interval(secs => %s),
                            error_code = %s, error = %s, locked_by = NULL, locked_at = NULL
                        WHERE id = %s""",
                    (backoff, code, message[:1000], task_id),
                )
                return "pending"
            cur.execute(
                "UPDATE job_tasks SET status = 'failed', error_code = %s, error = %s, finished_at = now(), locked_by = NULL WHERE id = %s",
                (code, message[:1000], task_id),
            )
            return "failed"

    # ------------------------------------------------------------------ giai đoạn
    def stage_started(self, job_id: str, stage: str, attempt: int) -> None:
        with self.tx() as cur:
            cur.execute(
                "UPDATE job_stages SET status = 'running', attempt = %s, started_at = now() WHERE job_id = %s AND stage = %s",
                (attempt, job_id, stage),
            )
            cur.execute(
                "UPDATE jobs SET current_stage = %s, progress = GREATEST(progress, %s) WHERE id = %s",
                (stage, max(0, STAGE_PROGRESS.get(stage, 0) - 5), job_id),
            )
            self._event(cur, job_id, "stage_started", stage=stage, data={"attempt": attempt})

    def stage_finished(
        self,
        job_id: str,
        stage: str,
        *,
        status: str = "succeeded",
        metrics: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self.tx() as cur:
            cur.execute(
                """UPDATE job_stages SET status = %s, finished_at = now(), metrics = %s, error = %s
                    WHERE job_id = %s AND stage = %s""",
                (status, to_jsonb(metrics or {}), error, job_id, stage),
            )
            if status == "succeeded":
                cur.execute(
                    "UPDATE jobs SET progress = GREATEST(progress, %s) WHERE id = %s",
                    (STAGE_PROGRESS.get(stage, 0), job_id),
                )
                self._event(cur, job_id, "stage_completed", stage=stage, data={"metrics": metrics or {}})
            else:
                self._event(
                    cur, job_id, "warning", stage=stage, message_vi=(error or "")[:300], data={"code": "stage_failed"}
                )

    def record_metrics(self, job_id: str, stage: str, metrics: dict) -> None:
        """Ghi số đo của một giai đoạn mà không đổi trạng thái (dùng cho giá trị tích luỹ)."""
        self.tx_execute(
            "UPDATE job_stages SET metrics = %s WHERE job_id = %s AND stage = %s",
            (to_jsonb(metrics), job_id, stage),
        )

    # ------------------------------------------------------------------ sự kiện
    def emit(
        self,
        job_id: str,
        type: str,
        *,
        stage: str | None = None,
        message_vi: str | None = None,
        data: dict | None = None,
        progress: dict | None = None,
    ) -> None:
        with self.tx() as cur:
            self._event(cur, job_id, type, stage=stage, message_vi=message_vi, data=data, progress=progress)

    def _event(
        self,
        cur: psycopg.Cursor,
        job_id: str,
        type: str,
        *,
        stage: str | None = None,
        message_vi: str | None = None,
        data: dict | None = None,
        progress: dict | None = None,
    ) -> None:
        """Payload đúng `job_event.schema.json` (không chứa nội dung tài liệu)."""
        payload: dict[str, Any] = {"type": type, "job_id": str(job_id)}
        if stage:
            payload["stage"] = stage
        if message_vi:
            payload["message_vi"] = message_vi[:300]
        if progress:
            payload["progress"] = progress
        if data:
            payload["data"] = data
        cur.execute(
            "INSERT INTO job_events (job_id, type, stage, payload) VALUES (%s, %s, %s, %s)",
            (job_id, type, stage, to_jsonb(payload)),
        )

    def progress(self, job_id: str) -> dict:
        done = int(
            self.scalar(
                "SELECT count(*) FROM job_stages WHERE job_id = %s AND status IN ('succeeded','skipped')", (job_id,)
            )
            or 0
        )
        total = len(ALL_STAGES)
        return {"done": done, "total": total, "pct": round(100 * done / total, 1)}

    # ------------------------------------------------------------------ job / tài liệu
    def load_job(self, job_id: str) -> dict:
        job = self.one("SELECT * FROM jobs WHERE id = %s", (job_id,))
        if job is None:
            raise LookupError(f"không có job {job_id}")
        return job

    def load_extraction(self, job: dict) -> Extraction:
        """Dựng lại `Extraction` từ `doc_paragraphs`/`doc_sections` (không đọc lại tệp gốc)."""
        document = self.one(
            "SELECT id, title, source_type, language_code, language_confidence, page_count, extraction_quality, extraction_warnings "
            "FROM documents WHERE id = %s",
            (job["document_id"],),
        )
        if document is None:
            raise LookupError("tài liệu của job đã bị xoá")
        paragraphs = [
            Paragraph(
                pid=row["pid"],
                idx=row["idx"],
                kind=row["kind"],
                content=row["content"],
                section_id=row["section_id"],
                page_start=row["page_start"],
                page_end=row["page_end"],
                timecode_start_ms=row["timecode_start_ms"],
                timecode_end_ms=row["timecode_end_ms"],
                speaker=row["speaker"],
            )
            for row in self.all(
                "SELECT * FROM doc_paragraphs WHERE document_id = %s ORDER BY idx", (job["document_id"],)
            )
        ]
        sections = [
            Section(
                section_id=row["section_id"],
                parent_section_id=row["parent_section_id"],
                title=row["title"],
                level=row["level"],
                idx=row["idx"],
                first_pid=row["first_pid"],
                last_pid=row["last_pid"],
            )
            for row in self.all("SELECT * FROM doc_sections WHERE document_id = %s ORDER BY idx", (job["document_id"],))
        ]
        return Extraction(
            title=document["title"],
            source_type=document["source_type"],
            paragraphs=paragraphs,
            sections=sections,
            warnings=list(document["extraction_warnings"] or []),
            language_code=document["language_code"],
            language_confidence=document["language_confidence"] or 0.0,
            page_count=document["page_count"],
            extraction_quality=document["extraction_quality"] if document["extraction_quality"] is not None else 1.0,
        )

    # ------------------------------------------------------------------ kết thúc
    def save_report(self, job: dict, result, segments: list[dict] | None = None) -> str:
        """Ghi `reports`/`report_sections`/`report_blocks` (+ `segments`, `knowledge_units`) — trả `report_id`.

        Mức `full_translation` ghi thêm `translation_items` (mỗi pid đúng một dòng, §6.10).
        """
        for item in getattr(result, "translation_items", []) or []:
            self.save_translation_item(job["id"], item)
        with self.tx() as cur:
            for row in segments or []:
                cur.execute(
                    """INSERT INTO segments (job_id, segment_id, idx, first_pid, last_pid, token_count, section_id, labels)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (job_id, segment_id) DO UPDATE SET
                            idx = EXCLUDED.idx, labels = EXCLUDED.labels""",
                    (
                        job["id"],
                        row["segment_id"],
                        int(row.get("idx") or 0),
                        row["first_pid"],
                        row["last_pid"],
                        int(row.get("token_count") or 1),
                        row.get("section_id"),
                        to_jsonb(row.get("labels") or {}),
                    ),
                )
            for unit in result.units:
                cur.execute(
                    """INSERT INTO knowledge_units (job_id, id, segment_id, local_id, type, importance,
                            title_vi, statement_vi, topics, terms, evidence, numbers, relations, attribution,
                            state, merged_into, omit_reason)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (job_id, id) DO UPDATE SET
                            importance = EXCLUDED.importance, state = EXCLUDED.state,
                            merged_into = EXCLUDED.merged_into, omit_reason = EXCLUDED.omit_reason""",
                    (
                        job["id"],
                        unit.id,
                        unit.segment_id,
                        unit.local_id,
                        unit.type,
                        unit.importance,
                        unit.title_vi,
                        unit.statement_vi,
                        list(unit.topics),
                        list(unit.terms),
                        to_jsonb(unit.evidence),
                        to_jsonb(unit.numbers),
                        to_jsonb(unit.relations),
                        unit.attribution,
                        unit.state,
                        unit.merged_into,
                        unit.omitted_reason,
                    ),
                )
            cur.execute(
                """INSERT INTO reports (job_id, document_id, user_id, title, level, plan, stats, scope_note_md, markdown, quality_grade)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (job_id) DO UPDATE SET plan = EXCLUDED.plan, stats = EXCLUDED.stats,
                        markdown = EXCLUDED.markdown, quality_grade = EXCLUDED.quality_grade
                   RETURNING id""",
                (
                    job["id"],
                    job["document_id"],
                    job["user_id"],
                    result.title,
                    result.level,
                    to_jsonb(result.plan or {}),
                    to_jsonb(result.stats or {}),
                    _scope_note(result),
                    result.markdown,
                    result.grade,
                ),
            )
            report_id = cur.fetchone()["id"]
            for order, section in enumerate(result.sections):
                cur.execute(
                    """INSERT INTO report_sections (report_id, section_id, idx, kind, title_vi)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (report_id, section_id) DO NOTHING""",
                    (report_id, section.id, order, _section_kind(section), section.title_vi),
                )
                for idx, block in enumerate(result.blocks_of(section.id)):
                    verdict = result.verdicts.get(block.block_id, {})
                    cur.execute(
                        """INSERT INTO report_blocks (report_id, section_id, block_id, idx, type, markdown_vi, cites, verdict, issues, status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (report_id, block_id) DO UPDATE SET markdown_vi = EXCLUDED.markdown_vi,
                                cites = EXCLUDED.cites, verdict = EXCLUDED.verdict, issues = EXCLUDED.issues, status = EXCLUDED.status""",
                        (
                            report_id,
                            section.id,
                            block.block_id,
                            idx,
                            block.type,
                            block.markdown_vi,
                            list(block.cites),
                            verdict.get("verdict"),
                            to_jsonb(verdict.get("issues") or []),
                            "removed" if block.removed else ("flagged" if block.flagged else "ok"),
                        ),
                    )
        return report_id

    def save_translation_item(self, job_id: str, item) -> None:
        """Ghi một dòng `translation_items` (upsert theo `(job_id, pid)`)."""
        self.tx_execute(
            """INSERT INTO translation_items (job_id, pid, vi, note_vi, flagged)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (job_id, pid) DO UPDATE SET
                    vi = EXCLUDED.vi, note_vi = EXCLUDED.note_vi, flagged = EXCLUDED.flagged""",
            (job_id, item.pid, item.vi, item.note_vi, bool(item.flagged)),
        )

    def finish_job(self, job: dict, result) -> dict:
        """Đóng job: `succeeded` (hoặc `failed` nếu hạng C mà không có nội dung), hoàn tín dụng khi cần."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'succeeded', quality_grade = %s, progress = 100, finished_at = now() WHERE id = %s",
                (result.grade, job["id"]),
            )
            self._event(
                cur,
                job["id"],
                "job_succeeded",
                data={"grade": result.grade, "stats": result.stats, "warnings": result.warnings[:5]},
                progress={"done": len(ALL_STAGES), "total": len(ALL_STAGES), "pct": 100.0},
            )
        if result.grade == "C":
            # Hạng C nghĩa là báo cáo có lỗi chưa sửa hết: hoàn 50% số đã trừ (SPEC §12.6).
            refreshed = self.load_job(job["id"])
            refunded = self.refund(job["id"], _half(refreshed["charged_credits"]), reason="grade_c")
            return {"refunded_credits": refunded}
        return {"refunded_credits": 0}

    def fail_job(self, job: dict, *, code: str, message: str, retryable: bool = False) -> dict:
        """Job hỏng sau khi hết lượt thử: `failed` + hoàn toàn bộ tín dụng đã trừ."""
        with self.tx() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'failed', error_code = %s, error_message = %s, finished_at = now(), progress = 100 WHERE id = %s",
                (code, message[:1000], job["id"]),
            )
            self._event(
                cur, job["id"], "job_failed", message_vi=message[:300], data={"code": code, "retryable": retryable}
            )
            cur.execute(
                "SELECT refund_credits(%s, %s, %s, %s) AS refunded",
                (job["user_id"], job["charged_credits"], job["id"], f"job_failed:{job['id']}"),
            )
            refunded = cur.fetchone()["refunded"]
        return {"refunded_credits": refunded}

    def conclude_cancel(self, job: dict) -> dict:
        """Job có `cancel_requested`: dừng ở ranh giới giai đoạn và hoàn tín dụng theo chính sách §11."""
        done = self.all(
            "SELECT stage FROM job_stages WHERE job_id = %s AND status IN ('succeeded','skipped') ORDER BY stage",
            (job["id"],),
        )
        # Đã sang phần tốn kém (map trở đi) thì không hoàn; trước đó hoàn đủ.
        charged_work = any(row["stage"] in ("map", "consolidate", "write", "verify", "repair") for row in done)
        with self.tx() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'canceled', finished_at = now(), progress = 100 WHERE id = %s",
                (job["id"],),
            )
            cur.execute(
                "UPDATE job_tasks SET status = 'skipped', finished_at = now(), locked_by = NULL WHERE job_id = %s AND status IN ('pending','running')",
                (job["id"],),
            )
            refunded = 0
            if job["charged_credits"]:
                # Trước `map` (phần tốn kém): hoàn đủ; sau `map`: hoàn 50% (SPEC §12.6).
                amount = _half(job["charged_credits"]) if charged_work else job["charged_credits"]
                suffix = ":partial" if charged_work else ""
                if amount:
                    cur.execute(
                        "SELECT refund_credits(%s, %s, %s, %s) AS refunded",
                        (job["user_id"], amount, job["id"], f"cancel:{job['id']}{suffix}"),
                    )
                    refunded = cur.fetchone()["refunded"]
            self._event(
                cur, job["id"], "job_canceled", data={"refunded_credits": refunded, "charged_work": charged_work}
            )
        return {"refunded_credits": refunded}

    def refund(self, job_id: str, amount: int, *, reason: str = "manual") -> int:
        job = self.one("SELECT user_id, charged_credits FROM jobs WHERE id = %s", (job_id,))
        if not job or amount <= 0:
            return 0
        return int(
            self.scalar(
                "SELECT refund_credits(%s, %s, %s, %s)",
                (job["user_id"], amount, job_id, f"refund:{reason}:{job_id}"),
            )
            or 0
        )

    def record_spend(self, usd: float) -> None:
        """Cộng chi tiêu vào `spend_daily` (hàm SQL lo phần trần/ngày); bật cờ `paused` khi chạm trần."""
        self.tx_execute("SELECT add_spend(%s::numeric)", (round(usd, 6),))

    def record_llm_call(
        self,
        job_id: str,
        *,
        model: str,
        status: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        tokens_thinking: int = 0,
        latency_ms: int | None = None,
        cost_usd: float = 0.0,
        shadow_cost_usd: float = 0.0,
        deployment_id: str | None = None,
        prompt_id: str | None = None,
        privacy_class: str | None = None,
    ) -> None:
        """Ghi sổ một lời gọi LLM. KHÔNG chứa prompt hay nội dung tài liệu (SPEC §14.5)."""
        self.tx_execute(
            """INSERT INTO llm_calls (job_id, model, status, tokens_in, tokens_out, tokens_thinking, latency_ms,
                    cost_usd, shadow_cost_usd, deployment_id, prompt_id, privacy_class)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                job_id,
                model,
                status,
                tokens_in,
                tokens_out,
                tokens_thinking,
                latency_ms,
                cost_usd,
                shadow_cost_usd,
                deployment_id,
                prompt_id,
                privacy_class,
            ),
        )


def _section_kind(section) -> str:
    kind = getattr(section, "kind", "body")
    return kind if kind in ("summary", "body", "facts_table", "glossary_appendix", "scope_note") else "body"


def _scope_note(result) -> str | None:
    markdown = result.markdown or ""
    marker = "## Phạm vi"
    if marker in markdown:
        return markdown.split(marker, 1)[1].strip()[:4000]
    return None
