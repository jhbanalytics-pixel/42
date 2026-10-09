"""Creator pages and communities (contract.md section 12.1 to 12.3) over the fixture store, and the SQL they run.

The fixtures (core/api/fixtures/people_*.json) hold, in Nigeria over the 28 days to the 30 September detect run:
six creators who each post three clean items (one community: macro, mega, mid, micro on X, nano, and a
suppressed macro); five more who share two clean items plus a sensitive, a generic, a board and four rule-3 items
(never a community); and four who share three clean items of their own (a component under five, hidden).
"""
import hashlib
import json
import re
import shutil
import statistics

import pytest

from core.api import people
from core.api import store as store_mod
from core.api.store import FIXTURES, BigQueryStore, FixtureStore
from core.api.today import NotFound

A = "7f2ab79603408ca60b74cc340171539579ae87710894abaebf234f04648bc35d"  # #fixture_ng_owambe
B = "d446222d56050176e27da32b1f13586cb9ca5359066bacbee8b56e83a8522593"  # fixture ng peaking sound
J = "3dec136054c79e29232da399ebc4af80e35075ecaf6e22df8474359322ab172a"  # #fixture_ng_jollof, no NG card
C = "7bbb4f7e688b04a917d2b8e744210b7066714a7b925861e236fecd8a3dd4a60d"  # #fixture_ng_promo, paid-led (G5b gate)
BOT = "1fde20148f78cbd64749e8ad9f4ed50c5ab2843d53d5ad7d661e7a230554e5bf"  # likely_coordinated
SPON = "9db7e4c8c38424f764f42ba06b28f026cd48eb1ce09fcdced22f24e7cfd57717"  # paid-led
CHECK = "4b942b2ea393cbe2c2e4e883ed5be7441b361229e446c6c5bbcb7fdba540fa33"  # check_pattern
HELD = "196c5995cccbc7eff97f244b604378afbc28dc4109d809a6d6e3cf7a4201bd97"  # held back by the gate (G1)
S = "84206424f5061968e55478e6b502e1fcda82fd7c58905b5fc8c17992ecc8f39f"  # sensitive
G = "9217fef145bc52df6588503059242e1156a6ba49b7f63336a557499523e55858"  # platform-generic (#fyp)
D = "afe0117aa6b96951c93fbebd594da1eebc52301fe61e1178cfee9a515e3f42b9"  # on a platform's board
NEVER_NAMED = {S, BOT, SPON, CHECK, HELD, C}
NEVER_A_BASIS = NEVER_NAMED | {G, D}
COMMUNITY = {"c_ng_macro", "c_ng_mega", "c_ng_mid", "c_ng_micro", "c_ng_nano", "c_ng_hidden"}
M_GROUP = {f"c_ng_m{n}" for n in range(1, 6)}
K_GROUP = {f"c_ng_k{n}" for n in range(1, 5)}
RUN = "r_detect_20260930_01"
START, END = "2026-09-03", "2026-09-30"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def walk(obj, path=()):
    """Every (path, key, value) in a JSON body, so a rule can be checked everywhere at once."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from walk(v, path + (k,))
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            yield path, n, v
            yield from walk(v, path + (n,))


def strings(obj):
    return [v for _, _, v in walk(obj) if isinstance(v, str)]


def is_figure(f):
    assert set(f) == {"value", "unit", "query_id", "run_id", "result_hash"}
    assert f["query_id"].startswith("q_") and re.fullmatch(r"sha256:[0-9a-f]{64}", f["result_hash"])


@pytest.fixture
def fx():
    return FixtureStore()


def fixtures_without(tmp_path, *names):
    for f in FIXTURES.glob("*.json"):
        if f.stem not in names:
            shutil.copy(f, tmp_path / f.name)
    return FixtureStore(root=tmp_path)


@pytest.fixture
def no_sensitive_set(tmp_path):
    """The fixtures as they stand before L2 ships any sensitive set."""
    return fixtures_without(tmp_path, "people_sensitive", "people_sensitive_complete")


@pytest.fixture
def keyword_only(tmp_path):
    """Only v_sensitive_items exists: the keyword list, a floor and not the complete set."""
    return fixtures_without(tmp_path, "people_sensitive_complete")


def all_bodies(store):
    bodies = [people.build_communities(store, m) for m in ("ZA", "NG", "KE")]
    for c in bodies[1]["communities"]:
        bodies.append(people.build_community(store, c["community_id"]))
    for creator_id in ("c_ng_macro", "c_ng_mega", "c_ng_m1", "c_ng_k1"):
        bodies.append(people.build_creator(store, creator_id, "NG"))
    return bodies


# Rule 1: the page threshold.

@pytest.mark.parametrize("creator_id", ["c_ng_mid", "c_ng_micro", "c_ng_nano", "c_ng_m2", "c_ng_k2"])
def test_below_macro_is_404_with_the_exact_words(fx, creator_id):
    with pytest.raises(NotFound) as exc:
        people.build_creator(fx, creator_id, "NG")
    assert str(exc.value) == "42 shows creators of this size only in totals"


def test_unknown_and_suppressed_creators_are_not_found_without_naming_the_reason(fx):
    for creator_id in ("c_nobody", "c_ng_hidden"):
        with pytest.raises(NotFound) as exc:
            people.build_creator(fx, creator_id, "NG")
        assert str(exc.value) == "No creator with that id"


def test_page_tier_is_macro_and_above():
    assert people.PAGE_TIERS == ("macro", "mega")


def test_market_must_be_one_of_the_three(fx):
    for market in (None, "", "all", "XX"):
        with pytest.raises(people.BadRequest):
            people.build_creator(fx, "c_ng_macro", market)
        with pytest.raises(people.BadRequest):
            people.build_communities(fx, market)


# The creator page.

def test_creator_page_shape_and_numbers(fx):
    body = people.build_creator(fx, "c_ng_macro", "NG")
    creator = body["creator"]
    assert {k: creator[k] for k in ("creator_id", "platform", "handle", "tier", "home_market", "profile_url")} == {
        "creator_id": "c_ng_macro", "platform": "tiktok", "handle": "@fixture_ng_macro", "tier": "macro",
        "home_market": "NG", "profile_url": "https://tiktok.com/@fixture_ng_macro"}
    is_figure(creator["followers"])
    assert creator["followers"]["value"] == 750000
    # Ten of its posts were first sighted in Nigeria in the window; the July post re-sighted by the legacy
    # lane in September does not count.
    window = {p["post_id"]: p for p in load("people_posts") if p["creator_id"] == "c_ng_macro"
              and p["post_id"] != "pp_macro_7"}
    assert body["reach"]["posts_28d"]["value"] == len(window) == 10
    assert body["reach"]["median_views"]["value"] == statistics.median(p["views"] for p in window.values())
    for f in body["reach"].values():
        is_figure(f)
    assert body["window"] == {"from": START, "to": END, "days": 28}


def test_creator_items_are_clean_cards_with_post_counts(fx):
    body = people.build_creator(fx, "c_ng_macro", "NG")
    got = {i["card"]["item_id"]: i["posts"]["value"] for i in body["items"]}
    assert got == {A: 2, B: 2}  # jollof has no NG card; promo is paid-led
    assert [i["posts"]["value"] for i in body["items"]] == sorted(got.values(), reverse=True)
    for i in body["items"]:
        is_figure(i["posts"])
        assert i["card"]["market"] == "NG"


def test_recent_posts_newest_first_without_sensitive_held_or_flagged_items(fx):
    body = people.build_creator(fx, "c_ng_macro", "NG")
    # pp_macro_7 was first seen in July, so it is outside the 28 days even though legacy re-sighted it.
    assert [p["id"] for p in body["recent_posts"]] == ["pp_macro_1", "pp_macro_2", "pp_macro_3", "pp_macro_6"]
    first = body["recent_posts"][0]
    assert first["platform"] == "tiktok" and first["handle"] == "@fixture_ng_macro"
    assert first["engagement"] == {"views": 120000, "likes": 6000, "comments": 300, "shares": 120}
    assert first["flags"] == [] and first["creator_tier"] == "macro"


def test_sensitive_and_rule_3_items_never_appear_on_a_creator_page(fx):
    for creator_id in ("c_ng_macro", "c_ng_m1"):
        body = people.build_creator(fx, creator_id, "NG")
        text = json.dumps(body)
        for item_id in NEVER_NAMED:
            assert item_id not in text
        assert "Fixture election post" not in text and "fixture ng election topic" not in text
    m1 = people.build_creator(fx, "c_ng_m1", "NG")
    assert {i["card"]["item_id"] for i in m1["items"]} == {B}
    assert [p["id"] for p in m1["recent_posts"]] == ["pp_m1_2"]
    assert m1["community"] is None


def test_formats_from_enrichment_and_null_without_it(fx):
    body = people.build_creator(fx, "c_ng_macro", "NG")
    assert [(f["format"], f["posts"]["value"]) for f in body["formats"]] == [("duet", 2), ("stitch", 1)]
    assert people.build_creator(fx, "c_ng_mega", "NG")["formats"] is None


def test_creator_page_carries_its_community(fx):
    listed = people.build_communities(fx, "NG")["communities"]
    assert people.build_creator(fx, "c_ng_macro", "NG")["community"] == {
        "label": listed[0]["label"], "community_id": listed[0]["community_id"]}
    assert people.build_creator(fx, "c_ng_k1", "NG")["community"] is None  # its component is under five


@pytest.mark.parametrize("platform,handle,url", [
    ("tiktok", "@h", "https://tiktok.com/@h"), ("instagram", "h", "https://instagram.com/h"),
    ("youtube", "h", "https://youtube.com/@h"), ("x", "@h", "https://x.com/h"), ("twitter", "h", "https://x.com/h"),
    ("facebook", "h", "https://facebook.com/h"), ("reddit", "u/h", "https://reddit.com/user/h"),
    ("threads", "h", "https://threads.net/@h"), ("snapchat", "h", None), ("tiktok", None, None),
    ("tiktok", "", None)])
def test_profile_url_templates(platform, handle, url):
    assert people.profile_url(platform, handle) == url


def test_twitter_creators_show_as_x(fx):
    body = people.build_creator(fx, "c_ng_k1", "NG")
    assert body["creator"]["platform"] == "x" and body["creator"]["profile_url"] == "https://x.com/fixture_ng_k1"
    assert all(p["platform"] == "x" for p in body["recent_posts"])


# Rule 2 before L2's sensitive set exists.

def test_before_the_sensitive_set_no_item_list_and_no_community(no_sensitive_set):
    body = people.build_creator(no_sensitive_set, "c_ng_macro", "NG")
    assert body["items"] is None and body["community"] is None
    assert body["note"] == "Topics per creator appear once 42 can keep sensitive topics off named pages"
    listed = people.build_communities(no_sensitive_set, "NG")
    assert listed["note"] == "Topics per creator appear once 42 can keep sensitive topics off named pages"
    assert listed["communities"]
    for c in listed["communities"]:
        assert c["members"] is None and c["example_posts"] is None
        is_figure(c["creators"])
    text = json.dumps(listed)
    for handle in ("fixture_ng_macro", "fixture_ng_mega", "c_ng_macro", "c_ng_mega"):
        assert handle not in text


def test_before_the_sensitive_set_community_topic_cards_carry_no_named_evidence(no_sensitive_set, monkeypatch):
    """Contract 12.1: until the sensitive set is complete a community shows counts only, so its topic cards
    carry no evidence author or thumbnail, even from a page-tier creator who could be named elsewhere."""
    real_card = people._Page.card

    def named_card(self, item_id):
        card = real_card(self, item_id)
        if card is None:
            return None
        return dict(card, evidence=[{"id": "p_named", "platform": "tiktok", "handle": "fixture_ng_macro",
                                     "url": "https://www.tiktok.com/@fixture_ng_macro/video/1", "flags": []}],
                    thumbnails=["p_named"])

    monkeypatch.setattr(people._Page, "card", named_card)
    listed = people.build_communities(no_sensitive_set, "NG")
    assert listed["communities"] and any(c["top_items"] for c in listed["communities"])
    bodies = [listed] + [people.build_community(no_sensitive_set, c["community_id"]) for c in listed["communities"]]
    for body in bodies:
        assert "fixture_ng_macro" not in json.dumps(body)


def test_with_the_sensitive_set_the_page_carries_no_note(fx):
    body = people.build_creator(fx, "c_ng_macro", "NG")
    assert body["note"] is None and body["recent_posts_note"] is None


def test_before_the_sensitive_set_no_recent_posts(no_sensitive_set):
    body = people.build_creator(no_sensitive_set, "c_ng_macro", "NG")
    assert body["recent_posts"] is None
    assert body["recent_posts_note"] == "Recent posts appear once 42 can keep sensitive topics off named pages"
    assert "pp_macro_" not in json.dumps(body)


# Rule 2 with only the keyword list (v_sensitive_items): it excludes, but switches nothing on.

def test_keyword_list_alone_keeps_item_lists_recent_posts_and_members_off(keyword_only):
    assert keyword_only.sensitive_items() == {S} and keyword_only.sensitive_complete() is False
    for creator_id in ("c_ng_macro", "c_ng_mega", "c_ng_m1", "c_ng_k1"):
        body = people.build_creator(keyword_only, creator_id, "NG")
        assert body["items"] is None and body["community"] is None
        assert body["note"] == "Topics per creator appear once 42 can keep sensitive topics off named pages"
        assert body["recent_posts"] is None
        assert body["recent_posts_note"] == "Recent posts appear once 42 can keep sensitive topics off named pages"
    listed = people.build_communities(keyword_only, "NG")
    assert listed["note"] == "Topics per creator appear once 42 can keep sensitive topics off named pages"
    assert listed["communities"]
    for c in listed["communities"]:
        assert c["members"] is None and c["example_posts"] is None
    one = people.build_community(keyword_only, listed["communities"][0]["community_id"])
    assert one["community"]["members"] is None and one["community"]["example_posts"] is None
    text = json.dumps(listed)
    for handle in ("fixture_ng_macro", "fixture_ng_mega", "c_ng_macro", "c_ng_mega"):
        assert handle not in text


def test_keyword_list_alone_still_keeps_its_items_off_every_named_page(keyword_only):
    assert people.excluded_items(keyword_only, keyword_only.latest_detect_run(), "NG", START, END)["sensitive"] == {S}
    listed = people.build_communities(keyword_only, "NG")["communities"]
    # The sensitive item is what keeps the five apart, so one community stays, on the clean items only.
    assert [c["community_id"] for c in listed] == [expected_id("NG", [A, B, J])]
    assert listed[0]["creators"]["value"] == 6
    for c in listed:
        assert S not in {t["item_id"] for t in c["top_items"]}
        assert "fixture ng election topic" not in c["label"]
        for creator_id in M_GROUP:
            assert creator_id not in json.dumps(c)
    for body in all_bodies(keyword_only):
        text = json.dumps(body)
        assert S not in text and "fixture ng election topic" not in text and "Fixture election post" not in text


# Rule 2 once v_sensitive_items_complete exists: lists on, read from the complete set.

def test_complete_view_switches_lists_on_and_its_items_stay_off(tmp_path):
    store = fixtures_without(tmp_path, "people_sensitive")
    (tmp_path / "people_sensitive_complete.json").write_text(json.dumps(
        [{"item_id": S, "market": None, "category": "political"},
         {"item_id": B, "market": "NG", "category": "religion"}]), encoding="utf-8")
    assert store.sensitive_complete() is True and store.sensitive_items() == {S, B}
    body = people.build_creator(store, "c_ng_macro", "NG")
    assert body["note"] is None and body["recent_posts_note"] is None
    assert {i["card"]["item_id"] for i in body["items"]} == {A}
    carrying = {p["post_id"] for p in store.window_posts(["c_ng_macro"], "NG", START, END) if B in p["item_ids"]}
    assert carrying and body["recent_posts"]
    assert not carrying & {p["id"] for p in body["recent_posts"]}
    text = json.dumps(body)
    assert S not in text and B not in text and "fixture ng peaking sound" not in text
    listed = people.build_communities(store, "NG")
    assert listed["note"] is None
    for c in listed["communities"]:
        assert c["members"] is not None and c["example_posts"] is not None
        assert {S, B}.isdisjoint(t["item_id"] for t in c["top_items"])


def test_complete_view_in_the_fixtures_turns_the_lists_on(fx):
    assert fx.sensitive_complete() is True and fx.sensitive_items() == {S}
    body = people.build_creator(fx, "c_ng_macro", "NG")
    assert body["items"] is not None and body["recent_posts"] is not None and body["community"] is not None


# Communities.

def expected_id(market, item_ids):
    return hashlib.sha256((market + ":" + ",".join(sorted(item_ids))).encode()).hexdigest()


def test_one_community_by_shared_clean_items(fx):
    body = people.build_communities(fx, "NG")
    assert body["market"] == "NG" and body["method"] == "shared_items"
    assert body["interaction"] == "Grouped by shared topics; interaction not yet measured"
    assert body["window"] == {"from": START, "to": END, "days": 28}
    assert len(body["communities"]) == 1
    c = body["communities"][0]
    assert c["community_id"] == expected_id("NG", [A, B, J])
    assert c["creators"]["value"] == len(COMMUNITY) == 6
    is_figure(c["creators"])
    assert [m["creator_id"] for m in c["members"]] == ["c_ng_mega", "c_ng_macro"]  # page tier, not suppressed
    assert {m["profile_url"] for m in c["members"]} == {"https://instagram.com/fixture_ng_mega",
                                                         "https://tiktok.com/@fixture_ng_macro"}
    assert {t["item_id"] for t in c["top_items"]} == {A, B}  # jollof has no NG card to show
    assert c["label"] == "#fixture_ng_jollof, #fixture_ng_owambe, fixture ng peaking sound"  # tie: by item_id
    assert c["platforms"] == ["instagram", "tiktok", "x", "youtube"]
    assert [(lang["lang"], lang["posts"]["value"]) for lang in c["languages"]] == [("en", 2), ("pcm", 1), ("yo", 1)]
    handles = {p["handle"] for p in c["example_posts"]}
    assert handles <= {"@fixture_ng_macro", "fixture_ng_mega"} and len(c["example_posts"]) == 6
    assert c["method"] == "shared_items"


def test_components_under_five_and_groups_joined_only_by_excluded_items_are_not_shown(fx):
    body = people.build_communities(fx, "NG")
    text = json.dumps(body)
    for creator_id in M_GROUP | K_GROUP:
        assert creator_id not in text
    assert people.build_communities(fx, "ZA")["communities"] == []


def test_excluded_items_never_in_label_top_items_or_as_a_basis(fx):
    for body in all_bodies(fx):
        text = json.dumps(body)
        for item_id in NEVER_A_BASIS:
            assert item_id not in text
        for word in ("fixture ng election topic", "#fyp", "#fixture_ng_board"):
            assert word not in text


class Loosened(FixtureStore):
    """The fixtures with one exclusion switched off, to show that exclusion alone keeps the five apart."""

    def __init__(self, drop):
        super().__init__()
        self.drop = drop

    def sensitive_items(self):
        return set() if self.drop == "sensitive" else super().sensitive_items()

    def generic_items(self):
        return set() if self.drop == "generic" else super().generic_items()

    def board_items(self, market, start, end):
        return set() if self.drop == "board" else super().board_items(market, start, end)

    def item_states(self, run, market):
        rows = super().item_states(run, market)
        if self.drop != "rule_3":
            return rows
        return [dict(r, eligible=True, authenticity="clear", sponsored_share=0.0, geo_status="local",
                     map_status="active") for r in rows]

    def item_gate(self, market, run_date):
        return [] if self.drop == "rule_3" else super().item_gate(market, run_date)


@pytest.mark.parametrize("drop", ["sensitive", "generic", "board", "rule_3"])
def test_each_exclusion_is_what_keeps_the_five_apart(drop):
    members = set()
    for c in people.build_communities(Loosened(drop), "NG")["communities"]:
        members |= {m["creator_id"] for m in c["members"] or []}
    assert "c_ng_m1" in members


class BriefHeldEverything(FixtureStore):
    """The fixtures with the brief holding the community's shared items for a reason that is not about coordination
    or payment, as staging's briefs of 1 to 3 October 2026 held every item."""

    def item_gate(self, market, run_date):
        rows = super().item_gate(market, run_date) or []
        held = [{"item_id": i, "market": "NG", "rule": None, "reason": "explanation_failed"} for i in (A, B, J)]
        return rows + [r for r in held if market in ("all", "NG")]


