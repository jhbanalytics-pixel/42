"""Replay of one past brief date through the explanation step: builds that date's candidates as this source would
(core/brief/sql/brief.sql pattern-first selection, job._candidates, the gate, job._merge folding), then runs the
brief's own writer, support check and critic on them (job._explain_one, core/brief/explain.py explain_trend) and
reports what the unchanged checks did. Run from the repository root:
    py -3.13 -m ops.runners.replay_explain YYYY-MM-DD [--markets ZA,NG,KE] [--max-candidates 30] [--max-usd 5]
        [--out FOLDER] [--paid TOKEN]
Needs google-cloud-bigquery and gcloud ADC on this PC; reads run as f42-builder through impersonation (RULES 13,
diagnose_selection.make_client). SELECT only, each query capped at 2 GB billed: the client refuses any statement
that is not a single SELECT, and any insert, load or other call, before it is sent. Nothing is written to the cloud:
no briefs or claim_checks rows, no ledger booking, no confirm search.

Without --paid it is a dry run with no model call: it prints the plan (per market, the candidates that would be
judged after folding, in the order the brief judges them, with rank, kind, pattern bucket, post count and what folded
into them), the worst-case model cost of the plan and a plan token. The paid run needs Albert's approval of that
plan: --paid TOKEN builds the plan again and refuses unless the token matches it exactly, so what runs is what was
approved; the token binds the date, markets, caps, the candidates, the model and the plan's worst-case estimate.
It then judges at most --max-candidates (at most 30) and stops before any model call whose estimate
(explain._estimate_usd, the brief's own) would take the run's spend past --max-usd. Each call is one request with
GEMINI_TIMEOUT_S and no SDK retry; the spend so far is the usage the model client reported, or the call's estimate
where it reported none, a failed or timed-out call included. A further attempt of a check that gave nothing back is
allowed only while the spend is under the cap, and the meter checks it like any other call.

Where the replay differs from the live brief (DIFFERENCES, printed with the plan):
- confirm searches are skipped (they spend SocialCrawl credit), so packs carry no "Also found by search" lines and a
  candidate held only by the post floors is not searched and regrown;
- packs are read from today's warehouse, which may hold posts a later confirm or collection added after that date;
- there is no _Breaker: a busy model is not waited out, its failure stands for that candidate;
- the day's spend already booked in the warehouse is not read: the run's own cap applies instead of the daily one.

Prints titles, counts, the critic's flags and fixed wording only. Model text (the critic's named explanation and
reason) is masked and cut as check_support_holds.model_text does, with links masked too; no post text, handles or
URLs. --out also writes the same as JSON there (keep it inside the approved local boundary; it names items and
titles)."""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from google.cloud import bigquery

from core.brief import explain, gatectx, job
from core.brief.evidence import read_hidden
from core.config.caps import model_daily_usd
from core.detect.sqlrun import AGENT, CORE
from core.llm.provider import default_model
from ops.runners.check_support_holds import model_text
from ops.runners.diagnose_selection import BUCKETS, CAP, make_client

MAX_CANDIDATES = 30
DEFAULT_USD = 5.0
# Worst case of one explain_trend with second_draft: the first draft, its one repair and the critic's second draft;
# three rounds of checks (first, repaired, second draft), each up to MAX_CLAIMS claims, the sentence and the critic,
# each tried twice; and the title's support check.
WRITER_CALLS = 3
CHECK_CALLS = 3 * (explain.MAX_CLAIMS + 2) * 2 + 1
# Bytes added to the first writer prompt to bound a later prompt that also carries a draft, faults or claims.
PROMPT_PAD = 32_000
DIFFERENCES = (
    "confirm searches skipped: no 'Also found by search' lines, floor-held candidates not regrown",
    "packs read from today's warehouse: may include posts added after the date by a later confirm or collection",
    "no _Breaker: a busy model is not waited out",
    "the day's booked spend is not read: the run's --max-usd cap applies instead of the daily cap",
)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_SUPPORT_VERDICT = re.compile(r"^(?:explanation sentence )?support check (supported|partial|unsupported):")


class Refused(RuntimeError):
    """A call the replay never makes: a write, a load, a stream or a statement that is not one SELECT."""


