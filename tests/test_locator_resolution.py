from ccitecheck.domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ccitecheck.judgment.statutes import resolve_location_candidates


def _evidence(article_no: str, text: str) -> ArticleEvidence:
    return ArticleEvidence(
        law_title="示例法",
        source_type="law",
        article_no=article_no,
        article_text=text,
        data_source=SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="北大法宝",
            source_url="https://example.test/law",
            status=LookupStatus.RELEVANT_ARTICLES_FOUND,
        ),
    )


def test_unique_verbatim_mcp_candidate_resolves_to_paragraph():
    resolution = resolve_location_candidates(
        "根据示例法规定，第二款正确内容。",
        [_evidence("第二条", "第一款其他内容。\n第二款正确内容。")],
    )

    assert resolution.status == "resolved"
    assert resolution.candidates[0].locator.article_no == "第二条"
    assert resolution.candidates[0].locator.paragraph_no == "第二款"


def test_multiple_verbatim_candidates_remain_pending():
    resolution = resolve_location_candidates(
        "共同内容。",
        [
            _evidence("第二条", "共同内容。"),
            _evidence("第三条", "共同内容。"),
        ],
    )

    assert resolution.status == "candidates_pending"
    assert len(resolution.candidates) == 2


def test_paraphrase_is_not_forced_into_a_location():
    resolution = resolve_location_candidates(
        "文书进行了概括转述。",
        [_evidence("第二条", "权威原文使用了完全不同的表述。")],
    )

    assert resolution.status == "not_found"
