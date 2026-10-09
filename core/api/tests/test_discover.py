"""Discover, Radar and topic pages over the fixtures and a fake BigQuery (contract.md sections 10.1 to 10.3)."""
import hashlib
import json
import re

import pytest

from core.api import discover, store as store_mod
from core.api.store import BigQueryStore, FixtureStore
from core.api.today import NotFound, NotReady

D30 = "2026-09-30"
RUN = "r_detect_20260930_01"
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def iid(kind, key):
    return hashlib.sha256(f"{kind}|{key}".encode("utf-8")).hexdigest()


STEP = iid("hashtag", "#fixture_za_step")          # in the 30 September brief, explained
HERITAGE = iid("hashtag", "#fixture_za_heritage")  # recurrence with a last wave
RISING = iid("topic", "fixture za rising topic")   # check pattern, not in the brief
BOARD = iid("hashtag", "#fixture_za_board")        # untested, newest
OUTAGE = iid("hashtag", "#fixture_za_outage")      # eligible, held by the gate view (G1)
BOTNET = iid("hashtag", "#fixture_za_botnet")
IMPORT = iid("hashtag", "#fixture_za_lagos_import")
SPONSORED = iid("hashtag", "#fixture_za_sponsored")
REJECTED = iid("topic", "fixture za rejected topic")
OTHER = iid("meme", "fixture za other meme")
OWAMBE = iid("hashtag", "#fixture_ng_owambe")
PEAKING = iid("sound", "fixture ng peaking sound")
PROMO = iid("hashtag", "#fixture_ng_promo")
MAINSTREAM = iid("topic", "fixture ke mainstream topic")


@pytest.fixture
def fx():
    return FixtureStore()


class Patched(FixtureStore):
    def __init__(self, **overrides):
        super().__init__()
        for name, fn in overrides.items():
            setattr(self, name, fn)


def after_warmup(**more):
    return Patched(first_ok_collect_date=lambda: "2026-09-01", **more)


def ids(cards):
    return [c["item_id"] for c in cards]


def canon_hash(rows):
    text = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_figure(f, value=None, unit=None):
    assert set(f) == {"value", "unit", "query_id", "run_id", "result_hash"}
    assert HASH.match(f["result_hash"]) and f["query_id"].startswith("q_") and f["run_id"]
    if value is not None:
        assert f["value"] == value
    if unit is not None:
        assert f["unit"] == unit
    return True


# ---------- fixture store ----------

def test_fixture_store_v2_reads(fx):
    run = fx.latest_detect_run()
    assert run == {"run_id": RUN, "run_date": D30}
    za = fx.item_states(run, "ZA")
    assert len(za) == 10 and all(r["market"] == "ZA" and r["metric_date"] == D30 for r in za)
    assert len(fx.item_states(run, "all")) == 14
    assert {(g["item_id"], g["rule"]) for g in fx.item_gate("all", D30)} == {(OUTAGE, "G1"), (PROMO, "G5b")}
    assert [g["item_id"] for g in fx.item_gate("ZA", D30)] == [OUTAGE]
    hist = fx.item_history(HERITAGE, "ZA", "2026-07-01", D30)
    assert [h["metric_date"] for h in hist] == ["2026-09-29", D30]
    assert len(fx.item_waves(HERITAGE, "ZA")) == 2
    assert fx.item_series(HERITAGE, "ZA", 28)
    assert [e["post_id"] for e in fx.item_evidence(HERITAGE, "ZA")] == ["tt_her_001", "x_her_002"]
    assert fx.coord_signals(RISING, "ZA", D30)[0]["signal"] == "coaction"


def test_fixture_store_missing_file_reads_as_not_built(tmp_path):
    fx = FixtureStore(root=tmp_path)
    assert fx.item_gate("ZA", D30) is None
    assert fx.item_series(HERITAGE, "ZA", 28) is None
    assert fx.item_evidence(HERITAGE, "ZA") is None
    assert fx.item_waves(HERITAGE, "ZA") is None
    assert fx.coord_signals(RISING, "ZA", D30) is None
    assert fx.latest_detect_run() is None


@pytest.mark.parametrize(("scope", "expected"), [
    ("market", "market"), ("global", "global"), ("local", "global"), (None, "global")
])
def test_discover_scope_is_limited_to_contract_values_and_defaults_global(scope, expected):
    def item_states(run, market):
        return [dict(r, market_scope=scope) for r in FixtureStore().item_states(run, market)]

    out = discover.build_discover(Patched(item_states=item_states), "ZA")
    card = next(c for c in out["items"] if c["item_id"] == STEP)
    assert card["market_scope"] == expected


def test_discover_scope_uses_same_date_brief_only_as_fallback():
    item_rows = [dict(r, market_scope="global") if r["item_id"] == STEP else r
                 for r in FixtureStore().item_states({"run_id": RUN, "run_date": D30}, "ZA")]
    briefs = json.loads(json.dumps(FixtureStore().briefs(D30)))
    step_brief = next(c for b in briefs for c in b["payload"].get("cards", []) if c["item_id"] == STEP)
    step_brief["market_scope"] = "market"
    step_brief.update(market_posts7=3, total_posts7=12, market_share7=0.25)
    store = Patched(item_states=lambda run, market: item_rows,
                    briefs=lambda date: briefs if date == D30 else [])

    card = next(c for c in discover.build_discover(store, "ZA")["items"] if c["item_id"] == STEP)
    assert card["market_scope"] == "global"
    assert (card["market_posts7"], card["total_posts7"], card["market_share7"]) == (None, None, None)
    assert STEP in ids(discover.build_discover(store, "ZA")["items"])

    item_rows = [dict(r, market_scope=None) if r["item_id"] == STEP else r for r in item_rows]
    store = Patched(item_states=lambda run, market: item_rows,
                    briefs=lambda date: briefs if date == D30 else [])
    card = next(c for c in discover.build_discover(store, "ZA")["items"] if c["item_id"] == STEP)
    assert card["market_scope"] == "market"
    assert (card["market_posts7"], card["total_posts7"], card["market_share7"]) == (3, 12, 0.25)

    invalid_rows = [dict(r, market_scope="local") if r["item_id"] == STEP else r for r in item_rows]
    store = Patched(item_states=lambda run, market: invalid_rows,
                    briefs=lambda date: briefs if date == D30 else [])
    card = next(c for c in discover.build_discover(store, "ZA")["items"] if c["item_id"] == STEP)
    assert card["market_scope"] == "global"
    assert (card["market_posts7"], card["total_posts7"], card["market_share7"]) == (None, None, None)

    stale_briefs = json.loads(json.dumps(briefs))
    next(b for b in stale_briefs if b["market"] == "ZA")["brief_date"] = "2026-09-29"
    store = Patched(item_states=lambda run, market: item_rows,
                    briefs=lambda date: stale_briefs)
    card = next(c for c in discover.build_discover(store, "ZA")["items"] if c["item_id"] == STEP)
    assert card["market_scope"] == "global"
    assert (card["market_posts7"], card["total_posts7"], card["market_share7"]) == (None, None, None)

    incomplete = json.loads(json.dumps(briefs))
    incomplete_step = next(c for b in incomplete for c in b["payload"].get("cards", []) if c["item_id"] == STEP)
    incomplete_step.pop("market_share7")
    store = Patched(item_states=lambda run, market: item_rows,
                    briefs=lambda date: incomplete if date == D30 else [])
    card = next(c for c in discover.build_discover(store, "ZA")["items"] if c["item_id"] == STEP)
    assert (card["market_posts7"], card["total_posts7"], card["market_share7"]) == (3, 12, None)


@pytest.mark.parametrize(("market_posts7", "total_posts7", "market_share7", "expected"), [
    (0, 0, None, (0, 0, None)),
    (0, 0, 0, (0, 0, None)),
    (1, 3, 1 / 3, (1, 3, 1 / 3)),
    ("3", 12, 0.25, (None, 12, None)),
    (-1, 12, 0.25, (None, 12, None)),
    (3, True, 0.25, (3, None, None)),
    (13, 13, 1.0, (None, None, None)),
    (3, 2, 1.0, (None, None, None)),
    (3, 12, "0.25", (3, 12, None)),
    (3, 12, True, (3, 12, None)),
    (3, 12, 1.01, (3, 12, None)),
    (3, 12, 0.3, (3, 12, None)),
    (None, None, None, (None, None, None)),
])
def test_discover_scope_basis_keeps_unknowns_null_and_rejects_malformed_values(
        market_posts7, total_posts7, market_share7, expected):
    rows = [dict(r, market_scope="global", market_posts7=market_posts7, total_posts7=total_posts7,
                 market_share7=market_share7, local_share=0.8)
            if r["item_id"] == STEP else r
            for r in FixtureStore().item_states({"run_id": RUN, "run_date": D30}, "ZA")]
    store = Patched(item_states=lambda run, market: rows)

    card = next(c for c in discover.build_discover(store, "ZA")["items"] if c["item_id"] == STEP)

    assert (card["market_posts7"], card["total_posts7"], card["market_share7"]) == expected


# ---------- Discover ----------

def test_discover_lists_eligible_items_in_order(fx):
    out = discover.build_discover(fx, "ZA")
    assert out["date"] == D30 and out["market"] == "ZA" and out["run_id"] == RUN
    # worth_raw orders the list while the cohort is under 20 items
    assert ids(out["items"]) == [STEP, HERITAGE, RISING, BOARD]
    assert [c["order"] for c in out["items"]] == [1, 2, 3, 4]
    assert out["next_cursor"] is None


def test_discover_held_back_with_reasons_from_gate_view_and_item_state(fx):
    held = discover.build_discover(fx, "ZA")["held_back"]
    got = {h["item_id"]: (h["reason"], h["reason_text"]) for h in held["items"]}
    assert held["count"] == 6 == len(held["items"])
    assert got == {
        OUTAGE: ("data_issue", "Data issue"),
        BOTNET: ("likely_coordinated", "Likely coordinated"),
        IMPORT: ("not_local", "Not local to this market"),
        SPONSORED: ("paid_led", "Paid-led"),
        REJECTED: ("not_active", "Not tracked by 42 right now"),
        OTHER: ("held_by_gate", "Held back by 42's checks"),
    }
    for h in held["items"]:
        assert h["card"]["item_id"] == h["item_id"] and h["title"] == h["card"]["title"]
        assert h["card"]["order"] is None
    rules = {h["item_id"]: h["card"]["held_back"]["rule"] for h in held["items"]}
    assert rules[OUTAGE] == "G1" and rules[BOTNET] == "G4" and rules[IMPORT] == "G6" and rules[SPONSORED] == "G5"
    assert rules[REJECTED] is None and rules[OTHER] is None


def test_discover_without_gate_view_derives_reasons_and_keeps_g1_items(fx):
    store = Patched(item_gate=lambda market, run_date: None)
    out = discover.build_discover(store, "ZA")
    assert OUTAGE in ids(out["items"])
    assert OUTAGE not in {h["item_id"] for h in out["held_back"]["items"]}
    assert out["held_back"]["count"] == 5
    ng = discover.build_discover(store, "NG")
    assert PROMO in ids(ng["items"])  # G5b comes only through L2's view


def test_ng_campaign_hashtag_held_through_gate_view(fx):
    ng = discover.build_discover(fx, "NG")
    (held,) = ng["held_back"]["items"]
    assert held["item_id"] == PROMO and held["reason"] == "paid_led" and held["card"]["held_back"]["rule"] == "G5b"


def test_card_from_the_brief_keeps_its_explanation(fx):
    card = discover.build_discover(fx, "ZA")["items"][0]
    assert card["item_id"] == STEP
    assert card["explanation_status"] == "explained" and card["explained"] is True
    assert card["explanation"] and card["claims"] and card["evidence"]
    assert card["rank"] == 1 and card["sparkline"]["points"]
    assert card["state"] == "emerging" and card["state_word"] == "Emerging"
    assert card["lifecycle"] == {"step": 1, "word": "Emerging", "rule": discover.LIFECYCLE["emerging"][2]}
    assert card["novelty"] == "new" and card["last_wave"] is None and card["diffusion"] == "bottom_up"
    assert card["origin"] is None and card["spread_line"] is None and card["watch_id"] is None
    assert card["market"] == "ZA" and card["date"] == D30


def test_card_not_in_the_brief_is_not_run(fx):
    card = next(c for c in discover.build_discover(fx, "ZA")["items"] if c["item_id"] == RISING)
    assert card["explanation_status"] == "not_run" and card["explained"] is False
    assert card["explanation"] is None and card["claims"] == [] and card["explanation_claim_ids"] == []
    assert card["evidence"] == [] and card["thumbnails"] == [] and card["rank"] is None
    assert card["flag"] == "check_pattern" and card["flag_word"] == "Unusual posting pattern"
    assert card["lifecycle"]["step"] == 2 and card["lifecycle"]["word"] == "Rising"
    assert card["title"] == "fixture za rising topic" and card["kind"] == "topic"
    assert card["count_line"] == "18 creators, 3 days"
    assert card["ask"] == "What is behind fixture za rising topic in South Africa this week?"


@pytest.mark.parametrize(("field", "post_id", "platform", "platform_word"), [
    ("label", "71011", "tiktok", "TikTok"),
    ("canonical_key", "71012", "twitter", "X"),
    ("brief_title", "71013", "instagram", "Instagram"),
])
def test_held_numeric_titles_match_their_own_evidence_post(field, post_id, platform, platform_word, fx):
    real_states, real_briefs, real_gate = fx.item_states, fx.briefs, fx.item_gate

    def item_states(run, market):
        rows = real_states(run, market)
        for row in rows:
            if row["item_id"] == STEP:
                row["label"] = post_id if field == "label" else None
                row["canonical_key"] = post_id if field == "canonical_key" else None
                row["platforms"] = [platform]
        return rows

    def briefs(date):
        rows = json.loads(json.dumps(real_briefs(date)))
        for brief in rows:
            for card in (brief["payload"].get("cards") or []) + (brief["payload"].get("more") or []):
                if card["item_id"] == STEP:
                    card["title"] = post_id if field == "brief_title" else "71999"
                    card["evidence"] = [
                        {"id": "71099", "platform": "tiktok", "handle": "unrelated"},
                        {"id": post_id, "platform": platform, "handle": f"matched_{field}"},
                    ]
                    card["evidence_ids"] = [e["id"] for e in card["evidence"]]
        return rows

    def item_gate(market, run_date):
        rows = real_gate(market, run_date) or []
        if market == "ZA":
            rows.append({"item_id": STEP, "market": "ZA", "rule": "G1", "reason": "data_issue"})
        return rows

    store = Patched(item_states=item_states, briefs=briefs, item_gate=item_gate)
    held = next(h for h in discover.build_discover(store, "ZA")["held_back"]["items"] if h["item_id"] == STEP)
    assert held["title"] == f"{platform_word} post by @matched_{field}"
    assert held["card"]["ask"] == f"What is behind {held['title']} in South Africa this week?"
    assert held["item_id"] == STEP and held["card"]["evidence_ids"] == ["71099", post_id]


def test_held_numeric_title_without_matching_post_id_uses_platform_only(fx):
    post_id = "71014"
    real_states, real_briefs, real_gate = fx.item_states, fx.briefs, fx.item_gate

    def item_states(run, market):
        rows = real_states(run, market)
        for row in rows:
            if row["item_id"] == STEP:
                row.update(label=post_id, canonical_key=None, platforms=["tiktok"])
        return rows

    def briefs(date):
        rows = json.loads(json.dumps(real_briefs(date)))
        for brief in rows:
            for card in (brief["payload"].get("cards") or []) + (brief["payload"].get("more") or []):
                if card["item_id"] == STEP:
                    card["title"] = post_id
                    card["evidence"] = [{"id": "71015", "platform": "tiktok", "handle": "unrelated"}]
                    card["evidence_ids"] = [e["id"] for e in card["evidence"]]
        return rows

    def item_gate(market, run_date):
        rows = real_gate(market, run_date) or []
        if market == "ZA":
            rows.append({"item_id": STEP, "market": "ZA", "rule": "G1", "reason": "data_issue"})
        return rows

    held = next(h for h in discover.build_discover(
        Patched(item_states=item_states, briefs=briefs, item_gate=item_gate), "ZA")["held_back"]["items"]
                if h["item_id"] == STEP)
    assert held["title"] == "TikTok post"
    assert "unrelated" not in held["title"] and held["title"] != post_id


