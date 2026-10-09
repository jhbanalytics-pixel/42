"""Claim rules K1, K2, K3, K5, K6, K8 and K10 (TRUST.md section 2, Stage 1A)."""

import copy
from datetime import date, datetime, timedelta, timezone

import pytest

from core.trust.claims import check_answer, located_market, named_markets

SAST = timezone(timedelta(hours=2))
START = datetime(2026, 9, 21, 0, 0, tzinfo=SAST)
END = datetime(2026, 9, 28, 6, 10, tzinfo=SAST)


def record(
    rid, platform, handle, text, *, market="ZA", posted_at="2026-09-26T19:40:00+02:00", flags=None
):
    return {
        "id": rid,
        "platform": platform,
        "handle": handle,
        "url": f"https://example.test/{rid}",
        "posted_at": posted_at,
        "market": market,
        "text": text,
        "engagement": {},
        "flags": flags or [],
    }


EVIDENCE = [
    record("tt_1", "tiktok", "@thandi", "Ngiyabonga   kakhulu,\nle challenge imnandi!"),
    record(
        "ig_2",
        "instagram",
        "@kagiso",
        "Everyone’s doing the amapiano step and the youth are loving it",
    ),
    record("x_3", "x", "@lagosboy", "Abeg, dis jollof don burst my brain o", market="NG"),
    record("tt_4", "tiktok", "@brandco", "Try our new flavour", flags=["brand"]),
    record("rd_5", "reddit", "@oldtimer", "Old thread", posted_at="2026-08-01T10:00:00+02:00"),
    record("gm_6", "gemini", "gemini", "Summary of the trend", flags=["generated"]),
    record("ig_7", "instagram", "@Thandi", "same creator on another platform"),
    record("yt_8", "youtube", "@sipho", "Watching the step everywhere"),
    record("tt_9", "tiktok", "@lerato", "Doing it too"),
    record("tt_10", "tiktok", "@bongani", "Me three"),
    record("yo_11", "x", "@ade", "Ọmọ mi, é kú iṣé", market="NG"),
    record("tt_12", "tiktok", "@paidpromo", "Sponsored dance", flags=["paid"]),
]

NUM_28 = {
    "value": 2.8,
    "unit": "times usual daily posts",
    "query_id": "q_12",
    "run_id": "r_20260928_0500",
    "result_hash": "sha256:aa",
}
NUM_COUNT = {
    "value": 184000,
    "unit": "views",
    "query_id": "q_13",
    "run_id": "r_20260928_0500",
    "result_hash": "sha256:bb",
}
NUM_SHARE = {
    "value": 0.4,
    "unit": "share of posts",
    "query_id": "q_14",
    "run_id": "r_20260928_0500",
    "result_hash": "sha256:cc",
}


def claim(
    cid, text, evidence_ids, *, label="single_source", kind="observation", quotes=None, numbers=None
):
    return {
        "id": cid,
        "text": text,
        "label": label,
        "kind": kind,
        "evidence_ids": list(evidence_ids),
        "quotes": quotes or [],
        "numbers": numbers or [],
    }


def answer(
    claims,
    *,
    status="complete",
    short_answer="The amapiano step is spreading in Gauteng.",
    so_what=None,
    watch_next=None,
):
    ids = [c["id"] for c in claims]
    return {
        "status": status,
        "as_of": "2026-09-28T06:10:00+02:00",
        "short_answer": short_answer,
        "claims": claims,
        "evidence": copy.deepcopy(EVIDENCE),
        "so_what": so_what
        if so_what is not None
        else [{"text": "Brands can join the step", "claim_ids": ids[:1]}],
        "watch_next": watch_next if watch_next is not None else [],
        "gaps": [],
        "context": "",
    }


def filler(n=2):
    """Clean claims that pass every rule, so status tests are not driven by claim count."""
    return [claim(f"f{i}", "Creators are posting the step", ["yt_8"]) for i in range(n)]


def run(ans, **kw):
    kw.setdefault("window_start", START)
    kw.setdefault("window_end", END)
    return check_answer(ans, **kw)


def verdict(checks, cid, rule):
    rows = [r for r in checks if r["claim_id"] == cid and r["rule"] == rule]
    assert rows, f"no {rule} row for {cid}"
    order = ["breach", "cut", "downgrade", "pass"]
    return sorted((r["verdict"] for r in rows), key=order.index)[0]


def surviving(new):
    return [c["id"] for c in new["claims"]]


# Shape and purity


def test_rows_have_the_claim_checks_shape():
    _, checks = run(answer([claim("c1", "Creators are posting the step", ["yt_8"])] + filler()))
    assert checks
    for row in checks:
        assert set(row) == {"claim_id", "rule", "verdict", "checker", "detail"}
        assert row["checker"] == "code"
        assert row["verdict"] in {"pass", "cut", "downgrade", "breach"}


def test_input_answer_is_not_mutated():
    ans = answer([claim("c1", "Gen Z loves it", ["yt_8"], label="corroborated")] + filler())
    before = copy.deepcopy(ans)
    new, _ = run(ans)
    assert ans == before
    assert new is not ans


def test_windows_accept_iso_strings():
    ans = answer([claim("c1", "Creators are posting the step", ["yt_8"])] + filler())
    _, checks = run(
        ans, window_start="2026-09-21T00:00:00+02:00", window_end="2026-09-28T06:10:00+02:00"
    )
    assert verdict(checks, "c1", "K3") == "pass"


# K1


def test_k1_isizulu_quote_passes_despite_whitespace():
    c = claim(
        "c1",
        "A Durban creator thanks her followers",
        ["tt_1"],
        quotes=[{"evidence_id": "tt_1", "text": "Ngiyabonga kakhulu, le challenge imnandi!"}],
    )
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K1") == "pass"
    assert "c1" in surviving(new)


