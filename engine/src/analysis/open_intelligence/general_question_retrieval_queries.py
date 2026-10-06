"""Fixed query request for bounded discovery of released signal candidates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from google.cloud import bigquery

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_planning import (
    validate_stored_question_plan,
)
from src.analysis.open_intelligence.general_question_request import (
    QuestionRequestInvalid,
    validate_question_request,
)
from src.analysis.open_intelligence.run_receipts import RUN_RECEIPT_CONTRACT_VERSION
from src.contracts.open_intelligence import CONTRACT_VERSION

_PROJECT = "ogilvy-trends-v2"
_DATASET = "trends_v2_staging"
_TEMPLATE_ID = "released_candidates_v1"
_RELEASE_CONTRACT_VERSION = "open_intelligence_quality_release_v2"
_JOHANNESBURG = ZoneInfo("Africa/Johannesburg")
_SCOPE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
)
_COUNT_HINT_COLUMNS = (
    "candidate_count",
    "evidence_count",
    "membership_count",
    "lineage_count",
    "analysis_count",
    "prediction_count",
    "outcome_count_at_completion",
)
_RESULT_COLUMNS = (
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
    "cluster_signature",
    "cluster_build_version",
    "receipt_cluster_build_version",
    "discovery_mode",
    "topic_tags",
    "breadth_score",
    "evidence_state",
    "created_at",
    "label_member_identity",
    "run_contract_version",
    "observation_start",
    "observation_end",
    "observation_method",
    "source_window_digest",
    "source_family_map_version",
    "rule_version",
    "status",
    "complete_partitions",
    "display_release_state",
    "candidate_count",
    "evidence_count",
    "membership_count",
    "lineage_count",
    "analysis_count",
    "prediction_count",
    "outcome_count_at_completion",
    "row_set_digest",
    "source_sha",
    "completed_at",
    "run_receipt_digest",
    "candidate_projection_digest",
    "packet_digest",
    "review_receipt_digest",
    "approval_addendum_sha256",
    "released_at",
    "release_contract_version",
    "release_day_sast",
    "qualified_source_family_count",
    "qualified_evidence_count",
    "latest_evidence_published_at",
    "matched_term_count",
    "full_matched_candidate_count",
)
_RESULT_SELECT = ",\n  ".join(_RESULT_COLUMNS)

_SQL = f"""WITH
receipt_rows AS (
  SELECT
    run_id,
    client_scope_id,
    market_scope,
    signal_date,
    observation_start,
    observation_end,
    observation_method,
    source_window_digest,
    cluster_build_version,
    source_family_map_version,
    rule_version,
    status,
    complete_partitions,
    display_release_state,
    candidate_count,
    evidence_count,
    membership_count,
    lineage_count,
    analysis_count,
    prediction_count,
    row_set_digest,
    source_sha,
    completed_at,
    run_contract_version,
    COUNT(*) OVER (PARTITION BY run_id) AS run_identity_count
  FROM `{_PROJECT}.{_DATASET}.open_intelligence_run_receipts_v1`
),
receipts AS (
  SELECT * EXCEPT(run_identity_count)
  FROM receipt_rows
  WHERE run_contract_version = @run_contract_version
    AND run_identity_count = 1
    AND client_scope_id = @client_scope_id
    AND status = 'completed'
    AND complete_partitions
    AND display_release_state = 'enabled'
    AND observation_start <= observation_end
    AND signal_date = observation_end
    AND observation_start <= @window_end
    AND observation_end >= @window_start
    AND observation_end < @release_day_sast
    AND completed_at <= @as_of
),
release_rows AS (
  SELECT
    run_id,
    run_receipt_digest,
    source_window_digest,
    candidate_projection_digest,
    packet_digest,
    review_receipt_digest,
    approval_addendum_sha256,
    released_at,
    release_contract_version,
    DATE(released_at, 'Africa/Johannesburg') AS release_day_sast,
    COUNT(*) OVER (PARTITION BY run_id) AS release_identity_count
  FROM `{_PROJECT}.{_DATASET}.open_intelligence_quality_release_records_v2`
),
releases AS (
  SELECT * EXCEPT(release_identity_count)
  FROM release_rows
  WHERE release_contract_version = @release_contract_version
    AND release_identity_count = 1
    AND released_at <= @as_of
    AND DATE(released_at, 'Africa/Johannesburg') <= @release_day_sast
),
outcome_counts AS (
  SELECT
    o.client_scope_id,
    o.run_id,
    o.signal_date,
    COUNT(*) AS outcome_count_at_completion
  FROM `{_PROJECT}.{_DATASET}.signal_outcomes_v2` AS o
  JOIN receipts AS r
    ON o.client_scope_id = r.client_scope_id
    AND o.run_id = r.run_id
    AND o.signal_date = r.signal_date
  WHERE o.contract_version = @contract_version
    AND o.evaluated_at <= r.completed_at
  GROUP BY o.client_scope_id, o.run_id, o.signal_date
),
evidence_rollup AS (
  SELECT
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    brand_config_id,
    audience_lens_ids,
    theme_id,
    COUNT(DISTINCT source_family) AS qualified_source_family_count,
    COUNT(DISTINCT evidence_id) AS qualified_evidence_count,
    MAX(published_at) AS latest_evidence_published_at,
    STRING_AGG(
      LOWER(CONCAT(
        COALESCE(source_label, ''), ' ',
        COALESCE(metric_label, ''), ' ',
        COALESCE(excerpt, '')
      )),
      ' ' ORDER BY evidence_id
    ) AS observed_evidence_text
  FROM `{_PROJECT}.{_DATASET}.signal_evidence_v2`
  WHERE client_scope_id = @client_scope_id
    AND market IN UNNEST(@markets)
    AND contract_version = @contract_version
    AND availability = 'available'
    AND evidence_state = 'ready'
    AND published_at >= @publication_start
    AND published_at < @publication_end_exclusive
    AND published_at <= @as_of
    AND created_at <= @as_of
  GROUP BY
    client_scope_id,
    run_id,
    signal_date,
    market,
    signal_id,
    brand_config_id,
    audience_lens_ids,
    theme_id
),
eligible AS (
  SELECT
    c.client_scope_id,
    c.market_scope,
    c.brand_config_id,
    c.audience_lens_ids,
    c.theme_id,
    c.run_id,
    c.contract_version,
    c.signal_id,
    c.signal_date,
    c.market,
    c.label,
    c.cluster_signature,
    c.cluster_build_version,
    r.cluster_build_version AS receipt_cluster_build_version,
    c.discovery_mode,
    c.topic_tags,
    c.breadth_score,
    c.evidence_state,
    c.created_at,
    c.label_member_identity,
    r.run_contract_version,
    r.observation_start,
    r.observation_end,
    r.observation_method,
    r.source_window_digest,
    r.source_family_map_version,
    r.rule_version,
    r.status,
    r.complete_partitions,
    r.display_release_state,
    r.candidate_count,
    r.evidence_count,
    r.membership_count,
    r.lineage_count,
    r.analysis_count,
    r.prediction_count,
    COALESCE(o.outcome_count_at_completion, 0) AS outcome_count_at_completion,
    r.row_set_digest,
    r.source_sha,
    r.completed_at,
    q.run_receipt_digest,
    q.candidate_projection_digest,
    q.packet_digest,
    q.review_receipt_digest,
    q.approval_addendum_sha256,
    q.released_at,
    q.release_contract_version,
    q.release_day_sast,
    COALESCE(e.qualified_source_family_count, 0) AS qualified_source_family_count,
    COALESCE(e.qualified_evidence_count, 0) AS qualified_evidence_count,
    e.latest_evidence_published_at,
    COALESCE(e.observed_evidence_text, '') AS observed_evidence_text
  FROM `{_PROJECT}.{_DATASET}.signal_candidates_v2` AS c
  JOIN receipts AS r
    ON r.client_scope_id = c.client_scope_id
    AND r.run_id = c.run_id
    AND r.signal_date = c.signal_date
  JOIN releases AS q
    ON q.run_id = r.run_id
    AND q.source_window_digest = r.source_window_digest
  LEFT JOIN outcome_counts AS o
    ON o.client_scope_id = r.client_scope_id
    AND o.run_id = r.run_id
    AND o.signal_date = r.signal_date
  JOIN evidence_rollup AS e
    ON e.client_scope_id = c.client_scope_id
    AND e.run_id = c.run_id
    AND e.signal_date = c.signal_date
    AND e.market = c.market
    AND e.signal_id = c.signal_id
    AND e.brand_config_id IS NOT DISTINCT FROM c.brand_config_id
    AND TO_JSON_STRING(e.audience_lens_ids) = TO_JSON_STRING(c.audience_lens_ids)
    AND e.theme_id IS NOT DISTINCT FROM c.theme_id
  WHERE c.contract_version = @contract_version
    AND c.client_scope_id = @client_scope_id
    AND c.market IN UNNEST(@markets)
    AND c.created_at <= @as_of
    AND ARRAY_LENGTH(IFNULL(c.audience_lens_ids, ARRAY<STRING>[])) = 0
    AND c.evidence_state = 'ready'
    AND e.qualified_evidence_count > 0
),
scored AS (
  SELECT
    * EXCEPT(observed_evidence_text),
    (
      SELECT COUNT(*)
      FROM UNNEST(@search_terms) AS term
      WHERE STRPOS(
        LOWER(CONCAT(
          label, ' ',
          ARRAY_TO_STRING(IFNULL(topic_tags, ARRAY<STRING>[]), ' '), ' ',
          observed_evidence_text
        )),
        term
      ) > 0
    ) AS matched_term_count
  FROM eligible
),
matched AS (
  SELECT
    *,
    COUNT(*) OVER () AS full_matched_candidate_count
  FROM scored
  WHERE ARRAY_LENGTH(@search_terms) = 0 OR matched_term_count > 0
)
SELECT
  {_RESULT_SELECT},
  full_matched_candidate_count > @candidate_limit AS candidate_rows_truncated
