"""Known-answer tests for the legacy backfill (BUILD.md 1.7, DATA.md section 2): seed_graph terms and
enriched_content handles become item_daily rows with lane_class 'legacy', memory only.

The two old tables are DuckDB copies of trends_v2_dev.seed_graph and trends_v2_dev.enriched_content with
their full column lists (engine/infra/bigquery_schemas), in a schema named legacy. run_legacy runs against
them through the detect job's JobClient, which adds insert_rows_json and load jobs to duck.Client.
"""

import json
from datetime import date, datetime, time, timezone

import pytest

from .. import aggregate, legacy, stats
from ..items import canonical_key, item_id
from . import duck
from .fixtures import D, health, run
from .test_detect_states import RANK, RULE, STATS_RUN, World, by, detect, fresh, sql_statements

try:                                  # the detect job's client may move into duck.py
    from .duck import JobClient
except ImportError:
    from .test_detect_job import JobClient

LEGACY_TABLES = """
CREATE TABLE legacy.seed_graph (
  market VARCHAR NOT NULL, term VARCHAR NOT NULL, term_type VARCHAR NOT NULL, platform VARCHAR NOT NULL,
  trend_date DATE NOT NULL, event_date DATE, row_count BIGINT, avg_genz_score DOUBLE, slang_row_share DOUBLE,
  topic_groups VARCHAR[], near_topics VARCHAR[], co_occur_terms VARCHAR[], sample_row_ids VARCHAR[],
  generated_at TIMESTAMPTZ);

CREATE TABLE legacy.enriched_content (
  id VARCHAR NOT NULL, source VARCHAR NOT NULL, platform VARCHAR NOT NULL, market VARCHAR NOT NULL,
  content_type VARCHAR, query_group VARCHAR, query_term VARCHAR, author_name VARCHAR, author_handle VARCHAR,
  author_handle_norm VARCHAR, title VARCHAR, text VARCHAR, url VARCHAR, published_at TIMESTAMPTZ,
  collected_at TIMESTAMPTZ NOT NULL, hashtags VARCHAR, views DOUBLE, likes DOUBLE, comments DOUBLE, shares DOUBLE,
  engagement_total DOUBLE, regional_score DOUBLE, genz_score DOUBLE, creator_watchlist_tier VARCHAR,
  creator_watchlist_score DOUBLE, slang_terms VARCHAR, slang_score DOUBLE, search_velocity_score DOUBLE,
  pipeline_run_id VARCHAR, v2tone VARCHAR, v2persons VARCHAR, v2orgs VARCHAR, v2locations VARCHAR, v2gcam VARCHAR,
  tone_avg DOUBLE, tone_polarity DOUBLE, topic_groups VARCHAR[], classification_layer VARCHAR, near_topic VARCHAR,
  near_cosine DOUBLE, sentiment_lexicon_score DOUBLE, endpoint VARCHAR, vendor_family VARCHAR,
  channel_family VARCHAR, source_family VARCHAR, geo_method_id VARCHAR, geo_receipt_id VARCHAR, native_id VARCHAR,
  source_family_map_version VARCHAR);
"""

KW = dict(core="core", agent="agent", legacy="legacy")

AMA = item_id("hashtag", canonical_key("hashtag", "amapiano"))
SOUND_ID = "7212345678901234567"
SOUND = item_id("sound", canonical_key("sound", SOUND_ID, "tiktok"))
KABZA = item_id("creator", canonical_key("creator", "kabza_de_small", "tiktok"))
EISH = item_id("meme", canonical_key("meme", "eish"))
SKIPPED = {
    item_id("hashtag", "searchonly"), item_id("hashtag", "googleonly"), item_id("hashtag", "globaltag"),
    item_id("hashtag", "latetag"), item_id("meme", "hello"), item_id("topic", "hello"),
    item_id("creator", canonical_key("creator", "trendsbot", "google_search")),
    item_id("creator", canonical_key("creator", "kabza_de_small", "news")),
}


def d(month, dd):
    return date(2026, month, dd)


def at(dt, hour=12):
    return datetime.combine(dt, time(hour), tzinfo=timezone.utc)


def sg(term, term_type, dt, row_count, market="za", platform="tiktok"):
    return {"market": market, "term": term, "term_type": term_type, "platform": platform, "trend_date": dt,
            "event_date": dt, "row_count": row_count, "avg_genz_score": 0.9, "slang_row_share": 0.1,
            "generated_at": at(dt, 23)}


