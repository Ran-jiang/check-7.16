"""北大法宝 MCP 客户端。

凭证从环境变量或本地 ``.env`` 文件读取，不写入 SQLite 或日志。
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from ....domain.legal_numbers import int_to_chinese_number

from ....infrastructure.config import load_project_env
from ....infrastructure.http import default_ssl_context
from ...queries import build_article_semantic_fallback_query
from .models import (
    PkulawArticle,
    PkulawCaseRecord,
    PkulawLawRecord,
    PkulawMcpError,
    PkulawNotConfiguredError,
    PkulawNotFoundError,
)
from .parsing import (
    parse_article_records as _parse_article_search_response,
    parse_case_records as _parse_case_list_response,
    parse_exact_article as _parse_get_article_response,
    parse_law_records as _parse_law_list_response,
)


DEFAULT_GATEWAY = "https://apim-gateway.pkulaw.com"
MCP_ENDPOINTS = {
    "law_keyword": "/mcp-law",
    "law_semantic": "/mcp-law-search-service",
    "case_keyword": "/mcp-case",
    "case_semantic": "/mcp-case-search-service",
}


class PkulawMcpClient:
    def __init__(
        self,
        access_token: Optional[str] = None,
        gateway: Optional[str] = None,
        timeout: int = 20,
    ):
        load_project_env()
        self.access_token = _clean_token(
            access_token or os.getenv("PKULAW_ACCESS_TOKEN")
        )
        self.gateway = (
            gateway or os.getenv("PKULAW_MCP_GATEWAY") or DEFAULT_GATEWAY
        ).rstrip("/")
        self.timeout = timeout
        if not self.access_token:
            raise PkulawNotConfiguredError("Pkulaw MCP credentials are not configured")

    def get_article(self, title: str, article_no: str) -> PkulawArticle:
        normalized = normalize_article_no(article_no)
        payload = self._call_tool(
            endpoint=MCP_ENDPOINTS["law_semantic"],
            tool_name="get_article",
            arguments={"title": title, "number": normalized},
        )
        data = _extract_payload_data(payload)
        return _parse_get_article_response(data, normalized)

    def get_law_list(
        self, title: str = "", fulltext: str = ""
    ) -> list[PkulawLawRecord]:
        if not title and not fulltext:
            raise PkulawMcpError("title or fulltext is required")
        payload = self._call_tool(
            endpoint=MCP_ENDPOINTS["law_keyword"],
            tool_name="get_law_list",
            arguments={"lawInput": {"Title": title, "Fulltext": fulltext}},
        )
        data = _extract_payload_data(payload)
        return _parse_law_list_response(data)

    def search_law_articles(self, text: str) -> list[PkulawArticle]:
        if not text.strip():
            raise PkulawMcpError("semantic law query is required")
        payload = self._call_tool(
            endpoint=MCP_ENDPOINTS["law_semantic"],
            tool_name="search_article",
            arguments={"text": text},
        )
        data = _extract_payload_data(payload)
        return _parse_article_search_response(data)

    def search_law_articles_for_article(
        self, title: str, article_no: str
    ) -> list[PkulawArticle]:
        return self.search_law_articles(
            build_article_semantic_fallback_query(title, article_no)
        )

    def get_case_list(
        self, title: str = "", fulltext: str = ""
    ) -> list[PkulawCaseRecord]:
        if not title and not fulltext:
            raise PkulawMcpError("case title or fulltext is required")
        payload = self._call_tool(
            endpoint=MCP_ENDPOINTS["case_keyword"],
            tool_name="get_case_list",
            # MCP schema uses ``Fulltext`` (lower-case t).  ``FullText`` is
            # silently ignored by the gateway and makes case-number-only
            # searches look as if no query was supplied.
            arguments={"caseInput": {"Title": title, "Fulltext": fulltext}},
        )
        data = _extract_payload_data(payload)
        return _parse_case_list_response(data)

    def search_cases(self, text: str) -> list[PkulawCaseRecord]:
        if not text.strip():
            raise PkulawMcpError("semantic case query is required")
        payload = self._call_tool(
            endpoint=MCP_ENDPOINTS["case_semantic"],
            tool_name="search_case",
            arguments={"text": text},
        )
        data = _extract_payload_data(payload)
        return _parse_case_list_response(data)

    def _call_tool(
        self, endpoint: str, tool_name: str, arguments: dict[str, Any]
    ) -> Any:
        url = f"{self.gateway}{endpoint}"
        request_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
            method="POST",
        )
        # 网络抖动/网关 5xx 退避重试；"未找到数据"是成功响应，不会走到这里。
        # 重试耗尽后抛 PkulawMcpError → 上游只报"无法判断"，绝不误判"法源不存在"。
        last_error: Exception | None = None
        for attempt in range(3):
            if attempt:
                time.sleep(0.5 * (2 ** (attempt - 1)))
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=default_ssl_context()
                ) as response:
                    raw = response.read().decode("utf-8")
                return _parse_mcp_response(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = PkulawMcpError(
                    f"Pkulaw MCP HTTP {exc.code}: {detail[:300]}"
                )
                if exc.code < 500:  # 4xx 是配置/鉴权问题，重试无意义
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = PkulawMcpError(f"Pkulaw MCP request failed: {reason}")
        raise last_error


_ARTICLE_NO = re.compile(r"^第(?P<base>\d+)条(?:之(?P<suffix>\d+))?$")


def normalize_article_no(article_no: str) -> str:
    text = article_no.strip()
    match = _ARTICLE_NO.fullmatch(text)
    if not match:
        return text
    try:
        base = int_to_chinese_number(int(match.group("base")))
        suffix = match.group("suffix")
        return f"第{base}条" + (
            f"之{int_to_chinese_number(int(suffix))}" if suffix else ""
        )
    except ValueError:
        return text


def _parse_mcp_response(raw: str) -> Any:
    text = raw.strip()
    if not text:
        raise PkulawMcpError("Pkulaw MCP returned an empty response")
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            item = line.removeprefix("data:").strip()
            if item and item != "[DONE]":
                events.append(json.loads(item))
    if events:
        return events[-1]
    return json.loads(text)


def _extract_payload_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "error" in payload:
        raise PkulawMcpError(str(payload["error"]))
    result = payload.get("result") if isinstance(payload, dict) else payload
    content = result.get("content") if isinstance(result, dict) else None
    is_error = bool(isinstance(result, dict) and result.get("isError"))
    if isinstance(content, list) and content:
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text", "")
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # 法宝工具在未命中时会返回纯文本错误（如 Data 为 None 的
                # pydantic 校验错误），归类为"检索完成但未找到"
                if is_error and ("input_value=None" in text or "未找到" in text):
                    raise PkulawNotFoundError("未找到数据") from None
                raise PkulawMcpError(text[:300]) from None
    if is_error:
        raise PkulawMcpError("Pkulaw tool returned an error without detail")
    return result


def _clean_token(token: Optional[str]) -> Optional[str]:
    if token is None:
        return None
    value = token.strip().strip('"').strip("'")
    return value[7:].strip() if value.lower().startswith("bearer ") else value