def test_communities_empty_when_the_brief_holds_their_items_even_with_the_sensitive_set():
    """Staging, 3 October 2026: Communities read empty. The sensitive set only takes names off (members and example
    posts are null while it is incomplete, the list itself stays); rule 3 as contract section 12.3 words it leaves
    out every item held back in the latest detect run, whatever the reason, so a brief that holds everything leaves
    no item to join creators by."""
    store = BriefHeldEverything()
    assert store.sensitive_complete()
    body = people.build_communities(store, "NG")
    assert body["communities"] == [] and body["note"] is None
    assert people.build_communities(FixtureStore(), "NG")["communities"]


def test_one_community_by_id_in_any_market(fx):
    listed = people.build_communities(fx, "NG")["communities"][0]
    one = people.build_community(fx, listed["community_id"])
    assert one["community"] == listed and one["market"] == "NG"
    assert people.build_community(fx, listed["community_id"], "NG")["community"] == listed
    with pytest.raises(NotFound):
        people.build_community(fx, listed["community_id"], "ZA")
    with pytest.raises(NotFound):
        people.build_community(fx, "0" * 64)


# Rule 3: no coordination or payment beside a name.

class Flagged(FixtureStore):
    """Brief cards whose evidence carries flags, as a coordinated or check-pattern post would."""

    def briefs(self, date):
        rows = json.loads(json.dumps(super().briefs(date)))
        for r in rows:
            for c in (r["payload"].get("cards") or []) + (r["payload"].get("more") or []):
                for e in c.get("evidence") or []:
                    e["flags"] = ["likely_coordinated", "check_pattern"]
                    e.update(creator_tier="macro", platform="tiktok", handle="@fixture_ng_macro")
        return rows


