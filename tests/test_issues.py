"""Tests for the /issues page."""

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
    return app_and_db[0].test_client()


def _open_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_issues_list_renders_seeded_open(client) -> None:
    r = client.get("/issues")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # All 9 seeded issues are open by default; surface a couple of recognisable items.
    assert "Lower back" in body
    assert "Open (" in body


def test_create_issue(client, app_and_db) -> None:
    r = client.post("/issues", data={
        "item": "Right shoulder twinge on Incline DB Bench",
        "status": "yellow",
        "action": "Drop a few lb, watch over Session 5",
        "severity": "2/10",
    })
    assert r.status_code == 302
    conn = _open_conn(app_and_db[1])
    row = conn.execute(
        "SELECT * FROM issues WHERE item LIKE '%shoulder twinge%'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "yellow"
    assert row["severity"] == "2/10"
    assert row["closed_at"] is None


def test_create_issue_requires_item(client) -> None:
    r = client.post("/issues", data={"item": ""})
    assert r.status_code == 302  # silently ignored, redirects back


def test_close_issue(client, app_and_db) -> None:
    conn = _open_conn(app_and_db[1])
    issue_id = conn.execute(
        "SELECT id FROM issues WHERE closed_at IS NULL LIMIT 1"
    ).fetchone()["id"]
    conn.close()
    r = client.post(f"/issues/{issue_id}/close")
    assert r.status_code == 302
    conn = _open_conn(app_and_db[1])
    closed_at = conn.execute(
        "SELECT closed_at FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()["closed_at"]
    assert closed_at is not None


def test_reopen_issue(client, app_and_db) -> None:
    conn = _open_conn(app_and_db[1])
    issue_id = conn.execute(
        "SELECT id FROM issues WHERE closed_at IS NULL LIMIT 1"
    ).fetchone()["id"]
    conn.close()
    client.post(f"/issues/{issue_id}/close")
    r = client.post(f"/issues/{issue_id}/reopen")
    assert r.status_code == 302
    conn = _open_conn(app_and_db[1])
    closed_at = conn.execute(
        "SELECT closed_at FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()["closed_at"]
    assert closed_at is None


def test_close_issue_404_on_missing(client) -> None:
    r = client.post("/issues/9999/close")
    assert r.status_code == 404


def test_closed_section_shows_closed_issue(client, app_and_db) -> None:
    conn = _open_conn(app_and_db[1])
    issue_id = conn.execute(
        "SELECT id FROM issues WHERE closed_at IS NULL LIMIT 1"
    ).fetchone()["id"]
    conn.close()
    client.post(f"/issues/{issue_id}/close")
    r = client.get("/issues")
    body = r.get_data(as_text=True)
    assert "Closed</strong>" in body
    assert "Reopen" in body
