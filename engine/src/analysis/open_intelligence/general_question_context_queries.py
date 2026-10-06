"""Fixed contextual reads of protected snapshots; no source or release authority."""

import hashlib
import json
import re
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta

from google.cloud import bigquery

from src.analysis.open_intelligence import pipeline
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_context_geo import CONTEXT_GEO_POLICY
from src.analysis.open_intelligence.general_question_parent_capsule import ParentSourceUnavailable
from src.analysis.open_intelligence.general_question_retrieval_queries import _validated_inputs
from src.analysis.open_intelligence.production_snapshot_rows import (
    ABSENT_NULLABLE_FIELDS,
    physical_fields,
)
from src.analysis.open_intelligence.production_snapshot_tables import (
    LANES,
    PROFILE_ID,
    retained_origin_registry,
    retained_v1_approval,
    retained_v1_result,
)
from src.analysis.open_intelligence.protected_context_registry import allowed_cutoffs, profile_for

SUPPLEMENT_EVIDENCE_LIMIT = 24


@dataclass(frozen=True)
class PreparedContextQuery:
    template_id: str
    sql: str
    parameters: tuple
    sql_digest: str
    parameters_digest: str
    candidate_limit: int
    transport_row_limit: int
    source_binding_digest: str
    expected_sample_keys: tuple
    source_snapshot_digest: str
    profile_id: str


def _json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("context_query_invalid")
            result[key] = item
        return result

    if type(value) is not str:
        raise ValueError("context_query_invalid")
    result = json.loads(
        value,
        object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("context_query_invalid")),
    )
    if type(result) is not dict:
        raise ValueError("context_query_invalid")
    return result


def _source(request, plan, source):
    try:
        _binding_digest(source)
        cutoff = date.fromisoformat(source["cutoff_date"])
        as_of = datetime.combine(cutoff + timedelta(days=1), time.min, UTC)
        captured = datetime.fromisoformat(source["captured_at"])
        if (
            cutoff.isoformat() not in allowed_cutoffs()
            or source["profile_id"] != profile_for(cutoff).profile_id
            or source["operation"] != "source_snapshot_capture"
            or source["source_relation_set_id"] != PROFILE_ID
            or source["client_scope_id"] != request["client_scope_id"]
            or source["market_scope"] != sorted(set(source["market_scope"]))
            or not set(plan["markets"]) <= set(source["market_scope"]) <= {"ke", "ng", "za"}
            or datetime.fromisoformat(source["source_as_of"]) != as_of
            or not as_of <= captured <= datetime.fromisoformat(request["as_of"])
            or re.fullmatch(r"[0-9a-f]{64}", source["snapshot_digest"]) is None
            or type(source["snapshot_tables"]) is not list
            or len(source["snapshot_tables"]) != 5
        ):
            raise ValueError()
        tables = {}
        for lane, row in zip(LANES, source["snapshot_tables"], strict=True):
            table = f"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_{cutoff:%Y%m%d}_{lane}"
            if (
                row["lane"] != lane
                or row["snapshot_table"] != table
                or datetime.fromisoformat(row["snapshot_time"]) != as_of
            ):
                raise ValueError()
            tables[lane] = table
        return cutoff, tables
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("context_query_invalid") from error


def _binding_digest(source):
    if type(source) is not dict:
        raise ValueError("context_query_invalid")
    digest = canonical_digest(
        {key: value for key, value in source.items() if key != "source_binding_digest"}
    )
    if "source_binding_digest" in source and source["source_binding_digest"] != digest:
        raise ValueError("context_query_invalid")
    return digest


def _context_requirements(plan):
    content = [item for item in plan["requirements"] if item["kind"] == "content"]
    return content or [item for item in plan["requirements"] if item["kind"] == "aggregate"]