FROM matched
ORDER BY
  matched_term_count DESC,
  qualified_source_family_count DESC,
  breadth_score DESC,
  latest_evidence_published_at DESC,
  signal_date DESC,
  released_at DESC,
  run_id,
  signal_id
LIMIT @candidate_limit"""


@dataclass(frozen=True, slots=True)
class ReleasedCandidateQuery:
    template_id: str
    sql: str
    parameters: tuple[bigquery.QueryParameter, ...]
    sql_digest: str
    parameters_digest: str
    candidate_limit: int
    transport_row_limit: int


def _validated_inputs(request, plan, intake):
    try:
        if not isinstance(request, dict):
            raise QuestionRequestInvalid()
        scope = {field: request[field] for field in _SCOPE_FIELDS}
        normalized_request = validate_question_request(
            request,
            scope=scope,
            policy_digest=request["policy_digest"],
        )
        normalized_plan = validate_stored_question_plan(
            plan,
            request=normalized_request,
            intake=intake,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("candidate_query_invalid") from exc
    if normalized_plan["status"] != "ready" or normalized_plan["window"] is None:
        raise ValueError("candidate_query_invalid")
    return normalized_request, normalized_plan


def _search_terms(plan):
    terms = []
    seen = set()
    supported = [
        requirement
        for requirement in plan["requirements"]
        if requirement["kind"] in ("content", "aggregate")
    ]
    if not supported:
        raise ValueError("candidate_query_invalid")
    for requirement in supported:
        for raw in requirement["search_terms"]:
            term = raw.strip().lower()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    if len(terms) > 96 or any(len(term) > 384 for term in terms):
        raise ValueError("candidate_query_invalid")
    return terms


def _parameters(request, plan, candidate_limit):
    window = plan["window"]
    as_of = datetime.fromisoformat(request["as_of"])
    publication_start = datetime.combine(date.fromisoformat(window["start"]), time.min, UTC)
    publication_end = datetime.combine(date.fromisoformat(window["end"]), time.min, UTC)
    publication_end_exclusive = publication_end + timedelta(days=1)
    return (
        bigquery.ScalarQueryParameter(
            "run_contract_version", "STRING", RUN_RECEIPT_CONTRACT_VERSION
        ),
        bigquery.ScalarQueryParameter(
            "release_contract_version", "STRING", _RELEASE_CONTRACT_VERSION
        ),
        bigquery.ScalarQueryParameter("contract_version", "STRING", CONTRACT_VERSION),
        bigquery.ScalarQueryParameter("client_scope_id", "STRING", request["client_scope_id"]),
        bigquery.ArrayQueryParameter("markets", "STRING", tuple(plan["markets"])),
        bigquery.ScalarQueryParameter("window_start", "DATE", date.fromisoformat(window["start"])),
        bigquery.ScalarQueryParameter("window_end", "DATE", date.fromisoformat(window["end"])),
        bigquery.ScalarQueryParameter("publication_start", "TIMESTAMP", publication_start),
        bigquery.ScalarQueryParameter(
            "publication_end_exclusive", "TIMESTAMP", publication_end_exclusive
        ),
        bigquery.ScalarQueryParameter("as_of", "TIMESTAMP", as_of),
        bigquery.ScalarQueryParameter(
            "release_day_sast", "DATE", as_of.astimezone(_JOHANNESBURG).date()
        ),
        bigquery.ArrayQueryParameter("search_terms", "STRING", tuple(_search_terms(plan))),
        bigquery.ScalarQueryParameter("candidate_limit", "INT64", candidate_limit),
    )


def build_released_candidate_query(
    request,
    plan,
    intake,
    *,
    candidate_limit: int,
) -> ReleasedCandidateQuery:
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 1000:
        raise ValueError("candidate_query_invalid")
    normalized_request, normalized_plan = _validated_inputs(request, plan, intake)
    try:
        parameters = _parameters(normalized_request, normalized_plan, candidate_limit)
        parameter_values = tuple(parameter.to_api_repr() for parameter in parameters)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("candidate_query_invalid") from exc
    return ReleasedCandidateQuery(
        template_id=_TEMPLATE_ID,
        sql=_SQL,
        parameters=parameters,
        sql_digest=hashlib.sha256(_SQL.encode("utf-8")).hexdigest(),
        parameters_digest=canonical_digest(parameter_values),
        candidate_limit=candidate_limit,
        transport_row_limit=candidate_limit,
    )


def select_released_candidates_for_inspection(rows, *, remaining_candidate_slots):
    if (
        type(rows) not in (list, tuple)
        or len(rows) > 1000
        or type(remaining_candidate_slots) is not int
        or not 0 <= remaining_candidate_slots <= 1000
    ):
        raise ValueError("inspection_selection_invalid")
    if not rows:
        return {
            "selected_ids": (),
            "selected_run_ids": (),
            "reserved_inspection_slots": 0,
            "omitted_runs": (),
            "discovery_truncated": False,
            "source_authority": False,
        }

    client_scope_id = None
    identities = set()
    full_counts = set()
    truncation = set()
    runs = {}
    for row in rows:
        if type(row) is not dict:
            raise ValueError("inspection_selection_invalid")
        identity = tuple(row.get(key) for key in ("run_id", "signal_id", "market"))
        scope = row.get("market_scope")
        client = row.get("client_scope_id")
        full_count = row.get("full_matched_candidate_count")
        truncated = row.get("candidate_rows_truncated")
        if (
            any(type(value) is not str or not value.strip() for value in identity)
            or type(client) is not str
            or not client.strip()
            or type(scope) not in (list, tuple)
            or not scope
            or any(type(value) is not str or not value for value in scope)
            or len(scope) != len(set(scope))
            or identity[2] not in scope
            or identity in identities
            or type(full_count) is not int
            or full_count < 0
            or type(truncated) is not bool
        ):
            raise ValueError("inspection_selection_invalid")
        if client_scope_id is None:
            client_scope_id = client
        elif client != client_scope_id:
            raise ValueError("inspection_selection_invalid")
        identities.add(identity)
        full_counts.add(full_count)
        truncation.add(truncated)
        group = runs.setdefault(identity[0], {"identities": [], "hints": []})
        group["identities"].append(identity)
        group["hints"].append(tuple(row.get(field) for field in _COUNT_HINT_COLUMNS))

    if (
        len(full_counts) != 1
        or len(truncation) != 1
        or next(iter(full_counts)) < len(rows)
        or next(iter(truncation)) is not (next(iter(full_counts)) > len(rows))
    ):
        raise ValueError("inspection_selection_invalid")

    selected_runs = []
    selected_run_set = set()
    omitted = []
    reserved = 0
    for run_id, group in runs.items():
        hint_rows = group["hints"]
        flattened = tuple(value for hints in hint_rows for value in hints)
        if any(value is None for value in flattened):
            reason = "count_hints_unknown"
            required = None
        elif any(type(value) is not int or value < 0 for value in flattened):
            reason = "count_hints_invalid"
            required = None
        elif any(hints != hint_rows[0] for hints in hint_rows[1:]):
            reason = "count_hints_inconsistent"
            required = None
        elif len(group["identities"]) > hint_rows[0][0]:
            reason = "candidate_count_exceeded"
            required = None
        else:
            required = sum(hint_rows[0])
            reason = None
        if reason is None and reserved + required <= remaining_candidate_slots:
            selected_runs.append(run_id)
            selected_run_set.add(run_id)
            reserved += required
            continue
        omitted.append(
            {
                "run_id": run_id,
                "reason": reason or "insufficient_inspection_slots",
                "required_inspection_slots": required,
            }
        )

    selected_ids = tuple(
        {key: row[key] for key in ("run_id", "signal_id", "market")}
        for row in rows
        if row["run_id"] in selected_run_set
    )
    return {
        "selected_ids": selected_ids,
        "selected_run_ids": tuple(selected_runs),
        "reserved_inspection_slots": reserved,
        "omitted_runs": tuple(omitted),
        "discovery_truncated": next(iter(truncation)),
        "source_authority": False,
    }


__all__ = [
    "ReleasedCandidateQuery",
    "build_released_candidate_query",
    "select_released_candidates_for_inspection",
]
