"""statutedb 入库与检索测试（内存库全链路）。"""

from __future__ import annotations

import pytest

from statutedb.db import init_db
from statutedb.importer import import_statute
from statutedb.law_parser import parse_law_lines
from statutedb.store import StatuteStore

MINI_LAW_LINES = [
    "中华人民共和国测试法",
    "（2020年5月28日第十三届全国人民代表大会第三次会议通过　自2021年1月1日起施行）",
    "第一编　总则",
    "第一章　基本规定",
    "第一条　为了测试本系统，制定本法。",
    "第二条　本法所称测试，包括下列情形：",
    "（一）单元测试；",
    "（二）集成测试。",
    "第三条　测试应当遵循自愿、公平原则。",
    "任何组织或者个人不得干扰测试。",
    "第三条之一　自动化测试参照前条执行。",
    "第二章　附则",
    "第四条　本法自2021年1月1日起施行。",
]


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    doc = parse_law_lines(MINI_LAW_LINES)
    import_statute(
        conn, doc, "law",
        source_url="https://flk.npc.gov.cn/test",
        extra_aliases=["测试法典"],
    )
    yield StatuteStore(conn)
    conn.close()


class TestResolveLaw:
    def test_full_title(self, store):
        assert store.resolve_law("中华人民共和国测试法").title == "中华人民共和国测试法"

    def test_short_title(self, store):
        assert store.resolve_law("测试法") is not None

    def test_with_brackets(self, store):
        """引注带书名号也能命中。"""
        assert store.resolve_law("《中华人民共和国测试法》") is not None

    def test_with_version_note(self, store):
        """引注带版本注记：查询变体剥离注记后命中。"""
        assert store.resolve_law("测试法（2020年）") is not None

    def test_manual_alias(self, store):
        assert store.resolve_law("测试法典") is not None

    def test_unknown_law(self, store):
        assert store.resolve_law("不存在的法") is None


class TestGetArticle:
    def test_exact(self, store):
        law = store.resolve_law("测试法")
        art = store.get_article(law.law_id, 3, 0)
        assert art.article_label == "第三条"
        assert "自愿、公平" in art.text
        # 第二款并入全条文本
        assert "不得干扰测试" in art.text

    def test_suffix_article(self, store):
        law = store.resolve_law("测试法")
        art = store.get_article_by_label(law.law_id, "第三条之一")
        assert art is not None
        assert art.article_num == 3 and art.article_suffix == 1

    def test_missing_article(self, store):
        law = store.resolve_law("测试法")
        assert store.get_article(law.law_id, 99, 0) is None

    def test_bad_label(self, store):
        law = store.resolve_law("测试法")
        assert store.get_article_by_label(law.law_id, "第X条") is None

    def test_section_path(self, store):
        law = store.resolve_law("测试法")
        art = store.get_article(law.law_id, 4, 0)
        assert "第二章 附则" in art.section_path


class TestClauses:
    def test_items(self, store):
        law = store.resolve_law("测试法")
        art = store.get_article(law.law_id, 2, 0)
        clauses = store.get_clauses(art.article_id)
        # 一款 + 两项
        assert [(c.para_num, c.item_num) for c in clauses] == [(1, 0), (1, 1), (1, 2)]
        assert clauses[2].text.startswith("（二）")

    def test_filter_by_para(self, store):
        law = store.resolve_law("测试法")
        art = store.get_article(law.law_id, 3, 0)
        second_para = store.get_clauses(art.article_id, para_num=2)
        assert len(second_para) == 1
        assert "不得干扰" in second_para[0].text


class TestFulltext:
    def test_hit(self, store):
        hits = store.search_fulltext("自愿、公平原则")
        assert hits and hits[0].article.article_label == "第三条"

    def test_scoped_to_law(self, store):
        law = store.resolve_law("测试法")
        assert store.search_fulltext("单元测试", law_id=law.law_id)

    def test_too_short(self, store):
        assert store.search_fulltext("测") == []

    def test_no_hit(self, store):
        assert store.search_fulltext("完全无关的一段话呀") == []


class TestReimport:
    def test_replace_on_reimport(self, store):
        """同名重导入 = 版本更新，整体替换。"""
        lines = list(MINI_LAW_LINES) + ["第五条　新增条文内容。"]
        doc = parse_law_lines(lines)
        import_statute(store.conn, doc, "law")
        law = store.resolve_law("测试法")
        assert store.article_count(law.law_id) == 6
        assert store.get_article(law.law_id, 5, 0) is not None
        # 旧条文仍在（替换而非叠加）
        assert len(store.list_laws()) == 1
        # FTS 与新内容一致
        assert store.search_fulltext("新增条文内容")
