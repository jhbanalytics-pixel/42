import io
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.execution_origins import OriginRefusal

from tests.unit import test_protected_production_snapshot as input_fixture
from tests.unit import test_protected_snapshot_integration as joined_fixture
from tests.unit.test_execution_manifest_origins import CASES as _ORIGIN_CASES
from tests.unit.test_open_intelligence_execution_approval_runtime import (
    SOURCE_SNAPSHOT_NOW,
    source_snapshot_runtime_fixture,
)


def module():
    from scripts.staging import capture_protected_production_snapshot

    return capture_protected_production_snapshot


class Objects:
    def __init__(self, contents, events):
        self.contents, self.events = contents, events

    def read_input(self, name, digest, *, timeout):
        self.events.append(("input", name, timeout))
        raw = self.contents[name]
        assert module().hashlib.sha256(raw).hexdigest() == digest
        return raw

    def preflight(self, *, timeout):
        self.events.append(("storage_preflight", timeout))


def compact_payload():
    return {
        "contract_version": "open_intelligence_protected_source_snapshot_v1",
        "cutoff_date": "2026-09-07",
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "source_as_of": "2026-09-08T00:00:00+00:00",
        "captured_at": None,
        "snapshot_plan_digest": "a" * 64,
        "snapshot_digest": None,
        "capture_receipt_digest": None,
        "creation_records": [],
        "artifact_attempt": None,
        "stored_artifact": None,
        "query_count": 0,
        "total_bytes_billed": None,
        "limitations": ["upstream_collection_completeness_unproven"],
        "missing_checks": ["synthetic_fixture_only"],
    }


def runtime_values():
    artifacts = input_fixture.artifacts()
    policy = json.loads(artifacts["storage_policy"])
    policy["price_review"]["reviewed_at"] = (SOURCE_SNAPSHOT_NOW - timedelta(minutes=1)).isoformat()
    policy["price_review"]["expires_at"] = (SOURCE_SNAPSHOT_NOW + timedelta(hours=1)).isoformat()
    artifacts["storage_policy"] = canonical_bytes(policy)
    payload, approval, execution, job, build, contents = source_snapshot_runtime_fixture(
        artifacts=artifacts
    )
    return artifacts, payload, approval, execution, job, build, contents


def invoke(monkeypatch, argv=("--cutoff-date", "2026-09-07", "--mode", "initial")):
    for name in module()._FORBIDDEN_RUNTIME_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_API_USE_CLIENT_CERTIFICATE", raising=False)
    monkeypatch.delenv("GOOGLE_API_USE_MTLS_ENDPOINT", raising=False)
    artifacts, payload, approval, execution, job, build, contents = runtime_values()
    events = []
    output = io.StringIO()
    captured_result = {}
    consumed_at = SOURCE_SNAPSHOT_NOW + timedelta(seconds=1)

    def consume(request):
        assert module()._DURABLE_ARTIFACT_CONTEXT is not None
        events.append(("consume", tuple(request)))
        return {
            **request,
            "consumption_contract_version": "open_intelligence_execution_consumption_v1",
            "consumption_id": execution_approval.consumption_id(
                approval.approval_id, execution["name"], consumed_at
            ),
            "consumed_at": consumed_at,
        }

    def record(request):
        events.append(("result", request["result_reference"]))
        captured_result.update(request)
        completed = consumed_at + timedelta(seconds=1)
        return {
            **request,
            "result_contract_version": "open_intelligence_execution_result_v1",
            "result_id": execution_approval.result_id(
                request["consumption_id"],
                request["result_reference"],
                request["result_digest"],
                request["status"],
                completed,
            ),
            "completed_at": completed,
        }

    def operation_runner(*args, **kwargs):
        assert module()._DURABLE_ARTIFACT_CONTEXT is None
        events.append(("operation", args[1], args[2]))
        return compact_payload(), "failed"

    with pytest.raises(ValueError) as refused:
        module()._main_impl(
            argv,
            now=lambda: SOURCE_SNAPSHOT_NOW,
            execution_reader=lambda: {"execution": execution, "job": job},
            approval_reader=lambda digest: approval,
            build_reader=lambda name: build,
            runtime_clients=lambda: (
                SimpleNamespace(project="ogilvy-trends-v2"),
                Objects(contents, events),
            ),
            consumption_writer=consume,
            result_writer=record,
            result_reader=lambda _consumption_id: (),
            operation_runner=operation_runner,
            source_preflight=lambda inputs, client: events.append(
                ("source_preflight", inputs["plan"])
            ),
            stdout=output,
            diagnostics=lambda phase, state, code=None: events.append(
                ("diagnostic", phase, state, code)
            ),
        )
    return refused.value, output.getvalue(), events, captured_result, payload, artifacts


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("--mode", "initial", "--cutoff-date", "2026-09-07"),
        ("--cutoff-date", "2026-09-06", "--mode", "initial"),
        ("--cutoff-date", "2026-09-07", "--mode", "retry"),
        ("--cutoff-date", "2026-09-07", "--mode", "initial", "extra"),
    ],
)
def test_invalid_exact_cli_refuses_before_runtime_access(argv):
    touched = []
    with pytest.raises(ValueError, match="snapshot_cli_invalid"):
        module()._main_impl(
            argv,
            now=lambda: touched.append("clock"),
            execution_reader=lambda: touched.append("execution"),
            approval_reader=lambda value: touched.append("approval"),
            build_reader=lambda value: touched.append("build"),
            runtime_clients=lambda: touched.append("runtime"),
            operation_runner=lambda *args, **kwargs: touched.append("operation"),
            source_preflight=lambda *args: touched.append("preflight"),
            stdout=io.StringIO(),
            diagnostics=lambda *args: touched.append("diagnostic"),
        )
    assert touched == []


