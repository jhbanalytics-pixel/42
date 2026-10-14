import copy
import json
from datetime import date
from pathlib import Path

import pytest

from core.brief import job, same_event
from core.brief.tests.test_brief_payload import cand, decision

D = date(2026, 10, 12)
CITY = "Fans reacted, likely because Manchester City defeated Liverpool in the Premier League on 11 Oct."
CITY_PLAYED = "Fans reacted, likely because Man City played Liverpool in the Premier League on 11 Oct."
MTN = "Fans reacted, likely because Mamelodi Sundowns defeated Orlando Pirates in the MTN8 final on 10 Oct."


def anchor(text, market="NG", checked=True):
    return same_event.fixture_anchor(text, market, D, checked=checked)


def item(n, text=CITY, creators=10, market="NG", checked=True):
    c = cand(n, title=f"topic {n}", explanation=text)
    c["numbers"][0]["value"] = creators
    c["_event_checked"] = checked
    c["market"] = market
    return c


def publish(items, posts=None, market="NG"):
    return job._publish_events(market, D, items, posts or {c["item_id"]: {"p1", "p2", "p3"} for c in items})


@pytest.fixture(autouse=True)
def admit(monkeypatch):
    monkeypatch.setattr(job, "_today_admits", lambda *args: True)


def test_checked_dated_fixture_accepts_played_and_defeated():
    assert anchor(CITY) is not None
    assert same_event.fixture_reason(anchor(CITY), anchor(CITY_PLAYED)) is not None
    assert same_event.fixture_reason(anchor(MTN, "ZA"), anchor(MTN.replace("defeated", "beat"), "ZA"))


@pytest.mark.parametrize("text", [
    CITY.replace("defeated Liverpool", "and Liverpool boycotted"),
    CITY.replace("defeated Liverpool", "and Liverpool cancelled"),
    CITY.replace("defeated", "signed"),
    CITY.replace(" on 11 Oct", ""),
    "Fans posted on 11 Oct, likely because Manchester City defeated Liverpool in the Premier League.",
    CITY.replace(" on 11 Oct", " on 11 Oct and 12 Oct"),
    CITY.replace("Liverpool", "Liverpool and Chelsea"),
    "Fans reacted, likely because Burna Boy performed at a show on 11 Oct.",
])
def test_unsupported_ambiguous_or_undated_anchor_is_refused(text):
    assert anchor(text) is None


def test_unchecked_and_malformed_anchors_are_refused():
    assert anchor(CITY, checked=False) is None
    assert anchor(None) is None
    for value in (None, {}, {"kind": "sports_fixture"}, "event", []):
        assert same_event.fixture_reason(anchor(CITY), value) is None


@pytest.mark.parametrize("text,market", [
    (CITY.replace("11 Oct", "10 Oct"), "NG"),
    (CITY.replace("Liverpool", "Arsenal"), "NG"),
    (CITY.replace("Premier League", "MTN8 final"), "NG"),
    (CITY.replace("Liverpool", "Liverpool Women"), "NG"),
    (CITY, "ZA"),
    (CITY.replace("League", "League semi-final"), "NG"),
    (CITY.replace("Manchester City defeated Liverpool", "Liverpool defeated Manchester City"), "NG"),
])
def test_distinct_fixture_dimensions_refuse_merge(text, market):
    assert same_event.fixture_reason(anchor(CITY), anchor(text, market)) is None


def test_conflicting_explicit_venues_refuse_merge():
    a = anchor(CITY.replace("on 11 Oct", "at Anfield on 11 Oct"))
    b = anchor(CITY.replace("on 11 Oct", "at Wembley on 11 Oct"))
    assert a is not None and b is not None
    assert same_event.fixture_reason(a, b) is None


def test_explicitly_different_actions_or_dates_are_not_possible_fragment_holds():
    a = item(1, MTN, market="ZA")
    for text in (MTN.replace("defeated Orlando Pirates", "and Orlando Pirates boycotted").replace(" on 10 Oct", ""),
                 MTN.replace("10 Oct", "11 Oct"), MTN.replace("final", "semi-final").replace(" on 10 Oct", "")):
        b = item(2, text, market="ZA")
        kept, receipt = publish([a, b], market="ZA")
        assert len(kept) == 2
        assert all(job._published(i) for i in kept)
        assert receipt["merges"] == receipt["possible_fragments"] == []


