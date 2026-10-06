"""End-to-end smoke test for raw to enrich to score path.

Runs without BigQuery or network. Proves the data contract holds across
build_raw_row, enrich_dataframe, and compute_trend_scores with synthetic
input. Regression guard for the whole pipeline shape.
"""

import sys
from datetime import UTC, date, datetime

import pandas as pd

sys.path.insert(0, "scripts")


def test_raw_to_enrich_to_score_end_to_end():
    from src.ingestion.enrichment import enrich_dataframe

    import run_rss_now as rr

    now = datetime.now(UTC)
    synthetic = [
        {
            "source": "rss",
            "platform": "web",
            "market": "za",
            "query_group": "news",
            "query_term": "amapiano",
            "text": "DJ lineup confirmed in Mzansi for kasi festival",
            "title": "Amapiano showcase in Jozi",
            "author_handle": "@jozimag",
            "url": "https://example.com/1",
            "published_at": now,
            "views": 1000,
            "likes": 50,
            "comments": 5,
            "shares": 2,
        },
    ]
    raw = [rr.build_raw_row(r, "run-x", now) for r in synthetic]
    assert raw[0]["engagement_total"] == 1057.0

    enriched = enrich_dataframe(pd.DataFrame(raw), "za")
    assert len(enriched) == 1
    assert enriched.loc[0, "regional_score"] > 0  # mzansi, jozi, kasi
    assert enriched.loc[0, "slang_score"] > 0  # mzansi, kasi

    market_counts = {
        ("za", "news"): {
            "item_count": 1,
            "source_diversity": 1,
            "regional_avg": float(enriched["regional_score"].mean()),
            "genz_avg": float(enriched["genz_score"].mean()),
            "slang_avg": float(enriched["slang_score"].mean()),
            "watchlist_avg": float(enriched["creator_watchlist_score"].mean()),
            "search_velocity_avg": 0.0,
            "engagement_sum": 1057.0,
            "creator_spread": 1,
        }
    }
    score_rows = rr.compute_trend_scores(market_counts, date.today(), now, {("za", "news"): 0.5})
    assert len(score_rows) == 1
    score = score_rows[0]
    assert score["market"] == "za"
    assert score["query_group"] == "news"
    assert 0.0 <= score["trend_score"] <= 1.0
    assert score["velocity_score"] == 0.5  # passed in
    assert score["engagement_score"] > 0  # engagement_total > 0 now contributes
