"""The weekly drift report of the seed loop (SOURCES.md "Keeping it open and unbiased"; BUILD.md task 2.4).

For the seven days ending on the run date (RUN_DATE or --run-date, else yesterday in SAST), per market, it
measures how concentrated expansion was on three dimensions and sets that against the unseeded feeds,
with the Herfindahl index (the sum of squared shares: 1 when one key holds everything, 1/n for n equal
keys). One parameterised SELECT reads every weight:

    topic      expansion: collect's credit_ledger credits in lanes expansion, exploration and anchor by
               item_id, else the lower-cased raw seed_key joined on the call identity. feeds: appearances
               rows of unbiased_rank lists in item_counter_daily, by item_id.
    platform   expansion: collect's credit_ledger credits in those lanes by platform, a search/multi call's
               credits split evenly over the platforms it searches. feeds: unbiased_rank sightings in
               post_observations, by platform.
    language   both sides: posts found (expansion and exploration lanes) and unbiased_rank sightings,
               joined to post_enrichment.langs, one count per language a post carries. A seed has no
               language before its posts are read, so this side counts posts, not credits.

A dimension has drifted when its expansion index is above limit(feed index) = max(FLOOR, feed + MARGIN).
FLOOR is 0.25, the index a split into four equal parts gives: an index above it means some key held
more than a quarter, the diversity quota's line. MARGIN is 0.10, so a dimension where the open feeds are
themselves concentrated (most feed posts are TikTok) only alerts when expansion is clearly narrower than
they are. With no feed rows the limit is FLOOR; with no expansion rows nothing is judged.

The weekly job logs one INFO line per market and dimension and, when any drifted, one ERROR line in the
shared alert format, "42 ALERT drift: <start> to <end> <market> <dimension> HHI <x> against feeds <y>
(limit <z>), top <key> <share>%", breaches joined by "; ". --report prints the same figures as a table
for a person and never writes the alert line. The drift report itself writes nothing to BigQuery.

After the drift lines the weekly job runs the calendar analogues step (task 3.6, core.collect.calendar
--analogues --apply through calendar.weekly_analogues): last year's analogue for each calendar row in the
coming window, read under the 5 GB dry-run cap and appended to calendar_analogues. The job runs as
f42-collector, which can write intelligence_42_core and read trends_v2_dev. The step fails soft: a refusal
or an error is logged as one WARNING line starting "calendar analogues:" (never a 42 ALERT line) and the
job still exits 0, so the drift report is never lost to it. --report never runs it.

    py -3.13 -m core.collect.drift --report --run-date 2026-09-27   the week's table, no alert line
    python -m core.collect.drift                                     the weekly job
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

from core.collect import chain
from core.collect.job import MARKETS, MULTI_PLATFORMS
from core.collect.writers import _query, table

log = logging.getLogger(__name__)

WINDOW_DAYS = 7
FLOOR = 0.25
MARGIN = 0.10
DIMENSIONS = ("topic", "platform", "language")
LANES = ("expansion", "exploration", "anchor")

READ_SQL = """SELECT 'expansion' AS side, 'topic' AS dimension, l.market,
  COALESCE(l.item_id, LOWER(r.seed_key)) AS item, SUM(l.credits_charged) AS weight
FROM `{credit_ledger}` l
LEFT JOIN (
  SELECT x.run_id, x.job, x.lane, x.market, x.route, x.params_hash, MIN(x.seed_key) AS seed_key
  FROM `{raw_responses}` x
  WHERE DATE(x.fetched_at) BETWEEN DATE_SUB(@start, INTERVAL 1 DAY) AND DATE_ADD(@end, INTERVAL 1 DAY)
  GROUP BY x.run_id, x.job, x.lane, x.market, x.route, x.params_hash
) r
  ON r.run_id = l.run_id AND r.job = l.job AND r.lane = l.lane AND r.market = l.market
  AND r.route = l.route AND r.params_hash = l.params_hash
