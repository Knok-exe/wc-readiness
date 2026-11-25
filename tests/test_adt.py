import pytest

from wc_readiness.adt import ParseError, hl7_date, is_wc_financial_class, parse_adt

MSH = "MSH|^~\\&|REG|HOSP|APP|HOSP|20261117101500||ADT^A04^ADT_A01|X1|P|2.5.1"


def test_complete_a04_fields(complete_a04):
    d = parse_adt(complete_a04)
    assert d.message_type == "ADT^A04"
    assert d.mrn == "700100"
    assert d.patient_name == "Testpatient, Riley"
    assert d.birth_date == "1988-04-12"
    assert d.visit_number == "FIN700100A"
    assert d.financial_class == "WC"
    assert (d.carrier.value, d.carrier.source) == ("Example Mutual WC Insurance", "IN1-4")
    assert (d.claim_number.value, d.claim_number.source) == ("WC-2026-004512", "IN1-36")
    assert (d.employer.value, d.employer.source) == ("Northwind Example Logistics", "IN2-3")
    assert d.injury_date == "2026-11-17"
    assert d.job_related == "Y"
    assert d.accident_code == "WPA"
    assert d.warnings == []


def test_incomplete_a04_placeholder_claim_and_no_employer(incomplete_a04):
    d = parse_adt(incomplete_a04)
    assert d.claim_number is None
    assert any("placeholder claim number" in w for w in d.warnings)
    assert d.employer is None
    assert d.job_related == ""
    assert d.injury_date == "2026-11-20"


def test_cr_line_endings_parse_the_same(complete_a04):
    assert parse_adt(complete_a04.replace("\n", "\r")) == parse_adt(complete_a04)


@pytest.mark.parametrize(
    "segments, value, source",
    [
        (["GT1|1||Doe^Jo|" + "|" * 47 + "Acme Org"], "Acme Org", "GT1-51"),
        (["GT1|1||Doe^Jo" + "|" * 13 + "Acme Person Employer"], "Acme Person Employer", "GT1-16"),
        (["IN1|1|P|C|Carrier" + "|" * 7 + "Group Employer"], "Group Employer", "IN1-11"),
        (["IN2|||Only Name Given"], "Only Name Given", "IN2-3"),
    ],
)
def test_employer_fallback_order(segments, value, source):
    msg = "\r".join([MSH, "PID|1||123^^^H^MR||Doe^Jo", *segments])
    d = parse_adt(msg)
    assert (d.employer.value, d.employer.source) == (value, source)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("20261117", "2026-11-17"),
        ("20261117083000-0500", "2026-11-17"),
        ("20261117083000.1234+0000", "2026-11-17"),
        ("202611", ""),
        ("20261332", ""),
        ("", ""),
        ("garbage", ""),
    ],
)
def test_hl7_date(raw, expected):
    assert hl7_date(raw) == expected


def test_invalid_injury_date_is_a_warning():
    d = parse_adt("\r".join([MSH, "PID|1||1", "ACC|20261399"]))
    assert d.injury_date == ""
    assert any("ACC-1" in w for w in d.warnings)


def test_non_adt_message_warns():
    d = parse_adt(MSH.replace("ADT^A04^ADT_A01", "ORU^R01") + "\rPID|1||1")
    assert any("expected an ADT" in w for w in d.warnings)


def test_missing_pid_warns():
    assert any("PID" in w for w in parse_adt(MSH).warnings)


def test_not_hl7_raises():
    with pytest.raises(ParseError):
        parse_adt("hello world")


def test_financial_class_matching_is_case_insensitive():
    assert is_wc_financial_class(" wc ", {"WC"})
    assert not is_wc_financial_class("COMM", {"WC"})
