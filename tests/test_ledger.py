import json
import sqlite3
from decimal import Decimal as D

import pytest

from alphagrid.ledger import Ledger, StateError


def account(cash="10000", equity=None):
    return {"id": "paper-fixture-account", "cash": cash, "equity": equity or cash,
            "last_equity": "10000"}


def payload():
    return {"client_order_id": "ag-test", "symbol": "SPY", "qty": "10"}


def buy(filled="0", price=None, legs=None):
    return {"id": "entry-id", "client_order_id": "ag-test", "symbol": "SPY",
            "side": "buy", "type": "limit", "status": "new", "qty": "10",
            "filled_qty": filled, "filled_avg_price": price, "legs": legs or []}


def sell(filled="0", price=None):
    return {"id": "exit-id", "symbol": "SPY", "side": "sell", "type": "limit",
            "status": "new", "qty": "10", "filled_qty": filled, "filled_avg_price": price}


def position(qty="10", **changes):
    return dict({"symbol": "SPY", "side": "long", "asset_class": "us_equity", "qty": qty}, **changes)


@pytest.fixture
def ledger(tmp_path):
    result = Ledger(tmp_path / "ledger.sqlite")
    result.initialize(account(), [], [], "2026-09-08")
    return result


def events(ledger):
    with ledger.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id")]


def test_flat_initialization_is_durable_and_once(ledger):
    restarted = Ledger(ledger.path)
    assert restarted.get("initialized") is True
    assert restarted.get("baseline_cash") == "10000"
    assert restarted.get("account_hash") != account()["id"]
    assert restarted.reconcile(account(), [], []) == {}
    with pytest.raises(StateError, match="already_initialized"):
        restarted.initialize(account(), [], [], "2026-09-08")
    assert [event["kind"] for event in events(ledger)] == ["initialized_flat"]


@pytest.mark.parametrize("positions,orders", [([position()], []), ([], [buy()])])
def test_initialization_requires_flat(tmp_path, positions, orders):
    ledger = Ledger(tmp_path / "state.sqlite")
    with pytest.raises(StateError, match="initialization_requires_flat_account"):
        ledger.initialize(account(), positions, orders, "2026-09-08")
    assert ledger.get("initialized") is None


def test_initialization_rejects_unexplained_equity(tmp_path):
    ledger = Ledger(tmp_path / "state.sqlite")
    with pytest.raises(StateError, match="invalid_flat_baseline"):
        ledger.initialize(account(equity="10001"), [], [], "2026-09-08")


def test_intent_survives_restart_before_submission_and_cannot_duplicate(ledger):
    assert ledger.reserve(payload(), "controlled paper test") is True
    restarted = Ledger(ledger.path)
    assert restarted.intents()[0]["snapshot"] is None
    assert restarted.intents()[0]["payload"] == payload()
    assert restarted.reserve(payload(), "duplicate attempt") is False
    with pytest.raises(StateError, match="ambiguous_submission"):
        restarted.reconcile(account(), [], [])
    assert [e["kind"] for e in events(ledger)] == ["initialized_flat", "reserved"]


def test_halted_or_uninitialized_ledger_cannot_reserve(ledger, tmp_path):
    with pytest.raises(StateError, match="halted"):
        Ledger(tmp_path / "new.sqlite").reserve(payload(), "test")
    ledger.halt("manual_stop")
    restarted = Ledger(ledger.path)
    assert restarted.get("halted") is True
    with pytest.raises(StateError, match="halted"):
        restarted.reserve(payload(), "test")
    assert restarted.get("halt_code") == "manual_stop"


def test_buy_partial_and_full_fill_reconcile_cash_and_position(ledger):
    ledger.reserve(payload(), "test")
    ledger.record("ag-test", buy("4", "100"))
    assert ledger.reconcile(account("9600", "10000"), [position("4")], [buy()]) == {"SPY": D(4)}
    ledger.record("ag-test", buy("10", "101"))
    assert ledger.reconcile(account("8990", "10000"), [position()], []) == {"SPY": D(10)}


def test_partial_exit_and_full_exit_reconcile_after_restart(ledger):
    ledger.reserve(payload(), "test")
    ledger.record("ag-test", buy("10", "100", [sell("3", "105")]))
    assert ledger.reconcile(account("9315", "10000"), [position("7")], [sell()]) == {"SPY": D(7)}
    restarted = Ledger(ledger.path)
    restarted.record("ag-test", buy("10", "100", [sell("10", "105")]))
    assert restarted.reconcile(account("10050"), [], []) == {}


