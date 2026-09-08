"""Require >=95% statement+branch coverage in EACH critical risk module."""
import json
from pathlib import Path

data = json.loads(Path("coverage.json").read_text())
required = {"risk_gate.py", "circuit_breaker.py", "position_sizer.py", "stop_manager.py"}
seen = set()
for filename, info in data["files"].items():
    name = filename.replace("\\", "/").split("/")[-1]
    if name in required:
        seen.add(name)
        percent = info["summary"]["percent_covered"]
        print(f"{name}: {percent:.2f}%")
        if percent < 95:
            raise SystemExit(f"FAIL: {name} coverage below 95%")
if seen != required:
    raise SystemExit("FAIL: missing coverage for a required risk module")
