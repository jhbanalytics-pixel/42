"""Unit tests for src/alerts/self_heal.py and the email digest heal surface."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from src.alerts.email_digest import HealedIssue, render_html, render_text
from src.alerts.self_heal import (
    FALLBACK_PATHS,
    HealResult,
    heal_errors,
    try_heal_rss_404,
)


def _mock_response(status: int = 200, body: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.text = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else body
    return resp


_FEED_BODY = b'<?xml version="1.0"?><rss version="2.0"><channel><title>X</title></channel></rss>'
_ATOM_BODY = (
    b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>X</title></feed>'
)


def test_try_heal_rss_404_finds_link_alternate():
    """When the origin HTML carries <link rel=alternate>, use that href first."""
    html_body = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" '
        'href="https://example.com/the-new-feed.xml">'
        "</head><body>hi</body></html>"
    )

    def fake_get(url, timeout=15, allow_redirects=True, headers=None):
        if url == "https://example.com":
            return _mock_response(200, html_body.encode())
        if url == "https://example.com/the-new-feed.xml":
            return _mock_response(200, _FEED_BODY)
        return _mock_response(404, b"")

    with patch("src.alerts.self_heal.requests.get", side_effect=fake_get):
        result = try_heal_rss_404({"url": "https://example.com/old-feed.xml"})

    assert result.kind == "fixed"
    assert result.new_url == "https://example.com/the-new-feed.xml"
    assert "validated" in result.reason


def test_try_heal_rss_404_falls_through_to_feed_path():
    """No <link> tag in origin HTML, but /feed serves a valid payload."""
    plain_html = "<html><body>no feed tags here</body></html>"

    def fake_get(url, timeout=15, allow_redirects=True, headers=None):
        if url == "https://example.com":
            return _mock_response(200, plain_html.encode())
        if url == "https://example.com/rss":
            return _mock_response(404, b"not found")
        if url == "https://example.com/feed":
            return _mock_response(200, _ATOM_BODY)
        return _mock_response(404, b"")

    with patch("src.alerts.self_heal.requests.get", side_effect=fake_get):
        result = try_heal_rss_404({"url": "https://example.com/old.xml"})

    assert result.kind == "fixed"
    assert result.new_url == "https://example.com/feed"


def test_try_heal_rss_404_returns_still_broken_when_no_candidate_works():
    """Every candidate 404s or returns non-feed HTML -> still_broken."""
    plain_html = "<html><body>nothing</body></html>"

    def fake_get(url, timeout=15, allow_redirects=True, headers=None):
        if url == "https://example.com":
            return _mock_response(200, plain_html.encode())
        return _mock_response(404, b"")

    with patch("src.alerts.self_heal.requests.get", side_effect=fake_get):
        result = try_heal_rss_404({"url": "https://example.com/old.xml"})

    assert result.kind == "still_broken"
    assert result.new_url is None
    # All fallback paths + origin root attempt should show in the reason count.
    assert "attempt" in result.reason


def test_heal_errors_rewrites_sources_yaml(tmp_path: Path):
    """heal_errors rewrites the broken URL in-place, preserving comments."""
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        """# Unified Sources Configuration
rss_feeds:
  ke:
    # Removed as of 22 Apr 2026: various dead feeds.
    - name: The Standard
      url: https://www.standardmedia.co.ke/rss/headlines.php
    - name: Broken Outlet
      url: https://broken.example.com/old-feed.xml
