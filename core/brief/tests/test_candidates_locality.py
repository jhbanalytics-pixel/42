"""The candidate statement of core/brief/sql/brief.sql with the locality edit of section 11.2, run on the harness.
The repository statement is the edited one; the frozen B0 ordering (B0_ORDERED) is the reference for rows on the v1 basis.
The v1 scope bucket and local_first stay in the statement (review N5): a row whose item_state.locality_basis is not
'locality_v2.1' orders exactly as the repository statement orders it, and a row on the v2 basis orders by the
checked locality status. """

from datetime import date, datetime, timedelta, timezone
import pytest

from core.api.today import _not_assessed
from core.brief import job
from core.brief.locality_audit import build_locality_audit
from core.detect import sqlrun
from core.detect.tests import duck
from core.trust.locality import read_locality, row_from_prefixed

UTC = timezone.utc
D = date(2026, 10, 7)
T = datetime(2026, 10, 7, 3, tzinfo=UTC)
RUN = "detect-1"
NL = chr(10)
V2 = "locality_v2.1"
STUBS = """
CREATE TABLE core.ms_stub (metric_date DATE, item_id VARCHAR, market VARCHAR, market_scope VARCHAR,
  total_posts7 BIGINT, market_posts7 BIGINT, market_news_posts7 BIGINT);
CREATE TABLE core.lf_stub (item_id VARCHAR, creators BIGINT);
"""
COLUMNS = ("metric_version", "population_posts", "known_posts", "local_posts", "foreign_posts", "unknown_posts",
           "status", "local_share", "population_digest", "population_cutoff", "checked_status")


B0_ORDERED = """ordered AS (
SELECT s.*, cm.kind map_kind, cm.status map_status, cm.label, cm.canonical_key, fs.first_seen,
  seen.platforms seen_platforms, ms.market_scope _selection_market_scope,
  ROW_NUMBER() OVER (ORDER BY s.eligible IS NOT TRUE,
    CASE WHEN ms.market_scope = 'market' AND ms.total_posts7 >= 3 AND ms.market_posts7 >= 2 THEN 0
         WHEN ms.market_scope = 'market' THEN 1 ELSE 2 END,
    IFNULL(lf.creators, 0) < 2,
    IFNULL(s.creators3, 0) < 2 OR IFNULL(s.posts3, 0) < 3,
    s.worth_raw IS NULL, s.worth_raw DESC, s.item_id) _selection_sql_rank
FROM {core}.v_item_state_current s
LEFT JOIN {core}.cultural_map cm ON cm.item_id = s.item_id AND cm.valid_to IS NULL
LEFT JOIN fs ON fs.item_id = s.item_id
LEFT JOIN seen ON seen.item_id = s.item_id
LEFT JOIN local_first lf ON lf.item_id = s.item_id
LEFT JOIN {core}.v_item_market_scope ms
  ON ms.metric_date = s.metric_date AND ms.item_id = s.item_id AND ms.market = s.market
WHERE s.metric_date = @d AND s.market = @market),
selection_snapshot AS (
  SELECT COUNT(*) total_count, COUNTIF(eligible IS TRUE) eligible_count,
    COUNT(DISTINCT run_id) detect_run_count, MAX(run_id) detect_run_id,
    ARRAY_AGG(IF(eligible IS TRUE,
      STRUCT(item_id, label, canonical_key, map_kind, _selection_sql_rank AS sql_rank,
             _selection_market_scope AS market_scope), NULL)
      IGNORE NULLS ORDER BY _selection_sql_rank) items
  FROM ordered)
SELECT ordered.*,
  IF(_selection_sql_rank = 1, TO_JSON_STRING(STRUCT(
    selection_snapshot.total_count, selection_snapshot.eligible_count,
    selection_snapshot.detect_run_count, selection_snapshot.detect_run_id, selection_snapshot.items)), NULL)
    _selection_snapshot
FROM ordered CROSS JOIN selection_snapshot
ORDER BY _selection_sql_rank
LIMIT 90;
"""


def stubbed(sql, ordered=None):
    """The statement with the two CTEs that read posts (sourced, local_first) and the scope view replaced by tables
    the test fills, so the same stub feeds the repository statement and the frozen B0 one. ordered: the text of
    the ordered CTE onward to use instead of the statement's own."""
    head = sql[:sql.index("sourced AS (")] + "local_first AS (SELECT item_id, creators FROM {core}.lf_stub),"
    tail = ordered if ordered is not None else job.QUERIES["candidates"][job.QUERIES["candidates"].index("ordered AS ("):]
    return (head + NL + tail).replace("{core}.v_item_market_scope ms", "{core}.ms_stub ms")


