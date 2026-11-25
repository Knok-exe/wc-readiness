"""Regression tests for Cipher's final code review of 8cb7fe5 (CIPHER-CODE-REVIEW.md, section 5).

Test names carry the review item (p0_1, p1_3, p2_7, ...). Probes p01-p17 are ported here
where practical, so they run offline in CI.
"""

import copy
import os
import sqlite3
import sys

import pytest

from wc_readiness import cli
from wc_readiness.adt import parse_adt
from wc_readiness.card import build_card
from wc_readiness.fhir_client import FhirClient, FhirError, FixtureSource
from wc_readiness.fhir_facts import ContextMismatch, gather
from wc_readiness.render import render_card, render_worklist
from wc_readiness.worklist import Worklist

P1, E1, P2, E2 = "demo-wc-1001", "demo-enc-1001", "demo-wc-1002", "demo-enc-1002"


def seg(text: str, name: str, idx: int, value: str) -> str:
    """Set field `idx` of every `name` segment (Cipher's common.seg)."""
    out = []
    for line in text.replace("\r", "\n").split("\n"):
        f = line.split("|")
        if f and f[0] == name:
            f += [""] * (idx + 1 - len(f))
            f[idx] = value
        out.append("|".join(f))
    return "\r".join(x for x in out if x)


def without_seg(text: str, name: str) -> str:
    return "\r".join(x for x in text.replace("\r", "\n").split("\n") if x and x[:3] != name)


def res(data, rtype, rid):
    return next(r for r in data["resources"] if r["resourceType"] == rtype and r["id"] == rid)


def make(data, p, e, adt_text=None, **kw):
    return build_card(
        gather(FixtureSource(data), p, e), parse_adt(adt_text) if adt_text else None, **kw
    )


def codes(card):
    return {g.code for g in card.gaps}


def state(wl):
    return [(i.gap_code, i.status, i.flag) for i in wl.items(include_closed=True)]


class Resp:
    def __init__(self, body, code=200):
        self._b, self.status_code, self.headers = body, code, {}

    def json(self):
        if isinstance(self._b, Exception):
            raise self._b
        return self._b


class Sess:
    """Fake requests session that records URLs and serves one body (or a function of the URL)."""

    def __init__(self, body):
        self.body, self.urls, self.headers = body, [], {}

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        return Resp(self.body(url) if callable(self.body) else self.body)


@pytest.fixture
def offline(monkeypatch, demo_data):
    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: FixtureSource(demo_data))
    return demo_data


# ---- P0-1: Encounter / Coverage of another patient ------------------------------------------


def test_p0_1_encounter_of_other_patient_is_refused(demo_data):
    with pytest.raises(ContextMismatch, match="belongs to Patient/demo-wc-1001"):
        gather(FixtureSource(demo_data), P2, E1)


def test_p0_1_cli_exits_1_and_writes_nothing(offline, tmp_path, capsys):
    db = tmp_path / "wl.db"
    assert cli.main(["card", "--patient", P2, "--encounter", E1, "--db", str(db)]) == 1
    out = capsys.readouterr()
    assert "belongs to Patient/demo-wc-1001" in out.err
    assert "demo-cov-1001-wc" not in out.out + out.err
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_p0_1_absolute_subject_reference_is_accepted(demo_data):
    data = copy.deepcopy(demo_data)
    res(data, "Encounter", E1)["subject"] = {"reference": "https://x/r4/Patient/demo-wc-1001"}
    assert gather(FixtureSource(data), P1, E1).encounter_read


def test_p0_1_coverage_of_other_patient_is_dropped(demo_data):
    data = copy.deepcopy(demo_data)
    cov = res(data, "Coverage", "demo-cov-1001-wc")
    cov["beneficiary"] = {"reference": "Patient/someone-else"}
    cov["subscriber"] = cov["beneficiary"]
    card = make(data, P1, E1)
    assert "demo-cov-1001-wc" not in [c.id for c in card.fhir.coverages]
    assert card.fhir.coverage_other_patient == ["demo-cov-1001-wc"]
    assert "COVERAGE_OTHER_PATIENT" in codes(card)


# ---- P0-2: runs without an ADT can't undo decisions -----------------------------------------