""",
        encoding="utf-8",
    )

    errors = [
        {
            "market": "ke",
            "source": "rss",
            "error": (
                "404 Client Error: Not Found for url: https://broken.example.com/old-feed.xml"
            ),
        }
    ]

    def fake_get(url, timeout=15, allow_redirects=True, headers=None):
        if url == "https://broken.example.com":
            return _mock_response(
                200,
                b'<html><head><link rel="alternate" type="application/rss+xml" '
                b'href="/new-feed.xml"></head></html>',
            )
        if url == "https://broken.example.com/new-feed.xml":
            return _mock_response(200, _FEED_BODY)
        return _mock_response(404, b"")

    with patch("src.alerts.self_heal.requests.get", side_effect=fake_get):
        results, remaining = heal_errors(errors, yaml_path)

    assert len(results) == 1
    assert results[0].kind == "fixed"
    assert results[0].new_url == "https://broken.example.com/new-feed.xml"
    assert remaining == []

    new_text = yaml_path.read_text(encoding="utf-8")
    # Comment preserved.
    assert "# Unified Sources Configuration" in new_text
    assert "# Removed as of 22 Apr 2026: various dead feeds." in new_text
    # Old URL gone, new URL in place, indentation intact.
    assert "https://broken.example.com/old-feed.xml" not in new_text
    assert "https://broken.example.com/new-feed.xml" in new_text
    # Working feed untouched.
    assert "https://www.standardmedia.co.ke/rss/headlines.php" in new_text
    # Structural key untouched.
    assert "name: Broken Outlet" in new_text


def test_heal_errors_skips_non_rss_errors(tmp_path: Path):
    """Non-RSS errors never trigger network calls and pass through unchanged."""
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("rss_feeds:\n  ke: []\n", encoding="utf-8")

    errors = [
        {"market": "za", "source": "youtube", "error": "quota exceeded 403"},
        {"market": "ng", "source": "ensemble", "error": "HTTP 495"},
        {"market": "ke", "source": "rss", "error": "connection reset by peer"},
    ]

    with patch("src.alerts.self_heal.requests.get") as mock_get:
        results, remaining = heal_errors(errors, yaml_path)

    # None of the non-RSS errors should have been processed, and the RSS
    # error is not 404-class, so heal_errors returns no results at all.
    assert results == []
    assert len(remaining) == 3
    mock_get.assert_not_called()


def test_email_digest_renders_auto_resolved_block():
    """render_html/text include the auto-fix section when heals present. The
    label is the honest run-local one, not a permanent-fix claim."""
    healed = [
        HealedIssue(
            label="RSS (KE)",
            original_url="https://old.example.com/feed.xml",
            new_url="https://old.example.com/rss",
            reason="validated new feed URL after 2 attempt(s)",
        )
    ]

    html_body = render_html({}, date(2026, 4, 24), healed=healed)
    assert "Auto-fixed (this run)" in html_body
    assert "https://old.example.com/feed.xml" in html_body
    assert "https://old.example.com/rss" in html_body
    assert "RSS (KE)" in html_body

    text_body = render_text({}, date(2026, 4, 24), healed=healed)
    assert "Auto-fixed (this run)" in text_body
    assert "https://old.example.com/feed.xml -> https://old.example.com/rss" in text_body


def test_email_digest_flips_issue_heading_when_heals_present():
    """With healed items present, the still-broken heading changes."""
    from src.alerts.email_digest import IssueFlag

    healed = [
        HealedIssue(
            label="RSS (ZA)",
            original_url="https://a.example/old",
            new_url="https://a.example/new",
        )
    ]
    issues = [IssueFlag(label="RSS (NG)", detail="dead host")]

    html_body = render_html({}, date(2026, 4, 24), issues=issues, healed=healed)
    assert "Still broken" in html_body
    assert "Things that broke" not in html_body

    # Backward compat: no healed items -> old heading.
    html_body_plain = render_html({}, date(2026, 4, 24), issues=issues)
    assert "Things that broke" in html_body_plain
    assert "Still broken" not in html_body_plain


def test_heal_errors_handles_invalid_yaml(tmp_path: Path):
    """Corrupt YAML must not crash heal_errors.

    The pipeline's safety contract is that alert delivery never breaks the
    run. A YAML parse error on sources.yaml (editor-munged indentation,
    stray tab) should be logged and still allow heal discovery to return
    results. The rewrite step will skip because the file was unreadable,
    so persisted flags stay False.
    """
    yaml_path = tmp_path / "sources.yaml"
    # Tab char inside a YAML block is a parse error.
    yaml_path.write_text(
        "rss_feeds:\n  ke:\n    - name: X\n\t  url: broken-tab-indent\n",
        encoding="utf-8",
    )
    errors = [
        {
            "market": "ke",
            "source": "rss",
            "error": "404 Client Error: Not Found for url: https://broken.example.com/feed",
        }
    ]

    def fake_get(url, timeout=8, allow_redirects=True, headers=None):
        return _mock_response(404, b"")

    with patch("src.alerts.self_heal.requests.get", side_effect=fake_get):
        results, remaining = heal_errors(errors, yaml_path)

    # No crash. Heal attempted, no valid candidate found, still_broken.
    assert len(results) == 1
    assert results[0].kind == "still_broken"
    assert len(remaining) == 1


def test_heal_errors_sets_persisted_false_when_write_fails(tmp_path: Path):
    """Disk-write failure on sources.yaml must mark every fix persisted=False.

    The email digest uses persisted to gate the 'auto-resolved' section.
    Surfacing an unpersisted fix lies to the reader: the same URL 404s
    tomorrow because nothing reached disk.
    """
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        """rss_feeds:
  ke:
    - name: Broken Outlet
      url: https://broken.example.com/old-feed.xml
