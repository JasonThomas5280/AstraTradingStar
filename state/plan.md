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

#### Operator-directed execution validation — 2026-09-08
The operator explicitly repeated the instruction to execute paper trades. Perform
one small manual paper execution-validation trade in a liquid listed equity,
limited to one share, a broker-hosted stop, and immediate exit after fill verification.
This one-off broker drill does not enable the unqualified autonomous strategy or
claim an investment edge. Log thesis, stop, target and broker-confirmed outcomes.
Require a flat reconciled account, open market, fresh quote, liquidity >=$50M/day,
all monetary risk limits, and conservative day-trade history verification first.
Use the independent watchdog for flattening; remain halted afterward. No retry of
an uncertain submission. Expected impact: a small execution cost, not forecast alpha.

#### P 3 revision: Explicit paper allocation experiment, 2026-09-08
The operator now explicitly prioritizes paper execution and <=$250 uninvested cash
and permits strategy replacement. Freeze diversified_momentum_paper_v1 before
research: rank a fixed 15-ETF universe by average 60/120-session price momentum;
buy whole shares in up to 12 names, <=14% target weight each, cash target $150,
5% broker stop, full 2R target, 20-session time exit, five-session exit cooldown.
Use actual historical bars, prior-session signals and modeled costs. Report all
results, including losses and benchmark underperformance; no profitability gate
or maximum-profit certificate is claimed for this operator-directed experiment.
Replaces pullback qualification, 90% sleeve and correlation-clique restrictions
only for the separate paper experiment. Keep cash-only, <=15% hard name limit,
<=0.75% initial stop risk, <=12 names, independent 2.5% daily-loss/10% drawdown
watchdog, fresh quotes, durable intent and reconciliation. Missing protection,
unknown fills or risk breach still halt. Resolve the completed drill halt only
after checking its postmortem and reconciling the flat account. Run tests before
paper submission. No secret persistence. No claim of live-money authorization.

#### P 3 revision: Intraday stock momentum and daily return objective
Operator sets a 10% daily portfolio-return research target, acknowledging not
every day will reach it. Add a stock-mover scanner and frozen five-minute
continuation strategy: regular-session listed common shares, >=10% gain versus
prior close, >=$1 price, >=1M session shares, above session VWAP, close beyond
the prior three-bar high with >=1.5x preceding six-bar median volume. Require
fresh completed bars and <=0.5% quoted spread. Stop at prior three-bar low,
0.5%-5% entry distance, target 3R, exit after45 minutes or15:50 ET. Size within
existing cash and monetary risk limits. Measure actual daily portfolio return,
10% hit rate, expectancy after costs, drawdown, turnover and benchmark return.
Record prospective scanner snapshots for later unbiased universe replay; label
retrospective tests of today's movers as selection-biased diagnostics. Never
present today's percentage gain as an achievable entry return. Keep current
positions protected while building and verifying the intraday strategy.

#### Available-feed revision
Use an explicitly separate IEX/news observation variant with live IEX five-minute
bars, IEX VWAP and relative volume, a provisional10,000-share IEX session floor,
and the same breakout/stop rules. This floor is not a validated conversion from
consolidated volume. Attach timestamped recent Alpaca news without treating a
headline as verified positive alpha. Keep the previous SIP diagnostic distinct;
its results do not validate this variant. Restart only the read-only scanner.

#### Explicit intraday execution authorization
The operator now requests "turn on intraday trading." Implement and deploy the
single mixed paper worker described in docs/intraday-execution.md, replacing
the ETF-only worker and preserving its independent watchdog. This supersedes
the earlier observation-only deployment restriction for this paper strategy.
Validate actual fresh signals before entries, permit one ETF rotation when a
qualified signal needs capacity, retain all monetary and bracket protections.

#### Execution discipline revision
Operator requests adjustment toward professional day trading. Apply observed
relative-volume prioritization, 1% completed-bar participation, spread <=20% of
initial stop risk, and a 20-minute stall exit when sampled peak never reached
0.5R and current price is not profitable. Retain paper-only execution and existing
loss controls. Frozen-universe comparison must be reported without a profitability
claim; this is experimental management refinement, not a certified trading edge.
