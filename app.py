"""Flask entry point. Phase 1 logger."""

from __future__ import annotations

import json

from flask import Flask, abort, render_template

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
        # Workout C template
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

    @app.get("/session/<int:session_id>")
    def session_detail(session_id: int):
        conn = db.get_conn()
        sess = queries.session_by_id(conn, session_id)
        if sess is None:
            abort(404)
        prescribed = queries.prescribed_for_session(conn, session_id)
        sets_by_prescribed = queries.sets_for_session(conn, session_id)
        return render_template(
            "session_detail.html",
            sess=sess,
            prescribed=prescribed,
            sets_by_prescribed=sets_by_prescribed,
        )

    @app.get("/healthz")
    def healthz():
        return {"ok": True}, 200

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
