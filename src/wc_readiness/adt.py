"""Pull the workers' comp fields out of an HL7 v2 ADT message (usually A04 "register a patient").

FHIR at Oracle Health doesn't expose employer, injury date, claim number or financial class
(see Pip's RESEARCH.md section 2b), but the registration system sends them in HL7 v2:

    PV1-20  Financial Class            e.g. WC  (values are site-defined)
    IN1-4   Insurance Company Name     the WC carrier or TPA
    IN1-11  Insured's Group Emp Name   employer, on some feeds
    IN1-15  Plan Type                  site table 0086
    IN1-36  Policy Number              many sites put the WC claim number here
    IN2-3   Insured's Employer's Name and ID
    ACC-1   Accident Date/Time         the injury date
    ACC-2   Accident Code              site table 0050
    ACC-5   Accident Job Related Indicator  Y / N
    ACC-8   Accident Description
    GT1-16  Guarantor Employer Name
    GT1-51  Guarantor Employer's Organization Name

Field numbers follow HL7 v2.5.1. Which fields a hospital actually fills is local build:
always check the site's ADT interface spec.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from .hl7v2 import Message, ParseError, Segment, parse_message

# Claim numbers that mean "not assigned yet" (WC EDI guides use UNKNOWN in REF*Y4).
PLACEHOLDER_CLAIM_NUMBERS = {"", "UNKNOWN", "UNK", "PENDING", "TBD", "NONE", "N/A", "NA", "0"}

DEFAULT_WC_FINANCIAL_CLASSES = frozenset({"WC", "WORKCOMP", "WORKERS COMP"})


@dataclass
class Sourced:
    """A value plus where it came from, so the card can say "Employer: Acme (GT1-51)"."""

    value: str
    source: str


@dataclass
class AdtWcData:
    message_type: str
    control_id: str
    mrn: str = ""
    patient_name: str = ""
    birth_date: str = ""
    visit_number: str = ""
    patient_class: str = ""
    financial_class: str = ""
    carrier: Sourced | None = None
    plan_type: str = ""
    claim_number: Sourced | None = None
    employer: Sourced | None = None
    injury_date: str = ""
    injury_date_raw: str = ""
    accident_code: str = ""
    job_related: str = ""  # "Y", "N" or "" (not sent)
    accident_description: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def hl7_date(text: str) -> str:
    """Turn an HL7 DTM/DT value (YYYY[MM[DD[HHMM...]]][+/-ZZZZ]) into an ISO date, or ''.

    Only the date part matters for an injury date, so time, fractions and offsets are ignored.
    """
    digits = ""
    for ch in text.strip():
        if not ch.isdigit():
            break
        digits += ch
    if len(digits) < 8:
        return ""
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
    except ValueError:
        return ""


def _first(*candidates: tuple[str, str]) -> Sourced | None:
    for value, source in candidates:
        if value.strip():
            return Sourced(value.strip(), source)
    return None


def _employer_name(seg: Segment, n: int, *, id_first: bool) -> str:
    """IN2-3 is an XCN (ID^name...), GT1-51 is an XON (name^...). Take the name component and
    fall back to the other one if the site only sent one."""
    first, second = seg.get(n, comp=1), seg.get(n, comp=2)
    if id_first:
        return second or first
    return first or second


def extract_wc(msg: Message) -> AdtWcData:
    data = AdtWcData(message_type=msg.type_label, control_id=msg.control_id)
    if msg.type_code != "ADT":
        data.warnings.append(f"expected an ADT message, got {msg.type_label}")

    pid = msg.first("PID")
    if pid is None:
        data.warnings.append("no PID segment: cannot tell which patient this is")
    else:
        data.mrn = pid.get(3)
        family, given = pid.get(5, comp=1), pid.get(5, comp=2)
        data.patient_name = ", ".join(p for p in (family, given) if p)
        data.birth_date = hl7_date(pid.get(7))

    pv1 = msg.first("PV1")
    if pv1 is not None:
        data.patient_class = pv1.get(2)
        data.visit_number = pv1.get(19)
        data.financial_class = pv1.get(20)

    in1 = msg.first("IN1")
    in2 = msg.first("IN2")
    gt1 = msg.first("GT1")
    acc = msg.first("ACC")

    if in1 is not None:
        data.carrier = _first((in1.get(4), "IN1-4"))
        data.plan_type = in1.get(15)
        claim = in1.get(36)
        if claim.strip().upper() in PLACEHOLDER_CLAIM_NUMBERS:
            if claim.strip():
                data.warnings.append(f"IN1-36 holds a placeholder claim number ({claim!r})")
        else:
            data.claim_number = Sourced(claim.strip(), "IN1-36")

    data.employer = _first(
        (_employer_name(in2, 3, id_first=True) if in2 else "", "IN2-3"),
        (_employer_name(gt1, 51, id_first=False) if gt1 else "", "GT1-51"),
        (gt1.get(16) if gt1 else "", "GT1-16"),
        (in1.get(11) if in1 else "", "IN1-11"),
    )

    if acc is not None:
        data.injury_date_raw = acc.get(1)
        data.injury_date = hl7_date(data.injury_date_raw)
        if data.injury_date_raw and not data.injury_date:
            data.warnings.append(f"ACC-1 is not a valid date: {data.injury_date_raw!r}")
        data.accident_code = acc.get(2)
        data.job_related = acc.get(5).strip().upper()
        data.accident_description = acc.get(8)
    return data


def parse_adt(text: str) -> AdtWcData:
    """Parse one HL7 v2 message and return its WC fields. Raises ParseError if malformed."""
    return extract_wc(parse_message(text))


def is_wc_financial_class(value: str, wc_classes: frozenset[str] | set[str]) -> bool:
    return value.strip().upper() in {c.upper() for c in wc_classes}


__all__ = [
    "DEFAULT_WC_FINANCIAL_CLASSES",
    "AdtWcData",
    "ParseError",
    "Sourced",
    "extract_wc",
    "hl7_date",
    "is_wc_financial_class",
    "parse_adt",
]