def ec(pid, handle, dt, market="za", platform="tiktok", source="ensembledata", hour=10):
    return {"id": pid, "source": source, "platform": platform, "market": market, "content_type": "post",
            "author_handle": "@" + handle, "author_handle_norm": handle, "text": "vibes #amapiano",
            "url": f"https://x.test/{pid}", "published_at": at(dt, hour - 2), "collected_at": at(dt, hour),
            "hashtags": "", "engagement_total": 10.0, "genz_score": 0.8, "search_velocity_score": 0.7}


def legacy_rows():
    seed = [
        # the hashtag: a ZA wave from 20 to 23 June peaking on 21 June at 9 posts over two platforms
        sg("amapiano", "hashtag", d(6, 20), 3), sg("amapiano", "hashtag", d(6, 21), 6),
        sg("amapiano", "hashtag", d(6, 21), 3, platform="instagram"), sg("amapiano", "hashtag", d(6, 22), 4),
        sg("amapiano", "hashtag", d(6, 23), 2), sg("amapiano", "hashtag", d(6, 21), 2, market="ng"),
        # the sound: 1 to 3 July, peak 12 on 2 July
        sg(SOUND_ID, "music", d(7, 1), 4), sg(SOUND_ID, "music", d(7, 2), 12), sg(SOUND_ID, "music", d(7, 3), 6),
        # the creator's July wave, where seed_graph and enriched_content overlap
        sg("kabza_de_small", "handle", d(7, 20), 10), sg("kabza_de_small", "handle", d(7, 21), 3),
        sg("eish", "slang", d(6, 25), 5),
        # never read: tokens, channels, the Google search lane, a market outside ZA, NG and KE, after until
        sg("hello", "token", d(6, 21), 100), sg("ucabcdefghijklmnop", "channel", d(6, 21), 9),
        sg("searchonly", "hashtag", d(6, 21), 50, platform="search"),
        sg("googleonly", "hashtag", d(6, 21), 50, platform="google_search"),
        sg("globaltag", "hashtag", d(6, 21), 40, market="us"),
        sg("latetag", "hashtag", d(9, 29), 30), sg("amapiano", "hashtag", d(9, 29), 30),
    ]
    enriched = (
        # the creator's May wave, before seed_graph existed: 2 posts on 10 May and 4 on 11 May
        [ec(f"m{i}", "kabza_de_small", d(5, 10)) for i in (1, 2)]
        + [ec(f"m{i}", "kabza_de_small", d(5, 11)) for i in (3, 4, 5, 6)]
        + [ec("m1", "kabza_de_small", d(5, 11), hour=15)]       # collected again: counts on its first day only
        + [ec(f"j{i}", "kabza_de_small", d(7, 20)) for i in range(7)]      # 7 against seed_graph's 10
        + [ec(f"k{i}", "kabza_de_small", d(7, 21)) for i in range(5)]      # 5 against seed_graph's 3
        + [ec("g1", "trendsbot", d(5, 11), platform="google_search", source="bigquery_trends"),
           ec("g2", "kabza_de_small", d(5, 11), source="Google Trends"),
           ec("n1", "kabza_de_small", d(5, 11), platform="news", source="gdelt")]
    )
    return seed, enriched


class LegacyClient(JobClient):
    def __init__(self, con):
        super().__init__(con)
        self.loads = []

    def load_table_from_json(self, rows, table, job_config=None):
        self.loads.append((f"{table.dataset_id}.{table.table_id}", job_config.write_disposition, len(rows)))
        return super().load_table_from_json(rows, table, job_config)


@pytest.fixture
def con():
    c = duck.connect()
    c.execute("CREATE SCHEMA legacy;")
    c.execute(LEGACY_TABLES)
    for stmt in sql_statements("waves.sql"):
        c.execute(duck.create_statement(stmt))
    seed, enriched = legacy_rows()
    duck.load(c, "legacy.seed_graph", seed)
    duck.load(c, "legacy.enriched_content", enriched)
    yield c
    c.close()


def backfill(con, **kw):
    client = LegacyClient(con)
    counts = legacy.run_legacy(client, run_id=kw.pop("run_id", "legacy-test"), **kw, **KW)
    return client, counts


def waves(con, item):
    return duck.query(con, "SELECT w.market, w.wave_start, w.wave_end, w.peak_date, w.peak_posts "
                           "FROM {core}.v_item_waves w WHERE w.item_id = @i ORDER BY w.market, w.wave_start",
                      {"i": item})


