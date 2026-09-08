# Operator-authorized intraday paper execution

The operator explicitly requested intraday trading be enabled. The mixed worker
`python -m alphagrid.intraday_execution --root <repository>` replaces the ETF-only
worker while adopting the already-running independent watchdog. It uses the same
supervisor/worker/execution locks and durable ledger. Do not run both workers.
Startup requires a fresh watchdog heartbeat and exact paper authorization.

The scanner stays a read-only producer. The worker reads a recent IEX scan, then
independently rechecks the asset, session, bars, daily close and quote before an
order. It retains the 10,000-share IEX volume floor, five-minute continuation,
90-second post-bar window and 0.5% spread limit. News does not drive entries.
One entry per symbol per date, at most three intraday names and 12 total names.
Initial name exposure is capped at 14%, planned stop risk at 0.75%, with whole
shares and $100 reserved cash. No margin or live endpoint is used.

When all 12 slots are occupied, a currently qualifying signal can cause one
smallest ETF position to close through the existing journaled exit path. A later
cycle must reconcile that sale and validate the signal again before buying.
Cash can temporarily exceed $250 if the signal disappears; cash targets never
justify entering on expired data. New ETF allocation is paused in mixed mode;
existing ETF protection and 20-session exits remain managed.

Stock entries use broker brackets with a 3R target. Unfilled parents expire via
the existing 60-second cancellation policy; intraday positions exit after 45
minutes or at/after 15:50 ET. No new stock entries at/after 15:00 ET. These are
polling-based time exits, not guaranteed exact execution timestamps. Any worker
exception latches a halt for the existing watchdog to handle. Local computer,
network and watchdog availability remain necessary.

Read reports/intraday_execution.json and ledger intraday_execution_report for
execution status. Scanner execution_enabled reflects a recent worker report;
orders_submitted in a scanner report counts scanner mutations (always zero),
not worker orders. Orders and fills must be verified from the ledger/broker.

This is explicitly experimental paper deployment. The prior selection-biased IEX
diagnostic had zero trades across 41 sessions and does not establish an edge or
support a claim of 10% daily returns. Enabling execution is not such evidence.

## Execution policy v2

Qualified candidates are prioritized by signal-bar volume divided by the median
of the preceding six bars, rather than by their already-realized daily gain.
Quantity is capped at 1% of completed signal-bar IEX volume, in addition to the
existing cash, position and risk limits. Spread must pass both the existing 0.5%
price limit and a 20% initial per-share stop-risk limit.

After 20 minutes, a position at/below entry exits if its sampled price has never
reached +0.5R. R means actual entry minus the original stop. The sampled peak is
durable across restarts; it is not a tick-complete maximum. Existing hard stops,
3R target and 45-minute/end-of-day exits remain. This may exit some trades that
would subsequently recover; no claim of superior expectancy is established.

A frozen 12-symbol, 41-session IEX comparison returned zero trades for v1 and
two trades / -0.01885% for v2 after modeled costs. Both are selection-biased and
neither achieved a 10% day. Historical spreads are unavailable and the replay
uses bar highs instead of sampled marks for stall management. This diagnostic
does not validate all live execution behavior or establish a profitable edge.

Reproduce with scripts/research_intraday.py --feed iex --policy v1 and --policy v2
--cached, supplying the same --universe-report for both. Reports are written to
reports/intraday_backtest_iex_v1.json and reports/intraday_backtest_iex_v2.json.
