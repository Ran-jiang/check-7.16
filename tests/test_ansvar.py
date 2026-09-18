"""Ansvar MCP 客户端与多法域法规适配器测试（不访问网络）。"""

from pathlib import Path

import pytest

from ccitecheck.domain.evidence import LookupStatus
from ccitecheck.retrieval.sources.ansvar.auth import AnsvarOAuth
from ccitecheck.retrieval.sources.ansvar.client import (
    AnsvarMcpClient,
    AnsvarMcpError,
    AnsvarQuotaExceededError,
    AnsvarRecord,
    _classify_jsonrpc_error,
    _map_args_to_schema,
    _parse_search_response,
)
from ccitecheck.retrieval.sources.ansvar.statutes import AnsvarSource, _pick_match
from ccitecheck.retrieval.sources.base import LookupRequest
from ccitecheck.verification.statutes.deterministic import assess_statute


class FakeAnsvarClient:
    def __init__(self, records=None, article=None, article_error=None):
        self.records = records or []
        self.article = article
        self.article_error = article_error
        self.search_calls = []
        self.article_calls = []

    def search_law(self, query, jurisdiction=""):
        self.search_calls.append((query, jurisdiction))
        return self.records

    def get_article_text(
        self, identifier, article_number, *, jurisdiction="", lookup_arguments=None
    ):
        self.article_calls.append(
            (identifier, article_number, jurisdiction, lookup_arguments)
        )
        if self.article_error:
            raise self.article_error
        return self.article


def _request(article_no="第二条", jurisdiction="DE"):
    return LookupRequest(
        law_title="Urheberrechtsgesetz",
        article_no=article_no,
        jurisdiction=jurisdiction,
    )


def test_client_requires_explicit_gateway(monkeypatch):
    monkeypatch.setenv("ANSVAR_MCP_GATEWAY", "")
    with pytest.raises(AnsvarMcpError):
        AnsvarMcpClient()


def test_search_parser_preserves_exact_lookup_hint():
    payload = {"result": {"structuredContent": {"results": [{
        "law_title": "Urheberrechtsgesetz",
        "canonical_ref": "DE:UrhG:2",
        "jurisdiction": "DE",
        "_citation": {
            "source_url": "https://www.gesetze-im-internet.de/urhg/__2.html",
            "publisher": "BMJ",
            "license": "DL-DE-BY-2.0",
            "lookup": {
                "tool": "get_provision",
                "arguments": {"canonical_ref": "DE:UrhG:2"},
            },
        },
    }]}}}
    record = _parse_search_response(payload)[0]
    assert record.identifier == "DE:UrhG:2"
    assert record.lookup_arguments == {"canonical_ref": "DE:UrhG:2"}
    assert record.publisher == "BMJ"


def test_schema_mapping_preserves_declared_types():
    schema = {"properties": {
        "query": {"type": "string"},
        "jurisdiction": {"type": "string"},
        "limit": {"type": "integer"},
    }}
    assert _map_args_to_schema(
        schema, {"query": "copyright", "jurisdictions": ["DE"], "limit": 5}
    ) == {"query": "copyright", "jurisdiction": "DE", "limit": 5}


def test_only_cap_exceeded_is_classified_as_quota():
    quota = _classify_jsonrpc_error({
        "code": -32000,
        "message": "request refused",
        "data": {"cause": "cap_exceeded"},
    })
    generic = _classify_jsonrpc_error({"code": -32000, "message": "source unavailable"})
    assert isinstance(quota, AnsvarQuotaExceededError)
    assert type(generic) is AnsvarMcpError


def test_source_uses_lookup_hint_and_returns_article():
    record = AnsvarRecord(
        title="Urheberrechtsgesetz",
        identifier="DE:UrhG",
        jurisdiction="DE",
        url="https://example.test/law",
        lookup_arguments={"canonical_ref": "DE:UrhG:2"},
    )
    client = FakeAnsvarClient(records=[record], article={
        "text": "§ 2\n\nProtected works include...",
        "url": "https://example.test/section-2",
    })
    result = AnsvarSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.evidence.article_text == "Protected works include..."
    assert client.article_calls == [
        ("DE:UrhG", "2", "DE", {"canonical_ref": "DE:UrhG:2"})
    ]


def test_article_fetch_error_is_not_reported_as_existence_success():
    client = FakeAnsvarClient(
        records=[AnsvarRecord(
            title="Urheberrechtsgesetz", identifier="DE:UrhG", jurisdiction="DE"
        )],
        article_error=AnsvarMcpError("source unavailable"),
    )
    result = AnsvarSource(client).lookup(_request())
    assert result.status == LookupStatus.SOURCE_ERROR
    assert result.evidence is None


def test_explicit_jurisdiction_never_accepts_other_country():
    records = [AnsvarRecord(title="Copyright Act", jurisdiction="GB")]
    assert _pick_match(records, "DE") is None


def test_completed_ansvar_miss_becomes_source_not_found_finding():
    result = AnsvarSource(FakeAnsvarClient()).lookup(_request(article_no=None))
    findings = assess_statute(
        "Urheberrechtsgesetz", None, result, [result.trace], []
    )
    assert findings[0].code.value == "source_not_found"
    assert "Ansvar" in findings[0].summary


def test_refresh_keeps_unrotated_refresh_token(tmp_path: Path, monkeypatch):
    oauth = AnsvarOAuth(token_cache=tmp_path / "tokens.json")
    oauth._client_id = "client"
    oauth._save_tokens({
        "access_token": "old",
        "refresh_token": "refresh",
        "expires_at": 0,
    })
    monkeypatch.setattr(
        oauth,
        "_refresh",
        lambda token: {"access_token": "new", "refresh_token": None, "expires_at": 99999999999},
    )
    assert oauth.get_bearer() == "new"
    assert oauth._load_tokens()["refresh_token"] == "refresh"
