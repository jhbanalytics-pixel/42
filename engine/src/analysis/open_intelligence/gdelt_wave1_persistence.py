"""Insert-only persistence for the bounded GDELT Wave 1 sample."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

from google.cloud import bigquery

from src.analysis.open_intelligence.funded_lane import (
    Wave1ExecutionCapability,
    _require_wave1_execution_capability,
    wave1_pilot_service_identity,
)
from src.ingestion.connectors.gdelt import (
    GDELT_EVENT_MARKET_SCHEMA,
    GDELT_EVENTS_SCHEMA,
    GDELT_GCAM_SCHEMA,
)

TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging"
TARGET_LOCATION = "US"
EVENTS_TABLE = "gdelt_events_wave1_v1"
MARKETS_TABLE = "gdelt_event_market_wave1_v1"
GCAM_TABLE = "gdelt_gcam_wave1_v1"
PROOF_VERSION = "gdelt_wave1_persistence_v1"
ROW_CEILING = 100
PARENT_LIMIT = 20
GCAM_LIMIT = 20
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_RUN = re.compile(r"[a-z0-9_][a-z0-9_-]{0,127}\Z")
_RECEIPT = re.compile(r"gdry_[0-9a-f]{64}\Z")
_FIPS_MARKETS = {"SF": "za", "NI": "ng", "KE": "ke"}
_ROLE_FIELDS = (
    ("action_geo", "action_geo_country_code"),
    ("actor1", "actor1_country_code"),
    ("actor2", "actor2_country_code"),
)
_EVENT_FIELDS = tuple(field[0] for field in GDELT_EVENTS_SCHEMA)
_EVENT_INPUT_FIELDS = (*_EVENT_FIELDS[:4], "action_geo_country_code", *_EVENT_FIELDS[4:])
_MARKET_FIELDS = tuple(field[0] for field in GDELT_EVENT_MARKET_SCHEMA)
_GCAM_FIELDS = tuple(field[0] for field in GDELT_GCAM_SCHEMA)


class GDELTPersistenceError(RuntimeError):
    pass


class GDELTImmutableConflict(GDELTPersistenceError):
    pass


@dataclass(frozen=True, slots=True)
class GDELTWave1Batch:
    run_id: str
    manifest_sha256: str
    events_dry_run_receipt_id: str
    gcam_dry_run_receipt_id: str
    events: tuple[Mapping[str, object], ...]
    event_markets: tuple[Mapping[str, object], ...]
    gcam: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class GDELTWave1PersistenceProof:
    proof_contract_version: str
    run_id: str
    manifest_sha256: str
    events_dry_run_receipt_id: str
    gcam_dry_run_receipt_id: str
    dry_run: bool
    inserted_counts: tuple[tuple[str, int], ...]
    unchanged_counts: tuple[tuple[str, int], ...]
    conflict_counts: tuple[tuple[str, int], ...]
    readback_digests: tuple[tuple[str, str], ...]
    complete: bool


def _text(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field} is invalid")
    return value


def _integer(value: object, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise ValueError(f"{field} is invalid")
    return value


def _float(value: object, field: str, *, nullable: bool = True) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is invalid")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} is invalid")
    return number


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} is invalid")
    return value.astimezone(UTC)


def _event(row: object) -> Mapping[str, object]:
    if not isinstance(row, Mapping) or set(row) != set(_EVENT_INPUT_FIELDS):
        raise ValueError("GDELT event row fields are invalid")
    values = {
        "GLOBALEVENTID": _integer(row["GLOBALEVENTID"], "GLOBALEVENTID", positive=True),
        "event_date": row["event_date"],
        "actor1_country_code": _text(
            row["actor1_country_code"], "actor1_country_code", nullable=True
        ),
        "actor2_country_code": _text(
            row["actor2_country_code"], "actor2_country_code", nullable=True
        ),
        "action_geo_country_code": _text(
            row["action_geo_country_code"], "action_geo_country_code", nullable=True
        ),
        "event_code": _text(row["event_code"], "event_code", nullable=True),
        "event_root_code": _text(row["event_root_code"], "event_root_code", nullable=True),
        "goldstein_scale": _float(row["goldstein_scale"], "goldstein_scale"),
        "mention_count": _integer(row["mention_count"], "mention_count"),
        "source_count": _integer(row["source_count"], "source_count"),
        "article_count": _integer(row["article_count"], "article_count"),
        "average_tone": _float(row["average_tone"], "average_tone"),
        "action_geo_name": _text(row["action_geo_name"], "action_geo_name", nullable=True),
        "action_geo_latitude": _float(row["action_geo_latitude"], "action_geo_latitude"),
        "action_geo_longitude": _float(row["action_geo_longitude"], "action_geo_longitude"),
        "source_url": _text(row["source_url"], "source_url", nullable=True),
        "date_added": _timestamp(row["date_added"], "date_added"),
    }
    if isinstance(values["event_date"], datetime) or not isinstance(values["event_date"], date):
        raise ValueError("event_date is invalid")
    if not any(values[field] in _FIPS_MARKETS for _, field in _ROLE_FIELDS):
        raise ValueError("GDELT event row has no target-market evidence")
    return MappingProxyType(values)


def _gcam_row(row: object) -> Mapping[str, object]:
    if not isinstance(row, Mapping) or set(row) != set(_GCAM_FIELDS):
        raise ValueError("GDELT GCAM row fields are invalid")
    values = {
        "document_url": _text(row["document_url"], "document_url"),
        "published_at": _timestamp(row["published_at"], "published_at"),
        **{
            field: _float(row[field], field)
            for field in ("v10_1", "v10_2", "v19_1", "v19_9", "v20_1")
        },
    }
    if not str(values["document_url"]).startswith(("http://", "https://")):
        raise ValueError("document_url is invalid")
    return MappingProxyType(values)


def _canonical(row: Mapping[str, object], fields: tuple[str, ...]) -> str:
    def value(item: object):
        if isinstance(item, datetime):
            return item.isoformat().replace("+00:00", "Z")
        if isinstance(item, date):
            return item.isoformat()
        return item

    return json.dumps(
        {field: value(row[field]) for field in fields},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _deduplicate(
    rows: Sequence[Mapping[str, object]],
    fields: tuple[str, ...],
    key_fields: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    seen: dict[tuple[object, ...], tuple[str, Mapping[str, object]]] = {}
    for row in rows:
        key = tuple(row[field] for field in key_fields)
        content = _canonical(row, fields)
        existing = seen.get(key)
        if existing is not None and existing[0] != content:
            raise ValueError("GDELT natural key has conflicting content")
        seen[key] = (content, row)
    return tuple(item[1] for _, item in sorted(seen.items(), key=lambda pair: pair[0]))


def _market_rows(
    events: Sequence[Mapping[str, object]],
    manifest_sha256: str,
    receipt_id: str,
) -> tuple[Mapping[str, object], ...]:
    rows = []
    for event in events:
        evidence: dict[str, str] = {}
        for role, field in _ROLE_FIELDS:
            market = _FIPS_MARKETS.get(event[field])
            if market is not None and market not in evidence:
                evidence[market] = role
        for market, role in sorted(evidence.items()):
            payload = {
                "manifest_sha256": manifest_sha256,
                "dry_run_receipt_id": receipt_id,
                "GLOBALEVENTID": event["GLOBALEVENTID"],
                "market": market,
                "evidence_role": role,
            }
            rows.append(
                MappingProxyType(
                    {
                        "GLOBALEVENTID": event["GLOBALEVENTID"],
                        "market": market,
                        "evidence_role": role,
                        "receipt_id": "gdmr_"
                        + hashlib.sha256(
                            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest(),
                    }
                )
            )
    return tuple(rows)


def build_wave1_gdelt_batch(
    *,
    events_rows: Sequence[Mapping[str, object]],
    gcam_rows: Sequence[Mapping[str, object]],
    run_id: str,
    manifest_sha256: str,
    events_dry_run_receipt_id: str,
    gcam_dry_run_receipt_id: str,
) -> GDELTWave1Batch:
    if not isinstance(run_id, str) or _RUN.fullmatch(run_id) is None:
        raise ValueError("run_id is invalid")
    if not isinstance(manifest_sha256, str) or _SHA.fullmatch(manifest_sha256) is None:
        raise ValueError("manifest_sha256 is invalid")
    for receipt_id in (events_dry_run_receipt_id, gcam_dry_run_receipt_id):
        if not isinstance(receipt_id, str) or _RECEIPT.fullmatch(receipt_id) is None:
            raise ValueError("dry-run receipt ID is invalid")
    events_with_geo = _deduplicate(
        tuple(_event(row) for row in events_rows),
        _EVENT_INPUT_FIELDS,
        ("GLOBALEVENTID",),
    )[:PARENT_LIMIT]
    gcam = _deduplicate(
        tuple(_gcam_row(row) for row in gcam_rows),
        _GCAM_FIELDS,
        ("document_url", "published_at"),
    )[:GCAM_LIMIT]
    if not events_with_geo:
        raise ValueError("GDELT Wave 1 Events result is empty")
    if not gcam:
        raise ValueError("GDELT Wave 1 GCAM result is empty")
    markets = _market_rows(events_with_geo, manifest_sha256, events_dry_run_receipt_id)
    events = tuple(
        MappingProxyType({field: row[field] for field in _EVENT_FIELDS}) for row in events_with_geo
    )
    if len(events) + len(markets) + len(gcam) > ROW_CEILING:
        raise ValueError("GDELT Wave 1 row ceiling is exceeded")
    return GDELTWave1Batch(
        run_id=run_id,
        manifest_sha256=manifest_sha256,
        events_dry_run_receipt_id=events_dry_run_receipt_id,
        gcam_dry_run_receipt_id=gcam_dry_run_receipt_id,
        events=events,
        event_markets=markets,
        gcam=gcam,
    )


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return "TIMESTAMP '" + value.isoformat().replace("+00:00", "Z") + "'"
    if isinstance(value, date):
        return "DATE '" + value.isoformat() + "'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    raise ValueError("GDELT SQL literal is unsupported")


_COLUMN_TYPES: Mapping[str, str] = MappingProxyType(
    {
        name: column_type
        for name, column_type, _mode in (
            *GDELT_EVENTS_SCHEMA,
            *GDELT_EVENT_MARKET_SCHEMA,
            *GDELT_GCAM_SCHEMA,
        )
    }
)


def _typed_literal(value: object, field: str) -> str:
    # A column that is NULL in every staged row would type as INT64 in the
    # UNION ALL of literals and lose its supertype with a STRING or FLOAT64
    # target column (refused on staging, 4 Sep 2026); every NULL names its
    # schema type instead.
    if value is None:
        return f"CAST(NULL AS {_COLUMN_TYPES[field]})"
    return _sql_literal(value)


def _staged_sql(rows: Sequence[Mapping[str, object]], fields: tuple[str, ...]) -> str:
    return (
        " UNION ALL ".join(
            "SELECT "
            + ", ".join(f"{_typed_literal(row[field], field)} AS `{field}`" for field in fields)
            for row in rows
        )
        or "SELECT "
        + ", ".join(f"{_typed_literal(None, field)} AS `{field}`" for field in fields)
        + " WHERE FALSE"
    )


# The market receipt hashes the manifest and the dry-run receipt, both new on
# every execution, so it is provenance of the first write rather than content
# (refused the tenth Wave 1 pilot on staging, 4 Sep 2026); the conflict rule
# and the readback compare a market on its key and evidence role.
_MARKET_PROVENANCE_FIELDS = ("receipt_id",)


def _content_fields(fields: tuple[str, ...], provenance: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(field for field in fields if field not in provenance)


def _table_transaction(
    table: str,
    rows: Sequence[Mapping[str, object]],
    fields: tuple[str, ...],
    key_fields: tuple[str, ...],
    prefix: str,
    provenance: tuple[str, ...] = (),
) -> str:
    target = f"`{TARGET_PROJECT}.{TARGET_DATASET}.{table}`"
    staged = _staged_sql(rows, fields)
    key = " AND ".join(
        f"target.`{field}` IS NOT DISTINCT FROM staged.`{field}`" for field in key_fields
    )
    exact = " AND ".join(
        f"target.`{field}` IS NOT DISTINCT FROM staged.`{field}`"
        for field in _content_fields(fields, provenance)
    )
    # The insert's NOT EXISTS is planned as an anti-semi join, which BigQuery
    # only accepts on an equality of fields from both sides (refused at
    # execution on staging, 4 Sep 2026); every key column is NOT NULL, so the
    # equality is the same predicate as the null-safe form above.
    key_equality = " AND ".join(f"target.`{field}` = staged.`{field}`" for field in key_fields)
    columns = ", ".join(f"`{field}`" for field in fields)
    selected = ", ".join(f"staged.`{field}`" for field in fields)
    return (
        f"SET {prefix}_conflict = (WITH staged AS ({staged}) SELECT COUNT(*) FROM staged "
        f"JOIN {target} target ON {key} WHERE NOT ({exact}));\n"
        f"ASSERT {prefix}_conflict = 0 AS 'immutable_conflict';\n"
        f"SET {prefix}_unchanged = (WITH staged AS ({staged}) SELECT COUNT(*) FROM staged "
        f"JOIN {target} target ON {key} WHERE {exact});\n"
        f"INSERT INTO {target} ({columns}) WITH staged AS ({staged}) SELECT {selected} "
        f"FROM staged WHERE NOT EXISTS (SELECT 1 FROM {target} target WHERE {key_equality});\n"
        f"SET {prefix}_inserted = @@row_count;\n"
    )


def _transaction_sql(batch: GDELTWave1Batch) -> str:
    declarations = "\n".join(
        f"DECLARE {prefix}_{kind} INT64 DEFAULT 0;"
        for prefix in ("events", "markets", "gcam")
        for kind in ("inserted", "unchanged", "conflict")
    )
    body = (
        _table_transaction(
            EVENTS_TABLE,
            batch.events,
            _EVENT_FIELDS,
            ("GLOBALEVENTID",),
            "events",
        )
        + _table_transaction(
            MARKETS_TABLE,
            batch.event_markets,
            _MARKET_FIELDS,
            ("GLOBALEVENTID", "market"),
            "markets",
            provenance=_MARKET_PROVENANCE_FIELDS,
        )
        + _table_transaction(
            GCAM_TABLE,
            batch.gcam,
            _GCAM_FIELDS,
            ("document_url", "published_at"),
            "gcam",
        )
    )
    result_fields = ", ".join(
        f"{prefix}_{kind} AS {prefix}_{kind}"
        for prefix in ("events", "markets", "gcam")
        for kind in ("inserted", "unchanged", "conflict")
    )
    return f"{declarations}\nBEGIN TRANSACTION;\n{body}COMMIT TRANSACTION;\nSELECT {result_fields};"


def _client(client: object):
    credentials = getattr(client, "_credentials", None)
    if (
        getattr(client, "project", None) != TARGET_PROJECT
        or getattr(client, "location", None) != TARGET_LOCATION
        or getattr(credentials, "service_account_email", None) != wave1_pilot_service_identity()
        or getattr(credentials, "quota_project_id", None) not in (None, TARGET_PROJECT)
    ):
        raise ValueError("GDELT persistence target is not approved")
    return client


def _counts(row: Mapping[str, object], kind: str) -> tuple[tuple[str, int], ...]:
    values = []
    for prefix, table in (
        ("events", EVENTS_TABLE),
        ("markets", MARKETS_TABLE),
        ("gcam", GCAM_TABLE),
    ):
        value = row.get(f"{prefix}_{kind}")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GDELTPersistenceError("GDELT persistence counts are invalid")
        values.append((table, value))
    return tuple(values)


def _readback_sql(
    table: str,
    rows: Sequence[Mapping[str, object]],
    fields: tuple[str, ...],
    key_fields: tuple[str, ...],
) -> str:
    target = f"`{TARGET_PROJECT}.{TARGET_DATASET}.{table}`"
    predicates = []
    for row in rows:
        predicates.append(
            "("
            + " AND ".join(
                f"`{field}` IS NOT DISTINCT FROM {_sql_literal(row[field])}" for field in key_fields
            )
            + ")"
        )
    where = " OR ".join(predicates) or "FALSE"
    return f"SELECT {', '.join(f'`{field}`' for field in fields)} FROM {target} WHERE {where}"


def _query_rows(job: object, *, max_results: int) -> tuple[Mapping[str, object], ...]:
    if getattr(job, "errors", None):
        raise GDELTPersistenceError("GDELT persistence query returned errors")
    return tuple(
        dict(row) for row in job.result(max_results=max_results, retry=None, job_retry=None)
    )


def persist_wave1_gdelt_batch(
    *,
    client: object,
    batch: GDELTWave1Batch,
    execution_capability: Wave1ExecutionCapability,
    dry_run: bool,
) -> GDELTWave1PersistenceProof:
    _require_wave1_execution_capability(execution_capability, action="gdelt_wave1_persistence")
    if not isinstance(batch, GDELTWave1Batch) or type(dry_run) is not bool:
        raise ValueError("GDELT persistence request is invalid")
    if execution_capability.manifest_sha256 != batch.manifest_sha256:
        raise ValueError("GDELT persistence manifest differs")
    writer = _client(client)
    sql = _transaction_sql(batch)
    dry_job = writer.query(
        sql,
        location=TARGET_LOCATION,
        job_config=bigquery.QueryJobConfig(
            use_legacy_sql=False,
            dry_run=True,
            use_query_cache=False,
        ),
        retry=None,
        job_retry=None,
    )
    dry_job.result()
    empty_counts = tuple((table, 0) for table in (EVENTS_TABLE, MARKETS_TABLE, GCAM_TABLE))
    if dry_run:
        return GDELTWave1PersistenceProof(
            proof_contract_version=PROOF_VERSION,
            run_id=batch.run_id,
            manifest_sha256=batch.manifest_sha256,
            events_dry_run_receipt_id=batch.events_dry_run_receipt_id,
            gcam_dry_run_receipt_id=batch.gcam_dry_run_receipt_id,
            dry_run=True,
            inserted_counts=empty_counts,
            unchanged_counts=empty_counts,
            conflict_counts=empty_counts,
            readback_digests=(),
            complete=False,
        )
    result_rows = _query_rows(
        writer.query(
            sql,
            location=TARGET_LOCATION,
            job_config=bigquery.QueryJobConfig(use_legacy_sql=False),
            retry=None,
            job_retry=None,
        ),
        max_results=2,
    )
    if len(result_rows) != 1:
        raise GDELTPersistenceError("GDELT persistence transaction cardinality is invalid")
    inserted = _counts(result_rows[0], "inserted")
    unchanged = _counts(result_rows[0], "unchanged")
    conflicts = _counts(result_rows[0], "conflict")
    if any(value for _, value in conflicts):
        raise GDELTImmutableConflict("GDELT natural key conflicts with existing content")
    expected = {
        EVENTS_TABLE: batch.events,
        MARKETS_TABLE: batch.event_markets,
        GCAM_TABLE: batch.gcam,
    }
    definitions = {
        EVENTS_TABLE: (_EVENT_FIELDS, ("GLOBALEVENTID",), ()),
        MARKETS_TABLE: (_MARKET_FIELDS, ("GLOBALEVENTID", "market"), _MARKET_PROVENANCE_FIELDS),
        GCAM_TABLE: (_GCAM_FIELDS, ("document_url", "published_at"), ()),
    }
    digests = []
    for table in (EVENTS_TABLE, MARKETS_TABLE, GCAM_TABLE):
        fields, keys, provenance = definitions[table]
        content = _content_fields(fields, provenance)
        rows = _query_rows(
            writer.query(
                _readback_sql(table, expected[table], fields, keys),
                location=TARGET_LOCATION,
                job_config=bigquery.QueryJobConfig(use_legacy_sql=False),
                retry=None,
                job_retry=None,
            ),
            max_results=len(expected[table]) + 1,
        )
        expected_json = tuple(sorted(_canonical(row, content) for row in expected[table]))
        actual_json = tuple(sorted(_canonical(row, content) for row in rows))
        if actual_json != expected_json:
            raise GDELTPersistenceError(f"{table} readback differs")
        digests.append(
            (
                table,
                hashlib.sha256(json.dumps(actual_json, separators=(",", ":")).encode()).hexdigest(),
            )
        )
    count_map = dict(inserted)
    unchanged_map = dict(unchanged)
    if any(count_map[table] + unchanged_map[table] != len(expected[table]) for table in expected):
        raise GDELTPersistenceError("GDELT persistence counts do not reconcile")
    return GDELTWave1PersistenceProof(
        proof_contract_version=PROOF_VERSION,
        run_id=batch.run_id,
        manifest_sha256=batch.manifest_sha256,
        events_dry_run_receipt_id=batch.events_dry_run_receipt_id,
        gcam_dry_run_receipt_id=batch.gcam_dry_run_receipt_id,
        dry_run=False,
        inserted_counts=inserted,
        unchanged_counts=unchanged,
        conflict_counts=conflicts,
        readback_digests=tuple(digests),
        complete=True,
    )
