"""Capture fixed source snapshots through the existing execution authority."""

import copy
import hashlib
import json
import re
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlsplit

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.analysis.open_intelligence.brain_contract import canonical_bytes

_CLI_REFUSAL = {
    "code": "snapshot_cli_invalid",
    "operation": "source_snapshot_capture",
    "phase": "bootstrap",
    "state": "failed",
}


# The accepted legacy policy's cutoffs, restated for the argument first check: the policy
# record itself is read through the imports below. A unit test pins the two equal.
_ARGUMENT_FIRST_LEGACY_CUTOFFS = ("2026-09-07",)


def _parse_arguments(argv, *, legacy_cutoffs=None):
    """The cutoff, mode and grant id of one command line, or ``snapshot_cli_invalid``.

    The legacy form ``--cutoff-date D --mode M`` admits only the cutoffs of the accepted
    legacy policy. The grant form appends ``--grant G``, the vector the active v2 origin
    approves; its cutoff is any calendar date here and is admitted or refused by the grant
    the storage policy artifact carries, which ``_validate_cli_inputs`` binds to ``G``.
    Neither form admits a cutoff its policy does not. The legacy cutoffs are the accepted
    policy's unless ``legacy_cutoffs`` names them, as the argument first check does.
    """
    values = tuple(argv) if isinstance(argv, (list, tuple)) else ()
    if (
        len(values) not in (4, 6)
        or any(type(value) is not str for value in values)
        or values[0] != "--cutoff-date"
        or values[2] != "--mode"
        or values[3] not in {"initial", "recover"}
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", values[1]) is None
        or (
            len(values) == 6
            and (values[4] != "--grant" or re.fullmatch(r"[a-z0-9_]+", values[5]) is None)
        )
    ):
        raise ValueError("snapshot_cli_invalid")
    try:
        cutoff = date.fromisoformat(values[1])
    except ValueError as error:
        raise ValueError("snapshot_cli_invalid") from error
    if len(values) == 4:
        if values[1] not in (
            _ACCEPTED_POLICY["allowed_cutoffs"] if legacy_cutoffs is None else legacy_cutoffs
        ):
            raise ValueError("snapshot_cli_invalid")
        return cutoff, values[3], None
    return cutoff, values[3], values[5]


def _parse_cli(argv):
    cutoff, mode, _grant_id = _parse_arguments(argv)
    return cutoff, mode


# A direct run checks both argument forms before the imports below, which reach the cloud
# client libraries; an invalid vector refuses with the same bootstrap record main writes.
if __name__ == "__main__":
    try:
        _parse_arguments(tuple(sys.argv[1:]), legacy_cutoffs=_ARGUMENT_FIRST_LEGACY_CUTOFFS)
    except ValueError:
        sys.stderr.write(canonical_bytes(_CLI_REFUSAL).decode("utf-8") + "\n")
        raise SystemExit(1) from None

from src.analysis.open_intelligence.production_snapshot import _scope, _serialize_plan
from src.analysis.open_intelligence.production_snapshot_tables import (
    CAPTURE_PLAN_V2_VERSION,
    build_snapshot_plan,
    derive_protected_creation_statements,
    retained_origin_registry,
    retained_v1_result,
    validate_capture_plan_v2,
)
from src.analysis.open_intelligence.staging_source_profile import (
    GRANT_CAPTURE_POLICY_VERSION,
    LEGACY_CAPTURE_POLICY_VERSION,
    accepted_capture_policy,
    validate_capture_policy_artifact,
)

_OPERATION = "source_snapshot_capture"
_PROJECT = "ogilvy-trends-v2"
_IDENTITY = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
_JOB = f"projects/{_PROJECT}/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging"
# The policy closure: contract digest, permitted cutoffs, storage constants and the modeled
# cost bound are read from the one accepted policy record of the legacy version.
_ACCEPTED_POLICY = accepted_capture_policy(LEGACY_CAPTURE_POLICY_VERSION)
_CONTRACT_SHA256 = _ACCEPTED_POLICY["contract_sha256"]
_MAXIMUM_CYCLE_COST_BOUND = _ACCEPTED_POLICY["maximum_cycle_cost_bound_micro_usd"]
_INPUTS = frozenset(
    {"capture_contract", "capture_plan", "recovery_context", "source_metadata", "storage_policy"}
)
_DURABLE_ARTIFACT_CONTEXT = None
_POLICY_CONSTANTS = _ACCEPTED_POLICY["storage_policy_constants"]


