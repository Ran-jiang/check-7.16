import json

import httpx
import pytest

from ccitecheck.infrastructure import http as http_module
from ccitecheck.infrastructure.http import (
    HttpRequestError,
    RetryPolicy,
    post_json_with_retry,
)
from ccitecheck.domain.result import (
    ArticleEvidence,
    ComparisonVerdict,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ccitecheck.judgment.semantic import DEFAULT_BASE_URL, PROMPT_PATH, QwenSemanticChecker


def test_prompt_scope_does_not_evaluate_legal_argument_or_conclusion():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "文书的法律论证是否成立" in prompt
    assert '"verdict": "bug"' not in prompt
    assert "上述五种错误类型之一" not in prompt
    assert "法律渊源不存在" not in prompt
    assert "结论是否必然成立" not in prompt


def _install_mock_client(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_module, "_HTTP_CLIENT", client)
    return client


def _qwen_response(text='{"verdict":"pass"}'):
    return {
        "output": [{"content": [{"type": "output_text", "text": text}]}]
    }


def test_qwen_request_uses_beijing_responses_api_without_thinking(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_qwen_response())

    _install_mock_client(monkeypatch, handler)
    checker = QwenSemanticChecker(api_key="test-key")
    evidence = ArticleEvidence(
        law_title="中华人民共和国民法典",
        source_type="law",
        article_no="第五百七十七条",
        article_text="当事人一方不履行合同义务的，应当承担违约责任。",
        data_source=SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="test",
            status=LookupStatus.ARTICLE_FOUND,
        ),
    )
    result = checker.compare(
        "被告应当承担违约责任。",
        "依据《民法典》第五百七十七条，被告应当承担违约责任。",
        "《民法典》第五百七十七条",
        evidence,
    )

    assert result.verdict == ComparisonVerdict.PASS
    assert captured["url"] == f"{DEFAULT_BASE_URL}/responses"
    assert captured["payload"]["model"] == "qwen3.7-plus"
    assert captured["payload"]["enable_thinking"] is False


def test_qwen_diff_summary_is_preserved_for_display_layer(monkeypatch):
    payload = {
        "output": [{"content": [{"type": "output_text", "text": json.dumps({
            "verdict": "issue",
            "issues": [{
                "error_type": "曲解权威文本原意",
                "risk_level": "HIGH",
                "diff_summary": "差" * 120,
                "suggestion": "请按原文修改。",
                "auto_fixable": False,
            }],
        }, ensure_ascii=False)}]}]
    }

    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=payload)
    )
    checker = QwenSemanticChecker(api_key="test-key")
    evidence = ArticleEvidence(
        law_title="网络数据安全管理条例",
        source_type="administrative_regulation",
        article_no="第十八条",
        article_text="不得干扰网络服务正常运行。",
        data_source=SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="test",
            status=LookupStatus.ARTICLE_FOUND,
        ),
    )
    result = checker.compare("文书表述", "上下文", "《网络数据安全管理条例》第十八条", evidence)
    assert len(result.issues[0].diff_summary) == 120


def test_qwen_malformed_json_is_repaired_once(monkeypatch):
    responses = iter([
        '{"verdict":"issue","issues":[{"error_type":"条款编号或引用定位错误" "risk_level":"HIGH"}]}',
        json.dumps({
            "verdict": "issue",
            "issues": [{
                "error_type": "条款编号或引用定位错误",
                "risk_level": "HIGH",
                "diff_summary": "文书所述内容与现行第二十七条不对应",
                "suggestion": "核对现行法条编号。",
                "auto_fixable": False,
            }],
            "notes": "",
        }, ensure_ascii=False),
    ])

    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_qwen_response(next(responses)))

    _install_mock_client(monkeypatch, handler)
    checker = QwenSemanticChecker(api_key="test-key")
    evidence = ArticleEvidence(
        law_title="中华人民共和国网络安全法",
        source_type="law",
        article_no="第二十七条",
        article_text="网络运营者应当制定网络安全事件应急预案。",
        data_source=SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="test",
            status=LookupStatus.ARTICLE_FOUND,
        ),
    )

    result = checker.compare("任何个人不得非法侵入网络。", "上下文", "《网络安全法》第二十七条", evidence)

    assert result.verdict == ComparisonVerdict.ISSUE
    assert len(calls) == 2


