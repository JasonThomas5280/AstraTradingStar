import json
from decimal import Decimal as D
import pytest
from hypothesis import given, strategies as st
from alphagrid.risk.circuit_breaker import CircuitBreaker, breach_reason


class Clock:
    now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Broker:
    def __init__(self, *, positions=None, orders=None, failure=None):
        self.remaining = [] if positions is None else positions
        self.orders = [] if orders is None else orders
        self.failure = failure
        self.calls = []

    def call(self, name, timeout):
        assert 0 < timeout <= 60
        self.calls.append(name)
        if self.failure == name:
            raise TimeoutError("sensitive broker detail must not reach disk")

    def cancel_all(self, *, timeout):
        self.call("cancel", timeout)

    def close_all(self, *, timeout):
        self.call("close", timeout)

    def open_orders(self, *, timeout):
        self.call("orders", timeout)
        return self.orders

    def positions(self, *, timeout):
        self.call("positions", timeout)
        return self.remaining


@pytest.mark.parametrize("args,expected", [
    ((100000, 100000, 100000, 0), None),
    ((97500, 100000, 100000, 0), "daily_loss"),
    ((100000, 100000, 100000, ".025"), "daily_loss"),
    ((90000, 90000, 100000, 0), "drawdown"),
    ((100000, 100000, 90000, 0), "inconsistent_equity_history"),
    ((0, 100000, 100000, 0), "daily_loss"),
    (("NaN", 100000, 100000, 0), "invalid_equity"),
    ((100000, 0, 100000, 0), "invalid_equity"),
])
def test_breaches(args, expected):
    assert breach_reason(*args) == expected


@given(equity=st.integers(0, 97500))
def test_daily_breach_property(equity):
    assert breach_reason(equity, 100000, 100000, 0) is not None


def test_latch_corruption_and_missing(tmp_path):
    path = tmp_path / "breaker.json"
    breaker = CircuitBreaker(path)
    assert breaker.is_halted()
    for contents in ["bad json", "null", "[]", "{}", '{"halted": "false"}']:
        path.write_text(contents)
        assert breaker.is_halted()
    path.write_text('{"halted": false}')
    assert not breaker.is_halted()


def test_success_is_durable_and_still_halted(tmp_path):
    breaker = CircuitBreaker(tmp_path / "breaker.json")
    clock, broker = Clock(), Broker()
    assert breaker.trip(broker, clock=clock, sleep=clock.sleep)
    assert CircuitBreaker(breaker.path).is_halted()
    state = json.loads(breaker.path.read_text())
    assert state["flat_verified"] and state["postmortem_required"]
    assert broker.calls == ["cancel", "orders", "positions", "close", "positions", "orders"]


@pytest.mark.parametrize("failure", ["cancel", "orders", "close", "positions"])
def test_failures_halt_without_leaking(tmp_path, failure):
    breaker = CircuitBreaker(tmp_path / "breaker.json")
    clock = Clock()
    assert not breaker.trip(Broker(failure=failure, positions=[{"symbol": "SPY", "side": "long", "qty": "1"}]), clock=clock, sleep=clock.sleep)
    assert clock.now == 60 and breaker.is_halted()
    assert "sensitive" not in breaker.path.read_text()
    assert not json.loads(breaker.path.read_text())["flat_verified"]


@pytest.mark.parametrize("remaining,orders", [([{"qty": "0.01"}], []),
    ([], [{"status": "pending_cancel"}]), ([], None)])
def test_partial_fills_and_unknown_orders_not_flat(tmp_path, remaining, orders):
    breaker, clock, broker = CircuitBreaker(tmp_path / "breaker.json"), Clock(), Broker()
    broker.remaining, broker.orders = remaining, orders
    assert not breaker.trip(broker, clock=clock, sleep=clock.sleep)
    if orders != []:
        assert "close" not in broker.calls


def test_partial_fill_then_verified_exit(tmp_path):
    class FillingBroker(Broker):
        def positions(self, *, timeout):
            self.call("positions", timeout)
            return [] if self.calls.count("positions") > 1 else [{"qty": "0.1"}]
    breaker, clock = CircuitBreaker(tmp_path / "breaker.json"), Clock()
    assert breaker.trip(FillingBroker(), clock=clock, sleep=clock.sleep)
    assert clock.now == 1


@pytest.mark.parametrize("slow_method", ["cancel", "orders", "close", "positions"])
def test_slow_calls_cannot_claim_timely_flat(tmp_path, slow_method):
    clock = Clock()
    class SlowBroker(Broker):
        def call(self, name, timeout):
            super().call(name, timeout)
            if name == slow_method:
                clock.now += 60  # Deliberately violates its <=10s call timeout.
    assert not CircuitBreaker(tmp_path / "b.json").trip(SlowBroker(), clock=clock, sleep=clock.sleep)


def test_invalid_trip_and_disk_failure(tmp_path, monkeypatch):
    breaker = CircuitBreaker(tmp_path / "breaker.json")
    for args in [dict(reason="arbitrary external text"), dict(budget=61)]:
        with pytest.raises(ValueError):
            breaker.trip(Broker(), **args)
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(breaker, "_persist", fail)
    broker = Broker()
    with pytest.raises(OSError):
        breaker.trip(broker)
    assert broker.calls == []

