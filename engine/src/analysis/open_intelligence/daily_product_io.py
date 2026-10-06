"""Meter external attempts before dispatch and retain ambiguous paid outcomes."""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

from .brain_contract import canonical_bytes, canonical_digest

# The model backed products and the usage ledger consumers they write under.
MODEL_PRODUCTS = ("trend_analysis", "daily_summary", "seed_insights", "creator_briefs")
USAGE_CONSUMERS = ("trend_analysis", "daily_summary", "seed_insights")
DETERMINISTIC_PRODUCTS = ("trend_scores", "seed_graph", "seed_candidates", "pan_african_stories")
# The natural key of each written table: a retry deletes only rows whose key and content
# this operation recorded writing, since the staging dataset is shared with other origins.
WRITE_KEYS = {
    "trend_scores": ("market", "query_group"),
    "seed_graph": ("market", "term", "term_type", "platform"),
    "seed_candidates": ("candidate_id",),
    "trend_analysis": ("analysis_id",),
    "daily_summary": ("summary_id",),
    "seed_insights": ("insight_id",),
    "creator_briefs": ("brief_id",),
    "pan_african_stories": ("story_id",),
    "gemini_usage": ("consumer", "usage_id"),
}
_READ_ONLY = re.compile(r"\s*(SELECT|WITH)\b", re.I)
# Refused anywhere in the text, literals and comments included, as the first guard did.
_WRITE_WORD = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|CALL|EXPORT|BEGIN|GRANT|REVOKE)\b",
    re.I,
)
# Refused in the statement text, where they could only start or alter another statement.
_SCRIPT_WORD = re.compile(r"\b(EXECUTE|IMMEDIATE|LOAD|SET|DECLARE|COMMIT|ROLLBACK)\b", re.I)
_WRITING_CONFIG = ("destination", "write_disposition", "create_disposition")
# String literals, quoted identifiers and comments, which carry no statement of their own.
# Every quoted form honours backslash escapes, and a comment ends at either line break.
_INERT = re.compile(
    r"'''(?:\\.|[^\\])*?'''|\"\"\"(?:\\.|[^\\])*?\"\"\""
    r"|'(?:\\.|[^'\\\r\n])*'|\"(?:\\.|[^\"\\\r\n])*\"|`(?:\\.|[^`\\\r\n])*`"
    r"|-{2}[^\r\n]*|#[^\r\n]*|/\*.*?\*/",
    re.S,
)


def require_read_only(sql, job_config, code):
    """Refuse anything but one reading statement whose job writes no table.

    Literals, quoted identifiers and comments are blanked before the separator and the
    script words are looked for, so a separator inside a string cannot refuse an honest
    read. A backslash or carriage return left outside them means the text was not read as
    the warehouse reads it, and refuses.
    """
    code_text = _INERT.sub(" ", sql) if isinstance(sql, str) else ""
    if (
        not isinstance(sql, str)
        or _WRITE_WORD.search(sql)
        or not _READ_ONLY.match(code_text)
        or ";" in code_text
        or "\\" in code_text
        or "\r" in code_text
        or _SCRIPT_WORD.search(code_text)
        or (
            job_config is not None
            and any(getattr(job_config, name, None) is not None for name in _WRITING_CONFIG)
        )
    ):
        raise ValueError(code)


_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,299}")


def _date_field(table):
    return "proposed_date" if table == "seed_candidates" else "trend_date"


def _row_digest(row, columns):
    return canonical_digest(canonical_rows([{column: row.get(column) for column in columns}]))