def test_count_line_uses_singular_creator_for_one(fx):
    real_states = fx.item_states

    def item_states(run, market):
        rows = real_states(run, market)
        for row in rows:
            if row["item_id"] == RISING:
                row["creators3"] = 1
        return rows

    rising = next(c for c in discover.build_discover(Patched(item_states=item_states), "ZA")["items"]
                  if c["item_id"] == RISING)
    assert rising["count_line"] == "1 creator, 3 days"
    assert is_figure(rising["reach"], 1, "creators in 3 days")


def test_brief_card_count_line_uses_singular_creator_and_preserves_rest(fx):
    real_briefs = fx.briefs

    def briefs(date):
        rows = json.loads(json.dumps(real_briefs(date)))
        for brief in rows:
            for card in (brief["payload"].get("cards") or []) + (brief["payload"].get("more") or []):
                if card["item_id"] == STEP:
                    card["count_line"] = "1 creators, 3 days, 2.2 times usual"
        return rows

    card = next(c for c in discover.build_discover(Patched(briefs=briefs), "ZA")["items"]
                if c["item_id"] == STEP)
    assert card["count_line"] == "1 creator, 3 days, 2.2 times usual"


def test_brief_card_count_line_keeps_eleven_creators_plural(fx):
    real_briefs = fx.briefs

    def briefs(date):
        rows = json.loads(json.dumps(real_briefs(date)))
        for brief in rows:
            for card in (brief["payload"].get("cards") or []) + (brief["payload"].get("more") or []):
                if card["item_id"] == STEP:
                    card["count_line"] = "11 creators, 3 days, 2.2 times usual"
        return rows

    card = next(c for c in discover.build_discover(Patched(briefs=briefs), "ZA")["items"]
                if c["item_id"] == STEP)
    assert card["count_line"] == "11 creators, 3 days, 2.2 times usual"


def test_reach_is_a_figure_from_creators3_and_its_hash_is_reproducible(fx):
    card = discover.build_discover(fx, "ZA")["items"][0]
    reach = card["reach"]
    assert is_figure(reach, 31, "creators in 3 days")
    assert reach["query_id"] == "q_discover_items" and reach["run_id"] == RUN
    row = next(r for r in fx.item_states(fx.latest_detect_run(), "ZA") if r["item_id"] == STEP)
    narrow = [{"item_id": STEP, "market": "ZA", "metric_date": D30, "run_id": RUN, "creators3": row["creators3"]}]
    assert reach["result_hash"] == canon_hash(narrow)
    rising = next(c for c in discover.build_discover(fx, "ZA")["items"] if c["item_id"] == RISING)
    assert rising["reach"] in rising["numbers"]  # a card outside the brief shows its reach as a number


def test_growth_is_null_in_warmup_and_a_figure_after(fx):
    assert all(c["growth"] is None for c in discover.build_discover(fx, "ZA")["items"])
    cards = {c["item_id"]: c for c in discover.build_discover(after_warmup(), "ZA")["items"]}
    assert is_figure(cards[STEP]["growth"], 2.8, "times usual")
    assert cards[BOARD]["growth"] is None  # untested: no main ratio
    assert cards[RISING]["count_line"] == "18 creators, 3 days, 2.2 times usual"
    assert cards[RISING]["growth"] in cards[RISING]["numbers"]


def test_recurrence_carries_its_last_wave(fx):
    card = next(c for c in discover.build_discover(fx, "ZA")["items"] if c["item_id"] == HERITAGE)
    assert card["novelty"] == "recurrence"
    assert card["last_wave"]["peak_date"] == "2025-09-24"
    assert is_figure(card["last_wave"]["peak_posts"], 240, "posts on the peak day")
    assert card["flag"] == "not_assessed" and card["flag_word"] == "Not assessed (thin sample)"


def test_flags_one_per_card_and_paid_led_word(fx):
    out = discover.build_discover(fx, "ZA")
    board = next(c for c in out["items"] if c["item_id"] == BOARD)
    assert board["flag"] == "market_unconfirmed" and board["flag_word"] == "Market unconfirmed"
    step = out["items"][0]
    assert step["flag"] is None and step["flag_word"] is None
    sponsored = next(h["card"] for h in out["held_back"]["items"] if h["item_id"] == SPONSORED)
    assert sponsored["flag"] == "paid_led" and sponsored["flag_word"] == "Paid-led"


def test_explanation_status_from_the_brief_card_wins(fx):
    real_briefs = fx.briefs

    def briefs(date):
        rows = json.loads(json.dumps(real_briefs(date)))
        for r in rows:
            for c in r["payload"].get("cards") or []:
                if c["item_id"] == STEP:
                    c["explanation_status"] = "failed_checks"
        return rows

    card = discover.build_discover(Patched(briefs=briefs), "ZA")["items"][0]
    assert card["explanation_status"] == "failed_checks" and card["explained"] is False
    assert card["explanation"] is None and card["claims"] == []
    assert card["evidence"]  # numbers and posts still show


def test_worth_pct_orders_when_the_cohort_has_it(fx):
    real = fx.item_states

    def item_states(run, market):
        rows = real(run, market)
        pct = {STEP: 0.1, HERITAGE: 0.2, RISING: 0.9, BOARD: 0.5, OUTAGE: 0.3}
        for r in rows:
            if r["item_id"] in pct:
                r["worth_pct"] = pct[r["item_id"]]
        return rows

    out = discover.build_discover(Patched(item_states=item_states), "ZA")
    assert ids(out["items"]) == [RISING, BOARD, HERITAGE, STEP]


def test_ties_on_worth_break_on_creators3(fx):
    real = fx.item_states

    def item_states(run, market):
        rows = real(run, market)
        for r in rows:
            r["worth_raw"] = 0.5
        return rows

    out = discover.build_discover(Patched(item_states=item_states), "ZA")
    assert ids(out["items"]) == [STEP, HERITAGE, RISING, BOARD]  # 31, 22, 18, 4 creators


@pytest.mark.parametrize("sort,expected", [
    ("velocity", [RISING, HERITAGE, STEP, BOARD]),   # 0.9, 0.55, 0.42, none last
    ("reach", [STEP, HERITAGE, RISING, BOARD]),      # 31, 22, 18, 4
    ("new", [BOARD, STEP, RISING, HERITAGE]),        # first_seen newest first
    ("order", [STEP, HERITAGE, RISING, BOARD]),
])
def test_discover_sorts(fx, sort, expected):
    assert ids(discover.build_discover(fx, "ZA", sort=sort)["items"]) == expected


def test_discover_filters_and_filter_values(fx):
    out = discover.build_discover(fx, "ZA", kind="hashtag")
    assert ids(out["items"]) == [STEP, HERITAGE, BOARD]
    assert all(h["card"]["kind"] == "hashtag" for h in out["held_back"]["items"])
    assert ids(discover.build_discover(fx, "ZA", state="rising")["items"]) == [RISING]
    assert ids(discover.build_discover(fx, "ZA", platform="x")["items"]) == [RISING]
    assert discover.build_discover(fx, "ZA", platform="x")["held_back"]["count"] == 1  # the botnet
    f = discover.build_discover(fx, "ZA", kind="hashtag")["filters"]
    assert f["kinds"] == ["hashtag", "meme", "topic"]
    assert f["states"] == sorted(f["states"]) and "on_the_boards" in f["states"]
    assert f["platforms"] == ["instagram", "tiktok", "x"]


def test_discover_pages_with_a_cursor(fx):
    p1 = discover.build_discover(fx, "ZA", limit=3)
    assert ids(p1["items"]) == [STEP, HERITAGE, RISING] and p1["next_cursor"] == "3"
    p2 = discover.build_discover(fx, "ZA", limit=3, cursor=p1["next_cursor"])
    assert ids(p2["items"]) == [BOARD] and p2["next_cursor"] is None
    assert p2["held_back"]["count"] == 6  # held back is listed whole on every page


@pytest.mark.parametrize("kwargs", [{"sort": "hot"}, {"limit": 0}, {"limit": 201}, {"cursor": "x"},
                                    {"cursor": "-1"}])
def test_discover_bad_arguments(fx, kwargs):
    with pytest.raises(discover.BadRequest):
        discover.build_discover(fx, "ZA", **kwargs)


def test_discover_all_markets(fx):
    out = discover.build_discover(fx, "all")
    assert out["market"] == "all"
    assert {c["market"] for c in out["items"]} == {"ZA", "NG", "KE"}
    firsts = [c for c in out["items"] if c["order"] == 1]
    assert [c["market"] for c in firsts] == ["ZA", "NG", "KE"]
    assert out["items"][:3] == firsts
    assert out["held_back"]["count"] == 7


def test_not_ready_without_a_detect_run():
    store = Patched(latest_detect_run=lambda: None)
    for call in (lambda: discover.build_discover(store, "ZA"), lambda: discover.build_radar(store, "ZA"),
                 lambda: discover.build_topic(store, STEP, "ZA")):
        with pytest.raises(NotReady):
            call()


def test_no_brief_yet_means_every_card_is_not_run(fx):
    out = discover.build_discover(Patched(briefs=lambda date: []), "ZA")
    assert {c["explanation_status"] for c in out["items"]} == {"not_run"}


# ---------- Radar ----------

def test_radar_in_warmup(fx):
    out = discover.build_radar(fx, "ZA")
    assert out["date"] == D30 and out["market"] == "ZA"
    assert [p["item_id"] for p in out["points"]] == [STEP, HERITAGE, RISING, BOARD]
    assert out["held_back_count"] == 6
    assert out["note"] == "Growth needs 14 days of data; showing reach only"
    for p in out["points"]:
        assert set(p) == {"item_id", "label", "kind", "state", "flag", "growth", "reach", "measures"}
        assert p["growth"] is None and is_figure(p["reach"], unit="creators in 3 days")
    assert out["points"][0]["label"] == "#fixture_za_step"


def test_radar_after_warmup_and_kind_filter(fx):
    out = discover.build_radar(after_warmup(), "ZA", kind="hashtag")
    assert out["note"] is None
    growth = {p["item_id"]: p["growth"] for p in out["points"]}
    assert set(growth) == {STEP, HERITAGE, BOARD}
    assert is_figure(growth[STEP], 2.8) and growth[BOARD] is None
    assert out["held_back_count"] == 4  # outage, botnet, import, sponsored


def test_radar_measures_count_every_lane_over_7_days(fx):
    # The fixtures' 3-day measured-lane count is not the only number: posts, creators and platforms over 7 days
    # count every collection lane, each post once and dated by its first sighting in the market, from
    # post_observations, post_items and posts. p03, first seen on 10 September and seen again on the 30th, is out.
    points = {p["item_id"]: p for p in discover.build_radar(fx, "ZA")["points"]}
    step = points[STEP]["measures"]
    assert is_figure(step["posts7"], 5, "posts in 7 days") and step["posts7"]["query_id"] == "q_item_reach"
    assert is_figure(step["creators7"], 3, "creators in 7 days")
    assert is_figure(step["measured_posts7"], 4) and is_figure(step["measured_creators7"], 3)
    assert is_figure(step["located7"], 0) and is_figure(step["own_feed7"], 0)
    assert step["views7"] is None and is_figure(step["views_posts7"], 0)  # the fixture posts carry no views
    assert step["local_share7"] is None  # nothing located: unknown, never 0%
    assert step["platforms"] == ["instagram", "tiktok", "x"]
    # The read ran and found no post for the board item: its counts are a measured 0, views stay unknown.
    board = points[BOARD]["measures"]
    assert is_figure(board["posts7"], 0) and board["views7"] is None and board["platforms"] == []


def test_radar_measures_none_without_the_reach_read():
    out = discover.build_radar(Patched(item_reach=lambda date, market, item_ids: None), "ZA")
    assert out["points"] and all(p["measures"] is None for p in out["points"])
    assert all(p["measures"] is None for p in discover.build_radar(FixtureStore(), "all")["points"])


def test_radar_measures_read_the_market_and_run_date():
    seen = []
    store = Patched(item_reach=lambda date, market, item_ids: seen.append((date, market, sorted(item_ids))) or [
        {"item_id": STEP, "posts7": 40, "creators7": 21, "views7": 120000, "engagement7": 900, "platforms": ["tiktok"],
         "located7": 12, "own_feed7": 5}])
    (step,) = [p for p in discover.build_radar(store, "ZA")["points"] if p["item_id"] == STEP]
    assert seen == [(D30, "ZA", sorted([STEP, HERITAGE, RISING, BOARD]))]
    assert step["measures"]["views7"]["value"] == 120000 and step["measures"]["located7"]["value"] == 12
    assert step["measures"]["local7"] is None and step["measures"]["local_share7"] is None  # no local7: no share
    assert step["reach"]["unit"] == "creators in 3 days"  # the gate's own count stays as it was


def test_radar_local_share_is_local_over_located_posts():
    store = Patched(item_reach=lambda date, market, item_ids: [
        {"item_id": STEP, "posts7": 90, "located7": 48, "local7": 30, "own_feed7": 4},
        {"item_id": HERITAGE, "posts7": 9, "located7": 0, "local7": 0, "own_feed7": 0}])
    points = {p["item_id"]: p["measures"] for p in discover.build_radar(store, "ZA")["points"]}
    assert is_figure(points[STEP]["local_share7"], 0.625, "share of located posts local to the market")
    assert points[HERITAGE]["local_share7"] is None  # nothing located: unknown, never 0%


def test_radar_window_names_the_days_and_each_platforms_valid_days(fx):
    window = discover.build_radar(fx, "ZA")["window"]
    assert (window["from"], window["to"], window["days"]) == ("2026-09-24", D30, 7)
    platforms = {p["platform"]: (p["days_ok"], p["days"]) for p in window["platforms"]}
    assert platforms["youtube"] == (2, 2) and platforms["tiktok"][1] == 2
    assert discover.build_radar(fx, "all")["window"] is None
    assert discover.build_radar(Patched(health_days=lambda *a: None), "ZA")["window"]["platforms"] is None


CREATOR_T2 = iid("creator", "reddit:t2_cw89ipiz")
CREATOR_UC = iid("creator", "youtube:ucgvgmmyewp9hxdqmfljbpkq")
CREATOR_NAMED = iid("creator", "youtube:ucnamednamednamednamednn")


def creator_rows(run, market):
    base = FixtureStore().item_states(run, market)[0]
    rows = [dict(base, item_id=CREATOR_T2, kind="creator", label="t2_cw89ipiz", canonical_key="reddit:t2_cw89ipiz"),
            dict(base, item_id=CREATOR_UC, kind="creator", label=None,
                 canonical_key="youtube:ucgvgmmyewp9hxdqmfljbpkq"),
            dict(base, item_id=CREATOR_NAMED, kind="creator", label="Agile Mind Cartoons",
                 canonical_key="youtube:ucnamednamednamednamednn")]
    return rows if market in ("ZA", "all") else []


def titles(store):
    out = discover.build_discover(store, "ZA")
    return {c["item_id"]: c["title"] for c in out["items"] + [h["card"] for h in out["held_back"]["items"]]}


