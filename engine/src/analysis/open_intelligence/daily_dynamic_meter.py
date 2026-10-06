"""Shared accounting for the dynamic composer's warehouse effects."""

from copy import deepcopy

from .brain_contract import canonical_bytes
from .daily_product_io import canonical_rows, require_read_only

DML_STATEMENTS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})
# A bound on the statements one script may list; a longer listing is not a measurement.
SCRIPT_CHILD_LIMIT = 1000


class _Job:
    def __init__(self, job, meter, *, write_bound=None, load_count=None, reported_rows_field=None):
        self._job, self._meter = job, meter
        self._write_bound, self._load_count = write_bound, load_count
        self._reported_rows_field = reported_rows_field
        self._done = False
        self._result = None

    def __getattr__(self, name):
        if name not in {"errors", "output_rows", "total_bytes_billed", "num_dml_affected_rows"}:
            raise AttributeError(name)
        return getattr(self._job, name)

    def result(self, **kwargs):
        if self._done:
            return self._result
        self._meter._check()
        try:
            options = {**kwargs, "retry": None, "timeout": min(kwargs.get("timeout") or 60, 60)}
            if self._load_count is None:
                options["job_retry"] = None
            result = self._job.result(**options)
            if getattr(self._job, "errors", None):
                raise ValueError("dynamic_job_failed")
            if self._load_count is not None:
                if (
                    type(self._job.output_rows) is not int
                    or self._job.output_rows != self._load_count
                ):
                    raise ValueError("dynamic_load_incomplete")
            else:
                billed = self._job.total_bytes_billed
                if type(billed) is not int or billed < 0:
                    raise ValueError("dynamic_job_usage_unknown")
                self._meter._budget.total_bytes_billed += billed
                if self._meter._budget.total_bytes_billed > self._meter._limits["max_bytes_billed"]:
                    raise ValueError("dynamic_query_budget_exhausted")
                if self._write_bound is not None:
                    affected = self._job.num_dml_affected_rows
                    if self._reported_rows_field is not None:
                        from itertools import islice

                        result = list(islice(result, 2))
                        if len(result) != 1:
                            raise ValueError("dynamic_job_usage_unknown")
                        reported = result[0].get(self._reported_rows_field)
                        # The script's own count is self reported. A script's parent job
                        # carries no DML count, so the server's per statement counts of its
                        # child jobs measure the write, and the two must agree.
                        measured = self._meter._script_dml_rows(self._job)
                        if (affected is not None and affected != measured) or measured != reported:
                            raise ValueError("dynamic_job_usage_unknown")
                        affected = measured
                    if type(affected) is not int or affected < 0:
                        raise ValueError("dynamic_job_usage_unknown")
                    if affected > self._write_bound:
                        raise ValueError("dynamic_write_bound_exceeded")
            self._result, self._done = result, True
            self._meter._pending -= 1
            return result
        except Exception:
            self._meter._budget.complete = False
            raise

    def to_dataframe(self):
        import pandas as pd

        return pd.DataFrame([dict(row) for row in self.result()])


