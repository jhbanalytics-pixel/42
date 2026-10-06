"""Fetch rising search terms per market from the Google Trends public dataset.

Fulfils the commitment in the 13 April Trend Scope Alignment email: confirm
BigQuery Trends coverage for ZA, NG, KE before committing to it as the search
velocity signal source.

Run:
    python scripts/fetch_search_velocity.py

Output:
    docs/reference/search_velocity_sample_<date>.json
    Coverage summary printed to stdout.
"""

from dotenv import load_dotenv

load_dotenv()

import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google.cloud import bigquery

MARKETS = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}
DAYS = 7
LIMIT = 25
QUERY_FILE = (
    Path(__file__).parent.parent / "infra" / "bigquery_queries" / "search_velocity_terms.sql"
)
OUTPUT_DIR = Path(__file__).parent.parent / "docs" / "reference"


def main():
    project = os.environ.get("GCP_PROJECT", "ogilvy-trends-v2")
    client = bigquery.Client(project=project)
    sql = QUERY_FILE.read_text()
    results = {}

    print(f"Project: {project}")
    print(f"Query window: last {DAYS} days, top {LIMIT} terms per market\n")

    for code, name in MARKETS.items():
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("country_code", "STRING", code),
                bigquery.ScalarQueryParameter("days", "INT64", DAYS),
                bigquery.ScalarQueryParameter("limit", "INT64", LIMIT),
            ]
        )
        try:
            rows = list(client.query(sql, job_config=job_config).result())
        except Exception as e:
            print(f"{code} ({name}): FAILED ,  {e}")
            results[code] = {"market": name, "row_count": 0, "error": str(e), "terms": []}
            continue

        results[code] = {
            "market": name,
            "row_count": len(rows),
            "terms": [
                {
                    "refresh_date": str(r.refresh_date),
                    "rank": r.rank,
                    "term": r.term,
                    "score": r.score,
                    "percent_gain": r.percent_gain,
                    "search_velocity_score": round(r.search_velocity_score, 4),
                }
                for r in rows
            ],
        }

        print(f"{code} ({name}): {len(rows)} rows")
        if rows:
            top = rows[0]
            print(f"  top term: '{top.term}' ,  score={top.score}, pct_gain={top.percent_gain}%")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / f"search_velocity_sample_{date.today().isoformat()}.json"
    output_file.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nWrote {output_file}")

    print("\n=== Coverage Summary ===")
    all_ok = True
    for code, data in results.items():
        status = "OK" if data["row_count"] > 0 else "EMPTY"
        if status == "EMPTY":
            all_ok = False
        print(f"  {code} ({data['market']}): {data['row_count']} rows [{status}]")

    if all_ok:
        print("\nAll three markets have data. Option A confirmed viable.")
    else:
        print(
            "\nOne or more markets returned no data. Check coverage before committing to this signal."
        )


if __name__ == "__main__":
    main()
