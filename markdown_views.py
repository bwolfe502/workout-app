"""Generate markdown views of current state, mirroring the source md files.

These views are the "current program state" half of the outbound Claude
bundle. They are also re-usable for `/export` (Phase 5 stretch).

The mesocycle view's structure mirrors `mesocycle1.md`; the workout-log view
mirrors `workoutlog.md`. Claude has trained on these formats during the
manual loop, so keeping them stable means the paste-in/paste-out loop is
a smooth handoff.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Iterable

import volume


# --- formatters -------------------------------------------------------------


def _fmt_weight(weight_lb: float | None, is_bw: bool, notation: str) -> str:
    if is_bw or notation == "bw":
        return "BW"
    if weight_lb is None:
        return "—"
    suffix = " /hand" if notation == "per_hand" else ""
    return f"{weight_lb:g}{suffix}"


def _fmt_reps(rep_low: int | None, rep_high: int | None) -> str:
    if rep_low is None and rep_high is None:
        return "—"
    if rep_low == rep_high or rep_high is None:
        return f"{rep_low}"
    return f"{rep_low}-{rep_high}"


def _fmt_set(set_row: sqlite3.Row, notation: str) -> str:
    if set_row["status"] == "skipped":
        return "— (skipped)"
    if set_row["status"] == "deferred":
        return "— (deferred)"
    reps = set_row["reps_actual"] or "—"
    w = set_row["weight_actual"]
    if w is None:
        return f"{reps}×BW"
    suffix = " lb/hand" if notation == "per_hand" else " lb"
    return f"{reps}×{w:g}{suffix}"


# --- mesocycle view ---------------------------------------------------------


def mesocycle_view(conn: sqlite3.Connection, mesocycle_id: int) -> str:
    """Render the active mesocycle in mesocycle1.md style."""
    meso = conn.execute(
        "SELECT * FROM mesocycles WHERE id = ?", (mesocycle_id,)
    ).fetchone()
    if meso is None:
        return ""
    out: list[str] = [f"# {meso['name']}"]
    out.append("")
    out.append("DB weights per hand · BB weights total · BW = bodyweight")
    out.append("")

    sessions = conn.execute(
        """
        SELECT id, day_number, planned_date, workout_letter, status, completed_at
          FROM sessions
         WHERE mesocycle_id = ? AND day_number IS NOT NULL
         ORDER BY day_number
        """,
        (mesocycle_id,),
    ).fetchall()
    for sess in sessions:
        tag = sess["status"]
        if tag == "completed":
            tag_str = "[COMPLETED]"
        elif tag == "partial":
            tag_str = "[COMPLETED — partial]"
        elif tag == "in_progress":
            tag_str = "[IN PROGRESS]"
        elif sess["day_number"] >= 10:
            tag_str = "(Deload)"
        else:
            tag_str = ""
        header = (
            f"## Session {sess['day_number']} — {sess['planned_date']} — "
            f"Workout {sess['workout_letter']}"
        )
        if tag_str:
            header += f" {tag_str}"
        out.append("")
        out.append(header)
        out.append("")
        prx = conn.execute(
            """
            SELECT p.sets_planned, p.rep_low, p.rep_high,
                   p.weight_lb, p.rir_target,
                   e.name AS exercise_name, e.notation, e.is_bodyweight
              FROM prescribed p
              JOIN exercises e ON e.id = p.exercise_id
             WHERE p.session_id = ?
             ORDER BY p.position
            """,
            (sess["id"],),
        ).fetchall()
        if not prx:
            out.append("_No prescription on file._")
            continue
        out.append("| Exercise | Sets | Reps | Weight | RIR |")
        out.append("|---|---|---|---|---|")
        for p in prx:
            weight = _fmt_weight(p["weight_lb"], bool(p["is_bodyweight"]), p["notation"])
            rir = "—" if p["rir_target"] is None else str(p["rir_target"])
            out.append(
                f"| {p['exercise_name']} | {p['sets_planned']} | "
                f"{_fmt_reps(p['rep_low'], p['rep_high'])} | {weight} | {rir} |"
            )
    return "\n".join(out)


# --- workout-log view -------------------------------------------------------


def workoutlog_view(
    conn: sqlite3.Connection,
    mesocycle_id: int,
    only_completed: bool = True,
) -> str:
    """Render completed sessions with actuals, in workoutlog.md style."""
    meso = conn.execute(
        "SELECT name, start_date FROM mesocycles WHERE id = ?", (mesocycle_id,)
    ).fetchone()
    if meso is None:
        return ""
    out: list[str] = [f"# Workout Log — {meso['name']}", ""]

    where = "AND status IN ('completed', 'partial')" if only_completed else ""
    sessions = conn.execute(
        f"""
        SELECT id, day_number, planned_date, workout_letter, status,
               narrative_md, hevy_url, completed_at
          FROM sessions
         WHERE mesocycle_id = ? {where}
         ORDER BY
           CASE WHEN day_number IS NULL THEN 1 ELSE 0 END,
           day_number,
           planned_date
        """,
        (mesocycle_id,),
    ).fetchall()
    if not sessions:
        out.append("_No sessions logged yet._")
        return "\n".join(out)

    for sess in sessions:
        if sess["day_number"] is not None:
            header = (
                f"## Session {sess['day_number']} — {sess['planned_date']} — "
                f"Workout {sess['workout_letter']}"
            )
        else:
            header = f"## {sess['planned_date']} — Extra"
        out.append("")
        out.append(header)
        out.append("")
        if sess["status"] == "partial":
            out.append("**Status:** Partial")
            out.append("")

        rows = conn.execute(
            """
            SELECT p.id AS prescribed_id,
                   p.sets_planned, p.rep_low, p.rep_high,
                   p.weight_lb, p.rir_target,
                   e.name AS exercise_name, e.notation, e.is_bodyweight
              FROM prescribed p
              JOIN exercises e ON e.id = p.exercise_id
             WHERE p.session_id = ?
             ORDER BY p.position
            """,
            (sess["id"],),
        ).fetchall()
        sets_by_pres = defaultdict(list)
        for s in conn.execute(
            """
            SELECT s.* FROM sets s
              JOIN prescribed p ON p.id = s.prescribed_id
             WHERE p.session_id = ?
             ORDER BY p.position, s.set_number
            """,
            (sess["id"],),
        ):
            sets_by_pres[s["prescribed_id"]].append(s)

        if not rows:
            out.append("_No prescription rows._")
            continue
        out.append("| Exercise | Prescribed | Actual |")
        out.append("|---|---|---|")
        for r in rows:
            pres_str = (
                f"{r['sets_planned']}×{_fmt_reps(r['rep_low'], r['rep_high'])} "
                f"@ {_fmt_weight(r['weight_lb'], bool(r['is_bodyweight']), r['notation'])}"
            )
            if r["rir_target"] is not None:
                pres_str += f", RIR {r['rir_target']}"
            actuals = sets_by_pres.get(r["prescribed_id"], [])
            if not actuals:
                actual_str = "—"
            elif len(actuals) == 1 and actuals[0]["status"] in ("skipped", "deferred"):
                actual_str = _fmt_set(actuals[0], r["notation"])
            else:
                # Collapse identical sets: "3×8 @ 30 lb" if 3 sets all match.
                completed = [s for s in actuals if s["status"] == "completed"]
                if (
                    len(completed) == len(actuals)
                    and len({(s["reps_actual"], s["weight_actual"]) for s in completed}) == 1
                ):
                    one = completed[0]
                    suffix = " lb/hand" if r["notation"] == "per_hand" else " lb"
                    if one["weight_actual"] is None:
                        actual_str = f"{len(completed)}×{one['reps_actual']} BW"
                    else:
                        actual_str = (
                            f"{len(completed)}×{one['reps_actual']} @ "
                            f"{one['weight_actual']:g}{suffix}"
                        )
                else:
                    actual_str = ", ".join(
                        f"1×{_fmt_set(s, r['notation'])}" for s in completed
                    ) or _fmt_set(actuals[0], r["notation"])
            out.append(f"| {r['exercise_name']} | {pres_str} | {actual_str} |")

        if sess["narrative_md"]:
            out.append("")
            out.append(_clip_narrative(sess["narrative_md"]))
        if sess["hevy_url"]:
            out.append("")
            out.append(f"Hevy: {sess['hevy_url']}")

    return "\n".join(out)


def _clip_narrative(text: str, max_len: int = 600) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


# --- issues view ------------------------------------------------------------


def issues_view(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT id, opened_at, item, status, action, severity
          FROM issues
         WHERE closed_at IS NULL
         ORDER BY opened_at DESC, id DESC
        """
    ).fetchall()
    out: list[str] = ["# Active Issues", ""]
    if not rows:
        out.append("_No open issues._")
        return "\n".join(out)
    out.append("| id | opened | item | status | action |")
    out.append("|---|---|---|---|---|")
    for r in rows:
        item = (r["item"] or "").replace("|", "\\|")
        status = (r["status"] or "").replace("|", "\\|")
        action = (r["action"] or "").replace("|", "\\|")
        out.append(f"| {r['id']} | {r['opened_at']} | {item} | {status} | {action} |")
    return "\n".join(out)


