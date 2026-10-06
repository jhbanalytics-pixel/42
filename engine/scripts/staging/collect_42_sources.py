"""Isolated staging collection boundary for the 42 source estate.

Three pure pieces. ``collect_staging`` refuses unless its caller names the
authority it runs under and the profile, the process environment and the
resolved dataset all name the staging source dataset, and only then hands off
to an injected collector in collection-only mode. The authority is either the
one ``manifest_authority`` verified for this entry point or one an outside
caller names as unverified, so a run under a manifest read and a run under a
caller's own approval chain are told apart rather than being left to look
alike.
``build_bootstrap_profile`` derives the bootstrap source profile from the
currently executable configuration and the explicit new-target authority,
carrying only routes both inputs approve. ``collection_receipt`` shapes the
terminal receipt of one collection run with counts that name their unit.

The policy digest the job definition stamps is a claim about the source estate
this image executes, so ``main`` reconciles that estate and recomputes its
digest before it binds the producer. The stamped value is checked against a
value this process derived from the connector registry and the shipped
configuration, never against another field of the environment block it is
checking and never merely against the shape of a digest, so a job definition
and an image that have parted refuse instead of collecting under a policy
reference that describes nothing.

Nothing here opens a network connection or constructs a cloud client. The
existing producer is imported only inside ``producer_collector``, and only when
that function is called, so importing this module never loads it.

Executable configuration shape. The producer declares its markets as a plain
list and its connector registry as ``(source_key, connector_class)`` tuples,
and neither carries caps; caps live in the source configuration the producer
loads. The caller therefore hands this builder the derived mapping:

    {"markets": ["za", "ng", "ke"],
     "routes": {"<source_key>": {"caps": {"<cap_name>": <int>}}}}

New-target authority shape:

    {"targets": {"<source_key>": {"caps": {"<cap_name>": <int>}}}}

A route is approved when its source key appears in both, and a cap is approved
when both inputs name it for that route, at the smaller of the two values, so
neither side can loosen or extend the other. A cap named by one input only is
listed under ``excluded_caps`` the same way an unapproved route is listed under
``excluded_routes``.
"""

from __future__ import annotations

import functools
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.analysis.open_intelligence.brain_contract import canonical_digest