def current(con):
    return duck.query(con, "SELECT * FROM {core}.v_item_daily_current i ORDER BY i.metric_date, i.item_id, i.platform")


def snapshot(con, table):
    return sorted(json.dumps(r, default=str, sort_keys=True)
                  for r in duck.query(con, f"SELECT * FROM {table} t"))


# The SQL file


def test_legacy_sql_never_reads_genz_search_velocity_or_star():
    text = legacy.LEGACY_SQL.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--")).lower()
    assert "genz" not in body and "velocity" not in body and "*" not in body.replace("count(*)", "")
    stmts = legacy.statements()
    assert set(stmts) == {"dates", "done", "first_collect", "seed", "handles", "merge"}
    for name, sql in stmts.items():
        verb = sql.lstrip().split(None, 1)[0].upper()
        assert verb == "MERGE" if name == "merge" else verb in ("SELECT", "WITH")
        assert not any(w in sql.upper() for w in ("DELETE", "DROP", "TRUNCATE", "REPLACE", "CREATE", "INSERT INTO"))
    assert stmts["merge"].startswith("MERGE {core}.cultural_map t")


# Waves, mapping and cultural_map


def test_three_known_recurring_items_show_their_earlier_waves(con):
    backfill(con)
    assert waves(con, AMA) == [
        {"market": "NG", "wave_start": d(6, 21), "wave_end": d(6, 21), "peak_date": d(6, 21), "peak_posts": 2},
        {"market": "ZA", "wave_start": d(6, 20), "wave_end": d(6, 23), "peak_date": d(6, 21), "peak_posts": 9},
    ]
    assert waves(con, SOUND) == [
        {"market": "ZA", "wave_start": d(7, 1), "wave_end": d(7, 3), "peak_date": d(7, 2), "peak_posts": 12}]
    assert waves(con, KABZA) == [
        {"market": "ZA", "wave_start": d(5, 10), "wave_end": d(5, 11), "peak_date": d(5, 11), "peak_posts": 4},
        {"market": "ZA", "wave_start": d(7, 20), "wave_end": d(7, 21), "peak_date": d(7, 20), "peak_posts": 10},
    ]
    assert waves(con, EISH) == [
        {"market": "ZA", "wave_start": d(6, 25), "wave_end": d(6, 25), "peak_date": d(6, 25), "peak_posts": 5}]


def test_rows_are_legacy_only_mapped_by_kind_and_carry_no_tier_geo_or_series(con):
    client, counts = backfill(con)
    rows = current(con)
    assert {r["item_id"] for r in rows} == {AMA, SOUND, KABZA, EISH}
    assert not SKIPPED & {r["item_id"] for r in duck.query(con, "SELECT i.item_id FROM {core}.item_daily i")}
    assert {r["lane_class"] for r in rows} == {"legacy"}
    assert {r["market"] for r in rows} == {"ZA", "NG"}
    assert max(r["metric_date"] for r in rows) == d(7, 21)
    for r in rows:
        assert r["series"] is None and r["protocol"] is None and r["tier_posts"] is None
        assert r["geo_known_posts"] is None and r["local_posts"] is None and r["unflagged_creators"] is None
        assert r["run_id"] == "legacy-test" and r["rule_version"] == legacy.RULE_VERSION
        assert r["source_regime"] == "legacy" and r["available_at"] is not None
    k = {(r["metric_date"], r["platform"]): r for r in rows if r["item_id"] == KABZA}
    assert (k[(d(5, 10), "tiktok")]["posts"], k[(d(5, 10), "tiktok")]["creators"]) == (2, 1)
    assert k[(d(5, 10), "tiktok")]["engagement"] == 20
    assert k[(d(7, 20), "tiktok")]["posts"] == 10 and k[(d(7, 20), "tiktok")]["creators"] is None   # seed_graph wins
    assert k[(d(7, 21), "tiktok")]["posts"] == 5 and k[(d(7, 21), "tiktok")]["creators"] == 1       # enriched wins
    dates = sorted({r["metric_date"] for r in rows})
    runs_rows = duck.query(con, "SELECT * FROM {agent}.runs r ORDER BY r.run_date")
    assert [r["run_date"] for r in runs_rows] == dates
    assert {(r["run_id"], r["stage"], r["status"]) for r in runs_rows} == {("legacy-test", "aggregate", "ok")}
    assert counts == {"dates": len(dates), "dates_done_before": 0, "item_daily": len(rows), "items": 4}
    cm = by(duck.query(con, "SELECT * FROM {core}.cultural_map c"), "item_id")
    assert set(cm) == {AMA, SOUND, KABZA, EISH}
    assert (cm[AMA]["kind"], cm[AMA]["label"], cm[AMA]["first_seen"], cm[AMA]["first_seen_market"],
            cm[AMA]["first_seen_platform"], cm[AMA]["last_seen"], cm[AMA]["status"]) == (
        "hashtag", "#amapiano", d(6, 20), "ZA", "tiktok", d(6, 23), "active")
    assert (cm[SOUND]["kind"], cm[SOUND]["canonical_key"]) == ("sound", f"tiktok:{SOUND_ID}")
    assert (cm[KABZA]["kind"], cm[KABZA]["first_seen"], cm[KABZA]["last_seen"]) == ("creator", d(5, 10), d(7, 21))
    assert (cm[EISH]["kind"], cm[EISH]["label"]) == ("meme", "eish")


