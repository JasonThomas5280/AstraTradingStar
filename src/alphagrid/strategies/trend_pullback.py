"""Frozen daily-bar interpretation of strategy 6.1; never an execution adapter."""
from dataclasses import dataclass
from datetime import date
import math
from typing import Sequence


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    completed: bool = True

    def __post_init__(self):
        if not isinstance(self.date, date):
            raise ValueError("date must be a date")
        for name in ("open", "high", "low", "close", "volume"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0 or (name != "volume" and value == 0):
                raise ValueError("bar prices must be positive and volume nonnegative, all finite")
            object.__setattr__(self, name, value)
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC range")
        if self.completed is not True:
            raise ValueError("only completed daily bars are accepted")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    as_of: date
    entry: float
    stop: float
    target: float
    thesis: str
    invalidation: str
    atr14: float
    average_daily_dollar_volume: float
    max_entry: float


def validate_bars(bars: Sequence[Bar]) -> None:
    if any(not isinstance(b, Bar) or not b.completed for b in bars):
        raise ValueError("only completed Bar instances accepted")
    if any(a.date >= b.date for a, b in zip(bars, bars[1:])):
        raise ValueError("bars must have unique increasing session dates")


def _ema(values, period):
    result = sum(values[:period]) / period
    for value in values[period:]:
        result += 2 / (period + 1) * (value - result)
    return result


def _rsi(values, period=14):
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(x, 0) for x in changes[:period]) / period
    loss = sum(max(-x, 0) for x in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    return 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss)


def _atr(bars, period=14):
    ranges = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
              for a, b in zip(bars, bars[1:])]
    value = sum(ranges[:period]) / period
    for item in ranges[period:]:
        value = (value * (period - 1) + item) / period
    return value


def signal(symbol: str, bars: Sequence[Bar]) -> Candidate | None:
    """Detect close-confirmed setup; entry is valid on the next session only.

    Fixed definitions: 50-session SMA rises versus previous session; preceding
    bearish candle touches EMA20 with lower volume; bullish reversal closes above
    preceding close with increased volume; Wilder RSI14 lies in [40,55].
    Liquidity uses mean close*volume over 20 completed sessions. No fitting.
    """
    validate_bars(bars)
    if not symbol or not symbol.strip():
        raise ValueError("symbol required")
    if len(bars) < 51:
        return None
    close = [b.close for b in bars]
    prior, current = bars[-2:]
    ema_prior = _ema(close[:-1], 20)
    if not (current.close >= 5 and sum(b.close * b.volume for b in bars[-20:]) / 20 >= 50_000_000):
        return None
    if not (sum(close[-50:]) > sum(close[-51:-1]) and current.close > sum(close[-50:]) / 50):
        return None
    if not (prior.close < prior.open and prior.low <= ema_prior <= prior.high
            and prior.volume < bars[-3].volume and current.close > current.open
            and current.close > prior.close and current.volume > prior.volume
            and 40 <= _rsi(close) <= 55):
        return None
    entry = math.ceil((current.high + 0.01) * 100 - 1e-8) / 100
    stop = max(min(prior.low, current.low) - 0.01, entry - 1.5 * _atr(bars))
    if stop <= 0 or stop >= entry:
        return None
    return Candidate(symbol.upper(), current.date, entry, stop, entry + 2 * (entry - stop),
                     "Rising SMA50; EMA20 pullback on declining volume; bullish volume reversal; RSI14 40-55.",
                     "Price reaches protective stop; setup expires after the next trading session.",
                     _atr(bars), sum(b.close * b.volume for b in bars[-20:]) / 20, entry * 1.001)


def trailing_stop(bars: Sequence[Bar], entry: float, current_stop: float) -> float:
    """At profitable close, tighten only; new stop is effective next session."""
    validate_bars(bars)
    if len(bars) < 15 or bars[-1].close <= entry:
        return current_stop
    proposed = max(_ema([b.close for b in bars], 10), bars[-1].close - 2 * _atr(bars))
    return max(current_stop, min(proposed, bars[-1].close - .01))
