import pytest
from google.cloud import bigquery

from core.detect import aggregate, job
from core.detect.tests import duck
from core.detect.tests.fixtures import D, day, obs, post, rid, run
from core.detect.tests.test_detect_job import FakeChain, JobClient


@pytest.fixture
def con():
    c = duck.connect()
    names = ("future", "retired", "foreign", "valid", "legacy")
    duck.load(c, "core.posts", [post(p, p, D, geo_market="ZA", geo_confidence=0.9,
                                    geo_source="ext_region") for p in names])
    duck.load(c, "core.post_observations", [obs(p, D, "panel", "panel", series="desk", protocol="p1")
                                           for p in names])
    duck.load(c, "core.post_items", [
        {"post_id": "future", "item_id": "future", "via": "cluster", "linked_on": day(-1), "link_market": "ZA"},
        {"post_id": "retired", "item_id": "retired", "via": "cluster", "linked_on": day(2), "link_market": "ZA"},
        {"post_id": "foreign", "item_id": "foreign", "via": "cluster", "linked_on": D, "link_market": "NG"},
        {"post_id": "valid", "item_id": "valid", "via": "cluster", "linked_on": D, "link_market": "ZA"},
        {"post_id": "legacy", "item_id": "legacy", "via": "hashtag"},
    ])
    duck.load(c, "core.post_item_end", [{"post_id": "retired", "item_id": "retired", "ended_on": D,
                                       "reason": "split", "lineage_id": "fixture"}])
    yield c
    c.close()


def test_window_counts_only_authoritative_links_in_the_observed_market(con):
    rows = duck.query(con, "SELECT item_id, market, posts3 FROM {core}.tvf_item_window(@d) ORDER BY item_id", {"d": D})
    assert rows == [{"item_id": item, "market": "ZA", "posts3": 1} for item in ("legacy", "valid")]


def test_aggregate_counts_only_authoritative_links_in_the_observed_market(con):
    aggregate._run(duck.Client(con), aggregate.aggregate_sql(), [
        bigquery.ScalarQueryParameter("d", "DATE", D),
        bigquery.ScalarQueryParameter("run_id", "STRING", "counted"),
        bigquery.ScalarQueryParameter("rule_version", "STRING", "fixture"),
    ], "core", "agent")
    rows = duck.query(con, "SELECT item_id, market, lane_class, posts FROM {core}.item_daily ORDER BY item_id, lane_class")
    assert rows == [{"item_id": item, "market": "ZA", "lane_class": lane, "posts": 1}
                    for item in ("legacy", "valid") for lane in ("_any", "panel")]


def test_market_link_cannot_follow_the_same_post_into_another_market(con):
    duck.load(con, "core.post_observations", [obs("valid", D, "panel", "panel", market="KE", series="desk", protocol="p1")])
    window = duck.query(con, "SELECT market, posts3 FROM {core}.tvf_item_window(@d) WHERE item_id = 'valid'", {"d": D})
    assert window == [{"market": "ZA", "posts3": 1}]
    aggregate._run(duck.Client(con), aggregate.aggregate_sql(), [
        bigquery.ScalarQueryParameter("d", "DATE", D),
        bigquery.ScalarQueryParameter("run_id", "STRING", "counted"),
        bigquery.ScalarQueryParameter("rule_version", "STRING", "fixture"),
    ], "core", "agent")
    assert duck.query(con, "SELECT DISTINCT market FROM {core}.item_daily WHERE item_id = 'valid'") == [{"market": "ZA"}]
    duck.load(con, "agent.runs", [run("detect", D)])
    duck.load(con, "core.item_state", [{"metric_date": D, "market": market, "item_id": "valid",
                                        "run_id": rid("detect", D)} for market in ("ZA", "KE")])
    scope = duck.query(con, "SELECT market, total_posts7 FROM {core}.v_item_market_scope ORDER BY market")
    assert scope == [{"market": "KE", "total_posts7": 0}, {"market": "ZA", "total_posts7": 1}]


def test_scope_counts_only_authoritative_links_at_each_state_date(con):
    duck.load(con, "agent.runs", [run("detect", D)])
    duck.load(con, "core.item_state", [{"metric_date": D, "market": "ZA", "item_id": item,
                                        "run_id": rid("detect", D)}
                                       for item in ("future", "retired", "foreign", "valid", "legacy")])
    rows = duck.query(con, "SELECT item_id, total_posts7 FROM {core}.v_item_market_scope ORDER BY item_id")
    assert rows == [{"item_id": item, "total_posts7": int(item in ("legacy", "valid"))}
                    for item in ("foreign", "future", "legacy", "retired", "valid")]