def test_evidence_flags_are_dropped_everywhere_on_named_pages():
    store = Flagged()
    bodies = all_bodies(store)
    seen = 0
    for body in bodies:
        for _, key, value in walk(body):
            if key == "flags":
                seen += 1
                assert value == []
    assert seen > 0
    card_evidence = [e for i in people.build_creator(store, "c_ng_macro", "NG")["items"] for e in i["card"]["evidence"]]
    assert card_evidence and all(e["flags"] == [] for e in card_evidence)


def test_named_cards_carry_evidence_from_page_tier_creators_only(fx):
    for body in all_bodies(fx):
        for path, key, value in walk(body):
            if key == "evidence" and isinstance(value, list):
                assert all(e.get("creator_tier") in people.PAGE_TIERS for e in value), path


def evidence(post_id, platform, handle, tier):
    return {"id": post_id, "platform": platform, "handle": handle, "url": None, "posted_at": None, "market": "NG",
            "text": post_id, "engagement": {}, "flags": ["check_pattern"], "creator_tier": tier}


class InEvidence(FixtureStore):
    """The owambe card's evidence also holds a post by the suppressed macro creator, one by a creator who was
    macro when they posted and is mid now, one by a handle 42 has no creator row for, and one by the macro."""

    def briefs(self, date):
        rows = json.loads(json.dumps(super().briefs(date)))
        for r in rows:
            for c in (r["payload"].get("cards") or []) + (r["payload"].get("more") or []):
                if r["market"] == "NG" and c["item_id"] == A:
                    c["evidence"] += [evidence("ev_hidden", "tiktok", "@Fixture_NG_Hidden", "macro"),
                                      evidence("ev_dropped", "tiktok", "@fixture_ng_mid", "macro"),
                                      evidence("ev_nobody", "tiktok", "@fixture_nobody", "mega"),
                                      evidence("ev_macro", "tiktok", "fixture_ng_macro", "mid")]
                    c["thumbnails"] = ["ev_hidden", "ev_macro"]
        return rows


