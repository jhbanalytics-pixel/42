"""BigQuery client wrapper with retry and environment-aware dataset selection."""

import datetime
import os
import re
import uuid
from typing import Any

import pandas as pd
from google.api_core import retry
from google.api_core.exceptions import (
    InternalServerError,
    ServiceUnavailable,
    TooManyRequests,
)
from google.cloud import bigquery

from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

# BigQuery does not allow query parameters in place of table or column
# identifiers, so any identifier interpolated into MERGE/CREATE SQL has to
# be validated up front. Allow ASCII alphanumerics + underscore. Anything
# else is a programming error or an injection attempt.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str, kind: str = "identifier") -> str:
    """Reject anything that is not a safe BigQuery identifier."""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe {kind}: {name!r}")
    return name


def get_dataset() -> str:
    """Get the BigQuery dataset name based on environment."""
    env = os.environ.get("TRENDS_ENV", "dev")
    base = os.environ.get("BIGQUERY_DATASET", "trends_v2")
    if env == "prod" or base.endswith(f"_{env}"):
        return base
    return f"{base}_{env}"


def get_client() -> bigquery.Client:
    """Get a BigQuery client."""
    project = os.environ.get("GCP_PROJECT")
    return bigquery.Client(project=project)


def insert_dataframe(
    df: pd.DataFrame,
    table_name: str,
    if_exists: str = "append",
) -> int:
    """Insert a DataFrame into a BigQuery table.

    Returns the number of rows inserted.

    Callers should cast DataFrame columns to explicit dtypes that match the
    target table schema before calling. No explicit schema is passed to
    load_table_from_dataframe, so loose or inferred dtypes can fail the load.
    """
    if if_exists not in ("append", "replace"):
        # Guard against a typo silently mapping to WRITE_TRUNCATE and wiping
        # the target table. Only the two explicit modes are accepted.
        raise ValueError(f"if_exists must be 'append' or 'replace', got {if_exists!r}")

    if df.empty:
        logger.warning("Empty DataFrame, skipping insert to %s", table_name)
        return 0

    client = get_client()
    dataset = get_dataset()
    table_ref = f"{client.project}.{dataset}.{table_name}"

    # Every target table exists; a load that could create one needs
    # bigquery.tables.create on the dataset, which the Wave 1 identity does not
    # hold (refused on staging, 4 Sep 2026). The load appends or truncates only.
    job_config = bigquery.LoadJobConfig(
        create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
        write_disposition=(
            bigquery.WriteDisposition.WRITE_APPEND
            if if_exists == "append"
            else bigquery.WriteDisposition.WRITE_TRUNCATE
        ),
    )

    job = client.load_table_from_dataframe(df, table_ref, job_config=job_config)
    job.result()  # Wait for completion

    logger.info("Inserted %d rows into %s", len(df), table_name)
    return len(df)


