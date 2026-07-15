"""北大法宝 MCP client.

Credentials are read from environment variables or a local .env file. Tokens are
never stored in SQLite or written to logs.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from legal_numbers import chinese_number_to_int


DEFAULT_GATEWAY = "https://apim-gateway.pkulaw.com"
MCP_ENDPOINTS = {
    "law_keyword": "/mcp-law",
    "law_semantic": "/mcp-law-search-service",
    "case_keyword": "/mcp-case",
    "case_semantic": "/mcp-case-search-service",
    "case_number": "/case_number_recognition",
    "fatiao": "/mcp-fatiao",
}


def default_ssl_context() -> ssl.SSLContext:
    """python.org 版 Python 不读系统钥匙串，显式使用 certifi 的 CA 库。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


@dataclass(frozen=True)
class PkulawLawRecord:
    title: str
    url: Optional[str] = None
    category: list[str] = field(default_factory=list)
    document_no: Optional[str] = None
    issue_department: list[str] = field(default_factory=list)
    issue_date: Optional[str] = None
    implement_date: Optional[str] = None
    timeliness: list[str] = field(default_factory=list)
    effectiveness: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PkulawArticle(PkulawLawRecord):
    article_no: str = ""
    article_text: str = ""


@dataclass(frozen=True)
class PkulawCaseNumber:
    text: str
    start: int
    end: int
    gid: str
    case_flag: str
    court: str
    title: str
    last_instance_date: Optional[str] = None
    url: Optional[str] = None


@dataclass(frozen=True)
class PkulawCaseRecord:
    title: str
    case_number: str = ""
    gid: str = ""
    court: str = ""
    last_instance_date: Optional[str] = None
    url: Optional[str] = None
    fulltext: Optional[str] = None


class PkulawMcpError(RuntimeError):
    pass


class PkulawNotConfiguredError(PkulawMcpError):
    """No usable local MCP credential was configured."""


class PkulawNotFoundError(PkulawMcpError):
    """法宝检索已完成但未命中任何数据（区别于配置/网络错误）。"""


