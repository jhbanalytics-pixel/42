"""Investigations: the money guard, plan checks and storage rows behind f42-agent's routes (contract.md 13.1).

Money comes first. Credits left under ASK_DAILY and model dollars left under MODEL_DAILY_USD are read before a
plan is drafted, edited or started, less the ceilings reserved for investigations still running. A budget that
cannot be read is unknown, and unknown never counts as money available.
"""
import json
import logging
import math
import os
import secrets
import threading

from core.api.today import PLATFORM_IDS, PLATFORM_WORDS
from core.config import caps as caps_config

log = logging.getLogger("f42-agent")

DEFAULT_ASK_DAILY = 600  # SETUP.md caps table: credits, all questions and investigations together
PLANNER_USD = 0.50  # the planner's own model ceiling (contract.md 13.3)
MAX_BYTES = 2 * 1024 ** 3
ASK_JOBS = ("ask", "investigation")
STATUSES = ("draft", "running", "complete", "stopped", "failed")
PLATFORMS = tuple(p for p in PLATFORM_WORDS if p not in PLATFORM_IDS)  # creator platforms (hiding a creator)
# The platform groups the agent's researchers search (core/agent/ask.py PLATFORM_GROUPS; a test keeps them equal).
# core.agent is not imported here, so f42-api can load this module without the agent's dependencies.
PLATFORM_LABELS = {"tiktok": "TikTok", "x": "X", "instagram": "Instagram", "youtube": "YouTube",
                   "facebook": "Facebook", "reddit_threads": "Reddit and Threads", "news": "news and web search"}
PLAN_PLATFORMS = tuple(PLATFORM_LABELS)
# Names older plans and clients used, read as the group that searches them.
LEGACY_PLATFORMS = {"twitter": "x", "reddit": "reddit_threads", "threads": "reddit_threads"}
# Platforms 42 collects but no researcher can search, so a plan cannot ask for them.
NOT_SEARCHABLE = {"telegram": "Telegram", "apple_music": "Apple Music"}
MAX_SUB_QUESTIONS = 8
ANGLE_MIN, ANGLE_CHARS = 3, 300  # the planner's bounds for an angle (core/agent/investigate.py ANGLE_CHARS)
MAX_TEXT = 500
MAX_RESEARCHERS = MAX_SUB_QUESTIONS
ROUND_USD = 4

MODEL_SPENT = "Today's model budget is spent; try tomorrow or ask Albert to raise MODEL_DAILY_USD"
BUDGET_UNKNOWN = ("Today's spend could not be read, so 42 will not {what} an investigation that might overspend; "
                  "try again in a few minutes.")
NEED_T3 = "Investigations need the T3 agent"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def caps():
    model_cap = caps_config.model_daily_usd()
    override = os.environ.get("MODEL_DAILY_USD")
    if override:
        model_cap = min(model_cap, float(override))
    return {"credits": float(os.environ.get("ASK_DAILY") or DEFAULT_ASK_DAILY), "model_usd": model_cap}


def budget_left(spent, reserved):
    """Credits and model dollars left today after spend and reservations, or None when the spend is unknown."""
    if not spent or not _number(spent.get("credits")) or not _number(spent.get("model_usd")):
        return None
    cap = caps()
    credits = cap["credits"] - spent["credits"] - reserved["credits"]
    usd = cap["model_usd"] - spent["model_usd"] - reserved["model_usd"]
    return {"credits": max(0, math.floor(credits + 1e-9)), "model_usd": max(0.0, round(usd, ROUND_USD))}


def spent_in_rows(rows, day):
    """Today's spend from runs rows held in memory: model dollars over every stage, credits over asks.

    A run can append several rows, so each run counts once at its highest figure. An ask row keeps its
    spend inside the record JSON (contract.md section 7), so that is read when the row has no model_usd."""
    usd, credits = {}, {}
    for row in rows:
        if row.get("run_date") != day:
            continue
        value = row.get("model_usd")
        if value is None:
            record = _json(row.get("record")) or {}
            value = (record.get("run") or {}).get("model_usd")
        if value is None:
            counts = _json(row.get("counts")) or {}
            value = counts.get("model_usd")
        if _number(value):
            run_id = row["run_id"]
            if run_id not in usd or value > usd[run_id]:
                usd[run_id] = value
        if row.get("stage") in ASK_JOBS and _number(row.get("credits")):
            credits[row["run_id"]] = max(credits.get(row["run_id"], 0), row["credits"])
    return {"credits": sum(credits.values()), "model_usd": round(sum(usd.values()), ROUND_USD)}


def _rows(client, sql, params):
    from google.cloud import bigquery
    config = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=MAX_BYTES)
    return [dict(row) for row in client.query(sql, job_config=config).result()]


