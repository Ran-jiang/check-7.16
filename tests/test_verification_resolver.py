from pathlib import Path

from ccitecheck.domain.citation import (
    ArticleRef,
    CaseCitationEntities,
    CaseRef,
    CaseReferenceType,
    Claim,
    ClaimDocument,
    ClaimMeta,
    ClaimType,
    LegalSource,
    LegalSourceClaimEntities,
    LegalSourceType,
)
from ccitecheck.infrastructure.database import (
    connect,
    init_db,
    upsert_article,
    upsert_law,
)
from ccitecheck.application.verify_claims import verify_claim_document
from ccitecheck.judgment.semantic import SemanticTransportError
from ccitecheck.output.summary import summarize_verification
from ccitecheck.domain.result import (
    ArticleEvidence,
    CaseLookupStatus,
    CaseCheck,
    ComparisonVerdict,
    LookupStatus,
    RiskLevel,
    SemanticComparison,
    SemanticErrorType,
    SemanticIssue,
    SourceTier,
    SourceTrace,
)
from ccitecheck.tracing.sources.pkulaw.client import (
    PkulawArticle,
    PkulawCaseNumber,
    PkulawCaseRecord,
    PkulawLawRecord,
    PkulawNotFoundError,
)
from ccitecheck.tracing.sources import (
    LocalSQLiteSource,
    LookupRequest,
    LookupResult,
    PkulawFallbackSource,
)
from ccitecheck.tracing.service import lookup_with_chain


def test_frontend_verification_json_includes_local_article(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn,
            {
                "title": "中华人民共和国劳动合同法",
                "source_type": "law",
                "status": "has_articles",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第三十七条",
                "text": "劳动者提前三十日以书面形式通知用人单位，可以解除劳动合同。",
                "version_label": "现行有效",
                "version_status": "effective",
                "source_name": "国家法律法规数据库",
                "source_url": "https://flk.npc.gov.cn/",
                "source_fetched_at": "2026-07-09T00:00:00+08:00",
            },
        )

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text="依据《劳动合同法》第三十七条，劳动者可以解除劳动合同。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="劳动合同法",
                            source_type=LegalSourceType.LAW,
                            articles=[ArticleRef(article="第三十七条")],
                        )
                    ]
                ),
            )
        ],
    )

    frontend_doc = verify_claim_document(claim_doc, db_path)

    assert len(frontend_doc.citation_cards) == 1
    check = frontend_doc.citation_cards[0].references[0]
    assert check.lookup_status == LookupStatus.ARTICLE_FOUND
    assert check.evidence is not None
    assert check.evidence.law_title == "中华人民共和国劳动合同法"
    assert "提前三十日" in check.evidence.article_text
    assert check.source_attempts[0].source_name == "国家法律法规数据库"


class FakeSemanticChecker:
    def compare(self, doc_quote, quote_context, cited_source, evidence):
        return SemanticComparison(
            verdict=ComparisonVerdict.ISSUE,
            issues=[
                SemanticIssue(
                    error_type=SemanticErrorType.MEANING_DISTORTED,
                    risk_level=RiskLevel.MEDIUM,
                    diff_summary="文书未明示被告不履行或履行不符合约定",
                    suggestion="核实并补充违约事实。",
                )
            ],
            notes="",
        )


def test_semantic_assessment_is_added_when_checker_is_configured(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn, {"title": "中华人民共和国民法典", "source_type": "law"}
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第五百七十七条",
                "text": "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担违约责任。",
            },
        )

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text="依据《民法典》第五百七十七条，被告应当承担违约责任。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="民法典",
                            source_type=LegalSourceType.LAW,
                            articles=[ArticleRef(article="第五百七十七条")],
                        )
                    ]
                ),
            )
        ],
    )

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        semantic_checker=FakeSemanticChecker(),
    )

    check = frontend_doc.citation_cards[0].references[0]
    comparison = check.semantic_comparison
    assert comparison.verdict == ComparisonVerdict.ISSUE
    assert comparison.issues[0].error_type == SemanticErrorType.MEANING_DISTORTED


