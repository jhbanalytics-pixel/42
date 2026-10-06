"""The suppression list on Compare and History (SETUP.md data protection, contract.md sections 11, 12.4 and 16): a
suppressed creator's posts leave every evidence list, their handle and YouTube channel id are masked in text, and
their channel's display name is removed where it names them (their own item's label, an author field). The list is
read once per request; when it cannot be read, no evidence author is named."""
import hashlib
import json
import re

import pytest

from core.api import compare, history
from core.api import store as store_mod
from core.api.store import FixtureStore

STEP = "7107ad863306852108ebb88392f1d5e0293922af5f5e7a5a4b81d466930f646e"
CHANNEL = "UC" + "fixtureHiddenChan_0001"
CHANNEL_NAME = "Fixture Hidden Channel"
YT_HANDLE = "fixture_yt_hidden"
YT_ITEM = hashlib.sha256(f"creator|youtube:{CHANNEL.casefold()}".encode("utf-8")).hexdigest()
YT_ROW = {"item_id": YT_ITEM, "kind": "creator", "canonical_key": "youtube:" + CHANNEL.casefold(),
          "label": CHANNEL_NAME, "aliases": [CHANNEL, f"@{YT_HANDLE}"], "status": None}


class Hidden(FixtureStore):
    """The fixtures with a suppressed YouTube creator (creator id = channel id) whose creator item is in the map,
    one of whose posts backs the step finding."""

    def __init__(self, listed=True):
        super().__init__()
        self.listed, self.reads = listed, 0

    def suppressed_creators(self):
        self.reads += 1
        if self.listed is None:
            return None
        base = super().suppressed_creators()
        return (base | {CHANNEL}) if self.listed else set()

    def creators_by_id(self, ids):
        rows = super().creators_by_id(ids) or []
        return rows + [{"creator_id": CHANNEL, "platform": "youtube", "handle": f"@{YT_HANDLE}", "followers": 900000,
                        "tier": "macro", "home_market": "ZA"}] if CHANNEL in ids else rows

    def map_items(self, item_ids):
        rows = super().map_items(item_ids) or []
        return rows + [dict(YT_ROW)] if YT_ITEM in item_ids else rows

    def search_items(self, q, limit):
        rows = super().search_items(q, limit) or []
        return rows + [dict(YT_ROW)] if "hidden" in q.lower() else rows

    def posts_by_id(self, post_ids):
        rows = super().posts_by_id(post_ids)
        for p in rows:
            if p["post_id"] == "p01":
                p.update(platform="youtube", handle=CHANNEL, creator_id=CHANNEL)
            else:
                p.update(text=f"{p.get('text')} with @{YT_HANDLE}, youtube.com/channel/{CHANNEL}")
        return rows


@pytest.fixture(autouse=True)
def clean():
    store_mod.SUPPRESSIONS.clear()
    yield
    store_mod.SUPPRESSIONS.clear()


def named(obj, *words):
    text = json.dumps(obj)
    return [w for w in words if re.search(rf"(?i)(?<![\w]){re.escape(w)}(?![\w])", text)]


def test_compare_masks_a_suppressed_channel_and_reads_the_list_once():
    store = Hidden()
    body = compare.build_compare(store, "items", items=f"{YT_ITEM},{STEP}", market="ZA")
    assert named(body, CHANNEL_NAME, CHANNEL, YT_HANDLE) == []
    assert {s["item_id"]: s["label"] for s in body["subjects"]}[YT_ITEM] == "YouTube channel"
    assert store.reads == 1


def test_history_item_and_search_mask_a_suppressed_channel():
    store = Hidden()
    item = history.build_history_item(store, YT_ITEM, "ZA")
    assert named(item, CHANNEL_NAME, CHANNEL, YT_HANDLE) == [] and item["label"] == "YouTube channel"
    found = history.build_history_search(store, "hidden", None)
    assert named(found, CHANNEL_NAME, CHANNEL, YT_HANDLE) == []
    assert [i["label"] for i in found["items"] if i["item_id"] == YT_ITEM] == ["YouTube channel"]


def test_history_findings_leave_out_a_suppressed_creators_posts():
    store = Hidden()
    f = history.build_history_findings(store, item_id=STEP)["findings"][0]
    assert [e["id"] for e in f["evidence"]] == ["p02"]
    assert named(f, CHANNEL, YT_HANDLE) == [] and store.reads == 1


def test_history_findings_name_no_author_when_the_list_cannot_be_read():
    f = history.build_history_findings(Hidden(listed=None), item_id=STEP)["findings"][0]
    assert f["evidence"] and all("handle" not in e and "url" not in e for e in f["evidence"])


def test_an_empty_list_changes_nothing():
    plain = FixtureStore()
    empty = FixtureStore()
    empty.suppressed_creators = lambda: set()
    her = "afe2bf2632b65cd9b5354f5bd4272ea81a4f7e004d01b3615fa9b479990a5156"
    for build in (lambda s: compare.build_compare(s, "items", items=f"{STEP},{her}", market="ZA"),
                  lambda s: history.build_history_item(s, STEP, "ZA"),
                  lambda s: history.build_history_search(s, "fixture_za", None),
                  lambda s: history.build_history_findings(s)):
        assert json.dumps(build(empty)) == json.dumps(build(plain))