def test_until_and_since_bound_the_dates(con):
    backfill(con, since=d(6, 21), until=d(7, 1))
    dates = {r["metric_date"] for r in current(con)}
    assert dates == {d(6, 21), d(6, 22), d(6, 23), d(6, 25), d(7, 1)}


def test_default_until_is_27_september():
    assert legacy.run_legacy.__kwdefaults__["until"] == date(2026, 9, 27)


def test_until_on_or_after_the_first_collect_day_raises_and_writes_nothing(con):
    duck.load(con, "core.collection_health", [health("feed_tiktok", d(7, 2))])
    for until in (d(7, 2), d(7, 3)):
        with pytest.raises(ValueError):
            backfill(con, until=until)
    assert duck.query(con, "SELECT * FROM {core}.item_daily i") == []
    backfill(con, until=d(7, 1))
    assert max(r["metric_date"] for r in current(con)) == d(7, 1)


def test_dates_with_a_non_legacy_aggregate_run_are_dropped(con):
    duck.load(con, "agent.runs", [run("aggregate", d(6, 21)), run("aggregate", d(6, 22), status="failed")])
    backfill(con)
    dates = {r["metric_date"] for r in duck.query(con, "SELECT * FROM {core}.item_daily i")}
    assert d(6, 21) not in dates and {d(6, 20), d(6, 22)} <= dates


def test_dates_query_drops_brand24_synthetic_rows_like_the_handles_query(con):
    duck.load(con, "legacy.enriched_content", [
        {**ec("b1", "someone", d(5, 15), source="brand24"), "content_type": "aggregate"}])
    sql = legacy.statements()["dates"].replace("{legacy}", "legacy")
    dates = {r["metric_date"] for r in duck.query(con, sql, {"since": d(1, 1), "until": d(9, 27)})}
    assert d(5, 15) not in dates and d(5, 10) in dates


def test_x_maps_to_twitter_and_seed_platforms_are_an_allow_list(con):
    duck.load(con, "legacy.enriched_content", [ec("x1", "kabza_de_small", d(5, 12), platform="x")])
    duck.load(con, "legacy.seed_graph", [
        sg("kabza_de_small", "handle", d(6, 19), 4, platform="x"),
        sg("podcasttag", "hashtag", d(6, 19), 20, platform="podcast"),
        sg("othertag", "hashtag", d(6, 19), 20, platform="other")])
    backfill(con)
    tw = item_id("creator", canonical_key("creator", "kabza_de_small", "twitter"))
    rows = [(r["metric_date"], r["platform"], r["posts"]) for r in current(con) if r["item_id"] == tw]
    assert rows == [(d(5, 12), "twitter", 1), (d(6, 19), "twitter", 4)]
    ids = {r["item_id"] for r in current(con)}
    assert item_id("hashtag", "podcasttag") not in ids and item_id("hashtag", "othertag") not in ids
    assert item_id("creator", canonical_key("creator", "kabza_de_small", "x")) not in ids


def test_day_rows_counts_one_item_once_whatever_its_label():
    seed = [{"market": "ZA", "platform": "tiktok", "term": "kabza_de_small", "term_type": "handle", "posts": 10,
             "available_at": at(d(7, 20))}]
    handles = [{"market": "ZA", "platform": "tiktok", "handle": "Kabza_De_Small", "posts": 7, "engagement": 70,
                "first_post_at": at(d(7, 20), 8), "available_at": at(d(7, 20))}]
    rows = legacy.day_rows(d(7, 20), seed, handles, "r")
    assert [(r["item_id"], r["platform"], r["posts"]) for r in rows] == [(KABZA, "tiktok", 10)]


