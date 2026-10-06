"""Read-only check for the confirm-finds scope fix. Run from the repo root at the release sha:
    py -3.13 ops/runners/check_scope_fix.py
1. Dry-runs every views.sql statement (v_item_market_scope included) against BigQuery: nothing is created.
2. Reads the market scope, with the checked-out market_scope.sql, for the items that dropped off on 2 Oct.
Prints counts and labels only. Every query is capped at 1 GB billed."""
import datetime as dt
import sys

from google.cloud import bigquery

sys.path.insert(0, ".")
from core.detect import sqlrun  # noqa: E402
from core.brief.market_scope import read_market_scope  # noqa: E402

P = "ogilvy-trends-v2"
c = bigquery.Client(project=P)
stmts = sqlrun.statements()
for s in stmts:
    c.query(s, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
print(f"views dry run ok: {len(stmts)} statements, including v_item_market_scope: "
      f"{any('v_item_market_scope' in s for s in stmts)}")

WANT = {"islamicvideo": "NG", "islamicreminder": "NG", "creatorsearchinsights": "NG", "madlangacommission": "ZA",
        "afrotech": "ZA", "sama28": "ZA"}
sql = f"""SELECT item_id, LOWER(TRIM(IFNULL(label, canonical_key), '#')) k FROM `{P}.intelligence_42_core.cultural_map`
          WHERE valid_to IS NULL AND LOWER(TRIM(IFNULL(label, canonical_key), '#')) IN UNNEST(@keys)"""
cfg = bigquery.QueryJobConfig(maximum_bytes_billed=10**9,
                              query_parameters=[bigquery.ArrayQueryParameter("keys", "STRING", list(WANT))])
d = dt.date(2026, 10, 2)
for r in c.query(sql, job_config=cfg).result():
    try:
        s = read_market_scope(c, {"item_id": r.item_id}, d, WANT[r.k])
        print(f"{WANT[r.k]} #{r.k}: {s['market_scope']} ({s['market_posts7']} of {s['total_posts7']} local)")
    except Exception as exc:
        print(f"{WANT[r.k]} #{r.k}: read failed ({type(exc).__name__})")
