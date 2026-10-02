"""Kho Lõi văn phong bản M0: một tệp JSON ngoài repo, có vòng đời và tính bất biến như §19.3.

M0 chưa có PostgreSQL, nhưng **quy tắc vòng đời phải giống** bản CSDL, nếu không thì cơ chế duyệt chỉ là hình thức:

| Quy tắc §19.3 | Cưỡng chế ở đây |
|---|---|
| Phiên bản `approved` không sửa nội dung, không xoá, không mở lại | `edit()` từ chối; `approve()` từ chối trên phiên bản đã duyệt |
| Không duyệt khi còn quyết định mở | `approve()` gọi `decisions_open()` |
| Không duyệt khi còn quy tắc/ví dụ `origin = ai` mà `reviewed = false`, hoặc lint có lỗi | `approve()` gọi `approval_problems()` |
| Duyệt phải có người duyệt và thời điểm | `approve(by=...)` ghi `approved_by`/`approved_at` |
| Sửa lõi đã duyệt = tạo phiên bản mới (`bump` patch/minor/major) | `bump()` sao chép nội dung sang bản nháp mới |
| Job chụp lại phiên bản đã dùng | `snapshot(core_id, version)` trả nội dung đã `resolve` để ghi vào job |

Kho mặc định: `~/.local/state/visynth/style_cores.json` (ngoài repo, cùng chỗ với sổ `llm_calls`).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from visynth.stylecore.core import approval_problems, compile_style_core, content_sha256, decisions_open, lint

STATUSES = ("draft", "in_review", "approved", "deprecated", "rejected")
BUMPS = ("patch", "minor", "major")


class StyleCoreError(RuntimeError):
    """Vi phạm vòng đời (ví dụ sửa phiên bản đã duyệt)."""


class ApprovalBlocked(StyleCoreError):
    """`approve()` bị chặn — `problems` nói vì sao."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def default_store_path() -> Path:
    return Path(os.environ.get("VISYNTH_STATE_DIR", Path.home() / ".local" / "state" / "visynth")) / "style_cores.json"


def _bump(version: str, kind: str) -> str:
    parts = [int(x) for x in version.split(".")]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