def test_unnumbered_citation_retrieves_related_local_articles(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn,
            {"title": "中华人民共和国网络安全法", "source_type": "law"},
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第一条",
                "text": "为了保障网络安全，维护国家安全和社会公共利益，保护公民、法人和其他组织的合法权益。",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第二条",
                "text": "本法适用于境内网络的建设、运营、维护和使用。",
            },
        )

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text="根据《网络安全法》，保障公民、法人合法权益，维护国家安全和公共利益。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="网络安全法",
                            source_type=LegalSourceType.LAW,
                        )
                    ]
                ),
            )
        ],
    )

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        semantic_checker=FakeSemanticChecker(),
    )

    check = frontend_doc.citation_cards[0].references[0]
    assert check.lookup_status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert check.article_no is None
    assert check.evidence.related_articles[0].article_no == "第一条"
    assert check.semantic_comparison is not None


class FakeLawListClient:
    def get_law_list(self, title="", fulltext=""):
        return [
            PkulawLawRecord(title="中华人民共和国国家安全法", timeliness=["现行有效"])
        ]

    def search_law_articles(self, text):
        return []


class FakeCanonicalTitleRetryClient:
    canonical_title = "最高人民法院、最高人民检察院关于办理危害计算机信息系统安全刑事案件应用法律若干问题的解释"

    def __init__(self):
        self.article_titles = []

    def get_article(self, title, article_no):
        self.article_titles.append(title)
        raise PkulawNotFoundError("未找到数据")

    def search_law_articles_for_article(self, title, article_no):
        return [
            PkulawArticle(
                title=self.canonical_title,
                article_no=article_no,
                article_text="非法获取计算机信息系统数据，具有法定情形的，应当认定为情节严重。",
                url="https://www.pkulaw.com/lar/example.html",
            )
        ]

    def get_law_list(self, title="", fulltext=""):
        return [PkulawLawRecord(title=self.canonical_title)]


def test_pkulaw_article_uses_semantic_fallback_after_exact_miss():
    client = FakeCanonicalTitleRetryClient()
    result = PkulawFallbackSource(client).lookup(
        LookupRequest(
            law_title="关于办理危害计算机信息系统安全刑事案件应用法律若干问题的解释",
            source_type="judicial_interpretation",
            article_no="第一条",
        )
    )

    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.evidence.article_text.startswith("非法获取")
    assert client.article_titles[-1] != client.canonical_title
    assert result.trace.metadata["route_attempts"][1]["service"] == "law_semantic_exact"


def test_pkulaw_unnumbered_lookup_reports_tool_text_limit():
    result = PkulawFallbackSource(FakeLawListClient()).lookup(
        LookupRequest(
            law_title="中华人民共和国国家安全法",
            source_type="law",
            context_text="维护国家安全和公共利益",
        )
    )

    assert result.status == LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
    assert "未取得条文全文" in result.trace.message


class FakeSemanticLawClient(FakeLawListClient):
    def search_law_articles(self, text):
        assert text.startswith("在《中华人民共和国国家安全法》中检索")
        return [
            PkulawArticle(
                title="中华人民共和国国家安全法",
                article_no="第三条",
                article_text="国家安全工作应当坚持总体国家安全观。",
                url="https://example.com/law/3",
            )
        ]


def test_pkulaw_unnumbered_lookup_uses_semantic_article_service_first():
    result = PkulawFallbackSource(FakeSemanticLawClient()).lookup(
        LookupRequest(
            law_title="中华人民共和国国家安全法",
            source_type="law",
            context_text="国家安全工作应当坚持总体国家安全观。",
        )
    )

    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert result.evidence.related_articles[0].article_no == "第三条"
    assert result.trace.metadata["retrieval_method"] == "pkulaw_law_semantic"


def test_pkulaw_unnumbered_lookup_without_credentials_is_nonfatal(monkeypatch):
    monkeypatch.setenv("PKULAW_ACCESS_TOKEN", "")

    result = PkulawFallbackSource().lookup(
        LookupRequest(
            law_title="虚构测试法",
            source_type="law",
            context_text="测试引用表述",
        )
    )

    assert result.status == LookupStatus.SOURCE_NOT_CONFIGURED


