"""Pipeline orchestrator ,  runs all connectors per market and writes to BigQuery.

Port source: trends-mvp/trends-free-mvp/pipeline.py main() (lines 398-451).
Port notes:
  - Instantiate all 5 connectors for the given market
  - Call safe_fetch() on each (returns empty DataFrame on failure, never raises)
  - Concatenate results and pass through enrichment layer
  - Insert to BigQuery raw_content and enriched_content tables
  - Log run to pipeline_runs table

Implementation: Phase 1.9 (1 day estimated).
"""

from src.utils.log_redactor import get_logger

logger = get_logger(__name__)


def run_market(market: str, dry_run: bool = False) -> dict:
    """Run the full ingestion pipeline for one market.

    Args:
        market: Market code ,  'za', 'ng', or 'ke'.
        dry_run: If True, fetch data but skip BigQuery writes.

    Returns:
        Dict with keys: market, rows_raw, rows_enriched, connector_counts, run_id.
    """
    # TODO(Phase 1.9): live orchestrator is scripts/run_rss_now.py. Fold it in here.
    raise NotImplementedError(
        "Phase 1.9: instantiate RSSConnector, YouTubeConnector, GDELTConnector, "
        "EnsembleConnector, BigQueryTrendsConnector for the given market. "
        "Call safe_fetch() on each. Concatenate. Call enrich_dataframe(). "
        "Call bigquery.insert_dataframe() unless dry_run. "
        "Log result to pipeline_runs table."
    )


def run_all_markets(dry_run: bool = False) -> list[dict]:
    """Run the pipeline for all three markets in sequence.

    Args:
        dry_run: If True, skip BigQuery writes.

    Returns:
        List of per-market result dicts from run_market().
    """
    results = []
    for market in ("za", "ng", "ke"):
        try:
            result = run_market(market, dry_run=dry_run)
            results.append({"market": market, "status": "ok", **result})
        except Exception as exc:
            logger.error("run_market failed for market=%s: %s", market, str(exc)[:300])
            results.append({"market": market, "status": "error", "error": str(exc)})
    return results
