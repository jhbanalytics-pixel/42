"""Ask at T2 (BUILD.md 1.17): parallel researchers per platform group, the critic in a fresh context and at most one
gap round, all with fakes: no network, no model, no SocialCrawl."""

import copy
import json
import re
import threading
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from core.agent import ask, critic, gemini_research
from core.agent.answer import validate_answer
from core.agent.context import TIERS, Refused, RunContext
from core.agent.tests import test_ask
from core.agent.tests.test_ask import COUNT_SQL, NOW, FakeModel, Harness, K4IntegrationModel
from core.agent.tools.socialcrawl import ALLOWED_ROUTES, socialcrawl_call
from core.agent.tools.sql_query import sql_query
from core.agent.tools.warehouse import search_posts
from core.agent.writer import (FIELDS_SCHEMA, K4_REWRITE_SCHEMA, K4_REWRITE_CALLS, K4_RECHECK_CALLS,
                               SUPPORT_SCHEMA, WRITER_SCHEMA)
from core.llm.provider import price_for, reserve_output

MARK = "CRITICMARKER"  # stands for any critic-written text; it must never reach an event, a row or the answer
ZA_GROUPS = ["tiktok", "x", "instagram", "youtube"]  # a one-market question gets four researchers
COUNT_QUERY_PURPOSE = "posts, authors and platforms for amapiano"


class Lanes:
    """The research fake for T2. Records every call; the first `parties` calls meet at a barrier, so researchers run
    one after another would time out there. Each tries a TikTok and a Threads live search."""

    def __init__(self, parties=4, overspend=0.0):
        self.calls, self.lock = [], threading.Lock()
        self.parties = parties
        self.barrier = threading.Barrier(parties, timeout=5) if parties else None
        self.overspend = overspend

    def __call__(self, ctx, prompt, options, emit, should_stop):
        with self.lock:
            index = len(self.calls)
            call = {"ctx": ctx, "prompt": prompt, "options": options, "credits_left": ctx.credits_left(),
                    "calls_left": ctx.calls_left(), "budget": dict(ctx.budget), "refused": [], "ran": []}
            self.calls.append(call)
        if self.barrier is not None and index < self.parties:
            self.barrier.wait()
        search_posts(ctx, options.warehouse, "amapiano")
        sql_query(ctx, options.warehouse, COUNT_SQL, purpose=COUNT_QUERY_PURPOSE,
                  params={"term": "amapiano"})
        for platform, endpoint in (("tiktok", "search/top"), ("threads", "search")):
            try:
                socialcrawl_call(ctx, options.client, platform, endpoint, {"query": "amapiano"}, max_credits=5)
                call["ran"].append(platform)
            except Refused:
                call["refused"].append(platform)
        ctx.credits_spent += self.overspend
        return {"note": "Reading: amapiano, South Africa, this week.", "tokens": {"input": 100, "output": 10},
                "usd": 0.01}

    def first_round(self):
        return self.calls[:self.parties]


def _count_query_id_from_writer_prompt(user):
    pattern = (r"^query (?P<header>\{[^\n]*\})\nrows shown:[^\n]*\n"
               r"<untrusted_content>\n(?P<rows>.*?)\n</untrusted_content>")
    for match in re.finditer(pattern, user, re.MULTILINE | re.DOTALL):
        header = json.loads(match.group("header"))
        if header.get("purpose") != COUNT_QUERY_PURPOSE:
            continue
        result_rows = json.loads(match.group("rows"))
        if any(isinstance(row, dict) and row.get("posts") == 6 and row.get("authors") == 5
               for row in result_rows):
            return header.get("query_id")
    return None


class CriticModel(FakeModel):
    """FakeModel, plus the critic: one script per critic call, claim id -> {"verdict", "label", "query"}; unnamed
    claims are kept. Every text field the critic writes carries MARK."""

    def __init__(self, *scripts, followups=True):
        super().__init__()
        self.scripts = list(scripts)
        self.followups = followups

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is not critic.CRITIC_SCHEMA:
            answer, usage = super().complete_json(
                system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)
            if schema is WRITER_SCHEMA:
                query_id = _count_query_id_from_writer_prompt(user)
                if query_id:
                    for claim in answer.get("claims") or []:
                        for number in claim.get("numbers") or []:
                            if number.get("value") in (6, 5):
                                number["query_id"] = query_id
            return answer, usage
        self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
        script = self.scripts.pop(0) if self.scripts else {}
        verdicts = []
        for cid in dict.fromkeys(re.findall(r'"id": "(c\d+)"', user)):
            v = {"verdict": "keep", "label": "", **script.get(cid, {})}
            query = v.get("query", f"{MARK} amapiano braai") if v["verdict"] == "needs_evidence" else ""
            verdicts.append({"claim_id": cid, "verdict": v["verdict"], "label": v["label"], "reason": f"{MARK} reason",
                             "quote": "", "query": query})
        followups = [{"platform": "reddit", "route": "reddit/search", "query": f"{MARK} amapiano",
                      "estimated_credits": 2, "value_per_credit": 1.0}] if self.followups else []
        return ({"verdicts": verdicts, "missing_perspectives": [f"{MARK} perspective"], "followups": followups,
                 "overall_risk": "medium"}, {"input_tokens": 3000, "output_tokens": 400, "usd": 0.015})

    def by(self, schema):
        return [c for c in self.calls if c["schema"] is schema]


