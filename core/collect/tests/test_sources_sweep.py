"""Tests for core/collect/sources_sweep.py, the candidate sweep for the market source lists."""

import io
import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from core.collect import job, local_sources
from core.collect import sources_sweep as sw
from core.collect.gdelt import OverCap
from core.collect.socialcrawl_client import Result
from core.config.tests.test_hubs import AGE_TERMS, DASHES

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
AS_OF = date(2026, 9, 30)
RUN_DATE = date(2026, 9, 30)


def fixture(key):
    return json.loads((FIXTURES / "sources_sweep.json").read_text(encoding="utf-8"))[key]


def by_key(records):
    return {(r["platform"], r["handle"].lower(), r["market"]): r for r in records}


class FakeGeo:
    def __call__(self, platform, market, *, ext_region=None, home_market=None, profile_location=None,
                 text=None, language=None):
        return (ext_region, 0.9, "ext_region") if ext_region in ("ZA", "NG", "KE") else (None, None, None)


def fake_item_id(kind, raw, platform=None):
    return f"{kind}|{platform}|{str(raw).casefold()}"


# Record shape, drop reasons and rule 1 -----------------------------------------------------------------

def test_record_has_the_fields_and_no_age_field():
    r = sw.record("@SowetoReels", "instagram", "ZA", source="warehouse", why="seen 5 of 7 days")
    assert r["handle"] == "SowetoReels" and r["platform"] == "instagram" and r["market"] == "ZA"
    for name in ("handle", "platform", "market", "kind", "followers", "last_post", "source", "why"):
        assert name in r
    assert r["kind"] in sw.KINDS
    assert not {"age", "age_band", "cohort", "birth_year"} & set(r)


def test_clean_handle_and_platform():
    assert sw.clean_handle("@TYLAOfficial") == "TYLAOfficial"
    assert sw.clean_handle("https://www.instagram.com/tyla/") == "tyla"
    assert sw.clean_handle("bad handle with spaces") is None
    assert sw.clean_handle("a" + "-" * 2 + "b") is None
    assert sw.norm_platform("Twitter") == "x"


@pytest.mark.parametrize("handle,kind", [
    ("instablog9ja", "gossip_entertainment"), ("mzansigossip", "gossip_entertainment"),
    ("SundayTimesZA", "news"), ("Soccer_Laduma", "sport"), ("afrobeatsdaily", "music"),
    ("safaricomplc", "brand"), ("100064873830467", "other"),
])
def test_classify(handle, kind):
    assert sw.classify(handle, platform="facebook") == kind


def test_classify_defaults_creator_on_creator_platforms():
    assert sw.classify("lasizwe", platform="instagram") == "creator"
    assert sw.classify("lasizwe", platform="x") == "other"


@pytest.mark.parametrize("handle,reason", [
    ("stjohnschurchza", "religious"), ("winnerschapelng", "religious"), ("rccgworldwide", "religious"),
    ("GovernmentZA", "government"), ("PresidencyZA", "government"), ("StateHouseKenya", "government"),
    ("gautenggov", "government"), ("NNPCLimited", "government"),
])
def test_religious_and_government_drops(handle, reason):
    assert sw.drop_reason(sw.record(handle, "x", "ZA", source="configs", why="w"), AS_OF) == reason


@pytest.mark.parametrize("handle,name", [
    ("nairobibaptist", None), ("ackstpeters", None), ("stpeterske", None), ("st.pauls_nairobi", None),
    ("saint_joseph_ke", None), ("gospelhubke", None), ("apostolicfaithke", None), ("adventistke", None),
    ("pentecostalke", None), ("page1", "St. Peter's ACK Nyeri"), ("page2", "ACK St Marks"),
])
def test_religious_forms_drop(handle, name):
    r = sw.record(handle, "facebook", "KE", source="brand24", why="w", name=name)
    assert sw.drop_reason(r, AS_OF) == "religious"


@pytest.mark.parametrize("handle", ["stanleyenow", "stonebwoy", "jeanbaptiste_dj", "fastjohnny", "bestpaulo",
                                    "stephenletoo", "stmarketsza", "backstpaul"])
def test_ordinary_names_with_similar_letters_stay(handle):
    assert sw.drop_reason(sw.record(handle, "instagram", "KE", source="configs", why="w"), AS_OF) is None


def test_rule_one_drops_blocked_and_hubs_age_terms():
    for term in AGE_TERMS:
        r = sw.record(f"abc{term.replace(' ', '')}xyz", "instagram", "ZA", source="configs", why="w")
        assert sw.drop_reason(r, AS_OF) == "rule_1", term
    named = sw.record("page1", "facebook", "ZA", source="brand24", why="w", name="Kasi " + AGE_TERMS[4])
    assert sw.drop_reason(named, AS_OF) == "rule_1"


def test_rule_one_uses_gdelt_blocked(monkeypatch):
    seen = []
    monkeypatch.setattr(sw, "blocked", lambda text: seen.append(text) or text == "flagged")
    assert sw.drop_reason(sw.record("flagged", "x", "ZA", source="configs", why="w"), AS_OF) == "rule_1"
    assert "flagged" in seen


def test_dead_only_when_last_post_is_known():
    old = sw.record("page", "instagram", "ZA", source="warehouse", why="w", last_post="2026-07-31")
    fresh = sw.record("page", "instagram", "ZA", source="warehouse", why="w", last_post="2026-08-01")
    unknown = sw.record("page", "instagram", "ZA", source="warehouse", why="w")
    assert sw.drop_reason(old, AS_OF) == "dead"
    assert sw.drop_reason(fresh, AS_OF) is None
    assert sw.drop_reason(unknown, AS_OF) is None


