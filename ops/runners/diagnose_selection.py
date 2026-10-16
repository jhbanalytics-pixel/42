"""Read-only check of which candidates the morning brief judged for one date, and which the pattern-first ordering
(core/brief/sql/brief.sql, pattern-first-selection) would judge instead. Run from the repository root:
    py -3.13 -m ops.runners.diagnose_selection YYYY-MM-DD [--out FOLDER]
Needs google-cloud-bigquery and gcloud ADC on this PC; reads run as f42-builder through impersonation (RULES 13, the
pattern the ops reports use). SELECT only, each query capped at 2 GB billed.

Per market it runs the candidate statement twice on the date's detect run, once as this source orders it and once
with the pattern bucket taken out of the ordering (the f9bd07c9 order), and reads the date's published brief payload.
It prints, for the first 10 of each order and for every held item: rank under both orders, kind, title, worth_raw,
the pattern bucket and its inputs (most creators on one sound and on how many days, most creators on one enriched
adoption format and on how many days, enriched and news-clip posts) and, for an item the brief judged, its stored hold
reason. The ten of the old order is the brief's judged set only when no candidate was held by G1 or a deadline; the
payload's held items are the authority for what was judged. Then a summary per market: how many of the judged were in
bucket 2 (one creator, a calendar moment or mostly news), how many bucket 0 rows the pool held, and how many of the
new ten the brief never judged.

Prints titles, counts and fixed wording only: no handles, post text or model text. --out also writes the same rows as
JSON there (keep it inside the approved local boundary; it names items and titles)."""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from google.cloud import bigquery

from core.brief import job
from core.detect import sqlrun

P = "ogilvy-trends-v2"
BUILDER = "f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com"
CORE, AGENT = "intelligence_42_core", "intelligence_42_agent"
CAP = 2 * 10**9
MARKETS = ("ZA", "NG", "KE")
TOP = job.CANDIDATES
BUCKETS = {0: "adopted", 1: "plain", 2: "one creator, moment or news"}
# The pattern bucket inside ROW_NUMBER's ORDER BY: the one CASE there that starts on its own line.
_ORDER_CASE = re.compile(r"\n    CASE\n      WHEN s\.kind = 'creator'.*?\n      ELSE 1 END,", re.DOTALL)


def old_order(sql):
    """The candidate statement with the pattern bucket taken out of the ordering, as f9bd07c9 ordered it."""
    out, n = _ORDER_CASE.subn("", sql)
    if n != 1:
        raise SystemExit("the candidate statement's pattern bucket was not found once; is this the pattern source?")
    return out


def query(client, sql, params):
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=CAP, use_query_cache=True, query_parameters=params)
    return [dict(r) for r in client.query(sqlrun.render(sql, core=CORE, agent=AGENT), job_config=cfg).result()]


def title(row):
    return job.card_title(row) or f"Unnamed {row.get('kind') or 'item'}"


def held_reasons(payload):
    """{item_id: hold reason} for every judged item, cards included as published."""
    out = {}
    for card in (payload.get("cards") or []) + (payload.get("more") or []):
        out[card.get("item_id")] = "published"
    for item in (payload.get("held_back") or {}).get("items") or []:
        reason = item.get("reason_text") or item.get("reason") or "held"
        failed = item.get("failed_reason")
        out[item.get("item_id")] = f"{reason}: {failed}" if failed else reason
    return out


def line(row, new_rank, old_rank, outcome):
    pattern = row.get("_selection_pattern")
    return {"item_id": row["item_id"], "new_rank": new_rank, "old_rank": old_rank, "kind": row.get("kind"),
            "title": title(row), "worth_raw": row.get("worth_raw"), "pattern": pattern,
            "pattern_words": BUCKETS.get(pattern), "sound_creators": row.get("_selection_sound_creators"),
            "sound_days": row.get("_selection_sound_days"), "format_creators": row.get("_selection_format_creators"),
            "format_days": row.get("_selection_format_days"), "enriched_posts": row.get("_selection_enriched_posts"),
            "news_clip_posts": row.get("_selection_news_clip_posts"), "outcome": outcome}