def select_only(sql):
    """Raises Refused unless sql is one SELECT (WITH ... SELECT included)."""
    body = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", str(sql))).strip().rstrip(";").strip()
    word = re.match(r"[A-Za-z]+", body)
    if word is None or word.group(0).upper() not in ("SELECT", "WITH") or ";" in body:
        raise Refused("replay_explain sends a single SELECT only")


class _Read:
    """A query job whose rows are kept: the candidate rows' pattern columns, which job._candidates drops."""

    def __init__(self, query_job, patterns, market):
        self._job, self._patterns, self._market = query_job, patterns, market

    def result(self, *args, **kwargs):
        rows = list(self._job.result(*args, **kwargs))
        for row in rows:
            keys = row.keys() if hasattr(row, "keys") else ()
            if "_selection_pattern" in keys:
                market = row["market"] if "market" in keys else self._market
                self._patterns[(market, row["item_id"])] = row["_selection_pattern"]
        return rows

    def __getattr__(self, name):
        return getattr(self._job, name)


class ReadOnlyClient:
    """The client the replay hands the job's functions: query() for one capped SELECT, and nothing else."""

    def __init__(self, client):
        self._client = client
        self.patterns = {}

    def query(self, sql, job_config=None, **kwargs):
        select_only(sql)
        config = job_config or bigquery.QueryJobConfig()
        config.maximum_bytes_billed = min(int(config.maximum_bytes_billed or CAP), CAP)
        market = next((p.value for p in getattr(config, "query_parameters", None) or ()
                       if getattr(p, "name", None) == "market"), None)
        return _Read(self._client.query(sql, job_config=config, **kwargs), self.patterns, market)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise Refused(f"replay_explain reads only; {name} is not sent")


class Meter:
    """The model client with the run's cap in front of every call. A call whose estimate would take the spend past
    max_usd is refused before it is sent; the spend is the reported usage, or the estimate where none was reported,
    a failed or timed-out call included."""

    def __init__(self, model, max_usd):
        self.model, self.max_usd = model, max_usd
        self.usd, self.estimated, self.calls, self.refused = 0.0, 0.0, [], False

    def room(self):
        """The retry guards: a further attempt only while the spend is under the cap and no call was refused."""
        return not self.refused and self.usd < self.max_usd

    def complete_json(self, **kw):
        estimate = explain._estimate_usd(kw["system"], kw["user"], kw["max_tokens"], kw["model"])
        if self.refused:
            raise Refused("a call was already refused at the run's cap; no further call is sent")
        if self.usd + estimate > self.max_usd:
            self.refused = True
            raise Refused(f"the next call (estimate USD {estimate:.4f}) would pass the run's cap")
        call = {"role": role(kw["schema"]), "estimate_usd": round(estimate, 6)}
        self.calls.append(call)
        self.estimated += estimate
        try:
            out, usage = self.model.complete_json(**kw)
        except Exception as exc:
            # A reply with known usage is booked at that usage; anything else (a timeout, a dropped connection, a
            # reply with no usage) at the estimate, since the request may have been billed.
            booked = exc.usd if getattr(exc, "usage", None) is not None and getattr(exc, "usd", None) is not None \
                else estimate
            self.usd += booked
            call.update(error=type(exc).__name__, usd=booked)
            raise
        usage = usage or {}
        reported = None if usage.get("usage_unknown") else usage.get("usd")
        self.usd += estimate if reported is None else reported
        call.update(usd=reported, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))
        return out, usage


def replay_model():
    """The brief's client with one request per call: GEMINI_TIMEOUT_S on the request and no SDK retry, so a call
    the meter let through is sent once and its estimate bounds it."""
    from core.llm.gemini import GeminiModel
    from core.llm.provider import provider

    provider()
    return GeminiModel(project=job.PROJECT, timeout_s=job.GEMINI_TIMEOUT_S, retries=0)


def role(schema):
    props = (schema or {}).get("properties") or {}
    return "critic" if "ruled_out" in props else "support" if "verdict" in props else "writer"


