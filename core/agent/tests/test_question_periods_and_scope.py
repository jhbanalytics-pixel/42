"""Ask fixes from the 16 October 2026 audit: the period a question names, re-runs after research spends its share, the
question's market on every warehouse tool, and X in the ranked list."""
from datetime import date, datetime

import pytest

from core.agent import ask, checks
from core.agent.context import Refused, RunContext, STORE_TOOL, result_hash
from core.agent.model_budget import AskModelBudget, BudgetRefused
from core.agent.ranked import ranked_list
from core.agent.tools import history
from core.agent.tools import warehouse as wh
from core.agent.tools.dates import SAST
from core.agent.tools.warehouse import _store_scope

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=SAST)


@pytest.mark.parametrize("question, start, end", [
    ("What was trending in SA in August?", date(2026, 8, 1), date(2026, 8, 31)),
    ("What was big in Lagos during July 2026?", date(2026, 7, 1), date(2026, 7, 31)),
    ("What happened in Nairobi in October?", date(2026, 10, 1), date(2026, 10, 16)),  # the month so far
    ("What happened in Joburg in December?", date(2025, 12, 1), date(2025, 12, 31)),  # not yet this year
    ("What did Nigerians post from 2026-09-01 to 2026-09-10?", date(2026, 9, 1), date(2026, 9, 10)),
    ("What moved between 2026-09-01 and 2026-09-03?", date(2026, 9, 1), date(2026, 9, 3)),
    ("What blew up in Lagos in the last 24 hours?", date(2026, 10, 15), date(2026, 10, 16)),
    ("What changed since October 2?", date(2026, 10, 2), date(2026, 10, 16)),
    # A relative window still wins over a calendar month named as context.
    ("Compared with June 2024, what rose in the last 7 days?", date(2026, 10, 10), date(2026, 10, 16)),
    # A single named day is context, not the window.
    ("AFCON qualifiers kicked off on 21 September. Who is winning right now?", date(2026, 9, 17), date(2026, 10, 16)),
])
def test_named_periods_set_the_window(question, start, end):
    assert ask._window(question, NOW) == (start, end)
    assert not ask._unread_period(question, NOW)


@pytest.mark.parametrize("question", ["What happened last weekend in Lagos?", "What was big in Kenya in 2025?"])
def test_a_period_the_window_cannot_read_is_flagged(question):
    assert ask._window_source(question, NOW) == ask.DEFAULT_WINDOW
    assert ask._unread_period(question, NOW)


def test_a_named_month_is_not_the_trending_board():
    assert ask._current_trending_intent("What is trending in SA right now?")
    assert not ask._current_trending_intent("What was trending in SA in August?")


class SemanticWarehouse:
    def dry_run(self, sql, params):
        return {"bytes": 1, "tables": []}

    def run(self, sql, params, cap):
        return [{"post_id": "p1", "engagement": 1200, "distance": 0.1}]


def test_a_number_rerun_is_not_cut_once_research_has_spent_its_share():
    budget = AskModelBudget(5.0, 1.0)
    ctx = RunContext(run_id="r1", tier="T1", as_of=NOW, market="ZA", window_start=date(2026, 9, 17),
                     window_end=date(2026, 10, 16), model_budget=budget)
    sql = ("SELECT s.post_id, s.engagement, s.distance FROM intelligence_42_agent.tvf_search_posts_scoped(@q, "
           "@market, @since, @until, @k, CAST(NULL AS ARRAY<STRING>), CAST(NULL AS STRING), @min_engagement) s")
    rows = SemanticWarehouse().run(sql, None, 0)
    ctx.queries["q_1"] = {"sql": sql, "params": {"q": "amapiano"}, "rows": rows, "result_hash": result_hash(rows),
                          "purpose": "x"}
    reservation = budget.reserve_cost(0.99, research=True)
    budget.settle_cost(reservation, 0.99)
    with pytest.raises(BudgetRefused):
        budget.reserve_cost(0.05, research=True)
    number = {"value": 1200, "query_id": "q_1", "run_id": "r1", "result_hash": ctx.queries["q_1"]["result_hash"],
              "unit": "engagements"}
    assert checks._number_problem(number, ctx, SemanticWarehouse(), {}) is None


class MarketWarehouse:
    def dry_run(self, sql, params):
        return {"bytes": 1, "tables": []}

    def run(self, sql, params, cap):
        self.params = params
        return []


def za_ctx():
    return RunContext(run_id="r1", tier="T1", as_of=NOW, market="ZA", window_start=date(2026, 9, 17),
                      window_end=date(2026, 10, 16))


def test_rising_topics_reads_only_the_questions_market():
    with pytest.raises(Refused, match="cannot read NG"):
        wh.rising_topics(za_ctx(), MarketWarehouse(), market="NG")
    warehouse = MarketWarehouse()
    wh.rising_topics(za_ctx(), warehouse, market="za")
    assert warehouse.params["market"] == "ZA"


def test_history_reads_only_the_questions_market():
    with pytest.raises(Refused):
        history._market(za_ctx(), "KE")
    assert history._market(za_ctx(), None) == "ZA"
    multi = RunContext(run_id="r2", tier="T1", as_of=NOW)
    assert history._market(multi, "NG") == "NG"


def test_the_ranked_list_reads_x_rows_stored_as_twitter():
    question = "Which hashtags are trending on Twitter in Nigeria this week?"
    ctx = RunContext(run_id="r1", tier="T1", as_of=NOW, market="NG", window_start=date(2026, 10, 10),
                     window_end=date(2026, 10, 16))
    _, _, params, _ = _store_scope(ctx, ask.question_platforms(question))
    rows = [{"platform": "twitter", "hashtag": "bbnaija", "creators": 40, "posts": 90, "sample_post_ids": ["t1"]}]
    ctx.queries["q_1"] = {"sql": "x", "params": params, "rows": rows, "result_hash": result_hash(rows),
                          "tool": STORE_TOOL}
    digest = ctx.queries["q_1"]["result_hash"]
    answer = {"status": "complete", "evidence": [{"id": "t1", "handle": "a"}], "claims": [{
        "id": "c1", "text": "#bbnaija led", "evidence_ids": ["t1"], "numbers": [
            {"value": 40, "unit": "creators", "query_id": "q_1", "run_id": "r1", "result_hash": digest},
            {"value": 90, "unit": "posts", "query_id": "q_1", "run_id": "r1", "result_hash": digest}]}]}
    ranked, notes = ranked_list(question, answer, ctx)
    assert ranked is not None and ranked["platform"] == "x", notes
