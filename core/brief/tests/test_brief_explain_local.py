"""The explainer's input and repair for local, citable evidence (2 Oct staging brief: 7 of 12 held under G10).

Two causes are reproduced here on fixtures, with a fake model and no network:

- Place: before the foreign veto, the gate and specificity rule counted a post found in the market's feeds as local
  even when located in another market, but K3 cuts any claim that cites a post located in another market, whatever its
  wording. The writer was shown such posts as ordinary evidence, so it cited them before and after repair.
- Local why-now and the simpler explanation: the writer was not told which posts are local or located in the
  market, what the critic counts as a local why-now, or what a news-driven pass needs, so it wrote why-nows the
  critic could not accept.

Every check stays as it is; these tests only pin what the writer is given and asked for.
"""

import copy
import json
import re

from core.brief import explain, holds_report
from core.brief.explain import explain_trend
from core.brief.job import failed_reason
from core.brief.tests.test_brief_explain import RULED_OUT, W_END, W_START, FakeModel, outside_fences

NG_AT = "2026-09-26T09:00:00+01:00"
CANDIDATE = {"item_id": "it_iv", "kind": "hashtag", "title": "#islamicvideo", "state": "rising"}


def ng_post(eid, handle, text, *, market=None, source_market="NG", platform="tiktok", views=1000, at=NG_AT):
    flags = [] if market else ["market_assumed"]
    return {"id": eid, "platform": platform, "handle": handle, "url": f"https://x/{eid}", "posted_at": at,
            "market": market, "source_market": source_market, "text": text, "quote_text": text,
            "engagement": {"views": views}, "flags": flags}


def islamicvideo_pack():
    """Three posts found in Nigeria's feeds; the Ghana location leaves two local and showable under the veto."""
    return {
        "evidence": [
            ng_post("ev_gh", "@deen_daily", "Short reminder on patience in hard times, share it", market="GH",
                    views=90000),
            ng_post("ev_a", "@ikeja_deen", "Friday reminder before Jumat prayers today", views=4000),
            ng_post("ev_b", "@kano_reminders", "Jumat reminder clip for everyone heading to the mosque",
                    platform="instagram", views=3000),
        ],
        "numbers": [{"value": 9, "unit": "creators in 3 days", "query_id": "q_c", "run_id": "r_1",
                     "result_hash": "sha256:aa"}],
        "facts": ["State: rising"],
    }


QUOTES = {"ev_gh": "Short reminder", "ev_a": "Friday reminder", "ev_b": "Jumat reminder"}


def writer_heads(prompt):
    """The code-written field line of each post in a writer prompt, outside the fences."""
    return [json.loads(fields) for fields in re.findall(r"^post (\{.*\})$", outside_fences(prompt), flags=re.M)]


def draft_citing(first, second):
    return {
        "explanation": "Two creators post short reminder clips, likely because Friday prayers set the timing.",
        "explanation_claim_ids": ["c1", "c3"],
        "claims": [
            {"id": "c1", "text": f'Creators post short reminder clips, one saying "{QUOTES[first]}".',
             "label": "observed", "kind": "observation", "evidence_ids": [first, second],
             "quotes": [{"evidence_id": first, "text": QUOTES[first]}], "number_ids": []},
            {"id": "c2", "text": "A cited clip is a short reminder.", "label": "single_source",
             "kind": "observation", "evidence_ids": [second], "quotes": [], "number_ids": []},
            {"id": "c3", "text": "Friday prayers likely explain the timing.", "label": "inferred",
             "kind": "interpretation", "evidence_ids": [first, second], "quotes": [], "number_ids": []},
        ],
    }


class CitesWhatItIsShown(FakeModel):
    """A writer that cites the first two posts its prompt offers as citable, in prompt order (posts come most
    engaged first). A post without a citable field counts as citable, as the old prompt implied."""

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if "explanation_claim_ids" in schema.get("properties", {}):
            offered = [h["id"] for h in writer_heads(user) if h.get("citable", True) is not False]
            self.drafts = [draft_citing(*offered[:2])]
        return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)


def run_ng(model, pack):
    return explain_trend(CANDIDATE, pack, model=model, spent_today_usd=0.0, window_start=W_START,
                         window_end=W_END, market="NG")


# Place: a post located in another market is never offered to the writer as citable


def test_the_fixture_counts_exclude_the_known_foreign_feed_post():
    assert holds_report.post_counts(islamicvideo_pack()["evidence"], "NG") == (3, 2, 2)