WHERE l.trend_date BETWEEN @start AND @end AND l.job = 'collect' AND l.lane IN UNNEST(@lanes)
  AND l.market IN UNNEST(@markets) AND COALESCE(l.item_id, LOWER(r.seed_key)) IS NOT NULL
GROUP BY l.market, item
UNION ALL
SELECT 'feed', 'topic', c.market, c.item_id, SUM(c.value)
FROM `{item_counter_daily}` c
WHERE c.obs_date BETWEEN @start AND @end AND c.unit = 'appearances' AND c.lane_class = 'unbiased_rank'
  AND c.market IN UNNEST(@markets)
GROUP BY c.market, c.item_id
UNION ALL
SELECT 'expansion', 'platform', l.market, p,
  SUM(l.credits_charged / ARRAY_LENGTH(IF(l.route = 'search/multi', @multi, [l.platform])))
FROM `{credit_ledger}` l, UNNEST(IF(l.route = 'search/multi', @multi, [l.platform])) p
WHERE l.trend_date BETWEEN @start AND @end AND l.job = 'collect' AND l.lane IN UNNEST(@lanes)
  AND l.market IN UNNEST(@markets)
GROUP BY l.market, p
UNION ALL
SELECT 'feed', 'platform', o.market, o.platform, COUNT(*)
FROM `{post_observations}` o
WHERE o.observed_date BETWEEN @start AND @end AND o.lane_class = 'unbiased_rank' AND o.market IN UNNEST(@markets)
GROUP BY o.market, o.platform
UNION ALL
SELECT IF(o.lane_class = 'unbiased_rank', 'feed', 'expansion'), 'language', o.market, lang, COUNT(*)
FROM `{post_observations}` o JOIN `{post_enrichment}` e ON e.post_id = o.post_id, UNNEST(e.langs) lang
WHERE o.observed_date BETWEEN @start AND @end AND o.market IN UNNEST(@markets)
  AND (o.lane_class = 'unbiased_rank' OR o.lane IN UNNEST(@lanes))
