"""End-to-end seed importer tests against the real source md files.

Fixtures under `tests/fixtures/` are byte-identical copies of the originals
in `C:/Users/bwolf/Downloads/files/`. If those originals change, refresh by
re-copying — there is no partial fixture set.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import db
import seed


FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- pure-parser tests ------------------------------------------------------


def test_parse_revisions_finds_all_rows() -> None:
    md = _read("trainingprogram.md")
    revs = seed.parse_revisions(md)
    # Count rows directly from the source so this test stays accurate if the
    # log grows. Truth: ≥ 8 dated rows.
    assert len(revs) >= 8
    # First row: Apr 25 OH triceps swap
    assert revs[0].date == "2026-04-25"
    assert "Triceps" in revs[0].change
    # Last row should be Apr 29 lower-back observation
    assert revs[-1].date == "2026-04-29"


def test_parse_mesocycle_finds_twelve_sessions_and_workout_c() -> None:
    md = _read("mesocycle1.md")
    sessions, workout_c = seed.parse_mesocycle_sessions(md, year=2026)
    assert len(sessions) == 12
    assert sessions[0].day_number == 1
    assert sessions[0].planned_date == "2026-04-22"
    assert sessions[0].workout_letter == "A"
    # Sessions 5-9 are not deload, sessions 10-12 are
    assert sessions[4].day_number == 5
    assert sessions[4].workout_letter == "A"
    # Workout C: 8 exercises
    assert len(workout_c) == 8
    names = [r.exercise_name for r in workout_c]
    assert "Push-Up (feet on floor)" in names
    assert "Heel-Elevated Goblet Squat" in names


def test_session_5_prescription_matches_md() -> None:
    md = _read("mesocycle1.md")
    sessions, _ = seed.parse_mesocycle_sessions(md, year=2026)
    s5 = next(s for s in sessions if s.day_number == 5)
    by_name = {p.exercise_name: p for p in s5.prescribed}
    # Incline DB Bench: 3×8 @ 35 lb, RIR 2
    incl = by_name["Incline DB Bench"]
    assert (incl.sets_planned, incl.rep_low, incl.weight_lb, incl.rir_target) == (3, 8, 35.0, 2)
    # BB Back Squat at 100 (the canonical name strips the "(or goblet sub)")
    sq = by_name["BB Back Squat"]
    assert (sq.sets_planned, sq.rep_low, sq.weight_lb) == (3, 8, 100.0)


def test_parse_actual_cell_simple_three_sets() -> None:
    sets, _ = seed.parse_actual_cell("3×8 @ 30 lb")
    assert len(sets) == 3
    assert all(s.reps_actual == 8 and s.weight_actual == 30 for s in sets)
    assert [s.set_number for s in sets] == [1, 2, 3]


def test_parse_actual_cell_multiple_chunks() -> None:
    sets, _ = seed.parse_actual_cell("1×12 @ 24 lb, 1×10 @ 20 lb")
    assert len(sets) == 2
    assert sets[0].reps_actual == 12 and sets[0].weight_actual == 24
    assert sets[1].reps_actual == 10 and sets[1].weight_actual == 20


def test_parse_actual_cell_kg_with_lb_paren() -> None:
    sets, _ = seed.parse_actual_cell("3×8 @ 10 kg (~22 lb)")
    assert len(sets) == 3
    assert all(s.weight_actual == 22 for s in sets)


def test_parse_actual_cell_skipped_and_deferred() -> None:
    sets, status = seed.parse_actual_cell("— (skipped)")
    assert sets == [] and status == "skipped"
    sets, status = seed.parse_actual_cell("— (deferred)")
    assert sets == [] and status == "deferred"


def test_parse_actual_cell_kinesis_level() -> None:
    sets, _ = seed.parse_actual_cell("3×8 @ level 12")
    assert len(sets) == 3
    assert all(s.weight_actual is None for s in sets)
    assert all(s.notes and "level" in s.notes for s in sets)


def test_parse_workoutlog_finds_sessions_extras_issues() -> None:
    md = _read("workoutlog.md")
    actuals, extras, issues = seed.parse_workoutlog(md, year=2026)
    assert set(actuals.keys()) == {1, 2, 3, 4}
    # Two extras: Thu Apr 23 carryover, Thu Apr 30 accessory pickup
    assert len(extras) == 2
    assert extras[0].planned_date == "2026-04-23"
    assert extras[1].planned_date == "2026-04-30"
    # Active Issues table has 9 rows
    assert len(issues) == 9


def test_session_1_actuals_capture_oh_triceps_two_drops() -> None:
    md = _read("workoutlog.md")
    actuals, _, _ = seed.parse_workoutlog(md, year=2026)
    s1 = actuals[1]
    oh = s1.rows["Overhead DB Triceps Ext"]
    assert len(oh.parsed_sets) == 2
    assert oh.parsed_sets[0].weight_actual == 24
    assert oh.parsed_sets[1].weight_actual == 20


def test_session_1_ez_bar_curl_skipped() -> None:
    md = _read("workoutlog.md")
    actuals, _, _ = seed.parse_workoutlog(md, year=2026)
    s1 = actuals[1]
    ez = s1.rows["EZ-Bar Curl"]
    assert ez.parsed_sets == []
    assert "skip" in ez.actual_text.lower()


# --- end-to-end seed tests --------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "gym.db"
    seed.main([
        "--source-dir", str(FIXTURES),
        "--db", str(db_path),
        "--reset",
    ])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_seed_creates_one_mesocycle(seeded_db: sqlite3.Connection) -> None:
    rows = seeded_db.execute("SELECT name, start_date FROM mesocycles").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Mesocycle 1"
    assert rows[0]["start_date"] == "2026-04-22"


def test_seed_creates_twelve_sessions_plus_two_extras(seeded_db: sqlite3.Connection) -> None:
    n_sessions = seeded_db.execute(
        "SELECT count(*) FROM sessions WHERE day_number IS NOT NULL"
    ).fetchone()[0]
    n_extras = seeded_db.execute(
        "SELECT count(*) FROM sessions WHERE status = 'extra'"
    ).fetchone()[0]
    assert n_sessions == 12
    assert n_extras == 2


def test_seed_session_1_status_partial(seeded_db: sqlite3.Connection) -> None:
    row = seeded_db.execute(
        "SELECT status FROM sessions WHERE day_number = 1"
    ).fetchone()
    assert row["status"] == "partial"


def test_seed_session_1_oh_triceps_has_two_actual_sets(seeded_db: sqlite3.Connection) -> None:
    row = seeded_db.execute(
        """
        SELECT s.weight_actual, s.reps_actual
          FROM sets s
          JOIN prescribed p ON p.id = s.prescribed_id
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 1
           AND e.name = 'Overhead DB Triceps Ext'
         ORDER BY s.set_number
        """
    ).fetchall()
    assert len(row) == 2
    assert row[0]["weight_actual"] == 24 and row[0]["reps_actual"] == 12
    assert row[1]["weight_actual"] == 20 and row[1]["reps_actual"] == 10


def test_seed_session_1_ez_bar_curl_has_skipped_set(seeded_db: sqlite3.Connection) -> None:
    rows = seeded_db.execute(
        """
        SELECT s.status
          FROM sets s
          JOIN prescribed p ON p.id = s.prescribed_id
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 1
           AND e.name = 'EZ-Bar Curl'
        """
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"


def test_seed_revisions_count_matches_md(seeded_db: sqlite3.Connection) -> None:
    n = seeded_db.execute("SELECT count(*) FROM revisions").fetchone()[0]
    md = _read("trainingprogram.md")
    expected = len(seed.parse_revisions(md))
    assert n == expected
    assert n >= 8


def test_seed_issues_count_nine(seeded_db: sqlite3.Connection) -> None:
    n = seeded_db.execute("SELECT count(*) FROM issues").fetchone()[0]
    assert n == 9


def test_seed_workout_c_template(seeded_db: sqlite3.Connection) -> None:
    row = seeded_db.execute(
        "SELECT letter, prescription_json FROM workout_templates"
    ).fetchone()
    assert row["letter"] == "C"
    import json
    pres = json.loads(row["prescription_json"])
    assert len(pres) == 8
    names = [p["exercise_name"] for p in pres]
    assert "Push-Up (feet on floor)" in names


def test_seed_aborts_without_reset_if_mesocycle_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "gym.db"
    seed.main([
        "--source-dir", str(FIXTURES),
        "--db", str(db_path),
        "--reset",
    ])
    rc = seed.main([
        "--source-dir", str(FIXTURES),
        "--db", str(db_path),
    ])
    assert rc == 1


def test_seed_session_5_incline_db_bench_prescription(seeded_db: sqlite3.Connection) -> None:
    row = seeded_db.execute(
        """
        SELECT p.sets_planned, p.rep_low, p.weight_lb, p.rir_target
          FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 5 AND e.name = 'Incline DB Bench'
        """
    ).fetchone()
    assert row is not None
    assert row["sets_planned"] == 3
    assert row["rep_low"] == 8
    assert row["weight_lb"] == 35.0
    assert row["rir_target"] == 2
