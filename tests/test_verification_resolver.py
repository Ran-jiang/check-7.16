from pathlib import Path

from claims.schema import (
    ArticleRef,
    CaseCitationEntities,
    CaseRef,
    CaseReferenceType,
    Claim,
    ClaimDebug,
    ClaimDocument,
    ClaimMeta,
    ClaimType,
    LegalSource,
    LegalSourceClaimEntities,
    LegalSourceType,
    VerificationRoute,
)
from laws.sqlite_store import connect, init_db, upsert_article, upsert_law
from verification.resolver import verify_claim_document_for_frontend
from verification.schema import (
    ArticleEvidence,
    CaseLookupStatus,
    ComparisonConfidence,
    ComparisonVerdict,
    LookupStatus,
    RiskLevel,
    SemanticComparison,
    SemanticErrorType,
    SemanticIssue,
    SourceTier,
    SourceTrace,
)
from verification.pkulaw_mcp import PkulawCaseNumber, PkulawLawRecord
from verification.sources import LocalSQLiteSource, LookupRequest, LookupResult, PkulawFallbackSource


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
                block_ids=["b_00001"],
                verification_route=VerificationRoute.STATUTE_DATABASE,
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="劳动合同法",
                            source_type=LegalSourceType.LAW,
                            articles=[ArticleRef(article="第三十七条")],
                        )
                    ]
                ),
                debug=ClaimDebug(methods=["rule"], candidate_count=1),
            )
        ],
    )

    frontend_doc = verify_claim_document_for_frontend(claim_doc, db_path)

    assert len(frontend_doc.legal_checks) == 1
    check = frontend_doc.legal_checks[0]
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
                    error_type=SemanticErrorType.CONCLUSION_NOT_NECESSARILY_SUPPORTED,
                    risk_level=RiskLevel.MEDIUM,
                    diff_summary="文书未明示被告不履行或履行不符合约定",
                    suggestion="核实并补充违约事实。",
                )
            ],
            confidence=ComparisonConfidence.HIGH,
            notes="",
        )


def test_semantic_assessment_is_added_when_checker_is_configured(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(conn, {"title": "中华人民共和国民法典", "source_type": "law"})
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
                block_ids=["b_00001"],
                verification_route=VerificationRoute.STATUTE_DATABASE,
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="民法典",
                            source_type=LegalSourceType.LAW,
                            articles=[ArticleRef(article="第五百七十七条")],
                        )
                    ]
                ),
                debug=ClaimDebug(methods=["rule"], candidate_count=1),
            )
        ],
    )

    frontend_doc = verify_claim_document_for_frontend(
        claim_doc,
        db_path,
        semantic_checker=FakeSemanticChecker(),
    )

    check = frontend_doc.legal_checks[0]
    comparison = check.semantic_comparison
    assert comparison.verdict == ComparisonVerdict.ISSUE
    assert comparison.issues[0].error_type == SemanticErrorType.CONCLUSION_NOT_NECESSARILY_SUPPORTED


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
            {"article_no": "第二条", "text": "本法适用于境内网络的建设、运营、维护和使用。"},
        )

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text="根据《网络安全法》，保障公民、法人合法权益，维护国家安全和公共利益。",
                anchor_ids=["line00001"],
                block_ids=["b_00001"],
                verification_route=VerificationRoute.STATUTE_DATABASE,
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="网络安全法",
                            source_type=LegalSourceType.LAW,
                        )
                    ]
                ),
                debug=ClaimDebug(methods=["rule"], candidate_count=1),
            )
        ],
    )

    frontend_doc = verify_claim_document_for_frontend(
        claim_doc,
        db_path,
        semantic_checker=FakeSemanticChecker(),
    )

    check = frontend_doc.legal_checks[0]
    assert check.lookup_status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert check.article_no is None
    assert check.evidence.related_articles[0].article_no == "第一条"
    assert check.semantic_comparison is not None


class FakeLawListClient:
    def get_law_list(self, title="", fulltext=""):
        return [PkulawLawRecord(title="中华人民共和国国家安全法", timeliness=["现行有效"])]


