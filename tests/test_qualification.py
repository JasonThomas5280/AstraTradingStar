from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from alphagrid.qualification import RISK_MODULES, source_fingerprint, validate_evidence


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("pass\n")
    (tmp_path / "pyproject.toml").write_text("# test")
    (tmp_path / "state").mkdir()
    monkeypatch.setattr("alphagrid.qualification.subprocess.run", lambda *a, **kw: SimpleNamespace(stdout="abc\n"))
    fingerprint = source_fingerprint(tmp_path)
    raw = tmp_path / "state/raw.json"
    raw.write_text('{"fixture": "synthetic test input; never deployment evidence"}')
    ref = {"path": "state/raw.json", "sha256": hashlib.sha256(raw.read_bytes()).hexdigest()}
    common = {"generated_at": NOW.isoformat(), "source_fingerprint": fingerprint,
              "git_head": "abc", "provenance": {"kind": "real", "provider": "test fixture", "inputs": [ref]}}
    artifacts = {
        "ci": dict(common, conclusion="success", tested_sha="abc", tests_passed=111, tests_failed=0,
                   coverage_percent=dict.fromkeys(RISK_MODULES, 95)),
        "replay": dict(common, start="2026-01-01T00:00:00Z", end="2026-05-01T00:00:00Z",
                       bar_interval="1Min", hard_risk_breaches=0, bars_processed=100000, trades=80),
        "oos": dict(common, start="2026-06-01T00:00:00Z", end="2026-07-01T00:00:00Z",
                    training_end="2026-05-31T00:00:00Z", trades=10, net_profit_after_costs=20,
                    expectancy_after_costs=2),
        "kill_switch": dict(common, environment="alpaca_paper", initial_positions=1, initial_open_orders=1,
                            final_positions=0, final_open_orders=0, triggered_at="2026-09-07T23:00:00Z",
                            verified_flat_at="2026-09-07T23:00:59Z"),
    }
    manifest = {"schema_version": 1, "source_fingerprint": fingerprint, "git_head": "abc",
                "generated_at": NOW.isoformat(), "artifacts": {}}

    def save():
        for kind, artifact in artifacts.items():
            path = tmp_path / f"state/{kind}.json"
            path.write_text(json.dumps(artifact))
            manifest["artifacts"][kind] = {"path": f"state/{kind}.json", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        (tmp_path / "state/qualification.json").write_text(json.dumps(manifest))
    save()
    return tmp_path, manifest, artifacts, save


def test_consistent_evidence_shape(evidence):
    root, *_ = evidence
    assert validate_evidence(root, NOW) == []


def test_missing_and_malformed(tmp_path):
    assert validate_evidence(tmp_path, NOW)
    (tmp_path / "state").mkdir()
    (tmp_path / "state/qualification.json").write_text("{")
    assert validate_evidence(tmp_path, NOW)


@pytest.mark.parametrize("kind,field,value", [
    ("ci", "coverage_percent", {"risk_gate": 100}),
    ("ci", "tested_sha", "old"),
    ("ci", "tests_failed", 1),
    ("replay", "bar_interval", "1Day"),
    ("replay", "hard_risk_breaches", 1),
    ("replay", "end", "2026-01-02T00:00:00Z"),
    ("replay", "provenance", {"kind": "synthetic"}),
    ("oos", "trades", 0),
    ("oos", "expectancy_after_costs", 0),
    ("oos", "expectancy_after_costs", 9),
    ("oos", "training_end", "2026-06-02T00:00:00Z"),
    ("kill_switch", "final_positions", 1),
    ("kill_switch", "final_open_orders", 1),
    ("kill_switch", "environment", "mock"),
    ("kill_switch", "verified_flat_at", "2026-09-07T23:01:01Z"),
    ("ci", "generated_at", "2026-08-01T00:00:00Z"),
    ("ci", "source_fingerprint", "wrong"),
])
def test_bad_metrics(evidence, kind, field, value):
    root, _, artifacts, save = evidence
    artifacts[kind][field] = value
    save()
    assert any(item.startswith(kind + ":") for item in validate_evidence(root, NOW))


def test_tampered_artifact(evidence):
    root, *_ = evidence
    (root / "state/ci.json").write_text("{}")
    assert "SHA256 mismatch" in " ".join(validate_evidence(root, NOW))


def test_tampered_provenance(evidence):
    root, *_ = evidence
    (root / "state/raw.json").write_text("changed")
    assert len(validate_evidence(root, NOW)) == 4


@pytest.mark.parametrize("field,value", [("git_head", "old"), ("source_fingerprint", "wrong"),
                                         ("generated_at", "2026-08-01T00:00:00Z")])
def test_manifest_mismatch(evidence, field, value):
    root, manifest, _, save = evidence
    manifest[field] = value
    save()
    assert validate_evidence(root, NOW)


def test_source_change_invalidates_but_config_does_not(evidence):
    root, *_ = evidence
    (root / "config.json").write_text('{"enabled":true}')
    assert not validate_evidence(root, NOW)
    (root / "src/a.py").write_text("changed")
    assert validate_evidence(root, NOW)
