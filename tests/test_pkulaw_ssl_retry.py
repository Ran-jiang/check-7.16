import ssl

from ccitecheck.tracing.sources.pkulaw.client import PkulawMcpClient


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

    monkeypatch.setattr("ccitecheck.tracing.sources.pkulaw.client.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("ccitecheck.tracing.sources.pkulaw.client.time.sleep", lambda _: None)
    client = PkulawMcpClient(access_token="test")
    assert client._call_tool("/test", "test", {}) == {"result": {}}
    assert calls == 2