def worst_usd(cand, d, model_id):
    """The plan's estimate for one candidate: every call explain_trend can make, each priced on the first writer
    prompt with PROMPT_PAD added and its full output reserved."""
    start, end = job._window(d, cand["market"])
    user = explain._writer_user(cand["row"], cand["pack"], cand["market"], start, end) + " " * PROMPT_PAD
    writer = explain._estimate_usd(explain.WRITER_SYSTEM, user, explain.WRITER_MAX_TOKENS, model_id)
    check = max(explain._estimate_usd(s, user, explain.SUPPORT_MAX_TOKENS, model_id)
                for s in (explain.SUPPORT_SYSTEM, explain.CRITIC_SYSTEM))
    return WRITER_CALLS * writer + CHECK_CALLS * check


def handles(cand):
    return [str(e.get("handle") or "").strip().lstrip("@") for e in cand["pack"].get("evidence") or []]


def safe(text, cand):
    return model_text(None if text is None else _URL.sub("[link]", str(text)), handles(cand))


def title(cand):
    """The candidate's title without a creator's name or handle."""
    row = cand["row"]
    return "[creator]" if row.get("kind") == "creator" else safe(row.get("title"), cand)


def build(client, d, markets, max_candidates, *, core=CORE, agent=AGENT, build_ctx=gatectx.build_ctx):
    """The plan: candidates, gate and folding as the job builds them, without confirm. Returns (plan, tasks,
    by_market). tasks are the Today-bound candidates of the chosen markets in the brief's judging order, cut to
    max_candidates."""
    hidden = read_hidden(client, core=core, agent=agent)
    calendar = {m: job.moments(client, d, m, core, agent) for m in job.MARKETS}
    by_market = job._candidates(client, d, build_ctx=build_ctx, campaign_hashtags=gatectx.load_campaign_hashtags(),
                                political_terms={m: gatectx.load_political_terms(m) for m in job.MARKETS},
                                hidden=hidden, core=core, agent=agent, calendar=calendar)
    rank = {id(c): n for m in job.MARKETS for n, c in enumerate(by_market[m], 1)}
    every = {(c["market"], c["row"]["item_id"]): c for m in job.MARKETS for c in by_market[m]}
    merged = job._merge(by_market)
    order = [c for c in job._in_rank_order(by_market) if c["market"] in markets]
    tasks = order[:max_candidates]
    try:
        model_id = default_model()
        price_error = None
    except Exception as exc:
        model_id, price_error = None, f"{type(exc).__name__}: {exc}"

    def entry(c):
        pattern = client.patterns.get((c["market"], c["row"]["item_id"]))
        out = {"market": c["market"], "item_id": c["row"]["item_id"], "rank": rank[id(c)], "title": title(c),
               "kind": c["row"].get("kind"), "pattern": pattern, "pattern_words": BUCKETS.get(pattern),
               "posts": len(c.get("posts") or ()), "pack_posts": len(c["pack"].get("evidence") or ()),
               "folded_in": [m["item_id"] for m in merged if m["market"] == c["market"]
                             and m["into"] == c["row"]["item_id"]]}
        return out

    judge = [entry(c) for c in tasks]
    for e, c in zip(judge, tasks):
        try:
            e["worst_usd"] = round(worst_usd(c, d, model_id), 4) if model_id else None
        except Exception as exc:
            price_error = price_error or f"{type(exc).__name__}: {exc}"
            e["worst_usd"] = None
    folded = [{"market": m["market"], "item_id": m["item_id"], "into": m["into"],
               "title": title(every[(m["market"], m["item_id"])]),
               "pattern": client.patterns.get((m["market"], m["item_id"]))} for m in merged if m["market"] in markets]
    held = [{"market": c["market"], "item_id": c["row"]["item_id"], "rank": rank[id(c)], "title": title(c),
             "rule": c["decision"].rule, "where": c["decision"].where, "reason": c["decision"].reason}
            for m in markets for c in by_market[m] if not job._for_today(c)]
    plan = {"date": d.isoformat(), "markets": list(markets), "max_candidates": max_candidates,
            "model": model_id, "judge": judge, "over_cap": [entry(c)["item_id"] for c in order[max_candidates:]],
            "folded": folded, "not_judged": held,
            "worst_usd": None if price_error else round(sum(e["worst_usd"] for e in judge), 4),
            "price_error": price_error, "differences": list(DIFFERENCES),
            "confirm": "skipped: confirm searches spend vendor credit; candidates are judged on their packs as built"}
    return plan, tasks, by_market