class DynamicMeter:
    def __init__(self, client):
        self._client = client
        self.project = getattr(client, "project", None)
        self.location = getattr(client, "location", None)
        self._credentials = getattr(client, "_credentials", None)
        self._budget, self._limits = None, None
        self._pending = 0
        self._created = set()

    def bind(self, budget):
        if self._budget is not None and self._budget is not budget:
            raise ValueError("dynamic_budget_binding_differs")
        if self._budget is None:
            self._budget, self._limits = budget, dict(budget.limits)
        self._check()

    def _check(self):
        if self._budget is None:
            raise ValueError("dynamic_meter_unbound")
        if dict(self._budget.limits) != self._limits:
            raise ValueError("dynamic_budget_binding_differs")
        self._budget.require_complete()

    def _script_dml_rows(self, job):
        """Sum the server's affected row counts over a finished script's child statements."""
        from itertools import islice

        children = list(
            islice(
                self._client.list_jobs(
                    parent_job=job, max_results=SCRIPT_CHILD_LIMIT + 1, retry=None, timeout=60
                ),
                SCRIPT_CHILD_LIMIT + 1,
            )
        )
        if len(children) > SCRIPT_CHILD_LIMIT:
            raise ValueError("dynamic_job_usage_unknown")
        total = 0
        for child in children:
            kind = getattr(child, "statement_type", None)
            if not isinstance(kind, str) or getattr(child, "errors", None):
                raise ValueError("dynamic_job_usage_unknown")
            if kind in DML_STATEMENTS:
                count = getattr(child, "num_dml_affected_rows", None)
                if type(count) is not int or count < 0:
                    raise ValueError("dynamic_job_usage_unknown")
                total += count
        return total

    def require_complete(self, budget):
        self._check()
        if budget is not self._budget:
            raise ValueError("dynamic_budget_binding_differs")
        if self._pending:
            raise ValueError("dynamic_job_pending")

    def _reserve_rows(self, count):
        self._check()
        if type(count) is not int or count < 0:
            raise ValueError("dynamic_write_bound_invalid")
        if self._budget.rows_written + count > self._limits["max_rows_written"]:
            raise ValueError("dynamic_write_budget_exhausted")
        self._budget.rows_written += count

    def query(self, sql, *, job_config=None, **kwargs):
        self._check()
        require_read_only(sql, job_config, "dynamic_write_reservation_missing")
        return self._query(sql, job_config=job_config, **kwargs)

    def query_write(self, sql, *, row_bound, job_config=None, reported_rows_field=None, **kwargs):
        if reported_rows_field not in (None, "_affected_rows"):
            raise ValueError("dynamic_write_count_field_invalid")
        self._reserve_rows(row_bound)
        return self._query(
            sql,
            job_config=job_config,
            write_bound=row_bound,
            reported_rows_field=reported_rows_field,
            **kwargs,
        )

    def _query(self, sql, *, job_config=None, write_bound=None, reported_rows_field=None, **kwargs):
        from google.cloud import bigquery

        self._check()
        if self._pending:
            raise ValueError("dynamic_job_pending")
        remaining = self._limits["max_bytes_billed"] - self._budget.total_bytes_billed
        if remaining <= 0:
            raise ValueError("dynamic_query_budget_exhausted")
        config = deepcopy(job_config) if job_config is not None else bigquery.QueryJobConfig()
        prior = config.maximum_bytes_billed
        config.maximum_bytes_billed = min(prior, remaining) if prior is not None else remaining
        self._budget.query_count += 1
        try:
            job = self._client.query(
                sql,
                job_config=config,
                **{
                    **kwargs,
                    "retry": None,
                    "job_retry": None,
                    "location": self.location,
                    "timeout": 60,
                },
            )
            self._pending += 1
            return _Job(job, self, write_bound=write_bound, reported_rows_field=reported_rows_field)
        except Exception:
            self._budget.complete = False
            raise

    def _table(self, table):
        name = str(getattr(table, "reference", table))
        if not name.startswith(self.project + ".trends_v2_staging."):
            raise ValueError("dynamic_table_target_invalid")
        return name

    def load_table_from_json(self, rows, table, **kwargs):
        self._check()
        self._table(table)
        rows = list(rows)
        self._reserve_rows(len(rows))
        self._budget.storage_write_count += 1
        self._budget.storage_write_bytes += len(canonical_bytes(canonical_rows(rows)))
        try:
            job = self._client.load_table_from_json(
                rows,
                table,
                **{**kwargs, "num_retries": 0, "location": self.location, "timeout": 60},
            )
            self._pending += 1
            return _Job(job, self, load_count=len(rows))
        except Exception:
            self._budget.complete = False
            raise

    def create_table(self, table, **kwargs):
        self._check()
        name = self._table(table)
        try:
            result = self._client.create_table(table, **{**kwargs, "retry": None, "timeout": 60})
            self._created.add(name)
            return result
        except Exception:
            self._budget.complete = False
            raise

    def get_table(self, table, **kwargs):
        self._check()
        self._table(table)
        try:
            return self._client.get_table(table, **{**kwargs, "retry": None, "timeout": 60})
        except Exception:
            self._budget.complete = False
            raise

    def delete_table(self, table, **kwargs):
        name = self._table(table)
        if name not in self._created:
            raise ValueError("dynamic_cleanup_target_invalid")
        try:
            return self._client.delete_table(table, **{**kwargs, "retry": None, "timeout": 60})
        except Exception:
            self._budget.complete = False
            raise
