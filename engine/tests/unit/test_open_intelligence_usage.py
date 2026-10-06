from __future__ import annotations

import datetime
import importlib
import re
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.cloud.bigquery.table import Row
from src.utils import gemini_usage as usage

TREND_DATE = datetime.date(2026, 8, 25)
RECORDED_AT = datetime.datetime(2026, 8, 25, 7, 10, 4, tzinfo=datetime.UTC)
ROOT = Path(__file__).resolve().parents[2]
STAGING_SERVICE_ACCOUNT = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"


def _event(**overrides):
    values = {
        "trend_date": TREND_DATE,
        "run_id": "question_run_001",
        "consumer": "open_question_answer",
        "stage": "planning",
        "call_index": 0,
        "market": "za",
        "gemini_model": "gemini-3.5-flash",
        "prompt_tokens": 2100,
        "completion_tokens": 180,
        "recorded_at": RECORDED_AT,
    }
    values.update(overrides)
    return usage.build_usage_event(**values)


def test_build_usage_event_uses_the_approved_full_digest_and_one_call():
    event = _event()

    assert event.usage_id == (
        "usage_19c66a5c5b50578654f2f0b7af84ef1eb8b3c734feccbcfe0ba6f7d1f57b235a"
    )
    assert event.calls == 1
    assert event.prompt_tokens == 2100
    assert event.completion_tokens == 180


def test_build_usage_event_is_deterministic_and_retains_the_pending_event():
    first = _event()
    second = _event()

    assert first == second
    assert first is not second
    with pytest.raises(FrozenInstanceError):
        first.prompt_tokens = 999


def test_build_usage_event_normalizes_recorded_at_to_utc():
    local_time = datetime.datetime(
        2026,
        8,
        25,
        9,
        10,
        4,
        tzinfo=datetime.timezone(datetime.timedelta(hours=2)),
    )

    event = _event(recorded_at=local_time)

    assert event.recorded_at == RECORDED_AT
    assert event.recorded_at.tzinfo is datetime.UTC


@pytest.mark.parametrize(
    ("consumer", "stage"),
    [
        ("dynamic_signal_summary", "planning"),
        ("open_question_answer", "summary"),
        ("trend_analysis", "summary"),
        ("unknown_consumer", "planning"),
    ],
)
def test_build_usage_event_rejects_unapproved_consumer_stage_pairs(consumer, stage):
    with pytest.raises(ValueError, match=r"consumer.*stage"):
        _event(consumer=consumer, stage=stage)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", None),
        ("run_id", ""),
        ("stage", None),
        ("call_index", None),
        ("call_index", -1),
        ("call_index", True),
        ("gemini_model", None),
        ("gemini_model", ""),
    ],
)
def test_build_usage_event_rejects_missing_or_invalid_metadata(field, value):
    with pytest.raises((TypeError, ValueError)):
        _event(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt_tokens", -1),
        ("completion_tokens", -1),
        ("prompt_tokens", 1.5),
        ("completion_tokens", True),
    ],
)
def test_build_usage_event_rejects_invalid_token_deltas(field, value):
    with pytest.raises((TypeError, ValueError)):
        _event(**{field: value})


def test_build_usage_event_accepts_only_the_approved_stage_pairings():
    summary = _event(
        consumer="dynamic_signal_summary",
        stage="summary",
        run_id="summary_run_001",
    )
    planning = _event(stage="planning")
    answering = _event(stage="answering", call_index=1)

    assert summary.stage == "summary"
    assert planning.stage == "planning"
    assert answering.stage == "answering"


class _MemoryStore:
    def __init__(self):
        self.rows = {}

    def merge_and_read(self, event):
        self.rows.setdefault(event.usage_id, asdict(event))
        return [dict(self.rows[event.usage_id])]


def test_persist_usage_event_retries_the_same_immutable_event_once():
    store = _MemoryStore()
    event = _event()

    assert usage.persist_usage_event(event, store=store) is event
    assert usage.persist_usage_event(event, store=store) is event
    assert list(store.rows) == [event.usage_id]
    assert store.rows[event.usage_id] == asdict(event)