def test_t2_writer_fixture_uses_the_count_query_id_from_its_prompt():
    user = ("query {\"query_id\": \"q_3\", \"purpose\": \"semantic search\"}\n"
            "rows shown: 1 of 1\n<untrusted_content>\n"
            "[{\"posts\": 6, \"authors\": 5}]\n</untrusted_content>\n"
            f"query {{\"query_id\": \"q_17\", \"purpose\": \"{COUNT_QUERY_PURPOSE}\"}}\n"
            "rows shown: 1 of 1\n<untrusted_content>\n"
            "[{\"posts\": 6, \"authors\": 5, \"platforms\": 2}]\n</untrusted_content>")
    model = CriticModel()

    answer, _ = model.complete_json(system="", user=user, schema=WRITER_SCHEMA,
                                    model="fake", max_tokens=1)

    assert [number["query_id"] for number in answer["claims"][0]["numbers"]] == ["q_17", "q_17"]


def t2(lanes=None, model=None, **kwargs):
    return Harness(research=lanes or Lanes(), model=model or CriticModel(), **kwargs)


def claim_events(h):
    return {e["claim"]["id"]: e for e in h.events if e["event"] == "claim"}


def critic_steps(h):
    return [e for e in h.events if e["event"] == "step" and e["kind"] == "critic"]


def rows(h):
    return [r for _, batch in h.tables.inserts for r in batch]


# Tiers and platform groups


def test_t2_is_in_the_tiers_with_the_agent_md_budget():
    assert TIERS["T2"] == {"credits": 300, "calls": 60, "max_turns": 40, "max_budget_usd": 6.00}
    assert ask.FIRST_ROUND_SHARE + ask.GAP_SHARE + ask.ENRICH_SHARE == pytest.approx(1.0)
    assert (ask.FIRST_ROUND_SHARE, ask.GAP_SHARE, ask.ENRICH_SHARE) == (0.55, 0.25, 0.2)
    assert (ask.MIN_RESEARCHERS, ask.MAX_RESEARCHERS) == (3, 5)
    # every researcher loop, the gap researcher's included, fits the whole ask's research budget
    assert ask.researcher_usd() * (ask.MAX_RESEARCHERS + 1) <= TIERS["T2"]["max_budget_usd"] + 1e-9


@pytest.mark.parametrize("markets, groups", [
    (["ZA"], ZA_GROUPS),
    (["NG"], ["x", "tiktok", "instagram", "news"]),
    (["KE"], ["x", "tiktok", "facebook", "news"]),
    (["ZA", "NG"], ["tiktok", "x", "instagram", "youtube", "news"]),
    (["ZA", "NG", "KE"], ["tiktok", "x", "instagram", "facebook", "youtube"]),
    ([], ["tiktok", "x", "instagram"]),
])
def test_platform_groups_follow_the_markets(markets, groups):
    assert ask.platform_groups(markets) == groups


def test_every_group_names_only_allowed_routes():
    for group in ask.PLATFORM_GROUPS:
        routes = ask.group_routes(group)
        assert routes and set(routes) <= ALLOWED_ROUTES
    assert "search/multi" in ask.group_routes("news") and "threads/search" in ask.group_routes("reddit_threads")


def test_t3_still_raises_and_t2_is_accepted():
    with pytest.raises(ValueError):
        t2().run(tier="T3")
    assert t2().run(tier="T2")["run"]["tier"] == "T2"


# Researchers