def merge_dataframe(
    df: pd.DataFrame,
    table_name: str,
    merge_keys: list[str],
) -> int:
    """MERGE a DataFrame into a BigQuery table on merge_keys.

    Rows whose merge_keys match existing rows are UPDATEd in place; the rest
    are INSERTed. Same-day re-runs stay idempotent instead of duplicating.

    Stages via a uniquely-named temp table in the same dataset, runs MERGE,
    then drops the temp in a finally block. Columns missing from the DataFrame
    are left to the target table's DEFAULT (or NULL) on insert.

    Returns the number of rows in the input DataFrame.
    """
    if df.empty:
        logger.warning("Empty DataFrame, skipping merge to %s", table_name)
        return 0

    if not merge_keys:
        raise ValueError("merge_keys must not be empty")

    # Identifier hardening: every name interpolated into the MERGE SQL
    # below must be a safe identifier. Bound parameters are not allowed
    # for table or column names in BigQuery.
    _validate_identifier(table_name, "table_name")
    for key in merge_keys:
        _validate_identifier(key, "merge_key")

    client = get_client()
    dataset = get_dataset()
    full_target = f"{client.project}.{dataset}.{table_name}"

    df_cols = list(df.columns)
    for col in df_cols:
        _validate_identifier(col, "column")
    missing = [k for k in merge_keys if k not in df_cols]
    if missing:
        raise ValueError(f"merge_keys {missing} not present in DataFrame columns")

    non_key_cols = [c for c in df_cols if c not in merge_keys]
    if not non_key_cols:
        raise ValueError(f"merge_keys {merge_keys} cover all DataFrame columns; nothing to update")

    # Pull target schema so we can stage with matching types and avoid
    # pandas-inferred INT64 for all-None columns that target declares STRING.
    target_table = client.get_table(full_target)
    target_fields = {f.name: f for f in target_table.schema}
    stage_schema = [target_fields[c] for c in df_cols if c in target_fields]
    unknown = [c for c in df_cols if c not in target_fields]
    if unknown:
        raise ValueError(f"DataFrame columns not in {table_name} schema: {unknown}")

    stage_name = f"_stage_{table_name}_{uuid.uuid4().hex[:16]}"
    full_stage = f"{client.project}.{dataset}.{stage_name}"

    try:
        stage_config = bigquery.LoadJobConfig(
            schema=stage_schema,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        load_job = client.load_table_from_dataframe(df, full_stage, job_config=stage_config)
        load_job.result()

        # Set a short expiration on the stage table so a hard kill (the
        # 2026-06-10 Cloud Run task timeout) that skips the finally block does
        # not leak _stage_* tables forever. BigQuery sweeps expired tables on
        # its own schedule even when this process dies.
        stage_table = client.get_table(full_stage)
        stage_table.expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        client.update_table(stage_table, ["expires"])

        on_clause = " AND ".join(f"T.{k} = S.{k}" for k in merge_keys)
        update_clause = ", ".join(f"{c} = S.{c}" for c in non_key_cols)
        insert_cols = ", ".join(df_cols)
        insert_vals = ", ".join(f"S.{c}" for c in df_cols)

        merge_sql = (
            f"MERGE `{full_target}` T "
            f"USING `{full_stage}` S "
            f"ON {on_clause} "
            f"WHEN MATCHED THEN UPDATE SET {update_clause} "
            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
        )
        client.query(merge_sql).result()

        logger.info("Merged %d rows into %s on %s", len(df), table_name, merge_keys)
        return len(df)
    finally:
        client.delete_table(full_stage, not_found_ok=True)


_PY_TO_BQ_TYPE = {
    bool: "BOOL",
    int: "INT64",
    float: "FLOAT64",
    str: "STRING",
    # A datetime.date binds as DATE, not STRING; without this a date param falls
    # back to STRING and a DATE_SUB(@d, ...) in the SQL raises (e.g. the
    # continuity lookup silently returned {} and treated every topic as new).
    datetime.date: "DATE",
    datetime.datetime: "TIMESTAMP",
}


def _build_query_parameters(
    params: dict[str, Any] | None,
) -> list[bigquery.ScalarQueryParameter]:
    if not params:
        return []
    query_params = []
    for name, value in params.items():
        bq_type = _PY_TO_BQ_TYPE.get(type(value), "STRING")
        query_params.append(bigquery.ScalarQueryParameter(name, bq_type, value))
    return query_params


@retry.Retry(
    predicate=retry.if_exception_type(
        ServiceUnavailable,
        InternalServerError,
        TooManyRequests,
    ),
    deadline=60,
)
def run_query(sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    """Execute a BigQuery query and return results as DataFrame."""
    client = get_client()
    dataset = get_dataset()

    # Replace {dataset} placeholder in SQL
    sql = sql.replace("{dataset}", dataset)
    sql = sql.replace("{project}", client.project)

    query_params = _build_query_parameters(params)
    job_config = bigquery.QueryJobConfig(query_parameters=query_params) if query_params else None

    job = client.query(sql, job_config=job_config)
    return job.to_dataframe()
