"""Ask power review, Critical 2: a transcript span is judged for K5 by its post, not by its own words.

K5 drops a disclosed ad and a copied caption from the authors a label counts, reading the record's own text. A span's
text is the transcript, which carries neither the caption's #ad nor its repeated words, so the span of a paid or
copied post counted as an independent author and lifted a label above what the post itself would allow. A span is now
judged as the post it belongs to, and counts for nothing when that post is excluded or cannot be found."""

import copy


from core.agent.checks import check_answer
from core.agent.context import RunContext
from core.agent.tests.test_checks import (AS_OF, PARAMS, ROWS, RUN, SQL, STORED, WINDOW, FakeWarehouse, claim, record,
                                          verdict)
from core.agent.tests.test_ask_power_transcripts import LINES, _cite, segments
from core.agent.tests.test_enrich_tools import FakeClient
from core.agent.tools.enrich_tools import get_transcript
from datetime import datetime

CAPTION = "Rate my plate honestly this Sunday the butternut is doing the most today #7colours"
AD = record("tt_ad", "tiktok", "@brand.kitchen", "Our 7 colours platter is back this Sunday #ad #7colours")
X = record("x_1", "x", "@lerato_mk", "Rate my 7 colours honestly, the butternut is doing the most #7colours")
FIRST = record("tt_a", "tiktok", "@first_poster", CAPTION)
COPY = record("tt_b", "tiktok", "@second_poster", CAPTION)
CLEAN = record("tt_clean", "tiktok", "@mpho.cooks.za", "Sunday 7 colours check, beetroot beside the chakalaka #7colours")


def world(stored, spans_of=(), lines=None):
    """lines maps a parent id to its own transcript lines; any other parent gets the shared LINES."""
    ctx = RunContext(run_id=RUN, tier="T1", as_of=datetime.fromisoformat(AS_OF), market="ZA")
    for r in [*STORED[:3], *stored]:  # the three posts make_draft names, beside this test's own
        ctx.evidence[r["id"]] = copy.deepcopy(r)
    ctx.record_query(SQL, PARAMS, copy.deepcopy(ROWS), "daily posts and ratio")
    ids = {}
    for parent in spans_of:
        items = segments((lines or {}).get(parent) or LINES)
        ids[parent] = get_transcript(ctx, FakeClient(quote=10.0, items=items), parent)["evidence_ids"][0]
    return ctx, ids


def label_of(ctx, cited, quote_id, label="corroborated"):
    draft = _cite(ctx, cited, quote_id, "the boerewors comes first")
    draft["claims"][0]["label"] = label
    answer, verdicts = check_answer(draft, ctx, FakeWarehouse([]), window=WINDOW, market="ZA")
    shown = claim(answer, "c1")
    return (shown["label"] if shown else None), verdicts


def test_the_post_itself_with_an_ad_caption_and_an_independent_post_is_single_source():
    ctx, ids = world([AD, X], spans_of=["tt_ad"])
    draft = _cite(ctx, ["tt_ad", "x_1"], "tt_ad", "Our 7 colours platter is back")
    draft["claims"][0]["label"] = "corroborated"
    answer, _ = check_answer(draft, ctx, FakeWarehouse([]), window=WINDOW, market="ZA")
    assert claim(answer, "c1")["label"] == "single_source"


def test_the_span_of_an_ad_post_with_an_independent_post_is_single_source_too():
    ctx, ids = world([AD, X], spans_of=["tt_ad"])
    label, verdicts = label_of(ctx, [ids["tt_ad"], "x_1"], ids["tt_ad"])
    assert label == "single_source"
    assert verdict(verdicts, "c1", "K5")["verdict"] == "downgrade"


def test_the_copy_of_a_caption_is_not_a_second_author_as_a_post():
    ctx, ids = world([FIRST, COPY, X], spans_of=["tt_b"])
    label, _ = label_of(ctx, ["tt_a", "tt_b"], ids["tt_b"], label="observed")
    assert label is None or label == "single_source"  # the quote is in the span, so only the label is under test


def test_the_span_of_a_copied_caption_does_not_count_as_a_second_author():
    ctx, ids = world([FIRST, COPY, X], spans_of=["tt_b"])
    label, verdicts = label_of(ctx, ["tt_a", ids["tt_b"]], ids["tt_b"], label="observed")
    assert label == "single_source"
    assert verdict(verdicts, "c1", "K5")["verdict"] == "downgrade"


def test_the_span_of_the_original_caption_still_counts():
    ctx, ids = world([FIRST, COPY, X], spans_of=["tt_a"])
    label, _ = label_of(ctx, [ids["tt_a"], "x_1"], ids["tt_a"])
    assert label == "corroborated"


def test_the_span_of_a_clean_post_with_an_independent_post_keeps_its_label():
    ctx, ids = world([CLEAN, X], spans_of=["tt_clean"])
    label, verdicts = label_of(ctx, [ids["tt_clean"], "x_1"], ids["tt_clean"])
    assert label == "corroborated" and verdict(verdicts, "c1", "K5")["verdict"] == "pass"


def test_a_span_whose_post_is_not_in_the_run_counts_for_nothing():
    ctx, ids = world([CLEAN, X], spans_of=["tt_clean"])
    del ctx.evidence["tt_clean"]
    label, _ = label_of(ctx, [ids["tt_clean"], "x_1"], ids["tt_clean"])
    assert label == "single_source"


