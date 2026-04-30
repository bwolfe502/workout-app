"""Tests for the inbound Claude pipeline: extract → validate → diff → apply."""

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


def _wrap_json(payload: dict) -> str:
    return f"Here are my updates:\n\n```json\n{json.dumps(payload, indent=2)}\n```\n\nThanks!"


# --- extract / validate ----------------------------------------------------


def test_extract_json_block_finds_fenced() -> None:
    raw = "Some prose\n\n```json\n{\"x\":1}\n```\n\ntrailing"
    assert claude_apply.extract_json_block(raw) == '{"x":1}'


def test_extract_json_block_handles_bare_json() -> None:
    raw = "{\"x\":1}"
    assert claude_apply.extract_json_block(raw) == '{"x":1}'


def test_extract_json_block_rejects_empty() -> None:
    with pytest.raises(ApplyError, match="empty"):
        claude_apply.extract_json_block("")


def test_extract_json_block_rejects_no_fence() -> None:
    with pytest.raises(ApplyError, match="No fenced"):
        claude_apply.extract_json_block("just prose, no JSON in sight")


def test_parse_and_validate_accepts_empty_object() -> None:
    out = claude_apply.parse_and_validate("{}")
    assert out == {}


def test_parse_and_validate_rejects_unknown_top_level_field() -> None:
    bad = json.dumps({"unknown_field": [1, 2, 3]})
    with pytest.raises(ApplyError, match="Schema violation"):
        claude_apply.parse_and_validate(bad)


def test_parse_and_validate_rejects_bad_revision_date() -> None:
    bad = json.dumps({
        "revisions": [{"date": "May 1", "change": "x", "reason": "y"}]
    })
    with pytest.raises(ApplyError, match="Schema violation"):
        claude_apply.parse_and_validate(bad)


def test_parse_and_validate_rejects_bad_json() -> None:
    with pytest.raises(ApplyError, match="Invalid JSON"):
        claude_apply.parse_and_validate("{not json")


# --- build_diff ------------------------------------------------------------


def test_diff_revision_add(conn) -> None:
    diff = claude_apply.build_diff(conn, {
        "revisions": [{"date": "2026-05-01", "change": "Foo", "reason": "Bar"}],
    }, _meso_id(conn))
    assert len(diff.entries) == 1
    e = diff.entries[0]
    assert e.kind == "revision_add"
    assert "Foo" in e.summary
    assert e.error is None


def test_diff_issue_close_existing(conn) -> None:
    issue_id = conn.execute("SELECT id FROM issues WHERE closed_at IS NULL LIMIT 1").fetchone()["id"]
    diff = claude_apply.build_diff(conn, {
        "issue_closes": [{"id": issue_id, "reason": "Resolved"}],
    }, _meso_id(conn))
    assert len(diff.entries) == 1
    assert diff.entries[0].error is None
    assert "Close issue" in diff.entries[0].summary


def test_diff_issue_close_missing(conn) -> None:
    diff = claude_apply.build_diff(conn, {
        "issue_closes": [{"id": 99999}],
    }, _meso_id(conn))
    assert diff.has_errors
    assert "does not exist" in diff.entries[0].error


def test_diff_prescription_change_real_row(conn) -> None:
    diff = claude_apply.build_diff(conn, {
        "prescription_updates": [{
            "session_day": 7,
            "exercise_name": "Incline DB Bench",
            "weight_lb": 40,
        }],
    }, _meso_id(conn))
    assert len(diff.entries) == 1
    e = diff.entries[0]
    assert e.error is None
    assert any("weight_lb" in d for d in e.details)


def test_diff_prescription_change_unknown_session(conn) -> None:
    diff = claude_apply.build_diff(conn, {
        "prescription_updates": [{
            "session_day": 99,
            "exercise_name": "Nonexistent",
            "weight_lb": 100,
        }],
    }, _meso_id(conn))
    assert diff.has_errors


def test_diff_prescription_noop_is_error(conn) -> None:
    """If every field matches current state, the update is flagged as a no-op."""
    diff = claude_apply.build_diff(conn, {
        "prescription_updates": [{
            "session_day": 7,
            "exercise_name": "Incline DB Bench",
            "weight_lb": 35,  # current value
            "rep_low": 8,     # current value
            "sets_planned": 4, # current value
        }],
    }, _meso_id(conn))
    assert diff.entries[0].error is not None
    assert "no-op" in diff.entries[0].error


def test_diff_exercise_swap(conn) -> None:
    diff = claude_apply.build_diff(conn, {
        "prescription_updates": [{
            "session_day": 6,
            "exercise_name": "Barbell RDL",
            "new_exercise_name": "DB Romanian Deadlift",
        }],
    }, _meso_id(conn))
    assert diff.entries[0].error is None
    assert any("exercise:" in d for d in diff.entries[0].details)


# --- apply -----------------------------------------------------------------


def test_apply_revision_writes_row(conn) -> None:
    response = {"revisions": [{"date": "2026-05-01", "change": "Trial X", "reason": "Y"}]}
    aiid = claude_apply.apply(conn, response, _meso_id(conn),
                              request_md="req", response_raw="raw")
    assert aiid > 0
    row = conn.execute("SELECT * FROM revisions WHERE date = '2026-05-01'").fetchone()
    assert row["change"] == "Trial X"
    audit = conn.execute("SELECT * FROM ai_interactions WHERE id = ?", (aiid,)).fetchone()
    assert audit["status"] == "applied"


