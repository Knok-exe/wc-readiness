"""Merge FHIR facts and HL7 v2 ADT fields into one WC readiness card, and list the gaps.

Precedence for each essential field (first one that has a value wins):

    Employer     ADT (IN2-3, GT1-51, GT1-16, IN1-11) -> Encounter custom attribute
    Injury date  ADT ACC-1 -> Encounter custom attribute -> injury Condition onset
    Claim #      ADT IN1-36 -> Encounter custom attribute -> WC Coverage member id
    Carrier      ADT IN1-4 -> WC Coverage payor

ADT wins because registration is where these are typed in, and FHIR doesn't carry most of them
(Pip's RESEARCH.md section 2b). The card always shows which source each value came from.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .adt import DEFAULT_WC_FINANCIAL_CLASSES, AdtWcData, Sourced, is_wc_financial_class
from .fhir_facts import FhirFacts

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# Encounter custom attributes are site-defined, so we match on the attribute *name*.
CUSTOM_ATTRIBUTE_HINTS = {
    "employer": ("employer",),
    "injury_date": ("injury date", "date of injury", "accident date", "doi"),
    "claim_number": ("claim",),
}


@dataclass
class Gap:
    code: str
    severity: str  # high | medium | low | info
    message: str
    fix_hint: str = ""


@dataclass
class ReadinessCard:
    patient_id: str
    encounter_id: str
    patient_name: str
    fhir: FhirFacts
    adt: AdtWcData | None
    essentials: dict[str, Sourced | None]
    wc_signals: list[str]
    patient_match: str
    adt_matched: bool = True
    gaps: list[Gap] = field(default_factory=list)

    @property
    def case_key(self) -> str:
        return f"Patient/{self.patient_id}|Encounter/{self.encounter_id or '-'}"

    @property
    def ready(self) -> bool:
        """No blocking gaps. A card with failed FHIR reads is not READY either: unknown is not
        the same as fine."""
        return not self.fhir.errors and not any(g.severity == "high" for g in self.gaps)

    @property
    def status(self) -> str:
        blocking = sum(1 for g in self.gaps if g.severity == "high")
        if blocking:
            return f"NOT READY ({blocking} blocking gap(s))"
        if self.fhir.errors:
            return f"UNKNOWN ({len(self.fhir.errors)} FHIR read(s) failed; re-run)"
        return "READY"

    def to_dict(self) -> dict:
        out = asdict(self)
        out["case_key"] = self.case_key
        out["status"] = self.status
        out["ready"] = self.ready
        return out


def _from_custom_attributes(attrs: dict[str, str], key: str) -> Sourced | None:
    for name, value in attrs.items():
        lowered = name.lower()
        if value.strip() and any(h in lowered for h in CUSTOM_ATTRIBUTE_HINTS[key]):
            value = value.strip()[:10] if key == "injury_date" else value.strip()
            return Sourced(value, f"Encounter custom attribute '{name}'")
    return None


def _first(*values: Sourced | None) -> Sourced | None:
    return next((v for v in values if v is not None and v.value), None)


def _patient_match(fhir: FhirFacts, adt: AdtWcData | None) -> tuple[str, bool]:
    """Return (description, ok). We only trust ADT fields when we can tie the message to the
    FHIR patient, by MRN first and then by name + birth date."""
    if adt is None:
        return "no ADT message supplied", True
    if adt.mrn and adt.mrn in fhir.mrns:
        return f"matched on MRN {adt.mrn}", True
    same_name = adt.patient_name and adt.patient_name.lower() == fhir.patient_name.lower()
    if same_name and adt.birth_date and adt.birth_date == fhir.birth_date:
        return "matched on name + birth date (MRN not found on FHIR Patient)", True
    return (
        f"NOT MATCHED: ADT MRN {adt.mrn or '?'} / {adt.patient_name or '?'} vs FHIR "
        f"{', '.join(fhir.mrns) or 'no MRN'} / {fhir.patient_name or '?'}",
        False,
    )


def build_card(
    fhir: FhirFacts,
    adt: AdtWcData | None = None,
    wc_financial_classes: frozenset[str] | set[str] = DEFAULT_WC_FINANCIAL_CLASSES,
) -> ReadinessCard:
    attrs = fhir.custom_attributes
    wc_cov = fhir.wc_coverages[0] if fhir.wc_coverages else None
    injury_dx = fhir.injury_conditions
    dx_onset = next((c for c in injury_dx if c.onset), None)

    match_text, matched = _patient_match(fhir, adt)
    use_adt = adt if matched else None

    essentials: dict[str, Sourced | None] = {
        "employer": _first(
            use_adt.employer if use_adt else None, _from_custom_attributes(attrs, "employer")
        ),
        "injury_date": _first(
            Sourced(use_adt.injury_date, "ACC-1") if use_adt and use_adt.injury_date else None,
            _from_custom_attributes(attrs, "injury_date"),
            Sourced(dx_onset.onset, f"Condition/{dx_onset.id} onset") if dx_onset else None,
        ),
        "claim_number": _first(
            use_adt.claim_number if use_adt else None,
            _from_custom_attributes(attrs, "claim_number"),
            Sourced(wc_cov.member_id, f"Coverage/{wc_cov.id} member id")
            if wc_cov and wc_cov.member_id
            else None,
        ),
        "carrier": _first(
            use_adt.carrier if use_adt else None,
            Sourced(wc_cov.payer, f"Coverage/{wc_cov.id} payor")
            if wc_cov and wc_cov.payer
            else None,
        ),
    }

    signals: list[str] = []
    fc_is_wc = bool(use_adt) and is_wc_financial_class(
        use_adt.financial_class, wc_financial_classes
    )
    if fc_is_wc:
        signals.append(f"PV1-20 financial class = {use_adt.financial_class}")
    if wc_cov:
        signals.append(f"Coverage.type {wc_cov.type_code} ({wc_cov.type_display or 'WC'})")
    if use_adt and use_adt.job_related == "Y":
        signals.append("ACC-5 job related = Y")
    if injury_dx:
        signals.append(f"injury diagnosis ({injury_dx[0].text})")

    card = ReadinessCard(
        patient_id=fhir.patient_id,
        encounter_id=fhir.encounter_id,
        patient_name=fhir.patient_name or (adt.patient_name if adt else ""),
        fhir=fhir,
        adt=adt,
        essentials=essentials,
        wc_signals=signals,
        patient_match=match_text,
        adt_matched=matched,
    )
    card.gaps = find_gaps(card, fc_is_wc=fc_is_wc, adt_matched=matched)
    return card


def find_gaps(card: ReadinessCard, *, fc_is_wc: bool, adt_matched: bool) -> list[Gap]:
    fhir, adt, ess = card.fhir, card.adt, card.essentials
    gaps: list[Gap] = []
    add = gaps.append
    coverage_unreadable = "Coverage" in fhir.errors
    condition_unreadable = "Condition" in fhir.errors

    for label, error in fhir.errors.items():
        add(
            Gap(
                f"FHIR_READ_FAILED:{label}",
                "info",
                f"Could not read {label} from FHIR ({error}). Treat related items as unknown, "
                "not missing.",
                "Retry later; the open sandbox sometimes times out.",
            )
        )

    if adt is None:
        add(
            Gap(
                "NO_ADT",
                "info",
                "No HL7 v2 ADT message supplied. Employer, injury date and claim # can then only "
                "come from encounter custom attributes, which most sites don't use for them.",
                "Pass --adt <file.hl7> (Phase 2: a live ADT feed).",
            )
        )
    elif not adt_matched:
        add(
            Gap(
                "ADT_PATIENT_MISMATCH",
                "high",
                f"The ADT message does not match this FHIR patient ({card.patient_match}). Its "
                "WC fields were NOT used.",
                "Check you picked the right message / patient.",
            )
        )

    if not fhir.wc_coverages and not coverage_unreadable:
        if fc_is_wc:
            msg = (
                f"Financial class says WC (PV1-20={adt.financial_class}) but no FHIR Coverage is "
                "typed as workers' comp (SOP 95 / WCBPOL)."
            )
            hint = "Ask registration/IT how WC health plans map to Coverage.type."
        else:
            msg = "No Coverage typed as workers' comp (SOP 95/951/953/954/959 or ActCode WCBPOL)."
            hint = "Register the WC carrier as a health plan on the encounter."
        add(Gap("WC_COVERAGE_MISSING", "high", msg, hint))

    missing = [
        ("carrier", "CARRIER_MISSING", "No WC carrier/TPA (IN1-4 or WC Coverage payor).",
         "Get the carrier from the employer or the injured worker."),
        ("employer", "EMPLOYER_MISSING", "No employer (IN2-3, GT1-51, GT1-16, IN1-11).",
         "Ask the patient for employer name and address."),
        ("injury_date", "INJURY_DATE_MISSING", "No injury date (ACC-1, custom attribute, or "
         "injury Condition onset).", "Capture the date of injury at registration."),
        ("claim_number", "CLAIM_NUMBER_MISSING", "No WC claim number (IN1-36 empty or "
         "'UNKNOWN').", "Call the carrier/employer for the claim #."),
    ]  # fmt: skip
    for key, code, msg, hint in missing:
        if ess.get(key) is None:
            add(Gap(code, "high", msg, hint))

    if adt is not None and adt_matched:
        if adt.job_related == "N":
            add(
                Gap(
                    "JOB_RELATED_CONFLICT",
                    "medium",
                    "ACC-5 says the accident is NOT job related, but this is being worked as WC.",
                    "Confirm with the patient/employer; fix ACC-5 or the financial class.",
                )
            )
        elif adt.job_related != "Y":
            add(
                Gap(
                    "JOB_RELATED_NOT_FLAGGED",
                    "medium",
                    "ACC-5 (accident job related) is not set to Y.",
                    "Set the job-related indicator in registration.",
                )
            )
        for warning in adt.warnings:
            add(Gap("ADT_WARNING", "info", warning))

    if not fhir.injury_conditions and not condition_unreadable:
        add(
            Gap(
                "INJURY_DX_MISSING",
                "medium",
                "No injury diagnosis (ICD-10-CM S/T or external cause V-Y) found for this "
                "encounter.",
                "Provider documents the injury diagnosis; coder confirms.",
            )
        )

    dates = {s.value for s in _injury_date_candidates(card) if s.value}
    if len(dates) > 1:
        add(
            Gap(
                "INJURY_DATE_CONFLICT",
                "medium",
                "Injury date differs between sources: "
                + "; ".join(f"{s.value} ({s.source})" for s in _injury_date_candidates(card)),
                "Confirm the date of injury with the patient and fix the wrong source.",
            )
        )

    if not fhir.occupation:
        add(
            Gap(
                "OCCUPATION_MISSING",
                "low",
                "No occupation recorded in social history (LOINC 11341-5 / ODH).",
                "Ask job title during nursing intake.",
            )
        )
    if not any("work" in d.lower() for d in fhir.documents):
        add(
            Gap(
                "WORK_STATUS_NOTE_MISSING",
                "info",
                "No work status / return-to-work document on this encounter yet.",
                "Expected later in the visit (Phase 1 writes it as a DocumentReference).",
            )
        )
    gaps.sort(key=lambda g: (SEVERITY_ORDER[g.severity], g.code))
    return gaps


def _injury_date_candidates(card: ReadinessCard) -> list[Sourced]:
    out: list[Sourced] = []
    if card.adt and card.adt.injury_date and card.adt_matched:
        out.append(Sourced(card.adt.injury_date, "ACC-1"))
    attr = _from_custom_attributes(card.fhir.custom_attributes, "injury_date")
    if attr:
        out.append(attr)
    out.extend(
        Sourced(c.onset, f"Condition/{c.id} onset") for c in card.fhir.injury_conditions if c.onset
    )
    return out