def put(con, item, known, local, status, version=V2, unknown=0, run=RUN, day=D, verified=True, market="NG", **over):
    row = {"run_date": day, "market": market, "item_id": item, "detect_run_id": run, "population_cutoff": T,
           "metric_version": version, "schema_version": 1, "computed_at": T, "population_posts": known + unknown,
           "known_posts": known, "local_posts": local, "foreign_posts": known - local, "unknown_posts": unknown,
           "feed_only_posts": 0, "vetoed_feed_posts": 0, "local_creators": local, "known_creators": known,
           "feed_only_creators": 0, "breadth_creators": local, "status": status,
           "local_share": local / known if known else None, "population_digest": "d" * 64, **over}
    duck.load(con, "core.item_locality", [row])
    if verified:
        duck.load(con, "core.item_locality_verified", [{
            "run_date": day, "market": market, "item_id": item, "detect_run_id": run, "metric_version": version,
            "verified_at": T, "member_rows": known + unknown, "population_digest": "d" * 64}])


def state(con, item, eligible, worth, basis=V2, run=RUN, day=D, creators3=3, posts3=3, market="NG"):
    duck.load(con, "core.item_state", [{
        "metric_date": day, "market": market, "item_id": item, "kind": "topic", "state_raw": "emerging", "state": "emerging",
        "untested": False, "creators3": creators3, "posts3": posts3, "worth_raw": worth, "geo_status": "market_unconfirmed",
        "eligible": eligible, "run_id": run, "rule_version": "r", "locality_basis": basis}])
    if not duck.query(con, "SELECT 1 x FROM {core}.cultural_map WHERE item_id = @i", {"i": item}):
        duck.load(con, "core.cultural_map", [{"item_id": item, "kind": "topic", "canonical_key": item, "label": item,
                                              "status": "active", "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
                                              "valid_to": None}])


@pytest.fixture
def con():
    c = duck.connect()
    c.execute(STUBS)
    duck.load(c, "agent.runs", [
        {"run_id": RUN, "stage": "detect", "run_date": D, "status": "ok", "started_at": T, "finished_at": T + timedelta(minutes=20)},
        {"run_id": "detect-0", "stage": "detect", "run_date": D - timedelta(days=1), "status": "ok",
         "started_at": T - timedelta(days=1), "finished_at": T - timedelta(days=1) + timedelta(minutes=20)}])
    for n in range(100):                                      # 100 eligible leaders, local and unconfirmed alternating
        state(c, f"e{n:03d}", True, 0.9 - n / 1000)
        put(c, f"e{n:03d}", 8, 6, "local") if n % 2 == 0 else put(c, f"e{n:03d}", 3, 3, "market_unconfirmed")
    for item, worth, known, local, status in (("lw2", .32, 8, 6, "local"), ("uw", .31, 3, 3, "market_unconfirmed"),
                                              ("lw", .30, 8, 6, "local"), ("uw2", .29, 3, 3, "market_unconfirmed"),
                                              ("d1", .20, 3, 3, "market_unconfirmed")):
        state(c, item, True, worth)
        put(c, item, known, local, status)
    state(c, "br1", True, 0.01)                               # same bucket as d1; only breadth separates br1 from br2
    put(c, "br1", 3, 3, "market_unconfirmed", local_creators=1, feed_only_creators=0, breadth_creators=1)
    state(c, "br2", True, 0.0)                                # local_creators 1 as br1, but the union count is 2
    put(c, "br2", 3, 3, "market_unconfirmed", local_creators=1, feed_only_creators=1, breadth_creators=2)
    state(c, "bad", True, 0.98)                               # stored status disagrees with its own counts
    put(c, "bad", 8, 0, "local")
    state(c, "bad2", True, 0.5)                               # unreadable and a breadth of 5 that must not be trusted
    put(c, "bad2", 8, 0, "local", breadth_creators=5)
    state(c, "dx", True, 0.15)                                # no locality row at all
    state(c, "dn", True, 0.99)                                # a stale eligible row whose v2 status is not_local
    put(c, "dn", 9, 1, "not_local")
    state(c, "d3", False, 0.1)                                # v1 admitted it; v2 says not_local, so eligible is false
    put(c, "d3", 8, 0, "not_local")
    state(c, "ix", False, 0.3)                                # ineligible and unreadable: not counted by the audit
    # another market of the same run, and another run of the same date: the audits must not list or count them
    state(c, "zaitem", True, 0.4, market="ZA")
    put(c, "zaitem", 8, 0, "not_local", market="ZA")
    state(c, "zamiss", True, 0.4, market="ZA")
    state(c, "orun", True, 0.4, run="detect-other")
    # another date, a good run of its own: the audits must not list or count it
    state(c, "old", True, 0.5, run="detect-0", day=D - timedelta(days=1))
    put(c, "old", 8, 0, "not_local", run="detect-0", day=D - timedelta(days=1))
    state(c, "oldbad", True, 0.5, run="detect-0", day=D - timedelta(days=1))
    put(c, "oldbad", 8, 0, "local", run="detect-0", day=D - timedelta(days=1))
    return c


