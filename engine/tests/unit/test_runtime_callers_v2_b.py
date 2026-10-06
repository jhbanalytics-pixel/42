"""R3 apply, proof and release consume v2 authority; protected capture refuses a fresh run."""

from __future__ import annotations

import functools
import hashlib
import inspect
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from scripts.staging import capture_protected_production_snapshot as capture
from scripts.staging import issue_r3_execution_proof as issuer
from scripts.staging import release_open_intelligence_run as release
from scripts.staging import replay_open_intelligence as replay
from src.analysis.open_intelligence import execution_approval, execution_generations
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.execution_origins import OriginRefusal
from src.analysis.open_intelligence.run_receipts import build_run_receipt, run_receipt_digest

from tests.unit.test_execution_manifest_origins import IMAGE_URI, SOURCE_SHA, manifest
from tests.unit.test_open_intelligence_release import _receipt_fields

V1 = "open_intelligence_execution_manifest_v1"
V2 = "open_intelligence_execution_manifest_v2"
RESULT_V1 = execution_approval._RESULT_VERSION
RESULT_V2 = execution_approval._RESULT_VERSION_V2
ACTIVE = execution_generations.ACTIVE_GENERATION_PAIR
GENERATION = execution_generations.active_generation()
REGISTRY = GENERATION.registry
FOREIGN_PAIR = ("1" * 64, "2" * 64)
OLD_APPLY_JOB = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-apply-staging"
)
OLD_APPLY_IDENTITY = "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com"


def origin_row(operation, manifest_version=V2):
    rows = [
        origin
        for origin in REGISTRY.values()
        if origin.manifest_version == manifest_version and operation in origin.operation_bindings
    ]
    assert len(rows) == 1
    return rows[0]


def binding(operation, manifest_version=V2):
    return origin_row(operation, manifest_version).operation_bindings[operation]


def foreign_generation():
    return SimpleNamespace(
        origin_registry_sha256=FOREIGN_PAIR[0],
        resource_manifest_sha256=FOREIGN_PAIR[1],
        registry=REGISTRY,
    )


def authority(operation, *, job=None, identity=None, image=IMAGE_URI, generation=GENERATION):
    bound = binding(operation)
    job = bound.job_resource if job is None else job
    identity = bound.service_identity if identity is None else identity
    return SimpleNamespace(
        operation=operation,
        manifest=SimpleNamespace(
            job_resource=job,
            service_identity=identity,
            image_uri=image,
            command=("python",),
            arguments=("scripts/staging/replay_open_intelligence.py",),
            input_artifacts=(("config", "3" * 64),),
            contract_sha256=origin_row(operation).contract_sha256,
        ),
        approval=SimpleNamespace(
            manifest_sha256="d" * 64,
            approval_id="exa_" + "e" * 64,
            approved_at=datetime(2026, 9, 13, 12, tzinfo=UTC),
        ),
        execution_name=job + "/executions/exe-1",
        job_resource=job,
        source_sha=SOURCE_SHA,
        image_uri=image,
        generation=generation,
    )


def consumption(pair=ACTIVE):
    return SimpleNamespace(
        consumption_id="exc_" + "f" * 64,
        origin_registry_sha256=pair[0],
        resource_manifest_sha256=pair[1],
    )


def chain_row(operation, payload, *, pair=ACTIVE, job=None, mode="historical_read"):
    manifest_payload = manifest(operation)
    if job is not None:
        manifest_payload["job_resource"] = job
    canonical_manifest = canonical_bytes(manifest_payload).decode()
    result_json = canonical_bytes(payload).decode()
    try:
        manifest_sha = execution_approval.manifest_sha256(
            manifest_payload, mode=mode, registry=REGISTRY
        )
    except (execution_approval.ApprovalRefusal, OriginRefusal):
        manifest_sha = hashlib.sha256(canonical_manifest.encode()).hexdigest()
    return {
        "approval_id": "exa_" + "a" * 64,
        "manifest_sha256": manifest_sha,
        "canonical_manifest_json": canonical_manifest,
        "consumption_id": "exc_" + "b" * 64,
        "execution_name": manifest_payload["job_resource"] + "/executions/exe-1",
        "job_resource": manifest_payload["job_resource"],
        "source_sha": manifest_payload["source_sha"],
        "image_uri": manifest_payload["image_uri"],
        "result_id": "exr_" + "c" * 64,
        "result_reference": "bq://result",
        "canonical_result_json": result_json,
        "result_digest": hashlib.sha256(result_json.encode()).hexdigest(),
        "status": "succeeded",
        "completed_at": datetime(2026, 9, 13, 12, tzinfo=UTC),
        "origin_registry_sha256": pair[0],
        "resource_manifest_sha256": pair[1],
    }


class Client:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query(self, sql, *_args, **kwargs):
        self.calls.append((sql, kwargs))
        rows = self.rows

        class Job:
            def result(self, **_kwargs):
                return rows

        return Job()


