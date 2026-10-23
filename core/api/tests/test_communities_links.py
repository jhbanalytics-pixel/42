"""Communities read a post's topic links as the market sees them (W14-01): a market run's topic joins two creators only
in that market, a link counts from its start date, and an ended link never counts. The BigQuery reader's SQL runs here
on DuckDB through the detect harness, with the dataset names the harness gives the tables."""
import datetime as dt

import pytest

from core.api import store as store_mod
from core.api.store import BigQueryStore
from core.detect.tests import duck

START, END = "2026-09-03", "2026-09-30"
SEEN = dt.date(2026, 9, 20)


class DuckStore(BigQueryStore):
    """BigQueryStore over the harness's core schema: a table is there when the harness made it."""

    def __init__(self, con, missing=()):
        super().__init__(project="fixture", client=duck.Client(con))
        self.missing = set(missing)

    def _find(self, name):
        return None if name in self.missing else f"core.{name}"


@pytest.fixture(autouse=True)
def fresh_catalog(monkeypatch):
    monkeypatch.setattr(store_mod, "_CATALOG", {})


@pytest.fixture
def con():
    c = duck.connect(views=False)
    for creator in ("ca", "cb"):
        post_id = "p_" + creator
        duck.load(c, "core.posts", [{"post_id": post_id, "platform": "tiktok", "creator_id": creator,
                                     "post_date": SEEN}])
        duck.load(c, "core.post_observations", [
            {"post_id": post_id, "observed_at": dt.datetime(2026, 9, 20, 12, tzinfo=dt.timezone.utc),
             "observed_date": SEEN, "market": market, "lane": "sweep", "lane_class": "unbiased_rank"}
            for market in ("NG", "ZA")])
        duck.load(c, "core.post_items", [
            {"post_id": post_id, "item_id": "h1", "via": "hashtag"},
            {"post_id": post_id, "item_id": "h2", "via": "hashtag"}])
    yield c
    c.close()


def links(con, item_id, via="cluster", linked_on=SEEN, link_market=None):
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": item_id, "via": via, "linked_on": linked_on,
                                        "link_market": link_market} for p in ("p_ca", "p_cb")])


def edges(con, market="NG", missing=()):
    return [(e["creator_a"], e["creator_b"], list(e["items"]))
            for e in DuckStore(con, missing).community_edges(market, START, END, [])]


def test_another_markets_topic_never_joins_two_creators(con):
    links(con, "t_za", link_market="ZA")
    assert edges(con, "NG") == []
    assert edges(con, "ZA") == [("ca", "cb", ["h1", "h2", "t_za"])]


@pytest.mark.parametrize("item_id, kw", [("t_ng", {"link_market": "NG"}), ("t_pan", {"via": "cluster_pan"}),
                                         ("t_old", {"linked_on": None})])
def test_the_markets_own_topic_a_pooled_topic_and_a_legacy_link_still_join_them(con, item_id, kw):
    links(con, item_id, **kw)
    assert edges(con, "NG") == [("ca", "cb", sorted(["h1", "h2", item_id]))]


def test_a_link_counts_from_its_start_date_and_until_its_end(con):
    links(con, "t_late", link_market="NG", linked_on=dt.date(2026, 10, 2))
    assert edges(con, "NG") == []
    links(con, "t_ended", link_market="NG")
    duck.load(con, "core.post_item_end", [{"post_id": p, "item_id": "t_ended", "ended_on": dt.date(2026, 9, 25),
                                           "reason": "split", "lineage_id": "s1"} for p in ("p_ca", "p_cb")])
    assert edges(con, "NG") == []
    # Without the end table the link is read as never ended, as the job read it before ends existed.
    assert edges(con, "NG", missing=("post_item_end",)) == [("ca", "cb", ["h1", "h2", "t_ended"])]


def test_window_posts_carry_only_the_markets_links(con):
    links(con, "t_za", link_market="ZA")
    links(con, "t_ng", link_market="NG")
    rows = DuckStore(con).window_posts(["ca"], "NG", START, END)
    assert [sorted(r["item_ids"]) for r in rows] == [["h1", "h2", "t_ng"]]