""",
        encoding="utf-8",
    )
    errors = [
        {
            "market": "ke",
            "source": "rss",
            "error": (
                "404 Client Error: Not Found for url: https://broken.example.com/old-feed.xml"
            ),
        }
    ]

    def fake_get(url, timeout=8, allow_redirects=True, headers=None):
        if url == "https://broken.example.com":
            return _mock_response(200, b"<html></html>")
        if url == "https://broken.example.com/rss":
            return _mock_response(200, _FEED_BODY)
        return _mock_response(404, b"")

    original_write_text = Path.write_text

    def failing_write_text(self, *args, **kwargs):
        if self == yaml_path:
            raise PermissionError("locked")
        return original_write_text(self, *args, **kwargs)

    with (
        patch("src.alerts.self_heal.requests.get", side_effect=fake_get),
        patch.object(Path, "write_text", failing_write_text),
    ):
        results, _ = heal_errors(errors, yaml_path)

    assert len(results) == 1
    assert results[0].kind == "fixed"
    assert results[0].new_url == "https://broken.example.com/rss"
    assert results[0].persisted is False
    # Original file unchanged on disk.
    after = yaml_path.read_text(encoding="utf-8")
    assert "https://broken.example.com/old-feed.xml" in after


def test_heal_errors_marks_persisted_true_on_successful_write(tmp_path: Path):
    """Happy path: successful rewrite flips persisted to True."""
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        """rss_feeds:
  ke:
    - name: Broken Outlet
      url: https://broken.example.com/old-feed.xml
""",
        encoding="utf-8",
    )
    errors = [
        {
            "market": "ke",
            "source": "rss",
            "error": (
                "404 Client Error: Not Found for url: https://broken.example.com/old-feed.xml"
            ),
        }
    ]

    def fake_get(url, timeout=8, allow_redirects=True, headers=None):
        if url == "https://broken.example.com":
            return _mock_response(200, b"<html></html>")
        if url == "https://broken.example.com/rss":
            return _mock_response(200, _FEED_BODY)
        return _mock_response(404, b"")

    with patch("src.alerts.self_heal.requests.get", side_effect=fake_get):
        results, _ = heal_errors(errors, yaml_path)

    assert results[0].kind == "fixed"
    assert results[0].persisted is True


def test_rewrite_yaml_handles_same_url_in_multiple_markets():
    """If the same dead URL is shared across markets (aggregator feeds),
    every occurrence gets rewritten in one pass."""
    from src.alerts.self_heal import _rewrite_yaml_url

    yaml_text = """rss_feeds:
  za:
    - name: AllAfrica
      url: https://shared.example.com/old.xml
  ng:
    - name: AllAfrica
      url: https://shared.example.com/old.xml
