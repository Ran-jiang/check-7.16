"""Qwen-powered semantic comparison against retrieved statute evidence."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from runtime_env import load_project_env

from .pkulaw_mcp import default_ssl_context
from .schema import ArticleEvidence, SemanticComparison

DEFAULT_MODEL = "qwen3.7-plus"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "legal_semantic_comparison.md"

# 改引建议：从本地全文召回的候选条款中，判断哪一条（如有）真正支持文书表述。
# 仅在语义核查判"无实质对应/定位错误"且该法本地有全文时调用。
SUGGEST_PROMPT = (
    "你是法律引用核查系统的改引建议模块。输入包含文书表述 doc_quote 与若干候选条款"
    " candidates（来自同一部法规的全文召回，含 article_no 与 article_text）。"
    "任务：判断哪一条候选的内容与 doc_quote 的表述实质对应。只依据候选条款文本判断，"
    "禁止使用模型记忆补全。若有对应，输出 {\"article_no\": \"第X条\"}；"
    "若都不对应或拿不准，输出 {\"article_no\": null}。仅输出该 JSON 对象。"
)


class SemanticChecker(Protocol):
    def compare(
        self,
        doc_quote: str,
        quote_context: str,
        cited_source: str,
        evidence: ArticleEvidence,
    ) -> SemanticComparison:
        ...


class SemanticCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class QwenSemanticChecker:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 60

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
            model=model or os.getenv("QWEN_MODEL") or os.getenv("LLM_MODEL", DEFAULT_MODEL),
            base_url=(
                os.getenv("QWEN_BASE_URL")
                or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)
            ).rstrip("/"),
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
            raw_comparison = json.loads(output_text)
            for issue in raw_comparison.get("issues", []):
                if isinstance(issue, dict) and isinstance(issue.get("diff_summary"), str):
                    issue["diff_summary"] = issue["diff_summary"][:80]
            return SemanticComparison.model_validate(raw_comparison)
        except ValueError as exc:
            raise SemanticCheckError(f"Qwen returned invalid semantic JSON: {exc}") from exc

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
            result = json.loads(output_text)
            article_no = result.get("article_no")
            return article_no if isinstance(article_no, str) and article_no else None
        except (SemanticCheckError, ValueError):
            # 改引建议是增强信息，失败时静默降级，不影响主结论
            return None

    def _post_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        # DashScope 是国内端点：本机代理会掐断其 TLS，必须绕过代理直连
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=default_ssl_context()),
        )
        last_error: Exception | None = None
        for attempt in range(3):
            if attempt:
                time.sleep(0.5 * (2 ** (attempt - 1)))
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = SemanticCheckError(f"Qwen API HTTP {exc.code}: {detail[:300]}")
                if exc.code < 500:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = SemanticCheckError(f"Qwen API request failed: {reason}")
        raise last_error or SemanticCheckError("Qwen API request failed")


def _source_metadata(evidence: ArticleEvidence) -> dict[str, Any]:
    return {
        "law_name": evidence.law_title,
        "article": evidence.article_no,
        "version_label": evidence.version_label,
        "version_status": evidence.version_status,
        "effective_from": evidence.effective_from,
        "effective_to": evidence.effective_to,
        "retrieved_articles": [
            item.model_dump(mode="json") for item in evidence.related_articles
        ],
        **evidence.source_metadata,
    }


def _extract_output_text(data: dict[str, Any]) -> str:
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"]
    raise SemanticCheckError("Qwen response did not include output_text")
