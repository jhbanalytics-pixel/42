"""Normalize captured v3 source rows without issuing snapshot or release authority."""

import copy
import math
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta

from scripts.staging.replay_open_intelligence import _digest

from src.analysis.open_intelligence import pipeline
from src.analysis.open_intelligence.brain_contract import (
    canonical_bytes,
    canonical_digest,
)
from src.analysis.open_intelligence.production_snapshot_tables import (
    LANES,
    _reviewed_source_metadata,
)

ABSENT_NULLABLE_FIELDS = (
    "vendor_family",
    "channel_family",
    "source_family",
    "endpoint",
    "native_id",
    "geo_method_id",
    "geo_receipt_id",
    "source_family_map_version",
)
# Native id projection versions. The legacy projection is the retained v3 capture shape,
# which removes native_id with the other absent nullable fields. The bound projection
# carries native_id when the source schema declares the column, so an absent column, a
# SQL NULL and a populated id stay distinguishable. Retained captures never change
# projection; a new source profile binds one explicitly.
NATIVE_ID_PROJECTION_LEGACY = "v3_absent_nullable_v1"
NATIVE_ID_PROJECTION_BOUND = "native_id_bound_v1"
NATIVE_ID_PROJECTIONS = (NATIVE_ID_PROJECTION_LEGACY, NATIVE_ID_PROJECTION_BOUND)
_CANDIDATE_LANES = ("event_ledger", "seed_graph", "seed_candidates")
_EVIDENCE_LANES = ("enriched_content", "raw_content")
_ALIASES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}


def absent_nullable_fields(projection_version=NATIVE_ID_PROJECTION_LEGACY):
    if projection_version == NATIVE_ID_PROJECTION_LEGACY:
        return ABSENT_NULLABLE_FIELDS
    if projection_version == NATIVE_ID_PROJECTION_BOUND:
        return tuple(field for field in ABSENT_NULLABLE_FIELDS if field != "native_id")
    raise ValueError("snapshot_rows_invalid")


def physical_fields(lane, *, projection_version=NATIVE_ID_PROJECTION_LEGACY, schema_fields=None):
    """Physical columns of one lane; under the bound projection native_id follows the schema."""
    absent = set(absent_nullable_fields(projection_version))
    if (
        projection_version == NATIVE_ID_PROJECTION_BOUND
        and schema_fields is not None
        and "native_id" not in schema_fields
    ):
        absent.add("native_id")
    fields = {
        "event_ledger": pipeline.EVENT_COLUMNS,
        "seed_graph": pipeline.SEED_GRAPH_COLUMNS,
        "seed_candidates": pipeline.SEED_CANDIDATE_COLUMNS,
        "enriched_content": tuple(
            field
            for field in pipeline.EVIDENCE_COLUMNS_BY_TABLE["enriched_content"]
            if field not in absent
        ),
        "raw_content": tuple(
            field
            for field in pipeline.EVIDENCE_COLUMNS_BY_TABLE["raw_content"]
            if field not in absent
        ),
    }
    if lane not in fields:
        raise ValueError("snapshot_rows_invalid")
    return fields[lane]


def native_identity_state(row, *, projection_version=NATIVE_ID_PROJECTION_LEGACY):
    """Report what one evidence row establishes about its native identity.

    The native key is the (platform, native_id) pair the Wave 1 deduplication already keys
    on. Anything short of a populated id is uncertain: the legacy projection never carried
    the column, an absent column was never captured, a SQL NULL was captured empty.
    """
    if projection_version not in NATIVE_ID_PROJECTIONS or type(row) is not dict:
        raise ValueError("snapshot_rows_invalid")
    state, native_key = "not_projected", None
    if projection_version == NATIVE_ID_PROJECTION_BOUND:
        if "native_id" not in row:
            state = "absent_column"
        elif row["native_id"] is None:
            state = "sql_null"
        else:
            native_id, platform = row["native_id"], row.get("platform")
            if (
                type(native_id) is not str
                or not native_id.strip()
                or type(platform) is not str
                or not platform.strip()
            ):
                raise ValueError("snapshot_rows_invalid")
            state, native_key = "populated", [platform.lower(), native_id]
    return {
        "projection_version": projection_version,
        "state": state,
        "native_key": native_key,
        "uncertain": native_key is None,
    }


def native_identity_groups(rows, *, projection_version=NATIVE_ID_PROJECTION_LEGACY):
    """Collapse rows sharing a populated native key; every uncertain row stays its own group."""
    if type(rows) not in (list, tuple):
        raise ValueError("snapshot_rows_invalid")
    exact, uncertain = {}, []
    for row in rows:
        state = native_identity_state(row, projection_version=projection_version)
        market, row_id = row.get("market"), row.get("id")
        if type(market) is not str or type(row_id) is not str or not market or not row_id:
            raise ValueError("snapshot_rows_invalid")
        member = [market, row_id]
        if state["native_key"] is None:
            uncertain.append(member)
        else:
            exact.setdefault(tuple(state["native_key"]), []).append(member)
    return [
        {"native_key": list(key), "uncertain": False, "members": sorted(exact[key])}
        for key in sorted(exact)
    ] + [
        {"native_key": None, "uncertain": True, "members": [member]} for member in sorted(uncertain)
    ]


