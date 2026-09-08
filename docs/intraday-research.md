# Intraday research toward a 10% daily portfolio return

## Current available-feed variant
The active read-only scanner now uses intraday_iex_news_5m_v1: live IEX bars,
IEX VWAP/relative volume and a provisional 10,000-share IEX session volume floor.
Recent Alpaca news is attached with publication and observation timestamps;
headline presence is not treated as a verified favorable catalyst. This is a
separate unvalidated research variant. It does not require recent SIP access.
The earlier SIP variant and its diagnostic below are retained for comparison;
those returns do not apply to the IEX variant. Both remain observation-only.

Scanner diagnostics preserve signal_eligible and signal_reason separately from
quote_reason and quote_age_seconds. Final eligibility still requires all checks;
a wide quote cannot conceal whether the underlying signal passed. Instrument
name filtering matches whole words, so United/Wright/Community company names
are not excluded merely because they contain unit/right substrings.

The operator's objective is now 10% daily portfolio return, with explicit
acknowledgment that not every day will reach it. This is a measured research
target, not a guarantee, accomplished result, or permission to report a stock's
already-realized daily gain as our portfolio return.

`python -m alphagrid.intraday_scan --root <repository>` runs a read-only scanner
every minute. It records the contemporaneous top-50 mover snapshot and rejects
warrants, rights, units, preferred securities and fund/ETF names. Prospective
snapshots live under ignored data/prospective_movers; the latest report is
reports/intraday_latest.json. The scanner cannot place orders. Existing ETF
execution and protection remain in the separately running paper service.

Frozen five-minute continuation rules:
- At least 10% above previous close, price at least $1, one million SIP session shares or 10,000 IEX session shares for the separate IEX variant.
- Last completed close above session VWAP and prior three-bar high.
- Volume at least 1.5 times preceding six-bar median volume.
- Stop at prior three-bar low, 0.5%-5% from entry limit; full 3R target.
- Quote age at most30 seconds, spread at most0.5%, entry price still within limit.
- No new signals after15:00 ET;45-minute maximum holding period and15:50 exit.

The connected account rejected recent SIP bars with HTTP403 and the explicit
message that its subscription does not permit querying recent SIP data. Delayed
SIP bars and current IEX bars are accessible. IEX covers only one venue and its
volume differs substantially from consolidated volume; it is not silently
substituted into the consolidated-volume strategy. In SIP mode it falls back to
16-minute delayed SIP for observation and always marks those candidates
ineligible for current execution. Real-time SIP entitlement or an explicitly
validated alternative feed is needed for that execution path.

`python scripts/research_intraday.py` downloads prior60 calendar days of actual
five-minute and daily SIP history for the current scanned common-share universe.
It replays signals using only completed prior bars, 5bp costs and10bp slippage
per side, whole-share sizing,14% name caps,0.75% planned stop risk, and daily-loss
stops. Reports/intraday_backtest_sip.json preserves hashes, daily returns, target hit
count and trade details. Daily-loss resets in this diagnostic differ from the
live service's latched halt. Price bars omit dividends; partial fills, spread,
halts and market impact are approximated. It does not simulate reallocating
the currently invested ETF account.

The first nine-symbol diagnostic had41 sessions and2 trades, a net return of
-0.74%, and zero days reaching10%. Today's universe selection creates severe
selection bias; these are neither unbiased nor out-of-sample results. Rules
were frozen before this run. No threshold was tuned to manufacture a passing
result, and this diagnostic does not enable intraday stock execution.

The matched-feed command `python scripts/research_intraday.py --feed iex`
downloads IEX history and writes reports/intraday_backtest_iex.json. Its first
11-symbol replay covered 41 sessions, generated zero trades and returned 0%,
with zero days reaching 10%. This selection-biased diagnostic does not establish
an edge. News is recorded prospectively but is not used in the historical entry
rule; this replay does not validate a news-based strategy.

Provider references: [Historical bars](https://docs.alpaca.markets/us/reference/stockbars)
and [market movers](https://docs.alpaca.markets/us/reference/movers-1).
