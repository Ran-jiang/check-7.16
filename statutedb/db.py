"""
SQLite schema 与连接管理。

设计要点（检索速度优先）：
  - laws + law_aliases：法规名经规范化别名表精确命中，O(1)
  - articles：一行一条，(law_id, article_num, article_suffix) 唯一索引直取
  - clauses：款/项细粒度行，引注到"第X条第2款第（三）项"时取用
  - articles_fts：trigram 分词的 FTS5 外部内容表，仅做条号缺失/错误时
    的转述文本反查（中文无需分词器词典，trigram 对子串匹配足够）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "0.3"

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "laws.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS laws (
  law_id            INTEGER PRIMARY KEY,
  title             TEXT NOT NULL UNIQUE,
  source_type       TEXT NOT NULL,
  doc_number        TEXT,
  issuing_authority TEXT,
  promulgated_on    TEXT,
  effective_on      TEXT,
  version_note      TEXT,
  status            TEXT NOT NULL DEFAULT 'effective',
  source_url        TEXT,
  source_hash       TEXT,
  imported_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS law_aliases (
  alias_norm TEXT NOT NULL,
  law_id     INTEGER NOT NULL REFERENCES laws(law_id) ON DELETE CASCADE,
  PRIMARY KEY (alias_norm, law_id)
);

CREATE TABLE IF NOT EXISTS articles (
  article_id     INTEGER PRIMARY KEY,
  law_id         INTEGER NOT NULL REFERENCES laws(law_id) ON DELETE CASCADE,
  article_num    INTEGER NOT NULL,
  article_suffix INTEGER NOT NULL DEFAULT 0,
  article_label  TEXT NOT NULL,
  section_path   TEXT NOT NULL DEFAULT '',
  text           TEXT NOT NULL,
  UNIQUE (law_id, article_num, article_suffix)
);

CREATE TABLE IF NOT EXISTS clauses (
  clause_id  INTEGER PRIMARY KEY,
  article_id INTEGER NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
  para_num   INTEGER NOT NULL,
  item_num   INTEGER NOT NULL DEFAULT 0,
  text       TEXT NOT NULL,
  UNIQUE (article_id, para_num, item_num)
);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
  text,
  content='articles',
  content_rowid='article_id',
  tokenize='trigram'
);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """打开连接（不建表）。外键约束默认开启。"""
    path = Path(db_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """建库建表（幂等），返回连接。"""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn
