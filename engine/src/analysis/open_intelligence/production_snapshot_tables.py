"""Offline preparation and readback checks for five fixed v3 table snapshots."""

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from urllib.parse import urlsplit
from weakref import WeakSet

from src.analysis.open_intelligence import pipeline as source_pipeline
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.execution_origins import (
    OriginRefusal,
    OriginRegistry,
    load_origin_registry,
    select_origin,
)

PROJECT = "ogilvy-trends-v2"
SOURCE_DATASET = "trends_v2_dev"
DESTINATION_DATASET = "trends_v2_staging"
PROFILE_ID = "trends_v2_dev_table_snapshot_v1"
LANES = (
    "event_ledger",
    "seed_graph",
    "seed_candidates",
    "enriched_content",
    "raw_content",
)
PARTITIONS = {
    "event_ledger": "trend_date",
    "seed_graph": "trend_date",
    "seed_candidates": "proposed_date",
    "enriched_content": "collected_at",
    "raw_content": "collected_at",
}
# Retained readers select one historical mode and the packaged retained registry before any
# v1 record is reconstructed. Historical v1 uses the approved retained registry only.
HISTORICAL_MODES = ("historical_read", "historical_replay")
_ENGINE_ROOT = Path(__file__).resolve().parents[3]
RETAINED_ORIGIN_REGISTRY_PATH = (
    _ENGINE_ROOT / "configs" / "open_intelligence" / "execution_origins_v1.json"
)
RETAINED_ORIGIN_REGISTRY_SHA256 = "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179"
_RETAINED_MANIFEST_VERSION = "open_intelligence_execution_manifest_v1"
_EVIDENCE_MISSING = {
    "channel_family",
    "endpoint",
    "geo_method_id",
    "geo_receipt_id",
    "native_id",
    "source_family",
    "source_family_map_version",
    "vendor_family",
}
_ALIASES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
_CREATION_INVOCATIONS = WeakSet()
_CREATION_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class SnapshotStatement:
    lane: str
    source_table: str
    destination_table: str
    source_as_of: datetime
    sql: str
    sql_digest: str
    source_schema_digest: str


@dataclass(frozen=True, slots=True)
class SnapshotPlan:
    profile_id: str
    cutoff_date: date
    source_as_of: datetime
    source_metadata_digest: str
    statements: tuple[SnapshotStatement, ...]
    plan_digest: str


def _schema(value):
    if type(value) is not dict or type(value.get("fields")) is not list:
        raise ValueError("source_metadata_invalid")

    def fields(items):
        output = []
        seen = set()
        for item in items:
            if type(item) is not dict:
                raise ValueError("source_metadata_invalid")
            name, kind = item.get("name"), item.get("type")
            mode = item.get("mode", "NULLABLE")
            if (
                type(name) is not str
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
                or name in seen
                or type(kind) is not str
                or type(mode) is not str
                or mode not in {"NULLABLE", "REQUIRED", "REPEATED"}
            ):
                raise ValueError("source_metadata_invalid")
            seen.add(name)
            nested = item.get("fields", [])
            if kind in {"RECORD", "STRUCT"}:
                if type(nested) is not list or not nested:
                    raise ValueError("source_metadata_invalid")
                children = fields(nested)
            else:
                if nested not in (None, []):
                    raise ValueError("source_metadata_invalid")
                children = []
            output.append(
                {
                    "name": name,
                    "type": _ALIASES.get(kind, kind),
                    "mode": mode,
                    "fields": children,
                }
            )
        return output

    return {"fields": fields(value["fields"])}


def _validate_source_metadata(value):
    if type(value) is not dict:
        raise ValueError("source_metadata_invalid")
    output = {}
    for lane in LANES:
        key = f"{SOURCE_DATASET}.{lane}"
        resource = value.get(key)
        required = {
            "event_ledger": set(source_pipeline.EVENT_COLUMNS),
            "seed_graph": set(source_pipeline.SEED_GRAPH_COLUMNS),
            "seed_candidates": set(source_pipeline.SEED_CANDIDATE_COLUMNS),
            "enriched_content": set(source_pipeline.EVIDENCE_COLUMNS_BY_TABLE["enriched_content"])
            - _EVIDENCE_MISSING,
            "raw_content": set(source_pipeline.EVIDENCE_COLUMNS_BY_TABLE["raw_content"])
            - _EVIDENCE_MISSING,
        }[lane]
        if (
            type(resource) is not dict
            or resource.get("status") != 200
            or resource.get("type") != "TABLE"
            or type(resource.get("timePartitioning")) is not dict
            or resource["timePartitioning"].get("type") != "DAY"
            or resource["timePartitioning"].get("field") != PARTITIONS[lane]
        ):
            raise ValueError("source_metadata_invalid")
        normalized = _schema(resource.get("schema"))
        if not required <= {field["name"] for field in normalized["fields"]}:
            raise ValueError("source_metadata_invalid")
        output[lane] = {
            "etag": resource.get("etag"),
            "schema": normalized,
            "schema_digest": canonical_digest(normalized),
            "streaming_buffer_present": "streamingBuffer" in resource,
        }
    return output


def _reviewed_source_metadata(value):
    if type(value) is not bytes or not value:
        raise ValueError("source_metadata_invalid")

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("source_metadata_invalid")
            result[key] = item
        return result

    try:
        document = json.loads(value.decode("utf-8"), object_pairs_hook=unique)
        metadata = _validate_source_metadata(document["tables"])
    except (UnicodeError, ValueError, KeyError, TypeError) as error:
        raise ValueError("source_metadata_invalid") from error
    return metadata, hashlib.sha256(value).hexdigest()


def _plan_core(profile_id, cutoff_date, source_as_of, source_metadata_digest, statements):
    return {
        "profile_id": profile_id,
        "cutoff_date": cutoff_date,
        "source_as_of": source_as_of,
        "source_metadata_digest": source_metadata_digest,
        "statements": [
            {
                field: getattr(item, field)
                for field in (
                    "lane",
                    "source_table",
                    "destination_table",
                    "source_as_of",
                    "sql",
                    "sql_digest",
                    "source_schema_digest",
                )
            }
            for item in statements
        ],
    }


def snapshot_destination_table(cutoff_date, lane):
    """The name the capture gives one lane's snapshot table of one closed observation day.

    One naming, read by the plan the capture producer runs and by every guard that has to
    recognise what that producer wrote, so the two can never drift apart.
    """
    if isinstance(cutoff_date, datetime) or type(cutoff_date) is not date or type(lane) is not str:
        raise ValueError("snapshot_destination_invalid")
    return f"open_intelligence_v3_source_{cutoff_date.strftime('%Y%m%d')}_{lane}"


def snapshot_destination(cutoff_date, lane):
    """The whole name of that table: the project and dataset the capture writes into."""
    return f"{PROJECT}.{DESTINATION_DATASET}.{snapshot_destination_table(cutoff_date, lane)}"


def build_snapshot_plan(cutoff_date, *, now, source_metadata):
    if (
        isinstance(cutoff_date, datetime)
        or type(cutoff_date) is not date
        or not isinstance(now, datetime)
        or now.tzinfo is None
    ):
        raise ValueError("snapshot_plan_invalid")
    current = now.astimezone(UTC)
    source_as_of = datetime.combine(cutoff_date + timedelta(days=1), time.min, UTC)
    age = current - source_as_of
    if age < timedelta(0) or age > timedelta(days=7):
        raise ValueError("snapshot_plan_invalid")
    try:
        metadata, metadata_digest = _reviewed_source_metadata(source_metadata)
    except ValueError as error:
        raise ValueError("snapshot_plan_invalid") from error
    stamp = source_as_of.strftime("%Y-%m-%d %H:%M:%S+00")
    statements = []
    for lane in LANES:
        source = f"{PROJECT}.{SOURCE_DATASET}.{lane}"
        destination = snapshot_destination(cutoff_date, lane)
        sql = (
            f"CREATE SNAPSHOT TABLE `{destination}`\n"
            f"CLONE `{source}`\n"
            f"FOR SYSTEM_TIME AS OF TIMESTAMP '{stamp}'"
        )
        statements.append(
            SnapshotStatement(
                lane,
                source,
                destination,
                source_as_of,
                sql,
                hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                metadata[lane]["schema_digest"],
            )
        )
    statements = tuple(statements)
    core = _plan_core(PROFILE_ID, cutoff_date, source_as_of, metadata_digest, statements)
    return SnapshotPlan(
        PROFILE_ID,
        cutoff_date,
        source_as_of,
        metadata_digest,
        statements,
        canonical_digest(core),
    )


