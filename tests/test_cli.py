import json
from pathlib import Path
import pytest
from alphagrid.cli import main
from alphagrid import cli
from alphagrid.execution.broker import BrokerError


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config").mkdir()
    original = Path(__file__).resolve().parents[1] / "config/authorization.yaml"
    (tmp_path / "config/authorization.yaml").write_text(original.read_text())
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("test")
    return tmp_path


def test_status_and_readiness_are_honest(root, monkeypatch, capsys):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert main(["--root", str(root), "status"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report["qualification_passed"] and report["blockers"]
    assert report["target_cash_fraction"] == "0.10"
    assert main(["--root", str(root), "readiness"]) == 1


def test_cli_sanitizes_missing_credentials(root, monkeypatch, capsys):
    def fail(*args):
        raise BrokerError("missing_credentials")
    monkeypatch.setattr(cli.PaperBroker, "account", fail)
    assert main(["--root", str(root), "check-account"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "missing_credentials"


def test_cannot_run_watchdog_without_initialization(root, capsys):
    assert main(["--root", str(root), "watchdog", "--once"]) == 1
    assert "initialize_required" in capsys.readouterr().out


def test_research_invalid_symbol_no_network(root, capsys):
    assert main(["--root", str(root), "research", "../bad", "no.csv"]) == 1
    assert "invalid_symbol" in capsys.readouterr().out