def _timestamp(value):
    if type(value) is not str:
        raise ValueError("snapshot_inputs_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("snapshot_inputs_invalid")
    return parsed.astimezone(UTC)


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _decode(raw):
    if type(raw) is not bytes:
        raise ValueError("snapshot_inputs_invalid")
    value = json.loads(raw.decode("utf-8"))
    if canonical_bytes(value) != raw:
        raise ValueError("snapshot_inputs_invalid")
    return value


def _validate_policy(policy, now):
    if type(policy) is dict and policy.get("contract_version") == GRANT_CAPTURE_POLICY_VERSION:
        validate_capture_policy_artifact(policy, now=now)
        return
    if type(policy) is not dict or set(policy) != set(_POLICY_CONSTANTS) | {"price_review"}:
        raise ValueError("snapshot_storage_policy_invalid")
    for key, expected in _POLICY_CONSTANTS.items():
        if type(policy[key]) is not type(expected) or policy[key] != expected:
            raise ValueError("snapshot_storage_policy_invalid")
    price = policy["price_review"]
    if type(price) is not dict or set(price) != {
        "reviewed_at",
        "expires_at",
        "pricing_sources",
        "assumptions",
        "maximum_cycle_cost_micro_usd",
    }:
        raise ValueError("snapshot_price_review_invalid")
    reviewed, expires = _timestamp(price["reviewed_at"]), _timestamp(price["expires_at"])
    if not reviewed <= now < expires or expires - reviewed > timedelta(days=1):
        raise ValueError("snapshot_price_review_invalid")
    cost = price["maximum_cycle_cost_micro_usd"]
    if type(cost) is not int or not 0 <= cost <= _MAXIMUM_CYCLE_COST_BOUND:
        raise ValueError("snapshot_price_review_invalid")
    for field in ("pricing_sources", "assumptions"):
        values = price[field]
        if (
            type(values) is not list
            or not values
            or any(type(item) is not str or not item.strip() for item in values)
            or values != sorted(set(values))
        ):
            raise ValueError("snapshot_price_review_invalid")
    for url in price["pricing_sources"]:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
            raise ValueError("snapshot_price_review_invalid")


def _validate_recovery(value, mode):
    if mode == "initial":
        if value is not None:
            raise ValueError("snapshot_recovery_invalid")
        return
    keys = {
        "contract_version",
        "initial_manifest_sha256",
        "initial_consumption_id",
        "initial_execution_name",
        "initial_result_id",
        "initial_result_digest",
    }
    if type(value) is not dict:
        raise ValueError("snapshot_recovery_invalid")
    if value.get("contract_version") == "open_intelligence_source_capture_recovery_v2":
        keys.add("failed_creation_job_digest")
        if not _digest(value.get("failed_creation_job_digest")):
            raise ValueError("snapshot_recovery_invalid")
    if value.get("contract_version") in {
        "open_intelligence_source_capture_recovery_v3",
        "open_intelligence_source_capture_recovery_v4",
    }:
        keys.add("ancestor_recovery_context")
        ancestor = value.get("ancestor_recovery_context")
        expected_ancestor = (
            "open_intelligence_source_capture_recovery_v3"
            if value["contract_version"] == "open_intelligence_source_capture_recovery_v4"
            else "open_intelligence_source_capture_recovery_v2"
        )
        if type(ancestor) is not dict or ancestor.get("contract_version") != expected_ancestor:
            raise ValueError("snapshot_recovery_invalid")
        _validate_recovery(ancestor, "recover")
    if set(value) != keys:
        raise ValueError("snapshot_recovery_invalid")
    if (
        value["contract_version"]
        not in {
            "open_intelligence_source_capture_recovery_v1",
            "open_intelligence_source_capture_recovery_v2",
            "open_intelligence_source_capture_recovery_v3",
            "open_intelligence_source_capture_recovery_v4",
        }
        or not _digest(value["initial_manifest_sha256"])
        or not _digest(value["initial_result_digest"])
        or type(value["initial_consumption_id"]) is not str
        or re.fullmatch(r"exc_[0-9a-f]{64}", value["initial_consumption_id"]) is None
        or type(value["initial_result_id"]) is not str
        or re.fullmatch(r"exr_[0-9a-f]{64}", value["initial_result_id"]) is None
        or type(value["initial_execution_name"]) is not str
        or not value["initial_execution_name"].startswith(_JOB + "/executions/")
        or re.fullmatch(r"[a-z0-9-]+", value["initial_execution_name"].rsplit("/", 1)[-1]) is None
    ):
        raise ValueError("snapshot_recovery_invalid")


def _initial_capture_manifest(recovery, current_manifest):
    if recovery is None:
        return current_manifest
    if recovery["contract_version"] == "open_intelligence_source_capture_recovery_v4":
        recovery = recovery["ancestor_recovery_context"]
    if recovery["contract_version"] == "open_intelligence_source_capture_recovery_v3":
        recovery = recovery["ancestor_recovery_context"]
    return recovery["initial_manifest_sha256"]


def _policy_bindings(artifacts):
    """The contract digest and permitted cutoffs of the policy the inputs carry."""
    policy = _decode(artifacts["storage_policy"])
    if (
        type(policy) is dict
        and policy.get("contract_version") == GRANT_CAPTURE_POLICY_VERSION
        and type(policy.get("grant")) is dict
    ):
        grant = policy["grant"]
        cutoffs = grant.get("allowed_cutoffs")
        return grant.get("contract_sha256"), tuple(cutoffs) if type(cutoffs) is list else ()
    return _CONTRACT_SHA256, _ACCEPTED_POLICY["allowed_cutoffs"]


def _validate_inputs(artifacts, cutoff, mode, now):
    if (
        type(artifacts) is not dict
        or set(artifacts) != _INPUTS
        or any(type(value) is not bytes for value in artifacts.values())
        or type(cutoff) is not date
        or mode not in {"initial", "recover"}
        or not isinstance(now, datetime)
        or now.tzinfo is None
    ):
        raise ValueError("snapshot_inputs_invalid")
    contract_sha256, allowed_cutoffs = _policy_bindings(artifacts)
    if (
        cutoff.isoformat() not in allowed_cutoffs
        or hashlib.sha256(artifacts["capture_contract"]).hexdigest() != contract_sha256
    ):
        raise ValueError("snapshot_inputs_invalid")
    now = now.astimezone(UTC)
    envelope = _decode(artifacts["capture_plan"])
    if type(envelope) is dict and envelope.get("contract_version") == CAPTURE_PLAN_V2_VERSION:
        # The v2 plan is admitted only under the grant bound policy, bound to that grant's
        # estate digest, grant id and permitted cutoffs; the legacy path below is untouched.
        policy = _decode(artifacts["storage_policy"])
        if type(policy) is not dict or policy.get("contract_version") != (
            GRANT_CAPTURE_POLICY_VERSION
        ):
            raise ValueError("snapshot_inputs_invalid")
        _validate_policy(policy, now)
        recovery = _decode(artifacts["recovery_context"])
        _validate_recovery(recovery, mode)
        plan = validate_capture_plan_v2(envelope, cutoff=cutoff, grant=policy["grant"], now=now)
        client_scope, markets = _scope(plan["client_scope_id"], plan["market_scope"])
        return {
            "plan": plan,
            "envelope": envelope,
            "policy": policy,
            "recovery": recovery,
            "client_scope_id": client_scope,
            "market_scope": markets,
        }
    if type(envelope) is not dict or set(envelope) != {
        "contract_version",
        "cutoff_date",
        "client_scope_id",
        "market_scope",
        "snapshot_plan",
        "creation_statements",
    }:
        raise ValueError("snapshot_inputs_invalid")
    if (
        envelope["contract_version"] != "open_intelligence_protected_capture_plan_v1"
        or envelope["cutoff_date"] != cutoff.isoformat()
        or type(envelope["market_scope"]) is not list
    ):
        raise ValueError("snapshot_inputs_invalid")
    client_scope, markets = _scope(envelope["client_scope_id"], envelope["market_scope"])
    if list(markets) != envelope["market_scope"]:
        raise ValueError("snapshot_inputs_invalid")
    plan = build_snapshot_plan(cutoff, now=now, source_metadata=artifacts["source_metadata"])
    if canonical_bytes(envelope["snapshot_plan"]) != canonical_bytes(
        _serialize_plan(plan)
    ) or canonical_bytes(envelope["creation_statements"]) != canonical_bytes(
        derive_protected_creation_statements(plan)
    ):
        raise ValueError("snapshot_inputs_invalid")
    policy, recovery = _decode(artifacts["storage_policy"]), _decode(artifacts["recovery_context"])
    _validate_policy(policy, now)
    _validate_recovery(recovery, mode)
    return {
        "plan": plan,
        "envelope": envelope,
        "policy": policy,
        "recovery": recovery,
        "client_scope_id": client_scope,
        "market_scope": markets,
    }


def _source_preflight(inputs, client):
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.production_snapshot_tables import _schema

    if getattr(client, "project", None) != _PROJECT:
        raise ValueError("snapshot_source_metadata_invalid")
    total = 0
    asof_millis = int(inputs["plan"].source_as_of.timestamp() * 1000)
    for statement in inputs["plan"].statements:
        native = client.get_table(
            f"{statement.source_table}@{asof_millis}", retry=None, timeout=10
        ).to_api_repr()
        project, dataset, table = statement.source_table.split(".")
        count = native.get("numBytes")
        modified = native.get("lastModifiedTime")
        if (
            native.get("tableReference")
            != {"projectId": project, "datasetId": dataset, "tableId": f"{table}@{asof_millis}"}
            or native.get("type") != "TABLE"
            or native.get("location") != "US"
            or type(count) is not str
            or re.fullmatch(r"0|[1-9][0-9]*", count) is None
            or type(modified) is not str
            or re.fullmatch(r"0|[1-9][0-9]*", modified) is None
            or canonical_digest(_schema(native.get("schema"))) != statement.source_schema_digest
        ):
            raise ValueError("snapshot_source_metadata_invalid")
        total += int(count)
    if total > inputs["policy"]["max_source_logical_bytes"]:
        raise ValueError("snapshot_source_size_exceeded")


def _initial_payload(reader, recovery, inputs):
    if not callable(reader):
        raise ValueError("snapshot_recovery_invalid")
    row = reader(recovery["initial_result_id"])
    # A retained v1 result, typed under historical replay against the packaged retained
    # registry so the row must sit under the retained source snapshot job.
    result = retained_v1_result(
        row,
        "snapshot_recovery_invalid",
        mode="historical_replay",
        registry=retained_origin_registry(),
    )
    if (
        result.result_id != recovery["initial_result_id"]
        or result.result_digest != recovery["initial_result_digest"]
        or result.manifest_sha256 != recovery["initial_manifest_sha256"]
        or result.consumption_id != recovery["initial_consumption_id"]
        or result.execution_name != recovery["initial_execution_name"]
        or result.operation != _OPERATION
    ):
        raise ValueError("snapshot_recovery_invalid")
    payload = json.loads(result.canonical_result_json)
    if (
        type(payload) is not dict
        or set(payload) != set(_empty_payload(inputs))
        or payload["contract_version"] != "open_intelligence_protected_source_snapshot_v1"
        or payload["cutoff_date"] != inputs["plan"].cutoff_date.isoformat()
        or payload["client_scope_id"] != inputs["client_scope_id"]
        or payload["market_scope"] != list(inputs["market_scope"])
        or payload["source_as_of"] != inputs["plan"].source_as_of.isoformat()
        or payload["snapshot_plan_digest"] != inputs["plan"].plan_digest
        or type(payload["query_count"]) is not int
        or not 0 <= payload["query_count"] <= 5
        or (
            payload["total_bytes_billed"] is not None
            and (
                type(payload["total_bytes_billed"]) is not int
                or not 0 <= payload["total_bytes_billed"] <= 1_000_000_000
            )
        )
    ):
        raise ValueError("snapshot_recovery_invalid")
    return payload


def _empty_payload(inputs):
    return {
        "contract_version": "open_intelligence_protected_source_snapshot_v1",
        "cutoff_date": inputs["plan"].cutoff_date.isoformat(),
        "client_scope_id": inputs["client_scope_id"],
        "market_scope": list(inputs["market_scope"]),
        "source_as_of": inputs["plan"].source_as_of.isoformat(),
        "captured_at": None,
        "snapshot_plan_digest": inputs["plan"].plan_digest,
        "snapshot_digest": None,
        "capture_receipt_digest": None,
        "creation_records": [],
        "artifact_attempt": None,
        "stored_artifact": None,
        "query_count": 0,
        "total_bytes_billed": 0,
        "limitations": ["upstream_collection_completeness_unproven"],
        "missing_checks": [],
    }


@contextmanager
def _operation_transport(client, objects):
    bucket = _POLICY_CONSTANTS["bucket"]
    scopes = (
        (client._http, "bigquery.googleapis.com", (f"/bigquery/v2/projects/{_PROJECT}/",)),
        (
            objects.bucket.client._http,
            "storage.googleapis.com",
            (
                f"/storage/v1/b/{bucket}",
                f"/upload/storage/v1/b/{bucket}/",
                f"/download/storage/v1/b/{bucket}/",
            ),
        ),
    )
    sessions = {}
    for session, host, prefixes in scopes:
        entry = sessions.setdefault(id(session), [session, session.request, []])
        entry[2].append((host, prefixes))

    def guarded(original, rules):
        def request(method, url, *args, **kwargs):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not any(
                parsed.netloc == host
                and any(
                    parsed.path == prefix.rstrip("/")
                    or parsed.path.startswith(prefix.rstrip("/") + "/")
                    for prefix in prefixes
                )
                for host, prefixes in rules
            ):
                raise ValueError("snapshot_transport_invalid")
            kwargs["allow_redirects"] = False
            response = original(method, url, *args, **kwargs)
            resumable_progress = (
                response.status_code == 308
                and method == "PUT"
                and parsed.netloc == "storage.googleapis.com"
                and parsed.path.startswith(f"/upload/storage/v1/b/{bucket}/")
                and "upload_id" in parse_qs(parsed.query)
                and "location" not in response.headers
            )
            if 300 <= response.status_code < 400 and not resumable_progress:
                raise ValueError("snapshot_transport_redirect_refused")
            return response

        return request

    try:
        for session, original, rules in sessions.values():
            session.request = guarded(original, rules)
        yield
    finally:
        for session, original, _rules in sessions.values():
            session.request = original


def _run_operation(
    artifacts,
    cutoff,
    mode,
    *,
    authority,
    consumption,
    client,
    objects,
    now,
    initial_result_reader=None,
    diagnostics=None,
):
    from src.analysis.open_intelligence import execution_approval as execution
    from src.analysis.open_intelligence import production_snapshot_tables as tables

    execution._require_consumed_execution(authority, consumption, _OPERATION)

    def recheck():
        execution._require_consumed_execution(authority, consumption, _OPERATION)

    def claim():
        with tables._CREATION_LOCK:
            if authority in tables._CREATION_INVOCATIONS:
                raise ValueError("snapshot_creation_already_attempted")
            tables._CREATION_INVOCATIONS.add(authority)

    return _execute_operation(
        artifacts,
        cutoff,
        mode,
        authority=authority,
        consumption=consumption,
        recheck=recheck,
        claim=claim,
        client=client,
        objects=objects,
        now=now,
        initial_result_reader=initial_result_reader,
        diagnostics=diagnostics,
    )


def _execute_operation(
    artifacts,
    cutoff,
    mode,
    *,
    authority,
    consumption,
    recheck,
    claim,
    client,
    objects,
    now,
    initial_result_reader=None,
    diagnostics=None,
):
    from src.analysis.open_intelligence import execution_approval as execution

    execution._require_consumed_execution(authority, consumption, _OPERATION)
    return _execute_validated_operation(
        artifacts,
        cutoff,
        mode,
        manifest=authority.manifest,
        consumption=consumption,
        recheck=recheck,
        claim=claim,
        client=client,
        objects=objects,
        now=now,
        initial_result_reader=initial_result_reader,
        diagnostics=diagnostics,
    )


def _execute_validated_operation(
    artifacts,
    cutoff,
    mode,
    *,
    manifest,
    consumption,
    recheck,
    claim,
    client,
    objects,
    now,
    initial_result_reader=None,
    diagnostics=None,
):
    with _operation_transport(client, objects):
        return _validated_operation(
            artifacts,
            cutoff,
            mode,
            manifest=manifest,
            consumption=consumption,
            recheck=recheck,
            claim=claim,
            client=client,
            objects=objects,
            now=now,
            initial_result_reader=initial_result_reader,
            diagnostics=diagnostics,
        )


def _validated_operation(
    artifacts,
    cutoff,
    mode,
    *,
    manifest,
    consumption,
    recheck,
    claim,
    client,
    objects,
    now,
    initial_result_reader=None,
    diagnostics=None,
):
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.production_snapshot_capture import (
        SnapshotCaptureError,
        capture_production_snapshot,
        validate_captured_production_snapshot,
    )
    from src.analysis.open_intelligence.production_snapshot_storage import SourceCaptureStorageError
    from src.analysis.open_intelligence.production_snapshot_tables import (
        SnapshotCreationError,
        _execute_validated_snapshot_creations,
    )

    if not callable(recheck) or not callable(claim):
        raise ValueError("snapshot_inputs_invalid")
    inputs = _validate_inputs(artifacts, cutoff, mode, now)
    if (
        manifest.contract_sha256 != _CONTRACT_SHA256
        or manifest.service_identity != _IDENTITY
        or manifest.limits["max_bytes_billed"] != 1_000_000_000
        or manifest.timeout_seconds != 600
        or any(
            hashlib.sha256(raw).hexdigest() != dict(manifest.input_artifacts).get(name)
            for name, raw in artifacts.items()
        )
    ):
        raise ValueError("snapshot_inputs_invalid")
    diagnostic_code = None
    started = monotonic()

    def current():
        return now.astimezone(UTC) + timedelta(seconds=max(0, monotonic() - started))

    def remaining():
        deadline = min(manifest.expires_at, consumption.consumed_at + timedelta(seconds=600))
        seconds = (deadline - current()).total_seconds()
        if seconds <= 0:
            raise ValueError("snapshot_operation_deadline")
        return min(seconds, 30)

    def emit(phase, state, code=None):
        if diagnostics is not None:
            diagnostics(
                {
                    "operation": _OPERATION,
                    "execution_name": consumption.execution_name,
                    "consumption_id": consumption.consumption_id,
                    "phase": phase,
                    "state": state,
                    "code": code,
                }
            )

    payload = _empty_payload(inputs)
    initial = None
    saved_artifact = None
    stored = None
    phase = "creation"
    capture_started = False
    capture = None
    try:
        if inputs["recovery"] is not None:
            initial = _initial_payload(initial_result_reader, inputs["recovery"], inputs)
            if inputs["recovery"][
                "contract_version"
            ] == "open_intelligence_source_capture_recovery_v3" and (
                initial["query_count"] != 0
                or any(
                    initial[field] is not None
                    for field in (
                        "captured_at",
                        "snapshot_digest",
                        "capture_receipt_digest",
                        "artifact_attempt",
                        "stored_artifact",
                    )
                )
            ):
                raise ValueError("snapshot_capture_recovery_unavailable")
            payload = copy.deepcopy(initial)
            payload["missing_checks"] = []
            if initial["artifact_attempt"] is not None:
                phase = "artifact_recovery"
                raw, stored = objects.read_capture(
                    initial["artifact_attempt"], initial["stored_artifact"], timeout=remaining()
                )
                saved_artifact = json.loads(raw)
                capture = validate_captured_production_snapshot(
                    saved_artifact["capture"],
                    cutoff_date=cutoff,
                    client_scope_id=inputs["client_scope_id"],
                    market_scope=inputs["market_scope"],
                    reviewed_metadata=artifacts["source_metadata"],
                    expected_creator_email=_IDENTITY,
                )
                snapshot = capture["assembly"]["snapshot"]
                if (
                    saved_artifact["initial_manifest_sha256"]
                    != inputs["recovery"]["initial_manifest_sha256"]
                    or canonical_bytes(saved_artifact["capture_plan"]) != artifacts["capture_plan"]
                    or snapshot["captured_at"] != initial["captured_at"]
                    or snapshot["source_digest"] != initial["snapshot_digest"]
                    or canonical_digest(capture) != initial["capture_receipt_digest"]
                    or capture["query_count"] != initial["query_count"]
                    or capture["total_bytes_billed"] != initial["total_bytes_billed"]
                ):
                    raise ValueError("snapshot_recovery_invalid")
            elif (
                any(
                    initial[field] is not None
                    for field in (
                        "captured_at",
                        "snapshot_digest",
                        "capture_receipt_digest",
                        "stored_artifact",
                    )
                )
                or initial["query_count"] != 0
            ):
                raise ValueError("snapshot_capture_recovery_unavailable")
        phase = "creation"
        emit(phase, "started")
        created = _execute_validated_snapshot_creations(
            inputs["plan"],
            creation_statements=inputs["envelope"]["creation_statements"],
            source_metadata=artifacts["source_metadata"],
            manifest=manifest,
            consumption=consumption,
            recheck=recheck,
            claim=claim,
            client=client,
            now=current(),
            capture_plan=inputs["envelope"],
            recovery_context=inputs["recovery"],
            initial_result_reader=initial_result_reader,
            prior_records=initial["creation_records"] if initial is not None else (),
            metadata_diagnostics=lambda lane, reason: emit(
                "creation_metadata_" + lane, "waiting", "snapshot_creation_metadata_" + reason
            ),
            prior_evidence=saved_artifact["creation_evidence"]
            if saved_artifact is not None
            else (),
        )
        payload["creation_records"] = created["creation_records"]
        emit(phase, "succeeded")
        if saved_artifact is not None:
            if canonical_bytes(created["snapshot_metadata"]) != canonical_bytes(
                capture["assembly"]["snapshot_metadata"]
            ):
                raise ValueError("snapshot_recovery_invalid")
            payload["stored_artifact"] = stored
            emit("artifact_recovery", "succeeded")
            return payload, "succeeded"
        phase = "capture"
        _validate_policy(inputs["policy"], current())
        if (consumption.consumed_at + timedelta(seconds=600) - current()).total_seconds() < 180:
            raise ValueError("snapshot_operation_deadline")
        emit(phase, "started")
        capture_started = True
        capture = capture_production_snapshot(
            cutoff,
            client_scope_id=inputs["client_scope_id"],
            market_scope=inputs["market_scope"],
            reviewed_metadata=artifacts["source_metadata"],
            delegate=client,
            expected_creator_email=_IDENTITY,
        )
        payload["query_count"] = capture["query_count"]
        payload["total_bytes_billed"] = capture["total_bytes_billed"]
        capture = validate_captured_production_snapshot(
            capture,
            cutoff_date=cutoff,
            client_scope_id=inputs["client_scope_id"],
            market_scope=inputs["market_scope"],
            reviewed_metadata=artifacts["source_metadata"],
            expected_creator_email=_IDENTITY,
        )
        if canonical_bytes(created["snapshot_metadata"]) != canonical_bytes(
            capture["assembly"]["snapshot_metadata"]
        ):
            raise ValueError("snapshot_source_metadata_invalid")
        snapshot = capture["assembly"]["snapshot"]
        payload.update(
            captured_at=snapshot["captured_at"],
            snapshot_digest=snapshot["source_digest"],
            capture_receipt_digest=canonical_digest(capture),
            limitations=sorted(set(snapshot["coverage_limitations"])),
        )
        emit(phase, "succeeded")
        phase = "storage"
        _validate_policy(inputs["policy"], current())
        initial_manifest = _initial_capture_manifest(
            inputs["recovery"], consumption.manifest_sha256
        )
        artifact = {
            "contract_version": "open_intelligence_protected_source_capture_artifact_v1",
            "initial_manifest_sha256": initial_manifest,
            "capture_plan": inputs["envelope"],
            "capture": capture,
            "creation_evidence": created["creation_evidence"],
        }
        raw, attempt = objects.prepare_capture(artifact, initial_manifest_sha256=initial_manifest)
        payload["artifact_attempt"] = attempt
        emit(phase, "started")
        payload["stored_artifact"] = objects.store_capture(raw, attempt, timeout=remaining())
        emit(phase, "succeeded")
        return payload, "succeeded"
    except SnapshotCreationError as error:
        payload["creation_records"] = error.creation_records
        if initial is None or initial["query_count"] == 0:
            payload["total_bytes_billed"] = None
        code = "snapshot_creation_incomplete"
        if str(error) in {
            "snapshot_creation_job_invalid",
            "snapshot_creation_table_invalid",
            "snapshot_creation_failed",
            "snapshot_creation_billing_unresolved",
            "snapshot_creation_recovery_invalid",
            "snapshot_creation_recovery_target_exists",
            "snapshot_creation_size_unavailable",
            "snapshot_creation_size_exceeded",
            "snapshot_creation_expired",
        }:
            diagnostic_code = str(error)
        reasons = getattr(error, "failed_predicates", ())
        allowed_metadata_reasons = {
            "location",
            "reference",
            "creation_time",
            "type",
            "expiration",
            "snapshot_definition",
            "snapshot_time",
            "streaming_buffer",
            "schema",
            "row_count",
            "size",
            "retained_size",
        }
        if (
            type(reasons) is tuple
            and reasons
            and all(
                type(reason) is str and reason in allowed_metadata_reasons for reason in reasons
            )
        ):
            diagnostic_code = "snapshot_creation_metadata_" + reasons[0]
    except SnapshotCaptureError as error:
        payload["query_count"] = len(error.query_receipts)
        billed = [item.get("total_bytes_billed") for item in error.query_receipts]
        payload["total_bytes_billed"] = (
            sum(billed) if all(type(item) is int and item >= 0 for item in billed) else None
        )
        code = "snapshot_capture_incomplete"
    except SourceCaptureStorageError:
        code = "snapshot_artifact_unavailable"
    except Exception:
        if capture_started and capture is None:
            raise
        code = "snapshot_recovery_invalid" if mode == "recover" else "snapshot_operation_incomplete"
    payload["missing_checks"] = sorted(set(payload["missing_checks"]) | {code})
    emit(phase, "failed", diagnostic_code or code)
    return payload, "failed"


def _execution_approval_artifact_bytes(name):
    if type(_DURABLE_ARTIFACT_CONTEXT) is not dict or name not in _INPUTS:
        raise ValueError("snapshot_inputs_invalid")
    value = _DURABLE_ARTIFACT_CONTEXT.get(name)
    if type(value) is not bytes:
        raise ValueError("snapshot_inputs_invalid")
    return value


_COMPACT_RESULT_FIELDS = {
    "contract_version",
    "cutoff_date",
    "client_scope_id",
    "market_scope",
    "source_as_of",
    "captured_at",
    "snapshot_plan_digest",
    "snapshot_digest",
    "capture_receipt_digest",
    "creation_records",
    "artifact_attempt",
    "stored_artifact",
    "query_count",
    "total_bytes_billed",
    "limitations",
    "missing_checks",
}
_FORBIDDEN_RUNTIME_ENVIRONMENT = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GOOGLE_CLOUD_UNIVERSE_DOMAIN",
    "STORAGE_EMULATOR_HOST",
    "BIGQUERY_EMULATOR_HOST",
    "GCE_METADATA_HOST",
    "GCE_METADATA_ROOT",
    "GCE_METADATA_IP",
    "GCE_METADATA_TIMEOUT",
    "GCE_METADATA_DETECT_RETRIES",
    "GCE_METADATA_MTLS_MODE",
)


