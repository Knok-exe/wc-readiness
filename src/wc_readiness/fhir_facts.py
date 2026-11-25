"""Turn raw FHIR resources into the handful of facts a WC readiness card needs.

Every lookup is wrapped so one failing request (the open sandbox sometimes times out on
Condition searches) shows up as "could not read" instead of crashing the whole card.
Missing data and unreadable data are different things, and the card says which is which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import tzinfo

from .dates import fhir_local_date
from .fhir_client import FhirError, FhirSource


class ContextMismatch(FhirError):
    """The Encounter asked for belongs to a different patient. Nothing from it may be used."""


# Source of Payment Typology (PHDSC/NAHDO). 95 = Worker's Compensation; 951/953/954/959 are
# its children (there is no 952). Oracle sends the OID form of the system.
SOP_SYSTEMS = {"urn:oid:2.16.840.1.113883.3.221.5", "https://nahdo.org/sopt"}
SOP_WC_CODES = {"95", "951", "953", "954", "959"}
ACTCODE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
ACTCODE_WC_CODES = {"WCBPOL", "WRKCOMP"}

CUSTOM_ATTRIBUTE_URL = "https://fhir-ehr.cerner.com/r4/StructureDefinition/custom-attribute"
COVERAGE_ENCOUNTER_URL_SUFFIX = "coverage-encounter"

# Occupation / employment observations
LOINC_OCCUPATION = {"11341-5", "85658-3"}  # History of Occupation; Occupation [Type] (ODH)
LOINC_EMPLOYMENT_STATUS = {"74165-2"}  # History of employment status (ODH)
SNOMED_EMPLOYMENT = {"224362002"}  # Employment/School status (Cerner social history)
DATA_ABSENT_SYSTEM = "http://terminology.hl7.org/CodeSystem/data-absent-reason"

ICD10_SYSTEMS = {"http://hl7.org/fhir/sid/icd-10-cm", "http://hl7.org/fhir/sid/icd-10"}


@dataclass
class CodeText:
    code: str
    system: str
    display: str


@dataclass
class CoverageFact:
    id: str
    type_code: str
    type_display: str
    type_system: str
    is_wc: bool
    payer: str
    plan: str
    member_id: str
    status: str
    encounter_level: bool
    period_start: str = ""  # local dates ('' = open-ended or unknown)
    period_end: str = ""

    def current_on(self, day: str) -> bool:
        """Active, and (if we know the visit date) the period includes it."""
        if self.status != "active":
            return False
        if not day:
            return True
        return (not self.period_start or self.period_start <= day) and (
            not self.period_end or day <= self.period_end
        )


@dataclass
class ConditionFact:
    id: str
    text: str
    codes: list[CodeText]
    onset: str
    category: list[str]
    is_injury: bool
    linked_to_encounter: bool


@dataclass
class FhirFacts:
    patient_id: str
    patient_name: str = ""
    birth_date: str = ""
    mrns: list[str] = field(default_factory=list)
    encounter_id: str = ""
    encounter_read: bool = False  # False: the Encounter itself could not be read
    encounter_summary: str = ""
    encounter_start: str = ""
    encounter_date: str = ""  # local (site time zone) date of encounter_start
    encounter_identifiers: list[str] = field(default_factory=list)
    custom_attributes: dict[str, str] = field(default_factory=dict)
    coverages: list[CoverageFact] = field(default_factory=list)
    conditions: list[ConditionFact] = field(default_factory=list)
    occupation: str = ""
    employment_status: str = ""
    documents: list[str] = field(default_factory=list)
    other_injury_problems: int = 0  # injury-coded conditions NOT linked to this encounter
    coverage_other_patient: list[str] = field(default_factory=list)  # dropped Coverage ids
    encounter_returned_id: str = ""  # set if the server answered with a different Encounter id
    errors: dict[str, str] = field(default_factory=dict)  # resource label -> error message

    @property
    def coverage_unreadable(self) -> bool:
        """True if any Coverage query failed (patient-level or encounter-level)."""
        return any(label.startswith("Coverage") for label in self.errors)

    @property
    def wc_coverages(self) -> list[CoverageFact]:
        """WC coverages that count for this visit: active and in date. Encounter-level first."""
        found = [c for c in self.coverages if c.is_wc and c.current_on(self.encounter_date)]
        return sorted(found, key=lambda c: not c.encounter_level)

    @property
    def inactive_wc_coverages(self) -> list[CoverageFact]:
        """WC coverages left out (cancelled, entered-in-error, an old claim's period...)."""
        return [c for c in self.coverages if c.is_wc and not c.current_on(self.encounter_date)]

    @property
    def injury_conditions(self) -> list[ConditionFact]:
        return [c for c in self.conditions if c.is_injury]

    @property
    def work_activity_conditions(self) -> list[ConditionFact]:
        """Conditions coded Y99.0, "civilian activity done for income or pay": not an injury
        diagnosis, but a (weak) sign the visit is about work."""
        return [
            c
            for c in self.conditions
            if any(
                x.system in ICD10_SYSTEMS and x.code.upper().replace(".", "") == "Y990"
                for x in c.codes
            )
        ]


def concept_text(concept: dict | None) -> str:
    if not concept:
        return ""
    if concept.get("text"):
        return concept["text"]
    for coding in concept.get("coding", []):
        if coding.get("display"):
            return coding["display"]
    return ""


def is_wc_coverage_type(concept: dict | None) -> bool:
    for coding in (concept or {}).get("coding", []):
        system, code = coding.get("system", ""), str(coding.get("code", ""))
        if system in SOP_SYSTEMS and code in SOP_WC_CODES:
            return True
        if system == ACTCODE_SYSTEM and code in ACTCODE_WC_CODES:
            return True
    return False


def coverage_fact(res: dict, tz: tzinfo | None = None) -> CoverageFact:
    coding = ((res.get("type") or {}).get("coding") or [{}])[0]
    plan = next(
        (
            c.get("name") or c.get("value", "")
            for c in res.get("class", [])
            if any(x.get("code") == "plan" for x in (c.get("type") or {}).get("coding", []))
        ),
        "",
    )
    ids = [i.get("value", "") for i in res.get("identifier", []) if i.get("value")]
    return CoverageFact(
        id=res.get("id", ""),
        type_code=str(coding.get("code", "")),
        type_display=concept_text(res.get("type")),
        type_system=coding.get("system", ""),
        is_wc=is_wc_coverage_type(res.get("type")),
        payer=", ".join(p.get("display", "") for p in res.get("payor", []) if p.get("display")),
        plan=plan,
        member_id=res.get("subscriberId") or (ids[0] if ids else ""),
        status=res.get("status", ""),
        encounter_level=any(
            e.get("url", "").endswith(COVERAGE_ENCOUNTER_URL_SUFFIX)
            for e in res.get("extension", [])
        ),
        period_start=fhir_local_date(str((res.get("period") or {}).get("start", "")), tz),
        period_end=fhir_local_date(str((res.get("period") or {}).get("end", "")), tz),
    )


def custom_attributes(encounter: dict) -> dict[str, str]:
    """Oracle Encounter custom attributes: site-defined registration fields, shaped as
    extension[url=custom-attribute] with sub-extensions custom-attribute-name / -value."""
    out: dict[str, str] = {}
    for ext in encounter.get("extension", []):
        if ext.get("url") != CUSTOM_ATTRIBUTE_URL:
            continue
        name = value = ""
        for sub in ext.get("extension", []):
            if sub.get("url") == "custom-attribute-name":
                name = sub.get("valueString", "")
            elif sub.get("url") == "custom-attribute-value":
                for key, val in sub.items():
                    if key.startswith("value"):
                        value = concept_text(val) if isinstance(val, dict) else str(val)
        if name:
            out[name] = value
    return out


def _is_injury_code(coding: dict) -> bool:
    """ICD-10-CM injury codes that can point at a work injury: chapter 19 S00-T77/T79 (T51-T65
    toxic exposures included) and external causes V00-Y38. Left out because they aren't
    accidents: T78 (allergy / adverse effects), T80-T88 (complications of care), Y39-Y99
    (misadventure during care, supplementary factors)."""
    if coding.get("system") not in ICD10_SYSTEMS:
        return False
    code = str(coding.get("code", "")).upper().replace(".", "")
    letter, digits = code[:1], code[1:3]
    if not digits.isdigit():
        return False
    n = int(digits)
    if letter == "S":
        return True
    if letter == "T":
        return n <= 77 or n == 79
    if letter in {"V", "W", "X"}:
        return True
    if letter == "Y":
        return n <= 38
    return False


def _refers_to(ref: str, rtype: str, rid: str) -> bool:
    """'Patient/1', 'https://server/r4/Patient/1' and 'Patient/1/_history/2' all mean Patient/1."""
    parts = ref.split("/_history/")[0].rstrip("/").split("/")
    return len(parts) >= 2 and parts[-2] == rtype and parts[-1] == rid


def condition_fact(
    res: dict, encounter_id: str, diagnosis_ids: set[str], tz: tzinfo | None = None
) -> ConditionFact:
    codes = [
        CodeText(str(c.get("code", "")), c.get("system", ""), c.get("display", ""))
        for c in (res.get("code") or {}).get("coding", [])
    ]
    categories = sorted(
        {
            c.get("code", "")
            for cat in res.get("category", [])
            for c in cat.get("coding", [])
            if c.get("code")
        }
    )
    enc_ref = (res.get("encounter") or {}).get("reference", "")
    return ConditionFact(
        id=res.get("id", ""),
        text=concept_text(res.get("code")),
        codes=codes,
        onset=fhir_local_date(str(res.get("onsetDateTime", "")), tz),
        category=categories,
        is_injury=any(_is_injury_code(c) for c in (res.get("code") or {}).get("coding", [])),
        linked_to_encounter=bool(encounter_id)
        and (enc_ref == f"Encounter/{encounter_id}" or res.get("id") in diagnosis_ids),
    )


def _verification(res: dict) -> str:
    return next(
        (c.get("code", "") for c in (res.get("verificationStatus") or {}).get("coding", [])), ""
    )


def _obs_value(res: dict) -> str:
    concept = res.get("valueCodeableConcept")
    if concept:
        if any(c.get("system") == DATA_ABSENT_SYSTEM for c in concept.get("coding", [])):
            return ""
        return concept_text(concept)
    if res.get("valueString"):
        return res["valueString"]
    return ""


def _codes(res: dict) -> set[str]:
    return {str(c.get("code", "")) for c in (res.get("code") or {}).get("coding", [])}


def _pick_encounter(encounters: list[dict]) -> dict | None:
    """Without an encounter id, use the one with the latest period.start (ISO strings sort)."""
    if not encounters:
        return None
    return max(encounters, key=lambda e: (e.get("period") or {}).get("start", ""))


def _safe_search(
    source: FhirSource, rtype: str, params: dict[str, str], label: str, errors: dict[str, str]
) -> list[dict]:
    try:
        return source.search(rtype, params)
    except FhirError as exc:
        errors[label] = str(exc)
        return []


def gather(
    source: FhirSource,
    patient_id: str,
    encounter_id: str | None = None,
    tz: tzinfo | None = None,
) -> FhirFacts:
    """Collect everything the card needs for one patient (and one encounter) with read-only
    GETs. Each request is independent: failures land in facts.errors. Dates are converted to the
    site time zone `tz` (default America/New_York).

    If an encounter id was asked for, it is kept even when reading that Encounter fails, so the
    worklist still files the result under the right case."""
    facts = FhirFacts(patient_id=patient_id, encounter_id=encounter_id or "")

    try:
        patient = source.read("Patient", patient_id)
        names = patient.get("name", [])
        official = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
        facts.patient_name = official.get("text") or ", ".join(
            p for p in (official.get("family", ""), " ".join(official.get("given", []))) if p
        )
        facts.birth_date = patient.get("birthDate", "")
        facts.mrns = [
            i.get("value", "")
            for i in patient.get("identifier", [])
            if i.get("value")
            and any(c.get("code") == "MR" for c in (i.get("type") or {}).get("coding", []))
        ]
    except FhirError as exc:
        facts.errors["Patient"] = str(exc)

    encounter = None
    try:
        if encounter_id:
            encounter = source.read("Encounter", encounter_id)
        else:
            encounter = _pick_encounter(
                source.search("Encounter", {"patient": patient_id, "_count": "20"})
            )
    except FhirError as exc:
        facts.errors["Encounter"] = str(exc)

    diagnosis_ids: set[str] = set()
    if encounter:
        subject = (encounter.get("subject") or {}).get("reference", "")
        if not _refers_to(subject, "Patient", patient_id):
            # A missing subject fails closed too: we can't show it's this patient's visit.
            raise ContextMismatch(
                f"Encounter/{encounter.get('id') or encounter_id} belongs to "
                f"{subject or 'no patient (Encounter.subject is missing)'}, "
                f"not Patient/{patient_id}"
            )
        facts.encounter_read = True
        returned = encounter.get("id", "")
        if encounter_id and returned and returned != encounter_id:
            facts.encounter_returned_id = returned  # keep the requested id: same case key
        else:
            facts.encounter_id = returned or facts.encounter_id
        cls = (encounter.get("class") or {}).get("display") or (encounter.get("class") or {}).get(
            "code", ""
        )
        etype = concept_text((encounter.get("type") or [{}])[0])
        facts.encounter_summary = " / ".join(
            p for p in (cls, etype, encounter.get("status", "")) if p
        )
        facts.encounter_start = (encounter.get("period") or {}).get("start", "")
        facts.encounter_date = fhir_local_date(facts.encounter_start, tz)
        facts.encounter_identifiers = [
            i.get("value", "") for i in encounter.get("identifier", []) if i.get("value")
        ]
        facts.custom_attributes = custom_attributes(encounter)
        for dx in encounter.get("diagnosis", []):
            ref = (dx.get("condition") or {}).get("reference", "")
            if ref.startswith("Condition/"):
                diagnosis_ids.add(ref.split("/", 1)[1])

    # Coverage: patient-level plus encounter-level. Oracle refuses patient and -encounter in
    # the same request (OBSERVED: "_id, patient, and -encounter may not be set at the same time").
    seen: set[str] = set()
    queries = [("Coverage", {"patient": patient_id})]
    if facts.encounter_id:
        queries.append(("Coverage (encounter)", {"-encounter": facts.encounter_id}))
    for label, params in queries:
        for res in _safe_search(source, "Coverage", params, label, facts.errors):
            if res.get("id") in seen:
                continue
            seen.add(res.get("id"))
            beneficiary = (res.get("beneficiary") or {}).get("reference", "")
            if beneficiary and not _refers_to(beneficiary, "Patient", patient_id):
                facts.coverage_other_patient.append(res.get("id", "?"))
                continue
            facts.coverages.append(coverage_fact(res, tz))

    # Conditions: Oracle's open sandbox ignored `encounter` and returned the full problem list
    # (1,700+ items for the test patient), so we filter here and keep only conditions linked to
    # this encounter. Injury-coded problems from other visits are only counted: on the live
    # sandbox they were old allergy (T78) entries that have nothing to do with a work injury.
    try:
        params = {"patient": patient_id}
        if facts.encounter_id:
            params["encounter"] = facts.encounter_id
        for res in source.search("Condition", params):
            if _verification(res) in {"entered-in-error", "refuted"}:
                continue
            fact = condition_fact(res, facts.encounter_id, diagnosis_ids, tz)
            if fact.linked_to_encounter:
                facts.conditions.append(fact)
            elif fact.is_injury:
                facts.other_injury_problems += 1
    except FhirError as exc:
        facts.errors["Condition"] = str(exc)

    try:
        for res in source.search(
            "Observation", {"patient": patient_id, "category": "social-history", "_count": "100"}
        ):
            codes = _codes(res)
            value = _obs_value(res)
            if codes & LOINC_OCCUPATION and value and not facts.occupation:
                facts.occupation = value
            elif codes & (LOINC_EMPLOYMENT_STATUS | SNOMED_EMPLOYMENT) and value:
                facts.employment_status = facts.employment_status or value
    except FhirError as exc:
        facts.errors["Observation"] = str(exc)

    if facts.encounter_id:
        try:
            for res in source.search(
                "DocumentReference", {"patient": patient_id, "encounter": facts.encounter_id}
            ):
                facts.documents.append(concept_text(res.get("type")) or res.get("id", ""))
        except FhirError as exc:
            facts.errors["DocumentReference"] = str(exc)
    return facts