def apply_proof_payload():
    bound = binding("r3_apply")
    return {
        "execution_name": bound.job_resource + "/executions/exe-1",
        "job_resource": bound.job_resource,
        "image_digest": "sha256:" + "b" * 64,
        "source_sha": SOURCE_SHA,
        "service_identity": bound.service_identity,
        "command": "python",
        "args": ["scripts/staging/replay_open_intelligence.py"],
        "config_digest": "c" * 64,
        "started_at": "2026-09-13T12:00:00.000000Z",
        "completed_at": "2026-09-13T12:01:00.000000Z",
        "status": "succeeded",
        "run_id": issuer.RUN_ID,
    }


@pytest.mark.parametrize("module", [replay, issuer, release])
def test_operation_binding_requires_an_explicit_mode_and_version(module):
    origin, bound = module._operation_binding(
        "r3_apply", manifest_version=V2, mode="new_consume", registry=REGISTRY
    )
    assert bound == binding("r3_apply")
    assert origin.image_repository.endswith("/intelligence-42/engine")
    _origin, retained = module._operation_binding(
        "r3_apply", manifest_version=V1, mode="historical_read", registry=REGISTRY
    )
    assert retained.job_resource == OLD_APPLY_JOB
    with pytest.raises(OriginRefusal, match="execution_origin_mode_forbidden"):
        module._operation_binding(
            "r3_apply", manifest_version=V1, mode="new_consume", registry=REGISTRY
        )
    with pytest.raises(OriginRefusal, match="execution_origin_mode_forbidden"):
        module._operation_binding("r3_apply", manifest_version=V2, mode=None, registry=REGISTRY)
    _origin, capture = module._operation_binding(
        "source_snapshot_capture", manifest_version=V2, mode="new_consume", registry=REGISTRY
    )
    assert capture.job_resource.endswith("/jobs/intelligence-42-daily-staging")
    assert (
        capture.service_identity
        == "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    with pytest.raises(ValueError):
        module._operation_binding(
            "bootstrap_migration_apply", manifest_version=V2, mode="new_consume", registry=REGISTRY
        )
    with pytest.raises(ValueError):
        module._operation_binding("r3_apply", manifest_version=V2, mode="new_consume", registry={})


@pytest.mark.parametrize(
    ("module", "operation"),
    [(replay, "r3_apply"), (issuer, "r3_proof_issue"), (release, "r3_release")],
)
def test_runtime_profile_comes_from_the_issued_authority_origin(module, operation):
    bound = binding(operation)
    origin, resolved = module._require_runtime_profile(
        authority(operation), operation, generation=GENERATION, binding=bound
    )
    assert resolved == bound
    assert origin.contract_sha256 == origin_row(operation).contract_sha256
    for broken in (
        authority(operation, job=OLD_APPLY_JOB),
        authority(operation, identity=OLD_APPLY_IDENTITY),
        authority(
            operation,
            image="us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:"
            + "b" * 64,
        ),
        authority(operation, generation=foreign_generation()),
        authority(operation, generation=None),
    ):
        with pytest.raises(ValueError):
            module._require_runtime_profile(broken, operation, generation=GENERATION, binding=bound)
    with pytest.raises(ValueError):
        module._require_runtime_profile(
            authority(operation),
            "r3_apply" if operation != "r3_apply" else "r3_release",
            generation=GENERATION,
            binding=bound,
        )
    with pytest.raises(ValueError):
        module._require_runtime_profile(
            authority(operation), operation, generation=GENERATION, binding=binding("r3_apply", V1)
        )


def test_apply_profile_is_the_v2_binding_under_an_explicit_mode():
    generation, origin, bound = replay._r3_apply_profile(mode="new_consume")
    assert (generation.origin_registry_sha256, generation.resource_manifest_sha256) == ACTIVE
    assert bound == binding("r3_apply")
    assert origin.contract_sha256 == origin_row("r3_apply").contract_sha256
    assert bound.service_identity != OLD_APPLY_IDENTITY
    assert bound.job_resource != OLD_APPLY_JOB
    with pytest.raises(OriginRefusal):
        replay._r3_apply_profile(mode="fresh")
    source = inspect.getsource(replay)
    assert 'os.environ.get("CLOUD_RUN_JOB") == "trends-engine-oi-apply-staging"' not in source
    tail = source[source.index('if __name__ == "__main__"') :]
    assert '_r3_apply_profile(mode="new_consume")' in tail
    assert replay.R3_APPLY_IDENTITY == OLD_APPLY_IDENTITY
    cli = inspect.getsource(replay._run_apply_cli_impl)
    assert "R3_APPLY_IDENTITY" not in cli


def _apply_fixture(monkeypatch, durable_authority, *, protected):
    from google.cloud import bigquery
    from scripts.migrations import create_open_intelligence_v2 as migration

    seen = {}
    monkeypatch.setattr(
        migration,
        "_authorized_credentials",
        lambda plan, *_args: seen.setdefault("service_account", plan.service_account) or object(),
    )

    class FakeJob:
        def __init__(self, rows):
            self.rows = rows

        def result(self, **_kwargs):
            return self.rows

    class FakeClient:
        def query(self, sql, **_kwargs):
            rows = (
                [
                    {"source_table": "event_ledger", "manifest_rows": 1, "matched_rows": 1},
                    {"source_table": "seed_graph", "manifest_rows": 2, "matched_rows": 2},
                ]
                if "source_copy_manifest" in sql
                else []
            )
            return FakeJob(rows)

    monkeypatch.setattr(bigquery, "Client", lambda **_kwargs: FakeClient())
    rule_bundle = SimpleNamespace(
        rule_version="composition_rules_v2", graph_rules=SimpleNamespace(pair_ceiling=10)
    )
    monkeypatch.setattr(replay, "load_composition_rules_v2", lambda *_a, **_k: rule_bundle)
    monkeypatch.setattr(
        replay,
        "run_dynamic_signal_identity_v2",
        lambda *_a, **_k: SimpleNamespace(components=(), missing_work=()),
    )
    monkeypatch.setattr(replay, "collect_component_factor_evidence", lambda **_k: {})
    empty = SimpleNamespace(
        metrics_by_component={},
        receipts_by_component={},
        readiness_by_component={},
        readiness_rules=object(),
        missing_by_component={},
        promotion_results_by_signal_id={},
        quality_failed_by_component={},
        factual_conflict_by_component={},
    )
    monkeypatch.setattr(replay, "assemble_component_inputs", lambda **_k: empty)
    monkeypatch.setattr(replay, "_load_r3_exposure_authority", lambda _r, **_k: ())
    monkeypatch.setattr(replay, "_load_r3_exposure_execution_proof", lambda _p, **_k: {})
    monkeypatch.setattr(replay, "_load_human_review_receipt", lambda _p: {})
    monkeypatch.setattr(replay, "evaluate_r3_quality_authority", lambda **_k: empty)

    def apply(**kwargs):
        kwargs["persist_runner"](batch=SimpleNamespace(), dry_run=True)
        return SimpleNamespace(
            run_id=kwargs["scope"].run_id,
            receipt=SimpleNamespace(
                status="completed", complete_partitions=True, display_release_state="blocked"
            ),
            admitted=(),
            skipped=(),
            persisted_counts={},
        )

    monkeypatch.setattr(replay, "apply_open_intelligence_run", apply)
    monkeypatch.setattr(replay, "_read_r3_execution_proof", lambda **_k: object())
    monkeypatch.setattr(replay, "render_r3_execution_proof", lambda _p: '{"proof":"ok"}\n')
    monkeypatch.setattr(
        replay,
        "persist_producing_batch",
        lambda **kwargs: (
            seen.setdefault("writer_identity", kwargs["writer_identity"])
            or SimpleNamespace(dry_run=kwargs["dry_run"], validated_counts={}, inserted_counts={})
        ),
    )
    if protected:

        def build(self):
            self.review_receipt = {"review_state": "pending"}
            self.exposure_proof = {"run_id": replay.R3_RUN_ID}
            self.exposure_receipts = ({"source_family": "reddit", "exposure_date": "x"},)
            self.source_window_receipts = ({"source_table": "event_ledger"},)
            seen["provider"] = (
                self._version,
                self._mode,
                (
                    self._generation.origin_registry_sha256,
                    self._generation.resource_manifest_sha256,
                ),
            )
            return {"r3_contract": b"contract"}

        monkeypatch.setattr(replay._R3ArtifactProvider, "_build", build)

    def load(operation, **kwargs):
        seen["load"] = (operation, kwargs)
        if protected:
            kwargs["artifact_reader"]("r3_contract")
        return durable_authority

    monkeypatch.setattr(replay.execution_approval, "_load_execution_authority", load)
    monkeypatch.setattr(
        replay.execution_approval,
        "_consume_execution_authority",
        lambda _authority: consumption(),
    )
    monkeypatch.setattr(
        replay.execution_approval,
        "_record_execution_result",
        lambda *args, **_kwargs: (
            seen.setdefault("record", args),
            SimpleNamespace(result_id="exr_" + "0" * 64),
        )[1],
    )
    argv = (
        []
        if protected
        else [
            "--apply-run",
            "--trend-date",
            "2026-09-03",
            "--run-id",
            replay.R3_RUN_ID,
            "--source-sha",
            SOURCE_SHA,
        ]
    )
    return seen, argv


@pytest.mark.parametrize("protected", [True, False])
def test_apply_loads_with_new_consume_and_binds_identity_from_the_origin(monkeypatch, protected):
    seen, argv = _apply_fixture(monkeypatch, authority("r3_apply"), protected=protected)
    payload = json.loads(replay.run_apply_cli(argv))
    assert payload["execution_approval"]["consumption_id"] == "exc_" + "f" * 64
    assert seen["load"][0] == "r3_apply"
    assert seen["load"][1]["mode"] == "new_consume"
    bound = binding("r3_apply")
    if protected:
        assert seen["service_account"] == bound.service_identity
        assert seen["writer_identity"] == bound.service_identity
        assert seen["provider"] == (RESULT_V2, "historical_read", ACTIVE)
    else:
        assert seen["service_account"] != bound.service_identity
    assert seen["record"][1].origin_registry_sha256 == ACTIVE[0]


@pytest.mark.parametrize("protected", [True, False])
def test_apply_refuses_the_old_identity_for_a_fresh_run(monkeypatch, protected):
    seen, argv = _apply_fixture(
        monkeypatch,
        authority("r3_apply", job=OLD_APPLY_JOB, identity=OLD_APPLY_IDENTITY),
        protected=protected,
    )
    with pytest.raises(replay.ApplyRefusal):
        replay.run_apply_cli(argv)
    assert seen["load"][1]["mode"] == "new_consume"
    assert "record" not in seen


def test_apply_failure_records_through_the_same_consumption_and_pair(monkeypatch):
    issued = authority("r3_apply")
    bound = consumption()
    recorded = []

    def fail_after_consumption(_argv, *, _terminal_context):
        _terminal_context.extend((issued, bound))
        raise RuntimeError("private apply detail")

    monkeypatch.setattr(replay, "_run_apply_cli_impl", fail_after_consumption)
    monkeypatch.setattr(
        replay.execution_approval,
        "_record_execution_result",
        lambda *args: recorded.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64),
    )
    with pytest.raises(RuntimeError, match="private apply detail"):
        replay.run_apply_cli([])
    assert len(recorded) == 1
    assert recorded[0][0] is issued
    assert recorded[0][1] is bound
    assert recorded[0][5] == "failed"

    def foreign_pair(_argv, *, _terminal_context):
        _terminal_context.extend((issued, consumption(FOREIGN_PAIR)))
        raise RuntimeError("foreign pair")

    monkeypatch.setattr(replay, "_run_apply_cli_impl", foreign_pair)
    with pytest.raises(RuntimeError, match="foreign pair"):
        replay.run_apply_cli([])
    assert len(recorded) == 1


