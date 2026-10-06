"""The suppression list on Today, trends, Discover and topic pages (SETUP.md data protection, contract.md sections
12.1 and 16): a suppressed creator's posts are left out of every card, held item and topic page, and their handle
is masked wherever else it is written. When the list cannot be read, no evidence author is named."""
from copy import deepcopy
import hashlib
import json
import re

import pytest

from core.api import discover, people, today
from core.api import store as store_mod
from core.api.store import FixtureStore

D30 = "2026-09-30"
HIDDEN = "fixture_ng_hidden"  # c_ng_hidden in people_creators.json, on people_suppressed.json
HIDDEN_POST = "tt_hidden_901"


def iid(kind, key):
    return hashlib.sha256(f"{kind}|{key}".encode("utf-8")).hexdigest()


ZA_A = iid("hashtag", "#fixture_za_step")
ZA_H = iid("hashtag", "#fixture_za_small")
HERITAGE = iid("hashtag", "#fixture_za_heritage")


class Patched(FixtureStore):
    def __init__(self, **overrides):
        super().__init__()
        for name, fn in overrides.items():
            setattr(self, name, fn)


@pytest.fixture(autouse=True)
def clean():
    store_mod.SUPPRESSIONS.clear()
    yield
    store_mod.SUPPRESSIONS.clear()


def hidden_record(handle="@Fixture_NG_Hidden"):
    return {"id": HIDDEN_POST, "platform": "tiktok", "handle": handle,
            "url": f"https://example.invalid/tiktok/{handle}/{HIDDEN_POST}", "posted_at": "2026-09-29T10:00:00+02:00",
            "market": "ZA", "text": "Post by the hidden creator about the step", "flags": [],
            "engagement": {"views": 10, "likes": 1, "comments": 0, "shares": 0},
            "thumbnail_url": f"https://example.invalid/thumb/{HIDDEN_POST}.jpg", "creator_tier": "macro"}


def with_hidden_posts(base):
    """The 30 September briefs with the hidden creator's post on the ZA step card and on a ZA held item, and their
    handle in another author's post text."""
    def briefs(date):
        rows = deepcopy(base.briefs(date))
        for row in rows:
            if row["market"] != "ZA" or date != D30:
                continue
            payload = row["payload"]
            for card in payload["cards"] + payload["more"]:
                if card["item_id"] == ZA_A:
                    card["evidence"].append(hidden_record())
                    card["evidence_ids"].append(HIDDEN_POST)
                    card["thumbnails"] = [HIDDEN_POST] + card["thumbnails"]
                    card["evidence"][0]["text"] += f" with @{HIDDEN}"
            held = next(i for i in payload["held_back"]["items"] if i["item_id"] == ZA_H)
            held["title"] = "tiktok:901"
            held["evidence"] = [dict(hidden_record("tiktok_user"), id="901", handle=f"@{HIDDEN}")]
            held["evidence_ids"] = ["901"]
        return rows
    return briefs


def named(obj, *words):
    """The words written anywhere in obj, as whole handles in any case."""
    text = json.dumps(obj)
    return [w for w in words if re.search(rf"(?i)(?<![\w]){re.escape(w)}(?![\w])", text)]


def evidence_ids(obj):
    found = []
    if isinstance(obj, dict):
        for record in obj.get("evidence") or []:
            if isinstance(record, dict):
                found.append(record.get("id"))
        for value in obj.values():
            found += evidence_ids(value)
    elif isinstance(obj, list):
        for value in obj:
            found += evidence_ids(value)
    return found


