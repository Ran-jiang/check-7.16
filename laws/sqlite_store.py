"""SQLite-backed local law repository."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


SCHEMA_VERSION = "1.1"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        _create_schema(conn)


def seed_common_laws(db_path: str | Path, catalog_path: str | Path | None = None) -> int:
    if catalog_path is None:
        catalog_path = Path(__file__).with_name("common_laws.json")
    records = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    init_db(db_path)
    with connect(db_path) as conn:
        count = 0
        for record in records:
            upsert_law(conn, record)
            count += 1
        return count


def find_law(conn: sqlite3.Connection, title: str) -> Optional[sqlite3.Row]:
    normalized = normalize_title(title)
    row = conn.execute(
        "SELECT * FROM laws WHERE normalized_title = ? LIMIT 1",
        (normalized,),
    ).fetchone()
    if row:
        return row
    return conn.execute(
        """
        SELECT l.*
        FROM law_aliases a
        JOIN laws l ON l.id = a.law_id
        WHERE a.alias = ?
        LIMIT 1
        """,
        (normalized,),
    ).fetchone()


def find_current_article(
    conn: sqlite3.Connection,
    title: str,
    article_no: str,
    as_of: str | date | None = None,
) -> Optional[sqlite3.Row]:
    law = find_law(conn, title)
    if not law:
        return None
    as_of_text = _date_text(as_of) or date.today().isoformat()
    return conn.execute(
        """
        SELECT
          l.title,
          l.source_type,
          l.status AS law_status,
          a.article_no,
          a.article_key,
          a.text,
          a.version_key,
          a.version_label,
          a.version_status,
          a.source_name,
          a.source_url,
          a.source_fetched_at,
          a.timeliness,
          a.effectiveness,
          a.issued_at,
          a.effective_from,
          a.effective_to,
          a.effective_at
        FROM articles a
        JOIN laws l ON l.id = a.law_id
        WHERE a.law_id = ?
          AND a.article_key = ?
          AND (a.effective_from IS NULL OR a.effective_from <= ?)
          AND (a.effective_to IS NULL OR a.effective_to > ?)
        ORDER BY
          CASE WHEN a.effective_from IS NULL THEN 0 ELSE 1 END DESC,
          a.effective_from DESC,
          a.id DESC
        LIMIT 1
        """,
        (law["id"], normalize_article_key(article_no), as_of_text, as_of_text),
    ).fetchone()


def list_current_articles(
    conn: sqlite3.Connection,
    title: str,
    as_of: str | date | None = None,
) -> list[sqlite3.Row]:
    """Return the latest effective version of every article in a law."""
    law = find_law(conn, title)
    if not law:
        return []
    as_of_text = _date_text(as_of) or date.today().isoformat()
    rows = conn.execute(
        """
        SELECT
          l.title,
          l.source_type,
          l.status AS law_status,
          a.article_no,
          a.article_key,
          a.text,
          a.version_key,
          a.version_label,
          a.version_status,
          a.source_name,
          a.source_url,
          a.source_fetched_at,
          a.timeliness,
          a.effectiveness,
          a.issued_at,
          a.effective_from,
          a.effective_to,
          a.effective_at
        FROM articles a
        JOIN laws l ON l.id = a.law_id
        WHERE a.law_id = ?
          AND (a.effective_from IS NULL OR a.effective_from <= ?)
          AND (a.effective_to IS NULL OR a.effective_to > ?)
        ORDER BY
          a.article_key,
          CASE WHEN a.effective_from IS NULL THEN 0 ELSE 1 END DESC,
          a.effective_from DESC,
          a.id DESC
        """,
        (law["id"], as_of_text, as_of_text),
    ).fetchall()
    latest_by_article: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest_by_article.setdefault(row["article_key"], row)
    return list(latest_by_article.values())


def upsert_law(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    now = _now()
    title = record["title"].strip()
    normalized_title = normalize_title(title)
    status = record.get("status", "catalog_only")
    conn.execute(
        """
        INSERT INTO laws (
          title, normalized_title, source_type, authority, category,
          priority, status, source_url, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(normalized_title) DO UPDATE SET
          title = excluded.title,
          source_type = excluded.source_type,
          authority = COALESCE(excluded.authority, laws.authority),
          category = COALESCE(excluded.category, laws.category),
          priority = COALESCE(excluded.priority, laws.priority),
          status = CASE
            WHEN laws.status = 'has_articles' AND excluded.status = 'catalog_only'
            THEN laws.status
            ELSE excluded.status
          END,
          source_url = COALESCE(excluded.source_url, laws.source_url),
          updated_at = excluded.updated_at
        """,
        (
            title,
            normalized_title,
            record.get("source_type", "other_normative_document"),
            record.get("authority"),
            record.get("category"),
            record.get("priority"),
            status,
            record.get("source_url"),
            now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM laws WHERE normalized_title = ?",
        (normalized_title,),
    ).fetchone()
    law_id = int(row["id"])
    aliases = set(record.get("aliases", [])) | set(generate_aliases(title))
    for alias in aliases:
        upsert_alias(conn, law_id, alias, canonical_title=title)
    return law_id


def upsert_alias(
    conn: sqlite3.Connection,
    law_id: int,
    alias: str,
    canonical_title: str | None = None,
) -> None:
    normalized_alias = normalize_title(alias)
    if not normalized_alias:
        return
    if canonical_title and normalized_alias == normalize_title(canonical_title):
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO law_aliases (alias, law_id)
        VALUES (?, ?)
        """,
        (normalized_alias, law_id),
    )