def _parameters(request, plan, source, cutoff, limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("context_query_invalid")
    requirements = _context_requirements(plan)
    if not requirements:
        raise ValueError("context_query_invalid")
    terms = sorted({term.strip().lower() for item in requirements for term in item["search_terms"]})
    window = plan["window"]
    return [
        bigquery.ScalarQueryParameter("capture_cutoff", "DATE", cutoff),
        bigquery.ArrayQueryParameter("markets", "STRING", plan["markets"]),
        bigquery.ArrayQueryParameter("search_terms", "STRING", terms),
        bigquery.ScalarQueryParameter(
            "as_of", "TIMESTAMP", datetime.fromisoformat(request["as_of"])
        ),
        bigquery.ScalarQueryParameter(
            "source_as_of", "TIMESTAMP", datetime.fromisoformat(source["source_as_of"])
        ),
        bigquery.ScalarQueryParameter(
            "publication_start",
            "TIMESTAMP",
            datetime.combine(date.fromisoformat(window["start"]), time.min, UTC),
        ),
        bigquery.ScalarQueryParameter(
            "publication_end",
            "TIMESTAMP",
            datetime.combine(date.fromisoformat(window["end"]) + timedelta(days=1), time.min, UTC),
        ),
        bigquery.ScalarQueryParameter("candidate_limit", "INT64", limit),
        bigquery.ScalarQueryParameter(
            "source_snapshot_digest", "STRING", source["snapshot_digest"]
        ),
        bigquery.ScalarQueryParameter("profile_id", "STRING", source["profile_id"]),
        bigquery.ScalarQueryParameter("source_binding_digest", "STRING", _binding_digest(source)),
    ]


def _prepared(template, sql, params, source, limit, keys=()):
    return PreparedContextQuery(
        template,
        sql,
        tuple(params),
        hashlib.sha256(sql.encode()).hexdigest(),
        canonical_digest(tuple(item.to_api_repr() for item in params)),
        limit,
        limit + 1,
        _binding_digest(source),
        tuple(keys),
        source["snapshot_digest"],
        source["profile_id"],
    )


def _requirement_relation(plan):
    """Typed arrays for the requirements and the explicit requirement to term relation."""
    requirements = _context_requirements(plan)
    relation = [
        (item["requirement_id"], term, "normalized_literal")
        for item in requirements
        for term in sorted({term.strip().lower() for term in item["search_terms"]})
    ]
    return [
        bigquery.ArrayQueryParameter(
            "requirement_ids", "STRING", [item["requirement_id"] for item in requirements]
        ),
        bigquery.ArrayQueryParameter(
            "requirement_mandatory", "BOOL", [item["mandatory"] is True for item in requirements]
        ),
        bigquery.ArrayQueryParameter(
            "relation_requirement_ids", "STRING", [row[0] for row in relation]
        ),
        bigquery.ArrayQueryParameter("relation_terms", "STRING", [row[1] for row in relation]),
        bigquery.ArrayQueryParameter("relation_methods", "STRING", [row[2] for row in relation]),
    ]


_REQUIREMENT_CTES = """requirements AS (
 SELECT @requirement_ids[OFFSET(position)] AS requirement_id,@requirement_mandatory[OFFSET(position)] AS mandatory
 FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@requirement_ids)-1)) position
), relation AS (
 SELECT @relation_requirement_ids[OFFSET(position)] AS requirement_id,@relation_terms[OFFSET(position)] AS term,@relation_methods[OFFSET(position)] AS method
 FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@relation_terms)-1)) position
)"""
_REQUIREMENT_HIT = """(NOT EXISTS(SELECT 1 FROM relation WHERE relation.requirement_id=requirements.requirement_id)
 OR EXISTS(SELECT 1 FROM relation WHERE relation.requirement_id=requirements.requirement_id AND STRPOS({search_text},relation.term)>0))"""


def _pair_coverage_ctes(identity, rank_keys):
    """Pair ranks per (requirement, market) and one selection priority per matching row.

    Priority 0 is the top row of a mandatory pair, 1 the top row of an optional pair, 2 the
    rest ordered by their best pair rank, so every pair keeps its head inside the cap and the
    remainder rotates across pairs instead of one market filling the limit.
    """
    return f"""pair_ranks AS (
 SELECT *,ROW_NUMBER() OVER(PARTITION BY requirement_id,market ORDER BY distinct_requirement_count DESC,{rank_keys}) AS pair_rank
 FROM (SELECT *,COUNT(*) OVER(PARTITION BY {identity}) AS distinct_requirement_count FROM requirement_hits)
), priority AS (
 SELECT {identity},MIN(IF(pair_rank=1,IF(mandatory,0,1),2)) AS selection_priority,MIN(pair_rank) AS pair_rank
 FROM pair_ranks GROUP BY {identity}
), eligible AS (
 SELECT matching.*,COALESCE(priority.selection_priority,3) AS selection_priority,COALESCE(priority.pair_rank,2147483647) AS pair_rank
 FROM matching LEFT JOIN priority USING({identity})
)"""


_META = """TO_JSON_STRING(STRUCT((SELECT COUNT(*) FROM picked) AS candidate_count,
 (SELECT COUNT(*) FROM eligible) AS full_matching_count,
 (SELECT COUNT(*) FROM eligible) > (SELECT COUNT(*) FROM picked) AS overflow,
 @source_snapshot_digest AS source_snapshot_digest, @profile_id AS profile_id))"""


def build_context_candidate_query(request, plan, intake, source_binding, *, candidate_limit):
    request, plan = _validated_inputs(request, plan, intake)
    cutoff, tables = _source(request, plan, source_binding)
    params = _parameters(request, plan, source_binding, cutoff, candidate_limit)
    parts = []
    for lane, field, date_field, key in (
        ("event_ledger", "entity_key", "trend_date", "ledger_id"),
        (
            "seed_graph",
            "term",
            "trend_date",
            "TO_JSON_STRING(STRUCT(market,term,term_type,platform))",
        ),
        ("seed_candidates", "candidate_value", "proposed_date", "candidate_id"),
    ):
        samples = "CAST([] AS ARRAY<STRING>)" if lane == "event_ledger" else "sample_row_ids"
        status = " AND status IN ('applied','approved')" if lane == "seed_candidates" else ""
        columns = ", ".join(f"`{name}`" for name in physical_fields(lane))
        parts.append(
            f"SELECT '{lane}' AS lane,market,{key} AS candidate_key,{samples} AS sample_row_ids,\n"
            f"TO_JSON_STRING(STRUCT({columns})) AS payload,LOWER({field}) AS search_text\n"
            f"FROM `{tables[lane]}` WHERE {date_field}=@capture_cutoff AND market IN UNNEST(@markets){status}"
        )
    params.extend(_requirement_relation(plan))
    ordering = (
        "selection_priority,pair_rank,matched_term_count DESC,lane,market,candidate_key,payload"
    )
    sql = (
        "WITH candidates AS (\n"
        + "\nUNION ALL\n".join(parts)
        + "\n), ranked AS (\n"
        + """SELECT *,
 (SELECT COUNT(*) FROM UNNEST(@search_terms) term WHERE STRPOS(search_text,term)>0) AS matched_term_count
 FROM candidates WHERE @source_binding_digest IS NOT NULL
), matching AS (
 SELECT * FROM ranked WHERE ARRAY_LENGTH(@search_terms)=0 OR matched_term_count>0
), """
        + _REQUIREMENT_CTES
        + """, requirement_hits AS (
 SELECT matching.lane,matching.market,matching.candidate_key,matching.payload,matching.matched_term_count,requirements.requirement_id,requirements.mandatory
 FROM matching CROSS JOIN requirements WHERE """
        + _REQUIREMENT_HIT.format(search_text="matching.search_text")
        + "\n), "
        + _pair_coverage_ctes(
            "lane,market,candidate_key,payload",
            "matched_term_count DESC,lane,candidate_key,payload",
        )
        + f""", picked AS (
 SELECT *,ROW_NUMBER() OVER(ORDER BY {ordering}) AS ordering
 FROM eligible ORDER BY {ordering} LIMIT @candidate_limit
), output AS (
 SELECT '__metadata__' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS candidate_key,
 CAST([] AS ARRAY<STRING>) AS sample_row_ids,"""
        + _META
        + """ AS payload,0 AS ordering
 UNION ALL SELECT lane,market,candidate_key,sample_row_ids,payload,ordering FROM picked
) SELECT lane,market,candidate_key,sample_row_ids,payload FROM output ORDER BY ordering"""
    )
    return _prepared(
        "protected_context_candidates_v1", sql, params, source_binding, candidate_limit
    )


def _wire_rows(rows, source, *, evidence):
    if type(rows) not in (list, tuple) or not rows:
        raise ValueError("context_query_invalid")
    fields = {"lane", "market", "payload"} | (
        {"id", "match_count"} if evidence else {"candidate_key", "sample_row_ids"}
    )
    if any(type(row) is not dict or set(row) != fields for row in rows):
        raise ValueError("context_query_invalid")
    meta = rows[0]
    if (
        meta["lane"] != "__metadata__"
        or meta["market"] is not None
        or (
            meta["id"] is not None or meta["match_count"] is not None
            if evidence
            else meta["candidate_key"] is not None or meta["sample_row_ids"] != []
        )
    ):
        raise ValueError("context_query_invalid")
    stats = _json(meta["payload"])
    if set(stats) != {
        "candidate_count",
        "full_matching_count",
        "overflow",
        "source_snapshot_digest",
        "profile_id",
    } or (
        type(stats["candidate_count"]) is not int
        or stats["candidate_count"] != len(rows) - 1
        or type(stats["full_matching_count"]) is not int
        or stats["full_matching_count"] < len(rows) - 1
        or type(stats["overflow"]) is not bool
        or stats["overflow"] != (stats["full_matching_count"] > len(rows) - 1)
        or stats["source_snapshot_digest"] != source["snapshot_digest"]
        or stats["profile_id"] != source["profile_id"]
    ):
        raise ValueError("context_query_invalid")
    records = []
    for row in rows[1:]:
        payload = _json(row["payload"])
        if row["market"] not in source["market_scope"] or payload.get("market") != row["market"]:
            raise ValueError("context_query_invalid")
        if evidence:
            if (
                row["lane"] not in LANES[3:]
                or type(row["id"]) is not str
                or not row["id"]
                or payload.get("id") != row["id"]
                or type(row["match_count"]) is not int
                or row["match_count"] < 1
            ):
                raise ValueError("context_query_invalid")
        elif (
            row["lane"] not in LANES[:3]
            or type(row["candidate_key"]) is not str
            or not row["candidate_key"]
            or type(row["sample_row_ids"]) is not list
            or any(type(v) is not str or not v for v in row["sample_row_ids"])
            or row["sample_row_ids"] != payload.get("sample_row_ids", [])
        ):
            raise ValueError("context_query_invalid")
        records.append({**row, "payload": payload})
    return {**stats, "records": records}


_DIRECT_ELIGIBILITY = """evidence.market IN UNNEST(@markets)
 AND evidence.collected_at<=@as_of AND evidence.collected_at<=@source_as_of
 AND (evidence.published_at IS NULL OR (evidence.published_at>=@publication_start AND evidence.published_at<@publication_end AND evidence.published_at<=@as_of AND evidence.published_at<=@source_as_of))
 AND COALESCE(evidence.published_at,evidence.collected_at)>=@publication_start
 AND COALESCE(evidence.published_at,evidence.collected_at)<@publication_end
"""

_EVIDENCE_CONTENT_ELIGIBILITY = """(NULLIF(TRIM(COALESCE(evidence.text,'')),'') IS NOT NULL
 OR NULLIF(TRIM(COALESCE(evidence.title,'')),'') IS NOT NULL)"""


def build_context_partition_query(request, plan, intake, source_binding):
    request, plan = _validated_inputs(request, plan, intake)
    cutoff, tables = _source(request, plan, source_binding)
    params = _parameters(request, plan, source_binding, cutoff, 1000)
    branches = [
        f"SELECT '{lane}' AS lane,CAST(DATE(evidence.collected_at) AS STRING) AS partition_date FROM `{tables[lane]}` evidence WHERE {_DIRECT_ELIGIBILITY} AND @source_binding_digest IS NOT NULL GROUP BY partition_date"
        for lane in ("enriched_content", "raw_content")
    ]
    sql = (
        "WITH eligible AS ("
        + " UNION ALL ".join(branches)
        + """), collected AS (
 SELECT ARRAY_AGG(STRUCT(lane,partition_date) ORDER BY lane,partition_date LIMIT 1001) AS partitions FROM eligible
) SELECT item.lane,item.partition_date,COALESCE(ARRAY_LENGTH(partitions),0) AS partition_count
FROM collected, UNNEST(ARRAY_CONCAT([STRUCT('__metadata__' AS lane,CAST(NULL AS STRING) AS partition_date)],IFNULL(partitions,[]))) item"""
    )
    return _prepared("protected_context_partitions_v1", sql, params, source_binding, 1000)


def decode_context_partition_rows(rows, *, query):
    if (
        query.template_id != "protected_context_partitions_v1"
        or type(rows) not in (list, tuple)
        or not rows
    ):
        raise ValueError("context_query_invalid")
    output = {"enriched_content": [], "raw_content": []}
    meta = [row for row in rows if row.get("lane") == "__metadata__"]
    if len(meta) != 1:
        raise ValueError("context_query_invalid")
    count = meta[0].get("partition_count")
    if (
        type(count) is not int
        or not 0 <= count <= 1000
        or len(rows) != count + 1
        or meta[0].get("partition_date") is not None
    ):
        raise ValueError("context_query_invalid")
    params = {p.name: getattr(p, "value", None) for p in query.parameters}
    for row in rows:
        if (
            set(row) != {"lane", "partition_date", "partition_count"}
            or type(row["partition_count"]) is not int
            or row["partition_count"] != count
        ):
            raise ValueError("context_query_invalid")
        if row["lane"] == "__metadata__":
            continue
        lane, day = row["lane"], row["partition_date"]
        if lane not in output or type(day) is not str:
            raise ValueError("context_query_invalid")
        parsed = date.fromisoformat(day)
        if (
            parsed.isoformat() != day
            or parsed > min(params["as_of"].date(), params["source_as_of"].date())
            or day in output[lane]
        ):
            raise ValueError("context_query_invalid")
        output[lane].append(day)
    return {lane: sorted(days) for lane, days in output.items()}


def _build_context_evidence_query(
    request,
    plan,
    intake,
    source_binding,
    *,
    candidate_rows,
    lane,
    candidate_limit,
    enriched_rows=None,
    index_rows=None,
    _index=False,
    partition_rows=None,
    _validation_legacy=False,
    geo_policy=False,
    parent_context=None,
    continuity_policy=False,
):
    if (
        type(_validation_legacy) is not bool
        or type(geo_policy) is not bool
        or (geo_policy and (partition_rows is None or _validation_legacy))
    ):
        raise ValueError("context_query_invalid")
    request, plan = _validated_inputs(request, plan, intake)
    cutoff, tables = _source(request, plan, source_binding)
    parent_refs = []
    if "parent_context_ref" in intake:
        from src.analysis.open_intelligence.general_question_parent_capsule import (
            select_parent_aliases,
            validate_capsule,
        )

        capsule = validate_capsule(
            parent_context, request=request, reference=intake["parent_context_ref"]
        )
        parent_refs = select_parent_aliases(capsule, plan["parent_receipt_aliases"])
        if any(
            row["source_binding_digest"] != _binding_digest(source_binding)
            or row["market"] not in plan["markets"]
            for row in parent_refs
        ):
            raise ParentSourceUnavailable()
    elif parent_context is not None:
        raise ValueError("parent_context_invalid")
    candidates = _wire_rows(candidate_rows, source_binding, evidence=False)
    keys = {
        (row["market"], value) for row in candidates["records"] for value in row["sample_row_ids"]
    }
    lane_parent_refs = [row for row in parent_refs if row["lane"] == lane]
    keys.update((row["market"], row["source_row_id"]) for row in lane_parent_refs)
    excluded_raw_refs = (
        [row for row in parent_refs if row["lane"] == "raw_content"]
        if lane == "enriched_content"
        else []
    )
    keys -= {(row["market"], row["source_row_id"]) for row in excluded_raw_refs}
    seed_keys = tuple(sorted(keys))
    supplement = partition_rows is not None and bool(seed_keys)
    if lane == "raw_content":
        enriched = _wire_rows(enriched_rows, source_binding, evidence=True)
        if any(row["lane"] != "enriched_content" for row in enriched["records"]):
            raise ValueError("context_query_invalid")
        keys -= {(row["market"], row["id"]) for row in enriched["records"]}
    elif lane != "enriched_content":
        raise ValueError("context_query_invalid")
    exhausted_raw_tail = (
        type(candidate_limit) is int
        and candidate_limit == 0
        and lane == "raw_content"
        and (supplement or (continuity_policy and parent_context is not None))
        and (
            0 < enriched["candidate_count"] <= SUPPLEMENT_EVIDENCE_LIMIT
            or enriched["candidate_count"] == 200
        )
    )
    params = _parameters(
        request, plan, source_binding, cutoff, 1 if exhausted_raw_tail else candidate_limit
    )
    if geo_policy:
        params.append(
            bigquery.ScalarQueryParameter("context_geo_policy", "STRING", CONTEXT_GEO_POLICY)
        )
    if continuity_policy:
        params.append(
            bigquery.ScalarQueryParameter(
                "parent_context_digest",
                "STRING",
                canonical_digest(parent_context) if parent_context is not None else "none",
            )
        )
        if lane_parent_refs:
            params.extend(
                [
                    bigquery.ArrayQueryParameter(
                        "parent_markets", "STRING", [row["market"] for row in lane_parent_refs]
                    ),
                    bigquery.ArrayQueryParameter(
                        "parent_ids", "STRING", [row["source_row_id"] for row in lane_parent_refs]
                    ),
                    bigquery.ArrayQueryParameter(
                        "parent_content_digests",
                        "STRING",
                        [row["content_digest"] for row in lane_parent_refs],
                    ),
                ]
            )
        if excluded_raw_refs:
            params.extend(
                [
                    bigquery.ArrayQueryParameter(
                        "excluded_parent_markets",
                        "STRING",
                        [row["market"] for row in excluded_raw_refs],
                    ),
                    bigquery.ArrayQueryParameter(
                        "excluded_parent_ids",
                        "STRING",
                        [row["source_row_id"] for row in excluded_raw_refs],
                    ),
                ]
            )
    if exhausted_raw_tail:
        params = [
            bigquery.ScalarQueryParameter("candidate_limit", "INT64", 0)
            if item.name == "candidate_limit"
            else item
            for item in params
        ]
    keys = tuple(sorted(keys))
    if len(keys) > 1000 or not {market for market, _ in keys} <= set(plan["markets"]):
        raise ValueError("context_query_invalid")
    direct = partition_rows is not None
    if direct:
        if not any(p.values for p in params if p.name == "search_terms") and not parent_refs:
            raise ValueError("context_query_invalid")
        partition_query = build_context_partition_query(request, plan, intake, source_binding)
        days = decode_context_partition_rows(partition_rows, query=partition_query)[lane]
        params.append(
            bigquery.ArrayQueryParameter(
                "eligible_partition_dates", "DATE", [date.fromisoformat(day) for day in days]
            )
        )
        params.append(bigquery.ScalarQueryParameter("direct_content_fallback", "BOOL", True))
        if supplement:
            params.extend(
                [
                    bigquery.ArrayQueryParameter(
                        "seed_markets", "STRING", [key[0] for key in seed_keys]
                    ),
                    bigquery.ArrayQueryParameter(
                        "seed_ids", "STRING", [key[1] for key in seed_keys]
                    ),
                ]
            )
    columns = ", ".join(
        f"CAST(NULL AS STRING) AS `{field}`"
        if field in ABSENT_NULLABLE_FIELDS
        else f"evidence.`{field}`"
        for field in pipeline.EVIDENCE_COLUMNS_BY_TABLE[lane]
    )
    partition_guard = ""
    if _index:
        columns = "evidence.market, evidence.id, CAST(DATE(evidence.collected_at) AS STRING) AS collected_date"
    else:
        index_query = _build_context_evidence_query(
            request,
            plan,
            intake,
            source_binding,
            candidate_rows=candidate_rows,
            lane=lane,
            candidate_limit=1000,
            enriched_rows=enriched_rows,
            _index=True,
            partition_rows=partition_rows,
            _validation_legacy=_validation_legacy,
            geo_policy=geo_policy,
            parent_context=parent_context,
            continuity_policy=continuity_policy,
        )
        indexed = decode_context_rows(index_rows, query=index_query)
        tuples = sorted(
            (row["market"], row["id"], date.fromisoformat(row["payload"]["collected_date"]))
            for row in indexed["records"]
        )
        keys = tuple((market, identifier) for market, identifier, _ in tuples)
        params.append(
            bigquery.ArrayQueryParameter("sample_dates", "DATE", [day for _, _, day in tuples])
        )
        dates = sorted(
            {date.fromisoformat(row["payload"]["collected_date"]) for row in indexed["records"]}
        )
        params.append(bigquery.ArrayQueryParameter("selected_dates", "DATE", dates))
        partition_guard = "AND DATE(evidence.collected_at) IN UNNEST(@selected_dates) AND DATE(evidence.collected_at)=requested.collected_date"
        if not dates:
            keys = ()
    prior_date_guard = (
        ""
        if _validation_legacy or direct
        else " AND DATE(prior.collected_at) BETWEEN DATE_SUB(@capture_cutoff, INTERVAL 6 DAY) AND @capture_cutoff"
    )
    fallback_guard = (
        f"AND NOT EXISTS (SELECT 1 FROM `{tables['enriched_content']}` prior WHERE prior.market=evidence.market AND prior.id=evidence.id AND prior.collected_at<=@source_as_of{prior_date_guard})"
        if lane == "raw_content" and _index
        else ""
    )
    if continuity_policy and lane_parent_refs and lane == "raw_content" and _index:
        fallback_guard = (
            "AND (EXISTS(SELECT 1 FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@parent_ids)-1)) position WHERE @parent_markets[OFFSET(position)]=evidence.market AND @parent_ids[OFFSET(position)]=evidence.id) OR "
            + fallback_guard.removeprefix("AND ")
            + ")"
        )
    content_guard = "" if _validation_legacy else f"\n AND {_EVIDENCE_CONTENT_ELIGIBILITY}"
    keyed_date_guard = (
        ""
        if _validation_legacy
        else "\n AND DATE(evidence.collected_at) BETWEEN DATE_SUB(@capture_cutoff, INTERVAL 6 DAY) AND @capture_cutoff"
    )
    direct_content_guard = (
        "\n" if _validation_legacy else f"\n AND {_EVIDENCE_CONTENT_ELIGIBILITY}\n"
    )
    ranking_columns = (
        ",COALESCE(evidence.platform,'') AS source_platform,"
        "(SELECT COUNT(*) FROM UNNEST(@search_terms) term WHERE STRPOS(LOWER(CONCAT(COALESCE(evidence.text,''),' ',COALESCE(evidence.title,''))),term)>0) AS term_relevance,"
        "IF(EXISTS(SELECT 1 FROM requested linked WHERE linked.market=evidence.market AND linked.id=evidence.id),0,1) AS seed_priority"
        if (supplement or continuity_policy) and _index
        else ""
    )
    keyed_where = f"""WHERE evidence.collected_at<=@as_of AND evidence.collected_at<=@source_as_of{keyed_date_guard}
 AND (evidence.published_at IS NULL OR (evidence.published_at>=@publication_start AND evidence.published_at<@publication_end AND evidence.published_at<=@as_of AND evidence.published_at<=@source_as_of)){content_guard}
 AND @source_binding_digest IS NOT NULL {fallback_guard} {partition_guard}"""
    direct_where = f"""WHERE {_DIRECT_ELIGIBILITY}{direct_content_guard} AND DATE(evidence.collected_at) IN UNNEST(@eligible_partition_dates)
 AND EXISTS(SELECT 1 FROM UNNEST(@search_terms) term WHERE STRPOS(LOWER(CONCAT(COALESCE(evidence.text,''),' ',COALESCE(evidence.title,''))),term)>0)
 AND @source_binding_digest IS NOT NULL {fallback_guard}"""
    eligible = (
        f"""SELECT '{lane}' AS lane,evidence.market,evidence.id,TO_JSON_STRING(STRUCT({columns})) AS payload,
 {"1" if _index else "COUNT(*) OVER (PARTITION BY evidence.market, evidence.id)"} AS match_count{ranking_columns}
 FROM `{tables[lane]}` evidence JOIN requested USING(market,id)
 {keyed_where}"""
        if keys
        else f"SELECT '{lane}' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS id,CAST(NULL AS STRING) AS payload,CAST(NULL AS INT64) AS match_count{',CAST(NULL AS STRING) AS source_platform,CAST(NULL AS INT64) AS term_relevance,CAST(NULL AS INT64) AS seed_priority' if (supplement or continuity_policy) and _index else ''} FROM UNNEST(ARRAY<INT64>[]) AS empty_row WHERE FALSE"
    )
    # A zero cap selects nothing, so the tail measurement keeps the retained shape and skips
    # the second scan the requirement hits would cost.
    pair_coverage = _index and not _validation_legacy and not exhausted_raw_tail
    hit_head = f"SELECT '{lane}' AS lane,evidence.market,evidence.id,requirements.requirement_id,requirements.mandatory,COALESCE(evidence.published_at,evidence.collected_at) AS observed_at"
    hit_predicate = _REQUIREMENT_HIT.format(
        search_text="LOWER(CONCAT(COALESCE(evidence.text,''),' ',COALESCE(evidence.title,'')))"
    )
    hits = (
        f"{hit_head}\n FROM `{tables[lane]}` evidence JOIN requested USING(market,id) CROSS JOIN requirements\n {keyed_where} AND {hit_predicate}"
        if keys
        else f"SELECT '{lane}' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS id,CAST(NULL AS STRING) AS requirement_id,CAST(NULL AS BOOL) AS mandatory,CAST(NULL AS TIMESTAMP) AS observed_at FROM UNNEST(ARRAY<INT64>[]) AS empty_row WHERE FALSE"
    )
    if direct and _index and days:
        direct_eligible = f"""SELECT '{lane}' AS lane,evidence.market,evidence.id,TO_JSON_STRING(STRUCT({columns})) AS payload,1 AS match_count{ranking_columns}
 FROM `{tables[lane]}` evidence {direct_where}"""
        eligible = eligible + " UNION ALL " + direct_eligible if supplement else direct_eligible
        direct_hits = f"{hit_head}\n FROM `{tables[lane]}` evidence CROSS JOIN requirements {direct_where} AND {hit_predicate}"
        hits = hits + " UNION ALL " + direct_hits if supplement else direct_hits
    if _index:
        eligible = "SELECT DISTINCT * FROM (" + eligible + ")"
    params.extend(
        [
            bigquery.ArrayQueryParameter("sample_markets", "STRING", [key[0] for key in keys]),
            bigquery.ArrayQueryParameter("sample_ids", "STRING", [key[1] for key in keys]),
        ]
    )
    if _index and not exhausted_raw_tail:
        # The legacy branch keeps the retained SQL text but shares the current parameter list,
        # so a retained binding replays as legacy SQL over the same parameters.
        params.extend(_requirement_relation(plan))
    if pair_coverage:
        selection = (
            _REQUIREMENT_CTES
            + f""", matching AS (
 {eligible}
), requirement_hits AS (
 SELECT lane,market,id,requirement_id,mandatory,MAX(observed_at) AS observed_at FROM ({hits}) GROUP BY lane,market,id,requirement_id,mandatory
), """
            + _pair_coverage_ctes("lane,market,id", "observed_at DESC,lane,id")
        )
    else:
        selection = f"eligible AS (\n {eligible}\n)"
    prefix = "selection_priority,pair_rank," if pair_coverage else ""
    sql = f"""WITH requested AS (
 SELECT @sample_markets[OFFSET(position)] AS market,@sample_ids[OFFSET(position)] AS id{"" if _index else ",@sample_dates[OFFSET(position)] AS collected_date"}
 FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@sample_ids)-1)) position
), {selection}, picked AS (
 SELECT *,ROW_NUMBER() OVER(ORDER BY {prefix}market,id,payload) AS ordering FROM eligible
 ORDER BY {prefix}market,id,payload LIMIT @candidate_limit
), output AS (
 SELECT '__metadata__' AS lane,CAST(NULL AS STRING) AS market,CAST(NULL AS STRING) AS id,{_META} AS payload,CAST(NULL AS INT64) AS match_count,0 AS ordering
 UNION ALL SELECT lane,market,id,payload,match_count,ordering FROM picked
) SELECT lane,market,id,payload,match_count FROM output ORDER BY ordering"""
    if (supplement or continuity_policy) and _index:
        ordering = f"{prefix}seed_priority,platform_round,term_relevance DESC,source_platform,market,id,payload"
        sql = sql.replace(
            f"), picked AS (\n SELECT *,ROW_NUMBER() OVER(ORDER BY {prefix}market,id,payload) AS ordering FROM eligible\n ORDER BY {prefix}market,id,payload LIMIT @candidate_limit",
            "), balanced AS (\n SELECT *,ROW_NUMBER() OVER(PARTITION BY seed_priority,source_platform ORDER BY term_relevance DESC,market,id,payload) AS platform_round FROM eligible\n), picked AS (\n"
            f" SELECT *,ROW_NUMBER() OVER(ORDER BY {ordering}) AS ordering FROM balanced\n ORDER BY {ordering} LIMIT @candidate_limit",
        )
    template = (
        "protected_context_enriched_v1"
        if lane == "enriched_content"
        else "protected_context_raw_v1"
    )
    if _index:
        template = template.replace("_v1", "_index_v1")
    if supplement:
        template = template.replace("_v1", "_v2")
    if geo_policy:
        sql = sql.replace(
            "AND @source_binding_digest IS NOT NULL",
            "AND @source_binding_digest IS NOT NULL AND @context_geo_policy = '"
            + CONTEXT_GEO_POLICY
            + "'",
        )
        template = template.rsplit("_v", 1)[0] + "_v3"
    if continuity_policy:
        sql = sql.replace(
            "AND @source_binding_digest IS NOT NULL",
            "AND @source_binding_digest IS NOT NULL AND @parent_context_digest IS NOT NULL",
        )
        if excluded_raw_refs:
            sql = sql.replace(
                "AND @source_binding_digest IS NOT NULL",
                "AND @source_binding_digest IS NOT NULL AND NOT EXISTS(SELECT 1 FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@excluded_parent_ids)-1)) position WHERE @excluded_parent_markets[OFFSET(position)]=evidence.market AND @excluded_parent_ids[OFFSET(position)]=evidence.id)",
            )
        if lane_parent_refs and supplement and _index:
            sql = sql.replace(
                "IF(EXISTS(SELECT 1 FROM requested linked WHERE linked.market=evidence.market AND linked.id=evidence.id),0,1) AS seed_priority",
                "IF(EXISTS(SELECT 1 FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@parent_ids)-1)) position WHERE @parent_markets[OFFSET(position)]=evidence.market AND @parent_ids[OFFSET(position)]=evidence.id),-1,IF(EXISTS(SELECT 1 FROM requested linked WHERE linked.market=evidence.market AND linked.id=evidence.id),0,1)) AS seed_priority",
            )
        template = template.rsplit("_v", 1)[0] + "_v4"
    return _prepared(template, sql, params, source_binding, candidate_limit, keys)


def build_context_evidence_query(*args, index_rows, **kwargs):
    if "_validation_legacy" in kwargs:
        raise TypeError("validation-only option")
    return _build_context_evidence_query(*args, index_rows=index_rows, **kwargs)


def build_context_index_query(*args, **kwargs):
    if "_validation_legacy" in kwargs:
        raise TypeError("validation-only option")
    return _build_context_evidence_query(*args, _index=True, **kwargs)


def decode_context_rows(rows, *, query):
    params = {
        item.name: getattr(item, "values", getattr(item, "value", None))
        for item in query.parameters
    }
    source = {
        "snapshot_digest": query.source_snapshot_digest,
        "profile_id": query.profile_id,
        "market_scope": params["markets"],
    }
    evidence = query.template_id != "protected_context_candidates_v1"
    result = _wire_rows(rows, source, evidence=evidence)
    excluded = set(
        zip(
            params.get("excluded_parent_markets", []),
            params.get("excluded_parent_ids", []),
            strict=True,
        )
    )
    if any((row["market"], row.get("id")) in excluded for row in result["records"]):
        raise ParentSourceUnavailable()
    if result["candidate_count"] > query.candidate_limit:
        raise ValueError("context_query_invalid")
    direct = params.get("direct_content_fallback") is True
    template = query.template_id.replace("_v2", "_v1").replace("_v3", "_v1").replace("_v4", "_v1")
    seed_keys = set(zip(params.get("seed_markets", []), params.get("seed_ids", []), strict=True))
    if evidence and any(
        (
            not (direct and template.endswith("_index_v1"))
            and (row["market"], row["id"]) not in query.expected_sample_keys
        )
        or row["lane"]
        != {
            "protected_context_enriched_v1": "enriched_content",
            "protected_context_raw_v1": "raw_content",
            "protected_context_enriched_index_v1": "enriched_content",
            "protected_context_raw_index_v1": "raw_content",
        }.get(template)
        for row in result["records"]
    ):
        raise ValueError("context_query_invalid")
    if template.endswith("_index_v1"):
        seen = set()
        for row in result["records"]:
            payload = row["payload"]
            if set(payload) != {"market", "id", "collected_date"} or row["match_count"] != 1:
                raise ValueError("context_query_invalid")
            collected = date.fromisoformat(payload["collected_date"])
            key = (row["market"], row["id"], collected)
            if (
                collected.isoformat() != payload["collected_date"]
                or collected > params["source_as_of"].date()
                or (direct and collected not in params["eligible_partition_dates"])
                or key in seen
            ):
                raise ValueError("context_query_invalid")
            seen.add(key)
    elif evidence:
        expected = set(
            zip(params["sample_markets"], params["sample_ids"], params["sample_dates"], strict=True)
        )
        for row in result["records"]:
            try:
                collected = datetime.fromisoformat(row["payload"]["collected_at"])
                if (
                    collected.tzinfo is None
                    or (row["market"], row["id"], collected.astimezone(UTC).date()) not in expected
                ):
                    raise ValueError()
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("context_query_invalid") from error
        if query.template_id.endswith("_v4") and params.get("parent_ids"):
            from scripts.staging.replay_open_intelligence import _digest

            parent_hashes = dict(
                zip(
                    zip(params["parent_markets"], params["parent_ids"], strict=True),
                    params["parent_content_digests"],
                    strict=True,
                )
            )
            if any(
                (row["market"], row["id"]) in parent_hashes
                and _digest(row["payload"]) != parent_hashes[(row["market"], row["id"])]
                for row in result["records"]
            ):
                raise ParentSourceUnavailable()
    if direct and evidence and not template.endswith("_index_v1"):
        for row in result["records"]:
            payload = row["payload"]
            search = ((payload.get("text") or "") + " " + (payload.get("title") or "")).lower()
            observed = datetime.fromisoformat(
                payload.get("published_at") or payload["collected_at"]
            )
            if (
                (
                    (row["market"], row["id"]) not in seed_keys
                    and not any(term in search for term in params["search_terms"])
                )
                or not params["publication_start"] <= observed < params["publication_end"]
                or observed > params["as_of"]
            ):
                raise ValueError("context_query_invalid")
    return result


_APPROVAL_TABLE = (
    "ogilvy-trends-v2.trends_v2_staging_approvals.open_intelligence_execution_approvals_v1"
)
_CONSUMPTION_TABLE = (
    "ogilvy-trends-v2.trends_v2_staging_approvals.open_intelligence_execution_consumptions_v1"
)
_RESULT_TABLE = (
    "ogilvy-trends-v2.trends_v2_staging_approvals.open_intelligence_execution_results_v1"
)
_RESULT_SQL = f"""WITH anchor AS (
 SELECT * FROM `{_CONSUMPTION_TABLE}` WHERE consumption_id=@consumption_id LIMIT 1
), consumptions AS (
 SELECT c.* FROM `{_CONSUMPTION_TABLE}` c WHERE c.consumption_id=@consumption_id
 OR c.approval_id=(SELECT approval_id FROM anchor) OR c.manifest_sha256=(SELECT manifest_sha256 FROM anchor) OR c.execution_name=(SELECT execution_name FROM anchor)
), approvals AS (
 SELECT a.* FROM `{_APPROVAL_TABLE}` a WHERE a.approval_id=(SELECT approval_id FROM anchor) OR a.manifest_sha256=(SELECT manifest_sha256 FROM anchor)
), results AS (
 SELECT r.* FROM `{_RESULT_TABLE}` r WHERE r.consumption_id=@consumption_id
 OR r.approval_id=(SELECT approval_id FROM anchor) OR r.manifest_sha256=(SELECT manifest_sha256 FROM anchor)
 OR r.result_id IN (SELECT result_id FROM `{_RESULT_TABLE}` WHERE consumption_id=@consumption_id)
)
SELECT (SELECT COUNT(*) FROM consumptions) AS consumption_count,
 (SELECT COUNT(*) FROM approvals) AS approval_count,(SELECT COUNT(*) FROM results) AS result_count,
 (SELECT TO_JSON_STRING(c) FROM consumptions c LIMIT 1) AS consumption_json,
 (SELECT TO_JSON_STRING(a) FROM approvals a JOIN anchor c ON a.approval_id=c.approval_id AND a.manifest_sha256=c.manifest_sha256 AND a.operation=c.operation LIMIT 1) AS approval_json,
 (SELECT TO_JSON_STRING(r) FROM results r JOIN anchor c ON r.consumption_id=c.consumption_id AND r.approval_id=c.approval_id AND r.manifest_sha256=c.manifest_sha256 AND r.operation=c.operation AND r.execution_name=c.execution_name LIMIT 1) AS result_json"""
_APPROVAL_SQL = f"""WITH matching AS (
 SELECT * FROM `{_APPROVAL_TABLE}` WHERE manifest_sha256=@manifest_sha256
 OR approval_id IN (SELECT approval_id FROM `{_APPROVAL_TABLE}` WHERE manifest_sha256=@manifest_sha256)
) SELECT COUNT(*) AS approval_count,ARRAY_AGG(TO_JSON_STRING(matching) LIMIT 1)[SAFE_OFFSET(0)] AS approval_json FROM matching"""
# The 42 (v2) family is read through separate statements keyed by the immutable key. Counts run
# over the whole family regardless of version literal or generation so a second row for one key
# refuses; the contract version is admitted on the returned row, and the projected chain must
# share one recorded generation pair.
_APPROVAL_TABLE_V2 = (
    "ogilvy-trends-v2.trends_v2_staging_approvals.open_intelligence_execution_approvals_v2"
)
_CONSUMPTION_TABLE_V2 = (
    "ogilvy-trends-v2.trends_v2_staging_approvals.open_intelligence_execution_consumptions_v2"
)
_RESULT_TABLE_V2 = (
    "ogilvy-trends-v2.trends_v2_staging_approvals.open_intelligence_execution_results_v2"
)
_APPROVAL_VERSION_V2 = "open_intelligence_execution_approval_v2"
_CONSUMPTION_VERSION_V2 = "open_intelligence_execution_consumption_v2"
_RESULT_VERSION_V2 = "open_intelligence_execution_result_v2"
_PAIR_MATCH = (
    "{left}.origin_registry_sha256={right}.origin_registry_sha256"
    " AND {left}.resource_manifest_sha256={right}.resource_manifest_sha256"
)
_RESULT_SQL_V2 = f"""WITH anchor AS (
 SELECT * FROM `{_CONSUMPTION_TABLE_V2}` WHERE consumption_id=@consumption_id
 AND consumption_contract_version='{_CONSUMPTION_VERSION_V2}' LIMIT 1
), consumptions AS (
 SELECT c.* FROM `{_CONSUMPTION_TABLE_V2}` c WHERE c.consumption_id=@consumption_id
 OR c.approval_id=(SELECT approval_id FROM anchor) OR c.manifest_sha256=(SELECT manifest_sha256 FROM anchor) OR c.execution_name=(SELECT execution_name FROM anchor)
), approvals AS (
 SELECT a.* FROM `{_APPROVAL_TABLE_V2}` a WHERE a.approval_id=(SELECT approval_id FROM anchor) OR a.manifest_sha256=(SELECT manifest_sha256 FROM anchor)
), results AS (
 SELECT r.* FROM `{_RESULT_TABLE_V2}` r WHERE r.consumption_id=@consumption_id
 OR r.approval_id=(SELECT approval_id FROM anchor) OR r.manifest_sha256=(SELECT manifest_sha256 FROM anchor)
 OR r.result_id IN (SELECT result_id FROM `{_RESULT_TABLE_V2}` WHERE consumption_id=@consumption_id)
)
SELECT (SELECT COUNT(*) FROM consumptions) AS consumption_count,
 (SELECT COUNT(*) FROM approvals) AS approval_count,(SELECT COUNT(*) FROM results) AS result_count,
 (SELECT TO_JSON_STRING(c) FROM consumptions c LIMIT 1) AS consumption_json,
 (SELECT TO_JSON_STRING(a) FROM approvals a JOIN anchor c ON a.approval_id=c.approval_id AND a.manifest_sha256=c.manifest_sha256 AND a.operation=c.operation AND {_PAIR_MATCH.format(left="a", right="c")} AND a.approval_contract_version='{_APPROVAL_VERSION_V2}' LIMIT 1) AS approval_json,
 (SELECT TO_JSON_STRING(r) FROM results r JOIN anchor c ON r.consumption_id=c.consumption_id AND r.approval_id=c.approval_id AND r.manifest_sha256=c.manifest_sha256 AND r.operation=c.operation AND r.execution_name=c.execution_name AND {_PAIR_MATCH.format(left="r", right="c")} AND r.result_contract_version='{_RESULT_VERSION_V2}' LIMIT 1) AS result_json"""
_APPROVAL_SQL_V2 = f"""WITH matching AS (
 SELECT * FROM `{_APPROVAL_TABLE_V2}` WHERE manifest_sha256=@manifest_sha256
 OR approval_id IN (SELECT approval_id FROM `{_APPROVAL_TABLE_V2}` WHERE manifest_sha256=@manifest_sha256)
) SELECT COUNT(*) AS approval_count,ARRAY_AGG(TO_JSON_STRING(matching) LIMIT 1)[SAFE_OFFSET(0)] AS approval_json FROM matching"""


def _authority_query(request, plan, intake, value, *, result, family="v1"):
    _validated_inputs(request, plan, intake)
    expression = r"exc_[0-9a-f]{64}" if result else r"[0-9a-f]{64}"
    if type(value) is not str or re.fullmatch(expression, value) is None:
        raise ValueError("context_query_invalid")
    if family == "v1":
        sql = _RESULT_SQL if result else _APPROVAL_SQL
    elif family == "v2":
        sql = _RESULT_SQL_V2 if result else _APPROVAL_SQL_V2
    else:
        raise ValueError("context_query_invalid")
    name = "consumption_id" if result else "manifest_sha256"
    parameters = (bigquery.ScalarQueryParameter(name, "STRING", value),)
    return PreparedContextQuery(
        f"protected_context_result_{family}" if result else f"protected_context_approval_{family}",
        sql,
        parameters,
        hashlib.sha256(sql.encode()).hexdigest(),
        canonical_digest(tuple(p.to_api_repr() for p in parameters)),
        0,
        1,
        canonical_digest({name: value}),
        (),
        None,
        None,
    )


def build_context_result_query(request, plan, intake, *, consumption_id):
    return _authority_query(request, plan, intake, consumption_id, result=True)


def build_context_result_query_v2(request, plan, intake, *, consumption_id):
    return _authority_query(request, plan, intake, consumption_id, result=True, family="v2")


def _context_consumption_ids(values):
    if (
        type(values) not in (list, tuple)
        or not 1 <= len(values) <= 3
        or any(
            type(value) is not str or re.fullmatch(r"exc_[0-9a-f]{64}", value) is None
            for value in values
        )
        or len(set(values)) != len(values)
    ):
        raise ValueError("context_authority_invalid")
    return tuple(sorted(values))


def build_context_result_batch_query(request, plan, intake, *, consumption_ids):
    _validated_inputs(request, plan, intake)
    identifiers = _context_consumption_ids(consumption_ids)
    parts, parameters = [], []
    for index, identifier in enumerate(identifiers):
        name = f"consumption_id_{index}"
        parameters.append(bigquery.ScalarQueryParameter(name, "STRING", identifier))
        selected = _RESULT_SQL.replace("@consumption_id", "@" + name)
        parts.append(
            f"SELECT @{name} AS requested_consumption_id, authority.* FROM ({selected}) authority"
        )
    sql = "\nUNION ALL\n".join(parts)
    parameters = tuple(parameters)
    return PreparedContextQuery(
        "protected_context_results_v1",
        sql,
        parameters,
        hashlib.sha256(sql.encode()).hexdigest(),
        canonical_digest(tuple(parameter.to_api_repr() for parameter in parameters)),
        0,
        len(identifiers),
        canonical_digest({"consumption_ids": identifiers}),
        (),
        None,
        None,
    )


def decode_context_result_batch_rows(rows, *, consumption_ids):
    identifiers = _context_consumption_ids(consumption_ids)
    if type(rows) not in (list, tuple) or len(rows) != len(identifiers):
        raise ValueError("context_authority_invalid")
    materials = {}
    for row in rows:
        if type(row) is not dict:
            raise ValueError("context_authority_invalid")
        identifier = row.get("requested_consumption_id")
        if type(identifier) is not str or identifier not in identifiers or identifier in materials:
            raise ValueError("context_authority_invalid")
        materials[identifier] = decode_context_result_rows(
            [{key: value for key, value in row.items() if key != "requested_consumption_id"}],
            consumption_id=identifier,
        )
    if set(materials) != set(identifiers):
        raise ValueError("context_authority_invalid")
    return materials


def build_context_approval_query(request, plan, intake, *, manifest_sha256):
    return _authority_query(request, plan, intake, manifest_sha256, result=False)


def build_context_approval_query_v2(request, plan, intake, *, manifest_sha256):
    return _authority_query(request, plan, intake, manifest_sha256, result=False, family="v2")


def _native(value, cls, timestamp_fields):
    row = _json(value)
    if set(row) != {field.name for field in fields(cls)}:
        raise ValueError("context_authority_invalid")
    for field in timestamp_fields:
        if type(row[field]) is not str:
            raise ValueError("context_authority_invalid")
        row[field] = datetime.fromisoformat(row[field].replace("Z", "+00:00"))
        if row[field].tzinfo is None:
            raise ValueError("context_authority_invalid")
    return row, cls(**row)


def _approval_row(rows):
    if (
        type(rows) not in (list, tuple)
        or len(rows) != 1
        or set(rows[0]) != {"approval_count", "approval_json"}
        or type(rows[0]["approval_count"]) is not int
        or rows[0]["approval_count"] != 1
    ):
        raise ValueError("context_authority_invalid")
    return rows[0]


def decode_context_approval_rows(rows, *, manifest_sha256):
    from src.analysis.open_intelligence.execution_approval import ExecutionApproval

    row = _approval_row(rows)
    registry = retained_origin_registry()
    _, approval = _native(row["approval_json"], ExecutionApproval, ("approved_at", "expires_at"))
    approval, _manifest = retained_v1_approval(
        approval, "context_authority_invalid", mode="historical_read", registry=registry
    )
    if (
        approval.manifest_sha256 != manifest_sha256
        or approval.operation != "source_snapshot_capture"
    ):
        raise ValueError("context_authority_invalid")
    return approval


def _generation_loader(generation_loader):
    if generation_loader is not None:
        return generation_loader
    from src.analysis.open_intelligence.execution_generations import load_trusted_generation

    return load_trusted_generation


def _native_v2(value, cls, timestamp_fields):
    row = _json(value)
    if set(row) != {field.name for field in fields(cls)}:
        raise ValueError("context_authority_invalid")
    for field in timestamp_fields:
        if type(row[field]) is not str:
            raise ValueError("context_authority_invalid")
        row[field] = datetime.fromisoformat(row[field].replace("Z", "+00:00"))
        if row[field].tzinfo is None:
            raise ValueError("context_authority_invalid")
    pair = (row["origin_registry_sha256"], row["resource_manifest_sha256"])
    if any(
        type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in pair
    ):
        raise ValueError("context_authority_invalid")
    return row, pair


def _trusted_generation(pair, generation_loader):
    from src.analysis.open_intelligence.execution_origins import OriginRegistry

    try:
        generation = _generation_loader(generation_loader)(pair[0], pair[1])
    except (LookupError, OSError, TypeError, ValueError) as error:
        raise ValueError("context_authority_invalid") from error
    registry = getattr(generation, "registry", None)
    if type(registry) is not OriginRegistry or registry.sha256 != pair[0]:
        raise ValueError("context_authority_invalid")
    return registry


def decode_context_approval_rows_v2(rows, *, manifest_sha256, operation, generation_loader=None):
    from src.analysis.open_intelligence.execution_approval import ExecutionApprovalV2

    row = _approval_row(rows)
    approval_row, pair = _native_v2(
        row["approval_json"], ExecutionApprovalV2, ("approved_at", "expires_at")
    )
    if type(operation) is not str or approval_row["operation"] != operation:
        raise ValueError("context_authority_invalid")
    registry = _trusted_generation(pair, generation_loader)
    try:
        approval = ExecutionApprovalV2(
            **approval_row,
            mode="historical_read",
            registry=registry,
            expected_resource_manifest_sha256=pair[1],
        )
    except (TypeError, ValueError) as error:
        raise ValueError("context_authority_invalid") from error
    if approval.manifest_sha256 != manifest_sha256:
        raise ValueError("context_authority_invalid")
    return {"approval": approval, "registry": registry, "generation": pair}


def _reviewed_late_failure(consumption, result):
    expected = {
        "manifest_sha256": "109ea182ba6f1b7042c76ecbea7d62cab753b3867cca57c909b422878b0a5433",
        "consumption_id": "exc_3178cc3f6011e245b21ade8f8677593f8cd23afedd7f23218ce13c209a2cbb47",
        "approval_id": "exa_9797927ec6012c50bbb811f49c8b92541e3e82f16a9842c4c4f73121c9c91924",
        "result_id": "exr_407b5ccbc5e2216f8e0582ea478af1cf9e726424c0aca5bb3ef0403ebc9f29bd",
        "result_digest": "466d2bafc12afd90fcc863ef300bb3f3b8875957a2e6d5d29c47ad3a8def2f23",
        "execution_name": "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging/executions/trends-engine-oi-source-snapshot-staging-v7k8f",
        "status": "failed",
        "completed_at": datetime(2026, 9, 8, 14, 4, 47, 839000, tzinfo=UTC),
    }
    if (
        any(getattr(result, field) != value for field, value in expected.items())
        or consumption.consumed_at != datetime(2026, 9, 8, 13, 30, 2, 162000, tzinfo=UTC)
        or hashlib.sha256(result.canonical_result_json.encode()).hexdigest() != result.result_digest
    ):
        return False
    payload = _json(result.canonical_result_json)
    return (
        type(payload) is dict
        and type(payload.get("query_count")) is int
        and payload["query_count"] == 0
        and all(
            field in payload and payload[field] is None
            for field in (
                "captured_at",
                "snapshot_digest",
                "capture_receipt_digest",
                "artifact_attempt",
                "stored_artifact",
            )
        )
    )


def _chain_row(rows):
    if (
        type(rows) not in (list, tuple)
        or len(rows) != 1
        or set(rows[0])
        != {
            "approval_count",
            "consumption_count",
            "result_count",
            "approval_json",
            "consumption_json",
            "result_json",
        }
    ):
        raise ValueError("context_authority_invalid")
    row = rows[0]
    if any(
        type(row[key]) is not int or row[key] != 1
        for key in ("approval_count", "consumption_count", "result_count")
    ):
        raise ValueError("context_authority_invalid")
    return row


def decode_context_result_rows_v2(rows, *, consumption_id, operation, generation_loader=None):
    from src.analysis.open_intelligence.execution_approval import (
        ExecutionApprovalV2,
        ExecutionConsumptionV2,
        ExecutionResultV2,
        validate_execution_chain_v2,
        validate_execution_manifest,
    )

    row = _chain_row(rows)
    approval_row, approval_pair = _native_v2(
        row["approval_json"], ExecutionApprovalV2, ("approved_at", "expires_at")
    )
    consumption_row, consumption_pair = _native_v2(
        row["consumption_json"], ExecutionConsumptionV2, ("consumed_at",)
    )
    result_row, result_pair = _native_v2(row["result_json"], ExecutionResultV2, ("completed_at",))
    if (
        type(operation) is not str
        or not approval_pair == consumption_pair == result_pair
        or approval_row["operation"] != operation
        or consumption_row["operation"] != operation
        or result_row["operation"] != operation
    ):
        raise ValueError("context_authority_invalid")
    registry = _trusted_generation(approval_pair, generation_loader)
    try:
        approval = ExecutionApprovalV2(
            **approval_row,
            mode="historical_read",
            registry=registry,
            expected_resource_manifest_sha256=approval_pair[1],
        )
        consumption = ExecutionConsumptionV2(
            **consumption_row,
            mode="historical_read",
            registry=registry,
            expected_resource_manifest_sha256=approval_pair[1],
        )
        result = ExecutionResultV2(
            **result_row,
            mode="historical_read",
            registry=registry,
            expected_resource_manifest_sha256=approval_pair[1],
        )
        validate_execution_chain_v2(
            approval,
            consumption,
            result,
            mode="historical_read",
            registry=registry,
            expected_resource_manifest_sha256=approval_pair[1],
        )
    except (TypeError, ValueError) as error:
        raise ValueError("context_authority_invalid") from error
    manifest = validate_execution_manifest(
        json.loads(approval.canonical_manifest_json), mode="historical_read", registry=registry
    )
    if (
        consumption.consumption_id != consumption_id
        or result.consumption_id != consumption_id
        or not approval.approved_at
        <= consumption.consumed_at
        < min(approval.expires_at, manifest.expires_at)
        or not consumption.consumed_at
        <= result.completed_at
        <= min(approval.expires_at, manifest.expires_at)
        or result.completed_at
        > consumption.consumed_at + timedelta(seconds=manifest.timeout_seconds)
    ):
        raise ValueError("context_authority_invalid")
    return {
        "approval": approval,
        "consumption": consumption,
        "result": result_row,
        "registry": registry,
        "generation": approval_pair,
    }


def decode_context_result_rows(rows, *, consumption_id):
    from src.analysis.open_intelligence.execution_approval import (
        ExecutionApproval,
        ExecutionConsumption,
        ExecutionResult,
    )

    row = _chain_row(rows)
    registry = retained_origin_registry()
    _, approval = _native(row["approval_json"], ExecutionApproval, ("approved_at", "expires_at"))
    _, consumption = _native(row["consumption_json"], ExecutionConsumption, ("consumed_at",))
    result_value, _ = _native(row["result_json"], ExecutionResult, ("completed_at",))
    result = retained_v1_result(
        result_value, "context_authority_invalid", mode="historical_read", registry=registry
    )
    approval, manifest = retained_v1_approval(
        approval, "context_authority_invalid", mode="historical_read", registry=registry
    )
    if (
        approval.operation != consumption.operation
        or result.operation != consumption.operation
        or approval.operation != "source_snapshot_capture"
        or consumption.consumption_id != consumption_id
        or result.consumption_id != consumption_id
        or approval.approval_id != consumption.approval_id
        or result.approval_id != consumption.approval_id
        or approval.manifest_sha256 != consumption.manifest_sha256
        or result.manifest_sha256 != consumption.manifest_sha256
        or result.execution_name != consumption.execution_name
        or consumption.job_resource != manifest.job_resource
        or consumption.source_sha != manifest.source_sha
        or consumption.image_uri != manifest.image_uri
        or result.result_reference != consumption.execution_name + "#source-snapshot"
        or not approval.approved_at
        <= consumption.consumed_at
        < min(approval.expires_at, manifest.expires_at)
        or not consumption.consumed_at
        <= result.completed_at
        <= min(approval.expires_at, manifest.expires_at)
        or (
            result.completed_at
            > consumption.consumed_at + timedelta(seconds=manifest.timeout_seconds)
            and not _reviewed_late_failure(consumption, result)
        )
    ):
        raise ValueError("context_authority_invalid")
    return {
        "approval": approval,
        "consumption": consumption,
        "result": result_value,
        "registry": registry,
    }


# Bridge v3 clone reads. Each read wraps one statement ``bridge_history_queries`` builds for
# the loaded capture, byte for byte, so it names only the pinned clone and the cells or
# window the loader admitted. The wrapper adds what Ask needs and nothing that widens the
# read: a JSON row projection, the plan's own search terms and a row cap one above the
# admitted limit, so an over limit read is seen as overflow and never silently truncated.
BRIDGE_HISTORY_TEMPLATE = "protected_context_bridge_history_v1"
BRIDGE_COLLECTION_TEMPLATE = "protected_context_bridge_collection_v1"
BRIDGE_TEMPLATES = frozenset({BRIDGE_HISTORY_TEMPLATE, BRIDGE_COLLECTION_TEMPLATE})
BRIDGE_COLLECTION_FIELDS = (
    "id",
    "market",
    "source",
    "source_family",
    "platform",
    "author_handle",
    "url",
    "title",
    "text",
    "published_at",
    "collected_at",
    "pipeline_run_id",
)


def _bridge_statement(source, plan, lane):
    from src.analysis.open_intelligence.bridge_history_queries import (
        bridge_collection_query,
        bridge_history_query,
    )
    from src.analysis.open_intelligence.source_estate_bridge import COLLECTION_TABLES
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        RECORDED_HISTORY_LANES,
    )

    window = {"window_start": plan["window"]["start"], "window_end": plan["window"]["end"]}
    if lane in RECORDED_HISTORY_LANES:
        return BRIDGE_HISTORY_TEMPLATE, bridge_history_query(source, lane, **window)
    if lane in COLLECTION_TABLES:
        return BRIDGE_COLLECTION_TEMPLATE, bridge_collection_query(source, lane, **window)
    raise ValueError("context_query_invalid")


