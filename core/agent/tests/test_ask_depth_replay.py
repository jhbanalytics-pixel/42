"""Ask depth, offline replay. A fixture shaped like the stored answer of the staging run behind "#humor in South Africa
this week" (70 live TikTok posts, 100 whole-store sample posts, a few comments, and three stored #humor posts) is put
beside a small warehouse whose humour posts sit in related tags and in the tag column. The topic sweep and the writer
pack run on it with no network and no model. The warehouse is DuckDB, so the SQL that runs is the SQL that ships, apart
from the semantic leg, which DuckDB cannot run and which the sweep records as failed."""

from datetime import date, datetime

import pytest

from core.agent import writer
from core.agent.context import RunContext
from core.agent.tests.test_history import DuckWarehouse
from core.agent.tools.warehouse import SWEEP_TOOL, search_posts, sweep_topic, topic_sweep

QUESTION = "#humor in South Africa this week"
WINDOW = (date(2026, 10, 4), date(2026, 10, 10))
DAY = date(2026, 10, 8)

# The stored answer, reduced to what the pack reads. The counts are the ones the run reported.
STORED_ANSWER = {
    "question": QUESTION, "market": "ZA", "window": ["2026-10-04", "2026-10-10"],
    "live_tiktok_posts": 70, "gate_sample_posts": 100, "comments": 12, "cited_humor_posts": 3,
}


def posts_table(con):
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.posts (post_id VARCHAR, platform VARCHAR, url VARCHAR, "
                "creator_id VARCHAR, published_at TIMESTAMP, post_date DATE, geo_market VARCHAR, geo_source VARCHAR, "
                "text VARCHAR, views BIGINT, likes BIGINT, comments BIGINT, shares BIGINT, engagement BIGINT, "
                "hashtags VARCHAR[])")
    con.execute("CREATE TABLE intelligence_42_core.creators (platform VARCHAR, creator_id VARCHAR, handle VARCHAR, "
                "home_market VARCHAR)")
    con.execute("CREATE TABLE intelligence_42_core.v_post_source_markets (post_id VARCHAR, "
                "source_markets VARCHAR[], source_sightings STRUCT(source_market VARCHAR, source_region VARCHAR, "
                "route VARCHAR, protocol VARCHAR, observed_at TIMESTAMP, obs_date DATE)[])")


def humour_store():
    import duckdb

    con = duckdb.connect()
    posts_table(con)
    rows, platforms = [], ("tiktok", "instagram", "x")

    def add(pid, platform, creator, tags, text="a post"):
        rows.append((pid, platform, f"https://p.example/{pid}", creator, DAY, text, tags))

    for i in range(3):  # what the run found: the literal tag, in the caption and the column
        add(f"lit{i}", "tiktok", f"lit_c{i}", ["humor", "memes", "comedia"], "so funny #humor #memes #comedia")
    for i in range(9):  # more posts that carry the tag in the column but not in the caption
        add(f"seed{i}", "tiktok", f"seed_c{i}", ["humor", "memes", "comedia"])
    for i in range(20):  # humour held in related tags only
        add(f"fam{i}", platforms[i % 3], f"fam_c{i % 20}", ["comedia", "memes"])
    for i in range(25):
        add(f"mzansi{i}", platforms[i % 3], f"mz_c{i % 12}", ["mzansicomedy", "funny"])
    for i in range(5):  # the tag itself, column only
        add(f"colonly{i}", "x", f"col_c{i}", ["Humor"])
    for i in range(150):  # the rest of the store
        add(f"noise{i}", platforms[i % 3], f"noise_c{i % 60}", ["amapiano"])
    con.executemany("INSERT INTO intelligence_42_core.posts VALUES (?, ?, ?, ?, NULL, ?, 'ZA', 'geotag', ?, 10, 1, "
                    "NULL, NULL, 11, ?)", rows)
    return con


class ReplayWarehouse(DuckWarehouse):
    """DuckDB for every query except the semantic function, which it cannot run."""

    def run(self, sql, params, max_bytes_billed):
        if "tvf_search_posts" in sql:
            raise RuntimeError("semantic function not available offline")
        return super().run(sql, params, max_bytes_billed)


@pytest.fixture
def ctx():
    return RunContext(run_id="r_replay", tier="T1", as_of=datetime(2026, 10, 10, 8, 0), market="ZA",
                      window_start=WINDOW[0], window_end=WINDOW[1])


