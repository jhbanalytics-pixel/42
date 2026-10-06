"""Pure, staging-only persistence planning for open intelligence rows."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Protocol, cast

from google.api_core.retry import Retry
from google.auth.compute_engine.credentials import Credentials as ComputeCredentials
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as AuthRequest
from google.cloud import bigquery

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.run_receipts import (
    RUN_RECEIPT_CONTRACT_VERSION,
    RUN_RECEIPT_ROW_FIELDS,
    OpenIntelligenceRunReceipt,
    build_run_receipt,
)

TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging"
TARGET_LOCATION = "US"
TARGET_WRITER_IDENTITY = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"

TABLE_BINDINGS = MappingProxyType(
    {
        "candidates": "signal_candidates_v2",
        "evidence": "signal_evidence_v2",
        "membership": "signal_membership_v2",
        "lineage": "signal_lineage_v2",
        "predictions": "signal_predictions_v2",
        "outcomes": "signal_outcomes_v2",
    }
)

ROW_FIELDS = MappingProxyType(
    {
        "candidates": (
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
            "run_id",
            "contract_version",
            "signal_id",
            "signal_date",
            "market",
            "label",
            "label_member_identity",
            "cluster_signature",
            "cluster_build_version",
            "model_version",
            "discovery_mode",
            "topic_tags",
            "novelty_score",
            "velocity_score",
            "breadth_score",
            "independence_score",
            "historical_similarity",
            "geo_confidence",
            "evidence_state",
            "created_at",
        ),
        "evidence": (
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
            "run_id",
            "contract_version",
            "signal_date",
            "market",
            "signal_id",
            "evidence_id",
            "row_id",
            "source_family",
            "vendor_family",
            "channel_family",
            "platform",
            "url",
            "published_at",
            "claim_role",
            "direction",
            "geo_confidence",
            "source_label",
            "author_label",
            "excerpt",
            "metric_label",
            "availability",
            "evidence_state",
            "created_at",
        ),
        "membership": (
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
            "run_id",
            "contract_version",
            "signal_date",
            "market",
            "signal_id",
            "member_id",
            "member_identity",
            "candidate_type",
            "canonical_value",
            "source_families",
            "vendor_families",
            "channel_families",
            "platforms",
            "row_id",
            "qualifies_evidence",
            "created_at",
        ),
        "lineage": (
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
            "run_id",
            "contract_version",
            "signal_date",
            "market",
            "from_signal_id",
            "to_signal_id",
            "relation",
            "overlap_score",
            "created_at",
        ),
        "predictions": (
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
            "run_id",
            "contract_version",
            "prediction_id",
            "signal_id",
            "signal_date",
            "market",
            "discovery_mode",
            "source_families",
            "evidence_state",
            "first_seen_at",
            "predicted_at",
            "expected_trajectory",
            "evaluation_date",
            "baseline",
            "promotion_target",
            "invalidation_condition",
            "cluster_build_version",
            "source_family_map_version",
            "rule_version",
            "display_eligible",
        ),
        "outcomes": (
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
            "run_id",
            "contract_version",
            "outcome_id",
            "prediction_id",
            "signal_id",
            "signal_date",
            "market",
            "discovery_mode",
            "source_families",
            "source_family_map_version",
            "evaluation_date",
            "evaluated_at",
            "outcome",
            "observed_velocity",
            "observed_breadth",
            "observed_evidence_family_count",
            "human_calibration_label",
            "human_reviewed_at",
            "resolution_reason",
            "rule_version",
        ),
    }
)

NATURAL_KEYS = MappingProxyType(
    {
        "candidates": ("client_scope_id", "signal_date", "market", "signal_id", "run_id"),
        "evidence": (
            "client_scope_id",
            "signal_date",
            "market",
            "signal_id",
            "evidence_id",
            "run_id",
        ),
        "membership": (
            "client_scope_id",
            "signal_date",
            "market",
            "signal_id",
            "member_id",
            "run_id",
        ),
        "lineage": (
            "client_scope_id",
            "signal_date",
            "market",
            "from_signal_id",
            "to_signal_id",
            "relation",
            "run_id",
        ),
        "predictions": ("prediction_id",),
        "outcomes": ("prediction_id", "evaluation_date", "run_id"),
    }
)

_REPEATED_FIELDS = frozenset(
    {
        "market_scope",
        "audience_lens_ids",
        "topic_tags",
        "source_families",
        "vendor_families",
        "channel_families",
        "platforms",
    }
)
_DATE_FIELDS = frozenset({"signal_date", "evaluation_date"})
_TIMESTAMP_FIELDS = frozenset(
    {
        "created_at",
        "published_at",
        "first_seen_at",
        "predicted_at",
        "evaluated_at",
        "human_reviewed_at",
    }
)
_FLOAT_FIELDS = frozenset(
    {
        "novelty_score",
        "velocity_score",
        "breadth_score",
        "independence_score",
        "historical_similarity",
        "geo_confidence",
        "overlap_score",
        "observed_velocity",
        "observed_breadth",
    }
)
_INTEGER_FIELDS = frozenset({"observed_evidence_family_count"})
_BOOLEAN_FIELDS = frozenset({"qualifies_evidence", "display_eligible"})
_OPTIONAL_FIELDS = frozenset(
    {
        "model_version",
        "historical_similarity",
        "url",
        "published_at",
        "source_label",
        "author_label",
        "excerpt",
        "metric_label",
        "observed_velocity",
        "observed_breadth",
        "observed_evidence_family_count",
        "human_calibration_label",
        "human_reviewed_at",
    }
)
_STRUCT_FIELDS = frozenset({"baseline", "promotion_target"})
_STRUCT_FIELDS_ORDER = ("velocity", "breadth", "evidence_family_count")


class PersistenceError(Exception):
    """Base error for the local persistence boundary."""

    def __init__(
        self,
        message: str,
        *,
        result: PersistenceResult | None = None,
        failed_table: str | None = None,
        cleanup_errors: tuple[CleanupFailure, ...] = (),
    ) -> None:
        super().__init__(message)
        self.result = result
        self.failed_table = failed_table
        self.cleanup_errors = cleanup_errors


class TargetInvalid(PersistenceError):
    """The requested target is not the approved staging target."""


class BatchInvalid(PersistenceError):
    """The immutable row batch does not match the approved schemas."""


class RuleInvalid(PersistenceError):
    """The rule bundle is not replay certified for persistence."""


class CandidateRunConflict(PersistenceError):
    """Reserved for a different candidate payload at an existing natural key."""


class ImmutableConflict(PersistenceError):
    """Reserved for different immutable content at an existing natural key."""


class ConcurrentWriteConflict(PersistenceError):
    """Reserved for a BigQuery serialization or concurrency conflict."""


class PersistenceBackendUnavailable(PersistenceError):
    """The real writer is deliberately unavailable in Task 5B2A."""


class CleanupFailure(PersistenceError):
    """Reserved for a staging-table cleanup failure in Task 5B2B."""


class DailyPersistenceCleanupFailure(CleanupFailure):
    """The daily transaction committed, but its disposable staging cleanup failed."""

    def __init__(
        self,
        message: str,
        *,
        committed_receipt: OpenIntelligenceRunReceipt,
        result: PersistenceResult,
        cleanup_errors: tuple[CleanupFailure, ...],
    ) -> None:
        super().__init__(message, result=result, cleanup_errors=cleanup_errors)
        self.committed_receipt = committed_receipt


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise BatchInvalid(f"{field} must be a nonempty string")
    if unicodedata.normalize("NFC", value) != value:
        raise BatchInvalid(f"noncanonical_unicode:{field}")
    return value


def _float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BatchInvalid(f"{field} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise BatchInvalid(f"{field} must be a finite number")
    return normalized


def _date(value: object, field: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise BatchInvalid(f"{field} must be a date")
    return value


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BatchInvalid(f"{field} must be a timezone-aware timestamp")
    return value.astimezone(UTC)


def _repeated(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise BatchInvalid(f"{field} must be a list or tuple")
    values = tuple(_text(item, field) for item in value)
    if len(set(values)) != len(values):
        raise BatchInvalid(f"{field} contains duplicate values")
    return tuple(sorted(values))


def _struct(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or tuple(value) != _STRUCT_FIELDS_ORDER:
        raise BatchInvalid(f"{field} fields are invalid")
    velocity = _float(value["velocity"], f"{field}.velocity")
    breadth = _float(value["breadth"], f"{field}.breadth")
    count = value["evidence_family_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise BatchInvalid(f"{field}.evidence_family_count must be a nonnegative integer")
    return MappingProxyType(
        {"velocity": velocity, "breadth": breadth, "evidence_family_count": count}
    )


def _canonicalize_value(field: str, value: object) -> object:
    if value is None:
        if field not in _OPTIONAL_FIELDS:
            raise BatchInvalid(f"{field} cannot be null")
        return None
    if field in _REPEATED_FIELDS:
        return _repeated(value, field)
    if field in _DATE_FIELDS:
        return _date(value, field)
    if field in _TIMESTAMP_FIELDS:
        return _timestamp(value, field)
    if field in _FLOAT_FIELDS:
        return _float(value, field)
    if field in _INTEGER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BatchInvalid(f"{field} must be a nonnegative integer")
        return value
    if field in _BOOLEAN_FIELDS:
        if type(value) is not bool:
            raise BatchInvalid(f"{field} must be boolean")
        return value
    if field in _STRUCT_FIELDS:
        return _struct(value, field)
    return _text(value, field)


def _versioned_fields(table: str, cluster_build_version: str | None = None) -> tuple[str, ...]:
    if cluster_build_version not in (None, "hybrid_graph_v1", "hybrid_graph_v2", "hybrid_graph_v3"):
        raise BatchInvalid("source_provenance_version_invalid")
    if table not in TABLE_BINDINGS:
        raise BatchInvalid("table is not approved")
    return ROW_FIELDS[table] + (
        ("source_provenance_json",)
        if table == "membership" and cluster_build_version == "hybrid_graph_v3"
        else ()
    )


def _provenance_id(row: Mapping[str, object], envelope: Mapping[str, object]) -> str:
    from src.analysis.open_intelligence.pipeline import _digest_identifier, _nested_identifier
    from src.analysis.open_intelligence.source_provenance import provenance_member_id

    legacy_id = _digest_identifier(
        "mem_",
        row["client_scope_id"],
        _date(row["signal_date"], "signal_date").isoformat(),
        row["market"],
        row["candidate_type"],
        row["canonical_value"],
        _nested_identifier(row["source_families"]),
        _nested_identifier(row["platforms"]),
        row["row_id"],
    )
    return provenance_member_id(legacy_id, envelope)


def _validate_membership_provenance(row: Mapping[str, object]) -> None:
    from src.analysis.open_intelligence.source_provenance import encode_source_provenance

    if (
        row["member_identity"]
        != f"{row['market']}|{row['candidate_type']}|{row['canonical_value']}"
    ):
        raise BatchInvalid("source_provenance_conflict")
    raw = row.get("source_provenance_json")
    if not isinstance(raw, str):
        raise BatchInvalid("source_provenance_missing")
    try:
        envelope = json.loads(raw)
        if encode_source_provenance(envelope) != raw:
            raise ValueError("source_provenance_conflict")
    except (ValueError, TypeError) as error:
        raise BatchInvalid(str(error)) from error
    pairs = envelope["pairs"]
    for field, key in (
        ("vendor_families", "vendor_family"),
        ("channel_families", "channel_family"),
        ("source_families", "channel_family"),
    ):
        if tuple(row[field]) != tuple(sorted({pair[key] for pair in pairs})):
            raise BatchInvalid("source_provenance_conflict")
    if row["member_id"] != _provenance_id(row, envelope):
        raise BatchInvalid("source_provenance_conflict")


def _canonicalize_row(
    table: str, row: object, *, cluster_build_version: str | None = None
) -> Mapping[str, object]:
    if table not in TABLE_BINDINGS:
        raise BatchInvalid("table is not approved")
    fields = _versioned_fields(table, cluster_build_version)
    if (
        isinstance(row, Mapping)
        and table == "membership"
        and cluster_build_version != "hybrid_graph_v3"
        and "source_provenance_json" in row
    ):
        if row["source_provenance_json"] is not None:
            raise BatchInvalid("source_provenance_version_invalid")
        row = {key: value for key, value in row.items() if key != "source_provenance_json"}
    if not isinstance(row, Mapping) or set(row) != set(fields):
        raise BatchInvalid(f"{table} fields are invalid")
    if table == "membership" and cluster_build_version == "hybrid_graph_v3":
        _validate_membership_provenance(row)
    return MappingProxyType(
        {
            field: row[field]
            if field == "source_provenance_json"
            else _canonicalize_value(field, row[field])
            for field in fields
        }
    )


def _natural_key(table: str, row: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(row[field] for field in NATURAL_KEYS[table])


def _typed_value(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if type(value) is bool:
        return {"type": "boolean", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, datetime):
        return {
            "type": "timestamp",
            "value": value.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, tuple):
        return {"type": "repeated", "value": [_typed_value(item) for item in value]}
    if isinstance(value, Mapping):
        return {
            "type": "struct",
            "value": {key: _typed_value(item) for key, item in value.items()},
        }
    raise BatchInvalid("canonical value is unsupported")


def canonical_typed_json(
    table: str,
    row: object,
    *,
    exclude: tuple[str, ...] = (),
    cluster_build_version: str | None = None,
) -> str:
    """Return the approved compact typed JSON for a validated row.

    ``exclude`` drops named fields from the rendering only; the row must still
    carry every approved field. The content projection uses it to leave run
    identity and the persistence clock out of the digest.
    """
    canonical_row = _canonicalize_row(table, row, cluster_build_version=cluster_build_version)
    return json.dumps(
        {
            field: _typed_value(canonical_row[field])
            for field in _versioned_fields(table, cluster_build_version)
            if field not in exclude
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _typed_json_sql(field: str, expression: str) -> str:
    if field in _REPEATED_FIELDS:
        value = (
            "CONCAT('[', ARRAY_TO_STRING(ARRAY(SELECT "
            f'CONCAT(\'{{"type":"string","value":\', TO_JSON_STRING(item), \'}}\') '
            f"FROM UNNEST({expression}) AS item ORDER BY item), ','), ']')"
        )
        return f'CONCAT(\'{{"type":"repeated","value":\', {value}, \'}}\')'
    if field in _STRUCT_FIELDS:
        children = []
        for child in _STRUCT_FIELDS_ORDER:
            child_type = "integer" if child == "evidence_family_count" else "float"
            children.append(
                f'\'"{child}":{{"type":"{child_type}","value":\', '
                f"TO_JSON_STRING({expression}.{child}), '}}'"
            )
        value = "CONCAT('{', " + ", ',', ".join(children) + ", '}')"
        return f'CONCAT(\'{{"type":"struct","value":\', {value}, \'}}\')'
    if field in _DATE_FIELDS:
        value = f"TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', {expression}))"
        kind = "date"
    elif field in _TIMESTAMP_FIELDS:
        value = (
            "TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', "
            f"{expression}, 'UTC'), 'Z'))"
        )
        kind = "timestamp"
    elif field in _FLOAT_FIELDS:
        value = f"TO_JSON_STRING({expression})"
        kind = "float"
    elif field in _INTEGER_FIELDS:
        value = f"TO_JSON_STRING({expression})"
        kind = "integer"
    elif field in _BOOLEAN_FIELDS:
        value = f"TO_JSON_STRING({expression})"
        kind = "boolean"
    else:
        value = f"TO_JSON_STRING({expression})"
        kind = "string"
    rendered = f'CONCAT(\'{{"type":"{kind}","value":\', {value}, \'}}\')'
    if field in _OPTIONAL_FIELDS:
        return f'IF({expression} IS NULL, \'{{"type":"null","value":null}}\', {rendered})'
    return rendered


def canonical_typed_json_sql(
    table: str,
    alias: str,
    *,
    exclude: tuple[str, ...] = (),
    cluster_build_version: str | None = None,
) -> str:
    """BigQuery expression mirroring canonical_typed_json without normalization."""
    if table not in TABLE_BINDINGS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None:
        raise BatchInvalid("canonical SQL target is invalid")
    arguments = ["'{'"]
    fields = tuple(
        field for field in _versioned_fields(table, cluster_build_version) if field not in exclude
    )
    for index, field in enumerate(fields):
        if index:
            arguments.append("','")
        arguments.extend(
            (
                f"'\"{field}\":'",
                _typed_json_sql(field, f"{alias}.{field}"),
            )
        )
    arguments.append("'}'")
    return "CONCAT(" + ", ".join(arguments) + ")"


CONTENT_PROJECTION_CONTRACT_VERSION = "dynamic_quality_projection_content_v1"
# The content projection leaves out what changes between two persistences of
# the same rows (the run identity and the persistence clock) and what Python
# and BigQuery render differently (floats: BigQuery prints 0 for 0.0 and 17
# significant digits, Python the shortest round trip; timestamps: BigQuery
# trims a fraction's trailing zeros). Scores are derived from the rows that
# stay in, and the review packet content digest carries every evidence
# timestamp in the SQL rendering, so nothing reviewable is lost.
CONTENT_PROJECTION_EXCLUDED_FIELDS = tuple(
    sorted({"run_id", "created_at", *_FLOAT_FIELDS, *_TIMESTAMP_FIELDS, *_STRUCT_FIELDS})
)


def _completed_provenance_runs_sql(*, project: str, dataset: str) -> str:
    """Admit whole completed runs before exposing canonical membership bytes."""
    _validate_target(project, dataset)
    return f"""WITH provenance_receipts AS (
  SELECT run_id, ANY_VALUE(client_scope_id) AS client_scope_id,
    ANY_VALUE(signal_date) AS signal_date, ANY_VALUE(cluster_build_version) AS cluster_build_version,
    ANY_VALUE(candidate_count) AS candidate_count, ANY_VALUE(membership_count) AS membership_count
  FROM `{project}.{dataset}.open_intelligence_run_receipts_v1` AS source_receipt
  WHERE client_scope_id != 'qa_canary'
  GROUP BY run_id
  HAVING COUNT(*) = 1 AND COUNTIF(status = 'completed' AND complete_partitions
    AND source_receipt.cluster_build_version IN ('hybrid_graph_v1', 'hybrid_graph_v2', 'hybrid_graph_v3')) = 1
), provenance_candidates AS (
  SELECT client_scope_id, run_id, signal_date, market, signal_id,
    COUNT(*) AS row_count, ANY_VALUE(cluster_build_version) AS cluster_build_version
  FROM `{project}.{dataset}.signal_candidates_v2`
  WHERE client_scope_id != 'qa_canary'
  GROUP BY client_scope_id, run_id, signal_date, market, signal_id
), provenance_candidate_checks AS (
  SELECT c.run_id, SUM(c.row_count) AS candidate_count,
    COUNTIF(c.row_count != 1 OR NOT IFNULL(c.cluster_build_version = r.cluster_build_version, FALSE)
      OR NOT IFNULL(c.client_scope_id = r.client_scope_id, FALSE)
      OR NOT IFNULL(c.signal_date = r.signal_date, FALSE)) AS invalid_count
  FROM provenance_candidates AS c JOIN provenance_receipts AS r ON r.run_id = c.run_id
  GROUP BY c.run_id
), provenance_membership_checks AS (
  SELECT m.run_id, COUNT(*) AS membership_count,
    COUNTIF(NOT IFNULL(c.row_count = 1, FALSE)
      OR NOT IFNULL(c.cluster_build_version = r.cluster_build_version, FALSE)
      OR NOT IFNULL(m.client_scope_id = r.client_scope_id, FALSE)
      OR NOT IFNULL(m.signal_date = r.signal_date, FALSE)
      OR (r.cluster_build_version = 'hybrid_graph_v3' AND m.source_provenance_json IS NULL)
      OR (r.cluster_build_version != 'hybrid_graph_v3' AND m.source_provenance_json IS NOT NULL)) AS invalid_count
  FROM `{project}.{dataset}.signal_membership_v2` AS m
  JOIN provenance_receipts AS r ON r.run_id = m.run_id
  LEFT JOIN provenance_candidates AS c ON c.client_scope_id = m.client_scope_id AND c.run_id = m.run_id
    AND c.signal_date = m.signal_date AND c.market = m.market AND c.signal_id = m.signal_id
  WHERE m.client_scope_id != 'qa_canary'
  GROUP BY m.run_id
)
SELECT r.run_id, r.cluster_build_version
FROM provenance_receipts AS r
LEFT JOIN provenance_candidate_checks AS c ON c.run_id = r.run_id
LEFT JOIN provenance_membership_checks AS m ON m.run_id = r.run_id
WHERE IFNULL(c.candidate_count, 0) = r.candidate_count AND IFNULL(c.invalid_count, 0) = 0
  AND IFNULL(m.membership_count, 0) = r.membership_count AND IFNULL(m.invalid_count, 0) = 0"""


def _membership_json_sql(
    alias: str,
    *,
    project: str,
    dataset: str,
    exclude: tuple[str, ...] = (),
    cluster_build_version: str | None = None,
    from_receipt: bool = False,
) -> str:
    """Select bytes only from an exact candidate and run version join."""
    legacy = canonical_typed_json_sql("membership", alias, exclude=exclude)
    v3 = canonical_typed_json_sql(
        "membership", alias, exclude=exclude, cluster_build_version="hybrid_graph_v3"
    )
    if from_receipt:
        return (
            f"CASE WHEN {alias}_authority.cluster_build_version IN ('hybrid_graph_v1', 'hybrid_graph_v2') "
            f"THEN {legacy} WHEN {alias}_authority.cluster_build_version = 'hybrid_graph_v3' "
            f"THEN {v3} ELSE NULL END"
        )
    keys = ("client_scope_id", "run_id", "signal_date", "market", "signal_id")
    candidate_where = " AND ".join(f"version_candidate.{key} = {alias}.{key}" for key in keys)
    candidate_query = f"FROM `{project}.{dataset}.signal_candidates_v2` AS version_candidate WHERE {candidate_where}"
    candidate_version = (
        f"(SELECT ANY_VALUE(version_candidate.cluster_build_version) {candidate_query})"
    )
    candidate_count = f"(SELECT COUNT(*) {candidate_query})"
    if cluster_build_version is not None:
        _versioned_fields("membership", cluster_build_version)
        version = f"'{cluster_build_version}'"
        receipt_guard = ""
    else:
        version = candidate_version
        receipt_guard = f"{version} IN ('hybrid_graph_v1', 'hybrid_graph_v2') AND "
    return (
        f"CASE WHEN {receipt_guard}{candidate_count} = 1 AND {candidate_version} = {version} "
        f"THEN CASE WHEN {version} IN ('hybrid_graph_v1', 'hybrid_graph_v2') "
        f"AND {alias}.source_provenance_json IS NULL THEN {legacy} "
        f"WHEN {version} = 'hybrid_graph_v3' AND {alias}.source_provenance_json IS NOT NULL THEN {v3} "
        "ELSE ERROR('source_provenance_version_invalid') END "
        "ELSE ERROR('source_provenance_conflict') END"
    )


def candidate_projection_content_digest_sql(
    run_id: str,
    *,
    project: str = TARGET_PROJECT,
    dataset: str = TARGET_DATASET,
    cluster_build_version: str | None = None,
) -> str:
    """Scalar BigQuery expression for the content projection of one run's rows.

    Approved 3 Sep 2026 (content-bound quality review): the same rows persisted
    under another run identity produce the same content digest.
    """
    if not isinstance(run_id, str) or re.fullmatch(r"[a-z0-9_][a-z0-9_-]{0,127}", run_id) is None:
        raise BatchInvalid("projection run id is invalid")
    families = ("candidates", "evidence", "membership")
    ctes = []
    arrays = {}
    for table in families:
        alias = table[:-1] if table.endswith("s") else table
        typed = canonical_typed_json_sql(table, alias, exclude=CONTENT_PROJECTION_EXCLUDED_FIELDS)
        if table == "membership":
            typed = _membership_json_sql(
                alias,
                project=project,
                dataset=dataset,
                exclude=CONTENT_PROJECTION_EXCLUDED_FIELDS,
                cluster_build_version=cluster_build_version,
            )
        keys = ", ".join(f"{alias}.{field}" for field in NATURAL_KEYS[table] if field != "run_id")
        projected_keys = ", ".join(field for field in NATURAL_KEYS[table] if field != "run_id")
        name = f"content_projection_{table}"
        ctes.append(
            f"{name} AS (SELECT {typed} AS row_json, {keys} "
            f"FROM `{project}.{dataset}.{TABLE_BINDINGS[table]}` AS {alias} "
            f"WHERE {alias}.run_id = '{run_id}' "
            f"AND {alias}.client_scope_id != 'qa_canary')"
        )
        arrays[table] = (
            "IFNULL((SELECT TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY "
            f"{projected_keys}, row_json)) FROM {name}), '[]')"
        )
    preimage = (
        "CONCAT('{\"candidates\":', "
        + arrays["candidates"]
        + ", ',\"evidence\":', "
        + arrays["evidence"]
        + ", ',\"membership\":', "
        + arrays["membership"]
        + f', \',"projection_contract_version":"{CONTENT_PROJECTION_CONTRACT_VERSION}"}}\')'
    )
    return "(WITH " + ", ".join(ctes) + f" SELECT LOWER(TO_HEX(SHA256({preimage}))))"


def candidate_projection_digest_sql(
    run_id: str,
    *,
    sql_expression: bool = False,
    project: str = TARGET_PROJECT,
    dataset: str = TARGET_DATASET,
    cluster_build_version: str | None = None,
) -> str:
    """Scalar BigQuery expression for dynamic_quality_projection_v2."""
    if not isinstance(run_id, str):
        raise BatchInvalid("projection run id is invalid")
    if sql_expression:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", run_id) is None:
            raise BatchInvalid("projection run SQL expression is invalid")
        run_filter = run_id
        run_json = f"TO_JSON_STRING(CAST({run_id} AS STRING))"
    else:
        if re.fullmatch(r"[a-z0-9_][a-z0-9_-]{0,127}", run_id) is None:
            raise BatchInvalid("projection run id is invalid")
        run_filter = f"'{run_id}'"
        run_json = f"TO_JSON_STRING('{run_id}')"
    families = ("candidates", "evidence", "membership")
    ctes = []
    arrays = {}
    for table in families:
        alias = table[:-1] if table.endswith("s") else table
        typed = canonical_typed_json_sql(table, alias)
        if table == "membership":
            typed = _membership_json_sql(
                alias, project=project, dataset=dataset, cluster_build_version=cluster_build_version
            )
        keys = ", ".join(f"{alias}.{field}" for field in NATURAL_KEYS[table])
        # The CTE projects the key columns by bare name; the aggregate over it cannot see
        # the table alias, so it orders by the projected names.
        projected_keys = ", ".join(NATURAL_KEYS[table])
        name = f"projection_{table}"
        ctes.append(
            f"{name} AS (SELECT {typed} AS row_json, {keys} "
            f"FROM `{project}.{dataset}.{TABLE_BINDINGS[table]}` AS {alias} "
            f"WHERE {alias}.run_id = {run_filter} "
            f"AND {alias}.client_scope_id != 'qa_canary')"
        )
        arrays[table] = (
            "IFNULL((SELECT TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY "
            f"{projected_keys}, row_json)) FROM {name}), '[]')"
        )
    preimage = (
        "CONCAT('{\"candidates\":', "
        + arrays["candidates"]
        + ", ',\"evidence\":', "
        + arrays["evidence"]
        + ", ',\"membership\":', "
        + arrays["membership"]
        + ', \',"projection_contract_version":"dynamic_quality_projection_v2"\', '
        + f"',\"run_id\":', {run_json}, '}}')"
    )
    return "(WITH " + ", ".join(ctes) + f" SELECT LOWER(TO_HEX(SHA256({preimage}))))"


def _analysis_canonical_json_sql(alias: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None:
        raise BatchInvalid("analysis canonical SQL alias is invalid")
    values = {
        "analysis_id": f"TO_JSON_STRING({alias}.analysis_id)",
        "analyzed_at": (
            f"TO_JSON_STRING(CONCAT(FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6S', "
            f"{alias}.analyzed_at, 'UTC'), 'Z'))"
        ),
        "audience_lens_ids": f"TO_JSON_STRING({alias}.audience_lens_ids)",
        "brand_config_id": f"TO_JSON_STRING({alias}.brand_config_id)",
        "client_scope_id": f"TO_JSON_STRING({alias}.client_scope_id)",
        "contract_version": f"TO_JSON_STRING({alias}.contract_version)",
        "contradictions": f"TO_JSON_STRING({alias}.contradictions)",
        "evidence_ids": f"TO_JSON_STRING({alias}.evidence_ids)",
        "evidence_state": f"TO_JSON_STRING({alias}.evidence_state)",
        "human_review_required": f"TO_JSON_STRING({alias}.human_review_required)",
        "limitations": f"TO_JSON_STRING({alias}.limitations)",
        "market": f"TO_JSON_STRING({alias}.market)",
        "market_scope": f"TO_JSON_STRING({alias}.market_scope)",
        "model_version": (
            f"IF({alias}.model_version IS NULL, 'null', TO_JSON_STRING({alias}.model_version))"
        ),
        "possible_response": (
            f"IF({alias}.possible_response IS NULL, 'null', "
            f"TO_JSON_STRING({alias}.possible_response))"
        ),
        "run_id": f"TO_JSON_STRING({alias}.run_id)",
        "signal_date": f"TO_JSON_STRING(FORMAT_DATE('%Y-%m-%d', {alias}.signal_date))",
        "signal_id": f"TO_JSON_STRING({alias}.signal_id)",
        "summary": f"TO_JSON_STRING({alias}.summary)",
        "theme_id": f"TO_JSON_STRING({alias}.theme_id)",
        "why_now": f"TO_JSON_STRING({alias}.why_now)",
    }
    arguments = ["'{'"]
    for index, field in enumerate(sorted(values)):
        if index:
            arguments.append("','")
        arguments.extend((f"'\"{field}\":'", values[field]))
    arguments.append("'}'")
    return "CONCAT(" + ", ".join(arguments) + ")"


def row_set_digest_sql(
    run_id: str,
    *,
    sql_expression: bool = False,
    project: str = TARGET_PROJECT,
    dataset: str = TARGET_DATASET,
    cluster_build_version: str | None = None,
) -> str:
    if sql_expression:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", run_id) is None:
            raise BatchInvalid("row-set run SQL expression is invalid")
        run_filter = run_id
    else:
        if re.fullmatch(r"[a-z0-9_][a-z0-9_-]{0,127}", run_id) is None:
            raise BatchInvalid("row-set run id is invalid")
        run_filter = f"'{run_id}'"
    ctes = []
    arrays = {}
    for table, table_name in TABLE_BINDINGS.items():
        alias = f"row_set_{table}_row"
        row_json = canonical_typed_json_sql(table, alias)
        if table == "membership":
            row_json = _membership_json_sql(
                alias, project=project, dataset=dataset, cluster_build_version=cluster_build_version
            )
        keys = ", ".join(f"{alias}.{field}" for field in NATURAL_KEYS[table])
        projected_keys = ", ".join(NATURAL_KEYS[table])
        cte = f"row_set_{table}"
        ctes.append(
            f"{cte} AS (SELECT {row_json} AS row_json, {keys} "
            f"FROM `{project}.{dataset}.{table_name}` AS {alias} "
            f"WHERE {alias}.run_id = {run_filter} "
            f"AND {alias}.client_scope_id != 'qa_canary')"
        )
        arrays[table] = (
            "IFNULL((SELECT TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY "
            f"{projected_keys}, row_json)) FROM {cte}), '[]')"
        )
    analysis_alias = "row_set_analysis_row"
    analysis_json = _analysis_canonical_json_sql(analysis_alias)
    ctes.append(
        f"row_set_analysis AS (SELECT {analysis_json} AS row_json "
        f"FROM `{project}.{dataset}.signal_analysis_v2` AS {analysis_alias} "
        f"WHERE {analysis_alias}.run_id = {run_filter} "
        f"AND {analysis_alias}.client_scope_id != 'qa_canary')"
    )
    arrays["analysis"] = (
        "IFNULL((SELECT TO_JSON_STRING(ARRAY_AGG(row_json ORDER BY row_json)) "
        "FROM row_set_analysis), '[]')"
    )
    ordered_families = tuple(sorted(("analysis", *TABLE_BINDINGS)))
    pieces = []
    for index, family in enumerate(ordered_families):
        prefix = "{" if index == 0 else ","
        pieces.extend((f"'{prefix}\"{family}\":'", arrays[family]))
    pieces.append("'}'")
    preimage = "CONCAT(" + ", ".join(pieces) + ")"
    return "(WITH " + ", ".join(ctes) + f" SELECT LOWER(TO_HEX(SHA256({preimage}))))"


@dataclass(frozen=True, slots=True)
class OpenIntelligenceRowBatch:
    candidates: tuple[Mapping[str, object], ...]
    evidence: tuple[Mapping[str, object], ...]
    membership: tuple[Mapping[str, object], ...]
    lineage: tuple[Mapping[str, object], ...]
    predictions: tuple[Mapping[str, object], ...]
    outcomes: tuple[Mapping[str, object], ...]
    cluster_build_version: str | None = None

    def __post_init__(self) -> None:
        _versioned_fields("membership", self.cluster_build_version)
        if self.cluster_build_version is not None:
            if any(
                row.get("cluster_build_version") != self.cluster_build_version
                for row in self.candidates
            ):
                raise BatchInvalid("source_provenance_version_invalid")
            joins = ("client_scope_id", "run_id", "signal_date", "market", "signal_id")
            for member in self.membership:
                if (
                    sum(
                        all(candidate.get(key) == member.get(key) for key in joins)
                        for candidate in self.candidates
                    )
                    != 1
                ):
                    raise BatchInvalid("source_provenance_conflict")
        else:
            versions = {row.get("cluster_build_version") for row in self.candidates}
            if len(versions) > 1 or not versions <= {"hybrid_graph_v1", "hybrid_graph_v2"}:
                raise BatchInvalid("source_provenance_version_invalid")
        for table in TABLE_BINDINGS:
            rows = getattr(self, table)
            if not isinstance(rows, (tuple, list)):
                raise BatchInvalid(f"{table} must be a tuple or list")
            canonical_rows = tuple(
                _canonicalize_row(table, row, cluster_build_version=self.cluster_build_version)
                for row in rows
            )
            if len({_natural_key(table, row) for row in canonical_rows}) != len(canonical_rows):
                raise BatchInvalid(f"{table} contains duplicate natural keys")
            ordered_rows = tuple(
                sorted(
                    canonical_rows,
                    key=lambda row: (
                        _natural_key(table, row),
                        canonical_typed_json(
                            table, row, cluster_build_version=self.cluster_build_version
                        ),
                    ),
                )
            )
            object.__setattr__(self, table, ordered_rows)


RUN_RECEIPT_TABLE = "open_intelligence_run_receipts_v1"


def build_run_receipt_row(
    batch: OpenIntelligenceRowBatch,
    analysis_rows: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    client_scope_id: str,
    market_scope: Sequence[str],
    signal_date: date,
    observation_start: date,
    observation_end: date,
    observation_method: str,
    source_window_digest: str,
    cluster_build_version: str,
    source_family_map_version: str,
    rule_version: str,
    status: str,
    complete_partitions: bool,
    source_sha: str,
    completed_at: datetime,
    row_set_digest: str | None = None,
) -> OpenIntelligenceRunReceipt:
    """The immutable receipt for one run, derived from the rows themselves.

    Two rules are enforced by construction rather than by validation. Every
    count comes from the rows present, so a caller cannot declare a total the
    rows do not support. And `display_release_state` is always `blocked`: a run
    can never release itself, and exposure is a separate human decision taken
    against a receipt that already exists.

    Analysis is not one of this batch's row families, so its rows are passed
    separately and counted here. A bare number is never accepted.
    """
    analysis = tuple(analysis_rows)
    _versioned_fields("membership", cluster_build_version)
    if any(row["cluster_build_version"] != cluster_build_version for row in batch.candidates):
        raise BatchInvalid("source_provenance_version_invalid")
    if (
        batch.cluster_build_version is not None
        and batch.cluster_build_version != cluster_build_version
    ):
        raise BatchInvalid("source_provenance_version_invalid")
    if (
        cluster_build_version == "hybrid_graph_v3"
        and batch.cluster_build_version != cluster_build_version
    ):
        raise BatchInvalid("source_provenance_version_invalid")
    if row_set_digest is None:
        row_set_digest = canonical_digest(
            {
                # The batch already canonicalises and sorts its own families in
                # __post_init__, so re-sorting them here would be dead code.
                **{
                    table: [
                        canonical_typed_json(
                            table, row, cluster_build_version=batch.cluster_build_version
                        )
                        for row in getattr(batch, table)
                    ]
                    for table in TABLE_BINDINGS
                },
                # Analysis rows arrive raw, so their order is normalised here or the
                # same run would digest two different ways.
                "analysis": sorted(canonical_bytes(dict(row)).decode("utf-8") for row in analysis),
            }
        )
    elif (
        not isinstance(row_set_digest, str) or re.fullmatch(r"[0-9a-f]{64}", row_set_digest) is None
    ):
        # The digest the written tables report through row_set_digest_sql. BigQuery and
        # Python render floats differently, so the proof and release SQL can only reproduce
        # a digest that was read back through the same SQL.
        raise BatchInvalid("row set digest is invalid")
    return build_run_receipt(
        run_contract_version=RUN_RECEIPT_CONTRACT_VERSION,
        run_id=run_id,
        client_scope_id=client_scope_id,
        market_scope=tuple(market_scope),
        signal_date=signal_date,
        observation_start=observation_start,
        observation_end=observation_end,
        observation_method=observation_method,
        source_window_digest=source_window_digest,
        cluster_build_version=cluster_build_version,
        source_family_map_version=source_family_map_version,
        rule_version=rule_version,
        status=status,
        complete_partitions=complete_partitions,
        display_release_state="blocked",
        candidate_count=len(batch.candidates),
        evidence_count=len(batch.evidence),
        membership_count=len(batch.membership),
        lineage_count=len(batch.lineage),
        analysis_count=len(analysis),
        prediction_count=len(batch.predictions),
        row_set_digest=row_set_digest,
        source_sha=source_sha,
        completed_at=completed_at,
    )


@dataclass(frozen=True, slots=True)
class PersistenceResult:
    project: str
    dataset: str
    dry_run: bool
    validated_counts: Mapping[str, int]
    inserted_counts: Mapping[str, int]
    unchanged_counts: Mapping[str, int]
    conflict_counts: Mapping[str, int]
    statement_digests: Mapping[str, str]
    cleanup_state: str


@dataclass(frozen=True, slots=True)
class PersistenceTarget:
    project: str
    dataset: str
    location: str
    writer_identity: str


@dataclass(frozen=True, slots=True)
class StatementPlan:
    table: str
    sql: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


class _CredentialsProtocol(Protocol):
    service_account_email: str
    quota_project_id: str | None


class _CountRowProtocol(Protocol):
    def __getitem__(self, field: str) -> object: ...


class _LoadJobProtocol(Protocol):
    errors: object
    output_rows: object

    def result(self, retry: Retry | None = ..., timeout: float | None = None) -> object: ...


class _JobProtocol(Protocol):
    errors: object

    def result(
        self,
        page_size: int | None = None,
        max_results: int | None = None,
        retry: Retry | None = ...,
        timeout: float | object | None = ...,
        start_index: int | None = None,
        job_retry: Retry | None = ...,
    ) -> Iterable[_CountRowProtocol]: ...


class _BigQueryClientProtocol(Protocol):
    project: str
    dataset: str
    location: str
    writer_identity: str
    credentials: _CredentialsProtocol

    def create_table(self, table: bigquery.Table, *, exists_ok: bool = False) -> bigquery.Table: ...

    def load_table_from_json(
        self,
        rows: list[dict[str, object]],
        destination: bigquery.Table,
        *,
        location: str,
        job_config: bigquery.LoadJobConfig,
    ) -> _LoadJobProtocol: ...

    def query(
        self,
        sql: str,
        *,
        job_config: bigquery.QueryJobConfig,
        location: str,
        retry: Retry | None,
        job_retry: Retry | None,
    ) -> _JobProtocol: ...

    def delete_table(self, table: bigquery.Table, *, not_found_ok: bool) -> None: ...


def _validate_target(project: object, dataset: object) -> tuple[str, str]:
    if project != TARGET_PROJECT or dataset != TARGET_DATASET:
        raise TargetInvalid("only the exact approved staging target is allowed")
    return TARGET_PROJECT, TARGET_DATASET


def _validated_target_descriptor(
    descriptor: object,
    project: str,
    dataset: str,
    *,
    writer_identity: str = TARGET_WRITER_IDENTITY,
) -> PersistenceTarget:
    if descriptor is None:
        raise TargetInvalid("a target descriptor is required")
    if isinstance(descriptor, PersistenceTarget):
        values = {
            "project": descriptor.project,
            "dataset": descriptor.dataset,
            "location": descriptor.location,
            "writer_identity": descriptor.writer_identity,
        }
    else:
        try:
            values = vars(descriptor)
        except TypeError as error:
            raise TargetInvalid("target descriptor is invalid") from error
    target = PersistenceTarget(
        project=values.get("project", ""),
        dataset=values.get("dataset", ""),
        location=values.get("location", ""),
        writer_identity=values.get("writer_identity", ""),
    )
    if (
        target.project != project
        or target.dataset != dataset
        or target.location != TARGET_LOCATION
        or target.writer_identity != writer_identity
    ):
        raise TargetInvalid("target descriptor is not the exact approved staging target")
    return target


DAILY_PROFILE_RULE_STATUS = "daily_profile_certified"


def _validate_daily_profile_rule_bundle(rule_bundle: object) -> None:
    """The daily profile path: the profile's own rules, bound to the profile that admits them.

    Additive beside the replay path, never instead of it. A daily bundle names its rule
    version, the release profile it belongs to by name and digest, the source policy the
    profile carries, and who approved it and when; a replay bundle is still admitted only
    as replay certified.
    """
    for field in ("rule_version", "profile_name", "approved_by"):
        if not isinstance(getattr(rule_bundle, field, None), str) or not getattr(
            rule_bundle, field
        ):
            raise RuleInvalid(f"{field} is required")
    for field in ("profile_digest", "source_policy_digest"):
        value = getattr(rule_bundle, field, None)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise RuleInvalid(f"{field} must be a sha256 digest")
    approved_at = getattr(rule_bundle, "approved_at", None)
    if not isinstance(approved_at, datetime) or approved_at.tzinfo is None:
        raise RuleInvalid("approved_at must be a timezone-aware timestamp")


def validate_rule_bundle(rule_bundle: object) -> None:
    if getattr(rule_bundle, "status", None) == DAILY_PROFILE_RULE_STATUS:
        _validate_daily_profile_rule_bundle(rule_bundle)
        return
    if getattr(rule_bundle, "status", None) != "replay_certified":
        raise RuleInvalid("rule bundle must be replay_certified")
    for field in ("rule_version", "replay_receipt_id", "approved_by"):
        if not isinstance(getattr(rule_bundle, field, None), str) or not getattr(
            rule_bundle, field
        ):
            raise RuleInvalid(f"{field} is required")
    approved_at = getattr(rule_bundle, "approved_at", None)
    if not isinstance(approved_at, datetime) or approved_at.tzinfo is None:
        raise RuleInvalid("approved_at must be a timezone-aware timestamp")


def plan_statements(
    project: object, dataset: object, batch: OpenIntelligenceRowBatch
) -> Mapping[str, StatementPlan]:
    """Create deterministic, read-only dry-run statements in required table order."""
    project, dataset = _validate_target(project, dataset)
    if not isinstance(batch, OpenIntelligenceRowBatch):
        raise BatchInvalid("batch must be OpenIntelligenceRowBatch")
    return MappingProxyType(
        {
            table: StatementPlan(
                table=table_name,
                sql=(
                    "SELECT @validated_row_count AS validated_row_count\n"
                    f"FROM `{project}.{dataset}.{table_name}`\n"
                    "WHERE FALSE"
                ),
                parameters={
                    "canonical_rows": getattr(batch, table),
                    "natural_keys": tuple(
                        _natural_key(table, row) for row in getattr(batch, table)
                    ),
                    "validated_row_count": len(getattr(batch, table)),
                    **(
                        {"cluster_build_version": batch.cluster_build_version}
                        if batch.cluster_build_version is not None
                        else {}
                    ),
                },
            )
            for table, table_name in TABLE_BINDINGS.items()
        }
    )


def _canonical_plan_json(table: str, plan: StatementPlan) -> str:
    parameters = plan.parameters
    canonical_rows = parameters["canonical_rows"]
    natural_keys = parameters["natural_keys"]
    row_count = parameters["validated_row_count"]
    if (
        not isinstance(canonical_rows, tuple)
        or not isinstance(natural_keys, tuple)
        or isinstance(row_count, bool)
        or not isinstance(row_count, int)
    ):
        raise BatchInvalid("statement plan parameters are invalid")
    return json.dumps(
        {
            "table": plan.table,
            "sql": plan.sql,
            "parameters": {
                "canonical_rows": [
                    json.loads(
                        canonical_typed_json(
                            table,
                            row,
                            cluster_build_version=parameters.get("cluster_build_version"),
                        )
                    )
                    for row in canonical_rows
                ],
                "natural_keys": [
                    [_typed_value(value) for value in natural_key] for natural_key in natural_keys
                ],
                "validated_row_count": _typed_value(row_count),
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _statement_digests(statements: Mapping[str, StatementPlan]) -> Mapping[str, str]:
    return MappingProxyType(
        {
            table: hashlib.sha256(_canonical_plan_json(table, plan).encode("utf-8")).hexdigest()
            for table, plan in statements.items()
        }
    )


def validate_real_client(
    client: object,
    project: str,
    *,
    writer_identity: str = TARGET_WRITER_IDENTITY,
) -> _BigQueryClientProtocol:
    if (
        getattr(client, "project", None) != project
        or getattr(client, "location", None) != TARGET_LOCATION
    ):
        raise TargetInvalid("client project and location must match the approved target")
    credentials = getattr(client, "_credentials", None)
    if (
        isinstance(credentials, ComputeCredentials)
        and getattr(credentials, "service_account_email", None) == "default"
    ):
        try:
            credentials.refresh(AuthRequest())
        except GoogleAuthError as error:
            raise TargetInvalid(
                "client credentials must use the approved staging writer"
            ) from error
    if getattr(credentials, "service_account_email", None) != writer_identity:
        raise TargetInvalid("client credentials must use the approved staging writer")
    if getattr(credentials, "quota_project_id", None) not in (None, project):
        raise TargetInvalid("client credentials cannot use a foreign quota project")
    return cast(_BigQueryClientProtocol, client)


def _schema(table: str, cluster_build_version: str | None = None) -> list[bigquery.SchemaField]:
    fields: list[bigquery.SchemaField] = []
    for field in _versioned_fields(table, cluster_build_version):
        if field in _REPEATED_FIELDS:
            fields.append(bigquery.SchemaField(field, "STRING", mode="REPEATED"))
        elif field in _DATE_FIELDS:
            fields.append(bigquery.SchemaField(field, "DATE", mode="REQUIRED"))
        elif field in _TIMESTAMP_FIELDS:
            fields.append(
                bigquery.SchemaField(
                    field, "TIMESTAMP", mode="NULLABLE" if field in _OPTIONAL_FIELDS else "REQUIRED"
                )
            )
        elif field in _FLOAT_FIELDS:
            fields.append(
                bigquery.SchemaField(
                    field, "FLOAT64", mode="NULLABLE" if field in _OPTIONAL_FIELDS else "REQUIRED"
                )
            )
        elif field in _INTEGER_FIELDS:
            fields.append(
                bigquery.SchemaField(
                    field, "INT64", mode="NULLABLE" if field in _OPTIONAL_FIELDS else "REQUIRED"
                )
            )
        elif field in _BOOLEAN_FIELDS:
            fields.append(bigquery.SchemaField(field, "BOOL", mode="REQUIRED"))
        elif field in _STRUCT_FIELDS:
            fields.append(
                bigquery.SchemaField(
                    field,
                    "RECORD",
                    mode="REQUIRED",
                    fields=(
                        bigquery.SchemaField("velocity", "FLOAT64", mode="REQUIRED"),
                        bigquery.SchemaField("breadth", "FLOAT64", mode="REQUIRED"),
                        bigquery.SchemaField("evidence_family_count", "INT64", mode="REQUIRED"),
                    ),
                )
            )
        else:
            fields.append(
                bigquery.SchemaField(
                    field, "STRING", mode="NULLABLE" if field in _OPTIONAL_FIELDS else "REQUIRED"
                )
            )
    return fields


def _temporary_table(
    project: str,
    dataset: str,
    table: str,
    statement_digest: str,
    cluster_build_version: str | None = None,
) -> bigquery.Table:
    created_at = datetime.now(UTC)
    temporary = bigquery.Table(
        f"{project}.{dataset}._oi_{statement_digest[:12]}_{uuid.uuid4().hex}_{TABLE_BINDINGS[table]}",
        schema=_schema(table, cluster_build_version),
    )
    temporary.expires = created_at + timedelta(hours=1)
    temporary.labels = {"open_intelligence_table": table}
    return temporary


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {field: _json_value(item) for field, item in value.items()}
    return value


def _load_rows(rows: tuple[Mapping[str, object], ...]) -> list[dict[str, object]]:
    return [{field: _json_value(row[field]) for field in row} for row in rows]


def _load_job_config() -> bigquery.LoadJobConfig:
    return bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        autodetect=False,
    )


def _query_job_config() -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(use_legacy_sql=False)


def _canonical_sql(table: str, alias: str, cluster_build_version: str | None = None) -> str:
    fields = ", ".join(
        f"{alias}.`{field}` AS `{field}`"
        for field in _versioned_fields(table, cluster_build_version)
    )
    return f"TO_JSON_STRING(STRUCT({fields}))"


def _temporary_identifier(temporary: bigquery.Table) -> str:
    return f"`{temporary.project}.{temporary.dataset_id}.{temporary.table_id}`"


def _natural_key_match(table: str) -> str:
    return " AND ".join(f"target.`{field}` = staged.`{field}`" for field in NATURAL_KEYS[table])


def _conflict_sql(project: str, dataset: str, table: str, temporary: bigquery.Table) -> str:
    target = f"`{project}.{dataset}.{TABLE_BINDINGS[table]}`"
    temp = _temporary_identifier(temporary)
    natural_key_match = _natural_key_match(table)
    target_json = _canonical_sql(table, "target")
    staged_json = _canonical_sql(table, "staged")
    return (
        "SELECT COUNT(*) AS conflict_count\n"
        f"FROM {temp} AS staged JOIN {target} AS target ON {natural_key_match}\n"
        f"WHERE {staged_json} != {target_json}"
    )


def _transaction_sql(
    project: str,
    dataset: str,
    table: str,
    temporary: bigquery.Table,
    cluster_build_version: str | None = None,
) -> str:
    target = f"`{project}.{dataset}.{TABLE_BINDINGS[table]}`"
    temp = _temporary_identifier(temporary)
    natural_key_match = _natural_key_match(table)
    conflict_name = "candidate_run_conflict" if table == "candidates" else "immutable_conflict"
    target_json = _canonical_sql(table, "target", cluster_build_version)
    staged_json = _canonical_sql(table, "staged", cluster_build_version)
    fields = ", ".join(f"`{field}`" for field in _versioned_fields(table, cluster_build_version))
    selected_fields = ", ".join(
        f"staged.`{field}`" for field in _versioned_fields(table, cluster_build_version)
    )
    write_sql = (
        f"MERGE {target} AS target USING {temp} AS staged ON {natural_key_match}\n"
        f"WHEN NOT MATCHED THEN INSERT ({fields}) VALUES ({selected_fields});\n"
        if table == "candidates"
        else f"INSERT INTO {target} ({fields})\nSELECT {selected_fields} FROM {temp} AS staged\n"
        f"WHERE NOT EXISTS (SELECT 1 FROM {target} AS target WHERE {natural_key_match});\n"
    )
    return (
        "DECLARE unchanged_count INT64 DEFAULT 0;\n"
        "DECLARE inserted_count INT64 DEFAULT 0;\n"
        "DECLARE conflict_count INT64 DEFAULT 0;\n"
        "DECLARE status INT64 DEFAULT 0;\n"
        "BEGIN TRANSACTION;\n"
        "SET conflict_count = (\n"
        f"  SELECT COUNT(*) FROM {temp} AS staged JOIN {target} AS target ON {natural_key_match}\n"
        f"  WHERE {staged_json} != {target_json}\n"
        ");\n"
        "BEGIN\n"
        "  ASSERT conflict_count = 0 AS 'immutable_conflict';\n"
        "EXCEPTION WHEN ERROR THEN\n"
        "  ROLLBACK TRANSACTION;\n"
        "  SET status = 1;\n"
        "END;\n"
        "IF status = 0 THEN\n"
        "ASSERT NOT EXISTS (\n"
        f"  SELECT 1 FROM {temp} AS staged JOIN {target} AS target ON {natural_key_match}\n"
        f"  WHERE {staged_json} != {target_json}\n"
        f") AS '{conflict_name}';\n"
        "SET unchanged_count = (\n"
        f"  SELECT COUNT(*) FROM {temp} AS staged JOIN {target} AS target ON {natural_key_match}\n"
        f"  WHERE {staged_json} = {target_json}\n"
        ");\n"
        + write_sql
        + "SET inserted_count = @@row_count;\n"
        + "COMMIT TRANSACTION;\nEND IF;\n"
        + "SELECT inserted_count AS inserted_count, unchanged_count AS unchanged_count, "
        + "conflict_count AS conflict_count, status AS status;"
    )


def _count_value(row: _CountRowProtocol, field: str) -> int:
    try:
        value = row[field]
    except (KeyError, TypeError) as error:
        raise PersistenceError(f"transaction returned invalid {field}") from error
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PersistenceError(f"transaction returned invalid {field}")
    return value


def _row_counts(job: _JobProtocol, *fields: str) -> tuple[int, ...]:
    returned = job.result(retry=None, job_retry=None)
    if getattr(job, "errors", None):
        raise PersistenceError("transaction job returned errors")
    iterator = iter(returned)
    try:
        row = next(iterator)
    except StopIteration as error:
        raise PersistenceError("transaction did not return exactly one count row") from error
    try:
        next(iterator)
    except StopIteration:
        pass
    else:
        raise PersistenceError("transaction did not return exactly one count row")
    if not hasattr(row, "__getitem__"):
        raise PersistenceError("transaction count row is invalid")
    return tuple(_count_value(row, field) for field in fields)


def _failure_for(table: str, error: Exception) -> PersistenceError:
    text = f"{getattr(error, 'reason', '')} {error}".lower()
    if any(token in text for token in ("concurrent", "serialization", "aborted")):
        return ConcurrentWriteConflict("concurrent write conflict")
    if "candidate_run_conflict" in text:
        return CandidateRunConflict("candidate run conflicts with immutable content")
    if "immutable_conflict" in text:
        return ImmutableConflict("immutable row conflicts with existing content")
    return PersistenceError(f"persistence failed while writing {table}")


def _result(
    *,
    project: str,
    dataset: str,
    batch: OpenIntelligenceRowBatch,
    inserted_counts: Mapping[str, int],
    unchanged_counts: Mapping[str, int],
    conflict_counts: Mapping[str, int],
    statement_digests: Mapping[str, str],
    cleanup_state: str,
) -> PersistenceResult:
    return PersistenceResult(
        project=project,
        dataset=dataset,
        dry_run=False,
        validated_counts=MappingProxyType(
            {table: len(getattr(batch, table)) for table in TABLE_BINDINGS}
        ),
        inserted_counts=MappingProxyType(dict(inserted_counts)),
        unchanged_counts=MappingProxyType(dict(unchanged_counts)),
        conflict_counts=MappingProxyType(dict(conflict_counts)),
        statement_digests=statement_digests,
        cleanup_state=cleanup_state,
    )


def persist_open_intelligence_rows(
    *,
    project: str,
    dataset: str,
    client: object | None,
    batch: OpenIntelligenceRowBatch,
    rule_bundle: object,
    dry_run: bool,
    writer_identity: str = TARGET_WRITER_IDENTITY,
) -> PersistenceResult:
    """Validate, plan, and write canonical rows through an injected client."""
    if type(dry_run) is not bool:
        raise BatchInvalid("dry_run must be boolean")
    project, dataset = _validate_target(project, dataset)
    validate_rule_bundle(rule_bundle)
    statements = plan_statements(project, dataset, batch)
    validated_counts = MappingProxyType(
        {table: len(getattr(batch, table)) for table in TABLE_BINDINGS}
    )
    zero_counts = MappingProxyType(dict.fromkeys(TABLE_BINDINGS, 0))
    statement_digests = _statement_digests(statements)
    if dry_run:
        _validated_target_descriptor(client, project, dataset, writer_identity=writer_identity)
        return PersistenceResult(
            project=project,
            dataset=dataset,
            dry_run=True,
            validated_counts=validated_counts,
            inserted_counts=zero_counts,
            unchanged_counts=zero_counts,
            conflict_counts=zero_counts,
            statement_digests=statement_digests,
            cleanup_state="not_started",
        )

    writer = validate_real_client(client, project, writer_identity=writer_identity)
    inserted_counts = dict.fromkeys(TABLE_BINDINGS, 0)
    unchanged_counts = dict.fromkeys(TABLE_BINDINGS, 0)
    conflict_counts = dict.fromkeys(TABLE_BINDINGS, 0)
    created_tables: list[bigquery.Table] = []
    primary_error: PersistenceError | None = None
    primary_cause: Exception | None = None
    failed_table: str | None = None
    cleanup_exceptions: list[Exception] = []

    try:
        for table in TABLE_BINDINGS:
            rows = getattr(batch, table)
            if not rows:
                continue
            failed_table = table
            temporary = _temporary_table(
                project, dataset, table, statement_digests[table], batch.cluster_build_version
            )
            created_tables.append(temporary)
            writer.create_table(temporary, exists_ok=False)
            load_job = writer.load_table_from_json(
                _load_rows(rows),
                temporary,
                location=TARGET_LOCATION,
                job_config=_load_job_config(),
            )
            load_job.result()
            if getattr(load_job, "errors", None):
                raise PersistenceError("temporary load returned row errors")
            output_rows = getattr(load_job, "output_rows", None)
            if (
                isinstance(output_rows, bool)
                or not isinstance(output_rows, int)
                or output_rows != len(rows)
            ):
                raise PersistenceError("temporary load was partial")
            transaction_job = writer.query(
                _transaction_sql(project, dataset, table, temporary, batch.cluster_build_version),
                location=TARGET_LOCATION,
                job_config=_query_job_config(),
                retry=None,
                job_retry=None,
            )
            inserted, unchanged, conflicts, status = _row_counts(
                transaction_job, "inserted_count", "unchanged_count", "conflict_count", "status"
            )
            if status == 1:
                conflict_counts[table] = conflicts
                error_type = CandidateRunConflict if table == "candidates" else ImmutableConflict
                raise error_type("immutable content conflicts with staged rows", failed_table=table)
            inserted_counts[table] = inserted
            unchanged_counts[table] = unchanged
            failed_table = None
    except Exception as error:
        if isinstance(error, PersistenceError):
            primary_error = error
        else:
            primary_error = _failure_for(failed_table or "unknown", error)
            primary_cause = error
        if failed_table is not None:
            primary_error.failed_table = failed_table
    finally:
        for temporary in reversed(created_tables):
            try:
                writer.delete_table(temporary, not_found_ok=True)
            except Exception as error:
                cleanup_exceptions.append(error)

    cleanup_state = "failed" if cleanup_exceptions else "complete"
    result = _result(
        project=project,
        dataset=dataset,
        batch=batch,
        inserted_counts=inserted_counts,
        unchanged_counts=unchanged_counts,
        conflict_counts=conflict_counts,
        statement_digests=statement_digests,
        cleanup_state=cleanup_state,
    )
    cleanup_errors = tuple(
        CleanupFailure("temporary table cleanup failed", result=result) for _ in cleanup_exceptions
    )
    for cleanup_error, cleanup_exception in zip(cleanup_errors, cleanup_exceptions, strict=True):
        cleanup_error.__cause__ = cleanup_exception
    if primary_error is not None:
        primary_error.result = result
        primary_error.cleanup_errors = cleanup_errors
        if primary_cause is not None:
            raise primary_error from primary_cause
        raise primary_error
    if cleanup_errors:
        aggregate_cleanup_error = CleanupFailure(
            "temporary table cleanup failed",
            result=result,
            cleanup_errors=cleanup_errors,
        )
        raise aggregate_cleanup_error from cleanup_exceptions[0]
    return result


DAILY_PERSIST_MUTEX_TABLE = "open_intelligence_daily_persist_mutex_v1"
DAILY_PERSIST_MUTEX_ID = "daily_composer"
_DAILY_RECEIPT_INPUT_FIELDS = frozenset(
    {
        "client_scope_id",
        "market_scope",
        "observation_start",
        "observation_method",
        "source_window_digest",
        "cluster_build_version",
        "rule_version",
        "source_family_map_version",
        "source_sha",
        "complete_partitions",
        "run_id",
        "signal_date",
        "completed_at",
    }
)
_DAILY_RETRY_BINDING_FIELDS = tuple(
    field
    for field in RUN_RECEIPT_ROW_FIELDS
    if field not in {"run_id", "row_set_digest", "display_release_state"}
)


def _daily_receipt(
    batch: OpenIntelligenceRowBatch, receipt_fields: Mapping[str, object], row_set_digest: str
) -> OpenIntelligenceRunReceipt:
    if (
        not isinstance(receipt_fields, Mapping)
        or set(receipt_fields) != _DAILY_RECEIPT_INPUT_FIELDS
    ):
        raise BatchInvalid("daily receipt fields are invalid")
    if batch.outcomes:
        raise BatchInvalid("daily outcomes must be empty")
    fields = dict(receipt_fields)
    run_id = fields["run_id"]
    signal_date = fields["signal_date"]
    client_scope_id = fields["client_scope_id"]
    for table in TABLE_BINDINGS:
        for row in getattr(batch, table):
            if (
                row["run_id"] != run_id
                or row["signal_date"] != signal_date
                or row["client_scope_id"] != client_scope_id
            ):
                raise BatchInvalid("daily batch scope differs")
    return build_run_receipt_row(
        batch,
        (),
        row_set_digest=row_set_digest,
        run_id=run_id,  # type: ignore[arg-type]
        client_scope_id=client_scope_id,  # type: ignore[arg-type]
        market_scope=fields["market_scope"],  # type: ignore[arg-type]
        signal_date=signal_date,  # type: ignore[arg-type]
        observation_start=fields["observation_start"],  # type: ignore[arg-type]
        observation_end=signal_date,  # type: ignore[arg-type]
        observation_method=fields["observation_method"],  # type: ignore[arg-type]
        source_window_digest=fields["source_window_digest"],  # type: ignore[arg-type]
        cluster_build_version=fields["cluster_build_version"],  # type: ignore[arg-type]
        source_family_map_version=fields["source_family_map_version"],  # type: ignore[arg-type]
        rule_version=fields["rule_version"],  # type: ignore[arg-type]
        status="completed",
        complete_partitions=fields["complete_partitions"],  # type: ignore[arg-type]
        source_sha=fields["source_sha"],  # type: ignore[arg-type]
        completed_at=fields["completed_at"],  # type: ignore[arg-type]
    )


def _daily_parameter(field: str, value: object):
    if isinstance(value, bool):
        return bigquery.ScalarQueryParameter(field, "BOOL", value)
    if isinstance(value, int):
        return bigquery.ScalarQueryParameter(field, "INT64", value)
    if isinstance(value, datetime):
        return bigquery.ScalarQueryParameter(field, "TIMESTAMP", value)
    if isinstance(value, date):
        return bigquery.ScalarQueryParameter(field, "DATE", value)
    if isinstance(value, tuple):
        return bigquery.ArrayQueryParameter(field, "STRING", list(value))
    return bigquery.ScalarQueryParameter(field, "STRING", value)


def _daily_staged_relation(
    project: str,
    dataset: str,
    table: str,
    temporary: bigquery.Table | None,
    cluster_build_version: str | None,
) -> str:
    if temporary is not None:
        return _temporary_identifier(temporary)
    fields = ", ".join(f"`{field}`" for field in _versioned_fields(table, cluster_build_version))
    target = f"`{project}.{dataset}.{TABLE_BINDINGS[table]}`"
    return f"(SELECT {fields} FROM {target} WHERE FALSE)"


def _daily_exact_assertions(
    project: str,
    dataset: str,
    table: str,
    staged: str,
    cluster_build_version: str | None,
    equality_condition: str,
) -> str:
    target = f"`{project}.{dataset}.{TABLE_BINDINGS[table]}`"
    match = _natural_key_match(table)
    target_json = _canonical_sql(table, "target", cluster_build_version)
    staged_json = _canonical_sql(table, "staged", cluster_build_version)
    return (
        "ASSERT NOT EXISTS (SELECT 1 FROM "
        f"{target} AS target WHERE target.run_id = @run_id "
        "AND target.client_scope_id != 'qa_canary' "
        f"AND NOT EXISTS (SELECT 1 FROM {staged} AS staged WHERE {match})) "
        "AS 'persist_batch_differs';\n"
        "ASSERT NOT EXISTS (SELECT 1 FROM "
        f"{target} AS target JOIN {staged} AS staged ON {match} "
        f"WHERE target.run_id = @run_id AND {target_json} != {staged_json}) "
        "AS 'persist_batch_differs';\n"
        f"IF {equality_condition} THEN\n"
        "ASSERT NOT EXISTS (SELECT 1 FROM "
        f"{staged} AS staged WHERE NOT EXISTS (SELECT 1 FROM {target} AS target "
        f"WHERE target.run_id = @run_id AND {match})) AS 'persist_batch_differs';\n"
        "END IF;\n"
    )


def _daily_insert_sql(
    project: str,
    dataset: str,
    table: str,
    staged: str,
    cluster_build_version: str | None,
) -> str:
    target = f"`{project}.{dataset}.{TABLE_BINDINGS[table]}`"
    match = _natural_key_match(table)
    fields = ", ".join(f"`{field}`" for field in _versioned_fields(table, cluster_build_version))
    selected = ", ".join(
        f"staged.`{field}`" for field in _versioned_fields(table, cluster_build_version)
    )
    return (
        f"INSERT INTO {target} ({fields}) SELECT {selected} FROM {staged} AS staged "
        f"WHERE NOT EXISTS (SELECT 1 FROM {target} AS target WHERE {match});\n"
        f"SET inserted_{table} = @@row_count;\n"
    )


def _daily_transaction_sql(
    project: str,
    dataset: str,
    staged_tables: Mapping[str, bigquery.Table],
    cluster_build_version: str | None,
) -> str:
    mutex = f"`{project}.{dataset}.{DAILY_PERSIST_MUTEX_TABLE}`"
    receipt = f"`{project}.{dataset}.{RUN_RECEIPT_TABLE}`"
    staged = {
        table: _daily_staged_relation(
            project, dataset, table, staged_tables.get(table), cluster_build_version
        )
        for table in TABLE_BINDINGS
    }
    declarations = [
        "-- daily_composition_atomic_v1",
        "DECLARE receipt_count INT64 DEFAULT 0;",
        "DECLARE inserted_receipt INT64 DEFAULT 0;",
        "DECLARE row_digest STRING;",
        *(f"DECLARE inserted_{table} INT64 DEFAULT 0;" for table in TABLE_BINDINGS),
        "BEGIN TRANSACTION;",
        f"UPDATE {mutex} SET fence_epoch = fence_epoch + 1, updated_at = @completed_at WHERE mutex_id = '{DAILY_PERSIST_MUTEX_ID}';",
        "ASSERT @@row_count = 1 AS 'daily_mutex_invalid';",
        f"ASSERT (SELECT COUNT(*) FROM {mutex}) = 1 AS 'daily_mutex_invalid';",
        f"SET receipt_count = (SELECT COUNT(*) FROM {receipt} WHERE run_id = @run_id);",
        "ASSERT receipt_count <= 1 AS 'persist_receipt_cardinality';",
        f"ASSERT NOT EXISTS (SELECT 1 FROM `{project}.{dataset}.signal_analysis_v2` WHERE run_id = @run_id AND client_scope_id != 'qa_canary') AS 'persist_batch_differs';",
    ]
    exact = [
        _daily_exact_assertions(
            project,
            dataset,
            table,
            staged[table],
            cluster_build_version,
            "receipt_count = 1",
        )
        for table in TABLE_BINDINGS
    ]
    digest_sql = row_set_digest_sql(
        "daily_run.run_id",
        sql_expression=True,
        project=project,
        dataset=dataset,
        cluster_build_version=cluster_build_version,
    )
    existing_checks = [
        "IF receipt_count = 1 THEN",
        "SET row_digest = (SELECT "
        + digest_sql
        + " FROM UNNEST([STRUCT(@run_id AS run_id)]) AS daily_run);",
        f"ASSERT (SELECT COUNT(*) FROM {receipt} WHERE run_id = @run_id AND row_set_digest = row_digest AND display_release_state IN ('blocked', 'enabled')"
        + "".join(f" AND {field} = @{field}" for field in _DAILY_RETRY_BINDING_FIELDS)
        + ") = 1 AS 'persist_batch_differs';",
        "ELSE",
    ]
    inserts = [
        _daily_insert_sql(project, dataset, table, staged[table], cluster_build_version)
        for table in TABLE_BINDINGS
    ]
    post_assertions = [
        _daily_exact_assertions(
            project, dataset, table, staged[table], cluster_build_version, "TRUE"
        )
        for table in TABLE_BINDINGS
    ]
    receipt_columns = ", ".join(f"`{field}`" for field in RUN_RECEIPT_ROW_FIELDS)
    receipt_values = ", ".join(
        "row_digest" if field == "row_set_digest" else f"@{field}"
        for field in RUN_RECEIPT_ROW_FIELDS
    )
    finish = [
        *post_assertions,
        "SET row_digest = (SELECT "
        + digest_sql
        + " FROM UNNEST([STRUCT(@run_id AS run_id)]) AS daily_run);",
        f"INSERT INTO {receipt} ({receipt_columns}) VALUES ({receipt_values});",
        "SET inserted_receipt = @@row_count;",
        f"ASSERT (SELECT COUNT(*) FROM {receipt} WHERE run_id = @run_id) = 1 AS 'persist_receipt_cardinality';",
        "END IF;",
        "COMMIT TRANSACTION;",
        f"SELECT {receipt_columns}, "
        + ", ".join(f"inserted_{table} AS _inserted_{table}" for table in TABLE_BINDINGS)
        + ", 1 + inserted_receipt + "
        + " + ".join(f"inserted_{table}" for table in TABLE_BINDINGS)
        + " AS _affected_rows"
        + f" FROM {receipt} WHERE run_id = @run_id;",
    ]
    return "\n".join((*declarations, *exact, *existing_checks, *inserts, *finish))


def _daily_failure(error: Exception) -> PersistenceError:
    text = f"{getattr(error, 'reason', '')} {error}".lower()
    if any(token in text for token in ("concurrent", "serialization", "aborted")):
        return ConcurrentWriteConflict("concurrent write conflict")
    if "persist_batch_differs" in text or "persist_receipt_cardinality" in text:
        return BatchInvalid("persist_batch_differs")
    if "daily_mutex_invalid" in text:
        return TargetInvalid("daily persistence mutex is invalid")
    return PersistenceError("daily composition persistence failed")


def _daily_receipt_from_row(row: Mapping[str, object]) -> OpenIntelligenceRunReceipt:
    fields = {field: row.get(field) for field in RUN_RECEIPT_ROW_FIELDS}
    if isinstance(fields["market_scope"], list):
        fields["market_scope"] = tuple(fields["market_scope"])
    return build_run_receipt(**fields)


def persist_daily_composition(
    *,
    project: str,
    dataset: str,
    client: object,
    batch: OpenIntelligenceRowBatch,
    rule_bundle: object,
    receipt_fields: Mapping[str, object],
    authority_receipt: object = None,
) -> OpenIntelligenceRunReceipt:
    """Persist one daily composition and its receipt through one mutex-fenced transaction."""
    project, dataset = _validate_target(project, dataset)
    validate_rule_bundle(rule_bundle)
    statements = plan_statements(project, dataset, batch)
    placeholder = _daily_receipt(batch, receipt_fields, "0" * 64)
    writer_identity = TARGET_WRITER_IDENTITY
    if authority_receipt is not None:
        from .daily_products import admit_execution

        writer_identity = admit_execution(authority_receipt).manifest.service_identity
    writer = validate_real_client(client, project, writer_identity=writer_identity)
    created: list[bigquery.Table] = []
    staged: dict[str, bigquery.Table] = {}
    cleanup_exceptions: list[Exception] = []
    committed: OpenIntelligenceRunReceipt | None = None
    inserted_counts = dict.fromkeys(TABLE_BINDINGS, 0)
    try:
        for table in TABLE_BINDINGS:
            rows = getattr(batch, table)
            if not rows:
                continue
            temporary = _temporary_table(
                project,
                dataset,
                table,
                _statement_digests(statements)[table],
                batch.cluster_build_version,
            )
            created.append(temporary)
            writer.create_table(temporary, exists_ok=False)
            load = writer.load_table_from_json(
                _load_rows(rows),
                temporary,
                location=TARGET_LOCATION,
                job_config=_load_job_config(),
            )
            load.result()
            if getattr(load, "errors", None) or getattr(load, "output_rows", None) != len(rows):
                raise PersistenceError("temporary load was partial")
            staged[table] = temporary
        parameters = [
            _daily_parameter(field, getattr(placeholder, field))
            for field in RUN_RECEIPT_ROW_FIELDS
            if field != "row_set_digest"
        ]
        query = writer.query
        query_options = {}
        if hasattr(writer, "query_write"):
            query = writer.query_write
            query_options = {
                "row_bound": sum(len(getattr(batch, table)) for table in TABLE_BINDINGS) + 2,
                "reported_rows_field": "_affected_rows",
            }
        job = query(
            _daily_transaction_sql(project, dataset, staged, batch.cluster_build_version),
            location=TARGET_LOCATION,
            job_config=bigquery.QueryJobConfig(use_legacy_sql=False, query_parameters=parameters),
            retry=None,
            job_retry=None,
            **query_options,
        )
        rows = tuple(job.result(retry=None, job_retry=None))
        if getattr(job, "errors", None) or len(rows) != 1:
            raise PersistenceError("daily transaction did not return one receipt")
        row = dict(rows[0])
        committed = _daily_receipt_from_row(row)
        for table in TABLE_BINDINGS:
            value = row.get(f"_inserted_{table}", 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PersistenceError("daily transaction returned invalid counts")
            inserted_counts[table] = value
    except Exception as error:
        if isinstance(error, PersistenceError):
            raise
        raise _daily_failure(error) from error
    finally:
        for temporary in reversed(created):
            try:
                writer.delete_table(temporary, not_found_ok=True)
            except Exception as error:
                cleanup_exceptions.append(error)
    result = _result(
        project=project,
        dataset=dataset,
        batch=batch,
        inserted_counts=inserted_counts,
        unchanged_counts={
            table: len(getattr(batch, table)) - inserted_counts[table] for table in TABLE_BINDINGS
        },
        conflict_counts=dict.fromkeys(TABLE_BINDINGS, 0),
        statement_digests=_statement_digests(statements),
        cleanup_state="failed" if cleanup_exceptions else "complete",
    )
    if cleanup_exceptions:
        errors = tuple(
            CleanupFailure("temporary table cleanup failed", result=result)
            for _ in cleanup_exceptions
        )
        raise DailyPersistenceCleanupFailure(
            "daily composition committed but staging cleanup failed",
            committed_receipt=cast(OpenIntelligenceRunReceipt, committed),
            result=result,
            cleanup_errors=errors,
        ) from cleanup_exceptions[0]
    return cast(OpenIntelligenceRunReceipt, committed)


# --- The producing bridge -----------------------------------------------------
#
# Approved by 42-noncanary-run-release-approval-request-2026-08-29.md section
# 3.3. A pure function from a pipeline result plus metric and readiness inputs
# to a persistable batch. A component is either admitted with measured metrics
# or skipped with named missing work. There is no third path: a component the
# caller says nothing about, or says two things about, refuses the whole build
# rather than vanishing, because a silently dropped component is a discovery
# that never happened as far as any receipt can tell.


def component_bridge_key(component: object) -> str:
    """The stable identity a caller uses to hand a component its inputs."""
    market = getattr(component, "market", None)
    members = getattr(component, "member_identities", None)
    if not isinstance(market, str) or not market:
        raise BatchInvalid("component market is invalid")
    if not isinstance(members, tuple) or not members:
        raise BatchInvalid("component member identities are invalid")
    return market + "||" + "|".join(sorted(members))


@dataclass(frozen=True)
class ProducingBridgeResult:
    """The batch, what it admitted and skipped, and the geographic record of its rows.

    ``geography`` carries, per admitted component and row, whatever geographic record the
    caller's receipts carried beside themselves: the deciding method, the record that
    carries its evidence, the resolved scope and the method's confidence. No target
    relation has a column for any of it, so it rides here rather than being written; a
    caller whose receipts carry no such record, the replay chain among them, gets an empty
    mapping and nothing else about the batch changes. What rides here is validated against
    the rows it covers by ``_component_geography`` before any row is built.

    Nothing in src or scripts reads this field. It is a sidecar the composer fills and the
    stage hands to a persist client that is unbound in every caller, so whatever it carries
    reaches no decision today. That is the reason the residual ``_component_geography``
    cannot separate is invisible rather than harmless, and it stops being invisible the
    moment a relation on this path gains a geographic column and a reader attributes a row
    by what rides here.
    """

    batch: OpenIntelligenceRowBatch
    admitted: tuple[str, ...]
    skipped: tuple[tuple[str, tuple[str, ...]], ...]
    geography: Mapping[str, Mapping[str, object]] = MappingProxyType({})


def _component_geography(receipts: Mapping[str, object]) -> Mapping[str, object] | None:
    """The geographic record beside one component's receipts, validated against its rows.

    Whatever carried the record must state a scope for every row the component writes and
    for no other row, and what it states for each of them must be a resolved geographic
    scope: the kernel's own record, carrying a deciding method, the reference of the
    record that carries its evidence, and its confidence. Coverage alone was not enough to
    keep the docstring's promise, because a value of ``None``, a bare string, a plain dict
    or an unresolved record covers a row while stating nothing about its geography.

    The check is opt in by attribute presence, and that is deliberate rather than an
    oversight. A receipts mapping that carries no ``geography`` attribute at all skips every
    check below and contributes nothing to the sidecar; a receipts mapping that carries the
    attribute is checked in full, including when it carries ``None``, which is refused
    rather than treated as absent. The replay chain is why: it hands plain receipt mappings
    with no geographic record beside them, and it must keep writing the same batch it wrote
    before this check existed. So the rule is not "a batch states its geography" but "a
    batch that states its geography states it completely and consistently", and a caller
    that means to be checked opts in by carrying the attribute. A caller that stops
    carrying it stops being checked, silently, which is the cost of the opt in and the
    reason the daily composer's own carrier is a type of its own rather than a loose dict.

    An unknown record is refused here rather than carried: the daily composer withholds a
    row it cannot resolve at component level with ``geo_scope_unknown``, so a component
    that reaches a batch has no unknown row, and a caller that presents one is presenting
    a row whose geography nothing states under a record that looks like one.

    Which row a record belongs to is checked twice over, where the record itself says so.
    The blocklist match, the script marker and the content marker are each resolved with
    the row and never with a receipt id, so a record naming one of them while citing any
    other reference did not come out of the kernel for the row it is filed under. And the
    float the batch is about to write for a row is a function of that row's own record, so
    the receipt's in market confidence must equal what the composer reads off the record
    beside it: zero for a contextual or foreign scope, the deciding tier for a local one.

    What survives both is a permutation of two rows whose records are alike in everything
    the kernel carries: both local, both decided by a retained geography receipt, each
    citing its own receipt id rather than its row. That residual changes no float, since
    both carry the same tier, and it changes nothing any reader sees, because no reader
    sees it: the record it lands in is the sidecar on ``ProducingBridgeResult``, which no
    production reader in src or scripts opens. So the residual is invisible, not harmless.
    The two are different claims and only the first is true. The moment a relation on this
    path gains a geographic column, or any reader attributes a row by the sidecar, the
    permutation is a live mis attribution of one row's geography to another, and nothing
    here catches it: separating those two needs a field tying a receipt id to its row,
    which no relation on this path holds.
    """
    from src.analysis.open_intelligence.daily_composer import _geo_confidence
    from src.analysis.open_intelligence.geographic_scope import (
        ROW_CITING_METHODS,
        GeographicScope,
    )

    record = getattr(receipts, "geography", None)
    if record is None and not hasattr(receipts, "geography"):
        return None
    if not isinstance(record, Mapping) or set(record) != set(receipts):
        raise BatchInvalid("a component geographic record must cover exactly its own rows")
    for row_id, scope in record.items():
        if not isinstance(scope, GeographicScope) or scope.scope == "unknown":
            raise BatchInvalid(
                "a component geographic record must state a resolved scope for every row"
                f" it covers; {row_id} states none"
            )
        if scope.method in ROW_CITING_METHODS and scope.evidence_ref != row_id:
            raise BatchInvalid(
                "a component geographic record decided by a row citing method must cite the"
                f" row it is filed under; {row_id} cites {scope.evidence_ref}"
            )
        carried = getattr(receipts[row_id], "geo_confidence", None)
        if carried != _geo_confidence(scope):
            raise BatchInvalid(
                "a component geographic record must agree with the in market confidence its"
                f" own row carries; {row_id} carries {carried}"
            )
    return MappingProxyType(dict(sorted(record.items())))


def _projected_memberships(result: object, component: object) -> tuple[object, ...]:
    from src.analysis.open_intelligence.pipeline import MembershipReceipt

    projection = getattr(result, "evidence_projection", None)
    projected_by_component = getattr(projection, "memberships_by_component", None)
    if not isinstance(projected_by_component, Mapping):
        raise BatchInvalid("measured component membership projection is unavailable")
    expected_identities = tuple(sorted(component.member_identities))
    matches = []
    for values in projected_by_component.values():
        if not isinstance(values, tuple) or any(
            not isinstance(item, MembershipReceipt) for item in values
        ):
            raise BatchInvalid("membership projection is invalid")
        if tuple(sorted(item.member_identity for item in values)) == expected_identities:
            matches.append(values)
    if len(matches) != 1:
        raise BatchInvalid("measured component membership projection is not exact")
    return matches[0]


def _build_membership_rows(
    *,
    candidate: Mapping[str, object],
    component: object,
    projected: tuple[object, ...],
    receipts: Mapping[str, object],
    source_provenance_by_member: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[tuple[Mapping[str, object], ...], bool]:
    available_members = {
        receipt.member_identity
        for receipt in receipts.values()
        if getattr(receipt, "availability", None) == "available"
    }
    label_rows = []
    rows = []
    for item in projected:
        expected_identity = f"{component.market}|{item.candidate_type}|{item.canonical_value}"
        if item.member_identity != expected_identity:
            raise BatchInvalid("membership projection identity is invalid")
        row = {
            "client_scope_id": candidate["client_scope_id"],
            "market_scope": candidate["market_scope"],
            "brand_config_id": candidate["brand_config_id"],
            "audience_lens_ids": candidate["audience_lens_ids"],
            "theme_id": candidate["theme_id"],
            "run_id": candidate["run_id"],
            "contract_version": candidate["contract_version"],
            "signal_date": candidate["signal_date"],
            "market": candidate["market"],
            "signal_id": candidate["signal_id"],
            "member_id": item.member_id,
            "member_identity": item.member_identity,
            "candidate_type": item.candidate_type,
            "canonical_value": item.canonical_value,
            "source_families": item.source_families,
            "vendor_families": item.vendor_families or item.source_families,
            "channel_families": item.channel_families or item.source_families,
            "platforms": item.platforms,
            "row_id": item.row_id,
            "qualifies_evidence": item.member_identity in available_members,
            "created_at": candidate["created_at"],
        }
        if source_provenance_by_member is not None:
            from src.analysis.open_intelligence.source_provenance import encode_source_provenance

            envelope = source_provenance_by_member.get(item.member_identity)
            if envelope is None:
                raise BatchInvalid("source_provenance_missing")
            row["source_provenance_json"] = encode_source_provenance(envelope)
            row["source_families"] = tuple(
                sorted({pair["channel_family"] for pair in envelope["pairs"]})
            )
            row["channel_families"] = row["source_families"]
            row["vendor_families"] = tuple(
                sorted({pair["vendor_family"] for pair in envelope["pairs"]})
            )
            _validate_membership_provenance(row)
        rows.append(row)
        if item.member_identity == candidate["label_member_identity"]:
            label_rows.append(row)
    if len(label_rows) != 1 or label_rows[0]["canonical_value"] != candidate["label"]:
        raise BatchInvalid("candidate label member identity is not exact")
    return tuple(rows), bool(label_rows[0]["qualifies_evidence"])


def _build_promoted_signal(
    candidate: Mapping[str, object],
    readiness: object,
    receipts: Mapping[str, object],
    *,
    label_is_joinable: bool,
    promotion_result: object | None,
) -> object | None:
    from src.analysis.open_intelligence.predictions import PromotedSignal
    from src.analysis.open_intelligence.scoring import DecisionStrengthResult

    if promotion_result is None:
        return None
    if not isinstance(promotion_result, DecisionStrengthResult):
        raise BatchInvalid("promotion result is invalid")
    if promotion_result.signal_id != candidate["signal_id"]:
        raise BatchInvalid("promotion result signal identity differs")
    if (
        candidate["evidence_state"] != "ready"
        or not label_is_joinable
        or promotion_result.promotion_eligible is not True
    ):
        return None
    published = tuple(
        receipt.published_at
        for receipt in receipts.values()
        if receipt.published_at is not None and receipt.availability == "available"
    )
    if not published:
        raise BatchInvalid("ready candidate has no timestamped evidence")
    return PromotedSignal(
        candidate=candidate,
        source_families=readiness.qualifying_families,
        first_seen_at=min(published),
    )


def build_producing_batch(
    result: object,
    *,
    scope: object,
    signal_date: date,
    created_at: datetime,
    metrics_by_component: Mapping[str, object],
    receipts_by_component: Mapping[str, Mapping[str, object]],
    readiness_by_component: Mapping[str, object],
    readiness_rules: object,
    missing_by_component: Mapping[str, tuple[str, ...]],
    quality_evaluated: bool = False,
    promotion_results_by_signal_id: Mapping[str, object] | None = None,
    quality_failed_by_component: Mapping[str, bool] | None = None,
    factual_conflict_by_component: Mapping[str, bool] | None = None,
    independence_policy: str = "wave1_family_v1",
    telemetry: object | None = None,
) -> ProducingBridgeResult:
    from src.analysis.open_intelligence.pipeline import (
        DynamicSignalRunResult,
        ProvenanceSignalRunResult,
    )
    from src.analysis.open_intelligence.predictions import (
        PredictionRules,
        PromotedSignal,
        build_signal_prediction_rows,
    )
    from src.analysis.open_intelligence.rows import build_dynamic_signal_rows
    from src.analysis.open_intelligence.source_provenance import encode_source_provenance

    provenance = None
    version = None
    source_binding_digest = None
    if isinstance(result, ProvenanceSignalRunResult):
        if (
            not isinstance(result.run, DynamicSignalRunResult)
            or result.run.rule_version != "composition_rules_v3"
        ):
            raise BatchInvalid("source_provenance_version_invalid")
        if (
            not isinstance(result.source_snapshot_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", result.source_snapshot_digest) is None
        ):
            raise BatchInvalid("source_provenance_snapshot_mismatch")
        provenance = result.source_provenance_by_member
        if not isinstance(provenance, Mapping):
            raise BatchInvalid("source_provenance_missing")
        expected_members = {
            f"{observation.market}|{observation.candidate_type}|{observation.term}"
            for observation in result.run.observations
        }
        if set(provenance) != expected_members:
            raise BatchInvalid("source_provenance_conflict")
        for envelope in provenance.values():
            try:
                encode_source_provenance(envelope)
            except ValueError as error:
                raise BatchInvalid(str(error)) from error
            if envelope["source_snapshot_digest"] != result.source_snapshot_digest:
                raise BatchInvalid("source_provenance_snapshot_mismatch")
        source_binding_digest = result.source_snapshot_digest
        result = result.run
        version = "hybrid_graph_v3"
    elif getattr(result, "rule_version", None) == "composition_rules_v3":
        raise BatchInvalid("source_provenance_missing")

    error_state = getattr(result, "error_state", None)
    if error_state is not None:
        raise BatchInvalid(f"pipeline result carries an error state: {error_state}")
    components = getattr(result, "components", None)
    if not isinstance(components, tuple):
        raise BatchInvalid("pipeline result components are invalid")
    if version is not None and any(component.build_version != version for component in components):
        raise BatchInvalid("source_provenance_version_invalid")

    candidates: list[Mapping[str, object]] = []
    evidence: list[Mapping[str, object]] = []
    membership_rows: list[Mapping[str, object]] = []
    promoted_signals: list[PromotedSignal] = []
    admitted: list[str] = []
    skipped: list[tuple[str, tuple[str, ...]]] = []
    geography: dict[str, Mapping[str, object]] = {}
    seen: set[str] = set()
    promotion_results = dict(promotion_results_by_signal_id or {})
    # The readiness handed in was classified with the technical verdict and the conflict flag;
    # the row builder recomputes it from the receipts and must see the same flags.
    quality_failed_flags = dict(quality_failed_by_component or {})
    factual_conflict_flags = dict(factual_conflict_by_component or {})
    for component in components:
        key = component_bridge_key(component)
        if key in seen:
            raise BatchInvalid("duplicate component identity in pipeline result")
        seen.add(key)
        measured = key in metrics_by_component
        named_missing = key in missing_by_component
        if measured and named_missing:
            raise BatchInvalid("a component cannot be both measured and missing metric authority")
        if not measured and not named_missing:
            raise BatchInvalid("a component must be measured or skipped with named missing work")
        if named_missing:
            reasons = tuple(sorted(set(missing_by_component[key])))
            if not reasons:
                raise BatchInvalid("a skipped component needs at least one named reason")
            skipped.append((key, reasons))
            continue
        if key not in receipts_by_component or key not in readiness_by_component:
            raise BatchInvalid("a measured component needs receipts and readiness to be admitted")
        candidate_row, evidence_rows = build_dynamic_signal_rows(
            component=component,
            scope=scope,
            signal_date=signal_date,
            created_at=created_at,
            metrics=metrics_by_component[key],
            receipts=receipts_by_component[key],
            readiness=readiness_by_component[key],
            readiness_rules=readiness_rules,
            quality_evaluated=quality_evaluated,
            quality_failed=bool(quality_failed_flags.get(key, False)),
            factual_conflict=bool(factual_conflict_flags.get(key, False)),
            # The run profile names the independence policy; a new staging run passes
            # explicit_origin_v2 and the release refuses a run whose policy differs.
            independence_policy=independence_policy,
        )
        projected_memberships = _projected_memberships(result, component)
        if version is not None and candidate_row["cluster_build_version"] != version:
            raise BatchInvalid("source_provenance_version_invalid")
        component_membership_rows, label_is_joinable = _build_membership_rows(
            candidate=candidate_row,
            component=component,
            projected=projected_memberships,
            receipts=receipts_by_component[key],
            source_provenance_by_member=provenance,
        )
        membership_rows.extend(component_membership_rows)
        promoted = _build_promoted_signal(
            candidate_row,
            readiness_by_component[key],
            receipts_by_component[key],
            label_is_joinable=label_is_joinable,
            promotion_result=promotion_results.get(candidate_row["signal_id"]),
        )
        if promoted is not None:
            promoted_signals.append(promoted)
        component_geography = _component_geography(receipts_by_component[key])
        if component_geography is not None:
            geography[key] = component_geography
        candidates.append(candidate_row)
        evidence.extend(evidence_rows)
        admitted.append(key)

    candidate_signal_ids = {row["signal_id"] for row in candidates}
    if set(promotion_results) - candidate_signal_ids:
        raise BatchInvalid("promotion result names an unknown signal")

    predictions = build_signal_prediction_rows(
        promoted_signals=promoted_signals,
        predicted_at=created_at,
        rules=PredictionRules(),
    )

    batch = OpenIntelligenceRowBatch(
        candidates=tuple(candidates),
        evidence=tuple(evidence),
        membership=tuple(membership_rows),
        lineage=(),
        predictions=predictions,
        outcomes=(),
        cluster_build_version=version,
    )
    if telemetry is not None:
        _record_membership_telemetry(
            telemetry,
            candidates=candidates,
            membership_rows=membership_rows,
            receipts_by_component=receipts_by_component,
            created_at=created_at,
            source_binding_digest=source_binding_digest,
        )
    return ProducingBridgeResult(
        batch=batch,
        admitted=tuple(admitted),
        skipped=tuple(sorted(skipped)),
        geography=MappingProxyType(dict(sorted(geography.items()))),
    )


def _record_membership_telemetry(
    telemetry: object,
    *,
    candidates: list[Mapping[str, object]],
    membership_rows: list[Mapping[str, object]],
    receipts_by_component: Mapping[str, Mapping[str, object]],
    created_at: datetime,
    source_binding_digest: str | None,
) -> None:
    """Hand the bridge's own rows and receipts to the injected telemetry.

    The batch is already built; nothing here changes it. Without a source
    snapshot digest the membership boundary has no source binding to record
    and is marked unavailable rather than bound to a guessed digest.
    """
    operation_id = None
    for row in (*membership_rows, *candidates):
        run_id = row.get("run_id")
        if isinstance(run_id, str) and run_id:
            operation_id = run_id
            break
    if operation_id is None:
        telemetry.boundary_unavailable(
            "membership", operation_id="unbound", reason_code="operation_id_unavailable"
        )
        return
    telemetry.count(
        "candidates", len(candidates), operation_id=operation_id, observed_at=created_at
    )
    telemetry.count(
        "membership_edges",
        len(membership_rows),
        operation_id=operation_id,
        observed_at=created_at,
    )
    if source_binding_digest is None:
        telemetry.boundary_unavailable(
            "membership",
            operation_id=operation_id,
            reason_code="source_binding_digest_unavailable",
        )
        return
    availability_by_member = {
        receipt.member_identity: getattr(receipt, "availability", None)
        for receipts in receipts_by_component.values()
        for receipt in receipts.values()
        if hasattr(receipt, "member_identity")
    }
    telemetry.emit_membership(
        membership_rows,
        created_at=created_at,
        source_binding_digest=source_binding_digest,
        availability_by_member=availability_by_member,
    )