class FakeArticleSource:
    def lookup(self, request: LookupRequest) -> LookupResult:
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="fake pkulaw",
            status=LookupStatus.ARTICLE_FOUND,
        )
        return LookupResult(
            LookupStatus.ARTICLE_FOUND,
            ArticleEvidence(
                law_title="中华人民共和国民法典",
                source_type=request.source_type,
                article_no=request.article_no,
                article_text="第五百七十七条　当事人一方不履行合同义务。",
                data_source=trace,
            ),
            trace,
        )


def test_local_catalog_without_article_continues_to_next_source(tmp_path: Path):
    from ccitecheck.infrastructure.database import seed_common_laws

    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    seed_common_laws(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text="依据《民法典》第五百七十七条，被告应当承担违约责任。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="民法典",
                            source_type=LegalSourceType.LAW,
                            articles=[ArticleRef(article="第五百七十七条")],
                        )
                    ]
                ),
            )
        ],
    )

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        sources=[
            LocalSQLiteSource(db_path),
            FakeArticleSource(),
        ],
    )

    check = frontend_doc.citation_cards[0].references[0]
    assert check.lookup_status == LookupStatus.ARTICLE_FOUND
    assert len(check.source_attempts) == 2
    assert "不履行合同义务" in check.evidence.article_text


def test_legacy_local_mcp_url_is_replaced_by_exact_article_url():
    request = LookupRequest(
        law_title="中华人民共和国商标法",
        source_type="law",
        article_no="第十三条",
    )

    class Source:
        def __init__(self, tier, url, text):
            self.tier, self.url, self.text = tier, url, text

        def lookup(self, request):
            trace = SourceTrace(
                tier=self.tier,
                source_name="local" if self.tier == SourceTier.LOCAL_SQLITE else "pkulaw",
                source_url=self.url,
                status=LookupStatus.ARTICLE_FOUND,
            )
            return LookupResult(
                LookupStatus.ARTICLE_FOUND,
                ArticleEvidence(
                    law_title=request.law_title,
                    source_type=request.source_type,
                    article_no=request.article_no,
                    article_text=self.text,
                    data_source=trace,
                ),
                trace,
            )

    result, attempts = lookup_with_chain(
        [
            Source(
                SourceTier.LOCAL_SQLITE,
                "[北大法宝](https://www.pkulaw.com/lar/dead.html?way=mcp)",
                "本地条文",
            ),
            Source(
                SourceTier.PKULAW_FALLBACK,
                "https://pkulaw.com/chl/current.html",
                "精确法条",
            ),
        ],
        request,
    )

    assert len(attempts) == 2
    assert result.evidence.article_text == "精确法条"
    assert result.trace.source_url == "https://pkulaw.com/chl/current.html"


def test_legacy_local_mcp_url_is_hidden_when_remote_repair_fails():
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="local",
        source_url="https://www.pkulaw.com/lar/dead.html?way=mcp",
        status=LookupStatus.ARTICLE_FOUND,
    )
    local = LookupResult(
        LookupStatus.ARTICLE_FOUND,
        ArticleEvidence(
            law_title="中华人民共和国商标法",
            source_type="law",
            article_no="第十三条",
            article_text="仍可用于核查的本地条文",
            data_source=trace,
        ),
        trace,
    )

    class FixedSource:
        def __init__(self, result): self.result = result
        def lookup(self, request): return self.result

    error_trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="pkulaw",
        status=LookupStatus.SOURCE_ERROR,
    )
    result, attempts = lookup_with_chain(
        [FixedSource(local), FixedSource(LookupResult(LookupStatus.SOURCE_ERROR, None, error_trace))],
        LookupRequest(law_title="中华人民共和国商标法", source_type="law", article_no="第十三条"),
    )

    assert len(attempts) == 2
    assert result.evidence.article_text == "仍可用于核查的本地条文"
    assert result.trace.source_url is None


