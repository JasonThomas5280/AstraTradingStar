"""Fail-closed consistency checks for operator-supplied deployment evidence.

This does not authenticate broker exports, CI, or the research methodology.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess


RISK_MODULES = ("position_sizer", "stop_manager", "risk_gate", "circuit_breaker")


def source_fingerprint(root: Path) -> str:
    """Hash sorted relative source paths and bytes, plus pyproject.toml."""
    digest = hashlib.sha256()
    paths = sorted([*root.glob("src/**/*.py"), root / "pyproject.toml"],
                   key=lambda p: p.relative_to(root).as_posix())
    for path in paths:
        name = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def _time(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return stamp


def _number(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a numeric metric")
    if not math.isfinite(value) or value < minimum:
        raise ValueError("invalid numeric metric")
    return value


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _reference(root, ref):
    path = (root / ref["path"]).resolve()
    _require(path.is_relative_to(root.resolve()), "artifact path escapes repository")
    data = path.read_bytes()
    _require(hashlib.sha256(data).hexdigest() == ref["sha256"], "artifact SHA256 mismatch")
    return data


def _period(artifact, days):
    start, end = _time(artifact["start"]), _time(artifact["end"])
    _require(end - start >= timedelta(days=days), f"requires at least {days} days")
    _require(end <= _time(artifact["generated_at"]), "period ends after generation")


def validate_evidence(root: Path, now: datetime) -> list[str]:
    """Return actionable blockers; only an empty list passes the gate."""
    blockers = []
    try:
        _require(now.tzinfo is not None, "now must include a timezone")
        manifest = json.loads((root / "state/qualification.json").read_text())
        _require(manifest["schema_version"] == 1, "unsupported evidence schema")
        fingerprint = source_fingerprint(root)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True, check=True,
                              timeout=10).stdout.strip()
        _require(manifest["source_fingerprint"] == fingerprint, "source fingerprint changed")
        _require(manifest["git_head"] == head, "evidence is not for current git HEAD")
        _require(timedelta(0) <= now - _time(manifest["generated_at"]) <= timedelta(days=7),
                 "manifest expired or from the future")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        return [f"qualification manifest: {exc}"]
    for kind in ("ci", "replay", "oos", "kill_switch"):
        try:
            artifact = json.loads(_reference(root, manifest["artifacts"][kind]))
            _require(artifact["source_fingerprint"] == fingerprint, "artifact source fingerprint mismatch")
            _require(artifact["git_head"] == head, "artifact git HEAD mismatch")
            _require(timedelta(0) <= now - _time(artifact["generated_at"]) <= timedelta(days=7),
                     "artifact expired or from the future")
            provenance = artifact["provenance"]
            _require(provenance["kind"] == "real", "synthetic or unknown provenance rejected")
            _require(isinstance(provenance["provider"], str) and bool(provenance["provider"].strip()),
                     "provider required")
            _require(bool(provenance["inputs"]), "hashed source inputs required")
            for reference in provenance["inputs"]:
                _require(bool(_reference(root, reference)), "empty provenance input")
            if kind == "ci":
                _require(artifact["conclusion"] == "success", "CI must succeed")
                _require(artifact["tested_sha"] == head, "CI tested SHA must match HEAD")
                _require(_number(artifact["tests_passed"], 1) > 0, "CI has no tests")
                _require(_number(artifact["tests_failed"]) == 0, "CI tests failed")
                for module in RISK_MODULES:
                    coverage = _number(artifact["coverage_percent"][module], 95)
                    _require(coverage <= 100, "coverage exceeds 100%")
            elif kind == "replay":
                _period(artifact, 90)
                _require(artifact["bar_interval"] in ("1Min", "5Min", "15Min"),
                         "requires intraday replay; daily bars cannot qualify")
                _require(_number(artifact["hard_risk_breaches"]) == 0, "hard risk breaches occurred")
                _number(artifact["bars_processed"], 1)
                _number(artifact["trades"], 1)
            elif kind == "oos":
                _period(artifact, 30)
                _require(_time(artifact["training_end"]) < _time(artifact["start"]),
                         "training must end before out-of-sample period")
                trades = _number(artifact["trades"], 1)
                profit = _number(artifact["net_profit_after_costs"], 0)
                expectancy = _number(artifact["expectancy_after_costs"], 0)
                _require(expectancy > 0 and profit > 0, "OOS expectancy must be positive after costs")
                _require(math.isclose(profit / trades, expectancy, rel_tol=1e-6, abs_tol=1e-8),
                         "OOS expectancy disagrees with profit/trades")
            else:
                _require(artifact["environment"] == "alpaca_paper", "real Alpaca paper drill required")
                _number(artifact["initial_positions"], 1)
                _number(artifact["initial_open_orders"], 1)
                _require(_number(artifact["final_positions"]) == 0, "drill did not flatten positions")
                _require(_number(artifact["final_open_orders"]) == 0, "drill left open orders")
                started, flat = _time(artifact["triggered_at"]), _time(artifact["verified_flat_at"])
                _require(timedelta(0) <= flat - started <= timedelta(seconds=60), "drill exceeded 60 seconds")
                _require(flat <= _time(artifact["generated_at"]), "drill verification is after generation")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            blockers.append(f"{kind}: {exc}")
    return blockers