class ProductBudget:
    def __init__(self, limits):
        self.limits = dict(limits)
        self.model_calls = 0
        self.query_count = 0
        self.total_bytes_billed = 0
        self.rows_written = 0
        self.storage_write_count = 0
        self.storage_write_bytes = 0
        self.complete = True

    def require_complete(self):
        if not self.complete:
            raise ValueError("products_effect_unknown")

    def model_attempt(self):
        self.model_capacity()
        self.model_calls += 1

    @property
    def models_funded(self):
        return self.limits["max_model_calls"] > 0

    def model_capacity(self):
        self.require_complete()
        if self.model_calls >= self.limits["max_model_calls"]:
            raise ValueError("products_model_budget_exhausted")

    def metering(self):
        self.require_complete()
        return {
            "query_count": self.query_count,
            "total_bytes_billed": self.total_bytes_billed,
            "storage_write_count": self.storage_write_count,
            "storage_write_bytes": self.storage_write_bytes,
            "rows_written": self.rows_written,
            "model_calls": self.model_calls,
            "vendor_credits": "0",
            "complete": True,
        }


class MeteredModels:
    def __init__(self, models, *, budget, completion, operation_id):
        self._models = models
        self._budget = budget
        self._completion = completion
        self._operation_id = operation_id
        self._sequence = 0

    def generate_content(self, *, model, contents, config):
        self._budget.model_capacity()
        config_value = config.model_dump(mode="json") if hasattr(config, "model_dump") else config
        request = {"model": model, "contents": contents, "config": config_value}
        self._sequence += 1
        attempt = f"model-{self._sequence}"
        try:
            retained = self._completion.begin_attempt(
                self._operation_id, attempt, {"request_digest": canonical_digest(request)}
            )
            if retained is None:
                # Only a dispatched request is a model call; a retained result replays free.
                self._budget.model_attempt()
                response = self._models.generate_content(
                    model=model, contents=contents, config=config
                )
                usage = getattr(response, "usage_metadata", None)
                counts = {
                    name: getattr(usage, name, None)
                    for name in (
                        "prompt_token_count",
                        "candidates_token_count",
                        "thoughts_token_count",
                    )
                }
                if counts["thoughts_token_count"] is None:
                    counts["thoughts_token_count"] = 0
                if any(type(value) is not int or value < 0 for value in counts.values()):
                    raise ValueError("products_model_usage_unknown")
                retained = {"text": response.text, "parsed": response.parsed, "usage": counts}
                self._completion.finish_attempt(self._operation_id, attempt, retained)
            return SimpleNamespace(
                text=retained["text"],
                parsed=retained["parsed"],
                usage_metadata=SimpleNamespace(**retained["usage"]),
            )
        except Exception:
            self._budget.complete = False
            raise


def native_model_sdk(*, project, location):
    from google import genai
    from google.genai import types

    return genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
    )


def canonical_rows(rows):
    def convert(value):
        if isinstance(value, dict):
            return {name: convert(item) for name, item in value.items()}
        if isinstance(value, list | tuple):
            return [convert(item) for item in value]
        if isinstance(value, date | datetime):
            return value.isoformat()
        return value

    return sorted((convert(dict(row)) for row in rows), key=canonical_bytes)


class _QueryJob:
    def __init__(self, job, budget):
        self._job, self._budget, self._rows = job, budget, None

    def result(self, **kwargs):
        if self._rows is None:
            try:
                result = self._job.result(timeout=60, max_results=100001)
                rows = list(result)
                billed = self._job.total_bytes_billed
                if type(billed) is not int or billed < 0:
                    raise ValueError("products_query_usage_unknown")
                self._budget.total_bytes_billed += billed
                if self._budget.total_bytes_billed > self._budget.limits["max_bytes_billed"]:
                    raise ValueError("products_query_budget_exhausted")
                if len(rows) > 100000 or getattr(result, "total_rows", len(rows)) > 100000:
                    raise ValueError("products_query_truncated")
                self._rows = rows
            except Exception:
                self._budget.complete = False
                raise
        return self._rows

    def to_dataframe(self):
        import pandas as pd

        return pd.DataFrame([dict(row) for row in self.result()])