STAGING_ENVIRONMENT = "staging"
STAGING_SOURCE_DATASET = "intelligence_42_sources_staging"
BOOTSTRAP_MARKETS = ("za", "ng", "ke")
PROFILE_CONTRACT_VERSION = "collection_profile_bootstrap_v1"
RECEIPT_CONTRACT_VERSION = "collection_receipt_v1"
TERMINAL_STATES = frozenset({"collected", "partial", "empty", "failed", "skipped"})
COLLECTED = "collected"
BALANCE_READ_STATUSES = frozenset({"measured", "unavailable"})
FUNDED_CLOSE_FIELDS = (
    "attribution_state",
    "balance_read_status",
    "calls",
    "balance_delta",
    "run_balance_delta",
    "terminal_id",
)
INGEST_JOB = "intelligence-42-ingest-staging"
EXECUTION_VARIABLE = "CLOUD_RUN_EXECUTION"
# The target this entry point runs against, pinned here rather than read from the
# payload being checked, so a manifest cannot name the project and region whose
# resources it then approves of.
PROJECT = "ogilvy-trends-v2"
REGION = "us-central1"
JOB_RESOURCE = f"//run.googleapis.com/projects/{PROJECT}/locations/{REGION}/jobs/{INGEST_JOB}"
SOURCE_DATASET_RESOURCE = (
    f"//bigquery.googleapis.com/projects/{PROJECT}/datasets/{STAGING_SOURCE_DATASET}"
)
MANIFEST_AUTHORITY_FIELDS = frozenset(
    {"job_resource", "source_dataset_resource", "execution_id", "resource_manifest_sha256"}
)
VERIFIED_AUTHORITY = "verified_manifest"
UNVERIFIED_AUTHORITY = "unverified"
AUTHORITY_KINDS = frozenset({VERIFIED_AUTHORITY, UNVERIFIED_AUTHORITY})
# The runtime names an execution of a job by that job's exact name and a five
# character lowercase suffix; nothing else is an execution of this job.
_EXECUTION_SUFFIX = re.compile(r"[a-z0-9]{5}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# The four digests the job definition sets beside the execution the runtime stamps.
COLLECTION_DIGEST_VARIABLES = {
    "source_sha": "COLLECTION_SOURCE_SHA",
    "image_uri": "COLLECTION_IMAGE_URI",
    "policy_sha256": "COLLECTION_POLICY_SHA256",
    "profile_sha256": "COLLECTION_PROFILE_SHA256",
}
# The two of those four that name a sha256. They are held to the same shape here
# as at the daily boundary, so the manual entry point is not the weaker of the two.
STAMPED_DIGEST_FIELDS = ("policy_sha256", "profile_sha256")
LOST_DATA_STATES = frozenset({"partial", "failed"})
EXCLUDED_NOT_IN_AUTHORITY = "not_in_new_target_authority"
EXCLUDED_NOT_EXECUTABLE = "not_executable"
EXCLUDED_NOT_IN_BOTH_INPUTS = "not_in_both_inputs"


def unverified_authority(declared_by: str) -> dict:
    """Name an authority this module verified nothing about.

    A caller that reaches the boundary outside this entry point runs under its
    own approval chain and keys the run by its own attempt, so no manifest read
    here stands behind it. It must still say who it is, by name, and that name
    is what tells such a run apart from a manifest verified one.
    """
    if not isinstance(declared_by, str) or not declared_by.strip():
        raise ValueError("collection_authority")
    return {"authority_kind": UNVERIFIED_AUTHORITY, "declared_by": declared_by.strip()}


def _job_execution(value: object) -> str | None:
    """The execution of exactly this job that ``value`` names, or None.

    The job segment is compared whole, so a sibling job whose name begins with
    this one's is not this job, and the suffix must be an execution suffix, so
    an empty, punctuated, invisible, traversing or line breaking remainder is
    not an execution. Every control character fails the suffix grammar, which
    is what keeps an execution id out of the operator's printed block.
    """
    if not isinstance(value, str):
        return None
    job, separator, suffix = value.rpartition("-")
    if not separator or job != INGEST_JOB or _EXECUTION_SUFFIX.fullmatch(suffix) is None:
        return None
    return value


def _declared_authority(authority: object) -> dict:
    """The authority the caller states it runs under, or a refusal.

    Either the mapping ``manifest_authority`` returns, whose resource names must
    be the pinned ones, whose execution must be an execution of this job and
    whose manifest digest must be a sha256, or an explicitly named unverified
    authority. Anything else leaves the run unplaced and refuses.
    """
    if not isinstance(authority, Mapping):
        raise ValueError("collection_authority")
    if authority.get("authority_kind") == UNVERIFIED_AUTHORITY:
        name = authority.get("declared_by")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("collection_authority")
        return {"authority_kind": UNVERIFIED_AUTHORITY, "declared_by": name.strip()}
    if set(authority) != MANIFEST_AUTHORITY_FIELDS:
        raise ValueError("collection_authority")
    digest = authority["resource_manifest_sha256"]
    execution = _job_execution(authority["execution_id"])
    if (
        authority["job_resource"] != JOB_RESOURCE
        or authority["source_dataset_resource"] != SOURCE_DATASET_RESOURCE
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or execution is None
    ):
        raise ValueError("collection_authority")
    return {
        "authority_kind": VERIFIED_AUTHORITY,
        "execution_id": execution,
        "resource_manifest_sha256": digest,
    }


def _receipt_under(receipt: object, declaration: Mapping[str, object]) -> object:
    """Bind the receipt to the authority the caller declared, where one was verified.

    A manifest verified authority names the exact execution the producer must
    have stamped, so a receipt carrying another one, or no receipt contract at
    all, refuses rather than being read as this execution's record. An
    explicitly named unverified authority verified nothing, so there is nothing
    to bind: that gap is what the declaration exists to state.
    """
    if declaration["authority_kind"] != VERIFIED_AUTHORITY:
        return receipt
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("contract_version") != RECEIPT_CONTRACT_VERSION
        or receipt.get("execution_id") != declaration["execution_id"]
    ):
        raise ValueError("collection_execution_identity")
    return receipt


