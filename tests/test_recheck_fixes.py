"""Regression tests for Sunny's re-check of 1b121f6 (R1 and the small follow-ups).

R1 is ported from Sunny's wc-readiness-review-scratch/recheck/probe3.py: a re-run without
--adt must never be read as "not a WC visit" and must never close work items.
"""

import dataclasses
import sqlite3

import pytest

from wc_readiness.adt import parse_adt
from wc_readiness.card import build_card
from wc_readiness.dates import fhir_local_date
from wc_readiness.fhir_client import FixtureSource
from wc_readiness.fhir_facts import gather
from wc_readiness.worklist import Worklist

P, E = "demo-wc-1002", "demo-enc-1002"


def codes(card):
    return {g.code for g in card.gaps}


def _without(data, rtype):
    return FixtureSource(
        {"resources": [r for r in data["resources"] if r["resourceType"] != rtype]}
    )


def _commercial(a04: str) -> str:
    return a04.replace("|WC|", "|COMM|").replace("WC001^Workers Comp", "P01^Health Plan")


@pytest.fixture
def casey(demo_source, incomplete_a04):
    return build_card(gather(demo_source, P, E), parse_adt(incomplete_a04))


@pytest.fixture
def not_wc(demo_data, incomplete_a04):
    """Matched ADT, PV1-20 = COMM, no ACC, no injury Conditions: positively not WC."""
    adt = parse_adt(_commercial(incomplete_a04).split("ACC|")[0])
    card = build_card(gather(_without(demo_data, "Condition"), P, E), adt)
    assert card.adt_matched and card.wc_status == "not_wc"
    return card


# ---- R1: no ADT / mismatched ADT is not "not WC" -------------------------------------------


def test_r1_probe3_rerun_without_adt_closes_nothing(demo_data, incomplete_a04):
    src = _without(demo_data, "Condition")
    wl = Worklist()
    assert wl.sync(build_card(gather(src, P, E), parse_adt(incomplete_a04)))["opened"] == 6
    rerun = build_card(gather(src, P, E), None)
    assert rerun.wc_status == "unknown"
    assert "NOT_WC_VISIT" not in codes(rerun)
    assert rerun.status.startswith("UNKNOWN")  # never falls through to READY
    counts = wl.sync(rerun)
    assert counts["resolved"] == 0 and counts["unchecked"] == 6
    assert {i.status for i in wl.items(include_closed=True)} == {"open"}
    assert [r["action"] for r in wl.audit()] == ["opened"] * 6


def test_r1_mismatched_adt_is_not_evidence(demo_data, incomplete_a04, casey):
    other = _commercial(incomplete_a04).split("ACC|")[0]
    other = other.replace(
        "PID|1||700200^^^EXAMPLEHOSP^MR||Sampleperson^Casey", "PID|1||999999^^^X^MR||Other^Pat"
    )
    card = build_card(gather(_without(demo_data, "Condition"), P, E), parse_adt(other))
    assert not card.adt_matched
    assert card.wc_status == "unknown" and "NOT_WC_VISIT" not in codes(card)
    wl = Worklist()
    wl.sync(casey)
    assert wl.sync(card)["resolved"] == 0


def test_r1_no_adt_on_a_wc_visit_doesnt_resolve_adt_items(demo_source):
    """FHIR coverage alone makes 1001 WC; without the ADT, ADT-sourced gaps can't be cleared."""
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), None)
    assert card.wc_status == "wc"
    assert {"CLAIM_NUMBER_MISSING", "INJURY_DATE_CONFLICT", "JOB_RELATED_NOT_FLAGGED"} <= (
        card.unchecked
    )


def test_r1_not_wc_needs_non_wc_financial_class(demo_data, incomplete_a04):
    adt = parse_adt(_commercial(incomplete_a04).split("ACC|")[0].replace("|COMM|", "||"))
    card = build_card(gather(_without(demo_data, "Condition"), P, E), adt)
    assert card.adt_matched and card.wc_status != "not_wc"