def test_creator_items_titled_with_an_id_get_the_creators_name():
    names = {"reddit:t2_cw89ipiz": {"creator_id": "t2_cw89ipiz", "platform": "reddit", "handle": "jozi_vibes",
                                    "display_name": None}}
    asked = []
    store = Patched(item_states=creator_rows,
                    creator_names=lambda keys: asked.append(sorted(keys)) or names)
    store.suppressed_creators = lambda: set()
    got = titles(store)
    assert asked == [["reddit:t2_cw89ipiz", "youtube:ucgvgmmyewp9hxdqmfljbpkq"]]  # a real label is never looked up
    assert got[CREATOR_T2] == "@jozi_vibes"
    assert got[CREATOR_UC] == "YouTube account, name not collected"
    assert got[CREATOR_NAMED] == "Agile Mind Cartoons"


def test_creator_display_name_wins_over_handle():
    names = {"youtube:ucgvgmmyewp9hxdqmfljbpkq": {"creator_id": "UCgvgmmyewp9hxdqmfljbpkq", "platform": "youtube",
                                                  "handle": None, "display_name": "Mzansi Kitchen"}}
    store = Patched(item_states=creator_rows, creator_names=lambda keys: names)
    store.suppressed_creators = lambda: set()
    assert titles(store)[CREATOR_UC] == "Mzansi Kitchen"


def test_suppressed_creator_or_unreadable_list_is_never_named():
    names = {"reddit:t2_cw89ipiz": {"creator_id": "t2_cw89ipiz", "platform": "reddit", "handle": "jozi_vibes",
                                    "display_name": "Jozi Vibes"}}
    hidden = Patched(item_states=creator_rows, creator_names=lambda keys: names)
    hidden.suppressed_creators = lambda: {"t2_cw89ipiz"}
    hidden.creators_by_id = lambda ids: [{"creator_id": "t2_cw89ipiz", "platform": "reddit", "handle": "jozi_vibes"}]
    assert "Jozi Vibes" not in titles(hidden).values()
    unread = Patched(item_states=creator_rows, creator_names=lambda keys: names)
    unread.suppressed_creators = lambda: None
    assert "Jozi Vibes" not in titles(unread).values()


def test_a_raw_creator_id_is_never_a_title_even_when_no_name_may_be_read():
    """Visual QA, 5 October 2026: Discover showed "t2_x9o72po41" as a title. With the suppression list unread no
    name is read, and a suppressed creator is not named, but an id no reader can use still reads as an account."""
    names = {"reddit:t2_cw89ipiz": {"creator_id": "t2_cw89ipiz", "platform": "reddit", "handle": "jozi_vibes",
                                    "display_name": "Jozi Vibes"}}
    unread = Patched(item_states=creator_rows, creator_names=lambda keys: names)
    unread.suppressed_creators = lambda: None
    got = titles(unread)
    assert got[CREATOR_T2] == "Reddit account, name not collected"
    assert got[CREATOR_UC] == "YouTube account, name not collected"
    assert got[CREATOR_NAMED] == "Agile Mind Cartoons"
    hidden = Patched(item_states=creator_rows, creator_names=lambda keys: names)
    hidden.suppressed_creators = lambda: {"t2_cw89ipiz"}
    hidden.creators_by_id = lambda ids: [{"creator_id": "t2_cw89ipiz", "platform": "reddit", "handle": "jozi_vibes"}]
    assert titles(hidden)[CREATOR_T2] == "Reddit account, name not collected"
    assert not any(re.fullmatch(r"t2_\w+", t) for t in titles(hidden).values())


def test_a_reddit_account_id_label_on_any_item_is_not_shown_as_its_title():
    other = iid("creator", "reddit:jozi_vibes")

    def states(run, market):
        base = FixtureStore().item_states(run, market)[0]
        return [dict(base, item_id=other, kind="creator", label="t2_x9o72po41", canonical_key="reddit:jozi_vibes",
                     platforms=["reddit"])] if market in ("ZA", "all") else []

    assert titles(Patched(item_states=states))[other] == "reddit:jozi_vibes"


def test_a_spike_no_creator_posted_reads_as_a_chart_place():
    """Visual QA, 5 October 2026: Drake tracks on the charts read "Spike" with 0 creators. detect's spike rule also
    fires on a chart's top 10 on 2 pulls alone; that card says so instead."""
    def states(run, market):
        rows = FixtureStore().item_states(run, market)
        return [dict(r, state="spike", creators3=c) for r, c in zip(rows, (0, None, 7))]

    out = discover.build_discover(Patched(item_states=states), "ZA")
    cards = out["items"] + [h["card"] for h in out["held_back"]["items"]]
    words = sorted(c["state_word"] for c in cards if c["state"] == "spike")
    assert words.count("High on the charts") == 2 and "Spike" in words


def test_topic_history_words_a_chart_only_spike_day():
    fx = FixtureStore()
    history = [{"metric_date": "2026-09-29", "state": "spike", "creators3": None},
               {"metric_date": D30, "state": "spike", "creators3": 6}]
    out = discover.build_topic(Patched(item_history=lambda *a: history), HERITAGE, "ZA")
    assert [h["state_word"] for h in out["history"]] == ["High on the charts", "Spike"]
    assert fx.item_history(HERITAGE, "ZA", "2026-09-01", D30)[0].keys() == {"metric_date", "state", "creators3"}


# ---------- Topic pages ----------

def test_topic_page(fx):
    out = discover.build_topic(fx, HERITAGE, "ZA")
    assert set(out) == {"card", "aliases", "history", "series", "spread", "origin", "news", "waves", "authenticity",
                        "evidence"}
    assert out["card"]["item_id"] == HERITAGE and out["card"]["order"] == 2 and "held_back" not in out["card"]
    assert out["aliases"] == ["#heritagefixture", "fixture heritage"]
    assert out["history"] == [{"date": "2026-09-29", "state": "recurring", "state_word": "Recurring"},
                              {"date": D30, "state": "recurring", "state_word": "Recurring"}]
    assert out["spread"] is None
    assert [w["peak_date"] for w in out["waves"]] == ["2026-09-29", "2025-09-24"]
    assert is_figure(out["waves"][1]["peak_posts"], 240, "posts on the peak day")
    assert out["waves"][1]["peak_posts"]["query_id"] == "q_item_waves"
    assert out["authenticity"] == {"flag": "not_assessed", "flag_word": "Not assessed (thin sample)", "signals": []}
    ev = out["evidence"]
    assert [e["id"] for e in ev] == ["tt_her_001", "x_her_002"]  # the NG post is another market's
    assert ev[0]["text"] == "Heritage day fit check, fixture post" and ev[0]["market"] == "ZA"
    assert ev[0]["engagement"] == {"views": 18400, "likes": 1200, "comments": 45, "shares": 30}
    assert ev[0]["posted_at"] == "2026-09-29T17:10:00+02:00" and ev[0]["creator_tier"] == "micro"


def test_topic_series_have_a_point_per_day_with_gaps(fx):
    series = discover.build_topic(fx, HERITAGE, "ZA")["series"]
    assert [(s["platform"], s["series"]) for s in series] == [("tiktok", "feed_tiktok"), ("x", "panel_x_hub")]
    feed = series[0]
    assert feed["series_words"] == "TikTok" and feed["unit"] == "appearances a day"
    assert series[1]["series_words"] == "X" and series[1]["unit"] == "posts a day"
    pts = feed["points"]
    assert len(pts) == discover.SERIES_DAYS and pts[-1]["date"] == D30 and pts[0]["date"] == "2026-09-03"
    by_day = {p["date"]: p["value"] for p in pts}
    assert by_day["2026-09-23"] is None      # before the series started
    assert by_day["2026-09-24"] == 3
    assert by_day["2026-09-27"] is None      # invalid day: a gap, never a zero
    assert by_day[D30] == 18
    assert all(set(p) == {"date", "value", "expected_low", "expected_high"} for p in pts)


def test_topic_authenticity_signals(fx):
    auth = discover.build_topic(fx, RISING, "ZA")["authenticity"]
    assert auth["flag"] == "check_pattern" and auth["flag_word"] == "Unusual posting pattern"
    sig = {s["signal"]: s for s in auth["signals"]}
    assert sig["young_accounts"]["words"] == "Accounts under 30 days old"
    assert sig["young_accounts"]["share"] is None
    assert sig["burst"]["words"]
    assert sig["coaction"]["words"]
    assert is_figure(sig["coaction"]["share"], 0.12)
    assert sig["coaction"]["share"]["run_id"] == "r_coaction_20260930_01"


def test_topic_authenticity_with_no_stored_assessment_is_not_read_as_clear(fx):
    assert discover.build_topic(fx, STEP, "ZA")["authenticity"]["flag"] is None  # stored "clear"
    plain = FixtureStore()

    def states(run, market):
        return [{k: v for k, v in r.items() if k != "authenticity"} if r["item_id"] == STEP else r
                for r in plain.item_states(run, market)]

    for missing in (states, lambda run, market: [dict(r, authenticity=None) if r["item_id"] == STEP else r
                                                 for r in plain.item_states(run, market)]):
        auth = discover.build_topic(Patched(item_states=missing), STEP, "ZA")["authenticity"]
        assert auth["flag"] == "not_stored" and auth["flag_word"] is None


def test_card_flags_are_unchanged_by_the_topic_assessment_wording(fx):
    plain = FixtureStore()
    store = Patched(item_states=lambda run, market: [dict(r, authenticity=None) if r["item_id"] == STEP else r
                                                    for r in plain.item_states(run, market)])
    assert discover.build_topic(store, STEP, "ZA")["card"].get("flag") is None


def test_signal_words_never_read_as_a_persons_age():
    for words in discover.SIGNAL_WORDS.values():
        assert "young" not in words.lower() and "old people" not in words.lower()
    assert discover.SIGNAL_WORDS["young_accounts"] == "Accounts under 30 days old"


def test_topic_held_back_item_returns_its_card_with_the_reason(fx):
    out = discover.build_topic(fx, BOTNET, "ZA")
    assert out["card"]["held_back"] == {"rule": "G4", "reason": "likely_coordinated",
                                       "reason_text": "Likely coordinated"}
    assert out["authenticity"]["flag"] == "likely_coordinated"


def test_gate_reason_reads_as_plain_words_on_discover_and_the_topic_page(fx):
    raw = "Paid-led: sponsored or brand-owned share 0.62"
    store = Patched(item_gate=lambda market, run_date: [{"item_id": OUTAGE, "market": "ZA", "rule": "G5", "reason": "paid_led",
                                               "reason_text": raw}])
    plain = {"rule": "G5", "reason": "paid_led", "reason_text": "Mostly sponsored or brand posts (62%)",
             "reason_raw": raw}
    held = next(h for h in discover.build_discover(store, "ZA")["held_back"]["items"] if h["item_id"] == OUTAGE)
    assert (held["reason_text"], held["reason_raw"]) == (plain["reason_text"], raw)
    assert held["card"]["held_back"] == plain
    assert discover.build_topic(store, OUTAGE, "ZA")["card"]["held_back"] == plain


def test_topic_unknown_item_or_market(fx):
    with pytest.raises(NotFound):
        discover.build_topic(fx, "nope", "ZA")
    with pytest.raises(NotFound):
        discover.build_topic(fx, HERITAGE, "NG")


def test_topic_degrades_when_l2_views_are_missing(fx):
    store = Patched(item_series=lambda *a: None, item_waves=lambda *a: None, item_evidence=lambda *a: None,
                    coord_signals=lambda *a: None, item_gate=lambda market, run_date: None)
    out = discover.build_topic(store, RISING, "ZA")
    assert out["series"] is None and out["waves"] is None and out["evidence"] is None
    assert [s["signal"] for s in out["authenticity"]["signals"]] == ["young_accounts", "burst"]
    assert out["card"]["reach"]["value"] == 18


# ---------- X collected as "twitter" ----------

def _as_twitter(value):
    return "twitter" if value == "x" else value


def twitter_store():
    """Staging posts carry platform "twitter"; every x in the fixtures becomes twitter, brief evidence included."""
    base = FixtureStore()

    def briefs(date):
        rows = base.briefs(date)
        for r in rows:
            p = r["payload"]
            for c in (p.get("cards") or []) + (p.get("more") or []):
                for e in c.get("evidence") or []:
                    e["platform"] = "twitter"
        return rows

    return Patched(
        briefs=briefs,
        item_states=lambda run, market: [dict(r, platforms=[_as_twitter(p) for p in r.get("platforms") or []])
                                         for r in base.item_states(run, market)],
        item_series=lambda *a: [dict(r, platform=_as_twitter(r.get("platform"))) for r in base.item_series(*a)],
        item_evidence=lambda *a: [dict(r, platform=_as_twitter(r.get("platform"))) for r in base.item_evidence(*a)])


def platform_values(value, path="$"):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "platform":
                yield f"{path}.{k}", v
            elif k == "platforms" and isinstance(v, list):
                yield from ((f"{path}.{k}[{n}]", p) for n, p in enumerate(v))
            else:
                yield from platform_values(v, f"{path}.{k}")
    elif isinstance(value, list):
        for n, v in enumerate(value):
            yield from platform_values(v, f"{path}[{n}]")


def test_discover_names_twitter_as_x_in_filters_and_matches_either_request_word():
    store = twitter_store()
    out = discover.build_discover(store, "ZA")
    assert out["filters"]["platforms"] == ["instagram", "tiktok", "x"]
    assert ids(discover.build_discover(store, "ZA", platform="x")["items"]) == [RISING]
    assert ids(discover.build_discover(store, "ZA", platform="twitter")["items"]) == [RISING]
    assert discover.build_discover(store, "ZA", platform="twitter")["held_back"]["count"] == 1
    assert ids(discover.build_discover(FixtureStore(), "ZA", platform="twitter")["items"]) == [RISING]


def test_discover_and_topic_bodies_never_say_twitter():
    store = twitter_store()
    bodies = [discover.build_discover(store, m) for m in ("ZA", "NG", "KE", "all")]
    bodies += [discover.build_topic(store, r["item_id"], r["market"])
               for r in store.item_states({"run_id": RUN, "run_date": D30}, "all")]
    seen = [(path, v) for body in bodies for path, v in platform_values(body)]
    assert any(v == "x" for _, v in seen)
    assert [(path, v) for path, v in seen if v == "twitter"] == []


def test_topic_series_and_evidence_read_twitter_as_x():
    out = discover.build_topic(twitter_store(), HERITAGE, "ZA")
    assert [(s["platform"], s["series"]) for s in out["series"]] == [("tiktok", "feed_tiktok"), ("x", "panel_x_hub")]
    assert out["series"][1]["series_words"] == "X"
    assert [(e["id"], e["platform"]) for e in out["evidence"]] == [("tt_her_001", "tiktok"), ("x_her_002", "x")]


# ---------- BigQuery store ----------

CATALOG_FULL = [
    *({"ds": "intelligence_42_core", "n": n, "what": "table"} for n in (
        "v_good_runs", "v_item_state_current", "cultural_map", "v_series_test_current", "v_item_waves",
        "v_coord_signals_current", "credit_ledger", "v_collection_health_current")),
    *({"ds": "intelligence_42_agent", "n": n, "what": "table"} for n in (
        "runs", "v_briefs_current", "v_item_gate_current", "v_item_evidence", "engine_scorecard")),
    {"ds": "intelligence_42_agent", "n": "tvf_item_timeseries", "what": "routine"},
    {"ds": "intelligence_42_agent", "n": "model_usd", "what": "runs_column"},
]
CATALOG_NOW = [r for r in CATALOG_FULL if r["n"] not in (
    "v_item_gate_current", "v_item_evidence", "tvf_item_timeseries", "engine_scorecard", "model_usd")]


