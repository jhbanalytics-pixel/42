"""Bounded released-source material assembly, without authority issuance."""

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from google.cloud import bigquery

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.brain_live_reader import (
    RELATION_FIELDS,
    SOURCE_COPY_RECEIPT_FIELDS,
    TARGET_DATASET,
    TARGET_PROJECT,
    _mapping,
    _source_copy_authority,
    _validate_complete_run_rows,
    _validate_future,
)
from src.analysis.open_intelligence.general_question_request import validate_question_request
from src.analysis.open_intelligence.persistence import (
    NATURAL_KEYS,
    TABLE_BINDINGS,
    BatchInvalid,
    _canonicalize_row,
    _canonicalize_value,
    _versioned_fields,
    row_set_digest_sql,
)
from src.analysis.open_intelligence.run_receipts import RUN_RECEIPT_ROW_FIELDS, build_run_receipt

FAMILY_RELATIONS = {**TABLE_BINDINGS, "analysis": "signal_analysis_v2"}
CONTENT_RELATIONS = FAMILY_RELATIONS
_SCOPE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
)
_WIRE_FIELDS = {"kind", "run_id", "signal_id", "market", "payload_json", "payload_sha256"}
_DATE_FIELDS = {
    "signal_date",
    "observation_start",
    "observation_end",
    "window_start",
    "window_end",
    "evaluation_date",
}
_TIME_FIELDS = {
    "created_at",
    "published_at",
    "completed_at",
    "analyzed_at",
    "first_seen_at",
    "predicted_at",
    "evaluated_at",
    "human_reviewed_at",
}
_META_LIMIT = 64


def _relation(name):
    return f"`{TARGET_PROJECT}.{TARGET_DATASET}.{name}`"


def _struct(alias, fields):
    return "STRUCT(" + ", ".join(f"{alias}.{field} AS {field}" for field in fields) + ")"