def test_apply_exposure_chain_reads_v2_rows_only_through_the_trusted_pair():
    payload = {"run_id": replay.R3_RUN_ID, "proof": "exposure"}
    row = chain_row("collection_exposure_issue", payload)
    provider = replay._R3ArtifactProvider(
        Client([row]), version=RESULT_V2, mode="historical_read", generation=GENERATION
    )
    assert provider._exposure_result() == payload
    sql, kwargs = provider._client.calls[0]
    assert "sp_read_open_intelligence_execution_result_chain_v2" in sql
    assert [p.name for p in kwargs["job_config"].query_parameters] == [
        "p_source_operation",
        "p_run_id",
    ]
    historical = replay._R3ArtifactProvider(
        Client([row]), version=RESULT_V1, mode="historical_read", generation=GENERATION
    )
    with pytest.raises(replay.ApplyRefusal):
        historical._exposure_result()
    assert "sp_read_open_intelligence_execution_result_chain_v1" in historical._client.calls[0][0]
    for broken in (
        chain_row("collection_exposure_issue", payload, pair=FOREIGN_PAIR),
        chain_row("collection_exposure_issue", payload, pair=(ACTIVE[0], "9" * 64)),
        chain_row("collection_exposure_issue", payload, job=OLD_APPLY_JOB),
        chain_row("r3_apply", payload),
        {**row, "origin_registry_sha256": ACTIVE[0].upper()},
    ):
        broken_provider = replay._R3ArtifactProvider(
            Client([broken]), version=RESULT_V2, mode="historical_read", generation=GENERATION
        )
        with pytest.raises(replay.ApplyRefusal):
            broken_provider._exposure_result()
    cross = replay._R3ArtifactProvider(
        Client([row]), version=RESULT_V2, mode="historical_read", generation=foreign_generation()
    )
    with pytest.raises(replay.ApplyRefusal):
        cross._exposure_result()
    with pytest.raises(replay.ApplyRefusal):
        replay._R3ArtifactProvider(
            Client([row]), version=RESULT_V2, mode="new_consume", generation=GENERATION
        )._exposure_result()
    with pytest.raises(replay.ApplyRefusal):
        replay._R3ArtifactProvider(
            Client([row]),
            version="open_intelligence_execution_result_v3",
            mode="historical_read",
            generation=GENERATION,
        )._exposure_result()