def test_cli_uses_real_authority_state_machine_and_records_exact_compact_payload(
    monkeypatch, capsys
):
    """Entry refusal proof. Under a generation whose capture policy is not the bridge policy
    (amendment e, pinned here) no fresh route binds source_snapshot_capture, so
    _main_impl refuses snapshot_fresh_route_unavailable after the argument and environment
    checks and before the clock, every reader, both preflights, the consume write, the
    runner and the result writer; main() turns that into exit code 1 and the bare code on
    stderr with no query issued. The exact compact payload proof that ran here on the live
    state machine now runs on the validated body with the retained view
    (test_validated_body_returns_the_exact_compact_result_record)."""
    from src.analysis.open_intelligence import execution_generations

    monkeypatch.setattr(execution_generations, "ACTIVE_GENERATION_PAIR", AMENDMENT_E_PAIR)
    refused, output, events, recorded, payload, _artifacts = invoke(monkeypatch)
    assert str(refused) == "snapshot_fresh_route_unavailable"
    assert type(refused) is ValueError
    assert not isinstance(refused, execution_approval.ApprovalRefusal)
    assert output == ""
    assert events == []
    assert recorded == {}
    assert payload["limits"]["max_bytes_billed"] == 1_000_000_000
    assert module()._DURABLE_ARTIFACT_CONTEXT is None
    queries = []
    monkeypatch.setattr(
        execution_approval, "_runtime_query", lambda *args, **kwargs: queries.append(args)
    )
    assert module().main(("--cutoff-date", "2026-09-07", "--mode", "initial")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "operation": "source_snapshot_capture",
        "phase": "bootstrap",
        "state": "failed",
        "code": "snapshot_fresh_route_unavailable",
    }
    assert queries == []


def test_main_under_the_bridge_generation_refuses_a_legacy_vector_before_any_query(
    monkeypatch, capsys
):
    """Under the packaged active pair route A opens, so main hands the vector to the bridge
    runner, which refuses a vector without a grant before any reader or query."""
    for name in module()._FORBIDDEN_RUNTIME_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name in (
        "GCP_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_API_USE_CLIENT_CERTIFICATE",
        "GOOGLE_API_USE_MTLS_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    queries = []
    monkeypatch.setattr(
        execution_approval, "_runtime_query", lambda *args, **kwargs: queries.append(args)
    )
    assert module().main(tuple(_legacy("2026-09-07"))) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "operation": "source_snapshot_capture",
        "phase": "bootstrap",
        "state": "failed",
        "code": "snapshot_cli_invalid",
    }
    assert queries == []


