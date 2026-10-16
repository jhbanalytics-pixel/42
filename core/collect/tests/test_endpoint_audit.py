"""Every SocialCrawl catalogue route has a verdict, and the live ones are the ones 42 can actually call."""

from core.agent.tools.socialcrawl import ALLOWED_ROUTES, FORBIDDEN
from core.collect import endpoint_audit as audit
from core.collect.parse import ROUTES as PARSED
from core.collect.socialcrawl_client import PRICED


def catalogue():
    return {route for route, _, _ in audit.routes()}


def test_every_catalogue_route_has_a_known_verdict_and_a_reason():
    rows = audit.table()
    assert len(rows) == len(audit.routes())
    for row in rows:
        assert row["verdict"] in audit.ORDER, row
        assert row["reason"].strip(), row


def test_every_named_route_is_in_the_catalogue():
    known = catalogue()
    for group in (audit.LIVE, audit.CANDIDATE, audit.LATER, audit.NEVER):
        assert set(group) <= known, sorted(set(group) - known)


def test_a_route_has_one_named_verdict_at_most():
    groups = (audit.LIVE, audit.CANDIDATE, audit.LATER, audit.NEVER)
    for i, a in enumerate(groups):
        for b in groups[i + 1:]:
            assert not set(a) & set(b), sorted(set(a) & set(b))


def test_every_live_route_is_priced_and_every_priced_route_is_live_or_explained():
    assert set(audit.LIVE) <= set(PRICED), sorted(set(audit.LIVE) - set(PRICED))
    for route in PRICED:
        assert audit.verdict(route)[0] in ("live", "candidate", "later", "never", "lookup"), route


def test_what_ask_and_the_morning_parser_read_is_live_or_ruled_out():
    for route in ALLOWED_ROUTES | set(PARSED):
        assert audit.verdict(route)[0] == "live", route
    for banned in FORBIDDEN:
        route = banned.rstrip("/")
        if route in catalogue():
            assert audit.verdict(route)[0] == "never", route


def test_the_wider_net_routes_are_live():
    for route in ("bluesky/search", "pinterest/search", "linkedin/search/posts"):
        assert audit.verdict(route)[0] == "live"


def test_the_markdown_counts_every_route_once():
    rows = audit.table()
    text = audit.markdown(rows)
    assert text.startswith(f"{len(rows)} catalogue routes")
    assert sum(1 for line in text.splitlines() if line.startswith("| ") and not line.startswith("| Route")) == len(rows)