def test_t2_runs_one_researcher_per_group_concurrently_on_scoped_budgets():
    lanes = Lanes(parties=4)
    h = t2(lanes)
    out = h.run(tier="T2")
    first = lanes.first_round()
    assert len(first) == 4  # the barrier passed, so all four ran at once
    assert len({id(c["ctx"]) for c in first}) == 4
    slice_ = 300 / 4
    for call in first:
        assert call["budget"] == {"credits": slice_, "calls": 11, "max_turns": 40,
                                  "max_budget_usd": ask.researcher_usd(), "credit_cap": 0.75 * slice_}
        assert call["credits_left"] == pytest.approx(0.75 * slice_)  # the lane's 25% stays for the gap round
        assert call["calls_left"] == 11
        assert call["ctx"].queries is first[0]["ctx"].queries  # query ids stay unique across researchers
    prompts = [c["prompt"] for c in first]
    for group in ZA_GROUPS:
        assert sum(", ".join(ask.group_routes(group)) in p for p in prompts) == 1
    for prompt in prompts:
        assert prompt.startswith("Question: What is behind amapiano")
        assert "41.25 credits" in prompt  # its fair share of the 55% first round
    assert out["run"]["credits"] <= 0.75 * 300
    assert validate_answer(out["answer"]) == []


def test_each_researcher_may_call_only_its_own_platforms():
    lanes = Lanes(parties=4)
    h = t2(lanes)
    out = h.run(tier="T2")
    first = lanes.first_round()
    assert sorted(c["ran"] for c in first) == [[], [], [], ["tiktok"]]
    assert all("threads" in c["refused"] for c in first)
    assert [c["route"] for c in h.clients[0].calls] == ["tiktok/search/top"]
    assert out["run"]["source_status"] == [{"platform": "tiktok", "route": "tiktok/search/top",
                                            "status": "rate_limited", "items": 0}]
    assert out["run"]["credits"] == 1.0


def test_the_researchers_spend_and_finds_are_summed_into_the_run():
    lanes = Lanes(parties=4)
    h = t2(lanes)
    out = h.run(tier="T2")
    run = out["run"]
    assert run["posts"] == 6
    assert run["tokens"]["input"] >= 4 * 100 and run["model_usd"] >= 4 * 0.01
    evidence = [e["evidence"]["id"] for e in h.events if e["event"] == "evidence"]
    assert len(evidence) == len(set(evidence)) == 6  # a post two researchers found is streamed once


def test_record_query_ids_stay_unique_across_threads():
    ctx = RunContext(run_id="r", tier="T2", as_of=NOW)
    lanes = [ctx.lane({"credits": 10, "calls": 1, "max_turns": 1, "max_budget_usd": 0.1}) for _ in range(8)]

    def work(lane):
        for i in range(200):
            lane.record_query("SELECT 1", {}, [{"i": i}], "p")

    threads = [threading.Thread(target=work, args=(lane,)) for lane in lanes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ctx.queries) == 1600


def test_merge_sums_counters_and_keeps_call_order():
    ctx = RunContext(run_id="r", tier="T2", as_of=NOW)
    a, b = (ctx.lane({"credits": 75, "calls": 11, "max_turns": 40, "max_budget_usd": 1.0}) for _ in range(2))
    a.credits_spent, a.calls_made, a.model_usd_extra, a.enrich_credits_spent = 3.0, 2, 0.01, 1.0
    b.credits_spent, b.calls_made, b.model_usd_extra = 4.0, 1, 0.02
    a.emit("socialcrawl", route="tiktok/search/top")
    b.emit("socialcrawl", route="twitter/search/tweets")
    a.evidence["p1"] = {"id": "p1", "from": "a"}
    b.evidence["p1"] = {"id": "p1", "from": "b"}
    ctx.merge(a)
    ctx.merge(b)
    assert (ctx.credits_spent, ctx.calls_made, ctx.enrich_credits_spent) == (7.0, 3, 1.0)
    assert ctx.model_usd_extra == pytest.approx(0.03)
    assert [e["route"] for e in ctx.events] == ["tiktok/search/top", "twitter/search/tweets"]
    assert ctx.evidence["p1"]["from"] == "a"


def test_a_lanes_budget_caps_credits_below_its_slice_but_enrichment_keeps_its_share():
    from core.agent.tools.enrich_tools import enrich_credits_left

    ctx = RunContext(run_id="r", tier="T2", as_of=NOW)
    lane = ctx.lane({"credits": 75.0, "calls": 11, "max_turns": 40, "max_budget_usd": 1.0, "credit_cap": 56.25})
    assert lane.credits_left() == 56.25 and enrich_credits_left(lane) == pytest.approx(15.0)
    lane.credits_spent = 50.0
    assert lane.credits_left() == pytest.approx(6.25) and enrich_credits_left(lane) == pytest.approx(6.25)
    assert ctx.credits_left() == 300  # the lane's spend reaches the ask only at the merge


