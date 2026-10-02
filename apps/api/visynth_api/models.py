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
    """Thân `POST /jobs` — theo hợp đồng, kèm vài trường nội bộ của bản tham chiếu."""

    document_id: str
    level: Level
    recipe_id: str | None = None
    glossary_ids: list[str] = Field(default_factory=list, max_length=5)
    custom_instructions: str = Field(default="", max_length=1000)
    notify_by_email: bool = False
    skip_glossary_review: bool = False
    style_core_id: str | None = None
    target_lang: str = Field(default="vi", max_length=10)
    privacy_class: Literal["standard", "private"] = "standard"
    style_core_version_id: str | None = None
    max_cost_usd: float | None = Field(default=None, gt=0)
    options: dict[str, Any] = Field(default_factory=dict)


class JobGlossaryItem(BaseModel):
    """Một mục trong `POST /jobs/{id}/glossary/confirm` (khớp `JobGlossaryEntry`)."""

    source_term: str = Field(min_length=1, max_length=200)
    target_term: str = Field(min_length=1, max_length=200)
    keep_original: bool = False
    case_sensitive: bool = False
    forbidden_variants: list[str] = Field(default_factory=list, max_length=20)
    term_type: Literal["concept", "proper_name", "acronym", "title", "unit", "other"] = "concept"
    note: str | None = Field(default=None, max_length=500)
    status: Literal["suggested", "confirmed", "rejected"] = "confirmed"
    origin: Literal["shared", "personal", "suggested", "user_edit"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class JobGlossaryConfirm(BaseModel):
    entries: list[JobGlossaryItem] = Field(default_factory=list, max_length=400)
    save_to_glossary_id: str | None = None


class InviteCreate(BaseModel):
    """Khớp `POST /admin/invites` của hợp đồng: `count` mã, hạn dùng tuỳ chọn."""

    count: int = Field(default=1, ge=1, le=200)
    credits_grant: int = Field(gt=0, le=10000)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_at: datetime | None = None
    note: str | None = Field(default=None, max_length=200)


class PoolCredentialCreate(BaseModel):
    """`POST /admin/pool/groups/{id}/credentials` — `secret` chỉ ghi, không bao giờ trả lại."""

    label: str = Field(min_length=1, max_length=120)
    secret: str = Field(min_length=16, max_length=400)


class PoolCredentialPatch(BaseModel):
    status: Literal["active", "disabled"]


class PoolGroupPatch(BaseModel):
    enabled: bool | None = None
    data_policy: Literal["no_training", "may_train", "unknown"] | None = None
    tos_flags: list[Literal["trial_only", "no_personal_data", "multi_account_risk", "no_eea_uk_ch"]] | None = None
    allowed_gates: list[Literal["dev", "A", "B", "C"]] | None = Field(default=None, min_length=1)
    safety_margin: float | None = Field(default=None, ge=0.3, le=1)
    day_margin: float | None = Field(default=None, ge=0.5, le=1)
    limits: dict[str, Any] | None = None
    risk_ack: list[Literal["multi_account_risk", "trial_only"]] | None = None


class PoolDeploymentPatch(BaseModel):
    enabled: bool | None = None
    weight: float | None = Field(default=None, gt=0)
    tags: list[str] | None = None
    limits: dict[str, Any] | None = None


class UserPatch(BaseModel):
    role: Literal["user", "curator", "admin"] | None = None
    status: Literal["pending", "active", "suspended"] | None = None


class StyleCoreCreateIn(BaseModel):
    """`POST /admin/style-cores` — tạo lõi + phiên bản `0.1.0`. Khớp `StyleCoreCreate` trong hợp đồng."""

    slug: str = Field(pattern=r"^[a-z0-9-]{3,60}$")
    name: str = Field(min_length=1, max_length=120)
    domain: str | None = Field(default=None, max_length=60)
    parent_id: str | None = None
    locale: str = Field(default="vi", max_length=10)
    content: dict[str, Any] = Field(default_factory=dict)
    origin: Literal["human", "ai_proposal", "import"] = "human"


class StyleCoreVersionCreateIn(BaseModel):
    base_version_id: str
    bump: Literal["patch", "minor", "major"] = "minor"
    origin: Literal["human", "ai_proposal", "import"] = "human"


class StyleCoreEditIn(BaseModel):
    content: dict[str, Any]
    comment: str | None = Field(default=None, max_length=1000)


class DecisionAnswerIn(BaseModel):
    decision_id: str = Field(pattern=r"^D[0-9]{2}$")
    answer: str = Field(min_length=1, max_length=200)


class StyleProposeIn(BaseModel):
    mode: Literal["bootstrap", "refine"]
    brief_vi: str = Field(min_length=1, max_length=3000)
    sample_document_ids: list[str] = Field(default_factory=list, max_length=10)
    reference_pairs: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    base_version_id: str | None = None


class TestDriveIn(BaseModel):
    paragraphs: list[dict[str, Any]] = Field(min_length=1, max_length=20)
    compare_to_version_id: str | None = None


class GlossaryDecisionIn(BaseModel):
    action: Literal["approve", "reject", "edit_approve"]
    target_term: str | None = Field(default=None, max_length=160)
    keep_original: bool | None = None
    forbidden_variants: list[str] = Field(default_factory=list, max_length=10)
    note: str | None = Field(default=None, max_length=300)


class GlossaryReleaseIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class GlossaryBootstrapIn(BaseModel):
    glossary_id: str
    sample_document_ids: list[str] = Field(min_length=1, max_length=10)
    reference_pairs: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    style_core_id: str | None = None


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
