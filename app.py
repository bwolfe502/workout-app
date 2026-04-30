"""Flask entry point. Phase 1 scaffold - real routes coming next."""

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)

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