def test_retry_after_429_then_success(monkeypatch):
    calls = 0
    sleeps = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"ok": True})

    _install_mock_client(monkeypatch, handler)
    result = post_json_with_retry(
        "https://example.test/responses",
        {},
        {},
        policy=RetryPolicy(),
        sleep=sleeps.append,
    )
    assert result == {"ok": True}
    assert calls == 2
    assert sleeps == [3.0]


def test_two_connect_errors_then_success(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("broken", request=request)
        return httpx.Response(200, json={"ok": True})

    _install_mock_client(monkeypatch, handler)
    result = post_json_with_retry(
        "https://example.test/responses",
        {},
        {},
        policy=RetryPolicy(),
        sleep=lambda delay: None,
    )
    assert result == {"ok": True}
    assert calls == 3


def test_http_400_is_not_retried(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(HttpRequestError) as caught:
        post_json_with_retry(
            "https://example.test/responses", {}, {}, policy=RetryPolicy()
        )
    assert caught.value.error_code == "http_4xx"
    assert calls == 1


def test_retry_after_beyond_budget_fails_without_sleep(monkeypatch):
    sleeps = []
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(HttpRequestError) as caught:
        post_json_with_retry(
            "https://example.test/responses",
            {},
            {},
            policy=RetryPolicy(budget_seconds=90),
            sleep=sleeps.append,
        )
    assert caught.value.error_code == "rate_limited"
    assert calls == 1
    assert sleeps == []


def test_retry_budget_stops_attempts_without_exceeding_deadline(monkeypatch):
    now = [0.0]
    calls = 0

    def clock():
        return now[0]

    def handler(request):
        nonlocal calls
        calls += 1
        now[0] += 43.0
        raise httpx.ReadTimeout("slow upstream", request=request)

    _install_mock_client(monkeypatch, handler)
    with pytest.raises(HttpRequestError) as caught:
        post_json_with_retry(
            "https://example.test/responses",
            {},
            {},
            policy=RetryPolicy(budget_seconds=90, max_attempts=4),
            sleep=lambda delay: now.__setitem__(0, now[0] + delay),
            clock=clock,
        )
    assert caught.value.error_code == "timeout"
    assert calls == 2
    assert now[0] <= 90.0


def test_prompt_contains_paragraph_level_instructions():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "target_paragraph" in prompt
    assert "款级定位核对" in prompt
    assert "未指明款号时，只引条文的部分款、项不构成问题" in prompt


def test_qwen_payload_includes_target_paragraph_when_cited(monkeypatch):
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_qwen_response())

    _install_mock_client(monkeypatch, handler)
    checker = QwenSemanticChecker(api_key="test-key")
    evidence = ArticleEvidence(
        law_title="中华人民共和国专利法",
        source_type="law",
        article_no="第九条",
        article_text=(
            "同样的发明创造只能授予一项专利权。\n"
            "两个以上的申请人分别就同样的发明创造申请专利的，专利权授予最先申请的人。"
        ),
        data_source=SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="test",
            status=LookupStatus.ARTICLE_FOUND,
        ),
    )
    checker.compare(
        "中国专利权取得采取申请在先原则。",
        "上下文",
        "《专利法》第九条第一款",
        evidence,
        paragraphs=["第一款"],
    )

    user_content = json.loads(captured["payload"]["input"][1]["content"])
    target = user_content["target_paragraph"]
    assert target["cited"] == "第一款"
    assert target["number"] == 1
    assert target["total_paragraphs"] == 2
    assert target["text"].startswith("同样的发明创造只能授予一项专利权")


def test_qwen_payload_omits_target_paragraph_when_not_cited(monkeypatch):
    captured = {}

    def handler(request):
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_qwen_response())

    _install_mock_client(monkeypatch, handler)
    checker = QwenSemanticChecker(api_key="test-key")
    evidence = ArticleEvidence(
        law_title="中华人民共和国专利法",
        source_type="law",
        article_no="第九条",
        article_text="同样的发明创造只能授予一项专利权。",
        data_source=SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="test",
            status=LookupStatus.ARTICLE_FOUND,
        ),
    )
    checker.compare("文书表述", "上下文", "《专利法》第九条", evidence)

    user_content = json.loads(captured["payload"]["input"][1]["content"])
    assert "target_paragraph" not in user_content
