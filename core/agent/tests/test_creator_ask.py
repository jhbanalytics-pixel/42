import copy

import pytest

from core.agent import checks
from core.agent.tests.test_ask import FakeModel, Harness, WRITER_SCHEMA
from core.agent.tests.test_creator_lookup import CRE_01, SPR_03, creator_con as creator_con_fixture
from core.agent.tests.test_history import DuckWarehouse
from core.agent.tools.warehouse import MAX_CREATOR_CANDIDATES, MAX_POSTS


EMPTY_WRITER_OUT = {
    "short_answer": "The stored evidence did not establish an answer.",
    "claims": [],
    "so_what": [],
    "watch_next": [],
    "gaps": [],
    "context": "",
}
MARKET_NAMES = {"KE": "Kenya", "NG": "Nigeria"}


@pytest.fixture
def creator_warehouse():
    con = creator_con_fixture.__wrapped__()
    yield DuckWarehouse(con)
    con.close()


def research_trace(events):
    def research(ctx, prompt, options, emit, should_stop):
        events.append({
            "prompt": prompt,
            "queries": copy.deepcopy(ctx.queries),
            "evidence": copy.deepcopy(ctx.evidence),
            "window": (ctx.window_start, ctx.window_end),
        })
        return {"note": "Ordinary research reached.", "tokens": {"input": 0, "output": 0}, "usd": 0.0}

    return research


def creator_harness(warehouse, events, *, spent=None, stop_flag=None):
    model = FakeModel()
    model.writer_out = copy.deepcopy(EMPTY_WRITER_OUT)
    harness = Harness(research=research_trace(events), spent=spent, model=model, stop_flag=stop_flag)
    harness.warehouse = harness.deps.warehouse = warehouse
    return harness, model


def writer_input(model):
    return next(call["user"] for call in model.calls if call["schema"] is WRITER_SCHEMA)


@pytest.mark.parametrize(("question", "post_ids", "weak_id", "market", "person_claim"), [
    (SPR_03, {"spr_located", "spr_profile"}, "spr_profile", "KE",
     "Kenyan creators are posting amapiano."),
    (CRE_01, {"cre_food"}, "cre_food", "NG",
     "Nigerian creators drove food and home-cooking content."),
])
def test_run_ask_discovers_stored_creators_before_research_and_packs_weak_posts(
    creator_warehouse, question, post_ids, weak_id, market, person_claim
):
    reached = []
    harness, model = creator_harness(creator_warehouse, reached)

    harness.run(question=question)

    assert len(reached) == 1
    query_rows = list(reached[0]["queries"].values())
    creator_queries = [q for q in query_rows if q["purpose"].startswith("discover_creators")]
    post_queries = [q for q in query_rows if q["purpose"].startswith("discover_creator_posts")]
    assert len(creator_queries) == 1
    assert len(post_queries) == 1
    assert "intelligence_42_core.creators" in creator_queries[0]["sql"]
    assert creator_queries[0]["params"]["market"] == market
    assert creator_queries[0]["params"]["creator_limit"] <= MAX_CREATOR_CANDIDATES
    assert post_queries[0]["params"]["post_limit"] <= MAX_POSTS
    assert all("followers" not in row and "profile_location" not in row
               for row in creator_queries[0]["rows"])

    evidence = reached[0]["evidence"]
    assert set(evidence) == post_ids
    assert evidence[weak_id]["flags"] == ["market_assumed"]
    assert evidence[weak_id]["source_market"] == market
    assert not checks._located(evidence[weak_id])
    problems, _ = checks._k3(person_claim, [weak_id], evidence, reached[0]["window"], [market])
    assert any(f"only seen in {MARKET_NAMES[market]}'s feeds" in problem for problem in problems)

    user = writer_input(model)
    for post_id in post_ids:
        assert post_id in user
    for query_id, query in reached[0]["queries"].items():
        if query in [*creator_queries, *post_queries]:
            assert query_id in user
    assert "Ordinary research reached." in user
    if question == CRE_01:
        assert "does not establish a mid-sized category" in user
        assert "not comment text or commenter behavior" in user


def test_run_ask_does_not_query_creators_for_an_unrelated_question(creator_warehouse):
    reached = []
    harness, _ = creator_harness(creator_warehouse, reached)

    harness.run(question="Is amapiano spreading in Kenya?")

    assert len(reached) == 1
    assert reached[0]["queries"] == {}
    assert creator_warehouse.runs == []


def test_run_ask_treats_a_successful_empty_creator_lookup_as_insufficient_data(creator_warehouse):
    creator_warehouse.con.execute("DELETE FROM intelligence_42_core.creators")
    reached = []
    harness, model = creator_harness(creator_warehouse, reached)

    out = harness.run(question=CRE_01)

    assert len(reached) == 1
    (query_id, query) = next(iter(reached[0]["queries"].items()))
    assert query["purpose"].startswith("discover_creators")
    assert query["rows"] == []
    assert query_id in writer_input(model)
    assert not any("RuntimeError" in n for n in out["run"]["notices"])


def test_run_ask_reports_creator_query_failure_and_continues_without_invented_profiles_or_posts(
    creator_warehouse,
):
    class FailedCreatorQuery(DuckWarehouse):
        def __init__(self, con):
            super().__init__(con)
            self.attempts = 0

        def run(self, sql, params, max_bytes_billed):
            if "FROM intelligence_42_core.creators c" in sql:
                self.attempts += 1
                raise RuntimeError("CREATOR_FAILURE_DETAIL_SENTINEL")
            return super().run(sql, params, max_bytes_billed)

    warehouse = FailedCreatorQuery(creator_warehouse.con)
    reached = []
    harness, model = creator_harness(warehouse, reached)

    out = harness.run(question=CRE_01)

    assert warehouse.attempts == 1
    assert len(reached) == 1
    assert reached[0]["evidence"] == {}
    user = writer_input(model)
    notices = " ".join(out["run"]["notices"])
    assert "RuntimeError" in notices and "RuntimeError" in user
    assert "CREATOR_FAILURE_DETAIL_SENTINEL" not in notices + user
    assert not any(name in user for name in ("food_creator", "food_gap", "cre_food"))
    assert "no creators" not in user.lower()


@pytest.mark.parametrize("state", ["budget_refused", "stopped"])
def test_run_ask_skips_creator_lookup_when_research_is_not_admitted(creator_warehouse, state):
    reached = []
    stop_flag = {"stop": state == "stopped"}
    spent = (lambda: 100.0) if state == "budget_refused" else None
    harness, _ = creator_harness(creator_warehouse, reached, spent=spent, stop_flag=stop_flag)

    harness.run(question=CRE_01)

    assert reached == []
    assert creator_warehouse.runs == []
