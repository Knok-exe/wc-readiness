"""Regression tests for Sunny's file review of dbb0d56 (SUNNY-FILE-REVIEW.md).

Item numbers match the review. Scenarios A-G are ported from Sunny's repro scripts
(wc-readiness-review-scratch/fail_probe.py and sync_probe.py) so they run offline in CI.
"""

import dataclasses
import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wc_readiness import cli
from wc_readiness.adt import parse_adt
from wc_readiness.card import build_card
from wc_readiness.dates import fhir_local_date, hl7_local_date
from wc_readiness.fhir_client import FhirError, FixtureSource
from wc_readiness.fhir_facts import gather
from wc_readiness.worklist import Worklist

ROOT = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
MSH = "MSH|^~\\&|REG|HOSP|APP|HOSP|20261117101500||ADT^A04^ADT_A01|X1|T|2.5.1"


class Flaky(FixtureSource):
    """Same idea as Sunny's fail_probe.Flaky: fail chosen searches/reads with a 504."""

    def __init__(self, data, fail):
        super().__init__(data)
        self.fail = fail

    def search(self, rtype, params):
        if (rtype, "-encounter" in params) in self.fail:
            raise FhirError("HTTP 504 timeout")
        return super().search(rtype, params)

    def read(self, rtype, rid):
        if (rtype, "read") in self.fail:
            raise FhirError("HTTP 504 timeout")
        return super().read(rtype, rid)


@pytest.fixture
def adt1(complete_a04):
    return parse_adt(complete_a04)


@pytest.fixture
def adt2(incomplete_a04):
    return parse_adt(incomplete_a04)


@pytest.fixture
def casey(demo_source, adt2):
    return build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), adt2)


def statuses(wl):
    return {i.gap_code: i.status for i in wl.items(include_closed=True)}


def codes(card):
    return {g.code for g in card.gaps}


# ---- 1. --db default ----------------------------------------------------------------------


def test_1_card_without_db_uses_wc_worklist_db_not_none(monkeypatch, tmp_path, demo_data, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: FixtureSource(demo_data))
    adt = str(ROOT / "examples/messages/a04_wc_incomplete.hl7")
    assert cli.main(["card", "--patient", "demo-wc-1002", "--adt", adt]) == 2
    assert (tmp_path / "wc_worklist.db").exists()
    assert not (tmp_path / "None").exists()
    capsys.readouterr()
    assert cli.main(["worklist"]) == 0  # also no --db: must read the same file
    assert "EMPLOYER_MISSING" in capsys.readouterr().out


def test_1_demo_leaves_no_files(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--demo"]) == 0
    assert list(tmp_path.iterdir()) == []


# ---- 2. failed reads never resolve items --------------------------------------------------


def test_2_timeout_does_not_resolve_items(demo_data, casey, adt2):
    """fail_probe scenario A: Coverage + Condition time out on a re-run."""
    wl = Worklist()
    wl.sync(casey)
    before = statuses(wl)
    flaky = Flaky(demo_data, {("Coverage", False), ("Condition", False)})
    bad = build_card(gather(flaky, "demo-wc-1002", "demo-enc-1002"), adt2)
    assert bad.status.startswith("NOT READY") or bad.status.startswith("UNKNOWN")
    assert "WC_COVERAGE_MISSING" in bad.unchecked
    counts = wl.sync(bad)
    assert counts["resolved"] == 0
    assert statuses(wl) == before


def test_2_encounter_level_coverage_failure_counts_as_unreadable(demo_data, adt1):
    """Low 18: a failed `Coverage?-encounter=` alone also makes coverage unknown."""
    flaky = Flaky(demo_data, {("Coverage", True)})
    card = build_card(gather(flaky, "demo-wc-1001", "demo-enc-1001"), adt1)
    assert card.fhir.coverage_unreadable
    assert not card.ready


def test_2_missing_essential_with_failed_fallback_is_unchecked(demo_data):
    """No ADT and Coverage failed: carrier is unknown, not missing."""
    flaky = Flaky(demo_data, {("Coverage", False)})
    card = build_card(gather(flaky, "demo-wc-1001", "demo-enc-1001"), None)
    assert "CARRIER_MISSING" not in codes(card)


# ---- 3. coordinator decisions stick -------------------------------------------------------


def test_3_dismissed_and_done_do_not_reopen_on_rerun(casey):
    """sync_probe: dismiss item 5, mark item 1 done, re-sync the same card."""
    wl = Worklist()
    wl.sync(casey)
    wl.update(5, status="dismissed", note="ACC-5 not used at our site")
    wl.update(1, status="done", note="called carrier")
    counts = wl.sync(casey)
    assert counts["reopened"] == 0
    assert wl.get(5).status == "dismissed"
    assert wl.get(1).status == "done" and wl.get(1).flag == "still_present"
    assert counts["flagged"] == 1
    assert wl.sync(casey)["flagged"] == 0  # flagged once, not on every run
    assert 1 in [i.id for i in wl.items()]  # a flagged done item stays visible
    assert 5 not in [i.id for i in wl.items()]


def test_3_dismissed_reopens_when_the_gap_changes(casey, demo_source, incomplete_a04):
    wl = Worklist()
    wl.sync(casey)
    item = next(i for i in wl.items() if i.gap_code == "WC_COVERAGE_MISSING")
    wl.update(item.id, status="dismissed", note="plan mapping known issue")
    # Registration changes the financial class to another WC value: message text changes.
    adt = parse_adt(incomplete_a04.replace("|FIN700200B|WC|", "|FIN700200B|WORKCOMP|"))
    changed = build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), adt)
    assert wl.sync(changed)["reopened"] == 1
    assert wl.get(item.id).status == "open"
    assert "gap changed" in wl.audit()[-1]["detail"]


