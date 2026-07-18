"""确定性规则判定与识别质量修复的单元测试（不依赖网络）。"""

from ccitecheck.recognition.cases import NAMED_CASE_PATTERN
from ccitecheck.recognition.statutes import _extract_articles_from_text
from ccitecheck.infrastructure.database import strip_version_annotation
from ccitecheck.judgment.deterministic import (
    build_rule_findings,
    classify_not_verifiable,
    suggest_similar_title,
)
from ccitecheck.domain.result import (
    ArticleEvidence,
    LookupStatus,
    RiskLevel,
    SemanticErrorType,
    SourceTier,
    SourceTrace,
)
from ccitecheck.tracing.sources import LookupResult


# ---------- D1 案例正则 ----------

def test_named_case_pattern_ignores_common_false_positives():
    negatives = [
        "业务部门应持续关注相关舆情与投诉情况，及时反馈群体性维权情况，对集体诉讼、民事诉讼做出预案。",
        "被诉侵权产品与涉案专利不构成相同。",
        "消费者可以提起诉讼或申请仲裁。",
        "上诉人对一审判决不服提起上诉。",
    ]
    for text in negatives:
        assert NAMED_CASE_PATTERN.search(text) is None, text


def test_named_case_pattern_matches_real_case_names():
    positives = [
        "甲公司诉乙公司买卖合同纠纷案",
        "斯广树诉天津联通电信服务合同纠纷案",
        "王老吉诉加多宝虚假宣传案",
    ]
    for text in positives:
        assert NAMED_CASE_PATTERN.search(text) is not None, text


# ---------- D2 版本注记剥离 ----------

def test_strip_version_annotation():
    assert strip_version_annotation("网络安全法（2025修正）") == "网络安全法"
    assert strip_version_annotation("公司法(2023修订)") == "公司法"
    assert strip_version_annotation("认定方法（试行）") == "认定方法（试行）"


# ---------- D3 条号范围展开 ----------

def test_article_range_expansion_fills_middle_articles():
    refs = _extract_articles_from_text("违反本法第四十三条至第四十五条规定")
    articles = {r.article for r in refs}
    assert {"第四十三条", "第四十四条", "第四十五条"} <= articles


# ---------- D4 不可核验文件分类 ----------

def testclassify_not_verifiable():
    assert classify_not_verifiable("互联网应用程序个人信息收集使用规定（征求意见稿）")
    assert classify_not_verifiable("信息安全技术 个人信息安全规范（GB/T 35273-2020）")
    assert classify_not_verifiable("中华人民共和国民法典") is None
    assert classify_not_verifiable("App违法违规收集使用个人信息行为认定方法") is None


# ---------- A1 条文不存在 ----------

def _local_partial_result(law_title: str, article_no: str, article_count: int):
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="本地库",
        status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
        metadata={"local_article_count": article_count},
    )
    evidence = ArticleEvidence(
        law_title=law_title,
        source_type="law",
        article_no=article_no,
        data_source=trace,
    )
    return LookupResult(trace.status, evidence, trace), [trace]


def test_article_not_exist_produces_high_finding():
    result, attempts = _local_partial_result("中华人民共和国民法典", "第一千三百条", 1260)
    findings = build_rule_findings(
        "中华人民共和国民法典", "第一千三百条", result, attempts, []
    )
    assert any(
        f.error_type == SemanticErrorType.LOCATION_ERROR
        and f.risk_level == RiskLevel.HIGH
        for f in findings
    )


# ---------- A2 废止检测 ----------

def test_repealed_law_produces_outdated_finding():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
    )
    evidence = ArticleEvidence(
        law_title="中华人民共和国合同法",
        source_type="law",
        version_status="废止或失效",
        data_source=trace,
    )
    result = LookupResult(trace.status, evidence, trace)
    findings = build_rule_findings(
        "中华人民共和国合同法", "第五十二条", result, [trace], []
    )
    assert any(
        f.error_type == SemanticErrorType.OUTDATED_SOURCE
        and f.risk_level == RiskLevel.HIGH
        for f in findings
    )
    # 已废止时不应叠加"未检索到条文"噪音
    assert not any(
        f.error_type == SemanticErrorType.LOCATION_ERROR for f in findings
    )


