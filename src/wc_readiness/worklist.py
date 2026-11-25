"""The coordinator worklist, stored in SQLite.

Oracle Health's FHIR API has no Task resource (Pip's RESEARCH.md section 0), so the app keeps
its own to-do list. Each readiness gap that a person has to act on becomes a work item:

    open -> in_progress -> done        (someone fixed it)
                        -> dismissed   (not applicable, with a note)

Re-running the card for the same encounter "syncs" the list:
  * a new gap opens a new item,
  * a gap that disappeared (someone fixed the source data) is closed as resolved by "system",
  * a gap that comes back after being closed is reopened.
Every change is written to an audit_log table (who, what, when), which a real deployment needs.

Times are stored in UTC (ISO 8601). Only info-level gaps are left off the list.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .card import ReadinessCard

STATUSES = ("open", "in_progress", "done", "dismissed", "resolved")
CLOSED = {"done", "dismissed", "resolved"}
WORKLIST_SEVERITIES = {"high", "medium", "low"}

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
    created_at: str
    updated_at: str


class Worklist:
    def __init__(self, path: str | Path = ":memory:", clock=utc_now):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._now = clock

    def close(self) -> None:
        self.conn.close()

    def _audit(self, item_id, case_key, action, detail, actor) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (item_id, case_key, action, detail, actor, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, case_key, action, detail, actor, self._now()),
        )

    def sync(self, card: ReadinessCard, actor: str = "system") -> dict[str, int]:
        """Bring the work items for this card's case in line with its current gaps."""
        now = self._now()
        key = card.case_key
        counts = {"opened": 0, "reopened": 0, "resolved": 0, "unchanged": 0}
        with self.conn:
            self.conn.execute(
                "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(case_key) DO UPDATE "
                "SET readiness = excluded.readiness, last_checked = excluded.last_checked, "
                "patient_name = excluded.patient_name",
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
                        " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                        (key, code, gap.severity, gap.message, gap.fix_hint, now, now),
                    )
                    self._audit(cur.lastrowid, key, "opened", code, actor)
                    counts["opened"] += 1
                elif row["status"] in CLOSED:
                    self.conn.execute(
                        "UPDATE work_items SET status='open', message=?, severity=?, updated_at=?"
                        " WHERE id=?",
                        (gap.message, gap.severity, now, row["id"]),
                    )
                    self._audit(row["id"], key, "reopened", f"was {row['status']}", actor)
                    counts["reopened"] += 1
                else:
                    self.conn.execute(
                        "UPDATE work_items SET message=?, severity=? WHERE id=?",
                        (gap.message, gap.severity, row["id"]),
                    )
                    counts["unchanged"] += 1
            for code, row in existing.items():
                if code not in current and row["status"] not in CLOSED:
                    self.conn.execute(
                        "UPDATE work_items SET status='resolved', updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    self._audit(row["id"], key, "resolved", "gap no longer present", actor)
                    counts["resolved"] += 1
        return counts

    def update(
        self,
        item_id: int,
        *,
        status: str | None = None,
        owner: str | None = None,
        note: str | None = None,
        actor: str = "coordinator",
    ) -> WorkItem:
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        row = self.conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"no work item {item_id}")
        changes = {
            k: v
            for k, v in (("status", status), ("owner", owner), ("note", note))
            if v is not None and v != row[k]
        }
        if changes:
            with self.conn:
                sets = ", ".join(f"{k} = ?" for k in changes)  # keys come from the tuple above
                self.conn.execute(
                    f"UPDATE work_items SET {sets}, updated_at = ? WHERE id = ?",
                    (*changes.values(), self._now(), item_id),
                )
                detail = "; ".join(f"{k}: {row[k]!r} -> {v!r}" for k, v in changes.items())
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
        sql = "SELECT w.*, c.patient_name FROM work_items w JOIN cases c USING (case_key) WHERE 1=1"
        args: list = []
        if item_id is not None:
            sql += " AND w.id = ?"
            args.append(item_id)
        if status:
            sql += " AND w.status = ?"
            args.append(status)
        elif not include_closed:
            sql += " AND w.status NOT IN ('done', 'dismissed', 'resolved')"
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