def test_p0_2_p11_no_adt_rerun_opens_nothing(demo_data, incomplete_a04):
    wl = Worklist()
    assert make(demo_data, P2, E2, incomplete_a04).wc_status == "wc"
    wl.sync(make(demo_data, P2, E2, incomplete_a04))
    before = state(wl)
    no_adt = make(demo_data, P2, E2)
    assert no_adt.wc_status == "unclear"
    assert next(g for g in no_adt.gaps if g.code == "WC_STATUS_UNCLEAR").provisional
    counts = wl.sync(no_adt)
    assert counts["opened"] == 0 and state(wl) == before


def test_p0_2_p14a_dismissed_unclear_survives_no_adt_run(demo_data, incomplete_a04):
    adt = seg(seg(incomplete_a04, "PV1", 20, "SP"), "IN1", 2, "PLAN01^Commercial")
    wl = Worklist()
    wl.sync(make(demo_data, P2, E2, adt))
    item = next(i for i in wl.items() if i.gap_code == "WC_STATUS_UNCLEAR")
    wl.update(item.id, status="dismissed", note="patient says not at work")
    assert wl.sync(make(demo_data, P2, E2))["reopened"] == 0
    assert wl.get(item.id).status == "dismissed"
    assert wl.sync(make(demo_data, P2, E2, adt))["reopened"] == 0
    assert wl.get(item.id).status == "dismissed"


def test_p0_2_p14b_dismissed_conflict_survives_no_adt_then_adt(demo_data, complete_a04):
    data = copy.deepcopy(demo_data)
    res(data, "Condition", "demo-dx-1001")["onsetDateTime"] = "2026-11-10"
    wl = Worklist()
    wl.sync(make(data, P1, E1, complete_a04))
    item = next(i for i in wl.items() if i.gap_code == "INJURY_DATE_CONFLICT")
    wl.update(item.id, status="dismissed", note="onset is symptom start, ACC-1 is right")
    counts = wl.sync(make(data, P1, E1))
    assert counts["opened"] == 0 and counts["reopened"] == 0
    assert wl.sync(make(data, P1, E1, complete_a04))["reopened"] == 0
    assert wl.get(item.id).status == "dismissed"
    assert [i.gap_code for i in wl.items(include_closed=True)] == ["INJURY_DATE_CONFLICT"]


def test_p0_2_p17_ready_case_rerun_without_adt_changes_nothing(demo_data, complete_a04):
    wl = Worklist()
    assert make(demo_data, P1, E1, complete_a04).status == "READY"
    wl.sync(make(demo_data, P1, E1, complete_a04))
    no_adt = make(demo_data, P1, E1)
    assert {"EMPLOYER_MISSING", "CLAIM_NUMBER_UNCONFIRMED"} <= codes(no_adt)
    counts = wl.sync(no_adt)
    assert counts["opened"] == 0 and wl.items(include_closed=True) == []
    assert "provisional" in render_card(no_adt)


def test_p0_2_case_without_any_adt_still_files_items(demo_data):
    """A FHIR-only workflow (never an ADT) keeps today's behavior."""
    wl = Worklist()
    counts = wl.sync(make(demo_data, P2, E2))
    assert counts["opened"] == 1
    assert [i.gap_code for i in wl.items()] == ["WC_STATUS_UNCLEAR"]


# ---- P1-1: site settings persist ---------------------------------------------------------------


def _site_w_adt(tmp_path, incomplete_a04):
    """probe5 B2: site WC class 'W', IN1 not marked WC, no ACC segment."""
    text = without_seg(seg(incomplete_a04, "PV1", 20, "W"), "ACC")
    text = text.replace("WC001^Workers Comp", "P01^Health Plan")
    path = tmp_path / "w.hl7"
    path.write_text(text)
    return str(path)


def test_p1_1_rerun_without_wc_class_uses_stored_value(offline, tmp_path, incomplete_a04, capsys):
    db, adt = str(tmp_path / "wl.db"), _site_w_adt(tmp_path, incomplete_a04)
    base = ["card", "--patient", P2, "--encounter", E2, "--adt", adt, "--db", db]
    assert cli.main([*base, "--wc-class", "W"]) == 2
    opened = len(Worklist(db).items())
    assert opened > 0
    capsys.readouterr()
    assert cli.main([*base, "--format", "json"]) == 2  # not 3 (not a WC visit)
    assert '"wc_status": "wc"' in capsys.readouterr().out
    with Worklist(db) as wl:
        assert len(wl.items()) == opened
        assert not [r for r in wl.audit() if r["action"] == "resolved"]


