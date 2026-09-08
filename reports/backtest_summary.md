# Frozen pullback strategy backtest

Starting equity: $2,500 per independent symbol run. Costs: 1 basis point per side; slippage: 2 basis points per side. Whole-share sizing, 15% name cap, effective 0.125% initial stop-risk budget. No parameter tuning.

| Symbol | Replay trades | Replay return | Max close drawdown | Holdout trades | Holdout return |
|---|---:|---:|---:|---:|---:|
| AAPL | 0 | 0.00% | 0.00% | 0 | 0.00% |
| AMZN | 0 | 0.00% | 0.00% | 0 | 0.00% |
| GOOGL | 0 | 0.00% | 0.00% | 0 | 0.00% |
| META | 0 | 0.00% | 0.00% | 0 | 0.00% |
| MSFT | 0 | 0.00% | 0.00% | 0 | 0.00% |
| NVDA | 0 | 0.00% | 0.00% | 0 | 0.00% |
| QQQ | 0 | 0.00% | 0.00% | 0 | 0.00% |
| SPY | 0 | 0.00% | 0.00% | 0 | 0.00% |

Replay: 2025-07-11 through 2026-09-04 (291 sessions after warmup). Holdout: 2026-07-27 through 2026-09-04 (30 sessions), reset to cash with prior bars used only for indicators.

Result: zero filled trades. Profitability, expectancy and win rate are unestablished; zero drawdown reflects remaining in cash. This strategy does not currently support the requested $250 cash ceiling.

Limitations: independent daily-bar simulations, not a combined portfolio or intraday risk replay. Previously downloaded local SIP datasets were reused; no new download or independent authenticity check occurred in this run. The holdout has already been inspected, so any rule changes require new unseen validation data.

The separate manual broker execution drill is excluded from strategy backtest results.
