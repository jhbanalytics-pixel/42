"""Read-only staging readback after a release: BigQuery facts and API shapes, as one Markdown file.

    py -3.13 ops/runners/staging_readback.py --date 2026-10-02 --out readback.md [--base <f42-api url>]

Reads only. BigQuery runs as the caller's own credentials with every query capped at 2 GB billed, and the API
part reads the passcode from F42_SMOKE_PASSCODE (skipped when unset). The passcode is never printed, and no line
carries a header value or an exception's text, only its type name. Lists are shown as counts, never their rows,
so no creator handle or post text lands in the file.
"""
import argparse
import datetime as dt
import json
import os
import sys

PROJECT = "ogilvy-trends-v2"
CORE = f"{PROJECT}.intelligence_42_core"
AGENT = f"{PROJECT}.intelligence_42_agent"
BASE = "https://f42-api-590353929363.us-central1.run.app"
CAP = 2 * 1024 ** 3
COUNTED = {"cards", "more", "held", "items", "alerts", "trends", "points", "rows", "issues", "markets", "also",
           "posts", "evidence", "series", "findings", "briefs", "asks", "watches", "schedules", "breaking"}

QUERIES = [
    ("Runs today (stage, status, model USD)",
     f"""SELECT stage, status, COUNT(*) n, ROUND(SUM(IFNULL(model_usd, 0)), 2) usd,
        FORMAT_TIMESTAMP('%H:%M', MAX(finished_at), 'Africa/Johannesburg') last_finished_sast
        FROM `{AGENT}.runs` WHERE run_date = @d GROUP BY stage, status ORDER BY stage, status"""),
    ("Latest brief run counts",
     f"""SELECT run_id, status, FORMAT_TIMESTAMP('%H:%M', finished_at, 'Africa/Johannesburg') finished_sast,
        TO_JSON_STRING(counts) counts, SUBSTR(IFNULL(error, ''), 1, 200) error
        FROM `{AGENT}.runs` WHERE run_date = @d AND stage = 'brief' AND finished_at IS NOT NULL
        ORDER BY finished_at DESC LIMIT 1"""),
    ("Briefs published today (cards, more, held per market)",
     f"""SELECT market, run_id, status, FORMAT_TIMESTAMP('%H:%M', published_at, 'Africa/Johannesburg') published_sast,
        ARRAY_LENGTH(JSON_QUERY_ARRAY(payload, '$.cards')) cards,
        ARRAY_LENGTH(JSON_QUERY_ARRAY(payload, '$.more')) more,
        CAST(JSON_VALUE(payload, '$.held_back.count') AS INT64) held
        FROM `{AGENT}.briefs` WHERE brief_date = @d
        QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY published_at DESC) = 1 ORDER BY market"""),
    ("Digest runs today",
     f"""SELECT status, FORMAT_TIMESTAMP('%H:%M', started_at, 'Africa/Johannesburg') started_sast,
        TO_JSON_STRING(counts) counts, SUBSTR(IFNULL(error, ''), 1, 200) error
        FROM `{AGENT}.runs` WHERE run_date = @d AND stage = 'digest' ORDER BY started_at"""),
    ("Credits charged today by job and lane",
     f"""SELECT job, lane, SUM(calls) calls, ROUND(SUM(IFNULL(credits_charged, 0)), 1) credits
        FROM `{CORE}.credit_ledger` WHERE trend_date = @d GROUP BY job, lane ORDER BY credits DESC"""),
    ("Confirm finds today and how many have any post_items link",
     f"""SELECT o.market, COUNT(DISTINCT o.post_id) confirm_posts, COUNT(DISTINCT pi.post_id) linked_posts
        FROM `{CORE}.post_observations` o
        LEFT JOIN `{CORE}.post_items` pi ON pi.post_id = o.post_id
        WHERE o.observed_date = @d AND o.lane = 'confirm' GROUP BY o.market ORDER BY o.market"""),
    ("Invalid collection_health rows today",
     f"""SELECT market, series, invalid_reason, calls_ok, calls FROM `{CORE}.collection_health`
        WHERE day = @d AND valid IS NOT TRUE ORDER BY market, series"""),
    ("New objects exist",
     f"""SELECT table_schema, table_name, table_type FROM `{PROJECT}.region-us.INFORMATION_SCHEMA.TABLES`
        WHERE table_name IN ('forecast_score', 'weekly_quality', 'breaking_signals', 'v_breaking_signals_current',
                             'v_suppressed_creators', 'suppressions') ORDER BY table_name"""),
    ("v_suppressed_creators readable",
     f"SELECT COUNT(*) n FROM `{CORE}.v_suppressed_creators`"),
]