def test_fixture_store_market_scope_joins_the_required_za_couple_row_by_date_item_and_market(tmp_path):
    couple = "191cbe10c89c022bac90815ff4393424dae55059cc9fc8726a3ca1c96ef0386c"
    state = {"item_id": couple, "metric_date": D30, "market": "ZA", "run_id": RUN, "label": "#couple"}
    (tmp_path / "item_state.json").write_text(json.dumps([state]), encoding="utf-8")
    (tmp_path / "item_market_scope.json").write_text(json.dumps([
        {"item_id": couple, "metric_date": D30, "market": "ZA", "market_scope": "global",
         "market_posts7": 3, "total_posts7": 12, "market_share7": 0.25},
        {"item_id": couple, "metric_date": "2026-09-29", "market": "ZA", "market_scope": "market",
         "market_posts7": 9, "total_posts7": 9, "market_share7": 1.0},
        {"item_id": couple, "metric_date": D30, "market": "NG", "market_scope": "market",
         "market_posts7": 8, "total_posts7": 8, "market_share7": 1.0},
        {"item_id": STEP, "metric_date": D30, "market": "ZA", "market_scope": "market",
         "market_posts7": 6, "total_posts7": 6, "market_share7": 1.0},
    ]), encoding="utf-8")

    rows = FixtureStore(root=tmp_path).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert rows == [dict(state, breakout_creators=None, tone_today=None, tone_before=None, market_scope="global",
                         market_posts7=3, market_news_posts7=None, total_posts7=12, market_share7=0.25)]
    card = discover._build_card(rows[0], None, False, None)
    assert card["item_id"] == couple and card["market"] == "ZA" and card["market_scope"] == "global"
    assert (card["market_posts7"], card["total_posts7"], card["market_share7"]) == (3, 12, 0.25)


@pytest.mark.parametrize("scope_rows", [
    [], [{"item_id": "191cbe10c89c022bac90815ff4393424dae55059cc9fc8726a3ca1c96ef0386c",
          "metric_date": D30, "market": "ZA", "market_scope": None}],
    [{"item_id": "191cbe10c89c022bac90815ff4393424dae55059cc9fc8726a3ca1c96ef0386c",
      "metric_date": D30, "market": "ZA", "market_scope": "invalid"}],
])
def test_fixture_store_present_unproven_scope_stays_global_over_brief(scope_rows, tmp_path):
    couple = "191cbe10c89c022bac90815ff4393424dae55059cc9fc8726a3ca1c96ef0386c"
    state = {"item_id": couple, "metric_date": D30, "market": "ZA", "run_id": RUN, "label": "#couple"}
    (tmp_path / "item_state.json").write_text(json.dumps([state]), encoding="utf-8")
    (tmp_path / "item_market_scope.json").write_text(json.dumps(scope_rows), encoding="utf-8")

    (row,) = FixtureStore(root=tmp_path).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    card = discover._build_card(row, {"market_scope": "market", "market_posts7": 3,
                                      "total_posts7": 12, "market_share7": 0.25}, False, None)
    assert row["market_scope"] == "global" and card["market_scope"] == "global"
    assert (row["market_posts7"], row["total_posts7"], row["market_share7"]) == (None, None, None)
    assert (card["market_posts7"], card["total_posts7"], card["market_share7"]) == (None, None, None)


def news_scope_row(market_posts7, market_news_posts7, **extra):
    return {"item_id": STEP, "market": "ZA", "metric_date": D30, "run_id": RUN, "label": "#step",
            "state": "rising", "market_scope": "market", "market_posts7": market_posts7,
            "market_news_posts7": market_news_posts7, "total_posts7": 12, "market_share7": market_posts7 / 12,
            "geo_status": "local", **extra}


@pytest.mark.parametrize(("row", "flag"), [
    (news_scope_row(7, 7), "market_unconfirmed"),
    (news_scope_row(7, 6), None),
    (news_scope_row(7, 0), None),
    (news_scope_row(0, 0, market_scope="global", market_share7=0.0), None),
    (news_scope_row(7, 7, geo_status="not_local"), None),
    (news_scope_row(7, 7, authenticity="check_pattern"), "check_pattern"),
    (news_scope_row(7, 7, authenticity="not_assessed"), "market_unconfirmed"),
    (news_scope_row(7, None), None),
])
def test_an_item_whose_market_posts_are_all_news_carries_market_unconfirmed(row, flag):
    card = discover._build_card(row, None, False, None)

    assert card["flag"] == flag
    assert card["market_scope"] == row["market_scope"]


def test_a_flag_the_card_holds_is_not_overridden_by_a_lower_flag_of_the_row():
    # the row cannot see the news count (a catalog that lags): not_assessed must not hide the card's market_unconfirmed
    row = news_scope_row(7, None, authenticity="not_assessed")
    card = {"item_id": STEP, "flag": "market_unconfirmed", "explanation_status": "held", "title": "x"}

    assert discover._build_card(row, card, False, None)["flag"] == "market_unconfirmed"


@pytest.mark.parametrize(("row_flag", "card_flag", "expected"), [
    ("not_assessed", "market_unconfirmed", "market_unconfirmed"),
    ("market_unconfirmed", "not_assessed", "market_unconfirmed"),
    ("paid_led", "market_unconfirmed", "paid_led"),
    ("market_unconfirmed", "paid_led", "paid_led"),
    ("check_pattern", "not_assessed", "check_pattern"),
    ("not_assessed", "likely_coordinated", "likely_coordinated"),
    ("market_unconfirmed", "market_unconfirmed", "market_unconfirmed"),
    (None, "market_unconfirmed", "market_unconfirmed"),
    ("not_assessed", None, "not_assessed"),
    (None, None, None),
])
def test_the_higher_of_the_row_and_card_flags_is_kept(row_flag, card_flag, expected):
    assert discover._higher_flag(row_flag, card_flag) == expected


def test_fixture_store_hands_on_the_market_news_count(tmp_path):
    state = {"item_id": STEP, "metric_date": D30, "market": "ZA", "run_id": RUN, "label": "#step"}
    (tmp_path / "item_state.json").write_text(json.dumps([state]), encoding="utf-8")
    (tmp_path / "item_market_scope.json").write_text(json.dumps([
        {"item_id": STEP, "metric_date": D30, "market": "ZA", "market_scope": "market", "market_posts7": 7,
         "market_news_posts7": 7, "total_posts7": 12, "market_share7": 7 / 12}]), encoding="utf-8")

    (row,) = FixtureStore(root=tmp_path).item_states({"run_id": RUN, "run_date": D30}, "ZA")

    assert row["market_news_posts7"] == 7
    assert discover._build_card(row, None, False, None)["flag"] == "market_unconfirmed"


def test_bigquery_item_states_reads_the_market_news_count_from_the_view():
    catalog = CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_market_scope", "what": "table"},
                              {"ds": "intelligence_42_core", "n": "market_news_posts7", "what": "scope_column"}]
    client = FakeClient(catalog, rows=[news_scope_row(7, 7)])
    (row,) = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)

    assert "t.market_news_posts7" in sql and "ms.market_news_posts7" in sql
    assert row["market_news_posts7"] == 7


def test_bigquery_item_states_before_the_view_gains_the_news_column_reads_it_as_unknown():
    # the API image can run before the detect job re-creates the view: the page must still load, without the count
    catalog = CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_market_scope", "what": "table"}]
    client = FakeClient(catalog, rows=[news_scope_row(7, None)])
    (row,) = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)

    assert "t.market_news_posts7" not in sql and "ms.market_news_posts7" not in sql
    assert "CAST(NULL AS INT64) AS market_news_posts7" in sql
    assert "ms.market_posts7" in sql and "ms.total_posts7" in sql
    assert row["market_news_posts7"] is None


def test_bigquery_item_states_without_the_scope_view_has_no_market_news_count():
    client = FakeClient(CATALOG_FULL, rows=[{"item_id": STEP, "market": "ZA", "metric_date": D30, "run_id": RUN}])
    BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)

    assert "CAST(NULL AS INT64) AS market_news_posts7" in sql


class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    def __init__(self, catalog, rows=None):
        self.catalog, self.rows, self.calls = catalog, rows or [], []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return FakeJob(self.catalog if "INFORMATION_SCHEMA" in sql else self.rows)


@pytest.fixture(autouse=True)
def fresh_catalog(monkeypatch):
    monkeypatch.setattr(store_mod, "_CATALOG", {})


def run_all_reads(bq):
    run = {"run_id": RUN, "run_date": D30}
    return {
        "latest_detect_run": bq.latest_detect_run(),
        "item_states": bq.item_states(run, "ZA"),
        "item_gate": bq.item_gate("ZA", D30),
        "item_history": bq.item_history(STEP, "ZA", "2026-07-01", D30),
        "item_series": bq.item_series(STEP, "ZA", 28),
        "item_waves": bq.item_waves(STEP, "ZA"),
        "coord_signals": bq.coord_signals(STEP, "ZA", D30),
        "item_evidence": bq.item_evidence(STEP, "ZA"),
    }


def test_bigquery_v2_reads_are_parameterised_and_capped():
    client = FakeClient(CATALOG_FULL, rows=[{"run_date": D30, "run_id": RUN}])
    run_all_reads(BigQueryStore(client=client))
    sqls = [s for s, _ in client.calls]
    assert sum("INFORMATION_SCHEMA" in s for s in sqls) == 1
    reads = [s for s in sqls if "INFORMATION_SCHEMA" not in s]
    assert len(reads) == 8
    joined = "\n".join(reads)
    for name in ("intelligence_42_core.v_good_runs", "intelligence_42_core.v_item_state_current",
                 "intelligence_42_core.cultural_map", "intelligence_42_core.v_series_test_current",
                 "intelligence_42_agent.v_item_gate_current", "intelligence_42_core.v_item_waves",
                 "intelligence_42_core.v_coord_signals_current", "intelligence_42_agent.v_item_evidence",
                 "intelligence_42_agent.tvf_item_timeseries"):
        assert f"ogilvy-trends-v2.{name}" in joined, name
    assert "valid_to IS NULL" in joined
    for sql, cfg in client.calls:
        assert cfg.maximum_bytes_billed == 2_000_000_000
    assert STEP not in joined and "'ZA'" not in joined and D30 not in joined
    names = {p.name for _, cfg in client.calls for p in cfg.query_parameters}
    assert {"d", "run_id", "market", "item_id", "start", "end", "days"} <= names
    for sql in reads:
        assert not re.search(r"\b(INSERT|UPDATE|MERGE|CREATE)\b", sql)


def test_bigquery_item_states_joins_market_scope_on_the_dated_item_market_key():
    catalog = CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_market_scope", "what": "table"}]
    client = FakeClient(catalog, rows=[{"item_id": STEP, "market": "ZA", "metric_date": D30,
                                       "run_id": RUN, "market_scope": "market", "market_posts7": 3,
                                       "total_posts7": 12, "market_share7": 0.25}])
    (row,) = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "intelligence_42_core.v_item_market_scope" in sql
    assert "WHERE t.metric_date = @d" in sql
    assert "ms.item_id = s.item_id AND ms.market = s.market AND ms.metric_date = s.metric_date" in sql
    assert "IF(ms.market_scope = 'market', 'market', 'global') AS market_scope" in sql
    assert all(f"ms.{field}" in sql for field in ("market_posts7", "total_posts7", "market_share7"))
    assert (row["market_posts7"], row["total_posts7"], row["market_share7"]) == (3, 12, 0.25)


def test_bigquery_item_states_without_market_scope_view_skips_the_join():
    client = FakeClient(CATALOG_FULL, rows=[{"item_id": STEP, "market": "ZA", "metric_date": D30,
                                            "run_id": RUN}])
    BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "v_item_market_scope" not in sql
    assert "CAST(NULL AS STRING) AS market_scope" in sql
    assert "CAST(NULL AS INT64) AS market_posts7" in sql
    assert "CAST(NULL AS INT64) AS total_posts7" in sql
    assert "CAST(NULL AS FLOAT64) AS market_share7" in sql


def test_bigquery_missing_views_degrade_to_none_without_querying_them():
    row = {"run_id": RUN, "run_date": D30, "x": 1}
    client = FakeClient(CATALOG_NOW, rows=[row])
    out = run_all_reads(BigQueryStore(client=client))
    assert out["item_gate"] is None and out["item_series"] is None and out["item_evidence"] is None
    assert out["item_waves"] == [row] and out["coord_signals"] == [row]
    joined = "\n".join(s for s, _ in client.calls)
    assert "v_item_gate_current" not in joined and "v_item_evidence" not in joined
    assert "tvf_item_timeseries" not in joined


def test_bigquery_no_detect_views_means_no_detect_run():
    client = FakeClient([r for r in CATALOG_FULL if r["n"] != "v_item_state_current"])
    assert BigQueryStore(client=client).latest_detect_run() is None


def test_bigquery_catalog_is_read_once_per_process():
    client = FakeClient(CATALOG_FULL)
    BigQueryStore(client=client).item_gate("ZA", D30)
    BigQueryStore(client=client).item_evidence(STEP, "ZA")
    assert sum("INFORMATION_SCHEMA" in s for s, _ in client.calls) == 1


def test_bigquery_catalog_failure_is_not_cached():
    class Flaky(FakeClient):
        fail = True

        def query(self, sql, job_config=None):
            if "INFORMATION_SCHEMA" in sql and Flaky.fail:
                Flaky.fail = False
                raise RuntimeError("unreachable")
            return super().query(sql, job_config)

    client = Flaky(CATALOG_FULL)
    with pytest.raises(RuntimeError):
        BigQueryStore(client=client).item_gate("ZA", D30)
    assert BigQueryStore(client=client).item_gate("ZA", D30) == []


def test_bigquery_rows_normalise_nested_dates():
    import datetime as dt

    row = {"item_id": STEP, "market": "ZA", "metric_date": dt.date(2026, 9, 30), "first_seen": dt.date(2026, 9, 1),
           "last_wave": {"peak_date": dt.date(2025, 9, 24), "peak_posts": 240}}
    client = FakeClient(CATALOG_FULL, rows=[row])
    (out,) = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert out["metric_date"] == D30 and out["first_seen"] == "2026-09-01"
    assert out["last_wave"] == {"peak_date": "2025-09-24", "peak_posts": 240}


# ---------- review notes: degrading joins, catalog time to live ----------

def test_bigquery_item_states_without_series_tests_skips_that_join():
    client = FakeClient([r for r in CATALOG_FULL if r["n"] != "v_series_test_current"])
    BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "v_series_test_current" not in sql
    assert "intelligence_42_core.cultural_map" in sql
    assert "main_vel" in sql and "platforms" in sql


def test_bigquery_item_states_without_the_map_falls_back_to_item_id_and_unknown_status():
    client = FakeClient([r for r in CATALOG_FULL if r["n"] != "cultural_map"])
    BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "cultural_map" not in sql
    assert "s.item_id AS label" in sql and "'unknown' AS map_status" in sql
    assert "v_series_test_current" in sql


def test_bigquery_item_states_with_neither_object_still_reads():
    client = FakeClient([r for r in CATALOG_FULL if r["n"] not in ("cultural_map", "v_series_test_current")],
                        rows=[{"item_id": STEP, "market": "ZA", "metric_date": D30, "run_id": RUN}])
    rows = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert rows[0]["item_id"] == STEP
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "cultural_map" not in sql and "v_series_test_current" not in sql


def test_unknown_map_status_is_held_by_the_gate_not_called_inactive(fx):
    def item_states(run, market):
        rows = FixtureStore().item_states(run, market)
        return [dict(r, label=r["item_id"], map_status="unknown", canonical_key=None) for r in rows]

    out = discover.build_discover(Patched(item_states=item_states, item_gate=lambda m, d: None), "ZA")
    held = {h["item_id"]: h["reason"] for h in out["held_back"]["items"]}
    assert held[REJECTED] == "held_by_gate"
    assert "not_active" not in held.values()
    not_in_brief = next(c for c in out["items"] if c["item_id"] == RISING)
    assert not_in_brief["title"] == "Social media post"
    assert not_in_brief["title"] != not_in_brief["item_id"]


