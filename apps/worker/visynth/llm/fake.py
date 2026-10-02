"""Client LLM giả để chạy toàn bộ pipeline mà không tốn token (M0-W1).

Dùng cho test, cho `visynth run --fake` và cho phát triển giao diện khi pool thật chưa sẵn sàng.
Mọi lời gọi được ghi lại vào `client.calls` để kiểm tra prompt/biến đã render đúng.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from visynth.llm.base import LLMError, LLMRequest, LLMResponse, Outcome


@dataclass
class FakeReply:
    """Một phản hồi kịch bản. `outcome != 'ok'` thì `complete()` ném `LLMError`."""

    text: str = ""
    parsed: Any | None = None
    outcome: str = "ok"
    retry_after_s: float | None = None
    latency_ms: int = 7
    model: str = "fake-model"
    deployment_id: str = "fake/deployment"

    @classmethod
    def json(cls, obj: Any, **kw: Any) -> FakeReply:
        return cls(text=json.dumps(obj, ensure_ascii=False), parsed=obj, **kw)

    @classmethod
    def error(cls, outcome: str, **kw: Any) -> FakeReply:
        return cls(outcome=outcome, **kw)


@dataclass
class FakeLLMClient:
    """Trả lời theo kịch bản: danh sách phản hồi cho từng `prompt_id`.

    - `replies`: `{'P0': [FakeReply, ...], '*': [FakeReply, ...]}`; hết kịch bản thì dùng `default`.
    - `handler`: hàm `(LLMRequest) -> FakeReply` khi cần logic động (ưu tiên hơn `replies`).
    - Token đếm thô `len(text)//4` — đủ để kiểm tra luồng, không phải số thật.
    """

    replies: dict[str, list[FakeReply]] = field(default_factory=dict)
    handler: Callable[[LLMRequest], FakeReply] | None = None
    default: FakeReply = field(default_factory=lambda: FakeReply(text=""))
    calls: list[LLMRequest] = field(default_factory=list)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        reply = self._next_reply(request)
        tokens_in = max(1, len(request.text) // 4)
        tokens_out = max(1, len(reply.text) // 4)
        outcome = Outcome(
            reply.outcome,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=reply.latency_ms,
            retry_after_s=reply.retry_after_s,
        )
        if reply.outcome != "ok":
            raise LLMError(outcome, detail=f"fake:{request.prompt_id}", deployment_id=reply.deployment_id)
        return LLMResponse(
            text=reply.text,
            outcome=outcome,
            parsed=reply.parsed,
            model=reply.model,
            deployment_id=reply.deployment_id,
            latency_ms=reply.latency_ms,
        )

    def _next_reply(self, request: LLMRequest) -> FakeReply:
        if self.handler is not None:
            return self.handler(request)
        queue = self.replies.get(request.prompt_id) or self.replies.get("*")
        if queue:
            return queue.pop(0)
        return self.default

    @property
    def calls_of(self) -> dict[str, list[LLMRequest]]:
        out: dict[str, list[LLMRequest]] = {}
        for c in self.calls:
            out.setdefault(c.prompt_id, []).append(c)
        return out


def scripted(entries: dict[str, Iterable[FakeReply | str | dict]]) -> FakeLLMClient:
    """Tiện dụng: `scripted({'P0': [json.dumps(profile)]})` → client có kịch bản."""
    replies: dict[str, list[FakeReply]] = {}
    for pid, items in entries.items():
        queue: list[FakeReply] = []
        for item in items:
            if isinstance(item, FakeReply):
                queue.append(item)
            elif isinstance(item, str):
                queue.append(FakeReply(text=item))
            else:
                queue.append(FakeReply.json(item))
        replies[pid] = queue
    return FakeLLMClient(replies=replies)
