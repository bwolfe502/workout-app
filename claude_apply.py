"""Inbound Claude response: extract → validate → diff → apply.

Pipeline (one direction, no surprises):

    raw paste                              str
       └─ extract_json_block            → str (JSON content)
           └─ json.loads + jsonschema    → dict (validated)
               └─ build_diff             → list[DiffEntry]
                   └─ apply              → SQLite transaction
                       └─ ai_interactions row written for rollback

Each step can fail loudly with a structured `ApplyError`. Nothing is
written until `apply()` is called.

The diff is computed *against the live DB* at apply time, not at preview
time, so the user gets the most up-to-date picture even if state changed
between paste and click.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import jsonschema

from claude_bundle import RESPONSE_SCHEMA


# ---- errors ----------------------------------------------------------------


class ApplyError(Exception):
    """Raised at any pipeline stage with a user-facing message."""


# ---- extraction ------------------------------------------------------------


_FENCED_JSON_RE = re.compile(
    r"```(?:json|JSON)?\s*\n(.*?)\n```",
    re.DOTALL,
)


def extract_json_block(raw: str) -> str:
    """Pull the first fenced ```json block out of Claude's response."""
    if not raw or not raw.strip():
        raise ApplyError("Response is empty.")
    m = _FENCED_JSON_RE.search(raw)
    if not m:
        # Fallback: maybe the whole thing is bare JSON.
        stripped = raw.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped
        raise ApplyError(
            "No fenced ```json block found. Claude must wrap the response in "
            "a triple-backtick json fence."
        )
    return m.group(1).strip()


# ---- validation ------------------------------------------------------------


