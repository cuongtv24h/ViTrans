"""Chạy migration bằng Alembic từ Python (`visynth db migrate`).

Dùng cùng đường với `alembic -c apps/api/alembic.ini upgrade head`: biến môi trường `VISYNTH_DB_DSN`
được đặt tạm cho tiến trình gọi.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = ROOT / "apps" / "api" / "alembic.ini"


def alembic_config(dsn: str | None = None) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ROOT / "apps" / "api" / "alembic"))
    if dsn:
        config.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    return config


def upgrade(dsn: str, revision: str = "head") -> None:
    """Áp migration tới `revision` (mặc định `head`)."""
    os.environ["VISYNTH_DB_DSN"] = dsn
    command.upgrade(alembic_config(dsn), revision)


def downgrade(dsn: str, revision: str = "-1") -> None:
    os.environ["VISYNTH_DB_DSN"] = dsn
    command.downgrade(alembic_config(dsn), revision)


def current(dsn: str) -> None:  # pragma: no cover - in ra cho CLI
    os.environ["VISYNTH_DB_DSN"] = dsn
    command.current(alembic_config(dsn))