class PkulawMcpClient:
    def __init__(
        self,
        access_token: Optional[str] = None,
        gateway: Optional[str] = None,
        timeout: int = 20,
    ):
        _load_dotenv()
        self.access_token = _clean_token(
            access_token
            or os.getenv("PKULAW_ACCESS_TOKEN")
        )
        self.gateway = (
            gateway
            or os.getenv("PKULAW_MCP_GATEWAY")
            or DEFAULT_GATEWAY
        ).rstrip("/")
        self.timeout = timeout
        if not self.access_token:
            raise PkulawNotConfiguredError("Pkulaw MCP credentials are not configured")

    def get_law_item_content(self, title: str, article_no: str) -> PkulawArticle:
        payload = self._call_tool(
            endpoint=MCP_ENDPOINTS["fatiao"],
            tool_name="get_law_item_content",
            arguments={"title": title, "tiao_num": article_no_to_number(article_no)},
        )
        data = _extract_payload_data(payload)
        return _parse_law_item_response(data, article_no)

    def get_law_list(self, title: str = "", fulltext: str = "") -> list[PkulawLawRecord]:
        if not title and not fulltext:
            raise PkulawMcpError("title or fulltext is required")
        payload = self._call_tool(
            endpoint=MCP_ENDPOINTS["law_keyword"],
            tool_name="get_law_list",
            arguments={"lawInput": {"Title": title, "FullText": fulltext}},
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

    def recognize_case_numbers(self, text: str) -> list[PkulawCaseNumber]:
        payload = self._call_tool(
            endpoint=MCP_ENDPOINTS["case_number"],
            tool_name="anhao_recognition",
            arguments={"text": text},
        )
        data = _extract_payload_data(payload)
        return _parse_anhao_response(data)

    def get_case_list(
        self, title: str = "", fulltext: str = ""
    ) -> list[PkulawCaseRecord]:
        if not title and not fulltext:
            raise PkulawMcpError("case title or fulltext is required")
        payload = self._call_tool(
            endpoint=MCP_ENDPOINTS["case_keyword"],
            tool_name="get_case_list",
            arguments={"title": title, "fulltext": fulltext},
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

    def _call_tool(self, endpoint: str, tool_name: str, arguments: dict[str, Any]) -> Any:
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
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = PkulawMcpError(f"Pkulaw MCP request failed: {reason}")
        raise last_error


def article_no_to_number(article_no: str) -> int | float:
    text = article_no.strip().replace("第", "").replace("条", "")
    if text.isdigit():
        return int(text)
    if "之" in text:
        base, suffix = text.split("之", 1)
        base_number = chinese_number_to_int(base)
        suffix_number = chinese_number_to_int(suffix)
        if (
            base_number is not None
            and suffix_number is not None
            and base_number > 0
            and 0 < suffix_number < 10
        ):
            return float(f"{base_number}.{suffix_number}")
    value = chinese_number_to_int(text)
    if value is None or value <= 0:
        raise PkulawMcpError(f"Unsupported article number: {article_no}")
    return value


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


def _parse_law_item_response(data: Any, requested_article_no: str) -> PkulawArticle:
    item = _response_data(data)
    if not isinstance(item, dict):
        raise PkulawMcpError("Unexpected get_law_item_content response shape")
    title = item.get("Title") or item.get("title")
    text = item.get("FullText") or item.get("fulltext") or item.get("article") or item.get("original_text")
    if not title or not text:
        raise PkulawMcpError("Pkulaw response is missing title or article text")
    base = _parse_law_record(item)
    return PkulawArticle(
        title=base.title,
        url=base.url,
        category=base.category,
        document_no=base.document_no,
        issue_department=base.issue_department,
        issue_date=base.issue_date,
        implement_date=base.implement_date,
        timeliness=base.timeliness,
        effectiveness=base.effectiveness,
        article_no=requested_article_no,
        article_text=_strip_article_heading(str(text)),
    )


def _strip_article_heading(text: str) -> str:
    """法宝 FullText 有时自带“第X条”，证据字段只保存条文内容。"""
    return re.sub(
        r"^\s*第[〇零一二三四五六七八九十百千万两0-9]+条(?:之[〇零一二三四五六七八九十百千万两0-9]+)?[\s　]*",
        "",
        text,
        count=1,
    )


def _parse_anhao_response(data: Any) -> list[PkulawCaseNumber]:
    value = _response_data(data)
    items: Any = None
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        for key in (
            "anhaoname", "anhaoName", "AnhaoName", "items", "records",
            "results", "list", "cases", "case_numbers", "caseNumbers",
        ):
            candidate = value.get(key)
            if isinstance(candidate, list):
                items = candidate
                break
        # 部分网关在仅识别到一个案号时直接返回记录对象。
        if items is None and any(
            key in value for key in ("caseFlag", "case_number", "CaseNO", "text")
        ):
            items = [value]
    if items is None and value in (None, {}):
        return []
    if not isinstance(items, list):
        raise PkulawMcpError("Unexpected anhao_recognition response shape")
    parsed = [_parse_case_number(item) for item in items if isinstance(item, dict)]
    # 网关偶尔会在不同包装层重复返回同一案号，以规范化案号/GID 去重。
    unique: dict[tuple[str, str], PkulawCaseNumber] = {}
    for item in parsed:
        key = (_compact_case_number(item.case_flag or item.text), item.gid)
        unique.setdefault(key, item)
    return list(unique.values())


def _parse_case_list_response(data: Any) -> list[PkulawCaseRecord]:
    records = _response_records(data)
    return [
        parsed
        for item in records
        if isinstance(item, dict)
        if (parsed := _parse_case_record(item)) is not None
    ]


def _parse_case_record(item: dict[str, Any]) -> PkulawCaseRecord | None:
    record = _flatten_metadata(item)
    title = _first_value(record, "Title", "title", "CaseName", "case_name")
    case_number = _first_value(
        record,
        "CaseFlag",
        "caseFlag",
        "CaseNO",
        "CaseNo",
        "case_number",
        "caseNumber",
    )
    if not title and not case_number:
        return None
    return PkulawCaseRecord(
        title=str(title or case_number),
        case_number=str(case_number or ""),
        gid=str(_first_value(record, "Gid", "gid", "GID") or ""),
        court=str(_first_value(record, "Court", "court") or ""),
        last_instance_date=_optional_text(
            _first_value(record, "LastInstanceDate", "lastInstanceDate", "judgment_date")
        ),
        url=_optional_text(_first_value(record, "Url", "url", "URL")),
        fulltext=_optional_text(
            _first_value(
                record,
                "FullText",
                "fulltext",
                "full_text",
                "Content",
                "content",
                "excerpt",
            )
        ),
    )


def _parse_case_number(item: dict[str, Any]) -> PkulawCaseNumber:
    item = _flatten_metadata(item)
    return PkulawCaseNumber(
        text=str(_first_value(item, "text", "matched_text", "anhao") or ""),
        start=int(_first_value(item, "start", "startIndex") or -1),
        end=int(_first_value(item, "end", "endIndex") or -1),
        gid=str(_first_value(item, "gid", "Gid", "GID") or ""),
        case_flag=str(_first_value(
            item, "caseFlag", "CaseFlag", "case_number", "caseNumber", "CaseNO", "CaseNo"
        ) or ""),
        court=str(_first_value(item, "court", "Court") or ""),
        title=str(_first_value(item, "title", "Title", "CaseName", "case_name") or ""),
        last_instance_date=_optional_text(_first_value(
            item, "lastInstanceDate", "LastInstanceDate", "judgment_date"
        )),
        url=_optional_text(_first_value(item, "url", "Url", "URL")),
    )


def _compact_case_number(value: str) -> str:
    return "".join(str(value).translate(str.maketrans({"（": "(", "）": ")", "〔": "(", "〕": ")"})).split())


def _parse_law_list_response(data: Any) -> list[PkulawLawRecord]:
    records = _response_records(data)
    return [_parse_law_record(record) for record in records if isinstance(record, dict)]


def _parse_article_search_response(data: Any) -> list[PkulawArticle]:
    records = _response_records(data)
    articles: list[PkulawArticle] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = _flatten_metadata(raw)
        title = _first_value(record, "Title", "title", "LawTitle", "law_title")
        article_text = _first_value(
            record,
            "FullText",
            "fulltext",
            "article_text",
            "ArticleText",
            "Content",
            "content",
            "text",
        )
        if not title or not article_text:
            continue
        base = _parse_law_record(record)
        article_no = _first_value(
            record,
            "ArticleNO",
            "ArticleNo",
            "article_no",
            "TiaoNum",
            "tiao_num",
        )
        articles.append(
            PkulawArticle(
                title=base.title,
                url=base.url,
                category=base.category,
                document_no=base.document_no,
                issue_department=base.issue_department,
                issue_date=base.issue_date,
                implement_date=base.implement_date,
                timeliness=base.timeliness,
                effectiveness=base.effectiveness,
                article_no=str(article_no or "相关条款"),
                article_text=str(article_text),
            )
        )
    return articles


def _parse_law_record(record: dict[str, Any]) -> PkulawLawRecord:
    return PkulawLawRecord(
        title=record.get("Title") or record.get("title") or "",
        url=record.get("Url") or record.get("url"),
        category=_as_list(record.get("Category")),
        document_no=record.get("DocumentNO") or record.get("document_no"),
        issue_department=_as_list(record.get("IssueDepartment")),
        issue_date=record.get("IssueDate") or record.get("issue_date"),
        implement_date=record.get("ImplementDate") or record.get("implement_date"),
        timeliness=_as_list(record.get("TimelinessDic") or record.get("timeliness")),
        effectiveness=_as_list(record.get("EffectivenessDic") or record.get("effectiveness")),
    )


def _response_data(data: Any) -> Any:
    if isinstance(data, dict):
        if data.get("Message") not in (None, "成功", "success"):
            message = str(data.get("Message"))
            if "未找到" in message or "无数据" in message:
                raise PkulawNotFoundError(message)
            raise PkulawMcpError(message)
        if "Data" in data:
            return data["Data"]
        if "data" in data:
            return data["data"]
    return data


def _response_records(data: Any) -> list[Any]:
    value = _response_data(data)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "records", "results", "list", "articles", "cases"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    if value in (None, {}):
        return []
    raise PkulawMcpError("Unexpected Pkulaw list response shape")


def _flatten_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        return {**record, **metadata}
    return record


def _first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _optional_text(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, dict):
        return [str(item) for item in value.values() if item is not None]
    return [str(value)]


def _clean_token(token: Optional[str]) -> Optional[str]:
    if token is None:
        return None
    value = token.strip().strip('"').strip("'")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _load_dotenv() -> None:
    for path in (Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
