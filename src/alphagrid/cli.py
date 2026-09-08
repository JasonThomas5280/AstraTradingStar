"""Operator commands. All broker mutation commands target paper exclusively."""
import argparse
import csv
import json
from pathlib import Path
import re
import sys
import time

from .execution.broker import PaperBroker, BrokerError
from .execution.paper_client import credential_status
from .ledger import StateError, utcnow
from .process_lock import process_lock
from .qualification import validate_evidence, source_fingerprint
from .research.backtest import load_csv, run_backtest
from .risk.circuit_breaker import CircuitBreaker
from .service import Engine, config, health, supervise, worker, watchdog


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--check-account", action="store_true", help="Compatibility alias for check-account")
    commands = parser.add_subparsers(dest="command")
    for name in ("status", "readiness", "check-account", "initialize", "serve", "halt", "drill"):
        commands.add_parser(name)
    for name in ("observe", "worker", "watchdog"):
        item = commands.add_parser(name)
        item.add_argument("--once", action="store_true")
    item = commands.add_parser("research")
    item.add_argument("symbol")
    item.add_argument("csv", type=Path)
    item = commands.add_parser("download")
    item.add_argument("symbol")
    item.add_argument("--start", required=True)
    item.add_argument("--end", required=True)
    item.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    command = "check-account" if args.check_account else args.command or "status"
    try:
        if command in ("status", "readiness"):
            cfg = config(root)
            blockers = validate_evidence(root, utcnow())
            creds = credential_status()
            missing = [name + " missing" for name, value in creds.items() if value == "missing"]
            result = {"mode": "paper", "entry_requested": cfg["trading_enabled"],
                      "qualification_passed": not blockers, "credentials": creds,
                      "blockers": missing + blockers, "target_cash_fraction": "0.10",
                      "source_fingerprint": source_fingerprint(root)}
            print(json.dumps(result, indent=2))
            return int(command == "readiness" and bool(result["blockers"]))
        if command == "check-account":
            broker = PaperBroker()
            account = broker.account()
            health(account)
            print(json.dumps({"mode": "paper", "credentials": credential_status(),
                              "account": {k: account[k] for k in ("equity", "cash", "buying_power")},
                              "positions": len(broker.positions()), "open_orders": len(broker.open_orders()),
                              "reconciled": False}, indent=2))
            return 0
        if command in ("research", "download"):
            if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", args.symbol):
                raise StateError("invalid_symbol")
            if command == "research":
                result = run_backtest(args.symbol, load_csv(args.csv))
                target = root / "reports" / ("research_" + args.symbol + ".json")
                target.parent.mkdir(exist_ok=True)
                target.write_text(json.dumps(result, indent=2, allow_nan=False))
                print(json.dumps({"report": str(target), "deployment_authorized": False}))
            else:
                bars = PaperBroker().bars(args.symbol, args.start, args.end)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("w", newline="") as handle:
                    out = csv.writer(handle)
                    out.writerow(["date", "open", "high", "low", "close", "volume"])
                    for b in bars:
                        out.writerow([b["t"][:10], b["o"], b["h"], b["l"], b["c"], b["v"]])
                print(json.dumps({"bars": len(bars), "source": "Alpaca SIP split-adjusted daily", "path": str(args.output)}))
            return 0
        if command == "serve":
            return supervise(root)
        if command in ("observe", "worker"):
            return worker(root, observe=command == "observe", once=args.once)
        engine = Engine(root)
        if command == "initialize":
            engine.initialize()
            print('{"initialized":true,"mode":"paper","orders_submitted":0}')
            return 0
        if command == "watchdog":
            if engine.ledger.get("initialized") is not True:
                raise StateError("initialize_required")
            return watchdog(root, once=args.once)
        if command in ("halt", "drill"):
            # Mutations require credentials and the expected dedicated paper account.
            account = engine.broker.account()
            health(account)
            before = engine.broker.positions()
            engine.halt("operator_" + command)
            started = time.monotonic()
            with process_lock(root / "state/execution.lock", timeout=20):
                flat = CircuitBreaker(root / "state/breaker.json").trip(engine.broker)
            result = {"mode": "paper", "flat_verified": flat,
                      "elapsed_seconds": time.monotonic() - started,
                      "initial_position_count": len(before), "halted": True,
                      "valid_positioned_drill": bool(before) and flat and time.monotonic()-started <= 60}
            (root / "reports").mkdir(exist_ok=True)
            (root / "reports" / "drill_result.json").write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
            return int(not flat)
    except (BrokerError, StateError) as error:
        print(json.dumps({"status": "blocked", "code": str(error), "mode": "paper"}))
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print('{"status":"blocked","code":"invalid_or_unavailable_input","mode":"paper"}')
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
