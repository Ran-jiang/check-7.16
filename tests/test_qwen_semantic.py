import json

import httpx
import pytest

from ccitecheck.infrastructure import http as http_module
from ccitecheck.infrastructure.http import (
    HttpRequestError,
    RetryPolicy,
    post_json_with_retry,
)
from ccitecheck.domain.evidence import (
    ArticleEvidence,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ccitecheck.domain.checks import CheckVerdict
from ccitecheck.judgment.semantic import (
    DEFAULT_BASE_URL,
    PROMPT_PATH,
    QwenSemanticChecker,
    SemanticResponseError,
)


def test_prompt_scope_does_not_evaluate_legal_argument_or_conclusion():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "文书的法律论证是否成立" in prompt
    assert '"verdict": "bug"' not in prompt
    assert "上述五种错误类型之一" not in prompt
    assert "法律渊源不存在" not in prompt
    assert "条款编号或引用定位错误" not in prompt
    assert "引用内容与权威文本无实质对应" not in prompt
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

    assert result.verdict == CheckVerdict.PASS
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
    assert len(result.findings[0].summary) == 120


def test_qwen_revision_requires_backend_approval_protocol(monkeypatch):
    response = json.dumps({
        "verdict": "issue",
        "issues": [{
            "error_type": "曲解权威文本原意",
            "risk_level": "MEDIUM",
            "diff_summary": "文书写了无条件义务，原文要求满足前提",
            "suggestion": "补充适用前提。",
            "revised_text": "满足前提时，应当履行义务。",
        }],
        "notes": "",
    }, ensure_ascii=False)
    _install_mock_client(
        monkeypatch,
        lambda request: httpx.Response(200, json=_qwen_response(response)),
    )
    checker = QwenSemanticChecker(api_key="test-key")
    evidence = ArticleEvidence(
        law_title="示例法",
        source_type="law",
        article_no="第一条",
        article_text="满足前提时，应当履行义务。",
        data_source=SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="test",
            status=LookupStatus.ARTICLE_FOUND,
        ),
    )

    result = checker.compare("应当履行义务。", "上下文", "《示例法》第一条", evidence)

    revision = result.findings[0].revision
    assert revision is not None
    assert revision.strategy == "replace_exact_text"
    assert revision.original_text == "应当履行义务。"
    assert revision.machine_applicable is True


def test_qwen_revision_cannot_change_deterministically_verified_citation(monkeypatch):
    response = json.dumps({
        "verdict": "issue",
        "issues": [{
            "error_type": "曲解权威文本原意",
            "risk_level": "HIGH",
            "diff_summary": "文书遗漏法定前提",
            "suggestion": "补充前提。",
            "revised_text": "依据《示例法》第二条，满足前提时应当履行义务。",
        }],
        "notes": "",
    }, ensure_ascii=False)
    _install_mock_client(monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)))
    checker = QwenSemanticChecker(api_key="test-key")
    evidence = ArticleEvidence(
        law_title="示例法", source_type="law", article_no="第一条",
        article_text="满足前提时，应当履行义务。",
        data_source=SourceTrace(tier=SourceTier.LOCAL_SQLITE, source_name="test", status=LookupStatus.ARTICLE_FOUND),
    )
    result = checker.compare(
        "依据《示例法》第一条，应当履行义务。", "上下文", "《示例法》第一条", evidence
    )
    assert result.findings[0].revision is None


def test_qwen_location_recheck_is_explicitly_structured(monkeypatch):
    response = json.dumps({
        "verdict": "issue",
        "issues": [{
            "error_type": "曲解权威文本原意",
            "risk_level": "HIGH",
            "diff_summary": "文书讨论独立著作权，原文规定出版者赔偿责任，两者主题完全无关",
            "suggestion": "重新检索正确条款。",
            "location_recheck_required": True,
            "revised_text": None,
        }],
        "notes": "",
    }, ensure_ascii=False)
    _install_mock_client(monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)))
    checker = QwenSemanticChecker(api_key="test-key")
    evidence = ArticleEvidence(
        law_title="示例解释", source_type="judicial_interpretation", article_no="第二十条",
        article_text="出版者未尽合理注意义务的，应当承担赔偿责任。",
        data_source=SourceTrace(tier=SourceTier.LOCAL_SQLITE, source_name="test", status=LookupStatus.ARTICLE_FOUND),
    )
    result = checker.compare("不同作者独立创作的作品各自享有著作权。", "", "《示例解释》第二十条", evidence)
    assert result.findings[0].location_recheck_required is True


