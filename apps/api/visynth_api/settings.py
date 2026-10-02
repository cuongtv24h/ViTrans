"""Cấu hình dịch vụ API (biến môi trường `VISYNTH_*`).

Không có giá trị bí mật nào nằm trong mã: `VISYNTH_DB_DSN` và `VISYNTH_SESSION_SECRET` phải được
cấp lúc chạy (xem `docs/BUILD_PLAN.md` mục VPS). File `.env` do `keyform.py` sinh chỉ dùng ở máy chạy thử.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass
class Settings:
    """Tham số chạy của API + worker."""

    db_dsn: str = ""
    session_secret: str = ""
    upload_dir: Path = field(default_factory=lambda: Path(os.environ.get("VISYNTH_UPLOAD_DIR", "/tmp/visynth/uploads")))
    pool_config: Path | None = None
    pool_master_key: str = ""  # POOL_MASTER_KEY: khoá chủ mã hoá khoá API (KHÔNG nằm trong CSDL/bản sao lưu)
    pool_master_key_file: Path | None = None  # POOL_MASTER_KEY_FILE: tệp 0400 hoặc Docker secret (§20.5)
    prompts_dir: Path | None = None
    schemas_dir: Path | None = None
    session_ttl_s: int = 14 * 24 * 3600
    max_upload_bytes: int = 25 * 1024 * 1024
    cookie_secure: bool = False
    cookie_name: str = "visynth_session"
    open_signup: bool = False  # khi bật, không cần mã mời (chỉ dùng ở máy chạy thử)
    signup_credits: int = 0  # tín dụng tặng khi đăng ký không mã mời (dev)
    admin_email: str = ""  # email tự động nhận vai trò admin
    worker_poll_s: float = 2.0
    worker_limit: int = 1
    worker_idle_exit_s: float = 0.0  # >0: tự thoát khi hàng đợi trống (chạy một lượt theo cron)
    base_url: str = "http://localhost:8000"
    #: Chỉ để chẩn đoán: khoá chủ được đọc từ tệp thay vì biến môi trường.
    pool_master_key_file_used: bool = False

    def __post_init__(self) -> None:
        if not self.db_dsn:
            self.db_dsn = os.environ.get("VISYNTH_DB_DSN", "")
        if not self.session_secret:
            self.session_secret = os.environ.get("VISYNTH_SESSION_SECRET", "")
        if self.pool_master_key_file is None and os.environ.get("VISYNTH_POOL_MASTER_KEY_FILE"):
            self.pool_master_key_file = Path(os.environ["VISYNTH_POOL_MASTER_KEY_FILE"])
        if not self.pool_master_key:
            self.pool_master_key = os.environ.get("VISYNTH_POOL_MASTER_KEY") or os.environ.get("POOL_MASTER_KEY", "")
        if not self.pool_master_key and self.pool_master_key_file is not None and self.pool_master_key_file.is_file():
            # Docker secret / tệp 0400: khoá không bao giờ nằm trong `.env`, `docker inspect` hay bản sao lưu
            self.pool_master_key = self.pool_master_key_file.read_text(encoding="utf-8").strip()
            self.pool_master_key_file_used = True
        if self.pool_config is None and os.environ.get("VISYNTH_POOL_CONFIG"):
            self.pool_config = Path(os.environ["VISYNTH_POOL_CONFIG"])
        if self.prompts_dir is None and os.environ.get("VISYNTH_PROMPTS_DIR"):
            self.prompts_dir = Path(os.environ["VISYNTH_PROMPTS_DIR"])
        if self.schemas_dir is None and os.environ.get("VISYNTH_SCHEMAS_DIR"):
            self.schemas_dir = Path(os.environ["VISYNTH_SCHEMAS_DIR"])

    def master_key(self) -> bytes | None:
        """Khoá chủ đã kiểm hợp lệ, hoặc None khi chưa cấu hình (khi đó không ghi/đọc được khoá mã hoá)."""
        if not self.pool_master_key:
            return None
        from visynth.pool.secrets import load_master_key

        return load_master_key(self.pool_master_key)

    def check(self) -> None:
        missing = [
            name for name, value in (("db_dsn", self.db_dsn), ("session_secret", self.session_secret)) if not value
        ]
        if missing:
            raise RuntimeError(f"thiếu cấu hình bắt buộc: {', '.join('VISYNTH_' + m.upper() for m in missing)}")


def load_settings(**overrides: object) -> Settings:
    """Đọc cấu hình từ môi trường; `overrides` dùng cho test."""
    base: dict[str, object] = {
        "upload_dir": Path(os.environ.get("VISYNTH_UPLOAD_DIR", "/tmp/visynth/uploads")),
        "cookie_secure": _bool("VISYNTH_COOKIE_SECURE"),
        "open_signup": _bool("VISYNTH_OPEN_SIGNUP"),
        "signup_credits": _int("VISYNTH_SIGNUP_CREDITS", 0),
        "admin_email": os.environ.get("VISYNTH_ADMIN_EMAIL", ""),
        "worker_poll_s": float(os.environ.get("VISYNTH_WORKER_POLL_S", "2")),
        "worker_limit": _int("VISYNTH_WORKER_LIMIT", 1),
        "worker_idle_exit_s": float(os.environ.get("VISYNTH_WORKER_IDLE_EXIT_S", "0")),
        "base_url": os.environ.get("VISYNTH_BASE_URL", "http://localhost:8000"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]
