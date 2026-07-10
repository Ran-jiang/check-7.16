import json

from verification.pkulaw_mcp import PkulawMcpClient, article_no_to_number


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
