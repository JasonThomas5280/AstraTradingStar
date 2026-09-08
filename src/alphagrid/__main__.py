"""python -m alphagrid [--check-account]: diagnostic only, never trades."""
import argparse
import json
from .execution.paper_client import PaperReader, BrokerUnavailable, credential_status
from .risk.numbers import number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-account", action="store_true", help="Read paper account, positions, and open orders")
    args = parser.parse_args()
    result = {"mode": "paper", "trading_enabled": False, "credentials": credential_status()}
    if not args.check_account:
        print(json.dumps(result, indent=2))
        return 0
    try:
        reader = PaperReader()
        account = reader.read("/v2/account")
        positions = reader.read("/v2/positions")
        orders = reader.read("/v2/orders?status=open&limit=500")
        if not isinstance(account, dict) or not isinstance(positions, list) or not isinstance(orders, list):
            raise ValueError("invalid broker schema")
        # Never dump raw responses, account identifiers, or exception strings.
        result["account"] = {k: str(number(account[k])) for k in ("equity", "cash", "buying_power")}
        result["position_count"] = len(positions)
        result["open_order_count"] = len(orders)
        result["reconciled"] = False
        result["note"] = "Diagnostic snapshot only; no local baseline or execution authorization."
    except (BrokerUnavailable, ValueError, KeyError, TypeError):
        result["error"] = "Paper account verification failed; check credentials, network, and account state."
        print(json.dumps(result, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
