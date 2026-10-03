"""Ngưỡng cảnh báo từ số liệu `ops health` (M3, §20.7).

Vì sao tách khỏi `ops.health`: con số thì khách quan, còn "bao nhiêu là đáng lo" là chính sách. Gộp hai
thứ vào một chỗ thì mỗi lần đổi ngưỡng phải sửa câu SQL, và không ai kiểm thử được ngưỡng.

Cách dùng:

* cron/máy giám sát: `visynth ops health --json` → mã thoát 3 khi có cảnh báo;
* `/healthz` của API trả `alerts` (chỉ mã + mức, không lộ số liệu nội bộ sâu) để giám sát ngoài máy thấy;
* hàm `evaluate` là HÀM THUẦN: đưa một dict số liệu vào, nhận danh sách cảnh báo ra — nên test được
  toàn bộ ngưỡng mà không cần CSDL.

Đổi ngưỡng bằng biến môi trường `VISYNTH_ALERT_<FIELD>` (đặt **-1** để tắt cảnh báo đó; 0 là ngưỡng
hợp lệ — "có một cái là đã đáng lo", ví dụ ví âm hay khoá bị cách ly).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

#: Mức độ: `info` (ghi nhận), `warn` (cần người xem trong ngày), `critical` (gọi người ngay).
SEVERITY_ORDER = {"info": 0, "warn": 1, "critical": 2}


@dataclass(frozen=True)
class Rule:
    """Một ngưỡng: khi `health[field]` VƯỢT `limit` thì cảnh báo."""

    field: str
    limit: float
    severity: str
    code: str
    message: str
    action: str


RULES: tuple[Rule, ...] = (
    Rule(
        "credentials_quarantined",
        0,
        "critical",
        "credential_quarantined",
        "có khoá API bị nhà cung cấp từ chối và đã bị cách ly",
        "kiểm tra khoá trong /admin/pool, xoay khoá mới rồi bật lại; job đang chạy có thể đã hỏng",
    ),
    Rule(
        "users_negative_credits",
        0,
        "warn",
        "negative_credit_balance",
        "có ví người dùng âm — sổ tín dụng không khớp",
        "rá soát `credit_ledger` của người dùng đó; đây là dữ liệu tiền, xử lý trước khi mời thêm người",
    ),
    Rule(
        "tasks_stale_running",
        0,
        "warn",
        "stale_running_tasks",
        "có task đang 'running' nhưng nhịp tim đã cũ — worker có thể đã chết",
        "chạy `visynth ops reap`; nếu lặp lại, xem log worker và giới hạn bộ nhớ container",
    ),
    Rule(
        "leases_expired",
        0,
        "warn",
        "expired_leases",
        "có chỗ đặt (lease) quá hạn chưa thu hồi",
        "`visynth ops reap` (cron mỗi phút) chưa chạy — kiểm crontab trên VPS",
    ),
    Rule(
        "deployments_open",
        0,
        "warn",
        "circuit_open",
        "có deployment đang mở cầu dao (nhà cung cấp lỗi liên tiếp)",
        "xem `llm_incidents`; nếu mọi deployment đều mở thì job sẽ chờ chứ không hỏng ngay",
    ),
    Rule(
        "jobs_waiting_30m",
        2,
        "warn",
        "jobs_waiting",
        "có job chờ quá 30 phút",
        "kiểm worker còn sống, hạn mức pool còn chỗ, và `ops health` xem task có bị kẹt không",
    ),
    Rule(
        "oldest_waiting_min",
        60,
        "warn",
        "queue_stalled",
        "job chờ lâu nhất đã quá 1 giờ",
        "hàng đợi tắc: kiểm worker, `llm_scope_state` cooldown, và trần chi tiêu",
    ),
    Rule(
        "tasks_failed_1h",
        5,
        "warn",
        "tasks_failing",
        "nhiều task hỏng trong 1 giờ",
        "đọc `job_events`/`llm_calls` của các task đó; lỗi lặp lại theo một mã nào?",
    ),
    Rule(
        "rate_limit_blocks_1h",
        50,
        "warn",
        "rate_limit_pressure",
        "có dấu hiệu dò quét hoặc lạm dụng (nhiều yêu cầu bị chặn)",
        "xem `rate_limit_hits` theo phạm vi; cân nhắc siết hạn mức hoặc chặn theo IP ở Caddy",
    ),
    Rule(
        "pool_incidents_1h",
        20,
        "warn",
        "pool_unstable",
        "pool ghi nhiều sự cố trong 1 giờ",
        "xem `llm_incidents`; thường là hạn mức free tier bị đụng trần hoặc nhà cung cấp chập chờn",
    ),
    Rule(
        "database_bytes",
        1_073_741_824,  # 1 GiB: cỡ VPS nhỏ; vượt thì bản sao lưu và khôi phục bắt đầu đau
        "warn",
        "database_growing",
        "CSDL đã vượt 1 GiB",
        "kiểm job_events/llm_calls cũ, chạy `ops purge`; dữ liệu tài liệu phải bị xoá theo hạn lưu",
    ),
)

#: Mã thoát của `visynth ops health` khi có cảnh báo (giám sát ngoài máy bắt được mà không phải đọc log).
ALERT_EXIT_CODE = 3


def thresholds_from_env(base: dict[str, float] | None = None) -> dict[str, float]:
    """Ngưỡng mặc định, ghi đè bằng `VISYNTH_ALERT_<FIELD>` (giá trị **-1** = tắt cảnh báo đó)."""
    limits = {rule.field: float(rule.limit) for rule in RULES} if base is None else dict(base)
    for rule in RULES:
        raw = os.environ.get(f"VISYNTH_ALERT_{rule.field.upper().replace('.', '_')}")
        if raw not in (None, ""):
            limits[rule.field] = float(raw)
    return limits


def evaluate(health: dict[str, Any], limits: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """So số liệu với ngưỡng. Trả danh sách cảnh báo đã sắp theo mức độ giảm dần."""
    limits = thresholds_from_env() if limits is None else limits
    alerts: list[dict[str, Any]] = []
    for rule in RULES:
        value = health.get(rule.field)
        if value is None:
            continue  # nguồn số liệu này không có (bản cũ/khác môi trường) — không đoán bừa
        try:
            number = float(value)
        except (TypeError, ValueError):  # pragma: no cover - phòng khi CSDL trả chuỗi lạ
            continue
        limit = float(limits.get(rule.field, rule.limit))
        if limit < 0:
            continue  # -1 = tắt cảnh báo này (0 vẫn là ngưỡng thật: vượt 0 là đáng lo)
        if number > limit:
            alerts.append(
                {
                    "code": rule.code,
                    "severity": rule.severity,
                    "field": rule.field,
                    "value": number,
                    "limit": limit,
                    "message": rule.message,
                    "action": rule.action,
                }
            )
    alerts.sort(key=lambda a: SEVERITY_ORDER.get(a["severity"], 1), reverse=True)
    return alerts


def status(alerts: list[dict[str, Any]]) -> str:
    """`ok` | `degraded` | `down` — dùng cho `/healthz` và cho giám sát ngoài máy."""
    if not alerts:
        return "ok"
    worst = max(SEVERITY_ORDER.get(a["severity"], 1) for a in alerts)
    return "critical" if worst >= SEVERITY_ORDER["critical"] else "degraded"


def summarize(alerts: list[dict[str, Any]]) -> str:
    """Một dòng cho cron/email: mã cảnh báo kèm con số."""
    if not alerts:
        return "không có cảnh báo"
    parts = [f"{a['code']}={a['value']:g}>{a['limit']:g} ({a['severity']})" for a in alerts]
    return "; ".join(parts)
