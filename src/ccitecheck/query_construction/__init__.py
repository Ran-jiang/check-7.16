"""查询假设构造层。"""

from .service import (
    QueryResources,
    build_initial_hypothesis,
    rebuild_hypothesis,
    resolve_identity_candidates,
)

__all__ = [
    "QueryResources",
    "build_initial_hypothesis",
    "rebuild_hypothesis",
    "resolve_identity_candidates",
]