# The critic


def test_the_critics_cut_withholds_and_its_downgrade_lowers():
    model = CriticModel({"c1": {"verdict": "downgrade", "label": "observed"}, "c3": {"verdict": "cut"}})
    h = t2(model=model)
    out = h.run(tier="T2")
    answer = out["answer"]
    assert validate_answer(answer) == []
    assert [c["id"] for c in answer["claims"]] == ["c1"]
    assert answer["claims"][0]["label"] == "observed"
    assert answer["status"] == "insufficient_evidence"  # one claim left
    events = claim_events(h)
    assert events["c1"]["check"] == "downgraded" and events["c1"]["reason"] == "label lowered to observed (critic)"
    assert events["c3"]["check"] == "cut" and events["c3"]["reason"] == critic.ROW_REASON["cut"]
    assert events["c3"]["claim"]["text"] == "Claim withheld by the trust gate (critic)"
    assert events["c3"]["claim"]["quotes"] == [] and events["c3"]["claim"]["numbers"] == []
    got = {(r["claim_id"], r["rule"], r["verdict"], r["checker"]) for r in rows(h)}
    assert ("c1", "critic", "downgrade", "critic") in got and ("c3", "critic", "cut", "critic") in got
    assert critic_steps(h) == [{"event": "step", "at": "2026-09-28T08:15:00+02:00", "kind": "critic",
                                "text": "Critic: 0 kept, 1 lowered, 1 cut, 0 pending", "platform": None,
                                "count": 2, "counts": {"kept": 0, "lowered": 1, "cut": 1, "pending": 0}}]
    assert len(model.by(critic.CRITIC_SCHEMA)) == 1


def test_a_critic_that_keeps_everything_changes_nothing():
    h = t2()
    out = h.run(tier="T2")
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1", "c3"]
    assert critic_steps(h)[0]["counts"] == {"kept": 2, "lowered": 0, "cut": 0, "pending": 0}


def test_no_critic_text_reaches_events_rows_or_the_answer():
    model = CriticModel({"c1": {"verdict": "downgrade", "label": "observed"}, "c3": {"verdict": "needs_evidence"}},
                        {"c3": {"verdict": "needs_evidence"}})
    h = t2(model=model)
    out = h.run(tier="T2")
    assert len(model.by(critic.CRITIC_SCHEMA)) == 2
    assert MARK not in json.dumps(h.events, default=str)
    assert MARK not in json.dumps(h.tables.inserts, default=str)
    assert MARK not in json.dumps(out, default=str)


def test_a_failed_critic_leaves_the_answer_and_says_so():
    class Failing(CriticModel):
        def complete_json(self, *, system, user, schema, model, max_tokens):
            if schema is critic.CRITIC_SCHEMA:
                raise RuntimeError(f"{MARK} boom")
            return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)

    h = t2(model=Failing())
    out = h.run(tier="T2")
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1", "c3"]
    assert ask.CRITIC_FAILED in out["run"]["notices"]
    assert MARK not in json.dumps(h.events, default=str) + json.dumps(out, default=str)


# The gap round


def test_a_pending_claim_triggers_one_gap_round_and_one_only():
    model = CriticModel({"c3": {"verdict": "needs_evidence"}}, {"c3": {"verdict": "needs_evidence"}},
                        {"c3": {"verdict": "needs_evidence"}})
    lanes = Lanes(parties=4)
    h = t2(lanes, model)
    out = h.run(tier="T2")
    assert len(lanes.calls) == 5  # four researchers, then one gap researcher
    gap = lanes.calls[4]
    assert f"{MARK} amapiano braai" in gap["prompt"] and "reddit/search" in gap["prompt"]
    assert gap["budget"]["credits"] == pytest.approx(0.25 * 300) and gap["budget"]["max_turns"] == 40
    assert gap["ran"] == ["tiktok", "threads"]  # the gap researcher is not held to one platform
    assert len(model.by(critic.CRITIC_SCHEMA)) == 2 and len(model.by(WRITER_SCHEMA)) == 2
    assert len(model.by(FIELDS_SCHEMA)) == 2
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1"]  # still pending after the gap round, so cut
    assert claim_events(h)["c3"]["check"] == "cut"
    assert claim_events(h)["c3"]["reason"] == critic.ROW_REASON["pending"]
    assert ("c3", "critic", "cut", "critic") in {(r["claim_id"], r["rule"], r["verdict"], r["checker"])
                                                  for r in rows(h)}
    assert [s["counts"]["pending"] for s in critic_steps(h)] == [1, 1]
    assert validate_answer(out["answer"]) == []


