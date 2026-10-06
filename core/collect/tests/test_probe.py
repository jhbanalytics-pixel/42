"""Unit tests for core/collect/probe.py and core/collect/probe_report.py (BUILD.md task 0.5). No network."""

import io
import json
import re
from contextlib import redirect_stdout
from datetime import date, datetime, timezone

import pytest
import yaml

from core.collect import probe, probe_report, socialcrawl_client
from core.collect.socialcrawl_client import PRICED, Result, SocialCrawlClient, forbidden, quote_for
from core.collect.stores import MemoryLedgerStore, MemoryRawStore

TODAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
TRENDS = "".join(["google", "trends"])
STOP_TOTAL = 150
DASHES = "[" + chr(0x2013) + chr(0x2014) + "]"
NO_AGE = "".join(["gen", "z"])

# docs/full-42/research/13-socialcrawl-full-map.md section 5, in order, plus probe 14; the free calls
# run first and credits/transactions last.
RESEARCH_ROUTES = [
    "credits/balance", "status", "utility/capabilities",
    "tiktok/trending", "tiktok/trending", "tiktok/trending",
    "youtube/videos/trending", "youtube/videos/trending", "youtube/videos/trending",
    "tiktok/hashtags/popular",
    "apple_music/charts", "apple_music/charts",
    "search/news",
    "twitter/search/tweets", "twitter/search/tweets",
    "youtube/search/advanced",
    "instagram/search/location", "instagram/location/posts",
    "facebook/events", "facebook/search/posts",
    "search/multi", "search/multi",
    "tiktok/search/top", "tiktok/search/top",
    "tiktok/song/videos", "tiktok/hashtag",
    "prism/post-stats",
    "web/scrape", "web/scrape", "web/scrape",
    "credits/transactions",
]


def trending_body(region_rows, prefix, music=True):
    items = []
    for i, region in enumerate(region_rows):
        post = {
            "id": f"{prefix}{i}",
            "url": f"https://www.tiktok.com/@maker{i}/video/{prefix}{i}",
            "author": {"username": f"maker{i}", "display_name": f"Maker {i}"},
            "content": {"text": f"hello @friend{i} #fyp"},
            "engagement": {"views": 1000 + i, "likes": 10},
            "ext": {"region": region},
        }
        if music and i == 0:
            post["music"] = {"id": f"clip-{prefix}", "title": "Song"}
        items.append({"post": post})
    return {"success": True, "credits_used": 5, "data": {"items": items}}


# probe.py -----------------------------------------------------------------------------------------


def test_plan_order_matches_the_research_list():
    plan = probe.plan(TODAY)
    assert [p["route"] for p in plan] == RESEARCH_ROUTES
    numbers = [p["n"] for p in plan if p["n"]]
    assert numbers == sorted(numbers)
    assert sorted(set(numbers)) == list(range(1, 15))
    ids = [p["id"] for p in plan]
    assert len(ids) == len(set(ids))


def test_plan_total_hold_is_within_the_stop_rule():
    total = sum(probe.hold(p) for p in probe.plan(TODAY))
    assert 0 < total <= STOP_TOTAL


def test_plan_holds_follow_the_decisions():
    by_id = {p["id"]: p for p in probe.plan(TODAY)}
    assert probe.hold(by_id["3"]) == 6
    assert "industry" not in by_id["3"]["params"]
    assert probe.hold(by_id["5"]) == 5
    assert by_id["5"]["params"]["countries"] == "ZA,NG,KE" and by_id["5"]["params"]["max_legs"] == 3
    assert by_id["10a"]["params"]["dry_run"] == 1 and "dry_run" not in by_id["10b"]["params"]
    assert all(p["params"]["feed"] == "local" for p in probe.plan(TODAY) if p["route"] == "tiktok/trending")


def test_no_trends_route_and_nothing_the_client_refuses():
    for p in probe.plan(TODAY):
        assert TRENDS not in re.sub(r"[^a-z]", "", json.dumps([p["route"], p["params"]]).lower())
        assert p["route"] in PRICED
        assert forbidden(p["route"], p["method"], dict(p["params"])) == ""
        quote_for(p["route"], p["method"], dict(probe.plan_params(p)))  # raises Refused when unpriced


def test_repeat_probe_calls_the_same_request_twice_without_the_cache():
    by_id = {p["id"]: p for p in probe.plan(TODAY)}
    assert by_id["11a"]["params"] == by_id["11b"]["params"]
    assert by_id["11a"]["params"]["country"] == "KE" and by_id["11a"]["params"]["seen"]
    assert by_id["11a"].get("use_cache", True) is True
    assert by_id["11b"]["use_cache"] is False


