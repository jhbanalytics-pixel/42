"""Versioned staging source profile and the capture policy closure.

A staging source profile binds one completed D01 source run to the instant its rows are
snapshotted. The closed observation window (the cutoff day, ending at the next UTC
midnight) is carried separately from ``snapshot_as_of``, the real instant the snapshot
reads, which must contain the run's writes and may never be in the future. The profile
carries its native identity projection as a plain version string; the projection itself
lives in the row preparation module and is selected by that string. Nothing that runs
today reads under the bound projection: the capture path takes the row preparation
module's default, which is the legacy projection, and the one retained context profile is
mapped to the legacy projection as well. The versioned selection is in place; a capture
bound to the bound projection is not.

The legacy protected capture keeps its own profile, dataset and projection; nothing here
redirects the protected reader or replaces its hard coded dataset.

The capture policy closure is the second half: one accepted policy record per version is
the source of the storage policy constants, the contract digest, the permitted cutoffs,
the reservation and the modeled cost bound. The operator command reads its constants
from here. Storage and compute prices are a measured input record the operator supplies,
never a live read, and the published assumptions are rewritten from those measured fields.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction
from urllib.parse import urlsplit

from scripts.staging.collect_42_sources import (
    BOOTSTRAP_MARKETS,
    COLLECTED,
    RECEIPT_CONTRACT_VERSION,
    STAGING_SOURCE_DATASET,
    TERMINAL_STATES,
)

from src.analysis.open_intelligence.capture_registry import READ_STATUSES, validate_capture_entry
from src.analysis.open_intelligence.production_snapshot_tables import (
    DESTINATION_DATASET,
    LANES,
    PROJECT,
    SOURCE_DATASET,
    snapshot_destination_table,
)
from src.analysis.open_intelligence.protected_context_registry import allowed_cutoffs

LEGACY_PROFILE_VERSION = "42_capture_v1"
NATIVE_IDENTITY_PROFILE_VERSION = "42_staging_source_v2"
LEGACY_PROJECTION_VERSION = "v3_absent_nullable_v1"
NATIVE_IDENTITY_PROJECTION_VERSION = "native_id_bound_v1"
STAGING_SOURCE_KIND = "staging_source_run"
LEGACY_SOURCE_KIND = "protected_source_capture"
SOURCE_LANES = tuple(sorted(LANES))
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GRANT_ID = re.compile(r"[a-z0-9_]+\Z")
_RECEIPT_FIELDS = frozenset(
    {
        "contract_version",
        "run_id",
        "execution_id",
        "source_sha",
        "image_uri",
        "policy_sha256",
        "profile_sha256",
        "cutoff",
        "market_states",
        "raw_rows_persisted",
        "enriched_rows_persisted",
        "funded_close",
        "complete",
    }
)
# The receipt names the kind of authority that produced it when the boundary
# placed the run under one it verified. A receipt that names none was produced
# under no manifest read there, which is the older shape and stays admissible.
_RECEIPT_AUTHORITY_KINDS = frozenset({"verified_manifest", "unverified"})
_RECEIPT_FIELD_SETS = (_RECEIPT_FIELDS, _RECEIPT_FIELDS | {"authority_kind"})
_RUN_FIELDS = frozenset({"receipt", "collection_started_at", "collection_completed_at"})


def _aware(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(code)
    return value.astimezone(UTC)


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(code)
    return value


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


# Profiles


@dataclass(frozen=True)
class LegacyCaptureProfile:
    """The protected capture as it already exists: old projection, old dataset, old reader."""

    profile_version: str
    projection_version: str
    source_kind: str
    source_project: str
    source_dataset: str
    snapshot_dataset: str
    source_tables: tuple[str, ...]


LEGACY_PROFILE = LegacyCaptureProfile(
    profile_version=LEGACY_PROFILE_VERSION,
    projection_version=LEGACY_PROJECTION_VERSION,
    source_kind=LEGACY_SOURCE_KIND,
    source_project=PROJECT,
    # Descriptive metadata of the retained capture, read from the tables module that
    # owns those names; the protected reader keeps its own literal and is not redirected.
    source_dataset=SOURCE_DATASET,
    snapshot_dataset=DESTINATION_DATASET,
    source_tables=SOURCE_LANES,
)


def profile_for_version(profile_version: str) -> LegacyCaptureProfile:
    """Historical profiles are looked up; staging source profiles are built from a run."""
    if profile_version == LEGACY_PROFILE_VERSION:
        return LEGACY_PROFILE
    raise ValueError("profile_version_unknown")


@dataclass(frozen=True)
class StagingSourceProfile:
    profile_version: str
    projection_version: str
    profile_id: str
    source_kind: str
    source_project: str
    source_dataset: str
    snapshot_dataset: str
    source_tables: tuple[str, ...]
    snapshot_tables: tuple[str, ...]
    scope: str
    markets: tuple[str, ...]
    observation_window_end: datetime
    collection_started_at: datetime
    collection_completed_at: datetime
    snapshot_as_of: datetime
    run_id: str
    execution_id: str
    source_sha: str
    image_uri: str
    policy_digest: str
    schema_digest: str
    source_digest: str

    def instants(self) -> dict[str, datetime]:
        return {
            "observation_window_end": self.observation_window_end,
            "collection_started_at": self.collection_started_at,
            "collection_completed_at": self.collection_completed_at,
            "snapshot_as_of": self.snapshot_as_of,
        }

    def capture_entry(
        self,
        *,
        operation_id: str,
        result_id: str,
        content_digest: str,
        image_digest: str,
        generation: str,
        completion_state: str,
        capture_available_at: datetime,
        expires_at: datetime | None = None,
    ) -> dict:
        """The registry entry of one capture attempt of this profile, grammar validated."""
        entry = {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "operation_id": operation_id,
            "result_id": result_id,
            "source_kind": self.source_kind,
            "source_tables": self.source_tables,
            "snapshot_tables": self.snapshot_tables,
            "scope": self.scope,
            "markets": self.markets,
            "observation_window_end": self.observation_window_end,
            "collection_started_at": self.collection_started_at,
            "collection_completed_at": self.collection_completed_at,
            "snapshot_as_of": self.snapshot_as_of,
            "capture_available_at": capture_available_at,
            "content_digest": content_digest,
            "schema_digest": self.schema_digest,
            "policy_digest": self.policy_digest,
            "source_digest": self.source_digest,
            "image_digest": image_digest,
            "generation": generation,
            "completion_state": completion_state,
        }
        if expires_at is not None:
            entry["expires_at"] = expires_at
        return validate_capture_entry(entry)


def validate_completed_source_run(run: object) -> dict:
    """Validate a D01 completed source run with its recorded collection instants."""
    code = "source_run_invalid"
    if not isinstance(run, Mapping) or set(run) != _RUN_FIELDS:
        raise ValueError(code)
    receipt = run["receipt"]
    if not isinstance(receipt, Mapping) or set(receipt) not in _RECEIPT_FIELD_SETS:
        raise ValueError(code)
    if receipt["contract_version"] != RECEIPT_CONTRACT_VERSION:
        raise ValueError(code)
    if "authority_kind" in receipt and receipt["authority_kind"] not in _RECEIPT_AUTHORITY_KINDS:
        raise ValueError(code)
    for field in ("run_id", "execution_id", "source_sha", "image_uri", "profile_sha256"):
        _text(receipt[field], code)
    _digest(receipt["policy_sha256"], code)
    cutoff_text = receipt["cutoff"]
    if not isinstance(cutoff_text, str):
        raise ValueError(code)
    try:
        cutoff = date.fromisoformat(cutoff_text)
    except ValueError as error:
        raise ValueError(code) from error
    if cutoff.isoformat() != cutoff_text:
        raise ValueError(code)
    states = receipt["market_states"]
    if (
        not isinstance(states, Mapping)
        or set(states) != set(BOOTSTRAP_MARKETS)
        or any(state not in TERMINAL_STATES for state in states.values())
        or not _count(receipt["raw_rows_persisted"])
        or not _count(receipt["enriched_rows_persisted"])
        or type(receipt["complete"]) is not bool
        or not (receipt["funded_close"] is None or isinstance(receipt["funded_close"], Mapping))
    ):
        raise ValueError(code)
    if receipt["complete"] is not True or any(state != COLLECTED for state in states.values()):
        raise ValueError("source_run_incomplete")
    started = _aware(run["collection_started_at"], code)
    completed = _aware(run["collection_completed_at"], code)
    if completed < started:
        raise ValueError(code)
    window_end = datetime.combine(cutoff + timedelta(days=1), time.min, UTC)
    if started < window_end:
        raise ValueError("source_run_window_open")
    return {
        "receipt": dict(receipt),
        "cutoff": cutoff,
        "observation_window_end": window_end,
        "collection_started_at": started,
        "collection_completed_at": completed,
    }


def read_completed_source_run(reader, run_id: str) -> dict:
    """Read one completed run through a read only reader, refusing every non ok outcome."""
    if not callable(reader):
        raise ValueError("source_run_reader_invalid")
    outcome = reader(run_id)
    if not isinstance(outcome, Mapping) or outcome.get("status") not in READ_STATUSES:
        raise ValueError("source_run_read_invalid")
    status = outcome["status"]
    if status == "access_denied":
        raise ValueError("source_run_read_access_denied")
    if status == "metadata_unavailable":
        raise ValueError("source_run_read_metadata_unavailable")
    if status == "invalid_authority":
        raise ValueError("source_run_read_authority_invalid")
    run = outcome.get("run")
    if not isinstance(run, Mapping):
        raise ValueError("source_run_read_invalid")
    validated = validate_completed_source_run(run)
    if validated["receipt"]["run_id"] != run_id:
        raise ValueError("source_run_invalid")
    return validated


def staging_profile_id(cutoff: date, policy_sha256: str) -> str:
    """The profile id a daily capture of this cutoff under this source policy registers as.

    The candidate binding derives the capture it requires from the collection it bound
    through this same function, so the two cannot name different profiles.
    """
    if type(cutoff) is not date:
        raise ValueError("profile_input_invalid")
    return f"staging-{cutoff.isoformat()}-{_digest(policy_sha256, 'profile_input_invalid')[:16]}"


def build_staging_source_profile(
    run: object,
    *,
    snapshot_as_of: datetime,
    scope: str,
    schema_digest: str,
    source_digest: str,
    now: datetime,
) -> StagingSourceProfile:
    """Bind a completed run to the exact instant its snapshot reads."""
    validated = validate_completed_source_run(run)
    snapshot = _aware(snapshot_as_of, "snapshot_instant_invalid")
    current = _aware(now, "snapshot_instant_invalid")
    if snapshot < validated["collection_completed_at"]:
        raise ValueError("source_run_snapshot_precedes_writes")
    if snapshot > current:
        raise ValueError("snapshot_instant_future")
    receipt = validated["receipt"]
    cutoff = validated["cutoff"]
    return StagingSourceProfile(
        profile_version=NATIVE_IDENTITY_PROFILE_VERSION,
        projection_version=NATIVE_IDENTITY_PROJECTION_VERSION,
        profile_id=staging_profile_id(cutoff, receipt["policy_sha256"]),
        source_kind=STAGING_SOURCE_KIND,
        source_project=PROJECT,
        source_dataset=STAGING_SOURCE_DATASET,
        snapshot_dataset=STAGING_SOURCE_DATASET,
        source_tables=SOURCE_LANES,
        # The tables this capture creates, read from the module that owns the naming the
        # only capture producer that can execute writes. ``snapshot_dataset`` above is the
        # dataset the v2 plan would write into once it can run; until then the entry a
        # capture registers must declare the tables that capture actually creates, so an
        # operator reading a registered entry reads table names that exist.
        snapshot_tables=tuple(snapshot_destination_table(cutoff, lane) for lane in SOURCE_LANES),
        scope=_text(scope, "profile_input_invalid"),
        # Every daily capture stands for all three bootstrap markets. The capture plan
        # permits a market subset and the coverage admission compares this set exactly,
        # so a capture legitimately run for two markets cannot register until this reads
        # the markets of the run it closes rather than the constant.
        markets=tuple(sorted(BOOTSTRAP_MARKETS)),
        observation_window_end=validated["observation_window_end"],
        collection_started_at=validated["collection_started_at"],
        collection_completed_at=validated["collection_completed_at"],
        snapshot_as_of=snapshot,
        run_id=receipt["run_id"],
        execution_id=receipt["execution_id"],
        source_sha=receipt["source_sha"],
        image_uri=receipt["image_uri"],
        policy_digest=receipt["policy_sha256"],
        schema_digest=_digest(schema_digest, "profile_input_invalid"),
        source_digest=_digest(source_digest, "profile_input_invalid"),
    )


# Capture policy closure

LEGACY_CAPTURE_POLICY_VERSION = "open_intelligence_source_capture_storage_v1"
GRANT_CAPTURE_POLICY_VERSION = "open_intelligence_source_capture_storage_v2"
LEGACY_CAPTURE_CONTRACT_SHA256 = "5dcd9346af27fd7dac97efdd138fce19b56813e0d1eb6629aebcfa9b0233595f"
CAPTURE_POLICY_HEADROOM_MINIMUM = Fraction(1, 10)
PRICE_OBSERVATION_VALIDITY = timedelta(hours=24)
_STORAGE_CONSTANTS = {
    "bucket": "ogilvy-trends-v2-oi-source-artifacts-staging",
    "input_prefix": "inputs/",
    "output_prefix": "captures/",
    "snapshot_retention_days": 90,
    "object_retention_days": 90,
    "max_source_logical_bytes": 5368709120,
    "max_artifact_bytes": 536870912,
}
_RECOVERY_ASSERTIONS = {
    "open_intelligence_source_capture_recovery_v1": {"initial_count": 1, "recovery_count": 0},
    "open_intelligence_source_capture_recovery_v2": {"initial_count": 1, "recovery_count": 0},
    "open_intelligence_source_capture_recovery_v3": {"initial_count": 1, "recovery_count": 1},
    "open_intelligence_source_capture_recovery_v4": {"initial_count": 1, "recovery_count": 2},
}
_LEGACY_POLICY = {
    "policy_version": LEGACY_CAPTURE_POLICY_VERSION,
    "routine": "sp_consume_open_intelligence_source_snapshot_v1",
    "contract_sha256": LEGACY_CAPTURE_CONTRACT_SHA256,
    "storage_policy_constants": {
        "contract_version": LEGACY_CAPTURE_POLICY_VERSION,
        "allowance_id": "source_capture_20260907_20260908_v1",
        **_STORAGE_CONSTANTS,
        "reserved_micro_usd_per_cutoff": 500000,
    },
    "maximum_cycle_cost_bound_micro_usd": 500000,
    "reservation": {
        "kind": "lifetime",
        "reserved_micro_usd_per_initial": 500000,
        "ceiling_micro_usd": 1000000,
    },
    "assertions": {
        "initial": {"initial_count": 0, "recovery_count": 0, "recovery_context": "null"},
        "recover": _RECOVERY_ASSERTIONS,
    },
}
_GRANT_FIELDS = frozenset(
    {
        "grant_id",
        "environment",
        "source_estate_digest",
        "contract_sha256",
        "valid_from",
        "valid_until",
        "allowed_cutoffs",
        "reserved_micro_usd_per_capture",
        "cumulative_ceiling_micro_usd",
        "revocation_state",
    }
)
MEASURED_PRICE_FIELDS = (
    "observed_at",
    "pricing_sources",
    "storage_micro_usd_per_gib_month",
    "query_micro_usd_per_tib",
    "operations_micro_usd_per_10k",
    "source_logical_bytes",
    "max_bytes_billed_per_query",
    "queries_per_cycle",
    "jobs_per_cycle",
    "retention_days",
    "operations_per_cycle",
    "permitted_retries",
    "cadence_cycles_per_day",
)
_POSITIVE_PRICE_FIELDS = ("queries_per_cycle", "jobs_per_cycle", "cadence_cycles_per_day")
_PRICE_REVIEW_FIELDS = frozenset(
    {"reviewed_at", "expires_at", "pricing_sources", "assumptions", "maximum_cycle_cost_micro_usd"}
)


def _grant_instant(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("capture_grant_invalid") from error
    return _aware(value, "capture_grant_invalid")


def validate_capture_grant(grant: object, *, now: datetime) -> dict:
    """Validate an immutable capture grant and return it with ISO instants."""
    code = "capture_grant_invalid"
    if not isinstance(grant, Mapping) or set(grant) != _GRANT_FIELDS:
        raise ValueError(code)
    grant_id = grant["grant_id"]
    if not isinstance(grant_id, str) or _GRANT_ID.fullmatch(grant_id) is None:
        raise ValueError(code)
    if grant["environment"] != "staging" or grant["revocation_state"] not in {
        "active",
        "revoked",
    }:
        raise ValueError(code)
    cutoffs = grant["allowed_cutoffs"]
    if type(cutoffs) is not list or not cutoffs or len(cutoffs) != len(set(cutoffs)):
        raise ValueError(code)
    for item in cutoffs:
        try:
            if not isinstance(item, str) or date.fromisoformat(item).isoformat() != item:
                raise ValueError(code)
        except ValueError as error:
            raise ValueError(code) from error
    reserved = grant["reserved_micro_usd_per_capture"]
    ceiling = grant["cumulative_ceiling_micro_usd"]
    if type(reserved) is not int or reserved <= 0 or type(ceiling) is not int or ceiling <= 0:
        raise ValueError(code)
    valid_from = _grant_instant(grant["valid_from"])
    valid_until = _grant_instant(grant["valid_until"])
    if valid_until <= valid_from:
        raise ValueError(code)
    current = _aware(now, code)
    if ceiling < reserved * len(cutoffs):
        raise ValueError("capture_grant_ceiling_insufficient")
    if grant["revocation_state"] != "active":
        raise ValueError("capture_grant_revoked")
    if current < valid_from:
        raise ValueError("capture_grant_not_yet_valid")
    if current >= valid_until:
        raise ValueError("capture_grant_expired")
    return {
        "grant_id": grant_id,
        "environment": "staging",
        "source_estate_digest": _digest(grant["source_estate_digest"], code),
        "contract_sha256": _digest(grant["contract_sha256"], code),
        "valid_from": valid_from.isoformat(),
        "valid_until": valid_until.isoformat(),
        "allowed_cutoffs": sorted(cutoffs),
        "reserved_micro_usd_per_capture": reserved,
        "cumulative_ceiling_micro_usd": ceiling,
        "revocation_state": "active",
    }


def _grant_policy(grant: Mapping, *, now: datetime) -> dict:
    checked = validate_capture_grant(grant, now=now)
    reserved = checked["reserved_micro_usd_per_capture"]
    headroom = math.ceil(reserved * CAPTURE_POLICY_HEADROOM_MINIMUM)
    return {
        "policy_version": GRANT_CAPTURE_POLICY_VERSION,
        "routine": "sp_consume_open_intelligence_source_snapshot_v2",
        "contract_sha256": checked["contract_sha256"],
        "storage_policy_constants": {
            "contract_version": GRANT_CAPTURE_POLICY_VERSION,
            "allowance_id": checked["grant_id"],
            **_STORAGE_CONSTANTS,
            "reserved_micro_usd_per_cutoff": reserved,
        },
        "maximum_cycle_cost_bound_micro_usd": reserved - headroom,
        "allowed_cutoffs": tuple(checked["allowed_cutoffs"]),
        "reservation": {
            "kind": "grant",
            "grant_id": checked["grant_id"],
            "reserved_micro_usd_per_initial": reserved,
            "ceiling_micro_usd": checked["cumulative_ceiling_micro_usd"],
            "valid_from": checked["valid_from"],
            "valid_until": checked["valid_until"],
        },
        "grant": checked,
        "assertions": {
            "initial": {
                "slot_initial_count": 0,
                "slot_recovery_count": 0,
                "recovery_context": "null",
            },
            "recover": copy.deepcopy(_RECOVERY_ASSERTIONS),
        },
    }


_LEDGER_FIELDS = frozenset({"slot_initial_count", "slot_recovery_count", "reserved_initial_count"})


def evaluate_capture_reservation(
    grant: object,
    *,
    now: datetime,
    cutoff: str,
    mode: str,
    grant_id: str,
    source_estate_digest: str,
    ledger: object,
    recovery: object = None,
) -> dict:
    """The grant bound reservation the v2 routine expresses, as one pure decision.

    ``ledger`` carries the counts the routine reads from the consumption tables: the global
    slot counts for this cutoff across both generations (initial and recover) and the count
    of this grant's admitted initial obligations. The refusal codes are the routine's.
    """
    checked = validate_capture_grant(grant, now=now)
    if mode not in {"initial", "recover"} or grant_id != checked["grant_id"]:
        raise ValueError("source_snapshot_arguments_invalid")
    if (
        not isinstance(ledger, Mapping)
        or set(ledger) != _LEDGER_FIELDS
        or any(type(value) is not int or value < 0 for value in ledger.values())
    ):
        raise ValueError("source_snapshot_reservation_ledger_invalid")
    if cutoff not in checked["allowed_cutoffs"]:
        raise ValueError("source_snapshot_cutoff_not_permitted")
    if source_estate_digest != checked["source_estate_digest"]:
        raise ValueError("source_snapshot_estate_mismatch")
    reserved = checked["reserved_micro_usd_per_capture"]
    ceiling = checked["cumulative_ceiling_micro_usd"]
    reserved_so_far = reserved * ledger["reserved_initial_count"]
    if mode == "initial":
        if recovery is not None:
            raise ValueError("source_snapshot_recovery_invalid")
        if ledger["slot_initial_count"] != 0 or ledger["slot_recovery_count"] != 0:
            raise ValueError("source_snapshot_slot_consumed")
        if reserved_so_far + reserved > ceiling:
            raise ValueError("source_snapshot_grant_exhausted")
        total = reserved_so_far + reserved
    else:
        if (
            not isinstance(recovery, Mapping)
            or recovery.get("contract_version") not in _RECOVERY_ASSERTIONS
        ):
            raise ValueError("source_snapshot_recovery_invalid")
        if ledger["slot_initial_count"] != 1:
            raise ValueError("source_snapshot_recovery_unavailable")
        if reserved_so_far > ceiling:
            raise ValueError("source_snapshot_grant_exhausted")
        expected = _RECOVERY_ASSERTIONS[recovery["contract_version"]]["recovery_count"]
        if ledger["slot_recovery_count"] != expected:
            raise ValueError("source_snapshot_allowance_consumed")
        total = reserved_so_far
    return {
        "cutoff": cutoff,
        "mode": mode,
        "grant_id": grant_id,
        "reserved_micro_usd": total,
        "ceiling_micro_usd": ceiling,
    }


def accepted_capture_policy(version: str, *, grant=None, now: datetime | None = None) -> dict:
    """The accepted policy record of one version; a copy, so callers cannot alter it."""
    if version == LEGACY_CAPTURE_POLICY_VERSION:
        return {**copy.deepcopy(_LEGACY_POLICY), "allowed_cutoffs": allowed_cutoffs()}
    if version == GRANT_CAPTURE_POLICY_VERSION:
        if grant is None or now is None:
            raise ValueError("capture_grant_required")
        return _grant_policy(grant, now=now)
    raise ValueError("capture_policy_version_unknown")


def validate_measured_price_inputs(record: object, *, now: datetime) -> dict:
    """Validate the operator supplied price observation; stale and future observations refuse."""
    code = "price_inputs_invalid"
    if not isinstance(record, Mapping) or set(record) != set(MEASURED_PRICE_FIELDS):
        raise ValueError(code)
    observed = _aware(record["observed_at"], code)
    current = _aware(now, code)
    if observed > current:
        raise ValueError("price_observation_future")
    if current - observed > PRICE_OBSERVATION_VALIDITY:
        raise ValueError("price_observation_stale")
    sources = record["pricing_sources"]
    if type(sources) is not list or not sources:
        raise ValueError(code)
    for url in sources:
        parts = urlsplit(url) if isinstance(url, str) else None
        if parts is None or parts.scheme != "https" or not parts.netloc or parts.username:
            raise ValueError(code)
    normalized = {"observed_at": observed, "pricing_sources": sorted(set(sources))}
    for field in MEASURED_PRICE_FIELDS[2:]:
        value = record[field]
        floor = 1 if field in _POSITIVE_PRICE_FIELDS else 0
        if type(value) is not int or value < floor:
            raise ValueError(code)
        normalized[field] = value
    return normalized


def model_capture_cycle_cost(record: Mapping) -> dict:
    """Model one recurring cycle from measured bytes, bounds, retention, operations, retries."""
    attempts = 1 + record["permitted_retries"]
    query = (
        record["queries_per_cycle"]
        * attempts
        * record["max_bytes_billed_per_query"]
        * record["query_micro_usd_per_tib"]
        // 2**40
    )
    storage = -(
        -(
            record["source_logical_bytes"]
            * record["retention_days"]
            * record["storage_micro_usd_per_gib_month"]
        )
        // (2**30 * 30)
    )
    operations = (
        record["operations_per_cycle"] * attempts * record["operations_micro_usd_per_10k"] // 10000
    )
    return {
        "query_micro_usd": query,
        "storage_micro_usd": storage,
        "operations_micro_usd": operations,
        "maximum_cycle_cost_micro_usd": query + storage + operations,
    }


def price_review_from_measured(record: Mapping) -> dict:
    """The price review block, its assumptions rewritten from the measured fields."""
    cost = model_capture_cycle_cost(record)
    attempts = 1 + record["permitted_retries"]
    assumptions = {
        f"source_logical_bytes={record['source_logical_bytes']} measured logical bytes of the completed source run",
        f"max_bytes_billed_per_query={record['max_bytes_billed_per_query']} bound on every capture query job",
        f"queries_per_cycle={record['queries_per_cycle']} capture queries per recurring cycle",
        f"jobs_per_cycle={record['jobs_per_cycle']} jobs per recurring cycle",
        f"retention_days={record['retention_days']} snapshot and object retention charged at the monthly storage rate",
        f"operations_per_cycle={record['operations_per_cycle']} storage operations per recurring cycle",
        f"permitted_retries={record['permitted_retries']} so {attempts} attempts of every query and operation are modeled",
        f"cadence_cycles_per_day={record['cadence_cycles_per_day']} recurring cycles per day",
        f"query_micro_usd_per_tib={record['query_micro_usd_per_tib']} observed query price",
        f"storage_micro_usd_per_gib_month={record['storage_micro_usd_per_gib_month']} observed storage price",
        f"operations_micro_usd_per_10k={record['operations_micro_usd_per_10k']} observed operations price",
    }
    return {
        "reviewed_at": record["observed_at"].isoformat(),
        "expires_at": (record["observed_at"] + PRICE_OBSERVATION_VALIDITY).isoformat(),
        "pricing_sources": list(record["pricing_sources"]),
        "assumptions": sorted(assumptions),
        "maximum_cycle_cost_micro_usd": cost["maximum_cycle_cost_micro_usd"],
    }


def build_capture_policy_artifact(version: str, *, price_inputs, now: datetime, grant=None) -> dict:
    """Generate the storage policy artifact of one accepted policy from measured prices."""
    record = validate_measured_price_inputs(price_inputs, now=now)
    policy = accepted_capture_policy(version, grant=grant, now=now)
    review = price_review_from_measured(record)
    if review["maximum_cycle_cost_micro_usd"] > policy["maximum_cycle_cost_bound_micro_usd"]:
        if version == GRANT_CAPTURE_POLICY_VERSION:
            raise ValueError("capture_policy_headroom_insufficient")
        raise ValueError("snapshot_price_review_invalid")
    artifact = {**policy["storage_policy_constants"], "price_review": review}
    if version == GRANT_CAPTURE_POLICY_VERSION:
        artifact["grant"] = policy["grant"]
    validate_capture_policy_artifact(artifact, now=now)
    return artifact


def _artifact_instant(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("snapshot_price_review_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("snapshot_price_review_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("snapshot_price_review_invalid")
    return parsed.astimezone(UTC)


def validate_capture_policy_artifact(artifact: object, *, now: datetime) -> dict:
    """Validate a storage policy artifact against the accepted policy its version names."""
    if type(artifact) is not dict or type(artifact.get("contract_version")) is not str:
        raise ValueError("snapshot_storage_policy_invalid")
    version = artifact["contract_version"]
    if version == LEGACY_CAPTURE_POLICY_VERSION:
        policy = accepted_capture_policy(version)
        expected_keys = set(policy["storage_policy_constants"]) | {"price_review"}
    elif version == GRANT_CAPTURE_POLICY_VERSION:
        if type(artifact.get("grant")) is not dict:
            raise ValueError("snapshot_storage_policy_invalid")
        policy = accepted_capture_policy(version, grant=artifact["grant"], now=now)
        expected_keys = set(policy["storage_policy_constants"]) | {"price_review", "grant"}
        if artifact["grant"] != policy["grant"]:
            raise ValueError("snapshot_storage_policy_invalid")
    else:
        raise ValueError("snapshot_storage_policy_invalid")
    if set(artifact) != expected_keys:
        raise ValueError("snapshot_storage_policy_invalid")
    for key, expected in policy["storage_policy_constants"].items():
        if type(artifact[key]) is not type(expected) or artifact[key] != expected:
            raise ValueError("snapshot_storage_policy_invalid")
    price = artifact["price_review"]
    if type(price) is not dict or set(price) != _PRICE_REVIEW_FIELDS:
        raise ValueError("snapshot_price_review_invalid")
    reviewed, expires = (
        _artifact_instant(price["reviewed_at"]),
        _artifact_instant(price["expires_at"]),
    )
    current = _aware(now, "snapshot_price_review_invalid")
    if not reviewed <= current < expires or expires - reviewed > PRICE_OBSERVATION_VALIDITY:
        raise ValueError("snapshot_price_review_invalid")
    cost = price["maximum_cycle_cost_micro_usd"]
    if type(cost) is not int or not 0 <= cost <= policy["maximum_cycle_cost_bound_micro_usd"]:
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
    return policy


__all__ = [
    "CAPTURE_POLICY_HEADROOM_MINIMUM",
    "GRANT_CAPTURE_POLICY_VERSION",
    "LEGACY_CAPTURE_POLICY_VERSION",
    "LEGACY_PROFILE",
    "LEGACY_PROFILE_VERSION",
    "LEGACY_PROJECTION_VERSION",
    "MEASURED_PRICE_FIELDS",
    "NATIVE_IDENTITY_PROFILE_VERSION",
    "NATIVE_IDENTITY_PROJECTION_VERSION",
    "PRICE_OBSERVATION_VALIDITY",
    "STAGING_SOURCE_DATASET",
    "StagingSourceProfile",
    "accepted_capture_policy",
    "build_capture_policy_artifact",
    "build_staging_source_profile",
    "evaluate_capture_reservation",
    "model_capture_cycle_cost",
    "price_review_from_measured",
    "profile_for_version",
    "read_completed_source_run",
    "validate_capture_grant",
    "validate_capture_policy_artifact",
    "validate_completed_source_run",
    "validate_measured_price_inputs",
]