def test_numeric_repair_runs_once_for_multiple_issues_across_the_gap_pass(monkeypatch):
    issues = [{"claim_id": "c1", "numerals": ["20%"], "invalid_numbers": []},
              {"claim_id": "c3", "numerals": ["40%"], "invalid_numbers": []}]
    repairs = []
    monkeypatch.setattr(ask, "unpinned_claim_numerals", lambda *args, **kwargs: issues)

    def repair(model, *, draft, issues, **kwargs):
        repairs.append(copy.deepcopy(issues))
        return draft, {"input_tokens": 11, "output_tokens": 7, "usd": 0.0002}

    monkeypatch.setattr(ask, "repair_answer_numbers", repair)
    lanes = Lanes(parties=4)
    model = CriticModel({"c3": {"verdict": "needs_evidence"}}, {})
    h = t2(lanes, model, check=None)

    h.run(tier="T2")

    assert len(lanes.calls) > 4
    assert repairs == [issues]


def test_a_claim_the_gap_round_resolves_is_kept():
    model = CriticModel({"c3": {"verdict": "needs_evidence"}}, {})
    lanes = Lanes(parties=4)
    h = t2(lanes, model)
    out = h.run(tier="T2")
    assert len(lanes.calls) == 5
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1", "c3"]
    assert claim_events(h)["c3"]["check"] == "downgraded"  # K5's code downgrade, as at T1
    assert out["run"]["source_status"][-2:] == [
        {"platform": "tiktok", "route": "tiktok/search/top", "status": "rate_limited", "items": 0},
        {"platform": "threads", "route": "threads/search", "status": "rate_limited", "items": 0}]


def test_real_t2_gap_pass_does_not_retry_a_claims_k4_rewrite():
    lanes = Lanes(parties=4)
    model = K4IntegrationModel(critic_pending=True)
    h = Harness(research=lanes, check=None, model=model)
    out = h.run(tier="T2")

    assert len(lanes.calls) == 5
    assert "What else do TikTok posts say about amapiano?" in lanes.calls[-1]["prompt"]
    assert sum(call["schema"] is WRITER_SCHEMA for call in model.calls) == 2
    assert sum(call["schema"] is K4_REWRITE_SCHEMA for call in model.calls) == 1
    assert sum(call["schema"] is SUPPORT_SCHEMA for call in model.calls) == 3
    assert out["run"]["tier"] == "T2"
    assert validate_answer(out["answer"]) == []
    (event,) = [event for event in h.events if event["event"] == "claim"]
    assert set(event) == {"event", "claim", "check", "reason"}
    assert event["check"] == "cut"
    assert test_ask.ORIGINAL_CLAIM not in json.dumps([h.events, out["answer"]], default=str)
    assert test_ask.STALE_HEADLINE not in json.dumps([h.events, out["answer"]], default=str)
    assert test_ask.STALE_CONTEXT not in json.dumps([h.events, out["answer"]], default=str)
    assert out["answer"]["so_what"] == [] and out["answer"]["watch_next"] == []
    assert any(row["claim_id"] == "c2" and row["rule"] == "K4" and row["verdict"] == "cut" for row in rows(h))


def test_no_reserve_left_means_pending_claims_are_cut():
    model = CriticModel({"c3": {"verdict": "needs_evidence"}})
    lanes = Lanes(parties=4, overspend=100.0)  # four researchers spend past the whole tier
    h = t2(lanes, model)
    out = h.run(tier="T2")
    assert len(lanes.calls) == 4 and len(model.by(critic.CRITIC_SCHEMA)) == 1
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1"]
    assert claim_events(h)["c3"]["reason"] == critic.ROW_REASON["pending"]
    assert validate_answer(out["answer"]) == []


def test_no_followups_left_means_pending_claims_are_cut():
    # a needs_evidence query that breaches is dropped by the critic, and there are no follow-up fetches
    model = CriticModel({"c3": {"verdict": "needs_evidence", "query": "gen z amapiano"}}, followups=False)
    lanes = Lanes(parties=4)
    h = t2(lanes, model)
    out = h.run(tier="T2")
    assert len(lanes.calls) == 4
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1"]


# The hold


def critic_call_usd(model_name):
    price = price_for(model_name)
    return (ask.CRITIC_INPUT_TOKENS * price["input"]
            + reserve_output(model_name, ask.CRITIC_MAX_TOKENS) * price["output"]) / 1e6


