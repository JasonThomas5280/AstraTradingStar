# AstraTradingStar / AlphaGrid

A supervised **paper-only** trading service with broker order submission, durable
reconciliation, a separate liquidation watchdog, and reproducible strategy research.
It has no live-money endpoint and makes no profitability guarantee.

## Current deployment status

The code can submit protected long-equity limit brackets after qualification.
Actual autonomous trading has **not been enabled**: the required intraday replay,
positive out-of-sample evidence, and real positioned kill-switch drill are missing.
An allocation request in config does not bypass those checks.

The operator target is **10% cash / 90% invested**, subject to qualified signals,
whole-share sizing, buying power, and hard risk limits. The service reports target
shortfall and retains cash when it cannot find qualifying entries. It never buys
solely to reduce cash.

## Setup

Python 3.11+ (CI uses 3.12):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m alphagrid status
python -m alphagrid check-account
```

Set `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in the launching process environment.
Use rotated paper credentials. No credentials belong in files, Git, reports, or
shell history. Endpoint overrides, redirects, and environment proxies are rejected.

## Operator commands

Run from the repository root, or pass `--root C:\path\to\AstraTradingStar` before
any command:

```powershell
python -m alphagrid initialize       # Dedicated account; must be flat, no open orders
python -m alphagrid observe --once   # Read/reconcile only; never starts the watchdog
python -m alphagrid readiness        # Lists deployment blockers; fails when blocked
python -m alphagrid serve            # Supervised worker + independent watchdog
python -m alphagrid halt             # Latch halt and attempt paper liquidation
python -m alphagrid drill            # Real paper drill; leaves account halted
```

`serve` requires current revision evidence and an initialized, unhalted account.
The worker scans every 5 minutes; the watchdog scans between risk checks and keeps
monitoring after flattening. If the worker fails or you interrupt the supervisor,
the watchdog is left running to monitor/liquidate; terminal output explains this.
Stopping the watchdog while positions remain defeats independent protection.

A halt has no automatic reset. Resolve the cause, write a postmortem, refresh all
qualification evidence, and reconcile state before a reviewed restart. Do not
remove the database or edit the halt latch to restart.

## Frozen strategy and risk

Strategy `6.1_daily_full_2R_trailing_10session_v1` uses completed daily bars: rising
50-day SMA, EMA 20 pullback with falling volume, bullish reversal with rising volume,
RSI 14 between 40 and 55, price at least $5, average daily dollar volume at least $50 M.
Entry requires a fresh quote crossing the signal trigger within its 0.1% entry cap.

This is an explicitly different exit variant from the initial 50% scale-out brief:
the full position receives a 2 R take-profit and stop bracket. Profitable closes can
tighten the stop using EMA 10/2 ATR. A 10-session time exit is requested near session
close (or next available session if overdue). The daily backtest approximates that
exit at the close, so its results are research, not execution certification.

The service requests at most 0.25% risk per entry before a permanent 0.5 macro multiplier
and an additional 0.5 drawdown multiplier after 1.5% daily loss. It preserves the 0.75%
hard risk ceiling,15% name concentration,12 positions,150% gross exposure,2.5% daily
loss halt,10% peak drawdown stop, and four-name correlation-cluster rejection.
PDT entries are conservatively blocked below $25,000 after 3 recorded day trades.
Crypto, options, shorts, other strategy families, and additions remain disabled.

Broker brackets activate exits only after the entry fully fills. Partial entries
therefore trigger a halt/liquidation attempt. Network/socket timeouts and exchange
halts can prevent a timely exit: the code reports verified outcomes and never
claims a guaranteed 60-second flatten. Protective stops cannot eliminate gap losses.

## Research and verification

```powershell
python -m alphagrid download AAPL --start 2025-01-01 --end 2026-09-04T23:59:59Z --output data/AAPL.csv
python -m alphagrid research AAPL data/AAPL.csv
python scripts/research_universe.py --initial-equity 100000
python -m coverage run -m pytest -q
python -m coverage report
python -m coverage json
python scripts/check_coverage.py
```

Research uses chronological data, fixed parameters, costs, capped entries,
conservative same-bar ordering, and an independent final 30-session holdout.
It never grants deployment authorization. Do not optimize on the holdout after
seeing its results; freeze any revised strategy before collecting new evidence.

See [qualification requirements](docs/qualification.md), [plan](state/plan.md), and
[verification status](reports/verification.md). Runtime data and account reports
stay local and are ignored by Git. Qualification artifacts are operator-supplied
proof whose hashes/consistency are checked; the validator does not authenticate
external research or prove a trading edge by itself.

References: [Alpaca orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca),
[historical bars](https://docs.alpaca.markets/us/reference/stockbars),
[authentication](https://docs.alpaca.markets/us/docs/authentication).
