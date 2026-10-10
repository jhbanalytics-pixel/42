"""Fieldwork over the fixtures and a fake BigQuery (contract.md section 17)."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from core.api import auth, fieldwork
from core.api.store import BigQueryStore, FixtureStore

SAST = dt.timezone(dt.timedelta(hours=2))
NOW = dt.datetime(2026, 10, 2, 16, 0, tzinfo=SAST)
D30 = "2026-09-30"


class Patched(FixtureStore):
    def __init__(self, **overrides):
        super().__init__()
        for name, fn in overrides.items():
            setattr(self, name, fn)


def market(out, code):
    return next(m for m in out["markets"] if m["market"] == code)


def source(out, code, series):
    return next(s for s in market(out, code)["sources"] if s["series"] == series)


@pytest.fixture
def out():
    return fieldwork.build_fieldwork(FixtureStore(), None, now=NOW)


def test_defaults_to_the_latest_collection_day(out):
    assert out["date"] == D30 and out["latest_day"] == D30 and out["today"] == "2026-10-02"
    assert out["health_state"] == "recorded" and out["health_note"] is None
    assert [m["market"] for m in out["markets"]] == ["ZA", "NG", "KE", "GLOBAL"]


def test_roster_comes_from_the_collect_config(out):
    reddit = source(out, "ZA", "list_reddit")
    assert reddit["group"] == "own" and reddit["members"][0] == "r/southafrica"
    assert reddit["planned_calls"] == 12  # six subreddits, hot and rising (core/collect/job.py REDDIT_SUBS)
    gossip = source(out, "NG", "panel_ig_gossip")
    assert gossip["members"] == ["@instablog9ja", "@lindaikejiblogofficial", "@gossipmilltv"]
    assert source(out, "KE", "feed_tiktok")["planned_calls"] == 1
    news = source(out, "ZA", "news_rss")
    assert "Briefly (RSS)" in news["members"] and "eNCA" in news["members"] and news["free"] is True
    desk = source(out, "ZA", "panel_culture_desk")
    assert desk["group"] == "creators" and "culture desk accounts" in desk["detail"]
    assert "kept creator" not in desk["detail"]
    kept = source(out, "ZA", "panel_curated_creators")
    assert kept["group"] == "creators" and "kept creator accounts in today's rotation" in kept["detail"]
    trends = source(out, "ZA", "google_trends")
    assert trends["group"] == "search_interest" and "never evidence" in trends["detail"]


def test_health_joins_by_market_and_series(out):
    feed = source(out, "ZA", "feed_tiktok")
    assert feed["status"] == "delivered" and feed["health"]["calls_ok"] == 12
    assert feed["health"]["items"]["value"] == 540 and feed["health"]["items"]["query_id"] == "q_fieldwork_health"
    assert feed["health"]["located_share"] == 0.45
    board = source(out, "ZA", "board_tiktok_hashtag")
    assert board["status"] == "failed" and board["status_words"] == "Failed: could not be read"
    assert source(out, "NG", "list_reddit")["status_words"] == "Failed: post count far from usual"
    assert source(out, "KE", "list_reddit")["status_words"] == "Failed: mix of posts far from usual"
    assert source(out, "NG", "panel_x_hub")["status_words"] == "Failed: too few accounts checked"
    # Planned with no health row that day: said, never a zero.
    apple = source(out, "ZA", "board_apple_music")
    assert apple["status"] == "no_record" and apple["health"] is None


def test_a_day_with_no_health_rows_says_so_and_invents_nothing():
    out = fieldwork.build_fieldwork(FixtureStore(), "2026-10-02", now=NOW)
    assert out["date"] == "2026-10-02" and out["health_state"] == "absent"
    assert "No collection record for this day yet" in out["health_note"]
    for m in out["markets"]:
        for s in m["sources"]:
            assert s["health"] is None
            assert s["status"] in ("waiting", "off")
    assert out["credits"]["spent"] is None and out["credits"]["state"] == "none"


def test_an_unreadable_health_view_is_unavailable_not_a_500():
    def boom(*_):
        raise RuntimeError("Not found: Table v_collection_health_current")

    out = fieldwork.build_fieldwork(Patched(collection_health=boom, latest_collection_day=boom, credits=boom),
                                    None, now=NOW)
    assert out["health_state"] == "unavailable" and out["health_note"] == fieldwork.HEALTH_UNAVAILABLE
    assert out["date"] == "2026-10-02"
    assert out["credits"]["state"] == "unavailable" and out["credits"]["spent"] is None
    assert all(s["status"] in ("unavailable", "off", "waiting") for m in out["markets"] for s in m["sources"])


def test_a_health_series_the_plan_does_not_name_still_shows():
    rows = FixtureStore().collection_health(D30) + [
        {"day": D30, "market": "ZA", "platform": "telegram", "series": "panel_telegram", "calls": 2, "calls_ok": 2,
         "items": 9, "valid": True, "invalid_reason": None, "located_share": None, "run_id": "r1"}]
    out = fieldwork.build_fieldwork(Patched(collection_health=lambda d: rows), D30, now=NOW)
    extra = source(out, "ZA", "panel_telegram")
    assert extra["planned_calls"] == 0 and extra["status"] == "delivered" and extra["health"]["items"]["value"] == 9


def test_several_parts_give_a_range_and_a_partial_status():
    rows = [{"day": D30, "market": "ZA", "platform": "reddit", "series": "list_reddit", "calls": 1, "calls_ok": 1,
             "items": 30, "valid": True, "invalid_reason": None, "located_share": 0.2, "run_id": "r1"},
            {"day": D30, "market": "ZA", "platform": "reddit", "series": "list_reddit", "calls": 1, "calls_ok": 0,
             "items": 0, "valid": False, "invalid_reason": "calls", "located_share": None, "run_id": "r1"},
            {"day": D30, "market": "ZA", "platform": "reddit", "series": "list_reddit", "calls": 1, "calls_ok": 1,
             "items": 25, "valid": True, "invalid_reason": None, "located_share": 0.6, "run_id": "r1"}]
    out = fieldwork.build_fieldwork(Patched(collection_health=lambda d: rows), D30, now=NOW)
    reddit = source(out, "ZA", "list_reddit")
    assert reddit["status"] == "partial" and reddit["status_words"] == "Partly delivered: 2 of 3 parts usable"
    assert reddit["health"]["located_share"] is None and reddit["health"]["located_range"] == [0.2, 0.6]
    assert reddit["health"]["items"]["value"] == 55


def test_credits_are_the_collect_share_against_its_cap(out):
    credits = out["credits"]
    assert credits["cap"] == 2000  # core/config/caps.yaml ENGINE_DAILY.collect
    # collect rows only: 70 + 50 + 40 (ZA), 100 (NG), 5 (no market); brief and ask spend is Coverage's.
    assert credits["spent"]["value"] == 265.0 and credits["spent"]["query_id"] == "q_fieldwork_credits"
    assert [m["market"] for m in credits["markets"]] == ["ZA", "NG", "GLOBAL"]
    assert 0 < credits["planned"] <= credits["cap"]


def test_google_trends_state_comes_from_the_collect_run():
    run = {"run_id": "r_collect", "stage": "collect", "run_date": D30, "status": "ok", "started_at": "a",
           "finished_at": "b", "counts": {"search_signal_states": {"ZA": "ok", "NG": "not_made"}}, "error": None}
    out = fieldwork.build_fieldwork(Patched(runs=lambda stage, d: [run]), D30, now=NOW)
    assert source(out, "ZA", "google_trends")["status_words"] == "Trending searches read"
    assert source(out, "NG", "google_trends")["status"] == "failed"
    assert source(out, "KE", "google_trends")["status_words"] == "Not reported by this day's collection"


def test_off_sources_say_why_in_plain_words(out):
    off = {(o["market"], o["name"]): o["why"] for o in out["off"]}
    assert ("GLOBAL", "X trends archive") in off
    assert "Kenya" in off[("KE", "Spotify daily chart (via Kworb)")]
    assert all("_" not in why and "http" not in why for why in off.values())


@pytest.mark.parametrize("now,expected", [
    (dt.datetime(2026, 10, 2, 1, 0, tzinfo=SAST), "2026-10-02T02:00:00+02:00"),
    (dt.datetime(2026, 10, 2, 2, 0, tzinfo=SAST), "2026-10-03T02:00:00+02:00"),
    (dt.datetime(2026, 10, 1, 23, 30, tzinfo=dt.timezone.utc), "2026-10-02T02:00:00+02:00"),
])
def test_next_collection_is_the_0200_sast_schedule(now, expected):
    assert fieldwork.next_collection(now)["at"] == expected


def test_copy_has_no_dashes_or_codes(out):
    texts = [out.get("health_note") or ""] + [o["why"] for o in out["off"]]
    for m in out["markets"]:
        for s in m["sources"]:
            texts += [s["name"], s["status_words"], s["detail"] or ""]
    for text in texts:
        assert "—" not in text and "–" not in text and "--" not in text


def test_bigquery_latest_collection_day_reads_the_health_view():
    class Job:
        def result(self):
            return [{"d": dt.date(2026, 9, 30)}]

    class Client:
        calls = []

        def query(self, sql, job_config=None):
            self.calls.append(sql)
            return Job()

    client = Client()
    assert BigQueryStore(client=client).latest_collection_day("2026-10-02") == D30
    assert "v_collection_health_current" in client.calls[0] and "@until" in client.calls[0]


def test_route_serves_fieldwork_and_refuses_a_bad_date(monkeypatch):
    from core.api import app as api_mod

    monkeypatch.setenv("F42_DATA", "fixtures")
    monkeypatch.setenv("UI_PASSCODE", "pw")
    monkeypatch.delenv("F42_AUTH_MODE", raising=False)
    auth.auth_limiter.hits.clear()
    client = TestClient(api_mod.app)
    assert client.get("/api/fieldwork?date=2026-09-30").status_code == 401
    r = client.get("/api/fieldwork?date=2026-09-30", headers={"X-Passcode": "pw"})
    assert r.status_code == 200 and r.json()["date"] == D30
    assert client.get("/api/fieldwork?date=30-09-2026", headers={"X-Passcode": "pw"}).status_code == 400


def test_every_name_is_a_whole_name_not_a_fragment(out):
    """The catalog spells rotation.africa's brand "rotation." with a stop; the page must not print "rotation. Kenya
    chart". No source, member or retired name may open on a lower-case word cut at a stop or end on one."""
    import re

    off = {(o["market"], o["name"]) for o in out["off"]}
    assert ("KE", "rotation.africa Kenya chart") in off
    fragment = re.compile(r"^[a-z]+\.\s|\s[a-z]+\.$|^[^\w@]|\.\s*$")
    names = [o["name"] for o in out["off"]]
    for m in out["markets"]:
        for s in m["sources"]:
            names += [s["name"]] + list(s["members"])
    bad = [n for n in names if fragment.search(n)]
    assert bad == []


