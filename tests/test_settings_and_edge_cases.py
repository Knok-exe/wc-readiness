"""Site settings stored in the worklist DB (including DBs made before settings existed),
FHIR-only WC status, next-link ports, Encounters without a subject, DB locking during the FHIR
fetch, WC class case, Y99.0, and audit output."""

import copy
import sqlite3

import pytest

from wc_readiness import cli
from wc_readiness.adt import parse_adt
from wc_readiness.card import build_card
from wc_readiness.fhir_client import FixtureSource, same_server
from wc_readiness.fhir_facts import ContextMismatch, gather
from wc_readiness.worklist import SettingsConflict, Worklist

P1, E1, P2, E2 = "demo-wc-1001", "demo-enc-1001", "demo-wc-1002", "demo-enc-1002"


def seg(text, name, idx, value):
    out = []
    for line in text.replace("\r", "\n").split("\n"):
        f = line.split("|")
        if f and f[0] == name:
            f += [""] * (idx + 1 - len(f))
            f[idx] = value
        out.append("|".join(f))
    return "\r".join(x for x in out if x)


def res(data, rtype, rid):
    return next(r for r in data["resources"] if r["resourceType"] == rtype and r["id"] == rid)


def state(wl):
    return [(i.gap_code, i.status, i.flag) for i in wl.items(include_closed=True)]


def site_w_adt(incomplete_a04):
    """site WC class 'W', IN1 not WC, no ACC."""
    text = "\r".join(
        x for x in incomplete_a04.replace("\r", "\n").split("\n") if x and not x.startswith("ACC")
    )
    return seg(seg(text, "PV1", 20, "W"), "IN1", 2, "PLAN01^Commercial")


@pytest.fixture
def offline(monkeypatch, demo_data):
    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: FixtureSource(demo_data))
    return demo_data


# ---- --no-worklist with another patient's encounter ----------------------------------------------


def test_no_worklist_other_patients_encounter_exits_1_cleanly(offline, capsys):
    rc = cli.main(["card", "--patient", P2, "--encounter", E1, "--no-worklist"])
    err = capsys.readouterr().err
    assert rc == 1
    assert err.startswith("error: Encounter/demo-enc-1001 belongs to")
    assert "Traceback" not in err


# ---- a DB from before settings existed -----------------------------------------------------------


@pytest.fixture
def legacy_w_db(tmp_path, demo_data, incomplete_a04):
    """a DB built with --wc-class W before settings were saved; one item in progress."""
    db = tmp_path / "old.db"
    adt_path = tmp_path / "w.hl7"
    adt_path.write_text(site_w_adt(incomplete_a04))
    nodx = copy.deepcopy(demo_data)
    nodx["resources"] = [r for r in nodx["resources"] if r["resourceType"] != "Condition"]
    with Worklist(db) as wl:
        card = build_card(
            gather(FixtureSource(nodx), P2, E2), parse_adt(adt_path.read_text()), {"W"}
        )
        wl.sync(card)
        wl.update(wl.items()[0].id, status="in_progress", owner="Knok")
        wl.conn.execute("DELETE FROM settings")  # = no settings table content in old versions
        wl.conn.execute("DROP TRIGGER audit_log_no_delete")  # older DBs had none
        wl.conn.execute("DELETE FROM audit_log WHERE item_id IS NULL")
        wl.conn.commit()
    return db, adt_path, nodx


def _card_args(db, adt):
    return ["card", "--patient", P2, "--encounter", E2, "--adt", str(adt), "--db", str(db)]


def test_legacy_db_without_flags_is_refused(legacy_w_db, monkeypatch, capsys):
    db, adt, nodx = legacy_w_db
    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: FixtureSource(nodx))
    with Worklist(db) as wl:
        before = state(wl)
    assert cli.main(_card_args(db, adt)) == 1
    assert "created before site settings" in capsys.readouterr().err
    with Worklist(db) as wl:
        assert state(wl) == before
        assert wl.setting("wc_classes") is None


def test_legacy_db_with_flags_records_them_and_closes_nothing(legacy_w_db, monkeypatch):
    db, adt, nodx = legacy_w_db
    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: FixtureSource(nodx))
    args = [*_card_args(db, adt), "--wc-class", "W", "--site-tz", "America/New_York"]
    assert cli.main(args) == 2
    with Worklist(db) as wl:
        assert wl.setting("wc_classes") == '["W"]'
        assert not [r for r in wl.audit() if r["action"] == "resolved"]
        assert [r["detail"] for r in wl.audit() if r["action"] == "settings"] == [
            "wc_classes = '[\"W\"]'",
            "site_tz = 'America/New_York'",
        ]
    assert cli.main(_card_args(db, adt)) == 2  # from now on the flags aren't needed


