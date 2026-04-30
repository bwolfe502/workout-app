"""Flask entry point. Phase 1 logger."""

from __future__ import annotations

from flask import Flask

import db


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    if config:
        app.config.update(config)
    db.init_app(app)

    @app.get("/")
    def home():
        return "workout-app — bootstrapped. Phase 1 routes coming.", 200

    @app.get("/healthz")
    def healthz():
        return {"ok": True}, 200

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