def test_historical_resolver_matches_date_function_without_cross_date_ranking(con):
    dates = [day(2), day(1), D, day(-1)]
    duck.load(con, "agent.runs", [run("detect", d) for d in dates])
    duck.load(con, "core.item_state", [{"metric_date": d, "market": "ZA", "item_id": "valid",
                                        "run_id": rid("detect", d)} for d in dates])
    duck.load(con, "core.post_item_lineage", [
        {"post_id": "retired", "item_id": "child", "linked_on": D, "link_market": "ZA", "lineage_id": "fixture"}])
    duck.load(con, "core.post_items", [
        {"post_id": "valid", "item_id": "loser", "via": "cluster_pan", "linked_on": D},
        {"post_id": "valid", "item_id": "valid", "via": "cluster", "linked_on": D, "link_market": "ZA"},
    ])
    for d in dates:
        dated = duck.query(con, "SELECT post_id, item_id, via, market FROM {core}.v_post_items_by_state_date "
                                "WHERE metric_date = @d ORDER BY post_id, item_id, via, market", {"d": d})
        authoritative = duck.query(con, "SELECT * FROM {core}.tvf_post_items(@d) "
                                        "ORDER BY post_id, item_id, via, market", {"d": d})
        assert dated == authoritative
def test_mixed_legacy_and_dated_link_counts_one_post_and_one_engagement(con):
    con.execute("UPDATE core.posts SET engagement = 7 WHERE post_id = 'valid'")
    duck.load(con, "core.post_items", [{"post_id": "valid", "item_id": "valid", "via": "hashtag"}])
    aggregate._run(duck.Client(con), aggregate.aggregate_sql(), [
        bigquery.ScalarQueryParameter("d", "DATE", D),
        bigquery.ScalarQueryParameter("run_id", "STRING", "counted"),
        bigquery.ScalarQueryParameter("rule_version", "STRING", "fixture"),
    ], "core", "agent")
    rows = duck.query(con, "SELECT posts, engagement FROM {core}.item_daily WHERE item_id = 'valid'")
    assert rows == [{"posts": 1, "engagement": 7}, {"posts": 1, "engagement": 7}]


def test_resolver_install_failure_fails_detect_even_with_temporal_schema_ready():
    con = duck.connect(views=False)

    class RefusedResolver(JobClient):
        def query(self, sql, job_config=None):
            if sql.startswith("CREATE OR REPLACE TABLE FUNCTION core.tvf_post_items"):
                raise PermissionError("fixture resolver installation refused")
            return super().query(sql, job_config)

    chain = FakeChain(con)
    with pytest.raises(RuntimeError, match="Data issue: dated post-item resolver unavailable"):
        job.run(RefusedResolver(con), D, chain=chain, core="core", agent="agent")
    assert chain.of("finish")[0][2] == "failed"
    assert chain.of("start_next") == []
    assert con.execute("SELECT COUNT(*) FROM core.item_daily").fetchone()[0] == 0
    con.close()


def test_bootstrap_installs_existing_resolver_before_dependent_views():
    con = duck.connect(views=False)
    client = JobClient(con)
    job.apply_counted_links(client, "core", "agent")
    from core.detect import sqlrun
    sqlrun.apply_views(client, "core", "agent")
    names = [sqlrun.object_name(s) for s in client.sql if s.lstrip().startswith("CREATE")]
    assert names[0] == "core.tvf_post_items"
    assert names.index("core.tvf_post_items") < names.index("core.tvf_item_window")
    con.close()


@pytest.mark.parametrize("missing", ["column", "end_table", "lineage_table"])
def test_missing_temporal_dependency_fails_detect_before_counts(missing):
    con = duck.connect(views=False)
    if missing == "column":
        con.execute("ALTER TABLE core.post_items DROP COLUMN linked_on")
    else:
        con.execute("DROP TABLE core." + ("post_item_end" if missing == "end_table" else "post_item_lineage"))
    chain = FakeChain(con)
    with pytest.raises(RuntimeError, match="Data issue: dated post-item resolver unavailable"):
        job.run(JobClient(con), D, chain=chain, core="core", agent="agent")
    assert chain.of("finish")[0][2] == "failed"
    assert chain.of("start_next") == []
    assert con.execute("SELECT COUNT(*) FROM core.item_daily").fetchone()[0] == 0
    con.close()
