"""引用归属、候选验证、版本与来源边界的回归检查。"""
import pytest

from ccitecheck.domain.citation import Claim, ClaimType, LegalSourceClaimEntities, ClaimDocument, ClaimMeta
from ccitecheck.domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ccitecheck.infrastructure.database import connect, init_db, upsert_law, upsert_article
from ccitecheck.recognition.statutes import extract_legal_sources, _extract_articles_from_text, invalid_number_tokens
from ccitecheck.recognition.spans import locate_claim_article_spans
from ccitecheck.retrieval.sources.local_laws import LocalSQLiteSource
from ccitecheck.retrieval.sources.base import LocationCandidateResult, LookupRequest, LookupResult
from ccitecheck.verification.statutes.locator import resolve_location_candidates, validate_semantic_support
from ccitecheck.verification.statutes.deterministic import assess_statute
from ccitecheck.orchestration.scheduler import verify_claim_document

A = "当事人应当按照约定全面履行自己的义务"
B = "处理个人信息应当遵循合法正当必要和诚信原则"


def claim(text):
    c = Claim(claim_id="cl_test", claim_type=ClaimType.LEGAL_SOURCE_CLAIM, text=text,
              anchor_ids=["line1"], entities=LegalSourceClaimEntities(legal_sources=extract_legal_sources(text)))
    locate_claim_article_spans(c)
    return c


def evidence(text, number="第二条"):
    return ArticleEvidence(law_title="示例法", article_no=number, article_text=text,
        source_metadata={"version_key": "2024-01-01"},
        data_source=SourceTrace(tier=SourceTier.LOCAL_SQLITE, source_name="test", status=LookupStatus.ARTICLE_FOUND))


def run(tmp_path, text, articles, version="2024-01-01", checker=None):
    path = tmp_path / "test.sqlite"
    init_db(path)
    with connect(path) as conn:
        law_id = upsert_law(conn, {"title": "示例法", "source_url": "https://example.test/law"})
        for number, body in articles:
            upsert_article(conn, law_id, {"article_no": number, "text": body, "version_key": version})
    doc = ClaimDocument(claim_meta=ClaimMeta(), claims=[claim(text)])
    return verify_claim_document(doc, path, sources=[LocalSQLiteSource(path)],
                                 semantic_checker=checker, include_cases=False).statute_results


def test_existing_article_wrong_content_local_only(tmp_path):
    result = run(tmp_path, f"《示例法》第一条规定{B}。", [("第一条", A), ("第二条", B)])[0]
    assert [f.code.value for f in result.findings] == ["article_number_error"]
    assert result.findings[0].risk_level == "HIGH"
    assert result.findings[0].resolved_locator.article_no == "第二条"
    assert result.evidence.article_no == "第一条"
    assert result.correction_evidence.article_no == "第二条"
    assert result.lookup_status == LookupStatus.ARTICLE_FOUND


def test_document_lookup_retries_transient_source_error(tmp_path):
    class FlakySource:
        calls = 0

        def lookup(self, request):
            self.calls += 1
            if self.calls == 1:
                trace = SourceTrace(
                    tier=SourceTier.PKULAW_FALLBACK,
                    source_name="flaky",
                    status=LookupStatus.SOURCE_ERROR,
                )
                return LookupResult(LookupStatus.SOURCE_ERROR, None, trace)
            found = evidence(A, "第一条")
            return LookupResult(LookupStatus.ARTICLE_FOUND, found, found.data_source)

    source = FlakySource()
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(),
            claims=[claim(f"《示例法》第一条规定{A}。")],
        ),
        tmp_path / "missing.sqlite",
        sources=[source],
        include_cases=False,
    ).statute_results[0]

    assert source.calls == 2
    assert result.lookup_status == LookupStatus.ARTICLE_FOUND
    assert len(result.source_attempts) == 2


def test_standalone_paragraph_typo_can_still_recover_article_number(tmp_path):
    result = run(
        tmp_path,
        "《示例法》第100款规定，非法人组织可以确定一人或者数人代表该组织从事民事活动。",
        [
            ("第100条", A),
            ("第105条", "非法人组织可以确定一人或者数人代表该组织从事民事活动。"),
        ],
    )[0]

    assert {finding.code.value for finding in result.findings} == {
        "format_error", "article_number_error",
    }
    assert result.findings[-1].resolved_locator.article_no == "第105条"


