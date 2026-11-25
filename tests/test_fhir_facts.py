import pytest

from wc_readiness.fhir_client import FhirError, FixtureSource
from wc_readiness.fhir_facts import (
    CUSTOM_ATTRIBUTE_URL,
    coverage_fact,
    custom_attributes,
    gather,
    is_wc_coverage_type,
)

SOP = "urn:oid:2.16.840.1.113883.3.221.5"


@pytest.mark.parametrize(
    "system, code, expected",
    [
        (SOP, "95", True),
        (SOP, "951", True),
        (SOP, "959", True),
        (SOP, "952", False),  # there is no 952 in SOP
        (SOP, "5", False),
        ("https://nahdo.org/sopt", "95", True),
        ("http://terminology.hl7.org/CodeSystem/v3-ActCode", "WCBPOL", True),
        ("http://terminology.hl7.org/CodeSystem/v3-ActCode", "PUBLICPOL", False),
        ("http://example.org/other", "95", False),
    ],
)
def test_wc_coverage_type(system, code, expected):
    assert is_wc_coverage_type({"coding": [{"system": system, "code": code}]}) is expected


def test_wc_coverage_type_handles_missing():
    assert is_wc_coverage_type(None) is False
    assert is_wc_coverage_type({}) is False


def test_coverage_without_type_like_oracle_self_pay():
    fact = coverage_fact(
        {"resourceType": "Coverage", "id": "c", "payor": [{"display": "SELF PAY"}]}
    )
    assert (fact.is_wc, fact.type_code, fact.payer) == (False, "", "SELF PAY")


def test_custom_attributes_in_oracle_shape():
    """Same structure as the open sandbox's Encounter 97954296 (OBSERVED by Pip)."""
    enc = {
        "extension": [
            {
                "url": CUSTOM_ATTRIBUTE_URL,
                "extension": [
                    {"url": "custom-attribute-name", "valueString": "Complete Reg?"},
                    {
                        "url": "custom-attribute-value",
                        "valueCodeableConcept": {"coding": [{"display": "No"}], "text": "No"},
                    },
                ],
            },
            {
                "url": CUSTOM_ATTRIBUTE_URL,
                "extension": [
                    {"url": "custom-attribute-name", "valueString": "MSP Complete Date"},
                    {"url": "custom-attribute-value", "valueDateTime": "2022-09-15T15:43:12Z"},
                ],
            },
            {"url": "http://example.org/other", "valueString": "ignored"},
        ]
    }
    assert custom_attributes(enc) == {
        "Complete Reg?": "No",
        "MSP Complete Date": "2022-09-15T15:43:12Z",
    }


def test_gather_complete_demo_case(demo_source):
    f = gather(demo_source, "demo-wc-1001", "demo-enc-1001")
    assert f.errors == {}
    assert f.patient_name == "Testpatient, Riley"
    assert f.mrns == ["700100"]
    assert [c.id for c in f.wc_coverages] == ["demo-cov-1001-wc"]
    assert f.wc_coverages[0].encounter_level
    assert [c.codes[0].code for c in f.injury_conditions] == ["S61.012A"]
    assert f.occupation == "Warehouse associate"
    assert f.employment_status == "Full time"
    assert f.custom_attributes["Injury Date"].startswith("2026-11-17")


def test_gather_ignores_data_absent_occupation(demo_source):
    assert gather(demo_source, "demo-wc-1002", "demo-enc-1002").occupation == ""


def test_gather_picks_latest_encounter_when_none_given(demo_data):
    extra = {
        "resourceType": "Encounter",
        "id": "older",
        "subject": {"reference": "Patient/demo-wc-1001"},
        "period": {"start": "2020-01-01T00:00:00Z"},
    }
    source = FixtureSource({"resources": [*demo_data["resources"], extra]})
    assert gather(source, "demo-wc-1001").encounter_id == "demo-enc-1001"


def test_gather_filters_problem_list_noise():
    """Oracle returned the whole problem list for Condition?encounter=; only keep encounter
    diagnoses and injuries, and drop entered-in-error."""
    p = {"reference": "Patient/p"}
    resources = [
        {"resourceType": "Patient", "id": "p", "name": [{"text": "X"}]},
        {"resourceType": "Encounter", "id": "e", "subject": p},
        {"resourceType": "Condition", "id": "sinus", "subject": p, "code": {"text": "Sinusitis"}},
        {
            "resourceType": "Condition",
            "id": "err",
            "subject": p,
            "encounter": {"reference": "Encounter/e"},
            "verificationStatus": {"coding": [{"code": "entered-in-error"}]},
        },
        {
            "resourceType": "Condition",
            "id": "back",
            "subject": p,
            "encounter": {"reference": "Encounter/e"},
            "code": {"text": "Low back strain"},
        },
    ]
    f = gather(FixtureSource({"resources": resources}), "p", "e")
    assert [c.id for c in f.conditions] == ["back"]
    assert f.injury_conditions == []  # free text only, no ICD-10 S/T code


def test_injury_problems_from_other_visits_are_counted_not_used():
    p = {"reference": "Patient/p"}
    old = {
        "resourceType": "Condition",
        "id": "old",
        "subject": p,
        "encounter": {"reference": "Encounter/other"},
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "T78.40XA"}]},
    }
    resources = [
        {"resourceType": "Patient", "id": "p"},
        {"resourceType": "Encounter", "id": "e", "subject": p},
        old,
    ]
    f = gather(FixtureSource({"resources": resources}), "p", "e")
    assert f.conditions == []
    assert f.other_injury_problems == 1


class FailingSource(FixtureSource):
    def __init__(self, data, fail):
        super().__init__(data)
        self.fail = fail

    def search(self, resource_type, params):
        if resource_type in self.fail:
            raise FhirError("HTTP 504 from server")
        return super().search(resource_type, params)


def test_one_failing_resource_does_not_sink_the_card(demo_data):
    f = gather(FailingSource(demo_data, {"Condition"}), "demo-wc-1001", "demo-enc-1001")
    assert set(f.errors) == {"Condition"}
    assert f.wc_coverages  # everything else still read


def test_unknown_patient_is_an_error_not_a_crash(demo_source):
    f = gather(demo_source, "nobody", None)
    assert "Patient" in f.errors
    assert f.encounter_id == ""
