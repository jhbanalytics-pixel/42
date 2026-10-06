"""Unit tests for BigQueryTrendsConnector."""

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
from google.api_core.exceptions import BadRequest
from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.bigquery_trends import BigQueryTrendsConnector

SOURCES_ACTIVE = {
    "bigquery_trends": {
        "active": True,
        "days": 7,
        "limit": 25,
    }
}

SOURCES_INACTIVE = {
    "bigquery_trends": {
        "active": False,
        "days": 7,
        "limit": 25,
    }
}


def _sample_query_result() -> pd.DataFrame:
    """One realistic row matching the SQL output columns."""
    return pd.DataFrame(
        [
            {
                "country_code": "ZA",
                "country_name": "South Africa",
                "refresh_date": date(2026, 4, 20),
                "week": date(2026, 4, 13),
                "rank": 1,
                "term": "load shedding stage 6",
                "score": 100,
                "percent_gain": 450,
                "search_velocity_score": 0.45,
            },
            {
                "country_code": "ZA",
                "country_name": "South Africa",
                "refresh_date": date(2026, 4, 20),
                "week": date(2026, 4, 13),
                "rank": 2,
                "term": "amapiano mix 2026",
                "score": 92,
                "percent_gain": 320,
                "search_velocity_score": 0.32,
            },
        ]
    )


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_fetch_returns_raw_columns(mock_run_query, _mock_sources):
    """fetch() output has exactly the 16 RAW_COLUMNS from base.py, in order."""
    mock_run_query.return_value = _sample_query_result()

    connector = BigQueryTrendsConnector(market="za")
    # Success path now logs INFO; patch logger to dodge Win+Py3.13 segfault.
    connector.logger = MagicMock()
    df = connector.fetch()

    assert list(df.columns) == list(RAW_COLUMNS)
    assert len(df) == 2
    # The four GDELT v2 fields are not set by the trends normaliser, so they
    # must surface as empty strings, never the literal "nan" in raw_content.
    for col in ("v2tone", "v2persons", "v2orgs", "v2locations"):
        assert df[col].tolist() == ["", ""], f"{col} should be '' not {df[col].tolist()}"


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_INACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_fetch_when_inactive_in_config_returns_empty(mock_run_query, _mock_sources):
    """When sources.yaml bigquery_trends.active is false, fetch returns empty and skips the query."""
    connector = BigQueryTrendsConnector(market="za")
    df = connector.fetch()

    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    mock_run_query.assert_not_called()


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_fetch_on_query_failure_returns_empty(mock_run_query, _mock_sources):
    """A BadRequest from BigQuery is swallowed, and fetch returns empty instead of crashing."""
    mock_run_query.side_effect = BadRequest("bad SQL")

    connector = BigQueryTrendsConnector(market="za")
    # logger.error path; patch to dodge Win+Py3.13+logging segfault locally.
    connector.logger = MagicMock()
    df = connector.fetch()

    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_fetch_maps_rising_terms_correctly(mock_run_query, _mock_sources):
    """A known input row maps cleanly to title, query_term, published_at, and the fixed fields."""
    mock_run_query.return_value = _sample_query_result().head(1)

    connector = BigQueryTrendsConnector(market="za")
    connector.logger = MagicMock()
    df = connector.fetch()

    row = df.iloc[0]
    assert row["title"] == "load shedding stage 6"
    assert row["query_term"] == "load shedding stage 6"
    assert row["text"] == ""
    assert row["url"] == ""
    assert row["source"] == "Google Trends"
    assert row["platform"] == "google_search"
    assert row["content_type"] == "search_term"
    assert row["query_group"] == "search_intent"
    assert row["views"] == 0.0
    assert row["likes"] == 0.0
    assert row["comments"] == 0.0
    assert row["shares"] == 0.0
    # published_at comes from the `week` column (week_start of the trend)
    published = row["published_at"]
    assert published is not None
    assert published.year == 2026
    assert published.month == 4
    assert published.day == 13


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_market_propagated_to_all_rows(mock_run_query, _mock_sources):
    """Every emitted row carries the connector's market code (lowercased, as-is)."""
    mock_run_query.return_value = _sample_query_result()

    for market in ("za", "ng"):
        connector = BigQueryTrendsConnector(market=market)
        connector.logger = MagicMock()
        df = connector.fetch()
        assert not df.empty
        assert (df["market"] == market).all(), (
            f"Expected all rows to have market={market}, got {df['market'].unique()}"
        )


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_fetch_empty_result_returns_empty_dataframe(mock_run_query, _mock_sources):
    """Empty SQL result (e.g. KE has no data in the public dataset) yields an empty DataFrame, not a crash."""
    mock_run_query.return_value = pd.DataFrame(
        columns=[
            "country_code",
            "country_name",
            "refresh_date",
            "week",
            "rank",
            "term",
            "score",
            "percent_gain",
            "search_velocity_score",
        ]
    )

    connector = BigQueryTrendsConnector(market="ke")
    # KE empty hits the new INFO "expected zero" branch; patch logger to
    # dodge Win+Py3.13+logging segfault locally. CI Ubuntu unaffected.
    connector.logger = MagicMock()
    df = connector.fetch()

    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_country_code_and_params_are_bound(mock_run_query, _mock_sources):
    """run_query is called with ISO country code, days, and limit from config."""
    mock_run_query.return_value = _sample_query_result().head(0)

    connector = BigQueryTrendsConnector(market="ng")
    # NG is expected-non-zero, so empty-result hits the new ERROR branch.
    # Patch the logger so the Win+Py3.13+logging segfault does not fire locally.
    connector.logger = MagicMock()
    connector.fetch()

    assert mock_run_query.called
    _, kwargs = mock_run_query.call_args
    params = kwargs.get("params") or mock_run_query.call_args[0][1]
    assert params["country_code"] == "NG"
    assert params["days"] == 7
    assert params["limit"] == 25


