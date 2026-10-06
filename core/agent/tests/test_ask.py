import copy
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from core.agent import ask, checks, critic, plain, skills
from core.agent.answer import validate_answer
from core.agent.context import TIERS, RunContext, result_hash
from core.agent.tools.socialcrawl import socialcrawl_call
from core.agent.tools.sql_query import MAX_BYTES_BILLED, check_sql, sql_query
from core.agent.tools.warehouse import BigQueryTableWriter, fetch_posts, search_posts
from core.agent.writer import (FIELDS_SCHEMA, K4_RECHECK_CALLS, K4_REWRITE_CALLS, K4_REWRITE_INPUT_TOKENS,
                               K4_REWRITE_MAX_TOKENS, K4_REWRITE_SCHEMA, SUPPORT_SCHEMA, WRITER_SCHEMA)
from core.llm.provider import reserve_output

SAST = timezone(timedelta(hours=2))
NOW = datetime(2026, 9, 28, 8, 15, 0, tzinfo=SAST)
ROOT = Path(__file__).resolve().parents[3]
COUNT_SQL = ("SELECT COUNT(*) AS posts, COUNT(DISTINCT p.creator_id) AS authors, COUNT(DISTINCT p.platform) AS platforms "
             "FROM intelligence_42_core.posts p WHERE CONTAINS_SUBSTR(p.text, @term)")
RUN_KEYS = {"run_id", "tier", "mode", "credits", "tokens", "seconds", "model_usd", "window", "posts", "platforms",
            "source_status", "followups", "notices"}


def query_embed_usd(query):
    from core.understand.embed import CHARS_PER_TOKEN, EMBED_USD_PER_MILLION_TOKENS
    return len(query) / CHARS_PER_TOKEN * EMBED_USD_PER_MILLION_TOKENS / 1e6


# make_research's search_posts embeds 'amapiano' for its semantic half, and the run's model_usd counts it (task 1.11).
SEARCH_EMBED_USD = query_embed_usd("amapiano")
ORIGINAL_CLAIM = "People say amapiano is fading."
NARROWED_CLAIM = "A TikTok post says amapiano cannot stop."
STALE_HEADLINE = "Chatter suggests amapiano's momentum is dropping."
STALE_CONTEXT = "Several posts imply interest is slowing."


def post(pid, platform, handle, text, day):
    return {"post_id": pid, "platform": platform, "url": f"https://example.test/{platform}/{pid}", "handle": handle,
            "published_at": datetime(2026, 9, day, 19, 40, tzinfo=SAST), "post_date": date(2026, 9, day),
            "geo_market": "ZA", "text": text, "views": 1000 * day, "likes": 10 * day, "comments": 3, "shares": 1,
            "engagement": 20 * day}


POSTS = [
    post("tt_1", "tiktok", "@dj_zinhle_fan", "amapiano Sunday session in Soweto, the log drum is everything", 22),
    post("tt_2", "tiktok", "@mzansi_moves", "new amapiano dance challenge from Durban", 24),
    post("tt_3", "tiktok", "@kasi_beats", "amapiano private school piano mix tonight", 25),
    post("tt_4", "tiktok", "@dj_zinhle_fan", "amapiano again, cannot stop", 26),
    post("x_1", "x", "@lebo_says", "Everyone at the braai asked for amapiano", 27),
    post("x_2", "x", "@thabo_za", "amapiano on every taxi rank speaker in Joburg", 27),
]


class FakeWarehouse:
    def __init__(self):
        self.runs = []
        self.control_runs = []

    def dry_run(self, sql, params):
        return {"bytes": 1000, "tables": ["intelligence_42_core.posts"]}

    def run(self, sql, params, max_bytes_billed):
        if "intelligence_42_agent.feedback" in sql:
            self.control_runs.append((sql, params, max_bytes_billed))
            return [{"payload": "[]"}]
        if "intelligence_42_core.post_enrichment" in sql:
            self.control_runs.append((sql, params, max_bytes_billed))
            return [{"payload": "[]"}]
        self.runs.append((sql, params))
        if "COUNT(" in sql:
            return [{"posts": 6, "authors": 5, "platforms": 2}]
        return copy.deepcopy(POSTS)


class FakeClient:
    """Stands in for the SocialCrawl client: Threads is rate limited, one credit charged."""

    def __init__(self, mode, run_id=None):
        self.mode = mode
        self.run_id = run_id
        self.calls = []

    def quote(self, route, params):
        return 1.0

    def call(self, route, params, *, lane, run_id, max_credits):
        self.calls.append({"route": route, "mode": self.mode, "run_id": run_id})
        return {"items": [], "next_cursor": None, "credits_charged": 1.0, "status": "rate_limited", "cache_hit": False}


class FakeTables:
    def __init__(self):
        self.inserts = []

    def insert(self, table, rows, row_ids=None):
        self.inserts.append((table, copy.deepcopy(rows)))


WRITER_OUT = {
    "short_answer": "Amapiano posts in South Africa this week came from five creators on TikTok and X.",
    "claims": [
        {"id": "c1", "text": "6 amapiano posts came from 5 creators on TikTok and X.", "label": "corroborated",
         "kind": "observation", "evidence_ids": ["tt_1", "tt_2", "tt_3", "x_1", "x_2"],
         "quotes": [{"evidence_id": "tt_1", "text": "the log drum is everything"},
                    {"evidence_id": "x_1", "text": "asked for amapiano"}],
         "numbers": [{"value": 6, "unit": "posts", "query_id": "q_3"},
                     {"value": 5, "unit": "creators", "query_id": "q_3"}]},
        {"id": "c2", "text": "People say amapiano is fading.", "label": "single_source", "kind": "interpretation",
         "evidence_ids": ["tt_4"], "quotes": [], "numbers": []},
        {"id": "c3", "text": "A Durban amapiano dance challenge is spreading.", "label": "corroborated",
         "kind": "observation", "evidence_ids": ["tt_2"],
         "quotes": [{"evidence_id": "tt_2", "text": "dance challenge from Durban"}], "numbers": []},
        {"id": "c4", "text": "Taxi ranks play amapiano.", "label": "observed", "kind": "observation",
         "evidence_ids": ["x_2"], "quotes": [{"evidence_id": "x_2", "text": "every taxi rank speaker in Pretoria"}],
         "numbers": []},
    ],
    "so_what": [{"text": "Amapiano is a shared soundtrack across two platforms.", "claim_ids": ["c1"]},
                {"text": "Interest may be cooling.", "claim_ids": ["c2"]}],
    "watch_next": [{"text": "Whether the Durban challenge reaches X.", "claim_ids": ["c1"], "forecast": False}],
    "gaps": [],
    "context": "",
}


CHECK_CALLS = []