def test_k1_pidgin_quote_passes_with_straight_versus_curly_quotes():
    ev_text = "Na so e be o, dem don carry the ‘jollof’ wahala"
    c = claim(
        "c1",
        "Lagos posters joke about jollof",
        ["x_3"],
        quotes=[{"evidence_id": "x_3", "text": "dem don carry the 'jollof' wahala"}],
    )
    ans = answer([c] + filler())
    ans["evidence"][2]["text"] = ev_text
    _, checks = run(ans)
    assert verdict(checks, "c1", "K1") == "pass"


def test_k1_yoruba_quote_passes_under_nfkc():
    # The record stores decomposed dots below; the quote uses the composed letters.
    composed = "Ọmọ mi"
    c = claim(
        "c1", "A Yoruba caption", ["yo_11"], quotes=[{"evidence_id": "yo_11", "text": composed}]
    )
    _, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K1") == "pass"


def test_k1_unresolved_evidence_id_is_cut():
    c = claim("c1", "Creators are posting the step", ["tt_999"])
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K1") == "cut"
    assert "c1" not in surviving(new)


def test_k1_claim_without_evidence_is_cut():
    new, checks = run(answer([claim("c1", "Creators are posting the step", [])] + filler()))
    assert verdict(checks, "c1", "K1") == "cut"
    assert "c1" not in surviving(new)


def test_k1_paraphrased_quote_is_cut():
    c = claim(
        "c1",
        "A Durban creator thanks her followers",
        ["tt_1"],
        quotes=[{"evidence_id": "tt_1", "text": "Thank you so much, this challenge is nice"}],
    )
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K1") == "cut"
    assert "c1" not in surviving(new)


def test_k1_quote_from_a_record_the_claim_does_not_cite_is_cut():
    c = claim(
        "c1",
        "Creators are posting the step",
        ["yt_8"],
        quotes=[{"evidence_id": "tt_1", "text": "Ngiyabonga kakhulu"}],
    )
    _, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K1") == "cut"


# K2


def test_k2_numeral_pinned_to_a_query_passes_and_dates_times_years_are_ignored():
    c = claim(
        "c1",
        "Posts using the sound reached 2.8 times usual daily posts on 26 September 2026 by 19:40",
        ["yt_8"],
        numbers=[NUM_28],
    )
    _, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K2") == "pass"


def test_k2_date_range_iso_date_and_sa_clock_time_are_ignored():
    c = claim(
        "c1",
        "Between 21 to 27 September, and again on 2026-09-26 at 07h30 and 7pm, creators posted",
        ["yt_8"],
    )
    _, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K2") == "pass"


def test_k2_numeral_inside_a_verified_quote_is_ignored():
    q = "Day 3 of doing the step"
    c = claim(
        "c1", f"One creator posts “{q}”", ["yt_8"], quotes=[{"evidence_id": "yt_8", "text": q}]
    )
    ans = answer([c] + filler())
    ans["evidence"][7]["text"] = "Day 3 of doing the step, still going"
    _, checks = run(ans)
    assert verdict(checks, "c1", "K1") == "pass"
    assert verdict(checks, "c1", "K2") == "pass"


def test_k2_quoted_words_without_a_quote_entry_are_prose_and_a_k1_cut():
    c = claim("c1", "Creators call it “the 100 step”", ["yt_8"])
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K2") == "cut"
    assert verdict(checks, "c1", "K1") == "cut"
    assert "c1" not in surviving(new)


def test_k2_rounded_display_and_percent_of_a_share_match():
    c = claim(
        "c1",
        "The clip passed 184k views and 40% of posts used the sound",
        ["yt_8"],
        numbers=[dict(NUM_COUNT, value=184213), dict(NUM_SHARE, value=0.4012)],
    )
    _, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K2") == "pass"


def test_k2_numeral_without_a_numbers_entry_is_cut():
    c = claim("c1", "The clip passed 184,000 views", ["yt_8"])
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K2") == "cut"
    assert "c1" not in surviving(new)


def test_k2_numbers_entry_without_result_hash_is_cut():
    entry = {k: v for k, v in NUM_COUNT.items() if k != "result_hash"}
    c = claim("c1", "The clip passed 184,000 views", ["yt_8"], numbers=[entry])
    _, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K2") == "cut"


def test_k2_bare_count_that_looks_like_a_year_still_needs_a_query():
    c = claim("c1", "The hashtag carried 2000 posts", ["yt_8"])
    _, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K2") == "cut"


def test_k2_rerun_count_must_match_exactly():
    c = claim("c1", "The clip passed 184,000 views", ["yt_8"], numbers=[NUM_COUNT])
    _, ok = run(answer([c] + filler()), rerun=lambda n: 184000)
    _, bad = run(answer([c] + filler()), rerun=lambda n: 184001)
    assert verdict(ok, "c1", "K2") == "pass"
    assert verdict(bad, "c1", "K2") == "cut"


def test_k2_rerun_ratio_allows_two_percent():
    c = claim("c1", "Posts reached 2.8 times usual daily posts", ["yt_8"], numbers=[NUM_28])
    _, ok = run(answer([c] + filler()), rerun=lambda n: 2.85)
    _, bad = run(answer([c] + filler()), rerun=lambda n: 2.9)
    assert verdict(ok, "c1", "K2") == "pass"
    assert verdict(bad, "c1", "K2") == "cut"


