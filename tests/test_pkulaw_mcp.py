import json

import pytest

from ccitecheck.domain.evidence import LookupStatus
from ccitecheck.tracing.sources.base import LookupRequest
from ccitecheck.tracing.sources.pkulaw.client import (
    MCP_ENDPOINTS,
    PkulawArticle,
    PkulawLawRecord,
    PkulawMcpClient,
    PkulawMcpError,
    PkulawNotFoundError,
    normalize_article_no,
)
from ccitecheck.tracing.sources.pkulaw.statutes import PkulawFallbackSource


class FakePkulawClient(PkulawMcpClient):
    def __init__(self, payload):
        super().__init__(access_token="test-token")
        self.payload = payload

    def _call_tool(self, endpoint, tool_name, arguments):
        return self.payload


def _mcp_text_payload(data):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(data, ensure_ascii=False),
                }
            ]
        },
    }


def test_get_article_uses_exact_semantic_tool_and_normalizes_number():
    client = CapturingPkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": {
                    "title": "中华人民共和国民法典",
                    "article": "第四十八条　条文内容",
                    "url": "https://x",
                },
            }
        )
    )
    article = client.get_article("民法典", "第48条")
    assert article.article_no == "第四十八条"
    assert article.article_text == "条文内容"
    assert client.calls == [
        (
            MCP_ENDPOINTS["law_semantic"],
            "get_article",
            {"title": "民法典", "number": "第四十八条"},
        )
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("第48条", "第四十八条"),
        ("第184条之1", "第一百八十四条之一"),
        ("第一百八十四条之一", "第一百八十四条之一"),
        ("非法输入", "非法输入"),
    ],
)
def test_normalize_article_no(raw, expected):
    assert normalize_article_no(raw) == expected


def test_get_law_list_parses_candidates():
    client = FakePkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": [
                    {
                        "Title": "中华人民共和国民法典",
                        "Url": "[北大法宝](https://example.com)",
                        "IssueDepartment": ["全国人民代表大会"],
                        "TimelinessDic": ["现行有效"],
                    }
                ],
            }
        )
    )

    records = client.get_law_list(title="民法典")

    assert len(records) == 1
    assert records[0].title == "中华人民共和国民法典"
    assert records[0].issue_department == ["全国人民代表大会"]


def test_get_law_list_discards_obsolete_lar_mcp_url():
    client = FakePkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": [{
                    "Title": "中华人民共和国商标法",
                    "Url": "[北大法宝](https://www.pkulaw.com/lar/dead.html?way=mcp)",
                }],
            }
        )
    )

    assert client.get_law_list(title="商标法")[0].url is None


class CapturingPkulawClient(FakePkulawClient):
    def __init__(self, payload):
        super().__init__(payload)
        self.calls = []

    def _call_tool(self, endpoint, tool_name, arguments):
        self.calls.append((endpoint, tool_name, arguments))
        return self.payload


@pytest.mark.parametrize(
    ("method", "args", "endpoint", "tool_name", "arguments"),
    [
        (
            "get_law_list",
            ("民法典", "违约责任"),
            MCP_ENDPOINTS["law_keyword"],
            "get_law_list",
            {"lawInput": {"Title": "民法典", "Fulltext": "违约责任"}},
        ),
        (
            "get_case_list",
            ("指导案例262号", "平台 责任"),
            MCP_ENDPOINTS["case_keyword"],
            "get_case_list",
            {"caseInput": {"Title": "指导案例262号", "Fulltext": "平台 责任"}},
        ),
    ],
)
def test_keyword_tools_use_official_mcp_paths_and_arguments(
    method, args, endpoint, tool_name, arguments
):
    payload = _mcp_text_payload({"Message": "成功", "Data": []})
    client = CapturingPkulawClient(payload)

    getattr(client, method)(*args)

    assert client.calls == [(endpoint, tool_name, arguments)]


