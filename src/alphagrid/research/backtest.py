"""Chronological daily research with frozen parameters and explicit limitations."""
import csv
import hashlib
import json
from datetime import date
import math
from pathlib import Path
from alphagrid.strategies.trend_pullback import Bar, signal, validate_bars, trailing_stop


def load_csv(path: str | Path) -> list[Bar]:
    """Read user-supplied actual, consistently adjusted daily OHLCV history."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [Bar(date.fromisoformat(row["date"]), *(float(row[k]) for k in
                ("open", "high", "low", "close", "volume"))) for row in csv.DictReader(handle)]


def _window(symbol, bars, start, end, equity, cost, slip):
    initial = equity
    cash = equity
    position = None
    trades = []
    peak = equity
    drawdown = 0.0
    for i in range(start, end):
        bar = bars[i]
        # Signal is evaluated only on history ending at yesterday's close.
        candidate = signal(symbol, bars[:i]) if position is None else None
        if candidate and bar.high > candidate.entry and bar.open <= candidate.max_entry:
            entry = max(bar.open, candidate.entry) * (1 + slip)
            # Risk at most 0.25% initial capital; 10% allocation; no leverage.
            quantity = max(0, math.floor(min(initial * .0025 / (entry - candidate.stop),
                                            cash * .1 / (entry * (1 + cost))))) if entry <= candidate.max_entry else 0
            if quantity:
                cash -= quantity * entry * (1 + cost)
                position = (quantity, entry, candidate.stop, candidate.target, bar.date, i)
        if position:
            quantity, entry, stop, target, entry_date, entry_index = position
            reason = None
            # On entry bars, the low may precede entry. Counting it anyway is
            # conservative. If both levels touch, stop always wins.
            if bar.low <= stop:
                exit_price = min(bar.open, stop) * (1 - slip)
                reason = "stop"
            elif bar.high > target:
                exit_price = target * (1 - slip)
                reason = "target"
            elif i == end - 1 or i-entry_index >= 9:
                exit_price = bar.close * (1 - slip)
                reason = "window_end" if i == end-1 else "time_stop"
            if reason:
                proceeds = quantity * exit_price * (1 - cost)
                pnl = proceeds - quantity * entry * (1 + cost)
                cash += proceeds
                trades.append({"entry_date": entry_date.isoformat(), "exit_date": bar.date.isoformat(),
                               "entry": entry, "exit": exit_price, "quantity": quantity,
                               "pnl": pnl, "reason": reason})
                position = None
            else:
                position = (quantity, entry, trailing_stop(bars[:i+1], entry, stop),
                            target, entry_date, entry_index)
        equity = cash + (position[0] * bar.close if position else 0)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    benchmark = bars[end-1].close * (1-slip) * (1-cost) / (bars[start].open * (1+slip) * (1+cost)) - 1
    return {"sessions": end-start, "start": bars[start].date.isoformat(),
            "end": bars[end-1].date.isoformat(), "initial_equity": initial, "ending_equity": equity,
            "return": equity/initial-1, "benchmark_buy_hold_return": benchmark,
            "max_close_drawdown": drawdown, "trade_count": len(trades),
            "expectancy_dollars": sum(t["pnl"] for t in trades)/len(trades) if trades else None,
            "win_rate": sum(t["pnl"] > 0 for t in trades)/len(trades) if trades else None,
            "trades": trades}


def run_backtest(symbol, bars, *, initial_equity=100000, cost_bps=1, slippage_bps=2):
    """Replay >=90 sessions and separately reset a final 30-session holdout.

    Last 30 sessions are never used for parameter selection. Holdout starts flat;
    older history supplies indicator warmup only. All parameters remain frozen.
    """
    validate_bars(bars)
    if len(bars) < 141:
        raise ValueError("need >=141 sessions: 51 warmup and >=90 replay including 30 holdout")
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("positive finite initial equity required")
    if any(not math.isfinite(x) or x < 0 or x >= 10000 for x in (cost_bps, slippage_bps)):
        raise ValueError("cost/slippage basis points must be finite in [0,10000)")
    args = (initial_equity, cost_bps/10000, slippage_bps/10000)
    split = len(bars)-30
    return {"strategy": "6.1_daily_full_2R_trailing_10session_v1", "symbol": symbol.upper(),
            "input_sha256": hashlib.sha256(json.dumps([
                [b.date.isoformat(), b.open, b.high, b.low, b.close, b.volume] for b in bars],
                separators=(",", ":")).encode()).hexdigest(),
            "deployment_authorized": False, "data_provenance": "user-supplied; authenticity not independently verified",
            "cost_bps_per_side": cost_bps, "slippage_bps_per_side": slippage_bps,
            "development": _window(symbol, bars, 51, split, *args),
            "holdout": _window(symbol, bars, split, len(bars), *args),
            "replay": _window(symbol, bars, 51, len(bars), *args),
            "limitations": ["Daily OHLC cannot verify intraday portfolio hard limits or execution sequencing.",
                            "No deployment certificate; no claim that data are authentic or results will persist.",
                            "Single symbol; no portfolio correlation, partial fills, halts, spread or market impact model.",
                            "Time exits and window liquidation approximate close fills plus slippage; actual fills may differ.",
                            "No parameter optimization; input history must consistently handle corporate actions.",
                            "Entry valid next session only, capped at trigger+0.1%; strict price penetration models fills but does not prove them.",
                            "Variant: full position at2R; profitable-close EMA10/2ATR tightening effective next session;10session time exit.",
                            "Stop-first on ambiguous bars; window-end liquidation; no same-session re-entry."]}