def test_p1_1_different_wc_class_needs_reconfigure(offline, tmp_path, incomplete_a04, capsys):
    db, adt = str(tmp_path / "wl.db"), _site_w_adt(tmp_path, incomplete_a04)
    base = ["card", "--patient", P2, "--encounter", E2, "--adt", adt, "--db", db]
    assert cli.main([*base, "--wc-class", "W"]) == 2
    assert cli.main([*base, "--wc-class", "X"]) == 1
    assert "--reconfigure" in capsys.readouterr().err


def test_p1_1_p08_site_tz_change_refused_then_audited(offline, tmp_path, capsys):
    db = str(tmp_path / "wl.db")
    base = ["card", "--patient", P1, "--encounter", E1, "--db", db]
    assert cli.main(base) in (0, 2)  # stores America/New_York
    assert cli.main([*base, "--site-tz", "UTC"]) == 1
    assert "America/New_York" in capsys.readouterr().err
    assert cli.main([*base, "--site-tz", "UTC", "--reconfigure"]) in (0, 2)
    with Worklist(db) as wl:
        assert wl.setting("site_tz") == "UTC"
        assert any(
            r["action"] == "settings" and "UTC" in r["detail"] and r["item_id"] is None
            for r in wl.audit()
        )


# ---- P1-2: only active, in-date WC coverage counts -------------------------------------------


@pytest.mark.parametrize("status", ["cancelled", "entered-in-error", "draft"])
def test_p1_2_p02_inactive_wc_coverage_doesnt_count(demo_data, complete_a04, status):
    data = copy.deepcopy(demo_data)
    res(data, "Coverage", "demo-cov-1001-wc")["status"] = status
    card = make(data, P1, E1, complete_a04)
    assert card.fhir.wc_coverages == []
    assert "WC_COVERAGE_MISSING" in codes(card) and "WC_COVERAGE_INACTIVE" in codes(card)
    assert not card.ready


def test_p1_2_p03_old_claim_coverage_is_not_a_wc_signal(demo_data, complete_a04):
    data = copy.deepcopy(demo_data)
    cov = res(data, "Coverage", "demo-cov-1001-wc")
    cov["period"] = {"start": "2019-01-01", "end": "2019-06-30"}
    cov["extension"] = [
        e for e in cov.get("extension", []) if not e.get("url", "").endswith("coverage-encounter")
    ]
    adt = seg(seg(complete_a04, "PV1", 20, "SP"), "IN1", 2, "PLAN01^Commercial")
    adt = seg(seg(adt, "IN1", 15, "COM"), "ACC", 5, "N")
    card = make(data, P1, E1, adt)
    assert card.wc_status == "not_wc"
    assert not any(s.startswith("Coverage.type") for s in card.wc_signals)
    assert "WC_COVERAGE_INACTIVE" in codes(card)


def test_p1_2_encounter_level_coverage_is_preferred(demo_data):
    data = copy.deepcopy(demo_data)
    enc_cov = res(data, "Coverage", "demo-cov-1001-wc")
    pat_cov = copy.deepcopy(enc_cov)
    pat_cov["id"] = "demo-cov-1001-wc-old"
    pat_cov["payor"] = [{"display": "Other WC Carrier"}]
    pat_cov["extension"] = [
        e
        for e in pat_cov.get("extension", [])
        if not e.get("url", "").endswith("coverage-encounter")
    ]
    data["resources"].insert(0, pat_cov)  # patient-level one comes first from the server
    card = make(data, P1, E1)
    assert card.fhir.wc_coverages[0].id == "demo-cov-1001-wc"
    assert card.essentials["carrier"].source == "Coverage/demo-cov-1001-wc payor"


# ---- P1-3: MRN with a different name AND birth date ------------------------------------------


def test_p1_3_p04_mrn_match_with_other_person_is_a_mismatch(
    demo_data, incomplete_a04, complete_a04
):
    wl = Worklist()
    wl.sync(make(demo_data, P2, E2, incomplete_a04))
    other = seg(complete_a04, "PID", 3, "700200^^^EXAMPLEHOSP^MR")
    other = seg(seg(other, "PID", 5, "Someone^Else"), "PID", 7, "19500101")
    card = make(demo_data, P2, E2, other)
    assert not card.adt_matched and "ADT_PATIENT_MISMATCH" in codes(card)
    counts = wl.sync(card)
    assert counts["resolved"] == 0 and counts["opened"] == 1