def collect_staging(profile: dict, *, authority, collector: Callable[..., dict]) -> dict:
    """Run collection-only mode against the staging source dataset, or refuse.

    ``authority`` is required and has no default: a caller must state what it
    runs under, because this module's own manifest read stands only behind the
    entry point below. Refusals fire in this order, each before the collector is
    imported or invoked: declared authority, profile environment, profile source
    dataset, runtime environment, resolved dataset. The dataset resolver is
    imported only once the runtime environment has passed.
    """
    declaration = _declared_authority(authority)
    if profile.get("environment") != STAGING_ENVIRONMENT:
        raise ValueError("environment")
    if profile.get("source_dataset") != STAGING_SOURCE_DATASET:
        raise ValueError("source_dataset")
    if os.environ.get("TRENDS_ENV") != STAGING_ENVIRONMENT:
        raise ValueError("runtime_environment")
    from src.utils.bigquery import get_dataset

    if get_dataset() != profile["source_dataset"]:
        raise ValueError("resolved_source_dataset")
    return _receipt_under(collector(stop_after_ingestion=True), declaration)


def producer_collector(
    *,
    receipt_ledger=None,
    collection_authority=None,
    collection_profile=None,
    authority_kind=None,
) -> Callable[..., dict]:
    """Bind the existing producer lazily, with the receipt ledger it records to.

    This is the only place the producer module is imported. The bound callable
    takes ``stop_after_ingestion`` and nothing else, and passes it through to
    the producer together with the ledger, whose receipt it returns unchanged.
    ``collection_authority``, when given, is the exact execution authority the
    producer stamps into that receipt, keyed by the business attempt, and it is
    forwarded as handed in so the producer never reads its identity from the
    environment; left None, the producer's own environment read stands.
    ``collection_profile``, when given, is the source profile the run is placed
    under, and the producer checks the ``profile_sha256`` it stamps against the
    digest of that profile rather than recording the digest on the job
    definition's word. ``authority_kind`` is the kind of authority the boundary
    placed the run under, and the producer writes it into the receipt so the
    record says which one produced it.

    The ledger is the durable store of collection receipts keyed by operation
    ID and the single commit point of a collection-only run. It exposes
    ``operation(operation_id)``, returning the recorded receipt or None,
    ``market_day(trend_date)``, returning the receipts recorded for that ISO
    date, and ``record(receipt, *, trend_date)``. Left unbound, the producer
    refuses collection-only mode before any connector is constructed.
    """
    from scripts.run_rss_now import _run_impl

    bound: dict[str, object] = {"receipt_ledger": receipt_ledger}
    if collection_authority is not None:
        bound["collection_authority"] = dict(collection_authority)
    if collection_profile is not None:
        bound["collection_profile"] = dict(collection_profile)
    if authority_kind is not None:
        bound["authority_kind"] = authority_kind

    @functools.wraps(_run_impl)
    def collector(*, stop_after_ingestion: bool) -> dict:
        return _run_impl(stop_after_ingestion=stop_after_ingestion, **bound)

    return collector


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _markets(configuration: Mapping[str, object]) -> list[str]:
    markets = configuration.get("markets")
    if (
        not isinstance(markets, list | tuple)
        or len(markets) != len(BOOTSTRAP_MARKETS)
        or set(markets) != set(BOOTSTRAP_MARKETS)
    ):
        raise ValueError("markets")
    return list(BOOTSTRAP_MARKETS)


def _route_caps(container: Mapping[str, object], field: str) -> dict[str, dict[str, int]]:
    entries = container.get(field)
    if not isinstance(entries, Mapping):
        raise ValueError(field)
    result: dict[str, dict[str, int]] = {}
    for key, entry in entries.items():
        if not isinstance(key, str) or not key or not isinstance(entry, Mapping):
            raise ValueError(f"{field}: {key!r}")
        caps = entry.get("caps", {})
        if not isinstance(caps, Mapping):
            raise ValueError(f"{field}: {key} caps")
        for name, value in caps.items():
            if not isinstance(name, str) or not _is_count(value):
                raise ValueError(f"{field}: {key} caps {name!r}")
        result[key] = dict(caps)
    return result


