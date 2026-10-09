"""Read-only report of why a published brief held its items (BUILD.md 1.12, TRUST.md section 2).

For one brief date it reads the brief run's counts, v_briefs_current and the run's claim_checks rows, plus the
baseline collection-health rows that decide G1, and prints one line per held item: market, rule, stored reason,
posts in the pack, local posts and posts 42 can show (the counts core/brief/job.py _gate reads), and the failed
claim-check wording for items held after explanation. Nothing is written. Every query is capped at 64 MiB billed.

Entry point: python -m core.brief.holds_report YYYY-MM-DD
"""

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date

from core.brief.payload import hold_base
from core.brief.specificity import counted_local_posts
from core.trust.claims import located_market

PROJECT = "ogilvy-trends-v2"
CORE = "intelligence_42_core"
AGENT = "intelligence_42_agent"
MAX_BYTES = 64 * 1024 * 1024
BASELINE = ("unbiased_rank", "panel", "unbiased_counter")

RUN = f"""SELECT run_id, status, started_at, finished_at, TO_JSON_STRING(counts) counts, error
FROM `{PROJECT}.{AGENT}.runs` WHERE stage = 'brief' AND run_date = @d ORDER BY started_at"""
BRIEFS = f"""SELECT market, run_id, status, TO_JSON_STRING(payload) payload
FROM `{PROJECT}.{AGENT}.v_briefs_current` WHERE brief_date = @d ORDER BY market"""
CHECKS = f"""SELECT answer_or_brief_id, claim_id, rule, verdict, reason
FROM `{PROJECT}.{AGENT}.claim_checks` WHERE run_id = @run_id"""
# Briefs stored before 4 Oct titled a creator item by its raw key ("youtube:uc…"). The report names such a title
# from the creators table, keyed as cultural_map folds a creator key, leaving out the suppression list.
CREATORS = f"""SELECT CONCAT(LOWER(TRIM(c.platform)), ':', LOWER(TRIM(c.creator_id))) AS key, c.handle, c.display_name
FROM `{PROJECT}.{CORE}.creators` c
WHERE CONCAT(LOWER(TRIM(c.platform)), ':', LOWER(TRIM(c.creator_id))) IN UNNEST(@keys)
  AND NOT EXISTS (SELECT 1 FROM `{PROJECT}.{CORE}.v_suppressed_creators` sc WHERE sc.creator_id = c.creator_id)
QUALIFY ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(c.platform)), LOWER(TRIM(c.creator_id))
                           ORDER BY c.display_name IS NULL, c.followers DESC) = 1"""
RAW_KEY = re.compile(r"[a-z][a-z0-9_]*:[^\s:]+")
HEALTH = f"""SELECT day, market, platform, series, lane_class, calls, calls_ok, items, ref_items, invalid_reason
FROM `{PROJECT}.{CORE}.v_collection_health_current`
WHERE day BETWEEN DATE_SUB(@d, INTERVAL 2 DAY) AND @d AND NOT valid
  AND lane_class IN ('unbiased_rank', 'panel', 'unbiased_counter')
ORDER BY market, day, platform, series"""


def post_counts(evidence, market):
    """(posts in the pack, local posts, posts 42 can show) as _gate counts them."""
    local = {r["id"] for r in counted_local_posts(evidence, market)}
    showable = [r for r in evidence if located_market(r) is None or r.get("id") in local]
    return len(evidence), len(local), len(showable)


def failed_checks(checks, run_id, market, item_id):
    """The wording of every check row on the item that did not pass, most common first."""
    key = f"{run_id}:{market}:{item_id}"
    # A title row (core/brief/explain.py TITLE_RULE) never held an item: a cut only drops the written title.
    rows = [c for c in checks if c["answer_or_brief_id"] == key and c["verdict"] != "pass" and c["rule"] != "title"]
    return [f"{n}x {reason}" for reason, n in Counter(c["reason"] or c["rule"] for c in rows).most_common()]


def tally(briefs, checks):
    """One dict per held item across the markets, and one per market with its status and banners."""
    markets, held = [], []
    for b in briefs:
        payload = json.loads(b["payload"]) if isinstance(b["payload"], str) else b["payload"]
        hb = payload.get("held_back") or {}
        markets.append({"market": b["market"], "status": b["status"],
                        "cards": len(payload.get("cards") or []) + len(payload.get("more") or []),
                        "held": hb.get("count", 0),
                        "banners": [f"{x.get('kind')}: {x.get('text')}" for x in payload.get("banners") or []]})
        for item in hb.get("items") or []:
            n, local, showable = post_counts(item.get("evidence") or [], b["market"])
            held.append({"market": b["market"], "item_id": item["item_id"], "title": item.get("title"),
                         "rule": item.get("rule"), "reason": item.get("reason"),
                         "reason_text": item.get("reason_text"), "posts": n, "local": local,
                         "showable": showable,
                         "failed": failed_checks(checks, b["run_id"], b["market"], item["item_id"])})
    return markets, held