def test_publication_keeps_ranked_representative_without_unioning_authority():
    a, b = item(1, creators=5), item(2, CITY_PLAYED, creators=20)
    before = copy.deepcopy(b)
    items, receipt = publish([a, b])
    assert [c["item_id"] for c in items] == [b["item_id"]]
    for key in ("claims", "explanation_claim_ids", "numbers", "evidence", "specificity", "explanation"):
        assert items[0].get(key) == before.get(key)
    assert items[0]["also"] == [{"item_id": a["item_id"], "title": a["title"]}]
    assert b == before
    assert receipt["merges"][0]["kept"] == b["item_id"]


def test_post_overlap_held_unchecked_and_nonadmitted_candidates_cannot_merge(monkeypatch):
    a, b = item(1), item(2, CITY_PLAYED)
    assert len(publish([a, b], {a["item_id"]: {"p1", "p2", "p3"}, b["item_id"]: {"p1", "p2", "p4"}})[0]) == 2
    b["decision"] = decision("held_back")
    assert len(publish([a, b])[0]) == 2
    b["decision"] = decision()
    b["_event_checked"] = False
    assert len(publish([a, b])[0]) == 2
    b["_event_checked"] = True
    monkeypatch.setattr(job, "_today_admits", lambda c, *args: c["item_id"] != b["item_id"])
    assert len(publish([a, b])[0]) == 2


def test_graph_bridge_cannot_join_disjoint_evidence_groups():
    a, b, c = item(1, creators=30), item(2, creators=20), item(3, creators=10)
    posts = {a["item_id"]: {"p1", "p2", "p3"}, b["item_id"]: {"p1", "p2", "p3", "q1", "q2", "q3"},
             c["item_id"]: {"q1", "q2", "q3"}}
    items, receipt = publish([a, b, c], posts)
    assert [i["item_id"] for i in items] == [a["item_id"], c["item_id"]]
    assert len(receipt["merges"]) == 1


def test_backfill_candidates_are_grouped_before_the_final_shown_limit():
    items = [item(n) for n in range(1, 13)]
    kept, receipt = publish(items)
    assert len(kept) == 1
    assert len(kept[0]["also"]) == 11
    assert receipt["judged"] == 12


def test_candidate_from_another_market_cannot_lend_identity():
    a, b = item(1), item(2, market="ZA")
    kept, receipt = publish([a, b])
    assert len(kept) == 2
    assert receipt["merges"] == receipt["possible_fragments"] == []


def test_retained_oct12_replay_merges_only_explicit_dated_fixture_anchors():
    data = json.loads((Path(__file__).parent / "fixtures" / "same_event_replay.json").read_text(encoding="utf-8"))
    brief = next(b for b in data["briefs"] if b["brief_date"] == "2026-10-12" and b["market"] == "NG")
    items, posts = [], {}
    for n, c in enumerate(brief["cards"], 1):
        i = item(n, c["explanation"], c["creators3"])
        i.update(item_id=c["item_id"], title=c["title"], also=c["also"])
        items.append(i)
        posts[i["item_id"]] = set(c["member_posts"])
    kept, receipt = publish(items, posts)
    assert len(kept) < len(items)
    assert receipt["merges"]
    assert all(m["item_id"][:10] in {"ad7f67c05b", "59090e1b52", "9cfaabf463", "3791be7f80"}
               for m in receipt["merges"])
    assert len(receipt["merges"]) == 1
    assert {p["item_id"][:10] for p in receipt["possible_fragments"]} == {"3791be7f80", "ad7f67c05b"}
    assert len([i for i in kept if job._published(i)]) == 4


def test_retained_za_undated_fragments_are_held_without_asserting_same_event():
    data = json.loads((Path(__file__).parent / "fixtures" / "same_event_replay.json").read_text(encoding="utf-8"))
    brief = next(b for b in data["briefs"] if b["brief_date"] == "2026-10-12" and b["market"] == "ZA")
    items, posts = [], {}
    for n, c in enumerate(brief["cards"], 1):
        i = item(n, c["explanation"], c["creators3"], market="ZA")
        i.update(item_id=c["item_id"], title=c["title"], also=c["also"])
        items.append(i)
        posts[i["item_id"]] = set(c["member_posts"])
    kept, receipt = publish(items, posts, "ZA")
    assert receipt["merges"] == []
    assert len(receipt["possible_fragments"]) == 3
    assert [i["item_id"] for i in kept if job._published(i)] == [items[0]["item_id"]]
    held = [i for i in kept if not job._published(i)]
    assert all(i["event_fragment"]["reference_item_id"] == items[0]["item_id"] for i in held)
    assert all(i["also"] == next(o["also"] for o in items if o["item_id"] == i["item_id"]) for i in held)