def test_r1_not_wc_closes_open_flags_in_progress_leaves_closed(casey, not_wc):
    wl = Worklist()
    wl.sync(casey)
    by = {i.gap_code: i for i in wl.items()}
    working = by["EMPLOYER_MISSING"].id
    done = by["CLAIM_NUMBER_MISSING"].id
    dismissed = by["WC_COVERAGE_MISSING"].id
    opened = by["JOB_RELATED_NOT_FLAGGED"].id
    wl.update(working, status="in_progress", owner="kim")
    wl.update(done, status="done")
    wl.update(dismissed, status="dismissed", note="known plan mapping")

    counts = wl.sync(not_wc)
    assert wl.get(opened).status == "resolved"
    detail = next(
        r["detail"] for r in wl.audit() if r["item_id"] == opened and r["action"] == "resolved"
    )
    assert detail.startswith("not a WC visit: Registered as a non-WC visit (PV1-20=COMM")
    assert wl.get(working).status == "in_progress" and wl.get(working).flag == "not_wc_visit"
    assert wl.get(done).status == "done" and wl.get(dismissed).status == "dismissed"
    assert counts["flagged"] == 1
    assert wl.sync(not_wc)["flagged"] == 0  # flagged once, not every run

    # Registration fixes the financial class: WC again.
    again = wl.sync(casey)
    assert wl.get(working).flag == "" and wl.get(working).status == "in_progress"
    assert wl.get(done).status == "done"  # the not-WC run didn't mark it "gone"
    assert wl.get(dismissed).status == "dismissed"
    assert wl.get(opened).status == "open" and again["reopened"] >= 1


# ---- small follow-ups ----------------------------------------------------------------------


def test_done_again_clears_still_present_flag(casey):
    wl = Worklist()
    wl.sync(casey)
    item = next(i for i in wl.items() if i.gap_code == "OCCUPATION_MISSING")
    wl.update(item.id, status="done")
    wl.sync(casey)
    assert wl.get(item.id).flag == "still_present"
    wl.update(item.id, status="done", note="occupation is on paper chart")
    assert wl.get(item.id).flag == ""
    assert "cleared" in wl.audit()[-1]["detail"]
    wl.update(item.id, status="done")  # nothing to do: no extra audit row
    assert "cleared" in wl.audit()[-1]["detail"]


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-21T02:15:00.1Z",
        "2026-11-21T02:15:00.12Z",
        "2026-11-21T02:15:00.1234Z",
        "2026-11-21T02:15:00.123456789+00:00",
    ],
)
def test_fhir_fraction_digits_any_length(value):
    assert fhir_local_date(value) == "2026-11-20"  # 9:15 PM Nov 20 in New York, on 3.10 too


def test_dismissed_survives_rewording_but_not_a_data_change(casey):
    wl = Worklist()
    wl.sync(casey)
    item = next(i for i in wl.items() if i.gap_code == "WC_COVERAGE_MISSING")
    wl.update(item.id, status="dismissed", note="x")
    reworded = dataclasses.replace(casey, gaps=[dataclasses.replace(g) for g in casey.gaps])
    for g in reworded.gaps:
        if g.code == "WC_COVERAGE_MISSING":
            g.message = "New wording in v0.2"
    assert wl.sync(reworded)["reopened"] == 0
    for g in reworded.gaps:
        if g.code == "WC_COVERAGE_MISSING":
            g.key = "fc=WORKCOMP"
    assert wl.sync(reworded)["reopened"] == 1


def test_migration_from_1b121f6_keeps_closed_items_closed(tmp_path, casey):
    db = tmp_path / "old.db"
    with Worklist(db) as wl:
        wl.sync(casey)
        total = len(wl.items())
        for i in wl.items():
            wl.update(i.id, status="dismissed", note="old decision")
    # Turn it into a 1b121f6 database: no key columns, and older wording stored.
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE work_items DROP COLUMN gap_key")
    con.execute("ALTER TABLE work_items DROP COLUMN closed_key")
    con.execute("UPDATE work_items SET message = 'old text', closed_message = 'old text'")
    con.commit()
    con.close()
    with Worklist(db) as wl:
        counts = wl.sync(casey)
        assert counts["reopened"] == 0 and counts["kept_closed"] == total
        assert {i.status for i in wl.items(include_closed=True)} == {"dismissed"}
        assert wl.sync(casey)["reopened"] == 0