def _proof_fixture(monkeypatch, issued, *, target_proof=None, apply_row=None):
    seen = {}
    target_proof = apply_proof_payload() if target_proof is None else target_proof
    row = chain_row("r3_apply", target_proof) if apply_row is None else apply_row
    monkeypatch.setattr(issuer.bigquery, "Client", lambda **_kwargs: Client([row]))
    receipt = {"run_id": issuer.RUN_ID, "status": "completed"}
    monkeypatch.setattr(issuer, "_blocked_receipt", lambda _client: receipt)

    def load(operation, **kwargs):
        seen["load"] = (operation, kwargs)
        kwargs["artifact_reader"]("proof_issuer_contract")
        return issued

    monkeypatch.setattr(issuer.execution_approval, "_load_execution_authority", load)
    monkeypatch.setattr(
        issuer.execution_approval, "_consume_execution_authority", lambda _a: consumption()
    )
    monkeypatch.setattr(issuer, "_snapshot", lambda _client: (receipt, {"candidates": 1}))
    monkeypatch.setattr(
        issuer.replay,
        "build_r3_execution_proof",
        lambda **kwargs: seen.setdefault("proof_authority", kwargs["authority"]) and {"p": 1},
    )
    monkeypatch.setattr(issuer.replay, "render_r3_execution_proof", lambda _p: '{"p":1}\n')
    monkeypatch.setattr(
        issuer.execution_approval,
        "_record_execution_result",
        lambda *args, **_k: (
            seen.setdefault("record", args),
            SimpleNamespace(result_id="exr_" + "2" * 64),
        )[1],
    )
    return seen, ["--r3-execution-name", row["execution_name"]]


