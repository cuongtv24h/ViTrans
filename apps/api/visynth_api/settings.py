"""Cấu hình dịch vụ API (biến môi trường `VISYNTH_*`).

Không có giá trị bí mật nào nằm trong mã: `VISYNTH_DB_DSN` và `VISYNTH_SESSION_SECRET` phải được
cấp lúc chạy (xem `docs/BUILD_PLAN.md` mục VPS). File `.env` do `keyform.py` sinh chỉ dùng ở máy chạy thử.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Các vị trí SPA đã biết. Tách thành hằng số để test được (và để người đọc thấy ngay "image nằm ở đâu").
_WEB_DIR_CANDIDATES: tuple[Path, ...] = (
    Path(__file__).resolve().parents[3] / "apps" / "web",  # chạy từ kho mã
    Path("/app/apps/web"),  # image Docker (WORKDIR /app, `COPY apps ./apps`)
)


def _find_web_dir() -> Path:
    """Thư mục SPA: thử lần lượt các vị trí hợp lý và trả về cái ĐẦU TIÊN có `index.html`.

    Thứ tự: cạnh mã nguồn (chạy từ kho mã) → `/app/apps/web` (image Docker) → `apps/web` trong thư
    mục làm việc hiện tại. Trả về ứng viên đầu tiên dù chưa có `index.html` để thông báo lỗi nói tới
    một đường dẫn có nghĩa.
    """
    candidates = [*_WEB_DIR_CANDIDATES, Path.cwd() / "apps" / "web"]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[0]


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
    #: Dịch vụ parser sandbox (§6.1/§20.4): bóc tách chạy trong container không mạng, read-only.
    #: Trống = bóc tách ngay trong tiến trình API (chỉ dùng ở máy phát triển).
    parser_url: str = ""
    parser_timeout_s: float = 120.0
    #: Thư mục SPA tĩnh (M2). `None` = tự tìm `apps/web` trong kho mã.
    web_dir: Path | None = None
    #: Chỉ để chẩn đoán: khoá chủ được đọc từ tệp thay vì biến môi trường.
    pool_master_key_file_used: bool = False
    #: --- Giới hạn tốc độ (M3, §20.4) -----------------------------------------------------------------
    #: Giá trị mặc định là giá trị CHẠY THẬT (máy phát triển đặt 0 để tắt hẳn hạn mức).
    #: 0 = tắt; đếm theo danh tính (token/IP) và băm trước khi lưu, xem `visynth_api/limits.py`.
    rate_limit_login_per_min: int = 10
    #: Số lần thử mật khẩu cho MỘT email trong 15 phút (chặn dò phân tán qua nhiều IP).
    rate_limit_login_per_account: int = 20
    rate_limit_register_per_hour: int = 5
    rate_limit_redeem_per_hour: int = 10
    rate_limit_takedown_per_hour: int = 5
    rate_limit_upload_per_hour: int = 30
    rate_limit_jobs_per_hour: int = 20
    rate_limit_estimate_per_hour: int = 60
    rate_limit_gate_per_hour: int = 60
    #: Trần thô cho toàn bộ `/api/v1` (chống quét); 0 = tắt.
    rate_limit_api_per_min: int = 300
    #: Muối để băm danh tính — trống thì suy ra từ `session_secret` (đổi muối = bộ đếm bắt đầu lại).
    rate_limit_salt: str = ""
    #: Thêm header bảo mật ở tầng API (HSTS/nosniff/frame-deny/referrer) — không phụ thuộc Caddy.
    security_headers: bool = True
    #: `max-age` của HSTS, chỉ có hiệu lực khi chạy HTTPS (`cookie_secure`).
    hsts_max_age_s: int = 31536000

    def __post_init__(self) -> None:
        if not self.parser_url:
            self.parser_url = os.environ.get("VISYNTH_PARSER_URL", "").rstrip("/")
        if self.web_dir is None:
            env_web = os.environ.get("VISYNTH_WEB_DIR")
            if env_web:
                self.web_dir = Path(env_web)
            else:
                # Không có biến môi trường thì phải TỰ TÌM, vì `pip install .` đặt gói vào
                # site-packages: đường dẫn tính từ `__file__` khi đó không trỏ về mã nguồn, và SPA
                # trên VPS sẽ 404 dù đã `COPY apps ./apps` vào image (bẫy đã gặp ở M2, vá ở M3).
                self.web_dir = _find_web_dir()
        if not self.rate_limit_salt:
            # Muối riêng là tốt nhất, nhưng có muối suy ra vẫn hơn là lưu khoá băm "trần" (đối chiếu được
            # giữa các bản cài). Đổi `session_secret` sẽ làm bộ đếm bắt đầu lại — chấp nhận được.
            self.rate_limit_salt = os.environ.get("VISYNTH_RATE_LIMIT_SALT") or self.session_secret or "visynth-dev"
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
        "parser_url": os.environ.get("VISYNTH_PARSER_URL", "").rstrip("/"),
        "cookie_secure": _bool("VISYNTH_COOKIE_SECURE"),
        "open_signup": _bool("VISYNTH_OPEN_SIGNUP"),
        "signup_credits": _int("VISYNTH_SIGNUP_CREDITS", 0),
        "admin_email": os.environ.get("VISYNTH_ADMIN_EMAIL", ""),
        "worker_poll_s": float(os.environ.get("VISYNTH_WORKER_POLL_S", "2")),
        "worker_limit": _int("VISYNTH_WORKER_LIMIT", 1),
        "worker_idle_exit_s": float(os.environ.get("VISYNTH_WORKER_IDLE_EXIT_S", "0")),
        "base_url": os.environ.get("VISYNTH_BASE_URL", "http://localhost:8000"),
        # Giới hạn tốc độ: đặt `VISYNTH_RATE_LIMIT_*` = 0 để tắt (máy phát triển / bài kiểm thử tải).
        "rate_limit_login_per_min": _int("VISYNTH_RATE_LIMIT_LOGIN_PER_MIN", 10),
        "rate_limit_login_per_account": _int("VISYNTH_RATE_LIMIT_LOGIN_PER_ACCOUNT", 20),
        "rate_limit_register_per_hour": _int("VISYNTH_RATE_LIMIT_REGISTER_PER_HOUR", 5),
        "rate_limit_redeem_per_hour": _int("VISYNTH_RATE_LIMIT_REDEEM_PER_HOUR", 10),
        "rate_limit_takedown_per_hour": _int("VISYNTH_RATE_LIMIT_TAKEDOWN_PER_HOUR", 5),
        "rate_limit_upload_per_hour": _int("VISYNTH_RATE_LIMIT_UPLOAD_PER_HOUR", 30),
        "rate_limit_jobs_per_hour": _int("VISYNTH_RATE_LIMIT_JOBS_PER_HOUR", 20),
        "rate_limit_estimate_per_hour": _int("VISYNTH_RATE_LIMIT_ESTIMATE_PER_HOUR", 60),
        "rate_limit_gate_per_hour": _int("VISYNTH_RATE_LIMIT_GATE_PER_HOUR", 60),
        "rate_limit_api_per_min": _int("VISYNTH_RATE_LIMIT_API_PER_MIN", 300),
        "security_headers": _bool("VISYNTH_SECURITY_HEADERS", True),
        "hsts_max_age_s": _int("VISYNTH_HSTS_MAX_AGE_S", 31536000),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]