def candidates(con, sql=None):
    return duck.query(con, sql or stubbed(job.QUERIES["candidates"]), {"d": D, "market": "NG"})


def without_leaders(con):
    for n in range(100):
        con.execute("DELETE FROM core.item_state WHERE item_id = ?", [f"e{n:03d}"])


def test_every_candidate_row_read_through_the_python_reader_agrees_with_the_sql_status(con):
    rows = candidates(con)
    assert rows
    for r in rows:
        if r["lrow_checked_status"] is None:
            assert r["item_id"] == "dx"
            continue
        record = row_from_prefixed(r)
        assert read_locality(record).status == r["lrow_checked_status"], r["item_id"]


def test_a_second_metric_version_in_the_same_run_does_not_duplicate_a_candidate(con):
    put(con, "e000", 8, 6, "local", version="locality_v2.2")
    ids = [r["item_id"] for r in candidates(con)]
    assert len(ids) == len(set(ids))


def test_the_pool_is_ordered_by_worth_alone_among_local_and_unconfirmed_rows(con):
    """Q3: the two admissible statuses are tied. The leaders alternate statuses, so a bucket that put local ahead of
    unconfirmed (or the reverse) would reorder them."""
    rows = candidates(con)
    assert len(rows) == job.POOL
    leaders = [r["item_id"] for r in rows]
    assert leaders == [f"e{n:03d}" for n in range(job.POOL)]
    without_leaders(con)
    assert [r["item_id"] for r in candidates(con)] == [
        "lw2", "uw", "lw", "uw2", "d1", "br2", "br1",          # bucket 0 by key then worth: local and unconfirmed tied
        "bad", "bad2", "dx",                                    # bucket 1: unreadable and missing, behind every readable row
        "dn",                                                   # bucket 2: a stale eligible not_local row
        "ix", "d3"]                                             # ineligible rows last, bucket 1 before bucket 2


def test_an_unreadable_rows_stored_breadth_is_not_trusted(con):
    without_leaders(con)
    ids = [r["item_id"] for r in candidates(con)]
    assert ids.index("bad") < ids.index("bad2") < ids.index("dx")       # worth decides: .98, .5, .15


def test_breadth_is_the_union_count_not_the_local_creator_count(con):
    without_leaders(con)
    ids = [r["item_id"] for r in candidates(con)]
    assert ids.index("br2") < ids.index("br1")                # worth says br1 first; the union count of 2 puts br2 ahead


def test_the_scope_handed_to_the_audit_comes_from_the_checked_status(con):
    without_leaders(con)
    by_id = {r["item_id"]: r for r in candidates(con)}
    assert by_id["bad"]["_selection_market_scope"] is None and by_id["d1"]["_selection_market_scope"] == "market"
    assert by_id["dn"]["_selection_market_scope"] == "global"


