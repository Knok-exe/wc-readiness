"""The coordinator worklist, stored in SQLite.

Oracle Health's FHIR API has no Task resource (Pip's RESEARCH.md section 0), so the app keeps
its own to-do list. Each readiness gap that a person has to act on becomes a work item:

    open -> in_progress -> done        (someone fixed it)
                        -> dismissed   (not applicable; a note is required)
    resolved                           (set only by the system: the gap went away on its own)

Re-running the card for the same encounter "syncs" the list. The rules:
  * a new gap opens a new item;
  * a gap that was checked and is gone closes its open item as `resolved`;
  * a gap that could NOT be checked this run (a FHIR read failed, the ADT was for someone else)
    leaves its item exactly as it was: a timeout must never close anything;
  * on a case that has seen a matched ADT, a run without one can't open, reopen, flag or
    re-key the ADT-dependent items (their gaps are "provisional"): the worklist may only act on
    an input that was actually present this run;
  * the coordinator's decisions stick: a `dismissed` item stays dismissed while the gap's data
    (Gap.key: the values and sources it is about, not its wording) is the same, and a `done`
    item whose gap is still there gets a "still present" flag instead of silently reopening
    (setting it to done again acknowledges it: the flag stays off until the gap's data changes);
  * a closed item only reopens when something really changed: the gap's data changed since it
    was closed, or the gap went away and later came back;
  * a visit that is positively NOT workers' comp (matched ADT, non-WC financial class) closes
    its open WC items with that reason, flags in-progress ones instead of closing them, and
    leaves done/dismissed ones alone;
  * changes to an open item's severity or text are audited too.
Every change is written to audit_log (who, what, when), which a real deployment needs.

Site rule settings (WC financial classes, site time zone) are stored in the DB on first use,
so a re-run that forgets --wc-class can't turn a WC visit into a non-WC one.

Times are stored in UTC (ISO 8601). Only info-level gaps are left off the list. The DB holds
patient names, so a new file is created readable by its owner only (0600).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .card import WC_RULE_CODES, ReadinessCard

STATUSES = ("open", "in_progress", "done", "dismissed", "resolved")
USER_STATUSES = ("open", "in_progress", "done", "dismissed")  # `resolved` is system-only
ACTIVE = {"open", "in_progress"}
CLOSED = {"done", "dismissed", "resolved"}
WORKLIST_SEVERITIES = {"high", "medium", "low"}
STILL_PRESENT = "still_present"
NOT_WC_FLAG = "not_wc_visit"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_key      TEXT PRIMARY KEY,
    patient_id    TEXT NOT NULL,
    encounter_id  TEXT NOT NULL,
    patient_name  TEXT NOT NULL DEFAULT '',
    readiness     TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_checked  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key    TEXT NOT NULL REFERENCES cases(case_key),
    gap_code    TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    fix_hint    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',
    owner       TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (case_key, gap_code)
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER,
    case_key    TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL,
    at          TEXT NOT NULL
);
"""

# Columns added after v0.1.0's first commit; added in place to older databases.
MIGRATIONS = {
    "present": "INTEGER NOT NULL DEFAULT 1",  # was the gap there on the last checked run?
    "closed_message": "TEXT NOT NULL DEFAULT ''",  # unused since gap keys; kept for old DBs
    "flag": "TEXT NOT NULL DEFAULT ''",  # still_present | not_wc_visit
    # The gap's data key (Gap.key) on the latest run, and what it was when a person closed the
    # item. NULL closed_key = closed before keys existed: adopt the current key, don't reopen.
    "gap_key": "TEXT",
    "closed_key": "TEXT",
    # What the coordinator acknowledged by re-setting the status with a flag on it
    # ("still_present:<gap key>" or "not_wc_visit"): that flag isn't raised again until it
    # would be about something new.
    "ack_key": "TEXT",
}
CASE_MIGRATIONS = {
    "adt_seen": "INTEGER NOT NULL DEFAULT 0",  # has a matched ADT ever been merged for it?
}


def normalize_setting(key: str, value: str) -> str:
    """WC classes are matched case-insensitively, so they're stored and compared upper-cased."""
    if key == "wc_classes":
        return json.dumps(sorted({c.strip().upper() for c in json.loads(value)}))
    return value


