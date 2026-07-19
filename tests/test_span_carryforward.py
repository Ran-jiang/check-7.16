"""承前引用（同条第X款/第五款）的跨句定位兜底。"""

from ccitecheck.domain.citation import (
    ArticleRef,
    Claim,
    ClaimType,
    LegalSource,
    LegalSourceClaimEntities,
    LegalSourceType,
)
from ccitecheck.recognition.spans import locate_claim_article_spans


def _claim(text: str, articles: list[ArticleRef]) -> Claim:
    return Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="中华人民共和国反不正当竞争法",
                    source_type=LegalSourceType.LAW,
                    articles=articles,
                )
            ]
        ),
    )


def test_carryforward_paragraph_reference_is_located():
    text = (
        "《中华人民共和国反不正当竞争法》第十三条第三款规定，经营者不得破坏技术管理措施。"
        "同条第四款规定经营者不得滥用平台规则。第五款进一步规定其他情形。"
    )
    claim = _claim(text, [
        ArticleRef(article="第十三条", paragraphs=["第三款"]),
        ArticleRef(article="第十三条", paragraphs=["第四款"]),
        ArticleRef(article="第十三条", paragraphs=["第五款"]),
    ])
    locate_claim_article_spans(claim)

    para3, para4, para5 = claim.entities.legal_sources[0].articles
    assert para3.span_status == "located"
    for ref, marker in [(para4, "第四款"), (para5, "第五款")]:
        assert ref.span_status == "located", f"{marker} 未定位"
        s, e = ref.citation_span
        assert marker in text[s:e]
