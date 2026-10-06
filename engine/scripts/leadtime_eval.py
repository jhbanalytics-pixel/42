#!/usr/bin/env python
"""Lead-time evaluation harness (read-only).

Turns "the engine detects cultural moves early" into a measured number. For
each entry in the standing ground-truth watchlist (configs/watchlist_ground_truth.yaml)
this checks when the move was first ingested (enriched_content) and first
briefed (trend_analysis), classifies the outcome, and prints a per-entry
table plus a summary block.

    python scripts/leadtime_eval.py [--window-days 30] [--json out.json]
                                     [--watchlist path/to/watchlist.yaml]

Read-only. No writes anywhere, no Gemini. Needs application-default
credentials for the engine project, the same as any local read. This is an
observability tool, not a gate: it always exits 0.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.bigquery import get_client, get_dataset

_DEFAULT_WATCHLIST = (
    Path(__file__).resolve().parent.parent / "configs" / "watchlist_ground_truth.yaml"
)
_REQUIRED_FIELDS = ("id", "market", "title", "event_date", "match_terms", "added", "source")

SURFACED = "SURFACED"
INGESTED_NOT_RANKED = "INGESTED_NOT_RANKED"
NEVER_INGESTED = "NEVER_INGESTED"


class WatchlistError(ValueError):
    """Raised when the ground-truth watchlist fails schema validation."""


def load_watchlist(path: Path) -> list[dict[str, Any]]:
    """Load and validate the ground-truth watchlist.

    Each entry must carry every field in _REQUIRED_FIELDS. event_date must
    parse as an ISO date. Raises WatchlistError on any violation so a broken
    config fails loudly rather than silently skipping entries.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("entries")
    if not isinstance(entries, list) or not entries:
        raise WatchlistError(f"{path}: no entries found")

    validated = []
    for entry in entries:
        missing = [f for f in _REQUIRED_FIELDS if f not in entry or entry[f] in (None, "")]
        if missing:
            raise WatchlistError(f"entry {entry.get('id', '?')!r} missing fields: {missing}")
        if not isinstance(entry["match_terms"], list) or not entry["match_terms"]:
            raise WatchlistError(f"entry {entry['id']!r} match_terms must be a non-empty list")

        event_date = entry["event_date"]
        if isinstance(event_date, str):
            event_date = datetime.date.fromisoformat(event_date)
        elif not isinstance(event_date, datetime.date):
            raise WatchlistError(f"entry {entry['id']!r} event_date is not a date: {event_date!r}")

        validated.append(
            {
                "id": str(entry["id"]),
                "market": str(entry["market"]),
                "title": str(entry["title"]),
                "event_date": event_date,
                "match_terms": [str(t).lower() for t in entry["match_terms"]],
                "added": entry["added"],
                "source": str(entry["source"]),
            }
        )
    return validated


def _build_match_condition(match_terms: list[str], haystack_expr: str, param_prefix: str):
    """Build a parameterized SQL OR condition matching any term in haystack_expr.

    Every term becomes a bound @param, never interpolated raw into the SQL,
    so an entry's match_terms cannot inject SQL. Returns (sql_fragment, params).
    """
    if not match_terms:
        raise ValueError("match_terms must not be empty")
    clauses = []
    params = {}
    for i, term in enumerate(match_terms):
        pname = f"{param_prefix}_{i}"
        clauses.append(f"STRPOS({haystack_expr}, @{pname}) > 0")
        params[pname] = term
    return " OR ".join(clauses), params


def _first_ingested(client, dataset, market: str, match_terms: list[str]):
    """Earliest DATE(collected_at) in enriched_content matching any term."""
    from google.cloud import bigquery

    haystack = "LOWER(CONCAT(IFNULL(title,''), ' ', IFNULL(text,'')))"
    condition, term_params = _build_match_condition(match_terms, haystack, "t")
    sql = f"""
    SELECT MIN(DATE(collected_at)) AS first_date
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE market = @market AND ({condition})
    """
    query_params = [bigquery.ScalarQueryParameter("market", "STRING", market)]
    for name, value in term_params.items():
        query_params.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    job_config = bigquery.QueryJobConfig(query_parameters=query_params)
    rows = list(client.query(sql, job_config=job_config).result())
    if not rows:
        return None
    return rows[0].get("first_date") if isinstance(rows[0], dict) else rows[0]["first_date"]


def _first_briefed(client, dataset, market: str, match_terms: list[str]):
    """Earliest trend_date in trend_analysis matching any term for the market."""
    from google.cloud import bigquery

    haystack = "LOWER(CONCAT(IFNULL(trend_synthesis,''), ' ', IFNULL(cultural_context,'')))"
    condition, term_params = _build_match_condition(match_terms, haystack, "t")
    sql = f"""
    SELECT MIN(trend_date) AS first_date
    FROM `{client.project}.{dataset}.trend_analysis`
    WHERE market = @market AND ({condition})
    """
    query_params = [bigquery.ScalarQueryParameter("market", "STRING", market)]
    for name, value in term_params.items():
        query_params.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    job_config = bigquery.QueryJobConfig(query_parameters=query_params)
    rows = list(client.query(sql, job_config=job_config).result())
    if not rows:
        return None
    return rows[0].get("first_date") if isinstance(rows[0], dict) else rows[0]["first_date"]


