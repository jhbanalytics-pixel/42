"""Ask power, item 3 of the ranked plan: a fetched transcript becomes evidence a claim can cite.

Before this, ctx.transcripts held the spoken words and nothing read them: no record carried a transcript_span and the
writer never saw a segment, so 10 credits bought words no claim could quote. Now get_transcript, and the transcript
watch_video pays for, write span records into ctx.evidence. A span is a child of its post: it takes the post's handle,
link, time, market, source market and flags, so it is exactly as located as the post and no more, and it counts as the
post's author, never a second one. The existing checks then read it like any other record.
"""

import copy

import pytest

from core.agent import ask, checks, writer
from core.agent.checks import check_answer
from core.agent.context import Refused, RunContext
from core.agent.tests.test_checks import WINDOW, FakeWarehouse, claim, make_ctx as checks_ctx, make_draft, verdict
from core.agent.tests.test_enrich_tools import AS_OF, FakeClient, make_ctx
from core.agent.tools import enrich_tools
from core.agent.tools.enrich_tools import get_comments, get_transcript

LINES = [
    "Welcome back to the channel today we are making a proper Sunday braai",
    "I can't be the only one who thinks the boerewors comes first",
    "My gran did it this way every single week and nobody argued",
    "Then the neighbours arrived with their own wors and the street went quiet",
    "Nobody told us it was a competition but here we are now",
    "So tell me in the comments who does it better",
]


def segments(lines=LINES, step=4.0):
    return [{"start": i * step, "end": (i + 1) * step, "text": text} for i, text in enumerate(lines)]


def spans_of(ctx, parent="tiktok_7412"):
    return [r for r in ctx.evidence.values() if r.get("parent_id") == parent and r.get("transcript_span")]


def transcript_ctx(**kw):
    c = make_ctx(**kw)
    out = get_transcript(c, FakeClient(quote=10.0, items=segments()), "tiktok_7412")
    return c, out


def test_a_transcript_becomes_span_records_that_a_claim_can_cite():
    c, out = transcript_ctx()
    spans = spans_of(c)
    assert spans and out["evidence_ids"] == [s["id"] for s in spans]
    parent = c.evidence["tiktok_7412"]
    for s in spans:
        assert s["parent_id"] == "tiktok_7412" and s["via"] == "tiktok/post/transcript"
        for field in ("platform", "handle", "url", "posted_at", "market"):
            assert s[field] == parent[field]
        assert s["text"] == s["transcript_span"]["text"]
        assert set(s["transcript_span"]) == {"start_s", "end_s", "text"}
        assert not checks.REQUIRED_FIELDS or all(s.get(f) for f in checks.REQUIRED_FIELDS)
    assert " ".join(s["text"] for s in spans) == " ".join(LINES)


def test_spans_are_cut_on_segment_edges_and_keep_their_times():
    c, _ = transcript_ctx()
    spans = spans_of(c)
    assert len(spans) > 1 and len(spans) <= enrich_tools.MAX_SPANS
    assert spans[0]["transcript_span"]["start_s"] == 0.0
    assert spans[-1]["transcript_span"]["end_s"] == 4.0 * len(LINES)
    for earlier, later in zip(spans, spans[1:]):
        assert earlier["transcript_span"]["end_s"] == later["transcript_span"]["start_s"]
    for s in spans:
        assert len(s["text"]) <= enrich_tools.SPAN_CHARS or len(s["text"].split(" ")) <= 12  # one long segment stands alone


def test_the_tool_result_hands_the_model_the_fenced_spans_and_their_ids():
    c, out = transcript_ctx()
    assert len(out["evidence"]) == len(out["evidence_ids"])
    for row in out["evidence"]:
        assert row["text"].startswith("<untrusted_content>") and row["evidence_id"] in out["evidence_ids"]
        assert row["parent_id"] == "tiktok_7412"
    assert out["segments"]  # the timed segments are still returned, as before


def test_a_span_is_as_located_as_its_post_and_no_more():
    c = make_ctx()
    c.evidence["tiktok_7412"]["flags"] = ["market_assumed"]
    c.evidence["tiktok_7412"]["source_market"] = "ZA"
    get_transcript(c, FakeClient(quote=10.0, items=segments()), "tiktok_7412")
    assert all(s["flags"] == ["market_assumed"] and s["source_market"] == "ZA" for s in spans_of(c))
    located, _ = transcript_ctx()
    assert all("market_assumed" not in s["flags"] for s in spans_of(located))


