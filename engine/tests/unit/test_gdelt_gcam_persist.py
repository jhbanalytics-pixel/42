"""Wave 0.2: GDELT GCAM emotion column persistence.

Proves the v2gcam STRING field is populated on the connector's output rows
when gdelt.gcam_enabled is true, stays empty when the flag is off (the dark
default), and that adding v2gcam to RAW_COLUMNS leaves the output frame
carrying the full canonical schema in either case.

These tests guard the column-add + wiring that must land BEFORE the flag
flips. They do NOT flip the flag (sources.yaml is untouched); they patch
load_sources in-test.
"""

from unittest.mock import patch

import pandas as pd
from src.ingestion.connectors.base import RAW_COLUMNS
from src.ingestion.connectors.gdelt import _GCAM_TARGET_DIMS, GDELTConnector

# The five target dims plus a leading word-count token and a couple of
# non-target tokens that must be dropped from the persisted string.
_RAW_GCAM = "wc:258,c1.2:1,v10.1:0.252,v10.2:0.261,v19.1:5.624,v19.9:5.102,v20.1:0.281,c8.42:3"


def _one_gkg_row(with_gcam: bool) -> pd.DataFrame:
    """One realistic GKG result row; optionally carrying the raw GCAM column.

    When gcam_enabled is true the SQL aliases the GCAM column to `gcam`, so the
    BQ result frame has that key. When false the column is never selected, so
    the key is absent, mirroring the live shape.
    """
    row = {
        "DATE": 20260619120000,
        "url": "https://news24.com/gcam-1",
        "source": "news24.com",
        "themes": "ECON_UNEMPLOYMENT;LEADER",
        "locations": "1#SF#South Africa#SF#-30#25#111111",
        "tone": "-2.5,1.1,3.6,4.7,10.2,0.0,300",
        "persons": "cyril ramaphosa",
        "orgs": "anc",
    }
    if with_gcam:
        row["gcam"] = _RAW_GCAM
    return pd.DataFrame([row])


def _sources(gcam_enabled: bool) -> dict:
    return {
        "gdelt": {
            "active": True,
            "days": 1,
            "limit": 2000,
            "gcam_enabled": gcam_enabled,
        }
    }


def test_flag_on_populates_v2gcam():
    """gcam_enabled=True writes the parsed target dims into v2gcam as code:value."""
    with (
        patch("src.ingestion.connectors.gdelt.load_sources", return_value=_sources(True)),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = _one_gkg_row(with_gcam=True)
        df = GDELTConnector(market="za").fetch()

    assert len(df) == 1
    v2gcam = df.iloc[0]["v2gcam"]
    # Only the five float-valued target dims persist, in their fixed order.
    assert v2gcam == "v10.1:0.252,v10.2:0.261,v19.1:5.624,v19.9:5.102,v20.1:0.281"
    # Non-target tokens are dropped from the persisted string.
    assert "wc:" not in v2gcam
    assert "c8.42" not in v2gcam
    # Every target dim is present.
    for code in _GCAM_TARGET_DIMS:
        assert f"{code}:" in v2gcam


def test_flag_off_leaves_v2gcam_empty():
    """gcam_enabled=False (dark default): SQL omits GCAM, v2gcam stays empty."""
    with (
        patch("src.ingestion.connectors.gdelt.load_sources", return_value=_sources(False)),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = _one_gkg_row(with_gcam=False)
        df = GDELTConnector(market="za").fetch()

    assert len(df) == 1
    assert df.iloc[0]["v2gcam"] == ""


def test_row_keeps_all_raw_columns_flag_on():
    """Output frame carries the full RAW_COLUMNS (incl. v2gcam) when flag on."""
    with (
        patch("src.ingestion.connectors.gdelt.load_sources", return_value=_sources(True)),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = _one_gkg_row(with_gcam=True)
        df = GDELTConnector(market="za").fetch()

    assert list(df.columns) == list(RAW_COLUMNS)
    assert "v2gcam" in df.columns


def test_row_keeps_all_raw_columns_flag_off():
    """Output frame carries the full RAW_COLUMNS (incl. v2gcam) when flag off."""
    with (
        patch("src.ingestion.connectors.gdelt.load_sources", return_value=_sources(False)),
        patch("src.ingestion.connectors.gdelt.bq_utils.run_query") as mock_run,
    ):
        mock_run.return_value = _one_gkg_row(with_gcam=False)
        df = GDELTConnector(market="za").fetch()

    assert list(df.columns) == list(RAW_COLUMNS)
    assert "v2gcam" in df.columns


def test_normalise_row_unit_flag_on_and_off():
    """_normalise_row direct: flag toggles v2gcam, schema keys constant.

    Bypasses the fetch plumbing so the persistence contract is pinned at the
    row level: same input row, v2gcam present only when the flag is on, and
    the returned dict always carries every RAW_COLUMNS key.
    """
    connector = GDELTConnector(market="za")
    raw_row = {
        "source": "news24.com",
        "url": "https://news24.com/gcam-1",
        "themes": "ECON_UNEMPLOYMENT",
        "persons": "cyril ramaphosa",
        "orgs": "anc",
        "locations": "1#SF#X#SF#0#0#0",
        "tone": "",
        "gcam": _RAW_GCAM,
    }

    on = connector._normalise_row(raw_row, gcam_enabled=True)
    off = connector._normalise_row(raw_row, gcam_enabled=False)

    assert on["v2gcam"] == "v10.1:0.252,v10.2:0.261,v19.1:5.624,v19.9:5.102,v20.1:0.281"
    assert off["v2gcam"] == ""
    for key in RAW_COLUMNS:
        assert key in on
        assert key in off