def test_today_leaves_out_a_suppressed_creators_posts_and_masks_their_handle():
    base = FixtureStore()
    store = Patched(briefs=with_hidden_posts(base))
    out = today.build_today(store, D30)
    assert named(out, HIDDEN) == []
    assert HIDDEN_POST not in evidence_ids(out) and "901" not in evidence_ids(out)
    za = next(m for m in out["markets"] if m["market"] == "ZA")
    held = next(i for i in za["held_back"]["items"] if i["item_id"] == ZA_H)
    assert held["evidence"] == [] and held["evidence_ids"] == []
    step = next((c for c in za["cards"] + za["more"] if c["item_id"] == ZA_A), None)
    if step is not None:
        assert HIDDEN_POST not in step["thumbnails"] and HIDDEN_POST not in step["evidence_ids"]
    # Everyone else is still named.
    assert named(out, "fixture_za_1") == ["fixture_za_1"]


def test_trends_and_trend_detail_leave_out_a_suppressed_creator():
    store = Patched(briefs=with_hidden_posts(FixtureStore()))
    assert named(today.build_trends(store, "ZA", D30), HIDDEN) == []
    held = today.build_trend(store, ZA_H, "ZA", D30)
    assert named(held, HIDDEN) == [] and held["evidence"] == []


def test_a_handle_hidden_from_the_app_leaves_today_on_the_next_read():
    store = FixtureStore()
    assert named(today.build_today(store, D30), "fixture_za_1") == ["fixture_za_1"]
    store_mod.SUPPRESSIONS.append({"suppression_id": "sup_app_000000000001", "status_at": "2026-09-30T08:00:00+00:00",
                                   "status": "suppressed", "creator_id": None, "platform": "tiktok",
                                   "handle": "fixture_za_1", "reason": "Asked to be left out", "who": "Thandi"})
    creators = store.creators_by_id

    def with_za_1(ids):
        rows = creators(ids) or []
        extra = {"creator_id": "c_za_1", "platform": "tiktok", "handle": "@fixture_za_1", "followers": 10,
                 "tier": "nano", "home_market": "ZA"}
        return rows + [extra] if "c_za_1" in ids else rows

    def by_handle(keys):
        row = {"creator_id": "c_za_1", "platform": "tiktok", "handle": "@fixture_za_1"}
        return [row] if "tiktok:fixture_za_1" in keys else []

    hidden = Patched(creators_by_id=with_za_1, creators_by_handle=by_handle)
    assert named(today.build_today(hidden, D30), "fixture_za_1") == []


@pytest.mark.parametrize("broken", ["missing", "raises"])
def test_today_names_no_evidence_author_when_the_list_cannot_be_read(broken):
    def suppressed_creators():
        if broken == "raises":
            raise RuntimeError("view read failed")
        return None

    store = Patched(briefs=with_hidden_posts(FixtureStore()), suppressed_creators=suppressed_creators)
    out = today.build_today(store, D30)
    assert named(out, HIDDEN, "fixture_za_1", "fixture_za_2") == []
    for record in [r for m in out["markets"] for c in m["cards"] + m["more"] for r in c["evidence"]]:
        assert "handle" not in record and "url" not in record


def test_discover_and_topic_leave_out_a_suppressed_creator():
    base = FixtureStore()

    def item_evidence(item_id, market):
        rows = base.item_evidence(item_id, market) or []
        return rows + [{"item_id": item_id, "market": market, "post_id": HIDDEN_POST, "platform": "tiktok",
                        "handle": f"@{HIDDEN}", "url": f"https://example.invalid/@{HIDDEN}/1",
                        "published_at": "2026-09-29T10:00:00+02:00", "excerpt": f"fixture post by @{HIDDEN}",
                        "views": 1, "likes": 0, "comments": 0, "shares": 0, "thumbnail_url": None,
                        "creator_tier": "macro"}]

    def item_states(run, market):
        rows = base.item_states(run, market)
        for r in rows:
            if r["item_id"] == HERITAGE:
                r["aliases"] = list(r.get("aliases") or []) + [f"@{HIDDEN}"]
        return rows

    store = Patched(briefs=with_hidden_posts(base), item_evidence=item_evidence, item_states=item_states,
                    first_ok_collect_date=lambda: "2026-09-01")
    assert named(discover.build_discover(store, "ZA"), HIDDEN) == []
    assert named(discover.build_radar(store, "ZA"), HIDDEN) == []
    topic = discover.build_topic(store, HERITAGE, "ZA")
    assert named(topic, HIDDEN) == [] and HIDDEN_POST not in evidence_ids(topic)
    assert [e["id"] for e in topic["evidence"]] == ["tt_her_001", "x_her_002"]
    step = discover.build_topic(store, ZA_A, "ZA")
    assert named(step, HIDDEN) == [] and HIDDEN_POST not in evidence_ids(step)


