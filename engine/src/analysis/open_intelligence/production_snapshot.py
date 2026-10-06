"""Assemble structurally validated production source snapshot material."""

import copy
import json
import re
from datetime import UTC, date, datetime

from scripts.staging.replay_open_intelligence import _digest

from src.analysis.open_intelligence.production_snapshot_rows import (
    normalize_snapshot_rows,
)
from src.analysis.open_intelligence.production_snapshot_tables import (
    LANES,
    PROFILE_ID,
    SnapshotPlan,
    SnapshotStatement,
    validate_snapshot_readback,
)

ASSEMBLY_CONTRACT_VERSION = "open_intelligence_production_snapshot_assembly_v1"
SNAPSHOT_CONTRACT_VERSION = "open_intelligence_source_snapshot_v3_production_v1"
_COVERAGE_KINDS = {"exposure", "pipeline_run", "source_window"}
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ASSEMBLY_FIELDS = (
    "assembly_contract_version",
    "snapshot",
    "source_metadata_utf8",
    "snapshot_plan",
    "snapshot_metadata",
    "readback_rows",
    "row_material",
    "collection_complete",
    "source_authority",
    "external_authority_required",
    "assembly_digest",
)
_SNAPSHOT_FIELDS = (
    "snapshot_contract_version",
    "snapshot_id",
    "cutoff_date",
    "source_as_of",
    "captured_at",
    "client_scope_id",
    "market_scope",
    "source_relation_set_id",
    "row_counts_by_table",
    "section_counts",
    "section_identity_sets",
    "table_digests",
    "rows_by_table",
    "table_snapshot_receipts",
    "physical_capture_complete",
    "coverage_receipt_refs",
    "coverage_limitations",
    "source_digest",
)
_EXTERNAL_AUTHORITY_REQUIRED = (
    "normalized_row_query_execution_proof_required",
    "snapshot_creation_execution_proof_required",
    "snapshot_storage_execution_proof_required",
)
_STRUCTURAL_LIMITATIONS = (
    "normalized_row_query_execution_proof_required",
    "retention_and_cost_review_required",
    "snapshot_creation_execution_proof_required",
    "streaming_buffer_exclusion_unproven",
    "upstream_collection_completeness_unproven",
)


