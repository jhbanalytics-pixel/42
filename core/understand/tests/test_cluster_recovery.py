import json
from datetime import timedelta

import pytest

from core.understand import cluster
from core.understand.tests.test_cluster import CORE, DAY, NetModel, core_db, duck_execute, map_row, synthetic_clusters


@pytest.mark.parametrize("committed_before_error", [False, True])
@pytest.mark.parametrize("recurrence", [False, True])
def test_partial_day_replays_frozen_batches_without_refitting(monkeypatch, committed_before_error, recurrence):
    con = core_db()
    con.execute('SET threads = 1')
    duck, writes = duck_execute(con), []
    clusters = synthetic_clusters(3)
    if recurrence:
        for c, vector in zip(clusters, ([0.9, 0.1], [-1.0, 0.0], [0.0, -1.0])):
            c["centroid"] = vector
        con.execute(f"INSERT INTO {CORE}.cultural_map VALUES ({', '.join('?' * 17)})",
                    map_row("old", [1.0, 0.0], last_seen=DAY - timedelta(days=30), recurrences=2))
        con.execute(f"INSERT INTO {CORE}.clusters VALUES (?, 'history', 'za', 'old', 'new', 'old', "
                    "['kota'], [], [1.0, 0.0])", [DAY - timedelta(days=30)])
    split = cluster.write_batches
    monkeypatch.setattr(cluster, "write_batches", lambda planned: split(planned, limit=1))
    monkeypatch.setattr(cluster, "fit_topics", lambda docs, emb: ([0] * len(docs), [1.0] * len(docs), {}))
    monkeypatch.setattr(cluster, "build_clusters", lambda *args: clusters)
    monkeypatch.setattr(cluster, "spend_today", lambda *args: (0.0, 20.0))
    monkeypatch.setattr(cluster, "book_spend", lambda execute, **kw: kw["usd"])

    def execute(text, params):
        if text == cluster.load("cluster_posts"):
            return [{"post_id": f"p{i}", "text": "", "today": True, "embedding": [1.0, 0.0]}
                    for i in range(cluster.MIN_POSTS)]
        if text == cluster.load("cluster_write"):
            writes.append(params)
            if len(writes) == 2:
                if committed_before_error:
                    duck(text, params)
                raise RuntimeError("write acknowledgement lost")
        return duck(text, params)

    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        cluster.run_cluster(execute, run_date=DAY, market="za", model=NetModel())
    first_write = json.loads(writes[0]["cluster_rows"])[0]

    def forbidden(*args, **kwargs):
        raise AssertionError("recovery must use the saved plan")

    monkeypatch.setattr(cluster, "fit_topics", forbidden)
    monkeypatch.setattr(cluster, "build_clusters", forbidden)
    monkeypatch.setattr(cluster, "label_net", forbidden)
    monkeypatch.setattr(cluster, "assign", forbidden)
    counts = cluster.run_cluster(execute, run_date=DAY, market="za")
    assert counts.get("skipped") is None
    assert counts["new"] == (1 if committed_before_error else 2)
    assert counts["members"] == (2 if committed_before_error else 4)
    assert counts.get("model_usd", 0) == counts.get("booked_usd", 0) == 0
    assert con.execute(f"SELECT cluster_id, COUNT(*) FROM {CORE}.clusters WHERE cluster_date = ? "
                       "GROUP BY ALL ORDER BY 1", [DAY]).fetchall() == [
        ("20260928-za-000", 1), ("20260928-za-001", 1), ("20260928-za-002", 1)]
    assert con.execute(f"SELECT COUNT(*), COUNT(DISTINCT (cluster_id, post_id)) FROM {CORE}.cluster_members").fetchone() == (6, 6)
    assert con.execute(f"SELECT COUNT(*) FROM {CORE}.cultural_map").fetchone() == (4 if recurrence else 3,)
    if recurrence:
        assert con.execute(f"SELECT centroid, recurrences FROM {CORE}.cultural_map "
                           "WHERE item_id = 'old' AND valid_to IS NULL").fetchone() == ([0.98, 0.02], 3)
    assert con.execute(f"SELECT label FROM {CORE}.clusters WHERE cluster_id = ?", [first_write["cluster_id"]]).fetchone() == ("kota",)
    assert cluster.run_cluster(execute, run_date=DAY, market="za")["skipped"] == "already_clustered"


