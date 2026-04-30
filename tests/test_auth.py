"""URL-token gate tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import app as app_module
import seed


FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "test-token-xyz"


@pytest.fixture
def gated_client(tmp_path: Path):
    db_path = tmp_path / "gym.db"
    seed.main([
        "--source-dir", str(FIXTURES),
        "--db", str(db_path),
        "--reset",
    ])
    flask_app = app_module.create_app({
        "DATABASE": str(db_path),
        "TESTING": True,
        "AUTH_TOKEN": TOKEN,
    })
    return flask_app.test_client()


@pytest.fixture
def open_client(tmp_path: Path):
    db_path = tmp_path / "gym.db"
    seed.main([
        "--source-dir", str(FIXTURES),
        "--db", str(db_path),
        "--reset",
    ])
    flask_app = app_module.create_app({
        "DATABASE": str(db_path),
        "TESTING": True,
        "AUTH_TOKEN": "",  # gate disabled
    })
    return flask_app.test_client()


def test_healthz_always_public(gated_client) -> None:
    r = gated_client.get("/healthz")
    assert r.status_code == 200


def test_no_cookie_redirects_to_login(gated_client) -> None:
    r = gated_client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_login_page_public(gated_client) -> None:
    r = gated_client.get("/login")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Password" in body
    # No token leaked
    assert TOKEN not in body


def test_login_form_preserves_next_url(gated_client) -> None:
    r = gated_client.get("/stats")
    assert r.status_code == 302
    assert "next=" in r.headers["Location"]
    assert "stats" in r.headers["Location"]


def test_wrong_password_returns_401_with_form(gated_client) -> None:
    r = gated_client.post("/login", data={"password": "wrong"})
    assert r.status_code == 401
    body = r.get_data(as_text=True)
    assert "Wrong password" in body
    # Form is still there
    assert "Sign in" in body


def test_correct_password_sets_cookie_and_redirects(gated_client) -> None:
    r = gated_client.post("/login", data={"password": TOKEN, "next": "/program"})
    assert r.status_code == 302
    assert r.headers["Location"] == "/program"
    cookie = r.headers.get("Set-Cookie", "")
    assert "workout_auth=" in cookie
    assert TOKEN in cookie


def test_login_rejects_open_redirect(gated_client) -> None:
    r = gated_client.post("/login", data={"password": TOKEN, "next": "//evil.com"})
    assert r.status_code == 302
    assert r.headers["Location"] == "/"


def test_wrong_token_in_url_redirects_to_login(gated_client) -> None:
    r = gated_client.get("/?token=nope")
    # Wrong token falls through the URL-token check to the login redirect.
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_correct_token_in_url_redirects_and_sets_cookie(gated_client) -> None:
    """The legacy ?token=… URL still works for bookmark recovery."""
    r = gated_client.get(f"/?token={TOKEN}")
    assert r.status_code == 302
    assert r.headers["Location"] == "/"
    cookie = r.headers.get("Set-Cookie", "")
    assert "workout_auth=" in cookie
    assert TOKEN in cookie
    assert "HttpOnly" in cookie


def test_token_querystring_preserves_other_args(gated_client) -> None:
    r = gated_client.get(f"/session/1?live=1&token={TOKEN}")
    assert r.status_code == 302
    assert "live=1" in r.headers["Location"]
    assert "token" not in r.headers["Location"]


def test_cookie_carries_subsequent_requests(gated_client) -> None:
    # First request sets the cookie
    gated_client.get(f"/?token={TOKEN}")
    # Subsequent request should be allowed without ?token
    r = gated_client.get("/program")
    assert r.status_code == 200


def test_open_client_has_no_gate(open_client) -> None:
    """When AUTH_TOKEN is empty, no gate runs."""
    r = open_client.get("/")
    assert r.status_code == 200
    r2 = open_client.get("/program")
    assert r2.status_code == 200


def test_post_endpoints_also_gated(gated_client) -> None:
    """The gate must protect POST routes too — no cookie, no write."""
    r = gated_client.post("/issues", data={"item": "Sneaky write"})
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_proxy_fix_trusts_forwarded_proto(gated_client) -> None:
    """ProxyFix should make request.is_secure True when X-Forwarded-Proto=https,
    so the auth cookie gets marked Secure in production."""
    r = gated_client.get(
        f"/?token={TOKEN}",
        headers={"X-Forwarded-Proto": "https", "Host": "lift.1490.sh"},
    )
    cookie = r.headers.get("Set-Cookie", "")
    assert "Secure" in cookie