def test_connector_class_attributes():
    """SOURCE_NAME and PLATFORM are the hard-coded values required by the schema."""
    assert BigQueryTrendsConnector.SOURCE_NAME == "bigquery_trends"
    assert BigQueryTrendsConnector.PLATFORM == "google_search"


def test_build_row_id_is_deterministic():
    """build_row_id returns the same hash for the same inputs, different for different."""
    a = BigQueryTrendsConnector.build_row_id("za", "amapiano", "2026-04-13")
    b = BigQueryTrendsConnector.build_row_id("za", "amapiano", "2026-04-13")
    c = BigQueryTrendsConnector.build_row_id("ng", "amapiano", "2026-04-13")
    assert a == b
    assert a != c


def _empty_sql_result() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "country_code",
            "country_name",
            "refresh_date",
            "week",
            "rank",
            "term",
            "score",
            "percent_gain",
            "search_velocity_score",
        ]
    )


def _calls_to_messages(mock_logger_method) -> list[str]:
    """Render every (fmt, *args) call on a mocked logger method into a string list."""
    rendered: list[str] = []
    for call in mock_logger_method.call_args_list:
        args = call.args
        if not args:
            continue
        fmt = args[0]
        fmt_args = args[1:]
        try:
            rendered.append(fmt % fmt_args if fmt_args else str(fmt))
        except Exception:
            rendered.append(str(fmt))
    return rendered


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_ke_empty_logs_info_not_error(mock_run_query, _mock_sources):
    """KE is expected-zero per public-dataset coverage. Empty result logs INFO, not ERROR."""
    mock_run_query.return_value = _empty_sql_result()

    connector = BigQueryTrendsConnector(market="ke")
    # Mock the connector logger to dodge the Win+Py3.13+logging segfault and
    # to make the call-site assertion direct.
    connector.logger = MagicMock()
    df = connector.fetch()

    assert df.empty
    assert not connector.logger.error.called, (
        f"Expected no ERROR for KE expected-zero. error() calls: {connector.logger.error.call_args_list}"
    )
    info_msgs = _calls_to_messages(connector.logger.info)
    assert any("expected zero" in m for m in info_msgs), (
        f"Expected INFO 'expected zero' note for KE, got: {info_msgs}"
    )


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_za_empty_logs_error(mock_run_query, _mock_sources):
    """ZA is expected to receive rows. Empty result must ERROR loudly so alerting fires."""
    mock_run_query.return_value = _empty_sql_result()

    connector = BigQueryTrendsConnector(market="za")
    connector.logger = MagicMock()
    df = connector.fetch()

    assert df.empty
    error_msgs = _calls_to_messages(connector.logger.error)
    assert any("0 rows" in m and "expected" in m for m in error_msgs), (
        f"Expected ERROR with '0 rows' and 'expected' for ZA, got: {error_msgs}"
    )


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_ng_empty_logs_error(mock_run_query, _mock_sources):
    """NG mirrors ZA: expected non-zero, so empty result is an ERROR."""
    mock_run_query.return_value = _empty_sql_result()

    connector = BigQueryTrendsConnector(market="ng")
    connector.logger = MagicMock()
    df = connector.fetch()

    assert df.empty
    error_msgs = _calls_to_messages(connector.logger.error)
    assert any("0 rows" in m for m in error_msgs), (
        f"Expected ERROR with '0 rows' for NG, got: {error_msgs}"
    )


