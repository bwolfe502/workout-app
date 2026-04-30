"""Tests for /metrics quick-log."""

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


def test_metrics_page_renders(client) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Weigh-in" in body
    assert "Daily quick-log" in body


def test_log_weigh_in_persists(client, app_and_db) -> None:
    r = client.post("/metrics/weigh-in", data={
        "date": "2026-04-30",
        "weight_lb": "186.4",
        "waist_in": "36.5",
    })
    assert r.status_code == 302
    conn = _open_conn(app_and_db[1])
    row = conn.execute(
        "SELECT * FROM weigh_ins WHERE date = '2026-04-30'"
    ).fetchone()
    assert row is not None
    assert row["weight_lb"] == 186.4
    assert row["waist_in"] == 36.5


def test_weigh_in_upserts_on_same_date(client, app_and_db) -> None:
    client.post("/metrics/weigh-in", data={"date": "2026-04-30", "weight_lb": "186"})
    client.post("/metrics/weigh-in", data={"date": "2026-04-30", "weight_lb": "185.5", "waist_in": "36"})
    conn = _open_conn(app_and_db[1])
    rows = conn.execute(
        "SELECT * FROM weigh_ins WHERE date = '2026-04-30'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["weight_lb"] == 185.5
    assert rows[0]["waist_in"] == 36


def test_log_weigh_in_requires_weight(client, app_and_db) -> None:
    client.post("/metrics/weigh-in", data={"date": "2026-04-30", "weight_lb": ""})
    conn = _open_conn(app_and_db[1])
    rows = conn.execute(
        "SELECT * FROM weigh_ins WHERE date = '2026-04-30'"
    ).fetchall()
    assert rows == []


def test_log_daily_persists(client, app_and_db) -> None:
    r = client.post("/metrics/daily", data={
        "date": "2026-04-30",
        "sleep_hours": "7.5",
        "energy": "8",
        "steps": "9200",
        "notes": "Felt good",
    })
    assert r.status_code == 302
    conn = _open_conn(app_and_db[1])
    row = conn.execute(
        "SELECT * FROM daily_metrics WHERE date = '2026-04-30'"
    ).fetchone()
    assert row["sleep_hours"] == 7.5
    assert row["energy"] == 8
    assert row["steps"] == 9200
    assert row["notes"] == "Felt good"


def test_daily_upserts_on_same_date(client, app_and_db) -> None:
    client.post("/metrics/daily", data={"date": "2026-04-30", "sleep_hours": "7"})
    client.post("/metrics/daily", data={"date": "2026-04-30", "sleep_hours": "7.5", "energy": "8"})
    conn = _open_conn(app_and_db[1])
    rows = conn.execute(
        "SELECT * FROM daily_metrics WHERE date = '2026-04-30'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["sleep_hours"] == 7.5
    assert rows[0]["energy"] == 8


def test_daily_skipped_if_all_blank(client, app_and_db) -> None:
    client.post("/metrics/daily", data={"date": "2026-04-30"})
    conn = _open_conn(app_and_db[1])
    rows = conn.execute(
        "SELECT * FROM daily_metrics WHERE date = '2026-04-30'"
    ).fetchall()
    assert rows == []


def test_metrics_page_shows_recent_logs(client) -> None:
    client.post("/metrics/weigh-in", data={"date": "2026-04-30", "weight_lb": "186.4"})
    client.post("/metrics/daily", data={"date": "2026-04-30", "sleep_hours": "7.5"})
    r = client.get("/metrics")
    body = r.get_data(as_text=True)
    assert "186.4 lb" in body
    assert "7.5 h" in body


def test_home_links_to_metrics(client) -> None:
    r = client.get("/")
    body = r.get_data(as_text=True)
    assert "/metrics" in body


def test_home_shows_latest_weigh_in(client, app_and_db) -> None:
    client.post("/metrics/weigh-in", data={"date": "2026-04-30", "weight_lb": "186.4", "waist_in": "36.5"})
    r = client.get("/")
    body = r.get_data(as_text=True)
    # The weight + unit are split across spans for the new metric-line layout.
    assert "186.4" in body
    assert "lb" in body
    assert "36.5" in body
    assert "in waist" in body
