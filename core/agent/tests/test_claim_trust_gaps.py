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
