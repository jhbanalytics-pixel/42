"""K2 counts against FLOAT64 results (audit W06-01) and K9 on common future wording (audit W06-02)."""
import pytest

from core.agent.tests.test_checks import FakeWarehouse, make_ctx, make_draft, run, verdict

FLOAT_ROWS = [{"day": "2026-09-27", "posts": 412.0, "ratio": 2.8}, {"day": "2026-09-26", "posts": 131.0, "ratio": 0.9}]


def float_count_draft(value):
    ctx = make_ctx(rows=FLOAT_ROWS)
    draft = make_draft(ctx)
    claim = draft["claims"][0]
    claim["text"] = claim["text"].replace("412 posts", f"{value} posts")
    claim["numbers"][0]["value"] = value
    return ctx, draft


def test_k2_cuts_a_count_that_only_falls_within_the_ratio_tolerance_of_a_float_cell():
    ctx, draft = float_count_draft(412.5)
    _, verdicts = run(draft, ctx, FakeWarehouse(FLOAT_ROWS))
    v = verdict(verdicts, "c1", "K2")
    assert v["verdict"] == "cut" and "412.5" in v["reason"]


def test_k2_keeps_a_count_equal_to_a_float_cell():
    ctx, draft = float_count_draft(412)
    _, verdicts = run(draft, ctx, FakeWarehouse(FLOAT_ROWS))
    assert verdict(verdicts, "c1", "K2")["verdict"] == "pass"


def test_k2_cuts_a_count_the_rerun_returns_only_within_the_tolerance():
    rows = [{"day": "2026-09-27", "posts": 412.0}]
    ctx = make_ctx(rows=rows)
    draft = make_draft(ctx)
    claim = draft["claims"][0]
    claim["text"] = "People on TikTok and X rated their Sunday lunch plates on 27 September: 412 posts."
    claim["numbers"] = [n for n in claim["numbers"] if n["value"] == 412]
    _, verdicts = run(draft, ctx, FakeWarehouse(rows))
    assert verdict(verdicts, "c1", "K2")["verdict"] == "pass"
    _, verdicts = run(draft, ctx, FakeWarehouse([{"day": "2026-09-27", "posts": 413.0}]))
    v = verdict(verdicts, "c1", "K2")
    assert v["verdict"] == "cut" and "re-run" in v["reason"]


@pytest.mark.parametrize("ratio,expected", [(2.75, "pass"), (2.72, "cut")])
def test_k2_ratios_keep_their_two_percent_tolerance(ratio, expected):
    rows = [{"day": "2026-09-27", "posts": 412.0, "ratio": ratio}]
    ctx = make_ctx(rows=rows)
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(rows))
    assert verdict(verdicts, "c1", "K2")["verdict"] == expected


@pytest.mark.parametrize("future", [
    "We expect plate ratings to take off next week.",
    "Plate ratings could surge next week.",
    "Plate ratings might explode before Heritage Day.",
    "Plate ratings are going to blow up on Sunday.",
    "Plate ratings may fade next week.",
])
def test_k9_cuts_common_unlogged_forecast_wording(future):
    ctx = make_ctx()
    draft = make_draft(ctx)
    draft["claims"][2]["text"] = future
    answer, verdicts = run(draft, ctx, FakeWarehouse(make_ctx().queries["q_1"]["rows"]))
    assert verdict(verdicts, "c3", "K9")["verdict"] == "cut"
    assert future not in str(answer)


@pytest.mark.parametrize("text", [
    "Watch whether plate ratings return.",
    "Watch whether plate ratings could surge again.",
])
def test_k9_keeps_a_neutral_watch_condition(text):
    ctx = make_ctx()
    draft = make_draft(ctx)
    draft["watch_next"][0]["text"] = text
    answer, verdicts = run(draft, ctx, FakeWarehouse(make_ctx().queries["q_1"]["rows"]))
    assert answer["watch_next"] == draft["watch_next"]
    assert not any(v["claim_id"] == "watch_next/0" and v["rule"] == "K9" for v in verdicts)


@pytest.mark.parametrize("past", [
    "Plate ratings surged on 27 September.",
    "Creators expected a quieter Sunday, but posts rose on 27 September.",
    "Brands should hold their plate posts until the evening.",
])
def test_k9_leaves_past_and_advice_wording_to_the_other_checks(past):
    from core.agent import checks
    assert not checks.FUTURE_ASSERTION.search(past)
