import base64
import importlib
from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from laws.sqlite_store import connect, init_db, upsert_article, upsert_law


def test_word_addin_document_check_api(tmp_path, monkeypatch):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as connection:
        law_id = upsert_law(
            connection,
            {"title": "中华人民共和国民法典", "source_type": "law"},
        )
        upsert_article(
            connection,
            law_id,
            {
                "article_no": "第五百七十七条",
                "text": "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担违约责任。",
            },
        )

    document = Document()
    document.add_paragraph("依据《中华人民共和国民法典》第五百七十七条，被告应当承担违约责任。")
    buffer = BytesIO()
    document.save(buffer)

    api_module = importlib.import_module("api.app")
    monkeypatch.setattr(api_module, "LAW_DB", db_path)
    client = TestClient(api_module.app)
    response = client.post(
        "/api/checks",
        json={
            "file_name": "test.docx",
            "docx_base64": base64.b64encode(buffer.getvalue()).decode(),
            "semantic_check": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["semantic_check"] is False
    assert payload["summary"]["total"] == 1
    assert payload["verification"]["legal_checks"][0]["exact_comparison"] is not None
