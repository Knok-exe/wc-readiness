from wc_readiness.adt import parse_adt
from wc_readiness.card import build_card
from wc_readiness.fhir_facts import gather
from wc_readiness.render import render_card

from .test_fhir_facts import FailingSource


def codes(card):
    return {g.code for g in card.gaps}


def test_complete_case_is_ready(demo_source, complete_a04):
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), parse_adt(complete_a04))
    assert card.ready, card.gaps
    assert card.status == "READY"
    assert card.patient_match == "matched on MRN 700100"
    assert card.essentials["claim_number"].source == "IN1-36"
    assert card.essentials["employer"].value == "Northwind Example Logistics"
    assert codes(card) == {"WORK_STATUS_NOTE_MISSING"}
    assert card.case_key == "Patient/demo-wc-1001|Encounter/demo-enc-1001"


def test_incomplete_case_gaps(demo_source, incomplete_a04):
    card = build_card(
        gather(demo_source, "demo-wc-1002", "demo-enc-1002"), parse_adt(incomplete_a04)
    )
    assert not card.ready
    assert card.status == "NOT READY (3 blocking gap(s))"
    assert {
        "WC_COVERAGE_MISSING",
        "EMPLOYER_MISSING",
        "CLAIM_NUMBER_MISSING",
        "JOB_RELATED_NOT_FLAGGED",
        "INJURY_DATE_CONFLICT",
        "OCCUPATION_MISSING",
    } <= codes(card)
    wc_gap = next(g for g in card.gaps if g.code == "WC_COVERAGE_MISSING")
    assert "PV1-20=WC" in wc_gap.message
    assert [g.severity for g in card.gaps] == sorted(
        (g.severity for g in card.gaps), key=["high", "medium", "low", "info"].index
    )


def test_no_adt_falls_back_to_fhir_sources(demo_source):
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), None)
    assert "NO_ADT" in codes(card)
    assert card.essentials["injury_date"].source == "Encounter custom attribute 'Injury Date'"
    assert card.essentials["injury_date"].value == "2026-11-17"
    assert card.essentials["claim_number"].source == "Coverage/demo-cov-1001-wc member id"
    assert card.essentials["carrier"].value == "Example Mutual WC Insurance"
    assert card.essentials["employer"] is None
    assert "EMPLOYER_MISSING" in codes(card)


def test_adt_for_a_different_patient_is_not_used(demo_source, complete_a04):
    card = build_card(gather(demo_source, "demo-wc-1002", "demo-enc-1002"), parse_adt(complete_a04))
    assert "ADT_PATIENT_MISMATCH" in codes(card)
    assert card.patient_match.startswith("NOT MATCHED")
    assert card.essentials["employer"] is None  # Riley's employer must not leak onto Casey
    assert card.essentials["claim_number"] is None


def test_name_and_dob_match_when_mrn_differs(demo_source, complete_a04):
    adt = parse_adt(complete_a04.replace("700100^^^", "999999^^^"))
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), adt)
    assert card.patient_match.startswith("matched on name + birth date")
    assert card.ready


def test_job_related_n_is_a_conflict(demo_source, complete_a04):
    adt = parse_adt(complete_a04.replace("|||Y|||", "|||N|||"))
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), adt)
    assert "JOB_RELATED_CONFLICT" in codes(card)


def test_custom_financial_class(demo_source, incomplete_a04):
    adt = parse_adt(incomplete_a04.replace("|WC|", "|W|"))
    facts = gather(demo_source, "demo-wc-1002", "demo-enc-1002")
    default = build_card(facts, adt)
    site = build_card(facts, adt, wc_financial_classes={"W"})
    assert not any("financial class" in s for s in default.wc_signals)
    assert any("financial class = W" in s for s in site.wc_signals)


def test_failed_read_means_unknown_not_ready(demo_data, complete_a04):
    facts = gather(FailingSource(demo_data, {"Condition"}), "demo-wc-1001", "demo-enc-1001")
    card = build_card(facts, parse_adt(complete_a04))
    assert "INJURY_DX_MISSING" not in codes(card)  # unknown, not missing
    assert "FHIR_READ_FAILED:Condition" in codes(card)
    assert not card.ready
    assert card.status.startswith("UNKNOWN")


def test_failed_coverage_read_suppresses_coverage_gap(demo_data, complete_a04):
    facts = gather(FailingSource(demo_data, {"Coverage"}), "demo-wc-1001", "demo-enc-1001")
    assert "WC_COVERAGE_MISSING" not in codes(build_card(facts, parse_adt(complete_a04)))


def test_render_card_mentions_sources_and_gaps(demo_source, incomplete_a04):
    card = build_card(
        gather(demo_source, "demo-wc-1002", "demo-enc-1002"), parse_adt(incomplete_a04)
    )
    text = render_card(card)
    assert "NOT READY" in text
    assert "Employer       MISSING" in text
    assert "Sample Casualty TPA   [IN1-4]" in text
    assert "CLAIM_NUMBER_MISSING" in text


def test_to_dict_is_json_friendly(demo_source, complete_a04):
    import json

    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), parse_adt(complete_a04))
    data = json.loads(json.dumps(card.to_dict()))
    assert data["status"] == "READY"
    assert data["essentials"]["claim_number"]["value"] == "WC-2026-004512"