def test_current_article_renumbering_uses_decisive_local_rank(tmp_path):
    claim_text = (
        "《示例法》第42条规定，行政机关作出责令停产停业、吊销许可证或者执照、"
        "较大数额罚款等行政处罚决定之前，应当告知当事人有要求举行听证的权利。"
    )
    result = run(tmp_path, claim_text, [
        ("第42条", "行政处罚应当由具有行政执法资格的执法人员实施。"),
        ("第44条", "行政机关在作出行政处罚决定之前，应当告知当事人拟作出的行政处罚内容。"),
        ("第63条", "行政机关拟作出较大数额罚款、吊销许可证件、责令停产停业等行政处罚决定，应当告知当事人有要求听证的权利。"),
        ("第70条", "行政机关应当依法公开行政处罚决定。"),
    ])[0]

    assert result.findings[0].code.value == "article_number_error"
    assert result.findings[0].resolved_locator.article_no == "第63条"


def test_authoritative_current_marker_allows_repair_without_version_key(tmp_path):
    class Source:
        def _evidence(self, article_no, text):
            trace = SourceTrace(
                tier=SourceTier.PKULAW_FALLBACK,
                source_name="北大法宝",
                status=LookupStatus.ARTICLE_FOUND,
                metadata={"timeliness": ["现行有效"]},
            )
            return ArticleEvidence(
                law_title="示例法(2023修正)",
                article_no=article_no,
                article_text=text,
                version_status="现行有效",
                source_metadata={"timeliness": ["现行有效"]},
                data_source=trace,
            )

        def lookup(self, request):
            evidence = self._evidence("第二百六十六条", "诈骗公私财物，数额较大的，依法处罚。")
            return LookupResult(LookupStatus.ARTICLE_FOUND, evidence, evidence.data_source)

        def locate_candidates(self, request):
            evidence = self._evidence(
                "第二百六十四条",
                "盗窃公私财物数额较大的，处三年以下有期徒刑、拘役或者管制。",
            )
            return LocationCandidateResult([evidence], evidence.data_source)

    text = "《示例法》第266条规定，盗窃公私财物数额较大的，处三年以下有期徒刑、拘役或者管制。"
    result = verify_claim_document(
        ClaimDocument(claim_meta=ClaimMeta(), claims=[claim(text)]),
        tmp_path / "missing.sqlite",
        sources=[Source()],
        include_cases=False,
    ).statute_results[0]

    assert result.findings[0].code.value == "article_number_error"
    assert result.findings[0].resolved_locator.article_no == "第二百六十四条"


def test_item_reference_without_parent_paragraph_is_repaired(tmp_path):
    text = (
        "第一款其他内容。\n"
        "第二款其他内容。\n"
        "有下列情形之一，调解无效的，应当准予离婚：\n"
        "（一）重婚或者与他人同居；\n（二）实施家庭暴力。"
    )
    result = run(
        tmp_path,
        "《示例法》第1079条第（一）项规定，重婚的，调解无效的，应当准予离婚。",
        [("第1079条", text)],
    )[0]

    assert result.findings[0].code.value == "citation_hierarchy_error"
    assert result.findings[0].resolved_locator.paragraph_no == "第三款"
    assert result.findings[0].resolved_locator.item_no == "第一项"


def test_current_position_wins_even_when_other_article_is_identical(tmp_path):
    result = run(tmp_path, f"《示例法》第一条规定{A}。", [("第一条", A), ("第二条", A)])[0]
    assert not result.findings
    assert result.outcome == "pass"


def test_existing_paragraph_wrong_content(tmp_path):
    result = run(tmp_path, f"《示例法》第一条第一款规定{B}。", [("第一条", A + "。\n" + B + "。")])[0]
    assert [f.code.value for f in result.findings] == ["citation_hierarchy_error"]
    assert result.findings[0].resolved_locator.paragraph_no == "第二款"


def test_existing_item_wrong_content(tmp_path):
    body = "应当满足以下条件：\n（一）" + A + "；\n（二）" + B + "。"
    result = run(tmp_path, f"《示例法》第一条第一项规定{B}。", [("第一条", body)])[0]
    assert [f.code.value for f in result.findings] == ["citation_hierarchy_error"]
    assert result.findings[0].resolved_locator.item_no == "第二项"


def test_extension_numbers_are_distinct(tmp_path):
    result = run(tmp_path, f"《示例法》第一条之一规定{B}。", [("第一条之一", A), ("第一条之二", B)])[0]
    assert result.findings[0].code.value == "article_number_error"
    assert result.findings[0].resolved_locator.article_no == "第一条之二"


