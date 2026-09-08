"""Offline service tests: all broker responses are explicit fixtures."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import json
import pytest

from alphagrid import service
from alphagrid.ledger import StateError, utcnow
from alphagrid.risk.risk_gate import Proposal, Snapshot
from alphagrid.strategies.trend_pullback import Bar
from alphagrid.strategies.trend_pullback import Candidate


def account():
    return dict(id="offline-fixture", status="ACTIVE", trading_blocked=False,
                account_blocked=False, currency="USD", equity="100000",
                last_equity="100000", cash="100000", buying_power="100000", daytrade_count=0)


def authorization():
    return dict(mode="paper", live_trading_authorized=False,
                paper_endpoint="https://paper-api.alpaca.markets", enabled_strategies=["6.1"],
                enabled_symbols=["SPY"], risk_fraction="0.0025", sleeve_fraction="0.10",
                trading_enabled=True)


class FakeBroker:
    def __init__(self):
        self.submissions = []
        self.fail_submission = False

    def account(self):
        return account()

    def positions(self):
        return []

    def open_orders(self):
        return []

    def clock(self):
        return dict(timestamp=utcnow().isoformat(), is_open=True)

    def submit_bracket(self, payload):
        self.submissions.append(payload)
        if self.fail_submission:
            raise TimeoutError("offline ambiguous response")
        return dict(payload, id="fake-order", status="new", filled_qty="0", legs=[])


@pytest.fixture
def engine(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/authorization.yaml").write_text(json.dumps(authorization()))
    instance = service.Engine(tmp_path, FakeBroker())
    instance.initialize()
    instance.ledger.reconcile(account(), [], [])
    instance.ledger.set("watchdog_heartbeat", utcnow().isoformat())
    return instance


def proposal():
    return Proposal("SPY", D(10), D(100), D(98), D(104), "offline thesis", "stop invalidates",
                    average_daily_dollar_volume=D(100_000_000), listed=True, halted=False)


def snapshot():
    return Snapshot(D(100000), D(100000), D(100000), D(0), reconciled=True,
                    healthy=True, halted=False, age_seconds=D(0), cooldown_clear=True,
                    journal_verified=True)


@pytest.mark.parametrize("field,value", [
    ("mode", "live"), ("live_trading_authorized", True),
    ("paper_endpoint", "https://api.alpaca.markets"), ("enabled_strategies", ["6.2"]),
    ("enabled_symbols", ["SPY", "SPY"]), ("enabled_symbols", ["spy"]),
    ("risk_fraction", "0.003"), ("sleeve_fraction", "0.91")])
def test_config_rejects_expansion(engine, field, value):
    cfg = authorization()
    cfg[field] = value
    (engine.root / "config/authorization.yaml").write_text(json.dumps(cfg))
    with pytest.raises(StateError):
        service.config(engine.root)


@pytest.mark.parametrize("field,value", [("status", "INACTIVE"), ("trading_blocked", True),
                                        ("account_blocked", None), ("currency", "EUR")])
def test_account_health_requires_explicit_permissions(field, value):
    item = account()
    item[field] = value
    with pytest.raises(StateError):
        service.health(item)


def test_clock_rejects_old_future_or_nonboolean_open():
    now = utcnow()
    for age in (-31, 31):
        with pytest.raises(StateError, match="stale_broker_clock"):
            service.clock_time(dict(timestamp=(now-timedelta(seconds=age)).isoformat(), is_open=True), now)
    with pytest.raises(StateError, match="invalid_broker_clock"):
        service.clock_time(dict(timestamp=now.isoformat(), is_open=1), now)
    assert service.clock_time(dict(timestamp=now.isoformat(), is_open=False), now).tzinfo is not None


@pytest.mark.parametrize("age,bid,ask", [(31, "100", "100.1"), (-1, "100", "100.1"),
                                      (0, "101", "100"), (0, "100", "101")])
def test_quotes_reject_stale_future_crossed_and_wide(age, bid, ask):
    now = utcnow()
    with pytest.raises(StateError, match="stale_or_wide_quote"):
        service.quote_prices(dict(t=(now-timedelta(seconds=age)).isoformat(), bp=bid, ap=ask), now)


def test_quote_accepts_fresh_tight_market():
    now = utcnow()
    assert service.quote_prices(dict(t=now.isoformat(), bp="100", ap="100.10"), now) == (D(100), D("100.10"))


def test_correlation_requires_aligned_varying_history():
    history = [Bar(date(2020,1,1)+timedelta(days=i), 100+i*i/100, 101+i*i/100,
                   99+i*i/100, 100+i*i/100, 1000000) for i in range(61)]
    assert service.correlation(history, history) == D(1)
    with pytest.raises(StateError, match="insufficient_correlation_history"):
        service.correlation(history, history[1:])
    flat = [replace(b, open=100, high=101, low=99, close=100) for b in history]
    with pytest.raises(StateError, match="undefined_correlation"):
        service.correlation(flat, flat)


def test_submit_reserves_before_post_and_deduplicates_across_restart(engine):
    original = engine.broker.submit_bracket
    def inspect_reservation(payload):
        intents = engine.ledger.intents()
        assert len(intents) == 1
        assert intents[0]["snapshot"] is None
        assert intents[0]["payload"] == payload
        return original(payload)
    engine.broker.submit_bracket = inspect_reservation
    assert engine.submit(proposal(), snapshot(), date(2020,1,1)) is True
    restarted = service.Engine(engine.root, engine.broker)
    assert restarted.submit(proposal(), snapshot(), date(2020,1,1)) is False
    assert len(engine.broker.submissions) == 1
    assert engine.ledger.intents()[0]["snapshot"]["id"] == "fake-order"


def test_ambiguous_post_persists_intent_and_halts(engine):
    engine.broker.fail_submission = True
    with pytest.raises(StateError, match="submission_requires_reconciliation"):
        engine.submit(proposal(), snapshot(), date(2020,1,1))
    assert engine.ledger.get("halted") is True
    assert engine.ledger.intents()[0]["snapshot"] is None
    assert (engine.root / "HALT.md").is_file()
    with pytest.raises(StateError, match="halted"):
        engine.submit(proposal(), snapshot(), date(2020,1,1))
    assert len(engine.broker.submissions) == 1


def test_submit_requires_fresh_watchdog(engine):
    engine.ledger.set("watchdog_heartbeat", (utcnow()-timedelta(seconds=16)).isoformat())
    with pytest.raises(StateError, match="watchdog_missing"):
        engine.submit(proposal(), snapshot(), date(2020,1,1))
    assert engine.broker.submissions == []
    assert engine.ledger.intents() == []


def test_submit_risk_rejection_never_reserves(engine):
    assert engine.submit(replace(proposal(), stop=D(101)), snapshot(), date(2020,1,1)) is False
    assert engine.broker.submissions == []
    assert engine.ledger.intents() == []


def test_submit_requires_recent_reconciliation(engine):
    engine.ledger.set("last_reconciled", (utcnow()-timedelta(seconds=31)).isoformat())
    assert engine.submit(proposal(), snapshot(), date(2020,1,1)) is False
    assert engine.broker.submissions == []
    assert engine.ledger.intents() == []


def test_watchdog_halt_between_reservation_and_post_blocks_mutation(engine, monkeypatch):
    reserve = engine.ledger.reserve
    def halt_after_reserve(payload, thesis):
        result = reserve(payload, thesis)
        engine.ledger.halt("offline_race_fixture")
        return result
    monkeypatch.setattr(engine.ledger, "reserve", halt_after_reserve)
    with pytest.raises(StateError, match="halted_before_post"):
        engine.submit(proposal(), snapshot(), date(2020,1,1))
    assert engine.broker.submissions == []
    assert len(engine.ledger.intents()) == 1


def test_execution_lock_held_during_broker_mutation_and_released_after(engine):
    original = engine.broker.submit_bracket
    def inspect_lock(payload):
        with pytest.raises(OSError):
            with service.process_lock(engine.root / "state/execution.lock"):
                pytest.fail("concurrent mutation acquired an occupied lock")
        return original(payload)
    engine.broker.submit_bracket = inspect_lock
    assert engine.submit(proposal(), snapshot(), date(2020,1,1)) is True
    with service.process_lock(engine.root / "state/execution.lock"):
        pass


@pytest.mark.parametrize("observe,expected", [(True,"observing"), (False,"qualification_blocked")])
def test_observe_and_unqualified_cycles_never_submit(engine, monkeypatch, observe, expected):
    monkeypatch.setattr(service, "validate_evidence", lambda root, now: ["missing_research"])
    assert engine.cycle(observe=observe)["status"] == expected
    assert engine.broker.submissions == []
    assert engine.ledger.intents() == []
    assert engine.ledger.get("latest_report")["mode"] == "paper"


class CycleBroker(FakeBroker):
    """Unfilled broker orders preserve account cash and appear in open orders."""
    def __init__(self, now):
        super().__init__()
        self.now = now
        self.orders = {}
        self.buying_power = "100000"
        self.asks = ["100.05"]
        self.quote_calls = 0

    def account(self):
        return dict(account(), buying_power=self.buying_power)

    def clock(self):
        return dict(timestamp=self.now.isoformat(), is_open=True)

    def quote(self, symbol):
        ask = self.asks[min(self.quote_calls, len(self.asks)-1)]
        self.quote_calls += 1
        return dict(t=self.now.isoformat(), bp=str(D(ask)-D(".05")), ap=ask)

    def asset(self, symbol):
        return dict(tradable=True, **{"class": "us_equity", "exchange": "NYSE"})

    def submit_bracket(self, payload):
        order = super().submit_bracket(payload)
        self.orders[payload["client_order_id"]] = order
        return order

    def order(self, client_id):
        return self.orders.get(client_id)

    def open_orders(self):
        return list(self.orders.values())


@pytest.fixture
def cycle_engine(tmp_path, monkeypatch):
    import alphagrid.ledger as ledger_module
    now = datetime(2026,9,8,14,30,tzinfo=timezone.utc)  # 10:30 Eastern, entry window
    monkeypatch.setattr(service, "utcnow", lambda: now)
    monkeypatch.setattr(ledger_module, "utcnow", lambda: now)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/authorization.yaml").write_text(json.dumps(authorization()))
    instance = service.Engine(tmp_path, CycleBroker(now))
    instance.initialize()
    instance.ledger.set("watchdog_heartbeat", now.isoformat())
    history = [Bar(now.date()-timedelta(days=61-i), 100,101,99,100,1_000_000)
               for i in range(61)]
    monkeypatch.setattr(instance, "history", lambda symbol, session: history)
    # The fixture isolates orchestration. These synthetic approvals and signals
    # are test inputs, never research evidence or actual qualification records.
    monkeypatch.setattr(service, "validate_evidence", lambda root, moment: [])
    monkeypatch.setattr(service, "signal", lambda symbol, bars: Candidate(
        symbol, bars[-1].date, 100,98,104,"offline signal","stop",2,100_000_000,100.1))
    return instance


def test_full_cycle_submits_persists_and_reconciles_without_duplicate(cycle_engine):
    engine = cycle_engine
    assert engine.cycle() == {"status": "submitted", "symbol": "SPY"}
    intent = engine.ledger.intents()[0]
    assert intent["snapshot"]["status"] == "new"
    assert intent["payload"]["order_class"] == "bracket"
    assert D(intent["payload"]["limit_price"]) == D("100.10")
    assert D(intent["payload"]["qty"]) > 0
    assert engine.cycle()["status"] == "no_qualified_entry"
    assert len(engine.broker.submissions) == 1
    assert len(engine.ledger.intents()) == 1
    assert engine.ledger.get("halted") is False
    assert engine.ledger.get("last_reconciled") == engine.broker.now.isoformat()


def test_full_cycle_no_available_buying_power_prevents_entry(cycle_engine):
    cycle_engine.broker.buying_power = "0"
    assert cycle_engine.cycle()["status"] == "no_qualified_entry"
    assert cycle_engine.broker.submissions == []
    assert cycle_engine.ledger.intents() == []


def test_full_cycle_second_quote_outside_trigger_prevents_entry(cycle_engine):
    cycle_engine.broker.asks = ["100.05", "100.20"]
    assert cycle_engine.cycle()["status"] == "no_qualified_entry"
    assert cycle_engine.broker.quote_calls == 2
    assert cycle_engine.broker.submissions == []
    assert cycle_engine.ledger.intents() == []
