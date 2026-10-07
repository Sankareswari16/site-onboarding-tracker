"""Request validation: bad input is rejected at the door with a clear message."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import REGIONS

_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class SiteCreate(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=100,
                                 description="Client-generated unique key; replays return the original record")
    site_name: str = Field(..., min_length=2, max_length=200)
    investigator_name: str = Field(..., min_length=2, max_length=200)
    region: str
    country: str = Field(..., min_length=2, max_length=2, description="ISO 3166-1 alpha-2")
    contact_email: str = Field(..., pattern=_EMAIL)

    @field_validator("region")
    @classmethod
    def region_known(cls, v: str) -> str:
        v = v.upper()
        if v not in REGIONS:
            raise ValueError(f"region must be one of {REGIONS}")
        return v

    @field_validator("site_name", "investigator_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: Optional[str] = Field(None, max_length=2000)
    expected_version: Optional[int] = Field(None, ge=1)

    @model_validator(mode="after")
    def reject_needs_reason(self) -> "DecisionIn":
        if self.decision == "reject" and not (self.comment and len(self.comment.strip()) >= 5):
            raise ValueError("a rejection requires a comment of at least 5 characters")
        return self
