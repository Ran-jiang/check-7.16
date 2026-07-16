"""把领域核查结果转换为摘要、前端数据和交付报告。"""

from .report import render_report_html
from .summary import VerificationSummary, summarize_verification
from .verification import LegalCheckData, build_verification_document

__all__ = [
    "LegalCheckData",
    "VerificationSummary",
    "build_verification_document",
    "render_report_html",
    "summarize_verification",
]
