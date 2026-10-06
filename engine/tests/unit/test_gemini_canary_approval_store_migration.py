from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import google.auth
import pytest
from google.api_core.exceptions import NotFound
from google.auth import credentials as auth_credentials
from google.auth import impersonated_credentials
from google.auth.compute_engine import credentials as compute_credentials
from google.cloud import bigquery
from google.oauth2 import credentials as user_credentials
from google.oauth2 import service_account

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(
    os.environ.get(
        "CANARY_MIGRATION_SCRIPT",
        ROOT / "scripts" / "migrations" / "create_gemini_canary_approval_store.py",
    )
)
FIXTURE = ROOT / "tests" / "fixtures" / "open_intelligence" / "canary_approval_store_v1.json"

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
MIGRATION_IDENTITY = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
CANARY_MEMBER = "serviceAccount:trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com"
LOCK_TIME = datetime(2026, 8, 28, 6, 30, tzinfo=UTC)
LOCK_TABLE = f"{PROJECT}.{DATASET}.gemini_canary_approval_lock_v1"

EXPECTED_DIGESTS = {
    "gemini_canary_planning_outputs_v1": (
        "pse_5069dde6b0ddc7098ac20c8caf70151457fd6a92a4eb1bfbeb27a13083b3bd14"
    ),
    "gemini_canary_plan_approvals_v1": (
        "pse_9cc29d1e3d06713be8f368955429c5fb16d3583375193e0fb1f32b0ca30c0c49"
    ),
    "gemini_canary_approval_companions_v1": (
        "pse_2f9d3f3b3946ba22ef956ae2c4d77e4646a6631f1a94284f6ae82bd8246fea48"
    ),
    "gemini_canary_approval_lock_v1": (
        "pse_057c12363143e9a4fde0cc4b28dd0ae8dc451a994a75f61ba89d8e7827cf379e"
    ),
}

EXPECTED_IAM = (
    ("gemini_canary_planning_outputs_v1", "roles/bigquery.dataEditor", CANARY_MEMBER),
    ("gemini_canary_approval_lock_v1", "roles/bigquery.dataEditor", CANARY_MEMBER),
    ("gemini_canary_plan_approvals_v1", "roles/bigquery.dataViewer", CANARY_MEMBER),
    ("gemini_canary_approval_companions_v1", "roles/bigquery.dataViewer", CANARY_MEMBER),
)

_DDL = re.compile(
    r"\ACREATE TABLE IF NOT EXISTS `(?P<ref>[^`]+)` \(\n  "
    r"(?P<columns>.*?)\n\)\nOPTIONS\(description='(?P<description>[^']*)'\);\Z",
    re.S,
)


class _Signer:
    key_id = "fixture-key"

    def sign(self, message: bytes) -> bytes:
        return b"fixture-signature"


def _service_credentials(
    email=MIGRATION_IDENTITY, quota_project_id=None
) -> service_account.Credentials:
    return service_account.Credentials(
        signer=_Signer(),
        service_account_email=email,
        token_uri="https://example.invalid/token",
        project_id=PROJECT,
        quota_project_id=quota_project_id,
    )


class _SpoofCredentials:
    service_account_email = MIGRATION_IDENTITY
    quota_project_id = PROJECT


class _ServiceCredentialSubclass(service_account.Credentials):
    pass


class _ComputeCredentialSubclass(compute_credentials.Credentials):
    pass


def _service_subclass_credentials():
    return _ServiceCredentialSubclass(
        signer=_Signer(),
        service_account_email=MIGRATION_IDENTITY,
        token_uri="https://example.invalid/token",
        project_id=PROJECT,
        quota_project_id=PROJECT,
    )


