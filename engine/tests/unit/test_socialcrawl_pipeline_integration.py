"""Integration proof for the SocialCrawl connector inside the real pipeline.

Written 24 Jul 2026 after an honest audit found the gap: every existing
SocialCrawl test patches `load_sources` with a fixture and stops at the
connector's own DataFrame. Nothing exercised the connector against the REAL
configs/sources.yaml, and nothing carried its output through the enrichment
that `run_rss_now` applies before writing to BigQuery.

That gap matters because the failure it hides is silent and total: a connector
that returns a frame the pipeline cannot enrich, or whose columns do not match
`enriched_content`, produces an empty market rather than an error. The vendor
switch went live with zero production runs behind it, so this is the only proof
available before the first cron.

Only the HTTP layer is mocked. The config, the connector, the column contract
and the enrichment are all real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from src.ingestion.connectors.base import ENRICHED_COLUMNS, RAW_COLUMNS
from src.ingestion.connectors.socialcrawl import SocialCrawlConnector
from src.utils.config_loader import load_sources

RECENT = (datetime.now(UTC) - timedelta(days=2)).isoformat()


def _post(handle="dj.x", text="Amapiano is everywhere right now #fyp #mzansi"):
    return {
        "computed": {"engagement_rate": 0.05, "language": "en"},
        "post": {
            "id": "v1",
            "url": f"https://www.tiktok.com/@{handle}/video/7236438903077489925",
            "published_at": RECENT,
            "author": {"username": handle, "display_name": "DJ X", "verified": False},
            "content": {"text": text},
            "engagement": {"views": 79130, "likes": 2236, "comments": 16, "shares": 105},
        },
    }


def _resp(items):
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"success": True, "credits_used": 1, "data": {"items": items}}
    return r


def _live_config_frame(market: str = "za") -> pd.DataFrame:
    """Run the connector against the REAL sources.yaml, mocking only HTTP."""
    SocialCrawlConnector.reset_credits()
    session = MagicMock()
    session.headers = {}
    session.get.return_value = _resp([_post()])
    with (
        patch("src.ingestion.connectors.socialcrawl.get_secret", return_value="sc_key"),
        patch("requests.Session", return_value=session),
    ):
        return SocialCrawlConnector(market=market).fetch()


# -- the config the cron will actually read -------------------------------


def test_live_sources_yaml_enables_socialcrawl_with_a_bounded_budget():
    """If this fails, the cron either runs nothing or runs unbounded."""
    cfg = load_sources()["socialcrawl"]
    assert cfg["enabled"] is True
    assert 0 < int(cfg["budget_credits_per_run"]) <= 1000
    assert int(cfg["max_age_days"]) > 0


def test_live_sources_yaml_has_seven_phases_and_three_markets():
    cfg = load_sources()["socialcrawl"]
    assert set(cfg["phases"]) == {
        "discover",
        "creators",
        "search",
        "reddit",
        "accounts",
        "news",
        "facebook",
    }
    for market in ("za", "ng", "ke"):
        block = cfg["markets"][market]
        assert block["terms"], f"{market} has no search terms"
        assert block["subreddits"], f"{market} has no subreddits"


def test_retired_vendors_are_off_in_the_live_config():
    """EnsembleData was cancelled. Leaving either flag true spends the
    ingestion window retrying a dead vendor before the replacement runs."""
    sources = load_sources()
    assert sources["ensemble_budget"]["enabled"] is False
    assert sources["reddit"]["enabled"] is False


# -- the connector against that config ------------------------------------


def test_connector_produces_rows_against_the_live_config():
    df = _live_config_frame()
    assert not df.empty, "connector returned nothing using the real sources.yaml"


def test_frame_matches_the_raw_column_contract_exactly():
    """run_rss_now concatenates connector frames, so an extra or reordered
    column poisons the concat for every other source in the market.

    Note on what this does NOT prove: `fetch` ends with
    `reindex(columns=RAW_COLUMNS, fill_value="")`, so a column dropped from the
    row dict is silently re-added. Mutation-tested 24 Jul 2026 and confirmed
    this assertion cannot fail on a missing key. It guards the reindex staying
    in place; `test_rows_carry_values_the_pipeline_actually_reads` below is the
    one that catches an empty row.
    """
    df = _live_config_frame()
    assert list(df.columns) == list(RAW_COLUMNS)


def test_rows_carry_values_the_pipeline_actually_reads():
    """The real boundary. A row can satisfy the column contract and still be
    useless: the reindex fills a missing field with "", which scoring and the
    brief generator read as absent. These five fields are the ones a brief
    cites, so an empty one is a silent quality failure rather than an error.
    """
    df = _live_config_frame()
    for column in ("source", "platform", "market", "url", "text"):
        blank = int((df[column].astype(str).str.strip() == "").sum())
        assert blank == 0, f"{blank} of {len(df)} rows have an empty {column}"
    # published_at drives the recency window and engagement_per_day; a blank
    # one is tolerated by the connector but must not be the common case.
    dated = int((df["published_at"].astype(str).str.strip() != "").sum())
    assert dated / len(df) >= 0.9, f"only {dated}/{len(df)} rows carry a date"


def test_every_phase_that_can_fire_actually_contributes_rows():
    """A phase present in config but producing nothing means a silent
    regression: the credits are spent and no rows land."""
    df = _live_config_frame()
    groups = set(df["query_group"])
    assert "creator_watchlist" in groups, "creators phase produced no rows"
    assert "socialcrawl" in groups, "search phase produced no rows"
    assert "reddit_sub" in groups, "reddit phase produced no rows"


def test_platform_column_carries_the_real_platform_not_the_vendor():
    """Scoring, seed_graph channel mapping and the creator watchlist all key
    off platform. 'socialcrawl' in that column breaks all three."""
    df = _live_config_frame()
    assert "socialcrawl" not in set(df["platform"])
    assert {"tiktok", "youtube"} & set(df["platform"])


def test_the_run_budget_is_shared_across_markets_not_per_market():
    """Three market instances in one process must draw on one ledger, or the
    cron spends three times the configured cap."""
    SocialCrawlConnector.reset_credits()
    session = MagicMock()
    session.headers = {}
    session.get.return_value = _resp([_post()])
    with (
        patch("src.ingestion.connectors.socialcrawl.get_secret", return_value="sc_key"),
        patch("requests.Session", return_value=session),
    ):
        for market in ("za", "ng", "ke"):
            SocialCrawlConnector(market=market).fetch()
    budget = int(load_sources()["socialcrawl"]["budget_credits_per_run"])
    assert SocialCrawlConnector.credits_used() <= budget


# -- the frame through the real enrichment --------------------------------


def test_frame_survives_the_real_enrichment_and_matches_enriched_columns():
    """The proof that matters. run_rss_now enriches every connector frame
    before writing to enriched_content; if SocialCrawl rows cannot survive
    that, tonight's cron writes an empty market and reports success."""
    enrich = pytest.importorskip("src.ingestion.enrichment")
    df = _live_config_frame()
    out = enrich.enrich_dataframe(df.copy(), market="za")
    assert not out.empty
    # pipeline_run_id is stamped by run_rss_now (line 235) after enrichment,
    # not by enrich_dataframe, so it is legitimately absent here.
    expected = [c for c in ENRICHED_COLUMNS if c != "pipeline_run_id"]
    missing = [c for c in expected if c not in out.columns]
    assert not missing, f"enrichment dropped columns the BQ table needs: {missing}"


def test_enrichment_resolves_the_creator_watchlist_tier_on_socialcrawl_rows():
    """Creator posts are the ONLY source of the watchlist signal. If the
    handle does not match through, the signal is silently zero."""
    enrich = pytest.importorskip("src.ingestion.enrichment")
    SocialCrawlConnector.reset_credits()
    session = MagicMock()
    session.headers = {}
    # tyla is a real tier_1 handle in configs/creators/za.yaml
    session.get.return_value = _resp([_post(handle="tyla")])
    with (
        patch("src.ingestion.connectors.socialcrawl.get_secret", return_value="sc_key"),
        patch("requests.Session", return_value=session),
    ):
        df = SocialCrawlConnector(market="za").fetch()
    out = enrich.enrich_dataframe(df.copy(), market="za")
    tiers = set(out["creator_watchlist_tier"]) - {""}
    assert tiers, "no socialcrawl row resolved a watchlist tier"
