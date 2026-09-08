# AstraTradingStar / AlphaGrid

Paper-only risk foundation. **Trading is disabled.** This repository does not
submit orders, run an autonomous trading service, or implement a profitable strategy.

The initial implementation checks long-equity proposals for strategy 6.1. It includes
decimal-based sizing, tightening-only stops, portfolio exposure and correlation checks,
a persistent halt controller with a broker interface, and read-only Alpaca diagnostics.
The circuit breaker is tested with fault-injection brokers; a real watchdog adapter
and real flatten drill are still required. Config files describe deployment intent;
they cannot enable execution in this release.

## Install and verify

Python 3.11 or later (CI uses 3.12):

```sh
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[test]"
python -m coverage run -m pytest -q
python -m coverage report
python -m coverage json
python scripts/check_coverage.py
python -m alphagrid
```

Credentials belong only in the process environment: `APCA_API_KEY_ID` and
`APCA_API_SECRET_KEY`. Rotate credentials that were exposed in chat. Do not commit
them, put them in reports, or paste them into command-line arguments. The diagnostic
prints presence only. After setting replacement credentials locally:

```sh
python -m alphagrid --check-account
```

This reads the paper account, positions and open-order count. It makes no orders
and does not establish reconciliation. Network errors fail closed. Endpoint changes,
redirects and environment proxies are rejected.

## Risk behavior and limits

- 2.5% daily-loss latch; sizing halves after a recorded 1.5% daily loss even if equity recovers.
- Refuse new entries at 10% peak drawdown (the stricter success constraint).
- 0.75% proposed position risk; 15% name concentration; 150% gross exposure;
  at most 12 underlying positions; 40% total phase-one sleeve.
- Existing holdings AND all pending entry reservations must enter the snapshot.
  Additions to an existing name are disabled. Pending cancels remain reservations.
- Block four-name cliques with pairwise correlation >=0.7; absent correlation blocks entry.
- Conservative PDT entry block at three day trades for equity below $25,000.
- Only listed, unhalted equities priced >=$5 with >=$50M average daily dollar volume.
- Crypto, options, short sales, and strategies 6.2-6.7 are entirely disabled. Their
  allocation, weekend and earnings requirements must be implemented before enablement.
- Gate decisions concern proposals only. There is no order manager, reservation lock,
  quote/calendar validation adapter, deployment authorization, or trading scheduler yet.
- The halt controller remains halted after a confirmed flat book. No automatic reset
  exists; a postmortem and verified next-session restart procedure must precede deployment.

A stop-based risk calculation cannot bound actual losses through market gaps,
slippage, exchange halts or unavailable broker APIs. The circuit breaker only reports
success after observing empty positions and orders within its deadline; an unresponsive
broker can prevent flattening. Call timeouts must be honored by its future adapter.

## Deployment blockers

See [state/plan.md](state/plan.md), [verification](reports/verification.md), and
[handoff](state/handoff.md). Unit/property/chaos tests do not substitute for the
required 90-day historical replay, 30-day walk-forward, five clean paper days,
or broker kill-switch drill. No historical performance or fills are fabricated.

References: [Alpaca authentication](https://docs.alpaca.markets/us/docs/authentication),
[paper account endpoint](https://docs.alpaca.markets/us/reference/getaccount-1).
