"""42's fifteen tools with no model SDK attached: schemas, descriptions, the guards and the plain functions.

The Gemini function-calling loop in core/agent/gemini_research.py uses these. Nothing here imports google.genai.
"""

from __future__ import annotations

import importlib
import json
import re

from core.agent.context import Refused, RunContext
from core.agent.forecast_promotion import MARKET_NAMES, PERIODS, TEMPLATES
from core.agent.tools.dates import resolve_dates
from core.agent.tools.enrich_tools import get_comments, get_transcript, watch_video
from core.agent.tools.forecast import log_forecast
from core.agent.tools.history import MARKETS, MAX_K, analogues, history
from core.agent.tools.socialcrawl import (
    EVERYWHERE_EXCLUDE,
    SEARCH_EVERYWHERE,
    budget_status,
    check_route,
    normalise_route,
    socialcrawl_call,
)
from core.agent.tools.sql_query import QUERY_PAGE_ROWS, check_sql, query_preview, query_rows, sql_query, warehouse_map_text

TOOL_NAMES = ["sql_query", "search_posts", "socialcrawl_call", "rising_topics", "recall_findings", "save_finding",
              "budget_status", "resolve_dates", "get_comments", "get_transcript", "watch_video", "log_forecast",
              "query_rows", "history", "analogues"]
# A SocialCrawl failure can carry a URL with a key, so these tools report only the exception type.
SOCIALCRAWL_TOOLS = ("socialcrawl_call", "get_comments", "get_transcript", "watch_video")
_ERROR_CHARS = 300
# Rule 9: a URL or query string in an exception can carry a key, so neither reaches the model.
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S*", re.IGNORECASE)
_QUERY = re.compile(r"\?\S*")

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}
# A forecast is published only as the line built from its stored row (forecast_promotion), so the model is told
# that wording here, from the same templates, and a statement worded any other way stays cut.
_FORECAST_STATEMENT = (
    "Word it exactly as the line for its target: "
    + " ".join(f"{target}: \"{t.format(label='<item name>', market='<market name>', period='<period>')}\""
               for target, t in TEMPLATES.items())
    + " <item name> is the item's label, <market name> is one of " + ", ".join(MARKET_NAMES.values())
    + ", and <period> is " + ", ".join(f"\"{p}\" when horizon_days is {d}" for d, p in PERIODS.items())
    + ". Write no other numbers in it."
)