def _impersonated_credentials():
    return impersonated_credentials.Credentials(
        source_credentials=_service_credentials(),
        target_principal=MIGRATION_IDENTITY,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


def _load_module():
    spec = importlib.util.spec_from_file_location("canary_approval_store_migration", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _schema_tuple(table):
    return tuple((field["name"], field["type"], field["mode"]) for field in table["schema"])


def _parse_ddl(sql):
    match = _DDL.fullmatch(sql)
    assert match is not None, sql
    schema = []
    for column in match.group("columns").split(",\n  "):
        name, declaration = column.split(" ", 1)
        required = declaration.endswith(" NOT NULL")
        if required:
            declaration = declaration.removesuffix(" NOT NULL")
        if declaration.startswith("ARRAY<") and declaration.endswith(">"):
            field_type = declaration[6:-1]
            mode = "REPEATED"
        else:
            field_type = declaration
            mode = "REQUIRED" if required else "NULLABLE"
        schema.append((name, field_type, mode))
    return {
        "table_ref": match.group("ref"),
        "schema": tuple(schema),
        "description": match.group("description"),
    }


def _table_resource(table_ref, schema, description, *, location=LOCATION, expires=None):
    return SimpleNamespace(
        schema=tuple(
            SimpleNamespace(name=name, field_type=field_type, mode=mode)
            for name, field_type, mode in schema
        ),
        description=description,
        location=location,
        expires=expires,
        time_partitioning=None,
        range_partitioning=None,
        clustering_fields=[],
        table_type="TABLE",
        table_id=table_ref.rsplit(".", 1)[-1],
    )


class _Job:
    def __init__(self, rows=()):
        self._rows = tuple(rows)

    def result(self):
        return self._rows


class _FakeClient:
    def __init__(self, *, existing=False, mutation=None):
        self.calls = []
        self.write_sql = []
        self.tables = {}
        self.lock_seeded = existing
        self.mutation = mutation
        if existing:
            for table in _fixture()["tables"]:
                table_ref = table["table_ref"]
                schema = _schema_tuple(table)
                description = table["description"]
                location = LOCATION
                expires = None
                if mutation == "schema" and table_ref == LOCK_TABLE:
                    schema = ((schema[0][0], "BYTES", schema[0][2]), *schema[1:])
                elif mutation == "description" and table_ref == LOCK_TABLE:
                    description = "wrong"
                elif mutation == "table_location" and table_ref == LOCK_TABLE:
                    location = "EU"
                elif mutation == "expiration" and table_ref == LOCK_TABLE:
                    expires = LOCK_TIME
                self.tables[table_ref] = _table_resource(
                    table_ref,
                    schema,
                    description,
                    location=location,
                    expires=expires,
                )

    def get_dataset(self, dataset_ref):
        self.calls.append(("get_dataset", dataset_ref))
        return SimpleNamespace(
            project=PROJECT,
            dataset_id=DATASET,
            location="EU" if self.mutation == "dataset_location" else LOCATION,
            default_table_expiration_ms=None,
            default_partition_expiration_ms=None,
        )

    def get_table(self, table_ref):
        self.calls.append(("get_table", table_ref))
        if table_ref not in self.tables:
            raise NotFound("missing fixture table")
        return self.tables[table_ref]

    def query(self, sql, **kwargs):
        assert kwargs.get("location") == LOCATION
        self.calls.append(("query", sql, kwargs))
        stripped = sql.lstrip()
        if stripped.startswith("CREATE TABLE"):
            parsed = _parse_ddl(sql)
            self.write_sql.append(sql)
            self.tables[parsed["table_ref"]] = _table_resource(
                parsed["table_ref"], parsed["schema"], parsed["description"]
            )
            return _Job()
        if stripped.startswith("INSERT INTO"):
            self.write_sql.append(sql)
            self.lock_seeded = True
            return _Job()
        lock = {
            "lock_name": "gemini_canary_approval_v1",
            "approval_contract_version": "gemini_canary_plan_approval_v1",
            "lock_version": 0,
            "state": "approving" if self.mutation == "lock_state" else "ready",
            "last_manifest_sha256": None,
            "created_at": LOCK_TIME,
            "updated_at": (
                LOCK_TIME + timedelta(seconds=1) if self.mutation == "lock_timestamp" else LOCK_TIME
            ),
        }
        return _Job((lock,) if self.lock_seeded else ())

    def get_iam_policy(self, table_ref):
        self.calls.append(("get_iam_policy", table_ref))
        return {}

    def set_iam_policy(self, *args, **kwargs):
        raise AssertionError("Task 1 must not mutate IAM")


def _install_runtime(monkeypatch, client, *, credentials=None, project=PROJECT):
    constructor_calls = []
    credentials = credentials or _service_credentials()
    monkeypatch.setattr(google.auth, "default", lambda: (credentials, project))

    def client_factory(**kwargs):
        constructor_calls.append(kwargs)
        return client

    monkeypatch.setattr(bigquery, "Client", client_factory)
    return constructor_calls, credentials


def _run_plan(migration, capsys):
    assert migration.main(["plan"]) == 0
    return json.loads(capsys.readouterr().out)


def _run_apply(migration, capsys):
    assert migration.main(["apply"]) == 0
    return json.loads(capsys.readouterr().out)


def test_public_apply_surface_accepts_only_argv():
    migration = _load_module()

    assert tuple(inspect.signature(migration.main).parameters) == ("argv",)
    assert not hasattr(migration, "apply_plan")
    public_functions = {
        name: value
        for name, value in inspect.getmembers(migration, inspect.isfunction)
        if not name.startswith("_") and value.__module__ == migration.__name__
    }
    assert set(public_functions) <= {"main", "parser"}
    assert not any(
        {"client", "credentials", "credential_loader", "client_factory"}
        & set(inspect.signature(function).parameters)
        for function in public_functions.values()
    )


def test_plan_constructs_no_client_or_credentials(monkeypatch, capsys):
    migration = _load_module()

    def boundary_bomb(*args, **kwargs):
        raise AssertionError("plan crossed a live client boundary")

    monkeypatch.setattr(google.auth, "default", boundary_bomb)
    monkeypatch.setattr(bigquery, "Client", boundary_bomb)
    output = _run_plan(migration, capsys)

    assert output["target"] == {"project": PROJECT, "dataset": DATASET, "location": LOCATION}


def test_emitted_ddl_parses_exact_schema_and_nullability(capsys):
    migration = _load_module()
    output = _run_plan(migration, capsys)
    fixture = _fixture()

    parsed = [_parse_ddl(sql) for sql in output["ddl_statements"]]
    assert [item["table_ref"] for item in parsed] == [
        item["table_ref"] for item in fixture["tables"]
    ]
    assert [item["schema"] for item in parsed] == [
        _schema_tuple(item) for item in fixture["tables"]
    ]
    assert [item["description"] for item in parsed] == [
        item["description"] for item in fixture["tables"]
    ]


def test_schema_digests_reproduce_from_fixture_bytes(capsys):
    migration = _load_module()
    output = _run_plan(migration, capsys)
    fixture = _fixture()
    reproduced = {}
    for table in fixture["tables"]:
        canonical = json.dumps(table["schema"], ensure_ascii=False, separators=(",", ":"))
        reproduced[table["name"]] = "pse_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert reproduced == EXPECTED_DIGESTS
    assert output["schema_digests"] == EXPECTED_DIGESTS


def test_lock_seed_uses_two_server_timestamps(capsys):
    migration = _load_module()
    sql = _run_plan(migration, capsys)["lock_seed_statement"]

    assert sql.count("CURRENT_TIMESTAMP()") == 2
    assert "@created_at" not in sql
    assert "@updated_at" not in sql
    assert "SERVER_CURRENT_TIMESTAMP" not in sql


def test_lock_seed_has_where_not_exists_guard(capsys):
    migration = _load_module()
    sql = _run_plan(migration, capsys)["lock_seed_statement"]

    assert sql.count("INSERT INTO") == 1
    assert f"WHERE NOT EXISTS (SELECT 1 FROM `{LOCK_TABLE}`);" in sql


def test_intended_iam_is_exact_and_table_scoped(capsys):
    migration = _load_module()
    output = _run_plan(migration, capsys)

    assert (
        tuple(
            (item["table"], item["role"], item["member"])
            for item in output["intended_table_iam_bindings"]
        )
        == EXPECTED_IAM
    )


@pytest.mark.parametrize("credential_kind", ["service", "compute"])
def test_apply_rejects_wrong_identity_before_client_construction(monkeypatch, credential_kind):
    migration = _load_module()
    client = _FakeClient(existing=True)
    if credential_kind == "service":
        credentials = _service_credentials("attacker@example.com")
    else:
        credentials = compute_credentials.Credentials(service_account_email="attacker@example.com")
    constructor_calls, _credentials = _install_runtime(monkeypatch, client, credentials=credentials)

    with pytest.raises(migration.MigrationRefusal, match="identity_invalid"):
        migration.main(["apply"])

    assert constructor_calls == []
    assert client.calls == []


@pytest.mark.parametrize(
    "credentials",
    [
        user_credentials.Credentials(token="fixture-user-token"),
        auth_credentials.AnonymousCredentials(),
        _impersonated_credentials(),
        _SpoofCredentials(),
        _service_subclass_credentials(),
        _ComputeCredentialSubclass(
            service_account_email=MIGRATION_IDENTITY,
            quota_project_id=PROJECT,
        ),
    ],
    ids=(
        "user-adc",
        "anonymous",
        "impersonated",
        "attribute-spoof",
        "service-subclass",
        "compute-subclass",
    ),
)
def test_credential_type_rejection_happens_before_client_construction(monkeypatch, credentials):
    migration = _load_module()
    client = _FakeClient(existing=True)
    constructor_calls, _credentials = _install_runtime(monkeypatch, client, credentials=credentials)

    with pytest.raises(migration.MigrationRefusal, match="identity_invalid"):
        migration.main(["apply"])

    assert constructor_calls == []
    assert client.calls == []


@pytest.mark.parametrize("quota_project", [None, PROJECT])
@pytest.mark.parametrize("credential_kind", ["service", "compute"])
def test_exact_credential_types_accept_approved_identity_project_and_quota(
    monkeypatch, capsys, quota_project, credential_kind
):
    migration = _load_module()
    client = _FakeClient(existing=True)
    if credential_kind == "service":
        credentials = _service_credentials(quota_project_id=quota_project)
    else:
        credentials = compute_credentials.Credentials(
            service_account_email=MIGRATION_IDENTITY,
            quota_project_id=quota_project,
        )
        monkeypatch.setattr(
            credentials,
            "refresh",
            lambda _request: pytest.fail("explicit compute identity was refreshed"),
        )
    constructor_calls, _credentials = _install_runtime(monkeypatch, client, credentials=credentials)

    result = _run_apply(migration, capsys)

    assert result["created_tables"] == []
    assert constructor_calls == [
        {"project": PROJECT, "credentials": credentials, "location": LOCATION}
    ]


@pytest.mark.parametrize("initial_identity", ["default", None, ""])
@pytest.mark.parametrize("quota_project", [None, PROJECT])
def test_exact_compute_deferred_identity_refreshes_once(
    monkeypatch, capsys, initial_identity, quota_project
):
    migration = _load_module()
    client = _FakeClient(existing=True)
    credentials = compute_credentials.Credentials(
        service_account_email=initial_identity,
        quota_project_id=quota_project,
    )
    request = object()
    refresh_calls = []

    def refresh(actual_request):
        refresh_calls.append(actual_request)
        credentials._service_account_email = MIGRATION_IDENTITY

    monkeypatch.setattr(credentials, "refresh", refresh)
    monkeypatch.setattr(migration.google_auth_requests, "Request", lambda: request)
    constructor_calls, _credentials = _install_runtime(monkeypatch, client, credentials=credentials)

    _run_apply(migration, capsys)

    assert refresh_calls == [request]
    assert constructor_calls == [
        {"project": PROJECT, "credentials": credentials, "location": LOCATION}
    ]


@pytest.mark.parametrize("credential_kind", ["service", "compute"])
def test_wrong_quota_project_rejected_before_refresh_or_client(monkeypatch, credential_kind):
    migration = _load_module()
    client = _FakeClient(existing=True)
    if credential_kind == "service":
        credentials = _service_credentials(quota_project_id="wrong-project")
    else:
        credentials = compute_credentials.Credentials(
            service_account_email="default",
            quota_project_id="wrong-project",
        )
        monkeypatch.setattr(
            credentials,
            "refresh",
            lambda _request: pytest.fail("wrong quota reached refresh"),
        )
    constructor_calls, _credentials = _install_runtime(monkeypatch, client, credentials=credentials)

    with pytest.raises(migration.MigrationRefusal, match="identity_invalid"):
        migration.main(["apply"])

    assert constructor_calls == []
    assert client.calls == []


def test_wrong_adc_project_rejected_before_compute_refresh_or_client(monkeypatch):
    migration = _load_module()
    client = _FakeClient(existing=True)
    credentials = compute_credentials.Credentials(service_account_email="default")
    monkeypatch.setattr(
        credentials,
        "refresh",
        lambda _request: pytest.fail("wrong project reached refresh"),
    )
    constructor_calls, _credentials = _install_runtime(
        monkeypatch,
        client,
        credentials=credentials,
        project="wrong-project",
    )

    with pytest.raises(migration.MigrationRefusal, match="identity_invalid"):
        migration.main(["apply"])

    assert constructor_calls == []
    assert client.calls == []


@pytest.mark.parametrize("resolved_identity", ["default", None, "attacker@example.com"])
def test_deferred_compute_wrong_identity_rejected_before_client(monkeypatch, resolved_identity):
    migration = _load_module()
    client = _FakeClient(existing=True)
    credentials = compute_credentials.Credentials(service_account_email="default")

    def refresh(_request):
        credentials._service_account_email = resolved_identity

    monkeypatch.setattr(credentials, "refresh", refresh)
    constructor_calls, _credentials = _install_runtime(monkeypatch, client, credentials=credentials)

    with pytest.raises(migration.MigrationRefusal, match="identity_invalid"):
        migration.main(["apply"])

    assert constructor_calls == []
    assert client.calls == []


def test_apply_creates_missing_store_from_emitted_sql(monkeypatch, capsys):
    migration = _load_module()
    client = _FakeClient()
    constructor_calls, credentials = _install_runtime(monkeypatch, client)

    result = _run_apply(migration, capsys)

    assert result["created_tables"] == [item["table_ref"] for item in _fixture()["tables"]]
    assert result["lock_seeded"] is True
    assert len(client.write_sql) == 5
    assert constructor_calls == [
        {"project": PROJECT, "credentials": credentials, "location": LOCATION}
    ]


def test_every_query_uses_us_location(monkeypatch, capsys):
    migration = _load_module()
    client = _FakeClient()
    _install_runtime(monkeypatch, client)

    _run_apply(migration, capsys)

    query_calls = [call for call in client.calls if call[0] == "query"]
    assert query_calls
    assert all(call[2] == {"location": LOCATION} for call in query_calls)


def test_second_apply_is_idempotent(monkeypatch, capsys):
    migration = _load_module()
    client = _FakeClient(existing=True)
    _install_runtime(monkeypatch, client)

    first = _run_apply(migration, capsys)
    second = _run_apply(migration, capsys)

    assert first["created_tables"] == []
    assert second["created_tables"] == []
    assert first["lock_seeded"] is False
    assert second["lock_seeded"] is False
    assert client.write_sql == []


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "description",
        "dataset_location",
        "table_location",
        "expiration",
        "lock_state",
        "lock_timestamp",
    ],
)
def test_existing_store_mismatch_stops_before_mutation(monkeypatch, mutation):
    migration = _load_module()
    client = _FakeClient(existing=True, mutation=mutation)
    _install_runtime(monkeypatch, client)

    with pytest.raises(migration.MigrationRefusal, match="storage_schema_mismatch"):
        migration.main(["apply"])

    assert client.write_sql == []
    assert [call[1] for call in client.calls if call[0] == "get_table"] == [
        item["table_ref"] for item in _fixture()["tables"]
    ]