class _QueryClient:
    def __init__(self, client, budget):
        self._client, self._budget = client, budget
        self.project, self.location = client.project, client.location

    def query(self, sql, *, job_config=None, **kwargs):
        require_read_only(sql, job_config, "products_query_not_read_only")
        return self._dispatch(sql, job_config=job_config)

    @staticmethod
    def _fingerprint_sql(columns):
        if any(_COLUMN.fullmatch(column) is None for column in columns):
            raise ValueError("product_write_record_invalid")
        struct = ", ".join(f"target.`{column}`" for column in columns)
        return f"FARM_FINGERPRINT(TO_JSON_STRING(STRUCT({struct})))"

    def fingerprinted_rows(self, table, *, trend_date, columns, consumer=None):
        """One day's rows of a table, each with the server's fingerprint of ``columns``."""
        from google.cloud import bigquery

        where = f"WHERE target.{_date_field(table)} = @trend_date"
        parameters = [bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date)]
        if consumer is not None:
            where += "\n  AND target.consumer IN UNNEST(@consumers)"
            parameters.append(bigquery.ArrayQueryParameter("consumers", "STRING", [consumer]))
        job = self.query(
            f"SELECT {self._fingerprint_sql(columns)} AS _fingerprint, target.*\n"
            f"FROM `{self.project}.trends_v2_staging.{table}` AS target\n{where}",
            job_config=bigquery.QueryJobConfig(query_parameters=parameters),
        )
        return [dict(row) for row in job.result()]

    def delete_recorded(self, table, *, trend_date, keys, columns, fingerprints, expected):
        """Delete, in one transaction, exactly ``expected`` rows of one day.

        A row is deleted only when its whole natural key is one of ``keys`` and the
        server's fingerprint of its written columns is one of ``fingerprints``, the
        fingerprints read from the rows just checked. The assertion rolls the delete back
        when any other row matches, such as one raced in after the check.
        """
        from google.cloud import bigquery

        key_columns = WRITE_KEYS[table]
        matched = " AND ".join(f"k.{column} = target.{column}" for column in key_columns)
        values = [
            bigquery.StructQueryParameter(
                None,
                *(
                    bigquery.ScalarQueryParameter(column, "STRING", value)
                    for column, value in zip(key_columns, key, strict=True)
                ),
            )
            for key in sorted(keys)
        ]
        job = self._dispatch(
            "BEGIN TRANSACTION;\n"
            f"DELETE FROM `{self.project}.trends_v2_staging.{table}` AS target\n"
            f"WHERE target.{_date_field(table)} = @trend_date\n"
            f"  AND EXISTS (SELECT 1 FROM UNNEST(@keys) AS k WHERE {matched})\n"
            f"  AND {self._fingerprint_sql(columns)} IN UNNEST(@fingerprints);\n"
            "ASSERT @@row_count = @expected AS 'products_delete_count_differs';\n"
            "COMMIT TRANSACTION;",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date),
                    bigquery.ArrayQueryParameter("keys", "STRUCT", values),
                    bigquery.ArrayQueryParameter("fingerprints", "INT64", sorted(fingerprints)),
                    bigquery.ScalarQueryParameter("expected", "INT64", expected),
                ]
            ),
        )
        job.result()

    def _dispatch(self, sql, *, job_config=None):
        from google.cloud import bigquery

        self._budget.require_complete()
        remaining = self._budget.limits["max_bytes_billed"] - self._budget.total_bytes_billed
        if remaining <= 0:
            self._budget.complete = False
            raise ValueError("products_query_budget_exhausted")
        config = deepcopy(job_config) if job_config is not None else bigquery.QueryJobConfig()
        config.maximum_bytes_billed = remaining
        self._budget.query_count += 1
        try:
            job = self._client.query(
                sql, job_config=config, retry=None, job_retry=None, location=self.location
            )
            return _QueryJob(job, self._budget)
        except Exception:
            self._budget.complete = False
            raise