# A suppressed YouTube creator: on the list by creator id (their channel id), with a handle in creators, and a
# creator item keyed by the channel id whose map label is the channel's display name.
CHANNEL = "UC" + "fixtureHiddenChan_0001"
CHANNEL_NAME = "Fixture Hidden Channel"
YT_HANDLE = "fixture_yt_hidden"
YT_ITEM = iid("creator", "youtube:" + CHANNEL.casefold())
YT_POST = "yt_hidden_701"


def youtube_hidden(base, **more):
    """base with the YouTube channel on the suppression list and in the map, more overrides on top."""
    creators, suppressed, map_items = base.creators_by_id, base.suppressed_creators, base.map_items

    def creators_by_id(ids):
        rows = creators(ids) or []
        extra = {"creator_id": CHANNEL, "platform": "youtube", "handle": f"@{YT_HANDLE}", "followers": 900000,
                 "tier": "macro", "home_market": "ZA"}
        return rows + [extra] if CHANNEL in ids else rows

    def items(item_ids):
        rows = map_items(item_ids) or []
        extra = {"item_id": YT_ITEM, "kind": "creator", "canonical_key": "youtube:" + CHANNEL.casefold(),
                 "label": CHANNEL_NAME, "aliases": [], "status": None}
        return rows + [extra] if YT_ITEM in item_ids else rows

    return Patched(creators_by_id=creators_by_id, suppressed_creators=lambda: (suppressed() or set()) | {CHANNEL},
                   map_items=items, **more)


def youtube_record():
    return {"id": YT_POST, "platform": "youtube", "handle": CHANNEL,
            "url": f"https://example.invalid/youtube/channel/{CHANNEL}/{YT_POST}",
            "posted_at": "2026-09-29T10:00:00+02:00", "market": "ZA", "text": "A video about the step",
            "flags": [], "engagement": {"views": 10, "likes": 1, "comments": 0, "shares": 0},
            "thumbnail_url": f"https://example.invalid/thumb/{YT_POST}.jpg", "creator_tier": "macro"}


def with_youtube_channel(base):
    """The 30 September briefs with a ZA card for the channel's creator item (titled by the channel id), the
    channel's video on the step card, and its channel id in other authors' post text. Its display name in other
    words is left alone (a name such as Music is also a word)."""
    def briefs(date):
        rows = deepcopy(base.briefs(date))
        for row in rows:
            if row["market"] != "ZA" or date != D30:
                continue
            payload = row["payload"]
            step = next(c for c in payload["cards"] + payload["more"] if c["item_id"] == ZA_A)
            channel = dict(deepcopy(step), item_id=YT_ITEM, kind="creator", title=CHANNEL)
            payload["more"].append(channel)
            step["evidence"].append(youtube_record())
            step["evidence_ids"].append(YT_POST)
            step["thumbnails"] = [YT_POST] + step["thumbnails"]
            step["evidence"][0]["text"] += f" as said on youtube.com/channel/{CHANNEL}"
            held = next(i for i in payload["held_back"]["items"] if i["item_id"] == ZA_H)
            held["evidence"][0]["text"] += f" as youtube:{CHANNEL} does"
        return rows
    return briefs