def test_market_rule_keeps_only_the_majority_market():
    records = [
        sw.record("a", "x", "NG", source="warehouse", why="w", sightings=5, total_sightings=7),
        sw.record("a", "x", "ZA", source="warehouse", why="w", sightings=2, total_sightings=7),
        sw.record("tie", "x", "NG", source="warehouse", why="w", sightings=3, total_sightings=6),
        sw.record("tie", "x", "KE", source="warehouse", why="w", sightings=3, total_sightings=6),
    ]
    kept, drops = sw.apply_rules(records, AS_OF, in_use=set())
    assert [(r["handle"], r["market"]) for r in kept] == [("a", "NG")]
    assert drops == Counter({"foreign": 3})


def test_market_rule_sums_sightings_across_origins():
    records = [
        sw.record("b", "x", "ZA", source="warehouse", why="w", sightings=2, total_sightings=4, origin="t1"),
        sw.record("b", "x", "ZA", source="warehouse", why="w", sightings=3, total_sightings=3, origin="t2"),
    ]
    kept, drops = sw.apply_rules(records, AS_OF, in_use=set())
    assert len(kept) == 2 and not drops   # ZA holds 5 of 7


# Source 1: the warehouse -------------------------------------------------------------------------------

def warehouse_query(core, legacy, fail=()):
    calls = []

    def query(sql, params):
        calls.append((sql, params))
        if any(name in sql for name in fail):
            raise OverCap("dry run reports 9000000000 bytes; the cap is 5,000,000,000")
        if "intelligence_42_core.post_observations" in sql:
            return core
        if "trends_v2_dev.enriched_content" in sql:
            return legacy
        return []
    return query, calls


def test_warehouse_sql_is_parameterised_date_bounded_and_social_only():
    queries = sw.warehouse_queries(date(2026, 9, 28), RUN_DATE)
    origins = [origin for origin, _, _ in queries]
    assert origins[0] == "intelligence_42_core"
    assert set(origins[1:]) == {
        "trends_v2_dev.enriched_content", "trends_v2_staging.enriched_content",
        "intelligence_42_sources_staging.enriched_content", "trends_v2_dev.seed_graph",
        "trends_v2_staging.seed_graph"}
    for origin, sql, params in queries:
        assert "2026-" not in sql, origin
        assert "@since" in sql and "@until" in sql, origin
        assert "trend_scores" not in sql and "google" not in sql.lower(), origin
        assert params["min_posts"] >= 2 and params["top"] > 0
        assert set(params["platforms"]) <= {"tiktok", "instagram", "twitter", "x", "youtube", "threads",
                                            "facebook", "reddit", "bluesky"}
        assert "search" not in params["platforms"] and "news" not in params["platforms"]
        if "enriched_content" in origin:
            assert "google" not in " ".join(params["sources"]) and "search" not in " ".join(params["sources"])
            assert "LOWER(e.source) IN UNNEST(@sources)" in sql
    core = queries[0]
    assert "observed_date BETWEEN @since AND @until" in core[1] and "post_date BETWEEN" in core[1]
    assert core[2]["since"] == date(2026, 9, 28) and core[2]["until"] == RUN_DATE


def test_from_warehouse_ranks_applies_market_rule_and_drops():
    query, calls = warehouse_query(fixture("warehouse_core"), fixture("warehouse_legacy"))
    kept, drops, notes = sw.from_warehouse(query, date(2026, 9, 24), RUN_DATE, as_of=AS_OF, in_use=set())
    assert len(calls) == len(sw.warehouse_queries(date(2026, 9, 24), RUN_DATE))
    got = by_key(kept)
    x = got[("x", "gistlovershq", "NG")]
    assert x["why"].startswith("seen 6 of 7 days in NG feeds, 41k engagements")
    assert x["followers"] == 250000 and x["source"] == "warehouse" and x["kind"] == "gossip_entertainment"
    # naijabeatsdaily: NG 5 + 60 of 67 sightings, so ZA's 2 are foreign.
    assert ("instagram", "naijabeatsdaily", "NG") in got and ("instagram", "naijabeatsdaily", "ZA") not in got
    # splitcreator: ZA 3 + 1 of 7 is a majority once the legacy row counts; KE's 3 are foreign.
    assert ("tiktok", "splitcreator", "ZA") in got and ("tiktok", "splitcreator", "KE") not in got
    assert drops == Counter({"foreign": 2, "dead": 1, "religious": 1, "government": 1})
    za_ig = sorted((r for r in kept if r["market"] == "ZA" and r["platform"] == "instagram"
                    and r["origin"] == "intelligence_42_core"), key=lambda r: r["rank"])
    assert [r["handle"] for r in za_ig] == ["sowetoreels", "mzansigossip"]   # 5 days each, more engagement first
    assert not notes


def test_from_warehouse_reports_a_refused_query_and_goes_on():
    query, _ = warehouse_query(fixture("warehouse_core"), [], fail=("trends_v2_staging.enriched_content",))
    kept, _, notes = sw.from_warehouse(query, date(2026, 9, 24), RUN_DATE, as_of=AS_OF, in_use=set())
    assert kept and len(notes) == 1 and "trends_v2_staging.enriched_content" in notes[0]
    assert "refused" in notes[0]


def test_live_warehouse_query_goes_through_the_gdelt_cap_helper(monkeypatch):
    seen = []

    class Job:
        def result(self):
            return [{"market": "ZA"}]

    def checked(client, sql, parameters=()):
        seen.append((client, sql, [p.name for p in parameters]))
        return Job()

    monkeypatch.setattr(sw, "_checked", checked)
    query = sw.bq_query("client")
    rows = query("SELECT 1", {"since": date(2026, 9, 28), "markets": ["ZA"], "top": 5})
    assert rows == [{"market": "ZA"}]
    assert seen == [("client", "SELECT 1", ["since", "markets", "top"])]


