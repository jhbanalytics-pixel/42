"""Produce the bridge v3 capture inputs and execution manifest for one closed collection day.

Owner run, offline and write once. The operator names the cutoff date and supplies, as
files, what only a native read can establish: the funded Wave 1 pilot's approval ledger
chain row (the chain statement's one row) and its Cloud Run v2 execution, the history
completion facts, the capture grant, the measured price observation, the capture contract
bytes, the tables.get resources of the seven source tables and the Cloud Build describe of
the image. This process never calls Google. It builds the nine input artifacts the v3
capture policy names, the execution manifest for the initial vector of that cutoff under
the grant on job intelligence-42-daily-staging under the orchestration identity, and runs
the approval preparation (``prepare_source_snapshot_capture_v3``) over those exact bytes.
Only when it passes are they written, to a directory that must not exist, so a proposal the
approval route would refuse is never left for review.

Every digest is computed here from bytes this process built. Nothing it writes is
authority: the approval, the consumption and the native evidence resolvers decide.

* The cutoff has no default. It must be the day the funded execution closes: the execution
  started at or after that day's observation window end and before the next day's.
* The one collection receipt is the funded execution's funded close: its run id is the
  execution id, its digest the close's result digest, and it spans the consumption to the
  recorded close. The receipt is bound here exactly as the capture evidence resolver binds
  it before the approval is spent, so a proposal that resolver would refuse is not written.
* The completion set has one explicit entry per (history lane, market, day) from the first
  product date the facts name through the cutoff. A day the facts do not cover has no
  composition chain behind it and is ``unavailable`` with ``native_completion_unrecorded``.
* The temporal rules are the reviewed rule list the repository carries, one rule per key
  the funded Wave 1 SocialCrawl writer can put into raw_content; rows of other connectors
  have no key and no rule. Without a reviewed list
  the producer refuses with ``bridge_temporal_rules_unreviewed`` and never invents rules.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.staging import build_open_intelligence_execution_manifest as manifest_builder
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.execution_origins import OriginRefusal

OPERATION = "source_snapshot_capture"
MODE = "new_approval"
RECEIPT_VERSION = "bridge_capture_inputs_production_v1"
FACTS_VERSION = "42_bridge_history_completion_facts_v1"
# Route A captures one closed collection day, named on the command line, in initial mode.
CAPTURE_MODE = "initial"
JOB_RESOURCE = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
SERVICE_IDENTITY = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
ARTIFACT_NAMES = (
    "bridge_policy",
    "capture_contract",
    "capture_plan",
    "collection_receipt_set",
    "history_completion_set",
    "recovery_context",
    "source_metadata",
    "storage_policy",
    "temporal_rules",
)
UNRECORDED = "native_completion_unrecorded"
# The capture runner's own profile: ten minutes and the policy byte bound.
TIMEOUT_SECONDS = 600
MAX_BYTES_BILLED = 1_000_000_000
MINIMUM_WINDOW = timedelta(hours=1)
# The reviewed temporal rule list: one rule per (source family, endpoint, content type) key
# the funded Wave 1 SocialCrawl writer can put into raw_content, every frozen Wave 1 route
# with each of the two vendor envelopes, post and comment. The capture copies both
# collection tables whole, so it also carries rows from the other connectors of the same
# and earlier runs; those rows have no endpoint or source family, no rule keys them, and
# these rules give them no event time. The consumer checks only the list's shape. An event
# instant is a post or comment whose published_at is the item's own timestamp on a route
# that lists that kind of item; every other key, and a YouTube comment time that nothing
# shows is exact, is a collection observation, the conservative basis. The table with its
# sources is configs/open_intelligence/candidate_contracts/source-bridge-temporal-rules-v1.md.
_EVENT = "native_event_instant"
_OBSERVED = "collection_observation"
_WITHHOLD = "withhold_event_time_claim"
_REVIEWED_RULE_ROWS = (
    ("reddit", "reddit/post/comments", "reddit/comment", _EVENT),
    ("reddit", "reddit/post/comments", "reddit/post", _OBSERVED),
    ("short_video", "instagram/audio/reels", "instagram/comment", _OBSERVED),
    ("short_video", "instagram/audio/reels", "instagram/post", _EVENT),
    ("short_video", "instagram/music/trending", "instagram/comment", _OBSERVED),
    ("short_video", "instagram/music/trending", "instagram/post", _OBSERVED),
    ("short_video", "instagram/search/reels", "instagram/comment", _OBSERVED),
    ("short_video", "instagram/search/reels", "instagram/post", _EVENT),
    ("short_video", "tiktok/song", "tiktok/comment", _OBSERVED),
    ("short_video", "tiktok/song", "tiktok/post", _OBSERVED),
    ("short_video", "tiktok/song/videos", "tiktok/comment", _OBSERVED),
    ("short_video", "tiktok/song/videos", "tiktok/post", _EVENT),
    ("youtube", "youtube/shorts/trending", "youtube/comment", _OBSERVED),
    ("youtube", "youtube/shorts/trending", "youtube/post", _EVENT),
    ("youtube", "youtube/video/comments", "youtube/comment", _OBSERVED),
    ("youtube", "youtube/video/comments", "youtube/post", _OBSERVED),
)
REVIEWED_TEMPORAL_RULES = {
    "contract_version": "source_observation_semantics_v1",
    "rules": [
        {
            "source_family": family,
            "endpoint": endpoint,
            "content_type": content_type,
            "time_basis": basis,
            "event_field": "published_at" if basis == _EVENT else None,
            "missing_time_action": _WITHHOLD,
            "future_time_action": _WITHHOLD,
        }
        for family, endpoint, content_type, basis in _REVIEWED_RULE_ROWS
    ],
}
# The command line: each switch is two hyphens and the input's name, in this order; the
# cutoff date comes first and every other value is an absolute path.
_CLI_NAMES = (
    "cutoff_date",
    "funded_chain",
    "funded_execution",
    "history_completions",
    "grant",
    "price_observation",
    "capture_contract",
    "source_metadata",
    "build",
    "output_dir",
)
_CLI_FLAGS = tuple("-" * 2 + name.replace("_", "-") for name in _CLI_NAMES)
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class ProducerRefusal(ValueError):
    def __init__(self, code: str, exit_code: int = 1):
        super().__init__(code)
        self.exit_code = exit_code


def _refuse(code: str, exit_code: int = 1):
    raise ProducerRefusal(code, exit_code)


def _parse_cli(argv: Sequence[str]) -> tuple[str, dict[str, Path]]:
    """The cutoff date and the input paths; any other command line refuses with exit 2."""
    values = tuple(argv)
    if (
        len(values) != 2 * len(_CLI_FLAGS)
        or values[0::2] != _CLI_FLAGS
        or any(type(value) is not str or not value for value in values[1::2])
    ):
        _refuse("bridge_capture_inputs_cli_invalid", 2)
    cutoff = values[1]
    try:
        parsed = date.fromisoformat(cutoff)
    except ValueError as error:
        raise ProducerRefusal("bridge_capture_inputs_cli_invalid", 2) from error
    if len(cutoff) != 10 or parsed.isoformat() != cutoff:
        _refuse("bridge_capture_inputs_cli_invalid", 2)
    paths = {name: Path(value) for name, value in zip(_CLI_NAMES[1:], values[3::2], strict=True)}
    if any(not path.is_absolute() for path in paths.values()):
        _refuse("bridge_capture_inputs_cli_invalid", 2)
    return cutoff, paths


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    observed = clock()
    if (
        not isinstance(observed, datetime)
        or observed.tzinfo is None
        or observed.utcoffset() != timedelta(0)
    ):
        _refuse("bridge_capture_inputs_clock_invalid")
    return observed.astimezone(UTC)


def _reviewed_temporal_rules() -> dict:
    rules = REVIEWED_TEMPORAL_RULES
    if rules is None:
        _refuse("bridge_temporal_rules_unreviewed")
    return json.loads(canonical_bytes(rules))


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ProducerRefusal("bridge_capture_inputs_unreadable") from error


def _read_json(path: Path) -> object:
    raw = _read_bytes(path)

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _refuse("bridge_capture_inputs_invalid")
            value[key] = item
        return value

    def nonfinite(_value):
        _refuse("bridge_capture_inputs_invalid")

    if raw.startswith(b"\xef\xbb\xbf"):
        _refuse("bridge_capture_inputs_invalid")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)
    except ProducerRefusal:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProducerRefusal("bridge_capture_inputs_invalid") from error


def _instant(value: object) -> datetime:
    if type(value) is not str:
        _refuse("bridge_capture_inputs_invalid")
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError as error:
        raise ProducerRefusal("bridge_capture_inputs_invalid") from error


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def _generation():
    """The active trusted generation, which must be the bridge generation."""
    from src.analysis.open_intelligence.bridge_ledger_binding import is_bridge_ledger_generation

    registry = manifest_builder._registry_of(manifest_builder._active_generation())
    if not is_bridge_ledger_generation(registry):
        _refuse("bridge_capture_generation_inactive")
    origin = execution_approval._v2_origin_for_operation(OPERATION, mode=MODE, registry=registry)
    policy = json.loads(
        execution_approval._policy_bytes_for_origin(registry=registry, origin=origin)
    )
    rule = policy["operation_validation"][OPERATION]
    if (
        tuple(rule["input_artifact_names"]) != ARTIFACT_NAMES
        or rule["job_resource"] != JOB_RESOURCE
        or rule["service_identity"] != SERVICE_IDENTITY
        or CAPTURE_MODE not in rule["arguments"]["modes"]
    ):
        _refuse("bridge_capture_generation_inactive")
    return registry, origin, rule


def _instant_after(value: str) -> datetime:
    """The first whole second at or after a Cloud Run instant, which may carry nanoseconds."""
    from src.analysis.open_intelligence.daily_execution_authority import native_instant_key

    whole = datetime.fromisoformat(value[:19]).replace(tzinfo=UTC)
    _, fraction = native_instant_key(value)
    return whole + timedelta(seconds=1) if fraction else whole


def _funded_receipt_set(
    chain: object, execution: object, *, cutoff: str, generation_loader
) -> tuple[dict, datetime]:
    """The one receipt of the funded execution that closes ``cutoff``, bound as the capture binds.

    ``chain`` is the chain statement's row for the pilot's consumption, as ``bq`` prints it,
    and ``execution`` its Cloud Run v2 execution. The collection clones snapshot at the
    first whole second after the execution completed.
    """
    from scripts.staging.render_protected_context_entry import _chain_row
    from src.analysis.open_intelligence.daily_child_execution import (
        FUNDED_EXECUTION_PREFIX,
        native_funded_execution_view,
    )
    from src.analysis.open_intelligence.general_question_context_queries import (
        decode_context_result_rows_v2,
    )
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        FUNDED_OPERATION,
        REF,
        resolve_funded_collection,
    )

    try:
        row = _chain_row(chain)
        consumption_id = json.loads(row["consumption_json"])["consumption_id"]
        checked = decode_context_result_rows_v2(
            [row],
            consumption_id=consumption_id,
            operation=FUNDED_OPERATION,
            generation_loader=generation_loader,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProducerRefusal("bridge_funded_chain_invalid") from error
    result, consumption = checked["result"], checked["consumption"]
    name = consumption.execution_name
    if not name.startswith(FUNDED_EXECUTION_PREFIX):
        _refuse("bridge_funded_chain_invalid")
    try:
        view = native_funded_execution_view(execution)
        started = datetime.fromisoformat(view["execution_started_at"]).astimezone(UTC)
        completed = _instant_after(view["completed_at"]) if "completed_at" in view else None
    except (KeyError, TypeError, ValueError) as error:
        raise ProducerRefusal("bridge_funded_execution_invalid") from error
    window_end = datetime.combine(date.fromisoformat(cutoff) + timedelta(days=1), time.min, UTC)
    if not window_end <= started < window_end + timedelta(days=1):
        _refuse("bridge_capture_cutoff_unproven")
    receipt = {
        "run_id": name[len(FUNDED_EXECUTION_PREFIX) :],
        "result_ref": {field: result[field] for field in sorted(REF)},
        "collection_receipt_digest": result["result_digest"],
        "collection_started_at": _stamp(consumption.consumed_at),
        "collection_completed_at": _stamp(result["completed_at"]),
    }
    try:
        resolve_funded_collection(
            receipt,
            read_chain=lambda requested: [dict(row)] if requested == consumption_id else [],
            read_funded_execution=lambda requested: dict(view),
            window_start=window_end,
            window_end=completed or window_end,
            generation_loader=generation_loader,
        )
    except ValueError as error:
        raise ProducerRefusal(str(error)) from error
    return (
        {"contract_version": "42_bridge_collection_receipt_set_v1", "receipts": [receipt]},
        completed,
    )


def _history_completion_set(value: object, *, cutoff: str) -> dict:
    """Every (history lane, market, day) through the cutoff, gaps explicitly unrecorded."""
    from src.analysis.open_intelligence.source_estate_bridge import HISTORY_TABLES, MARKETS
    from src.analysis.open_intelligence.source_estate_bridge_evidence import HISTORY

    if (
        type(value) is not dict
        or set(value) != {"contract_version", "first_product_date", "entries"}
        or value["contract_version"] != FACTS_VERSION
        or type(value["entries"]) is not list
    ):
        _refuse("bridge_history_completions_invalid")
    try:
        first = date.fromisoformat(value["first_product_date"])
    except (TypeError, ValueError) as error:
        raise ProducerRefusal("bridge_history_completions_invalid") from error
    last = date.fromisoformat(cutoff)
    if first.isoformat() != value["first_product_date"] or first > last:
        _refuse("bridge_history_completions_invalid")
    days = [
        (first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
    ]
    supplied = {}
    for entry in value["entries"]:
        if (
            type(entry) is not dict
            or set(entry) != HISTORY
            or entry["state"] not in ("completed", "empty")
            or entry["product_date"] not in days
        ):
            _refuse("bridge_history_completions_invalid")
        key = (entry["lane"], entry["market"], entry["product_date"])
        if key in supplied:
            _refuse("bridge_history_completions_invalid")
        supplied[key] = entry
    grid = [(lane, market, day) for lane in HISTORY_TABLES for market in MARKETS for day in days]
    if not set(supplied) <= set(grid):
        _refuse("bridge_history_completions_invalid")
    entries = [
        supplied.get(key)
        or {
            "lane": key[0],
            "market": key[1],
            "product_date": key[2],
            "state": "unavailable",
            "result_ref": None,
            "product_receipt_digest": None,
            "output_digest": None,
            "row_count": None,
            "completed_at": None,
            "available_at": None,
            "reason_code": UNRECORDED,
        }
        for key in grid
    ]
    return {"contract_version": "42_bridge_history_completion_set_v1", "entries": entries}


def _storage_policy(grant: object, price: object, observed_at: datetime, *, cutoff: str) -> dict:
    from src.analysis.open_intelligence.staging_source_profile import (
        GRANT_CAPTURE_POLICY_VERSION,
        MEASURED_PRICE_FIELDS,
        build_capture_policy_artifact,
    )

    if type(price) is not dict or set(price) != set(MEASURED_PRICE_FIELDS):
        _refuse("bridge_price_observation_invalid")
    measured = dict(price, observed_at=_instant(price["observed_at"]))
    if type(grant) is not dict or cutoff not in (grant.get("allowed_cutoffs") or ()):
        _refuse("bridge_capture_grant_invalid")
    try:
        return build_capture_policy_artifact(
            GRANT_CAPTURE_POLICY_VERSION, price_inputs=measured, now=observed_at, grant=grant
        )
    except ValueError as error:
        raise ProducerRefusal("bridge_storage_policy_invalid") from error


def _profile(*, cutoff, artifacts, grant, source_metadata, collection_as_of):
    from src.analysis.open_intelligence.production_snapshot_tables import _schema
    from src.analysis.open_intelligence.source_estate_bridge import (
        COLLECTION_DATASET,
        COLLECTION_TABLES,
        LANES,
        PRODUCT_DATASET,
        PROFILE_VERSION,
        PROJECT,
        PROJECTION_VERSION,
        TEMPORAL_VERSION,
    )

    day = date.fromisoformat(cutoff)
    window_end = _stamp(datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC))
    profile_id = f"staging_bridge_v3_{day:%Y%m%d}"
    tables = source_metadata.get("tables") if type(source_metadata) is dict else None
    if type(tables) is not dict:
        _refuse("bridge_source_metadata_invalid")
    relations = []
    for lane in LANES:
        collection = lane in COLLECTION_TABLES
        dataset = COLLECTION_DATASET if collection else PRODUCT_DATASET
        source = f"{PROJECT}.{dataset}.{lane}"
        resource = tables.get(source)
        try:
            schema_digest = canonical_digest(_schema(resource.get("schema")))
        except (AttributeError, TypeError, ValueError) as error:
            raise ProducerRefusal("bridge_source_metadata_invalid") from error
        relations.append(
            {
                "lane": lane,
                "role": "collection_evidence" if collection else "derived_history",
                "source_table": source,
                "destination_table": f"{PROJECT}.{PRODUCT_DATASET}.{profile_id}_{lane}",
                "snapshot_as_of": _stamp(collection_as_of) if collection else window_end,
                "source_schema_digest": schema_digest,
            }
        )
    return {
        "profile_id": profile_id,
        "profile_version": PROFILE_VERSION,
        "projection_version": PROJECTION_VERSION,
        "observation_window_end": window_end,
        "source_estate_digest": grant["source_estate_digest"],
        "grant_id": grant["grant_id"],
        "schema_digest": canonical_digest(
            [
                {key: row[key] for key in ("lane", "source_table", "source_schema_digest")}
                for row in relations
            ]
        ),
        "bridge_policy_digest": canonical_digest(artifacts["bridge_policy"]),
        "temporal_rules_version": TEMPORAL_VERSION,
        "temporal_rules_digest": canonical_digest(artifacts["temporal_rules"]),
        "collection_snapshot_as_of": _stamp(collection_as_of),
        "history_snapshot_as_of": window_end,
        "collection_receipt_set_digest": canonical_digest(artifacts["collection_receipt_set"]),
        "history_completion_set_digest": canonical_digest(artifacts["history_completion_set"]),
        "relation_bindings": relations,
    }


def _client_scope_id() -> str:
    from src.analysis.open_intelligence.source_estate_bridge import MARKETS
    from src.contracts.open_intelligence import resolve_client_scope

    return resolve_client_scope(run_id=OPERATION, market_scope=MARKETS).client_scope_id


def _write_new(directory: Path, files: dict[str, bytes]) -> None:
    try:
        directory.mkdir(parents=False, exist_ok=False)
        (directory / "inputs").mkdir(exist_ok=False)
    except FileExistsError as error:
        raise ProducerRefusal("bridge_capture_inputs_output_exists") from error
    except OSError as error:
        raise ProducerRefusal("bridge_capture_inputs_write_failed") from error
    for relative, raw in files.items():
        path = directory / relative
        try:
            with path.open("xb") as handle:
                handle.write(raw)
        except FileExistsError as error:
            raise ProducerRefusal("bridge_capture_inputs_output_exists") from error
        except OSError as error:
            raise ProducerRefusal("bridge_capture_inputs_write_failed") from error
        if path.read_bytes() != raw:
            _refuse("bridge_capture_inputs_write_failed")


def produce(
    argv: Sequence[str],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    generation_loader=None,
) -> dict[str, object]:
    """Build and write the proposal. ``generation_loader`` None trusts the packaged catalogue."""
    from src.analysis.open_intelligence.source_estate_bridge import bridge_policy
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        parse_bridge_artifacts,
    )
    from src.analysis.open_intelligence.source_estate_bridge_plan import build_bridge_plan

    cutoff, paths = _parse_cli(argv)
    output = paths.pop("output_dir")
    if output.exists():
        _refuse("bridge_capture_inputs_output_exists")
    temporal_rules = _reviewed_temporal_rules()
    registry, origin, rule = _generation()
    observed_at = _utc_now(clock)
    # The funded run decides first whether it closes the named day at all.
    receipts, funded_completed = _funded_receipt_set(
        _read_json(paths["funded_chain"]),
        _read_json(paths["funded_execution"]),
        cutoff=cutoff,
        generation_loader=generation_loader,
    )

    grant = _read_json(paths["grant"])
    storage_policy = _storage_policy(
        grant, _read_json(paths["price_observation"]), observed_at, cutoff=cutoff
    )
    checked_grant = storage_policy["grant"]
    contract = _read_bytes(paths["capture_contract"])
    if hashlib.sha256(contract).hexdigest() != checked_grant["contract_sha256"]:
        _refuse("bridge_capture_contract_invalid")
    history = _history_completion_set(_read_json(paths["history_completions"]), cutoff=cutoff)
    try:
        artifacts = parse_bridge_artifacts(
            {
                "bridge_policy": bridge_policy(),
                "temporal_rules": temporal_rules,
                "collection_receipt_set": receipts,
                "history_completion_set": history,
            }
        )
    except ValueError as error:
        raise ProducerRefusal("bridge_capture_artifacts_invalid") from error
    source_metadata = _read_json(paths["source_metadata"])
    # The clones snapshot the collection once the funded execution has completed.
    collection_as_of = funded_completed
    if collection_as_of > observed_at:
        _refuse("bridge_capture_collection_not_closed")
    profile = _profile(
        cutoff=cutoff,
        artifacts=artifacts,
        grant=checked_grant,
        source_metadata=source_metadata,
        collection_as_of=collection_as_of,
    )
    try:
        plan = build_bridge_plan(
            profile=profile,
            source_metadata=source_metadata,
            storage_policy=storage_policy,
            cutoff_date=cutoff,
            client_scope_id=_client_scope_id(),
            now=observed_at,
            artifacts=artifacts,
        )
    except ValueError as error:
        raise ProducerRefusal(str(error)) from error
    arguments = execution_approval.build_source_snapshot_capture_arguments_v2(
        plan,
        grant=checked_grant,
        mode=CAPTURE_MODE,
        now=observed_at,
        rule=rule["arguments"],
        source_metadata=source_metadata,
        storage_policy=storage_policy,
        bridge_artifacts=artifacts,
    )
    switches = rule["arguments"]
    if tuple(arguments) != (
        *switches["prefix"],
        cutoff,
        switches["mode_switch"],
        CAPTURE_MODE,
        switches["grant_switch"],
        checked_grant["grant_id"],
    ):
        _refuse("bridge_capture_arguments_invalid")

    raw = {
        "bridge_policy": canonical_bytes(artifacts["bridge_policy"]),
        "capture_contract": contract,
        "capture_plan": canonical_bytes(plan),
        "collection_receipt_set": canonical_bytes(artifacts["collection_receipt_set"]),
        "history_completion_set": canonical_bytes(artifacts["history_completion_set"]),
        "recovery_context": canonical_bytes(None),
        "source_metadata": canonical_bytes(source_metadata),
        "storage_policy": canonical_bytes(storage_policy),
        "temporal_rules": canonical_bytes(artifacts["temporal_rules"]),
    }
    digests = {name: hashlib.sha256(raw[name]).hexdigest() for name in ARTIFACT_NAMES}

    build = _read_json(paths["build"])
    receipt = execution_approval.build_provenance_from_response(
        build,
        origin.contract_sha256,
        manifest_version=origin.manifest_version,
        mode=MODE,
        registry=registry,
    )
    price_expires = datetime.fromisoformat(storage_policy["price_review"]["expires_at"])
    grant_until = datetime.fromisoformat(checked_grant["valid_until"]).astimezone(UTC)
    expires_at = min(price_expires, grant_until, observed_at + timedelta(days=1)) - timedelta(
        microseconds=1
    )
    if expires_at - observed_at < MINIMUM_WINDOW:
        _refuse("bridge_capture_window_too_short")
    payload = {
        "manifest_version": origin.manifest_version,
        "operation": OPERATION,
        "contract_sha256": origin.contract_sha256,
        "project": receipt.project_id,
        "datasets": list(rule["datasets"]),
        "location": "US",
        "job_resource": JOB_RESOURCE,
        "service_identity": SERVICE_IDENTITY,
        "source_sha": receipt.resolved_source_sha,
        "image_uri": receipt.image_uri,
        "build_resource": receipt.build_resource,
        "command": list(rule["command"]),
        "arguments": list(arguments),
        "environment": [dict(item) for item in rule["environment"]],
        "secrets": list(rule["secrets"]),
        "max_retries": 0,
        "timeout_seconds": TIMEOUT_SECONDS,
        "input_artifacts": [{"name": name, "sha256": digests[name]} for name in ARTIFACT_NAMES],
        "limits": {
            "max_bytes_billed": MAX_BYTES_BILLED,
            "max_credits": 0,
            "max_model_calls": 0,
            "max_rows_written": 0,
        },
        "expires_at": _stamp(expires_at),
    }
    manifest, canonical, manifest_digest = manifest_builder._admit_manifest(
        payload, mode=MODE, registry=registry
    )
    if (
        manifest.job_resource != JOB_RESOURCE
        or manifest.service_identity != SERVICE_IDENTITY
        or dict(manifest.input_artifacts) != digests
    ):
        _refuse("bridge_capture_manifest_invalid")

    # The approval preparation over the exact bytes to be written, never over this
    # process's objects; nothing is written unless it passes.
    prepared = execution_approval.prepare_source_snapshot_capture_v3(
        dict(manifest.input_artifacts),
        artifact_reader={name: bytes(raw[name]) for name in ARTIFACT_NAMES}.__getitem__,
        rule=rule["arguments"],
        now=observed_at,
    )
    if canonical_bytes(prepared) != raw["capture_plan"]:
        _refuse("bridge_capture_plan_mismatch")
    # Each file is read back and must equal the bytes the preparation passed.
    _write_new(
        output,
        {
            **{f"inputs/{name}.json": raw[name] for name in ARTIFACT_NAMES},
            "manifest.json": canonical,
        },
    )
    result = {
        "contract_version": RECEIPT_VERSION,
        "output_dir": str(output),
        "manifest_sha256": manifest_digest,
        "operation": OPERATION,
        "arguments": list(manifest.arguments),
        "job_resource": manifest.job_resource,
        "service_identity": manifest.service_identity,
        "observed_at": _stamp(observed_at),
        "expires_at": payload["expires_at"],
        "input_artifacts": digests,
        "unavailable_history_entries": sum(
            entry["state"] == "unavailable" for entry in history["entries"]
        ),
    }
    return result


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
        manifest_builder._emit(sys.stderr, {"error": "bridge_capture_inputs_internal"})
        return 1
    manifest_builder._emit(sys.stdout, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
