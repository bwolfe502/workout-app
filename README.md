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

## Deploy (Phase 4 — `lift.1490.sh`)

First-time install on the droplet:

```bash
ssh root@104.236.8.9
git clone https://github.com/bwolfe502/workout-app.git /opt/workout-app
# Copy the three source md files into /opt/workout-app/seed-source/ first
# (or skip; deploy.sh seeds only if the dir is present).
bash /opt/workout-app/deploy/deploy.sh first-install
# follow the printed first-login URL (contains the generated token)
certbot --nginx -d lift.1490.sh --redirect
# Then replace the placeholder `location /` block in
# /etc/nginx/sites-available/lift.1490.sh with proxy_pass to 127.0.0.1:8092
# (see deploy/lift.1490.sh.nginx for the post-certbot template), then
# `nginx -t && systemctl reload nginx`.
```

Updates:

```bash
ssh root@104.236.8.9 'cd /opt/workout-app && git pull && bash deploy/deploy.sh'
```

The systemd unit (`deploy/workout-app.service`) runs gunicorn bound to
`127.0.0.1:8092` as user `workout`; nginx (`deploy/lift.1490.sh.nginx`)
proxies https → that port. The URL-token gate (`WORKOUT_TOKEN` env in
`/opt/workout-app/.env`) protects every path except `/healthz`.
`deploy/backup.sh` runs nightly via `/etc/cron.d/workout-app-backup`,
keeping 30 days of gzipped SQLite snapshots in `/opt/workout-app/backups`.

## Status

**Phases 1–4 complete.** Seed importer + mobile logger + metrics +
issues + stats charts + Claude paste-in/paste-out loop with diff
preview, audit log, and rollback. Deployed at
[lift.1490.sh](https://lift.1490.sh).
