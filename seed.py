"""One-shot seed importer for the three source md files.

Reads `trainingprogram.md`, `mesocycle1.md`, `workoutlog.md` from a source
directory (default: `C:/Users/bwolf/Downloads/files/`) and inserts:

    - 1 row in `mesocycles`             (Mesocycle 1)
    - 1 row in `workout_templates`      (Workout C swap day)
    - 12 rows in `sessions`             (Session 1..12)
    - 0..N rows in `sessions` w/ status='extra' for non-numbered days
      (Thu Apr 23 carryover, Thu Apr 30 accessory pickup)
    - rows in `exercises`               (de-duped via alias map)
    - rows in `prescribed`              (one per exercise per session)
    - rows in `sets`                    (expanded from "3×8 @ 30 lb" actuals)
    - rows in `revisions`               (10 from the trainingprogram log)
    - rows in `issues`                  (from workoutlog Active Issues table)

Idempotency: by default, aborts if a mesocycle named "Mesocycle 1" already
exists. Pass `--reset` to wipe and re-seed.

Usage:

    python -m seed [--source-dir DIR] [--db PATH] [--reset]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import db


# --- exercise canonicalization ---------------------------------------------

# Maps observed names → canonical name. Anything not in this map is inserted
# as-is. Phase 2 can fold more aliases in once we see real-world drift.
EXERCISE_ALIASES: dict[str, str] = {
    "Incline DB Bench Press": "Incline DB Bench",
    "BB Back Squat (or goblet sub)": "BB Back Squat",
    "BB Back Squat (to parallel, NOT below)": "BB Back Squat",
    "Lat Pulldown (neutral)": "Lat Pulldown",
    "Lat Pulldown OR Chin-Up": "Lat Pulldown",
    "Kinesis Lat Pulldown": "Lat Pulldown",
    "Triceps Pushdown (rope)": "Triceps Pushdown",
    "Kinesis Triceps Pushdown": "Triceps Pushdown",
    "Triceps Rope Pushdown": "Triceps Pushdown",
    "DB RDL": "DB Romanian Deadlift",
    "DB RDL (sub for BB RDL)": "DB Romanian Deadlift",
    "Seated DB Shoulder Press (neutral)": "Seated DB Shoulder Press",
    "DB Shoulder Press (neutral)": "Seated DB Shoulder Press",
    "Romanian Deadlift (barbell)": "Barbell RDL",
    "Flat DB Bench Press": "Flat DB Bench",
    "Flat DB Bench (elbows tucked)": "Flat DB Bench",
    "Heel-Elev Goblet Squat": "Heel-Elevated Goblet Squat",
    "Goblet Squat (heel-elev)": "Heel-Elevated Goblet Squat",
    "Cable Face Pull": "Cable Face Pull",
    "Kinesis Face Pull": "Cable Face Pull",
    "Seated Cable Row (neutral grip)": "Seated Cable Row (neutral)",
    "DB Curl (sub for EZ-Bar)": "DB Curl",
    "Leg Extension (selectorized)": "Leg Extension",
    "Overhead DB Triceps Ext": "Overhead DB Triceps Ext",
    "Hanging or Lying Leg Raise": "Hanging Leg Raise",
}

# Per-exercise metadata. Anything not present gets sensible defaults.
EXERCISE_METADATA: dict[str, dict[str, Any]] = {
    "Incline DB Bench":           dict(category="compound_push", primary_muscles="chest,front_delt,triceps", notation="per_hand", default_tempo="3-0-1-0"),
    "Flat DB Bench":              dict(category="compound_push", primary_muscles="chest,front_delt,triceps", notation="per_hand", default_tempo="3-0-1-0"),
    "Seated DB Shoulder Press":   dict(category="compound_push", primary_muscles="front_delt,side_delt,triceps", notation="per_hand", default_tempo="3-0-1-0"),
    "Push-Up (feet on floor)":    dict(category="compound_push", primary_muscles="chest,front_delt,triceps", notation="bw", is_bodyweight=True, default_tempo="3-0-1-0"),
    "Chest-Supported DB Row":     dict(category="compound_pull", primary_muscles="back,rear_delt,biceps", notation="per_hand", default_tempo="2-1-1-0"),
    "Single-Arm DB Row":          dict(category="compound_pull", primary_muscles="back,rear_delt,biceps", notation="per_hand", default_tempo="2-0-1-1"),
    "Lat Pulldown":               dict(category="compound_pull", primary_muscles="back,biceps", notation="total", default_tempo="2-0-1-1"),
    "Seated Cable Row (neutral)": dict(category="compound_pull", primary_muscles="back,rear_delt,biceps", notation="total", default_tempo="2-1-1-1"),
    "BB Back Squat":              dict(category="squat", primary_muscles="quads,glutes", notation="total", default_tempo="3-0-1-0"),
    "Heel-Elevated Goblet Squat": dict(category="squat", primary_muscles="quads,glutes", notation="total", default_tempo="3-0-1-0"),
    "Leg Extension":              dict(category="isolation", primary_muscles="quads", notation="total"),
    "Barbell RDL":                dict(category="hinge", primary_muscles="hamstrings,glutes,back", notation="total", default_tempo="3-0-1-0"),
    "DB Romanian Deadlift":       dict(category="hinge", primary_muscles="hamstrings,glutes,back", notation="per_hand", default_tempo="3-0-1-0"),
    "DB Lateral Raise":           dict(category="isolation", primary_muscles="side_delt", notation="per_hand", default_tempo="2-1-1-0"),
    "Cable Face Pull":            dict(category="isolation", primary_muscles="rear_delt,upper_back", notation="total", default_tempo="2-1-1-1"),
    "DB Curl":                    dict(category="isolation", primary_muscles="biceps", notation="per_hand", default_tempo="2-0-1-0"),
    "DB Hammer Curl":             dict(category="isolation", primary_muscles="biceps,brachialis", notation="per_hand", default_tempo="2-0-1-0"),
    "EZ-Bar Curl":                dict(category="isolation", primary_muscles="biceps", notation="total"),
    "Triceps Pushdown":           dict(category="isolation", primary_muscles="triceps", notation="total", default_tempo="2-1-1-0"),
    "Overhead DB Triceps Ext":    dict(category="isolation", primary_muscles="triceps", notation="per_hand"),
    "Standing Calf Raise":        dict(category="isolation", primary_muscles="calves", notation="total"),
    "Hanging Leg Raise":          dict(category="core", primary_muscles="abs", notation="bw", is_bodyweight=True),
    "Lying Leg Raise":            dict(category="core", primary_muscles="abs", notation="bw", is_bodyweight=True),
}


def canonical_exercise_name(raw: str) -> str:
    return EXERCISE_ALIASES.get(raw.strip(), raw.strip())


# --- parsed-data containers -------------------------------------------------


@dataclass
class PrescribedRow:
    exercise_name: str
    sets_planned: int
    rep_low: int | None
    rep_high: int | None
    weight_lb: float | None
    rir_target: int | None
    notes: str | None = None


@dataclass
class ActualSet:
    """One logged set, expanded from cells like '3×8 @ 30 lb'."""

    set_number: int
    reps_actual: int | None
    weight_actual: float | None
    rir_actual: int | None
    status: str  # completed / skipped / deferred
    notes: str | None = None


@dataclass
class SessionPrescription:
    day_number: int
    workout_letter: str  # 'A' | 'B'
    planned_date: str  # ISO date
    status_tag: str | None  # raw text from header brackets, lowercased
    prescribed: list[PrescribedRow] = field(default_factory=list)


@dataclass
class SessionActuals:
    day_number: int | None  # None for extra days
    label: str  # session header text
    planned_date: str
    status: str  # completed | partial | extra
    narrative_md: str
    hevy_url: str | None = None
    # exercise_name → list of (prescribed_text, actual_cells, notes_cell)
    rows: dict[str, "ActualRow"] = field(default_factory=dict)


@dataclass
class ActualRow:
    exercise_name: str
    prescribed_text: str
    actual_text: str
    notes_text: str
    parsed_sets: list[ActualSet]


@dataclass
class RevisionRow:
    date: str  # ISO
    change: str
    reason: str


@dataclass
class IssueRow:
    item: str
    status: str
    action: str


@dataclass
class WorkoutCTemplateRow:
    exercise_name: str
    sets: int
    reps: int
    weight_lb: float | None
    rir: int | None


@dataclass
class SeedData:
    mesocycle_name: str
    mesocycle_start: str  # ISO
    workout_c: list[WorkoutCTemplateRow]
    sessions: list[SessionPrescription]
    extras: list[SessionActuals]  # status='extra' rows
    actuals_by_day: dict[int, SessionActuals]
    revisions: list[RevisionRow]
    issues: list[IssueRow]


# --- markdown table parsing -------------------------------------------------

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[-:|\s]+\|\s*$")


def _split_row(line: str) -> list[str]:
    # Strip leading/trailing pipe, split on pipes, trim cells.
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _parse_md_tables(lines: list[str]) -> list[tuple[int, list[str], list[list[str]]]]:
    """Return (start_line, header, rows) for every GFM table in `lines`."""
    out: list[tuple[int, list[str], list[list[str]]]] = []
    i = 0
    while i < len(lines):
        if (
            _TABLE_ROW_RE.match(lines[i])
            and i + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            header = _split_row(lines[i])
            j = i + 2
            rows = []
            while j < len(lines) and _TABLE_ROW_RE.match(lines[j]):
                rows.append(_split_row(lines[j]))
                j += 1
            out.append((i, header, rows))
            i = j
        else:
            i += 1
    return out


# --- date helpers -----------------------------------------------------------

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_DOW_DATE_RE = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Z][a-z]{2})\s+(\d{1,2})\b")


def _parse_dow_date(text: str, year: int) -> str | None:
    """Extract 'Wed Apr 22' from text → ISO 'YYYY-MM-DD'."""
    m = _DOW_DATE_RE.search(text)
    if not m:
        return None
    _, mon, day = m.group(1), m.group(2), int(m.group(3))
    return date(year, _MONTHS[mon], day).isoformat()


# --- trainingprogram.md parsing --------------------------------------------

_REVISION_DATE_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})$")


def parse_revisions(md: str) -> list[RevisionRow]:
    """Parse the 'Revisions Log' table from trainingprogram.md."""
    lines = md.splitlines()
    # Find the revisions section header
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## revisions log"):
            start = i
            break
    if start is None:
        return []
    # First table after that section is the revisions table
    tables = _parse_md_tables(lines[start:])
    if not tables:
        return []
    _, header, rows = tables[0]
    if [h.lower() for h in header] != ["date", "change", "reason"]:
        return []
    out: list[RevisionRow] = []
    for r in rows:
        if len(r) != 3:
            continue
        m = _REVISION_DATE_RE.match(r[0])
        if not m:
            continue
        mon, day, year = m.group(1), int(m.group(2)), int(m.group(3))
        iso = date(year, _MONTHS[mon], day).isoformat()
        out.append(RevisionRow(date=iso, change=r[1], reason=r[2]))
    return out


# --- mesocycle1.md parsing --------------------------------------------------

_SESSION_HEADER_RE = re.compile(
    r"^##\s*Session\s+(\d+)\s+—\s+(\w+\s+\w+\s+\d+)\s+—\s+Workout\s+([ABC])\s*(.*)$"
)
_RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")


def _parse_int(s: str) -> int | None:
    s = s.strip()
    if not s or s == "—":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_weight_cell(s: str) -> tuple[float | None, str | None]:
    """Return (weight_lb, note). 'BW' → None weight + note. '40' → 40.0."""
    s = s.strip()
    if not s or s.upper() == "BW":
        return None, None
    if "-" in s:  # range like "40-50": take low end
        parts = s.split("-")
        try:
            return float(parts[0]), f"range {s}"
        except ValueError:
            return None, s
    try:
        return float(s), None
    except ValueError:
        return None, s


def _parse_reps_cell(s: str) -> tuple[int | None, int | None]:
    """Return (rep_low, rep_high). '8' → (8, 8); '8-12' → (8, 12)."""
    s = s.strip()
    m = _RANGE_RE.match(s)
    if m:
        return int(m.group(1)), int(m.group(2))
    n = _parse_int(s)
    return n, n


def parse_mesocycle_sessions(md: str, year: int) -> tuple[list[SessionPrescription], list[WorkoutCTemplateRow]]:
    """Parse Sessions 1..N + Workout C template from mesocycle1.md."""
    lines = md.splitlines()
    sessions: list[SessionPrescription] = []
    workout_c: list[WorkoutCTemplateRow] = []

    # Walk line by line, capturing each "## Session N" or "## Workout C" block.
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _SESSION_HEADER_RE.match(line)
        if m:
            day = int(m.group(1))
            dow_date = m.group(2)
            letter = m.group(3)
            suffix = (m.group(4) or "").strip()
            # Suffix examples: '[COMPLETED]', '[COMPLETED — partial]', '(Deload)', ''
            tag = suffix.strip("[]() ").lower() or None
            iso = _parse_dow_date(dow_date, year) or ""
            # Find the next table within this section
            sect_end = n
            for j in range(i + 1, n):
                if lines[j].startswith("## "):
                    sect_end = j
                    break
            tables = _parse_md_tables(lines[i + 1 : sect_end])
            prescribed: list[PrescribedRow] = []
            if tables:
                _, header, rows = tables[0]
                # Expect: Exercise | Sets | Reps | Weight | RIR
                if [h.lower() for h in header[:5]] == ["exercise", "sets", "reps", "weight", "rir"]:
                    for r in rows:
                        if len(r) < 5:
                            continue
                        ex_raw = r[0]
                        sets = _parse_int(r[1]) or 0
                        rep_low, rep_high = _parse_reps_cell(r[2])
                        weight, weight_note = _parse_weight_cell(r[3])
                        rir = _parse_int(r[4])
                        prescribed.append(
                            PrescribedRow(
                                exercise_name=canonical_exercise_name(ex_raw),
                                sets_planned=sets,
                                rep_low=rep_low,
                                rep_high=rep_high,
                                weight_lb=weight,
                                rir_target=rir,
                                notes=weight_note,
                            )
                        )
            sessions.append(
                SessionPrescription(
                    day_number=day,
                    workout_letter=letter,
                    planned_date=iso,
                    status_tag=tag,
                    prescribed=prescribed,
                )
            )
            i = sect_end
            continue
        if line.strip().lower().startswith("## workout c"):
            # Parse the table directly after this header.
            sect_end = n
            for j in range(i + 1, n):
                if lines[j].startswith("## "):
                    sect_end = j
                    break
            tables = _parse_md_tables(lines[i + 1 : sect_end])
            if tables:
                _, header, rows = tables[0]
                if [h.lower() for h in header[:5]] == ["exercise", "sets", "reps", "weight", "rir"]:
                    for r in rows:
                        if len(r) < 5:
                            continue
                        ex_raw = r[0]
                        sets = _parse_int(r[1]) or 0
                        rep_low, _ = _parse_reps_cell(r[2])
                        weight, _ = _parse_weight_cell(r[3])
                        rir = _parse_int(r[4])
                        workout_c.append(
                            WorkoutCTemplateRow(
                                exercise_name=canonical_exercise_name(ex_raw),
                                sets=sets,
                                reps=rep_low or 0,
                                weight_lb=weight,
                                rir=rir,
                            )
                        )
            i = sect_end
            continue
        i += 1
    return sessions, workout_c


# --- workoutlog.md parsing --------------------------------------------------

_WORKOUTLOG_SESSION_RE = re.compile(
    r"^##\s*Session\s+(\d+)\s+—\s+(\w+\s+\w+\s+\d+)\s+—\s+Workout\s+([ABC])\s*(.*)$"
)
_WORKOUTLOG_EXTRA_RE = re.compile(
    r"^##\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+—\s+(.+)$"
)
_HEVY_URL_RE = re.compile(r"hevy\.com/workout/\S+")
_STATUS_LINE_RE = re.compile(r"^\*\*Status:\*\*\s+(\w+)", re.IGNORECASE)
_KG_LB_PAREN_RE = re.compile(r"\(~?(\d+(?:\.\d+)?)\s*lb(?:/hand)?\)")
_SETS_REPS_RE = re.compile(r"(\d+)\s*[×x]\s*(\d+)")
_AT_WEIGHT_RE = re.compile(r"@\s*([0-9]+(?:\.\d+)?)\s*(lb|kg|level\s+\d+)?", re.IGNORECASE)


def parse_prescribed_text(s: str) -> tuple[int, int | None, int | None, float | None, int | None]:
    """Parse '3×8 @ 30, RIR 3' → (sets, rep_low, rep_high, weight, rir).

    Used as a fallback when mesocycle1.md says 'See workout-log' (Sessions 1–4)
    and we have to synthesize prescription rows from workoutlog.md's
    'Prescribed' column.

    Examples:
        '3×8 @ 30, RIR 3'                         → (3, 8, 8, 30, 3)
        '2×10 BW, RIR 2'                          → (2, 10, 10, None, 2)
        '2×10 @ 40 EZ, RIR 2'                     → (2, 10, 10, 40, 2)
        '3×12 @ 90, RIR 2 (revised mid-session…)' → (3, 12, 12, 90, 2)
        '3×8 @ 105, RIR 2'                        → (3, 8, 8, 105, 2)
    """
    s = s.strip()
    sets, rep_low, rep_high, weight, rir = 0, None, None, None, None
    m = _SETS_REPS_RE.search(s)
    if m:
        sets = int(m.group(1))
        rep_low = rep_high = int(m.group(2))
    if not re.search(r"\bBW\b", s):
        wm = re.search(r"@\s*([0-9]+(?:\.\d+)?)", s)
        if wm:
            weight = float(wm.group(1))
    rm = re.search(r"RIR\s+(\d+)", s, re.IGNORECASE)
    if rm:
        rir = int(rm.group(1))
    return sets, rep_low, rep_high, weight, rir


def parse_actual_cell(cell: str) -> tuple[list[ActualSet], str]:
    """Parse an 'Actual' or 'Done' cell into ActualSet rows + a status hint.

    Examples:
        '3×8 @ 30 lb'                    → 3 sets, weight 30
        '1×12 @ 24 lb, 1×10 @ 20 lb'     → 2 sets at different weights
        '3×8 @ 10 kg (~22 lb)'           → 3 sets, weight 22 (lb conversion)
        '3×12 @ level 4'                 → 3 sets, weight None, note 'level 4'
        '— (skipped)'                    → status='skipped', no sets
        '— (deferred)'                   → status='deferred', no sets
        '2×20 @ 9 lb'                    → 2 sets, weight 9
    """
    cell = cell.strip()
    if not cell or cell.startswith("—"):
        if "skip" in cell.lower():
            return [], "skipped"
        if "defer" in cell.lower():
            return [], "deferred"
        return [], "skipped"

    out: list[ActualSet] = []
    set_no = 0
    for chunk in cell.split(","):
        chunk = chunk.strip()
        m = _SETS_REPS_RE.search(chunk)
        if not m:
            continue
        n_sets = int(m.group(1))
        reps = int(m.group(2))
        # weight: prefer "(~22 lb)" parenthetical (kg→lb conversion already
        # done by author), else fall back to the raw "@ N" with unit detection
        lb_paren = _KG_LB_PAREN_RE.search(chunk)
        weight: float | None
        note: str | None = None
        if lb_paren:
            weight = float(lb_paren.group(1))
        else:
            am = _AT_WEIGHT_RE.search(chunk)
            if am:
                val = float(am.group(1))
                unit = (am.group(2) or "").lower()
                if unit.startswith("level"):
                    weight = None
                    note = unit  # e.g. "level 4"
                elif unit == "kg":
                    weight = round(val * 2.20462, 1)
                    note = f"{val:g} kg"
                else:
                    weight = val
            else:
                # "level 12" without "@" is also possible
                lvl = re.search(r"level\s+(\d+)", chunk, re.IGNORECASE)
                if lvl:
                    weight = None
                    note = f"level {lvl.group(1)}"
                else:
                    weight = None
        for _ in range(n_sets):
            set_no += 1
            out.append(
                ActualSet(
                    set_number=set_no,
                    reps_actual=reps,
                    weight_actual=weight,
                    rir_actual=None,
                    status="completed",
                    notes=note,
                )
            )
    return out, "completed"


def _section_text(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start:end]).strip()


def _section_status(text: str) -> str | None:
    m = _STATUS_LINE_RE.search(text)
    if m:
        word = m.group(1).lower()
        if word == "partial":
            return "partial"
        if word == "complete" or word == "completed":
            return "completed"
    return None


def parse_workoutlog(md: str, year: int) -> tuple[dict[int, SessionActuals], list[SessionActuals], list[IssueRow]]:
    """Parse workoutlog.md → (actuals_by_session, extras, issues)."""
    lines = md.splitlines()
    n = len(lines)
    actuals: dict[int, SessionActuals] = {}
    extras: list[SessionActuals] = []
    issues: list[IssueRow] = []

    i = 0
    while i < n:
        line = lines[i]
        if not line.startswith("## "):
            i += 1
            continue
        # Find section bounds
        sect_end = n
        for j in range(i + 1, n):
            if lines[j].startswith("## "):
                sect_end = j
                break
        body = _section_text(lines, i + 1, sect_end)

        # Active Issues
        if line.lower().startswith("## active issues"):
            tables = _parse_md_tables(lines[i + 1 : sect_end])
            if tables:
                _, header, rows = tables[0]
                # Expect: Item | Status | Action
                if [h.lower() for h in header[:3]] == ["item", "status", "action"]:
                    for r in rows:
                        if len(r) >= 3:
                            issues.append(
                                IssueRow(
                                    item=r[0].strip("*"),
                                    status=r[1].strip("*"),
                                    action=r[2].strip("*"),
                                )
                            )
            i = sect_end
            continue

        # Numbered session
        m = _WORKOUTLOG_SESSION_RE.match(line)
        if m:
            day = int(m.group(1))
            dow_date = m.group(2)
            iso = _parse_dow_date(dow_date, year) or ""
            status = _section_status(body) or "completed"
            hevy_match = _HEVY_URL_RE.search(body)
            actuals[day] = SessionActuals(
                day_number=day,
                label=line.strip("# "),
                planned_date=iso,
                status=status,
                narrative_md=body,
                hevy_url=hevy_match.group(0) if hevy_match else None,
                rows=_parse_actuals_rows(lines[i + 1 : sect_end]),
            )
            i = sect_end
            continue

        # Extra day (Carryover, Accessory Pickup) — has a date and a non-Session label
        m2 = _WORKOUTLOG_EXTRA_RE.match(line)
        if m2:
            mon, day_n, label = m2.group(2), int(m2.group(3)), m2.group(4).strip()
            iso = date(year, _MONTHS[mon], day_n).isoformat()
            if "missed session" in label.lower():
                # Don't store missed sessions as rows; they're narrative-only.
                i = sect_end
                continue
            hevy_match = _HEVY_URL_RE.search(body)
            extras.append(
                SessionActuals(
                    day_number=None,
                    label=line.strip("# "),
                    planned_date=iso,
                    status="extra",
                    narrative_md=body,
                    hevy_url=hevy_match.group(0) if hevy_match else None,
                    rows=_parse_actuals_rows(lines[i + 1 : sect_end]),
                )
            )
            i = sect_end
            continue

        i = sect_end

    return actuals, extras, issues


def _parse_actuals_rows(section_lines: list[str]) -> dict[str, ActualRow]:
    tables = _parse_md_tables(section_lines)
    if not tables:
        return {}
    _, header, rows = tables[0]
    headers_lc = [h.lower() for h in header]
    # Schema A (numbered sessions): Exercise | Prescribed | Actual | Notes
    # Schema B (extras):            Exercise | Done | Notes
    if "actual" in headers_lc:
        actual_idx = headers_lc.index("actual")
        prescribed_idx = headers_lc.index("prescribed") if "prescribed" in headers_lc else None
        notes_idx = headers_lc.index("notes") if "notes" in headers_lc else None
    elif "done" in headers_lc:
        actual_idx = headers_lc.index("done")
        prescribed_idx = None
        notes_idx = headers_lc.index("notes") if "notes" in headers_lc else None
    else:
        return {}
    out: dict[str, ActualRow] = {}
    for r in rows:
        if len(r) <= actual_idx:
            continue
        ex_raw = r[0]
        canonical = canonical_exercise_name(ex_raw)
        actual_text = r[actual_idx]
        prescribed_text = r[prescribed_idx] if prescribed_idx is not None and len(r) > prescribed_idx else ""
        notes_text = r[notes_idx] if notes_idx is not None and len(r) > notes_idx else ""
        sets, _ = parse_actual_cell(actual_text)
        # Stamp any "RIR N" found in the prescribed cell into rir_actual? No —
        # leave RIR blank unless explicitly logged (we don't have it in the md).
        out[canonical] = ActualRow(
            exercise_name=canonical,
            prescribed_text=prescribed_text,
            actual_text=actual_text,
            notes_text=notes_text,
            parsed_sets=sets,
        )
    return out


# --- assembly ---------------------------------------------------------------


def build_seed_data(
    trainingprogram_md: str,
    mesocycle_md: str,
    workoutlog_md: str,
    year: int = 2026,
) -> SeedData:
    revisions = parse_revisions(trainingprogram_md)
    sessions, workout_c = parse_mesocycle_sessions(mesocycle_md, year)
    actuals_by_day, extras, issues = parse_workoutlog(workoutlog_md, year)
    # Mesocycle start = Session 1 planned_date.
    start = next(
        (s.planned_date for s in sessions if s.day_number == 1),
        f"{year}-01-01",
    )
    return SeedData(
        mesocycle_name="Mesocycle 1",
        mesocycle_start=start,
        workout_c=workout_c,
        sessions=sessions,
        extras=extras,
        actuals_by_day=actuals_by_day,
        revisions=revisions,
        issues=issues,
    )


# --- DB writes --------------------------------------------------------------


def _upsert_exercise(conn, name: str) -> int:
    meta = EXERCISE_METADATA.get(name, {})
    cur = conn.execute("SELECT id FROM exercises WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        """
        INSERT INTO exercises
            (name, category, primary_muscles, notation, is_bodyweight, default_tempo)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            meta.get("category"),
            meta.get("primary_muscles"),
            meta.get("notation", "total"),
            1 if meta.get("is_bodyweight") else 0,
            meta.get("default_tempo"),
        ),
    )
    return cur.lastrowid


