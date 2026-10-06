"""One honest local trend, end to end through every gate of the morning brief (core/brief/job.py).

A TikTok-led ZA dance trend: six posts from four different creators on six different days in the window, each in
the creator's own words, none flagged sponsored and every one read for a paid label (sponsor_checked true), two of
them naming the dated local cause ("it dropped on Monday"), all sighted in a measured lane, with valid collection
days on the main platform. The DuckDB tables, the evidence pack, the market scope read, the real gate context, the
claim checks, the place check, specificity, the gate and the payload are the real modules. Only the model is
scripted, and it answers as each prompt asks: a writer draft that cites the posts truthfully, support verdicts
that follow SUPPORT_SYSTEM and a critic verdict that follows CRITIC_SYSTEM.

Case 1: the posts are located in ZA (geo_market ZA, confidence 0.9 from a known source). The card publishes.
Case 2: the same posts with no known location, found only in South Africa's own feeds (source_market ZA). The
owner's rules (29 Sept, 2 Oct): such posts may back claims worded as seen in South Africa's feeds, one confidence
step lower, and Today shows a trend when most of its posts are located in the market or come from its own feeds.
The card publishes with those claims one label lower than in case 1.
"""

import json
import re
from datetime import datetime, time, timedelta, timezone

import pytest

from core.brief import gatectx, job
from core.brief import market_scope as brief_market_scope
from core.brief.tests.test_brief_job import EARLY, Client, FakeChain, FakeConfirm, pin_brief_model_cap
from core.detect.tests import duck
from core.detect.tests.fixtures import D, at, day, health, rid, run

SAST = timezone(timedelta(hours=2), "SAST")
ITEM = "za_kasi_step"
TITLE = "#KasiStepChallenge"

# (post id, creator, platform, days before D, SAST hour, text). D is Sunday 20 September 2026, so day(6) is Monday
# 14 September, the first day of the window. Four creators, at most two posts each, six different days.
POSTS = (
    ("p_naledi_1", "naledi.moves", "tiktok", 6, 19,
     "Kasi Step Challenge since it dropped on Monday, my sister and I learnt it in the yard"),
    ("p_bongi_1", "bongi_dances", "tiktok", 5, 18,
     "Since it dropped on Monday the whole street is doing the kasi step, this is our version"),
    ("p_sipho_1", "sipho_kasi", "tiktok", 4, 20,
     "Tried the kasi step after work, my knees are not ready but we move"),
    ("p_lerato_1", "lerato.steps", "tiktok", 3, 17,
     "Kasi step with my gran at the family braai, she did it better than me"),
    ("p_naledi_2", "naledi.moves", "tiktok", 2, 19,
     "Day five of the kasi step and I finally got the spin right"),
    ("p_bongi_2", "bongi_dances", "tiktok", 1, 16,
     "Taught the kasi step to my cousins this weekend, they nailed it"),
)
POST_IDS = [p[0] for p in POSTS]
FOUR = ["p_naledi_1", "p_bongi_1", "p_sipho_1", "p_lerato_1"]


def posted(days_before, hour):
    return datetime.combine(day(days_before), time(hour), SAST)


