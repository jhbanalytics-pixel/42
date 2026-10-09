"""Rule (b) of the label-only change counts shared members between the item's cluster of this market today and its cluster
of the same market on the previous day, and nothing else (Albert's typed text of Q14, ruling of 9 Oct 2026). It acts
only under the v2 locality authority, renames an item at most once per run date, and leaves the records the review
needs in the counts. cluster_items.sql runs on the DuckDB harness of test_cluster.py, so the set is tested as the
statement builds it."""

import json
import math
from datetime import date, datetime, timedelta

import pytest

from core.conftest import set_locality_authority
from core.trust.locality import MIN_SHARED_MEMBERS
from core.understand import cluster
from core.understand.tests.test_cluster import (CORE, NetModel, clean_net, core_db, duck_execute, fake_run,  # noqa: F401
                                                map_row)
from core.understand.tests.test_label_only_change import (NEW_LABEL, NEW_WORDS, OLD_LABEL, OLD_WORDS, a_cluster, an_item,
                                                          at)

RUN = date(2026, 10, 7)
YESTERDAY = RUN - timedelta(days=1)


def world(clusters, members, runs=()):
    """A DuckDB core with one current topic row, item topic_a, the given clusters (date, id, market, label) and members
    (cluster id, post, probability)."""
    con = core_db()
    con.execute(f"INSERT INTO {CORE}.cultural_map VALUES ({', '.join('?' * 17)})",
                map_row("topic_a", [1.0, 0.0, 0.0], last_seen=YESTERDAY, label=OLD_LABEL))
    for day, cid, market, label in clusters:
        con.execute(f"INSERT INTO {CORE}.clusters VALUES (?, ?, ?, 'topic_a', 'match', ?, ?, [], NULL)",
                    [day, cid, market, label, OLD_WORDS])
    con.executemany(f"INSERT INTO {CORE}.cluster_members VALUES (?, ?, ?)", members)
    for run_id, market, summary in runs:
        con.execute(f"INSERT INTO {PROJECT_AGENT}.runs VALUES (?, 'understand_cluster_plan', ?, 'ready', NULL, NULL, ?)",
                    [run_id, RUN, json.dumps({"market": market, "batch_index": -1, "summary": summary})])
    return con


PROJECT_AGENT = '"ogilvy-trends-v2".intelligence_42_agent'


def recent(con, market="ng", cap=None):
    params = cluster.item_params(RUN, market)
    if cap is not None:
        params["member_cap"] = cap
    [item] = duck_execute(con)(cluster.load("cluster_items"), params)
    return item


def test_only_the_same_market_cluster_of_the_previous_day_counts():
    con = world(
        [(YESTERDAY, "ng-y", "ng", OLD_LABEL), (YESTERDAY, "za-y", "za", OLD_LABEL),
         (RUN - timedelta(days=3), "pan-3", "pan", OLD_LABEL), (RUN - timedelta(days=2), "ng-2", "ng", OLD_LABEL)],
        [("ng-y", "p1", .9), ("ng-y", "p2", .8), ("za-y", "p10", .9), ("za-y", "p11", .9), ("pan-3", "p20", .9),
         ("ng-2", "p30", .9)])
    assert sorted(recent(con, "ng")["recent_members"]) == ["p1", "p2"]
    assert sorted(recent(con, "za")["recent_members"]) == ["p10", "p11"]
    assert list(recent(con, "ke")["recent_members"] or []) == []
    assert list(recent(con, "pan")["recent_members"] or []) == []          # the pan cluster is 3 days back, not yesterday


def test_the_market_is_matched_whatever_its_case():
    con = world([(YESTERDAY, "ng-y", "NG", OLD_LABEL)], [("ng-y", "p1", .9), ("ng-y", "p2", .8)])
    assert sorted(recent(con, "ng")["recent_members"]) == ["p1", "p2"]