# ---- P1-4: FHIR ids are validated and quoted ---------------------------------------------------


def test_p1_4_p07_bad_patient_id_is_an_argparse_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["card", "--patient", "1/../../Encounter/2", "--no-worklist"])
    assert exc.value.code == 2
    assert "not a valid FHIR id" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["1?x", "1/../Encounter/2", "123#frag", "a b", "..", ""])
def test_p1_4_client_refuses_bad_ids_before_any_request(bad):
    s = Sess({"resourceType": "Patient", "id": "1"})
    with pytest.raises(ValueError):
        FhirClient("https://x/r4", session=s).read("Patient", bad)
    assert s.urls == []


def test_p1_4_read_checks_the_returned_resource_type():
    s = Sess({"resourceType": "Encounter", "id": "1"})
    with pytest.raises(FhirError, match="got a Encounter"):
        FhirClient("https://x/r4", session=s).read("Patient", "1")


# ---- P1-5: next links must stay on the same server ---------------------------------------------


@pytest.mark.parametrize(
    "base, next_url",
    [
        ("https://fhir.example.com", "https://fhir.example.com.attacker.net/steal?x=1"),
        ("https://fhir.example.com/r4", "https://fhir.example.com/r4x/Coverage?page=2"),
        ("https://fhir.example.com/r4", "http://fhir.example.com/r4/Coverage?page=2"),
        ("https://fhir.example.com/r4", "https://fhir.example.com:8443/r4/Coverage?page=2"),
    ],
)
def test_p1_5_p05_lookalike_next_link_is_not_followed(base, next_url):
    def body(url):
        if url == next_url:
            return {"resourceType": "Bundle", "entry": []}
        return {"resourceType": "Bundle", "link": [{"relation": "next", "url": next_url}]}

    s = Sess(body)
    with pytest.raises(FhirError, match="leaves the server"):
        FhirClient(base, session=s).search("Coverage", {"patient": "1"})
    assert len(s.urls) == 1


def test_p1_5_same_server_next_link_is_followed():
    def body(url):
        if "page=2" in url:
            return {
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Coverage", "id": "b"}}],
            }
        return {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Coverage", "id": "a"}}],
            "link": [{"relation": "next", "url": "https://FHIR.example.com/r4/Coverage?page=2"}],
        }

    rows = FhirClient("https://fhir.example.com/r4", session=Sess(body)).search("Coverage", {})
    assert [r["id"] for r in rows] == ["a", "b"]


# ---- P2 ------------------------------------------------------------------------------------------


def test_p2_1_p09_truncated_search_makes_rules_unchecked():
    def body(url):
        page = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
        if "/Patient/" in url:
            return {"resourceType": "Patient", "id": "1"}
        if "/Encounter/" in url:
            return {"resourceType": "Encounter", "id": "E1", "subject": {"reference": "Patient/1"}}
        if "/Condition" not in url:
            return {"resourceType": "Bundle", "entry": []}
        return {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Condition", "id": f"c{page}"}}],
            "link": [{"relation": "next", "url": f"https://x/r4/Condition?page={page + 1}"}],
        }

    facts = gather(FhirClient("https://x/r4", session=Sess(body)), "1", "E1")
    assert "truncated after 5 pages" in facts.errors["Condition"]
    card = build_card(facts)
    assert "INJURY_DX_MISSING" not in codes(card)


@pytest.mark.parametrize("body", [[], "oops", ValueError("not json"), {"no": "resourceType"}])
def test_p2_2_p06_non_fhir_bodies_are_read_errors(body):
    facts = gather(FhirClient("https://x", session=Sess(body)), "1", "2")
    assert {"Patient", "Encounter", "Coverage", "Condition"} <= set(facts.errors)
    assert not facts.encounter_read


