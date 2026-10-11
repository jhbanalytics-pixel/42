"""Offline replay of the ten Ask benchmark questions (ASK-POWER-PLAN section 3, Release D plan item 4).

    py -3.13 -m core.eval.ask_replay OUTDIR [ID ...]

Each question runs through the real run_ask: the real trust checks (checks.py, K1 to K10), the real writer, support
check, field check and K4 rewrite code, the real search, SQL and SocialCrawl tool code, the real model budget and hold.
Only the outside world is replaced, by a recorded fixture per question in core/eval/ask_replay_fixtures: the rows the
warehouse returns, the items SocialCrawl returns, the model replies and the token counts behind them. No network,
no BigQuery, no model call and no credit is spent, so it costs nothing and can run on every Ask change.

What a green run proves is that the machinery holds: the gate cuts what it should, no claim that survives it fails the
independent promptfoo asserts, the answer keeps its contract and its meta digest, a hidden creator never reaches a
reader, and the booked model spend stays inside the hold. What it cannot prove is that Ask is good: the posts, the
drafts and the support verdicts are the fixture's. ask_replay_score.MEANING says which score is which.

The run exits 1 on a breach and on nothing else. A breach is an expectation-free fault (an error, a contract problem, a
gate leak, spend over the hold, a failed meta digest, a hidden creator visible); no fixture decides what passes, so a
fixture cannot grade itself. The pinned expectations per question live in the tests."""

import argparse
import copy
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from core.agent import answer_state, ask, writer
from core.agent.answer import validate_answer
from core.agent.tools.enrich_tools import get_comments
from core.agent.tools.socialcrawl import creator_key, socialcrawl_call
from core.agent.tools.sql_query import sql_query
from core.agent.tools.warehouse import search_posts
from core.api import privacy
from core.eval import ask_replay_score as score
from core.eval import quality_score, review
from core.llm.provider import price_for

EVAL = Path(__file__).resolve().parent
FIXTURES = EVAL / "ask_replay_fixtures"
SAST = timezone(timedelta(hours=2))
AS_OF = datetime(2026, 10, 11, 8, 0, 0, tzinfo=SAST)

# ASK-POWER-PLAN section 3, in the plan's order: the four why questions across three markets first, then the rest,
# then the humour question as the regression.
ORDER = ["WHY-01", "WHY-02", "WHY-03", "NOW-01", "NOW-03", "RISE-02", "CRE-01", "BRD-03", "AUT-01", "HUMOUR-ZA"]

# Output tokens for a model call a fixture does not state. The input tokens of a call are a quarter of the bytes of
# the prompt it was really sent, so a bigger evidence pack costs more. Both are estimates of a real run, not
# measurements: the dollar figures are therefore fixture-bound (ask_replay_score.MEANING).
DEFAULT_OUTPUT = {"writer": 2_500, "support": 150, "fields": 200, "rewrite": 80, "headline": 60}
BYTES_PER_TOKEN = 4
TOP_KEYS = {"id", "note", "tier", "question", "vars", "hidden_before", "hidden_after", "posts", "bulk", "queries",
            "research", "socialcrawl", "writer", "support", "rewrite", "headline_rewrite", "usage"}
POST_KEYS = {"id", "platform", "handle", "days_ago", "hour", "text", "views", "likes", "comments", "shares",
             "geo_market"}

LIMITS = [
    "Tier T1 only. The T2 route and the critic are not built at this base, so no question is routed to T2.",
    "No whole-store count and no topic sweep: Deps.store_counts and Deps.topic_sweep are None, because the recorded "
    "warehouse cannot answer their SQL. Posts read means posts the recorded searches returned.",
    "The research, the writer drafts and the support verdicts are recorded. Whether a real model writes them, and "
    "whether a real model agrees a claim is supported, is for the live run.",
    "K2 re-runs a query against the same recorded rows it was first answered from, so the re-run always agrees. What "
    "the replay does check is that every numeral in a claim is pinned to a number in a query's recorded rows.",
    "No clock, no rate limit and no daily cap: spend so far today is read as zero, and ASK_DAILY and "
    "MODEL_DAILY_USD are not exercised.",
]

CLAIM_LINE = re.compile(r"^Claim: (.*)\nLabel:", re.S)
FIELD_ITEM = re.compile(r"(?m)^item (\d+) ")


