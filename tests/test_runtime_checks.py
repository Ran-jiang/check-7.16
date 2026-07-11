from pathlib import Path

from laws.sqlite_store import connect, init_db, upsert_article, upsert_law
from runtime_checks import check_runtime


def test_check_runtime_reports_ready_law_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(conn, {"title": "中华人民共和国测试法", "source_type": "law"})
        upsert_article(conn, law_id, {"article_no": "第一条", "text": "测试。"})

    monkeypatch.setenv("PKULAW_ACCESS_TOKEN", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")

    results = check_runtime(db_path)

    assert all(result.ok for result in results)
    assert "1 laws" in results[0].message
    assert results[1].message == "optional semantic checks not configured"
    assert results[2].message == "optional fallback not configured"


def test_check_runtime_reports_missing_db(tmp_path: Path):
    results = check_runtime(tmp_path / "missing.sqlite")

    assert not results[0].ok