def upsert_article(conn: sqlite3.Connection, law_id: int, record: dict[str, Any]) -> int:
    article_no = record["article_no"].strip()
    article_key = normalize_article_key(article_no)
    version_label = record.get("version_label")
    version_status = record.get("version_status")
    effective_from = record.get("effective_from")
    version_key = normalize_version_key(record.get("version_key") or effective_from or "current")
    text = record.get("text", "").strip()
    now = _now()
    conn.execute(
        """
        INSERT INTO articles (
          law_id, article_no, article_key, version_key, version_label,
          version_status, text, source_name, source_url, source_fetched_at,
          timeliness, effectiveness, issued_at, effective_from, effective_to,
          effective_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(law_id, article_key, version_key) DO UPDATE SET
          article_no = excluded.article_no,
          version_label = COALESCE(excluded.version_label, articles.version_label),
          version_status = COALESCE(excluded.version_status, articles.version_status),
          text = excluded.text,
          source_name = COALESCE(excluded.source_name, articles.source_name),
          source_url = COALESCE(excluded.source_url, articles.source_url),
          source_fetched_at = COALESCE(excluded.source_fetched_at, articles.source_fetched_at),
          timeliness = COALESCE(excluded.timeliness, articles.timeliness),
          effectiveness = COALESCE(excluded.effectiveness, articles.effectiveness),
          issued_at = COALESCE(excluded.issued_at, articles.issued_at),
          effective_from = COALESCE(excluded.effective_from, articles.effective_from),
          effective_to = excluded.effective_to,
          effective_at = COALESCE(excluded.effective_at, articles.effective_at),
          updated_at = excluded.updated_at
        """,
        (
            law_id,
            article_no,
            article_key,
            version_key,
            version_label,
            version_status,
            text,
            record.get("source_name"),
            record.get("source_url"),
            record.get("source_fetched_at"),
            version_label,
            record.get("effectiveness"),
            record.get("issued_at"),
            effective_from,
            record.get("effective_to"),
            effective_from,
            now,
        ),
    )
    conn.execute("UPDATE laws SET status = 'has_articles', updated_at = ? WHERE id = ?", (now, law_id))
    row = conn.execute(
        "SELECT id FROM articles WHERE law_id = ? AND article_key = ? AND version_key = ?",
        (law_id, article_key, version_key),
    ).fetchone()
    return int(row["id"])


def normalize_title(title: str) -> str:
    return "".join(title.split()).replace("《", "").replace("》", "").replace("〈", "").replace("〉", "")


def normalize_article_key(article_no: str) -> str:
    return "".join(article_no.split()).replace("第", "").replace("条", "")


def normalize_version_key(version_key: str) -> str:
    return "".join(version_key.split()) or "current"


def generate_aliases(title: str) -> Iterable[str]:
    if title.startswith("中华人民共和国") and len(title) > len("中华人民共和国") + 1:
        yield title.replace("中华人民共和国", "", 1)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS schema_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS laws (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          normalized_title TEXT NOT NULL UNIQUE,
          source_type TEXT NOT NULL,
          authority TEXT,
          category TEXT,
          priority INTEGER,
          status TEXT NOT NULL DEFAULT 'catalog_only',
          source_url TEXT,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS law_aliases (
          alias TEXT PRIMARY KEY,
          law_id INTEGER NOT NULL REFERENCES laws(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS articles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          law_id INTEGER NOT NULL REFERENCES laws(id) ON DELETE CASCADE,
          article_no TEXT NOT NULL,
          article_key TEXT NOT NULL,
          version_key TEXT NOT NULL DEFAULT 'current',
          version_label TEXT,
          version_status TEXT,
          text TEXT NOT NULL,
          source_name TEXT,
          source_url TEXT,
          source_fetched_at TEXT,
          timeliness TEXT,
          effectiveness TEXT,
          issued_at TEXT,
          effective_from TEXT,
          effective_to TEXT,
          effective_at TEXT,
          updated_at TEXT NOT NULL,
          UNIQUE(law_id, article_key, version_key)
        );

        CREATE INDEX IF NOT EXISTS idx_laws_priority ON laws(priority);
        CREATE INDEX IF NOT EXISTS idx_articles_law_key ON articles(law_id, article_key, effective_from);

        INSERT OR REPLACE INTO schema_meta (key, value)
        VALUES ('schema_version', '{SCHEMA_VERSION}');
        """
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_text(value: str | date | None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]
