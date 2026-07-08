"""路由器测试：三层回退顺序与本地权威语义（stub 外部源）。"""

from __future__ import annotations

from typing import Optional

import pytest

from retrieval.local_source import LocalSource
from retrieval.router import ProvisionRouter, queries_from_claims
from retrieval.schema import (
    ProvisionEvidence,
    ProvisionQuery,
    RetrievalStatus,
)
from statutedb.db import init_db
from statutedb.importer import import_statute
from statutedb.law_parser import parse_law_lines
from statutedb.store import StatuteStore

LAW_LINES = [
    "中华人民共和国测试法",
    "第一条　为了测试本系统，制定本法。",
    "第二条　测试应当遵循自愿、公平原则。",
    "（一）单元测试优先；",
    "（二）集成测试其次。",
]


class StubSource:
    """可编程外部源：记录调用并返回预设结果。"""

    def __init__(self, name: str, evidence: Optional[ProvisionEvidence] = None,
                 error: Optional[Exception] = None):
        self.name = name
        self.evidence = evidence
        self.error = error
        self.calls: list[ProvisionQuery] = []

    def fetch(self, query: ProvisionQuery) -> Optional[ProvisionEvidence]:
        self.calls.append(query)
        if self.error:
            raise self.error
        return self.evidence


@pytest.fixture()
def local():
    conn = init_db(":memory:")
    doc = parse_law_lines(LAW_LINES)
    import_statute(conn, doc, "law")
    yield LocalSource(StatuteStore(conn))
    conn.close()


def _evidence(provider: str) -> ProvisionEvidence:
    return ProvisionEvidence(
        provider=provider, law_title="外部法",
        article_label="第一条", text="第一条　外部条文。",
        source_url="https://example.gov.cn/law",
    )


class TestLocalFirst:
    def test_local_hit_skips_fallbacks(self, local):
        gov = StubSource("gov_search", _evidence("gov_search"))
        router = ProvisionRouter(local, [gov])
        result = router.resolve(ProvisionQuery(
            law_title="测试法", article_label="第一条"
        ))
        assert result.status == RetrievalStatus.FOUND
        assert result.evidence.provider == "local"
        assert gov.calls == []

    def test_local_article_not_found_is_terminal(self, local):
        """法在库内、条号超界 → 引注错误信号，不下探外部源。"""
        gov = StubSource("gov_search", _evidence("gov_search"))
        router = ProvisionRouter(local, [gov])
        result = router.resolve(ProvisionQuery(
            law_title="测试法", article_label="第九十九条"
        ))
        assert result.status == RetrievalStatus.ARTICLE_NOT_FOUND
        assert gov.calls == []

    def test_article_not_found_with_suggestions(self, local):
        result = ProvisionRouter(local).resolve(ProvisionQuery(
            law_title="测试法", article_label="第九十九条",
            quote_text="自愿、公平原则",
        ))
        assert result.status == RetrievalStatus.ARTICLE_NOT_FOUND
        assert result.suggestions
        assert result.suggestions[0].article_label == "第二条"

    def test_law_level_citation(self, local):
        result = ProvisionRouter(local).resolve(ProvisionQuery(
            law_title="测试法"
        ))
        assert result.status == RetrievalStatus.LAW_FOUND_NO_ARTICLE

    def test_law_level_with_quote_locates_article(self, local):
        """无条款号但有转述文本 → FTS 定位到具体条文。"""
        result = ProvisionRouter(local).resolve(ProvisionQuery(
            law_title="测试法", quote_text="自愿、公平原则",
        ))
        assert result.status == RetrievalStatus.FOUND
        assert result.evidence.article_label == "第二条"
        assert result.evidence.note is not None

    def test_clause_texts_for_item_citation(self, local):
        result = ProvisionRouter(local).resolve(ProvisionQuery(
            law_title="测试法", article_label="第二条",
            paragraph_labels=["第一款"], item_labels=["第（二）项"],
        ))
        assert result.status == RetrievalStatus.FOUND
        assert result.evidence.clause_texts == ["（二）集成测试其次。"]


