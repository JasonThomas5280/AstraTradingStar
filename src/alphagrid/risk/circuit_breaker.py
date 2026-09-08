"""Durable halt latch and bounded flatten controller; broker adapter not wired.

The broker interface MUST bound each call by its supplied remaining timeout.
It must count partial fills as positions and pending cancels as open orders.
This controller never treats an acknowledgement as a verified flat account.
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

    def _persist(self, reason, flat):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump({"halted": True, "reason": reason, "flat_verified": flat,
                       "postmortem_required": True}, handle)
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
        self._persist(reason, False)  # Halt survives exceptions and restarts.
        deadline = clock() + budget
        while clock() < deadline:
            try:
                broker.cancel_all(timeout=max(0.001, deadline - clock()))
                if clock() >= deadline:
                    break
                if broker.open_orders(timeout=max(0.001, deadline - clock())) != []:
                    sleep(min(1, max(0, deadline - clock())))
                    continue
                if clock() >= deadline:
                    break
                # Adapter must reconcile existing exit orders before resubmitting.
                broker.close_all(timeout=max(0.001, deadline - clock()))
                if clock() >= deadline:
                    break
                positions = broker.positions(timeout=max(0.001, deadline - clock()))
                if clock() >= deadline:
                    break
                orders = broker.open_orders(timeout=max(0.001, deadline - clock()))
                if positions == [] and orders == [] and clock() < deadline:
                    self._persist(reason, True)
                    return True
            except Exception:
                # No raw exception strings: they can contain headers or secrets.
                pass
            sleep(min(1, max(0, deadline - clock())))
        return False  # Remains latched; operator action and postmortem required.