# Source 2: the configs ---------------------------------------------------------------------------------

def test_from_configs_marks_what_is_in_use():
    kept, drops = sw.from_configs(as_of=AS_OF)
    got = by_key(kept)
    sunday = got[("x", "sundaytimesza", "ZA")]
    assert sunday["in_use"] and "hubs.yaml x" in sunday["why"] and sunday["source"] == "configs"
    assert got[("instagram", "tyla", "ZA")]["in_use"]
    assert got[("instagram", "instablog9ja", "NG")]["in_use"]            # local_sources IG_GOSSIP
    assert got[("instagram", "instablog9ja", "NG")]["kind"] == "gossip_entertainment"
    assert got[("rss", "https://punchng.com/feed/", "NG")]["in_use"]      # local_sources FEEDS
    assert not got[("tiktok", "kabzadesmall", "ZA")]["in_use"]
    assert "engine/configs/creators/za.yaml (tiktok tier_2)" in got[("tiktok", "kabzadesmall", "ZA")]["why"]
    assert got[("tiktok", "kabzadesmall", "ZA")]["tier"] == "tier_2"
    assert got[("tiktok", "jay_onair", "NG")]["tier"] == "tier_3"
    assert sunday["tier"] == "page"
    assert got[("rss", "https://punchng.com/feed/", "NG")]["tier"] == "page"
    for r in kept:   # the source file name only, never a file:line
        assert not re.search(r"\.(yaml|py):\d", r["why"]), r["why"]
    tyla_yt = got[("youtube", "tylaofficial", "ZA")]
    assert tyla_yt["followers"] == 5870000 and tyla_yt["last_post"] == "2026-04-27"
    assert ("x", "governmentza", "ZA") not in got and drops["government"] >= 3
    assert drops["rule_1"] >= 1
    # Dead counts from the date the registry was probed (28 May 2026), not from today: @Boity last uploaded
    # in 2021, while Tyla's 27 April 2026 upload was inside the 60 days before that probe.
    assert ("youtube", "boity", "ZA") not in got and drops["dead"] >= 1


# Source 3: the Brand24 export --------------------------------------------------------------------------

def test_from_brand24_keeps_single_market_pages_with_evidence():
    kept, drops = sw.from_brand24(io.StringIO("\n".join(fixture("brand24_csv"))), as_of=AS_OF, in_use=set())
    got = {r["handle"]: r for r in kept}
    assert set(got) == {"100000000000001", "kasigossipdaily", "abc123share", "KickoffSoccerZA"}
    assert got["100000000000001"]["market"] == "NG" and got["100000000000001"]["platform"] == "facebook"
    assert got["kasigossipdaily"]["kind"] == "gossip_entertainment"      # 3 rows but seen in August 2026
    assert got["KickoffSoccerZA"]["kind"] == "sport"                    # name read from the URL
    assert got["kasigossipdaily"]["last_post"] == "2026-08-19"
    assert got["100000000000001"]["why"] == "Brand24 tag NG, 14 rows from 2026-06-02 to 2026-08-20"
    assert drops == Counter({"below_threshold": 1, "foreign": 1, "religious": 1, "government": 1, "dead": 1})


@pytest.mark.parametrize("segment", ["share", "groups", "people", "profile.php", "permalink", "permalink.php",
                                     "story.php", "watch", "events", "photo", "photos", "photo.php", "posts",
                                     "videos", "reel", "reels", "pages"])
def test_a_facebook_path_that_is_not_a_page_yields_no_handle(segment):
    assert sw._page_handle(f"https://www.facebook.com/{segment}/abc123") is None
    r = sw.record(segment, "facebook", "ZA", source="brand24", why="w")
    assert sw.drop_reason(r, AS_OF) == "not_a_page"


def test_brand24_share_url_with_a_share_slug_is_dropped():
    rows = ["page_slug,markets,rows_,first_seen,last_seen,sample_url",
            "share,za,40,2026-06-01,2026-08-20,https://www.facebook.com/share/p/xyz123",
            "groups,ke,40,2026-06-01,2026-08-20,https://www.facebook.com/groups/12345",
            "mzansivibes,za,40,2026-06-01,2026-08-20,https://www.facebook.com/share/abc",
            "200000000000002,za,40,2026-06-01,2026-08-20,https://www.facebook.com/permalink.php?id=2"]
    kept, drops = sw.from_brand24(io.StringIO("\n".join(rows)), as_of=AS_OF, in_use=set())
    assert {r["handle"] for r in kept} == {"mzansivibes", "200000000000002"}
    assert drops == Counter({"not_a_page": 2})
    # A pure number reaches a draft only when the configs list it.
    proposal = sw.propose(kept, 1000, {m: {p: 0 for p in sw.PANELS} for m in sw.MARKETS},
                          sw.panel_prices(RUN_DATE))
    assert [c["handle"] for c in proposal["added"]["ZA"]["facebook"]] == ["mzansivibes"]
    listed = kept + [cand("200000000000002", "facebook", "ZA", kind="other", followers=None, source="configs")]
    proposal = sw.propose(listed, 1000, {m: {p: 0 for p in sw.PANELS} for m in sw.MARKETS},
                          sw.panel_prices(RUN_DATE))
    assert {c["handle"] for c in proposal["added"]["ZA"]["facebook"]} == {"mzansivibes", "200000000000002"}


def test_from_brand24_stops_on_an_unexpected_header():
    with pytest.raises(sw.Brand24Header) as caught:
        sw.from_brand24(io.StringIO("page_slug,markets,email\nx,za,a\n"), as_of=AS_OF, in_use=set())
    assert "page_slug,markets,email" in str(caught.value) and "x,za,a" not in str(caught.value)