def test_qwen_malformed_json_is_repaired_once(monkeypatch):
    responses = iter([
        '{"verdict":"issue","issues":[{"error_type":"曲解权威文本原意" "risk_level":"HIGH"}]}',
        json.dumps({
            "verdict": "issue",
            "issues": [{
                "error_type": "曲解权威文本原意",
                "risk_level": "HIGH",
                "diff_summary": "文书写了无条件义务，原文规定了适用前提",
                "suggestion": "补充原文规定的适用前提。",
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

    assert result.verdict == CheckVerdict.ISSUE
    assert len(calls) == 2


@pytest.mark.parametrize("error_type", [
    "条款编号或引用定位错误",
    "引用内容与权威文本无实质对应",
])
def test_statute_llm_rejects_deterministic_error_types(monkeypatch, error_type):
    response = json.dumps({
        "verdict": "issue",
        "issues": [{
            "error_type": error_type,
            "risk_level": "HIGH",
            "diff_summary": "不应由模型判断",
            "suggestion": "不应由模型建议定位。",
        }],
        "notes": "",
    }, ensure_ascii=False)
    _install_mock_client(
        monkeypatch,
        lambda request: httpx.Response(200, json=_qwen_response(response)),
    )
    checker = QwenSemanticChecker(api_key="test-key")
    evidence = ArticleEvidence(
        law_title="某法",
        source_type="law",
        article_no="第一条",
        article_text="权威文本。",
        data_source=SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="test",
            status=LookupStatus.ARTICLE_FOUND,
        ),
    )

    with pytest.raises(SemanticResponseError, match="reserved for deterministic checks"):
        checker.compare("文书表述", "上下文", "《某法》第一条", evidence)


_REASONING_TEXT = "公司章程可以限制股权转让。该约定系公司自治的体现。该约定不违反公司法的禁止性规定。"


def _reasoning_response(assertions):
    return json.dumps(
        {"verdict": "issue", "assertions": assertions, "notes": ""},
        ensure_ascii=False,
    )


def test_case_distorted_with_valid_hits_returns_excerpt_from_source(monkeypatch):
    response = _reasoning_response([{
        "id": 1, "judgment": "distorted", "hit_sentence_ids": [1, 2],
        "risk_level": "HIGH",
        "diff_summary": "文书将章程限制股权转让扩张为股东失权。",
        "suggestion": "按说理原句改写为章程限制转让。",
    }])
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "法院认可章程对股东失权的安排。", _REASONING_TEXT, "某案",
    )

    assert check.verdict == CheckVerdict.ISSUE
    finding = check.findings[0]
    assert finding.code.value == "holding_distorted"
    assert finding.matched_excerpt == "……公司章程可以限制股权转让。该约定系公司自治的体现。……"


def test_case_distorted_with_fabricated_hits_downgrades_to_manual(monkeypatch):
    response = _reasoning_response([{
        "id": 1, "judgment": "distorted", "hit_sentence_ids": [99],
        "risk_level": "HIGH", "diff_summary": "编造的定位", "suggestion": "改写。",
    }])
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "法院认可章程对股东失权的安排。", _REASONING_TEXT, "某案",
    )

    finding = check.findings[0]
    assert finding.code.value == "holding_unsupported"
    assert finding.matched_excerpt is None
    assert "人工核对" in finding.suggestion


def test_case_unsupported_reports_missing_basis(monkeypatch):
    response = _reasoning_response([{
        "id": 1, "judgment": "unsupported", "hit_sentence_ids": [],
        "risk_level": "MEDIUM",
        "diff_summary": "说理只讨论股权转让限制，未涉及公司解散。",
        "suggestion": "删除该观点或核对案例来源。",
    }])
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "法院认为公司应当解散。", _REASONING_TEXT, "某案",
    )

    assert check.verdict == CheckVerdict.ISSUE
    assert check.findings[0].code.value == "holding_unsupported"


def test_case_unsupported_on_truncated_reasoning_defers_to_manual(monkeypatch):
    truncated_reasoning = "公司章程可以限制股权转让。损失赔偿额应当相当于因违约所造"
    response = _reasoning_response([{
        "id": 1, "judgment": "unsupported", "hit_sentence_ids": [],
        "risk_level": "MEDIUM", "diff_summary": "未讨论", "suggestion": "核对。",
    }])
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "法院认为公司应当解散。", truncated_reasoning, "某案",
    )

    assert check.verdict == CheckVerdict.INSUFFICIENT_INPUT
    assert check.findings == []
    assert "截断" in check.notes and "人工核对" in check.notes


def test_case_all_supported_passes(monkeypatch):
    response = json.dumps({
        "verdict": "pass",
        "assertions": [{"id": 1, "judgment": "supported", "hit_sentence_ids": [2]}],
        "notes": "",
    }, ensure_ascii=False)
    _install_mock_client(
        monkeypatch, lambda request: httpx.Response(200, json=_qwen_response(response)),
    )

    check = QwenSemanticChecker(api_key="test-key").compare_holding(
        "章程限制转让系公司自治。", _REASONING_TEXT, "某案",
    )

    assert check.verdict == CheckVerdict.PASS
    assert check.findings == []


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
    assert "确定性程序切分到准确的条、款或项" in prompt
    assert "未指明款号时，只引条文的部分款、项不构成问题" in prompt


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


def test_model_selection_switches_provider_and_protocol(monkeypatch):
    """三个可选模型：千问走 DashScope /responses，DeepSeek 走 /chat/completions。"""
    from ccitecheck.judgment.semantic import QwenSemanticChecker, resolve_model_option

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.delenv("LLM_DEFAULT_MODEL", raising=False)

    plus = QwenSemanticChecker.from_env("qwen3.7-plus")
    assert plus.provider == "dashscope" and plus.model == "qwen3.7-plus"

    mx = QwenSemanticChecker.from_env("qwen3.7-max")
    assert mx.provider == "dashscope" and mx.model == "qwen3.7-max"

    deepseek = QwenSemanticChecker.from_env("deepseek")
    assert deepseek.provider == "deepseek"
    assert deepseek.base_url == "https://api.deepseek.com"

    # 未知标识回退到第一个模型
    assert resolve_model_option("不存在的模型").key == "qwen3.7-plus"


def test_deepseek_uses_chat_completions_endpoint(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"verdict":"pass"}'}}]})

    _install_mock_client(monkeypatch, handler)
    checker = QwenSemanticChecker(api_key="k", model="deepseek-v4-pro",
                                  base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                                  provider="deepseek")
    text = checker._chat("系统提示", "用户内容")

    assert captured["url"].endswith("/chat/completions")
    assert "messages" in captured["body"] and "input" not in captured["body"]
    assert text == '{"verdict":"pass"}'
