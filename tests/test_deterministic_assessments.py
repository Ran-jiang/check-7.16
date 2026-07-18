from ccitecheck.domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ccitecheck.domain.statute_results import StatuteErrorCode, StatuteVersion
from ccitecheck.judgment.statutes import assess_statute
from ccitecheck.tracing.sources import LookupResult


def test_source_not_found_requires_completed_pkulaw_search():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.SOURCE_ERROR,
    )
    result = LookupResult(LookupStatus.LAW_NOT_FOUND, None, trace)

    assert assess_statute("某法", "第一条", result, [trace], []) == []


def test_source_not_found_uses_only_completed_pkulaw_search():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.LAW_NOT_FOUND,
        metadata={"search_completed": True},
    )
    result = LookupResult(LookupStatus.LAW_NOT_FOUND, None, trace)

    findings = assess_statute("某法", "第一条", result, [trace], [])

    assert findings[0].code == StatuteErrorCode.SOURCE_NOT_FOUND


def test_repealed_source_suppresses_location_error():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
    )
    evidence = ArticleEvidence(
        law_title="旧法",
        source_type="law",
        version_status="废止",
        data_source=trace,
    )
    result = LookupResult(LookupStatus.LAW_FOUND_ARTICLE_MISSING, evidence, trace)

    findings = assess_statute("旧法", "第三条", result, [trace], [])

    assert [finding.code for finding in findings] == [StatuteErrorCode.SOURCE_REPEALED]


def test_historical_article_turns_missing_location_into_amended_source():
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="本地库",
        status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
        metadata={"local_article_count": 10},
    )
    result = LookupResult(LookupStatus.LAW_FOUND_ARTICLE_MISSING, None, trace)
    historical = StatuteVersion(
        version_key="2018",
        effective_to="2020-01-01",
        article_no="第十二条",
        article_text="历史版本条文。",
    )

    findings = assess_statute(
        "示例法", "第十二条", result, [trace], [], [historical]
    )

    assert findings[0].code == StatuteErrorCode.SOURCE_AMENDED
    assert findings[0].historical_version == historical
