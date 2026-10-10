"""Ask power, item 6: a follow-up starts from what its parent cited, and cannot widen market or window.

Before this the child got the parent's question, short answer and five claim texts as context and researched from zero
(173 to 241 s measured). Now the parent's cited evidence ids are re-read from the store through the gate's listed-id
fetch, inside the child's window, and the queries the parent's numbers rest on are re-run under new query ids, both
before the first research turn. Nothing the parent said is trusted as evidence: only its ids and its SQL cross, the
posts come back from the store, the SQL passes the model's own guard, and every number is a fresh result with its own
hash. The child's market and window are its own; a post or query outside them is left out.
"""

import copy
from datetime import date

import pytest

from core.agent import ask
from core.agent.context import result_hash
from core.agent.tests.test_ask import NOW, POSTS, FakeWarehouse, Harness, post

WINDOW = {"from": "2026-09-22", "to": "2026-09-28"}
FOLLOW_UP = "Which creators are driving it?"
COUNT = ("SELECT COUNT(*) AS posts, COUNT(DISTINCT p.platform) AS platforms FROM intelligence_42_core.posts p "
         "WHERE p.post_date BETWEEN DATE('2026-09-22') AND DATE('2026-09-28')")
OLD = post("tt_old", "tiktok", "@early_bird", "amapiano before the window", 1)
NG = {**post("tt_ng", "tiktok", "@lagos_beats", "amapiano from Lagos", 24), "geo_market": "NG"}
STORE = {r["post_id"]: r for r in [*POSTS, OLD, NG]}


def parent_answer(*ids, extra=None):
    claims = [{"id": f"c{i}", "text": f"Claim {i}", "evidence_ids": [eid]} for i, eid in enumerate(ids, start=1)]
    return {"short_answer": "Amapiano.", "claims": claims, **(extra or {})}


def parent(*ids, market="ZA", window=WINDOW, queries=None, answer=None):
    out = {"question": "What is trending in South Africa this week?", "answer": answer or parent_answer(*ids),
           "window": window, "market": market}
    if queries is not None:
        out["queries"] = queries
    return out


class Store(FakeWarehouse):
    """Answers a fetch of listed post ids from STORE, and keeps what it was asked and in what order."""

    def __init__(self):
        super().__init__()
        self.fetches, self.order = [], []

    def run(self, sql, params, max_bytes_billed):
        if "p.post_id IN" in sql:
            ids = [v for k, v in (params or {}).items() if k.startswith("post_id_")]
            self.fetches.append({"ids": ids, "since": params["source_since"], "until": params["source_until"]})
            self.order.append("fetch")
            return [copy.deepcopy(STORE[i]) for i in ids if i in STORE]
        return super().run(sql, params, max_bytes_billed)


def follow(parent_dict, question=FOLLOW_UP, market="ZA", **kw):
    """Run a follow-up and return (output, what research saw at its first turn, the harness)."""
    seen = {}

    def research(ctx, prompt, options, emit, should_stop):
        seen.update(prompt=prompt, evidence={k: copy.deepcopy(v) for k, v in ctx.evidence.items()},
                    queries={k: dict(v) for k, v in ctx.queries.items()}, fetches=len(h.warehouse.fetches),
                    steps=[e.get("text") for e in h.events if e.get("event") == "step"])
        return {"note": "", "tokens": {"input": 10, "output": 5}, "usd": 0.001}

    h = Harness(research=research, **kw)
    h.warehouse = h.deps.warehouse = Store()
    out = h.run(question=question, parent=parent_dict, market=market)
    return out, seen, h


# What crosses from the parent


def test_the_cited_ids_come_in_order_without_repeats():
    answer = parent_answer("tt_2", "tt_1", "tt_2", "x_1")
    got = ask.parent_reuse(parent(answer=answer), market="ZA", window=(date(2026, 9, 22), date(2026, 9, 28)),
                           as_of=NOW)
    assert got["post_ids"] == ["tt_2", "tt_1", "x_1"]


def test_ids_that_are_not_store_posts_are_not_fetched_and_a_span_reads_its_post():
    answer = parent_answer("tt_1_span_2", "tiktok_comment_77", "[unresolved]", "", "x_1")
    answer["claims"].append({"id": "c9", "text": "t", "evidence_ids": [7, None, "tt_1"]})
    got = ask.parent_reuse(parent(answer=answer), market="ZA", window=(date(2026, 9, 22), date(2026, 9, 28)),
                           as_of=NOW)
    assert got["post_ids"] == ["tt_1", "x_1"]


def test_a_follow_up_with_nothing_cited_and_no_queries_reuses_nothing():
    assert ask.parent_reuse(parent(), market="ZA", window=(date(2026, 9, 22), date(2026, 9, 28)), as_of=NOW) is None
    assert ask.parent_reuse(None, market="ZA", window=(date(2026, 9, 22), date(2026, 9, 28)), as_of=NOW) is None
    assert ask.parent_reuse({"question": "q", "answer": None}, market="ZA",
                            window=(date(2026, 9, 22), date(2026, 9, 28)), as_of=NOW) is None