def token(d, markets, max_candidates, max_usd, item_ids, model_id, worst):
    raw = json.dumps([d.isoformat(), list(markets), max_candidates, round(float(max_usd), 6), list(item_ids),
                      model_id, worst])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def support_counts(result):
    """Support verdicts of the final draft's claims and its sentence, and of the rows before the repair round."""
    out = {"claims": Counter(), "sentence": None, "before_repair": Counter()}
    for row in result.get("checks") or []:
        if row.get("rule") != "K4" or row.get("checker") != "model":
            continue
        detail = str(row.get("detail") or "")
        before = detail.startswith(job.REPAIR)
        found = _SUPPORT_VERDICT.match(detail[len(job.REPAIR):] if before else detail)
        verdict = found.group(1) if found else "incomplete"
        if before:
            out["before_repair"][verdict] += 1
        elif row.get("claim_id") is None:
            out["sentence"] = verdict
        else:
            out["claims"][verdict] += 1
    return {k: dict(v) if isinstance(v, Counter) else v for k, v in out.items()}


def critic_fields(critic, cand):
    if not isinstance(critic, dict):
        return None
    out = {k: critic.get(k) for k in explain.CRITIC_FIELDS if k not in ("non_cultural_explanation", "reason")}
    out["non_cultural_explanation"] = safe(critic.get("non_cultural_explanation"), cand)
    out["reason"] = safe(critic.get("reason"), cand)
    rivals = critic.get("code_rivals")
    if isinstance(rivals, dict) and "disagreement" in rivals:
        out["code_rivals_disagreement"] = rivals["disagreement"]
    return out


def outcomes(d, by_market, results, markets):
    """{(market, item_id): outcome} as the brief's payload would show each judged candidate."""
    for m in markets:
        for c in by_market[m]:
            if id(c) in results and job._for_today(c):
                c["decision"] = job._gate(c, results[id(c)]["reason"] is None)
    out = {}
    for m in markets:
        p = job._market_payload(m, d, by_market[m], results, banners=[], moments_=[], boards_=[], issues=[])
        for card in p["cards"] + p["more"]:
            out[(m, card["item_id"])] = {"outcome": "published"}
            for also in card.get("also") or []:
                out.setdefault((m, also["item_id"]), {"outcome": f"published, merged into {card['item_id']}"})
        for item in p["held_back"]["items"]:
            failed = item.get("failed_reason")
            out[(m, item["item_id"])] = {"outcome": "held", "rule": item.get("rule"), "reason": item.get("reason"),
                                         "reason_text": item.get("reason_text"), "failed_reason": failed,
                                         "held_by": str(failed).split(":")[0] if failed else item.get("rule")}
    return out


def judge(tasks, d, model, max_usd, max_candidates):
    """Explains tasks in order under the run's caps. Returns ({id(cand): result}, meter, stop or None)."""
    meter = Meter(model, max_usd)
    meter.capped_at = None
    results, stop = {}, None
    for n, cand in enumerate(tasks):
        if n >= min(max_candidates, MAX_CANDIDATES):
            stop = "candidate cap"
            break
        if meter.refused or meter.usd >= max_usd:
            stop = "USD cap"
            break
        # explain_trend refuses a call when the day's spend plus its estimate passes the daily cap; given the run's
        # remaining allowance as the room left under that cap, its own check stops at max_usd with reason model_cap.
        before = model_daily_usd() - (max_usd - meter.usd)
        spent = meter.usd
        results[id(cand)] = job._explain_one(cand, model=meter, spent_before=before, d=d,
                                             model_call_guard=lambda: True, retry_guard=meter.room,
                                             retry_504_guard=meter.room)
        results[id(cand)]["meter_usd"] = meter.usd - spent
        if meter.refused:
            meter.capped_at = id(cand)
        if results[id(cand)]["reason"] == "model_cap" or meter.refused:
            stop = "USD cap"
            break
    return results, meter, stop