def test_today_masks_a_suppressed_youtube_channels_name_and_id():
    base = FixtureStore()
    store = youtube_hidden(base, briefs=with_youtube_channel(base))
    for out in (today.build_today(store, D30), today.build_trends(store, "ZA", D30),
                today.build_trend(store, ZA_A, "ZA", D30)):
        assert named(out, CHANNEL_NAME, CHANNEL, YT_HANDLE) == []
        assert YT_POST not in evidence_ids(out)
    for out in (today.build_today(store, D30), today.build_trends(store, "ZA", D30)):
        assert named(out, "fixture_za_1") == ["fixture_za_1"]  # everyone else is still named
    za = next(m for m in today.build_today(store, D30)["markets"] if m["market"] == "ZA")
    shown = {c["item_id"]: c for c in za["cards"] + za["more"]}
    if YT_ITEM in shown:
        assert shown[YT_ITEM]["title"] == "YouTube channel"


def test_discover_and_topic_mask_a_suppressed_youtube_channels_name_and_id():
    base = FixtureStore()

    def item_states(run, market):
        rows = base.item_states(run, market)
        if market == "ZA":
            heritage = next(r for r in rows if r["item_id"] == HERITAGE)
            rows.append(dict(deepcopy(heritage), item_id=YT_ITEM, kind="creator", label=CHANNEL_NAME,
                             canonical_key="youtube:" + CHANNEL.casefold(), aliases=[CHANNEL, f"@{YT_HANDLE}"]))
        return rows

    def item_evidence(item_id, market):
        rows = base.item_evidence(item_id, market) or []
        return rows + [{"item_id": item_id, "market": market, "post_id": YT_POST, "platform": "youtube",
                        "handle": CHANNEL, "url": f"https://example.invalid/youtube/{CHANNEL}/1",
                        "published_at": "2026-09-29T10:00:00+02:00", "excerpt": f"{CHANNEL_NAME} on the heritage",
                        "views": 1, "likes": 0, "comments": 0, "shares": 0, "thumbnail_url": None,
                        "creator_tier": "macro"}]

    store = youtube_hidden(base, briefs=with_youtube_channel(base), item_states=item_states,
                           item_evidence=item_evidence, first_ok_collect_date=lambda: "2026-09-01")
    assert named(discover.build_discover(store, "ZA"), CHANNEL_NAME, CHANNEL, YT_HANDLE) == []
    assert named(discover.build_radar(store, "ZA"), CHANNEL_NAME, CHANNEL, YT_HANDLE) == []
    for item_id in (YT_ITEM, HERITAGE):
        topic = discover.build_topic(store, item_id, "ZA")
        assert named(topic, CHANNEL_NAME, CHANNEL, YT_HANDLE) == [] and YT_POST not in evidence_ids(topic)
    assert discover.build_topic(store, YT_ITEM, "ZA")["card"]["title"] == "YouTube channel"


def test_a_community_label_never_names_a_suppressed_youtube_channel():
    base = FixtureStore()

    def community_edges(market, start, end, excluded):
        return [dict(e, items=sorted(set(e["items"]) | {YT_ITEM}))
                for e in base.community_edges(market, start, end, excluded) or []]

    store = youtube_hidden(base, community_edges=community_edges)
    body = people.build_communities(store, "NG")
    assert body["communities"] and named(body, CHANNEL_NAME, CHANNEL, YT_HANDLE) == []
    assert "YouTube channel" in body["communities"][0]["label"].split(", ")


def test_a_community_label_never_names_a_suppressed_creators_own_item_on_any_platform():
    base = FixtureStore()
    own = iid("creator", f"tiktok:{HIDDEN}")
    map_items = base.map_items

    def community_edges(market, start, end, excluded):
        return [dict(e, items=sorted(set(e["items"]) | {own}))
                for e in base.community_edges(market, start, end, excluded) or []]

    def items(item_ids):
        rows = map_items(item_ids) or []
        extra = {"item_id": own, "kind": "creator", "canonical_key": f"tiktok:{HIDDEN}", "label": "Hidden Dancer",
                 "aliases": [], "status": None}
        return rows + [extra] if own in item_ids else rows

    store = Patched(community_edges=community_edges, map_items=items)
    body = people.build_communities(store, "NG")
    assert body["communities"] and named(body, "Hidden Dancer", HIDDEN) == []


