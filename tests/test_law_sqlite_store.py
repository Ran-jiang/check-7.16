from pathlib import Path

from laws.sqlite_store import (
    find_current_article,
    init_db,
    seed_common_laws,
    connect,
    upsert_article,
    upsert_law,
)


def test_seed_common_laws_and_alias_lookup(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    count = seed_common_laws(db_path)

    assert count >= 40

    with connect(db_path) as conn:
        law = conn.execute(
            "SELECT * FROM laws WHERE normalized_title = ?",
            ("中华人民共和国劳动合同法",),
        ).fetchone()
        assert law is not None

        alias = conn.execute(
            "SELECT * FROM law_aliases WHERE alias = ?",
            ("劳动合同法",),
        ).fetchone()
        assert alias is not None
        assert alias["law_id"] == law["id"]


def test_import_bundle_and_find_current_article(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn,
            {
                "title": "中华人民共和国劳动合同法",
                "source_type": "law",
                "authority": "全国人大常委会",
                "category": "劳动用工",
                "status": "has_articles",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第三十七条",
                "text": "劳动者提前三十日以书面形式通知用人单位，可以解除劳动合同。",
                "version_label": "现行有效",
                "version_status": "effective",
                "source_name": "国家法律法规数据库",
                "source_url": "https://flk.npc.gov.cn/",
                "source_fetched_at": "2026-07-09T00:00:00+08:00",
            },
        )

    with connect(db_path) as conn:
        article = find_current_article(conn, "劳动合同法", "第三十七条")
        assert article is not None
        assert article["title"] == "中华人民共和国劳动合同法"
        assert "提前三十日" in article["text"]


def test_find_current_article_selects_version_by_effective_date(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn,
            {
                "title": "中华人民共和国商标法",
                "source_type": "law",
                "status": "has_articles",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第一条",
                "text": "现行版本。",
                "version_key": "2019-04-23",
                "version_label": "现行有效",
                "version_status": "effective",
                "effective_from": "2019-04-23",
                "effective_to": "2027-01-01",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第一条",
                "text": "未来版本。",
                "version_key": "2027-01-01",
                "version_label": "尚未生效",
                "version_status": "future_effective",
                "effective_from": "2027-01-01",
            },
        )

    with connect(db_path) as conn:
        current = find_current_article(conn, "商标法", "第一条", as_of="2026-07-10")
        future = find_current_article(conn, "商标法", "第一条", as_of="2027-01-01")

    assert current["text"] == "现行版本。"
    assert current["version_key"] == "2019-04-23"
    assert future["text"] == "未来版本。"
    assert future["version_status"] == "future_effective"
