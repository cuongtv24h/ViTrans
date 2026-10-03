"""Điểm vào của dịch vụ API (M1).

Chạy thật:  `uvicorn visynth_api.app:build_app --factory --host 0.0.0.0 --port 8000`
Kiểm thử:   `create_app(settings, db)` để tiêm cấu hình và kết nối sẵn.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from visynth_api import __version__
from visynth_api.db import Database
from visynth_api.errors import Problem, problem_handler
from visynth_api.headers import SecurityHeadersMiddleware
from visynth_api.limits import RateLimiter, RateLimitMiddleware
from visynth_api.routers import account, admin, catalog, curation, documents, glossaries, jobs, pool_admin, reports
from visynth_api.settings import Settings, load_settings

log = logging.getLogger("visynth.api")

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    """Dựng ứng dụng FastAPI. Không mở kết nối CSDL khi khởi tạo trừ khi `db` được truyền vào."""
    settings = settings or load_settings()
    app = FastAPI(
        title="ViSynth API",
        version=__version__,
        description="Dịch tổng hợp tài liệu Việt–Anh có kiểm chứng nguồn (M1)",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.db = db if db is not None else Database(settings.db_dsn)
    # Giới hạn tốc độ (M3): bộ đếm nằm trong CSDL nên nhiều tiến trình API dùng chung một hạn mức.
    app.state.rate_limiter = RateLimiter(app.state.db, salt=settings.rate_limit_salt or settings.session_secret)

    app.add_exception_handler(Problem, problem_handler)
    app.add_exception_handler(Exception, _unhandled)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):  # pragma: no cover - FastAPI mặc định
        return JSONResponse(
            status_code=422,
            content={
                "type": "about:blank",
                "title": "Dữ liệu không hợp lệ",
                "status": 422,
                "code": "validation_error",
                "detail": "tham số/đầu vào không hợp lệ",
                "errors": [
                    {"loc": list(err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")}
                    for err in exc.errors()[:20]
                ],
            },
            media_type="application/problem+json",
        )

    # Thứ tự middleware: cái thêm SAU nằm NGOÀI, nên header bảo mật cũng phủ cả câu trả lời 429.
    # Giới hạn tốc độ đứng trước phần xử lý tuyến để không tốn công parse thân yêu cầu khi đã bị chặn.
    app.add_middleware(RateLimitMiddleware, settings=settings)
    if settings.security_headers:
        app.add_middleware(
            SecurityHeadersMiddleware,
            hsts_max_age_s=settings.hsts_max_age_s if settings.cookie_secure else 0,
        )

    for module in (account, documents, jobs, glossaries, reports, catalog, admin, curation, pool_admin):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/healthz", tags=["ops"])
    def healthz(detail: bool = False) -> dict:
        """Trạng thái sống + phiên bản lược đồ (dùng cho giám sát ngoài máy).

        Mặc định chỉ trả trạng thái và **mã** cảnh báo — đủ để giám sát reo, không lộ số liệu nội bộ.
        `?detail=1` trả kèm con số và việc cần làm (dùng khi người vận hành mở trình duyệt).
        """
        from visynth.worker import alerts as alert_rules
        from visynth.worker import ops

        try:
            db_conn: Database = app.state.db
            payload: dict = {
                "version": __version__,
                "db": {"schema_version": db_conn.schema_version(), "tables": db_conn.table_count()},
            }
            # Cảnh báo tính từ số liệu vận hành thật; CSDL đọc được thì mới có số liệu để nói.
            signal_keys = (
                "tasks_pending",
                "tasks_running",
                "tasks_failed_1h",
                "tasks_stale_running",
                "jobs_active",
                "jobs_waiting_30m",
                "oldest_waiting_min",
                "leases_expired",
                "credentials_quarantined",
                "deployments_open",
                "users_negative_credits",
                "rate_limit_blocks_1h",
                "pool_incidents_1h",
                "database_bytes",
            )
            try:
                signals = {key: value for key, value in ops.health(settings.db_dsn).items() if key in signal_keys}
            except Exception as exc:  # noqa: BLE001 - số liệu phụ, không được làm hỏng healthz
                log.warning("healthz không đọc được số liệu vận hành: %s", exc)
                signals = {}
            found = alert_rules.evaluate(signals) if signals else []
            payload["status"] = alert_rules.status(found) if signals else "ok"
            payload["alerts"] = found if detail else [a["code"] for a in found]
            if detail and signals:
                payload["signals"] = signals
            return payload
        except Exception as exc:  # pragma: no cover - chỉ xảy ra khi CSDL chết
            log.warning("healthz không kết nối được CSDL: %s", exc)
            return {
                "status": "degraded",
                "version": __version__,
                "db": {"error": type(exc).__name__},
                "alerts": ["db_unreachable"],
            }

    # SPA không bước build (M2) phục vụ ngay từ API khi chạy một tiến trình: cùng gốc nên cookie phiên
    # hoạt động, không CORS, và SSE không đi qua hai tầng proxy. Trên VPS, Caddy vẫn là cổng duy nhất.
    # Gắn SAU CÙNG: mount ở "/" sẽ nuốt mọi đường dẫn chưa khớp, nên mọi tuyến API phải đăng ký trước.
    web_dir = Path(settings.web_dir) if settings.web_dir else None
    if web_dir and (web_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    else:
        log.warning("không thấy SPA ở %s — chỉ chạy API", web_dir)

    return app


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("lỗi không lường trước ở %s %s", request.method, request.url.path)
    return await problem_handler(request, Problem(500, "internal_error", "lỗi máy chủ"))


def build_app() -> FastAPI:  # pragma: no cover - dùng bởi uvicorn --factory
    settings = load_settings()
    settings.check()
    return create_app(settings)
