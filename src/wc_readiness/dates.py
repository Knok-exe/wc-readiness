"""Turning timestamps into *local* calendar dates.

A FHIR dateTime like 2026-11-21T02:15:00Z is 9:15 PM on Nov 20 in New York. Cutting the string
at 10 characters would say Nov 21 and invent a one-day "conflict" with an HL7 ACC-1 of 20261120.
So every timestamp that carries a UTC offset is converted to the site's time zone first; values
without an offset (plain dates, or HL7 times sent without one) are taken as already local.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

DEFAULT_SITE_TZ = "America/New_York"

_HL7_DTM = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})?(\d{2})?(?:\.\d+)?)?([+-]\d{4})?$")


def site_tz(name: str = DEFAULT_SITE_TZ) -> tzinfo:
    return ZoneInfo(name)


_FRACTION = re.compile(r"(T\d{2}:\d{2}:\d{2})\.(\d+)")


def _six_digit_fraction(text: str) -> str:
    """FHIR allows any number of fraction digits; Python 3.10's fromisoformat only takes 3 or 6.
    Pad or cut to 6 (sub-microsecond digits can't change the date)."""
    return _FRACTION.sub(lambda m: f"{m[1]}.{m[2][:6].ljust(6, '0')}", text, count=1)


def fhir_local_date(value: str, tz: tzinfo | None = None) -> str:
    """ISO date (YYYY-MM-DD) of a FHIR date/dateTime in the site time zone, or '' if unusable."""
    text = (value or "").strip()
    if len(text) < 10:
        return ""  # '', year-only or year-month: not a day
    try:
        if len(text) == 10:
            return date.fromisoformat(text).isoformat()
        moment = datetime.fromisoformat(_six_digit_fraction(text.replace("Z", "+00:00")))
    except ValueError:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz or site_tz())
    return moment.date().isoformat()


def hl7_local_date(text: str, tz: tzinfo | None = None) -> str:
    """ISO date of an HL7 DT/DTM. With an offset (e.g. -0500) and a tz, convert to site time;
    without an offset the value is taken as local, as HL7 intends."""
    m = _HL7_DTM.match((text or "").strip())
    if not m:
        return ""
    year, month, day, hour, minute, second, offset = m.groups()
    try:
        moment = datetime(  # noqa: DTZ001 - made aware just below when an offset was sent
            int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0)
        )
    except ValueError:
        return ""
    if offset and hour and tz is not None:
        sign = -1 if offset[0] == "-" else 1
        delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        moment = moment.replace(tzinfo=timezone(sign * delta)).astimezone(tz)
    return moment.date().isoformat()


_US_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def loose_local_date(value: str, tz: tzinfo | None = None) -> str:
    """Like fhir_local_date, but also takes a US MM/DD/YYYY string, which is how a free-text
    registration field (an Encounter custom attribute) often holds a date. '' if unusable."""
    found = fhir_local_date(value, tz)
    if found:
        return found
    m = _US_DATE.match((value or "").strip())
    if not m:
        return ""
    try:
        return date(int(m[3]), int(m[1]), int(m[2])).isoformat()
    except ValueError:
        return ""
