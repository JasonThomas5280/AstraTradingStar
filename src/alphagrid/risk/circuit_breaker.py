"""Durable halt latch and phased flatten controller.

Timeouts and monotonic deadline checks are best effort: synchronous socket
operations can overrun their supplied timeout. Late responses never prove a
timely flatten. Partial fills remain positions and pending cancels remain orders.
Acknowledgements never substitute for a verified flat account.
"""
import json
import os
import time
from pathlib import Path
from .numbers import number


def breach_reason(equity, start_equity, peak_equity, worst_daily_loss):
    try:
        equity = number(equity)
        start, peak = (number(x, positive=True) for x in (start_equity, peak_equity))
        loss = number(worst_daily_loss)
        if peak < max(start, equity):
            return "inconsistent_equity_history"
        if equity <= start * number("0.975") or loss >= number("0.025"):
            return "daily_loss"
        if equity <= peak * number("0.90"):
            return "drawdown"
        return None
    except (ValueError, ArithmeticError):
        return "invalid_equity"


class CircuitBreaker:
    def __init__(self, path):
        self.path = Path(path)

    def is_halted(self):
        # A missing/corrupt file is uninitialized, never permission to trade.
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("halted") is not False
        except (OSError, ValueError, AttributeError):
            return True

    def _persist(self, reason, flat, *, closing_started=False, liquidation_ids=None,
                 uncertain_close=False, exit_sides=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump({"halted": True, "reason": reason, "flat_verified": flat,
                       "postmortem_required": True, "closing_started": closing_started,
                       "liquidation_ids": liquidation_ids or [], "uncertain_close": uncertain_close,
                       "exit_sides": exit_sides or {}}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    def trip(self, broker, *, reason="manual_drill", clock=time.monotonic,
             sleep=time.sleep, budget=60):
        # Fixed allowlist keeps arbitrary broker errors/news out of persisted state.
        if reason not in {"manual_drill", "daily_loss", "drawdown", "invalid_equity",
                          "reconciliation", "inconsistent_equity_history"}:
            raise ValueError("unknown halt reason")
        budget = float(number(budget, positive=True))
        if budget > 60:
            raise ValueError("flatten deadline exceeds policy")
        try:
            previous = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError):
            previous = {}
        closing = previous.get("closing_started") is True and previous.get("flat_verified") is not True
        uncertain = previous.get("uncertain_close") is True
        liquidation_ids = previous.get("liquidation_ids", []) if closing else []
        exit_sides = previous.get("exit_sides", {}) if closing else {}
        def persist(flat=False):
            self._persist(reason, flat, closing_started=closing,
                          liquidation_ids=liquidation_ids, uncertain_close=uncertain,
                          exit_sides=exit_sides)
        persist()  # The phase/intent survives exceptions and process restarts.
        deadline = clock() + budget
        canceled = closing
        def timeout():
            if clock() >= deadline:
                raise TimeoutError("flatten_deadline")
            return min(10, deadline - clock())
        while clock() < deadline:
            try:
                if not canceled:
                    broker.cancel_all(timeout=timeout())
                    canceled = True
                if not closing:
                    orders = broker.open_orders(timeout=timeout())
                    if orders != []:
                        canceled = False
                        sleep(min(1, max(0, deadline - clock())))
                        continue
                    positions = broker.positions(timeout=timeout())
                    if not isinstance(positions, list):
                        raise ValueError("unknown_positions")
                    exit_sides = {p["symbol"]: "sell" if p["side"] == "long" else "buy"
                                  for p in positions}
                    closing, uncertain = True, True
                    persist()  # Durable close intent BEFORE any liquidation mutation.
                    result = broker.close_all(timeout=timeout())
                    liquidation_ids = [o["id"] for o in result or [] if isinstance(o, dict) and o.get("id")]
                    uncertain = False
                    persist()
                positions = broker.positions(timeout=timeout())
                orders = broker.open_orders(timeout=timeout())
                if positions == [] and orders == [] and clock() < deadline:
                    persist(True)
                    return True
                # After liquidation starts, NEVER blanket-cancel pending exits.
                # Unknown market exits can be an accepted-but-unacknowledged close;
                # preserve only the expected closing direction for its symbol.
                if isinstance(orders, list):
                    for order in orders:
                        own = order.get("id") in liquidation_ids
                        unresolved_exit = (uncertain and order.get("type") == "market"
                            and order.get("order_class") in (None, "", "simple")
                            and order.get("symbol") in exit_sides
                            and order.get("side") == exit_sides[order["symbol"]])
                        if not own and not unresolved_exit:
                            broker.cancel(order["id"], timeout=timeout())
            except Exception:
                # No raw exception strings; unknown close results are never retried.
                pass
            sleep(min(1, max(0, deadline - clock())))
        return False  # Halt and unresolved close intent remain durable.