def _validate_cli_inputs(artifacts, argv, now):
    """Validate the inputs under the command line's own form, grant bound to grant form."""
    cutoff, mode, grant_id = _parse_arguments(argv)
    inputs = _validate_inputs(artifacts, cutoff, mode, now)
    policy = inputs["policy"]
    if grant_id is None:
        if policy.get("contract_version") != LEGACY_CAPTURE_POLICY_VERSION:
            raise ValueError("snapshot_cli_invalid")
        return inputs
    envelope = inputs["envelope"]
    if (
        policy.get("contract_version") != GRANT_CAPTURE_POLICY_VERSION
        or policy["grant"]["grant_id"] != grant_id
        or envelope.get("contract_version") != CAPTURE_PLAN_V2_VERSION
        or envelope["snapshot_plan"]["grant_id"] != grant_id
        or cutoff.isoformat() not in policy["grant"]["allowed_cutoffs"]
    ):
        raise ValueError("snapshot_inputs_invalid")
    return inputs


def _validate_runtime_environment():
    import os

    if (
        any(os.environ.get(name) for name in _FORBIDDEN_RUNTIME_ENVIRONMENT)
        or os.environ.get("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false") != "false"
        or os.environ.get("GOOGLE_API_USE_MTLS_ENDPOINT", "never") not in {"never", "auto"}
        or any(
            os.environ.get(name, _PROJECT) != _PROJECT
            for name in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT")
        )
    ):
        raise ValueError("snapshot_runtime_environment_invalid")


