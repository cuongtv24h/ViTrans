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

    for module in (account, documents, jobs, glossaries, reports, catalog, admin, curation, pool_admin):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/healthz", tags=["ops"])
    def healthz() -> dict:
        """Trạng thái sống + phiên bản lược đồ (dùng cho giám sát ngoài máy)."""
        try:
            db_conn: Database = app.state.db
            return {
                "status": "ok",
                "version": __version__,
                "db": {"schema_version": db_conn.schema_version(), "tables": db_conn.table_count()},
            }
        except Exception as exc:  # pragma: no cover - chỉ xảy ra khi CSDL chết
            log.warning("healthz không kết nối được CSDL: %s", exc)
            return {"status": "degraded", "version": __version__, "db": {"error": type(exc).__name__}}

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
