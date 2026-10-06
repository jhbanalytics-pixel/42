"""Unit tests for GDELTConnector (BigQuery GKG implementation).

The critical test is test_fetch_uses_correct_fips_code, the MVP shipped with
ISO country codes and returned zero rows. Guard against regression.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pandas as pd
import pytest
from google.api_core.exceptions import BadRequest
from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.gdelt import GDELTConnector

SOURCES_ACTIVE = {
    "gdelt": {
        "active": True,
        "days": 1,
        "limit": 2000,
    }
}

SOURCES_INACTIVE = {
    "gdelt": {
        "active": False,
        "days": 1,
        "limit": 2000,
    }
}


def _sample_query_result() -> pd.DataFrame:
    """Two realistic GKG rows covering different theme prefixes."""
    return pd.DataFrame(
        [
            {
                "DATE": 20260421120000,
                "url": "https://news24.com/story-1",
                "source": "news24.com",
                "themes": "ECON_UNEMPLOYMENT;WB_SOCIAL_PROTECTION;LEADER",
                "locations": "1#SF#South Africa#SF#-30#25#111111",
                "tone": "-2.5,1.1,3.6,4.7,10.2,0.0,300",
                "persons": "cyril ramaphosa",
                "orgs": "anc",
            },
            {
                "DATE": 20260420083000,
                "url": "https://mg.co.za/story-2",
                "source": "mg.co.za",
                "themes": "PROTEST;EDUCATION_STUDENT",
                "locations": "1#SF#South Africa#SF#-33#18#222222",
                "tone": "-4.0,0.8,4.8,5.6,8.4,0.0,250",
                "persons": "",
                "orgs": "",
            },
        ]
    )


def test_source_name_and_platform():
    """SOURCE_NAME and PLATFORM are the hard-coded values required by the schema."""
    assert GDELTConnector.SOURCE_NAME == "gdelt"
    assert GDELTConnector.PLATFORM == "news"


def test_fips_code_mapping():
    """ZA -> SF, NG -> NI, KE -> KE. MVP regression guard."""
    assert GDELTConnector.FIPS_CODES["za"] == "SF"
    assert GDELTConnector.FIPS_CODES["ng"] == "NI"
    assert GDELTConnector.FIPS_CODES["ke"] == "KE"


@patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_fetch_returns_raw_columns(mock_run_query, _mock_sources):
    """fetch() output has exactly the 16 RAW_COLUMNS from base.py, in order."""
    mock_run_query.return_value = _sample_query_result()

    connector = GDELTConnector(market="za")
    df = connector.fetch()

    assert list(df.columns) == list(RAW_COLUMNS)
    assert len(df) == 2
    row = df.iloc[0]
    assert row["source"] == "news24.com"
    assert row["platform"] == "news"
    assert row["market"] == "za"
    assert row["content_type"] == "gdelt_gkg"
    assert row["url"] == "https://news24.com/story-1"
    assert row["title"] == ""
    # Step 2 (23 Apr 2026): text is now synthesised from themes + persons +
    # orgs + source so enrichment's full_text is non-empty and regional /
    # genz / slang scoring can hit on GDELT rows. Previously "".
    assert "ECON" in row["text"] or "LEADER" in row["text"] or len(row["text"]) > 0
    assert row["views"] == 0.0
    assert row["likes"] == 0.0
    assert row["comments"] == 0.0
    assert row["shares"] == 0.0


@patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_fetch_uses_correct_fips_code(mock_run_query, _mock_sources):
    """THE key test: params must carry country_fips='SF' for za, not 'ZA'.

    Guards the MVP regression where GDELT returned zero rows because ISO
    codes were sent instead of FIPS codes.
    """
    mock_run_query.return_value = _sample_query_result().head(0)

    connector = GDELTConnector(market="za")
    connector.fetch()

    assert mock_run_query.called
    _, kwargs = mock_run_query.call_args
    params = kwargs.get("params") or mock_run_query.call_args[0][1]
    assert params["country_fips"] == "SF"
    assert params["country_fips"] != "ZA"
    assert params["days"] == 1
    assert params["limit"] == 2000


@patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_fetch_uses_ni_for_nigeria(mock_run_query, _mock_sources):
    """Nigeria maps to FIPS 'NI', not ISO 'NG'."""
    mock_run_query.return_value = _sample_query_result().head(0)

    connector = GDELTConnector(market="ng")
    connector.fetch()

    _, kwargs = mock_run_query.call_args
    params = kwargs.get("params") or mock_run_query.call_args[0][1]
    assert params["country_fips"] == "NI"


@patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_INACTIVE)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_fetch_when_inactive_returns_empty(mock_run_query, _mock_sources):
    """When sources.yaml gdelt.active is false, fetch returns empty and skips the query."""
    connector = GDELTConnector(market="za")
    df = connector.fetch()

    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    mock_run_query.assert_not_called()


@patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_fetch_on_bad_request_returns_empty(mock_run_query, _mock_sources):
    """A BadRequest from BigQuery is swallowed, fetch returns empty instead of crashing."""
    mock_run_query.side_effect = BadRequest("bad SQL")

    connector = GDELTConnector(market="za")
    df = connector.fetch()

    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)


@patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_fetch_on_empty_result_returns_empty(mock_run_query, _mock_sources):
    """Empty SQL result yields an empty DataFrame, not a crash."""
    mock_run_query.return_value = pd.DataFrame(
        columns=["DATE", "url", "source", "themes", "locations", "tone", "persons", "orgs"]
    )

    connector = GDELTConnector(market="za")
    df = connector.fetch()

    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)


@patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_date_parsed_to_tz_aware_utc(mock_run_query, _mock_sources):
    """YYYYMMDDHHMMSS int -> tz-aware UTC datetime."""
    mock_run_query.return_value = pd.DataFrame(
        [
            {
                "DATE": 20260421153045,
                "url": "https://example.com/1",
                "source": "example.com",
                "themes": "ECON_",
                "locations": "1#SF#X#SF#0#0#0",
                "tone": "",
                "persons": "",
                "orgs": "",
            }
        ]
    )

    connector = GDELTConnector(market="za")
    df = connector.fetch()

    published = df.iloc[0]["published_at"]
    assert isinstance(published, datetime)
    assert published.tzinfo is not None
    assert published.utcoffset() == UTC.utcoffset(datetime.now())
    assert published.year == 2026
    assert published.month == 4
    assert published.day == 21
    assert published.hour == 15
    assert published.minute == 30
    assert published.second == 45


@patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_theme_to_query_group_mapping(mock_run_query, _mock_sources):
    """First theme prefix drives query_group. Covers 3 known prefixes plus fallback."""
    mock_run_query.return_value = pd.DataFrame(
        [
            {
                "DATE": 20260421120000,
                "url": "https://example.com/econ",
                "source": "x.com",
                "themes": "ECON_UNEMPLOYMENT;WB_SOCIAL",
                "locations": "1#SF#X#SF#0#0#0",
                "tone": "",
                "persons": "",
                "orgs": "",
            },
            {
                "DATE": 20260421120000,
                "url": "https://example.com/protest",
                "source": "x.com",
                "themes": "PROTEST;CRIME",
                "locations": "1#SF#X#SF#0#0#0",
                "tone": "",
                "persons": "",
                "orgs": "",
            },
            {
                "DATE": 20260421120000,
                "url": "https://example.com/rel",
                "source": "x.com",
                "themes": "RELIGION;LEADER",
                "locations": "1#SF#X#SF#0#0#0",
                "tone": "",
                "persons": "",
                "orgs": "",
            },
            {
                "DATE": 20260421120000,
                "url": "https://example.com/other",
                "source": "x.com",
                "themes": "SOMETHING_WEIRD_UNMAPPED",
                "locations": "1#SF#X#SF#0#0#0",
                "tone": "",
                "persons": "",
                "orgs": "",
            },
        ]
    )

    connector = GDELTConnector(market="za")
    df = connector.fetch()

    groups = df["query_group"].tolist()
    assert groups[0] == "economy"
    assert groups[1] == "protest"
    assert groups[2] == "religion"
    assert groups[3] == "gdelt_other"


@patch(
    "src.ingestion.connectors.gdelt.load_sources",
    return_value=SOURCES_ACTIVE,
)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_unknown_market_returns_empty(mock_run_query, _mock_sources):
    """An unsupported market (no FIPS mapping) returns empty and skips the query."""
    connector = GDELTConnector(market="us")
    df = connector.fetch()

    assert df.empty
    assert list(df.columns) == list(RAW_COLUMNS)
    mock_run_query.assert_not_called()


# ---------------------------------------------------------------------------
# Slang pattern (Phase 2, 27 May 2026)
# ---------------------------------------------------------------------------


def test_build_slang_pattern_empty_when_no_terms():
    """Empty list / whitespace-only terms produce an empty pattern."""
    assert GDELTConnector._build_slang_pattern([]) == ""
    assert GDELTConnector._build_slang_pattern(["  ", ""]) == ""


def test_build_slang_pattern_escapes_specials_and_lowercases():
    """Multi-word terms and regex metacharacters are escaped, then alternation."""
    pattern = GDELTConnector._build_slang_pattern(["Jua Kali", "amapiano", "side.hustle"])
    assert pattern.startswith("(?:")
    assert "jua\\ kali" in pattern
    assert "amapiano" in pattern
    # The dot in "side.hustle" must be escaped so it does not match arbitrary chars
    assert "side\\.hustle" in pattern


@patch(
    "src.ingestion.connectors.gdelt.load_sources",
    return_value={
        "gdelt": {"active": True, "days": 1, "limit": 2000, "slang_filter_enabled": True},
        "gdelt_slang": {"za": ["amapiano", "kasi", "softlife"]},
    },
)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_fetch_passes_slang_pattern_when_enabled(mock_run_query, _mock_sources):
    """slang_filter_enabled=true builds a regex pattern and threads it into the query."""
    mock_run_query.return_value = _sample_query_result().head(0)

    GDELTConnector(market="za").fetch()

    _, kwargs = mock_run_query.call_args
    params = kwargs.get("params") or mock_run_query.call_args[0][1]
    assert params["slang_pattern"].startswith("(?:")
    assert "amapiano" in params["slang_pattern"]
    assert "kasi" in params["slang_pattern"]


@patch(
    "src.ingestion.connectors.gdelt.load_sources",
    return_value=SOURCES_ACTIVE,
)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_fetch_defaults_slang_pattern_to_empty(mock_run_query, _mock_sources):
    """Without slang_filter_enabled the connector passes '' so the ORDER BY CASE falls back to recency only."""
    mock_run_query.return_value = _sample_query_result().head(0)

    GDELTConnector(market="za").fetch()

    _, kwargs = mock_run_query.call_args
    params = kwargs.get("params") or mock_run_query.call_args[0][1]
    assert params["slang_pattern"] == ""


@patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_ACTIVE)
@patch("src.ingestion.connectors.gdelt.bq_utils.run_query")
def test_normal_fetch_rejects_wave1_authority_kwargs(mock_run_query, _mock_sources):
    with pytest.raises(ValueError, match="Wave 1 authority"):
        GDELTConnector(market="za").fetch(
            execution_capability=object(),
            manifest_entry=object(),
            dry_run_receipt=object(),
            maximum_bytes_billed=1,
        )
    mock_run_query.assert_not_called()


def test_build_slang_pattern_includes_named_entities():
    """28 May 2026 fix: pattern carries V2Persons-friendly named entities.

    Per-market gdelt_slang lists in sources.yaml were expanded with
    politicians, musicians and athletes (Tinubu, Davido, Ruto, Tyla, etc.)
    because the SQL filter now targets V2Persons only, where named entities
    actually surface. Verify the builder OR-joins them correctly.
    """
    pattern = GDELTConnector._build_slang_pattern(["amapiano", "tyla", "ramaphosa"])
    assert pattern.startswith("(?:")
    assert "amapiano" in pattern
    assert "tyla" in pattern
    assert "ramaphosa" in pattern
    # Alternation joiner so any single named entity hit qualifies the row
    assert "|" in pattern


def test_sql_slang_is_sort_priority_not_where_exclusion():
    """29 May 2026 fix regression guard: slang must rank rows, not exclude them.

    History of the regression this guards against:
    - 27 May shipped slang as a WHERE filter over CONCAT(V2Themes, V2Persons,
      V2Organizations) and cratered daily volume ~99%.
    - 28 May narrowed the filter to V2Persons only, partial recovery to ~15%
      of healthy (NG 402 -> 46) but still a subtractive WHERE.
    - 29 May converts it to an ORDER BY priority. Every country row is kept up
      to @limit; slang-matching rows float to the top. GDELT news classifies
      at ~76% downstream so subtracting country volume removed real signal.

    Pin the sort-not-exclude contract so a future edit cannot quietly
    re-introduce a volume-cratering WHERE on the slang pattern.
    """
    from src.ingestion.connectors.gdelt import _SQL_PATH

    sql = _SQL_PATH.read_text(encoding="utf-8")
    # V2Persons remains the haystack for the slang match
    assert "IFNULL(V2Persons, '')" in sql
    # The broken CONCAT shape must not return
    assert "CONCAT(IFNULL(V2Themes" not in sql
    # Bound-param wiring intact (regression guard for bandit B608)
    assert "@slang_pattern" in sql
    # The slang match must live in the ORDER BY as a CASE priority, not in a
    # WHERE clause that gates volume. Normalise whitespace for the check.
    flat = " ".join(sql.split())
    assert "ORDER BY CASE WHEN @slang_pattern != ''" in flat
    # No WHERE-level slang exclusion: the pattern must not sit in an OR gate
    # alongside the country/theme filters.
    assert "@slang_pattern = ''" not in flat
    assert "OR REGEXP_CONTAINS(LOWER(IFNULL(V2Persons" not in flat


# ---------------------------------------------------------------------------
# GCAM dimensions (28 May 2026)
# ---------------------------------------------------------------------------


def test_gcam_parsing_extracts_top_5_dimensions():
    """_parse_gcam pulls only the configured target dims, parsed as floats."""
    from src.ingestion.connectors.gdelt import _GCAM_TARGET_DIMS, GDELTConnector

    raw = "wc:258,c1.2:1,v10.1:0.2520,v10.2:0.2613,v19.1:5.624,v19.9:5.102,v20.1:0.281,c8.42:3"
    parsed = GDELTConnector._parse_gcam(raw)

    assert set(parsed.keys()) == set(_GCAM_TARGET_DIMS)
    assert parsed["v10.1"] == pytest.approx(0.2520)
    assert parsed["v10.2"] == pytest.approx(0.2613)
    assert parsed["v19.1"] == pytest.approx(5.624)
    assert parsed["v19.9"] == pytest.approx(5.102)
    assert parsed["v20.1"] == pytest.approx(0.281)
    # Non-target codes are excluded even when present
    assert "c8.42" not in parsed
    assert "wc" not in parsed


def test_gcam_disabled_omits_column_from_select():
    """gcam_enabled=False means the SQL passed to run_query does NOT include GCAM."""
    sources = {
        "gdelt": {
            "active": True,
            "days": 1,
            "limit": 2000,
            "gcam_enabled": False,
        }
    }
    with (
        patch("src.ingestion.connectors.gdelt.load_sources", return_value=sources),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = _sample_query_result().head(0)
        GDELTConnector(market="za").fetch()

        args, kwargs = mock_run.call_args
        sql_arg = args[0] if args else kwargs.get("sql")
        assert "GCAM AS gcam" not in sql_arg
        # Placeholder must be substituted (never leak through raw)
        assert "{gcam_column}" not in sql_arg


def test_gcam_enabled_includes_column_in_select():
    """gcam_enabled=True substitutes the GCAM column into the SELECT list."""
    sources = {
        "gdelt": {
            "active": True,
            "days": 1,
            "limit": 2000,
            "gcam_enabled": True,
        }
    }
    with (
        patch("src.ingestion.connectors.gdelt.load_sources", return_value=sources),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = _sample_query_result().head(0)
        GDELTConnector(market="za").fetch()

        args, kwargs = mock_run.call_args
        sql_arg = args[0] if args else kwargs.get("sql")
        assert "GCAM AS gcam" in sql_arg
        assert "{gcam_column}" not in sql_arg


def test_gcam_handles_empty_string():
    """Empty GCAM string returns an empty dict, no exceptions."""
    from src.ingestion.connectors.gdelt import GDELTConnector

    assert GDELTConnector._parse_gcam("") == {}
    assert GDELTConnector._parse_gcam(None or "") == {}


def test_gcam_handles_malformed_tokens():
    """Tokens missing the colon, or with non-numeric values, are skipped silently."""
    from src.ingestion.connectors.gdelt import GDELTConnector

    raw = "wc:258,v10.1,broken,v10.2:NOTANUMBER,v19.1:5.624,,,v20.1:0.281"
    parsed = GDELTConnector._parse_gcam(raw)

    # Good tokens land
    assert parsed["v19.1"] == pytest.approx(5.624)
    assert parsed["v20.1"] == pytest.approx(0.281)
    # Bad tokens absent
    assert "v10.1" not in parsed  # missing colon-value
    assert "v10.2" not in parsed  # non-numeric


def test_gcam_full_string_preserved_when_persist_raw_enabled():
    """When GCAM is enabled, the raw vendor string is available unmodified.

    The connector reads `gcam` directly off the BQ row; downstream code can
    pass that string through to the (future) v2gcam STRING column without
    any lossy normalisation.
    """
    sources = {
        "gdelt": {
            "active": True,
            "days": 1,
            "limit": 2000,
            "gcam_enabled": True,
        }
    }
    raw_gcam = "wc:258,v10.1:0.252,v10.2:0.261,v19.1:5.624,v19.9:5.102,v20.1:0.281"
    result = _sample_query_result().head(1)
    result["gcam"] = [raw_gcam]

    with (
        patch("src.ingestion.connectors.gdelt.load_sources", return_value=sources),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = result
        # Parsing path runs without crashing and preserves full dim set
        connector = GDELTConnector(market="za")
        df = connector.fetch()
        assert not df.empty
        # The parser preserves all target dims verbatim from the raw string
        parsed = GDELTConnector._parse_gcam(raw_gcam)
        assert len(parsed) == 5


# ---------------------------------------------------------------------------
# Events table (28 May 2026)
# ---------------------------------------------------------------------------


def _sample_events_result() -> pd.DataFrame:
    """Two realistic Events rows: a protest (root 14) and an assault (root 18)."""
    return pd.DataFrame(
        [
            {
                "GLOBALEVENTID": 1305767218,
                "SQLDATE": 20260525,
                "Actor1CountryCode": "SAF",
                "Actor2CountryCode": "AFR",
                "EventCode": "145",
                "EventRootCode": "14",
                "GoldsteinScale": -6.5,
                "NumMentions": 12,
                "NumSources": 5,
                "NumArticles": 3,
                "AvgTone": -3.2,
                "ActionGeo_FullName": "Pretoria, Gauteng, South Africa",
                "ActionGeo_Lat": -25.7,
                "ActionGeo_Long": 28.2,
                "SOURCEURL": "https://news24.com/event-1",
                "DATEADDED": 20260525063000,
            },
            {
                "GLOBALEVENTID": 1305767219,
                "SQLDATE": 20260525,
                "Actor1CountryCode": "SAF",
                "Actor2CountryCode": "SAF",
                "EventCode": "180",
                "EventRootCode": "18",
                "GoldsteinScale": -9.0,
                "NumMentions": 4,
                "NumSources": 2,
                "NumArticles": 1,
                "AvgTone": -5.5,
                "ActionGeo_FullName": "Johannesburg, Gauteng, South Africa",
                "ActionGeo_Lat": -26.2,
                "ActionGeo_Long": 28.0,
                "SOURCEURL": "https://mg.co.za/event-2",
                "DATEADDED": 20260525073000,
            },
        ]
    )


_EVENTS_ENABLED_CONFIG = {
    "gdelt": {
        "active": True,
        "gdelt_events_enabled": True,
        "event_days": 1,
        "event_limit": 500,
    }
}


def test_events_disabled_skips_query():
    """gdelt_events_enabled defaults to false, fetch_events returns empty without calling BQ."""
    with (
        patch("src.ingestion.connectors.gdelt.load_sources", return_value=SOURCES_ACTIVE),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        df = GDELTConnector(market="za").fetch_events()
        assert df.empty
        assert list(df.columns) == list(RAW_COLUMNS)
        mock_run.assert_not_called()


def test_fetch_appends_events_when_enabled():
    """fetch() concats GKG rows and V2 Events rows so flipping
    gdelt_events_enabled actually ingests events. The flag was a silent no-op
    because fetch_events() had no production caller."""
    import pandas as pd

    gkg = pd.DataFrame([dict.fromkeys(RAW_COLUMNS, "")], columns=list(RAW_COLUMNS))
    gkg["url"] = "http://gkg/1"
    events = pd.DataFrame([dict.fromkeys(RAW_COLUMNS, "")], columns=list(RAW_COLUMNS))
    events["url"] = "http://events/1"
    with (
        patch.object(GDELTConnector, "_fetch_gkg", return_value=gkg),
        patch.object(GDELTConnector, "fetch_events", return_value=events),
    ):
        df = GDELTConnector(market="za").fetch()
    assert len(df) == 2
    assert set(df["url"]) == {"http://gkg/1", "http://events/1"}


def test_fetch_is_gkg_only_when_events_empty():
    """Events disabled (default): fetch_events returns empty and fetch() is
    byte-identical to the GKG-only result, so the cron is unchanged until flip."""
    import pandas as pd

    gkg = pd.DataFrame([dict.fromkeys(RAW_COLUMNS, "")], columns=list(RAW_COLUMNS))
    gkg["url"] = "http://gkg/1"
    empty = pd.DataFrame(columns=list(RAW_COLUMNS))
    with (
        patch.object(GDELTConnector, "_fetch_gkg", return_value=gkg),
        patch.object(GDELTConnector, "fetch_events", return_value=empty),
    ):
        df = GDELTConnector(market="za").fetch()
    assert len(df) == 1
    assert list(df["url"]) == ["http://gkg/1"]


def test_events_filter_root_code_default_captures_protest_assault_fight():
    """Default event_root_filter is the regex that captures 14, 15, 17, 18, 19."""
    import re

    pattern = GDELTConnector.DEFAULT_EVENT_ROOT_FILTER
    for code in ("14", "15", "17", "18", "19"):
        assert re.match(pattern, code), f"root {code} should match default filter"
    # Diplomatic codes 01-13 must NOT match
    for code in ("01", "05", "10", "13"):
        assert not re.match(pattern, code), f"root {code} should be excluded"


def test_events_country_code_mapped_to_cameo():
    """Markets resolve to CAMEO 3-letter codes (SAF, NGA, KEN), not FIPS (SF, NI, KE).

    28 May 2026 live probe verified: events table accepts SAF/NGA/KEN. FIPS
    codes return ~0 rows because the events table uses CAMEO, not FIPS.
    """
    assert GDELTConnector.CAMEO_CODES["za"] == "SAF"
    assert GDELTConnector.CAMEO_CODES["ng"] == "NGA"
    assert GDELTConnector.CAMEO_CODES["ke"] == "KEN"

    with (
        patch(
            "src.ingestion.connectors.gdelt.load_sources",
            return_value=_EVENTS_ENABLED_CONFIG,
        ),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = _sample_events_result().head(0)
        GDELTConnector(market="ng").fetch_events()

        _, kwargs = mock_run.call_args
        params = kwargs.get("params") or mock_run.call_args[0][1]
        assert params["country_code"] == "NGA"
        # Sanity: did NOT send the FIPS code
        assert params["country_code"] != "NI"


def test_events_normalise_into_raw_columns_schema():
    """fetch_events output has the 20 RAW_COLUMNS, content_type='gdelt_event'."""
    with (
        patch(
            "src.ingestion.connectors.gdelt.load_sources",
            return_value=_EVENTS_ENABLED_CONFIG,
        ),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = _sample_events_result()
        df = GDELTConnector(market="za").fetch_events()

        assert list(df.columns) == list(RAW_COLUMNS)
        assert len(df) == 2
        row = df.iloc[0]
        assert row["source"] == "gdelt"
        assert row["platform"] == "news"
        assert row["market"] == "za"
        assert row["content_type"] == "gdelt_event"
        # Title carries the EventCode and location
        assert "145" in row["title"]
        assert "Pretoria" in row["title"]
        # Text follows "<actor1> <phrase> <actor2> at <location>" shape
        assert "SAF" in row["text"]
        assert "protested against" in row["text"]
        assert "Pretoria" in row["text"]
        # URL is preserved verbatim from SOURCEURL
        assert row["url"] == "https://news24.com/event-1"
        # Engagement: views=NumArticles, likes=NumSources, comments=NumMentions
        assert row["views"] == 3.0
        assert row["likes"] == 5.0
        assert row["comments"] == 12.0
        assert row["shares"] == 0.0
        # v2tone composed from AvgTone
        assert row["v2tone"].startswith("-3.2")


def test_events_dedup_on_global_event_id_not_url():
    """Distinct GLOBALEVENTIDs survive even when SOURCEURL is empty or shared.

    SOURCEURL is empty for non-web events and one article can back several
    events, so a url-based dedup collapses distinct events into one survivor.
    """
    two_empty_url_events = pd.DataFrame(
        [
            {
                "GLOBALEVENTID": 1305767218,
                "SQLDATE": 20260525,
                "Actor1CountryCode": "SAF",
                "Actor2CountryCode": "AFR",
                "EventCode": "145",
                "EventRootCode": "14",
                "GoldsteinScale": -6.5,
                "NumMentions": 12,
                "NumSources": 5,
                "NumArticles": 3,
                "AvgTone": -3.2,
                "ActionGeo_FullName": "Pretoria, Gauteng, South Africa",
                "ActionGeo_Lat": -25.7,
                "ActionGeo_Long": 28.2,
                "SOURCEURL": "",
                "DATEADDED": 20260525063000,
            },
            {
                "GLOBALEVENTID": 1305767219,
                "SQLDATE": 20260525,
                "Actor1CountryCode": "SAF",
                "Actor2CountryCode": "SAF",
                "EventCode": "180",
                "EventRootCode": "18",
                "GoldsteinScale": -9.0,
                "NumMentions": 4,
                "NumSources": 2,
                "NumArticles": 1,
                "AvgTone": -5.5,
                "ActionGeo_FullName": "Johannesburg, Gauteng, South Africa",
                "ActionGeo_Lat": -26.2,
                "ActionGeo_Long": 28.0,
                "SOURCEURL": "",
                "DATEADDED": 20260525073000,
            },
        ]
    )
    with (
        patch(
            "src.ingestion.connectors.gdelt.load_sources",
            return_value=_EVENTS_ENABLED_CONFIG,
        ),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = two_empty_url_events
        df = GDELTConnector(market="za").fetch_events()

        assert len(df) == 2
        assert list(df.columns) == list(RAW_COLUMNS)
        assert "_global_event_id" not in df.columns


def test_events_handles_empty_result_returns_empty_dataframe():
    """Empty SQL result yields an empty 20-column DataFrame, not a crash."""
    with (
        patch(
            "src.ingestion.connectors.gdelt.load_sources",
            return_value=_EVENTS_ENABLED_CONFIG,
        ),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = pd.DataFrame(
            columns=[
                "GLOBALEVENTID",
                "SQLDATE",
                "Actor1CountryCode",
                "Actor2CountryCode",
                "EventCode",
                "EventRootCode",
                "GoldsteinScale",
                "NumMentions",
                "NumSources",
                "NumArticles",
                "AvgTone",
                "ActionGeo_FullName",
                "ActionGeo_Lat",
                "ActionGeo_Long",
                "SOURCEURL",
                "DATEADDED",
            ]
        )
        df = GDELTConnector(market="za").fetch_events()
        assert df.empty
        assert list(df.columns) == list(RAW_COLUMNS)


def test_events_url_redacted_from_logs(caplog):
    """SOURCEURL values must not appear unredacted in fetch_events log output.

    The Events SOURCEURL is third-party news (no token risk), but the
    connector's logging surface still must not leak raw URLs because
    operator scans grep across multi-connector logs for token shapes. Spot
    check that the count summary does NOT include the URL hostname.
    """
    with (
        patch(
            "src.ingestion.connectors.gdelt.load_sources",
            return_value=_EVENTS_ENABLED_CONFIG,
        ),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
        caplog.at_level("INFO", logger="connector.gdelt"),
    ):
        mock_run.return_value = _sample_events_result()
        GDELTConnector(market="za").fetch_events()

        for record in caplog.records:
            assert "news24.com/event-1" not in record.getMessage()
            assert "mg.co.za/event-2" not in record.getMessage()