def test_a_span_takes_its_market_from_the_post_not_from_the_question():
    c = make_ctx()  # the question's market is ZA; x_99 is a Nigerian post
    get_transcript(c, FakeClient(quote=10.0, items=segments()), "x_99")
    spans = spans_of(c, "x_99")
    assert spans and {s["market"] for s in spans} == {"NG"}
    assert {s["platform"] for s in spans} == {"x"}


def test_a_span_does_not_copy_the_posts_engagement_or_flags_by_reference():
    c, _ = transcript_ctx()
    spans = spans_of(c)
    spans[0]["flags"].append("paid")
    assert "paid" not in c.evidence["tiktok_7412"]["flags"] and "paid" not in spans[1]["flags"]


def test_a_second_call_is_free_and_writes_no_new_records():
    c = make_ctx()
    client = FakeClient(quote=10.0, items=segments())
    first = get_transcript(c, client, "tiktok_7412")
    before = copy.deepcopy(c.evidence)
    second = get_transcript(c, client, "tiktok_7412")
    assert second["cached"] is True and second["credits_spent"] == 0
    assert second["evidence_ids"] == first["evidence_ids"] and c.evidence == before
    assert len(client.calls) == 1


def test_a_cached_transcript_with_no_spans_yet_gets_them_without_a_new_call():
    c = make_ctx()
    c.transcripts["tiktok_7412"] = {"status": "ok", "segments": [{"start_s": 0.0, "end_s": 2.0, "text": "Sawubona"},
                                                                  {"start_s": 2.0, "end_s": 4.0, "text": "welcome all"}]}
    client = FakeClient(quote=10.0, items=segments())
    out = get_transcript(c, client, "tiktok_7412")
    assert client.calls == [] and out["cached"] is True
    assert [s["text"] for s in spans_of(c)] == ["Sawubona welcome all"]


def test_a_very_long_transcript_is_capped_and_says_so():
    lines = [f"line number {i} with some spoken words in it to fill the span" for i in range(400)]
    c = make_ctx()
    out = get_transcript(c, FakeClient(quote=10.0, items=segments(lines, 1.0)), "tiktok_7412")
    assert len(spans_of(c)) == enrich_tools.MAX_SPANS == len(out["evidence_ids"])
    assert out["spans_dropped"] > 0 and out["status"] == "ok"
    assert out["segments"] and len(out["segments"]) == 400  # the model still reads every segment


@pytest.mark.parametrize("status,items", [("error", []), ("rate_limited", []), ("empty", []), ("ok", [{"t": 1}])])
def test_a_failed_transcript_adds_no_records_and_costs_only_its_own_call(status, items):
    c = make_ctx()
    client = FakeClient(quote=10.0, items=items, status=status, charged=0.0)
    out = get_transcript(c, client, "tiktok_7412")
    assert out["evidence_ids"] == [] and out["evidence"] == [] and spans_of(c) == []
    assert out["status"] != "ok" or out["reason"]
    assert len(client.calls) == 1 and c.calls_made == 1 and c.credits_spent == 0.0 == out["credits_spent"]
    assert "tiktok_7412" not in c.transcripts and not any(r.get("parent_id") for r in c.evidence.values())


def test_a_charged_failure_costs_what_the_vendor_charged_and_nothing_more():
    c = make_ctx()
    c.credits_spent = c.enrich_credits_spent = 11.0  # the T1 pool is 21, so 10 are left for this call
    client = FakeClient(quote=10.0, items=[], status="error", charged=10.0)
    out = get_transcript(c, client, "tiktok_7412")
    assert out["credits_spent"] == 10.0 and c.credits_spent == c.enrich_credits_spent == 21.0
    assert len(client.calls) == 1
    retry = FakeClient(quote=10.0, items=segments())
    with pytest.raises(Refused):  # nothing was kept, and the pool the failed call drained cannot pay for a retry
        get_transcript(c, retry, "tiktok_7412")
    assert retry.calls == [] and c.credits_spent == 21.0 and spans_of(c) == []