@pytest.mark.parametrize(
    "code, injury",
    [
        ("S93.401A", True),
        ("T51.0X1A", True),  # toxic exposure: can be a work injury
        ("T79.4XXA", True),
        ("W19.XXXA", True),
        ("Y38.0", True),
        ("T78.40XA", False),  # allergy
        ("T81.4XXA", False),  # complication of care
        ("Y63.2", False),  # misadventure during care
        ("Y93.01", False),
        ("I10", False),
    ],
)
def test_p2_3_injury_code_ranges(code, injury):
    from wc_readiness.fhir_facts import _is_injury_code

    assert _is_injury_code({"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": code}) is injury


def test_p2_3_p13_allergy_is_not_in_the_conflict(demo_data, complete_a04):
    data = copy.deepcopy(demo_data)
    dx = copy.deepcopy(res(data, "Condition", "demo-dx-1001"))
    dx["id"] = "demo-dx-1001-w"
    dx["code"] = {
        "coding": [
            {
                "system": "http://hl7.org/fhir/sid/icd-10-cm",
                "code": "T78.40XA",
                "display": "Allergy, unspecified",
            }
        ]
    }
    dx["onsetDateTime"] = "2025-03-02"
    data["resources"].append(dx)
    card = make(data, P1, E1, complete_a04)
    assert "INJURY_DATE_CONFLICT" not in codes(card)
    assert [c.id for c in card.fhir.injury_conditions] == ["demo-dx-1001"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_p2_4_p10_new_db_is_owner_only(tmp_path):
    db = tmp_path / "demo.db"
    assert cli.main(["--demo", "--db", str(db), "--format", "json"]) == 0
    assert os.stat(db).st_mode & 0o777 == 0o600


def test_p2_5_p10_mistyped_db_is_an_error_not_a_new_list(tmp_path, capsys):
    db = tmp_path / "tpyo.db"
    for cmd in (["worklist"], ["audit"], ["update", "1", "--status", "done"]):
        assert cli.main([*cmd, "--db", str(db)]) == 1
        assert "cannot open worklist DB" in capsys.readouterr().err
    assert not db.exists()


def test_p2_5_p15_bad_db_files_exit_1_without_traceback(tmp_path, capsys):
    bad = tmp_path / "notadb.db"
    bad.write_text("hello, not sqlite")
    folder = tmp_path / "adir"
    folder.mkdir()
    for db in (bad, folder):
        for cmd in (["worklist"], ["--demo"]):
            assert cli.main([*cmd, "--db", str(db)]) == 1
            assert "cannot open worklist DB" in capsys.readouterr().err


def test_p2_6_pid3_repetition_typed_mr_is_used(demo_data, incomplete_a04):
    adt = seg(incomplete_a04, "PID", 3, "C123^^^X^CMRN~700200^^^EXAMPLEHOSP^MR")
    assert parse_adt(adt).mrn == "700200"
    card = make(demo_data, P2, E2, adt)
    assert card.adt_matched and "matched on MRN 700200" in card.patient_match


def test_p2_7_control_characters_are_not_printed(demo_data, incomplete_a04):
    adt = seg(incomplete_a04, "ACC", 8, "Fell\x1b]0;pwned\x07 off ladder\x9b")
    card = make(demo_data, P2, E2, adt)
    assert "\x1b]0;pwned\x07" in card.adt.accident_description  # data kept as-is
    text = render_card(card)
    assert not any(ch in text for ch in "\x1b\x07\x9b")
    assert "Fell?]0;pwned? off ladder?" in text


def test_p2_7_worklist_and_audit_output_are_cleaned(tmp_path, capsys):
    db = str(tmp_path / "wl.db")
    assert cli.main(["--demo", "--db", db, "--format", "json"]) == 0
    capsys.readouterr()
    assert cli.main(["update", "1", "--owner", "Eve\x1b[2J", "--db", db]) == 0
    assert cli.main(["audit", "--db", db]) == 0
    assert "\x1b" not in capsys.readouterr().out
    with Worklist(db) as wl:
        assert "\x1b" not in render_worklist(wl.items())


def test_p2_8_p12_done_again_acknowledgment_sticks(demo_data, incomplete_a04):
    wl = Worklist()
    card = make(demo_data, P2, E2, incomplete_a04)
    wl.sync(card)
    item = next(i for i in wl.items() if i.gap_code == "EMPLOYER_MISSING")
    wl.update(item.id, status="done", note="fixed in reg")
    wl.sync(card)
    assert wl.get(item.id).flag == "still_present"
    wl.update(item.id, status="done")
    assert wl.get(item.id).flag == ""
    wl.sync(card)
    assert wl.get(item.id).flag == ""  # acknowledged: stays off while the data is the same
    # The gap's data changes: the acknowledgment no longer covers it (the item reopens).
    claim = next(i for i in wl.items(include_closed=True) if i.gap_code == "CLAIM_NUMBER_MISSING")
    wl.update(claim.id, status="done")
    wl.sync(card)
    wl.update(claim.id, status="done")
    changed = copy.deepcopy(card)
    for g in changed.gaps:
        if g.code == "CLAIM_NUMBER_MISSING":
            g.key = "different data"
    wl.sync(changed)
    assert wl.get(claim.id).status == "open"
    wl.update(claim.id, status="done")
    wl.sync(changed)
    assert wl.get(claim.id).flag == "still_present"  # the old acknowledgment was cleared


def test_p2_8_in_progress_again_acknowledges_not_wc_flag(demo_data, incomplete_a04):
    wl = Worklist()
    wc = make(demo_data, P2, E2, incomplete_a04)
    wl.sync(wc)
    item = next(i for i in wl.items() if i.gap_code == "EMPLOYER_MISSING")
    wl.update(item.id, status="in_progress", owner="kim")
    data = copy.deepcopy(demo_data)
    data["resources"] = [r for r in data["resources"] if r["resourceType"] != "Condition"]
    not_wc_adt = without_seg(seg(incomplete_a04, "PV1", 20, "COMM"), "ACC")
    not_wc_adt = not_wc_adt.replace("WC001^Workers Comp", "P01^Health Plan")
    not_wc = make(data, P2, E2, not_wc_adt)
    assert not_wc.wc_status == "not_wc"
    wl.sync(not_wc)
    assert wl.get(item.id).flag == "not_wc_visit"
    wl.update(item.id, status="in_progress")
    assert wl.get(item.id).flag == ""
    wl.sync(not_wc)
    assert wl.get(item.id).flag == ""
    wl.sync(wc)  # WC again: the acknowledgment is forgotten
    wl.sync(not_wc)
    assert wl.get(item.id).flag == "not_wc_visit"


def test_p2_9_server_returning_another_encounter_id_keeps_the_case_key(demo_data):
    class Renamed(FixtureSource):
        def read(self, rtype, rid):
            body = super().read(rtype, rid)
            if rtype == "Encounter":
                body["id"] = "merged-123"
            return body

    facts = gather(Renamed(demo_data), P2, E2)
    card = build_card(facts)
    assert card.encounter_id == E2 and card.case_key.endswith(f"Encounter/{E2}")
    assert facts.encounter_returned_id == "merged-123"
    assert "ENCOUNTER_ID_CHANGED" in codes(card)


def test_p1_1_library_sync_refuses_a_card_built_with_other_wc_classes(
    demo_data, incomplete_a04, tmp_path
):
    """probe5 B2 at the library level: the worklist itself checks the card's site rules."""
    from wc_readiness.worklist import SettingsConflict

    adt = parse_adt(_site_w_adt_text(incomplete_a04))
    wl = Worklist()
    first = build_card(gather(FixtureSource(demo_data), P2, E2), adt, frozenset({"W"}))
    wl.sync(first)
    before = state(wl)
    forgot = build_card(gather(FixtureSource(demo_data), P2, E2), adt)
    assert forgot.wc_status != "wc"  # the card alone can't know...
    with pytest.raises(SettingsConflict):
        wl.sync(forgot)  # ...but the worklist refuses it
    assert state(wl) == before


def _site_w_adt_text(incomplete_a04):
    text = without_seg(seg(incomplete_a04, "PV1", 20, "W"), "ACC")
    return text.replace("WC001^Workers Comp", "P01^Health Plan")


def test_p0_2_old_db_cases_count_as_having_seen_an_adt(tmp_path, demo_data, incomplete_a04):
    db = tmp_path / "old.db"
    with Worklist(db) as wl:
        wl.sync(make(demo_data, P2, E2, incomplete_a04))
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE cases DROP COLUMN adt_seen")  # an 8cb7fe5-era database
    con.commit()
    con.close()
    with Worklist(db) as wl:
        before = state(wl)
        assert wl.sync(make(demo_data, P2, E2))["opened"] == 0
        assert state(wl) == before