def test_missing_article_has_one_verified_correction(tmp_path):
    result = run(tmp_path, f"《示例法》第九条规定{A}。", [("第一条", A)])[0]
    assert [f.code.value for f in result.findings] == ["article_number_error"]
    assert "不存在" not in result.findings[0].summary


class PkulawMissingSource:
    def __init__(
        self,
        candidate=None,
        *,
        status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
        conflict=False,
    ):
        self.candidate = candidate
        self.status = status
        self.conflict = conflict

    def lookup(self, request):
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="北大法宝 MCP",
            status=self.status,
            metadata={
                "title": "示例法（2024修正）",
                "version_key": "2024-01-01",
                "version_confirmed": self.conflict,
                "article_absence_confirmation": "conflict" if self.conflict else "single_signal",
                "article_absent_confirmed": (
                    self.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
                    and not self.conflict
                ),
            },
        )
        evidence = None if self.status == LookupStatus.LAW_NOT_FOUND else ArticleEvidence(
            law_title="示例法（2024修正）",
            article_no=request.article_no,
            source_metadata=trace.metadata,
            data_source=trace,
        )
        return LookupResult(self.status, evidence, trace)

    def locate_candidates(self, request):
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="北大法宝 MCP",
            status=LookupStatus.RELEVANT_ARTICLES_FOUND,
        )
        return LocationCandidateResult([self.candidate] if self.candidate else [], trace)


def test_authoritative_absence_is_article_number_error_without_quote(tmp_path):
    path = tmp_path / "absence.sqlite"
    init_db(path)
    result = verify_claim_document(
        ClaimDocument(claim_meta=ClaimMeta(), claims=[claim("参见《示例法》第九条。")]),
        path,
        sources=[PkulawMissingSource()],
        include_cases=False,
    ).statute_results[0]

    assert [finding.code.value for finding in result.findings] == ["article_number_error"]
    assert result.findings[0].risk_level == "HIGH"
    assert result.findings[0].resolved_locator is None


def test_authoritative_absence_bypasses_version_gate_for_verified_repair(tmp_path):
    path = tmp_path / "absence-repair.sqlite"
    init_db(path)
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝 MCP",
        status=LookupStatus.ARTICLE_FOUND,
        metadata={"version_key": "2025-01-01", "version_confirmed": False},
    )
    candidate = ArticleEvidence(
        law_title="示例法（2024修正）",
        article_no="第二条",
        article_text=A,
        source_metadata=trace.metadata,
        data_source=trace,
    )
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(),
            claims=[claim(f"《示例法》第九条规定{A}。")],
        ),
        path,
        sources=[PkulawMissingSource(candidate)],
        include_cases=False,
    ).statute_results[0]

    assert result.findings[0].code.value == "article_number_error"
    assert result.findings[0].resolved_locator.article_no == "第二条"


def test_law_not_found_is_not_hijacked_by_absence_branch(tmp_path):
    path = tmp_path / "law-not-found.sqlite"
    init_db(path)
    result = verify_claim_document(
        ClaimDocument(claim_meta=ClaimMeta(), claims=[claim("参见《星际航行法》第十条。")]),
        path,
        sources=[PkulawMissingSource(status=LookupStatus.LAW_NOT_FOUND)],
        include_cases=False,
    ).statute_results[0]

    assert all(finding.code.value != "article_number_error" for finding in result.findings)


def test_conflicting_absence_signals_stay_medium(tmp_path):
    path = tmp_path / "absence-conflict.sqlite"
    init_db(path)
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝 MCP",
        status=LookupStatus.ARTICLE_FOUND,
        metadata={"version_key": "2024-01-01", "version_confirmed": True},
    )
    candidate = ArticleEvidence(
        law_title="示例法（2024修正）",
        article_no="第二条",
        article_text=A,
        source_metadata=trace.metadata,
        data_source=trace,
    )
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(),
            claims=[claim(f"《示例法》第九条规定{A}。")],
        ),
        path,
        sources=[PkulawMissingSource(candidate, conflict=True)],
        include_cases=False,
    ).statute_results[0]

    assert result.findings[0].code.value == "article_not_found"
    assert result.findings[0].risk_level == "MEDIUM"


def test_unknown_version_never_confirms_correction(tmp_path):
    result = run(tmp_path, f"《示例法》第一条规定{B}。", [("第一条", A), ("第二条", B)], version="current")[0]
    assert not any(f.risk_level == "HIGH" for f in result.findings)
    assert result.outcome == "review"