def test_k2_rerun_share_allows_two_percent():
    c = claim("c1", "40% of posts used the sound", ["yt_8"], numbers=[NUM_SHARE])
    _, ok = run(answer([c] + filler()), rerun=lambda n: 0.407)
    _, bad = run(answer([c] + filler()), rerun=lambda n: 0.42)
    assert verdict(ok, "c1", "K2") == "pass"
    assert verdict(bad, "c1", "K2") == "cut"


def test_k2_rerun_that_fails_cuts():
    def broken(_):
        raise RuntimeError("query failed")

    c = claim("c1", "The clip passed 184,000 views", ["yt_8"], numbers=[NUM_COUNT])
    _, checks = run(answer([c] + filler()), rerun=broken)
    assert verdict(checks, "c1", "K2") == "cut"


# K3


def test_k3_records_in_window_and_market_pass():
    c = claim("c1", "Creators are posting the step", ["tt_1", "yt_8"])
    _, checks = run(answer([c] + filler()), market="ZA")
    assert verdict(checks, "c1", "K3") == "pass"


def test_k3_record_outside_window_is_cut():
    c = claim("c1", "Creators are posting the step", ["rd_5", "yt_8"])
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K3") == "cut"
    assert "c1" not in surviving(new)


def test_k3_record_from_another_market_is_cut():
    c = claim("c1", "Creators are joking about jollof", ["x_3"])
    new, checks = run(answer([c] + filler()), market="ZA")
    assert verdict(checks, "c1", "K3") == "cut"
    assert "c1" not in surviving(new)


# K5


def label_of(new, cid):
    return next(c["label"] for c in new["claims"] if c["id"] == cid)


def test_k5_two_authors_on_two_platforms_allows_corroborated():
    c = claim("c1", "Creators are posting the step", ["tt_1", "yt_8"], label="corroborated")
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K5") == "pass"
    assert label_of(new, "c1") == "corroborated"


def test_k5_one_author_downgrades_corroborated_to_single_source():
    c = claim("c1", "Creators are posting the step", ["tt_1"], label="corroborated")
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K5") == "downgrade"
    assert label_of(new, "c1") == "single_source"
    assert "c1" in surviving(new)


def test_k5_same_handle_on_two_platforms_is_one_author():
    c = claim("c1", "Creators are posting the step", ["tt_1", "ig_7"], label="observed")
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K5") == "downgrade"
    assert label_of(new, "c1") == "single_source"


def test_k5_brand_and_paid_records_are_not_independent():
    c = claim("c1", "Creators are posting the step", ["tt_1", "tt_4", "tt_12"], label="observed")
    new, _ = run(answer([c] + filler()))
    assert label_of(new, "c1") == "single_source"


def test_k5_two_authors_on_one_platform_allow_observed_not_corroborated():
    c = claim("c1", "Creators are posting the step", ["tt_1", "tt_9"], label="corroborated")
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K5") == "downgrade"
    assert label_of(new, "c1") == "observed"


def test_k5_three_authors_plus_a_metric_allow_corroborated():
    c = claim(
        "c1",
        "Posts reached 2.8 times usual daily posts",
        ["tt_1", "tt_9", "tt_10"],
        label="corroborated",
        numbers=[NUM_28],
    )
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K5") == "pass"
    assert label_of(new, "c1") == "corroborated"


def test_k5_three_authors_without_a_metric_allow_observed():
    c = claim(
        "c1", "Creators are posting the step", ["tt_1", "tt_9", "tt_10"], label="corroborated"
    )
    new, _ = run(answer([c] + filler()))
    assert label_of(new, "c1") == "observed"


def test_k5_interpretation_is_capped_at_inferred():
    c = claim(
        "c1",
        "The step reads as a reply to load shedding",
        ["tt_1", "yt_8"],
        label="observed",
        kind="interpretation",
    )
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K5") == "downgrade"
    assert label_of(new, "c1") == "inferred"


def test_k5_label_below_allowed_is_kept():
    c = claim("c1", "Creators are posting the step", ["tt_1", "yt_8"], label="single_source")
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K5") == "pass"
    assert label_of(new, "c1") == "single_source"


# K6


def test_k6_generation_term_is_a_breach():
    c = claim("c1", "Gen Z creators are posting the step", ["yt_8"])
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K6") == "breach"
    assert "c1" not in surviving(new)


def test_k6_age_range_is_a_breach():
    c = claim("c1", "Mostly 18-24 viewers engage", ["yt_8"])
    _, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K6") == "breach"