def test_persist_usage_event_rejects_a_different_content_collision_and_keeps_first():
    store = _MemoryStore()
    first = _event(prompt_tokens=2100)
    collision = _event(prompt_tokens=2101)
    assert collision.usage_id == first.usage_id

    usage.persist_usage_event(first, store=store)
    with pytest.raises(usage.GeminiUsagePersistenceError, match="mismatch"):
        usage.persist_usage_event(collision, store=store)

    assert store.rows[first.usage_id] == asdict(first)


@pytest.mark.parametrize("rows", [[], [{}, {}]])
def test_persist_usage_event_rejects_missing_or_duplicate_readback(rows):
    class _Store:
        def merge_and_read(self, _event):
            return rows

    with pytest.raises(usage.GeminiUsagePersistenceError, match="readback"):
        usage.persist_usage_event(_event(), store=_Store())


def test_persist_usage_event_wraps_merge_or_query_failure():
    class _Store:
        def merge_and_read(self, _event):
            raise RuntimeError("query failed")

    with pytest.raises(usage.GeminiUsagePersistenceError, match="persistence failed"):
        usage.persist_usage_event(_event(), store=_Store())


class _QueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _Credentials:
    def __init__(
        self,
        service_account_email=STAGING_SERVICE_ACCOUNT,
        *,
        quota_project_id=None,
        resolved_email=None,
    ):
        self.service_account_email = service_account_email
        self.quota_project_id = quota_project_id
        self.resolved_email = resolved_email
        self.refreshes = 0

    def refresh(self, _request):
        self.refreshes += 1
        if self.resolved_email is not None:
            self.service_account_email = self.resolved_email


class _CapturingClient:
    def __init__(
        self,
        readback,
        *,
        project="ogilvy-trends-v2",
        location="US",
        credentials=None,
        omit_field=None,
    ):
        self.project = project
        self.location = location
        self._credentials = credentials or _Credentials()
        self.readback = readback
        self.omit_field = omit_field
        self.calls = []
        self.returned_rows = []

    def query(self, sql, *, job_config, location):
        self.calls.append((sql, job_config, location))
        if sql.lstrip().startswith("MERGE"):
            rows = []
        else:
            projection = sql.split("SELECT ", 1)[1].split("\nFROM", 1)[0]
            fields = [field.strip() for field in projection.split(",")]
            if self.omit_field is not None:
                fields.remove(self.omit_field)
            values = tuple(self.readback[field] for field in fields)
            row = Row(values, {field: index for index, field in enumerate(fields)})
            self.returned_rows = [row]
            rows = self.returned_rows
        return _QueryJob(rows)


def test_bigquery_store_uses_parameterized_insert_only_merge_and_exact_readback():
    event = _event()
    client = _CapturingClient(asdict(event))
    store = usage.BigQueryUsageEventStore(
        client,
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
    )

    assert usage.persist_usage_event(event, store=store) is event
    assert len(client.calls) == 2

    merge_sql, merge_config, merge_location = client.calls[0]
    assert "`ogilvy-trends-v2.trends_v2_staging.gemini_usage`" in merge_sql
    assert "WHEN NOT MATCHED THEN" in merge_sql
    assert "WHEN MATCHED" not in merge_sql
    assert "UPDATE" not in merge_sql
    assert event.run_id not in merge_sql
    assert event.gemini_model not in merge_sql
    assert merge_location == "US"
    merge_params = {p.name: (p.type_, p.value) for p in merge_config.query_parameters}
    assert merge_params == {
        "usage_id": ("STRING", event.usage_id),
        "trend_date": ("DATE", event.trend_date),
        "run_id": ("STRING", event.run_id),
        "consumer": ("STRING", event.consumer),
        "stage": ("STRING", event.stage),
        "call_index": ("INT64", event.call_index),
        "market": ("STRING", event.market),
        "gemini_model": ("STRING", event.gemini_model),
        "calls": ("INT64", 1),
        "prompt_tokens": ("INT64", event.prompt_tokens),
        "completion_tokens": ("INT64", event.completion_tokens),
        "recorded_at": ("TIMESTAMP", event.recorded_at),
    }
    for name in merge_params:
        assert f"@{name} AS {name}" in merge_sql

    read_sql, read_config, read_location = client.calls[1]
    assert "`ogilvy-trends-v2.trends_v2_staging.gemini_usage`" in read_sql
    projection = read_sql.split("SELECT ", 1)[1].split("\nFROM", 1)[0]
    assert tuple(field.strip() for field in projection.split(",")) == (usage.USAGE_EVENT_FIELDS)
    assert "WHERE usage_id = @usage_id" in read_sql
    assert read_location == "US"
    assert [(p.name, p.type_, p.value) for p in read_config.query_parameters] == [
        ("usage_id", "STRING", event.usage_id)
    ]
    assert isinstance(client.returned_rows[0], Row)