def _shared_caps(
    executable: Mapping[str, int], approved: Mapping[str, int]
) -> tuple[dict[str, int], list[str]]:
    shared = sorted(set(executable) & set(approved))
    dropped = sorted(set(executable) ^ set(approved))
    return {name: min(executable[name], approved[name]) for name in shared}, dropped


def build_bootstrap_profile(executable_configuration: dict, new_target_authority: dict) -> dict:
    """Build the bootstrap source profile from the two approval inputs.

    Only routes named by both the executable configuration and the new-target
    authority are carried, and within them only caps named by both, at the
    smaller value. The rest are listed under ``excluded_routes`` and
    ``excluded_caps`` with the reason. The digest covers the canonical form of
    every other field: sorted keys, compact separators, UTF-8.
    """
    markets = _markets(executable_configuration)
    routes = _route_caps(executable_configuration, "routes")
    targets = _route_caps(new_target_authority, "targets")
    approved: dict[str, dict[str, dict[str, int]]] = {}
    excluded_routes: list[dict[str, str]] = []
    excluded_caps: list[dict[str, str]] = []
    for key in sorted(routes):
        if key not in targets:
            excluded_routes.append({"route": key, "reason": EXCLUDED_NOT_IN_AUTHORITY})
            continue
        caps, dropped = _shared_caps(routes[key], targets[key])
        approved[key] = {"caps": caps}
        excluded_caps.extend(
            {"route": key, "cap": name, "reason": EXCLUDED_NOT_IN_BOTH_INPUTS} for name in dropped
        )
    excluded_routes.extend(
        {"route": key, "reason": EXCLUDED_NOT_EXECUTABLE}
        for key in sorted(set(targets) - set(routes))
    )
    excluded_routes.sort(key=lambda item: item["route"])
    profile: dict[str, object] = {
        "contract_version": PROFILE_CONTRACT_VERSION,
        "environment": STAGING_ENVIRONMENT,
        "source_dataset": STAGING_SOURCE_DATASET,
        "markets": markets,
        "routes": approved,
        "excluded_routes": excluded_routes,
        "excluded_caps": excluded_caps,
    }
    profile["profile_sha256"] = canonical_digest(profile)
    return profile