def test_legacy_db_accept_defaults(legacy_w_db, monkeypatch):
    db, adt, nodx = legacy_w_db
    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: FixtureSource(nodx))
    assert cli.main([*_card_args(db, adt), "--accept-defaults"]) in (2, 3)
    with Worklist(db) as wl:
        assert wl.setting("site_tz") == "America/New_York"


def test_library_sync_on_legacy_db_is_refused(legacy_w_db, demo_data, incomplete_a04):
    db, adt, nodx = legacy_w_db
    card = build_card(gather(FixtureSource(nodx), P2, E2), parse_adt(adt.read_text()))
    with Worklist(db) as wl, pytest.raises(SettingsConflict):
        wl.sync(card)


def test_new_db_still_saves_defaults(offline, tmp_path):
    db = tmp_path / "new.db"
    assert cli.main(["card", "--patient", P1, "--encounter", E1, "--db", str(db)]) in (0, 2)
    with Worklist(db) as wl:
        assert wl.setting("site_tz") == "America/New_York"


# ---- FHIR-only WC status -------------------------------------------------------------------------


def test_fhir_only_unclear_item_resolves_when_wc_coverage_appears(demo_data):
    data = copy.deepcopy(demo_data)
    wl = Worklist()
    first = build_card(gather(FixtureSource(data), P2, E2))
    assert first.wc_status == "unclear"
    wl.sync(first)
    wc = copy.deepcopy(res(data, "Coverage", "demo-cov-1002-ph"))
    wc["id"] = "cov-wc-new"
    wc["type"] = {"coding": [{"system": "urn:oid:2.16.840.1.113883.3.221.5", "code": "95"}]}
    wc["payor"] = [{"display": "New WC Carrier"}]
    data["resources"].append(wc)
    second = build_card(gather(FixtureSource(data), P2, E2))
    assert second.wc_status == "wc"
    assert wl.sync(second)["resolved"] == 1
    item = next(i for i in wl.items(include_closed=True) if i.gap_code == "WC_STATUS_UNCLEAR")
    assert item.status == "resolved"


def test_adt_case_unclear_item_still_held_without_adt(demo_data, incomplete_a04):
    """The no-ADT protection still applies once the case has had an ADT."""
    adt = seg(seg(incomplete_a04, "PV1", 20, "SP"), "IN1", 2, "PLAN01^Commercial")
    data = copy.deepcopy(demo_data)
    wl = Worklist()
    wl.sync(build_card(gather(FixtureSource(data), P2, E2), parse_adt(adt)))
    wc = copy.deepcopy(res(data, "Coverage", "demo-cov-1002-ph"))
    wc["id"] = "cov-wc-new"
    wc["type"] = {"coding": [{"system": "urn:oid:2.16.840.1.113883.3.221.5", "code": "95"}]}
    data["resources"].append(wc)
    no_adt = build_card(gather(FixtureSource(data), P2, E2))
    assert no_adt.wc_status == "wc"
    wl.sync(no_adt)
    item = next(i for i in wl.items(include_closed=True) if i.gap_code == "WC_STATUS_UNCLEAR")
    # Resolved by positive FHIR evidence (active WC coverage).
    assert item.status == "resolved"


# ---- next-link paths and default ports -----------------------------------------------------------

BASE = "https://fhir.example.com/r4/tenant"


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://fhir.example.com:443/r4/tenant/x", True),
        ("https://FHIR.example.com:443/r4/tenant/x", True),
        ("https://fhir.example.com/r4/tenant/../other/x", False),
        ("https://fhir.example.com/r4/tenant/%2e%2e/other/x", False),
        ("https://fhir.example.com/r4/tenant/%2E%2E/other/x", False),
        ("https://fhir.example.com/r4/tenant/./x", False),
        ("https://fhir.example.com:8443/r4/tenant/x", False),
        ("https://fhir.example.com:notaport/r4/tenant/x", False),
        ("https://user:pw@fhir.example.com/r4/tenant/x", False),
        ("https://fhir.example.com\\@evil.net/r4/tenant/x", False),
        ("https://fhir.example.com./r4/tenant/x", False),
    ],
)
def test_next_link_path_and_default_ports(url, ok):
    assert same_server(url, BASE) is ok


def test_base_with_explicit_default_port():
    assert same_server("https://h/r4/x", "https://h:443/r4")
    assert same_server("http://h:80/r4/x", "http://h/r4")
    assert not same_server("http://h:443/r4/x", "http://h/r4")


# ---- Encounter without a subject -----------------------------------------------------------------


def test_encounter_without_subject_is_refused(demo_data):
    data = copy.deepcopy(demo_data)
    res(data, "Encounter", E1).pop("subject", None)
    with pytest.raises(ContextMismatch, match="subject is missing"):
        gather(FixtureSource(data), P2, E1)
    with pytest.raises(ContextMismatch):
        gather(FixtureSource(data), P1, E1)  # even "its own" patient: can't be shown