class FakeModel:
    """Writer returns WRITER_OUT; the support check passes every claim except c2."""

    writer_out = WRITER_OUT

    def __init__(self):
        self.calls = []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
        if schema is WRITER_SCHEMA:
            return copy.deepcopy(self.writer_out), {"input_tokens": 2000, "output_tokens": 500, "usd": 0.0135}
        if schema is K4_REWRITE_SCHEMA:
            return {"text": ""}, {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
        if schema is FIELDS_SCHEMA:
            indexes = sorted({int(i) for i in re.findall(r"(?m)^item (\d+) ", user)})
            return {"fields": [{"index": i, "demographic_inference": False, "forecast_assertion": False,
                                "country_people": [], "so_what_supported": True}
                               for i in indexes]}, {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
        verdict = "unsupported" if "fading" in user else "supported"
        return {"verdict": verdict, "reason": f"{verdict} by the cited post", "demographic_inference": False,
                "tone_claim": False, "forecast_assertion": False, "country_people": []}, \
            {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}


class NumericRepairModel(FakeModel):
    def __init__(self, writer_drafts):
        super().__init__()
        self.writer_drafts = [copy.deepcopy(draft) for draft in writer_drafts]
        self.writer_usages = [{"input_tokens": 2000, "output_tokens": 500, "usd": 0.0135},
                              {"input_tokens": 37, "output_tokens": 19, "usd": 0.0023}]
        self.usages = []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is WRITER_SCHEMA and self.writer_drafts:
            self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
            out = self.writer_drafts.pop(0)
            usage = self.writer_usages.pop(0)
        else:
            out, usage = super().complete_json(system=system, user=user, schema=schema, model=model,
                                               max_tokens=max_tokens)
        self.usages.append(copy.deepcopy(usage))
        return out, usage


class K4IntegrationModel(FakeModel):
    def __init__(self, *, critic_pending=False, stop_flag=None, fail_rewrite=None):
        super().__init__()
        claim = copy.deepcopy(WRITER_OUT["claims"][1])
        self.writer_out = {
            "short_answer": STALE_HEADLINE,
            "claims": [claim],
            "so_what": [{"text": "Interest is cooling.", "claim_ids": [claim["id"]]}],
            "watch_next": [{"text": "Watch whether fading continues.", "claim_ids": [claim["id"]],
                            "forecast": False}],
            "gaps": [],
            "context": STALE_CONTEXT,
        }
        self.critic_pending = critic_pending
        self.stop_flag = stop_flag
        self.fail_rewrite = fail_rewrite

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is SUPPORT_SCHEMA:
            count = sum(call["schema"] is SUPPORT_SCHEMA for call in self.calls)
            self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
            verdict = ("partial", "supported", "partial")[min(count, 2)]
            result = {"verdict": verdict, "reason": "The cited post does not support the claim as written.",
                      "demographic_inference": False, "tone_claim": False, "forecast_assertion": False,
                      "country_people": []}
            if self.stop_flag is not None and count == 0:
                self.stop_flag["stop"] = True
            return result, {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
        if schema is K4_REWRITE_SCHEMA:
            self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
            if self.fail_rewrite is not None:
                error = RuntimeError("rewrite provider call failed")
                if self.fail_rewrite == "known_zero":
                    error.usage = {"input_tokens": 0, "output_tokens": 0, "usd": 0.0}
                raise error
            return {"text": NARROWED_CLAIM}, {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
        if schema is critic.CRITIC_SCHEMA:
            count = sum(call["schema"] is critic.CRITIC_SCHEMA for call in self.calls)
            self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
            verdict = "needs_evidence" if self.critic_pending and count == 0 else "keep"
            return ({"verdicts": [{"claim_id": "c2", "verdict": verdict, "label": "",
                                   "reason": "The claim needs another source.", "quote": "",
                                   "query": "What else do TikTok posts say about amapiano?" if verdict == "needs_evidence"
                                   else ""}],
                     "missing_perspectives": [], "followups": [], "overall_risk": "medium"},
                    {"input_tokens": 3000, "output_tokens": 400, "usd": 0.015})
        return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)


def fake_check(draft, ctx, warehouse, *, window, markets):
    """Two code rules, enough to exercise cut and downgrade: K1 quotes verbatim, K5 one platform is not corroborated.
    Source gaps come from the checks, which own them. A claim citing a post that is not evidence (one outside the
    window is never stored) is cut, as K1 cuts it."""
    assert isinstance(window, tuple) and all(isinstance(d, date) for d in window)
    assert isinstance(markets, list)
    CHECK_CALLS.append({"window": window, "markets": markets})
    answer = copy.deepcopy(draft)
    verdicts, kept = [], []
    for claim in answer["claims"]:
        bad = [q for q in claim.get("quotes") or []
               if q["text"] not in ctx.evidence.get(q["evidence_id"], {}).get("text", "")]
        if bad or any(e not in ctx.evidence for e in claim["evidence_ids"]):
            verdicts.append({"claim_id": claim["id"], "rule": "K1", "verdict": "cut",
                             "reason": "quote not found verbatim in the cited post", "checker": "code"})
            continue
        verdicts.append({"claim_id": claim["id"], "rule": "K1", "verdict": "pass", "reason": "", "checker": "code"})
        platforms = {ctx.evidence[e]["platform"] for e in claim["evidence_ids"]}
        if claim["label"] == "corroborated" and len(platforms) < 2:
            claim["label"] = "single_source"
            verdicts.append({"claim_id": claim["id"], "rule": "K5", "verdict": "downgrade",
                             "reason": "one platform only; lowered to single_source", "checker": "code"})
        else:
            verdicts.append({"claim_id": claim["id"], "rule": "K5", "verdict": "pass", "reason": "", "checker": "code"})
        kept.append(claim)
    answer["claims"] = kept
    answer["gaps"] = answer["gaps"] + checks.source_gaps(ctx, window)
    return answer, verdicts


def make_research(stop_flag=None, live=True, seen=None):
    def research(ctx, prompt, options, emit, should_stop):
        if seen is not None:
            seen.update(market=ctx.market, prompt=prompt, options=options, tier=ctx.tier)
        emit.step("search", "Searching stored posts for 'amapiano'")
        search_posts(ctx, options.warehouse, "amapiano")
        if stop_flag is not None:
            stop_flag["stop"] = True
            return {"note": "", "tokens": {"input": 400, "output": 50}, "usd": 0.004}
        sql_query(ctx, options.warehouse, COUNT_SQL, purpose="posts, authors and platforms for amapiano",
                  params={"term": "amapiano"})
        if live:
            emit.step("search", "Searching Threads live", platform="threads")
            socialcrawl_call(ctx, options.client, "threads", "search", {"query": "amapiano"}, max_credits=5)
        return {"note": "Reading: amapiano, South Africa, this week.", "tokens": {"input": 1000, "output": 300},
                "usd": 0.02}

    return research


class Harness:
    def __init__(self, research=None, spent=None, check=fake_check, stop_flag=None, model=None, now=None):
        self.warehouse = FakeWarehouse()
        self.tables = FakeTables()
        self.model = model or FakeModel()
        self.clients = []
        self.events = []
        self.stop_flag = stop_flag if stop_flag is not None else {"stop": False}

        def socialcrawl(mode, run_id):
            client = FakeClient(mode, run_id)
            self.clients.append(client)
            return client

        self.deps = ask.Deps(warehouse=self.warehouse, socialcrawl=socialcrawl, tables=self.tables, model=self.model,
                             research=research or make_research(), now=lambda: NOW if now is None else now, check=check,
                             spent_today_usd=spent)

    def run(self, **request):
        body = {"question": "What is behind amapiano in South Africa this week?", "tier": "T1", "mode": "live",
                "ask_id": "a_20260928_test", **request}
        return ask.run_ask(body, self.events.append, lambda: self.stop_flag["stop"], deps=self.deps)


def kinds(events):
    return [e["event"] for e in events]


def test_run_ask_repairs_multiple_unpinned_claim_numerals_and_books_the_call():
    draft = copy.deepcopy(WRITER_OUT)
    draft["claims"][1]["text"] += " 20% faster."
    draft["claims"][2]["text"] += " 40% faster."
    model = NumericRepairModel([draft, copy.deepcopy(WRITER_OUT)])
    h = Harness(check=None, model=model)

    out = h.run()

    rows = [row for _, inserted in h.tables.inserts for row in inserted]
    assert {(row["claim_id"], row["verdict"]) for row in rows
            if row["rule"] == "K2" and row["claim_id"] in {"c2", "c3"}} == {("c2", "pass"), ("c3", "pass")}
    assert len([call for call in model.calls if call["schema"] is WRITER_SCHEMA]) == 2
    assert out["run"]["tokens"] == {
        "input": 1000 + sum(usage["input_tokens"] for usage in model.usages),
        "output": 300 + sum(usage["output_tokens"] for usage in model.usages),
    }
    assert out["run"]["model_usd"] == pytest.approx(
        0.02 + SEARCH_EMBED_USD + sum(usage["usd"] for usage in model.usages))
    assert all("20%" not in event.get("text", "") and "40%" not in event.get("text", "")
               for event in h.events if event["event"] == "step")


def test_run_ask_keeps_k2_cut_when_numeric_repair_retains_the_unpinned_percentage():
    draft = copy.deepcopy(WRITER_OUT)
    draft["claims"][2]["text"] += " 40% faster."
    model = NumericRepairModel([draft, draft])
    h = Harness(check=None, model=model)

    h.run()

    rows = [row for _, inserted in h.tables.inserts for row in inserted]
    assert any(row["claim_id"] == "c3" and row["rule"] == "K2" and row["verdict"] == "cut" for row in rows)
    assert len([call for call in model.calls if call["schema"] is WRITER_SCHEMA]) == 2


def test_full_t1_run_gives_a_valid_answer_and_run_object():
    h = Harness()
    out = h.run()
    answer, run = out["answer"], out["run"]

    assert set(out) == {"answer", "run", "query_receipts"}
    assert validate_answer(answer) == []
    assert answer["status"] == "partial"  # K10: the support check cut c2
    assert [c["id"] for c in answer["claims"]] == ["c1", "c3"]
    assert {c["id"]: c["label"] for c in answer["claims"]} == {"c1": "corroborated", "c3": "single_source"}
    cited = {e for c in answer["claims"] for e in c["evidence_ids"]}
    assert len(cited) >= 5
    assert {r["platform"] for r in answer["evidence"]} == {"tiktok", "x"}
    assert answer["claims"][0]["numbers"][0]["run_id"] == run["run_id"]
    assert [s["claim_ids"] for s in answer["so_what"]] == [["c1"]]
    assert {"what": "No live Threads data", "searched": "threads/search, 22 to 28 September",
            "why": "rate_limited"} in answer["gaps"]

    assert set(run) == RUN_KEYS
    assert re.fullmatch(r"r_20260928_081500_[0-9a-f]{8}_[0-9a-f]{32}", run["run_id"])
    assert run["tier"] == "T1" and run["mode"] == "live"
    assert run["credits"] == 1.0
    # three support calls, made at the same time, then one K4 rewrite and one field check
    assert run["tokens"] == {"input": 1000 + 2000 + (3 + 1) * 100 + 100,
                             "output": 300 + 500 + (3 + 1) * 20 + 20}
    assert run["model_usd"] == pytest.approx(0.02 + 0.0135 + (3 + 1) * 0.0006 + 0.0006 + SEARCH_EMBED_USD)
    assert [call["schema"] for call in h.model.calls] == [WRITER_SCHEMA, SUPPORT_SCHEMA, SUPPORT_SCHEMA,
                                                          SUPPORT_SCHEMA, K4_REWRITE_SCHEMA, FIELDS_SCHEMA]
    field_call = h.model.calls[-1]
    assert field_call["max_tokens"] == ask.field_max_tokens(2) <= ask.FIELD_MAX_TOKENS
    assert run["window"] == {"from": "2026-09-22", "to": "2026-09-28"}
    assert run["posts"] == 6 and run["platforms"] == 2
    assert run["source_status"] == [{"platform": "threads", "route": "threads/search", "status": "rate_limited",
                                     "items": 0}]
    assert 1 <= len(run["followups"]) <= 3 and all(f.endswith("?") for f in run["followups"])
    assert run["notices"] == []
    assert isinstance(run["seconds"], (int, float)) and run["seconds"] >= 0
    assert answer["as_of"] == "2026-09-28T08:15:00+02:00"


def test_the_run_keeps_a_receipt_for_the_query_behind_every_number():
    h = Harness()
    out = h.run()
    receipts = out["query_receipts"]
    numbers = [n for c in out["answer"]["claims"] for n in c.get("numbers") or []]
    assert numbers and {n["query_id"] for n in numbers} <= set(receipts)
    for number in numbers:
        receipt = receipts[number["query_id"]]
        assert set(receipt) == {"purpose", "sql", "params", "result_hash", "row_count", "rows"}
        assert receipt["result_hash"] == number["result_hash"] == result_hash(receipt["rows"])
        assert receipt["row_count"] == len(receipt["rows"])
    count = next(r for r in receipts.values() if r["purpose"] == "posts, authors and platforms for amapiano")
    assert count["sql"] == COUNT_SQL and count["params"] == {"term": "amapiano"}
    assert count["rows"] == [{"posts": 6, "authors": 5, "platforms": 2}]
    assert "query_receipts" not in out["run"]


def test_query_receipts_cap_the_rows_and_are_plain_json():
    ctx = RunContext(run_id="r_x", tier="T1", as_of=NOW)
    rows = [{"day": date(2026, 9, 1), "at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc), "n": i} for i in range(120)]
    query_id, digest = ctx.record_query("SELECT day, at, n FROM t", {"day": date(2026, 9, 1)}, rows, "daily counts")
    receipt = ask.query_receipts(ctx)[query_id]
    assert receipt["row_count"] == 120 and len(receipt["rows"]) == ask.RECEIPT_ROWS == 50
    assert receipt["result_hash"] == digest  # the hash of every row, not only the kept ones
    assert receipt["params"] == {"day": "2026-09-01"} and receipt["rows"][0]["day"] == "2026-09-01"
    assert json.loads(json.dumps(receipt)) == receipt


def test_real_run_rewrites_a_partial_k4_claim_and_emits_the_narrowed_claim():
    h = Harness(research=make_research(live=False), check=None, model=K4IntegrationModel())
    out = h.run()

    assert validate_answer(out["answer"]) == []
    assert out["answer"]["claims"][0]["text"] == NARROWED_CLAIM
    assert out["answer"]["short_answer"] == checks.INSUFFICIENT
    assert out["answer"]["context"] == ""
    assert out["answer"]["so_what"] == []
    assert out["answer"]["watch_next"] == []
    assert out["answer"]["claims"][0]["label"] == "inferred"
    assert STALE_HEADLINE not in json.dumps([h.events, out["answer"]], default=str)
    assert STALE_CONTEXT not in json.dumps([h.events, out["answer"]], default=str)
    (event,) = [event for event in h.events if event["event"] == "claim"]
    assert set(event) == {"event", "claim", "check", "reason"}
    assert event["check"] == "downgraded"
    assert event["reason"] == "label lowered to inferred (K5)"
    assert event["claim"]["text"] == NARROWED_CLAIM
    assert event["claim"]["evidence_ids"] == ["tt_4"]
    assert event["claim"]["quotes"] == [] and event["claim"]["numbers"] == []
    assert event["claim"]["label"] == "inferred"
    assert ORIGINAL_CLAIM not in json.dumps([h.events, out["answer"]], default=str)
    assert sum(call["schema"] is K4_REWRITE_SCHEMA for call in h.model.calls) == 1
    assert sum(call["schema"] is SUPPORT_SCHEMA for call in h.model.calls) == 2
    assert FIELDS_SCHEMA not in [call["schema"] for call in h.model.calls]
    assert out["run"]["tokens"] == {"input": 1000 + 2000 + 3 * 100, "output": 300 + 500 + 3 * 20}
    assert out["run"]["model_usd"] == pytest.approx(0.02 + 0.0135 + 3 * 0.0006 + SEARCH_EMBED_USD)


@pytest.mark.parametrize("fail_rewrite", ["unknown", "known_zero"])
def test_real_run_accounts_for_a_failed_k4_rewrite(fail_rewrite):
    h = Harness(research=make_research(live=False), check=None,
                model=K4IntegrationModel(fail_rewrite=fail_rewrite))
    with pytest.raises(RuntimeError, match="rewrite provider call failed") as caught:
        h.run()

    run = caught.value.run
    rewrite = {"input_tokens": 0, "output_tokens": 0, "usd": 0.0}
    if fail_rewrite == "unknown":
        rewrite = {"input_tokens": K4_REWRITE_INPUT_TOKENS,
                   "output_tokens": reserve_output(ask.MODEL, K4_REWRITE_MAX_TOKENS),
                   "usd": ask.call_usd(ask.MODEL, K4_REWRITE_INPUT_TOKENS, K4_REWRITE_MAX_TOKENS)}
    assert run["tokens"] == {"input": 1000 + 2000 + 100 + rewrite["input_tokens"],
                             "output": 300 + 500 + 20 + rewrite["output_tokens"]}
    assert run["model_usd"] == pytest.approx(0.02 + 0.0135 + 0.0006 + rewrite["usd"] + SEARCH_EMBED_USD)
    assert [call["schema"] for call in h.model.calls] == [WRITER_SCHEMA, SUPPORT_SCHEMA, K4_REWRITE_SCHEMA]
    assert h.tables.inserts == []
    assert not any(event["event"] == "claim" for event in h.events)


def test_real_run_cancellation_before_k4_rewrite_dispatch_does_not_reserve_its_cost():
    stop_flag = {"stop": False}
    h = Harness(research=make_research(live=False), check=None, stop_flag=stop_flag,
                model=K4IntegrationModel(stop_flag=stop_flag))
    out = h.run()

    assert validate_answer(out["answer"]) == []
    assert out["answer"]["status"] == "insufficient_evidence"
    assert [call["schema"] for call in h.model.calls] == [WRITER_SCHEMA, SUPPORT_SCHEMA]
    assert out["run"]["tokens"] == {"input": 1000 + 2000 + 100, "output": 300 + 500 + 20}
    assert out["run"]["model_usd"] == pytest.approx(0.02 + 0.0135 + 0.0006 + SEARCH_EMBED_USD)
    assert h.tables.inserts == []
    assert not any(event["event"] == "claim" for event in h.events)


def test_real_t3_gap_pass_does_not_retry_a_claims_k4_rewrite():
    plan = {"sub_questions": [{"id": "q1", "text": "What do TikTok posts say about amapiano this week?",
                                "platforms": ["tiktok"], "credits": 20}],
            "gap_round": True, "max_credits": 100, "max_model_usd": 10.0}
    research_calls = []
    base_research = make_research(live=False)

    def research(*args, **kwargs):
        research_calls.append(True)
        return base_research(*args, **kwargs)

    model = K4IntegrationModel(critic_pending=True)
    h = Harness(research=research, check=None, model=model)
    out = h.run(tier="T3", plan=plan, max_credits=100, max_model_usd=10.0,
                investigation_id="i_20260928_test")

    assert len(research_calls) == 2
    assert sum(call["schema"] is WRITER_SCHEMA for call in model.calls) == 2
    assert sum(call["schema"] is K4_REWRITE_SCHEMA for call in model.calls) == 1
    assert sum(call["schema"] is SUPPORT_SCHEMA for call in model.calls) == 3
    assert validate_answer(out["answer"]) == []
    (event,) = [event for event in h.events if event["event"] == "claim"]
    assert event["check"] == "cut"
    assert ORIGINAL_CLAIM not in json.dumps([h.events, out["answer"]], default=str)
    assert STALE_HEADLINE not in json.dumps([h.events, out["answer"]], default=str)
    assert STALE_CONTEXT not in json.dumps([h.events, out["answer"]], default=str)
    assert out["answer"]["so_what"] == [] and out["answer"]["watch_next"] == []
    assert out["run"]["tier"] == "T3"
    assert out["run"]["notice"]["status"] == "insufficient_evidence"
    assert h.tables.inserts


def test_ask_records_the_exact_creator_metadata_note_sent_to_the_writer(monkeypatch):
    size_note = "Only macro and mega page tiers pass; size is not otherwise established."
    comment_note = "Stored posts have counts, not comment text or commenter behavior."
    discovered = {"creator_size_status": "unknown", "creator_size_note": size_note,
                  "comment_text_status": "unavailable", "comment_text_note": comment_note,
                  "discovery_gaps": []}
    monkeypatch.setattr(ask, "is_creator_question", lambda question: True)
    monkeypatch.setattr(ask, "discover_creators", lambda ctx, warehouse, question: discovered)
    contexts = []
    base = make_research(live=False)

    def research(ctx, prompt, options, emit, should_stop):
        contexts.append(ctx)
        return base(ctx, prompt, options, emit, should_stop)

    model = FakeModel()
    h = Harness(research=research, model=model)
    h.run(question="Which creators in South Africa post about amapiano?")

    expected_note = "\n\n".join(("Reading: amapiano, South Africa, this week.", ask.CREATOR_DISCOVERY_NOTE,
                                  f"Creator size is unknown. {size_note}",
                                  f"Comment text is unavailable. {comment_note}"))
    assert contexts[0].writer_note == expected_note
    writer_input = next(call["user"] for call in model.calls if call["schema"] is WRITER_SCHEMA)
    assert expected_note in writer_input


def test_same_second_asks_keep_distinct_run_spend_and_shared_snapshots_deduplicate():
    from core.api.investigations import spent_in_rows

    h = Harness()
    context_run_ids = []
    research = h.deps.research

    def capture_context(ctx, *args):
        context_run_ids.append(ctx.run_id)
        return research(ctx, *args)

    h.deps.research = capture_context
    first = h.run(ask_id="ask-a")
    second = h.run(ask_id="ask-b")
    first_run, second_run = first["run"], second["run"]

    rows = [{"run_id": run["run_id"], "run_date": NOW.date(), "stage": "ask", "credits": run["credits"],
             "model_usd": run["model_usd"]} for run in (first_run, second_run)]
    rows.append({**rows[0], "model_usd": rows[0]["model_usd"] / 2})
    spent = spent_in_rows(rows, NOW.date())

    assert spent["model_usd"] == round(first_run["model_usd"] + second_run["model_usd"], 4)
    assert first_run["run_id"] != second_run["run_id"]
    assert context_run_ids == [first_run["run_id"], second_run["run_id"]]
    assert [client.run_id for client in h.clients] == [first_run["run_id"], second_run["run_id"]]
    for result in (first, second):
        assert all(number["run_id"] == result["run"]["run_id"]
                   for claim in result["answer"]["claims"] for number in claim.get("numbers", []))


def test_t2_loads_native_policy_once_and_refreshes_only_new_evidence_languages():
    from core.agent import critic
    from core.agent.tests import test_ask_t2

    class GapEvidenceLanes(test_ask_t2.Lanes):
        def __call__(self, ctx, prompt, options, emit, should_stop):
            gap_round = "credit_cap" not in ctx.budget
            result = super().__call__(ctx, prompt, options, emit, should_stop)
            if gap_round:
                ctx.evidence["gap_new"] = {
                    "id": "gap_new", "market": "ZA", "geo_market": "ZA", "platform": "x",
                    "handle": "@gap_new", "text": "A new post from the gap round", "flags": [],
                    "published_at": datetime(2026, 9, 27, 19, 40, tzinfo=SAST),
                    "post_date": date(2026, 9, 27),
                }
            return result

    model = test_ask_t2.CriticModel({"c3": {"verdict": "needs_evidence"}}, {})
    lanes = GapEvidenceLanes(parties=4)
    h = test_ask_t2.t2(lanes, model)
    original_run = h.warehouse.run

    def run(sql, params, max_bytes_billed):
        if "intelligence_42_agent.feedback" in sql:
            h.warehouse.control_runs.append((sql, params, max_bytes_billed))
            return [{"payload": "[]"}]
        if "intelligence_42_core.post_enrichment" in sql:
            h.warehouse.control_runs.append((sql, params, max_bytes_billed))
            rows = [{"post_id": post_id, "langs": ["en"]} for post_id in params.values()]
            return [{"payload": json.dumps(rows)}]
        return original_run(sql, params, max_bytes_billed)

    h.warehouse.run = run
    out = h.run(tier="T2")

    feedback_reads = [entry for entry in h.warehouse.control_runs if "feedback" in entry[0]]
    language_reads = [entry for entry in h.warehouse.control_runs if "post_enrichment" in entry[0]]
    assert len(feedback_reads) == 1
    assert len(language_reads) == 2
    assert set(language_reads[0][1].values()) == {row["post_id"] for row in POSTS}
    assert list(language_reads[1][1].values()) == ["gap_new"]
    shared_queries = lanes.calls[0]["ctx"].queries
    assert not any("feedback" in query["sql"] or "post_enrichment" in query["sql"]
                   for query in shared_queries.values())
    assert len(model.by(WRITER_SCHEMA)) == 2
    assert len(model.by(SUPPORT_SCHEMA)) == 6
    assert len(model.by(FIELDS_SCHEMA)) == 2
    assert len(model.by(critic.CRITIC_SCHEMA)) == 2
    assert set(out) == {"answer", "run", "query_receipts"} and set(out["run"]) == RUN_KEYS
    assert validate_answer(out["answer"]) == []


def test_events_come_in_order_with_evidence_for_every_record_and_one_claim_event_each():
    h = Harness()
    h.run()
    order = kinds(h.events)
    assert set(order) == {"step", "evidence", "claim"}
    first_claim = order.index("claim")
    assert "claim" not in order[:first_claim] and all(k == "claim" for k in order[first_claim:])
    assert order[0] == "step"

    evidence_ids = [e["evidence"]["id"] for e in h.events if e["event"] == "evidence"]
    assert sorted(evidence_ids) == sorted(p["post_id"] for p in POSTS)
    for step in (e for e in h.events if e["event"] == "step"):
        assert step["kind"] in ("plan", "search", "found", "transcribe", "read", "check", "write", "note")
        assert step["text"] and step["at"] == "2026-09-28T08:15:00+02:00"
        assert "seq" not in step

    claims = {e["claim"]["id"]: e for e in h.events if e["event"] == "claim"}
    assert list(claims) == ["c1", "c2", "c3", "c4"]
    assert claims["c1"]["check"] == "verified" and claims["c1"]["reason"] is None
    assert claims["c2"]["check"] == "cut" and "unsupported" in claims["c2"]["reason"]
    assert claims["c3"]["check"] == "downgraded" and "single_source" in claims["c3"]["reason"]
    assert claims["c3"]["claim"]["label"] == "single_source"
    assert claims["c4"]["check"] == "cut" and "verbatim" in claims["c4"]["reason"]


def test_claim_check_verdicts_are_logged_with_run_and_ask_ids():
    def check_with_null_pass_reason(draft, ctx, warehouse, *, window, markets):
        answer, verdicts = fake_check(draft, ctx, warehouse, window=window, markets=markets)
        next(v for v in verdicts if v["claim_id"] == "c1" and v["rule"] == "K1")["reason"] = None
        return answer, verdicts

    h = Harness(check=check_with_null_pass_reason)
    out = h.run()
    assert [t for t, _ in h.tables.inserts] == ["intelligence_42_agent.claim_checks"]
    rows = h.tables.inserts[0][1]
    assert all(set(r) == {"answer_or_brief_id", "claim_id", "rule", "verdict", "checker", "reason", "run_id"}
               for r in rows)
    assert {r["answer_or_brief_id"] for r in rows} == {"a_20260928_test"}
    assert {r["run_id"] for r in rows} == {out["run"]["run_id"]}
    got = {(r["claim_id"], r["rule"], r["verdict"], r["checker"]) for r in rows}
    assert ("c3", "K5", "downgrade", "code") in got
    assert ("c4", "K1", "cut", "code") in got
    assert ("c2", "K4", "cut", "model") in got
    assert ("c1", "K4", "pass", "model") in got
    by_claim_rule = {(r["claim_id"], r["rule"]): r for r in rows}
    assert by_claim_rule[("c4", "K1")]["reason"] == "quote not found verbatim in the cited post"
    assert by_claim_rule[("c2", "K4")]["reason"] == "unsupported by the cited post"
    assert by_claim_rule[("c1", "K1")]["reason"] is None
    assert ("short_answer", "K10", "pass", "code") in got
    assert len(rows) == 3 * 2 + 1 + 3 + 1  # K1 and K5 for three claims, K1 cut for c4, K4 for three claims, K10


class FakeBQClient:
    def __init__(self):
        self.inserts = []

    def insert_rows_json(self, table, rows, row_ids=None):
        self.inserts.append({"table": table, "rows": copy.deepcopy(rows), "row_ids": row_ids})
        return []


def real_writer():
    writer = BigQueryTableWriter()
    writer._client = FakeBQClient()
    return writer


def test_claim_checks_go_through_the_real_writer_with_retry_safe_row_ids():
    # claim_checks rows have no finding_id; the real writer must take them, keyed so a retry lands once.
    h = Harness()
    h.deps.tables = writer = real_writer()
    h.run()
    (insert,) = writer._client.inserts
    assert insert["table"] == "ogilvy-trends-v2.intelligence_42_agent.claim_checks"
    assert all("finding_id" not in r for r in insert["rows"])
    assert insert["row_ids"] == [f"a_20260928_test:{r['claim_id']}:{r['rule']}:{i}"
                                 for i, r in enumerate(insert["rows"])]
    assert len(set(insert["row_ids"])) == len(insert["rows"])


class RepeatedIdModel(FakeModel):
    """The writer gives two claims the same id; the logged row ids must still be unique."""

    def complete_json(self, **kwargs):
        out, usage = super().complete_json(**kwargs)
        if kwargs["schema"] is WRITER_SCHEMA:
            out["claims"][2]["id"] = "c1"
        return out, usage


def test_claim_check_row_ids_stay_unique_when_a_claim_id_repeats():
    h = Harness(model=RepeatedIdModel())
    h.deps.tables = writer = real_writer()
    with pytest.raises(RuntimeError, match="duplicate claim id"):
        h.run()  # the contract refuses the answer, but the verdicts are logged first
    (insert,) = writer._client.inserts
    assert len([r for r in insert["rows"] if (r["claim_id"], r["rule"]) == ("c1", "K1")]) == 2
    assert len(set(insert["row_ids"])) == len(insert["rows"])


def test_claim_check_row_ids_use_the_run_id_when_no_ask_id_is_given():
    h = Harness()
    h.deps.tables = writer = real_writer()
    out = h.run(ask_id=None)
    (insert,) = writer._client.inserts
    run_id = out["run"]["run_id"]
    assert {r["answer_or_brief_id"] for r in insert["rows"]} == {run_id}
    assert insert["row_ids"] == [f"{run_id}:{r['claim_id']}:{r['rule']}:{i}" for i, r in enumerate(insert["rows"])]


def test_recheck_after_the_support_check_sets_the_final_answer_and_its_rows_are_logged(monkeypatch):
    seen, windows = [], []

    def recheck(answer, ctx, *, window=None, code_gaps=()):
        seen.append(copy.deepcopy(answer))
        windows.append(window)
        row = {"claim_id": "short_answer", "rule": "K8", "verdict": "cut",
               "reason": "unmatched quoted span 'x_z2'",
               "checker": "code", "run_id": ctx.run_id}
        return {**answer, "short_answer": ""}, [row]

    monkeypatch.setattr(checks, "recheck_fields", recheck, raising=False)
    h = Harness()
    out = h.run()
    assert [c["id"] for c in seen[0]["claims"]] == ["c1", "c3"]  # after the support check cut c2
    assert windows == [(date(2026, 9, 22), date(2026, 9, 28))]
    assert out["answer"]["short_answer"] == ""
    rows = h.tables.inserts[0][1]
    assert ("short_answer", "K8", "cut", "code") in {(r["claim_id"], r["rule"], r["verdict"], r["checker"])
                                                      for r in rows}
    field_row = next(r for r in rows if r["claim_id"] == "short_answer" and r["rule"] == "K8")
    assert field_row["reason"] == (
        "a quote not found in its posts (K8): every quoted span must be verbatim in a cited post, "
        f"checked stored evidence none cited, shown as {ask.UNRESOLVED}")
    assert "x_z2" not in field_row["reason"]
    assert rows[-1]["answer_or_brief_id"] == "a_20260928_test" and rows[-1]["run_id"] == out["run"]["run_id"]


BREACH = "Gen Z drivers are furious about fuel."
WITHHELD = "Claim withheld by the trust gate (K6)"


class BreachModel(FakeModel):
    """The writer adds a claim with an age term and a misquote, so K6 or K1 cuts it."""

    def complete_json(self, **kwargs):
        out, usage = super().complete_json(**kwargs)
        if kwargs["schema"] is WRITER_SCHEMA:
            out["claims"].append({"id": "c5", "text": BREACH, "label": "single_source", "kind": "observation",
                                  "evidence_ids": ["tt_1"], "numbers": [],
                                  "quotes": [{"evidence_id": "tt_1", "text": "fuel is too expensive"}]})
        return out, usage


@pytest.mark.parametrize("check", [None, fake_check], ids=["real_checks_K6", "stub_checks_K1_only"])
def test_a_cut_claim_with_a_breach_is_emitted_withheld_and_never_with_its_text(check):
    h = Harness(check=check, model=BreachModel())
    out = h.run()
    (event,) = [e for e in h.events if e["event"] == "claim" and e["claim"]["id"] == "c5"]
    assert event["check"] == "cut"
    assert event["claim"]["text"] == WITHHELD
    assert event["claim"]["quotes"] == []
    assert event["claim"]["evidence_ids"] == ["tt_1"]
    for payload in (h.events, h.tables.inserts, out["answer"]):
        assert "gen z" not in json.dumps(payload, default=str).lower()


def test_a_k6_cut_withholds_the_claim_text_even_without_a_text_breach():
    def k6_check(draft, ctx, warehouse, *, window, markets):
        answer, verdicts = fake_check(draft, ctx, warehouse, window=window, markets=markets)
        answer["claims"] = [c for c in answer["claims"] if c["id"] != "c3"]
        verdicts.append({"claim_id": "c3", "rule": "K6", "verdict": "cut", "checker": "code",
                         "reason": "breach: generated output tt_2 cited as evidence"})
        return answer, verdicts

    h = Harness(check=k6_check)
    h.run()
    (event,) = [e for e in h.events if e["event"] == "claim" and e["claim"]["id"] == "c3"]
    assert event["check"] == "cut" and event["claim"]["text"] == WITHHELD and event["claim"]["quotes"] == []
    assert event["claim"]["evidence_ids"] == WRITER_OUT["claims"][2]["evidence_ids"]


def test_the_withheld_text_is_itself_clean():
    assert ask.WITHHELD == WITHHELD
    assert checks._text_breaches(ask.WITHHELD) == []


class ExtraClaimModel(FakeModel):
    """The writer adds one claim, c5, that the real checks cut."""

    def __init__(self, claim):
        super().__init__()
        self.claim = claim

    def complete_json(self, **kwargs):
        out, usage = super().complete_json(**kwargs)
        if kwargs["schema"] is WRITER_SCHEMA:
            out["claims"].append({"id": "c5", "label": "single_source", "kind": "observation", "quotes": [],
                                  "numbers": [], **self.claim})
        return out, usage


@pytest.mark.parametrize("rule, claim, detail", [
    ("K2", {"text": "Amapiano posts rose 340% this week.", "evidence_ids": ["tt_1"]}, "340"),
    ("K8", {"text": 'A fan wrote "the braai never ends before sunrise" about amapiano.', "evidence_ids": ["x_1"]},
     "the braai never ends before sunrise"),
])
def test_a_cut_claim_event_gives_the_rule_in_general_words_and_never_the_cut_detail(rule, claim, detail):
    verdicts = []

    def real_check(*args, **kwargs):
        answer, rows = checks.check_answer(*args, **kwargs)
        verdicts.extend(rows)
        return answer, rows

    h = Harness(check=real_check, model=ExtraClaimModel(claim))
    h.run()
    (event,) = [e for e in h.events if e["event"] == "claim" and e["claim"]["id"] == "c5"]
    what = checks.RULE_GAPS[rule][0]
    assert event["check"] == "cut" and f"{what} ({rule})" in event["reason"]
    assert detail not in event["reason"] and detail not in event["claim"]["text"]
    assert detail not in json.dumps(event)
    assert checks._text_breaches(event["claim"]["text"]) == []
    (row,) = [v for v in verdicts if v["claim_id"] == "c5" and v["rule"] == rule]
    assert row["verdict"] == "cut" and detail in row["reason"]
    assert ("c5", rule, "cut") in {(r["claim_id"], r["rule"], r["verdict"]) for r in h.tables.inserts[0][1]}


def test_a_support_cut_event_never_repeats_the_model_reason():
    raw_reason = "unsupported: the post only jokes that amapiano died in 2019"

    class ChattyModel(FakeModel):
        def complete_json(self, **kwargs):
            out, usage = super().complete_json(**kwargs)
            if kwargs["schema"] is SUPPORT_SCHEMA and out["verdict"] == "unsupported":
                out["reason"] = raw_reason
            return out, usage

    h = Harness(model=ChattyModel())
    out = h.run()
    (event,) = [e for e in h.events if e["event"] == "claim" and e["claim"]["id"] == "c2"]
    assert event["check"] == "cut" and "(K4)" in event["reason"]
    assert raw_reason not in json.dumps(out["answer"])
    assert raw_reason not in json.dumps(h.events)
    rows = h.tables.inserts[0][1]
    assert ("c2", "K4", "cut") in {(r["claim_id"], r["rule"], r["verdict"]) for r in rows}
    (support_row,) = [r for r in rows if r["claim_id"] == "c2" and r["rule"] == "K4"]
    assert support_row["reason"] == raw_reason


def progress():
    return ask.Progress(lambda e: None, lambda: NOW, market_label="South Africa",
                        window=(date(2026, 9, 22), date(2026, 9, 28)))


@pytest.mark.parametrize("name, tool_input, generic", [
    ("mcp__f42__search_posts", {"query": "gen z slang"}, "Searching stored posts"),
    ("mcp__f42__search_posts", {"query": "amapiano", "platforms": ["teenagers"]}, "Searching stored posts"),
    ("mcp__f42__recall_findings", {"query": "gen z slang"}, "Checking saved findings"),
    ("mcp__f42__resolve_dates", {"expression": "since gen z started posting"}, "Working out the dates"),
    ("mcp__f42__sql_query", {"sql": "SELECT 1", "purpose": "search volume for gen z slang"}, "Counting in the warehouse"),
    ("mcp__f42__socialcrawl_call", {"platform": "tiktok", "endpoint": "gen z slang"}, "Searching a live source"),
])
def test_step_text_never_repeats_a_breaching_tool_input(name, tool_input, generic):
    kind, text, _ = ask.describe_tool(name, tool_input, progress())
    assert text == generic
    assert checks._text_breaches(text) == []


def test_a_clean_tool_input_still_shows_in_the_step():
    _, text, _ = ask.describe_tool("mcp__f42__search_posts", {"query": "amapiano slang"}, progress())
    assert "amapiano slang" in text


def test_any_step_text_with_a_breach_is_replaced_before_it_is_emitted():
    events = []
    p = ask.Progress(events.append, lambda: NOW, market_label="South Africa",
                     window=(date(2026, 9, 22), date(2026, 9, 28)))
    p.step("note", "search_posts did not run: refused query 'gen z slang'")
    assert events[0]["kind"] == "note" and checks._text_breaches(events[0]["text"]) == []
    assert "gen z" not in events[0]["text"].lower()


@pytest.mark.parametrize("tier", ["T3", "t1", "X"])  # T2 runs since task 1.17 (test_ask_t2.py)
def test_tiers_beyond_t2_raise(tier):
    with pytest.raises(ValueError):
        Harness().run(tier=tier)


@pytest.mark.parametrize("request_patch", [{"question": "hi"}, {"question": "x" * 2001}, {"mode": "cached"},
                                           {"market": "GH"}])
def test_bad_requests_raise(request_patch):
    with pytest.raises(ValueError):
        Harness().run(**request_patch)


def test_missing_tier_defaults_to_t1():
    body = {"question": "What is behind amapiano in South Africa this week?"}
    h = Harness()
    out = ask.run_ask(body, h.events.append, lambda: False, deps=h.deps)
    assert out["run"]["tier"] == "T1" and out["run"]["mode"] == "live"


@pytest.mark.parametrize("question, markets", [
    ("What is amapiano doing in South Africa?", ["ZA"]),
    ("Is this big in SA right now?", ["ZA"]),
    ("What is Mzansi saying about load shedding?", ["ZA"]),
    ("Joburg taxi memes", ["ZA"]),
    ("What is trending in Johannesburg?", ["ZA"]),
    ("Cape Town beach posts", ["ZA"]),
    ("Durban curry debates", ["ZA"]),
    ("What are Nigerians saying about fuel prices?", ["NG"]),
    ("Naija street food", ["NG"]),
    ("Lagos traffic jokes", ["NG"]),
    ("Abuja weddings", ["NG"]),
    ("What is Kenya talking about?", ["KE"]),
    ("Nairobi matatu art", ["KE"]),
    ("Mombasa food posts", ["KE"]),
    ("Compare Lagos and Nairobi street food", ["NG", "KE"]),
    ("Is Kenyan drill bigger than SA amapiano in Lagos?", ["KE", "ZA", "NG"]),
    ("What is new in amapiano?", []),
    ("Is salsa dancing growing?", []),
])
def test_market_detection(question, markets):
    assert ask.detect_markets(question) == markets


def test_detected_market_reaches_the_research_context_and_the_checks():
    seen = {}
    CHECK_CALLS.clear()
    h = Harness(research=make_research(seen=seen))
    out = h.run(question="What is behind amapiano in Lagos this week?", market=None)
    assert seen["market"] == "NG"
    assert CHECK_CALLS[-1]["markets"] == ["NG"]
    assert validate_answer(out["answer"]) == []


def test_two_country_question_passes_both_markets_and_leaves_ctx_market_unset():
    seen = {}
    CHECK_CALLS.clear()
    h = Harness(research=make_research(seen=seen))
    h.run(question="Is amapiano bigger in South Africa or Nigeria this week?", market=None)
    assert CHECK_CALLS[-1]["markets"] == ["ZA", "NG"]
    assert seen["market"] is None
    assert "Market: South Africa and Nigeria." in seen["options"].system_prompt


def test_no_market_named_passes_an_empty_list():
    seen = {}
    CHECK_CALLS.clear()
    Harness(research=make_research(seen=seen)).run(question="What is behind amapiano this week?", market=None)
    assert CHECK_CALLS[-1]["markets"] == [] and seen["market"] is None


def test_run_ask_puts_the_window_on_the_context():
    seen = {}

    def research(ctx, prompt, options, emit, should_stop):
        seen.update(window=(ctx.window_start, ctx.window_end))
        return make_research()(ctx, prompt, options, emit, should_stop)

    out = Harness(research=research).run(question="amapiano in South Africa over the last 10 days")
    assert seen["window"] == (date(2026, 9, 19), date(2026, 9, 28))
    assert out["run"]["window"] == {"from": "2026-09-19", "to": "2026-09-28"}


def test_run_counts_x_and_twitter_as_one_platform():
    def research(ctx, prompt, options, emit, should_stop):
        out = make_research(live=False)(ctx, prompt, options, emit, should_stop)
        ctx.evidence["tw_1"] = dict(ctx.evidence["x_1"], id="tw_1", platform="twitter")
        return out

    out = Harness(research=research).run()
    assert out["run"]["platforms"] == 2  # tiktok and x


def test_explicit_market_wins_over_the_question():
    seen = {}
    CHECK_CALLS.clear()
    Harness(research=make_research(seen=seen)).run(question="amapiano in Lagos", market="KE")
    assert seen["market"] == "KE"
    assert CHECK_CALLS[-1]["markets"] == ["KE"]


@pytest.mark.parametrize("question, window", [
    ("amapiano in South Africa over the last 10 days", {"from": "2026-09-19", "to": "2026-09-28"}),
    ("amapiano in South Africa", {"from": "2026-08-30", "to": "2026-09-28"}),  # no window: the 30-day default
    ("amapiano in South Africa yesterday", {"from": "2026-09-27", "to": "2026-09-27"}),
    ("amapiano in South Africa today", {"from": "2026-09-28", "to": "2026-09-28"}),
    ("amapiano in South Africa last month", {"from": "2026-08-30", "to": "2026-09-28"}),
])
def test_window_comes_from_the_question(question, window):
    assert Harness().run(question=question)["run"]["window"] == window


@pytest.mark.parametrize("question, start, end", [
    ("What moved over the last ten days?", date(2026, 9, 19), date(2026, 9, 28)),
    ("What moved in the last two weeks?", date(2026, 9, 15), date(2026, 9, 28)),
    ("What moved in the past three weeks?", date(2026, 9, 8), date(2026, 9, 28)),
    ("What moved over the last four weeks?", date(2026, 9, 1), date(2026, 9, 28)),
    ("What moved over the last two months?", date(2026, 7, 31), date(2026, 9, 28)),
    ("What moved in the last couple of months?", date(2026, 7, 31), date(2026, 9, 28)),
    ("What moved in the last few days?", date(2026, 9, 26), date(2026, 9, 28)),
    ("What moved over the last three months?", date(2026, 7, 1), date(2026, 9, 28)),
    ("What moved last month?", date(2026, 8, 30), date(2026, 9, 28)),
    ("What moved in the last month?", date(2026, 8, 30), date(2026, 9, 28)),
    ("What moved this week?", date(2026, 9, 22), date(2026, 9, 28)),
    ("What moved last week?", date(2026, 9, 22), date(2026, 9, 28)),
    ("What moved this month?", date(2026, 8, 30), date(2026, 9, 28)),  # months are 30 days, like weeks are 7
    ("What moved in the last 60 days?", date(2026, 7, 31), date(2026, 9, 28)),
    ("What moved in the last 7 days?", date(2026, 9, 22), date(2026, 9, 28)),  # a stated window beats the default
    ("What moved in the LAST 60 DAYS?", date(2026, 7, 31), date(2026, 9, 28)),
    ("What moved in the 10 days before?", date(2026, 9, 19), date(2026, 9, 28)),
    ("What moved since the start of September?", date(2026, 9, 1), date(2026, 9, 28)),
    ("What moved since the beginning of August?", date(2026, 8, 1), date(2026, 9, 28)),
    ("The campaign has run since mid-August. What moved?", date(2026, 8, 15), date(2026, 9, 28)),
    ("What moved since 3 September?", date(2026, 9, 3), date(2026, 9, 28)),
    ("What moved since the start of December?", date(2025, 12, 1), date(2026, 9, 28)),  # not yet this year
    ("What moved yesterday?", date(2026, 9, 27), date(2026, 9, 27)),
    ("What moved today?", date(2026, 9, 28), date(2026, 9, 28)),
    # A comparison clause widens the window to cover both periods it compares.
    ("What is rising right now compared with last month?", date(2026, 7, 31), date(2026, 9, 28)),
    ("What rose in the last 30 days compared with the 30 days before?", date(2026, 7, 31), date(2026, 9, 28)),
    ("What rose in the last three weeks against the previous four weeks?", date(2026, 8, 11), date(2026, 9, 28)),
    # The first stated window wins; later ones are forecasts or context.
    ("What rose in the last two weeks, and what will grow two weeks from now?", date(2026, 9, 15), date(2026, 9, 28)),
    # No window, and no date table for named moments, so the default 30 days ending as_of stands.
    ("How did Heritage Day posts compare?", date(2026, 8, 30), date(2026, 9, 28)),
    ("What is Lagos talking about?", date(2026, 8, 30), date(2026, 9, 28)),
    ("What were the last days of winter like?", date(2026, 8, 30), date(2026, 9, 28)),
    ("What is Lagos talking about right now?", date(2026, 8, 30), date(2026, 9, 28)),
])
def test_window_reads_number_words_and_units(question, start, end):
    assert ask._window(question, NOW) == (start, end)


QUESTIONS = yaml.safe_load((ROOT / "core" / "eval" / "questions.yaml").read_text(encoding="utf-8"))

# Questions whose window_days the text cannot give, with the length the resolver does give and why.
WINDOW_EXCEPTIONS = {
    "RISE-02": (21, "the text says the last three weeks; window_days adds 28 days before it that the text never names"),
    "WHY-02": (30, "year on year, 1 August to now plus the same period in 2025; 'this time last year' has no length"),
    "SPR-01": (30, "the text says the last month; window_days doubles it to trace where the sound started"),
    "CRE-02": (30, "no window in the text, so the default applies; the author chose 60"),
    "BRD-02": (30, "no window in the text ('right now'), so the default applies; the author chose 60"),
    "CMP-02": (45, "the text says since mid-August; window_days 30 is shorter than its own evidence rule, "
                   "posts dated from 19 August"),
    "HIS-01": (30, "history over 2024 to 2026; no date table for Heritage Day, so the default applies"),
    "HIS-02": (7, "history around 1 October 2024 and 2025; the text's own window is this week"),
    "HIS-03": (30, "history back to June 2024; the text's own window is the last 30 days"),
    "FWD-03": (30, "a forecast to June 2027 with no observed window in the text; the author chose 90"),
}

# Questions whose markets the text does not name, with the markets it does name and why.
MARKET_EXCEPTIONS = {
    "SPR-03": (["KE"], "the text names only Kenya; ZA is there because amapiano is South African, and a genre is "
                       "not a place"),
    "FWD-03": (["KE"], "the text names only East African TikTok; ZA and NG are there because AFCON is continental, "
                       "which the text does not say"),
}


def test_questions_file_has_thirty_questions():
    ids = [q["vars"]["id"] for q in QUESTIONS]
    assert len(ids) == 30 == len(set(ids))
    assert set(WINDOW_EXCEPTIONS) <= set(ids) and set(MARKET_EXCEPTIONS) <= set(ids)


@pytest.mark.parametrize("item", [q["vars"] for q in QUESTIONS], ids=lambda v: v["id"])
def test_every_eval_question_resolves_to_its_window(item):
    start, end = ask._window(item["question"], NOW)
    assert end == date(2026, 9, 28)
    days = (end - start).days + 1
    if item["id"] in WINDOW_EXCEPTIONS:
        resolved, _ = WINDOW_EXCEPTIONS[item["id"]]
        assert days == resolved and days != item["window_days"]  # an exception that starts matching leaves the list
    else:
        assert days == item["window_days"], item["window"]


@pytest.mark.parametrize("item", [q["vars"] for q in QUESTIONS], ids=lambda v: v["id"])
def test_every_eval_question_detects_its_markets(item):
    found = ask.detect_markets(item["question"])
    assert len(found) == len(set(found))
    if item["id"] in MARKET_EXCEPTIONS:
        named, _ = MARKET_EXCEPTIONS[item["id"]]
        assert found == named and set(found) != set(item["markets"])
    else:
        assert set(found) == set(item["markets"])


@pytest.mark.parametrize("question, markets", [
    ("Is there a run club scene in Kisumu?", ["KE"]),
    ("Is there a run club scene in Kisumu or Nakuru?", ["KE"]),
    ("Which sound will take over East African TikTok?", ["KE"]),
    ("How did this year's Heritage Day and braai posts compare?", ["ZA"]),
    ("Compare the japa conversation in Nigeria with the leaving-SA conversation", ["NG", "ZA"]),
    ("Nigerian and South African stuff travels to Kenya", ["NG", "ZA", "KE"]),
    ("What do Kenyans in Mombasa and Nigerians in Abuja share?", ["KE", "NG"]),
    ("What is Southern Africa's biggest sound?", []),
    ("Salsa is big in Salsaville", []),
])
def test_market_detection_covers_the_places_the_questions_use(question, markets):
    assert ask.detect_markets(question) == markets


def test_replay_mode_reaches_the_client():
    h = Harness()
    out = h.run(mode="replay")
    assert [c.mode for c in h.clients] == ["replay"]
    assert h.clients[0].calls and h.clients[0].calls[0]["mode"] == "replay"
    assert out["run"]["mode"] == "replay"


def test_parent_and_from_card_go_into_the_prompt_as_context():
    seen = {}
    h = Harness(research=make_research(seen=seen))
    h.run(parent={"question": "What is amapiano?", "answer": {"short_answer": "A house music style from Gauteng."}},
          from_card={"item_id": "item_abc", "market": "ZA", "date": "2026-09-28"})
    assert "What is amapiano?" in seen["prompt"] and "A house music style from Gauteng." in seen["prompt"]
    assert "item_abc" in seen["prompt"]
    assert "Monday 28 September 2026" in seen["options"].system_prompt
    assert "Window: 2026-09-22 to 2026-09-28" in seen["options"].system_prompt
    assert "{" not in seen["options"].system_prompt


def test_stop_mid_research_gives_a_valid_insufficient_evidence_answer():
    flag = {"stop": False}
    h = Harness(research=make_research(stop_flag=flag), stop_flag=flag)
    out = h.run()
    answer = out["answer"]
    assert validate_answer(answer) == []
    assert answer["status"] == "insufficient_evidence"
    assert answer["claims"] == []
    assert any("stopped" in g["what"].lower() or "stopped" in g["why"].lower() for g in answer["gaps"])
    assert h.model.calls == []
    assert "claim" not in kinds(h.events)
    assert out["run"]["posts"] == 6
    assert out["run"]["tokens"] == {"input": 400, "output": 50}
    assert h.tables.inserts == []


def test_stop_during_writer_skips_later_checks_and_claim_persistence():
    stop_flag = {"stop": False}

    class StopsAfterWriter(FakeModel):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            if kwargs["schema"] is WRITER_SCHEMA:
                stop_flag["stop"] = True
            return result

    check_calls = []

    def check(draft, ctx, warehouse, *, window, markets):
        check_calls.append(True)
        return fake_check(draft, ctx, warehouse, window=window, markets=markets)

    h = Harness(check=check, stop_flag=stop_flag, model=StopsAfterWriter())
    out = h.run()

    assert validate_answer(out["answer"]) == []
    assert out["answer"]["status"] == "insufficient_evidence"
    assert check_calls == []
    assert [call["schema"] for call in h.model.calls] == [WRITER_SCHEMA]
    assert h.tables.inserts == []
    assert out["run"]["model_usd"] == pytest.approx(0.02 + 0.0135 + SEARCH_EMBED_USD)
    assert out["run"]["tokens"] == {"input": 1000 + 2000, "output": 300 + 500}


def test_stop_from_the_write_step_prevents_the_writer_call():
    stop_flag = {"stop": False}
    h = Harness(stop_flag=stop_flag)

    def emit(event):
        h.events.append(event)
        if event.get("event") == "step" and event.get("kind") == "write":
            stop_flag["stop"] = True

    out = ask.run_ask({"question": "What is behind amapiano in South Africa this week?", "tier": "T1",
                       "mode": "live", "ask_id": "a_20260928_test"}, emit, lambda: stop_flag["stop"],
                      deps=h.deps)

    assert out["answer"]["status"] == "insufficient_evidence"
    assert h.model.calls == []
    assert h.tables.inserts == []
    assert out["run"]["model_usd"] == pytest.approx(0.02 + SEARCH_EMBED_USD)


def test_stop_during_support_prevents_the_next_paid_check_and_keeps_its_cost():
    stop_flag = {"stop": False}

    class StopsAfterFirstSupport(FakeModel):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            if (kwargs["schema"] is SUPPORT_SCHEMA
                    and sum(call["schema"] is SUPPORT_SCHEMA for call in self.calls) == 1):
                stop_flag["stop"] = True
            return result

    h = Harness(stop_flag=stop_flag, model=StopsAfterFirstSupport())
    out = h.run()

    assert validate_answer(out["answer"]) == []
    assert out["answer"]["status"] == "insufficient_evidence"
    assert [call["schema"] for call in h.model.calls] == [WRITER_SCHEMA, SUPPORT_SCHEMA]
    assert h.tables.inserts == []
    assert out["run"]["model_usd"] == pytest.approx(0.02 + 0.0135 + 0.0006 + SEARCH_EMBED_USD)
    assert out["run"]["tokens"] == {"input": 1000 + 2000 + 100, "output": 300 + 500 + 20}


def test_stop_during_claim_emission_prevents_claim_check_persistence():
    stop_flag = {"stop": False}
    h = Harness(stop_flag=stop_flag)

    def emit(event):
        h.events.append(event)
        if event.get("event") == "claim":
            stop_flag["stop"] = True

    out = ask.run_ask({"question": "What is behind amapiano in South Africa this week?", "tier": "T1",
                       "mode": "live", "ask_id": "a_20260928_test"}, emit, lambda: stop_flag["stop"],
                      deps=h.deps)

    assert out["answer"]["status"] == "insufficient_evidence"
    assert len([event for event in h.events if event.get("event") == "claim"]) == 1
    assert h.tables.inserts == []
    assert out["run"]["model_usd"] > 0.02


def test_stop_during_the_field_check_keeps_its_cost_and_skips_persistence():
    stop_flag = {"stop": False}

    class StopsAfterFieldCheck(FakeModel):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            if kwargs["schema"] is FIELDS_SCHEMA:
                stop_flag["stop"] = True
            return result

    h = Harness(stop_flag=stop_flag, model=StopsAfterFieldCheck())
    out = h.run()

    assert out["answer"]["status"] == "insufficient_evidence"
    assert [call["schema"] for call in h.model.calls] == [WRITER_SCHEMA, SUPPORT_SCHEMA, SUPPORT_SCHEMA,
                                                          SUPPORT_SCHEMA, K4_REWRITE_SCHEMA, FIELDS_SCHEMA]
    assert h.tables.inserts == []
    assert out["run"]["model_usd"] == pytest.approx(0.02 + 0.0135 + 5 * 0.0006 + SEARCH_EMBED_USD)
    assert out["run"]["tokens"] == {"input": 1000 + 2000 + 5 * 100, "output": 300 + 500 + 5 * 20}


def hold(tier="T1", model=ask.MODEL):
    return ask.hold_usd(tier, model)


def current_model_cap():
    return ask.model_daily_usd(now=NOW)


@pytest.fixture
def cheaper_fast_model(monkeypatch):
    """A fast Gemini model priced below the main one (GEMINI_FAST_MODEL), so the T0 fallback hold is the smaller."""
    from core.llm.provider import GEMINI_LIST_PRICES

    monkeypatch.setitem(GEMINI_LIST_PRICES, "gemini-fast", {"input": 0.30, "output": 2.50, "cached": 0.03})
    monkeypatch.setattr(ask, "FALLBACK_MODEL", "gemini-fast")
    return "gemini-fast"


def fallback_spend():
    """Spend where no main-model hold fits but the T0 fast-model hold does."""
    return current_model_cap() - hold("T0", ask.FALLBACK_MODEL) - 0.01


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_model_cap_is_read_at_ask_decision_across_the_sast_cutover():
    before = datetime(2026, 10, 2, 23, 59, 59, tzinfo=SAST)
    after = datetime(2026, 10, 3, 0, 0, 0, tzinfo=SAST)
    assert ask.model_daily_usd(now=before) == 80.0
    assert ask.model_daily_usd(now=after) == 20.0

    admitted = Harness(spent=lambda: 20.0, now=before)
    admitted_out = admitted.run()
    assert admitted_out["run"]["tier"] == "T1"

    refused = Harness(spent=lambda: 20.0, now=after)
    refused_out = refused.run()
    assert refused_out["answer"]["status"] == "insufficient_evidence"
    assert refused.model.calls == []


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_ask_uses_the_cap_at_admission_time_if_the_spend_read_crosses_cutover():
    before = datetime(2026, 10, 2, 23, 59, 59, tzinfo=SAST)
    after = datetime(2026, 10, 3, 0, 0, 0, tzinfo=SAST)
    moments = iter((before, after))
    h = Harness(spent=lambda: 20.0)
    h.deps.now = lambda: next(moments)

    out = h.run()

    assert out["answer"]["as_of"] == before.isoformat()
    assert out["answer"]["status"] == "insufficient_evidence"
    assert h.model.calls == []


@pytest.mark.usefixtures("cheaper_fast_model")
def test_the_hold_is_the_tier_budget_plus_the_writer_and_ten_support_calls_at_list_price():
    assert (ask.WRITER_MAX_TOKENS, ask.SUPPORT_MAX_TOKENS, ask.SUPPORT_CALLS) == (8000, 400, 10)
    assert ask.WRITER_INPUT_TOKENS >= 100_000 and ask.SUPPORT_INPUT_TOKENS >= 10_000
    assert (K4_REWRITE_INPUT_TOKENS, K4_REWRITE_MAX_TOKENS, K4_REWRITE_CALLS, K4_RECHECK_CALLS) == (20_000, 200, 10, 10)
    for tier in ("T0", "T1"):
        for model in (ask.MODEL, ask.FALLBACK_MODEL):
            one_pass = (ask.call_usd(model, ask.WRITER_INPUT_TOKENS, ask.WRITER_MAX_TOKENS)
                        + ask.SUPPORT_CALLS * ask.call_usd(model, ask.SUPPORT_INPUT_TOKENS, ask.SUPPORT_MAX_TOKENS)
                        + ask.FIELD_CALLS * ask.call_usd(model, ask.FIELD_INPUT_TOKENS, ask.FIELD_MAX_TOKENS)
                        + K4_REWRITE_CALLS * ask.call_usd(model, K4_REWRITE_INPUT_TOKENS, K4_REWRITE_MAX_TOKENS)
                        + K4_RECHECK_CALLS * ask.call_usd(model, ask.SUPPORT_INPUT_TOKENS, ask.SUPPORT_MAX_TOKENS))
            assert ask.pass_usd(model) == pytest.approx(one_pass)
            repair_call = ask.call_usd(model, ask.WRITER_INPUT_TOKENS, ask.WRITER_MAX_TOKENS)
            assert hold(tier, model) == pytest.approx(TIERS[tier]["max_budget_usd"] + one_pass + repair_call)
    assert ask.FIELD_INPUT_TOKENS == 10 * 4 * 1000 + ask.FIELD_ITEMS * ask.FIELD_ITEM_TOKENS
    assert ask.FIELD_ITEMS >= 2 + 2 * 5 and ask.FIELD_MAX_TOKENS == 400 + 40 * ask.FIELD_ITEMS
    assert hold("T0", ask.FALLBACK_MODEL) < hold("T0") < hold("T1")


def test_the_hold_covers_every_model_call_an_ask_makes():
    h = Harness()
    h.run()
    writer = [c for c in h.model.calls if c["schema"] is WRITER_SCHEMA]
    support = [c for c in h.model.calls if c["schema"] is SUPPORT_SCHEMA]
    assert len(writer) == 1 and writer[0]["max_tokens"] <= ask.WRITER_MAX_TOKENS
    assert 1 <= len(support) <= ask.SUPPORT_CALLS
    assert all(c["max_tokens"] <= ask.SUPPORT_MAX_TOKENS for c in support)


def test_t1_is_admitted_while_its_whole_hold_fits_under_the_cap():
    seen = {}
    h = Harness(research=make_research(seen=seen), spent=lambda: current_model_cap() - hold() - 0.01)
    out = h.run()
    assert out["run"]["tier"] == "T1" and seen["options"].model == ask.MODEL == "gemini-3.8-flash"
    assert out["run"]["notices"] == []


def test_t1_whose_hold_would_cross_the_cap_falls_back_to_t0_and_the_fast_model(cheaper_fast_model):
    seen = {}
    h = Harness(research=make_research(seen=seen), spent=lambda: current_model_cap() - hold() + 0.01)
    out = h.run()
    assert out["run"]["tier"] == "T0" and seen["tier"] == "T0"
    assert seen["options"].model == cheaper_fast_model
    assert {c["model"] for c in h.model.calls} == {cheaper_fast_model}
    assert any(f"USD {current_model_cap():g}" in n and "the faster model" in n for n in out["run"]["notices"])
    assert not any("could not be read" in n for n in out["run"]["notices"])


def test_spend_at_the_cap_minus_a_cent_gives_no_t1():
    seen = {}
    h = Harness(research=make_research(seen=seen), spent=lambda: current_model_cap() - 0.01)
    out = h.run()
    assert out["run"]["tier"] != "T1" and seen == {}
    assert h.model.calls == []


def test_a_t0_ask_on_the_main_model_that_does_not_fit_falls_back_to_the_fast_model(cheaper_fast_model):
    seen = {}
    h = Harness(research=make_research(seen=seen), spent=lambda: current_model_cap() - hold("T0") + 0.01)
    out = h.run(tier="T0")
    assert out["run"]["tier"] == "T0" and seen["options"].model == cheaper_fast_model
    assert not any("could not be read" in n for n in out["run"]["notices"])


def test_model_spend_that_leaves_room_only_for_the_fast_model_falls_back_to_t0_on_it(cheaper_fast_model):
    seen = {}
    h = Harness(research=make_research(seen=seen), spent=fallback_spend)
    out = h.run()
    assert out["run"]["tier"] == "T0" and seen["tier"] == "T0"
    assert seen["options"].model == cheaper_fast_model
    assert {c["model"] for c in h.model.calls} == {cheaper_fast_model}
    assert any(f"USD {current_model_cap():g}" in n for n in out["run"]["notices"])
    assert not any("could not be read" in n for n in out["run"]["notices"])


def test_the_fast_model_fallback_reaches_the_support_check_calls(cheaper_fast_model):
    h = Harness(spent=fallback_spend)
    h.run()
    assert [c["model"] for c in h.model.calls if c["schema"] is SUPPORT_SCHEMA] == [cheaper_fast_model] * 3


@pytest.mark.parametrize("spent", [lambda: current_model_cap() - hold("T0", ask.FALLBACK_MODEL) + 0.01,
                                   lambda: current_model_cap() - 0.01, lambda: current_model_cap(),
                                   lambda: current_model_cap() + 5.0],
                         ids=["just_over_the_fallback_hold", "19.99", "at_the_cap", "over_the_cap"])
def test_spend_with_no_room_for_the_fallback_hold_refuses_the_ask(spent):
    seen = {}
    h = Harness(research=make_research(seen=seen), spent=spent)
    out = h.run()
    answer, run = out["answer"], out["run"]
    assert validate_answer(answer) == []
    assert answer["status"] == "insufficient_evidence" and answer["claims"] == []
    assert any("model budget for today is spent" in n and f"USD {current_model_cap():g}" in n
               for n in run["notices"])
    assert seen == {} and h.model.calls == [] and h.tables.inserts == []
    assert run["model_usd"] == 0.0 and set(run) == RUN_KEYS
    assert ask._IN_FLIGHT == {}


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_a_failing_spend_reader_refuses_the_ask_like_an_over_cap_ask_with_a_notice():
    # Spec change: an unknown spend proves no hold fits, so the ask is refused rather than run at T0 on the fast model.
    def broken():
        raise TimeoutError("runs read timed out")

    seen = {}
    h = Harness(research=make_research(seen=seen), spent=broken)
    out = h.run()
    answer, run = out["answer"], out["run"]
    assert validate_answer(answer) == []
    assert answer == Harness(spent=lambda: 25.0).run()["answer"]
    assert answer["status"] == "insufficient_evidence" and "model budget for today" in answer["short_answer"]
    assert any("could not be read" in n and "USD 20" in n for n in run["notices"])
    assert not any("the faster model" in n for n in run["notices"])
    assert seen == {} and h.model.calls == [] and h.tables.inserts == []
    assert run["model_usd"] == 0.0 and set(run) == RUN_KEYS
    assert ask._IN_FLIGHT == {}


class SpendWarehouse:
    def __init__(self, usd=3.5, error=None):
        self.usd, self.error, self.runs = usd, error, []

    def run(self, sql, params, max_bytes_billed):
        self.runs.append((sql, params, max_bytes_billed))
        if self.error:
            raise self.error
        return [{"usd": self.usd}]


def test_spend_query_is_read_only_parameterised_and_reads_every_stage():
    # Spec change (task 1.11): MODEL_DAILY_USD is one daily total, so the sum no longer filters on stage.
    check_sql(ask.SPEND_SQL)
    assert "intelligence_42_agent.runs" in ask.SPEND_SQL
    assert "JSON_VALUE(r.record, '$.run.model_usd')" in ask.SPEND_SQL
    assert "JSON_VALUE(r.counts, '$.model_usd')" in ask.SPEND_SQL
    assert "@day" in ask.SPEND_SQL
    assert "stage" not in ask.SPEND_SQL.lower()
    assert "'ask'" not in ask.SPEND_SQL and "2026" not in ask.SPEND_SQL


def test_model_spend_today_sums_every_run_for_the_sast_day():
    wh = SpendWarehouse(usd=12.25)
    late_utc = datetime(2026, 9, 27, 22, 30, tzinfo=timezone.utc)  # already 28 September in Johannesburg
    assert ask.model_spend_today(wh, late_utc) == 12.25
    ((sql, params, cap),) = wh.runs
    assert sql == ask.SPEND_SQL
    assert params == {"day": date(2026, 9, 28)}
    assert cap == MAX_BYTES_BILLED


class RunsWarehouse:
    """intelligence_42_agent.runs held in SQLite; each query is transpiled from BigQuery SQL and really executed."""

    def __init__(self, rows):
        import sqlite3

        self.db = sqlite3.connect(":memory:")
        self.db.execute("ATTACH DATABASE ':memory:' AS intelligence_42_agent")
        self.db.execute("CREATE TABLE intelligence_42_agent.runs (run_id TEXT, stage TEXT, run_date TEXT, counts TEXT, "
                        "record TEXT)")
        self.db.executemany("INSERT INTO intelligence_42_agent.runs VALUES (?, ?, ?, ?, ?)",
                            [(r["run_id"], r["stage"], r["run_date"].isoformat(),
                              json.dumps(r["counts"]) if r.get("counts") is not None else None,
                              json.dumps(r["record"]) if r.get("record") is not None else None) for r in rows])
        self.runs = []

    def run(self, sql, params, max_bytes_billed):
        import sqlglot

        self.runs.append((sql, params))
        lite = sqlglot.transpile(sql, read="bigquery", write="sqlite")[0]
        bound = {k: v.isoformat() if isinstance(v, date) else v for k, v in params.items()}
        cursor = self.db.execute(lite, bound)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def runs_row(run_id, stage, day, counts=None, record=None):
    return {"run_id": run_id, "stage": stage, "run_date": day, "counts": counts, "record": record}


TODAY, YESTERDAY = date(2026, 9, 28), date(2026, 9, 27)


def test_model_spend_today_counts_ask_understand_and_brief_rows_from_either_json_path():
    wh = RunsWarehouse([
        runs_row("r_ask", "ask", TODAY, record={"run": {"model_usd": 4.0}}),
        runs_row("r_embed", "understand", TODAY, counts={"posts": 900, "model_usd": 0.5}),
        runs_row("r_brief", "brief", TODAY, counts={"model_usd": 1.25}),
        runs_row("r_both", "ask", TODAY, counts={"model_usd": 99.0}, record={"run": {"model_usd": 2.0}}),
        runs_row("r_none", "collect", TODAY, counts={"posts": 12}),
        runs_row("r_old", "brief", YESTERDAY, counts={"model_usd": 50.0}),
    ])
    assert ask.model_spend_today(wh, NOW) == pytest.approx(4.0 + 0.5 + 1.25 + 2.0)


def test_model_spend_today_is_zero_when_no_row_today_carries_model_spend():
    wh = RunsWarehouse([runs_row("r_none", "collect", TODAY, counts={"posts": 12}),
                        runs_row("r_old", "ask", YESTERDAY, record={"run": {"model_usd": 3.0}})])
    assert ask.model_spend_today(wh, NOW) == 0.0


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_brief_and_embedding_spend_count_toward_the_cap_in_admission():
    ask_only = [runs_row("r_ask", "ask", TODAY, record={"run": {"model_usd": 1.0}})]
    others = [runs_row("r_embed", "understand", TODAY, counts={"model_usd": 9.0}),
              runs_row("r_brief", "brief", TODAY, counts={"model_usd": 10.0})]

    def harness(rows):
        wh = RunsWarehouse(rows)
        return Harness(spent=lambda: ask.model_spend_today(wh, NOW))

    alone = harness(ask_only).run()["run"]
    assert alone["tier"] == "T1" and alone["notices"] == []

    h = harness(ask_only + others)
    run = h.run()["run"]
    assert any("model budget for today is spent" in n for n in run["notices"])
    assert h.model.calls == [] and run["model_usd"] == 0.0


def test_model_spend_today_treats_null_as_zero():
    assert ask.model_spend_today(SpendWarehouse(usd=None), NOW) == 0.0


def test_model_spend_today_lets_a_read_failure_through_for_run_ask_to_fail_closed():
    with pytest.raises(TimeoutError):
        ask.model_spend_today(SpendWarehouse(error=TimeoutError("slow")), NOW)


def test_default_deps_wire_a_spend_reader_on_the_default_warehouse():
    deps = ask._default_deps()
    assert callable(deps.spent_today_usd)
    wh = SpendWarehouse(usd=7.0)
    deps.warehouse.run = wh.run  # no cloud: the reader must go through this warehouse's run path
    assert deps.spent_today_usd() == 7.0
    ((sql, params, _),) = wh.runs
    assert sql == ask.SPEND_SQL and "stage" not in params
    assert params["day"] == datetime.now(ask.SAST).date()


def test_model_spend_under_the_cap_keeps_the_tier_and_the_main_model(cheaper_fast_model):
    seen = {}
    h = Harness(research=make_research(seen=seen), spent=lambda: 5.0)
    out = h.run()
    assert out["run"]["tier"] == "T1" and seen["options"].model == "gemini-3.8-flash"
    assert {c["model"] for c in h.model.calls} == {"gemini-3.8-flash"}
    assert out["run"]["notices"] == []


def test_full_run_through_the_real_code_checks():
    h = Harness(check=None)
    out = h.run()
    answer = out["answer"]
    assert validate_answer(answer) == []
    ids = [c["id"] for c in answer["claims"]]
    assert "c1" in ids and "c4" not in ids  # c4 misquotes x_2, so K1 cuts it
    rows = h.tables.inserts[0][1]
    assert {r["checker"] for r in rows} == {"code", "model"}
    assert ("c4", "cut") in {(r["claim_id"], r["verdict"]) for r in rows}
    claims = {e["claim"]["id"]: e["check"] for e in h.events if e["event"] == "claim"}
    assert claims["c4"] == "cut" and claims["c1"] in ("verified", "downgraded")


class ThreadsGapModel(FakeModel):
    """The writer reports the Threads failure itself as well, the way a model often does."""

    def complete_json(self, **kwargs):
        out, usage = super().complete_json(**kwargs)
        if kwargs["schema"] is WRITER_SCHEMA:
            out["gaps"] = [{"what": "Threads search was rate limited", "searched": "threads/search", "why": "rate limit"}]
        return out, usage


def test_a_rate_limited_threads_source_yields_exactly_one_gap():
    h = Harness(check=None, model=ThreadsGapModel())
    answer = h.run()["answer"]
    threads = [g for g in answer["gaps"] if "threads" in (g["what"] + g["searched"] + g["why"]).lower()]
    assert threads == [{"what": "No live Threads data", "searched": "threads/search, 22 to 28 September",
                        "why": "rate_limited"}]
    writer_call = next(c for c in h.model.calls if c["schema"] is WRITER_SCHEMA)
    assert "threads/search" in writer_call["user"] and "rate_limited" in writer_call["user"]


WINDOW_GAP = {"what": "No Reddit posts in the last 7 days", "searched": "reddit/search", "why": "zero results"}


class WindowGapModel(FakeModel):
    """The writer names the window's length in a gap, which the checks allow."""

    def complete_json(self, **kwargs):
        out, usage = super().complete_json(**kwargs)
        if kwargs["schema"] is WRITER_SCHEMA:
            out["gaps"] = [dict(WINDOW_GAP)]
        return out, usage


def test_a_gap_naming_the_window_length_survives_the_real_checks_and_the_recheck():
    h = Harness(check=None, model=WindowGapModel())
    out = h.run()
    assert out["run"]["window"] == {"from": "2026-09-22", "to": "2026-09-28"}
    assert WINDOW_GAP in out["answer"]["gaps"]
    rows = h.tables.inserts[0][1]
    assert [r for r in rows if str(r["claim_id"]).startswith("gaps/")] == []


NOTE = "Reading: amapiano, South Africa, this week. tt_99 looks like the post that started it."


class NoteCitingModel(FakeModel):
    """The writer cites tt_99, which only the research note names; it is not in the evidence pack."""

    def complete_json(self, **kwargs):
        out, usage = super().complete_json(**kwargs)
        if kwargs["schema"] is WRITER_SCHEMA:
            out["claims"].append({"id": "c5", "text": "One post started the amapiano wave.", "label": "single_source",
                                  "kind": "observation", "evidence_ids": ["tt_99"], "quotes": [], "numbers": []})
        return out, usage


def test_research_note_reaches_the_writer_but_an_id_only_in_the_note_is_cut():
    base = make_research()

    def research(*args):
        return {**base(*args), "note": NOTE}

    h = Harness(research=research, check=None, model=NoteCitingModel())
    answer = h.run()["answer"]
    writer_call = next(c for c in h.model.calls if c["schema"] is WRITER_SCHEMA)
    assert NOTE in writer_call["user"]
    assert "c5" not in [c["id"] for c in answer["claims"]]
    assert "tt_99" not in {r["id"] for r in answer["evidence"]}
    claims = {e["claim"]["id"]: e for e in h.events if e["event"] == "claim"}
    assert claims["c5"]["check"] == "cut" and "tt_99" not in claims["c5"]["reason"]
    assert "1 id that does not resolve" in claims["c5"]["reason"]
    assert validate_answer(answer) == []


def test_an_answer_that_fails_the_contract_raises():
    def broken_check(draft, ctx, warehouse, *, window, markets):
        answer = copy.deepcopy(draft)
        answer["claims"][0]["quotes"] = [{"evidence_id": "tt_1", "text": "words nobody posted"}]
        return answer, []

    with pytest.raises(RuntimeError, match="verbatim"):
        Harness(check=broken_check).run()


def test_a_failure_after_the_writer_carries_the_run_so_far():
    def failing_check(draft, ctx, warehouse, *, window, markets):
        raise TimeoutError("checks timed out")

    h = Harness(check=failing_check)
    with pytest.raises(TimeoutError, match="checks timed out") as caught:
        h.run()
    run = caught.value.run
    assert set(run) == RUN_KEYS
    assert run["model_usd"] > 0
    assert run["model_usd"] == pytest.approx(0.02 + 0.0135 + SEARCH_EMBED_USD)
    assert run["tokens"] == {"input": 1000 + 2000, "output": 300 + 500}
    assert run["credits"] == 1.0
    assert run["tier"] == "T1" and run["mode"] == "live"
    assert run["window"] == {"from": "2026-09-22", "to": "2026-09-28"}
    assert run["posts"] == 6 and run["platforms"] == 2
    assert run["source_status"] == [{"platform": "threads", "route": "threads/search", "status": "rate_limited",
                                     "items": 0}]
    assert run["followups"] == []
    assert isinstance(run["seconds"], (int, float)) and run["seconds"] >= 0


def test_a_contract_failure_carries_the_run_with_the_support_check_spend():
    def broken_check(draft, ctx, warehouse, *, window, markets):
        answer = copy.deepcopy(draft)
        answer["claims"][0]["quotes"] = [{"evidence_id": "tt_1", "text": "words nobody posted"}]
        return answer, []

    h = Harness(check=broken_check)
    with pytest.raises(RuntimeError, match="verbatim") as caught:
        h.run()
    run = caught.value.run
    assert set(run) == RUN_KEYS
    support_calls = len([c for c in h.model.calls if c["schema"] is SUPPORT_SCHEMA])
    assert support_calls > 0
    billed = support_calls + 2  # the support calls, K4 rewrite and field check
    assert run["model_usd"] == pytest.approx(0.02 + 0.0135 + billed * 0.0006 + SEARCH_EMBED_USD)
    assert run["tokens"] == {"input": 1000 + 2000 + billed * 100, "output": 300 + 500 + billed * 20}


def test_not_connected_client_reports_auth_failed_and_a_notice():
    client = ask.NotConnectedClient("live")
    result = client.call("tiktok/search", {}, lane="agent_live", run_id="r", max_credits=5)
    assert result["status"] == "auth_failed" and result["items"] == []
    assert client.notices == ["Live search is not connected yet; this answer uses stored posts only"]

    h = Harness()
    h.deps.socialcrawl = ask.NotConnectedClient
    out = h.run()
    assert out["run"]["notices"] == ["Live search is not connected yet; this answer uses stored posts only"]
    assert out["run"]["source_status"][0]["status"] == "auth_failed"
    assert validate_answer(out["answer"]) == []


def test_run_ask_builds_the_client_with_the_run_id_and_mode():
    h = Harness()
    out = h.run(mode="replay")
    (client,) = h.clients
    assert client.run_id == out["run"]["run_id"] and client.mode == "replay"


def test_scheduled_run_id_is_kept_on_the_run_the_client_and_the_numbers():
    # Contract 14.2: a scheduled ask sends sched-<date>-<schedule_id>, and its credit_ledger rows carry it.
    h = Harness()
    out = h.run(run_id="sched-2026-09-28-s_0a1b2c3d4e5f", schedule_id="s_0a1b2c3d4e5f")
    (client,) = h.clients
    assert out["run"]["run_id"] == "sched-2026-09-28-s_0a1b2c3d4e5f"
    assert client.run_id == "sched-2026-09-28-s_0a1b2c3d4e5f"
    assert all(n["run_id"] == "sched-2026-09-28-s_0a1b2c3d4e5f"
               for c in out["answer"]["claims"] for n in c.get("numbers", []))


@pytest.mark.parametrize("run_id", [
    "r_20260928_081500_custom",  # only the scheduled job may name its run
    "sched-2026-09-27-s_yesterday",  # not today's date in SAST
    "sched-2026-09-28-s_../other",
    "sched-2026-09-28-S_UPPER",
    "sched-2026-09-28-",
    "sched-2026-9-28-s_short",
    " sched-2026-09-28-s_padded",
    "sched-2026-09-28-s_padded\n",
    "sched-2026-09-28-s_" + "a" * 40,
    7,
])
def test_a_run_id_that_is_not_todays_scheduled_id_is_refused_before_any_spend(run_id):
    h = Harness()
    with pytest.raises(ValueError, match="run_id"):
        h.run(run_id=run_id)
    assert h.clients == []


def test_a_scheduled_run_id_is_only_for_t0_or_t1():
    h = Harness()
    with pytest.raises(ValueError, match="run_id"):
        h.run(run_id="sched-2026-09-28-s_deeper", tier="T2")
    assert h.clients == []


def test_a_scheduled_run_id_cannot_be_used_twice():
    h = Harness()
    h.run(run_id="sched-2026-09-28-s_twice")
    with pytest.raises(ValueError, match="run_id"):
        h.run(run_id="sched-2026-09-28-s_twice")
    assert len(h.clients) == 1


def test_no_run_id_still_mints_an_r_run_id():
    h = Harness()
    out = h.run(run_id=None)
    assert re.fullmatch(r"r_20260928_081500_[0-9a-f]{8}_[0-9a-f]{32}", out["run"]["run_id"])


class FakeL1Client:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def fake_collect(monkeypatch):
    """A fake core.collect (lane L1's client and stores) and a BigQuery client that never connects."""
    import types

    from google.cloud import bigquery

    package = types.ModuleType("core.collect")
    package.__path__ = []
    client_mod = types.ModuleType("core.collect.socialcrawl_client")
    client_mod.SocialCrawlClient = FakeL1Client
    client_mod.requests_http = lambda *a, **k: pytest.fail("no HTTP in tests")
    stores = types.ModuleType("core.collect.stores")
    stores.BigQueryLedgerStore = stores.BigQueryRawStore = lambda client, project: (client, project)
    monkeypatch.setitem(sys.modules, "core.collect", package)
    monkeypatch.setitem(sys.modules, "core.collect.socialcrawl_client", client_mod)
    monkeypatch.setitem(sys.modules, "core.collect.stores", stores)
    monkeypatch.setattr(bigquery, "Client", lambda project: object())


def test_default_socialcrawl_factory_binds_l1_through_the_adapter(fake_collect):
    from core.agent.tools import sc_adapter

    adapter = ask._socialcrawl_client("replay", run_id="r_20260928_081500_abcd1234")
    assert isinstance(adapter, sc_adapter.L1Adapter)
    assert adapter.client.kwargs["run_id"] == "r_20260928_081500_abcd1234"
    assert adapter.client.kwargs["mode"] == "replay"
    assert adapter.client.kwargs["share"] == "ask"


def test_run_ask_on_the_default_factory_hands_research_the_adapter_for_this_run(fake_collect):
    from core.agent.tools import sc_adapter

    seen = {}
    h = Harness(research=make_research(seen=seen, live=False))
    h.deps.socialcrawl = ask._socialcrawl_client
    out = h.run(mode="replay")
    adapter = seen["options"].client.client
    assert isinstance(adapter, sc_adapter.L1Adapter)
    assert adapter.client.kwargs["run_id"] == out["run"]["run_id"]
    assert adapter.client.kwargs["mode"] == "replay"
    assert out["run"]["notices"] == []


def test_default_socialcrawl_factory_uses_the_stub_without_core_collect(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.collect", None)
    monkeypatch.delitem(sys.modules, "core.collect.socialcrawl_client", raising=False)
    monkeypatch.delitem(sys.modules, "core.collect.stores", raising=False)
    client = ask._socialcrawl_client("replay", run_id="r_test")
    assert isinstance(client, ask.NotConnectedClient)
    assert client.mode == "replay"
    assert client.notices == [ask.NOT_CONNECTED]


def test_default_socialcrawl_factory_raises_on_a_broken_import_other_than_core_collect(fake_collect, monkeypatch):
    import google.cloud

    monkeypatch.delattr(google.cloud, "bigquery", raising=False)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)
    with pytest.raises(ImportError):
        ask._socialcrawl_client("live", run_id="r_test")


def test_the_guessed_import_path_is_gone():
    source = Path(ask.__file__).read_text(encoding="utf-8")
    assert "importlib" not in source and "SocialCrawlClient(mode=mode)" not in source


def test_module_import_loads_no_cloud_or_model_sdk():
    # A site .pth file already loads the empty google.cloud namespace at startup, so compare before and after.
    code = ("import sys; before = set(sys.modules); import core.agent.ask; "
            "bad = sorted(m for m in set(sys.modules) - before if m == 'google.cloud' or m.startswith('google.cloud.') "
            "or m == 'google.genai' or m.startswith('google.genai.')); print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    assert out.stdout.decode("utf-8").strip() == "[]"


# The default research is the Gemini loop, driven by a scripted fake google-genai client: no network.

def gemini_part(name=None, text=None, **args):
    from types import SimpleNamespace as NS

    call = NS(id=f"call_{name}", name=name, args=args) if name else None
    return NS(function_call=call, text=text, thought=False)


def gemini_reply(*parts, pin=1200, pout=400):
    from types import SimpleNamespace as NS

    return NS(candidates=[NS(content=NS(role="model", parts=list(parts)), finish_reason=NS(name="STOP"))],
              usage_metadata=NS(prompt_token_count=pin, response_token_count=pout, thoughts_token_count=0,
                                tool_use_prompt_token_count=0, candidates_token_count=None))


class FakeGenaiClient:
    def __init__(self, *responses):
        from types import SimpleNamespace as NS

        self.responses, self.calls = list(responses), []
        self.models = NS(generate_content=self.generate)

    def generate(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def research_setup(ctx_market="ZA"):
    from core.agent.context import RunContext

    ctx = RunContext(run_id="r_test", tier="T1", as_of=NOW, market=ctx_market)
    warehouse = FakeWarehouse()
    setup = ask.ResearchSetup(system_prompt="You are 42's lead analyst.", model="gemini-3.8-flash",
                              warehouse=warehouse, client=FakeClient("live"), tables=FakeTables())
    events = []
    progress = ask.Progress(events.append, lambda: NOW, market_label="South Africa",
                            window=(date(2026, 9, 22), date(2026, 9, 28)))
    return ctx, warehouse, setup, events, progress


def amapiano_client():
    return FakeGenaiClient(
        gemini_reply(gemini_part(text="Reading the warehouse first."),
                     gemini_part("search_posts", query="amapiano", since="2026-09-22", until="2026-09-28")),
        gemini_reply(gemini_part("sql_query", sql=COUNT_SQL, purpose="authors for amapiano",
                                 params={"term": "amapiano"})),
        gemini_reply(gemini_part(text="Reading: amapiano, South Africa.")))


def test_default_research_emits_plain_steps_evidence_and_usage(monkeypatch):
    from core.agent import gemini_research
    from core.llm.provider import price_for

    client = amapiano_client()
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    ctx, warehouse, setup, events, progress = research_setup()
    result = ask.default_research(ctx, "What is behind amapiano?", setup, progress, lambda: False)

    assert client.calls[0]["model"] == "gemini-3.8-flash"
    assert client.calls[0]["config"].system_instruction == "You are 42's lead analyst."
    price = price_for("gemini-3.8-flash")
    assert result == {"note": "Reading the warehouse first.\nReading: amapiano, South Africa.",
                      "tokens": {"input": 3 * 1200, "output": 3 * 400},
                      "usd": pytest.approx(3 * (1200 * price["input"] + 400 * price["output"]) / 1e6),
                      "stopped": False}

    steps = [e for e in events if e["event"] == "step"]
    assert steps[0] == {"event": "step", "at": "2026-09-28T08:15:00+02:00", "kind": "search",
                        "text": "Searching stored posts for 'amapiano', South Africa, 22 to 28 September",
                        "platform": None, "count": None}
    found = [s for s in steps if s["kind"] == "found"]
    assert found[0]["count"] == 6 and found[0]["text"] == "6 posts found on 2 platforms"
    assert found[0]["platform"] is None
    assert any(s["kind"] == "read" and "authors for amapiano" in s["text"] for s in steps)
    evidence = [e["evidence"]["id"] for e in events if e["event"] == "evidence"]
    assert sorted(evidence) == sorted(p["post_id"] for p in POSTS)
    first_evidence = [e["event"] for e in events].index("evidence")
    assert [e["event"] for e in events].index("step") < first_evidence


def test_default_research_stops_when_stop_is_asked(monkeypatch):
    from core.agent import gemini_research

    client = amapiano_client()
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    ctx, warehouse, setup, events, progress = research_setup()
    result = ask.default_research(ctx, "q", setup, progress, lambda: len(ctx.evidence) > 0)

    assert result["stopped"] is True and len(client.calls) == 1
    assert result["tokens"] == {"input": 1200, "output": 400}
    assert not any(e["event"] == "step" and "authors for amapiano" in e["text"] for e in events)
    assert len(ctx.queries) == 2  # search_posts records its keyword and semantic queries


def test_a_k1_cut_event_never_shows_its_unverified_quotes():
    claim = {"id": "c9", "text": "People joke about the price.", "quotes": [{"evidence_id": "tt_1", "text": "made up words"}],
             "evidence_ids": ["tt_1"]}
    rows = [{"claim_id": "c9", "rule": "K1", "verdict": "cut", "reason": "quote not verbatim"}]
    shown = ask._withheld(claim, rows, [{"id": "tt_1", "text": "fuel is too expensive"}])
    assert shown["quotes"] == []
    assert shown["text"] == "People joke about the price."
    assert shown["evidence_ids"] == ["tt_1"]


# Round 3: spend on failure, refused routes in the event stream, and the in-flight spend hold

class TruncatingClient:
    """A google-genai client whose writer response stops at MAX_TOKENS: billed, but unusable."""

    def __init__(self):
        from types import SimpleNamespace as NS

        self.models = NS(generate_content=self.generate)

    def generate(self, **kwargs):
        from types import SimpleNamespace as NS
        return NS(candidates=[NS(finish_reason=NS(name="MAX_TOKENS"))], text='{"short_answer": "Amap',
                  usage_metadata=NS(prompt_token_count=2000, response_token_count=8000, thoughts_token_count=0,
                                    tool_use_prompt_token_count=0, cached_content_token_count=0))


def test_a_writer_max_tokens_failure_carries_its_cost_into_the_run():
    from core.llm.gemini import GeminiModel
    from core.llm.provider import price_for

    model = GeminiModel(client=TruncatingClient())
    h = Harness(model=model)
    with pytest.raises(RuntimeError, match="max_tokens") as caught:
        h.run()
    run = caught.value.run
    price = price_for(ask.MODEL)
    writer_usd = (2000 * price["input"] + 8000 * price["output"]) / 1e6
    assert run["model_usd"] == pytest.approx(0.02 + writer_usd + SEARCH_EMBED_USD)
    assert run["tokens"] == {"input": 1000 + 2000, "output": 300 + 8000}


def test_a_support_check_failure_carries_the_support_spend_so_far_into_the_run():
    class FailsSecondSupport(FakeModel):
        def complete_json(self, **kwargs):
            if kwargs["schema"] is SUPPORT_SCHEMA and "fading" in kwargs["user"]:  # c2, the second claim
                self.calls.append({"model": kwargs["model"], "schema": kwargs["schema"], "user": kwargs["user"]})
                error = RuntimeError("support check hit max_tokens")
                error.usage = {"input_tokens": 100, "output_tokens": 400, "usd": 0.0063}
                raise error
            return super().complete_json(**kwargs)

    h = Harness(model=FailsSecondSupport())
    with pytest.raises(RuntimeError, match="max_tokens") as caught:
        h.run()
    run = caught.value.run
    # The support checks run at the same time, so c3's was billed too and counts with c1's and the failed c2's.
    assert run["model_usd"] == pytest.approx(0.02 + 0.0135 + 2 * 0.0006 + 0.0063 + SEARCH_EMBED_USD)
    assert run["tokens"] == {"input": 1000 + 2000 + 2 * 100 + 100, "output": 300 + 500 + 2 * 20 + 400}


def test_a_missing_forecast_classification_fails_closed_and_keeps_its_cost():
    class MissingForecastFlag(FakeModel):
        def complete_json(self, **kwargs):
            result = super().complete_json(**kwargs)
            if kwargs["schema"] is SUPPORT_SCHEMA:
                result[0].pop("forecast_assertion")
            return result

    model = MissingForecastFlag()
    h = Harness(model=model)
    with pytest.raises(ValueError, match="forecast_assertion") as caught:
        h.run()

    # The three support checks run at the same time: all are billed and counted, and c1's missing flag fails closed.
    assert [call["schema"] for call in model.calls] == [WRITER_SCHEMA, SUPPORT_SCHEMA, SUPPORT_SCHEMA, SUPPORT_SCHEMA]
    assert caught.value.run["tokens"] == {"input": 1000 + 2000 + 3 * 100, "output": 300 + 500 + 3 * 20}
    assert caught.value.run["model_usd"] == pytest.approx(0.02 + 0.0135 + 3 * 0.0006 + SEARCH_EMBED_USD)


@pytest.mark.parametrize("spent, tier", [(None, "T1"), (fallback_spend, "T0")])
def test_research_that_raises_before_reporting_its_cost_counts_only_the_calls_it_reserved(spent, tier):
    # On Gemini every research call books its reserve in the question's model budget before it is sent, so a
    # research failure counts what it booked (nothing here), not the whole tier budget.
    def research(ctx, prompt, options, emit, should_stop):
        raise ConnectionError("agent stream dropped")

    h = Harness(research=research, spent=spent)
    with pytest.raises(ConnectionError) as caught:
        h.run()
    run = caught.value.run
    assert run["tier"] == tier
    assert run["model_usd"] == 0.0
    assert any(n.startswith("Research failed before reporting usage") for n in run["notices"])
    assert not any("is counted as spent" in n for n in run["notices"])
    assert h.model.calls == []


def test_research_that_reported_its_cost_is_not_charged_the_budget_on_a_later_failure():
    def failing_check(draft, ctx, warehouse, *, window, markets):
        raise TimeoutError("checks timed out")

    h = Harness(check=failing_check)
    with pytest.raises(TimeoutError) as caught:
        h.run()
    assert caught.value.run["model_usd"] == pytest.approx(0.02 + 0.0135 + SEARCH_EMBED_USD)
    assert not any("research" in n.lower() for n in caught.value.run["notices"])


REFUSED_ROUTE_INPUTS = [
    {"platform": "google_trends", "endpoint": "interest", "params": {"q": "amapiano"}, "max_credits": 2},
    {"platform": "google_trends", "endpoint": "interest/over-time", "max_credits": 2},
    {"platform": "prism", "endpoint": "trend-board", "max_credits": 2},
    {"platform": "tiktok", "endpoint": "user/audience", "max_credits": 2},
    {"platform": "tiktok", "endpoint": "never/listed", "max_credits": 2},
]


@pytest.mark.parametrize("tool_input", REFUSED_ROUTE_INPUTS)
def test_a_refused_route_gets_the_generic_step_and_is_never_named(tool_input):
    kind, text, platform = ask.describe_tool("mcp__f42__socialcrawl_call", tool_input, progress())
    assert text == "Searching a live source" and platform is None
    assert tool_input["platform"] not in text and tool_input["endpoint"] not in text


def allowed_route():
    """A route the guard allows today, taken from its own list so this test follows the list."""
    from core.agent.tools.socialcrawl import ALLOWED_ROUTES, SEARCH_EVERYWHERE

    route = sorted(r for r in ALLOWED_ROUTES if r != SEARCH_EVERYWHERE and not r.startswith("google"))[0]
    platform, endpoint = route.split("/", 1)
    return route, {"platform": platform, "endpoint": endpoint, "max_credits": 2}


def test_an_allowed_route_still_shows_in_the_step():
    route, tool_input = allowed_route()
    _, text, platform = ask.describe_tool("mcp__f42__socialcrawl_call", tool_input, progress())
    assert route in text and platform == tool_input["platform"]


def test_a_step_line_that_breaches_falls_back_to_the_generic_line(monkeypatch):
    route, tool_input = allowed_route()
    monkeypatch.setattr(ask, "_text_breaches", lambda text: ["a breach"] if route in str(text) else [])
    _, text, platform = ask.describe_tool("mcp__f42__socialcrawl_call", tool_input, progress())
    assert text == "Searching a live source" and platform is None


def test_a_refused_google_trends_call_never_reaches_the_event_stream():
    events = research_error_events(None, "socialcrawl_call", {"platform": "google_trends", "endpoint": "interest",
                                                              "params": {"q": "amapiano"}, "max_credits": 2})
    steps = [e for e in events if e["event"] == "step"]
    assert len(steps) == 2 and steps[0]["text"] == "Searching a live source"
    shown = json.dumps(events).lower()
    assert "google" not in shown and "trends" not in shown and "interest" not in shown


def test_a_cut_reason_that_would_breach_gives_the_rule_wording_alone():
    claim = {"id": "c9", "text": "x", "evidence_ids": ["tt_1", "gen z fan page"]}
    stored = {"tt_1": {"id": "tt_1"}, "gen z fan page": {"id": "gen z fan page"}}
    reason = ask._cut_reason(claim, [{"claim_id": "c9", "rule": "K1", "verdict": "cut"}], stored, {})
    what, why = checks.RULE_GAPS["K1"]
    assert reason == f"{what} (K1): {why}"
    assert checks._text_breaches(reason) == []


def test_a_clean_cut_reason_still_names_what_was_checked():
    claim = {"id": "c9", "text": "x", "evidence_ids": ["tt_1"]}
    assert ask._cut_reason(claim, [{"rule": "K1", "verdict": "cut"}], {"tt_1": {"id": "tt_1"}}, {}).endswith(
        "checked stored evidence tt_1")


def test_the_recheck_is_called_directly_so_its_absence_is_never_skipped(monkeypatch):
    monkeypatch.delattr(checks, "recheck_fields")
    with pytest.raises(AttributeError):
        Harness().run()


def one_t1_hold_left():
    """Spend where one T1 hold fits, but a second T1 hold beside it would cross the cap."""
    return current_model_cap() - 2 * hold() + 0.01


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_two_concurrent_asks_the_first_hold_pushes_the_second_over_the_cap():
    import threading

    inside, release = threading.Event(), threading.Event()
    base = make_research()
    held = {}

    def slow(*args):
        held.update(ask._IN_FLIGHT)
        inside.set()
        assert release.wait(5)
        return base(*args)

    first = Harness(research=slow, spent=one_t1_hold_left)
    result = {}
    worker = threading.Thread(target=lambda: result.update(out=first.run()))
    worker.start()
    try:
        assert inside.wait(5)
        second = Harness(spent=one_t1_hold_left).run()  # the first ask's T1 hold leaves room only for T0
    finally:
        release.set()
        worker.join(5)
    assert list(held.values()) == [pytest.approx(hold())]
    assert result["out"]["run"]["tier"] == "T1"
    assert second["run"]["tier"] == "T0"
    assert any("USD 20" in n for n in second["run"]["notices"])
    assert Harness(spent=one_t1_hold_left).run()["run"]["tier"] == "T1"  # both holds released once settled
    assert ask._IN_FLIGHT == {}


def test_a_failed_ask_releases_its_hold():
    def research(ctx, prompt, options, emit, should_stop):
        raise ConnectionError("agent stream dropped")

    with pytest.raises(ConnectionError):
        Harness(research=research, spent=one_t1_hold_left).run()
    assert Harness(spent=one_t1_hold_left).run()["run"]["tier"] == "T1"


def test_a_slow_spend_read_does_not_block_another_ask_releasing_its_hold():
    import threading

    inside, release, first_done = threading.Event(), threading.Event(), threading.Event()
    base = make_research()

    def slow_research(*args):
        inside.set()
        assert release.wait(5)
        return base(*args)

    first = Harness(research=slow_research, spent=lambda: 5.0)
    result = {}

    def run_first():
        result["out"] = first.run()
        first_done.set()

    worker = threading.Thread(target=run_first)
    worker.start()
    seen = {}

    def slow_reader():
        release.set()  # the first ask finishes while this read is still going
        seen["first_finished_during_read"] = first_done.wait(2)
        return 5.0

    try:
        assert inside.wait(5)
        second = Harness(spent=slow_reader).run()
    finally:
        release.set()
        worker.join(5)
    assert seen["first_finished_during_read"] is True
    assert result["out"]["run"]["tier"] == "T1" and second["run"]["tier"] == "T1"
    assert ask._IN_FLIGHT == {}


def no_room_beside_a_finished_ask():
    """Spend read before a finished ask's runs row lands: one T1 hold fits only while that ask's spend is left out."""
    return current_model_cap() - hold() - 0.001


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_a_finished_asks_spend_counts_until_its_runs_row_is_recorded():
    first = Harness(spent=lambda: 0.0).run()
    assert first["run"]["model_usd"] > 0.001
    # Nothing has written the first ask's runs row yet, so the spend read cannot include it.
    second = Harness(spent=no_room_beside_a_finished_ask).run(ask_id="a_20260928_second")
    assert second["run"]["tier"] == "T0"
    ask.recorded("a_20260928_test")
    ask.recorded("a_20260928_second")
    assert Harness(spent=no_room_beside_a_finished_ask).run(ask_id="a_20260928_third")["run"]["tier"] == "T1"
    assert ask._IN_FLIGHT == {}


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_an_ask_finishing_during_a_spend_read_still_counts_against_that_read():
    import threading

    inside, release, first_done = threading.Event(), threading.Event(), threading.Event()
    base = make_research()

    def slow_research(*args):
        inside.set()
        assert release.wait(5)
        return base(*args)

    first = Harness(research=slow_research, spent=lambda: 0.0)
    result = {}

    def run_first():
        result["out"] = first.run()
        first_done.set()

    worker = threading.Thread(target=run_first)
    worker.start()

    def stale_reader():
        release.set()  # the first ask finishes while this read is still going, its runs row not yet written
        assert first_done.wait(5)
        return no_room_beside_a_finished_ask()

    try:
        assert inside.wait(5)
        second = Harness(spent=stale_reader).run(ask_id="a_20260928_second")
    finally:
        release.set()
        worker.join(5)
    assert result["out"]["run"]["tier"] == "T1"
    assert second["run"]["tier"] == "T0"


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_a_runs_row_recorded_during_a_spend_read_still_counts_against_that_read():
    Harness(spent=lambda: 0.0).run()

    def stale_reader():
        ask.recorded("a_20260928_test")  # the row lands after this read began, so the read may not include it
        return no_room_beside_a_finished_ask()

    assert Harness(spent=stale_reader).run(ask_id="a_20260928_second")["run"]["tier"] == "T0"
    ask.recorded("a_20260928_second")
    # A read that starts after the row landed sees it, so nothing else is added.
    assert Harness(spent=no_room_beside_a_finished_ask).run(ask_id="a_20260928_third")["run"]["tier"] == "T1"


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_an_unrecorded_asks_spend_stops_counting_on_another_day():
    Harness(spent=lambda: 0.0).run()
    tomorrow = Harness(spent=no_room_beside_a_finished_ask, now=NOW + timedelta(days=1))
    assert tomorrow.run(ask_id="a_20260929_second")["run"]["tier"] == "T1"


# Round 7 (task 1.11): the model's demographic net, quoted age words, model input in steps, and the embed spend

YOUTH_POST = post("x_3", "x", "@kasi_voice", "Our youths deserve better than this, amapiano on every corner", 27)
YOUTH_CLAIM = {"text": 'A post on X says "Our youths deserve better than this" about amapiano.', "evidence_ids": ["x_3"],
               "quotes": [{"evidence_id": "x_3", "text": "Our youths deserve better than this"}]}


class YouthWarehouse(FakeWarehouse):
    def run(self, sql, params, max_bytes_billed):
        rows = super().run(sql, params, max_bytes_billed)
        return rows if "COUNT(" in sql else rows + [copy.deepcopy(YOUTH_POST)]


@pytest.mark.parametrize("check", [None, fake_check], ids=["real_checks", "stub_checks"])
def test_a_surviving_claim_quoting_an_age_word_from_its_post_is_shown_with_its_text(check):
    h = Harness(check=check, model=ExtraClaimModel(YOUTH_CLAIM))
    h.warehouse = h.deps.warehouse = YouthWarehouse()
    out = h.run()
    assert "c5" in [c["id"] for c in out["answer"]["claims"]]
    (event,) = [e for e in h.events if e["event"] == "claim" and e["claim"]["id"] == "c5"]
    assert event["check"] == "verified"
    assert event["claim"]["text"] == YOUTH_CLAIM["text"]
    assert event["claim"]["quotes"] == YOUTH_CLAIM["quotes"]


def test_withheld_scans_the_claim_against_its_cited_records():
    claim = {"id": "c5", **YOUTH_CLAIM}
    record = {"id": "x_3", "text": YOUTH_POST["text"]}
    assert ask._withheld(claim, [], [record])["text"] == YOUTH_CLAIM["text"]
    assert ask._withheld(claim, [])["text"] == WITHHELD
    assert ask._withheld({**claim, "text": "Our youths love amapiano."}, [], [record])["text"] == WITHHELD


class DemographicModel(FakeModel):
    """The support checker flags c3 as demographic inference; the word list sees nothing in it."""

    def complete_json(self, **kwargs):
        out, usage = super().complete_json(**kwargs)
        if kwargs["schema"] is SUPPORT_SCHEMA:
            flagged = "Durban amapiano dance challenge" in kwargs["user"]
            out = {**out, "demographic_inference": flagged,
                   "reason": "it reads the dancers as salaried" if flagged else out["reason"]}
        return out, usage


def test_a_claim_the_model_flags_as_demographic_is_cut_and_withheld_in_the_event_stream():
    h = Harness(model=DemographicModel())
    out = h.run()
    assert "c3" not in [c["id"] for c in out["answer"]["claims"]]
    (event,) = [e for e in h.events if e["event"] == "claim" and e["claim"]["id"] == "c3"]
    assert event["check"] == "cut"
    assert event["claim"]["text"] == WITHHELD and event["claim"]["quotes"] == []
    assert "(K6)" in event["reason"]
    dumped = json.dumps([h.events, h.tables.inserts, out], default=str)
    assert "salaried" not in dumped and "Durban amapiano dance challenge is spreading" not in json.dumps(event)
    assert ("c3", "K6", "cut", "model") in {(r["claim_id"], r["rule"], r["verdict"], r["checker"])
                                           for r in h.tables.inserts[0][1]}


@pytest.mark.parametrize("name, tool_input, generic", [
    ("mcp__f42__sql_query", {"sql": "SELECT 1", "purpose": "posts rose 340% for amapiano"}, "Counting in the warehouse"),
    ("mcp__f42__sql_query", {"sql": "SELECT 1", "purpose": "amapiano in 12 markets"}, "Counting in the warehouse"),
    ("mcp__f42__search_posts", {"query": "amapiano 2019"}, "Searching stored posts"),
    ("mcp__f42__recall_findings", {"query": "amapiano 3x growth"}, "Checking saved findings"),
])
def test_a_step_line_from_model_input_with_an_unpinned_number_gets_the_generic_line(name, tool_input, generic):
    kind, text, platform = ask.describe_tool(name, tool_input, progress())
    assert text == generic and platform is None


def test_a_step_line_from_clean_model_input_still_shows_it():
    _, text, _ = ask.describe_tool("mcp__f42__sql_query", {"sql": "SELECT 1", "purpose": "authors for amapiano"},
                                   progress())
    assert text == "Counting in the warehouse: authors for amapiano"
    _, text, _ = ask.describe_tool("mcp__f42__search_posts", {"query": "amapiano"}, progress())
    assert "'amapiano'" in text and "22 to 28 September" in text


def test_the_run_model_usd_includes_the_search_query_embeddings():
    long_query = "amapiano log drum soweto braai taxi rank durban"
    base = make_research()

    def research(ctx, prompt, options, emit, should_stop):
        result = base(ctx, prompt, options, emit, should_stop)
        search_posts(ctx, options.warehouse, long_query)
        return result

    run = Harness(research=research).run()["run"]
    embeds = SEARCH_EMBED_USD + query_embed_usd(long_query)
    calls = 3 + 1 + 1  # three support calls, one K4 rewrite and one field check
    assert run["model_usd"] == pytest.approx(0.02 + 0.0135 + calls * 0.0006 + embeds)
    assert run["model_usd"] - (0.02 + 0.0135 + calls * 0.0006) == pytest.approx(embeds)


# Round 8 (task 1.11): resolve_dates input and tool error text in steps, and the field-level model net

def test_a_resolve_dates_step_with_an_unpinned_number_gets_the_generic_line():
    assert ask.MODEL_INPUT["resolve_dates"] == "expression"
    _, text, platform = ask.describe_tool("mcp__f42__resolve_dates", {"expression": "since posts rose 340%"},
                                          progress())
    assert text == "Working out the dates" and platform is None
    _, text, _ = ask.describe_tool("mcp__f42__resolve_dates", {"expression": "this week"}, progress())
    assert text == "Working out the dates for 'this week'"


def research_error_events(error_text, tool, tool_input):
    """One tool call through the Gemini research loop. With error_text the call comes back as that error; with None
    it runs through the loop's own schema check and guard. Returns the events."""
    from core.agent import gemini_research
    from core.agent.context import RunContext

    client = FakeGenaiClient(gemini_reply(gemini_part(tool, **tool_input)), gemini_reply(gemini_part(text="ok")))
    events = []
    p = ask.Progress(events.append, lambda: NOW, market_label="South Africa",
                     window=(date(2026, 9, 22), date(2026, 9, 28)))
    ctx = RunContext(run_id="r_test", tier="T1", as_of=NOW, market="ZA")
    setup = ask.ResearchSetup(system_prompt="s", model="gemini-3.8-flash", warehouse=None, client=None, tables=None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gemini_research, "client_factory", lambda: client)
        if error_text is not None:
            mp.setattr(gemini_research, "_run_call", lambda ctx, functions, name, args: (error_text, True))
        gemini_research.gemini_research(ctx, "q", setup, p, lambda: False)
    return events


def consume_error(error_text, tool="sql_query", tool_input=None):
    tool_input = {"sql": "SELECT 1", "purpose": "authors for amapiano"} if tool_input is None else tool_input
    events = research_error_events(error_text, tool, tool_input)
    return [e["text"] for e in events if e["event"] == "step" and e["kind"] == "note"]


@pytest.mark.parametrize("error_text", ["refused: posts rose 340% in 12 markets", "refused: gen z slang is not a term",
                                        "refused: google trends is not a source"])
def test_tool_error_text_with_a_figure_or_breach_shows_the_generic_error_line(error_text):
    notes = consume_error(error_text)
    assert notes == ["A warehouse count did not run"]


def test_clean_tool_error_text_stays_out_of_the_step():
    # The error is written for the model and names tables and tools, so a strategist sees only the plain line.
    assert consume_error("refused: the table is outside the allowed datasets") == ["A warehouse count did not run"]


PROBE_CLAIM = "Newlyweds on X and TikTok made 6 amapiano posts from 5 creators."
PROBE_HEADLINE = "Newlyweds and new graduates lead the ANC 2027 talk, and women frame it as a race."
PROBE_CONTEXT = "Mostly women and newlyweds."
PROBE_SO_WHAT = "Speak to students and new mums with a race framing."
CLEAN_SO_WHAT = "Taxi rank speakers are a place to be heard."
FIELDS_USAGE = {"input_tokens": 700, "output_tokens": 45, "usd": 0.0042}


class ProbeModel(FakeModel):
    """The reviewer's round-8 probe: support flags c1 as demographic; the field check flags what flag_fields names."""

    def __init__(self, flag_fields=True):
        super().__init__()
        self.flag_fields = flag_fields

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is FIELDS_SCHEMA:
            self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
            items = re.findall(r"item (\d+) \(.*?\)\n<untrusted_content>\n(.*?)\n</untrusted_content>", user, re.S)
            flag = re.compile(r"newlywed|women|mums", re.I)
            return {"fields": [{"index": int(i), "demographic_inference": self.flag_fields and bool(flag.search(t)),
                                "forecast_assertion": False, "country_people": [], "so_what_supported": True}
                               for i, t in items]}, dict(FIELDS_USAGE)
        out, usage = super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)
        if schema is WRITER_SCHEMA:
            out["claims"][0]["text"] = PROBE_CLAIM
            out["claims"][1]["text"] = "People say amapiano is everywhere."  # passes support, so c2 and c3 stand
            out.update(short_answer=PROBE_HEADLINE, context=PROBE_CONTEXT,
                       so_what=[{"text": CLEAN_SO_WHAT, "claim_ids": ["c2"]},
                                {"text": PROBE_SO_WHAT, "claim_ids": ["c3"]}])
        elif schema is SUPPORT_SCHEMA:
            out = {**out, "demographic_inference": "Newlyweds" in user}
        return out, usage


def test_the_round_8_probe_fields_are_removed_from_the_answer_events_and_rows():
    h = Harness(model=ProbeModel())
    out = h.run()
    answer = out["answer"]
    assert "c1" not in [c["id"] for c in answer["claims"]]
    assert answer["status"] in ("partial", "insufficient_evidence")
    assert answer["short_answer"] in ("", checks.INSUFFICIENT) and answer["context"] == ""
    assert [s["text"] for s in answer["so_what"]] == [CLEAN_SO_WHAT]
    dumped = json.dumps([h.events, h.tables.inserts, out], default=str).lower()
    for removed in (PROBE_HEADLINE, PROBE_CONTEXT, PROBE_SO_WHAT, PROBE_CLAIM, "newlywed", "women", "mums",
                    "graduates"):
        assert removed.lower() not in dumped
    rows = {(r["claim_id"], r["rule"], r["verdict"], r["checker"]) for r in h.tables.inserts[0][1]}
    assert {("short_answer", "K6", "cut", "model"), ("context", "K6", "cut", "model"),
            ("so_what/1", "K6", "cut", "model")} <= rows
    assert ("so_what/0", "K6", "cut", "model") not in rows
    assert validate_answer(answer) == []


def test_without_the_field_flags_the_probe_fields_pass_every_other_check():
    h = Harness(model=ProbeModel(flag_fields=False))
    answer = h.run()["answer"]
    assert "c1" not in [c["id"] for c in answer["claims"]]
    assert answer["short_answer"] == PROBE_HEADLINE and answer["context"] == PROBE_CONTEXT
    assert [s["text"] for s in answer["so_what"]] == [CLEAN_SO_WHAT, PROBE_SO_WHAT]


def test_the_field_check_runs_once_after_the_recheck_and_its_spend_counts_toward_the_run():
    h = Harness(model=ProbeModel())
    run = h.run()["run"]
    fields = [c for c in h.model.calls if c["schema"] is FIELDS_SCHEMA]
    support = [c for c in h.model.calls if c["schema"] is SUPPORT_SCHEMA]
    assert len(fields) == 1 and fields[0]["max_tokens"] <= ask.FIELD_MAX_TOKENS
    assert fields[0]["model"] == ask.MODEL
    assert run["model_usd"] == pytest.approx(0.02 + 0.0135 + len(support) * 0.0006 + FIELDS_USAGE["usd"]
                                             + SEARCH_EMBED_USD)
    assert run["tokens"]["input"] >= FIELDS_USAGE["input_tokens"]


def test_a_field_check_failure_carries_its_billed_usage_into_the_run():
    class FailingFields(ProbeModel):
        def complete_json(self, **kwargs):
            if kwargs["schema"] is FIELDS_SCHEMA:
                error = RuntimeError("gemini-3.8-flash hit max_tokens=400; the JSON is truncated")
                error.usage = dict(FIELDS_USAGE)
                raise error
            return super().complete_json(**kwargs)

    h = Harness(model=FailingFields())
    with pytest.raises(RuntimeError, match="max_tokens") as caught:
        h.run()
    support = [c for c in h.model.calls if c["schema"] is SUPPORT_SCHEMA]
    assert caught.value.run["model_usd"] == pytest.approx(0.02 + 0.0135 + len(support) * 0.0006
                                                          + FIELDS_USAGE["usd"] + SEARCH_EMBED_USD)


# Round 9 (task 1.11): the draft's own gaps in the field check, cut claims never shown, and the tone net

PROBE_GAP = {"what": "What newlyweds and new graduates think of the 2027 election",
             "searched": "stored posts and live sources", "why": "no post in the pack speaks to it"}


class GapProbeModel(FakeModel):
    """The writer adds the reviewer's round-9 gap; the field check flags any item naming newlyweds."""

    def __init__(self, flag_fields=True):
        super().__init__()
        self.flag_fields = flag_fields

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is FIELDS_SCHEMA:
            self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
            items = re.findall(r"item (\d+) \((\S+?)[;)].*?\n<untrusted_content>\n(.*?)\n</untrusted_content>", user,
                               re.S)
            self.sent = [where for _, where, _ in items]
            return {"fields": [{"index": int(i), "demographic_inference": self.flag_fields and "newlywed" in t.lower(),
                                "forecast_assertion": False, "country_people": [], "so_what_supported": True}
                               for i, _, t in items]}, dict(FIELDS_USAGE)
        out, usage = super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)
        if schema is WRITER_SCHEMA:
            out["gaps"] = [dict(PROBE_GAP)]
        return out, usage


def test_without_the_field_flag_the_round_9_gap_reaches_the_answer_and_the_followups():
    out = Harness(model=GapProbeModel(flag_fields=False)).run()
    assert PROBE_GAP in out["answer"]["gaps"]
    assert any("newlywed" in f for f in out["run"]["followups"])


def test_the_round_9_gap_the_field_check_flags_is_gone_from_the_answer_run_and_events():
    h = Harness(model=GapProbeModel())
    out = h.run()
    answer, run = out["answer"], out["run"]
    assert PROBE_GAP not in answer["gaps"]
    assert any(g["why"].startswith("field check: ") for g in answer["gaps"])
    dumped = json.dumps([h.events, h.tables.inserts, out], default=str).lower()
    for removed in ("newlywed", "graduates", "2027 election"):
        assert removed not in dumped
    assert 1 <= len(run["followups"]) <= 3
    rows = {(r["claim_id"], r["rule"], r["verdict"], r["checker"]) for r in h.tables.inserts[0][1]}
    assert ("gaps/0", "K6", "cut", "model") in rows
    (fields,) = [c for c in h.model.calls if c["schema"] is FIELDS_SCHEMA]
    assert h.model.sent.count("gaps/0") == 1 and not any(w.startswith("gaps/") and w != "gaps/0" for w in h.model.sent)
    assert fields["max_tokens"] == 400 + 40 * len(h.model.sent) <= ask.FIELD_MAX_TOKENS
    assert validate_answer(answer) == []


CLEAN_GAP = {"what": "Whether amapiano Sundays reach Durban", "searched": "stored posts", "why": "no Durban posts"}
THREADS_GAP = {"what": "No live Threads data", "searched": "threads/search, 22 to 28 September", "why": "rate_limited"}


class GateTextModel(GapProbeModel):
    """The writer adds a clean gap, the round-9 gap and six so_what items, so the gate writes its own fixed gaps."""

    def complete_json(self, *, system, user, schema, model, max_tokens):
        out, usage = super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)
        if schema is WRITER_SCHEMA:
            out["gaps"] = [dict(CLEAN_GAP), dict(PROBE_GAP)]
            out["so_what"] = [dict(WRITER_OUT["so_what"][0]) for _ in range(6)]
        return out, usage


@pytest.mark.parametrize("check", [None, fake_check], ids=["real_checks", "stub_checks"])
def test_no_followup_is_built_from_a_gate_text(check):
    from core.agent import writer
    out = Harness(check=check, model=GateTextModel()).run()
    gaps, followups = out["answer"]["gaps"], out["run"]["followups"]
    gate = [g for g in gaps if g not in (CLEAN_GAP, THREADS_GAP)]
    assert plain.gap(writer.DROPPED_ITEMS_GAP) in gate and plain.gap(writer.DROPPED_DRAFT_GAP) in gate
    # The gate's own gaps reach the reader in plain words: "Claim c2 removed: ... (K6)" shows as "A claim was removed".
    assert any(g["what"].startswith("A claim was removed") for g in gate)
    if check is None:
        assert any(g["what"].startswith("A claim was cut") for g in gate)
    assert sorted(followups) == sorted(["What does Threads show once threads/search answers again?",
                                        f"Can 42 fill this gap: {CLEAN_GAP['what']}?"])
    for gap in gate:
        for followup in followups:
            assert gap["what"].rstrip(".")[:80] not in followup


def test_followups_come_only_from_the_gaps_they_are_allowed():
    status = [{"platform": "threads", "route": "threads/search", "status": "rate_limited", "items": 0}]
    gate = [{"what": "Claim c2 cut: a quote not found in its posts (K8)", "searched": "stored evidence tt_1",
             "why": "every quoted span must be verbatim in a cited post"}]
    assert ask._followups(gate + [THREADS_GAP], status, allowed=[THREADS_GAP]) == [
        "What does Threads show once threads/search answers again?"]
    assert ask._followups(gate, status, allowed=[]) == []


def test_a_followup_is_cut_on_a_word_boundary_and_never_ends_in_an_age_term():
    what = "Whether the heritage walks in Orlando East and Kliptown now pull in Soweto Gen Zambia tours"
    assert what[:80].endswith("Soweto Gen Z") and not checks._text_breaches(what)
    gap = {"what": what, "searched": "stored posts", "why": "no Soweto posts"}
    (followup,) = ask._followups([gap], [], allowed=[gap])
    assert followup == "Can 42 fill this gap: Whether the heritage walks in Orlando East and Kliptown now pull in Soweto Gen?"
    assert not checks._text_breaches(followup)


@pytest.mark.parametrize("what", ["Why Gen Z fans skip the braai", "Why 3 venues in Soweto closed"])
def test_a_followup_that_would_breach_or_carry_an_unpinned_figure_is_dropped(what):
    gap = {"what": what, "searched": "stored posts", "why": "no posts"}
    assert ask._followups([gap, CLEAN_GAP], [], allowed=[gap, CLEAN_GAP]) == [
        f"Can 42 fill this gap: {CLEAN_GAP['what']}?"]


def test_a_stopped_run_still_offers_to_run_again():
    flag = {"stop": False}
    h = Harness(research=make_research(stop_flag=flag), stop_flag=flag)
    run = h.run()["run"]
    assert "Can you run this question again to the end?" in run["followups"]


K1_PROBE = {"text":"Newlyweds and new mums asked for amapiano at the braai.", "evidence_ids": ["x_1"],
            "quotes": [{"evidence_id": "x_1", "text": "asked for amapiano at the braai"}]}


@pytest.mark.parametrize("check", [None, fake_check], ids=["real_checks", "stub_checks"])
def test_a_k1_cut_claim_is_withheld_in_its_event_and_never_shown(check):
    h = Harness(check=check, model=ExtraClaimModel(K1_PROBE))
    out = h.run()
    (event,) = [e for e in h.events if e["event"] == "claim" and e["claim"]["id"] == "c5"]
    assert event["check"] == "cut"
    assert event["claim"]["text"] == "Claim withheld by the trust gate (K1)"
    assert event["claim"]["quotes"] == [] and event["claim"]["numbers"] == []
    assert event["claim"]["evidence_ids"] == ["x_1"]
    dumped = json.dumps([h.events, h.tables.inserts, out], default=str).lower()
    for removed in ("newlywed", "mums", K1_PROBE["text"].lower()):
        assert removed not in dumped


def test_no_cut_claim_shows_its_draft_text_in_any_event_whatever_the_rule():
    h = Harness()
    h.run()
    cut = [e for e in h.events if e["event"] == "claim" and e["check"] == "cut"]
    assert {e["claim"]["id"] for e in cut} == {"c2", "c4"}  # c2 by the support check (K4), c4 by K1
    shown = {e["claim"]["id"]: e["claim"] for e in cut}
    assert shown["c2"]["text"] == "Claim withheld by the trust gate (K4)"
    assert shown["c4"]["text"] == "Claim withheld by the trust gate (K1)"
    dumped = json.dumps([h.events, h.tables.inserts], default=str)
    for claim in WRITER_OUT["claims"]:
        if claim["id"] in shown:
            assert claim["text"] not in dumped
            assert shown[claim["id"]]["evidence_ids"] == claim["evidence_ids"]
            assert shown[claim["id"]]["quotes"] == [] and shown[claim["id"]]["numbers"] == []
    verified = [e for e in h.events if e["event"] == "claim" and e["check"] != "cut"]
    assert {e["claim"]["text"] for e in verified} == {WRITER_OUT["claims"][0]["text"], WRITER_OUT["claims"][2]["text"]}


PIDGIN_POST = post("ng_1", "tiktok", "@lagos.gist", "Abeg this amapiano don tire us, wetin we go dance #amapiano", 27)
PIDGIN_POST["geo_market"] = "NG"
TONE_PROBE = {"text": "Lagos posters slammed the amapiano wave on TikTok and X.", "label": "observed",
              "evidence_ids": ["ng_1", "x_1"]}


class PidginWarehouse(FakeWarehouse):
    def run(self, sql, params, max_bytes_billed):
        rows = super().run(sql, params, max_bytes_billed)
        return rows if "COUNT(" in sql else rows + [copy.deepcopy(PIDGIN_POST)]


class ToneModel(ExtraClaimModel):
    """The support checker marks the claim that says posters slammed amapiano as a tone claim."""

    def complete_json(self, **kwargs):
        out, usage = super().complete_json(**kwargs)
        if kwargs["schema"] is SUPPORT_SCHEMA:
            out = {**out, "tone_claim": "slammed" in kwargs["user"]}
        return out, usage


def test_a_tone_claim_the_model_flags_on_a_pidgin_post_is_shown_downgraded_with_a_model_k5_row():
    assert not checks.is_tone_claim(TONE_PROBE["text"])
    h = Harness(model=ToneModel(TONE_PROBE))
    h.warehouse = h.deps.warehouse = PidginWarehouse()
    out = h.run()
    (claim,) = [c for c in out["answer"]["claims"] if c["id"] == "c5"]
    assert claim["label"] == "single_source"
    (event,) = [e for e in h.events if e["event"] == "claim" and e["claim"]["id"] == "c5"]
    assert event["check"] == "downgraded" and event["reason"] == "label lowered to single_source (K5)"
    assert event["claim"]["text"] == TONE_PROBE["text"]
    rows = {(r["claim_id"], r["rule"], r["verdict"], r["checker"]) for r in h.tables.inserts[0][1]}
    assert ("c5", "K5", "downgrade", "model") in rows


# Round 10 (task 1.11): an id the model invented never reaches a gap, a claim event or a logged row

PROBE_IDS = ["x_z2", "teen_voters_1", "Gen Z voters", "kids_2", "students_1"]
PROBE_QUERY_IDS = ["q_Gen_Z_voters", "youth_posts"]
ID_PROBE = {"text": "Everyone at the braai asked for amapiano.", "evidence_ids": ["x_1", *PROBE_IDS],
            "numbers": [{"value": 3, "unit": "posts", "query_id": q} for q in PROBE_QUERY_IDS]}


def test_claim_check_rows_count_unresolved_refs_in_code_reasons():
    h = Harness(check=None, model=ExtraClaimModel(ID_PROBE))
    h.run()
    rows = [row for _, inserted in h.tables.inserts for row in inserted if row["claim_id"] == "c5"]
    by_rule = {row["rule"]: row for row in rows}

    assert by_rule["K1"]["reason"] == (
        "a citation that does not match its stored post (K1): every evidence id must resolve to a stored post, "
        "and every quote must be verbatim in it, checked stored evidence x_1 and 5 ids that do not resolve")
    assert by_rule["K2"]["reason"] == (
        "a number with no query behind it (K2): every number must come from a query recorded in this run and come "
        "back the same on re-run, checked recorded queries: 2 ids that do not resolve")
    logged_reasons = json.dumps([row["reason"] for row in rows], default=str).lower()
    assert all(invented.lower() not in logged_reasons for invented in PROBE_IDS + PROBE_QUERY_IDS)


def test_invented_evidence_and_query_ids_never_reach_the_answer_the_events_or_the_rows():
    h = Harness(check=None, model=ExtraClaimModel(ID_PROBE))
    out = h.run()
    (event,) = [e for e in h.events if e["event"] == "claim" and e["claim"]["id"] == "c5"]
    assert event["check"] == "cut"
    assert event["claim"]["evidence_ids"] == ["x_1"] and "unresolved_ids" not in event["claim"]
    assert "5 ids that do not resolve" in event["reason"] and "2 ids that do not resolve" in event["reason"]
    assert any(g["searched"] == "stored evidence x_1 and 5 ids that do not resolve" for g in out["answer"]["gaps"])
    assert any(g["searched"] == "recorded queries: 2 ids that do not resolve" for g in out["answer"]["gaps"])
    dumped = json.dumps([h.events, h.tables.inserts, out], default=str)
    for invented in PROBE_IDS + PROBE_QUERY_IDS:
        assert invented not in dumped


@pytest.mark.parametrize("records", [[], None, [{"id": "tt_1", "text": "amapiano"}]])
def test_withheld_keeps_only_evidence_ids_that_resolve_and_a_placeholder_when_none_does(records):
    claim = {"id": "c9", "text": "x", "evidence_ids": ["x_z2", "Gen Z voters"], "quotes": [], "numbers": []}
    rows = [{"claim_id": "c9", "rule": "K1", "verdict": "cut"}]
    shown = ask._withheld(claim, rows, records, cut=True)
    assert shown["evidence_ids"] == [ask.UNRESOLVED] and set(shown) == set(claim)
    assert "x_z2" not in json.dumps(shown) and "Gen Z" not in json.dumps(shown)


@pytest.mark.parametrize("cited", [["x_z2", "Gen Z voters"], [3], [], ["tt_1", "kids_2"]])
def test_a_cut_claim_event_keeps_the_rubric_claim_shape(cited):
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).parents[2] / "eval" / "answer.schema.json").read_text(encoding="utf-8"))
    claim = {"id": "c9", "text": "Teens at the braai.", "label": "observed", "kind": "observation",
             "evidence_ids": cited, "quotes": [], "numbers": []}
    rows = [{"claim_id": "c9", "rule": "K1", "verdict": "cut"}]
    shown = ask._withheld(claim, rows, [{"id": "tt_1", "text": "amapiano"}], cut=True)
    Draft202012Validator({"$ref": "#/$defs/claim", "$defs": schema["$defs"]}).validate(shown)


def test_withheld_keeps_resolved_ids_in_order_and_adds_no_count_when_all_resolve():
    records = [{"id": "tt_1", "text": "amapiano"}, {"id": "x_1", "text": "braai"}]
    claim = {"id": "c9", "text": "Amapiano at the braai.", "evidence_ids": ["x_1", "tt_1"], "quotes": [], "numbers": []}
    shown = ask._withheld(claim, [], records)
    assert shown == claim
    mixed = ask._withheld({**claim, "evidence_ids": ["x_1", "kids_2", "tt_1", {"id": "teen"}]}, [], records)
    assert mixed["evidence_ids"] == ["x_1", "tt_1"] and set(mixed) == set(claim)


def test_a_cut_reason_names_only_the_ids_that_resolve():
    claim = {"id": "c9", "text": "x", "evidence_ids": ["tt_1", "teen_voters_1", "kids_2"],
             "numbers": [{"query_id": "youth_posts"}]}
    rows = [{"rule": "K1", "verdict": "cut"}, {"rule": "K2", "verdict": "cut"}]
    reason = ask._cut_reason(claim, rows, {"tt_1": {"id": "tt_1"}}, {})
    assert "checked stored evidence tt_1 and 2 ids that do not resolve" in reason
    assert "checked recorded queries: 1 id that does not resolve" in reason
    assert "teen" not in reason and "kids" not in reason and "youth" not in reason


# Review minors (task 1.11): gap allowances in the follow-ups, and a placeholder no post id can take

SKILL_GAP = {"what": "Instagram was not searched at T1", "searched": "stored posts", "why": "not needed at T1"}
ELECTION_GAP = {"what": "How the 2027 election plays in Soweto taxi ranks", "searched": "stored posts",
                "why": "no Soweto posts"}
ELECTION_FOLLOWUP = f"Can 42 fill this gap: {ELECTION_GAP['what']}?"


def test_a_gap_the_gate_allows_keeps_its_followup_like_the_skill_example():
    assert SKILL_GAP["what"] in skills.load_skill("culture-read")
    assert checks._gap_problems(SKILL_GAP, [], checks._allowance(None, NOW)) == {}
    assert ask._followups([SKILL_GAP], [], allowed=[SKILL_GAP]) == [f"Can 42 fill this gap: {SKILL_GAP['what']}?"]


def test_a_followup_keeps_an_event_year_only_with_the_gate_allowance():
    window = (date(2026, 8, 30), date(2026, 9, 28))
    allow = checks._allowance(window, NOW)
    assert ask._followups([ELECTION_GAP], [], allowed=[ELECTION_GAP], allow=allow) == [ELECTION_FOLLOWUP]
    assert ask._followups([ELECTION_GAP], [], allowed=[ELECTION_GAP]) == []


class ElectionGapModel(FakeModel):
    def complete_json(self, *, system, user, schema, model, max_tokens):
        out, usage = super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)
        if schema is WRITER_SCHEMA:
            out["gaps"] = [dict(ELECTION_GAP)]
        return out, usage


