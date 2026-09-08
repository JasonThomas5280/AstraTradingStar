# Deployment qualification evidence

`validate_evidence(root, now)` returns blockers. An empty list indicates internal
consistency of operator-supplied evidence, not independently authenticated broker
results, CI results, research integrity, or a promise of profit. Never promote test
fixtures, fabricated reports, or daily-bar research into deployment evidence.

The operator must supply `state/qualification.json` containing:

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-08T00:00:00Z",
  "source_fingerprint": "SHA256 returned by source_fingerprint(root)",
  "git_head": "exact current git HEAD",
  "artifacts": {
    "ci": {"path": "state/ci.json", "sha256": "file SHA256"},
    "replay": {"path": "state/replay.json", "sha256": "file SHA256"},
    "oos": {"path": "state/oos.json", "sha256": "file SHA256"},
    "kill_switch": {"path": "state/kill_switch.json", "sha256": "file SHA256"}
  }
}
```

All four JSON artifacts repeat `generated_at`, `source_fingerprint`, and
`git_head`. Manifest and artifacts must be no more than seven days old, with
timezone-aware timestamps and no future generation dates. Source fingerprints
include sorted relative paths and bytes of `src/**/*.py` and `pyproject.toml`;
configuration changes do not alter them. Any commit changes HEAD and requires
fresh evidence for that revision, including documentation commits.

Each artifact also contains `provenance` with `kind: "real"`, a nonempty
`provider`, and nonempty `inputs`: an array of `{path, sha256}` references to
nonempty local source exports. All references must resolve within the repository.
Digests detect changes; they do not prove an operator's assertion is truthful.
Use actual CI exports, historical intraday data/trade ledgers, and broker drill
responses. Keep exports free of API secrets. These files are operator supplied;
the software does not manufacture passing evidence.

Additional required artifact fields:

| Artifact | Fields and acceptance criteria |
|---|---|
| `ci` | `conclusion: "success"`, `tested_sha` equals HEAD, `tests_passed > 0`, `tests_failed: 0`; `coverage_percent` maps each of `position_sizer`, `stop_manager`, `risk_gate`, `circuit_breaker` to a number in [95,100]. |
| `replay` | `start`, `end` span at least 90 days; `bar_interval` is `1Min`, `5Min`, or `15Min`; `hard_risk_breaches: 0`, `bars_processed > 0`, `trades > 0`. Daily data is insufficient. |
| `oos` | `start`, `end` span at least 30 days; `training_end < start`; `trades > 0`; `net_profit_after_costs > 0` and `expectancy_after_costs > 0` with expectancy equal to net profit divided by trades. Include commissions, slippage and spread in the underlying research. |
| `kill_switch` | `environment: "alpaca_paper"`, `initial_positions > 0`, `initial_open_orders > 0`, `final_positions: 0`, `final_open_orders: 0`; `triggered_at` through `verified_flat_at` is at most 60 seconds. Provenance must contain real paper broker cancellation, liquidation and post-drill verification responses. |

Replay and OOS periods must end by artifact generation; drill verification must
precede generation. A real drill is an explicit operator-controlled activity;
this validator does not place orders to generate evidence. A passing validator
cannot replace reviewing data completeness, survivorship bias, overfitting,
execution assumptions and the underlying broker records.