def _insert_session(conn, mesocycle_id: int, sess: SessionPrescription, actuals: SessionActuals | None) -> int:
    # Map status_tag + actuals → final status.
    if actuals is not None:
        status = actuals.status  # 'completed' or 'partial'
    else:
        status = "planned"
    # Deload sessions: tag like "deload" in mesocycle1.md? It's in the header
    # like "## Session 10 — ... (Deload)" — captured as workout_letter? No, the
    # header pattern doesn't capture (Deload). We could detect it but for Phase 1
    # keep status='planned'.
    cur = conn.execute(
        """
        INSERT INTO sessions
            (mesocycle_id, day_number, planned_date, completed_at,
             workout_letter, status, narrative_md, hevy_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mesocycle_id,
            sess.day_number,
            sess.planned_date or None,
            None,  # completed_at — we don't backfill timestamps
            sess.workout_letter,
            status,
            actuals.narrative_md if actuals else None,
            actuals.hevy_url if actuals else None,
        ),
    )
    return cur.lastrowid


def _insert_extra_session(conn, mesocycle_id: int, extra: SessionActuals) -> int:
    cur = conn.execute(
        """
        INSERT INTO sessions
            (mesocycle_id, day_number, planned_date, completed_at,
             workout_letter, status, narrative_md, hevy_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mesocycle_id,
            None,
            extra.planned_date,
            None,
            None,
            "extra",
            extra.narrative_md,
            extra.hevy_url,
        ),
    )
    return cur.lastrowid


