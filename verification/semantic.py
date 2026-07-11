"""Qwen-powered semantic comparison against retrieved statute evidence."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from runtime_env import load_project_env

from .pkulaw_mcp import default_ssl_context
from .schema import ArticleEvidence, SemanticComparison

DEFAULT_MODEL = "qwen3.7-max"
DEFAULT_BASE_URL = (
    "https://llm-qs6teo3293en0sk8.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "legal_semantic_comparison.md"


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
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise SemanticCheckError("DASHSCOPE_API_KEY is required for semantic checks")
        return cls(
            api_key=api_key,
            model=model or os.getenv("QWEN_MODEL", DEFAULT_MODEL),
            base_url=os.getenv("QWEN_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
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
            return SemanticComparison.model_validate_json(output_text)
        except ValueError as exc:
            raise SemanticCheckError(f"Qwen returned invalid semantic JSON: {exc}") from exc

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
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=default_ssl_context()
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SemanticCheckError(f"Qwen API HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise SemanticCheckError(f"Qwen API request failed: {exc.reason}") from exc


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
