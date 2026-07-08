"""
第三层数据源：北大法宝 MCP 兜底。

依赖 pkulaw_client.pkulaw_get_article（MCP streamable-HTTP 客户端），
本模块只做 ProvisionQuery ↔ 法宝工具入参/出参的映射。

法宝"精准查找法条"工具签名（已在网关确认）：
  get_article(title: 法规标题中文, number: 法条序号中文如"第四十八条") → 条文文本
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from statutedb.normalizer import strip_parenthetical, normalize_title

from .schema import ProvisionEvidence, ProvisionQuery

logger = logging.getLogger(__name__)

PROVIDER_NAME = "pkulaw"


class PkulawSource:
    """法宝 MCP 的 FallbackSource 实现。"""

    name = PROVIDER_NAME

    def __init__(self, url: Optional[str] = None,
                 headers: Optional[dict[str, str]] = None):
        self.url = url
        self.headers = headers

    @property
    def configured(self) -> bool:
        return bool(self.url or os.environ.get("PKULAW_MCP_URL"))

    def fetch(self, query: ProvisionQuery) -> Optional[ProvisionEvidence]:
        if not self.configured:
            raise RuntimeError(
                "法宝 MCP 未配置：请在 .env 设置 PKULAW_MCP_URL / PKULAW_MCP_HEADERS"
            )
        if not query.article_label:
            # 法宝精准查找法条需要条号；法规级引注本层不处理
            return None

        from .pkulaw_client import pkulaw_get_article

        # 法宝按全称检索更稳：去掉版本注记
        title = strip_parenthetical(normalize_title(query.law_title))
        text = pkulaw_get_article(
            title=title,
            number=query.article_label,
            url=self.url,
            headers=self.headers,
        )
        text = (text or "").strip()
        if not text or _looks_like_miss(text):
            return None
        return ProvisionEvidence(
            provider=PROVIDER_NAME,
            law_title=query.law_title,
            article_label=query.article_label,
            text=text,
            note="北大法宝 MCP（精准查找法条）",
        )


def _looks_like_miss(text: str) -> bool:
    """法宝未命中时可能返回提示语而非条文，做启发式识别。"""
    miss_markers = ["未找到", "未检索到", "没有找到", "无匹配", "不存在", "无结果"]
    return len(text) < 60 and any(m in text for m in miss_markers)