def test_3_done_reopens_when_gap_went_away_and_came_back(casey, demo_source, incomplete_a04):
    wl = Worklist()
    wl.sync(casey)
    claim = next(i for i in wl.items() if i.gap_code == "CLAIM_NUMBER_MISSING")
    wl.update(claim.id, status="done")
    fixed = build_card(
        gather(demo_source, "demo-wc-1002", "demo-enc-1002"),
        parse_adt(incomplete_a04.replace("|UNKNOWN", "|WC-1")),
    )
    wl.sync(fixed)
    assert wl.get(claim.id).status == "done"
    assert wl.sync(casey)["reopened"] == 1
    assert wl.get(claim.id).status == "open"


def test_3_dismiss_needs_note_and_resolved_is_system_only(casey):
    """Low 19."""
    wl = Worklist()
    wl.sync(casey)
    with pytest.raises(ValueError, match="note"):
        wl.update(1, status="dismissed")
    with pytest.raises(ValueError, match="resolved"):
        wl.update(1, status="resolved")


def test_3_old_database_is_migrated_without_reopening(tmp_path, casey):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE work_items (id INTEGER PRIMARY KEY AUTOINCREMENT, case_key TEXT NOT NULL,"
        " gap_code TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL, fix_hint TEXT"
        " NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', owner TEXT NOT NULL DEFAULT"
        " '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        " UNIQUE (case_key, gap_code));"
    )
    conn.close()
    wl = Worklist(db)
    wl.sync(casey)
    wl.update(2, status="dismissed", note="n/a")
    wl.close()
    wl = Worklist(db)
    assert wl.sync(casey)["reopened"] == 0


# ---- 4. Encounter read failure ------------------------------------------------------------


def test_4_encounter_read_failure_keeps_case_key(demo_data, adt1):
    """fail_probe scenario B."""
    card = build_card(
        gather(Flaky(demo_data, {("Encounter", "read")}), "demo-wc-1001", "demo-enc-1001"), adt1
    )
    assert card.case_key == "Patient/demo-wc-1001|Encounter/demo-enc-1001"
    assert "INJURY_DX_MISSING" not in codes(card)
    assert "INJURY_DX_MISSING" in card.unchecked
    assert not card.ready


def test_4_unknown_encounter_is_not_synced(demo_data, adt2):
    flaky = Flaky(demo_data, {("Encounter", False)})  # latest-encounter search fails
    card = build_card(gather(flaky, "demo-wc-1002", None), adt2)
    assert not card.has_case
    wl = Worklist()
    assert wl.sync(card)["skipped"] == 1
    assert wl.items(include_closed=True) == []