@pytest.mark.parametrize("handle", ["itsdennyc", "dennyc", "denny_c254", "DennyC", "giantevilclown"])
def test_a_single_short_lowercase_token_with_no_page_word_is_person_shaped(handle):
    assert sw.person_shaped(handle)


@pytest.mark.parametrize("handle", ["mzansimagic", "channelsforum", "kasigossipdaily", "KickoffSoccerZA",
                                    "citizentvkenya", "kissfmkenya", "naijamusichub", "lagosclub", "trendsmedia",
                                    "thebrandcollective", "100000000000001", "averyveryverylongname"])
def test_a_page_word_a_place_word_a_number_or_a_long_name_is_not_person_shaped(handle):
    assert not sw.person_shaped(handle)


def brand24_candidates(rows):
    header = "page_slug,markets,rows_,first_seen,last_seen,sample_url"
    kept, _ = sw.from_brand24(io.StringIO("\n".join([header] + rows)), as_of=AS_OF, in_use=set())
    return kept


def test_only_named_brand24_pages_with_ten_rows_reach_a_draft():
    kept = brand24_candidates([
        "www.defimedia.info,ke,6,2026-07-29,2026-08-12,https://www.facebook.com/www.defimedia.info/",
        "www.bignews.co.ke,ke,40,2026-05-01,2026-08-20,https://www.facebook.com/www.bignews.co.ke/",
        "tukonews.ke,ke,40,2026-05-01,2026-08-20,https://www.facebook.com/tukonews.ke",
        "itsdennyc,ke,1,2026-08-10,2026-08-10,https://www.facebook.com/itsdennyc",
        "wanjikum,ke,25,2026-05-01,2026-08-20,https://www.facebook.com/wanjikum",
        "kasigossipdaily,za,3,2026-08-03,2026-08-19,https://www.facebook.com/kasigossipdaily",
        "mzansimagic,za,12,2026-05-23,2026-08-17,https://www.facebook.com/mzansimagic",
        "channelsforum,ng,12,2026-05-29,2026-08-18,https://www.facebook.com/channelsforum"])
    # Seen in August keeps a page in the candidates file, whatever its row count or shape.
    assert {r["handle"] for r in kept} == {"www.defimedia.info", "www.bignews.co.ke", "tukonews.ke", "itsdennyc",
                                           "wanjikum", "kasigossipdaily", "mzansimagic", "channelsforum"}
    proposal = sw.propose(kept, 1000, {m: {p: 0 for p in sw.PANELS} for m in sw.MARKETS},
                          sw.panel_prices(RUN_DATE))
    added = {c["handle"] for m in sw.MARKETS for c in proposal["added"][m]["facebook"]}
    assert added == {"mzansimagic", "channelsforum"}
    assert proposal["held"] == Counter({"brand24_not_a_page": 3, "brand24_few_rows": 2, "brand24_person_shaped": 1})


def test_a_person_shaped_brand24_page_needs_followers_at_the_floor():
    kept = brand24_candidates(["wanjikum,ke,25,2026-05-01,2026-08-20,https://www.facebook.com/wanjikum"])
    for followers, reason in ((None, "brand24_person_shaped"), (9999, "below_follower_floor"), (10000, None)):
        c = sw.merge_candidates(kept + [cand("wanjikum", "facebook", "KE", followers=followers)])[0]
        assert sw.draft_hold(c) == reason


def test_the_brand24_why_says_the_tag_and_rows_and_never_only():
    kept = brand24_candidates(
        ["www.defimedia.info,ke,6,2026-07-29,2026-08-12,https://www.facebook.com/www.defimedia.info/"])
    assert kept[0]["why"] == "Brand24 tag KE, 6 rows from 2026-07-29 to 2026-08-12"
    assert "only" not in kept[0]["why"]


# Source 4: discovery -----------------------------------------------------------------------------------

def terms():
    return {m: [f"{m.lower()} term {i}" for i in range(1, 9)] for m in sw.MARKETS}


def test_discover_calls_use_priced_routes_and_fit_the_cap():
    calls = sw.discover_calls(terms(), RUN_DATE)
    assert len(calls) == 48
    assert {c["route"] for c in calls} == {"tiktok/search/top", "search/multi"}
    assert sum(c["hold"] for c in calls) == 120 <= sw.DISCOVER_CAP == 150
    multi = next(c for c in calls if c["route"] == "search/multi")
    assert multi["params"]["platforms"] == "instagram,youtube,twitter,facebook" and multi["hold"] == 4


def test_cap_calls_holds_every_call_past_the_cap():
    calls = sw.discover_calls(terms(), RUN_DATE)
    made, held = sw.cap_calls(calls, 100)
    assert sum(c["hold"] for c in made) <= 100
    assert len(made) + len(held) == len(calls) and held
    assert sum(c["hold"] for c in made) + held[0]["hold"] > 100


class ChargingClient:
    def __init__(self, charge):
        self.charge, self.calls = charge, []

    def call(self, route, params, *, method=None, market=None, seed_key=None, lane=None, **kw):
        self.calls.append((route, params, market, seed_key, lane))
        return Result("ok", route, credits_charged=self.charge(route), body={"data": {"items": []}})


def test_run_discover_stops_before_any_call_that_would_pass_150():
    calls = sw.discover_calls(terms(), RUN_DATE)
    client = ChargingClient(lambda route: 9 if route == "search/multi" else 3)
    results, spent, stopped = sw.run_discover(client, calls, sw.DISCOVER_CAP, out=io.StringIO())
    assert spent <= 150
    nxt = calls[len(client.calls)]
    assert spent + nxt["hold"] > 150 or spent + 9 > 150
    assert stopped and len(results) == len(client.calls) < len(calls)
    assert all(lane is None for *_, lane in client.calls)


