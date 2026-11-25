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
        "OCCUPATION_MISSING",
    } <= codes(card)
    # The custom attribute is 02:15Z on Nov 21 = Nov 20 local, same as ACC-1: no conflict.
    assert "INJURY_DATE_CONFLICT" not in codes(card)
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


# ---- ADT must be this visit's, not just this patient's ----------------------------------------


def test_adt_from_another_visit_is_not_used(demo_source, complete_a04):
    """Same patient, other visit (PV1-19 isn't this Encounter's FIN): an old claim's A04 must not
    fill in this visit's claim # or employer, or make it READY."""
    old_visit = complete_a04.replace("FIN700100A", "FIN-2025-OLD")
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), parse_adt(old_visit))
    assert not card.adt_matched and card.adt_visit_mismatch
    assert "PV1-19 visit FIN-2025-OLD is not this Encounter" in card.patient_match
    gap = next(g for g in card.gaps if g.code == "ADT_VISIT_MISMATCH")
    assert gap.severity == "high"
    assert "ADT_PATIENT_MISMATCH" not in codes(card)
    assert card.essentials["claim_number"].source != "IN1-36"  # not taken from that A04
    assert not card.ready


def test_visit_number_compare_ignores_case_and_leading_zeros(demo_source, complete_a04):
    adt = complete_a04.replace("FIN700100A", "fin700100a")
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), parse_adt(adt))
    assert card.adt_matched and card.adt_visit_checked
    facts = gather(demo_source, "demo-wc-1001", "demo-enc-1001")
    facts.encounter_identifiers = ["0016137"]
    card = build_card(facts, parse_adt(complete_a04.replace("FIN700100A", "16137")))
    assert card.adt_matched


def test_adt_without_visit_number_is_used_but_flagged(demo_source, complete_a04):
    adt = complete_a04.replace("FIN700100A", "")
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), parse_adt(adt))
    assert card.adt_matched and not card.adt_visit_checked
    assert "ADT_VISIT_UNVERIFIED" in codes(card)
    assert "ADT_VISIT_MISMATCH" in card.unchecked  # can't close an earlier mismatch item
    assert card.ready  # info only


# ---- placeholders are not values ---------------------------------------------------------------


def test_placeholder_employer_and_carrier_in_adt_are_skipped(demo_source, complete_a04):
    adt = complete_a04.replace("EMP0042^Northwind Example Logistics", "^UNKNOWN").replace(
        "Example Mutual WC Insurance", "N/A"
    )
    data = parse_adt(adt)
    assert any("IN2-3 holds a placeholder ('UNKNOWN')" in w for w in data.warnings)
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), data)
    employer, carrier = card.essentials["employer"], card.essentials["carrier"]
    assert employer.value != "UNKNOWN" and not employer.source.startswith("IN2-3")
    assert carrier.value != "N/A" and carrier.source.startswith("Coverage/")


def _facts_with_attrs(demo_source, attrs):
    facts = gather(demo_source, "demo-wc-1002", "demo-enc-1002")
    facts.custom_attributes = attrs
    return facts


def test_custom_attribute_placeholders_and_non_dates_are_ignored(demo_source):
    attrs = {"Employer": "N/A", "Claim #": "Pending", "Injury Date": "unknown"}
    card = build_card(_facts_with_attrs(demo_source, attrs), None)
    assert card.essentials["employer"] is None
    assert card.essentials["claim_number"] is None
    assert card.essentials["injury_date"].source.startswith("Condition/")  # next source
    ignored = next(g for g in card.gaps if g.code == "CUSTOM_ATTRIBUTE_IGNORED")
    assert "'Employer' = 'N/A' (placeholder)" in ignored.message
    assert "'Injury Date' = 'unknown' (placeholder)" in ignored.message


def test_custom_attribute_us_date_is_read(demo_source):
    card = build_card(_facts_with_attrs(demo_source, {"Date of Injury": "11/20/2026"}), None)
    assert card.essentials["injury_date"].value == "2026-11-20"
    card = build_card(_facts_with_attrs(demo_source, {"Date of Injury": "next Tuesday"}), None)
    assert "'Date of Injury' = 'next Tuesday' (not a date)" in next(
        g.message for g in card.gaps if g.code == "CUSTOM_ATTRIBUTE_IGNORED"
    )


# ---- injury date sanity ------------------------------------------------------------------------


def test_injury_date_after_visit_blocks(demo_source, complete_a04):
    adt = complete_a04.replace("ACC|20261117083000-0500", "ACC|20261119")
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), parse_adt(adt))
    gap = next(g for g in card.gaps if g.code == "INJURY_DATE_AFTER_VISIT")
    assert gap.severity == "high"
    assert "2026-11-19 (ACC-1) is after this visit started (2026-11-17)" in gap.message
    assert not card.ready


def test_injury_date_before_or_on_visit_day_is_fine(demo_source, complete_a04):
    card = build_card(gather(demo_source, "demo-wc-1001", "demo-enc-1001"), parse_adt(complete_a04))
    assert "INJURY_DATE_AFTER_VISIT" not in codes(card)
    assert "INJURY_DATE_AFTER_VISIT" not in card.unchecked


# ---- matching and small rules ------------------------------------------------------------------


def test_name_match_ignores_middle_names_suffixes_and_punctuation(demo_source, complete_a04):
    facts = gather(demo_source, "demo-wc-1001", "demo-enc-1001")
    facts.family_name, facts.given_name = "Testpatient Jr.", "Riley"
    facts.patient_name = "Testpatient Jr., Riley Ann"
    card = build_card(facts, parse_adt(complete_a04))
    assert "ADT_DEMOGRAPHICS_DIFFER" not in codes(card)
    # Same name + DOB is enough without an MRN match, middle name or not.
    adt = complete_a04.replace("700100^^^", "999999^^^")
    card = build_card(facts, parse_adt(adt))
    assert card.patient_match.startswith("matched on name + birth date")


def test_work_status_document_detection(demo_source, complete_a04):
    facts = gather(demo_source, "demo-wc-1001", "demo-enc-1001")
    facts.documents = ["ED Workup", "Network Note", "Paperwork"]
    assert "WORK_STATUS_NOTE_MISSING" in codes(build_card(facts, parse_adt(complete_a04)))
    facts.documents = ["Work Status Note"]
    assert "WORK_STATUS_NOTE_MISSING" not in codes(build_card(facts, parse_adt(complete_a04)))
