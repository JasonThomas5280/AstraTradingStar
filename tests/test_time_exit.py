from copy import deepcopy
import pytest
from alphagrid.ledger import Ledger, StateError


def make_ledger(tmp_path):
    book = Ledger(tmp_path / "book.db")
    book.initialize({"id":"paper", "equity":"2500", "cash":"2500"}, [], [], "2026-09-08")
    payload = {"client_order_id":"ag-test", "symbol":"TEST", "qty":"1"}
    book.reserve(payload, "test thesis")
    order = {"id":"entry", "client_order_id":"ag-test", "symbol":"TEST", "side":"buy",
             "qty":"1", "filled_qty":"1", "filled_avg_price":"100", "status":"filled", "legs":[]}
    book.record("ag-test", order)
    return book


def close_order(**changes):
    result = {"id":"exit", "symbol":"TEST", "side":"sell", "qty":"1", "filled_qty":"1",
              "filled_avg_price":"105", "status":"filled"}
    result.update(changes)
    return result


def test_time_exit_durable_cash_and_position_reconciliation(tmp_path):
    book = make_ledger(tmp_path)
    assert book.reserve_close("ag-test", "TEST")
    assert not book.reserve_close("ag-test", "TEST")
    with pytest.raises(StateError, match="ambiguous_close"):
        book.reconcile({"id":"paper", "cash":"2400"}, [], [])
    book.record_close("ag-test", close_order())
    restored = Ledger(book.path)
    assert restored.reconcile({"id":"paper", "cash":"2505"}, [], []) == {}
    assert restored.active_intents() == []
    assert restored.get("last_exit_at")


def test_close_identity_and_fill_regression(tmp_path):
    book = make_ledger(tmp_path)
    with pytest.raises(StateError, match="unknown_close_parent"):
        book.reserve_close("unknown", "TEST")
    book.reserve_close("ag-test", "TEST")
    with pytest.raises(StateError, match="close_identity"):
        book.record_close("ag-test", close_order(symbol="OTHER"))
    book.record_close("ag-test", close_order())
    for changes in ({"id":"other"}, {"filled_qty":"0"}):
        with pytest.raises(StateError, match="close_regression"):
            book.record_close("ag-test", close_order(**changes))


def test_external_exit_overfill_halts(tmp_path):
    book = make_ledger(tmp_path)
    book.reserve_close("ag-test", "TEST")
    book.record_close("ag-test", close_order(qty="2", filled_qty="2"))
    with pytest.raises(StateError, match="exit_overfill"):
        book.reconcile({"id":"paper", "cash":"2610"}, [], [])
