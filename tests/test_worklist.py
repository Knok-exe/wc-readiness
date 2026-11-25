import itertools
import sqlite3

import pytest

from wc_readiness.adt import parse_adt
from wc_readiness.card import build_card
from wc_readiness.fhir_facts import gather
from wc_readiness.render import render_worklist
from wc_readiness.worklist import Worklist


def fake_clock():
    counter = itertools.count()
    return lambda: f"2026-11-24T12:00:{next(counter):02d}+00:00"


@pytest.fixture
def cards(demo_source, complete_a04, incomplete_a04):
    good = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), parse_adt(complete_a04))
    bad = build_card(
        gather(demo_source, "demo-wc-1002", "demo-enc-1002"), parse_adt(incomplete_a04)
    )
    return good, bad


def test_sync_opens_one_item_per_actionable_gap(cards):
    good, bad = cards
    wl = Worklist(clock=fake_clock())
    assert wl.sync(good)["opened"] == 0  # only an info gap
    counts = wl.sync(bad)
    actionable = [g for g in bad.gaps if g.severity != "info"]
    assert counts["opened"] == len(actionable)
    items = wl.items()
    assert [i.severity for i in items][:3] == ["high", "high", "high"]
    assert all(i.status == "open" and i.patient_name == "Sampleperson, Casey" for i in items)


def test_resync_is_idempotent(cards):
    _, bad = cards
    wl = Worklist(clock=fake_clock())
    wl.sync(bad)
    counts = wl.sync(bad)
    assert counts["opened"] == 0 and counts["resolved"] == 0
    assert counts["unchanged"] == len(wl.items())


def test_fixed_gap_resolves_and_returning_gap_reopens(cards, demo_source, incomplete_a04):
    _, bad = cards
    wl = Worklist(clock=fake_clock())
    wl.sync(bad)
    fixed_adt = parse_adt(incomplete_a04.replace("|UNKNOWN", "|WC-2026-009999"))
    fixed = build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), fixed_adt)
    assert wl.sync(fixed)["resolved"] == 1
    claim = wl.items(status="resolved")[0]
    assert claim.gap_code == "CLAIM_NUMBER_MISSING"
    assert wl.sync(bad)["reopened"] == 1
    assert wl.get(claim.id).status == "open"
    actions = [r["action"] for r in wl.audit(bad.case_key) if r["item_id"] == claim.id]
    assert actions == ["opened", "resolved", "reopened"]


def test_update_changes_status_owner_and_audits(cards):
    _, bad = cards
    wl = Worklist(clock=fake_clock())
    wl.sync(bad)
    item = wl.items()[0]
    updated = wl.update(item.id, status="in_progress", owner="Knok", actor="knok")
    assert (updated.status, updated.owner) == ("in_progress", "Knok")
    last = wl.audit()[-1]
    assert last["actor"] == "knok" and "status: 'open' -> 'in_progress'" in last["detail"]
    wl.update(item.id, status="done", note="claim # from adjuster")
    assert item.id not in [i.id for i in wl.items()]
    assert item.id in [i.id for i in wl.items(include_closed=True)]


def test_update_no_change_writes_no_audit(cards):
    _, bad = cards
    wl = Worklist(clock=fake_clock())
    wl.sync(bad)
    item = wl.items()[0]
    before = len(wl.audit())
    wl.update(item.id, status="open")
    assert len(wl.audit()) == before


def test_update_rejects_bad_input(cards):
    wl = Worklist(clock=fake_clock())
    wl.sync(cards[1])
    with pytest.raises(ValueError):
        wl.update(1, status="finished")
    with pytest.raises(KeyError):
        wl.update(999, status="done")


def test_worklist_persists_to_file(tmp_path, cards):
    db = tmp_path / "wl.db"
    wl = Worklist(db)
    wl.sync(cards[1])
    n = len(wl.items())
    wl.close()
    assert len(Worklist(db).items()) == n


def test_default_clock_is_utc_iso(cards):
    wl = Worklist()
    wl.sync(cards[1])
    assert wl.items()[0].created_at.endswith("+00:00")


def test_wrong_visit_adt_leaves_items_alone(demo_source, incomplete_a04):
    wl = Worklist(clock=fake_clock())
    right = parse_adt(incomplete_a04)
    wl.sync(build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), right))
    before = {i.gap_code: i.status for i in wl.items()}
    wrong = parse_adt(incomplete_a04.replace("FIN700200B", "FIN-OTHER"))
    counts = wl.sync(build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), wrong))
    assert counts["resolved"] == 0 and counts["opened"] == 1  # only ADT_VISIT_MISMATCH
    after = {i.gap_code: i.status for i in wl.items()}
    assert after == {**before, "ADT_VISIT_MISMATCH": "open"}
    wl.sync(build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), right))
    assert "ADT_VISIT_MISMATCH" not in {i.gap_code for i in wl.items()}  # resolved


def test_dismiss_needs_a_note_in_the_same_update(cards):
    _, bad = cards
    wl = Worklist(clock=fake_clock())
    wl.sync(bad)
    item = wl.items()[0]
    wl.update(item.id, status="in_progress", note="waiting on carrier")
    with pytest.raises(ValueError, match="note"):
        wl.update(item.id, status="dismissed")  # the old note is not a reason
    assert wl.update(item.id, status="dismissed", note="not used at our site").status == (
        "dismissed"
    )


def test_audit_log_is_append_only(cards):
    _, bad = cards
    wl = Worklist(clock=fake_clock())
    wl.sync(bad)
    for sql in ("UPDATE audit_log SET actor = 'someone else'", "DELETE FROM audit_log"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            wl.conn.execute(sql)


def test_items_show_the_visit(cards):
    _, bad = cards
    wl = Worklist(clock=fake_clock())
    wl.sync(bad)
    assert {i.encounter_id for i in wl.items()} == {"demo-enc-1002"}
    assert "demo-enc-1002" in render_worklist(wl.items())