def test_a_suppressed_creator_never_appears_in_card_evidence():
    store = InEvidence()
    creator = people.build_creator(store, "c_ng_macro", "NG")
    community = people.build_communities(store, "NG")["communities"][0]
    for body in [creator, community] + all_bodies(store):
        text = json.dumps(body).lower()
        assert "ev_hidden" not in text and "fixture_ng_hidden" not in text
    owambe = next(i["card"] for i in creator["items"] if i["card"]["item_id"] == A)
    assert [e["id"] for e in owambe["evidence"]] == ["ev_macro"] and owambe["thumbnails"] == ["ev_macro"]


def test_card_evidence_is_named_by_the_creator_tier_now():
    body = people.build_creator(InEvidence(), "c_ng_macro", "NG")
    ids = [e["id"] for i in body["items"] for e in i["card"]["evidence"]]
    assert "ev_dropped" not in ids  # macro when posted, mid now
    assert "ev_nobody" not in ids  # no creator row: tier unknown, so not named
    assert "ev_macro" in ids  # macro now, whatever the tier at post time
    assert all(e["flags"] == [] for i in body["items"] for e in i["card"]["evidence"])


def test_no_card_evidence_is_named_without_the_creators_table():
    class NoCreators(InEvidence):
        def creators_by_handle(self, keys):
            return None

    body = people.build_creator(NoCreators(), "c_ng_macro", "NG")
    assert all(i["card"]["evidence"] == [] for i in body["items"])