def world(*, located):
    """The ZA trend. located: the posts carry geo_market ZA at 0.9 from a known source; else they have no known
    location and are sighted in South Africa's own TikTok feed on the day they were posted."""
    con = duck.connect()
    duck.load(con, "agent.runs", [run("detect", D), run("stats", D)] + [run("collect", day(i)) for i in range(21)])
    duck.load(con, "core.item_state", [{
        "metric_date": D, "market": "ZA", "item_id": ITEM, "kind": "hashtag", "state_raw": "rising",
        "state": "rising", "untested": False, "main_ratio": 3.1, "creators3": 4, "posts3": 6,
        "top_creator_share3": 0.33, "authenticity": "clear", "sponsored_share": 0.0,
        "geo_status": "local" if located else "market_unconfirmed", "local_share": 1.0 if located else None,
        "geo_known_posts7": 6 if located else 0, "worth_raw": 0.8, "eligible": True, "run_id": rid("detect", D),
        "rule_version": "r1"}])
    duck.load(con, "core.cultural_map", [{
        "item_id": ITEM, "kind": "hashtag", "canonical_key": "kasistepchallenge", "label": TITLE,
        "first_seen": day(6), "status": "active", "valid_from": at(day(400)), "valid_to": None}])
    creators = {}
    for pid, handle, platform, before, hour, text in POSTS:
        creator = f"cr_{handle}"
        if creator not in creators:
            creators[creator] = handle
            duck.load(con, "core.creators", [{"creator_id": creator, "platform": platform, "handle": f"@{handle}",
                                              "coord_score": 0, "account_created_at": at(day(900))}])
        when = posted(before, hour)
        duck.load(con, "core.posts", [{
            "post_id": pid, "platform": platform, "creator_id": creator, "creator_tier_at_post": "micro",
            "text": text, "hashtags": ["kasistepchallenge"], "published_at": when, "post_date": when.date(),
            "views": 4000 + 100 * before, "likes": 300, "comments": 20, "shares": 10, "engagement": 330 + before,
            "geo_market": "ZA" if located else None, "geo_confidence": 0.9 if located else None,
            "geo_source": "ext_region" if located else None}])
        duck.load(con, "core.post_items", [{"post_id": pid, "item_id": ITEM, "via": "hashtag"}])
        seen = when + timedelta(hours=2)
        duck.load(con, "core.post_observations", [{
            "post_id": pid, "observed_at": seen, "observed_date": seen.astimezone(SAST).date(), "market": "ZA",
            "platform": platform, "route": "tiktok/trending", "series": "feed_tiktok", "lane": "feed",
            "lane_class": "unbiased_rank", "run_id": rid("collect", seen.date())}])
        # The enrich model read every post and found no paid marker, so sponsor_checked is true.
        duck.load(con, "core.post_enrichment", [{"post_id": pid, "sponsored": False, "near_dup_size": 1}])
        if not located:
            duck.load(con, "core.source_market_fixture", [{
                "post_id": pid, "source_markets": ["ZA"],
                "source_sightings": [{"source_market": "ZA", "source_region": None, "route": "tiktok/trending",
                                      "protocol": "tiktok/trending?feed=local", "observed_at": seen,
                                      "obs_date": seen.astimezone(SAST).date()}]}])
    # Valid collection days on the main platform (TikTok) for the three days the gate reads.
    duck.load(con, "core.collection_health", [health("feed_tiktok", day(i)) for i in range(3)])
    return con


# The model's answers. Claim wording is the only thing that differs between the two cases: case 2 words every
# place as seen in South Africa's feeds and labels those claims one step lower, as WRITER law 12 asks.


def claims_for(located):
    where = "Creators located in South Africa" if located else "Seen in South Africa's feeds, four creators"
    two = "Two creators located in South Africa" if located else "Seen in South Africa's feeds, two creators"
    placed = "observed" if located else "single_source"
    return [
        {"id": "c1", "text": f"{where} post their own kasi step videos on different days, one dancing with \"my gran "
                             f"at the family braai\".",
         "label": placed, "kind": "observation", "evidence_ids": FOUR,
         "quotes": [{"evidence_id": "p_lerato_1", "text": "my gran at the family braai"}], "number_ids": []},
        {"id": "c2", "text": "The earliest post in the pack is a TikTok video posted on Monday.",
         "label": "single_source", "kind": "observation", "evidence_ids": ["p_naledi_1"], "quotes": [],
         "number_ids": []},
        {"id": "c3", "text": f"{two} tie their videos to Monday, writing \"it dropped on Monday\".",
         "label": placed, "kind": "observation", "evidence_ids": ["p_naledi_1", "p_bongi_1"],
         "quotes": [{"evidence_id": "p_naledi_1", "text": "it dropped on Monday"}], "number_ids": []},
        {"id": "c4", "text": "The cited posts come from four different creators, in their own words, and none "
                             "carries a sponsored flag.",
         "label": "observed", "kind": "observation", "evidence_ids": FOUR, "quotes": [], "number_ids": []},
    ]


def sentence_for(located):
    lead = "Creators in South Africa are" if located else "Seen in South Africa's feeds, creators are"
    return (f"{lead} posting their own kasi step videos across the week, likely because the dance dropped on "
            "Monday, as two of them say.")


def draft_for(located):
    return {"explanation": sentence_for(located), "explanation_claim_ids": ["c1", "c3", "c4"],
            "claims": claims_for(located)}


