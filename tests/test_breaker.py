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
    assert broker.calls == ["cancel", "orders", "close", "positions", "orders"]


@pytest.mark.parametrize("failure", ["cancel", "orders", "close", "positions"])
def test_failures_halt_without_leaking(tmp_path, failure):
    breaker = CircuitBreaker(tmp_path / "breaker.json")
    clock = Clock()
    assert not breaker.trip(Broker(failure=failure), clock=clock, sleep=clock.sleep)
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
                clock.now += timeout
    assert not CircuitBreaker(tmp_path / "b.json").trip(SlowBroker(), clock=clock, sleep=clock.sleep)


def test_invalid_trip_and_disk_failure(tmp_path, monkeypatch):
    breaker = CircuitBreaker(tmp_path / "breaker.json")
    for args in [dict(reason="arbitrary external text"), dict(budget=61)]:
        with pytest.raises(ValueError):
            breaker.trip(Broker(), **args)
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(breaker, "_persist", fail)
    broker = Broker()
    with pytest.raises(OSError):
        breaker.trip(broker)
    assert broker.calls == []
