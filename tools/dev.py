#!/usr/bin/env python3
"""MVP chạy ngay trên máy: dựng cả hệ thống bằng MỘT lệnh, không cần Docker.

Vì sao có tệp này: các phần "làm cứng" cho VPS (Caddy, sao lưu, giám sát) không cần thiết để **kiểm tra
luồng hoạt động thực tế**. Công cụ này dựng đúng những gì cần cho việc đó:

* PostgreSQL nhúng (`pgserver`) — không cài gì, không Docker;
* migration Alembic + dữ liệu mẫu (tài khoản demo, mã mời, tín dụng);
* API phục vụ luôn SPA không bước build (cùng gốc ⇒ cookie/SSE hoạt động như trên VPS);
* worker chạy pipeline thật (7 giai đoạn) và cổng glossary;
* **hai chế độ LLM**: chưa có khoá thì dùng client giả (kiểm luồng, không tốn tiền); có khoá trong `.env`
  thì dùng pool thật (chất lượng thật).

Các lệnh:

    python tools/dev.py keys     # biểu mẫu nhập khoá API → .env (quyền 600), KHÔNG in lại khoá
    python tools/dev.py up       # dựng + chạy API và worker (giữ tiền cảnh; Ctrl-C để dừng)
    python tools/dev.py flow     # chạy trọn luồng qua HTTP và in kết quả từng bước
    python tools/dev.py status   # xem trạng thái (CSDL, cổng, chế độ LLM)
    python tools/dev.py reset    # xoá sạch dữ liệu dev

Biến môi trường: `VISYNTH_DEV_DIR` (mặc định `/tmp/visynth-dev`), `VISYNTH_DEV_PORT` (mặc định 8080).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV_DIR = Path(os.environ.get("VISYNTH_DEV_DIR") or "/tmp/visynth-dev")
DEFAULT_PORT = int(os.environ.get("VISYNTH_DEV_PORT") or "8080")
DEMO_EMAIL = "demo@vitrans.example.com"
DEMO_PASSWORD = "demo-12345"
INVITE_CODE = "DEMO-MOI-2026"
FIXTURE = ROOT / "eval" / "fixtures" / "demo_lecture.txt"
LEVEL = "deep_synthesis"

for path in (ROOT / "apps" / "api", ROOT / "apps" / "worker"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


# -------------------------------------------------------------------------------------- tiện ích


def say(message: str) -> None:
    print(message, flush=True)


def load_dotenv() -> dict[str, str]:
    """Đọc `.env` trong repo (nếu có) và đưa vào môi trường, KHÔNG in giá trị ra màn hình."""
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        env[name.strip()] = value.strip().strip("'\"")
    for name, value in env.items():
        os.environ.setdefault(name, value)
    return env


def have_real_keys() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("NVIDIA_API_KEY"))


def dsn_file() -> Path:
    return DEV_DIR / "dsn"


def read_dsn() -> str:
    path = dsn_file()
    if not path.is_file():
        raise SystemExit("chưa dựng CSDL dev — chạy `python tools/dev.py up` trước")
    return path.read_text(encoding="utf-8").strip()


# -------------------------------------------------------------------------------------- CSDL dev


def ensure_database(*, reset: bool = False) -> str:
    """Dựng (hoặc dùng lại) PostgreSQL nhúng rồi chạy migration tới head."""
    import pgserver  # phụ thuộc dev (`pip install -e ".[dev]"`)

    from visynth_api.migrate import upgrade

    if reset and DEV_DIR.exists():
        shutil.rmtree(DEV_DIR, ignore_errors=True)
    (DEV_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(DEV_DIR / "pg"))
    dsn = server.get_uri()
    dsn_file().write_text(dsn + "\n", encoding="utf-8")
    upgrade(dsn)
    return dsn


def seed(dsn: str, *, with_pool: bool) -> None:
    """Dữ liệu tối thiểu để bấm được mọi màn hình: một tài khoản admin demo, mã mời, tín dụng."""
    from visynth_api.db import Database
    from visynth_api.security import hash_password

    db = Database(dsn)
    try:
        user_id = db.scalar("SELECT id FROM users WHERE email = %s", (DEMO_EMAIL,))
        if user_id is None:
            user_id = db.scalar(
                """INSERT INTO users (email, display_name, role, status)
                   VALUES (%s, 'Người dùng demo', 'admin', 'active') RETURNING id""",
                (DEMO_EMAIL,),
            )
            db.execute(
                "INSERT INTO local_credentials (user_id, password_hash) VALUES (%s, %s)",
                (user_id, hash_password(DEMO_PASSWORD)),
            )
            db.execute(
                "INSERT INTO credit_ledger (user_id, delta, reason) VALUES (%s, 500, 'grant_signup')",
                (user_id,),
            )
            say(f"  tài khoản demo: {DEMO_EMAIL} / {DEMO_PASSWORD} (admin, 500 tín dụng)")
        if db.scalar("SELECT count(*) FROM invite_codes WHERE code = %s", (INVITE_CODE,)) == 0:
            db.execute(
                """INSERT INTO invite_codes (code, created_by, credits_grant, max_uses)
                   VALUES (%s, %s, 100, 10)""",
                (INVITE_CODE, user_id),
            )
            say(f"  mã mời: {INVITE_CODE} (100 tín dụng, 10 lượt)")
        if with_pool:
            seed_pool(db)
    finally:
        db.close()


#: Hạn mức đặt tạm cho môi trường dev. **Không phải số đo thật** — trên VPS phải chạy `pool probe`
#: rồi điền số thật (xem `docs/BUILD_PLAN.md` B2); hạn mức sai làm mất hoà mạng với nhà cung cấp.
DEV_LIMITS = {
    "gemini": {"rpm": 8, "tpm": 200_000, "rpd": 400, "tpd": 4_000_000, "concurrency": 2},
    "nvidia": {"rpm": 15, "tpm": 200_000, "rpd": 800, "tpd": 4_000_000, "concurrency": 3},
}

PROVIDERS = {
    "gemini": {
        "kind": "gemini_native",
        "base_url": "https://generativelanguage.googleapis.com",
        "display_name": "Google Gemini",
        "quirks": {"quota_scope": "deployment", "auth_header": "x-goog-api-key"},
    },
    "nvidia": {
        "kind": "openai_compat",
        "base_url": "https://integrate.api.nvidia.com",
        "display_name": "NVIDIA API Catalog (thử nghiệm)",
        "quirks": {"quota_scope": "deployment", "auth_header": "bearer"},
    },
}


def pick_model(kind: str, key: str, base_url: str) -> str | None:
    """Hỏi chính nhà cung cấp xem khoá đang dùng được model nào — thay vì đoán tên model.

    Đây là bài học từ M0: tên model thay đổi liên tục và tài liệu trên mạng mâu thuẫn nhau, nên cách
    duy nhất đúng là `GET /models` bằng chính khoá đó.
    """
    from visynth.pool.adapters import list_models

    try:
        models = list_models({"id": "probe", "kind": kind, "base_url": base_url}, key)
    except Exception as exc:  # noqa: BLE001 - dev tool: báo lý do rồi để người dùng tự chọn
        say(f"  ! không liệt kê được model của {kind}: {exc}")
        return None
    if not models:
        return None
    # Ưu tiên model rẻ/nhanh cho việc dev: flash > lite > instruct/chat > phần còn lại.
    for needle in ("flash", "lite", "instruct", "chat", "llama", "nemotron"):
        for name in models:
            if needle in name.lower() and "embedding" not in name.lower():
                return name
    return models[0]


def seed_pool(db) -> None:
    """Nạp cấu hình pool thật từ khoá trong `.env` (tham chiếu `env:` — khoá KHÔNG vào CSDL)."""
    from visynth.pool import dbstore

    providers, models, groups, deployments, credentials = [], [], [], [], {}
    for provider_id, spec in PROVIDERS.items():
        env_name = "GEMINI_API_KEY" if provider_id == "gemini" else "NVIDIA_API_KEY"
        key = os.environ.get(env_name)
        if not key:
            continue
        model_name = pick_model(spec["kind"], key, spec["base_url"]) or (
            "gemini-3.8-flash" if provider_id == "gemini" else "meta/llama-3.3-70b-instruct"
        )
        providers.append({"id": provider_id, **spec})
        model_id = f"{provider_id}:main"
        models.append(
            {
                "id": model_id,
                "provider": provider_id,
                "model_id": model_name,
                "ctx_in": 1_000_000 if provider_id == "gemini" else 128_000,
                "max_out": 65_536 if provider_id == "gemini" else 8192,
                "structured": "json_schema" if provider_id == "gemini" else "json_object",
                "vision": provider_id == "gemini",
                "pdf": provider_id == "gemini",
                "tokenizer_factor": 1.0,
                "price_key": "gemini-3.8-flash" if provider_id == "gemini" else None,
                "shadow_price_key": "gemini-3.8-flash" if provider_id == "gemini" else None,
                "quality": {"json": 0.85, "vi_write": 0.8, "long_context": 0.8},
                "adapter_options": {"max_tokens_param": "max_tokens"},
            }
        )
        group_id = f"{provider_id}-dev"
        # `trial` + cờ rủi ro cho NVIDIA: nhóm này chỉ được dùng ở gate `dev` (thoả thuận trial cấm
        # dùng cho người dùng thật) — đúng quy tắc đã chốt ở §17.
        tier = "paid" if provider_id == "gemini" else "trial"
        tos = [] if provider_id == "gemini" else ["trial_only"]
        groups.append(
            {
                "id": group_id,
                "provider": provider_id,
                "label": f"{provider_id} (máy dev)",
                "tier": tier,
                "data_policy": "no_training" if provider_id == "gemini" else "unknown",
                "allowed_gates": ["dev"] if provider_id != "gemini" else ["dev", "A"],
                "tos_flags": tos,
                "risk_ack": tos,
                "safety_margin": 0.85,
                "day_margin": 0.95,
                "limits": DEV_LIMITS[provider_id],
                "credentials": [
                    {"id": f"{group_id}-k1", "label": f"khoá {provider_id}", "secret_ref": f"env:{env_name}"}
                ],
            }
        )
        credentials[group_id] = f"env:{env_name}"
        deployments.append(
            {
                "id": f"{group_id}/main",
                "group": group_id,
                "model": model_id,
                "enabled": True,
                "tags": [],
                "limits": DEV_LIMITS[provider_id],
            }
        )
    if not providers:
        say("  ! không có khoá nào trong .env — bỏ qua nạp pool")
        return

    profiles = [
        {
            "name": name,
            "needs": {
                "structured": "json_object",
                "min_ctx_in": 8000,
                "vision": False,
                "pdf": False,
                "min_quality": {"json": 0.5},
            },
            "tiers": [
                {
                    "tier_no": 1,
                    "name": "dev",
                    "select_group_tiers": ["trial"],
                    "strategy": "headroom",
                    "max_wait_s": 20,
                },
                {
                    "tier_no": 2,
                    "name": "paid",
                    "select_group_tiers": ["paid"],
                    "strategy": "headroom",
                    "max_wait_s": 600,
                },
            ],
        }
        for name in ("fast", "writer", "verifier", "curator")
    ]
    profiles.append(
        {
            "name": "ocr",
            "needs": {
                "structured": "json_object",
                "min_ctx_in": 8000,
                "vision": True,
                "pdf": True,
                "min_quality": {"json": 0.5},
            },
            "tiers": [
                {"tier_no": 1, "name": "paid", "select_group_tiers": ["paid"], "strategy": "ordered", "max_wait_s": 600}
            ],
        }
    )
    cfg = {
        "version": 1,
        "note": "Pool dev do tools/dev.py sinh: tham chiếu env, hạn mức đặt tạm (không phải số đo thật).",
        "policy": {
            "priority_reserve": 0.2,
            "lease_ttl_s": 300,
            "diversity_max_wait_s": 30,
            "allow_risk_at_public_gates": False,
        },
        "providers": providers,
        "models": models,
        "groups": groups,
        "deployments": deployments,
        "profiles": profiles,
    }
    result = dbstore.apply_config(db, cfg, dry_run=False)
    deployments = ", ".join(item["id"] for item in cfg["deployments"])
    say(f"  pool thật: {len(cfg['deployments'])} deployment ({deployments})")
    if result.get("applied") is not None and not result.get("applied"):
        say(f"  ! apply_config: {json.dumps(result, ensure_ascii=False)[:200]}")


# -------------------------------------------------------------------------------------- chạy


def env_for_children(dsn: str, port: int) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "VISYNTH_DB_DSN": dsn,
            "VISYNTH_SESSION_SECRET": env.get("VISYNTH_SESSION_SECRET") or "dev-secret-khong-dung-that",
            "VISYNTH_UPLOAD_DIR": str(DEV_DIR / "uploads"),
            "VISYNTH_WEB_DIR": str(ROOT / "apps" / "web"),
            "VISYNTH_PROMPTS_DIR": str(ROOT / "docs" / "prompts"),
            "VISYNTH_SCHEMAS_DIR": str(ROOT / "docs" / "schemas"),
            "VISYNTH_BASE_URL": f"http://localhost:{port}",
            "VISYNTH_ADMIN_EMAIL": DEMO_EMAIL,
            "VISYNTH_COOKIE_SECURE": "false",
            # Máy dev: nới hạn mức để không ai bị chặn khi bấm thử liên tục (hạn mức thật đã có test riêng).
            "VISYNTH_RATE_LIMIT_LOGIN_PER_MIN": "120",
            "VISYNTH_RATE_LIMIT_LOGIN_PER_ACCOUNT": "120",
            "VISYNTH_RATE_LIMIT_API_PER_MIN": "0",
        }
    )
    if not have_real_keys():
        # Không có khoá: pipeline chạy bằng client giả (đủ để kiểm luồng, không tốn tiền, không cần mạng).
        env["VISYNTH_LLM_FAKE"] = "1"
    return env


def start_worker(dsn: str, env: dict[str, str], *, fake: bool) -> subprocess.Popen:
    log = (DEV_DIR / "worker.log").open("a", encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "visynth.cli",
        "worker",
        "--db-dsn",
        dsn,
        "--worker-id",
        "dev-1",
        "--poll",
        "1",
    ]
    cmd.append("--fake" if fake else "--pool-from-db")
    if fake:
        cmd.append("--gate")
        cmd.append("dev")
    say(
        f"  worker: {'client giả (không tốn token)' if fake else 'pool thật trong CSDL'} — log: {DEV_DIR / 'worker.log'}"
    )
    return subprocess.Popen(cmd, env=env, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)


def cmd_up(args: argparse.Namespace) -> int:
    port = args.port or DEFAULT_PORT
    load_dotenv()
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    say("1/4 dựng PostgreSQL nhúng + migration…")
    dsn = ensure_database(reset=args.reset)
    say("2/4 dữ liệu mẫu…")
    real = have_real_keys() and not args.fake
    seed(dsn, with_pool=real)
    env = env_for_children(dsn, port)

    say("3/4 worker…")
    worker = start_worker(dsn, env, fake=not real)

    say(f"4/4 API + SPA ở http://0.0.0.0:{port}  (Ctrl-C để dừng)")
    say(f"    đăng nhập: {DEMO_EMAIL} / {DEMO_PASSWORD} — mã mời {INVITE_CODE}")
    if not real:
        say("    CHƯA có khoá trong .env ⇒ LLM giả. Muốn nội dung thật: `python tools/dev.py keys` rồi chạy lại.")
    say(f"    tài liệu để thử: {FIXTURE.relative_to(ROOT)}")

    import uvicorn

    def stop(*_args: object) -> None:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except Exception:  # noqa: BLE001
            worker.kill()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    os.environ.update(env)
    try:
        uvicorn.run("visynth_api.app:create_app", factory=True, host="0.0.0.0", port=port, log_level="info")
    finally:
        worker.terminate()
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    del args
    shutil.rmtree(DEV_DIR, ignore_errors=True)
    say(f"đã xoá {DEV_DIR}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    del args
    say(f"thư mục dev : {DEV_DIR} ({'có' if DEV_DIR.exists() else 'chưa có'})")
    if dsn_file().is_file():
        say(f"CSDL dev    : có (dsn: {dsn_file()})")
    say(f"khoá trong .env: {'có' if (load_dotenv() or have_real_keys()) else 'KHÔNG'}")
    port = DEFAULT_PORT
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/healthz", timeout=3) as response:
            say(f"API cổng {port}: {response.read().decode('utf-8')[:160]}")
    except Exception as exc:  # noqa: BLE001
        say(f"API cổng {port}: không trả lời ({type(exc).__name__})")
    return 0


# -------------------------------------------------------------------------------------- nhập khoá


def cmd_keys(args: argparse.Namespace) -> int:
    """Biểu mẫu nhập khoá — ghi thẳng `.env` quyền 600, không hiện lại giá trị."""
    del args
    say("Dán khoá API (Enter để bỏ qua khoá không dùng). Khoá KHÔNG được in ra màn hình hay ghi vào log.")
    values: dict[str, str] = {}
    for name, hint in (
        ("GEMINI_API_KEY", "Google AI Studio (khoá chính cho tiếng Việt)"),
        ("NVIDIA_API_KEY", "NVIDIA API Catalog (bản thử nghiệm, chỉ dùng ở gate dev)"),
        ("RESERVE_API_KEY", "tuỳ chọn: một khoá dự phòng theo chuẩn OpenAI"),
    ):
        entered = getpass.getpass(f"  {name} — {hint}: ").strip()
        if entered:
            values[name] = entered

    path = ROOT / ".env"
    existing: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()
    existing.update(values)
    body = [
        "# Nơi DUY NHẤT chứa khoá API thật (đã bị .gitignore). Không dán khoá vào chat/commit.",
        "",
    ]
    for name in ("GEMINI_API_KEY", "NVIDIA_API_KEY", "RESERVE_API_KEY"):
        body.append(f"{name}={existing.get(name, '')}")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    path.chmod(0o600)
    say(f"đã ghi {path} (quyền 600) với {len(values)} khoá mới. Chạy lại `python tools/dev.py up` để dùng.")
    return 0


# -------------------------------------------------------------------------------------- luồng thật


class Client:
    """HTTP client nhỏ (urllib) để chạy luồng thật — không thêm phụ thuộc chỉ để kiểm thử."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.cookie: str | None = None

    def _open(self, method: str, path: str, data: bytes | None, headers: dict[str, str]) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=dict(headers))
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()
                set_cookie = response.headers.get("Set-Cookie")
                if set_cookie:
                    self.cookie = set_cookie.split(";")[0]
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()

    def json(
        self, method: str, path: str, payload: dict | None = None, extra: dict[str, str] | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        headers.update(extra or {})
        status, _, body = self._open(method, path, data, headers)
        try:
            return status, json.loads(body.decode("utf-8"))
        except ValueError:
            return status, {"raw": body[:200].decode("utf-8", "replace")}

    def upload(self, path: str, filename: str, content: bytes, fields: dict[str, str]) -> tuple[int, dict]:
        boundary = f"----visynth{uuid.uuid4().hex}"
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: text/plain\r\n\r\n".encode()
            + content
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        status, _, raw = self._open("POST", path, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            return status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return status, {"raw": raw[:200].decode("utf-8", "replace")}


class Timeout(Exception):
    """Hết thời gian chờ một bước của luồng."""


def wait_for(fn, *, timeout_s: float, every_s: float = 1.0, what: str = "điều kiện"):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(every_s)
    raise Timeout(f"quá hạn {timeout_s:.0f}s khi chờ {what}")


def cmd_flow(args: argparse.Namespace) -> int:
    """Chạy trọn luồng thật (tải tài liệu → bóc tách → báo giá → job → duyệt glossary → báo cáo → xuất).

    Đây là cách kiểm "luồng có chạy không": mọi bước đi qua HTTP như trình duyệt vẫn làm.
    """
    port = args.port or DEFAULT_PORT
    base = f"http://127.0.0.1:{port}/api/v1"
    client = Client(base)
    steps: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        steps.append((name, ok, detail))
        say(f"  [{'x' if ok else ' '}] {name}{(' — ' + detail) if detail else ''}")

    def summary() -> int:
        failed = [name for name, ok, _ in steps if not ok]
        say("")
        if failed:
            say(f"KẾT QUẢ: {len(steps) - len(failed)}/{len(steps)} bước đạt — còn lỗi: {', '.join(failed)}")
            return 1
        say(f"KẾT QUẢ: tất cả {len(steps)} bước đạt")
        return 0

    def document_or_none() -> dict | None:
        payload = client.json("GET", f"/documents/{document_id}")[1]
        return payload if payload.get("status") in ("ready", "failed") else None

    say(f"luồng thật qua {base}")
    document_id: str | None = None
    report_id: str | None = None
    try:
        status, health = client.json("GET", "/healthz")
        record("healthz", status == 200, f"schema={(health.get('db') or {}).get('schema_version')}")

        status, login = client.json("POST", "/auth/login", {"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
        record("đăng nhập", status == 200, f"vai trò={((login.get('user') or {}).get('role'))}")
        if status != 200:
            return summary()

        status, payload = client.upload(
            "/documents",
            FIXTURE.name,
            FIXTURE.read_bytes(),
            {"rights_attested": "true", "title": "Bài giảng mẫu (dev)"},
        )
        upload = payload.get("document") or payload
        document_id = upload.get("id")
        record("tải tài liệu", status in (200, 201, 202) and bool(document_id), f"id={str(document_id)[:8]}")
        if not document_id:
            return summary()

        document = wait_for(document_or_none, timeout_s=180, every_s=1.0, what="bóc tách xong")
        record(
            "bóc tách",
            document.get("status") == "ready",
            f"{document.get('word_count')} từ · {document.get('language_code')} · chất lượng {document.get('extraction_quality')}",
        )

        status, quote = client.json("POST", "/jobs/estimate", {"document_id": document_id, "level": LEVEL})
        record(
            "báo giá",
            status == 200,
            f"{quote.get('credits')} tín dụng · ~${quote.get('cost_usd')} · {quote.get('minutes_low')}–{quote.get('minutes_high')} phút",
        )

        # `POST /jobs` đòi khoá chống trùng (`Idempotency-Key`) — gửi lại cùng khoá sẽ không trừ tiền hai lần.
        idempotency_key = f"dev-flow-{uuid.uuid4().hex[:16]}"
        status, created = client.json(
            "POST",
            "/jobs",
            {"document_id": document_id, "level": LEVEL},
            extra={"Idempotency-Key": idempotency_key},
        )
        job_id = (created.get("job") or {}).get("id")
        record(
            "tạo job",
            status in (200, 201, 202) and bool(job_id),
            f"id={str(job_id)[:8]} · còn {created.get('balance')} tín dụng",
        )
        if not job_id:
            return summary()

        noted: dict[str, object] = {}

        def poll() -> dict | None:
            """Theo dõi job; tới cổng `awaiting_glossary` thì tự duyệt (như người dùng bấm) rồi để chạy tiếp."""
            payload = client.json("GET", f"/jobs/{job_id}")[1]
            job = payload.get("job") or {}
            if job.get("status") == "awaiting_glossary" and "glossary" not in noted:
                entries = [
                    {
                        "source_term": entry["source_term"],
                        "target_term": entry.get("target_term") or entry["source_term"],
                        "keep_original": bool(entry.get("keep_original")),
                        "case_sensitive": bool(entry.get("case_sensitive")),
                        "term_type": entry.get("term_type") or "concept",
                        "status": "confirmed",
                    }
                    for entry in (client.json("GET", f"/jobs/{job_id}/glossary")[1].get("entries") or [])
                    if entry.get("status") == "suggested"
                ]
                noted["glossary"] = len(entries)
                noted["glossary_status"] = client.json(
                    "POST", f"/jobs/{job_id}/glossary/confirm", {"entries": entries}
                )[0]
            if job.get("status") in ("succeeded", "failed", "canceled", "expired"):
                return payload
            return None

        final = wait_for(poll, timeout_s=args.timeout, every_s=2.0, what="job xong")
        if "glossary" in noted:
            record(
                "cổng glossary (tự duyệt rồi chạy tiếp)",
                noted.get("glossary_status") == 202,
                f"{noted['glossary']} mục đã duyệt",
            )
        job = final.get("job") or {}
        record(
            "chạy pipeline",
            job.get("status") == "succeeded",
            f"trạng thái={job.get('status')} · {len(final.get('stages') or [])} giai đoạn",
        )

        report_id = final.get("report_id") or report_id
        if not report_id:
            listing = client.json("GET", "/reports")[1]
            report_id = ((listing.get("items") or [{}])[0]).get("id")
        record("có báo cáo", bool(report_id), f"report={str(report_id)[:8]}")
        if not report_id:
            return summary()

        full = client.json("GET", f"/reports/{report_id}")[1]
        sections = full.get("sections") or []
        blocks = [block for section in sections for block in (section.get("blocks") or [])]
        cited = [block for block in blocks if block.get("cites")]
        record(
            "báo cáo có khối",
            bool(blocks),
            f"{len(sections)} mục · {len(blocks)} khối · {len(cited)} khối có trích dẫn",
        )

        if cited:
            # Khối trích dẫn id đơn vị tri thức (`U-0001`); API trả `unit_sources` để tra ra pid đoạn nguồn.
            cite = cited[0]["cites"][0]
            sources = (full.get("unit_sources") or {}).get(cite) or []
            record("tra trích dẫn → đoạn nguồn", bool(sources), f"{cite} → {', '.join(sources[:3])}")
            pid = sources[0] if sources else cite
            paragraphs = client.json("GET", f"/documents/{document_id}/paragraphs?pids={pid}&limit=1")[1]
            items = paragraphs.get("items") or []
            first = (items or [{}])[0]
            record(
                "mở được đoạn nguồn theo trích dẫn",
                bool(items),
                f"pid={pid} · {len(first.get('content') or '')} ký tự nguồn",
            )
        else:
            record("tra trích dẫn → đoạn nguồn", False, "báo cáo chưa có trích dẫn")

        for kind in ("md", "html"):
            status, _, raw = client._open("GET", f"/reports/{report_id}/export?format={kind}", None, {})
            record(f"xuất {kind.upper()}", status == 200 and len(raw) > 100, f"{len(raw)} byte")
    except Timeout as exc:
        say(f"  [!] {exc}")
    return summary()


# -------------------------------------------------------------------------------------- điểm vào


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MVP chạy ngay (không Docker) — xem docstring trong tệp.")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="dựng CSDL + dữ liệu mẫu rồi chạy API và worker")
    up.add_argument("--port", type=int, default=DEFAULT_PORT)
    up.add_argument("--reset", action="store_true", help="xoá dữ liệu dev trước khi dựng")
    up.add_argument("--fake", action="store_true", help="buộc dùng LLM giả dù .env có khoá")
    up.set_defaults(func=cmd_up)

    flow = sub.add_parser("flow", help="chạy trọn luồng qua HTTP và in kết quả")
    flow.add_argument("--port", type=int, default=DEFAULT_PORT)
    flow.add_argument("--timeout", type=float, default=600.0, help="thời gian tối đa cho một job (giây)")
    flow.set_defaults(func=cmd_flow)

    keys = sub.add_parser("keys", help="nhập khoá API vào .env (không in lại khoá)")
    keys.set_defaults(func=cmd_keys)

    sub.add_parser("status", help="xem trạng thái").set_defaults(func=cmd_status)
    sub.add_parser("reset", help="xoá dữ liệu dev").set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