MUSIC_CHANNEL = "UC" + "a" * 22


def test_a_suppressed_channels_name_is_not_masked_in_free_text_of_other_items():
    value = {"cards": [{"title": "Music festival buzz in Joburg", "summary": "amapiano music trends"}]}
    out = today.without_hidden(value, (set(), {MUSIC_CHANNEL}, {"Music"}))
    assert out["cards"][0] == {"title": "Music festival buzz in Joburg", "summary": "amapiano music trends"}


def test_a_suppressed_channels_name_is_removed_where_it_names_the_creator():
    own = iid("creator", "youtube:" + MUSIC_CHANNEL.casefold())
    value = {"cards": [{"item_id": own, "kind": "creator", "title": "Music", "label": "Music",
                        "ask": "What is behind Music in South Africa this week?"},
                       {"item_id": "other", "kind": "topic", "title": "Music festival buzz",
                        "evidence": [{"id": "p1", "platform": "youtube", "handle": "fixture_reposter",
                                      "author_name": "Music", "text": f"Music festival, via {MUSIC_CHANNEL}"}]}]}
    out = today.without_hidden(value, (set(), {MUSIC_CHANNEL}, {"Music"}))
    mine, other = out["cards"]
    assert (mine["title"], mine["label"]) == ("YouTube channel", "YouTube channel")
    assert mine["ask"] == "What is behind YouTube channel in South Africa this week?"
    assert other["title"] == "Music festival buzz"
    record = other["evidence"][0]
    assert record["author_name"] != "Music" and record["handle"] == "fixture_reposter"
    assert record["text"].startswith("Music festival, via ") and MUSIC_CHANNEL not in record["text"]


def test_topic_origin_and_news_mask_a_suppressed_creators_handle():
    # The views carry no creator field, but a news story's label is its query text, which may name a handle.
    def news_followthrough(item_id, market, until):
        return [{"market": market, "news_day": None, "news_day_from": None, "seed_date": "2026-09-28",
                 "item_id": item_id, "label": f"@{HIDDEN} on heritage day", "kind": "topic",
                 "matched_items": [{"item_id": item_id, "label": f"@{HIDDEN}", "matched_by": "seed"}],
                 "collect_ran": True, "reached_state": False, "first_state_day": None,
                 "first_measured": "2026-09-24", "lag_days": None, "creator_id": "c_ng_hidden"}]

    def item_origin(item_id, market):
        return [{"item_id": item_id, "market": market, "origin": None, "first_measured": "2026-09-24",
                 "after_collection_began": True, "first_state_day": None, "lead_news_day": None, "lag_days": None,
                 "creator_id": "c_ng_hidden", "handle": f"@{HIDDEN}"}]

    store = Patched(news_followthrough=news_followthrough, item_origin=item_origin)
    topic = discover.build_topic(store, HERITAGE, "ZA")
    assert topic["news"] and topic["origin"]
    assert named(topic, HIDDEN) == [] and "c_ng_hidden" not in json.dumps(topic)


def _secondary_claim_card(cited):
    """The passing Today card with a second explanation claim that cites and quotes only the post cited."""
    from core.api.tests.test_today import _passing_specificity_card

    card = _passing_specificity_card()
    record = hidden_record() if cited == HIDDEN_POST else next(r for r in card["evidence"] if r["id"] == cited)
    if cited == HIDDEN_POST:
        card["evidence"].append(record)
        card["evidence_ids"].append(HIDDEN_POST)
    card["claims"].append({"id": "c3", "text": "A second creator posts the step too", "kind": "observation",
                           "evidence_ids": [cited],
                           "quotes": [{"evidence_id": cited, "text": " ".join(record["text"].split()[:4])}]})
    card["explanation_claim_ids"].append("c3")
    return card


