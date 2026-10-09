"""Gate G6 once locality_v2 is the single admission gate (C4 v3 section 11.3).

For a candidate whose item_state row says locality_basis = locality_v2.1 the gate reads the retained row through the
one reader, from the counts, and not the pack scope: not_local is held by G6, market_unconfirmed and local pass, and
the flag follows the W8-DEC-17 label. A row that cannot be read is held as a data issue, never as not_local. A
candidate on any other basis is gated by the pack scope exactly as before."""

import pytest

from core.trust.gate import gate_card
from core.trust.locality import METRIC_VERSION

CTX = {"valid_days": [True, True, True], "lane_classes": ["unbiased_rank"], "campaign_hashtags": [], "political": False,
       "corroborated_unbiased": False, "explanation_passed": True}


def lrow(known, local, unknown=0, **over):
    foreign = known - local
    row = {"metric_version": METRIC_VERSION, "schema_version": 1, "population_posts": known + unknown,
           "known_posts": known, "local_posts": local, "foreign_posts": foreign, "unknown_posts": unknown,
           "status": "market_unconfirmed" if known < 8 else ("local" if 5 * local >= 3 * known else "not_local"),
           "local_share": local / known if known else None, "breadth_creators": local}
    row.update(over)
    return {f"lrow_{k}": v for k, v in row.items()}


def card(known, local, **over):
    base = {"item_id": "x", "state": "rising", "locality_basis": "locality_v2.1", "sponsored_share": 0.0,
            # the pack fields say global and are inconsistent on purpose: on the v2 basis they decide nothing
            "market_scope": "global", "market_posts7": 1, "total_posts7": 12, "market_share7": 0.9, **lrow(known, local)}
    base.update(over)
    return base


def test_not_local_is_held_by_g6_with_the_counts_in_the_text():
    d = gate_card(card(10, 2), CTX)
    assert (d.publish, d.where, d.rule) == (False, "held_back", "G6")
    assert "2 of 10" in d.reason


def test_local_with_the_label_passes_without_a_flag():
    d = gate_card(card(20, 20), CTX)
    assert (d.publish, d.where, d.flag, d.rule) == (True, "today", None, None)


@pytest.mark.parametrize(("known", "local"), [(8, 6), (8, 5), (15, 11), (20, 14)])
def test_local_under_the_wilson_floor_passes_with_the_market_unconfirmed_flag(known, local):
    d = gate_card(card(known, local), CTX)
    assert (d.publish, d.where, d.flag) == (True, "today", "Market unconfirmed")


def test_market_unconfirmed_passes_with_the_flag():
    d = gate_card(card(3, 3), CTX)
    assert (d.publish, d.where, d.flag) == (True, "today", "Market unconfirmed")


@pytest.mark.parametrize("damage", [{"lrow_status": "local"}, {"lrow_known_posts": None}, {"lrow_metric_version": "x"},
                                    {"lrow_local_share": 0.9}])
def test_a_row_that_cannot_be_read_is_a_data_issue_and_never_not_local(damage):
    c = card(8, 0)                                            # the counts say not_local
    c.update(damage)
    d = gate_card(c, CTX)
    assert (d.publish, d.rule) == (False, "G1") and d.flag == "Data issue"


def test_a_candidate_with_no_row_columns_on_the_v2_basis_is_a_data_issue():
    c = {k: v for k, v in card(8, 5).items() if not k.startswith("lrow_")}
    assert gate_card(c, CTX).rule == "G1"


def test_g1_and_g3_still_come_first():
    assert gate_card(card(10, 2), {**CTX, "valid_days": [True, False, True]}).rule == "G1"
    assert gate_card(card(10, 2), {**CTX, "lane_classes": ["search_presence"]}).rule == "G3"


def test_later_gates_still_apply_to_a_local_candidate():
    assert gate_card(card(20, 20, sponsored_share=0.6), CTX).rule == "G5"


def test_the_v1_basis_is_gated_by_the_pack_scope_as_before():
    c = {"item_id": "x", "state": "rising", "locality_basis": "v1", "sponsored_share": 0.0, "market_scope": "global",
         "market_posts7": 1, "total_posts7": 12, "market_share7": 1 / 12, **lrow(20, 20)}
    d = gate_card(c, CTX)
    assert (d.publish, d.rule) == (False, "G6") and "Global" in d.reason
    no_basis = dict(c)
    del no_basis["locality_basis"]
    assert gate_card(no_basis, CTX).rule == "G6"