def test_validated_body_returns_the_exact_compact_result_record(monkeypatch):
    """The compact payload proof, moved from the fresh CLI path to the validated body: the
    body returns exactly the compact result fields, and the v1 result record built from
    that payload under the retained view carries the payload byte for byte with the
    execution bound reference."""
    _http, _storage_http, args, view = joined_fixture.joined(monkeypatch)
    payload, status = module()._execute_validated_operation(**args)
    assert status == "succeeded", payload["missing_checks"]
    assert set(payload) == module()._COMPACT_RESULT_FIELDS
    result = view.result_record(
        payload,
        status=status,
        completed_at=SOURCE_SNAPSHOT_NOW + timedelta(seconds=5),
        reference=view.consumption.execution_name + "#source-snapshot",
    )
    assert json.loads(result.canonical_result_json) == payload
    assert set(json.loads(result.canonical_result_json)) == module()._COMPACT_RESULT_FIELDS
    assert result.status == "succeeded"
    assert result.result_reference.startswith(
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
    )
    assert result.result_reference.endswith("#source-snapshot")
    assert result.manifest_sha256 == view.consumption.manifest_sha256
    assert result.consumption_id == view.consumption.consumption_id
    assert result.operation == "source_snapshot_capture"


def test_source_consumption_writer_uses_exact_eight_parameter_procedure():
    artifacts = input_fixture.artifacts()
    observed = []

    def query(procedure, parameters):
        observed.append((procedure, parameters))
        return {
            "consumption_contract_version": "open_intelligence_execution_consumption_v1",
            "consumption_id": "exc_" + "1" * 64,
            "consumed_at": SOURCE_SNAPSHOT_NOW,
        }

    request = {
        "approval_id": "exa_" + "0" * 64,
        "manifest_sha256": "a" * 64,
        "operation": "source_snapshot_capture",
        "execution_name": "execution",
        "job_resource": "job",
        "source_sha": "b" * 40,
        "image_uri": "image",
    }
    result = module()._source_consumption_writer(artifacts, query=query)(request)

    procedure, parameters = observed[0]
    assert procedure == "sp_consume_open_intelligence_source_snapshot_v1"
    assert [item.name for item in parameters] == [
        "manifest_sha256",
        "execution_name",
        "job_resource",
        "source_sha",
        "image_uri",
        "capture_plan_json",
        "recovery_context_json",
        "storage_policy_json",
    ]
    assert parameters[5].value == artifacts["capture_plan"].decode()
    assert parameters[6].value == "null"
    assert parameters[7].value == artifacts["storage_policy"].decode()
    assert result["consumption_id"] == "exc_" + "1" * 64


@pytest.mark.parametrize(
    "field,value", [("max_bytes_billed", 999_999_999), ("timeout_seconds", 599)]
)
def test_fixed_collector_profile_refuses_lower_signed_ceiling(field, value):
    authority = SimpleNamespace(
        manifest=SimpleNamespace(limits={"max_bytes_billed": 1_000_000_000}, timeout_seconds=600)
    )
    if field == "max_bytes_billed":
        authority.manifest.limits[field] = value
    else:
        authority.manifest.timeout_seconds = value
    with pytest.raises(ValueError, match="snapshot_runtime_profile_invalid"):
        module()._validate_operation_profile(authority)


def test_recovery_result_adapter_reads_by_consumption_and_verifies_result_id():
    recovery = {
        "initial_consumption_id": "exc_" + "1" * 64,
        "initial_result_id": "exr_" + "2" * 64,
    }
    row = SimpleNamespace(result_id=recovery["initial_result_id"])
    calls = []
    reader = module()._initial_result_reader(
        recovery, lambda consumption_id: calls.append(consumption_id) or (row,)
    )
    assert reader(recovery["initial_result_id"]) is row
    assert calls == [recovery["initial_consumption_id"]]
    with pytest.raises(ValueError, match="snapshot_recovery_invalid"):
        reader("exr_" + "3" * 64)


def test_forbidden_credential_override_refuses_before_runtime_client(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "synthetic.json")
    with pytest.raises(ValueError, match="snapshot_runtime_environment_invalid"):
        module()._validate_runtime_environment()


