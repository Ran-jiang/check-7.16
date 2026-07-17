from ccitecheck.domain.citation import (
    ArticleRef,
    Claim,
    ClaimType,
    LegalSource,
    LegalSourceClaimEntities,
    LegalSourceType,
)
from ccitecheck.domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ccitecheck.domain.result import (
    ComparisonVerdict,
    SemanticCheckResult,
    SemanticExecutionStatus,
)
from ccitecheck.judgment.semantic import _source_metadata
from ccitecheck.judgment.service import decide_semantic_gate
from ccitecheck.recognition.spans import locate_claim_article_spans
from ccitecheck.tracing.sources import LookupResult


def _source(title, *articles):
    return LegalSource(
        title=title,
        source_type=LegalSourceType.LAW,
        articles=[ArticleRef(article=value) for value in articles],
    )


def test_span_locator_rejects_wrong_owner_and_marks_nested_reference():
    text = (
        "《解释》第九条规定，该情形属于商标法第十三条第二款规定的容易导致混淆。"
        "第十条规定，应当综合判断。"
    )
    explanation = _source("解释", "第九条", "第十三条", "第十条")
    trademark = _source("商标法", "第十三条")
    trademark.articles[0].paragraphs = ["第二款"]
    claim = Claim(
        claim_id="cl_1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[explanation, trademark]),
    )

    locate_claim_article_spans(claim)

    assert explanation.articles[0].span_status == "located"
    assert explanation.articles[1].span_status == "error"
    assert explanation.articles[2].span_status == "located"
    assert trademark.articles[0].reference_role == "nested"
    assert trademark.articles[0].parent_reference_id == ("解释", "第九条")
    assert trademark.articles[0].quote_span is None
    assert text[slice(*trademark.articles[0].citation_span)] == "商标法第十三条第二款"


def test_unique_article_assignment_allows_cross_sentence_continuation():
    trademark = _source("商标法", "第十三条")
    explanation = _source("解释", "第九条", "第十条")
    claim = Claim(
        claim_id="cl_1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="依照《商标法》第十三条的规定处理。《解释》第九条规定，构成混淆。第十条规定，应当综合判断。",
        anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[trademark, explanation]),
    )
    locate_claim_article_spans(claim)
    assert explanation.articles[1].span_status == "located"


def test_empty_quote_span_is_valid_fallback():
    source = _source("民法典", "第一条", "第二条")
    claim = Claim(
        claim_id="cl_1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="依照《民法典》第一条、第二条之规定，判决如下。",
        anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[source]),
    )
    locate_claim_article_spans(claim)
    assert source.articles[0].quote_span is None
    assert source.articles[0].span_status == "located"
    assert source.articles[1].quote_span is None


def test_enumerated_and_ranged_articles_share_deterministic_mention_span():
    source = _source("民法典", "第九条", "第十条", "第十一条")
    claim = Claim(
        claim_id="cl_1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="根据《民法典》第九条至第十一条，应当依法处理。",
        anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[source]),
    )
    locate_claim_article_spans(claim)
    assert {article.span_status for article in source.articles} == {"located"}
    assert len({article.mention_span for article in source.articles}) == 1
    assert len({article.quote_span for article in source.articles}) == 1


def test_semantic_result_derives_retryable_from_error_code():
    result = SemanticCheckResult(
        execution_status=SemanticExecutionStatus.LLM_ERROR,
        error_code="transport_error",
        notes="TLS failed",
    )
    assert result.verdict is None
    assert result.retryable is True
    passed = SemanticCheckResult(verdict=ComparisonVerdict.PASS, notes="")
    assert passed.execution_status == SemanticExecutionStatus.COMPLETED


def test_source_metadata_does_not_leak_timeliness():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="pkulaw",
        status=LookupStatus.ARTICLE_FOUND,
    )
    evidence = ArticleEvidence(
        law_title="某法（2020修正）",
        source_type="law",
        article_no="第一条",
        article_text="正文",
        version_label="已被修改",
        version_status="已被修改",
        source_metadata={"timeliness": ["已被修改"]},
        data_source=trace,
    )
    metadata = _source_metadata(evidence)
    assert "version_status" not in metadata
    assert "timeliness" not in metadata
    assert "已被修改" not in str(metadata)


def test_gate_does_not_block_repealed_warning_when_text_exists():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="pkulaw",
        status=LookupStatus.ARTICLE_FOUND,
    )
    evidence = ArticleEvidence(
        law_title="某法",
        source_type="law",
        article_no="第一条",
        article_text="正文",
        data_source=trace,
    )
    gate = decide_semantic_gate(LookupResult(LookupStatus.ARTICLE_FOUND, evidence, trace), [])
    assert gate.proceed is True


def test_nested_reference_requires_successful_lookup():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="pkulaw",
        status=LookupStatus.SOURCE_ERROR,
    )
    gate = decide_semantic_gate(
        LookupResult(LookupStatus.SOURCE_ERROR, None, trace),
        [],
        reference_role="nested",
    )
    assert gate.reason == "retrieval_incomplete"
