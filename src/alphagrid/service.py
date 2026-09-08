"""Supervised, paper-only long-equity service. No LLM or web-text execution.

The worker alone creates entries. The independent watchdog only cancels/closes.
The account must be dedicated to this service and initialized while flat.
"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D, ROUND_DOWN, ROUND_UP
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from .execution.broker import PaperBroker, BrokerError
from .ledger import Ledger, StateError, timestamp, utcnow
from .process_lock import process_lock
from .qualification import validate_evidence
from .risk.circuit_breaker import CircuitBreaker, breach_reason
from .risk.numbers import number
from .risk.position_sizer import size_long
from .risk.risk_gate import Exposure, Proposal, Snapshot, evaluate
from .strategies.trend_pullback import Bar, signal, trailing_stop

EASTERN = ZoneInfo("America/New_York")
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
ACTIVE_STOPS = {"new", "accepted"}


def protective_stops(order):
    """Alpaca keeps an armed bracket stop HELD while its take-profit is NEW.

    Do not accept two held legs or a partial/unfilled parent as armed protection.
    Alpaca staff description: https://forum.alpaca.markets/t/3697/2
    """
    legs = order.get('legs') or []
    armed_bracket = (order.get('status') == 'filled'
                     and number(order['filled_qty']) == number(order['qty'], positive=True)
                     and any(l.get('type') == 'limit' and l.get('status') in ACTIVE_STOPS
                             and l.get('side') == 'sell' for l in legs))
    return [l for l in legs if l.get('type') == 'stop' and
            (l.get('status') in ACTIVE_STOPS or (armed_bracket and l.get('status') == 'held'))]


def config(root):
    # JSON is the deliberately restricted YAML subset accepted for authorization.
    cfg = json.loads((Path(root) / "config/authorization.yaml").read_text())
    if (cfg.get("mode") != "paper" or cfg.get("live_trading_authorized") is not False
            or cfg.get("paper_endpoint") != "https://paper-api.alpaca.markets"
            or cfg.get("enabled_strategies") != ["6.1"]):
        raise StateError("invalid_authorization")
    symbols = cfg.get("enabled_symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 30 or len(set(symbols)) != len(symbols):
        raise StateError("invalid_universe")
    if any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", s) for s in symbols):
        raise StateError("invalid_universe")
    if not 0 < number(cfg["risk_fraction"]) <= D("0.0025"):
        raise StateError("risk_configuration_exceeds_initial_sleeve")
    if not 0 < number(cfg["sleeve_fraction"]) <= D("0.90"):
        raise StateError("sleeve_configuration_exceeds_initial_sleeve")
    return cfg


def health(account):
    if (account.get("status") != "ACTIVE" or account.get("trading_blocked") is not False
            or account.get("account_blocked") is not False or account.get("currency") != "USD"):
        raise StateError("account_not_active")
    for key in ("equity", "last_equity", "cash", "buying_power"):
        number(account[key], positive=key in ("equity", "last_equity"))


def clock_time(clock, now=None):
    stamp = timestamp(clock["timestamp"])
    if abs(((now or utcnow()) - stamp).total_seconds()) > 30:
        raise StateError("stale_broker_clock")
    if type(clock.get("is_open")) is not bool:
        raise StateError("invalid_broker_clock")
    return stamp.astimezone(EASTERN)


def quote_prices(quote, now=None):
    age = ((now or utcnow()) - timestamp(quote["t"])).total_seconds()
    bid, ask = number(quote["bp"], positive=True), number(quote["ap"], positive=True)
    if not 0 <= age <= 30 or bid > ask or (ask - bid) / ask > D("0.002"):
        raise StateError("stale_or_wide_quote")
    return bid, ask


def correlation(left, right):
    a, b = {bar.date: bar.close for bar in left}, {bar.date: bar.close for bar in right}
    dates = sorted(a.keys() & b.keys())[-61:]
    if len(dates) < 61:
        raise StateError("insufficient_correlation_history")
    xs = [a[y] / a[x] - 1 for x, y in zip(dates, dates[1:])]
    ys = [b[y] / b[x] - 1 for x, y in zip(dates, dates[1:])]
    ax, ay = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x-ax)*(y-ay) for x, y in zip(xs, ys))
    denominator = (sum((x-ax)**2 for x in xs) * sum((y-ay)**2 for y in ys)) ** .5
    if denominator == 0:
        raise StateError("undefined_correlation")
    return D(str(max(-1, min(1, numerator / denominator))))


class Engine:
    def __init__(self, root, broker=None):
        self.root = Path(root).resolve()
        self.broker = broker or PaperBroker()
        self.ledger = Ledger(self.root / "state/runtime.db")
        self.cache = {}

    def halt(self, code):
        self.ledger.halt(code)
        (self.root / "alerts").mkdir(exist_ok=True)
        name = utcnow().strftime("URGENT_%Y%m%d_%H%M%S.md")
        message = f"# Paper service halted\n\nReason: {code}\nNew entries disabled. Verify broker positions and orders.\nA written postmortem and all qualification evidence are required before restart.\n"
        (self.root / "alerts" / name).write_text(message)
        (self.root / "HALT.md").write_text(message)

    def initialize(self):
        config(self.root)
        account = self.broker.account()
        health(account)
        session = clock_time(self.broker.clock()).date().isoformat()
        self.ledger.initialize(account, self.broker.positions(), self.broker.open_orders(), session)

    def refresh(self, *, allow_partial_settlement=False):
        # Resolve every prior intent before any account comparison or fresh entry.
        for item in self.ledger.exits():
            if item["snapshot"] is None:
                raise StateError("ambiguous_close")
            if item["snapshot"]["status"] not in TERMINAL:
                self.ledger.record_close(item["parent_id"], self.broker.order_by_id(item["snapshot"]["id"]))
        for item in self.ledger.active_intents():
            order = self.broker.order(item["client_id"])
            if order is None:
                raise StateError("ambiguous_submission")
            self.ledger.record(item["client_id"], order)
        account, positions, orders = self.broker.account(), self.broker.positions(), self.broker.open_orders()
        health(account)
        self.ledger.reconcile(account, positions, orders)
        current_clock = self.broker.clock()
        session = clock_time(current_clock)
        marked = self.ledger.mark_equity(account, session.date().isoformat())
        reason = breach_reason(*marked)
        if reason:
            raise StateError(reason)
        if len(positions) > 12:
            raise StateError("position_count")
        equity = marked[0]
        notionals = [number(p["qty"], positive=True) * number(p["current_price"], positive=True) for p in positions]
        if any(n > equity * D(".15") for n in notionals) or sum(notionals) > equity * D("1.5"):
            raise StateError("portfolio_exposure_breach")
        # Partial entry fills do not yet activate bracket protection at Alpaca.
        for item in self.ledger.intents():
            order = item["snapshot"]
            filled, qty = number(order["filled_qty"]), number(order["qty"], positive=True)
            sold = sum(number(leg["filled_qty"]) for leg in order["legs"])
            if 0 < filled < qty:
                # A short, bounded settlement window is allowed only when the
                # caller blocks new entries until this parent fully settles.
                if allow_partial_settlement and 0 <= (utcnow()-timestamp(item['created_at'])).total_seconds() < 15:
                    continue
                raise StateError("partial_entry_requires_flatten")
            if filled > sold:
                exits = [e for e in self.ledger.exits() if e["parent_id"] == item["client_id"] and e["snapshot"]]
                if exits:
                    exit_order = exits[0]["snapshot"]
                    sold += number(exit_order["filled_qty"])
                    if filled <= sold or exit_order["status"] not in TERMINAL:
                        continue
                stops = protective_stops(order)
                if len(stops) != 1 or number(stops[0]["qty"], positive=True) < filled - sold:
                    raise StateError("missing_broker_stop")
        return account, positions, orders, current_clock, marked

    def history(self, symbol, session_date):
        key = (symbol, session_date)
        if key not in self.cache:
            start = (session_date - timedelta(days=300)).isoformat()
            end = (session_date - timedelta(days=1)).isoformat() + "T23:59:59Z"
            raw = self.broker.bars(symbol, start, end)
            bars = [Bar(timestamp(b["t"]).astimezone(EASTERN).date(),
                        b["o"], b["h"], b["l"], b["c"], b["v"]) for b in raw]
            sessions = self.broker.calendar((session_date - timedelta(days=14)).isoformat(), session_date.isoformat())
            previous = max(date.fromisoformat(s["date"]) for s in sessions if date.fromisoformat(s["date"]) < session_date)
            if len(bars) < 61 or bars[-1].date != previous or any(b.date >= session_date for b in bars):
                raise StateError("stale_daily_history")
            self.cache[key] = bars
        return self.cache[key]

    def snapshot(self, account, positions, marked, histories):
        exposures = [Exposure(p["symbol"], number(p["qty"], positive=True),
                              number(p["current_price"], positive=True)) for p in positions]
        for item in self.ledger.intents():
            order = item["snapshot"]
            if order["status"] not in TERMINAL:
                qty = number(order["qty"], positive=True) - number(order["filled_qty"])
                if qty > 0:
                    exposures.append(Exposure(item["symbol"], qty, number(item["payload"]["limit_price"], positive=True)))
        names = sorted(histories)
        correlations = {(a, b): correlation(histories[a], histories[b])
                        for i, a in enumerate(names) for b in names[i+1:]}
        equity, start, peak, loss = marked
        return Snapshot(equity, start, peak, loss, tuple(exposures), correlations,
                        reconciled=True, healthy=True, halted=False, age_seconds=D(0),
                        day_trades=int(account["daytrade_count"]), macro_multiplier=D(".5"),
                        cooldown_clear=True, journal_verified=True)

    def submit(self, proposal, state, signal_date):
        """Only called by the sole worker, after fresh broker and evidence checks."""
        with process_lock(self.root / "state/execution.lock", timeout=20):
            return self._submit_locked(proposal, state, signal_date)

    def _submit_locked(self, proposal, state, signal_date):
        reconciled = self.ledger.get("last_reconciled")
        age = D(str((utcnow() - timestamp(reconciled)).total_seconds())) if reconciled else D("Infinity")
        decision = evaluate(proposal, replace(state, age_seconds=age))
        if not decision.allowed:
            self.ledger.event("rejected", {"symbol": proposal.symbol, "reason": decision.reason})
            return False
        if self.ledger.get("halted", True):
            raise StateError("halted")
        heartbeat = self.ledger.get("watchdog_heartbeat")
        if not heartbeat or not 0 <= (utcnow() - timestamp(heartbeat)).total_seconds() <= 15:
            raise StateError("watchdog_missing")
        client_id = "ag-" + hashlib.sha256(f"6.1-bracket:{signal_date}:{proposal.symbol}".encode()).hexdigest()[:32]
        payload = {"symbol": proposal.symbol, "qty": str(proposal.quantity), "side": "buy",
                   "type": "limit", "time_in_force": "gtc", "order_class": "bracket",
                   "client_order_id": client_id, "limit_price": str(proposal.entry),
                   "take_profit": {"limit_price": str(proposal.target)},
                   "stop_loss": {"stop_price": str(proposal.stop)}}
        if not self.ledger.reserve(payload, proposal.thesis):
            return False
        # A watchdog halt between reservation and POST must stop the submission.
        if self.ledger.get("halted", True):
            raise StateError("halted_before_post")
        try:
            order = self.broker.submit_bracket(payload)
            self.ledger.record(client_id, order)
        except Exception:
            # Any failed mutation leaves intent present. Never automatic resubmit.
            self.halt("submission_requires_reconciliation")
            raise StateError("submission_requires_reconciliation") from None
        return True

    def cycle(self, *, observe=False):
        cfg = config(self.root)
        if self.ledger.get("halted", True):
            return {"status": "halted"}
        account, positions, orders, market, marked = self.refresh()
        session = clock_time(market)
        # Cancel unfilled entries after five minutes or outside the session.
        for item in self.ledger.intents():
            order = item["snapshot"]
            if order["status"] not in TERMINAL and number(order["filled_qty"]) == 0:
                age = (utcnow() - timestamp(item["created_at"])).total_seconds()
                if not market["is_open"] or age >= 300:
                    if not observe:
                        self.broker.cancel(order["id"])
                    return {"status": "canceling_expired_entry"}
        self.write_report(account, positions, orders)
        if not market["is_open"]:
            return {"status": "market_closed"}
        blockers = validate_evidence(self.root, utcnow())
        histories = {p["symbol"]: self.history(p["symbol"], session.date()) for p in positions}
        # Broker-hosted exits persist even when qualification expires. Risk exits
        # remain permitted; never remove or widen a stop during requalification.
        for item in self.ledger.intents():
            order = item["snapshot"]
            if item["symbol"] not in histories:
                continue
            stops = [leg for leg in order["legs"] if leg["type"] == "stop" and leg["status"] in ACTIVE_STOPS]
            if not stops:
                continue
            bars = histories[item["symbol"]]
            entered = timestamp(order["filled_at"]).astimezone(EASTERN).date()
            held_sessions = sum(b.date >= entered for b in bars) + 1
            near_close = "next_close" in market and (timestamp(market["next_close"])-utcnow()).total_seconds() <= 300
            if not observe and (held_sessions > 10 or (held_sessions == 10 and near_close)):
                self.close_for_time_stop(item)
                return {"status": "time_stop_requested"}
            stop = number(stops[0]["stop_price"], positive=True)
            next_stop = D(str(trailing_stop(bars, float(order["filled_avg_price"]), float(stop)))).quantize(D(".01"), rounding=ROUND_DOWN)
            bid, _ = quote_prices(self.broker.quote(item["symbol"]))
            if next_stop > stop and next_stop < bid - D(".01") and not observe:
                self.ledger.event("tighten_requested", {"id": stops[0]["id"], "stop": str(next_stop)})
                with process_lock(self.root / "state/execution.lock", timeout=20):
                    if self.ledger.get("halted", True):
                        raise StateError("halted")
                    self.broker.replace_stop(stops[0]["id"], str(next_stop))
                return {"status": "stop_tightened"}
        if observe:
            return {"status": "observing", "blockers": blockers}
        if blockers or cfg.get("trading_enabled") is not True:
            return {"status": "qualification_blocked", "blockers": blockers}
        if session.hour < 10 or (session.hour, session.minute) >= (15, 30):
            return {"status": "outside_entry_window"}
        if self.ledger.get("last_exit_at") and (utcnow()-timestamp(self.ledger.get("last_exit_at"))).total_seconds() < 1800:
            return {"status": "exit_cooldown"}
        # Fixed order, no uncalibrated EV score or optimization of a held-out set.
        for symbol in cfg["enabled_symbols"]:
            if any(p["symbol"] == symbol for p in positions):
                continue
            bars = self.history(symbol, session.date())
            candidate = signal(symbol, bars)
            if candidate is None:
                continue
            self.ledger.event("candidate", {"symbol": symbol, "as_of": str(candidate.as_of),
                                           "entry": candidate.entry, "stop": candidate.stop,
                                           "thesis": candidate.thesis})
            bid, ask = quote_prices(self.broker.quote(symbol))
            if not D(str(candidate.entry)) <= ask <= D(str(candidate.max_entry)):
                continue
            asset = self.broker.asset(symbol)
            if asset.get("tradable") is not True or asset.get("class") != "us_equity" or asset.get("exchange") == "OTC":
                continue
            # Reconcile AGAIN after data downloads; quotes and risk inputs must be fresh.
            account, positions, orders, market, marked = self.refresh()
            if not market["is_open"]:
                return {"status": "market_closed"}
            _, fresh_ask = quote_prices(self.broker.quote(symbol))
            if not D(str(candidate.entry)) <= fresh_ask <= D(str(candidate.max_entry)):
                continue
            active_names = {p["symbol"] for p in positions}
            active_names.update(i["symbol"] for i in self.ledger.intents() if i["snapshot"]["status"] not in TERMINAL)
            for name in active_names:
                histories[name] = self.history(name, session.date())
            local_histories = {name: histories[name] for name in active_names}
            local_histories[symbol] = bars
            state = self.snapshot(account, positions, marked, local_histories)
            limit = D(str(candidate.max_entry)).quantize(D(".01"), rounding=ROUND_DOWN)
            stop = D(str(candidate.stop)).quantize(D(".01"), rounding=ROUND_UP)
            if stop >= limit:
                continue
            scale = D(".5") * (D(".5") if marked[3] >= D(".015") else D(1))
            qty = size_long(marked[0], limit, stop, risk_fraction=cfg["risk_fraction"], multiplier=scale,
                            notional_fraction=min(D(".15"), number(cfg["sleeve_fraction"])))
            existing = sum(e.quantity * e.price for e in state.exposures)
            available = min(marked[0] * number(cfg["sleeve_fraction"]) - existing, number(account["buying_power"]))
            qty = min(qty, max(D(0), (available / limit).to_integral_value(rounding=ROUND_DOWN)))
            if qty < 1:
                continue
            proposal = Proposal(symbol, qty, limit, stop, limit + 2*(limit-stop), candidate.thesis,
                                candidate.invalidation, average_daily_dollar_volume=D(str(candidate.average_daily_dollar_volume)),
                                listed=True, halted=False)
            if self.submit(proposal, state, candidate.as_of):
                return {"status": "submitted", "symbol": symbol}
        return {"status": "no_qualified_entry"}

    def close_for_time_stop(self, item):
        """Persist exit intent, settle protective cancels, submit a single close."""
        with process_lock(self.root / "state/execution.lock", timeout=20):
            if self.ledger.get("halted", True):
                raise StateError("halted")
            if not self.ledger.reserve_close(item["client_id"], item["symbol"]):
                return False
            try:
                for leg in item["snapshot"]["legs"]:
                    if leg["status"] not in TERMINAL:
                        self.broker.cancel(leg["id"])
                deadline = time.monotonic() + 10
                while any(o.get("symbol") == item["symbol"] for o in self.broker.open_orders()):
                    if time.monotonic() >= deadline:
                        raise StateError("exit_cancellation_unsettled")
                    time.sleep(.25)
                order = self.broker.close_position(item["symbol"])
                self.ledger.record_close(item["client_id"], order)
            except Exception:
                self.halt("close_requires_reconciliation")
                raise StateError("close_requires_reconciliation") from None
        return True

    def write_report(self, account, positions, orders):
        report = {"at": utcnow().isoformat(), "mode": "paper", "equity": account["equity"],
                  "cash": account["cash"], "buying_power": account["buying_power"],
                  "positions": [{key: p.get(key) for key in ("symbol", "qty", "avg_entry_price", "unrealized_pl")}
                                for p in positions], "open_orders": len(orders),
                  "halted": self.ledger.get("halted", True)}
        report["cash_fraction"] = str(number(account["cash"]) / number(account["equity"], positive=True))
        report["target_cash_fraction"] = "0.10"
        report["allocation_target_met"] = number(report["cash_fraction"]) <= D(".10")
        self.ledger.set("latest_report", report)
        path = self.root / "reports" / ("daily_" + utcnow().date().isoformat() + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(report, indent=2))


def worker(root, observe=False, *, once=False):
    engine = Engine(root)
    with process_lock(Path(root) / "state/worker.lock"):
        while True:
            try:
                result = engine.cycle(observe=observe)
                print(json.dumps(result), flush=True)
            except Exception:
                engine.halt("worker_failure")
                print('{"status":"halted","reason":"worker_failure"}', flush=True)
                return 1
            if once:
                return 0
            time.sleep(300)


def watchdog(root, *, once=False):
    engine = Engine(root)
    breaker = CircuitBreaker(Path(root) / "state/breaker.json")
    with process_lock(Path(root) / "state/watchdog.lock"):
        while True:
            reason = None
            try:
                # A previously latched halt must reach liquidation even if account
                # health or clock endpoints are unavailable.
                if not engine.ledger.get("halted", True):
                    account = engine.broker.account()
                    health(account)
                    session = clock_time(engine.broker.clock())
                    marked = engine.ledger.mark_equity(account, session.date().isoformat())
                    reason = breach_reason(*marked)
                    if reason:
                        engine.halt(reason)
                    else:
                        deadline = time.monotonic() + 8
                        for item in engine.ledger.active_intents():
                            if engine.ledger.get("halted", True):
                                break
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise StateError("watchdog_sweep_deadline")
                            order = engine.broker.order(item["client_id"], timeout=min(2, remaining))
                            if time.monotonic() > deadline:
                                raise StateError("watchdog_sweep_deadline")
                            if order is None:
                                if (utcnow()-timestamp(item["created_at"])).total_seconds() > 15:
                                    raise StateError("ambiguous_submission")
                                continue
                            filled = number(order["filled_qty"])
                            qty = number(order["qty"], positive=True)
                            if filled > qty:
                                raise StateError("entry_overfill")
                            if 0 < filled < qty:
                                if 0 <= (utcnow()-timestamp(item['created_at'])).total_seconds() < 15:
                                    continue
                                raise StateError("partial_entry_requires_flatten")
                            legs = order.get("legs") or []
                            if any(leg.get("symbol") != item["symbol"] or leg.get("side") != "sell" for leg in legs):
                                raise StateError("invalid_exit_leg")
                            sold = sum(number(leg["filled_qty"]) for leg in legs)
                            closing = next((e for e in engine.ledger.exits()
                                            if e["parent_id"] == item["client_id"]), None)
                            if closing:
                                close_age = (utcnow()-timestamp(closing["created_at"])).total_seconds()
                                close_order = closing["snapshot"]
                                if close_order is None:
                                    if 0 <= close_age < 30:
                                        continue
                                    raise StateError("ambiguous_time_exit")
                                if close_order.get("side") != "sell" or close_order.get("symbol") != item["symbol"]:
                                    raise StateError("invalid_time_exit")
                                sold += number(close_order["filled_qty"])
                                if sold > filled:
                                    raise StateError("exit_overfill")
                                if filled > sold and close_order["status"] not in TERMINAL:
                                    if 0 <= close_age < 30:
                                        continue
                                    raise StateError("time_exit_not_settled")
                            if sold > filled:
                                raise StateError("exit_overfill")
                            if filled > sold:
                                stops = protective_stops(order)
                                if (len(stops) != 1 or number(stops[0]["qty"], positive=True)
                                        - number(stops[0]["filled_qty"]) < filled - sold):
                                    raise StateError("missing_broker_stop")
                        # Readiness means a completed health/protection scan.
                        if not engine.ledger.get("halted", True):
                            engine.ledger.set("watchdog_heartbeat", utcnow().isoformat())
            except Exception as exc:
                # Preserve fixed internal failure codes without broker payloads or secrets.
                detail = str(exc) if isinstance(exc, StateError) else type(exc).__name__
                if not re.fullmatch(r'[A-Za-z_]{1,80}', detail):
                    detail = 'unclassified_failure'
                engine.ledger.event('watchdog_failure_detail', {'code': detail})
                engine.halt("watchdog_api_or_protection_failure")
            if engine.ledger.get("halted", True):
                try:
                    # Halt is durable before waiting for an in-flight entry POST.
                    # Every worker mutation uses this same cross-process lock.
                    with process_lock(Path(root) / "state/execution.lock", timeout=20):
                        flat = breaker.trip(engine.broker, reason=reason or "reconciliation")
                    engine.ledger.event("flatten_attempt", {"flat_verified": flat})
                    engine.ledger.set("watchdog_flat_verified", flat)
                except Exception:
                    engine.ledger.set("watchdog_flat_verified", False)
                    engine.ledger.event("flatten_attempt", {"flat_verified": False})
                # Keep monitoring even after flat verification: an ambiguous POST
                # may become visible at the broker after a client timeout.
            if once:
                return 0
            time.sleep(5)


def supervise(root):
    root = Path(root).resolve()
    cfg = config(root)
    blockers = validate_evidence(root, utcnow())
    if blockers or cfg.get("trading_enabled") is not True:
        print(json.dumps({"status": "blocked", "blockers": blockers or ["trading disabled in config"]}, indent=2))
        return 1
    engine = Engine(root)
    if engine.ledger.get("initialized") is not True or engine.ledger.get("halted", True):
        raise StateError("initialize_flat_account_or_resolve_halt")
    engine.refresh()
    with process_lock(root / "state/supervisor.lock"):
        base = [sys.executable, "-m", "alphagrid", "--root", str(root)]
        launch = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        # Discard old readiness so a prior process cannot authorize this launch.
        engine.ledger.set("watchdog_heartbeat", None)
        guard = None
        trader = None
        try:
            guard = subprocess.Popen(base + ["watchdog"], **launch)
            for _ in range(30):
                if guard.poll() is not None or engine.ledger.get("halted", True):
                    raise StateError("watchdog_failed_startup")
                stamp = engine.ledger.get("watchdog_heartbeat")
                if stamp and 0 <= (utcnow()-timestamp(stamp)).total_seconds() < 5:
                    break
                time.sleep(1)
            else:
                raise StateError("watchdog_failed_startup")
            trader = subprocess.Popen(base + ["worker"], **launch)
            while guard.poll() is None and trader.poll() is None:
                if engine.ledger.get("halted", True):
                    break
                time.sleep(1)
            engine.halt("service_process_exited_or_halted")
            return 1
        except KeyboardInterrupt:
            engine.halt("operator_stop")
            return 0
        except Exception:
            engine.halt("supervisor_failure")
            return 1
        finally:
            if trader and trader.poll() is None:
                trader.terminate()
                try:
                    trader.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    trader.kill()
                    trader.wait(timeout=15)
            # Never kill the only flattening process on an arbitrary timeout.
            # A halted watchdog stays alive to catch late broker acknowledgments.
            if guard is None or guard.poll() is not None:
                try:
                    guard = subprocess.Popen(base + ["watchdog"], **launch)
                except Exception:
                    engine.halt("watchdog_restart_failed")
            print(json.dumps({"status": "halted", "watchdog_monitoring": guard is not None and guard.poll() is None}), flush=True)