def test_direct_script_invalid_arguments_refuse_from_unrelated_working_directory(tmp_path):
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts/staging/capture_protected_production_snapshot.py"
    )
    environment = dict(os.environ)
    for name in module()._FORBIDDEN_RUNTIME_ENVIRONMENT:
        environment.pop(name, None)
    result = subprocess.run(
        [sys.executable, str(script), "--cutoff-date", "invalid", "--mode", "initial"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert json.loads(result.stderr.decode("utf-8")) == {
        "code": "snapshot_cli_invalid",
        "operation": "source_snapshot_capture",
        "phase": "bootstrap",
        "state": "failed",
    }


_ARGUMENT_FIRST_PROBE = """
import json, os, runpy, sys
script = sys.argv[1]
sys.argv = [script, *sys.argv[2:]]
pinned = os.environ.get("PROBE_ACTIVE_GENERATION_PAIR")
if pinned:
    # Pin the active pair the way a test monkeypatches it, before the script loads.
    from pathlib import Path
    sys.path.insert(0, str(Path(script).resolve().parents[2]))
    from src.analysis.open_intelligence import execution_generations
    execution_generations.ACTIVE_GENERATION_PAIR = tuple(pinned.split(","))
# Namespace package stubs that site installs at interpreter startup are not the script's.
before = set(sys.modules)
try:
    runpy.run_path(script, run_name="__main__")
    code = None
except SystemExit as exit_:
    code = exit_.code
heavy = sorted(
    name
    for name in set(sys.modules) - before
    if name.startswith(("google.cloud", "google.auth", "scripts.staging", "src.analysis.open_intelligence.production_snapshot"))
)
sys.__stdout__.write(json.dumps({"exit": code, "heavy": heavy}))
"""


@pytest.mark.parametrize(
    "arguments",
    [
        ["--cutoff-date", "invalid", "--mode", "initial"],
        ["--cutoff-date", "2026-09-07", "--mode", "replay"],
        ["--cutoff-date", "2026-09-07"],
        [],
    ],
)
def test_direct_script_invalid_arguments_refuse_before_client_imports(tmp_path, arguments):
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts/staging/capture_protected_production_snapshot.py"
    )
    environment = dict(os.environ)
    for name in module()._FORBIDDEN_RUNTIME_ENVIRONMENT:
        environment.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", _ARGUMENT_FIRST_PROBE, str(script), *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert json.loads(result.stdout.decode("utf-8")) == {"exit": 1, "heavy": []}
    assert (
        result.stderr
        == canonical_bytes(
            {
                "code": "snapshot_cli_invalid",
                "operation": "source_snapshot_capture",
                "phase": "bootstrap",
                "state": "failed",
            }
        )
        + b"\n"
    )


def test_argument_first_check_is_the_same_parser_main_uses():
    source = (
        Path(__file__).resolve().parents[2]
        / "scripts/staging/capture_protected_production_snapshot.py"
    ).read_text(encoding="utf-8")
    assert source.count("def _parse_cli(") == 1
    # The shape check runs before the first import that reaches the client libraries.
    assert source.index("def _parse_cli(") < source.index(
        "from src.analysis.open_intelligence.production_snapshot import"
    )
    with pytest.raises(ValueError, match="snapshot_cli_invalid"):
        module()._parse_cli(["--cutoff-date", "invalid", "--mode", "initial"])
    assert module()._parse_cli(["--cutoff-date", "2026-09-07", "--mode", "recover"])[1] == "recover"


# The grant form the active origin approves, flags and all, and the vectors built from it.
_CUTOFF_FLAG, _, _MODE_FLAG, _, _GRANT_FLAG, _GRANT_ID = _ORIGIN_CASES["source_snapshot_capture"][
    "arguments"
][1:]


def _granted(cutoff="2026-09-22", mode="initial", grant=_GRANT_ID):
    return [_CUTOFF_FLAG, cutoff, _MODE_FLAG, mode, _GRANT_FLAG, grant]


def _legacy(cutoff, mode="initial"):
    return [_CUTOFF_FLAG, cutoff, _MODE_FLAG, mode]


# Amendment e: trusted, and its capture policy takes the v2 plan, so the fresh route is
# closed under it as under every generation that is not the bridge generation.
AMENDMENT_E_PAIR = (
    "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179",
    "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09",
)


def _run_argument_first_probe(tmp_path, arguments, timeout, active_pair=None):
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts/staging/capture_protected_production_snapshot.py"
    )
    environment = dict(os.environ)
    for name in (
        *module()._FORBIDDEN_RUNTIME_ENVIRONMENT,
        "GOOGLE_API_USE_CLIENT_CERTIFICATE",
        "GOOGLE_API_USE_MTLS_ENDPOINT",
        "GCP_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
    ):
        environment.pop(name, None)
    environment.pop("PROBE_ACTIVE_GENERATION_PAIR", None)
    if active_pair is not None:
        environment["PROBE_ACTIVE_GENERATION_PAIR"] = ",".join(active_pair)
    result = subprocess.run(
        [sys.executable, "-c", _ARGUMENT_FIRST_PROBE, str(script), *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return json.loads(result.stdout.decode("utf-8")), result.stderr


def _bootstrap_record(code):
    return (
        canonical_bytes(
            {
                "code": code,
                "operation": "source_snapshot_capture",
                "phase": "bootstrap",
                "state": "failed",
            }
        )
        + b"\n"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        _granted(),
        _ORIGIN_CASES["source_snapshot_capture"]["arguments"][1:],
        _granted("2026-09-13", mode="recover"),
        _legacy("2026-09-07"),
    ],
)
def test_direct_script_admits_the_grant_and_legacy_forms_past_the_argument_first_check(
    tmp_path, arguments
):
    # An admitted vector reaches main, which, under a generation whose capture policy is not
    # the bridge policy, reads no runtime state before it refuses the fresh route; the
    # imports behind the argument first check are loaded by then.
    probe, stderr = _run_argument_first_probe(
        tmp_path, arguments, timeout=120, active_pair=AMENDMENT_E_PAIR
    )
    assert probe["exit"] == 1
    assert "src.analysis.open_intelligence.production_snapshot" in probe["heavy"]
    assert stderr == _bootstrap_record("snapshot_fresh_route_unavailable")


@pytest.mark.parametrize(
    "arguments",
    [_granted("2026-09-13", mode="recover"), _legacy("2026-09-07")],
)
def test_direct_script_under_the_bridge_generation_refuses_what_route_a_does_not_admit(
    tmp_path, arguments
):
    # Under the packaged active pair route A opens, and the bridge runner admits only an
    # initial capture under a grant: a recover or legacy vector refuses before any runtime
    # state is read.
    probe, stderr = _run_argument_first_probe(tmp_path, arguments, timeout=120)
    assert probe["exit"] == 1
    assert "src.analysis.open_intelligence.production_snapshot" in probe["heavy"]
    assert stderr == _bootstrap_record("snapshot_cli_invalid")


@pytest.mark.parametrize(
    "arguments",
    [
        _granted("2026-02-30"),
        _granted("20260922"),
        _granted(grant="Grant-X"),
        _granted(grant=""),
        _granted(mode="retry"),
        [_CUTOFF_FLAG, "2026-09-22", _GRANT_FLAG, _GRANT_ID, _MODE_FLAG, "initial"],
        [*_granted(), "extra"],
        _granted()[:5],
        [*_granted()[:4], _GRANT_FLAG.replace("grant", "policy"), _GRANT_ID],
        _legacy("2026-09-08"),
        _legacy("2026-09-22"),
    ],
)
def test_direct_script_malformed_grant_and_legacy_forms_refuse_before_client_imports(
    tmp_path, arguments
):
    probe, stderr = _run_argument_first_probe(tmp_path, arguments, timeout=10)
    assert probe == {"exit": 1, "heavy": []}
    assert stderr == _bootstrap_record("snapshot_cli_invalid")


def test_argument_first_check_is_the_bridge_parser_with_the_accepted_legacy_cutoffs():
    source = (
        Path(__file__).resolve().parents[2]
        / "scripts/staging/capture_protected_production_snapshot.py"
    ).read_text(encoding="utf-8")
    assert source.count("def _parse_arguments(") == 1
    assert source.index("def _parse_arguments(") < source.index(
        "from src.analysis.open_intelligence.production_snapshot import"
    )
    capture = module()
    # The cutoffs restated for the argument first check are the accepted policy's own.
    assert capture._ACCEPTED_POLICY["allowed_cutoffs"] == capture._ARGUMENT_FIRST_LEGACY_CUTOFFS
    assert capture._parse_arguments(
        _granted(), legacy_cutoffs=capture._ARGUMENT_FIRST_LEGACY_CUTOFFS
    ) == capture._parse_arguments(_granted())


@pytest.mark.parametrize("mode", ["initial", "recovery"])
def test_main_shares_one_control_budget_across_all_source_readers(monkeypatch, mode):
    from dataclasses import asdict

    _, approval, *_ = source_snapshot_runtime_fixture()
    calls = []

    def query(procedure, parameters, **kwargs):
        calls.append((procedure, kwargs))
        if "execution_approval_v1" in procedure:
            return asdict(approval)
        if "sp_record" in procedure:
            return {
                "result_contract_version": "open_intelligence_execution_result_v1",
                "result_id": "synthetic",
                "completed_at": SOURCE_SNAPSHOT_NOW,
            }
        return ()

    monkeypatch.setattr(execution_approval, "_runtime_query", query)

    def run(argv, **kwargs):
        kwargs["approval_reader"](approval.manifest_sha256)
        kwargs["control_query"]("sp_consume_open_intelligence_source_snapshot_v1", [])
        if mode == "recovery":
            kwargs["result_reader"]("original")
        # The result writer main() builds is bound to the retained pair and refuses before
        # any query, so it never draws on the shared budget (no fresh route, successor 1).
        with pytest.raises(OriginRefusal, match=r"^execution_origin_mode_forbidden$"):
            kwargs["result_writer"](
                {
                    "consumption_id": "synthetic",
                    "result_reference": "synthetic",
                    "canonical_result_json": "{}",
                    "result_digest": "0" * 64,
                    "status": "failed",
                }
            )
        assert not any("sp_record" in procedure for procedure, _ in calls)
        kwargs["result_reader"]("synthetic")
        for _ in range(9 - len(calls)):
            kwargs["result_reader"]("synthetic")
        with pytest.raises(execution_approval.ApprovalRefusal):
            kwargs["control_query"]("sp_consume_open_intelligence_source_snapshot_v1", [])
        return 0

    monkeypatch.setattr(module(), "_main_impl", run)
    assert module().main(("--cutoff-date", "2026-09-07", "--mode", mode)) == 0
    assert len(calls) == 9
    assert all(options["_bounded_control"] is True for _, options in calls)
    assert sum(options["_source_byte_limit"] for _, options in calls) == 1_250_000_000


@pytest.mark.parametrize(
    "error,expected",
    [
        (ValueError("snapshot_source_metadata_invalid"), "snapshot_source_metadata_invalid"),
        (ValueError("storage_preflight_invalid"), "storage_preflight_invalid"),
        (
            execution_approval.ApprovalRefusal("execution_approval_artifact_mismatch"),
            "execution_approval_artifact_mismatch",
        ),
        (RuntimeError("private source text and secret token"), "source_snapshot_capture_refused"),
        (RuntimeError("snapshot_source_metadata_invalid"), "source_snapshot_capture_refused"),
        (
            ValueError("snapshot_source_metadata_invalid: private source text"),
            "source_snapshot_capture_refused",
        ),
    ],
)
def test_bootstrap_diagnostic_retains_only_exact_controlled_refusal_codes(
    monkeypatch, capsys, error, expected
):
    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(module(), "_main_impl", refuse)
    assert module().main(("--cutoff-date", "2026-09-07", "--mode", "initial")) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "operation": "source_snapshot_capture",
        "phase": "bootstrap",
        "state": "failed",
        "code": expected,
    }
    assert "private source text" not in output.err
    assert "secret token" not in output.err