def test_rows_on_the_v1_basis_order_exactly_as_the_repository_statement_orders_them(con):
    """Review N5: the release order puts the brief first, so rows of a v1 detect run meet the new statement."""
    con.execute("DELETE FROM core.item_state")
    rows = (("v1a", .5, "market", 5, 4, 3, 5, 5), ("v1b", .9, "market", 1, 1, 0, 5, 5), ("v1c", .8, "global", 0, 0, 4, 5, 5),
            ("v1d", .7, "market", 5, 4, 0, 5, 5), ("v1e", .95, None, 0, 0, 0, 1, 1), ("v1f", .6, "market", 4, 3, 3, 0, 0))
    for item, worth, scope, total, mkt, lf, c3, p3 in rows:
        state(con, item, True, worth, basis="v1" if item != "v1f" else None, creators3=c3, posts3=p3)
        if scope:
            duck.load(con, "core.ms_stub", [{"metric_date": D, "item_id": item, "market": "NG", "market_scope": scope,
                                              "total_posts7": total, "market_posts7": mkt,
                                              "market_news_posts7": 0}])
        if lf:
            duck.load(con, "core.lf_stub", [{"item_id": item, "creators": lf}])
    # a v2 locality row exists for v1b: it must be ignored while the basis is v1
    put(con, "v1b", 8, 0, "not_local")
    original = candidates(con, stubbed(job.QUERIES["candidates"], B0_ORDERED))
    edited = candidates(con)
    assert [r["item_id"] for r in edited] == [r["item_id"] for r in original]
    assert [r["_selection_market_scope"] for r in edited] == [r["_selection_market_scope"] for r in original]
    assert [r["item_id"] for r in edited] != sorted((r["item_id"] for r in edited), key=lambda i: -dict((x[0], x[1]) for x in rows)[i])


def test_the_audit_statement_lists_the_not_local_item_the_pool_cannot_show_and_only_this_dates_rows(con):
    text = sqlrun.render(job.QUERIES["not_local_audit"], "core", "agent")
    rows = duck.query(con, text, {"d": D, "market": "NG"})
    assert [(r["item_id"], r["known_posts"], r["local_posts"], r["not_local_total"]) for r in rows] == [("dn", 9, 1, 2), ("d3", 8, 0, 2)]


def test_the_unreadable_count_covers_a_missing_row_and_contradicting_rows_of_eligible_items_of_this_run_only(con):
    text = sqlrun.render(job.QUERIES["unreadable_audit"], "core", "agent")
    assert duck.query(con, text, {"d": D, "market": "NG", "run_id": RUN}) == [{"unreadable_total": 3}]   # dx, bad, bad2


def test_the_locality_audit_block_is_separate_and_core_api_not_assessed_still_reads_the_payload():
    """Review N1. The function under test is the repository's own validator. One valid out-of-pool entry plus a
    not_local entry inside not_assessed makes it return None for the whole block (the defect); the audit block
    beside it changes nothing."""
    na = {"detect_run_id": "detect-1", "pool_limit": 90, "judged_limit": 10, "count": 1, "items": [
        {"item_id": "i1", "title": "One", "status": "not_assessed", "reason": "outside_candidate_pool", "sql_rank": 91,
         "pool_rank": None, "market_scope": "market"}]}
    audit = build_locality_audit("detect-1", [{"item_id": "dn", "label": "DN", "known_posts": 9, "local_posts": 1,
                                                "local_share": 1 / 9, "not_local_total": 2},
                                               {"item_id": "i1", "label": "One", "known_posts": 8, "local_posts": 0,
                                                "local_share": 0.0, "not_local_total": 2}], 3, represented_ids={"i1"})
    payload = {"not_assessed": na, "locality_audit": audit}
    got = _not_assessed(payload)
    assert got is not None and got["count"] == 1 and got["items"][0]["item_id"] == "i1"
    broken = {"not_assessed": dict(na, count=2, items=na["items"] + [
        {"item_id": "dn", "title": "DN", "status": "not_assessed", "reason": "not_local", "sql_rank": 2000,
         "pool_rank": None, "market_scope": "global"}])}
    assert _not_assessed(broken) is None                      # why the not_local entries do not go into not_assessed
    assert audit["block_version"] == 1 and audit["not_local_total"] == 2 and audit["unreadable_total"] == 3
    assert [i["item_id"] for i in audit["items"]] == ["dn"]   # i1 is already represented, so it is not listed twice


def test_the_audit_block_caps_its_listing_and_keeps_the_total_apart():
    rows = [{"item_id": f"i{n:03d}", "label": f"I{n}", "known_posts": 9, "local_posts": 1, "local_share": 1 / 9,
             "not_local_total": 60} for n in range(60)]
    block = build_locality_audit("detect-1", rows, 0)
    assert (block["not_local_total"], block["listed"], len(block["items"])) == (60, 50, 50)
