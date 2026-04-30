"""Tests for /stats and the volume.py rollups."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app as app_module
import seed
import volume


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def app_and_db(tmp_path: Path):
    db_path = tmp_path / "gym.db"
    seed.main([
        "--source-dir", str(FIXTURES),
        "--db", str(db_path),
        "--reset",
    ])
    flask_app = app_module.create_app({"DATABASE": str(db_path), "TESTING": True})
    return flask_app, db_path


@pytest.fixture
def conn(app_and_db) -> sqlite3.Connection:
    c = sqlite3.connect(app_and_db[1])
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def client(app_and_db):
    return app_and_db[0].test_client()


# --- volume.py rollups -----------------------------------------------------


def test_volume_by_muscle_week_groups_completed_sets(conn) -> None:
    meso_id = conn.execute("SELECT id FROM mesocycles WHERE name = 'Mesocycle 1'").fetchone()["id"]
    rollup = volume.volume_by_muscle_week(conn, meso_id)
    # Chest sets through Apr 30: Session 1 (3 incl. bench) + Session 4 (3 flat).
    # Plus Session 2 (3 flat) + Session 3 (3 incl). 12 chest sets total.
    chest_total = sum(rollup["chest"].values())
    assert chest_total == 12
    # Back gets row + pulldown volume — should be > chest.
    back_total = sum(rollup["back"].values())
    assert back_total >= chest_total


def test_volume_buckets_by_week(conn) -> None:
    meso_id = conn.execute("SELECT id FROM mesocycles WHERE name = 'Mesocycle 1'").fetchone()["id"]
    rollup = volume.volume_by_muscle_week(conn, meso_id)
    # Mesocycle starts Wed Apr 22. Sessions 1-2 in week 1, 3-4 in week 2.
    weeks = list(rollup["chest"].keys())
    assert weeks[0].startswith("W1 (2026-04-22")
    # At least 2 weeks of data
    assert len(weeks) >= 2


def test_strength_trend_for_incline_db_bench(conn) -> None:
    meso_id = conn.execute("SELECT id FROM mesocycles WHERE name = 'Mesocycle 1'").fetchone()["id"]
    ex = conn.execute(
        "SELECT id FROM exercises WHERE name = 'Incline DB Bench'"
    ).fetchone()
    trend = volume.strength_trend(conn, ex["id"], meso_id)
    # Sessions 1 + 3 logged Incline DB Bench at 30 lb × 8 reps each.
    assert len(trend) >= 2
    s1 = trend[0]
    assert s1["top_weight"] == 30.0
    assert s1["top_reps"] == 8
    # Epley estimate
    assert s1["e1rm"] == round(30 * (1 + 8/30), 1)


def test_strength_trend_picks_top_weight(conn) -> None:
    """If the same session has two sets at different weights, top wins."""
    meso_id = conn.execute("SELECT id FROM mesocycles WHERE name = 'Mesocycle 1'").fetchone()["id"]
    ex = conn.execute(
        "SELECT id FROM exercises WHERE name = 'Overhead DB Triceps Ext'"
    ).fetchone()
    trend = volume.strength_trend(conn, ex["id"], meso_id)
    # Session 1: 1×12 @ 24 + 1×10 @ 20 → top weight = 24
    assert trend[0]["top_weight"] == 24
    assert trend[0]["top_reps"] == 12


def test_pain_by_week_returns_label_keys(conn) -> None:
    meso_id = conn.execute("SELECT id FROM mesocycles WHERE name = 'Mesocycle 1'").fetchone()["id"]
    by_week = volume.pain_by_week(conn, meso_id)
    # All 9 seeded issues are opened on the mesocycle start date (Apr 22) by
    # the importer; they should land entirely in week 1.
    assert sum(by_week.values()) == 9
    week_labels = list(by_week.keys())
    assert week_labels[0].startswith("W1")


def test_bodyweight_trend_empty_when_no_weighins(conn) -> None:
    assert volume.bodyweight_trend(conn) == []


def test_trained_exercises_only_includes_logged(conn) -> None:
    meso_id = conn.execute("SELECT id FROM mesocycles WHERE name = 'Mesocycle 1'").fetchone()["id"]
    rows = volume.trained_exercises(conn, meso_id)
    names = [r["name"] for r in rows]
    assert "Incline DB Bench" in names
    # EZ-Bar Curl was skipped in Session 1; no weight_actual → not trained.
    assert "EZ-Bar Curl" not in names


# --- /stats route ----------------------------------------------------------


def test_stats_page_renders(client) -> None:
    r = client.get("/stats")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Volume per muscle" in body
    assert "Strength trend" in body
    assert "Bodyweight" in body
    # Inlined JSON payload for charts must be present.
    assert "__statsData" in body


def test_stats_strength_dropdown_changes_exercise(client, app_and_db) -> None:
    conn = sqlite3.connect(app_and_db[1])
    conn.row_factory = sqlite3.Row
    ex = conn.execute("SELECT id FROM exercises WHERE name = 'BB Back Squat'").fetchone()
    r = client.get(f"/stats?exercise_id={ex['id']}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "BB Back Squat" in body


def test_stats_with_seeded_weighins(client, app_and_db) -> None:
    client.post("/metrics/weigh-in", data={"date": "2026-04-22", "weight_lb": "186"})
    client.post("/metrics/weigh-in", data={"date": "2026-04-29", "weight_lb": "185.4", "waist_in": "36"})
    r = client.get("/stats")
    body = r.get_data(as_text=True)
    # Bodyweight section should now be populated; the inlined JSON must have
    # both rows.
    assert "186" in body
    assert "185.4" in body
