"""Regression tests for Cipher's approval round of 383112f: NEW-4 (next-link path hardening,
probe t04) and NEW-5 (an unconfirmed `done` is called out on a no-ADT run, probe t01)."""

import json

import pytest

from wc_readiness import cli
from wc_readiness.fhir_client import ORACLE_OPEN_SANDBOX, FixtureSource, same_server
from wc_readiness.worklist import Worklist

P1, E1 = "demo-wc-1001", "demo-enc-1001"
B = ORACLE_OPEN_SANDBOX


def seg(text, name, idx, value):
    out = []
    for line in text.replace("\r", "\n").split("\n"):
        f = line.split("|")
        if f and f[0] == name:
            f += [""] * (idx + 1 - len(f))
            f[idx] = value
        out.append("|".join(f))
    return "\r".join(x for x in out if x)


# ---- NEW-4 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/%c0%ae%c0%ae/other",  # overlong UTF-8 '.'
        "/%e0%80%ae%e0%80%ae/o",  # 3-byte overlong
        "/\uff0e\uff0e/other",  # fullwidth dots, raw
        "/%EF%BC%8E%EF%BC%8E/other",  # fullwidth dots, encoded
        "/\u2025/other",  # two-dot leader
        "/.../other",
        "/..%20/other",
        "/..%00/other",
        "/..%09/o",
        "/.\t./other",
        "/..\n/other",
        "//evil.com/x",
        "/x//y",
        "/..;x=1/o",
        "/....;/o",
        "/caf%C3%A9",  # non-ASCII in general
        "/a%20b",
        "/a%7Cb",  # '|' is not a plain URL path character
    ],
)
def test_new4_next_link_paths_rejected(path):
    assert same_server(f"{B}{path}", B) is False


@pytest.mark.parametrize(
    "url",
    [
        # Shapes of the Oracle/Cerner open sandbox's own Bundle.link[next] (synthetic values).
        f"{B}/Condition?patient=12724066&_count=50&-pageContext=eyJhbGciOiJub25lIn0%3D%3D"
        "&-pageDirection=NEXT",
        f"{B}/Encounter?patient=12724066&_count=20&-pageContext=abc.DEF_123-~&-pageDirection=NEXT",
        f"{B}/Coverage?patient=12724066&-pageContext=x%2Fy%2Bz",
        f"{B}/Observation?patient=12724066&category=social-history&-pageContext=1",
        f"{B}/Patient/12724066",
        f"{B}/",
        f"{B}/Encounter?x=../../y#/../z",  # dots in the query/fragment aren't the path
        f"{B}/x;jsessionid=AB12",
        f"https://FHIR-OPEN.cerner.com:443{B.split('.com', 1)[1]}/Encounter?page=2",
    ],
)
def test_new4_legit_sandbox_next_links_still_pass(url):
    assert same_server(url, B) is True


# ---- NEW-5 ---------------------------------------------------------------------------------------


@pytest.fixture
def offline(monkeypatch, demo_data):
    monkeypatch.setattr(cli, "FhirClient", lambda *a, **k: FixtureSource(demo_data))
    return demo_data


@pytest.fixture
def setup(offline, tmp_path, complete_a04, capsys):
    """An ADT run with no employer (EMPLOYER_MISSING, high) and no claim #; returns helpers."""
    text = "\r".join(
        x
        for x in complete_a04.replace("\r", "\n").split("\n")
        if x and not x.startswith(("IN2", "GT1"))
    )
    noemp = tmp_path / "noemp.hl7"
    noemp.write_text(seg(text, "IN1", 36, ""))
    full = tmp_path / "full.hl7"
    full.write_text(complete_a04)
    db = str(tmp_path / "w.db")
    base = ["card", "--patient", P1, "--encounter", E1, "--db", db]

    def run(*extra):
        rc = cli.main([*base, *extra])
        out = capsys.readouterr().out
        line = next((x for x in out.splitlines() if x.startswith("WC READINESS CARD")), out)
        return rc, line, out

    def update(code, status):
        with Worklist(db, create=False) as wl:
            iid = next(i.id for i in wl.items(include_closed=True) if i.gap_code == code)
        assert cli.main(["update", "--db", db, str(iid), "--status", status]) == 0
        capsys.readouterr()

    assert run("--adt", str(noemp))[0] == 2
    return run, update, noemp, full


NOTE = "[1 item(s) marked done not yet confirmed by an ADT: EMPLOYER_MISSING]"


def test_new5_done_not_confirmed_is_called_out(setup):
    run, update, _, _ = setup
    update("EMPLOYER_MISSING", "done")
    rc, line, _ = run()
    assert rc == 0 and line.startswith("WC READINESS CARD   READY [") and line.endswith(NOTE)
    rc, line, _ = run("--no-worklist")
    assert rc == 0 and line.endswith(NOTE)
    rc, _, out = run("--format", "json")
    card = json.loads(out)["cards"][0]
    assert rc == 0 and card["ready"] is True
    assert card["done_unconfirmed"] == ["EMPLOYER_MISSING"] and card["status"].endswith(NOTE)


def test_new5_acknowledged_still_present_is_also_unconfirmed(setup):
    run, update, noemp, _ = setup
    update("EMPLOYER_MISSING", "done")
    assert run("--adt", str(noemp))[0] == 2  # still there: flagged still_present
    rc, line, _ = run()
    assert rc == 2 and "EMPLOYER_MISSING; no matched ADT" in line
    assert "not yet confirmed" not in line  # listed once, as blocking
    update("EMPLOYER_MISSING", "done")  # acknowledged
    rc, line, _ = run()
    assert rc == 0 and line.endswith(NOTE)


def test_new5_no_note_once_an_adt_run_confirms_it(setup):
    run, update, _, full = setup
    update("EMPLOYER_MISSING", "done")
    assert run("--adt", str(full))[0] == 0  # employer now in the ADT: gap gone
    rc, line, _ = run()
    assert rc == 0 and "not yet confirmed" not in line
    assert line.startswith("WC READINESS CARD   READY")


def test_new5_no_note_with_an_adt_or_for_other_states(setup):
    run, update, noemp, _ = setup
    rc, line, _ = run()  # open item: blocking, no done note
    assert rc == 2 and "not yet confirmed" not in line
    update("EMPLOYER_MISSING", "done")
    rc, line, _ = run("--adt", str(noemp))  # with an ADT the done item is checked, not noted
    assert rc == 2 and "not yet confirmed" not in line


# ---- found while re-running s02: two runs migrating the same file at once ---------------------


def test_concurrent_open_does_not_add_the_same_column_twice(tmp_path, monkeypatch):
    import sqlite3

    from wc_readiness import worklist

    db = tmp_path / "fresh.db"
    with sqlite3.connect(db) as con:
        con.executescript(worklist.SCHEMA)  # tables without the migrated columns
    con.close()
    orig = Worklist._missing_columns
    calls = []

    def stale_then_real(self):
        calls.append(1)
        if len(calls) == 1:
            stale = orig(self)  # what this run saw before taking the lock...
            Worklist(db).close()  # ...while another run migrated the file
            return stale
        return orig(self)

    monkeypatch.setattr(Worklist, "_missing_columns", stale_then_real)
    with Worklist(db) as wl:  # used to fail: duplicate column name
        assert {"present", "ack_key"} <= wl._columns("work_items")
        assert "adt_seen" in wl._columns("cases")
