"""Offline replay of a retained day through the a80 understand and scope code (ops/evaluation/replay_a80.py).

The small tests build a synthetic snapshot with the same seven parquet files the retained days carry, so they run
anywhere. The last block reads the retained snapshots; it runs only when F42_REPLAY_SNAPSHOTS names that folder."""
import difflib
import hashlib
import os
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from core.brief import job
from core.detect import sqlrun
from ops.evaluation import replay_a80 as ra

DAY = "2026-10-05"
D = date(2026, 10, 5)
NOON = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
FILES = ["observations", "post_items", "posts", "clusters", "cluster_members", "cultural_map",
         "suppressed_creators"]


def parquet(folder, name, columns, rows):
    """One parquet file from python rows; columns is [(name, duckdb type)]."""
    with duckdb.connect() as con:
        con.execute(f"CREATE TABLE t ({', '.join(f'{n} {t}' for n, t in columns)})")
        for row in rows:
            con.execute(f"INSERT INTO t VALUES ({', '.join('?' * len(columns))})", list(row))
        con.execute(f"COPY t TO '{(folder / (name + '.parquet')).as_posix()}' (FORMAT PARQUET)")


OBS = [("post_id", "VARCHAR"), ("observed_at", "TIMESTAMPTZ"), ("observed_date", "DATE"), ("market", "VARCHAR"),
       ("source_market", "VARCHAR"), ("source_region", "VARCHAR"), ("platform", "VARCHAR"), ("route", "VARCHAR"),
       ("lane", "VARCHAR"), ("lane_class", "VARCHAR")]
POSTS = [("post_id", "VARCHAR"), ("platform", "VARCHAR"), ("published_at", "TIMESTAMPTZ"), ("post_date", "DATE"),
         ("geo_market", "VARCHAR"), ("geo_confidence", "DOUBLE"), ("geo_source", "VARCHAR"),
         ("geo_scope", "INTEGER"), ("creator_id_hash", "VARCHAR"), ("creator_key_hash", "VARCHAR")]
MAP = [("item_id", "VARCHAR"), ("kind", "VARCHAR"), ("canonical_key", "VARCHAR"), ("label", "VARCHAR"),
       ("first_seen", "DATE"), ("first_seen_market", "VARCHAR"), ("last_seen", "DATE"), ("lifecycle", "INTEGER"),
       ("status", "VARCHAR"), ("valid_from", "TIMESTAMPTZ"), ("valid_to", "TIMESTAMPTZ")]