class SettingsConflict(ValueError):
    """An explicit --wc-class / --site-tz differs from what this worklist was set up with."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class WorkItem:
    id: int
    case_key: str
    patient_name: str
    gap_code: str
    severity: str
    message: str
    fix_hint: str
    status: str
    owner: str
    note: str
    flag: str
    created_at: str
    updated_at: str


def _connect(path: str | Path, create: bool, read_only: bool = False) -> sqlite3.Connection:
    if str(path) == ":memory:":
        return sqlite3.connect(":memory:")
    target = Path(path)
    if read_only:
        return sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
    if not create:
        # Reading/updating an existing list: a mistyped --db must be an error, not a new,
        # empty worklist that says "nothing to do".
        return sqlite3.connect(f"{target.resolve().as_uri()}?mode=rw", uri=True)
    if not target.exists():
        os.close(os.open(target, os.O_CREAT | os.O_WRONLY, 0o600))  # owner-only: holds PHI
    return sqlite3.connect(str(target))


class Worklist:
    def __init__(
        self,
        path: str | Path = ":memory:",
        clock=utc_now,
        *,
        create: bool = True,
        read_only: bool = False,
    ):
        """`read_only` opens an existing file without creating, migrating or writing anything
        (card --no-worklist uses it to read a case's ADT history); sync then refuses to run."""
        self.read_only = read_only
        self.conn = _connect(path, create, read_only)
        self._now = clock
        self._pending: dict[str, tuple[str, str | None, str]] = {}  # settings not yet saved
        self.conn.row_factory = sqlite3.Row
        if read_only:
            return
        try:
            self.conn.executescript(SCHEMA)
            if self._missing_columns():
                # Take the write lock, then look again: two runs opening a new or old file at
                # the same moment would otherwise both try to add the same column.
                self.conn.execute("BEGIN IMMEDIATE")
                for table, column, ddl in self._missing_columns():
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    if column == "adt_seen":
                        # Older DBs didn't record it. Assume yes, so the safe rule applies
                        # (a run without ADT can't change ADT-dependent items).
                        self.conn.execute("UPDATE cases SET adt_seen = 1")
            self.conn.commit()
        except sqlite3.Error:
            self.conn.close()
            raise

    def _missing_columns(self) -> list[tuple[str, str, str]]:
        out = []
        for table, migrations in (("work_items", MIGRATIONS), ("cases", CASE_MIGRATIONS)):
            have = self._columns(table)
            out += [(table, c, ddl) for c, ddl in migrations.items() if c not in have]
        return out

    # ---- site settings ------------------------------------------------------------------------

    def setting(self, key: str) -> str | None:
        """The site setting in effect: one decided this run (not written yet), else stored."""
        if key in self._pending:
            return self._pending[key][0]
        if "settings" not in self._tables():
            return None  # a read-only, not yet migrated file
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def _is_legacy(self) -> bool:
        """A DB that already has cases but no saved settings: made before settings existed.
        Its cases may have been checked with a --wc-class we don't know, so defaults must not
        be saved into it silently."""
        tables = self._tables()
        if self._pending or (
            "settings" in tables and self.conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
        ):
            return False
        return "cases" in tables and (
            self.conn.execute("SELECT 1 FROM cases LIMIT 1").fetchone() is not None
        )

    def _tables(self) -> set[str]:
        return {
            r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def configure(
        self,
        values: dict[str, str],
        explicit: set[str],
        *,
        reconfigure: bool = False,
        accept_defaults: bool = False,
        actor: str = "system",
    ) -> dict[str, str]:
        """Return the effective site settings. The first run on a new DB saves `values`; later
        runs use what's saved. An explicit value that differs raises SettingsConflict unless
        `reconfigure`, which changes it (audited). A DB that predates settings needs every
        value passed explicitly once (or `accept_defaults` / `reconfigure`).

        Nothing is written here, so no write lock is held while FHIR is fetched: the values are
        saved by the next sync, inside its transaction (and dropped if the run fails before)."""
        legacy = self._is_legacy()
        out = {}
        for key, value in values.items():
            value = normalize_setting(key, value)
            stored = self.setting(key)
            stored = None if stored is None else normalize_setting(key, stored)
            if stored is None:
                if legacy and key not in explicit and not (accept_defaults or reconfigure):
                    label = {"wc_classes": "WC classes (--wc-class)", "site_tz": "time zone"}
                    raise SettingsConflict(
                        "this worklist was created before site settings were saved, so it has "
                        f"no record of the {label.get(key, key)} it was checked with. Pass "
                        "--wc-class "
                        "and --site-tz once to record them, or --accept-defaults if the defaults "
                        "are right."
                    )
                self._pending[key] = (value, None, actor)
                out[key] = value
            elif value != stored and key in explicit:
                if not reconfigure:
                    raise SettingsConflict(
                        f"this worklist uses {key} {stored} (site setting); you passed {value}. "
                        "Pass --reconfigure to change it."
                    )
                self._pending[key] = (value, stored, actor)
                out[key] = value
            else:
                out[key] = stored
        return out

    def _flush_settings(self) -> None:
        """Write the settings decided by configure(). Runs inside sync's write transaction, so
        the re-read below can't race: if another run saved a different value since configure()
        looked (e.g. two first runs on a new DB), this run's cards were built with stale
        settings and must not be filed. Nothing is written then (the transaction rolls back)."""
        pending, self._pending = self._pending, {}
        for key, (value, stored, actor) in pending.items():
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            now = None if row is None else normalize_setting(key, row["value"])
            if now == value:
                continue  # another run already saved the same value: nothing to do
            if now != stored:
                raise SettingsConflict(
                    f"another run changed the site setting {key} while this one was fetching "
                    f"(this run expected {stored!r}, the worklist now has {now!r}). Nothing was "
                    "written; re-run."
                )
            self.conn.execute(
                "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                "SET value = excluded.value",
                (key, value),
            )
            detail = f"{key}: {stored!r} -> {value!r}" if stored else f"{key} = {value!r}"
            self._audit(None, "settings", "settings", detail, actor)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Worklist:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _audit(self, item_id, case_key, action, detail, actor) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (item_id, case_key, action, detail, actor, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, case_key, action, detail, actor, self._now()),
        )

    def _set(self, item_id: int, **values) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)  # keys are fixed names from this module
        self.conn.execute(f"UPDATE work_items SET {sets} WHERE id = ?", (*values.values(), item_id))

    def _check_settings(self, card: ReadinessCard) -> None:
        """A card built with other site rules than this worklist's must not change it (a
        forgotten --wc-class would otherwise turn WC visits into non-WC ones)."""
        used = {"site_tz": card.site_tz}
        if card.wc_classes:
            used["wc_classes"] = json.dumps(sorted(card.wc_classes))
        for key, value in used.items():
            if not value:
                continue
            stored = self.setting(key)
            stored = None if stored is None else normalize_setting(key, stored)
            if stored is None:
                if self._is_legacy():
                    raise SettingsConflict(
                        "this worklist was created before site settings were saved: record "
                        "them first (CLI: --wc-class/--site-tz or --accept-defaults; code: "
                        "Worklist.configure)"
                    )
                self.configure({key: value}, {key})
            elif stored != value:
                raise SettingsConflict(
                    f"this card was built with {key} {value}, but this worklist uses {stored} "
                    "(site setting). Pass --reconfigure to change it."
                )

    def sync(self, card: ReadinessCard, actor: str = "system") -> dict[str, int]:
        """Bring the work items for this card's case in line with its current gaps."""
        counts = dict.fromkeys(
            ("opened", "reopened", "resolved", "changed", "unchanged", "kept_closed",
             "flagged", "unchecked", "skipped"),
            0,
        )  # fmt: skip
        if self.read_only:
            raise sqlite3.OperationalError("this worklist was opened read-only")
        if not card.has_case:
            counts["skipped"] = 1  # no encounter id: we'd file it under the wrong case
            with self.conn:
                self._begin()
                self._flush_settings()
            return counts
        now = self._now()
        key = card.case_key
        adt_now = card.adt is not None and card.adt_matched
        with self.conn:
            self._begin()  # take the write lock first, so the reads below can't go stale
            self._check_settings(card)
            self._flush_settings()
            # Decide readiness from the worklist state before this run changes it. Held and
            # unchecked items (the only ones assess counts) are left exactly as they are below.
            adt_seen = self.assess(card)
            self.conn.execute(
                "INSERT INTO cases (case_key, patient_id, encounter_id, patient_name, readiness,"
                " first_seen, last_checked, adt_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(case_key) DO UPDATE "
                "SET readiness = excluded.readiness, last_checked = excluded.last_checked, "
                "adt_seen = MAX(cases.adt_seen, excluded.adt_seen), "
                "patient_name = CASE WHEN excluded.patient_name = '' THEN cases.patient_name "
                "ELSE excluded.patient_name END",
                (
                    key,
                    card.patient_id,
                    card.encounter_id,
                    card.patient_name,
                    card.status,
                    now,
                    now,
                    int(adt_now),
                ),
            )
            gaps = [g for g in card.gaps if g.severity in WORKLIST_SEVERITIES]
            # Provisional gaps were computed without the ADT. On a case that has had an ADT,
            # they are "couldn't check": no open, reopen, flag or key change.
            held = {g.code for g in gaps if g.provisional} if adt_seen else set()
            counts["unchecked"] += len(held)
            current = {g.code: g for g in gaps if g.code not in held}
            existing = {
                row["gap_code"]: row
                for row in self.conn.execute("SELECT * FROM work_items WHERE case_key = ?", (key,))
            }
            for code, gap in current.items():
                row = existing.get(code)
                if row is None:
                    cur = self.conn.execute(
                        "INSERT INTO work_items (case_key, gap_code, severity, message, fix_hint,"
                        " gap_key, status, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                        (key, code, gap.severity, gap.message, gap.fix_hint, gap.key, now, now),
                    )
                    self._audit(cur.lastrowid, key, "opened", code, actor)
                    counts["opened"] += 1
                    continue
                self._sync_present(row, gap, key, now, actor, counts)
            for code, row in existing.items():
                if code in current or code in held:
                    continue
                if code in card.unchecked:
                    counts["unchecked"] += 1  # couldn't check: leave it exactly as it is
                    continue
                if card.wc_status == "not_wc" and code in WC_RULE_CODES:
                    self._close_not_wc(row, card, key, now, actor, counts)
                    continue
                if row["present"]:
                    self._set(row["id"], present=0)
                if row["status"] in ACTIVE:
                    self._set(row["id"], status="resolved", flag="", updated_at=now)
                    self._audit(row["id"], key, "resolved", "gap no longer present", actor)
                    counts["resolved"] += 1
                elif row["flag"] == STILL_PRESENT:
                    self._set(row["id"], flag="", updated_at=now)
                    self._audit(row["id"], key, "confirmed", "gap now gone after done", actor)
        return counts

    def _begin(self) -> None:
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")

    def assess(self, card: ReadinessCard) -> bool:
        """Fill in what the worklist knows about this card's case, without writing anything.
        Returns whether the case has had a matched ADT before.

        If it has and this run has none, the run's provisional (ADT-based) gaps can't be
        re-checked: they don't count for readiness (card.provisional_ignored), and instead the
        case's high items for those gaps that the last ADT run left open, in progress, or done
        but still present (card.held_blocking) do. So the verdict is the same whether or not
        the worklist is written (sync and card --no-worklist both call this)."""
        card.provisional_ignored = False
        card.held_blocking = []
        card.done_unconfirmed = []
        tables = self._tables()
        if not card.has_case or "cases" not in tables:
            return False
        cols = self._columns("cases")
        row = self.conn.execute(
            f"SELECT {'adt_seen' if 'adt_seen' in cols else '1'} AS adt_seen "
            "FROM cases WHERE case_key = ?",
            (card.case_key,),
        ).fetchone()
        adt_seen = bool(row and row["adt_seen"])  # older files: assume yes, as the migration does
        if not adt_seen or (card.adt is not None and card.adt_matched):
            return adt_seen
        card.provisional_ignored = True
        held = {g.code for g in card.gaps if g.provisional} | set(card.unchecked)
        flag = "flag" if "flag" in self._columns("work_items") else "''"
        rows = self.conn.execute(
            f"SELECT gap_code FROM work_items WHERE case_key = ? AND severity = 'high' "
            f"AND (status IN ('open', 'in_progress') OR {flag} = ?) ORDER BY gap_code",
            (card.case_key, STILL_PRESENT),
        )
        card.held_blocking = [r["gap_code"] for r in rows if r["gap_code"] in held]
        # `done` items whose gap no run with an ADT has seen gone yet (present is cleared only
        # when a run that could check the gap found it gone; older files: assume not).
        item_cols = self._columns("work_items")
        present = "present" if "present" in item_cols else "1"
        rows = self.conn.execute(
            f"SELECT gap_code FROM work_items WHERE case_key = ? AND status = 'done' "
            f"AND {present} = 1 ORDER BY gap_code",
            (card.case_key,),
        )
        card.done_unconfirmed = [
            r["gap_code"]
            for r in rows
            if r["gap_code"] in held and r["gap_code"] not in card.held_blocking
        ]
        return adt_seen

    def _close_not_wc(self, row, card, key, now, actor, counts) -> None:
        """The visit is positively not WC (a matched ADT with a non-WC financial class).
        * open items are closed as resolved, with their own audit reason;
        * in_progress items are NOT closed: someone is working them, so they get a
          not_wc_visit flag and stay on the list for that person to decide;
        * done/dismissed items are left alone (and not marked "gone"), so a later flip back
          to WC doesn't reopen them just because of this run."""
        reason = next((g.message for g in card.gaps if g.code == "NOT_WC_VISIT"), "not a WC visit")
        if row["status"] == "open":
            self._set(row["id"], status="resolved", present=0, flag="", updated_at=now)
            self._audit(row["id"], key, "resolved", f"not a WC visit: {reason}", actor)
            counts["resolved"] += 1
        elif (
            row["status"] == "in_progress"
            and row["flag"] != NOT_WC_FLAG
            and row["ack_key"] != NOT_WC_FLAG  # the owner already saw it and kept working
        ):
            self._set(row["id"], flag=NOT_WC_FLAG, updated_at=now)
            self._audit(
                row["id"], key, "flagged", f"left in progress, but not a WC visit: {reason}", actor
            )
            counts["flagged"] += 1
        else:
            counts["kept_closed"] += 1

    def _sync_present(self, row, gap, key, now, actor, counts) -> None:
        status = row["status"]
        changed = gap.message != row["message"] or gap.severity != row["severity"]
        if row["gap_key"] != gap.key:
            self._set(row["id"], gap_key=gap.key)
        if status in ACTIVE:
            if row["flag"] == NOT_WC_FLAG or row["ack_key"] == NOT_WC_FLAG:  # WC visit again
                self._set(row["id"], flag="", ack_key=None, updated_at=now)
                self._audit(row["id"], key, "unflagged", "WC visit again", actor)
            if changed:
                self._set(
                    row["id"],
                    severity=gap.severity,
                    message=gap.message,
                    fix_hint=gap.fix_hint,
                    present=1,
                    updated_at=now,
                )
                self._audit(row["id"], key, "changed", _diff(row, gap), actor)
                counts["changed"] += 1
            else:
                if not row["present"]:
                    self._set(row["id"], present=1)
                counts["unchanged"] += 1
            return

        came_back = not row["present"]
        if status != "resolved" and row["closed_key"] is None:
            # Closed before gap keys existed (or by an older version): adopt, don't reopen.
            self._set(row["id"], closed_key=gap.key)
            data_changed = False
        else:
            data_changed = status != "resolved" and gap.key != row["closed_key"]
        if status == "resolved" or came_back or data_changed:
            why = (
                "gap came back"
                if status == "resolved" or came_back
                else f"gap data changed since it was closed ({row['closed_key']!r} -> {gap.key!r})"
            )
            self._set(
                row["id"],
                status="open",
                severity=gap.severity,
                message=gap.message,
                fix_hint=gap.fix_hint,
                present=1,
                flag="",
                closed_key=None,
                ack_key=None,
                updated_at=now,
            )
            detail = f"was {status}; {why}"
            if changed:
                detail += f"; {_diff(row, gap)}"
            self._audit(row["id"], key, "reopened", detail, actor)
            counts["reopened"] += 1
        elif (
            status == "done"
            and row["flag"] != STILL_PRESENT
            and row["ack_key"] != f"{STILL_PRESENT}:{gap.key}"  # acknowledged, same data
        ):
            self._set(row["id"], flag=STILL_PRESENT, updated_at=now)
            self._audit(
                row["id"], key, "flagged", "marked done but the gap is still present", actor
            )
            counts["flagged"] += 1
        else:
            counts["kept_closed"] += 1  # dismissed (or already flagged): respect the decision

    def update(
        self,
        item_id: int,
        *,
        status: str | None = None,
        owner: str | None = None,
        note: str | None = None,
        actor: str = "coordinator",
    ) -> WorkItem:
        if status is not None and status not in USER_STATUSES:
            raise ValueError(
                f"status must be one of {', '.join(USER_STATUSES)} ('resolved' is set by the "
                "system when a gap goes away)"
            )
        row = self.conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"no work item {item_id}")
        if status == "dismissed" and not (note or row["note"]).strip():
            raise ValueError("dismissing needs a --note saying why")
        changes = {
            k: v
            for k, v in (("status", status), ("owner", owner), ("note", note))
            if v is not None and v != row[k]
        }
        clear_flag = status is not None and status == row["status"] and row["flag"]
        if changes or clear_flag:
            extra: dict = {"updated_at": self._now()}
            if "status" in changes or clear_flag:
                extra["flag"] = ""
                extra["ack_key"] = None
            if clear_flag and "status" not in changes:
                # Same status again = "I've seen the flag": remember what was acknowledged.
                extra["ack_key"] = (
                    f"{STILL_PRESENT}:{row['gap_key'] or ''}"
                    if row["flag"] == STILL_PRESENT
                    else row["flag"]
                )
            if "status" in changes:
                # Remember the gap's data when a person closes it, so a real change reopens it.
                extra["closed_key"] = row["gap_key"] if status in CLOSED else None
            with self.conn:
                self._set(item_id, **changes, **extra)
                detail = "; ".join(f"{k}: {row[k]!r} -> {v!r}" for k, v in changes.items())
                if clear_flag and "status" not in changes:
                    detail = "; ".join(p for p in (detail, f"flag {row['flag']!r} cleared") if p)
                self._audit(item_id, row["case_key"], "updated", detail, actor)
        return self.get(item_id)

    def get(self, item_id: int) -> WorkItem:
        items = self.items(item_id=item_id, include_closed=True)
        if not items:
            raise KeyError(f"no work item {item_id}")
        return items[0]

    def items(
        self,
        *,
        include_closed: bool = False,
        status: str | None = None,
        case_key: str | None = None,
        item_id: int | None = None,
    ) -> list[WorkItem]:
        """Open/in-progress items, plus `done` items flagged still present (they need a look)."""
        sql = "SELECT w.*, c.patient_name FROM work_items w JOIN cases c USING (case_key) WHERE 1=1"
        args: list = []
        if item_id is not None:
            sql += " AND w.id = ?"
            args.append(item_id)
        if status:
            sql += " AND w.status = ?"
            args.append(status)
        elif not include_closed:
            sql += " AND (w.status IN ('open', 'in_progress') OR w.flag != '')"
        if case_key:
            sql += " AND w.case_key = ?"
            args.append(case_key)
        sql += (
            " ORDER BY CASE w.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
            " w.created_at, w.id"
        )
        return [
            WorkItem(**{k: r[k] for k in WorkItem.__dataclass_fields__})
            for r in self.conn.execute(sql, args)
        ]

    def audit(self, case_key: str | None = None) -> Iterable[sqlite3.Row]:
        if case_key:
            return self.conn.execute(
                "SELECT * FROM audit_log WHERE case_key = ? ORDER BY id", (case_key,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()


def _diff(row, gap) -> str:
    parts = []
    if gap.severity != row["severity"]:
        parts.append(f"severity: {row['severity']!r} -> {gap.severity!r}")
    if gap.message != row["message"]:
        parts.append(f"message: {row['message']!r} -> {gap.message!r}")
    return "; ".join(parts) or "no change"
