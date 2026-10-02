"""Danh mục dùng chung cho người dùng: công thức, Lõi văn phong đã duyệt, biểu mẫu takedown.

Chỉ trả dữ liệu đã công khai: công thức `is_official` + công thức của chính người dùng,
Lõi văn phong có phiên bản `approved` (SPEC §19.3, §13).
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, Request

from visynth_api.db import Database
from visynth_api.errors import Problem, sql_problem
from visynth_api.models import TakedownIn
from visynth_api.security import current_user

router = APIRouter()


def _db(request: Request) -> Database:
    return request.app.state.db


@router.get("/recipes", tags=["catalog"])
def list_recipes(request: Request, user: dict = Depends(current_user)) -> dict:
    """Công thức chính thức (is_official) + công thức của tôi."""
    db = _db(request)
    rows = db.all(
        "SELECT id, slug, name, description, visibility, is_official, version, updated_at FROM recipes "
        "WHERE is_official OR owner_id = %s ORDER BY is_official DESC, name",
        (user["id"],),
    )
    return {"items": rows}


@router.get("/recipes/{recipe_id}", tags=["catalog"])
def get_recipe(recipe_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    db = _db(request)
    row = db.one(
        "SELECT id, slug, owner_id, name, description, visibility, is_official, version, config, updated_at "
        "FROM recipes WHERE id = %s AND (is_official OR owner_id = %s)",
        (recipe_id, user["id"]),
    )
    if row is None:
        raise Problem(404, "not_found", "không có công thức này")
    return row


@router.get("/style-cores", tags=["catalog"])
def list_style_cores(request: Request, user: dict = Depends(current_user)) -> dict:
    """Các Lõi văn phong có phiên bản đã duyệt mà người dùng được chọn."""
    db = _db(request)
    rows = db.all(
        """SELECT c.id, c.slug, c.scope, c.name, c.domain, c.locale, c.parent_id,
                  (SELECT v.id FROM style_core_versions v
                    WHERE v.style_core_id = c.id AND v.status = 'approved'
                    ORDER BY v.version DESC LIMIT 1) AS latest_approved_version
             FROM style_cores c
            WHERE c.scope = 'system' OR c.owner_id = %s
            ORDER BY c.scope, c.name""",
        (user["id"],),
    )
    return {"items": rows}


@router.post("/takedown", status_code=202, tags=["catalog"])
def create_takedown(body: TakedownIn, request: Request) -> dict:
    """Biểu mẫu công khai báo cáo vi phạm bản quyền/nội dung (không cần đăng nhập).

    M1 chưa gắn Turnstile; khi lên VPS phải bật (§14.5) — ghi ở `docs/BUILD_PLAN.md`.
    """
    db: Database = _db(request)
    try:
        row = db.one(
            """INSERT INTO takedown_requests (reporter_name, reporter_email, report_id, description)
               VALUES (%s, %s, %s, %s) RETURNING id, status, created_at""",
            (body.reporter_name, body.reporter_email, body.report_id, body.description),
        )
    except psycopg.errors.ForeignKeyViolation as exc:
        raise Problem(404, "not_found", "không có báo cáo này") from exc
    except psycopg.Error as exc:
        raise sql_problem(exc) from exc
    return {"ok": True, "ticket": row["id"], "status": row["status"]}