def _date(value):
    if isinstance(value, datetime):
        raise ValueError("snapshot_rows_invalid")
    if isinstance(value, date):
        return value.isoformat()
    if type(value) is not str:
        raise ValueError("snapshot_rows_invalid")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("snapshot_rows_invalid")
    return value


def _timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("snapshot_rows_invalid")
    if parsed.tzinfo is None:
        raise ValueError("snapshot_rows_invalid")
    return parsed.astimezone(UTC).isoformat()


def _value(value, schema):
    mode = schema["mode"]
    kind = _ALIASES.get(schema["type"], schema["type"])
    if mode == "REPEATED":
        if type(value) not in (list, tuple):
            raise ValueError("snapshot_rows_invalid")
        item_schema = {**schema, "mode": "REQUIRED"}
        return [_value(item, item_schema) for item in value]
    if value is None:
        if mode == "REQUIRED":
            raise ValueError("snapshot_rows_invalid")
        return None
    if kind == "DATE":
        return _date(value)
    if kind == "TIMESTAMP":
        return _timestamp(value)
    if kind == "INT64":
        if type(value) is not int or not -(2**63) <= value < 2**63:
            raise ValueError("snapshot_rows_invalid")
        return value
    if kind == "FLOAT64":
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("snapshot_rows_invalid")
        return float(value)
    if kind == "BOOL":
        if type(value) is not bool:
            raise ValueError("snapshot_rows_invalid")
        return value
    if kind == "STRING":
        if type(value) is not str:
            raise ValueError("snapshot_rows_invalid")
        return value
    raise ValueError("snapshot_rows_invalid")


def _normalize_lane(
    lane,
    rows,
    metadata,
    markets,
    cutoff,
    source_as_of,
    projection_version=NATIVE_ID_PROJECTION_LEGACY,
):
    if type(rows) not in (list, tuple):
        raise ValueError("snapshot_rows_invalid")
    if lane in pipeline.SOURCE_CEILINGS and len(rows) > pipeline.SOURCE_CEILINGS[lane]:
        raise ValueError("snapshot_rows_invalid")
    schema = {field["name"]: field for field in metadata[lane]["schema"]["fields"]}
    expected = physical_fields(
        lane, projection_version=projection_version, schema_fields=set(schema)
    )
    if any(field not in schema for field in expected):
        raise ValueError("snapshot_rows_invalid")
    output = []
    for raw in rows:
        if type(raw) is not dict or set(raw) != set(expected):
            raise ValueError("snapshot_rows_invalid")
        item = {field: _value(raw[field], schema[field]) for field in expected}
        if type(item.get("market")) is not str or item["market"] not in markets:
            raise ValueError("snapshot_rows_invalid")
        if lane in _CANDIDATE_LANES:
            field = "proposed_date" if lane == "seed_candidates" else "trend_date"
            if item[field] != cutoff.isoformat():
                raise ValueError("snapshot_rows_invalid")
            if (
                lane == "event_ledger"
                and item.get("as_of") is not None
                and datetime.fromisoformat(item["as_of"]) > source_as_of
            ):
                raise ValueError("snapshot_rows_invalid")
            if (
                lane == "seed_graph"
                and item.get("event_date") is not None
                and date.fromisoformat(item["event_date"]) > cutoff
            ):
                raise ValueError("snapshot_rows_invalid")
            if (
                lane == "seed_candidates"
                and item["status"] not in pipeline.ACCEPTED_SEED_CANDIDATE_STATUSES
            ):
                raise ValueError("snapshot_rows_invalid")
        else:
            collected = datetime.fromisoformat(item["collected_at"])
            published = (
                datetime.fromisoformat(item["published_at"])
                if item["published_at"] is not None
                else None
            )
            if (
                not cutoff - timedelta(days=6) <= collected.date() <= cutoff
                or collected > source_as_of
                or (published is not None and published > source_as_of)
                or any(
                    type(item.get(field)) is not str or not item[field]
                    for field in ("id", "source", "platform")
                )
            ):
                raise ValueError("snapshot_rows_invalid")
        output.append(item)
    return output