def test_the_ids_are_capped_at_the_most_a_fetch_reads():
    ids = [f"tt_{i}" for i in range(500)]
    got = ask.parent_reuse(parent(*ids), market="ZA", window=(date(2026, 9, 22), date(2026, 9, 28)), as_of=NOW)
    assert got["post_ids"] == ids[:ask.MAX_POSTS]


@pytest.mark.parametrize("parent_market,child_market,reused", [
    ("ZA", "ZA", True), ("ZA", None, True), (None, "ZA", True), ("NG", "ZA", False), ("ZA", "KE", False)])
def test_a_follow_up_never_reads_a_different_market_than_its_own(parent_market, child_market, reused):
    got = ask.parent_reuse(parent("tt_1", market=parent_market), market=child_market,
                           window=(date(2026, 9, 22), date(2026, 9, 28)), as_of=NOW)
    assert (got is not None) is reused


def query(sql=COUNT, purpose="posts and platforms in the window", params=None):
    return {"query_id": "q_3", "purpose": purpose, "sql": sql, "params": params or {}}


def test_queries_cross_only_when_the_window_and_the_market_are_the_same():
    child = (date(2026, 9, 22), date(2026, 9, 28))
    same = ask.parent_reuse(parent("tt_1", queries=[query()]), market="ZA", window=child, as_of=NOW)
    assert [q["sql"] for q in same["queries"]] == [COUNT]
    other_window = ask.parent_reuse(parent("tt_1", queries=[query()]), market="ZA",
                                    window=(date(2026, 9, 19), date(2026, 9, 28)), as_of=NOW)
    assert other_window["queries"] == [] and other_window["post_ids"] == ["tt_1"]
    no_market = ask.parent_reuse(parent("tt_1", market=None, queries=[query()]), market="ZA", window=child, as_of=NOW)
    assert no_market["queries"] == []
    child_open = ask.parent_reuse(parent("tt_1", queries=[query()]), market=None, window=child, as_of=NOW)
    assert child_open["queries"] == []


def test_only_plain_model_queries_cross():
    child = (date(2026, 9, 22), date(2026, 9, 28))
    queries = [query(), query(purpose="Whole-store posts by platform"),
               query(purpose="Topic sweep: amapiano"), query(purpose="fetch_posts: 5 posts listed in query rows"),
               query(purpose="search_posts: amapiano"), query(sql=""), query(sql=7), {"sql": COUNT}, "COUNT", None,
               query(params={"since": {"nested": "object"}}), query(params={1: "x"}), query(params=["not", "a", "map"]),
               query(sql=COUNT + " /* " + "x" * 20_000 + " */")]
    got = ask.parent_reuse(parent("tt_1", queries=queries), market="ZA", window=child, as_of=NOW)
    assert [q["purpose"] for q in got["queries"]] == ["posts and platforms in the window"]


def test_plain_json_parameters_cross_and_are_passed_on():
    child = (date(2026, 9, 22), date(2026, 9, 28))
    sql = COUNT.replace("DATE('2026-09-22')", "DATE(@since)")
    queries = [query(sql=sql, params={"since": "2026-09-22", "tags": ["a", "b"], "n": 3}), query(sql=sql, params={})]
    got = ask.parent_reuse(parent("tt_1", queries=queries), market="ZA", window=child, as_of=NOW)
    assert [q["params"] for q in got["queries"]] == [{"since": "2026-09-22", "tags": ["a", "b"], "n": 3}, {}]


def test_at_most_six_queries_cross_and_their_purposes_are_bounded():
    child = (date(2026, 9, 22), date(2026, 9, 28))
    queries = [query(sql=COUNT + f" LIMIT {i + 1}", purpose="p" * 900) for i in range(20)]
    got = ask.parent_reuse(parent("tt_1", queries=queries), market="ZA", window=child, as_of=NOW)
    assert len(got["queries"]) == ask.PARENT_REUSE_QUERIES == 6
    assert all(len(q["purpose"]) <= 200 for q in got["queries"])


# What the child run does with it


def test_the_parents_posts_are_in_evidence_before_the_first_research_turn_and_read_inside_the_childs_window():
    out, seen, h = follow(parent("tt_1", "tt_2", "x_1"))
    assert seen["fetches"] == 1 and h.warehouse.fetches[0]["ids"] == ["tt_1", "tt_2", "x_1"]
    assert (h.warehouse.fetches[0]["since"], h.warehouse.fetches[0]["until"]) == (date(2026, 9, 22), date(2026, 9, 28))
    assert {"tt_1", "tt_2", "x_1"} <= set(seen["evidence"])
    assert ask.REUSE_NOTE in seen["prompt"] and "tt_1, tt_2, x_1" in seen["prompt"]
    assert any("Re-reading" in (s or "") for s in seen["steps"])
    assert out["answer"] is not None