@pytest.mark.parametrize("check", [None, fake_check], ids=["real_checks", "stub_checks"])
def test_run_ask_keeps_the_event_year_followup_the_gate_allowed(check):
    out = Harness(check=check, model=ElectionGapModel()).run()
    assert ELECTION_GAP in out["answer"]["gaps"]
    assert ELECTION_FOLLOWUP in out["run"]["followups"]


def test_the_placeholder_is_no_post_id_so_a_post_called_unresolved_is_not_mistaken_for_it():
    records = [{"id": "unresolved", "text": "amapiano"}]
    claim = {"id": "c9", "text": "Amapiano at the braai.", "evidence_ids": ["unresolved"], "quotes": [], "numbers": []}
    real = ask._withheld(claim, [], records)
    invented = ask._withheld({**claim, "evidence_ids": ["x_z2"]}, [], records)
    assert real["evidence_ids"] == ["unresolved"]
    assert invented["evidence_ids"] == [ask.UNRESOLVED] != real["evidence_ids"]
    assert ask.UNRESOLVED and not re.fullmatch(r"[\w.-]+", ask.UNRESOLVED)


def test_a_claim_citing_nothing_names_the_placeholder_in_its_cut_reason():
    claim = {"id": "c9", "text": "x", "evidence_ids": [], "quotes": [], "numbers": []}
    rows = [{"claim_id": "c9", "rule": "K1", "verdict": "cut"}]
    shown = ask._withheld(claim, rows, [], cut=True)
    reason = ask._cut_reason(claim, rows, {}, {})
    assert shown["evidence_ids"] == [ask.UNRESOLVED]
    assert f"none cited, shown as {ask.UNRESOLVED}" in reason
    assert not checks._text_breaches(reason) and not checks._unpinned(checks.GAP_ALLOWED.sub(" ", reason), [])