def test_run_discover_stops_on_a_client_stop():
    class Halting(ChargingClient):
        def call(self, route, params, **kw):
            super().call(route, params, **kw)
            return Result("cap_reached", route, reason="build share: over 150")

    client = Halting(lambda route: 0)
    _, spent, stopped = sw.run_discover(client, sw.discover_calls(terms(), RUN_DATE), 150, out=io.StringIO())
    assert len(client.calls) == 1 and spent == 0 and "cap_reached" in stopped


def test_discover_records_from_parsed_search_bodies():
    bodies = json.loads((FIXTURES / "parse_search.json").read_text(encoding="utf-8"))
    calls = sw.discover_calls({"ZA": ["amapiano"], "NG": [], "KE": []}, RUN_DATE)
    top = next(c for c in calls if c["route"] == "tiktok/search/top")
    multi = next(c for c in calls if c["route"] == "search/multi")
    fetched = datetime(2026, 9, 28, 0, 30, tzinfo=timezone.utc)
    results = [(top, bodies["search_top"]), (multi, bodies["search_multi"])]
    kept, drops = sw.discover_records(results, fetched, item_id_fn=fake_item_id, geo_fn=FakeGeo(),
                                      as_of=AS_OF, in_use=set())
    got = by_key(kept)
    kasi = got[("tiktok", "kasi.beats", "ZA")]
    assert kasi["source"] == "discover" and "tiktok/search/top" in kasi["why"] and "amapiano" in kasi["why"]
    assert kasi["last_post"] == "2026-09-26"
    assert ("instagram", "mzansi.food", "ZA") not in got     # no located post, so no market majority
    assert drops["foreign"] >= 1


def test_top_terms_filter_rule_one_generic_tags_and_duplicates(monkeypatch):
    monkeypatch.setattr(sw, "blocked", lambda text: "flagged" in text)
    rows = [{"market": "ZA", "label": "#Amapiano", "kind": "hashtag", "posts": 50},
            {"market": "ZA", "label": "amapiano", "kind": "topic", "posts": 40},
            {"market": "ZA", "label": "#fyp", "kind": "hashtag", "posts": 30},
            {"market": "ZA", "label": "flagged thing", "kind": "topic", "posts": 20}]
    rows += [{"market": "NG", "label": f"term{i}", "kind": "topic", "posts": 100 - i} for i in range(12)]
    got = sw.top_terms(rows)
    assert got["ZA"] == ["amapiano"]
    assert got["NG"] == [f"term{i}" for i in range(8)] and got["KE"] == []


def test_discover_live_refused_outside_a_cloud_run_job():
    def never(*a, **k):
        raise AssertionError("no network")

    out = io.StringIO()
    code = sw.main(["--discover"], env={}, make_client=never, query=never, out=out)
    assert code == 2 and "Cloud Run" in out.getvalue()


def test_discover_plan_makes_no_network_call():
    def never(*a, **k):
        raise AssertionError("no network")

    out = io.StringIO()
    assert sw.main(["--discover", "--plan", "--run-date", "2026-09-30"], env={}, make_client=never, query=never,
                   out=out) == 0
    text = out.getvalue()
    assert "tiktok/search/top" in text and "search/multi" in text and "cap 150" in text
    assert "total hold 120" in text


def test_discover_from_run_rebuilds_params_by_hash():
    bodies = json.loads((FIXTURES / "parse_search.json").read_text(encoding="utf-8"))
    calls = sw.discover_calls({"ZA": ["amapiano"], "NG": [], "KE": []}, RUN_DATE)
    rows = [{"route": c["route"], "market": c["market"], "seed_key": c["term"], "params_hash": c["phash"],
             "fetched_at": "2026-09-30T08:00:00+00:00",
             "body": json.dumps(bodies["search_top" if c["route"] == "tiktok/search/top" else "search_multi"])}
            for c in calls]
    rows.append({"route": "search/multi", "market": "ZA", "seed_key": "amapiano", "params_hash": "other",
                 "fetched_at": "2026-09-30T08:00:00+00:00", "body": "{}"})
    matched, unmatched = sw.match_run_rows(rows, RUN_DATE)
    assert len(matched) == 2 and unmatched == 1


# Every CLI mode has a form that makes no network call ------------------------------------------------

def test_warehouse_plan_prints_sql_without_a_query():
    def never(*a, **k):
        raise AssertionError("no network")

    out = io.StringIO()
    assert sw.main(["--from-warehouse", "--plan", "--run-date", "2026-09-30"], env={}, query=never, out=out) == 0
    assert "intelligence_42_core.post_observations" in out.getvalue() and "@since" in out.getvalue()


def test_configs_and_brand24_plans_write_nothing(monkeypatch):
    monkeypatch.setattr(sw, "write_candidates", lambda *a, **k: pytest.fail("wrote a file under --plan"))
    out = io.StringIO()
    assert sw.main(["--from-configs", "--plan", "--run-date", "2026-09-30"], env={}, out=out) == 0
    assert "configs:" in out.getvalue()
    path = FIXTURES / "sources_sweep.json"
    monkeypatch.setattr(sw, "open_brand24", lambda p: io.StringIO("\n".join(fixture("brand24_csv"))))
    assert sw.main(["--from-brand24", str(path), "--plan", "--run-date", "2026-09-30"], env={}, out=out) == 0
    assert "brand24: 4 kept" in out.getvalue()


# The candidates file ------------------------------------------------------------------------------------

def test_candidates_lines_replace_only_their_source():
    old = [json.dumps(sw.record("a", "x", "ZA", source="configs", why="w")),
           json.dumps(sw.record("b", "x", "ZA", source="warehouse", why="w"))]
    new = sw.merge_lines(old, "warehouse", [sw.record("c", "x", "NG", source="warehouse", why="w2")])
    handles = [(json.loads(line)["source"], json.loads(line)["handle"]) for line in new]
    assert handles == [("configs", "a"), ("warehouse", "c")]