def test_every_source_says_what_it_reads(out):
    """One rhythm on the page: every source row carries a description line, so no row stands shorter."""
    missing = [(m["market"], s["series"]) for m in out["markets"] for s in m["sources"] if not s["detail"]]
    assert missing == []


def test_every_retired_local_scrape_says_why_in_plain_words():
    from core.collect import local_sources

    assert set(fieldwork.DROPPED_SCRAPE_WORDS) == set(local_sources.DROPPED_SCRAPES)
    off = {(o["market"], o["name"]): o["why"] for o in fieldwork._off(fieldwork._plan("2026-10-05"), "2026-10-05")}
    assert "refused" in off[("NG", "Nairaland front page")]
    assert all((m, "Audiomack trending") in off for m in ("ZA", "NG", "KE"))
    assert all("_" not in why and "http" not in why for why in off.values())


def test_a_calls_failure_that_names_its_class_reads_as_a_failed_or_unmade_call():
    rows = [{"day": D30, "market": "ZA", "platform": "reddit", "series": "list_reddit", "calls": 1, "calls_ok": 0,
             "items": 0, "valid": False, "invalid_reason": "calls: http_5xx", "located_share": None, "run_id": "r1"},
            {"day": D30, "market": "ZA", "platform": "reddit", "series": "list_reddit", "calls": 0, "calls_ok": 0,
             "items": 0, "valid": False, "invalid_reason": "calls: robots", "located_share": None, "run_id": "r1"}]
    out = fieldwork.build_fieldwork(Patched(collection_health=lambda d: rows), D30, now=NOW)
    reddit = source(out, "ZA", "list_reddit")
    assert reddit["status"] == "failed"
    assert reddit["status_words"] == "Failed: could not be read; no calls were made"