def _runtime_clients(identity=_IDENTITY):
    from google.auth.compute_engine import Credentials, _metadata
    from google.auth.transport.requests import Request
    from google.cloud import bigquery, storage
    from src.analysis.open_intelligence.production_snapshot_storage import SourceCaptureObjects

    _validate_runtime_environment()
    request = Request()
    project = _metadata.get(
        request,
        "project/project-id",
        root="http://metadata.google.internal/computeMetadata/v1/",
        retry_count=1,
    )
    credentials = Credentials(
        quota_project_id=_PROJECT,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
        universe_domain="googleapis.com",
    )
    credentials.refresh(request)
    if project != _PROJECT or credentials.service_account_email != identity:
        raise ValueError("snapshot_runtime_identity_invalid")
    query_client = bigquery.Client(project=_PROJECT, location="US", credentials=credentials)
    storage_client = storage.Client(project=_PROJECT, credentials=credentials)
    objects = SourceCaptureObjects(storage_client.bucket(_POLICY_CONSTANTS["bucket"]))
    return query_client, objects


def _source_consumption_writer(artifacts, *, query=None):
    from google.cloud import bigquery
    from src.analysis.open_intelligence import execution_approval

    boundary = execution_approval._runtime_query if query is None else query

    def write(request):
        parameters = [
            bigquery.ScalarQueryParameter("manifest_sha256", "STRING", request["manifest_sha256"]),
            bigquery.ScalarQueryParameter("execution_name", "STRING", request["execution_name"]),
            bigquery.ScalarQueryParameter("job_resource", "STRING", request["job_resource"]),
            bigquery.ScalarQueryParameter("source_sha", "STRING", request["source_sha"]),
            bigquery.ScalarQueryParameter("image_uri", "STRING", request["image_uri"]),
            bigquery.ScalarQueryParameter(
                "capture_plan_json", "STRING", artifacts["capture_plan"].decode("utf-8")
            ),
            bigquery.ScalarQueryParameter(
                "recovery_context_json", "STRING", artifacts["recovery_context"].decode("utf-8")
            ),
            bigquery.ScalarQueryParameter(
                "storage_policy_json", "STRING", artifacts["storage_policy"].decode("utf-8")
            ),
        ]
        row = boundary("sp_consume_open_intelligence_source_snapshot_v1", parameters)
        return {
            **request,
            "consumption_contract_version": execution_approval._runtime_row_value(
                row, "consumption_contract_version"
            ),
            "consumption_id": execution_approval._runtime_row_value(row, "consumption_id"),
            "consumed_at": execution_approval._runtime_row_value(row, "consumed_at"),
        }

    return write


