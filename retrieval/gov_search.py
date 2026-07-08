"""
第二层数据源：搜索接口 + site:gov.cn 限定。

搜索接口用腾讯云联网搜索（与 MarKUP 同款），流程借鉴其
"搜索→验证→二次搜索"骨架，但本项目查询是结构化的（法规名+条号），
验证用确定性检查（目标条号是否在页面命中），二次检索是固定的
查询阶梯而非 LLM 重构检索词：

  查询阶梯: 《全称》+条号 → 简称+条号 → 全称
  每级: 搜索(site=gov.cn) → 过滤非 gov.cn 域名 → 逐页抓取
        → 按条号边界抽取条文 → 命中即返回

配置（.env）：
  TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY  必填
  WSA_HOST / WSA_ACTION / WSA_VERSION               可选覆盖
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from statutedb.cn_num import parse_article_label
from statutedb.normalizer import query_variants

from .schema import ProvisionEvidence, ProvisionQuery

logger = logging.getLogger(__name__)

PROVIDER_NAME = "gov_search"

# 腾讯云联网搜索默认参数（可被 env 覆盖；以官方 API 文档为准）
_DEFAULT_HOST = "wsa.tencentcloudapi.com"
_DEFAULT_ACTION = "SearchPro"
_DEFAULT_VERSION = "2025-05-08"
_DEFAULT_SERVICE = "wsa"

_PAGE_FETCH_LIMIT = 3      # 每级查询最多抓取的候选页数
_PAGE_TIMEOUT = 15.0
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_CN_NUM = r"[零一二两三四五六七八九十百千万0-9]+"
_ARTICLE_ANY_PATTERN = re.compile(rf"第({_CN_NUM})条(?:之({_CN_NUM}))?")


class SearchResult(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""


# ============================================================
# 腾讯云 TC3-HMAC-SHA256 签名（标准算法，手写以避免引入 SDK）
# ============================================================

def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _tc3_headers(
    secret_id: str,
    secret_key: str,
    host: str,
    service: str,
    action: str,
    version: str,
    payload: str,
) -> dict[str, str]:
    """构造带 TC3 签名的请求头。"""
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")

    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_headers = (
        f"content-type:application/json; charset=utf-8\n"
        f"host:{host}\n"
        f"x-tc-action:{action.lower()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    )

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    )

    secret_date = _hmac_sha256(f"TC3{secret_key}".encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, service)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
    }


class TencentSearchClient:
    """腾讯云联网搜索客户端（site 限定检索）。"""

    def __init__(
        self,
        secret_id: Optional[str] = None,
        secret_key: Optional[str] = None,
        timeout: float = 20.0,
    ):
        self.secret_id = secret_id or os.environ.get("TENCENTCLOUD_SECRET_ID", "")
        self.secret_key = secret_key or os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
        self.host = os.environ.get("WSA_HOST", _DEFAULT_HOST)
        self.action = os.environ.get("WSA_ACTION", _DEFAULT_ACTION)
        self.version = os.environ.get("WSA_VERSION", _DEFAULT_VERSION)
        self.timeout = timeout
        if not self.secret_id or not self.secret_key:
            raise RuntimeError(
                "缺少腾讯云凭据：请在 .env 配置 TENCENTCLOUD_SECRET_ID / "
                "TENCENTCLOUD_SECRET_KEY"
            )

    def search(
        self, query: str, site: Optional[str] = None, count: int = 10
    ) -> list[SearchResult]:
        body: dict = {"Query": query, "Cnt": count}
        if site:
            body["Site"] = site
        payload = json.dumps(body, ensure_ascii=False)
        headers = _tc3_headers(
            self.secret_id, self.secret_key, self.host,
            _DEFAULT_SERVICE, self.action, self.version, payload,
        )
        resp = httpx.post(
            f"https://{self.host}/",
            content=payload.encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json().get("Response", {})
        if "Error" in data:
            raise RuntimeError(
                f"腾讯云搜索接口错误: {data['Error'].get('Code')} "
                f"{data['Error'].get('Message')}"
            )
        return _parse_search_pages(data)


def _parse_search_pages(response: dict) -> list[SearchResult]:
    """
    防御式解析搜索结果列表。

    联网搜索返回 Pages（每项为 JSON 字符串）；字段名按候选列表探测，
    等接口文档确认后可收紧。
    """
    raw_items = response.get("Pages") or response.get("Results") or []
    results: list[SearchResult] = []
    for item in raw_items:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                continue
        if not isinstance(item, dict):
            continue
        url = _first_of(item, ["url", "Url", "link", "Link"])
        if not url:
            continue
        results.append(SearchResult(
            title=_first_of(item, ["title", "Title", "name", "Name"]),
            url=url,
            snippet=_first_of(
                item,
                ["passage", "Passage", "snippet", "Snippet",
                 "abstract", "Abstract", "content", "Content", "summary"],
            ),
        ))
    return results


def _first_of(d: dict, keys: list[str]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# ============================================================
# 网页正文抽取（标准库 HTMLParser，不引 bs4）
# ============================================================

class _TextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "head"}
    _BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "td",
                   "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t　]+", " ", ln).strip()
                 for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — 残缺 HTML 尽力而为
        pass
    return parser.text()


def _decode_page(resp: httpx.Response) -> str:
    """gov.cn 站点新旧并存，charset 声明混乱，按 meta 探测编码。"""
    content = resp.content
    head = content[:2048].decode("ascii", errors="ignore").lower()
    m = re.search(r'charset=["\']?([a-z0-9-]+)', head)
    encoding = m.group(1) if m else (resp.charset_encoding or "utf-8")
    if encoding in ("gb2312", "gbk"):
        encoding = "gb18030"
    try:
        return content.decode(encoding, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


# ============================================================
# 条文边界抽取
# ============================================================

def extract_article_from_text(
    page_text: str, article_num: int, article_suffix: int = 0
) -> Optional[str]:
    """
    从页面文本中按条号边界抽取目标条文。

    定位第一个等于目标 (num, suffix) 的"第X条"出现位置，
    截取到下一个不同条号（或文本尾/上限）为止。
    """
    from statutedb.cn_num import cn_to_int

    matches = list(_ARTICLE_ANY_PATTERN.finditer(page_text))
    for i, m in enumerate(matches):
        try:
            num = cn_to_int(m.group(1))
            suffix = cn_to_int(m.group(2)) if m.group(2) else 0
        except ValueError:
            continue
        if (num, suffix) != (article_num, article_suffix):
            continue
        # 找下一个不同条号作为右边界
        end = len(page_text)
        for nm in matches[i + 1:]:
            try:
                n2 = cn_to_int(nm.group(1))
                s2 = cn_to_int(nm.group(2)) if nm.group(2) else 0
            except ValueError:
                continue
            if (n2, s2) != (num, suffix):
                end = nm.start()
                break
        text = page_text[m.start():end].strip()
        text = re.sub(r"\n{2,}", "\n", text)[:4000].strip()
        # 确定性验证：除条号标签外要有实体内容
        if len(text) > len(m.group(0)) + 4:
            return text
    return None


def _is_gov_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "gov.cn" or host.endswith(".gov.cn")


# ============================================================
# GovSearchSource（FallbackSource 实现）
# ============================================================

class GovSearchSource:
    """搜索 + 抓取 + 条文抽取。"""

    name = PROVIDER_NAME

    def __init__(self, client: Optional[TencentSearchClient] = None):
        # 延迟建客户端：无凭据时 fetch 阶段才报错（便于 CLI 提示）
        self._client = client

    @property
    def client(self) -> TencentSearchClient:
        if self._client is None:
            self._client = TencentSearchClient()
        return self._client

    def fetch(self, query: ProvisionQuery) -> Optional[ProvisionEvidence]:
        parsed = (
            parse_article_label(query.article_label)
            if query.article_label else None
        )

        for search_query in self._query_ladder(query):
            try:
                results = self.client.search(search_query, site="gov.cn")
            except Exception as e:  # noqa: BLE001
                logger.warning("gov_search 搜索失败 %r: %s", search_query, e)
                raise
            gov_results = [r for r in results if _is_gov_url(r.url)]

            if parsed is None:
                # 法规级引注：确认 gov.cn 上存在同名法规页面即可
                evidence = self._law_level_evidence(query, gov_results)
                if evidence:
                    return evidence
                continue

            for result in gov_results[:_PAGE_FETCH_LIMIT]:
                text = self._fetch_and_extract(result.url, parsed)
                if text:
                    return ProvisionEvidence(
                        provider=PROVIDER_NAME,
                        law_title=query.law_title,
                        article_label=query.article_label,
                        text=text,
                        source_url=result.url,
                        note=f"检索词: {search_query}",
                    )
        return None

    # ------------------------------------------------------------

    def _query_ladder(self, query: ProvisionQuery) -> list[str]:
        """查询阶梯：《变体》+条号 → 变体。变体按 query_variants 优先级。"""
        variants = query_variants(query.law_title)
        ladder: list[str] = []
        if query.article_label:
            for v in variants:
                ladder.append(f"《{v}》 {query.article_label}")
        ladder.extend(f"《{v}》" for v in variants[:1])
        # 去重保序
        seen: set[str] = set()
        return [q for q in ladder if not (q in seen or seen.add(q))]

    def _law_level_evidence(
        self, query: ProvisionQuery, results: list[SearchResult]
    ) -> Optional[ProvisionEvidence]:
        from statutedb.normalizer import normalize_title

        target = normalize_title(query.law_title)
        for r in results:
            if target and target in normalize_title(r.title):
                return ProvisionEvidence(
                    provider=PROVIDER_NAME,
                    law_title=query.law_title,
                    text="",
                    source_url=r.url,
                    note="法规级引注，gov.cn 检索到同名法规页面",
                )
        return None

    def _fetch_and_extract(
        self, url: str, parsed: tuple[int, int]
    ) -> Optional[str]:
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=_PAGE_TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001 — 单页失败换下一页
            logger.info("gov_search 抓取失败 %s: %s", url, e)
            return None
        page_text = html_to_text(_decode_page(resp))
        return extract_article_from_text(page_text, parsed[0], parsed[1])
