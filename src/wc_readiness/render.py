"""Plain-text rendering of a readiness card and the worklist (no colours, works anywhere)."""

from __future__ import annotations

import textwrap

from .card import ReadinessCard
from .worklist import WorkItem

LABELS = {
    "employer": "Employer",
    "injury_date": "Injury date",
    "claim_number": "Claim #",
    "carrier": "Carrier / TPA",
}
MARK = {"high": "!!", "medium": "! ", "low": "- ", "info": "i "}
WIDTH = 92


def _wrap(text: str, indent: str) -> list[str]:
    return textwrap.wrap(text, WIDTH, initial_indent=indent, subsequent_indent=indent + "   ")


def _short_list(values: list[str], keep: int = 2) -> str:
    extra = len(values) - keep
    return ", ".join(values[:keep]) + (f" (+{extra} more)" if extra > 0 else "")


def render_card(card: ReadinessCard) -> str:
    f, adt = card.fhir, card.adt
    rule = "=" * WIDTH
    lines = [
        rule,
        f"WC READINESS CARD   {card.status}",
        rule,
        f"Patient    {card.patient_name or '?'}  (FHIR Patient/{card.patient_id}"
        + (f", DOB {f.birth_date}" if f.birth_date else "")
        + (f", MRN {_short_list(f.mrns)}" if f.mrns else "")
        + ")",
        f"Encounter  {card.encounter_id or 'none found'}"
        + (f"  {f.encounter_summary}" if f.encounter_summary else "")
        + (f"  started {f.encounter_start[:10]}" if f.encounter_start else ""),
        "ADT        "
        + (f"{adt.message_type} {adt.control_id}, {card.patient_match}" if adt else "none"),
    ]
    lines += _wrap(f"WC signals {'; '.join(card.wc_signals) or 'none found'}", "")
    lines += ["", "WC essentials"]
    for key, label in LABELS.items():
        val = card.essentials.get(key)
        shown = f"{val.value}   [{val.source}]" if val else "MISSING"
        lines.append(f"  {label:<14} {shown}")

    lines += ["", "From FHIR (read-only GETs)"]
    if f.coverages:
        for c in f.coverages:
            flag = "WC " if c.is_wc else "   "
            lines.append(
                f"  {flag}Coverage {c.type_code or '-':<5} {c.type_display or 'no type':<26} "
                f"payer: {c.payer or '?'}" + (f"  member id: {c.member_id}" if c.member_id else "")
            )
    else:
        lines.append("  Coverage       none")
    if f.conditions:
        for c in f.conditions:
            code = next((x.code for x in c.codes if x.code), "")
            tag = "injury" if c.is_injury else "dx"
            lines.append(
                f"  {tag:<6} {c.text} {('(' + code + ')') if code else ''}"
                + (f" onset {c.onset}" if c.onset else "")
            )
    else:
        lines.append("  Diagnoses      none linked to this encounter")
    if f.other_injury_problems:
        lines.append(
            f"  (+{f.other_injury_problems} injury-coded problem(s) from other visits, not used)"
        )
    lines.append(f"  Occupation     {f.occupation or 'not recorded'}")
    lines.append(f"  Employment     {f.employment_status or 'not recorded'}")
    if f.custom_attributes:
        attrs = "; ".join(f"{k} = {v}" for k, v in f.custom_attributes.items())
        lines += _wrap(f"Custom attrs   {attrs}", "  ")
    lines.append(f"  Documents      {len(f.documents)} on this encounter")

    if adt:
        lines += ["", "From HL7 v2 ADT"]
        lines.append(f"  PV1-20 financial class  {adt.financial_class or '-'}")
        lines.append(f"  IN1-15 plan type        {adt.plan_type or '-'}")
        lines.append(f"  ACC-2 accident code     {adt.accident_code or '-'}")
        lines.append(f"  ACC-5 job related       {adt.job_related or '-'}")
        if adt.accident_description:
            lines.append(f"  ACC-8 description       {adt.accident_description}")

    lines += ["", f"Gaps ({len(card.gaps)})"]
    if not card.gaps:
        lines.append("  none")
    for g in card.gaps:
        lines += _wrap(f"{MARK[g.severity]} [{g.severity}] {g.code}: {g.message}", "  ")
        if g.fix_hint:
            lines += _wrap(f"-> {g.fix_hint}", "       ")
    lines.append(rule)
    return "\n".join(lines)


def render_worklist(items: list[WorkItem]) -> str:
    if not items:
        return "Worklist: nothing to do."
    header = f"{'ID':>3}  {'Status':<11} {'Sev':<6} {'Patient':<20} {'Item':<26} Owner"
    lines = ["Coordinator worklist", header, "-" * len(header)]
    lines.extend(
        f"{it.id:>3}  {it.status:<11} {it.severity:<6} {it.patient_name[:20]:<20} "
        f"{it.gap_code[:26]:<26} {it.owner or '-'}"
        for it in items
    )
    return "\n".join(lines)