# Proposal arithmetic ------------------------------------------------------------------------------------

@pytest.mark.usefixtures("collect_share_2000")
def test_morning_totals_read_from_the_collect_plan():
    assert sw.morning_totals(RUN_DATE) == (1830, 34)
    share = job.load_caps()["ENGINE_DAILY"]["collect"]
    budget = sw.budget_for(RUN_DATE)
    assert budget["budget"] == share - 1830 - 34 == 136 and budget["headroom"] == 136
    assert budget["basis"] == (f"collect share {share} minus the morning plan 1864 "
                               "(SocialCrawl phases 1830, local sources 34)")


def test_panel_prices_come_from_priced():
    prices = sw.panel_prices(RUN_DATE)
    assert prices == {"x": 1, "culture_desk": 2, "facebook": 1, "ig_gossip": 2, "feeds": 0}


def cand(handle, platform, market, kind="creator", **kw):
    kw.setdefault("followers", 20000)
    return sw.record(handle, platform, market, source=kw.pop("source", "warehouse"), why=f"why {handle}",
                     kind=kind, **kw)


def test_propose_balances_markets_and_fits_the_budget():
    candidates = [cand("a1", "x", "ZA", days_seen=5), cand("a2", "x", "ZA", days_seen=4),
                  cand("c1", "instagram", "ZA"), cand("b1", "x", "NG"), cand("f1", "facebook", "KE", kind="news"),
                  cand("g1", "instagram", "ZA", kind="gossip_entertainment"),
                  cand("used", "x", "KE", in_use=True), cand("r1", "rss", "NG", kind="news", source="configs")]
    current = {m: {p: 0 for p in sw.PANELS} for m in sw.MARKETS}
    proposal = sw.propose(candidates, 7, current, sw.panel_prices(RUN_DATE))
    added = {(m, p): [c["handle"] for c in proposal["added"][m][p]] for m in sw.MARKETS for p in sw.PANELS
             if proposal["added"][m][p]}
    assert added == {("ZA", "x"): ["a1"], ("ZA", "culture_desk"): ["c1"], ("ZA", "ig_gossip"): ["g1"],
                     ("NG", "x"): ["b1"], ("KE", "facebook"): ["f1"], ("NG", "feeds"): ["r1"]}
    assert proposal["credits"] == {"ZA": 5, "NG": 1, "KE": 1} and proposal["total"] == 7


def test_propose_respects_the_prism_item_limit_and_merges_sources():
    candidates = [cand(f"c{i}", "instagram", "ZA", engagement=100 - i) for i in range(30)]
    candidates.append(cand("c0", "instagram", "ZA", source="brand24"))
    current = {m: {p: 0 for p in sw.PANELS} for m in sw.MARKETS}
    current["ZA"]["culture_desk"] = 12
    proposal = sw.propose(candidates, 1000, current, sw.panel_prices(RUN_DATE))
    desk = proposal["added"]["ZA"]["culture_desk"]
    assert len(desk) == 25 - 12 and proposal["total"] == 2 * 13
    assert desk[0]["handle"] == "c0" and desk[0]["sources"] == ["brand24", "warehouse"]


@pytest.mark.usefixtures("collect_share_2000")
def test_print_proposal_shows_added_credits_per_morning():
    candidates = [cand("a1", "x", "ZA"), cand("c1", "instagram", "NG")]
    out = io.StringIO()
    share = job.load_caps()["ENGINE_DAILY"]["collect"]
    budget = sw.budget_for(RUN_DATE)
    proposal = sw.propose(candidates, budget["budget"], sw.current_sizes(), sw.panel_prices(RUN_DATE))
    proposal.update(budget)
    sw.print_proposal(proposal, RUN_DATE, out)
    text = out.getvalue()
    assert "SocialCrawl phases 1830, local sources 34" in text
    assert f"budget {share - 1864}: collect share {share} minus the morning plan 1864" in text
    assert "added credits per morning: 3" in text and "over" not in text


@pytest.mark.usefixtures("collect_share_2000")
def test_the_default_budget_is_the_collect_share_left_and_never_exceeded():
    # Every paid panel in every market offers more than the 136 credits left, so the budget is what stops it.
    candidates = [cand(f"{platform}{kind}{i}", platform, market, kind=kind) for market in sw.MARKETS
                  for platform, kind in (("x", "creator"), ("facebook", "news"), ("instagram", "creator"),
                                         ("instagram", "gossip_entertainment")) for i in range(25)]
    budget = sw.budget_for(RUN_DATE)["budget"]
    assert budget == job.load_caps()["ENGINE_DAILY"]["collect"] - 1864
    proposal = sw.propose(candidates, budget, sw.current_sizes(), sw.panel_prices(RUN_DATE))
    assert 0 < proposal["total"] <= budget == 136


def test_only_pages_outlets_and_public_creators_reach_a_draft():
    rows = [cand("nofollow", "instagram", "ZA", followers=None),
            cand("small", "instagram", "ZA", followers=9999),
            cand("atfloor", "instagram", "ZA", followers=10000),
            cand("verifiedone", "tiktok", "ZA", followers=None, verified=True),
            cand("mentioned", "x", "ZA", followers=None, source="discover"),
            cand("100000000000001", "facebook", "ZA", kind="other", followers=None, source="brand24"),
            cand("kasigossipdaily", "facebook", "ZA", followers=None, source="brand24", posts=14),
            cand("100064817165112", "facebook", "ZA", kind="other", followers=None, source="configs"),
            cand("https://example.co.za/feed/", "rss", "ZA", kind="news", followers=None, source="configs")]
    proposal = sw.propose(rows, 1000, {m: {p: 0 for p in sw.PANELS} for m in sw.MARKETS},
                          sw.panel_prices(RUN_DATE))
    added = {c["handle"] for m in sw.MARKETS for p in sw.PANELS for c in proposal["added"][m][p]}
    assert added == {"atfloor", "verifiedone", "kasigossipdaily", "100064817165112", "https://example.co.za/feed/"}
    assert proposal["held"] == Counter({"followers_unknown": 2, "below_follower_floor": 1, "brand24_numeric_id": 1})
    assert sw.FOLLOWER_FLOOR == {"instagram": 10000, "tiktok": 10000, "x": 10000, "youtube": 10000,
                                 "facebook": 10000, "threads": 10000}