# ---- no write lock during the FHIR fetch ---------------------------------------------------------


@pytest.mark.parametrize("extra", [[], ["--site-tz", "UTC", "--reconfigure"]])
def test_db_is_not_write_locked_during_the_fhir_fetch(monkeypatch, demo_data, tmp_path, extra):
    db = tmp_path / "lock.db"
    if extra:  # a configured DB being reconfigured
        assert cli.main(["--demo", "--db", str(db), "--format", "json"]) == 0
    seen = []

    class Probe(FixtureSource):
        def read(self, rtype, rid):
            con = sqlite3.connect(db, timeout=0)
            try:
                con.execute("CREATE TABLE IF NOT EXISTS other_writer (x)")
                con.commit()
                seen.append("write ok")
            except sqlite3.OperationalError as exc:
                seen.append(str(exc))
            finally:
                con.close()
            return super().read(rtype, rid)

    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: Probe(demo_data))
    assert cli.main(["card", "--patient", P1, "--encounter", E1, "--db", str(db), *extra]) in (
        0,
        2,
    )
    assert seen and set(seen) == {"write ok"}
    with Worklist(db) as wl:
        assert wl.setting("site_tz") == ("UTC" if extra else "America/New_York")


# ---- WC class case -------------------------------------------------------------------------------


def test_wc_class_case_doesnt_matter(offline, tmp_path, incomplete_a04, capsys):
    adt = tmp_path / "w.hl7"
    adt.write_text(site_w_adt(incomplete_a04))
    db = tmp_path / "wl.db"
    args = ["card", "--patient", P2, "--encounter", E2, "--adt", str(adt), "--db", str(db)]
    assert cli.main([*args, "--wc-class", "W"]) == 2
    capsys.readouterr()
    assert cli.main([*args, "--wc-class", " w "]) == 2
    out = capsys.readouterr()
    assert "error" not in out.err and "'unchanged': 6" in out.out


# ---- READY case re-run without an ADT ------------------------------------------------------------


def test_ready_case_without_adt_stays_ready_and_says_why(offline, tmp_path, complete_a04, capsys):
    adt = tmp_path / "c.hl7"
    adt.write_text(complete_a04)
    db = str(tmp_path / "wl.db")
    base = ["card", "--patient", P1, "--encounter", E1, "--db", db]
    assert cli.main([*base, "--adt", str(adt)]) == 0
    capsys.readouterr()
    assert cli.main(base) == 0
    out = capsys.readouterr().out
    assert "WC READINESS CARD   READY [2 provisional ADT-based gap(s) not re-checked" in out
    assert "EMPLOYER_MISSING (provisional: no matched ADT)" in out
    with Worklist(db) as wl:
        assert wl.items(include_closed=True) == []
        case = wl.conn.execute("SELECT readiness FROM cases").fetchone()[0]
        assert case.startswith("READY")


def test_without_any_adt_history_provisional_gaps_still_count(offline, tmp_path, capsys):
    rc = cli.main(["card", "--patient", P1, "--encounter", E1, "--db", str(tmp_path / "w.db")])
    assert rc == 2
    assert "NOT READY (1 blocking gap(s), 1 provisional: no matched ADT)" in capsys.readouterr().out


# ---- Y99.0 is a weak WC signal -------------------------------------------------------------------


def test_y99_0_is_a_weak_wc_signal(demo_data):
    data = copy.deepcopy(demo_data)
    data["resources"] = [r for r in data["resources"] if r["resourceType"] != "Condition"]
    data["resources"].append(
        {
            "resourceType": "Condition",
            "id": "y990",
            "subject": {"reference": f"Patient/{P2}"},
            "encounter": {"reference": f"Encounter/{E2}"},
            "code": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/sid/icd-10-cm",
                        "code": "Y99.0",
                        "display": "Civilian activity done for income or pay",
                    }
                ]
            },
        }
    )
    card = build_card(gather(FixtureSource(data), P2, E2))
    assert card.fhir.injury_conditions == []  # still not an injury diagnosis
    assert card.wc_status == "unclear"
    assert any(s.startswith("Y99.0") and s.endswith("[weak]") for s in card.wc_signals)


# ---- audit output for settings -------------------------------------------------------------------


def test_audit_shows_settings_rows_as_settings(tmp_path, capsys):
    db = str(tmp_path / "d.db")
    assert cli.main(["--demo", "--db", db, "--format", "json"]) == 0
    capsys.readouterr()
    assert cli.main(["audit", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "item None" not in out
    assert "  settings  settings " in out
    assert "item   1  opened" in out