def test_suppressed_creators_are_never_members_or_example_authors(fx):
    for body in all_bodies(fx):
        for _, key, value in walk(body):
            if key == "members" and value:
                assert "c_ng_hidden" not in {m["creator_id"] for m in value}
            if key == "example_posts" and value:
                assert not {p["id"] for p in value} & {"pp_hidden_1", "pp_hidden_2", "pp_hidden_3"}
                assert "@fixture_ng_hidden" not in {p["handle"] for p in value}
        assert "fixture_ng_hidden" not in json.dumps(body)


class EarlierFlag(FixtureStore):
    """Owambe was flagged likely_coordinated in the 29 September run, and is clean in the 30 September one. Peaking
    sound is flagged only in a failed run and in a run before the window, which must not count."""

    def _runs_all(self):
        return super()._runs_all() + [{"run_id": "r_detect_20260801_01", "stage": "detect", "run_date": "2026-08-01",
                                       "status": "ok", "started_at": "2026-08-01T04:00:00+02:00",
                                       "finished_at": "2026-08-01T04:30:00+02:00", "counts": {}, "error": None}]

    def _optional(self, name):
        rows = super()._optional(name)
        if name != "item_state" or rows is None:
            return rows
        clean = next(r for r in rows if r["item_id"] == A and r["market"] == "NG")
        flagged = dict(clean, authenticity="likely_coordinated", eligible=False)
        return rows + [dict(flagged, run_id="r_detect_20260929_01", metric_date="2026-09-29"),
                       dict(flagged, item_id=B, run_id="r_detect_20260930_00", metric_date="2026-09-30"),
                       dict(flagged, item_id=B, run_id="r_detect_20260801_01", metric_date="2026-08-01")]