def test_proof_loads_with_new_consume_and_resolves_both_bindings(monkeypatch, capsys):
    seen, argv = _proof_fixture(monkeypatch, authority("r3_proof_issue"))
    assert issuer.main(argv) == 0
    assert seen["load"] == (
        "r3_proof_issue",
        {"mode": "new_consume", "artifact_reader": issuer._execution_approval_artifact_bytes},
    )
    assert seen["proof_authority"]["job_resource"] == binding("r3_apply").job_resource
    assert seen["proof_authority"]["service_identity"] == binding("r3_apply").service_identity
    assert seen["record"][1].origin_registry_sha256 == ACTIVE[0]
    assert json.loads(capsys.readouterr().out)["execution_approval"]["result_id"].startswith("exr_")
    assert issuer._DURABLE_ARTIFACT_CONTEXT is None


def test_proof_refuses_the_old_identity_and_a_wrong_upstream_apply(monkeypatch):
    seen, argv = _proof_fixture(
        monkeypatch, authority("r3_proof_issue", job=OLD_APPLY_JOB, identity=OLD_APPLY_IDENTITY)
    )
    with pytest.raises(issuer.ProofIssuerRefusal):
        issuer.main(argv)
    assert "record" not in seen
    old_proof = {
        **apply_proof_payload(),
        "job_resource": OLD_APPLY_JOB,
        "service_identity": OLD_APPLY_IDENTITY,
        "execution_name": OLD_APPLY_JOB + "/executions/exe-1",
    }
    old_row = {
        **chain_row("r3_apply", old_proof),
        "job_resource": OLD_APPLY_JOB,
        "execution_name": OLD_APPLY_JOB + "/executions/exe-1",
    }
    seen, argv = _proof_fixture(monkeypatch, authority("r3_proof_issue"), apply_row=old_row)
    with pytest.raises(issuer.ProofIssuerRefusal):
        issuer.main(argv)
    assert "record" not in seen
    seen, argv = _proof_fixture(monkeypatch, authority("r3_proof_issue"), target_proof=old_proof)
    with pytest.raises(issuer.ProofIssuerRefusal):
        issuer.main(argv)
    # The wrong upstream proof surfaces after consumption, so one failed result is recorded
    # through the same consumption and pair.
    assert seen["record"][1].origin_registry_sha256 == ACTIVE[0]
    assert seen["record"][5] == "failed"
    with pytest.raises(issuer.ProofIssuerRefusal):
        issuer._target_proof_authority(
            {"proof": {**apply_proof_payload(), "job_resource": issuer.R3_APPLY_JOB}},
            apply_binding=binding("r3_apply"),
        )
    assert issuer.R3_APPLY_JOB == OLD_APPLY_JOB


def test_proof_target_chain_admits_v2_rows_by_the_same_pair_only():
    payload = apply_proof_payload()
    row = chain_row("r3_apply", payload)
    kwargs = {
        "version": RESULT_V2,
        "mode": "historical_read",
        "generation": GENERATION,
        "apply_binding": binding("r3_apply"),
    }
    result = issuer._target_result(Client([row]), row["execution_name"], **kwargs)
    assert result["proof"] == payload
    for broken in (
        chain_row("r3_apply", payload, pair=FOREIGN_PAIR),
        chain_row("r3_proof_issue", payload),
        {**row, "execution_name": OLD_APPLY_JOB + "/executions/exe-1"},
    ):
        with pytest.raises(issuer.ProofIssuerRefusal):
            issuer._target_result(Client([broken]), row["execution_name"], **kwargs)
    with pytest.raises(issuer.ProofIssuerRefusal):
        issuer._target_result(
            Client([row]), row["execution_name"], **{**kwargs, "generation": foreign_generation()}
        )
    with pytest.raises(issuer.ProofIssuerRefusal):
        issuer._target_result(
            Client([row]), row["execution_name"], **{**kwargs, "mode": "new_consume"}
        )
    historical = Client([row])
    with pytest.raises(issuer.ProofIssuerRefusal):
        issuer._target_result(historical, row["execution_name"], **{**kwargs, "version": RESULT_V1})
    assert "sp_read_open_intelligence_execution_result_chain_v1" in historical.calls[0][0]


def test_proof_failure_records_through_the_same_consumption_and_pair(monkeypatch):
    issued = authority("r3_proof_issue")
    recorded = []
    monkeypatch.setattr(
        issuer.execution_approval,
        "_record_execution_result",
        lambda *args: recorded.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64),
    )

    def foreign_pair(_argv, *, _terminal_context):
        _terminal_context.extend((issued, consumption(FOREIGN_PAIR), "exe"))
        raise RuntimeError("foreign pair")

    monkeypatch.setattr(issuer, "_main_impl", foreign_pair)
    with pytest.raises(RuntimeError, match="foreign pair"):
        issuer.main([])
    assert recorded == []
    bound = consumption()

    def same_pair(_argv, *, _terminal_context):
        _terminal_context.extend((issued, bound, "exe"))
        raise RuntimeError("same pair")

    monkeypatch.setattr(issuer, "_main_impl", same_pair)
    with pytest.raises(RuntimeError, match="same pair"):
        issuer.main([])
    assert len(recorded) == 1
    assert recorded[0][1] is bound
    assert recorded[0][5] == "failed"


