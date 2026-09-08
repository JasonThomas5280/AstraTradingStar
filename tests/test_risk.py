from dataclasses import replace
from decimal import Decimal as D
from itertools import combinations

import pytest
from hypothesis import given, strategies as st

from alphagrid.risk.numbers import number
from alphagrid.risk.position_sizer import size_long
from alphagrid.risk.stop_manager import Stop
from alphagrid.risk.risk_gate import Proposal, Snapshot, Exposure, evaluate


def proposal(**changes):
    return replace(Proposal("TEST", D(100), D(100), D(95), D(110),
                            "Pullback thesis", "Close below reversal low",
                            average_daily_dollar_volume=D(60_000_000),
                            listed=True, halted=False), **changes)


def snapshot(**changes):
    return replace(Snapshot(D(100_000), D(100_000), D(100_000), D(0),
                            reconciled=True, healthy=True, halted=False,
                            age_seconds=D(0), macro_multiplier=D(1),
                            cooldown_clear=True, journal_verified=True), **changes)


def test_good_proposal():
    decision = evaluate(proposal(), snapshot())
    assert decision.allowed
    assert "execution_disabled" in decision.reason
    with pytest.raises(TypeError):
        bool(decision)


@pytest.mark.parametrize("value", [None, True, False, "bad", "NaN", "Infinity", "-Infinity", -1])
def test_invalid_number(value):
    with pytest.raises(ValueError):
        number(value)
    assert not evaluate(proposal(entry=value), snapshot()).allowed


def test_sizing_round_down_and_caps():
    assert size_long(100_000, 100, 95) == 150
    assert size_long(100_000, 100, 90) == 75
    assert size_long(100, 100, 99) == 0
    assert size_long(1000, 100, 95, step="0.1") == D("1.5")
    assert size_long(100_000, 100, 90, multiplier="0.5") == 37


@pytest.mark.parametrize("changes", [dict(stop=100), dict(stop=101), dict(stop=0),
    dict(equity=0), dict(entry=-1), dict(risk_fraction="0.0076"), dict(multiplier="1.1"),
    dict(notional_fraction="0.16"), dict(step=0)])
def test_invalid_sizing(changes):
    args = dict(equity=100_000, entry=100, stop=95)
    args.update(changes)
    with pytest.raises(ValueError):
        size_long(**args)


@given(equity=st.integers(1, 10_000_000), entry=st.integers(2, 10_000),
       distance=st.integers(1, 1000))
def test_sizing_never_exceeds_budget(equity, entry, distance):
    stop = D(entry) / 2 if distance >= entry else D(entry - distance)
    qty = size_long(equity, entry, stop)
    assert qty * (entry - stop) <= D(equity) * D("0.0075")
    assert qty * entry <= D(equity) * D("0.15")
    assert qty == qty.to_integral_value()


def test_stops():
    long = Stop("long", D(95))
    short = Stop("short", D(105))
    assert long.triggered(95) and long.triggered(94) and not long.triggered(96)
    assert short.triggered(105) and short.triggered(106) and not short.triggered(104)
    assert long.tighten(96).price == 96
    assert short.tighten(104).price == 104
    assert long.tighten(95) == long
    for stop, price in [(long, 94), (short, 106)]:
        with pytest.raises(ValueError):
            stop.tighten(price)
    with pytest.raises(ValueError):
        Stop("other", D(95))


@given(old=st.integers(1, 10000), difference=st.integers(1, 10000))
def test_stops_never_widen(old, difference):
    with pytest.raises(ValueError):
        Stop("short", D(old)).tighten(old + difference)
    with pytest.raises(ValueError):
        Stop("long", D(old + difference)).tighten(old)


@pytest.mark.parametrize("changes,reason", [
    ({"reconciled": False}, "unverified_or_halted"),
    ({"reconciled": "true"}, "unverified_or_halted"),
    ({"healthy": False}, "unverified_or_halted"),
    ({"halted": True}, "unverified_or_halted"),
    ({"age_seconds": 31}, "stale_or_missing_evidence"),
    ({"cooldown_clear": False}, "stale_or_missing_evidence"),
    ({"journal_verified": False}, "stale_or_missing_evidence"),
    ({"peak_equity": 99_999}, "inconsistent_equity_history"),
    ({"equity": 99_000}, "inconsistent_equity_history"),
    ({"worst_daily_loss": D("0.025")}, "daily_loss"),
    ({"peak_equity": 120_000}, "drawdown"),
    ({"macro_multiplier": 2}, "invalid_input"),
    ({"day_trades": -1}, "invalid_input"),
    ({"day_trades": True}, "invalid_input"),
    ({"equity": 24_000, "start_equity": 24_000, "peak_equity": 24_000,
      "day_trades": 3}, "pdt_conservative_entry_block"),
])
def test_snapshot_denials(changes, reason):
    result = evaluate(proposal(), snapshot(**changes))
    assert not result.allowed and result.reason == reason


