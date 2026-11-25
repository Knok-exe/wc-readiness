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

import re
from dataclasses import asdict, dataclass, field

from .dates import hl7_local_date
from .hl7v2 import Message, ParseError, Segment, parse_message

# Claim numbers that mean "not assigned yet" (WC EDI guides use UNKNOWN in REF*Y4).
PLACEHOLDER_CLAIM_NUMBERS = {"", "UNKNOWN", "UNK", "PENDING", "TBD", "NONE", "N/A", "NA", "0"}

DEFAULT_WC_FINANCIAL_CLASSES = frozenset({"WC", "WORKCOMP", "WORKERS COMP"})

# How an IN1 says "this insurance is the workers' comp one": IN1-15 plan type, or IN1-2 plan id
# text. Both are site tables, so these are common values, not a standard list.
WC_PLAN_TYPES = frozenset({"WC", "WORKCOMP", "WORKERS COMP", "WCBPOL"})
WC_PLAN_TEXT = re.compile(r"\bwork(?:er'?s'?)?\s*comp|\bWC\b", re.IGNORECASE)


@dataclass
class Sourced:
    """A value plus where it came from, so the card can say "Employer: Acme (GT1-51)"."""

    value: str
    source: str
    confirmed: bool = True  # False = plausible but not certain (e.g. guarantor's employer)


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
    in1_count: int = 0
    wc_in1: str = ""  # "marked" (IN1-2/IN1-15 say WC), "assumed" (the only IN1), or "" (none)
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
    """Turn an HL7 DT/DTM value (YYYYMMDD[HHMM[SS[.S]]][+/-ZZZZ]) into an ISO date, or ''.

    The date is taken as written (local to the sender). The card converts offsets to the site
    time zone with dates.hl7_local_date when it compares dates.
    """
    return hl7_local_date(text)


def _first(*candidates: tuple[str, str, bool]) -> Sourced | None:
    for value, source, confirmed in candidates:
        if value.strip():
            return Sourced(value.strip(), source, confirmed)
    return None


def is_wc_in1(in1: Segment) -> bool:
    if in1.get(15).strip().upper() in WC_PLAN_TYPES:
        return True
    return any(WC_PLAN_TEXT.search(in1.get(2, comp=i)) for i in (1, 2))


def _in2_after(msg: Message, in1: Segment) -> Segment | None:
    """IN2 belongs to the IN1 right before it (IN1, IN2, IN3 repeat as a group)."""
    idx = next(i for i, s in enumerate(msg.segments) if s is in1)
    for seg in msg.segments[idx + 1 :]:
        if seg.name == "IN2":
            return seg
        if seg.name == "IN1":
            return None
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

    in1s = msg.all("IN1")
    data.in1_count = len(in1s)
    marked = [s for s in in1s if is_wc_in1(s)]
    in1: Segment | None = None
    if marked:
        in1, data.wc_in1 = marked[0], "marked"
    elif len(in1s) == 1:
        in1, data.wc_in1 = in1s[0], "assumed"
        data.warnings.append("the only IN1 is not marked as WC (IN1-2/IN1-15); using it anyway")
    elif in1s:
        data.warnings.append(
            f"{len(in1s)} IN1 segments and none marked as WC (IN1-2/IN1-15): carrier and claim "
            "# not taken from IN1"
        )
    in2 = _in2_after(msg, in1) if in1 is not None else None
    gt1 = msg.first("GT1")
    acc = msg.first("ACC")

    if in1 is not None:
        sure = data.wc_in1 == "marked"
        n = msg.all("IN1").index(in1) + 1
        label = "IN1" if len(in1s) == 1 else f"IN1[{n}]"
        data.carrier = _first((in1.get(4), f"{label}-4", sure))
        data.plan_type = in1.get(15)
        claim = in1.get(36)
        if claim.strip().upper() in PLACEHOLDER_CLAIM_NUMBERS:
            if claim.strip():
                data.warnings.append(f"IN1-36 holds a placeholder claim number ({claim!r})")
        else:
            data.claim_number = Sourced(claim.strip(), f"{label}-36", sure)

    # Only IN2-3 under the WC IN1 is "the patient's employer" with any confidence. GT1 employer
    # fields describe the guarantor's employer, and IN1-11 the insured's group employer, which
    # are often the same person/company in WC but not always: those are marked unconfirmed.
    data.employer = _first(
        (
            _employer_name(in2, 3, id_first=True) if in2 else "",
            "IN2-3",
            data.wc_in1 == "marked",
        ),
        (_employer_name(gt1, 51, id_first=False) if gt1 else "", "GT1-51 (guarantor)", False),
        (gt1.get(16) if gt1 else "", "GT1-16 (guarantor)", False),
        (in1.get(11) if in1 else "", "IN1-11 (group employer)", False),
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