def test_bigquery_store_round_trips_null_market_and_utc_timestamp():
    local_time = datetime.datetime(
        2026,
        8,
        25,
        9,
        10,
        4,
        tzinfo=datetime.timezone(datetime.timedelta(hours=2)),
    )
    event = _event(market=None, recorded_at=local_time)
    client = _CapturingClient(asdict(event))
    store = usage.BigQueryUsageEventStore(
        client,
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
    )

    assert usage.persist_usage_event(event, store=store) is event
    merge_config = client.calls[0][1]
    params = {parameter.name: parameter.value for parameter in merge_config.query_parameters}
    assert params["market"] is None
    assert params["recorded_at"] == RECORDED_AT
    assert params["recorded_at"].tzinfo is datetime.UTC
    assert dict(client.returned_rows[0].items()) == asdict(event)


@pytest.mark.parametrize(
    ("omitted_field",),
    [
        ("usage_id",),
        ("trend_date",),
        ("run_id",),
        ("consumer",),
        ("stage",),
        ("call_index",),
        ("market",),
        ("gemini_model",),
        ("calls",),
        ("prompt_tokens",),
        ("completion_tokens",),
        ("recorded_at",),
    ],
)
def test_bigquery_store_rejects_readback_with_any_omitted_field(omitted_field):
    event = _event()
    client = _CapturingClient(asdict(event), omit_field=omitted_field)
    store = usage.BigQueryUsageEventStore(
        client,
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
    )

    with pytest.raises(usage.GeminiUsagePersistenceError, match="mismatch"):
        usage.persist_usage_event(event, store=store)


@pytest.mark.parametrize(
    ("project", "dataset"),
    [
        ("other-project", "trends_v2_staging"),
        ("ogilvy-trends-v2", "trends_v2"),
        ("ogilvy-trends-v2", "trends_v2_staging_qa"),
    ],
)
def test_bigquery_store_rejects_any_nonstaging_table(project, dataset):
    client = _CapturingClient({})

    with pytest.raises(ValueError, match="staging"):
        usage.BigQueryUsageEventStore(client, project=project, dataset=dataset)

    assert client.calls == []


@pytest.mark.parametrize(
    ("project", "dataset"),
    [
        ("other-project", "trends_v2_staging"),
        ("ogilvy-trends-v2", "trends_v2"),
        ("ogilvy-trends-v2", "trends_v2_staging_qa"),
    ],
)
def test_live_persistence_fences_target_before_client_creation(project, dataset):
    created = []

    def forbidden_client(**_kwargs):
        created.append(True)
        raise AssertionError("client must not be created for a refused target")

    with pytest.raises(ValueError, match="staging"):
        usage.persist_usage_event(
            _event(),
            project=project,
            dataset=dataset,
            credentials_loader=lambda **_kwargs: (_Credentials(), "ogilvy-trends-v2"),
            client_factory=forbidden_client,
        )

    assert created == []