def questions_by_id():
    found = yaml.safe_load((EVAL / "questions.yaml").read_text(encoding="utf-8"))
    return {q["vars"]["id"]: q for q in found}


def load(qid):
    """One fixture, checked for keys it may not carry: a typo in a fixture key would otherwise be ignored."""
    fx = json.loads((FIXTURES / f"{qid}.json").read_text(encoding="utf-8"))
    extra = set(fx) - TOP_KEYS
    if extra or fx.get("id") != qid:
        raise ValueError(f"fixture {qid}: unknown keys {sorted(extra)} or an id that is not {qid}")
    for p in fx.get("posts", []):
        if set(p) - POST_KEYS:
            raise ValueError(f"fixture {qid}: post {p.get('id')} carries unknown keys {sorted(set(p) - POST_KEYS)}")
    return fx


def question_vars(fx, library):
    """The question and its scoring vars. The nine questions in questions.yaml are read from it, never copied into
    a fixture, so the question asked cannot drift from the question scored; the humour question carries its own."""
    if fx.get("question"):
        return {"question": fx["question"], **fx["vars"]}
    return copy.deepcopy(library[fx["id"]]["vars"])


def _row(p, market):
    when = (AS_OF - timedelta(days=p["days_ago"])).replace(hour=p.get("hour", 12), minute=7, second=0)
    views = p.get("views", 1000)
    likes, comments, shares = p.get("likes", views // 25), p.get("comments", 3), p.get("shares", 1)
    return {"post_id": p["id"], "platform": p["platform"], "url": f"https://example.test/{p['platform']}/{p['id']}",
            "creator_id": f"c_{p['handle'].lstrip('@').lower()}", "handle": p["handle"], "published_at": when,
            "post_date": when.date(), "geo_market": p.get("geo_market", market), "text": p["text"], "views": views,
            "likes": likes, "comments": comments, "shares": shares, "engagement": likes + comments + shares}


def post_rows(fx, market):
    """The stored posts the fixture's warehouse holds: the explicit ones, then the bulk the fixture declares, made
    by a fixed rule (platform and creator by position, age by position, text from the stems) so the count of posts
    read can be large without a thousand lines of JSON."""
    rows = [_row(p, market) for p in fx.get("posts", [])]
    bulk = fx.get("bulk")
    if bulk:
        lo, hi = bulk["days"]
        for i in range(bulk["count"]):
            creator = i % bulk["creators"]
            rows.append(_row({
                "id": f"{bulk['prefix']}_{i}", "platform": bulk["platforms"][creator % len(bulk["platforms"])],
                "handle": f"@{bulk['prefix']}_creator_{creator}", "days_ago": lo + i % (hi - lo + 1),
                "text": f"{bulk['stems'][i % len(bulk['stems'])]} ({i})", "views": 800 + (i * 37) % 4000}, market))
    return rows


def hidden_keys(entries):
    return {creator_key(e["platform"], e["handle"]) for e in entries or []}


class ReplayWarehouse:
    """The Warehouse protocol over recorded rows. A query whose text holds a fixture query's match (and whose
    parameters hold the fixture query's) gets that query's rows; a post lookup gets the fixture's posts, narrowed to
    the ids, dates and platforms it names; the suppression read gets the hidden creators among the keys it is asked
    about. Stored search does not leave suppressed creators out in SQL, so a stored post of one is the privacy
    projection's to withhold when the answer is read (hidden_after), and hidden_before reaches live items only."""

    def __init__(self, fx, market):
        self.rows = post_rows(fx, market)
        self.queries = fx.get("queries", [])
        self.hidden = hidden_keys(fx.get("hidden_before"))

    def dry_run(self, sql, params):
        return {"bytes": 1000, "tables": ["intelligence_42_core.posts"]}

    def run(self, sql, params, max_bytes_billed):
        if "intelligence_42_agent.feedback" in sql or "intelligence_42_core.post_enrichment" in sql:
            return [{"payload": "[]"}]
        if "keys" in (params or {}) and "suppressions" in sql:
            asked = set(str(params["keys"]).split("\n"))
            return [{"key": k} for k in sorted(self.hidden & asked)]
        for spec in self.queries:
            if spec["match"] in sql and all((params or {}).get(k) == v for k, v in spec.get("params", {}).items()):
                return copy.deepcopy(spec["rows"])
        rows = self.rows
        params = params or {}
        wanted = {v for k, v in params.items() if k.startswith("post_id_")}
        if wanted:
            rows = [r for r in rows if r["post_id"] in wanted]
        if "since" in params and "until" in params:
            rows = [r for r in rows if params["since"] <= r["post_date"] <= params["until"]]
        platforms = {v for k, v in params.items() if k.startswith("platform_")}
        if platforms:
            rows = [r for r in rows if r["platform"] in platforms]
        return copy.deepcopy(rows)


def live_items(spec):
    """The items a recorded route returns: the ones listed, then the bulk it declares (a count of items from a set of
    creators with texts from stems, by a fixed rule, as post_rows does for stored posts)."""
    items = copy.deepcopy(spec.get("items", []))
    bulk = spec.get("bulk")
    for i in range(bulk["count"] if bulk else 0):
        when = AS_OF - timedelta(days=bulk["days"][0] + i % (bulk["days"][1] - bulk["days"][0] + 1), hours=3)
        items.append({"id": f"{bulk['prefix']}{i}", "handle": f"@{bulk['prefix']}_creator_{i % bulk['creators']}",
                      "market": bulk["market"], "posted_at": when.isoformat(),
                      "url": f"https://example.test/live/{bulk['prefix']}{i}",
                      "text": f"{bulk['stems'][i % len(bulk['stems'])]} ({i})",
                      "engagement": {"views": 500 + (i * 53) % 9000, "likes": 20 + (i * 7) % 400}})
    return items


class ReplayClient:
    """The SocialCrawl client over recorded routes. A route the fixture did not record is a fixture fault and raises:
    a replay never invents a result."""

    notices = ()

    def __init__(self, routes):
        self.routes = routes

    def _spec(self, route):
        if route not in self.routes:
            raise KeyError(f"the fixture recorded no result for {route}")
        return self.routes[route]

    def quote(self, route, params):
        spec = self._spec(route)
        return float(spec.get("quote", spec.get("credits_charged", 1.0)))

    def call(self, route, params, *, lane, run_id, max_credits):
        spec = self._spec(route)
        return {"items": live_items(spec), "next_cursor": None,
                "credits_charged": spec.get("credits_charged", 1.0), "status": spec.get("status", "ok"),
                "cache_hit": False, "reason": spec.get("reason", "")}


class RecordingTables:
    def __init__(self):
        self.inserts = []

    def insert(self, table, rows, row_ids=None):
        self.inserts.append((table, copy.deepcopy(rows)))

    def claim_checks(self):
        return [r for t, rows in self.inserts if t.endswith("claim_checks") for r in rows]


def _usd(model, tokens):
    price = price_for(model)
    return (tokens[0] * price["input"] + tokens[1] * price["output"]) / 1_000_000


class ReplayModel:
    """The writer model over recorded replies. The writer returns the fixture's draft with its query labels resolved
    to the ids this run recorded; a support check returns the verdict the fixture records for the claim it is shown
    (supported when it records none); a call the fixture has no reply for raises."""

    def __init__(self, fx, labels):
        self.fx, self.labels = fx, labels
        self.drafted = {c["id"]: c["text"] for c in fx["writer"]["claims"]}

    def usage(self, kind, model, prompt, max_tokens):
        """The call as a real one would bill it: a quarter of the prompt's bytes in, the fixture's output tokens
        (never more than the call may ask for) out."""
        size = sum(len(part.encode("utf-8")) for part in prompt)
        t = (-(-size // BYTES_PER_TOKEN),
             min(self.fx.get("usage", {}).get(kind, DEFAULT_OUTPUT[kind]), max_tokens))
        return {"input_tokens": t[0], "output_tokens": t[1], "usd": _usd(model, t)}

    def resolve(self, draft):
        for claim in draft["claims"]:
            for n in claim.get("numbers", []):
                if str(n["query_id"]).startswith("@"):
                    n["query_id"] = self.labels[n["query_id"][1:]]
        return draft

    def complete_json(self, *, system, user, schema, model, max_tokens):
        prompt = (system, user, json.dumps(schema, ensure_ascii=False, separators=(",", ":")))

        def usage(kind):
            return self.usage(kind, model, prompt, max_tokens)

        if schema is writer.WRITER_SCHEMA:
            return self.resolve(copy.deepcopy(self.fx["writer"])), usage("writer")
        if schema is writer.SUPPORT_SCHEMA:
            shown = CLAIM_LINE.match(user)
            cid = next((c for c, text in self.drafted.items() if shown and text == shown.group(1)), None)
            said = self.fx.get("support", {}).get(cid, {"verdict": "supported", "reason": "supported by the cited post"})
            return ({"verdict": said["verdict"], "reason": said["reason"], "demographic_inference": False,
                     "tone_claim": False, "forecast_assertion": False, "country_people": []},
                    usage("support"))
        if schema is writer.K4_REWRITE_SCHEMA:
            for cid, narrowed in self.fx.get("rewrite", {}).items():
                if self.drafted.get(cid) and self.drafted[cid] in user:
                    return {"text": narrowed}, usage("rewrite")
            return {"text": ""}, usage("rewrite")
        if schema is writer.HEADLINE_REWRITE_SCHEMA:
            return {"text": self.fx.get("headline_rewrite", "")}, usage("headline")
        if schema is writer.FIELDS_SCHEMA:
            seen = sorted({int(i) for i in FIELD_ITEM.findall(user)})
            return ({"fields": [{"index": i, "demographic_inference": False, "forecast_assertion": False,
                                 "country_people": [], "so_what_supported": True} for i in seen]},
                    usage("fields"))
        raise AssertionError("the fixture records no reply for this model call")


def make_research(fx, labels, on_topic, tally):
    """The research the fixture records, run through the real tools: each recorded call is made against the replay
    warehouse and client, so the evidence, the queries and their receipts are the ones the tool code makes."""

    def research(ctx, prompt, options, emit, should_stop):
        for call in fx["research"]["calls"]:
            tool = call["tool"]
            args = call.get("args", {})
            if tool == "search_posts":
                kind, text, platform = ask.describe_tool("search_posts", {"query": call["query"], **args}, emit)
                emit.step(kind, text, platform=platform)
                found = search_posts(ctx, options.warehouse, call["query"], **args)
                on_topic.update(e["id"] for e in found["evidence"])
                labels[call.get("as", call["query"])] = found["query_id"]
            elif tool == "sql_query":
                kind, text, platform = ask.describe_tool("sql_query", {"purpose": call["purpose"], "sql": call["sql"]},
                                                         emit)
                emit.step(kind, text, platform=platform)
                found = sql_query(ctx, options.warehouse, call["sql"], purpose=call["purpose"],
                                  params=call.get("params"))
                labels[call["as"]] = found["query_id"]
            elif tool == "socialcrawl":
                tool_input = {"platform": call["platform"], "endpoint": call["endpoint"]}
                kind, text, platform = ask.describe_tool("socialcrawl_call", tool_input, emit)
                emit.step(kind, text, platform=platform)
                found = socialcrawl_call(ctx, options.client, call["platform"], call["endpoint"],
                                         call.get("params", {}), max_credits=call.get("max_credits", 5),
                                         warehouse=options.warehouse)
                tally["suppressed_live"] += found.get("suppressed", 0)
            elif tool == "get_comments":
                emit.step("read", "Reading the comments under a post")
                get_comments(ctx, options.client, call["evidence_id"], call.get("limit", 20),
                             call.get("max_credits", 1.0))
            else:
                raise ValueError(f"fixture {fx['id']}: unknown research tool {tool!r}")
        t = tuple(fx["research"].get("tokens", (60_000, 3_000)))
        return {"note": fx["research"].get("note", ""), "tokens": {"input": t[0], "output": t[1]},
                "usd": _usd(options.model, t)}

    return research


def _projection(record, fx):
    """The record as a reader sees it once the creators in hidden_after are hidden, and what the projection withheld."""
    keys = hidden_keys(fx.get("hidden_after"))
    out = privacy.project_record(record, None, hidden=(keys, set(), set()))
    marker = out.get("privacy") or {}
    return out, {"state": marker.get("state", "none"),
                 "withheld": marker.get("withheld", {"posts": 0, "claims": 0, "summary": False})}


def _visible(handles, value):
    text = json.dumps(value, default=str).lower()
    return sorted(h for h in handles if h.lstrip("@").lower() in text)


def run_question(qid, library=None):
    """Replay one question and return everything the scorer and the report read."""
    library = library or questions_by_id()
    fx = load(qid)
    vars_ = question_vars(fx, library)
    market = vars_["markets"][0] if len(vars_["markets"]) == 1 else None
    tier = fx.get("tier", "T1")
    labels, on_topic, tally = {}, set(), {"suppressed_live": 0}
    warehouse, tables = ReplayWarehouse(fx, market), RecordingTables()
    client = ReplayClient(fx.get("socialcrawl", {}))
    model = ReplayModel(fx, labels)
    events = []
    deps = ask.Deps(warehouse=warehouse, socialcrawl=lambda mode, run_id=None: client, tables=tables, model=model,
                    research=make_research(fx, labels, on_topic, tally), now=lambda: AS_OF, check=None,
                    spent_today_usd=lambda: 0.0)
    ask_id = f"a_replay_{qid}"
    result = {"id": qid, "family": vars_["family"], "market": market, "tier": tier, "vars": vars_,
              "note": fx.get("note", ""), "error": None, "breaches": []}
    try:
        out = ask.run_ask({"question": re.sub(r"\s+", " ", vars_["question"]).strip(), "tier": tier, "mode": "replay",
                           "ask_id": ask_id}, events.append, lambda: False, deps=deps)
    except Exception as exc:  # a fault in the replay or the pipeline is reported, and the other questions still run
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["breaches"].append({"kind": "error", "detail": result["error"]})
        return result
    finally:
        ask.recorded(ask_id)
    answer, run = out["answer"], out["run"]
    record = {"ask_id": ask_id, "status": "complete", "answer": answer, "run": run}
    projected, privacy_row = _projection(record, fx)
    before = [h["handle"] for h in fx.get("hidden_before", [])]
    after = [h["handle"] for h in fx.get("hidden_after", [])]
    result.update({
        "answer": answer, "run": run, "answer_meta": out.get("answer_meta"), "claim_checks": tables.claim_checks(),
        "events": events, "on_topic_ids": sorted(on_topic), "hold_usd": ask.hold_usd(run["tier"], ask.MODEL),
        "model": ask.MODEL,
        "privacy": {"hidden_before": len(before), "hidden_after": len(after),
                    "suppressed_at_read": tally["suppressed_live"], **privacy_row},
        "projected": projected,
    })
    result["contract"] = validate_answer(answer)
    result["numbers"] = score.number_receipts(answer, out["query_receipts"])
    result["meta_problem"] = answer_state.verify_stored({**record, "answer_meta": out.get("answer_meta")})
    result["visible_before"] = _visible(before, {"answer": answer, "events": events})
    result["visible_after"] = _visible(after, {k: v for k, v in projected.items() if k != "privacy"})
    return result


def breaches(r):
    """The expectation-free faults of one replayed question. Empty is the only healthy value."""
    if r["error"]:
        return list(r["breaches"])
    found = []
    if r["contract"]:
        found.append({"kind": "contract", "detail": "; ".join(r["contract"][:3])})
    for metric in score.gate_leaks(r["checks"]):
        found.append({"kind": "gate_leak", "detail": f"a claim the gate kept fails {metric}: "
                                                     f"{r['checks'][metric]['reason']}"})
    for problem in r["numbers"]["problems"]:
        found.append({"kind": "number_hash", "detail": problem})
    if float(r["run"]["model_usd"]) > r["hold_usd"]:
        found.append({"kind": "over_hold", "detail": f"model spend {r['run']['model_usd']:.4f} is over the hold "
                                                     f"{r['hold_usd']:.4f}"})
    if r["meta_problem"]:
        found.append({"kind": "answer_meta", "detail": f"the stored summary state does not verify: {r['meta_problem']}"})
    if r["visible_before"] or r["visible_after"]:
        found.append({"kind": "privacy", "detail": "a hidden creator is visible to a reader: "
                                                   + ", ".join(r["visible_before"] + r["visible_after"])})
    return found


def replay(only=None):
    """Run the ten (or the ones named) and score them. Returns the results in the plan's order."""
    library = questions_by_id()
    wanted = [q for q in ORDER if not only or q in only]
    results = [run_question(q, library) for q in wanted]
    live = [r for r in results if not r["error"]]
    checks = score.deterministic_checks([(r["answer"], r["vars"]) for r in live]) if live else []
    for r, c in zip(live, checks):
        r["checks"] = c
        r["measures"] = score.measures(r)
        r["outcomes"] = score.outcomes(r["answer"], r["claim_checks"], r["events"])
        r["review"] = score.review_flags(r["answer"], r["market"])
        r["breaches"] = breaches(r)
    view = (score.score_run_view([(r["id"], r["vars"], r["answer"], r["checks"]) for r in live])
            if live else None)
    return results, view


def sheet_claims(results):
    """The kept claims of every replayed answer in the shape the weekly review draws its sample from."""
    pool = []
    for r in results:
        if r["error"]:
            continue
        cited = {e["id"]: e for e in r["answer"]["evidence"]}
        for c in r["answer"]["claims"]:
            quotes = " / ".join(q["text"] for q in c.get("quotes", []))
            pool.append({"id": f"{r['id']}:{c['id']}", "market": r["market"], "text": c["text"], "quote": quotes,
                         "url": cited[c["evidence_ids"][0]]["url"],
                         "numbers": [{"query": n["query_id"]} for n in c.get("numbers", [])]})
    return pool


def review_sheet(results):
    return review.review_sheet(review.claim_sample(sheet_claims(results), week="replay", seed="ask-replay"))


def summary(results, view):
    """The compact, repeatable record of a replay: no run ids, no clock, so two runs of the same code write the same
    file and a diff of the file is a diff of behaviour."""
    rows = []
    for r in results:
        if r["error"]:
            rows.append({"id": r["id"], "error": r["error"], "breaches": r["breaches"]})
            continue
        sums = score.cited(r["answer"])
        rows.append({
            "id": r["id"], "family": r["family"], "market": r["market"], "tier": r["run"]["tier"],
            "window": r["run"]["window"], "status": r["answer"]["status"], "note": r["note"],
            "claims": {"drafted": r["outcomes"]["drafted"], "kept": r["outcomes"]["kept"],
                       "downgraded": r["outcomes"]["downgraded"], "cut": r["outcomes"]["cut"]},
            "cited": {k: sums[k] for k in ("posts", "creators", "platforms", "comment_claims")},
            "privacy": r["privacy"],
            "cost": {"model_usd": round(float(r["run"]["model_usd"]), 4), "hold_usd": round(r["hold_usd"], 4),
                     "credits": r["run"]["credits"], "model": r["model"]},
            "measures": r["measures"],
            "checks": {m: {"pass": c["pass"], "reason": c["reason"]} for m, c in r["checks"].items()},
            "numbers": r["numbers"],
            "read_first": r["review"]["read_first"], "review_scope": r["review"]["scope"],
            "breaches": r["breaches"],
        })
    return {"version": 1, "as_of": AS_OF.isoformat(), "questions": rows, "score_run": view,
            "meaning": score.MEANING, "breached": [r["id"] for r in results if r["breaches"]]}


def render_markdown(s):
    lines = ["# Ask replay, ten benchmark questions", "",
             f"Replayed offline at {s['as_of']} through run_ask with recorded fixtures. No cloud, no model call, no "
             "credit. Breaches: " + (", ".join(s["breached"]) if s["breached"] else "none") + ".", "",
             "| Id | Market | Status | Drafted | Kept | Cut | Downgraded | Posts | Creators | Platforms | "
             "Privacy withheld | Model USD | Hold USD |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for q in s["questions"]:
        if "error" in q:
            lines.append(f"| {q['id']} | error | {q['error']} |")
            continue
        w = q["privacy"]["withheld"]
        lines.append(
            f"| {q['id']} | {q['market']} | {q['status']} | {q['claims']['drafted']} | {len(q['claims']['kept'])} | "
            f"{len(q['claims']['cut'])} | {len(q['claims']['downgraded'])} | {q['cited']['posts']} | "
            f"{q['cited']['creators']} | {q['cited']['platforms']} | posts {w['posts']}, claims {w['claims']} | "
            f"{q['cost']['model_usd']:.4f} | {q['cost']['hold_usd']:.4f} |")
    for q in s["questions"]:
        if "error" in q:
            continue
        lines += ["", f"## {q['id']} ({q['market']}, {q['family']})", "", q["note"], ""]
        for cut in q["claims"]["cut"]:
            lines.append(f"- Cut {cut['claim_id']} by {', '.join(cut['rules']) or 'no rule'}: {cut['reason']}")
        if q["claims"]["downgraded"]:
            lines.append("- Label lowered: " + ", ".join(q["claims"]["downgraded"]))
        p, w = q["privacy"], q["privacy"]["withheld"]
        lines.append(f"- Privacy: {p['hidden_before']} creators hidden while it ran ({p['suppressed_at_read']} live "
                     f"items left out); {p['hidden_after']} hidden after it was stored (state {p['state']}): withheld "
                     f"{w['posts']} posts, {w['claims']} claims, summary {'withheld' if w['summary'] else 'kept'}")
        lines.append("- Promptfoo asserts: " + ", ".join(
            f"{m} {'pass' if c['pass'] else 'FAIL'}" for m, c in q["checks"].items()))
        lines += [f"  - {m}: {c['reason']}" for m, c in q["checks"].items() if not c["pass"]]
        n = q["numbers"]
        lines.append(f"- Numbers: {n['recomputed']} of {n['numbers']} carry a hash recomputed from their query rows")
        lines.append("- Read first: " + ", ".join(
            f"{r['claim_id']} (risk {r['risk']}{', non-English' if r['non_english'] else ''})"
            for r in q["read_first"]))
        lines += ["", "| Measure | Value | Target | Result | What it means offline |", "|---|---|---|---|---|"]
        for m in q["measures"]:
            lines.append(f"| {m['name']} | {json.dumps(m['value'])} | {json.dumps(m['target'])} | {m['result']} | "
                         f"{m['meaning']} |")
        for b in q["breaches"]:
            lines.append(f"- BREACH {b['kind']}: {b['detail']}")
    v = s["score_run"]
    if v:
        lines += ["", "## score_run on these answers", "",
                  f"Citation integrity: {v['trust']['citation_integrity']['passed']} of "
                  f"{v['trust']['citation_integrity']['checked']}. Number pinning by score_run, which checks only that a hash "
                  f"is shaped like a sha256: {v['trust']['number_pinning']['pinned']} of "
                  f"{v['trust']['number_pinning']['numbers']}; the hashes recomputed from the query rows are under "
                  f"each question. Fabricated citations: {v['fabricated_citations'] or 'none'}. "
                  f"False full refusals: {v['false_full_refusals'] or 'none'}.",
                  "Not graded offline: " + ", ".join(v["ungraded"]) + ". Only the staging run grades these."]
    groups = {"offline": [], "fixture": [], "live": []}
    for name, kind in s["meaning"].items():
        groups[kind].append(name)
    lines += ["", "## What the replay can and cannot say", "",
              "Meaningful offline (the code did the work): " + ", ".join(sorted(groups["offline"])) + ".",
              "Fixture-bound (the code counted, the number is the fixture's): " + ", ".join(sorted(groups["fixture"])) + ".",
              "Only the live evaluation can give: " + ", ".join(sorted(groups["live"])) + ".", "",
              "What this replay does not run:"] + [f"- {limit}" for limit in LIMITS]
    return "\n".join(lines) + "\n"


def write_report(out, results, view):
    """Write the summary, its Markdown, the review sheet and one answer file per question into out. Never a
    score-*.json file: see ask_replay_score.score_file_names."""
    out = Path(out)
    (out / "answers").mkdir(parents=True, exist_ok=True)
    s = summary(results, view)
    (out / "ask-replay.json").write_text(json.dumps(s, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    (out / "ask-replay.md").write_text(render_markdown(s), encoding="utf-8")
    (out / "review-sheet.csv").write_text(review_sheet(results), encoding="utf-8", newline="")
    for r in results:
        if not r["error"]:
            (out / "answers" / f"{r['id']}.json").write_text(
                json.dumps({"answer": r["answer"], "run": r["run"], "projected": r["projected"]}, indent=2,
                           ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    if score.score_file_names(out) or quality_score.latest_run(out, date(2026, 10, 5)):
        raise RuntimeError("the replay folder holds a file the weekly quality score would read")
    return s


def main(argv=None):
    p = argparse.ArgumentParser(description="Replay the ten Ask benchmark questions offline and score them.")
    p.add_argument("out", help="folder for the report")
    p.add_argument("ids", nargs="*", help="replay only these ids; all ten when none is given")
    a = p.parse_args(argv)
    results, view = replay(a.ids)
    s = write_report(a.out, results, view)
    for q in s["questions"]:
        print(f"{q['id']}: " + (q["error"] if "error" in q else
              f"{q['status']}, kept {len(q['claims']['kept'])} of {q['claims']['drafted']}, "
              f"{len(q['breaches'])} breaches"))
    print(f"{'BREACH' if s['breached'] else 'CLEAN'}: {Path(a.out) / 'ask-replay.md'}")
    return 1 if s["breached"] else 0


if __name__ == "__main__":
    sys.exit(main())