# --- volume view ------------------------------------------------------------


def volume_view(conn: sqlite3.Connection, mesocycle_id: int) -> str:
    rollup = volume.volume_by_muscle_week(conn, mesocycle_id)
    out: list[str] = ["# Volume per Muscle / Week", ""]
    if not rollup:
        out.append("_No completed sets yet._")
        return "\n".join(out)
    weeks = list(next(iter(rollup.values())).keys())
    headers = ["muscle", *weeks, "target"]
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "|".join(["---"] * len(headers)) + "|")
    for muscle in sorted(rollup.keys()):
        counts = rollup[muscle]
        target_low = volume.VOLUME_TARGETS_LOW.get(muscle, "—")
        target_high = volume.VOLUME_TARGETS_HIGH.get(muscle, "—")
        target = f"{target_low}-{target_high}"
        row = [muscle, *(str(counts[w]) for w in weeks), target]
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


# --- body metrics view ------------------------------------------------------


def metrics_view(conn: sqlite3.Connection, limit: int = 8) -> str:
    weighs = conn.execute(
        "SELECT date, weight_lb, waist_in FROM weigh_ins ORDER BY date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    dailies = conn.execute(
        "SELECT date, sleep_hours, energy, steps FROM daily_metrics ORDER BY date DESC LIMIT ?",
        (limit,),
    ).fetchall()

    out: list[str] = ["# Body Metrics", ""]
    if weighs:
        out.append("## Weigh-ins")
        out.append("| date | weight (lb) | waist (in) |")
        out.append("|---|---|---|")
        for w in reversed(weighs):
            waist = "—" if w["waist_in"] is None else f"{w['waist_in']:g}"
            out.append(f"| {w['date']} | {w['weight_lb']:g} | {waist} |")
        out.append("")
    if dailies:
        out.append("## Recent daily")
        out.append("| date | sleep | energy | steps |")
        out.append("|---|---|---|---|")
        for d in reversed(dailies):
            sleep = "—" if d["sleep_hours"] is None else f"{d['sleep_hours']:g} h"
            energy = "—" if d["energy"] is None else str(d["energy"])
            steps = "—" if d["steps"] is None else f"{d['steps']:,}"
            out.append(f"| {d['date']} | {sleep} | {energy} | {steps} |")
    if not weighs and not dailies:
        out.append("_No body metrics logged yet._")
    return "\n".join(out)


# --- revisions view ---------------------------------------------------------


def revisions_view(conn: sqlite3.Connection, mesocycle_id: int) -> str:
    rows = conn.execute(
        """
        SELECT date, change, reason
          FROM revisions
         WHERE mesocycle_id = ?
         ORDER BY date
        """,
        (mesocycle_id,),
    ).fetchall()
    out: list[str] = ["# Revisions Log", ""]
    if not rows:
        out.append("_No revisions yet._")
        return "\n".join(out)
    out.append("| date | change | reason |")
    out.append("|---|---|---|")
    for r in rows:
        change = (r["change"] or "").replace("|", "\\|")
        reason = (r["reason"] or "").replace("|", "\\|")
        out.append(f"| {r['date']} | {change} | {reason} |")
    return "\n".join(out)