def test_pkulaw_unnumbered_lookup_reports_tool_text_limit():
    result = PkulawFallbackSource(FakeLawListClient()).lookup(
        LookupRequest(
            law_title="中华人民共和国国家安全法",
            source_type="law",
            context_text="维护国家安全和公共利益",
        )
    )

    assert result.status == LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
    assert "不返回匹配条号和条文全文" in result.trace.message


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
    from laws.sqlite_store import seed_common_laws

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
                block_ids=["b_00001"],
                verification_route=VerificationRoute.STATUTE_DATABASE,
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="民法典",
                            source_type=LegalSourceType.LAW,
                            articles=[ArticleRef(article="第五百七十七条")],
                        )
                    ]
                ),
                debug=ClaimDebug(methods=["rule"], candidate_count=1),
            )
        ],
    )

    frontend_doc = verify_claim_document_for_frontend(
        claim_doc,
        db_path,
        sources=[
            LocalSQLiteSource(db_path),
            FakeArticleSource(),
        ],
    )

    check = frontend_doc.legal_checks[0]
    assert check.lookup_status == LookupStatus.ARTICLE_FOUND
    assert len(check.source_attempts) == 2
    assert "不履行合同义务" in check.evidence.article_text


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
        block_ids=["b_00001"],
        verification_route=VerificationRoute.CASE_DATABASE_EXACT,
        entities=CaseCitationEntities(
            case_refs=[
                CaseRef(
                    reference_type=CaseReferenceType.WITH_CASE_NUMBER,
                    case_number=case_number,
                )
            ]
        ),
        debug=ClaimDebug(methods=["rule"], candidate_count=1),
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

    frontend_doc = verify_claim_document_for_frontend(
        claim_doc,
        db_path,
        case_recognizer=recognizer,
    )

    assert not frontend_doc.legal_checks
    verified, flagged = frontend_doc.case_checks
    assert verified.lookup_status == CaseLookupStatus.VERIFIED
    assert verified.evidence.court == "浙江省杭州市钱塘区人民法院"
    assert verified.evidence.url.endswith(".html")
    assert flagged.lookup_status == CaseLookupStatus.NOT_FOUND
    assert flagged.evidence is None


def test_match_law_record_accepts_promulgation_notice_title():
    from verification.pkulaw_mcp import PkulawLawRecord
    from verification.sources import _match_law_record

    records = [
        PkulawLawRecord(title="中国互联网金融协会关于举办“《常见类型移动互联网应用程序必要个人信息范围规定》政策解读”培训班的通知"),
        PkulawLawRecord(title="国家互联网信息办公室秘书局等关于印发《常见类型移动互联网应用程序必要个人信息范围规定》的通知"),
    ]
    matched = _match_law_record("常见类型移动互联网应用程序必要个人信息范围规定", records)
    assert matched is records[1]

    # 只有培训班通知（无印发/发布字样）时不得视为命中
    assert _match_law_record("常见类型移动互联网应用程序必要个人信息范围规定", records[:1]) is None


def _simple_claim(claim_id: str, text: str, title: str, article: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        block_ids=["b_00001"],
        verification_route=VerificationRoute.STATUTE_DATABASE,
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title=title,
                    source_type=LegalSourceType.LAW,
                    articles=[ArticleRef(article=article)],
                )
            ]
        ),
        debug=ClaimDebug(methods=["rule"], candidate_count=1),
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
    frontend_doc = verify_claim_document_for_frontend(
        claim_doc, db_path, sources=[source]
    )
    # 3 条 check 全部产出，但底层只发生 2 次查询（第五条去重）
    assert len(frontend_doc.legal_checks) == 3
    assert len(source.calls) == 2
    assert frontend_doc.legal_checks[0].evidence.article_text == "条文"
    assert frontend_doc.legal_checks[1].evidence.article_text == "条文"


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
            block_ids=["b_00001"],
            verification_route=VerificationRoute.CASE_DATABASE_SEARCH,
            entities=CaseCitationEntities(
                reference_type=CaseReferenceType.WITH_CASE_NUMBER,
                case_refs=[CaseRef(reference_type=CaseReferenceType.WITH_CASE_NUMBER, case_number=number)],
            ),
            debug=ClaimDebug(methods=["rule"], candidate_count=1),
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
    frontend_doc = verify_claim_document_for_frontend(
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
            _simple_claim("cl_00001", "依据《合同法》第五十二条。", "合同法", "第五十二条"),
        ],
    )
    frontend_doc = verify_claim_document_for_frontend(
        claim_doc,
        db_path,
        sources=[RepealedSource()],
        semantic_checker=ExplodingChecker(),
    )
    check = frontend_doc.legal_checks[0]
    assert check.rule_findings
    assert check.rule_findings[0].error_type == SemanticErrorType.OUTDATED_SOURCE
    assert check.semantic_comparison is None