def _exact_fields(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("production_snapshot_invalid")


def _json_copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("production_snapshot_invalid") from error


def _scope(client_scope_id, market_scope):
    if (
        type(client_scope_id) is not str
        or not client_scope_id.strip()
        or client_scope_id != client_scope_id.strip()
        or len(client_scope_id) > 256
        or any(ord(character) < 32 for character in client_scope_id)
        or type(market_scope) not in (list, tuple)
        or not market_scope
        or tuple(market_scope) != tuple(sorted(set(market_scope)))
        or any(market not in {"ke", "ng", "za"} for market in market_scope)
    ):
        raise ValueError("production_snapshot_invalid")
    return client_scope_id, tuple(market_scope)


def _coverage_references(value):
    if type(value) not in (list, tuple):
        raise ValueError("production_snapshot_invalid")
    output = []
    for item in value:
        _exact_fields(item, ("kind", "receipt_id", "receipt_digest"))
        kind = item["kind"]
        receipt_id = item["receipt_id"]
        receipt_digest = item["receipt_digest"]
        if (
            kind not in _COVERAGE_KINDS
            or type(receipt_id) is not str
            or not receipt_id.strip()
            or receipt_id != receipt_id.strip()
            or len(receipt_id) > 512
            or any(ord(character) < 32 for character in receipt_id)
            or type(receipt_digest) is not str
            or _DIGEST.fullmatch(receipt_digest) is None
        ):
            raise ValueError("production_snapshot_invalid")
        output.append(
            {
                "kind": kind,
                "receipt_id": receipt_id,
                "receipt_digest": receipt_digest,
            }
        )
    ordering = [(item["kind"], item["receipt_id"], item["receipt_digest"]) for item in output]
    if ordering != sorted(ordering) or len(ordering) != len(set(ordering)):
        raise ValueError("production_snapshot_invalid")
    return output


def _serialize_plan(plan):
    if type(plan) is not SnapshotPlan:
        raise ValueError("production_snapshot_invalid")
    return {
        "profile_id": plan.profile_id,
        "cutoff_date": plan.cutoff_date.isoformat(),
        "source_as_of": plan.source_as_of.astimezone(UTC).isoformat(),
        "source_metadata_digest": plan.source_metadata_digest,
        "statements": [
            {
                "lane": item.lane,
                "source_table": item.source_table,
                "destination_table": item.destination_table,
                "source_as_of": item.source_as_of.astimezone(UTC).isoformat(),
                "sql": item.sql,
                "sql_digest": item.sql_digest,
                "source_schema_digest": item.source_schema_digest,
            }
            for item in plan.statements
        ],
        "plan_digest": plan.plan_digest,
    }


def _utc_timestamp(value):
    if type(value) is not str:
        raise ValueError("production_snapshot_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("production_snapshot_invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("production_snapshot_invalid")
    parsed = parsed.astimezone(UTC)
    if parsed.isoformat() != value:
        raise ValueError("production_snapshot_invalid")
    return parsed


def _deserialize_plan(value):
    _exact_fields(
        value,
        (
            "profile_id",
            "cutoff_date",
            "source_as_of",
            "source_metadata_digest",
            "statements",
            "plan_digest",
        ),
    )
    if type(value["cutoff_date"]) is not str or type(value["statements"]) is not list:
        raise ValueError("production_snapshot_invalid")
    try:
        cutoff = date.fromisoformat(value["cutoff_date"])
    except ValueError as error:
        raise ValueError("production_snapshot_invalid") from error
    if cutoff.isoformat() != value["cutoff_date"]:
        raise ValueError("production_snapshot_invalid")
    statements = []
    for item in value["statements"]:
        _exact_fields(
            item,
            (
                "lane",
                "source_table",
                "destination_table",
                "source_as_of",
                "sql",
                "sql_digest",
                "source_schema_digest",
            ),
        )
        statements.append(
            SnapshotStatement(
                lane=item["lane"],
                source_table=item["source_table"],
                destination_table=item["destination_table"],
                source_as_of=_utc_timestamp(item["source_as_of"]),
                sql=item["sql"],
                sql_digest=item["sql_digest"],
                source_schema_digest=item["source_schema_digest"],
            )
        )
    return SnapshotPlan(
        profile_id=value["profile_id"],
        cutoff_date=cutoff,
        source_as_of=_utc_timestamp(value["source_as_of"]),
        source_metadata_digest=value["source_metadata_digest"],
        statements=tuple(statements),
        plan_digest=value["plan_digest"],
    )


def _table_receipts(table_result, row_material):
    output = []
    for item in table_result["tables"]:
        lane = item["lane"]
        output.append(
            {
                **copy.deepcopy(item),
                "physical_result_digest": row_material["physical_table_digests"][lane],
                "adapted_result_digest": row_material["adapted_table_digests"][lane],
                "execution_authority": False,
            }
        )
    return output


def _snapshot_id(*, cutoff_date, captured_at, client_scope_id, markets, plan, material):
    identity = _digest(
        {
            "domain": SNAPSHOT_CONTRACT_VERSION,
            "cutoff_date": cutoff_date.isoformat(),
            "captured_at": captured_at.astimezone(UTC).isoformat(),
            "client_scope_id": client_scope_id,
            "market_scope": list(markets),
            "plan_digest": plan.plan_digest,
            "material_digest": material["material_digest"],
        }
    )
    return "production_snapshot_" + identity


def _section_identities(rows_by_table):
    return {
        lane: [f"{index:08d}:{_digest(row)}" for index, row in enumerate(rows_by_table[lane])]
        for lane in LANES
    }


def build_production_snapshot(
    *,
    cutoff_date,
    captured_at,
    client_scope_id,
    market_scope,
    reviewed_metadata,
    plan,
    snapshot_metadata,
    readback_rows,
    rows_by_table,
    coverage_receipt_refs=(),
):
    try:
        if isinstance(cutoff_date, datetime) or type(cutoff_date) is not date:
            raise ValueError()
        client_scope_id, markets = _scope(client_scope_id, market_scope)
        if not isinstance(captured_at, datetime) or captured_at.tzinfo is None:
            raise ValueError()
        captured_at = captured_at.astimezone(UTC)
        if type(reviewed_metadata) is not bytes:
            raise ValueError()
        metadata_text = reviewed_metadata.decode("utf-8")
        table_result = validate_snapshot_readback(
            plan,
            source_metadata=reviewed_metadata,
            snapshot_metadata=snapshot_metadata,
            readback_rows=readback_rows,
        )
        if (
            plan.profile_id != PROFILE_ID
            or plan.cutoff_date != cutoff_date
            or plan.source_as_of.isoformat() != table_result["source_as_of"]
        ):
            raise ValueError()
        row_material = normalize_snapshot_rows(
            cutoff_date,
            source_as_of=plan.source_as_of,
            captured_at=captured_at,
            market_scope=markets,
            reviewed_metadata=reviewed_metadata,
            rows_by_table=rows_by_table,
        )
        full_snapshot_counts = {item["lane"]: item["row_count"] for item in table_result["tables"]}
        if any(
            len(row_material["physical_rows_by_table"][lane]) > full_snapshot_counts[lane]
            for lane in LANES
        ):
            raise ValueError()
        coverage_refs = _coverage_references(coverage_receipt_refs)
        adapted_rows = copy.deepcopy(row_material["adapted_rows_by_table"])
        counts = {lane: len(adapted_rows[lane]) for lane in LANES}
        snapshot = {
            "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
            "snapshot_id": _snapshot_id(
                cutoff_date=cutoff_date,
                captured_at=captured_at,
                client_scope_id=client_scope_id,
                markets=markets,
                plan=plan,
                material=row_material,
            ),
            "cutoff_date": cutoff_date.isoformat(),
            "source_as_of": plan.source_as_of.astimezone(UTC).isoformat(),
            "captured_at": captured_at.isoformat(),
            "client_scope_id": client_scope_id,
            "market_scope": list(markets),
            "source_relation_set_id": PROFILE_ID,
            "row_counts_by_table": counts,
            "section_counts": copy.deepcopy(counts),
            "section_identity_sets": _section_identities(adapted_rows),
            "table_digests": copy.deepcopy(row_material["adapted_table_digests"]),
            "rows_by_table": adapted_rows,
            "table_snapshot_receipts": _table_receipts(table_result, row_material),
            "physical_capture_complete": False,
            "coverage_receipt_refs": coverage_refs,
            "coverage_limitations": list(_STRUCTURAL_LIMITATIONS),
        }
        snapshot["source_digest"] = _digest(snapshot)
        assembly = {
            "assembly_contract_version": ASSEMBLY_CONTRACT_VERSION,
            "snapshot": snapshot,
            "source_metadata_utf8": metadata_text,
            "snapshot_plan": _serialize_plan(plan),
            "snapshot_metadata": _json_copy(snapshot_metadata),
            "readback_rows": _json_copy(readback_rows),
            "row_material": _json_copy(row_material),
            "collection_complete": False,
            "source_authority": False,
            "external_authority_required": list(_EXTERNAL_AUTHORITY_REQUIRED),
        }
        assembly["assembly_digest"] = _digest(
            {"domain": ASSEMBLY_CONTRACT_VERSION, "value": assembly}
        )
        _json_copy(assembly)
        return assembly
    except (UnicodeError, TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
        raise ValueError("production_snapshot_invalid") from error


def validate_production_snapshot(
    value,
    *,
    cutoff_date,
    captured_at,
    client_scope_id,
    market_scope,
    reviewed_metadata,
    plan,
    snapshot_metadata,
    readback_rows,
    rows_by_table,
    coverage_receipt_refs=(),
):
    try:
        _exact_fields(value, _ASSEMBLY_FIELDS)
        _exact_fields(value["snapshot"], _SNAPSHOT_FIELDS)
        expected = build_production_snapshot(
            cutoff_date=cutoff_date,
            captured_at=captured_at,
            client_scope_id=client_scope_id,
            market_scope=market_scope,
            reviewed_metadata=reviewed_metadata,
            plan=plan,
            snapshot_metadata=snapshot_metadata,
            readback_rows=readback_rows,
            rows_by_table=rows_by_table,
            coverage_receipt_refs=coverage_receipt_refs,
        )
        for lane in LANES:
            actual_rows = value["snapshot"]["rows_by_table"][lane]
            expected_rows = expected["snapshot"]["rows_by_table"][lane]
            if (
                type(actual_rows) is not list
                or len(actual_rows) != len(expected_rows)
                or any(
                    type(row) is not dict or set(row) != set(expected_row)
                    for row, expected_row in zip(actual_rows, expected_rows, strict=True)
                )
            ):
                raise ValueError()
        if value != expected:
            raise ValueError()
        return copy.deepcopy(expected)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
        raise ValueError("production_snapshot_invalid") from error


def validate_prepared_production_snapshot(
    value,
    *,
    cutoff_date,
    client_scope_id,
    market_scope,
):
    try:
        _exact_fields(value, _ASSEMBLY_FIELDS)
        _exact_fields(value["snapshot"], _SNAPSHOT_FIELDS)
        snapshot = value["snapshot"]
        if type(value["source_metadata_utf8"]) is not str:
            raise ValueError()
        plan = _deserialize_plan(value["snapshot_plan"])
        captured_at = _utc_timestamp(snapshot["captured_at"])
        return validate_production_snapshot(
            value,
            cutoff_date=cutoff_date,
            captured_at=captured_at,
            client_scope_id=client_scope_id,
            market_scope=market_scope,
            reviewed_metadata=value["source_metadata_utf8"].encode("utf-8"),
            plan=plan,
            snapshot_metadata=value["snapshot_metadata"],
            readback_rows=value["readback_rows"],
            rows_by_table=value["row_material"]["physical_rows_by_table"],
            coverage_receipt_refs=snapshot["coverage_receipt_refs"],
        )
    except (UnicodeError, TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
        raise ValueError("production_snapshot_invalid") from error


__all__ = [
    "ASSEMBLY_CONTRACT_VERSION",
    "LANES",
    "SNAPSHOT_CONTRACT_VERSION",
    "build_production_snapshot",
    "validate_prepared_production_snapshot",
    "validate_production_snapshot",
]
