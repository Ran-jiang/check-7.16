import json

import pytest

from verification.pkulaw_mcp import (
    MCP_ENDPOINTS,
    PkulawMcpClient,
    article_no_to_number,
)


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


def test_get_law_item_content_parses_full_text():
    client = FakePkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": {
                    "Title": "中华人民共和国民法典",
                    "FullText": "第五百七十七条　当事人一方不履行合同义务。",
                    "Url": "[北大法宝](https://example.com#tiao_577.0)",
                    "IssueDate": "2020.05.28",
                    "ImplementDate": "2021.01.01",
                    "TimelinessDic": ["现行有效"],
                    "EffectivenessDic": ["法律"],
                },
            }
        )
    )

    article = client.get_law_item_content("民法典", "第五百七十七条")

    assert article.title == "中华人民共和国民法典"
    assert article.article_no == "第五百七十七条"
    assert "不履行合同义务" in article.article_text
    assert article.timeliness == ["现行有效"]


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


def test_recognize_case_numbers_parses_anhaoname_array():
    client = FakePkulawClient(
        _mcp_text_payload(
            {
                "anhaoname": [
                    {
                        "text": "（2024）浙0114破1-6号之二",
                        "start": 20,
                        "end": 38,
                        "gid": "08df102e7c10f206b6d395298deef3e4750099a86672ec1ebdfb",
                        "caseFlag": "（2024）浙0114破1-6号之二",
                        "court": "浙江省杭州市钱塘区人民法院",
                        "title": "指导性案例252号：浙江某新材料股份有限公司系列执行实施案",
                        "lastInstanceDate": "2024.06.18",
                        "url": "https://www.pkulaw.com/pfnl/08df102e7c10f206.html",
                    }
                ]
            }
        )
    )

    cases = client.recognize_case_numbers("……（2024）浙0114破1-6号之二……")

    assert len(cases) == 1
    assert cases[0].case_flag == "（2024）浙0114破1-6号之二"
    assert cases[0].court == "浙江省杭州市钱塘区人民法院"
    assert cases[0].gid.startswith("08df102e")


def test_article_no_to_number_supports_chinese_and_zhi_suffix():
    assert article_no_to_number("第五百七十七条") == 577
    assert article_no_to_number("第二条之一") == 2.1


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
            {"title": "民法典", "fulltext": "违约责任"},
        ),
        (
            "get_case_list",
            ("指导案例262号", "平台 责任"),
            MCP_ENDPOINTS["case_keyword"],
            "get_case_list",
            {"title": "指导案例262号", "fulltext": "平台 责任"},
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


def test_legacy_url_and_headers_configuration_is_supported(monkeypatch):
    monkeypatch.delenv("PKULAW_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("PKULAW_MCP_GATEWAY", raising=False)
    monkeypatch.setenv(
        "PKULAW_MCP_URL", "https://apim-gateway.pkulaw.com/mcp-fatiao"
    )
    monkeypatch.setenv(
        "PKULAW_MCP_HEADERS",
        json.dumps({"Authorization": "Bearer legacy-token", "X-Test": "value"}),
    )

    client = PkulawMcpClient()

    assert client.access_token == "legacy-token"
    assert client.gateway == "https://apim-gateway.pkulaw.com"
    assert client.headers == {"X-Test": "value"}
