"""Tests for core/collect/local_sources.py, the local sources phase of task 2.12.

No network: a fake SocialCrawl client serves the invented web/scrape and prism/profiles bodies in
fixtures/local_*.json, and a fake getter serves robots.txt, the RSS and Atom feeds and the App Store
chart. The socket is shut for the --plan test.
"""

import json
import logging
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from core.collect import local_sources as ls
from core.collect.gdelt import SEED_FIELDS
from core.collect.parse import COUNTER_COLUMNS, OBSERVATION_COLUMNS, POST_COLUMNS
from core.collect.socialcrawl_client import PRICED, Result, quote_for

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRAPE = json.loads((FIXTURES / "local_scrape.json").read_text(encoding="utf-8"))
PROFILES = json.loads((FIXTURES / "local_profiles.json").read_text(encoding="utf-8"))
GOOGLE_PLAY = json.loads((FIXTURES / "local_google_play.json").read_text(encoding="utf-8"))
RSS = (FIXTURES / "local_rss.xml").read_text(encoding="utf-8")
ATOM = (FIXTURES / "local_atom.xml").read_text(encoding="utf-8")
APP_STORE = (FIXTURES / "local_app_store.json").read_text(encoding="utf-8")
MONDAY = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)  # 02:30 SAST, 01:30 WAT, 03:30 EAT
HOSTS = {"kworb.net": "kworb_spotify", "www.boomplay.com": "boomplay", "audiomack.com": "audiomack",
         "www.shazam.com": "shazam", "turntablecharts.com": "turntable", "www.nairaland.com": "nairaland"}
KINDS = {"topic", "hashtag", "sound", "format", "meme", "creator", "brand", "event"}


