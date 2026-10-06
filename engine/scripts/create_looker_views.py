"""Deploy BigQuery views for the Looker Studio dashboard.

Creates four views in trends_v2_dev that Thapelo connects to in Looker Studio.
Safe to rerun ,  uses CREATE OR REPLACE VIEW.

Run:
    python scripts/create_looker_views.py
"""

from dotenv import load_dotenv

load_dotenv()

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google.cloud import bigquery
from src.utils.bigquery import get_dataset

PROJECT = os.environ.get("GCP_PROJECT", "ogilvy-trends-v2")
DATASET = get_dataset()
VIEWS_DIR = Path(__file__).parent.parent / "infra" / "bigquery_views"

VIEWS = [
    "v_latest_trend_scores.sql",
    "v_content_volume.sql",
    "v_pipeline_health.sql",
    "v_market_comparison.sql",
    "v_trend_briefs.sql",
    "v_trend_briefs_creators.sql",
    "v_trend_briefs_social_refs.sql",
]


def deploy_views():
    client = bigquery.Client(project=PROJECT)
    print(f"Project : {PROJECT}")
    print(f"Dataset : {DATASET}")
    print()

    for filename in VIEWS:
        view_name = filename.replace(".sql", "")
        sql = (VIEWS_DIR / filename).read_text()
        sql = sql.replace("{project}", PROJECT).replace("{dataset}", DATASET)

        try:
            client.query(sql).result()
            print(f"OK  {view_name}")
        except Exception as e:
            print(f"FAIL {view_name}: {e}")
            sys.exit(1)

    print()
    print("All views deployed.")
    print()
    print("Connect Thapelo to these in Looker Studio:")
    for filename in VIEWS:
        view_name = filename.replace(".sql", "")
        print(f"  {PROJECT}.{DATASET}.{view_name}")


if __name__ == "__main__":
    deploy_views()