def test_merge_moves_first_seen_earlier_and_never_reopens_a_closed_row(con):
    duck.load(con, "core.cultural_map", [
        {"item_id": AMA, "kind": "hashtag", "canonical_key": "amapiano", "label": "#Amapiano",
         "first_seen": d(9, 29), "first_seen_market": "KE", "first_seen_platform": "x", "last_seen": d(9, 30),
         "status": "rejected", "valid_from": at(d(9, 29)), "valid_to": None},
        {"item_id": SOUND, "kind": "sound", "canonical_key": f"tiktok:{SOUND_ID}", "label": SOUND_ID,
         "first_seen": d(9, 29), "last_seen": d(9, 29), "status": "active", "valid_from": at(d(9, 1)),
         "valid_to": at(d(9, 2))},
    ])
    backfill(con)
    cm = duck.query(con, "SELECT * FROM {core}.cultural_map c WHERE c.item_id IN (@a, @s)", {"a": AMA, "s": SOUND})
    a = [r for r in cm if r["item_id"] == AMA]
    assert len(a) == 1
    assert (a[0]["first_seen"], a[0]["first_seen_market"], a[0]["first_seen_platform"], a[0]["last_seen"],
            a[0]["label"], a[0]["status"]) == (d(6, 20), "ZA", "tiktok", d(9, 30), "#Amapiano", "rejected")
    s = [r for r in cm if r["item_id"] == SOUND]
    assert len(s) == 1 and s[0]["first_seen"] == d(9, 29)


def test_stoplisted_legacy_hashtags_are_generic_and_keep_their_history(con):
    fyp, viral = item_id("hashtag", "fyp"), item_id("hashtag", "viral")
    duck.load(con, "legacy.seed_graph", [sg("#FYP", "hashtag", d(6, 21), 40), sg("Viral", "hashtag", d(6, 22), 30),
                                         sg("fyp", "slang", d(6, 21), 5)])
    duck.load(con, "core.cultural_map", [
        {"item_id": viral, "kind": "hashtag", "canonical_key": "viral", "label": "#viral", "first_seen": d(9, 29),
         "last_seen": d(9, 29), "status": "active", "valid_from": at(d(9, 1)), "valid_to": None},
        {"item_id": AMA, "kind": "hashtag", "canonical_key": "amapiano", "label": "#amapiano",
         "first_seen": d(9, 29), "last_seen": d(9, 29), "status": "generic", "valid_from": at(d(9, 1)),
         "valid_to": None}])
    backfill(con)
    cm = by(duck.query(con, "SELECT * FROM {core}.cultural_map c"), "item_id")
    assert cm[fyp]["status"] == "generic" and cm[viral]["status"] == "generic"
    assert cm[viral]["first_seen"] == d(6, 22)
    assert cm[AMA]["status"] == "active"                             # off the stoplist: back to active
    assert cm[item_id("meme", "fyp")]["status"] == "active"          # the stoplist is for hashtags only
    posts = {r["item_id"]: r["posts"] for r in current(con) if r["item_id"] in (fyp, viral)}
    assert posts == {fyp: 40, viral: 30}


def test_legacy_merge_uses_the_aggregate_status_rule():
    merge = legacy.statements()["merge"]
    agg = aggregate.cultural_map_merge_sql()
    rule = "status = IF(t.status IN ('active', 'generic'), s.status, t.status)"
    assert rule in merge and rule in agg
    assert "s.last_seen, s.status, CURRENT_TIMESTAMP()" in merge and "s.last_seen, s.status, CURRENT_TIMESTAMP()" in agg
    assert "DELETE" not in merge.upper()


# Memory only: no baseline, no floors


def test_legacy_rows_never_form_a_series_or_meet_floors(con):
    backfill(con)
    for dt in (d(5, 11), d(6, 21), d(7, 2), d(7, 20), D):
        assert duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": dt}) == []
        assert duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d)", {"d": dt}) == []
    assert duck.query(con, "SELECT * FROM {core}.post_observations p") == []
    assert duck.query(con, "SELECT * FROM {core}.item_daily i WHERE i.lane_class != 'legacy'") == []