def _ledger_credits(client, project, day):
    from google.cloud import bigquery
    rows = _rows(client,
                 f"SELECT SUM(l.credits_charged) AS credits FROM `{project}.intelligence_42_core.credit_ledger` l "
                 "WHERE l.trend_date = @d AND l.job IN UNNEST(@jobs)",
                 [bigquery.ScalarQueryParameter("d", "DATE", day),
                  bigquery.ArrayQueryParameter("jobs", "STRING", list(ASK_JOBS))])
    return float(rows[0]["credits"] or 0) if rows else 0.0


def runs_columns(client, project):
    """The column names intelligence_42_agent.runs has now (none while the table does not exist)."""
    return {row["column_name"] for row in _rows(
        client, f"SELECT c.column_name FROM `{project}.intelligence_42_agent.INFORMATION_SCHEMA.COLUMNS` c "
                "WHERE c.table_name = 'runs'", [])}


def _runs_usd(client, project, day):
    from google.cloud import bigquery
    columns = runs_columns(client, project)
    parts = []
    if "model_usd" in columns:
        parts.append("r.model_usd")
    if "record" in columns:
        parts.append("SAFE_CAST(JSON_VALUE(r.record, '$.run.model_usd') AS FLOAT64)")
    if "counts" in columns:
        parts.append("SAFE_CAST(JSON_VALUE(r.counts, '$.model_usd') AS FLOAT64)")
    if not parts:
        log.warning("intelligence_42_agent.runs has no spend column; model spend is unknown")
        return None
    usd = parts[0] if len(parts) == 1 else f"COALESCE({', '.join(parts)})"
    rows = _rows(client,
                 f"SELECT SUM(u.usd) AS usd FROM (SELECT r.run_id, MAX({usd}) AS usd "
                 f"FROM `{project}.intelligence_42_agent.runs` r WHERE r.run_date = @d GROUP BY r.run_id) u",
                 [bigquery.ScalarQueryParameter("d", "DATE", day)])
    return round(float(rows[0]["usd"] or 0), ROUND_USD) if rows else 0.0


def spent_in_bigquery(client, project, day):
    """Today's spend from credit_ledger and runs. Any read that fails comes back None (unknown)."""
    out = {}
    for key, read in (("credits", _ledger_credits), ("model_usd", _runs_usd)):
        try:
            out[key] = read(client, project, day)
        except Exception as exc:  # NotFound, BadRequest, unreachable: none of them means money is available
            log.warning("reading today's %s spend failed (%s); treating it as unknown", key, type(exc).__name__)
            out[key] = None
    return out


class Reservations:
    """Ceilings granted to investigations still running (and USD 0.50 per plan being drafted), held until each ends."""

    def __init__(self):
        self._held = {}
        self._lock = threading.Lock()

    def add(self, key, ceilings):
        with self._lock:
            self._held[key] = {"credits": ceilings.get("credits", 0), "model_usd": ceilings.get("model_usd", 0)}

    def release(self, key):
        with self._lock:
            self._held.pop(key, None)

    def total(self):
        with self._lock:
            held = list(self._held.values())
        return {"credits": sum(h["credits"] for h in held),
                "model_usd": round(sum(h["model_usd"] for h in held), ROUND_USD)}

    def clear(self):
        with self._lock:
            self._held.clear()


RESERVED = Reservations()


def refusal(estimate, left):
    """The words for whichever budget the estimate breaks, or None when it fits both."""
    broken, caps_named = [], []
    if estimate["credits"] > left["credits"]:
        broken.append(f"This plan needs about {estimate['credits']:,} credits but only {left['credits']:,} are left "
                      "today under ASK_DAILY")
        caps_named.append("ASK_DAILY")
    if estimate["model_usd"] > left["model_usd"]:
        broken.append(f"This plan needs about USD {estimate['model_usd']:.2f} of model spend but only "
                      f"USD {left['model_usd']:.2f} is left today under MODEL_DAILY_USD")
        caps_named.append("MODEL_DAILY_USD")
    if not broken:
        return None
    return "; ".join(broken) + f". Trim the plan, try tomorrow or ask Albert to raise {' and '.join(caps_named)} for today."


def ceilings(plan, left):
    """What run_ask may spend: the lower of the plan's ceiling and the budget left now (net of reservations)."""
    return {"max_credits": min(plan["max_credits"], left["credits"]),
            "max_model_usd": round(min(plan["max_model_usd"], left["model_usd"]), ROUND_USD)}


def clamp(plan, left):
    """The plan with its ceilings no higher than the budgets left when it was made."""
    return {**plan, **ceilings(plan, left)}


def _text(value, limit=MAX_TEXT):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def angle_ok(value):
    """An angle the planner takes: 3 to 300 characters once trimmed, so a draft refuses it before any spend."""
    return isinstance(value, str) and ANGLE_MIN <= len(value.strip()) <= ANGLE_CHARS


def _credits(value):
    """value rounded to cents when it is a finite number, 0 or more; None otherwise."""
    if not _number(value) or not math.isfinite(value) or value < 0:
        return None
    return round(value, 2)


def _platform_list():
    return ", ".join(PLATFORM_LABELS[p] for p in PLAN_PLATFORMS)