def test_4_omitted_encounter_uses_the_real_id(demo_source, adt2):
    """fail_probe scenario F."""
    card = build_card(gather(demo_source, "demo-wc-1002", None), adt2)
    assert card.case_key == "Patient/demo-wc-1002|Encounter/demo-enc-1002"


# ---- 5. ADT mismatch ----------------------------------------------------------------------


def test_5_adt_mismatch_does_not_resolve_adt_items(demo_source, casey, adt1):
    """fail_probe scenario D."""
    wl = Worklist()
    wl.sync(casey)
    mismatch = build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), adt1)
    assert "ADT_PATIENT_MISMATCH" in codes(mismatch)
    counts = wl.sync(mismatch)
    assert counts["resolved"] == 0
    st = statuses(wl)
    assert st["JOB_RELATED_NOT_FLAGGED"] == "open"
    assert st["EMPLOYER_MISSING"] == "open"
    assert st["ADT_PATIENT_MISMATCH"] == "open"


# ---- 6. audited severity/message changes --------------------------------------------------


def test_6_severity_change_is_audited_and_bumps_updated_at(casey):
    """fail_probe scenario E."""
    ticks = iter(range(100))
    wl = Worklist(clock=lambda: f"2026-11-24T12:00:{next(ticks):02d}+00:00")
    wl.sync(casey)
    before = next(i for i in wl.items() if i.gap_code == "OCCUPATION_MISSING")
    changed = dataclasses.replace(casey, gaps=[dataclasses.replace(g) for g in casey.gaps])
    for g in changed.gaps:
        if g.code == "OCCUPATION_MISSING":
            g.severity, g.message, g.fix_hint = "high", "changed", "new hint"
    assert wl.sync(changed)["changed"] == 1
    after = wl.get(before.id)
    assert (after.severity, after.message, after.fix_hint) == ("high", "changed", "new hint")
    assert after.updated_at > before.updated_at
    last = wl.audit()[-1]
    assert last["action"] == "changed" and "severity: 'low' -> 'high'" in last["detail"]


def test_6_reopen_refreshes_fix_hint(casey):
    wl = Worklist()
    wl.sync(casey)
    item = next(i for i in wl.items() if i.gap_code == "OCCUPATION_MISSING")
    wl.update(item.id, status="dismissed", note="x")
    changed = dataclasses.replace(casey, gaps=[dataclasses.replace(g) for g in casey.gaps])
    for g in changed.gaps:
        if g.code == "OCCUPATION_MISSING":
            g.message, g.fix_hint = "different text", "fresh hint"
    wl.sync(changed)
    assert wl.get(item.id).fix_hint == "fresh hint"


# ---- 7. WC IN1 selection, member id is not a claim # --------------------------------------


def test_7_picks_the_wc_in1_not_the_first():
    msg = "\r".join(
        [
            MSH,
            "PID|1||1",
            "IN1|1|BCBS01^Blue Plan|B1|Blue Health" + "|" * 32 + "MEMBER123",
            "IN2|||E1^Wrong Employer",
            "IN1|2|WC01^Workers Comp|W1|State Fund WC" + "|" * 11 + "WC" + "|" * 21 + "CLM-9",
            "IN2|||E2^Right Employer",
        ]
    )
    d = parse_adt(msg)
    assert (d.carrier.value, d.carrier.source) == ("State Fund WC", "IN1[2]-4")
    assert d.claim_number.value == "CLM-9"
    assert d.employer.value == "Right Employer"
    assert d.wc_in1 == "marked"


def test_7_several_unmarked_in1_give_nothing():
    msg = "\r".join([MSH, "PID|1||1", "IN1|1|A|A|Plan A", "IN1|2|B|B|Plan B"])
    d = parse_adt(msg)
    assert d.carrier is None and d.claim_number is None
    assert any("none marked as WC" in w for w in d.warnings)


def test_7_single_unmarked_in1_is_used_but_unconfirmed(demo_source, complete_a04):
    adt = parse_adt(complete_a04.replace("WC001^Workers Comp", "P01^Plan").replace("||WC|", "|||"))
    assert adt.wc_in1 == "assumed" and not adt.carrier.confirmed
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), adt)
    assert "CARRIER_UNCONFIRMED" in codes(card)


