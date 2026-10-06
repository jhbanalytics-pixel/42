"""B0b FORCE_REINGEST_MARKETS idempotency override tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import scripts.run_rss_now as cron


def test_parse_force_reingest_empty():
    with patch.dict("os.environ", {"FORCE_REINGEST_MARKETS": ""}, clear=False):
        assert cron._parse_force_reingest_markets() == set()


def test_parse_force_reingest_markets_list():
    with patch.dict("os.environ", {"FORCE_REINGEST_MARKETS": "ke, za"}, clear=False):
        assert cron._parse_force_reingest_markets() == {"ke", "za"}


def test_cleanup_market_day_rows_runs_delete_for_each_table():
    client = MagicMock()
    with (
        patch("src.utils.bigquery.get_client", return_value=client),
        patch("src.utils.bigquery.get_dataset", return_value="trends_v2_dev"),
    ):
        cron._cleanup_market_day_rows({"ke"})
    assert client.query.call_count == 3


def test_force_reingest_removes_market_from_skip_set():
    with patch.dict("os.environ", {"FORCE_REINGEST_MARKETS": "ke"}, clear=False):
        skip = {"za", "ke"}
        force = cron._parse_force_reingest_markets()
        assert skip - force == {"za"}