def synthetic(tmp_path):
    """A one day snapshot, one post per creator, all seen on the Nigerian feed: veto (four posts located in ZA and
    two not located), home (four located in NG), open (four not located). Plus a cluster world."""
    folder = tmp_path / DAY
    folder.mkdir()
    obs, posts, links = [], [], []
    for item, geo, count in (("veto", "ZA", 4), ("home", "NG", 4), ("open", None, 4), ("veto", None, 2)):
        for n in range(count):
            pid = f"{item}{n}{geo or 'u'}"
            obs.append((pid, NOON, D, "NG", "NG", "NG", "tiktok", "tiktok/feed", "sweep", "unbiased_rank"))
            posts.append((pid, "tiktok", NOON, D, geo, 0.9 if geo else None, "ext_region" if geo else None, None,
                          f"c-{pid}", f"tiktok:c-{pid}"))
            links.append((pid, item, "hashtag"))
    # cluster world: topic tA keeps x1 and x3 as inliers and holds x2 as a reassigned outlier; tB is outliers only
    members = [("20261005-ng-000", "x1", 0.9), ("20261005-ng-000", "x2", 0.0), ("20261005-ng-000", "x3", 0.5),
               ("20261005-ng-001", "y1", 0.0), ("20261005-ng-001", "y2", 0.0),
               ("20261005-pan-000", "x1", 0.7), ("20261005-pan-000", "x2", 0.0),
               ("20261004-ng-000", "w1", 0.0)]
    for pid in ("x1", "x2", "x3", "y1", "y2", "z1", "w1"):
        obs.append((pid, NOON, D, "NG", "NG", "NG", "tiktok", "tiktok/feed", "sweep", "unbiased_rank"))
        posts.append((pid, "tiktok", NOON, D, None, None, None, None, f"c-{pid}", f"tiktok:c-{pid}"))
    links += [("x1", "tA", "cluster"), ("x2", "tA", "cluster"), ("x3", "tA", "cluster"), ("y1", "tB", "cluster"),
              ("y2", "tB", "cluster"), ("x1", "tP", "cluster_pan"), ("x2", "tP", "cluster_pan"),
              ("z1", "tC", "cluster"), ("x2", "hx", "hashtag")]
    clusters = [(D, "20261005-ng-000", "ng", "tA", "match"), (D, "20261005-ng-001", "ng", "tB", "new"),
                (D, "20261005-pan-000", "ng", "tP", "match"), (D - timedelta(days=1), "20261004-ng-000", "ng", "tW", "new")]
    items = ["veto", "home", "open", "tA", "tB", "tP", "tC", "hx"]
    cmap = [(i, "topic" if i.startswith("t") else "hashtag", i, f"label {i}", D, "NG", D, 0, "active",
             NOON - timedelta(days=30), None) for i in items]
    parquet(folder, "observations", OBS, obs)
    parquet(folder, "posts", POSTS, posts)
    parquet(folder, "post_items", [("post_id", "VARCHAR"), ("item_id", "VARCHAR"), ("via", "VARCHAR")], links)
    parquet(folder, "clusters", [("cluster_date", "DATE"), ("cluster_id", "VARCHAR"), ("market", "VARCHAR"),
                                 ("item_id", "VARCHAR"), ("match_kind", "VARCHAR")], clusters)
    parquet(folder, "cluster_members", [("cluster_id", "VARCHAR"), ("post_id", "VARCHAR"),
                                        ("probability", "DOUBLE")], members)
    parquet(folder, "cultural_map", MAP, cmap)
    parquet(folder, "suppressed_creators", [("creator_id_hash", "VARCHAR")], [])
    return tmp_path


def scope_by_item(con, variant):
    frame = ra.scope_frame(con, variant)
    return {r.item_id: (r.market_scope, r.market_posts7, r.total_posts7) for r in frame.itertuples()}


@pytest.fixture
def world(tmp_path):
    snap = synthetic(tmp_path)
    con = ra.load_world(snap, DAY)
    ra.add_derived_state_keys(con, DAY, ("NG",))
    yield con
    con.close()


# Inputs: the checksum file proves nothing about itself, so its own digest is pinned

def sums_for(folder, names):
    lines = [f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}" for n in names]
    sums = folder / "SHA256SUMS.txt"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return hashlib.sha256(sums.read_bytes()).hexdigest()


def test_checksums_pass_when_every_file_matches_and_the_sums_file_is_the_pinned_one(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"one")
    (tmp_path / "b.bin").write_bytes(b"two")
    pinned = sums_for(tmp_path, ["a.bin", "b.bin"])
    assert ra.verify_snapshots(tmp_path, sums_sha256=pinned) == 2


def test_a_changed_file_fails_the_checksum(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"one")
    pinned = sums_for(tmp_path, ["a.bin"])
    (tmp_path / "a.bin").write_bytes(b"one!")
    with pytest.raises(ra.ReplayInputError, match="a.bin"):
        ra.verify_snapshots(tmp_path, sums_sha256=pinned)


def test_a_file_and_its_line_rewritten_together_fail_the_pinned_sums_digest(tmp_path):
    # Changing the file and recomputing its line leaves every line true, so only the pinned digest can object.
    (tmp_path / "a.bin").write_bytes(b"one")
    pinned = sums_for(tmp_path, ["a.bin"])
    (tmp_path / "a.bin").write_bytes(b"one!")
    sums_for(tmp_path, ["a.bin"])
    with pytest.raises(ra.ReplayInputError, match="SHA256SUMS.txt"):
        ra.verify_snapshots(tmp_path, sums_sha256=pinned)


def test_a_listed_file_that_is_missing_fails(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"one")
    pinned = sums_for(tmp_path, ["a.bin"])
    (tmp_path / "a.bin").unlink()
    with pytest.raises(ra.ReplayInputError, match="a.bin"):
        ra.verify_snapshots(tmp_path, sums_sha256=pinned)