def test_configs_tier_3_enters_a_draft_only_with_known_followers_at_the_floor():
    rows = [cand("____tiana27", "instagram", "NG", followers=None, source="configs", tier="tier_3"),
            cand("smallfan", "instagram", "NG", followers=9999, source="configs", tier="tier_3"),
            cand("bigfan", "instagram", "NG", followers=10000, source="configs", tier="tier_3"),
            cand("tierone", "instagram", "NG", followers=None, source="configs", tier="tier_1"),
            cand("tiertwo", "tiktok", "NG", followers=None, source="configs", tier="tier_2")]
    proposal = sw.propose(rows, 1000, {m: {p: 0 for p in sw.PANELS} for m in sw.MARKETS},
                          sw.panel_prices(RUN_DATE))
    added = {c["handle"] for c in proposal["added"]["NG"]["culture_desk"]}
    assert added == {"bigfan", "tierone", "tiertwo"}
    assert proposal["held"] == Counter({"followers_unknown": 1, "below_follower_floor": 1})


def test_a_warehouse_follower_count_lets_a_tier_3_handle_in():
    rows = [cand("fanpage", "instagram", "NG", followers=None, source="configs", tier="tier_3"),
            cand("fanpage", "instagram", "NG", followers=50000, source="warehouse")]
    proposal = sw.propose(rows, 1000, {m: {p: 0 for p in sw.PANELS} for m in sw.MARKETS},
                          sw.panel_prices(RUN_DATE))
    assert [c["handle"] for c in proposal["added"]["NG"]["culture_desk"]] == ["fanpage"]


def test_queues_rank_by_tier_then_engagement_and_never_alphabetically():
    rows = [cand("____tiana27", "instagram", "NG", source="configs", tier="tier_2"),
            cand("__aa", "instagram", "NG", source="configs", tier="tier_2", engagement=10),
            cand("tianaofficial", "instagram", "NG", source="configs", tier="tier_2"),
            cand("zlayer", "instagram", "NG", source="configs", tier="tier_1"),
            cand("zzbusy", "instagram", "NG", source="configs", tier="tier_2", engagement=500),
            cand("_fanpage_", "instagram", "NG", source="configs", tier="tier_3", followers=90000),
            cand("_pagex", "x", "NG", source="configs", tier="page", kind="news"),
            cand("__loud", "x", "NG", source="warehouse", kind="news", days_seen=7, engagement=9000)]
    proposal = sw.propose(rows, 1000, {m: {p: 0 for p in sw.PANELS} for m in sw.MARKETS},
                          sw.panel_prices(RUN_DATE))
    desk = [c["handle"] for c in proposal["added"]["NG"]["culture_desk"]]
    assert desk == ["zlayer", "zzbusy", "__aa", "tianaofficial", "____tiana27", "_fanpage_"]
    assert [c["handle"] for c in proposal["added"]["NG"]["x"]] == ["_pagex", "__loud"]


def test_the_candidates_file_defaults_outside_the_repo():
    assert ROOT not in sw.CANDIDATES.resolve().parents


# Draft writing ------------------------------------------------------------------------------------------

def real_hubs():
    return (ROOT / "core/config/hubs.yaml").read_text(encoding="utf-8")


def real_local():
    return (ROOT / "core/collect/local_sources.py").read_text(encoding="utf-8")


def undrafted_hubs():
    """hubs.yaml as it reads with no sweep draft, whether or not a draft sits on disk."""
    text = real_hubs()
    cut = text.find(sw.HUBS_MARK)
    base = text if cut < 0 else text[:cut].rstrip("\n") + "\n"
    assert sw.HUBS_MARK not in base and not re.search(r"(?m)^draft:", base)
    return base


def undrafted_local():
    """local_sources.py as it reads with no sweep draft, whether or not a draft sits on disk."""
    base = re.sub(r"\n\n" + re.escape(sw.LOCAL_BEGIN) + r".*?" + re.escape(sw.LOCAL_END) + r"\n", "", real_local(),
                  flags=re.S)
    assert sw.LOCAL_BEGIN not in base and sw.LOCAL_END not in base
    return base


def sample_proposal():
    candidates = [cand("gistlovershq", "x", "NG", kind="gossip_entertainment"),
                  cand("sowetoreels", "instagram", "ZA"),
                  cand("100000000000001", "facebook", "NG", kind="other", source="brand24"),
                  cand("mzansigossip", "instagram", "ZA", kind="gossip_entertainment"),
                  cand("https://example.co.za/feed/", "rss", "ZA", kind="news", source="configs", name="Example")]
    return sw.propose(candidates, 344, sw.current_sizes(), sw.panel_prices(RUN_DATE))