def classify(first_ingested, first_briefed) -> str:
    """Classify an entry into SURFACED / INGESTED_NOT_RANKED / NEVER_INGESTED."""
    if first_briefed is not None:
        return SURFACED
    if first_ingested is not None:
        return INGESTED_NOT_RANKED
    return NEVER_INGESTED


def lead_days(event_date: datetime.date, observed_date) -> int | None:
    """Days between event_date and observed_date, positive = observed before event.

    Works for the future-event case too (Saba Saba, event_date after today):
    a lead_days value can be negative if the observation lands after the
    event, which is the expected shape for a not-yet-happened event that has
    already been ingested or briefed early (buildup coverage).
    """
    if observed_date is None:
        return None
    return (event_date - observed_date).days


def evaluate_entry(client, dataset, entry: dict[str, Any]) -> dict[str, Any]:
    first_ingested = _first_ingested(client, dataset, entry["market"], entry["match_terms"])
    first_briefed = _first_briefed(client, dataset, entry["market"], entry["match_terms"])
    entry_class = classify(first_ingested, first_briefed)
    return {
        "id": entry["id"],
        "market": entry["market"],
        "title": entry["title"],
        "event_date": entry["event_date"],
        "first_ingested": first_ingested,
        "first_briefed": first_briefed,
        "class": entry_class,
        "ingest_lead_days": lead_days(entry["event_date"], first_ingested),
        "brief_lead_days": lead_days(entry["event_date"], first_briefed),
    }


def _fmt_date(d) -> str:
    return d.isoformat() if d else "-"


def print_report(results: list[dict[str, Any]]) -> None:
    print(f"{'id':<32} {'mkt':<4} {'class':<20} {'ingested':<12} {'briefed':<12} {'lead_days':>9}")
    for r in results:
        lead = r["brief_lead_days"]
        lead_str = str(lead) if lead is not None else "-"
        print(
            f"{r['id']:<32} {r['market']:<4} {r['class']:<20} "
            f"{_fmt_date(r['first_ingested']):<12} {_fmt_date(r['first_briefed']):<12} {lead_str:>9}"
        )

    total = len(results)
    surfaced = [r for r in results if r["class"] == SURFACED]
    ingested_not_ranked = [r for r in results if r["class"] == INGESTED_NOT_RANKED]
    never_ingested = [r for r in results if r["class"] == NEVER_INGESTED]
    surfaced_rate = len(surfaced) / total if total else 0.0
    lead_values = [r["brief_lead_days"] for r in surfaced if r["brief_lead_days"] is not None]
    median_lead = statistics.median(lead_values) if lead_values else None

    print()
    print("summary:")
    print(f"  entries evaluated: {total}")
    print(f"  surfaced rate: {surfaced_rate:.0%} ({len(surfaced)}/{total})")
    print(f"  ingested-not-ranked: {len(ingested_not_ranked)}")
    print(f"  never-ingested: {len(never_ingested)}")
    print(
        f"  median brief lead days (surfaced only): {median_lead if median_lead is not None else '-'}"
    )


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    surfaced = [r for r in results if r["class"] == SURFACED]
    ingested_not_ranked = [r for r in results if r["class"] == INGESTED_NOT_RANKED]
    never_ingested = [r for r in results if r["class"] == NEVER_INGESTED]
    lead_values = [r["brief_lead_days"] for r in surfaced if r["brief_lead_days"] is not None]
    return {
        "entries_evaluated": total,
        "surfaced_count": len(surfaced),
        "surfaced_rate": (len(surfaced) / total) if total else 0.0,
        "ingested_not_ranked_count": len(ingested_not_ranked),
        "never_ingested_count": len(never_ingested),
        "median_brief_lead_days": statistics.median(lead_values) if lead_values else None,
    }


def _json_default(obj):
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {obj!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days", type=int, default=30, help="reporting window, informational only"
    )
    parser.add_argument(
        "--json", type=Path, default=None, help="optional path to write JSON output"
    )
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=_DEFAULT_WATCHLIST,
        help="path to the ground-truth watchlist",
    )
    args = parser.parse_args()

    try:
        entries = load_watchlist(args.watchlist)
    except WatchlistError as exc:
        print(f"watchlist error: {exc}")
        return 0

    client = get_client()
    dataset = get_dataset()

    results = []
    for entry in entries:
        try:
            results.append(evaluate_entry(client, dataset, entry))
        except Exception as exc:
            print(f"  ({entry['id']}: read failed, skipping: {exc!r})")

    if not results:
        print("no entries evaluated")
        return 0

    print(f"lead-time evaluation, window {args.window_days} days, {len(results)} entries")
    print_report(results)

    if args.json:
        payload = {
            "window_days": args.window_days,
            "results": results,
            "summary": build_summary(results),
        }
        args.json.write_text(json.dumps(payload, default=_json_default, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
