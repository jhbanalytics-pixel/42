"""Whole-store breadth for Ask (Albert, 4 October: answers drew on about 20 accounts while the store held thousands of
posts). Code counts every stored post in the market and window per platform, per sound and per hashtag; the writer
leads claims with those totals and cites posts as samples. Run on DuckDB fixtures, transpiled from BigQuery."""

import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from core.agent.context import RunContext
from core.agent.tests.test_history import DuckWarehouse, assert_guarded
from core.agent.tools.warehouse import (STORE_TOOL, TIKTOK_MUSIC_URL, store_breadth, store_totals)

WINDOW = (date(2026, 9, 28), date(2026, 10, 4))
TITLED = "7400000000000000001"
UNTITLED = "7400000000000000002"
SIGHTING = [{"source_market": "ZA", "source_region": "ZA", "route": "tiktok/trending",
             "protocol": "tiktok/trending?region=ZA", "observed_at": datetime(2026, 9, 30), "obs_date": date(2026, 9, 30)}]


def _posts():
    """Many more posts and creators than any sample: 30 TikTok creators use the titled sound, 12 the untitled one."""
    rows = []
    for i in range(30):  # located in ZA, one post each, captions on all but the first
        rows.append((f"obs1_t{i:03d}", "tiktok", f"https://www.tiktok.com/@za_{i}/video/{i}", f"za_{i}",
                     datetime(2026, 9, 29, 8, i), date(2026, 9, 29), "ZA", "ext_region",
                     None if i == 0 else f"dance {i}", ["amapiano", "fyp"], TITLED, 100 + i))
    for i in range(12):  # seen in ZA's feeds, no located market; the earliest post has no caption
        rows.append((f"obs1_u{i:03d}", "tiktok", f"https://www.tiktok.com/@feed_{i}/video/{i}", f"feed_{i}",
                     datetime(2026, 9, 30, 9, i), date(2026, 9, 30), None, None,
                     None if i == 0 else f"challenge {i}", ["gqomchallenge"], UNTITLED, 50 + i))
    rows.append(("obs1_ng", "tiktok", "https://www.tiktok.com/@ng/video/1", "ng_1", datetime(2026, 9, 30),
                 date(2026, 9, 30), "NG", "ext_region", "lagos", ["afrobeats"], TITLED, 999))
    rows.append(("obs1_old", "tiktok", "https://www.tiktok.com/@za_0/video/0", "za_0", datetime(2026, 9, 20),
                 date(2026, 9, 20), "ZA", "ext_region", "old", ["amapiano"], TITLED, 5))
    rows.append(("obs1_x", "twitter", "https://x.com/za/status/1", "za_x", datetime(2026, 10, 1),
                 date(2026, 10, 1), "ZA", "ext_region", "Kaizer Chiefs", ["kaizerchiefs"], None, 7))
    return rows


@pytest.fixture
def con():
    import duckdb  # test-only

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, "
                "creator_id VARCHAR, published_at TIMESTAMP, post_date DATE, geo_market VARCHAR, geo_source VARCHAR, "
                "text VARCHAR, hashtags VARCHAR[], sound_id VARCHAR, engagement BIGINT)")
    con.execute("CREATE TABLE intelligence_42_core.creators (creator_id VARCHAR, platform VARCHAR, handle VARCHAR, "
                "home_market VARCHAR)")
    con.execute("CREATE TABLE intelligence_42_core.v_post_source_markets (post_id VARCHAR, "
                "source_sightings STRUCT(source_market VARCHAR, source_region VARCHAR, route VARCHAR, "
                "protocol VARCHAR, observed_at TIMESTAMP, obs_date DATE)[])")
    con.execute("CREATE TABLE intelligence_42_core.cultural_map (item_id VARCHAR, kind VARCHAR, canonical_key VARCHAR, "
                "label VARCHAR, valid_to TIMESTAMP)")
    con.executemany("INSERT INTO intelligence_42_core.posts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", _posts())
    con.executemany("INSERT INTO intelligence_42_core.creators VALUES (?, 'tiktok', ?, NULL)",
                    [(f"za_{i}", f"za_{i}") for i in range(30)] + [(f"feed_{i}", f"feed_{i}") for i in range(12)])
    con.executemany("INSERT INTO intelligence_42_core.v_post_source_markets VALUES (?, ?)",
                    [(f"obs1_u{i:03d}", SIGHTING) for i in range(12)])
    con.executemany("INSERT INTO intelligence_42_core.cultural_map VALUES (?, 'sound', ?, ?, ?)", [
        ("i_titled", f"tiktok:{TITLED}", "Moyongcwele by Nontokozo Mkhize", None),
        ("i_untitled", f"tiktok:{UNTITLED}", UNTITLED, None),  # the item map stores an unnamed sound by its id
        ("i_old", f"tiktok:{TITLED}", "Old label", datetime(2026, 1, 1)),
    ])
    return con


