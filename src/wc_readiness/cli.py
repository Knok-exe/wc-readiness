"""Command line entry point: `wc-readiness`.

    wc-readiness --demo                              offline demo (bundled synthetic data)
    wc-readiness card --patient 12724066 --encounter 97954296 \\
        --adt examples/messages/a04_sandbox_12724066.hl7
    wc-readiness adt examples/messages/a04_wc_complete.hl7
    wc-readiness worklist
    wc-readiness update 3 --status in_progress --owner "Knok"
    wc-readiness audit

Exit codes for `card`: 0 READY, 2 not ready / unknown / unclear, 3 not a WC visit, 1 error.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from . import __version__
from .adt import DEFAULT_WC_FINANCIAL_CLASSES, AdtWcData, ParseError, parse_adt
from .card import ReadinessCard, build_card
from .dates import DEFAULT_SITE_TZ, site_tz
from .fhir_client import ORACLE_OPEN_SANDBOX, FhirClient, FhirSource, FixtureSource
from .fhir_facts import gather
from .render import render_card, render_worklist
from .worklist import USER_STATUSES, Worklist

DEFAULT_DB = "wc_worklist.db"


def demo_file(name: str) -> str:
    return resources.files("wc_readiness").joinpath("demo", name).read_text(encoding="utf-8")


def load_demo() -> tuple[FixtureSource, list[dict]]:
    data = json.loads(demo_file("demo_fhir.json"))
    return FixtureSource(data), data["cases"]


def read_adt(path: str | None) -> AdtWcData | None:
    if not path:
        return None
    return parse_adt(Path(path).read_text(encoding="utf-8", errors="replace"))


def make_card(source: FhirSource, patient: str, encounter: str | None, adt, args) -> ReadinessCard:
    facts = gather(source, patient, encounter, args.tz)
    return build_card(facts, adt, args.wc_classes, args.tz)


def emit(cards: list[ReadinessCard], wl: Worklist | None, fmt: str) -> None:
    if fmt == "json":
        out = {"cards": [c.to_dict() for c in cards]}
        if wl is not None:
            out["worklist"] = [vars(i) for i in wl.items()]
        print(json.dumps(out, indent=2, default=str))
        return
    for card in cards:
        print(render_card(card))
        print()
    if wl is not None:
        print(render_worklist(wl.items()))


def cmd_demo(args) -> int:
    """The demo uses an in-memory worklist unless --db is given, so it never leaves files."""
    source, cases = load_demo()
    with Worklist(args.db or ":memory:") as wl:
        cards = []
        for case in cases:
            card = make_card(
                source, case["patient"], case["encounter"], parse_adt(demo_file(case["adt"])), args
            )
            wl.sync(card)
            cards.append(card)
        if args.format == "text":
            print("Offline demo: invented patients, bundled FHIR fixture + synthetic HL7 v2 A04s.")
            print(f"Worklist: {args.db or 'in memory (gone when the demo ends)'}\n")
        emit(cards, wl, args.format)
    return 0


def cmd_card(args) -> int:
    adt = read_adt(args.adt)
    source = FhirClient(args.server, timeout=args.timeout)
    card = make_card(source, args.patient, args.encounter, adt, args)
    if args.no_worklist:
        emit([card], None, args.format)
    else:
        with Worklist(args.db) as wl:
            counts = wl.sync(card)
            if args.format == "text":
                if counts["skipped"]:
                    print("Worklist NOT updated: no encounter could be identified for this card.")
                else:
                    shown = {k: v for k, v in counts.items() if v}
                    print(f"Worklist {args.db}: {shown or 'no changes'}")
            emit([card], wl, args.format)
    if card.ready:
        return 0
    return 3 if card.wc_status == "not_wc" else 2


def cmd_adt(args) -> int:
    print(json.dumps(read_adt(args.file).to_dict(), indent=2))
    return 0


def cmd_worklist(args) -> int:
    with Worklist(args.db) as wl:
        items = wl.items(include_closed=args.all, status=args.status)
        if args.format == "json":
            print(json.dumps([vars(i) for i in items], indent=2))
        else:
            print(render_worklist(items))
    return 0


def cmd_update(args) -> int:
    with Worklist(args.db) as wl:
        try:
            item = wl.update(
                args.id, status=args.status, owner=args.owner, note=args.note, actor=args.actor
            )
        except (KeyError, ValueError) as exc:
            print(f"error: {exc.args[0]}", file=sys.stderr)
            return 1
        print(render_worklist([item]))
    return 0


def cmd_audit(args) -> int:
    with Worklist(args.db) as wl:
        for row in wl.audit():
            print(
                f"{row['at']}  item {row['item_id']!s:>3}  {row['action']:<9} {row['actor']:<12} "
                f"{row['detail']}"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wc-readiness",
        description="Workers' comp readiness card + coordinator worklist (Phase 0, synthetic "
        "data only, read-only FHIR).",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--demo", action="store_true", help="shortcut for `wc-readiness demo` (same options)"
    )
    db = argparse.ArgumentParser(add_help=False)
    db.add_argument("--db", default=DEFAULT_DB, help=f"SQLite worklist file (default {DEFAULT_DB})")
    fmt = argparse.ArgumentParser(add_help=False)
    fmt.add_argument("--format", choices=["text", "json"], default="text")
    rules = argparse.ArgumentParser(add_help=False)
    rules.add_argument(
        "--wc-class",
        dest="wc_classes",
        action="append",
        help="PV1-20 financial class value(s) that mean WC at your site (repeatable; default: "
        + ", ".join(sorted(DEFAULT_WC_FINANCIAL_CLASSES))
        + ")",
    )
    rules.add_argument(
        "--site-tz",
        default=DEFAULT_SITE_TZ,
        help=f"time zone used to turn timestamps into dates (default {DEFAULT_SITE_TZ})",
    )
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("demo", parents=[fmt, rules], help="offline demo, no network")
    d.add_argument("--db", default=None, help="keep the demo worklist in this file")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("card", parents=[db, fmt, rules], help="build a card from a FHIR server")
    c.add_argument("--patient", required=True, help="FHIR Patient id")
    c.add_argument("--encounter", help="FHIR Encounter id (default: latest for the patient)")
    c.add_argument("--adt", help="HL7 v2 ADT file to merge (synthetic!)")
    c.add_argument("--server", default=ORACLE_OPEN_SANDBOX, help="FHIR R4 base URL")
    c.add_argument("--timeout", type=float, default=60.0, help="seconds per request")
    c.add_argument("--no-worklist", action="store_true", help="don't write to the worklist DB")
    c.set_defaults(func=cmd_card)

    a = sub.add_parser("adt", help="show the WC fields found in an HL7 v2 ADT file (JSON)")
    a.add_argument("file")
    a.set_defaults(func=cmd_adt)

    w = sub.add_parser("worklist", parents=[db, fmt], help="show the coordinator worklist")
    w.add_argument("--all", action="store_true", help="include closed items")
    w.add_argument("--status", choices=("open", "in_progress", "done", "dismissed", "resolved"))
    w.set_defaults(func=cmd_worklist)

    u = sub.add_parser("update", parents=[db], help="change a work item")
    u.add_argument("id", type=int)
    u.add_argument("--status", choices=USER_STATUSES)
    u.add_argument("--owner")
    u.add_argument("--note", help="required when dismissing")
    u.add_argument("--actor", default="coordinator", help="who is making the change (audit)")
    u.set_defaults(func=cmd_update)

    au = sub.add_parser("audit", parents=[db], help="print the worklist audit log")
    au.set_defaults(func=cmd_audit)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--demo" in argv and not any(a in argv for a in ("-h", "--help", "--version")):
        argv.remove("--demo")  # `--demo --format json` == `demo --format json`
        argv.insert(0, "demo")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1
    args.wc_classes = (
        frozenset(args.wc_classes)
        if getattr(args, "wc_classes", None)
        else DEFAULT_WC_FINANCIAL_CLASSES
    )
    if hasattr(args, "site_tz"):
        try:
            args.tz = site_tz(args.site_tz)
        except (ZoneInfoNotFoundError, ValueError):
            print(f"error: unknown time zone {args.site_tz!r}", file=sys.stderr)
            return 1
    try:
        return args.func(args)
    except ParseError as exc:
        print(f"error: not a valid HL7 v2 message: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
