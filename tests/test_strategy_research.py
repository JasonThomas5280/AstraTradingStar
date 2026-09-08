from datetime import date, timedelta
import pytest
from alphagrid.strategies.trend_pullback import Bar, Candidate, signal, trailing_stop
from alphagrid.research import backtest


def bars(count=141):
    return [Bar(date(2020, 1, 1)+timedelta(days=i), 100, 101, 99, 100, 1_000_000) for i in range(count)]


def candidate(bar):
    return Candidate("SPY", bar.date, 100, 98, 104, "test", "stop", 2, 100_000_000, 100.1)


@pytest.mark.parametrize("kwargs", [{"close": float("nan")}, {"volume": -1}, {"low": 102}, {"completed": False}])
def test_bad_bar_rejected(kwargs):
    values = dict(date=date(2020,1,1), open=100, high=101, low=99, close=100, volume=100)
    values.update(kwargs)
    with pytest.raises(ValueError):
        Bar(**values)


def test_chronology_and_min_history():
    history = bars()
    with pytest.raises(ValueError):
        signal("SPY", history[::-1])
    with pytest.raises(ValueError):
        backtest.run_backtest("SPY", history[:140])
    assert signal("SPY", history[:50]) is None
    assert signal("SPY", history) is None  # flat SMA and no reversal


def test_no_lookahead_and_holdout_reset(monkeypatch):
    history = bars()
    seen = []
    def inspect(symbol, prior):
        seen.append(len(prior))
        assert prior == history[:len(prior)]
        return None
    monkeypatch.setattr(backtest, "signal", inspect)
    result = backtest.run_backtest("SPY", history)
    assert result["replay"]["sessions"] == 90
    assert result["holdout"]["sessions"] == 30
    assert result["holdout"]["initial_equity"] == 100000
    assert max(seen) == 140
    assert result["deployment_authorized"] is False
    assert result["holdout"]["expectancy_dollars"] is None


def test_stop_first_and_transaction_costs(monkeypatch):
    history = bars()
    history[51] = Bar(history[51].date, 100, 105, 97, 101, 1_000_000)
    monkeypatch.setattr(backtest, "signal", lambda s, p: candidate(p[-1]) if len(p)==51 else None)
    result = backtest.run_backtest("SPY", history)
    trade = result["replay"]["trades"][0]
    assert trade["reason"] == "stop"
    assert trade["exit"] < 98
    assert trade["entry"] > 100
    assert trade["pnl"] < (98-100)*trade["quantity"]


def test_gap_stop_and_no_entry_above_limit(monkeypatch):
    history = bars()
    history[52] = Bar(history[52].date, 95, 96, 94, 95, 1_000_000)
    monkeypatch.setattr(backtest, "signal", lambda s, p: candidate(p[-1]) if len(p)==51 else None)
    result = backtest.run_backtest("SPY", history)
    assert result["replay"]["trades"][0]["exit"] < 95
    history[51] = Bar(history[51].date, 102, 105, 101, 102, 1_000_000)
    assert backtest.run_backtest("SPY", history)["replay"]["trade_count"] == 0


def test_touch_does_not_fill_and_ten_session_exit(monkeypatch):
    history = bars()
    monkeypatch.setattr(backtest, "signal", lambda s, p: candidate(p[-1]) if len(p)==51 else None)
    result = backtest.run_backtest("SPY", history)
    assert result["replay"]["trades"][0]["reason"] == "time_stop"
    assert result["replay"]["trades"][0]["exit_date"] == history[60].date.isoformat()
    history[51] = Bar(history[51].date, 99, 100, 99, 100, 1_000_000)
    assert backtest.run_backtest("SPY", history)["replay"]["trade_count"] == 0


def test_trailing_stop_never_loosens():
    history = bars(60)
    assert trailing_stop(history, 101, 98) == 98
    assert trailing_stop(history, 90, 99.99) == 99.99
    assert trailing_stop(history, 90, 95) > 95


def test_real_indicators_confirm_setup_and_liquidity_gate():
    from dataclasses import replace
    from alphagrid.strategies.trend_pullback import _ema
    prices = [90+18*i/39 for i in range(40)] + [108-4*i/10 for i in range(1,11)] + [104.2]
    history = [Bar(date(2020,1,1)+timedelta(days=i), c+.1, c+1, c-1, c, 1_000_000)
               for i, c in enumerate(prices)]
    ema = _ema(prices[:-1],20)
    history[-2] = Bar(history[-2].date,104.2,max(105,ema),min(103,ema),104,800_000)
    history[-1] = Bar(history[-1].date,104.1,105.2,103.2,104.2,1_100_000)
    result = signal("spy",history)
    assert result is not None
    assert result.entry > history[-1].high
    assert result.target-result.entry == pytest.approx(2*(result.entry-result.stop))
    assert result.symbol == "SPY"
    assert signal("SPY",[replace(b, volume=b.volume/10) for b in history]) is None
    assert signal("SPY",history[:-1]+[replace(history[-1],volume=700_000)]) is None