def report(d, plan, tasks, by_market, results, meter, stop):
    shown = outcomes(d, by_market, results, plan["markets"]) if results else {}
    items = []
    for e, cand in zip(plan["judge"], tasks):
        result = results.get(id(cand))
        entry = dict(e)
        if result is None:
            entry.update(outcome="not judged", stopped=stop)
        elif id(cand) == meter.capped_at:
            # A refused call can end softly inside the explain step (a title support check not run), so the
            # candidate is reported as stopped whatever reason the step gave.
            entry.update(outcome="stopped: USD cap", error=safe(result.get("error"), cand))
        elif result["reason"] in ("model_cap", "model_error", "job_error"):
            entry.update(outcome=f"stopped: {result['reason']}", error=safe(result.get("error"), cand))
        else:
            entry.update(shown.get((cand["market"], cand["row"]["item_id"]), {"outcome": "not in payload"}))
        if result is not None:
            entry.update(explain_reason=result["reason"], usage_usd=round(result.get("usage_usd") or 0.0, 6),
                         meter_usd=round(result.get("meter_usd") or 0.0, 6),
                         support=support_counts(result), critic=critic_fields(result.get("critic"), cand))
        items.append(entry)
    summary = {}
    for m in plan["markets"]:
        mine = [i for i in items if i["market"] == m and "support" in i and not i["outcome"].startswith("stopped")]
        summary[m] = {"judged": len(mine), "published": sum(1 for i in mine if i["outcome"].startswith("published")),
                      "held_by": dict(Counter(i.get("held_by") or "?" for i in mine if i["outcome"] == "held")),
                      "pattern_0": sum(1 for i in mine if i["pattern"] == 0),
                      "stopped": sum(1 for i in items if i["market"] == m and i["outcome"].startswith("stopped"))}
    return {"items": items, "summary": summary, "stop": stop, "calls": meter.calls,
            "usd_reported_or_estimated": round(meter.usd, 6), "usd_estimated": round(meter.estimated, 6),
            "ledger": "not booked: the replay writes nothing to the warehouse"}


def show_plan(plan, plan_token, max_usd):
    print(f"== replay plan for {plan['date']}, markets {','.join(plan['markets'])}, model {plan['model']}")
    print("  differs from the live brief: " + "; ".join(plan["differences"]))
    for m in plan["markets"]:
        mine = [e for e in plan["judge"] if e["market"] == m]
        print(f"\n== {m}: {len(mine)} to judge, {sum(1 for e in plan['folded'] if e['market'] == m)} folded,"
              f" {sum(1 for e in plan['not_judged'] if e['market'] == m)} held or off Today before the model")
        for e in mine:
            print(f"  rank {e['rank']:>3}  {e['kind'] or '?':8} {(e['title'] or '')[:48]:48} bucket {e['pattern']}"
                  f" ({e['pattern_words']})  posts {e['posts']} (pack {e['pack_posts']})  worst USD {e['worst_usd']}")
            for f in e["folded_in"]:
                print(f"      folded in: {f}")
        for f in (f for f in plan["folded"] if f["market"] == m):
            print(f"  folded, not judged: {f['title'][:48]} [{f['item_id']}] into [{f['into']}]")
        for h in (h for h in plan["not_judged"] if h["market"] == m):
            print(f"  not judged: rank {h['rank']:>3} {(h['title'] or '')[:48]} {h['rule'] or h['where']}")
    if plan["over_cap"]:
        print(f"\n{len(plan['over_cap'])} Today-bound candidates are past --max-candidates and are not judged")
    if plan["price_error"]:
        print(f"\nworst-case cost: not priced ({plan['price_error']})")
    else:
        print(f"\nworst-case model cost of the plan: USD {plan['worst_usd']:.2f} for {len(plan['judge'])} candidates;"
              f" the run stops before any call that would pass USD {max_usd:.2f}")
    print(f"plan token: {plan_token}")