def test_the_t2_hold_covers_every_researcher_two_passes_and_two_critic_calls():
    model = ask.MODEL
    one_pass = ask.pass_usd(model)
    repair_call = ask.call_usd(model, ask.WRITER_INPUT_TOKENS, ask.WRITER_MAX_TOKENS)
    # the one short answer rewrite an ask may make, held once like the numeric repair
    headline_call = ask.call_usd(model, ask.HEADLINE_REWRITE_INPUT_TOKENS, ask.HEADLINE_REWRITE_MAX_TOKENS)
    expected = 6.00 + 2 * one_pass + 2 * critic_call_usd(critic.critic_model()) + repair_call + headline_call
    assert ask.hold_usd("T2", model) == pytest.approx(expected)
    assert ask.CRITIC_MAX_TOKENS == critic.BASE_TOKENS + critic.TOKENS_PER_CLAIM * ask.MAX_CLAIMS
    assert K4_REWRITE_CALLS == K4_RECHECK_CALLS == ask.MAX_CLAIMS
    assert ask.hold_usd("T2", model) < ask.model_daily_usd(now=NOW)


def test_the_t2_hold_prices_the_critic_at_its_own_model(monkeypatch):
    from core.llm.provider import GEMINI_LIST_PRICES

    monkeypatch.setitem(GEMINI_LIST_PRICES, "gemini-critic", {"input": 3.00, "output": 15.00, "cached": 0.30})
    base, default = ask.hold_usd("T2", ask.MODEL), critic.critic_model()
    monkeypatch.setenv("CRITIC_MODEL", "gemini-critic")
    assert critic_call_usd("gemini-critic") > critic_call_usd(default)
    assert ask.hold_usd("T2", ask.MODEL) == pytest.approx(
        base - 2 * critic_call_usd(default) + 2 * critic_call_usd("gemini-critic"))


def test_every_model_call_a_t2_ask_makes_is_inside_the_hold():
    model = CriticModel({"c3": {"verdict": "needs_evidence"}}, {})
    h = t2(model=model)
    h.run(tier="T2")
    critic_calls = model.by(critic.CRITIC_SCHEMA)
    assert len(model.by(WRITER_SCHEMA)) <= 2 + ask.NUMERIC_REPAIR_CALLS
    assert len(model.by(FIELDS_SCHEMA)) <= 2 * ask.FIELD_CALLS
    assert len(model.by(SUPPORT_SCHEMA)) <= 2 * ask.SUPPORT_CALLS + K4_RECHECK_CALLS
    assert len(model.by(K4_REWRITE_SCHEMA)) <= K4_REWRITE_CALLS
    assert 1 <= len(critic_calls) <= 2
    assert all(c["model"] == critic.critic_model() and c["max_tokens"] <= ask.CRITIC_MAX_TOKENS for c in critic_calls)


def test_the_critic_spend_is_in_the_run():
    h = t2()
    out = h.run(tier="T2")
    assert out["run"]["model_usd"] >= 4 * 0.01 + 0.015
    assert out["run"]["tokens"]["input"] >= 4 * 100 + 3000


def test_a_t2_hold_that_does_not_fit_falls_back_to_t0():
    lanes = Lanes(parties=0)
    h = t2(lanes, spent=lambda: ask.model_daily_usd(now=NOW) - ask.hold_usd("T2", ask.MODEL) + 0.01)
    out = h.run(tier="T2")
    assert out["run"]["tier"] == "T0" and len(lanes.calls) == 1
    assert lanes.calls[0]["ctx"].tier == "T0" and not critic_steps(h)


def test_a_researcher_that_fails_charges_the_whole_research_budget():
    class Broken(Lanes):
        def __call__(self, ctx, prompt, options, emit, should_stop):
            super().__call__(ctx, prompt, options, emit, should_stop)
            if "X" in prompt.split("Your platforms: ", 1)[1].split(".", 1)[0]:
                raise RuntimeError("research broke")
            return {"note": "", "tokens": {"input": 1, "output": 1}, "usd": 0.01}

    h = t2(Broken(parties=4))
    with pytest.raises(RuntimeError) as err:
        h.run(tier="T2")
    assert err.value.run["model_usd"] >= TIERS["T2"]["max_budget_usd"]
    assert err.value.run["credits"] == 1.0  # the TikTok researcher's call still counts


# Both research loops honour a researcher's own limits


def gemini_reply(*parts, pin=100):
    return NS(candidates=[NS(content=NS(role="model", parts=list(parts)), finish_reason=NS(name="STOP"))],
              usage_metadata=NS(prompt_token_count=pin, response_token_count=10, thoughts_token_count=5,
                                tool_use_prompt_token_count=0, candidates_token_count=None))