def show(entry):
    worth = entry["worth_raw"]
    print(f"  new {entry['new_rank'] or '-':>3} old {entry['old_rank'] or '-':>3}  {entry['kind'] or '?':8} "
          f"{(entry['title'] or '')[:48]:48} worth {worth if worth is None else round(worth, 3)}  "
          f"bucket {entry['pattern']} ({entry['pattern_words']})  sound {entry['sound_creators']}c/{entry['sound_days']}d"
          f"  format {entry['format_creators']}c/{entry['format_days']}d  news clips "
          f"{entry['news_clip_posts']}/{entry['enriched_posts']}  {entry['outcome'] or 'not judged'}")


def make_client():
    """A BigQuery client acting as f42-builder, impersonated from this PC's ADC for ten minutes (RULES 13)."""
    import google.auth
    from google.auth import impersonated_credentials

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials = impersonated_credentials.Credentials(
        source_credentials=source, target_principal=BUILDER,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"], lifetime=600)
    return bigquery.Client(project=P, credentials=credentials, location="US")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("date", type=dt.date.fromisoformat)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    client = make_client()
    d = args.date
    new_sql = job.query_sql("candidates")  # for the locality authority in force, as the brief reads it
    old_sql = old_order(new_sql)
    payloads = {r["market"]: json.loads(r["p"]) for r in query(client, (
        "SELECT market, TO_JSON_STRING(payload) p FROM {agent}.v_briefs_current WHERE brief_date = @d"),
        [bigquery.ScalarQueryParameter("d", "DATE", d)])}
    report = {"date": d.isoformat(), "markets": {}}
    for m in MARKETS:
        params = [bigquery.ScalarQueryParameter("d", "DATE", d), bigquery.ScalarQueryParameter("market", "STRING", m)]
        new_rows, old_rows = query(client, new_sql, params), query(client, old_sql, params)
        new_rank = {r["item_id"]: n for n, r in enumerate(job._ranked(new_rows), 1)}
        old_rank = {r["item_id"]: n for n, r in enumerate(job._ranked(old_rows), 1)}
        by_id = {r["item_id"]: r for r in new_rows + old_rows}
        outcomes = held_reasons(payloads.get(m) or {})
        shown = [i for i, n in sorted(new_rank.items(), key=lambda x: x[1]) if n <= TOP]
        shown += [i for i, n in sorted(old_rank.items(), key=lambda x: x[1]) if n <= TOP and i not in shown]
        shown += [i for i in outcomes if i not in shown and i in by_id]
        entries = [line(by_id[i], new_rank.get(i), old_rank.get(i), outcomes.get(i)) for i in shown]
        judged = [e for e in entries if e["outcome"]]
        summary = {
            "pool_rows": len(new_rows),
            "pool_by_bucket": {b: sum(1 for r in new_rows if r.get("_selection_pattern") == b) for b in BUCKETS},
            "judged": len(judged),
            "judged_by_bucket": {b: sum(1 for e in judged if e["pattern"] == b) for b in BUCKETS},
            "judged_published": sum(1 for e in judged if e["outcome"] == "published"),
            "new_top_never_judged": sum(1 for e in entries if e["new_rank"] and e["new_rank"] <= TOP
                                        and not e["outcome"]),
            "unjudged_payload_items": sorted(i for i in outcomes if i not in by_id),
        }
        report["markets"][m] = {"summary": summary, "items": entries}
        print(f"== {m} {d}: pool {summary['pool_rows']}, pool by bucket {summary['pool_by_bucket']}, judged "
              f"{summary['judged']} (by bucket {summary['judged_by_bucket']}, published {summary['judged_published']}),"
              f" new ten never judged {summary['new_top_never_judged']}")
        for e in entries:
            show(e)
        if summary["unjudged_payload_items"]:
            print(f"  {len(summary['unjudged_payload_items'])} payload items are outside this pool (another run?)")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"selection-{d.isoformat()}.json"
        path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