GROUP BY 1, 3, 4
"""


def hhi(weights):
    """Sum of squared shares; None when there is nothing to share."""
    values = [w for w in weights.values() if w]
    total = sum(values)
    return sum((w / total) ** 2 for w in values) if total > 0 else None


def limit(feed):
    return FLOOR if feed is None else max(FLOOR, feed + MARGIN)


def start_of(end):
    return end - timedelta(days=WINDOW_DAYS - 1)


def read(bq, end):
    from google.cloud import bigquery

    names = ("raw_responses", "item_counter_daily", "credit_ledger", "post_observations", "post_enrichment")
    sql = READ_SQL.format(**{name: table(name) for name in names})
    rows = _query(bq, sql, [
        bigquery.ScalarQueryParameter("start", "DATE", start_of(end)),
        bigquery.ScalarQueryParameter("end", "DATE", end),
        bigquery.ArrayQueryParameter("markets", "STRING", list(MARKETS)),
        bigquery.ArrayQueryParameter("lanes", "STRING", list(LANES)),
        bigquery.ArrayQueryParameter("multi", "STRING", MULTI_PLATFORMS.split(","))])
    return [dict(r.items()) for r in rows]


def report(rows, markets=MARKETS):
    """One entry per market and dimension: both indexes, the limit, the top expansion key and its share."""
    weights = {}
    for r in rows:
        group = weights.setdefault((r["market"], r["dimension"], r["side"]), {})
        item = "unknown" if r["item"] is None else str(r["item"])
        group[item] = group.get(item, 0.0) + float(r["weight"] or 0)
    out = []
    for market in markets:
        for dimension in DIMENSIONS:
            spent = weights.get((market, dimension, "expansion"), {})
            expansion, feed = hhi(spent), hhi(weights.get((market, dimension, "feed"), {}))
            top = max(spent, key=lambda k: (spent[k], k)) if expansion is not None else None
            out.append({"market": market, "dimension": dimension, "expansion": expansion, "feed": feed,
                        "limit": limit(feed), "top": top,
                        "top_share": spent[top] / sum(spent.values()) if top is not None else None,
                        "drifted": expansion is not None and expansion > limit(feed)})
    return out


def _breach(r):
    feed = "none" if r["feed"] is None else f"{r['feed']:.2f}"
    return (f"{r['market']} {r['dimension']} HHI {r['expansion']:.2f} against feeds {feed} "
            f"(limit {r['limit']:.2f}), top {r['top']} {round(100 * r['top_share'])}%")


def alert_line(found, end):
    breaches = [_breach(r) for r in found if r["drifted"]]
    if not breaches:
        return None
    return f"42 ALERT drift: {start_of(end).isoformat()} to {end.isoformat()} " + "; ".join(breaches)


def _fmt(value):
    return "none" if value is None else f"{value:.2f}"


def print_report(found, end):
    print(f"drift report {start_of(end).isoformat()} to {end.isoformat()} (Herfindahl index; limit is the larger "
          f"of {FLOOR} and the feeds plus {MARGIN})")
    print(f"{'market':6} {'dimension':9} {'expansion':>9} {'feeds':>6} {'limit':>6}  top expansion key")
    for r in found:
        top = f"{r['top']} {round(100 * r['top_share'])}%" if r["top"] is not None else "no expansion rows"
        print(f"{r['market']:6} {r['dimension']:9} {_fmt(r['expansion']):>9} {_fmt(r['feed']):>6} "
              f"{r['limit']:>6.2f}  {top}" + ("  drifted" if r["drifted"] else ""))
    breaches = [_breach(r) for r in found if r["drifted"]]
    print("would alert: " + "; ".join(breaches) if breaches else "no drift")


def run_analogues_step(step, bq):
    """Run the weekly calendar analogues step and log its outcome; never raises."""
    try:
        code = step(bq)
    except Exception as error:  # fail soft: the drift report is this job's main work
        log.warning("calendar analogues: failed, drift report unaffected (%s: %s)", type(error).__name__, error)
        return
    if code == 0:
        log.info("calendar analogues: appended")
    else:
        log.warning("calendar analogues: refused (exit %s), drift report unaffected", code)


def _calendar_analogues(bq):
    from core.collect import calendar

    return calendar.weekly_analogues(bq)


def main(argv=None, *, env=None, bq=None, clock=None, analogues=None):
    """analogues is the weekly calendar step, called with the BigQuery client. When main builds its own client
    (the deployed job) it defaults to calendar.weekly_analogues; a caller that passes its own client and no step
    gets the drift report only."""
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", action="store_true", help="print the week's figures; no alert line")
    parser.add_argument("--run-date", help="last day of the week, YYYY-MM-DD; default RUN_DATE or yesterday in SAST")
    args = parser.parse_args(argv)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    raw = args.run_date or env.get("RUN_DATE")
    end = date.fromisoformat(raw) if raw else now.astimezone(chain.SAST).date() - timedelta(days=1)
    if bq is None:
        from google.cloud import bigquery

        bq = bigquery.Client(project=chain.PROJECT)
        analogues = analogues or _calendar_analogues
    found = report(read(bq, end))
    if args.report:
        print_report(found, end)
        return 0
    for r in found:
        log.info("drift %s %s expansion %s feeds %s limit %.2f", r["market"], r["dimension"], _fmt(r["expansion"]),
                 _fmt(r["feed"]), r["limit"])
    line = alert_line(found, end)
    if line:
        log.error(line)
    if analogues is not None:
        run_analogues_step(analogues, bq)
    return 0


if __name__ == "__main__":
    from core.collect.reconcile import JsonFormatter

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    raise SystemExit(main())