class GeminiLanes:
    """A thread-safe fake Gemini client: the first turn of each loop asks for budget_status and a stored-post search,
    the next ends it."""

    def __init__(self, pin=100):
        self.lock, self.calls, self.pin = threading.Lock(), [], pin
        self.models = NS(generate_content=self.generate)

    def generate(self, **kw):
        contents = list(kw["contents"])
        with self.lock:
            self.calls.append({**kw, "contents": contents})
        if len(contents) == 1:
            budget = NS(function_call=NS(id="call_budget", name="budget_status", args={}), text=None, thought=False)
            search = NS(function_call=NS(id="call_search", name="search_posts", args={"query": "amapiano"}), text=None,
                        thought=False)
            return gemini_reply(budget, search, pin=self.pin)
        return gemini_reply(NS(function_call=None, text="Reading: amapiano.", thought=False), pin=self.pin)


@pytest.fixture
def gemini_env(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "10")


class BudgetLoop:
    """A fake Gemini client that asks for budget_status on every turn, so only a limit ends the loop."""

    def __init__(self):
        self.calls = []
        self.models = NS(generate_content=self.generate)

    def generate(self, **kw):
        self.calls.append(kw)
        return gemini_reply(NS(function_call=NS(id="call_budget", name="budget_status", args={}), text=None,
                               thought=False))


def loop_setup(tier="T1"):
    ctx = RunContext(run_id="r", tier=tier, as_of=NOW, market="ZA")
    progress = ask.Progress(lambda e: None, lambda: NOW, market_label="South Africa",
                            window=(datetime(2026, 9, 22).date(), datetime(2026, 9, 28).date()))
    options = ask.ResearchSetup(system_prompt="s", model="gemini-3.8-flash", warehouse=None, client=None, tables=None)
    return ctx, progress, options


def test_the_gemini_loop_takes_a_researchers_turns(monkeypatch, gemini_env):
    client = BudgetLoop()
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    ctx, progress, options = loop_setup("T2")
    lane = ctx.lane({"credits": 75.0, "calls": 11, "max_turns": 3, "max_budget_usd": 1.0, "credit_cap": 56.25})
    assert lane.budget["max_turns"] == 3
    gemini_research.gemini_research(lane, "Q?", options, progress, lambda: False)
    assert len(client.calls) == 3


def test_the_gemini_loop_keeps_the_tier_limits_at_t1(monkeypatch, gemini_env):
    client = BudgetLoop()
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    ctx, progress, options = loop_setup("T1")
    assert (ctx.budget["max_turns"], ctx.budget["max_budget_usd"]) == (30, 2.00)
    gemini_research.gemini_research(ctx, "Q?", options, progress, lambda: False)
    assert len(client.calls) == 30


def test_the_gemini_loop_stops_at_a_researchers_usd(monkeypatch, gemini_env):
    client = GeminiLanes(pin=2_000_000)  # USD 2 a turn: over a researcher's USD 1, under the tier's USD 6
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    ctx = RunContext(run_id="r", tier="T2", as_of=NOW, market="ZA")
    lane = ctx.lane({"credits": 75.0, "calls": 11, "max_turns": 40, "max_budget_usd": 1.0, "credit_cap": 56.25})
    progress = ask.Progress(lambda e: None, lambda: NOW, market_label="South Africa",
                            window=(datetime(2026, 9, 22).date(), datetime(2026, 9, 28).date()))
    options = ask.ResearchSetup(system_prompt="s", model="gemini-3.8-flash", warehouse=None, client=None, tables=None)
    result = gemini_research.gemini_research(lane, "Q?", options, progress, lambda: False)
    assert len(client.calls) == 1 and "budget" in result["note"]


def test_t2_runs_on_the_gemini_loop_with_the_fake_client(monkeypatch, gemini_env):
    client = GeminiLanes()
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    h = Harness(research=gemini_research.gemini_research, model=CriticModel())
    out = h.run(tier="T2")
    assert out["run"]["tier"] == "T2"
    firsts = [c for c in client.calls if len(c["contents"]) == 1]
    assert len(firsts) == 4
    prompts = sorted(c["contents"][0].parts[0].text for c in firsts)
    assert all("Your platforms: " in p for p in prompts)
    budgets = [json.loads(c["contents"][-1].parts[0].function_response.response["output"])
               for c in client.calls if len(c["contents"]) == 3]
    assert budgets == [{"credits_left": 56.25, "calls_left": 11}] * 4
    assert critic_steps(h) and validate_answer(out["answer"]) == []


