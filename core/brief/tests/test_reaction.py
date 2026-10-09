"""N24 reaction records. The evidence records are built by the real core.brief.evidence._record from pack-shaped
rows, so a field the pack emits and the record does not carry fails here. Lane 4 places this at
core/brief/tests/test_reaction.py. The one-line change to _record that test N-00 asks for is in section 20."""

import copy
from datetime import datetime, timedelta, timezone

from core.brief import evidence
from core.brief.reaction import creator_key, reacting_creators, reaction_records, span_hash
from core.brief.specificity import local_posts

W = ("2026-10-01", "2026-10-07")
NG = timezone(timedelta(hours=1))


def row(pid, handle, text, platform="tiktok", *, outlet=False, sponsored=False, flagged=False, geo="NG"):
    """A row as the pack statement of appendix G returns it."""
    return {"post_id": pid, "platform": platform, "url": f"https://example.invalid/{pid}",
            "published_at": datetime(2026, 10, 6, 9, tzinfo=timezone.utc), "creator_tier": "micro",
            "geo_market": geo, "geo_confidence": 0.9 if geo else None, "geo_source": "ext_region" if geo else None,
            "source_market": None, "quote_text": text, "views": 10, "likes": 1, "comments": 0, "shares": 0,
            "thumbnail_url": None, "duration_s": None, "handle": handle, "flagged": flagged, "sponsored": sponsored,
            "near_dup": False, "sponsor_checked": True, "is_outlet": outlet}


ROWS = [
    row("p1", "@Ada", "I cannot believe the finale ended like that, my whole street is shouting"),
    row("p2", "ada", "second post by the same creator, still shouting about the finale"),
    row("p3", "bola", "Winner announced and honestly I called it weeks ago"),
    row("p4", "@bellanaija", "BREAKING the winner has been announced tonight", platform="twitter", outlet=True, geo=None),
    row("p5", "chi", "BREAKING the winner has been announced tonight", sponsored=True),
    row("p6", "dayo", "Winner announced and honestly I called it weeks ago and more"),
    row("p7", "efe", "loving this brand giveaway so much honestly", sponsored=True),
]
CLAIMS = [
    {"id": "c1", "evidence_ids": ["p1", "p2", "p3"], "quotes": [
        {"evidence_id": "p1", "text": "my whole street is shouting"},
        {"evidence_id": "p2", "text": "still shouting about the finale"},
        {"evidence_id": "p3", "text": "honestly I called it weeks ago"}]},
    {"id": "c3", "evidence_ids": ["p7"], "quotes": [{"evidence_id": "p7", "text": "loving this brand giveaway so much"}]},
    {"id": "c2", "evidence_ids": ["p4", "p5"], "quotes": [
        {"evidence_id": "p4", "text": "the winner has been announced tonight"},
        {"evidence_id": "p5", "text": "the winner has been announced tonight"}]},
]


def records(rows=ROWS):
    return [evidence._record(r, NG) for r in rows]


def build(**over):
    ev = over.pop("evidence", None) or records()
    args = dict(claims=CLAIMS, rests_on={"c1", "c2", "c3"}, supported_claim_ids={"c1", "c2", "c3"}, evidence=ev,
                market="NG", window=W, event_kind="scheduled", local_ids={r["id"] for r in local_posts(ev, "NG")})
    args.update(over)
    return reaction_records(**args)


def test_N00_the_evidence_record_carries_the_pack_rows_outlet_class():
    recs = {r["id"]: r for r in records()}
    assert recs["p4"]["outlet_class"] == "outlet" and recs["p1"]["outlet_class"] == "creator"


def test_two_posts_by_one_creator_count_as_one_and_outlets_and_paid_posts_never_count():
    out = build()
    assert sorted(r["post_id"] for r in out) == ["p1", "p2", "p3"]
    assert reacting_creators(out) == 2          # ada twice, bola once


def test_each_record_carries_event_span_hash_creator_market_window():
    r = build()[0]
    assert set(r) == {"record_version", "post_id", "creator_key", "market", "window", "event_kind", "span_hash",
                      "claim_id", "support"}
    assert r["span_hash"] == span_hash("my whole street is shouting") and len(r["span_hash"]) == 64
    assert r["creator_key"] == "tiktok:ada" and r["window"] == list(W) and r["market"] == "NG"


def test_a_claim_the_sentence_does_not_rest_on_or_the_support_check_did_not_pass_adds_nothing():
    assert build(rests_on={"c2", "c3"}) == []
    assert build(supported_claim_ids={"c2", "c3"}) == []


def test_a_quote_that_is_not_in_the_post_adds_nothing():
    bad = copy.deepcopy(CLAIMS)
    bad[0]["quotes"][0]["text"] = "my whole street is cheering"
    assert sorted(r["post_id"] for r in build(claims=bad)) == ["p2", "p3"]


def test_a_post_not_local_to_the_market_adds_nothing():
    assert reacting_creators(build(local_ids={"p1", "p2"})) == 1


def test_a_span_that_repeats_an_outlet_post_adds_nothing():
    claims = [{"id": "c1", "evidence_ids": ["p1", "p6"], "quotes": [
        {"evidence_id": "p1", "text": "the winner has been announced tonight"}]}]
    rows = copy.deepcopy(ROWS)
    rows[0]["quote_text"] = "RT the winner has been announced tonight lol"
    assert build(claims=claims, rests_on={"c1"}, supported_claim_ids={"c1"}, evidence=records(rows)) == []


def test_the_same_handle_on_two_platforms_is_two_creators():
    rows = copy.deepcopy(ROWS)
    rows[1]["platform"], rows[1]["handle"] = "instagram", "ada"
    assert reacting_creators(build(evidence=records(rows))) == 3


def test_a_post_with_no_handle_cannot_be_counted():
    rows = copy.deepcopy(ROWS)
    rows[2]["handle"] = None
    assert reacting_creators(build(evidence=records(rows))) == 1


def test_span_hash_ignores_spacing_and_width_variants():
    assert span_hash("my  whole\u00a0street is shouting") == span_hash("my whole street is shouting")


def test_a_row_without_the_outlet_column_gets_no_class_and_its_post_never_counts():
    rows = copy.deepcopy(ROWS)
    for r in rows:
        del r["is_outlet"]
    recs = records(rows)
    assert all("outlet_class" not in r for r in recs)
    assert build(evidence=recs) == []


def test_the_outlet_class_is_the_only_key_the_record_gains():
    with_col = records()
    without = records([{k: v for k, v in r.items() if k != "is_outlet"} for r in ROWS])
    assert [set(a) - set(b) for a, b in zip(with_col, without)] == [{"outlet_class"}] * len(ROWS)


def test_a_twitter_handle_and_an_x_handle_are_one_creator_key():
    assert creator_key("twitter", "@Ada") == "x:ada" == creator_key("x", "ada")


def test_a_quote_on_a_post_the_claim_does_not_cite_adds_nothing():
    claims = [{"id": "c1", "evidence_ids": ["p1"], "quotes": [
        {"evidence_id": "p1", "text": "my whole street is shouting"},
        {"evidence_id": "p3", "text": "honestly I called it weeks ago"}]}]
    out = build(claims=claims, rests_on={"c1"}, supported_claim_ids={"c1"})
    assert [r["post_id"] for r in out] == ["p1"]


def test_span_hash_folds_width_variants_to_the_plain_form():
    assert span_hash("\uff53\uff48\uff4f\uff55\uff54\uff49\uff4e\uff47") == span_hash("shouting")
    assert span_hash("\ufb01nale") == span_hash("finale")
