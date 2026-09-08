import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest
from alphagrid.execution import broker as module
from alphagrid.execution.broker import PaperBroker, BrokerError, NoRedirect


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "fake-test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fake-test-secret")
    calls, responses = [], []
    class Response(io.BytesIO):
        status = 200
    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            value = responses.pop(0)
            if isinstance(value, BaseException):
                raise value
            return Response(json.dumps(value).encode())
    def build(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], NoRedirect)
        return Opener()
    monkeypatch.setattr(module, "build_opener", build)
    return calls, responses


def payload():
    return dict(symbol="SPY", qty=2, side="buy", type="limit", time_in_force="day",
                order_class="bracket", client_order_id="ag-123", limit_price="100.00",
                take_profit={"limit_price": "104.00"}, stop_loss={"stop_price": "98.00"})


def test_submit_encoding(transport):
    calls, responses = transport
    responses.append({"id": "order-1"})
    assert PaperBroker().submit_bracket(payload()) == {"id": "order-1"}
    req, timeout = calls[0]
    assert req.full_url == module.PAPER_BASE + "/v2/orders"
    assert req.method == "POST" and timeout == 10
    assert json.loads(req.data)["qty"] == "2"


@pytest.mark.parametrize("change", [{"side": "sell"}, {"type": "market"}, {"qty": 1.5},
    {"qty": "NaN"}, {"symbol": "BTC/USD"}, {"symbol": "../../evil"},
    {"client_order_id": "x&evil=1"}, {"notional": 10}, {"extended_hours": True},
    {"stop_loss": {"stop_price": 101}}, {"take_profit": {"limit_price": 90}},
    {"stop_loss": {"stop_price": 98, "limit_price": 97}}, {"qty": True}])
def test_invalid_bracket_never_sent(transport, change):
    with pytest.raises(BrokerError):
        PaperBroker().submit_bracket(dict(payload(), **change))
    assert transport[0] == []


@pytest.mark.parametrize("error,ambiguous,status", [
    (HTTPError("sensitive", 429, "sensitive", {}, None), False, 429),
    (HTTPError("sensitive", 500, "sensitive", {}, None), True, 500),
    (TimeoutError("sensitive"), True, None), (URLError("sensitive"), True, None)])
def test_mutation_errors_no_retry_sanitized(transport, error, ambiguous, status):
    calls, responses = transport
    responses.append(error)
    with pytest.raises(BrokerError) as caught:
        PaperBroker().submit_bracket(payload())
    assert len(calls) == 1
    assert caught.value.ambiguous == ambiguous and caught.value.status == status
    assert "sensitive" not in str(caught.value)
    assert "fake-test-secret" not in str(caught.value)