def show_report(out):
    for i in out["items"]:
        print(f"\n{i['market']} rank {i['rank']} {(i['title'] or '')[:60]}  [{i['item_id']}]  bucket {i['pattern']}")
        line = i["outcome"]
        if i["outcome"] == "held":
            line += f" by {i.get('held_by')} ({i.get('rule')} {i.get('reason')}): {i.get('failed_reason')}"
        print(f"  outcome: {line}")
        if "support" in i:
            print(f"  explain reason {i.get('explain_reason')}, usage USD {i.get('usage_usd')}, metered USD"
                  f" {i.get('meter_usd')} (a failed call at its estimate), support {i['support']}")
        critic = i.get("critic")
        if critic:
            flags = ", ".join(f"{k} {critic.get(k)}" for k in ("ruled_out", "news_driven", "scheduled_event",
                                                               "local_reaction", "local_why_now"))
            print(f"  critic: {flags}")
            print(f"    named: {critic['non_cultural_explanation']}")
            print(f"    reason: {critic['reason']}")
    print("\n== summary")
    for m, s in out["summary"].items():
        print(f"  {m}: judged {s['judged']}, published {s['published']}, held by check {s['held_by']},"
              f" pattern bucket 0 judged {s['pattern_0']}, stopped by a cap or error {s['stopped']}")
    print(f"  stop: {out['stop'] or 'none'}; {len(out['calls'])} model calls; spend USD"
          f" {out['usd_reported_or_estimated']:.4f} (reported usage, estimate where none), estimated before the calls"
          f" USD {out['usd_estimated']:.4f}; {out['ledger']}")


def main(argv=None, client=None, model=None, build_ctx=gatectx.build_ctx, core=CORE, agent=AGENT):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("date", type=dt.date.fromisoformat)
    ap.add_argument("--markets", default=",".join(job.MARKETS))
    ap.add_argument("--max-candidates", type=int, default=MAX_CANDIDATES)
    ap.add_argument("--max-usd", type=float, default=DEFAULT_USD)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--paid")
    args = ap.parse_args(argv)
    markets = tuple(m.strip().upper() for m in args.markets.split(",") if m.strip())
    if not markets or any(m not in job.MARKETS for m in markets) or len(set(markets)) != len(markets):
        print(f"--markets takes some of {','.join(job.MARKETS)}", file=sys.stderr)
        return 2
    if not 1 <= args.max_candidates <= MAX_CANDIDATES:
        print(f"--max-candidates must be 1 to {MAX_CANDIDATES}", file=sys.stderr)
        return 2
    if not 0 < args.max_usd < float("inf"):
        print("--max-usd must be a positive amount", file=sys.stderr)
        return 2
    d = args.date
    reader = ReadOnlyClient(client if client is not None else make_client())
    plan, tasks, by_market = build(reader, d, markets, args.max_candidates, core=core, agent=agent,
                                   build_ctx=build_ctx)
    plan_token = token(d, markets, args.max_candidates, args.max_usd, [e["item_id"] for e in plan["judge"]],
                       plan["model"], plan["worst_usd"])
    plan["token"], plan["max_usd"] = plan_token, args.max_usd
    show_plan(plan, plan_token, args.max_usd)
    out = {"plan": plan}
    code = 0
    if args.paid is None:
        print("dry run: no model call made. To run it, approve this plan and pass --paid with the token above.")
    elif plan["price_error"]:
        print(f"refused: the plan is not priced ({plan['price_error']}); nothing was sent to the model",
              file=sys.stderr)
        out["refused"] = "not priced"
        code = 1
    elif args.paid != plan_token:
        print(f"refused: --paid {args.paid} does not match this plan's token {plan_token}; nothing was sent to the"
              " model", file=sys.stderr)
        out["refused"] = "token mismatch"
        code = 1
    else:
        if model is None:
            model = replay_model()
        try:
            results, meter, stop = judge(tasks, d, model, args.max_usd, args.max_candidates)
        finally:
            close = getattr(model, "close_deadline_calls", None)
            if callable(close):
                close()
        out["run"] = report(d, plan, tasks, by_market, results, meter, stop)
        show_report(out["run"])
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        kind = "paid" if "run" in out else "plan"
        path = args.out / f"replay-{d.isoformat()}-{kind}-{plan_token}.json"
        path.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        print(f"wrote {path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
