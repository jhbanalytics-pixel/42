import pytest

from core.detect.tests import duck
from core.detect.tests.fixtures import D, day, obs, post
from core.detect.tests.test_detect_states import World, detect, fresh, sql_statements


@pytest.mark.parametrize("lane_class,lane", [("panel", "panel"), ("unbiased_rank", "sweep")])
def test_recollected_old_posts_do_not_become_fresh_measured_evidence(lane_class, lane):
    con = duck.connect()
    observations, posts, links = [], [], []
    for n in range(5):
        pid = f"old-{n}"
        posts.append(post(pid, f"creator-{n}", day(60)))
        observations.extend([obs(pid, day(60), lane_class, lane), obs(pid, D, lane_class, lane)])
        links.append({"post_id": pid, "item_id": "repeat"})
    duck.load(con, "core.posts", posts)
    duck.load(con, "core.post_observations", observations)
    duck.load(con, "core.post_items", links)
    rows = duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d)", {"d": D})
    assert rows == []
    con.close()


def test_first_sighting_is_market_and_lane_group_specific_and_excludes_future_reads():
    con = duck.connect()
    duck.load(con, "core.posts", [post("shared", "one", day(60)), post("new", "two", D)])
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": "item"} for p in ("shared", "new")])
    duck.load(con, "core.post_observations", [
        obs("shared", day(60), "panel", "panel", market="ZA"),
        obs("shared", D, "panel", "panel", market="KE"),
        obs("shared", D, "search_presence", "expansion", market="ZA"),
        obs("new", D, "panel", "panel", market="ZA"),
        obs("new", day(-1), "panel", "panel", market="KE"),
    ])
    rows = {r["market"]: r for r in duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d)", {"d": D})}
    assert rows["ZA"]["posts3"] == rows["ZA"]["creators3"] == 1
    assert rows["ZA"]["posts7"] == 2
    assert rows["KE"]["posts3"] == rows["KE"]["creators3"] == 1
    assert rows["KE"]["posts7"] == 1
    con.close()


def test_recollected_posts_cannot_requalify_a_fresh_state_but_new_posts_can():
    con = duck.connect()
    for stmt in sql_statements("waves.sql"):
        con.execute(duck.create_statement(stmt))
    world = World()
    fresh(world, "repeat", first=0)
    fresh(world, "actual", first=0)
    old = [r for r in world.t["core.post_observations"] if r["post_id"].startswith("repeat-")]
    world.t["core.post_observations"].extend([{**r, "observed_date": day(60)} for r in old])
    world.build(con)
    rows = detect(con)
    assert "repeat" not in rows
    assert rows["actual"]["state"] == "new_to_42"
    assert rows["actual"]["eligible"] is True
    con.close()
