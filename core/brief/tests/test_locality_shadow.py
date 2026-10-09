"""locality_v2 in shadow (C4 v3 section 10, test L-13): until Albert's decision the brief reads the retained row, stores
it beside the v1 fields and controls nothing.

Invariance: the golden-path world run with locality rows and without them gives the same payload once the additive
keys are removed, and a read that fails (a missing view, a refused query) changes nothing but the omitted block."""

import copy
import json
from datetime import datetime, timezone

import pytest

from core.brief import job, payload
from core.brief.tests.test_brief_golden_path import ITEM, HonestModel, brief, world
from core.brief.tests.test_brief_job import Client
from core.detect.tests import duck
from core.detect.tests.fixtures import D, rid
from core.trust.locality import METRIC_VERSION

T = datetime(2026, 9, 20, 3, tzinfo=timezone.utc)


def add_locality(con, known=20, local=20, *, verified=True, market="ZA", item=ITEM, **over):
    row = {"run_date": D, "market": market, "item_id": item, "detect_run_id": rid("detect", D), "population_cutoff": T,
           "metric_version": METRIC_VERSION, "schema_version": 1, "computed_at": T, "population_posts": known,
           "known_posts": known, "local_posts": local, "foreign_posts": known - local, "unknown_posts": 0,
           "feed_only_posts": 0, "vetoed_feed_posts": 0, "local_creators": local, "known_creators": known,
           "feed_only_creators": 0, "breadth_creators": local,
           "status": "market_unconfirmed" if known < 8 else ("local" if 5 * local >= 3 * known else "not_local"),
           "local_share": local / known if known else None, "population_digest": "d" * 64, **over}
    duck.load(con, "core.item_locality", [row])
    if verified:
        duck.load(con, "core.item_locality_verified", [{
            "run_date": D, "market": market, "item_id": item, "detect_run_id": row["detect_run_id"],
            "metric_version": METRIC_VERSION, "verified_at": T, "member_rows": known, "population_digest": "d" * 64}])


def strip(value):
    """The additive keys of shadow removed, to compare a payload with and without rows."""
    value = copy.deepcopy(value)
    if isinstance(value, dict):
        value.pop("locality_v2", None)
        value.pop("locality_shadow", None)
        return {k: strip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


@pytest.fixture
def baseline(monkeypatch):
    return brief(world(located=True), HonestModel(True), monkeypatch)


def test_with_rows_and_without_the_payload_is_the_same_once_the_additive_keys_are_removed(baseline, monkeypatch):
    counts0, payload0, checks0 = baseline
    con = world(located=True)
    add_locality(con)
    counts1, payload1, checks1 = brief(con, HonestModel(True), monkeypatch)
    assert strip(payload1) == strip(payload0) and strip(counts1) == strip(counts0)
    assert [c["verdict"] for c in checks1] == [c["verdict"] for c in checks0]


def test_without_rows_nothing_is_added_to_the_payload_or_the_counts(baseline):
    counts0, payload0, _ = baseline
    assert "locality_v2" not in json.dumps(payload0) and "locality_shadow" not in counts0


def test_a_card_carries_the_versioned_block_with_the_label(monkeypatch):
    con = world(located=True)
    add_locality(con, 20, 20)
    counts, got, _ = brief(con, HonestModel(True), monkeypatch)
    [card] = got["cards"]
    block = card["locality_v2"]
    assert (block["block_version"], block["metric_version"], block["schema_version"]) == (1, METRIC_VERSION, 1)
    assert (block["status"], block["label"], block["known_posts"], block["local_posts"]) == ("local", "local", 20, 20)
    assert card["market_scope"] == "market" and card["flag"] is None            # the v1 fields still decide the card
    assert counts["locality_shadow"] == [{"market": "ZA", "item_id": ITEM, "pack_scope": "market",
                                          "v2_status": "local", "v2_label": "local"}]


def test_a_local_row_under_the_wilson_label_floor_is_stored_as_market_unconfirmed_and_changes_no_flag(monkeypatch):
    con = world(located=True)
    add_locality(con, 8, 6)                                  # 0.75 of 8: status local, lower bound 0.409
    counts, got, _ = brief(con, HonestModel(True), monkeypatch)
    [card] = got["cards"]
    assert (card["locality_v2"]["status"], card["locality_v2"]["label"]) == ("local", "market_unconfirmed")
    assert card["flag"] is None                              # shadow: v1 still decides the flag


def test_a_row_that_contradicts_itself_is_stored_as_unreadable_with_no_counts(monkeypatch):
    con = world(located=True)
    add_locality(con, 8, 0, status="local")                  # claims local, the counts say not_local
    _, got, _ = brief(con, HonestModel(True), monkeypatch)
    block = got["cards"][0]["locality_v2"]
    assert (block["status"], block["reason"], block["label"], block["known_posts"]) == ("unreadable", "status_mismatch", None, None)


def test_a_key_without_a_verification_row_gives_no_block(baseline, monkeypatch):
    _, payload0, _ = baseline
    con = world(located=True)
    add_locality(con, verified=False)
    _, got, _ = brief(con, HonestModel(True), monkeypatch)
    assert got == payload0


class RefusingClient(Client):
    """A client whose locality read fails: the view is missing, or the query is refused."""

    def query(self, sql, job_config=None):
        if "v_item_locality" in sql:
            raise RuntimeError("Not found: Table v_item_locality_current")
        return super().query(sql, job_config)


def test_a_failed_locality_read_changes_nothing_but_the_omitted_block(baseline, monkeypatch):
    _, payload0, _ = baseline
    con = world(located=True)
    add_locality(con)
    monkeypatch.setattr("core.brief.tests.test_brief_golden_path.Client", RefusingClient)
    _, got, _ = brief(con, HonestModel(True), monkeypatch)
    assert got == payload0


def test_a_held_item_carries_the_block_too():
    cand = {"item_id": "x", "title": "X", "kind": "hashtag", "decision": {"publish": False, "where": "held_back", "flag": None,
            "reason": "Fewer than 3 posts 42 can show", "rule": None, "numbers_only": False},
            "held_reason": "not_confirmed", "evidence": [], "numbers": [], "locality_v2": {"block_version": 1, "status": "local"}}
    assert payload._held_item(cand)["locality_v2"] == {"block_version": 1, "status": "local"}
    del cand["locality_v2"]
    assert "locality_v2" not in payload._held_item(cand)