def test_k6_hedged_demographic_inference_is_a_breach():
    for text in (
        "The audience skews young",
        "Posters are likely female",
        "Popular with millennials",
        "Young creators lead it",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "breach", text


def test_k6_google_trends_is_a_breach():
    _, checks = run(answer([claim("c1", "Google Trends shows interest", ["yt_8"])] + filler()))
    assert verdict(checks, "c1", "K6") == "breach"


def test_k6_generated_record_as_evidence_is_a_breach():
    _, checks = run(
        answer([claim("c1", "Creators are posting the step", ["gm_6", "yt_8"])] + filler())
    )
    assert verdict(checks, "c1", "K6") == "breach"


def test_k6_terms_inside_a_verbatim_quote_are_allowed():
    q = "the youth are loving it"
    c = claim("c1", f"One post says “{q}”", ["ig_2"], quotes=[{"evidence_id": "ig_2", "text": q}])
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K6") == "pass"
    assert "c1" in surviving(new)


def test_k6_youth_day_and_youth_month_are_calendar_moments_not_breaches():
    for text in ("Posts about Youth Day rose in Soweto", "Youth Month playlists are circulating"):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "pass", text


def test_k6_range_of_counted_things_is_not_a_breach():
    for text in (
        "Each creator made 20-30 posts",
        "10-15 creators joined",
        "It ran for 3-5 days",
        "Between 20-30 videos used the sound",
        "40-60 percent of posts used it",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "pass", text


def test_k6_age_ranges_still_breach():
    for text in (
        "Mostly 18-24",
        "Viewers aged 18-24 engage",
        "Popular with 18-24 year olds",
        "Popular with 25-year-olds",
        "Posters in their 20s",
        "Mostly 18-24yo viewers",
        "A 25yo creator started it",
        "people 18-24 share it",
        "Youth culture drives it",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "breach", text


def test_k6_clean_claim_with_date_range_passes():
    _, checks = run(
        answer([claim("c1", "Posting ran from 21 to 27 September", ["yt_8"])] + filler())
    )
    assert verdict(checks, "c1", "K6") == "pass"


def test_k6_breach_in_short_answer_blanks_it_and_forces_partial():
    ans = answer(filler(3), short_answer="Teens across Gauteng are doing the step.")
    new, checks = run(ans)
    assert new["short_answer"] == ""
    assert new["status"] == "partial"
    assert any(
        r["rule"] == "K6" and r["verdict"] == "breach" and r["claim_id"] is None for r in checks
    )


def test_k6_breach_in_so_what_or_watch_next_drops_the_item():
    ans = answer(
        filler(3),
        so_what=[
            {"text": "Speak to young people", "claim_ids": ["f0"]},
            {"text": "Join the step", "claim_ids": ["f1"]},
        ],
        watch_next=[{"text": "Watch whether boomers join", "claim_ids": ["f0"], "forecast": False}],
    )
    new, _ = run(ans)
    assert [s["text"] for s in new["so_what"]] == ["Join the step"]
    assert new["watch_next"] == []


# K8


def test_k8_unmarked_translation_is_cut():
    q = {"evidence_id": "tt_1", "text": "Ngiyabonga kakhulu", "translation": "Thank you very much"}
    c = claim("c1", "A Durban creator thanks her followers", ["tt_1"], quotes=[q])
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K8") == "cut"
    assert "c1" not in surviving(new)


def test_k8_marked_translation_passes():
    q = {
        "evidence_id": "tt_1",
        "text": "Ngiyabonga kakhulu",
        "translation": "Thank you very much",
        "translation_marked": True,
    }
    c = claim("c1", "A Durban creator thanks her followers", ["tt_1"], quotes=[q])
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K8") == "pass"
    assert "c1" in surviving(new)


# K10


def k10_rows(checks):
    return [r for r in checks if r["rule"] == "K10"]


def test_k10_clean_answer_stays_complete():
    new, checks = run(answer(filler(3)))
    assert new["status"] == "complete"
    assert k10_rows(checks) and all(r["verdict"] == "pass" for r in k10_rows(checks))


def test_k10_one_cut_makes_complete_partial_and_drops_items_on_it():
    bad = claim("c1", "Gen Z creators are posting the step", ["yt_8"])
    ans = answer(
        [bad] + filler(2),
        so_what=[
            {"text": "Lean in", "claim_ids": ["c1", "f0"]},
            {"text": "Join", "claim_ids": ["f1"]},
        ],
        watch_next=[{"text": "Watch it", "claim_ids": ["c1"], "forecast": False}],
    )
    new, checks = run(ans)
    assert new["status"] == "partial"
    assert surviving(new) == ["f0", "f1"]
    assert [s["text"] for s in new["so_what"]] == ["Join"]
    assert new["watch_next"] == []
    assert any(r["verdict"] != "pass" for r in k10_rows(checks))


def test_k10_fewer_than_two_surviving_claims_is_insufficient_evidence():
    bad = [claim(f"c{i}", "Gen Z creators", ["yt_8"]) for i in range(2)]
    new, _ = run(answer(bad + filler(1)))
    assert new["status"] == "insufficient_evidence"


def test_k10_complete_with_no_claims_is_insufficient_evidence():
    new, _ = run(answer([], so_what=[]))
    assert new["status"] == "insufficient_evidence"


# Reviewer blockers and notes


def test_b1_claim_without_an_id_is_a_k1_cut():
    c = claim(None, "Creators are posting the step", ["yt_8"])
    new, checks = run(answer([c] + filler(2)))
    assert verdict(checks, "claims[0]", "K1") == "cut"
    assert len(new["claims"]) == 2


def test_b1_duplicate_claim_ids_are_k1_cuts():
    dup = [claim("d", "Creators are posting the step", ["yt_8"]) for _ in range(2)]
    new, checks = run(answer(dup + filler(2)))
    assert verdict(checks, "d", "K1") == "cut"
    assert surviving(new) == ["f0", "f1"]


def test_b2_quote_words_outside_quotation_marks_still_breach():
    q = "the youth are loving it"
    c = claim(
        "c1",
        "Posters say the youth are loving it",
        ["ig_2"],
        quotes=[{"evidence_id": "ig_2", "text": q}],
    )
    _, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K6") == "breach"


def test_b2_another_claims_quote_does_not_exempt_a_claim():
    q = "the youth are loving it"
    c1 = claim("c1", f"One post says “{q}”", ["ig_2"], quotes=[{"evidence_id": "ig_2", "text": q}])
    c2 = claim("c2", f"Posters repeat “{q}”", ["ig_2"])
    _, checks = run(answer([c1, c2] + filler()))
    assert verdict(checks, "c1", "K6") == "pass"
    assert verdict(checks, "c2", "K6") == "breach"


def test_b2_quote_of_a_cut_claim_exempts_nothing_in_short_answer():
    q = "the youth are loving it"
    c1 = claim(
        "c1", f"The 5 posts say “{q}”", ["ig_2"], quotes=[{"evidence_id": "ig_2", "text": q}]
    )
    new, checks = run(answer([c1] + filler(2), short_answer=f"Posts say “{q}”."))
    assert "c1" not in surviving(new)
    assert new["short_answer"] == ""
    assert any(r["rule"] == "K6" and r["claim_id"] is None for r in checks)


def test_b2_quote_of_a_surviving_claim_exempts_the_short_answer():
    q = "the youth are loving it"
    c1 = claim("c1", f"One post says “{q}”", ["ig_2"], quotes=[{"evidence_id": "ig_2", "text": q}])
    new, _ = run(answer([c1] + filler(2), short_answer=f"Posts say “{q}”."))
    assert new["short_answer"] == f"Posts say “{q}”."
    assert new["status"] == "complete"


def test_b4_widened_age_terms_breach():
    for text in (
        "Gen Alphas copy it",
        "Gen Zs copy it",
        "Younger audiences share it",
        "The youngest viewers share it",
        "Kids are copying it",
        "Children love the song",
        "School-going fans share it",
        "Learners share it at break",
        "Pensioners are posting it",
        "The elderly join in",
        "Seniors dance too",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "breach", text


def test_b4_calendar_and_non_audience_uses_still_pass():
    for text in (
        "Youth Day posts rose",
        "Youth Month playlists circulate",
        "Learner's licence queues trend",
        "A senior editor posted it",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "pass", text


def test_b5_breach_in_a_quote_translation_cuts_the_claim():
    q = {
        "evidence_id": "tt_1",
        "text": "Ngiyabonga kakhulu",
        "translation": "Thank you, young people",
        "translation_marked": True,
    }
    c = claim("c1", "A Durban creator thanks her followers", ["tt_1"], quotes=[q])
    new, checks = run(answer([c] + filler()))
    assert verdict(checks, "c1", "K6") == "breach"
    assert "c1" not in surviving(new)


def test_b5_breach_in_a_proposal_basis_or_falsifier_cuts_the_claim():
    for field in ("basis", "falsifier"):
        c = claim("c1", "Brands could join the step", ["yt_8"], kind="proposal", label="inferred")
        c[field] = "Teens drive the step"
        new, checks = run(answer([c] + filler()))
        assert verdict(checks, "c1", "K6") == "breach", field
        assert "c1" not in surviving(new)


def test_b5_breach_in_a_gap_drops_the_gap():
    ans = answer(filler(3))
    ans["gaps"] = [
        {"what": "No Gen Z data", "searched": "tiktok/search", "why": "none"},
        {"what": "No Threads data", "searched": "threads/search", "why": "Google Trends is banned"},
        {"what": "No Reddit data", "searched": "reddit/search", "why": "rate_limited"},
    ]
    new, checks = run(ans)
    assert [g["what"] for g in new["gaps"]] == ["No Reddit data"]
    assert sum(r["rule"] == "K6" and r["verdict"] == "breach" for r in checks) == 2


def test_b6_unpinned_numeral_in_short_answer_blanks_it_and_forces_partial():
    new, checks = run(answer(filler(3), short_answer="The step passed 184,000 views."))
    assert new["short_answer"] == ""
    assert new["status"] == "partial"
    assert any(
        r["rule"] == "K2" and r["claim_id"] is None and r["verdict"] == "cut" for r in checks
    )


def test_b6_numeral_pinned_on_a_surviving_claim_is_allowed_in_answer_fields():
    c = claim("c1", "The clip passed 184,000 views", ["yt_8"], numbers=[NUM_COUNT])
    ans = answer(
        [c] + filler(2),
        short_answer="The step passed 184k views on 26 September.",
        so_what=[{"text": "184,000 views is enough to join", "claim_ids": ["c1"]}],
        watch_next=[{"text": "Watch the 184,000 figure", "claim_ids": ["c1"], "forecast": False}],
    )
    new, _ = run(ans)
    assert new["short_answer"] == "The step passed 184k views on 26 September."
    assert len(new["so_what"]) == 1 and len(new["watch_next"]) == 1
    assert new["status"] == "complete"


def test_b6_unpinned_numeral_drops_so_what_and_watch_next_items():
    ans = answer(
        filler(3),
        so_what=[
            {"text": "Post within 3 days", "claim_ids": ["f0"]},
            {"text": "Join", "claim_ids": ["f1"]},
        ],
        watch_next=[{"text": "Watch for 50 creators", "claim_ids": ["f0"], "forecast": False}],
    )
    new, checks = run(ans)
    assert [s["text"] for s in new["so_what"]] == ["Join"]
    assert new["watch_next"] == []
    assert sum(r["rule"] == "K2" and r["claim_id"] is None for r in checks) == 2


def test_b6_numeral_pinned_only_on_a_cut_claim_is_not_allowed():
    c = claim("c1", "Gen Z gave it 184,000 views", ["yt_8"], numbers=[NUM_COUNT])
    new, _ = run(answer([c] + filler(2), short_answer="It passed 184,000 views."))
    assert new["short_answer"] == ""


def test_n7_ranges_of_other_counted_things_do_not_breach():
    for noun in (
        "comments",
        "likes",
        "views",
        "shares",
        "replies",
        "reposts",
        "hashtags",
        "sounds",
        "songs",
        "markets",
        "hours",
        "weeks",
    ):
        text = f"It drew 20-30 {noun}"
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "pass", text


def test_n8_near_duplicate_records_are_not_independent():
    c = claim("c1", "Creators are posting the step", ["tt_1", "yt_8"], label="corroborated")
    ans = answer([c] + filler())
    ans["evidence"][7]["flags"] = ["near_duplicate"]
    new, _ = run(ans)
    assert label_of(new, "c1") == "single_source"


def test_n9_date_only_window_is_rejected():
    ans = answer(filler(3))
    for kw in (
        {"window_end": "2026-09-28"},
        {"window_start": "2026-09-21"},
        {"window_end": date(2026, 9, 28)},
    ):
        with pytest.raises(ValueError, match="aware datetime"):
            run(ans, **kw)


# Re-review blockers and notes


def test_r1_quote_that_is_only_the_banned_term_does_not_shield_the_claim():
    q = "Gen Z"
    c = claim(
        "c1", f"“{q}” are adopting it fast", ["ig_2"], quotes=[{"evidence_id": "ig_2", "text": q}]
    )
    ans = answer([c] + filler())
    ans["evidence"][1]["text"] = "Gen Z are adopting it, such a Gen Z thing"
    _, checks = run(ans)
    assert verdict(checks, "c1", "K1") == "pass"
    assert verdict(checks, "c1", "K6") == "breach"


def test_r1_verified_quote_with_three_other_words_stays_allowed_only_when_verified():
    q = "such a Gen Z thing"
    ok = claim(
        "c1", f"One post calls it “{q}”", ["ig_2"], quotes=[{"evidence_id": "ig_2", "text": q}]
    )
    bare = claim("c2", f"Posters call it “{q}”", ["ig_2"])
    ans = answer([ok, bare] + filler())
    ans["evidence"][1]["text"] = "Gen Z are adopting it, such a Gen Z thing"
    _, checks = run(ans)
    assert verdict(checks, "c1", "K6") == "pass"
    assert verdict(checks, "c2", "K6") == "breach"


def test_r2_unpinned_numeral_in_context_blanks_it_without_changing_status():
    ans = answer(filler(3))
    ans["context"] = "South Africa has 62 million people."
    new, checks = run(ans)
    assert new["context"] == ""
    assert new["status"] == "complete"
    assert any(
        r["rule"] == "K2" and r["claim_id"] is None and r["detail"].startswith("context")
        for r in checks
    )


def test_r3_window_phrase_matching_the_window_length_is_not_a_figure():
    # The test window runs 21 September 00:00 to 28 September 06:10: 7 days, 1 week, 174 hours.
    for text in (
        "Posting rose over the last 7 days",
        "It held for the past 1 week",
        "Seen in the previous 174 hours",
        "Covid-19 memes returned",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K2") == "pass", text


def test_r3_window_phrase_of_another_length_and_list_names_still_need_a_number():
    for text in ("Posting rose over the last 3 days", "It entered the Top 10"):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K2") == "cut", text


def test_r4_standalone_year_is_not_a_figure():
    for text in (
        "The 2026 elections dominate the feed",
        "2026 was loud",
        "Kwaito ruled radio in 1998",
        "The 2026 wave is here",
        "The 2026 Tshwala Bam wave is here",
        "It has grown since 2019",
        "Mzansi’s 2026 moment arrived",
        "A 2025 edition returned",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K2") == "pass", text


def test_r7_year_shaped_counts_outside_the_allowlist_need_a_pin():
    for text in (
        "It reached 2045 mentions",
        "About 1950 reactions came in",
        "The sound has 2031 uses",
        "It got 2012 saves",
        "Posts hit 2047 on Friday",
        "Kwaito ruled 1998 radio",
        "In the 2031 wave",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K2") == "cut", text
    new, _ = run(answer(filler(3), short_answer="Mentions hit 2045 this week."))
    assert new["short_answer"] == ""
    assert new["status"] == "partial"


def test_r4_year_shaped_count_still_needs_a_number():
    for text in ("The tag carried 2025 posts", "It reached 2020% growth", "2019 followers joined"):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K2") == "cut", text


def test_r5_childrens_day_and_next_gen_do_not_breach():
    for text in (
        "Children's Day posts rose in Lagos",
        "Children’s Day posts rose in Lagos",
        "The next-gen X series phones sold out",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "pass", text


def test_r5_kid_names_and_next_gen_z_as_audience_still_breach():
    for text in ("Kid X dropped a verse", "The next Gen Z wave is here", "Children love the song"):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "breach", text


def test_r9_year_followed_by_a_hyphen_or_plural_noun_is_a_count():
    for text in (
        "Out of 2020 mentions, most were positive",
        "2010 TikTokers joined",
        "The 1999 reactions were mixed",
        "A 2012-post spike hit Friday",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K2") == "cut", text
    for text in (
        "The 2026 wave is here",
        "In 2019 it peaked",
        "In 2025 the wave peaked",
        "The 2026 elections loom",
        "Since 2019 this has grown",
        "The 2025 Durban July drew crowds",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K2") == "pass", text


def test_r8_next_gen_exemption_covers_only_gen_x():
    for text in (
        "The next-gen Z wave is here",
        "next-gen Zers love it",
        "next-gen Alpha fans share it",
        "Generation X posters share it",
    ):
        _, checks = run(answer([claim("c1", text, ["yt_8"])] + filler()))
        assert verdict(checks, "c1", "K6") == "breach", text
    _, checks = run(answer([claim("c1", "The next-gen X series sold out", ["yt_8"])] + filler()))
    assert verdict(checks, "c1", "K6") == "pass"


def test_r6_curly_single_quote_with_an_apostrophe_inside_is_one_span():
    q = "it’s giving"
    c = claim("c1", f"Creators say ‘{q}’", ["yt_8"], quotes=[{"evidence_id": "yt_8", "text": q}])
    ans = answer([c] + filler())
    ans["evidence"][7]["text"] = "Honestly it’s giving main character"
    _, checks = run(ans)
    assert verdict(checks, "c1", "K1") == "pass"


# Rules lifted from lane L3's own checks


def quote_claim(text):
    return claim(
        "c1",
        "A Durban creator thanks her followers",
        ["tt_1"],
        quotes=[{"evidence_id": "tt_1", "text": text}],
    )


def test_l3_one_word_quote_is_cut():
    new, checks = run(answer([quote_claim("Ngiyabonga")] + filler()))
    assert verdict(checks, "c1", "K1") == "cut"
    assert "c1" not in surviving(new)


def test_l3_quote_starting_or_ending_mid_word_is_cut():
    for text in ("giyabonga kakhulu", "kakhulu, le chall", "yabonga kakhulu, le challenge imnandi"):
        _, checks = run(answer([quote_claim(text)] + filler()))
        assert verdict(checks, "c1", "K1") == "cut", text


def test_l3_quote_on_word_boundaries_passes():
    for text in ("kakhulu, le challenge", "le challenge imnandi!", "Ngiyabonga kakhulu"):
        _, checks = run(answer([quote_claim(text)] + filler()))
        assert verdict(checks, "c1", "K1") == "pass", text


def unlocated(ans, *indexes, market=None):
    """Mark evidence records as having no known location, the way the brief's evidence pack does."""
    for i in indexes:
        ans["evidence"][i]["market"] = market
        ans["evidence"][i]["flags"] = ["market_assumed"]
    return ans


def test_l3_an_unlocated_record_does_not_cut_a_claim_that_names_no_place():
    for ids in (["ig_2"], ["ig_2", "tt_1"]):
        for mark in (None, "ZA"):
            ans = unlocated(answer([claim("c1", "Creators are posting the step", ids)] + filler()), 1,
                            market=mark)
            for market in ("ZA", None):
                new, checks = run(ans, market=market)
                assert verdict(checks, "c1", "K3") == "pass", (ids, mark, market)
                assert "c1" in surviving(new)


def test_l3_a_record_located_in_another_market_still_cuts_beside_an_unlocated_one():
    ans = unlocated(answer([claim("c1", "Creators are joking about jollof", ["ig_2", "x_3"])] + filler()), 1)
    new, checks = run(ans, market="ZA")
    assert verdict(checks, "c1", "K3") == "cut"
    assert "c1" not in surviving(new)


KE_TEXT = "The hashtag spread among sports creators and commentary accounts in Kenya."


@pytest.mark.parametrize("market", ["KE", None])
def test_l3_a_place_claim_citing_only_unlocated_records_is_cut(market):
    ans = unlocated(answer([claim("c1", KE_TEXT, ["ig_2", "yt_8"])] + filler()), 1, 7)
    new, checks = run(ans, market=market)
    assert verdict(checks, "c1", "K3") == "cut"
    assert "c1" not in surviving(new)


@pytest.mark.parametrize("market", ["KE", None])
def test_l3_a_place_claim_citing_a_record_located_there_passes(market):
    ans = unlocated(answer([claim("c1", KE_TEXT, ["ig_2", "yt_8"])] + filler()), 1)
    ans["evidence"][7]["market"] = "KE"
    new, checks = run(ans, market=market)
    assert verdict(checks, "c1", "K3") == "pass"
    assert "c1" in surviving(new)


@pytest.mark.parametrize("text,ids,expected", [
    ("Nigerian creators post the step", ["tt_1"], "cut"),
    ("Creators in Lagos post the step", ["tt_1", "x_3"], "pass"),
    ("Mzansi creators post the step", ["x_3"], "cut"),
    ("Creators in ZA post the step", ["tt_1"], "pass"),
    ("Creators in Kenya and South Africa post the step", ["tt_1"], "cut"),
])
def test_l3_every_market_a_claim_names_needs_a_record_located_there(text, ids, expected):
    _, checks = run(answer([claim("c1", text, ids)] + filler()))
    assert verdict(checks, "c1", "K3") == expected, text


@pytest.mark.parametrize("text,quote", [
    ('A creator wrote "love from Nairobi to everyone"', "love from Nairobi to everyone"),
    ('Creators "in Nairobi tonight" are doing the step', "in Nairobi tonight"),
])
def test_l3_a_place_inside_a_verified_quote_still_needs_a_record_located_there(text, quote):
    ans = answer([claim("c1", text, ["yt_8"], quotes=[{"evidence_id": "yt_8", "text": quote}])] + filler())
    ans["evidence"][7]["text"] = "Doing the step, love from Nairobi to everyone, in Nairobi tonight"
    ans["evidence"][7]["market"], ans["evidence"][7]["flags"] = None, ["market_assumed"]
    _, checks = run(ans, market="KE")
    assert verdict(checks, "c1", "K1") == "pass"
    assert verdict(checks, "c1", "K3") == "cut"
    ans["evidence"][7]["market"], ans["evidence"][7]["flags"] = "KE", []
    _, checks = run(ans, market="KE")
    assert verdict(checks, "c1", "K3") == "pass"


def test_l3_market_assumed_record_remains_an_independent_author():
    c = claim("c1", "Creators are posting the step", ["tt_1", "yt_8"], label="corroborated")
    ans = answer([c] + filler())
    ans["evidence"][7]["flags"] = ["market_assumed"]
    assert located_market(ans["evidence"][7]) is None
    new, checks = run(ans)
    assert verdict(checks, "c1", "K5") == "pass"
    assert label_of(new, "c1") == "corroborated"


def test_l3_items_whose_claims_were_all_cut_are_dropped():
    bad = [claim(f"c{i}", "Gen Z creators", ["yt_8"]) for i in (1, 2)]
    ans = answer(
        bad + filler(2),
        so_what=[
            {"text": "Lean in", "claim_ids": ["c1", "c2"]},
            {"text": "Join", "claim_ids": ["f0"]},
        ],
        watch_next=[
            {"text": "Watch it", "claim_ids": ["c1", "c2"], "forecast": False},
            {"text": "Watch the step", "claim_ids": ["f1"], "forecast": False},
        ],
    )
    new, checks = run(ans)
    assert [s["text"] for s in new["so_what"]] == ["Join"]
    assert [w["text"] for w in new["watch_next"]] == ["Watch the step"]
    assert sum(r["rule"] == "K10" and r["verdict"] == "cut" for r in checks) == 2


# Place words: the gazetteer of core/detect/geo.py plus demonyms and nicknames, whole words and hashtag parts

PLACE_PROBES = [
    ("KE", "Creators on #KenyaTwitter post the step"),
    ("KE", "Nairobians post the step"),
    ("KE", "Kenya's creators post the step"),
    ("KE", "Creators in Nakuru and Eldoret post the step"),
    ("KE", "Creators on #kenyatwitter post the step"),
    ("NG", "Lagosians post the step"),
    ("NG", "Creators in Lekki/Enugu post the step"),
    ("NG", "9ja creators post the step"),
    ("NG", "Creators on #NaijaTwitter post the step"),
    ("NG", "The #lagosnights crowd posts the step"),
    ("ZA", "Creators in Gauteng post the step"),
    ("ZA", "KZN creators post the step"),
    ("ZA", "Creators in the Western Cape post the step"),
    ("ZA", "Creators in Sandton post the step"),
    ("ZA", "Jo'burg creators post the step"),
    ("ZA", "Saffas post the step"),
    ("ZA", "Creators in RSA post the step"),
    ("ZA", "Capetonians post the step"),
    ("ZA", "Creators on #SouthAfricaTwitter post the step"),
    ("ZA", "Creators on #CapeTownVibes post the step"),
]


def place_answer(text, located):
    """A claim citing ig_2 and yt_8, both unlocated, or yt_8 located in the given market."""
    ans = unlocated(answer([claim("c1", text, ["ig_2", "yt_8"])] + filler()), 1, 7)
    if located:
        ans["evidence"][7]["market"], ans["evidence"][7]["flags"] = located, []
    return ans


@pytest.mark.parametrize("market,text", PLACE_PROBES, ids=[t for _, t in PLACE_PROBES])
def test_l3_a_probe_place_claim_on_unlocated_records_is_cut(market, text):
    for brief in (market, None):
        new, checks = run(place_answer(text, None), market=brief)
        assert verdict(checks, "c1", "K3") == "cut", (text, brief)
        assert "c1" not in surviving(new)


@pytest.mark.parametrize("market,text", PLACE_PROBES, ids=[t for _, t in PLACE_PROBES])
def test_l3_a_probe_place_claim_with_a_record_located_there_passes(market, text):
    for brief in (market, None):
        _, checks = run(place_answer(text, market), market=brief)
        assert verdict(checks, "c1", "K3") == "pass", (text, brief)


@pytest.mark.parametrize("market,text", PLACE_PROBES, ids=[t for _, t in PLACE_PROBES])
def test_l3_named_markets_reads_every_probe(market, text):
    assert named_markets(text) == [market], text


@pytest.mark.parametrize("text", [
    "Creators post the step", "Creators say Kenyatta is trending", "Durbanville",
    "Creators in SAN post it", "A sa creator", "Creators at the ke stall",
])
def test_l3_named_markets_ignores_words_that_only_contain_a_place(text):
    assert named_markets(text) == [], text


def test_l3_a_place_outside_the_three_markets_needs_a_record_located_there_too():
    _, checks = run(place_answer("Creators in Accra post the step", None), market="NG")
    assert verdict(checks, "c1", "K3") == "cut"
    _, checks = run(place_answer("Creators in Accra post the step", "GH"))
    assert verdict(checks, "c1", "K3") == "pass"


def test_l3_a_market_assumed_flag_never_hides_a_stated_foreign_market():
    ans = answer([claim("c1", "Creators are joking about jollof", ["x_3"])] + filler())
    ans["evidence"][2]["flags"] = ["market_assumed"]
    new, checks = run(ans, market="ZA")
    assert verdict(checks, "c1", "K3") == "cut"
    assert "c1" not in surviving(new)
    assert located_market(ans["evidence"][2]) is None


def test_n22_every_flag_the_brief_evidence_builder_sets_for_a_non_independent_post_is_not_independent():
    # core/brief/evidence.py _record sets flagged (a creator with a coordination score), sponsored and
    # near_duplicate; market_assumed only says the location is unknown. K5 must read the first three as
    # non-independent, as the gate context does for corroboration.
    for flag in ("flagged", "sponsored", "near_duplicate"):
        c = claim("c1", "Creators are posting the step", ["tt_1", "yt_8"], label="corroborated")
        ans = answer([c] + filler())
        ans["evidence"][7]["flags"] = [flag]
        new, _ = run(ans)
        assert label_of(new, "c1") == "single_source", flag
    c = claim("c1", "Creators are posting the step", ["tt_1", "yt_8"], label="corroborated")
    ans = answer([c] + filler())
    ans["evidence"][7]["flags"] = ["market_assumed"]
    new, _ = run(ans)
    assert label_of(new, "c1") == "corroborated"


def test_n22_k5_excludes_everything_the_gate_context_excludes_from_corroboration():
    from core.brief.gatectx import NOT_INDEPENDENT as GATE_SET
    from core.trust.claims import NOT_INDEPENDENT as K5_SET

    assert GATE_SET <= K5_SET, sorted(GATE_SET - K5_SET)
