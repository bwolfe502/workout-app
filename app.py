"""Flask entry point."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from urllib.parse import urlencode

from flask import Flask, abort, make_response, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import claude_apply
import claude_bundle
import db
import queries
import volume

# Paths exempt from the URL-token gate.
#   /healthz    — systemd watchdog / uptime probes
#   /auth/check — the gate's own check endpoint, used by nginx auth_request
#                 to delegate auth for sibling subdomains (ops.1490.sh).
_PUBLIC_PATHS: frozenset[str] = frozenset({"/healthz", "/auth/check"})
_AUTH_COOKIE = "workout_auth"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # one year


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    # AUTH_TOKEN — when set, every request needs ?token=… (one-shot, then a
    # cookie carries it). Empty / unset → no gate, useful in local dev.
    app.config.setdefault("AUTH_TOKEN", os.environ.get("WORKOUT_TOKEN", ""))
    # AUTH_COOKIE_DOMAIN — set to a parent domain (e.g. ".1490.sh") to share
    # the auth cookie across sibling subdomains. Unset → host-only cookie.
    app.config.setdefault(
        "AUTH_COOKIE_DOMAIN",
        os.environ.get("WORKOUT_COOKIE_DOMAIN", "") or None,
    )
    if config:
        app.config.update(config)

    # Behind nginx in prod, trust X-Forwarded-Proto so request.is_secure works
    # and the auth cookie can be marked Secure.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    _register_token_gate(app)
    _register_error_handlers(app)

    # Expose template helpers without forcing every route to pass them.
    app.jinja_env.globals.update(
        format_weight=queries.format_weight,
        format_reps=queries.format_reps,
        format_session_label=queries.format_session_label,
    )

    # ---- read pages -------------------------------------------------------

    @app.get("/")
    def home():
        conn = db.get_conn()
        meso = queries.active_mesocycle(conn)
        next_sess = queries.next_session(conn, meso["id"]) if meso else None
        next_prescribed = (
            queries.prescribed_for_session(conn, next_sess["id"]) if next_sess else []
        )
        partials = queries.partial_sessions(conn, meso["id"]) if meso else []
        return render_template(
            "home.html",
            mesocycle=meso,
            next_sess=next_sess,
            next_prescribed=next_prescribed,
            partials=partials,
            today=date.today().isoformat(),
            issues=queries.open_issues(conn),
            weigh_in=queries.last_weigh_in(conn),
        )

    @app.get("/program")
    def program():
        conn = db.get_conn()
        meso = queries.active_mesocycle(conn)
        if meso is None:
            return render_template("program.html", mesocycle=None,
                                   sessions=[], prescribed_by_session={},
                                   revisions=[], workout_c=[])
        sessions = queries.numbered_sessions(conn, meso["id"])
        prescribed_by_session: dict[int, list] = {}
        for s in sessions:
            prescribed_by_session[s["id"]] = queries.prescribed_for_session(conn, s["id"])
        wc_row = conn.execute(
            "SELECT prescription_json FROM workout_templates WHERE letter = 'C'"
        ).fetchone()
        workout_c = json.loads(wc_row["prescription_json"]) if wc_row else []
        return render_template(
            "program.html",
            mesocycle=meso,
            sessions=sessions,
            prescribed_by_session=prescribed_by_session,
            revisions=queries.revisions(conn, meso["id"]),
            workout_c=workout_c,
        )

    @app.get("/sessions")
    def sessions():
        conn = db.get_conn()
        meso = queries.active_mesocycle(conn)
        rows = queries.all_sessions(conn, meso["id"]) if meso else []
        return render_template("sessions.html", sessions=rows)

    # ---- live + read-only session detail ---------------------------------

    @app.get("/session/<int:session_id>")
    def session_detail(session_id: int):
        conn = db.get_conn()
        sess = queries.session_by_id(conn, session_id)
        if sess is None:
            abort(404)
        prescribed = queries.prescribed_for_session(conn, session_id)
        sets_by_prescribed = queries.sets_for_session(conn, session_id)
        # Routing: planned / in_progress → live view by default. Partial
        # sessions stay read-only unless the user opts in with ?live=1
        # ("Continue logging" button on the detail view), so accidental
        # taps on a finished day don't create surprise set rows.
        live_opt_in = request.args.get("live") == "1"
        is_live = sess["status"] in ("planned", "in_progress") or (
            live_opt_in and sess["status"] in ("partial", "extra")
        )
        template = "session_live.html" if is_live else "session_detail.html"
        last_time = (
            queries.previous_sets_by_exercise(conn, session_id) if is_live else {}
        )
        exercises_for_swap = queries.all_exercises(conn) if is_live else []
        unaddressed_count = sum(
            1 for p in prescribed if not sets_by_prescribed.get(p["prescribed_id"])
        )
        return render_template(
            template,
            sess=sess,
            prescribed=prescribed,
            sets_by_prescribed=sets_by_prescribed,
            last_time=last_time,
            exercises_for_swap=exercises_for_swap,
            unaddressed_count=unaddressed_count,
        )

    # ---- htmx mutations on the live view ---------------------------------

    @app.post("/session/<int:session_id>/exercise/<int:prescribed_id>/set")
    def log_set(session_id: int, prescribed_id: int):
        return _swap_after_mutation(
            session_id, prescribed_id, _do_log_set, just_logged=True
        )

    @app.post("/session/<int:session_id>/exercise/<int:prescribed_id>/skip")
    def skip_exercise(session_id: int, prescribed_id: int):
        return _swap_after_mutation(
            session_id, prescribed_id, lambda c, p: _do_marker(c, p, "skipped")
        )

    @app.post("/session/<int:session_id>/exercise/<int:prescribed_id>/defer")
    def defer_exercise(session_id: int, prescribed_id: int):
        return _swap_after_mutation(
            session_id, prescribed_id, lambda c, p: _do_marker(c, p, "deferred")
        )

    @app.post("/session/<int:session_id>/exercise/<int:prescribed_id>/swap")
    def swap_exercise(session_id: int, prescribed_id: int):
        """Repoint a prescribed row to a different exercise. Mid-session
        ad-hoc substitution (equipment taken, joints feeling off, etc.).
        Keeps sets_planned / rep range / weight / RIR target unchanged so
        the user's plan is preserved; they can still override on the next
        set log. Does NOT create a revisions entry — those are reserved
        for explicit program changes through the Claude review loop."""
        new_exercise_id = _to_int(request.form.get("exercise_id"))
        if new_exercise_id is None:
            abort(400)
        return _swap_after_mutation(
            session_id, prescribed_id,
            lambda c, p: c.execute(
                "UPDATE prescribed SET exercise_id = ? WHERE id = ?",
                (new_exercise_id, p),
            ),
        )

    @app.post("/session/<int:session_id>/exercise/<int:prescribed_id>/flag")
    def flag_exercise(session_id: int, prescribed_id: int):
        """Quick-create an issue from inside the live view. Returns a tiny
        confirmation partial that htmx swaps in over the inline form, so
        the user stays on the session page instead of being kicked out to
        /issues mid-workout."""
        conn = db.get_conn()
        prescribed = _prescribed_by_id(conn, prescribed_id)
        if prescribed is None or prescribed["session_id"] != session_id:
            abort(404)
        ex_row = conn.execute(
            "SELECT name FROM exercises WHERE id = ?",
            (prescribed["exercise_id"],),
        ).fetchone()
        ex_name = ex_row["name"] if ex_row else "exercise"
        item_text = (request.form.get("item") or "").strip()
        if not item_text:
            return ('<p class="flag-confirm muted small">'
                    'Type something first, then tap Flag.</p>'), 200
        full_item = f"{ex_name} — {item_text}"
        status = (request.form.get("status") or "yellow").strip()
        conn.execute(
            "INSERT INTO issues (opened_at, item, status) VALUES (?, ?, ?)",
            (date.today().isoformat(), full_item, status),
        )
        conn.commit()
        return (
            '<p class="flag-confirm">'
            'Issue logged. <a href="/issues">View notes →</a></p>'
        ), 200

    @app.post("/session/<int:session_id>/finish")
    def finish_session(session_id: int):
        conn = db.get_conn()
        sess = queries.session_by_id(conn, session_id)
        if sess is None:
            abort(404)
        narrative = request.form.get("narrative", "").strip()
        # Status: completed if every prescribed exercise has a non-skipped/
        # deferred marker or at least one completed set; partial otherwise.
        prescribed = queries.prescribed_for_session(conn, session_id)
        sets_by_prescribed = queries.sets_for_session(conn, session_id)
        status = _classify_completion(prescribed, sets_by_prescribed)
        conn.execute(
            """
            UPDATE sessions
               SET completed_at = ?, status = ?, narrative_md = ?
             WHERE id = ?
            """,
            (datetime.now().isoformat(timespec="seconds"), status,
             narrative or None, session_id),
        )
        conn.commit()
        return redirect(url_for("home"))

    # ---- claude loop -----------------------------------------------------

    @app.get("/claude")
    def claude_review():
        conn = db.get_conn()
        meso = queries.active_mesocycle(conn)
        trigger = request.args.get("trigger") or (
            claude_bundle.default_trigger(conn, meso["id"]) if meso else ""
        )
        bundle = claude_bundle.build_bundle(conn, meso["id"], trigger) if meso else ""
        return render_template(
            "claude.html",
            bundle=bundle,
            trigger=trigger,
            mesocycle=meso,
        )

    @app.get("/claude/apply")
    def claude_apply_get():
        return render_template("claude_apply.html",
                               raw="", diff=None, error=None, applied_id=None)

    @app.post("/claude/apply")
    def claude_apply_post():
        conn = db.get_conn()
        meso = queries.active_mesocycle(conn)
        if meso is None:
            return render_template("claude_apply.html", raw="",
                                   diff=None, error="No active mesocycle.",
                                   applied_id=None)
        raw = request.form.get("raw", "")
        action = request.form.get("action") or "preview"
        try:
            json_text = claude_apply.extract_json_block(raw)
            response = claude_apply.parse_and_validate(json_text)
            diff = claude_apply.build_diff(conn, response, meso["id"])
        except claude_apply.ApplyError as e:
            return render_template("claude_apply.html", raw=raw,
                                   diff=None, error=str(e), applied_id=None)

        if action == "apply" and not diff.has_errors and not diff.is_empty:
            try:
                applied_id = claude_apply.apply(
                    conn, response, meso["id"],
                    request_md="(generated bundle, not stored verbatim)",
                    response_raw=raw,
                )
            except claude_apply.ApplyError as e:
                return render_template("claude_apply.html", raw=raw,
                                       diff=diff, error=str(e), applied_id=None)
            return render_template("claude_apply.html", raw="",
                                   diff=None, error=None, applied_id=applied_id,
                                   applied_diff=diff)

        # Preview only
        return render_template("claude_apply.html", raw=raw,
                               diff=diff, error=None, applied_id=None)

    @app.get("/claude/log")
    def claude_log():
        conn = db.get_conn()
        rows = queries.ai_interactions(conn)
        # Pre-decode JSON for the template (Jinja's tojson roundtrip is ugly).
        decoded = []
        for r in rows:
            decoded.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "status": r["status"],
                "parsed_json": json.loads(r["parsed_json"]) if r["parsed_json"] else {},
                "snapshot": json.loads(r["applied_diff"]) if r["applied_diff"] else {},
            })
        return render_template("claude_log.html", interactions=decoded,
                               error=request.args.get("error"))

    @app.post("/claude/log/<int:ai_id>/rollback")
    def claude_rollback(ai_id: int):
        conn = db.get_conn()
        try:
            claude_apply.rollback(conn, ai_id)
        except claude_apply.ApplyError as e:
            return redirect(url_for("claude_log", error=str(e)))
        return redirect(url_for("claude_log"))

    # ---- stats -----------------------------------------------------------

    @app.get("/stats")
    def stats():
        conn = db.get_conn()
        meso = queries.active_mesocycle(conn)
        if meso is None:
            return render_template("stats.html", mesocycle=None,
                                   volume={}, strength=[], strength_exercise=None,
                                   trained_exercises=[], bodyweight=[],
                                   pain_by_week={}, volume_targets_low={},
                                   volume_targets_high={})
        meso_id = meso["id"]
        trained = volume.trained_exercises(conn, meso_id)
        # Pick exercise for strength chart from query string, else first lift.
        ex_id = request.args.get("exercise_id", type=int)
        if not ex_id and trained:
            ex_id = trained[0]["id"]
        strength = volume.strength_trend(conn, ex_id, meso_id) if ex_id else []
        chosen_ex = next((e for e in trained if e["id"] == ex_id), None)

        return render_template(
            "stats.html",
            mesocycle=meso,
            volume=volume.volume_by_muscle_week(conn, meso_id),
            strength=strength,
            strength_exercise=chosen_ex,
            trained_exercises=trained,
            bodyweight=volume.bodyweight_trend(conn),
            pain_by_week=volume.pain_by_week(conn, meso_id),
            volume_targets_low=volume.VOLUME_TARGETS_LOW,
            volume_targets_high=volume.VOLUME_TARGETS_HIGH,
        )

    # ---- metrics ---------------------------------------------------------

    @app.get("/metrics")
    def metrics():
        conn = db.get_conn()
        return render_template(
            "metrics.html",
            today=date.today().isoformat(),
            weigh_ins=queries.recent_weigh_ins(conn),
            dailies=queries.recent_daily_metrics(conn),
            last_weigh_in=queries.last_weigh_in(conn),
        )

    @app.post("/metrics/weigh-in")
    def metrics_weigh_in():
        d = (request.form.get("date") or date.today().isoformat()).strip()
        weight = _to_float(request.form.get("weight_lb"))
        waist = _to_float(request.form.get("waist_in"))
        if weight is None:
            return redirect(url_for("metrics"))
        conn = db.get_conn()
        conn.execute(
            """
            INSERT INTO weigh_ins (date, weight_lb, waist_in)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                weight_lb = excluded.weight_lb,
                waist_in  = excluded.waist_in
            """,
            (d, weight, waist),
        )
        conn.commit()
        return redirect(url_for("metrics"))

    @app.post("/metrics/daily")
    def metrics_daily():
        d = (request.form.get("date") or date.today().isoformat()).strip()
        sleep = _to_float(request.form.get("sleep_hours"))
        energy = _to_int(request.form.get("energy"))
        steps = _to_int(request.form.get("steps"))
        notes = (request.form.get("notes") or "").strip() or None
        if sleep is None and energy is None and steps is None and not notes:
            return redirect(url_for("metrics"))
        conn = db.get_conn()
        conn.execute(
            """
            INSERT INTO daily_metrics (date, sleep_hours, energy, steps, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                sleep_hours = excluded.sleep_hours,
                energy      = excluded.energy,
                steps       = excluded.steps,
                notes       = excluded.notes
            """,
            (d, sleep, energy, steps, notes),
        )
        conn.commit()
        return redirect(url_for("metrics"))

    # ---- issues ----------------------------------------------------------

    @app.get("/issues")
    def issues_list():
        conn = db.get_conn()
        # Don't .strip() — deep-links from the live view often end with
        # ': ' on purpose so the user can start typing right after the colon.
        prefill_item = request.args.get("item") or ""
        return render_template(
            "issues.html",
            open_issues=queries.open_issues(conn),
            closed_issues=queries.closed_issues(conn),
            prefill_item=prefill_item,
        )

    @app.post("/issues")
    def issues_create():
        item = (request.form.get("item") or "").strip()
        if not item:
            return redirect(url_for("issues_list"))
        status = (request.form.get("status") or "yellow").strip()
        action = (request.form.get("action") or "").strip() or None
        severity = (request.form.get("severity") or "").strip() or None
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO issues (opened_at, item, status, action, severity) VALUES (?, ?, ?, ?, ?)",
            (date.today().isoformat(), item, status, action, severity),
        )
        conn.commit()
        return redirect(url_for("issues_list"))

    @app.post("/issues/<int:issue_id>/close")
    def issues_close(issue_id: int):
        conn = db.get_conn()
        issue = queries.issue_by_id(conn, issue_id)
        if issue is None:
            abort(404)
        conn.execute(
            "UPDATE issues SET closed_at = ? WHERE id = ?",
            (date.today().isoformat(), issue_id),
        )
        conn.commit()
        return redirect(url_for("issues_list"))

    @app.post("/issues/<int:issue_id>/reopen")
    def issues_reopen(issue_id: int):
        conn = db.get_conn()
        issue = queries.issue_by_id(conn, issue_id)
        if issue is None:
            abort(404)
        conn.execute(
            "UPDATE issues SET closed_at = NULL WHERE id = ?",
            (issue_id,),
        )
        conn.commit()
        return redirect(url_for("issues_list"))

    @app.get("/healthz")
    def healthz():
        return {"ok": True}, 200

    return app


# ---- URL-token gate -------------------------------------------------------


def _register_token_gate(app: Flask) -> None:
    """Single shared-secret gate. Three ways in, all use the same WORKOUT_TOKEN:

      1. Password form at /login (primary UX)
      2. ?token=<secret> in URL (operator convenience, bookmark recovery)
      3. Existing workout_auth cookie (sticky for a year after either of above)

    /healthz and /login are public; if AUTH_TOKEN is empty (local dev), no
    gate runs at all.
    """

    @app.before_request
    def _check_token():
        expected = app.config.get("AUTH_TOKEN") or ""
        if not expected:
            return None  # gate disabled
        if request.path in _PUBLIC_PATHS or request.path == "/login":
            return None
        # Static assets must be reachable without auth so the login page
        # can pull its own CSS / JS / fonts before the cookie is set.
        if request.path.startswith("/static/"):
            return None
        if request.cookies.get(_AUTH_COOKIE) == expected:
            return None
        if request.args.get("token") == expected:
            # Strip token from query string and bake a cookie.
            clean_args = {k: v for k, v in request.args.items() if k != "token"}
            target = request.path
            if clean_args:
                target = f"{request.path}?{urlencode(clean_args)}"
            return _set_auth_cookie_and_redirect(target, expected)
        # Otherwise, send to /login with a `next` param so we can return them
        # to where they were going.
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_url))

    @app.get("/login")
    def login():
        return render_template("login.html",
                               next_url=request.args.get("next") or "/",
                               error=None)

    @app.post("/login")
    def login_submit():
        expected = app.config.get("AUTH_TOKEN") or ""
        password = request.form.get("password", "")
        next_url = request.form.get("next") or "/"
        # Sanitize next_url — must be a relative path within this app, not
        # an open redirect to some other site.
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/"
        if password and password == expected:
            return _set_auth_cookie_and_redirect(next_url, expected)
        return render_template("login.html", next_url=next_url,
                               error="Wrong password."), 401

    @app.get("/auth/check")
    def auth_check():
        """nginx auth_request endpoint for sibling-subdomain protection.

        Returns 204 if the workout_auth cookie matches AUTH_TOKEN (or if
        the gate is disabled), 401 otherwise. No body — nginx auth_request
        only inspects the status code.
        """
        expected = app.config.get("AUTH_TOKEN") or ""
        if not expected:
            return "", 204
        if request.cookies.get(_AUTH_COOKIE) == expected:
            return "", 204
        return "", 401


def _set_auth_cookie_and_redirect(target: str, token: str):
    from flask import current_app
    resp = make_response(redirect(target))
    kwargs: dict = dict(
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        secure=request.is_secure,
        samesite="Lax",
    )
    domain = current_app.config.get("AUTH_COOKIE_DOMAIN")
    if domain:
        kwargs["domain"] = domain
    resp.set_cookie(_AUTH_COOKIE, token, **kwargs)
    return resp


# ---- error pages ----------------------------------------------------------


def _register_error_handlers(app: Flask) -> None:
    """Friendlier 401 page than Werkzeug's stock 'credentials' copy.

    Doesn't leak the token (anyone on the internet hits this); just tells the
    user what they need so they can paste it from their notes / dev's .env.
    """

    @app.errorhandler(401)
    def _unauthorized(_e):
        return render_template("401.html"), 401


# ---- helpers --------------------------------------------------------------


def _swap_after_mutation(session_id, prescribed_id, action, *, just_logged=False):
    conn = db.get_conn()
    sess = queries.session_by_id(conn, session_id)
    if sess is None:
        abort(404)
    prescribed = _prescribed_by_id(conn, prescribed_id)
    if prescribed is None or prescribed["session_id"] != session_id:
        abort(404)
    # Lazy transition: planned → in_progress on first action.
    if sess["status"] == "planned":
        conn.execute(
            "UPDATE sessions SET status = 'in_progress' WHERE id = ?",
            (session_id,),
        )
    action(conn, prescribed_id)
    conn.commit()
    # Re-fetch + re-render the exercise block partial. We re-pass last_time
    # and exercises_for_swap so the swapped-in block has the same context as
    # the original full-page render — otherwise the "Last time" line and the
    # swap dropdown disappear after every htmx interaction.
    refreshed = _prescribed_for_block(conn, prescribed_id)
    sets_by_prescribed = queries.sets_for_session(conn, session_id)
    sess = queries.session_by_id(conn, session_id)
    return render_template(
        "_exercise_block.html",
        p=refreshed,
        sess=sess,
        sets_by_prescribed=sets_by_prescribed,
        last_time=queries.previous_sets_by_exercise(conn, session_id),
        exercises_for_swap=queries.all_exercises(conn),
        just_logged=just_logged,
    )


def _do_log_set(conn, prescribed_id):
    weight = _to_float(request.form.get("weight"))
    reps = _to_int(request.form.get("reps"))
    rir = _to_int(request.form.get("rir"))
    notes = request.form.get("notes") or None
    next_n = (conn.execute(
        "SELECT count(*) FROM sets WHERE prescribed_id = ? AND status = 'completed'",
        (prescribed_id,),
    ).fetchone()[0] or 0) + 1
    conn.execute(
        """
        INSERT INTO sets
            (prescribed_id, set_number, reps_actual, weight_actual,
             rir_actual, status, notes, logged_at)
        VALUES (?, ?, ?, ?, ?, 'completed', ?, ?)
        """,
        (prescribed_id, next_n, reps, weight, rir, notes,
         datetime.now().isoformat(timespec="seconds")),
    )


def _do_marker(conn, prescribed_id, status):
    """Skip/defer the whole exercise. Wipes any partial sets & writes one marker row."""
    conn.execute("DELETE FROM sets WHERE prescribed_id = ?", (prescribed_id,))
    conn.execute(
        """
        INSERT INTO sets (prescribed_id, set_number, status, logged_at)
        VALUES (?, 1, ?, ?)
        """,
        (prescribed_id, status, datetime.now().isoformat(timespec="seconds")),
    )


def _prescribed_by_id(conn, prescribed_id):
    return conn.execute(
        "SELECT * FROM prescribed WHERE id = ?", (prescribed_id,)
    ).fetchone()


def _prescribed_for_block(conn, prescribed_id):
    """Same shape as queries.prescribed_for_session() rows, single id."""
    return conn.execute(
        """
        SELECT p.id          AS prescribed_id,
               p.position    AS position,
               p.sets_planned, p.rep_low, p.rep_high,
               p.weight_lb, p.rir_target, p.notes AS prescribed_notes,
               e.id          AS exercise_id,
               e.name        AS exercise_name,
               e.notation    AS notation,
               e.is_bodyweight AS is_bodyweight,
               e.default_tempo,
               e.primary_muscles
          FROM prescribed p
          JOIN exercises e ON e.id = p.exercise_id
         WHERE p.id = ?
        """,
        (prescribed_id,),
    ).fetchone()


def _classify_completion(prescribed_rows, sets_by_prescribed) -> str:
    """Return 'completed' if every exercise has at least one logged set
    (regardless of status), 'partial' if any exercise has zero rows."""
    if not prescribed_rows:
        return "partial"
    has_partial_gap = False
    for p in prescribed_rows:
        rows = sets_by_prescribed.get(p["prescribed_id"], [])
        if not rows:
            has_partial_gap = True
            break
        # If marker is skipped/deferred, that still counts as "addressed"
    return "partial" if has_partial_gap else "completed"


def _to_float(s):
    if s is None or s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s):
    if s is None or s == "":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