def _timestamp(value):
    if type(value) is not str:
        raise ValueError("snapshot_readback_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("snapshot_readback_invalid")
    return parsed.astimezone(UTC)


def derive_protected_creation_statements(plan):
    if type(plan) is not SnapshotPlan:
        raise ValueError("snapshot_plan_invalid")
    as_of = datetime.combine(plan.cutoff_date + timedelta(days=1), time.min, UTC)
    core = _plan_core(
        plan.profile_id,
        plan.cutoff_date,
        plan.source_as_of,
        plan.source_metadata_digest,
        plan.statements,
    )
    if (
        plan.profile_id != PROFILE_ID
        or plan.source_as_of != as_of
        or canonical_digest(core) != plan.plan_digest
        or tuple(item.lane for item in plan.statements) != LANES
    ):
        raise ValueError("snapshot_plan_invalid")
    output = []
    for item in plan.statements:
        source = f"{PROJECT}.{SOURCE_DATASET}.{item.lane}"
        target = (
            f"{PROJECT}.{DESTINATION_DATASET}.open_intelligence_v3_source_"
            f"{plan.cutoff_date:%Y%m%d}_{item.lane}"
        )
        sql = (
            f"CREATE SNAPSHOT TABLE `{target}`\nCLONE `{source}`\n"
            f"FOR SYSTEM_TIME AS OF TIMESTAMP '{as_of:%Y-%m-%d %H:%M:%S+00}'"
        )
        if (
            item.source_table != source
            or item.destination_table != target
            or item.source_as_of != as_of
            or item.sql != sql
            or item.sql_digest != hashlib.sha256(sql.encode()).hexdigest()
        ):
            raise ValueError("snapshot_plan_invalid")
        sql += (
            "\nOPTIONS (expiration_timestamp = TIMESTAMP '"
            f"{as_of + timedelta(days=90):%Y-%m-%d %H:%M:%S+00}')"
        )
        output.append(
            {"lane": item.lane, "sql": sql, "sql_digest": hashlib.sha256(sql.encode()).hexdigest()}
        )
    return output


# D03 capture plan v2 and result v2. The grant bound consume routine reads the plan's top
# level key set and the snapshot plan fields named here; the routine text is the pin and
# this module carries the same names so a plan the Python side builds is one the routine
# accepts. The legacy v1 plan and result keep their own readers above and below untouched.
CAPTURE_PLAN_V2_VERSION = "open_intelligence_protected_capture_plan_v2"
SOURCE_SNAPSHOT_RESULT_V2_VERSION = "open_intelligence_protected_source_snapshot_v2"
CAPTURE_PLAN_FIELDS = frozenset(
    {
        "client_scope_id",
        "contract_version",
        "creation_statements",
        "cutoff_date",
        "market_scope",
        "snapshot_plan",
    }
)
CAPTURE_PLAN_V2_SNAPSHOT_FIELDS = frozenset(
    {
        "profile_id",
        "profile_version",
        "projection_version",
        "source_dataset",
        "snapshot_dataset",
        "observation_window_end",
        "snapshot_as_of",
        "source_estate_digest",
        "grant_id",
        "schema_digest",
        "statements",
    }
)
SOURCE_SNAPSHOT_RESULT_V2_FIELDS = frozenset(
    {
        "contract_version",
        "cutoff_date",
        "client_scope_id",
        "market_scope",
        "profile_id",
        "profile_version",
        "projection_version",
        "source_dataset",
        "observation_window_end",
        "snapshot_as_of",
        "source_estate_digest",
        "grant_id",
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
)
_V2_STATEMENT_FIELDS = ("lane", "source_table", "destination_table", "snapshot_as_of", "sql")
_V2_SNAPSHOT_RETENTION = timedelta(days=90)
_V2_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_V2_GRANT_ID = re.compile(r"[a-z0-9_]+\Z")
_V2_PROFILE_ID = re.compile(r"staging-(\d{4}-\d{2}-\d{2})-[0-9a-f]{16}\Z")


def _v2_instant(value, code):
    """Parse one ISO 8601 instant carried as text; naive text refuses."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(code) from error
    else:
        raise ValueError(code)
    if parsed.tzinfo is None:
        raise ValueError(code)
    return parsed.astimezone(UTC)


def _v2_grant_binding(grant, code):
    if (
        not isinstance(grant, dict)
        or not isinstance(grant.get("grant_id"), str)
        or _V2_GRANT_ID.fullmatch(grant["grant_id"]) is None
        or not isinstance(grant.get("source_estate_digest"), str)
        or _V2_HEX_64.fullmatch(grant["source_estate_digest"]) is None
        or type(grant.get("allowed_cutoffs")) is not list
        or any(type(item) is not str for item in grant["allowed_cutoffs"])
    ):
        raise ValueError(code)
    return grant["grant_id"], grant["source_estate_digest"], tuple(grant["allowed_cutoffs"])


def _v2_statements(cutoff_date, snapshot_as_of, source_dataset, snapshot_dataset, lanes):
    """The snapshot statements of one v2 plan, derived from its instants alone."""
    stamp = f"{snapshot_as_of:%Y-%m-%d %H:%M:%S.%f}+00"
    expiry = snapshot_as_of + _V2_SNAPSHOT_RETENTION
    day = cutoff_date.strftime("%Y%m%d")
    statements, creations = [], []
    for lane in lanes:
        source = f"{PROJECT}.{source_dataset}.{lane}"
        destination = f"{PROJECT}.{snapshot_dataset}.staging_source_{day}_{lane}"
        sql = (
            f"CREATE SNAPSHOT TABLE `{destination}`\n"
            f"CLONE `{source}`\n"
            f"FOR SYSTEM_TIME AS OF TIMESTAMP '{stamp}'"
        )
        statements.append(
            {
                "lane": lane,
                "source_table": source,
                "destination_table": destination,
                "snapshot_as_of": snapshot_as_of.isoformat(),
                "sql": sql,
                "sql_digest": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            }
        )
        creation = (
            sql
            + "\nOPTIONS (expiration_timestamp = TIMESTAMP '"
            + f"{expiry:%Y-%m-%d %H:%M:%S.%f}+00')"
        )
        creations.append(
            {
                "lane": lane,
                "sql": creation,
                "sql_digest": hashlib.sha256(creation.encode("utf-8")).hexdigest(),
            }
        )
    return statements, creations


def build_capture_plan_v2(profile, *, grant, client_scope_id, market_scope, now):
    """The v2 capture plan of one staging source profile under one capture grant.

    The profile supplies the native identity versions, the dataset, the window end and
    the snapshot instant; the grant supplies the estate digest, the grant id and the
    permitted cutoffs. Every value the routine asserts is checked here first.
    """
    from src.analysis.open_intelligence import staging_source_profile as policy
    from src.analysis.open_intelligence.production_snapshot import _scope

    code = "source_snapshot_plan_invalid"
    if not isinstance(profile, policy.StagingSourceProfile):
        raise ValueError(code)
    grant_id, estate_digest, allowed_cutoffs = _v2_grant_binding(grant, code)
    current = _v2_instant(now, code)
    window_end = profile.observation_window_end.astimezone(UTC)
    snapshot_as_of = profile.snapshot_as_of.astimezone(UTC)
    cutoff_date = (window_end - timedelta(days=1)).date()
    if (
        profile.profile_version != policy.NATIVE_IDENTITY_PROFILE_VERSION
        or profile.projection_version != policy.NATIVE_IDENTITY_PROJECTION_VERSION
        or profile.source_dataset != policy.STAGING_SOURCE_DATASET
        or profile.snapshot_dataset != policy.STAGING_SOURCE_DATASET
        or profile.source_tables != policy.SOURCE_LANES
        or window_end != datetime.combine(cutoff_date + timedelta(days=1), time.min, UTC)
        or snapshot_as_of < window_end
        or snapshot_as_of > current
    ):
        raise ValueError(code)
    if cutoff_date.isoformat() not in allowed_cutoffs:
        raise ValueError("source_snapshot_cutoff_not_permitted")
    scope_id, markets = _scope(client_scope_id, market_scope)
    statements, creations = _v2_statements(
        cutoff_date,
        snapshot_as_of,
        profile.source_dataset,
        profile.snapshot_dataset,
        profile.source_tables,
    )
    plan = {
        "client_scope_id": scope_id,
        "contract_version": CAPTURE_PLAN_V2_VERSION,
        "creation_statements": creations,
        "cutoff_date": cutoff_date.isoformat(),
        "market_scope": list(markets),
        "snapshot_plan": {
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "projection_version": profile.projection_version,
            "source_dataset": profile.source_dataset,
            "snapshot_dataset": profile.snapshot_dataset,
            "observation_window_end": window_end.isoformat(),
            "snapshot_as_of": snapshot_as_of.isoformat(),
            "source_estate_digest": estate_digest,
            "grant_id": grant_id,
            "schema_digest": profile.schema_digest,
            "statements": statements,
        },
    }
    return validate_capture_plan_v2(plan, cutoff=cutoff_date, grant=grant, now=current)


def validate_capture_plan_v2(envelope, *, cutoff, grant, now):
    """Refuse any v2 plan the grant bound routine would refuse, with the routine's codes.

    Shape and instant faults raise ``source_snapshot_plan_invalid``, a cutoff outside the
    grant raises ``source_snapshot_cutoff_not_permitted`` and an estate digest or grant id
    that is not the grant's raises ``source_snapshot_estate_mismatch``.
    """
    from src.analysis.open_intelligence import staging_source_profile as policy
    from src.analysis.open_intelligence.production_snapshot import _scope

    code = "source_snapshot_plan_invalid"
    if (
        type(envelope) is not dict
        or set(envelope) != CAPTURE_PLAN_FIELDS
        or envelope["contract_version"] != CAPTURE_PLAN_V2_VERSION
        or isinstance(cutoff, datetime)
        or type(cutoff) is not date
        or envelope["cutoff_date"] != cutoff.isoformat()
    ):
        raise ValueError(code)
    grant_id, estate_digest, allowed_cutoffs = _v2_grant_binding(grant, code)
    current = _v2_instant(now, code)
    try:
        _scope(envelope["client_scope_id"], envelope["market_scope"])
    except ValueError as error:
        raise ValueError(code) from error
    if type(envelope["market_scope"]) is not list:
        raise ValueError(code)
    snapshot = envelope["snapshot_plan"]
    if type(snapshot) is not dict or set(snapshot) != CAPTURE_PLAN_V2_SNAPSHOT_FIELDS:
        raise ValueError(code)
    window_end = _v2_instant(snapshot["observation_window_end"], code)
    snapshot_as_of = _v2_instant(snapshot["snapshot_as_of"], code)
    profile_id = snapshot["profile_id"]
    if (
        snapshot["profile_version"] != policy.NATIVE_IDENTITY_PROFILE_VERSION
        or snapshot["projection_version"] != policy.NATIVE_IDENTITY_PROJECTION_VERSION
        or snapshot["source_dataset"] != policy.STAGING_SOURCE_DATASET
        or snapshot["snapshot_dataset"] != policy.STAGING_SOURCE_DATASET
        or snapshot["observation_window_end"] != window_end.isoformat()
        or snapshot["snapshot_as_of"] != snapshot_as_of.isoformat()
        or window_end != datetime.combine(cutoff + timedelta(days=1), time.min, UTC)
        or snapshot_as_of < window_end
        or snapshot_as_of > current
        or not isinstance(snapshot["schema_digest"], str)
        or _V2_HEX_64.fullmatch(snapshot["schema_digest"]) is None
        or not isinstance(profile_id, str)
        or _V2_PROFILE_ID.fullmatch(profile_id) is None
        or _V2_PROFILE_ID.fullmatch(profile_id).group(1) != cutoff.isoformat()
        or not isinstance(snapshot["source_estate_digest"], str)
        or _V2_HEX_64.fullmatch(snapshot["source_estate_digest"]) is None
        or not isinstance(snapshot["grant_id"], str)
        or _V2_GRANT_ID.fullmatch(snapshot["grant_id"]) is None
    ):
        raise ValueError(code)
    statements, creations = _v2_statements(
        cutoff,
        snapshot_as_of,
        snapshot["source_dataset"],
        snapshot["snapshot_dataset"],
        policy.SOURCE_LANES,
    )
    if snapshot["statements"] != statements or envelope["creation_statements"] != creations:
        raise ValueError(code)
    if cutoff.isoformat() not in allowed_cutoffs:
        raise ValueError("source_snapshot_cutoff_not_permitted")
    if snapshot["source_estate_digest"] != estate_digest or snapshot["grant_id"] != grant_id:
        raise ValueError("source_snapshot_estate_mismatch")
    return envelope


def validate_source_snapshot_result_v2(payload, *, cutoff):
    """The v2 result record of one capture attempt: v1's shape with the v2 plan identity.

    ``source_as_of`` gives way to the profile's window end and snapshot instant, and the
    grant binding travels with the result. An attempt that never captured carries the
    empty digests and a zero query count, which is the shape the routine reads back
    before it admits a recovery.
    """
    from src.analysis.open_intelligence import staging_source_profile as policy
    from src.analysis.open_intelligence.production_snapshot import _scope

    code = "source_snapshot_result_invalid"
    if (
        type(payload) is not dict
        or set(payload) != SOURCE_SNAPSHOT_RESULT_V2_FIELDS
        or payload["contract_version"] != SOURCE_SNAPSHOT_RESULT_V2_VERSION
        or isinstance(cutoff, datetime)
        or type(cutoff) is not date
        or payload["cutoff_date"] != cutoff.isoformat()
        or type(payload["market_scope"]) is not list
    ):
        raise ValueError(code)
    try:
        _scope(payload["client_scope_id"], payload["market_scope"])
    except ValueError as error:
        raise ValueError(code) from error
    window_end = _v2_instant(payload["observation_window_end"], code)
    snapshot_as_of = _v2_instant(payload["snapshot_as_of"], code)
    profile_id = payload["profile_id"]
    if (
        payload["profile_version"] != policy.NATIVE_IDENTITY_PROFILE_VERSION
        or payload["projection_version"] != policy.NATIVE_IDENTITY_PROJECTION_VERSION
        or payload["source_dataset"] != policy.STAGING_SOURCE_DATASET
        or payload["observation_window_end"] != window_end.isoformat()
        or payload["snapshot_as_of"] != snapshot_as_of.isoformat()
        or window_end != datetime.combine(cutoff + timedelta(days=1), time.min, UTC)
        or snapshot_as_of < window_end
        or not isinstance(profile_id, str)
        or _V2_PROFILE_ID.fullmatch(profile_id) is None
        or _V2_PROFILE_ID.fullmatch(profile_id).group(1) != cutoff.isoformat()
        or not isinstance(payload["source_estate_digest"], str)
        or _V2_HEX_64.fullmatch(payload["source_estate_digest"]) is None
        or not isinstance(payload["grant_id"], str)
        or _V2_GRANT_ID.fullmatch(payload["grant_id"]) is None
        or not isinstance(payload["snapshot_plan_digest"], str)
        or _V2_HEX_64.fullmatch(payload["snapshot_plan_digest"]) is None
        or type(payload["query_count"]) is not int
        or not 0 <= payload["query_count"] <= 5
        or type(payload["creation_records"]) is not list
        or len(payload["creation_records"]) > 5
        or any(
            type(payload[field]) is not list
            or any(type(item) is not str or not item.strip() for item in payload[field])
            for field in ("limitations", "missing_checks")
        )
    ):
        raise ValueError(code)
    billed = payload["total_bytes_billed"]
    if billed is not None and (type(billed) is not int or not 0 <= billed <= 1_000_000_000):
        raise ValueError(code)
    digests = (payload["snapshot_digest"], payload["capture_receipt_digest"])
    if payload["captured_at"] is None:
        if (
            any(digest is not None for digest in digests)
            or payload["artifact_attempt"] is not None
            or payload["stored_artifact"] is not None
            or payload["query_count"] != 0
        ):
            raise ValueError(code)
        return payload
    captured_at = _v2_instant(payload["captured_at"], code)
    if payload["captured_at"] != captured_at.isoformat() or captured_at < snapshot_as_of:
        raise ValueError(code)
    if any(
        not isinstance(digest, str) or _V2_HEX_64.fullmatch(digest) is None for digest in digests
    ):
        raise ValueError(code)
    return payload


def validate_snapshot_readback(plan, *, source_metadata, snapshot_metadata, readback_rows):
    if type(plan) is not SnapshotPlan:
        raise ValueError("snapshot_readback_invalid")
    try:
        _metadata, metadata_digest = _reviewed_source_metadata(source_metadata)
        expected = build_snapshot_plan(
            plan.cutoff_date,
            now=plan.source_as_of,
            source_metadata=source_metadata,
        )
    except ValueError as error:
        raise ValueError("snapshot_readback_invalid") from error
    if metadata_digest != plan.source_metadata_digest or plan != expected:
        raise ValueError("snapshot_readback_invalid")
    if type(snapshot_metadata) is not dict or set(snapshot_metadata) != set(LANES):
        raise ValueError("snapshot_readback_invalid")
    if type(readback_rows) not in (list, tuple) or len(readback_rows) != len(LANES):
        raise ValueError("snapshot_readback_invalid")
    indexed = {}
    for row in readback_rows:
        if (
            type(row) is not dict
            or set(row) != {"lane", "table_id", "row_count"}
            or row["lane"] in indexed
            or row["lane"] not in LANES
            or type(row["row_count"]) is not int
            or row["row_count"] < 0
        ):
            raise ValueError("snapshot_readback_invalid")
        indexed[row["lane"]] = copy.deepcopy(row)
    output = []
    for item in plan.statements:
        resource = snapshot_metadata[item.lane]
        expected_table = item.destination_table.rsplit(".", 1)[-1]
        reference = {
            "projectId": PROJECT,
            "datasetId": DESTINATION_DATASET,
            "tableId": expected_table,
        }
        base = {"projectId": PROJECT, "datasetId": SOURCE_DATASET, "tableId": item.lane}
        definition = resource.get("snapshotDefinition") if type(resource) is dict else None
        count = resource.get("numRows") if type(resource) is dict else None
        if (
            type(resource) is not dict
            or resource.get("type") != "SNAPSHOT"
            or resource.get("tableReference") != reference
            or type(definition) is not dict
            or set(definition) != {"baseTableReference", "snapshotTime"}
            or definition["baseTableReference"] != base
            or _timestamp(definition["snapshotTime"]) != plan.source_as_of
            or (
                "expirationTime" in resource
                and resource["expirationTime"]
                != str(int((plan.source_as_of + timedelta(days=90)).timestamp() * 1000))
            )
            or "streamingBuffer" in resource
            or type(count) is not str
            or not count.isdecimal()
            or canonical_digest(_schema(resource.get("schema"))) != item.source_schema_digest
        ):
            raise ValueError("snapshot_readback_invalid")
        row = indexed.get(item.lane)
        if row is None or row["table_id"] != expected_table or row["row_count"] != int(count):
            raise ValueError("snapshot_readback_invalid")
        output.append(
            {
                "lane": item.lane,
                "source_table": item.source_table,
                "snapshot_table": item.destination_table,
                "snapshot_time": plan.source_as_of.isoformat(),
                "type": "SNAPSHOT",
                "etag": resource.get("etag"),
                "schema_digest": item.source_schema_digest,
                "row_count": int(count),
                "sql_digest": item.sql_digest,
            }
        )
    return {
        "contract_version": "v3_snapshot_table_preparation_v1",
        "profile_id": plan.profile_id,
        "cutoff_date": plan.cutoff_date.isoformat(),
        "source_as_of": plan.source_as_of.isoformat(),
        "plan_digest": plan.plan_digest,
        "tables": output,
        "physical_snapshot_complete": True,
        "collection_complete": False,
        "limitations": [
            "streaming_buffer_exclusion_unproven",
            "upstream_collection_completeness_unproven",
            "retention_and_cost_review_required",
        ],
    }


class SnapshotCreationError(ValueError):
    def __init__(self, code, records, evidence, metadata, rows):
        super().__init__(code)
        self.code = code
        self.creation_records = copy.deepcopy(records)
        self.creation_evidence = copy.deepcopy(evidence)
        self.snapshot_metadata = copy.deepcopy(metadata)
        self.readback_rows = copy.deepcopy(rows)
        self.failed_predicates = ()


def _creation_table_failures(resource, native, target, plan, item, observed_at, retained_bytes):
    created = resource.get("creationTime")
    definition = resource.get("snapshotDefinition")
    try:
        snapshot_time_valid = (
            _timestamp((definition or {}).get("snapshotTime")) == plan.source_as_of
        )
    except (TypeError, ValueError, AttributeError):
        snapshot_time_valid = False
    try:
        schema_valid = (
            canonical_digest(_schema(resource.get("schema"))) == item.source_schema_digest
        )
    except ValueError:
        schema_valid = False
    size = resource.get("numBytes")
    size_valid = type(size) is str and re.fullmatch(r"[0-9]+", size) is not None
    checks = {
        "location": resource.get("location") == "US",
        "reference": resource.get("tableReference") == target,
        "creation_time": type(created) is str
        and created.isdecimal()
        and int(native["statistics"]["creationTime"])
        <= int(created)
        <= int(observed_at.timestamp() * 1000),
        "type": resource.get("type") == "SNAPSHOT",
        "expiration": resource.get("expirationTime")
        == str(int((plan.source_as_of + timedelta(days=90)).timestamp() * 1000)),
        "snapshot_definition": type(definition) is dict
        and definition
        == {
            "baseTableReference": {
                "projectId": PROJECT,
                "datasetId": SOURCE_DATASET,
                "tableId": item.lane,
            },
            "snapshotTime": definition.get("snapshotTime"),
        },
        "snapshot_time": snapshot_time_valid,
        "streaming_buffer": "streamingBuffer" not in resource,
        "schema": schema_valid,
        "row_count": type(resource.get("numRows")) is str and resource["numRows"].isdecimal(),
        "size": size_valid,
        "retained_size": not size_valid or retained_bytes + int(size) <= 5368709120,
    }
    return tuple(name for name, valid in checks.items() if not valid)


def _creation_config(sql):
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(
        use_legacy_sql=False,
        use_query_cache=False,
        dry_run=False,
        job_timeout_ms=600_000,
    ).to_api_repr() | {"query": {"query": sql, "useLegacySql": False, "useQueryCache": False}}


def _creation_job(job, *, native, job_id, sql, identity, earliest, latest, target):
    from google.cloud import bigquery

    if not isinstance(job, bigquery.QueryJob):
        raise ValueError("snapshot_creation_job_invalid")
    native = copy.deepcopy(native)
    config = copy.deepcopy(native.get("configuration"))
    if type(config) is not dict or type(config.get("query")) is not dict:
        raise ValueError("snapshot_creation_job_invalid")
    if "jobType" in config and config.pop("jobType") != "QUERY":
        raise ValueError("snapshot_creation_job_invalid")
    config.setdefault("dryRun", False)
    query = config["query"]
    if "destinationTable" in query and query.pop("destinationTable") != target:
        raise ValueError("snapshot_creation_job_invalid")
    for key, default in {
        "priority": "INTERACTIVE",
        "createDisposition": "CREATE_IF_NEEDED",
        "writeDisposition": "WRITE_EMPTY",
        "allowLargeResults": False,
        "flattenResults": True,
        "maximumBillingTier": 1,
    }.items():
        if key in query:
            value = query.pop(key)
            if type(value) is not type(default) or value != default:
                raise ValueError("snapshot_creation_job_invalid")
    stats = native.get("statistics", {})
    created = stats.get("creationTime")
    state = native.get("status", {}).get("state")
    if (
        native.get("jobReference") != {"projectId": PROJECT, "location": "US", "jobId": job_id}
        or native.get("user_email") != identity
        or config != _creation_config(sql)
        or state not in {"PENDING", "RUNNING", "DONE"}
        or type(created) is not str
        or not created.isdecimal()
        or not int(earliest.timestamp() * 1000) <= int(created) <= int(latest.timestamp() * 1000)
    ):
        raise ValueError("snapshot_creation_job_invalid")
    if state == "DONE" and not native.get("status", {}).get("errorResult"):
        query_stats = stats.get("query", {})
        if (
            query_stats.get("statementType") != "CREATE_SNAPSHOT_TABLE"
            or query_stats.get("ddlOperationPerformed") != "CREATE"
            or query_stats.get("ddlTargetTable") != target
        ):
            raise ValueError("snapshot_creation_job_invalid")
    return native


def retained_origin_registry():
    return load_origin_registry(
        RETAINED_ORIGIN_REGISTRY_PATH,
        expected_sha256=RETAINED_ORIGIN_REGISTRY_SHA256,
        contract_root=_ENGINE_ROOT,
    )


def _retained_v1_origin(operation, *, mode, registry):
    if mode not in HISTORICAL_MODES:
        raise OriginRefusal("execution_origin_mode_forbidden")
    if type(registry) is not OriginRegistry:
        raise OriginRefusal("execution_origin_pair_invalid")
    matches = [
        origin
        for origin in registry.values()
        if origin.manifest_version == _RETAINED_MANIFEST_VERSION
        and isinstance(operation, str)
        and operation in origin.operation_bindings
    ]
    if len(matches) != 1:
        raise OriginRefusal("execution_origin_pair_invalid")
    return select_origin(
        manifest_version=matches[0].manifest_version,
        contract_sha256=matches[0].contract_sha256,
        mode=mode,
        registry=registry,
    )


def retained_v1_result(value, code, *, mode, registry):
    from src.analysis.open_intelligence import execution_approval

    if mode not in HISTORICAL_MODES or type(registry) is not OriginRegistry:
        raise ValueError(code)
    result = execution_approval._result_from_value(value, code)
    origin = _retained_v1_origin(result.operation, mode=mode, registry=registry)
    binding = origin.operation_bindings[result.operation]
    if not result.execution_name.startswith(binding.job_resource + "/executions/"):
        raise ValueError(code)
    return result


def retained_v2_capture_result(value, code, *, cutoff, generation_loader=None):
    """One completed v2 source snapshot capture, read from its v2 ledger result row.

    The row names its generation pair, and the pair is admitted only when the packaged
    catalogue of reviewed generations holds it; the record is then typed under that
    generation, which recomputes the result id over the pair and the digest over the
    canonical payload. The payload is validated at ``cutoff``, the cutoff the caller pins
    from an independent record (the registry or the approved manifest), never the one the
    payload names, and it must describe a capture that finished: captured, stored,
    metered, with no missing check and one succeeded creation per lane into the table the
    v2 plan derives for that cutoff. The record is read under historical replay, the mode
    every retained reader here selects.
    """
    from dataclasses import fields as dataclass_fields

    from src.analysis.open_intelligence import execution_approval
    from src.analysis.open_intelligence.protected_context_registry import v2_snapshot_tables
    from src.analysis.open_intelligence.staging_source_profile import SOURCE_LANES

    names = {field.name for field in dataclass_fields(execution_approval.ExecutionResultV2)}
    if (
        isinstance(cutoff, datetime)
        or type(cutoff) is not date
        or not isinstance(value, dict)
        or set(value) != names
    ):
        raise ValueError(code)
    row = dict(value)
    try:
        if isinstance(row["completed_at"], str):
            row["completed_at"] = datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00"))
        if not isinstance(row["completed_at"], datetime) or row["completed_at"].tzinfo is None:
            raise ValueError(code)
        pair = (row["origin_registry_sha256"], row["resource_manifest_sha256"])
        if any(type(item) is not str or _V2_HEX_64.fullmatch(item) is None for item in pair):
            raise ValueError(code)
        if generation_loader is None:
            from src.analysis.open_intelligence.execution_generations import (
                load_trusted_generation as generation_loader,
            )
        registry = getattr(generation_loader(*pair), "registry", None)
        if type(registry) is not OriginRegistry or registry.sha256 != pair[0]:
            raise ValueError(code)
        result = execution_approval.ExecutionResultV2(
            **row,
            mode="historical_replay",
            registry=registry,
            expected_resource_manifest_sha256=pair[1],
        )
        payload = json.loads(result.canonical_result_json)
        validate_source_snapshot_result_v2(payload, cutoff=cutoff)
    except (LookupError, OSError, TypeError, ValueError) as error:
        raise ValueError(code) from error
    records = payload["creation_records"]
    if (
        result.operation != "source_snapshot_capture"
        or result.status != "succeeded"
        or result.result_reference != result.execution_name + "#source-snapshot"
        or payload["captured_at"] is None
        or _v2_instant(payload["captured_at"], code) > result.completed_at
        or payload["missing_checks"] != []
        or payload["artifact_attempt"] is None
        or payload["stored_artifact"] is None
        or payload["total_bytes_billed"] is None
        or len(records) != len(SOURCE_LANES)
        or any(
            type(record) is not dict
            or record.get("lane") != lane
            or record.get("state") != "succeeded"
            or record.get("destination") != table
            for record, lane, table in zip(
                records, SOURCE_LANES, v2_snapshot_tables(cutoff), strict=True
            )
        )
    ):
        raise ValueError(code)
    return result, payload


def retained_v1_approval(value, code, *, mode, registry):
    from src.analysis.open_intelligence import execution_approval

    if mode not in HISTORICAL_MODES or type(registry) is not OriginRegistry:
        raise ValueError(code)
    approval = execution_approval._revalidate_runtime_approval(value)
    manifest = execution_approval.validate_execution_manifest(
        json.loads(approval.canonical_manifest_json), mode=mode, registry=registry
    )
    if approval.operation != manifest.operation:
        raise ValueError(code)
    return approval, manifest


def _require_retained_consumption(consumption, operation, *, mode, registry):
    origin = _retained_v1_origin(operation, mode=mode, registry=registry)
    binding = origin.operation_bindings[operation]
    execution_name = getattr(consumption, "execution_name", None)
    if (
        getattr(consumption, "operation", None) != operation
        or getattr(consumption, "job_resource", None) != binding.job_resource
        or not isinstance(execution_name, str)
        or not execution_name.startswith(binding.job_resource + "/executions/")
    ):
        raise ValueError("snapshot_creation_context_invalid")


def _creation_recovery(context, reader, capture_plan, plan, now):

    keys = {
        "contract_version",
        "initial_manifest_sha256",
        "initial_consumption_id",
        "initial_execution_name",
        "initial_result_id",
        "initial_result_digest",
    }
    if (
        type(context) is dict
        and context.get("contract_version") == "open_intelligence_source_capture_recovery_v2"
    ):
        keys.add("failed_creation_job_digest")
        digest = context.get("failed_creation_job_digest")
        if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("snapshot_creation_recovery_invalid")
    continuation = type(context) is dict and context.get("contract_version") in {
        "open_intelligence_source_capture_recovery_v3",
        "open_intelligence_source_capture_recovery_v4",
    }
    metadata_continuation = (
        continuation
        and context["contract_version"] == "open_intelligence_source_capture_recovery_v4"
    )
    if continuation:
        keys.add("ancestor_recovery_context")
        ancestor_context = context.get("ancestor_recovery_context")
        expected_ancestor = (
            "open_intelligence_source_capture_recovery_v3"
            if metadata_continuation
            else "open_intelligence_source_capture_recovery_v2"
        )
        if (
            type(ancestor_context) is not dict
            or ancestor_context.get("contract_version") != expected_ancestor
        ):
            raise ValueError("snapshot_creation_recovery_invalid")
    if (
        type(context) is not dict
        or set(context) != keys
        or not callable(reader)
        or context["contract_version"]
        not in {
            "open_intelligence_source_capture_recovery_v1",
            "open_intelligence_source_capture_recovery_v2",
            "open_intelligence_source_capture_recovery_v3",
            "open_intelligence_source_capture_recovery_v4",
        }
    ):
        raise ValueError("snapshot_creation_recovery_invalid")
    result = retained_v1_result(
        reader(context["initial_result_id"]),
        "snapshot_creation_recovery_invalid",
        mode="historical_replay",
        registry=retained_origin_registry(),
    )
    if (
        result.operation != "source_snapshot_capture"
        or result.completed_at > now
        or any(
            getattr(result, field) != context["initial_" + field]
            for field in (
                "manifest_sha256",
                "consumption_id",
                "execution_name",
                "result_id",
                "result_digest",
            )
        )
    ):
        raise ValueError("snapshot_creation_recovery_invalid")
    payload = json.loads(result.canonical_result_json)
    expected = {
        "contract_version": "open_intelligence_protected_source_snapshot_v1",
        "cutoff_date": plan.cutoff_date.isoformat(),
        "source_as_of": plan.source_as_of.isoformat(),
        "client_scope_id": capture_plan["client_scope_id"],
        "market_scope": capture_plan["market_scope"],
        "snapshot_plan_digest": plan.plan_digest,
    }
    records = payload.get("creation_records")
    if (
        any(payload.get(key) != value for key, value in expected.items())
        or type(records) is not list
        or len(records) > 5
    ):
        raise ValueError("snapshot_creation_recovery_invalid")
    ancestor, ancestor_records = None, []
    if continuation:
        ancestor, ancestor_records = _creation_recovery(
            ancestor_context, reader, capture_plan, plan, now
        )
        if (
            ancestor.manifest_sha256 == result.manifest_sha256
            or ancestor.completed_at > result.completed_at
            or not 1 <= len(ancestor_records) <= len(records)
            or any(record["state"] != "succeeded" for record in ancestor_records[:-1])
            or (
                not metadata_continuation
                and ancestor_records[-1]["state"] not in {"failed", "unresolved"}
            )
        ):
            raise ValueError("snapshot_creation_recovery_invalid")
        for baseline in (ancestor, result):
            baseline_payload = json.loads(baseline.canonical_result_json)
            if (
                baseline.status != "failed"
                or type(baseline_payload.get("query_count")) is not int
                or baseline_payload["query_count"] != 0
                or any(
                    field not in baseline_payload or baseline_payload[field] is not None
                    for field in (
                        "captured_at",
                        "snapshot_digest",
                        "capture_receipt_digest",
                        "artifact_attempt",
                        "stored_artifact",
                    )
                )
            ):
                raise ValueError("snapshot_creation_recovery_invalid")
    for index, (item, record) in enumerate(zip(plan.statements, records, strict=False)):
        owner = (
            ancestor.manifest_sha256
            if continuation and index < len(ancestor_records) - 1
            else result.manifest_sha256
        )
        expected_job = (
            ancestor_records[index]["job_id"]
            if metadata_continuation and index < len(ancestor_records)
            else f"oi_v3_snapshot_{owner}_{item.lane}"
        )
        if (
            type(record) is not dict
            or set(record) != {"lane", "destination", "job_id", "native_job_digest", "state"}
            or record["lane"] != item.lane
            or record["destination"] != item.destination_table
            or record["job_id"] != expected_job
            or record["state"] not in {"succeeded", "failed", "unresolved"}
            or (
                record["native_job_digest"] is not None
                and re.fullmatch("[0-9a-f]{64}", str(record["native_job_digest"])) is None
            )
        ):
            raise ValueError("snapshot_creation_recovery_invalid")
        if continuation and (
            record["state"] == "failed"
            or (
                index < len(ancestor_records)
                and ancestor_records[index]["state"] == "succeeded"
                and record != ancestor_records[index]
            )
        ):
            raise ValueError("snapshot_creation_recovery_invalid")
    return result, copy.deepcopy(records)


def _validate_failed_creation_evidence(
    initial, records, entry, plan, statements, identity, recovery_context
):
    from types import SimpleNamespace

    from google.cloud import bigquery

    payload = json.loads(initial.canonical_result_json)
    empty_fields = (
        "captured_at",
        "snapshot_digest",
        "capture_receipt_digest",
        "artifact_attempt",
        "stored_artifact",
    )
    if (
        recovery_context.get("contract_version") != "open_intelligence_source_capture_recovery_v2"
        or initial.status != "failed"
        or type(payload.get("query_count")) is not int
        or payload["query_count"] != 0
        or any(field not in payload or payload[field] is not None for field in empty_fields)
        or not 1 <= len(records) <= 5
        or any(record["state"] != "succeeded" for record in records[:-1])
        or records[-1]["state"] not in {"failed", "unresolved"}
    ):
        raise ValueError("snapshot_creation_recovery_invalid")
    index = len(records) - 1
    record, statement = records[index], plan.statements[index]
    if (
        type(entry) is not dict
        or set(entry)
        != {
            "lane",
            "manifest_sha256",
            "consumption_id",
            "execution_name",
            "native_job",
            "snapshot_metadata",
        }
        or entry["lane"] != statement.lane
        or entry["manifest_sha256"] != initial.manifest_sha256
        or entry["consumption_id"] != initial.consumption_id
        or entry["execution_name"] != initial.execution_name
        or entry["snapshot_metadata"] is not None
    ):
        raise ValueError("snapshot_creation_recovery_invalid")
    native = _creation_job(
        bigquery.QueryJob.from_api_repr(
            copy.deepcopy(entry["native_job"]), SimpleNamespace(project=PROJECT)
        ),
        native=entry["native_job"],
        job_id=record["job_id"],
        sql=statements[index]["sql"],
        identity=identity,
        earliest=plan.source_as_of,
        latest=initial.completed_at,
        target={
            "projectId": PROJECT,
            "datasetId": DESTINATION_DATASET,
            "tableId": statement.destination_table.rsplit(".", 1)[1],
        },
    )
    if (
        canonical_digest(native) != recovery_context.get("failed_creation_job_digest")
        or native["status"]["state"] != "DONE"
        or type(native["status"].get("errorResult")) is not dict
        or not native["status"]["errorResult"]
        or native.get("statistics", {}).get("query", {}).get("ddlOperationPerformed") == "CREATE"
        or (
            record["native_job_digest"] is not None
            and record["native_job_digest"] != canonical_digest(native)
        )
    ):
        raise ValueError("snapshot_creation_recovery_invalid")
    return index


def execute_protected_snapshot_creations(
    plan,
    *,
    creation_statements,
    source_metadata,
    authority,
    consumption,
    client,
    now,
    capture_plan,
    recovery_context=None,
    initial_result_reader=None,
    prior_evidence=(),
    prior_records=(),
    metadata_diagnostics=None,
):
    from src.analysis.open_intelligence import execution_approval

    _require_retained_consumption(
        consumption,
        "source_snapshot_capture",
        mode="historical_replay",
        registry=retained_origin_registry(),
    )
    execution_approval._require_consumed_execution(
        authority, consumption, "source_snapshot_capture"
    )

    def recheck():
        execution_approval._require_consumed_execution(
            authority, consumption, "source_snapshot_capture"
        )

    def claim():
        with _CREATION_LOCK:
            if authority in _CREATION_INVOCATIONS:
                raise ValueError("snapshot_creation_already_attempted")
            _CREATION_INVOCATIONS.add(authority)

    return _execute_validated_snapshot_creations(
        plan,
        manifest=authority.manifest,
        consumption=consumption,
        recheck=recheck,
        claim=claim,
        creation_statements=creation_statements,
        source_metadata=source_metadata,
        client=client,
        now=now,
        capture_plan=capture_plan,
        recovery_context=recovery_context,
        initial_result_reader=initial_result_reader,
        prior_evidence=prior_evidence,
        prior_records=prior_records,
        metadata_diagnostics=metadata_diagnostics,
    )


def _execute_validated_snapshot_creations(
    plan,
    *,
    manifest,
    consumption,
    recheck,
    claim,
    creation_statements,
    source_metadata,
    client,
    now,
    capture_plan,
    recovery_context=None,
    initial_result_reader=None,
    prior_evidence=(),
    prior_records=(),
    metadata_diagnostics=None,
):
    from google.cloud import bigquery

    from src.analysis.open_intelligence.production_snapshot import _scope, _serialize_plan

    started = monotonic()
    retained_registry = retained_origin_registry()
    _require_retained_consumption(
        consumption, "source_snapshot_capture", mode="historical_replay", registry=retained_registry
    )
    if not callable(recheck) or not callable(claim):
        raise ValueError("snapshot_creation_context_invalid")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("snapshot_creation_context_invalid")
    now = now.astimezone(UTC)
    deadline = min(
        manifest.expires_at, consumption.consumed_at + timedelta(seconds=manifest.timeout_seconds)
    )

    def clock():
        return now + timedelta(seconds=max(0, monotonic() - started))

    def remaining():
        duration = (deadline - clock()).total_seconds()
        if duration <= 0:
            raise ValueError("snapshot_creation_expired")
        return min(30, duration)

    remaining()
    if now < consumption.consumed_at:
        raise ValueError("snapshot_creation_context_invalid")
    expected_plan = build_snapshot_plan(plan.cutoff_date, now=now, source_metadata=source_metadata)
    statements = derive_protected_creation_statements(plan)
    artifacts = dict(manifest.input_artifacts)
    if (
        plan != expected_plan
        or creation_statements != statements
        or type(capture_plan) is not dict
        or set(capture_plan)
        != {
            "contract_version",
            "cutoff_date",
            "client_scope_id",
            "market_scope",
            "snapshot_plan",
            "creation_statements",
        }
        or capture_plan["contract_version"] != "open_intelligence_protected_capture_plan_v1"
        or capture_plan["cutoff_date"] != plan.cutoff_date.isoformat()
        or capture_plan["snapshot_plan"] != _serialize_plan(plan)
        or capture_plan["creation_statements"] != statements
        or hashlib.sha256(canonical_bytes(capture_plan)).hexdigest()
        != artifacts.get("capture_plan")
        or hashlib.sha256(source_metadata).hexdigest() != artifacts.get("source_metadata")
        or hashlib.sha256(canonical_bytes(recovery_context)).hexdigest()
        != artifacts.get("recovery_context")
    ):
        raise ValueError("snapshot_creation_context_invalid")
    _scope(capture_plan["client_scope_id"], capture_plan["market_scope"])
    mode = "initial" if recovery_context is None else "recover"
    if manifest.arguments != (
        "scripts/staging/capture_protected_production_snapshot.py",
        "--cutoff-date",
        plan.cutoff_date.isoformat(),
        "--mode",
        mode,
    ):
        raise ValueError("snapshot_creation_context_invalid")
    initial, original_records, ancestor, ancestor_records, origin, origin_records = (
        None,
        [],
        None,
        [],
        None,
        [],
    )
    continuation = recovery_context is not None and recovery_context.get("contract_version") in {
        "open_intelligence_source_capture_recovery_v3",
        "open_intelligence_source_capture_recovery_v4",
    }
    metadata_continuation = (
        continuation
        and recovery_context["contract_version"] == "open_intelligence_source_capture_recovery_v4"
    )
    if recovery_context is not None:
        initial, original_records = _creation_recovery(
            recovery_context, initial_result_reader, capture_plan, plan, clock()
        )
        if initial.manifest_sha256 == consumption.manifest_sha256:
            raise ValueError("snapshot_creation_recovery_invalid")
        if continuation:
            ancestor, ancestor_records = _creation_recovery(
                recovery_context["ancestor_recovery_context"],
                initial_result_reader,
                capture_plan,
                plan,
                clock(),
            )
            if ancestor.manifest_sha256 == consumption.manifest_sha256:
                raise ValueError("snapshot_creation_recovery_invalid")
            if metadata_continuation:
                origin, origin_records = _creation_recovery(
                    recovery_context["ancestor_recovery_context"]["ancestor_recovery_context"],
                    initial_result_reader,
                    capture_plan,
                    plan,
                    clock(),
                )
                if origin.manifest_sha256 in {
                    consumption.manifest_sha256,
                    initial.manifest_sha256,
                    ancestor.manifest_sha256,
                }:
                    raise ValueError("snapshot_creation_recovery_invalid")
    if prior_records and list(prior_records) != original_records:
        raise ValueError("snapshot_creation_recovery_invalid")
    if prior_evidence and recovery_context is None:
        raise ValueError("snapshot_creation_recovery_invalid")
    if (
        not isinstance(client, bigquery.Client)
        or client.project != PROJECT
        or client.location not in (None, "US")
        or getattr(client._credentials, "service_account_email", None) != manifest.service_identity
        or client._default_query_job_config is not None
        or client._connection.API_BASE_URL != "https://bigquery.googleapis.com"
        or getattr(client._http, "is_mtls", False)
    ):
        raise ValueError("snapshot_creation_client_invalid")
    claim()
    records, evidence, metadata, rows = copy.deepcopy(original_records), [], {}, []
    retained_bytes = 0
    failed_predicates = ()
    original_http = client._http.request
    allowed_gets = set()
    pending_post = None
    native_jobs = {}

    def guarded_http(method, url, **kwargs):
        nonlocal pending_post
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "bigquery.googleapis.com"
            or parsed.fragment
        ):
            raise ValueError("snapshot_creation_transport_invalid")
        if method == "POST":
            if parsed.path != f"/bigquery/v2/projects/{PROJECT}/jobs" or pending_post is None:
                raise ValueError("snapshot_creation_transport_invalid")
            body = json.loads(kwargs.get("data", "null"))
            if (
                body.get("jobReference") != pending_post[0]
                or body.get("configuration") != pending_post[1]
                or set(body) != {"jobReference", "configuration"}
            ):
                raise ValueError("snapshot_creation_transport_invalid")
            remaining()
            pending_post = None
        elif method != "GET" or parsed.path not in allowed_gets:
            raise ValueError("snapshot_creation_transport_invalid")
        kwargs["allow_redirects"] = False
        duration = remaining()
        if type(kwargs.get("timeout")) in (int, float):
            duration = min(duration, kwargs["timeout"])
        kwargs["timeout"] = duration
        response = original_http(method, url, **kwargs)
        if method == "GET" and "/jobs/" in parsed.path and response.status_code == 200:
            native_jobs[parsed.path.rsplit("/", 1)[1]] = copy.deepcopy(response.json())
        return response

    client._http.request = guarded_http
    try:
        replacement_index = None
        if continuation:
            failed_owner = origin if metadata_continuation else ancestor
            failed_records = origin_records if metadata_continuation else ancestor_records
            failed_context = (
                recovery_context["ancestor_recovery_context"]["ancestor_recovery_context"]
                if metadata_continuation
                else recovery_context["ancestor_recovery_context"]
            )
            previous = failed_records[-1]
            job_id = previous["job_id"]
            allowed_gets = {f"/bigquery/v2/projects/{PROJECT}/jobs/{job_id}"}
            client.get_job(job_id, project=PROJECT, location="US", retry=None, timeout=remaining())
            failed_entry = {
                "lane": previous["lane"],
                "manifest_sha256": failed_owner.manifest_sha256,
                "consumption_id": failed_owner.consumption_id,
                "execution_name": failed_owner.execution_name,
                "native_job": native_jobs[job_id],
                "snapshot_metadata": None,
            }
            _validate_failed_creation_evidence(
                failed_owner,
                failed_records,
                failed_entry,
                plan,
                statements,
                manifest.service_identity,
                failed_context,
            )
            evidence.append(failed_entry)
        elif original_records and original_records[-1]["state"] in {"failed", "unresolved"}:
            previous = original_records[-1]
            job_id = previous["job_id"]
            table_name = previous["destination"].rsplit(".", 1)[1]
            allowed_gets = {
                f"/bigquery/v2/projects/{PROJECT}/jobs/{job_id}",
                f"/bigquery/v2/projects/{PROJECT}/datasets/{DESTINATION_DATASET}/tables/{table_name}",
            }
            client.get_job(job_id, project=PROJECT, location="US", retry=None, timeout=remaining())
            native = native_jobs[job_id]
            if native.get("status", {}).get("state") == "DONE" and native["status"].get(
                "errorResult"
            ):
                failed_entry = {
                    "lane": previous["lane"],
                    "manifest_sha256": initial.manifest_sha256,
                    "consumption_id": initial.consumption_id,
                    "execution_name": initial.execution_name,
                    "native_job": native,
                    "snapshot_metadata": None,
                }
                replacement_index = _validate_failed_creation_evidence(
                    initial,
                    original_records,
                    failed_entry,
                    plan,
                    statements,
                    manifest.service_identity,
                    recovery_context,
                )
                from google.api_core.exceptions import NotFound

                try:
                    client.get_table(previous["destination"], retry=None, timeout=remaining())
                except NotFound:
                    pass
                else:
                    raise ValueError("snapshot_creation_recovery_target_exists")
                evidence.append(failed_entry)
        if (
            recovery_context is not None
            and recovery_context["contract_version"]
            == "open_intelligence_source_capture_recovery_v2"
            and replacement_index is None
        ):
            raise ValueError("snapshot_creation_recovery_invalid")
        for index, (item, statement) in enumerate(zip(plan.statements, statements, strict=True)):
            previous = original_records[index] if index < len(original_records) else None
            if index == replacement_index:
                previous = None
            previous_owner = next(
                (
                    owner
                    for owner in (initial, ancestor, origin)
                    if owner is not None
                    and previous is not None
                    and previous["job_id"] == f"oi_v3_snapshot_{owner.manifest_sha256}_{item.lane}"
                ),
                initial,
            )
            manifest_id = (
                previous_owner.manifest_sha256 if previous else consumption.manifest_sha256
            )
            owner_id = previous_owner.consumption_id if previous else consumption.consumption_id
            execution_name = (
                previous_owner.execution_name if previous else consumption.execution_name
            )
            job_id = f"oi_v3_snapshot_{manifest_id}_{item.lane}"
            reference = {"projectId": PROJECT, "location": "US", "jobId": job_id}
            table_name = item.destination_table.rsplit(".", 1)[1]
            target = {"projectId": PROJECT, "datasetId": DESTINATION_DATASET, "tableId": table_name}
            allowed_gets = {
                f"/bigquery/v2/projects/{PROJECT}/jobs/{job_id}",
                f"/bigquery/v2/projects/{PROJECT}/datasets/{DESTINATION_DATASET}/tables/{table_name}",
            }
            record = {
                "lane": item.lane,
                "destination": item.destination_table,
                "job_id": job_id,
                "native_job_digest": None,
                "state": "unresolved",
            }
            if index >= len(records):
                records.append(record)
            else:
                records[index] = record
            if previous is None:
                _require_retained_consumption(
                    consumption,
                    "source_snapshot_capture",
                    mode="historical_replay",
                    registry=retained_registry,
                )
                recheck()
                config = _creation_config(statement["sql"])
                pending_post = (reference, config)
                try:
                    client.query(
                        statement["sql"],
                        job_id=job_id,
                        job_config=bigquery.QueryJobConfig.from_api_repr(config),
                        project=PROJECT,
                        location="US",
                        retry=None,
                        job_retry=None,
                        timeout=remaining(),
                        api_method=bigquery.enums.QueryApiMethod.INSERT,
                    )
                except Exception:
                    # A lost acknowledgement only permits exact job readback.
                    pass
                finally:
                    pending_post = None
            job = client.get_job(
                job_id, project=PROJECT, location="US", retry=None, timeout=remaining()
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
                    job_id, project=PROJECT, location="US", retry=None, timeout=remaining()
                )
            limits = {
                "job_id": job_id,
                "sql": statement["sql"],
                "identity": manifest.service_identity,
                "earliest": plan.source_as_of if previous else consumption.consumed_at,
                "latest": min(clock(), previous_owner.completed_at) if previous else clock(),
                "target": target,
            }
            native = _creation_job(job, native=native, **limits)
            record["native_job_digest"] = canonical_digest(native)
            entry = {
                "lane": item.lane,
                "manifest_sha256": manifest_id,
                "consumption_id": owner_id,
                "execution_name": execution_name,
                "native_job": native,
                "snapshot_metadata": None,
            }
            evidence.append(entry)
            if native["status"].get("errorResult"):
                record["state"] = "failed"
                raise ValueError("snapshot_creation_failed")
            if previous and previous["state"] == "failed":
                raise ValueError("snapshot_creation_recovery_invalid")
            if (
                previous
                and previous["state"] == "succeeded"
                and previous["native_job_digest"] != record["native_job_digest"]
            ):
                raise ValueError("snapshot_creation_recovery_invalid")
            if native["statistics"]["query"].get("totalBytesBilled") != "0":
                raise ValueError("snapshot_creation_billing_unresolved")
            metadata_deadline = min(deadline, clock() + timedelta(seconds=30))
            metadata_code = "snapshot_creation_table_invalid"
            metadata_warned = False
            while True:
                remaining()
                duration = (metadata_deadline - clock()).total_seconds()
                if duration <= 0:
                    raise ValueError(metadata_code)
                resource = copy.deepcopy(
                    client.get_table(
                        item.destination_table, retry=None, timeout=min(remaining(), duration)
                    ).to_api_repr()
                )
                entry["snapshot_metadata"] = resource
                failed_predicates = _creation_table_failures(
                    resource, native, target, plan, item, clock(), retained_bytes
                )
                if failed_predicates and not metadata_warned:
                    metadata_warned = True
                    if metadata_diagnostics is not None:
                        metadata_diagnostics(item.lane, failed_predicates[0])
                remaining()
                if not failed_predicates and clock() <= metadata_deadline:
                    break
                if set(failed_predicates) == {"size"}:
                    metadata_code = "snapshot_creation_size_unavailable"
                elif set(failed_predicates) == {"retained_size"}:
                    metadata_code = "snapshot_creation_size_exceeded"
                else:
                    metadata_code = "snapshot_creation_table_invalid"
                duration = (metadata_deadline - clock()).total_seconds()
                if duration <= 0:
                    raise ValueError(metadata_code)
                sleep(min(1, remaining(), duration))
            retained_bytes += int(resource["numBytes"])
            metadata[item.lane] = resource
            rows.append(
                {"lane": item.lane, "table_id": table_name, "row_count": int(resource["numRows"])}
            )
            record["state"] = "succeeded"
        validate_snapshot_readback(
            plan, source_metadata=source_metadata, snapshot_metadata=metadata, readback_rows=rows
        )
        remaining()
        return {
            "creation_records": records,
            "creation_evidence": evidence,
            "snapshot_metadata": metadata,
            "readback_rows": rows,
        }
    except Exception as error:
        code = (
            str(error)
            if type(error) is ValueError and str(error).startswith("snapshot_creation_")
            else "snapshot_creation_unresolved"
        )
        failure = SnapshotCreationError(code, records, evidence, metadata, rows)
        failure.failed_predicates = failed_predicates
        raise failure from None
    finally:
        client._http.request = original_http


__all__ = [
    "LANES",
    "SnapshotCreationError",
    "SnapshotPlan",
    "SnapshotStatement",
    "build_snapshot_plan",
    "derive_protected_creation_statements",
    "execute_protected_snapshot_creations",
    "snapshot_destination",
    "snapshot_destination_table",
    "validate_snapshot_readback",
]
