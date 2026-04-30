# workout-app

A self-hosted hypertrophy program tracker with a structured Claude review loop.

Replaces Hevy (logger) and the manual copy-paste-into-Claude flow with a single
webapp. SQLite is the source of truth; Claude reviews land via a strict
JSON-schema paste-in / paste-out flow with diff preview and audit log.

Deployed at `lift.1490.sh`.

## Stack

- Python 3 + Flask + SQLite
- Server-rendered Jinja2 templates, htmx for interactions, Chart.js for graphs
- No build step. Deployed via systemd + nginx + certbot.

## Layout

```
app.py              Flask app factory + routes
db.py               SQLite connection, schema migrations
models.py           Dataclasses
claude_bundle.py    Build outbound Claude bundle (markdown + schema)
claude_apply.py     Parse + validate + diff Claude response
markdown_views.py   Generate mesocycle / log views from DB
volume.py           Sets-per-muscle and strength rollups
seed.py             One-time import from md files
templates/          Jinja2 templates (mobile-first)
static/             CSS + JS
data/gym.db         SQLite file (gitignored)
exports/            Generated md/csv exports (gitignored)
tests/              pytest
```

## Run locally

```bash
python -m venv .venv
. .venv/Scripts/activate    # Windows: . .venv/Scripts/activate
pip install -r requirements.txt
python -m seed              # one-time, imports current md files
flask --app app run --debug
```

Open http://localhost:5000.

## Status

**Phase 1 (logger) complete.** Seed importer reads the three source md
files, mobile workout view logs sets via htmx with a rest timer, and
the read-only program/sessions/detail pages match the source. Phase 2
(metrics, issues, charts) is next.
