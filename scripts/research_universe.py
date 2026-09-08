"""Download actual SIP daily bars and report frozen-strategy research, never trade."""
import csv
import argparse
from datetime import date, timedelta
import json
from pathlib import Path

from alphagrid.execution.broker import PaperBroker
from alphagrid.research.backtest import load_csv, run_backtest

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--initial-equity", type=float, required=True)
args = parser.parse_args()
cfg = json.loads((root / "config/authorization.yaml").read_text())
broker = PaperBroker()
today = date.today()
summaries = []
for symbol in cfg["enabled_symbols"]:
    raw = broker.bars(symbol, (today - timedelta(days=500)).isoformat(),
                      (today - timedelta(days=1)).isoformat() + "T23:59:59Z")
    path = root / "data" / (symbol + ".csv")
    path.parent.mkdir(exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        writer.writerows([b["t"][:10], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in raw)
    result = run_backtest(symbol, load_csv(path), initial_equity=args.initial_equity)
    report = root / "reports" / ("research_" + symbol + ".json")
    report.write_text(json.dumps(result, indent=2, allow_nan=False))
    summary = {"symbol": symbol, "bars": len(raw), "holdout": result["holdout"]}
    summaries.append(summary)
    print(json.dumps(summary), flush=True)
(root / "reports/research_universe.json").write_text(json.dumps(summaries, indent=2))
