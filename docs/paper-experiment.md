# Operator-directed momentum paper experiment

This separate experimental service implements the operator's 2026-09-08 revised
mandate to deploy paper cash, targeting $150 cash and reporting whether cash is
at most $250. It does not certify maximum profitability or benchmark outperformance.
The original qualified pullback service remains unchanged and unqualified.

The frozen strategy ranks 15 ETFs by the average of 60- and 120-session price
momentum. It initially spreads allocation over up to 12 names, then assigns
whole-share residuals in rank order. Each new name is at most 14% of equity,
with a 5% broker stop and full-position 10% profit target. There is a 20-session
time exit and five-session cooldown after exits. Negative momentum can be held
to honor the allocation mandate; this is a relative momentum strategy with an
exposure target, not an absolute trend filter.

The experiment explicitly replaces the earlier positive-expectancy deployment
gate, pullback entry filter, 90% sleeve ceiling and correlation-clique rejection.
The old broker daytrade-count field is not used by this experimental worker;
its removal is documented by Alpaca on July 6, 2026. The experiment does not
assert a regulatory interpretation or certify a three-day-trade budget.

Preserved controls: fixed paper endpoint, no leverage or shorting, 12 names,
15% hard name cap, 0.75% planned initial stop risk per name, fresh quotes,
durable intent before POST, no uncertain retries, broker reconciliation, and
independent daily-loss/drawdown watchdog. Stop execution can slip. A 2.5% daily
loss or 10% drawdown still halts and attempts liquidation. Exits can increase
cash above $250. Neither cash exposure nor returns can be guaranteed continuously.

The paper broker deliberately simulates partial fills. The worker blocks further
entries while a parent settles; a partial parent has at most 15 seconds from
durable reservation to fill fully before the watchdog halts and liquidates.
Bracket stops are not yet armed during that brief interval. Fully filled brackets
recognize the broker's held-stop/active-profit-leg state; two held legs still fail.

Run offline research with `python scripts/backtest_momentum.py` after fetching
the fixed-universe CSV inputs. Data are SIP, split-adjusted daily bars; dividends
are omitted. The development and final-year validation windows reset separately
to $2,500. Costs are 1bp per side and slippage 2bp per side. Daily-close risk
checks cannot reproduce intraday watchdog behavior. A risk halt stops further
simulated entries for that window. Reports retain data/config hashes and trades.

The first run returned 25.53% in development (halted May 5, 2022) versus 113.77%
SPY price return over the full development window. Final-year validation returned
11.70% versus SPY 18.15%, with 167 trades and 5.79% maximum closing drawdown.
These results do not establish superiority. Inputs cover 2020 through September
4, 2026. Full machine-readable results stay in local reports/momentum_backtest.json.

Run with environment credentials using
`python -m alphagrid.paper_experiment --root <repository>`.
The one-time `--resume-drill` flag is restricted to the documented completed
execution drill, a reconciled flat account and no previous experimental resume.
It must not be used for later risk failures. Both credentials remain in process
environment only; no credential files are created. The service runs on this
computer and requires it to remain awake and connected. Credentials must be
provided again after a process/machine restart. Local logs and broker state are
the source of execution status, not the existence of a process ID.

To stop safely, use the existing `python -m alphagrid --root <repository> halt`
command and verify liquidation with the independent watchdog. Do not kill the
watchdog while positions or uncertain orders remain. Unexpected cash flows,
including unjournaled ETF dividends, currently trigger reconciliation halt.
