from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date

import pytest
from scripts.staging import copy_open_intelligence_replay_sources as copy

START = date(2026, 8, 21)
END = date(2026, 9, 3)


class Job:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def result(self):
        return self.rows


@dataclass
class CopyState:
    control_tables: int = 3
    total_lock_rows: int = 1
    matching_lock_rows: int = 1
    lock_version: int = 0
    lock_contract_version: str = copy.COPY_CONTRACT_VERSION
    lock_state: str = "ready"
    target_rows: dict[str, int] = field(default_factory=dict)
    manifest_rows: dict[str, int] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    tampered_table: str | None = None
    unowned_table: str | None = None
    orphan_table: str | None = None
    duplicate_manifest_table: str | None = None
    duplicate_header_table: str | None = None


class StatefulClient:
    def __init__(self, plan, state=None):
        self.plan = plan
        self.state = state or CopyState()
        self.queries = []

    def query(self, sql, **_kwargs):
        self.queries.append(sql)
        if sql == self.plan.infrastructure_query:
            return Job(
                (
                    {
                        "infrastructure_ready": 1,
                        "control_table_count": self.state.control_tables,
                        "total_lock_rows": self.state.total_lock_rows,
                        "matching_lock_rows": self.state.matching_lock_rows,
                        "lock_version": self.state.lock_version,
                        "lock_contract_version": self.state.lock_contract_version,
                        "lock_state": self.state.lock_state,
                    },
                )
            )
        if sql == self.plan.schema_query:
            return Job(self.plan.expected_schema_rows)
        if sql == self.plan.target_query:
            return Job(
                {
                    "table_name": name,
                    "target_rows": self.state.target_rows.get(name, 0),
                }
                for name in self.plan.tables
            )
        for name, query in self.plan.source_queries.items():
            if sql == query:
                item = self.plan.tables[name]
                return Job(
                    (
                        {
                            "table_name": name,
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
            return Job(
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
        if sql == self.plan.apply_sql:
            return Job((self._apply(),))
        if sql == self.plan.rollback_sql:
            return Job((self._rollback(),))
        raise AssertionError("unexpected query")

    def _apply(self):
        snapshot = deepcopy(self.state)
        before = self.state.lock_version
        self.state.lock_version += 1
        inserted = unchanged = manifest_inserted = manifest_unchanged = conflicts = 0
        error_code = None
        for name, item in self.plan.tables.items():
            target = self.state.target_rows.get(name, 0)
            manifest = self.state.manifest_rows.get(name, 0)
            if self.state.duplicate_manifest_table == name:
                conflicts += 1
                error_code = "manifest_conflict"
                continue
            if (
                self.state.tampered_table == name
                or self.state.unowned_table == name
                or self.state.orphan_table == name
                or target not in (0, item.source_rows)
                or manifest not in (0, item.source_rows)
                or (target == 0) != (manifest == 0)
            ):
                conflicts += 1
                error_code = "target_content_conflict"
                continue
            if target == 0:
                self.state.target_rows[name] = item.source_rows
                self.state.manifest_rows[name] = item.source_rows
                inserted += item.source_rows
                manifest_inserted += item.source_rows
            else:
                unchanged += item.source_rows
                manifest_unchanged += item.source_rows
        headers_inserted = headers_unchanged = 0
        if conflicts == 0:
            for name, item in self.plan.tables.items():
                if self.state.duplicate_header_table == name:
                    conflicts += 1
                    error_code = "receipt_conflict"
                    continue
                existing = self.state.headers.get(name)
                if existing is None:
                    self.state.headers[name] = item.source_set_digest
                    headers_inserted += 1
                elif existing == item.source_set_digest:
                    headers_unchanged += 1
                else:
                    conflicts += 1
                    error_code = "receipt_conflict"
        result = {
            "inserted": inserted,
            "unchanged": unchanged,
            "conflicts": conflicts,
            "manifest_inserted": manifest_inserted,
            "manifest_unchanged": manifest_unchanged,
            "receipt_headers_inserted": headers_inserted,
            "receipt_headers_unchanged": headers_unchanged,
            "lock_version_before": before,
            "lock_version_after": self.state.lock_version,
            "error_code": error_code if conflicts else None,
        }
        if conflicts:
            self.state.__dict__.update(snapshot.__dict__)
        return result

    def _rollback(self):
        snapshot = deepcopy(self.state)
        before = self.state.lock_version
        self.state.lock_version += 1
        deleted = manifest_deleted = conflicts = 0
        for name, item in self.plan.tables.items():
            target = self.state.target_rows.get(name, 0)
            manifest = self.state.manifest_rows.get(name, 0)
            if (
                self.state.tampered_table == name
                or target != item.source_rows
                or manifest != item.source_rows
            ):
                conflicts += 1
                continue
            deleted += target
            manifest_deleted += manifest
        if conflicts == 0:
            self.state.target_rows.clear()
            self.state.manifest_rows.clear()
        result = {
            "deleted": deleted,
            "manifest_deleted": manifest_deleted,
            "conflicts": conflicts,
            "lock_version_before": before,
            "lock_version_after": self.state.lock_version,
            "error_code": "rollback_conflict" if conflicts else None,
        }
        if conflicts:
            self.state.__dict__.update(snapshot.__dict__)
        return result


def plan():
    return copy.build_plan("staging", START, END)


def test_infrastructure_requires_three_tables_and_one_bound_lock_row():
    value = plan()
    with pytest.raises(copy.CopyRefusal, match="infrastructure_missing"):
        copy.execute_plan(
            value, apply=False, client=StatefulClient(value, CopyState(control_tables=2))
        )
    with pytest.raises(copy.CopyRefusal, match="lock_missing"):
        copy.execute_plan(
            value,
            apply=False,
            client=StatefulClient(value, CopyState(matching_lock_rows=0)),
        )
    with pytest.raises(copy.CopyRefusal, match="lock_conflict"):
        copy.execute_plan(
            value,
            apply=False,
            client=StatefulClient(
                value,
                CopyState(total_lock_rows=2, matching_lock_rows=1),
            ),
        )
    for state in (
        CopyState(lock_contract_version="other"),
        CopyState(lock_state="disabled"),
    ):
        with pytest.raises(copy.CopyRefusal, match="lock_conflict"):
            copy.execute_plan(value, apply=False, client=StatefulClient(value, state))


def test_coverage_query_preserves_request_multiplicity_and_derives_digest():
    sql = plan().coverage_query
    assert "UNION ALL" in sql
    assert "SELECT DISTINCT" not in sql
    assert "raw_requests" in sql
    assert "raw_matched" in sql
    assert "GROUP BY replay_date,market,sample_id" in sql
    assert "raw_request_instances" in sql
    assert "coverage_set_v1_chunk1000" in sql
    assert copy.COVERAGE_DIGEST not in sql
    assert "coverage_incomplete" in plan().apply_sql
    assert "coverage_set_v1_chunk1000" in plan().apply_sql


def test_schema_and_filter_digests_are_derived_not_trusted():
    value = plan()
    assert tuple(copy.filter_digest(item) for item in value.tables.values()) == tuple(
        item.filter_digest for item in value.tables.values()
    )
    assert tuple(copy.schema_digest(item) for item in value.tables.values()) == tuple(
        item.schema_digest for item in value.tables.values()
    )
    client = StatefulClient(value)
    copy.execute_plan(value, apply=False, client=client)
    assert value.schema_query in client.queries


def test_first_apply_and_unchanged_rerun_are_derived_from_state():
    value = plan()
    client = StatefulClient(value)
    first = copy.execute_plan(value, apply=True, client=client)
    second = copy.execute_plan(value, apply=True, client=client)

    assert (first["total_inserted"], first["total_unchanged"]) == (296003, 0)
    assert (first["receipt_headers_inserted"], first["receipt_headers_unchanged"]) == (4, 0)
    assert (first["lock_version_before"], first["lock_version_after"]) == (0, 1)
    assert (second["total_inserted"], second["total_unchanged"]) == (0, 296003)
    assert (second["receipt_headers_inserted"], second["receipt_headers_unchanged"]) == (0, 4)
    assert (second["lock_version_before"], second["lock_version_after"]) == (1, 2)


@pytest.mark.parametrize("fault", ["tampered_table", "unowned_table", "orphan_table"])
def test_apply_refuses_tamper_unowned_and_orphan_states(fault):
    value = plan()
    state = CopyState(
        target_rows={name: item.source_rows for name, item in value.tables.items()},
        manifest_rows={name: item.source_rows for name, item in value.tables.items()},
        headers={name: item.source_set_digest for name, item in value.tables.items()},
    )
    setattr(state, fault, "event_ledger")
    with pytest.raises(copy.CopyRefusal, match="target_content_conflict"):
        copy.execute_plan(value, apply=True, client=StatefulClient(value, state))


def test_first_apply_allows_absent_headers_but_rejects_different_header():
    value = plan()
    first = copy.execute_plan(value, apply=True, client=StatefulClient(value))
    assert first["receipt_headers_inserted"] == 4

    state = CopyState(headers={"event_ledger": "different"})
    with pytest.raises(copy.CopyRefusal, match="receipt_conflict"):
        copy.execute_plan(value, apply=True, client=StatefulClient(value, state))
    assert state.target_rows == {}
    assert state.manifest_rows == {}
    assert state.lock_version == 0


@pytest.mark.parametrize(
    ("fault", "error_code"),
    [
        ("duplicate_manifest_table", "manifest_conflict"),
        ("duplicate_header_table", "receipt_conflict"),
    ],
)
def test_identical_duplicate_natural_keys_fail_inside_transaction(fault, error_code):
    value = plan()
    client = StatefulClient(value)
    copy.execute_plan(value, apply=True, client=client)
    setattr(client.state, fault, "event_ledger")
    before = client.state.lock_version

    with pytest.raises(copy.CopyRefusal, match=error_code):
        copy.execute_plan(value, apply=True, client=client)

    assert client.state.lock_version == before


def test_apply_sql_checks_manifest_and_header_total_vs_distinct_counts():
    sql = plan().apply_sql
    assert "manifest_total_count" in sql
    assert "manifest_distinct_count" in sql
    assert "receipt_total_count" in sql
    assert "receipt_distinct_count" in sql
    assert "COUNT(DISTINCT TO_JSON_STRING(STRUCT(copy_run_id,target_table,copy_row_id)))" in sql
    assert "COUNT(DISTINCT TO_JSON_STRING(STRUCT(copy_run_id,source_table)))" in sql


def test_rollback_deletes_exact_owned_state_and_retains_manifests_on_tamper():
    value = plan()
    client = StatefulClient(value)
    copy.execute_plan(value, apply=True, client=client)
    result = copy.execute_rollback_plan(value, client=client)
    assert result == {
        "deleted": 296003,
        "manifest_deleted": 296003,
        "lock_version_before": 1,
        "lock_version_after": 2,
    }
    assert client.state.headers

    tampered = StatefulClient(value)
    copy.execute_plan(value, apply=True, client=tampered)
    tampered.state.tampered_table = "seed_graph"
    before = dict(tampered.state.manifest_rows)
    with pytest.raises(copy.CopyRefusal, match="rollback_conflict"):
        copy.execute_rollback_plan(value, client=tampered)
    assert tampered.state.manifest_rows == before
    assert tampered.state.lock_version == 1


def test_rollback_sql_binds_manifest_ids_and_projection_hashes_and_is_printed():
    value = plan()
    for item in value.tables.values():
        assert item.name in value.rollback_sql
        assert "copy_row_id" in value.rollback_sql
        for column in item.columns:
            assert column in value.rollback_sql
    result = copy.execute_plan(value, apply=False, client=StatefulClient(value))
    assert result["rollback_sql"] == value.rollback_sql
    assert result["rollback_sql_digest"] == copy.sha256_text(value.rollback_sql)


def test_rollback_sql_counts_each_delete_before_asserting_it():
    # @@row_count describes the statement just executed, so the running total must be taken
    # straight after the DELETE. Reading it after the ASSERT left deleted_count at zero on the
    # live 3 September rollback and the transaction refused with rollback_manifest_mismatch.
    value = plan()
    running = 0
    for item in value.tables.values():
        running += item.source_rows
        delete_index = value.rollback_sql.index(
            f"DELETE FROM `{copy.PROJECT}.{copy.TARGET_DATASET}.{item.name}`"
        )
        tail = value.rollback_sql[delete_index:]
        statements = tail.split("\n")[1:3]
        assert statements == [
            "SET deleted_count=deleted_count+@@row_count;",
            f"ASSERT deleted_count={running} AS 'rollback_delete_mismatch';",
        ]
