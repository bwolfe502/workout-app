"""Flask entry point. Phase 1 logger."""

from __future__ import annotations

import json
from datetime import date, datetime

from flask import Flask, abort, redirect, render_template, request, url_for

import db
import queries


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    if config:
        app.config.update(config)
    db.init_app(app)

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
        return render_template(
            "home.html",
            mesocycle=meso,
            next_sess=next_sess,
            next_prescribed=next_prescribed,
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
        return render_template(
            template,
            sess=sess,
            prescribed=prescribed,
            sets_by_prescribed=sets_by_prescribed,
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

    # ---- issues ----------------------------------------------------------

    @app.get("/issues")
    def issues_list():
        conn = db.get_conn()
        return render_template(
            "issues.html",
            open_issues=queries.open_issues(conn),
            closed_issues=queries.closed_issues(conn),
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
    # Re-fetch + re-render the exercise block partial.
    refreshed = _prescribed_for_block(conn, prescribed_id)
    sets_by_prescribed = queries.sets_for_session(conn, session_id)
    sess = queries.session_by_id(conn, session_id)
    return render_template(
        "_exercise_block.html",
        p=refreshed,
        sess=sess,
        sets_by_prescribed=sets_by_prescribed,
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
