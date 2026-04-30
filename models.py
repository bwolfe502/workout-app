"""Dataclasses mirroring the SQLite schema in db.py.

These are pure data containers. They don't know about persistence — code that
reads from sqlite3.Row builds them with `from_row`, and code that writes maps
them back with `as_dict`. The set dataclass is named `WorkoutSet` to avoid
shadowing the built-in `set`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

# --- valid value sets, mirrored from CHECK constraints in db.SCHEMA_SQL ---

EXERCISE_NOTATIONS: frozenset[str] = frozenset({"per_hand", "total", "bw"})
SESSION_STATUSES: frozenset[str] = frozenset(
    {"planned", "in_progress", "completed", "partial", "extra", "skipped"}
)
SET_STATUSES: frozenset[str] = frozenset({"completed", "skipped", "deferred"})
AI_INTERACTION_STATUSES: frozenset[str] = frozenset(
    {"pending", "applied", "rolled_back", "failed"}
)


def _from_row(cls: type, row: Mapping[str, Any] | None):
    if row is None:
        return None
    field_names = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: row[k] for k in row.keys() if k in field_names})


@dataclass
class Mesocycle:
    id: int | None = None
    name: str = ""
    start_date: str = ""  # ISO date 'YYYY-MM-DD'
    end_date: str | None = None
    status: str = "active"
    philosophy_md: str | None = None
    notes_md: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Mesocycle | None":
        return _from_row(cls, row)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkoutTemplate:
    id: int | None = None
    letter: str = ""  # 'A', 'B', 'C'
    name: str = ""
    prescription_json: str = "[]"  # JSON-encoded list of prescription dicts

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "WorkoutTemplate | None":
        return _from_row(cls, row)


@dataclass
class Session:
    id: int | None = None
    mesocycle_id: int = 0
    day_number: int | None = None
    planned_date: str | None = None
    completed_at: str | None = None
    workout_letter: str | None = None
    status: str = "planned"
    narrative_md: str | None = None
    hevy_url: str | None = None

    def __post_init__(self) -> None:
        if self.status not in SESSION_STATUSES:
            raise ValueError(f"invalid session status: {self.status!r}")

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Session | None":
        return _from_row(cls, row)


@dataclass
class Exercise:
    id: int | None = None
    name: str = ""
    category: str | None = None
    primary_muscles: str | None = None  # csv: 'chest,front_delt,triceps'
    notation: str = "total"
    is_bodyweight: bool = False
    default_tempo: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.notation not in EXERCISE_NOTATIONS:
            raise ValueError(f"invalid exercise notation: {self.notation!r}")

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Exercise | None":
        if row is None:
            return None
        return cls(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            primary_muscles=row["primary_muscles"],
            notation=row["notation"],
            is_bodyweight=bool(row["is_bodyweight"]),
            default_tempo=row["default_tempo"],
            notes=row["notes"],
        )


@dataclass
class Prescribed:
    id: int | None = None
    session_id: int = 0
    position: int = 0
    exercise_id: int = 0
    sets_planned: int = 0
    rep_low: int | None = None
    rep_high: int | None = None
    weight_lb: float | None = None
    rir_target: int | None = None
    tempo: str | None = None
    notes: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Prescribed | None":
        return _from_row(cls, row)


@dataclass
class WorkoutSet:
    """One logged set. Named `WorkoutSet` to avoid shadowing built-in `set`."""

    id: int | None = None
    prescribed_id: int = 0
    set_number: int = 0
    reps_actual: int | None = None
    weight_actual: float | None = None
    rir_actual: int | None = None
    status: str = "completed"
    notes: str | None = None
    logged_at: str | None = None

    def __post_init__(self) -> None:
        if self.status not in SET_STATUSES:
            raise ValueError(f"invalid set status: {self.status!r}")

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "WorkoutSet | None":
        return _from_row(cls, row)


@dataclass
class Revision:
    id: int | None = None
    mesocycle_id: int | None = None
    date: str = ""  # ISO date
    change: str = ""
    reason: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Revision | None":
        return _from_row(cls, row)


@dataclass
class Issue:
    id: int | None = None
    opened_at: str = ""
    closed_at: str | None = None
    item: str = ""
    status: str = ""  # free-form: yellow, red, monitoring, resolved, permanent
    action: str | None = None
    severity: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "Issue | None":
        return _from_row(cls, row)


@dataclass
class WeighIn:
    id: int | None = None
    date: str = ""
    weight_lb: float = 0.0
    waist_in: float | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "WeighIn | None":
        return _from_row(cls, row)


@dataclass
class DailyMetric:
    id: int | None = None
    date: str = ""
    sleep_hours: float | None = None
    energy: int | None = None  # 1-10
    steps: int | None = None
    notes: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "DailyMetric | None":
        return _from_row(cls, row)


@dataclass
class AIInteraction:
    id: int | None = None
    created_at: str = ""
    request_md: str = ""
    response_raw: str | None = None
    parsed_json: str | None = None
    applied_diff: str | None = None
    status: str = "pending"

    def __post_init__(self) -> None:
        if self.status not in AI_INTERACTION_STATUSES:
            raise ValueError(f"invalid ai_interaction status: {self.status!r}")

    @classmethod
    def from_row(cls, row: Mapping[str, Any] | None) -> "AIInteraction | None":
        return _from_row(cls, row)


# --- helpers ---


def parse_iso_date(s: str) -> date:
    """Parse 'YYYY-MM-DD' → date. Tiny wrapper for callers that want a date object."""
    return date.fromisoformat(s)


def parse_iso_datetime(s: str) -> datetime:
    return datetime.fromisoformat(s)
