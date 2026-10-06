#!/usr/bin/env python
"""Read-only shadow reporter for the PULSE Intelligence Core.

During the observation window (RECONCILE_ENABLED on, but the Core still renders
NOTHING) the shadow stage writes two tables a human needs to inspect before any
promotion to Phase 1: event_ledger (the per-market event-state snapshot) and
reconcile_actions (what reconcile WOULD do to each brief claim). This script
reads both for a date and prints a concise per-market summary so the operator
can watch the shadow without opening BigQuery.

    python scripts/reconcile_shadow_digest.py [--date YYYY-MM-DD]

Defaults to yesterday UTC (the date the last cron scored). Needs application-
default credentials for the engine project, the same as any local read.

Empty-safe by contract: before activation the tables do not exist, and on a
quiet day they are empty. Either way this prints a single no-data line and exits
0. It never raises, so it is safe to wire into morning-check.
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252, which cannot encode ledger event text
# (HTML entities, non-Latin titles). Force UTF-8 so printing never raises.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from google.api_core.exceptions import NotFound
from src.utils.bigquery import get_client, get_dataset

MARKETS = ("za", "ng", "ke")

# How many sample rows to surface per section so the digest stays readable.
_LEDGER_SAMPLES = 3
_ACTION_SAMPLES = 2


def _yesterday_utc() -> datetime.date:
    return datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=1)


def _query_rows(client, sql, trend_date):
    """Run a parameterised read keyed on trend_date.

    Returns a list of row dicts, or None when the table does not exist yet
    (pre-activation). A missing table is the expected pre-activation state, not
    an error, so it is caught and signalled with None for the caller to report
    as no-data. Any other failure also degrades to None so the digest never
    raises.
    """
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    try:
        rows = list(client.query(sql, job_config=job_config).result())
    except NotFound:
        return None
    except Exception as exc:
        print(f"  (read failed, treating as no data: {exc!r})")
        return None
    return [dict(r) for r in rows]


def _fetch_ledger(client, dataset, trend_date):
    sql = f"""
    SELECT market, entity_key, event_kind, state_label, state_text,
           confidence, resolved_by, corroborating_sources
    FROM `{client.project}.{dataset}.event_ledger`
    WHERE trend_date = @trend_date
    ORDER BY market, confidence DESC
    """
    return _query_rows(client, sql, trend_date)


def _fetch_actions(client, dataset, trend_date):
    sql = f"""
    SELECT market, action, claim_before, claim_after, matched_entity_key,
           receipt_ids, confidence_tier
    FROM `{client.project}.{dataset}.reconcile_actions`
    WHERE trend_date = @trend_date
    ORDER BY market, action
    """
    return _query_rows(client, sql, trend_date)


def _by_market(rows):
    out: dict[str, list[dict]] = {m: [] for m in MARKETS}
    for row in rows or []:
        out.setdefault(str(row.get("market") or "?"), []).append(row)
    return out


def _state_coverage_line(ledger_rows) -> str:
    """resolved/scheduled/unknown counts across a market's ledger rows.

    Phase 4 prep metric: the graduation-pack analysis (docs/reconcile-
    graduation-pack-2026-07-04.md) found state_label 'unknown' is the single
    biggest blocker to a claim graduating past 'labelled', so this surfaces
    that split without needing a separate report run.
    """
    resolved = sum(1 for r in ledger_rows if str(r.get("state_label")) == "resolved")
    scheduled = sum(1 for r in ledger_rows if str(r.get("state_label")) == "scheduled")
    unknown = sum(1 for r in ledger_rows if str(r.get("state_label")) == "unknown")
    total = len(ledger_rows)
    pct = f"{unknown / total:.0%}" if total else "0%"
    return (
        f"  state coverage: resolved={resolved} scheduled={scheduled} unknown={unknown} "
        f"({unknown}/{total} = {pct} unresolved)"
    )


def _print_market(market, ledger_rows, action_rows):
    print(f"\n[{market.upper()}]")

    # Ledger summary.
    print(f"  ledger events: {len(ledger_rows)}")
    print(_state_coverage_line(ledger_rows))
    for row in ledger_rows[:_LEDGER_SAMPLES]:
        label = str(row.get("state_label") or "?")
        text = str(row.get("state_text") or "").strip()
        if len(text) > 90:
            text = text[:87] + "..."
        conf = row.get("confidence")
        conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
        print(f"    - [{label} conf={conf_str} via {row.get('resolved_by') or '?'}] {text}")

    # Reconcile summary.
    counts: dict[str, int] = {}
    for row in action_rows:
        counts[str(row.get("action") or "?")] = counts.get(str(row.get("action") or "?"), 0) + 1
    checked = len(action_rows)
    stale = counts.get("stale", 0)
    labelled = counts.get("labelled", 0)
    corroborated = counts.get("corroborated", 0)
    no_match = counts.get("no_match", 0)
    print(
        f"  claims checked: {checked} "
        f"(stale {stale}, labelled {labelled}, corroborated {corroborated}, no_match {no_match})"
    )

    # Show a corrected claim and its receipts, the highest-value thing to eyeball.
    corrected = [r for r in action_rows if str(r.get("action")) == "stale"]
    for row in corrected[:_ACTION_SAMPLES]:
        before = str(row.get("claim_before") or "").strip()
        after = str(row.get("claim_after") or "").strip()
        receipts = list(row.get("receipt_ids") or [])
        if len(before) > 70:
            before = before[:67] + "..."
        if len(after) > 70:
            after = after[:67] + "..."
        print(f"    corrected: {before!r}")
        print(f"           ->: {after!r}")
        print(f"      receipts: {receipts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=datetime.date.fromisoformat,
        default=None,
        help="trend_date to report (YYYY-MM-DD). Defaults to yesterday UTC.",
    )
    args = parser.parse_args()
    trend_date = args.date or _yesterday_utc()

    client = get_client()
    dataset = get_dataset()

    ledger = _fetch_ledger(client, dataset, trend_date)
    actions = _fetch_actions(client, dataset, trend_date)

    # Empty-safe: missing tables (None) or zero rows both mean nothing to show.
    if not ledger and not actions:
        print(f"no shadow data for {trend_date} yet (tables not applied or RECONCILE_ENABLED off)")
        return 0

    print(f"PULSE Intelligence Core shadow digest for {trend_date}")
    ledger_by_market = _by_market(ledger)
    actions_by_market = _by_market(actions)
    for market in MARKETS:
        _print_market(market, ledger_by_market.get(market, []), actions_by_market.get(market, []))

    total_ledger = len(ledger or [])
    total_actions = len(actions or [])
    print(
        f"\ntotal: {total_ledger} ledger events, {total_actions} reconcile actions across markets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