def _terminal_states(market_states: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(market_states, Mapping):
        raise ValueError("market_states")
    unknown = sorted(str(market) for market in market_states if market not in BOOTSTRAP_MARKETS)
    if unknown:
        raise ValueError(f"market_states: unknown market {unknown}")
    missing = [market for market in BOOTSTRAP_MARKETS if market not in market_states]
    if missing:
        raise ValueError(f"market_states: missing market {missing}")
    states: dict[str, str] = {}
    for market in BOOTSTRAP_MARKETS:
        state = market_states[market]
        if state not in TERMINAL_STATES:
            raise ValueError(f"market_states: {market} state {state!r} is not terminal")
        states[market] = state
    return states


def _funded_close(value: object) -> dict | None:
    """Validate the funded terminal close carried by a receipt.

    ``None`` means no funded lane was active. Otherwise the block names the
    attribution state, the balance read status, the call count, the two
    balance deltas as decimal text and the terminal row ID. A balance the
    close could not read stays ``None`` in both deltas: an unknown balance is
    never written down as zero.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(FUNDED_CLOSE_FIELDS):
        raise ValueError("funded_close")
    if not isinstance(value["attribution_state"], str) or not value["attribution_state"]:
        raise ValueError("funded_close: attribution_state")
    status = value["balance_read_status"]
    if status not in BALANCE_READ_STATUSES:
        raise ValueError("funded_close: balance_read_status")
    if not _is_count(value["calls"]):
        raise ValueError("funded_close: calls")
    for field in ("balance_delta", "run_balance_delta"):
        delta = value[field]
        if status == "unavailable" and delta is not None:
            raise ValueError(
                f"funded_close: {field} must stay unknown when the balance was not read"
            )
        if delta is not None and (not isinstance(delta, str) or not delta):
            raise ValueError(f"funded_close: {field}")
    if status == "measured" and value["balance_delta"] is None:
        raise ValueError("funded_close: balance_delta")
    terminal_id = value["terminal_id"]
    if terminal_id is not None and (not isinstance(terminal_id, str) or not terminal_id):
        raise ValueError("funded_close: terminal_id")
    return {field: value[field] for field in FUNDED_CLOSE_FIELDS}


def _profile_digest(profile: object) -> str:
    """The digest of the source profile, recomputed the way the builder computes it."""
    if not isinstance(profile, Mapping):
        raise ValueError("profile must be the source profile it names")
    return canonical_digest(
        {key: value for key, value in profile.items() if key != "profile_sha256"}
    )


def collection_receipt(
    *,
    run_id,
    execution_id,
    source_sha,
    image_uri,
    policy_sha256,
    profile_sha256,
    cutoff,
    market_states: dict,
    raw_count: int,
    enriched_count: int,
    funded_close: dict | None = None,
    profile: Mapping[str, object] | None = None,
    authority_kind: str | None = None,
) -> dict:
    """Shape the terminal receipt of one collection run.

    ``complete`` is true only when all three markets are present and each one
    is collected. A partial or failed market leaves data missing; a skipped or
    empty market means this run persisted nothing for it, which must not read
    as all-market completion. Counts are named by their unit. ``funded_close``
    carries the funded terminal close when that lane was active.

    The two digests must be sha256 digests, not merely text, so a receipt
    naming a policy it never ran under is incomplete rather than complete. A
    caller holding the source profile hands it in as ``profile``, and then
    ``profile_sha256`` is checked against the digest of that profile instead of
    being taken on the caller's word.

    ``authority_kind`` is the kind of authority the run was placed under, and it
    is carried into the receipt so a consumer reading the record can tell a
    manifest verified run from one that ran under a caller's own approval chain.
    A receipt that names no authority kind was produced under no manifest read
    here, and must not be read as a verified one.
    """
    identity = {
        "run_id": run_id,
        "execution_id": execution_id,
        "source_sha": source_sha,
        "image_uri": image_uri,
        "policy_sha256": policy_sha256,
        "profile_sha256": profile_sha256,
        "cutoff": cutoff,
    }
    for field, value in identity.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty string")
    for field in ("policy_sha256", "profile_sha256"):
        if _SHA256.fullmatch(identity[field]) is None:
            raise ValueError(f"{field} must be a sha256 digest")
    if profile is not None and identity["profile_sha256"] != _profile_digest(profile):
        raise ValueError("profile_sha256 must be the digest of the profile it names")
    if authority_kind is not None and authority_kind not in AUTHORITY_KINDS:
        raise ValueError("authority_kind must name an authority this boundary places")
    states = _terminal_states(market_states)
    counts = {"raw_rows_persisted": raw_count, "enriched_rows_persisted": enriched_count}
    parameters = {"raw_rows_persisted": "raw_count", "enriched_rows_persisted": "enriched_count"}
    for field, value in counts.items():
        if not _is_count(value):
            raise ValueError(f"{field}: {parameters[field]} must be a non-negative integer")
    receipt = {
        "contract_version": RECEIPT_CONTRACT_VERSION,
        **identity,
        "market_states": states,
        **counts,
        "funded_close": _funded_close(funded_close),
        "complete": all(states[market] == COLLECTED for market in BOOTSTRAP_MARKETS),
    }
    if authority_kind is not None:
        receipt["authority_kind"] = authority_kind
    return receipt


def entry_point_profile() -> dict:
    """The source profile this entry point runs under, carrying its own digest.

    The boundary is handed this profile and the producer is handed the same one,
    so the ``profile_sha256`` the job definition stamps into the receipt is
    checked against the digest of the profile the run actually used instead of
    being recorded on the job definition's word. The job definition therefore
    declares ``COLLECTION_PROFILE_SHA256`` as this profile's digest; a value
    naming some other profile refuses at the receipt rather than being recorded
    as this run's.
    """
    profile: dict[str, object] = {
        "contract_version": PROFILE_CONTRACT_VERSION,
        "environment": STAGING_ENVIRONMENT,
        "source_dataset": STAGING_SOURCE_DATASET,
        "markets": list(BOOTSTRAP_MARKETS),
    }
    profile["profile_sha256"] = canonical_digest(profile)
    return profile


def manifest_authority(*, generation=None, environ=None) -> dict:
    """Read the approved resource manifest and this execution's identity.

    The manifest is the pinned generation the resource binding approved, read
    through its own reader, which admits one exact provider qualified name
    carrying the action. This entry point invokes the ingest job and writes the
    staging source dataset, so both are checked, and a manifest that carries
    neither refuses. The two resource names are built from the target pinned in
    this module, never from the payload being checked, so a manifest naming
    another project or region cannot approve of its own resources; it is
    checked against the pinned target and refuses. The digest the generation
    carries is a claim about itself, so the manifest is read again from the
    catalogue under that digest, before any approval is taken, and the approved
    manifest must be that same manifest. The catalogue read is the active
    generation read: the catalogue retains prior generations by design, and a
    retired one is not an authority this entry point runs under. The digest this
    function returns is the caller's own claim, admitted only because reading the
    catalogue under it produced the manifest that was approved; it is not a second
    independent value and nothing here should be read as making it one.

    The execution identity is the one the runtime stamps for this job, and the
    producer records its receipt under that exact operation, so an execution
    belonging to another job, or one that is no execution of this job, refuses
    rather than becoming a foreign operation key. Every check here runs before
    the collector is imported.
    """
    from src.analysis.open_intelligence.execution_generations import (
        active_generation,
        assert_resource_allowed,
        require_active_generation,
    )

    generation = active_generation() if generation is None else generation
    manifest = generation.resource_manifest
    if manifest.get("project") != PROJECT or manifest.get("region") != REGION:
        raise ValueError("resource_manifest_target")
    trusted = require_active_generation(
        generation.origin_registry_sha256, generation.resource_manifest_sha256
    )
    assert_resource_allowed(JOB_RESOURCE, "invoke", generation=generation)
    assert_resource_allowed(SOURCE_DATASET_RESOURCE, "write", generation=generation)
    if dict(trusted.resource_manifest) != dict(manifest):
        raise ValueError("resource_manifest_digest")
    values = os.environ if environ is None else environ
    execution = values.get(EXECUTION_VARIABLE, "")
    execution = execution.strip() if isinstance(execution, str) else ""
    if not execution:
        raise ValueError("execution_identity")
    if _job_execution(execution) is None:
        raise ValueError("execution_job")
    return {
        "job_resource": JOB_RESOURCE,
        "source_dataset_resource": SOURCE_DATASET_RESOURCE,
        "execution_id": execution,
        "resource_manifest_sha256": trusted.resource_manifest_sha256,
    }


def reconciled_policy_digest() -> str:
    """The policy digest of the source estate this tree reconciles, recomputed here.

    The estate is read from the producer's connector registry and the shipped
    configuration by the reconciliation that owns it, and the digest is that
    reconciliation's own digest over the reconciled rows. Nothing about it is
    taken from the run being checked, so it is an independent reading of the
    question the stamped digest answers rather than a restatement of it.

    The import is made here, not at module scope, so importing this module
    reconciles nothing and a refusal that fires before this point never reads a
    connector module.
    """
    from src.analysis.open_intelligence.source_inventory import staging_source_estate

    return staging_source_estate()[1]


def _stamped_authority(execution_id: str, values: Mapping[str, str]) -> dict:
    """The exact authority the producer stamps, keyed by the execution just verified.

    The execution is the one the manifest read admitted, not a second read of
    the environment, so the value checked and the value stamped are one value
    from one source. The four digests are read from the same environment mapping
    this entry point was given; they are not carried from anything verified, so
    each is held to its own shape here. A missing one refuses before the producer
    is bound, and so does one of the two sha256 fields that is not sixty-four
    hexadecimal characters, which is what the daily boundary requires of the same
    value.

    ``policy_sha256`` is the one of the four this tree can answer for itself, so
    it is answered rather than carried: it must be the digest of the source
    estate reconciled here. A digest shaped string refuses, and so does the
    digest of some other real estate, because the value is compared against a
    recomputation and not against a pattern.
    """
    authority = {"execution_id": execution_id}
    for field, variable in COLLECTION_DIGEST_VARIABLES.items():
        value = values.get(variable, "")
        value = value.strip() if isinstance(value, str) else ""
        if not value:
            raise ValueError(f"collection_authority: {variable}")
        if field in STAMPED_DIGEST_FIELDS and _SHA256.fullmatch(value) is None:
            raise ValueError(f"collection_authority: {variable}")
        authority[field] = value
    if authority["policy_sha256"] != reconciled_policy_digest():
        raise ValueError("collection_policy_unreconciled")
    return authority


def main(
    *,
    generation=None,
    environ=None,
    receipt_ledger=None,
    collector_factory: Callable[..., Callable[..., dict]] = producer_collector,
) -> int:
    """Run one collection only execution of the staging producer and report it.

    The order is the whole point. The manifest reader and the execution identity
    come first, then the stamped digests and the estate the policy digest among
    them claims, then the staging boundary, and only then is the producer bound
    and imported, so a refusal at any of those never loads the collector. The
    execution authority the producer stamps into its receipt is the one this
    entry point verified and handed it, so there is no second copy read from
    somewhere else, and the receipt that comes back must carry it. The producer
    is handed the source profile this run is placed under as well, so the profile
    digest it stamps is checked against that profile rather than recorded on the
    job definition's word, and the kind of authority the boundary placed the run
    under, so the record says which one produced it.

    The exit status is the operator's signal, and the receipt is the record. A
    receipt that does not name every market is no record of a run against them,
    and refuses rather than reporting a clean one, and it refuses before it is
    printed, so an incomplete receipt never reaches the operator block as this
    run's record. A market that lost data
    leaves the status failed so the job stops instead of reporting a clean run;
    the partial rows and the receipt are kept either way. A market that was
    skipped as already collected, or that had nothing to collect, lost nothing
    and leaves the status clean.
    """
    authority = manifest_authority(generation=generation, environ=environ)
    stamped = _stamped_authority(
        authority["execution_id"], os.environ if environ is None else environ
    )
    print(
        f"Collection job: {authority['job_resource']}\n"
        f"Execution:      {authority['execution_id']}\n"
        f"Manifest:       {authority['resource_manifest_sha256']}"
    )

    profile = entry_point_profile()
    declaration = _declared_authority(authority)

    def collector(**kwargs) -> dict:
        return collector_factory(
            receipt_ledger=receipt_ledger,
            collection_authority=stamped,
            collection_profile=profile,
            authority_kind=declaration["authority_kind"],
        )(**kwargs)

    receipt = collect_staging(profile, authority=authority, collector=collector)
    states = _terminal_states(receipt.get("market_states"))
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    lost = sorted(market for market, state in states.items() if state in LOST_DATA_STATES)
    if lost:
        print(f"Collection lost data in {', '.join(lost)}")
        return 1
    return 0


def entry() -> int:
    """The ingest job's entry point: ``main`` over the ingest receipt ledger.

    The producer refuses collection only mode without a ledger, and the ingest identity
    holds no object grant, so the ledger is the receipt table in the staging source
    dataset the identity already writes. It is built only here, after import, and the
    orchestration identity reads the same table back by this execution's id.
    """
    from src.analysis.open_intelligence.ingest_receipts import native_collection_ledger

    return main(receipt_ledger=native_collection_ledger())


__all__ = [
    "AUTHORITY_KINDS",
    "BALANCE_READ_STATUSES",
    "BOOTSTRAP_MARKETS",
    "COLLECTION_DIGEST_VARIABLES",
    "EXECUTION_VARIABLE",
    "FUNDED_CLOSE_FIELDS",
    "INGEST_JOB",
    "JOB_RESOURCE",
    "LOST_DATA_STATES",
    "PROFILE_CONTRACT_VERSION",
    "PROJECT",
    "RECEIPT_CONTRACT_VERSION",
    "REGION",
    "SOURCE_DATASET_RESOURCE",
    "STAGING_SOURCE_DATASET",
    "STAMPED_DIGEST_FIELDS",
    "TERMINAL_STATES",
    "UNVERIFIED_AUTHORITY",
    "VERIFIED_AUTHORITY",
    "build_bootstrap_profile",
    "collect_staging",
    "collection_receipt",
    "entry",
    "entry_point_profile",
    "main",
    "manifest_authority",
    "producer_collector",
    "reconciled_policy_digest",
    "unverified_authority",
]


if __name__ == "__main__":  # pragma: no cover - job entry point
    raise SystemExit(entry())
