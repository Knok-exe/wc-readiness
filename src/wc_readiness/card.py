"""Merge FHIR facts and HL7 v2 ADT fields into one WC readiness card, and list the gaps.

Precedence for each essential field (first one that has a value wins):

    Employer     ADT IN2-3 under the WC IN1 -> Encounter custom attribute
                 -> GT1-51 / GT1-16 / IN1-11 (unconfirmed: may be the guarantor's employer)
    Injury date  ADT ACC-1 -> Encounter custom attribute -> injury Condition onset (unconfirmed)
    Claim #      ADT IN1-36 of the WC IN1 -> Encounter custom attribute
                 -> WC Coverage member id (unconfirmed: a member id is not a claim #)
    Carrier      ADT IN1-4 of the WC IN1 -> WC Coverage payor

ADT wins because registration is where these are typed in, and FHIR doesn't carry most of them
(Pip's RESEARCH.md section 2b). The card always shows which source each value came from, and
"unconfirmed" values raise a medium gap instead of counting as done.

Three outcomes per rule, which matters for the worklist:
  * the gap is present            -> in card.gaps
  * the gap was checked and absent -> neither list (the worklist may close its item)
  * the rule could not be checked  -> in card.unchecked (a FHIR read failed, the ADT is for
                                      someone else, ...): the worklist must leave its item alone
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import tzinfo

from .adt import DEFAULT_WC_FINANCIAL_CLASSES, AdtWcData, Sourced, is_wc_financial_class
from .dates import fhir_local_date, hl7_local_date, site_tz
from .fhir_facts import FhirFacts

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# Encounter custom attributes are site-defined, so we match on the attribute *name*.
CUSTOM_ATTRIBUTE_HINTS = {
    "employer": re.compile(r"\bemployer\b(?!.*\b(address|phone|city|state|zip)\b)", re.I),
    "injury_date": re.compile(r"injury date|date of injury|accident date|\bdoi\b", re.I),
    "claim_number": re.compile(r"\bclaim\s*(#|no\b|num|number\b|id\b)", re.I),
}

ESSENTIALS = {
    "employer": "EMPLOYER",
    "injury_date": "INJURY_DATE",
    "claim_number": "CLAIM_NUMBER",
    "carrier": "CARRIER",
}
ESSENTIAL_HELP = {
    "employer": (
        "No employer (IN2-3, custom attribute, GT1-51, GT1-16, IN1-11).",
        "Ask the patient for employer name and address.",
    ),
    "injury_date": (
        "No injury date (ACC-1, custom attribute, or injury Condition onset).",
        "Capture the date of injury at registration.",
    ),
    "claim_number": (
        "No WC claim number (IN1-36 empty or 'UNKNOWN').",
        "Call the carrier/employer for the claim #.",
    ),
    "carrier": (
        "No WC carrier/TPA (IN1-4 of the WC IN1, or WC Coverage payor).",
        "Get the carrier from the employer or the injured worker.",
    ),
}
# Which FHIR reads each essential's fallbacks depend on. If one failed and nothing was found,
# "missing" can't be claimed.
ESSENTIAL_FHIR_DEPS = {
    "employer": ("Encounter",),
    "injury_date": ("Encounter", "Condition"),
    "claim_number": ("Encounter", "Coverage"),
    "carrier": ("Coverage",),
}

# Every rule code that only applies to a WC visit.
WC_RULE_CODES = (
    {f"{p}_MISSING" for p in ESSENTIALS.values()}
    | {f"{p}_UNCONFIRMED" for p in ESSENTIALS.values()}
    | {
        "WC_COVERAGE_MISSING",
        "JOB_RELATED_CONFLICT",
        "JOB_RELATED_NOT_FLAGGED",
        "INJURY_DX_MISSING",
        "INJURY_DATE_CONFLICT",
        "OCCUPATION_MISSING",
    }
)

# Gaps whose answer depends on ADT data. Without a matched ADT they are computed from partial
# data (e.g. a custom attribute instead of IN2-3, a key without ACC-1), so they are marked
# provisional and the worklist doesn't let them change a case that has seen an ADT.
ADT_DEPENDENT_CODES = (
    {f"{p}_MISSING" for p in ESSENTIALS.values()}
    | {f"{p}_UNCONFIRMED" for p in ESSENTIALS.values()}
    | {
        "INJURY_DATE_CONFLICT",
        "WC_STATUS_UNCLEAR",
        "JOB_RELATED_CONFLICT",
        "JOB_RELATED_NOT_FLAGGED",
    }
)


@dataclass
class Gap:
    code: str
    severity: str  # high | medium | low | info
    message: str
    fix_hint: str = ""
    # The data the gap is about (values, sources), without the wording. The worklist compares
    # this, not `message`, to decide whether a dismissed item's gap "really changed", so a
    # rewording in a later release doesn't reopen everything. "" = nothing beyond the code.
    key: str = ""
    # Computed without a matched ADT although it depends on ADT data (see ADT_DEPENDENT_CODES).
    provisional: bool = False


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
    adt_matched: bool = True  # False also when the match couldn't be checked
    wc_status: str = "wc"  # wc | not_wc | unclear | unknown
    site_tz: str = ""
    wc_classes: list[str] = field(default_factory=list)  # the PV1-20 values that meant WC
    # Set by Worklist.sync when this case has had a matched ADT before: provisional gaps from
    # this ADT-less run then don't count for readiness (they couldn't change the worklist either).
    provisional_ignored: bool = False
    # Set with provisional_ignored (by Worklist.assess): the case's high worklist items for the
    # gaps this run couldn't re-check that are still open, in progress, or done but flagged as
    # still present. They come from the last ADT run and still block readiness.
    held_blocking: list[str] = field(default_factory=list)
    # Also set by Worklist.assess on such a run: items for those gaps that a coordinator marked
    # `done` but no ADT run has confirmed gone yet. They don't block (done is a deliberate
    # decision), but the status line says so, so READY isn't read as verified.
    done_unconfirmed: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    unchecked: set[str] = field(default_factory=set)

    @property
    def case_key(self) -> str:
        return f"Patient/{self.patient_id}|Encounter/{self.encounter_id or '-'}"

    @property
    def has_case(self) -> bool:
        """Can results be filed on the worklist? Not without knowing which encounter."""
        return bool(self.patient_id and self.encounter_id)

    @property
    def counted_gaps(self) -> list[Gap]:
        """The gaps that decide readiness (provisional ones drop out when provisional_ignored)."""
        if self.provisional_ignored:
            return [g for g in self.gaps if not g.provisional]
        return self.gaps

    @property
    def provisional_count(self) -> int:
        return sum(1 for g in self.gaps if g.provisional and g.severity != "info")

    @property
    def ready(self) -> bool:
        """A WC visit with no blocking gaps and every FHIR read working. Unknown is not fine."""
        return (
            self.wc_status == "wc"
            and not self.fhir.errors
            and not any(g.severity == "high" for g in self.counted_gaps)
            and not self.held_blocking
        )

    @property
    def status(self) -> str:
        text = self._status()
        if self.provisional_ignored and self.provisional_count:
            text += (
                f" [{self.provisional_count} provisional ADT-based gap(s) not re-checked: no "
                "matching ADT this run, so the worklist items from the last ADT run stand]"
            )
        if self.done_unconfirmed:
            n = len(self.done_unconfirmed)
            text += (
                f" [{n} item(s) marked done not yet confirmed by an ADT: "
                f"{', '.join(self.done_unconfirmed)}]"
            )
        return text

    def _status(self) -> str:
        blocking = sum(1 for g in self.counted_gaps if g.severity == "high")
        note = ""
        if blocking:
            provisional = sum(
                1 for g in self.counted_gaps if g.severity == "high" and g.provisional
            )
            note = f", {provisional} provisional: no matched ADT" if provisional else ""
        held = len(self.held_blocking)
        if blocking or held:
            parts = [f"{blocking} blocking gap(s){note}"] if blocking else []
            if held:
                parts.append(
                    f"{held} blocking item(s) still open from the last ADT run: "
                    f"{', '.join(self.held_blocking)}; no matched ADT this run"
                )
            return f"NOT READY ({'; '.join(parts)})"
        if self.wc_status == "not_wc":
            return "NOT A WC VISIT"
        if self.fhir.errors:
            return f"UNKNOWN ({len(self.fhir.errors)} FHIR read(s) failed; re-run)"
        if self.wc_status == "unclear":
            return "WC STATUS UNCLEAR"
        if self.wc_status == "unknown":
            return "UNKNOWN (no usable ADT and no WC signal in FHIR: can't tell if this is WC)"
        return "READY"

    def to_dict(self) -> dict:
        out = asdict(self)
        out["unchecked"] = sorted(self.unchecked)
        out["case_key"] = self.case_key
        out["status"] = self.status
        out["ready"] = self.ready
        return out


def _from_custom_attributes(
    attrs: dict[str, str], key: str, tz: tzinfo | None = None
) -> Sourced | None:
    for name, value in attrs.items():
        if value.strip() and CUSTOM_ATTRIBUTE_HINTS[key].search(name):
            value = value.strip()
            if key == "injury_date":
                value = fhir_local_date(value, tz) or value
            return Sourced(value, f"Encounter custom attribute '{name}'")
    return None


def _first(*values: Sourced | None) -> Sourced | None:
    return next((v for v in values if v is not None and v.value), None)


def _patient_match(fhir: FhirFacts, adt: AdtWcData | None) -> tuple[str, bool, bool]:
    """Return (description, ok, demographics_differ). We only trust ADT fields when we can tie
    the message to the FHIR patient, by MRN first and then by name + birth date."""
    if adt is None:
        return "no ADT message supplied", False, False
    if "Patient" in fhir.errors:
        return "could not check (FHIR Patient read failed)", False, False
    same_name = bool(adt.patient_name) and adt.patient_name.lower() == fhir.patient_name.lower()
    same_dob = bool(adt.birth_date) and adt.birth_date == fhir.birth_date
    all_present = all((adt.patient_name, adt.birth_date, fhir.patient_name, fhir.birth_date))
    if adt.mrn and adt.mrn in fhir.mrns and all_present and not same_name and not same_dob:
        # Same MRN but a different name AND birth date: an MRN collision or typo, not this
        # patient. Using its WC fields could close this patient's items.
        return (
            f"NOT MATCHED: MRN {adt.mrn} matches but name AND birth date differ (ADT "
            f"{adt.patient_name} {adt.birth_date} vs FHIR {fhir.patient_name} {fhir.birth_date})",
            False,
            False,
        )
    if adt.mrn and adt.mrn in fhir.mrns:
        differ = (bool(adt.patient_name) and not same_name) or (
            bool(adt.birth_date) and not same_dob
        )
        return f"matched on MRN {adt.mrn}", True, differ
    if same_name and same_dob:
        return "matched on name + birth date (MRN not found on FHIR Patient)", True, False
    return (
        f"NOT MATCHED: ADT MRN {adt.mrn or '?'} / {adt.patient_name or '?'} vs FHIR "
        f"{', '.join(fhir.mrns[:3]) or 'no MRN'} / {fhir.patient_name or '?'}",
        False,
        False,
    )


def build_card(
    fhir: FhirFacts,
    adt: AdtWcData | None = None,
    wc_financial_classes: frozenset[str] | set[str] = DEFAULT_WC_FINANCIAL_CLASSES,
    tz: tzinfo | None = None,
) -> ReadinessCard:
    tz = tz or site_tz()
    attrs = fhir.custom_attributes
    wc_cov = fhir.wc_coverages[0] if fhir.wc_coverages else None
    injury_dx = fhir.injury_conditions
    dx_onset = next((c for c in injury_dx if c.onset), None)

    match_text, matched, demographics_differ = _patient_match(fhir, adt)
    use_adt = adt if matched else None
    adt_employer = use_adt.employer if use_adt else None
    adt_injury = hl7_local_date(use_adt.injury_date_raw, tz) if use_adt else ""

    essentials: dict[str, Sourced | None] = {
        "employer": _first(
            adt_employer if adt_employer and adt_employer.confirmed else None,
            _from_custom_attributes(attrs, "employer"),
            adt_employer,  # guarantor / group employer: shown, but flagged unconfirmed
        ),
        "injury_date": _first(
            Sourced(adt_injury, "ACC-1") if adt_injury else None,
            _from_custom_attributes(attrs, "injury_date", tz),
            Sourced(dx_onset.onset, f"Condition/{dx_onset.id} onset", confirmed=False)
            if dx_onset
            else None,
        ),
        "claim_number": _first(
            use_adt.claim_number if use_adt else None,
            _from_custom_attributes(attrs, "claim_number"),
            Sourced(wc_cov.member_id, f"Coverage/{wc_cov.id} member id", confirmed=False)
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
    if use_adt and use_adt.wc_in1 == "marked":
        signals.append("IN1 plan marked WC")
    if use_adt and use_adt.job_related == "Y":
        signals.append("ACC-5 job related = Y")

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
        site_tz=str(tz),
        wc_classes=sorted({c.strip().upper() for c in wc_financial_classes}),
    )
    card.wc_status = _wc_status(card, strong=bool(signals), wc_classes=wc_financial_classes)
    if injury_dx:
        card.wc_signals.append(f"injury diagnosis ({injury_dx[0].text}) [weak]")
    if fhir.work_activity_conditions:
        card.wc_signals.append("Y99.0 activity done for income or pay [weak]")
    card.gaps, card.unchecked = find_gaps(
        card, fc_is_wc=fc_is_wc, demographics_differ=demographics_differ, tz=tz
    )
    return card


def _wc_status(card: ReadinessCard, *, strong: bool, wc_classes) -> str:
    """Is this a workers' comp visit at all?

    Only positive evidence makes a visit "not WC": an ADT that matches this patient and carries
    a financial class (PV1-20) that isn't a WC class. A missing or mismatched ADT is NOT
    evidence (forgetting --adt must never close the coordinator's items), so it gives
    "unclear" (injury/accident seen) or "unknown".
    """
    if strong:
        return "wc"
    if card.fhir.errors:
        return "unknown"
    adt = card.adt if card.adt_matched else None
    weak = (
        bool(card.fhir.injury_conditions)
        or bool(card.fhir.work_activity_conditions)
        or (adt is not None and bool(adt.injury_date_raw or adt.accident_code))
    )
    fc = adt.financial_class.strip() if adt is not None else ""
    if adt is not None and fc and not is_wc_financial_class(fc, wc_classes):
        if adt.job_related == "N" or not weak:
            return "not_wc"
        return "unclear"
    if card.adt is not None and not card.adt_matched:
        return "unknown"  # the ADT is someone else's: we know nothing about this visit's class
    return "unclear" if weak else "unknown"


def find_gaps(
    card: ReadinessCard, *, fc_is_wc: bool, demographics_differ: bool, tz: tzinfo
) -> tuple[list[Gap], set[str]]:
    gaps, unchecked = _find_gaps(
        card, fc_is_wc=fc_is_wc, demographics_differ=demographics_differ, tz=tz
    )
    if not (card.adt is not None and card.adt_matched):
        # No matched ADT: ADT-dependent results are provisional, and their absence proves
        # nothing (so an earlier ADT-based item can't be resolved by this run).
        for gap in gaps:
            if gap.code in ADT_DEPENDENT_CODES:
                gap.provisional = True
        # Exception: on a visit FHIR shows to be WC (active WC coverage), "WC status unclear"
        # was settled by positive evidence, so its item may close.
        dependent = ADT_DEPENDENT_CODES
        if card.wc_status == "wc":
            dependent = dependent - {"WC_STATUS_UNCLEAR"}
        unchecked |= dependent - {g.code for g in gaps}
    return gaps, unchecked


def _context_gaps(fhir: FhirFacts) -> list[Gap]:
    """Info notes about data that was left out on purpose."""
    out = []
    if fhir.coverage_other_patient:
        out.append(
            Gap(
                "COVERAGE_OTHER_PATIENT",
                "info",
                "Ignored Coverage belonging to another patient: "
                + ", ".join(fhir.coverage_other_patient),
                "Check the encounter's coverage links in registration.",
            )
        )
    if fhir.inactive_wc_coverages:
        out.append(
            Gap(
                "WC_COVERAGE_INACTIVE",
                "info",
                "WC Coverage not counted (not active, or its period doesn't include this visit): "
                + "; ".join(
                    f"Coverage/{c.id} status={c.status or '?'} period "
                    f"{c.period_start or '?'}..{c.period_end or '?'}"
                    for c in fhir.inactive_wc_coverages
                ),
                "If this visit is part of that claim, registration must update the coverage.",
            )
        )
    if fhir.encounter_returned_id:
        out.append(
            Gap(
                "ENCOUNTER_ID_CHANGED",
                "info",
                f"Asked for Encounter/{fhir.encounter_id}, the server returned "
                f"Encounter/{fhir.encounter_returned_id} (merged record?). The case stays filed "
                "under the requested id.",
            )
        )
    return out


def _find_gaps(
    card: ReadinessCard, *, fc_is_wc: bool, demographics_differ: bool, tz: tzinfo
) -> tuple[list[Gap], set[str]]:
    fhir, adt, ess = card.fhir, card.adt, card.essentials
    gaps: list[Gap] = _context_gaps(fhir)
    unchecked: set[str] = set()
    add = gaps.append
    failed = set(fhir.errors)
    if fhir.coverage_unreadable:
        failed.add("Coverage")
    if fhir.encounter_id and not fhir.encounter_read:
        failed.add("Encounter")
    adt_usable = adt is not None and card.adt_matched

    for label, error in fhir.errors.items():
        add(
            Gap(
                f"FHIR_READ_FAILED:{label}",
                "info",
                f"Could not read {label} from FHIR ({error}). Related items are unknown, not "
                "missing, and the worklist leaves them as they were.",
                "Retry later; the open sandbox sometimes times out.",
            )
        )

    # --- Is the ADT about this patient? ---------------------------------------------------
    if adt is None:
        unchecked |= {"ADT_PATIENT_MISMATCH", "ADT_DEMOGRAPHICS_DIFFER"}
        add(
            Gap(
                "NO_ADT",
                "info",
                "No HL7 v2 ADT message supplied. Employer, injury date and claim # can then only "
                "come from encounter custom attributes, which most sites don't use for them.",
                "Pass --adt <file.hl7> (Phase 2: a live ADT feed).",
            )
        )
    elif "Patient" in fhir.errors:
        unchecked |= {"ADT_PATIENT_MISMATCH", "ADT_DEMOGRAPHICS_DIFFER"}
    elif not card.adt_matched:
        add(
            Gap(
                "ADT_PATIENT_MISMATCH",
                "high",
                f"The ADT message does not match this FHIR patient ({card.patient_match}). Its "
                "WC fields were NOT used, and ADT-based items were left as they were.",
                "Check you picked the right message / patient.",
                key=f"adt_mrn={adt.mrn}",
            )
        )
        unchecked.add("ADT_DEMOGRAPHICS_DIFFER")
    elif demographics_differ:
        add(
            Gap(
                "ADT_DEMOGRAPHICS_DIFFER",
                "medium",
                f"MRN matches but name/birth date differ: ADT {adt.patient_name or '?'} "
                f"{adt.birth_date or '?'} vs FHIR {fhir.patient_name or '?'} "
                f"{fhir.birth_date or '?'}.",
                "Confirm identity; one of the systems may hold an old name or a typo.",
                key=f"{adt.patient_name}|{adt.birth_date}|{fhir.patient_name}|{fhir.birth_date}",
            )
        )
    if adt_usable:
        for warning in adt.warnings:
            add(Gap("ADT_WARNING", "info", warning))

    # --- Is this a WC visit? -----------------------------------------------------------------
    if card.wc_status == "not_wc":
        add(
            Gap(
                "NOT_WC_VISIT",
                "info",
                f"Registered as a non-WC visit (PV1-20={adt.financial_class.strip()}"
                + (", ACC-5=N" if adt.job_related == "N" else "")
                + ") and no WC coverage or WC-marked IN1, so the WC checks don't apply.",
                "If this IS a work injury, registration needs to set the WC financial class/plan.",
                key=f"fc={adt.financial_class.strip()}",
            )
        )
        _finish(gaps)
        return gaps, unchecked  # WC rule codes are "checked and absent": not applicable
    if card.wc_status == "unclear":
        add(
            Gap(
                "WC_STATUS_UNCLEAR",
                "medium",
                "There's an injury, accident or work-activity code but no WC financial class, WC "
                "coverage or "
                "job-related flag. Is this a work injury?",
                "Ask the patient whether the injury happened at work; update registration.",
                key=f"fc={adt.financial_class.strip() if adt_usable else ''}",
            )
        )
    if card.wc_status != "wc":
        if card.wc_status == "unknown":
            unchecked.add("WC_STATUS_UNCLEAR")
            if adt is None:
                add(
                    Gap(
                        "WC_STATUS_UNKNOWN",
                        "info",
                        "No ADT supplied and FHIR shows no WC coverage or injury: can't tell "
                        "whether this is a WC visit. WC items were left as they were.",
                        "Re-run with --adt.",
                    )
                )
        unchecked |= WC_RULE_CODES
        _finish(gaps)
        return gaps, unchecked

    # --- WC rules -----------------------------------------------------------------------------
    if "Coverage" in failed:
        unchecked.add("WC_COVERAGE_MISSING")
    elif not fhir.wc_coverages:
        if fc_is_wc:
            msg = (
                f"Financial class says WC (PV1-20={adt.financial_class}) but no FHIR Coverage is "
                "typed as workers' comp (SOP 95 / WCBPOL)."
            )
            hint = "Ask registration/IT how WC health plans map to Coverage.type."
        else:
            msg = "No Coverage typed as workers' comp (SOP 95/951/953/954/959 or ActCode WCBPOL)."
            hint = "Register the WC carrier as a health plan on the encounter."
        fc = adt.financial_class.strip() if adt_usable else ""
        add(Gap("WC_COVERAGE_MISSING", "high", msg, hint, key=f"fc={fc}" if fc_is_wc else ""))

    for key, prefix in ESSENTIALS.items():
        value = ess.get(key)
        missing_code, unconfirmed_code = f"{prefix}_MISSING", f"{prefix}_UNCONFIRMED"
        if value is None:
            sources_unknown = (adt is not None and not adt_usable) or any(
                dep in failed for dep in ESSENTIAL_FHIR_DEPS[key]
            )
            if sources_unknown:
                unchecked |= {missing_code, unconfirmed_code}
            else:
                msg, hint = ESSENTIAL_HELP[key]
                add(Gap(missing_code, "high", msg, hint))
        elif not value.confirmed:
            add(
                Gap(
                    unconfirmed_code,
                    "medium",
                    f"{key.replace('_', ' ').capitalize()} '{value.value}' comes from "
                    f"{value.source}, which may not be the WC value.",
                    "Confirm it with the patient, employer or carrier.",
                    key=f"{value.value}|{value.source}",
                )
            )

    if adt is None:
        # Without the ADT, an essential that isn't found is still reported, but an earlier
        # ADT-based gap can't be confirmed as fixed: don't let a forgotten --adt close items.
        for prefix in ESSENTIALS.values():
            unchecked |= {f"{prefix}_MISSING", f"{prefix}_UNCONFIRMED"}
        unchecked.add("INJURY_DATE_CONFLICT")

    if adt_usable:
        if adt.job_related == "N":
            add(
                Gap(
                    "JOB_RELATED_CONFLICT",
                    "high",
                    "ACC-5 says the accident is NOT job related, but the visit is registered as "
                    "WC. Billing WC for a non-work injury gets denied.",
                    "Confirm with the patient/employer; fix ACC-5 or the financial class.",
                    key="acc5=N",
                )
            )
        elif adt.job_related != "Y":
            add(
                Gap(
                    "JOB_RELATED_NOT_FLAGGED",
                    "medium",
                    "ACC-5 (accident job related) is not set to Y.",
                    "Set the job-related indicator in registration.",
                    key=f"acc5={adt.job_related}",
                )
            )
    else:
        unchecked |= {"JOB_RELATED_CONFLICT", "JOB_RELATED_NOT_FLAGGED"}

    if {"Condition", "Encounter"} & failed:
        unchecked.add("INJURY_DX_MISSING")
    elif not fhir.injury_conditions:
        add(
            Gap(
                "INJURY_DX_MISSING",
                "medium",
                "No injury diagnosis (ICD-10-CM S/T or external cause V-Y) found for this "
                "encounter.",
                "Provider documents the injury diagnosis; coder confirms.",
            )
        )

    candidates = _injury_date_candidates(card, tz)
    if len({s.value for s in candidates}) > 1:
        add(
            Gap(
                "INJURY_DATE_CONFLICT",
                "medium",
                "Injury date differs between sources: "
                + "; ".join(f"{s.value} ({s.source})" for s in candidates),
                "Confirm the date of injury with the patient and fix the wrong source.",
                key=";".join(sorted(f"{s.value}|{s.source}" for s in candidates)),
            )
        )
    elif {"Condition", "Encounter"} & failed or (adt is not None and not adt_usable):
        unchecked.add("INJURY_DATE_CONFLICT")

    if "Observation" in failed:
        unchecked.add("OCCUPATION_MISSING")
    elif not fhir.occupation:
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
    _finish(gaps)
    return gaps, unchecked


def _finish(gaps: list[Gap]) -> None:
    gaps.sort(key=lambda g: (SEVERITY_ORDER[g.severity], g.code))


def _injury_date_candidates(card: ReadinessCard, tz: tzinfo) -> list[Sourced]:
    """All injury dates we can see, as local calendar dates, for the conflict check."""
    out: list[Sourced] = []
    if card.adt and card.adt_matched:
        adt_date = hl7_local_date(card.adt.injury_date_raw, tz)
        if adt_date:
            out.append(Sourced(adt_date, "ACC-1"))
    attr = _from_custom_attributes(card.fhir.custom_attributes, "injury_date", tz)
    if attr and fhir_local_date(attr.value, tz):
        out.append(attr)
    out.extend(
        Sourced(c.onset, f"Condition/{c.id} onset") for c in card.fhir.injury_conditions if c.onset
    )
    return out