@pytest.mark.parametrize("controlled", [False, True])
def test_bootstrap_preserves_only_controlled_nested_consumption_budget_cause(
    monkeypatch, capsys, controlled
):
    from tests.unit.test_open_intelligence_execution_approval_runtime import _load

    *_unused, authority = _load([])

    def writer(request):
        if controlled:
            raise execution_approval.ApprovalRefusal(
                "execution_approval_query_budget_exceeded"
            ) from RuntimeError("private SQL and token")
        raise RuntimeError("execution_approval_query_budget_exceeded private SQL and token")

    def run(*args, **kwargs):
        with pytest.raises(execution_approval.ApprovalRefusal) as caught:
            execution_approval._consume_execution_authority(authority, consumption_writer=writer)
        assert str(caught.value) == "execution_approval_concurrent_conflict"
        raise caught.value

    monkeypatch.setattr(module(), "_main_impl", run)
    assert module().main(("--cutoff-date", "2026-09-07", "--mode", "initial")) == 1
    output = capsys.readouterr()
    assert json.loads(output.err)["code"] == (
        "execution_approval_query_budget_exceeded"
        if controlled
        else "execution_approval_concurrent_conflict"
    )
    assert "private SQL" not in output.err
    assert "token" not in output.err


def test_validate_inputs_binds_cutoff_and_contract_through_the_accepted_policy_artifact():
    """The permitted cutoffs and the contract digest come from the policy artifact the inputs
    carry: the legacy artifact keeps the two historical cutoffs and the legacy contract, a
    grant artifact permits exactly its own cutoffs against its own contract digest."""
    from datetime import UTC, datetime, timedelta

    from src.analysis.open_intelligence import staging_source_profile as policy

    now = input_fixture.NOW
    values = input_fixture.artifacts()
    legacy = module()._validate_inputs(values, input_fixture.CUTOFF, "initial", now)
    assert legacy["policy"]["contract_version"] == policy.LEGACY_CAPTURE_POLICY_VERSION
    with pytest.raises(ValueError, match="snapshot_inputs_invalid"):
        module()._validate_inputs(values, input_fixture.CUTOFF + timedelta(days=2), "initial", now)
    price = {
        "observed_at": now - timedelta(minutes=1),
        "pricing_sources": ["https://cloud.google.com/bigquery/pricing"],
        "storage_micro_usd_per_gib_month": 20000,
        "query_micro_usd_per_tib": 6250000,
        "operations_micro_usd_per_10k": 50000,
        "source_logical_bytes": 5 * 2**30,
        "max_bytes_billed_per_query": 5 * 2**30,
        "queries_per_cycle": 5,
        "jobs_per_cycle": 5,
        "retention_days": 90,
        "operations_per_cycle": 40,
        "permitted_retries": 1,
        "cadence_cycles_per_day": 1,
    }

    def grant(**overrides):
        base = {
            "grant_id": "source_capture_grant_2026_09_v2",
            "environment": "staging",
            "source_estate_digest": "7" * 64,
            "contract_sha256": module()._CONTRACT_SHA256,
            "valid_from": datetime(2026, 9, 1, tzinfo=UTC),
            "valid_until": datetime(2026, 10, 1, tzinfo=UTC),
            "allowed_cutoffs": ["2026-09-07", "2026-09-09"],
            "reserved_micro_usd_per_capture": 750000,
            "cumulative_ceiling_micro_usd": 1500000,
            "revocation_state": "active",
        }
        base.update(overrides)
        return base

    def with_grant(**overrides):
        artifact = policy.build_capture_policy_artifact(
            policy.GRANT_CAPTURE_POLICY_VERSION,
            grant=grant(**overrides),
            price_inputs=price,
            now=now,
        )
        return {**values, "storage_policy": canonical_bytes(artifact)}

    accepted = module()._validate_inputs(with_grant(), input_fixture.CUTOFF, "initial", now)
    assert accepted["policy"]["grant"]["grant_id"] == "source_capture_grant_2026_09_v2"
    assert accepted["policy"]["price_review"]["maximum_cycle_cost_micro_usd"] > 500000
    with pytest.raises(ValueError, match="snapshot_inputs_invalid"):
        module()._validate_inputs(
            with_grant(allowed_cutoffs=["2026-09-09"], cumulative_ceiling_micro_usd=750000),
            input_fixture.CUTOFF,
            "initial",
            now,
        )
    with pytest.raises(ValueError, match="snapshot_inputs_invalid"):
        module()._validate_inputs(
            with_grant(contract_sha256="8" * 64), input_fixture.CUTOFF, "initial", now
        )