def test_possible_fragment_preserves_evidence_and_uncertainty_without_new_public_claim_text():
    from core.brief.payload import build_market_payload
    a, b = item(1, MTN, market="ZA"), item(2, MTN.replace(" on 10 Oct", ""), market="ZA")
    original = copy.deepcopy(b)
    items, receipt = publish([a, b], market="ZA")
    payload = build_market_payload("ZA", D, items, moments=[], boards=[], banners=[])
    assert [c["item_id"] for c in payload["cards"]] == [a["item_id"]]
    held = payload["held_back"]["items"][0]
    assert held["reason"] == "not_confirmed"
    assert held["event_fragment"]["status"] == "possible_duplicate"
    assert held["event_fragment"]["missing"] == ["event_date"]
    for key in ("evidence", "numbers"):
        assert held[key] == original[key]
    assert not {"claims", "explanation_claim_ids", "explanation"} & set(held)
    candidate = next(i for i in items if i["item_id"] == b["item_id"])
    assert candidate["claims"] == original["claims"]
    assert candidate["explanation"] == original["explanation"]
    assert payload["cards"][0]["also"] == []


def test_full_job_applies_publication_after_g10_and_preserves_headline_binding():
    from core.brief.tests.test_event_merge import EventModel, FakeConfirm, all_cards, brief, payload, two_items, world
    con = world(n=0)
    two_items(con)
    first = MTN.replace("defeated", "beat")
    r = brief(con, model=EventModel({"mtn8 final": first, "sundowns pirates": MTN}), confirm=FakeConfirm())
    market = payload(r, "ZA")
    assert [c["item_id"] for c in all_cards(market)] == ["za_b"]
    assert market["cards"][0]["also"] == [{"item_id": "za_a", "title": "mtn8 final"}]
    assert market["headline"]["item_id"] == "za_b"
    assert market["headline"]["claim_ids"] == market["cards"][0]["explanation_claim_ids"]
    assert market["event_publication"]["merges"][0]["item_id"] == "za_a"


def test_writer_requests_explicit_cause_date_without_inheriting_posted_at():
    from core.brief.explain import WRITER_SYSTEM
    assert "Preserve an explicit event date from cited post text in the why-now clause" in WRITER_SYSTEM
    assert "Never substitute posted_at or the collection window for an event date" in WRITER_SYSTEM


@pytest.mark.parametrize("lead", ["Fans of the women's match reacted", "Fans of the semi-final reacted"])
def test_adversarial_lead_qualifiers_apply_to_fixture_and_publication(lead):
    text = CITY.replace("Fans reacted", lead)
    assert same_event.fixture_reason(anchor(CITY), anchor(text)) is None
    kept, receipt = publish([item(1), item(2, text)])
    assert len(kept) == 2
    assert receipt["merges"] == []


@pytest.mark.parametrize("text", [
    "Fans reacted, likely because Manchester City defeated Liverpool in the Premier League; posts surged on 11 Oct.",
    "Fans reacted, likely because Manchester City defeated Liverpool in the Premier League and posts surged on 11 Oct.",
    "Fans reacted, likely because Manchester City defeated Liverpool in the Premier League and discussion peaked on 11 Oct.",
    "Fans reacted, likely because Manchester City defeated Liverpool in the Premier League and engagement spiked on 11 Oct.",
    "Fans reacted, likely because Manchester City defeated Liverpool in the Premier League during the season that began on 11 Oct.",
    "The Chelsea match was held on 11 Oct. Fans reacted, likely because Manchester City defeated Liverpool in the Premier League.",
    "Fans reacted, likely because Manchester City defeated Liverpool in the Premier League on 11 Oct while Liverpool defeated Manchester City in the reverse fixture on 11 Oct.",
])
def test_adversarial_posting_time_and_reverse_fixtures_refused(text):
    assert anchor(text) is None


