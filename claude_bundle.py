"""Build the outbound 'paste-into-Claude' bundle.

Bundle = three blocks concatenated:
  1. Instruction (what Claude must do + the strict JSON-response schema)
  2. Current program state (markdown views from markdown_views.py)
  3. Trigger (free-text describing what just happened, e.g. "Session 5 done")

The instruction block constrains Claude to a single fenced ```json block
matching `RESPONSE_SCHEMA`, optionally followed by free-text narrative.
The matching parser/validator lives in `claude_apply.py`.
"""

from __future__ import annotations

import json
import sqlite3

import markdown_views


# The strict JSON schema Claude's response must satisfy. Stored as Python
# dict so jsonschema.validate can use it directly; serialized into the
# instruction block via json.dumps(indent=2) for readability.
RESPONSE_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "revisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["date", "change", "reason"],
                "properties": {
                    "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                    "change": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        },
        "issue_opens": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "status"],
                "properties": {
                    "item": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "minLength": 1},
                    "action": {"type": ["string", "null"]},
                    "severity": {"type": ["string", "null"]},
                },
            },
        },
        "issue_closes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
                    "reason": {"type": ["string", "null"]},
                },
            },
        },
        "prescription_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["session_day", "exercise_name"],
                "properties": {
                    "session_day": {"type": "integer", "minimum": 1},
                    "exercise_name": {"type": "string", "minLength": 1},
                    "new_exercise_name": {"type": ["string", "null"]},
                    "sets_planned": {"type": ["integer", "null"], "minimum": 0},
                    "rep_low": {"type": ["integer", "null"], "minimum": 0},
                    "rep_high": {"type": ["integer", "null"], "minimum": 0},
                    "weight_lb": {"type": ["number", "null"], "minimum": 0},
                    "rir_target": {"type": ["integer", "null"], "minimum": 0},
                    "notes": {"type": ["string", "null"]},
                },
            },
        },
        "narrative": {"type": ["string", "null"]},
    },
}


INSTRUCTION_BLOCK = """\
# Workout review request

You are reviewing a hypertrophy program tracked by a self-hosted webapp. The
webapp is the source of truth for prescriptions, actuals, issues, body
metrics, and the revisions log; the markdown sections below were rendered
live from its SQLite database.

Your job: based on the trigger described at the bottom, propose updates to
the program state. The user will paste your response into the webapp's
"Apply" form, where it will be validated against the schema below, rendered
as a diff, and (if approved) applied in a single transaction.

## Response format — strict

Respond with **exactly one fenced ```json block**, followed by an optional
free-text narrative. The JSON must validate against this JSON Schema:

```json
{schema}
```

Field semantics:

- `revisions[]` — append rows to the revisions log. Use ISO date.
- `issue_opens[]` — open new issues. `status` is free text but conventional
  values are: yellow, red, monitoring, confirmed, permanent.
- `issue_closes[]` — close existing issues by id (see "Active Issues" table
  below for ids). `reason` is optional.
- `prescription_updates[]` — change the prescription for a future session.
  Identify the row by `(session_day, exercise_name)` (current name in the
  DB, before any swap). To swap exercises, set `new_exercise_name`. Any
  field left null/missing is unchanged.
- `narrative` — free text back to the user. Optional.

If you have nothing to change, return `{{}}` plus narrative.

Do not include any other prose before the JSON block. The parser extracts
the first fenced ```json block and rejects unknown fields.
"""


def build_bundle(
    conn: sqlite3.Connection,
    mesocycle_id: int,
    trigger: str = "",
) -> str:
    """Concatenate instructions + state + trigger into one paste-ready block."""
    schema_str = json.dumps(RESPONSE_SCHEMA, indent=2)
    parts: list[str] = [
        INSTRUCTION_BLOCK.format(schema=schema_str),
        "---",
        "# Program state",
        "",
        markdown_views.mesocycle_view(conn, mesocycle_id),
        "",
        markdown_views.workoutlog_view(conn, mesocycle_id),
        "",
        markdown_views.issues_view(conn),
        "",
        markdown_views.volume_view(conn, mesocycle_id),
        "",
        markdown_views.metrics_view(conn),
        "",
        markdown_views.revisions_view(conn, mesocycle_id),
        "",
        "---",
        "# Trigger",
        "",
        trigger.strip() or "_(no trigger description provided)_",
    ]
    return "\n".join(parts)


def default_trigger(conn: sqlite3.Connection, mesocycle_id: int) -> str:
    """Sensible default trigger string based on the latest completed session."""
    row = conn.execute(
        """
        SELECT day_number, workout_letter, status, completed_at
          FROM sessions
         WHERE mesocycle_id = ?
           AND day_number IS NOT NULL
           AND status IN ('completed', 'partial')
         ORDER BY completed_at DESC, day_number DESC
         LIMIT 1
        """,
        (mesocycle_id,),
    ).fetchone()
    if row is None:
        return "Mid-cycle review — please look at recent state and flag anything."
    return (
        f"Session {row['day_number']} ({row['status']}, Workout "
        f"{row['workout_letter']}) just finished. Please review the actuals, "
        "flag anything that should change going forward, and propose updates "
        "to upcoming session prescriptions or the revisions/issues log."
    )
