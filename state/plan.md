# AlphaGrid implementation plan

## Proposed

### P 1: Paper-only risk foundation (2026-09-08)
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

### P 2: Autonomous paper execution and frozen strategy research (2026-09-08)
What: Implement an authenticated paper-only broker adapter, durable serialized order
intents and reconciliation, supervised trading worker plus independent watchdog,
strategy 6.1 signal generation, chronological research, operator CLI, and failure tests.
Why: Turn the foundation into executable service code while rejecting uncertain
fills, missing validation, stale data, external holdings and duplicate entry attempts.
Expected impact: Measurable paper expectancy after validation; no guaranteed profit.
Use the frozen pullback rules and a conservative 0.25% entry risk / 10% sleeve for
initial qualification. Optimize only from new out-of-sample evidence, not promises.
Measure: Unit/property/chaos CI passes, no duplicate POST on uncertain response,
restart reconstruction, independent kill behavior, historical holdout metrics.
Rollback: Disable new entries and latch halt on reconciliation, watchdog, or risk failure.
Historical daily-bar research is not a substitute for intraday replay or a real
broker flatten drill. Actual service startup requires environment credentials and
verified deployment evidence. Live money and strategies 6.2-6.7 remain unauthorized.

### P 3: Operator allocation target (2026-09-08, market open)
What: Record operator target of no more than 10% cash as 90% invested target, replacing
the initial 10% service sleeve and original 40%6.1 sleeve with a 90% maximum sleeve.
Why: Explicit operator request to put qualified paper capital to work.
Expected impact: Higher exposure and potential drawdown; profitability remains unknown.
Measure: Report actual cash fraction and target shortfall. Do not force entries when
signals, available buying power, concentration, correlation or qualification reject them.
All section 3 hard limits remain enforced, including 0.75% risk,15% name,12 positions,
150% gross,2.5% daily loss. Initial per-entry risk stays 0.25% before conservative scaling.
Rollback: Halt on risk/reconciliation faults; retain cash when qualified demand is absent.
