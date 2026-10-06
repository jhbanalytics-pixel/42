#!/usr/bin/env python
"""Read-only reconcile coverage report for the PULSE Intelligence Core.

reconcile_shadow_digest.py reports what already landed in reconcile_actions,
i.e. only the claims that survived the ledger prefilter. This script goes one
step earlier: it rebuilds the claim surface from trend_analysis (headline +
social_refs) the same way the pipeline's _reconcile_shadow stage does, so it
can show the drop-off the digest cannot see:

    raw claims (every headline / source-ref that passed the reconcilable
    marker filter) -> kept claims (survived _filter_claims_to_ledger, i.e.
    anchored to a real ledger event) -> actions (stale / corroborated /
    labelled / no_match), with the reasoning behind each action.

Read-only: builds the event ledger via build_event_ledger (a BQ SELECT, no
Gemini call, matching what the production shadow stage runs today with
client=None) and reconciles claims in pure Python. Nothing is written to
BigQuery.

    python scripts/reconcile_coverage_report.py [--date YYYY-MM-DD] [--market za]

Defaults to yesterday UTC. Needs application-default credentials for the
engine project, the same as any local read. The documented Windows +
Python 3.13 BQ SDK segfault (DEVELOPMENT.md Known Gotchas) is a logging /
native-gRPC interaction, not a hard per-box ceiling: `backfill_seed_graph.py`
(commit 8cffcb7) worked around it by disabling logging before any BQ client
call, and the same fix applied here runs a full day's ~2,300+ row
enriched_content pull clean on this box (verified 4 Jul 2026). Kept as a
belt-and-braces default; still safe (and preferred for CI/Cloud Run, where
the crash never applied) to run this from a non-Windows environment too.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from pathlib import Path
from typing import Any

logging.disable(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Some headlines carry emoji; a Windows cp1252 console cannot encode them.
# Replace rather than crash, matching the other read-only report scripts.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from google.api_core.exceptions import NotFound
from src.analysis.claim_gathering import _filter_claims_to_ledger, _reconcile_claims_for_market
from src.analysis.event_ledger import MARKETS, build_event_ledger
from src.analysis.reconcile import (
    _claim_implies_future,
    _correction_allowed,
    _event_identity_confirmed,
    _event_is_fresher,
    _factual_source_count,
    _match_event,
    _normalize,
    reconcile_claims,
)
from src.utils.bigquery import get_client, get_dataset

_CLAIM_TRUNCATE = 90


def _yesterday_utc() -> datetime.date:
    return datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=1)


def _fetch_briefs(client, dataset, trend_date) -> dict[tuple[str, str], dict[str, Any]]:
    """Latest headline + social_refs per (market, query_group) for trend_date.

    Empty-safe: a missing table or a quiet day both return {}.
    """
    sql = f"""
    SELECT market, query_group, headline, social_refs
    FROM `{client.project}.{dataset}.trend_analysis`
    WHERE trend_date = @trend_date
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY market, query_group ORDER BY analyzed_at DESC
    ) = 1
    """
    from google.cloud import bigquery as bq

    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    try:
        rows = list(client.query(sql, job_config=job_config).result())
    except NotFound:
        return {}
    except Exception as exc:
        print(f"  (trend_analysis read failed, treating as no data: {exc!r})")
        return {}

    briefs: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        market = str(row.get("market") or "")
        topic = str(row.get("query_group") or "")
        briefs[(market, topic)] = {
            "market": market,
            "topic": topic,
            "headline": str(row.get("headline") or ""),
            "social_refs": list(row.get("social_refs") or []),
        }
    return briefs


def _truncate(text: str, limit: int = _CLAIM_TRUNCATE) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _explain_action(
    action: dict[str, Any],
    event: dict[str, Any] | None,
    issue_date: str,
    matched_phrase: str = "",
) -> str:
    """Recompute the human-readable reason for an action's outcome.

    Mirrors reconcile.reconcile_claims's own branching so the report explains
    exactly the same rule the deterministic reconciler applied, without
    reaching into reconcile_claims's private return path.
    """
    verb = action.get("action")
    if verb == "no_match" or event is None:
        return "no ledger event anchors to this claim"

    state_label = _normalize(event.get("state_label", "")) or "unknown"
    factual = _factual_source_count(event.get("corroborating_sources") or [])
    conf = float(event.get("confidence") or 0.0)
    contradicted = (
        _claim_implies_future(action.get("claim_before", ""))
        and state_label == "resolved"
        and _event_is_fresher(event, issue_date)
    )

    if verb == "stale":
        return f"contradicted + fresh resolved event, gate passed (factual={factual}, confidence={conf:.2f})"
    if verb == "corroborated":
        return f"fresh agreement with ledger (state={state_label}, factual={factual})"
    if verb == "labelled":
        if contradicted:
            identity = _event_identity_confirmed(
                action.get("claim_before", ""), event, matched_phrase
            )
            if not identity:
                return (
                    "contradicted but the event identity is unconfirmed: the claim and the "
                    "ledger state share only the anchor entity, not the event"
                )
            gate = _correction_allowed(event)
            return (
                f"contradicted but thin evidence, correction gate {'passed' if gate else 'failed'} "
                f"(factual={factual} need>=2, confidence={conf:.2f} need>=0.55)"
            )
        return f"matched event (state={state_label}) but nothing to assert"
    return "unrecognised action"


def _report_market(
    market: str,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
    trend_date: datetime.date,
    client,
    dataset: str,
) -> dict[str, Any]:
    """Recompute raw claims, kept claims, and actions for one market."""
    issue_date = trend_date.isoformat()

    ledger = build_event_ledger(trend_date, market, client=None, bq_client=client, dataset=dataset)

    raw_claims = _reconcile_claims_for_market(market, briefs_by_topic)
    kept_claims = _filter_claims_to_ledger(raw_claims, ledger)
    actions = reconcile_claims(kept_claims, ledger, issue_date)

    action_counts: dict[str, int] = {}
    detail: list[dict[str, Any]] = []
    for action in actions:
        verb = str(action.get("action") or "?")
        action_counts[verb] = action_counts.get(verb, 0) + 1
        event, matched_phrase = _match_event(action.get("claim_before", ""), ledger)
        detail.append(
            {
                "claim_before": action.get("claim_before", ""),
                "action": verb,
                "matched_entity_key": action.get("matched_entity_key", ""),
                "state_label": event.get("state_label") if event else "",
                "corroborating_sources": action.get("receipt_ids", []),
                "confidence": round(float(event.get("confidence") or 0.0), 4) if event else None,
                "confidence_tier": action.get("confidence_tier", ""),
                "why": _explain_action(action, event, issue_date, matched_phrase),
            }
        )

    return {
        "market": market,
        "raw_claims": raw_claims,
        "kept_claims": kept_claims,
        "dropped_by_prefilter": len(raw_claims) - len(kept_claims),
        "ledger_events": len(ledger),
        "action_counts": action_counts,
        "detail": detail,
    }


def _print_market(report: dict[str, Any]) -> None:
    market = report["market"]
    print(f"\n[{market.upper()}]")
    print(f"  ledger events:        {report['ledger_events']}")
    print(f"  raw claims:           {len(report['raw_claims'])}")
    print(
        f"  kept after prefilter: {len(report['kept_claims'])} "
        f"(dropped {report['dropped_by_prefilter']})"
    )
    counts = report["action_counts"]
    print(
        "  actions: "
        f"stale={counts.get('stale', 0)} "
        f"corroborated={counts.get('corroborated', 0)} "
        f"labelled={counts.get('labelled', 0)} "
        f"no_match={counts.get('no_match', 0)}"
    )
    for row in report["detail"]:
        conf = row["confidence"]
        conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
        print(f"    - [{row['action']}] {_truncate(row['claim_before'])!r}")
        print(
            f"        entity={row['matched_entity_key'] or '-'} "
            f"state={row['state_label'] or '-'} "
            f"conf={conf_str} tier={row['confidence_tier']} "
            f"sources={row['corroborating_sources']}"
        )
        print(f"        why: {row['why']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=datetime.date.fromisoformat,
        default=None,
        help="trend_date to report (YYYY-MM-DD). Defaults to yesterday UTC.",
    )
    parser.add_argument(
        "--market",
        choices=MARKETS,
        default=None,
        help="Restrict to one market. Defaults to all three.",
    )
    args = parser.parse_args()
    trend_date = args.date or _yesterday_utc()
    markets = [args.market] if args.market else list(MARKETS)

    client = get_client()
    dataset = get_dataset()
    briefs_by_topic = _fetch_briefs(client, dataset, trend_date)

    print(f"PULSE reconcile coverage report for {trend_date}")
    if not briefs_by_topic:
        print("no trend_analysis rows for this date (nothing to reconcile against)")
        return 0

    for market in markets:
        report = _report_market(market, briefs_by_topic, trend_date, client, dataset)
        _print_market(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