def causes(held):
    """Held items grouped by their stored wording, biggest group first."""
    by = defaultdict(list)
    for h in held:
        by[hold_base(h["reason_text"]) if h["reason_text"] else h["reason"]].append(f"{h['market']} {h['title']}")
    return sorted(by.items(), key=lambda kv: -len(kv[1]))


def named_titles(held, names):
    """Each held item's title, with the creators row's display name, else its @handle, in front of a title that is
    a raw creator key (names: {key: creators row})."""
    for h in held:
        c = names.get(str(h["title"] or "").casefold())
        if not c:
            continue
        name = str(c.get("display_name") or "").strip() or (
            "@" + str(c.get("handle")).strip().lstrip("@") if str(c.get("handle") or "").strip() else "")
        if name:
            h["title"] = f"{name} ({h['title']})"
    return held


def _query(client, sql, params):
    from google.cloud import bigquery

    qp = [bigquery.ArrayQueryParameter(k, "STRING", list(v)) if isinstance(v, (list, tuple)) else
          bigquery.ScalarQueryParameter(k, "DATE" if isinstance(v, date) else "STRING", v)
          for k, v in params.items()]
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=qp, dry_run=True,
                                                               use_query_cache=False))
    if dry.total_bytes_processed > MAX_BYTES:
        raise SystemExit(f"refused: query would read {dry.total_bytes_processed} bytes, over {MAX_BYTES}")
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=qp,
                                                               maximum_bytes_billed=MAX_BYTES))
    return [dict(r) for r in job.result()], dry.total_bytes_processed


def main(argv):
    from google.cloud import bigquery

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a title a narrow console cannot show never stops the report

    d = date.fromisoformat(argv[1])
    client = bigquery.Client(project=PROJECT, location="US")
    runs, b1 = _query(client, RUN, {"d": d})
    briefs, b2 = _query(client, BRIEFS, {"d": d})
    run_ids = sorted({b["run_id"] for b in briefs})
    checks, b3 = [], 0
    for run_id in run_ids:
        rows, n = _query(client, CHECKS, {"run_id": run_id})
        checks += rows
        b3 += n
    health, b4 = _query(client, HEALTH, {"d": d})
    markets, held = tally(briefs, checks)
    keys = sorted({str(h["title"]).casefold() for h in held if RAW_KEY.fullmatch(str(h["title"] or ""))})
    rows, b5 = [], 0
    if keys:
        try:
            rows, b5 = _query(client, CREATORS, {"keys": keys})
        except (Exception, SystemExit) as e:  # names are a reading aid; the report goes on with the stored titles
            print(f"creator names not read: {e}")
    named_titles(held, {str(r["key"]): r for r in rows})
    print(f"bytes read: runs {b1}, briefs {b2}, claim_checks {b3}, health {b4}, creators {b5}")

    print("\nBRIEF RUNS")
    for r in runs:
        print(f"{r['run_id']} {r['status']} {r['started_at']} -> {r['finished_at']} error={r['error']}")
        print(f"  counts {r['counts']}")
    print(f"\nMARKETS (current run {', '.join(run_ids) or 'none'})")
    for m in markets:
        print(f"{m['market']} status={m['status']} cards={m['cards']} held={m['held']}")
        for banner in m["banners"]:
            print(f"  banner {banner}")
    print("\nHELD ITEMS (posts / local / showable as _gate counts them)")
    for h in held:
        print(f"{h['market']} | {h['title']} | {h['item_id']} | rule={h['rule']} reason={h['reason']} | "
              f"{h['reason_text']} | posts {h['posts']} local {h['local']} showable {h['showable']}")
        for f in h["failed"]:
            print(f"    check: {f}")
    print("\nCAUSES")
    for text, items in causes(held):
        print(f"{len(items)} {text}: {'; '.join(items)}")
    print("\nINVALID BASELINE HEALTH ROWS (G1), last 3 days")
    for r in health:
        print(f"{r['market']} {r['day']} {r['platform']} {r['series']} {r['lane_class']} calls {r['calls_ok']}/"
              f"{r['calls']} items {r['items']} ref {r['ref_items']} {r['invalid_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