def test_a_failed_transcript_is_a_plain_gap_in_the_answer():
    c = make_ctx()
    get_transcript(c, FakeClient(quote=10.0, items=[], status="error", charged=10.0), "tiktok_7412")
    gaps = checks.source_gaps(c, WINDOW)
    assert [g["what"] for g in gaps] == ["Transcript of a TikTok post not fetched"]
    assert gaps[0]["why"] == "error" and gaps[0]["searched"] == "tiktok/post/transcript"


# The checks read a span like any other record.


def _cite(ctx, ids, quote_id, quote_text, text="A creator said it aloud in the video."):
    draft = make_draft(ctx)
    draft["claims"] = [{"id": "c1", "text": text, "label": "single_source", "kind": "observation",
                        "evidence_ids": list(ids), "quotes": [{"evidence_id": quote_id, "text": quote_text}]}]
    draft["so_what"] = [{"text": "Treat it as one creator's view.", "claim_ids": ["c1"]}]
    draft["watch_next"] = [{"text": "Watch whether others repeat it.", "claim_ids": ["c1"], "forecast": False}]
    draft["short_answer"] = "One creator said it aloud."
    draft["evidence"] = [copy.deepcopy(ctx.evidence[i]) for i in ids]
    return draft


def checks_with_transcript(parent="tt_1"):
    ctx = checks_ctx()
    out = get_transcript(ctx, FakeClient(quote=10.0, items=segments()), parent)
    return ctx, out["evidence_ids"], FakeWarehouse([])


def test_a_verbatim_quote_from_a_span_passes_the_checks():
    ctx, ids, wh = checks_with_transcript()
    answer, verdicts = check_answer(_cite(ctx, ids[:1], ids[0], "the boerewors comes first"), ctx, wh,
                                    window=WINDOW, market="ZA")
    assert claim(answer, "c1") is not None
    assert verdict(verdicts, "c1", "K1")["verdict"] == "pass" and verdict(verdicts, "c1", "K8")["verdict"] == "pass"
    assert [e["id"] for e in answer["evidence"]] == ids[:1]
    assert answer["evidence"][0]["transcript_span"]["text"] == ctx.evidence[ids[0]]["text"]


def test_a_quote_that_is_not_in_the_span_is_cut():
    ctx, ids, wh = checks_with_transcript()
    answer, verdicts = check_answer(_cite(ctx, ids[:1], ids[0], "the chakalaka comes first"), ctx, wh,
                                    window=WINDOW, market="ZA")
    assert claim(answer, "c1") is None
    assert verdict(verdicts, "c1", "K1")["verdict"] == "cut"


def test_a_quote_cannot_be_stitched_across_two_spans():
    ctx, ids, wh = checks_with_transcript()
    assert len(ids) > 1
    a, b = ctx.evidence[ids[0]]["text"], ctx.evidence[ids[1]]["text"]
    stitched = " ".join(a.split()[-3:] + b.split()[:3])
    answer, verdicts = check_answer(_cite(ctx, ids[:2], ids[0], stitched), ctx, wh, window=WINDOW, market="ZA")
    assert claim(answer, "c1") is None and verdict(verdicts, "c1", "K1")["verdict"] == "cut"


def test_a_span_is_cut_by_k3_when_its_post_is_outside_the_window():
    ctx = checks_ctx()
    ctx.evidence["tt_1"]["posted_at"] = "2026-08-01T10:00:00+02:00"
    ids = get_transcript(ctx, FakeClient(quote=10.0, items=segments()), "tt_1")["evidence_ids"]
    answer, verdicts = check_answer(_cite(ctx, ids[:1], ids[0], "the boerewors comes first"), ctx,
                                    FakeWarehouse([]), window=WINDOW, market="ZA")
    assert claim(answer, "c1") is None and verdict(verdicts, "c1", "K3")["verdict"] == "cut"


def test_a_span_of_a_post_seen_only_in_feeds_cannot_stand_for_a_located_claim():
    ctx = checks_ctx()
    ctx.evidence["tt_1"]["flags"] = ["market_assumed"]
    ctx.evidence["tt_1"]["source_market"] = "ZA"
    ids = get_transcript(ctx, FakeClient(quote=10.0, items=segments()), "tt_1")["evidence_ids"]
    draft = _cite(ctx, ids[:1], ids[0], "the boerewors comes first", text="Creators in South Africa said it aloud.")
    answer, verdicts = check_answer(draft, ctx, FakeWarehouse([]), window=WINDOW, market="ZA")
    assert verdict(verdicts, "c1", "K3")["verdict"] == "cut"


