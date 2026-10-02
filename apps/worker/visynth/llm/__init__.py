"""Hợp đồng LLM client và bản giả (M0-W1)."""

from visynth.llm.base import FATAL, RETRYABLE, LLMClient, LLMError, LLMRequest, LLMResponse, Outcome, classify_http
from visynth.llm.fake import FakeLLMClient, FakeReply, scripted

__all__ = [
    "FATAL",
    "RETRYABLE",
    "FakeLLMClient",
    "FakeReply",
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "Outcome",
    "classify_http",
    "scripted",
]