def test_the_cap_keeps_the_highest_probabilities_and_breaks_ties_by_post_id():
    con = world([(YESTERDAY, "ng-y", "ng", OLD_LABEL)],
                [("ng-y", "p1", .9), ("ng-y", "p2", .5), ("ng-y", "p3", .9), ("ng-y", "p4", .7)])
    assert list(recent(con, cap=2)["recent_members"]) == ["p1", "p3"]
    assert list(recent(con, cap=3)["recent_members"]) == ["p1", "p3", "p4"]


def test_a_member_in_two_clusters_of_the_day_is_listed_once_at_its_highest_probability():
    con = world([(YESTERDAY, "ng-y", "ng", OLD_LABEL), (YESTERDAY, "ng-y2", "ng", OLD_LABEL)],
                [("ng-y", "p1", .2), ("ng-y2", "p1", .9), ("ng-y", "p2", .5)])
    assert list(recent(con, cap=1)["recent_members"]) == ["p1"]


def test_the_cap_the_run_passes_leaves_room_for_the_rule():
    """With a cap of 1 the rule could never see two shared members, and would be silently off."""
    params = cluster.item_params(RUN, "ng")
    assert params == {"run_date": RUN, "market": "ng", "member_cap": cluster.RECENT_MEMBERS_CAP}
    assert cluster.RECENT_MEMBERS_CAP >= 1000


@pytest.fixture
def v2(monkeypatch):
    set_locality_authority(monkeypatch, "v2")


def test_the_rule_renames_from_members_the_statement_reads_and_not_from_other_markets(v2):
    con = world([(YESTERDAY, "ng-y", "ng", OLD_LABEL), (YESTERDAY, "za-y", "za", OLD_LABEL),
                 (RUN - timedelta(days=3), "pan-3", "pan", OLD_LABEL)],
                [("za-y", "p1", .9), ("pan-3", "p2", .9), ("ng-y", "z1", .9), ("ng-y", "z2", .9)])
    item = {**an_item("topic_a", at(0.9), OLD_WORDS, ["football"], label=OLD_LABEL),
            "recent_members": recent(con, "ng")["recent_members"]}
    c = a_cluster("c9", at(1.0), NEW_WORDS, ["football"], label=NEW_LABEL, members=["p1", "p2", "p3"])
    planned = cluster.plan([c], cluster.assign([c], [item], RUN), [item], RUN, "ng")
    # p1 is in the ZA cluster of yesterday and p2 in the pan cluster of three days ago: neither is this market's.
    assert planned["map_rows"][0]["label"] == OLD_LABEL and planned["label_changes"] == []
    assert planned["label_drift_candidates"][0]["shared_members"] == 0


def test_two_shared_members_of_the_same_market_and_day_rename_it(v2):
    con = world([(YESTERDAY, "ng-y", "ng", OLD_LABEL)], [("ng-y", "p1", .9), ("ng-y", "p2", .9), ("ng-y", "z", .1)])
    item = {**an_item("topic_a", at(0.9), OLD_WORDS, ["football"], label=OLD_LABEL),
            "recent_members": recent(con, "ng")["recent_members"]}
    c = a_cluster("c9", at(1.0), NEW_WORDS, ["football"], label=NEW_LABEL, members=["p1", "p2", "p3"])
    planned = cluster.plan([c], cluster.assign([c], [item], RUN), [item], RUN, "ng")
    assert planned["label_changes"][0]["shared_members"] == MIN_SHARED_MEMBERS == 2


def drift(recent_members, members=("p1", "p2", "p3"), **item):
    old = {**an_item("topic_a", at(0.9), OLD_WORDS, ["football"], label=OLD_LABEL, recent_members=recent_members), **item}
    c = a_cluster("c9", at(1.0), NEW_WORDS, ["football"], label=NEW_LABEL, members=list(members))
    return cluster.plan([c], cluster.assign([c], [old], RUN), [old], RUN, "ng")


def test_under_v1_a_label_never_changes_and_nothing_is_recorded(monkeypatch):
    set_locality_authority(monkeypatch, "v1")
    got = drift(["p1", "p2"])
    assert got["map_rows"][0]["label"] == OLD_LABEL and got["map_rows"][0]["aliases"] == []
    assert got["label_changes"] == [] and got["label_drift_candidates"] == []