def test_the_pinned_digest_is_a_sha256():
    assert len(ra.PINNED_SUMS_SHA256) == 64 and int(ra.PINNED_SUMS_SHA256, 16) >= 0


# The v1 scope view is a pinned copy; it must differ from the a80 view only by the veto

def view_statement(name, text):
    return next(s for s in text if sqlrun.object_name(s).endswith(name))


A80_COMMIT = "a80be1d"
REPO = Path(__file__).resolve().parents[2]


def a80_views_sql():
    """core/detect/sql/views.sql as it stood at the live staging commit, read from git."""
    done = subprocess.run(["git", "-C", str(REPO), "show", f"{A80_COMMIT}:core/detect/sql/views.sql"],
                          capture_output=True, check=True)
    return done.stdout.decode("utf-8").replace("\r\n", "\n")


def test_the_v1_view_differs_from_the_a80_view_only_in_the_veto_clauses():
    a80 = ra.scope_view_sql("a80").splitlines()
    v1 = ra.scope_view_sql("v1").splitlines()
    hunks = [op for op in difflib.SequenceMatcher(None, v1, a80).get_opcodes() if op[0] != "equal"]
    veto = [(a, b) for a, b in ((chr(10).join(v1[a0:a1]), chr(10).join(a80[b0:b1])) for _, a0, a1, b0, b1 in hunks)
            if "NULLIF" in b]
    assert len(veto) == 2, "one veto in creator_ranked and one in scored_posts"
    for a, b in ((chr(10).join(v1[a0:a1]), chr(10).join(a80[b0:b1])) for _, a0, a1, b0, b1 in hunks):
        if "NULLIF" not in b:
            assert b.replace(")", "", 1) == a, "the other hunks only close the new group with one more bracket"
    assert "NULLIF" not in chr(10).join(v1)


def test_the_a80_arm_is_the_pinned_copy_of_the_view_at_the_live_staging_commit_not_the_checkout():
    pinned = ra.SCOPE_A80.read_text(encoding="utf-8").strip()
    live = [s for s in sqlrun.split(a80_views_sql())
            if s.lstrip().startswith("CREATE OR REPLACE VIEW {core}.v_item_market_scope ")]
    assert len(live) == 1
    live = live[0].strip()
    assert pinned == live, f"scope_view_a80.sql is not the view in {A80_COMMIT}:core/detect/sql/views.sql"
    assert ra.scope_view_sql("a80") == sqlrun.render(pinned, "core", "agent").strip()


def test_the_a80_arm_does_not_read_the_checkouts_scope_view(monkeypatch):
    pinned = ra.scope_view_sql("a80")
    other = "CREATE OR REPLACE VIEW core.v_item_market_scope AS SELECT 1 AS metric_date"
    monkeypatch.setattr(sqlrun, "statements", lambda **_: [other])
    assert ra.scope_view_sql("a80") == pinned != other
    assert "market_news_posts7" not in pinned, "the a80 view predates the news scope column"


def a80_candidates_sql():
    """The candidates statement of core/brief/sql/brief.sql at the live staging commit, read from git."""
    done = subprocess.run(["git", "-C", str(REPO), "show", f"{A80_COMMIT}:core/brief/sql/brief.sql"],
                          capture_output=True, check=True)
    text = done.stdout.decode("utf-8").replace("\r\n", "\n")
    found = [s[s.index("-- name: candidates"):] for s in sqlrun.split(text) if "-- name: candidates" in s]
    assert len(found) == 1
    return found[0].strip()


def test_the_candidates_statement_the_replay_runs_is_the_pinned_copy_of_the_one_at_the_live_staging_commit():
    pinned = ra.CANDIDATES_A80.read_text(encoding="utf-8").strip()
    assert pinned == a80_candidates_sql(), f"candidates_a80.sql is not the statement in {A80_COMMIT}:core/brief/sql/brief.sql"
    assert "market_news_posts7" not in pinned and "v_item_locality_current" not in pinned