def test_a_reused_post_is_what_the_store_holds_not_what_the_parent_payload_says():
    answer = parent_answer("tt_1", extra={"evidence": [{"id": "tt_1", "text": "TAMPERED", "market": "NG",
                                                         "handle": "@forged", "posted_at": "2026-09-25T10:00:00+02:00"}]})
    out, seen, h = follow(parent(answer=answer))
    record = seen["evidence"]["tt_1"]
    assert record["text"] == STORE["tt_1"]["text"] and record["handle"] == STORE["tt_1"]["handle"]
    assert record["market"] == "ZA" and "TAMPERED" not in seen["prompt"]


def test_a_cited_post_outside_the_childs_window_is_left_out():
    out, seen, h = follow(parent("tt_1", "tt_old"))
    assert "tt_old" not in seen["evidence"] and "tt_1" in seen["evidence"]
    assert "tt_old" not in repr(out["answer"]["evidence"])


def test_a_cited_post_from_another_market_is_left_out():
    out, seen, h = follow(parent("tt_1", "tt_ng"))
    assert "tt_ng" not in seen["evidence"] and "tt_1" in seen["evidence"]


def test_the_childs_own_window_bounds_the_fetch_and_the_parents_queries_stay_home():
    out, seen, h = follow(parent("tt_1", queries=[query()]), question="Which creators drove it over the last 10 days?")
    assert (h.warehouse.fetches[0]["since"], h.warehouse.fetches[0]["until"]) == (date(2026, 9, 19), date(2026, 9, 28))
    assert not any(q["sql"] == COUNT for q in seen["queries"].values())


def test_a_different_market_reads_nothing_of_the_parent():
    out, seen, h = follow(parent("tt_1", market="NG", queries=[query()]), market="ZA")
    assert seen["fetches"] == 0 and "tt_1" not in seen["evidence"]
    assert ask.REUSE_NOTE not in seen["prompt"]


def test_the_parents_queries_are_rerun_under_new_ids_with_fresh_hashes():
    out, seen, h = follow(parent("tt_1", queries=[query()]))
    reruns = [(qid, q) for qid, q in seen["queries"].items() if q["sql"] == COUNT]
    assert len(reruns) == 1
    qid, q = reruns[0]
    assert q["purpose"].startswith(ask.REUSE_PURPOSE) and "posts and platforms in the window" in q["purpose"]
    assert q["result_hash"] == result_hash(q["rows"])  # recomputed here, never read from the parent
    assert qid in seen["prompt"]


def test_a_parent_query_the_guard_refuses_is_skipped_and_the_run_goes_on():
    bad = query(sql="SELECT table_name FROM intelligence_42_core.INFORMATION_SCHEMA.TABLES", purpose="schema peek")
    out, seen, h = follow(parent("tt_1", queries=[bad, query()]))
    assert [q["sql"] for q in seen["queries"].values() if "INFORMATION_SCHEMA" in q["sql"]] == []
    assert any(q["sql"] == COUNT for q in seen["queries"].values())
    assert "schema peek" not in seen["prompt"]
    assert out["answer"] is not None


def test_reuse_replaces_the_opening_lookups_and_a_first_question_keeps_them():
    out, seen, h = follow(parent("tt_1"))
    assert ask.OPENING_NOTE not in seen["prompt"]
    first = {}

    def research(ctx, prompt, options, emit, should_stop):
        first["prompt"] = prompt
        return {"note": "", "tokens": {"input": 1, "output": 1}, "usd": 0.0}

    Harness(research=research).run(market="ZA")
    assert ask.OPENING_NOTE in first["prompt"]


def test_when_nothing_of_the_parent_survives_the_opening_lookups_still_run():
    out, seen, h = follow(parent("tt_old", "tt_ng"))
    assert ask.REUSE_NOTE not in seen["prompt"] and ask.OPENING_NOTE in seen["prompt"]


def test_a_failing_parent_fetch_does_not_fail_the_follow_up():
    class Broken(Store):
        def run(self, sql, params, max_bytes_billed):
            if "p.post_id IN" in sql:
                raise RuntimeError("bigquery unavailable")
            return super().run(sql, params, max_bytes_billed)

    seen = {}

    def research(ctx, prompt, options, emit, should_stop):
        seen["prompt"] = prompt
        return {"note": "", "tokens": {"input": 1, "output": 1}, "usd": 0.0}

    h = Harness(research=research)
    h.warehouse = h.deps.warehouse = Broken()
    out = h.run(question=FOLLOW_UP, parent=parent("tt_1"), market="ZA")
    assert out["answer"] is not None and ask.REUSE_NOTE not in seen["prompt"]


def test_reuse_adds_no_credits_and_no_model_spend():
    plain = Harness()
    base = plain.run(question=FOLLOW_UP, parent=parent(), market="ZA")["run"]
    out, _, _ = follow(parent("tt_1", "tt_2", queries=[query()]))
    assert out["run"]["credits"] <= base["credits"]
    assert out["run"]["model_usd"] <= base["model_usd"] + 1e-9