class LiquidatingBroker(Broker):
    def __init__(self, *, ambiguous=False, late_order=False, fill=True):
        super().__init__(positions=[{"symbol": "SPY", "side": "long", "qty": "1"}])
        self.ambiguous, self.late_order, self.fill = ambiguous, late_order, fill
        self.polls = 0
        self.cancelled_ids = []

    def close_all(self, *, timeout):
        self.call("close", timeout)
        self.orders = [{"id": "liquidation", "symbol": "SPY", "side": "sell", "type": "market",
                        "order_class": "simple", "status": "new"}]
        if self.late_order:
            self.orders.append({"id": "late-entry", "symbol": "QQQ", "side": "buy", "type": "limit"})
        if self.ambiguous:
            raise TimeoutError("accepted order but response lost")
        return [self.orders[0]]

    def cancel_all(self, *, timeout):
        self.call("cancel", timeout)
        assert not any(order.get("id") == "liquidation" for order in self.orders)
        self.orders = []

    def cancel(self, order_id, *, timeout):
        self.call("cancel_one", timeout)
        assert order_id != "liquidation"
        self.cancelled_ids.append(order_id)
        self.orders = [order for order in self.orders if order["id"] != order_id]

    def positions(self, *, timeout):
        self.call("positions", timeout)
        if "close" in self.calls:
            self.polls += 1
            if self.fill and self.polls >= 3:
                self.remaining, self.orders = [], []
        return self.remaining


@pytest.mark.parametrize("ambiguous", [False, True])
def test_liquidation_is_not_canceled_or_duplicated(tmp_path, ambiguous):
    clock, broker = Clock(), LiquidatingBroker(ambiguous=ambiguous)
    breaker = CircuitBreaker(tmp_path / "breaker.json")
    assert breaker.trip(broker, clock=clock, sleep=clock.sleep)
    assert broker.calls.count("cancel") == 1
    assert broker.calls.count("close") == 1
    assert broker.cancelled_ids == []
    assert json.loads(breaker.path.read_text())["flat_verified"] is True


def test_late_external_order_canceled_without_disturbing_liquidation(tmp_path):
    clock, broker = Clock(), LiquidatingBroker(late_order=True)
    assert CircuitBreaker(tmp_path / "breaker.json").trip(broker, clock=clock, sleep=clock.sleep)
    assert broker.cancelled_ids == ["late-entry"]
    assert broker.calls.count("close") == 1 and broker.calls.count("cancel") == 1


@pytest.mark.parametrize("ambiguous", [False, True])
def test_restart_preserves_pending_close_and_never_resubmits(tmp_path, ambiguous):
    path, clock, broker = tmp_path / "breaker.json", Clock(), LiquidatingBroker(ambiguous=ambiguous, fill=False)
    assert not CircuitBreaker(path).trip(broker, clock=clock, sleep=clock.sleep, budget=2)
    before = broker.calls[:]
    broker.fill = True
    assert CircuitBreaker(path).trip(broker, clock=clock, sleep=clock.sleep, budget=3)
    assert broker.calls.count("close") == before.count("close") == 1
    assert broker.calls.count("cancel") == before.count("cancel") == 1


def test_crash_window_with_invisible_close_never_resubmits(tmp_path):
    clock, broker = Clock(), LiquidatingBroker(ambiguous=True, fill=False)
    breaker = CircuitBreaker(tmp_path / "breaker.json")
    assert not breaker.trip(broker, clock=clock, sleep=clock.sleep, budget=1)
    broker.orders = []  # Accepted order temporarily absent from the broker snapshot.
    assert not CircuitBreaker(breaker.path).trip(broker, clock=clock, sleep=clock.sleep, budget=2)
    assert broker.calls.count("close") == 1
    assert json.loads(breaker.path.read_text())["uncertain_close"] is True


def test_close_intent_committed_before_broker_mutation(tmp_path):
    breaker, clock = CircuitBreaker(tmp_path / "breaker.json"), Clock()
    class InspectingBroker(Broker):
        def close_all(self, *, timeout):
            state = json.loads(breaker.path.read_text())
            assert state["closing_started"] and state["uncertain_close"] and state["halted"]
            super().close_all(timeout=timeout)
    assert breaker.trip(InspectingBroker(), clock=clock, sleep=clock.sleep)


@pytest.mark.parametrize("contents", ["null", "[]"])
def test_invalid_previous_state_still_latches(tmp_path, contents):
    path, clock = tmp_path / "breaker.json", Clock()
    path.write_text(contents)
    assert CircuitBreaker(path).trip(Broker(), clock=clock, sleep=clock.sleep)
    assert CircuitBreaker(path).is_halted()


def test_unknown_position_snapshot_never_submits_close(tmp_path):
    class UnknownBroker(Broker):
        def positions(self, *, timeout):
            return None
    clock, broker = Clock(), UnknownBroker()
    assert not CircuitBreaker(tmp_path / "b.json").trip(broker, clock=clock, sleep=clock.sleep, budget=2)
    assert "close" not in broker.calls


def test_unknown_orders_after_closing_cannot_verify_flat(tmp_path):
    class UnknownOrdersBroker(Broker):
        def open_orders(self, *, timeout):
            self.call("orders", timeout)
            return [] if "close" not in self.calls else None
    clock, broker = Clock(), UnknownOrdersBroker()
    assert not CircuitBreaker(tmp_path / "b.json").trip(broker, clock=clock, sleep=clock.sleep, budget=2)
    assert broker.calls.count("close") == 1