def test_judged_ten_and_the_pool_run_the_pinned_candidates_statement_not_the_checkouts(world, monkeypatch):
    ra.add_pinned_states(world, DAY, "NG", [
        {"item_id": "veto", "eligible": True, "creators3": 4, "posts3": 4, "worth_raw": 0.9},
        {"item_id": "home", "eligible": True, "creators3": 4, "posts3": 4, "worth_raw": 0.5},
        {"item_id": "open", "eligible": True, "creators3": 4, "posts3": 4, "worth_raw": 0.1}])
    ra.use_scope(world, ra.scope_frame(world, "a80"))
    ten, pool = ra.judged_ten(world, DAY, "NG"), ra._pool(world, DAY, "NG")
    assert ten == ["home", "open", "veto"] == pool
    monkeypatch.setitem(job.QUERIES, "candidates", "SELECT 'other' AS item_id")
    assert ra.judged_ten(world, DAY, "NG") == ten and ra._pool(world, DAY, "NG") == pool


def test_the_judged_ten_is_the_first_ten_of_the_pool(monkeypatch):
    rows = [{"item_id": f"i{n}", "label": f"Label {n}", "canonical_key": f"key {n}"} for n in range(12)]
    monkeypatch.setattr(ra.duck, "query", lambda con, sql, params=None: rows)
    assert ra.judged_ten(None, DAY, "NG") == [f"i{n}" for n in range(10)]
    assert ra._pool(None, DAY, "NG") == [f"i{n}" for n in range(12)]


def test_every_insert_in_the_replay_names_its_columns():
    """The harness tables grow columns (locality-v2b added two to post_items), so a positional insert breaks."""
    text = Path(ra.__file__).read_text(encoding="utf-8")
    inserts = re.findall(r"INSERT INTO\s+[\w.]+\s*(\(?)", text)
    assert inserts and all(inserts), "an INSERT INTO without a column list"


# Scope: a post the Nigerian feed saw but the post says is elsewhere

def test_v1_counts_a_foreign_located_post_the_market_feed_saw_and_a80_does_not(world):
    v1, a80 = scope_by_item(world, "v1"), scope_by_item(world, "a80")
    assert v1["veto"] == ("market", 6, 6)
    assert a80["veto"] == ("global", 2, 6)


def test_located_and_unlocated_posts_score_the_same_under_both_rules(world):
    v1, a80 = scope_by_item(world, "v1"), scope_by_item(world, "a80")
    for item in ("home", "open"):
        assert v1[item] == a80[item] == ("market", 4, 4)


def test_scope_frame_rejects_an_unknown_variant(world):
    with pytest.raises(ValueError):
        ra.scope_frame(world, "a79")


# Understand: reassigned outliers are the members with probability zero

def test_noise_table_counts_members_noise_and_vanishing_clusters(tmp_path):
    snap = synthetic(tmp_path)
    table = ra.noise_table(snap, DAY).set_index("kind")
    ng, pan = table.loc["ng"], table.loc["pan"]
    assert (ng.clusters, ng.members, ng.noise_members, ng.kept_members, ng.vanishing_clusters) == (2, 5, 3, 2, 1)
    assert (pan.clusters, pan.members, pan.noise_members, pan.kept_members, pan.vanishing_clusters) == (1, 2, 1, 1, 0)


def test_noise_table_leaves_other_days_clusters_out(tmp_path):
    snap = synthetic(tmp_path)
    assert set(ra.noise_table(snap, DAY)["kind"]) == {"ng", "pan"}
    assert ra.noise_table(snap, DAY)["members"].sum() == 7


def test_noise_table_reports_how_close_to_zero_the_smallest_positive_probability_is(tmp_path):
    snap = synthetic(tmp_path)
    assert ra.noise_table(snap, DAY).set_index("kind").loc["ng"].min_positive == pytest.approx(0.5)


def test_a80_links_drop_only_cluster_links_with_no_positive_member(tmp_path):
    snap = synthetic(tmp_path)
    kept = {(r[0], r[1]) for r in ra.load_world(snap, DAY, links="a80").execute(
        "SELECT post_id, item_id FROM core.post_items").fetchall()}
    recorded = {(r[0], r[1]) for r in ra.load_world(snap, DAY).execute(
        "SELECT post_id, item_id FROM core.post_items").fetchall()}
    assert recorded - kept == {("x2", "tA"), ("y1", "tB"), ("y2", "tB"), ("x2", "tP")}
    assert ("x1", "tA") in kept and ("x3", "tA") in kept and ("x1", "tP") in kept
    assert ("z1", "tC") in kept, "a cluster link with no member row in the window is unknown, so it stays"
    assert ("x2", "hx") in kept, "a hashtag link is not a cluster link"