def parse_and_validate(json_text: str) -> dict[str, Any]:
    """Decode + schema-validate. Raises ApplyError on either failure."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ApplyError(f"Invalid JSON: {e.msg} (line {e.lineno}, col {e.colno})") from e
    if not isinstance(data, dict):
        raise ApplyError("Top-level JSON must be an object.")
    try:
        jsonschema.validate(data, RESPONSE_SCHEMA)
    except jsonschema.ValidationError as e:
        path = "/".join(str(p) for p in e.absolute_path) or "(root)"
        raise ApplyError(f"Schema violation at {path}: {e.message}") from e
    return data


# ---- diff ------------------------------------------------------------------


@dataclass
class DiffEntry:
    """One human-readable change. `kind` drives the UI grouping."""

    kind: str  # 'revision_add', 'issue_open', 'issue_close', 'prescription_update'
    summary: str  # one-line description for the preview
    details: list[str] = field(default_factory=list)  # before/after rows
    error: str | None = None  # set if this entry can't be applied as-is


@dataclass
class Diff:
    entries: list[DiffEntry] = field(default_factory=list)
    narrative: str | None = None

    @property
    def has_errors(self) -> bool:
        return any(e.error for e in self.entries)

    @property
    def is_empty(self) -> bool:
        return not self.entries


def build_diff(
    conn: sqlite3.Connection,
    response: dict[str, Any],
    mesocycle_id: int,
) -> Diff:
    """Compute what the response would change, without writing anything."""
    diff = Diff(narrative=response.get("narrative"))

    for r in response.get("revisions", []) or []:
        diff.entries.append(DiffEntry(
            kind="revision_add",
            summary=f"Add revision {r['date']}: {r['change']}",
            details=[f"reason: {r['reason']}"],
        ))

    for o in response.get("issue_opens", []) or []:
        details = [f"status: {o['status']}"]
        if o.get("severity"):
            details.append(f"severity: {o['severity']}")
        if o.get("action"):
            details.append(f"action: {o['action']}")
        diff.entries.append(DiffEntry(
            kind="issue_open",
            summary=f"Open issue: {o['item']}",
            details=details,
        ))

    for c in response.get("issue_closes", []) or []:
        existing = conn.execute(
            "SELECT id, item, closed_at FROM issues WHERE id = ?", (c["id"],)
        ).fetchone()
        if existing is None:
            diff.entries.append(DiffEntry(
                kind="issue_close",
                summary=f"Close issue #{c['id']} (NOT FOUND)",
                error=f"Issue id {c['id']} does not exist.",
            ))
        elif existing["closed_at"] is not None:
            diff.entries.append(DiffEntry(
                kind="issue_close",
                summary=f"Close issue #{c['id']}: already closed",
                error=f"Issue #{c['id']} is already closed ({existing['closed_at']}).",
            ))
        else:
            details = [f"item: {existing['item']}"]
            if c.get("reason"):
                details.append(f"reason: {c['reason']}")
            diff.entries.append(DiffEntry(
                kind="issue_close",
                summary=f"Close issue #{c['id']}: {existing['item']}",
                details=details,
            ))

    for u in response.get("prescription_updates", []) or []:
        diff.entries.append(_diff_prescription(conn, mesocycle_id, u))

    return diff


def _diff_prescription(
    conn: sqlite3.Connection,
    mesocycle_id: int,
    u: dict[str, Any],
) -> DiffEntry:
    day = u["session_day"]
    name = u["exercise_name"]
    current = conn.execute(
        """
        SELECT p.id, p.sets_planned, p.rep_low, p.rep_high, p.weight_lb,
               p.rir_target, p.notes, e.id AS exercise_id, e.name AS exercise_name,
               e.notation
          FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.mesocycle_id = ? AND sess.day_number = ? AND e.name = ?
        """,
        (mesocycle_id, day, name),
    ).fetchone()
    if current is None:
        return DiffEntry(
            kind="prescription_update",
            summary=f"Update Session {day} → {name} (NOT FOUND)",
            error=f"No prescribed row for Session {day} / {name}.",
        )

    changes: list[str] = []
    new_name = u.get("new_exercise_name")
    if new_name and new_name != current["exercise_name"]:
        changes.append(f"exercise: {current['exercise_name']} → {new_name}")
        # Verify target exercise exists or will be created.
        target = conn.execute(
            "SELECT id FROM exercises WHERE name = ?", (new_name,)
        ).fetchone()
        if target is None:
            changes.append(f"  (will create new exercise '{new_name}')")

    for fld in ("sets_planned", "rep_low", "rep_high", "weight_lb", "rir_target", "notes"):
        if fld in u and u[fld] is not None and u[fld] != current[fld]:
            changes.append(f"{fld}: {current[fld]} → {u[fld]}")

    if not changes:
        return DiffEntry(
            kind="prescription_update",
            summary=f"Session {day} {name}: no changes",
            error="Update would be a no-op (every field matches current state).",
        )

    return DiffEntry(
        kind="prescription_update",
        summary=f"Update Session {day} {name}",
        details=changes,
    )


# ---- apply -----------------------------------------------------------------


def apply(
    conn: sqlite3.Connection,
    response: dict[str, Any],
    mesocycle_id: int,
    *,
    request_md: str,
    response_raw: str,
) -> int:
    """Write all mutations + the audit row inside a single transaction.

    Returns the new ai_interactions.id for rollback referencing.
    Raises ApplyError if any individual mutation fails.
    """
    # Re-build diff so any errors surface before we touch state.
    diff = build_diff(conn, response, mesocycle_id)
    if diff.has_errors:
        msgs = [e.error for e in diff.entries if e.error]
        raise ApplyError("Diff has errors; nothing applied:\n  - " + "\n  - ".join(msgs))

    # The "applied diff" is the human-readable record we store for the audit
    # log; the "snapshot" is the structured undo data needed for rollback.
    snapshot = _take_snapshot(conn, response, mesocycle_id)

    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()

    # Apply mutations
    for r in response.get("revisions", []) or []:
        conn.execute(
            "INSERT INTO revisions (mesocycle_id, date, change, reason) VALUES (?, ?, ?, ?)",
            (mesocycle_id, r["date"], r["change"], r["reason"]),
        )
    for o in response.get("issue_opens", []) or []:
        conn.execute(
            "INSERT INTO issues (opened_at, item, status, action, severity) VALUES (?, ?, ?, ?, ?)",
            (today, o["item"], o["status"], o.get("action"), o.get("severity")),
        )
    for c in response.get("issue_closes", []) or []:
        conn.execute(
            "UPDATE issues SET closed_at = ? WHERE id = ?",
            (today, c["id"]),
        )
    for u in response.get("prescription_updates", []) or []:
        _apply_prescription(conn, mesocycle_id, u)

    # Audit row
    cur = conn.execute(
        """
        INSERT INTO ai_interactions
            (created_at, request_md, response_raw, parsed_json, applied_diff, status)
        VALUES (?, ?, ?, ?, ?, 'applied')
        """,
        (
            now,
            request_md,
            response_raw,
            json.dumps(response),
            json.dumps(snapshot),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _apply_prescription(
    conn: sqlite3.Connection,
    mesocycle_id: int,
    u: dict[str, Any],
) -> None:
    day = u["session_day"]
    name = u["exercise_name"]
    row = conn.execute(
        """
        SELECT p.id FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.mesocycle_id = ? AND sess.day_number = ? AND e.name = ?
        """,
        (mesocycle_id, day, name),
    ).fetchone()
    if row is None:
        raise ApplyError(f"Prescription not found for Session {day} / {name}.")

    new_name = u.get("new_exercise_name")
    if new_name and new_name != name:
        target = conn.execute(
            "SELECT id FROM exercises WHERE name = ?", (new_name,)
        ).fetchone()
        if target is None:
            cur = conn.execute(
                "INSERT INTO exercises (name, notation) VALUES (?, 'total')",
                (new_name,),
            )
            target_id = cur.lastrowid
        else:
            target_id = target["id"]
        conn.execute(
            "UPDATE prescribed SET exercise_id = ? WHERE id = ?",
            (target_id, row["id"]),
        )

    fields = ("sets_planned", "rep_low", "rep_high", "weight_lb", "rir_target", "notes")
    sets = [(f, u[f]) for f in fields if f in u and u[f] is not None]
    if sets:
        cols = ", ".join(f"{f} = ?" for f, _ in sets)
        args = [v for _, v in sets] + [row["id"]]
        conn.execute(f"UPDATE prescribed SET {cols} WHERE id = ?", args)


def _take_snapshot(
    conn: sqlite3.Connection,
    response: dict[str, Any],
    mesocycle_id: int,
) -> dict[str, Any]:
    """Capture before-images for everything the response will change.

    Stored as JSON in ai_interactions.applied_diff so /claude/log can replay
    the inverse on rollback.
    """
    snap: dict[str, Any] = {
        "revisions_added": [],
        "issues_opened": [],
        "issue_closes": [],
        "prescription_updates": [],
    }

    # Revisions/issue_opens roll back via id, but we don't know ids until
    # after insert — apply() backfills these. Here just record the payload.
    for r in response.get("revisions", []) or []:
        snap["revisions_added"].append({
            "date": r["date"], "change": r["change"], "reason": r["reason"],
        })
    for o in response.get("issue_opens", []) or []:
        snap["issues_opened"].append({
            "item": o["item"], "status": o["status"],
            "action": o.get("action"), "severity": o.get("severity"),
        })

    for c in response.get("issue_closes", []) or []:
        before = conn.execute(
            "SELECT id, closed_at FROM issues WHERE id = ?", (c["id"],)
        ).fetchone()
        if before:
            snap["issue_closes"].append({
                "id": before["id"], "previous_closed_at": before["closed_at"],
            })

    for u in response.get("prescription_updates", []) or []:
        before = conn.execute(
            """
            SELECT p.id, p.sets_planned, p.rep_low, p.rep_high, p.weight_lb,
                   p.rir_target, p.notes, p.exercise_id
              FROM prescribed p
              JOIN sessions sess ON sess.id = p.session_id
              JOIN exercises e ON e.id = p.exercise_id
             WHERE sess.mesocycle_id = ? AND sess.day_number = ? AND e.name = ?
            """,
            (mesocycle_id, u["session_day"], u["exercise_name"]),
        ).fetchone()
        if before:
            snap["prescription_updates"].append({
                "prescribed_id": before["id"],
                "before": {
                    "sets_planned": before["sets_planned"],
                    "rep_low": before["rep_low"],
                    "rep_high": before["rep_high"],
                    "weight_lb": before["weight_lb"],
                    "rir_target": before["rir_target"],
                    "notes": before["notes"],
                    "exercise_id": before["exercise_id"],
                },
                "request": u,
            })

    return snap
