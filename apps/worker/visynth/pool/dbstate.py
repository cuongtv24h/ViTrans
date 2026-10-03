"""Trạng thái pool trong CSDL — đường chạy của VPS (M3, §17).

Vì sao cần: `PoolState` trong `state.py` giữ hạn mức, cooldown, cầu dao và **cách ly khoá** trong RAM
của tiến trình. Trên VPS có nhiều tiến trình (API + worker, `uvicorn --workers`, cron `ops reap`), nên
trạng thái trong RAM nghĩa là:

* hai tiến trình cùng tiêu một ngân sách RPM ⇒ vượt hạn mức nhà cung cấp rồi mới biết;
* khởi động lại worker là quên hết cooldown ⇒ dồn dập gọi lại đúng lúc nhà cung cấp đang chặn;
* khoá bị nhà cung cấp từ chối (401/403) chỉ bị cách ly trong một tiến trình ⇒ tiến trình khác vẫn thử
  lại khoá chết đó, và `ops health` báo `credentials_quarantined = 0` (cảnh báo mù).

Lớp này gọi thẳng các hàm SQL `pool_try_reserve` / `pool_settle` / `pool_snapshot` — bản SQL của cùng
ngữ nghĩa với `state.py` (đã có test đối chiếu hai bản trong `tests/test_pool.py`). Nhờ vậy khoá bị từ
chối được cách ly ở CSDL, lease nằm trong `llm_leases` cho `ops reap` thu hồi, và số liệu sức khoẻ trong
`ops health` phản ánh sự thật.

Chi phí: thêm một giao dịch ngắn cho mỗi lời gọi LLM (một lời gọi LLM mất hàng giây). Đổi lại là tính
đúng — và tính đúng của hạn mức chính là thứ giữ hoà mạng với nhà cung cấp.
"""

from __future__ import annotations

import math

from visynth.llm.base import Outcome
from visynth.pool.state import INF, ReserveResult

#: `p_scope`: rate limit nhà cung cấp báo theo nhóm (khoá) hay theo deployment? Mặc định nhóm, khớp với
#: `state.py` (khi không rõ thì phạt cả hai là sai — phạt nhóm là đủ vì hạn mức thường gắn với khoá).
SCOPE_BY_KIND = {
    "rate_limited_day": "group",
    "rate_limited_minute": "deployment",
}


class DbPoolState:
    """Cùng giao diện với `PoolState` (`try_reserve`/`settle`/`snapshot`) nhưng lưu ở PostgreSQL."""

    def __init__(self, db, *, now=None) -> None:
        self.db = db
        self._snapshots: dict[tuple[str, float], dict] = {}
        self.clock = now

    # ------------------------------------------------------------------ đặt chỗ
    def try_reserve(
        self,
        dep,
        tin: int,
        tout: int,
        priority: str,
        now: float,
        reserve: float = 0.2,
        ttl: float = 300.0,
    ) -> ReserveResult:
        row = self.db.one(
            "SELECT * FROM pool_try_reserve(%s, %s, %s, %s, %s, %s, %s)",
            (dep.id, int(tin), int(tout), priority, float(now), float(reserve), float(ttl)),
        )
        if row is None:  # pragma: no cover - hàm luôn trả một hàng
            return ReserveResult(False, wait_s=INF, reason="no_deployment")
        if row["ok"]:
            return ReserveResult(
                True,
                lease_id=int(row["lease_id"]),
                credential_id=row["credential_id"],
                basis_tokens=int(row["basis_tokens"] or 0),
            )
        wait = row["wait_s"]
        wait = INF if wait is None or math.isinf(float(wait)) else float(wait)
        return ReserveResult(False, wait_s=wait, reason=row["reason"] or "")

    # ------------------------------------------------------------------ quyết toán
    def settle(self, lease_id: int, outcome: Outcome, now: float) -> None:
        self.db.execute(
            "SELECT pool_settle(%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                int(lease_id),
                outcome.kind,
                int(outcome.tokens_in or 0),
                int(outcome.tokens_out or 0),
                int(outcome.latency_ms) if outcome.latency_ms is not None else None,
                float(outcome.retry_after_s) if outcome.retry_after_s is not None else None,
                SCOPE_BY_KIND.get(outcome.kind, "deployment"),
                float(now),
            ),
        )

    # ------------------------------------------------------------------ ảnh chụp
    def snapshot(self, dep, now: float) -> dict:
        key = (dep.id, round(now, 1))
        cached = self._snapshots.get(key)
        if cached is not None:
            return cached
        row = self.db.one("SELECT * FROM pool_snapshot(%s, %s)", (dep.id, float(now)))
        payload = dict(row or {})
        payload.setdefault("headroom", 0.0)
        payload.setdefault("ewma_success", 0.5)
        payload.setdefault("inflight", 0)
        payload.setdefault("cooldown_until", 0.0)
        payload.setdefault("circuit", "closed")
        # Ảnh chụp dùng để xếp hạng ứng viên trong CÙNG một lượt gọi, nên nhớ tạm theo thời điểm là đủ;
        # giữ vô hạn sẽ làm bộ nhớ phình theo số lời gọi.
        if len(self._snapshots) > 512:
            self._snapshots.clear()
        self._snapshots[key] = payload
        return payload

    # ------------------------------------------------------------------ tiện ích
    def incidents(self, *, limit: int = 50) -> list[dict]:  # pragma: no cover - dùng cho admin
        return self.db.all(
            "SELECT id, at, deployment_id, credential_id, kind, detail FROM llm_incidents ORDER BY id DESC LIMIT %s",
            (limit,),
        )