def test_rule_3_covers_every_good_detect_run_in_the_28_days():
    store = EarlierFlag()
    blocked = people.excluded_items(store, store.latest_detect_run(), "NG", START, END)["rule_3"]
    assert A in blocked and B not in blocked
    body = people.build_creator(store, "c_ng_macro", "NG")
    assert A not in json.dumps(body) and "#fixture_ng_owambe" not in json.dumps(body)
    assert {i["card"]["item_id"] for i in body["items"]} == {B}
    assert "pp_macro_1" not in [p["id"] for p in body["recent_posts"]]  # an owambe post


def test_detect_states_are_the_good_detect_runs_in_the_window(fx):
    rows = EarlierFlag().detect_states(START, END)
    assert {r["run_id"] for r in rows} == {"r_detect_20260929_01", "r_detect_20260930_01"}
    assert {r["run_id"] for r in fx.detect_states(START, END)} <= {"r_detect_20260929_01", "r_detect_20260930_01"}


def test_coord_score_never_appears_in_any_response(fx):
    for store in (fx, Flagged()):
        for body in all_bodies(store):
            assert not any(key == "coord_score" for _, key, _ in walk(body))
            assert "coord_score" not in json.dumps(body)


def test_rule_3_items_never_join_edges(fx):
    blocked = people.excluded_items(fx, fx.latest_detect_run(), "NG", START, END)
    assert {BOT, SPON, CHECK, HELD, C} <= blocked["rule_3"]
    assert blocked["sensitive"] == {S} and blocked["generic"] == {G} and blocked["board"] == {D}
    assert {A, B, J}.isdisjoint(set().union(*blocked.values()))


# Nothing calls a model.

