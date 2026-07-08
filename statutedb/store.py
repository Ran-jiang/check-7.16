"""
本地法条库检索 API。

主路径全部走精确索引：
  法规名 → normalizer.query_variants → law_aliases 精确命中 → law_id
  条款号 → cn_num.parse_article_label → (num, suffix) 唯一索引直取
全文检索（trigram FTS）仅用于条号缺失/错误时按转述文本反查候选。
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from pydantic import BaseModel

from .cn_num import parse_article_label
from .normalizer import query_variants


class LawRecord(BaseModel):
    law_id: int
    title: str
    source_type: str
    doc_number: Optional[str] = None
    issuing_authority: Optional[str] = None
    promulgated_on: Optional[str] = None
    effective_on: Optional[str] = None
    version_note: Optional[str] = None
    status: str = "effective"
    source_url: Optional[str] = None
    source_hash: Optional[str] = None


class ArticleRecord(BaseModel):
    article_id: int
    law_id: int
    article_num: int
    article_suffix: int
    article_label: str
    section_path: str
    text: str


class ClauseRecord(BaseModel):
    clause_id: int
    article_id: int
    para_num: int
    item_num: int
    text: str


class FtsHit(BaseModel):
    """全文反查命中：条文 + 所属法规 + bm25 得分（越小越相关）。"""
    article: ArticleRecord
    law_title: str
    score: float


class StatuteStore:
    """本地法条库只读检索接口。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- 法规名解析 ----

    def resolve_law(self, title: str) -> Optional[LawRecord]:
        """
        引注法规名 → LawRecord。

        按 query_variants 优先级逐一在别名表精确匹配；
        全部未中返回 None（表示该法规不在本地库，路由器下探）。
        """
        for variant in query_variants(title):
            row = self.conn.execute(
                """SELECT l.* FROM law_aliases a
                   JOIN laws l ON l.law_id = a.law_id
                   WHERE a.alias_norm = ?""",
                (variant,),
            ).fetchone()
            if row:
                return LawRecord(**dict(row))
        return None

    # ---- 条款直取 ----

    def get_article(
        self, law_id: int, article_num: int, article_suffix: int = 0
    ) -> Optional[ArticleRecord]:
        row = self.conn.execute(
            """SELECT * FROM articles
               WHERE law_id=? AND article_num=? AND article_suffix=?""",
            (law_id, article_num, article_suffix),
        ).fetchone()
        return ArticleRecord(**dict(row)) if row else None

    def get_article_by_label(
        self, law_id: int, article_label: str
    ) -> Optional[ArticleRecord]:
        """"第一百八十四条之一" 形式的标签直取。标签不合法返回 None。"""
        parsed = parse_article_label(article_label)
        if parsed is None:
            return None
        return self.get_article(law_id, parsed[0], parsed[1])

    def get_clauses(
        self,
        article_id: int,
        para_num: Optional[int] = None,
        item_num: Optional[int] = None,
    ) -> list[ClauseRecord]:
        """取条下的款/项；para_num/item_num 缺省时返回全部。"""
        sql = "SELECT * FROM clauses WHERE article_id=?"
        params: list = [article_id]
        if para_num is not None:
            sql += " AND para_num=?"
            params.append(para_num)
        if item_num is not None:
            sql += " AND item_num=?"
            params.append(item_num)
        sql += " ORDER BY para_num, item_num"
        rows = self.conn.execute(sql, params).fetchall()
        return [ClauseRecord(**dict(r)) for r in rows]

    # ---- 全文反查兜底 ----

    def search_fulltext(
        self,
        text: str,
        law_id: Optional[int] = None,
        limit: int = 5,
    ) -> list[FtsHit]:
        """
        按文本片段反查候选条文（trigram FTS，bm25 排序）。

        trigram 分词要求查询至少 3 个字符，过短查询直接返回空。
        查询文本作为整体短语匹配（加引号），避免 FTS 语法字符干扰。
        """
        text = text.strip()
        if len(text) < 3:
            return []
        # FTS5 短语查询：内部双引号转义
        phrase = '"' + text.replace('"', '""') + '"'
        sql = """SELECT a.*, l.title AS law_title, bm25(articles_fts) AS score
                 FROM articles_fts f
                 JOIN articles a ON a.article_id = f.rowid
                 JOIN laws l ON l.law_id = a.law_id
                 WHERE articles_fts MATCH ?"""
        params: list = [phrase]
        if law_id is not None:
            sql += " AND a.law_id = ?"
            params.append(law_id)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        hits: list[FtsHit] = []
        for r in rows:
            d = dict(r)
            law_title = d.pop("law_title")
            score = d.pop("score")
            hits.append(FtsHit(
                article=ArticleRecord(**d),
                law_title=law_title,
                score=score,
            ))
        return hits

    # ---- 管理 ----

    def list_laws(self) -> list[LawRecord]:
        rows = self.conn.execute(
            "SELECT * FROM laws ORDER BY law_id"
        ).fetchall()
        return [LawRecord(**{k: r[k] for k in r.keys()
                             if k != "imported_at"}) for r in rows]

    def article_count(self, law_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE law_id=?", (law_id,)
        ).fetchone()
        return row["n"]