def _insert_prescribed(conn, session_id: int, position: int, prx: PrescribedRow, exercise_id: int) -> int:
    cur = conn.execute(
        """
        INSERT INTO prescribed
            (session_id, position, exercise_id, sets_planned, rep_low,
             rep_high, weight_lb, rir_target, tempo, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            position,
            exercise_id,
            prx.sets_planned,
            prx.rep_low,
            prx.rep_high,
            prx.weight_lb,
            prx.rir_target,
            None,  # tempo from EXERCISE_METADATA via exercise.default_tempo
            prx.notes,
        ),
    )
    return cur.lastrowid


def _insert_set(conn, prescribed_id: int, s: ActualSet) -> None:
    conn.execute(
        """
        INSERT INTO sets
            (prescribed_id, set_number, reps_actual, weight_actual,
             rir_actual, status, notes, logged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prescribed_id,
            s.set_number,
            s.reps_actual,
            s.weight_actual,
            s.rir_actual,
            s.status,
            s.notes,
            None,
        ),
    )


def _insert_skipped_or_deferred(conn, prescribed_id: int, status: str, notes: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO sets
            (prescribed_id, set_number, status, notes)
        VALUES (?, 1, ?, ?)
        """,
        (prescribed_id, status, notes),
    )


def write_seed(conn, data: SeedData) -> dict[str, int]:
    """Insert all parsed data inside a transaction. Returns counts."""
    counts = {"sessions": 0, "prescribed": 0, "sets": 0, "extras": 0, "exercises": 0,
              "revisions": 0, "issues": 0, "workout_c": 0}

    # Mesocycle
    cur = conn.execute(
        "INSERT INTO mesocycles (name, start_date, status) VALUES (?, ?, ?)",
        (data.mesocycle_name, data.mesocycle_start, "active"),
    )
    meso_id = cur.lastrowid

    # Workout C template
    if data.workout_c:
        prescription = [
            {
                "exercise_name": r.exercise_name,
                "sets": r.sets,
                "reps": r.reps,
                "weight_lb": r.weight_lb,
                "rir": r.rir,
            }
            for r in data.workout_c
        ]
        conn.execute(
            "INSERT INTO workout_templates (letter, name, prescription_json) VALUES (?, ?, ?)",
            ("C", "Optional Pump & Recovery", json.dumps(prescription)),
        )
        counts["workout_c"] = len(prescription)
        # Also bootstrap exercise rows for the C template.
        for r in data.workout_c:
            _upsert_exercise(conn, r.exercise_name)

    # Revisions
    for r in data.revisions:
        conn.execute(
            "INSERT INTO revisions (mesocycle_id, date, change, reason) VALUES (?, ?, ?, ?)",
            (meso_id, r.date, r.change, r.reason),
        )
        counts["revisions"] += 1

    # Issues — open all of them as of mesocycle start; resolved/permanent flagged in status text
    for issue in data.issues:
        conn.execute(
            "INSERT INTO issues (opened_at, item, status, action) VALUES (?, ?, ?, ?)",
            (data.mesocycle_start, issue.item, issue.status, issue.action),
        )
        counts["issues"] += 1

    # Sessions + prescribed + sets
    for sess in data.sessions:
        actuals = data.actuals_by_day.get(sess.day_number)
        sess_id = _insert_session(conn, meso_id, sess, actuals)
        counts["sessions"] += 1
        # Pick prescription source. Sessions 1–4 in mesocycle1.md just say
        # "See workout-log" with no prescription table; synthesize the
        # prescription from workoutlog.md's "Prescribed" column.
        prescribed_rows = list(sess.prescribed)
        if not prescribed_rows and actuals is not None:
            for ex_name, row in actuals.rows.items():
                sets_p, rl, rh, w, rir = parse_prescribed_text(row.prescribed_text)
                prescribed_rows.append(PrescribedRow(
                    exercise_name=ex_name,
                    sets_planned=sets_p or 1,
                    rep_low=rl,
                    rep_high=rh,
                    weight_lb=w,
                    rir_target=rir,
                    notes=None,
                ))
        for position, prx in enumerate(prescribed_rows, start=1):
            ex_id = _upsert_exercise(conn, prx.exercise_name)
            prescribed_id = _insert_prescribed(conn, sess_id, position, prx, ex_id)
            counts["prescribed"] += 1
            if actuals is None:
                continue
            row = actuals.rows.get(prx.exercise_name)
            if row is None:
                continue
            if not row.parsed_sets:
                # Skipped/deferred
                lc = row.actual_text.lower()
                status = "deferred" if "defer" in lc else "skipped"
                _insert_skipped_or_deferred(conn, prescribed_id, status, row.notes_text or None)
                counts["sets"] += 1
            else:
                for s in row.parsed_sets:
                    _insert_set(conn, prescribed_id, s)
                    counts["sets"] += 1

    # Extras (Apr 23 carryover, Apr 30 pickup)
    for extra in data.extras:
        sess_id = _insert_extra_session(conn, meso_id, extra)
        counts["extras"] += 1
        for position, (ex_name, row) in enumerate(extra.rows.items(), start=1):
            ex_id = _upsert_exercise(conn, ex_name)
            # For extras we don't have a real prescription; create a stub with
            # sets_planned = the actual count, reps from the first set, weight
            # from the first set (so /sessions can render uniformly).
            first = row.parsed_sets[0] if row.parsed_sets else None
            sets_planned = len(row.parsed_sets) or 1
            cur = conn.execute(
                """
                INSERT INTO prescribed
                    (session_id, position, exercise_id, sets_planned,
                     rep_low, rep_high, weight_lb, rir_target, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sess_id,
                    position,
                    ex_id,
                    sets_planned,
                    first.reps_actual if first else None,
                    first.reps_actual if first else None,
                    first.weight_actual if first else None,
                    None,
                    "from extra-day pickup",
                ),
            )
            prescribed_id = cur.lastrowid
            counts["prescribed"] += 1
            for s in row.parsed_sets:
                _insert_set(conn, prescribed_id, s)
                counts["sets"] += 1

    counts["exercises"] = conn.execute("SELECT count(*) FROM exercises").fetchone()[0]
    return counts


