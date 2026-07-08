"""
法规入库：StatuteDoc → SQLite。

同名法规重复导入视为版本更新：整体替换条文与别名（法条更新监控
的落地方式就是重新下载官方文本再导入，source_hash 变化即有修订）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .law_parser import StatuteDoc
from .normalizer import alias_variants, normalize_title


def import_statute(
    conn: sqlite3.Connection,
    doc: StatuteDoc,
    source_type: str,
    source_url: Optional[str] = None,
    extra_aliases: Optional[list[str]] = None,
    issuing_authority: Optional[str] = None,
) -> int:
    """
    导入一部法规（同名则整体替换），返回 law_id。

    Args:
        conn: 已建表的连接
        doc: 解析结果
        source_type: law / judicial_interpretation / other_normative_document
        source_url: 官方来源 URL
        extra_aliases: 人工补充的简称（如"民法典总则编解释"）
        issuing_authority: 制定机关
    """
    if not doc.articles:
        raise ValueError(f"《{doc.title}》未解析到任何条文，拒绝入库")

    title = normalize_title(doc.title)
    source_hash = _content_hash(doc)
    now = datetime.now(timezone.utc).isoformat()

    cur = conn.cursor()
    existing = cur.execute(
        "SELECT law_id FROM laws WHERE title = ?", (title,)
    ).fetchone()

    if existing:
        law_id = existing["law_id"]
        # 整体替换：级联删除旧条文/款项/别名，FTS 由触发式重建
        cur.execute("DELETE FROM articles WHERE law_id = ?", (law_id,))
        cur.execute("DELETE FROM law_aliases WHERE law_id = ?", (law_id,))
        cur.execute(
            """UPDATE laws SET source_type=?, doc_number=?, issuing_authority=?,
               promulgated_on=?, effective_on=?, version_note=?,
               source_url=?, source_hash=?, imported_at=?
               WHERE law_id=?""",
            (source_type, doc.doc_number, issuing_authority,
             doc.promulgated_on, doc.effective_on, doc.version_note,
             source_url, source_hash, now, law_id),
        )
        # 外部内容 FTS 不随 DELETE 自动清理，重建最稳妥
        cur.execute(
            "INSERT INTO articles_fts(articles_fts) VALUES ('rebuild')"
        )
    else:
        cur.execute(
            """INSERT INTO laws (title, source_type, doc_number,
               issuing_authority, promulgated_on, effective_on, version_note,
               source_url, source_hash, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (title, source_type, doc.doc_number, issuing_authority,
             doc.promulgated_on, doc.effective_on, doc.version_note,
             source_url, source_hash, now),
        )
        law_id = cur.lastrowid

    # ---- 别名 ----
    aliases = set(alias_variants(title))
    for alias in extra_aliases or []:
        aliases.update(alias_variants(alias))
    for alias in aliases:
        cur.execute(
            "INSERT OR IGNORE INTO law_aliases (alias_norm, law_id) VALUES (?,?)",
            (alias, law_id),
        )

    # ---- 条文与款项 ----
    for art in doc.articles:
        cur.execute(
            """INSERT INTO articles (law_id, article_num, article_suffix,
               article_label, section_path, text) VALUES (?,?,?,?,?,?)""",
            (law_id, art.article_num, art.article_suffix,
             art.article_label, art.section_path, art.full_text),
        )
        article_id = cur.lastrowid
        cur.execute(
            "INSERT INTO articles_fts(rowid, text) VALUES (?,?)",
            (article_id, art.full_text),
        )
        for para_num, para in enumerate(art.paragraphs, start=1):
            cur.execute(
                """INSERT INTO clauses (article_id, para_num, item_num, text)
                   VALUES (?,?,?,?)""",
                (article_id, para_num, 0, para.text),
            )
            for item_num, item_text in enumerate(para.items, start=1):
                cur.execute(
                    """INSERT INTO clauses (article_id, para_num, item_num, text)
                       VALUES (?,?,?,?)""",
                    (article_id, para_num, item_num, item_text),
                )

    conn.commit()
    return law_id


def _content_hash(doc: StatuteDoc) -> str:
    """条文全文 SHA-256，用于监控官方文本是否有修订。"""
    h = hashlib.sha256()
    for art in doc.articles:
        h.update(art.full_text.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
