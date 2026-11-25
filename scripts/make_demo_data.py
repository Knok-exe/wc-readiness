"""Regenerate the synthetic demo data: two HL7 v2 A04 messages + an Oracle-shaped FHIR fixture.

Everything here is invented. Names, MRNs, employers and claim numbers are fake, and the FHIR
resources are hand-built to look like what Oracle Health's R4 API returns (same extension URLs
and code systems), so the code paths are the same as against the real sandbox.

Run from the repo root:  python scripts/make_demo_data.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "src" / "wc_readiness" / "demo"
EXAMPLES = ROOT / "examples" / "messages"

SOP = "urn:oid:2.16.840.1.113883.3.221.5"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
CA_URL = "https://fhir-ehr.cerner.com/r4/StructureDefinition/custom-attribute"


def seg(name: str, fields: dict[int, str]) -> str:
    """Build a segment from {field_number: value}; empty fields in between are filled in."""
    last = max(fields)
    values = [fields.get(i, "") for i in range(1, last + 1)]
    while values and values[-1] == "":
        values.pop()
    return "|".join([name, *values])


def msh(control_id: str, when: str) -> str:
    return (
        f"MSH|^~\\&|REGADT|EXAMPLEHOSP|WCREADY|EXAMPLEHOSP|{when}||ADT^A04^ADT_A01|"
        f"{control_id}|P|2.5.1"
    )


def a04(
    *,
    control_id: str,
    when: str,
    mrn: str,
    name: str,
    dob: str,
    sex: str,
    fin: str,
    financial_class: str,
    carrier_id: str,
    carrier: str,
    plan_type: str,
    claim: str,
    employer_in2: str,
    gt1_employer: str,
    injury: str,
    job_related: str,
    description: str,
) -> str:
    segs = [
        msh(control_id, when),
        f"EVN|A04|{when}",
        seg("PID", {1: "1", 3: f"{mrn}^^^EXAMPLEHOSP^MR", 5: name, 7: dob, 8: sex}),
        seg(
            "PV1",
            {1: "1", 2: "E", 3: "ED^BAY4^^EXAMPLEHOSP", 19: fin, 20: financial_class, 44: when},
        ),
        seg(
            "IN1",
            {1: "1", 2: "WC001^Workers Comp", 3: carrier_id, 4: carrier, 15: plan_type, 36: claim},
        ),
    ]
    if employer_in2:
        segs.append(seg("IN2", {3: employer_in2}))
    segs.append(seg("GT1", {1: "1", 3: name, 16: "", 51: gt1_employer}))
    segs.append(
        seg("ACC", {1: injury, 2: "WPA^Workplace accident^L", 5: job_related, 8: description})
    )
    # Real feeds end segments with CR; files here use LF so they read nicely on GitHub.
    # The parser accepts both.
    return "\n".join(segs) + "\n"


def custom_attr(i: int, attr_id: str, name: str, value: dict) -> dict:
    return {
        "id": f"CA-{i}",
        "url": CA_URL,
        "extension": [
            {"id": attr_id, "url": "custom-attribute-name", "valueString": name},
            {"url": "custom-attribute-value", **value},
        ],
    }


def patient(pid: str, mrn: str, family: str, given: str, dob: str, gender: str) -> dict:
    return {
        "resourceType": "Patient",
        "id": pid,
        "identifier": [
            {
                "type": {
                    "coding": [
                        {"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}
                    ],
                    "text": "MRN",
                },
                "system": "urn:oid:2.16.840.1.113883.3.787.0.0",
                "value": mrn,
            }
        ],
        "name": [
            {"use": "official", "text": f"{family}, {given}", "family": family, "given": [given]}
        ],
        "gender": gender,
        "birthDate": dob,
    }


def encounter(eid: str, pid: str, fin: str, start: str, attrs: list[dict], dx: list[str]) -> dict:
    return {
        "resourceType": "Encounter",
        "id": eid,
        "extension": attrs,
        "identifier": [{"type": {"text": "FIN NBR"}, "value": fin}],
        "status": "in-progress",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "EMER",
            "display": "emergency",
        },
        "type": [{"text": "Emergency"}],
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": start},
        "diagnosis": [{"condition": {"reference": f"Condition/{c}"}} for c in dx],
    }


def coverage(
    cid: str,
    pid: str,
    code: str,
    display: str,
    text: str,
    payer: str,
    enc: str | None = None,
    member: str | None = None,
) -> dict:
    res = {
        "resourceType": "Coverage",
        "id": cid,
        "status": "active",
        "type": {"coding": [{"system": SOP, "code": code, "display": display}], "text": text},
        "subscriber": {"reference": f"Patient/{pid}"},
        "beneficiary": {"reference": f"Patient/{pid}"},
        "payor": [{"reference": f"Organization/{cid}-payer", "display": payer}],
        "class": [
            {
                "type": {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/coverage-class",
                            "code": "plan",
                        }
                    ]
                },
                "value": f"{cid}-plan",
                "name": payer,
            }
        ],
    }
    if member:
        res["subscriberId"] = member
    if enc:
        res["extension"] = [
            {
                "url": "https://fhir-ehr.cerner.com/r4/StructureDefinition/coverage-encounter",
                "valueReference": {"reference": f"Encounter/{enc}"},
            }
        ]
    return res


def condition(cid: str, pid: str, enc: str, code: str, display: str, onset: str) -> dict:
    return {
        "resourceType": "Condition",
        "id": cid,
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "verificationStatus": {"coding": [{"code": "confirmed"}]},
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                        "code": "encounter-diagnosis",
                    }
                ]
            }
        ],
        "code": {"coding": [{"system": ICD10, "code": code, "display": display}], "text": display},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{enc}"},
        "onsetDateTime": onset,
    }


def social(oid: str, pid: str, system: str, code: str, text: str, value: dict) -> dict:
    return {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "social-history",
                    }
                ]
            }
        ],
        "code": {"coding": [{"system": system, "code": code}], "text": text},
        "subject": {"reference": f"Patient/{pid}"},
        "valueCodeableConcept": value,
    }


def build() -> None:
    DEMO.mkdir(parents=True, exist_ok=True)
    EXAMPLES.mkdir(parents=True, exist_ok=True)

    complete = a04(
        control_id="WCDEMO0001",
        when="20261117101500",
        mrn="700100",
        name="Testpatient^Riley",
        dob="19880412",
        sex="F",
        fin="FIN700100A",
        financial_class="WC",
        carrier_id="EXMUT01",
        carrier="Example Mutual WC Insurance",
        plan_type="WC",
        claim="WC-2026-004512",
        employer_in2="EMP0042^Northwind Example Logistics",
        gt1_employer="Northwind Example Logistics",
        injury="20261117083000-0500",
        job_related="Y",
        description="Cut left thumb on box cutter while unpacking freight",
    )
    incomplete = a04(
        control_id="WCDEMO0002",
        when="20261121143000",
        mrn="700200",
        name="Sampleperson^Casey",
        dob="19950203",
        sex="M",
        fin="FIN700200B",
        financial_class="WC",
        carrier_id="SAMPCAS01",
        carrier="Sample Casualty TPA",
        plan_type="",
        claim="UNKNOWN",
        employer_in2="",
        gt1_employer="",
        injury="20261120",
        job_related="",
        description="Twisted right ankle stepping off loading dock",
    )
    sandbox = a04(
        control_id="WCLIVE0001",
        when="20261124090000",
        mrn="6000031",
        name="SANDBOX^TESTPATIENT",
        dob="",
        sex="",
        fin="16137",
        financial_class="WC",
        carrier_id="SYNWC01",
        carrier="Synthetic State Fund WC",
        plan_type="WC",
        claim="WC-SYN-000777",
        employer_in2="EMP0099^Contoso Example Hospital Supply",
        gt1_employer="Contoso Example Hospital Supply",
        injury="20261123",
        job_related="Y",
        description="Synthetic: lifting strain while stocking shelves",
    )
    (DEMO / "a04_wc_complete.hl7").write_text(complete, newline="")
    (DEMO / "a04_wc_incomplete.hl7").write_text(incomplete, newline="")
    (EXAMPLES / "a04_wc_complete.hl7").write_text(complete, newline="")
    (EXAMPLES / "a04_wc_incomplete.hl7").write_text(incomplete, newline="")
    (EXAMPLES / "a04_sandbox_12724066.hl7").write_text(sandbox, newline="")

    resources = [
        # Case 1: everything in place -> READY
        patient("demo-wc-1001", "700100", "Testpatient", "Riley", "1988-04-12", "female"),
        encounter(
            "demo-enc-1001",
            "demo-wc-1001",
            "FIN700100A",
            "2026-11-17T15:10:00Z",
            [
                custom_attr(
                    0, "ENCNTR:900001", "Complete Reg?", {"valueCodeableConcept": {"text": "Yes"}}
                ),
                custom_attr(
                    1, "ENCNTR:900002", "Injury Date", {"valueDateTime": "2026-11-17T13:30:00Z"}
                ),
            ],
            ["demo-dx-1001"],
        ),
        coverage(
            "demo-cov-1001-wc",
            "demo-wc-1001",
            "95",
            "WORKER'S COMPENSATION",
            "Workers' Compensation",
            "Example Mutual WC Insurance",
            enc="demo-enc-1001",
            member="WC-2026-004512",
        ),
        coverage(
            "demo-cov-1001-ph",
            "demo-wc-1001",
            "5",
            "PRIVATE HEALTH INSURANCE",
            "Private Health Insurance",
            "Example Health Plan",
        ),
        condition(
            "demo-dx-1001",
            "demo-wc-1001",
            "demo-enc-1001",
            "S61.012A",
            "Laceration without foreign body of left thumb without damage to nail, "
            "initial encounter",
            "2026-11-17T13:30:00Z",
        ),
        social(
            "demo-sh-1001-occ",
            "demo-wc-1001",
            "http://loinc.org",
            "11341-5",
            "History of Occupation",
            {"text": "Warehouse associate"},
        ),
        social(
            "demo-sh-1001-emp",
            "demo-wc-1001",
            "http://snomed.info/sct",
            "224362002",
            "Employment/School status",
            {"text": "Full time"},
        ),
        # Case 2: registered as WC but incomplete -> NOT READY, lands on the worklist
        patient("demo-wc-1002", "700200", "Sampleperson", "Casey", "1995-02-03", "male"),
        encounter(
            "demo-enc-1002",
            "demo-wc-1002",
            "FIN700200B",
            "2026-11-21T19:30:00Z",
            [
                custom_attr(
                    0, "ENCNTR:900001", "Complete Reg?", {"valueCodeableConcept": {"text": "No"}}
                ),
                custom_attr(
                    1, "ENCNTR:900002", "Injury Date", {"valueDateTime": "2026-11-21T00:00:00Z"}
                ),
            ],
            ["demo-dx-1002"],
        ),
        coverage(
            "demo-cov-1002-ph",
            "demo-wc-1002",
            "5",
            "PRIVATE HEALTH INSURANCE",
            "Private Health Insurance",
            "Example Health Plan",
        ),
        condition(
            "demo-dx-1002",
            "demo-wc-1002",
            "demo-enc-1002",
            "S93.401A",
            "Sprain of unspecified ligament of right ankle, initial encounter",
            "2026-11-20T00:00:00Z",
        ),
        social(
            "demo-sh-1002-occ",
            "demo-wc-1002",
            "http://loinc.org",
            "11341-5",
            "History of Occupation",
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/data-absent-reason",
                        "code": "unknown",
                    }
                ],
                "text": "Unknown",
            },
        ),
    ]
    fixture = {
        "_comment": "Synthetic, invented data shaped like Oracle Health R4 responses. Not PHI.",
        "cases": [
            {"patient": "demo-wc-1001", "encounter": "demo-enc-1001", "adt": "a04_wc_complete.hl7"},
            {
                "patient": "demo-wc-1002",
                "encounter": "demo-enc-1002",
                "adt": "a04_wc_incomplete.hl7",
            },
        ],
        "resources": resources,
    }
    (DEMO / "demo_fhir.json").write_text(json.dumps(fixture, indent=1) + "\n")
    print(f"wrote demo data to {DEMO} and {EXAMPLES}")


if __name__ == "__main__":
    build()
