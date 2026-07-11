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
    def compare(self, doc_quote, quote_context, cited_source, evidence, diff_result):
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
    assert check.exact_comparison is not None
    assert not check.exact_comparison.exact_match
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
    assert check.exact_comparison is None
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