def support_for(located):
    """SUPPORT_SYSTEM verdicts for each text the support check is asked about, with the reason the rules give."""
    place = ("each cited post's market field is ZA, which shows the place" if located else
             "each cited post has source_market ZA and unknown location, and the claim words the place only as seen "
             "in South Africa's feeds, at a label one step below located evidence")
    c = {x["id"]: x["text"] for x in claims_for(located)}
    return {
        c["c1"]: ("supported", f"Four different handles post kasi step videos on four different posted_at days; "
                               f"the quote is in p_lerato_1's text; {place}."),
        c["c2"]: ("supported", "p_naledi_1 is earliest_in_pack, its platform is tiktok and its posted_at is Monday "
                               "14 September; the wording is scoped to the pack."),
        c["c3"]: ("supported", f"Both cited texts say 'it dropped on Monday' in their own words; {place}."),
        c["c4"]: ("supported", "Four different handles, captions in the first person, and no flags line names "
                               "sponsored."),
        sentence_for(located): ("supported", f"The cited posts show creators posting their own kasi step videos on "
                                             f"different days and two texts name Monday's drop, which makes the "
                                             f"hedged why-now reasonable; {place}."),
    }


def critic_for(located):
    """CRITIC_SYSTEM's verdict. The simplest non-cultural explanation for a dance to a new release is a paid push;
    every post was read for a paid label (sponsor_checked true) and none carries a sponsored flag, and four unrelated
    creators post in their own words on different days, so it is ruled out. Two cited posts state the timing in
    their own words ("it dropped on Monday"); in case 2 they are own_feed_local and the sentence words them as seen
    in South Africa's feeds, which CRITIC_SYSTEM allows for a local why-now."""
    return {"non_cultural_explanation": "a paid or sponsored push behind the release",
            "ruled_out": True, "news_driven": False, "local_reaction": False, "local_why_now": True,
            "reason": "Every post was checked and shows no paid label, and four unrelated creators post their own "
                      "takes on different days; two cited posts give the timing in their own words."}


_CLAIM = re.compile(r"^Claim:\n<untrusted_content>\n(.*?)\n</untrusted_content>\nLabel: (.*)$", re.M | re.S)


class HonestModel:
    """The scripted model: the writer returns draft_for, the support check returns support_for for the text it is
    shown (unsupported for any text it was not scripted for), the critic returns critic_for."""

    def __init__(self, located, critic=None):
        self.located = located
        self.critic_answer = critic or critic_for(located)
        self.writer, self.support, self.critic = [], [], []
        self.answers = support_for(located)

    def complete_json(self, *, system, user, schema, model, max_tokens):
        usage = {"input_tokens": 100, "output_tokens": 50, "usd": 0.001}
        props = schema["properties"]
        if "explanation_claim_ids" in props:
            self.writer.append(user)
            return draft_for(self.located), usage
        if "ruled_out" in props:
            self.critic.append(user)
            return self.critic_answer, usage
        m = _CLAIM.match(user)
        text, label = (m.group(1), m.group(2).splitlines()[0]) if m else (None, None)
        self.support.append((text, label))
        verdict, reason = self.answers.get(text, ("unsupported", "not scripted"))
        return {"verdict": verdict, "reason": reason}, usage


def brief(con, model, monkeypatch):
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    pin_brief_model_cap(monkeypatch, EARLY)
    client = Client(con)
    chain = FakeChain()
    counts = job.run(client, D, chain=chain, model=model, make_sc=lambda run_id: object(), clock=lambda: EARLY,
                     build_ctx=gatectx.build_ctx, confirm=FakeConfirm(), core="core", agent="agent")
    rows = {r["market"]: r for r in client.inserted.get("agent.briefs", [])}
    return counts, json.loads(rows["ZA"]["payload"]), client.inserted.get("agent.claim_checks", [])


def holds(payload):
    return [(i["item_id"], i["reason"], i.get("rule"), i.get("failed_reason")) for i in payload["held_back"]["items"]]


@pytest.mark.parametrize("located", [True, False], ids=["located_in_za", "own_feeds_only"])
def test_the_pack_and_gate_context_are_what_the_honest_trend_gives(located, monkeypatch):
    con = world(located=located)
    client = Client(con)
    row = job._ranked(job._query(client, "candidates", {"d": D, "market": "ZA"}, "core", "agent"))[0]
    cand = job._prepare(client, D, "ZA", row, build_ctx=gatectx.build_ctx,
                        campaign_hashtags=gatectx.load_campaign_hashtags(),
                        political_terms=gatectx.load_political_terms("ZA"), core="core", agent="agent")
    evidence = cand["pack"]["evidence"]
    assert sorted(e["id"] for e in evidence) == sorted(POST_IDS)
    assert {e["market"] for e in evidence} == ({"ZA"} if located else {None})
    assert {e["source_market"] for e in evidence} == ({None} if located else {"ZA"})
    assert all(e["sponsor_checked"] is True and "sponsored" not in e["flags"] for e in evidence)
    assert (cand["row"]["market_scope"], cand["row"]["market_posts7"], cand["row"]["total_posts7"]) == (
        "market", 6, 6)
    ctx = cand["ctx"]
    assert ctx["valid_days"] == [True, True, True]
    assert "unbiased_rank" in ctx["lane_classes"]
    assert ctx["political"] is False and ctx["sponsored_share"] == 0 and ctx["paid_key"] is None
    decision = job._gate(cand, None)
    assert (decision.publish, decision.where, cand["held_reason"]) == (True, "today", None)