# --- entry point ------------------------------------------------------------


def _project_default_db() -> Path:
    return Path(__file__).parent / "data" / "gym.db"


def _default_source_dir() -> Path:
    return Path("C:/Users/bwolf/Downloads/files")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-dir", type=Path, default=_default_source_dir())
    p.add_argument("--db", type=Path, default=_project_default_db())
    p.add_argument("--reset", action="store_true",
                   help="wipe and re-seed even if Mesocycle 1 already exists")
    p.add_argument("--year", type=int, default=2026,
                   help="year used to resolve session dates (default: 2026)")
    return p.parse_args(argv)


def _read_md(source_dir: Path, name: str) -> str:
    path = source_dir / name
    if not path.exists():
        sys.exit(f"missing source file: {path}")
    return path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.reset:
        db.reset_db(args.db)
    else:
        db.init_db(args.db)

    import sqlite3
    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        existing = conn.execute(
            "SELECT id FROM mesocycles WHERE name = ?",
            ("Mesocycle 1",),
        ).fetchone()
        if existing and not args.reset:
            print("Mesocycle 1 already exists. Use --reset to wipe and re-seed.",
                  file=sys.stderr)
            return 1

        tp = _read_md(args.source_dir, "trainingprogram.md")
        me = _read_md(args.source_dir, "mesocycle1.md")
        wl = _read_md(args.source_dir, "workoutlog.md")

        data = build_seed_data(tp, me, wl, year=args.year)
        with conn:
            counts = write_seed(conn, data)
    finally:
        conn.close()

    print("seeded:")
    for k, v in counts.items():
        print(f"  {k:>12} = {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