def test_a_writer_that_cites_what_it_is_offered_no_longer_cites_a_post_located_in_another_market():
    model = CitesWhatItIsShown()
    result = run_ng(model, islamicvideo_pack())

    k3_cuts = [r for r in result["checks"] if r["rule"] == "K3" and r["verdict"] == "cut"]
    assert k3_cuts == [], failed_reason({**result, "rests_on": ["c1", "c3"]})
    assert result["numbers_only"] is False and result["reason"] is None
    cited = {e for c in result["claims"] for e in c["evidence_ids"]}
    assert "ev_gh" not in cited
    assert len(model.writer_calls()) == 1


def test_the_writer_sees_a_post_located_elsewhere_as_not_citable_and_never_its_text():
    pack = islamicvideo_pack()
    user = explain._writer_user(CANDIDATE, pack, "NG", W_START, W_END)
    by_id = {h["id"]: h for h in writer_heads(user)}

    assert by_id["ev_gh"]["citable"] is False
    assert by_id["ev_a"]["citable"] is True and by_id["ev_b"]["citable"] is True
    assert "Short reminder on patience" not in user
    assert "Friday reminder before Jumat prayers today" in user


def test_the_place_check_still_cuts_a_writer_that_cites_the_barred_post_anyway():
    stubborn = draft_citing("ev_gh", "ev_a")
    model = FakeModel([stubborn, stubborn])
    result = run_ng(model, islamicvideo_pack())

    assert result["numbers_only"] is True and result["reason"] == "failed_checks"
    assert any(r["rule"] == "K3" and r["verdict"] == "cut" and "ev_gh" in r["detail"]
               and not r["detail"].startswith("before repair") for r in result["checks"])


def test_the_repair_round_names_the_barred_posts_and_asks_for_citable_ones_instead():
    model = FakeModel([draft_citing("ev_gh", "ev_a"), draft_citing("ev_a", "ev_b")])
    result = run_ng(model, islamicvideo_pack())

    repair = model.writer_calls()[1]["user"]
    rest = outside_fences(repair)
    assert '["ev_gh"]' in rest
    assert "located in another market" in rest
    assert "cite a post marked citable true instead, or cut the claim" in rest
    assert result["numbers_only"] is False and result["reason"] is None


def test_the_repair_round_adds_no_barred_line_when_the_draft_cites_no_barred_post():
    model = FakeModel([dict(draft_citing("ev_a", "ev_b"), explanation_claim_ids=["c9"]),
                       draft_citing("ev_a", "ev_b")])
    run_ng(model, islamicvideo_pack())
    assert "located in another market:" not in outside_fences(model.writer_calls()[1]["user"])


def test_the_pack_and_every_check_record_are_left_as_they_were():
    pack = islamicvideo_pack()
    before = copy.deepcopy(pack)
    model = FakeModel([draft_citing("ev_gh", "ev_a"), draft_citing("ev_gh", "ev_a")])
    run_ng(model, pack)
    assert pack == before


# Local why-now: the writer is told which posts are local and located, and what the critic accepts


def located_pack():
    pack = islamicvideo_pack()
    pack["evidence"].append(ng_post("ev_ng", "@abuja_deen", "Jumat reminder from our masjid this Friday",
                                    market="NG", views=2000))
    pack["evidence"].append(ng_post("ev_ke", "@nairobi_deen", "Reminder clip for the week", source_market="KE",
                                    views=1000))
    return pack


def test_the_writer_is_told_which_citable_posts_are_local_and_which_are_located_in_the_market():
    user = explain._writer_user(CANDIDATE, located_pack(), "NG", W_START, W_END)
    marks = {h["id"]: (h["citable"], h.get("local"), h.get("located_in_market")) for h in writer_heads(user)}
    assert marks == {"ev_gh": (False, None, None), "ev_a": (True, True, False), "ev_b": (True, True, False),
                     "ev_ng": (True, True, True), "ev_ke": (True, False, False)}
    [scope] = [line for line in outside_fences(user).splitlines() if line.startswith("Posts you may cite")]
    assert '["ev_a", "ev_b", "ev_ng", "ev_ke"]' in scope
    assert 'marked local true: ["ev_a", "ev_b", "ev_ng"]' in scope
    assert 'marked located_in_market true: ["ev_ng"]' in scope
    assert "citable false" in scope


def writer_text():
    return " ".join(explain.WRITER_SYSTEM.split())


def test_the_writer_cites_only_citable_posts():
    assert ("13 Cite only posts marked citable true. A post marked citable false is located in another market, and "
            "the place check cuts any claim that cites it, whatever its wording, feed wording included."
            ) in explain.WRITER_SYSTEM.splitlines()


def test_the_writer_states_a_timely_local_why_now_in_the_sentence_on_local_posts():
    text = writer_text()
    assert "Put the why-now in the explanation sentence itself as its one hedged clause" in text
    assert ("name a timely local cause: an event, date, release, announcement, match, holiday or moment that a cited "
            "local post names in its own words or that its posted_at dates") in text
    assert ("Popularity, growth, engagement, a trend being discussed, or a country or community label is not a "
            "why-now.") in text
    assert "preferring posts marked located_in_market true" in text
    assert "When no cited local post shows a timely cause, do not invent one." in text