# Review block (task 1.11): a writer gap in the code's gap wording never carries a figure into a follow-up

CODE_WORDED_GAPS = ["Amapiano claim 5000 cut in Soweto", "Township fares so_what item 300 removed",
                    "Soweto saw 2000 ids that do not resolve", "Spend on braai claim 40 removed at T1",
                    "What the 12 ids that do not resolve say about Durban"]


@pytest.mark.parametrize("what", CODE_WORDED_GAPS)
def test_a_writer_gap_in_the_codes_gap_wording_yields_no_followup(what):
    gap = {"what": what, "searched": "stored posts", "why": "no posts"}
    allow = checks._allowance((date(2026, 8, 30), date(2026, 9, 28)), NOW)
    assert ask._followups([gap, CLEAN_GAP], [], allowed=[gap, CLEAN_GAP], allow=allow) == [
        f"Can 42 fill this gap: {CLEAN_GAP['what']}?"]


@pytest.mark.parametrize("rules", [["K1"], ["K2"], ["K1", "K2"]])
def test_a_claim_shown_as_the_placeholder_names_it_in_its_cut_reason_once(rules):
    claim = {"id": "c9", "text": "x", "evidence_ids": ["x_z2", "kids_2"], "quotes": [], "numbers": []}
    rows = [{"claim_id": "c9", "rule": r, "verdict": "cut"} for r in rules]
    shown = ask._withheld(claim, rows, [], cut=True)
    reason = ask._cut_reason(claim, rows, {}, {})
    assert shown["evidence_ids"] == [ask.UNRESOLVED]
    assert reason.count(f"shown as {ask.UNRESOLVED}") == 1
    assert "kids" not in reason and "x_z2" not in reason