@pytest.mark.usefixtures("collect_share_2000")
def test_draft_leaves_the_plan_totals_unchanged():
    hubs = undrafted_hubs()
    local = undrafted_local()
    proposal = sample_proposal()
    new_hubs = sw.render_hubs_draft(hubs, proposal, RUN_DATE)
    new_local = sw.render_local_draft(local, proposal, RUN_DATE)
    assert new_hubs != hubs and new_local != local
    before, after = sw.check_draft(hubs, local, new_hubs, new_local, RUN_DATE)
    assert before == after == (1830, 34)
    config = {"markets": job.load_config()["markets"], "hubs": yaml.safe_load(new_hubs)}
    assert job.plan(RUN_DATE, config=config, reels={})["total"] == 1830
    doc = yaml.safe_load(new_hubs)
    assert doc["markets"] == yaml.safe_load(hubs)["markets"]
    ng_x = doc["draft"]["markets"]["ng"]["x"]
    assert ng_x[0]["handle"] == "gistlovershq" and ng_x[0]["why"] == "why gistlovershq"
    assert doc["draft"]["markets"]["ng"]["facebook"][0]["handle"] == "100000000000001"
    module = sw.load_module(new_local)
    assert module.IG_GOSSIP == local_sources.IG_GOSSIP
    assert [(f.source, f.market, f.url) for f in module.FEEDS] == [(f.source, f.market, f.url)
                                                                   for f in local_sources.FEEDS]
    assert module.IG_GOSSIP_DRAFT["ZA"][0]["handle"] == "mzansigossip"
    assert module.FEEDS_DRAFT[0]["url"] == "https://example.co.za/feed/"
    assert local_sources.total_hold(module.plan(RUN_DATE)) == 34


def test_draft_rewrite_replaces_the_old_draft():
    hubs = undrafted_hubs()
    local = undrafted_local()
    once = sw.render_hubs_draft(hubs, sample_proposal(), RUN_DATE)
    twice = sw.render_hubs_draft(once, sample_proposal(), RUN_DATE)
    assert once == twice and once.startswith(hubs.rstrip("\n"))
    local_once = sw.render_local_draft(local, sample_proposal(), RUN_DATE)
    assert sw.render_local_draft(local_once, sample_proposal(), RUN_DATE) == local_once
    # Whatever draft the real files hold now, a rewrite over them replaces it whole (force: colleagues may have
    # edited it by hand).
    assert sw.render_hubs_draft(real_hubs(), sample_proposal(), RUN_DATE, force=True) == once
    assert sw.render_local_draft(real_local(), sample_proposal(), RUN_DATE, force=True) == local_once


def test_draft_text_keeps_the_hubs_checks():
    hubs = undrafted_hubs()
    proposal = sample_proposal()
    proposal["added"]["ZA"]["x"].append({**cand("dashy", "x", "ZA"), "why": "one " + chr(0x2014) + " two",
                                         "sources": ["warehouse"]})
    text = sw.render_hubs_draft(hubs, proposal, RUN_DATE).lower()
    for term in AGE_TERMS:
        assert term not in text
    for dash in DASHES[:2]:   # the module's own CLI flags are double hyphens
        assert dash not in text


def test_check_draft_refuses_a_draft_that_changes_the_plan():
    hubs = undrafted_hubs()
    local = undrafted_local()
    line = '      - {handle: IOL, platform: x, kind: news, source: "engine/configs/sources.yaml:1142"}\n'
    changed = hubs.replace(line, "")
    assert changed != hubs
    with pytest.raises(sw.DraftChangesPlan):
        sw.check_draft(hubs, local, changed, local, RUN_DATE)


@pytest.mark.usefixtures("collect_share_2000")
def test_write_draft_plan_writes_nothing(monkeypatch):
    monkeypatch.setattr(sw, "read_candidates", lambda path: [cand("gistlovershq", "x", "NG")])
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: pytest.fail("wrote a file under --plan"))
    out = io.StringIO()
    assert sw.main(["--write-draft", "--plan", "--run-date", "2026-09-30"], env={}, out=out) == 0
    assert "plan totals unchanged: 1830 and local 34" in out.getvalue()


def test_module_holds_no_age_terms():
    text = (ROOT / "core/collect/sources_sweep.py").read_text(encoding="utf-8").lower()
    for term in AGE_TERMS:
        assert term not in text, term
    for dash in DASHES[:2]:   # the module's own CLI flags are double hyphens
        assert dash not in text


def test_the_draft_says_its_budget_and_warns_when_over_the_collect_share():
    hubs = undrafted_hubs()
    proposal = sample_proposal()
    proposal.update(basis="collect share 800 minus the morning plan 671", headroom=129)
    doc = yaml.safe_load(sw.render_hubs_draft(hubs, proposal, RUN_DATE))
    assert doc["draft"]["budget"] == proposal["budget"]
    assert doc["draft"]["budget_basis"] == "collect share 800 minus the morning plan 671"
    assert "warning" not in doc["draft"]
    proposal.update(headroom=3)
    text = sw.render_hubs_draft(hubs, proposal, RUN_DATE)
    assert "# WARNING:" in text and "over the collect share" in yaml.safe_load(text)["draft"]["warning"]


def test_a_hand_edited_draft_is_not_overwritten_without_force():
    hubs = undrafted_hubs()
    local = undrafted_local()
    once = sw.render_hubs_draft(hubs, sample_proposal(), RUN_DATE)
    edited = once.replace("why gistlovershq", "checked by the Lagos desk")
    assert edited != once
    with pytest.raises(sw.DraftEdited):
        sw.render_hubs_draft(edited, sample_proposal(), RUN_DATE)
    assert sw.render_hubs_draft(edited, sample_proposal(), RUN_DATE, force=True) == once
    local_once = sw.render_local_draft(local, sample_proposal(), RUN_DATE)
    local_edited = local_once.replace('"handle": "mzansigossip"', '"handle": "mzansigossip_za"')
    assert local_edited != local_once
    with pytest.raises(sw.DraftEdited):
        sw.render_local_draft(local_edited, sample_proposal(), RUN_DATE)
    assert sw.render_local_draft(local_edited, sample_proposal(), RUN_DATE, force=True) == local_once
