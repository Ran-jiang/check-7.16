"""法域识别与超边界拦截（out_of_scope）的单元测试。"""

from pathlib import Path

from ccitecheck.application.verify_claims import verify_claim_document
from ccitecheck.domain.citation import (
    ArticleRef,
    Claim,
    ClaimDocument,
    ClaimMeta,
    ClaimType,
    CaseCitationEntities,
    CaseRef,
    CaseReferenceType,
    LegalSource,
    LegalSourceClaimEntities,
    LegalSourceType,
)
from ccitecheck.domain.evidence import CaseLookupStatus, LookupStatus
from ccitecheck.infrastructure.database import init_db
from ccitecheck.judgment.cases import verify_case_claims
from ccitecheck.recognition.cases import extract_case_refs
from ccitecheck.recognition.jurisdiction import detect_jurisdiction
from ccitecheck.recognition.statutes import extract_legal_sources


# ---------- 法域检测 ----------

def test_detect_jurisdiction_by_adjacent_prefix():
    assert detect_jurisdiction("著作权法", "大陆法系国家多规定，德国") == "FOREIGN"
    assert detect_jurisdiction("著作权法", "……参见法国") == "FOREIGN"
    assert detect_jurisdiction("人工智能统一规则", "2021年4月，欧盟") == "EU"
    assert detect_jurisdiction("著作权法", "依据我国") == "CN"
    assert detect_jurisdiction("著作权法", "") == "CN"


def test_detect_jurisdiction_generic_mentions_do_not_trigger():
    # 泛称不紧邻书名号时不应触发
    assert detect_jurisdiction("著作权法", "大陆法系国家多有规定，我国") == "CN"


def test_detect_jurisdiction_by_alias_table():
    assert detect_jurisdiction("通用数据保护条例", "") == "EU"
    assert detect_jurisdiction("知识产权法典", "") == "FOREIGN"
    assert detect_jurisdiction("GDPR", "") == "EU"


def test_extract_legal_sources_carries_jurisdiction():
    sources = extract_legal_sources(
        "德国《著作权法》第2条与法国《知识产权法典》均有规定；"
        "我国《著作权法》第三条另有定义。"
    )
    by_title = {}
    for source in sources:
        by_title.setdefault(source.title, source)
    assert by_title["著作权法"].jurisdiction == "FOREIGN"
    assert by_title["知识产权法典"].jurisdiction == "FOREIGN"

    cn = extract_legal_sources("依据《著作权法》第三条的定义。")
    assert cn[0].jurisdiction == "CN"

    eu = extract_legal_sources("欧盟《通用数据保护条例》正式生效。")
    assert eu[0].jurisdiction == "EU"


# ---------- 外国判例识别 ----------

def test_extract_case_refs_flags_foreign_citations():
    refs = extract_case_refs(
        "美国联邦最高法院在 Roe v. Wade 案及 347 U.S. 483 判例中确立了相关规则。"
    )
    foreign = [r for r in refs if r.jurisdiction == "FOREIGN"]
    assert any("Roe" in (r.case_name or "") for r in foreign)
    assert any("U.S." in (r.case_name or "") for r in foreign)


def test_extract_case_refs_chinese_cases_stay_cn():
    refs = extract_case_refs("在腾讯公司诉上海盈讯公司著作权侵权纠纷案中，法院认为……")
    assert refs and all(r.jurisdiction == "CN" for r in refs)


# ---------- 法规侧拦截 ----------

def _statute_claim_doc(jurisdiction: str, title: str = "著作权法") -> ClaimDocument:
    return ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text=f"德国《{title}》第2条另有规定。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title=title,
                            source_type=LegalSourceType.LAW,
                            jurisdiction=jurisdiction,
                            articles=[ArticleRef(article="第二条")],
                        )
                    ]
                ),
            )
        ],
    )


def test_foreign_statute_is_intercepted_without_lookup(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    frontend_doc = verify_claim_document(
        _statute_claim_doc("FOREIGN"), db_path, include_cases=False
    )
    check = frontend_doc.statute_results[0]
    assert check.lookup_status == LookupStatus.OUT_OF_SCOPE
    assert check.evidence is None
    assert not check.findings
    assert check.outcome == "bug"
    assert check.source_attempts[0].source_name == "CCiteCheck 法域分类"
    assert "超出本产品核查边界" in check.source_attempts[0].message


def test_eu_statute_without_gateway_reports_not_configured(
    tmp_path: Path, monkeypatch
):
    # 置空而非删除：本机 .env 配置了真实网关，空串可挡住 load_project_env 的 setdefault
    monkeypatch.setenv("EURLEX_MCP_GATEWAY", "")
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    frontend_doc = verify_claim_document(
        _statute_claim_doc("EU", "通用数据保护条例"), db_path, include_cases=False
    )
    check = frontend_doc.statute_results[0]
    assert check.lookup_status == LookupStatus.SOURCE_NOT_CONFIGURED
    assert "欧盟法规数据源未配置" in check.source_attempts[0].message


# ---------- 案例侧拦截 ----------

def test_foreign_case_is_intercepted_without_recognizer_calls():
    class ExplodingRecognizer:
        def __getattr__(self, name):
            raise AssertionError("外国判例不应触发任何案例数据源调用")

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.CASE_CITATION,
                text="美国联邦最高法院在 Roe v. Wade 案中确立了相关规则。",
                anchor_ids=["line00001"],
                entities=CaseCitationEntities(
                    case_refs=[
                        CaseRef(
                            reference_type=CaseReferenceType.WITHOUT_CASE_NUMBER,
                            case_name="Roe v. Wade",
                            jurisdiction="FOREIGN",
                        )
                    ]
                ),
            )
        ],
    )
    checks = verify_case_claims(claim_doc, ExplodingRecognizer())
    assert len(checks) == 1
    assert checks[0].lookup_status == CaseLookupStatus.OUT_OF_SCOPE
    assert "不支持的外国法域" in checks[0].message