@pytest.mark.parametrize("located", [True, False], ids=["located_in_za", "own_feeds_only"])
def test_an_honest_local_trend_publishes_as_an_explained_card(located, monkeypatch):
    model = HonestModel(located)
    counts, payload, checks = brief(world(located=located), model, monkeypatch)

    assert holds(payload) == []
    assert counts["cards"] == 1 and counts["held"] == 0
    [card] = payload["cards"]
    assert card["item_id"] == ITEM and card["title"] == TITLE
    assert card["explained"] is True and card["explanation_status"] == "explained"
    assert card["explanation"] == sentence_for(located)
    assert card["explanation_claim_ids"] == ["c1", "c3", "c4"]
    assert card["specificity"]["status"] == "pass"
    assert card["market_scope"] == "market" and card["market_share7"] == 1.0
    assert card["news_driven"] is False
    assert payload["status"] == "published"
    # The honest draft passed every code check first time, so no repair round was needed.
    assert len(model.writer) == 1
    assert [t for t, _ in model.support if t not in model.answers] == []
    assert len(model.critic) == 1
    assert [c for c in checks if c["verdict"] in ("cut", "breach")] == []
    labels = {c["id"]: c["label"] for c in card["claims"]}
    # Case 2's place claims stand one label lower than case 1's: observed when located, single_source when only
    # seen in the market's own feeds. Claims that name no place keep the label their authors allow.
    assert labels == {"c1": "observed" if located else "single_source", "c2": "single_source",
                      "c3": "observed" if located else "single_source", "c4": "observed"}


# Recorded, not endorsed: the same honest trend when the critic names the release itself as the simplest
# non-cultural explanation. WRITER_SYSTEM asks for a why-now that names "an event, date, release, announcement,
# match, holiday or moment"; CRITIC_SYSTEM's menu holds "a news event or scheduled event that alone accounts for the
# posts", and its event pass (news_driven) is worded for "a news event" only. Whether a release, match or holiday
# card publishes then rests on whether the critic reads it as news. That reading is the owner's call, so these
# cases pin what the code does with each answer today.


def release_critic(news_driven):
    return {"non_cultural_explanation": "a scheduled event: the song's release on Monday accounts for the posts",
            "ruled_out": False, "news_driven": news_driven, "local_reaction": news_driven, "local_why_now": True,
            "reason": "Two cited posts tie their videos to Monday's drop and four creators react in their own words."}


@pytest.mark.parametrize("located", [True, False], ids=["located_in_za", "own_feeds_only"])
def test_a_release_named_as_a_scheduled_event_holds_the_card(located, monkeypatch):
    counts, payload, checks = brief(world(located=located), HonestModel(located, release_critic(False)), monkeypatch)
    assert counts["cards"] == 0
    assert holds(payload) == [(ITEM, "explanation_failed", "G10",
                               "Critic: a simpler explanation was not ruled out: a news or scheduled event")]
    assert [c["rule"] for c in checks if c["verdict"] in ("cut", "breach")] == ["critic"]


@pytest.mark.parametrize("located", [True, False], ids=["located_in_za", "own_feeds_only"])
def test_a_release_read_as_news_with_local_reaction_publishes_one_step_lower(located, monkeypatch):
    counts, payload, _ = brief(world(located=located), HonestModel(located, release_critic(True)), monkeypatch)
    assert counts["cards"] == 1 and holds(payload) == []
    [card] = payload["cards"]
    assert card["explained"] is True and card["news_driven"] is True
    lower = "single_source" if located else "inferred"
    assert {c["id"]: c["label"] for c in card["claims"]} == {"c1": lower, "c2": "inferred", "c3": lower,
                                                            "c4": "single_source"}
