"""Kho khoá API ngoài repo + đọc `.env` lúc chạy (SPEC §17.14, quyết định 02/10/2026).

Quy tắc bất di bất dịch: **khoá thật không bao giờ nằm trong cây mã nguồn, không vào git, không vào log,
không in ra màn hình**. Khoá chỉ tồn tại ở một trong hai nơi:

* biến môi trường (nạp từ `.env` đã bị `.gitignore`, hoặc do shell cấp) — `secret_ref` dạng `env:TÊN`;
* kho đã mã hoá `~/.config/visynth/secrets.json` (quyền 600) — `secret_ref` dạng `enc:credential-id`.
  Khoá chủ (master key) lấy từ `VISYNTH_MASTER_KEY` hoặc `~/.config/visynth/master.key` (quyền 600).

`pool_config.json` chỉ chứa `secret_ref` + `last4` + `fingerprint`; không chứa khoá.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from visynth.pool.secrets import decrypt_secret, encrypt_secret, fingerprint, last4, load_master_key, new_master_key

DEFAULT_DIR = Path(os.environ.get("VISYNTH_CONFIG_DIR") or (Path.home() / ".config" / "visynth"))
DEFAULT_SECRETS_FILE = Path(os.environ.get("VISYNTH_SECRETS_FILE") or (DEFAULT_DIR / "secrets.json"))
DEFAULT_MASTER_FILE = Path(os.environ.get("VISYNTH_MASTER_FILE") or (DEFAULT_DIR / "master.key"))


class KeyNotFound(RuntimeError):
    """`secret_ref` không phân giải được (thiếu biến môi trường hoặc thiếu mục trong kho)."""


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


# ------------------------------------------------------------------ .env


def load_dotenv(path: str | os.PathLike[str] = ".env", *, override: bool = False) -> list[str]:
    """Nạp `.env` kiểu `TÊN=giá trị` vào `os.environ`. Trả về danh sách tên biến ĐÃ nạp.

    Không hỗ trợ nội suy; bỏ qua dòng trống và dòng bắt đầu bằng `#`. Chỉ đặt biến khi chưa có
    (trừ khi `override=True`) — biến do shell cấp luôn thắng tệp.
    """
    p = Path(path)
    if not p.exists():
        return []
    loaded: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded


# ------------------------------------------------------------------ kho mã hoá


@dataclass
class Registry:
    """Kho khoá đã mã hoá. `path` nằm NGOÀI repo (mặc định `~/.config/visynth/secrets.json`)."""

    path: Path = field(default=DEFAULT_SECRETS_FILE)
    master_file: Path = field(default=DEFAULT_MASTER_FILE)
    _master: bytes | None = None
    _data: dict = field(default_factory=lambda: {"version": 1, "credentials": {}})

    # --- khoá chủ
    def master(self) -> bytes:
        if self._master is not None:
            return self._master
        env = os.environ.get("VISYNTH_MASTER_KEY")
        if env:
            self._master = load_master_key(env)
            return self._master
        if self.master_file.exists():
            self._master = load_master_key(self.master_file.read_text(encoding="utf-8").strip())
            return self._master
        raise KeyNotFound(
            f"chưa có khoá chủ: đặt VISYNTH_MASTER_KEY hoặc tạo {self.master_file} bằng `visynth pool keygen`"
        )

    def keygen(self) -> str:
        """Sinh khoá chủ mới, ghi vào `master_file` (quyền 600) và trả về chuỗi khoá (chỉ in MỘT lần cho chủ máy)."""
        key = new_master_key()
        _write_private(self.master_file, (key + "\n").encode("utf-8"))
        self._master = load_master_key(key)
        return key

    # --- đọc/ghi
    def load(self) -> Registry:
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        return self

    def save(self) -> None:
        _write_private(self.path, json.dumps(self._data, ensure_ascii=False, indent=2).encode("utf-8"))

    @property
    def credentials(self) -> dict:
        return self._data.setdefault("credentials", {})

    # --- thao tác
    def add(self, credential_id: str, secret: str, *, replace: bool = False) -> dict:
        """Mã hoá và lưu một khoá. Trả về bản ghi KHÔNG chứa khoá (để đưa vào `pool_config`)."""
        creds = self.credentials
        if credential_id in creds and not replace:
            raise KeyError(f"credential '{credential_id}' đã tồn tại (dùng replace=True để ghi đè)")
        blob = encrypt_secret(self.master(), credential_id, secret)
        rec = {
            "id": credential_id,
            "fingerprint": fingerprint(self.master(), secret),
            "last4": last4(secret),
            "blob": blob.hex(),
        }
        creds[credential_id] = rec
        self.save()
        return {k: v for k, v in rec.items() if k != "blob"}

    def get(self, credential_id: str) -> str:
        rec = self.credentials.get(credential_id)
        if not rec:
            raise KeyNotFound(f"kho không có credential '{credential_id}' ({self.path})")
        return decrypt_secret(self.master(), credential_id, bytes.fromhex(rec["blob"]))

    def has(self, credential_id: str) -> bool:
        return credential_id in self.credentials

    def ids(self) -> list[str]:
        return sorted(self.credentials)

    def remove(self, credential_id: str) -> None:
        if self.credentials.pop(credential_id, None) is not None:
            self.save()

    # --- phân giải `secret_ref`
    def resolve(self, secret_ref: str) -> str:
        """`env:TÊN` -> biến môi trường; `enc:id` -> kho mã hoá; `TÊN` trần -> coi như biến môi trường."""
        kind, _, name = secret_ref.partition(":")
        if kind == "env":
            value = os.environ.get(name)
            if not value:
                raise KeyNotFound(f"thiếu biến môi trường '{name}' (nạp từ .env hoặc shell)")
            return value
        if kind == "enc":
            return self.get(name)
        value = os.environ.get(secret_ref)
        if not value:
            raise KeyNotFound(f"không hiểu secret_ref '{secret_ref}' (dùng env:TÊN hoặc enc:id)")
        return value

    def describe(self, credential_id: str) -> dict:
        rec = self.credentials.get(credential_id) or {}
        return {"id": credential_id, "last4": rec.get("last4"), "fingerprint": rec.get("fingerprint")}


def resolve_key(secret_ref: str, registry: Registry | None = None) -> str:
    """Tiện ích cho tầng trên: phân giải khoá theo `secret_ref`, tự nạp `.env` trước nếu cần."""
    reg = registry or Registry()
    try:
        return reg.resolve(secret_ref)
    except KeyNotFound:
        if secret_ref.startswith("env:"):
            load_dotenv()
            return reg.resolve(secret_ref)
        raise