def test_plan_flag_prints_every_call_and_the_total_without_network(monkeypatch):
    monkeypatch.setattr(probe, "live_client", lambda run_id: pytest.fail("--plan must not build a client"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--plan"], today=TODAY) == 0
    text = buf.getvalue()
    for p in probe.plan(TODAY):
        assert p["id"] in text and p["route"] in text
    total = sum(probe.hold(p) for p in probe.plan(TODAY))
    assert f"total hold {total}" in text


class FakeClient:
    """Answers by probe order; records every call. stop_at is the call index that returns stop_status."""

    def __init__(self, stop_at=None, stop_status="insufficient_credits", bodies=None):
        self.calls = []
        self.stop_at, self.stop_status = stop_at, stop_status
        self.bodies = bodies or {}

    def call(self, route, params=None, **kw):
        self.calls.append({"route": route, "params": params, **kw})
        if self.stop_at is not None and len(self.calls) - 1 >= self.stop_at:
            return Result(self.stop_status, route, reason="stop")
        body = self.bodies.get((route, kw.get("market")), {"success": True, "data": {"items": [{"a": 1}]}})
        items = body["data"]["items"] if isinstance(body.get("data"), dict) else []
        return Result("ok", route, "h", 200, 1, 1, False, body, items, [])


def fake_bodies():
    return {
        ("tiktok/trending", "ZA"): trending_body(["ZA", "ZA", "US"], "za"),
        ("tiktok/trending", "NG"): trending_body(["NG", "GH"], "ng"),
        ("tiktok/trending", "KE"): trending_body(["KE"], "ke"),
        ("instagram/search/location", "ZA"): {"success": True, "data": {"items": [{"pk": "2159", "name": "Soweto"}]}},
    }


@pytest.mark.parametrize("status", ["insufficient_credits", "balance_floor", "cap_reached"])
def test_main_stops_the_run_and_never_retries(status):
    client = FakeClient(stop_at=5, stop_status=status, bodies=fake_bodies())
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.main([], client=client, clock=lambda: NOW)
    assert len(client.calls) == 6
    keys = [(c["route"], json.dumps(c["params"], sort_keys=True)) for c in client.calls]
    assert len(keys) == len(set(keys))
    assert "stopped" in buf.getvalue()


def test_main_runs_every_probe_once_in_order_with_derived_ids():
    client = FakeClient(bodies=fake_bodies())
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main([], client=client, clock=lambda: NOW) == 0
    assert [c["route"] for c in client.calls] == RESEARCH_ROUTES
    by_route = {}
    for c in client.calls:
        by_route.setdefault(c["route"], []).append(c)
    assert by_route["instagram/location/posts"][0]["params"]["location_id"] == "2159"
    assert by_route["tiktok/song/videos"][0]["params"]["clipId"] == "clip-za"
    urls = by_route["prism/post-stats"][0]["params"]["urls"]
    assert len(urls) == 3 and all(u.startswith("https://www.tiktok.com/") for u in urls)
    assert by_route["prism/post-stats"][0]["method"] == "POST"
    searches = by_route["tiktok/search/top"]
    assert searches[0]["params"] == searches[1]["params"]
    assert searches[0]["use_cache"] is True and searches[1]["use_cache"] is False
    assert all(c["lane"] is None for c in client.calls)
    assert [c["seed_key"] for c in client.calls] == [p["id"] for p in probe.plan(TODAY)]


def test_missing_source_row_skips_the_derived_probe():
    bodies = fake_bodies()
    bodies[("instagram/search/location", "ZA")] = {"success": True, "data": {"items": []}}
    for market in ("ZA", "NG", "KE"):
        bodies[("tiktok/trending", market)] = trending_body(["ZA"], market.lower(), music=False)
    client = FakeClient(bodies=bodies)
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.main([], client=client, clock=lambda: NOW)
    routes = [c["route"] for c in client.calls]
    assert "instagram/location/posts" not in routes and "tiktok/song/videos" not in routes
    assert "prism/post-stats" in routes
    lines = [line for line in buf.getvalue().splitlines() if "skipped" in line]
    assert len(lines) == 2 and all("no " in line for line in lines)


def test_log_lines_carry_no_body_and_no_key(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "sc-test-sentinel-9a")
    client = FakeClient(bodies=fake_bodies())
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.main([], client=client, clock=lambda: NOW)
    text = buf.getvalue()
    assert "sc-test-sentinel-9a" not in text
    assert "maker0" not in text and "hello" not in text
    line = next(line for line in text.splitlines() if line.startswith("1-za "))
    assert "tiktok/trending" in line and "ok" in line and "items=3" in line


# probe_report.py ----------------------------------------------------------------------------------


def test_local_share_and_overlap_on_a_fixture():
    za = trending_body(["ZA", "ZA", "US", "NG"], "za")
    ng = trending_body(["NG", "GH", "NG", "NG"], "ng")
    ke = trending_body(["KE", "TZ"], "ke")
    ke["data"]["items"].append(ng["data"]["items"][0])  # one NG video also in the KE feed
    assert probe_report.local_share(za, "ZA") == (2, 4, 0.5)
    assert probe_report.local_share(ng, "NG") == (3, 4, 0.75)
    assert probe_report.local_share(ke, "KE") == (1, 3, pytest.approx(1 / 3))
    assert probe_report.local_share({"success": True, "data": {"items": []}}, "KE") == (0, 0, None)
    assert probe_report.overlap(ng, ke) == (1, 4, 3)


def ledger_row(route, phash, charged, cache_hit=False, when="2026-09-29T04:00:00+00:00"):
    return {"route": route, "params_hash": phash, "credits_charged": charged, "credits_quoted": charged,
            "cache_hit": cache_hit, "calls": 0 if cache_hit else 1, "market": None, "logged_at": when}


def raw_row(route, phash, seed_key, status, body, when="2026-09-29T04:00:00+00:00"):
    return {"route": route, "params_hash": phash, "seed_key": seed_key, "http_status": status, "body": body,
            "market": None, "fetched_at": when, "cache_hit": False}


def report_fixture():
    trend = trending_body(["ZA", "US"], "za")
    search = {"success": True, "credits_used": 0.5, "data": {"items": [{"a": 1}, {"a": 2}]}}
    ledger = [
        ledger_row("tiktok/trending", "h1", 5, when="2026-09-29T04:00:01+00:00"),
        ledger_row("tiktok/search/top", "h2", 1, when="2026-09-29T04:00:02+00:00"),
        ledger_row("tiktok/search/top", "h2", 1, when="2026-09-29T04:00:03+00:00"),
        ledger_row("search/news", "h3", 5, when="2026-09-29T04:00:04+00:00"),
    ]
    raw = [
        raw_row("tiktok/trending", "h1", "1-za", 200, trend, "2026-09-29T04:00:01+00:00"),
        raw_row("tiktok/search/top", "h2", "11a", 200, search, "2026-09-29T04:00:02+00:00"),
        raw_row("tiktok/search/top", "h2", "11b", 200, {"success": True, "data": {"items": []}},
                "2026-09-29T04:00:03+00:00"),
        raw_row("search/news", "h3", "5", 502, None, "2026-09-29T04:00:04+00:00"),
    ]
    return ledger, raw


def test_report_table_rows_match_ledger_rows():
    ledger, raw = report_fixture()
    table = probe_report.table(ledger, raw)
    assert [(r["probe"], r["route"], r["status"], r["charged"], r["reported"], r["rows"]) for r in table] == [
        ("1-za", "tiktok/trending", 200, 5, 5, 2),
        ("11a", "tiktok/search/top", 200, 1, 0.5, 2),
        ("11b", "tiktok/search/top", 200, 1, None, 0),
        ("5", "search/news", 502, 5, None, 0),
    ]


def test_cached_ledger_row_is_shown_as_cached():
    ledger = [ledger_row("tiktok/trending", "h1", 0, cache_hit=True)]
    assert probe_report.table(ledger, []) == [
        {"probe": None, "route": "tiktok/trending", "status": "cached", "charged": 0, "reported": None, "rows": 0}]


def test_report_markdown_states_low_share_and_overlap():
    ledger, raw = report_fixture()
    shares = {"ZA": (1, 4, 0.25), "NG": (3, 4, 0.75), "KE": (0, 0, None)}
    text = probe_report.render("probe-20260929T040000Z", probe_report.table(ledger, raw), shares, (2, 4, 3),
                               missing=["8b"])
    assert "| 1-za | tiktok/trending | 200 | 5 | 5 | 2 |" in text
    assert "ZA: 1 of 4 (25%), under 40%" in text
    assert "NG: 3 of 4 (75%)" in text and "NG: 3 of 4 (75%), under" not in text
    assert "KE: no videos" in text
    assert "The feeds overlap: NG and KE share 2 video ids (4 NG, 3 KE)." in text
    assert "8b" in text
    prose = "\n".join(line for line in text.splitlines() if not re.fullmatch(r"\|[-|: ]+\|", line))
    assert not re.search(DASHES, text) and "--" not in prose
    distinct = probe_report.render("r", [], {}, (0, 4, 3), missing=[])
    assert "NG and KE feeds are distinct: 0 shared video ids (4 NG, 3 KE)." in distinct


TT = "tiktok/trending"
REVIEW_CASES = [
    *[(k, {"items": [{"snippet": {k: "Thabo Mokoena"}}]}, TT)
      for k in ["channelTitle", "screenName", "displayName", "fullName", "authorDisplayName", "authorName"]],
    ("author string", {"author": "thabo_m"}, TT),
    ("creators[].name", {"creators": [{"name": "Thabo Mokoena"}]}, TT),
    ("top_creators[].name", {"top_creators": [{"name": "Thabo Mokoena", "handle": "thabo_m"}]}, TT),
    ("top_creators string list", {"top_creators": ["thabo_m", "lerato_k"]}, TT),
    ("local phone 10", {"text": "call 0825550101"}, TT),
    ("local phone 11 NG", {"text": "call 08031234567"}, TT),
    ("bare domain path", {"text": "see instagram.com/thabo_m"}, TT),
    ("phone (082) 555-0101", {"text": "call (082) 555-0101"}, TT),
    ("phone 27 82 555 0101 no plus", {"text": "whatsapp 27 82 555 0101"}, TT),
    ("phone 082 555 01 01", {"text": "call 082 555 01 01"}, TT),
    ("phone as int under contact", {"contact": 27825550101}, TT),
    ("phone as int under tel", {"tel": 825550101}, TT),
    ("mentions list plain", {"post": {"mentions": ["thabo_m", "lerato_k"]}}, TT),
    ("Facebook from.name", {"items": [{"post": {"from": {"name": "Thabo Mokoena", "id": "1"}}}]}, TT),
    ("twitter legacy.name nested", {"user": {"legacy": {"name": "Thabo Mokoena", "screen_name": "thabo_m"}}}, TT),
    ("handle keyed dict", {"users": {"thabo_m": {"followers": 10}}}, TT),
    ("subdomain profile", {"text": "bio: thabo.substack.com"}, TT),
    ("word@handle no boundary", {"text": "IG@thabo_m"}, TT),
    ("fullwidth at", {"text": "＠thabo_m"}, TT),
    ("email obfuscated", {"text": "thabo at gmail dot com"}, TT),
    ("email plain", {"bio": "thabo.m@gmail.com"}, TT),
    ("uppercase URL", {"text": "HTTPS://INSTAGRAM.COM/THABO"}, TT),
    ("author_slug", {"post": {"author_slug": "thabo-m"}}, TT),
    ("poster/by key", {"post": {"posted_by": "thabo_m", "by": "lerato_k"}}, TT),
    ("nick key", {"nick": "thabo_m"}, TT),
    ("organizer.name events", {"events": [{"organizer": {"name": "Thabo Mokoena"}}]}, TT),
    ("host name events", {"events": [{"host": "Thabo Mokoena"}]}, TT),
    ("commenter", {"comments": [{"commenter": "thabo_m", "text": "fire"}]}, TT),
    ("uploader_id plain", {"uploader_id": "thabo_m"}, TT),
    ("wa.me link", {"text": "order wa.me/27825550101"}, TT),
    ("tiktok @ url no scheme", {"text": "tiktok.com/@thabo_m"}, TT),
    ("mailto", {"text": "mailto:thabo@gmail.com"}, TT),
    ("region key raw", {"ext": {"region": "thabo@gmail.com"}}, TT),
    ("key named email-ish in key", {"meta": {"thabo@gmail.com": 1}}, TT),
    ("account balance", {"balance": 100, "email": "thabo@gmail.com"}, "credits/balance"),
    ("account transactions", {"items": [{"user": "thabo_m"}]}, "credits/transactions"),
]


ROUND3_CASES = {
    "01 nested person depth4": {"data": {"items": [{"stats": {"playCount": 5}, "wrap": {"inner": {"author": {
        "uniqueId": "thabo_m", "nickname": "Thabo Mokoena", "signature": "call 082 123 4567", "text": "hi thabo"}}}}]}},
    "02 arrays of strings under text": {"data": [{"text": ["Thabo Mokoena", "mail thabo@gmail.com", "@lerato_k"]}]},
    "03 arrays under author": {"authors": ["thabo_m", "lerato"], "mentions": ["thabo_m"]},
    "04 phone as int under value": {"author": {"phone": {"value": 27821234567}}},
    "05 phone as int under count": {"contact": {"count": 27821234567}},
    "06 phone int plain": {"meta": {"phone": 27821234567, "wa": 821234567}},
    "07 fb events": {"data": {"events": [{
        "name": "Amapiano Sundays", "id": "123",
        "hosts": [{"name": "Thabo Mokoena", "url": "https://facebook.com/thabo"}],
        "place": {"name": "Soweto Theatre", "contact": "thabo@gmail.com"},
        "description": "RSVP 071 555 1234 or thabo at gmail dot com", "going_count": 55,
        "start_time": "2026-10-03T20:00:00+02:00"}]}},
    "08 prism profiles": {"profiles": [{"platform": "tiktok", "handle": "thabo_m", "display_name": "Thabo",
                                        "bio": "IG thabo_m", "followers": 1200,
                                        "profile_url": "https://tiktok.com/@thabo_m"}]},
    "09 search/multi": {"results": {
        "instagram": [{"caption": "vibes w/ Thabo", "owner_username": "thabo_m",
                       "title": "Thabo Mokoena (@thabo_m) on Instagram"}],
        "twitter": [{"user": {"screen_name": "thabo"}, "full_text": "hi", "in_reply_to_screen_name": "lerato"}]}},
    "10 music original sound": {"data": [{"music": {"title": "original sound - thabo_m",
                                                    "authorName": "Thabo Mokoena", "id": "7"}}]},
    "11 fb tags people": {"post": {"tags": [{"name": "Thabo Mokoena", "id": "1"}],
                                   "message_tags": [{"name": "Lerato K"}]}},
    "12 handle key outside person": {"stats": {"thabo_m": 3, "lerato.k": 4, "27821234567": 1}},
    "13 status token handle": {"author": {"status": "Thabo Mokoena", "type": "thabo_m"}},
    "14 birth date in person": {"user": {"birth_date": "1998-04-02", "birthday": "02/04/1998"}},
    "15 youtube channel result": {"items": [{
        "kind": "youtube#searchResult", "id": {"kind": "youtube#channel", "channelId": "UC1"},
        "snippet": {"title": "Thabo Mokoena", "channelTitle": "Thabo Mokoena",
                    "description": "Bookings: thabo[at]gmail[dot]com, +27 (0)82-123-4567"}}]},
    "16 phone odd separators": {"text": "call 082/123/4567 or 082_123_4567 or 082,123,4567"},
    "17 fullwidth": {"text": "thabo＠gmail．com and ＠thabo_m and "
                             "０８２１２３４５６７"},
    "18 url bypasses": {"description": "tiktok dot com/@thabo, hxxps://evil[.]com, instagram.com/thabo_m"},
    "19 key with at": {"data": {"thabo@gmail.com": 1, "@thabo": 2}},
    "20 hashtag list": {"hashtags": ["#amapiano", "@thabo_m", "thabo_m"],
                        "challenges": [{"title": "thabochallenge", "desc": "by @thabo"}]},
    "21 tag-parent name nested person": {"music": {"author": {"name": "Thabo"}},
                                         "sound": {"name": "Thabo voice 0821234567"}},
    "22 date-key phone string": {"posted_time": "0821234567", "edit_date": "27821234567", "x_time": 2025551234},
    "23 body as string": "Thabo Mokoena 082 123 4567",
    "24 id holding email": {"email_id": "thabo@gmail.com", "user_ids": ["thabo_m"]},
    "25 deep list depth": {"a": [[[{"owner": {"text": "Thabo"}}]]]},
    "26 region token abuse": {"region": "Tha", "language": "thabo", "platform": "thabo m", "kind": "thabo.m"},
    "27 contact list type/value": {"page": {"contact": [{"type": "phone", "value": "+27 82 123 4567"},
                                                        {"type": "whatsapp", "value": 27821234567}]}},
    "28 digits in key value text": {"content": "ref 12345678 and 1234 5678 9"},
}


# Samples are value-free skeletons (task 0.5 review, round 5 design change).

TYPE_NAMES = {"<str>", "<int>", "<float>", "<bool>", "<null>"}
LIST_MARK = re.compile(r"<list of \d+>")


def assert_value_free(node):
    """Every key is a vocabulary word or "<key>", every leaf a type name, every list a length mark."""
    if isinstance(node, dict):
        assert set(node) <= set(probe_report.VOCABULARY) | {"<key>"}, set(node)
        assert "<key>" not in node or len(node) == 1
        for value in node.values():
            assert_value_free(value)
    elif isinstance(node, list):
        assert LIST_MARK.fullmatch(node[0]) and len(node) <= 2
        for value in node[1:]:
            assert_value_free(value)
    else:
        assert node in TYPE_NAMES, node


def input_tokens(node):
    """Every string key, string value and number in node, as text."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from input_tokens(value)
    elif isinstance(node, list):
        for value in node:
            yield from input_tokens(value)
    elif isinstance(node, str):
        yield node
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield str(node)


def test_scalars_become_their_type_names():
    assert [probe_report.skeleton(v) for v in ("thabo", 27821234567, 4.5, True, None)] == [
        "<str>", "<int>", "<float>", "<bool>", "<null>"]


def test_lists_keep_their_first_element_and_their_length():
    assert probe_report.skeleton([{"id": "a"}, {"id": "b", "url": "c"}]) == ["<list of 2>", {"id": "<str>"}]
    assert probe_report.skeleton([]) == ["<list of 0>"]


def test_vocabulary_keys_survive_and_one_other_key_collapses_the_dict():
    assert probe_report.skeleton({"id": "1", "views": 3, "ext": {"region": "ZA"}}) == {
        "id": "<str>", "views": "<int>", "ext": {"region": "<str>"}}
    assert probe_report.skeleton({"id": 1, "thabo_m": "x"}) == {"<key>": "<int>"}
    assert probe_report.skeleton({"users": {"thabo_m": {"id": "1"}, "lerato": {}}}) == {
        "<key>": {"<key>": {"id": "<str>"}}}


def test_vocabulary_is_the_field_list_of_the_probe_dump():
    vocab = probe_report.VOCABULARY
    assert len(vocab) == len(set(vocab)) == 272 + 7  # the dump's fields plus the envelope names it lacked
    for known in ("published_at_epoch", "hasPaidProductPlacement", "on_screen_texts", "vendor_labels", "items"):
        assert known in vocab
    for absent in ("users", "nick", "from", "artist", "status_text", "Region", "thabo_m"):
        assert absent not in vocab


ENVELOPE = ["cached", "credits_remaining", "credits_used", "data", "endpoint", "pagination", "has_more",
            "next_cursor", "page_size", "platform", "request_id", "success", "error", "message", "code"]


def test_envelope_of_a_live_shaped_body_keeps_its_keys_and_descends_into_data():
    body = {"success": True, "cached": False, "credits_used": 5, "credits_remaining": 248000,
            "endpoint": "/v1/tiktok/trending", "platform": "tiktok", "request_id": "req_9x",
            "pagination": {"has_more": True, "next_cursor": "24", "page_size": 24},
            "data": {"items": [{"post": {"id": "7401", "url": "https://www.tiktok.com/@a/video/7401",
                                         "ext": {"region": "ZA"}}}], "total": None}}
    assert probe_report.skeleton(body) == {
        "success": "<bool>", "cached": "<bool>", "credits_used": "<int>", "credits_remaining": "<int>",
        "endpoint": "<str>", "platform": "<str>", "request_id": "<str>",
        "pagination": {"has_more": "<bool>", "next_cursor": "<str>", "page_size": "<int>"},
        "data": {"items": ["<list of 1>", {"post": {"id": "<str>", "url": "<str>", "ext": {"region": "<str>"}}}],
                 "total": "<null>"}}
    failed = {"success": False, "error": {"code": "INSUFFICIENT_CREDITS", "message": "top up"}}
    assert probe_report.skeleton(failed) == {"success": "<bool>", "error": {"code": "<str>", "message": "<str>"}}
    assert set(ENVELOPE) <= set(probe_report.VOCABULARY)


ROUND1_CASES = [
    {"success": True, "credits_used": 5, "data": {"items": [{"post": {
        "id": "7401", "url": "https://www.tiktok.com/@dj.x/video/7401",
        "author": {"username": "dj.x", "display_name": "DJ X", "name": "Thabo M",
                   "avatar_url": "https://p16.example/a.jpg", "email": "dj@example.com",
                   "phone": "+27 82 555 0101", "followers": 5000},
        "content": {"text": "Call +27 82 555 0101 or mail dj@example.com, shout @friend https://x.co/a"},
        "engagement": {"views": 12000, "likes": 900}, "ext": {"region": "ZA"}}}] * 5, "has_more": True}},
    {"id": "7400000000000000001", "published_at": "2026-09-27T18:04:11Z", "caption": "12 000 views",
     "hashtag": {"name": "amapiano", "views": 3}},
]
ROUND2_EXTRA = [
    {"authorMeta": {"displayName": "Thabo Mokoena", "uniqueId": "thabo_m", "nickName": "Thabz",
                    "profilePicUrl": "https://cdn.example/p.jpg", "fans": 5000},
     "Screen-Name": "thabo_m2", "phoneNumber": 27825550101, "emailAddress": "t@example.com"},
    {"author": "thabo_m", "music": {"title": "Song", "author": "Lerato K"}, "users": ["thabo_m", "lerato_k"]},
    {"creators": [{"name": "Lerato Khumalo", "followers": 10}], "top_creators": [{"name": "Amaka Bello"}],
     "channel": {"title": "Kamau Vlogs", "subscribers": 7}, "location": {"name": "Soweto"}},
    {"caption": "WhatsApp 0825550101 or 08031234567, see instagram.com/thabo_m, mail thabo.m@example.co.za"},
]
ROUND4_CASES = [
    {"UserModule": {"users": {"thabo_m": {"uniqueId": "thabo_m"}, "kasi_vid": {"uniqueId": "kasi_vid"},
                              "rashid": {"nickname": "Rashid Bello"}, "mzansi_link": {}, "lerato_time": {},
                              "music": {}},
                    "stats": {"kasi_vid": {"followerCount": 5}, "khalid_count": 3}}},
    {"nick": {"david": 1}}, {"nick": {"david": {"id": "4"}}}, {"from": {"hamid": {"id": "1", "name": "Hamid"}}},
]
ROUND5_CASES = [
    {"data": {"uniqueId": "thabo_m", "nickname": "Thabo Mokoena", "signature": "call 0821234567 thabo@x.co.za",
              "region": "ZA", "followerCount": 1200, "createTime": 1600000000}},
    {"data": {"mentions_map": {"david": 1, "khalid": {"x": "y"}}, "counts_by_user": {"rashid": 5, "thabovid": 3}}},
    {"data": {"byUser": {"thabo": 1}, "leaders": {"majid": 3, "farid_count": 4, "partytime": "2024-01-01"}}},
    {"data": {"status": "Thabo", "type": "Mokoena", "kind": "lindiwe/sithole"}},
    {"music": {"artist": {"type": "Sipho", "birthDate": "1990-05-01", "joinedDate": "2020-01-01"}}},
    {"data": {"createTime": 8031234567, "updateTime": 2348031234567}},
    {"post": {"publishDate": "08031234567"}},
    {"stats": {"playCount": 27821234567}},
    {"author": {"followerCount": 27821234567}},
    {"business": {"reachCount": 27821234567, "phoneCount": 821234567}},
    {"place": {"phone": 27821234567, "website": "thabo.co.za", "address": "12 Main", "rating": 4.5}},
    {"hashtags": [{"name": "＠thabo"}, "@thabo"]},
    {"data": {"language": "Tom", "lang": "Ann-Marie", "region": "Jo"}},
    {"profile_url": "mailto:thabo@x.co.za", "avatarLarger": "https://x/y.jpg"},
    {"data": {"x0821234567id": 1}},
    {"data": {"createTime": "٨٠٣١٢٣٤٥٦٧"}},
    {"comments": [{"user": {"type": "Thabo", "status": "active", "joinTime": "2020-01-01",
                            "birthdayTime": 631152000}}]},
    {"data": {"phoneId": "0821234567", "contact_id": "thabo@x.co.za"}},
    {"articles": [{"byline": "By Thabo Mokoena", "source": {"name": "News24", "url": "https://news24.com"},
                   "author": "Thabo Mokoena"}]},
    {"data": {"platform": "thabo", "tiktok": {"handle": "thabo"}}},
    {"data": {"nick": {"david": "x"}, "from": {"hamid": 1}}},
    {"author": {"createTime": 8031234567}},
    {"items": [{"timestamp": 2547123456780}]},
    {"data": {"subtitle": "Thabo 0821234567", "headline": "call thabo", "bio": "x", "alt_text": "y",
              "textExtra": [{"userUniqueId": "thabo", "hashtagName": "t", "userId": "1"}]}},
    {"stats": {"playCount": "0821234567"}},
]
ACCOUNT_CASES = [{"email": "albert@x.com", "balance": 999}, {"tx": [{"api_key": "sk_live_abc"}]},
                 {"success": True, "data": {"balance": 248123}},
                 {"success": True, "data": {"items": [{"request_id": "req-77", "credits": 5}]}}]
ALL_REVIEW_CASES = (
    [("round1", b) for b in ROUND1_CASES] + [("round2", b) for b in ROUND2_EXTRA]
    + [("round2 " + label, b) for label, b, _ in REVIEW_CASES]
    + [("round3 " + name, b) for name, b in ROUND3_CASES.items()]
    + [("round4", b) for b in ROUND4_CASES] + [("round5", b) for b in ROUND5_CASES]
    + [("account", b) for b in ACCOUNT_CASES]
)


@pytest.mark.parametrize("label,body", ALL_REVIEW_CASES, ids=[f"{i:03d} {c[0]}" for i, c in enumerate(ALL_REVIEW_CASES)])
def test_skeleton_of_every_reviewer_case_holds_no_input_string_or_number(label, body, tmp_path):
    skel = probe_report.skeleton(body)
    assert_value_free(skel)
    written, withheld = probe_report.write_samples(
        [{"seed_key": "x", "route": "tiktok/trending", "body": body}], tmp_path)
    assert withheld == [] and len(written) == 1
    text = written[0].read_text(encoding="utf-8")
    allowed = set(probe_report.VOCABULARY) | TYPE_NAMES | {"<key>", "<list of", "probe", "route", "body",
                                                           "tiktok/trending"}
    for token in set(input_tokens(body)):
        if len(token) >= 3 and not any(token in word for word in allowed):
            assert token not in text, token


def test_gate_flags_anything_that_is_not_a_skeleton():
    for leak in ("call 0825550101", "see instagram.com/thabo_m", "hi @thabo_m", "t@example.com",
                 "https://tiktok.com/v/1", "call (082) 555-0101", "whatsapp 27 82 555 0101", "082/123/4567",
                 "082_123_4567", "082,123,4567", "thabo at gmail dot com", "＠thabo_m"):
        assert probe_report.residue({"x": leak}), leak
    assert probe_report.residue({"views": 5}) and probe_report.residue({"x": [1.5]})
    assert not probe_report.residue({"a": "<str>", "b": ["<list of 3>", {"id": "<null>", "<key>": "<bool>"}]})


def test_gate_refuses_a_file_that_still_holds_values(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_report, "skeleton", lambda value: value)
    raw = [raw_row(TT, "h1", "1-za", 200, {"success": True, "data": {"items": [
        {"post": {"id": "1", "content": {"text": "WhatsApp 0825550101"}}}]}})]
    written, withheld = probe_report.write_samples(raw, tmp_path)
    assert written == [] and withheld == ["1-za_tiktok-trending.json"]
    assert not (tmp_path / "1-za_tiktok-trending.json").exists()
    text = probe_report.render("r", [], {}, (0, 1, 1), missing=[], withheld=withheld)
    assert "Withheld by the redaction gate: 1-za_tiktok-trending.json." in text


def test_a_sample_holds_only_probe_route_and_skeleton(tmp_path):
    body = trending_body(["ZA", "US"], "za")
    written, _ = probe_report.write_samples([raw_row(TT, "h1", "1-za", 200, body)], tmp_path)
    sample = json.loads(written[0].read_text(encoding="utf-8"))
    assert sample == {"probe": "1-za", "route": TT, "body": probe_report.skeleton(body)}


def test_write_samples_names_files_by_probe_and_route(tmp_path):
    _, raw = report_fixture()
    written, withheld = probe_report.write_samples(raw, tmp_path)
    assert withheld == []
    names = [p.name for p in written]
    assert len(names) == 4 and set(names) == {"11a_tiktok-search-top.json", "11b_tiktok-search-top.json",
                                              "1-za_tiktok-trending.json", "5_search-news.json"}
    text = (tmp_path / "1-za_tiktok-trending.json").read_text(encoding="utf-8")
    assert "maker0" not in text and "ZA" not in text


def test_source_files_hold_no_banned_literals():
    for mod in (probe, probe_report):
        with open(mod.__file__, encoding="utf-8") as f:
            text = f.read()
        squashed = re.sub(r"[\s_./-]", "", text.lower())
        assert TRENDS not in squashed
        assert NO_AGE not in squashed
        assert not re.search(DASHES, text)


# probe.py --bodies: one live body per comment, transcript and parser route (L3 Needs 13) ----------------

BODY_ROUTES = [
    ("comments", "tiktok/post/comments", 1), ("comments", "instagram/post/comments", 5),
    ("comments", "youtube/video/comments", 1), ("comments", "reddit/post/comments", 5),
    ("comments", "twitter/tweet/replies", 1),
    ("transcripts", "youtube/video/transcript", 3), ("transcripts", "tiktok/post/transcript", 10),
    ("transcripts", "instagram/media/transcript", 10), ("transcripts", "twitter/tweet/transcript", 10),
    ("transcripts", "reddit/post/transcript", 10),
    ("parsers", "tiktok/search/hashtag", 1), ("parsers", "tiktok/profile/videos", 1),
    ("parsers", "instagram/audio/reels", 1),
]
URLS = {
    "tiktok": "https://www.tiktok.com/@kasi.keys/video/7400000000000000001",
    "instagram": "https://www.instagram.com/reel/C9xyzAbc/",
    "youtube": "https://www.youtube.com/watch?v=abcDEF12345",
    "reddit": "https://www.reddit.com/r/southafrica/comments/1abcde/load_shedding_again/",
    "twitter": "https://x.com/lagos_daily/status/1840000000000000001",
}


def post_row(platform, market="ZA", hashtags=("amapiano",), sound_id="ig_audio_77", url=None):
    return {"url": url or URLS[platform], "platform": platform, "geo_market": market, "hashtags": list(hashtags),
            "sound_id": sound_id}


class FakeQuery:
    """Stands in for the parameterised posts read; answers by the platforms parameter."""

    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows if rows is not None else {p: [post_row(p)] for p in URLS}

    def __call__(self, sql, params):
        self.calls.append((sql, dict(params)))
        return [dict(r) for p in params["platforms"] for r in self.rows.get(p, [])]


def test_bodies_plan_routes_groups_and_holds():
    calls, dropped = probe.bodies_plan()
    assert dropped == []
    assert [(p["group"], p["route"], probe.hold(p)) for p in calls] == BODY_ROUTES
    total = sum(probe.hold(p) for p in calls)
    assert total == 59 and total <= probe.BODIES_CAP == 70
    assert sum(probe.hold(p) for p in calls if p["group"] != "parsers") == 56
    ids = [p["id"] for p in calls]
    assert len(ids) == len(set(ids)) and not set(ids) & {p["id"] for p in probe.plan(TODAY)}
    for p in calls:
        assert p["route"] in PRICED and p["method"] == "GET"
        assert forbidden(p["route"], p["method"], dict(p["params"])) == ""
        assert TRENDS not in re.sub(r"[^a-z]", "", json.dumps([p["route"], p["params"]]).lower())


def test_bodies_plan_drops_an_unpriced_route(monkeypatch):
    monkeypatch.delitem(socialcrawl_client.PRICED, "reddit/post/transcript")
    calls, dropped = probe.bodies_plan()
    assert dropped == ["reddit/post/transcript"]
    assert "reddit/post/transcript" not in [p["route"] for p in calls] and len(calls) == 12


def test_bodies_plan_flag_prints_the_plan_without_network(monkeypatch):
    monkeypatch.setattr(probe, "live_client", lambda *a, **k: pytest.fail("--plan must not build a client"))
    monkeypatch.setattr(probe, "posts_query", lambda: pytest.fail("--plan must not read posts"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--bodies", "--plan"], today=TODAY) == 0
    text = buf.getvalue()
    for group, route, _ in BODY_ROUTES:
        assert route in text and group in text
    assert "total hold 59" in text and "cap 70" in text
    for group, total in (("comments", 13), ("transcripts", 43), ("parsers", 3)):
        assert f"{group}: {total} credits" in text


def test_posts_read_is_parameterised_one_per_route():
    query = FakeQuery()
    calls, _ = probe.bodies_plan()
    for p in calls:
        probe.candidates(p, TODAY, query)
    assert len(query.calls) == len(calls) == 13
    for (sql, params), p in zip(query.calls, calls):
        assert sql == probe.POSTS_SQL
        assert params["markets"] == ["ZA", "NG", "KE"] and p["platform"] in params["platforms"]
        assert params["since"] == date(2026, 9, 22) and params["today"] == TODAY
        assert params["video"] is (p["group"] == "transcripts")
        assert params["sound"] is (p["route"] == "instagram/audio/reels")
        assert params["hashtag"] is (p["route"] == "tiktok/search/hashtag")
    for token in ("@platforms", "@markets", "@since", "@today", "@video", "@video_url", "@sound", "@hashtag"):
        assert token in probe.POSTS_SQL
    for literal in ("'ZA'", "'tiktok'", "2026-", '"ZA"'):
        assert literal not in probe.POSTS_SQL
    assert "intelligence_42_core.posts" in probe.POSTS_SQL
    twitter = next(p for p in calls if p["route"] == "twitter/tweet/replies")
    probe.candidates(twitter, TODAY, query)
    assert set(query.calls[-1][1]["platforms"]) == {"twitter", "x"}


def test_pick_fills_each_route_from_a_real_post():
    calls = {p["route"]: p for p in probe.bodies_plan()[0]}
    rows = [post_row("tiktok", "NG")]
    assert probe.pick(calls["tiktok/post/comments"], rows) == ({"url": URLS["tiktok"]}, "NG", "")
    assert probe.pick(calls["tiktok/profile/videos"], rows) == ({"handle": "kasi.keys"}, "NG", "")
    assert probe.pick(calls["tiktok/search/hashtag"], rows) == ({"hashtag": "amapiano", "region": "NG"}, "NG", "")
    ig = [post_row("instagram", "KE")]
    assert probe.pick(calls["instagram/audio/reels"], ig) == ({"audio_id": "ig_audio_77"}, "KE", "")
    assert probe.pick(calls["twitter/tweet/transcript"], [post_row("twitter")]) == ({"url": URLS["twitter"]}, "ZA", "")
    params, market, why = probe.pick(calls["reddit/post/comments"], [])
    assert params is None and market is None and "no reddit post" in why


def test_pick_keeps_rule_1_and_generic_tags_out_of_the_request():
    calls = {p["route"]: p for p in probe.bodies_plan()[0]}
    age_tag = NO_AGE + "vibes"
    rows = [post_row("tiktok", hashtags=[age_tag, "fyp"]), post_row("tiktok", "KE", hashtags=["#Amapiano"])]
    assert probe.pick(calls["tiktok/search/hashtag"], rows) == ({"hashtag": "amapiano", "region": "KE"}, "KE", "")
    aged = post_row("tiktok", url=f"https://www.tiktok.com/@{NO_AGE}_daily/video/7400000000000000002")
    assert probe.pick(calls["tiktok/profile/videos"], [aged]) == (None, None, "no tiktok post passed rule 1")
    assert probe.pick(calls["tiktok/post/comments"], [aged])[0] is None


def body_http(credits=None):
    """A SocialCrawl stand-in: every route answers one comment-shaped row; credits overrides credits_used."""
    seen = []

    def http(method, url, *, params=None, json=None, headers=None, timeout=None):
        route = url.split("/v1/", 1)[1]
        seen.append(route)
        if route == "credits/balance":
            return 200, {"success": True, "data": {"balance": 250000}}, {}
        used = (credits or {}).get(route, quote_for(route, method, params))
        return 200, {"success": True, "credits_used": used,
                     "data": {"items": [{"comment": {"id": "c1", "text": "fire"}}]}}, {}
    http.seen = seen
    return http


def body_client(http, ledger=None):
    return SocialCrawlClient(share="build", run_id="probe-bodies-test", mode="live",
                             ledger=ledger if ledger is not None else MemoryLedgerStore(), raw=MemoryRawStore(),
                             http=http, clock=lambda: NOW, schedule_started=True)


def test_bodies_run_books_every_call_on_the_build_share_with_no_lane(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "sc-test-sentinel-9a")
    http = body_http()
    client = body_client(http)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--bodies"], client=client, clock=lambda: NOW, query=FakeQuery()) == 0
    calls, _ = probe.bodies_plan()
    assert [r for r in http.seen if r != "credits/balance"] == [p["route"] for p in calls]
    paid = [r for r in client.ledger.rows if r["route"] != "credits/balance"]
    assert len(paid) == 13 and all(r["job"] == "build" and r["lane"] is None for r in paid)
    assert all(r["job"] == "build" for r in client.ledger.rows)
    assert sum(r["credits_charged"] for r in paid) == 59
    assert [r["seed_key"] for r in client.raw.rows] == [p["id"] for p in calls]
    text = buf.getvalue()
    assert "sc-test-sentinel-9a" not in text and "kasi.keys" not in text and "tiktok.com" not in text
    assert "59 credits charged" in text


def test_bodies_run_stops_before_the_call_that_would_cross_70(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "sc-test-sentinel-9a")
    client = body_client(body_http(credits={"instagram/post/comments": 40}))
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.main(["--bodies"], client=client, clock=lambda: NOW, query=FakeQuery())
    charged = [r["credits_charged"] for r in client.ledger.rows if r["route"] != "credits/balance"]
    # 1 + 40 + 1 + 5 + 1 + 3 + 10 = 61; the next hold of 10 would make 71.
    assert charged == [1, 40, 1, 5, 1, 3, 10] and sum(charged) <= probe.BODIES_CAP
    assert "stopped before t3" in buf.getvalue() and "cap of 70" in buf.getvalue()


def test_bodies_run_meets_the_build_share_cap_too(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "sc-test-sentinel-9a")
    ledger = MemoryLedgerStore()
    ledger.append({"trend_date": TODAY.isoformat(), "job": "build", "lane": None, "credits_charged": 140,
                   "cache_hit": False})
    client = body_client(body_http(), ledger)
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.main(["--bodies"], client=client, clock=lambda: NOW, query=FakeQuery())
    assert "cap_reached" in buf.getvalue() and "stopped after c4" in buf.getvalue()
    assert sum(r["credits_charged"] for r in ledger.rows) == 147


@pytest.mark.parametrize("mode,first_id", [("bodies", "c1"), ("ig", "ig-za-0")])
@pytest.mark.parametrize("status", ["error", "refunded", "forbidden", "not_in_replay",
                                     "insufficient_credits", "balance_floor", "cap_reached"])
def test_paid_probes_stop_after_any_final_failure(mode, first_id, status):
    client = FakeClient(stop_at=0, stop_status=status)
    buf = io.StringIO()
    with redirect_stdout(buf):
        if mode == "bodies":
            calls, _ = probe.bodies_plan()
            assert probe.run_bodies(client, calls[:2], TODAY, FakeQuery(), "test") == 0
        else:
            calls, _ = probe.ig_plan()
            assert probe.run_ig_locations(client, calls[:2], "test") == 0
    assert len(client.calls) == 1
    assert f"stopped after {first_id}: {status}" in buf.getvalue()


@pytest.mark.parametrize("mode", ["bodies", "ig"])
@pytest.mark.parametrize("status", ["empty", "cached"])
def test_paid_probes_continue_after_an_empty_or_cached_result(mode, status):
    class EmptyThenErrorClient:
        def __init__(self):
            self.calls = []

        def call(self, route, params=None, **kw):
            self.calls.append({"route": route, "params": params, **kw})
            result_status = status if len(self.calls) == 1 else "error"
            return Result(result_status, route)

    client = EmptyThenErrorClient()
    buf = io.StringIO()
    with redirect_stdout(buf):
        if mode == "bodies":
            calls, _ = probe.bodies_plan()
            assert probe.run_bodies(client, calls[:2], TODAY, FakeQuery(), "test") == 0
        else:
            calls, _ = probe.ig_plan()
            assert probe.run_ig_locations(client, calls[:2], "test") == 0
    assert len(client.calls) == 2
    assert status in buf.getvalue()
    assert "stopped after" in buf.getvalue() and "error" in buf.getvalue()


def test_bodies_live_client_uses_the_build_share_after_the_schedule(monkeypatch):
    seen = {}
    monkeypatch.setattr(probe, "live_client", lambda run_id, **kw: seen.update(run_id=run_id, **kw) or FakeClient())
    with redirect_stdout(io.StringIO()):
        probe.main(["--bodies"], clock=lambda: NOW, query=FakeQuery())
    assert seen == {"run_id": "probe-bodies-20260929T040000Z", "schedule_started": True}


@pytest.mark.parametrize("status", ["insufficient_credits", "balance_floor", "cap_reached"])
def test_bodies_run_stops_on_a_stop_status(status):
    client = FakeClient(stop_at=2, stop_status=status)
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.main(["--bodies"], client=client, clock=lambda: NOW, query=FakeQuery())
    assert len(client.calls) == 3 and "stopped after c3" in buf.getvalue()
    assert all(c["lane"] is None for c in client.calls)


def test_bodies_run_skips_a_route_with_no_post_and_exits_0_on_a_failure():
    client = FakeClient()
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--bodies"], client=client, clock=lambda: NOW,
                          query=FakeQuery({"tiktok": [post_row("tiktok")]})) == 0
    assert {c["route"].split("/")[0] for c in client.calls} == {"tiktok"} and len(client.calls) == 4
    assert buf.getvalue().count("skipped") == 9

    def boom(sql, params):
        raise RuntimeError("bigquery down")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--bodies"], client=FakeClient(), clock=lambda: NOW, query=boom) == 0
    assert "stopped: RuntimeError" in buf.getvalue()


# probe.py --routes: one body for each named route, to confirm the Ask prices (L3 Needs 5, L4 Needs 25) --------

PRICE_ROUTES = [
    ("details", "tiktok/post", 1), ("details", "instagram/post", 1), ("details", "youtube/video", 1),
    ("details", "twitter/tweet", 1), ("details", "reddit/post", 1), ("details", "facebook/post", 1),
    ("details", "threads/post", 1), ("ask", "instagram/tagged", 5), ("ask", "prism/mentions", 7),
]
PRICE_NAMES = [route for _, route, _ in PRICE_ROUTES]
DETAIL_URLS = dict(URLS, facebook="https://www.facebook.com/lagoscity/posts/pfbid0abc",
                   threads="https://www.threads.net/@kasi.keys/post/C9abc")
HUBS_YAML = """markets:
  za:
    culture_desk:
      - {handle: bafana_tiktok, platform: tiktok}
      - {handle: AGE_daily, platform: instagram}
      - {handle: tyla, platform: instagram}
  ng:
    culture_desk:
      - {handle: wizkidayo, platform: instagram}
""".replace("AGE", NO_AGE)


@pytest.fixture
def hubs(tmp_path, monkeypatch):
    path = tmp_path / "hubs.yaml"
    path.write_text(HUBS_YAML, encoding="utf-8")
    monkeypatch.setattr(probe, "HUBS", path)
    return path


def test_routes_plan_holds_each_named_route_at_its_priced_value():
    calls, dropped = probe.bodies_plan(PRICE_NAMES)
    assert dropped == []
    assert [(p["group"], p["route"], probe.hold(p)) for p in calls] == PRICE_ROUTES
    assert sum(probe.hold(p) for p in calls) == 19 <= probe.BODIES_CAP
    ids = [p["id"] for p in calls]
    default_ids = {p["id"] for p in probe.bodies_plan()[0]}
    assert len(ids) == len(set(ids)) and not set(ids) & (default_ids | {p["id"] for p in probe.plan(TODAY)})
    for p in calls:
        assert p["method"] == "GET" and forbidden(p["route"], p["method"], dict(p["params"])) == ""
    mentions = next(p for p in calls if p["route"] == "prism/mentions")
    assert set(mentions["params"]["platforms"].split(",")) == {"twitter", "reddit", "instagram"}


def test_routes_plan_can_name_a_default_body_route_and_keeps_table_order():
    calls, _ = probe.bodies_plan(["prism/mentions", "tiktok/post/comments"])
    assert [p["id"] for p in calls] == ["c1", "a2"]


def test_routes_plan_drops_an_unpriced_route(monkeypatch):
    monkeypatch.delitem(socialcrawl_client.PRICED, "threads/post")
    calls, dropped = probe.bodies_plan(PRICE_NAMES)
    assert dropped == ["threads/post"] and len(calls) == 8


def test_routes_flag_plan_prints_only_the_named_routes_without_network(monkeypatch):
    monkeypatch.setattr(probe, "live_client", lambda *a, **k: pytest.fail("--plan must not build a client"))
    monkeypatch.setattr(probe, "posts_query", lambda: pytest.fail("--plan must not read posts"))
    monkeypatch.setattr(probe, "hub_rows", lambda: pytest.fail("--plan must not read hubs"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--bodies", "--routes", ",".join(PRICE_NAMES), "--plan"], today=TODAY) == 0
    text = buf.getvalue()
    for group, route, hold in PRICE_ROUTES:
        assert re.search(rf"{group} +{re.escape(route)} +{hold} ", text), route
    assert "details: 7 credits" in text and "ask: 12 credits" in text
    assert "9 calls, total hold 19 credits, cap 70" in text
    assert "comments" not in text and "transcript" not in text
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--routes", "tiktok/post,prism/mentions", "--plan"], today=TODAY) == 0
    assert "2 calls, total hold 8 credits, cap 70" in buf.getvalue()


def test_routes_flag_refuses_a_route_it_does_not_know(monkeypatch):
    monkeypatch.setattr(probe, "live_client", lambda *a, **k: pytest.fail("no client for a refused list"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--bodies", "--routes", "tiktok/post,prism/earliness"], today=TODAY) == 2
    assert "not a body route: prism/earliness" in buf.getvalue()


def test_hub_handles_come_from_the_instagram_culture_desk_and_keep_rule_1(hubs):
    calls = {p["route"]: p for p in probe.bodies_plan(PRICE_NAMES)[0]}
    query = FakeQuery()
    rows = probe.candidates(calls["instagram/tagged"], TODAY, query)
    assert query.calls == []
    assert [r["handle"] for r in rows] == [NO_AGE + "_daily", "tyla", "wizkidayo"]
    assert probe.pick(calls["instagram/tagged"], rows) == ({"handle": "tyla"}, "ZA", "")
    assert probe.pick(calls["prism/mentions"], rows) == (
        {"handle": "tyla", "platforms": "twitter,reddit,instagram"}, "ZA", "")
    assert probe.pick(calls["instagram/tagged"], rows[:1]) == (None, None, "no instagram post passed rule 1")


def test_routes_run_books_one_body_each_on_the_build_share_within_the_cap(monkeypatch, hubs):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "sc-test-sentinel-9a")
    http = body_http()
    client = body_client(http)
    query = FakeQuery({p: [post_row(p, url=u)] for p, u in DETAIL_URLS.items()})
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--bodies", "--routes", ",".join(PRICE_NAMES)], client=client, clock=lambda: NOW,
                          query=query) == 0
    assert [r for r in http.seen if r != "credits/balance"] == PRICE_NAMES
    paid = [r for r in client.ledger.rows if r["route"] != "credits/balance"]
    assert [r["credits_charged"] for r in paid] == [h for _, _, h in PRICE_ROUTES]
    assert all(r["job"] == "build" and r["lane"] is None for r in client.ledger.rows)
    assert [r["seed_key"] for r in client.raw.rows] == [p["id"] for p in probe.bodies_plan(PRICE_NAMES)[0]]
    text = buf.getvalue()
    assert "held=7 charged=7" in text and "19 credits charged" in text
    for leak in ("sc-test-sentinel-9a", "tyla", "kasi.keys", "facebook.com", "threads.net"):
        assert leak not in text, leak


def test_routes_run_stops_before_the_call_that_would_cross_70(monkeypatch, hubs):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "sc-test-sentinel-9a")
    client = body_client(body_http(credits={"reddit/post": 60}))
    query = FakeQuery({p: [post_row(p, url=u)] for p, u in DETAIL_URLS.items()})
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.main(["--routes", ",".join(PRICE_NAMES)], client=client, clock=lambda: NOW, query=query)
    charged = [r["credits_charged"] for r in client.ledger.rows if r["route"] != "credits/balance"]
    # 1 + 1 + 1 + 1 + 60 + 1 + 1 = 66; instagram/tagged would hold 5 more.
    assert charged == [1, 1, 1, 1, 60, 1, 1] and "stopped before a1" in buf.getvalue()


# probe_report.py --bodies: redacted fixtures and the report section ------------------------------------

COMMENT_BODY = {
    "success": True, "credits_used": 1, "request_id": "req_abc123",
    "pagination": {"has_more": True, "next_cursor": "cursor_zz9"},
    "data": {"items": [
        {"comment": {"comment_id": "7412000000000000001", "text": "Thabo Mokoena you killed it, call 0821234567",
                     "created_at": "2026-09-27T18:04:11Z", "like_count": 12, "reply_count": 3,
                     "author": {"username": "thabo_m", "display_name": "Thabo Mokoena",
                                "avatar_url": "https://p16.example/a.jpg", "profile_url": "https://tiktok.com/@thabo_m"},
                     "mentions": ["@lerato_k"], "is_pinned": False, "parent_id": None},
         "computed": {"labels": {"sentiment": "positive", "question": False}}},
        {"comment": {"comment_id": "7412000000000000002", "text": "mail lerato@example.co.za",
                     "author": {"username": "lerato_k"}}},
        {"comment": {"comment_id": "c3", "text": "yebo"}},
        {"comment": {"comment_id": "c4", "text": "sho"}},
    ]},
}
TRANSCRIPT_BODY = {
    "success": True, "credits_used": 10,
    "data": {"language": "en", "video_id": "abcDEF12345", "url": "https://www.youtube.com/watch?v=abcDEF12345",
             "segments": [{"start": "0.00", "end": "4.20", "text": "Welcome back Thabo"},
                          {"start": 4.2, "end": 9.8, "text": "subscribe to thabo_m", "offset": "00:04.20"}],
             "transcript": "Welcome back Thabo subscribe to thabo_m"},
}


def test_redaction_keeps_field_names_and_types_and_no_value():
    red = probe_report.redact(COMMENT_BODY)
    items = red["data"]["items"]
    assert items[0] == "<list of 4>" and len(items) == 1 + probe_report.LIST_CAP
    comment = items[1]["comment"]
    assert set(comment) == {"comment_id", "text", "created_at", "like_count", "reply_count", "author", "mentions",
                            "is_pinned", "parent_id"}
    assert comment["created_at"] == "<str date>" and comment["like_count"] == "<int>"
    assert comment["comment_id"] == "<str number>" and comment["text"] == "<str>"
    assert comment["author"] == {"username": "<str>", "display_name": "<str>", "avatar_url": "<str url>",
                                 "profile_url": "<str url>"}
    assert comment["mentions"] == ["<list of 1>", "<str>"]
    assert comment["is_pinned"] == "<bool>" and comment["parent_id"] == "<null>"
    assert items[1]["computed"]["labels"] == {"sentiment": "<str>", "question": "<bool>"}
    seg = probe_report.redact(TRANSCRIPT_BODY)["data"]["segments"]
    assert seg[0] == "<list of 2>" and seg[1]["start"] == "<str number>" and seg[2]["start"] == "<float>"
    assert seg[2]["offset"] == "<str time>"
    assert probe_report.redact({"users": {"thabo.m": {"id": "1"}}}) == {"users": {"<key>": {"id": "<str number>"}}}
    assert probe_report.redact({"x": {"127": 1}}) == {"x": {"<key>": "<int>"}}


def test_body_fixture_holds_no_text_handle_id_or_url(tmp_path):
    raw = [raw_row("tiktok/post/comments", "h1", "c1", 200, COMMENT_BODY),
           raw_row("youtube/video/transcript", "h2", "t1", 200, TRANSCRIPT_BODY),
           raw_row("tiktok/trending", "h3", "1-za", 200, trending_body(["ZA"], "za"))]
    path = tmp_path / "socialcrawl_bodies.json"
    written, withheld = probe_report.write_bodies(raw, path, run_id="probe-bodies-20260929T060000Z")
    assert withheld == [] and written == ["tiktok/post/comments", "youtube/video/transcript"]
    text = path.read_text(encoding="utf-8")
    fixture = json.loads(text)
    assert set(fixture) == {"_note", "tiktok/post/comments", "youtube/video/transcript"}
    assert fixture["tiktok/post/comments"]["probe"] == "c1"
    assert not probe_report.residue(fixture)
    for body in (COMMENT_BODY, TRANSCRIPT_BODY):
        for token in set(input_tokens(body)):
            if len(token) >= 3 and not re.fullmatch(r"[A-Za-z_]+", token):
                assert token not in text, token
    for leak in ("Thabo", "thabo", "lerato", "Mokoena", "yebo", "fire", "positive", "abcDEF", "example",
                 "7412", "0821234567", "req_abc", "cursor_zz", "Welcome", "http"):
        assert leak not in text, leak
    for field in ("comment_id", "reply_count", "display_name", "segments", "start", "offset", "transcript"):
        assert f'"{field}"' in text


def test_body_fixture_withholds_what_the_gate_catches_and_merges_reruns(tmp_path, monkeypatch):
    path = tmp_path / "socialcrawl_bodies.json"
    probe_report.write_bodies([raw_row("tiktok/post/comments", "h1", "c1", 200, COMMENT_BODY)], path, run_id="r1")
    monkeypatch.setattr(probe_report, "redact", lambda value: value)
    written, withheld = probe_report.write_bodies(
        [raw_row("reddit/post/comments", "h2", "c4", 200, {"text": "WhatsApp 0825550101"})], path, run_id="r2")
    assert written == [] and withheld == ["reddit/post/comments"]
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert "tiktok/post/comments" in fixture and "reddit/post/comments" not in fixture


def test_bodies_report_section_lists_credits_per_route_by_group():
    ledger = [ledger_row("tiktok/post/comments", "h1", 1, when="2026-09-29T06:00:01+00:00"),
              ledger_row("youtube/video/transcript", "h2", 3, when="2026-09-29T06:00:02+00:00"),
              ledger_row("instagram/audio/reels", "h3", 1, when="2026-09-29T06:00:03+00:00")]
    raw = [raw_row("tiktok/post/comments", "h1", "c1", 200, COMMENT_BODY, "2026-09-29T06:00:01+00:00"),
           raw_row("youtube/video/transcript", "h2", "t1", 200, TRANSCRIPT_BODY, "2026-09-29T06:00:02+00:00"),
           raw_row("instagram/audio/reels", "h3", "p3", 200, {"success": True, "data": {"items": []}},
                   "2026-09-29T06:00:03+00:00")]
    section = probe_report.render_bodies("probe-bodies-r", probe_report.table(ledger, raw), withheld=["x/y"],
                                         new_keys=["comment_id", "segments"])
    comments_rows = len(probe_report.rows_of(COMMENT_BODY))
    transcript_rows = len(probe_report.rows_of(TRANSCRIPT_BODY))
    assert section.startswith(probe_report.BODIES_HEADING)
    assert f"| c1 | comments | tiktok/post/comments | 200 | 1 | 1 | {comments_rows} |" in section
    assert f"| t1 | transcripts | youtube/video/transcript | 200 | 3 | 10 | {transcript_rows} |" in section
    assert "| p3 | parsers | instagram/audio/reels | 200 | 1 | n/a | 0 |" in section
    assert "Comments: 1 credits. Transcripts: 3 credits. Parsers: 1 credits. Total: 5 credits." in section
    assert "c2" in section and "Withheld by the redaction gate: x/y." in section
    assert "comment_id, segments" in section
    assert not re.search(DASHES, section)
    old = "# SocialCrawl probe report\n\nStage 0 table\n\n" + probe_report.BODIES_HEADING + "\n\nold run\n"
    merged = probe_report.merge_section(old, section)
    assert merged.startswith("# SocialCrawl probe report\n\nStage 0 table\n\n") and "old run" not in merged
    assert merged.count(probe_report.BODIES_HEADING) == 1
    assert probe_report.merge_section("", section) == section


def test_bodies_report_writes_and_totals_a_routes_run(tmp_path):
    detail = {"success": True, "credits_used": 1, "data": {"post": {"id": "7400000000000000001", "caption": "hi"}}}
    mentions = {"success": True, "credits_used": 2, "data": {"items": [{"platform": "x", "text": "@tyla fire"}]}}
    raw = [raw_row("tiktok/post", "h1", "d1", 200, detail, "2026-09-29T06:00:01+00:00"),
           raw_row("prism/mentions", "h2", "a2", 200, mentions, "2026-09-29T06:00:02+00:00")]
    ledger = [ledger_row("tiktok/post", "h1", 1, when="2026-09-29T06:00:01+00:00"),
              ledger_row("prism/mentions", "h2", 2, when="2026-09-29T06:00:02+00:00")]
    path = tmp_path / "socialcrawl_bodies.json"
    written, withheld = probe_report.write_bodies(raw, path, run_id="probe-bodies-r")
    assert written == ["tiktok/post", "prism/mentions"] and withheld == []
    text = path.read_text(encoding="utf-8")
    assert "tyla" not in text and "7400" not in text
    section = probe_report.render_bodies("probe-bodies-r", probe_report.table(ledger, raw))
    assert "| d1 | details | tiktok/post | 200 | 1 | 1 | 0 |" in section
    assert "| a2 | ask | prism/mentions | 200 | 2 | 2 | 1 |" in section
    assert "Details: 1 credits. Ask: 2 credits. Total: 3 credits." in section
    missing = section.split("Not called (skipped or stopped): ")[1].split(".")[0].split(", ")
    assert missing == ["d2", "d3", "d4", "d5", "d6", "d7", "a1"]


# probe.py --ig-locations: Instagram location ids for core/config/markets.yaml -----------------------------


def ig_row(pk, name, lat, lng, fb=None):
    """One instagram/search/location item in the recorded probe shape (probe-20260928T191308Z, 8a)."""
    return {"location": {"address": "", "city": None, "external_source": "facebook_places",
                         "facebook_places_id": fb or pk, "has_viewer_saved": False, "lat": lat, "lng": lng,
                         "name": name, "pk": pk, "short_name": name},
            "subtitle": "", "title": name, "vendor_labels": {}}


def ig_body(rows, credits=5):
    return {"success": True, "cached": False, "credits_used": credits, "data": {"dropped": 0, "items": rows}}


SOWETO = [
    ig_row(320191774775153, "Soweto Diepkloof Zone 2", -26.264811666667, 28.40297),
    ig_row(405474552946348, "Soweto Organizacao NEGRA", -23.55197, -46.63079),       # Brazil
    ig_row(137545546357317, "Soweto Orlando West", -26.20126, 27.91991),
    ig_row(566386939, "Soweto Theatre", -26.24918563, 27.85977797, fb=415880431767671),
    ig_row(456490771769090, "Soweto Kahawa West", -1.18333, 36.9167),                 # Kenya
    ig_row(274414569384413, "Soweto South Africa", -33.91438103, 151.17794208),      # Australia
    ig_row(137545546357317, "Soweto Orlando West", -26.20126, 27.91991),             # a repeat
]
CITY_ROWS = {
    "Johannesburg": [ig_row(111, "Johannesburg, South Africa", -26.2041, 28.0473)],
    "Cape Town": [ig_row(112, "Cape Town, Western Cape", -33.9249, 18.4241)],
    "Durban": [ig_row(113, "Durban, KwaZulu Natal", -29.8587, 31.0218),
               ig_row(114, "Durban, Guyana", 6.8, -58.15)],
    "Lagos": [ig_row(211, "Lagos, Nigeria", 6.5244, 3.3792), ig_row(212, "Lagos, Portugal", 37.1028, -8.6730)],
    "Abuja": [ig_row(213, "Abuja, Nigeria", 9.0765, 7.3986)],
    "Port Harcourt": [ig_row(214, "Port Harcourt", 4.8156, 7.0498)],
    "Nairobi": [ig_row(311, "Nairobi, Kenya", -1.2921, 36.8219)],
    "Mombasa": [ig_row(312, "Mombasa", -4.0435, 39.6682)],
    "Kisumu": [ig_row(313, "Kisumu", -0.0917, 34.7680), ig_row(314, "Kisumu Street, Lagos", 6.45, 3.40)],
}


def test_ig_plan_searches_three_cities_a_market_for_45_credits():
    calls, dropped = probe.ig_plan()
    assert dropped == []
    assert [(p["market"], p["params"]["query"]) for p in calls] == [
        ("ZA", "Johannesburg"), ("ZA", "Cape Town"), ("ZA", "Durban"),
        ("NG", "Lagos"), ("NG", "Abuja"), ("NG", "Port Harcourt"),
        ("KE", "Nairobi"), ("KE", "Mombasa"), ("KE", "Kisumu")]
    assert all(p["route"] == "instagram/search/location" and p["method"] == "GET" for p in calls)
    assert all(forbidden(p["route"], p["method"], dict(p["params"])) == "" for p in calls)
    assert [probe.hold(p) for p in calls] == [5] * 9 and sum(probe.hold(p) for p in calls) == probe.IG_CAP == 45
    ids = [p["id"] for p in calls]
    assert len(ids) == len(set(ids)) and not set(ids) & {p["id"] for p in probe.plan(TODAY)}


def test_ig_plan_drops_a_query_rule_1_blocks(monkeypatch):
    monkeypatch.setattr(probe, "IG_CITIES", {"ZA": ("Durban", NO_AGE + " Durban"), "NG": ("Lagos",), "KE": ()})
    calls, dropped = probe.ig_plan()
    assert [p["params"]["query"] for p in calls] == ["Durban", "Lagos"]
    assert dropped == [("ZA", NO_AGE + " Durban")]


@pytest.mark.parametrize("market,lat,lng,inside", [
    ("ZA", -26.26, 28.40, True), ("ZA", -34.83, 20.0, True), ("ZA", -23.55, -46.63, False),
    ("ZA", -1.18, 36.92, False), ("ZA", -33.91, 151.18, False),
    ("NG", 6.52, 3.38, True), ("NG", 13.5, 13.0, True), ("NG", 37.1, -8.67, False), ("NG", -26.2, 28.0, False),
    ("KE", -1.29, 36.82, True), ("KE", -4.04, 39.67, True), ("KE", 6.45, 3.40, False), ("KE", -26.2, 28.0, False),
])
def test_market_boxes_hold_the_country_and_nothing_far_outside(market, lat, lng, inside):
    assert probe.in_market(market, lat, lng) is inside


def test_ig_candidates_take_the_id_derive_passed_and_drop_other_countries():
    found = probe.ig_candidates("ZA", SOWETO)
    assert [c["id"] for c in found] == ["320191774775153", "137545546357317", "566386939"]
    assert found[0] == {"id": "320191774775153", "name": "Soweto Diepkloof Zone 2", "lat": -26.264811666667,
                        "lng": 28.40297}
    # derive() gave instagram/location/posts the pk of the first result: the same id comes first here.
    params, _ = probe.derive({"params": {}, "derive": {"location_id": "8a"}},
                             {"8a": Result("ok", "instagram/search/location", items=SOWETO)})
    assert params["location_id"] == found[0]["id"]
    assert probe.ig_candidates("KE", SOWETO)[0]["name"] == "Soweto Kahawa West"
    assert probe.ig_candidates("NG", SOWETO) == []


def test_ig_candidates_skip_rows_without_coordinates_an_id_or_a_clean_name():
    rows = [{"location": {"pk": 1, "name": "No coords"}}, {"location": {"lat": -26.2, "lng": 28.0, "name": "No id"}},
            ig_row(2, NO_AGE + " Soweto", -26.2, 28.0), ig_row(3, "Soweto", "-26.2", "28.0"), "junk"]
    assert [c["id"] for c in probe.ig_candidates("ZA", rows)] == ["3"]


def test_ig_locations_plan_flag_prints_without_network(monkeypatch):
    monkeypatch.setattr(probe, "live_client", lambda *a, **k: pytest.fail("--plan must not build a client"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--ig-locations", "--plan"], today=TODAY) == 0
    text = buf.getvalue()
    for city in ("Johannesburg", "Port Harcourt", "Kisumu"):
        assert city in text
    assert "9 calls, total hold 45 credits, cap 45" in text


def ig_http(credits=None):
    """instagram/search/location answered from CITY_ROWS by query; credits overrides credits_used."""
    seen = []

    def http(method, url, *, params=None, json=None, headers=None, timeout=None):
        route = url.split("/v1/", 1)[1]
        seen.append((route, (params or {}).get("query")))
        if route == "credits/balance":
            return 200, {"success": True, "data": {"balance": 250000}}, {}
        return 200, ig_body(CITY_ROWS[params["query"]], (credits or {}).get(params["query"], 5)), {}
    http.seen = seen
    return http


def test_ig_locations_run_books_nine_searches_on_the_build_share_and_prints_candidates(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "sc-test-sentinel-9a")
    http = ig_http()
    client = body_client(http)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--ig-locations"], client=client, clock=lambda: NOW) == 0
    assert [q for r, q in http.seen if r != "credits/balance"] == [
        "Johannesburg", "Cape Town", "Durban", "Lagos", "Abuja", "Port Harcourt", "Nairobi", "Mombasa", "Kisumu"]
    paid = [r for r in client.ledger.rows if r["route"] != "credits/balance"]
    assert len(paid) == 9 and sum(r["credits_charged"] for r in paid) == 45
    assert all(r["job"] == "build" and r["lane"] is None for r in client.ledger.rows)
    assert [r["market"] for r in paid] == ["ZA"] * 3 + ["NG"] * 3 + ["KE"] * 3
    text = buf.getvalue()
    assert "done: 9 calls, 45 credits charged" in text
    assert "ZA 113 Durban, KwaZulu Natal -29.8587 31.0218" in text
    assert "NG 211 Lagos, Nigeria 6.5244 3.3792" in text and "KE 313 Kisumu -0.0917 34.768" in text
    for outside in ("Guyana", "Portugal", "Kisumu Street"):
        assert outside not in text
    assert "sc-test-sentinel-9a" not in text


def test_ig_locations_run_stops_before_the_call_that_would_cross_45(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "sc-test-sentinel-9a")
    client = body_client(ig_http(credits={"Johannesburg": 20}))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--ig-locations"], client=client, clock=lambda: NOW) == 0
    charged = [r["credits_charged"] for r in client.ledger.rows if r["route"] != "credits/balance"]
    # 20 + 5 x 4 = 40; the next hold of 5 lands on 45, the one after would make 50.
    assert charged == [20, 5, 5, 5, 5, 5] and sum(charged) <= probe.IG_CAP
    assert "stopped before ig-ke-0" in buf.getvalue() and "cap of 45" in buf.getvalue()


def test_ig_locations_run_meets_the_build_share_cap_too(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "sc-test-sentinel-9a")
    ledger = MemoryLedgerStore()
    ledger.append({"trend_date": TODAY.isoformat(), "job": "build", "lane": None, "credits_charged": 140,
                   "cache_hit": False})
    client = body_client(ig_http(), ledger)
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.main(["--ig-locations"], client=client, clock=lambda: NOW)
    assert "cap_reached" in buf.getvalue() and "stopped after ig-za-2" in buf.getvalue()
    assert sum(r["credits_charged"] for r in ledger.rows) == 150


def test_ig_locations_live_client_uses_the_build_share_after_the_schedule(monkeypatch):
    seen = {}
    monkeypatch.setattr(probe, "live_client", lambda run_id, **kw: seen.update(run_id=run_id, **kw) or FakeClient())
    with redirect_stdout(io.StringIO()):
        probe.main(["--ig-locations"], clock=lambda: NOW)
    assert seen == {"run_id": "probe-ig-20260929T040000Z", "schedule_started": True}


def ig_raw(market, seed_key, rows, status=200):
    row = raw_row("instagram/search/location", "h-" + seed_key, seed_key, status, ig_body(rows) if rows else None)
    return {**row, "market": market}


def test_ig_locations_report_prints_a_block_ready_to_paste_into_markets_yaml():
    raw = [ig_raw("ZA", "8a", SOWETO), ig_raw("ZA", "ig-za-0", CITY_ROWS["Johannesburg"]),
           ig_raw("NG", "ig-ng-0", CITY_ROWS["Lagos"]), ig_raw("NG", "ig-ng-1", None, status=503),
           raw_row("tiktok/trending", "h9", "1-za", 200, trending_body(["ZA"], "za")),
           ig_raw("KE", "ig-ke-0", [])]
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert probe.main(["--ig-locations-report", "probe-ig-r"], read=lambda run_id: raw) == 0
    text = buf.getvalue()
    blocks = {}
    for part in text.split("# markets.")[1:]:
        market, _, body = part.partition("\n")
        blocks[market.strip()] = yaml.safe_load(body)
    assert set(blocks) == {"za", "ng"}
    za = blocks["za"]["instagram_locations"]
    assert za["values"] == ["320191774775153", "137545546357317", "566386939", "111"]
    assert "probe-ig-r" in za["source"]
    assert blocks["ng"]["instagram_locations"]["values"] == ["211"]
    assert '- "320191774775153"  # Soweto Diepkloof Zone 2, -26.264812, 28.40297' in text
    assert "KE: no candidates" in text
    assert "Portugal" not in text and not re.search(DASHES, text)


def test_ig_locations_report_keeps_at_most_five_a_market():
    rows = [ig_row(900 + i, f"Durban spot {i}", -29.85, 31.02) for i in range(8)]
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.main(["--ig-locations-report", "r"], read=lambda run_id: [ig_raw("ZA", "ig-za-2", rows)])
    block = yaml.safe_load(buf.getvalue().split("# markets.za\n")[1])
    assert block["instagram_locations"]["values"] == [str(900 + i) for i in range(5)]
    assert "8 candidates" in buf.getvalue()