def _validate_operation_profile(authority):
    manifest = getattr(authority, "manifest", None)
    limits = getattr(manifest, "limits", None)
    if (
        (not isinstance(limits, dict) and not hasattr(limits, "get"))
        or limits.get("max_bytes_billed") != 1_000_000_000
        or getattr(manifest, "timeout_seconds", None) != 600
    ):
        raise ValueError("snapshot_runtime_profile_invalid")


def _initial_result_reader(recovery, result_reader):
    from src.analysis.open_intelligence import execution_approval

    if (
        type(recovery) is not dict
        or not callable(result_reader)
        or type(recovery.get("initial_consumption_id")) is not str
        or type(recovery.get("initial_result_id")) is not str
    ):
        raise ValueError("snapshot_recovery_invalid")

    contexts = {recovery["initial_result_id"]: recovery}
    if recovery.get("contract_version") in {
        "open_intelligence_source_capture_recovery_v3",
        "open_intelligence_source_capture_recovery_v4",
    }:
        ancestor = recovery["ancestor_recovery_context"]
        if ancestor["initial_result_id"] in contexts:
            raise ValueError("snapshot_recovery_invalid")
        contexts[ancestor["initial_result_id"]] = ancestor
        if recovery["contract_version"] == "open_intelligence_source_capture_recovery_v4":
            origin = ancestor["ancestor_recovery_context"]
            if origin["initial_result_id"] in contexts:
                raise ValueError("snapshot_recovery_invalid")
            contexts[origin["initial_result_id"]] = origin
    cached = {}

    def read(result_id):
        if result_id not in contexts:
            raise ValueError("snapshot_recovery_invalid")
        if result_id in cached:
            return cached[result_id]
        rows = result_reader(contexts[result_id]["initial_consumption_id"])
        if not isinstance(rows, (list, tuple)) or len(rows) != 1:
            raise ValueError("snapshot_recovery_invalid")
        row = rows[0]
        if execution_approval._runtime_row_value(row, "result_id") != result_id:
            raise ValueError("snapshot_recovery_invalid")
        if len(contexts) > 1:
            cached[result_id] = row
        return row

    return read


def _manifest_artifact_digests(approval):
    try:
        payload = json.loads(approval.canonical_manifest_json)
        values = payload["input_artifacts"]
        pairs = {item["name"]: item["sha256"] for item in values}
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("snapshot_runtime_authority_invalid") from error
    if set(pairs) != _INPUTS | {"build_provenance"} or any(
        not _digest(value) for value in pairs.values()
    ):
        raise ValueError("snapshot_runtime_authority_invalid")
    return pairs


def _main_impl(
    argv,
    *,
    now,
    execution_reader,
    approval_reader,
    build_reader,
    runtime_clients,
    operation_runner,
    source_preflight,
    stdout,
    diagnostics,
    consumption_writer=None,
    control_query=None,
    result_writer=None,
    result_reader=None,
    bridge_runner=None,
):
    from functools import partial

    from src.analysis.open_intelligence import execution_approval

    cutoff, mode = _parse_cli(argv)
    _validate_runtime_environment()
    # The only fresh route is route A: the active generation's capture policy takes the v3
    # bridge plan. The retained v1 authority cannot be consumed again, so every other plan
    # refuses before any reader.
    rule = _bridge_capture_rule()
    if rule is not None and bridge_runner is not None:
        return bridge_runner(tuple(argv), rule=rule)
    raise ValueError("snapshot_fresh_route_unavailable")
    current = now()
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("snapshot_runtime_environment_invalid")
    current = current.astimezone(UTC)
    client, objects = runtime_clients()
    runtime_pair = execution_reader()
    try:
        _execution, job = execution_approval._runtime_read_pair(runtime_pair)
        annotations = job["template"]["annotations"]
        manifest_digest = annotations["42.ogilvy/execution-approval-sha256"]
    except (KeyError, TypeError) as error:
        raise ValueError("snapshot_runtime_authority_invalid") from error
    approval = approval_reader(manifest_digest)
    digests = _manifest_artifact_digests(approval)
    artifacts = {
        name: objects.read_input(name, digests[name], timeout=30) for name in sorted(_INPUTS)
    }
    build_cache = []

    def cached_build_reader(resource):
        if not build_cache:
            build_cache.append(build_reader(resource))
        return build_cache[0]

    global _DURABLE_ARTIFACT_CONTEXT
    if _DURABLE_ARTIFACT_CONTEXT is not None:
        raise ValueError("snapshot_runtime_context_invalid")
    _DURABLE_ARTIFACT_CONTEXT = artifacts
    try:
        authority = execution_approval._load_execution_authority(
            _OPERATION,
            mode="new_consume",
            execution_reader=lambda: runtime_pair,
            approval_reader=lambda _digest_value: approval,
            build_reader=cached_build_reader,
            artifact_reader=_execution_approval_artifact_bytes,
            now=lambda: current,
        )
        _validate_operation_profile(authority)
        inputs = _validate_cli_inputs(artifacts, argv, current)
        objects.preflight(timeout=30)
        source_preflight(inputs, client)
        writer = (
            _source_consumption_writer(artifacts, query=control_query)
            if consumption_writer is None
            else consumption_writer
        )
        consumption = execution_approval._consume_execution_authority(
            authority, consumption_writer=writer
        )
    finally:
        _DURABLE_ARTIFACT_CONTEXT = None
    recovery_reader = (
        None
        if inputs["recovery"] is None
        else _initial_result_reader(
            inputs["recovery"],
            partial(
                execution_approval._default_result_reader,
                version=execution_approval._RESULT_VERSION,
                mode="historical_replay",
            )
            if result_reader is None
            else result_reader,
        )
    )
    payload, status = operation_runner(
        artifacts,
        cutoff,
        mode,
        authority=authority,
        consumption=consumption,
        client=client,
        objects=objects,
        now=now(),
        initial_result_reader=recovery_reader,
        diagnostics=diagnostics,
    )
    if (
        type(payload) is not dict
        or set(payload) != _COMPACT_RESULT_FIELDS
        or status
        not in {
            "succeeded",
            "failed",
        }
    ):
        raise ValueError("snapshot_operation_result_invalid")
    canonical_result_json = canonical_bytes(payload).decode("utf-8")
    result_digest = hashlib.sha256(canonical_result_json.encode("utf-8")).hexdigest()
    record_kwargs = {}
    if result_writer is not None:
        record_kwargs["result_writer"] = result_writer
    if result_reader is not None:
        record_kwargs["result_reader"] = result_reader
    result = execution_approval._record_execution_result(
        authority,
        consumption,
        authority.execution_name + "#source-snapshot",
        canonical_result_json,
        result_digest,
        status,
        **record_kwargs,
    )
    output = {
        "payload": payload,
        "execution_result": {
            "manifest_sha256": authority.approval.manifest_sha256,
            "consumption_id": consumption.consumption_id,
            "result_id": result.result_id,
            "result_digest": result_digest,
            "status": status,
        },
    }
    stdout.write(canonical_bytes(output).decode("utf-8") + "\n")
    return 0 if status == "succeeded" else 1