def test_7_coverage_member_id_is_unconfirmed_claim_number(demo_source):
    """fail_probe scenario C: no ADT, claim # only from the WC Coverage member id."""
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), None)
    assert "CLAIM_NUMBER_MISSING" not in codes(card)
    gap = next(g for g in card.gaps if g.code == "CLAIM_NUMBER_UNCONFIRMED")
    assert gap.severity == "medium"


# ---- 8. employer fallback confidence ------------------------------------------------------


def test_8_guarantor_employer_is_unconfirmed(demo_source, complete_a04):
    adt = parse_adt(complete_a04.replace("IN2|||EMP0042^Northwind Example Logistics\n", ""))
    assert adt.employer.source == "GT1-51 (guarantor)"
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), adt)
    assert "EMPLOYER_UNCONFIRMED" in codes(card)
    assert "EMPLOYER_MISSING" not in codes(card)


def test_8_custom_attribute_beats_guarantor_employer(demo_data, complete_a04):
    data = json.loads(json.dumps(demo_data))
    enc = next(r for r in data["resources"] if r["id"] == "demo-enc-1001")
    enc["extension"].append(
        {
            "url": "https://fhir-ehr.cerner.com/r4/StructureDefinition/custom-attribute",
            "extension": [
                {"url": "custom-attribute-name", "valueString": "WC Employer"},
                {"url": "custom-attribute-value", "valueString": "Attr Employer Inc"},
            ],
        }
    )
    adt = parse_adt(complete_a04.replace("IN2|||EMP0042^Northwind Example Logistics\n", ""))
    card = build_card(gather(FixtureSource(data), "demo-wc-1001", "demo-enc-1001"), adt)
    assert card.essentials["employer"].value == "Attr Employer Inc"
    assert "EMPLOYER_UNCONFIRMED" not in codes(card)


# ---- 9. NOT_WC_VISIT / WC_STATUS_UNCLEAR --------------------------------------------------


def _without(data, rtype):
    return FixtureSource(
        {"resources": [r for r in data["resources"] if r["resourceType"] != rtype]}
    )


def _commercial(a04: str) -> str:
    """Same message, registered as a commercial (non-WC) visit."""
    return a04.replace("|WC|", "|COMM|").replace("WC001^Workers Comp", "P01^Health Plan")


def test_9_not_wc_visit(demo_data, incomplete_a04, casey):
    adt = parse_adt(_commercial(incomplete_a04).split("ACC|")[0])
    card = build_card(
        gather(_without(demo_data, "Condition"), "demo-wc-1002", "demo-enc-1002"), adt
    )
    assert card.wc_status == "not_wc"
    assert card.status == "NOT A WC VISIT"
    assert (
        codes(card) & {"EMPLOYER_MISSING", "WC_COVERAGE_MISSING", "CLAIM_NUMBER_MISSING"} == set()
    )
    assert "NOT_WC_VISIT" in codes(card)
    wl = Worklist()
    wl.sync(casey)
    assert wl.sync(card)["resolved"] > 0  # a decided non-WC visit closes the WC items


def test_9_acc5_n_without_other_signals_is_not_wc(demo_source, incomplete_a04):
    adt = parse_adt(_commercial(incomplete_a04).replace("ACC|20261120|", "ACC|"))
    adt = dataclasses.replace(adt, job_related="N")
    card = build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), adt)
    assert card.wc_status == "not_wc"


def test_9_injury_without_wc_signal_is_unclear(demo_source, casey):
    """sync_probe: patient 1002 without its ADT has only an injury diagnosis."""
    card = build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), None)
    assert card.wc_status == "unclear"
    assert "WC_STATUS_UNCLEAR" in codes(card)
    assert "EMPLOYER_MISSING" not in codes(card)
    wl = Worklist()
    wl.sync(casey)
    assert wl.sync(card)["resolved"] == 0  # unclear: WC items left alone


# ---- 10. ACC-5 = N on a WC visit is blocking -----------------------------------------------


def test_10_job_related_conflict_is_high(demo_source, complete_a04):
    adt = parse_adt(complete_a04.replace("|||Y|||", "|||N|||"))
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), adt)
    gap = next(g for g in card.gaps if g.code == "JOB_RELATED_CONFLICT")
    assert gap.severity == "high"
    assert not card.ready