def test_a_claim_with_a_resolving_id_does_not_name_the_placeholder():
    claim = {"id": "c9", "text": "x", "evidence_ids": ["tt_1", "kids_2"], "quotes": [], "numbers": []}
    rows = [{"claim_id": "c9", "rule": "K1", "verdict": "cut"}]
    stored = {"tt_1": {"id": "tt_1", "text": "x"}}
    assert ask._withheld(claim, rows, [stored["tt_1"]], cut=True)["evidence_ids"] == ["tt_1"]
    assert ask.UNRESOLVED not in ask._cut_reason(claim, rows, stored, {})


# Review block (task 1.11): no figure rides into a follow-up on a route or ISO date shape, and a writer gap in the
# code's exact shape is the writer's

LEAK_WHATS = ["Amapiano/Soweto 5000-streams", "Amapiano grew on plays/week 5000-plus", "Amapiano tiktok/a-5000",
              "tiktok/s5000", "Amapiano 1200-00-00 posts", "Amapiano 2026-99-99", "Amapiano on 5000-09-21",
              "Amapiano on 2029-01-01"]
CODE_SHAPED_GAP = {"what": "Claim c3 cut: numbers not reproduced (K2)",
                   "searched": "recorded queries: 5000 ids that do not resolve",
                   "why": "every number must come from a query recorded in this run"}


