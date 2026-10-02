"""Glossary chuẩn (§19.5): P13 hài hoà đề xuất, hàng đợi duyệt, bản phát hành bất biến.

Đường đi đúng như sơ đồ §19.5 — phần dễ sai nhất là **bằng chứng**: model chỉ trả `ctx_ids`, code ghép
đoạn trích thật bằng `attach_evidence()` và loại `ctx_id` lạ. Trạng thái chỉ đi một chiều:
`suggested → confirmed | rejected`; mục `rejected` được GIỮ LẠI để P1/P13 không đề xuất lại; bản phát hành
chỉ chứa mục `confirmed` và là ảnh chụp bất biến.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from visynth.checks.merge import attach_evidence, merge_candidates
from visynth.prompts import default_prompts_dir, default_schemas_dir, render_prompt
from visynth.structured import SchemaStore
from visynth.textnorm import nfc

PROMPT_ID = "P13"
PROMPT_FILE = "P13_glossary_harmonizer"
STATUSES = ("suggested", "confirmed", "rejected")


def term_key(term: str) -> str:
    """Khoá so khớp thuật ngữ: NFC, gộp khoảng trắng, không phân biệt hoa/thường (§19.5 điểm 5)."""
    return re.sub(r"\s+", " ", nfc(term)).strip().casefold()


def default_store_path() -> Path:
    return Path(os.environ.get("VISYNTH_STATE_DIR", Path.home() / ".local" / "state" / "visynth")) / "glossary.json"


class GlossaryError(RuntimeError):
    """Thao tác sai vòng đời glossary (ví dụ duyệt mục đã từ chối)."""


# ------------------------------------------------------------------ render P13


def render_p13(
    merged: list[dict],
    *,
    policy: dict,
    existing: list[dict],
    pairs: list[dict] | None = None,
    max_entries: int = 60,
) -> Any:
    return render_prompt(
        default_prompts_dir(),
        PROMPT_ID,
        {
            "merged_candidates_json": json.dumps(merged, ensure_ascii=False, indent=1),
            "existing_glossary_json": json.dumps(existing, ensure_ascii=False, indent=1),
            "terminology_policy_json": json.dumps(policy or {}, ensure_ascii=False, indent=1),
            "reference_pairs_json": json.dumps(pairs or [], ensure_ascii=False, indent=1),
            "max_entries": max_entries,
        },
    )


def demo_proposals(merged: list[dict]) -> dict:
    """Đề xuất giả chạy khô: lấy biến thể nhiều tài liệu nhất, đánh dấu `needs_human` khi có xung đột."""
    entries = []
    for m in merged[:20]:
        best = m["variants"][0]
        entries.append(
            {
                "source_term": m["source_term"],
                "target_term": best["target_term"],
                "keep_original": m["keep_original_votes"]["false"] < m["keep_original_votes"]["true"],
                "case_sensitive": False,
                "forbidden_variants": [v["target_term"] for v in m["variants"][1:]][:10],
                "term_type": m["term_type"],
                "note": "ĐỀ XUẤT GIẢ (chạy khô): chọn biến thể được nhiều tài liệu đề xuất nhất.",
                "confidence": min(0.5, m["avg_confidence"]),
                "needs_human": m["conflict"],
                "question_vi": "Các tài liệu mẫu dùng nhiều cách dịch khác nhau; chọn cách nào?"
                if m["conflict"]
                else None,
                "ctx_ids": [c["ctx_id"] for c in m["ctx"][:4]],
            }
        )
    return {"entries": entries, "dropped": []}


def harmonise(
    client,
    *,
    per_doc: dict[str, list[dict]],
    contexts: dict | None = None,
    policy: dict | None = None,
    existing: list[dict] | None = None,
    pairs: list[dict] | None = None,
    max_entries: int = 60,
    schemas: SchemaStore | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Gộp tất định → P13 → ghép bằng chứng. Trả `(entries, dropped, merged)`."""
    merged = merge_candidates(per_doc, contexts)
    if client is None:
        raw = demo_proposals(merged)
    else:
        from visynth.llm.base import LLMRequest

        prompt = render_p13(merged, policy=policy or {}, existing=existing or [], pairs=pairs, max_entries=max_entries)
        store = schemas or SchemaStore(default_schemas_dir())
        schema = store.load("glossary_proposals.schema.json")
        req = LLMRequest(
            prompt_id="P13",
            system=prompt.system,
            user=prompt.user,
            schema=schema,
            max_output_tokens=prompt.max_output_tokens,
            thinking=prompt.thinking,
            needs={"structured": "required"},
            metadata={"stage": "curate"},
        )
        resp = client.complete(req)
        raw = resp.parsed if resp.parsed is not None else json.loads(resp.text)
        store.validate(raw, schema, what="P13")

    known = {m["source_term"].casefold(): m for m in merged}
    entries, dropped = [], list(raw.get("dropped") or [])
    for e in raw.get("entries") or []:
        m = known.get(nfc(str(e.get("source_term", ""))).strip().casefold())
        if m is None:  # model bịa thuật ngữ ngoài danh sách gộp → loại, không tin model
            dropped.append({"source_term": e.get("source_term", ""), "reason_vi": "không có trong danh sách gộp"})
            continue
        if not re.fullmatch(r"[^/]{1,120}", str(e.get("target_term", ""))):
            dropped.append({"source_term": m["source_term"], "reason_vi": "target_term không hợp lệ"})
            continue
        entries.append(e)
    return attach_evidence(entries, merged), dropped, merged