# ---------- A3 法源不存在与法名纠错 ----------

def test_law_not_found_after_completed_search_produces_high_finding():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.LAW_NOT_FOUND,
        metadata={"search_completed": True},
    )
    result = LookupResult(LookupStatus.LAW_NOT_FOUND, None, trace)
    findings = build_rule_findings(
        "中华人民共和国印章管理办法", "第五条", result, [trace], []
    )
    assert any(
        f.error_type == SemanticErrorType.SOURCE_NOT_FOUND
        and f.risk_level == RiskLevel.HIGH
        for f in findings
    )


def testsuggest_similar_title_catches_one_char_typo():
    suggestion = suggest_similar_title(
        "消费者召回管理暂行规定", ["消费品召回管理暂行规定", "中华人民共和国民法典"]
    )
    assert suggestion == "消费品召回管理暂行规定"


def testsuggest_similar_title_rejects_weak_matches():
    suggestion = suggest_similar_title(
        "中华人民共和国印章管理办法", ["中华人民共和国税收征收管理法"]
    )
    assert suggestion is None


# ---------- A4 款级越界与款切分 ----------

def _article_found_result(law_title: str, article_no: str, article_text: str):
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="本地库",
        status=LookupStatus.ARTICLE_FOUND,
    )
    evidence = ArticleEvidence(
        law_title=law_title,
        source_type="law",
        article_no=article_no,
        article_text=article_text,
        data_source=trace,
    )
    return LookupResult(trace.status, evidence, trace), [trace]


_PATENT_ARTICLE_9 = (
    "同样的发明创造只能授予一项专利权。但是，同一申请人同日对同样的发明创造"
    "既申请实用新型专利又申请发明专利，先获得的实用新型专利权尚未终止，且申请人"
    "声明放弃该实用新型专利权的，可以授予发明专利权。\n"
    "两个以上的申请人分别就同样的发明创造申请专利的，专利权授予最先申请的人。"
)


def test_paragraph_out_of_range_produces_high_finding():
    result, attempts = _article_found_result(
        "中华人民共和国专利法", "第九条", _PATENT_ARTICLE_9
    )
    findings = build_rule_findings(
        "中华人民共和国专利法", "第九条", result, attempts, [],
        paragraphs=["第五款"],
    )
    assert any(
        f.error_type == SemanticErrorType.LOCATION_ERROR
        and f.risk_level == RiskLevel.HIGH
        and "第五款" in f.diff_summary
        for f in findings
    )


def test_paragraph_in_range_produces_no_finding():
    result, attempts = _article_found_result(
        "中华人民共和国专利法", "第九条", _PATENT_ARTICLE_9
    )
    findings = build_rule_findings(
        "中华人民共和国专利法", "第九条", result, attempts, [],
        paragraphs=["第二款"],
    )
    assert not findings


def test_split_paragraphs_merges_item_lines():
    from ccitecheck.judgment.paragraphs import split_paragraphs

    text = (
        "本法所称的作品，是指文学、艺术和科学领域内具有独创性并能以一定形式表现的智力成果，包括：\n"
        "（一）文字作品；\n"
        "（二）口述作品；\n"
        "符合作品特征的其他智力成果依照本法规定保护。"
    )
    segments = split_paragraphs(text)
    assert len(segments) == 2
    assert "（二）口述作品" in segments[0]


def test_resolve_paragraph_locates_target_and_reports_total():
    from ccitecheck.judgment.paragraphs import resolve_paragraph

    location = resolve_paragraph("第二款", _PATENT_ARTICLE_9)
    assert location is not None
    assert location.number == 2
    assert location.total == 2
    assert "最先申请的人" in location.text

    overflow = resolve_paragraph("第三款", _PATENT_ARTICLE_9)
    assert overflow.text is None and overflow.total == 2
