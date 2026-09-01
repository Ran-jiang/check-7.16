"""可追溯检索假设模型。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class JurisdictionCandidate(BaseModel):
    code: str
    score: float | None = Field(default=None, ge=0, le=1)
    basis: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class IdentityCandidate(BaseModel):
    title: str
    basis: str
    priority: int = Field(ge=1)
    source_url: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


class VersionHypothesis(BaseModel):
    year: int | None = None
    kind: Literal["amended", "revised", "historical", "current", "unknown"] = "unknown"
    temporal_reference: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


class NormalizedLocator(BaseModel):
    article_number: int | None = None
    article_raw: str | None = None
    paragraph_number: int | None = None
    paragraph_raw: str | None = None
    item_number: int | None = None
    item_raw: str | None = None
    structure_labels: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourcePlanItem(BaseModel):
    source_id: str
    priority: int = Field(ge=1)
    reason: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderStrategy(BaseModel):
    source_id: str
    goal: str
    preferred_mode: str
    allowed_fallbacks: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class HypothesisAssumption(BaseModel):
    code: str
    value: Any = None
    basis: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchHypothesis(BaseModel):
    hypothesis_id: str = Field(default_factory=lambda: str(uuid4()))
    claim_id: str
    mention_id: str | None = None
    parent_hypothesis_id: str | None = None
    attempt_no: int = Field(default=1, ge=1)
    jurisdiction: JurisdictionCandidate
    identity_candidates: list[IdentityCandidate] = Field(default_factory=list)
    version_hypothesis: VersionHypothesis | None = None
    normalized_locator: NormalizedLocator | None = None
    source_plan: list[SourcePlanItem] = Field(default_factory=list)
    provider_strategies: list[ProviderStrategy] = Field(default_factory=list)
    assumptions: list[HypothesisAssumption] = Field(default_factory=list)
    trigger_reason: str = "initial"
    model_config = ConfigDict(extra="forbid", frozen=True)


class HypothesisFeedback(BaseModel):
    trigger_reason: str
    identity_title: str | None = None
    article_raw: str | None = None
    version_year: int | None = None
    basis: str
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = [
    "HypothesisAssumption",
    "HypothesisFeedback",
    "IdentityCandidate",
    "JurisdictionCandidate",
    "NormalizedLocator",
    "ProviderStrategy",
    "SearchHypothesis",
    "SourcePlanItem",
    "VersionHypothesis",
]