def test_under_v2_the_same_input_renames(v2):
    got = drift(["p1", "p2"])
    assert got["map_rows"][0]["label"] == NEW_LABEL and len(got["label_changes"]) == 1


def test_an_item_renamed_by_another_market_run_today_is_not_renamed_again(v2):
    got = drift(["p1", "p2"], renamed_today=True)
    assert got["map_rows"][0]["label"] == OLD_LABEL and got["label_changes"] == []
    assert got["label_drift_candidates"] == [{"item_id": "topic_a", "cluster_id": "c9", "shared_members": 2,
                                              "renamed_today": True}]


def test_cluster_items_says_which_items_another_market_run_renamed_today():
    changes = {"label_changes": [{"item_id": "topic_a", "from": "x", "to": "y", "shared_members": 2}]}
    drifted = {"label_drift_candidates": [{"item_id": "topic_b", "cluster_id": "c", "shared_members": 0}]}
    con = world([(YESTERDAY, "ng-y", "ng", OLD_LABEL)], [("ng-y", "p1", .9)],
                runs=[("plan-ng", "ng", changes), ("plan-za", "za", drifted), ("plan-ke", "ke", {})])
    assert recent(con, "za")["renamed_today"] is True                 # the NG plan renamed topic_a
    assert recent(con, "ng")["renamed_today"] is False                # its own plan is not another run's
    assert recent(con, "ke")["renamed_today"] is True
    con = world([(YESTERDAY, "ng-y", "ng", OLD_LABEL)], [("ng-y", "p1", .9)], runs=[("plan-za", "za", drifted)])
    assert recent(con, "ng")["renamed_today"] is False                # a drift candidate is not a rename


def test_a_plan_of_another_day_does_not_block():
    con = world([(YESTERDAY, "ng-y", "ng", OLD_LABEL)], [("ng-y", "p1", .9)])
    con.execute(f"INSERT INTO {PROJECT_AGENT}.runs VALUES ('old', 'understand_cluster_plan', ?, 'ready', NULL, NULL, ?)",
                [YESTERDAY, json.dumps({"market": "za", "summary": {"label_changes": [{"item_id": "topic_a"}]}})])
    assert recent(con, "ng")["renamed_today"] is False


# What the run records

def _items_for_run(recent_members):
    from core.understand.tests.test_cluster import DAY as CDAY

    old = an_item("topic_a", at(0.9), OLD_WORDS, ["football"], label=OLD_LABEL, recent_members=recent_members)
    old["last_seen"], old["first_seen"] = CDAY - timedelta(days=1), CDAY - timedelta(days=5)
    return [old]


def _run(monkeypatch, recent_members, clusters=1):
    from core.understand.tests.test_cluster import DAY as CDAY

    items = _items_for_run(recent_members)
    cs = []
    for k in range(clusters):
        c = a_cluster(f"{CDAY:%Y%m%d}-za-00{k}", at(1.0), NEW_WORDS, ["football"], label=NEW_LABEL)
        c["market"] = "za"
        cs.append(c)
    return fake_run(monkeypatch, cs, items=items, model=NetModel())


def test_the_counts_list_the_label_changes_as_the_saved_plan_returns_them(monkeypatch, v2):
    """The counts of a run are read back from the plan's saved summary, so a list in them is a list that was saved."""
    counts, writes = _run(monkeypatch, ["p1", "p2"])
    assert counts["label_changes"] == [{"item_id": "topic_a", "from": OLD_LABEL, "to": NEW_LABEL, "shared_members": 2}]
    assert counts["label_changes_total"] == 1 and "label_drift_candidates" not in counts


def test_the_counts_list_the_drift_candidates_with_their_shared_members(monkeypatch, v2):
    counts, _ = _run(monkeypatch, ["z1"])
    [entry] = counts["label_drift_candidates"]
    assert (entry["item_id"], entry["shared_members"]) == ("topic_a", 0) and counts["label_drift_candidates_total"] == 1
    assert "label_changes" not in counts