@pytest.mark.parametrize("quota_project_id", [None, "ogilvy-trends-v2"])
def test_live_persistence_uses_the_exact_staging_adc_identity(quota_project_id):
    event = _event()
    credentials = _Credentials(quota_project_id=quota_project_id)
    loader_calls = []
    factory_calls = []

    def credentials_loader(**kwargs):
        loader_calls.append(kwargs)
        return credentials, "ogilvy-trends-v2"  # gitleaks:allow, public fixture project identifier

    def client_factory(**kwargs):
        factory_calls.append(kwargs)
        return _CapturingClient(
            asdict(event),
            project=kwargs["project"],
            location=kwargs["location"],
            credentials=kwargs["credentials"],
        )

    assert (
        usage.persist_usage_event(
            event,
            credentials_loader=credentials_loader,
            client_factory=client_factory,
            request_factory=object,
        )
        is event
    )
    assert len(loader_calls) == 1
    assert factory_calls == [
        {
            "project": "ogilvy-trends-v2",
            "location": "US",
            "credentials": credentials,
        }
    ]


@pytest.mark.parametrize(
    ("credentials", "adc_project"),
    [
        (_Credentials(service_account_email=None), "ogilvy-trends-v2"),
        (
            _Credentials(service_account_email="default", resolved_email="default"),
            "ogilvy-trends-v2",
        ),
        (
            _Credentials(
                service_account_email=(
                    "trends-engine-production@ogilvy-trends-v2.iam.gserviceaccount.com"
                )
            ),
            "ogilvy-trends-v2",
        ),
        (_Credentials(), "other-project"),
        (
            _Credentials(quota_project_id="other-project"),
            "ogilvy-trends-v2",
        ),
    ],
)
def test_live_persistence_rejects_unapproved_adc_before_client(credentials, adc_project):
    created = []

    def client_factory(**_kwargs):
        created.append(True)
        raise AssertionError("client must not be created for rejected ADC")

    with pytest.raises(ValueError, match="staging"):
        usage.persist_usage_event(
            _event(),
            credentials_loader=lambda **_kwargs: (credentials, adc_project),
            client_factory=client_factory,
            request_factory=object,
        )

    assert created == []


@pytest.mark.parametrize(
    "client_overrides",
    [
        {"project": "other-project"},
        {"location": "EU"},
        {"credentials": _Credentials()},
        {
            "credentials": _Credentials(
                service_account_email=(
                    "trends-engine-production@ogilvy-trends-v2.iam.gserviceaccount.com"
                )
            )
        },
    ],
)
def test_live_persistence_rejects_mismatched_client_before_query(client_overrides):
    event = _event()
    credentials = _Credentials()
    client_values = {"credentials": credentials}
    client_values.update(client_overrides)
    client = _CapturingClient(
        asdict(event),
        **client_values,
    )

    with pytest.raises(ValueError, match="staging"):
        usage.persist_usage_event(
            event,
            credentials_loader=lambda **_kwargs: (credentials, "ogilvy-trends-v2"),  # gitleaks:allow, public fixture project identifier
            client_factory=lambda **_kwargs: client,
            request_factory=object,
        )

    assert client.calls == []


def test_live_persistence_resolves_deferred_compute_identity_once():
    event = _event()
    credentials = _Credentials(
        service_account_email="default",
        resolved_email=STAGING_SERVICE_ACCOUNT,
    )

    def client_factory(**kwargs):
        return _CapturingClient(
            asdict(event),
            project=kwargs["project"],
            location=kwargs["location"],
            credentials=kwargs["credentials"],
        )

    assert (
        usage.persist_usage_event(
            event,
            credentials_loader=lambda **_kwargs: (credentials, "ogilvy-trends-v2"),  # gitleaks:allow, public fixture project identifier
            client_factory=client_factory,
            request_factory=object,
        )
        is event
    )
    assert credentials.refreshes == 1


def _migration():
    return importlib.import_module("scripts.migrations.add_open_intelligence_usage_columns")


