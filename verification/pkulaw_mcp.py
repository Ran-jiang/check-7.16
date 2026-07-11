"""北大法宝 MCP client.

Credentials are read from environment variables or a local .env file. Tokens are
never stored in SQLite or written to logs.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


DEFAULT_GATEWAY = "https://apim-gateway.pkulaw.com"


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


class PkulawMcpError(RuntimeError):
    pass


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
        self.access_token = _clean_token(access_token or os.getenv("PKULAW_ACCESS_TOKEN"))
        self.gateway = (gateway or os.getenv("PKULAW_MCP_GATEWAY") or DEFAULT_GATEWAY).rstrip("/")
        self.timeout = timeout
        if not self.access_token:
            raise PkulawMcpError("PKULAW_ACCESS_TOKEN is not configured")

    def get_law_item_content(self, title: str, article_no: str) -> PkulawArticle:
        payload = self._call_tool(
            endpoint="/mcp-fatiao",
            tool_name="get_law_item_content",
            arguments={"title": title, "tiao_num": article_no_to_number(article_no)},
        )
        data = _extract_payload_data(payload)
        return _parse_law_item_response(data, article_no)

    def get_law_list(self, title: str = "", fulltext: str = "") -> list[PkulawLawRecord]:
        if not title and not fulltext:
            raise PkulawMcpError("title or fulltext is required")
        payload = self._call_tool(
            endpoint="/mcp-law",
            tool_name="get_law_list",
            arguments={"title": title, "fulltext": fulltext},
        )
        data = _extract_payload_data(payload)
        return _parse_law_list_response(data)

    def recognize_case_numbers(self, text: str) -> list[PkulawCaseNumber]:
        payload = self._call_tool(
            endpoint="/case_number_recognition",
            tool_name="anhao_recognition",
            arguments={"text": text},
        )
        data = _extract_payload_data(payload)
        return _parse_anhao_response(data)

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
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=default_ssl_context()
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise PkulawMcpError(f"Pkulaw MCP HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise PkulawMcpError(f"Pkulaw MCP request failed: {exc.reason}") from exc
        return _parse_mcp_response(raw)


def article_no_to_number(article_no: str) -> int | float:
    text = article_no.strip().replace("第", "").replace("条", "")
    if text.isdigit():
        return int(text)
    if "之" in text:
        base, suffix = text.split("之", 1)
        base_number = _chinese_number_to_int(base)
        suffix_number = _chinese_number_to_int(suffix)
        if base_number > 0 and 0 < suffix_number < 10:
            return float(f"{base_number}.{suffix_number}")
    value = _chinese_number_to_int(text)
    if value <= 0:
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
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            if not text:
                return {}
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
        article_text=text,
    )


def _parse_anhao_response(data: Any) -> list[PkulawCaseNumber]:
    items = data.get("anhaoname") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise PkulawMcpError("Unexpected anhao_recognition response shape")
    return [_parse_case_number(item) for item in items if isinstance(item, dict)]


def _parse_case_number(item: dict[str, Any]) -> PkulawCaseNumber:
    return PkulawCaseNumber(
        text=item.get("text", ""),
        start=item.get("start", -1),
        end=item.get("end", -1),
        gid=item.get("gid", ""),
        case_flag=item.get("caseFlag", ""),
        court=item.get("court", ""),
        title=item.get("title", ""),
        last_instance_date=item.get("lastInstanceDate"),
        url=item.get("url"),
    )


def _parse_law_list_response(data: Any) -> list[PkulawLawRecord]:
    records = _response_data(data)
    if not isinstance(records, list):
        raise PkulawMcpError("Unexpected get_law_list response shape")
    return [_parse_law_record(record) for record in records if isinstance(record, dict)]


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


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, dict):
        return [str(item) for item in value.values() if item is not None]
    return [str(value)]


def _chinese_number_to_int(text: str) -> int:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000}
    section = 0
    number = 0
    for char in text:
        if char in digits:
            number = digits[char]
        elif char in units:
            section += (number or 1) * units[char]
            number = 0
        else:
            return 0
    return section + number


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
