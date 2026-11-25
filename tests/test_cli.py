import json
from pathlib import Path

import pytest

from wc_readiness import cli
from wc_readiness.fhir_client import FixtureSource

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "messages"


def test_demo_flag_prints_cards_and_worklist(capsys):
    assert cli.main(["--demo"]) == 0
    out = capsys.readouterr().out
    assert "WC READINESS CARD   READY" in out
    assert "NOT READY (3 blocking gap(s))" in out
    assert "Coordinator worklist" in out
    assert "CLAIM_NUMBER_MISSING" in out


def test_demo_json(capsys):
    assert cli.main(["demo", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [c["status"] for c in data["cards"]] == ["READY", "NOT READY (3 blocking gap(s))"]
    assert len(data["worklist"]) == 6


def test_no_args_prints_help(capsys):
    assert cli.main([]) == 1
    assert "usage" in capsys.readouterr().out


@pytest.fixture
def offline_card_command(monkeypatch, demo_data):
    """Point `card` at the demo fixture instead of the network."""
    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: FixtureSource(demo_data))


def test_card_then_worklist_update_audit(offline_card_command, tmp_path, capsys):
    db = str(tmp_path / "wl.db")
    adt = str(EXAMPLES / "a04_wc_incomplete.hl7")
    rc = cli.main(["card", "--patient", "demo-wc-1002", "--adt", adt, "--db", db])
    assert rc == 2  # not ready
    assert "opened" in capsys.readouterr().out

    assert cli.main(["worklist", "--db", db]) == 0
    assert "EMPLOYER_MISSING" in capsys.readouterr().out

    assert cli.main(["update", "1", "--status", "done", "--owner", "Knok", "--db", db]) == 0
    assert "done" in capsys.readouterr().out
    assert cli.main(["worklist", "--db", db, "--status", "done"]) == 0
    assert "Knok" in capsys.readouterr().out

    assert cli.main(["audit", "--db", db]) == 0
    assert "updated" in capsys.readouterr().out
    assert cli.main(["update", "99", "--status", "done", "--db", db]) == 1


def test_card_ready_exit_code_and_no_worklist(offline_card_command, tmp_path, capsys):
    rc = cli.main(
        [
            "card",
            "--patient",
            "demo-wc-1001",
            "--encounter",
            "demo-enc-1001",
            "--adt",
            str(EXAMPLES / "a04_wc_complete.hl7"),
            "--no-worklist",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["cards"][0]["ready"] is True


def test_adt_command(capsys):
    assert cli.main(["adt", str(EXAMPLES / "a04_wc_complete.hl7")]) == 0
    assert json.loads(capsys.readouterr().out)["claim_number"]["value"] == "WC-2026-004512"


def test_bad_adt_file(tmp_path, capsys):
    bad = tmp_path / "bad.hl7"
    bad.write_text("not hl7")
    assert cli.main(["adt", str(bad)]) == 1
    assert "not a valid HL7 v2 message" in capsys.readouterr().err
    assert cli.main(["adt", str(tmp_path / "missing.hl7")]) == 1
