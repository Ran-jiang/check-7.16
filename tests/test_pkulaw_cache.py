"""北大法宝缓存层与并发去重的测试。"""

from __future__ import annotations


import pytest

from verification.pkulaw_cache import (
    CachedPkulawClient,
    TTL_SECONDS,
    cache_clear,
    cache_status,
    connect_cache,
)
from verification.pkulaw_mcp import (
    PkulawArticle,
    PkulawLawRecord,
    PkulawNotFoundError,
)


class FakeClient:
    """记录调用次数的假 MCP 客户端。"""

    def __init__(self, timeliness=("现行有效",)):
        self.article_calls = 0
        self.list_calls = 0
        self.timeliness = list(timeliness)

    def get_law_item_content(self, title, article_no):
        self.article_calls += 1
        if "不存在" in title:
            raise PkulawNotFoundError("未找到数据")
        return PkulawArticle(
            title=title,
            url="https://www.pkulaw.com/x",
            timeliness=self.timeliness,
            article_no=article_no,
            article_text="条文内容",
        )

    def get_law_list(self, title="", fulltext=""):
        self.list_calls += 1
        if "不存在" in title:
            raise PkulawNotFoundError("未找到数据")
        return [PkulawLawRecord(title=title, url="https://x", timeliness=self.timeliness)]


@pytest.fixture()
def cache_db(tmp_path):
    return tmp_path / "cache.sqlite"


def test_effective_article_cached(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    first = cached.get_law_item_content("中华人民共和国民法典", "第五百七十七条")
    second = cached.get_law_item_content("中华人民共和国民法典", "第五百七十七条")
    assert fake.article_calls == 1
    assert second.article_text == first.article_text == "条文内容"
    assert second.url == "https://www.pkulaw.com/x"


def test_not_found_negative_cached(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    for _ in range(2):
        with pytest.raises(PkulawNotFoundError):
            cached.get_law_item_content("不存在的法", "第一条")
    assert fake.article_calls == 1


def test_repealed_law_list_stores_minimal_payload(cache_db):
    fake = FakeClient(timeliness=("废止或失效",))
    cached = CachedPkulawClient(fake, cache_db)
    cached.get_law_list(title="中华人民共和国合同法")
    cached.get_law_list(title="中华人民共和国合同法")
    assert fake.list_calls == 1
    with connect_cache(cache_db) as conn:
        row = conn.execute("SELECT status, payload FROM cache_entries").fetchone()
    assert row["status"] == "repealed"
    # 按约定：废止条目只存法名+时效，不存链接等完整元数据
    assert "url" not in row["payload"]
    assert "废止或失效" in row["payload"]


def test_expired_effective_entry_revalidates_instead_of_refetch(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    cached.get_law_item_content("中华人民共和国民法典", "第五百七十七条")
    # 人为把 verified_at 拨回过期
    with connect_cache(cache_db) as conn:
        conn.execute(
            "UPDATE cache_entries SET verified_at = verified_at - ?",
            (TTL_SECONDS["effective"] + 10,),
        )
        conn.commit()
    result = cached.get_law_item_content("中华人民共和国民法典", "第五百七十七条")
    # 只做了一次轻量时效验证（get_law_list），没有重拉条文全文
    assert fake.article_calls == 1
    assert fake.list_calls == 1
    assert result.article_text == "条文内容"


def test_cache_status_and_clear(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    cached.get_law_item_content("中华人民共和国民法典", "第五百七十七条")
    info = cache_status(cache_db)
    assert info["groups"] and info["groups"][0]["n"] == 1
    assert cache_clear(cache_db) == 1
    assert cache_status(cache_db)["groups"] == []