def _sql():
    unions = []
    for table, relation in CONTENT_RELATIONS.items():
        field_names = RELATION_FIELDS[relation]
        fields = ", ".join(f"t.{field} AS {field}" for field in field_names)
        signal = "t.signal_id" if "signal_id" in field_names else "CAST(NULL AS STRING)"
        if table == "membership":
            fields += ", JSON_VALUE(TO_JSON_STRING(t), '$.source_provenance_json') AS source_provenance_json"
        unions.append(
            f"SELECT '{table}' AS kind, t.run_id, {signal} AS signal_id, t.market, "
            f"TO_JSON_STRING(STRUCT({fields})) AS payload_json "
            f"FROM {_relation(relation)} AS t JOIN receipts AS rr ON t.run_id = rr.run_id "
            "WHERE rr.position = 1 AND t.client_scope_id = @client_scope_id "
            "AND t.market IN UNNEST(@allowed_markets) "
            "AND t.signal_date = rr.signal_date AND t.contract_version = '2.0.0'"
            + (" AND t.evaluated_at <= rr.completed_at" if table == "outcomes" else "")
        )
    counts = ", ".join(
        f"(SELECT COUNT(*) FROM {_relation(relation)} AS f WHERE f.run_id = r.run_id "
        "AND f.client_scope_id = @client_scope_id AND f.signal_date = rr.signal_date "
        "AND f.contract_version = '2.0.0'"
        + (" AND f.evaluated_at <= rr.completed_at" if family == "outcomes" else "")
        + f") AS {family}"
        for family, relation in FAMILY_RELATIONS.items()
    )
    copy_where = "x.window_start = rr.observation_start AND x.window_end = rr.observation_end"
    schema_tables = ", ".join("'" + name + "'" for name in RELATION_FIELDS)
    digest = (
        "CASE rr.cluster_build_version "
        + " ".join(
            f"WHEN '{version}' THEN "
            + row_set_digest_sql("r.run_id", sql_expression=True, cluster_build_version=version)
            for version in ("hybrid_graph_v1", "hybrid_graph_v2", "hybrid_graph_v3")
        )
        + " ELSE ERROR('source_material_version_invalid') END"
    )
    receipt_struct = _struct("rr", RUN_RECEIPT_ROW_FIELDS)
    copy_struct = ", ".join(f"x.{field}" for field in SOURCE_COPY_RECEIPT_FIELDS)
    return f"""WITH selected AS (
SELECT @run_ids[OFFSET(i)] AS run_id, @signal_ids[OFFSET(i)] AS signal_id,
       @selected_markets[OFFSET(i)] AS market
FROM UNNEST(GENERATE_ARRAY(0, ARRAY_LENGTH(@run_ids) - 1)) AS i
), runs AS (SELECT DISTINCT run_id FROM selected),
receipts AS (
SELECT rr.*, ROW_NUMBER() OVER(PARTITION BY rr.run_id ORDER BY TO_JSON_STRING(rr)) AS position
FROM {_relation("open_intelligence_run_receipts_v1")} AS rr
JOIN runs USING(run_id) WHERE rr.client_scope_id = @client_scope_id
), manifest AS (
SELECT copy_run_id, target_table, window_start, window_end, COUNT(*) AS manifest_rows
FROM {_relation("open_intelligence_source_copy_manifest_v1")}
GROUP BY copy_run_id, target_table, window_start, window_end
), schema_rows AS (
SELECT table_name, column_name, data_type, is_nullable, ordinal_position
FROM `{TARGET_PROJECT}.{TARGET_DATASET}.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ({schema_tables})
), all_content AS (
{" UNION ALL ".join(unions)}
), bounded_content AS (
SELECT kind, run_id, signal_id, market, payload_json FROM all_content
ORDER BY kind, run_id, signal_id, market, payload_json LIMIT @candidate_limit
), run_material AS (
SELECT 'run_metadata' AS kind, r.run_id, CAST(NULL AS STRING) AS signal_id,
       CAST(NULL AS STRING) AS market,
       TO_JSON_STRING(STRUCT(
         (SELECT COUNT(*) FROM {_relation("open_intelligence_run_receipts_v1")} AS all_receipts
          WHERE all_receipts.run_id = r.run_id) AS receipt_count,
         IF(rr.run_id IS NULL, NULL, TO_JSON_STRING({receipt_struct})) AS receipt_json,
         {digest} AS row_set_digest,
         STRUCT({counts}) AS family_counts,
         (SELECT COUNT(*) FROM manifest AS x WHERE {copy_where}) AS manifest_count,
         (SELECT COUNT(*) FROM {_relation("open_intelligence_source_copy_receipts_v1")} AS x
          WHERE {copy_where}) AS copy_receipt_count,
         ARRAY(SELECT AS STRUCT x.* FROM manifest AS x WHERE {copy_where}
               ORDER BY copy_run_id, target_table LIMIT {_META_LIMIT + 1}) AS manifest,
         ARRAY(SELECT AS STRUCT {copy_struct}
               FROM {_relation("open_intelligence_source_copy_receipts_v1")} AS x WHERE {copy_where}
               ORDER BY copy_run_id, source_table LIMIT {_META_LIMIT + 1}) AS copy_receipts
       )) AS payload_json
FROM runs AS r LEFT JOIN receipts AS rr ON rr.run_id = r.run_id AND rr.position = 1
), request_material AS (
SELECT 'request_metadata' AS kind, CAST(NULL AS STRING) AS run_id,
       CAST(NULL AS STRING) AS signal_id, CAST(NULL AS STRING) AS market,
       TO_JSON_STRING(STRUCT(
         @request_digest AS request_digest,
         (SELECT COUNT(*) FROM bounded_content) AS candidate_count,
         (SELECT COUNT(*) FROM all_content) AS available_content_rows,
         (SELECT COUNT(*) FROM all_content) - (SELECT COUNT(*) FROM bounded_content) AS overflow_count,
         (SELECT COUNT(*) FROM bounded_content WHERE kind = 'evidence') AS evidence_count,
         (SELECT COUNT(*) FROM all_content WHERE kind = 'evidence') AS available_evidence_rows,
         ARRAY(SELECT AS STRUCT * FROM schema_rows ORDER BY table_name, ordinal_position LIMIT 1001) AS schema_rows,
         (SELECT COUNT(*) FROM schema_rows) AS schema_row_count
       )) AS payload_json
), material AS (
SELECT * FROM request_material UNION ALL SELECT * FROM run_material UNION ALL SELECT * FROM bounded_content
)
SELECT *, LOWER(TO_HEX(SHA256(payload_json))) AS payload_sha256
FROM material ORDER BY kind, run_id, signal_id, market, payload_json"""


