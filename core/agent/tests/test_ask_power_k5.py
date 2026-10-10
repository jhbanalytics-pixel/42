"""Ask power review, Critical 2: a transcript span is judged for K5 by its post, not by its own words.

K5 drops a disclosed ad and a copied caption from the authors a label counts, reading the record's own text. A span's
text is the transcript, which carries neither the caption's #ad nor its repeated words, so the span of a paid or
copied post counted as an independent author and lifted a label above what the post itself would allow. A span is now
judged as the post it belongs to, and counts for nothing when that post is excluded or cannot be found."""

import copy

import pytest

from core.agent.checks import check_answer
from core.agent.context import RunContext
from core.agent.tests.test_checks import (AS_OF, PARAMS, ROWS, RUN, SQL, STORED, WINDOW, FakeWarehouse, claim, record,
                                          verdict)
from core.agent.tests.test_ask_power_transcripts import _cite, segments
from core.agent.tests.test_enrich_tools import FakeClient
from core.agent.tools.enrich_tools import get_transcript
from datetime import datetime

CAPTION = "Rate my plate honestly this Sunday the butternut is doing the most today #7colours"
AD = record("tt_ad", "tiktok", "@brand.kitchen", "Our 7 colours platter is back this Sunday #ad #7colours")
X = record("x_1", "x", "@lerato_mk", "Rate my 7 colours honestly, the butternut is doing the most #7colours")
FIRST = record("tt_a", "tiktok", "@first_poster", CAPTION)
COPY = record("tt_b", "tiktok", "@second_poster", CAPTION)
CLEAN = record("tt_clean", "tiktok", "@mpho.cooks.za", "Sunday 7 colours check, beetroot beside the chakalaka #7colours")


def world(stored, spans_of=()):
    ctx = RunContext(run_id=RUN, tier="T1", as_of=datetime.fromisoformat(AS_OF), market="ZA")
    for r in [*STORED[:3], *stored]:  # the three posts make_draft names, beside this test's own
        ctx.evidence[r["id"]] = copy.deepcopy(r)
    ctx.record_query(SQL, PARAMS, copy.deepcopy(ROWS), "daily posts and ratio")
    ids = {}
    for parent in spans_of:
        ids[parent] = get_transcript(ctx, FakeClient(quote=10.0, items=segments()), parent)["evidence_ids"][0]
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
