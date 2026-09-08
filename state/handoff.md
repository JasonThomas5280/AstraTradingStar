# AlphaGrid handoff — 2026-09-08

Branch: codex/autonomous-paper-service. Paper execution, supervised worker,
independent watchdog, ledger/reconciliation, strategy research, and tests implemented.

Actual account authentication succeeded; balances are kept in local runtime state.
At verification there were no positions or open orders. Flat initialization and a
read-only reconciliation cycle succeeded. No trades placed by this task.
Credentials are never stored in the repository. Use rotated environment credentials.

Requested target:10% cash /90% invested, constrained by risk and qualified signals.
Historical research:342 actual daily bars for each of 8 symbols, zero trades in the
30-session holdout at tested sizing. No profitable edge established. Strategies
6.2–6.7 remain disabled. The full 2 R exit is a separately documented 6.1 variant.

Deployment blockers: current CI/evidence, actual 90-day intraday replay, positive
30-day OOS, real positioned paper flatten drill. Qualification manifest is absent.
No autonomous trading service was started. Do not infer readiness from passing tests.

Next: inspect CI, freeze revisions before unseen validation, complete genuine
qualification artifacts, then use initialize/readiness/serve per README. The
operator's cash target alone is insufficient to approve an entry.

## Execution drill update — 2026-09-08
One operator-directed paper entry and immediate watchdog exit completed. Broker
verified the account flat with no open orders. Local reports/operator_drill.json
and state/postmortem_2026-09-08.md contain actual fills and outcome. Halt is latched.
Account response omits daytrade_count; autonomous startup requires a tested
history-derived counter fallback rather than assuming zero. No autonomous strategy
was enabled by this manual execution test.

## Revised operator mandate and experimental worker
The operator explicitly requested immediate allocation with <=$250 cash and
authorized strategy replacement. config/paper_experiment.json and
docs/paper-experiment.md define the separate experimental momentum service.
It is not a qualified pullback deployment. Current execution status must be read
from local reports/momentum_paper_account.json, runtime ledger and the broker.
Local logs/momentum_service_v3.log tracks the current startup. Credentials are
in the hidden process environment only. Do not launch another worker or watchdog
without inspecting existing processes and ownership locks.

Integration repairs: accept a held bracket stop only with a fully filled parent
and active profit leg; allow a bounded 15-second partial-fill settlement interval
while blocking new entries; reset stale empty liquidation phase only when flat,
reconciled, and holding watchdog ownership. Actual earlier recovery fills are
journaled; postmortem_momentum_2026-09-08.md stays local. Full suite:295 tests.
Research: final252-session experiment return11.70%, SPY price return18.15%,
167 trades, max closing drawdown5.79%. Experimental, not proven outperformance.

## Intraday research target revision
Operator targets10% daily portfolio return. config/intraday_research.json and
docs/intraday-research.md define a read-only stock continuation scanner and
historical diagnostic. New recent SIP bars are forbidden by current entitlement;
delayed SIP and real-time IEX are accessible. No silent feed substitution.
Scanner captures prospective mover snapshots and marks delayed candidates
ineligible. First current-universe diagnostic:41 sessions,2 trades,-0.74%,zero
10% days, selection-biased; not a qualification or a basis for claimed profits.
Current ETF worker/watchdog remain separate. Latest scanner state lives in
reports/intraday_latest.json and its process/log files stay local.