def _briefs_with(card):
    base = FixtureStore()

    def briefs(date):
        rows = deepcopy(base.briefs(date))
        for row in rows:
            if row["market"] == "ZA" and date == D30:
                row["payload"]["cards"] = [deepcopy(card)]
                row["payload"]["more"] = []
                row["payload"]["headline"] = None
        return rows
    return briefs


def test_today_holds_a_card_whose_secondary_claim_cites_a_suppressed_post():
    # Control: the same second claim on a post that stays is shown.
    shown = _secondary_claim_card("ig_za_007")
    za = next(m for m in today.build_today(Patched(briefs=_briefs_with(shown)), D30)["markets"] if m["market"] == "ZA")
    assert [c["item_id"] for c in za["cards"]] == [ZA_A]

    out = today.build_today(Patched(briefs=_briefs_with(_secondary_claim_card(HIDDEN_POST))), D30)
    za = next(m for m in out["markets"] if m["market"] == "ZA")
    assert za["cards"] == [] and za["more"] == []
    held = next(i for i in za["held_back"]["items"] if i["item_id"] == ZA_A)
    assert held["reason_text"] == today.REJECT_POSTS
    # The suppressed creator's words leave the held item with their post.
    assert "Post by the hidden creator" not in json.dumps(out)
    # Every quote still on a card or held item resolves to a post returned beside it.
    for holder in [c for m in out["markets"] for c in m["cards"] + m["more"] + m["held_back"]["items"]]:
        ids = {e["id"] for e in holder.get("evidence") or []}
        for claim in holder.get("claims") or []:
            assert {q["evidence_id"] for q in claim.get("quotes") or []} <= ids


# A claim that cites a suppressed creator's post beside posts that stay is checked again on the posts that stay, by
# the brief's own rules: its label is lowered to what they allow (never raised), it is dropped when they no longer
# support it, and the card is held when what is left no longer carries the explanation.
KE_FEED_TEXT = "The step is also seen in Kenya's feeds"


def _mixed_claim_card(text="Two creators post the step too", label="observed", rested=False, hidden=None):
    """The passing Today card with a third claim citing the suppressed creator's post and ig_za_007, quoting the
    latter."""
    from core.api.tests.test_today import _passing_specificity_card

    card = _passing_specificity_card()
    card["market"] = "ZA"
    card["evidence"].append(hidden or hidden_record())
    card["evidence_ids"].append(HIDDEN_POST)
    kept = next(r for r in card["evidence"] if r["id"] == "ig_za_007")
    card["claims"].append({"id": "c3", "text": text, "label": label, "kind": "observation",
                           "evidence_ids": [HIDDEN_POST, "ig_za_007"],
                           "quotes": [{"evidence_id": "ig_za_007", "text": " ".join(kept["text"].split()[:4])}],
                           "numbers": []})
    if rested:
        card["explanation_claim_ids"].append("c3")
    return card


def _za(out):
    return next(m for m in out["markets"] if m["market"] == "ZA")


def _ke_feed_record():
    return dict(hidden_record(), market=None, source_market="KE")


def test_a_claim_on_a_suppressed_post_and_others_is_labelled_by_the_posts_that_stay():
    store = Patched(briefs=_briefs_with(_mixed_claim_card()))
    za = _za(today.build_today(store, D30))
    card = next(c for c in za["cards"] if c["item_id"] == ZA_A)
    claim = next(c for c in card["claims"] if c["id"] == "c3")
    # Two authors made it observed; the one that stays allows single source.
    assert claim["label"] == "single_source" and claim["evidence_ids"] == ["ig_za_007"]
    # Claims the hidden post never touched keep their labels.
    assert next(c for c in card["claims"] if c["id"] == "c1")["label"] == "corroborated"
    # The trend list reads the same claim.
    listed = next(c for c in today.build_trends(store, "ZA", D30)["cards"] if c["item_id"] == ZA_A)
    assert next(c for c in listed["claims"] if c["id"] == "c3")["label"] == "single_source"