class FakeCaseRecognizer:
    def __init__(self, cases: list[PkulawCaseNumber]):
        self.cases = cases

    def recognize(self, text: str) -> list[PkulawCaseNumber]:
        return self.cases


def _case_claim(claim_id: str, case_number: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=ClaimType.CASE_CITATION,
        text=f"参见{case_number}民事判决。",
        anchor_ids=["line00001"],
        entities=CaseCitationEntities(
            case_refs=[
                CaseRef(
                    reference_type=CaseReferenceType.WITH_CASE_NUMBER,
                    case_number=case_number,
                )
            ]
        ),
    )


def test_case_numbers_verified_and_flagged_against_recognizer(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            _case_claim("cl_00001", "（2024）浙0114破1-6号之二"),
            _case_claim("cl_00002", "（2099）虚构民终9999号"),
        ],
    )
    recognizer = FakeCaseRecognizer(
        [
            PkulawCaseNumber(
                text="（2024）浙0114破1-6号之二",
                start=2,
                end=20,
                gid="08df102e7c10f206",
                case_flag="(2024)浙0114破1-6号之二",
                court="浙江省杭州市钱塘区人民法院",
                title="指导性案例252号：某执行实施案",
                last_instance_date="2024.06.18",
                url="https://www.pkulaw.com/pfnl/08df102e7c10f206.html",
            )
        ]
    )

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        case_recognizer=recognizer,
    )

    assert not frontend_doc.citation_cards
    verified, flagged = frontend_doc.case_checks
    assert verified.lookup_status == CaseLookupStatus.VERIFIED
    assert verified.evidence.court == "浙江省杭州市钱塘区人民法院"
    assert verified.evidence.url.endswith(".html")
    assert flagged.lookup_status == CaseLookupStatus.NOT_FOUND
    assert flagged.evidence is None


class FakeCaseSearcher:
    def __init__(self):
        self.keyword_calls = []
        self.semantic_calls = []

    def recognize(self, text):
        raise AssertionError("no-number case must not use case-number recognition")

    def search_keyword(self, title, fulltext):
        self.keyword_calls.append((title, fulltext))
        return [PkulawCaseRecord(title="不相关案例", url="https://example.com/other")]

    def search_semantic(self, text):
        self.semantic_calls.append(text)
        return [
            PkulawCaseRecord(
                title="指导性案例262号：某平台纠纷案",
                case_number="（2024）最高法民终262号",
                gid="case-262",
                court="最高人民法院",
                url="https://example.com/case/262",
            )
        ]


def test_case_without_number_uses_keyword_then_semantic_search(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.CASE_CITATION,
                text="最高人民法院在指导案例262号中明确了平台责任。",
                context_text="最高人民法院在指导案例262号中明确了平台责任。",
                anchor_ids=["line00001"],
                entities=CaseCitationEntities(
                    case_refs=[
                        CaseRef(
                            reference_type=CaseReferenceType.WITHOUT_CASE_NUMBER,
                            case_name="指导案例262号",
                            court="最高人民法院",
                        )
                    ]
                ),
            )
        ],
    )
    searcher = FakeCaseSearcher()

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        case_recognizer=searcher,
    )

    check = frontend_doc.case_checks[0]
    assert check.lookup_status == CaseLookupStatus.VERIFIED
    assert check.evidence.case_number == "（2024）最高法民终262号"
    assert len(check.source_attempts) == 2
    assert searcher.keyword_calls[0][0] == "指导案例262号"
    assert searcher.semantic_calls