@pytest.fixture
def ctx():
    return RunContext(run_id="run_breadth", tier="T1", as_of=datetime(2026, 10, 4, 20, 0), market="ZA",
                      window_start=WINDOW[0], window_end=WINDOW[1])


def _rows(ctx, qid):
    return ctx.queries[qid]["rows"]


def test_totals_count_every_stored_post_in_the_market_and_window_per_platform(ctx, con):
    wh = DuckWarehouse(con)
    out = store_breadth(ctx, wh)
    assert_guarded(wh)
    totals = {r["platform"]: r for r in _rows(ctx, out["totals"])}
    # 30 located + 12 seen in ZA feeds on TikTok; the NG post and the post before the window are not counted
    assert totals["tiktok"]["posts"] == 42 and totals["tiktok"]["creators"] == 42
    assert totals["tiktok"]["located_posts"] == 30 and totals["tiktok"]["located_creators"] == 30
    assert totals["twitter"]["posts"] == 1
    assert all(ctx.queries[q]["tool"] == STORE_TOOL for q in out.values())
    assert all("Whole-store" in ctx.queries[q]["purpose"] for q in out.values())
    assert not any(ch.isdigit() for q in out.values() for ch in ctx.queries[q]["purpose"])


def test_sound_rows_count_the_whole_store_and_name_an_untitled_sound_without_inventing_a_title(ctx, con):
    out = store_breadth(ctx, DuckWarehouse(con))
    sounds = {r["sound_id"]: r for r in _rows(ctx, out["sounds"])}
    titled, untitled = sounds[TITLED], sounds[UNTITLED]
    assert (titled["sound_title"], titled["posts"], titled["creators"]) == ("Moyongcwele by Nontokozo Mkhize", 30, 30)
    assert untitled["sound_title"] is None  # a label equal to the id is no title
    assert (untitled["posts"], untitled["creators"], untitled["located_posts"]) == (12, 12, 0)
    assert untitled["earliest_creator"] == "feed_0"
    assert untitled["sound_link"] == TIKTOK_MUSIC_URL + UNTITLED == "https://www.tiktok.com/music/_-" + UNTITLED
    # samples: the earliest post (so its handle is a stored post's) and the two most engaged citable posts
    assert sorted(untitled["sample_post_ids"]) == ["obs1_u000", "obs1_u010", "obs1_u011"]
    assert sorted(titled["sample_post_ids"]) == ["obs1_t000", "obs1_t028", "obs1_t029"]
    assert [r["sound_id"] for r in _rows(ctx, out["sounds"])] == [TITLED, UNTITLED]  # most creators first


def test_hashtag_rows_are_per_platform_with_one_citable_sample(ctx, con):
    out = store_breadth(ctx, DuckWarehouse(con))
    tags = {(r["platform"], r["hashtag"]): r for r in _rows(ctx, out["hashtags"])}
    assert tags[("tiktok", "amapiano")]["creators"] == 30 and tags[("tiktok", "gqomchallenge")]["posts"] == 12
    assert tags[("tiktok", "amapiano")]["sample_post_ids"] == ["obs1_t029"]
    assert ("tiktok", "afrobeats") not in tags  # the NG post is outside the market