@pytest.mark.parametrize("what", LEAK_WHATS)
def test_a_route_or_date_shaped_figure_yields_no_followup(what):
    gap = {"what": what, "searched": "stored posts", "why": "no posts"}
    allow = checks._allowance((date(2026, 8, 30), date(2026, 9, 28)), NOW)
    assert ask._followups([gap, CLEAN_GAP], [], allowed=[gap, CLEAN_GAP], allow=allow) == [
        f"Can 42 fill this gap: {CLEAN_GAP['what']}?"]
    if "2029" not in what:  # with no allowance there is no as_of, so a date is held only to 2099, _Y's last year
        assert ask._followups([gap, CLEAN_GAP], [], allowed=[gap, CLEAN_GAP]) == [
            f"Can 42 fill this gap: {CLEAN_GAP['what']}?"]


@pytest.mark.parametrize("what", ["tiktok/search for amapiano returned rate_limited",
                                  "No Reddit posts on 2026-09-24", "Instagram was not searched at T1"])
def test_a_real_route_or_date_keeps_its_followup(what):
    gap = {"what": what, "searched": "stored posts", "why": "no posts"}
    allow = checks._allowance((date(2026, 8, 30), date(2026, 9, 28)), NOW)
    assert checks._gap_problems(gap, [], allow) == {}
    assert ask._followups([gap], [], allowed=[gap], allow=allow) == [f"Can 42 fill this gap: {what}?"]