def test_missing_auth(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    with pytest.raises(BrokerError, match="missing_credentials"):
        PaperBroker().account()


@pytest.mark.parametrize("path", ["https://api.alpaca.markets/v2/orders", "//evil.test", "/v2/account/../orders"])
def test_endpoint_rejection(path):
    with pytest.raises(BrokerError, match="endpoint_rejected"):
        PaperBroker()._request("GET", path)


def test_redirect_rejection():
    with pytest.raises(BrokerError, match="redirect_rejected"):
        NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")


def test_bars_pagination_and_feeds(transport):
    calls, responses = transport
    responses.extend([{"bars": {"SPY": [{"c": 1}]}, "next_page_token": "foo+/="},
                      {"bars": {"SPY": [{"c": 2}]}, "next_page_token": None}, {"quote": {"ap": 2}}])
    broker = PaperBroker()
    assert broker.bars("SPY", "2025-01-01", "2025-12-31") == [{"c": 1}, {"c": 2}]
    query = parse_qs(urlsplit(calls[1][0].full_url).query)
    assert query["page_token"] == ["foo+/="] and query["adjustment"] == ["split"]
    assert query["feed"] == ["sip"] and query["timeframe"] == ["1Day"]
    assert broker.quote("SPY") == {"ap": 2}
    assert calls[2][0].full_url.endswith("/SPY/quotes/latest?feed=iex")


def test_repeated_pagination_fails(transport):
    transport[1].extend([{"bars": {}, "next_page_token": "x"}] * 2)
    with pytest.raises(BrokerError, match="invalid_pagination"):
        PaperBroker().bars("SPY", "2025-01-01", "2025-12-31")


def test_order_not_found_and_snapshot_truncation(transport):
    transport[1].extend([HTTPError("", 404, "", {}, None), [{}] * 500])
    assert PaperBroker().order("ag-123") is None
    with pytest.raises(BrokerError, match="incomplete_order_snapshot"):
        PaperBroker().open_orders()


def test_close_pending_exit_is_not_duplicated(transport):
    order = {"symbol": "SPY", "side": "sell", "type": "market", "order_class": "simple"}
    transport[1].extend([[order], [{"symbol": "SPY", "side": "long"}]])
    assert PaperBroker().close_all() == [order]
    assert all(req.method == "GET" for req, _ in transport[0])


def test_close_protection_requires_settlement(transport):
    transport[1].extend([[{"symbol": "SPY", "type": "stop"}], [{"symbol": "SPY", "side": "long"}]])
    with pytest.raises(BrokerError, match="orders_not_settled"):
        PaperBroker().close_all()


def test_close_ambiguous_latches_no_duplicate(transport):
    positions = [{"symbol": "SPY", "side": "long"}]
    transport[1].extend([[], positions, TimeoutError(), [], positions])
    broker = PaperBroker()
    with pytest.raises(BrokerError, match="transport_error"):
        broker.close_all()
    with pytest.raises(BrokerError, match="close_requires_reconciliation"):
        broker.close_all()
    assert sum(req.method == "DELETE" for req, _ in transport[0]) == 1


def test_read_timeout_is_not_ambiguous(transport):
    transport[1].append(TimeoutError())
    with pytest.raises(BrokerError) as caught:
        PaperBroker().positions()
    assert not caught.value.ambiguous


def test_remaining_endpoints(transport):
    transport[1].extend([{}] * 7)
    broker = PaperBroker()
    broker.clock()
    broker.calendar("2026-01-01", "2026-02-01")
    broker.order_by_id("abc-123")
    broker.asset("BRK.B")
    broker.cancel("abc-123")
    broker.cancel_all()
    broker.account()
    assert [req.method for req, _ in transport[0]] == ["GET"] * 4 + ["DELETE"] * 2 + ["GET"]


def test_stop_replacement_tightens_and_close_path(transport):
    transport[1].extend([{"side": "sell", "type": "stop", "stop_price": "98"}, {}, {}])
    broker = PaperBroker()
    broker.replace_stop("abc-123", "99")
    request = transport[0][1][0]
    assert request.method == "PATCH" and json.loads(request.data) == {"stop_price": "99"}
    broker.close_position("SPY")
    assert transport[0][2][0].full_url == module.PAPER_BASE + "/v2/positions/SPY"


@pytest.mark.parametrize("side,kind,price", [("sell", "stop", "97"), ("buy", "stop", "99"),
                                           ("sell", "limit", "99"), ("sell", "stop", "98")])
def test_stop_cannot_loosen_or_change_other_orders(transport, side, kind, price):
    transport[1].append({"side": side, "type": kind, "stop_price": "98"})
    with pytest.raises(BrokerError, match="stop_must_tighten"):
        PaperBroker().replace_stop("abc-123", price)
    assert len(transport[0]) == 1


def test_successful_close_visibility_gap_does_not_duplicate(transport):
    positions = [{"symbol": "SPY", "side": "long"}]
    transport[1].extend([[], positions, {"id": "closing-order"}, [], positions])
    broker = PaperBroker()
    assert broker.close_all() == [{"id": "closing-order"}]
    with pytest.raises(BrokerError, match="close_requires_reconciliation"):
        broker.close_all()
    assert sum(req.method == "DELETE" for req, _ in transport[0]) == 1


def test_cancel_preserves_supplied_deadline(transport):
    transport[1].append(None)
    PaperBroker().cancel("late-entry", timeout=0.5)
    assert transport[0][0][1] == 0.5
