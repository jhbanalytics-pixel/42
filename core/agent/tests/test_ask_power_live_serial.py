"""Ask power, item 4 (live search on three platforms in parallel): not built, and why this file exists.

Ask holds one live SocialCrawl call at a time on purpose. L1's client checks the share's cap and then calls with no
lock (core/collect/socialcrawl_client.py, _call then _checked then _live, ledger written afterwards), so two calls in
flight can each pass the cap check and overshoot it by a quote. sc_adapter.LOCK closes that, and
test_sc_adapter proves both that the lock serialises and that without it the calls overlap. Making three searches run
at once means making that check and reserve atomic inside core/collect, which this lane does not own. The research
loop has its own reason too: a live call's guard reads the spend of every earlier call, so it must see them finished.

These tests pin the other half of the rule, in the research loop: a model turn that asks for several live calls gets
them one after another, never as a parallel batch, and every live tool is outside PARALLEL_TOOLS. They passed when
written, because they describe the rule as it stands; their job is to fail if someone widens it.
"""

from core.agent import gemini_research

LIVE_TOOLS = ("socialcrawl_call", "get_comments", "get_transcript", "watch_video")


def test_no_live_tool_is_in_the_parallel_set():
    assert not set(LIVE_TOOLS) & set(gemini_research.PARALLEL_TOOLS)


def test_a_turn_with_three_live_searches_is_three_batches_of_one():
    calls = [type("Call", (), {"name": "socialcrawl_call", "args": {"platform": p}})() for p in ("tiktok", "x", "youtube")]
    functions = {"socialcrawl_call": lambda **a: {}}
    batches = gemini_research._batches(calls, functions)
    assert [len(b) for b in batches] == [1, 1, 1]

