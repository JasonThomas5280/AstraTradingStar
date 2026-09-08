"""Phase-one long-equity proposal checks, NOT an execution authorization.

    No strategies, crypto, options, shorts or additions may execute in this release.
    A future order manager must serialize reconciliation + reservations + submission.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations
from .numbers import number


@dataclass(frozen=True)
class Exposure:
    symbol: str
    quantity: Decimal
    price: Decimal  # Conservative current mark or pending limit, whichever is higher.
    asset: str = "equity"


@dataclass(frozen=True)
class Proposal:
    symbol: str
    quantity: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    thesis: str
    invalidation: str
    asset: str = "equity"
    strategy: str = "6.1"
    side: str = "long"
    average_daily_dollar_volume: Decimal = Decimal(0)
    listed: bool = False
    halted: bool = True


@dataclass(frozen=True)
class Snapshot:
    equity: Decimal
    start_equity: Decimal
    peak_equity: Decimal
    # Latched maximum daily loss; must survive recovery and process restarts.
    worst_daily_loss: Decimal
    exposures: tuple[Exposure, ...] = ()  # Includes ALL pending entry reservations.
    correlations: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    reconciled: bool = False
    healthy: bool = False
    halted: bool = True
    age_seconds: Decimal = Decimal("Infinity")
    day_trades: int = 0
    macro_multiplier: Decimal = Decimal("0.5")
    cooldown_clear: bool = False
    journal_verified: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

    def __bool__(self):
        raise TypeError("inspect Decision.allowed explicitly")


def evaluate(proposal: Proposal, state: Snapshot) -> Decision:
    """Fail closed on malformed/unverified inputs. No implicit truthy decisions."""
    try:
        return _evaluate(proposal, state)
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        return Decision(False, "invalid_input")


def _evaluate(p, s):
    equity, start, peak = (number(x, positive=True) for x in
                            (s.equity, s.start_equity, s.peak_equity))
    loss, age = number(s.worst_daily_loss), number(s.age_seconds)
    if peak < max(start, equity) or loss < max(Decimal(0), (start - equity) / start):
        return Decision(False, "inconsistent_equity_history")
    if s.reconciled is not True or s.healthy is not True or s.halted is not False:
        return Decision(False, "unverified_or_halted")
    if age > 30 or s.cooldown_clear is not True or s.journal_verified is not True:
        return Decision(False, "stale_or_missing_evidence")
    if loss >= Decimal("0.025"):
        return Decision(False, "daily_loss")
    if equity <= peak * Decimal("0.90"):
        return Decision(False, "drawdown")
    if p.asset != "equity" or p.strategy != "6.1" or p.side != "long":
        return Decision(False, "phase_one_only")
    if not p.symbol or not p.symbol.isascii() or not p.thesis.strip() or not p.invalidation.strip():
        return Decision(False, "missing_thesis")
    qty, entry, stop, target = (number(x, positive=True) for x in
                                (p.quantity, p.entry, p.stop, p.target))
    if not stop < entry < target:
        return Decision(False, "invalid_exit")
    if p.listed is not True or p.halted is not False or entry < 5 or number(
            p.average_daily_dollar_volume) < 50_000_000:
        return Decision(False, "universe_or_halt")
    if type(s.day_trades) is not int or s.day_trades < 0:
        raise ValueError("invalid day-trade count")
    if equity < 25_000 and s.day_trades >= 3:
        return Decision(False, "pdt_conservative_entry_block")
    scale = number(s.macro_multiplier, positive=True)
    if scale not in (Decimal("0.5"), Decimal(1)):
        raise ValueError("invalid macro multiplier")
    if loss >= Decimal("0.015"):
        scale *= Decimal("0.5")
    if (entry - stop) * qty > equity * Decimal("0.0075") * scale:
        return Decision(False, "position_risk")

    names = {}
    for item in s.exposures:
        if item.asset != "equity":
            return Decision(False, "unsupported_existing_exposure")
        if not item.symbol or not item.symbol.isascii():
            raise ValueError("invalid symbol")
        value = number(item.quantity, positive=True) * number(item.price, positive=True)
        names[item.symbol] = names.get(item.symbol, Decimal(0)) + value
    if p.symbol in names:
        return Decision(False, "additions_disabled")
    names[p.symbol] = entry * qty
    if len(names) > 12:
        return Decision(False, "position_count")
    if any(value > equity * Decimal("0.15") for value in names.values()):
        return Decision(False, "concentration")
    if sum(names.values()) > equity * Decimal("1.5"):
        return Decision(False, "gross_exposure")
    # Phase one consists solely of strategy 6.1: stricter 40% sleeve cap.
    if sum(names.values()) > equity * Decimal("0.40"):
        return Decision(False, "strategy_sleeve")
    edges = set()
    for a, b in combinations(sorted(names), 2):
        value = s.correlations.get((a, b))
        if value is None:
            return Decision(False, "missing_60_day_correlation")
        value = Decimal(str(value))
        if not value.is_finite() or abs(value) > 1:
            raise ValueError("invalid correlation")
        if value >= Decimal("0.7"):
            edges.add((a, b))
    if any(all(pair in edges for pair in combinations(group, 2))
           for group in combinations(sorted(names), 4)):
        return Decision(False, "correlation_cluster")
    return Decision(True, "proposal_within_limits_execution_disabled")