def test_validate_inputs_accepts_a_v2_plan_under_the_grant_policy_and_keeps_v1_byte_for_byte():
    """A v2 capture plan is admitted only through the grant bound policy artifact, bound to
    the grant's identity and estate; the legacy policy keeps admitting the v1 plan exactly
    as before and refuses the v2 plan."""
    from datetime import UTC, datetime, timedelta

    from src.analysis.open_intelligence import production_snapshot_tables as tables
    from src.analysis.open_intelligence import staging_source_profile as policy

    from tests.unit import test_staging_source_profile as profiles

    now = profiles.NOW
    grant = {
        "grant_id": "source_capture_grant_2026_09_v2",
        "environment": "staging",
        "source_estate_digest": "7" * 64,
        "contract_sha256": module()._CONTRACT_SHA256,
        "valid_from": datetime(2026, 9, 1, tzinfo=UTC),
        "valid_until": datetime(2026, 10, 1, tzinfo=UTC),
        "allowed_cutoffs": ["2026-09-12", "2026-09-13"],
        "reserved_micro_usd_per_capture": 750000,
        "cumulative_ceiling_micro_usd": 1500000,
        "revocation_state": "active",
    }
    price = {
        "observed_at": now - timedelta(minutes=1),
        "pricing_sources": ["https://cloud.google.com/bigquery/pricing"],
        "storage_micro_usd_per_gib_month": 20000,
        "query_micro_usd_per_tib": 6250000,
        "operations_micro_usd_per_10k": 50000,
        "source_logical_bytes": 5 * 2**30,
        "max_bytes_billed_per_query": 5 * 2**30,
        "queries_per_cycle": 5,
        "jobs_per_cycle": 5,
        "retention_days": 90,
        "operations_per_cycle": 40,
        "permitted_retries": 1,
        "cadence_cycles_per_day": 1,
    }
    artifact = policy.build_capture_policy_artifact(
        policy.GRANT_CAPTURE_POLICY_VERSION, grant=grant, price_inputs=price, now=now
    )
    plan = tables.build_capture_plan_v2(
        profiles.profile(),
        grant=grant,
        client_scope_id="ogilvy_default",
        market_scope=["ke", "ng", "za"],
        now=now,
    )
    values = {
        **input_fixture.artifacts(),
        "capture_plan": canonical_bytes(plan),
        "storage_policy": canonical_bytes(artifact),
    }
    accepted = module()._validate_inputs(values, profiles.CUTOFF, "initial", now)
    assert accepted["plan"] == plan
    assert accepted["envelope"] == plan
    assert accepted["policy"]["grant"]["grant_id"] == grant["grant_id"]
    assert accepted["client_scope_id"] == "ogilvy_default"
    assert accepted["market_scope"] == ("ke", "ng", "za")
    # The v2 plan binds to the grant it names: another grant refuses it.
    other = policy.build_capture_policy_artifact(
        policy.GRANT_CAPTURE_POLICY_VERSION,
        grant={**grant, "grant_id": "another_grant"},
        price_inputs=price,
        now=now,
    )
    with pytest.raises(ValueError, match="source_snapshot_estate_mismatch"):
        module()._validate_inputs(
            {**values, "storage_policy": canonical_bytes(other)},
            profiles.CUTOFF,
            "initial",
            now,
        )
    # The legacy policy admits only the v1 plan, and the v1 plan still reads as before.
    with pytest.raises(ValueError, match="snapshot_inputs_invalid"):
        module()._validate_inputs(
            {**values, "storage_policy": input_fixture.artifacts()["storage_policy"]},
            profiles.CUTOFF,
            "initial",
            now,
        )
    legacy = module()._validate_inputs(
        input_fixture.artifacts(), input_fixture.CUTOFF, "initial", input_fixture.NOW
    )
    assert legacy["envelope"]["contract_version"] == "open_intelligence_protected_capture_plan_v1"
    assert type(legacy["plan"]).__name__ == "SnapshotPlan"
