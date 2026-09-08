# AlphaGrid implementation plan

## Proposed

### P1: Paper-only risk foundation (2026-09-08)
What: Implement position sizing, immutable/tightening stops, pre-trade portfolio checks,
a persistent circuit breaker, paper-only read-only broker diagnostics, and CI tests.
Why: Prevent discretionary or malformed requests from bypassing the master risk rules.
Expected impact: No P&L until deployment evidence exists; reject uncertain risk inputs.
Measure: >=95% coverage in each of the four risk modules; boundary, property, and chaos tests.
Rollback: Keep trading disabled on any failed check, stale/missing state, or reconciliation failure.
Deployment remains blocked until all section 9 checks have verifiable evidence. No strategy code yet.

## Live
None. No broker connection or trades established.

## Reverted
None.

## 30/60/90 day roadmap
- Days 1-30: Complete risk foundation, source data and execution reconciliation, run
  90-day replay and 30-day out-of-sample validation; establish independent watchdog.
  Only then enable 6.1 at a small sleeve. Observe five clean trading days with
  positive/flat expectancy and zero limit touches before considering 6.2-6.7.
- Days 31-60: Accumulate auditable paper results, attribution, daily/weekly reporting,
  weekly kill-switch drills, and strategy-specific expectancy. Disable negative expectancy.
- Days 61-90: Evaluate rolling 60-day Sharpe >=1.5, 30-day positive expectancy,
  max drawdown <=10%, >=60 documented closed trades, and replay/P&L agreement within 5%.
  These are acceptance targets, not forecasts. Live capital is not authorized.

## Outstanding deployment gates
- Rotated paper credentials supplied through environment variables.
- Account/positions/open-order reconciliation and durable start-of-day/peak equity.
- CI passing on the exact deployed revision.
- 90-day replay with no hard-limit breaches using actual data.
- 30-day out-of-sample positive expectancy.
- Independent paper-account flatten verification within 60 seconds.
- Execution lifecycle, news/halt/calendar/correlation adapters, and monitoring.
