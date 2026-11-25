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
  * the coordinator's decisions stick: a `dismissed` item stays dismissed while the gap's data
    (Gap.key: the values and sources it is about, not its wording) is the same, and a `done`
    item whose gap is still there gets a "still present" flag instead of silently reopening
    (setting it to done again clears the flag);
  * a closed item only reopens when something really changed: the gap's data changed since it
    was closed, or the gap went away and later came back;
  * a visit that is positively NOT workers' comp (matched ADT, non-WC financial class) closes
    its open WC items with that reason, flags in-progress ones instead of closing them, and
    leaves done/dismissed ones alone;
  * changes to an open item's severity or text are audited too.
Every change is written to audit_log (who, what, when), which a real deployment needs.

Times are stored in UTC (ISO 8601). Only info-level gaps are left off the list.
"""

from __future__ import annotations

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
}


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


class Worklist:
    def __init__(self, path: str | Path = ":memory:", clock=utc_now):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(work_items)")}
        for column, ddl in MIGRATIONS.items():
            if column not in have:
                self.conn.execute(f"ALTER TABLE work_items ADD COLUMN {column} {ddl}")
        self.conn.commit()
        self._now = clock

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

    def sync(self, card: ReadinessCard, actor: str = "system") -> dict[str, int]:
        """Bring the work items for this card's case in line with its current gaps."""
        counts = dict.fromkeys(
            ("opened", "reopened", "resolved", "changed", "unchanged", "kept_closed",
             "flagged", "unchecked", "skipped"),
            0,
        )  # fmt: skip
        if not card.has_case:
            counts["skipped"] = 1  # no encounter id: we'd file it under the wrong case
            return counts
        now = self._now()
        key = card.case_key
        with self.conn:
            self.conn.execute(
                "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(case_key) DO UPDATE "
                "SET readiness = excluded.readiness, last_checked = excluded.last_checked, "
                "patient_name = CASE WHEN excluded.patient_name = '' THEN cases.patient_name "
                "ELSE excluded.patient_name END",
                (key, card.patient_id, card.encounter_id, card.patient_name, card.status, now, now),
            )
            current = {g.code: g for g in card.gaps if g.severity in WORKLIST_SEVERITIES}
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
                if code in current:
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
        elif row["status"] == "in_progress" and row["flag"] != NOT_WC_FLAG:
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
            if row["flag"] == NOT_WC_FLAG:  # it's a WC visit again
                self._set(row["id"], flag="", updated_at=now)
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
                updated_at=now,
            )
            detail = f"was {status}; {why}"
            if changed:
                detail += f"; {_diff(row, gap)}"
            self._audit(row["id"], key, "reopened", detail, actor)
            counts["reopened"] += 1
        elif status == "done" and row["flag"] != STILL_PRESENT:
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