# ---- 11. local dates, not UTC slices ------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-21T02:15:00Z", "2026-11-20"),
        ("2026-11-21T00:00:00.000Z", "2026-11-20"),
        ("2026-11-17T13:30:00Z", "2026-11-17"),
        ("2026-11-21T00:00:00-05:00", "2026-11-21"),
        ("2026-11-21T00:00:00", "2026-11-21"),  # no offset: already local
        ("2026-11-21", "2026-11-21"),
        ("2026-11", ""),
        ("not a date", ""),
    ],
)
def test_11_fhir_local_date(value, expected):
    assert fhir_local_date(value, NY) == expected


def test_11_hl7_offset_converted_to_site_time():
    assert hl7_local_date("20261121013000+0000", NY) == "2026-11-20"
    assert hl7_local_date("20261121013000", NY) == "2026-11-21"  # no offset: local as sent
    assert hl7_local_date("20261121", NY) == "2026-11-21"


def test_11_demo_has_no_false_injury_date_conflict(casey):
    assert "INJURY_DATE_CONFLICT" not in codes(casey)
    assert casey.fhir.injury_conditions[0].onset == "2026-11-20"


def test_11_real_conflict_still_found(demo_source, incomplete_a04):
    adt = parse_adt(incomplete_a04.replace("ACC|20261120|", "ACC|20261118|"))
    card = build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), adt)
    assert "INJURY_DATE_CONFLICT" in codes(card)


def test_11_site_tz_option(demo_data):
    """In UTC the 02:15Z attribute is Nov 21, which does conflict with ACC-1 Nov 20."""
    tz = ZoneInfo("UTC")
    adt = parse_adt((ROOT / "examples/messages/a04_wc_incomplete.hl7").read_text())
    card = build_card(
        gather(FixtureSource(demo_data), "demo-wc-1002", "demo-enc-1002", tz), adt, tz=tz
    )
    assert "INJURY_DATE_CONFLICT" in codes(card)


# ---- 13. README quick start for Windows beginners ------------------------------------------


def test_13_readme_quick_start_covers_activation_and_uv_run():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in (".venv\\Scripts\\Activate.ps1", ".venv\\Scripts\\activate.bat", "uv run"):
        assert needle in text


# ---- lows ---------------------------------------------------------------------------------


def test_low14_demo_flag_accepts_format_json(capsys):
    assert cli.main(["--demo", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["cards"]


def test_low15_help_lists_all_default_wc_classes(capsys):
    with pytest.raises(SystemExit):
        cli.main(["card", "--help"])
    assert "WORKERS COMP" in capsys.readouterr().out


def test_low16_mrn_match_with_different_demographics_is_flagged(demo_source, adt2):
    """fail_probe scenario G."""
    adt = dataclasses.replace(adt2, patient_name="Other, Person", birth_date="1950-01-01")
    card = build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), adt)
    assert "ADT_DEMOGRAPHICS_DIFFER" in codes(card)


def test_low17_claim_status_attribute_is_not_a_claim_number(demo_data):
    data = json.loads(json.dumps(demo_data))
    enc = next(r for r in data["resources"] if r["id"] == "demo-enc-1002")
    enc["extension"].append(
        {
            "url": "https://fhir-ehr.cerner.com/r4/StructureDefinition/custom-attribute",
            "extension": [
                {"url": "custom-attribute-name", "valueString": "Claim Status"},
                {"url": "custom-attribute-value", "valueString": "Pending"},
            ],
        }
    )
    adt = parse_adt((ROOT / "examples/messages/a04_wc_incomplete.hl7").read_text())
    card = build_card(gather(FixtureSource(data), "demo-wc-1002", "demo-enc-1002"), adt)
    assert card.essentials["claim_number"] is None


def test_low19_update_error_returns_1_and_closes(tmp_path, capsys):
    db = str(tmp_path / "x.db")
    assert cli.main(["update", "5", "--status", "done", "--db", db]) == 1
    assert "no work item" in capsys.readouterr().err


def test_low21_synthetic_messages_are_test_messages():
    for path in (ROOT / "examples/messages").glob("*.hl7"):
        assert parse_adt(path.read_text()).control_id
        assert path.read_text().splitlines()[0].split("|")[10] == "T"
