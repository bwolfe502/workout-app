"""Schema and migration tests for db.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import db


EXPECTED_TABLES = {
    "mesocycles",
    "workout_templates",
    "sessions",
    "exercises",
    "prescribed",
    "sets",
    "revisions",
    "issues",
    "weigh_ins",
    "daily_metrics",
    "ai_interactions",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def test_init_db_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "gym.db"
    db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    assert EXPECTED_TABLES <= _table_names(conn)


def test_init_db_sets_user_version(tmp_path: Path) -> None:
    db_path = tmp_path / "gym.db"
    db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION


def test_init_db_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "gym.db"
    db.init_db(db_path)
    db.init_db(db_path)  # second call must be a no-op, not a crash
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert EXPECTED_TABLES <= _table_names(conn)


def test_reset_db_wipes_data(tmp_path: Path) -> None:
    db_path = tmp_path / "gym.db"
    db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mesocycles (name, start_date) VALUES (?, ?)",
        ("Mesocycle 1", "2026-04-22"),
    )
    conn.commit()
    conn.close()

    db.reset_db(db_path)
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT count(*) FROM mesocycles").fetchone()[0]
    assert count == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "gym.db"
    db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions (mesocycle_id, status) VALUES (?, ?)",
            (999, "planned"),
        )
        conn.commit()


def test_session_status_check_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "gym.db"
    db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        "INSERT INTO mesocycles (name, start_date) VALUES (?, ?)",
        ("Mesocycle 1", "2026-04-22"),
    )
    meso_id = cur.lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions (mesocycle_id, status) VALUES (?, ?)",
            (meso_id, "bogus"),
        )
        conn.commit()


def test_exercise_notation_check_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "gym.db"
    db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO exercises (name, notation) VALUES (?, ?)",
            ("Bogus", "kilograms"),
        )
        conn.commit()


def test_create_app_initializes_db(tmp_path: Path) -> None:
    """Smoke test: create_app() applies the schema on startup."""
    import app as app_module

    flask_app = app_module.create_app({"DATABASE": str(tmp_path / "gym.db")})
    with flask_app.app_context():
        from db import get_conn

        conn = get_conn()
        assert EXPECTED_TABLES <= _table_names(conn)
