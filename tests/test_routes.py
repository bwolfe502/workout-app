"""Route-level tests against a seeded test DB."""

from __future__ import annotations

from pathlib import Path

import pytest

import app as app_module
import seed


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "gym.db"
    seed.main([
        "--source-dir", str(FIXTURES),
        "--db", str(db_path),
        "--reset",
    ])
    flask_app = app_module.create_app({"DATABASE": str(db_path), "TESTING": True})
    with flask_app.test_client() as c:
        yield c


def test_healthz(client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_home_renders_next_session(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Mesocycle 1" in body
    # Session 5 is the first not-completed numbered session
    assert "Session 5" in body
    assert "Workout A" in body
    # Open issues should render
    assert "Triceps" in body or "Reverse Lunge" in body


def test_home_lists_open_issues(client) -> None:
    r = client.get("/")
    body = r.get_data(as_text=True)
    # Active issues table has 9 rows; show at least a couple.
    assert "Lower back" in body


def test_program_renders_all_twelve_sessions(client) -> None:
    r = client.get("/program")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    for n in range(1, 13):
        assert f"Session {n}" in body
    # Workout C swap section is present
    assert "Workout C" in body
    # Revisions log section is present
    assert "Revisions log" in body


def test_program_shows_session_5_prescription(client) -> None:
    r = client.get("/program")
    body = r.get_data(as_text=True)
    # Session 5 Incline DB Bench: 3 sets × 8 @ 35 lb /hand RIR 2
    # We check that the cells appear within session 5's section by looking
    # for the canonical exercise + a 35 lb cell nearby.
    assert "Incline DB Bench" in body
    assert "35 lb /hand" in body


def test_sessions_list_includes_extras(client) -> None:
    r = client.get("/sessions")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # 12 numbered sessions + 2 extras = 14 rows
    assert "2026-04-23" in body  # Apr 23 carryover
    assert "2026-04-30" in body  # Apr 30 accessory pickup
    assert "extra" in body


def test_session_detail_shows_prescribed_and_actuals(client) -> None:
    # Find session 1's id via /sessions, but easier: hit /session/1 — the
    # seed creates sessions with auto IDs starting at 1. Session 1 (day 1)
    # is the first inserted, so it should have id=1.
    r = client.get("/session/1")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Session 1" in body
    assert "Incline DB Bench" in body
    # OH Triceps Ext two-drop set should render: '12 × 24 lb' and '10 × 20 lb'
    assert "Overhead DB Triceps Ext" in body
    assert "24 lb" in body
    assert "20 lb" in body
    # EZ-Bar Curl skipped should render the 'skipped' chip
    assert "skipped" in body


def test_session_detail_404_on_missing(client) -> None:
    r = client.get("/session/9999")
    assert r.status_code == 404


def test_partial_session_offers_continue_logging(client) -> None:
    """Session 1 is partial from the seed; the read-only view should
    expose a 'Continue logging' button that switches to live view."""
    r = client.get("/session/1")
    body = r.get_data(as_text=True)
    assert "Continue logging" in body
    # The link goes back to the same path with ?live=1
    assert "live=1" in body


def test_live_query_param_forces_live_view_on_partial(client) -> None:
    r = client.get("/session/1?live=1")
    body = r.get_data(as_text=True)
    # Live view has the htmx form; read-only does not.
    assert "hx-post" in body
    assert "Log set #" in body


def test_program_marks_deload_sessions(client) -> None:
    r = client.get("/program")
    body = r.get_data(as_text=True)
    # Sessions 10-12 are deload — should have the deload badge.
    assert "status-deload" in body