# --- Human approval principal -------------------------------------------------
#
# One named human approves a canary plan. The infrastructure identity that owns
# the project is not that human, and no machine identity ever is.


def test_the_sole_canary_approver_is_the_named_ogilvy_human():
    migration = _load_module()
    assert migration.CANARY_HUMAN_APPROVER == "albert.meintjes@ogilvy.co.za"
    assert (
        migration._validate_approval_principal("albert.meintjes@ogilvy.co.za")
        == "albert.meintjes@ogilvy.co.za"
    )


def test_the_infrastructure_identity_may_not_approve():
    """Break caught: the project owner account approving its own model spend.

    jhb.analytics@gmail.com owns the project and deploys it. Owning the
    infrastructure is not authority to approve what the model is asked to do.
    """
    migration = _load_module()
    with pytest.raises(migration.MigrationRefusal, match="infrastructure and deployment"):
        migration._validate_approval_principal("jhb.analytics@gmail.com")


@pytest.mark.parametrize(
    "principal",
    [
        "trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com",
        "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "590353929363-compute@developer.gserviceaccount.com",
        "serviceAccount:trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com",
    ],
    ids=["canary_sa", "staging_sa", "compute_sa", "prefixed_sa"],
)
def test_no_machine_identity_may_approve(principal):
    migration = _load_module()
    # The message matters, not only the refusal. An operator who sees a
    # machine identity named as an approver needs to know that is why, rather
    # than reading a generic "not the approved human" and hunting for a typo.
    with pytest.raises(migration.MigrationRefusal, match="machine identity"):
        migration._validate_approval_principal(principal)


@pytest.mark.parametrize(
    "principal",
    [
        "someone.else@ogilvy.co.za",
        "albert.meintjes@wpp.com",
        "albert.meintjes@ogilvy.co.za.attacker.test",
        "attacker@evil.test",
        "",
        "   ",
        None,
        123,
    ],
    ids=[
        "other_ogilvy_human",
        "right_human_wrong_domain",
        "suffix_lookalike_domain",
        "foreign",
        "empty",
        "blank",
        "none",
        "not_a_string",
    ],
)
def test_every_foreign_principal_is_refused(principal):
    migration = _load_module()
    with pytest.raises(migration.MigrationRefusal):
        migration._validate_approval_principal(principal)


def test_surrounding_whitespace_and_case_are_normalised_not_a_second_identity():
    migration = _load_module()
    assert (
        migration._validate_approval_principal("  Albert.Meintjes@Ogilvy.co.za  ")
        == "albert.meintjes@ogilvy.co.za"
    )
