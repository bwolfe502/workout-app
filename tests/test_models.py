"""Dataclass behavior tests for models.py."""

from __future__ import annotations

import pytest

from models import (
    Exercise,
    Mesocycle,
    Prescribed,
    Session,
    WorkoutSet,
    AIInteraction,
)


def test_session_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="invalid session status"):
        Session(mesocycle_id=1, status="bogus")


def test_session_accepts_valid_statuses() -> None:
    for s in ("planned", "in_progress", "completed", "partial", "extra", "skipped"):
        Session(mesocycle_id=1, status=s)


def test_exercise_rejects_invalid_notation() -> None:
    with pytest.raises(ValueError, match="invalid exercise notation"):
        Exercise(name="Foo", notation="kg")


def test_exercise_from_row_coerces_bool() -> None:
    row = {
        "id": 1,
        "name": "Pull-Up",
        "category": "compound_pull",
        "primary_muscles": "back",
        "notation": "bw",
        "is_bodyweight": 1,
        "default_tempo": None,
        "notes": None,
    }
    e = Exercise.from_row(row)
    assert e is not None
    assert e.is_bodyweight is True
    assert e.notation == "bw"


def test_workout_set_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="invalid set status"):
        WorkoutSet(prescribed_id=1, status="bogus")


def test_ai_interaction_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="invalid ai_interaction status"):
        AIInteraction(request_md="hi", status="bogus")


def test_mesocycle_as_dict_round_trips() -> None:
    m = Mesocycle(id=1, name="Mesocycle 1", start_date="2026-04-22")
    d = m.as_dict()
    assert d["name"] == "Mesocycle 1"
    assert d["start_date"] == "2026-04-22"


def test_from_row_returns_none_for_none() -> None:
    assert Session.from_row(None) is None
    assert Exercise.from_row(None) is None
    assert Prescribed.from_row(None) is None
