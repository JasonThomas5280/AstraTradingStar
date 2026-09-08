from decimal import Decimal, ROUND_FLOOR
from .numbers import number


def size_long(equity, entry, stop, *, risk_fraction="0.0075",
              multiplier=1, notional_fraction="0.15", step=1):
    """Floor units to BOTH risk and notional budgets; never round up.

    This is a proposal only. The portfolio gate must check the final request.
    Budget excludes gap/slippage losses, which no stop can guarantee against.
    """
    equity, entry, stop = (number(x, positive=True) for x in (equity, entry, stop))
    risk, scale, cap, step = (number(x, positive=True) for x in
                              (risk_fraction, multiplier, notional_fraction, step))
    if stop >= entry or risk > Decimal("0.0075") or scale > 1 or cap > Decimal("0.15"):
        raise ValueError("sizing exceeds policy")
    units = min(equity * risk * scale / (entry - stop), equity * cap / entry)
    return (units / step).to_integral_value(rounding=ROUND_FLOOR) * step