def fake_item_id(kind, raw, platform=None):
    """Lane L2's rules: an unknown kind or an empty key raises ValueError; sound and creator need platform."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    key = " ".join(str(raw or "").strip().lstrip("#@").casefold().split())
    if not key:
        raise ValueError("empty key")
    if kind in ("sound", "creator"):
        if not platform:
            raise ValueError(f"{kind} needs a platform")
        key = f"{platform}:{key}"
    return f"{kind}|{key}"


def fake_geo(platform, market, *, ext_region=None, home_market=None, profile_location=None, text=None,
             language=None):
    return None, None, None


def envelope(markdown, url):
    return {"success": True, "credits_used": 1, "endpoint": "/v1/web/scrape", "platform": "web",
            "data": {"page": {"status_code": 200, "url": url, "final_url": url,
                              "content": {"markdown": markdown, "html": None, "raw_html": None, "summary": None}}}}


class FakeClient:
    def __init__(self, status=None, charge=None):
        self.status = status or (lambda n, route: "ok")
        self.charge = charge  # credits the vendor reports per call, overriding the quote
        self.calls = []

    def call(self, route, params=None, *, method=None, market=None, item_id=None, seed_key=None, agent=None,
             lane=None, use_cache=True):
        self.calls.append({"route": route, "params": params, "market": market, "lane": lane})
        status = self.status(len(self.calls), route)
        if status != "ok":
            return Result(status, route, reason="scripted")
        hold = quote_for(route, method or PRICED[route].method, params)
        if route == "web/scrape":
            body = envelope(SCRAPE[HOSTS[urlsplit(params["url"]).netloc]], params["url"])
            items = []
        else:
            body = json.loads(json.dumps(PROFILES))
            items = body["data"]["results"]
        charged = hold if self.charge is None else max(hold, self.charge)
        return Result("ok", route, credits_quoted=hold, credits_charged=charged, body=body, items=items)


class FakeGet:
    def __init__(self, robots=None):
        self.robots = robots or {}
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            found = self.robots.get(parts.netloc, (404, ""))
            if isinstance(found, Exception):
                raise found
            return found
        if "marketingtools.apple.com" in parts.netloc:
            return 200, APP_STORE
        feed = next((f for f in ls.FEEDS if f.url == url), None)
        if feed is None:
            return 404, ""
        return {"NG": (200, RSS), "KE": (200, ATOM), "ZA": (500, "")}[feed.market]


def run(client=None, get=None, day=MONDAY, **kw):
    kw.setdefault("sleep", lambda seconds: None)
    return ls.run(day, "run-1", client=client or FakeClient(), get=get or FakeGet(), clock=lambda: NOW,
                  item_id_fn=fake_item_id, geo_fn=fake_geo, **kw)


# The plan

def test_every_day_of_the_week_holds_at_most_40_credits():
    for offset in range(7):
        fetches = ls.plan(MONDAY + timedelta(days=offset))
        assert ls.total_hold(fetches) <= ls.LOCAL_DAILY_CAP == 40


def test_monday_plan_holds_34_with_the_za_google_play_chart():
    fetches = ls.plan(MONDAY)
    assert ls.total_hold(fetches) == 34
    play = [f for f in fetches if f.route == "google_play/app-list"]
    assert [f.market for f in play] == ["ZA"]
    assert [f.market for f in ls.plan(MONDAY + timedelta(days=1)) if f.route == "google_play/app-list"] == ["NG"]
    assert [f.market for f in ls.plan(MONDAY + timedelta(days=2)) if f.route == "google_play/app-list"] == ["KE"]
    assert not [f for f in ls.plan(MONDAY + timedelta(days=3)) if f.route == "google_play/app-list"]


def test_plan_covers_every_named_source_in_every_market_it_serves():
    fetches = ls.plan(MONDAY)
    by = {}
    for f in fetches:
        by.setdefault(f.source, set()).add(f.market)
    three = {"ZA", "NG", "KE"}
    assert "nairaland" not in by and "audiomack" not in by  # retired (DROPPED_SCRAPES)
    assert by["turntable"] == {"NG"}
    for source in ("boomplay", "shazam", "ig_gossip_panel", "app_store"):
        assert by[source] == three, source
    assert by["kworb_spotify"] == {"ZA", "NG"}
    assert by["shazam_city"] == {"ZA", "NG"}
    assert {f.market for f in fetches if f.route == "rss"} == three
    names = {f.source for f in fetches if f.route == "rss"}
    assert {"briefly", "tuko", "bellanaija", "punch"} <= names
    assert not names & set(ls.DROPPED_FEEDS)


def test_paid_fetches_quote_through_the_client_and_never_add_a_proxy():
    for f in ls.plan(MONDAY):
        if f.route in ("rss", "apple_rss"):
            assert f.hold() == 0 and not f.paid
            continue
        assert f.paid
        assert f.route in ("web/scrape", "prism/profiles", "google_play/app-list")
        assert "proxy" not in f.params and "pdf_parse" not in f.params
        if f.route == "google_play/app-list":
            assert not f.priced() and f.hold() == 5
        else:
            assert f.priced() and f.hold() == quote_for(f.route, PRICED[f.route].method, f.params)


def test_scrape_holds_one_credit_and_the_panel_two_a_handle():
    fetches = ls.plan(MONDAY)
    assert {f.hold() for f in fetches if f.route == "web/scrape"} == {1}
    for f in fetches:
        if f.route == "prism/profiles":
            assert f.params["include"] == "posts"
            assert f.params["since"] == "2026-10-04"
            assert all(i["platform"] == "instagram" for i in f.params["items"])
            assert f.hold() == 2 * len(f.params["items"])


def test_every_fetch_names_its_source_series_and_protocol():
    for f in ls.plan(MONDAY):
        assert f.source and f.series and f.url
        assert f.url in f.protocol() or f.route == "prism/profiles"
    app = [f for f in ls.plan(MONDAY) if f.source == "app_store"]
    assert all(f.series == "board_app_store_iphone" and "iphone_only" in f.protocol() for f in app)


def test_plan_cli_prints_every_fetch_and_the_total_with_no_network(monkeypatch, capsys):
    def no_network(*a, **k):
        raise AssertionError("--plan opened a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    assert ls.main(["--plan", "--run-date", "2026-10-05"]) == 0
    out = capsys.readouterr().out
    for f in ls.plan(MONDAY):
        assert f.url in out or f.route == "prism/profiles", f.url
    for market in ("ZA", "NG", "KE"):
        assert f"{market} hold" in out
    assert "total hold 34 credits" in out
    assert "cap 40" in out
    assert "not priced" in out
    for name in ls.DROPPED_FEEDS:
        assert name in out


# Parsers

def test_kworb_table_gives_ranked_spotify_tracks_by_id():
    entries = ls.chart_entries(SCRAPE["kworb_spotify"], ls.SCRAPE_SOURCES["kworb_spotify"])
    assert [e["rank"] for e in entries] == [1, 2, 3]
    assert entries[0] == {"rank": 1, "title": "Sunday Braai", "artist": "Lwazi Sky", "native": "0aB1cD2eF3gH4iJ5kL6mN7"}
    assert entries[2]["title"] == "Stokvel Friday (feat. Thandi Waves)"


def test_boomplay_numbered_list_skips_cover_images():
    entries = ls.chart_entries(SCRAPE["boomplay"], ls.SCRAPE_SOURCES["boomplay"])
    assert [(e["rank"], e["title"], e["artist"], e["native"]) for e in entries] == [
        (1, "Owambe Season", "Ade Bright", "130200101"),
        (2, "Japa Letter", "Chioma Rays", "130200102"),
        (3, "Lagos Traffic Jam", "Ade Bright", "130200103")]


@pytest.mark.parametrize("market, expected", [
    ("NG", [(1, "Holy Ghost", "Omah Lay"), (2, "reason", "Omah Lay"),
            (3, "BADMAN GANGSTA ft. Tiakola", "Asake")]),
    ("KE", [(1, "Holy Ghost", "Omah Lay"), (2, "reason", "Omah Lay"),
            (3, "BADMAN GANGSTA ft. Tiakola", "Asake")]),
    ("ZA", [(1, "Luqale sakudlala", "G-Jealous"),
            (2, "Sengithole Omunye (Remix) ft. Chulumanco M", "Feza"),
            (3, "Faithful God", "S.O.N Music")]),
])
def test_recorded_boomplay_cards_read_song_and_artist_links(market, expected):
    recorded = json.loads((FIXTURES / "local_boomplay_recorded.json").read_text(encoding="utf-8"))
    markdown = next(row["markdown"] for row in recorded["rows"] if row["market"] == market)
    entries = ls.chart_entries(markdown, ls.SCRAPE_SOURCES["boomplay"])
    assert [(e["rank"], e["title"], e["artist"]) for e in entries] == expected
    assert all(e["native"] is None for e in entries)
    assert ls.song_key(entries[0]) == f"{expected[0][2]} - {expected[0][1]}"


def test_recorded_boomplay_card_cut_before_its_name_is_not_a_numeric_song():
    recorded = json.loads((FIXTURES / "local_boomplay_recorded.json").read_text(encoding="utf-8"))
    markdown = next(row["markdown"] for row in recorded["rows"] if row["market"] == "NG")
    incomplete = markdown.split("     [Holy Ghost]", 1)[0]
    assert ls.chart_entries(incomplete, ls.SCRAPE_SOURCES["boomplay"]) == []


def test_audiomack_list_takes_the_artist_after_the_link():
    entries = ls.chart_entries(SCRAPE["audiomack"], ls.SCRAPE_SOURCES["audiomack"])
    assert len(entries) == 4
    assert entries[1] == {"rank": 2, "title": "Matatu Ride", "artist": "Sheng Crew", "native": "sheng-crew/song/matatu-ride"}


def test_shazam_rank_blocks():
    entries = ls.chart_entries(SCRAPE["shazam"], ls.SCRAPE_SOURCES["shazam"])
    assert [(e["rank"], e["title"], e["artist"], e["native"]) for e in entries] == [
        (1, "Sunday Braai", "Lwazi Sky", "1700000001"),
        (2, "Amapiano Sunrise", "Thandi Waves", "1700000002"),
        (3, "Jozi Rain", "Mpho K", "1700000003")]


def test_turntable_table_without_ids_keys_on_artist_and_title():
    entries = ls.chart_entries(SCRAPE["turntable"], ls.SCRAPE_SOURCES["turntable"])
    assert [(e["rank"], e["title"], e["artist"], e["native"]) for e in entries] == [
        (1, "Owambe Season", "Ade Bright", None), (2, "Japa Letter", "Chioma Rays", None),
        (3, "Suya Night", "Tunde Flow", None)]
    assert ls.song_key(entries[0]) == "Ade Bright - Owambe Season"


def test_nairaland_front_page_threads_in_order_once_each():
    threads = ls.nairaland_threads(SCRAPE["nairaland"])
    assert [(t["rank"], t["native"]) for t in threads] == [(1, "8123456"), (2, "8123460"), (3, "8123471")]
    assert threads[1]["title"] == "Super Eagles Squad List Announced For Qualifiers"
    assert threads[1]["url"] == "https://www.nairaland.com/8123460/super-eagles-squad-list-announced"


def test_headlines_from_rss_and_atom_keep_the_last_two_days():
    ng = ls.headlines(RSS, NOW)
    assert len(ng) == 8 and not any("Old story" in h for h in ng)
    ke = ls.headlines(ATOM, NOW)
    assert ke[0] == "Harambee Stars coach names squad for Nations Cup qualifier" and len(ke) == 4


def test_news_seeds_need_two_headlines_and_drop_rule_one_phrases():
    ng = ls.news_seeds("NG", ls.headlines(RSS, NOW), MONDAY, fake_item_id)
    assert {s["query"] for s in ng} == {"Super Eagles", "Ade Bright"}
    ke = ls.news_seeds("KE", ls.headlines(ATOM, NOW), MONDAY, fake_item_id)
    assert {s["query"] for s in ke} == {"Harambee Stars", "Wavy K", "Nairobi"}
    seed = next(s for s in ng if s["query"] == "Super Eagles")
    assert set(seed) == {name for name, _ in SEED_FIELDS}
    assert seed["kind"] == "topic" and seed["lane"] == "expansion" and seed["template"] == "search/multi"
    assert seed["item_id"] == "topic|super eagles" and seed["priority"] == 2.0 and seed["seed_date"] == MONDAY
    assert seed["credits_estimate"] > 0


def test_app_board_reads_app_store_and_google_play_shapes():
    assert ls.app_board(json.loads(APP_STORE)) == ["Sixty Minute Groceries", "Kasi Bank App", "Braai Radio"]
    assert ls.app_board(GOOGLE_PLAY) == ["Sixty Minute Groceries", "Kasi Bank App", "Jozi Taxi"]


# The run

def test_run_lands_rows_for_every_source_and_a_coverage_record_for_every_fetch():
    client = FakeClient()
    out = run(client)
    fetches = ls.plan(MONDAY)
    assert len(out["records"]) == len(fetches)
    series = {r["series"] for r in out["records"] if r["ok"]}
    for name in ("board_kworb_spotify", "board_boomplay", "board_shazam", "board_shazam_city",
                 "board_turntable", "board_app_store_iphone", "panel_ig_gossip", "news_rss"):
        assert name in series, name
    for r in out["records"]:
        assert r["day"] == "2026-10-05" and r["protocol"] and r["series"] and r["route"]
    assert out["credits"] <= ls.LOCAL_DAILY_CAP
    assert out["credits"] == sum(
        quote_for(call["route"], PRICED[call["route"]].method, call["params"])
        for call in client.calls
    )
    assert {c["route"] for c in client.calls} == {"web/scrape", "prism/profiles"}


def test_retired_local_sources_are_left_out_of_the_plan_and_write_no_health_row(monkeypatch, capsys):
    from core.collect import writers

    retired = {("kworb_spotify", "KE"), ("ewn", "ZA")}
    for offset in range(7):
        assert not [f for f in ls.plan(MONDAY + timedelta(days=offset)) if (f.source, f.market) in retired]

    client, get = FakeClient(), FakeGet()
    out = run(client, get)
    assert not [c for c in client.calls if c["params"].get("url", "").endswith("/ke_daily.html")]
    assert not [url for url in get.urls if "ewn.co.za" in url]
    assert not [r for r in out["records"] if r["market"] == "KE" and r["series"] == "board_kworb_spotify"]
    assert not [r for r in out["records"] if "ewn.co.za" in r["protocol"]]
    health = writers.health_rows(out["records"], out["posts"], {}, "run-1")
    assert not [h for h in health if h["market"] == "KE" and h["platform"] == "spotify"]
    assert not [h for h in health if "ewn.co.za" in h["protocol"]]
    for market in ("ZA", "NG"):
        record = next(r for r in out["records"] if r["market"] == market and r["series"] == "board_kworb_spotify")
        assert record["ok"] and record["calls"] == 1
    apple_ke = next(f for f in ls.plan(MONDAY) if f.source == "app_store" and f.market == "KE")
    apple_record = next(r for r in out["records"] if r["market"] == "KE" and r["protocol"] == apple_ke.protocol())
    assert apple_record["ok"] and apple_record["calls"] == 1
    assert out["credits"] == sum(
        quote_for(call["route"], PRICED[call["route"]].method, call["params"])
        for call in client.calls
    )

    def no_network(*a, **k):
        raise AssertionError("--plan opened a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    assert ls.main(["--plan", "--run-date", "2026-10-05"]) == 0
    printed = capsys.readouterr().out
    assert "dropped scrape kworb_spotify KE:" in printed and "dropped feed ewn:" in printed
    assert "ke_daily.html" not in printed.split("dropped")[0] and "ewn.co.za" not in printed.split("dropped")[0]


def test_run_counters_are_board_ranks_in_the_ddl_columns():
    out = run()
    assert out["counters"]
    for c in out["counters"]:
        assert tuple(c) == COUNTER_COLUMNS
        assert c["is_board"] is True and c["unit"] == "rank" and c["lane_class"] == "unbiased_rank"
        assert c["series"] in ls.LOCAL_RANK_SERIES and c["pull_seq"] == 1 and c["source"] == "live"
    kworb_za = [c for c in out["counters"] if c["series"] == "board_kworb_spotify" and c["market"] == "ZA"]
    assert [(c["item_id"], c["value"]) for c in kworb_za] == [
        ("sound|spotify:0ab1cd2ef3gh4ij5kl6mn7", 1.0), ("sound|spotify:1bc2de3fg4hi5jk6lm7no8", 2.0),
        ("sound|spotify:2cd3ef4gh5ij6kl7mn8op9", 3.0)]
    assert "kworb.net/spotify/country/za_daily.html" in kworb_za[0]["protocol"]
    turntable = [c for c in out["counters"] if c["series"] == "board_turntable"]
    assert turntable[0]["item_id"] == "sound|turntable:ade bright - owambe season"
    apps = [c for c in out["counters"] if c["series"] == "board_app_store_iphone" and c["market"] == "ZA"]
    assert apps[0]["item_id"] == "brand|sixty minute groceries" and apps[0]["platform"] == "app_store"


def test_run_pull_seq_continues_from_the_last_stored_pull():
    fetch = next(f for f in ls.plan(MONDAY) if f.source == "kworb_spotify" and f.market == "ZA")
    out = run(last_pulls={("ZA", "board_kworb_spotify", fetch.protocol()): 6})
    kworb_za = [c for c in out["counters"] if c["series"] == "board_kworb_spotify" and c["market"] == "ZA"]
    assert {c["pull_seq"] for c in kworb_za} == {7}


def test_run_nairaland_threads_are_posts_with_ranked_observations(monkeypatch):
    monkeypatch.delitem(ls.DROPPED_SCRAPES, ("nairaland", "NG"))  # retired; the reader stays for its return
    out = run()
    posts = [p for p in out["posts"] if p["platform"] == "nairaland"]
    assert [p["native_id"] for p in posts] == ["8123456", "8123460", "8123471"]
    assert all(tuple(p) == POST_COLUMNS and p["endpoint"] == "web/scrape" and p["run_id"] == "run-1" for p in posts)
    obs = [o for o in out["observations"] if o["series"] == "board_nairaland"]
    assert [o["rank"] for o in obs] == [1, 2, 3]
    assert all(tuple(o) == OBSERVATION_COLUMNS and o["lane_class"] == "unbiased_rank" and o["market"] == "NG"
               for o in obs)
    assert all(o["source_market"] == "NG" and o["source_region"] is None for o in obs)
    record = next(r for r in out["records"] if r["series"] == "board_nairaland")
    assert record["items"] == 3 and record["post_ids"] == [p["post_id"] for p in posts]


def test_run_ig_gossip_panel_is_its_own_panel_series():
    client = FakeClient()
    out = run(client)
    obs = [o for o in out["observations"] if o["series"] == "panel_ig_gossip"]
    assert len(obs) == 9  # 3 posts in each of 3 markets
    assert all(o["lane_class"] == "panel" and o["protocol"].startswith("panel:") for o in obs)
    assert all(o["source_market"] is None and o["source_region"] is None for o in obs)  # no handle on the lists
    record = next(r for r in out["records"] if r["series"] == "panel_ig_gossip" and r["market"] == "ZA")
    assert record["units_planned"] == 3 and record["units_ok"] == 2 and record["lane_class"] == "panel"
    call = next(c for c in client.calls if c["route"] == "prism/profiles")
    assert call["lane"] == "panel"


def test_run_news_gives_seeds_and_coverage_but_never_posts_or_counters():
    out = run()
    assert {s["market"] for s in out["seeds"]} == {"NG", "KE"}
    assert not [p for p in out["posts"] if p["platform"] == "news"]
    assert not [c for c in out["counters"] if c["series"] == "news_rss"]
    za = [r for r in out["records"] if r["series"] == "news_rss" and r["market"] == "ZA"]
    assert not [r for r in za if "ewn.co.za" in r["protocol"]]
    assert za and all(not r["ok"] and r["status"] == "http_500" for r in za)
    ng = [r for r in out["records"] if r["series"] == "news_rss" and r["market"] == "NG"]
    assert all(r["ok"] and r["items"] == 8 and r["lane_class"] == "context" for r in ng)


@pytest.mark.parametrize("source, series, markets", [
    ("audiomack", "board_audiomack", {"ZA", "NG", "KE"}),
    ("nairaland", "board_nairaland", {"NG"}),
])
def test_a_scraped_board_with_no_readable_entries_is_a_failed_fetch_not_a_valid_empty_board(
        monkeypatch, source, series, markets):
    from core.collect import writers

    shell = ("# Trending Now\n\n[Sign in](https://example.com/login) / [Upload](https://example.com/upload)\n\n"
             "Download the app to keep listening.\n")
    monkeypatch.setitem(SCRAPE, source, shell)
    for market in markets:  # both are retired; the no_entries rule stays for every scraped board
        monkeypatch.delitem(ls.DROPPED_SCRAPES, (source, market))
    client = FakeClient()
    out = run(client)

    records = [r for r in out["records"] if r["series"] == series]
    assert {r["market"] for r in records} == markets
    for r in records:
        assert r["calls"] == 1 and not r["ok"] and r["items"] == 0 and r["units_ok"] == 0
        assert r["status"] == "no_entries" and "no entries" in r["reason"]
    assert not [c for c in out["counters"] if c["series"] == series]
    assert not [o for o in out["observations"] if o["series"] == series]

    health = [h for h in writers.health_rows(out["records"], out["posts"], {}, "run-1") if h["series"] == series]
    assert health and all(not h["valid"] and h["invalid_reason"] == "calls: empty" for h in health)
    assert [c for c in out["counters"] if c["series"] == "board_boomplay"]


def test_run_records_google_play_as_not_priced_without_calling():
    client = FakeClient()
    out = run(client)
    record = next(r for r in out["records"] if r["series"] == "board_google_play")
    assert record["status"] == "not_priced" and record["calls"] == 0 and not record["ok"]
    assert not [c for c in client.calls if c["route"] == "google_play/app-list"]


def test_robots_disallow_stops_a_scrape_before_any_credit():
    get = FakeGet(robots={"kworb.net": (200, "User-agent: *\nDisallow: /spotify/\n")})
    client = FakeClient()
    out = run(client, get)
    assert not [c for c in client.calls if "kworb.net" in c["params"].get("url", "")]
    kworb = [r for r in out["records"] if r["series"] == "board_kworb_spotify"]
    assert {r["market"] for r in kworb} == {"ZA", "NG"}
    assert all(r["status"] == "robots_disallowed" and r["calls"] == 0 for r in kworb)
    assert get.urls.count("https://kworb.net/robots.txt") == 1


def test_robots_rules_for_our_agent_are_honoured_and_unreachable_robots_means_no(monkeypatch):
    monkeypatch.delitem(ls.DROPPED_SCRAPES, ("nairaland", "NG"))  # retired; planned here to test the rule
    get = FakeGet(robots={"www.nairaland.com": (200, "User-agent: 42-trend-engine\nDisallow: /\n"),
                          "www.shazam.com": OSError("timed out"),
                          "turntablecharts.com": (403, "")})
    client = FakeClient()
    out = run(client, get)
    urls = {c["params"].get("url", "") for c in client.calls}
    assert not any(host in u for u in urls for host in ("nairaland.com", "shazam.com", "turntablecharts.com"))
    blocked = {r["series"] for r in out["records"] if r["status"] == "robots_disallowed"}
    assert blocked == {"board_nairaland", "board_shazam", "board_shazam_city", "board_turntable"}
    assert any("boomplay.com" in u for u in urls)


def test_a_rate_limited_robots_txt_means_no():
    get = FakeGet(robots={"kworb.net": (429, "")})
    client = FakeClient()
    out = run(client, get)
    assert not [c for c in client.calls if "kworb.net" in c["params"].get("url", "")]
    kworb = [r for r in out["records"] if r["series"] == "board_kworb_spotify"]
    assert {r["market"] for r in kworb} == {"ZA", "NG"}
    assert all(r["status"] == "robots_disallowed" and r["calls"] == 0 for r in kworb)


def test_cap_reached_stops_every_later_paid_call():
    client = FakeClient(status=lambda n, route: "cap_reached" if n == 2 else "ok")
    out = run(client)
    assert len(client.calls) == 2
    statuses = [r["status"] for r in out["records"] if r["route"] in ("web/scrape", "prism/profiles")]
    assert statuses.count("cap_reached") == 1 and "not_made" in statuses


def test_the_local_cap_holds_at_run_time(monkeypatch):
    monkeypatch.setattr(ls, "LOCAL_DAILY_CAP", 9)  # ZA's paid fetches hold 10, so the ZA panel is over the cap
    out = run()
    assert out["credits"] <= 9
    assert any(r["status"] == "over_local_cap" for r in out["records"])


def test_an_over_reporting_vendor_stops_the_phase_once_charges_reach_the_cap():
    client = FakeClient(charge=12)  # every call billed at 12 against a quote of 1 or 6
    out = run(client)
    assert len(client.calls) == 4  # 12, 24, 36, then 48: the call that passes 40 is the last one
    assert ls.LOCAL_DAILY_CAP <= out["credits"] == 48
    paid = [r for r in out["records"] if r["route"] in ("web/scrape", "prism/profiles", "google_play/app-list")]
    made = [r for r in paid if r["calls"]]
    assert len(made) == 4 and paid[:4] == made
    later = [r for r in paid[4:] if r["status"] != "not_priced"]
    assert later and all(r["status"] == "not_made" and r["calls"] == 0 for r in later)
    assert all("local cap" in r["reason"] and "48" in r["reason"] for r in later)


def test_free_fetches_send_a_user_agent_naming_42_and_no_email_address(monkeypatch):
    seen = {}

    class Response:
        status_code, text = 200, "ok"

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers, timeout=timeout)
        return Response()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    assert ls.http_get("https://example.org/feed") == (200, "ok")
    assert "42" in seen["headers"]["User-Agent"] and "Ogilvy" in seen["headers"]["User-Agent"]
    assert "From" not in seen["headers"]
    assert not any("@" in value for value in seen["headers"].values())
    assert seen["timeout"]
    # A slow free chart (NG App Store, 3 Oct 2026) gets a 60 second read and a 10 second connect.
    assert seen["timeout"] == (10, 60)


def test_failed_free_http_response_logs_safe_fields_and_keeps_success_controls(caplog):
    base_get = FakeGet()
    apple_ke = next(f.url for f in ls.plan(MONDAY) if f.source == "app_store" and f.market == "KE")
    private_body = "response-body-must-not-be-logged"

    def get(url):
        if url == apple_ke:
            return 503, private_body
        return base_get(url)

    caplog.set_level(logging.WARNING, logger=ls.__name__)
    out = run(get=get)

    events = [json.loads(record.getMessage()) for record in caplog.records if record.name == ls.__name__]
    apple_events = [event for event in events if event["source"] == "app_store"]
    assert apple_events == [{
        "event": "local_source_failure",
        "run_id": "run-1",
        "source": "app_store",
        "market": "KE",
        "route": "apple_rss",
        "status": "http_error",
        "http_status": 503,
    }]
    assert private_body not in caplog.text and apple_ke not in caplog.text
    records = {r["market"]: r for r in out["records"] if r["series"] == "board_app_store_iphone"}
    assert records["KE"]["status"] == "http_503" and records["KE"]["calls"] == 1
    assert all(records[m]["ok"] for m in ("ZA", "NG"))


def test_failed_free_get_logs_exception_class_without_exception_value(caplog):
    base_get = FakeGet()
    apple_ke = next(f.url for f in ls.plan(MONDAY) if f.source == "app_store" and f.market == "KE")
    private_detail = "request-detail-must-not-be-logged"

    def get(url):
        if url == apple_ke:
            raise OSError(private_detail)
        return base_get(url)

    caplog.set_level(logging.WARNING, logger=ls.__name__)
    out = run(get=get)

    events = [json.loads(record.getMessage()) for record in caplog.records if record.name == ls.__name__]
    apple_events = [event for event in events if event["source"] == "app_store"]
    assert apple_events == [{
        "event": "local_source_failure",
        "run_id": "run-1",
        "source": "app_store",
        "market": "KE",
        "route": "apple_rss",
        "status": "get_error",
        "exception_class": "OSError",
    }]
    assert private_detail not in caplog.text and apple_ke not in caplog.text
    records = {r["market"]: r for r in out["records"] if r["series"] == "board_app_store_iphone"}
    assert records["KE"]["status"] == "error" and records["KE"]["calls"] == 1
    assert all(records[m]["ok"] for m in ("ZA", "NG"))


def test_failed_free_parser_logs_class_without_exception_value_or_body(caplog, monkeypatch):
    base_get = FakeGet()
    apple_ke = next(f.url for f in ls.plan(MONDAY) if f.source == "app_store" and f.market == "KE")
    private_value = "app-title-must-not-be-logged"
    original_app_board = ls.app_board

    def get(url):
        if url == apple_ke:
            return 200, json.dumps({"feed": {"results": [{"name": private_value}]}})
        return base_get(url)

    def app_board(body):
        names = original_app_board(body)
        if private_value in names:
            raise ValueError(private_value)
        return names

    monkeypatch.setattr(ls, "app_board", app_board)
    caplog.set_level(logging.WARNING, logger=ls.__name__)
    out = run(get=get)

    events = [json.loads(record.getMessage()) for record in caplog.records if record.name == ls.__name__]
    apple_events = [event for event in events if event["source"] == "app_store"]
    assert apple_events == [{
        "event": "local_source_failure",
        "run_id": "run-1",
        "source": "app_store",
        "market": "KE",
        "route": "apple_rss",
        "status": "parse_error",
        "http_status": 200,
        "exception_class": "ValueError",
    }]
    assert private_value not in caplog.text and apple_ke not in caplog.text
    records = {r["market"]: r for r in out["records"] if r["series"] == "board_app_store_iphone"}
    assert records["KE"]["status"] == "unparsable" and records["KE"]["calls"] == 1
    assert all(records[m]["ok"] for m in ("ZA", "NG"))


@pytest.mark.parametrize("body", [{}, {"error": "rate limited"}, {"feed": {"title": "Top Free Apps"}},
                                  {"feed": {"results": {"name": "Not a list"}}}])
def test_an_app_store_body_without_the_chart_list_is_unparsable_not_a_valid_empty_chart(body):
    from core.collect import writers

    base_get = FakeGet()
    apple_ke = next(f.url for f in ls.plan(MONDAY) if f.source == "app_store" and f.market == "KE")

    def get(url):
        return (200, json.dumps(body)) if url == apple_ke else base_get(url)

    out = run(get=get)

    records = {r["market"]: r for r in out["records"] if r["series"] == "board_app_store_iphone"}
    ke = records["KE"]
    assert ke["status"] == "unparsable" and ke["calls"] == 1 and not ke["ok"]
    assert ke["items"] == 0 and ke["units_ok"] == 0 and ke["reason"]
    assert not [c for c in out["counters"] if c["series"] == "board_app_store_iphone" and c["market"] == "KE"]
    health = [h for h in writers.health_rows(out["records"], out["posts"], {}, "run-1")
              if h["series"] == "board_app_store_iphone" and h["market"] == "KE"]
    assert health and all(not h["valid"] for h in health)
    assert all(records[m]["ok"] for m in ("ZA", "NG"))


def test_an_app_store_chart_with_an_empty_results_list_stays_a_read_empty_chart():
    base_get = FakeGet()
    apple_ke = next(f.url for f in ls.plan(MONDAY) if f.source == "app_store" and f.market == "KE")

    def get(url):
        return (200, json.dumps({"feed": {"results": []}})) if url == apple_ke else base_get(url)

    out = run(get=get)
    ke = next(r for r in out["records"] if r["series"] == "board_app_store_iphone" and r["market"] == "KE")
    assert ke["status"] == "ok" and ke["ok"] and ke["items"] == 0 and ke["units_ok"] == 1


def test_panel_handles_and_notes_carry_no_rule_one_words():
    from core.collect.gdelt import blocked
    for f in ls.plan(MONDAY):
        assert not blocked(f.note)
        for item in f.params.get("items", []):
            assert not blocked(item["handle"])


def test_ig_gossip_panel_reads_the_handles_the_source_audit_kept():
    # 42_Source_Audit_Kept_Accounts_2026-09-30: @lindaikejiblogofficial replaces @lindaikejiblog (NG) and
    # @mpashogram replaces @mpasho_news (KE); both are active in core/config/uefa_creators.yaml.
    handles = {f.market: f.params["items"] for f in ls.plan(date(2026, 9, 29)) if f.source == "ig_gossip_panel"}
    ng = [i["handle"] for i in handles["NG"]]
    ke = [i["handle"] for i in handles["KE"]]
    assert "lindaikejiblogofficial" in ng and "lindaikejiblog" not in ng
    assert "mpashogram" in ke and "mpashonews" not in ke


def test_ig_gossip_posts_from_a_listed_market_outlet_count_as_seen_in_its_feeds(monkeypatch):
    # TRUST.md "Market by source": a post from a local outlet listed for that market. A gossip page counts
    # only when the curated lists (the 30 Sept source audit, the research and Ogilvy lists) hold its handle
    # for that market, the same accounts the curated rotation already reads as the market's feeds.
    from core.collect import curated_creators
    listed = [
        {"platform": "instagram", "handle": "gossip.page.one", "url": "https://www.instagram.com/gossip.page.one/",
         "market": "ZA", "source": curated_creators.AUDITED_ACCOUNT_SOURCE, "active": True},
        {"platform": "instagram", "handle": "Blog.Page.Two", "url": "https://www.instagram.com/blog.page.two/",
         "market": "NG", "source": "research_confirmed", "active": True},
        {"platform": "instagram", "handle": "blog.page.two", "url": "https://www.instagram.com/blog.page.two/",
         "market": "KE", "source": curated_creators.AUDITED_ACCOUNT_SOURCE, "active": False},
    ]
    monkeypatch.setattr(curated_creators, "load_manifest", lambda: listed)
    out = run()
    posts = {p["post_id"]: p for p in out["posts"]}
    obs = [o for o in out["observations"] if o["series"] == "panel_ig_gossip"]
    tagged = {(o["market"], posts[o["post_id"]]["creator_id"], o["source_market"]) for o in obs}
    assert tagged == {("ZA", "gossip.page.one", "ZA"), ("ZA", "blog.page.two", None),
                      ("NG", "gossip.page.one", None), ("NG", "blog.page.two", "NG"),
                      ("KE", "gossip.page.one", None), ("KE", "blog.page.two", None)}
    assert all(o["source_region"] is None for o in obs)
    others = [o for o in out["observations"] if o["series"] != "panel_ig_gossip" and o["platform"] == "instagram"]
    assert all(o["source_market"] is None for o in others)


def test_ng_ig_gossip_panel_reads_only_pages_the_source_audit_kept():
    # NG's panel was effort-invalid on 30 Sept, 1 and 2 Oct. @bellanaijaonline was a desk draft the 30 Sept
    # source audit never kept; @gossipmilltv is an NG publisher it kept (profile observed and matched).
    from core.collect import curated_creators
    listed = {(r["platform"], r["handle"]) for r in curated_creators.load_manifest()
              if r["market"] == "NG" and r["source"] == curated_creators.AUDITED_ACCOUNT_SOURCE and r["active"]}
    handles = {f.market: [i["handle"] for i in f.params["items"]]
               for f in ls.plan(date(2026, 10, 5)) if f.source == "ig_gossip_panel"}
    assert handles["NG"] == ["instablog9ja", "lindaikejiblogofficial", "gossipmilltv"]
    assert all(("instagram", h) in listed for h in handles["NG"])


def test_nairaland_and_audiomack_are_retired_with_a_named_reason_and_write_no_health_row(monkeypatch, capsys):
    # 1 to 3 Oct 2026: board_nairaland NG was planned every run and never called (the robots.txt check is the
    # only path to 0 calls of 1 planned for that page alone), and board_audiomack gave no entries in every
    # market. Like kworb KE, they leave the plan instead of failing their platforms' days.
    from core.collect import writers

    retired = {("nairaland", "NG"), ("audiomack", "ZA"), ("audiomack", "NG"), ("audiomack", "KE")}
    assert retired <= set(ls.DROPPED_SCRAPES)
    assert all(ls.DROPPED_SCRAPES[key] for key in retired)
    for offset in range(7):
        assert not [f for f in ls.plan(MONDAY + timedelta(days=offset)) if (f.source, f.market) in retired]
    client, get = FakeClient(), FakeGet()
    out = run(client, get)
    assert not [c for c in client.calls if urlsplit(c["params"].get("url", "")).netloc in
                ("www.nairaland.com", "audiomack.com")]
    assert not [u for u in get.urls if "nairaland.com" in u or "audiomack.com" in u]
    assert not [r for r in out["records"] if r["series"] in ("board_nairaland", "board_audiomack")]
    health = writers.health_rows(out["records"], out["posts"], {}, "run-1")
    assert not [h for h in health if h["platform"] in ("nairaland", "audiomack")]
    # The scrapes stay defined, so deleting the DROPPED_SCRAPES entry plans the page again.
    monkeypatch.delitem(ls.DROPPED_SCRAPES, ("nairaland", "NG"))
    assert [f.market for f in ls.plan(MONDAY) if f.source == "nairaland"] == ["NG"]
    monkeypatch.undo()

    def no_network(*a, **k):
        raise AssertionError("--plan opened a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    assert ls.main(["--plan", "--run-date", "2026-10-05"]) == 0
    printed = capsys.readouterr().out
    assert "dropped scrape nairaland NG: the robots.txt check refused" in printed
    for market in ("ZA", "NG", "KE"):
        assert f"dropped scrape audiomack {market}: trending-now came back with no chart rows" in printed
    assert "nairaland.com" not in printed and "audiomack.com" not in printed


@pytest.mark.parametrize("first", [(504, "<html>504 Gateway Time-out</html>"), TimeoutError("read timed out"),
                                   ConnectionError("reset"), (429, "")], ids=["504", "timeout", "connection", "429"])
def test_a_transient_app_store_failure_is_tried_once_more_and_the_chart_counts(first):
    # The Apple feed answers 504 after about 30 s now and then (NG on 3 Oct 2026, KE on 1 Oct), which failed the
    # day's baseline chart on one attempt. A second attempt after FREE_RETRY_WAIT reads the chart.
    from core.collect import writers

    apple_ng = next(f.url for f in ls.plan(MONDAY) if f.source == "app_store" and f.market == "NG")
    base, seen, waits = FakeGet(), [], []

    def get(url):
        if url == apple_ng:
            seen.append(url)
            if len(seen) == 1:
                if isinstance(first, Exception):
                    raise first
                return first
        return base(url)

    out = ls.run(MONDAY, "run-1", client=FakeClient(), get=get, clock=lambda: NOW, item_id_fn=fake_item_id,
                 geo_fn=fake_geo, sleep=waits.append)
    assert len(seen) == 2 and waits == [ls.FREE_RETRY_WAIT]
    record = next(r for r in out["records"] if r["series"] == "board_app_store_iphone" and r["market"] == "NG")
    assert record["ok"] and record["calls"] == 1 and record["items"] > 0
    assert any(c["market"] == "NG" and c["series"] == "board_app_store_iphone" for c in out["counters"])
    health = {(h["market"], h["series"]): h for h in writers.health_rows(out["records"], out["posts"], {}, "run-1")}
    assert health["NG", "board_app_store_iphone"]["valid"] is True


@pytest.mark.parametrize("answer", [(404, ""), (403, ""), (200, "not json")], ids=["404", "403", "unparsable"])
def test_a_lasting_app_store_failure_is_not_tried_again(answer):
    apple_ng = next(f.url for f in ls.plan(MONDAY) if f.source == "app_store" and f.market == "NG")
    base, seen, waits = FakeGet(), [], []

    def get(url):
        if url == apple_ng:
            seen.append(url)
            return answer
        return base(url)

    out = ls.run(MONDAY, "run-1", client=FakeClient(), get=get, clock=lambda: NOW, item_id_fn=fake_item_id,
                 geo_fn=fake_geo, sleep=waits.append)
    assert len(seen) == 1 and waits == []
    record = next(r for r in out["records"] if r["series"] == "board_app_store_iphone" and r["market"] == "NG")
    assert not record["ok"] and record["calls"] == 1


def test_a_chart_that_fails_twice_fails_the_day_and_says_it_was_tried_twice():
    from core.collect import writers

    apple_ng = next(f.url for f in ls.plan(MONDAY) if f.source == "app_store" and f.market == "NG")
    base, seen, waits = FakeGet(), [], []

    def get(url):
        if url == apple_ng:
            seen.append(url)
            return 504, ""
        return base(url)

    out = ls.run(MONDAY, "run-1", client=FakeClient(), get=get, clock=lambda: NOW, item_id_fn=fake_item_id,
                 geo_fn=fake_geo, sleep=waits.append)
    assert len(seen) == 2 and waits == [ls.FREE_RETRY_WAIT]
    record = next(r for r in out["records"] if r["series"] == "board_app_store_iphone" and r["market"] == "NG")
    assert record["status"] == "http_504" and record["calls"] == 1 and "2 attempts" in record["reason"]
    health = {(h["market"], h["series"]): h for h in writers.health_rows(out["records"], out["posts"], {}, "run-1")}
    assert health["NG", "board_app_store_iphone"]["invalid_reason"] == "calls: http_5xx"


def test_news_feeds_are_context_and_keep_one_attempt():
    # Only the free rank list (the App Store chart, a baseline series) is tried again; the ZA feeds answer 500
    # here and are fetched once each.
    base, waits = FakeGet(), []
    out = ls.run(MONDAY, "run-1", client=FakeClient(), get=base, clock=lambda: NOW, item_id_fn=fake_item_id,
                 geo_fn=fake_geo, sleep=waits.append)
    za_feeds = [f.url for f in ls.FEEDS if f.market == "ZA"]
    assert all(base.urls.count(url) == 1 for url in za_feeds) and waits == []
    assert all(not r["ok"] for r in out["records"] if r["series"] == "news_rss" and r["market"] == "ZA")


def test_health_says_which_failure_failed_a_local_fetch():
    from core.collect import writers

    apple = {f.market: f.url for f in ls.plan(MONDAY) if f.source == "app_store"}
    base = FakeGet(robots={"kworb.net": (200, "User-agent: *\nDisallow: /spotify/\n")})

    def get(url):
        if url == apple["KE"]:
            return 503, "body"
        if url == apple["NG"]:
            raise TimeoutError("read timed out")
        return base(url)

    client = FakeClient(status=lambda n, route: "error" if route == "prism/profiles" else "ok")
    out = run(client, get)
    health = {(h["market"], h["series"]): h for h in writers.health_rows(out["records"], out["posts"], {}, "run-1")}
    assert health["ZA", "board_kworb_spotify"]["invalid_reason"] == "calls: robots"
    assert health["KE", "board_app_store_iphone"]["invalid_reason"] == "calls: http_5xx"
    assert health["NG", "board_app_store_iphone"]["invalid_reason"] == "calls: timeout"
    assert health["ZA", "news_rss"]["invalid_reason"] == "calls: http_5xx"  # the ZA feeds answer 500 here
    assert health["ZA", "panel_ig_gossip"]["invalid_reason"] == "calls: error"  # a scripted result has no class
    assert health["ZA", "board_app_store_iphone"]["invalid_reason"] is None
    for h in health.values():
        assert h["invalid_reason"] is None or ("://" not in h["invalid_reason"] and "body" not in h["invalid_reason"])


def test_a_failed_paid_scrape_passes_the_client_failure_class_to_health():
    from core.collect import writers

    class Failing(FakeClient):
        def call(self, route, params=None, **kw):
            if route == "web/scrape" and "boomplay" in params["url"]:
                self.calls.append({"route": route, "params": params, "market": kw.get("market"), "lane": None})
                return Result("refunded", route, reason="HTTP 503 is refunded (3 attempts: http_5xx, http_5xx, "
                                                        "http_5xx)", attempts=3, failure="http_5xx")
            return super().call(route, params, **kw)

    out = run(Failing())
    boomplay = [r for r in out["records"] if r["series"] == "board_boomplay"]
    assert boomplay and all(r["failure"] == "http_5xx" and "3 attempts" in r["reason"] for r in boomplay)
    health = [h for h in writers.health_rows(out["records"], out["posts"], {}, "run-1")
              if h["series"] == "board_boomplay"]
    assert health and all(h["invalid_reason"] == "calls: http_5xx" for h in health)


APPLE_ROBOTS = "https://rss.marketingtools.apple.com/robots.txt"


def robots_then(answers):
    """A getter whose Apple robots.txt answers each of answers in turn (the last one again after that), counting
    the reads; every other URL is served as FakeGet serves it."""
    base, reads = FakeGet(), []

    def get(url):
        if url == APPLE_ROBOTS:
            answer = answers[min(len(reads), len(answers) - 1)]
            reads.append(url)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return base(url)

    return get, reads


@pytest.mark.parametrize("first", [(502, "<html>502 Bad Gateway</html>"), (504, ""), TimeoutError("read timed out")],
                         ids=["502", "504", "timeout"])
def test_a_robots_txt_read_that_fails_in_passing_is_read_once_more_and_the_charts_count(first):
    # The Apple host answers a 5xx now and then (ZA 502, then 200 three seconds later, 4 Oct 2026). robots.txt is
    # read once a run per host, so one passing failure there failed every market's App Store chart on calls.
    from core.collect import writers

    get, reads = robots_then([first, (200, "User-agent: *\nAllow: /\n")])
    waits = []
    out = ls.run(MONDAY, "run-1", client=FakeClient(), get=get, clock=lambda: NOW, item_id_fn=fake_item_id,
                 geo_fn=fake_geo, sleep=waits.append)
    assert len(reads) == 2 and waits == [ls.FREE_RETRY_WAIT]
    health = {(h["market"], h["series"]): h for h in writers.health_rows(out["records"], out["posts"], {}, "run-1")}
    for market in ("ZA", "NG", "KE"):
        assert health[market, "board_app_store_iphone"]["valid"] is True
        assert any(c["market"] == market and c["series"] == "board_app_store_iphone" for c in out["counters"])


@pytest.mark.parametrize("answers", [[(503, "")], [(504, ""), TimeoutError("read timed out")],
                                     [ConnectionError("reset")]], ids=["5xx twice", "5xx then timeout", "connection"])
def test_a_robots_txt_read_that_fails_twice_still_means_no(answers):
    from core.collect import writers

    get, reads = robots_then(answers)
    waits = []
    out = ls.run(MONDAY, "run-1", client=FakeClient(), get=get, clock=lambda: NOW, item_id_fn=fake_item_id,
                 geo_fn=fake_geo, sleep=waits.append)
    assert len(reads) == 2 and waits == [ls.FREE_RETRY_WAIT]
    apple = [r for r in out["records"] if r["series"] == "board_app_store_iphone"]
    assert {r["market"] for r in apple} == {"ZA", "NG", "KE"}
    assert all(r["status"] == "robots_disallowed" and r["calls"] == 0 and not r["ok"] for r in apple)
    assert not any(c["series"] == "board_app_store_iphone" for c in out["counters"])
    health = {(h["market"], h["series"]): h for h in writers.health_rows(out["records"], out["posts"], {}, "run-1")}
    for market in ("ZA", "NG", "KE"):
        assert health[market, "board_app_store_iphone"]["invalid_reason"] == "calls: robots"


@pytest.mark.parametrize("answer, allowed", [((404, ""), True), ((403, ""), False), ((401, ""), False)],
                         ids=["404", "403", "401"])
def test_a_lasting_robots_txt_answer_is_read_once_and_decides_as_before(answer, allowed):
    get, reads = robots_then([answer])
    waits = []
    out = ls.run(MONDAY, "run-1", client=FakeClient(), get=get, clock=lambda: NOW, item_id_fn=fake_item_id,
                 geo_fn=fake_geo, sleep=waits.append)
    assert len(reads) == 1 and waits == []
    apple = [r for r in out["records"] if r["series"] == "board_app_store_iphone"]
    assert len(apple) == 3
    if allowed:
        assert all(r["ok"] and r["calls"] == 1 for r in apple)
    else:
        assert all(r["status"] == "robots_disallowed" and r["calls"] == 0 for r in apple)


def test_robots_reads_never_allow_on_a_failed_answer():
    sleeps = []
    for answer in ((500, ""), (429, ""), OSError("unreachable"), TimeoutError("read timed out")):
        def get(url, answer=answer):
            if isinstance(answer, Exception):
                raise answer
            return answer
        assert ls.Robots(get, sleeps.append).allowed("https://example.org/chart") is False
    assert ls.Robots(lambda url: (404, ""), sleeps.append).allowed("https://example.org/chart") is True
