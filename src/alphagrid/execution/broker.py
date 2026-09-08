"""Bounded Alpaca paper transport. No retries and no endpoint overrides.

Open orders are nested roots (``legs`` retained). Callers must durably journal
submission intent and reconcile ambiguous mutations before submitting again.
Historical bars use SIP, split adjustment; latest quotes use IEX explicitly.
"""
import json
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PAPER_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"


class BrokerError(RuntimeError):
    def __init__(self, code, status=None, ambiguous=False):
        self.code, self.status, self.ambiguous = code, status, ambiguous
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BrokerError("redirect_rejected", status=code,
                          ambiguous=req is not None and req.method != "GET")


def _identifier(value, symbol=False):
    pattern = r"[A-Z][A-Z0-9.\-]{0,14}" if symbol else r"[A-Za-z0-9_\-]{1,48}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise BrokerError("invalid_identifier")
    return value


def _number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BrokerError("invalid_number") from None
    if not result.is_finite() or result <= 0:
        raise BrokerError("invalid_number")
    return result


class PaperBroker:
    def __init__(self):
        self._uncertain_closes = set()

    def _request(self, method, path, *, query=None, payload=None, data=False, timeout=10):
        # Private dispatch is still allowlisted, so malformed paths cannot send secrets elsewhere.
        allowed = ((data and method == "GET" and re.fullmatch(
            r"/v2/stocks/(bars|[A-Z][A-Z0-9.\-]{0,14}/quotes/latest)", path)) or
            (not data and method == "GET" and re.fullmatch(
                r"/v2/(account|clock|calendar|positions|orders|orders:by_client_order_id|orders/[A-Za-z0-9_\-]{1,48}|assets/[A-Z][A-Z0-9.\-]{0,14})", path)) or
            (not data and method == "POST" and path == "/v2/orders") or
            (not data and method == "PATCH" and re.fullmatch(r"/v2/orders/[A-Za-z0-9_\-]{1,48}", path)) or
            (not data and method == "DELETE" and re.fullmatch(
                r"/v2/(orders|orders/[A-Za-z0-9_\-]{1,48}|positions/[A-Z][A-Z0-9.\-]{0,14})", path)))
        if not allowed:
            raise BrokerError("endpoint_rejected")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 10:
            raise BrokerError("invalid_timeout")
        key = os.environ.get("APCA_API_KEY_ID", "").strip()
        secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
        if not key or not secret:
            raise BrokerError("missing_credentials")
        mutation = method != "GET"
        try:
            url = (DATA_BASE if data else PAPER_BASE) + path
            if query:
                url += "?" + urlencode(query)
            body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
            request = Request(url, data=body, method=method, headers={
                "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json", "Content-Type": "application/json"})
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    raise BrokerError("http_error", response.status,
                                      mutation and response.status >= 500)
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise BrokerError("response_too_large", ambiguous=mutation)
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            raise BrokerError("http_error", exc.code, mutation and exc.code >= 500) from None
        except (URLError, TimeoutError, OSError):
            raise BrokerError("transport_error", ambiguous=mutation) from None
        except (ValueError, UnicodeError):
            raise BrokerError("invalid_response", ambiguous=mutation) from None

    def account(self):
        return self._request("GET", "/v2/account")

    def clock(self):
        return self._request("GET", "/v2/clock")

    def calendar(self, start, end):
        start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
        if start > end:
            raise BrokerError("invalid_date_range")
        return self._request("GET", "/v2/calendar", query={"start": str(start), "end": str(end)})

    def positions(self, timeout=10):
        return self._request("GET", "/v2/positions", timeout=timeout)

    def open_orders(self, timeout=10):
        result = self._request("GET", "/v2/orders", query={
            "status": "open", "limit": 500, "nested": "true"}, timeout=timeout)
        if not isinstance(result, list) or len(result) >= 500:
            raise BrokerError("incomplete_order_snapshot")
        return result

    def order(self, client_id, timeout=10):
        try:
            return self._request("GET", "/v2/orders:by_client_order_id",
                                 query={"client_order_id": _identifier(client_id), "nested": "true"}, timeout=timeout)
        except BrokerError as exc:
            if exc.status == 404:
                return None
            raise

    def order_by_id(self, order_id):
        return self._request("GET", "/v2/orders/" + _identifier(order_id), query={"nested": "true"})

    def asset(self, symbol):
        return self._request("GET", "/v2/assets/" + _identifier(symbol, True))

    def submit_bracket(self, payload):
        required = {"symbol", "qty", "side", "type", "time_in_force", "order_class",
                    "client_order_id", "limit_price", "take_profit", "stop_loss"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise BrokerError("invalid_bracket")
        if (payload["side"] != "buy" or payload["type"] != "limit" or
                payload["time_in_force"] not in ("day", "gtc") or payload["order_class"] != "bracket"):
            raise BrokerError("invalid_bracket")
        _identifier(payload["symbol"], True)
        _identifier(payload["client_order_id"])
        qty = _number(payload["qty"])
        if qty != qty.to_integral_value():
            raise BrokerError("invalid_quantity")
        if (not isinstance(payload["take_profit"], dict) or set(payload["take_profit"]) != {"limit_price"}
                or not isinstance(payload["stop_loss"], dict) or set(payload["stop_loss"]) != {"stop_price"}):
            raise BrokerError("invalid_bracket")
        limit = _number(payload["limit_price"])
        take = _number(payload["take_profit"]["limit_price"])
        stop = _number(payload["stop_loss"]["stop_price"])
        if not stop < limit < take or limit - stop < Decimal("0.01"):
            raise BrokerError("invalid_bracket_prices")
        clean = dict(payload, qty=str(qty), limit_price=str(limit),
                     take_profit={"limit_price": str(take)}, stop_loss={"stop_price": str(stop)})
        return self._request("POST", "/v2/orders", payload=clean)

    def cancel(self, order_id, timeout=10):
        return self._request("DELETE", "/v2/orders/" + _identifier(order_id), timeout=timeout)

    def cancel_all(self, timeout=10):
        return self._request("DELETE", "/v2/orders", timeout=timeout)

    def replace_stop(self, order_id, new_price):
        old = self.order_by_id(order_id)
        price = _number(new_price)
        if (not isinstance(old, dict) or old.get("side") != "sell" or old.get("type") != "stop"
                or price <= _number(old.get("stop_price"))):
            raise BrokerError("stop_must_tighten")
        return self._request("PATCH", "/v2/orders/" + _identifier(order_id),
                             payload={"stop_price": str(price)})

    def close_position(self, symbol, timeout=10):
        """Single close; caller journals intent and reconciles/cancels active orders first."""
        return self._request("DELETE", "/v2/positions/" + _identifier(symbol, True), timeout=timeout)

    def close_all(self, timeout=10):
        """Close positions only after prior orders settle; never duplicate an active exit.

        A shared monotonic budget is checked between requests. Socket timeouts
        are best effort, not a hard wall-clock guarantee. Ambiguous exits latch
        in this process; the controller persists close intent across restarts.
        """
        import time
        deadline = time.monotonic() + min(timeout, 10)
        def remaining():
            result = deadline - time.monotonic()
            if result <= 0:
                raise BrokerError("close_deadline", ambiguous=True)
            return result
        orders = self.open_orders(timeout=remaining())
        positions = self.positions(timeout=remaining())
        if not isinstance(positions, list):
            raise BrokerError("invalid_positions")
        results = []
        for position in positions:
            symbol = _identifier(position["symbol"], True)
            active = [order for order in orders if order.get("symbol") == symbol]
            if active:
                # Protective brackets and partially canceled entries need caller reconciliation.
                if all(order.get("type") == "market" and order.get("order_class") in (None, "", "simple")
                       and order.get("side") == ("sell" if position.get("side") == "long" else "buy")
                       for order in active):
                    results.extend(active)
                    continue
                raise BrokerError("orders_not_settled")
            if symbol in self._uncertain_closes:
                raise BrokerError("close_requires_reconciliation", ambiguous=True)
            self._uncertain_closes.add(symbol)
            try:
                results.append(self._request("DELETE", "/v2/positions/" + symbol, timeout=remaining()))
            except BrokerError as exc:
                if not exc.ambiguous:
                    self._uncertain_closes.discard(symbol)
                raise
        self._uncertain_closes.intersection_update(p["symbol"] for p in positions)
        return results

    def bars(self, symbol, start, end):
        symbol = _identifier(symbol, True)
        query = {"symbols": symbol, "start": str(start), "end": str(end), "timeframe": "1Day",
                 "feed": "sip", "adjustment": "split", "sort": "asc", "limit": 10000}
        result, seen = [], set()
        for _ in range(100):
            page = self._request("GET", "/v2/stocks/bars", data=True, query=query)
            if not isinstance(page, dict) or not isinstance(page.get("bars"), dict):
                raise BrokerError("invalid_bars")
            bars = page["bars"].get(symbol, [])
            if not isinstance(bars, list):
                raise BrokerError("invalid_bars")
            result.extend(bars)
            token = page.get("next_page_token")
            if token is None:
                return result
            if not isinstance(token, str) or not token or token in seen:
                raise BrokerError("invalid_pagination")
            seen.add(token)
            query["page_token"] = token
        raise BrokerError("pagination_limit")

    def quote(self, symbol):
        result = self._request("GET", "/v2/stocks/" + _identifier(symbol, True) + "/quotes/latest",
                               data=True, query={"feed": "iex"})
        if not isinstance(result, dict) or not isinstance(result.get("quote"), dict):
            raise BrokerError("invalid_quote")
        return result["quote"]
