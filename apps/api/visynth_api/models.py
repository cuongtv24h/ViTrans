"""Kiểu yêu cầu/trả lời của API (M1). Trả lời dùng thẳng hàng CSDL để khớp `docs/api/openapi.yaml`."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

Level = Literal["full_translation", "detailed_synthesis", "deep_synthesis", "executive_brief"]
JobStatus = Literal["queued", "running", "awaiting_glossary", "succeeded", "failed", "canceled", "expired"]


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    invite_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{6,32}$")
    display_name: str | None = Field(default=None, max_length=120)
    locale: str = Field(default="vi", max_length=10)
    tos_version: str | None = Field(default=None, max_length=40)
    consent_cross_border: bool = False
    consent_shared_processing: bool = False
    age_confirmed: bool = False


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class ConsentsIn(BaseModel):
    tos_version: str | None = Field(default=None, max_length=40)
    consent_cross_border: bool = False
    consent_shared_processing: bool = False
    age_confirmed: bool = False


class RedeemIn(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9-]{6,32}$")


class JobCreate(BaseModel):
    document_id: str
    level: Level
    target_lang: str = Field(default="vi", max_length=10)
    privacy_class: Literal["standard", "private"] = "standard"
    style_core_version_id: str | None = None
    max_cost_usd: float | None = Field(default=None, gt=0)
    options: dict[str, Any] = Field(default_factory=dict)


class InviteCreate(BaseModel):
    """Khớp `POST /admin/invites` của hợp đồng: `count` mã, hạn dùng tuỳ chọn."""

    count: int = Field(default=1, ge=1, le=200)
    credits_grant: int = Field(gt=0, le=10000)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_at: datetime | None = None
    note: str | None = Field(default=None, max_length=200)


class UserPatch(BaseModel):
    role: Literal["user", "curator", "admin"] | None = None
    status: Literal["pending", "active", "suspended"] | None = None


class SpendCapIn(BaseModel):
    daily_spend_cap_usd: float = Field(gt=0, le=10_000)


class GlossaryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    domain: str | None = Field(default=None, max_length=80)
    source_lang: str = Field(default="en", max_length=10)
    target_lang: str = Field(default="vi", max_length=10)


class GlossaryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    domain: str | None = Field(default=None, max_length=80)


class GlossaryEntryIn(BaseModel):
    source_term: str = Field(min_length=1, max_length=120)
    target_term: str = Field(min_length=1, max_length=160)
    keep_original: bool = False
    case_sensitive: bool = False
    forbidden_variants: list[str] = Field(default_factory=list, max_length=10)
    term_type: Literal["concept", "proper_name", "acronym", "title", "unit", "other"] = "concept"
    note: str | None = Field(default=None, max_length=300)


class BlockFlagIn(BaseModel):
    reason: Literal["sai", "thieu", "kho_doc", "khac"] = "khac"
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackIn(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=10)
    comment: str | None = Field(default=None, max_length=2000)


class TakedownIn(BaseModel):
    reporter_name: str | None = Field(default=None, max_length=120)
    reporter_email: EmailStr
    report_id: str | None = None
    description: str = Field(min_length=10, max_length=4000)