"""
    new_text, replaced = _rewrite_yaml_url(
        yaml_text,
        "https://shared.example.com/old.xml",
        "https://shared.example.com/new.xml",
    )
    assert replaced is True
    assert new_text.count("https://shared.example.com/new.xml") == 2
    assert "https://shared.example.com/old.xml" not in new_text


def test_is_404_class_does_not_trigger_on_transient_errors():
    """Transient network failures (timeouts, SSL, 5xx, max-retries) should
    not trigger healing, to avoid wasting up to 7 HTTP requests on a feed
    that will recover on its own next run."""
    from src.alerts.self_heal import _is_404_class

    # Genuine 404 / DNS failures stay healable.
    assert _is_404_class("404 Client Error: Not Found") is True
    assert _is_404_class("NameResolutionError on example.com") is True
    assert _is_404_class("failed to resolve example.com") is True

    # Transient-looking errors should NOT trigger healing.
    assert _is_404_class("HTTPSConnectionPool: Max retries exceeded") is False
    assert _is_404_class("SSLError: certificate verify failed") is False
    assert _is_404_class("ReadTimeoutError after 30 seconds") is False
    assert _is_404_class("500 Internal Server Error") is False


def test_heal_fetches_send_polite_user_agent():
    """Both origin discovery and candidate validation must send the polite
    bot User-Agent. SSA outlets bot-block the default python-requests UA, so
    a heal that omits it 403s on every fetch and can never succeed."""
    from src.ingestion.connectors.rss import RSSConnector

    seen_headers: list[dict | None] = []

    def fake_get(url, timeout=8, allow_redirects=True, headers=None):
        seen_headers.append(headers)
        if url == "https://example.com":
            return _mock_response(
                200,
                b'<html><head><link rel="alternate" type="application/rss+xml" '
                b'href="/new.xml"></head></html>',
            )
        if url == "https://example.com/new.xml":
            return _mock_response(200, _FEED_BODY)
        return _mock_response(404, b"")

    with patch("src.alerts.self_heal.requests.get", side_effect=fake_get):
        result = try_heal_rss_404({"url": "https://example.com/old.xml"})

    assert result.kind == "fixed"
    # Origin fetch + candidate validation both carry the polite UA.
    assert seen_headers, "no fetch was attempted"
    for headers in seen_headers:
        assert headers is not None
        assert headers.get("User-Agent") == RSSConnector.USER_AGENT


def test_dns_class_error_short_circuits_discovery():
    """A DNS-failure error means the hostname is gone. Discovery only re-probes
    the same dead host, so every candidate fails by construction. The heal must
    short-circuit to still_broken without firing any HTTP request."""
    error = {
        "url": "https://gone.example.com/feed.xml",
        "error": "NameResolutionError: failed to resolve gone.example.com",
    }

    with patch("src.alerts.self_heal.requests.get") as mock_get:
        result = try_heal_rss_404(error)

    assert result.kind == "still_broken"
    assert "host unresolvable" in result.reason
    mock_get.assert_not_called()


def test_dns_class_short_circuit_from_url_only():
    """Even when the error string is empty, a 404-path error still runs
    discovery; the DNS short-circuit only fires on DNS-class error text."""

    def fake_get(url, timeout=8, allow_redirects=True, headers=None):
        return _mock_response(404, b"")

    # No DNS marker in the error text -> discovery runs (HTTP fired).
    with patch("src.alerts.self_heal.requests.get", side_effect=fake_get) as mock_get:
        result = try_heal_rss_404({"url": "https://example.com/old.xml", "error": "404 Not Found"})

    assert result.kind == "still_broken"
    mock_get.assert_called()


def test_rewrite_does_not_corrupt_prefix_url(tmp_path: Path):
    """A longer feed URL that has the broken URL as a prefix must not be
    partially rewritten. Exact-token match on the url: line only."""
    from src.alerts.self_heal import _rewrite_yaml_url

    yaml_text = """rss_feeds:
  ke:
    - name: Broken
      url: https://example.com/feed
    - name: Sibling Longer
      url: https://example.com/feed/extra.xml
"""
    new_text, replaced = _rewrite_yaml_url(
        yaml_text,
        "https://example.com/feed",
        "https://example.com/rss",
    )
    assert replaced is True
    # The exact broken URL was swapped.
    assert "url: https://example.com/rss\n" in new_text
    # The longer sibling URL is untouched (not partially rewritten).
    assert "https://example.com/feed/extra.xml" in new_text
    assert "https://example.com/rss/extra.xml" not in new_text


def test_rewrite_matches_url_at_end_of_line_without_trailing_space(tmp_path: Path):
    """A url: line whose value ends the string (no trailing newline) still
    matches the end-of-line anchor."""
    from src.alerts.self_heal import _rewrite_yaml_url

    yaml_text = "    - url: https://example.com/feed"
    new_text, replaced = _rewrite_yaml_url(
        yaml_text,
        "https://example.com/feed",
        "https://example.com/rss",
    )
    assert replaced is True
    assert new_text == "    - url: https://example.com/rss"


def test_fixed_but_unpersisted_feed_re_flagged_in_remaining(tmp_path: Path):
    """A fixed result whose write fails (persisted=False) must be appended back
    to remaining_errors, so the broken feed lands in the Still-broken digest
    section rather than vanishing from both sections."""
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        """rss_feeds:
  ke:
    - name: Broken Outlet
      url: https://broken.example.com/old-feed.xml
