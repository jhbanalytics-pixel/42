"""The window numbers are reproducible for the detect run: bounded by the run's start, capped in bytes, partition filtered.

rival_window reads post_observations up to the detect run's started_at, so a post observed after the run cannot change
a number K2 re-runs, and the cutoff is stored with every window number and part of its query id.
"""

import re
from datetime import timedelta

from core.brief import evidence
from core.brief.holds_report import MAX_BYTES
from core.brief.tests.test_brief_evidence import DETECT, add_post, build, canonical_hash, current, utc, world
from core.brief.tests.test_brief_rivals_evidence import rival_world, window_row
from core.detect.tests import duck
from core.detect.tests.fixtures import D, at, day

CUTOFF = at(D, 11)          # run("detect", D) starts at 11:00 UTC on D
WINDOW_NUMBERS = ["posts7", "burst_share", "top3_share", "near_dup_share"]
WINDOW_PINS = ["small_at", "large_at"]


def window_entries(pack):
    return [e for e in pack["numbers"] + pack.get("pinned", []) if e.get("rival_field") in WINDOW_NUMBERS + WINDOW_PINS]


def add_late(con, pid, observed_at, creator="c8"):
    add_post(con, pid, creator, utc(day(1), 10, 30), 5, creator_tier_at_post="macro")
    con.execute("UPDATE core.post_observations SET observed_at = ? WHERE post_id = ?", [observed_at, pid])


def test_every_window_value_stores_the_detect_runs_start_as_its_cutoff_and_state_values_store_none():
    pack, *_ = build(rival_world())
    entries = window_entries(pack)
    assert [e["rival_field"] for e in entries] == WINDOW_NUMBERS + WINDOW_PINS
    assert {e["cutoff"] for e in entries} == {CUTOFF.isoformat()}
    assert len({e["post_snapshot"] for e in entries}) == 1 and entries[0]["post_snapshot"].startswith("sha256:")
    state = [e for e in pack["numbers"] + pack["pinned"] if e not in entries and "rival_field" in e]
    assert state and not any("cutoff" in e for e in state)


def test_the_cutoff_is_part_of_the_window_query_id_and_not_of_the_state_query_id():
    first, *_ = build(rival_world())
    con = rival_world()
    con.execute("UPDATE agent.runs SET started_at = ? WHERE run_id = ?", [at(D, 12), DETECT])
    second, *_ = build(con)
    ids = lambda pack: {e["rival_field"]: e["query_id"] for e in pack["numbers"] + pack["pinned"]  # noqa: E731
                        if "rival_field" in e}
    a, b = ids(first), ids(second)
    assert all(a[f] != b[f] for f in WINDOW_NUMBERS + WINDOW_PINS)
    assert all(a[f] == b[f] for f in a if f not in WINDOW_NUMBERS + WINDOW_PINS)


def test_a_post_observed_after_the_cutoff_leaves_every_rerun_and_result_hash_unchanged():
    con = rival_world()
    pack, _, rerun, _ = build(con)
    add_late(con, "p_late", at(D, 15))
    assert window_row(con)["posts7"] == 15              # the late post is in the data, and an uncut read sees it
    for e in window_entries(pack):
        now = rerun(e)
        assert now == e["value"], e["query_id"]
        if e["rival_field"] in WINDOW_NUMBERS:
            assert canonical_hash([{"value": now}]) == e["result_hash"]


def test_a_post_item_link_written_after_the_pack_cannot_move_a_pinned_value():
    """The reviewer's fixture: the brief's own confirm step links a post that already had a pre-cutoff observation
    in the market, between building the pack and the K2 re-run."""
    con = rival_world()
    add_post(con, "p_x", "c9", utc(day(1), 10, 30), 5, item="i2")      # observed before the cutoff, linked elsewhere
    pack, _, rerun, _ = build(con)
    posts7 = next(e for e in pack["numbers"] if e.get("rival_field") == "posts7")
    assert posts7["value"] == 14
    duck.load(con, "core.post_items", [{"post_id": "p_x", "item_id": "i1", "via": "confirm"}])
    assert window_row(con)["posts7"] == 15                              # a live read now counts the new link
    for e in window_entries(pack):
        now = rerun(e)
        assert now == e["value"], e["query_id"]
        if e["rival_field"] in WINDOW_NUMBERS:
            assert canonical_hash([{"value": now}]) == e["result_hash"]
    row = k2_posts7(pack, rerun, posts7)
    assert row["verdict"] == "pass"


def k2_posts7(pack, rerun, entry):
    from core.brief.tests.test_brief_rivals_evidence import k2_row
    return k2_row(pack, rerun, "14 posts in seven days.", [entry])


def test_with_the_cutoff_after_every_observation_the_window_values_equal_the_detect_table_function():
    con = rival_world()
    pack, *_ = build(con)
    live = window_row(con)
    got = {e["rival_field"]: e["value"] for e in pack["numbers"] + pack["pinned"] if "rival_field" in e}
    for field in WINDOW_NUMBERS:
        assert got[field] == live[field], field
    assert evidence.datetime.fromisoformat(got["small_at"]) == live["small_at"]
    assert evidence.datetime.fromisoformat(got["large_at"]) == live["large_at"]


