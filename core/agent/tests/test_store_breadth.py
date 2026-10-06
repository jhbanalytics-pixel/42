"""Whole-store breadth for Ask (Albert, 4 October: answers drew on about 20 accounts while the store held thousands of
posts). Code counts every stored post in the market and window per platform, per sound and per hashtag; the writer
leads claims with those totals and cites posts as samples. Run on DuckDB fixtures, transpiled from BigQuery."""

from datetime import date, datetime

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
    assert len(calls) == 1 and calls[0]["platforms"] == ["tiktok"]
    assert calls[0]["queries_before"]  # after research, so research queries keep their ids
    store = out["run"]["store"]
    assert store == {"posts": 1108, "creators": 852, "located_posts": 477, "located_creators": 332, "platforms": 1,
                     "query_id": store["query_id"]}
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
    gap = next(g for g in draft["gaps"] if g["what"] == "Some retrieved posts could not be cited")
    assert gap["why"] == ("Some stored posts had no author handle, which every cited post needs, so they were left "
                          "out of drafting.")
    assert writer.writer_gaps(draft["gaps"]) == []
    assert writer.unshowable_gap(["text", "url"])["why"].startswith("Some stored posts had no link or caption text,")
    assert writer.unshowable_gap([]) == writer.UNSHOWABLE_EVIDENCE_GAP


def test_the_writer_is_told_to_take_counts_from_the_whole_store_and_name_untitled_sounds_by_their_first_creator():
    system = writer.WRITER_SYSTEM.format(days=7)
    assert "Queries whose purpose starts 'Whole-store' count every stored post" in system
    assert "how many there are is never a count" in system
    assert "never write the id or its link and never make up a title" in system
    assert "name it by the earliest_creator its row gives" in system
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
