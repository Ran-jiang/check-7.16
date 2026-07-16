"""HTTP API 中 Word 与飞书共用的请求和响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ccitecheck.domain.result import FrontendVerificationDocument
from ccitecheck.parsing.feishu import FeishuDocumentSnapshot
from ccitecheck.output.summary import VerificationSummary


class DocumentCheckRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    docx_base64: str = Field(min_length=1)
    semantic_check: bool = True
    include_statutes: bool = True
    include_cases: bool = True


class SelectionSourceBlock(BaseModel):
    """选区中一行文本在当前 Word 文档里的真实起点。"""

    block_id: str = Field(min_length=1)
    char_start: int = Field(ge=0)


class SelectionCheckRequest(BaseModel):
    """核查用户在 Word 中选中的文本片段。"""

    file_name: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=200_000)
    semantic_check: bool = True
    include_statutes: bool = True
    include_cases: bool = True
    source_blocks: list[SelectionSourceBlock] = Field(default_factory=list)


class FeishuDocumentCheckRequest(BaseModel):
    """飞书文档块快照核查请求。"""

    snapshot: FeishuDocumentSnapshot
    semantic_check: bool = True
    include_statutes: bool = True
    include_cases: bool = True


CheckSummary = VerificationSummary


class DocumentCheckResponse(BaseModel):
    file_name: str
    document_key: str
    semantic_check: bool
    summary: CheckSummary
    verification: FrontendVerificationDocument


class ReportRequest(BaseModel):
    """由前端回传核查结果与用户标记，生成可交付的核查报告。"""

    file_name: str = Field(min_length=1, max_length=255)
    semantic_check: bool = True
    summary: CheckSummary
    verification: FrontendVerificationDocument
    # 核查编号对应人工处理状态：接受或忽略。
    decisions: dict[str, str] = Field(default_factory=dict)

    @field_validator("decisions")
    @classmethod
    def validate_decisions(cls, decisions: dict[str, str]) -> dict[str, str]:
        invalid = sorted(set(decisions.values()) - {"accepted", "ignored"})
        if invalid:
            raise ValueError(f"不支持的人工处理状态：{', '.join(invalid)}")
        return decisions


class ReportResponse(BaseModel):
    report_id: str
    url: str