class BoundedProductIO:
    """The bounded boundary one product attempt reads and writes through.

    Every non empty write is recorded, before it is dispatched, in the operation's create
    only write ledger: the natural keys and content digests of the rows. A later attempt of
    the same operation deletes only rows it finds under those recorded keys with recorded
    content, and refuses when a recorded key holds anything else, so rows other origins
    wrote into the shared staging day are never deleted. The product tables are replaced up
    front, before any stage reads them, so no stage skips work on an earlier attempt's rows.
    The usage ledger is replaced one consumer at a time, only when that consumer's usage
    is being rewritten, so a refused retry never erases spend it did not record again.
    """

    def __init__(self, boundary, *, admission, budget, trend_date, ledger, operation_id):
        from .persistence import validate_real_client

        if boundary.dataset != "trends_v2_staging":
            raise ValueError("products_target_invalid")
        validate_real_client(
            boundary.client,
            admission.manifest.project,
            writer_identity=admission.manifest.service_identity,
        )
        self._boundary, self._budget, self._day = boundary, budget, trend_date
        self.client = _QueryClient(boundary.client, budget)
        self.dataset = boundary.dataset
        self._expected = {}
        self._ledger, self._operation_id = ledger, operation_id
        self._sequence = {}
        self._usage_replaced = set()
        self._written_keys = {}

    def query_runner(self, sql, *, params):
        from google.cloud import bigquery

        values = [
            bigquery.ScalarQueryParameter(
                name, "DATE" if isinstance(value, date) else "INT64", value
            )
            for name, value in params.items()
        ]
        return self.client.query(
            sql.format(project=self.client.project, dataset=self.dataset),
            job_config=bigquery.QueryJobConfig(query_parameters=values),
        ).to_dataframe()

    def read_rows(self, table, **kwargs):
        from google.cloud import bigquery

        allowed = {
            "trend_scores",
            "seed_graph",
            "seed_candidates",
            "trend_analysis",
            "daily_summary",
            "seed_insights",
            "creator_briefs",
            "pan_african_stories",
            "gemini_usage",
            "v_seed_first_seen",
            "v_desk_dynamic_signals_v2",
        }
        if table not in allowed:
            raise ValueError("products_read_target_invalid")
        date_field = _date_field(table)
        parameters = []
        if table == "v_seed_first_seen":
            clause = "first_seen_event_date <= @trend_date"
            parameters.append(bigquery.ScalarQueryParameter("trend_date", "DATE", self._day))
        elif table == "v_desk_dynamic_signals_v2":
            clause = "run_id = @run_id"
            parameters.append(bigquery.ScalarQueryParameter("run_id", "STRING", kwargs["run_id"]))
        else:
            clause = f"{date_field} = @trend_date"
            parameters.append(bigquery.ScalarQueryParameter("trend_date", "DATE", self._day))
        if table == "gemini_usage":
            # The usage ledger is shared; the products own only their own consumers' rows.
            consumers = kwargs.get("consumers", USAGE_CONSUMERS)
            if not set(consumers) <= set(USAGE_CONSUMERS):
                raise ValueError("products_read_target_invalid")
            clause += " AND consumer IN UNNEST(@consumers)"
            parameters.append(
                bigquery.ArrayQueryParameter("consumers", "STRING", sorted(consumers))
            )
        rows = [
            dict(row)
            for row in self.client.query(
                f"SELECT * FROM `{self.client.project}.{self.dataset}.{table}` WHERE {clause}",
                job_config=bigquery.QueryJobConfig(query_parameters=parameters),
            ).result()
        ]
        if kwargs.get("identifiers"):
            rows = [row for row in rows if row.get("brief_id") in kwargs["identifiers"]]
        return rows

    def sink(self, table):
        allowed = {
            "trend_scores",
            "seed_graph",
            "seed_candidates",
            "trend_analysis",
            "daily_summary",
            "seed_insights",
            "creator_briefs",
            "pan_african_stories",
            "gemini_usage",
        }
        if table not in allowed:
            raise ValueError("products_write_target_invalid")

        def write(rows):
            self._budget.require_complete()
            date_field = _date_field(table)
            for row in rows:
                row_day = row.get(date_field)
                if isinstance(row_day, str):
                    try:
                        row_day = date.fromisoformat(row_day)
                    except ValueError:
                        row_day = None
                market = row.get("market")
                markets = row.get("markets", [])
                if (
                    row_day != self._day
                    or (market is not None and market not in {"za", "ng", "ke"})
                    or not isinstance(markets, list | tuple)
                    or not set(markets) <= {"za", "ng", "ke"}
                ):
                    self._budget.complete = False
                    raise ValueError("products_row_scope_invalid")
            expected = canonical_rows(rows)
            prior = self._expected.get(table, [])
            if expected:
                columns = set((prior or expected)[0])
                if any(set(row) != columns for row in expected):
                    raise ValueError("products_write_schema_differs:" + table)
            if self._budget.rows_written + len(rows) > self._budget.limits["max_rows_written"]:
                self._budget.complete = False
                raise ValueError("products_write_budget_exhausted")
            if not rows:
                return 0
            keys = self._keys(table, rows)
            if table == "gemini_usage":
                consumers = {key[0] for key in keys}
                if not consumers <= set(USAGE_CONSUMERS):
                    self._budget.complete = False
                    raise ValueError("products_row_scope_invalid")
                for consumer in sorted(consumers - self._usage_replaced):
                    self._replace_recorded(table, consumer=consumer)
                    self._usage_replaced.add(consumer)
            self._record(table, expected, keys)
            self._expected.setdefault(table, []).extend(expected)
            self._budget.rows_written += len(rows)
            self._budget.storage_write_count += 1
            self._budget.storage_write_bytes += len(canonical_bytes(expected))
            try:
                count = self._boundary.sink(table)(rows)
                if type(count) is not int or count != len(rows):
                    raise ValueError("products_write_incomplete")
                self.outcome(table)
                return count
            except Exception:
                self._budget.complete = False
                raise

        return write

    def _keys(self, table, rows):
        # Keys are unique across an attempt's writes to a table, so a recorded key holding
        # more than one row on replacement is another writer's row.
        written = self._written_keys.setdefault(table, set())
        keys = []
        for row in rows:
            key = tuple(row.get(column) for column in WRITE_KEYS[table])
            if any(not isinstance(value, str) or not value for value in key):
                self._budget.complete = False
                raise ValueError("products_write_key_invalid:" + table)
            if key in written:
                self._budget.complete = False
                raise ValueError("products_write_key_duplicate:" + table)
            written.add(key)
            keys.append(key)
        return keys

    def _record(self, table, expected, keys):
        """Record the rows about to be written before the write is dispatched."""
        if table not in self._sequence:
            self._sequence[table] = len(self._ledger.prior_writes(self._operation_id, table))
        self._sequence[table] += 1
        columns = sorted(expected[0])
        try:
            self._ledger.record_write(
                self._operation_id,
                table,
                self._sequence[table],
                columns=columns,
                keys=[list(key) for key in keys],
                digests=sorted({_row_digest(row, columns) for row in expected}),
            )
        except Exception:
            self._budget.complete = False
            raise

    def _replace_recorded(self, table, *, consumer=None):
        """Delete the rows earlier attempts of this operation recorded writing to ``table``."""
        records = self._ledger.prior_writes(self._operation_id, table)
        if not records:
            return
        column_sets = {tuple(record["columns"]) for record in records}
        if len(column_sets) != 1:
            raise ValueError("product_write_record_invalid")
        (columns,) = column_sets
        keys = {
            tuple(key)
            for record in records
            for key in record["keys"]
            if consumer is None or key[0] == consumer
        }
        if not keys:
            return
        accepted = {digest for record in records for digest in record["digests"]}
        found, fingerprints = Counter(), set()
        for row in self.client.fingerprinted_rows(
            table, trend_date=self._day, columns=columns, consumer=consumer
        ):
            key = tuple(row.get(column) for column in WRITE_KEYS[table])
            if key not in keys:
                continue
            found[key] += 1
            # A recorded key holding content this operation never wrote, or a second row,
            # is another writer's.
            if found[key] > 1 or _row_digest(row, columns) not in accepted:
                raise ValueError("products_partition_foreign:" + table)
            fingerprint = row["_fingerprint"]
            if type(fingerprint) is not int:
                raise ValueError("products_partition_foreign:" + table)
            fingerprints.add(fingerprint)
        if found:
            try:
                self.client.delete_recorded(
                    table,
                    trend_date=self._day,
                    keys=set(found),
                    columns=columns,
                    fingerprints=fingerprints,
                    expected=sum(found.values()),
                )
            except Exception:
                self._budget.complete = False
                raise

    def replace_prior_writes(self, tables):
        """Replace, before any stage runs, what earlier attempts wrote to the product tables."""
        self._budget.require_complete()
        for table in tables:
            if table == "gemini_usage" or table not in WRITE_KEYS:
                raise ValueError("products_write_target_invalid")
            self._replace_recorded(table)

    def outcome(self, table, **kwargs):
        self._budget.require_complete()
        if table == "v_seed_first_seen":
            return self._first_seen()
        if table.startswith("v_"):
            raise ValueError("products_read_target_invalid")
        expected = self._expected.get(table, [])
        if table == "gemini_usage":
            kwargs = {"consumers": tuple(self._usage_replaced)}
        rows = canonical_rows(self.read_rows(table, **kwargs))
        if expected:
            columns = set(expected[0])
            if any(not columns <= set(row) for row in rows):
                self._budget.complete = False
                raise ValueError("products_readback_differs:" + table)
            rows = canonical_rows([{column: row[column] for column in columns} for row in rows])
        # What was written and what was read are digested from their own sources.
        written = canonical_digest(sorted(expected, key=canonical_bytes))
        readback = canonical_digest(rows)
        if written != readback:
            self._budget.complete = False
            raise ValueError("products_readback_differs:" + table)
        return {
            "state": "completed" if rows else "empty",
            "row_count": len(rows),
            "output_digest": written,
            "readback_digest": readback,
        }

    def _first_seen(self):
        """The first seen view must show every term this attempt wrote to the seed graph."""
        required = sorted(
            {(row["market"], row["term"]) for row in self._expected.get("seed_graph", [])}
        )
        rows = canonical_rows(self.read_rows("v_seed_first_seen"))
        shown = {(row["market"], row["term"]) for row in rows}
        found = [key for key in required if key in shown]
        if found != required:
            self._budget.complete = False
            raise ValueError("products_first_seen_incomplete")
        return {
            "state": "completed" if rows else "empty",
            "row_count": len(rows),
            "output_digest": canonical_digest([list(key) for key in required]),
            "readback_digest": canonical_digest([list(key) for key in found]),
        }

    def view_readback(self, table, *, run_id):
        """What one run's rows read as through a release gated view."""
        self._budget.require_complete()
        if table != "v_desk_dynamic_signals_v2":
            raise ValueError("products_read_target_invalid")
        rows = canonical_rows(self.read_rows(table, run_id=run_id))
        return {"row_count": len(rows), "readback_digest": canonical_digest(rows)}

    def unfunded(self, table):
        """The outcome of a model backed product the admitted origin funds no call for."""
        if table not in MODEL_PRODUCTS or self._budget.models_funded:
            raise ValueError("products_unfunded_invalid")
        empty = canonical_digest([])
        return {
            "state": "unfunded",
            "row_count": 0,
            "output_digest": empty,
            "readback_digest": empty,
        }
