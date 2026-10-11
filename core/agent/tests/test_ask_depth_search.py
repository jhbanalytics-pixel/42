"""Ask depth, items 1 and 2: one failed search leg no longer kills the other, both reasons are returned and logged,
a refusal names the allowed arguments, and a hashtag search matches the tag column and its co-occurring tags."""

import logging
from datetime import datetime

import pytest

from core.agent import gemini_research
from core.agent.context import Refused, RunContext
from core.agent.tests.test_warehouse_tools import SplitWarehouse, kw, sem
from core.agent.toolset import SCHEMAS
from core.agent.tools.warehouse import search_posts

NOW = datetime(2026, 10, 10, 6, 0)


@pytest.fixture
def ctx():
    return RunContext(run_id="r_depth", tier="T1", as_of=NOW, market="ZA")


class KeywordFails(SplitWarehouse):
    """The keyword query raises; the semantic query answers with rows, or fails too."""

    def __init__(self, keyword_error, semantic=(), semantic_error=None):
        super().__init__([], list(semantic), fail=semantic_error)
        self.keyword_error = keyword_error

    def run(self, sql, params, max_bytes_billed):
        if "tvf_search_posts" not in sql:
            self.runs.append((sql, params, max_bytes_billed))
            raise self.keyword_error
        return super().run(sql, params, max_bytes_billed)


# item 1: one failed leg does not kill the call


def test_keyword_failure_still_runs_the_semantic_leg(ctx):
    wh = KeywordFails(RuntimeError("keyword boom"), semantic=[sem("s1", 0.1), sem("s2", 0.2)])
    out = search_posts(ctx, wh, "#humor")
    assert [e["id"] for e in out["evidence"]] == ["s1", "s2"]
    assert out["keyword"] == "unavailable"
    assert "keyword boom" in out["keyword_reason"]
    assert "semantic" not in out
    assert len(out["query_ids"]) == 1 and out["query_id"] == out["query_ids"][0]
    assert "tvf_search_posts" in ctx.queries[out["query_id"]]["sql"]


def test_keyword_failure_is_logged_with_its_reason(ctx, caplog):
    wh = KeywordFails(RuntimeError("keyword boom"), semantic=[sem("s1", 0.1)])
    with caplog.at_level(logging.WARNING, logger="core.agent.tools.warehouse"):
        search_posts(ctx, wh, "#humor")
    lines = [r.getMessage() for r in caplog.records if "search_posts" in r.getMessage()]
    assert any("keyword" in m and "r_depth" in m and "keyword boom" in m for m in lines)


def test_both_legs_failing_returns_both_reasons(ctx):
    wh = KeywordFails(RuntimeError("keyword boom"), semantic_error=RuntimeError("semantic boom"))
    with pytest.raises(Refused) as caught:
        search_posts(ctx, wh, "#humor")
    message = str(caught.value)
    assert "keyword boom" in message and "semantic boom" in message
    assert message.index("Keyword") < message.index("keyword boom")
    assert message.index("Semantic") < message.index("semantic boom")


def test_too_many_terms_fails_the_keyword_leg_only(ctx):
    wh = SplitWarehouse([], [sem("s1", 0.1)])
    out = search_posts(ctx, wh, "one two three four five six seven eight nine")
    assert [e["id"] for e in out["evidence"]] == ["s1"]
    assert out["keyword"] == "unavailable"
    assert "at most 8" in out["keyword_reason"]
    assert len(wh.runs) == 1 and "tvf_search_posts" in wh.runs[0][0]


def test_an_empty_query_is_still_refused_before_any_leg_runs(ctx):
    wh = SplitWarehouse([kw("a")], [sem("b", 0.1)])
    with pytest.raises(Refused, match="empty"):
        search_posts(ctx, wh, "   ")
    assert wh.runs == []


def test_a_working_keyword_leg_adds_no_keyword_note(ctx):
    out = search_posts(ctx, SplitWarehouse([kw("a")], [sem("b", 0.1)]), "braai")
    assert "keyword" not in out and "keyword_reason" not in out


# item 1: the refusal says what is allowed; the market argument the model reached for is accepted when it matches


def test_an_unknown_argument_refusal_lists_the_allowed_arguments():
    message = gemini_research._invalid_args("search_posts", {"query": "x", "colour": "red"})
    assert message and "colour" in message
    for name in ("query", "platforms", "since", "until", "min_engagement", "author", "sort", "limit", "market"):
        assert name in message
    assert len(message) <= 400


def test_the_allowed_list_does_not_reach_a_message_for_a_wrong_type():
    message = gemini_research._invalid_args("search_posts", {"query": 5})
    assert message and message.startswith("Invalid arguments")


def test_search_posts_declares_an_optional_market():
    assert "market" in SCHEMAS["search_posts"]["properties"]
    assert "market" not in SCHEMAS["search_posts"]["required"]
    assert gemini_research._invalid_args("search_posts", {"query": "#humor", "market": "ZA"}) is None


def test_the_question_market_is_accepted_and_changes_nothing(ctx):
    plain = SplitWarehouse([kw("a")], [])
    search_posts(ctx, plain, "braai")
    named = SplitWarehouse([kw("a")], [])
    search_posts(RunContext(run_id="r2", tier="T1", as_of=NOW, market="ZA"), named, "braai", market="za")
    assert [r[1] for r in named.runs] == [r[1] for r in plain.runs]


def test_another_market_is_refused_in_plain_words(ctx):
    wh = SplitWarehouse([kw("a")], [])
    with pytest.raises(Refused) as caught:
        search_posts(ctx, wh, "braai", market="NG")
    assert "ZA" in str(caught.value) and "NG" in str(caught.value) and "market" in str(caught.value)
    assert wh.runs == []


def test_a_market_is_refused_when_the_question_has_none():
    ctx = RunContext(run_id="r3", tier="T1", as_of=NOW, market=None)
    with pytest.raises(Refused, match="market"):
        search_posts(ctx, SplitWarehouse([kw("a")], []), "braai", market="ZA")