def test_a80_links_change_the_items_scope(tmp_path):
    snap = synthetic(tmp_path)
    con = ra.load_world(snap, DAY, links="a80")
    ra.add_derived_state_keys(con, DAY, ("NG",))
    scope = scope_by_item(con, "a80")
    assert "tB" not in scope, "an item with every link dropped has no post left to be keyed by"
    assert scope["tA"][2] == 2
    ra.add_pinned_states(con, DAY, "NG", [{"item_id": "tB", "eligible": True}])
    assert scope_by_item(con, "a80")["tB"] == ("global", 0, 0)


# Candidates: the repo's own candidates statement, with the scope table swapped in

def test_judged_ten_follows_the_candidates_statement_with_the_scope_swapped_in(world):
    ra.add_pinned_states(world, DAY, "NG", [
        {"item_id": "veto", "eligible": True, "creators3": 4, "posts3": 4, "worth_raw": 0.9},
        {"item_id": "home", "eligible": True, "creators3": 4, "posts3": 4, "worth_raw": 0.5},
        {"item_id": "open", "eligible": True, "creators3": 4, "posts3": 4, "worth_raw": 0.1},
        {"item_id": "tA", "eligible": False, "creators3": 4, "posts3": 4, "worth_raw": 2.0}])
    ra.use_scope(world, ra.scope_frame(world, "v1"))
    assert ra.judged_ten(world, DAY, "NG") == ["veto", "home", "open", "tA"]
    ra.use_scope(world, ra.scope_frame(world, "a80"))
    # veto is global under a80, so it falls behind the two market rows; an ineligible row stays last
    assert ra.judged_ten(world, DAY, "NG") == ["home", "open", "veto", "tA"]


def test_pinned_states_replace_the_derived_keys_for_their_market(world):
    ra.add_pinned_states(world, DAY, "NG", [
        {"item_id": "home", "eligible": True, "creators3": 1, "posts3": 1, "worth_raw": 0.2}])
    assert world.execute("SELECT item_id FROM core.item_state WHERE market = 'NG'").fetchall() == [("home",)]


# Summaries of two scope frames and two candidate lists

def frame(rows):
    import pandas as pd

    return pd.DataFrame([{"metric_date": D, "item_id": i, "market": "NG", "market_scope": sc, "market_posts7": m,
                          "total_posts7": t, "market_share7": (m / t if t else None)} for i, sc, m, t in rows],
                        columns=["metric_date", "item_id", "market", "market_scope", "market_posts7", "total_posts7",
                                 "market_share7"])


def test_scope_counts_split_market_global_and_the_top_bucket():
    counts = ra.scope_counts(frame([("a", "market", 2, 3), ("b", "market", 2, 2), ("c", "market", 1, 1),
                                    ("d", "global", 0, 0), ("e", "global", 1, 4)]))
    assert counts == {"keys": 5, "market": 3, "global": 2, "bucket0": 1, "market_posts7": 6, "total_posts7": 10,
                      "empty": 1}


def test_the_top_bucket_needs_three_scoped_posts_and_two_market_posts():
    # brief.sql: market scope AND total_posts7 >= 3 AND market_posts7 >= 2. A real market row has both, so the rows
    # that probe each limit alone are made by hand.
    assert ra.scope_counts(frame([("a", "market", 1, 3)]))["bucket0"] == 0
    assert ra.scope_counts(frame([("b", "market", 2, 2)]))["bucket0"] == 0
    assert ra.scope_counts(frame([("c", "market", 2, 3)]))["bucket0"] == 1
    assert ra.scope_counts(frame([("d", "global", 2, 3)]))["bucket0"] == 0


def test_scope_counts_of_nothing_is_all_zero():
    assert ra.scope_counts(frame([])) == {"keys": 0, "market": 0, "global": 0, "bucket0": 0, "market_posts7": 0,
                                          "total_posts7": 0, "empty": 0}