@pytest.mark.parametrize("marker", ["missing", "incomplete"])
def test_a_plan_without_all_batches_cannot_write_clusters(marker):
    calls = []

    def execute(text, params):
        calls.append(text)
        if marker == "missing":
            return [{"n": 1}]
        return [{"n": 0, "checkpoint": {"batch_count": 2, "summary": {}}, "batch_index": 0,
                 "batch": {"map_rows": [], "cluster_rows": [], "member_rows": []}}]

    with pytest.raises(RuntimeError, match="recovery plan"):
        cluster.run_cluster(execute, run_date=DAY, market="za")
    assert calls == [cluster.load("cluster_done")]


def test_existing_rows_without_a_plan_are_not_a_completion_receipt():
    def execute(text, params):
        assert text == cluster.load("cluster_done")
        return [{"n": 1}]

    with pytest.raises(RuntimeError, match="no recovery plan"):
        cluster.run_cluster(execute, run_date=DAY, market="za")


def test_pending_update_holds_when_another_market_advanced_its_map_version(monkeypatch):
    con = core_db()
    con.execute("SET threads = 1")
    duck = duck_execute(con)
    za = synthetic_clusters(3)
    for c, vector in zip(za, ([-1.0, 0.0], [0.9, 0.1], [0.0, -1.0])):
        c["centroid"] = vector
    ng = synthetic_clusters(1)
    ng[0].update(cluster_id="20260928-ng-000", market="ng", centroid=[0.8, 0.2])
    monkeypatch.setattr(cluster, "fit_topics", lambda docs, emb: ([0] * len(docs), [1.0] * len(docs), {}))
    monkeypatch.setattr(cluster, "build_clusters", lambda *args: za if args[5] == "za" else ng)
    monkeypatch.setattr(cluster, "spend_today", lambda *args: (0.0, 20.0))
    monkeypatch.setattr(cluster, "book_spend", lambda execute, **kw: kw["usd"])
    split = cluster.write_batches
    monkeypatch.setattr(cluster, "write_batches", lambda planned: split(planned, limit=1))
    con.execute(f"INSERT INTO {CORE}.cultural_map VALUES ({', '.join('?' * 17)})",
                map_row("old", [1.0, 0.0], last_seen=DAY - timedelta(days=30), recurrences=2))
    con.execute(f"INSERT INTO {CORE}.clusters VALUES (?, 'history', 'za', 'old', 'new', 'old', "
                "['kota'], [], [1.0, 0.0])", [DAY - timedelta(days=30)])
    failed = False

    def execute(text, params):
        nonlocal failed
        if text == cluster.load("cluster_posts"):
            return [{"post_id": f"{params['market']}-p{i}", "text": "", "today": True, "embedding": [1.0, 0.0]}
                    for i in range(cluster.MIN_POSTS)]
        if text == cluster.load("cluster_write") and params["market"] == "za" and not failed:
            if json.loads(params["cluster_rows"])[0]["cluster_id"] == "20260928-za-001":
                failed = True
                raise RuntimeError("second ZA batch failed")
        return duck(text, params)

    with pytest.raises(RuntimeError, match="second ZA batch failed"):
        cluster.run_cluster(execute, run_date=DAY, market="za", model=NetModel())
    cluster.run_cluster(execute, run_date=DAY, market="ng", model=NetModel())
    assert con.execute(f"SELECT centroid FROM {CORE}.cultural_map WHERE item_id='old' AND valid_to IS NULL").fetchone() == ([0.96, 0.04],)
    tables = (f"{CORE}.cultural_map", f"{CORE}.clusters", f"{CORE}.cluster_members",
              '"ogilvy-trends-v2".intelligence_42_agent.runs')
    before = [con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in tables]

    def forbidden(*args, **kwargs):
        raise AssertionError("a held recovery must retain the original plan")

    for name in ("fit_topics", "build_clusters", "label_net", "assign"):
        monkeypatch.setattr(cluster, name, forbidden)
    with pytest.raises(RuntimeError, match="version advanced") as held:
        cluster.run_cluster(execute, run_date=DAY, market="za")
    assert held.value.failed_batch == 1
    assert held.value.unwritten_cluster_ids == ["20260928-za-001", "20260928-za-002"]
    assert held.value.model_usd == held.value.booked_usd == 0
    assert before == [con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in tables]