PATHS = ["/api/today", "/api/discover", "/api/discover/radar", "/api/alerts", "/api/coverage",
         "/api/history/briefs", "/api/history/findings", "/api/history/asks", "/api/watches", "/api/schedules",
         "/api/suppressions", "/api/communities", "/api/dossiers", "/api/investigations", "/api/skins"]


def table(rows):
    if not rows:
        return "(no rows)\n"
    cols = list(rows[0].keys())
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(str(r[c]).replace("|", "/") for c in cols) + " |")
    return "\n".join(out) + "\n"


def bigquery_part(day):
    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT)
    parts = []
    for title, sql in QUERIES:
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=CAP,
                                      query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", day)])
        try:
            rows = [dict(r.items()) for r in client.query(sql, job_config=cfg).result()]
            parts.append(f"### {title}\n\n{table(rows)}")
        except Exception as exc:  # the text can echo the query; the type is enough to act on
            parts.append(f"### {title}\n\nFAILED ({type(exc).__name__})\n")
    return parts


def shape(value, path="", out=None, depth=0):
    """Counts of the lists under counted keys, and scalar flags, up to four levels down."""
    out = [] if out is None else out
    if depth > 4:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, list) and k in COUNTED:
                out.append(f"{p}: {len(v)}")
            if isinstance(v, (bool, int, float)) and k in ("ok", "first_morning", "active", "day", "shown", "posts",
                                                             "partial", "news_driven", "pulse_ready"):
                out.append(f"{p}={v}")
            if isinstance(v, (dict, list)):
                shape(v, p, out, depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value[:3]):
            shape(v, f"{path}[{i}]", out, depth + 1)
    return out


def first_item_id(today):
    for m in (today.get("markets") or []) if isinstance(today, dict) else []:
        for c in (m.get("cards") or []) + (m.get("more") or []) + ((m.get("held_back") or {}).get("items") or []):
            if isinstance(c, dict) and c.get("item_id"):
                return c["item_id"]
    return None


def api_part(base):
    import httpx

    code = os.environ.get("F42_SMOKE_PASSCODE")
    if not code:
        return ["### API\n\nskipped: F42_SMOKE_PASSCODE is not set\n"]
    lines = ["### API\n", "| path | status | bytes | shape |", "|---|---|---|---|"]
    headers = {"X-Passcode": code}
    today = None
    with httpx.Client(timeout=60.0) as client:
        try:
            health = client.get(f"{base}/api/health").json()
            lines.insert(1, f"health: version {health.get('version')}, checks {json.dumps(health.get('checks'))}\n")
        except Exception as exc:
            lines.insert(1, f"health: request failed ({type(exc).__name__})\n")
        paths = list(PATHS)
        for i, path in enumerate(paths):
            try:
                resp = client.get(f"{base}{path}", headers=headers)
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else None
                if path == "/api/today" and isinstance(body, dict):
                    today = body
                    item = first_item_id(body)
                    if item:
                        paths += [f"/api/topics/{item}", f"/api/trends/{item}", f"/api/history/items/{item}"]
                summary = "; ".join(shape(body)[:25]) if body is not None else "-"
                lines.append(f"| {path} | {resp.status_code} | {len(resp.content)} | {summary} |")
            except Exception as exc:
                lines.append(f"| {path} | failed ({type(exc).__name__}) | - | - |")
    if today:
        lines.append("\nToday per market:\n")
        for m in today.get("markets") or []:
            held = (m.get("held_back") or {}).get("items") or []
            reasons = sorted({str(h.get("reason") or "")[:60] for h in held if isinstance(h, dict)})
            lines.append(f"- {m.get('market')}: status {m.get('status')}, cards {len(m.get('cards') or [])}, "
                         f"more {len(m.get('more') or [])}, held {len(held)}; reasons {reasons}")
    return ["\n".join(lines) + "\n"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--no-bigquery", action="store_true")
    args = ap.parse_args(argv)
    day = dt.date.fromisoformat(args.date)
    parts = [f"# Staging readback {args.date}\n\nWritten {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z. "
             f"Read-only; lists are counts only.\n"]
    if not args.no_bigquery:
        parts += bigquery_part(day)
    parts += api_part(args.base.rstrip("/"))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