def test_case_name_containment_remains_verified(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.CASE_CITATION,
                text="甲公司诉乙公司合同纠纷案具有参考意义。",
                context_text="甲公司诉乙公司合同纠纷案具有参考意义。",
                anchor_ids=["line00001"],
                entities=CaseCitationEntities(
                    case_refs=[
                        CaseRef(
                            reference_type=CaseReferenceType.WITHOUT_CASE_NUMBER,
                            case_name="甲公司诉乙公司合同纠纷案",
                        )
                    ]
                ),
            )
        ],
    )

    class ContainmentSearcher:
        def recognize(self, text):
            raise AssertionError("not used")

        def search_keyword(self, title, fulltext):
            return [PkulawCaseRecord(title="甲公司诉乙公司合同纠纷案再审审查案")]

        def search_semantic(self, text):
            return []

    check = verify_claim_document(
        claim_doc, db_path, case_recognizer=ContainmentSearcher()
    ).case_checks[0]
    assert check.lookup_status == CaseLookupStatus.VERIFIED


def test_match_law_record_accepts_promulgation_notice_title():
    from ccitecheck.tracing.sources.pkulaw.client import PkulawLawRecord
    from ccitecheck.tracing.sources.pkulaw.matching import match_law_record

    records = [
        PkulawLawRecord(
            title="中国互联网金融协会关于举办“《常见类型移动互联网应用程序必要个人信息范围规定》政策解读”培训班的通知"
        ),
        PkulawLawRecord(
            title="国家互联网信息办公室秘书局等关于印发《常见类型移动互联网应用程序必要个人信息范围规定》的通知"
        ),
    ]
    matched = match_law_record(
        "常见类型移动互联网应用程序必要个人信息范围规定", records
    )
    assert matched is records[1]
    assert (
        match_law_record("常见类型移动互联网应用程序必要个人信息范围规定", records[:1])
        is None
    )


def _simple_claim(claim_id: str, text: str, title: str, article: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title=title,
                    source_type=LegalSourceType.LAW,
                    articles=[ArticleRef(article=article)],
                )
            ]
        ),
    )


class CountingSource:
    """记录 lookup 调用次数的假法条源。"""

    def __init__(self):
        self.calls = []

    def lookup(self, request: LookupRequest) -> LookupResult:
        self.calls.append((request.law_title, request.article_no))
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="counting",
            status=LookupStatus.ARTICLE_FOUND,
        )
        evidence = ArticleEvidence(
            law_title=request.law_title,
            source_type="law",
            article_no=request.article_no,
            article_text="条文",
            data_source=trace,
        )
        return LookupResult(LookupStatus.ARTICLE_FOUND, evidence, trace)


class FailOncePerQuoteChecker:
    def __init__(self):
        self.calls = {}

    def compare(self, doc_quote, quote_context, cited_source, evidence):
        count = self.calls.get(doc_quote, 0) + 1
        self.calls[doc_quote] = count
        if count == 1:
            raise SemanticTransportError("temporary EOF", "transport_error")
        return SemanticComparison(verdict=ComparisonVerdict.PASS)


def test_semantic_salvage_retries_in_submission_order_and_honors_cap(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("QWEN_SEMANTIC_WORKERS", "1")
    monkeypatch.setenv("QWEN_SALVAGE_MAX", "1")
    checker = FailOncePerQuoteChecker()
    claims = [
        _simple_claim("cl_00001", "第一次引用第五条。", "个人信息保护法", "第五条"),
        _simple_claim("cl_00002", "第二次引用第六条。", "个人信息保护法", "第六条"),
    ]
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
            claims=claims,
        ),
        tmp_path / "missing.sqlite",
        sources=[CountingSource()],
        semantic_checker=checker,
        include_cases=False,
    )
    comparisons = [
        reference.semantic_comparison
        for card in result.citation_cards
        for reference in card.references
    ]
    assert comparisons[0].execution_status == "completed"
    assert comparisons[0].notes == "（打捞轮恢复）"
    assert comparisons[1].execution_status == "llm_error"
    assert comparisons[1].retryable is True
    assert list(checker.calls.values()) == [2, 1]


