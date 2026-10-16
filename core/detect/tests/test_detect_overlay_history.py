"""A Seasonal or Recurring day rests on the state it overrode (base_state). Peaking and Mainstream read recent growth
from that state, so a calendar-driven Rising item can still peak; before 16 October 2026 they read the shown state
only, and such an item fell to no state when it slowed."""
import pytest

from core.detect.tests.fixtures import day, rid
from core.detect.tests.test_detect_states import World, con, detect  # noqa: F401

VALUES = {8: 1, 7: 2, 6: 4, 5: 8, 4: 10, 3: 10, 2: 9, 1: 9, 0: 9}


@pytest.mark.parametrize("shown, base", [("rising", "rising"), ("seasonal", "rising"), ("recurring", "rising")])
def test_an_overlay_on_rising_still_counts_as_recent_growth(con, shown, base):
    w = World()
    w.protocol("feed_tiktok", 20)
    for i in range(20, -1, -1):
        w.counter("pk", "feed_tiktok", i, float(VALUES.get(i, 1)))
    w.t["core.item_state"].append({"metric_date": day(3), "market": "ZA", "item_id": "pk", "kind": "hashtag",
                                   "state": shown, "state_raw": shown, "base_state": base,
                                   "run_id": rid("detect", day(3))})
    w.days["detect"].add(day(3))
    w.build(con)
    con.execute("UPDATE core.series_test SET test = 'nb', significant = FALSE, mu = 9, p_mid = 0.5, q = 0.9")
    assert detect(con).get("pk", {}).get("state") == "peaking"