SOURCES_WITH_EXPECTED_OVERRIDE = {
    "bigquery_trends": {
        "active": True,
        "days": 7,
        "limit": 25,
        # Operator override: KE coverage just came back online. Promote KE to expected.
        "expected_per_market": {"za": True, "ng": True, "ke": True},
    }
}


@patch(
    "src.ingestion.connectors.bigquery_trends.load_sources",
    return_value=SOURCES_WITH_EXPECTED_OVERRIDE,
)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_expected_per_market_config_override(mock_run_query, _mock_sources):
    """sources.yaml override flipping ke to expected=True elevates KE-empty to ERROR."""
    mock_run_query.return_value = _empty_sql_result()

    connector = BigQueryTrendsConnector(market="ke")
    connector.logger = MagicMock()
    df = connector.fetch()

    assert df.empty
    error_msgs = _calls_to_messages(connector.logger.error)
    assert any("0 rows" in m for m in error_msgs), (
        f"With ke override expected=True, empty must ERROR. Got: {error_msgs}"
    )


# ---------------------------------------------------------------------------
# Wave 2: international_top_terms (steady-state top-chart feed) coverage
# ---------------------------------------------------------------------------

SOURCES_TOP_TERMS_ON = {
    "bigquery_trends": {
        "active": True,
        "days": 7,
        "limit": 25,
        "top_terms": {"enabled": True, "days": 7, "limit": 25},
    }
}

SOURCES_TOP_TERMS_OFF = {
    "bigquery_trends": {
        "active": True,
        "days": 7,
        "limit": 25,
        # top_terms block omitted entirely; treated as disabled.
    }
}


def _sample_top_terms_result() -> pd.DataFrame:
    """Realistic top_terms rows in the shape search_top_terms.sql returns."""
    return pd.DataFrame(
        [
            {
                "country_code": "ZA",
                "country_name": "South Africa",
                "refresh_date": date(2026, 5, 25),
                "week": date(2026, 5, 24),
                "rank": 1,
                "term": "amapiano",
                "score": 100,
            },
            {
                "country_code": "ZA",
                "country_name": "South Africa",
                "refresh_date": date(2026, 5, 25),
                "week": date(2026, 5, 24),
                "rank": 7,
                "term": "load shedding",
                "score": 78,
            },
        ]
    )


def _empty_top_terms_result() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "country_code",
            "country_name",
            "refresh_date",
            "week",
            "rank",
            "term",
            "score",
        ]
    )


@patch(
    "src.ingestion.connectors.bigquery_trends.load_sources",
    return_value=SOURCES_TOP_TERMS_OFF,
)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_top_terms_disabled_skips_query(mock_run_query, _mock_sources):
    """top_terms.enabled missing or false: no second BQ call for the top-terms feed."""
    # Rising-terms call returns one row so the path exercises the success branch.
    mock_run_query.return_value = _sample_query_result().head(1)

    connector = BigQueryTrendsConnector(market="za")
    connector.logger = MagicMock()
    df = connector.fetch()

    # Exactly one run_query call: rising terms. top_terms must not fire.
    assert mock_run_query.call_count == 1
    # And only the rising-feed content_type lands.
    assert (df["content_type"] == "search_term").all()


@patch(
    "src.ingestion.connectors.bigquery_trends.load_sources",
    return_value=SOURCES_TOP_TERMS_ON,
)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_top_terms_country_code_mapped_correctly(mock_run_query, _mock_sources):
    """top_terms call binds the same ZA/NG/KE country code as the rising feed."""
    mock_run_query.side_effect = [
        _sample_query_result().head(0),  # rising empty (expected-true for NG triggers error log)
        _sample_top_terms_result(),  # top_terms returns rows
    ]

    connector = BigQueryTrendsConnector(market="ng")
    connector.logger = MagicMock()
    connector.fetch()

    assert mock_run_query.call_count == 2
    top_call = mock_run_query.call_args_list[1]
    top_params = top_call.kwargs.get("params") or top_call.args[1]
    assert top_params["country_code"] == "NG"
    assert top_params["days"] == 7
    assert top_params["lim"] == 25