def test_adversarial_held_projection_cannot_keep_suppressed_post_words(monkeypatch):
    from core.api import today
    from core.brief.payload import build_market_payload
    a, b = item(1, MTN, market="ZA"), item(2, MTN.replace(" on 10 Oct", ""), market="ZA")
    hidden_id = b["evidence"][0]["id"]
    b["evidence"][0].update(platform="tiktok", handle="synthetic_hidden", text="distinct private sentence")
    b["claims"][0].update(text="distinct private sentence", evidence_ids=[hidden_id])
    b["explanation"] = "distinct private sentence. " + b["explanation"]
    items, receipt = publish([a, b], market="ZA")
    assert receipt["possible_fragments"]
    assert next(i for i in items if i["item_id"] == b["item_id"])["claims"] == b["claims"]
    payload = build_market_payload("ZA", D, items, moments=[], boards=[], banners=[])
    projected = today.without_hidden(payload, ({"tiktok:synthetic_hidden"}, set(), set()))
    held = projected["held_back"]["items"][0]
    assert hidden_id not in [e["id"] for e in held["evidence"]]
    assert "distinct private sentence" not in json.dumps(today._held_item(held, {}))


def test_adversarial_full_today_omits_new_held_claim_text():
    from core.api import today
    from core.api.tests.test_today_suppression import Patched, FixtureStore, D30, HIDDEN_POST, hidden_record
    from core.brief.payload import build_market_payload
    a, b = item(1, MTN, market="ZA"), item(2, MTN.replace(" on 10 Oct", ""), market="ZA")
    b["evidence"] = [hidden_record()]
    b["claims"][0].update(text="distinct private sentence", evidence_ids=[HIDDEN_POST])
    b["explanation"] = "distinct private sentence. " + b["explanation"]
    items, receipt = publish([a, b], market="ZA")
    assert receipt["possible_fragments"]
    produced = build_market_payload("ZA", D, items, moments=[], boards=[], banners=[])["held_back"]["items"][0]
    base = FixtureStore()
    rows = copy.deepcopy(base.briefs(D30))
    row = next(r for r in rows if r["market"] == "ZA")
    row["payload"]["held_back"]["items"].append(produced)
    store = Patched(briefs=lambda day: copy.deepcopy(rows) if day == D30 else base.briefs(day))
    response = today.build_today(store, D30)
    held = next(h for m in response["markets"] if m["market"] == "ZA"
                for h in m["held_back"]["items"] if h["item_id"] == b["item_id"])
    assert not held["evidence"]
    assert "distinct private sentence" not in json.dumps(held)


RETAINED_14OCT_FIXTURES = [{'item_id': 'ce51ad79cd95fe086405298693bd01078d7159812559515d235297574e0045f5',
  'title': '#orlandopirates',
  'explanation': 'South African football creators reacted on TikTok to matches involving Orlando '
                 'Pirates, likely because the MTN8 final and Betway Premiership fixtures took '
                 'place between 10 Oct and 13 Oct.',
  'critic': {'scheduled_event': True,
             'local_reaction': True,
             'local_why_now': True,
             'news_driven': False,
             'ruled_out': False,
             'reason': 'The posts directly coincide with scheduled South African football '
                       'fixtures, featuring distinct personal reactions and banter from local '
                       'creators.'}},
 {'item_id': 'afa961b7335738b68a3f85c6178f633fa6a688caf7dbe81dfcc57bc15a5db1d2',
  'title': '#sundowns',
  'explanation': 'South African creators reacted to the MTN8 football final on TikTok, likely '
                 'because Mamelodi Sundowns defeated Orlando Pirates to claim the cup.',
  'critic': {'scheduled_event': True,
             'local_reaction': True,
             'local_why_now': True,
             'news_driven': False,
             'ruled_out': False,
             'reason': 'The posts directly discuss and react to the scheduled MTN8 cup final match '
                       'between Mamelodi Sundowns and Orlando Pirates with personal reactions from '
                       'local supporters.'}}]


@pytest.mark.parametrize("retained", RETAINED_14OCT_FIXTURES, ids=["pirates", "sundowns"])
def test_retained_14oct_primary_fixture_reaction_is_held_without_a_dated_anchor(retained):
    from core.brief.payload import build_market_payload

    candidate = item(1, retained["explanation"], market="ZA")
    candidate.update(item_id=retained["item_id"], title=retained["title"], critic=retained["critic"], also=[])
    before = copy.deepcopy(candidate)
    kept, receipt = publish([candidate], market="ZA")
    assert len(kept) == 1 and not job._published(kept[0])
    assert kept[0]["decision"].rule == "G3"
    assert kept[0]["held_reason"] == "not_confirmed"
    assert receipt["merges"] == receipt["possible_fragments"] == []
    for key in ("numbers", "evidence", "claims", "explanation", "explanation_claim_ids", "also"):
        assert kept[0][key] == before[key]
    assert "event_fragment" not in kept[0]
    assert candidate == before
    payload = build_market_payload("ZA", D, kept, moments=[], boards=[], banners=[])
    assert payload["cards"] == payload["more"] == []
    held = payload["held_back"]["items"][0]
    assert held["reason"] == "not_confirmed"
    assert held["numbers"] == before["numbers"] and held["evidence"] == before["evidence"]
    assert not {"claims", "explanation", "explanation_claim_ids", "event_fragment"} & set(held)