def _release_chain_rows():
    receipt = build_run_receipt(**_receipt_fields())
    apply_payload = {**apply_proof_payload(), "run_id": release.R3_RUN_ID}
    apply_row = chain_row("r3_apply", apply_payload)
    proof_manifest = manifest("r3_proof_issue")
    issuer_contract = canonical_bytes(
        {
            "contract_version": "r3-proof-issuer-durable-v1",
            "run_id": release.R3_RUN_ID,
            "target_result_digest": apply_row["result_digest"],
            "target_result_reference": apply_row["result_reference"],
        }
    )
    for artifact in proof_manifest["input_artifacts"]:
        if artifact["name"] == "proof_issuer_contract":
            artifact["sha256"] = hashlib.sha256(issuer_contract).hexdigest()
    proof_sha = execution_approval.manifest_sha256(
        proof_manifest, mode="historical_read", registry=REGISTRY
    )
    from tests.unit.test_open_intelligence_release import _execution_proof

    bound = binding("r3_apply")
    proof_payload = {
        **_execution_proof(run_receipt_digest(receipt), proof_sha),
        "execution_name": bound.job_resource + "/executions/exe-1",
        "job_resource": bound.job_resource,
        "service_identity": bound.service_identity,
    }
    proof_row = {
        **chain_row("r3_proof_issue", proof_payload),
        "canonical_manifest_json": canonical_bytes(proof_manifest).decode(),
        "manifest_sha256": proof_sha,
    }
    return receipt, apply_row, proof_row


def test_release_chain_reader_admits_v2_rows_by_the_same_pair_only():
    _receipt, apply_row, _proof_row = _release_chain_rows()
    kwargs = {"version": RESULT_V2, "mode": "historical_read", "generation": GENERATION}
    client = Client([apply_row])
    result = release._read_operation_result_chain(client, "r3_apply", **kwargs)
    assert result["payload"]["run_id"] == release.R3_RUN_ID
    assert result["registry"] is not None
    assert "sp_read_open_intelligence_execution_result_chain_v2" in client.calls[0][0]
    for broken in (
        {**apply_row, "origin_registry_sha256": FOREIGN_PAIR[0]},
        {**apply_row, "resource_manifest_sha256": "9" * 64},
        {**apply_row, "job_resource": OLD_APPLY_JOB},
    ):
        with pytest.raises(release.ReleaseRefusal):
            release._read_operation_result_chain(Client([broken]), "r3_apply", **kwargs)
    with pytest.raises(release.ReleaseRefusal):
        release._read_operation_result_chain(
            Client([apply_row]), "r3_apply", **{**kwargs, "generation": foreign_generation()}
        )
    with pytest.raises(release.ReleaseRefusal):
        release._read_operation_result_chain(
            Client([apply_row]), "r3_apply", **{**kwargs, "mode": "new_consume"}
        )
    historical = Client([apply_row])
    with pytest.raises(release.ReleaseRefusal):
        release._read_operation_result_chain(
            historical, "r3_apply", **{**kwargs, "version": RESULT_V1}
        )
    assert "sp_read_open_intelligence_execution_result_chain_v1" in historical.calls[0][0]


def test_release_inputs_bind_the_upstream_apply_from_the_v2_origin(monkeypatch):
    receipt, apply_row, proof_row = _release_chain_rows()
    seen_profiles = []

    def reader(value):
        # Every reader on the seam takes the run profile it reads under and is handed it.
        def read(_client, **kwargs):
            seen_profiles.append(kwargs["profile"])
            return value

        return read

    monkeypatch.setattr(release, "_read_blocked_receipt", reader(receipt))
    monkeypatch.setattr(release, "_control_digests", reader(("3" * 64, "4" * 64)))
    review_packet = {"packet_digest": "4" * 64}
    monkeypatch.setattr(release, "_read_review_packet", reader(review_packet))
    monkeypatch.setattr(release, "_read_quality_review_receipt", reader({"review": 1}))
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return "inputs"

    monkeypatch.setattr(release, "_build_release_inputs", build)
    rows = {"r3_apply": apply_row, "r3_proof_issue": proof_row}

    class ChainClient:
        def query(self, sql, *, job_config, **_kwargs):
            operation = job_config.query_parameters[0].value
            assert "result_chain_v2" in sql

            class Job:
                def result(self, **_k):
                    return [rows[operation]]

            return Job()

    bound = binding("r3_apply")
    assert (
        release._read_release_inputs(
            ChainClient(),
            version=RESULT_V2,
            mode="historical_read",
            generation=GENERATION,
            apply_binding=bound,
        )
        == "inputs"
    )
    assert seen["apply_binding"] == bound
    assert seen["execution_proof_manifest_sha256"] == proof_row["manifest_sha256"]
    assert seen["profile"] is release.R3_PROFILE
    assert seen_profiles == [release.R3_PROFILE] * 4
    rows["r3_apply"] = {
        **apply_row,
        "job_resource": OLD_APPLY_JOB,
        "execution_name": OLD_APPLY_JOB + "/executions/exe-1",
    }
    with pytest.raises(release.ReleaseRefusal):
        release._read_release_inputs(
            ChainClient(),
            version=RESULT_V2,
            mode="historical_read",
            generation=GENERATION,
            apply_binding=bound,
        )


