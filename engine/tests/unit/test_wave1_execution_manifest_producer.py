"""The owner run Wave 1 manifest producer and the job loader that checks it.

Every native client is a fake behind an injection seam: BigQuery is an in memory
table state, Cloud Build is a describe payload, git is a recorded runner. The job side
is the real loader fed by the job's own artifact provider over the same fake tables.
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from scripts import run_rss_now as run
from scripts.staging import approve_open_intelligence_execution as approval_cli
from scripts.staging import deploy_open_intelligence_job as deployer
from scripts.staging import produce_wave1_execution_manifest as producer
from scripts.staging import render_open_intelligence_execution_package as renderer
from src.analysis.open_intelligence import execution_approval, execution_generations
from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

from tests.unit.test_execution_runtime_v2 import (
    APPROVED_BY,
    REGISTRY_SHA256,
    RESOURCE_SHA256,
    context,
    phrase_sha,
)

SOURCE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
BUILD_ID = "11111111-1111-4111-8111-111111111111"
BUILD_RESOURCE = f"projects/ogilvy-trends-v2/locations/us-central1/builds/{BUILD_ID}"
JOB_RESOURCE = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-funded-pilot-staging"
)
PRODUCED_AT = datetime(2026, 9, 23, 8, 0, 0, 123456, tzinfo=UTC)
APPROVED_AT = datetime(2026, 9, 23, 8, 20, tzinfo=UTC)
JOB_STARTED_AT = datetime(2026, 9, 23, 9, 5, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))


def generation():
    return execution_generations.active_generation()


def origin():
    return execution_approval._v2_origin_for_operation(
        "wave1_pilot", mode="new_approval", registry=generation().registry
    )


def build_describe(*, source_sha=SOURCE_SHA, image_digest=IMAGE_DIGEST):
    selected = origin()
    return {
        "name": BUILD_RESOURCE,
        "id": BUILD_ID,
        "projectId": "ogilvy-trends-v2",
        "status": "SUCCESS",
        "results": {
            "images": [
                {"name": f"{selected.image_repository}:{source_sha}", "digest": image_digest}
            ]
        },
        "finishTime": "2026-09-23T07:30:00.000000Z",
        "source": {
            "connectedRepository": {"repository": selected.connected_repo, "revision": source_sha}
        },
    }


def tables():
    """The live staging rows the Wave 1 builders read, one entry per queried table."""
    return {
        "ledger": {
            "monthly_ledger_debit": Decimal("12.000000000"),
            "monthly_vendor_reported": Decimal("12.000000000"),
            "month_opening_balance": Decimal("250100.000000000"),
            "unreconciled_execution_ids": [],
            "consecutive_complete_runs": 1,
            "runs_today": 0,
        },
        "seed": {
            "replacement_run_id": "run_20260903_dynamic_apply_v2_r16",
            "row_set_digest": "e" * 64,
            "source_sha": "f" * 40,
            "tiktok_music_identifiers": [
                {"identifier_type": "music_id", "value": "song-1", "market": "za"}
            ],
            "reddit_urls": [{"url": "https://reddit.invalid/post", "market": "ke"}],
            "instagram_search_terms": [{"term": "culture", "market": "ng"}],
            "youtube_short_urls": [{"url": "https://youtube.invalid/short", "market": "za"}],
        },
        "source_lab": [
            {
                "route_path": f"/v1/{route}",
                "status": "inventory_only",
                "last_checked_at": "2026-09-22T06:00:00Z",
                "calls": 0,
            }
            for route in sorted(WAVE1_ROUTE_SPECS)
        ],
    }


@dataclass
class _Job:
    rows: tuple

    def result(self, **_kwargs):
        return self.rows


@dataclass
class FakeBigQuery:
    """Answers the three Wave 1 read queries from an in memory table state."""

    state: dict
    project: str = "ogilvy-trends-v2"
    location: str = "US"
    statements: list = field(default_factory=list)

    def query(self, sql, job_config=None, **kwargs):
        self.statements.append(sql)
        if "socialcrawl_credit_ledger_v1" in sql:
            return _Job((dict(self.state["ledger"]),))
        if "open_intelligence_run_receipts_v1" in sql:
            return _Job((copy.deepcopy(self.state["seed"]),))
        if "v_source_lab_v2" in sql:
            return _Job(tuple({"row_json": json.dumps(row)} for row in self.state["source_lab"]))
        pytest.fail(f"unexpected query: {sql[:80]}")


def git_runner(head=SOURCE_SHA, status=""):
    calls = []

    def runner(command):
        calls.append(command)
        if command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, stdout=status, stderr="")
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout=head + "\n", stderr="")
        pytest.fail(f"unexpected command: {command}")

    runner.calls = calls
    return runner


def produce(tmp_path, state=None, *, clock=PRODUCED_AT, describe=None, runner=None, argv=None):
    output = (tmp_path / "wave1-manifest.json").resolve()
    client = FakeBigQuery(tables() if state is None else state)
    code = producer.main(
        ["--build-resource", BUILD_RESOURCE, "--output-file", str(output)]
        if argv is None
        else argv,
        build_reader=lambda name: build_describe() if describe is None else describe,
        client_factory=lambda: client,
        clock=lambda: clock,
        runner=git_runner() if runner is None else runner,
    )
    return code, output, client


def approved_job(manifest_bytes: bytes):
    """The approval row and the deployed job the approve CLI and deploy would leave."""
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    payload = json.loads(manifest_bytes)
    expires_at = datetime.strptime(payload["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=UTC
    )
    approval = execution_approval.ExecutionApprovalV2(
        approval_contract_version="open_intelligence_execution_approval_v2",
        approval_id=execution_approval.approval_id_v2(
            digest,
            APPROVED_BY,
            APPROVED_AT,
            origin_registry_sha256=REGISTRY_SHA256,
            resource_manifest_sha256=RESOURCE_SHA256,
        ),
        manifest_version=payload["manifest_version"],
        operation="wave1_pilot",
        contract_sha256=payload["contract_sha256"],
        manifest_sha256=digest,
        canonical_manifest_json=manifest_bytes.decode(),
        approved_by=APPROVED_BY,
        approved_at=APPROVED_AT,
        expires_at=expires_at,
        approval_phrase_sha256=phrase_sha("wave1_pilot", digest),
        origin_registry_sha256=REGISTRY_SHA256,
        resource_manifest_sha256=RESOURCE_SHA256,
        **context(),
    )
    env = list(payload["environment"]) + [
        {"name": name, "valueSource": {"secretKeyRef": {"secret": name, "version": "1"}}}
        for name in payload["secrets"]
    ]
    task = {
        "serviceAccount": payload["service_identity"],
        "maxRetries": 0,
        "timeout": f"{payload['timeout_seconds']}s",
        "containers": [
            {
                "image": payload["image_uri"],
                "command": payload["command"],
                "args": payload["arguments"],
                "env": env,
            }
        ],
    }
    annotations = {
        "42.ogilvy/execution-approval-sha256": digest,
        "42.ogilvy/source-sha": payload["source_sha"],
        "42.ogilvy/origin-registry-sha256": REGISTRY_SHA256,
        "42.ogilvy/resource-manifest-sha256": RESOURCE_SHA256,
    }
    execution = {
        "name": payload["job_resource"] + "/executions/pilot-1",
        "job": payload["job_resource"],
        "annotations": dict(annotations),
        "template": copy.deepcopy(task),
    }
    job = {
        "name": payload["job_resource"],
        "template": {"annotations": dict(annotations), "template": copy.deepcopy(task)},
    }
    return approval, execution, job


def job_load(manifest_bytes, state, *, started_at=JOB_STARTED_AT, now=None):
    """What run_rss_now._begin_wave1_durable_execution does before consumption."""
    approval, execution, job = approved_job(manifest_bytes)
    provider = run._Wave1ArtifactProvider(FakeBigQuery(state), started_at)
    authority = execution_approval._load_execution_authority(
        "wave1_pilot",
        mode="new_consume",
        execution_reader=lambda: {"execution": execution, "job": job},
        approval_reader=lambda digest: approval,
        build_reader=lambda name: build_describe(),
        artifact_reader=provider.read,
        now=lambda: started_at if now is None else now,
    )
    return authority, provider


def refusal(code):
    return pytest.raises(
        (execution_approval.ApprovalRefusal, execution_approval.OriginRefusal),
        match=f"^{code}$",
    )


# the manifest the job accepts


def test_the_job_loader_admits_the_produced_manifest_over_unchanged_tables(tmp_path, capsys):
    code, output, _client = produce(tmp_path)
    assert code == 0, capsys.readouterr().err
    receipt = json.loads(capsys.readouterr().out)
    raw = output.read_bytes()
    assert receipt["manifest_sha256"] == hashlib.sha256(raw).hexdigest()

    authority, provider = job_load(raw, tables())

    assert authority.operation == "wave1_pilot"
    assert authority.job_resource == JOB_RESOURCE
    assert authority.manifest.service_identity == (
        "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert authority.approval.manifest_sha256 == receipt["manifest_sha256"]
    rebuilt = provider.inputs.artifacts
    pinned = dict(authority.manifest.input_artifacts)
    assert {name: hashlib.sha256(value).hexdigest() for name, value in rebuilt.items()} == {
        name: digest for name, digest in pinned.items() if name != "build_provenance"
    }
    assert receipt["input_artifacts"] == pinned


def test_the_manifest_carries_the_origin_binding_limits_and_the_build_it_names(tmp_path, capsys):
    code, output, _client = produce(tmp_path)
    assert code == 0, capsys.readouterr().err
    payload = json.loads(output.read_bytes())
    selected = origin()
    assert payload["operation"] == "wave1_pilot"
    assert payload["manifest_version"] == "open_intelligence_execution_manifest_v2"
    assert payload["contract_sha256"] == selected.contract_sha256
    assert payload["job_resource"] == JOB_RESOURCE
    assert payload["datasets"] == ["trends_v2_staging", "trends_v2_staging_funded"]
    assert payload["arguments"] == ["scripts/run_rss_now.py"]
    assert payload["secrets"] == ["SOCIALCRAWL_OGILVY_API_KEY"]
    assert payload["limits"] == {
        "max_bytes_billed": 50_000_000_000,
        "max_credits": 63,
        "max_model_calls": 0,
        "max_rows_written": 100,
    }
    assert payload["max_retries"] == 0
    # The legacy run_rss_now job ran with a three hour task timeout, the policy maximum.
    assert payload["timeout_seconds"] == 10800
    assert payload["build_resource"] == BUILD_RESOURCE
    assert payload["source_sha"] == SOURCE_SHA
    assert payload["image_uri"] == f"{selected.image_repository}@{IMAGE_DIGEST}"
    # Recomputed here from the describe payload, never read from the manifest.
    receipt = execution_approval.build_provenance_from_response(
        build_describe(),
        selected.contract_sha256,
        manifest_version=selected.manifest_version,
        mode="new_consume",
        registry=generation().registry,
    )
    expected = hashlib.sha256(
        execution_approval.canonical_build_provenance_bytes(receipt, origin=selected)
    ).hexdigest()
    pinned = {item["name"]: item["sha256"] for item in payload["input_artifacts"]}
    assert pinned["build_provenance"] == expected


# drift between producer time and job time


def _drift(change):
    state = tables()
    if change == "ledger_debit":
        state["ledger"]["monthly_ledger_debit"] = Decimal("13")
        state["ledger"]["monthly_vendor_reported"] = Decimal("13")
    elif change == "run_closed_today":
        state["ledger"]["runs_today"] = 1
        state["ledger"]["consecutive_complete_runs"] = 2
    elif change == "source_lab_row":
        state["source_lab"][0]["last_checked_at"] = "2026-09-23T08:30:00Z"
    elif change == "seed_graph":
        state["seed"]["tiktok_music_identifiers"] = [
            {"identifier_type": "music_id", "value": "song-2", "market": "za"}
        ]
    else:
        raise AssertionError(change)
    return state


@pytest.mark.parametrize(
    "change", ["ledger_debit", "run_closed_today", "source_lab_row", "seed_graph"]
)
def test_the_job_refuses_when_a_live_table_changes_after_production(tmp_path, change, capsys):
    code, output, _client = produce(tmp_path)
    assert code == 0, capsys.readouterr().err
    with refusal("execution_approval_artifact_mismatch"):
        job_load(output.read_bytes(), _drift(change))


def test_the_approval_expires_with_the_utc_day_the_preflight_describes(tmp_path, capsys):
    code, output, _client = produce(tmp_path)
    assert code == 0, capsys.readouterr().err
    payload = json.loads(output.read_bytes())
    assert payload["expires_at"] == "2026-09-23T23:59:59.999999Z"
    next_day = datetime(2026, 9, 24, 0, 5, tzinfo=UTC)
    with refusal("execution_approval_execution_mismatch"):
        job_load(output.read_bytes(), tables(), started_at=next_day)


def test_the_producer_refuses_a_window_shorter_than_one_hour(tmp_path, capsys):
    late = datetime(2026, 9, 23, 23, 1, tzinfo=UTC)
    code, output, _client = produce(tmp_path, clock=late)
    assert code == 1
    assert json.loads(capsys.readouterr().err) == {"error": "wave1_manifest_window_too_short"}
    assert not output.exists()


def test_the_producer_refuses_a_blocked_funded_preflight_and_writes_nothing(tmp_path, capsys):
    state = tables()
    # 250100 opening less 25101 spent leaves 224999, under the 225000 credit floor.
    state["ledger"]["monthly_ledger_debit"] = Decimal("25101")
    state["ledger"]["monthly_vendor_reported"] = Decimal("25101")
    code, output, _client = produce(tmp_path, state)
    assert code == 1
    assert json.loads(capsys.readouterr().err) == {"error": "wave1_manifest_artifact_unavailable"}
    assert not output.exists()


# rule 6: nothing that proves the manifest comes from its caller


@pytest.mark.parametrize(
    "extra",
    [
        ["--source-sha", SOURCE_SHA],
        ["--image-uri", "us-central1-docker.pkg.dev/x@" + IMAGE_DIGEST],
        ["--input-artifact", "funded_preflight=" + "0" * 64],
        ["--expires-at", "2026-09-23T12:00:00.000000Z"],
    ],
)
def test_the_cli_takes_only_the_build_and_the_output_path(tmp_path, extra, capsys):
    output = (tmp_path / "wave1-manifest.json").resolve()
    argv = ["--build-resource", BUILD_RESOURCE, "--output-file", str(output), *extra]
    code, _output, client = produce(tmp_path, argv=argv)
    assert code == 2
    assert json.loads(capsys.readouterr().err) == {"error": "wave1_manifest_cli_invalid"}
    assert client.statements == []
    assert not output.exists()


def test_the_cli_refuses_a_relative_output_or_a_build_outside_the_project(tmp_path, capsys):
    for argv in (
        ["--build-resource", BUILD_RESOURCE, "--output-file", "wave1-manifest.json"],
        [
            "--build-resource",
            "projects/other/locations/us-central1/builds/" + BUILD_ID,
            "--output-file",
            str((tmp_path / "m.json").resolve()),
        ],
    ):
        code, _output, client = produce(tmp_path, argv=argv)
        assert code == 2
        assert client.statements == []
    capsys.readouterr()


def test_the_artifact_digests_come_from_the_job_builder_over_the_job_client(
    tmp_path, monkeypatch, capsys
):
    seen = {}
    marker = object()
    artifacts = {
        "funded_preflight": b'{"a":1}',
        "gdelt_dry_run_set": b'{"b":2}',
        "r3_seed_manifest": b'{"c":3}',
        "source_lab_snapshot": b'{"d":4}',
        "wave1_contract": json.dumps({"max_credits": 63}).encode(),
    }

    def builder(*, client, observed_at):
        seen["client"] = client
        seen["observed_at"] = observed_at
        return dict(artifacts)

    monkeypatch.setattr(run, "_build_wave1_execution_artifacts", builder)
    monkeypatch.setattr(run, "_wave1_artifact_client", lambda: marker)
    output = (tmp_path / "wave1-manifest.json").resolve()
    code = producer.main(
        ["--build-resource", BUILD_RESOURCE, "--output-file", str(output)],
        build_reader=lambda name: build_describe(),
        clock=lambda: PRODUCED_AT,
        runner=git_runner(),
    )
    assert code == 0, capsys.readouterr().err
    assert seen == {"client": marker, "observed_at": PRODUCED_AT}
    pinned = {
        item["name"]: item["sha256"] for item in json.loads(output.read_bytes())["input_artifacts"]
    }
    for name, value in artifacts.items():
        assert pinned[name] == hashlib.sha256(value).hexdigest()


def test_the_job_itself_reads_through_the_shared_client_factory(monkeypatch):
    clients = []

    def factory():
        clients.append(object())
        return clients[-1]

    monkeypatch.setattr(run, "_wave1_artifact_client", factory)
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("SOCIALCRAWL_FUNDED_STAGE_NAME", "stage_1_wave_1")
    seen = {}

    def loader(operation, *, mode, artifact_reader):
        seen["client"] = artifact_reader.__self__.client
        raise execution_approval.ApprovalRefusal("execution_approval_unavailable")

    monkeypatch.setattr(execution_approval, "_load_execution_authority", loader)
    with refusal("execution_approval_unavailable"):
        run._begin_wave1_durable_execution("run_1", JOB_STARTED_AT)
    assert seen["client"] is clients[0]


def test_the_producer_is_read_only_and_repeatable(tmp_path, capsys):
    code, output, client = produce(tmp_path)
    assert code == 0, capsys.readouterr().err
    assert len(client.statements) == 3
    for sql in client.statements:
        head = sql.lstrip().split(None, 1)[0].upper()
        assert head in {"SELECT", "WITH"}
        for verb in ("INSERT", "MERGE", "UPDATE ", "DELETE", "CALL ", "CREATE", "DROP"):
            assert verb not in sql.upper()
    second = tmp_path / "again"
    second.mkdir()
    code, again, _client = produce(second)
    assert code == 0
    assert again.read_bytes() == output.read_bytes()
    capsys.readouterr()


def test_the_producer_refuses_a_local_tree_other_than_the_build_source(tmp_path, capsys):
    for runner in (git_runner(head="c" * 40), git_runner(status=" M engine/x.py\n")):
        code, output, client = produce(tmp_path, runner=runner)
        assert code == 1
        assert json.loads(capsys.readouterr().err) == {"error": "wave1_manifest_source_mismatch"}
        assert client.statements == []
        assert not output.exists()


def test_the_producer_refuses_a_build_that_is_not_the_connected_source(tmp_path, capsys):
    describe = build_describe()
    del describe["source"]
    code, output, client = produce(tmp_path, describe=describe)
    assert code == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": "execution_approval_build_provenance_invalid"
    }
    assert client.statements == []
    assert not output.exists()


def test_the_producer_never_overwrites_an_existing_manifest(tmp_path, capsys):
    output = (tmp_path / "wave1-manifest.json").resolve()
    output.write_bytes(b"kept")
    code, _output, client = produce(tmp_path)
    assert code == 1
    assert json.loads(capsys.readouterr().err) == {"error": "execution_approval_output_exists"}
    assert output.read_bytes() == b"kept"
    assert client.statements == []


# the consumers take the produced bytes unchanged


def test_renderer_review_and_deploy_admit_the_produced_bytes(tmp_path, capsys):
    code, output, _client = produce(tmp_path)
    assert code == 0, capsys.readouterr().err
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    active = generation()

    _payload, canonical, mode = renderer._load_manifest(output, digest, generation=active)
    assert (canonical, mode) == (output.read_bytes(), "new_approval")

    review = approval_cli.review_manifest(output, digest)
    assert review["manifest_sha256"] == digest
    assert review["operation"] == "wave1_pilot"

    manifest = deployer.load_operation_execution_manifest(
        output, digest, "wave1_pilot", generation=active
    )
    desired = deployer.desired_operation_job_from_execution_manifest(
        manifest,
        operation="wave1_pilot",
        execution_approval_sha256=digest,
        dependency_lock_sha256="c" * 64,
        runtime_sbom_sha256="d" * 64,
        origin=deployer.origin_for_manifest(manifest, generation=active),
        generation=active,
    )
    assert desired.job == "intelligence-42-funded-pilot-staging"
    assert dict(desired.annotations)["42.ogilvy/execution-approval-sha256"] == digest
    assert desired.timeout_seconds == 10800


def test_the_producer_source_carries_no_query_or_artifact_builder_of_its_own():
    source = Path(producer.__file__).read_text(encoding="utf-8")
    for token in ("SELECT", "client.query", "canonical_bytes(", "read_monthly_funded_lane"):
        assert token not in source
    assert "_build_wave1_execution_artifacts" in source
    assert "canonical_build_provenance_bytes" in source
    assert timedelta(hours=1) == producer.MINIMUM_WINDOW


@pytest.mark.parametrize("field", ["image_uri", "source_sha"])
def test_the_job_refuses_a_manifest_whose_image_or_source_is_not_the_build_it_pins(
    tmp_path, field, capsys
):
    """The build_provenance digest pins the build, so the manifest's own image and source
    must be that build's; a manifest carrying another image with the same pinned digest
    is refused at run time, not trusted because the approver signed both."""
    code, output, _client = produce(tmp_path)
    assert code == 0, capsys.readouterr().err
    payload = json.loads(output.read_bytes())
    if field == "image_uri":
        payload["image_uri"] = payload["image_uri"][:-64] + "c" * 64
    else:
        payload["source_sha"] = "c" * 40
    forged = execution_approval.canonical_manifest_bytes(
        payload, mode="new_consume", registry=generation().registry
    )
    with refusal("execution_approval_artifact_mismatch"):
        job_load(forged, tables())
