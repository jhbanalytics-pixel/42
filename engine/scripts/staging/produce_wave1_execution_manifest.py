"""Produce the v2 wave1_pilot execution manifest from the tables the job will read.

Owner run and read only. The command names one Cloud Build and one output path and
nothing else: every digest in the manifest is computed here from bytes this process
rebuilt, never taken from its caller.

* build_provenance is the loader's own canonical receipt of the build describe, read
  through the loader's own build reader, and the source SHA and image URI are that
  receipt's values.
* The five data artifacts are built by run_rss_now._build_wave1_execution_artifacts
  over run_rss_now._wave1_artifact_client, the builder and client the job itself uses
  before it consumes its approval. There is no second implementation here.

The rebuild is deterministic for one source tree, one set of table rows and one UTC
date, but the tables are live: the funded ledger, v_source_lab_v2, seed_graph and the
R3 evidence can change between production and execution. The job never accepts drift.
Its loader recomputes each artifact digest from bytes it rebuilds and refuses with
execution_approval_artifact_mismatch before the approval is consumed. The manifest
expires at the end of the UTC day the funded preflight describes (its as_of date), so
the operator's window runs from production to the earlier of that instant and the next
write to any of those tables.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.staging import build_open_intelligence_execution_manifest as manifest_builder
from scripts.staging.deploy_open_intelligence_job import (
    DeploymentError,
    _check_local_git,
    _run_subprocess,
)
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.execution_origins import OriginRefusal

OPERATION = "wave1_pilot"
MODE = "new_approval"
RECEIPT_VERSION = "wave1_execution_manifest_production_v1"
# The run_rss_now job ran with a three hour task timeout (deploy_open_intelligence_job
# desired_job), which is the policy maximum.
TIMEOUT_SECONDS = 10800
# The Wave 1 contract caps one funded execution at 63 credits (run_rss_now
# _wave1_contract); the manifest limit is that cap, the top of the policy range.
MAX_CREDITS = 63
MINIMUM_WINDOW = timedelta(hours=1)
_BUILD_RESOURCE = re.compile(r"projects/ogilvy-trends-v2/locations/us-central1/builds/[^/]+")
_CLI_FLAGS = ("--build-resource", "--output-file")


class ProducerRefusal(ValueError):
    def __init__(self, code: str, exit_code: int = 1):
        super().__init__(code)
        self.exit_code = exit_code


def _parse_cli(argv: Sequence[str]) -> tuple[str, Path]:
    values = tuple(argv)
    if (
        len(values) != 4
        or values[0::2] != _CLI_FLAGS
        or any(type(value) is not str or not value for value in values[1::2])
    ):
        raise ProducerRefusal("wave1_manifest_cli_invalid", exit_code=2)
    build_resource, output = values[1], Path(values[3])
    if _BUILD_RESOURCE.fullmatch(build_resource) is None or not output.is_absolute():
        raise ProducerRefusal("wave1_manifest_cli_invalid", exit_code=2)
    return build_resource, output


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    observed_at = clock()
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() != timedelta(0)
    ):
        raise ProducerRefusal("wave1_manifest_clock_invalid")
    return observed_at.astimezone(UTC)


def _expires_at(observed_at: datetime) -> datetime:
    """The last microsecond of the UTC day the funded preflight is read for."""
    next_day = datetime.combine(observed_at.date() + timedelta(days=1), time.min, tzinfo=UTC)
    expires_at = next_day - timedelta(microseconds=1)
    if expires_at - observed_at < MINIMUM_WINDOW:
        raise ProducerRefusal("wave1_manifest_window_too_short")
    return expires_at


def _job_module():
    # The module the loader resolves for wave1_pilot (execution_approval
    # _default_artifact_reader), so the builder is the job's own object.
    from scripts import run_rss_now

    return run_rss_now


def _build_provenance(build_resource: str, *, origin, registry, build_reader):
    receipt = execution_approval.build_provenance_from_response(
        build_reader(build_resource),
        origin.contract_sha256,
        manifest_version=origin.manifest_version,
        mode=MODE,
        registry=registry,
    )
    if receipt.build_resource != build_resource:
        raise ProducerRefusal("execution_approval_build_provenance_invalid")
    return receipt, execution_approval.canonical_build_provenance_bytes(receipt, origin=origin)


def _data_artifacts(observed_at: datetime, *, client_factory) -> dict[str, bytes]:
    run_rss_now = _job_module()
    factory = run_rss_now._wave1_artifact_client if client_factory is None else client_factory
    try:
        artifacts = run_rss_now._build_wave1_execution_artifacts(
            client=factory(), observed_at=observed_at
        )
    except RuntimeError as error:
        # A blocked preflight, an incomplete Source Lab snapshot or an invalid seed set
        # refuses here exactly as it would refuse the job.
        raise ProducerRefusal("wave1_manifest_artifact_unavailable") from error
    if not isinstance(artifacts, dict) or any(
        not isinstance(value, bytes) for value in artifacts.values()
    ):
        raise ProducerRefusal("wave1_manifest_artifact_unavailable")
    try:
        contract = json.loads(artifacts["wave1_contract"])
    except (KeyError, ValueError) as error:
        raise ProducerRefusal("wave1_manifest_artifact_unavailable") from error
    if not isinstance(contract, dict) or contract.get("max_credits") != MAX_CREDITS:
        raise ProducerRefusal("wave1_manifest_artifact_unavailable")
    return artifacts


def _policy_rule(origin, registry) -> dict[str, object]:
    policy = json.loads(
        execution_approval._policy_bytes_for_origin(registry=registry, origin=origin)
    )
    return policy["operation_validation"][OPERATION]


def produce(
    argv: Sequence[str],
    *,
    build_reader: Callable[[str], object] | None = None,
    client_factory: Callable[[], object] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    runner=None,
) -> dict[str, object]:
    build_resource, output_path = _parse_cli(argv)
    if output_path.exists():
        raise ProducerRefusal("execution_approval_output_exists")
    registry = manifest_builder._registry_of(manifest_builder._active_generation())
    origin = execution_approval._v2_origin_for_operation(OPERATION, mode=MODE, registry=registry)
    build_reader = (
        execution_approval._default_build_reader if build_reader is None else build_reader
    )
    receipt, provenance = _build_provenance(
        build_resource, origin=origin, registry=registry, build_reader=build_reader
    )
    # The code derived artifacts (route contract, GDELT receipts, query text) must be
    # the bytes of the image the job runs, so the local tree must be the build source.
    try:
        _check_local_git(receipt.resolved_source_sha, _run_subprocess if runner is None else runner)
    except DeploymentError as error:
        raise ProducerRefusal("wave1_manifest_source_mismatch") from error
    observed_at = _utc_now(clock)
    expires_at = _expires_at(observed_at)
    artifacts = {
        "build_provenance": provenance,
        **_data_artifacts(observed_at, client_factory=client_factory),
    }
    rule = _policy_rule(origin, registry)
    if sorted(artifacts) != list(rule["input_artifact_names"]):
        raise ProducerRefusal("wave1_manifest_artifact_unavailable")
    limits = rule["limits"]
    payload = {
        "manifest_version": origin.manifest_version,
        "operation": OPERATION,
        "contract_sha256": origin.contract_sha256,
        "project": receipt.project_id,
        "datasets": list(rule["datasets"]),
        "location": "US",
        "job_resource": rule["job_resource"],
        "service_identity": rule["service_identity"],
        "source_sha": receipt.resolved_source_sha,
        "image_uri": receipt.image_uri,
        "build_resource": receipt.build_resource,
        "command": list(rule["command"]),
        "arguments": list(rule["arguments"]["value"]),
        "environment": [dict(item) for item in rule["environment"]],
        "secrets": list(rule["secrets"]),
        "max_retries": 0,
        "timeout_seconds": TIMEOUT_SECONDS,
        "input_artifacts": [
            {"name": name, "sha256": hashlib.sha256(artifacts[name]).hexdigest()}
            for name in sorted(artifacts)
        ],
        "limits": {
            "max_bytes_billed": limits["max_bytes_billed"]["exact"],
            "max_credits": MAX_CREDITS,
            "max_model_calls": limits["max_model_calls"]["exact"],
            "max_rows_written": limits["max_rows_written"]["exact"],
        },
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    manifest, canonical, digest = manifest_builder._admit_manifest(
        payload, mode=MODE, registry=registry
    )
    manifest_builder._write_canonical(output_path, canonical)
    return {
        "contract_version": RECEIPT_VERSION,
        "manifest_file": str(output_path),
        "manifest_sha256": digest,
        "operation": manifest.operation,
        "build_resource": manifest.build_resource,
        "source_sha": manifest.source_sha,
        "image_uri": manifest.image_uri,
        "observed_at": observed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "expires_at": payload["expires_at"],
        "input_artifacts": dict(manifest.input_artifacts),
    }


def main(argv: Sequence[str] | None = None, **seams) -> int:
    values = tuple(sys.argv[1:] if argv is None else argv)
    try:
        receipt = produce(values, **seams)
    except (ProducerRefusal, manifest_builder.ManifestBuilderRefusal) as error:
        manifest_builder._emit(sys.stderr, {"error": str(error)})
        return error.exit_code
    except (execution_approval.ApprovalRefusal, OriginRefusal) as error:
        manifest_builder._emit(sys.stderr, {"error": str(error)})
        return 1
    except Exception:
        manifest_builder._emit(sys.stderr, {"error": "wave1_manifest_producer_internal"})
        return 1
    manifest_builder._emit(sys.stdout, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
