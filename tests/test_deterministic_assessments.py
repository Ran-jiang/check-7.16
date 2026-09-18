from types import SimpleNamespace

from ccitecheck.domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ccitecheck.domain.statute_results import StatuteErrorCode, StatuteFinding, StatuteVersion
from ccitecheck.orchestration.scheduler import _resolve_repealed_successors
from ccitecheck.verification.statutes import assess_statute
from ccitecheck.retrieval.sources import LookupResult
from ccitecheck.retrieval.sources.base import LocationCandidateResult


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


def test_confirmed_correct_title_is_law_name_error():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.LAW_NOT_FOUND,
        metadata={"search_completed": True, "suggested_title": "正确法名"},
    )
    evidence = ArticleEvidence(
        law_title="正确法名",
        source_type="law",
        article_no="第一条",
        article_text="权威条文。",
        data_source=trace,
    )
    result = LookupResult(LookupStatus.LAW_NOT_FOUND, evidence, trace)

    findings = assess_statute("错别法名", "第一条", result, [trace], [])

    assert findings[0].code == StatuteErrorCode.LAW_NAME_ERROR
    assert findings[0].suggestion == "法律名称引用错误，应为《正确法名》。"


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


def test_repealed_source_uses_returned_repeal_decision_metadata():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE,
        metadata={
            "candidates": [{
                "title": "全国人民代表大会常务委员会关于废止《中华人民共和国农业税条例》的决定",
                "issue_date": "2005.12.29",
                "implement_date": "2006.01.01",
            }]
        },
    )
    evidence = ArticleEvidence(
        law_title="中华人民共和国农业税条例",
        version_status="废止或失效",
        data_source=trace,
    )

    finding = assess_statute(
        "中华人民共和国农业税条例", None,
        LookupResult(trace.status, evidence, trace), [trace], []
    )[0]

    assert "2005.12.29" in finding.suggestion
    assert "自2006.01.01起废止" in finding.suggestion


def test_timeliness_distinguishes_amended_partial_and_fully_repealed():
    def finding(status):
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="北大法宝",
            status=LookupStatus.ARTICLE_FOUND,
        )
        evidence = ArticleEvidence(
            law_title="示例法（2017修正）",
            article_no="第四十条",
            article_text="条文。",
            version_status=status,
            data_source=trace,
        )
        return assess_statute(
            "示例法", "第四十条",
            LookupResult(trace.status, evidence, trace), [trace], []
        )[0]

    assert finding("已被修改").code == StatuteErrorCode.SOURCE_AMENDED
    assert finding("部分失效").code == StatuteErrorCode.SOURCE_AMENDED
    assert finding("废止或失效").code == StatuteErrorCode.SOURCE_REPEALED


def test_implementation_date_and_future_status_are_checked():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.ARTICLE_FOUND,
    )
    evidence = ArticleEvidence(
        law_title="示例法",
        article_no="第一条",
        article_text="条文。",
        version_status="现行有效",
        effective_from="2026.07.01",
        data_source=trace,
    )
    wrong_date = assess_statute(
        "示例法", "第一条", LookupResult(trace.status, evidence, trace), [trace], [],
        claim_text="《示例法》自2026年1月1日起施行。",
    )[0]
    assert wrong_date.suggestion == "施行日期错误，应为2026.07.01。"

    future = evidence.model_copy(update={"version_status": "尚未生效"})
    future_finding = assess_statute(
        "示例法", "第一条", LookupResult(trace.status, future, trace), [trace], [],
        claim_text="当前应当依据《示例法》第一条处理。",
    )[0]
    assert "尚未生效" in future_finding.summary


def test_unrelated_current_candidates_are_not_called_successors():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.RELEVANT_ARTICLES_FOUND,
    )
    candidate = ArticleEvidence(
        law_title="某现行相关法",
        article_no="第一条",
        article_text="与农业税追缴规则无关的条文。",
        data_source=trace,
    )

    class NoiseSource:
        def locate_successor_candidates(self, request):
            return LocationCandidateResult([candidate], trace)

    finding = StatuteFinding(
        code=StatuteErrorCode.SOURCE_REPEALED,
        risk_level="HIGH",
        summary="《农业税条例》已废止",
        suggestion="《农业税条例》已废止或失效，请核对废止依据和现行规定。",
    )
    item = SimpleNamespace(
        jurisdiction="CN",
        law_title="农业税条例",
        claim=SimpleNamespace(text="依据《农业税条例》追缴农业税及滞纳金。"),
    )

    _resolve_repealed_successors([NoiseSource()], [item], {0: [finding]})

    assert "未检索到可确认的现行继受法" in finding.suggestion


def test_unique_successor_includes_repeal_basis_and_current_rule():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.RELEVANT_ARTICLES_FOUND,
    )
    successor = ArticleEvidence(
        law_title="中华人民共和国民法典",
        article_no="第五百六十三条",
        article_text="当事人一方迟延履行主要债务，经催告后在合理期限内仍未履行，当事人可以解除合同。",
        effective_from="2021-01-01",
        data_source=trace,
    )
    basis = ArticleEvidence(
        law_title="中华人民共和国民法典",
        article_no="第一千二百六十条",
        article_text="《中华人民共和国合同法》同时废止。",
        data_source=trace,
    )

    class SuccessorSource:
        def locate_successor_candidates(self, request):
            return LocationCandidateResult([successor], trace)

        def locate_repeal_basis(self, request, successor_title):
            return LocationCandidateResult([basis], trace)

    text = (
        "当事人一方迟延履行主要债务，经催告后在合理期限内仍未履行的，"
        "对方可以依据《中华人民共和国合同法》第九十四条解除合同。"
    )
    finding = StatuteFinding(
        code=StatuteErrorCode.SOURCE_REPEALED,
        risk_level="HIGH",
        summary="《中华人民共和国合同法》已废止",
        suggestion="《中华人民共和国合同法》已废止或失效。",
    )
    item = SimpleNamespace(
        jurisdiction="CN",
        law_title="中华人民共和国合同法",
        article_no="第九十四条",
        article=None,
        claim=SimpleNamespace(text=text),
    )

    _resolve_repealed_successors([SuccessorSource()], [item], {0: [finding]})

    assert "2021-01-01" in finding.suggestion
    assert "第一千二百六十条" in finding.suggestion
    assert "第五百六十三条" in finding.suggestion


def test_historical_article_turns_missing_location_into_amended_source():
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="本地库",
        status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
        metadata={"local_article_count": 10, "full_text_complete": True, "version_confirmed": True,
                  "completeness_source": "https://example.test/full", "article_keys": [str(i) for i in range(1, 11)]},
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
