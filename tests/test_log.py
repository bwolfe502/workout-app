"""Tests for /claude/log and rollback."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import app as app_module
import claude_apply
import seed
from claude_apply import ApplyError


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
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture
def client(app_and_db):
    return app_and_db[0].test_client()


def _meso_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT id FROM mesocycles WHERE name='Mesocycle 1'").fetchone()["id"]


# --- snapshot capture during apply -----------------------------------------


def test_apply_snapshot_captures_inserted_revision_id(conn) -> None:
    aiid = claude_apply.apply(
        conn, {"revisions": [{"date": "2026-05-01", "change": "x", "reason": "y"}]},
        _meso_id(conn), request_md="r", response_raw="raw",
    )
    snap = json.loads(conn.execute(
        "SELECT applied_diff FROM ai_interactions WHERE id = ?", (aiid,)
    ).fetchone()["applied_diff"])
    assert len(snap["revisions_added"]) == 1
    assert snap["revisions_added"][0]["id"] is not None


def test_apply_snapshot_captures_prescription_before(conn) -> None:
    aiid = claude_apply.apply(conn, {
        "prescription_updates": [{
            "session_day": 7, "exercise_name": "Incline DB Bench", "weight_lb": 40,
        }],
    }, _meso_id(conn), request_md="r", response_raw="raw")
    snap = json.loads(conn.execute(
        "SELECT applied_diff FROM ai_interactions WHERE id = ?", (aiid,)
    ).fetchone()["applied_diff"])
    pres = snap["prescription_updates"]
    assert len(pres) == 1
    assert pres[0]["before"]["weight_lb"] == 35  # current value before apply


# --- rollback --------------------------------------------------------------


def test_rollback_revision_deletes_row(conn) -> None:
    aiid = claude_apply.apply(conn, {
        "revisions": [{"date": "2026-05-01", "change": "Trial", "reason": "Y"}],
    }, _meso_id(conn), request_md="r", response_raw="raw")
    n_before = conn.execute("SELECT count(*) FROM revisions").fetchone()[0]
    claude_apply.rollback(conn, aiid)
    n_after = conn.execute("SELECT count(*) FROM revisions").fetchone()[0]
    assert n_after == n_before - 1
    status = conn.execute(
        "SELECT status FROM ai_interactions WHERE id = ?", (aiid,)
    ).fetchone()["status"]
    assert status == "rolled_back"


def test_rollback_issue_open_deletes_row(conn) -> None:
    aiid = claude_apply.apply(conn, {
        "issue_opens": [{"item": "Brand new tweak", "status": "yellow"}],
    }, _meso_id(conn), request_md="r", response_raw="raw")
    assert conn.execute(
        "SELECT count(*) FROM issues WHERE item = 'Brand new tweak'"
    ).fetchone()[0] == 1
    claude_apply.rollback(conn, aiid)
    assert conn.execute(
        "SELECT count(*) FROM issues WHERE item = 'Brand new tweak'"
    ).fetchone()[0] == 0


def test_rollback_issue_close_restores_open_state(conn) -> None:
    issue_id = conn.execute(
        "SELECT id FROM issues WHERE closed_at IS NULL LIMIT 1"
    ).fetchone()["id"]
    aiid = claude_apply.apply(conn, {
        "issue_closes": [{"id": issue_id, "reason": "x"}],
    }, _meso_id(conn), request_md="r", response_raw="raw")
    assert conn.execute(
        "SELECT closed_at FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()["closed_at"] is not None
    claude_apply.rollback(conn, aiid)
    assert conn.execute(
        "SELECT closed_at FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()["closed_at"] is None


def test_rollback_prescription_restores_all_fields(conn) -> None:
    before = conn.execute(
        """
        SELECT p.weight_lb, p.rep_low, p.rep_high, p.sets_planned, p.rir_target
          FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 7 AND e.name = 'Incline DB Bench'
        """
    ).fetchone()
    aiid = claude_apply.apply(conn, {
        "prescription_updates": [{
            "session_day": 7, "exercise_name": "Incline DB Bench",
            "weight_lb": 50, "rep_low": 4, "rep_high": 6, "sets_planned": 5,
        }],
    }, _meso_id(conn), request_md="r", response_raw="raw")
    after = conn.execute(
        """
        SELECT p.weight_lb FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 7 AND e.name = 'Incline DB Bench'
        """
    ).fetchone()
    assert after["weight_lb"] == 50
    claude_apply.rollback(conn, aiid)
    restored = conn.execute(
        """
        SELECT p.weight_lb, p.rep_low, p.rep_high, p.sets_planned, p.rir_target
          FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 7 AND e.name = 'Incline DB Bench'
        """
    ).fetchone()
    assert restored["weight_lb"] == before["weight_lb"]
    assert restored["rep_low"] == before["rep_low"]
    assert restored["rep_high"] == before["rep_high"]
    assert restored["sets_planned"] == before["sets_planned"]


def test_rollback_exercise_swap_restores_original_exercise(conn) -> None:
    before_ex = conn.execute(
        """
        SELECT e.name FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 7 AND e.name = 'Incline DB Bench'
        """
    ).fetchone()
    aiid = claude_apply.apply(conn, {
        "prescription_updates": [{
            "session_day": 7, "exercise_name": "Incline DB Bench",
            "new_exercise_name": "Larsen Press (new)",
        }],
    }, _meso_id(conn), request_md="r", response_raw="raw")
    claude_apply.rollback(conn, aiid)
    # The pre-update exercise_id is stored in snapshot.before; rollback
    # should put the prescribed row back on Incline DB Bench.
    after = conn.execute(
        """
        SELECT count(*) FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 7 AND e.name = 'Incline DB Bench'
        """
    ).fetchone()[0]
    assert after == 1


def test_rollback_already_rolled_back_raises(conn) -> None:
    aiid = claude_apply.apply(conn, {
        "revisions": [{"date": "2026-05-01", "change": "x", "reason": "y"}],
    }, _meso_id(conn), request_md="r", response_raw="raw")
    claude_apply.rollback(conn, aiid)
    with pytest.raises(ApplyError, match="not 'applied'"):
        claude_apply.rollback(conn, aiid)


def test_rollback_missing_id_raises(conn) -> None:
    with pytest.raises(ApplyError, match="not found"):
        claude_apply.rollback(conn, 9999)


# --- /claude/log + rollback route ------------------------------------------


def test_log_page_lists_interactions(client, app_and_db) -> None:
    # Apply something to populate the log
    conn = sqlite3.connect(app_and_db[1])
    conn.row_factory = sqlite3.Row
    claude_apply.apply(conn, {
        "revisions": [{"date": "2026-05-01", "change": "Test rev", "reason": "smoke"}],
    }, conn.execute("SELECT id FROM mesocycles WHERE name='Mesocycle 1'").fetchone()["id"],
       request_md="r", response_raw="raw")
    conn.close()
    r = client.get("/claude/log")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Claude audit log" in body
    assert "Test rev" in body
    assert "Roll back" in body


def test_log_page_empty(client) -> None:
    r = client.get("/claude/log")
    body = r.get_data(as_text=True)
    assert "No interactions yet" in body


def test_rollback_route_undoes_change(client, app_and_db) -> None:
    raw = """```json
{"revisions": [{"date": "2026-05-01", "change": "X", "reason": "Y"}]}
```"""
    client.post("/claude/apply", data={"raw": raw, "action": "apply"})
    conn = sqlite3.connect(app_and_db[1])
    conn.row_factory = sqlite3.Row
    aiid = conn.execute("SELECT id FROM ai_interactions").fetchone()["id"]
    conn.close()
    r = client.post(f"/claude/log/{aiid}/rollback", follow_redirects=False)
    assert r.status_code == 302
    conn = sqlite3.connect(app_and_db[1])
    conn.row_factory = sqlite3.Row
    n = conn.execute(
        "SELECT count(*) FROM revisions WHERE date = '2026-05-01' AND change = 'X'"
    ).fetchone()[0]
    assert n == 0
    status = conn.execute(
        "SELECT status FROM ai_interactions WHERE id = ?", (aiid,)
    ).fetchone()["status"]
    assert status == "rolled_back"


def test_rollback_route_shows_error_on_double_rollback(client, app_and_db) -> None:
    raw = """```json
{"revisions": [{"date": "2026-05-01", "change": "X", "reason": "Y"}]}
```"""
    client.post("/claude/apply", data={"raw": raw, "action": "apply"})
    conn = sqlite3.connect(app_and_db[1])
    conn.row_factory = sqlite3.Row
    aiid = conn.execute("SELECT id FROM ai_interactions").fetchone()["id"]
    conn.close()
    client.post(f"/claude/log/{aiid}/rollback")
    r = client.post(f"/claude/log/{aiid}/rollback", follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Rollback failed" in body
    # Apostrophes in error text are HTML-escaped — match the substring loosely.
    assert "applied" in body
    assert "rolled_back" in body
