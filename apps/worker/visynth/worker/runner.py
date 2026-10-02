"""Vòng đời worker (M1): nhận task theo lease → chạy pipeline → checkpoint + sự kiện → quyết toán.

Trình tự một lượt `run_once()`:

1. `reclaim_stale_tasks` — task của worker chết (lease quá hạn) trở về `pending`.
2. `queued` → `running` + sinh `job_tasks` cho từng giai đoạn.
3. `claim_tasks` (advisory lock theo job, `FOR UPDATE SKIP LOCKED`).
4. Mỗi task: dựng lại `Extraction` từ CSDL, chạy 7 giai đoạn, sau MỖI giai đoạn ghi
   `job_stages` (checkpoint quan sát được), `job_tasks.result` (tiến độ đã chạy), `job_events` (SSE)
   và `heartbeat` để gia hạn lease.
5. Hết giai đoạn: `finalize` → ghi `reports`/`report_sections`/`report_blocks` → `finish_job`
   (hạng C thì hoàn tín dụng). Lỗi tạm thời: task `pending` + backoff; hết lượt: job `failed` + hoàn đủ.

Mỗi task = MỘT giai đoạn. Sau mỗi giai đoạn worker ghi `job_checkpoints.state` (ảnh chụp trạng thái
pipeline); task chạy lại sau khi worker chết hoặc lỗi tạm thời sẽ khôi phục điểm lưu gần nhất và chỉ chạy
tiếp giai đoạn của mình — không gọi lại LLM cho các giai đoạn đã xong.

Tài liệu dài ≥ `gate_min_words` (mặc định 2000 từ) thì sau giai đoạn `glossary` job dừng ở
`awaiting_glossary` cho người dùng duyệt (`POST /jobs/{id}/glossary/confirm`); quá `glossary_review_deadline`
thì `sweep_glossary_gates` tự xác nhận mục `confidence >= 0.7` và cho chạy tiếp (SPEC §6.4, AC-02/AC-04).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from visynth.llm.base import RETRYABLE, LLMClient, LLMError
from visynth.pipeline.assemble import finalize
from visynth.pipeline.models import JobOptions
from visynth.pipeline.stages import DeferredError, Pipeline
from visynth.worker.store import WorkerStore, stage_sequence

log = logging.getLogger("visynth.worker")

#: Sự kiện nội bộ của pipeline → loại sự kiện trong hợp đồng (`job_event.schema.json`).
_EVENT_MAP = {
    "job_started": "stage_started",
    "warning": "warning",
    "stage_progress": "stage_progress",
    "stage_done": "stage_completed",
    "block_removed": "warning",
    "job_succeeded": "job_succeeded",
    "awaiting_glossary": "awaiting_glossary",
    "glossary_auto_confirmed": "glossary_confirmed",
}
#: Giai đoạn → nhãn tiếng Việt hiển thị cho người dùng.
STAGE_LABEL = {
    "profile": "Đọc hồ sơ tài liệu",
    "glossary": "Chốt thuật ngữ",
    "map": "Trích xuất đơn vị tri thức",
    "consolidate": "Hợp nhất và lập dàn ý",
    "write": "Viết báo cáo",
    "verify": "Kiểm chứng trích dẫn",
    "repair": "Sửa các đoạn lỗi",
    "translate": "Dịch toàn văn",
}
#: Trường không bao giờ được đưa vào `job_events` (nội dung tài liệu/báo cáo).
_PRIVATE_KEYS = frozenset({"text", "content", "markdown", "quote", "snippet", "statement_vi", "title_vi"})


class JobWorker:
    """Một tiến trình worker. `client_factory(job)` trả `LLMClient` cho job (pool thật hoặc client giả)."""

    def __init__(
        self,
        store: WorkerStore,
        client_factory: Callable[[dict], LLMClient],
        *,
        prompts_dir: str | None = None,
        schemas_dir: str | None = None,
        limit: int = 1,
        per_job_limit: int = 1,
        stale: str = "3 minutes",
        style_core_text: str = "",
    ) -> None:
        self.store = store
        self.client_factory = client_factory
        self.prompts_dir = prompts_dir
        self.schemas_dir = schemas_dir
        self.limit = limit
        self.per_job_limit = per_job_limit
        self.stale = stale
        self.style_core_text = style_core_text

    # ------------------------------------------------------------------ vòng lặp
    def run_once(self) -> dict:
        reclaimed = self.store.reclaim_stale(self.stale)
        if reclaimed:
            log.info("thu hồi %d task quá hạn lease", reclaimed)
        expired_gates = self.store.sweep_glossary_gates()
        if expired_gates:
            log.info("tự xác nhận %d cổng glossary quá hạn", expired_gates)
        promoted = self.store.promote_queued(self.limit)
        for job in promoted:
            self.store.emit(job["id"], "job_queued", message_vi="Đã xếp hàng, chờ worker", data={"level": job["level"]})
        tasks = self.store.claim(limit=self.limit, per_job_limit=self.per_job_limit)
        results = [self.run_task(task) for task in tasks]
        return {
            "reclaimed": reclaimed,
            "gates_auto_confirmed": expired_gates,
            "promoted": len(promoted),
            "claimed": len(tasks),
            "results": results,
        }

    def run_forever(
        self, poll_s: float = 2.0, idle_exit_after: float | None = None
    ) -> None:  # pragma: no cover - vòng lặp dài
        idle_s = 0.0
        while True:
            outcome = self.run_once()
            if outcome["claimed"]:
                idle_s = 0.0
                continue
            time.sleep(poll_s)
            idle_s += poll_s
            if idle_exit_after is not None and idle_s >= idle_exit_after:
                log.info("hàng đợi trống %.0fs — thoát", idle_s)
                return

    # ------------------------------------------------------------------ một task = một giai đoạn
    def run_task(self, task: dict) -> dict:
        """Chạy ĐÚNG giai đoạn của task, dựa trên điểm lưu của giai đoạn trước (M1)."""
        job = self.store.load_job(str(task["job_id"]))
        if job["cancel_requested"]:
            return {"job_id": job["id"], "canceled": True, **self.store.conclude_cancel(job)}
        stage = task["stage"]
        try:
            outcome = self._run_stage(task, job, stage)
        except (DeferredError, LLMError) as exc:
            wait_s = _defer_wait_s(exc)
            if wait_s is None:
                return self._fail_task(task, job, exc)
            # Pool chưa có chỗ (mọi deployment đang bận/hết hạn mức): hoãn task, KHÔNG tính lần thử (§6.10 bước 1).
            self.store.defer(task["id"], wait_s)
            log.info("hoãn task %s %.0fs: %s", task["id"], wait_s, exc)
            return {"job_id": job["id"], "deferred": True, "wait_s": round(wait_s, 1)}
        except Exception as exc:  # noqa: BLE001 - mọi lỗi phải trở thành trạng thái task/job
            return self._fail_task(task, job, exc)

        stats = (
            self.store.one("SELECT tokens_in, tokens_out, cost_usd FROM job_tasks WHERE id = %s", (task["id"],)) or {}
        )
        if stats.get("cost_usd"):
            self.store.record_spend(float(stats["cost_usd"]))
        return self._finish_task(task, job, stage, outcome, stats)

    def _fail_task(self, task: dict, job: dict, exc: Exception) -> dict:
        """Ghi lỗi task/job (thử lại được hay không) và trả kết quả cho `run_once`."""
        retryable = _retryable(exc)
        state = self.store.task_failed(task["id"], code=type(exc).__name__, message=str(exc), retryable=retryable)
        log.warning("task %s lỗi (%s): %s", task["id"], state, exc)
        if state == "failed":
            remaining = self.store.tx_execute(
                "UPDATE job_tasks SET status = 'failed', error_code = 'job_failed', finished_at = now() "
                "WHERE job_id = %s AND status IN ('pending','running')",
                (job["id"],),
            )
            outcome = self.store.fail_job(job, code=type(exc).__name__, message=str(exc), retryable=retryable)
            return {"job_id": job["id"], "failed": True, "tasks_failed": remaining, **outcome}
        return {"job_id": job["id"], "retry": True, "state": state}

    def _finish_task(self, task: dict, job: dict, stage: str, outcome: dict, stats: dict) -> dict:
        """Ghi kết quả một giai đoạn đã chạy xong (chờ glossary / xong job / còn giai đoạn sau)."""
        if outcome.get("awaiting_glossary"):
            self.store.task_succeeded(
                task["id"], result={"awaiting_glossary": True, "candidates": outcome["candidates"]}
            )
            return {
                "job_id": job["id"],
                "awaiting_glossary": True,
                "candidates": outcome["candidates"],
                "finished": False,
            }

        if outcome.get("finished"):
            result = outcome["result"]
            self.store.task_succeeded(
                task["id"],
                result={"report_id": str(outcome["report_id"]), "grade": result.grade, "stats": result.stats},
                tokens_in=int(stats.get("tokens_in") or 0),
                tokens_out=int(stats.get("tokens_out") or 0),
                cost_usd=float(stats.get("cost_usd") or 0),
            )
            log.info(
                "job %s xong: hạng %s, coverage %.2f, %d lời gọi LLM",
                job["id"],
                result.grade,
                float(result.stats.get("coverage_core") or 0),
                result.stats_llm.calls,
            )
            return {"job_id": job["id"], "report_id": outcome["report_id"], "grade": result.grade, "finished": True}

        self.store.task_succeeded(
            task["id"],
            result={"stage": stage, "done": True},
            tokens_in=int(stats.get("tokens_in") or 0),
            tokens_out=int(stats.get("tokens_out") or 0),
            cost_usd=float(stats.get("cost_usd") or 0),
        )
        return {"job_id": job["id"], "stage": stage, "finished": False, "awaiting_glossary": False}

    # ------------------------------------------------------------------ một giai đoạn
    def _pipeline_for(self, job: dict, stage: str) -> Pipeline:
        """Dựng pipeline: khôi phục điểm lưu gần nhất trước `stage` rồi nạp glossary đã chốt của job."""
        extraction = self.store.load_extraction(job)
        options = self._options(job)
        _, state = self.store.latest_checkpoint_before(str(job["id"]), stage)
        pipeline = Pipeline.from_state(
            extraction,
            self.client_factory(job),
            options,
            state or {},
            prompts_dir=self.prompts_dir,
            schemas_dir=self.schemas_dir,
        )
        preset = self._preset_glossary(job)
        if preset:
            pipeline.set_confirmed_glossary(preset)
        return pipeline

    def _preset_glossary(self, job: dict) -> list[dict]:
        """Glossary dùng cho job: bản phát hành người dùng chọn (chuẩn/cá nhân) + mục đã chốt của job.

        Mục của job thắng khi trùng `source_term` (người dùng đã sửa thì bản sửa được dùng).
        """
        options = dict(job.get("options") or {})
        merged: dict[str, dict] = {}
        for entry in self.store.load_shared_glossary(list(options.get("glossary_ids") or [])):
            merged[str(entry["source_term"]).casefold()] = entry
        for entry in self.store.load_job_glossary(str(job["id"])):
            merged[str(entry["source_term"]).casefold()] = entry
        return list(merged.values())

    def _options(self, job: dict) -> JobOptions:
        """Lựa chọn của job (cột `options` jsonb) + Lõi văn phong đã ghim — chỉ nhận khoá hợp lệ."""
        raw = dict(job.get("options") or {})
        allowed = {k: v for k, v in raw.items() if k in JobOptions.__dataclass_fields__}
        allowed.pop("style_core_text", None)
        return JobOptions(level=job["level"], style_core_text=self.style_core_text or "", **allowed)

    def _run_stage(self, task: dict, job: dict, stage: str) -> dict:
        pipeline = self._pipeline_for(job, stage)
        before_in, before_out = pipeline.result.stats_llm.tokens_in, pipeline.result.stats_llm.tokens_out
        self.store.heartbeat(task["id"])
        self.store.stage_started(job["id"], stage, int(task["attempt"] or 1))
        if stage == "glossary" and bool(dict(job.get("options") or {}).get("glossary_confirmed")):
            # Người dùng đã chốt qua cổng duyệt: dùng đúng danh sách đó, không gọi P1 lần nữa.
            pipeline.stage_glossary_decided(self.store.load_job_glossary(str(job["id"])))
        else:
            getattr(pipeline, f"stage_{stage}")()
        self._flush_events(job["id"], pipeline.result.events, 0, stage=stage)
        self.store.stage_finished(job["id"], stage, metrics=self._metrics(pipeline, stage))

        delta_in = pipeline.result.stats_llm.tokens_in - before_in
        delta_out = pipeline.result.stats_llm.tokens_out - before_out
        stage_cost = self._cost_usd(delta_in, delta_out)
        self.store.tx_execute(
            "UPDATE job_tasks SET tokens_in = %s, tokens_out = %s, cost_usd = %s, heartbeat_at = now() WHERE id = %s",
            (delta_in, delta_out, stage_cost, task["id"]),
        )

        if stage == "glossary":
            if pipeline.gate_pending:
                candidates = self.store.save_job_glossary(str(job["id"]), pipeline.pending_glossary, status="pending")
                self.store.open_glossary_gate(
                    str(job["id"]), candidates=candidates, minutes=pipeline.options.glossary_review_minutes
                )
                return {"awaiting_glossary": True, "candidates": candidates}
            # Tài liệu ngắn / người dùng bỏ qua cổng: ghi lại các mục đã tự xác nhận.
            self.store.save_job_glossary(str(job["id"]), pipeline.glossary, status="confirmed")

        if stage != stage_sequence(str(job["level"]))[-1]:
            # Huỷ giữa chừng: không lưu điểm lưu để lần chạy sau vẫn dừng đúng chỗ.
            if not self.store.scalar("SELECT cancel_requested FROM jobs WHERE id = %s", (job["id"],)):
                self.store.save_checkpoint(job["id"], stage, pipeline.as_state())
            return {"finished": False, "stage": stage}

        result = finalize(pipeline)
        if str(job["level"]) == "full_translation":
            from visynth.pipeline.run import build_translation_markdown

            result.markdown = build_translation_markdown(pipeline, result, pipeline.ext)
        shadow_usd = self._cost_usd(result.stats_llm.tokens_in, result.stats_llm.tokens_out)
        self.store.tx_execute("UPDATE jobs SET actual_shadow_usd = %s WHERE id = %s", (shadow_usd, job["id"]))
        report_id = self.store.save_report(job, result, pipeline.segment_rows())
        self.store.finish_job(job, result)
        return {"finished": True, "report_id": report_id, "result": result}

    def _cost_usd(self, tokens_in: int, tokens_out: int) -> float:
        """Chi phí 'bóng' theo giá tham chiếu — dùng chính hàm SQL `llm_cost_usd` để không lệch giá."""
        if not tokens_in and not tokens_out:
            return 0.0
        row = self.store.one(
            """SELECT model FROM llm_prices WHERE effective_from <= current_date
                ORDER BY effective_from DESC, model LIMIT 1"""
        )
        if not row:
            return 0.0
        return float(
            self.store.scalar(
                "SELECT llm_cost_usd(%s, current_date, %s, 0, %s, 0)",
                (row["model"], tokens_in, tokens_out),
            )
            or 0
        )

    def _metrics(self, pipeline: Pipeline, stage: str) -> dict:
        result = pipeline.result
        metrics = {
            "stage": stage,
            "units": len(result.units),
            "sections": len(result.sections),
            "blocks": len(result.blocks),
            "llm_calls": result.stats_llm.calls,
            "tokens_in": result.stats_llm.tokens_in,
            "tokens_out": result.stats_llm.tokens_out,
        }
        if str(getattr(result, "level", "")) == "full_translation":
            # §6.10: số đo của giai đoạn dịch nằm trong `job_stages.metrics` để đối chiếu hạng chất lượng.
            metrics.update(result.stats_llm.extra.get("translate") or {})
            metrics["translation_items"] = len(getattr(result, "translation_items", []) or [])
            metrics["flagged_ratio"] = round(
                sum(1 for i in getattr(result, "translation_items", []) or [] if i.flagged)
                / max(1, len(getattr(result, "translation_items", []) or [])),
                4,
            )
        return metrics

    def _flush_events(self, job_id: str, events: list[dict], emitted: int, *, stage: str | None = None) -> int:
        """Đẩy sự kiện mới của pipeline vào `job_events` (đã che nội dung tài liệu)."""
        for event in events[emitted:]:
            kind = _EVENT_MAP.get(event["type"], "stage_progress")
            data = {key: value for key, value in (event.get("data") or {}).items() if key not in _PRIVATE_KEYS}
            message = None
            if kind == "stage_completed":
                message = f"Xong: {STAGE_LABEL.get(stage or '', stage or '')}"
            elif event["type"] == "warning":
                message = str(data.get("code") or "cảnh báo")
            self.store.emit(
                job_id,
                kind,
                stage=stage,
                message_vi=message,
                data=_json_safe(data),
                progress=self.store.progress(job_id),
            )
        return len(events)


def _defer_wait_s(exc: Exception) -> float | None:
    """Số giây cần hoãn nếu lỗi là "pool chưa có chỗ"; `None` nghĩa là lỗi thật."""
    if isinstance(exc, DeferredError):
        return exc.wait_s
    if isinstance(exc, LLMError) and exc.outcome.kind == "deferred":
        return float(exc.outcome.retry_after_s or 60.0)
    return None


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, LLMError) and exc.outcome.kind in RETRYABLE:
        return True
    return bool(getattr(exc, "retryable", False))


def _json_safe(data: dict[str, Any]) -> dict[str, Any]:
    """Chỉ giữ giá trị JSON hoá được (giá trị lạ bị bỏ, không làm hỏng sự kiện)."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:500]
        elif isinstance(value, (list, tuple)):
            out[key] = [v for v in list(value)[:20] if isinstance(v, (str, int, float, bool)) or v is None]
        elif isinstance(value, dict):
            out[key] = {
                k: v for k, v in list(value.items())[:20] if isinstance(v, (str, int, float, bool)) or v is None
            }
    return out