def test_canonical_schema_keeps_old_columns_and_adds_nullable_event_metadata():
    sql = (ROOT / "infra" / "bigquery_schemas" / "gemini_usage.sql").read_text(encoding="utf-8")
    old_columns = {
        "usage_id": "STRING NOT NULL",
        "trend_date": "DATE NOT NULL",
        "consumer": "STRING NOT NULL",
        "market": "STRING",
        "gemini_model": "STRING",
        "calls": "INT64",
        "prompt_tokens": "INT64",
        "completion_tokens": "INT64",
        "recorded_at": "TIMESTAMP",
    }
    for name, definition in old_columns.items():
        assert re.search(rf"^\s*{name}\s+{definition}\s*,?$", sql, re.MULTILINE)
    for name, field_type in {
        "run_id": "STRING",
        "stage": "STRING",
        "call_index": "INT64",
    }.items():
        match = re.search(rf"^\s*{name}\s+([^,]+),?$", sql, re.MULTILINE)
        assert match is not None
        assert match.group(1).strip() == field_type
    assert "aggregate rows" in sql.lower()
    assert "per-call deltas" in sql.lower()


def test_migration_default_dry_run_never_creates_a_client(capsys):
    migration = _migration()
    created = []

    def forbidden_client(**_kwargs):
        created.append(True)
        raise AssertionError("dry run must not create a BigQuery client")

    assert migration.main([], client_factory=forbidden_client) == 0
    assert created == []
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert output.count("ADD COLUMN IF NOT EXISTS") == 3


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--project", "other-project"),
        ("--dataset", "trends_v2"),
        ("--dataset", "trends_v2_staging_qa"),
        ("--location", "EU"),
        ("--service-account", "other@example.iam.gserviceaccount.com"),
    ],
)
def test_migration_apply_rejects_any_unapproved_target_before_client(flag, value):
    migration = _migration()
    created = []

    def forbidden_client(**_kwargs):
        created.append(True)
        raise AssertionError("client must not be created for a refused target")

    with pytest.raises(ValueError, match="staging"):
        migration.main(["--apply", flag, value], client_factory=forbidden_client)
    assert created == []


class _MigrationJob:
    def result(self):
        return None


class _MigrationClient:
    project = "ogilvy-trends-v2"
    location = "US"

    def __init__(self, schema):
        self._credentials = SimpleNamespace(
            service_account_email=("trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com")
        )
        self.schema = schema
        self.queries = []
        self.table_refs = []

    def query(self, sql, *, location):
        self.queries.append((sql, location))
        return _MigrationJob()

    def get_table(self, table_ref):
        self.table_refs.append(table_ref)
        return SimpleNamespace(schema=self.schema)


def _schema_field(name, field_type, mode="NULLABLE"):
    return SimpleNamespace(name=name, field_type=field_type, mode=mode)


def _event_schema():
    return [
        _schema_field("usage_id", "STRING", "REQUIRED"),
        _schema_field("run_id", "STRING"),
        _schema_field("stage", "STRING"),
        _schema_field("call_index", "INTEGER"),
    ]


def test_migration_apply_uses_idempotent_ddl_then_verifies_the_table_resource():
    migration = _migration()
    client = _MigrationClient(_event_schema())

    assert migration.main(["--apply"], client_factory=lambda **_kwargs: client) == 0

    assert client.queries == [
        (
            "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.gemini_usage` "
            "ADD COLUMN IF NOT EXISTS run_id STRING",
            "US",
        ),
        (
            "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.gemini_usage` "
            "ADD COLUMN IF NOT EXISTS stage STRING",
            "US",
        ),
        (
            "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.gemini_usage` "
            "ADD COLUMN IF NOT EXISTS call_index INT64",
            "US",
        ),
    ]
    assert client.table_refs == ["ogilvy-trends-v2.trends_v2_staging.gemini_usage"]