@dataclass
class StyleCoreStore:
    """Kho lõi văn phong + phiên bản. `data` giữ nguyên cấu trúc để tái lập được từ tệp."""

    path: Path = field(default_factory=default_store_path)
    data: dict = field(default_factory=lambda: {"version": 1, "cores": {}})

    # ------------------------------------------------------------------ nạp/ghi
    @classmethod
    def load(cls, path: str | Path | None = None) -> StyleCoreStore:
        p = Path(path).expanduser() if path else default_store_path()
        if not p.exists():
            return cls(path=p)
        return cls(path=p, data=json.loads(p.read_text(encoding="utf-8")))

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".style_cores.tmp.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.path

    # ------------------------------------------------------------------ truy vấn
    def core(self, core_id: str) -> dict:
        try:
            return self.data["cores"][core_id]
        except KeyError:
            raise StyleCoreError(f"không có lõi '{core_id}' trong kho") from None

    def version(self, core_id: str, version: str | None = None) -> dict:
        versions = self.core(core_id)["versions"]
        if not versions:
            raise StyleCoreError(f"lõi '{core_id}' chưa có phiên bản nào")
        if version is None:
            return versions[-1]
        for v in versions:
            if v["version"] == version:
                return v
        raise StyleCoreError(f"lõi '{core_id}' không có phiên bản '{version}'")

    def find(self, version_id: str) -> tuple[dict, dict] | None:
        """Tìm theo `core_id@version` hoặc chỉ `core_id` (bản mới nhất)."""
        core_id, _, version = version_id.partition("@")
        if core_id not in self.data["cores"]:
            return None
        return self.core(core_id), self.version(core_id, version or None)

    # ------------------------------------------------------------------ tạo và sửa
    def create(
        self,
        core_id: str,
        content: dict,
        *,
        version: str = "0.1.0",
        parent_id: str | None = None,
        open_decisions: list | None = None,
        by: str | None = None,
        at: float | None = None,
    ) -> dict:
        if core_id in self.data["cores"]:
            raise StyleCoreError(f"lõi '{core_id}' đã tồn tại")
        record = {
            "id": core_id,
            "parent_id": parent_id,
            "created_at": at if at is not None else time.time(),
            "versions": [
                {
                    "version": version,
                    "status": "draft",
                    "content": content,
                    "content_sha256": content_sha256(content),
                    "open_decisions": open_decisions or [],
                    "answers": {},
                    "history": [{"action": "create", "by": by, "at": at if at is not None else time.time()}],
                }
            ],
        }
        self.data["cores"][core_id] = record
        return record

    def _touch(self, core_id: str, version: dict, action: str, by: str | None, detail: str | None = None) -> None:
        version["history"].append({"action": action, "by": by, "at": time.time(), "detail": detail})

    def edit(self, core_id: str, content: dict, *, version: str | None = None, by: str | None = None) -> dict:
        v = self.version(core_id, version)
        if v["status"] in ("approved", "deprecated"):
            raise StyleCoreError(
                f"phiên bản {v['version']} đã ở trạng thái {v['status']} — không sửa được; dùng `bump` để tạo bản nháp mới"
            )
        v["content"] = content
        v["content_sha256"] = content_sha256(content)
        self._touch(core_id, v, "edit", by)
        return v

    def bump(self, core_id: str, kind: str = "patch", *, by: str | None = None) -> dict:
        """Sao chép nội dung của phiên bản mới nhất sang một bản nháp mới (copy-on-write §19.3)."""
        if kind not in BUMPS:
            raise StyleCoreError(f"kiểu tăng phiên bản không hợp lệ: {kind} (dùng {', '.join(BUMPS)})")
        latest = self.version(core_id)
        new = {
            "version": _bump(latest["version"], kind),
            "status": "draft",
            "content": json.loads(json.dumps(latest["content"])),
            "content_sha256": latest["content_sha256"],
            "open_decisions": [],
            "answers": {},
            "history": [
                {
                    "action": "bump",
                    "by": by,
                    "at": time.time(),
                    "detail": f"từ {latest['version']} ({kind})",
                }
            ],
        }
        self.core(core_id)["versions"].append(new)
        return new

    # ------------------------------------------------------------------ vòng đời duyệt
    def answer(
        self, core_id: str, decision_id: str, answer: str, *, version: str | None = None, by: str | None = None
    ) -> dict:
        v = self.version(core_id, version)
        if v["status"] in ("approved", "deprecated"):
            raise StyleCoreError(f"phiên bản {v['version']} đã {v['status']} — không trả lời quyết định được nữa")
        v["answers"][decision_id] = answer
        self._touch(core_id, v, "answer", by, f"{decision_id}={answer}")
        return v

    def submit(self, core_id: str, *, version: str | None = None, by: str | None = None) -> dict:
        v = self.version(core_id, version)
        if v["status"] != "draft":
            raise StyleCoreError(f"chỉ gửi duyệt được từ trạng thái draft (đang là {v['status']})")
        v["status"] = "in_review"
        self._touch(core_id, v, "submit", by)
        return v

    def approve(self, core_id: str, *, by: str, version: str | None = None) -> dict:
        """Duyệt một phiên bản. Ném `ApprovalBlocked` kèm danh sách vấn đề nếu chưa đủ điều kiện (§19.3)."""
        v = self.version(core_id, version)
        if v["status"] == "approved":
            raise StyleCoreError(f"phiên bản {v['version']} đã được duyệt")
        if v["status"] not in ("in_review", "draft"):
            raise StyleCoreError(f"không duyệt được phiên bản ở trạng thái {v['status']}")
        if not by:
            raise StyleCoreError("duyệt phải có người duyệt (`by`)")
        problems = approval_problems(v["content"], decisions_open(v["open_decisions"], v["answers"]))
        if problems:
            raise ApprovalBlocked(problems)
        v["status"] = "approved"
        v["approved_by"] = by
        v["approved_at"] = time.time()
        self._touch(core_id, v, "approve", by)
        return v

    def reject(self, core_id: str, *, by: str, reason: str = "", version: str | None = None) -> dict:
        v = self.version(core_id, version)
        if v["status"] in ("approved", "deprecated"):
            raise StyleCoreError(f"phiên bản {v['version']} đã {v['status']} — không từ chối được")
        v["status"] = "rejected"
        self._touch(core_id, v, "reject", by, reason)
        return v

    def deprecate(self, core_id: str, *, by: str, version: str | None = None, reason: str = "") -> dict:
        v = self.version(core_id, version)
        if v["status"] != "approved":
            raise StyleCoreError(f"chỉ ngừng dùng được phiên bản đã duyệt (đang là {v['status']})")
        v["status"] = "deprecated"
        self._touch(core_id, v, "deprecate", by, reason)
        return v

    # ------------------------------------------------------------------ dùng trong job
    def snapshot(self, version_id: str) -> dict:
        """Nội dung để ghi vào job (job chụp lại phiên bản, nên kết quả tái lập được dù lõi đổi sau đó)."""
        found = self.find(version_id)
        if found is None:
            raise StyleCoreError(f"không tìm thấy lõi '{version_id}'")
        core, v = found
        return {
            "core_id": core["id"],
            "version": v["version"],
            "status": v["status"],
            "content_sha256": v["content_sha256"],
            "content": v["content"],
            "parent_id": core.get("parent_id"),
        }

    def compile(self, version_id: str, stage: str = "write", max_chars: int | None = None) -> str:
        """Biên dịch phiên bản để đưa vào prompt — CHẶN nếu phiên bản chưa được duyệt (§19.3)."""
        found = self.find(version_id)
        if found is None:
            raise StyleCoreError(f"không tìm thấy lõi '{version_id}'")
        core, v = found
        if v["status"] != "approved":
            raise StyleCoreError(
                f"lõi {core['id']}@{v['version']} đang ở trạng thái {v['status']}: chỉ dùng được bản đã duyệt. "
                "Duyệt bằng `approve` sau khi trả lời hết quyết định mở."
            )
        return compile_style_core(v["content"], stage, max_chars)

    # ------------------------------------------------------------------ tiện ích
    def lint_version(self, version_id: str) -> list[dict]:
        found = self.find(version_id)
        if found is None:
            raise StyleCoreError(f"không tìm thấy lõi '{version_id}'")
        _, v = found
        return [{"severity": p.severity, "path": p.path, "message": p.message} for p in lint(v["content"])]

    def status_rows(self) -> list[dict]:
        rows = []
        for core_id, core in sorted(self.data["cores"].items()):
            for v in core["versions"]:
                rows.append(
                    {
                        "core_id": core_id,
                        "version": v["version"],
                        "status": v["status"],
                        "parent_id": core.get("parent_id"),
                        "rules": len(v["content"].get("rules", [])),
                        "exemplars": len(v["content"].get("exemplars", [])),
                        "open_decisions": len(decisions_open(v["open_decisions"], v["answers"])),
                        "approved_by": v.get("approved_by"),
                        "sha": v["content_sha256"][:12],
                    }
                )
        return rows


__all__ = ["BUMPS", "STATUSES", "ApprovalBlocked", "StyleCoreError", "StyleCoreStore", "default_store_path"]
