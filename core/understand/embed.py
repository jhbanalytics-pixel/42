"""Post embeddings (BUILD.md 1.8): gemini-embedding-001 at 768 dimensions through the BigQuery remote model.

run_embed creates the model if needed, embeds each window of posts sighted in it that have text and no embedding
yet, at most slice_limit posts per window, then makes sure tvf_search_posts exists, and the vector index too once
post_enrichment holds INDEX_MIN_ROWS embeddings (BigQuery refuses an IVF index below 5,000 rows). Below that
counts["index"] is deferred_below_5000, or deferred_no_embeddings at none. An index BigQuery refuses anyway is
recorded as failed, with the error class and the first line of its message in index_error, and the run goes on: the
embeddings are written. Every statement is CREATE ... IF NOT EXISTS, an append or a read, so a rerun repeats
nothing. execute(sql, params, max_bytes=None) runs one query job with named parameters, billing at most max_bytes
when given, and returns its rows, or a job-like dict holding them under "rows". The counts it returns add
chars_sent, the characters sent to the model across all windows, tokens, the input tokens embed.sql counts for them
(the token_count Vertex reports in ml_generate_embedding_statistics, or where it reports none the post's UTF-8 bytes
at three a token, at most 2,048), model_usd, those tokens at the list price, and embedded_total, every
post_enrichment row that holds an embedding after the run.

book_spend is the understand job's spend ledger. Before each window run_embed calls spent_today (spend_today, which
imports core.agent.ask only when called) and sizes the window from the room left: max_rows is slice_limit, or fewer
when slice_limit posts at POST_CEILING_USD (MAX_TOKENS_PER_POST, the model's 2,048-token input, the most one post
can bill: a 4,000-character isiZulu or emoji-heavy post can reach it) would pass the cap (cap_limited). A window
with room for no post is not sent, and none after it. It then books the window's ceiling, max_rows at
POST_CEILING_USD, through the book callable the job hands it, sends the window, and books a correction to the
window's true spend, priced from its tokens (a credit, or a charge should Vertex report more than the ceiling). From
before the window is sent, the daily spend query (core.agent.ask.SPEND_SQL, every runs row of the day) sees its
spend or more, even when the run is killed mid-window. booked_usd sums what was booked. When the spend cannot be
read, or the ceiling cannot be booked, no more windows are sent (spend_unknown, with the error class in
spend_error).

A window that raises, or is killed, keeps its ceiling booked: post_enrichment holds no run stamp, so what it
really embedded cannot be read back, and each retry books a ceiling of its own. So before booking each ceiling
run_embed also calls uncorrected (uncorrected_today, which reads embed_uncorrected.sql), and once today's
ceilings that no correction followed reach one full window (WINDOW_CEILING_USD, USD 15.36) it sends no window
(retry_capped, with the amount in uncorrected_usd). When that read fails it sends none either (spend_unknown).

While a window runs, its ceiling (up to USD 15.36) counts in today's spend until its correction is booked, and Ask
reads the same spend against MODEL_DAILY_USD. So Ask may refuse a question during the dawn embed run when other
spend that day is already high; once the window returns, its correction brings the day down to its true spend.
"""
import json
import math
import uuid
from datetime import timedelta, timezone
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"
SAST = timezone(timedelta(hours=2))  # Africa/Johannesburg keeps no daylight saving
SLICE_DAYS = 7
BACKFILL_DAYS = 90  # DATA.md section 2: re-embed only the last 90 days
SLICE_LIMIT = 50_000  # most posts one window sends to the model
COUNTS = ("embedded", "failed", "skipped", "chars_sent", "tokens")
# gemini-embedding-001 list price in USD per million input tokens, to confirm against the billing export.
# The job books run_embed's model_usd with book_spend, so it is this run's share of the day's model spend.
EMBED_USD_PER_MILLION_TOKENS = 0.15
CHARS_PER_TOKEN = 4  # core.agent's estimate for embedding a search query; posts are priced from their tokens
# embed.sql cuts each post's content at this many characters. A post still bills up to the model's input limit,
# MAX_TOKENS_PER_POST, so no post costs more than POST_CEILING_USD and no window more than SLICE_LIMIT of them,
# USD 15.36.
MAX_CHARS_PER_POST = 4000
MAX_TOKENS_PER_POST = 2_048
# BigQuery refuses CREATE VECTOR INDEX with IVF on fewer rows than this. tvf_search_posts scores exact cosine and
# needs no index, so below it the index waits.
INDEX_MIN_ROWS = 5_000
# One model spend, appended to runs as it happens. chain.begin and upstream match a stage exactly, so an
# understand_spend row never stands in for, or blocks, an understand run.
SPEND_ROW_SQL = (
    "INSERT INTO `ogilvy-trends-v2.intelligence_42_agent.runs` "
    "(run_id, stage, run_date, status, started_at, finished_at, counts)\n"
    "VALUES (@run_id, 'understand_spend', @run_date, 'ok', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), "
    "PARSE_JSON(@counts))")