def test_a_disclosure_spoken_in_the_video_excludes_the_span_as_well():
    ctx, ids = world([CLEAN, X], spans_of=["tt_clean"])
    span = ctx.evidence[ids["tt_clean"]]
    span["text"] = span["transcript_span"]["text"] = "this video is a paid partnership with a kitchen brand"
    draft = _cite(ctx, [ids["tt_clean"], "x_1"], ids["tt_clean"], "a paid partnership with a kitchen brand")
    draft["claims"][0]["label"] = "corroborated"
    answer, _ = check_answer(draft, ctx, FakeWarehouse([]), window=WINDOW, market="ZA")
    assert claim(answer, "c1")["label"] == "single_source"


def test_a_post_and_its_own_span_cited_together_are_still_one_author_and_one_original():
    ctx, ids = world([FIRST, COPY, X], spans_of=["tt_a"])
    label, _ = label_of(ctx, ["tt_a", ids["tt_a"], "x_1"], ids["tt_a"])
    assert label == "corroborated"  # tt_a is the original; its span does not make it a copy of itself


def test_a_flag_set_on_the_post_after_its_span_was_written_still_excludes_the_span():
    ctx, ids = world([CLEAN, X], spans_of=["tt_clean"])
    ctx.evidence["tt_clean"]["flags"] = ["sponsored"]
    assert ctx.evidence[ids["tt_clean"]]["flags"] == []
    label, _ = label_of(ctx, [ids["tt_clean"], "x_1"], ids["tt_clean"])
    assert label == "single_source"


# Re-review, Critical A: the words a span carries are still compared with every other cited record's words.

IG = record("ig_1", "instagram", "@other.creator", "Totally different caption about plates and sauce for Sunday")
ECHO = record("tt_say", "tiktok", "@echo_poster", "placeholder caption replaced below with the spoken words in the test")


def test_two_creators_spans_with_the_same_transcript_are_one_author_not_two():
    ctx, ids = world([CLEAN, IG, X], spans_of=["tt_clean", "ig_1"])
    assert ctx.evidence[ids["tt_clean"]]["text"] == ctx.evidence[ids["ig_1"]]["text"]
    label, verdicts = label_of(ctx, [ids["tt_clean"], ids["ig_1"]], ids["tt_clean"], label="corroborated")
    assert label == "single_source"
    assert verdict(verdicts, "c1", "K5")["verdict"] == "downgrade"


def test_a_caption_that_repeats_another_creators_spoken_words_is_not_a_second_author():
    ctx, ids = world([CLEAN, ECHO, X], spans_of=["tt_clean"])
    ctx.evidence["tt_say"]["text"] = ctx.evidence[ids["tt_clean"]]["text"]
    label, _ = label_of(ctx, [ids["tt_clean"], "tt_say"], ids["tt_clean"], label="observed")
    assert label == "single_source"


def test_two_creators_on_two_platforms_with_their_own_transcripts_still_corroborate():
    own = {"ig_1": ["Honestly the plating matters more than the sauce on a Sunday afternoon at home",
                    "My nan taught me to start with the pap and finish with the chakalaka every time",
                    "Share this with someone who still puts the gravy on first"]}
    ctx, ids = world([CLEAN, IG, X], spans_of=["tt_clean", "ig_1"], lines=own)
    assert ctx.evidence[ids["tt_clean"]]["text"] != ctx.evidence[ids["ig_1"]]["text"]
    label, verdicts = label_of(ctx, [ids["tt_clean"], ids["ig_1"]], ids["tt_clean"], label="corroborated")
    assert label == "corroborated"
    reason = verdict(verdicts, "c1", "K5")["reason"]
    assert verdict(verdicts, "c1", "K5")["verdict"] == "pass" and "2 unrelated author groups on 2 platforms" in reason


def test_identical_uncited_spans_do_link_authors_and_cap_the_label():
    """Pins the other side: independence is judged over every stored record of the answer (W8-DEC-16), so two creators
    whose transcripts share a later span, cited or not, are one group however different their cited spans are."""
    shared = ["The same trending sound line plays here and everybody sings along to it again",
              "Now the chorus comes round and the whole street joins in at the top of its voice"]
    first_a = " ".join(["Welcome back to the kitchen where today we cook a proper Sunday lunch for the whole family"] * 3)
    first_b = " ".join(["Honestly the plating matters more than the sauce on a Sunday afternoon at home with friends"] * 3)
    own = {"tt_clean": [first_a, *shared], "ig_1": [first_b, *shared]}
    ctx, ids = world([CLEAN, IG, X], spans_of=["tt_clean", "ig_1"], lines=own)
    a, b = ids["tt_clean"], ids["ig_1"]
    assert ctx.evidence[a]["text"] != ctx.evidence[b]["text"]
    assert ctx.evidence["tt_clean_span_2"]["text"] == ctx.evidence["ig_1_span_2"]["text"]  # uncited, identical
    draft = _cite(ctx, [a, b], a, "Welcome back to the kitchen")
    draft["claims"][0]["label"] = "corroborated"
    answer, verdicts = check_answer(draft, ctx, FakeWarehouse([]), window=WINDOW, market="ZA")
    assert claim(answer, "c1")["label"] == "observed"
    assert "1 unrelated author group on 2 platforms" in verdict(verdicts, "c1", "K5")["reason"]