def test_a_named_platform_limits_every_count_and_binds_the_stored_x_name(ctx, con):
    wh = DuckWarehouse(con)
    out = store_breadth(ctx, wh, platforms=["x"])
    assert {r["platform"] for r in _rows(ctx, out["totals"])} == {"twitter"}
    assert _rows(ctx, out["sounds"]) == []
    sql, params = wh.runs[0]
    assert "p.platform IN (@platform_0, @platform_1)" in sql
    assert {params["platform_0"], params["platform_1"]} == {"x", "twitter"}


def test_store_totals_feed_the_meta_line(ctx, con):
    out = store_breadth(ctx, DuckWarehouse(con))
    assert store_totals(ctx, out["totals"]) == {"posts": 43, "creators": 43, "located_posts": 31,
                                                 "located_creators": 31, "platforms": 2, "query_id": out["totals"]}
    assert store_totals(ctx, None) is None


def test_no_market_counts_the_whole_store(con):
    ctx = RunContext(run_id="r", tier="T1", as_of=datetime(2026, 10, 4, 20, 0), window_start=WINDOW[0],
                     window_end=WINDOW[1])
    out = store_breadth(ctx, DuckWarehouse(con))
    totals = {r["platform"]: r for r in _rows(ctx, out["totals"])}
    assert totals["tiktok"]["posts"] == 43 and totals["tiktok"]["located_posts"] == 31


# The gate: the whole-store counts run once before the writer, lead the pack and feed the meta line

from core.agent import ask, writer  # noqa: E402
from core.agent.tests.test_ask import Harness, WRITER_SCHEMA  # noqa: E402
from core.agent.tests.test_writer_showable_evidence import (FixedModel, _context, _output,  # noqa: E402
                                                            _write)

STORE_TOTALS = [{"platform": "tiktok", "posts": 1108, "creators": 852, "located_posts": 477, "located_creators": 332}]
STORE_SOUNDS = [{"platform": "tiktok", "sound_id": UNTITLED, "sound_title": None, "posts": 64, "creators": 51,
                 "located_posts": 20, "located_creators": 17, "earliest_creator": "feed_0",
                 "sound_link": TIKTOK_MUSIC_URL + UNTITLED, "sample_post_ids": ["obs1_sample"]}]


def fake_store_counts(calls, fail=False):
    def store_counts(ctx, warehouse, platforms):
        calls.append({"platforms": platforms, "queries_before": list(ctx.queries)})
        if fail:
            raise RuntimeError("bytes billed")
        out = {}
        for name, rows in (("totals", STORE_TOTALS), ("sounds", STORE_SOUNDS)):
            qid, _ = ctx.record_query(f"SELECT 1 FROM intelligence_42_core.posts p -- {name}", {}, rows,
                                      f"Whole-store {name} per platform: every stored post in South Africa")
            ctx.queries[qid]["tool"] = STORE_TOOL
            out[name] = qid
        return out
    return store_counts


def test_the_gate_counts_the_whole_store_once_and_the_meta_line_carries_its_totals():
    calls = []
    h = Harness()
    h.deps.store_counts = fake_store_counts(calls)
    out = h.run(question="Which sounds are rising on TikTok in South Africa this week?")
    assert len(calls) == 1 and calls[0]["platforms"] == ["tiktok"]  # exactly once per run
    store = out["run"]["store"]
    assert store == {"posts": 1108, "creators": 852, "located_posts": 477, "located_creators": 332, "platforms": 1,
                     "query_id": store["query_id"]}
    # the count's queries are recorded after research's, so research keeps its ids
    receipts = out["query_receipts"]
    store_ids = [int(q[2:]) for q, r in receipts.items() if r["purpose"].startswith("Whole-store")]
    research_ids = [int(q[2:]) for q, r in receipts.items() if not r["purpose"].startswith(("Whole-store", "fetch_posts"))]
    assert len(store_ids) == 2 and store["query_id"] in receipts
    assert research_ids and max(research_ids) < min(store_ids)
    user = next(c["user"] for c in h.model.calls if c["schema"] is WRITER_SCHEMA)
    assert user.index("Whole-store totals") < user.index("\npost {")  # the store counts lead the pack
    assert any(e["event"] == "step" and e["text"].startswith("Counting every stored post") for e in h.events)