def load(name):
    return (SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")


def book_spend(execute, *, run_id, run_date, usd, what, credit=False) -> float:
    """Append a runs row booking usd of model spend on run_date, its own run_id derived from run_id, and return the
    amount booked. Nothing is booked for zero, nor for less unless credit is true: a credit booked as it happens
    would be taken again by a retry, so the caller keeps it for its understand row. An embed correction is the
    exception (credit=True): it undoes part of a ceiling this same run booked, and a retry books a ceiling of its own."""
    usd = round(float(usd or 0), 6)
    if usd == 0 or (usd < 0 and not credit):
        return 0.0
    execute(SPEND_ROW_SQL, spend_row(run_id=run_id, run_date=run_date, usd=usd, what=what))
    return usd


def spend_row(*, run_id, run_date, usd, what) -> dict:
    """SPEND_ROW_SQL's parameters for usd of model spend on run_date, under a run_id of its own derived from
    run_id."""
    return {"run_id": f"{run_id}-spend-{uuid.uuid4().hex[:8]}", "run_date": run_date,
            "counts": json.dumps({"model_usd": usd, "what": what})}


def windows(run_date, days):
    """Inclusive (from, to) date pairs covering the days ending on run_date, newest first, at most 7 days each."""
    if days < 1:
        raise ValueError(f"days must be at least 1, got {days}")
    out = []
    end = run_date
    first = run_date - timedelta(days=days - 1)
    while end >= first:
        start = max(first, end - timedelta(days=SLICE_DAYS - 1))
        out.append((start, end))
        end = start - timedelta(days=1)
    return out


def _counts_row(result, name="embed"):
    rows = result.get("rows") if isinstance(result, dict) else result
    rows = list(rows or [])
    if not rows:
        raise RuntimeError(f"{name}.sql returned no counts row")
    return dict(rows[0])


def spend_today(execute, now) -> tuple[float, float]:
    """(today's model spend, MODEL_DAILY_USD), read the way Ask reads it. Raises when the spend cannot be read, and
    when core.agent cannot be imported, which is imported only here."""
    from core.agent.ask import model_daily_usd, model_spend_today

    class Warehouse:
        def run(self, sql, params, max_bytes=None):
            result = execute(sql, params, max_bytes=max_bytes)
            rows = result.get("rows") if isinstance(result, dict) else result
            return [dict(row) for row in rows or []]

    return model_spend_today(Warehouse(), now), model_daily_usd(now=now)


def _usd(tokens) -> float:
    return round(tokens * EMBED_USD_PER_MILLION_TOKENS / 1_000_000, 6)


# Not rounded: at six places it would fall below the true price and 50,000 of them below the window's.
POST_CEILING_USD = MAX_TOKENS_PER_POST * EMBED_USD_PER_MILLION_TOKENS / 1_000_000
WINDOW_CEILING_USD = round(SLICE_LIMIT * POST_CEILING_USD, 6)


def uncorrected_today(execute, day) -> float:
    """USD of the embed ceilings booked on day that no correction followed (embed_uncorrected.sql), read under the
    byte ceiling Ask reads spend with. Raises when the read fails, and when core.agent cannot be imported, which is
    imported only here."""
    from core.agent.tools.sql_query import MAX_BYTES_BILLED

    result = execute(load("embed_uncorrected"), {"day": day}, max_bytes=MAX_BYTES_BILLED)
    rows = result.get("rows") if isinstance(result, dict) else result
    rows = list(rows or [])
    return float(dict(rows[0]).get("usd") or 0.0) if rows else 0.0


def run_embed(execute, *, run_date, days=1, slice_limit=SLICE_LIMIT, book=None, spent_today=None,
              uncorrected=None, spend_day=None, clock=None):
    """book(usd, what) books a spend, or a credit, and returns the amount booked; spent_today() returns (today's model
    spend, the cap); uncorrected() returns today's embed ceilings no correction followed. When the counts cannot be
    finished, the counts so far ride on the exception as embed_counts."""
    if slice_limit < 1:
        raise ValueError(f"slice_limit must be at least 1, got {slice_limit}")
    slices = windows(run_date, days)
    execute(load("model"), {})
    totals = {**dict.fromkeys(COUNTS, 0), "model_usd": 0.0, **({"booked_usd": 0.0} if book else {})}
    embed_sql = load("embed")
    try:
        for start, end in slices:
            if spend_day is not None and clock is not None and clock().astimezone(SAST).date() != spend_day:
                totals["day_changed"] = True
                break
            max_rows = slice_limit
            if spent_today is not None:
                try:
                    spent, cap = spent_today()
                except Exception as err:
                    totals["spend_unknown"], totals["spend_error"] = True, type(err).__name__
                    break
                if spend_day is not None and clock is not None and clock().astimezone(SAST).date() != spend_day:
                    totals["day_changed"] = True
                    break
                # Rounded before the floor so float noise in the division never costs a post.
                max_rows = min(slice_limit, max(0, math.floor(round((cap - spent) / POST_CEILING_USD, 6))))
                if max_rows < slice_limit:
                    totals["cap_limited"] = True
                if max_rows == 0:
                    break
            if uncorrected is not None:
                try:
                    stuck = round(float(uncorrected()), 6)
                except Exception as err:
                    totals["spend_unknown"], totals["spend_error"] = True, type(err).__name__
                    break
                if stuck >= WINDOW_CEILING_USD:
                    totals["retry_capped"], totals["uncorrected_usd"] = True, stuck
                    break
                if spend_day is not None and clock is not None and clock().astimezone(SAST).date() != spend_day:
                    totals["day_changed"] = True
                    break
            ceiling = 0.0
            if book:
                try:
                    ceiling = book(round(max_rows * POST_CEILING_USD, 6), "embed_ceiling")
                except Exception as err:
                    totals["spend_unknown"], totals["spend_error"] = True, type(err).__name__
                    break
                totals["booked_usd"] = round(totals["booked_usd"] + ceiling, 6)
            if spend_day is not None and clock is not None and clock().astimezone(SAST).date() != spend_day:
                totals["day_changed"] = True
                if book and ceiling:
                    correction = book(round(-ceiling, 6), "embed_correction")
                    totals["booked_usd"] = round(totals["booked_usd"] + correction, 6)
                break
            row = _counts_row(execute(embed_sql, {"from_date": start, "to_date": end, "max_rows": max_rows}))
            for key in COUNTS:
                totals[key] += int(row.get(key) or 0)
            totals["model_usd"] = _usd(totals["tokens"])
            if book:
                # The run's rounded total less everything booked, so the bookings add up to model_usd exactly.
                correction = round(totals["model_usd"] - totals["booked_usd"], 6)
                totals["booked_usd"] = round(totals["booked_usd"] + book(correction, "embed_correction"), 6)
    except Exception as err:
        err.embed_counts = totals
        raise
    stored = int(_counts_row(execute(load("embedding_count"), {}), "embedding_count").get("n") or 0)
    totals["embedded_total"] = stored
    if stored == 0:
        totals["index"] = "deferred_no_embeddings"
    elif stored < INDEX_MIN_ROWS:
        totals["index"] = "deferred_below_5000"
    else:
        # The embeddings are written already, so an index BigQuery refuses is recorded, not raised.
        try:
            execute(load("index"), {})
            totals["index"] = "created_or_exists"
        except Exception as err:
            first = (str(err).splitlines() or [""])[0][:300]
            totals["index"], totals["index_error"] = "failed", f"{type(err).__name__}: {first}"
    execute(load("tvf_search_posts"), {})
    return totals
