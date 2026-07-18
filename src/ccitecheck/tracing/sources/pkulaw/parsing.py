"""北大法宝工具响应到领域模型的解析。"""

from __future__ import annotations

import re
from typing import Any, Optional

from .models import (
    PkulawArticle,
    PkulawCaseRecord,
    PkulawLawRecord,
    PkulawMcpError,
    PkulawNotFoundError,
)
from .urls import usable_mcp_url


def parse_exact_article(data: Any, requested_article_no: str) -> PkulawArticle:
    item = response_data(data)
    if item is None or (isinstance(item, str) and "未找到" in item):
        raise PkulawNotFoundError("未找到数据")
    if not isinstance(item, dict):
        raise PkulawMcpError("Unexpected get_article response shape")
    title = first_value(item, "Title", "title")
    text = first_value(item, "Article", "article")
    if not title or not text:
        raise PkulawMcpError("Pkulaw response is missing title or article text")
    return PkulawArticle(
        title=str(title),
        url=usable_mcp_url(optional_text(first_value(item, "Url", "url"))),
        article_no=requested_article_no,
        article_text=strip_article_heading(str(text)),
    )


def parse_case_records(data: Any) -> list[PkulawCaseRecord]:
    return [
        parsed
        for item in response_records(data)
        if isinstance(item, dict)
        if (parsed := parse_case_record(item)) is not None
    ]


def parse_case_record(item: dict[str, Any]) -> PkulawCaseRecord | None:
    record = flatten_metadata(item)
    title = first_value(record, "Title", "title")
    case_number = first_value(record, "CaseNO", "CaseFlag", "case_number")
    if not title and not case_number:
        return None
    return PkulawCaseRecord(
        title=str(title or case_number),
        case_number=str(case_number or ""),
        gid=str(first_value(record, "Gid", "gid") or ""),
        court=str(first_value(record, "Court", "court", "courthouse_name") or ""),
        last_instance_date=optional_text(
            first_value(record, "LastInstanceDate", "lastInstanceDate")
        ),
        url=usable_mcp_url(optional_text(first_value(record, "Url", "url"))),
        fulltext=optional_text(first_value(record, "FullText", "fulltext")),
        holding=optional_text(first_value(record, "CaseGist", "Identified", "identified")),
    )


def parse_law_records(data: Any) -> list[PkulawLawRecord]:
    return [
        parse_law_record(record)
        for record in response_records(data)
        if isinstance(record, dict)
    ]


def parse_article_records(data: Any) -> list[PkulawArticle]:
    articles: list[PkulawArticle] = []
    for raw in response_records(data):
        if not isinstance(raw, dict):
            continue
        record = flatten_metadata(raw)
        title = first_value(record, "Title", "title")
        article_text = first_value(
            record, "FullText", "fulltext", "ArticleText", "article_text"
        )
        if not title or not article_text:
            continue
        base = parse_law_record(record)
        article_no = first_value(record, "ArticleNO", "article_no")
        articles.append(
            article_from_record(base, str(article_no or ""), str(article_text))
        )
    return articles


def parse_law_record(record: dict[str, Any]) -> PkulawLawRecord:
    return PkulawLawRecord(
        title=str(first_value(record, "Title", "title") or ""),
        url=usable_mcp_url(optional_text(first_value(record, "Url", "url"))),
        category=as_list(first_value(record, "Category", "category")),
        document_no=optional_text(first_value(record, "DocumentNO", "document_no")),
        issue_department=as_list(
            first_value(record, "IssueDepartment", "issue_department")
        ),
        issue_date=optional_text(first_value(record, "IssueDate", "issue_date")),
        implement_date=optional_text(
            first_value(record, "ImplementDate", "implement_date")
        ),
        timeliness=as_list(first_value(record, "TimelinessDic", "timeliness")),
        effectiveness=as_list(first_value(record, "EffectivenessDic", "effectiveness")),
    )


def response_data(data: Any) -> Any:
    if isinstance(data, dict):
        message = data.get("Message")
        if message not in (None, "成功", "success"):
            text = str(message)
            if "未找到" in text or "无数据" in text:
                raise PkulawNotFoundError(text)
            raise PkulawMcpError(text)
        if "Data" in data:
            return data["Data"]
        if "data" in data:
            return data["data"]
    return data


def response_records(data: Any) -> list[Any]:
    value = response_data(data)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "records", "results"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    if value in (None, {}):
        return []
    raise PkulawMcpError("Unexpected Pkulaw list response shape")


def article_from_record(
    base: PkulawLawRecord, article_no: str, article_text: str
) -> PkulawArticle:
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
        article_no=article_no,
        article_text=strip_article_heading(article_text),
    )


def strip_article_heading(text: str) -> str:
    return re.sub(
        r"^\s*第[〇零一二三四五六七八九十百千万两0-9]+条(?:之[〇零一二三四五六七八九十百千万两0-9]+)?[\s　]*",
        "",
        text,
        count=1,
    )


def flatten_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    return {**record, **metadata} if isinstance(metadata, dict) else record


def first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def optional_text(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def optional_int(value: Any, default: int) -> int:
    return default if value in (None, "") else int(value)


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def compact_case_number(value: str) -> str:
    table = str.maketrans({"（": "(", "）": ")", "〔": "(", "〕": ")"})
    return "".join(str(value).translate(table).split())


__all__ = [
    "parse_article_records",
    "parse_case_records",
    "parse_exact_article",
    "parse_law_records",
]