# Route A: one approval ledger execution of the v3 bridge capture plan. The route opens only
# when the active generation's capture policy takes that plan; every other plan keeps the
# fresh route closed.
_BRIDGE_SCRIPT = "scripts/staging/capture_protected_production_snapshot.py"
_BRIDGE_INPUTS = (
    "bridge_policy",
    "temporal_rules",
    "collection_receipt_set",
    "history_completion_set",
)
_BRIDGE_RESULT_VERSION = "open_intelligence_protected_source_snapshot_v3"
_BRIDGE_CODE = re.compile(r"(?:snapshot|source_bridge|bridge)_[a-z0-9_]+")


def _bridge_capture_rule():
    """The active generation's v3 bridge capture rule, or None for every other generation."""
    from src.analysis.open_intelligence import execution_generations
    from src.analysis.open_intelligence.bridge_ledger_binding import (
        _capture_rule,
        is_bridge_ledger_generation,
    )

    generation = execution_generations.load_trusted_generation(
        *execution_generations.ACTIVE_GENERATION_PAIR
    )
    if not is_bridge_ledger_generation(generation.registry):
        return None
    return _capture_rule(generation.registry)


def _require_bridge_client(client, identity):
    from google.cloud import bigquery

    if (
        not isinstance(client, bigquery.Client)
        or client.project != _PROJECT
        or client.location not in (None, "US")
        or getattr(client._credentials, "service_account_email", None) != identity
        or client._default_query_job_config is not None
        or client._connection.API_BASE_URL != "https://bigquery.googleapis.com"
        or getattr(client._http, "is_mtls", False)
    ):
        raise ValueError("snapshot_runtime_identity_invalid")


@contextmanager
def _bridge_creation_transport(client, allowed, native_jobs):
    """Admit only the one pending creation POST and the current lane's job and clone reads,
    and keep the REST resource of every job read."""
    original = client._http.request

    def request(method, url, *args, **kwargs):
        parsed = urlsplit(url)
        if (
            args
            or parsed.scheme != "https"
            or parsed.netloc != "bigquery.googleapis.com"
            or parsed.fragment
        ):
            raise ValueError("snapshot_transport_invalid")
        if method == "POST":
            pending = allowed["post"]
            body = json.loads(kwargs.get("data") or "null")
            if (
                parsed.path != f"/bigquery/v2/projects/{_PROJECT}/jobs"
                or pending is None
                or type(body) is not dict
                or set(body) != {"jobReference", "configuration"}
                or body["jobReference"] != pending[0]
                or body["configuration"] != pending[1]
            ):
                raise ValueError("snapshot_transport_invalid")
            allowed["post"] = None
        elif method != "GET" or parsed.path not in allowed["gets"]:
            raise ValueError("snapshot_transport_invalid")
        response = original(method, url, **kwargs)
        if method == "GET" and "/jobs/" in parsed.path and response.status_code == 200:
            native_jobs[parsed.path.rsplit("/", 1)[1]] = copy.deepcopy(response.json())
        return response

    client._http.request = request
    try:
        yield
    finally:
        client._http.request = original


def _bridge_limitations(parsed):
    limitations = {"upstream_collection_completeness_unproven"}
    if any(
        row["state"] == "unavailable" and row["reason_code"] == "native_completion_unrecorded"
        for row in parsed["history_completion_set"]["entries"]
    ):
        limitations.add("native_completion_unrecorded")
    return sorted(limitations)


