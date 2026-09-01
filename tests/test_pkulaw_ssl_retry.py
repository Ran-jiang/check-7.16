from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import ssl
import urllib.error

import pytest

from ccitecheck.retrieval.sources.pkulaw.client import (
    PkulawMcpClient,
    PkulawMcpError,
    PkulawNotConfiguredError,
)


def test_ssl_error_is_retried(monkeypatch):
    calls = 0

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self): return b'{"result": {}}'

    def urlopen(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ssl.SSLError("unexpected eof")
        return Response()

    monkeypatch.setattr("ccitecheck.retrieval.sources.pkulaw.client.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("ccitecheck.retrieval.sources.pkulaw.client.time.sleep", lambda _: None)
    client = PkulawMcpClient(access_token="test")
    assert client._call_tool("/test", "test", {}) == {"result": {}}
    assert calls == 2


def test_missing_token_fails_before_network(monkeypatch):
    monkeypatch.setenv("PKULAW_ACCESS_TOKEN", "")
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.urllib.request.urlopen",
        lambda *args, **kwargs: pytest.fail("network should not be called"),
    )

    with pytest.raises(PkulawNotConfiguredError, match="PKULAW_ACCESS_TOKEN"):
        PkulawMcpClient()._call_tool("/test", "test", {})


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "鉴权失败"),
        (403, "鉴权失败"),
        (429, "请求额度受限"),
    ],
)
def test_account_failures_open_circuit_without_repeated_calls(
    monkeypatch, status, message
):
    calls = 0

    def urlopen(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://example.test", status, "error", {}, BytesIO(b'{"error":"detail"}')
        )

    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.urllib.request.urlopen", urlopen
    )
    client = PkulawMcpClient(access_token="test")

    with pytest.raises(PkulawMcpError, match=message):
        client._call_tool("/test", "test", {})
    with pytest.raises(PkulawMcpError, match="暂停重复请求"):
        client._call_tool("/test", "test", {})

    assert calls == 1


def test_concurrent_auth_failures_only_probe_upstream_once(monkeypatch):
    calls = 0

    def urlopen(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://example.test", 401, "unauthorized", {}, BytesIO(b"unauthorized")
        )

    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.urllib.request.urlopen", urlopen
    )
    client = PkulawMcpClient(access_token="test")

    def invoke():
        with pytest.raises(PkulawMcpError, match="鉴权失败"):
            client._call_tool("/test", "test", {})

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: invoke(), range(24)))

    assert calls == 1


def test_exhausted_service_failures_open_short_circuit(monkeypatch):
    calls = 0

    def urlopen(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://example.test", 503, "unavailable", {}, BytesIO(b"unavailable")
        )

    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.urllib.request.urlopen", urlopen
    )
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.time.sleep", lambda _: None
    )
    client = PkulawMcpClient(access_token="test")

    with pytest.raises(PkulawMcpError, match="服务暂时不可用"):
        client._call_tool("/test", "test", {})
    with pytest.raises(PkulawMcpError, match="暂停重复请求"):
        client._call_tool("/test", "test", {})

    assert calls == 3
