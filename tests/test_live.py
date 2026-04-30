"""Tests for the htmx live-session mutations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app as app_module
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
def client(app_and_db):
    flask_app, _ = app_and_db
    return flask_app.test_client()


def _open_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _session_5_first_prescribed(db_path: Path) -> tuple[int, int]:
    """Return (session_id, prescribed_id) of Session 5's position-1 row."""
    conn = _open_conn(db_path)
    row = conn.execute(
        """
        SELECT sess.id AS session_id, p.id AS prescribed_id
          FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
         WHERE sess.day_number = 5 AND p.position = 1
        """
    ).fetchone()
    conn.close()
    return row["session_id"], row["prescribed_id"]


def test_session_5_renders_live_view(client, app_and_db) -> None:
    sess_id, _ = _session_5_first_prescribed(app_and_db[1])
    r = client.get(f"/session/{sess_id}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Live view has the htmx form action attribute
    assert "hx-post" in body
    assert "Log set #1" in body
    assert "Incline DB Bench" in body


def test_log_set_persists_and_returns_partial(client, app_and_db) -> None:
    sess_id, prescribed_id = _session_5_first_prescribed(app_and_db[1])
    r = client.post(
        f"/session/{sess_id}/exercise/{prescribed_id}/set",
        data={"weight": "35", "reps": "8", "rir": "2"},
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Returned partial should show the new set chip and a #2 form
    assert "#1: 8 ×" in body
    assert "Log set #2" in body
    # Persisted in DB
    conn = _open_conn(app_and_db[1])
    rows = conn.execute(
        "SELECT * FROM sets WHERE prescribed_id = ? ORDER BY set_number",
        (prescribed_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["weight_actual"] == 35
    assert rows[0]["reps_actual"] == 8
    assert rows[0]["rir_actual"] == 2
    assert rows[0]["status"] == "completed"
    # Session status flipped from planned → in_progress
    sess = conn.execute("SELECT status FROM sessions WHERE id = ?", (sess_id,)).fetchone()
    assert sess["status"] == "in_progress"


def test_log_set_appears_in_session_detail_after_finish(client, app_and_db) -> None:
    sess_id, prescribed_id = _session_5_first_prescribed(app_and_db[1])
    client.post(
        f"/session/{sess_id}/exercise/{prescribed_id}/set",
        data={"weight": "35", "reps": "8", "rir": "2"},
    )
    # The /sessions list immediately reflects in_progress status
    r = client.get("/sessions")
    body = r.get_data(as_text=True)
    assert "in_progress" in body


def test_skip_exercise_writes_marker(client, app_and_db) -> None:
    sess_id, prescribed_id = _session_5_first_prescribed(app_and_db[1])
    r = client.post(f"/session/{sess_id}/exercise/{prescribed_id}/skip")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "skipped" in body
    # Form must be hidden after skipping
    assert "Log set #1" not in body
    conn = _open_conn(app_and_db[1])
    rows = conn.execute(
        "SELECT * FROM sets WHERE prescribed_id = ?", (prescribed_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"


def test_defer_exercise_writes_marker(client, app_and_db) -> None:
    sess_id, prescribed_id = _session_5_first_prescribed(app_and_db[1])
    r = client.post(f"/session/{sess_id}/exercise/{prescribed_id}/defer")
    assert r.status_code == 200
    conn = _open_conn(app_and_db[1])
    rows = conn.execute(
        "SELECT status FROM sets WHERE prescribed_id = ?", (prescribed_id,),
    ).fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "deferred"


def test_skip_after_logged_set_replaces_log(client, app_and_db) -> None:
    sess_id, prescribed_id = _session_5_first_prescribed(app_and_db[1])
    client.post(f"/session/{sess_id}/exercise/{prescribed_id}/set",
                data={"weight": "35", "reps": "8", "rir": "2"})
    client.post(f"/session/{sess_id}/exercise/{prescribed_id}/skip")
    conn = _open_conn(app_and_db[1])
    rows = conn.execute(
        "SELECT status FROM sets WHERE prescribed_id = ?", (prescribed_id,),
    ).fetchall()
    # Skip wipes prior partial sets and writes a single marker.
    assert len(rows) == 1 and rows[0]["status"] == "skipped"


def test_log_set_404_on_wrong_session(client, app_and_db) -> None:
    _, prescribed_id = _session_5_first_prescribed(app_and_db[1])
    r = client.post(
        f"/session/9999/exercise/{prescribed_id}/set",
        data={"weight": "35", "reps": "8", "rir": "2"},
    )
    assert r.status_code == 404


def test_finish_session_marks_completed_and_redirects(client, app_and_db) -> None:
    sess_id, prescribed_id = _session_5_first_prescribed(app_and_db[1])
    # Skip every prescribed exercise so the session counts as fully addressed.
    conn = _open_conn(app_and_db[1])
    pres = conn.execute(
        "SELECT id FROM prescribed WHERE session_id = ? ORDER BY position",
        (sess_id,),
    ).fetchall()
    conn.close()
    for p in pres:
        client.post(f"/session/{sess_id}/exercise/{p['id']}/skip")
    r = client.post(f"/session/{sess_id}/finish",
                    data={"narrative": "felt fine, BB squat clean"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/")
    # Status is 'completed' because every prescribed row is addressed.
    conn = _open_conn(app_and_db[1])
    sess = conn.execute(
        "SELECT status, completed_at, narrative_md FROM sessions WHERE id = ?",
        (sess_id,),
    ).fetchone()
    assert sess["status"] == "completed"
    assert sess["completed_at"] is not None
    assert "BB squat clean" in sess["narrative_md"]


def test_finish_session_with_unaddressed_exercises_marks_partial(client, app_and_db) -> None:
    sess_id, prescribed_id = _session_5_first_prescribed(app_and_db[1])
    # Log only one exercise, leave the rest untouched.
    client.post(f"/session/{sess_id}/exercise/{prescribed_id}/set",
                data={"weight": "35", "reps": "8", "rir": "2"})
    client.post(f"/session/{sess_id}/finish", data={"narrative": "ran out of time"})
    conn = _open_conn(app_and_db[1])
    sess = conn.execute(
        "SELECT status FROM sessions WHERE id = ?", (sess_id,)
    ).fetchone()
    assert sess["status"] == "partial"


def test_log_set_404_on_mismatched_prescribed(client, app_and_db) -> None:
    sess_id, _ = _session_5_first_prescribed(app_and_db[1])
    r = client.post(
        f"/session/{sess_id}/exercise/999999/set",
        data={"weight": "35", "reps": "8", "rir": "2"},
    )
    assert r.status_code == 404
