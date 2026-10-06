"""Head-to-head vendor scorecard. Read-only.

EnsembleData was cancelled on 23 Jul 2026 but keeps serving until its billing
period closes, and SocialCrawl went live on the 24th. For those few days both
vendors run on the same pipeline, the same markets and the same day, which is a
controlled comparison that cannot be reconstructed once the old account dies.

This captures it. Six measures, all from BigQuery, none of them chosen to
flatter the new vendor:

    rows_per_day        the number everyone asks about first
    topic_coverage      distinct topic_groups the source actually feeds
    freshness_hours     median age of ingested content at collection time
    geo_precision       share of rows carrying a VERIFIED country, not an
                        inferred one (v2locations populated)
    corroboration       share of the source's topics that also appear on a
                        second platform, so a single-platform spike cannot
                        pass as a confirmed trend
    row_integrity       share of rows that are actually USABLE: a parseable
                        date, a resolvable author, a real url and non-trivial
                        text. A row missing these still counts toward volume
                        while contributing nothing a brief can cite, which is
                        exactly how a vendor looks productive and is junk
    cost_per_1k_rows    the commercial line, in vendor-native units

Rows is expected to favour EnsembleData and the other five to favour
SocialCrawl. A scorecard that wins on everything is not believable and should
be checked for a bug rather than shown to anyone.

Usage:
    python scripts/vendor_scorecard.py [--days 7] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_dev"

# Vendor-native price per day, for the cost line. EnsembleData was a flat
# monthly Bronze subscription (~R3,700/mo, so ~R123/day) against a 5,000-unit
# daily cap; SocialCrawl is prepaid credits at a modelled ~290/day. The units
# differ on purpose: the comparable figure is cost per 1,000 rows, not the
# raw daily number.
VENDOR_DAILY_COST_ZAR = {"EnsembleData": 123.0, "socialcrawl": None}

SQL = f"""
WITH win AS (
  SELECT
    source, market, topic_groups, platform, v2locations, published_at, collected_at,
    author_handle, url, text
  FROM `{PROJECT}.{DATASET}.enriched_content`
  WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
    AND source IN ('EnsembleData', 'Reddit', 'socialcrawl')
),
norm AS (
  SELECT
    -- Reddit billed the EnsembleData ledger, so it counts as EnsembleData
    -- volume. Charging the old vendor for rows it paid for is the honest
    -- comparison; excluding them would flatter SocialCrawl.
    IF(source IN ('EnsembleData', 'Reddit'), 'EnsembleData', 'socialcrawl') AS vendor,
    market, platform, topic_groups, v2locations, author_handle, url, text,
    SAFE_CAST(published_at AS TIMESTAMP) AS pub, collected_at
  FROM win
),
base AS (
  SELECT
    vendor,
    COUNT(*) AS rows_total,
    COUNT(DISTINCT DATE(collected_at)) AS days_seen,
    COUNT(DISTINCT platform) AS platforms,
    COUNTIF(v2locations IS NOT NULL AND v2locations != '') AS geo_verified,
    COUNTIF(
      pub IS NOT NULL
      AND author_handle IS NOT NULL AND author_handle != ''
      AND url IS NOT NULL AND url != ''
      AND LENGTH(IFNULL(text, '')) >= 20
    ) AS rows_usable,
    APPROX_QUANTILES(
      TIMESTAMP_DIFF(collected_at, pub, HOUR), 100
    )[OFFSET(50)] AS freshness_hours_median
  FROM norm
  WHERE pub IS NULL OR pub <= collected_at
  GROUP BY vendor
),
topics AS (
  SELECT vendor, COUNT(DISTINCT tg) AS topic_coverage
  FROM norm, UNNEST(IFNULL(topic_groups, [])) AS tg
  WHERE tg != ''
  GROUP BY vendor
),
-- A topic counts as corroborated for a vendor when that vendor surfaced it on
-- two or more distinct platforms in the window.
corrob AS (
  SELECT vendor,
         COUNTIF(plats >= 2) AS corroborated_topics,
         COUNT(*) AS scored_topics
  FROM (
    SELECT vendor, market, tg, COUNT(DISTINCT platform) AS plats
    FROM norm, UNNEST(IFNULL(topic_groups, [])) AS tg
    WHERE tg != ''
    GROUP BY vendor, market, tg
  )
  GROUP BY vendor
)
SELECT
  b.vendor, b.rows_total, b.days_seen, b.platforms, b.geo_verified, b.rows_usable,
  b.freshness_hours_median,
  IFNULL(t.topic_coverage, 0) AS topic_coverage,
  IFNULL(c.corroborated_topics, 0) AS corroborated_topics,
  IFNULL(c.scored_topics, 0) AS scored_topics
