"""Selected fresh v2 runtime callers, group A.

Four entrypoints (migration apply, exposure issue, Brain read, Wave 1 pilot) and the Wave 1
capability consumer load execution authority with explicit mode new_consume, take job,
principal, repository and image namespace from the origin the issued authority carries, and
record failure with the same consumption and generation pair as the consumption.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import execution_approval, execution_generations, funded_lane
from src.analysis.open_intelligence.execution_origins import select_origin

from tests.unit.test_execution_runtime_v2 import (
    OTHER_SHA256,
    PAIR,
    consume,
    consumption_row,
    fixture,
    load,
    result_row,
)

MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
CONSUMPTION_V2 = "open_intelligence_execution_consumption_v2"
RETAINED_IDENTITIES = {
    "migration_apply": (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-migration-staging",
        "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    ),
    "collection_exposure_issue": (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-exposure-issuer-staging",
        "trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com",
    ),
    "brain_read": (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-brain-staging",
        "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com",
    ),
    "wave1_pilot": (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-open-intelligence-staging",
        "trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com",
    ),
}
RETAINED_PRINCIPALS = (
    "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com",
    "trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com",
    "trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com",
    "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com",
    "trends-engine-oi-wave1@ogilvy-trends-v2.iam.gserviceaccount.com",
)


def migration_module():
    return importlib.import_module("scripts.migrations.create_open_intelligence_v2")


def issuer_module():
    return importlib.import_module("scripts.staging.issue_collection_exposure_receipts")


def brain_module():
    return importlib.import_module("scripts.staging.run_live_intelligence_brain")


def pilot_module():
    return importlib.import_module("scripts.run_rss_now")


def binding(operation):
    generation = execution_generations.active_generation()
    fx = fixture(operation)
    origin = select_origin(
        manifest_version=MANIFEST_V2,
        contract_sha256=fx.payload["contract_sha256"],
        mode="new_consume",
        registry=generation.registry,
    )
    return origin, origin.operation_bindings[operation]


def consumed(operation):
    fx = fixture(operation)
    authority = load(fx)
    consumption = consume(fx, authority)
    return fx, authority, consumption


def v1_approval_authority(authority):
    """The issued authority with its v2 approval swapped for a v1 shaped record."""
    supplied = SimpleNamespace(
        **{name: getattr(authority, name) for name in type(authority).__slots__},
    )
    supplied.approval = SimpleNamespace(
        manifest_sha256=authority.approval.manifest_sha256,
        approval_id=authority.approval.approval_id,
        approved_at=authority.approval.approved_at,
        origin_registry_sha256=PAIR[0],
        resource_manifest_sha256=PAIR[1],
    )
    return supplied


def retained_authority(operation, **overrides):
    """A well formed authority double carrying the retained v1 identity and no generation."""
    job, identity = RETAINED_IDENTITIES[operation]
    manifest = SimpleNamespace(
        manifest_version="open_intelligence_execution_manifest_v1",
        contract_sha256="a" * 64,
        operation=operation,
        job_resource=job,
        service_identity=identity,
        source_sha="b" * 40,
        image_uri="us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:"
        + "c" * 64,
        command=("python",),
        arguments=(),
        input_artifacts=(("config", "d" * 64),),
        limits={"max_bytes_billed": 50_000_000_000},
    )
    values = {
        "operation": operation,
        "manifest": manifest,
        "approval": SimpleNamespace(
            manifest_sha256="e" * 64,
            approval_id="exa_" + "f" * 64,
            approved_at=datetime(2026, 9, 13, 11, tzinfo=UTC),
        ),
        "execution_name": job + "/executions/exe-1",
        "job_resource": job,
        "source_sha": manifest.source_sha,
        "image_uri": manifest.image_uri,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def recording_writer(rows):
    def writer(request):
        rows.append(dict(request))
        return result_row(request)

    return writer


def recorded_result(rows, consumption_id):
    assert len(rows) == 1
    assert rows[0]["consumption_id"] == consumption_id
    return [result_row(rows[0])]


def wrong_generation_row(fx):
    return lambda request: consumption_row(fx, request, resource_sha=OTHER_SHA256)


def v1_family_row(fx):
    def writer(request):
        row = consumption_row(fx, request)
        row["consumption_contract_version"] = "open_intelligence_execution_consumption_v1"
        return row

    return writer


def assert_failure_recorded(rows, consumption):
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    assert row["consumption_id"] == consumption.consumption_id
    assert row["approval_id"] == consumption.approval_id
    assert (row["origin_registry_sha256"], row["resource_manifest_sha256"]) == PAIR
    assert (consumption.origin_registry_sha256, consumption.resource_manifest_sha256) == PAIR
    assert '"status":"failed"' in row["canonical_result_json"]


# migration_apply


def test_migration_loader_receives_explicit_new_consume_mode():
    migration = migration_module()
    _fx, authority, consumption = consumed("migration_apply")
    calls = []

    def loader(operation, **kwargs):
        calls.append((operation, kwargs))
        return authority

    payload = migration._execute_durable_migration(
        authority_loader=loader,
        authority_consumer=lambda supplied: consumption,
        plan_applier=lambda plan: tuple(
            item.name for item in (*plan.statements, *plan.runtime_closure_statements)
        ),
        result_recorder=lambda *args: SimpleNamespace(result_id="exr_" + "1" * 64),
    )
    assert calls == [("migration_apply", {"mode": "new_consume"})]
    assert payload["status"] == "succeeded"


def test_migration_plan_identity_and_iam_principals_come_from_the_active_origin():
    migration = migration_module()
    origin, bound = binding("migration_apply")
    plan = migration.build_plan("staging")
    assert plan.service_account == bound.service_identity
    assert (
        plan.service_account == "intelligence-42-migration@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert (plan.origin_registry_sha256, plan.resource_manifest_sha256) == PAIR
    assert plan.connected_repo == origin.connected_repo
    assert plan.image_repository == origin.image_repository
    apply_binding = binding("r3_apply")[1]
    release_binding = binding("r3_release")[1]
    principals = tuple(item.principal for item in plan.runtime_closure_iam_bindings)
    # Since amendment d r3_apply and r3_release share one identity, so the closure is
    # deduplicated on principal, resource and role and the release row folds into the
    # apply row.
    assert apply_binding.service_identity == release_binding.service_identity
    assert principals == (
        "user:albert.meintjes@ogilvy.co.za",
        f"serviceAccount:{apply_binding.service_identity}",
    )
    rendered = migration.render_dry_run(plan)
    assert f"Required service identity: {bound.service_identity}" in rendered
    assert PAIR[0] in rendered
    assert PAIR[1] in rendered
    payload = json.loads(migration._build_migration_execution_artifacts(plan)["migration_plan"])
    assert payload["service_account"] == bound.service_identity
    assert payload["origin_registry_sha256"] == PAIR[0]
    assert payload["resource_manifest_sha256"] == PAIR[1]
    qa = migration.build_plan("qa")
    assert qa.service_account == "trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert (qa.origin_registry_sha256, qa.resource_manifest_sha256) == (None, None)


def test_migration_plan_render_selects_the_origin_under_new_approval(monkeypatch):
    migration = migration_module()
    modes = []
    real = migration.select_origin

    def spy(**kwargs):
        modes.append(kwargs["mode"])
        return real(**kwargs)

    monkeypatch.setattr(migration, "select_origin", spy)
    plan = migration.build_plan("staging")
    migration.render_dry_run(plan)
    assert modes
    assert set(modes) == {"new_approval"}
    _fx, authority, consumption = consumed("migration_apply")
    modes.clear()
    migration._execute_durable_migration(
        authority_loader=lambda operation, **kwargs: authority,
        authority_consumer=lambda supplied: consumption,
        plan_applier=lambda plan: tuple(
            item.name for item in (*plan.statements, *plan.runtime_closure_statements)
        ),
        result_recorder=lambda *args: SimpleNamespace(result_id="exr_" + "1" * 64),
    )
    assert modes[-1] == "new_consume"
    assert set(modes[:-1]) == {"new_approval"}


def test_migration_source_carries_no_retained_principal():
    source = inspect.getsource(migration_module())
    for principal in RETAINED_PRINCIPALS:
        assert principal not in source
    assert "trends-engine-oi-migration-staging" not in source


@pytest.mark.parametrize(
    "change",
    ["retained_identity", "foreign_generation", "foreign_job", "v1_approval"],
)
def test_migration_fresh_run_refuses_an_authority_off_the_active_origin(change):
    migration = migration_module()
    _fx, authority, consumption = consumed("migration_apply")
    if change == "retained_identity":
        supplied = retained_authority("migration_apply")
    else:
        supplied = SimpleNamespace(
            **{name: getattr(authority, name) for name in type(authority).__slots__},
        )
        if change == "foreign_generation":
            supplied.generation = SimpleNamespace(
                origin_registry_sha256=OTHER_SHA256,
                resource_manifest_sha256=PAIR[1],
                registry=authority.generation.registry,
            )
        elif change == "foreign_job":
            supplied.job_resource = RETAINED_IDENTITIES["migration_apply"][0]
        else:
            supplied = v1_approval_authority(authority)
    events = []
    with pytest.raises(migration.MigrationError, match="origin"):
        migration._execute_durable_migration(
            authority_loader=lambda operation, **kwargs: supplied,
            authority_consumer=lambda _authority: events.append("consume") or consumption,
            plan_applier=lambda plan: events.append("apply") or (),
            result_recorder=lambda *args: events.append("result"),
        )
    assert events == []
    assert migration._DURABLE_ARTIFACT_CONTEXT is None


def test_migration_failure_is_recorded_with_the_consumed_pair():
    migration = migration_module()
    _fx, authority, consumption = consumed("migration_apply")
    rows = []

    def fail_apply(_plan):
        raise RuntimeError("private failure detail")

    with pytest.raises(RuntimeError, match="private failure detail"):
        migration._execute_durable_migration(
            authority_loader=lambda operation, **kwargs: authority,
            authority_consumer=lambda supplied: consumption,
            plan_applier=fail_apply,
            result_recorder=partial(
                execution_approval._record_execution_result,
                result_writer=recording_writer(rows),
                result_reader=lambda consumption_id: recorded_result(rows, consumption_id),
            ),
        )
    assert_failure_recorded(rows, consumption)
    assert "private failure detail" not in rows[0]["canonical_result_json"]


@pytest.mark.parametrize("family", ["other_generation", "v1_record"])
def test_migration_refuses_a_consumption_from_another_generation_or_family(family):
    migration = migration_module()
    fx = fixture("migration_apply")
    authority = load(fx)
    writer = wrong_generation_row(fx) if family == "other_generation" else v1_family_row(fx)
    events = []
    with pytest.raises(execution_approval.ApprovalRefusal, match="schema_mismatch"):
        migration._execute_durable_migration(
            authority_loader=lambda operation, **kwargs: authority,
            authority_consumer=partial(
                execution_approval._consume_execution_authority, consumption_writer=writer
            ),
            plan_applier=lambda plan: events.append("apply") or (),
            result_recorder=lambda *args: events.append("result"),
        )
    assert events == []


# collection_exposure_issue


def exposure_receipt(fx, identity):
    from tests.unit.test_collection_exposure_receipts import _fields

    return _fields(
        source_sha=fx.payload["source_sha"],
        image_digest=fx.payload["image_uri"].rsplit("@", 1)[1],
        issuer_identity=identity,
    )


def exposure_query_runner(receipt, events):
    def query_runner(sql):
        events.append("query")
        if sql.startswith("SELECT COUNT(*)"):
            return [{"existing": 0}]
        if sql.startswith("SELECT exposure_contract_version"):
            return [dict(receipt)]
        return []

    return query_runner


def test_exposure_loader_receives_explicit_new_consume_mode(monkeypatch):
    issuer = issuer_module()
    fx = fixture("collection_exposure_issue")
    authority = load(fx)
    origin, bound = binding("collection_exposure_issue")
    receipt = exposure_receipt(fx, bound.service_identity)
    calls = []
    rows = []
    events = []

    def loader(operation, **kwargs):
        calls.append((operation, kwargs))
        return authority

    monkeypatch.setattr(
        execution_approval,
        "_default_result_reader",
        lambda consumption_id, **_kwargs: recorded_result(rows, consumption_id),
    )
    output = issuer._execute_durable_collection_exposure(
        receipts=(receipt,),
        query_runner=exposure_query_runner(receipt, events),
        authority_loader=loader,
        consumption_writer=lambda request: consumption_row(fx, request),
        result_writer=recording_writer(rows),
    )
    assert calls == [
        (
            "collection_exposure_issue",
            {"mode": "new_consume", "artifact_reader": issuer._execution_approval_artifact_bytes},
        )
    ]
    assert output["job_resource"] == bound.job_resource
    assert output["service_identity"] == bound.service_identity
    assert output["image_digest"] == fx.payload["image_uri"].rsplit("@", 1)[1]
    assert re.fullmatch(origin.image_uri_regex, authority.image_uri) is not None


def test_exposure_source_carries_no_retained_identity():
    source = inspect.getsource(issuer_module())
    for principal in RETAINED_PRINCIPALS:
        assert principal not in source
    assert "trends-engine-oi-exposure-issuer-staging" not in source


@pytest.mark.parametrize(
    "change", ["retained_identity", "foreign_generation", "foreign_job", "v1_approval"]
)
def test_exposure_fresh_run_refuses_an_authority_off_the_active_origin(change):
    issuer = issuer_module()
    fx, authority, _consumption = consumed("collection_exposure_issue")
    if change == "retained_identity":
        supplied = retained_authority("collection_exposure_issue")
    else:
        supplied = SimpleNamespace(
            **{name: getattr(authority, name) for name in type(authority).__slots__},
        )
        if change == "foreign_generation":
            supplied.generation = SimpleNamespace(
                origin_registry_sha256=PAIR[0],
                resource_manifest_sha256=OTHER_SHA256,
                registry=authority.generation.registry,
            )
        elif change == "foreign_job":
            supplied.job_resource = RETAINED_IDENTITIES["collection_exposure_issue"][0]
        else:
            supplied = v1_approval_authority(authority)
    receipt = exposure_receipt(fx, supplied.manifest.service_identity)
    events = []
    with pytest.raises(issuer.ExposureRefusal, match="origin"):
        issuer._execute_durable_collection_exposure(
            receipts=(receipt,),
            query_runner=exposure_query_runner(receipt, events),
            authority_loader=lambda operation, **kwargs: supplied,
            consumption_writer=lambda request: events.append("consume") or None,
            result_writer=lambda request: events.append("result") or None,
        )
    assert events == []
    assert issuer._DURABLE_ARTIFACT_CONTEXT is None


def test_exposure_receipt_issued_under_the_retained_principal_refuses(monkeypatch):
    issuer = issuer_module()
    fx = fixture("collection_exposure_issue")
    authority = load(fx)
    receipt = exposure_receipt(fx, RETAINED_IDENTITIES["collection_exposure_issue"][1])
    rows = []
    events = []
    consumptions = []

    def writer(request):
        row = consumption_row(fx, request)
        consumptions.append(row)
        return row

    monkeypatch.setattr(
        execution_approval,
        "_default_result_reader",
        lambda consumption_id, **_kwargs: recorded_result(rows, consumption_id),
    )
    with pytest.raises(issuer.ExposureRefusal, match="durable execution authority"):
        issuer._execute_durable_collection_exposure(
            receipts=(receipt,),
            query_runner=exposure_query_runner(receipt, events),
            authority_loader=lambda operation, **kwargs: authority,
            consumption_writer=writer,
            result_writer=recording_writer(rows),
        )
    assert events == []
    assert len(rows) == 1
    assert len(consumptions) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["consumption_id"] == consumptions[0]["consumption_id"]
    assert (rows[0]["origin_registry_sha256"], rows[0]["resource_manifest_sha256"]) == PAIR


def test_exposure_failure_is_recorded_with_the_consumed_pair(monkeypatch):
    issuer = issuer_module()
    fx = fixture("collection_exposure_issue")
    authority = load(fx)
    _origin, bound = binding("collection_exposure_issue")
    receipt = exposure_receipt(fx, bound.service_identity)
    rows = []
    consumptions = []

    def writer(request):
        row = consumption_row(fx, request)
        consumptions.append(row)
        return row

    def fail_query(_sql):
        raise RuntimeError("private warehouse detail")

    monkeypatch.setattr(
        execution_approval,
        "_default_result_reader",
        lambda consumption_id, **_kwargs: recorded_result(rows, consumption_id),
    )
    with pytest.raises(RuntimeError, match="private warehouse detail"):
        issuer._execute_durable_collection_exposure(
            receipts=(receipt,),
            query_runner=fail_query,
            authority_loader=lambda operation, **kwargs: authority,
            consumption_writer=writer,
            result_writer=recording_writer(rows),
        )
    assert len(rows) == 1
    assert len(consumptions) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["consumption_id"] == consumptions[0]["consumption_id"]
    assert (rows[0]["origin_registry_sha256"], rows[0]["resource_manifest_sha256"]) == PAIR
    assert "private warehouse detail" not in rows[0]["canonical_result_json"]


@pytest.mark.parametrize("family", ["other_generation", "v1_record"])
def test_exposure_refuses_a_consumption_from_another_generation_or_family(family):
    issuer = issuer_module()
    fx = fixture("collection_exposure_issue")
    authority = load(fx)
    _origin, bound = binding("collection_exposure_issue")
    receipt = exposure_receipt(fx, bound.service_identity)
    writer = wrong_generation_row(fx) if family == "other_generation" else v1_family_row(fx)
    events = []
    with pytest.raises(execution_approval.ApprovalRefusal, match="schema_mismatch"):
        issuer._execute_durable_collection_exposure(
            receipts=(receipt,),
            query_runner=exposure_query_runner(receipt, events),
            authority_loader=lambda operation, **kwargs: authority,
            consumption_writer=writer,
            result_writer=lambda request: events.append("result") or None,
        )
    assert events == []


# brain_read


def brain_payload(cli, profile, *, invocation_arguments, annotations=None):
    task = {
        "containers": [
            {
                "image": profile.image_repository + "@sha256:" + "a" * 64,
                "command": ["python"],
                "args": ["scripts/staging/run_live_intelligence_brain.py", *invocation_arguments],
                "env": [
                    {"name": name, "value": value}
                    for name, value in execution_approval._DEFAULT_ENVIRONMENT
                ],
                "resources": {"limits": {"memory": "4Gi"}},
                "volumeMounts": [],
            }
        ],
        "serviceAccount": profile.service_identity,
        "maxRetries": 0,
        "timeout": "900s",
        "volumes": [],
    }
    values = {
        "42.ogilvy/contract-sha256": cli._CONTRACT_SHA256,
        "42.ogilvy/dependency-lock-sha256": "1" * 64,
        "42.ogilvy/runtime-sbom-sha256": "2" * 64,
        "42.ogilvy/source-sha": "b" * 40,
        "42.ogilvy/execution-approval-sha256": "d" * 64,
        "42.ogilvy/origin-registry-sha256": profile.origin_registry_sha256,
        "42.ogilvy/resource-manifest-sha256": profile.resource_manifest_sha256,
    }
    values.update(annotations or {})
    values["42.ogilvy/deployment-manifest-sha256"] = cli.canonical_digest(
        cli._deployment_manifest(
            annotations=values,
            task=cli._task_authority({"template": task}, execution=True),
            invocation_arguments=invocation_arguments,
            profile=profile,
        )
    )
    return {
        "execution": {
            "name": profile.job_resource + "/executions/exe-1",
            "job": profile.job_resource,
            "annotations": dict(values),
            "template": json.loads(json.dumps(task)),
        },
        "job": {
            "name": profile.job_resource,
            "template": {"annotations": dict(values), "template": json.loads(json.dumps(task))},
        },
    }


BRAIN_ARGUMENTS = (
    "--target",
    "staging",
    "--run-id",
    "run_20260913_brain",
    "--signal-id",
    "sig_" + "c" * 64,
    "--research-depth",
    "briefing",
)


def test_brain_profile_comes_from_the_active_origin_and_names_no_retained_identity():
    cli = brain_module()
    origin, bound = binding("brain_read")
    profile = cli._brain_profile()
    assert profile.job_resource == bound.job_resource
    assert profile.service_identity == bound.service_identity
    assert (
        profile.service_identity == "intelligence-42-brain@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert profile.connected_repo == origin.connected_repo
    assert profile.image_repository == origin.image_repository
    assert profile.image_uri_regex == origin.image_uri_regex
    assert (profile.origin_registry_sha256, profile.resource_manifest_sha256) == PAIR
    assert profile.manifest_version == MANIFEST_V2
    assert profile.secret_versions == ()
    assert set(cli._ANNOTATIONS.values()) == (
        execution_approval._BRAIN_DURABLE_ANNOTATIONS | execution_approval._GENERATION_ANNOTATIONS
    )
    source = inspect.getsource(cli)
    for principal in RETAINED_PRINCIPALS:
        assert principal not in source
    assert "trends-engine-oi-brain-staging" not in source


def test_brain_runtime_identity_binds_the_profile_job_principal_image_and_pair(monkeypatch):
    cli = brain_module()
    profile = cli._brain_profile()
    monkeypatch.setattr(cli, "_verify_runtime_files", lambda **_kwargs: None)
    payload = brain_payload(cli, profile, invocation_arguments=BRAIN_ARGUMENTS)
    identity = cli._verify_runtime_identity(
        payload=payload, invocation_arguments=BRAIN_ARGUMENTS, distributions=()
    )
    assert identity.source_sha == "b" * 40
    manifest = cli._deployment_manifest(
        annotations=payload["execution"]["annotations"],
        task=cli._task_authority(payload["execution"], execution=True),
        invocation_arguments=BRAIN_ARGUMENTS,
        profile=profile,
    )
    assert manifest["job_resource"] == profile.job_resource
    assert manifest["service_identity"] == profile.service_identity
    assert manifest["manifest_version"] == MANIFEST_V2
    assert manifest["secret_versions"] == ()
    assert manifest["operation"] == "brain_read"


@pytest.mark.parametrize(
    "change",
    ["retained_job", "retained_identity", "retained_image", "missing_pair", "foreign_pair"],
)
def test_brain_runtime_identity_refuses_the_retained_identity_or_another_generation(
    monkeypatch, change
):
    cli = brain_module()
    profile = cli._brain_profile()
    monkeypatch.setattr(cli, "_verify_runtime_files", lambda **_kwargs: None)
    job, identity = RETAINED_IDENTITIES["brain_read"]
    payload = brain_payload(cli, profile, invocation_arguments=BRAIN_ARGUMENTS)
    for view in (payload["execution"], payload["job"]):
        if view is payload["execution"]:
            annotations, template = view["annotations"], view["template"]
        else:
            annotations, template = view["template"]["annotations"], view["template"]["template"]
        if change == "retained_job":
            view["name"] = job + ("/executions/exe-1" if view is payload["execution"] else "")
            if view is payload["execution"]:
                view["job"] = job
        elif change == "retained_identity":
            template["serviceAccount"] = identity
        elif change == "retained_image":
            template["containers"][0]["image"] = (
                "us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:"
                + "a" * 64
            )
        elif change == "missing_pair":
            annotations.pop("42.ogilvy/origin-registry-sha256")
            annotations.pop("42.ogilvy/resource-manifest-sha256")
        else:
            annotations["42.ogilvy/resource-manifest-sha256"] = OTHER_SHA256
    with pytest.raises(cli.RuntimeIdentityRefusal):
        cli._verify_runtime_identity(
            payload=payload, invocation_arguments=BRAIN_ARGUMENTS, distributions=()
        )


def brain_run(cli, monkeypatch, authority, *, events, fail_read=False):
    fx_source = authority.source_sha
    receipt = SimpleNamespace(source_sha=fx_source)
    artifacts = {
        "brain_contract": b"brain",
        "run_receipt": b"receipt",
        "source_window_receipt_set": b"source-window",
    }
    inputs = cli.BrainControlInputs(receipt=receipt, artifacts=artifacts)
    calls = []
    monkeypatch.setattr(cli, "_read_brain_control_artifacts", lambda **_kwargs: inputs)

    def loader(operation, **kwargs):
        calls.append((operation, kwargs))
        kwargs["artifact_reader"]("brain_contract")
        events.append("load")
        return authority

    monkeypatch.setattr(cli.execution_approval, "_load_execution_authority", loader)
    read = SimpleNamespace(receipt=receipt, read_receipt={"run_receipt_verified": True})

    def read_authority(**_kwargs):
        events.append("evidence")
        if fail_read:
            raise cli.LiveBrainReadRefusal("readback_refused", "private evidence detail")
        return read

    monkeypatch.setattr(cli, "read_live_brain_authority", read_authority)
    monkeypatch.setattr(
        cli, "_issue_live_brain_runtime_request", lambda *_args, **_kwargs: SimpleNamespace()
    )
    snapshot = SimpleNamespace(snapshot_id="bsp_" + "1" * 64, snapshot_digest="2" * 64)
    monkeypatch.setattr(cli, "_issue_live_brain_evidence_snapshot", lambda *_args: snapshot)
    brain_result = SimpleNamespace(result_digest="3" * 64)
    monkeypatch.setattr(cli, "_run_intelligence_brain_from_authority", lambda *_args: brain_result)
    monkeypatch.setattr(
        cli,
        "_plain",
        lambda value: {"result_digest": value.result_digest} if value is brain_result else value,
    )
    return calls, partial(
        cli._execute,
        run_id="run_20260913_brain",
        signal_id="sig_" + "c" * 64,
        research_depth="briefing",
        invocation_arguments=BRAIN_ARGUMENTS,
        runtime_identity_verifier=lambda _args: (
            events.append("identity") or SimpleNamespace(source_sha=fx_source)
        ),
        bigquery_factory=lambda: object(),
    )


def test_brain_loader_receives_explicit_new_consume_mode(monkeypatch):
    cli = brain_module()
    _fx, authority, consumption = consumed("brain_read")
    events = []
    calls, execute = brain_run(cli, monkeypatch, authority, events=events)
    monkeypatch.setattr(
        cli.execution_approval, "_consume_execution_authority", lambda _a: consumption
    )
    rows = []
    monkeypatch.setattr(
        cli.execution_approval,
        "_record_execution_result",
        partial(
            execution_approval._record_execution_result,
            result_writer=recording_writer(rows),
            result_reader=lambda consumption_id: recorded_result(rows, consumption_id),
        ),
    )
    payload = execute()
    assert calls == [
        (
            "brain_read",
            {"mode": "new_consume", "artifact_reader": cli._execution_approval_artifact_bytes},
        )
    ]
    assert payload["execution_approval"]["consumption_id"] == consumption.consumption_id
    assert rows[0]["status"] == "succeeded"
    assert (rows[0]["origin_registry_sha256"], rows[0]["resource_manifest_sha256"]) == PAIR


@pytest.mark.parametrize(
    "change", ["retained_identity", "foreign_generation", "foreign_job", "v1_approval"]
)
def test_brain_fresh_run_refuses_an_authority_off_the_active_origin(monkeypatch, change):
    cli = brain_module()
    _fx, authority, consumption = consumed("brain_read")
    if change == "retained_identity":
        supplied = retained_authority("brain_read")
    else:
        supplied = SimpleNamespace(
            **{name: getattr(authority, name) for name in type(authority).__slots__},
        )
        if change == "foreign_generation":
            supplied.generation = SimpleNamespace(
                origin_registry_sha256=OTHER_SHA256,
                resource_manifest_sha256=PAIR[1],
                registry=authority.generation.registry,
            )
        elif change == "foreign_job":
            supplied.job_resource = RETAINED_IDENTITIES["brain_read"][0]
        else:
            supplied = v1_approval_authority(authority)
    events = []
    _calls, execute = brain_run(cli, monkeypatch, supplied, events=events)
    monkeypatch.setattr(
        cli.execution_approval,
        "_consume_execution_authority",
        lambda _a: events.append("consume") or consumption,
    )
    monkeypatch.setattr(
        cli.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: events.append("result"),
    )
    with pytest.raises(cli.RuntimeIdentityRefusal, match="origin"):
        execute()
    assert "consume" not in events
    assert "result" not in events
    assert "evidence" not in events
    assert cli._DURABLE_ARTIFACT_CONTEXT is None


def test_brain_failure_after_consumption_is_recorded_with_the_consumed_pair(monkeypatch):
    cli = brain_module()
    fx = fixture("brain_read")
    authority = load(fx)
    events = []
    consumptions = []

    def writer(request):
        row = consumption_row(fx, request)
        consumptions.append(row)
        return row

    _calls, execute = brain_run(cli, monkeypatch, authority, events=events, fail_read=True)
    monkeypatch.setattr(
        cli.execution_approval,
        "_consume_execution_authority",
        partial(execution_approval._consume_execution_authority, consumption_writer=writer),
    )
    rows = []
    monkeypatch.setattr(
        cli.execution_approval,
        "_record_execution_result",
        partial(
            execution_approval._record_execution_result,
            result_writer=recording_writer(rows),
            result_reader=lambda consumption_id: recorded_result(rows, consumption_id),
        ),
    )
    with pytest.raises(cli.LiveBrainReadRefusal):
        execute()
    assert len(rows) == 1
    assert len(consumptions) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["consumption_id"] == consumptions[0]["consumption_id"]
    assert (rows[0]["origin_registry_sha256"], rows[0]["resource_manifest_sha256"]) == PAIR
    failure = json.loads(rows[0]["canonical_result_json"])
    assert failure["status"] == "failed"
    assert failure["error_code"] == "readback_refused"
    assert "private evidence detail" not in rows[0]["canonical_result_json"]


@pytest.mark.parametrize("family", ["other_generation", "v1_record"])
def test_brain_refuses_a_consumption_from_another_generation_or_family(monkeypatch, family):
    cli = brain_module()
    fx = fixture("brain_read")
    authority = load(fx)
    writer = wrong_generation_row(fx) if family == "other_generation" else v1_family_row(fx)
    events = []
    _calls, execute = brain_run(cli, monkeypatch, authority, events=events)
    monkeypatch.setattr(
        cli.execution_approval,
        "_consume_execution_authority",
        partial(execution_approval._consume_execution_authority, consumption_writer=writer),
    )
    monkeypatch.setattr(
        cli.execution_approval,
        "_record_execution_result",
        lambda *_args, **_kwargs: events.append("result"),
    )
    with pytest.raises(cli.RuntimeIdentityRefusal, match="approval refused"):
        execute()
    assert "evidence" not in events
    assert "result" not in events


# wave1_pilot


def wave1_contract_artifact():
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SET_SHA256

    return json.dumps(
        {"contract_version": "wave1-pilot-durable-v1", "route_set_sha256": WAVE1_ROUTE_SET_SHA256}
    ).encode()


def wave1_entry(run, monkeypatch, authority, *, events, consume=None):
    from src.ingestion.connectors import gdelt

    artifacts = {
        "wave1_contract": wave1_contract_artifact(),
        "r3_seed_manifest": b'{"seed":1}',
        "gdelt_dry_run_set": b'{"receipts":[{"name":"events"},{"name":"gcam"}]}',
        "source_lab_snapshot": b"source-lab",
        "funded_preflight": b"preflight",
    }
    calls = []
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    monkeypatch.setattr(run, "bigquery", SimpleNamespace(Client=lambda **_kwargs: object()))
    monkeypatch.setattr(run, "_build_wave1_execution_artifacts", lambda **_kwargs: artifacts)
    # The execution credential the entry checks before it consumes: the registry identity.
    funded = SimpleNamespace(
        _credentials=SimpleNamespace(
            service_account_email=binding("wave1_pilot")[1].service_identity
        )
    )
    monkeypatch.setattr(run, "_wave1_result_client", lambda: funded)

    def loader(operation, **kwargs):
        calls.append((operation, dict(kwargs.items())))
        for name in artifacts:
            kwargs["artifact_reader"](name)
        events.append("load")
        return authority

    monkeypatch.setattr(run.execution_approval, "_load_execution_authority", loader)
    if consume is not None:
        monkeypatch.setattr(run.execution_approval, "_consume_execution_authority", consume)
    monkeypatch.setattr(
        run, "_wave1_requests_from_seed_manifest", lambda _payload: events.append("requests") or ()
    )
    monkeypatch.setattr(
        gdelt, "_dry_run_receipt_from_mapping", lambda item: SimpleNamespace(name=item["name"])
    )
    monkeypatch.setattr(
        gdelt,
        "bind_wave1_gdelt_manifest_entry",
        lambda _capability, _artifact, name: events.append(f"gdelt:{name}") or name,
    )
    monkeypatch.setattr(run, "_ACTIVE_WAVE1_EXECUTION", None)
    return calls, partial(
        run._begin_wave1_durable_execution, "run_wave1", datetime(2026, 9, 13, 12, tzinfo=UTC)
    )


def test_wave1_loader_receives_explicit_new_consume_mode_and_binds_the_origin_job(monkeypatch):
    run = pilot_module()
    fx = fixture("wave1_pilot")
    authority = load(fx)
    origin, bound = binding("wave1_pilot")
    events = []
    calls, begin = wave1_entry(
        run,
        monkeypatch,
        authority,
        events=events,
        consume=partial(
            execution_approval._consume_execution_authority,
            consumption_writer=lambda request: consumption_row(fx, request),
        ),
    )
    try:
        execution = begin()
        assert calls[0][0] == "wave1_pilot"
        assert calls[0][1]["mode"] == "new_consume"
        assert set(calls[0][1]) == {"mode", "artifact_reader"}
        assert type(execution.consumption) is execution_approval.ExecutionConsumptionV2
        assert execution.consumption.job_resource == bound.job_resource
        assert execution.capability.execution_name.startswith(bound.job_resource + "/executions/")
        assert re.fullmatch(origin.image_uri_regex, execution.capability.image_uri) is not None
        assert (authority, execution.consumption, "run_wave1") == run._ACTIVE_WAVE1_EXECUTION
    finally:
        run._ACTIVE_WAVE1_EXECUTION = None


def test_wave1_source_carries_no_retained_identity():
    source = inspect.getsource(pilot_module())
    for principal in RETAINED_PRINCIPALS:
        assert principal not in source
    assert "trends-engine-open-intelligence-staging" not in source


@pytest.mark.parametrize(
    "change", ["retained_identity", "foreign_generation", "foreign_job", "v1_approval"]
)
def test_wave1_fresh_run_refuses_an_authority_off_the_active_origin(monkeypatch, change):
    run = pilot_module()
    _fx, authority, consumption = consumed("wave1_pilot")
    if change == "retained_identity":
        supplied = retained_authority("wave1_pilot")
    else:
        supplied = SimpleNamespace(
            **{name: getattr(authority, name) for name in type(authority).__slots__},
        )
        if change == "foreign_generation":
            supplied.generation = SimpleNamespace(
                origin_registry_sha256=OTHER_SHA256,
                resource_manifest_sha256=PAIR[1],
                registry=authority.generation.registry,
            )
        elif change == "foreign_job":
            supplied.job_resource = RETAINED_IDENTITIES["wave1_pilot"][0]
        else:
            supplied = v1_approval_authority(authority)
    events = []
    _calls, begin = wave1_entry(
        run,
        monkeypatch,
        supplied,
        events=events,
        consume=lambda _authority: events.append("consume") or consumption,
    )
    try:
        with pytest.raises(RuntimeError, match="origin"):
            begin()
        assert "consume" not in events
        assert run._ACTIVE_WAVE1_EXECUTION is None
        assert run._DURABLE_WAVE1_ARTIFACT_CONTEXT is None
    finally:
        run._ACTIVE_WAVE1_EXECUTION = None


def test_wave1_failure_after_consumption_is_recorded_with_the_consumed_pair(monkeypatch):
    run = pilot_module()
    _fx, authority, consumption = consumed("wave1_pilot")
    rows = []

    def fail_run(**_kwargs):
        run._ACTIVE_WAVE1_EXECUTION = (authority, consumption, "run_wave1")
        raise RuntimeError("private pipeline detail")

    monkeypatch.setattr(run, "_run_impl", fail_run)
    monkeypatch.setattr("src.observability.events.insert_dataframe", lambda *_args: None)
    monkeypatch.setattr(
        run.execution_approval,
        "_record_execution_result",
        partial(
            execution_approval._record_execution_result,
            result_writer=recording_writer(rows),
            result_reader=lambda consumption_id: recorded_result(rows, consumption_id),
        ),
    )
    with pytest.raises(RuntimeError, match="private pipeline detail"):
        run.run()
    assert_failure_recorded(rows, consumption)
    assert rows[0]["result_reference"] == authority.execution_name + "#wave1-pilot-failed"
    assert "private pipeline detail" not in rows[0]["canonical_result_json"]
    assert run._ACTIVE_WAVE1_EXECUTION is None


@pytest.mark.parametrize("family", ["other_generation", "v1_record"])
def test_wave1_refuses_a_consumption_from_another_generation_or_family(monkeypatch, family):
    run = pilot_module()
    fx = fixture("wave1_pilot")
    authority = load(fx)
    writer = wrong_generation_row(fx) if family == "other_generation" else v1_family_row(fx)
    events = []
    _calls, begin = wave1_entry(
        run,
        monkeypatch,
        authority,
        events=events,
        consume=partial(execution_approval._consume_execution_authority, consumption_writer=writer),
    )
    try:
        with pytest.raises(execution_approval.ApprovalRefusal, match="schema_mismatch"):
            begin()
        assert "requests" not in events
        assert run._ACTIVE_WAVE1_EXECUTION is None
    finally:
        run._ACTIVE_WAVE1_EXECUTION = None


# Wave 1 capability consumer


def test_capability_accepts_the_v2_consumption_bound_to_its_authority():
    _fx, authority, consumption = consumed("wave1_pilot")
    _origin, bound = binding("wave1_pilot")
    capability = funded_lane._issue_wave1_execution_capability(authority, consumption)
    assert funded_lane._require_wave1_execution_capability(capability, action="x") is capability
    assert capability.consumption_id == consumption.consumption_id
    assert capability.manifest_sha256 == authority.approval.manifest_sha256
    assert capability.execution_name == authority.execution_name
    assert capability.execution_name.startswith(bound.job_resource + "/executions/")
    assert capability.image_uri == authority.image_uri
    assert capability.maximum_bytes_billed == 50_000_000_000


def forged_consumption(consumption, **changes):
    forged = object.__new__(execution_approval.ExecutionConsumptionV2)
    for name in type(consumption).__slots__:
        object.__setattr__(forged, name, changes.get(name, getattr(consumption, name)))
    return forged


def test_capability_refuses_a_consumption_from_another_generation_family_or_operation():
    _fx, authority, consumption = consumed("wave1_pilot")
    cases = (
        ("generation", forged_consumption(consumption, origin_registry_sha256=OTHER_SHA256)),
        ("generation", forged_consumption(consumption, resource_manifest_sha256=OTHER_SHA256)),
        (
            "family",
            forged_consumption(
                consumption,
                consumption_contract_version="open_intelligence_execution_consumption_v1",
            ),
        ),
        ("operation", forged_consumption(consumption, operation="r3_apply")),
        (
            "job",
            forged_consumption(consumption, job_resource=RETAINED_IDENTITIES["wave1_pilot"][0]),
        ),
        ("approval", forged_consumption(consumption, approval_id="exa_" + "0" * 64)),
        ("manifest", forged_consumption(consumption, manifest_sha256="0" * 64)),
        (
            "execution",
            forged_consumption(
                consumption, execution_name=authority.job_resource + "/executions/other"
            ),
        ),
    )
    for name, candidate in cases:
        with pytest.raises(funded_lane.Wave1ExecutionBlocked, match=name):
            funded_lane._issue_wave1_execution_capability(authority, candidate)
    from tests.unit.test_wave1_durable_capability import _consumption

    with pytest.raises(funded_lane.Wave1ExecutionBlocked, match="family"):
        funded_lane._issue_wave1_execution_capability(authority, _consumption())
    other_authority = load(fixture("wave1_pilot"))
    with pytest.raises(funded_lane.Wave1ExecutionBlocked):
        funded_lane._issue_wave1_execution_capability(other_authority, consumption)
    assert funded_lane._require_wave1_execution_capability(
        funded_lane._issue_wave1_execution_capability(authority, consumption), action="x"
    )


def test_capability_retained_v1_job_is_unreachable_from_a_fresh_authority():
    source = inspect.getsource(funded_lane)
    assert "ExecutionConsumptionV2" in source
    _fx, authority, consumption = consumed("wave1_pilot")
    from tests.unit.test_wave1_durable_capability import _consumption

    retained = _consumption()
    assert retained.job_resource == funded_lane._RETAINED_WAVE1_JOB
    with pytest.raises(funded_lane.Wave1ExecutionBlocked, match="family"):
        funded_lane._issue_wave1_execution_capability(authority, retained)
    with pytest.raises(funded_lane.Wave1ExecutionBlocked, match="job"):
        funded_lane._issue_wave1_execution_capability(
            authority, forged_consumption(consumption, job_resource=retained.job_resource)
        )