def test_release_build_inputs_refuse_a_proof_from_the_wrong_upstream_apply():
    from src.analysis.open_intelligence import live_quality

    from tests.unit import test_dynamic_quality_review as review_fixtures
    from tests.unit.test_open_intelligence_release import _execution_proof

    receipt = build_run_receipt(**_receipt_fields())
    bound = binding("r3_apply")
    proof = {
        **_execution_proof(run_receipt_digest(receipt)),
        "execution_name": bound.job_resource + "/executions/exe-1",
        "job_resource": bound.job_resource,
        "service_identity": bound.service_identity,
    }
    review_packet = live_quality.build_review_packet(release.R3_RUN_ID, review_fixtures._batch())
    review = review_fixtures._receipt(review_packet)
    values = {
        "receipt": receipt,
        "execution_proof_manifest_sha256": "1" * 64,
        "quality_review_receipt": review,
        "review_packet": review_packet,
        "candidate_projection_digest": review["candidate_projection_digest"],
    }
    inputs = release._build_release_inputs(
        execution_proof_bytes=canonical_bytes(proof), apply_binding=bound, **values
    )
    assert inputs.artifacts["execution_proof"] == canonical_bytes(proof)
    old_proof = _execution_proof(run_receipt_digest(receipt))
    with pytest.raises(release.ReleaseRefusal):
        release._build_release_inputs(
            execution_proof_bytes=canonical_bytes(old_proof), apply_binding=bound, **values
        )
    with pytest.raises(release.ReleaseRefusal):
        release._build_release_inputs(
            execution_proof_bytes=canonical_bytes(proof),
            apply_binding=binding("r3_apply", V1),
            **values,
        )
    retained = release._build_release_inputs(
        execution_proof_bytes=canonical_bytes(old_proof),
        apply_binding=binding("r3_apply", V1),
        **values,
    )
    assert retained.artifacts["execution_proof"] == canonical_bytes(old_proof)


def _release_fixture(monkeypatch, issued):
    seen = {}
    receipt = build_run_receipt(**_receipt_fields())
    inputs = release.ReleaseInputs(
        receipt=receipt,
        evidence=SimpleNamespace(release_contract_digest="7" * 64),
        artifacts={"blocked_run_receipt": b"blocked"},
    )
    monkeypatch.setattr(release.bigquery, "Client", lambda **_kwargs: object())

    def read_inputs(_client, **kwargs):
        seen["inputs"] = kwargs
        return inputs

    monkeypatch.setattr(release, "_read_release_inputs", read_inputs)

    def load(operation, **kwargs):
        seen["load"] = (operation, kwargs)
        kwargs["artifact_reader"]("blocked_run_receipt")
        return issued

    monkeypatch.setattr(release.execution_approval, "_load_execution_authority", load)
    monkeypatch.setattr(
        release.execution_approval, "_consume_execution_authority", lambda _a: consumption()
    )
    monkeypatch.setattr(
        release,
        "_release_open_intelligence_run",
        lambda **_k: release.ReleaseReport(
            run_id=release.R3_RUN_ID,
            blocked_receipt_digest="a" * 64,
            released_receipt_digest="d" * 64,
            run_receipt_digest="d" * 64,
            source_window_digest="a" * 64,
            candidate_projection_digest="3" * 64,
            packet_digest="4" * 64,
            review_receipt_digest="5" * 64,
            approval_addendum_sha256="7" * 64,
            released_at=datetime(2026, 9, 13, 12, tzinfo=UTC),
            release_contract_version=release.RELEASE_CONTRACT_VERSION,
            approved_by="durable_execution_approval",
            approval_document="r3-release-contract-v1",
        ),
    )
    monkeypatch.setattr(
        release.execution_approval,
        "_record_execution_result",
        lambda *args, **_k: (
            seen.setdefault("record", args),
            SimpleNamespace(result_id="exr_" + "b" * 64),
        )[1],
    )
    return seen


def test_release_loads_with_new_consume_and_resolves_both_bindings(monkeypatch, capsys):
    seen = _release_fixture(monkeypatch, authority("r3_release"))
    assert release.main([]) == 0
    assert seen["load"] == (
        "r3_release",
        {"mode": "new_consume", "artifact_reader": release._execution_approval_artifact_bytes},
    )
    generation = seen["inputs"].pop("generation")
    assert (generation.origin_registry_sha256, generation.resource_manifest_sha256) == ACTIVE
    assert seen["inputs"] == {
        "version": RESULT_V2,
        "mode": "historical_read",
        "apply_binding": binding("r3_apply"),
        "profile": release.R3_PROFILE,
    }
    assert seen["record"][1].origin_registry_sha256 == ACTIVE[0]
    assert json.loads(capsys.readouterr().out)["execution_approval"]["result_id"].startswith("exr_")