def test_scope_changes_counts_flips_bucket_moves_and_vanished_keys():
    before = frame([("a", "market", 3, 3), ("b", "market", 1, 1), ("c", "global", 0, 3), ("d", "market", 3, 3)])
    after = frame([("a", "global", 1, 3), ("b", "market", 1, 1), ("c", "market", 3, 3), ("e", "market", 3, 3)])
    assert ra.scope_changes(before, after) == {
        "shared": 3, "market_to_global": 1, "global_to_market": 1, "bucket0_lost": 1, "bucket0_gained": 1,
        "only_before": 1, "only_after": 1}


def test_ten_changes_names_who_left_and_who_came_in():
    base = ["a", "b", "c", "d"]
    assert ra.ten_changes(base, base) == {"same_order": True, "same_set": True, "changed": 0, "entering": [],
                                          "leaving": []}
    assert ra.ten_changes(base, ["b", "a", "e", "c"]) == {"same_order": False, "same_set": False, "changed": 1,
                                                          "entering": ["e"], "leaving": ["d"]}


# One day end to end

def test_replay_day_reports_noise_scope_and_links_for_the_synthetic_day(tmp_path):
    snap = synthetic(tmp_path)
    out = ra.replay_day(snap, DAY)
    ng = out["understand"]["ng"]
    assert (ng["members"], ng["noise_members"], ng["kept_members"], ng["noise_share"]) == (5, 3, 2, 0.6)
    assert out["understand"]["pan"]["noise_share"] == 0.5
    rule = out["scope"]["NG"]["all"]["rule_effect"]
    assert rule["market_to_global"] == 1 and rule["global_to_market"] == 0
    links = out["scope"]["NG"]["topic"]["links_effect"]
    assert links["only_before"] == 1, "tB has no link left, so it has no key"


def test_replay_day_groups_items_by_kind(tmp_path):
    snap = synthetic(tmp_path)
    out = ra.replay_day(snap, DAY)
    assert out["scope"]["NG"]["topic"]["v1_recorded"]["keys"] == 4
    assert out["scope"]["NG"]["other"]["v1_recorded"]["keys"] == 4


def pinned_inputs(tmp_path, snap, home_scope=("market", 4)):
    """A manifest and a pinned state file for the synthetic day, with the scope rows equal to what v1 computes
    unless home_scope says otherwise (the warehouse reading differing from the snapshot's)."""
    import json

    (snap / f"manifest-{DAY}.json").write_text(json.dumps({"detect_run_id": "detect-test"}), encoding="utf-8")
    rows = []
    for item, worth, scope in (("veto", 0.9, ("market", 6, 6)), ("home", 0.5, (home_scope[0], home_scope[1], 4)),
                               ("open", 0.1, ("market", 4, 4)), ("tA", 2.0, ("market", 3, 3))):
        rows.append({"metric_date": DAY, "run_id": "detect-test", "item_id": item, "eligible": item != "tA",
                     "creators3": 4, "posts3": 4, "worth_raw": worth,
                     "scope": {"market_scope": scope[0], "market_posts7": scope[1], "total_posts7": scope[2],
                               "market_share7": 1.0}})
    states = tmp_path / "states.json"
    states.write_text(json.dumps({"days": {DAY: rows}}), encoding="utf-8")
    return states


def test_replay_pinned_orders_the_control_then_each_variant(tmp_path):
    snap = synthetic(tmp_path)
    out = ra.replay_pinned(snap, pinned_inputs(tmp_path, snap), DAY)
    assert out["control"]["ten"] == ["veto", "home", "open", "tA"]
    assert out["control"]["scope_agreement"] == {"keys": 4, "equal": 4, "same_scope": 4, "same_top_bucket": 4}
    assert out["variants"]["v1_recomputed"]["vs_control"]["same_order"] is True
    a80 = out["variants"]["a80_rule"]
    assert a80["ten"] == ["home", "open", "veto", "tA"]
    assert a80["vs_control"] == {"same_order": False, "same_set": True, "changed": 0, "entering": [], "leaving": []}


