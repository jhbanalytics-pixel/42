from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from scripts.staging import copy_open_intelligence_replay_sources as copy

START = date(2026, 8, 21)
END = date(2026, 9, 3)


def test_copy_cli_help_runs_from_repository_root() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "scripts/staging/copy_open_intelligence_replay_sources.py",
            "--help",
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--start-date" in result.stdout


class _Job:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def result(self):
        return self.rows


class _SdkRow:
    def __init__(self, values):
        self.values = dict(values)

    def keys(self):
        return tuple(self.values)

    def __getitem__(self, key):
        return self.values[key]


def test_one_normalizes_real_bigquery_row_protocol() -> None:
    result = copy._one(_Job((_SdkRow({"source_rows": 3, "state": "ready"}),)))

    assert result == {"source_rows": 3, "state": "ready"}


def test_schema_validation_allows_extra_source_columns_and_physical_order() -> None:
    plan = copy.build_plan("staging", START, END)
    actual = [dict(row) for row in plan.expected_schema_rows]
    for row in actual:
        if row["side"] in {"source", "target"}:
            row["ordinal"] = int(row["ordinal"]) + 50
    actual.append(
        {
            "side": "source",
            "table_name": "event_ledger",
            "ordinal": 8,
            "field_name": "state_text",
            "field_type": "STRING",
            "field_mode": "NULLABLE",
        }
    )

    copy._validate_schema_rows(actual, plan.expected_schema_rows)


@pytest.mark.parametrize("mutation", ["missing", "wrong_type", "extra_control"])
def test_schema_validation_rejects_required_and_control_drift(mutation) -> None:
    plan = copy.build_plan("staging", START, END)
    actual = [dict(row) for row in plan.expected_schema_rows]
    if mutation == "missing":
        actual = actual[1:]
    elif mutation == "wrong_type":
        actual[0]["field_type"] = "BYTES"
    else:
        actual.append(
            {
                "side": "control",
                "table_name": "open_intelligence_source_copy_lock_v1",
                "ordinal": 99,
                "field_name": "unexpected",
                "field_type": "STRING",
                "field_mode": "NULLABLE",
            }
        )

    with pytest.raises(copy.CopyRefusal, match="schema_mismatch"):
        copy._validate_schema_rows(actual, plan.expected_schema_rows)


class _FrozenClient:
    def __init__(self, plan):
        self.plan = plan
        self.queries = []

    def query(self, sql, **_kwargs):
        self.queries.append(sql)
        if sql == self.plan.infrastructure_query:
            return _Job(
                (
                    {
                        "infrastructure_ready": 1,
                        "control_table_count": 3,
                        "total_lock_rows": 1,
                        "matching_lock_rows": 1,
                        "lock_version": 0,
                        "lock_contract_version": copy.COPY_CONTRACT_VERSION,
                        "lock_state": "ready",
                    },
                )
            )
        if sql == self.plan.schema_query:
            return _Job(self.plan.expected_schema_rows)
        if sql == self.plan.target_query:
            return _Job({"table_name": name, "target_rows": 0} for name in self.plan.tables)
        for table, query in self.plan.source_queries.items():
            if sql == query:
                item = self.plan.tables[table]
                return _Job(
                    (
                        {
                            "table_name": table,
                            "source_rows": item.source_rows,
                            "active_dates": item.active_dates,
                            "daily_maximum": item.daily_maximum,
                            "natural_keys": item.source_rows,
                            "source_set_digest": item.source_set_digest,
                        },
                    )
                )
        if sql == self.plan.coverage_query:
            item = self.plan.tables["enriched_content"]
            return _Job(
                (
                    {
                        "request_instances": item.source_rows,
                        "matched_once": item.source_rows,
                        "missing": 0,
                        "ambiguous": 0,
                        "coverage_digest": item.coverage_digest,
                        "raw_request_instances": item.source_rows + 1,
                        "raw_matched_once": item.source_rows + 1,
                        "raw_missing": 0,
                        "raw_ambiguous": 0,
                    },
                )
            )
        raise AssertionError("unexpected query")


