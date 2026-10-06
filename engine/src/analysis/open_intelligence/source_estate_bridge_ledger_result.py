"""The approval-route result check for a bridge v3 capture on the approval ledger.

The daily route binds the clones to a daily consumption's child identity. On the approval
route the principal is the approved manifest's service identity, the cutoff and grant are
the approved vector's, and the estate digest is the one the grant of the storage policy the
manifest digests carries. Every other check is the daily route's own, run by the same code
(``check_successful_bridge_result``), and each creation job must also be created, started
and ended between the consumption and the capture, so this check is never weaker than the
daily check.
"""

import hashlib
from datetime import UTC, datetime, time, timedelta

from .brain_contract import canonical_bytes
from .source_estate_bridge import _parse_timestamp
from .source_estate_bridge_result import check_successful_bridge_result

_CODE = "source_bridge_result_invalid"
_BRIDGE_INPUTS = (
    "bridge_policy",
    "temporal_rules",
    "collection_receipt_set",
    "history_completion_set",
)


def _sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _principal(profile, *, plan, plan_inputs, authority, capture_consumption):
    from . import execution_approval
    from .bridge_ledger_binding import LEDGER_OPERATION, _capture_rule

    try:
        execution_approval._require_consumed_execution(
            authority, capture_consumption, LEDGER_OPERATION
        )
        manifest = authority.manifest
        rule = _capture_rule(authority.generation.registry)
        vector = execution_approval.read_source_snapshot_capture_arguments_v2(
            manifest.arguments, rule=rule["arguments"]
        )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(_CODE) from error
    digests = dict(manifest.input_artifacts)
    storage = plan_inputs["storage_policy"]
    grant = storage.get("grant") if type(storage) is dict else None
    window_end = datetime.combine(vector.cutoff_date + timedelta(days=1), time.min, tzinfo=UTC)
    if (
        manifest.operation != LEDGER_OPERATION
        or capture_consumption.manifest_sha256 != authority.approval.manifest_sha256
        or capture_consumption.execution_name != authority.execution_name
        or type(grant) is not dict
        or _parse_timestamp(profile["observation_window_end"], _CODE) != window_end
        or plan["cutoff_date"] != vector.cutoff_date.isoformat()
        or vector.grant_id != profile["grant_id"]
        or grant.get("grant_id") != vector.grant_id
        or grant.get("source_estate_digest") != profile["source_estate_digest"]
        # The inputs the plan is compared under are the approved bytes, never the caller's.
        or _sha256(plan) != digests.get("capture_plan")
        or _sha256(storage) != digests.get("storage_policy")
        or _sha256(plan_inputs["source_metadata"]) != digests.get("source_metadata")
        or type(plan_inputs["artifacts"]) is not dict
        or any(
            _sha256(plan_inputs["artifacts"].get(name)) != digests.get(name)
            for name in _BRIDGE_INPUTS
        )
    ):
        raise ValueError(_CODE)
    return manifest.service_identity, grant["source_estate_digest"], capture_consumption.consumed_at


def validate_successful_ledger_bridge_result(
    value,
    *,
    plan,
    plan_inputs,
    capture_facts,
    creation_records,
    creation_completed_at,
    artifact_attempt,
    stored_artifact,
    execution_status,
    native_metering,
    native_jobs,
    authority,
    capture_consumption,
    clone_metadata,
):
    """``authority`` is the consumed approval-ledger authority the capture ran under and
    ``capture_consumption`` its bound consumption; the approved manifest's service identity
    is the only principal whose jobs can create the clones. ``clone_metadata`` is the
    tables.get resource of each clone, keyed by destination table."""

    def principal(profile):
        return _principal(
            profile,
            plan=plan,
            plan_inputs=plan_inputs,
            authority=authority,
            capture_consumption=capture_consumption,
        )

    return check_successful_bridge_result(
        value,
        plan=plan,
        plan_inputs=plan_inputs,
        capture_facts=capture_facts,
        creation_records=creation_records,
        creation_completed_at=creation_completed_at,
        artifact_attempt=artifact_attempt,
        stored_artifact=stored_artifact,
        execution_status=execution_status,
        native_metering=native_metering,
        native_jobs=native_jobs,
        clone_metadata=clone_metadata,
        principal=principal,
    )
