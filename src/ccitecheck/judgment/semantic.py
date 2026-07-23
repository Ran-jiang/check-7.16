"""使用千问将文书引用与已取得的法条证据做语义比较。"""

from __future__ import annotations

import json
import os
import re
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
    NestedReferenceMatch,
    StatuteErrorCode,
    StatuteFinding,
    StatuteMeaningCheck,
)
from .markers import strip_internal_markers
from .reasoning import (
    build_excerpt,
    clean_reasoning_text,
    reasoning_is_truncated,
    split_reasoning_sentences,
)

DEFAULT_MODEL = "qwen3.7-plus"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "statute_meaning_check.md"
)
REASONING_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "case_reasoning_check.md"
REPROPOSAL_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "statute_locator_reproposal.md"
NESTED_REFERENCE_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "nested_reference_match.md"
_ARTICLE_NO_FORMAT = re.compile(r"第[零一二两三四五六七八九十百千0-9]+条")
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
    ) -> StatuteMeaningCheck: ...

    def compare_nested_reference(
        self,
        *,
        parent_source: str,
        parent_text: str,
        child_source: str,
        child_text: str,
    ) -> NestedReferenceMatch: ...


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
class ModelOption:
    """可选模型：同一套提示词，按厂商协议差异发送请求。"""

    key: str          # 前端与 API 使用的标识
    label: str        # 展示名
    provider: str     # dashscope（通义千问 /responses）| deepseek（DeepSeek 官方 /chat/completions）
    env_model: str    # 读取实际 model id 的环境变量
    default_model: str
    env_keys: tuple[str, ...]  # 该模型的密钥环境变量，按优先级


SUPPORTED_MODELS: tuple[ModelOption, ...] = (
    ModelOption("qwen3.7-plus", "通义千问 3.7 Plus", "dashscope", "QWEN_MODEL", "qwen3.7-plus",
                ("QWEN_PLUS_API_KEY", "DASHSCOPE_API_KEY", "LLM_API_KEY")),
    ModelOption("qwen3.7-max", "通义千问 3.7 Max", "dashscope", "QWEN_MAX_MODEL", "qwen3.7-max",
                ("QWEN_MAX_API_KEY", "DASHSCOPE_API_KEY", "LLM_API_KEY")),
    ModelOption("deepseek", "DeepSeek V4 Pro", "deepseek", "DEEPSEEK_MODEL", "deepseek-v4-pro",
                ("DEEPSEEK_API_KEY",)),
)


def model_api_key(option: "ModelOption") -> str | None:
    """按优先级取该模型的密钥：专用 key 优先，未设则回退共用 key。"""
    for name in option.env_keys:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def resolve_model_option(key: str | None) -> ModelOption:
    """按标识取模型；未指定时用 LLM_DEFAULT_MODEL，再退回第一个。"""
    wanted = (key or os.getenv("LLM_DEFAULT_MODEL") or "").strip()
    for option in SUPPORTED_MODELS:
        if option.key == wanted:
            return option
    if wanted:
        # 兼容直接传实际 model id（如 CLI 历史用法）
        for option in SUPPORTED_MODELS:
            if os.getenv(option.env_model, option.default_model) == wanted:
                return option
    return SUPPORTED_MODELS[0]


