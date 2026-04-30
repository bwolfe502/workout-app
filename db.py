"""SQLite connection + schema management.

The database is the source of truth (markdown is a derived view). This module
owns the schema, a tiny migration helper keyed off `PRAGMA user_version`, and a
per-request connection helper for Flask.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, current_app, g

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mesocycles (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    philosophy_md   TEXT,
    notes_md        TEXT
);

CREATE TABLE IF NOT EXISTS workout_templates (
    id                  INTEGER PRIMARY KEY,
    letter              TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    prescription_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercises (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    category        TEXT,
    primary_muscles TEXT,
    notation        TEXT NOT NULL DEFAULT 'total'
                        CHECK (notation IN ('per_hand', 'total', 'bw')),
    is_bodyweight   INTEGER NOT NULL DEFAULT 0
                        CHECK (is_bodyweight IN (0, 1)),
    default_tempo   TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY,
    mesocycle_id    INTEGER NOT NULL REFERENCES mesocycles(id) ON DELETE CASCADE,
    day_number      INTEGER,
    planned_date    TEXT,
    completed_at    TEXT,
    workout_letter  TEXT,
    status          TEXT NOT NULL DEFAULT 'planned'
                        CHECK (status IN (
                            'planned', 'in_progress', 'completed',
                            'partial', 'extra', 'skipped'
                        )),
    narrative_md    TEXT,
    hevy_url        TEXT
);

CREATE TABLE IF NOT EXISTS prescribed (
    id              INTEGER PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    exercise_id     INTEGER NOT NULL REFERENCES exercises(id),
    sets_planned    INTEGER NOT NULL,
    rep_low         INTEGER,
    rep_high        INTEGER,
    weight_lb       REAL,
    rir_target      INTEGER,
    tempo           TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS sets (
    id              INTEGER PRIMARY KEY,
    prescribed_id   INTEGER NOT NULL REFERENCES prescribed(id) ON DELETE CASCADE,
    set_number      INTEGER NOT NULL,
    reps_actual     INTEGER,
    weight_actual   REAL,
    rir_actual      INTEGER,
    status          TEXT NOT NULL DEFAULT 'completed'
                        CHECK (status IN ('completed', 'skipped', 'deferred')),
    notes           TEXT,
    logged_at       TEXT
);

CREATE TABLE IF NOT EXISTS revisions (
    id              INTEGER PRIMARY KEY,
    mesocycle_id    INTEGER REFERENCES mesocycles(id) ON DELETE SET NULL,
    date            TEXT NOT NULL,
    change          TEXT NOT NULL,
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY,
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    item        TEXT NOT NULL,
    status      TEXT NOT NULL,
    action      TEXT,
    severity    TEXT
);

CREATE TABLE IF NOT EXISTS weigh_ins (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL UNIQUE,
    weight_lb   REAL NOT NULL,
    waist_in    REAL
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    id              INTEGER PRIMARY KEY,
    date            TEXT NOT NULL UNIQUE,
    sleep_hours     REAL,
    energy          INTEGER,
    steps           INTEGER,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS ai_interactions (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    request_md      TEXT NOT NULL,
    response_raw    TEXT,
    parsed_json     TEXT,
    applied_diff    TEXT,
    status          TEXT NOT NULL
                        CHECK (status IN ('pending', 'applied', 'rolled_back', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_mesocycle ON sessions(mesocycle_id, day_number);
CREATE INDEX IF NOT EXISTS idx_prescribed_session ON prescribed(session_id, position);
CREATE INDEX IF NOT EXISTS idx_sets_prescribed   ON sets(prescribed_id, set_number);
CREATE INDEX IF NOT EXISTS idx_revisions_meso    ON revisions(mesocycle_id, date);
CREATE INDEX IF NOT EXISTS idx_issues_status     ON issues(status, opened_at);
"""


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """Apply schema if user_version < SCHEMA_VERSION. Idempotent."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def reset_db(db_path: str | Path) -> None:
    """Wipe and re-apply schema. Used by `python -m seed --reset`."""
    p = Path(db_path)
    if p.exists():
        p.unlink()
    init_db(db_path)


def get_conn() -> sqlite3.Connection:
    """Per-request connection. Cached on Flask `g`."""
    if "db" not in g:
        g.db = _connect(current_app.config["DATABASE"])
    return g.db


def close_conn(_e: Any = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app: Flask) -> None:
    """Wire DB lifecycle into the Flask app."""
    app.config.setdefault(
        "DATABASE",
        str(Path(app.root_path) / "data" / "gym.db"),
    )
    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_conn)
