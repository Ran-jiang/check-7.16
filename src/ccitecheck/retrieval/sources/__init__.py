"""权威法律信息数据源适配器的公共入口。"""

from .base import LookupRequest, LookupResult, StatuteSource
from .eurlex import EurLexSource
from .local_laws import LocalSQLiteSource
from .pkulaw.cases import CaseSearcher, PkulawCaseSource
from .pkulaw.statutes import PkulawFallbackSource

__all__ = [
    "CaseSearcher",
    "EurLexSource",
    "LocalSQLiteSource",
    "LookupRequest",
    "LookupResult",
    "PkulawCaseSource",
    "PkulawFallbackSource",
    "StatuteSource",
]
