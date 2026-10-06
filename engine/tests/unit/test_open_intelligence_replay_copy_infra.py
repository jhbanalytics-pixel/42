from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from scripts.migrations import create_open_intelligence_replay_copy_infra as infra

COPY_RUN_ID = "dynamic_replay_source_copy_20260821_20260903_v1"


class _Job:
    def __init__(self, rows=()):
        self._rows = tuple(rows)

    def result(self):
        return self._rows


class _Client:
    def __init__(self, plan, *, total_lock_rows=1, matching_lock_rows=1):
        self.plan = plan
        self.queries = []
        self.total_lock_rows = total_lock_rows
        self.matching_lock_rows = matching_lock_rows

    def query(self, sql, **kwargs):
        self.queries.append((sql, kwargs))
        if "total_lock_rows" in sql:
            return _Job(
                (
                    {
                        "total_lock_rows": self.total_lock_rows,
                        "matching_lock_rows": self.matching_lock_rows,
                        "copy_run_id": COPY_RUN_ID,
                        "copy_contract_version": infra.COPY_CONTRACT_VERSION,
                        "lock_version": 0,
                        "state": "ready",
                    },
                )
            )
        return _Job()

    def get_table(self, table_ref):
        statement = next(item for item in self.plan.statements if item.table_ref == table_ref)
        return SimpleNamespace(
            schema=tuple(
                SimpleNamespace(
                    name=name,
                    field_type={"INT64": "INTEGER", "FLOAT64": "FLOAT", "BOOL": "BOOLEAN"}.get(
                        field_type, field_type
                    ),
                    mode=mode,
                )
                for name, field_type, mode in statement.schema
            ),
            time_partitioning=(
                SimpleNamespace(field=statement.partition_field)
                if statement.partition_field
                else None
            ),
            clustering_fields=list(statement.clustering_fields),
            description=statement.description,
        )


def test_plan_has_exact_three_tables_and_atomic_lock_ctas():
    plan = infra.build_plan("staging", COPY_RUN_ID)

    assert (plan.project, plan.dataset, plan.location, plan.service_account) == (
        "ogilvy-trends-v2",
        "trends_v2_staging",
        "US",
        "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    )
    assert tuple(item.name for item in plan.statements) == (
        "open_intelligence_source_copy_lock_v1",
        "open_intelligence_source_copy_manifest_v1",
        "open_intelligence_source_copy_receipts_v1",
    )
    lock_sql = plan.statements[0].sql
    assert "CREATE TABLE IF NOT EXISTS" in lock_sql
    assert "AS SELECT" in lock_sql
    assert COPY_RUN_ID in lock_sql
    assert "INSERT" not in lock_sql
    assert "DELETE" not in lock_sql


def test_manifest_and_receipt_schemas_are_exact():
    plan = infra.build_plan("staging", COPY_RUN_ID)

    assert plan.statements[1].schema == infra.MANIFEST_SCHEMA
    assert plan.statements[1].partition_field == "window_end"
    assert plan.statements[1].clustering_fields == ("copy_run_id", "target_table")
    assert plan.statements[2].schema == infra.RECEIPT_SCHEMA
    assert plan.statements[2].partition_field == "window_end"
    assert plan.statements[2].clustering_fields == ("copy_run_id", "source_table")


def test_dry_run_validates_every_ddl_without_applying_it():
    plan = infra.build_plan("staging", COPY_RUN_ID)
    client = _Client(plan)

    result = infra.execute_plan(plan, apply=False, client=client)

    assert result == {
        "mode": "dry-run",
        "target": "staging",
        "copy_run_id": COPY_RUN_ID,
        "statement_count": 3,
        "write_state": "not_started",
    }
    assert len(client.queries) == 3
    assert all(kwargs["job_config"].dry_run is True for _sql, kwargs in client.queries)
    assert all(
        isinstance(kwargs["job_config"], infra.bigquery.QueryJobConfig)
        for _sql, kwargs in client.queries
    )


def test_apply_executes_exact_ddl_then_reads_schema_and_singleton():
    plan = infra.build_plan("staging", COPY_RUN_ID)
    client = _Client(plan)

    result = infra.execute_plan(plan, apply=True, client=client)

    assert result["mode"] == "apply"
    assert result["tables_created_or_matched"] == 3
    assert result["lock_rows"] == 1
    assert [sql for sql, _kwargs in client.queries[:3]] == [item.sql for item in plan.statements]
    assert "total_lock_rows" in client.queries[3][0]
    parameters = client.queries[3][1]["job_config"].query_parameters
    assert [(item.name, item.value) for item in parameters] == [("copy_run_id", COPY_RUN_ID)]


def test_apply_readback_rejects_one_approved_and_one_foreign_lock_row():
    plan = infra.build_plan("staging", COPY_RUN_ID)
    client = _Client(plan, total_lock_rows=2, matching_lock_rows=1)

    with pytest.raises(infra.InfraRefusal, match="lock_conflict"):
        infra.execute_plan(plan, apply=True, client=client)

    readback_sql = client.queries[3][0]
    assert "total_lock_rows" in readback_sql
    assert "matching_lock_rows" in readback_sql


@pytest.mark.parametrize("target", ["production", "qa", "staging_qa", ""])
def test_plan_refuses_every_nonstaging_target(target):
    with pytest.raises(infra.InfraRefusal, match="target_invalid"):
        infra.build_plan(target, COPY_RUN_ID)


def test_plan_refuses_copy_run_drift_and_mutated_sql():
    with pytest.raises(infra.InfraRefusal, match="copy_run_id_invalid"):
        infra.build_plan("staging", "other_run")

    plan = infra.build_plan("staging", COPY_RUN_ID)
    changed = replace(plan.statements[0], sql=plan.statements[0].sql.replace("ready", "active"))
    with pytest.raises(infra.InfraRefusal, match="plan_invalid"):
        infra.execute_plan(replace(plan, statements=(changed, *plan.statements[1:])), apply=False)


def test_cli_contract_has_only_the_four_approved_command_shapes(capsys):
    plan = infra.build_plan("staging", COPY_RUN_ID)
    assert (
        infra.main(
            ["--target", "staging", "--copy-run-id", COPY_RUN_ID],
            client=_Client(plan),
        )
        == 0
    )
    dry = capsys.readouterr().out
    assert '"mode":"dry-run"' in dry

    with pytest.raises(SystemExit):
        infra.main(["--target", "qa", "--copy-run-id", COPY_RUN_ID])
