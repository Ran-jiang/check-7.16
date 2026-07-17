"""使用千问将文书引用与已取得的法条证据做语义比较。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..infrastructure.config import load_project_env
from ..infrastructure.http import (
    HttpRequestError,
    HttpResponseJSONError,
    RetryPolicy,
    post_json_with_retry,
)

from ..domain.evidence import ArticleEvidence
from ..domain.result import SemanticComparison

DEFAULT_MODEL = "qwen3.7-plus"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "legal_semantic_comparison.md"
)
_LLM_ISSUE_TYPES = {
    "条款编号或引用定位错误",
    "曲解权威文本原意",
    "引用内容与权威文本无实质对应",
}

# 改引建议：从本地全文召回的候选条款中，判断哪一条（如有）真正支持文书表述。
# 仅在语义核查判"无实质对应/定位错误"且该法本地有全文时调用。
SUGGEST_PROMPT = (
    "你是法律引用核查系统的改引建议模块。输入包含文书表述 doc_quote 与若干候选条款"
    " candidates（来自同一部法规的全文召回，含 article_no 与 article_text）。"
    "任务：判断哪一条候选的内容与 doc_quote 的表述实质对应。只依据候选条款文本判断，"
    '禁止使用模型记忆补全。若有对应，输出 {"article_no": "第X条"}；'
    '若都不对应或拿不准，输出 {"article_no": null}。仅输出该 JSON 对象。'
)


class SemanticChecker(Protocol):
    def compare(
        self,
        doc_quote: str,
        quote_context: str,
        cited_source: str,
        evidence: ArticleEvidence,
    ) -> SemanticComparison: ...


class SemanticCheckError(RuntimeError):
    error_code = "semantic_error"


class SemanticTransportError(SemanticCheckError):
    def __init__(self, message: str, error_code: str = "transport_error"):
        super().__init__(message)
        self.error_code = error_code


class SemanticResponseError(SemanticCheckError):
    def __init__(self, message: str, error_code: str = "invalid_schema"):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class QwenSemanticChecker:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 60
    retry_budget_seconds: float = 90.0
    retry_max_attempts: int = 4

    @classmethod
    def from_env(cls, model: str | None = None) -> "QwenSemanticChecker":
        load_project_env()
        api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("LLM_API_KEY")
        if not api_key:
            raise SemanticCheckError(
                "DASHSCOPE_API_KEY is required for semantic checks"
            )
        return cls(
            api_key=api_key,
            model=model
            or os.getenv("QWEN_MODEL")
            or os.getenv("LLM_MODEL", DEFAULT_MODEL),
            base_url=(
                os.getenv("QWEN_BASE_URL")
                or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)
            ).rstrip("/"),
            timeout=int(os.getenv("QWEN_TIMEOUT_SECONDS", "60")),
            retry_budget_seconds=float(
                os.getenv("QWEN_RETRY_BUDGET_SECONDS", "90")
            ),
            retry_max_attempts=int(os.getenv("QWEN_RETRY_MAX_ATTEMPTS", "4")),
        )

    def compare(
        self,
        doc_quote: str,
        quote_context: str,
        cited_source: str,
        evidence: ArticleEvidence,
    ) -> SemanticComparison:
        if not evidence.article_text:
            raise SemanticCheckError("未取得法条原文，无法进行语义对比")

        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "doc_quote": doc_quote,
                            "quote_context": quote_context,
                            "cited_source": cited_source,
                            "statute_text": evidence.article_text,
                            "source_metadata": _source_metadata(evidence),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "enable_thinking": False,
        }
        data = self._post_response(payload)
        output_text = _extract_output_text(data)
        try:
            raw_comparison = _load_json_object(output_text)
        except (json.JSONDecodeError, ValueError):
            repair_payload = {
                "model": self.model,
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "将用户提供的内容修复为一个语义等价、可由 JSON.parse() 直接解析的"
                            "合法 JSON 对象。不得增删事实，不得输出 Markdown 或解释。"
                        ),
                    },
                    {"role": "user", "content": output_text},
                ],
                "enable_thinking": False,
            }
            repaired = _extract_output_text(self._post_response(repair_payload))
            try:
                raw_comparison = _load_json_object(repaired)
            except (json.JSONDecodeError, ValueError) as exc:
                raise SemanticResponseError(
                    f"Qwen returned invalid semantic JSON: {exc}"
                    , "invalid_json"
                ) from exc

        try:
            for issue in raw_comparison.get("issues", []):
                if not isinstance(issue, dict) or issue.get("error_type") not in _LLM_ISSUE_TYPES:
                    raise SemanticResponseError(
                        "Qwen returned an error_type reserved for deterministic checks",
                        "invalid_schema",
                    )
            return SemanticComparison.model_validate(raw_comparison)
        except SemanticResponseError:
            raise
        except ValueError as exc:
            raise SemanticResponseError(
                f"Qwen returned invalid semantic JSON: {exc}"
            ) from exc

    def suggest_article(
        self,
        doc_quote: str,
        candidates: list[dict[str, str]],
    ) -> str | None:
        """从候选条款中挑出真正支持文书表述的条号；没有则返回 None。"""
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": SUGGEST_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"doc_quote": doc_quote, "candidates": candidates},
                        ensure_ascii=False,
                    ),
                },
            ],
            "enable_thinking": False,
        }
        try:
            data = self._post_response(payload)
            output_text = _extract_output_text(data)
            result = _load_json_object(output_text)
            article_no = result.get("article_no")
            return article_no if isinstance(article_no, str) and article_no else None
        except (SemanticCheckError, ValueError, json.JSONDecodeError):
            # 改引建议是增强信息，失败时静默降级，不影响主结论
            return None

    def _post_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return post_json_with_retry(
                f"{self.base_url}/responses",
                payload,
                {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                },
                policy=RetryPolicy(
                    max_attempts=self.retry_max_attempts,
                    budget_seconds=self.retry_budget_seconds,
                    read_timeout=float(self.timeout),
                ),
            )
        except HttpRequestError as exc:
            raise SemanticTransportError(
                f"Qwen API request failed: {exc}", exc.error_code
            ) from exc
        except HttpResponseJSONError as exc:
            raise SemanticResponseError(
                f"Qwen returned invalid response JSON: {exc}", "invalid_json"
            ) from exc


def _source_metadata(evidence: ArticleEvidence) -> dict[str, Any]:
    return {
        "law_name": evidence.law_title,
        "article": evidence.article_no,
        "retrieved_articles": [
            item.model_dump(mode="json") for item in evidence.related_articles
        ],
    }


def _load_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = (
            value.removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("semantic response must be a JSON object")
    return parsed


def _extract_output_text(data: dict[str, Any]) -> str:
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"]
    raise SemanticResponseError(
        "Qwen response did not include output_text", "invalid_schema"
    )