def previous_run_evidence(ctx):
    """The evidence the staging run held beside its three humour posts, in the order it found it."""
    def record(pid, platform, handle, **extra):
        ctx.evidence[pid] = {"id": pid, "platform": platform, "handle": handle, "url": f"https://prev.example/{pid}",
                             "posted_at": "2026-10-08T10:00:00", "market": "ZA", "text": "a post",
                             "engagement": {"views": 1}, "flags": ["market_assumed"], "source_market": None, **extra}

    for i in range(STORED_ANSWER["live_tiktok_posts"]):
        record(f"live{i}", "tiktok", f"live_c{i % 40}")
    for i in range(STORED_ANSWER["gate_sample_posts"]):
        record(f"sample{i}", ("tiktok", "x")[i % 2], f"sample_c{i % 50}")
    for i in range(STORED_ANSWER["comments"]):
        record(f"comment{i}", "tiktok", f"commenter{i}", parent_id="live0")
    ctx.record_query("SELECT 1", {}, [{"post_id": f"sample{i}"} for i in range(100)],
                     "fetch_posts: 100 posts listed in query rows")


def pack_order(ctx):
    blocks, left = writer._pack(ctx, list(ctx.evidence.values()))
    import json

    ids = [json.loads(b.split("\n", 1)[0][len("post "):])["id"] for b in blocks if b.startswith("post ")]
    return ids, left


def test_the_question_names_the_tag_the_run_searched():
    assert sweep_topic(QUESTION) == ["#humor"]


def test_the_literal_search_finds_only_the_posts_that_carry_the_tag(ctx):
    out = search_posts(ctx, ReplayWarehouse(humour_store()), "humor", limit=100)
    assert {e["id"] for e in out["evidence"]} == ({"lit0", "lit1", "lit2"} | {f"colonly{i}" for i in range(5)}
                                                  | {f"seed{i}" for i in range(9)})


def test_the_sweep_reaches_the_tag_family_and_the_pack_puts_it_first(ctx):
    previous_run_evidence(ctx)
    out = topic_sweep(ctx, ReplayWarehouse(humour_store()), sweep_topic(QUESTION), platforms=None)
    assert out["searches"] == 2 and set(out["failed"]) == {"semantic a", "semantic b"}
    assert {"comedia", "memes"} <= set(out["family"])
    on_topic = {pid for pid in ctx.evidence if pid.startswith(("lit", "seed", "fam", "colonly"))}
    assert len(on_topic) >= 30 > STORED_ANSWER["cited_humor_posts"]  # 3 before, the family now
    assert not any(pid.startswith("mzansi") for pid in ctx.evidence) or "mzansicomedy" in out["family"]
    assert not any(pid.startswith("noise") for pid in ctx.evidence)

    ids, left = pack_order(ctx)
    assert left == {"posts": 0, "queries": 0} and on_topic <= set(ids) and len(ids) <= writer.MAX_PACK_POSTS
    for platform in ("tiktok", "x", "instagram"):
        mine = [pid for pid in ids if ctx.evidence[pid]["platform"] == platform]
        kinds = ["topic" if pid in on_topic else "comment" if pid.startswith("comment") else
                 "sample" if pid.startswith("sample") else "live" for pid in mine]
        rank = {"topic": 0, "live": 1, "sample": 2, "comment": 3}
        assert "topic" in kinds and [rank[k] for k in kinds] == sorted(rank[k] for k in kinds)
    first_ten = ids[:10]
    assert {ctx.evidence[pid]["platform"] for pid in first_ten} >= {"tiktok", "x", "instagram"}


def test_no_creator_has_more_than_three_posts_in_what_the_sweep_stores(ctx):
    topic_sweep(ctx, ReplayWarehouse(humour_store()), sweep_topic(QUESTION), platforms=None)
    per_creator = {}
    for record in ctx.evidence.values():
        per_creator[record["handle"]] = per_creator.get(record["handle"], 0) + 1
    assert max(per_creator.values()) <= 3


def test_the_sweep_queries_are_recorded_and_stay_out_of_the_pack_blocks(ctx):
    topic_sweep(ctx, ReplayWarehouse(humour_store()), sweep_topic(QUESTION), platforms=None)
    swept = [q for q in ctx.queries.values() if q.get("tool") == SWEEP_TOOL]
    assert len(swept) == 2 and all(q["result_hash"].startswith("sha256:") and q["rows"] for q in swept)
    blocks, _ = writer._pack(ctx, list(ctx.evidence.values()))
    assert not any("Topic sweep" in b for b in blocks)