@dataclass(frozen=True)
class PreparedSourceQuery:
    template_id: str
    sql: str
    parameters: tuple
    sql_digest: str
    parameters_digest: str
    candidate_limit: int
    transport_row_limit: int
    request: dict
    selected: tuple
    evidence_limit: int
    plan_window: dict


def build_selected_source_query(
    request, selected, *, plan_window, candidate_limit, evidence_limit=200
):
    if not isinstance(request, dict):
        raise ValueError("source_query_invalid")
    normalized = validate_question_request(
        request,
        scope={key: request.get(key) for key in _SCOPE_FIELDS},
        policy_digest=request.get("policy_digest"),
    )
    try:
        if type(plan_window) is not dict or set(plan_window) != {"start", "end", "closed"}:
            raise ValueError()
        window_start = date.fromisoformat(plan_window["start"])
        window_end = date.fromisoformat(plan_window["end"])
        as_of = datetime.fromisoformat(normalized["as_of"])
        if (
            type(plan_window["closed"]) is not bool
            or window_start > window_end
            or (window_end - window_start).days >= 366
            or window_end > as_of.date()
        ):
            raise ValueError()
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("source_query_invalid") from error
    if (
        type(candidate_limit) is not int
        or not 1 <= candidate_limit <= 1000
        or type(evidence_limit) is not int
        or not 0 <= evidence_limit <= 200
        or type(selected) not in (list, tuple)
        or not 1 <= len(selected) <= candidate_limit
    ):
        raise ValueError("source_query_invalid")
    identities = []
    for identity in selected:
        if type(identity) is not dict or set(identity) != {"run_id", "signal_id", "market"}:
            raise ValueError("source_query_invalid")
        if any(
            type(value) is not str or not value.strip() or len(value) > 256
            for value in identity.values()
        ):
            raise ValueError("source_query_invalid")
        if identity["market"] not in normalized["market_scope"]:
            raise ValueError("source_query_invalid")
        identities.append((identity["run_id"], identity["signal_id"], identity["market"]))
    if len(set(identities)) != len(identities):
        raise ValueError("source_query_invalid")
    identities.sort()
    params = (
        bigquery.ScalarQueryParameter("client_scope_id", "STRING", normalized["client_scope_id"]),
        bigquery.ArrayQueryParameter("run_ids", "STRING", [item[0] for item in identities]),
        bigquery.ArrayQueryParameter("signal_ids", "STRING", [item[1] for item in identities]),
        bigquery.ArrayQueryParameter(
            "selected_markets", "STRING", [item[2] for item in identities]
        ),
        bigquery.ScalarQueryParameter("request_digest", "STRING", normalized["request_digest"]),
        bigquery.ScalarQueryParameter("candidate_limit", "INT64", candidate_limit),
        bigquery.ScalarQueryParameter("evidence_limit", "INT64", evidence_limit),
        bigquery.ArrayQueryParameter("allowed_markets", "STRING", normalized["market_scope"]),
    )
    sql = _sql()
    return PreparedSourceQuery(
        "released_evidence_v1",
        sql,
        params,
        hashlib.sha256(sql.encode()).hexdigest(),
        canonical_digest([param.to_api_repr() for param in params]),
        candidate_limit,
        candidate_limit + len({item[0] for item in identities}) + 1,
        normalized,
        tuple(identities),
        evidence_limit,
        copy.deepcopy(plan_window),
    )


def _json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("source_material_invalid")
            result[key] = item
        return result

    if type(value) is not str:
        raise ValueError("source_material_invalid")
    result = json.loads(value, object_pairs_hook=unique)
    if type(result) is not dict:
        raise ValueError("source_material_invalid")
    return result


def _dates(row):
    result = dict(row)
    for key in _DATE_FIELDS:
        if result.get(key) is not None:
            result[key] = date.fromisoformat(result[key])
    for key in _TIME_FIELDS:
        if result.get(key) is not None:
            result[key] = datetime.fromisoformat(result[key])
    return result


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("source_material_invalid")
    return value


