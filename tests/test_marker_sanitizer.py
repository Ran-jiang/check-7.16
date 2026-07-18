from ccitecheck.domain.revisions import RevisionProposal
from ccitecheck.domain.statute_results import StatuteErrorCode, StatuteFinding
from ccitecheck.judgment.markers import strip_internal_markers


def test_strips_internal_markers_without_damaging_legal_text():
    text = "line00023[[内部]]⟦a⟧【anchor:x】保留【裁判要旨】[2019]京73民初1234号 guideline2024"
    cleaned = strip_internal_markers(text)
    assert "line00023" not in cleaned
    assert "[[内部]]" not in cleaned
    assert "⟦a⟧" not in cleaned
    assert "【anchor:x】" not in cleaned
    assert "【裁判要旨】" in cleaned
    assert "[2019]京73民初1234号" in cleaned
    assert "guideline2024" in cleaned


def test_revised_text_alone_is_not_auto_fixable():
    issue = StatuteFinding(
        code=StatuteErrorCode.CITATION_LOCATION_ERROR,
        risk_level="MEDIUM",
        summary="条号错误",
        suggestion="修改条号",
    )
    assert issue.revision is None


def test_explicit_revision_controls_machine_applicability():
    issue = StatuteFinding(
        code=StatuteErrorCode.MEANING_DISTORTED,
        risk_level="MEDIUM",
        summary="遗漏前提",
        suggestion="补充前提",
        revision=RevisionProposal(
            strategy="replace_exact_text",
            original_text="原文",
            revised_text="修订文本",
            rationale="补充前提",
            machine_applicable=True,
        ),
    )
    assert issue.revision.machine_applicable is True