""",
        encoding="utf-8",
    )
    err = {
        "market": "ke",
        "source": "rss",
        "error": ("404 Client Error: Not Found for url: https://broken.example.com/old-feed.xml"),
    }

    def fake_get(url, timeout=8, allow_redirects=True, headers=None):
        if url == "https://broken.example.com":
            return _mock_response(200, b"<html></html>")
        if url == "https://broken.example.com/rss":
            return _mock_response(200, _FEED_BODY)
        return _mock_response(404, b"")

    original_write_text = Path.write_text

    def failing_write_text(self, *args, **kwargs):
        if self == yaml_path:
            raise PermissionError("locked")
        return original_write_text(self, *args, **kwargs)

    with (
        patch("src.alerts.self_heal.requests.get", side_effect=fake_get),
        patch.object(Path, "write_text", failing_write_text),
    ):
        results, remaining = heal_errors([err], yaml_path)

    assert results[0].kind == "fixed"
    assert results[0].persisted is False
    # The originating error is re-flagged so it still shows as still-broken.
    assert err in remaining


@pytest.mark.xfail(
    reason=(
        "Dead heal trigger pinned, not hidden. RSSConnector.fetch swallows a "
        "404 (raise_for_status -> RequestException caught + logged + continue), "
        "so safe_fetch returns an empty frame and the pipeline never appends an "
        "error for that feed. heal_errors is fed from that pipeline error list, "
        "so the RSS-404 heal path is unreachable in production. The connector is "
        "intentionally NOT changed to surface 404s: self_heal is documented "
        "dormant (its rewrite lands on an ephemeral runner disk recreated from "
        "the baked image each run, so it cannot durably persist). See the "
        "module docstring in src/alerts/self_heal.py."
    ),
    strict=True,
)
def test_rss_404_does_not_reach_heal_errors(tmp_path: Path):
    """A live 404 driven through RSSConnector.fetch never reaches heal_errors.

    This xfail documents the current dead-trigger boundary. It builds the
    pipeline error list the same way scripts/run_rss_now.py does (one entry
    per connector whose safe_fetch raises) and feeds it to heal_errors. Because
    the connector swallows the 404 internally, that list is empty and the heal
    is never attempted. The xfail asserts the heal WOULD see the 404; when the
    connector is one day taught to surface 404s (durable self_heal), this test
    flips to a pass and the xfail marker can be removed.
    """
    from src.ingestion.connectors.rss import RSSConnector

    sources = {
        "rss_feeds": {"ke": [{"name": "Dead Feed", "url": "https://dead.example.com/feed.xml"}]}
    }

    http_error = requests.exceptions.HTTPError(
        "404 Client Error: Not Found for url: https://dead.example.com/feed.xml"
    )

    def raise_404():
        raise http_error

    response = MagicMock()
    response.raise_for_status.side_effect = raise_404

    connector = RSSConnector(market="ke")

    # Build the pipeline error list exactly as run_rss_now.run does: append a
    # dict only when safe_fetch raises. The connector swallows the 404, so
    # nothing is appended.
    errors: list[dict] = []
    with (
        patch("src.ingestion.connectors.rss.load_sources", return_value=sources),
        patch.object(connector._session, "get", return_value=response),
    ):
        try:
            connector.safe_fetch()
        except Exception as exc:  # pragma: no cover - swallowed before here
            errors.append({"market": "ke", "source": "rss", "error": str(exc)})

    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "rss_feeds:\n  ke:\n    - name: Dead Feed\n      url: https://dead.example.com/feed.xml\n",
        encoding="utf-8",
    )

    with patch("src.alerts.self_heal.requests.get") as mock_get:
        _results, _remaining = heal_errors(errors, yaml_path)

    # Desired behaviour (currently unmet): the 404 reaches heal_errors and a
    # discovery fetch fires. Today the connector swallows the 404, so errors is
    # empty, heal_errors is a no-op, and no fetch happens -> xfail.
    mock_get.assert_called()