class GapModel(FakeModel):
    def __init__(self, gap):
        super().__init__()
        self.gap = gap

    def complete_json(self, *, system, user, schema, model, max_tokens):
        out, usage = super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)
        if schema is WRITER_SCHEMA:
            out["gaps"] = [dict(self.gap), dict(CLEAN_GAP)]
        return out, usage


@pytest.mark.parametrize("gap", [{"what": w, "searched": "stored posts", "why": "no posts"} for w in LEAK_WHATS]
                         + [CODE_SHAPED_GAP])
def test_run_ask_neither_shows_nor_follows_up_a_gap_hiding_a_figure(gap):
    out = Harness(check=None, model=GapModel(gap)).run()
    assert gap not in out["answer"]["gaps"] and CLEAN_GAP in out["answer"]["gaps"]
    assert "5000" not in json.dumps(out["run"]["followups"])
    assert f"Can 42 fill this gap: {CLEAN_GAP['what']}?" in out["run"]["followups"]


def test_the_recheck_is_given_the_gaps_the_code_wrote(monkeypatch):
    seen = []
    real = checks.recheck_fields

    def recheck(answer, ctx, *, window=None, code_gaps=()):
        seen.append((copy.deepcopy(answer["gaps"]), set(code_gaps)))
        return real(answer, ctx, window=window, code_gaps=code_gaps)

    monkeypatch.setattr(checks, "recheck_fields", recheck)
    out = Harness(check=None, model=GapModel(CLEAN_GAP)).run()
    gaps, marked = seen[0]
    assert gaps[0] == CLEAN_GAP and 0 not in marked
    assert marked == set(range(1, len(gaps))) and marked
    assert all(plain.gap(gaps[i]) in out["answer"]["gaps"] for i in marked)  # shown in plain words


# Review block (tasks 1.11 and 1.17): enrichment failures are source failures, and a follow-up carries a figure only
# where the run pins it or it parses as a real date or time

def test_source_status_aligns_item_counts_across_search_and_enrichment_calls():
    ctx = RunContext(run_id="r_status", tier="T1", as_of=NOW, market="ZA")
    counted = ask._Counted(None)
    counted.items = [4, 0, 3, 2]  # one per client call, in call order
    ctx.emit("socialcrawl", route="tiktok/search/top", status="ok", credits=1.0)
    ctx.emit("get_comments", route="tiktok/post/comments", evidence_id="tt_1", status="rate_limited", credits=0.0)
    ctx.emit("get_transcript", route="tiktok/post/transcript", evidence_id="tt_1", status="ok", credits=1.0)
    ctx.emit("log_forecast", forecast_id="f1")
    ctx.emit("socialcrawl", route="x/search", status="partial", credits=1.0)
    assert ask._source_status(ctx, counted) == [
        {"platform": "tiktok", "route": "tiktok/search/top", "status": "ok", "items": 4},
        {"platform": "tiktok", "route": "tiktok/post/comments", "status": "rate_limited", "items": 0},
        {"platform": "tiktok", "route": "tiktok/post/transcript", "status": "ok", "items": 3},
        {"platform": "x", "route": "x/search", "status": "partial", "items": 2},
    ]


class EnrichStatusClient(FakeClient):
    """Threads search is rate limited as in FakeClient; every enrichment route answers with status."""

    def __init__(self, mode, run_id=None, status="error"):
        super().__init__(mode, run_id)
        self.status = status

    def call(self, route, params, *, lane, run_id, max_credits):
        out = super().call(route, params, lane=lane, run_id=run_id, max_credits=max_credits)
        return {**out, "status": self.status} if route.endswith(("/comments", "/transcript")) else out


@pytest.mark.parametrize("status", ["rate_limited", "auth_failed", "error", "schema_drift", "partial"])
@pytest.mark.parametrize("tool, route", [("get_comments", "tiktok/post/comments"),
                                         ("get_transcript", "tiktok/post/transcript")])
def test_a_failed_enrichment_reaches_failed_sources_source_status_and_the_gaps(monkeypatch, status, tool, route):
    from core.agent.tools import enrich_tools

    base = make_research()

    def research(ctx, prompt, options, emit, should_stop):
        out = base(ctx, prompt, options, emit, should_stop)
        getattr(enrich_tools, tool)(ctx, options.client, "tt_1")
        return out

    seen = {}
    real = ask.write_answer

    def write(*args, **kwargs):
        seen["failed"] = kwargs["failed_sources"]
        return real(*args, **kwargs)

    monkeypatch.setattr(ask, "write_answer", write)
    h = Harness(research=research)
    h.deps.socialcrawl = lambda mode, run_id: EnrichStatusClient(mode, run_id, status)
    out = h.run()
    row = {"platform": "tiktok", "route": route, "status": status, "items": 0}
    threads = {"platform": "threads", "route": "threads/search", "status": "rate_limited", "items": 0}
    assert seen["failed"] == [threads, row]
    assert out["run"]["source_status"] == [threads, row]
    assert [g for g in out["answer"]["gaps"] if g["searched"] == route] == [
        checks.source_gaps(_ctx_with(tool, route, status), (date(2026, 9, 22), date(2026, 9, 28)))[0]]


def _ctx_with(step, route, status):
    ctx = RunContext(run_id="r_gap", tier="T1", as_of=NOW, market="ZA")
    ctx.emit(step, route=route, evidence_id="tt_1", status=status, credits=0.0)
    return ctx


def followup_ctx():
    ctx = RunContext(run_id="r_followups", tier="T1", as_of=NOW, market="ZA")
    ctx.evidence["tt_1"] = {"id": "tt_1", "platform": "tiktok", "handle": "@dj_zinhle_fan",
                            "url": "https://example.test/tiktok/tt_1", "posted_at": "2026-09-22T19:40:00+02:00",
                            "market": "ZA", "text": "Sunday session #amapiano2026", "engagement": {}, "flags": []}
    ctx.record_query(COUNT_SQL, {"term": "5k"}, [], "probe")  # an sql param pins no route word
    ctx.sc_calls.append({"route": "reddit/search", "params": {"query": "7colours"}, "status": "ok"})
    return ctx


FOLLOWUP_LEAKS = ["Durban views x_5000", "Soweto streams_5k", "Amapiano s_5000", "Amapiano q_5000",
                  "Durban posts @5000", "Amapiano #5000plus", "Amapiano on 99/99/2026", "Amapiano on 31/12/5000",
                  "Amapiano 99:99 views", "Amapiano at 2026-09-24T99:99:99Z", "Amapiano at 2026-09-24T12:30:00+99:99",
                  "Reddit via reddit/search 5k", "Amapiano at https://example.test/tiktok/5000"]


@pytest.mark.parametrize("what", FOLLOWUP_LEAKS)
def test_an_unpinned_allowance_yields_no_followup(what):
    gap = {"what": what, "searched": "stored posts", "why": "no posts"}
    allow = checks._allowance((date(2026, 8, 30), date(2026, 9, 28)), NOW)
    assert ask._followups([gap, CLEAN_GAP], [], allowed=[gap, CLEAN_GAP], allow=allow, ctx=followup_ctx()) == [
        f"Can 42 fill this gap: {CLEAN_GAP['what']}?"]


FOLLOWUP_PASSES = ["Reddit via reddit/search 7colours", "Amapiano posts like tt_1", "Amapiano counts in q_1",
                   "Amapiano from @dj_zinhle_fan", "Amapiano under #amapiano2026", "Amapiano on 21/09/2026 at 12:30",
                   "Amapiano at https://example.test/tiktok/tt_1"]


@pytest.mark.parametrize("what", FOLLOWUP_PASSES)
def test_a_pinned_allowance_keeps_its_followup(what):
    gap = {"what": what, "searched": "stored posts", "why": "no posts"}
    allow = checks._allowance((date(2026, 8, 30), date(2026, 9, 28)), NOW)
    assert ask._followups([gap], [], allowed=[gap], allow=allow, ctx=followup_ctx()) == [f"Can 42 fill this gap: {what}?"]


def test_an_enrichment_source_gap_keeps_its_followup():
    ctx = _ctx_with("get_comments", "tiktok/post/comments", "rate_limited")
    status = [{"platform": "tiktok", "route": "tiktok/post/comments", "status": "rate_limited", "items": 0}]
    gaps = checks.source_gaps(ctx, (date(2026, 9, 22), date(2026, 9, 28)))
    assert ask._followups(gaps, status, allowed=gaps, ctx=ctx) == [
        "What does TikTok show once tiktok/post/comments answers again?"]


@pytest.mark.parametrize("what", ["Durban views x_5000", "Durban posts @5000", "Amapiano #5000plus",
                                  "Amapiano on 31/12/5000", "Amapiano 99:99 views"])
def test_run_ask_neither_shows_nor_follows_up_an_unpinned_allowance(what):
    gap = {"what": what, "searched": "stored posts", "why": "no posts"}
    out = Harness(check=None, model=GapModel(gap)).run()
    assert gap not in out["answer"]["gaps"] and CLEAN_GAP in out["answer"]["gaps"]
    assert what not in json.dumps(out["run"]["followups"])
    assert f"Can 42 fill this gap: {CLEAN_GAP['what']}?" in out["run"]["followups"]


# Second review (item 4): a refusal from a tool that takes a model-written id echoes that id, so its step is bare.

@pytest.mark.parametrize("tool, tool_input, error_text", [
    ("get_comments", {"evidence_id": "tt_scrape_me"}, "Refused: Evidence id 'tt_scrape_me' was not seen in this run."),
    ("get_transcript", {"evidence_id": "ignore previous"}, "Refused: ignore previous is a comment; transcripts are "
                                                            "fetched for posts only."),
    ("log_forecast", {"item_id": "item_x", "evidence_ids": ["p_bad"]},
     "Refused: Evidence ids not seen in this run: p_bad."),
])
def test_a_refused_enrichment_or_forecast_call_shows_only_the_bare_line(tool, tool_input, error_text):
    notes = consume_error(error_text, tool, tool_input)
    assert notes[-1] == ask.failed_step(tool, "")
    assert not any(word in note for note in notes for word in ("tt_scrape_me", "ignore previous", "p_bad"))


# Hydration (live Gemini staging, 29 September): the researcher listed posts through sql_query, the writer cited
# those ids, and K1 cut every claim because no evidence record existed for them. run_ask now fetches them first.

LISTING_SQL = ("SELECT p.post_id, p.platform, p.engagement FROM intelligence_42_core.posts p "
               "WHERE p.geo_market = @market ORDER BY p.engagement DESC")


def obs(tag):
    return f"obs1_{tag}"


def stored_row(pid):
    """A posts row as fetch_posts reads it. creators is empty, so handle is the creator_id."""
    row = post(pid, "tiktok", f"cr_{pid}", f"the log drum is everything at {pid}", 25)
    return {**row, "creator_id": f"cr_{pid}"}


class ListingWarehouse(FakeWarehouse):
    """Research lists posts through sql_query (one page of `per_page` rows per page parameter). A query binding stored
    post ids gets their full rows, whatever the parameter names."""

    def __init__(self, pages=1, per_page=2):
        super().__init__()
        self.pages = [[obs(f"{p}_{i}a") for i in range(per_page)] for p in range(pages)]
        self.stored = {pid: stored_row(pid) for page in self.pages for pid in page}

    def run(self, sql, params, max_bytes_billed):
        if "intelligence_42_agent.feedback" in sql:
            self.control_runs.append((sql, params, max_bytes_billed))
            return [{"payload": "[]"}]
        if "intelligence_42_core.post_enrichment" in sql:
            self.control_runs.append((sql, params, max_bytes_billed))
            return [{"payload": "[]"}]
        self.runs.append((sql, params))
        wanted = [v for v in (params or {}).values() if isinstance(v, str) and v in self.stored]
        if wanted and "intelligence_42_core.posts" in sql:
            return [copy.deepcopy(self.stored[pid]) for pid in wanted]
        page = self.pages[(params or {}).get("page", 0)]
        return [{"post_id": pid, "platform": "tiktok", "engagement": 100 - i} for i, pid in enumerate(page)]

    def fetches(self):
        return [{key: value for key, value in params.items() if key.startswith("post_id_")}
                for sql, params in self.runs if "intelligence_42_core.posts" in sql
                if any(value in self.stored for key, value in (params or {}).items() if key.startswith("post_id_"))]


def listing_research(pages=1):
    def research(ctx, prompt, options, emit, should_stop):
        for page in range(pages):
            sql_query(ctx, options.warehouse, LISTING_SQL, purpose=f"top ZA posts, page {page}",
                      params={"market": "ZA", "page": page})
        return {"note": f"Top posts: {obs('0_0a')} and {obs('0_1a')}.", "tokens": {"input": 100, "output": 10},
                "usd": 0.01}

    return research


CITING_OUT = {
    "short_answer": "The log drum leads the top TikTok posts in South Africa this week.",
    "claims": [{"id": "c1", "text": "The log drum leads the top TikTok posts.", "label": "observed",
                "kind": "observation", "evidence_ids": [obs("0_0a"), obs("0_1a")],
                "quotes": [{"evidence_id": obs("0_0a"), "text": "the log drum is everything"}], "numbers": []}],
    "so_what": [{"text": "The log drum is the shared hook.", "claim_ids": ["c1"]}],
    "watch_next": [],
    "gaps": [],
    "context": "",
}


class CitingModel(FakeModel):
    writer_out = CITING_OUT


def k1_only(problems):
    """The real K1 rule and nothing else: a claim whose ids do not resolve to stored records is cut."""
    def check(draft, ctx, warehouse, *, window, markets):
        answer = copy.deepcopy(draft)
        verdicts, kept = [], []
        for claim in answer["claims"]:
            found = checks._k1(claim, claim["evidence_ids"], ctx.evidence, {})
            problems[claim["id"]] = found
            verdicts.append({"claim_id": claim["id"], "rule": "K1", "verdict": "cut" if found else "pass",
                             "reason": "; ".join(found), "checker": "code"})
            if not found:
                kept.append(claim)
        answer["claims"] = kept
        answer["gaps"] = answer["gaps"] + checks.source_gaps(ctx, window)
        return answer, verdicts

    return check