def test_semantic_tools_parse_law_and_case_records():
    law_client = CapturingPkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": [
                    {
                        "Title": "中华人民共和国民法典",
                        "ArticleNO": "第五百七十七条",
                        "FullText": "当事人一方不履行合同义务，应当承担违约责任。",
                        "Url": "https://example.com/law/577",
                    }
                ],
            }
        )
    )
    case_client = CapturingPkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": [
                    {
                        "Title": "指导性案例262号：某平台纠纷案",
                        "CaseNO": "（2024）最高法民终262号",
                        "Court": "最高人民法院",
                        "Url": "https://example.com/case/262",
                    }
                ],
            }
        )
    )

    articles = law_client.search_law_articles("违约责任")
    cases = case_client.search_cases("指导案例262号")

    assert articles[0].article_no == "第五百七十七条"
    assert articles[0].article_text.startswith("当事人")
    assert law_client.calls[0][:2] == (
        MCP_ENDPOINTS["law_semantic"],
        "search_article",
    )
    assert cases[0].case_number == "（2024）最高法民终262号"
    assert case_client.calls[0][:2] == (
        MCP_ENDPOINTS["case_semantic"],
        "search_case",
    )


def test_current_access_token_and_gateway_configuration(monkeypatch):
    monkeypatch.setenv("PKULAW_ACCESS_TOKEN", "current-token")
    monkeypatch.setenv("PKULAW_MCP_GATEWAY", "https://apim-gateway.pkulaw.com")

    client = PkulawMcpClient()

    assert client.access_token == "current-token"
    assert client.gateway == "https://apim-gateway.pkulaw.com"


class RoutingClient:
    def __init__(self, *, exact=None, semantic=None, laws=None):
        self.exact = exact
        self.semantic = [] if semantic is None else semantic
        self.laws = [] if laws is None else laws
        self.calls = []

    @staticmethod
    def _resolve(value):
        if isinstance(value, Exception):
            raise value
        return value

    def get_article(self, title, article_no):
        self.calls.append(("get_article", title, article_no))
        value = self._resolve(self.exact)
        if value is None:
            raise PkulawNotFoundError("未找到数据")
        return value

    def search_law_articles_for_article(self, title, article_no):
        self.calls.append(("semantic_exact", title, article_no))
        return self._resolve(self.semantic)

    def search_law_articles(self, text):
        self.calls.append(("semantic", text))
        return self._resolve(self.semantic)

    def get_law_list(self, title="", fulltext=""):
        self.calls.append(("law_list", title))
        return self._resolve(self.laws)


LAW = PkulawLawRecord(
    title="中华人民共和国民法典",
    timeliness=["现行有效"],
    effectiveness=["法律"],
    implement_date="2021-01-01",
)


def _article(
    article_no="第四十八条", text="保护当事人的合法民事权益。", title=LAW.title
):
    return PkulawArticle(title=title, article_no=article_no, article_text=text)


def _request(article_no="第四十八条", context="保护当事人的合法民事权益。"):
    return LookupRequest(
        law_title="民法典",
        source_type="law",
        article_no=article_no,
        context_text=context,
    )


def test_numbered_exact_hit_enriches_timeliness_and_records_route_order():
    client = RoutingClient(exact=_article(), laws=[LAW])
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.evidence.version_status == "现行有效"
    assert [
        (x["service"], x["status"]) for x in result.trace.metadata["route_attempts"]
    ] == [("law_search_get_article", "completed"), ("law_keyword", "completed")]
    assert (
        result.trace.metadata["route_attempts"][1]["purpose"] == "timeliness_enrichment"
    )


def test_location_candidate_query_returns_same_law_articles():
    client = RoutingClient(semantic=[
        _article("第二条", "第一款。\n第二款正确内容。"),
        _article("第三条", "其他内容。", title="其他法"),
    ])

    result = PkulawFallbackSource(client).locate_candidates(
        _request("第二条", "第二款正确内容。")
    )

    assert [candidate.article_no for candidate in result.candidates] == ["第二条"]
    assert result.trace.metadata["route_attempts"][0]["purpose"] == "citation_location"


def test_numbered_mismatched_exact_result_is_ignored_and_uses_semantic():
    client = RoutingClient(
        exact=_article(title="中华人民共和国刑法"), semantic=[_article()], laws=[LAW]
    )
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.trace.metadata["route_attempts"][0]["status"] == "mismatched"
    assert any(call[0] == "semantic_exact" for call in client.calls)