def test_apply_issue_open_writes_row(conn) -> None:
    response = {
        "issue_opens": [{
            "item": "New tweak",
            "status": "yellow",
            "action": "Watch",
            "severity": "low",
        }],
    }
    claude_apply.apply(conn, response, _meso_id(conn), request_md="r", response_raw="raw")
    row = conn.execute("SELECT * FROM issues WHERE item = 'New tweak'").fetchone()
    assert row is not None
    assert row["status"] == "yellow"
    assert row["closed_at"] is None


def test_apply_issue_close_stamps_today(conn) -> None:
    issue_id = conn.execute("SELECT id FROM issues WHERE closed_at IS NULL LIMIT 1").fetchone()["id"]
    response = {"issue_closes": [{"id": issue_id, "reason": "ok"}]}
    claude_apply.apply(conn, response, _meso_id(conn), request_md="r", response_raw="raw")
    row = conn.execute("SELECT closed_at FROM issues WHERE id = ?", (issue_id,)).fetchone()
    assert row["closed_at"] is not None


def test_apply_prescription_update_changes_weight(conn) -> None:
    response = {"prescription_updates": [{
        "session_day": 7, "exercise_name": "Incline DB Bench", "weight_lb": 40,
    }]}
    claude_apply.apply(conn, response, _meso_id(conn), request_md="r", response_raw="raw")
    row = conn.execute(
        """
        SELECT p.weight_lb FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 7 AND e.name = 'Incline DB Bench'
        """
    ).fetchone()
    assert row["weight_lb"] == 40


def test_apply_exercise_swap_creates_new_exercise(conn) -> None:
    response = {"prescription_updates": [{
        "session_day": 7, "exercise_name": "Incline DB Bench",
        "new_exercise_name": "Larsen Press (new)",
    }]}
    claude_apply.apply(conn, response, _meso_id(conn), request_md="r", response_raw="raw")
    new_ex = conn.execute("SELECT id FROM exercises WHERE name = 'Larsen Press (new)'").fetchone()
    assert new_ex is not None
    row = conn.execute(
        """
        SELECT e.name FROM prescribed p
          JOIN sessions sess ON sess.id = p.session_id
          JOIN exercises e ON e.id = p.exercise_id
         WHERE sess.day_number = 7 AND p.exercise_id = ?
        """,
        (new_ex["id"],),
    ).fetchone()
    assert row["name"] == "Larsen Press (new)"


def test_apply_aborts_when_diff_has_errors(conn) -> None:
    response = {"issue_closes": [{"id": 99999}]}
    with pytest.raises(ApplyError, match="errors; nothing applied"):
        claude_apply.apply(conn, response, _meso_id(conn),
                           request_md="r", response_raw="raw")
    # Confirm nothing landed
    audit = conn.execute("SELECT count(*) FROM ai_interactions").fetchone()[0]
    assert audit == 0


# --- /claude/apply route ---------------------------------------------------


def test_apply_get_renders_form(client) -> None:
    r = client.get("/claude/apply")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Apply Claude response" in body
    assert "Preview" in body


def test_apply_preview_shows_diff(client) -> None:
    raw = _wrap_json({
        "revisions": [{"date": "2026-05-01", "change": "Test", "reason": "Smoke"}],
        "narrative": "Looks good.",
    })
    r = client.post("/claude/apply", data={"raw": raw, "action": "preview"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Diff preview" in body
    assert "Add revision" in body
    assert "Looks good" in body
    # Apply button is shown when diff is clean
    assert "value=\"apply\"" in body


def test_apply_preview_blocks_on_invalid_payload(client) -> None:
    r = client.post("/claude/apply", data={"raw": "not json", "action": "preview"})
    body = r.get_data(as_text=True)
    assert "Error" in body
    assert "No fenced" in body
    # Apply button must NOT appear
    assert "value=\"apply\"" not in body


def test_apply_preview_blocks_apply_when_diff_has_errors(client) -> None:
    raw = _wrap_json({"issue_closes": [{"id": 99999}]})
    r = client.post("/claude/apply", data={"raw": raw, "action": "preview"})
    body = r.get_data(as_text=True)
    assert "does not exist" in body
    assert "value=\"apply\"" not in body


def test_apply_apply_writes_and_shows_confirmation(client, app_and_db) -> None:
    raw = _wrap_json({
        "revisions": [{"date": "2026-05-01", "change": "Bumped Inc DB → 35",
                       "reason": "felt easy"}],
    })
    r = client.post("/claude/apply", data={"raw": raw, "action": "apply"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Applied" in body
    assert "ai_interactions row #" in body
    conn = sqlite3.connect(app_and_db[1])
    conn.row_factory = sqlite3.Row
    n = conn.execute(
        "SELECT count(*) FROM revisions WHERE date = '2026-05-01' AND change LIKE 'Bumped%'"
    ).fetchone()[0]
    assert n == 1
    audit = conn.execute("SELECT status FROM ai_interactions").fetchone()
    assert audit["status"] == "applied"