# ------------------------------------------------------------------ kho glossary + phát hành


def _release_id(n: int) -> str:
    return f"v{n}"


@dataclass
class GlossaryStore:
    """Kho glossary M0 (JSON ngoài repo). `entries` khoá theo `term_key(source_term)`."""

    path: Path = field(default_factory=default_store_path)
    data: dict = field(default_factory=lambda: {"version": 1, "glossary_id": "default", "entries": {}, "releases": []})

    # ------------------------------------------------------------------ nạp/ghi
    @classmethod
    def load(cls, path: str | Path | None = None) -> GlossaryStore:
        p = Path(path).expanduser() if path else default_store_path()
        if not p.exists():
            return cls(path=p)
        return cls(path=p, data=json.loads(p.read_text(encoding="utf-8")))

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".glossary.tmp.")
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
    def entry(self, source_term: str) -> dict:
        key = term_key(source_term)
        if key not in self.data["entries"]:
            raise GlossaryError(f"glossary không có mục '{source_term}'")
        return self.data["entries"][key]

    def confirmed(self) -> list[dict]:
        return [e for e in self.data["entries"].values() if e["status"] == "confirmed"]

    def rows(self, status: str | None = None) -> list[dict]:
        rows = list(self.data["entries"].values())
        if status:
            rows = [r for r in rows if r["status"] == status]
        # mục cần người quyết định lên trước (§19.5 điểm 4)
        return sorted(
            rows,
            key=lambda e: (not e.get("needs_human", False), e["status"] != "suggested", e["source_term"].casefold()),
        )

    # ------------------------------------------------------------------ đề xuất và duyệt
    def suggest(self, entry: dict, *, by: str = "ai") -> str:
        """Thêm mục `suggested`. Trả `added | merged | skipped_rejected | skipped_confirmed`."""
        key = term_key(entry["source_term"])
        current = self.data["entries"].get(key)
        if current is not None:
            if current["status"] == "rejected":
                return "skipped_rejected"  # §19.5 điểm 2: không đề xuất lại mục đã từ chối
            if current["status"] == "confirmed":
                return "skipped_confirmed"
            current["evidence"] = _merge_evidence(current.get("evidence", []), entry.get("evidence", []))
            current["proposed_by"] = current.get("proposed_by", by)
            return "merged"
        self.data["entries"][key] = {
            "source_term": entry["source_term"],
            "target_term": entry["target_term"],
            "keep_original": bool(entry.get("keep_original", False)),
            "case_sensitive": bool(entry.get("case_sensitive", False)),
            "forbidden_variants": list(entry.get("forbidden_variants") or []),
            "term_type": entry.get("term_type", "concept"),
            "note": entry.get("note", ""),
            "confidence": float(entry.get("confidence", 0.0)),
            "needs_human": bool(entry.get("needs_human", False)),
            "question_vi": entry.get("question_vi"),
            "evidence": list(entry.get("evidence") or []),
            "status": "suggested",
            "proposed_by": by,
            "reviewed_by": None,
            "reviewed_at": None,
            "released": [],
        }
        return "added"

    def suggest_many(self, entries: list[dict], *, by: str = "ai") -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in entries:
            outcome = self.suggest(e, by=by)
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts

    def approve(self, source_term: str, *, by: str, target_term: str | None = None, **edits: Any) -> dict:
        entry = self.entry(source_term)
        if entry["status"] == "confirmed":
            raise GlossaryError(f"'{entry['source_term']}' đã được duyệt")
        if entry["status"] == "rejected":
            raise GlossaryError(f"'{entry['source_term']}' đã bị từ chối — mở lại là việc của người quản trị")
        if not by:
            raise GlossaryError("duyệt phải có người duyệt (`by`)")
        if target_term:
            edits["target_term"] = target_term
        for k, v in edits.items():
            if k not in entry:
                raise GlossaryError(f"không sửa được trường '{k}'")
            entry[k] = v
        entry["status"] = "confirmed"
        entry["reviewed_by"] = by
        entry["reviewed_at"] = time.time()
        entry["needs_human"] = False
        entry["question_vi"] = None
        return entry

    def reject(self, source_term: str, *, by: str, reason: str = "") -> dict:
        entry = self.entry(source_term)
        if entry["status"] == "confirmed":
            raise GlossaryError(f"'{entry['source_term']}' đã được duyệt — không từ chối trực tiếp")
        entry["status"] = "rejected"
        entry["reviewed_by"] = by
        entry["reviewed_at"] = time.time()
        entry["note"] = (entry.get("note") or "") + (f" | lý do từ chối: {reason}" if reason else "")
        return entry

    # ------------------------------------------------------------------ phát hành
    def publish(self, *, by: str, summary: str = "") -> dict:
        """Phát hành bản mới: chỉ mục `confirmed`, là ảnh chụp BẤT BIẾN (§19.5 quy tắc 1)."""
        if not by:
            raise GlossaryError("phát hành phải có người phát hành (`by`)")
        confirmed = sorted(self.confirmed(), key=lambda e: e["source_term"].casefold())
        if not confirmed:
            raise GlossaryError("không có mục `confirmed` nào để phát hành")
        release_id = _release_id(len(self.data["releases"]) + 1)
        snapshot = [{k: v for k, v in e.items() if k not in ("released",)} for e in confirmed]
        payload = {
            "release": release_id,
            "at": time.time(),
            "by": by,
            "summary": summary,
            "count": len(snapshot),
            "content_sha256": hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "entries": snapshot,
        }
        self.data["releases"].append(payload)
        for e in confirmed:
            e.setdefault("released", []).append(release_id)
        return payload

    def release(self, release_id: str) -> dict:
        for r in self.data["releases"]:
            if r["release"] == release_id:
                return r
        raise GlossaryError(f"chưa có bản phát hành '{release_id}'")

    def diff(self, release_id: str) -> dict:
        """So bản phát hành với bản trước (thêm/sửa/xoá) — cho nút "Phát hành bản mới" §19.6."""
        names = [r["release"] for r in self.data["releases"]]
        i = names.index(release_id)
        new = {e["source_term"]: e for e in self.release(release_id)["entries"]}
        old = {e["source_term"]: e for e in (self.release(names[i - 1])["entries"] if i else [])}
        return {
            "added": sorted(set(new) - set(old)),
            "removed": sorted(set(old) - set(new)),
            "changed": sorted(t for t in set(new) & set(old) if new[t]["target_term"] != old[t]["target_term"]),
        }


def _merge_evidence(old: list[dict], new: list[dict]) -> list[dict]:
    seen = {(e.get("doc_ref"), e.get("snippet")) for e in old}
    return old + [e for e in new if (e.get("doc_ref"), e.get("snippet")) not in seen]


__all__ = [
    "PROMPT_ID",
    "STATUSES",
    "GlossaryError",
    "GlossaryStore",
    "default_store_path",
    "demo_proposals",
    "harmonise",
    "render_p13",
    "term_key",
]