def test_the_writer_shows_what_the_evidence_holds_against_a_simpler_explanation():
    text = writer_text()
    for menu in ("a paid or sponsored push", "a platform feature change", "a coordinated push",
                 "a news or scheduled event", "a collection artefact", "one viral post or one creator"):
        assert menu in text
    assert ("different creators posting in their own words, posts without sponsored flags, posts on different days "
            "or platforms") in text
    assert "two distinct local posts as concrete examples" in text
    assert "by two different creators where the pack has them" in text


def test_the_writer_is_told_what_a_news_driven_card_needs():
    text = writer_text()
    assert explain.NEWS_MIN_CREATORS == 2
    assert ("When the posts respond to a news or scheduled event, say so plainly in the sentence and rest it on "
            "claims that cite at least two different local creators reacting in their own words, such as opinions, "
            "jokes or personal stories, and quote one such reaction.") in text
    assert "Posts that only repeat, quote or share the news show no local reaction." in text


def test_the_writer_states_events_only_as_its_cited_posts_state_them():
    assert ("State an event, person or fact only as a cited post's own text or fields state it; what you know from "
            "outside the pack is not evidence.") in writer_text()


def test_the_new_writer_lines_carry_no_nationality_or_age_framing():
    banned = re.compile(r"\b(?:nigerians?|kenyans?|south africans?|ghanaians?|gen ?z|millennials?|boomers?|youth|"
                        r"young|teens?|teenagers?|older|elderly|generation)\b", re.I)
    lines = explain.WRITER_SYSTEM.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("13 "))
    assert not [line for line in lines[start:] if banned.search(line)]


# BR-1 (4 Oct): an own-feed-only pack lost cards when the writer placed the cause "in Kenya" instead of wording it
# as seen in Kenya's feeds. The writer is now told so in law 12 and in its scope line; the checks are unchanged.

OWN_FEED_LINE = ("When every cited local post a why-now clause rests on is marked located_in_market false, word that "
                 "clause as seen in the market's feeds, such as \"seen in Kenya's feeds\", and name no place inside "
                 "the market as the cause.")


def test_law_12_tells_the_writer_to_word_an_own_feed_only_why_now_as_seen_in_the_markets_feeds():
    [law] = [line for line in explain.WRITER_SYSTEM.splitlines() if line.startswith("12 ")]
    assert law.endswith(OWN_FEED_LINE)
    assert law.startswith("12 source_market is the market of the feed where 42 found a post, not the physical "
                          "location of its author.")
    assert explain.WRITER_SYSTEM.count(OWN_FEED_LINE) == 1


def own_feed_only_pack():
    return {
        "evidence": [
            ng_post("ev_feed_a", "@instablog9ja", "Jumat reminder before prayers today"),
            ng_post("ev_feed_b", "@lindaikejiblogofficial", "Friday reminder clip for everyone"),
            ng_post("ev_ng", "@abuja_deen", "Jumat reminder from our masjid this Friday", market="NG"),
        ],
        "numbers": [],
        "facts": ["State: rising"],
    }


def test_the_writer_scope_names_the_own_feed_posts_and_their_feed_wording():
    user = explain._writer_user(CANDIDATE, own_feed_only_pack(), "NG", W_START, W_END)
    [scope] = [line for line in outside_fences(user).splitlines() if line.startswith("Posts you may cite")]
    assert 'marked located_in_market true: ["ev_ng"]' in scope
    assert ('Of the local posts, found in Nigeria\'s feeds with location unknown: ["ev_feed_a", "ev_feed_b"]; '
            "a why-now resting only on them is worded as seen in Nigeria's feeds and names no place in Nigeria as "
            "the cause.") in scope


def test_the_writer_scope_names_no_own_feed_posts_when_every_local_post_is_located():
    pack = own_feed_only_pack()
    pack["evidence"] = pack["evidence"][2:]
    user = explain._writer_user(CANDIDATE, pack, "NG", W_START, W_END)
    [scope] = [line for line in outside_fences(user).splitlines() if line.startswith("Posts you may cite")]
    assert "found in Nigeria's feeds with location unknown: []" in scope


def test_the_place_check_already_cuts_the_cause_placed_in_the_market_on_own_feed_posts_and_passes_feed_wording():
    """What the writer is now told is what the code checks already enforce; no check changed."""
    from core.trust.claims import place_fault

    records = own_feed_only_pack()["evidence"][:2]
    assert place_fault("Reminders in Nigeria likely follow Friday prayers.", records)
    assert place_fault("Seen in Nigeria's feeds, reminders likely follow Friday prayers.", records) is None