def test_nothing_calls_a_model():
    from pathlib import Path

    for mod in (people, store_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for word in ("vertexai", "genai", "generate_content", "ML.GENERATE", "openai"):
            assert word not in src


# BigQuery: parameterised, capped, degrading, and never reading coord_score.

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


CORE_OBJECTS = ("posts", "post_observations", "post_items", "cultural_map", "creators", "item_counter_daily",
                "post_enrichment", "v_good_runs", "v_item_state_current")


def catalog(*extra, map_columns=()):
    rows = [{"ds": "intelligence_42_core", "n": n, "what": "table"} for n in CORE_OBJECTS + extra]
    rows += [{"ds": "intelligence_42_core", "n": c, "what": "map_column"} for c in map_columns]
    return rows + [{"ds": "intelligence_42_agent", "n": "runs", "what": "table"}]


def run_people_queries(bq):
    bq.creator("c_ng_macro")
    bq.creators_by_id(["c_ng_macro", "c_ng_mega"])
    bq.sensitive_items()
    bq.generic_items()
    bq.board_items("NG", START, END)
    bq.suppressed_creators()
    bq.creator_posts("c_ng_macro", [S, BOT], 12)
    bq.window_posts(["c_ng_macro"], "NG", START, END)
    bq.community_edges("NG", START, END, [S, BOT])
    bq.creators_by_handle(["tiktok:fixture_ng_macro"])
    bq.detect_states(START, END)


def test_bigquery_people_reads_are_parameterised_capped_and_read_only():
    client = FakeClient(catalog("v_sensitive_items", "v_suppressed_creators", map_columns=("status",)))
    run_people_queries(BigQueryStore(client=client))
    assert len(client.calls) > 5
    for sql, cfg in client.calls:
        assert cfg.maximum_bytes_billed == 2_000_000_000
        assert not re.search(r"\b(INSERT|UPDATE|MERGE|CREATE|DELETE|DROP|TRUNCATE)\b", sql)
        assert "coord_score" not in sql and "ML." not in sql
        if "INFORMATION_SCHEMA" not in sql:
            for literal in ("c_ng_macro", S, "'NG'", START, END):
                assert literal not in sql
    joined = "\n".join(s for s, _ in client.calls)
    assert "v_sensitive_items" in joined and "v_suppressed_creators" in joined
    assert "is_board" in joined and "'generic'" in joined
    assert "IN UNNEST(@keys)" in joined


def test_bigquery_creators_by_handle_matches_the_key_people_builds():
    client = FakeClient(catalog())
    BigQueryStore(client=client).creators_by_handle([store_mod.creator_key("twitter", "@Fixture_NG_K1")])
    sql, cfg = client.calls[-1]
    assert "coord_score" not in sql and "IF(LOWER(TRIM(c.platform)) = 'twitter', 'x', LOWER(TRIM(c.platform)))" in sql
    assert [p.values for p in cfg.query_parameters if p.name == "keys"] == [["x:fixture_ng_k1"]]
    assert store_mod.creator_key("reddit", " u/Name ") == "reddit:name"
    assert store_mod.creator_key("tiktok", "@") is None and store_mod.creator_key("tiktok", None) is None


def test_bigquery_detect_states_read_every_good_detect_run_in_the_window():
    client = FakeClient(catalog())
    BigQueryStore(client=client).detect_states(START, END)
    sql = client.calls[-1][0]
    assert "v_good_runs" in sql and "g.stage = 'detect'" in sql and "BETWEEN @start AND @end" in sql
    assert "g.run_id = s.run_id" in sql and "coord_score" not in sql
    store_mod._CATALOG.clear()
    client = FakeClient([r for r in catalog() if r["n"] != "v_good_runs"])
    assert BigQueryStore(client=client).detect_states(START, END) is None


def test_bigquery_generic_items_need_the_status_column():
    client = FakeClient(catalog(), rows=[{"item_id": G}])
    assert BigQueryStore(client=client).generic_items() == set()
    assert not any("cm.status" in s for s, _ in client.calls)
    store_mod._CATALOG.clear()
    client = FakeClient(catalog(map_columns=("status",)), rows=[{"item_id": G}])
    assert BigQueryStore(client=client).generic_items() == {G}


def test_bigquery_edges_follow_the_market_day_and_three_item_rule():
    client = FakeClient(catalog())
    BigQueryStore(client=client).community_edges("NG", START, END, [S])
    sql = next(s for s, _ in client.calls if "post_observations" in s)
    assert "MIN(po.observed_date)" in sql and "po.lane_class != 'legacy'" in sql
    assert "IFNULL(po.lane, '') NOT IN ('legacy', 'agent_live')" in sql
    assert "NOT IN UNNEST(@excluded)" in sql
    assert "HAVING COUNT(*) >= 3" in sql and "a.creator_id < b.creator_id" in sql


def test_bigquery_sensitive_set_from_the_view_the_column_or_not_yet():
    client = FakeClient(catalog(), rows=[{"item_id": S}])
    assert BigQueryStore(client=client).sensitive_items() is None
    store_mod._CATALOG.clear()
    client = FakeClient(catalog(map_columns=("sensitive",)), rows=[{"item_id": S}])
    assert BigQueryStore(client=client).sensitive_items() == {S}
    assert any("cm.sensitive" in s for s, _ in client.calls)
    store_mod._CATALOG.clear()
    client = FakeClient(catalog("v_sensitive_items"), rows=[{"item_id": S}])
    assert BigQueryStore(client=client).sensitive_items() == {S}


def test_bigquery_the_set_is_complete_only_with_the_complete_view_in_core():
    for extra, map_columns in (((), ()), ((), ("sensitive",)), (("v_sensitive_items",), ("sensitive",))):
        store_mod._CATALOG.clear()
        assert BigQueryStore(client=FakeClient(catalog(*extra, map_columns=map_columns))).sensitive_complete() is False
    store_mod._CATALOG.clear()
    agent_only = catalog("v_sensitive_items") + [
        {"ds": "intelligence_42_agent", "n": "v_sensitive_items_complete", "what": "table"}]
    assert BigQueryStore(client=FakeClient(agent_only)).sensitive_complete() is False
    store_mod._CATALOG.clear()
    client = FakeClient(catalog("v_sensitive_items", "v_sensitive_items_complete", map_columns=("sensitive",)),
                        rows=[{"item_id": S}, {"item_id": B}])
    bq = BigQueryStore(client=client)
    assert bq.sensitive_complete() is True
    assert bq.sensitive_items() == {S, B}
    # The set is the union of every source (store.sensitive_items); the complete view is one of the reads.
    reads = [s for s, _ in client.calls if "intelligence_42_core.v_sensitive_items_complete" in s]
    assert reads and all("cm.sensitive" not in s for s in reads)


def test_bigquery_people_degrade_when_tables_are_missing():
    client = FakeClient([r for r in catalog() if r["n"] not in ("creators", "posts")])
    bq = BigQueryStore(client=client)
    assert bq.creator("c_ng_macro") is None
    assert bq.creator_posts("c_ng_macro", [], 12) is None
    assert bq.window_posts(["c_ng_macro"], "NG", START, END) is None
    assert bq.community_edges("NG", START, END, []) is None
    assert bq.suppressed_creators() is None
    assert not any("post_observations" in s for s, _ in client.calls)


def test_sensitive_set_is_the_union_of_every_source_that_exists():
    class Both(store_mod.FixtureStore):
        def _optional(self, name):
            if name == "people_sensitive_complete":
                return [{"item_id": "S", "market": None, "category": "political"}]
            if name == "people_sensitive":
                return [{"item_id": "K", "market": None, "category": "religion"}]
            return super()._optional(name)

    assert Both().sensitive_items() == {"S", "K"}

    class Catalog:
        objects = {f"{store_mod.CORE}.v_sensitive_items_complete", f"{store_mod.CORE}.v_sensitive_items"}

    sql = []

    class FakeBQ(store_mod.BigQueryStore):
        def __init__(self):
            pass

        def _catalog(self):
            return {"objects": Catalog.objects, "map_columns": set()}

        def _t(self, name):
            return f"`{name}`"

        def _find(self, name):
            full = f"{store_mod.CORE}.{name}"
            return f"`{full}`" if full in Catalog.objects else None

        def _query(self, text, **params):
            sql.append(text)
            return [{"item_id": "S"}] if "complete" in text else [{"item_id": "K"}]

    assert FakeBQ().sensitive_items() == {"S", "K"}
    assert any("v_sensitive_items_complete" in s for s in sql)
    assert any("v_sensitive_items`" in s for s in sql)


def test_the_api_switches_on_only_with_the_complete_view_detect_builds_when_ready():
    # Producer and reader agree on the name and dataset: detect's default apply leaves the complete view out, so
    # the API keeps named item lists off; built once SENSITIVE_COMPLETE_READY is set, the API reads it.
    from core.detect import sqlrun

    def catalog_of(statements):
        rows = catalog()
        for stmt in statements:
            ds, name = sqlrun.object_name(stmt).split(".")
            rows.append({"ds": ds, "n": name, "what": "table"})  # INFORMATION_SCHEMA.TABLES lists views too
        return rows

    store_mod._CATALOG.clear()
    default = BigQueryStore(client=FakeClient(catalog_of(sqlrun.agent_statements()), rows=[{"item_id": S}]))
    assert default.sensitive_complete() is False and default.sensitive_items() == {S}
    store_mod._CATALOG.clear()
    built = sqlrun.agent_statements(sensitive_complete=True, religious_holidays=False)
    ready = BigQueryStore(client=FakeClient(catalog_of(built), rows=[{"item_id": S}]))
    assert ready.sensitive_complete() is True and ready.sensitive_items() == {S}
    complete = next(s for s in built if sqlrun.object_name(s).endswith(".v_sensitive_items_complete"))
    assert re.search(r"\bitem_id\b", complete) and re.search(r"\bcategory\b", complete)


def test_a_community_heading_never_shows_an_item_id(fx, monkeypatch):
    """An unnamed TikTok sound is labelled by its numeric audio id; the heading leaves the id out."""
    real_card = people._Page.card

    def numeric(self, item_id):
        card = real_card(self, item_id)
        return None if card is None else dict(card, title="7626690624321800974")

    monkeypatch.setattr(people._Page, "card", numeric)
    c = people.build_communities(fx, "NG")["communities"][0]
    assert "7626690624321800974" not in c["label"] and c["label"]
    assert people._ID_LIKE.fullmatch("tiktok_sound:7626690624321800974")
    assert not people._ID_LIKE.fullmatch("#fixture_ng_jollof")


def test_a_post_with_two_enrichment_rows_counts_once(fx, monkeypatch):
    """Product review COM-D02: an embedding row beside the language row doubled posts in counts and examples."""
    before = people.build_communities(fx, "NG")["communities"][0]
    real = fx.window_posts

    def doubled(*args):
        rows = real(*args) or []
        return rows + [dict(r, langs=None, formats=None) for r in rows]

    monkeypatch.setattr(fx, "window_posts", doubled)
    after = people.build_communities(fx, "NG")["communities"][0]
    assert after["languages"] == before["languages"]
    ids = [p["id"] for p in after["example_posts"] or []]
    assert len(ids) == len(set(ids)) and ids == [p["id"] for p in before["example_posts"] or []]
    creator = people.build_creator(fx, "c_ng_macro", "NG")
    monkeypatch.setattr(fx, "window_posts", real)
    assert creator == people.build_creator(fx, "c_ng_macro", "NG")


def test_before_the_sensitive_set_a_community_title_names_no_author(no_sensitive_set, monkeypatch):
    real_card = people._Page.card

    def authored(self, item_id):
        card = real_card(self, item_id)
        return None if card is None else dict(card, title="TikTok post by @fixture_ng_macro")

    monkeypatch.setattr(people._Page, "card", authored)
    listed = people.build_communities(no_sensitive_set, "NG")
    assert listed["communities"] and "fixture_ng_macro" not in json.dumps(listed)
    assert "TikTok post" in listed["communities"][0]["label"]


def test_a_real_label_starting_uc_is_not_an_id():
    assert not people._ID_LIKE.fullmatch("ucl_champions_league_final_2026")
    assert people._ID_LIKE.fullmatch("youtube:ucnem4dhhuwmvwmyktz8x46g")