def test_multiple_references_are_grouped_in_one_citation_card(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="依照《商标法》第十三条第一款、第三款，《解释》第九条、第十条，判决如下。",
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(legal_sources=[
            LegalSource(
                title="商标法",
                source_type=LegalSourceType.LAW,
                articles=[ArticleRef(article="第十三条", paragraphs=["第一款", "第三款"])],
            ),
            LegalSource(
                title="解释",
                source_type=LegalSourceType.JUDICIAL_INTERPRETATION,
                articles=[ArticleRef(article="第九条"), ArticleRef(article="第十条")],
            ),
        ]),
    )
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
            claims=[claim],
        ),
        db_path,
        sources=[CountingSource()],
    )
    assert len(result.citation_cards) == 1
    assert len(result.citation_cards[0].references) == 3
    assert result.citation_cards[0].references[0].paragraphs == ["第一款", "第三款"]
    summary = summarize_verification(result)
    assert summary.card_total == 1
    assert summary.reference_total == 3
    assert summary.total == 3
    result.case_checks.append(CaseCheck(
        check_id="cc_00001",
        claim_id="cl_00001",
        claim_text=claim.text,
        lookup_status=CaseLookupStatus.VERIFIED,
    ))
    mixed_summary = summarize_verification(result)
    assert mixed_summary.card_total == 1
    assert mixed_summary.reference_total == 4


def test_duplicate_citations_share_one_lookup(tmp_path: Path):
    """同一（法名, 条号）在多个 claim 中重复引用时只查一次。"""
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            _simple_claim("cl_00001", "第一次引用第五条。", "个人信息保护法", "第五条"),
            _simple_claim("cl_00002", "第二次引用第五条。", "个人信息保护法", "第五条"),
            _simple_claim("cl_00003", "引用第六条。", "个人信息保护法", "第六条"),
        ],
    )
    source = CountingSource()
    frontend_doc = verify_claim_document(claim_doc, db_path, sources=[source])
    # 3 条 check 全部产出，但底层只发生 2 次查询（第五条去重）
    assert sum(len(card.references) for card in frontend_doc.citation_cards) == 3
    assert len(source.calls) == 2
    assert frontend_doc.citation_cards[0].references[0].evidence.article_text == "条文"
    assert frontend_doc.citation_cards[1].references[0].evidence.article_text == "条文"


class CountingRecognizer:
    def __init__(self):
        self.calls = 0

    def recognize(self, text: str):
        self.calls += 1
        return [
            PkulawCaseNumber(
                text="（2020）京01民终1号",
                start=0,
                end=10,
                gid="g1",
                case_flag="（2020）京01民终1号",
                court="北京市第一中级人民法院",
                title="某案",
            )
        ]


def test_case_claims_batched_into_single_recognition(tmp_path: Path):
    """多个含案号的 claim 合并为一次案号识别调用。"""
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)

    def case_claim(claim_id: str, number: str) -> Claim:
        return Claim(
            claim_id=claim_id,
            claim_type=ClaimType.CASE_CITATION,
            text=f"参见{number}判决。",
            anchor_ids=["line00001"],
            entities=CaseCitationEntities(
                reference_type=CaseReferenceType.WITH_CASE_NUMBER,
                case_refs=[
                    CaseRef(
                        reference_type=CaseReferenceType.WITH_CASE_NUMBER,
                        case_number=number,
                    )
                ],
            ),
        )

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            case_claim("cl_00001", "（2020）京01民终1号"),
            case_claim("cl_00002", "（2021）沪02民终2号"),
        ],
    )
    recognizer = CountingRecognizer()
    frontend_doc = verify_claim_document(
        claim_doc, db_path, case_recognizer=recognizer, include_statutes=False
    )
    assert recognizer.calls == 1
    assert len(frontend_doc.case_checks) == 2
    statuses = {check.lookup_status for check in frontend_doc.case_checks}
    assert statuses == {CaseLookupStatus.VERIFIED, CaseLookupStatus.NOT_FOUND}