def test_a_legacy_item_reads_recurring_when_a_new_wave_starts(con):
    w = World()
    for item in (AMA, SOUND, KABZA):
        fresh(w, item)
        for i in (2, 1, 0):
            w.daily(item, i, 1, **RANK)
    w.items -= {AMA, SOUND, KABZA}            # loaded below with their real kinds
    w.load(con)
    duck.load(con, "core.cultural_map", [
        {"item_id": i, "kind": k, "canonical_key": i, "label": i, "status": "active",
         "valid_from": at(D), "valid_to": None} for i, k in ((AMA, "hashtag"), (SOUND, "sound"), (KABZA, "creator"))])
    before = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d) s ORDER BY s.series_id", {"d": D})
    assert {r["item_id"] for r in before} == {AMA, SOUND, KABZA}
    backfill(con, until=d(9, 10))            # the World collects from 11 September
    assert duck.query(con, "SELECT MIN(h.day) m FROM {core}.collection_health h") == [{"m": d(9, 11)}]
    signal = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d) s ORDER BY s.series_id", {"d": D})
    assert signal == before                  # same series, obs_prior and first_measured: warm-up is not shortened
    duck.load(con, "core.series_test", stats.passthrough_rows(signal, D, STATS_RUN, RULE))
    duck.load(con, "agent.runs", [run("stats", D, STATS_RUN)])
    rows = detect(con)
    expected = {AMA: (d(6, 21), 9), SOUND: (d(7, 2), 12), KABZA: (d(7, 20), 10)}
    for item, (peak_date, peak_posts) in expected.items():
        r = rows[item]
        assert (r["state"], r["novelty"]) == ("recurring", "recurrence")
        assert r["last_wave"] == {"peak_date": peak_date, "peak_posts": peak_posts}
        assert (r["creators3"], r["posts3"]) == (3, 3)          # only the new posts meet floors
    assert EISH not in rows


# Idempotent and append only


def test_second_run_adds_nothing_and_a_split_run_equals_one_run(con):
    _, first = backfill(con, until=d(6, 30))
    _, rest = backfill(con, run_id="legacy-second")
    tables = ("core.item_daily", "core.cultural_map", "agent.runs")
    after = {t: snapshot(con, t) for t in tables}
    _, again = backfill(con, run_id="legacy-third")
    assert again == {"dates": 0, "dates_done_before": first["dates"] + rest["dates"], "item_daily": 0, "items": 0}
    assert {t: snapshot(con, t) for t in tables} == after
    assert rest["dates_done_before"] == first["dates"]
    split = sorted((r["metric_date"], r["market"], r["platform"], r["item_id"], r["posts"]) for r in current(con))

    fresh_con = duck.connect()
    fresh_con.execute("CREATE SCHEMA legacy;")
    fresh_con.execute(LEGACY_TABLES)
    seed, enriched = legacy_rows()
    duck.load(fresh_con, "legacy.seed_graph", seed)
    duck.load(fresh_con, "legacy.enriched_content", enriched)
    backfill(fresh_con)
    whole = sorted((r["metric_date"], r["market"], r["platform"], r["item_id"], r["posts"]) for r in current(fresh_con))
    fresh_con.close()
    assert split == whole


def test_writes_only_by_append_and_one_merge_and_never_touches_the_old_tables(con):
    before = {t: snapshot(con, t) for t in ("legacy.seed_graph", "legacy.enriched_content")}
    client, _ = backfill(con)
    assert {t: snapshot(con, t) for t in before} == before
    for sql in client.sql:
        verb = sql.lstrip().split(None, 1)[0].upper()
        assert verb in ("SELECT", "WITH", "MERGE")
        assert all(w not in sql.upper() for w in ("DELETE", "DROP", "TRUNCATE", "REPLACE", "CREATE", "INSERT INTO"))
        if verb == "MERGE":
            assert sql.lstrip().startswith("MERGE core.cultural_map t")
        assert "genz" not in sql.lower() and "velocity" not in sql.lower()
    assert {t for t, _, _ in client.loads} == {"core.item_daily"}
    assert {disp for _, disp, _ in client.loads} == {"WRITE_APPEND"}
    assert {t for t, _ in client.inserted} == {"agent.runs"}


def test_main_backfills_with_the_defaults(con, capsys):
    assert legacy.main(client=LegacyClient(con), **KW) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["legacy"]["item_daily"] == len(current(con)) and out["legacy"]["dates"] > 0