FROM base b
LEFT JOIN topics t USING (vendor)
LEFT JOIN corrob c USING (vendor)
ORDER BY b.rows_total DESC
"""


@dataclass
class VendorRow:
    vendor: str
    rows_per_day: int = 0
    topic_coverage: int = 0
    platforms: int = 0
    freshness_hours: float | None = None
    geo_precision_pct: float = 0.0
    corroboration_pct: float = 0.0
    row_integrity_pct: float = 0.0
    cost_per_1k_rows: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def fetch(days: int) -> list[VendorRow]:
    from google.cloud import bigquery as bq

    client = bq.Client(project=PROJECT)
    cfg = bq.QueryJobConfig(query_parameters=[bq.ScalarQueryParameter("days", "INT64", days)])
    out: list[VendorRow] = []
    for r in client.query(SQL, job_config=cfg).result():
        seen = int(r.days_seen or 1) or 1
        rows_total = int(r.rows_total or 0)
        per_day = round(rows_total / seen)
        geo_pct = round(100.0 * int(r.geo_verified or 0) / rows_total, 1) if rows_total else 0.0
        usable_pct = round(100.0 * int(r.rows_usable or 0) / rows_total, 1) if rows_total else 0.0
        corr_pct = (
            round(100.0 * int(r.corroborated_topics or 0) / int(r.scored_topics), 1)
            if int(r.scored_topics or 0)
            else 0.0
        )
        daily_cost = VENDOR_DAILY_COST_ZAR.get(r.vendor)
        cost_1k = round(daily_cost / (per_day / 1000.0), 2) if daily_cost and per_day else None
        out.append(
            VendorRow(
                vendor=r.vendor,
                rows_per_day=per_day,
                topic_coverage=int(r.topic_coverage or 0),
                platforms=int(r.platforms or 0),
                freshness_hours=(
                    float(r.freshness_hours_median)
                    if r.freshness_hours_median is not None
                    else None
                ),
                geo_precision_pct=geo_pct,
                corroboration_pct=corr_pct,
                row_integrity_pct=usable_pct,
                cost_per_1k_rows=cost_1k,
                raw={
                    "rows_total": rows_total,
                    "days_seen": seen,
                    "geo_verified": int(r.geo_verified or 0),
                    "rows_usable": int(r.rows_usable or 0),
                    "scored_topics": int(r.scored_topics or 0),
                },
            )
        )
    return out


def render(rows: list[VendorRow], days: int) -> str:
    if not rows:
        return "VENDOR SCORECARD: no rows in the window."
    hdr = f"VENDOR SCORECARD, last {days} days\n"
    cols = f"  {'measure':22}" + "".join(f"{r.vendor:>16}" for r in rows)
    lines = [hdr, cols, "  " + "-" * (22 + 16 * len(rows))]

    def line(label: str, fn) -> str:
        return f"  {label:22}" + "".join(f"{fn(r):>16}" for r in rows)

    lines.append(line("rows/day", lambda r: f"{r.rows_per_day:,}"))
    lines.append(line("  of which usable", lambda r: f"{r.row_integrity_pct}%"))
    lines.append(line("topic coverage", lambda r: r.topic_coverage))
    lines.append(line("platforms", lambda r: r.platforms))
    lines.append(
        line(
            "freshness (h, median)",
            lambda r: "n/a" if r.freshness_hours is None else f"{r.freshness_hours:.0f}",
        )
    )
    lines.append(line("geo verified %", lambda r: f"{r.geo_precision_pct}%"))
    lines.append(line("corroborated %", lambda r: f"{r.corroboration_pct}%"))
    lines.append(
        line(
            "cost / 1k rows (R)",
            lambda r: "n/a" if r.cost_per_1k_rows is None else f"{r.cost_per_1k_rows}",
        )
    )
    lines.append("")
    lines.append("  Reddit rows count as EnsembleData: they billed its ledger, so charging")
    lines.append("  the old vendor for them is the honest comparison.")
    lines.append("")
    lines.append("  Read rows/day and usable% together. A row with no date, no author or")
    lines.append("  no url counts toward volume and gives a brief nothing to cite.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = fetch(args.days)
    if args.json:
        print(json.dumps([r.__dict__ for r in rows], indent=2))
    else:
        print(render(rows, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