def _schema(properties: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


SCHEMAS = {
    "sql_query": _schema({
        "sql": {"type": "string", "description": "One read-only BigQuery SELECT over intelligence_42_core or "
                                                 "intelligence_42_agent, tables dataset-qualified."},
        "purpose": {"type": "string", "description": "What this number is for, in one plain line a strategist reads: "
                                                     "no digits, table or column names, as in 'top sounds last week'."},
        "params": {"type": "object", "description": "Named query parameters."},
        "max_bytes_billed": {"type": "integer", "minimum": 1},
    }, ["sql", "purpose"]),
    "search_posts": _schema({
        "query": _STR, "platforms": _STRS, "since": {"type": "string", "description": "YYYY-MM-DD"},
        "until": {"type": "string", "description": "YYYY-MM-DD"}, "min_engagement": {"type": "number", "minimum": 0},
        "author": _STR, "sort": _STR, "limit": {"type": "integer", "minimum": 1},
    }, ["query"]),
    "socialcrawl_call": _schema({
        "platform": _STR, "endpoint": _STR, "params": {"type": "object"},
        "max_credits": {"type": "number", "minimum": 0, "description": "Most credits this call may spend."},
        "cursor": _STR,
    }, ["platform", "endpoint", "max_credits"]),
    "rising_topics": _schema({
        "date": {"type": "string", "description": "YYYY-MM-DD"}, "window_days": {"type": "integer", "minimum": 1},
        "market": _STR, "kind": _STR, "min_platforms": {"type": "integer", "minimum": 1},
    }),
    "recall_findings": _schema({
        "query": _STR, "since": {"type": "string", "description": "YYYY-MM-DD"},
        "status": {"type": "string", "enum": ["current", "stale", "contradicted"]},
    }, ["query"]),
    "save_finding": _schema({
        "claim": _STR, "evidence_ids": {**_STRS, "description": "Evidence ids of posts this run stored."},
        "label": {"type": "string", "description": "observed, corroborated, single_source or inferred."},
        "topic": _STR, "query_ids": _STRS,
        "review_by": {"type": "string", "description": "YYYY-MM-DD"},
        "item_ids": {"type": "array", "items": {"type": "string"},
                     "description": "Only item ids that history, analogues or rising_topics returned in this run; "
                                    "ids from sql_query rows are refused. Leave it empty otherwise."},
        "contradicts": {"type": "array", "items": {"type": "string"},
                        "description": "Finding ids from recall_findings this claim contradicts, about the same "
                                       "item."},
    }, ["claim", "evidence_ids", "label", "topic", "query_ids", "review_by", "item_ids"]),
    "budget_status": _schema({}),
    "resolve_dates": _schema({"expression": {"type": "string", "description": "For example: last 7 days, "
                                                                              "yesterday, since 2026-09-01."}},
                             ["expression"]),
    "get_comments": _schema({
        "evidence_id": {"type": "string", "description": "A post's evidence id from this run."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "max_credits": {"type": "number", "minimum": 0, "description": "Most credits this call may spend."},
    }, ["evidence_id", "max_credits"]),
    "get_transcript": _schema({"evidence_id": {"type": "string", "description": "A video post's evidence id."}},
                              ["evidence_id"]),
    "watch_video": _schema({
        "evidence_id": {"type": "string", "description": "A TikTok, YouTube or Instagram video post's evidence id."},
        "question": {"type": "string", "description": "What to look for in the clip, in one plain question."},
    }, ["evidence_id", "question"]),
    "log_forecast": _schema({
        "item_id": _STR,
        "market": {"type": "string", "enum": ["ZA", "NG", "KE"]},
        "target": {"type": "string", "enum": ["reach_rising", "cross_market", "persist_50"]},
        "horizon_days": {"type": "integer", "enum": [7, 14]},
        "probability": {"type": "number", "minimum": 0, "maximum": 1},  # the tool refuses 0 and 1 itself
        "statement": {"type": "string", "description": _FORECAST_STATEMENT},
        "evidence_ids": _STRS,
    }, ["item_id", "market", "target", "horizon_days", "probability", "statement", "evidence_ids"]),
    "history": _schema({
        "item_id": _STR, "query": {"type": "string", "description": "Words in the item's label, if you have no id."},
        "market": {"type": "string", "enum": list(MARKETS)}, "since": {"type": "string", "description": "YYYY-MM-DD"},
    }),
    "analogues": _schema({
        "item_id": _STR, "text": {"type": "string", "description": "Words naming the item, if you have no id."},
        "market": {"type": "string", "enum": list(MARKETS)},
        "k": {"type": "integer", "minimum": 1, "maximum": MAX_K},
    }),
    "query_rows": _schema({
        "query_id": {**_STR, "description": "A query_id already recorded in this question."},
        "offset": {"type": "integer", "minimum": 0, "default": 0,
                   "description": "Zero-based position in the original recorded order."},
        "limit": {"type": "integer", "minimum": 1, "maximum": QUERY_PAGE_ROWS, "default": QUERY_PAGE_ROWS},
    }, ["query_id"]),
}

DESCRIPTIONS = {
    "sql_query": "Run one read-only query over 42's warehouse. Returns the first ten rows in recorded order, "
                 "query_id, bytes, result_hash, row_count, recorded_row_count, columns, truncated and preview. "
                 "row_count is accessible recorded rows (at most 500), not a corpus total. recorded_row_count "
                 "includes any over-limit sentinel covered by result_hash. truncated means further query rows "
                 "may exist. preview gives offset, limit, returned, has_more and next_offset. Use query_rows "
                 "to inspect any omitted visible row before reasoning from it; do not treat a preview as the "
                 "whole result. The writer and checks keep the original stored rows. "
                 "Cite the query_id for every number. Several independent counts go out as separate sql_query "
                 "calls in the same turn: up to four run at the same time, and each is checked and capped as if it "
                 "ran alone. Do not send them one per turn. Use only these names; any other table fails. "
                 + warehouse_map_text(),
    "search_posts": "Search stored posts by full text and meaning. Free. Returns evidence. Words in the query must all "
                    "appear in a post; put OR between alternatives, as in 'amapiano OR gqom'.",
    "socialcrawl_call": "Fetch fresh posts from SocialCrawl. Spends credits; check budget_status first. "
                        "Returns evidence ids, items, next_cursor, credits_spent and status.",
    "rising_topics": "Topics rising in the detection tables for a date and window.",
    "recall_findings": "Search saved findings. Status is current, stale (past its review date) or contradicted (a "
                       "later finding on the same item said otherwise).",
    "save_finding": "Save a finding with its evidence ids, query ids and item ids. Name in contradicts any recalled "
                    "finding on the same item it overturns. Returns the finding id. It is kept only if the answer "
                    "makes the same claim, word for word, and that claim passes the answer's checks.",
    "budget_status": "Credits and SocialCrawl calls left for this question.",
    "resolve_dates": "Turn a date expression into an inclusive from and to date. Never compute dates yourself.",
    "get_comments": "Fetch comments on a post already in this run's evidence. Spends enrichment credits (20% of the "
                    "budget, top 20 items). Returns comment evidence ids you can cite.",
    "get_transcript": "Fetch a video post's transcript as timed segments. Spends enrichment credits; a second call "
                      "for the same post is free.",
    "watch_video": "Watch a video post already in this run's evidence with a question in mind. Returns its format, "
                   "hook, on-screen text, setting, people, brands, sound, edit style and observations with times in "
                   "seconds. Spends enrichment credits (20% of the budget, top 20 items) on its transcript and "
                   "screen text, and model spend.",
    "log_forecast": "Log a forecast about an item: reach_rising, cross_market or persist_50 over 7 or 14 days, with "
                    "a probability and evidence ids, and the statement worded as its field says. Returns the forecast_id any "
                    "claim about the future must cite.",
    "history": "An item's waves, oldest first: start, end, peak day and peak posts, latest marking the most recent "
               "wave, which may be over, and calendar moments near each peak. Give an item_id or words from its label. Cite each wave's "
               "query_id.",
    "analogues": "Items in 42's cultural map nearest to an item by meaning, each with its past waves. Says when it "
                 "falls back to shared label words. Cite each row's query_id.",
    "query_rows": "Read exact rows from a query already recorded in this question, without querying the warehouse "
                  "or spending source credits. Returns rows in their original order with query_id, result_hash, "
                  "row_count, recorded_row_count, columns, truncated and preview (offset, limit, returned, has_more, "
                  "next_offset). Read up to fifty rows per call. Follow next_offset until has_more is false when "
                  "the whole visible result is needed. row_count is the accessible recorded result, at most 500, "
                  "never a complete corpus count. Cite the original query_id for every number.",
}


def build_functions(ctx: RunContext, warehouse, client, writer) -> dict:
    """name -> plain function, each bound to ctx and its dependency."""
    wh = importlib.import_module("core.agent.tools.warehouse")

    def dates(expression: str) -> dict:
        start, end = resolve_dates(expression, ctx.as_of)
        return {"from": start.isoformat(), "to": end.isoformat()}

    functions = {
        "sql_query": lambda **a: query_preview(ctx, sql_query(ctx, warehouse, **a)),
        "search_posts": lambda **a: wh.search_posts(ctx, warehouse, **a),
        "socialcrawl_call": lambda **a: socialcrawl_call(ctx, client, **{"params": {}, **a}, warehouse=warehouse),
        "rising_topics": lambda **a: wh.rising_topics(ctx, warehouse, **a),
        "recall_findings": lambda **a: wh.recall_findings(ctx, warehouse, **a),
        "save_finding": lambda **a: wh.save_finding(ctx, writer, warehouse=warehouse, **a),
        "budget_status": lambda: budget_status(ctx),
        "resolve_dates": dates,
        "get_comments": lambda **a: get_comments(ctx, client, **a),
        "get_transcript": lambda **a: get_transcript(ctx, client, **a),
        "watch_video": lambda **a: watch_video(ctx, client, warehouse=warehouse, **a),
        "log_forecast": lambda **a: log_forecast(ctx, warehouse, writer, **a),
        "query_rows": lambda **a: query_rows(ctx, **a),
        "history": lambda **a: history(ctx, warehouse, **a),
        "analogues": lambda **a: analogues(ctx, warehouse, **a),
    }
    return functions


def guard(ctx: RunContext, name: str, args: dict) -> None:
    """The checks a tool call passes before its function runs. Raises Refused."""
    if name == "sql_query":
        try:
            check_sql(args.get("sql") or "")
        except Refused:
            raise
        except Exception as e:
            raise Refused(f"The SQL guard could not check this query ({type(e).__name__}), so it is refused.") from e
    elif name == "socialcrawl_call":
        try:
            route = normalise_route(args.get("platform") or "", args.get("endpoint") or "")
            params = dict(args.get("params") or {})
            if route == SEARCH_EVERYWHERE:
                params["exclude"] = ",".join(EVERYWHERE_EXCLUDE)
            check_route(route, params)
            max_credits = float(args["max_credits"])
        except Refused:
            raise
        except (KeyError, TypeError, ValueError):
            raise Refused("max_credits must be a number.") from None
        if ctx.calls_left() <= 0:
            raise Refused(f"No SocialCrawl calls left for this question ({ctx.budget['calls']} allowed).")
        if max_credits > ctx.credits_left():
            raise Refused(f"max_credits {max_credits} is over the {ctx.credits_left()} credits left in this "
                          f"question's budget.")


def run_plain(name: str, fn, args: dict) -> tuple[str, bool]:
    """(text, is_error): the tool's result as JSON, or its error in words safe to show the model."""
    try:
        return json.dumps(fn(**args), default=str, ensure_ascii=False), False
    except Refused as e:
        return f"Refused: {e}", True
    except Exception as e:
        if name in SOCIALCRAWL_TOOLS:
            return f"{name} failed ({type(e).__name__})", True
        first_line = (str(e).strip().splitlines() or [""])[0]
        first_line = _QUERY.sub("", _URL.sub("<url>", first_line))[:_ERROR_CHARS]
        return f"{name} failed ({type(e).__name__}): {first_line}", True