def test_a_rechecked_label_is_never_raised_and_a_news_driven_card_stays_one_step_lower():
    card = _mixed_claim_card(label="inferred")
    za = _za(today.build_today(Patched(briefs=_briefs_with(card)), D30))
    assert next(c for c in za["cards"][0]["claims"] if c["id"] == "c3")["label"] == "inferred"

    news = _mixed_claim_card(label="single_source")
    news["news_driven"] = True
    za = _za(today.build_today(Patched(briefs=_briefs_with(news)), D30))
    # One author allows single source; a news-driven card sits one step lower.
    assert next(c for c in za["cards"][0]["claims"] if c["id"] == "c3")["label"] == "inferred"


def test_a_claim_the_posts_that_stay_no_longer_support_is_dropped():
    card = _mixed_claim_card(KE_FEED_TEXT, label="single_source", hidden=_ke_feed_record())
    za = _za(today.build_today(Patched(briefs=_briefs_with(card)), D30))
    shown = next(c for c in za["cards"] if c["item_id"] == ZA_A)
    # Kenya rested on the hidden post's feed alone; the explanation never rested on the claim, so the card stays.
    assert [c["id"] for c in shown["claims"]] == ["c1", "c2"]
    assert "Kenya" not in json.dumps(shown["claims"])


def test_a_card_is_held_when_its_explanation_rested_on_a_claim_that_was_dropped():
    card = _mixed_claim_card(KE_FEED_TEXT, label="single_source", rested=True, hidden=_ke_feed_record())
    store = Patched(briefs=_briefs_with(card))
    za = _za(today.build_today(store, D30))
    assert za["cards"] == [] and za["more"] == []
    held = next(i for i in za["held_back"]["items"] if i["item_id"] == ZA_A)
    assert held["reason_text"] == today.REJECT_EXPLAINED
    assert held["failed_reason"] == today.HIDDEN_UNSUPPORTED
    # Other readers show it as numbers and posts, as a card whose explanation failed its checks.
    listed = next(c for c in today.build_trends(store, "ZA", D30)["cards"] if c["item_id"] == ZA_A)
    assert listed["explained"] is False and listed["explanation"] is None and listed["claims"] == []


def test_a_card_is_held_when_a_dropped_claim_leaves_fewer_than_2_claims():
    card = _mixed_claim_card(KE_FEED_TEXT, label="single_source", hidden=_ke_feed_record())
    card["claims"] = [c for c in card["claims"] if c["id"] != "c2"]
    za = _za(today.build_today(Patched(briefs=_briefs_with(card)), D30))
    assert za["cards"] == []
    held = next(i for i in za["held_back"]["items"] if i["item_id"] == ZA_A)
    assert held["failed_reason"] == today.HIDDEN_UNSUPPORTED


def test_a_card_is_held_when_the_suppressed_post_leaves_fewer_than_3_posts_it_can_show():
    card = _mixed_claim_card()
    card["evidence"] = [r for r in card["evidence"] if r["id"] != "yt_za_008"]
    card["evidence_ids"] = [i for i in card["evidence_ids"] if i != "yt_za_008"]
    card["claims"][0]["evidence_ids"] = ["tt_za_006", "ig_za_007"]
    card["specificity"]["local_evidence_ids"] = ["ig_za_007", "tt_za_006"]
    # Control: with the post kept, 3 posts can be shown.
    kept = deepcopy(card)
    kept["evidence"][-1] = dict(kept["evidence"][-1], handle="@fixture_za_kept")
    assert [c["item_id"] for c in _za(today.build_today(Patched(briefs=_briefs_with(kept)), D30))["cards"]] == [ZA_A]

    za = _za(today.build_today(Patched(briefs=_briefs_with(card)), D30))
    assert za["cards"] == []
    held = next(i for i in za["held_back"]["items"] if i["item_id"] == ZA_A)
    assert held["failed_reason"] == today.HIDDEN_TOO_FEW_POSTS
