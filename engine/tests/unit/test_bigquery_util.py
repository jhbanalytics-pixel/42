"""Unit tests for src/utils/bigquery.py helpers."""

import datetime
import re
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from src.utils import bigquery as bq_util

# ----- get_dataset() environment-suffix logic -------------------------------


def test_get_dataset_prod_returns_base(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "prod")
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2")
    assert bq_util.get_dataset() == "trends_v2"


def test_get_dataset_staging_appends_env(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2")
    assert bq_util.get_dataset() == "trends_v2_staging"


def test_get_dataset_dev_appends_env(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "dev")
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2")
    assert bq_util.get_dataset() == "trends_v2_dev"


def test_get_dataset_unset_env_defaults_to_dev_suffix(monkeypatch):
    # TRENDS_ENV unset defaults to "dev", so the suffix branch applies.
    monkeypatch.delenv("TRENDS_ENV", raising=False)
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2")
    assert bq_util.get_dataset() == "trends_v2_dev"


def test_get_dataset_respects_custom_base(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", "custom_base")
    assert bq_util.get_dataset() == "custom_base_staging"


def test_get_dataset_unset_base_defaults(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "prod")
    monkeypatch.delenv("BIGQUERY_DATASET", raising=False)
    assert bq_util.get_dataset() == "trends_v2"


def _make_mock_client(schema_fields: list[str]) -> MagicMock:
    """Return a MagicMock bigquery.Client with enough surface area for merge_dataframe."""
    client = MagicMock()
    client.project = "fake-project"

    # Real SchemaField objects (not MagicMocks): LoadJobConfig(schema=...)
    # validates each item at construction and rejects mocks.
    from google.cloud import bigquery as real_bq

    fake_fields = [real_bq.SchemaField(name, "STRING") for name in schema_fields]
    target_table = MagicMock()
    target_table.schema = fake_fields
    client.get_table.return_value = target_table

    load_job = MagicMock()
    load_job.result.return_value = None
    client.load_table_from_dataframe.return_value = load_job

    query_job = MagicMock()
    query_job.result.return_value = None
    client.query.return_value = query_job

    client.delete_table.return_value = None
    return client


def test_merge_dataframe_stage_table_name_has_sufficient_entropy():
    """Stage table suffixes must be 16 hex chars and unique across 100 calls."""
    df = pd.DataFrame({"id": [1, 2], "value": ["a", "b"]})
    columns = list(df.columns)

    stage_names: list[str] = []
    with patch.object(bq_util, "get_client", return_value=_make_mock_client(columns)):
        for _ in range(100):
            client = bq_util.get_client()
            client.load_table_from_dataframe.reset_mock()
            bq_util.merge_dataframe(df, "target_table", ["id"])
            stage_table_ref = client.load_table_from_dataframe.call_args.args[1]
            stage_names.append(stage_table_ref)

    assert len(set(stage_names)) == 100, "stage table names must be unique"

    suffix_re = re.compile(r"_stage_target_table_([0-9a-f]+)$")
    for name in stage_names:
        match = suffix_re.search(name)
        assert match, f"unexpected stage table name: {name}"
        suffix = match.group(1)
        assert len(suffix) >= 16, f"suffix too short: {suffix!r} in {name}"


def test_merge_dataframe_sets_stage_table_expiration():
    """The stage table must get a near-future expiration so a hard kill that
    skips the finally-block cleanup cannot leak _stage_* tables forever."""
    df = pd.DataFrame({"id": [1, 2], "value": ["a", "b"]})
    client = _make_mock_client(list(df.columns))

    before = datetime.datetime.now(datetime.UTC)
    with patch.object(bq_util, "get_client", return_value=client):
        bq_util.merge_dataframe(df, "target_table", ["id"])
    after = datetime.datetime.now(datetime.UTC)

    client.update_table.assert_called_once()
    staged_table, fields = client.update_table.call_args.args
    assert fields == ["expires"]
    expires = staged_table.expires
    assert before + datetime.timedelta(minutes=55) <= expires
    assert expires <= after + datetime.timedelta(hours=1)


# ----- insert_dataframe if_exists validation --------------------------------


@pytest.mark.parametrize("bad_value", ["truncate", "replace_all", "APPEND", "", "overwrite"])
def test_insert_dataframe_rejects_unknown_if_exists(bad_value):
    """Any if_exists outside {"append","replace"} must raise rather than
    silently mapping to WRITE_TRUNCATE and wiping the target table."""
    df = pd.DataFrame({"id": [1]})
    with pytest.raises(ValueError, match="if_exists"):
        bq_util.insert_dataframe(df, "target_table", if_exists=bad_value)


def test_insert_dataframe_replace_maps_to_write_truncate():
    df = pd.DataFrame({"id": [1, 2]})
    client = _make_mock_client(list(df.columns))
    captured = {}

    def _load(_df, _ref, job_config):
        captured["disposition"] = job_config.write_disposition
        captured["create"] = job_config.create_disposition
        job = MagicMock()
        job.result.return_value = None
        return job

    client.load_table_from_dataframe.side_effect = _load
    from google.cloud import bigquery as real_bq

    with patch.object(bq_util, "get_client", return_value=client):
        bq_util.insert_dataframe(df, "target_table", if_exists="replace")
    assert captured["disposition"] == real_bq.WriteDisposition.WRITE_TRUNCATE
    # The load never creates a table: every target exists and the Wave 1 identity
    # holds no bigquery.tables.create on the dataset (4 Sep 2026).
    assert captured["create"] == real_bq.CreateDisposition.CREATE_NEVER


def test_insert_dataframe_append_maps_to_write_append():
    df = pd.DataFrame({"id": [1, 2]})
    client = _make_mock_client(list(df.columns))
    captured = {}

    def _load(_df, _ref, job_config):
        captured["disposition"] = job_config.write_disposition
        job = MagicMock()
        job.result.return_value = None
        return job

    client.load_table_from_dataframe.side_effect = _load
    from google.cloud import bigquery as real_bq

    with patch.object(bq_util, "get_client", return_value=client):
        bq_util.insert_dataframe(df, "target_table", if_exists="append")
    assert captured["disposition"] == real_bq.WriteDisposition.WRITE_APPEND


def test_build_query_parameters_binds_date_and_datetime():
    # A datetime.date must bind as DATE, not fall back to STRING (a STRING bind
    # made DATE_SUB(@d, ...) raise and silently disarmed the continuity lookup).
    params = bq_util._build_query_parameters({"trend_date": datetime.date(2026, 6, 21)})
    assert len(params) == 1
    assert params[0].type_ == "DATE"
    ts = bq_util._build_query_parameters({"ts": datetime.datetime(2026, 6, 21, 12, 0)})
    assert ts[0].type_ == "TIMESTAMP"


def test_get_dataset_does_not_double_suffix_an_already_suffixed_base(monkeypatch):
    # The Wave 1 contract pins BIGQUERY_DATASET=trends_v2_staging with TRENDS_ENV=staging.
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2_staging")
    assert bq_util.get_dataset() == "trends_v2_staging"