class _RawCoverageConflictClient(_FrozenClient):
    def query(self, sql, **kwargs):
        if sql == self.plan.coverage_query:
            item = self.plan.tables["enriched_content"]
            return _Job(
                (
                    {
                        "request_instances": item.source_rows,
                        "matched_once": item.source_rows,
                        "missing": 0,
                        "ambiguous": 0,
                        "coverage_digest": item.coverage_digest,
                        "raw_request_instances": item.source_rows + 1,
                        "raw_matched_once": item.source_rows,
                        "raw_missing": 1,
                        "raw_ambiguous": 0,
                    },
                )
            )
        return super().query(sql, **kwargs)


def test_plan_freezes_exact_tables_counts_digests_and_candidate_safety_flags():
    plan = copy.build_plan("staging", START, END)

    assert (plan.project, plan.source_dataset, plan.target_dataset, plan.location) == (
        "ogilvy-trends-v2",
        "trends_v2_dev",
        "trends_v2_staging",
        "US",
    )
    assert tuple(plan.tables) == (
        "event_ledger",
        "seed_graph",
        "seed_candidates",
        "enriched_content",
    )
    assert tuple(item.source_rows for item in plan.tables.values()) == (5158, 210000, 0, 80845)
    assert plan.tables["event_ledger"].source_set_digest.startswith("c9dfd8dd")
    assert plan.tables["seed_candidates"].source_set_digest == copy.ZERO_SOURCE_SET_DIGEST
    assert "safety_flags" in plan.tables["seed_candidates"].columns
    assert "safety_flags" not in copy.SEED_CANDIDATE_COLUMNS


def test_frozen_fixture_matches_plan_exactly():
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "open_intelligence"
            / "replay_source_copy_v1.json"
        ).read_text(encoding="utf-8")
    )
    plan = copy.build_plan("staging", START, END)

    assert fixture["copy_run_id"] == plan.copy_run_id
    assert fixture["copy_contract_version"] == copy.COPY_CONTRACT_VERSION
    assert fixture["tables"] == {
        name: [item.source_rows, item.source_set_digest] for name, item in plan.tables.items()
    }
    assert fixture["coverage_digest"] == plan.tables["enriched_content"].coverage_digest


def test_chunked_digest_matches_literal_vector_and_zero_contract():
    pairs = (
        ("0" * 64, "1" * 64),
        ("2" * 64, "3" * 64),
    )
    assert (
        copy.chunked_digest(pairs)
        == "a25fc3cfe5a2792bed87ffbcf002535b18c9c8d2b3c59bfbcb32b0c7302a099d"
    )
    assert (
        copy.chunked_digest(())
        == "f69dade361b3d2c792f04091254f78752440ecde380c1caf69b5e01c0d6db0ee"
    )


def test_source_queries_are_select_only_explicit_and_replay_date_bounded():
    plan = copy.build_plan("staging", START, END)

    for table, sql in plan.source_queries.items():
        assert "SELECT *" not in sql
        assert "trends_v2_dev" in sql
        assert "trends_v2_staging" not in sql
        assert not any(token in sql.upper() for token in (" INSERT ", " UPDATE ", " DELETE "))
        for field in plan.tables[table].columns:
            assert field in sql
    coverage = plan.coverage_query
    enriched = plan.source_queries["enriched_content"]
    assert "source_row.market" in enriched
    assert "source_row.id" in enriched
    assert "requested.market=source.market" not in enriched
    assert "replay_date" in coverage
    assert "DATE_SUB(replay_date, INTERVAL 6 DAY)" in coverage
    assert "raw_content" not in coverage
    assert "trends_v2_staging" in plan.target_query
    assert "trends_v2_dev" not in plan.target_query


def test_dry_run_rejects_raw_request_multiplicity_before_distinct_digest() -> None:
    plan = copy.build_plan("staging", START, END)

    with pytest.raises(copy.CopyRefusal, match="coverage_incomplete"):
        copy.execute_plan(plan, apply=False, client=_RawCoverageConflictClient(plan))


