"""Ask market scope for the 6 October 2026 tester report (Bug A, adjacent defect).

Place in core/agent/tests/ and run from the engine-style root the repo's Ask suite uses. Written against a80be1d.

The stored ask a_20261006_b3b2a066 (market ZA, a question about Kenya) hydrated 14 posts located in KE, NG or ZW into
a ZA ask through fetch_posts, from model-written SQL with no market filter. K3 kept them out of the cited evidence, but
run.posts is len(ctx.evidence), so the "posts read" line and the live "Posts gathered" list count them as read for a
South Africa question. This test pins that the count of posts read for a single-market ask leaves out posts located in
another market.
"""
from core.agent.tests.test_ask import Harness, make_research

QUESTION = "amapiano in South Africa this week"


def test_posts_read_leaves_out_posts_located_in_another_market():
    def research(ctx, prompt, options, emit, should_stop):
        out = make_research(live=False)(ctx, prompt, options, emit, should_stop)
        for n, market in enumerate(("KE", "KE", "NG"), start=1):
            ctx.evidence[f"other_{n}"] = dict(ctx.evidence["x_1"], id=f"other_{n}", market=market, flags=[])
        return out

    in_market = Harness(research=make_research(live=False)).run(question=QUESTION, market="ZA")
    with_others = Harness(research=research).run(question=QUESTION, market="ZA")
    assert in_market["run"]["posts"] > 0
    assert with_others["run"]["posts"] == in_market["run"]["posts"]


def _with_others(captured=None, markets=("KE", "KE", "NG")):
    def research(ctx, prompt, options, emit, should_stop):
        out = make_research(live=False)(ctx, prompt, options, emit, should_stop)
        for n, market in enumerate(markets, start=1):
            ctx.evidence[f"other_{n}"] = dict(ctx.evidence["x_1"], id=f"other_{n}", market=market, flags=[])
        if captured is not None:
            captured.append(ctx)
        return out

    return research


def test_posts_located_in_another_market_stay_in_the_evidence_store_with_their_own_label():
    # The store keeps them as labelled evidence (test_fetch_posts_hydrates_query_row_ids_into_evidence expects an NG
    # post fetched in a ZA ask to be stored as NG); only the count of posts read for the market leaves them out.
    captured = []
    Harness(research=_with_others(captured)).run(question=QUESTION, market="ZA")
    ctx = captured[0]
    assert [ctx.evidence[f"other_{n}"]["market"] for n in (1, 2, 3)] == ["KE", "KE", "NG"]
    assert ctx.market == "ZA"


def test_the_writing_step_counts_the_same_posts_as_the_run():
    harness = Harness(research=_with_others())
    out = harness.run(question=QUESTION, market="ZA")
    writing = [e["text"] for e in harness.events if e.get("event") == "step" and e.get("kind") == "write"
               and e["text"].startswith("Writing the answer from")]
    assert writing and writing[0].startswith(f"Writing the answer from {out['run']['posts']} posts")


def test_platforms_are_counted_from_the_same_posts_as_the_run():
    def research(ctx, prompt, options, emit, should_stop):
        out = make_research(live=False)(ctx, prompt, options, emit, should_stop)
        ctx.evidence["other_1"] = dict(ctx.evidence["x_1"], id="other_1", market="KE", platform="youtube", flags=[])
        return out

    in_market = Harness(research=make_research(live=False)).run(question=QUESTION, market="ZA")
    with_other = Harness(research=research).run(question=QUESTION, market="ZA")
    assert with_other["run"]["platforms"] == in_market["run"]["platforms"]


def test_an_ask_with_no_single_market_counts_every_post_it_read():
    unscoped = "amapiano this week"
    in_market = Harness(research=make_research(live=False)).run(question=unscoped)
    with_others = Harness(research=_with_others()).run(question=unscoped)
    assert with_others["run"]["posts"] == in_market["run"]["posts"] + 3


def _found_steps(market, records):
    from core.agent import ask
    from core.agent.context import RunContext
    from core.agent.tests.test_ask import NOW
    events = []
    progress = ask.Progress(events.append, lambda: NOW, market_label="x", window=(NOW.date(), NOW.date()))
    ctx = RunContext(run_id="run_test", tier="T1", as_of=NOW, market=market)
    for n, record_market in enumerate(records, start=1):
        ctx.evidence[f"p{n}"] = {"id": f"p{n}", "platform": "x", "market": record_market, "flags": []}
    ask._found(progress, ctx)
    return [e for e in events if e["event"] == "step"], [e for e in events if e["event"] == "evidence"]


def test_the_found_step_counts_only_posts_for_the_asked_market_and_still_sends_every_post_as_evidence():
    steps, evidence = _found_steps("ZA", ["ZA", "ZA", "KE", "NG"])
    assert [(s["kind"], s["count"], s["text"]) for s in steps] == [("found", 2, "2 posts found on X")]
    assert [e["evidence"]["market"] for e in evidence] == ["ZA", "ZA", "KE", "NG"]


def test_no_found_step_is_sent_when_every_fresh_post_is_from_another_market():
    steps, evidence = _found_steps("ZA", ["KE"])
    assert steps == [] and len(evidence) == 1


def test_an_ask_with_no_single_market_gets_a_found_step_for_every_fresh_post():
    steps, _ = _found_steps(None, ["ZA", "KE", "NG"])
    assert [(s["count"], s["text"]) for s in steps] == [(3, "3 posts found on X")]