@pytest.mark.parametrize("changes,reason", [
    ({"asset": "crypto"}, "phase_one_only"),
    ({"asset": "option"}, "phase_one_only"),
    ({"strategy": "6.2"}, "phase_one_only"),
    ({"side": "short"}, "phase_one_only"),
    ({"symbol": ""}, "missing_thesis"),
    ({"symbol": "☃"}, "missing_thesis"),
    ({"thesis": " "}, "missing_thesis"),
    ({"invalidation": ""}, "missing_thesis"),
    ({"stop": 101}, "invalid_exit"),
    ({"target": 99}, "invalid_exit"),
    ({"listed": False}, "universe_or_halt"),
    ({"halted": True}, "universe_or_halt"),
    ({"average_daily_dollar_volume": 49_999_999}, "universe_or_halt"),
    ({"entry": 4, "stop": 3}, "universe_or_halt"),
    ({"quantity": 151}, "position_risk"),
    ({"quantity": 151, "stop": 99}, "concentration"),
])
def test_proposal_denials(changes, reason):
    result = evaluate(proposal(**changes), snapshot())
    assert not result.allowed and result.reason == reason


def exposures(count, quantity=100):
    return tuple(Exposure(f"S{i:02}", D(quantity), D(100)) for i in range(count))


def correlations(items, value=D(0)):
    return {pair: value for pair in combinations(sorted([e.symbol for e in items] + ["TEST"]), 2)}


@pytest.mark.parametrize("items,reason", [
    ((Exposure("TEST", D(1), D(100)),), "additions_disabled"),
    ((Exposure("X", D(1), D(100), "crypto"),), "unsupported_existing_exposure"),
    ((Exposure("", D(1), D(100)),), "invalid_input"),
    (exposures(12, 1), "position_count"),
    (exposures(1, 151), "concentration"),
    (exposures(10, 150), "gross_exposure"),
    (exposures(4, 100), "strategy_sleeve"),
    (exposures(1, 100), "missing_60_day_correlation"),
])
def test_portfolio_checks(items, reason):
    result = evaluate(proposal(), snapshot(exposures=items))
    assert not result.allowed and result.reason == reason


def test_pending_orders_are_aggregated():
    items = (Exposure("X", D(80), D(100)), Exposure("X", D(80), D(100)))
    assert evaluate(proposal(), snapshot(exposures=items)).reason == "concentration"


def test_correlation_boundaries():
    items = exposures(3)
    s = snapshot(exposures=items, correlations=correlations(items, D("0.7")))
    assert evaluate(proposal(), s).reason == "correlation_cluster"
    assert evaluate(proposal(), replace(s, correlations=correlations(items, D("0.6999")))).allowed
    assert evaluate(proposal(), replace(s, correlations=correlations(items, D(-1)))).allowed
    for val in [D("NaN"), D(2), D(-2)]:
        assert evaluate(proposal(), replace(s, correlations=correlations(items, val))).reason == "invalid_input"


def test_latched_half_size_and_macro_compound():
    assert evaluate(proposal(), snapshot(worst_daily_loss=D("0.015"))).reason == "position_risk"
    assert evaluate(proposal(quantity=75), snapshot(worst_daily_loss=D("0.015"))).allowed
    assert evaluate(proposal(quantity=38), snapshot(worst_daily_loss=D("0.015"),
        macro_multiplier=D("0.5"))).reason == "position_risk"
    assert evaluate(proposal(quantity=37), snapshot(worst_daily_loss=D("0.015"),
        macro_multiplier=D("0.5"))).allowed


@given(excess=st.integers(1, 100000))
def test_property_single_position_risk(excess):
    assert not evaluate(proposal(quantity=150 + D(excess)/1000), snapshot()).allowed


@given(loss=st.decimals(min_value="0.025", max_value=1, allow_nan=False, allow_infinity=False))
def test_property_daily_loss(loss):
    assert not evaluate(proposal(), snapshot(worst_daily_loss=loss)).allowed


@given(count=st.integers(12, 30))
def test_property_position_count(count):
    assert not evaluate(proposal(), snapshot(exposures=exposures(count, 1))).allowed


@given(qty=st.integers(151, 100000))
def test_property_name_concentration(qty):
    assert not evaluate(proposal(quantity=qty, stop=D("99.999")), snapshot()).allowed


@given(qty=st.integers(141, 150))
def test_property_gross_exposure(qty):
    assert not evaluate(proposal(), snapshot(exposures=exposures(10, qty))).allowed


@given(asset=st.sampled_from(["crypto", "option"]), qty=st.integers(1, 100000),
       strategy=st.sampled_from(["6.3", "6.4", "6.5"]))
def test_non_equity_limits_fail_closed(asset, qty, strategy):
    # Crypto allocation/weekend and option risk/earnings limits cannot be
    # bypassed: ALL such proposals are disabled in the phase-one foundation.
    assert not evaluate(proposal(asset=asset, quantity=qty, strategy=strategy), snapshot()).allowed


@given(count=st.integers(3, 20))
def test_property_pdt(count):
    s = snapshot(equity=20_000, start_equity=20_000, peak_equity=20_000, day_trades=count)
    assert not evaluate(proposal(quantity=1), s).allowed


@given(corr=st.decimals(min_value="0.7", max_value=1, allow_nan=False, allow_infinity=False))
def test_property_correlation(corr):
    items = exposures(3)
    assert not evaluate(proposal(), snapshot(exposures=items, correlations=correlations(items, corr))).allowed


@given(qty=st.integers(1, 100000))
def test_property_halted_name(qty):
    assert not evaluate(proposal(halted=True, quantity=qty), snapshot()).allowed
