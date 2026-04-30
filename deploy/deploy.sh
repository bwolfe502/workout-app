#!/usr/bin/env bash
# Run on the droplet: idempotent install / update of the workout-app service.
#
#   ssh root@104.236.8.9 'bash /opt/workout-app/deploy/deploy.sh'
#
# First-time install: clone the repo to /opt/workout-app, then `bash
# deploy/deploy.sh first-install` (which also creates the user and asks
# certbot for a cert).
#
# Updates: from /opt/workout-app, run `git pull && bash deploy/deploy.sh`.

set -euo pipefail

APP_DIR="/opt/workout-app"
SERVICE_NAME="workout-app"
USER_NAME="workout"
NGINX_VHOST="lift.1490.sh"

log() { printf '[deploy] %s\n' "$*"; }

cd "$APP_DIR"

if [[ "${1:-}" == "first-install" ]]; then
    log "ensuring system prereqs (sqlite3 CLI, python3-venv)"
    apt-get install -y -qq sqlite3 python3-venv >/dev/null

    log "creating user $USER_NAME (if not present)"
    id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$USER_NAME"

    log "ensuring data/ + exports/ exist"
    mkdir -p "$APP_DIR/data" "$APP_DIR/exports"

    if [[ ! -f "$APP_DIR/.env" ]]; then
        TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 28)"
        cat > "$APP_DIR/.env" <<EOF
WORKOUT_TOKEN=$TOKEN
EOF
        chmod 600 "$APP_DIR/.env"
        log "generated .env with WORKOUT_TOKEN — first login URL:"
        log "    https://$NGINX_VHOST/?token=$TOKEN"
    fi
fi

log "venv + deps"
if [[ ! -d "$APP_DIR/venv" ]]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

log "chown $APP_DIR → $USER_NAME"
chown -R "$USER_NAME:$USER_NAME" "$APP_DIR"

log "seed (only if mesocycle 1 missing — aborts otherwise)"
if [[ -d "$APP_DIR/seed-source" ]]; then
    # cd is required: `python -m seed` resolves modules from cwd, not from
    # the venv's site-packages.
    (cd "$APP_DIR" && sudo -u "$USER_NAME" "$APP_DIR/venv/bin/python" -m seed \
        --source-dir "$APP_DIR/seed-source" \
        --db "$APP_DIR/data/gym.db") || \
        log "  seed skipped (already present or seed-source missing)"
else
    log "  no seed-source/ directory — skipping (manual seed if needed)"
fi

log "installing systemd unit"
install -m 0644 "$APP_DIR/deploy/workout-app.service" \
    "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

if [[ "${1:-}" == "first-install" ]]; then
    log "installing nginx vhost (pre-TLS)"
    install -m 0644 "$APP_DIR/deploy/lift.1490.sh.nginx" \
        "/etc/nginx/sites-available/$NGINX_VHOST"
    ln -sf "/etc/nginx/sites-available/$NGINX_VHOST" \
           "/etc/nginx/sites-enabled/$NGINX_VHOST"
    nginx -t
    systemctl reload nginx
    log "next: certbot --nginx -d $NGINX_VHOST"
fi

log "installing daily backup cron"
install -m 0755 "$APP_DIR/deploy/backup.sh" /usr/local/bin/workout-app-backup.sh
cat > /etc/cron.d/workout-app-backup <<'CRON'
# Daily SQLite backup at 03:00 server time
0 3 * * * workout /usr/local/bin/workout-app-backup.sh >> /var/log/workout-app-backup.log 2>&1
CRON

log "(re)starting service"
systemctl restart "$SERVICE_NAME"
sleep 1
systemctl --no-pager --lines=10 status "$SERVICE_NAME" || true

log "done"