class TestFallbackChain:
    def test_falls_through_in_order(self, local):
        gov = StubSource("gov_search", None)          # 未命中
        pkulaw = StubSource("pkulaw", _evidence("pkulaw"))
        router = ProvisionRouter(local, [gov, pkulaw])
        result = router.resolve(ProvisionQuery(
            law_title="外部法", article_label="第一条"
        ))
        assert result.status == RetrievalStatus.FOUND
        assert result.evidence.provider == "pkulaw"
        assert len(gov.calls) == 1
        assert result.providers_tried == ["local", "gov_search", "pkulaw"]

    def test_first_fallback_hit_stops(self, local):
        gov = StubSource("gov_search", _evidence("gov_search"))
        pkulaw = StubSource("pkulaw", _evidence("pkulaw"))
        router = ProvisionRouter(local, [gov, pkulaw])
        result = router.resolve(ProvisionQuery(
            law_title="外部法", article_label="第一条"
        ))
        assert result.evidence.provider == "gov_search"
        assert pkulaw.calls == []

    def test_error_does_not_break_chain(self, local):
        gov = StubSource("gov_search", error=RuntimeError("接口超时"))
        pkulaw = StubSource("pkulaw", _evidence("pkulaw"))
        router = ProvisionRouter(local, [gov, pkulaw])
        result = router.resolve(ProvisionQuery(
            law_title="外部法", article_label="第一条"
        ))
        assert result.status == RetrievalStatus.FOUND
        assert result.evidence.provider == "pkulaw"
        assert result.errors and "接口超时" in result.errors[0]

    def test_all_miss(self, local):
        router = ProvisionRouter(local, [StubSource("gov_search", None)])
        result = router.resolve(ProvisionQuery(
            law_title="外部法", article_label="第一条"
        ))
        assert result.status == RetrievalStatus.NOT_FOUND
        assert result.providers_tried == ["local", "gov_search"]


class TestQueriesFromClaims:
    CLAIMS_DOC = {
        "claims": [
            {
                "claim_id": "cl_00001",
                "claim_type": "legal_source_claim",
                "entities": {
                    "legal_sources": [
                        {
                            "title": "中华人民共和国民法典",
                            "source_type": "law",
                            "articles": [
                                {"article": "第一千零八十四条",
                                 "paragraphs": ["第三款"], "items": []},
                                {"article": "第五百七十七条",
                                 "paragraphs": [], "items": []},
                            ],
                        },
                        {
                            "title": "最高人民法院关于适用《中华人民共和国民法典》婚姻家庭编的解释（一）",
                            "source_type": "judicial_interpretation",
                            "articles": [],
                        },
                    ],
                },
            },
            {
                "claim_id": "cl_00002",
                "claim_type": "legal_source_paraphrase",
                "entities": {
                    "legal_sources": [
                        {"title": "反不正当竞争法", "source_type": "law",
                         "articles": [{"article": "第九条",
                                       "paragraphs": [], "items": []}]},
                    ],
                    "paraphrase_text": "经营者不得实施侵犯商业秘密的行为",
                },
            },
            {
                "claim_id": "cl_00003",
                "claim_type": "case_citation",
                "entities": {"case_refs": []},
            },
        ],
    }

    def test_expansion(self):
        queries = queries_from_claims(self.CLAIMS_DOC)
        # 2 条文 + 1 法规级 + 1 转述 = 4；案例 claim 不展开
        assert len(queries) == 4
        assert queries[0].article_label == "第一千零八十四条"
        assert queries[0].paragraph_labels == ["第三款"]
        assert queries[2].article_label is None
        assert queries[2].source_type == "judicial_interpretation"

    def test_paraphrase_quote_text(self):
        queries = queries_from_claims(self.CLAIMS_DOC)
        assert queries[3].quote_text == "经营者不得实施侵犯商业秘密的行为"
        assert queries[3].claim_id == "cl_00002"
