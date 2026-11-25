"""Regression tests for Cipher's re-test of 4185048 ('Re-test of 4185048' in
CIPHER-CODE-REVIEW.md): NEW-1 (a no-ADT re-run must not call a NOT READY case READY), NEW-2
(concurrent first runs must not overwrite each other's settings) and NEW-3 (next-link path
tricks). Probes s01/s01b/s02/s03 are ported here."""

import json
import sqlite3

import pytest

from wc_readiness import cli
from wc_readiness.adt import parse_adt
from wc_readiness.card import build_card
from wc_readiness.fhir_client import FixtureSource, same_server
from wc_readiness.fhir_facts import gather
from wc_readiness.worklist import SettingsConflict, Worklist

P1, E1 = "demo-wc-1001", "demo-enc-1001"
BASE = "https://fhir.example.com/r4/tenant"


def seg(text, name, idx, value):
    out = []
    for line in text.replace("\r", "\n").split("\n"):
        f = line.split("|")
        if f and f[0] == name:
            f += [""] * (idx + 1 - len(f))
            f[idx] = value
        out.append("|".join(f))
    return "\r".join(x for x in out if x)


@pytest.fixture
def offline(monkeypatch, demo_data):
    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: FixtureSource(demo_data))
    return demo_data


@pytest.fixture
def no_employer_adt(tmp_path, complete_a04):
    """s01: the complete A04 without IN2/GT1 (no employer) and without IN1-36 (claim number)."""
    text = "\r".join(
        x
        for x in complete_a04.replace("\r", "\n").split("\n")
        if x and not x.startswith(("IN2", "GT1"))
    )
    path = tmp_path / "noemp.hl7"
    path.write_text(seg(text, "IN1", 36, ""))
    return path


def run(capsys, *args):
    rc = cli.main(list(args))
    return rc, capsys.readouterr().out


def status_line(out):
    return next(x for x in out.splitlines() if x.startswith("WC READINESS CARD"))


def item_id(db, code):
    with Worklist(db, create=False) as wl:
        return next(i.id for i in wl.items(include_closed=True) if i.gap_code == code)


# ---- NEW-1 ---------------------------------------------------------------------------------------


def test_new1_not_ready_case_stays_not_ready_without_adt(
    offline, tmp_path, no_employer_adt, capsys
):
    db = str(tmp_path / "w.db")
    base = ["card", "--patient", P1, "--encounter", E1, "--db", db]
    rc, out = run(capsys, *base, "--adt", str(no_employer_adt))
    assert rc == 2 and "NOT READY (1 blocking gap(s))" in status_line(out)

    rc, out = run(capsys, *base)
    line = status_line(out)
    assert rc == 2
    assert line.startswith(
        "WC READINESS CARD   NOT READY (1 blocking item(s) still open from the last ADT run: "
        "EMPLOYER_MISSING; no matched ADT this run)"
    )
    assert "provisional ADT-based gap(s) not re-checked" in line

    rc, out = run(capsys, *base, "--format", "json")
    card = json.loads(out)["cards"][0]
    assert rc == 2 and card["ready"] is False and card["status"].startswith("NOT READY")
    assert card["held_blocking"] == ["EMPLOYER_MISSING"]
    with Worklist(db, create=False) as wl:
        assert wl.conn.execute("SELECT readiness FROM cases").fetchone()[0].startswith("NOT READY")
        high = [(i.gap_code, i.status) for i in wl.items() if i.severity == "high"]
        assert high == [("EMPLOYER_MISSING", "open")]

    # --no-worklist reads the same DB and gives the same verdict, writing nothing.
    before = (tmp_path / "w.db").read_bytes()
    rc, out = run(capsys, *base, "--no-worklist")
    assert rc == 2 and status_line(out) == line
    assert "read only (--no-worklist), not updated" in out
    assert (tmp_path / "w.db").read_bytes() == before


def test_new1_done_item_still_present_blocks_until_acknowledged(
    offline, tmp_path, no_employer_adt, capsys
):
    db = str(tmp_path / "w.db")
    base = ["card", "--patient", P1, "--encounter", E1, "--db", db]
    adt = ["--adt", str(no_employer_adt)]
    assert run(capsys, *base, *adt)[0] == 2
    emp = item_id(db, "EMPLOYER_MISSING")

    # Marked done, no ADT since: the last ADT run's item is closed, so READY (plus the note).
    assert cli.main(["update", "--db", db, str(emp), "--status", "done"]) == 0
    rc, out = run(capsys, *base)
    assert rc == 0
    assert status_line(out).startswith("WC READINESS CARD   READY [2 provisional")

    # The next ADT run still sees the gap: done + still_present. Without ADT that blocks.
    assert run(capsys, *base, *adt)[0] == 2
    with Worklist(db, create=False) as wl:
        assert wl.get(emp).flag == "still_present"
    rc, out = run(capsys, *base)
    assert rc == 2
    assert "1 blocking item(s) still open from the last ADT run: EMPLOYER_MISSING" in out
    rc, out = run(capsys, *base, "--no-worklist")
    assert rc == 2 and "EMPLOYER_MISSING; no matched ADT" in status_line(out)

    # The coordinator acknowledges it (done again): no longer blocking.
    assert cli.main(["update", "--db", db, str(emp), "--status", "done"]) == 0
    capsys.readouterr()
    assert run(capsys, *base)[0] == 0
    assert run(capsys, *base, "--no-worklist")[0] == 0