def test_a_post_and_its_own_transcript_are_one_author_not_two():
    ctx, ids, wh = checks_with_transcript()
    draft = _cite(ctx, ["tt_1", ids[0]], ids[0], "the boerewors comes first")
    draft["claims"][0]["label"] = "corroborated"
    answer, verdicts = check_answer(draft, ctx, wh, window=WINDOW, market="ZA")
    shown = claim(answer, "c1")
    assert shown is not None and shown["label"] == "single_source"
    assert verdict(verdicts, "c1", "K5")["verdict"] == "downgrade"


# What the writer and the run object see


def _post_blocks(ctx):
    records, _ = writer._writer_evidence(ctx)
    blocks, _ = writer._pack(ctx, records)
    return [b for b in blocks if b.startswith("post ")]


def test_the_pack_names_a_span_as_words_spoken_in_its_post():
    ctx, ids, _ = checks_with_transcript()
    blocks = [b for b in _post_blocks(ctx) if f'"id": "{ids[0]}"' in b]
    assert len(blocks) == 1
    head = blocks[0].splitlines()[0]
    assert '"transcript_of": "tt_1"' in head and '"start_s": 0.0' in head
    assert ctx.evidence[ids[0]]["text"] in blocks[0]


def test_the_writer_is_told_how_to_read_a_spoken_span():
    assert "transcript_of" in writer.WRITER_SYSTEM


def test_spans_are_not_counted_as_posts_read():
    ctx, ids, _ = checks_with_transcript()
    without = checks_ctx()
    assert len(ask._posts_read(ctx)) == len(ask._posts_read(without))


def test_a_span_is_not_announced_as_a_post_found():
    ctx = checks_ctx()
    steps = []
    progress = ask.Progress(lambda e: steps.append(e), lambda: ctx.as_of, market_label="South Africa",
                            window=WINDOW)
    ask._found(progress, ctx)
    before = [s for s in steps if s.get("event") == "step" and s.get("kind") == "found"]
    get_transcript(ctx, FakeClient(quote=10.0, items=segments()), "tt_1")
    steps.clear()
    ask._found(progress, ctx)
    after = [s for s in steps if s.get("event") == "step" and s.get("kind") == "found"]
    assert after == [] and len(before) == 1
    assert any(s.get("event") == "evidence" and s["evidence"].get("transcript_span") for s in steps)


def test_a_transcript_watch_video_pays_for_becomes_spans_too():
    c = make_ctx()
    calls = enrich_tools._VideoCalls(c, FakeClient(quote=10.0, items=segments()), "tiktok_7412")
    out = calls.call("tiktok/post/transcript", {"url": c.evidence["tiktok_7412"]["url"]})
    assert out.status == "ok" and len(spans_of(c)) > 1
    assert " ".join(s["text"] for s in spans_of(c)) == " ".join(LINES)


def test_comments_are_unchanged():
    c = make_ctx()
    out = get_comments(c, FakeClient(quote=1.0), "tiktok_7412", limit=5, max_credits=1.0)
    assert out["evidence_ids"] and not any(r.get("transcript_span") for r in c.evidence.values())


def test_a_checked_answer_with_a_span_meets_the_answer_contract():
    import json
    from pathlib import Path

    import jsonschema

    from core.agent.answer import validate_answer

    ctx, ids, wh = checks_with_transcript()
    answer, _ = check_answer(_cite(ctx, ids[:1], ids[0], "the boerewors comes first"), ctx, wh, window=WINDOW,
                             market="ZA")
    assert validate_answer(answer) == []
    schema = json.loads((Path(checks.__file__).parents[1] / "eval" / "answer.schema.json").read_text("utf-8"))
    jsonschema.validate(answer, schema)


def test_the_tool_text_says_a_transcript_gives_citable_spans():
    from core.agent.toolset import DESCRIPTIONS

    text = DESCRIPTIONS["get_transcript"]
    assert "evidence ids" in text and "cite" in text