def test_a_failed_store_count_says_so_and_the_answer_still_runs():
    h = Harness()
    h.deps.store_counts = fake_store_counts([], fail=True)
    out = h.run()
    assert ask.STORE_FAILED in out["run"]["notices"] and "store" not in out["run"]
    assert out["answer"]["claims"]


def test_without_a_store_counter_the_run_object_is_unchanged():
    out = Harness().run()
    assert "store" not in out["run"]


def test_live_deps_count_the_whole_store():
    import inspect
    assert "store_counts=store_breadth" in inspect.getsource(ask._default_deps)


def test_sample_post_ids_in_store_rows_are_fetched_as_citable_posts():
    from core.agent.context import RunContext as Ctx
    ctx = Ctx(run_id="r", tier="T1", as_of=datetime(2026, 10, 4, 20, 0), market="ZA")
    qid, _ = ctx.record_query("SELECT 1", {}, STORE_SOUNDS, "Whole-store sounds")
    ctx.queries[qid]["tool"] = STORE_TOOL
    assert ask.listed_post_ids(ctx) == ["obs1_sample"]


@pytest.mark.parametrize("question,expected", [
    ("Which sounds are rising on TikTok in South Africa this week?", ["tiktok"]),
    ("What are people on X saying about Kaizer Chiefs?", ["x"]),
    ("Which posts from X mention the derby?", ["x"]),
    ("What is X (Twitter) saying about load shedding?", ["x"]),
    ("Is brand X trending in South Africa?", []),
    ("What do Generation X fans post about amapiano?", []),
    ("What is X saying about Kaizer Chiefs?", []),
    ("Which clinics on X-ray waiting lists are posting?", []),
    ("tweets and Instagram reels about amapiano", ["instagram", "x"]),
    ("What is behind amapiano in South Africa this week?", []),
    ("which comment threads mention load shedding, x or y?", []),
])
def test_a_question_that_names_platforms_limits_the_store_counts_to_them(question, expected):
    assert ask.question_platforms(question) == expected


# The writer pack is bounded, and the store counts go first

def _big_context(posts, text_chars, queries):
    from core.agent.context import RunContext as Ctx
    from core.agent.tools.warehouse import _store
    ctx = Ctx(run_id="r", tier="T1", as_of=datetime(2026, 10, 4, 20, 0), market="ZA",
              window_start=WINDOW[0], window_end=WINDOW[1])
    for i in range(posts):
        _store(ctx, {"post_id": f"obs1_{i:032d}", "platform": "tiktok", "url": f"https://t.test/{i}",
                     "handle": f"h{i}", "published_at": datetime(2026, 10, 1, 9, 0), "post_date": date(2026, 10, 1),
                     "geo_market": "ZA", "geo_source": "ext_region", "text": "w" * text_chars, "views": 1})
    for i in range(queries):
        ctx.record_query("SELECT 1", {}, [{"n": j, "pad": "p" * 200} for j in range(60)], f"research count {i}")
    qid, _ = ctx.record_query("SELECT 2", {}, STORE_TOTALS, "Whole-store totals per platform")
    ctx.queries[qid]["tool"] = STORE_TOOL
    return ctx