def test_apply_sql_is_lock_first_insert_only_and_owns_manifest_and_headers():
    plan = copy.build_plan("staging", START, END)
    sql = plan.apply_sql

    assert sql.index("BEGIN TRANSACTION") < sql.index(
        "UPDATE `ogilvy-trends-v2.trends_v2_staging.open_intelligence_source_copy_lock_v1`"
    )
    assert "CAST(src.`source` AS STRING) AS source" in sql
    assert " source.candidate_id" not in sql
    assert "FROM (SELECT candidate_id" in sql
    assert ") src;" in sql
    assert "raw_matched_once=raw_request_instances" in sql
    assert "raw_missing=0" in sql
    assert sql.count("CAST(NULL AS STRING) AS coverage_digest") == 3
    assert sql.index("copy_lock_invalid") < sql.index(
        "INSERT INTO `ogilvy-trends-v2.trends_v2_staging.event_ledger`"
    )
    assert (
        "INSERT INTO `ogilvy-trends-v2.trends_v2_staging.open_intelligence_source_copy_manifest_v1`"
        in sql
    )
    assert (
        "INSERT INTO `ogilvy-trends-v2.trends_v2_staging.open_intelligence_source_copy_receipts_v1`"
        in sql
    )
    assert "safety_flags" in sql
    assert "trends_v2`" not in sql
    assert "TRUNCATE" not in sql
    assert "MERGE" not in sql
    assert sql.count("UPDATE ") == 1
    assert "DELETE " not in sql


def test_rollback_plan_is_not_a_command_and_mutates_lock_before_owned_deletes():
    plan = copy.build_plan("staging", START, END)
    sql = plan.rollback_sql

    assert "--rollback" not in copy.parser().format_help()
    assert sql.index(
        "UPDATE `ogilvy-trends-v2.trends_v2_staging.open_intelligence_source_copy_lock_v1`"
    ) < sql.index("DELETE FROM `ogilvy-trends-v2.trends_v2_staging.event_ledger`")
    assert "source_content_sha256" in sql
    assert "open_intelligence_source_copy_manifest_v1" in sql
    assert "open_intelligence_source_copy_receipts_v1`" not in sql.split("DELETE FROM")[-1]
    assert "ogilvy-trends-v2.trends_v2_dev" not in sql


def test_dry_run_uses_selects_only_and_returns_sanitized_frozen_receipt():
    plan = copy.build_plan("staging", START, END)
    client = _FrozenClient(plan)

    result = copy.execute_plan(plan, apply=False, client=client)

    assert result["mode"] == "dry-run"
    assert result["total_source_rows"] == 296003
    assert result["total_inserted"] == 0
    assert result["manifest_inserted"] == 0
    assert result["receipt_headers_inserted"] == 0
    assert result["complete_partitions"] is True
    assert all(query.lstrip().upper().startswith("SELECT") for query in client.queries)
    assert not any(
        value in repr(result)
        for value in ("fixture_entity_value", "fixture_sample_value", "fixture_text_value")
    )


@pytest.mark.parametrize(
    "fragment",
    [
        "copy_lock_invalid",
        "safety_flags",
        "open_intelligence_source_copy_manifest_v1",
        "open_intelligence_source_copy_receipts_v1",
        "source_content_sha256",
        "manifest_duplicate",
        "receipt_duplicate",
    ],
)
def test_sql_mutations_fail_closed(fragment):
    plan = copy.build_plan("staging", START, END)
    mutated = plan.apply_sql.replace(fragment, "", 1)
    assert mutated != plan.apply_sql
    with pytest.raises(copy.CopyRefusal, match="sql_invalid"):
        copy.validate_apply_sql(mutated)


@pytest.mark.parametrize(
    ("target", "start", "end"),
    [
        ("production", START, END),
        ("staging", date(2026, 8, 13), END),
        ("staging", START, date(2026, 8, 28)),
    ],
)
def test_plan_refuses_target_or_window_drift(target, start, end):
    with pytest.raises(copy.CopyRefusal):
        copy.build_plan(target, start, end)


def test_cli_exposes_only_copy_dry_run_and_apply(capsys):
    plan = copy.build_plan("staging", START, END)
    assert (
        copy.main(
            ["--target", "staging", "--start-date", "2026-08-21", "--end-date", "2026-09-03"],
            client=_FrozenClient(plan),
        )
        == 0
    )
    assert '"mode":"dry-run"' in capsys.readouterr().out
    assert "rollback" not in copy.parser().format_help().lower()