def test_new1_ready_case_without_adt_and_in_progress_item(
    offline, tmp_path, no_employer_adt, capsys
):
    db = str(tmp_path / "w.db")
    base = ["card", "--patient", P1, "--encounter", E1, "--db", db]
    assert run(capsys, *base, "--adt", str(no_employer_adt))[0] == 2
    emp = item_id(db, "EMPLOYER_MISSING")
    assert cli.main(["update", "--db", db, str(emp), "--status", "in_progress"]) == 0
    capsys.readouterr()
    rc, out = run(capsys, *base)
    assert rc == 2 and "still open from the last ADT run: EMPLOYER_MISSING" in out


def test_new1_no_worklist_without_a_db_counts_provisional_gaps(offline, tmp_path, capsys):
    db = tmp_path / "none.db"
    rc, out = run(
        capsys, "card", "--patient", P1, "--encounter", E1, "--db", str(db), "--no-worklist"
    )
    assert rc == 2
    assert "ADT history is unknown, so provisional gaps count" in out
    assert "NOT READY (1 blocking gap(s), 1 provisional: no matched ADT)" in status_line(out)
    assert not db.exists()


def test_new1_no_worklist_on_legacy_db_reads_without_migrating(
    offline, tmp_path, demo_data, complete_a04, capsys
):
    db = tmp_path / "old.db"
    with Worklist(db) as wl:
        wl.sync(build_card(gather(FixtureSource(demo_data), P1, E1), parse_adt(complete_a04)))
        wl.conn.execute("DELETE FROM settings")
        wl.conn.execute("DROP TRIGGER audit_log_no_delete")  # older DBs had none
        wl.conn.execute("DELETE FROM audit_log")
        wl.conn.commit()
    before = db.read_bytes()
    base = ["card", "--patient", P1, "--encounter", E1, "--db", str(db), "--no-worklist"]
    assert cli.main(base) == 1
    assert "created before site settings were saved" in capsys.readouterr().err
    assert cli.main([*base, "--accept-defaults"]) == 0  # READY ADT run, nothing open
    assert db.read_bytes() == before


def test_new1_read_only_worklist_never_writes(tmp_path, demo_data):
    db = tmp_path / "w.db"
    Worklist(db).close()
    with Worklist(db, read_only=True) as wl:
        card = build_card(gather(FixtureSource(demo_data), P1, E1), None)
        assert wl.assess(card) is False
        with pytest.raises(sqlite3.OperationalError, match="read-only"):
            wl.sync(card)


# ---- NEW-2 ---------------------------------------------------------------------------------------


def test_new2_concurrent_first_runs_dont_overwrite_settings(tmp_path):
    db = tmp_path / "race.db"
    a, b = Worklist(db), Worklist(db)
    a.configure({"wc_classes": '["W"]', "site_tz": "UTC"}, {"wc_classes", "site_tz"})
    b.configure({"wc_classes": '["WC"]', "site_tz": "UTC"}, set())  # defaults, decided "at once"
    a.sync(build_card_stub())
    a.conn.commit()
    with pytest.raises(SettingsConflict, match="another run changed the site setting wc_classes"):
        b.sync(build_card_stub())
    assert not b.conn.in_transaction  # rolled back
    a.close(), b.close()
    with Worklist(db) as wl:
        assert wl.setting("wc_classes") == '["W"]'
        details = [r["detail"] for r in wl.audit() if r["action"] == "settings"]
        assert details == ["wc_classes = '[\"W\"]'", "site_tz = 'UTC'"]


def test_new2_concurrent_first_runs_with_same_settings_are_fine(tmp_path):
    db = tmp_path / "same.db"
    a, b = Worklist(db), Worklist(db)
    for wl in (a, b):
        wl.configure({"wc_classes": '["WC"]', "site_tz": "UTC"}, set())
    a.sync(build_card_stub())
    a.conn.commit()
    b.sync(build_card_stub())
    b.conn.commit()
    with Worklist(db) as wl:
        assert len([r for r in wl.audit() if r["action"] == "settings"]) == 2
    a.close(), b.close()


def build_card_stub():
    """A card with no encounter: sync only saves the settings (the 'skipped' path)."""
    from wc_readiness.fhir_facts import FhirFacts

    return build_card(FhirFacts(patient_id="x"), None)


# ---- NEW-3 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, ok",
    [
        (f"{BASE}/Coverage?page=2", True),
        (f"{BASE}/x;jsessionid=1", True),  # a harmless path parameter is still fine
        (f"{BASE}/..;/other/x", False),
        (f"{BASE}/.;x/other", False),
        (f"{BASE}/..%5cother/x", False),
        (f"{BASE}/..%5Cother/x", False),
        (f"{BASE}/%252e%252e/other/x", False),
        (f"{BASE}/a%2fb", True),  # an encoded slash alone is not a traversal
        (f"{BASE}/..%2fother", False),
        ("https://@fhir.example.com/r4/tenant/x", False),
        ("https://:@fhir.example.com/r4/tenant/x", False),
    ],
)
def test_new3_next_link_path_tricks(url, ok):
    assert same_server(url, BASE) is ok
