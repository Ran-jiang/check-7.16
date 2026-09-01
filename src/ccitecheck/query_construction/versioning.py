"""法规版本查询假设。"""

from ..domain.queries import VersionHypothesis
from .normalization import split_version_annotation


def build_version_hypothesis(title: str) -> VersionHypothesis | None:
    _, year, kind = split_version_annotation(title)
    if year is None:
        return None
    return VersionHypothesis(year=year, kind=kind or "unknown", temporal_reference=title)


__all__ = ["build_version_hypothesis"]