def test_competing_articles_remain_pending(tmp_path):
    result = run(tmp_path, f"《示例法》第一条规定{B}。", [("第一条", A), ("第二条", B), ("第三条", B)])[0]
    assert not any(f.risk_level == "HIGH" for f in result.findings)
    assert result.location_resolution.status == "candidates_pending"


def test_paragraph_splicing_is_not_confirmed():
    assert resolve_location_candidates(A + "，" + B, [evidence(A + "。\n" + B)]).status != "resolved"
    assert resolve_location_candidates(A + "，" + B, [evidence(A + "；中间说明；" + B)]).status == "resolved"


def test_item_introduction_and_offsets():
    text = "处理个人信息需要符合以下条件：\n（一）取得个人的明确同意；\n（二）为履行合同所必需。"
    r = resolve_location_candidates("处理个人信息需要符合以下条件：取得个人的明确同意", [evidence(text)])
    assert r.status == "resolved"
    c = r.candidates[0]
    assert c.confirmed_level == "item"
    assert c.text == "".join(text[a:b] for a,b in c.evidence_spans)


@pytest.mark.parametrize("quote,text", [("拘役", "拘役"), ("罚金", "罚金"), (A, A.replace("应当", "不应当")), ("处三年以下有期徒刑或者拘役", "处五年以下有期徒刑或者拘役"), ("取得个人同意后可以处理个人信息", "取得个人的同意后可以处理个人信息")])
def test_short_and_substantive_differences_not_confirmed(quote,text):
    assert resolve_location_candidates(quote, [evidence(text)]).status != "resolved"


def test_numeric_normalization_and_adjacent_fragments():
    r = resolve_location_candidates("处3年以下有期徒刑，并处罚金", [evidence("处三年以下有期徒刑并处罚金")])
    assert r.status == "resolved"


def test_semantic_evidence_spans_are_not_sufficient():
    q, t = "处三年以下有期徒刑", "处五年以下有期徒刑"
    verdict = {"supported": True, "differences": [], "checks": dict.fromkeys(["subject","condition","numbers","negation","consequence"], True), "evidence": [{"quote_start":0,"quote_end":len(q),"source_start":0,"source_end":len(t),"quote":q,"source":t}]}
    assert not validate_semantic_support(q,t,verdict)
    verdict["evidence"][0]["source"] = "编造的文本"
    assert not validate_semantic_support(q,t,verdict)


def test_counts_do_not_prove_complete_corpus():
    trace = evidence(A).data_source
    trace.status = LookupStatus.LAW_FOUND_ARTICLE_MISSING
    trace.metadata = {"local_article_count": 505}
    r = LookupResult(trace.status, None, trace)
    f = assess_statute("示例法", "第五百条", r, [trace], [])[0]
    assert f.risk_level == "MEDIUM" and "不存在" not in f.summary.replace("尚不能确认条号不存在", "")
    trace.metadata.update(full_text_complete=True, version_confirmed=True, completeness_source="https://example.test/full", article_keys=["1", "1-1"])
    assert assess_statute("示例法", "第二条", r, [trace], [])[0].risk_level == "HIGH"
    assert assess_statute("示例法", "第一条之一", r, [trace], [])[0].risk_level == "MEDIUM"


def test_ranges_preserve_digit_style_and_order():
    assert [a.article for a in _extract_articles_from_text("第3条至第5条，第五条，7-8条")] == ["第3条","第4条","第5条","第7条","第8条"]
    assert [a.article for a in _extract_articles_from_text("第三条至第五条")] == ["第三条","第四条","第五条"]
    assert invalid_number_tokens("第一百百条") == ["一百百"]


def test_law_url_fallback_all_local_branches(tmp_path):
    path = tmp_path / "links.sqlite"
    init_db(path)
    with connect(path) as conn:
        law = upsert_law(conn, {"title":"示例法", "source_url":"https://example.test/full"})
        upsert_article(conn, law, {"article_no":"第一条", "text":A})
    source = LocalSQLiteSource(path)
    for request in [LookupRequest("示例法", "第一条"), LookupRequest("示例法", "第九条"), LookupRequest("示例法", query_text=A), LookupRequest("示例法", existence_only=True)]:
        assert source.lookup(request).trace.source_url == "https://example.test/full"