def bridge_read_lanes(source, plan):
    """The clone lanes one Ask read of ``source`` covers, in lane order.

    Collection lanes are always read; a recorded history lane is read only where the
    loader admitted at least one completed cell inside the plan window.
    """
    from src.analysis.open_intelligence.source_estate_bridge import COLLECTION_TABLES
    from src.analysis.open_intelligence.source_estate_bridge import LANES as BRIDGE_LANES
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        RECORDED_HISTORY_LANES,
    )

    return tuple(
        lane
        for lane in BRIDGE_LANES
        if lane in COLLECTION_TABLES
        or (lane in RECORDED_HISTORY_LANES and _bridge_statement(source, plan, lane)[1] is not None)
    )


def _bridge_parameter(item):
    kind = item["parameterType"]["type"]
    if kind == "ARRAY":
        return bigquery.ArrayQueryParameter(
            item["name"],
            item["parameterType"]["arrayType"]["type"],
            [value["value"] for value in item["parameterValue"]["arrayValues"]],
        )
    if kind == "TIMESTAMP":
        value = datetime.fromisoformat(item["parameterValue"]["value"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("context_query_invalid")
        return bigquery.ScalarQueryParameter(item["name"], "TIMESTAMP", value)
    raise ValueError("context_query_invalid")


def build_bridge_context_query(request, plan, intake, source, *, lane, row_limit):
    """One capped read of one pinned bridge clone for the plan window and search terms."""
    from src.analysis.open_intelligence.bridge_history_queries import partition_column

    request, plan = _validated_inputs(request, plan, intake)
    if type(row_limit) is not int or not 1 <= row_limit <= 1000:
        raise ValueError("context_query_invalid")
    try:
        template, statement = _bridge_statement(source, plan, lane)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("context_query_invalid") from error
    if statement is None:
        raise ValueError("context_query_invalid")
    if template == BRIDGE_HISTORY_TEMPLATE:
        projection = "bridge_row"
        text = "LOWER(TO_JSON_STRING(bridge_row))"
    else:
        projection = (
            "STRUCT("
            + ", ".join(f"bridge_row.{name} AS {name}" for name in BRIDGE_COLLECTION_FIELDS)
            + ")"
        )
        text = "LOWER(CONCAT(IFNULL(bridge_row.title, ''), ' ', IFNULL(bridge_row.text, '')))"
    terms = sorted(
        {
            term.strip().lower()
            for requirement in _context_requirements(plan)
            for term in requirement["search_terms"]
            if term.strip()
        }
    )
    sql = (
        f"SELECT TO_JSON_STRING({projection}) AS row_json\n"
        f"FROM (\n{statement['sql']}\n) AS bridge_row\n"
        + (
            f"WHERE EXISTS(SELECT 1 FROM UNNEST(@ask_terms) AS term WHERE STRPOS({text}, term) > 0)\n"
            if terms
            else ""
        )
        + f"ORDER BY bridge_row.{partition_column(lane)} DESC, row_json\n"
        "LIMIT @ask_row_limit"
    )
    params = [_bridge_parameter(item) for item in statement["parameters"]]
    if terms:
        params.append(bigquery.ArrayQueryParameter("ask_terms", "STRING", terms))
    params.append(bigquery.ScalarQueryParameter("ask_row_limit", "INT64", row_limit + 1))
    return PreparedContextQuery(
        template,
        sql,
        tuple(params),
        hashlib.sha256(sql.encode()).hexdigest(),
        canonical_digest(tuple(item.to_api_repr() for item in params)),
        row_limit,
        row_limit + 1,
        canonical_digest(source.binding),
        (),
        source.binding["snapshot_digest"],
        source.profile_id,
    )


def decode_bridge_context_rows(rows, *, query):
    """The JSON rows of one bridge read, capped at its limit, with its overflow named."""
    if (
        query.template_id not in BRIDGE_TEMPLATES
        or type(rows) not in (list, tuple)
        or len(rows) > query.transport_row_limit
    ):
        raise ValueError("context_query_invalid")
    records = []
    for row in rows:
        if type(row) is not dict or set(row) != {"row_json"}:
            raise ValueError("context_query_invalid")
        records.append(_json(row["row_json"]))
    kept = records[: query.candidate_limit]
    return {
        "records": kept,
        "candidate_count": len(kept),
        "overflow": len(records) > query.candidate_limit,
    }