def _bridge_operation(
    plan, artifacts, *, parsed, storage, authority, consumption, client, objects, now, emit
):
    """Run the seven creation statements byte for byte, read back each job and clone, store
    the capture artifact and return the v3 result payload and its status."""
    from time import sleep

    from google.cloud import bigquery
    from src.analysis.open_intelligence import execution_approval
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.production_snapshot_storage import (
        _CAPTURE_VERSION_V2,
        SourceCaptureStorageError,
    )
    from src.analysis.open_intelligence.production_snapshot_tables import (
        _creation_config,
        _creation_job,
    )
    from src.analysis.open_intelligence.source_estate_bridge import (
        clone_fingerprint,
        validate_capture_facts,
    )
    from src.analysis.open_intelligence.source_estate_bridge_creation import (
        validate_creation_evidence,
    )
    from src.analysis.open_intelligence.source_estate_bridge_ledger_result import (
        validate_successful_ledger_bridge_result,
    )
    from src.analysis.open_intelligence.source_estate_bridge_result import _PROFILE_RESULT_FIELDS

    manifest = authority.manifest
    profile = plan["snapshot_plan"]
    relations = profile["relation_bindings"]
    identity = manifest.service_identity
    deadline = min(
        manifest.expires_at, consumption.consumed_at + timedelta(seconds=manifest.timeout_seconds)
    )

    def clock():
        value = now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("snapshot_runtime_environment_invalid")
        return value.astimezone(UTC)

    def remaining():
        seconds = (deadline - clock()).total_seconds()
        if seconds <= 0:
            raise ValueError("snapshot_operation_deadline")
        return min(30, seconds)

    plan_inputs = {
        "profile": profile,
        "source_metadata": json.loads(artifacts["source_metadata"]),
        "storage_policy": storage,
        "cutoff_date": plan["cutoff_date"],
        "client_scope_id": plan["client_scope_id"],
        "now": clock(),
        "artifacts": parsed,
    }
    payload = {key: copy.deepcopy(profile[key]) for key in _PROFILE_RESULT_FIELDS}
    payload.update(
        contract_version=_BRIDGE_RESULT_VERSION,
        cutoff_date=plan["cutoff_date"],
        client_scope_id=plan["client_scope_id"],
        market_scope=copy.deepcopy(plan["market_scope"]),
        captured_at=None,
        snapshot_plan_digest=canonical_digest(plan),
        snapshot_digest=None,
        capture_receipt_digest=None,
        creation_records=[],
        artifact_attempt=None,
        stored_artifact=None,
        query_count=0,
        total_bytes_billed=None,
        limitations=_bridge_limitations(parsed),
        missing_checks=[],
    )
    records, evidence, jobs, native_jobs = payload["creation_records"], [], {}, {}
    allowed = {"post": None, "gets": set()}
    phase = "creation"
    try:
        emit(phase, "started")
        with (
            _operation_transport(client, objects),
            _bridge_creation_transport(client, allowed, native_jobs),
        ):
            for relation, statement in zip(relations, plan["creation_statements"], strict=True):
                lane = relation["lane"]
                # The name the capture contract and the capture registry give the job.
                job_id = f"oi_v3_snapshot_{consumption.manifest_sha256}_{lane}"
                reference = {"projectId": _PROJECT, "location": "US", "jobId": job_id}
                project, dataset, table = relation["destination_table"].split(".")
                target = {"projectId": project, "datasetId": dataset, "tableId": table}
                allowed["gets"] = {
                    f"/bigquery/v2/projects/{_PROJECT}/jobs/{job_id}",
                    f"/bigquery/v2/projects/{project}/datasets/{dataset}/tables/{table}",
                }
                record = {**relation, "job_id": job_id, "native_job_digest": None}
                record["state"] = "unresolved"
                records.append(record)
                execution_approval._require_consumed_execution(authority, consumption, _OPERATION)
                config = _creation_config(statement["sql"])
                allowed["post"] = (reference, config)
                payload["query_count"] += 1
                try:
                    client.query(
                        statement["sql"],
                        job_id=job_id,
                        job_config=bigquery.QueryJobConfig.from_api_repr(config),
                        project=_PROJECT,
                        location="US",
                        retry=None,
                        job_retry=None,
                        timeout=remaining(),
                        api_method=bigquery.enums.QueryApiMethod.INSERT,
                    )
                except Exception:
                    # A lost acknowledgement only permits the exact job's readback.
                    pass
                finally:
                    allowed["post"] = None
                job = client.get_job(
                    job_id, project=_PROJECT, location="US", retry=None, timeout=remaining()
                )
                while True:
                    native = native_jobs.get(job_id)
                    if type(native) is not dict or native.get("jobReference") != reference:
                        raise ValueError("snapshot_creation_job_invalid")
                    state = native.get("status", {}).get("state")
                    if state == "DONE":
                        break
                    if state not in {"PENDING", "RUNNING"}:
                        raise ValueError("snapshot_creation_job_invalid")
                    sleep(min(1, remaining()))
                    job = client.get_job(
                        job_id, project=_PROJECT, location="US", retry=None, timeout=remaining()
                    )
                native = _creation_job(
                    job,
                    native=native,
                    job_id=job_id,
                    sql=statement["sql"],
                    identity=identity,
                    earliest=consumption.consumed_at,
                    latest=clock(),
                    target=target,
                )
                record["native_job_digest"] = canonical_digest(native)
                if native["status"].get("errorResult"):
                    record["state"] = "failed"
                    raise ValueError("snapshot_creation_failed")
                if native["statistics"]["query"].get("totalBytesBilled") != "0":
                    raise ValueError("snapshot_creation_billing_unresolved")
                resource = copy.deepcopy(
                    client.get_table(
                        relation["destination_table"], retry=None, timeout=remaining()
                    ).to_api_repr()
                )
                record["state"] = "succeeded"
                jobs[lane] = job
                evidence.append(
                    {
                        "lane": lane,
                        "manifest_sha256": consumption.manifest_sha256,
                        "consumption_id": consumption.consumption_id,
                        "execution_name": consumption.execution_name,
                        "native_job": native,
                        "snapshot_metadata": resource,
                    }
                )
            allowed["gets"] = set()
            observed = clock()
            checked = validate_creation_evidence(
                plan,
                evidence,
                plan_inputs=plan_inputs,
                jobs=jobs,
                manifest_sha256=consumption.manifest_sha256,
                consumption_id=consumption.consumption_id,
                execution_name=consumption.execution_name,
                writer_identity=identity,
                earliest=consumption.consumed_at,
                observed_at=observed,
                max_bytes_billed=manifest.limits["max_bytes_billed"],
            )
            if canonical_bytes(checked["creation_records"]) != canonical_bytes(records):
                raise ValueError("bridge_creation_evidence_invalid")
            emit(phase, "succeeded")
            phase = "capture"
            captured_at = execution_approval._format_timestamp(
                observed, "snapshot_operation_incomplete"
            )
            # The readback digest is the clone fingerprint's, the one the reader recomputes
            # from tables.get at request time.
            readbacks = []
            for readback, entry in zip(checked["relation_readbacks"], evidence, strict=True):
                readbacks.append(
                    {key: readback[key] for key in readback if key != "metadata_digest"}
                    | {
                        "metadata_digest": canonical_digest(
                            clone_fingerprint(entry["snapshot_metadata"])
                        )
                    }
                )
            facts = validate_capture_facts(
                {
                    "contract_version": "open_intelligence_source_bridge_capture_v1",
                    "profile_digest": canonical_digest(profile),
                    "relation_readbacks": readbacks,
                    **{
                        key: profile[key]
                        for key in (
                            "collection_receipt_set_digest",
                            "history_completion_set_digest",
                            "projection_version",
                            "temporal_rules_version",
                            "temporal_rules_digest",
                        )
                    },
                    "captured_at": captured_at,
                },
                profile=profile,
                artifacts=parsed,
                source_estate_digest=storage["grant"]["source_estate_digest"],
                creation_completed_at=checked["creation_completed_at"],
            )
            phase = "storage"
            emit(phase, "started")
            artifact = {
                "contract_version": _CAPTURE_VERSION_V2,
                "initial_manifest_sha256": consumption.manifest_sha256,
                "capture_plan": plan,
                "capture": facts,
                "creation_evidence": evidence,
            }
            raw, attempt = objects.prepare_capture(
                artifact,
                initial_manifest_sha256=consumption.manifest_sha256,
                artifact_version=_CAPTURE_VERSION_V2,
            )
            payload["artifact_attempt"] = attempt
            stored = objects.store_capture(
                raw, attempt, timeout=remaining(), artifact_version=_CAPTURE_VERSION_V2
            )
        emit(phase, "succeeded")
        phase = "result"
        payload.update(
            captured_at=captured_at,
            snapshot_digest=canonical_digest(facts),
            capture_receipt_digest=canonical_digest(records),
            stored_artifact=stored,
            total_bytes_billed=checked["total_bytes_billed"],
        )
        validate_successful_ledger_bridge_result(
            payload,
            plan=plan,
            plan_inputs=plan_inputs,
            capture_facts=facts,
            creation_records=records,
            creation_completed_at=checked["creation_completed_at"],
            artifact_attempt=attempt,
            stored_artifact=stored,
            execution_status="succeeded",
            native_metering={
                "query_count": payload["query_count"],
                "total_bytes_billed": checked["total_bytes_billed"],
            },
            native_jobs={record["job_id"]: native_jobs[record["job_id"]] for record in records},
            authority=authority,
            capture_consumption=consumption,
            # The tables.get resources the creation evidence recorded, one per clone.
            clone_metadata={
                relation["destination_table"]: entry["snapshot_metadata"]
                for relation, entry in zip(relations, evidence, strict=True)
            },
        )
        return payload, "succeeded"
    except SourceCaptureStorageError:
        code = "snapshot_artifact_unavailable"
    except Exception as error:
        text = str(error) if isinstance(error, ValueError) else ""
        code = text if _BRIDGE_CODE.fullmatch(text) else "snapshot_operation_incomplete"
    for field in ("captured_at", "snapshot_digest", "capture_receipt_digest", "stored_artifact"):
        payload[field] = None
    payload["total_bytes_billed"] = None
    payload["missing_checks"] = [code]
    emit(phase, "failed", code)
    return payload, "failed"


def _capture_evidence_readers(client):
    """The capture evidence readers, run with the runtime client's own credentials.

    ``read_chain(consumption_id)`` is the approval ledger's chain statement, one capped
    query with no retry; ``read_funded_execution(execution_id)`` is one Cloud Run v2 GET of
    a pinned funded pilot execution with no redirect and no refresh retry. The packaged
    catalogue decides which generations the chain may record (``generation_loader`` None).
    """
    from types import SimpleNamespace

    from src.analysis.open_intelligence import bridge_native_clients as native

    credentials = client._credentials
    return SimpleNamespace(
        read_chain=native.ledger_reader(native.BridgeWarehouse(credentials)),
        read_funded_execution=native.BridgeFundedExecutions(credentials).read,
        generation_loader=None,
    )