def test_a_detect_run_with_no_start_time_pins_no_window_value_and_keeps_the_state_values(capsys):
    con = rival_world()
    con.execute("UPDATE agent.runs SET started_at = NULL WHERE run_id = ?", [DETECT])
    pack, *_ = build(con)
    assert not window_entries(pack)
    assert {e["rival_field"] for e in pack["numbers"] if "rival_field" in e} == {
        "sponsored_share", "local_share", "markets_hot"}
    assert "rival_cutoff_missing" in capsys.readouterr().err


class Recording(duck.Client):
    def __init__(self, con):
        super().__init__(con)
        self.caps = []

    def query(self, sql, job_config=None):
        self.caps.append((sql, job_config.maximum_bytes_billed if job_config else None))
        return super().query(sql, job_config)


def test_the_new_queries_run_under_the_brief_byte_cap_and_the_old_ones_are_untouched():
    con = rival_world()
    client = Recording(con)
    evidence.build_pack(client, current(con), D, "ZA", core="core", agent="agent")
    capped = [sql for sql, cap in client.caps if cap == MAX_BYTES]
    assert MAX_BYTES == 64 * 1024 * 1024
    assert len(capped) == 4
    assert sum("st.diffusion" in s for s in capped) == 1 and sum("r.started_at" in s for s in capped) == 1
    assert sum("po.observed_at <= @cutoff" in s for s in capped) == 2          # the snapshot and the window
    assert all(cap is None for sql, cap in client.caps if sql not in capped)


def test_a_run_that_did_not_write_this_items_state_gives_no_cutoff(capsys):
    con = rival_world()
    row = current(con)
    con.execute("UPDATE core.item_state SET run_id = 'detect-other' WHERE item_id = 'i1'")
    pack, *_ = evidence.build_pack(duck.Client(con), {**row, "run_id": DETECT}, D, "ZA", core="core", agent="agent")
    assert not window_entries(pack) and "rival_cutoff_missing" in capsys.readouterr().err


def test_each_new_query_filters_its_partitioned_tables():
    window, state, cutoff, posts = (evidence.QUERIES[n] for n in
                                    ("rival_window", "rival_state", "rival_cutoff", "rival_posts"))
    for sql in (window, posts):
        assert "po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 27 DAY) AND @d" in sql
        assert "po.observed_at <= @cutoff" in sql
    assert "post_items" not in window and "po.post_id IN UNNEST(SPLIT(@post_ids, CHR(10)))" in window
    assert "st.metric_date = @d" in state and "st.run_id = @run_id" in state
    assert re.search(r"r\.run_date BETWEEN DATE_SUB\(@d, INTERVAL 1 DAY\) AND DATE_ADD\(@d, INTERVAL 2 DAY\)", cutoff)


def test_a_post_with_no_near_duplicate_size_is_not_measured_as_zero_near_duplicates():
    con = rival_world()
    con.execute("UPDATE core.post_enrichment SET near_dup_size = NULL")
    pack, *_ = build(con)
    assert not any(e.get("rival_field") == "near_dup_share" for e in pack["numbers"])
    assert window_row(con)["near_dup_share"] == 0.0              # the table function reads a missing size as 1


def test_near_duplicates_are_measured_over_the_posts_that_have_a_size():
    con = rival_world()
    con.execute("UPDATE core.post_enrichment SET near_dup_size = NULL WHERE post_id NOT IN ('p0', 'p5')")
    pack, *_ = build(con)
    share = next(e for e in pack["numbers"] if e.get("rival_field") == "near_dup_share")
    assert share["value"] == 0.5                                 # p0 has size 3 and p5 has size 1


def test_the_pack_says_how_the_rival_read_went():
    assert build(rival_world())[0]["rival_read"] == "ok"
    assert "rival_read" not in build(world())[0]
    con = rival_world()
    con.execute("UPDATE agent.runs SET started_at = NULL WHERE run_id = ?", [DETECT])
    assert build(con)[0]["rival_read"] == "cutoff_missing"


def test_a_failed_rival_read_is_marked_on_the_pack():
    con = rival_world()

    class Broken(duck.Client):
        def query(self, sql, job_config=None):
            if "po.observed_at <= @cutoff" in sql:
                raise RuntimeError("window query failed")
            return super().query(sql, job_config)

    pack, *_ = evidence.build_pack(Broken(con), current(con), D, "ZA", core="core", agent="agent")
    assert pack["rival_read"] == "failed"


def test_a_later_observation_of_a_known_post_cannot_change_a_pinned_value():
    """The cutoff bounds the window query as well as the snapshot: a post the pack already holds, seen again after the
    run in a measured lane, would otherwise become a measured small post and move small_at."""
    con = rival_world()
    add_post(con, "p_old", "c9", utc(day(20), 8), 5, sightings=(("exploration", "search_presence"),),
             creator_tier_at_post="nano")
    pack, _, rerun, _ = build(con)
    small = next(e for e in pack["pinned"] if e["rival_field"] == "small_at")
    assert evidence.datetime.fromisoformat(small["value"]) == utc(day(5), 11)
    duck.load(con, "core.post_observations", [{
        "post_id": "p_old", "observed_at": at(D, 15), "observed_date": D, "market": "ZA", "platform": "tiktok",
        "lane": "sweep", "lane_class": "unbiased_rank", "run_id": "collect-late"}])
    assert window_row(con)["small_at"] == utc(day(20), 8)
    assert rerun(small) == small["value"]