# Hydration at T2: researchers list posts through sql_query in their own lanes; after the merge, run_ask fetches the
# listed posts once, before the writer, so the claims citing them pass K1.


class CitingCriticModel(CriticModel):
    writer_out = test_ask.CITING_OUT


def test_t2_hydrates_query_row_ids_from_every_lane_before_the_writer():
    problems = {}
    h = test_ask.listing_harness(problems, research=test_ask.listing_research(), model=CitingCriticModel())
    out = h.run(tier="T2")

    assert problems == {"c1": []}
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1"]
    listings = [p for _, p in h.warehouse.runs if "page" in (p or {})]
    assert len(listings) == len(ZA_GROUPS)  # every lane listed the same two posts
    (fetch,) = h.warehouse.fetches()  # fetched once, after the merge
    assert sorted(fetch.values()) == [test_ask.obs("0_0a"), test_ask.obs("0_1a")]
    evidence = [e["evidence"]["id"] for e in h.events if e["event"] == "evidence"]
    assert sorted(evidence) == sorted(fetch.values())


# The brand lens and context pack at T2 (f42-agent F42_T2_READY): the whole T2 hold is reserved under the daily cap
# before any researcher or model call, and a spend that cannot be read refuses the ask before any of them.


class HoldWatch(Lanes):
    """Lanes that note, at each researcher's start, the holds in flight and the model calls made so far."""

    def __init__(self, model, **kwargs):
        super().__init__(**kwargs)
        self.model, self.seen = model, []

    def __call__(self, ctx, prompt, options, emit, should_stop):
        with self.lock:
            self.seen.append({"in_flight": list(ask._IN_FLIGHT.values()), "model_calls": len(self.model.calls)})
        return super().__call__(ctx, prompt, options, emit, should_stop)


def gemini_ask(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1.5")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "7.5")
    monkeypatch.setattr(ask, "MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(ask, "FALLBACK_MODEL", "gemini-3.8-flash")


@pytest.mark.parametrize("on_gemini", [False, True])
@pytest.mark.parametrize("skill", ["brand-implication", "context-pack"])
def test_a_t2_skill_ask_reserves_the_whole_t2_hold_before_dispatch(monkeypatch, skill, on_gemini):
    if on_gemini:
        gemini_ask(monkeypatch)
    before = dict(ask._IN_FLIGHT)
    model = CriticModel()
    lanes = HoldWatch(model, parties=0)
    h = t2(lanes, model=model, spent=lambda: 0.0)
    out = h.run(tier="T2", skill=skill)
    hold = ask.hold_usd("T2", ask.MODEL)
    assert out["run"]["tier"] == "T2"
    assert lanes.seen, "no researcher ran"
    first = lanes.seen[0]
    assert first["model_calls"] == 0
    assert sorted(first["in_flight"]) == sorted([*before.values(), pytest.approx(hold)])
    assert ask._IN_FLIGHT == before  # released when the ask ends


@pytest.mark.parametrize("on_gemini", [False, True])
@pytest.mark.parametrize("skill", ["brand-implication", "context-pack"])
def test_a_t2_skill_ask_whose_spend_cannot_be_read_is_refused_before_dispatch(monkeypatch, skill, on_gemini):
    if on_gemini:
        gemini_ask(monkeypatch)
    before = dict(ask._IN_FLIGHT)
    model = CriticModel()
    lanes = HoldWatch(model, parties=0)

    def unreadable():
        raise RuntimeError("spend read failed")

    h = t2(lanes, model=model, spent=unreadable)
    out = h.run(tier="T2", skill=skill)
    assert lanes.calls == [] and model.calls == []
    assert out["run"]["credits"] == 0 and out["run"]["model_usd"] == 0
    assert any("could not be read" in n for n in out["run"]["notices"])
    assert ask._IN_FLIGHT == before


@pytest.mark.parametrize("skill", ["brand-implication", "context-pack"])
def test_a_t2_skill_hold_counts_other_asks_in_flight(monkeypatch, skill):
    hold = ask.hold_usd("T2", ask.MODEL)
    cap = ask.model_daily_usd(now=NOW)
    monkeypatch.setitem(ask._IN_FLIGHT, object(), cap - hold + 0.01)  # another ask's hold leaves too little
    model = CriticModel()
    lanes = HoldWatch(model, parties=0)
    out = t2(lanes, model=model, spent=lambda: 0.0).run(tier="T2", skill=skill)
    assert out["run"]["tier"] != "T2"
    assert all(ctx_seen["model_calls"] == 0 for ctx_seen in lanes.seen[:1])
    assert all(c["ctx"].tier != "T2" for c in lanes.calls)
