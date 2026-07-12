"""Request and response models for the Word add-in API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from verification.schema import FrontendVerificationDocument


class DocumentCheckRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    docx_base64: str = Field(min_length=1)
    semantic_check: bool = True


class SelectionCheckRequest(BaseModel):
    """核查用户在 Word 中选中的文本片段。"""

    file_name: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=200_000)
    semantic_check: bool = True


class CheckSummary(BaseModel):
    total: int
    passed: int
    issues: int
    bugs: int
    cases_verified: int
    cases_not_found: int


class DocumentCheckResponse(BaseModel):
    file_name: str
    semantic_check: bool
    summary: CheckSummary
    verification: FrontendVerificationDocument


class ReportRequest(BaseModel):
    """由前端回传核查结果与用户标记，生成可交付的核查报告。"""

    file_name: str = Field(min_length=1, max_length=255)
    semantic_check: bool = True
    summary: CheckSummary
    verification: FrontendVerificationDocument
    # check_id → accepted / ignored / escalated
    decisions: dict[str, str] = Field(default_factory=dict)


class ReportResponse(BaseModel):
    report_id: str
    url: str
