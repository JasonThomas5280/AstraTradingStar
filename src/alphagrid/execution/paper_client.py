import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PAPER_BASE = "https://paper-api.alpaca.markets"


class BrokerUnavailable(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BrokerUnavailable("redirect rejected")


def credential_status():
    return {name: "present (masked)" if os.environ.get(name, "").strip() else "missing"
            for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")}


class PaperReader:
    """Only reads three known paths; secrets never appear in object repr/errors.

    Does not use environment proxies or accept endpoint overrides. It has no
    mutation methods. Production order lifecycle / SDK integration is deferred.
    """
    def read(self, path):
        if path not in ("/v2/account", "/v2/positions", "/v2/orders?status=open&limit=500"):
            raise ValueError("path is not an approved read")
        key = os.environ.get("APCA_API_KEY_ID", "").strip()
        secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
        if not key or not secret:
            raise BrokerUnavailable("paper credentials missing from environment")
        try:
            request = Request(PAPER_BASE + path, headers={
                "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json"}, method="GET")
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=10) as response:
                if response.status != 200:
                    raise BrokerUnavailable("unexpected broker status")
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise BrokerUnavailable("broker response too large")
                return json.loads(body)
        except HTTPError as error:
            code = error.code
            raise BrokerUnavailable(f"broker HTTP {code}; trading remains disabled") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise BrokerUnavailable("broker request failed; trading remains disabled") from None