def test_a_run_that_renames_and_drifts_nothing_keeps_its_counts_as_they_were(monkeypatch, v2):
    quiet, _ = fake_run(monkeypatch, [], items=[])
    assert not {"label_changes", "label_changes_total", "label_drift_candidates"} & set(quiet)


def test_the_lists_in_the_counts_are_capped_like_the_merge_review():
    changes = [{"item_id": f"t{i}", "from": "a", "to": "b", "shared_members": 2} for i in range(60)]
    capped = cluster.counts_lists({"label_changes": changes, "label_drift_candidates": []})
    assert len(capped["label_changes"]) == cluster.MERGE_REVIEW_LISTED == 50
    assert capped["label_changes_total"] == 60 and "label_drift_candidates" not in capped


# The measurement counts the set the rule counts.

MEASURE = (__import__("pathlib").Path(cluster.__file__).resolve().parents[1] / "detect" / "sql"
           / "label_shared_members.sql")


def measured(con):
    text = MEASURE.read_text(encoding="utf-8").replace("{core}", f"`ogilvy-trends-v2.intelligence_42_core`")
    [row] = duck_execute(con)(text, {"start": RUN, "end": RUN})
    return row


def with_today(con, members, label="new name", market="ng"):
    con.execute(f"INSERT INTO {CORE}.clusters VALUES (?, 'today', ?, 'topic_a', 'match', ?, ?, [], NULL)",
                [RUN, market, label, NEW_WORDS])
    con.executemany(f"INSERT INTO {CORE}.cluster_members VALUES ('today', ?, 0.9)", [(p,) for p in members])


@pytest.mark.parametrize(("history", "members", "shared"), [
    ([(YESTERDAY, "ng-y", "ng", "old name")], {"ng-y": ["p1", "p2"]}, 2),
    ([(YESTERDAY, "ng-y", "ng", "old name")], {"ng-y": ["p1"]}, 1),
    # the review's probe: a ZA cluster of yesterday and a pan cluster of three days back share posts with today's NG
    # cluster, the NG cluster of yesterday shares none
    ([(YESTERDAY, "ng-y", "ng", "old name"), (YESTERDAY, "za-y", "za", "old name"),
      (RUN - timedelta(days=3), "pan-3", "pan", "old name")], {"ng-y": ["z"], "za-y": ["p1"], "pan-3": ["p2"]}, 0),
])
def test_the_rule_and_the_measurement_count_the_same_shared_members(history, members, shared):
    con = world(history, [(cid, post, 0.9) for cid, posts in members.items() for post in posts])
    with_today(con, ["p1", "p2", "p3"])
    row = measured(con)
    rule = len({"p1", "p2", "p3"} & set(recent(con, "ng")["recent_members"] or []))
    assert rule == shared
    assert (row["at_1"], row["at_2"], row["at_3"]) == tuple(int(shared >= n) for n in (1, 2, 3))
    assert row["label_changes"] == 1


# The checkpoint summary keeps every renamed item id, so the same-day guard is not cut at the display cap (50).