def test_bigquery_catalog_is_read_again_after_ten_minutes(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(store_mod.time, "monotonic", lambda: clock["t"])
    client = FakeClient(CATALOG_FULL)
    BigQueryStore(client=client).item_gate("ZA", D30)
    clock["t"] += 599
    BigQueryStore(client=client).item_gate("ZA", D30)
    assert sum("INFORMATION_SCHEMA" in s for s, _ in client.calls) == 1
    clock["t"] += 2
    BigQueryStore(client=client).item_gate("ZA", D30)
    assert sum("INFORMATION_SCHEMA" in s for s, _ in client.calls) == 2


# ---------- alerts (contract.md section 10.5) ----------

def watch(watch_id, target, rule, market="ZA", status="active", label="A watch"):
    return {"watch_id": watch_id, "created_at": "2026-09-29T08:00:00+02:00", "who": "passcode",
            "target": target, "market": market, "rule": rule, "label": label, "status": status}


def test_fixture_store_previous_detect_run_and_matches(fx, tmp_path):
    assert fx.latest_detect_run(until="2026-09-29") == {"run_id": "r_detect_20260929_01", "run_date": "2026-09-29"}
    assert fx.latest_detect_run(until="2026-09-28") is None
    assert fx.latest_detect_run(until=D30) == {"run_id": RUN, "run_date": D30}
    assert fx.watch_matches({"run_id": RUN, "run_date": D30}) is None
    (tmp_path / "watch_matches.json").write_text(json.dumps([
        {"watch_id": "w_1", "match_date": D30, "item_id": "a", "market": "ZA", "method": "semantic", "run_id": RUN},
        {"watch_id": "w_1", "match_date": D30, "item_id": "b", "market": "ZA", "method": "semantic", "run_id": RUN},
        {"watch_id": "w_2", "match_date": "2026-09-29", "item_id": "c", "market": "ZA", "method": "semantic",
         "run_id": "r_detect_20260929_01"}]), encoding="utf-8")
    assert FixtureStore(root=tmp_path).watch_matches({"run_id": RUN, "run_date": D30}) == {"w_1": ["a", "b"]}


def test_bigquery_previous_detect_run_and_matches_are_parameterised():
    client = FakeClient(CATALOG_FULL + [{"ds": "intelligence_42_agent", "n": "watch_matches", "what": "table"}],
                        rows=[{"run_date": "2026-09-29", "run_id": "r_detect_20260929_01",
                               "watch_id": "w_1", "item_id": "a"}])
    store = BigQueryStore(client=client)
    assert store.latest_detect_run(until="2026-09-29")["run_id"] == "r_detect_20260929_01"
    assert store.watch_matches({"run_id": RUN, "run_date": D30}) == {"w_1": ["a"]}
    reads = [(s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s]
    assert "@until" in reads[0][0] and "2026-09-29" not in reads[0][0]
    assert "ogilvy-trends-v2.intelligence_42_agent.watch_matches" in reads[1][0] and RUN not in reads[1][0]
    assert all(c.maximum_bytes_billed == 2_000_000_000 for _, c in reads)


def with_breakouts(tmp_path, rows):
    for f in store_mod.FIXTURES.glob("*.json"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    (tmp_path / "breakout_signals.json").write_text(json.dumps(rows), encoding="utf-8")
    return FixtureStore(root=tmp_path)


def signal(item_id, metric_date=D30, market="ZA", creators=2, run_id=RUN):
    return {"metric_date": metric_date, "market": market, "item_id": item_id, "run_id": run_id,
            "creators": creators, "posts": creators, "evidence_post_ids": [], "top_ratio": 4.0,
            "held_flagged": 0, "rule_version": "breakout-v1"}


def test_fixture_item_states_carry_the_days_breakout_count_and_null_without_the_table(fx, tmp_path):
    run = {"run_id": RUN, "run_date": D30}
    assert {r["breakout_creators"] for r in fx.item_states(run, "all")} == {None}
    store = with_breakouts(tmp_path, [signal(STEP, creators=2), signal(STEP, creators=4, run_id="r_other"),
                                      signal(HERITAGE, market="NG"), signal(RISING, metric_date="2026-09-29")])
    got = {(r["item_id"], r["market"]): r["breakout_creators"] for r in store.item_states(run, "all")}
    assert got[(STEP, "ZA")] == 4
    assert got[(HERITAGE, "ZA")] is None and got[(RISING, "ZA")] is None


def test_alerts_fire_on_a_new_breakout_and_not_on_one_the_run_before_had(tmp_path):
    store = with_breakouts(tmp_path, [signal(STEP, creators=3), signal(HERITAGE),
                                      signal(HERITAGE, metric_date="2026-09-29", run_id="r_detect_20260929_01")])
    watches = [watch("w_step", {"kind": "item", "item_id": STEP}, {"breakout": True}, label="Step breakout"),
               watch("w_her", {"kind": "item", "item_id": HERITAGE}, {"breakout": True}),
               watch("w_rise", {"kind": "item", "item_id": RISING}, {"breakout": True})]
    out = discover.build_alerts(store, watches)
    assert [(a["watch_id"], a["item_id"], a["fired_because"]) for a in out["alerts"]] == [
        ("w_step", STEP, "Creator breakout: 3 creators well above their usual views")]
    assert out["waiting"] == []


def test_bigquery_item_states_read_the_days_breakout_signals_when_the_table_exists():
    catalog = CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "breakout_signals", "what": "table"}]
    client = FakeClient(catalog, rows=[{"item_id": STEP, "market": "ZA", "breakout_creators": 3}])
    (row,) = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "ogilvy-trends-v2.intelligence_42_core.breakout_signals" in sql
    assert "MAX(b.creators) AS breakout_creators" in sql and "WHERE b.metric_date = @d" in sql
    assert "bo.item_id = s.item_id AND bo.market = s.market" in sql
    assert row["breakout_creators"] == 3


def test_bigquery_item_states_without_breakout_signals_give_a_null_count():
    client = FakeClient(CATALOG_FULL, rows=[{"item_id": STEP, "market": "ZA"}])
    BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "breakout_signals" not in sql and "CAST(NULL AS INT64) AS breakout_creators" in sql


def with_fixtures(tmp_path, **tables):
    for f in store_mod.FIXTURES.glob("*.json"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    for name, rows in tables.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")
    return FixtureStore(root=tmp_path)


def tone(item_id, metric_date, value, market="ZA", posts=6):
    return {"metric_date": metric_date, "market": market, "item_id": item_id, "enriched_posts": posts, "tone": value}


def test_fixture_item_states_carry_the_tone_today_and_the_day_before_and_null_without_the_view(fx, tmp_path):
    run = {"run_id": RUN, "run_date": D30}
    assert {(r["tone_today"], r["tone_before"]) for r in fx.item_states(run, "all")} == {(None, None)}
    store = with_fixtures(tmp_path, item_tone_daily=[
        tone(STEP, D30, -0.5), tone(STEP, "2026-09-29", 0.25), tone(STEP, "2026-09-28", 0.75),
        tone(HERITAGE, D30, None, posts=3), tone(HERITAGE, "2026-09-29", 0.5), tone(RISING, D30, 0.5, market="NG")])
    got = {(r["item_id"], r["market"]): (r["tone_today"], r["tone_before"]) for r in store.item_states(run, "all")}
    assert got[(STEP, "ZA")] == (-0.5, 0.25)
    assert got[(HERITAGE, "ZA")] == (None, 0.5)
    assert got[(RISING, "ZA")] == (None, None)


def test_alerts_fire_on_a_tone_flip_and_on_a_creator_surge_detect_matched(tmp_path):
    store = with_fixtures(tmp_path, item_tone_daily=[
        tone(STEP, D30, -0.5), tone(STEP, "2026-09-29", 0.25), tone(HERITAGE, D30, 0.5),
        tone(HERITAGE, "2026-09-29", 0.25)], watch_matches=[
        {"watch_id": "w_surge", "match_date": D30, "item_id": HERITAGE, "market": "ZA", "method": "item_id",
         "run_id": "w"},
        {"watch_id": "w_surge", "match_date": "2026-09-29", "item_id": RISING, "market": "ZA", "method": "item_id",
         "run_id": "w"}])
    watches = [watch("w_step", {"kind": "item", "item_id": STEP}, {"tone_flip": True}, label="Step tone"),
               watch("w_her", {"kind": "item", "item_id": HERITAGE}, {"tone_flip": True}),
               watch("w_surge", {"kind": "creator", "value": "@fixture"}, {"creator_surge": True}, label="Surge")]
    out = discover.build_alerts(store, watches)
    assert [(a["watch_id"], a["item_id"], a["fired_because"]) for a in out["alerts"]] == [
        ("w_step", STEP, "Tone flipped from positive to negative"),
        ("w_surge", HERITAGE, "Creator surge: a post at three or more times their usual views")]
    assert out["waiting"] == []


def test_alerts_creator_surge_does_not_fire_again_when_the_previous_run_matched_it(tmp_path):
    row = {"watch_id": "w_surge", "item_id": HERITAGE, "market": "ZA", "method": "item_id", "run_id": "w"}
    store = with_fixtures(tmp_path, watch_matches=[dict(row, match_date=D30), dict(row, match_date="2026-09-29")])
    watches = [watch("w_surge", {"kind": "creator", "value": "@fixture"}, {"creator_surge": True}, label="Surge")]
    out = discover.build_alerts(store, watches)
    assert out["alerts"] == [] and out["waiting"] == []


def test_a_failing_tone_read_leaves_tones_null_and_still_returns_the_rows_discover_and_alerts_read(caplog):
    catalog = CATALOG_FULL + [{"ds": "intelligence_42_agent", "n": "v_item_tone_daily", "what": "table"}]

    class ToneFails(FakeClient):
        def query(self, sql, job_config=None):
            if "v_item_tone_daily" in sql:
                self.calls.append((sql, job_config))
                raise RuntimeError("tone view broken")
            return super().query(sql, job_config)

    client = ToneFails(catalog, rows=[{"item_id": STEP, "market": "ZA"}])
    with caplog.at_level("WARNING"):
        (row,) = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert (row["item_id"], row["tone_today"], row["tone_before"]) == (STEP, None, None)
    assert "tone" in caplog.text
    main = next(s for s, _ in client.calls if "v_item_state_current" in s)
    assert "v_item_tone_daily" not in main


def test_alerts_creator_surge_waits_without_watch_matches(fx):
    watches = [watch("w_surge", {"kind": "creator", "value": "@fixture"}, {"creator_surge": True}, label="Surge")]
    out = discover.build_alerts(fx, watches)
    assert out["alerts"] == []
    assert out["waiting"] == [{"watch_id": "w_surge", "label": "Surge",
                               "waiting": "Waiting for creator views detection"}]


class ByTable(FakeClient):
    """Answers the tone read with tone rows and every other read with rows."""

    def __init__(self, catalog, rows, tones):
        super().__init__(catalog, rows)
        self.tones = tones

    def query(self, sql, job_config=None):
        if "v_item_tone_daily" in sql:
            self.calls.append((sql, job_config))
            return FakeJob(self.tones)
        return super().query(sql, job_config)


def test_bigquery_item_states_read_the_tone_view_in_its_own_capped_query_for_two_days():
    catalog = CATALOG_FULL + [{"ds": "intelligence_42_agent", "n": "v_item_tone_daily", "what": "table"}]
    client = ByTable(catalog, rows=[{"item_id": STEP, "market": "ZA"}, {"item_id": HERITAGE, "market": "ZA"}],
                     tones=[{"item_id": STEP, "market": "ZA", "metric_date": D30, "tone": -0.5},
                            {"item_id": STEP, "market": "ZA", "metric_date": "2026-09-29", "tone": 0.25},
                            {"item_id": HERITAGE, "market": "NG", "metric_date": D30, "tone": 0.5}])
    rows = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    got = {r["item_id"]: (r["tone_today"], r["tone_before"]) for r in rows}
    assert got == {STEP: (-0.5, 0.25), HERITAGE: (None, None)}
    main = next(s for s, _ in client.calls if "v_item_state_current" in s)
    assert "v_item_tone_daily" not in main
    sql, cfg = next((s, c) for s, c in client.calls if "v_item_tone_daily" in s)
    assert "ogilvy-trends-v2.intelligence_42_agent.v_item_tone_daily" in sql
    assert "t.metric_date IN (@d, @d_before)" in sql
    params = {p.name: p.value for p in cfg.query_parameters}
    assert (str(params["d"]), str(params["d_before"])) == (D30, "2026-09-29")
    assert cfg.maximum_bytes_billed == 2_000_000_000


def test_bigquery_item_states_without_the_tone_view_give_null_tones_without_a_read():
    client = FakeClient(CATALOG_FULL, rows=[{"item_id": STEP, "market": "ZA"}])
    (row,) = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert not any("v_item_tone_daily" in s for s, _ in client.calls)
    assert (row["tone_today"], row["tone_before"]) == (None, None)


def test_bigquery_watch_matches_missing_table_is_none_without_a_query():
    client = FakeClient(CATALOG_FULL)
    assert BigQueryStore(client=client).watch_matches({"run_id": RUN, "run_date": D30}) is None
    assert not any("watch_matches" in s for s, _ in client.calls)


def test_alerts_fire_on_change_since_the_previous_run_and_carry_the_discover_card(fx):
    watches = [watch("w_rise", {"kind": "item", "item_id": RISING}, {"state_in": ["rising"]}),
               watch("w_step", {"kind": "hashtag", "value": "#fixture_za_step"}, {"state_in": ["emerging"]}),
               watch("w_her", {"kind": "item", "item_id": HERITAGE}, {"state_in": ["recurring"]}),
               watch("w_wait", {"kind": "item", "item_id": STEP}, {"creator_surge": True}, label="Step surge"),
               watch("w_off", {"kind": "item", "item_id": RISING}, {"state_in": ["rising"]}, status="paused")]
    out = discover.build_alerts(fx, watches)
    assert out["date"] == D30
    got = {a["watch_id"]: a for a in out["alerts"]}
    assert set(got) == {"w_rise", "w_step"}
    assert got["w_rise"]["fired_because"] == "Entered Rising" and got["w_rise"]["since"] == D30
    assert got["w_step"]["item_id"] == STEP and got["w_step"]["market"] == "ZA"
    # Restated 5 October 2026: a Discover page card also carries reach7, its 7-day reach from every lane, read for
    # the page only; the alert's card is the same card without it.
    discover_cards = {c["item_id"]: {k: v for k, v in c.items() if k != "reach7"}
                      for c in discover.build_discover(fx, "ZA")["items"]}
    for a in out["alerts"]:
        assert set(a) == {"watch_id", "label", "item_id", "market", "fired_because", "since", "card"}
        assert a["card"] == dict(discover_cards[a["item_id"]], watch_id=a["watch_id"])
    assert out["waiting"] == [{"watch_id": "w_wait", "label": "Step surge",
                               "waiting": "Waiting for creator views detection"}]


def test_alerts_on_held_back_items_keep_the_reason_on_the_card(fx):
    out = discover.build_alerts(fx, [watch("w_bot", {"kind": "item", "item_id": BOTNET}, {"state_in": ["rising"]})])
    (alert,) = out["alerts"]
    assert alert["card"]["held_back"]["reason"] == "likely_coordinated"


def test_alerts_for_a_date_use_that_run_and_the_run_before_it(fx):
    # 29 September is the first detect run, so there is nothing to compare against and nothing fires.
    out = discover.build_alerts(fx, [watch("w_new", {"kind": "item", "item_id": STEP}, {"state_in": ["new_to_42"]})],
                                date="2026-09-29")
    assert out["date"] == "2026-09-29"
    assert out["alerts"] == []
    assert out["waiting"] == [{"watch_id": "w_new", "label": "A watch",
                               "waiting": "Waiting for a second detect run to compare against"}]
    assert discover.build_alerts(fx, [watch("w_new", {"kind": "item", "item_id": STEP},
                                            {"state_in": ["new_to_42"]})])["alerts"] == []


def test_alerts_with_no_earlier_run_only_wait(fx):
    watches = [watch("w_rise", {"kind": "item", "item_id": RISING}, {"state_in": ["rising"]}, label="Rising"),
               watch("w_ratio", {"kind": "item", "item_id": HERITAGE}, {"ratio_over": 1}, label="Ratio"),
               watch("w_reach", {"kind": "hashtag", "value": "#fixture_za_step"}, {"reach_over": 1}, label="Reach"),
               watch("w_break", {"kind": "item", "item_id": STEP}, {"breakout": True}, label="Step breakout"),
               watch("w_tone", {"kind": "item", "item_id": STEP}, {"tone_flip": True}, label="Step tone"),
               watch("w_wait", {"kind": "item", "item_id": STEP}, {"creator_surge": True}, label="Step surge")]
    out = discover.build_alerts(fx, watches, date="2026-09-29")
    compare = "Waiting for a second detect run to compare against"
    assert out["alerts"] == []
    assert out["waiting"] == [{"watch_id": "w_rise", "label": "Rising", "waiting": compare},
                              {"watch_id": "w_ratio", "label": "Ratio", "waiting": compare},
                              {"watch_id": "w_reach", "label": "Reach", "waiting": compare},
                              {"watch_id": "w_break", "label": "Step breakout", "waiting": compare},
                              {"watch_id": "w_tone", "label": "Step tone", "waiting": compare},
                              {"watch_id": "w_wait", "label": "Step surge",
                               "waiting": "Waiting for creator views detection"}]


def test_alerts_brand_keyword_matches_the_item_aliases(fx):
    out = discover.build_alerts(fx, [watch("w_b", {"kind": "brand", "value": "fixture heritage"}, {"reach_over": 20})])
    assert [(a["item_id"], a["fired_because"]) for a in out["alerts"]] == [
        (HERITAGE, "Reach passed 20 creators in 3 days")]


def test_alerts_use_watch_matches_when_the_run_has_them(fx):
    store = Patched(watch_matches=lambda run: {"w_b": [RISING]})
    out = discover.build_alerts(store, [watch("w_b", {"kind": "brand", "value": "nothing matches this"},
                                              {"reach_over": 15})])
    assert [a["item_id"] for a in out["alerts"]] == [RISING]


def test_alerts_with_no_watches_and_before_any_detect_run(fx):
    assert discover.build_alerts(fx, []) == {"date": D30, "run_id": RUN, "alerts": [], "waiting": []}
    with pytest.raises(NotReady):
        discover.build_alerts(Patched(latest_detect_run=lambda until=None: None), [])


# ---------- worth-attention never reaches the app (contract.md section 10) ----------

WORTH = ("worth_pct", "worth_raw")


def worth_paths(value, path="$"):
    if isinstance(value, dict):
        for k, v in value.items():
            if k in WORTH:
                yield f"{path}.{k}"
            yield from worth_paths(v, f"{path}.{k}")
    elif isinstance(value, list):
        for n, v in enumerate(value):
            yield from worth_paths(v, f"{path}[{n}]")
    elif isinstance(value, str) and any(w in value for w in WORTH):
        yield path


def test_worth_walker_finds_a_planted_key():
    assert list(worth_paths({"a": [{"b": {"worth_pct": 0.9}}]})) == ["$.a[0].b.worth_pct"]
    assert list(worth_paths({"a": "worth_raw 0.7"})) == ["$.a"]


def test_worth_pct_and_worth_raw_never_reach_any_response():
    # Only the latest run has rows, so every watch below fires and every alert card is walked.
    fx = after_warmup(item_states=lambda run, market: [
        dict(r, worth_pct=0.8) for r in FixtureStore().item_states(run, market) if run["run_id"] == RUN])
    everything = FixtureStore().item_states({"run_id": RUN, "run_date": D30}, "all")
    watches = [watch(f"w_{n}", {"kind": "item", "item_id": r["item_id"]}, {"reach_over": 1}, market="all")
               for n, r in enumerate(everything)]
    responses = [discover.build_discover(fx, m) for m in ("ZA", "NG", "KE", "all")]
    responses += [discover.build_radar(fx, m) for m in ("ZA", "NG", "KE", "all")]
    responses += [discover.build_topic(fx, r["item_id"], r["market"]) for r in everything]
    responses.append(discover.build_alerts(fx, watches))
    assert len(responses[-1]["alerts"]) == len(everything)
    for body in responses:
        assert list(worth_paths(json.loads(json.dumps(body)))) == []


def test_lifecycle_rules_are_plain_words():
    # Each rule shows on the card under its state, so it avoids method words a client would not use.
    for _step, _word, rule in discover.LIFECYCLE.values():
        assert "ratio" not in rule.lower() and "significant" not in rule.lower(), rule


# ---------- Spread (L2's v_item_spread, BUILD.md 2.3 and 2.8) ----------

SPREAD_ROW = {"item_id": HERITAGE, "market": "ZA", "metric_date": D30,
              "tiers": {"nano": 3, "micro": 2, "mid": 1, "macro": 0, "mega": 0},
              "platform_first_seen": [{"platform": "tiktok", "first_seen": "2026-09-24"},
                                      {"platform": "twitter", "first_seen": "2026-09-26"},
                                      {"platform": "x", "first_seen": "2026-09-27"}],
              "spread_line": "First seen on TikTok 24 September, X 26 September"}
SPREAD_ROWS = [SPREAD_ROW,
               dict(SPREAD_ROW, metric_date="2026-09-29", spread_line="First seen on TikTok 24 September"),
               dict(SPREAD_ROW, market="NG", spread_line="First seen on TikTok 28 September")]


def spread_store(rows=SPREAD_ROWS):
    def item_spread(date, market, item_id=None):
        return [r for r in rows if r["metric_date"] == date and market in ("all", r["market"])
                and item_id in (None, r["item_id"])]
    return Patched(item_spread=item_spread)


def test_fixture_store_reads_spread_for_the_date_market_and_item(tmp_path):
    assert FixtureStore(root=tmp_path).item_spread(D30, "ZA", HERITAGE) is None
    (tmp_path / "item_spread.json").write_text(json.dumps(SPREAD_ROWS), encoding="utf-8")
    fx = FixtureStore(root=tmp_path)
    assert fx.item_spread(D30, "ZA", HERITAGE) == [SPREAD_ROW]
    assert fx.item_spread(D30, "ZA", STEP) == []
    assert fx.item_spread(D30, "all") == [SPREAD_ROWS[0], SPREAD_ROWS[2]]


def test_topic_spread_maps_the_view_to_the_contract_shape():
    out = discover.build_topic(spread_store(), HERITAGE, "ZA")
    spread = out["spread"]
    assert set(spread) == {"platforms", "tiers", "markets"}
    # X collected as twitter and as x is one platform at its earliest day.
    assert spread["platforms"] == [{"platform": "tiktok", "first_seen": "2026-09-24"},
                                   {"platform": "x", "first_seen": "2026-09-26"}]
    assert set(spread["tiers"]) == {"nano", "micro", "mid", "macro", "mega"}
    for tier, value in SPREAD_ROW["tiers"].items():
        f = spread["tiers"][tier]
        assert is_figure(f, value, "posts first seen in 7 days")
        assert f["query_id"] == "q_item_spread" and f["run_id"] == RUN
    assert spread["tiers"]["mega"]["value"] == 0      # a measured zero, not a gap
    # The item's rows in every market on the run date, read in the same query.
    assert [(m["market"], m["first_seen"]) for m in spread["markets"]] == [("ZA", "2026-09-24"), ("NG", "2026-09-24")]
    assert out["card"]["spread_line"] == "First seen on TikTok 24 September, X 26 September"


def test_topic_spread_is_null_without_a_row_for_the_item_on_the_run_date():
    assert discover.build_topic(spread_store(), RISING, "ZA")["spread"] is None
    assert discover.build_topic(spread_store([SPREAD_ROWS[1]]), HERITAGE, "ZA")["spread"] is None
    assert discover.build_topic(Patched(item_spread=lambda *a, **k: None), HERITAGE, "ZA")["spread"] is None


def test_topic_spread_tiers_the_view_leaves_out_stay_null():
    row = dict(SPREAD_ROW, tiers={"nano": 3, "micro": None}, platform_first_seen=[], spread_line=None)
    spread = discover.build_topic(spread_store([row]), HERITAGE, "ZA")["spread"]
    assert spread["platforms"] == []
    # A market with no platform sighting has no first-seen day, and a missing tier leaves its posts unknown.
    assert spread["markets"] == [{"market": "ZA", "first_seen": None, "posts": None}]
    assert is_figure(spread["tiers"]["nano"], 3)
    assert spread["tiers"]["micro"] is None and spread["tiers"]["mega"] is None


def test_discover_cards_carry_the_view_spread_line():
    items = {c["item_id"]: c for c in discover.build_discover(spread_store(), "ZA")["items"]}
    assert items[HERITAGE]["spread_line"] == "First seen on TikTok 24 September, X 26 September"
    assert items[STEP]["spread_line"] is None
    ng = {c["item_id"]: c for c in discover.build_discover(spread_store(), "all")["items"]}
    assert ng[HERITAGE]["spread_line"] == "First seen on TikTok 24 September, X 26 September"


def test_discover_cards_without_the_spread_view_keep_the_state_column():
    def item_states(run, market):
        return [dict(r, spread_line="From item_state") for r in FixtureStore().item_states(run, market)]
    store = Patched(item_states=item_states, item_spread=lambda *a, **k: None)
    assert {c["spread_line"] for c in discover.build_discover(store, "ZA")["items"]} == {"From item_state"}


def test_bigquery_item_spread_reads_the_view_by_date_market_and_item():
    import datetime as dt

    catalog = CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_spread", "what": "table"}]
    client = FakeClient(catalog, rows=[dict(SPREAD_ROW, metric_date=dt.date(2026, 9, 30), platform_first_seen=[
        {"platform": "tiktok", "first_seen": dt.date(2026, 9, 24)}])])
    (row,) = BigQueryStore(client=client).item_spread(D30, "ZA", HERITAGE)
    assert row["metric_date"] == D30 and row["platform_first_seen"] == [{"platform": "tiktok",
                                                                         "first_seen": "2026-09-24"}]
    sql, cfg = next((s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "`ogilvy-trends-v2.intelligence_42_core.v_item_spread`" in sql
    assert "s.metric_date = @d" in sql and HERITAGE not in sql and D30 not in sql
    assert {p.name for p in cfg.query_parameters} == {"d", "market", "item_id"}
    assert cfg.maximum_bytes_billed == 2_000_000_000


def test_bigquery_item_spread_is_none_without_the_view_and_never_queries_it():
    client = FakeClient(CATALOG_FULL)
    assert BigQueryStore(client=client).item_spread(D30, "ZA", HERITAGE) is None
    assert BigQueryStore(client=client).item_spread(D30, "all") is None
    assert not any("v_item_spread" in s for s, _ in client.calls)


def test_topic_spread_names_no_person():
    out = discover.build_topic(spread_store(), HERITAGE, "ZA")
    text = json.dumps(out["spread"])
    for p in out["spread"]["platforms"]:
        assert set(p) == {"platform", "first_seen"}
    assert "@" not in text and "handle" not in text and "creator_id" not in text


def test_discover_cards_keep_the_state_column_for_items_the_view_has_no_row_for():
    # The view holds only items with a measured post in its 7 days; the others keep item_state's line.
    def item_states(run, market):
        return [dict(r, spread_line="From item_state") for r in FixtureStore().item_states(run, market)]
    store = Patched(item_states=item_states, item_spread=spread_store().item_spread)
    items = {c["item_id"]: c for c in discover.build_discover(store, "ZA")["items"]}
    assert items[HERITAGE]["spread_line"] == "First seen on TikTok 24 September, X 26 September"
    assert items[STEP]["spread_line"] == "From item_state"


def test_bigquery_item_spread_that_fails_to_read_is_none_and_logged(capsys):
    class Failing(FakeClient):
        def query(self, sql, job_config=None):
            if "v_item_spread" in sql and "INFORMATION_SCHEMA" not in sql:
                raise RuntimeError("Query exceeded limit for bytes billed")
            return super().query(sql, job_config)
    catalog = CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_spread", "what": "table"}]
    assert BigQueryStore(client=Failing(catalog)).item_spread(D30, "ZA", HERITAGE) is None
    assert "v_item_spread read failed: RuntimeError" in capsys.readouterr().err


# ---------- Cross-market spread (v_item_spread across markets, BUILD.md 2.3) ----------

def test_topic_spread_lists_each_market_the_view_has_a_row_for_from_one_read():
    reads = []

    def item_spread(date, market, item_id=None):
        reads.append((date, market, item_id))
        return spread_store().item_spread(date, market, item_id)
    out = discover.build_topic(Patched(item_spread=item_spread), HERITAGE, "ZA")
    # The cards read the page's market for every item; the spread block reads this item in every market once.
    assert [r for r in reads if r[2] is not None] == [(D30, "all", HERITAGE)]
    markets = out["spread"]["markets"]
    assert [m["market"] for m in markets] == ["ZA", "NG"]
    assert all(set(m) == {"market", "first_seen", "posts"} for m in markets)
    # First seen is the market's earliest platform sighting; posts is its tier counts summed.
    assert markets[0]["first_seen"] == "2026-09-24"
    assert is_figure(markets[0]["posts"], 6, "posts first seen in 7 days")
    assert markets[0]["posts"]["query_id"] == "q_item_spread" and markets[0]["posts"]["run_id"] == RUN


def test_topic_spread_markets_never_invent_a_market_and_order_by_first_seen():
    ke = dict(SPREAD_ROW, market="KE", platform_first_seen=[{"platform": "tiktok", "first_seen": "2026-09-20"}])
    empty = dict(SPREAD_ROW, market="NG", platform_first_seen=[], tiers={"nano": 1})
    markets = discover.build_topic(spread_store([SPREAD_ROW, ke, empty]), HERITAGE, "ZA")["spread"]["markets"]
    assert [(m["market"], m["first_seen"]) for m in markets] == [("KE", "2026-09-20"), ("ZA", "2026-09-24"),
                                                                 ("NG", None)]
    only = discover.build_topic(spread_store([SPREAD_ROW]), HERITAGE, "ZA")["spread"]["markets"]
    assert [m["market"] for m in only] == ["ZA"]


# ---------- Origin and news (v_item_origin, v_news_followthrough, BUILD.md 2.5) ----------

ORIGIN_ROW = {"item_id": HERITAGE, "market": "ZA", "origin": None, "first_measured": "2026-09-24",
              "after_collection_began": True, "first_state_day": "2026-09-27", "lead_news_day": None,
              "lag_days": None}
NEWS_SEED = iid("topic", "heritage day braai")
NEWS_ROWS = [
    {"market": "ZA", "news_day": None, "news_day_from": None, "seed_date": "2026-09-22", "item_id": NEWS_SEED,
     "label": "heritage day braai", "kind": "topic", "lane": "expansion", "rise_score": 0.8,
     "matched_items": [{"item_id": HERITAGE, "label": "#fixture_za_heritage", "matched_by": "hashtag_key"},
                       {"item_id": NEWS_SEED, "label": "heritage day braai", "matched_by": "seed"}],
     "collect_ran": True, "collect_runs": 2, "yield_posts": 14, "yield_new_creators": 5, "reached_state": True,
     "states_7d": ["emerging"], "first_state_day": "2026-09-27", "first_measured": "2026-09-24", "lag_days": None},
    {"market": "ZA", "news_day": "2026-09-25", "news_day_from": "gdelt", "seed_date": "2026-09-26",
     "item_id": HERITAGE, "label": "#fixture_za_heritage", "kind": "hashtag", "lane": "exploration",
     "rise_score": 0.4, "matched_items": [{"item_id": HERITAGE, "label": "#fixture_za_heritage",
                                           "matched_by": "seed"}],
     "collect_ran": False, "collect_runs": 0, "yield_posts": None, "yield_new_creators": None,
     "reached_state": False, "states_7d": [], "first_state_day": None, "first_measured": "2026-09-24",
     "lag_days": 1},
]
NEWS_KEYS = {"seed_date", "news_day", "news_day_from", "title", "matched_by", "collect_ran", "reached_state",
             "first_state_day", "first_measured", "lag_days"}


def origin_store(origin=(ORIGIN_ROW,), news=NEWS_ROWS, **more):
    def item_origin(item_id, market):
        return None if origin is None else [r for r in origin if r["item_id"] == item_id and r["market"] == market]

    def news_followthrough(item_id, market, until):
        return None if news is None else [
            r for r in news if r["market"] == market and r["seed_date"] <= until
            and (r["item_id"] == item_id or any(m["item_id"] == item_id for m in r["matched_items"]))]
    return Patched(item_origin=item_origin, news_followthrough=news_followthrough, **more)


def test_fixture_store_reads_origin_and_news_for_the_item_and_market(tmp_path):
    fx = FixtureStore(root=tmp_path)
    assert fx.item_origin(HERITAGE, "ZA") is None and fx.news_followthrough(HERITAGE, "ZA", D30) is None
    (tmp_path / "item_origin.json").write_text(json.dumps([ORIGIN_ROW, dict(ORIGIN_ROW, market="NG")]),
                                               encoding="utf-8")
    news = NEWS_ROWS + [dict(NEWS_ROWS[0], market="NG"), dict(NEWS_ROWS[0], seed_date="2026-10-01")]
    (tmp_path / "news_followthrough.json").write_text(json.dumps(news), encoding="utf-8")
    fx = FixtureStore(root=tmp_path)
    assert fx.item_origin(HERITAGE, "ZA") == [ORIGIN_ROW]
    assert fx.item_origin(STEP, "ZA") == []
    # A row counts when the item is the seed's own item or one the bridge matched; never after the run date.
    assert fx.news_followthrough(HERITAGE, "ZA", D30) == NEWS_ROWS
    assert fx.news_followthrough(NEWS_SEED, "ZA", D30) == [NEWS_ROWS[0]]
    assert fx.news_followthrough(STEP, "ZA", D30) == []


def test_topic_origin_maps_the_view_row():
    out = discover.build_topic(origin_store(), HERITAGE, "ZA")
    assert out["origin"] == {"origin": None, "market": "ZA", "first_measured": "2026-09-24",
                             "after_collection_began": True, "first_state_day": "2026-09-27",
                             "lead_news_day": None, "lag_days": None}
    led = dict(ORIGIN_ROW, origin="news_led", lead_news_day="2026-09-23", lag_days=1)
    out = discover.build_topic(origin_store(origin=[led]), HERITAGE, "ZA")
    assert out["origin"]["origin"] == "news_led" and out["origin"]["lag_days"] == 1
    assert out["origin"]["lead_news_day"] == "2026-09-23"


def test_topic_origin_is_null_without_a_row_or_the_view():
    assert discover.build_topic(origin_store(origin=[]), HERITAGE, "ZA")["origin"] is None
    assert discover.build_topic(origin_store(origin=None), HERITAGE, "ZA")["origin"] is None
    assert discover.build_topic(origin_store(), STEP, "ZA")["origin"] is None
    assert discover.build_topic(FixtureStore(), HERITAGE, "ZA")["origin"] is None


def test_topic_news_lists_the_stories_newest_first_with_dates_and_lag():
    news = discover.build_topic(origin_store(), HERITAGE, "ZA")["news"]
    assert [n["seed_date"] for n in news] == ["2026-09-26", "2026-09-22"]
    assert all(set(n) == NEWS_KEYS for n in news)
    assert news[0] == {"seed_date": "2026-09-26", "news_day": "2026-09-25", "news_day_from": "gdelt",
                       "title": "#fixture_za_heritage", "matched_by": "seed", "collect_ran": False,
                       "reached_state": False, "first_state_day": None, "first_measured": "2026-09-24",
                       "lag_days": 1}
    # How this topic matched the story, not how the story's own item did.
    assert news[1]["matched_by"] == "hashtag_key" and news[1]["title"] == "heritage day braai"
    assert news[1]["lag_days"] is None and news[1]["news_day"] is None


def test_topic_news_is_null_without_a_row_or_the_view():
    assert discover.build_topic(origin_store(news=[]), HERITAGE, "ZA")["news"] is None
    assert discover.build_topic(origin_store(news=None), HERITAGE, "ZA")["news"] is None
    assert discover.build_topic(origin_store(), STEP, "ZA")["news"] is None
    assert discover.build_topic(FixtureStore(), HERITAGE, "ZA")["news"] is None


def test_topic_origin_news_and_markets_name_no_person():
    def item_spread(date, market, item_id=None):
        return [dict(r, creator_id="c_za_9", handle="@fixture_za_9")
                for r in spread_store().item_spread(date, market, item_id)]
    origin = [dict(ORIGIN_ROW, creator_id="c_za_9", handle="@fixture_za_9")]
    news = [dict(r, creator_id="c_za_9", handle="@fixture_za_9") for r in NEWS_ROWS]
    out = discover.build_topic(origin_store(origin=origin, news=news, item_spread=item_spread), HERITAGE, "ZA")
    text = json.dumps([out["origin"], out["news"], out["spread"]["markets"]])
    assert "fixture_za_9" not in text and "c_za_9" not in text
    assert "handle" not in text and "creator_id" not in text


def test_bigquery_origin_and_news_read_their_views_parameterised_and_capped():
    import datetime as dt

    catalog = CATALOG_FULL + [{"ds": "intelligence_42_agent", "n": n, "what": "table"}
                              for n in ("v_item_origin", "v_news_followthrough")]
    client = FakeClient(catalog, rows=[dict(ORIGIN_ROW, first_measured=dt.date(2026, 9, 24),
                                            first_state_day=dt.date(2026, 9, 27))])
    bq = BigQueryStore(client=client)
    (row,) = bq.item_origin(HERITAGE, "ZA")
    assert row["first_measured"] == "2026-09-24" and row["first_state_day"] == "2026-09-27"
    client.rows = [dict(NEWS_ROWS[1], seed_date=dt.date(2026, 9, 26), news_day=dt.date(2026, 9, 25))]
    (story,) = bq.news_followthrough(HERITAGE, "ZA", D30)
    assert story["seed_date"] == "2026-09-26" and story["news_day"] == "2026-09-25"
    reads = [(s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s]
    (osql, ocfg), (nsql, ncfg) = reads
    assert "`ogilvy-trends-v2.intelligence_42_agent.v_item_origin`" in osql
    assert "`ogilvy-trends-v2.intelligence_42_agent.v_news_followthrough`" in nsql
    assert "UNNEST(n.matched_items)" in nsql and "n.seed_date <= @d" in nsql
    for sql in (osql, nsql):
        assert HERITAGE not in sql and "'ZA'" not in sql and D30 not in sql
    assert {p.name for p in ocfg.query_parameters} == {"item_id", "market"}
    assert {p.name for p in ncfg.query_parameters} == {"item_id", "market", "d"}
    assert ocfg.maximum_bytes_billed == ncfg.maximum_bytes_billed == 2_000_000_000


def test_bigquery_origin_and_news_are_none_without_their_views_and_never_query_them():
    client = FakeClient(CATALOG_FULL)
    bq = BigQueryStore(client=client)
    assert bq.item_origin(HERITAGE, "ZA") is None and bq.news_followthrough(HERITAGE, "ZA", D30) is None
    assert not any("v_item_origin" in s or "v_news_followthrough" in s for s, _ in client.calls)


@pytest.mark.parametrize("view, read", [("v_item_origin", lambda bq: bq.item_origin(HERITAGE, "ZA")),
                                        ("v_news_followthrough",
                                         lambda bq: bq.news_followthrough(HERITAGE, "ZA", D30))])
def test_bigquery_origin_and_news_that_fail_to_read_are_none_and_logged(capsys, view, read):
    class Failing(FakeClient):
        def query(self, sql, job_config=None):
            if view in sql and "INFORMATION_SCHEMA" not in sql:
                raise RuntimeError("Query exceeded limit for bytes billed")
            return super().query(sql, job_config)
    catalog = CATALOG_FULL + [{"ds": "intelligence_42_agent", "n": view, "what": "table"}]
    assert read(BigQueryStore(client=Failing(catalog))) is None
    assert f"{view} read failed: RuntimeError" in capsys.readouterr().err


REACH_CATALOG = CATALOG_FULL + [{"ds": "intelligence_42_core", "n": n, "what": "table"}
                                for n in ("post_observations", "post_items", "posts", "creators")]


def test_bigquery_item_reach_is_parameterised_over_7_days_and_every_lane():
    client = FakeClient(REACH_CATALOG, rows=[{"item_id": STEP, "posts7": 40}])
    assert BigQueryStore(client=client).item_reach(D30, "ZA", [STEP, HERITAGE]) == [{"item_id": STEP, "posts7": 40}]
    sql, cfg = next((s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "INTERVAL 27 DAY" in sql and "HAVING MIN(po.observed_date) > DATE_SUB(@d, INTERVAL 7 DAY)" in sql
    assert "po.market = @market" in sql and "lane_class != 'legacy'" in sql
    assert "MAX(IFNULL(c.coord_score, 0)) >= 1 flagged" in sql and "IF(j.flagged, NULL, j.creator_id)" in sql
    assert "'placebo', 'agent_live'" in sql  # every other lane counts; rank and panel only split measured out
    assert STEP not in sql and D30 not in sql and "'ZA'" not in sql
    assert {p.name for p in cfg.query_parameters} == {"d", "market", "item_ids"}
    assert cfg.maximum_bytes_billed == 2_000_000_000


def test_bigquery_item_reach_and_names_are_none_without_tables_or_on_failure():
    client = FakeClient(CATALOG_FULL)
    assert BigQueryStore(client=client).item_reach(D30, "ZA", [STEP]) is None
    assert BigQueryStore(client=client).creator_names({"reddit:t2_x"}) is None
    assert not any("post_observations" in s or "creators" in s for s, _ in client.calls)

    class Failing(FakeClient):
        def query(self, sql, job_config=None):
            if "INFORMATION_SCHEMA" in sql:
                return super().query(sql, job_config)
            raise RuntimeError("boom")
    store_mod._CATALOG.clear()
    assert BigQueryStore(client=Failing(REACH_CATALOG)).item_reach(D30, "ZA", [STEP]) is None
    assert BigQueryStore(client=Failing(REACH_CATALOG)).creator_names({"reddit:t2_x"}) is None


def test_bigquery_creator_names_match_the_folded_item_key():
    client = FakeClient(REACH_CATALOG, rows=[{"key": "youtube:ucabc", "creator_id": "UCabc", "platform": "youtube",
                                              "handle": None, "display_name": "Mzansi Kitchen"}])
    assert BigQueryStore(client=client).creator_names({"youtube:ucabc"})["youtube:ucabc"]["display_name"] == \
        "Mzansi Kitchen"
    sql, cfg = next((s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "LOWER(TRIM(c.creator_id))" in sql and "ucabc" not in sql
    assert {p.name for p in cfg.query_parameters} == {"keys"}


def test_bigquery_health_days_read_baseline_lanes_by_platform():
    catalog = CATALOG_FULL
    client = FakeClient(catalog, rows=[{"platform": "youtube", "days": 7, "days_ok": 6}])
    assert BigQueryStore(client=client).health_days("ZA", "2026-09-24", D30) == [
        {"platform": "youtube", "days": 7, "days_ok": 6}]
    sql, cfg = next((s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "v_collection_health_current" in sql and "'unbiased_rank', 'panel', 'unbiased_counter'" in sql
    assert {p.name for p in cfg.query_parameters} == {"market", "start", "end"}


def test_bigquery_item_reach_counts_located_and_local_by_the_market_scope_rule():
    client = FakeClient(REACH_CATALOG, rows=[])
    BigQueryStore(client=client).item_reach(D30, "ZA", [STEP])
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "geo_source IN ('ext_region', 'home_market', 'place_mention')" in sql
    assert "COUNTIF(j.known OR j.own_feed) located7" in sql and "COUNTIF(j.in_market OR j.own_feed) local7" in sql


def test_an_item_the_gate_admitted_on_the_run_date_is_not_held_by_an_older_hold_or_a_blank_row(fx):
    """v_item_gate_current carries every brief date and the admitted ('today', 'moments') rows with no
    reason as well as the held ones. Only the latest brief on or before the run date may hold an item; an
    admitted row never reads as a hold with a blank reason, and an older day's hold never overrides it."""
    run_date = str(discover._run(fx)["run_date"])
    real_gate = fx.item_gate

    def item_gate(market, run_date):
        rows = [dict(r, brief_date=run_date, place="held_back") for r in real_gate(market, run_date) or []]
        if market in ("ZA", "all"):
            rows += [{"item_id": STEP, "market": "ZA", "brief_date": "2026-09-01", "place": "held_back",
                      "rule": "G1", "reason": "data_issue", "reason_text": None},
                     {"item_id": STEP, "market": "ZA", "brief_date": run_date, "place": "today",
                      "rule": None, "reason": None, "reason_text": None}]
        return rows

    out = discover.build_discover(Patched(item_gate=item_gate), "ZA")
    held = {h["item_id"] for h in out["held_back"]["items"]}
    assert STEP not in held
    assert OUTAGE in held
    assert all(h["reason_text"] for h in out["held_back"]["items"])


# Visual QA, 5 October 2026 (DT02): one Instagram post read under two record ids showed as two identical cards.
def test_topic_evidence_shows_each_linked_post_once():
    base = FixtureStore()

    def twice(*a):
        rows = base.item_evidence(*a)
        return rows[:1] + [dict(rows[0], post_id=(rows[0].get("post_id") or rows[0].get("id")) + "_again")] + rows[1:]

    once = discover.build_topic(base, HERITAGE, "ZA")["evidence"]
    out = discover.build_topic(Patched(item_evidence=twice), HERITAGE, "ZA")["evidence"]
    assert [e["id"] for e in out] == [e["id"] for e in once]
    assert len({e["url"] for e in out}) == len(out)


def test_discover_cards_carry_the_7_day_reach_from_every_lane_beside_the_3_day_reach(fx):
    # Owner, 5 October 2026: "1 creator in 3 days" read as rookie numbers. The 3-day reach counts only the boards and
    # followed accounts the checks use; each card now also carries Radar's 7-day creators from every lane.
    out = discover.build_discover(fx, "ZA")
    cards = {c["item_id"]: c for c in out["items"]}
    radar = {p["item_id"]: p["measures"] for p in discover.build_radar(fx, "ZA")["points"]}
    assert is_figure(cards[STEP]["reach7"], 3, "creators in 7 days")
    assert cards[STEP]["reach7"] == radar[STEP]["creators7"]
    assert cards[STEP]["reach"]["unit"] == "creators in 3 days"  # the 3-day figure stays as it was
    assert is_figure(cards[BOARD]["reach7"], 0)  # the read ran and found no post: a measured 0


def test_discover_7_day_reach_leaves_order_and_held_back_alone(fx):
    before = discover.build_discover(Patched(item_reach=lambda date, market, item_ids: None), "ZA")
    after = discover.build_discover(fx, "ZA")
    assert ids(before["items"]) == ids(after["items"])
    assert [c["order"] for c in before["items"]] == [c["order"] for c in after["items"]]
    assert before["held_back"]["count"] == after["held_back"]["count"]
    assert all(c["reach7"] is None for c in before["items"])  # no read: no figure, never a 0
    for b, a in zip(before["items"], after["items"]):
        assert b["reach"] == a["reach"]


def test_discover_7_day_reach_reads_each_market_of_the_page_once():
    seen = []
    store = Patched(item_reach=lambda date, market, item_ids: seen.append((date, market, sorted(item_ids))) or [])
    out = discover.build_discover(store, "all", limit=3)
    markets = sorted({c["market"] for c in out["items"]})
    assert [m for _, m, _ in seen] == markets
    for _, m, item_ids in seen:
        assert item_ids == sorted(c["item_id"] for c in out["items"] if c["market"] == m)


@pytest.mark.parametrize("brief_case, expected", [
    ("matching", "A checked story title"),
    ("missing", "Step watch"),
    ("blank", "Step watch"),
    ("stale", "Step watch"),
    ("wrong_market", "Step watch"),
    ("wrong_item", "Step watch"),
    ("unchecked", "Step watch"),
])
def test_alert_titles_use_only_the_checked_brief_for_the_detect_date_and_market(brief_case, expected):
    rows = json.loads(json.dumps(FixtureStore().briefs(D30)))
    row = next(r for r in rows if r["market"] == "ZA")
    written = next(c for c in row["payload"]["cards"] if c["item_id"] == STEP)
    written.update(title_written="  A checked   story title  ", explained=True, explanation_status="explained")
    if brief_case == "missing":
        written.pop("title_written")
    elif brief_case == "blank":
        written["title_written"] = " \t\n"
    elif brief_case == "stale":
        row["brief_date"] = "2026-09-29"
    elif brief_case == "wrong_market":
        row["market"] = "NG"
    elif brief_case == "wrong_item":
        written["item_id"] = "another-item"
    elif brief_case == "unchecked":
        written.update(explained=False, explanation_status="failed_checks")
    dates = []

    def briefs(date):
        dates.append(date)
        return rows

    watches = [watch("w_step", {"kind": "item", "item_id": STEP}, {"state_in": ["emerging"]}, label="Step watch")]
    before = json.dumps(watches)
    out = discover.build_alerts(Patched(briefs=briefs), watches, date=D30)
    (result,) = out["alerts"]
    assert result["label"] == expected
    assert result["card"]["title"] != "A checked story title"
    assert dates == [D30]
    assert json.dumps(watches) == before


class RollbackClient(FakeClient):
    """A catalog that still lists the news column (read before detect's older code re-created the view without
    it), then a view that no longer has it: the first read of item_states fails the way BigQuery does."""

    def __init__(self, stale, fresh, rows):
        super().__init__(stale, rows)
        self.stale, self.fresh, self.failures = stale, fresh, 0

    def query(self, sql, job_config=None):
        if "INFORMATION_SCHEMA" in sql:
            self.catalog = self.stale if not self.calls else self.fresh
        elif "t.market_news_posts7" in sql:
            self.calls.append((sql, job_config))
            self.failures += 1
            raise RuntimeError("400 Name market_news_posts7 not found inside t at [1:300]")
        return super().query(sql, job_config)


def test_item_states_drops_the_catalog_and_retries_once_when_the_news_column_is_gone():
    view = {"ds": "intelligence_42_core", "n": "v_item_market_scope", "what": "table"}
    stale = CATALOG_FULL + [view, {"ds": "intelligence_42_core", "n": "market_news_posts7", "what": "scope_column"}]
    client = RollbackClient(stale, CATALOG_FULL + [view], rows=[news_scope_row(7, None)])

    (row,) = BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")

    reads = [s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s]
    assert client.failures == 1 and len(reads) == 2
    assert "t.market_news_posts7" in reads[0] and "t.market_news_posts7" not in reads[1]
    assert "CAST(NULL AS INT64) AS market_news_posts7" in reads[1]
    assert row["market_news_posts7"] is None


def test_item_states_does_not_retry_an_unrelated_error():
    class Broken(FakeClient):
        def query(self, sql, job_config=None):
            if "INFORMATION_SCHEMA" not in sql:
                self.calls.append((sql, job_config))
                raise RuntimeError("403 Access Denied")
            return super().query(sql, job_config)

    client = Broken(CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_market_scope", "what": "table"}])
    with pytest.raises(RuntimeError, match="Access Denied"):
        BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert len([s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s]) == 1


def test_item_states_does_not_retry_an_unrecognised_name_that_is_not_the_news_column():
    class Other(FakeClient):
        def query(self, sql, job_config=None):
            if "INFORMATION_SCHEMA" not in sql:
                self.calls.append((sql, job_config))
                raise RuntimeError("400 Unrecognized name: sponsored_share at [1:9]")
            return super().query(sql, job_config)

    client = Other(CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_market_scope", "what": "table"}])
    with pytest.raises(RuntimeError, match="sponsored_share"):
        BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert len([s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s]) == 1


def test_item_states_does_not_retry_a_not_found_inside_error_for_another_name():
    class Other(FakeClient):
        def query(self, sql, job_config=None):
            if "INFORMATION_SCHEMA" not in sql:
                self.calls.append((sql, job_config))
                raise RuntimeError("400 Name sponsored_share not found inside t at [1:9]")
            return super().query(sql, job_config)

    client = Other(CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_market_scope", "what": "table"}])
    with pytest.raises(RuntimeError, match="sponsored_share"):
        BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert len([s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s]) == 1


def test_item_states_does_not_retry_another_error_that_mentions_the_news_column():
    class Quota(FakeClient):
        def query(self, sql, job_config=None):
            if "INFORMATION_SCHEMA" not in sql:
                self.calls.append((sql, job_config))
                raise RuntimeError("403 Quota exceeded while reading market_news_posts7")
            return super().query(sql, job_config)

    client = Quota(CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_market_scope", "what": "table"}])
    with pytest.raises(RuntimeError, match="Quota"):
        BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert len([s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s]) == 1


def test_item_states_retries_only_once_when_the_error_persists():
    class Still(FakeClient):
        def query(self, sql, job_config=None):
            if "INFORMATION_SCHEMA" not in sql:
                self.calls.append((sql, job_config))
                raise RuntimeError("400 Unrecognized name: market_news_posts7 at [1:9]")
            return super().query(sql, job_config)

    stale = CATALOG_FULL + [{"ds": "intelligence_42_core", "n": "v_item_market_scope", "what": "table"},
                            {"ds": "intelligence_42_core", "n": "market_news_posts7", "what": "scope_column"}]
    client = Still(stale)
    with pytest.raises(RuntimeError, match="Unrecognized name"):
        BigQueryStore(client=client).item_states({"run_id": RUN, "run_date": D30}, "ZA")
    assert len([s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s]) == 2
# W8-DEC-17 on Discover: the flag reads the row's counts through locality.geo_status, as the brief card does, not the
# raw geo_status item_state wrote.

def _with_counts(item_id, **fields):
    def item_states(run, market):
        return [dict(r, **fields) if r["item_id"] == item_id else r for r in FixtureStore().item_states(run, market)]
    return item_states


def test_an_unbriefed_item_whose_counts_do_not_confirm_local_is_market_unconfirmed():
    counts = dict(geo_status="local", local_share=5 / 8, geo_known_posts7=8, authenticity="clear",
                  sponsored_share=0)
    out = discover.build_discover(Patched(item_states=_with_counts(RISING, **counts)), "ZA")
    card = next(c for c in out["items"] if c["item_id"] == RISING)
    assert card["flag"] == "market_unconfirmed" and card["flag_word"] == "Market unconfirmed"


def test_counts_that_confirm_local_leave_the_flag_clear():
    counts = dict(geo_status="local", local_share=1.0, geo_known_posts7=30, authenticity="clear", sponsored_share=0)
    out = discover.build_discover(Patched(item_states=_with_counts(RISING, **counts)), "ZA")
    card = next(c for c in out["items"] if c["item_id"] == RISING)
    assert card["flag"] is None and card["flag_word"] is None


def test_not_assessed_does_not_override_the_briefs_market_unconfirmed():
    counts = dict(geo_status="local", local_share=5 / 8, geo_known_posts7=8, authenticity="not_assessed",
                  sponsored_share=0)
    briefs = json.loads(json.dumps(FixtureStore().briefs(D30)))
    step_brief = next(c for b in briefs for c in b["payload"].get("cards", []) if c["item_id"] == STEP)
    step_brief["flag"] = "market_unconfirmed"
    store = Patched(item_states=_with_counts(STEP, **counts), briefs=lambda date: briefs if date == D30 else [])
    card = next(c for c in discover.build_discover(store, "ZA")["items"] if c["item_id"] == STEP)
    assert card["flag"] == "market_unconfirmed"
def _g10_hold_store(fx, held_item=None):
    """A store where the gate view holds RISING under G10 and, when held_item is given, the ZA brief names it in
    held_back.items with those fields (core/brief/payload.py _held_item writes the item that way)."""
    run_date = str(discover._run(fx)["run_date"])
    real_gate, real_briefs = fx.item_gate, fx.briefs

    def item_gate(market, run_date):
        rows = list(real_gate(market, run_date) or [])
        if market in ("ZA", "all"):
            rows.append({"item_id": RISING, "market": "ZA", "brief_date": run_date, "place": "held_back",
                         "rule": "G10", "reason": "explanation_failed", "reason_text": "Explanation failed its checks"})
        return rows

    def briefs(date):
        rows = json.loads(json.dumps(real_briefs(date)))
        if held_item is not None:
            for row in rows:
                if row["market"] == "ZA":
                    block = row["payload"].setdefault("held_back", {"count": 0, "text": "", "items": []})
                    block["items"] = [i for i in block["items"] if i["item_id"] != RISING] + [
                        dict({"item_id": RISING, "title": "Rising topic", "rule": "G10", "reason": "explanation_failed",
                              "reason_text": "Explanation failed its checks", "evidence_ids": [], "evidence": [],
                              "numbers": [], "count_line": None}, **held_item)]
        return rows

    return Patched(item_gate=item_gate, briefs=briefs)


def _g10_hold_words(fx, held_item=None):
    out = discover.build_discover(_g10_hold_store(fx, held_item), "ZA")
    return next(h for h in out["held_back"]["items"] if h["item_id"] == RISING)["reason_text"]


def test_a_g10_hold_whose_explanation_ran_and_failed_its_checks_says_so(fx):
    """N23, review of f9573c6: the held item in the brief carries the job's failed_reason when the explanation ran
    and failed. Discover found no card for it and used to word every such hold as never reached."""
    words = _g10_hold_words(fx, {"failed_reason": "A claim was not supported by its posts"})
    assert words == "The explanation did not pass our checks"


def test_a_g10_hold_on_a_topic_a_busy_model_left_unexplained_says_the_model_was_busy(fx):
    from core.brief.payload import NOT_RUN_REASONS
    assert _g10_hold_words(fx, {"failed_reason": NOT_RUN_REASONS[0]}) == "Not explained in time: the model was busy"


def test_a_g10_hold_on_a_topic_the_model_never_reached_says_so_when_the_brief_says_not_run(fx):
    assert _g10_hold_words(fx, {"explanation_status": "not_run", "failed_reason": None}) == (
        "Not explained: the model did not get to this topic")


@pytest.mark.parametrize("held_item", [None, {"failed_reason": None}, {"explanation_status": "explained"}])
def test_a_g10_hold_with_no_proof_that_the_model_skipped_it_keeps_the_checks_wording(fx, held_item):
    """A G10 hold also covers an explanation that ran and failed specificity (no failed_reason), and a hold the
    brief rows cannot be read for. Nothing there shows the model skipped the topic, so the a80 words stand."""
    assert _g10_hold_words(fx, held_item) == "The explanation did not pass our checks"


def test_a_hold_from_an_old_brief_does_not_stick_to_an_item_the_newest_brief_does_not_name(fx):
    """N42: the latest brief of a market decides its gate rows. An item that brief does not name keeps no old hold;
    it reads from item_state like any item the gate never saw."""
    run_date = str(discover._run(fx)["run_date"])

    def item_gate(market, run_date):
        if market not in ("ZA", "all"):
            return []
        return [{"item_id": STEP, "market": "ZA", "brief_date": "2026-09-01", "place": "held_back",
                 "rule": "G1", "reason": "data_issue", "reason_text": None},
                {"item_id": OTHER, "market": "ZA", "brief_date": run_date, "place": "today",
                 "rule": None, "reason": None, "reason_text": None}]

    out = discover.build_discover(Patched(item_gate=item_gate), "ZA")
    assert STEP not in {h["item_id"] for h in out["held_back"]["items"]}
    assert STEP in {c["item_id"] for c in out["items"]}


def test_a_hold_in_the_newest_brief_still_holds_and_other_markets_keep_their_own_latest_brief(fx):
    run_date = str(discover._run(fx)["run_date"])

    def item_gate(market, run_date):
        return [{"item_id": STEP, "market": "ZA", "brief_date": run_date, "place": "held_back",
                 "rule": "G1", "reason": "data_issue", "reason_text": None},
                {"item_id": OWAMBE, "market": "NG", "brief_date": "2026-09-01", "place": "held_back",
                 "rule": "G1", "reason": "data_issue", "reason_text": None}]

    held = {h["item_id"] for h in discover.build_discover(Patched(item_gate=item_gate), "all")["held_back"]["items"]}
    assert {STEP, OWAMBE} <= held


def test_bigquery_item_gate_reads_only_recent_brief_dates():
    client = FakeClient(CATALOG_FULL)
    BigQueryStore(client=client).item_gate("ZA", D30)
    sql = next(s for s, _ in client.calls if "v_item_gate_current" in s and "INFORMATION_SCHEMA" not in s)
    assert "g.brief_date >=" in sql


def test_bigquery_item_gate_reads_the_fourteen_days_up_to_the_run_date_not_up_to_today():
    """N42, review of f9573c6: a dated read more than 14 days back lost every brief-only hold because the window
    ended today. The window is the 14 days up to the date being read."""
    import datetime as dt
    client = FakeClient(CATALOG_FULL)
    BigQueryStore(client=client).item_gate("ZA", "2026-09-10")
    sql, cfg = next(c for c in client.calls if "v_item_gate_current" in c[0] and "INFORMATION_SCHEMA" not in c[0])
    assert "CURRENT_DATE" not in sql
    assert "g.brief_date >= DATE_SUB(@run_date, INTERVAL 14 DAY)" in sql and "g.brief_date <= @run_date" in sql
    assert {p.name: p.value for p in cfg.query_parameters}["run_date"] == dt.date(2026, 9, 10)


def test_discover_asks_the_gate_for_the_run_it_is_reading(fx):
    seen = []
    real = fx.item_gate

    def item_gate(market, run_date):
        seen.append((market, run_date))
        return real(market, run_date)

    discover.build_discover(Patched(item_gate=item_gate), "ZA")
    assert seen == [("ZA", D30)]


def test_a_dated_alerts_read_asks_the_gate_for_that_runs_date(fx):
    seen = []

    def item_gate(market, run_date):
        seen.append(run_date)
        return []

    discover.build_alerts(Patched(item_gate=item_gate), [], date=D30)
    assert seen == [D30]


def test_a_hold_is_worded_from_the_brief_card_when_the_brief_has_one():
    from core.api.held_words import plain_reason
    from core.brief.payload import NOT_RUN_REASONS
    busy = sorted(NOT_RUN_REASONS)[0]
    assert discover._hold_basis({"explanation_status": "not_run", "failed_reason": busy}, None) == ("not_run", busy)
    assert discover._hold_basis({"explanation_status": "failed_checks", "failed_reason": "x"}, {"explanation_status": "not_run"}) \
        == ("failed_checks", "x")  # the card is the brief's record of the item; the held item is only the fallback
    held = {"rule": "G10", "reason": "explanation_failed", "reason_text": "Explanation failed its checks"}
    for card, words in (({"explanation_status": "not_run", "failed_reason": busy}, "Not explained in time: the model was busy"),
                        ({"explanation_status": "not_run"}, "Not explained: the model did not get to this topic"),
                        ({"explanation_status": "failed_checks", "failed_reason": "x"}, "The explanation did not pass our checks")):
        status, failed = discover._hold_basis(card, None)
        assert plain_reason(dict(held, **({"failed_reason": failed} if failed else {})), status)["reason_text"] == words
