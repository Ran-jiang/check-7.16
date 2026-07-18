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
from ..domain.case_results import CaseErrorCode, CaseFinding, CaseHoldingCheck
from ..domain.checks import CheckVerdict
from ..domain.revisions import RevisionProposal
from ..domain.statute_results import (
    StatuteErrorCode,
    StatuteFinding,
    StatuteMeaningCheck,
)
from .markers import strip_internal_markers
from .paragraphs import resolve_paragraph

DEFAULT_MODEL = "qwen3.7-plus"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "statute_meaning_check.md"
)
HOLDING_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "case_holding_comparison.md"
_LLM_ISSUE_TYPES = {
    "曲解权威文本原意",
}


class SemanticChecker(Protocol):
    def compare(
        self,
        doc_quote: str,
        quote_context: str,
        cited_source: str,
        evidence: ArticleEvidence,
        paragraphs: list[str] | None = None,
    ) -> StatuteMeaningCheck: ...


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
        paragraphs: list[str] | None = None,
    ) -> StatuteMeaningCheck:
        if not evidence.article_text:
            raise SemanticCheckError("未取得法条原文，无法进行语义对比")

        user_input = {
            "doc_quote": doc_quote,
            "quote_context": quote_context,
            "cited_source": cited_source,
            "statute_text": evidence.article_text,
            "source_metadata": _source_metadata(evidence),
        }
        if paragraphs:
            target = resolve_paragraph(paragraphs[0], evidence.article_text)
            if target is not None:
                user_input["target_paragraph"] = {
                    "cited": paragraphs[0],
                    "number": target.number,
                    "total_paragraphs": target.total,
                    "text": target.text,
                }
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
                {
                    "role": "user",
                    "content": json.dumps(
                        user_input,
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
            raw_comparison["notes"] = strip_internal_markers(str(raw_comparison.get("notes", "")))
            for issue in raw_comparison.get("issues", []):
                if not isinstance(issue, dict) or issue.get("error_type") not in _LLM_ISSUE_TYPES:
                    raise SemanticResponseError(
                        "Qwen returned an error_type reserved for deterministic checks",
                        "invalid_schema",
                    )
                for field in ("diff_summary", "suggestion", "revised_text"):
                    if isinstance(issue.get(field), str):
                        issue[field] = strip_internal_markers(issue[field])
                if issue.get("revised_text") == doc_quote:
                    issue["revised_text"] = None
            comparison = _statute_check_from_raw(raw_comparison)
            _approve_statute_revisions(comparison, raw_comparison, doc_quote)
            return comparison
        except SemanticResponseError:
            raise
        except ValueError as exc:
            raise SemanticResponseError(
                f"Qwen returned invalid semantic JSON: {exc}"
            ) from exc

    def compare_holding(self, paraphrase_text: str, holding_text: str, case_title: str) -> CaseHoldingCheck:
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": HOLDING_PROMPT_PATH.read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps({
                    "paraphrase_text": paraphrase_text,
                    "case_title": case_title,
                    "authoritative_holding": holding_text,
                }, ensure_ascii=False)},
            ],
            "enable_thinking": False,
        }
        raw = _load_json_object(_extract_output_text(self._post_response(payload)))
        raw["notes"] = strip_internal_markers(str(raw.get("notes", "")))
        for issue in raw.get("issues", []):
            if not isinstance(issue, dict) or issue.get("error_type") != "所述观点非该案裁判观点":
                raise SemanticResponseError("Qwen returned an invalid case holding error_type")
            for field in ("diff_summary", "suggestion"):
                if isinstance(issue.get(field), str):
                    issue[field] = strip_internal_markers(issue[field])
        try:
            return _case_check_from_raw(raw)
        except ValueError as exc:
            raise SemanticResponseError(f"Qwen returned invalid case holding JSON: {exc}") from exc

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


def _approve_statute_revisions(
    comparison: StatuteMeaningCheck,
    raw: dict[str, Any],
    doc_quote: str,
) -> None:
    """批准仅替换本次精确核查片段的法规语义修订。"""
    for issue, raw_issue in zip(comparison.findings, raw.get("issues", [])):
        proposed_text = raw_issue.get("revised_text")
        if not proposed_text or proposed_text == doc_quote:
            continue
        issue.revision = RevisionProposal(
            strategy="replace_exact_text",
            original_text=doc_quote,
            revised_text=proposed_text,
            rationale=issue.suggestion,
            machine_applicable=True,
            preconditions=["original_text_unique", "document_unchanged"],
        )


def _statute_check_from_raw(raw: dict[str, Any]) -> StatuteMeaningCheck:
    findings = []
    for issue in raw.get("issues", []):
        finding = StatuteFinding(
            code=StatuteErrorCode.MEANING_DISTORTED,
            risk_level=issue["risk_level"],
            summary=issue["diff_summary"],
            suggestion=issue["suggestion"],
        )
        findings.append(finding)
    return StatuteMeaningCheck(
        verdict=CheckVerdict(raw["verdict"]),
        findings=findings,
        notes=raw.get("notes", ""),
    )


def _case_check_from_raw(raw: dict[str, Any]) -> CaseHoldingCheck:
    return CaseHoldingCheck(
        verdict=CheckVerdict(raw["verdict"]),
        findings=[
            CaseFinding(
                code=CaseErrorCode.HOLDING_NOT_IN_CASE,
                risk_level=issue["risk_level"],
                summary=issue["diff_summary"],
                suggestion=issue["suggestion"],
            )
            for issue in raw.get("issues", [])
        ],
        notes=raw.get("notes", ""),
    )


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