def test_no_number_substantive_request_recalls_text(tmp_path):
    result = run(tmp_path, f"《示例法》规定{A}。", [("第一条", A)])[0]
    assert result.lookup_status == LookupStatus.RELEVANT_ARTICLES_FOUND


def test_semantic_paraphrase_can_resolve_with_checked_spans():
    from ccitecheck.infrastructure.debug_timing import DebugTimer
    q = "个人信息处理者取得个人的同意后可以处理个人信息"
    t = "个人信息处理者经个人同意可以处理个人信息"
    class Checker:
        def verify_location(self, quote, current, source):
            return {"supported": True, "differences": [],
                    "checks": dict.fromkeys(["subject","condition","numbers","negation","consequence"], True),
                    "evidence": [{"quote_start":0,"quote_end":len(quote),"source_start":0,"source_end":len(source),"quote":quote,"source":source}]}
    timer = DebugTimer()
    with timer.activate():
        r = resolve_location_candidates(q, [evidence(t)], semantic_checker=Checker(), current_text=A)
    assert r.status == "resolved" and r.candidates[0].supported
    assert timer.snapshot()["location.verify_llm"].calls == 1


def test_indented_authority_offsets_remain_exact():
    text = "第一条　处理个人信息需要符合以下条件：\r\n    (一)取得个人的明确同意；\r\n    (二)为履行合同所必需。"
    r = resolve_location_candidates("处理个人信息需要符合以下条件：取得个人的明确同意", [evidence(text, "第一条")])
    assert r.status == "resolved"
    assert r.candidates[0].text == "".join(text[a:b] for a,b in r.candidates[0].evidence_spans)


@pytest.mark.parametrize("supports", [True, False])
def test_planner_candidate_uses_same_content_verification(tmp_path, supports):
    from ccitecheck.domain.queries import RepairPlan, RepairDiagnosis, RetrievalQueryPlan
    class Source:
        def lookup(self, request):
            e = evidence(B if supports else A)
            e.data_source.metadata.update(version_key="2024-01-01", version_confirmed=True)
            if request.article_no == "第二条":
                return LookupResult(LookupStatus.ARTICLE_FOUND, e, e.data_source)
            missing = e.model_copy(update={"article_no":request.article_no,"article_text":None})
            return LookupResult(LookupStatus.LAW_FOUND_ARTICLE_MISSING, missing, e.data_source)
    class Planner:
        def plan_repair(self, **kwargs):
            return RepairPlan(diagnosis=RepairDiagnosis(reason="article_error", confidence=.99, message="候选第二条"),
                retry_request=RetrievalQueryPlan(route="statute_exact",target_name="示例法",article_no="第二条"))
    path = tmp_path / "planner.sqlite"
    init_db(path)
    c = claim(f"《示例法》第九条规定{B}。")
    r = verify_claim_document(ClaimDocument(claim_meta=ClaimMeta(), claims=[c]), path,
        sources=[Source()], semantic_checker=Planner(), include_cases=False).statute_results[0]
    assert r.lookup_status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
    assert r.evidence.article_text is None
    assert r.correction_evidence is not None
    assert any(f.code.value == "article_number_error" and f.risk_level == "HIGH" for f in r.findings) == supports


def test_local_corpus_loaded_once_per_run(tmp_path, monkeypatch):
    import ccitecheck.retrieval.sources.local_laws as module
    original = module.list_current_articles
    calls = []
    def tracked(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "list_current_articles", tracked)
    run(tmp_path, f"《示例法》第一条规定{A}，第二条规定{B}。", [("第一条", A), ("第二条", B)])
    assert len(calls) == 1


def test_verified_url_enrichment_preserves_local_text_and_source():
    from ccitecheck.orchestration.policies.retrieval import lookup_with_chain
    local = evidence(A, "第一条")
    local.data_source.metadata.update(version_key="2024-01-01",version_identified=True)
    remote = local.model_copy(deep=True)
    remote.data_source.tier = SourceTier.PKULAW_FALLBACK
    remote.data_source.source_url = "https://example.test/remote"
    class Source:
        def __init__(self, e): self.e = e
        def lookup(self, request): return LookupResult(LookupStatus.ARTICLE_FOUND, self.e, self.e.data_source)
    r, attempts = lookup_with_chain([Source(local), Source(remote)], LookupRequest("示例法", "第一条"))
    assert r.evidence is local
    assert r.evidence.data_source.tier == SourceTier.LOCAL_SQLITE
    assert r.trace.source_url == remote.data_source.source_url
    assert len(attempts) == 2