@dataclass(frozen=True)
class QwenSemanticChecker:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout: int = 60
    retry_budget_seconds: float = 90.0
    retry_max_attempts: int = 4
    provider: str = "dashscope"

    @classmethod
    def from_env(cls, model: str | None = None) -> "QwenSemanticChecker":
        load_project_env()
        option = resolve_model_option(model)
        api_key = model_api_key(option)
        if not api_key:
            raise SemanticCheckError(
                f"{option.env_keys[0]} is required for semantic checks with {option.label}"
            )
        if option.provider == "deepseek":
            base_url = (
                os.getenv("DEEPSEEK_BASE_URL")
                or os.getenv("QWEN_BASE_URL")
                or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)
            )
        else:
            base_url = (
                os.getenv("QWEN_BASE_URL")
                or os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL)
            )
        return cls(
            api_key=api_key,
            model=os.getenv(option.env_model, option.default_model),
            base_url=base_url.rstrip("/"),
            timeout=int(os.getenv("QWEN_TIMEOUT_SECONDS", "60")),
            retry_budget_seconds=float(
                os.getenv("QWEN_RETRY_BUDGET_SECONDS", "90")
            ),
            retry_max_attempts=int(os.getenv("QWEN_RETRY_MAX_ATTEMPTS", "4")),
            provider=option.provider,
        )

    def _chat(self, system_prompt: str, user_content: str) -> str:
        """按厂商协议发起一次对话并返回模型输出文本。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        if self.provider == "dashscope":
            return _extract_output_text(
                self._post("/responses", {
                    "model": self.model, "input": messages, "enable_thinking": False,
                })
            )
        return _extract_chat_text(
            self._post("/chat/completions", {"model": self.model, "messages": messages})
        )

    def compare(
        self,
        doc_quote: str,
        quote_context: str,
        cited_source: str,
        evidence: ArticleEvidence,
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
        output_text = self._chat(
            PROMPT_PATH.read_text(encoding="utf-8"),
            json.dumps(user_input, ensure_ascii=False),
        )
        try:
            raw_comparison = _load_json_object(output_text)
        except (json.JSONDecodeError, ValueError):
            repaired = self._chat(
                "将用户提供的内容修复为一个语义等价、可由 JSON.parse() 直接解析的"
                "合法 JSON 对象。不得增删事实，不得输出 Markdown 或解释。",
                output_text,
            )
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
        assertions = split_reasoning_sentences(paraphrase_text) or [clean_reasoning_text(paraphrase_text)]
        sentences = split_reasoning_sentences(holding_text)
        truncated = reasoning_is_truncated(sentences)
        raw_text = self._chat(
            REASONING_PROMPT_PATH.read_text(encoding="utf-8"),
            json.dumps({
                    "case_title": case_title,
                    "assertions": [
                        {"id": index, "text": text}
                        for index, text in enumerate(assertions, start=1)
                    ],
                    "reasoning_sentences": [
                        {"id": index, "text": text}
                        for index, text in enumerate(sentences, start=1)
                    ],
                "reasoning_truncated": truncated,
            }, ensure_ascii=False),
        )
        raw = _load_json_object(raw_text)
        try:
            return _case_reasoning_check_from_raw(raw, assertions, sentences, truncated)
        except (ValueError, TypeError) as exc:
            raise SemanticResponseError(f"Qwen returned invalid case reasoning JSON: {exc}") from exc

    def propose_locator_candidate(
        self,
        *,
        law_title: str,
        document_quote: str,
        cited_article_no: str,
        cited_article_text: str,
        tried: list[dict[str, str]],
    ) -> str | None:
        """已验证候选均不匹配后，携带验证反馈向模型索取下一个候选条号。"""
        raw_text = self._chat(
            REPROPOSAL_PROMPT_PATH.read_text(encoding="utf-8"),
            json.dumps({
                    "law_title": law_title,
                    "document_quote": document_quote,
                    "cited_article": {
                        "article_no": cited_article_no,
                        "article_text": cited_article_text,
                    },
                "tried_candidates": tried,
            }, ensure_ascii=False),
        )
        raw = _load_json_object(raw_text)
        return _valid_article_no(raw.get("candidate_article_no"))

    def compare_nested_reference(
        self,
        *,
        parent_source: str,
        parent_text: str,
        child_source: str,
        child_text: str,
    ) -> NestedReferenceMatch:
        """只判断 child 是否为 parent 权威条文实际转引的规则。"""
        raw = _load_json_object(self._chat(
            NESTED_REFERENCE_PROMPT_PATH.read_text(encoding="utf-8"),
            json.dumps({
                "parent_source": parent_source,
                "parent_text": parent_text,
                "child_source": child_source,
                "child_text": child_text,
            }, ensure_ascii=False),
        ))
        try:
            return NestedReferenceMatch(
                verdict=str(raw.get("verdict", "")),
                matched_locator=_valid_article_no(raw.get("matched_locator")),
                reason=strip_internal_markers(str(raw.get("reason", ""))),
            )
        except ValueError as exc:
            raise SemanticResponseError(
                f"Qwen returned invalid nested-reference JSON: {exc}"
            ) from exc

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return post_json_with_retry(
                f"{self.base_url}{path}",
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
                f"LLM API request failed: {exc}", exc.error_code
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
        if not _safe_llm_revision(doc_quote, proposed_text):
            continue
        issue.revision = RevisionProposal(
            strategy="replace_exact_text",
            original_text=doc_quote,
            revised_text=proposed_text,
            rationale=issue.suggestion,
            machine_applicable=True,
            preconditions=["original_text_unique", "document_unchanged"],
        )


def _risk_level(issue: dict[str, Any]) -> str:
    """模型偶尔返回小写风险等级，统一大写后再交给领域模型校验。"""
    return str(issue.get("risk_level", "")).strip().upper()


def _valid_article_no(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if _ARTICLE_NO_FORMAT.fullmatch(stripped) else None


def _statute_check_from_raw(raw: dict[str, Any]) -> StatuteMeaningCheck:
    findings = []
    for issue in raw.get("issues", []):
        finding = StatuteFinding(
            code=StatuteErrorCode.MEANING_DISTORTED,
            risk_level=_risk_level(issue),
            summary=issue["diff_summary"],
            suggestion=issue["suggestion"],
            location_recheck_required=bool(issue.get("location_recheck_required", False)),
            candidate_article_no=_valid_article_no(issue.get("candidate_article_no")),
        )
        findings.append(finding)
    return StatuteMeaningCheck(
        verdict=CheckVerdict(raw["verdict"]),
        findings=findings,
        notes=raw.get("notes", ""),
    )


def _case_reasoning_check_from_raw(
    raw: dict[str, Any],
    assertions: list[str],
    sentences: list[str],
    truncated: bool,
) -> CaseHoldingCheck:
    """按定位校验结果分流两类错误；模型给的是材料，分支由程序决定。"""
    if raw.get("verdict") == CheckVerdict.INSUFFICIENT_INPUT.value:
        return CaseHoldingCheck(
            verdict=CheckVerdict.INSUFFICIENT_INPUT,
            notes=strip_internal_markers(str(raw.get("notes", ""))),
        )
    items = raw.get("assertions")
    if not isinstance(items, list):
        raise ValueError("missing assertions list")
    findings: list[CaseFinding] = []
    truncation_notes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("assertion item is not an object")
        assertion_id = item.get("id")
        if not isinstance(assertion_id, int) or not 1 <= assertion_id <= len(assertions):
            raise ValueError(f"assertion id {assertion_id!r} out of range")
        assertion_text = assertions[assertion_id - 1]
        judgment = item.get("judgment")
        hits = [hit for hit in item.get("hit_sentence_ids") or [] if isinstance(hit, int)]
        valid_hits = [hit for hit in hits if 1 <= hit <= len(sentences)]
        if judgment == "supported":
            continue
        if judgment == "distorted" and valid_hits:
            finding = CaseFinding(
                code=CaseErrorCode.HOLDING_DISTORTED,
                risk_level=_risk_level_or_medium(item),
                summary=_user_text(item.get("diff_summary"), limit=300)
                or "文书转述与裁判说理原文含义不符。",
                suggestion=_user_text(item.get("suggestion"))
                or "文书转述改变了裁判说理的含义，请对照说理原句修改。",
                matched_excerpt=build_excerpt(sentences, valid_hits),
            )
            proposed = item.get("revised_text")
            if _safe_llm_revision(assertion_text, proposed):
                finding.revision = RevisionProposal(
                    strategy="replace_exact_text",
                    original_text=assertion_text,
                    revised_text=proposed,
                    rationale=finding.suggestion,
                    machine_applicable=True,
                    preconditions=["original_text_unique", "document_unchanged"],
                )
            findings.append(finding)
        elif judgment == "unsupported" and not hits:
            if truncated:
                truncation_notes.append(
                    f"裁判说理文本疑似截断，观点「{assertion_text}」未能核验，请人工核对原文书。"
                )
                continue
            findings.append(CaseFinding(
                code=CaseErrorCode.HOLDING_UNSUPPORTED,
                risk_level=_risk_level_or_medium(item),
                summary=_user_text(item.get("diff_summary"), limit=300)
                or "该观点在北大法宝收录的裁判说理中没有对应内容。",
                suggestion=_user_text(item.get("suggestion"))
                or "该观点在该案裁判说理中无对应依据，请核对是否引用了正确案例。",
            ))
        else:
            # 判定与定位证据自相矛盾（曲解却无有效句号 / 无依据却给了句号），
            # 结论无法确定性验证，保守转人工。
            findings.append(CaseFinding(
                code=CaseErrorCode.HOLDING_UNSUPPORTED,
                risk_level="MEDIUM",
                summary=f"观点「{_user_text(assertion_text, limit=120)}」的核查结论缺少可验证的说理定位。",
                suggestion="未能在裁判说理中定位该观点的依据句段，请人工核对原文书。",
            ))
    notes = strip_internal_markers(str(raw.get("notes", "")))
    if truncation_notes:
        notes = " ".join(filter(None, [notes, *truncation_notes]))
    if findings:
        verdict = CheckVerdict.ISSUE
    elif truncation_notes:
        verdict = CheckVerdict.INSUFFICIENT_INPUT
    else:
        verdict = CheckVerdict.PASS
    return CaseHoldingCheck(verdict=verdict, findings=findings, notes=notes)


def _risk_level_or_medium(item: dict[str, Any]) -> str:
    level = _risk_level(item)
    return level if level in ("HIGH", "MEDIUM") else "MEDIUM"


def _user_text(value: Any, limit: int | None = None) -> str:
    text = strip_internal_markers(value.strip()) if isinstance(value, str) else ""
    if limit is not None and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


_LEGAL_CITATION = re.compile(r"《[^》]+》(?:第[^，。；\s]{1,30}条(?:第[^，。；\s]{1,12}[款项])?)?")
_CASE_NUMBER = re.compile(r"[（(〔]\d{4}[）)〕][^，。；\s]{2,24}?号")


def _safe_llm_revision(original: str, revised: Any) -> bool:
    """LLM 只能最小改写论述，不得顺手改变已确定的引用身份。"""
    if not isinstance(revised, str) or not revised.strip() or revised == original:
        return False
    if len(revised) < max(4, len(original) // 2) or len(revised) > len(original) * 2 + 80:
        return False
    for pattern in (_LEGAL_CITATION, _CASE_NUMBER):
        if pattern.findall(original) != pattern.findall(revised):
            return False
    return True


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


def _extract_chat_text(data: dict[str, Any]) -> str:
    """解析 OpenAI 兼容的 chat/completions 响应（DeepSeek 官方等）。"""
    for choice in data.get("choices", []):
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content
    raise SemanticResponseError(
        "LLM response did not include message content", "invalid_schema"
    )


def _extract_output_text(data: dict[str, Any]) -> str:
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"]
    raise SemanticResponseError(
        "Qwen response did not include output_text", "invalid_schema"
    )
