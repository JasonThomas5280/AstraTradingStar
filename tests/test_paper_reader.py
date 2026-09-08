import io
from urllib.error import HTTPError, URLError
import pytest
from alphagrid.execution import paper_client as module
from alphagrid.execution.paper_client import PaperReader, BrokerUnavailable, NoRedirect, credential_status


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-only-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-only-secret")


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert set(credential_status().values()) == {"missing"}
    with pytest.raises(BrokerUnavailable):
        PaperReader().read("/v2/account")


@pytest.mark.parametrize("path", ["https://api.alpaca.markets/v2/account", "/v2/orders", "//evil.test", "/v2/account/../orders"])
def test_path_allowlist(path):
    with pytest.raises(ValueError):
        PaperReader().read(path)


def test_redirects_rejected():
    with pytest.raises(BrokerUnavailable):
        NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test")


def test_reader_is_get_only_and_masks(credentials, monkeypatch):
    class Response(io.BytesIO):
        status = 200
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://paper-api.alpaca.markets/v2/account"
            assert request.method == "GET" and timeout == 10
            assert request.get_header("Apca-api-secret-key") == "test-only-secret"
            return Response(b'{"equity": "100000"}')
    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    assert PaperReader().read("/v2/account") == {"equity": "100000"}
    assert "test-only" not in str(credential_status())


@pytest.mark.parametrize("error", [HTTPError("x", 401, "sensitive", {}, None),
    HTTPError("x", 500, "sensitive", {}, None), URLError("sensitive"), TimeoutError("sensitive")])
def test_reader_errors_sanitized(credentials, monkeypatch, error):
    class Opener:
        def open(self, *args, **kwargs):
            raise error
    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    with pytest.raises(BrokerUnavailable) as caught:
        PaperReader().read("/v2/account")
    assert "sensitive" not in str(caught.value) and "test-only-secret" not in str(caught.value)


@pytest.mark.parametrize("body,status", [(b"not-json", 200), (b"x" * 2_000_001, 200), (b"{}", 503)],
                         ids=["invalid-json", "oversized", "unexpected-status"])
def test_bad_responses(credentials, monkeypatch, body, status):
    class Response(io.BytesIO):
        pass
    class Opener:
        def open(self, *args, **kwargs):
            response = Response(body)
            response.status = status
            return response
    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    with pytest.raises(BrokerUnavailable):
        PaperReader().read("/v2/account")