def _sort_rows(lane, rows):
    fields = {
        "event_ledger": ("market", "entity_key", "ledger_id"),
        "seed_graph": ("market", "term", "term_type", "platform"),
        "seed_candidates": (
            "market",
            "candidate_value",
            "candidate_type",
            "candidate_id",
        ),
        "enriched_content": ("market", "id", "collected_at", "source", "platform"),
        "raw_content": ("market", "id", "collected_at", "source", "platform"),
    }[lane]
    return sorted(
        rows,
        key=lambda row: (
            *((row[field] is not None, row[field]) for field in fields),
            canonical_bytes(row),
        ),
    )


def _candidate_identity(lane, row):
    fields = {
        "event_ledger": ("market", "ledger_id"),
        "seed_graph": ("market", "term", "term_type", "platform"),
        "seed_candidates": ("market", "candidate_id"),
    }[lane]
    identity = tuple(row[field] for field in fields)
    if any(type(value) is not str or not value for value in identity):
        raise ValueError("snapshot_rows_invalid")
    return identity


def _typed_evidence(rows):
    output = []
    counts = Counter((row["market"], row["id"]) for row in rows)
    for row in rows:
        item = copy.deepcopy(row)
        item["collected_at"] = datetime.fromisoformat(item["collected_at"])
        if item["published_at"] is not None:
            item["published_at"] = datetime.fromisoformat(item["published_at"])
        item["match_count"] = counts[(row["market"], row["id"])]
        output.append(item)
    return tuple(output)


def _adapt_evidence_row(lane, row, projection_version=NATIVE_ID_PROJECTION_LEGACY):
    logical = pipeline.EVIDENCE_COLUMNS_BY_TABLE[lane]
    absent = set(absent_nullable_fields(projection_version))
    missing = set(logical) - set(row)
    if missing != absent and not (
        projection_version == NATIVE_ID_PROJECTION_BOUND and missing == absent | {"native_id"}
    ):
        raise ValueError("snapshot_rows_invalid")
    return {field: row.get(field) for field in logical}