def test_replay_pinned_also_compares_every_variant_with_the_v1_view_rebuilt_from_the_snapshot(tmp_path):
    # The warehouse reading puts home outside the market; the snapshot's v1 view does not. The control order and
    # the rebuilt v1 order differ, and the a80 effect is read against the rebuilt one, which shares its approximations.
    snap = synthetic(tmp_path)
    out = ra.replay_pinned(snap, pinned_inputs(tmp_path, snap, home_scope=("global", 0)), DAY)
    assert out["control"]["ten"] == ["veto", "open", "home", "tA"]
    base = out["variants"]["v1_recomputed"]
    assert base["ten"] == ["veto", "home", "open", "tA"]
    assert base["vs_control"]["same_order"] is False and base["vs_v1_recomputed"]["same_order"] is True
    a80 = out["variants"]["a80_rule"]
    assert a80["vs_v1_recomputed"] == {"same_order": False, "same_set": True, "changed": 0, "entering": [],
                                       "leaving": []}
    assert a80["pool_vs_v1_recomputed"]["same_set"] is True


def test_replay_pinned_runs_the_a80_links_variants(tmp_path):
    snap = synthetic(tmp_path)
    out = ra.replay_pinned(snap, pinned_inputs(tmp_path, snap), DAY)
    full = out["variants"]["a80_full"]
    assert full["ten"][:3] == ["home", "open", "veto"]
    assert out["variants"]["v1_a80links"]["ten"][0] == "veto"


def test_replay_pinned_refuses_states_from_another_detect_run(tmp_path):
    import json

    snap = synthetic(tmp_path)
    states = pinned_inputs(tmp_path, snap)
    (snap / f"manifest-{DAY}.json").write_text(json.dumps({"detect_run_id": "detect-other"}), encoding="utf-8")
    with pytest.raises(ra.ReplayInputError, match="detect-test"):
        ra.replay_pinned(snap, states, DAY)


# The retained snapshots

SNAP_ENV = os.environ.get("F42_REPLAY_SNAPSHOTS")
retained = pytest.mark.skipif(not SNAP_ENV, reason="F42_REPLAY_SNAPSHOTS not set")

RECORDED_5_OCT_TEN = {
    "306b75a27e592b0d4fcbb6923937bd5408b8c6e1b022e4db00c8371cd11a9eba",
    "1df3f88973a32ed7e6102a4bdbf0bd6fc3e9f0c130c24c8ad9f9e5fc9db3b2b0",
    "9d2db15dbba1d1c8512e9c1df96fc0c7fc6924426229dd4ee0f58e6a6ff7bfac",
    "1b6715ab16faf660a2c440ed13d03d35d6152838b6e83843b2934a06f21684db",
    "dd4c5835d728fbf0590643f3b5820868c4bd88b13b761d3b562058d03bc35d25",
    "242d8356f96b16072fa8039170e281eed57b4ac056af1753180341224a10dec8",
    "d6b9ae3166da6a6881e4a8942ba1479d04448623513d20630e32769352d4dc6f",
    "b1818c033b46c189355bbab0d1e99e447e5305b63bdd6e2aba3eb20750f1c0d1",
    "2ebeff3b900892df6bf20a8d203f94bc8deef366921cdb93d736c2ea8534c103",
    "de19acb99481a95780bbcdcc33de7e0a1591654ad9722223c7d26630de0fd073",
}


@retained
def test_the_retained_snapshots_pass_their_pinned_checksums():
    assert ra.verify_snapshots(Path(SNAP_ENV)) == 36


@retained
def test_recorded_members_equal_the_snapshot_members_and_today_posts_in_every_cell():
    for day in ra.DATES:
        table = ra.noise_table(Path(SNAP_ENV), day).set_index("kind")
        for kind, (clusters, members, today, _) in ra.RECORDED_UNDERSTAND[day].items():
            row = table.loc[kind]
            assert (row.clusters, row.members) == (clusters, members), (day, kind)
            assert members == today, "the recorded run assigned every post sighted today"


@retained
def test_control_the_v1_candidate_order_rebuilds_the_recorded_ten_for_5_oct():
    states = os.environ.get("F42_REPLAY_STATES")
    assert states, "F42_REPLAY_STATES names rank-analysis.json"
    con = ra.control_world(Path(SNAP_ENV), Path(states), DAY)
    assert set(ra.judged_ten(con, DAY, "NG")) == RECORDED_5_OCT_TEN