@patch(
    "src.ingestion.connectors.bigquery_trends.load_sources",
    return_value=SOURCES_TOP_TERMS_ON,
)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_top_terms_normalise_into_raw_columns_schema(mock_run_query, _mock_sources):
    """top_terms rows map into the 16-column RAW schema with the Wave 2 field choices."""
    mock_run_query.side_effect = [
        _sample_query_result().head(0),  # rising empty for ZA
        _sample_top_terms_result(),  # top_terms ZA returns 2 rows
    ]

    connector = BigQueryTrendsConnector(market="za")
    connector.logger = MagicMock()
    df = connector.fetch()

    assert list(df.columns) == list(RAW_COLUMNS)
    assert len(df) == 2

    row = df.iloc[0]
    assert row["source"] == "bigquery_trends"
    assert row["platform"] == "google_search"
    assert row["market"] == "za"
    assert row["content_type"] == "top_term"
    assert row["query_group"] == "google_trends_top"
    assert row["query_term"] == "amapiano"
    assert row["title"] == "Top search: amapiano"
    assert "amapiano" in row["text"]
    assert "ZA" in row["text"]
    assert "#1" in row["text"]
    assert "2026-05-24" in row["text"]
    # Inverse-rank views: rank 1 -> 25, rank 7 -> 19.
    assert row["views"] == 25.0
    assert df.iloc[1]["views"] == 19.0
    assert row["likes"] == 0.0
    assert row["comments"] == 0.0
    assert row["shares"] == 0.0
    assert row["url"] == ""
    published = row["published_at"]
    assert published is not None
    assert published.year == 2026
    assert published.month == 5
    assert published.day == 24


SOURCES_TOP_TERMS_LIMIT_100 = {
    "bigquery_trends": {
        "active": True,
        "days": 7,
        "limit": 25,
        "top_terms": {"enabled": True, "days": 7, "limit": 100},
    }
}


@patch(
    "src.ingestion.connectors.bigquery_trends.load_sources",
    return_value=SOURCES_TOP_TERMS_LIMIT_100,
)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_top_terms_views_offset_tracks_configured_limit(mock_run_query, _mock_sources):
    """Inverse-rank views derive the offset from the configured limit, not a fixed 26.

    With limit=100, rank 1 -> 100 and rank 50 -> 51. The old hardcoded-26 offset
    gave rank 50 a views=0.0, defeating the engagement encoding past rank 25.
    """
    rows = pd.DataFrame(
        [
            {
                "country_code": "ZA",
                "country_name": "South Africa",
                "refresh_date": date(2026, 5, 25),
                "week": date(2026, 5, 24),
                "rank": 1,
                "term": "amapiano",
                "score": 100,
            },
            {
                "country_code": "ZA",
                "country_name": "South Africa",
                "refresh_date": date(2026, 5, 25),
                "week": date(2026, 5, 24),
                "rank": 50,
                "term": "deep mid rank",
                "score": 40,
            },
        ]
    )
    mock_run_query.side_effect = [
        _sample_query_result().head(0),  # rising empty for ZA
        rows,  # top_terms ZA with limit 100
    ]

    connector = BigQueryTrendsConnector(market="za")
    connector.logger = MagicMock()
    df = connector.fetch()

    assert df.iloc[0]["views"] == 100.0
    assert df.iloc[1]["views"] == 51.0


@patch(
    "src.ingestion.connectors.bigquery_trends.load_sources",
    return_value=SOURCES_TOP_TERMS_ON,
)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_top_terms_handles_empty_result_returns_empty_dataframe(mock_run_query, _mock_sources):
    """Empty top_terms result still yields a 16-column empty DataFrame, no crash."""
    mock_run_query.side_effect = [
        _sample_query_result().head(1),  # rising ZA: 1 row
        _empty_top_terms_result(),  # top_terms ZA: empty
    ]

    connector = BigQueryTrendsConnector(market="za")
    connector.logger = MagicMock()
    df = connector.fetch()

    # Rising row survives, top_terms contributes nothing.
    assert list(df.columns) == list(RAW_COLUMNS)
    assert len(df) == 1
    assert df.iloc[0]["content_type"] == "search_term"