def decode_selected_source_rows(rows, *, query):
    try:
        return _decode(rows, query)
    except (ValueError, TypeError, KeyError, RecursionError, BatchInvalid) as error:
        raise ValueError("source_material_invalid") from error


def _decode(rows, query):
    if type(query) is not PreparedSourceQuery or type(rows) not in (list, tuple):
        raise ValueError("source_material_invalid")
    rebuilt = build_selected_source_query(
        query.request,
        [
            dict(zip(("run_id", "signal_id", "market"), item, strict=True))
            for item in query.selected
        ],
        plan_window=query.plan_window,
        candidate_limit=query.candidate_limit,
        evidence_limit=query.evidence_limit,
    )
    if (
        query.sql != rebuilt.sql
        or query.sql_digest != rebuilt.sql_digest
        or query.template_id != rebuilt.template_id
        or query.transport_row_limit != rebuilt.transport_row_limit
        or query.parameters_digest != canonical_digest([p.to_api_repr() for p in query.parameters])
        or query.parameters_digest != rebuilt.parameters_digest
        or len(rows) > query.transport_row_limit
    ):
        raise ValueError("source_material_invalid")
    summary = None
    metadata = {}
    content = []
    for wire in rows:
        wire = dict(wire)
        if (
            set(wire) != _WIRE_FIELDS
            or type(wire["payload_json"]) is not str
            or wire["payload_sha256"] != hashlib.sha256(wire["payload_json"].encode()).hexdigest()
        ):
            raise ValueError("source_material_invalid")
        value = _json(wire["payload_json"])
        if wire["kind"] == "request_metadata":
            if summary is not None or any(
                wire[key] is not None for key in ("run_id", "signal_id", "market")
            ):
                raise ValueError("source_material_invalid")
            summary = value
        elif wire["kind"] == "run_metadata":
            if (
                wire["run_id"] in metadata
                or wire["signal_id"] is not None
                or wire["market"] is not None
            ):
                raise ValueError("source_material_invalid")
            metadata[wire["run_id"]] = value
        elif wire["kind"] in CONTENT_RELATIONS:
            if wire["run_id"] not in {item[0] for item in query.selected}:
                raise ValueError("source_material_invalid")
            content.append((wire, value))
        else:
            raise ValueError("source_material_invalid")
    if summary is None or set(metadata) != {item[0] for item in query.selected}:
        raise ValueError("source_material_invalid")
    if set(summary) != {
        "request_digest",
        "candidate_count",
        "available_content_rows",
        "overflow_count",
        "evidence_count",
        "available_evidence_rows",
        "schema_rows",
        "schema_row_count",
    }:
        raise ValueError("source_material_invalid")
    for field in (
        "candidate_count",
        "available_content_rows",
        "overflow_count",
        "evidence_count",
        "available_evidence_rows",
        "schema_row_count",
    ):
        _count(summary[field])
    if (
        summary["request_digest"] != query.request["request_digest"]
        or summary["candidate_count"] != len(content)
        or len(content) > query.candidate_limit
        or summary["available_content_rows"] - len(content) != summary["overflow_count"]
        or summary["evidence_count"] != sum(wire["kind"] == "evidence" for wire, _ in content)
        or summary["available_evidence_rows"] < summary["evidence_count"]
        or summary["available_content_rows"] < summary["available_evidence_rows"]
        or type(summary["schema_rows"]) is not list
        or summary["schema_row_count"] > 1000
        or summary["schema_row_count"] != len(summary["schema_rows"])
    ):
        raise ValueError("source_material_invalid")
    receipts = {}
    for run_id, value in metadata.items():
        if set(value) != {
            "receipt_count",
            "receipt_json",
            "row_set_digest",
            "family_counts",
            "manifest_count",
            "copy_receipt_count",
            "manifest",
            "copy_receipts",
        }:
            raise ValueError("source_material_invalid")
        if _count(value["receipt_count"]) != 1:
            raise ValueError("source_material_invalid")
        receipt = build_run_receipt(**_dates(_json(value["receipt_json"])))
        if (
            receipt.run_id != run_id
            or receipt.client_scope_id != query.request["client_scope_id"]
            or receipt.status != "completed"
            or not receipt.complete_partitions
            or receipt.completed_at > datetime.fromisoformat(query.request["as_of"])
            or receipt.row_set_digest != value["row_set_digest"]
        ):
            raise ValueError("source_material_invalid")
        _versioned_fields("membership", receipt.cluster_build_version)
        if set(value["family_counts"]) != set(FAMILY_RELATIONS):
            raise ValueError("source_material_invalid")
        for family, count in value["family_counts"].items():
            _count(count)
            field = {"candidates": "candidate", "predictions": "prediction"}.get(
                family, family
            ) + "_count"
            if family != "outcomes" and count != getattr(receipt, field):
                raise ValueError("source_material_invalid")
        for count_key, rows_key, relation in (
            ("manifest_count", "manifest", "open_intelligence_source_copy_manifest_v1"),
            ("copy_receipt_count", "copy_receipts", "open_intelligence_source_copy_receipts_v1"),
        ):
            if (
                type(value[rows_key]) is not list
                or not 1 <= _count(value[count_key]) <= _META_LIMIT
                or len(value[rows_key]) != value[count_key]
            ):
                raise ValueError("source_material_invalid")
            for source in value[rows_key]:
                if set(source) != set(RELATION_FIELDS[relation]):
                    raise ValueError("source_material_invalid")
                if (
                    source["window_start"] != receipt.observation_start.isoformat()
                    or source["window_end"] != receipt.observation_end.isoformat()
                ):
                    raise ValueError("source_material_invalid")
        _source_copy_authority(receipt, tuple(value["manifest"]), tuple(value["copy_receipts"]))
        receipts[run_id] = receipt
    records = {table: [] for table in CONTENT_RELATIONS}
    selected_facts = []
    candidates = {}
    producer_scope_unverifiable = False
    seen = set()
    as_of = datetime.fromisoformat(query.request["as_of"])
    window = query.plan_window
    for wire, value in sorted(content, key=lambda item: item[0]["kind"] != "candidates"):
        table = wire["kind"]
        record = _dates(value)
        receipt = receipts[wire["run_id"]]
        record = _mapping(record, CONTENT_RELATIONS[table], receipt.cluster_build_version)
        canonical = (
            _canonicalize_row(table, record, cluster_build_version=receipt.cluster_build_version)
            if table in TABLE_BINDINGS
            else {**record, **{key: _canonicalize_value(key, record[key]) for key in _SCOPE_FIELDS}}
        )
        identity = (
            (table, *(canonical[key] for key in NATURAL_KEYS[table]))
            if table in TABLE_BINDINGS
            else (table, canonical["run_id"], canonical["analysis_id"])
        )
        if identity in seen:
            raise ValueError("source_material_invalid")
        seen.add(identity)
        if (
            record["client_scope_id"] != query.request["client_scope_id"]
            or record["run_id"] != wire["run_id"]
            or record.get("signal_id") != wire["signal_id"]
            or record["market"] != wire["market"]
            or record["market"] not in query.request["market_scope"]
            or record["signal_date"] != receipt.signal_date
            or record["contract_version"] != "2.0.0"
        ):
            raise ValueError("source_material_invalid")
        _validate_future((record,), receipt.completed_at)
        is_selected = (
            record["run_id"],
            record.get("signal_id"),
            record["market"],
        ) in query.selected
        if (
            table == "candidates"
            and is_selected
            and (
                record["audience_lens_ids"]
                or record["cluster_build_version"] != receipt.cluster_build_version
            )
        ):
            raise ValueError("source_material_invalid")
        if table == "evidence" and is_selected and record["published_at"] is None:
            raise ValueError("source_material_invalid")
        records[table].append(record)
        if "signal_id" not in record:
            continue
        candidate_identity = tuple(
            canonical[key]
            for key in ("client_scope_id", "run_id", "signal_date", "market", "signal_id")
        )
        if table == "candidates":
            if candidate_identity in candidates:
                raise ValueError("source_material_invalid")
            candidates[candidate_identity] = canonical
        else:
            candidate = candidates.get(candidate_identity)
            if candidate is None:
                producer_scope_unverifiable = True
                continue
            if any(
                canonical[key] != candidate[key]
                for key in _SCOPE_FIELDS
                if key in RELATION_FIELDS[CONTENT_RELATIONS[table]]
            ):
                raise ValueError("source_material_invalid")
        if (
            table == "evidence"
            and is_selected
            and record["availability"] == "available"
            and record["evidence_state"] == "ready"
            and record["published_at"] <= as_of
        ):
            if window is not None and not (
                datetime.combine(date.fromisoformat(window["start"]), time.min, UTC)
                <= record["published_at"]
                < datetime.combine(date.fromisoformat(window["end"]), time.min, UTC)
                + timedelta(days=1)
            ):
                continue
            selected_facts.append(
                {
                    "row_id": record["row_id"],
                    "evidence_id": record["evidence_id"],
                    "market": record["market"],
                    "excerpt": record["excerpt"],
                    "source_label": record["source_label"],
                    "author": record["author_label"],
                    "url": record["url"],
                    "platform": record["platform"],
                    "published_at": record["published_at"],
                    "collected_at": None,
                    "source_family": record["source_family"],
                    "source_relation": CONTENT_RELATIONS["evidence"],
                    "source_run_id": record["run_id"],
                    "source_signal_id": record["signal_id"],
                    "source_scope": {key: copy.deepcopy(record[key]) for key in _SCOPE_FIELDS},
                    "projection_sha256": wire["payload_sha256"],
                }
            )
    if not summary["overflow_count"] and any(
        sum(
            (row["run_id"], row["signal_id"], row["market"]) == identity
            for row in records["candidates"]
        )
        != 1
        for identity in query.selected
    ):
        raise ValueError("source_material_invalid")
    missing = [
        "full_run_row_and_reference_validation",
        "unselected_future_and_version_validation",
        "physical_schema_validation",
        "source_row_copy_binding",
        "resolved_plan_window_and_scope_admission",
        "release_pointer_and_runtime_authorization",
        "actual_select_job_and_digest_sql_parity",
    ]
    if summary["overflow_count"]:
        missing.append("selected_content_overflow")
        selected_facts = []
    else:
        for run_id, receipt in receipts.items():
            run_rows = {
                relation: tuple(record for record in records[family] if record["run_id"] == run_id)
                for family, relation in FAMILY_RELATIONS.items()
            }
            if any(
                len(run_rows[relation]) != metadata[run_id]["family_counts"][family]
                for family, relation in FAMILY_RELATIONS.items()
            ):
                raise ValueError("source_material_invalid")
            _validate_complete_run_rows(receipt, run_rows)
        missing.remove("full_run_row_and_reference_validation")
        missing.remove("unselected_future_and_version_validation")
    selected_facts.sort(
        key=lambda fact: (
            fact["source_run_id"],
            fact["market"],
            fact["source_signal_id"],
            fact["evidence_id"],
        )
    )
    available_fact_count = None if summary["overflow_count"] else len(selected_facts)
    selected_facts = selected_facts[: query.evidence_limit]
    selection_counts = {
        "available_fact_count": available_fact_count,
        "selected_fact_count": len(selected_facts),
        "omitted_fact_count": (
            None if available_fact_count is None else available_fact_count - len(selected_facts)
        ),
        "limit": query.evidence_limit,
    }
    selection_truncation_reason = (
        "inspection_truncated"
        if available_fact_count is None
        else "selection_limit"
        if selection_counts["omitted_fact_count"] > 0
        else None
    )
    if any(receipt.cluster_build_version == "hybrid_graph_v3" for receipt in receipts.values()):
        missing.append("original_v3_source_snapshot_binding")
    if producer_scope_unverifiable:
        missing.append("selected_producer_scope_unverifiable")
    return {
        "material_version": "general_question_source_material_v1",
        "request_scope": {key: copy.deepcopy(query.request[key]) for key in _SCOPE_FIELDS},
        "records": records,
        "selected_facts": selected_facts,
        "selection_counts": selection_counts,
        "selection_truncation_reason": selection_truncation_reason,
        "run_metadata": metadata,
        "schema_rows": summary["schema_rows"],
        "candidate_count": len(content),
        "evidence_count": summary["evidence_count"],
        "overflow_count": summary["overflow_count"],
        "collection_time_available": False,
        "missing_checks": missing,
    }
