"""Verify GCP bootstrap: auth, dataset, schemas, cross-region query works.

Run after setup_bigquery.py to confirm the full chain is wired up end-to-end.
Also doubles as Phase 0.3 verification (BigQuery Trends coverage for ZA/NG/KE).
"""

from dotenv import load_dotenv

load_dotenv()

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google.cloud import bigquery
from src.utils.bigquery import get_dataset

project = os.environ["GCP_PROJECT"]
dataset = get_dataset()
client = bigquery.Client(project=project)

print(f"Project: {project}\n")

# Test 1: list datasets
datasets = [d.dataset_id for d in client.list_datasets()]
if dataset not in datasets:
    raise RuntimeError(f"{dataset} not found: {datasets}")
print(f"Datasets: {datasets}")

# Test 2: list tables in the resolved dataset
tables = sorted(
    t.table_id for t in client.list_tables(f"{project}.{dataset}") if t.table_type == "TABLE"
)
expected = {
    "creator_briefs",
    "daily_summary",
    "enriched_content",
    "pipeline_runs",
    "raw_content",
    "trend_analysis",
    "trend_cycles",
    "trend_scores",
    "ugc_tracking",
}
# Subset, not exact-equality: the dataset grows over time (daily_summary was
# added after the original 8). A correct setup can carry extra tables without
# being a mismatch; only a MISSING expected table is a real failure.
missing = expected - set(tables)
if missing:
    raise RuntimeError(f"Table mismatch. Missing {sorted(missing)}. Got {tables}")
print(f"Tables: {len(expected)}/{len(expected)} expected present ({len(tables)} total)")

# Test 3: confirm search_velocity_score column was added
schema = {f.name for f in client.get_table(f"{project}.{dataset}.trend_scores").schema}
if "search_velocity_score" not in schema:
    raise RuntimeError("search_velocity_score missing from trend_scores")
print("search_velocity_score: present on trend_scores")

schema = {f.name for f in client.get_table(f"{project}.{dataset}.enriched_content").schema}
if "search_velocity_score" not in schema:
    raise RuntimeError("search_velocity_score missing from enriched_content")
print("search_velocity_score: present on enriched_content")

# Test 4: cross-region query to google_trends public dataset (proves US multi-region)
print("\nCross-region query to bigquery-public-data.google_trends...")
sql = """
SELECT country_name, term, score
FROM `bigquery-public-data.google_trends.international_top_rising_terms`
WHERE country_name IN ('South Africa', 'Nigeria', 'Kenya')
ORDER BY refresh_date DESC, score DESC
LIMIT 6
"""
try:
    job_config = bigquery.QueryJobConfig(job_timeout_ms=30000)
    rows = list(client.query(sql, job_config=job_config).result())
    print(f"Returned {len(rows)} rows")
    for r in rows[:3]:
        print(f"  {r.country_name}: {r.term} (score={r.score})")
    print("\nBootstrap VERIFIED. Ready for Phase 1.1.")
except Exception as e:
    print(f"Cross-region query FAILED: {e}")
    print("Either dataset is not in US multi-region (drop and recreate)")
    print("or google_trends table schema changed (check fallback query below)")
    try:
        fallback_config = bigquery.QueryJobConfig(job_timeout_ms=30000)
        fallback = client.query(
            "SELECT table_name FROM `bigquery-public-data.google_trends.INFORMATION_SCHEMA.TABLES`",
            job_config=fallback_config,
        ).result()
        print("Available tables in google_trends:")
        for r in fallback:
            print(f"  {r.table_name}")
    except Exception as fallback_exc:
        # Fallback diagnostic itself failed; print and let the original
        # exception propagate via the outer raise.
        print(f"  (fallback table list also failed: {fallback_exc})")
    raise
