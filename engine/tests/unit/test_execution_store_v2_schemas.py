"""Contract proof for the additive versioned execution store schemas."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "infra" / "bigquery_schemas"

INHERITED_TABLES = (
    "open_intelligence_execution_approvals",
    "open_intelligence_execution_consumptions",
    "open_intelligence_execution_results",
)
NEW_FIELDS = (
    ("origin_registry_sha256", "STRING", "REQUIRED", "Immutable origin registry digest."),
    (
        "resource_manifest_sha256",
        "STRING",
        "REQUIRED",
        "Immutable resource manifest digest.",
    ),
)
NEW_TABLE_FIELDS = {
    "open_intelligence_execution_origin_registries_v1": (
        ("origin_registry_sha256", "STRING", "REQUIRED", "Immutable origin registry digest."),
        (
            "canonical_registry_json",
            "STRING",
            "REQUIRED",
            "Complete canonical origin registry JSON.",
        ),
        ("registered_at", "TIMESTAMP", "REQUIRED", "BigQuery server-owned registration time."),
        ("registered_by", "STRING", "REQUIRED", "Registering control-plane identity."),
    ),
    "open_intelligence_execution_resource_manifests_v1": (
        (
            "resource_manifest_sha256",
            "STRING",
            "REQUIRED",
            "Immutable resource manifest digest.",
        ),
        ("origin_registry_sha256", "STRING", "REQUIRED", "Immutable origin registry digest."),
        (
            "canonical_resource_manifest_json",
            "STRING",
            "REQUIRED",
            "Complete canonical resource manifest JSON.",
        ),
        ("registered_at", "TIMESTAMP", "REQUIRED", "BigQuery server-owned registration time."),
        ("registered_by", "STRING", "REQUIRED", "Registering control-plane identity."),
    ),
    "open_intelligence_execution_active_generation_v1": (
        ("origin_registry_sha256", "STRING", "REQUIRED", "Active origin registry digest."),
        (
            "resource_manifest_sha256",
            "STRING",
            "REQUIRED",
            "Active resource manifest digest.",
        ),
    ),
}
LOCK_TABLE = "open_intelligence_execution_approval_lock"


def _sql(table: str, version: str) -> str:
    return (SCHEMAS / f"{table}_{version}.sql").read_text(encoding="utf-8")


def _assert_schema(sql: str, expected: tuple[tuple[str, str, str, str], ...]) -> None:
    assert migration._schema_from_sql(sql) == expected


@pytest.mark.parametrize("table", INHERITED_TABLES)
def test_v2_immutable_rows_preserve_v1_schema_and_append_generation_digests(table: str):
    inherited = migration._schema_from_sql(_sql(table, "v1"))
    actual_sql = _sql(table, "v2")

    _assert_schema(actual_sql, inherited + NEW_FIELDS)
    assert f"`{{project}}.{{dataset}}.{table}_v2`" in actual_sql


def test_v2_lock_preserves_v1_layout_and_declares_prepared_state():
    inherited = migration._schema_from_sql(_sql(LOCK_TABLE, "v1"))
    actual_sql = _sql(LOCK_TABLE, "v2")
    actual = migration._schema_from_sql(actual_sql)

    assert actual[:3] == inherited[:3]
    assert actual[4:] == inherited[4:]
    assert actual[3][:3] == inherited[3][:3]
    for state in ("prepared", "ready", "approving", "consuming", "recording_result", "disabled"):
        assert state in actual[3][3]
    assert f"`{{project}}.{{dataset}}.{LOCK_TABLE}_v2`" in actual_sql


@pytest.mark.parametrize("table,expected", tuple(NEW_TABLE_FIELDS.items()))
def test_registration_and_active_generation_tables_have_exact_required_layout(table, expected):
    actual_sql = _sql(table.rsplit("_", 1)[0], table.rsplit("_", 1)[1])

    _assert_schema(actual_sql, expected)
    assert f"`{{project}}.{{dataset}}.{table}`" in actual_sql


@pytest.mark.parametrize(
    "table",
    [*INHERITED_TABLES, LOCK_TABLE, *NEW_TABLE_FIELDS],
)
def test_versioned_store_schemas_are_additive_unseeded_and_unpartitioned(table: str):
    version = "v2" if table in (*INHERITED_TABLES, LOCK_TABLE) else table.rsplit("_", 1)[1]
    stem = table if table in (*INHERITED_TABLES, LOCK_TABLE) else table.rsplit("_", 1)[0]
    sql = _sql(stem, version)

    for forbidden in (
        "INSERT ",
        "MERGE ",
        "PARTITION BY",
        "CLUSTER BY",
        "PRIMARY KEY",
        "UNIQUE",
        "DEFAULT ",
        "GRANT ",
        "CREATE INDEX",
    ):
        assert forbidden not in sql.upper()


@pytest.mark.parametrize("mutation", ["order", "type", "nullability"])
def test_schema_contract_rejects_field_mutations(mutation: str):
    sql = _sql("open_intelligence_execution_active_generation", "v1")
    expected = NEW_TABLE_FIELDS["open_intelligence_execution_active_generation_v1"]

    if mutation == "order":
        first, second = sql.splitlines()[1:3]
        mutated = sql.replace(f"{first}\n{second}", f"{second}\n{first}", 1)
    elif mutation == "type":
        mutated = sql.replace("origin_registry_sha256 STRING", "origin_registry_sha256 INT64", 1)
    else:
        mutated = sql.replace(
            "origin_registry_sha256 STRING NOT NULL", "origin_registry_sha256 STRING", 1
        )

    with pytest.raises(AssertionError):
        _assert_schema(mutated, expected)


def test_versioned_store_installation_maps_list_every_v2_schema_once():
    from scripts import setup_bigquery as setup

    expected = (
        *(f"{table}_v2" for table in INHERITED_TABLES),
        f"{LOCK_TABLE}_v2",
        *NEW_TABLE_FIELDS,
        "open_intelligence_execution_origin_policies_v1",
        "open_intelligence_execution_derivations_v1",
        "open_intelligence_execution_derivation_tombstones_v1",
    )
    assert sorted(migration.V2_TABLE_NAMES) == sorted(expected)
    assert len(set(migration.V2_TABLE_NAMES)) == len(migration.V2_TABLE_NAMES)
    assert tuple(setup.APPROVAL_SCHEMA_ORDER[4:]) == tuple(
        f"{table}.sql" for table in migration.V2_TABLE_NAMES
    )
    assert migration.V2_TABLE_NAMES[-2:] == (
        "open_intelligence_execution_derivations_v1",
        "open_intelligence_execution_derivation_tombstones_v1",
    )
    assert tuple(migration.TABLE_NAMES) == tuple(
        name.removesuffix(".sql") for name in setup.APPROVAL_SCHEMA_ORDER[:4]
    )
    for table in migration.V2_TABLE_NAMES:
        assert (SCHEMAS / f"{table}.sql").is_file()


# Every column the DDL declares must be one the readback parser reads, otherwise a
# table passes the plan and refuses its own readback after the install has landed.
_DECLARED_COLUMN = re.compile(
    r"^  (?P<name>[a-z0-9_]+) (?P<type>STRING|INT64|TIMESTAMP)(?P<required> NOT NULL)?"
    r"(?: OPTIONS\(description = '(?P<description>[^']*)'\))?,?$",
    re.MULTILINE,
)
_API_TYPES = {"INT64": "INTEGER"}


@pytest.mark.parametrize("table", migration.V2_TABLE_NAMES)
def test_readback_parser_reads_every_declared_column(table: str):
    sql = (SCHEMAS / f"{table}.sql").read_text(encoding="utf-8")
    declared = _DECLARED_COLUMN.findall(sql)
    assert declared, table
    assert len(migration._schema_from_sql(sql)) == len(declared), table


class _Field:
    def __init__(self, name: str, field_type: str, mode: str, description: str | None):
        self.name = name
        self.field_type = field_type
        self.mode = mode
        self.description = description


class _Table:
    def __init__(self, schema: list[_Field]):
        self.schema = schema
        self.table_type = "TABLE"
        self.expires = None


def _api_schema(sql: str) -> list[_Field]:
    # The shape get_table reports for a table created from the file: every declared
    # column, NOT NULL as REQUIRED, INT64 as INTEGER, and None where the DDL has no
    # description.
    return [
        _Field(
            match.group("name"),
            _API_TYPES.get(match.group("type"), match.group("type")),
            "REQUIRED" if match.group("required") else "NULLABLE",
            match.group("description"),
        )
        for match in _DECLARED_COLUMN.finditer(sql)
    ]


class _Rows:
    def __init__(self, rows: list[dict[str, object]]):
        self._rows = rows

    def result(self) -> tuple[dict[str, object], ...]:
        return tuple(self._rows)


class _ReadbackClient:
    """Answers the readback from the plan files; get_table is recorded per table."""

    def __init__(self, plan, v1_lock: tuple[object, ...], renamed: str | None = None):
        self.plan = plan
        self.v1_lock = v1_lock
        self.renamed = renamed
        self.tables_read: list[str] = []
        self.routines = {
            item.name: migration._render_sql(migration._routine_body(item.sql))
            for item in migration.build_plan().routines
        }
        self.routines.update(
            {item.name: migration._v2_routine_definition(item) for item in plan.routines}
        )

    def get_table(self, reference: str) -> _Table:
        name = reference.rsplit(".", 1)[1]
        self.tables_read.append(name)
        sql = (SCHEMAS / f"{name}.sql").read_text(encoding="utf-8")
        schema = _api_schema(sql)
        if name == self.renamed:
            schema[-1].name = schema[-1].name + "_renamed"
        return _Table(schema)

    def query(self, sql: str, job_config=None) -> _Rows:
        if "open_intelligence_execution_approval_lock_v1`" in sql:
            return _Rows([dict(zip(migration._LOCK_FIELDS, self.v1_lock))])
        if "INFORMATION_SCHEMA.ROUTINES" in sql:
            return _Rows(
                [
                    {"routine_name": name, "routine_definition": definition}
                    for name, definition in self.routines.items()
                ]
            )
        if "open_intelligence_execution_approval_lock_v2`" in sql:
            return _Rows(
                [
                    {
                        "lock_name": migration.V2_APPROVAL_CONTRACT_VERSION,
                        "approval_contract_version": migration.V2_APPROVAL_CONTRACT_VERSION,
                        "lock_version": 0,
                        "state": "prepared",
                    }
                ]
            )
        for item in self.plan.registrations:
            if f"= '{item.sha256}'" in sql:
                return _Rows([{"canonical_json": item.canonical_json}])
        raise AssertionError(f"unexpected readback query: {sql[:80]}")


def test_readback_v2_store_reads_all_ten_tables_from_the_files_and_refuses_a_renamed_column(
    monkeypatch,
):
    plan = migration.build_v2_plan()
    v1_lock = (
        "open_intelligence_execution_approval_v1",
        "open_intelligence_execution_approval_v1",
        3,
        "ready",
        None,
        "2026-09-13T00:00:00+00:00",
        "2026-09-13T00:00:00+00:00",
    )
    assert len(v1_lock) == len(migration._LOCK_FIELDS)

    client = _ReadbackClient(plan, v1_lock)
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: client)
    migration._readback_v2_store(plan, object(), v1_lock)
    assert client.tables_read == list(migration.V2_TABLE_NAMES)
    assert len(client.tables_read) == 10

    renamed = _ReadbackClient(plan, v1_lock, renamed="open_intelligence_execution_derivations_v1")
    monkeypatch.setattr(migration, "_bigquery_client", lambda _credentials: renamed)
    with pytest.raises(migration.MigrationRefusal, match=r"^execution_approval_schema_mismatch$"):
        migration._readback_v2_store(plan, object(), v1_lock)
    assert renamed.tables_read[-1] == "open_intelligence_execution_derivations_v1"