def _fifty_one_renames(monkeypatch):
    """A run whose 51 matched clusters each rename their item; the 51st item is topic_a. Returns the counts the run
    reports and the summary it saved in the plan's ready checkpoint."""
    from core.understand.tests.test_cluster import DAY as CDAY, WRITE_PARAMS

    dim, total = 120, cluster.MERGE_REVIEW_LISTED + 1
    ids = [f"topic_{k:02d}" for k in range(total - 1)] + ["topic_a"]
    items, clusters = [], []
    for k, iid in enumerate(ids):
        vec = [0.0] * dim
        vec[k], vec[k + 60] = 0.9, math.sqrt(1 - 0.81)
        old = an_item(iid, vec, OLD_WORDS, ["football"], label=OLD_LABEL, recent_members=["p1", "p2"])
        old["last_seen"], old["first_seen"] = CDAY - timedelta(days=1), CDAY - timedelta(days=5)
        items.append(old)
        unit = [0.0] * dim
        unit[k] = 1.0
        c = a_cluster(f"{CDAY:%Y%m%d}-za-{k:03d}", unit, NEW_WORDS, ["football"], label=NEW_LABEL)
        c["market"] = "za"
        clusters.append(c)
    monkeypatch.setattr(cluster, "fit_topics", lambda docs, emb: ([0] * len(docs), [1.0] * len(docs), {}))
    monkeypatch.setattr(cluster, "build_clusters", lambda *args: clusters)
    checkpoints, persisted = [], set()

    def execute(text, params):
        if text == cluster.load("cluster_done"):
            ready = next((p for p in checkpoints if p["batch_index"] == -1), None)
            if ready is None:
                return [{"n": 0}]
            return [{"n": len(persisted), "written_ids": sorted(persisted), "checkpoint": {
                "batch_count": ready["batch_count"], "summary": json.loads(ready["summary"])},
                "batch_index": p["batch_index"], "batch": {k: json.loads(p[k]) for k in WRITE_PARAMS}}
                for p in checkpoints if p["batch_index"] >= 0]
        if text == cluster.load("cluster_checkpoint"):
            checkpoints.append(dict(params))
            return []
        if text == cluster.load("cluster_posts"):
            return [{"post_id": f"p{i}", "text": "", "today": True, "embedding": [1.0, 0.0]}
                    for i in range(cluster.MIN_POSTS)]
        if text == cluster.load("cluster_items"):
            return [dict(i) for i in items]
        assert text == cluster.load("cluster_write")
        persisted.update(r["cluster_id"] for r in json.loads(params["cluster_rows"]))
        rows = {k: json.loads(params[k]) for k in WRITE_PARAMS}
        return [{**{k: len(v) for k, v in rows.items()}, "matched": len(rows["cluster_rows"]), "recurrences": 0,
                 "variants": 0, "new_items": 0}]

    counts = cluster.run_cluster(execute, run_date=CDAY, market="za", model=NetModel())
    [ready] = [p for p in checkpoints if p["batch_index"] == -1]
    return counts, json.loads(ready["summary"]), ids


def test_the_saved_summary_holds_every_renamed_item_while_the_counts_list_only_fifty(monkeypatch, v2):
    counts, summary, ids = _fifty_one_renames(monkeypatch)
    assert counts["label_changes_total"] == 51 and len(counts["label_changes"]) == 50
    assert summary["renamed_item_ids"] == ids and len(summary["label_changes"]) == 50
    assert "renamed_item_ids" not in counts                         # display only: the run's counts stay as they were


def test_a_run_that_renames_nothing_saves_no_id_list(monkeypatch, v2):
    counts, _ = _run(monkeypatch, ["z1"])
    assert "renamed_item_ids" not in counts


def test_a_fifty_first_rename_of_another_market_blocks_the_same_item_here(monkeypatch, v2):
    """The review's failing input: the 51st rename of a run is not in its first 50 label_changes."""
    _, summary, ids = _fifty_one_renames(monkeypatch)
    assert ids[-1] == "topic_a" and ids[-1] not in [c["item_id"] for c in summary["label_changes"]]
    con = world([(YESTERDAY, "ng-y", "ng", OLD_LABEL)], [("ng-y", "p1", .9)], runs=[("plan-za", "za", summary)])
    assert recent(con, "ng")["renamed_today"] is True
    assert recent(con, "za")["renamed_today"] is False


def test_a_plan_saved_before_the_id_list_existed_still_blocks_by_its_label_changes():
    old = {"label_changes": [{"item_id": "topic_a", "from": "x", "to": "y", "shared_members": 2}]}
    con = world([(YESTERDAY, "ng-y", "ng", OLD_LABEL)], [("ng-y", "p1", .9)], runs=[("plan-za", "za", old)])
    assert recent(con, "ng")["renamed_today"] is True