def test_release_refuses_the_old_identity_for_a_fresh_run(monkeypatch):
    seen = _release_fixture(
        monkeypatch,
        authority(
            "r3_release",
            job="projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-release-staging",
            identity="trends-engine-oi-r3-release@ogilvy-trends-v2.iam.gserviceaccount.com",
        ),
    )
    with pytest.raises(release.ReleaseRefusal):
        release.main([])
    assert seen["load"][1]["mode"] == "new_consume"
    assert "record" not in seen


def test_release_failure_records_through_the_same_consumption_and_pair(monkeypatch):
    issued = authority("r3_release")
    recorded = []
    monkeypatch.setattr(
        release.execution_approval,
        "_record_execution_result",
        lambda *args: recorded.append(args) or SimpleNamespace(result_id="exr_" + "d" * 64),
    )

    def foreign_pair(_argv, *, _terminal_context):
        _terminal_context.extend((issued, consumption(FOREIGN_PAIR)))
        raise RuntimeError("foreign pair")

    monkeypatch.setattr(release, "_main_impl", foreign_pair)
    with pytest.raises(RuntimeError, match="foreign pair"):
        release.main([])
    assert recorded == []
    bound = consumption()

    def same_pair(_argv, *, _terminal_context):
        _terminal_context.extend((issued, bound))
        raise RuntimeError("same pair")

    monkeypatch.setattr(release, "_main_impl", same_pair)
    with pytest.raises(RuntimeError, match="same pair"):
        release.main([])
    assert len(recorded) == 1
    assert recorded[0][1] is bound
    assert recorded[0][5] == "failed"


def test_capture_refuses_a_fresh_consume_with_a_named_code_before_any_reader(monkeypatch):
    for name in capture._FORBIDDEN_RUNTIME_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    touched = []
    for mode in ("initial", "recover"):
        with pytest.raises(ValueError, match=r"^snapshot_fresh_route_unavailable$"):
            capture._main_impl(
                ("--cutoff-date", "2026-09-07", "--mode", mode),
                now=lambda: touched.append("clock"),
                execution_reader=lambda: touched.append("execution"),
                approval_reader=lambda _value: touched.append("approval"),
                build_reader=lambda _value: touched.append("build"),
                runtime_clients=lambda: touched.append("runtime"),
                operation_runner=lambda *a, **k: touched.append("operation"),
                source_preflight=lambda *a: touched.append("preflight"),
                stdout=io.StringIO(),
                diagnostics=lambda *a: touched.append("diagnostic"),
            )
    assert touched == []
    assert "snapshot_fresh_route_unavailable" in capture._BOOTSTRAP_REFUSAL_CODES
    with pytest.raises(ValueError, match="snapshot_cli_invalid"):
        capture._main_impl(
            (),
            now=lambda: touched.append("clock"),
            execution_reader=lambda: touched.append("execution"),
            approval_reader=lambda _value: touched.append("approval"),
            build_reader=lambda _value: touched.append("build"),
            runtime_clients=lambda: touched.append("runtime"),
            operation_runner=lambda *a, **k: touched.append("operation"),
            source_preflight=lambda *a: touched.append("preflight"),
            stdout=io.StringIO(),
            diagnostics=lambda *a: touched.append("diagnostic"),
        )
    assert touched == []


def test_capture_main_names_the_refusal_and_binds_retained_readers_to_historical_replay(
    monkeypatch, capsys
):
    seen = {}

    def impl(_argv, **kwargs):
        seen.update(kwargs)
        raise ValueError("snapshot_fresh_route_unavailable")

    monkeypatch.setattr(capture, "_main_impl", impl)
    monkeypatch.setattr(execution_approval, "_source_control_query", lambda: "query")
    assert capture.main(["--cutoff-date", "2026-09-07", "--mode", "initial"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "snapshot_fresh_route_unavailable"
    for name, version in (
        ("approval_reader", execution_approval._APPROVAL_VERSION),
        ("result_reader", RESULT_V1),
        ("result_writer", RESULT_V1),
    ):
        reader = seen[name]
        assert isinstance(reader, functools.partial)
        assert reader.keywords["mode"] == "historical_replay"
        assert reader.keywords["version"] == version
        assert reader.keywords["query"] == "query"
    with pytest.raises(OriginRefusal, match="execution_origin_mode_forbidden"):
        seen["result_writer"]({})


def test_capture_retained_replay_types_rows_under_historical_replay(monkeypatch):
    recovery = {"initial_result_id": "exr_" + "1" * 64}
    seen = {}

    def retained(value, code, *, mode, registry):
        seen["typed"] = (value, mode, registry.sha256)
        raise ValueError(code)

    monkeypatch.setattr(capture, "retained_v1_result", retained)
    row = {"result_id": recovery["initial_result_id"]}
    with pytest.raises(ValueError, match=r"^snapshot_recovery_invalid$"):
        capture._initial_payload(lambda _id: row, recovery, {})
    # Retained v1 rows are typed under the retained amendment e registry, which is no
    # longer the active registry.
    assert seen["typed"] == (
        row,
        "historical_replay",
        "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179",
    )
    assert ACTIVE[0] != seen["typed"][2]
    source = inspect.getsource(capture._initial_payload)
    assert "_result_from_value" not in source
    assert 'mode="historical_replay"' in source
    assert "retained_origin_registry()" in source
    assert "ExecutionResult" not in source
