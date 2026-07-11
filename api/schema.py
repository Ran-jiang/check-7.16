"""Request and response models for the Word add-in API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from verification.schema import FrontendVerificationDocument


class DocumentCheckRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    docx_base64: str = Field(min_length=1)
    semantic_check: bool = True


class CheckSummary(BaseModel):
    total: int
    passed: int
    issues: int
    bugs: int
    exact_matches: int
    cases_verified: int
    cases_not_found: int


class DocumentCheckResponse(BaseModel):
    file_name: str
    semantic_check: bool
    summary: CheckSummary
    verification: FrontendVerificationDocument