def test_cross_version_related_evidence_is_not_relabelled(tmp_path):
    from ccitecheck.domain.evidence import ArticleExcerpt
    class Source:
        def lookup(self, request):
            e = evidence(A)
            e.article_no = None
            e.article_text = None
            e.related_articles = [ArticleExcerpt(law_title="示例法",version_key="2020-01-01",article_no="第二条",article_text=B,relevance_score=1)]
            e.data_source.metadata.update(version_key="2024-01-01",version_confirmed=True)
            return LookupResult(LookupStatus.RELEVANT_ARTICLES_FOUND,e,e.data_source)
    path = tmp_path / "versions.sqlite"
    init_db(path)
    r = verify_claim_document(ClaimDocument(claim_meta=ClaimMeta(),claims=[claim(f"《示例法》第九条规定{B}。")]),
        path,sources=[Source()],include_cases=False).statute_results[0]
    assert not any(f.risk_level == "HIGH" for f in r.findings)


def test_corrected_content_still_checks_independent_application_issue(tmp_path):
    from ccitecheck.domain.statute_results import LegalApplicationCheck, LegalApplicationReview
    class Checker:
        def compare_application(self, original_text, authorities):
            assert authorities[0].article_text == B
            return LegalApplicationCheck(verdict="review",reviews=[LegalApplicationReview(
                error_type="format_error",summary="独立的错别字",suggestion="修改错别字")])
    result = run(tmp_path, f"《示例法》第一条规定{B}。", [("第一条",A),("第二条",B)],checker=Checker())[0]
    assert result.findings[0].code.value == "article_number_error"
    assert result.application_check.reviews[0].error_type == "format_error"


def test_item_number_uses_actual_marker_not_list_offset():
    from ccitecheck.verification.statutes.structure import parse_article_structure
    from ccitecheck.verification.statutes.location import assess_location, LocationStatus
    from ccitecheck.domain.statute_results import StatuteLocator
    structure = parse_article_structure("第一条", "符合条件：\n（一）第一项内容；\n（三）第三项内容。")
    result = assess_location(structure,[StatuteLocator(article_no="第一条",item_no="第二项")])
    assert result.status == LocationStatus.INVALID
    result = assess_location(structure,[StatuteLocator(article_no="第一条",item_no="第三项")])
    assert result.status == LocationStatus.VALID and "第三项内容" in result.authoritative_text


def test_range_preserves_explicit_endpoint_hierarchy():
    refs = _extract_articles_from_text("第3条至第5条第二款")
    assert refs[-1].article == "第5条" and refs[-1].paragraphs == ["第二款"]
    assert refs[0].paragraphs == [] and refs[1].paragraphs == []


def test_import_restores_verified_criminal_law_link_for_matching_version(tmp_path):
    path = tmp_path / "import.sqlite"
    init_db(path)
    with connect(path) as conn:
        law = upsert_law(conn, {"title":"中华人民共和国刑法"})
        upsert_article(conn,law,{"article_no":"第一条","text":A,"version_key":"2021-03-01"})
        row = conn.execute("SELECT source_url FROM articles WHERE law_id=?",(law,)).fetchone()
        assert row["source_url"] == "https://www.lawinfochina.com/display.aspx?id=34470&lib=law"
        assert conn.execute("SELECT source_url FROM laws WHERE id=?",(law,)).fetchone()[0] == row["source_url"]
        upsert_article(conn,law,{"article_no":"第一条","text":B,"version_key":"2024-03-01"})
        assert conn.execute("SELECT source_url FROM articles WHERE law_id=? AND version_key='2024-03-01'",(law,)).fetchone()[0] is None


def test_omitted_substantive_condition_does_not_confirm_quote():
    quote = "行为人应当承担相应的赔偿责任"
    source = "如果行为人具有过错，行为人应当承担相应的赔偿责任。"
    assert resolve_location_candidates(quote, [evidence(source)]).status != "resolved"


def test_local_verified_location_skips_planner_retry(tmp_path):
    class Planner:
        calls = 0
        def plan_repair(self, **kwargs):
            self.calls += 1
            raise AssertionError("本地已经确认，无须规划重试")
    planner = Planner()
    r = run(tmp_path, f"《示例法》第九条规定{A}。", [("第一条", A)], checker=planner)[0]
    assert r.findings[0].code.value == "article_number_error"
    assert planner.calls == 0
