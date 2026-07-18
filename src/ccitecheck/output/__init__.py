"""把领域核查结果转换为摘要和前端数据。"""

from .summary import VerificationSummary, summarize_verification

__all__ = [
    "VerificationSummary",
    "summarize_verification",
]