def _sub_question(raw, seen):
    if not isinstance(raw, dict):
        return None, "Each sub-question must be an object with id, text, platforms and credits."
    qid, text, platforms = raw.get("id"), raw.get("text"), raw.get("platforms")
    if not _text(qid, 20) or qid in seen:
        return None, "Each sub-question needs its own id, up to 20 characters."
    if not _text(text):
        return None, f"Sub-question {qid} needs text, 1 to {MAX_TEXT} characters."
    if not isinstance(platforms, list) or not platforms:
        return None, f"Sub-question {qid} needs at least one platform."
    named = []
    for p in platforms:
        p = LEGACY_PLATFORMS.get(p, p) if isinstance(p, str) else p
        if p in NOT_SEARCHABLE:
            return None, (f"Sub-question {qid}: 42 cannot search {NOT_SEARCHABLE[p]} in an investigation; "
                          f"choose from {_platform_list()}.")
        if p not in PLAN_PLATFORMS:
            return None, f"Sub-question {qid}: platforms must be from {_platform_list()}."
        if p not in named:
            named.append(p)
    credits = _credits(raw.get("credits"))
    if credits is None:
        return None, f"Sub-question {qid}: credits must be a number, 0 or more."
    return {"id": qid, "text": text.strip(), "platforms": named, "credits": credits}, None


def validate_plan(plan):
    """(clean plan, None) keeping only the plan's own keys, or (None, the reason in plain words). It accepts the plan
    the agent's planner drafts and the agent's own validate_plan runs: its platform group ids and credits as any
    number, 0 or more, kept to two decimals."""
    if not isinstance(plan, dict):
        return None, "plan must be an object."
    raw = plan.get("sub_questions")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SUB_QUESTIONS:
        return None, f"A plan needs 1 to {MAX_SUB_QUESTIONS} sub-questions."
    subs, seen = [], set()
    for item in raw:
        sub, why = _sub_question(item, seen)
        if why:
            return None, why
        seen.add(sub["id"])
        subs.append(sub)
    if not isinstance(plan.get("gap_round"), bool):
        return None, "gap_round must be true or false."
    max_credits, max_usd = _credits(plan.get("max_credits")), plan.get("max_model_usd")
    if max_credits is None:
        return None, "max_credits must be a number, 0 or more."
    if not _number(max_usd) or not math.isfinite(max_usd) or max_usd < 0:
        return None, "max_model_usd must be a number, 0 or more."
    return {"sub_questions": subs, "researchers": len(subs), "gap_round": plan["gap_round"],
            "max_credits": max_credits, "max_model_usd": max_usd}, None


def validate_estimate(estimate):
    if not isinstance(estimate, dict) or not all(
            _number(estimate.get(k)) and estimate[k] >= 0 for k in ("credits", "model_usd", "minutes")):
        return None
    return {"credits": estimate["credits"], "model_usd": estimate["model_usd"], "minutes": estimate["minutes"]}


def fixture_estimate(plan):
    """Stands in for L3's estimate_plan (no model call) when F42_AGENT=fixture."""
    n = len(plan["sub_questions"])
    return {"credits": sum(q["credits"] for q in plan["sub_questions"]), "model_usd": round(0.5 * n, 2),
            "minutes": 2 * n}


def fixture_planner(request):
    """Stands in for L3's plan_investigation when F42_AGENT=fixture: a fixed three-part plan and a small spend."""
    plan = {"sub_questions": [
        {"id": "q1", "text": "Where did it start, and who posted about it first?", "platforms": ["tiktok", "x"],
         "credits": 120},
        {"id": "q2", "text": "Which communities picked it up, in which languages and places?",
         "platforms": ["tiktok", "instagram"], "credits": 100},
        {"id": "q3", "text": "Is it still growing, and what do people say about it?",
         "platforms": ["x", "reddit_threads", "youtube"], "credits": 80}],
        "researchers": 3, "gap_round": True, "max_credits": 400, "max_model_usd": 3.0}
    run = {"run_id": f"r_plan_{secrets.token_hex(4)}", "tier": "T3", "credits": 0, "model_usd": 0.04,
           "tokens": {"input": 3000, "output": 800}, "seconds": 2}
    return {"plan": plan, "estimate": fixture_estimate(plan), "run": run}


def storage_row(investigation_id, version, created_at, status, question, market, plan, estimate, ask_id, run_id):
    """One append-only row of intelligence_42_agent.investigations."""
    return {"investigation_id": investigation_id, "version": version, "created_at": created_at, "who": "passcode",
            "status": status, "question": question, "market": market, "plan": json.dumps(plan),
            "estimate": json.dumps(estimate), "ask_id": ask_id, "run_id": run_id}


def view(row):
    """A stored row as the API shows it: plan and estimate as objects, times as ISO strings."""
    out = dict(row)
    out["plan"], out["estimate"] = _json(out["plan"]), _json(out["estimate"])
    if hasattr(out.get("created_at"), "isoformat"):
        out["created_at"] = out["created_at"].isoformat()
    return out
