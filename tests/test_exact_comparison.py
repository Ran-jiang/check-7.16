from verification.exact_comparison import compare_exact_text


def test_exact_comparison_preserves_literal_differences():
    result = compare_exact_text("应当承担责任。", "应当承担责任")

    assert not result.exact_match
    assert result.document_text == "应当承担责任。"
    assert result.statute_text == "应当承担责任"
    assert result.operations[-1].document_text == "。"


def test_exact_comparison_reports_identical_text():
    result = compare_exact_text("完全一致", "完全一致")

    assert result.exact_match
    assert result.operations == []
