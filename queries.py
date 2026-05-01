"""Read-only queries for the Phase 1 logger pages.

Kept separate from `app.py` so route handlers stay thin and queries can be
unit-tested directly against a seeded in-memory DB.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def active_mesocycle(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM mesocycles WHERE status = 'active' ORDER BY start_date DESC LIMIT 1"
    ).fetchone()


def all_sessions(conn: sqlite3.Connection, mesocycle_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT id, day_number, planned_date, completed_at, workout_letter,
               status, narrative_md, hevy_url
          FROM sessions
         WHERE mesocycle_id = ?
         ORDER BY
           CASE WHEN day_number IS NULL THEN 1 ELSE 0 END,
           day_number,
           planned_date
        """,
        (mesocycle_id,),
    ))


def numbered_sessions(conn: sqlite3.Connection, mesocycle_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT id, day_number, planned_date, completed_at, workout_letter,
               status, narrative_md, hevy_url
          FROM sessions
         WHERE mesocycle_id = ? AND day_number IS NOT NULL
         ORDER BY day_number
        """,
        (mesocycle_id,),
    ))


def next_session(conn: sqlite3.Connection, mesocycle_id: int) -> sqlite3.Row | None:
    """First session that hasn't been completed yet."""
    return conn.execute(
        """
        SELECT id, day_number, planned_date, workout_letter, status
          FROM sessions
         WHERE mesocycle_id = ?
           AND day_number IS NOT NULL
           AND completed_at IS NULL
           AND status IN ('planned', 'in_progress')
         ORDER BY day_number
         LIMIT 1
        """,
        (mesocycle_id,),
    ).fetchone()


def session_by_id(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()


def prescribed_for_session(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT p.id          AS prescribed_id,
               p.position    AS position,
               p.sets_planned, p.rep_low, p.rep_high,
               p.weight_lb, p.rir_target, p.notes AS prescribed_notes,
               e.id          AS exercise_id,
               e.name        AS exercise_name,
               e.notation    AS notation,
               e.is_bodyweight AS is_bodyweight,
               e.default_tempo,
               e.primary_muscles
          FROM prescribed p
          JOIN exercises e ON e.id = p.exercise_id
         WHERE p.session_id = ?
         ORDER BY p.position
        """,
        (session_id,),
    ))


def sets_for_session(conn: sqlite3.Connection, session_id: int) -> dict[int, list[sqlite3.Row]]:
    """Return {prescribed_id: [set rows...]} for a session."""
    rows = conn.execute(
        """
        SELECT s.*, p.id AS prescribed_id
          FROM sets s
          JOIN prescribed p ON p.id = s.prescribed_id
         WHERE p.session_id = ?
         ORDER BY p.position, s.set_number
        """,
        (session_id,),
    ).fetchall()
    out: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        out.setdefault(r["prescribed_id"], []).append(r)
    return out


def previous_sets_by_exercise(
    conn: sqlite3.Connection, session_id: int
) -> dict[int, dict[str, Any]]:
    """For each exercise prescribed in `session_id`, return the most recent
    prior session that recorded completed sets for it.

    Shape: {exercise_id: {"day_number", "planned_date", "sets": [rows]}}.
    Empty dict if no priors. Used to render a "Last time: …" hint above each
    exercise's set form so the lifter doesn't have to scroll back through
    /sessions to recall what they did.
    """
    cur_exercise_ids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT exercise_id FROM prescribed WHERE session_id = ?",
            (session_id,),
        )
    ]
    out: dict[int, dict[str, Any]] = {}
    for ex_id in cur_exercise_ids:
        prior = conn.execute(
            """
            SELECT p.id AS prescribed_id,
                   sess.id AS session_id,
                   sess.day_number,
                   sess.planned_date,
                   sess.completed_at
              FROM prescribed p
              JOIN sessions sess ON sess.id = p.session_id
             WHERE p.exercise_id = ?
               AND p.session_id != ?
               AND EXISTS (
                   SELECT 1 FROM sets s
                    WHERE s.prescribed_id = p.id AND s.status = 'completed'
               )
             ORDER BY COALESCE(sess.completed_at, sess.planned_date) DESC,
                      sess.id DESC
             LIMIT 1
            """,
            (ex_id, session_id),
        ).fetchone()
        if prior is None:
            continue
        sets = list(conn.execute(
            """
            SELECT set_number, reps_actual, weight_actual, rir_actual
              FROM sets
             WHERE prescribed_id = ? AND status = 'completed'
             ORDER BY set_number
            """,
            (prior["prescribed_id"],),
        ))
        out[ex_id] = {
            "session_id": prior["session_id"],
            "day_number": prior["day_number"],
            "planned_date": prior["planned_date"],
            "sets": sets,
        }
    return out


def partial_sessions(
    conn: sqlite3.Connection, mesocycle_id: int, limit: int = 3
) -> list[sqlite3.Row]:
    """Numbered sessions in this mesocycle marked 'partial'. Surfaced on
    Today as a recovery hint after an early Mark-complete tap.
    """
    return list(conn.execute(
        """
        SELECT id, day_number, planned_date, workout_letter, status
          FROM sessions
         WHERE mesocycle_id = ?
           AND day_number IS NOT NULL
           AND status = 'partial'
         ORDER BY day_number DESC
         LIMIT ?
        """,
        (mesocycle_id, limit),
    ))


def all_exercises(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every exercise, alphabetised. Powers the in-session 'Swap exercise'
    dropdown on the live view. Includes notation/is_bodyweight so the form
    can hint at unit semantics in the option label.
    """
    return list(conn.execute(
        """
        SELECT id, name, notation, is_bodyweight, primary_muscles
          FROM exercises
         ORDER BY name COLLATE NOCASE
        """
    ))


def open_issues(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT * FROM issues WHERE closed_at IS NULL ORDER BY opened_at DESC, id DESC
        """
    ))


def closed_issues(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT * FROM issues
         WHERE closed_at IS NOT NULL
         ORDER BY closed_at DESC, id DESC
         LIMIT ?
        """,
        (limit,),
    ))


def issue_by_id(conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()


def ai_interactions(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT id, created_at, status, parsed_json, applied_diff
          FROM ai_interactions
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        (limit,),
    ))


def ai_interaction_by_id(conn: sqlite3.Connection, ai_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ai_interactions WHERE id = ?", (ai_id,)
    ).fetchone()


def revisions(conn: sqlite3.Connection, mesocycle_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT * FROM revisions WHERE mesocycle_id = ? ORDER BY date
        """,
        (mesocycle_id,),
    ))


def last_weigh_in(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM weigh_ins ORDER BY date DESC LIMIT 1"
    ).fetchone()


def recent_weigh_ins(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM weigh_ins ORDER BY date DESC LIMIT ?", (limit,),
    ))


def recent_daily_metrics(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT ?", (limit,),
    ))


# --- formatters used by templates ------------------------------------------


def format_weight(prescribed_row: Any) -> str:
    """Render the weight cell as it appears on /program: '35 lb' or 'BW'."""
    if prescribed_row["is_bodyweight"]:
        return "BW"
    w = prescribed_row["weight_lb"]
    if w is None:
        return "—"
    notation = prescribed_row["notation"]
    suffix = " /hand" if notation == "per_hand" else ""
    # Drop trailing .0 for cleaner display
    s = f"{w:g}"
    return f"{s} lb{suffix}"


def format_reps(prescribed_row: Any) -> str:
    lo, hi = prescribed_row["rep_low"], prescribed_row["rep_high"]
    if lo is None and hi is None:
        return "—"
    if lo == hi or hi is None:
        return f"{lo}"
    return f"{lo}-{hi}"


def format_session_label(session_row: Any) -> str:
    if session_row["day_number"] is None:
        return session_row["planned_date"] or "Extra day"
    letter = session_row["workout_letter"] or "?"
    return f"Session {session_row['day_number']} — Workout {letter}"