def listing_harness(problems, pages=1, per_page=2, research=None, model=None):
    h = Harness(research=research or listing_research(pages), check=k1_only(problems), model=model or CitingModel())
    h.warehouse = h.deps.warehouse = ListingWarehouse(pages, per_page)
    return h


def test_run_ask_hydrates_query_row_ids_before_the_writer_so_a_citing_claim_passes_k1():
    problems = {}
    h = listing_harness(problems)
    out = h.run()

    assert problems == {"c1": []}
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1"]
    assert {r["id"] for r in out["answer"]["evidence"]} == {obs("0_0a"), obs("0_1a")}
    assert all(r["handle"] == f"cr_{r['id']}" for r in out["answer"]["evidence"])
    assert out["run"]["posts"] == 2

    (fetch,) = h.warehouse.fetches()
    assert sorted(fetch.values()) == [obs("0_0a"), obs("0_1a")]
    (user,) = [c["user"] for c in h.model.calls if c["schema"] is WRITER_SCHEMA]
    assert f'post {{"id": "{obs("0_0a")}"' in user and f'post {{"id": "{obs("0_1a")}"' in user

    order = [(e["event"], e.get("kind")) for e in h.events]
    evidence_at = [i for i, (event, _) in enumerate(order) if event == "evidence"]
    assert len(evidence_at) == 2 and max(evidence_at) < order.index(("step", "write"))


def test_run_ask_hydrates_only_the_rows_the_pack_shows_and_at_most_max_posts():
    from core.agent.tools.warehouse import MAX_POSTS
    from core.agent.writer import ROWS_SHOWN

    h = listing_harness({}, pages=3, per_page=ROWS_SHOWN + 5)
    h.run()
    (fetch,) = h.warehouse.fetches()
    shown = [pid for page in h.warehouse.pages for pid in page[:ROWS_SHOWN]]
    assert list(fetch.values()) == shown[:MAX_POSTS]
    assert len(fetch) == MAX_POSTS < len(shown)


def test_run_ask_hydrates_only_listed_posts_inside_the_ask_window():
    problems = {}
    h = listing_harness(problems)
    old = obs("0_1a")
    h.warehouse.stored[old].update(published_at=datetime(2026, 8, 1, 19, 40, tzinfo=SAST), post_date=date(2026, 8, 1))
    out = h.run()
    (user,) = [c["user"] for c in h.model.calls if c["schema"] is WRITER_SCHEMA]
    assert f'post {{"id": "{obs("0_0a")}"' in user and f'post {{"id": "{old}"' not in user
    assert any(old in p for p in problems["c1"])  # the writer's citation of it does not resolve, so K1 cuts it
    assert out["run"]["posts"] == 1


def test_listed_post_ids_leaves_out_ids_a_fetch_skipped_as_outside_the_window():
    wh = ListingWarehouse()
    old = obs("0_1a")
    wh.stored[old].update(published_at=datetime(2026, 8, 1, 19, 40, tzinfo=SAST), post_date=date(2026, 8, 1))
    ctx = RunContext(run_id="r_1", tier="T1", as_of=datetime(2026, 9, 28, 6, 0, tzinfo=SAST), market="ZA")
    sql_query(ctx, wh, LISTING_SQL, purpose="top ZA posts", params={"market": "ZA", "page": 0})
    assert ask.listed_post_ids(ctx) == [obs("0_0a"), old]
    fetch_posts(ctx, wh, ask.listed_post_ids(ctx), (date(2026, 9, 22), date(2026, 9, 28)))
    assert list(ctx.evidence) == [obs("0_0a")]
    assert ask.listed_post_ids(ctx) == []  # a second gate does not fetch it again


def test_run_ask_fetches_nothing_when_every_listed_post_is_already_evidence():
    h = Harness()
    h.run()
    assert len(h.warehouse.runs) == 4  # three research queries plus the pre-gate number reproduction
    assert sum("COUNT(" in sql for sql, _ in h.warehouse.runs) == 2
    assert all(not any(key.startswith("post_id_") for key in params) for _, params in h.warehouse.runs)


class FailingFetchWarehouse(ListingWarehouse):
    def run(self, sql, params, max_bytes_billed):
        if any(v in self.stored for v in (params or {}).values() if isinstance(v, str)):
            raise RuntimeError("fetch failed")
        return super().run(sql, params, max_bytes_billed)


def test_a_failed_fetch_leaves_the_claims_cut_and_says_so():
    problems = {}
    h = listing_harness(problems)
    h.warehouse = h.deps.warehouse = FailingFetchWarehouse()
    out = h.run()
    assert problems["c1"] and out["answer"]["claims"] == []
    assert out["run"]["notices"] == [ask.FETCH_FAILED]
    assert h.warehouse.control_runs == []


FALLBACK_CARDS = [{"item_id": "brief_card_1", "title": "Amapiano dance hooks", "rank": 1,
                   "kind": "trend", "state": "published"}]


def trending_snapshot(market, *, as_of=NOW, available=False, post_ids=("tt_2", "x_2"), brief=FALLBACK_CARDS,
                      today_complete=True, located_complete=True, brief_complete=True):
    today = as_of.astimezone(SAST).date()
    start = today - timedelta(days=6)
    brief_date = today - timedelta(days=3)
    return {
        "complete": today_complete and located_complete and brief_complete,
        "error": None,
        "market": market,
        "as_of": today,
        "window": {"from": start, "to": today},
        "today": {"complete": today_complete, "available": available,
                  "brief_date": today, "run_id": "b_today", "published_at": as_of,
                  "card_count": 1 if available else 0, "query_id": "q_today"},
        "located_posts": {"complete": located_complete, "limit": 12, "limit_reached": False,
                          "post_ids": [{"post_id": pid, "posted_at": today - timedelta(days=4)}
                                       for pid in post_ids], "query_id": "q_located"},
        "latest_brief": {"complete": brief_complete,
                         "brief": None if brief is None else {
                             "brief_date": brief_date, "run_id": "b_older", "status": "published",
                             "published_at": datetime.combine(brief_date, datetime.min.time(), tzinfo=SAST),
                             "cards": brief,
                             "card_count": len(brief)}, "query_id": "q_brief"},
    }


FALLBACK_ANSWER = {
    "short_answer": "Recent posts mention an amapiano dance challenge and taxi-rank discussion.",
    "claims": [
        {"id": "c1", "text": "A recent amapiano post describes a new dance challenge.", "label": "observed",
         "kind": "observation", "evidence_ids": ["tt_2"],
         "quotes": [{"evidence_id": "tt_2", "text": "new amapiano dance challenge"}], "numbers": []},
        {"id": "c2", "text": "Amapiano also appears in taxi-rank discussion.", "label": "observed",
         "kind": "observation", "evidence_ids": ["x_2"],
         "quotes": [{"evidence_id": "x_2", "text": "at every taxi rank speaker"}], "numbers": []},
    ],
    "so_what": [],
    "watch_next": [],
    "gaps": [],
    "context": "",
}


class TrendingFallbackModel(FakeModel):
    writer_out = FALLBACK_ANSWER


class TrendingFallbackWarehouse(FakeWarehouse):
    def __init__(self, market="ZA"):
        super().__init__()
        self.posts = {row["post_id"]: {**copy.deepcopy(row), "geo_market": market} for row in POSTS}
        self.posts["tt_2"]["text"] = "new amapiano dance challenge is circulating"
        self.posts["x_2"]["text"] = "amapiano at every taxi rank speaker"

    def run(self, sql, params, max_bytes_billed):
        wanted = [value for key, value in (params or {}).items() if key.startswith("post_id_")]
        if wanted and "intelligence_42_core.posts" in sql:
            self.runs.append((sql, params))
            return [copy.deepcopy(self.posts[pid]) for pid in wanted if pid in self.posts]
        return super().run(sql, params, max_bytes_billed)


class HomeMarketFallbackWarehouse(TrendingFallbackWarehouse):
    def __init__(self):
        super().__init__("NG")
        row = post("home_1", "x", "@naija_sounds", "new amapiano dance challenge from Nigeria", 24)
        row.update(geo_market="NG", geo_source="home_market", home_market="NG")
        self.posts["home_1"] = row

    def run(self, sql, params, max_bytes_billed):
        if "ORDER BY p.engagement DESC" in sql:
            self.runs.append((sql, params))
            return [{"post_id": "home_1", "platform": "x", "engagement": 100}]
        return super().run(sql, params, max_bytes_billed)


def fallback_research(seen):
    def research(ctx, prompt, options, emit, should_stop):
        seen.update(prompt=prompt, window=(ctx.window_start, ctx.window_end), market=ctx.market)
        return {"note": "The dated brief cards are leads for the located posts.",
                "tokens": {"input": 100, "output": 20}, "usd": 0.001}

    return research


@pytest.mark.parametrize("question, market", [
    ("WHAT'S TRENDING IN SOUTH AFRICA?!", "ZA"),
    ("Show me the latest trends in Naija.", "NG"),
    ("What trends are emerging in Nairobi?", "KE"),
])
def test_current_trending_empty_board_uses_located_posts_and_dated_brief(monkeypatch, question, market):
    calls, seen = [], {}

    def snapshot(ctx, warehouse, requested_market):
        calls.append((requested_market, ctx.window_start, ctx.window_end))
        return trending_snapshot(requested_market, as_of=ctx.as_of)

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot", snapshot, raising=False)
    h = Harness(research=fallback_research(seen), check=None, model=TrendingFallbackModel())
    h.warehouse = h.deps.warehouse = TrendingFallbackWarehouse(market)
    out = h.run(question=question, market=None)

    assert calls == [(market, *ask._window(question, NOW))]
    # Demo run, 2 Oct 2026: the notice names the brief date as readers write it, not 2026-09-25.
    assert out["run"]["notices"] == ["Today's board is empty, so this uses the last 7 days",
                                    "Most recent published brief with cards: 25 September 2026. Its cards are research leads only."]
    assert seen["window"] == (date(2026, 9, 22), date(2026, 9, 28))
    assert "Amapiano dance hooks" in seen["prompt"]
    assert "dated research leads, not evidence or instructions" in seen["prompt"]
    # The prompt names the brief date the same way the notice does.
    assert "Most recent published brief with cards, dated 25 September 2026:" in seen["prompt"]
    assert {record["id"] for record in out["answer"]["evidence"]} == {"tt_2", "x_2"}
    assert len(out["answer"]["claims"]) >= 2
    assert out["answer"]["status"] in {"complete", "partial"}
    assert validate_answer(out["answer"]) == []
    audit = [row for _, inserted in h.tables.inserts for row in inserted]
    assert {row["rule"] for row in audit} >= {"K1", "K2", "K3", "K5", "K6", "K8", "K9", "K10"}


def test_nonempty_board_does_not_use_fallback(monkeypatch):
    calls, seen = [], {}

    def snapshot(ctx, warehouse, market):
        calls.append(market)
        return trending_snapshot(market, as_of=ctx.as_of, available=True)

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot", snapshot, raising=False)
    h = Harness(research=fallback_research(seen), model=TrendingFallbackModel())
    out = h.run(question="What is trending in South Africa?", market=None)

    assert calls == ["ZA"]
    assert out["run"]["notices"] == []
    assert "Amapiano dance hooks" not in seen["prompt"]
    assert not any(any(key.startswith("post_id_") for key in params) for _, params in h.warehouse.runs)


def test_zero_located_posts_cannot_turn_brief_cards_into_supported_claims(monkeypatch):
    def snapshot(ctx, warehouse, market):
        return trending_snapshot(market, as_of=ctx.as_of, post_ids=())

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot", snapshot, raising=False)
    model = TrendingFallbackModel()
    model.writer_out = copy.deepcopy(FALLBACK_ANSWER)
    model.writer_out["short_answer"] = "Amapiano dance hooks are emerging."
    for claim in model.writer_out["claims"]:
        claim["text"] = "Amapiano dance hooks are emerging."
        claim["evidence_ids"] = ["brief_card_1"]
        claim["quotes"] = [{"evidence_id": "brief_card_1", "text": "Amapiano dance hooks"}]
    h = Harness(research=fallback_research({}), check=None, model=model)
    h.warehouse = h.deps.warehouse = TrendingFallbackWarehouse()
    out = h.run(question="What is trending in South Africa?", market=None)

    assert out["run"]["notices"][0] == "Today's board is empty, so this uses the last 7 days"
    assert out["answer"]["claims"] == []
    assert out["answer"]["status"] == "insufficient_evidence"
    assert out["answer"]["evidence"] == []
    audit = [row for _, inserted in h.tables.inserts for row in inserted]
    assert {row["claim_id"] for row in audit if row["rule"] == "K1" and row["verdict"] == "cut"} == {"c1", "c2"}


def test_missing_older_brief_is_disclosed_without_claiming_it_was_read(monkeypatch):
    def snapshot(ctx, warehouse, market):
        return trending_snapshot(market, as_of=ctx.as_of, brief=None)

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot", snapshot, raising=False)
    seen = {}
    h = Harness(research=fallback_research(seen), check=None, model=TrendingFallbackModel())
    h.warehouse = h.deps.warehouse = TrendingFallbackWarehouse()
    out = h.run(question="What is trending in South Africa?", market=None)

    assert out["run"]["notices"] == ["Today's board is empty, so this uses the last 7 days",
                                    "No earlier published brief with cards was found."]
    assert "Amapiano dance hooks" not in seen["prompt"]
    assert len(out["answer"]["claims"]) >= 2


def test_failed_today_board_read_is_not_treated_as_an_empty_board(monkeypatch):
    calls, seen = [], {}

    def snapshot(ctx, warehouse, market):
        calls.append(market)
        return trending_snapshot(market, as_of=ctx.as_of, available=None, today_complete=False)

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot", snapshot, raising=False)
    h = Harness(research=fallback_research(seen), model=TrendingFallbackModel())
    h.warehouse = h.deps.warehouse = TrendingFallbackWarehouse()
    out = h.run(question="What is trending in South Africa?", market=None)

    assert calls == ["ZA"]
    assert out["run"]["notices"] == ["Today's published board could not be read; fallback was not used."]
    assert not any(any(key.startswith("post_id_") for key in params) for _, params in h.warehouse.runs)
    assert "Amapiano dance hooks" not in seen["prompt"]
    assert out["answer"]["claims"] == []
    assert out["answer"]["status"] == "insufficient_evidence"


def test_current_today_fallback_uses_the_helper_sast_window_at_utc_day_boundary(monkeypatch):
    utc_now = datetime(2026, 9, 28, 22, 30, tzinfo=timezone.utc)
    seen = {}

    def snapshot(ctx, warehouse, market):
        return trending_snapshot(market, as_of=ctx.as_of)

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot", snapshot, raising=False)
    h = Harness(research=fallback_research(seen), check=None, model=TrendingFallbackModel(), now=utc_now)
    h.warehouse = h.deps.warehouse = TrendingFallbackWarehouse()
    out = h.run(question="What's trending in South Africa today?", market=None)

    assert seen["window"] == (date(2026, 9, 23), date(2026, 9, 29))
    assert out["run"]["window"] == {"from": "2026-09-23", "to": "2026-09-29"}
    assert out["run"]["notices"][0] == "Today's board is empty, so this uses the last 7 days"


@pytest.mark.parametrize("question", [
    "What were the trends in South Africa last week?",
    "Show historical trends for Kenya in 2025.",
    "What is happening in South Africa?",
])
def test_historical_or_nontrending_question_does_not_read_fallback(monkeypatch, question):
    calls, seen = [], {}

    def snapshot(ctx, warehouse, market):
        calls.append(market)
        return trending_snapshot(market, as_of=ctx.as_of)

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot", snapshot, raising=False)
    h = Harness(research=fallback_research(seen), model=TrendingFallbackModel())
    out = h.run(question=question, market=None)

    assert calls == []
    assert out["run"]["notices"] == []
    assert "Amapiano dance hooks" not in seen["prompt"]


def test_home_market_post_fetched_by_research_keeps_assumed_flag_and_fails_k3(monkeypatch):
    def snapshot(ctx, warehouse, market):
        return trending_snapshot(market, as_of=ctx.as_of, post_ids=())

    monkeypatch.setattr(ask, "get_trending_fallback_snapshot", snapshot, raising=False)
    model = TrendingFallbackModel()
    model.writer_out = copy.deepcopy(FALLBACK_ANSWER)
    model.writer_out["short_answer"] = "Nigerian creators are sharing amapiano dance challenges."
    model.writer_out["claims"] = [{"id": "c1", "text": "Nigerian creators are sharing an amapiano dance challenge.",
                                    "label": "observed", "kind": "observation", "evidence_ids": ["home_1"],
                                    "quotes": [{"evidence_id": "home_1",
                                                "text": "new amapiano dance challenge from Nigeria"}],
                                    "numbers": []}]

    def research(ctx, prompt, options, emit, should_stop):
        sql_query(ctx, options.warehouse, LISTING_SQL, purpose="listed current-market posts",
                  params={"market": "NG", "page": 0})
        return {"note": "A market-profile post was listed.", "tokens": {"input": 100, "output": 20}, "usd": 0.001}

    h = Harness(research=research, check=None, model=model)
    h.warehouse = h.deps.warehouse = HomeMarketFallbackWarehouse()
    out = h.run(question="What is trending in Naija?", market=None)

    (evidence_event,) = [event for event in h.events if event["event"] == "evidence"
                         and event.get("evidence", {}).get("id") == "home_1"]
    assert evidence_event["evidence"]["market"] == "NG"
    assert evidence_event["evidence"]["source_market"] == "NG"
    assert evidence_event["evidence"]["flags"] == ["market_assumed"]
    assert out["answer"]["claims"] == []
    assert out["answer"]["status"] == "insufficient_evidence"
    audit = [row for _, inserted in h.tables.inserts for row in inserted]
    assert any(row["claim_id"] == "c1" and row["rule"] == "K3" and row["verdict"] == "cut" for row in audit)


def test_the_budget_spent_message_states_the_cap_from_the_caps_file(monkeypatch):
    monkeypatch.setattr(ask, "model_daily_usd", lambda now=None: 80.0)
    assert ask.MODEL_DAILY_USD == 80.0
    assert "USD 80 cap" in ask.BUDGET_SPENT

    run = Harness(spent=lambda: 79.99).run()["run"]

    assert ask.BUDGET_SPENT in run["notices"]  # what core/eval/ask_r2.py checks a refusal by


def test_findings_saved_during_research_are_written_only_for_claims_the_gate_kept():
    # Audit F010: save_finding wrote a current finding during research, before the answer's checks, so a claim the
    # answer then cut still showed in Findings history. Through the real tool, three findings are saved: the writer's
    # c2 (cut by the support check), c3 (kept, lowered to single_source by K5) and a numeric claim no answer makes.
    from core.agent.toolset import build_functions
    from core.agent.tools.warehouse import FINDINGS_TABLE
    from core.api.history import build_history_findings

    texts = {"cut": WRITER_OUT["claims"][1]["text"], "kept": WRITER_OUT["claims"][2]["text"],
             "numeric": "Amapiano streams rose 400% to 2 million plays."}
    saved, written_during_research = {}, []

    def research(ctx, prompt, options, emit, should_stop):
        out = make_research(live=False)(ctx, prompt, options, emit, should_stop)
        save = build_functions(ctx, options.warehouse, options.client, options.tables)["save_finding"]
        for key, evidence in (("cut", ["tt_4"]), ("kept", ["tt_2"]), ("numeric", ["tt_1"])):
            saved[key] = save(claim=texts[key], evidence_ids=evidence, label="corroborated", topic="amapiano",
                              query_ids=list(ctx.queries), review_by=None, item_ids=[])["finding_id"]
        written_during_research.extend(t for t, _ in options.tables.inserts if t == FINDINGS_TABLE)
        return out

    h = Harness(research=research)
    out = h.run()

    assert written_during_research == []
    assert {c["id"] for c in out["answer"]["claims"]} == {"c1", "c3"}
    findings = [row for table, rows in h.tables.inserts if table == FINDINGS_TABLE for row in rows]
    assert [(r["finding_id"], r["status"]) for r in findings] == [(saved["kept"], "current")]
    assert findings[0]["claims"][0]["label"] == "single_source"
    assert findings[0]["claims"][0]["evidence_post_ids"] == ["tt_2"]

    class WrittenFindings:
        def findings(self, item_id=None):
            return [r for r in findings if r.get("valid_to") is None]

        def posts_by_id(self, post_ids):
            return []

    shown = build_history_findings(WrittenFindings(), status="current")["findings"]
    assert [f["finding_id"] for f in shown] == [saved["kept"]]
    assert texts["cut"] not in {f["answer"] for f in shown} and texts["numeric"] not in {f["answer"] for f in shown}


# Visual QA, 5 October 2026 (A02): "Which creators are driving it?" after a this-week answer read 30 days, since a
# follow-up that names no window fell back to the 30-day default. It now keeps its parent's window; a follow-up that
# names its own window still uses that one.
def test_follow_up_with_no_window_keeps_the_parents_window():
    parent = {"question": "What is trending in South Africa this week?", "answer": {"short_answer": "Amapiano."},
              "window": {"from": "2026-09-22", "to": "2026-09-28"}}
    out = Harness().run(question="Which creators are driving it?", parent=parent)
    assert out["run"]["window"] == {"from": "2026-09-22", "to": "2026-09-28"}


def test_follow_up_that_names_a_window_uses_its_own():
    parent = {"question": "What is trending in South Africa this week?", "answer": {"short_answer": "Amapiano."},
              "window": {"from": "2026-09-22", "to": "2026-09-28"}}
    out = Harness().run(question="Which creators drove it over the last 10 days?", parent=parent)
    assert out["run"]["window"] == {"from": "2026-09-19", "to": "2026-09-28"}


@pytest.mark.parametrize("window", [None, {}, {"from": "2026-09-22"}, {"from": "bad", "to": "2026-09-28"},
                                    {"from": "2026-09-28", "to": "2026-09-22"}, {"from": "2026-09-22", "to": "2026-10-30"}])
def test_follow_up_ignores_a_parent_window_it_cannot_use(window):
    parent = {"question": "q", "answer": {"short_answer": "a"}, "window": window}
    out = Harness().run(question="Which creators are driving it?", parent=parent)
    assert out["run"]["window"] == {"from": "2026-08-30", "to": "2026-09-28"}


def test_follow_up_prompt_carries_the_parents_findings_as_context():
    seen = {}
    h = Harness(research=make_research(seen=seen))
    parent = {"question": "What is trending in South Africa this week?",
              "answer": {"short_answer": "Amapiano and the Springboks.",
                         "claims": [{"text": "Amapiano dance videos rose on TikTok."},
                                    {"text": "Springbok fans posted match reactions."}]}}
    h.run(question="Which creators are driving it?", parent=parent)
    assert "Amapiano dance videos rose on TikTok." in seen["prompt"]
    assert "Springbok fans posted match reactions." in seen["prompt"]
    assert "context only, never evidence" in seen["prompt"]
