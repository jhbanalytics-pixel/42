"""Construct historical comparison profiles without granting capture authority."""

import re
from datetime import datetime
from uuid import uuid4

from .brain_contract import canonical_bytes, canonical_digest
from .daily_execution_contracts import _instant
from .execution_approval import _format_timestamp
from .historical_connector_contract import validate_policy, validate_profile


def _normalized_instant(value):
    code = "historical_timestamp_invalid"
    try:
        if isinstance(value, str):
            if any(
                any(digit != "0" for digit in part[6:]) for part in re.findall(r"[.,](\d+)", value)
            ):
                raise ValueError(code)
            value = _instant(value, code)
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(code)
        nanosecond = getattr(value, "nanosecond", 0)
        if type(nanosecond) is not int or nanosecond != 0:
            raise ValueError(code)
        return _format_timestamp(value, code)
    except (OverflowError, TypeError) as exc:
        raise ValueError(code) from exc


def build_historical_profile(
    *,
    policy,
    client_scope_id,
    snapshot_as_of,
    observed_at,
    source_schema_digests,
    projection_version,
    retry_profile=None,
):
    """Build a fresh profile or compare an exact prior profile for retry planning.

    A matching prior profile proves no native ownership or completed operation.
    """
    policy = validate_policy(policy)
    snapshot_as_of = _normalized_instant(snapshot_as_of)
    observed_at = _normalized_instant(observed_at)
    if type(source_schema_digests) is not dict or set(source_schema_digests) != set(
        policy["relations"]
    ):
        raise ValueError("historical_schema_digests_invalid")
    previous = None
    if retry_profile is not None:
        try:
            previous = validate_profile(
                retry_profile,
                policy=policy,
                observed_at=observed_at,
                expected_client_scope_id=client_scope_id,
            )
        except ValueError as exc:
            raise ValueError("historical_capture_identity_conflict") from exc
        capture_id = previous["capture_id"]
    else:
        capture_id = f"hc_{uuid4().hex}"
    profile = {
        "contract_version": "historical_connector_profile_v1",
        "profile_id": f"historical_connector_{capture_id}_v1",
        "capture_id": capture_id,
        "policy_digest": canonical_digest(policy),
        "client_scope_id": client_scope_id,
        "market_scope": policy["market_scope"],
        "snapshot_as_of": snapshot_as_of,
        "relation_bindings": [
            {
                "lane": lane,
                "source_table": f"{policy['project']}.{policy['source_dataset']}.{lane}",
                "destination_table": f"{policy['project']}.{policy['snapshot_dataset']}.historical_connector_{capture_id}_{lane}",
                "snapshot_as_of": snapshot_as_of,
                "source_schema_digest": source_schema_digests[lane],
            }
            for lane in policy["relations"]
        ],
        "projection_version": projection_version,
        "temporal_rules_digest": policy["temporal_rules_digest"],
        "identity_policy_digest": policy["identity_policy_digest"],
    }
    profile = validate_profile(
        profile,
        policy=policy,
        observed_at=observed_at,
        expected_client_scope_id=client_scope_id,
    )
    if previous is not None and canonical_bytes(profile) != canonical_bytes(previous):
        raise ValueError("historical_capture_identity_conflict")
    return profile