@patch(
    "src.ingestion.connectors.bigquery_trends.load_sources",
    return_value=SOURCES_TOP_TERMS_ON,
)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_top_terms_ke_empty_logs_info_not_error(mock_run_query, _mock_sources):
    """KE top_terms expected-zero per the live probe: empty result logs INFO, not ERROR."""
    mock_run_query.side_effect = [
        _empty_sql_result(),  # rising KE empty (also expected-zero, logs INFO)
        _empty_top_terms_result(),  # top_terms KE empty
    ]

    connector = BigQueryTrendsConnector(market="ke")
    connector.logger = MagicMock()
    df = connector.fetch()

    assert df.empty
    error_msgs = _calls_to_messages(connector.logger.error)
    assert not any("top_terms" in m for m in error_msgs), (
        f"KE top_terms must not ERROR. error() calls: {error_msgs}"
    )
    info_msgs = _calls_to_messages(connector.logger.info)
    assert any("top_terms" in m and "expected zero" in m for m in info_msgs), (
        f"Expected INFO 'top_terms ... expected zero' for KE, got: {info_msgs}"
    )


@patch(
    "src.ingestion.connectors.bigquery_trends.load_sources",
    return_value=SOURCES_TOP_TERMS_ON,
)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_top_terms_za_empty_logs_error(mock_run_query, _mock_sources):
    """ZA top_terms is expected non-zero; empty result must ERROR loudly."""
    mock_run_query.side_effect = [
        _sample_query_result().head(1),  # rising ZA: 1 row (success)
        _empty_top_terms_result(),  # top_terms ZA: empty (alert)
    ]

    connector = BigQueryTrendsConnector(market="za")
    connector.logger = MagicMock()
    connector.fetch()

    error_msgs = _calls_to_messages(connector.logger.error)
    assert any("top_terms" in m and "0 rows" in m for m in error_msgs), (
        f"Expected ERROR with 'top_terms' and '0 rows' for ZA, got: {error_msgs}"
    )


@patch(
    "src.ingestion.connectors.bigquery_trends.load_sources",
    return_value=SOURCES_TOP_TERMS_ON,
)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_top_terms_unknown_market_returns_empty(mock_run_query, _mock_sources):
    """Unknown market short-circuits BEFORE either BQ call; top_terms never runs."""
    connector = BigQueryTrendsConnector(market="xx")
    connector.logger = MagicMock()
    df = connector.fetch()

    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    mock_run_query.assert_not_called()


# ---------------------------------------------------------------------------
# SEARCHVEL revive (SEARCH_VELOCITY_ENABLED): the rising feed carries the
# SQL-computed search_velocity_score only when the flag is on.
# ---------------------------------------------------------------------------


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_search_velocity_flag_off_drops_column(mock_run_query, _mock_sources, monkeypatch):
    """Flag OFF (default): output is exactly RAW_COLUMNS and the SQL-computed
    search_velocity_score is dropped, byte-identical to today."""
    monkeypatch.delenv("SEARCH_VELOCITY_ENABLED", raising=False)
    mock_run_query.return_value = _sample_query_result()

    connector = BigQueryTrendsConnector(market="za")
    connector.logger = MagicMock()
    df = connector.fetch()

    assert list(df.columns) == list(RAW_COLUMNS)
    assert "search_velocity_score" not in df.columns


@patch("src.ingestion.connectors.bigquery_trends.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.bigquery_trends.bq_utils.run_query")
def test_search_velocity_flag_on_carries_value(mock_run_query, _mock_sources, monkeypatch):
    """Flag ON: output gains a search_velocity_score column carrying the real
    SQL value per row (0.45 / 0.32 from the sample)."""
    monkeypatch.setenv("SEARCH_VELOCITY_ENABLED", "true")
    mock_run_query.return_value = _sample_query_result()

    connector = BigQueryTrendsConnector(market="za")
    connector.logger = MagicMock()
    df = connector.fetch()

    assert "search_velocity_score" in df.columns
    assert df.iloc[0]["search_velocity_score"] == 0.45
    assert df.iloc[1]["search_velocity_score"] == 0.32
    # The 20 RAW columns are still all present, in order, ahead of the new slot.
    assert list(df.columns)[: len(RAW_COLUMNS)] == list(RAW_COLUMNS)
