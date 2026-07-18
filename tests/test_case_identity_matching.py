from ccitecheck.judgment.cases import _match_case_record, _same_court
from ccitecheck.tracing.sources.pkulaw.client import PkulawCaseRecord


def test_specific_court_alias_is_equivalent():
    assert _same_court("重庆一中院", "重庆市第一中级人民法院")


def test_broad_region_court_is_not_a_specific_court_alias():
    assert not _same_court("重庆法院", "重庆市第一中级人民法院")


def test_duplicate_exact_titles_require_a_unique_matching_court():
    records = [
        PkulawCaseRecord(title="甲诉乙合同纠纷案", court="重庆市第一中级人民法院"),
        PkulawCaseRecord(title="甲诉乙合同纠纷案", court="重庆市第二中级人民法院"),
    ]

    match, basis = _match_case_record(None, "甲诉乙合同纠纷案", "重庆一中院", records)

    assert match is records[0]
    assert basis == "exact_title_and_court"


def test_duplicate_exact_titles_do_not_accept_a_broad_region_court():
    records = [
        PkulawCaseRecord(title="甲诉乙合同纠纷案", court="重庆市第一中级人民法院"),
        PkulawCaseRecord(title="甲诉乙合同纠纷案", court="重庆市第二中级人民法院"),
    ]

    match, basis = _match_case_record(None, "甲诉乙合同纠纷案", "重庆法院", records)

    assert match is None
    assert basis is None


def test_duplicate_supplier_records_with_same_number_court_and_date_are_one_case():
    records = [
        PkulawCaseRecord(
            title="李某诉周某案",
            case_number="（2019）京73民终225号",
            court="北京知识产权法院",
            last_instance_date="2020.05.26",
            url="https://example.com/a",
        ),
        PkulawCaseRecord(
            title="李霞与周梅森案",
            case_number="（2019）京73民终225号",
            court="北京知识产权法院",
            last_instance_date="2020.05.26",
            holding="裁判观点",
            url="https://example.com/b",
        ),
    ]

    match, basis = _match_case_record("(2019)京73民终225号", None, None, records)

    assert match is records[1]
    assert basis == "exact_case_number_cluster"
