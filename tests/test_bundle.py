"""Tests for markdown_views.py + claude_bundle.py + /claude."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import app as app_module
import claude_bundle
import markdown_views
import seed


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


def _meso_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT id FROM mesocycles WHERE name='Mesocycle 1'").fetchone()["id"]


# --- markdown views --------------------------------------------------------


def test_mesocycle_view_lists_all_sessions(conn) -> None:
    md = markdown_views.mesocycle_view(conn, _meso_id(conn))
    assert md.startswith("# Mesocycle 1")
    for n in range(1, 13):
        assert f"## Session {n}" in md
    # Session 5 — Workout A header is on schedule
    assert "Session 5 — 2026-05-01 — Workout A" in md
    # A specific prescription cell from Session 5
    assert "| Incline DB Bench | 3 | 8 | 35 /hand | 2 |" in md


def test_mesocycle_view_marks_partial_and_deload(conn) -> None:
    md = markdown_views.mesocycle_view(conn, _meso_id(conn))
    assert "[COMPLETED — partial]" in md  # Sessions 1, 3, 4
    assert "(Deload)" in md  # Sessions 10-12


def test_workoutlog_view_only_completed_by_default(conn) -> None:
    md = markdown_views.workoutlog_view(conn, _meso_id(conn))
    # Sessions 1-4 are completed/partial; 5+ are planned and should NOT appear.
    assert "Session 1 —" in md
    assert "Session 4 —" in md
    assert "Session 5 —" not in md
    # OH Triceps two-drop set should render specifically.
    assert "1×12 @ 24 lb/hand, 1×10 @ 20 lb/hand" in md or \
           "1×12×24 lb/hand, 1×10×20 lb/hand" in md or \
           "1×12 @ 24" in md


def test_workoutlog_view_collapses_uniform_sets(conn) -> None:
    md = markdown_views.workoutlog_view(conn, _meso_id(conn))
    # Session 1 Incline DB Bench: 3 sets all at 8 reps × 30 lb → "3×8 @ 30 lb/hand"
    assert "3×8 @ 30 lb/hand" in md


def test_workoutlog_view_marks_skipped(conn) -> None:
    md = markdown_views.workoutlog_view(conn, _meso_id(conn))
    assert "skipped" in md
    assert "deferred" in md


def test_issues_view_renders_open_issues_with_ids(conn) -> None:
    md = markdown_views.issues_view(conn)
    assert md.startswith("# Active Issues")
    assert "| id |" in md
    # 9 seeded issues, all open.
    rows = [l for l in md.splitlines() if l.startswith("| ") and not l.startswith("| id")]
    # Header row + separator are filtered above; only data rows remain.
    data_rows = [r for r in rows if not r.startswith("|---")]
    assert len(data_rows) == 9


def test_volume_view_renders_targets(conn) -> None:
    md = markdown_views.volume_view(conn, _meso_id(conn))
    assert md.startswith("# Volume per Muscle / Week")
    assert "chest" in md
    assert "10-12" in md  # chest target band


def test_metrics_view_handles_empty(conn) -> None:
    md = markdown_views.metrics_view(conn)
    assert "No body metrics" in md


def test_revisions_view_renders_seeded_rows(conn) -> None:
    md = markdown_views.revisions_view(conn, _meso_id(conn))
    assert md.startswith("# Revisions Log")
    assert "Triceps" in md  # OH triceps swap revision is in the log


# --- bundle ----------------------------------------------------------------


def test_build_bundle_includes_all_sections(conn) -> None:
    bundle = claude_bundle.build_bundle(conn, _meso_id(conn), trigger="Session 5 done")
    assert "# Workout review request" in bundle
    assert "JSON Schema" in bundle
    assert "# Mesocycle 1" in bundle
    assert "# Workout Log — Mesocycle 1" in bundle
    assert "# Active Issues" in bundle
    assert "# Volume per Muscle / Week" in bundle
    assert "# Revisions Log" in bundle
    assert "Session 5 done" in bundle


def test_build_bundle_handles_empty_trigger(conn) -> None:
    bundle = claude_bundle.build_bundle(conn, _meso_id(conn))
    assert "(no trigger description provided)" in bundle


def test_default_trigger_picks_latest_completed(conn) -> None:
    trig = claude_bundle.default_trigger(conn, _meso_id(conn))
    # Session 4 is the latest partial / completed in the seed.
    assert "Session 4" in trig
    assert "partial" in trig


def test_response_schema_is_valid_json_schema() -> None:
    # Make sure the schema is at least JSON-serializable and has the right shape.
    s = claude_bundle.RESPONSE_SCHEMA
    assert s["type"] == "object"
    assert "revisions" in s["properties"]
    assert "issue_opens" in s["properties"]
    assert "issue_closes" in s["properties"]
    assert "prescription_updates" in s["properties"]
    json.dumps(s)  # serialisable


# --- /claude page ----------------------------------------------------------


def test_claude_page_renders(client) -> None:
    r = client.get("/claude")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Claude review" in body
    assert "Bundle" in body
    assert "# Workout review request" in body
    # Copy button text was shortened from "Copy to clipboard" → "Copy"
    assert 'id="copy-bundle"' in body


def test_claude_page_uses_default_trigger_when_unset(client) -> None:
    r = client.get("/claude")
    body = r.get_data(as_text=True)
    assert "Session 4" in body  # default trigger picked up Session 4


def test_claude_page_accepts_trigger_querystring(client) -> None:
    r = client.get("/claude?trigger=Custom%20review%20please")
    body = r.get_data(as_text=True)
    assert "Custom review please" in body
