"""面向 API 与前端的法规、案例核验文档。"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .case_results import CaseVerificationResult
from .statute_results import StatuteVerificationResult


class FrontendVerificationDocument(BaseModel):
    schema_version: str = "0.5"
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_claim_doc_id: str
    statute_results: list[StatuteVerificationResult] = Field(default_factory=list)
    case_results: list[CaseVerificationResult] = Field(default_factory=list)


__all__ = ["FrontendVerificationDocument"]
