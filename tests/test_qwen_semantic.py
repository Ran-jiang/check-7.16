import json

from verification.schema import (
    ArticleEvidence,
    ComparisonVerdict,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from verification.semantic import DEFAULT_BASE_URL, PROMPT_PATH, QwenSemanticChecker


def test_prompt_scope_does_not_evaluate_legal_argument_or_conclusion():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "不评价法律论证是否成立" in prompt
    assert "不评价文书结论在法律上是否成立" in prompt
    assert "结论是否必然成立" not in prompt


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(
            {
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": '{"verdict":"pass"}'}
                        ]
                    }
                ]
            }
        ).encode()


def test_qwen_request_uses_beijing_responses_api_without_thinking(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout, context=None):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        return FakeResponse()

    def fake_open(self, request, timeout=None):
        return fake_urlopen(request, timeout=timeout)

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)
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