def normalize_snapshot_rows(
    cutoff_date,
    *,
    source_as_of,
    captured_at,
    market_scope,
    reviewed_metadata,
    rows_by_table,
    projection_version=NATIVE_ID_PROJECTION_LEGACY,
):
    try:
        if projection_version not in NATIVE_ID_PROJECTIONS:
            raise ValueError()
        if isinstance(cutoff_date, datetime) or type(cutoff_date) is not date:
            raise ValueError()
        expected_as_of = datetime.combine(cutoff_date + timedelta(days=1), time.min, UTC)
        if not isinstance(source_as_of, datetime) or source_as_of.tzinfo is None:
            raise ValueError()
        source_as_of = source_as_of.astimezone(UTC)
        if source_as_of != expected_as_of:
            raise ValueError()
        if not isinstance(captured_at, datetime) or captured_at.tzinfo is None:
            raise ValueError()
        captured_at = captured_at.astimezone(UTC)
        if captured_at < source_as_of:
            raise ValueError()
        if (
            type(market_scope) not in (list, tuple)
            or not market_scope
            or tuple(market_scope) != tuple(sorted(set(market_scope)))
            or any(market not in {"ke", "ng", "za"} for market in market_scope)
            or type(rows_by_table) is not dict
            or set(rows_by_table) != set(LANES)
        ):
            raise ValueError()
        metadata, metadata_digest = _reviewed_source_metadata(reviewed_metadata)
        normalized = {
            lane: _normalize_lane(
                lane,
                rows_by_table[lane],
                metadata,
                tuple(market_scope),
                cutoff_date,
                source_as_of,
                projection_version=projection_version,
            )
            for lane in LANES
        }
        for lane in _CANDIDATE_LANES:
            identities = [_candidate_identity(lane, row) for row in normalized[lane]]
            if len(identities) != len(set(identities)):
                raise ValueError()
        sample_keys = set()
        for lane in ("seed_graph", "seed_candidates"):
            for row in normalized[lane]:
                for row_id in pipeline._sample_ids(row, lane):
                    sample_keys.add((row["market"], row_id))
        if len(sample_keys) > pipeline.MAX_REQUESTED_SAMPLE_KEYS:
            raise ValueError()
        requested = tuple(sorted(sample_keys))
        requested_set = set(requested)
        for lane in _EVIDENCE_LANES:
            if any((row["market"], row["id"]) not in requested_set for row in normalized[lane]):
                raise ValueError()
        enriched_rows = _sort_rows("enriched_content", normalized["enriched_content"])
        if len(enriched_rows) > len(requested) + 1:
            raise ValueError()
        enriched_typed = _typed_evidence(
            [
                _adapt_evidence_row("enriched_content", row, projection_version)
                for row in enriched_rows
            ]
        )
        enriched, enriched_ambiguous = pipeline._unique_evidence_rows(
            enriched_typed,
            "enriched_content",
            skip_unsupported_identity=True,
        )
        enriched_present = {(row["market"], row["id"]) for row in enriched_rows}
        enriched_unsupported = {
            key for key in enriched_present if key not in enriched and key not in enriched_ambiguous
        }
        unresolved_for_raw = {
            key
            for key in requested
            if key not in enriched
            and key not in enriched_ambiguous
            and key not in enriched_unsupported
        }
        raw_rows = _sort_rows("raw_content", normalized["raw_content"])
        if len(raw_rows) > len(unresolved_for_raw) + 1 or any(
            (row["market"], row["id"]) not in unresolved_for_raw for row in raw_rows
        ):
            raise ValueError()
        raw_typed = _typed_evidence(
            [_adapt_evidence_row("raw_content", row, projection_version) for row in raw_rows]
        )
        raw, raw_ambiguous = pipeline._unique_evidence_rows(
            raw_typed,
            "raw_content",
            skip_unsupported_identity=True,
        )
        raw_present = {(row["market"], row["id"]) for row in raw_rows}
        raw_unsupported = {
            key for key in raw_present if key not in raw and key not in raw_ambiguous
        }
        ambiguous = set(enriched_ambiguous) | set(raw_ambiguous)
        unsupported = enriched_unsupported | raw_unsupported
        resolved = {**raw, **enriched}
        for key in ambiguous | unsupported:
            resolved.pop(key, None)
        missing = requested_set - set(resolved) - ambiguous - unsupported
        normalized["enriched_content"] = enriched_rows
        normalized["raw_content"] = raw_rows
        for lane in _CANDIDATE_LANES:
            normalized[lane] = _sort_rows(lane, normalized[lane])
        adapted = copy.deepcopy(normalized)
        for lane in _EVIDENCE_LANES:
            adapted[lane] = [
                _adapt_evidence_row(lane, row, projection_version) for row in normalized[lane]
            ]
        adapted_index = {}
        for lane in _EVIDENCE_LANES:
            counts = Counter((row["market"], row["id"]) for row in adapted[lane])
            adapted_index[lane] = {
                (row["market"], row["id"]): row
                for row in adapted[lane]
                if counts[(row["market"], row["id"])] == 1
            }
        resolved_refs = []
        for key, item in sorted(resolved.items()):
            source_table = item.source_table
            row = adapted_index[source_table].get(key)
            if row is None:
                raise ValueError()
            resolved_refs.append(
                {
                    "market": key[0],
                    "row_id": key[1],
                    "source_table": source_table,
                    "vendor_family": item.vendor_family,
                    "channel_family": item.channel_family,
                    "physical_row_digest": _digest(
                        {
                            field: row[field]
                            for field in physical_fields(
                                source_table,
                                projection_version=projection_version,
                                schema_fields=set(row),
                            )
                        }
                    ),
                    "adapted_row_digest": _digest(row),
                }
            )
        material = {
            "material_version": "v3_snapshot_rows_preparation_v1",
            "cutoff_date": cutoff_date.isoformat(),
            "source_as_of": source_as_of.isoformat(),
            "captured_at": captured_at.isoformat(),
            "market_scope": list(market_scope),
            "reviewed_metadata_sha256": metadata_digest,
            "physical_rows_by_table": normalized,
            "adapted_rows_by_table": adapted,
            "physical_table_digests": {lane: _digest(normalized[lane]) for lane in LANES},
            "adapted_table_digests": {lane: _digest(adapted[lane]) for lane in LANES},
            "requested_sample_keys": [list(key) for key in requested],
            "resolved_refs": resolved_refs,
            "missing_refs": [list(key) for key in sorted(missing)],
            "ambiguous_refs": [list(key) for key in sorted(ambiguous)],
            "unsupported_refs": [list(key) for key in sorted(unsupported)],
            "collection_complete": False,
            "limitations": [
                "streaming_buffer_exclusion_unproven",
                "upstream_collection_completeness_unproven",
            ],
        }
        if projection_version != NATIVE_ID_PROJECTION_LEGACY:
            # Source schema provenance keeps an absent column apart from a captured NULL.
            material["native_id_projection"] = {
                "version": projection_version,
                "source_columns": {
                    lane: "present"
                    if any(
                        field["name"] == "native_id" for field in metadata[lane]["schema"]["fields"]
                    )
                    else "absent"
                    for lane in _EVIDENCE_LANES
                },
            }
        material["material_digest"] = canonical_digest(material)
        return material
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
        raise ValueError("snapshot_rows_invalid") from error


__all__ = [
    "ABSENT_NULLABLE_FIELDS",
    "NATIVE_ID_PROJECTIONS",
    "NATIVE_ID_PROJECTION_BOUND",
    "NATIVE_ID_PROJECTION_LEGACY",
    "absent_nullable_fields",
    "native_identity_groups",
    "native_identity_state",
    "normalize_snapshot_rows",
    "physical_fields",
]