def test_rule_findings_skip_semantic_llm(tmp_path: Path):
    """确定性规则已有结论时不再调用语义核查。"""
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)

    class RepealedSource:
        def lookup(self, request: LookupRequest) -> LookupResult:
            trace = SourceTrace(
                tier=SourceTier.PKULAW_FALLBACK,
                source_name="fake",
                status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
            )
            evidence = ArticleEvidence(
                law_title=request.law_title,
                source_type="law",
                article_no=request.article_no,
                version_status="废止或失效",
                data_source=trace,
            )
            return LookupResult(trace.status, evidence, trace)

    class ExplodingChecker:
        def compare(self, *args, **kwargs):
            raise AssertionError("确定性结论已存在，不应调用 LLM")

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            _simple_claim(
                "cl_00001", "依据《合同法》第五十二条。", "合同法", "第五十二条"
            ),
        ],
    )
    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        sources=[RepealedSource()],
        semantic_checker=ExplodingChecker(),
    )
    check = frontend_doc.citation_cards[0].references[0]
    assert check.rule_findings
    assert check.rule_findings[0].error_type == SemanticErrorType.OUTDATED_SOURCE
    assert check.semantic_comparison.execution_status.value == "skipped"
    assert check.semantic_comparison.skipped_reason == "retrieval_incomplete"


def test_extraction_respects_scope_selection():
    """未勾选的核查范围在提取阶段即跳过。"""
    from ccitecheck.recognition.service import extract_claims
    from tests.test_rule_engine import _make_parsed_doc

    doc = _make_parsed_doc(
        [
            "依据《民法典》第五百七十七条，应当承担违约责任。",
            "参见（2020）京01民终1号判决。",
        ]
    )
    only_statutes = extract_claims(doc, include_statutes=True, include_cases=False)
    assert all(
        c.claim_type == ClaimType.LEGAL_SOURCE_CLAIM for c in only_statutes.claims
    )
    assert len(only_statutes.claims) == 1

    only_cases = extract_claims(doc, include_statutes=False, include_cases=True)
    assert all(c.claim_type != ClaimType.LEGAL_SOURCE_CLAIM for c in only_cases.claims)
    assert len(only_cases.claims) == 1


def test_llm_issue_appends_alternative_article_suggestion(tmp_path: Path):
    """判"无实质对应"后，从本地全文召回并给出"疑似应改引第X条"。"""
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn,
            {
                "title": "中华人民共和国民法典",
                "source_type": "law",
                "status": "has_articles",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第五百七十七条",
                "text": "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第六百五十七条",
                "text": "赠与合同是赠与人将自己的财产无偿给予受赠人，受赠人表示接受赠与的合同。",
            },
        )

    class MismatchChecker:
        suggestion_calls = 0

        def compare(self, doc_quote, quote_context, cited_source, evidence):
            return SemanticComparison(
                verdict=ComparisonVerdict.ISSUE,
                issues=[
                    SemanticIssue(
                        error_type=SemanticErrorType.NO_SUBSTANTIVE_MATCH,
                        risk_level=RiskLevel.HIGH,
                        diff_summary="所引条文调整违约责任，与赠与撤销无关",
                        suggestion="请核实引用条文。",
                    )
                ],
                notes="",
            )

        def suggest_article(self, doc_quote, candidates):
            self.suggestion_calls += 1
            assert any(c["article_no"] == "第六百五十七条" for c in candidates)
            return "第六百五十七条"

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="t.docx",
        ),
        claims=[
            _simple_claim(
                "cl_00001",
                "根据《民法典》第五百七十七条，赠与合同是赠与人将自己的财产无偿给予受赠人的合同。",
                "民法典",
                "第五百七十七条",
            ),
            _simple_claim(
                "cl_00002",
                "根据《民法典》第五百七十七条，赠与合同是赠与人将自己的财产无偿给予受赠人的合同。",
                "民法典",
                "第五百七十七条",
            ),
        ],
    )
    checker = MismatchChecker()
    frontend_doc = verify_claim_document(
        claim_doc, db_path, semantic_checker=checker
    )
    check = frontend_doc.citation_cards[0].references[0]
    suggestion = check.semantic_comparison.issues[0].suggestion
    assert "疑似应改引第六百五十七条" in suggestion
    assert checker.suggestion_calls == 1
    assert "疑似应改引第六百五十七条" in frontend_doc.citation_cards[1].references[0].semantic_comparison.issues[0].suggestion
