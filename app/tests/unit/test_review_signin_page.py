"""The page a reviewer signs in from.

The identity provider's client id is the audience the review credential is
verified against, so the page names it from that same configuration and
nowhere else. With review configured the page carries the client id, loads the
provider's sign-in script and allows exactly the provider origins that script
needs. With review not configured the page carries none of it and the policy
stays the shipped one.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from src.api import main

AUDIENCE = "1234567890-review.apps.googleusercontent.com"
PAGE = "<!doctype html><html><head><meta charset=\"UTF-8\" /></head><body></body></html>"
GSI_SCRIPT = '<script src="https://accounts.google.com/gsi/client" async></script>'


@pytest.fixture
def page(tmp_path, monkeypatch):
    index = tmp_path / "index.html"
    index.write_text(PAGE, encoding="utf-8")
    monkeypatch.setattr(main, "WEB_DIST_INDEX", index)
    return TestClient(main.app)


def configure(monkeypatch, audience=AUDIENCE):
    monkeypatch.setenv("REVIEW_OAUTH_AUDIENCE", audience)
    monkeypatch.setenv("REVIEW_OAUTH_ISSUER", "https://accounts.google.com")
    monkeypatch.setenv("REVIEW_HOSTED_DOMAIN", "ogilvy.co.za")
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", json.dumps({"1001": "claim_approver"}))


def unconfigure(monkeypatch):
    for name in (
        "REVIEW_OAUTH_AUDIENCE",
        "REVIEW_HOSTED_DOMAIN",
        "REVIEW_SUBJECT_ALLOWLIST",
    ):
        monkeypatch.delenv(name, raising=False)


def test_configured_review_names_the_client_id_and_loads_the_provider_script(page, monkeypatch):
    configure(monkeypatch)
    response = page.get("/")
    assert response.status_code == 200
    body = response.text
    assert f'<meta name="review-oauth-client-id" content="{AUDIENCE}" />' in body
    assert body.count(GSI_SCRIPT) == 1
    assert body.index(GSI_SCRIPT) < body.index("</head>")


def test_configured_review_allows_exactly_the_provider_origins(page, monkeypatch):
    configure(monkeypatch)
    csp = page.get("/").headers["Content-Security-Policy"]
    assert "script-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/client;" in csp
    assert (
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
        "https://accounts.google.com/gsi/style;"
    ) in csp
    assert "connect-src 'self' https://accounts.google.com/gsi/;" in csp
    assert "frame-src https://accounts.google.com/gsi/;" in csp
    assert "frame-ancestors 'none'" in csp
    assert csp.count("accounts.google.com") == 4


def test_unconfigured_review_leaves_the_page_and_policy_as_shipped(page, monkeypatch):
    unconfigure(monkeypatch)
    response = page.get("/")
    assert response.text == PAGE
    assert "review-oauth-client-id" not in response.text
    assert "accounts.google.com" not in response.text
    assert response.headers["Content-Security-Policy"] == main.CSP


def test_partial_review_configuration_names_nothing(page, monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv("REVIEW_SUBJECT_ALLOWLIST")
    response = page.get("/")
    assert response.text == PAGE
    assert "review-oauth-client-id" not in response.text
    assert response.headers["Content-Security-Policy"] == main.CSP


def test_the_client_id_is_escaped_into_the_attribute(page, monkeypatch):
    configure(monkeypatch, audience='a"><script>x</script>')
    body = page.get("/").text
    assert "<script>x</script>" not in body
    assert 'content="a&quot;&gt;&lt;script&gt;x&lt;/script&gt;"' in body


def test_other_routes_keep_the_shipped_policy(page, monkeypatch):
    configure(monkeypatch)
    assert page.get("/api/health").headers["Content-Security-Policy"] == main.CSP