@pytest.mark.parametrize("text", [
    "South African creators made handmade jerseys for the MTN8 final, likely because they share a weekly craft ritual.",
    "South African creators reacted to new jersey designs before the MTN8 final, likely because supporters customize shirts.",
    "South African creators reacted to MTN8 fan art, likely because supporters share drawings after matches.",
    "South African creators reacted to the MTN8 final by making handmade jerseys, likely because supporters share a weekly craft ritual.",
    "South African creators reacted to the MTN8 final celebrations, likely because supporters share a weekly dance ritual.",
])
def test_football_behavioral_explanation_is_not_held_by_fixture_identity(text):
    candidate = item(1, text, market="ZA")
    candidate["critic"] = copy.deepcopy(RETAINED_14OCT_FIXTURES[0]["critic"])
    before = copy.deepcopy(candidate)
    kept, receipt = publish([candidate], market="ZA")
    assert kept == [before] and job._published(kept[0])
    assert receipt["merges"] == receipt["possible_fragments"] == []


@pytest.mark.parametrize("critic", [None, {}, {"scheduled_event": False}, {"scheduled_event": "true"}])
def test_fixture_identity_hold_requires_structured_scheduled_event(critic):
    candidate = item(1, RETAINED_14OCT_FIXTURES[1]["explanation"], market="ZA")
    candidate["critic"] = critic
    kept, _ = publish([candidate], market="ZA")
    assert job._published(kept[0])


def test_dated_distinct_primary_fixture_reactions_stay_independent():
    a = item(1, "South African creators reacted to the MTN8 final, likely because Mamelodi Sundowns defeated Orlando Pirates in the MTN8 final on 10 Oct.", market="ZA")
    b = item(2, "South African creators reacted to the MTN8 final, likely because Mamelodi Sundowns defeated Orlando Pirates in the MTN8 final on 11 Oct.", market="ZA")
    for c in (a, b):
        c["critic"] = copy.deepcopy(RETAINED_14OCT_FIXTURES[1]["critic"])
    kept, receipt = publish([a, b], market="ZA")
    assert len(kept) == 2 and all(job._published(c) for c in kept)
    assert receipt["merges"] == receipt["possible_fragments"] == []


def test_fixture_identity_hold_does_not_change_unchecked_or_nonadmitted_candidates(monkeypatch):
    a = item(1, RETAINED_14OCT_FIXTURES[1]["explanation"], market="ZA", checked=False)
    b = item(2, RETAINED_14OCT_FIXTURES[1]["explanation"], market="ZA")
    for c in (a, b):
        c["critic"] = copy.deepcopy(RETAINED_14OCT_FIXTURES[1]["critic"])
    monkeypatch.setattr(job, "_today_admits", lambda c, *_: c["item_id"] != b["item_id"])
    kept, _ = publish([a, b], market="ZA")
    assert kept == [a, b] and all(job._published(c) for c in kept)


@pytest.mark.parametrize("retained", RETAINED_14OCT_FIXTURES, ids=["pirates", "sundowns"])
def test_runtime_payload_retains_scheduled_critic_for_fixture_identity_hold(retained):
    from core.brief import explain

    row = item(1, retained["explanation"], market="ZA")
    candidate = {"row": row, "decision": row["decision"], "sparkline": [],
                 "pack": {"numbers": row["numbers"], "evidence": row["evidence"]}}
    result = {"reason": None, "numbers_only": False, "explanation": retained["explanation"],
              "local_why_now_checked": True, "critic": explain._critic_answer(retained["critic"]),
              "claims": row["claims"], "explanation_claim_ids": row["explanation_claim_ids"]}
    runtime_item = job._payload_candidate(candidate, result)
    assert runtime_item["critic"]["scheduled_event"] is True and runtime_item["_event_checked"] is True
    kept, _ = publish([runtime_item], market="ZA")
    assert len(kept) == 1 and not job._published(kept[0])
