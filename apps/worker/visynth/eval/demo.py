"""Kịch bản giả cho M0-W2: chạy trọn P0–P8 trên tài liệu mẫu mà không gọi mạng.

`DemoProducer` đọc prompt ĐÃ render (không đoán mò) và trả lời bằng dữ liệu **suy ra từ chính tài liệu**:
mọi câu trong báo cáo chỉ dùng ý có trong unit, mọi trích đoạn bằng chứng lấy nguyên văn từ đoạn nguồn,
con số nào xuất hiện trong báo cáo cũng có trong nguồn. Nhờ vậy ngoài việc kiểm tra luồng P0–P8, kịch bản này
còn là bài kiểm tra thật cho D1–D9: đổi `tamper` để dựng lỗi rồi xem P5 phát hiện và P7 sửa.

`tamper` (cố ý phá để thử đường sửa lỗi, §6.13):

| Giá trị | Lỗi tiêm vào | Đường sửa mong đợi |
|---|---|---|
| `number` | P4 viết `99%` thay vì `12%` | P5 báo `number_mismatch` → P7 sửa lại đúng |
| `glossary` | P4 dùng thuật ngữ nguồn `segment` chưa dịch | P5 báo `term_inconsistency` → P7 sửa thành `đoạn` |
| `fabricated` | P4 thêm câu `40 ngôn ngữ` không có trong nguồn | P5 báo `fabricated` → P7 không sửa được → khối bị bỏ |
| `plan` | P3 bỏ một unit core khỏi mọi mục | `check_plan` phát hiện → `plan_autofixed` |
| `quote` | P2 trích đoạn bằng chứng không có trong nguồn | P2 chạy lại (`map_retry`), unit thành `unverified` |
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from visynth.extract import Extraction, extract
from visynth.llm.base import LLMRequest
from visynth.llm.fake import FakeLLMClient, FakeReply

#: Tài liệu mẫu trong repo (từ `apps/worker/visynth/eval/demo.py` lên gốc repo rồi vào `eval/fixtures`).
DEMO_FIXTURE = Path(__file__).resolve().parents[4] / "eval" / "fixtures" / "demo_lecture.txt"

TAMPER_MODES = ("number", "glossary", "fabricated", "plan", "quote", "untranslated")

# ------------------------------------------------------------------ dữ liệu suy ra từ tài liệu

#: Văn bản P10 "đọc" được từ một trang quét trong kịch bản giả (đủ dài để không bị coi là trang lỗi).
OCR_SAMPLE_TEXT = (
    "Bản quét này được OCR trong kịch bản giả của ViSynth. Nội dung nói về quy trình dịch tài liệu: "
    "giữ nguyên số liệu và mã định danh, dùng đúng thuật ngữ đã chốt, và đánh cờ thay vì đoán khi không chắc. "
) * 3

#: Bảng thuật ngữ do P1 trả về. Phải là thuật ngữ CÓ THẬT trong tài liệu mẫu.
GLOSSARY: list[dict[str, Any]] = [
    {
        "source_term": "pid",
        "target_term": "pid",
        "keep_original": True,
        "alternatives": [],
        "term_type": "acronym",
        "forbidden_variants": ["P-id", "mã PID"],
        "rationale_vi": "Mã đoạn dùng xuyên suốt tài liệu và cả trong báo cáo kỹ thuật; giữ nguyên để đối chiếu.",
    },
    {
        "source_term": "segment",
        "target_term": "đoạn",
        "keep_original": False,
        "alternatives": ["phân đoạn"],
        "term_type": "concept",
        "forbidden_variants": ["segment"],
        "rationale_vi": "Từ tiếng Anh đã có cách gọi tiếng Việt ngắn; tránh dùng lẫn hai cách.",
    },
    {
        "source_term": "pipeline",
        "target_term": "quy trình",
        "keep_original": False,
        "alternatives": ["dây chuyền"],
        "term_type": "concept",
        "forbidden_variants": ["pipeline"],
        "rationale_vi": "Mô tả đúng vai trò trong quy trình thay vì phiên âm từ tiếng Anh.",
    },
]

#: pid -> câu tiếng Việt của đơn vị tri thức. Không có con số nào ngoài nguồn, không có thuật ngữ cấm.
#: Độ dài các câu trong cùng một mục được giữ xấp xỉ nhau để D8 (§6.8) không phải đánh cờ.
UNIT_TEXT: dict[str, dict[str, Any]] = {
    "P000002": {
        "title": "Nguồn gốc tài liệu mẫu",
        "statement": (
            "Tài liệu này là ghi chú bài giảng do nhóm ViSynth tự viết để thử quy trình tổng hợp, không trích "
            "từ nguồn có bản quyền và cũng không nhằm dùng làm dẫn chứng học thuật cho bất kỳ kết luận nào."
        ),
        "type": "admin",
        "importance": "core",
    },
    "P000004": {
        "title": "Vì sao cần tổng hợp thay vì dịch thô",
        "statement": (
            "Một bài giảng 90 phút thường lặp lại phần giới thiệu, phần nhắc lại, ví dụ và hỏi đáp, nên dịch "
            "nguyên văn buộc người đọc tự lọc bỏ; báo cáo tổng hợp giữ ý cốt lõi kèm trích dẫn nguồn."
        ),
        "type": "argument",
        "importance": "core",
    },
    "P000006": {
        "title": "Câu hỏi thứ nhất: đơn vị nào là cốt lõi",
        "statement": "Câu hỏi thứ nhất trước khi viết: đơn vị tri thức nào là cốt lõi và đơn vị nào chỉ là ví dụ minh hoạ?",
        "type": "qa",
        "importance": "core",
    },
    "P000007": {
        "title": "Câu hỏi thứ hai: dữ kiện có đúng nguồn",
        "statement": "Câu hỏi thứ hai: số liệu, ngày tháng và tên riêng có xuất hiện đúng như nguồn hay không, và có bị làm tròn?",
        "type": "qa",
        "importance": "core",
    },
    "P000008": {
        "title": "Câu hỏi thứ ba: đâu là ý tác giả",
        "statement": "Câu hỏi thứ ba: câu nào là ý của tác giả và câu nào là phần diễn giải do hệ thống tự thêm vào?",
        "type": "qa",
        "importance": "core",
    },
    "P000010": {
        "title": "Bốn mức đầu ra theo tỷ lệ độ dài",
        "statement": (
            "Tài liệu nêu bốn mức đầu ra kèm tỷ lệ độ dài: dịch đầy đủ khoảng 100% nguồn, tổng hợp chi tiết khoảng "
            "35%, báo cáo chuyên sâu khoảng 12%, tóm lược điều hành khoảng 3%; mức đầu tiên dùng khi cần đọc từng ý, "
            "mức cuối chỉ giữ ý trung tâm."
        ),
        "type": "fact_data",
        "importance": "core",
        "facts": [
            ("Dịch đầy đủ", "khoảng 100% nguồn"),
            ("Tổng hợp chi tiết", "khoảng 35% nguồn"),
            ("Báo cáo chuyên sâu", "khoảng 12% nguồn"),
            ("Tóm lược điều hành", "khoảng 3% nguồn"),
        ],
    },
    "P000012": {
        "title": "Kiểm tra pid bằng code",
        "statement": "Tài liệu gợi ý kiểm tra bằng code: mọi pid đầu vào phải xuất hiện đúng một lần ở đầu ra, không bị lặp lại.",
        "type": "procedure",
        "importance": "supporting",
    },
    "P000013": {
        "title": "Ba nhóm kiểm tra tất định",
        "statement": (
            "Ba nhóm kiểm tra tất định thường dùng gồm: trích đoạn bằng chứng phải có thật trong nguồn, con số "
            "phải tìm thấy trong nguồn, và thuật ngữ phải khớp bảng đã duyệt."
        ),
        "type": "fact_data",
        "importance": "core",
    },
    "P000014": {
        "title": "Nhóm kiểm tra trích đoạn",
        "statement": "Nhóm thứ nhất: mọi trích đoạn bằng chứng trong báo cáo phải có thật trong nguồn, không được diễn giải lại.",
        "type": "procedure",
        "importance": "minor",
    },
    "P000015": {
        "title": "Nhóm kiểm tra con số",
        "statement": "Nhóm thứ hai: mọi con số trong báo cáo phải tìm thấy trong nguồn, kể cả số phần trăm và ngày tháng.",
        "type": "procedure",
        "importance": "minor",
    },
    "P000016": {
        "title": "Nhóm kiểm tra thuật ngữ",
        "statement": "Nhóm thứ ba: thuật ngữ trong báo cáo phải khớp bảng thuật ngữ đã duyệt, không dùng biến thể bị cấm.",
        "type": "procedure",
        "importance": "minor",
    },
    "P000017": {
        "title": "Nguyên tắc kết luận",
        "statement": (
            "Kết luận của tài liệu: giữ nguyên số liệu, không thêm kiến thức ngoài nguồn, và đánh cờ khi không "
            "chắc thay vì tự đoán."
        ),
        "type": "argument",
        "importance": "core",
    },
}

#: Tên mục báo cáo (S01 = mục tóm tắt đầu tiên theo §6.6 bước 3).
TITLE_VI = {
    "S01": "Tóm tắt điều hành",
    "S02": "Ba câu hỏi trước khi viết báo cáo",
    "S03": "Các mức đầu ra theo tỷ lệ độ dài",
    "S04": "Kiểm tra tất định và nguyên tắc kết luận",
}

#: pid -> section_id mà đơn vị được gán vào mục báo cáo (S01 luôn là mục tóm tắt).
SECTION_OF: dict[str, str] = {
    "P000002": "S01",
    "P000004": "S01",
    "P000006": "S02",
    "P000007": "S02",
    "P000008": "S02",
    "P000010": "S03",
    "P000012": "S04",
    "P000013": "S04",
    "P000014": "S04",
    "P000015": "S04",
    "P000016": "S04",
    "P000017": "S04",
}

SECTION_ORDER = ("S01", "S02", "S03", "S04")


# ------------------------------------------------------------------ tiện ích


def demo_extraction() -> Extraction:
    """Bóc tách tài liệu mẫu trong repo (TXT, tiếng Việt)."""
    return extract(DEMO_FIXTURE)


def _evidence_of(ext: Extraction) -> dict[str, str]:
    """Trích đoạn bằng chứng: câu đầu của mỗi đoạn, lấy nguyên văn nên luôn khớp nguồn."""
    out: dict[str, str] = {}
    for p in ext.paragraphs:
        first = re.split(r"(?<=[.!?])\s+", p.content.strip())[0].strip()
        out[p.pid] = first[:300]
    return out


def _cut_to_words(text: str, limit: int) -> str:
    words = text.split()
    if limit <= 0 or len(words) <= limit:
        return text
    return " ".join(words[:limit]).rstrip(",;:") + "…"


def _tag(body: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", body, re.S)
    return m.group(1) if m else ""


def _json_in(body: str, name: str) -> Any:
    return json.loads(_tag(body, name))


def _fit_targets(raw: dict[str, int], budget: int) -> dict[str, int]:
    """Mục tiêu từng mục theo số từ THẬT của mục đó (D8 §6.8), co đều để tổng nằm trong ±15% ngân sách (`check_plan`).

    Sàn 40 theo `report_plan.schema.json` (target_words minimum = 40). Câu trong mỗi mục được soạn dài gần
    bằng nhau nên "co đều" vẫn giữ mọi khối trong dải ±35% của D8.
    """
    total_raw = sum(raw.values()) or 1
    scale = min(1.3, max(1.0, budget / total_raw)) if budget else 1.0
    targets = {sid: max(40, round(words * scale)) for sid, words in raw.items()}
    if not budget:
        return targets
    lo, hi = 0.85 * budget, 1.15 * budget
    total = sum(targets.values())
    if total > hi:
        factor = hi / total
        targets = {sid: max(40, int(words * factor)) for sid, words in targets.items()}
    total = sum(targets.values())
    biggest = max(targets, key=lambda sid: (targets[sid], sid))
    if total > hi:
        targets[biggest] = max(40, targets[biggest] - (total - int(hi)))
    elif total < lo:
        targets[biggest] += int(lo - total) + 1
    return targets


class DemoProducer:
    """Sinh phản hồi cho từng prompt bằng cách đọc biến ĐÃ render trong `request.user`."""

    def __init__(self, extraction: Extraction | None = None, *, tamper: str | None = None):
        if tamper is not None and tamper not in TAMPER_MODES:
            raise ValueError(f"tamper không hợp lệ: {tamper!r} (chọn trong {TAMPER_MODES})")
        self.ext = extraction if extraction is not None else demo_extraction()
        self.tamper = tamper
        self.para = {p.pid: p.content for p in self.ext.paragraphs}
        self.evidence = _evidence_of(self.ext)
        self.seg_of = self._segments()
        self.unit_id = self._assign_unit_ids()
        self.named_terms: set[str] = set()  # thuật ngữ keep_original đã ghi dạng 'đích (nguồn)'
        self.reported: set[str] = set()  # khối đã bị P5 đánh dấu (lần kiểm tra lại phải sạch)
        self.verdicts: dict[str, str] = {}  # block_id -> phán quyết P5 gần nhất

    # -------------------------------------------------------------- ánh xạ tất định
    def _segments(self) -> dict[str, str]:
        from visynth.segment import segment_extraction

        segs = segment_extraction(self.ext, mode="map", target_tokens=8000)
        return {p.pid: seg.segment_id for seg in segs for p in seg.paragraphs}

    def _assign_unit_ids(self) -> dict[str, str]:
        """Gán U-XXXX đúng thứ tự pipeline (`stage_map` xếp theo segment_id rồi số hiệu local)."""
        pids = sorted((pid for pid in UNIT_TEXT if pid in self.para), key=lambda p: (self.seg_of[p], int(p[1:])))
        return {pid: f"U-{i:04d}" for i, pid in enumerate(pids, 1)}

    def _units_for(self, pid: str) -> list[dict[str, Any]]:
        plan = UNIT_TEXT.get(pid)
        if plan is None:
            return []
        quote = self.evidence[pid]
        if self.tamper == "quote" and pid == "P000004":
            quote = "Câu này hoàn toàn không có trong tài liệu nguồn."
        numbers = re.findall(r"\d{1,3}(?:[.,]\d+)?%?", self.para[pid])
        return [
            {
                "local_id": "u1",
                "type": plan["type"],
                "importance": plan["importance"],
                "title_vi": plan["title"],
                "statement_vi": plan["statement"],
                "topics": ["tổng hợp", "kiểm chứng"] if plan["importance"] != "minor" else ["kiểm chứng"],
                "evidence": [{"pid": pid, "quote": quote}],
                "numbers": [{"source_text": n, "kind": "percent" if n.endswith("%") else "quantity"} for n in numbers],
                "terms": [t["source_term"] for t in GLOSSARY if t["source_term"] in self.para[pid].lower()],
                "relations": [],
                "attribution": "unclear" if plan["type"] == "admin" else "author",
            }
        ]

    def _label(self, pid: str) -> str:
        plan = UNIT_TEXT.get(pid)
        if plan is None:
            return "core"
        return {"admin": "admin", "qa": "qa", "example": "example", "procedure": "core"}.get(plan["type"], "core")

    # -------------------------------------------------------------- P0–P8
    def _p0(self, _request: LLMRequest) -> FakeReply:
        profile = {
            "language_code": self.ext.language_code or "vi",
            "language_confidence": round(self.ext.language_confidence or 0.9, 3),
            "doc_type": "notes",
            "domain": "Kỹ thuật phần mềm — quy trình tổng hợp tài liệu",
            "domain_tags": ["tổng hợp tài liệu", "kiểm chứng", "quy trình"],
            "title_guess": self.ext.title,
            "authors_or_speakers": ["Nhóm ViSynth"],
            "register": "formal",
            "attribution_mode": "neutral_facts",
            "structure": {
                "has_toc": False,
                "has_headings": True,
                "has_speaker_turns": False,
                "has_timecodes": False,
                "has_footnotes": False,
                "has_tables": True,
            },
            "content_risks": ["tables", "code", "many_numbers"],
            "recommended_segmentation": {"strategy": "by_headings", "target_tokens": 8000},
            "glossary_hint": "Giữ nguyên 'pid'; các thuật ngữ tiếng Anh khác dùng từ tiếng Việt ngắn.",
            "notes_vi": "Tài liệu mẫu ngắn: có bảng, danh sách và một khối code.",
        }
        return FakeReply.json(profile)

    def _p1(self, _request: LLMRequest) -> FakeReply:
        full = self.ext.full_text().lower()
        order = [p.pid for p in self.ext.paragraphs]
        candidates = []
        for term in GLOSSARY:
            needle = term["source_term"].lower()
            occurrences = full.count(needle)
            if not occurrences:
                continue
            entry = {
                "source_term": term["source_term"],
                "target_term": term["target_term"],
                "keep_original": term["keep_original"],
                "alternatives": list(term["alternatives"]),
                "term_type": term["term_type"],
                "rationale_vi": term["rationale_vi"],
            }
            entry.update(
                {
                    "confidence": 0.9,
                    "occurrences": occurrences,
                    "first_pid": next(pid for pid in order if needle in self.para[pid].lower()),
                }
            )
            candidates.append(entry)
        return FakeReply.json({"candidates": candidates})

    def _p2(self, request: LLMRequest) -> FakeReply:
        seg_id = _tag(request.user, "segment_id").strip() or "SEG-001"
        text = _tag(request.user, "segment")
        pids = [pid for pid in re.findall(r"\[(P\d{6})\]", text) if pid in self.para]
        units = [dict(u, local_id=f"u{i}") for i, u in enumerate((u for pid in pids for u in self._units_for(pid)), 1)]
        summary = " ".join(UNIT_TEXT[pid]["statement"] for pid in pids if pid in UNIT_TEXT)
        return FakeReply.json(
            {
                "segment_id": seg_id,
                "segment_summary_vi": _cut_to_words(summary, 60) or "Đoạn này chỉ có tiêu đề và nhãn mục.",
                "paragraph_labels": [{"from_pid": pid, "to_pid": pid, "label": self._label(pid)} for pid in pids],
                "units": units,
                "new_terms": [{"source_term": t["source_term"], "target_term": t["target_term"]} for t in GLOSSARY],
                "quality_flags": [],
            }
        )

    def _p3(self, request: LLMRequest) -> FakeReply:
        m = re.search(r"of\s+(\d+)\s+Vietnamese words", request.text)  # câu này nằm ở phần SYSTEM của P3
        budget = int(m.group(1)) if m else 200
        level = _tag(request.user, "level").strip() or "deep_synthesis"
        # `_plannable_units` giữ unit `minor` ở mức detailed_synthesis; các mức khác hệ thống tự coi là bị lược bỏ.
        keep_minor = level == "detailed_synthesis"
        live = {pid for pid in UNIT_TEXT if self.unit_id.get(pid)}
        plan_units = {
            pid: uid for pid, uid in self.unit_id.items() if keep_minor or UNIT_TEXT[pid]["importance"] != "minor"
        }
        by_section = {sid: sorted(pid for pid in plan_units if SECTION_OF.get(pid) == sid) for sid in SECTION_ORDER}
        sections = []
        for sid in SECTION_ORDER:
            sections.append(
                {
                    "id": sid,
                    "kind": "summary" if sid == "S01" else "body",
                    "title_vi": TITLE_VI[sid],
                    "purpose_vi": "Nêu phạm vi và nguồn của báo cáo."
                    if sid == "S01"
                    else f"Trình bày {TITLE_VI[sid].lower()} kèm dẫn chứng.",
                    "unit_ids": [plan_units[pid] for pid in by_section[sid]],
                    "format_hint": "bullets" if sid == "S01" else "mixed",
                }
            )
        if self.tamper == "plan":
            # Bỏ một unit core khỏi mọi mục: `check_plan` phải phát hiện và `autofix_plan` gán lại (§6.13 D8).
            lost = plan_units["P000013"]
            for s in sections:
                s["unit_ids"] = [u for u in s["unit_ids"] if u != lost]
        assigned = {u for s in sections for u in s["unit_ids"]}
        raw = {
            s["id"]: max(12, sum(len(UNIT_TEXT[pid]["statement"].split()) for pid in by_section[s["id"]]))
            for s in sections
        }
        targets = _fit_targets(raw, budget)
        for s in sections:
            s["target_words"] = targets[s["id"]]
        plan = {
            "report_title_vi": "Ghi chú bài giảng: tổng hợp và kiểm chứng",
            "report_subtitle_vi": "Tài liệu mẫu cho M0 — bản tổng hợp có trích dẫn nguồn",
            "sections": sections,
            "merged_groups": [],
            "omitted": [
                {"unit_id": uid, "reason": "level_policy"}
                for pid, uid in plan_units.items()
                if uid not in assigned and pid in live
            ],
            "include_facts_table": True,
            "facts_unit_ids": [self.unit_id["P000010"]],
            "include_glossary_appendix": True,
        }
        return FakeReply.json(plan)

    def _p4(self, request: LLMRequest) -> FakeReply:
        section = _json_in(request.user, "section")
        units = _json_in(request.user, "units")
        blocks: list[dict[str, Any]] = []
        for i, u in enumerate(units, 1):
            pid = next((p for p, uid in self.unit_id.items() if uid == u["id"]), None)
            plan = UNIT_TEXT.get(pid or "", {})
            text = plan.get("statement", "")
            if self.tamper == "number" and "12%" in text:
                text = text.replace("12%", "99%")
            if self.tamper == "glossary" and section["id"] == "S02" and i == 1:
                text += " Bước này chạy trên từng segment của tài liệu."
            if self.tamper == "fabricated" and section["id"] == "S02" and i == 1:
                text += " Quy trình còn tự động dịch tài liệu sang 40 ngôn ngữ khác."
            block: dict[str, Any] = {"type": "paragraph", "markdown_vi": text, "cites": [u["id"]]}
            blocks.append(block)
        return FakeReply.json({"section_id": section["id"], "blocks": blocks, "flags": []})

    def _p5(self, request: LLMRequest) -> FakeReply:
        sec_id = _tag(request.user, "section_id").strip() or "S01"
        blocks = _json_in(request.user, "blocks")
        marker = {"number": "99%", "glossary": " segment ", "fabricated": "40 ngôn ngữ"}.get(self.tamper or "")
        issues_by_kind = {
            "number": ("number_mismatch", "Khối ghi 99% trong khi nguồn chỉ nêu 12%.", "Sửa lại thành 12%."),
            "glossary": ("term_inconsistency", "Thuật ngữ nguồn 'segment' xuất hiện chưa dịch.", "Thay bằng 'đoạn'."),
            "fabricated": ("fabricated", "Câu về 40 ngôn ngữ không có trong đoạn nguồn nào.", "Bỏ câu đó."),
        }
        results: list[dict[str, Any]] = []
        if marker:
            for block in blocks:
                if marker not in block["markdown_vi"] or block["block_id"] in self.reported:
                    continue  # chỉ đánh dấu khối thật sự chứa lỗi; lần kiểm tra lại sau khi sửa phải sạch
                self.reported.add(block["block_id"])
                kind, detail, fix = issues_by_kind[self.tamper]
                verdict = "unsupported" if self.tamper == "fabricated" else "partially_supported"
                self.verdicts[block["block_id"]] = verdict
                results.append(
                    {
                        "block_id": block["block_id"],
                        "verdict": verdict,
                        "issues": [
                            {
                                "type": kind,
                                "detail_vi": detail,
                                "source_quote": self.evidence.get("P000004", "") or None,
                                "suggested_fix_vi": fix,
                            }
                        ],
                    }
                )
        return FakeReply.json({"section_id": sec_id, "results": results})

    def _p6(self, request: LLMRequest) -> FakeReply:
        units = _json_in(request.user, "units")
        blocks = _tag(request.user, "report_blocks")
        ids = re.findall(r"(S\d{2}\.b\d{2})", blocks) or ["S01.b01"]
        return FakeReply.json(
            {
                "results": [
                    {
                        "unit_id": u["id"],
                        "covered": "yes",
                        "block_id": self._block_of(u["id"], ids) or ids[0],
                        "note_vi": "Ý này đã có trong mục tương ứng.",
                    }
                    for u in units
                ]
            }
        )

    def _block_of(self, unit_id: str, ids: list[str]) -> str | None:
        pid = next((p for p, uid in self.unit_id.items() if uid == unit_id), None)
        sid = SECTION_OF.get(pid or "")
        return next((b for b in ids if sid and b.startswith(sid)), None)

    def _p7(self, request: LLMRequest) -> FakeReply:
        sec_id = _tag(request.user, "section_id").strip() or "S01"
        blocks = _json_in(request.user, "blocks")
        issues = _json_in(request.user, "issues")
        replace: list[dict[str, Any]] = []
        for block in blocks:
            if self.verdicts.get(block["block_id"]) in ("unsupported", "contradicted"):
                continue  # viết lại để che lỗi là sai (§6.9): để vòng dọn cuối bỏ khối
            mine = issues.get(block["block_id"]) or []
            types = {i.get("type") for i in mine}
            if not types & {"number_mismatch", "term_inconsistency", "overreach", "missing_nuance"}:
                continue  # `fabricated` không sửa được bằng cách viết lại -> để hệ thống bỏ khối
            text = block["markdown_vi"]
            if "number_mismatch" in types:
                text = text.replace("99%", "12%")
            if "term_inconsistency" in types:
                text = re.sub(r"\bsegment(s)?\b", "đoạn", text)
            replace.append(
                {
                    "block_id": block["block_id"],
                    "block": {"type": block["type"], "markdown_vi": text, "cites": block["cites"]},
                }
            )
        return FakeReply.json({"section_id": sec_id, "replace": replace, "insert_after": [], "delete": []})

    def _p8(self, request: LLMRequest) -> FakeReply:
        stats = _tag(request.user, "stats")
        note = (
            "## Phạm vi & cách xử lý\n\n"
            "Đây là báo cáo tổng hợp và phân tích tài liệu mẫu, KHÔNG phải bản dịch nguyên văn từng câu.\n\n"
            "**Được giữ đầy đủ:** các ý cốt lõi về lý do tổng hợp thay vì dịch thô, ba câu hỏi phải trả lời, "
            "các mức đầu ra và các nhóm kiểm tra tất định.\n\n"
            "**Được cô đọng hoặc lược bớt:** các ví dụ minh hoạ và phần lặp lại đã được rút gọn; "
            "mọi con số và thuật ngữ giữ nguyên theo nguồn.\n\n"
            "**Lưu ý về độ tin cậy:** báo cáo do AI tạo, đã qua kiểm tra tất định và bước kiểm chứng. "
            f"Thống kê lượt gọi: {stats} Hãy đối chiếu đoạn nguồn được trích dẫn trước khi dùng cho quyết định quan trọng."
        )
        return FakeReply(text=note)

    def _p9(self, request: LLMRequest) -> FakeReply:
        """P9 — dịch đầy đủ.

        Tài liệu mẫu đã là tiếng Việt, nên "bản dịch" là chính nguồn **đã áp glossary đã chốt**:
        thay `source_term` (và các `forbidden_variants`) bằng `target_term`, mục `keep_original`
        lần dùng đầu ghi dạng `đích (nguồn)`. Nhờ vậy mọi phép kiểm tất định trong
        `pipeline.translate` đều chạy thật trên kết quả, không phải kịch bản rỗng.

        Tamper `untranslated` bỏ bước áp glossary ⇒ dựng cảnh báo "nghi chưa dịch"/"thuật ngữ nguồn
        chưa dịch" để kiểm đường chạy lại rồi đánh cờ của giai đoạn `translate`.
        """
        seg_id = _tag(request.user, "segment_id").strip()
        segment = _tag(request.user, "segment")
        glossary: list[dict] = []
        raw = _tag(request.user, "glossary").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                glossary = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                glossary = []
        items: list[dict[str, Any]] = []
        for line in segment.split("\n\n"):
            m = re.match(r"\[([^\]]+)\]\s*(.*)", line.strip(), re.S)
            if not m:
                continue
            vi = m.group(2).strip()
            if self.tamper != "untranslated":
                vi = self._apply_glossary(vi, glossary)
            items.append({"pid": m.group(1), "vi": vi})
        return FakeReply.json({"segment_id": seg_id, "items": items, "notes": []})

    def _apply_glossary(self, text: str, glossary: list[dict]) -> str:
        """Thay thuật ngữ nguồn bằng thuật ngữ đích đã chốt (tất định, không gọi model)."""
        out = text
        for entry in glossary:
            target = str(entry.get("target_term") or "").strip()
            source = str(entry.get("source_term") or "").strip()
            if not target or not source:
                continue
            flags = 0 if entry.get("case_sensitive") else re.IGNORECASE
            forms = [source, *(entry.get("forbidden_variants") or [])]
            for form in forms:
                form = str(form).strip()
                if not form or form.casefold() == target.casefold():
                    continue
                replacement = target
                if entry.get("keep_original") and source.casefold() not in self.named_terms:
                    # mục keep_original: lần dùng đầu phải là 'đích (nguồn)' (SPEC §6.6)
                    self.named_terms.add(source.casefold())
                    replacement = f"{target} ({source})"
                out = re.sub(rf"(?<!\w){re.escape(form)}(?!\w)", replacement, out, flags=flags)
        return out

    def _p10(self, request: LLMRequest) -> FakeReply:
        """P10 — OCR: trả văn bản của cụm trang được yêu cầu, kèm mốc `<<<PAGE n>>>`.

        Kịch bản giả dùng chính đoạn văn của tài liệu mẫu (chia đều cho số trang trong `page_range`),
        nên kết quả OCR vẫn là văn bản Việt có thật để các giai đoạn sau kiểm tra được.
        """
        page_range = _tag(request.user, "page_range").strip()
        start, _, end = page_range.partition("-")
        first, last = int(start or 1), int(end or start or 1)
        # Tài liệu quét chưa có đoạn nào (bản thân P10 mới tạo ra chúng) nên có văn bản dự phòng.
        paragraphs = [p.content for p in self.ext.paragraphs if p.content.strip()] or [OCR_SAMPLE_TEXT]
        pages = []
        for page in range(first, last + 1):
            body = paragraphs[(page - 1) % len(paragraphs)]
            pages.append(f"<<<PAGE {page}>>>\n{body}")
        return FakeReply(text="\n\n".join(pages))

    def __call__(self, request: LLMRequest) -> FakeReply:
        handlers = {
            "P0": self._p0,
            "P1": self._p1,
            "P2": self._p2,
            "P3": self._p3,
            "P4": self._p4,
            "P5": self._p5,
            "P6": self._p6,
            "P7": self._p7,
            "P8": self._p8,
            "P9": self._p9,
            "P10": self._p10,
        }
        fn = handlers.get(request.prompt_id)
        if fn is None:  # pragma: no cover - M0 chỉ có P0–P8
            return FakeReply.json({})
        return fn(request)


def demo_producer(extraction: Extraction | None = None, *, tamper: str | None = None) -> DemoProducer:
    """Bộ sinh phản hồi (giữ trạng thái giữa các prompt) cho tài liệu mẫu."""
    return DemoProducer(extraction, tamper=tamper)


def demo_client(
    extraction: Extraction | None = None,
    *,
    tamper: str | None = None,
    level: str = "deep_synthesis",  # mức chỉ đến từ P3 nên producer không cần; giữ tham số cho tương thích
) -> FakeLLMClient:
    """Client giả chạy được trọn P0–P8; `tamper` dựng lỗi cho đường kiểm chứng/sửa lỗi."""
    del level
    return FakeLLMClient(handler=DemoProducer(extraction, tamper=tamper))
