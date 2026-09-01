"""从 RawClaim 构造可追溯的 SearchHypothesis。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..domain.claims import RawClaim, RawLegalMention
from ..domain.queries import (
    HypothesisAssumption,
    HypothesisFeedback,
    IdentityCandidate,
    JurisdictionCandidate,
    SearchHypothesis,
    SourcePlanItem,
    VersionHypothesis,
)
from ..infrastructure.database import normalize_title
from .aliases import alias_map
from .jurisdiction import detect_jurisdiction
from .locator import normalize_locator
from .name_resolution import LawLexicon
from .normalization import normalize_identity_title
from .source_planner import plan_sources
from .strategies.cases import case_strategies
from .strategies.statutes import statute_strategies
from .versioning import build_version_hypothesis


@dataclass(frozen=True)
class QueryResources:
    law_db: str | Path | None = None
    lexicon: LawLexicon | None = None

    def law_lexicon(self) -> LawLexicon:
        return self.lexicon or LawLexicon.load(self.law_db)


def _mention(claim: RawClaim, mention_id: str | None) -> RawLegalMention | None:
    if mention_id is None:
        return claim.legal_mentions[0] if claim.legal_mentions else None
    return next((item for item in claim.legal_mentions if item.mention_id == mention_id), None)


def _identities(
    claim: RawClaim,
    mention: RawLegalMention | None,
    resources: QueryResources,
) -> list[IdentityCandidate]:
    if mention is None:
        case = claim.case_mentions[0] if claim.case_mentions else None
        raw = (case.raw_case_number or case.raw_case_name) if case else None
        return [IdentityCandidate(title=raw, basis="raw", priority=1)] if raw else []

    raw = (mention.raw_title or mention.raw_title_candidate or "").strip()
    if not raw:
        return []
    candidates = [IdentityCandidate(title=raw, basis="raw", priority=1)]
    aliases = alias_map(claim.alias_declarations)
    alias_title = aliases.get(normalize_title(raw))
    lexicon = resources.law_lexicon()
    resolved = alias_title or lexicon.canonical_title_for(raw)
    basis = "document_alias" if alias_title else "lexicon"
    if resolved is None and mention.raw_title_candidate:
        match = lexicon.longest_suffix_match(mention.raw_title_candidate)
        resolved = match.canonical_title if match else None
    if resolved and normalize_title(resolved) != normalize_title(raw):
        candidates.append(IdentityCandidate(
            title=resolved,
            basis=basis,
            priority=2,
        ))
    return candidates


def resolve_identity_candidates(
    raw_title: str,
    resources: QueryResources,
    *,
    declarations=(),
    raw_title_candidate: str | None = None,
) -> list[IdentityCandidate]:
    """供批量 Scheduler 适配器复用同一法名解释顺序。"""
    raw = (raw_title or raw_title_candidate or "").strip()
    if not raw:
        return []
    result = [IdentityCandidate(title=raw, basis="raw", priority=1)]
    aliases = alias_map(list(declarations))
    alias_title = aliases.get(normalize_title(raw))
    lexicon = resources.law_lexicon()
    resolved = alias_title or lexicon.canonical_title_for(raw)
    basis = "document_alias" if alias_title else "lexicon"
    if resolved is None and raw_title_candidate:
        matched = lexicon.longest_suffix_match(raw_title_candidate)
        if matched:
            resolved = matched.canonical_title
    if resolved and normalize_title(resolved) != normalize_title(raw):
        result.append(IdentityCandidate(title=resolved, basis=basis, priority=2))
    return result


def build_initial_hypothesis(
    claim: RawClaim,
    resources: QueryResources,
    *,
    mention_id: str | None = None,
) -> SearchHypothesis:
    mention = _mention(claim, mention_id)
    identities = _identities(claim, mention, resources)
    title = identities[0].title if identities else ""
    jurisdiction_code = detect_jurisdiction(title, claim.context_text or claim.raw_text)
    jurisdiction = JurisdictionCandidate(
        code=jurisdiction_code,
        basis=["title_or_adjacent_text"],
    )
    locator = (
        normalize_locator(
            mention.article_raw,
            mention.paragraphs_raw,
            mention.items_raw,
            mention.structures_raw,
        )
        if mention else None
    )
    if claim.case_mentions:
        source_plan = [] if jurisdiction_code == "FOREIGN" else [SourcePlanItem(
            source_id="pkulaw_cases",
            priority=1,
            reason="cn_case_authoritative_source",
        )]
        strategies = case_strategies(source_plan)
    else:
        source_plan = plan_sources(jurisdiction_code)
        strategies = statute_strategies(source_plan, locator)
    return SearchHypothesis(
        claim_id=claim.claim_id,
        mention_id=mention.mention_id if mention else None,
        jurisdiction=jurisdiction,
        identity_candidates=identities,
        version_hypothesis=(
            build_version_hypothesis(title)
            if title and not claim.case_mentions else None
        ),
        normalized_locator=locator,
        source_plan=source_plan,
        provider_strategies=strategies,
        assumptions=[HypothesisAssumption(
            code="identity_order",
            value=[item.title for item in identities],
            basis="raw_then_document_alias_then_lexicon",
        )],
        trigger_reason="initial",
    )


def rebuild_hypothesis(
    claim: RawClaim,
    previous: SearchHypothesis,
    feedback: HypothesisFeedback,
    resources: QueryResources,
) -> SearchHypothesis:
    identities = list(previous.identity_candidates)
    if feedback.identity_title:
        title = normalize_identity_title(feedback.identity_title)
        identities = [
            IdentityCandidate(title=title, basis=feedback.basis, priority=1),
            *[
                item.model_copy(update={"priority": index + 2})
                for index, item in enumerate(identities)
                if normalize_title(item.title) != normalize_title(title)
            ],
        ]
    locator = previous.normalized_locator
    if feedback.article_raw:
        locator = normalize_locator(
            feedback.article_raw,
            [locator.paragraph_raw] if locator and locator.paragraph_raw else [],
            [locator.item_raw] if locator and locator.item_raw else [],
            locator.structure_labels if locator else [],
        )
    source_plan = plan_sources(previous.jurisdiction.code)
    return SearchHypothesis(
        claim_id=claim.claim_id,
        mention_id=previous.mention_id,
        parent_hypothesis_id=previous.hypothesis_id,
        attempt_no=previous.attempt_no + 1,
        jurisdiction=previous.jurisdiction,
        identity_candidates=identities,
        version_hypothesis=(
            previous.version_hypothesis.model_copy(update={"year": feedback.version_year})
            if feedback.version_year and previous.version_hypothesis
            else (
                VersionHypothesis(
                    year=feedback.version_year,
                    temporal_reference=feedback.basis,
                )
                if feedback.version_year else previous.version_hypothesis
            )
        ),
        normalized_locator=locator,
        source_plan=source_plan,
        provider_strategies=statute_strategies(source_plan, locator),
        assumptions=[*previous.assumptions, HypothesisAssumption(
            code=feedback.trigger_reason,
            value=feedback.model_dump(exclude_none=True),
            basis=feedback.basis,
        )],
        trigger_reason=feedback.trigger_reason,
    )


__all__ = [
    "QueryResources",
    "build_initial_hypothesis",
    "rebuild_hypothesis",
    "resolve_identity_candidates",
]