def test_the_writer_pack_is_capped_in_posts_and_bytes_and_fits_the_writer_hold():
    ctx = _big_context(posts=400, text_chars=2000, queries=30)
    model = FixedModel(_output([]))
    _write(model, ctx)
    user, system = model.calls[0]["user"], model.calls[0]["system"]
    assert user.count("\npost {") <= writer.MAX_PACK_POSTS
    assert writer._input_token_upper_bound(system, user, WRITER_SCHEMA) <= ask.WRITER_INPUT_TOKENS
    assert ctx.writer_pack_left_out["posts"] >= 400 - writer.MAX_PACK_POSTS and ctx.writer_pack_left_out["queries"]
    assert "The pack is bounded" in user
    pack = user.split("Evidence pack:", 1)[1]
    assert pack.lstrip().startswith("query ") and "Whole-store totals" in pack.split("\npost {", 1)[0]


def test_a_small_pack_is_whole_and_says_nothing_about_bounds():
    ctx = _big_context(posts=3, text_chars=50, queries=2)
    model = FixedModel(_output([]))
    _write(model, ctx)
    assert ctx.writer_pack_left_out == {"posts": 0, "queries": 0}
    assert "The pack is bounded" not in model.calls[0]["user"]
    assert model.calls[0]["user"].count("\npost {") == 3


# Retrieved posts that cannot be cited: the gap says which fields they lacked, and stays the code's

def test_the_uncitable_post_gap_names_the_missing_fields_and_stays_a_code_gap():
    ctx = _context(include_good=True)  # two stored posts with no handle
    draft, _ = _write(FixedModel(_output([])), ctx)
    gap = next(g for g in draft["gaps"] if g["what"] == "Some posts found could not be quoted")
    assert gap["why"] == ("some stored posts had no author handle, which every quoted post needs, so they were left "
                          "out of the answer")
    assert writer.writer_gaps(draft["gaps"]) == []
    assert writer.unshowable_gap(["text", "url"])["why"].startswith("some stored posts had no link or caption text,")
    assert writer.unshowable_gap([]) == writer.UNSHOWABLE_EVIDENCE_GAP


def test_the_writer_takes_whole_store_counts_and_names_untitled_sounds_from_cited_usage():
    system = writer.WRITER_SYSTEM.format(days=7)
    assert "Queries whose purpose starts 'Whole-store' count every stored post" in system
    assert "how many there are is never a count" in system
    assert "never write the id or its link and never make up a title" in system
    assert "earliest_creator matches a handle from a cited post that uses the sound" in system
    assert "exactly as in 'seen in South Africa's feeds'" in system and "located in South Africa or" not in system


def test_the_feed_wording_the_writer_is_given_is_the_wording_k3_accepts():
    from core.agent import checks
    from core.agent.tools.warehouse import _store
    ctx = _context(include_good=False)
    feed = _store(ctx, {"post_id": "obs1_feed", "platform": "tiktok", "url": "https://t.test/feed", "handle": "feed_0",
                        "published_at": datetime(2026, 9, 30, 9, 0), "post_date": date(2026, 9, 30),
                        "geo_market": None, "text": "challenge", "home_market": None,
                        "source_sightings": [{"source_market": "ZA"}]}, allow_context_market=False)
    assert feed["source_market"] == "ZA" and not feed.get("located_market")
    stored = {"obs1_feed": ctx.evidence["obs1_feed"]}
    shown = "An untitled sound was used by 12 creators, seen in South Africa's feeds."
    assert checks._source_scope_problems(shown, list(stored.items())) == []
    told = "An untitled sound was used by 12 creators, located in South Africa or seen in its feeds."
    assert checks._source_scope_problems(told, list(stored.items()))  # the old wording K3 cut


