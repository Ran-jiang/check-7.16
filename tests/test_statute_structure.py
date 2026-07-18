from ccitecheck.domain.statute_results import StatuteLocator
from ccitecheck.judgment.statutes import (
    LocationStatus,
    assess_location,
    parse_article_structure,
)


ARTICLE_TEXT = """第二条　第一款内容。
第二款引导语：
（一）第一项内容；
（二）第二项内容。
第三款内容。"""


def test_parse_article_structure_preserves_paragraph_and_item_levels():
    structure = parse_article_structure("第二条", ARTICLE_TEXT)

    assert structure is not None
    assert [paragraph.paragraph_no for paragraph in structure.paragraphs] == [
        "第一款", "第二款", "第三款"
    ]
    assert [item.item_no for item in structure.paragraphs[1].items] == [
        "第一项", "第二项"
    ]


def test_location_selects_the_cited_paragraph_only():
    structure = parse_article_structure("第二条", ARTICLE_TEXT)

    assessment = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第三款")],
    )

    assert assessment.status == LocationStatus.VALID
    assert assessment.authoritative_text == "第三款内容。"


def test_location_rejects_missing_paragraph():
    structure = parse_article_structure("第二条", ARTICLE_TEXT)

    assessment = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第四款")],
    )

    assert assessment.status == LocationStatus.INVALID
    assert "不存在第四款" in assessment.message


def test_location_validates_item_inside_its_paragraph():
    structure = parse_article_structure("第二条", ARTICLE_TEXT)

    valid = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第二款", item_no="第二项")],
    )
    invalid = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第三款", item_no="第二项")],
    )

    assert valid.authoritative_text == "（二）第二项内容。"
    assert invalid.status == LocationStatus.INVALID
    assert "第三款共0项" in invalid.message


def test_structure_unavailable_does_not_validate_subarticle_locator():
    assessment = assess_location(
        None,
        [StatuteLocator(article_no="第二条", paragraph_no="第二款")],
    )

    assert assessment.status == LocationStatus.STRUCTURE_UNAVAILABLE


def test_flattened_article_does_not_claim_later_paragraph_is_missing():
    structure = parse_article_structure("第二条", "第二条　未保留自然段边界的条文文本。")

    assessment = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第二款")],
    )

    assert assessment.status == LocationStatus.STRUCTURE_UNAVAILABLE