def _run_bridge_capture(
    argv,
    *,
    rule,
    now,
    execution_reader,
    approval_reader,
    build_reader,
    runtime_clients,
    bridge_evidence_readers,
    consume_query,
    result_writer,
    result_reader,
    stdout,
    diagnostics,
):
    """Run one approved initial capture of the v3 bridge plan under the approval ledger.

    Every check that can refuse without spending the approval runs before consumption: the
    command line is the approved vector, the clients are the policy identity's, the inputs
    are the manifest's digests, the recovery context is null and the plan regenerates from
    the approved bytes. After consumption every outcome is recorded as the execution's one
    result, succeeded only when the approval-route result check admits it.
    """
    from src.analysis.open_intelligence import execution_approval
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        parse_bridge_artifacts,
    )

    cutoff, mode, grant_id = _parse_arguments(argv)
    if grant_id is None or mode != "initial":
        raise ValueError("snapshot_cli_invalid")
    names = tuple(rule["input_artifact_names"])
    identity = rule["service_identity"]
    current = now()
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("snapshot_runtime_environment_invalid")
    current = current.astimezone(UTC)
    client, objects = runtime_clients(identity)
    _require_bridge_client(client, identity)
    runtime_pair = execution_reader()
    try:
        _execution, job = execution_approval._runtime_read_pair(runtime_pair)
        manifest_digest = job["template"]["annotations"]["42.ogilvy/execution-approval-sha256"]
    except (KeyError, TypeError) as error:
        raise ValueError("snapshot_runtime_authority_invalid") from error
    approval = approval_reader(manifest_digest)
    try:
        items = json.loads(approval.canonical_manifest_json)["input_artifacts"]
        digests = {item["name"]: item["sha256"] for item in items}
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("snapshot_runtime_authority_invalid") from error
    if (
        len(items) != len(names)
        or set(digests) != set(names)
        or any(not _digest(value) for value in digests.values())
    ):
        raise ValueError("snapshot_runtime_authority_invalid")
    artifacts = {name: objects.read_input(name, digests[name], timeout=30) for name in names}
    # Route A is an initial capture: no recovery context is approved with it.
    if artifacts["recovery_context"] != b"null":
        raise ValueError("snapshot_recovery_invalid")
    build_cache = []

    def cached_build_reader(resource):
        if not build_cache:
            build_cache.append(build_reader(resource))
        return build_cache[0]

    authority = execution_approval._load_source_snapshot_authority(
        mode="new_consume",
        execution_reader=lambda: runtime_pair,
        approval_reader=lambda _digest_value: approval,
        build_reader=cached_build_reader,
        artifact_reader=artifacts.__getitem__,
        now=lambda: current,
    )
    manifest = authority.manifest
    _validate_operation_profile(authority)
    if manifest.arguments != (_BRIDGE_SCRIPT, *argv):
        raise ValueError("snapshot_cli_invalid")
    if manifest.service_identity != identity or manifest.job_resource != rule["job_resource"]:
        raise ValueError("snapshot_runtime_identity_invalid")
    plan = execution_approval.prepare_source_snapshot_capture_v3(
        dict(manifest.input_artifacts),
        artifact_reader=artifacts.__getitem__,
        rule=rule["arguments"],
        now=current,
    )
    storage = json.loads(artifacts["storage_policy"])
    if (
        canonical_bytes(plan) != artifacts["capture_plan"]
        or plan["cutoff_date"] != cutoff.isoformat()
        or plan["snapshot_plan"]["grant_id"] != grant_id
        or storage["grant"]["grant_id"] != grant_id
        or storage.get("bucket") != objects.bucket.name
    ):
        raise ValueError("snapshot_plan_invalid")
    parsed = parse_bridge_artifacts({name: json.loads(artifacts[name]) for name in _BRIDGE_INPUTS})
    objects.preflight(timeout=30)
    # A callable builds the evidence readers from the runtime client, so they read as the
    # policy identity the client was just checked to carry.
    readers = (
        bridge_evidence_readers(client)
        if callable(bridge_evidence_readers)
        else bridge_evidence_readers
    )
    consumption = execution_approval._consume_source_snapshot_authority(
        authority,
        artifact_reader=artifacts.__getitem__,
        query=consume_query,
        bridge_evidence_readers=readers,
    )

    def emit(phase, state, code=None):
        if diagnostics is not None:
            diagnostics(
                {
                    "operation": _OPERATION,
                    "execution_name": consumption.execution_name,
                    "consumption_id": consumption.consumption_id,
                    "phase": phase,
                    "state": state,
                    "code": code,
                }
            )

    payload, status = _bridge_operation(
        plan,
        artifacts,
        parsed=parsed,
        storage=storage,
        authority=authority,
        consumption=consumption,
        client=client,
        objects=objects,
        now=now,
        emit=emit,
    )
    canonical_result_json = canonical_bytes(payload).decode("utf-8")
    result_digest = hashlib.sha256(canonical_result_json.encode("utf-8")).hexdigest()
    record_kwargs = {}
    if result_writer is not None:
        record_kwargs["result_writer"] = result_writer
    if result_reader is not None:
        record_kwargs["result_reader"] = result_reader
    result = execution_approval._record_execution_result(
        authority,
        consumption,
        consumption.execution_name + "#source-snapshot",
        canonical_result_json,
        result_digest,
        status,
        **record_kwargs,
    )
    output = {
        "payload": payload,
        "execution_result": {
            "manifest_sha256": authority.approval.manifest_sha256,
            "consumption_id": consumption.consumption_id,
            "result_id": result.result_id,
            "result_digest": result_digest,
            "status": status,
        },
    }
    stdout.write(canonical_bytes(output).decode("utf-8") + "\n")
    return 0 if status == "succeeded" else 1


def _safe_diagnostic(event):
    import sys

    fields = ("operation", "execution_name", "consumption_id", "phase", "state", "code")
    if type(event) is not dict or any(
        key in event and event[key] is not None and type(event[key]) is not str for key in fields
    ):
        value = {
            "operation": _OPERATION,
            "phase": "diagnostics",
            "state": "failed",
            "code": "source_snapshot_capture_refused",
        }
    else:
        value = {key: event[key] for key in fields if key in event}
    sys.stderr.write(canonical_bytes(value).decode("utf-8") + "\n")


_BOOTSTRAP_REFUSAL_CODES = frozenset(
    {
        "snapshot_cli_invalid",
        "snapshot_inputs_invalid",
        "snapshot_storage_policy_invalid",
        "snapshot_price_review_invalid",
        "snapshot_recovery_invalid",
        "snapshot_runtime_environment_invalid",
        "snapshot_runtime_identity_invalid",
        "snapshot_runtime_authority_invalid",
        "snapshot_runtime_context_invalid",
        "snapshot_runtime_profile_invalid",
        "snapshot_fresh_route_unavailable",
        "snapshot_source_metadata_invalid",
        "snapshot_source_size_exceeded",
        "snapshot_plan_invalid",
        "storage_preflight_invalid",
        "input_invalid",
        "input_unavailable",
        "execution_approval_target_invalid",
        "execution_approval_unavailable",
        "execution_approval_consumed",
        "execution_approval_expired",
        "execution_approval_concurrent_conflict",
        "execution_approval_identity_invalid",
        "execution_approval_execution_mismatch",
        "execution_approval_schema_mismatch",
        "execution_approval_manifest_invalid",
        "execution_approval_build_provenance_invalid",
        "execution_approval_artifact_mismatch",
        "execution_approval_internal_refusal",
        "execution_approval_query_budget_exceeded",
        "source_snapshot_plan_invalid",
        "bridge_payload_contract_unavailable",
        "bridge_evidence_readers_unavailable",
        "bridge_evidence_unavailable",
    }
)


def main(argv=None):
    import sys
    from functools import partial

    from src.analysis.open_intelligence import execution_approval

    values = tuple(sys.argv[1:] if argv is None else argv)
    try:
        query = execution_approval._source_control_query()
        return _main_impl(
            values,
            now=lambda: datetime.now(UTC),
            execution_reader=execution_approval._default_execution_reader,
            # Retained readers only: every default adapter is bound to the v1 record
            # literal under historical replay, and the writer refuses under that pair.
            approval_reader=partial(
                execution_approval._default_approval_reader,
                version=execution_approval._APPROVAL_VERSION,
                mode="historical_replay",
                query=query,
            ),
            build_reader=execution_approval._default_build_reader,
            runtime_clients=_runtime_clients,
            operation_runner=_run_operation,
            source_preflight=_source_preflight,
            stdout=sys.stdout,
            diagnostics=_safe_diagnostic,
            result_reader=partial(
                execution_approval._default_result_reader,
                version=execution_approval._RESULT_VERSION,
                mode="historical_replay",
                query=query,
            ),
            result_writer=partial(
                execution_approval._default_result_writer,
                version=execution_approval._RESULT_VERSION,
                mode="historical_replay",
                query=query,
            ),
            control_query=query,
            bridge_runner=partial(
                _run_bridge_capture,
                now=lambda: datetime.now(UTC),
                execution_reader=execution_approval._default_execution_reader,
                approval_reader=partial(
                    execution_approval._default_approval_reader,
                    version=execution_approval._APPROVAL_VERSION_V2,
                    mode="new_consume",
                    query=query,
                ),
                build_reader=execution_approval._default_build_reader,
                runtime_clients=_runtime_clients,
                # The funded pilot's ledger result row and its Cloud Run execution, read
                # as the runtime identity; any failed or denied read refuses before the
                # consumer spends the approval.
                bridge_evidence_readers=_capture_evidence_readers,
                consume_query=query,
                result_writer=partial(
                    execution_approval._default_result_writer,
                    version=execution_approval._RESULT_VERSION_V2,
                    mode="new_consume",
                    query=query,
                ),
                result_reader=partial(
                    execution_approval._default_result_reader,
                    version=execution_approval._RESULT_VERSION_V2,
                    mode="new_consume",
                    query=query,
                ),
                stdout=sys.stdout,
                diagnostics=_safe_diagnostic,
            ),
        )
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) else None
        if (
            isinstance(error, execution_approval.ApprovalRefusal)
            and code == "execution_approval_concurrent_conflict"
            and isinstance(error.__cause__, execution_approval.ApprovalRefusal)
            and str(error.__cause__) == "execution_approval_query_budget_exceeded"
        ):
            code = "execution_approval_query_budget_exceeded"
        _safe_diagnostic(
            {
                "operation": _OPERATION,
                "phase": "bootstrap",
                "state": "failed",
                "code": code
                if code in _BOOTSTRAP_REFUSAL_CODES
                else "source_snapshot_capture_refused",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