def test_the_listed_sounds_and_hashtags_are_counted_again_over_the_days_before_the_window(ctx, con):
    # Two ZA posts in the seven days before the window use the titled sound and #amapiano; one is a day too early.
    con.executemany("INSERT INTO intelligence_42_core.posts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        ("obs1_b1", "tiktok", "https://www.tiktok.com/@za_1/video/b1", "za_1", datetime(2026, 9, 22), date(2026, 9, 22),
         "ZA", "ext_region", "before", ["amapiano"], TITLED, 3),
        ("obs1_b2", "tiktok", "https://www.tiktok.com/@za_2/video/b2", "za_2", datetime(2026, 9, 27), date(2026, 9, 27),
         "ZA", "ext_region", "before", ["Amapiano"], TITLED, 3),
    ])
    wh = DuckWarehouse(con)
    out = store_breadth(ctx, wh)

    assert_guarded(wh)
    query = ctx.queries[out["before"]]
    assert query["tool"] == STORE_TOOL and query["purpose"].startswith("Whole-store")
    assert "before the window" in query["purpose"] and not any(ch.isdigit() for ch in query["purpose"])
    rows = {(r["kind"], r["sound_id"] or r["hashtag"]): r for r in query["rows"]}
    # obs1_old (20 September) is outside the seven days before; the untitled sound and #gqomchallenge had no posts
    assert rows[("sound", TITLED)]["posts"] == 2 and rows[("sound", TITLED)]["creators"] == 2
    assert rows[("hashtag", "amapiano")]["posts"] == 2 and rows[("hashtag", "amapiano")]["located_posts"] == 2
    assert ("sound", UNTITLED) not in rows and ("hashtag", "gqomchallenge") not in rows
    assert all(r["platform"] == "tiktok" for r in query["rows"])


def test_no_before_window_query_when_the_window_lists_no_sound_or_hashtag(ctx, con):
    con.execute("DELETE FROM intelligence_42_core.posts")
    out = store_breadth(ctx, DuckWarehouse(con))
    assert "before" not in out


@pytest.mark.parametrize("window_end, before_start, before_end", [
    (date(2026, 10, 4), date(2026, 9, 21), date(2026, 9, 27)),
    (date(2026, 10, 2), date(2026, 9, 23), date(2026, 9, 27)),
])
def test_existing_comparison_queries_keep_exact_adjacent_windows_and_count_scope(ctx, con,
                                                                                window_end, before_start, before_end):
    ctx.window_end = window_end
    out = store_breadth(ctx, DuckWarehouse(con))
    current = ctx.queries[out["sounds"]]
    before = ctx.queries[out["before"]]
    assert current["params"]["since"] == date(2026, 9, 28)
    assert current["params"]["until"] == window_end
    assert (before["params"]["since"], before["params"]["until"]) == (before_start, before_end)
    assert current["params"]["market"] == before["params"]["market"] == "ZA"
    assert current["params"]["profile_source"] == before["params"]["profile_source"]
    assert "COUNT(DISTINCT s.post_id) AS posts" in current["sql"]
    assert "COUNT(DISTINCT b.post_id) AS posts" in before["sql"]
    assert all(query["result_hash"].startswith("sha256:") for query in (current, before))


RANKED_WINDOW = (date(2026, 9, 30), date(2026, 10, 6))
RANKED_PLATFORMS = ["tiktok", "instagram", "youtube", "x", "facebook", "reddit", "threads"]