@pytest.mark.parametrize("cash,positions,orders,code", [
    ("10000", [position()], [], "position_mismatch"),
    ("10001", [], [], "cash_mismatch"),
    ("10000", [], [{"id": "external"}], "external_order"),
    ("10000", [position(side="short")], [], "unsupported_position"),
    ("10000", [position(asset_class="crypto")], [], "unsupported_position"),
])
def test_external_changes_rejected(ledger, cash, positions, orders, code):
    with pytest.raises(StateError, match=code):
        ledger.reconcile(account(cash), positions, orders)
    assert ledger.get("last_reconciled") is None


def test_external_nested_order_rejected(ledger):
    ledger.reserve(payload(), "test")
    ledger.record("ag-test", buy())
    with pytest.raises(StateError, match="external_order"):
        ledger.reconcile(account(), [], [dict(buy(), legs=[{"id": "external"}])])


def test_wrong_account_rejected_even_if_cash_matches(ledger):
    with pytest.raises(StateError, match="wrong_account"):
        ledger.reconcile(dict(account(), id="other-account"), [], [])


def test_buy_fill_regression_preserves_previous_snapshot(ledger):
    ledger.reserve(payload(), "test")
    ledger.record("ag-test", buy("5", "100"))
    before = events(ledger)
    with pytest.raises(StateError, match="fill_regression"):
        ledger.record("ag-test", buy("4", "100"))
    assert ledger.intents()[0]["snapshot"]["filled_qty"] == "5"
    assert events(ledger) == before


@pytest.mark.parametrize("change,code", [({"symbol": "QQQ"}, "order_identity_mismatch"),
    ({"side": "sell"}, "order_identity_mismatch"), ({"client_order_id": "another"}, "order_identity_mismatch"),
    ({"qty": "11"}, "order_quantity_mismatch")])
def test_mismatched_broker_identity_rejected(ledger, change, code):
    ledger.reserve(payload(), "test")
    with pytest.raises(StateError, match=code):
        ledger.record("ag-test", dict(buy(), **change))
    assert ledger.intents()[0]["snapshot"] is None


def test_unknown_order_cannot_be_recorded(ledger):
    with pytest.raises(StateError, match="unknown_intent"):
        ledger.record("ag-test", buy())


def test_duplicate_snapshot_has_no_duplicate_event_and_strips_raw_text(ledger):
    ledger.reserve(payload(), "test")
    snapshot = dict(buy(), raw_message="untrusted broker text")
    ledger.record("ag-test", snapshot)
    ledger.record("ag-test", snapshot)
    assert [e["kind"] for e in events(ledger)].count("broker_update") == 1
    assert "untrusted broker text" not in json.dumps(ledger.intents())
    assert "untrusted broker text" not in json.dumps(events(ledger))


@pytest.mark.parametrize("snapshot,code", [
    (buy("11", "100"), "overfill"),
    (buy("10", "100", [sell("11", "101")]), "exit_overfill"),
    (buy("10", "100", [dict(sell(), side="buy")]), "invalid_exit_leg"),
])
def test_invalid_fill_reconciliation_rejected(ledger, snapshot, code):
    ledger.reserve(payload(), "test")
    ledger.record("ag-test", snapshot)
    with pytest.raises(StateError, match=code):
        ledger.reconcile(account(), [], [])


def test_duplicate_positions_rejected(ledger):
    ledger.reserve(payload(), "test")
    ledger.record("ag-test", buy("10", "100"))
    with pytest.raises(StateError, match="duplicate_position"):
        ledger.reconcile(account("9000"), [position("5"), position("5")], [])


def test_daily_loss_latches_recovery_and_rollover_uses_last_equity(ledger):
    assert ledger.mark_equity(account(equity="9500"), "2026-09-08") == (D(9500), D(10000), D(10000), D(".05"))
    ledger.mark_equity(account(equity="10200"), "2026-09-08")
    restarted = Ledger(ledger.path)
    assert D(restarted.get("worst_daily_loss")) == D(".05")
    assert D(restarted.get("peak_equity")) == D(10200)
    values = restarted.mark_equity(dict(account(equity="10098"), last_equity="10200"), "2026-09-09")
    assert values == (D(10098), D(10200), D(10200), D(".01"))
    assert restarted.get("session_date") == "2026-09-09"


def test_rollover_does_not_clear_explicit_halt(ledger):
    ledger.halt("daily_loss")
    ledger.mark_equity(account(), "2026-09-09")
    assert Ledger(ledger.path).get("halted") is True


@pytest.mark.parametrize("sql", ["UPDATE events SET kind='tampered'", "DELETE FROM events"])
def test_event_log_is_append_only(ledger, sql):
    original = events(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with ledger.connect() as db:
            db.execute(sql)
    assert events(ledger) == original
    ledger.event("operator_note", {"code": "checked"})
    assert events(ledger)[:-1] == original