@pytest.mark.parametrize(
    "schema",
    [
        [_schema_field("run_id", "STRING"), _schema_field("stage", "STRING")],
        [
            _schema_field("run_id", "BYTES"),
            _schema_field("stage", "STRING"),
            _schema_field("call_index", "INTEGER"),
        ],
        [
            _schema_field("run_id", "STRING", "REQUIRED"),
            _schema_field("stage", "STRING"),
            _schema_field("call_index", "INTEGER"),
        ],
    ],
)
def test_migration_schema_verification_rejects_missing_wrong_type_or_mode(schema):
    migration = _migration()

    with pytest.raises(migration.UsageColumnMigrationError, match="schema"):
        migration.verify_columns(SimpleNamespace(schema=schema))


def test_migration_apply_rejects_the_wrong_runtime_service_account():
    migration = _migration()
    client = _MigrationClient(_event_schema())
    client._credentials.service_account_email = "wrong@example.iam.gserviceaccount.com"

    with pytest.raises(ValueError, match="service account"):
        migration.main(["--apply"], client_factory=lambda **_kwargs: client)

    assert client.queries == []


def test_migration_resolves_a_deferred_compute_service_account_before_apply():
    migration = _migration()
    client = _MigrationClient(_event_schema())

    class _DeferredCredentials:
        service_account_email = "default"

        def __init__(self):
            self.refreshes = 0

        def refresh(self, _request):
            self.refreshes += 1
            self.service_account_email = (
                "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
            )

    credentials = _DeferredCredentials()
    client._credentials = credentials

    assert migration.main(["--apply"], client_factory=lambda **_kwargs: client) == 0
    assert credentials.refreshes == 1
    assert len(client.queries) == 3


def test_migration_recovers_after_the_first_column_was_applied():
    migration = _migration()
    usage_id = _schema_field("usage_id", "STRING", "REQUIRED")
    consumer = _schema_field("consumer", "STRING", "REQUIRED")

    class _PartialMigrationClient(_MigrationClient):
        def __init__(self):
            super().__init__([usage_id, consumer])
            self.fail_second = True

        def query(self, sql, *, location):
            self.queries.append((sql, location))
            if self.fail_second and len(self.queries) == 2:
                raise RuntimeError("interrupted after first DDL")
            column_name, field_type = sql.rsplit(" ", 2)[-2:]
            exists = any(field.name == column_name for field in self.schema)
            if exists and "IF NOT EXISTS" not in sql:
                raise RuntimeError(f"duplicate column {column_name}")
            if not exists:
                self.schema.append(_schema_field(column_name, field_type))
            return _MigrationJob()

    client = _PartialMigrationClient()
    factory_calls = []

    def client_factory(**kwargs):
        factory_calls.append(kwargs)
        return client

    with pytest.raises(RuntimeError, match="interrupted"):
        migration.main(["--apply"], client_factory=client_factory)

    assert [field.name for field in client.schema] == ["usage_id", "consumer", "run_id"]
    assert client.schema[:2] == [usage_id, consumer]

    client.fail_second = False
    assert migration.main(["--apply"], client_factory=client_factory) == 0

    assert factory_calls == [
        {"project": "ogilvy-trends-v2", "location": "US"},
        {"project": "ogilvy-trends-v2", "location": "US"},
    ]
    assert [sql for sql, _location in client.queries].count(
        "ALTER TABLE `ogilvy-trends-v2.trends_v2_staging.gemini_usage` "
        "ADD COLUMN IF NOT EXISTS run_id STRING"
    ) == 2
    assert client.schema[:2] == [usage_id, consumer]
    assert {field.name: (field.field_type, field.mode) for field in client.schema} == {
        "usage_id": ("STRING", "REQUIRED"),
        "consumer": ("STRING", "REQUIRED"),
        "run_id": ("STRING", "NULLABLE"),
        "stage": ("STRING", "NULLABLE"),
        "call_index": ("INT64", "NULLABLE"),
    }
    assert client.table_refs == ["ogilvy-trends-v2.trends_v2_staging.gemini_usage"]
