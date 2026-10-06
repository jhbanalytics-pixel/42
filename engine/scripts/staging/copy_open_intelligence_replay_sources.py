"""Staging-only dynamic replay source-copy runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import google.auth
from google.cloud import bigquery
from scripts.migrations import create_open_intelligence_replay_copy_infra as infra
from src.analysis.open_intelligence.pipeline import (
    ENRICHED_EVIDENCE_COLUMNS,
    EVENT_COLUMNS,
    SEED_CANDIDATE_COLUMNS,
    SEED_GRAPH_COLUMNS,
)

PROJECT = "ogilvy-trends-v2"
SOURCE_DATASET = "trends_v2_dev"
TARGET_DATASET = "trends_v2_staging"
LOCATION = "US"
SERVICE_ACCOUNT = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
COPY_CONTRACT_VERSION = "dynamic_replay_source_copy_v1"
COPY_RUN_ID = "dynamic_replay_source_copy_20260821_20260903_v1"
START_DATE = date(2026, 8, 21)
END_DATE = date(2026, 9, 3)
MARKETS = ("za", "ng", "ke")
# The four tables one copy run covers; the review procedure and the R3 apply read the same set.
SOURCE_COPY_TABLES = ("event_ledger", "seed_graph", "seed_candidates", "enriched_content")
ZERO_SOURCE_SET_DIGEST = "f69dade361b3d2c792f04091254f78752440ecde380c1caf69b5e01c0d6db0ee"
COVERAGE_DIGEST = "165144e87c2aea395920c8c85253cf00c6ec835b9da98058e8bdcd06f3ccc9d2"

_TYPE_MAP = {
    "STRING": "STRING",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "INT64": "INT64",
    "FLOAT64": "FLOAT64",
    "STRING_REPEATED": "ARRAY<STRING>",
}

EVENT_SPECS = (
    ("ledger_id", "STRING"),
    ("trend_date", "DATE"),
    ("market", "STRING"),
    ("entity_key", "STRING"),
    ("entity_aliases", "STRING_REPEATED"),
    ("event_kind", "STRING"),
    ("state_label", "STRING"),
    ("as_of", "TIMESTAMP"),
    ("corroborating_sources", "STRING_REPEATED"),
    ("source_count", "INT64"),
    ("confidence", "FLOAT64"),
)
GRAPH_SPECS = (
    ("market", "STRING"),
    ("term", "STRING"),
    ("term_type", "STRING"),
    ("platform", "STRING"),
    ("trend_date", "DATE"),
    ("event_date", "DATE"),
    ("row_count", "INT64"),
    ("topic_groups", "STRING_REPEATED"),
    ("near_topics", "STRING_REPEATED"),
    ("co_occur_terms", "STRING_REPEATED"),
    ("sample_row_ids", "STRING_REPEATED"),
)
CANDIDATE_SPECS = (
    ("candidate_id", "STRING"),
    ("proposed_date", "DATE"),
    ("market", "STRING"),
    ("candidate_type", "STRING"),
    ("candidate_value", "STRING"),
    ("source", "STRING"),
    ("lane", "STRING"),
    ("score", "FLOAT64"),
    ("safety_flags", "STRING_REPEATED"),
    ("evidence_topics", "STRING_REPEATED"),
    ("sample_row_ids", "STRING_REPEATED"),
    ("status", "STRING"),
)
ENRICHED_SPECS = tuple(
    (
        field,
        "STRING_REPEATED"
        if field == "topic_groups"
        else "TIMESTAMP"
        if field in {"published_at", "collected_at"}
        else "FLOAT64"
        if field
        in {
            "views",
            "likes",
            "comments",
            "shares",
            "engagement_total",
            "regional_score",
            "search_velocity_score",
            "tone_avg",
            "tone_polarity",
            "sentiment_lexicon_score",
        }
        else "STRING",
    )
    for field in ENRICHED_EVIDENCE_COLUMNS
)


class CopyRefusal(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TableContract:
    name: str
    date_field: str
    columns: tuple[str, ...]
    specs: tuple[tuple[str, str], ...]
    natural_key: tuple[str, ...]
    source_rows: int
    active_dates: int
    daily_maximum: int
    hard_ceiling: int
    zero_row_status: str
    filter_digest: str
    schema_digest: str
    source_set_digest: str
    coverage_digest: str | None = None


@dataclass(frozen=True, slots=True)
class CopyPlan:
    target: str
    project: str
    source_dataset: str
    target_dataset: str
    location: str
    service_account: str
    copy_run_id: str
    start_date: date
    end_date: date
    markets: tuple[str, ...]
    tables: Mapping[str, TableContract]
    infrastructure_query: str
    schema_query: str
    expected_schema_rows: tuple[Mapping[str, object], ...]
    target_query: str
    source_queries: Mapping[str, str]
    coverage_query: str
    apply_sql: str
    rollback_sql: str


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def chunked_digest(
    pairs: Sequence[tuple[str, str]], *, prefix: str = "source_set_v1_chunk1000"
) -> str:
    ordered = tuple(sorted(pairs))
    chunks = []
    for index in range(0, len(ordered), 1000):
        items = ordered[index : index + 1000]
        chunk_hash = sha256_text("\n".join(f"{row_id}:{row_hash}" for row_id, row_hash in items))
        chunks.append(f"{index // 1000}:{len(items)}:{chunk_hash}")
    return sha256_text(prefix + "\n" + "\n".join(chunks))


def _filter_payload(table: TableContract) -> dict[str, object]:
    if table.name == "enriched_content":
        return {
            "version": "filter_v1",
            "table": table.name,
            "date_expression": "DATE(collected_at)",
            "support_start_date": "2026-08-15",
            "support_end_date": "2026-09-03",
            "replay_start_date": "2026-08-21",
            "replay_end_date": "2026-09-03",
            "markets": ["za", "ng", "ke"],
            "requested_key_sources": [
                "seed_graph",
                "seed_candidates:applied,approved",
            ],
            "coverage_key": ["replay_date", "market", "sample_row_id"],
            "join_key": ["market", "id"],
            "coverage_rule": "exactly_one_in_replay_date_minus_6_through_replay_date",
            "dedupe_key": ["market", "id"],
        }
    return {
        "version": "filter_v1",
        "table": table.name,
        "date_expression": table.date_field,
        "start_date": "2026-08-21",
        "end_date": "2026-09-03",
        "markets": ["za", "ng", "ke"],
        "statuses": ["applied", "approved"] if table.name == "seed_candidates" else [],
    }


def filter_digest(table: TableContract) -> str:
    return sha256_text(json.dumps(_filter_payload(table), separators=(",", ":")))


def _schema_fields(table: TableContract) -> tuple[tuple[str, str, str], ...]:
    required = {
        "seed_graph": {"market", "term", "term_type", "platform", "trend_date"},
        "seed_candidates": {
            "candidate_id",
            "proposed_date",
            "market",
            "candidate_type",
            "candidate_value",
            "source",
            "lane",
            "status",
        },
        "enriched_content": {"id", "source", "platform", "market", "collected_at"},
    }.get(table.name, set())
    output = []
    for name, declared_type in table.specs:
        if declared_type == "STRING_REPEATED":
            output.append((name, "STRING", "REPEATED"))
        else:
            output.append((name, declared_type, "REQUIRED" if name in required else "NULLABLE"))
    return tuple(output)


def schema_digest(table: TableContract) -> str:
    payload = {
        "version": "schema_digest_v1",
        "table": table.name,
        "fields": [
            {"ordinal": ordinal, "name": name, "type": field_type, "mode": mode}
            for ordinal, (name, field_type, mode) in enumerate(_schema_fields(table), 1)
        ],
    }
    return sha256_text(json.dumps(payload, separators=(",", ":")))


def _table_contracts() -> Mapping[str, TableContract]:
    items = (
        TableContract(
            "event_ledger",
            "trend_date",
            EVENT_COLUMNS,
            EVENT_SPECS,
            ("trend_date", "market", "entity_key"),
            5158,
            14,
            395,
            350000,
            "nonzero",
            "afca4e6f2cc77d47b4ae02669339fd58cf8f73bb257ea745a6687a1ac13171cf",
            "8c06ee2effdd4f19b7f03b93a2b1670b92c70a988b33efea73e65e535e28afca",
            "c9dfd8dde82d86aaca29f125b6b43de0641ea08dedf2e2b3ab7d54d84fb5e465",
        ),
        TableContract(
            "seed_graph",
            "trend_date",
            SEED_GRAPH_COLUMNS,
            GRAPH_SPECS,
            ("market", "term", "term_type", "platform", "trend_date"),
            210000,
            14,
            15000,
            210000,
            "nonzero",
            "7905a08c82886d8187945672b99dfb1646060f9b8a21a5f00de56a5f5d095abe",
            "eff815e5b17f012874c71e2e3b0fb8a2e400d958d46147322edaa49e388a3e24",
            "418566b2043fcb748a4791f374068dc80f578e4d217d449fc36e079ce2332290",
        ),
        TableContract(
            "seed_candidates",
            "proposed_date",
            tuple(field for field, _type in CANDIDATE_SPECS),
            CANDIDATE_SPECS,
            ("candidate_id",),
            0,
            0,
            0,
            630,
            "verified_zero",
            "b5a45ad0c684dd5d78e61acdf9453b720fb83dcdfc798cc7da4943b2d0f663c6",
            "724b0197eb0217a334eff0131ecbd9d9578428b78a0034def31ed129ff060aee",
            ZERO_SOURCE_SET_DIGEST,
        ),
        TableContract(
            "enriched_content",
            "DATE(collected_at)",
            ENRICHED_EVIDENCE_COLUMNS,
            ENRICHED_SPECS,
            ("market", "id"),
            80845,
            14,
            6761,
            1053150,
            "nonzero",
            "cee006a9171d1b7250270a26555673285fa2c98cb455421c5642d32de34ff1aa",
            "81622075fc81012fc56e17c3df4019213f4ef628e6a44956136ef6a74d363a01",
            "a2bf50bef58581924060ee3dfa5402599b27837ea61a6ca785f1e232c8620cb3",
            "165144e87c2aea395920c8c85253cf00c6ec835b9da98058e8bdcd06f3ccc9d2",
        ),
    )
    return MappingProxyType({item.name: item for item in items})


def _cast_struct(specs: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(
        f"CAST({name} AS {_TYPE_MAP[field_type]}) AS {name}" for name, field_type in specs
    )


def _cast_struct_for(specs: tuple[tuple[str, str], ...], alias: str) -> str:
    return ", ".join(
        f"CAST({alias}.`{name}` AS {_TYPE_MAP[field_type]}) AS {name}" for name, field_type in specs
    )


def _filter(table: TableContract) -> str:
    date_filter = (
        "DATE(collected_at) BETWEEN DATE '2026-08-15' AND DATE '2026-09-03'"
        if table.name == "enriched_content"
        else f"{table.date_field} BETWEEN DATE '2026-08-21' AND DATE '2026-09-03'"
    )
    status = " AND status IN ('approved','applied')" if table.name == "seed_candidates" else ""
    return f"{date_filter} AND market IN ('za','ng','ke'){status}"


def _requested_keys_sql() -> str:
    return (
        "SELECT DISTINCT market,sample_id FROM ("
        f"SELECT market,sample_id FROM `{PROJECT}.{SOURCE_DATASET}.seed_graph`,UNNEST(sample_row_ids) sample_id WHERE trend_date BETWEEN DATE '2026-08-21' AND DATE '2026-09-03' AND market IN ('za','ng','ke') "
        "UNION ALL "
        f"SELECT market,sample_id FROM `{PROJECT}.{SOURCE_DATASET}.seed_candidates`,UNNEST(sample_row_ids) sample_id WHERE proposed_date BETWEEN DATE '2026-08-21' AND DATE '2026-09-03' AND market IN ('za','ng','ke') AND status IN ('approved','applied'))"
    )


def _source_relation(table: TableContract) -> str:
    source = f"`{PROJECT}.{SOURCE_DATASET}.{table.name}`"
    if table.name != "enriched_content":
        return f"(SELECT {_explicit_columns(table)} FROM {source} WHERE {_filter(table)})"
    return (
        f"(SELECT {_explicit_columns(table)} FROM {source} source_row WHERE DATE(collected_at) BETWEEN DATE '2026-08-15' AND DATE '2026-09-03' AND market IN ('za','ng','ke') "
        f"AND EXISTS (SELECT 1 FROM ({_requested_keys_sql()}) requested WHERE requested.market=source_row.market AND requested.sample_id=source_row.id))"
    )


def _audit_query(table: TableContract) -> str:
    key = _cast_struct(
        tuple(next(spec for spec in table.specs if spec[0] == name) for name in table.natural_key)
    )
    row = _cast_struct(table.specs)
    return (
        "SELECT table_name, source_rows, active_dates, daily_maximum, natural_keys, "
        "source_set_digest FROM (WITH canonical AS (SELECT "
        f"LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST('{table.name}' AS STRING) AS source_table, {key}))))) copy_row_id, "
        f"LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT({row}))))) row_hash, {table.date_field} audit_date "
        f"FROM {_source_relation(table)}), numbered AS (SELECT copy_row_id,row_hash,audit_date,ROW_NUMBER() OVER(ORDER BY copy_row_id) row_number FROM canonical), "
        "chunks AS (SELECT DIV(row_number-1,1000) chunk_index, COUNT(*) chunk_count, "
        "LOWER(TO_HEX(SHA256(STRING_AGG(copy_row_id||':'||row_hash,'\\n' ORDER BY copy_row_id)))) chunk_hash FROM numbered GROUP BY chunk_index), "
        f"summary AS (SELECT '{table.name}' table_name, (SELECT COUNT(*) FROM canonical) source_rows, "
        "(SELECT COUNT(DISTINCT audit_date) FROM canonical) active_dates, "
        "(SELECT COALESCE(MAX(n),0) FROM (SELECT audit_date,COUNT(*) n FROM canonical GROUP BY audit_date)) daily_maximum, "
        "(SELECT COUNT(DISTINCT copy_row_id) FROM canonical) natural_keys, "
        "LOWER(TO_HEX(SHA256('source_set_v1_chunk1000\\n'||COALESCE(STRING_AGG(CAST(chunk_index AS STRING)||':'||CAST(chunk_count AS STRING)||':'||chunk_hash,'\\n' ORDER BY chunk_index),'')))) source_set_digest "
        "FROM chunks) SELECT table_name,source_rows,active_dates,daily_maximum,natural_keys,source_set_digest FROM summary)"
    )


def _coverage_query() -> str:
    return (
        "SELECT request_instances,matched_once,missing,ambiguous,coverage_digest,raw_request_instances,raw_matched_once,raw_missing,raw_ambiguous FROM "
        "(WITH raw_requests AS (SELECT trend_date replay_date, market, sample_id FROM "
        f"`{PROJECT}.{SOURCE_DATASET}.seed_graph`, UNNEST(sample_row_ids) sample_id WHERE trend_date BETWEEN DATE '2026-08-21' AND DATE '2026-09-03' AND market IN ('za','ng','ke') UNION ALL "
        "SELECT proposed_date replay_date, market, sample_id FROM "
        f"`{PROJECT}.{SOURCE_DATASET}.seed_candidates`, UNNEST(sample_row_ids) sample_id WHERE proposed_date BETWEEN DATE '2026-08-21' AND DATE '2026-09-03' AND market IN ('za','ng','ke') AND status IN ('approved','applied')), "
        "raw_matched AS (SELECT replay_date,market,sample_id,(SELECT COUNT(*) FROM "
        f"`{PROJECT}.{SOURCE_DATASET}.enriched_content` evidence WHERE evidence.market=raw_requests.market AND evidence.id=raw_requests.sample_id AND DATE(evidence.collected_at) BETWEEN DATE_SUB(replay_date, INTERVAL 6 DAY) AND replay_date) match_count FROM raw_requests), "
        "requests AS (SELECT replay_date,market,sample_id,ANY_VALUE(match_count) match_count FROM raw_matched GROUP BY replay_date,market,sample_id), "
        "hashed AS (SELECT LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST(replay_date AS DATE) AS replay_date,CAST(market AS STRING) AS market,CAST(sample_id AS STRING) AS sample_row_id))))) copy_row_id,LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST(match_count AS INT64) AS match_count))))) row_hash,match_count FROM requests), "
        "numbered AS (SELECT copy_row_id,row_hash,match_count,ROW_NUMBER() OVER(ORDER BY copy_row_id) row_number FROM hashed), "
        "chunks AS (SELECT DIV(row_number-1,1000) chunk_index,COUNT(*) chunk_count,LOWER(TO_HEX(SHA256(STRING_AGG(copy_row_id||':'||row_hash,'\\n' ORDER BY copy_row_id)))) chunk_hash FROM numbered GROUP BY chunk_index), "
        "digest AS (SELECT LOWER(TO_HEX(SHA256('coverage_set_v1_chunk1000\\n'||COALESCE(STRING_AGG(CAST(chunk_index AS STRING)||':'||CAST(chunk_count AS STRING)||':'||chunk_hash,'\\n' ORDER BY chunk_index),'')))) coverage_digest FROM chunks) "
        "SELECT COUNT(*) request_instances,COUNTIF(match_count=1) matched_once,COUNTIF(match_count=0) missing,COUNTIF(match_count>1) ambiguous,(SELECT coverage_digest FROM digest) coverage_digest,(SELECT COUNT(*) FROM raw_matched) raw_request_instances,(SELECT COUNTIF(match_count=1) FROM raw_matched) raw_matched_once,(SELECT COUNTIF(match_count=0) FROM raw_matched) raw_missing,(SELECT COUNTIF(match_count>1) FROM raw_matched) raw_ambiguous FROM requests)"
    )


def _explicit_columns(table: TableContract) -> str:
    return ", ".join(f"`{field}`" if field == "rows" else field for field in table.columns)


def _natural_match(table: TableContract, left: str, right: str) -> str:
    return " AND ".join(f"{left}.{field}={right}.{field}" for field in table.natural_key)


def _copy_id_expr(table: TableContract, alias: str) -> str:
    key_specs = tuple(
        next(spec for spec in table.specs if spec[0] == name) for name in table.natural_key
    )
    return (
        "LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT("
        f"CAST('{table.name}' AS STRING) AS source_table,{_cast_struct_for(key_specs, alias)}"
        ")))))"
    )


def _content_hash_expr(table: TableContract, alias: str) -> str:
    return f"LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT({_cast_struct_for(table.specs, alias)})))))"


def _target_filter(table: TableContract) -> str:
    if table.name == "enriched_content":
        return (
            "DATE(target.collected_at) BETWEEN DATE '2026-08-15' AND DATE '2026-09-03' "
            f"AND target.market IN ('za','ng','ke') AND EXISTS (SELECT 1 FROM ({_requested_keys_sql()}) requested WHERE requested.market=target.market AND requested.sample_id=target.id)"
        )
    date_field = "proposed_date" if table.name == "seed_candidates" else "trend_date"
    status = (
        " AND target.status IN ('approved','applied')" if table.name == "seed_candidates" else ""
    )
    return f"target.{date_field} BETWEEN DATE '2026-08-21' AND DATE '2026-09-03' AND target.market IN ('za','ng','ke'){status}"


def _digest_select(temp_name: str) -> str:
    return (
        "WITH numbered AS (SELECT copy_row_id,source_content_sha256,ROW_NUMBER() OVER(ORDER BY copy_row_id) row_number FROM "
        f"{temp_name}), chunks AS (SELECT DIV(row_number-1,1000) chunk_index,COUNT(*) chunk_count,LOWER(TO_HEX(SHA256(STRING_AGG(copy_row_id||':'||source_content_sha256,'\\n' ORDER BY copy_row_id)))) chunk_hash FROM numbered GROUP BY chunk_index) "
        "SELECT LOWER(TO_HEX(SHA256('source_set_v1_chunk1000\\n'||COALESCE(STRING_AGG(CAST(chunk_index AS STRING)||':'||CAST(chunk_count AS STRING)||':'||chunk_hash,'\\n' ORDER BY chunk_index),'')))) FROM chunks"
    )


def _apply_sql(tables: Mapping[str, TableContract]) -> str:
    parts = [
        "DECLARE inserted_count INT64 DEFAULT 0;",
        "DECLARE unchanged_count INT64 DEFAULT 0;",
        "DECLARE conflict_count INT64 DEFAULT 0;",
        "DECLARE manifest_inserted_count INT64 DEFAULT 0;",
        "DECLARE manifest_unchanged_count INT64 DEFAULT 0;",
        "DECLARE manifest_total_count INT64 DEFAULT 0;",
        "DECLARE manifest_distinct_count INT64 DEFAULT 0;",
        "DECLARE receipt_inserted_count INT64 DEFAULT 0;",
        "DECLARE receipt_unchanged_count INT64 DEFAULT 0;",
        "DECLARE receipt_total_count INT64 DEFAULT 0;",
        "DECLARE receipt_distinct_count INT64 DEFAULT 0;",
        "DECLARE lock_version_before INT64;",
        "DECLARE lock_version_after INT64;",
        f"SET lock_version_before=(SELECT lock_version FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` WHERE copy_run_id='{COPY_RUN_ID}');",
        "BEGIN TRANSACTION;",
        f"UPDATE `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` SET lock_version=lock_version+1, updated_at=CURRENT_TIMESTAMP() WHERE copy_run_id='{COPY_RUN_ID}' AND copy_contract_version='{COPY_CONTRACT_VERSION}' AND state='ready';",
        "ASSERT @@row_count = 1 AS 'copy_lock_invalid';",
        f"SET lock_version_after=(SELECT lock_version FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` WHERE copy_run_id='{COPY_RUN_ID}');",
        f"CREATE TEMP TABLE coverage_audit AS {_coverage_query()};",
        f"ASSERT (SELECT request_instances={tables['enriched_content'].source_rows} AND matched_once={tables['enriched_content'].source_rows} AND missing=0 AND ambiguous=0 AND coverage_digest='{COVERAGE_DIGEST}' AND raw_request_instances>{tables['enriched_content'].source_rows} AND raw_matched_once=raw_request_instances AND raw_missing=0 AND raw_ambiguous=0 FROM coverage_audit) AS 'coverage_incomplete';",
    ]
    for table in tables.values():
        columns = _explicit_columns(table)
        target = f"`{PROJECT}.{TARGET_DATASET}.{table.name}`"
        target_alias_columns = ", ".join(f"src.{field}" for field in table.columns)
        key_struct = _cast_struct_for(
            tuple(
                next(spec for spec in table.specs if spec[0] == name) for name in table.natural_key
            ),
            "src",
        )
        row_struct = _cast_struct_for(table.specs, "src")
        parts.extend(
            (
                f"CREATE TEMP TABLE tmp_{table.name} AS SELECT {target_alias_columns}, LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT(CAST('{table.name}' AS STRING) AS source_table, {key_struct}))))) copy_row_id, LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT({row_struct}))))) source_content_sha256, NOT EXISTS (SELECT 1 FROM {target} target WHERE {_natural_match(table, 'target', 'src')}) was_absent FROM {_source_relation(table)} src;",
                f"ASSERT (SELECT COUNT(*) FROM tmp_{table.name}) = {table.source_rows} AS 'frozen_digest_mismatch';",
                f"ASSERT (SELECT COUNT(DISTINCT copy_row_id) FROM tmp_{table.name}) = {table.source_rows} AS 'source_key_duplicate';",
                f"ASSERT ({_digest_select(f'tmp_{table.name}')}) = '{table.source_set_digest}' AS 'frozen_digest_mismatch';",
                f"ASSERT NOT EXISTS (SELECT 1 FROM {target} target LEFT JOIN tmp_{table.name} src ON {_natural_match(table, 'target', 'src')} LEFT JOIN `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_manifest_v1` manifest ON manifest.copy_run_id='{COPY_RUN_ID}' AND manifest.target_table='{table.name}' AND manifest.copy_row_id=src.copy_row_id WHERE ({_target_filter(table)}) AND (src.copy_row_id IS NULL OR manifest.copy_row_id IS NULL OR manifest.source_content_sha256!=src.source_content_sha256 OR {_content_hash_expr(table, 'target')}!=src.source_content_sha256 OR {_copy_id_expr(table, 'target')}!=src.copy_row_id)) AS 'target_unowned_row';",
                f"ASSERT NOT EXISTS (SELECT 1 FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_manifest_v1` manifest LEFT JOIN tmp_{table.name} src ON src.copy_row_id=manifest.copy_row_id LEFT JOIN {target} target ON {_natural_match(table, 'target', 'src')} WHERE manifest.copy_run_id='{COPY_RUN_ID}' AND manifest.target_table='{table.name}' AND (src.copy_row_id IS NULL OR target.{table.natural_key[0]} IS NULL OR {_content_hash_expr(table, 'target')}!=manifest.source_content_sha256 OR {_copy_id_expr(table, 'target')}!=manifest.copy_row_id)) AS 'manifest_conflict';",
                f"SET unchanged_count=unchanged_count+(SELECT COUNTIF(NOT was_absent) FROM tmp_{table.name});",
                f"INSERT INTO {target} ({columns}) SELECT {columns} FROM tmp_{table.name} WHERE was_absent;",
                "SET inserted_count=inserted_count+@@row_count;",
                f"SET manifest_unchanged_count=manifest_unchanged_count+(SELECT COUNTIF(NOT was_absent) FROM tmp_{table.name});",
                f"INSERT INTO `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_manifest_v1` (copy_run_id,copy_contract_version,source_project,source_dataset,target_dataset,source_table,target_table,copy_row_id,source_content_sha256,window_start,window_end,inserted_at) SELECT '{COPY_RUN_ID}','{COPY_CONTRACT_VERSION}','{PROJECT}','{SOURCE_DATASET}','{TARGET_DATASET}','{table.name}','{table.name}',copy_row_id,source_content_sha256,DATE '2026-08-21',DATE '2026-09-03',CURRENT_TIMESTAMP() FROM tmp_{table.name} WHERE was_absent;",
                "SET manifest_inserted_count=manifest_inserted_count+@@row_count;",
            )
        )
    manifest_table = f"`{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_manifest_v1`"
    parts.extend(
        (
            f"SET manifest_total_count=(SELECT COUNT(*) FROM {manifest_table} WHERE copy_run_id='{COPY_RUN_ID}');",
            f"SET manifest_distinct_count=(SELECT COUNT(DISTINCT TO_JSON_STRING(STRUCT(copy_run_id,target_table,copy_row_id))) FROM {manifest_table} WHERE copy_run_id='{COPY_RUN_ID}');",
            "ASSERT manifest_total_count=296003 AS 'manifest_count_invalid';",
            "ASSERT manifest_distinct_count=296003 AS 'manifest_duplicate';",
            *(
                f"ASSERT (SELECT COUNT(*) FROM {manifest_table} WHERE copy_run_id='{COPY_RUN_ID}' AND target_table='{item.name}')={item.source_rows} AS 'manifest_count_invalid';"
                for item in tables.values()
            ),
        )
    )
    header_values = ",".join(
        f"STRUCT('{COPY_RUN_ID}' AS copy_run_id,'{COPY_CONTRACT_VERSION}' AS copy_contract_version,'{PROJECT}' AS source_project,'{SOURCE_DATASET}' AS source_dataset,'{TARGET_DATASET}' AS target_dataset,'{item.name}' AS source_table,'{item.name}' AS target_table,DATE '2026-08-21' AS window_start,DATE '2026-09-03' AS window_end,'{item.filter_digest}' AS filter_digest,'{item.schema_digest}' AS schema_digest,'{item.source_set_digest}' AS source_set_digest,{repr(item.coverage_digest) if item.coverage_digest else 'CAST(NULL AS STRING)'} AS coverage_digest,{item.source_rows} AS source_rows,'{item.zero_row_status}' AS zero_row_status,{item.hard_ceiling} AS hard_ceiling)"
        for item in tables.values()
    ).replace("'NULL'", "NULL")
    parts.extend(
        (
            f"CREATE TEMP TABLE expected_receipts AS SELECT receipt.* FROM UNNEST([{header_values}]) receipt;",
            f"ASSERT NOT EXISTS (SELECT 1 FROM (SELECT copy_run_id,copy_contract_version,source_project,source_dataset,target_dataset,source_table,target_table,window_start,window_end,filter_digest,schema_digest,source_set_digest,coverage_digest,source_rows,zero_row_status,hard_ceiling FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_receipts_v1` WHERE copy_run_id='{COPY_RUN_ID}') existing FULL OUTER JOIN expected_receipts expected USING(copy_run_id,source_table) WHERE expected.copy_run_id IS NULL OR (existing.copy_run_id IS NOT NULL AND TO_JSON_STRING(existing)!=TO_JSON_STRING(expected))) AS 'receipt_conflict';",
            f"SET receipt_unchanged_count=(SELECT COUNT(*) FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_receipts_v1` existing JOIN expected_receipts expected USING(copy_run_id,source_table));",
            f"INSERT INTO `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_receipts_v1` (copy_run_id,copy_contract_version,source_project,source_dataset,target_dataset,source_table,target_table,window_start,window_end,filter_digest,schema_digest,source_set_digest,coverage_digest,source_rows,zero_row_status,hard_ceiling) SELECT copy_run_id,copy_contract_version,source_project,source_dataset,target_dataset,source_table,target_table,window_start,window_end,filter_digest,schema_digest,source_set_digest,coverage_digest,source_rows,zero_row_status,hard_ceiling FROM expected_receipts expected WHERE NOT EXISTS (SELECT 1 FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_receipts_v1` existing WHERE existing.copy_run_id=expected.copy_run_id AND existing.source_table=expected.source_table);",
            "SET receipt_inserted_count=@@row_count;",
            f"SET receipt_total_count=(SELECT COUNT(*) FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_receipts_v1` WHERE copy_run_id='{COPY_RUN_ID}');",
            f"SET receipt_distinct_count=(SELECT COUNT(DISTINCT TO_JSON_STRING(STRUCT(copy_run_id,source_table))) FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_receipts_v1` WHERE copy_run_id='{COPY_RUN_ID}');",
            "ASSERT receipt_total_count=4 AS 'receipt_count_invalid';",
            "ASSERT receipt_distinct_count=4 AS 'receipt_duplicate';",
            "COMMIT TRANSACTION;",
            "SELECT inserted_count inserted,unchanged_count unchanged,conflict_count conflicts,manifest_inserted_count manifest_inserted,manifest_unchanged_count manifest_unchanged,receipt_inserted_count receipt_headers_inserted,receipt_unchanged_count receipt_headers_unchanged,lock_version_before,lock_version_after,NULL error_code;",
        )
    )
    return "\n".join(parts)


def _rollback_sql(tables: Mapping[str, TableContract]) -> str:
    parts = [
        "DECLARE deleted_count INT64 DEFAULT 0;",
        "DECLARE manifest_deleted_count INT64 DEFAULT 0;",
        "DECLARE conflict_count INT64 DEFAULT 0;",
        "DECLARE lock_version_before INT64;",
        "DECLARE lock_version_after INT64;",
        f"SET lock_version_before=(SELECT lock_version FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` WHERE copy_run_id='{COPY_RUN_ID}');",
        "BEGIN TRANSACTION;",
        f"UPDATE `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` SET lock_version=lock_version+1, updated_at=CURRENT_TIMESTAMP() WHERE copy_run_id='{COPY_RUN_ID}' AND copy_contract_version='{COPY_CONTRACT_VERSION}' AND state='ready';",
        "ASSERT @@row_count = 1 AS 'copy_lock_invalid';",
        f"SET lock_version_after=(SELECT lock_version FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` WHERE copy_run_id='{COPY_RUN_ID}');",
    ]
    running_total = 0
    for table in tables.values():
        running_total += table.source_rows
        target = f"`{PROJECT}.{TARGET_DATASET}.{table.name}`"
        manifest = f"`{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_manifest_v1`"
        parts.extend(
            (
                f"CREATE TEMP TABLE rollback_{table.name} AS SELECT {_explicit_columns(table)}, {_copy_id_expr(table, 'target')} copy_row_id, {_content_hash_expr(table, 'target')} source_content_sha256 FROM {target} target WHERE EXISTS (SELECT 1 FROM {manifest} owned WHERE owned.copy_run_id='{COPY_RUN_ID}' AND owned.target_table='{table.name}' AND owned.copy_row_id={_copy_id_expr(table, 'target')});",
                f"ASSERT (SELECT COUNT(*) FROM {manifest} WHERE copy_run_id='{COPY_RUN_ID}' AND target_table='{table.name}')={table.source_rows} AS 'manifest_invalid';",
                f"ASSERT (SELECT COUNT(*) FROM rollback_{table.name})={table.source_rows} AS 'rollback_target_missing';",
                f"ASSERT NOT EXISTS (SELECT 1 FROM {manifest} owned FULL OUTER JOIN rollback_{table.name} target USING(copy_row_id) WHERE owned.copy_run_id='{COPY_RUN_ID}' AND owned.target_table='{table.name}' AND (target.copy_row_id IS NULL OR owned.copy_row_id IS NULL OR owned.source_content_sha256!=target.source_content_sha256)) AS 'rollback_content_conflict';",
                f"DELETE FROM {target} target WHERE EXISTS (SELECT 1 FROM {manifest} owned WHERE owned.copy_run_id='{COPY_RUN_ID}' AND owned.target_table='{table.name}' AND owned.copy_row_id={_copy_id_expr(table, 'target')} AND owned.source_content_sha256={_content_hash_expr(table, 'target')});",
                "SET deleted_count=deleted_count+@@row_count;",
                f"ASSERT deleted_count={running_total} AS 'rollback_delete_mismatch';",
            )
        )
    parts.extend(
        (
            f"DELETE FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_manifest_v1` WHERE copy_run_id='{COPY_RUN_ID}';",
            "SET manifest_deleted_count=@@row_count;",
            "ASSERT manifest_deleted_count=deleted_count AS 'rollback_manifest_mismatch';",
            "COMMIT TRANSACTION;",
            "SELECT deleted_count deleted,manifest_deleted_count manifest_deleted,conflict_count conflicts,lock_version_before,lock_version_after,NULL error_code;",
        )
    )
    return "\n".join(parts)


def _infrastructure_query() -> str:
    return (
        "SELECT control_table_count=3 AND total_lock_rows=1 AND matching_lock_rows=1 infrastructure_ready,control_table_count,total_lock_rows,matching_lock_rows,lock_version,lock_contract_version,lock_state FROM (SELECT "
        f"(SELECT COUNT(*) FROM `{PROJECT}.{TARGET_DATASET}.INFORMATION_SCHEMA.TABLES` WHERE table_name IN ('open_intelligence_source_copy_lock_v1','open_intelligence_source_copy_manifest_v1','open_intelligence_source_copy_receipts_v1')) control_table_count,"
        f"(SELECT COUNT(*) FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1`) total_lock_rows,"
        f"(SELECT COUNT(*) FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` WHERE copy_run_id=@copy_run_id) matching_lock_rows,"
        f"(SELECT MAX(lock_version) FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` WHERE copy_run_id=@copy_run_id) lock_version,"
        f"(SELECT ANY_VALUE(copy_contract_version) FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` WHERE copy_run_id=@copy_run_id) lock_contract_version,"
        f"(SELECT ANY_VALUE(state) FROM `{PROJECT}.{TARGET_DATASET}.open_intelligence_source_copy_lock_v1` WHERE copy_run_id=@copy_run_id) lock_state)"
    )


def _expected_schema_rows(tables: Mapping[str, TableContract]) -> tuple[Mapping[str, object], ...]:
    rows: list[Mapping[str, object]] = []
    for side in ("source", "target"):
        for table in tables.values():
            for ordinal, (name, field_type, mode) in enumerate(_schema_fields(table), 1):
                rows.append(
                    {
                        "side": side,
                        "table_name": table.name,
                        "ordinal": ordinal,
                        "field_name": name,
                        "field_type": field_type,
                        "field_mode": mode,
                    }
                )
    controls = {
        "open_intelligence_source_copy_lock_v1": infra.LOCK_SCHEMA,
        "open_intelligence_source_copy_manifest_v1": infra.MANIFEST_SCHEMA,
        "open_intelligence_source_copy_receipts_v1": infra.RECEIPT_SCHEMA,
    }
    for table_name, schema in controls.items():
        for ordinal, (name, field_type, mode) in enumerate(schema, 1):
            rows.append(
                {
                    "side": "control",
                    "table_name": table_name,
                    "ordinal": ordinal,
                    "field_name": name,
                    "field_type": field_type,
                    "field_mode": mode,
                }
            )
    return tuple(rows)


def _schema_query() -> str:
    tables = ",".join(f"'{table}'" for table in SOURCE_COPY_TABLES)
    controls = "'open_intelligence_source_copy_lock_v1','open_intelligence_source_copy_manifest_v1','open_intelligence_source_copy_receipts_v1'"
    return (
        "SELECT side,table_name,ordinal_position ordinal,column_name field_name,"
        "IF(STARTS_WITH(data_type,'ARRAY<'),'STRING',data_type) field_type,"
        "IF(STARTS_WITH(data_type,'ARRAY<'),'REPEATED',IF(is_nullable='NO','REQUIRED','NULLABLE')) field_mode FROM ("
        f"SELECT 'source' side,* FROM `{PROJECT}.{SOURCE_DATASET}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name IN ({tables}) UNION ALL "
        f"SELECT 'target' side,* FROM `{PROJECT}.{TARGET_DATASET}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name IN ({tables}) UNION ALL "
        f"SELECT 'control' side,* FROM `{PROJECT}.{TARGET_DATASET}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name IN ({controls})) "
        "ORDER BY CASE side WHEN 'source' THEN 1 WHEN 'target' THEN 2 ELSE 3 END,"
        "CASE table_name WHEN 'event_ledger' THEN 1 WHEN 'seed_graph' THEN 2 WHEN 'seed_candidates' THEN 3 WHEN 'enriched_content' THEN 4 WHEN 'open_intelligence_source_copy_lock_v1' THEN 5 WHEN 'open_intelligence_source_copy_manifest_v1' THEN 6 ELSE 7 END,ordinal"
    )


def _target_query() -> str:
    tables = SOURCE_COPY_TABLES
    parts = [
        f"SELECT '{table}' table_name,COUNT(*) target_rows FROM `{PROJECT}.{TARGET_DATASET}.{table}`"
        for table in tables
    ]
    return (
        "SELECT table_name,target_rows FROM (" + " UNION ALL ".join(parts) + ") ORDER BY table_name"
    )


def build_plan(target: str, start_date: date, end_date: date) -> CopyPlan:
    if target != "staging":
        raise CopyRefusal("target_invalid")
    if start_date != START_DATE or end_date != END_DATE:
        raise CopyRefusal("window_invalid")
    tables = _table_contracts()
    source_queries = MappingProxyType({name: _audit_query(item) for name, item in tables.items()})
    plan = CopyPlan(
        target,
        PROJECT,
        SOURCE_DATASET,
        TARGET_DATASET,
        LOCATION,
        SERVICE_ACCOUNT,
        COPY_RUN_ID,
        start_date,
        end_date,
        MARKETS,
        tables,
        _infrastructure_query(),
        _schema_query(),
        _expected_schema_rows(tables),
        _target_query(),
        source_queries,
        _coverage_query(),
        _apply_sql(tables),
        _rollback_sql(tables),
    )
    validate_apply_sql(plan.apply_sql)
    return plan


def validate_apply_sql(sql: str) -> None:
    required_counts = {
        "copy_lock_invalid": 1,
        "safety_flags": 10,
        "open_intelligence_source_copy_manifest_v1": 18,
        "open_intelligence_source_copy_receipts_v1": 6,
        "source_content_sha256": 36,
        "BEGIN TRANSACTION": 1,
        "COMMIT TRANSACTION": 1,
        "manifest_duplicate": 1,
        "receipt_duplicate": 1,
    }
    if any(sql.count(fragment) != count for fragment, count in required_counts.items()):
        raise CopyRefusal("sql_invalid")
    if "SELECT *" in sql or "TRUNCATE" in sql or "DELETE " in sql or sql.count("UPDATE ") != 1:
        raise CopyRefusal("sql_invalid")
    if f"`{PROJECT}.trends_v2`" in sql:
        raise CopyRefusal("sql_invalid")


def _validate_plan(plan: CopyPlan) -> None:
    if not isinstance(plan, CopyPlan) or plan != build_plan(
        plan.target, plan.start_date, plan.end_date
    ):
        raise CopyRefusal("plan_invalid")


def _one(job: Any) -> Mapping[str, Any]:
    rows = tuple(job.result())
    if len(rows) != 1:
        raise CopyRefusal("source_snapshot_conflict")
    row = rows[0]
    if isinstance(row, Mapping):
        return row
    try:
        normalized = {}
        for key in tuple(row.keys()):
            normalized[key] = row[key]
        return normalized
    except (AttributeError, KeyError, TypeError):
        raise CopyRefusal("source_snapshot_conflict") from None


def _validate_schema_rows(
    actual_rows: Sequence[Mapping[str, object]],
    expected_rows: Sequence[Mapping[str, object]],
) -> None:
    aliases = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}

    def key(row: Mapping[str, object]) -> tuple[str, str, str]:
        return (str(row["side"]), str(row["table_name"]), str(row["field_name"]))

    actual_by_key: dict[tuple[str, str, str], Mapping[str, object]] = {}
    for row in actual_rows:
        row_key = key(row)
        if row_key in actual_by_key:
            raise CopyRefusal("schema_mismatch")
        actual_by_key[row_key] = row

    expected_by_key = {key(row): row for row in expected_rows}
    if len(expected_by_key) != len(expected_rows):
        raise CopyRefusal("schema_mismatch")

    for row_key, expected in expected_by_key.items():
        actual = actual_by_key.get(row_key)
        if actual is None:
            raise CopyRefusal("schema_mismatch")
        actual_type = aliases.get(str(actual["field_type"]), str(actual["field_type"]))
        if actual_type != expected["field_type"] or actual["field_mode"] != expected["field_mode"]:
            raise CopyRefusal("schema_mismatch")
        if expected["side"] == "control" and actual["ordinal"] != expected["ordinal"]:
            raise CopyRefusal("schema_mismatch")

    expected_control = {row_key for row_key in expected_by_key if row_key[0] == "control"}
    actual_control = {row_key for row_key in actual_by_key if row_key[0] == "control"}
    if actual_control != expected_control:
        raise CopyRefusal("schema_mismatch")


def execute_plan(plan: CopyPlan, *, apply: bool, client: Any | None = None) -> dict[str, object]:
    _validate_plan(plan)
    if client is None:
        client = _default_client(plan)
    parameter_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("copy_run_id", "STRING", plan.copy_run_id)]
    )
    infra_state = _one(
        client.query(
            plan.infrastructure_query,
            location=plan.location,
            job_config=parameter_config,
        )
    )
    if infra_state.get("control_table_count") != 3:
        raise CopyRefusal("infrastructure_missing")
    if infra_state.get("matching_lock_rows") != 1 or not isinstance(
        infra_state.get("lock_version"), int
    ):
        raise CopyRefusal("lock_missing")
    if infra_state.get("total_lock_rows") != 1:
        raise CopyRefusal("lock_conflict")
    if (
        infra_state.get("lock_contract_version") != COPY_CONTRACT_VERSION
        or infra_state.get("lock_state") != "ready"
    ):
        raise CopyRefusal("lock_conflict")
    if any(filter_digest(item) != item.filter_digest for item in plan.tables.values()):
        raise CopyRefusal("frozen_digest_mismatch")
    if any(schema_digest(item) != item.schema_digest for item in plan.tables.values()):
        raise CopyRefusal("schema_mismatch")
    actual_schema = tuple(
        dict(row) for row in client.query(plan.schema_query, location=plan.location).result()
    )
    _validate_schema_rows(actual_schema, plan.expected_schema_rows)
    table_receipts = []
    target_rows = {
        row["table_name"]: row["target_rows"]
        for row in client.query(plan.target_query, location=plan.location).result()
    }
    if set(target_rows) != set(plan.tables):
        raise CopyRefusal("infrastructure_schema_mismatch")
    for name, query in plan.source_queries.items():
        actual = _one(client.query(query, location=plan.location))
        expected = plan.tables[name]
        if (
            actual.get("source_rows") != expected.source_rows
            or actual.get("natural_keys") != expected.source_rows
            or actual.get("source_set_digest") != expected.source_set_digest
            or actual.get("active_dates") != expected.active_dates
            or actual.get("daily_maximum") != expected.daily_maximum
            or target_rows[name] not in (0, expected.source_rows)
        ):
            raise CopyRefusal("frozen_digest_mismatch")
        table_receipts.append(
            {
                "table": name,
                "source_rows": expected.source_rows,
                "active_dates": expected.active_dates,
                "daily_maximum": expected.daily_maximum,
                "hard_ceiling": expected.hard_ceiling,
                "source_set_digest": expected.source_set_digest,
                "target_rows_before": target_rows[name],
            }
        )
    coverage = _one(client.query(plan.coverage_query, location=plan.location))
    enriched = plan.tables["enriched_content"]
    if (
        coverage.get("request_instances") != enriched.source_rows
        or coverage.get("matched_once") != enriched.source_rows
        or coverage.get("missing") != 0
        or coverage.get("ambiguous") != 0
        or coverage.get("coverage_digest") != enriched.coverage_digest
        or not isinstance(coverage.get("raw_request_instances"), int)
        or coverage.get("raw_request_instances") <= enriched.source_rows
        or coverage.get("raw_matched_once") != coverage.get("raw_request_instances")
        or coverage.get("raw_missing") != 0
        or coverage.get("raw_ambiguous") != 0
    ):
        raise CopyRefusal("coverage_incomplete")
    base = {
        "target": plan.target,
        "copy_run_id": plan.copy_run_id,
        "copy_contract_version": COPY_CONTRACT_VERSION,
        "project": plan.project,
        "source_dataset": plan.source_dataset,
        "target_dataset": plan.target_dataset,
        "location": plan.location,
        "window_start": plan.start_date.isoformat(),
        "window_end": plan.end_date.isoformat(),
        "markets": list(plan.markets),
        "complete_partitions": True,
        "tables": table_receipts,
        "total_source_rows": sum(item.source_rows for item in plan.tables.values()),
        "total_conflicts": 0,
        "cleanup_state": "not_started" if not apply else "complete",
        "rollback_sql_digest": sha256_text(plan.rollback_sql),
        "rollback_sql": plan.rollback_sql,
    }
    if not apply:
        return {
            **base,
            "mode": "dry-run",
            "total_inserted": 0,
            "total_unchanged": 0,
            "manifest_inserted": 0,
            "manifest_unchanged": 0,
            "receipt_headers_inserted": 0,
            "receipt_headers_unchanged": 0,
        }
    receipt = _one(client.query(plan.apply_sql, location=plan.location))
    error_code = receipt.get("error_code")
    if error_code:
        raise CopyRefusal(str(error_code))
    if (
        receipt.get("inserted", 0) + receipt.get("unchanged", 0) != 296003
        or receipt.get("conflicts") != 0
        or receipt.get("manifest_inserted", 0) + receipt.get("manifest_unchanged", 0) != 296003
        or receipt.get("receipt_headers_inserted", 0) + receipt.get("receipt_headers_unchanged", 0)
        != 4
        or receipt.get("lock_version_after") != receipt.get("lock_version_before") + 1
    ):
        raise CopyRefusal("transaction_failed")
    return {
        **base,
        "mode": "apply",
        "total_inserted": receipt["inserted"],
        "total_unchanged": receipt["unchanged"],
        "manifest_inserted": receipt["manifest_inserted"],
        "manifest_unchanged": receipt["manifest_unchanged"],
        "receipt_headers_inserted": receipt["receipt_headers_inserted"],
        "receipt_headers_unchanged": receipt["receipt_headers_unchanged"],
        "lock_version_before": receipt["lock_version_before"],
        "lock_version_after": receipt["lock_version_after"],
    }


def execute_rollback_plan(plan: CopyPlan, *, client: Any | None = None) -> dict[str, int]:
    _validate_plan(plan)
    if client is None:
        client = _default_client(plan)
    receipt = _one(client.query(plan.rollback_sql, location=plan.location))
    error_code = receipt.get("error_code")
    if error_code:
        raise CopyRefusal(str(error_code))
    if (
        receipt.get("deleted") != 296003
        or receipt.get("manifest_deleted") != 296003
        or receipt.get("conflicts") != 0
        or receipt.get("lock_version_after") != receipt.get("lock_version_before") + 1
    ):
        raise CopyRefusal("rollback_conflict")
    return {
        "deleted": receipt["deleted"],
        "manifest_deleted": receipt["manifest_deleted"],
        "lock_version_before": receipt["lock_version_before"],
        "lock_version_after": receipt["lock_version_after"],
    }


def _default_client(plan: CopyPlan) -> Any:
    credentials, adc_project = google.auth.default()
    identity = getattr(credentials, "service_account_email", None)
    if adc_project != plan.project or identity != plan.service_account:
        raise CopyRefusal("runtime_identity_invalid")
    return bigquery.Client(project=plan.project, credentials=credentials, location=plan.location)


def parser() -> argparse.ArgumentParser:
    output = argparse.ArgumentParser()
    output.add_argument("--target", required=True, choices=("staging",))
    output.add_argument("--start-date", required=True, type=date.fromisoformat)
    output.add_argument("--end-date", required=True, type=date.fromisoformat)
    output.add_argument("--apply", action="store_true")
    return output


def main(argv: Sequence[str] | None = None, *, client: Any | None = None) -> int:
    args = parser().parse_args(argv)
    result = execute_plan(
        build_plan(args.target, args.start_date, args.end_date),
        apply=args.apply,
        client=client,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