def _ranked_store():
    """A ZA-like whole store: seven platforms, twelve hashtags each, posts in the window and in the seven days before."""
    import duckdb  # test-only

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, "
                "creator_id VARCHAR, published_at TIMESTAMP, post_date DATE, geo_market VARCHAR, geo_source VARCHAR, "
                "text VARCHAR, hashtags VARCHAR[], sound_id VARCHAR, engagement BIGINT)")
    con.execute("CREATE TABLE intelligence_42_core.creators (creator_id VARCHAR, platform VARCHAR, handle VARCHAR, "
                "home_market VARCHAR)")
    con.execute("CREATE TABLE intelligence_42_core.v_post_source_markets (post_id VARCHAR, "
                "source_sightings STRUCT(source_market VARCHAR, source_region VARCHAR, route VARCHAR, "
                "protocol VARCHAR, observed_at TIMESTAMP, obs_date DATE)[])")
    con.execute("CREATE TABLE intelligence_42_core.cultural_map (item_id VARCHAR, kind VARCHAR, canonical_key VARCHAR, "
                "label VARCHAR, valid_to TIMESTAMP)")
    rows, n = [], 0
    for p, platform in enumerate(RANKED_PLATFORMS):
        for k in range(12):
            for j in range(3 + k % 5):
                for before in (False, True):
                    day = (RANKED_WINDOW[0] - timedelta(days=1 + j % 7) if before
                           else RANKED_WINDOW[0] + timedelta(days=j % 7))
                    n += 1
                    sound = f"74000000000000{p:02d}{k:03d}" if platform in ("tiktok", "instagram", "youtube") else None
                    rows.append((f"p{n}", platform, f"https://example.com/{n}", f"c{p}_{k}_{j}_{before}",
                                 datetime.combine(day, datetime.min.time()) + timedelta(hours=9), day, "ZA",
                                 "ext_region", f"post {n} about tag{k} on {platform} " + "words " * 30,
                                 [f"tag{p}_{k}", "fyp", "southafrica"], sound, 10 * n))
    con.executemany("INSERT INTO intelligence_42_core.posts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return con


def _ranked_answer():
    """Eight ranked claims, one per hashtag, each citing its window creators and posts and its posts before."""
    ctx = RunContext(run_id="r_ranked", tier="T1", as_of=datetime(2026, 10, 6, 19, 18, tzinfo=timezone.utc),
                     market="ZA", window_start=RANKED_WINDOW[0], window_end=RANKED_WINDOW[1])
    out = store_breadth(ctx, DuckWarehouse(_ranked_store()))
    tags, before_query = ctx.queries[out["hashtags"]], ctx.queries[out["before"]]
    before = {(r["platform"], r["hashtag"]): r for r in before_query["rows"] if r["kind"] == "hashtag"}
    claims, supporting = [], {}
    for i, row in enumerate([r for r in tags["rows"] if (r["platform"], r["hashtag"]) in before][:8]):
        prior = before[(row["platform"], row["hashtag"])]
        eids = []
        for k in range(4):
            eid = f"e{i}_{k}"
            ctx.evidence[eid] = {"id": eid, "platform": row["platform"], "handle": f"@h{i}{k}",
                                 "url": f"https://example.com/{eid}", "posted_at": "2026-10-02T09:00:00+02:00",
                                 "market": "ZA", "text": f"#{row['hashtag']} " + "a caption with words " * 20,
                                 "engagement": {"views": 100}, "flags": []}
            eids.append(eid)
        pin = {"run_id": ctx.run_id}
        claims.append({"id": f"c{i + 1}", "kind": "observation", "label": "corroborated", "evidence_ids": eids,
                       "quotes": [],
                       "text": f"#{row['hashtag']} was used by {row['creators']} creators in {row['posts']} posts, "
                               f"against {prior['posts']} posts in the 7-day period before",
                       "numbers": [{**pin, "value": row["creators"], "unit": "creators", "query_id": out["hashtags"],
                                    "result_hash": tags["result_hash"]},
                                   {**pin, "value": row["posts"], "unit": "posts", "query_id": out["hashtags"],
                                    "result_hash": tags["result_hash"]},
                                   {**pin, "value": prior["posts"], "unit": "posts", "query_id": out["before"],
                                    "result_hash": before_query["result_hash"]}]})
        supporting[f"c{i + 1}"] = [row, row, prior]
    answer = {"status": "complete", "as_of": "2026-10-06T19:18:26+02:00",
              "short_answer": "Hashtags about local football and music lead South African posts this week.",
              "context": "The counts cover every stored post in the window.",
              "claims": claims, "evidence": [ctx.evidence[e] for c in claims for e in c["evidence_ids"]],
              "so_what": [{"text": "Music hashtags give brands a wide local moment.", "claim_ids": ["c1", "c2", "c3"]},
                          {"text": "Football talk is steady week on week.", "claim_ids": ["c4", "c5"]}],
              "watch_next": [{"text": "Whether the music hashtags keep their creators.", "claim_ids": ["c1"],
                              "forecast": False}],
              "gaps": []}
    return ctx, out, answer, supporting


class _ScopeModel:
    def __init__(self):
        self.calls = []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        self.calls.append({"system": system, "user": user, "schema": schema})
        usage = {"input_tokens": 1, "output_tokens": 1, "usd": 0.0}
        if schema is writer.SUPPORT_SCHEMA:
            return {"verdict": "supported", "reason": "ok", "demographic_inference": False, "tone_claim": False,
                    "forecast_assertion": False, "country_people": []}, usage
        if schema is writer.K4_REWRITE_SCHEMA:
            return {"text": "A narrower claim."}, usage
        indexes = sorted({int(i) for i in re.findall(r"(?m)^item (\d+) \(", user)})
        return {"fields": [{"index": i, "demographic_inference": False, "forecast_assertion": False,
                            "country_people": [], "so_what_supported": True} for i in indexes]}, usage


def _bound(call):
    return writer._input_token_upper_bound(call["system"], call["user"], call["schema"])


def test_a_ranked_list_with_before_window_counts_fits_every_check_in_one_call_with_each_numbers_row():
    # Live staging, 6 October: eight ranked claims citing window and before-window counts repeated each query's SQL
    # and full key lists for every number with every row holding its value, so the support check and the K4 rewrite
    # cut them for their input budget and the field check read about 98 KB against 46.6 KB.
    ctx, out, answer, supporting = _ranked_answer()
    before_sql, tags_sql = ctx.queries[out["before"]]["sql"], ctx.queries[out["hashtags"]]["sql"]
    model = _ScopeModel()

    checked, rows, _ = writer.apply_support(model, answer, ctx)

    assert [c["id"] for c in checked["claims"]] == [f"c{i}" for i in range(1, 9)]
    assert [r["verdict"] for r in rows if r["rule"] == "K4"] == ["pass"] * 8
    assert len(model.calls) == 8 and all(_bound(call) <= writer.SUPPORT_INPUT_TOKENS for call in model.calls)
    for claim, call in zip(answer["claims"], model.calls):
        # each query's SQL and full parameters once, and every number's own row
        assert call["user"].count(before_sql) == 1 and call["user"].count(tags_sql) == 1
        assert json.dumps(ctx.queries[out["before"]]["params"]["tag_keys"]) in call["user"]
        assert all(json.dumps(row, default=str, ensure_ascii=False) in call["user"] for row in supporting[claim["id"]])

    for claim in answer["claims"]:
        rewrite = _ScopeModel()
        records = [ctx.evidence[e] for e in claim["evidence_ids"]]
        text, _, dispatched = writer._rewrite_call(rewrite, claim, "partial", records, "gemini-3.8-flash",
                                                   query_scope=writer._claim_query_scope(claim, ctx.queries))
        assert dispatched and text == "A narrower claim." and _bound(rewrite.calls[0]) <= writer.K4_REWRITE_INPUT_TOKENS

    fielded = _ScopeModel()
    result, field_rows, _ = writer.apply_field_check(fielded, answer, ctx)

    (call,) = fielded.calls
    assert _bound(call) <= writer.FIELD_INPUT_TOKENS
    assert field_rows == [] and len(result["so_what"]) == 2 and result["short_answer"]
    assert call["user"].count(before_sql) == 1 and call["user"].count(tags_sql) == 1
    for claim_id in ("c1", "c2", "c3", "c4", "c5"):
        assert all(json.dumps(row, default=str, ensure_ascii=False) in call["user"] for row in supporting[claim_id])