def test_numbered_exact_miss_semantic_same_number_is_article_found():
    client = RoutingClient(semantic=[_article()], laws=[LAW])
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND


def test_numbered_semantic_other_articles_are_ranked_and_limited():
    articles = [
        _article(f"第{i}条", "保护当事人的合法民事权益和财产权利。")
        for i in range(1, 6)
    ]
    result = PkulawFallbackSource(RoutingClient(semantic=articles, laws=[LAW])).lookup(
        _request()
    )
    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert 0 < len(result.evidence.related_articles) <= 3


def test_numbered_all_article_routes_miss_but_law_exists():
    result = PkulawFallbackSource(RoutingClient(laws=[LAW])).lookup(_request())
    assert result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING


def test_numbered_all_routes_miss_returns_candidates_and_completed_marker():
    candidate = PkulawLawRecord(title="中华人民共和国民法典总则编司法解释")
    result = PkulawFallbackSource(RoutingClient(laws=[candidate])).lookup(_request())
    assert result.status == LookupStatus.LAW_NOT_FOUND
    assert result.trace.metadata["search_completed"] is True
    assert result.trace.metadata["candidate_titles"] == [candidate.title]


def test_numbered_exact_network_error_does_not_degrade():
    client = RoutingClient(
        exact=PkulawMcpError("network"), semantic=[_article()], laws=[LAW]
    )
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.SOURCE_ERROR
    assert [call[0] for call in client.calls] == ["get_article"]


def test_numbered_semantic_error_cannot_be_reported_as_missing():
    client = RoutingClient(semantic=PkulawMcpError("semantic network"), laws=[LAW])
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.SOURCE_ERROR
    assert result.trace.metadata["route_attempts"][-2]["status"] == "error"


def test_unnumbered_bare_reference_skips_semantic_search():
    client = RoutingClient(laws=[LAW])
    request = LookupRequest(
        law_title="民法典", source_type="law", context_text="根据《民法典》规定"
    )
    result = PkulawFallbackSource(client).lookup(request)
    assert result.status == LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
    assert not any(call[0].startswith("semantic") for call in client.calls)


def test_unnumbered_substantive_reference_returns_ranked_articles_and_filters_titles():
    client = RoutingClient(
        laws=[LAW],
        semantic=[
            _article("第一条", "保护当事人的合法民事权益。"),
            _article(
                "第二条", "保护当事人的合法民事权益。", title="中华人民共和国刑法"
            ),
        ],
    )
    request = LookupRequest(
        law_title="民法典",
        source_type="law",
        context_text="应当保护当事人的合法民事权益和财产权利",
    )
    result = PkulawFallbackSource(client).lookup(request)
    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert [x.article_no for x in result.evidence.related_articles] == ["第一条"]


def test_unnumbered_semantic_article_without_number_has_no_blank_prefix():
    text = "保护当事人的合法民事权益和财产权利。"
    client = RoutingClient(laws=[LAW], semantic=[_article("", text)])
    request = LookupRequest(law_title="民法典", source_type="law", context_text=text)
    result = PkulawFallbackSource(client).lookup(request)
    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert result.evidence.article_text == text


@pytest.mark.parametrize(
    ("semantic", "expected"),
    [
        (
            [_article("第一条", "保护当事人的合法民事权益。")],
            LookupStatus.RELEVANT_ARTICLES_FOUND,
        ),
        ([], LookupStatus.LAW_NOT_FOUND),
    ],
)
def test_unnumbered_keyword_miss_semantic_outcomes(semantic, expected):
    request = LookupRequest(
        law_title="民法典",
        source_type="law",
        context_text="应当保护当事人的合法民事权益和财产权利",
    )
    result = PkulawFallbackSource(RoutingClient(semantic=semantic)).lookup(request)
    assert result.status == expected


def test_unnumbered_keyword_miss_semantic_error_is_source_error():
    request = LookupRequest(
        law_title="民法典",
        source_type="law",
        context_text="应当保护当事人的合法民事权益和财产权利",
    )
    result = PkulawFallbackSource(
        RoutingClient(semantic=PkulawMcpError("semantic network"))
    ).lookup(request)
    assert result.status == LookupStatus.SOURCE_ERROR
