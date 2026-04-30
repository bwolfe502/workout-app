"""Live SQL rollups for /stats.

Nothing here is persisted — every chart re-queries from the source-of-truth
tables on each request. Volume is "hard sets per muscle per week," matching
the program's own targets in section 6 of trainingprogram.md.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta


def _set_anchor_date(row: sqlite3.Row) -> str | None:
    """Pick the date a set 'happened on'. Logged_at first, else session date."""
    if row["logged_at"]:
        return row["logged_at"][:10]
    if row["completed_at"]:
        return row["completed_at"][:10]
    return row["planned_date"]


def _week_index(d: str | None, start: date) -> int | None:
    if not d:
        return None
    try:
        anchor = date.fromisoformat(d[:10])
    except ValueError:
        return None
    delta = (anchor - start).days
    if delta < 0:
        return None
    return delta // 7 + 1  # 1-indexed


def _week_label(start: date, idx: int) -> str:
    week_start = start + timedelta(days=(idx - 1) * 7)
    return f"W{idx} ({week_start.isoformat()})"


def volume_by_muscle_week(
    conn: sqlite3.Connection,
    mesocycle_id: int,
) -> dict[str, dict[str, int]]:
    """Return {muscle: {week_label: hard_set_count}} for completed sets only.

    A "hard set" is any logged set with status='completed'. Each set
    contributes 1 to every muscle named in `exercises.primary_muscles`.
    Sets that landed before the mesocycle start are skipped.
    """
    meso = conn.execute(
        "SELECT start_date FROM mesocycles WHERE id = ?", (mesocycle_id,)
    ).fetchone()
    if meso is None:
        return {}
    start = date.fromisoformat(meso["start_date"])

    rows = conn.execute(
        """
        SELECT s.id, s.logged_at,
               sess.completed_at, sess.planned_date,
               e.primary_muscles
          FROM sets s
          JOIN prescribed p ON p.id = s.prescribed_id
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE s.status = 'completed'
           AND sess.mesocycle_id = ?
        """,
        (mesocycle_id,),
    ).fetchall()

    counts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        anchor = _set_anchor_date(r)
        wk = _week_index(anchor, start)
        if wk is None:
            continue
        muscles = (r["primary_muscles"] or "").split(",")
        for m in (mu.strip() for mu in muscles if mu.strip()):
            counts[m][wk] += 1

    if not counts:
        return {}
    max_week = max(max(weeks.keys()) for weeks in counts.values())
    out: dict[str, dict[str, int]] = {}
    for muscle in sorted(counts.keys()):
        out[muscle] = {
            _week_label(start, w): counts[muscle].get(w, 0)
            for w in range(1, max_week + 1)
        }
    return out


def strength_trend(
    conn: sqlite3.Connection,
    exercise_id: int,
    mesocycle_id: int | None = None,
) -> list[dict]:
    """Per session: top weight, reps at that weight, est. 1RM (Epley).

    Returns rows ordered by session date. For sessions where the same
    weight has multiple sets, we pick the highest rep count (closer to
    failure → more honest 1RM est).
    """
    sql = [
        """
        SELECT sess.day_number,
               sess.planned_date,
               sess.completed_at,
               s.weight_actual,
               s.reps_actual,
               s.set_number
          FROM sets s
          JOIN prescribed p ON p.id = s.prescribed_id
          JOIN sessions sess ON sess.id = p.session_id
         WHERE s.status = 'completed'
           AND p.exercise_id = ?
           AND s.weight_actual IS NOT NULL
           AND s.reps_actual IS NOT NULL
        """
    ]
    args: list = [exercise_id]
    if mesocycle_id is not None:
        sql.append("AND sess.mesocycle_id = ?")
        args.append(mesocycle_id)
    sql.append("ORDER BY sess.planned_date, sess.day_number, s.set_number")
    rows = conn.execute("\n".join(sql), args).fetchall()

    by_session: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key = (r["planned_date"], r["day_number"])
        by_session[key].append(r)

    out: list[dict] = []
    for (planned_date, day_number), session_rows in sorted(by_session.items()):
        # Top weight, then most reps at that weight
        top_weight = max(r["weight_actual"] for r in session_rows)
        top_at = max(
            (r["reps_actual"] for r in session_rows if r["weight_actual"] == top_weight),
            default=0,
        )
        # Epley 1RM = weight × (1 + reps/30)
        e1rm = round(top_weight * (1 + top_at / 30), 1)
        out.append({
            "date": planned_date,
            "day_number": day_number,
            "top_weight": top_weight,
            "top_reps": top_at,
            "e1rm": e1rm,
        })
    return out


def bodyweight_trend(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT date, weight_lb, waist_in FROM weigh_ins ORDER BY date"
    ).fetchall()
    return [{"date": r["date"], "weight_lb": r["weight_lb"], "waist_in": r["waist_in"]}
            for r in rows]


def pain_by_week(
    conn: sqlite3.Connection,
    mesocycle_id: int,
) -> dict[str, int]:
    """Issues opened per week, keyed by week label aligned to mesocycle start."""
    meso = conn.execute(
        "SELECT start_date FROM mesocycles WHERE id = ?", (mesocycle_id,)
    ).fetchone()
    if meso is None:
        return {}
    start = date.fromisoformat(meso["start_date"])
    rows = conn.execute("SELECT opened_at FROM issues").fetchall()

    by_week: dict[int, int] = defaultdict(int)
    for r in rows:
        wk = _week_index(r["opened_at"], start)
        if wk is not None:
            by_week[wk] += 1

    if not by_week:
        return {}
    max_week = max(by_week.keys())
    return {
        _week_label(start, w): by_week.get(w, 0)
        for w in range(1, max_week + 1)
    }


def trained_exercises(
    conn: sqlite3.Connection,
    mesocycle_id: int,
) -> list[sqlite3.Row]:
    """Exercises with at least one completed set, for the strength dropdown."""
    return list(conn.execute(
        """
        SELECT DISTINCT e.id, e.name, e.notation
          FROM exercises e
          JOIN prescribed p ON p.exercise_id = e.id
          JOIN sets s ON s.prescribed_id = p.id
          JOIN sessions sess ON sess.id = p.session_id
         WHERE s.status = 'completed'
           AND s.weight_actual IS NOT NULL
           AND sess.mesocycle_id = ?
         ORDER BY e.name
        """,
        (mesocycle_id,),
    ))


# Volume targets from trainingprogram.md section 6 — used as overlay lines.
VOLUME_TARGETS_LOW: dict[str, int] = {
    "chest": 10, "back": 12, "quads": 8, "hamstrings": 6,
    "side_delt": 8, "rear_delt": 3, "biceps": 6, "triceps": 6, "abs": 4,
}
VOLUME_TARGETS_HIGH: dict[str, int] = {
    "chest": 12, "back": 14, "quads": 10, "hamstrings": 9,
    "side_delt": 10, "rear_delt": 6, "biceps": 8, "triceps": 8, "abs": 6,
}
