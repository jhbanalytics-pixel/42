"""Execute fixed query routes; returned rows carry no source authority."""

from __future__ import annotations

import copy
import json
import math
import os
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from time import monotonic
from urllib.parse import urlparse
from uuid import uuid4

from google.api_core.exceptions import DeadlineExceeded, NotFound
from google.cloud import bigquery
from requests.exceptions import Timeout as HTTPTimeout

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_calls import GeneralQuestionCalls
from src.analysis.open_intelligence.general_question_context_queries import (
    BRIDGE_TEMPLATES,
    build_bridge_context_query,
    build_context_approval_query,
    build_context_candidate_query,
    build_context_evidence_query,
    build_context_index_query,
    build_context_partition_query,
    build_context_result_batch_query,
    build_context_result_query,
    decode_bridge_context_rows,
    decode_context_approval_rows,
    decode_context_partition_rows,
    decode_context_result_batch_rows,
    decode_context_result_rows,
    decode_context_rows,
)
from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    control_time,
    query_binding_digest,
    query_job_id,
    require_request_id,
    utc_time,
)
from src.analysis.open_intelligence.general_question_copy_validation import (
    build_current_source_copy_query,
    validate_current_source_copy_proof,
)
from src.analysis.open_intelligence.general_question_deployment import validate_question_invocation
from src.analysis.open_intelligence.general_question_parent_capsule import ParentSourceUnavailable
from src.analysis.open_intelligence.general_question_planning import validate_stored_question_plan
from src.analysis.open_intelligence.general_question_queries import (
    GeneralQuestionQueries,
    _require_no_observed_overage,
)
from src.analysis.open_intelligence.general_question_release_material import (
    build_release_material_query,
)
from src.analysis.open_intelligence.general_question_retrieval_queries import (
    build_released_candidate_query,
)
from src.analysis.open_intelligence.general_question_source_query import (
    build_selected_source_query,
    decode_selected_source_rows,
)

_PROJECT = "ogilvy-trends-v2"
_LOCATION = "US"
_ENDPOINT = "https://bigquery.googleapis.com"
_IDENTITY = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
_CONTEXT_DATA_ROUTES = frozenset(
    {
        "protected_context_candidates_v1",
        "protected_context_enriched_v1",
        "protected_context_raw_v1",
        "protected_context_enriched_index_v1",
        "protected_context_raw_index_v1",
        "protected_context_enriched_v2",
        "protected_context_raw_v2",
        "protected_context_enriched_index_v2",
        "protected_context_raw_index_v2",
        "protected_context_enriched_v3",
        "protected_context_raw_v3",
        "protected_context_enriched_index_v3",
        "protected_context_raw_index_v3",
        "protected_context_enriched_v4",
        "protected_context_raw_v4",
        "protected_context_enriched_index_v4",
        "protected_context_raw_index_v4",
    }
)
_CONTEXT_AUTH_ROUTES = frozenset(
    {
        "protected_context_result_v1",
        "protected_context_results_v1",
        "protected_context_approval_v1",
        "protected_context_partitions_v1",
    }
)


def _planning_context(invocation, store, scope, runtime_identity, plan):
    context = validate_question_invocation(
        invocation, store=store, scope=scope, runtime_identity=runtime_identity
    )
    calls = GeneralQuestionCalls(store)
    current, _, _ = calls._context(context["request"]["request_id"], scope)
    calls._budget(current, "answering")
    snapshot = calls.read_response(context["request"]["request_id"], stage="planning", scope=scope)
    stored = store._objects.read(f"requests/{context['request']['request_id']}/plan.json")
    if stored is None:
        raise QuestionStoreError("plan_invalid")
    try:
        from src.analysis.open_intelligence.general_question_parent_context import (
            read_parent_context,
        )
        from src.analysis.open_intelligence.general_question_runtime import (
            _validate_planned_draft,
        )

        # The draft revalidates under the adapter recorded on the planning call, exactly as
        # the planning stage validated it; a challenge_v5 draft carries evidence_purpose on
        # every requirement and the older key set alone would refuse it here.
        expected = _validate_planned_draft(
            snapshot,
            request=context["request"],
            intake=context["intake"],
            parent_context=read_parent_context(store, context["request"], context["intake"]),
        )
        saved = validate_stored_question_plan(
            stored.value, request=context["request"], intake=context["intake"]
        )
        if plan != expected or saved != expected:
            raise ValueError()
    except (TypeError, ValueError) as error:
        raise QuestionStoreError("plan_invalid") from error
    return context


def _configuration(query, reservation, context):
    duration = control_time(context["admission"]["deadline_at"]) - control_time(
        reservation["reserved_at"]
    )
    milliseconds = int(duration.total_seconds() * 1000)
    if milliseconds <= 0:
        raise QuestionStoreError("request_expired")
    config = bigquery.QueryJobConfig(
        use_legacy_sql=False,
        use_query_cache=False,
        dry_run=False,
        maximum_bytes_billed=reservation["maximum_bytes_billed"],
        query_parameters=list(query.parameters),
        job_timeout_ms=milliseconds,
    )
    requested = config.to_api_repr()
    requested["query"]["query"] = query.sql
    return config, requested


def _milliseconds(value):
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000


def _validated_job(job, spec, *, deadline, observed_at):
    if not isinstance(job, bigquery.QueryJob) or (
        job.project != _PROJECT
        or job.location != _LOCATION
        or job.job_id != spec["job_id"]
        or job.user_email != spec["creator_email"]
        or type(job.state) is not str
        or job.state not in {"PENDING", "RUNNING", "DONE"}
    ):
        raise QuestionStoreError("query_job_invalid")
    native = copy.deepcopy(job._properties)
    statistics = native.get("statistics")
    if type(statistics) is not dict:
        raise QuestionStoreError("query_job_time_invalid")
    try:
        raw_created = statistics.get("creationTime")
        if type(raw_created) is float:
            if not math.isfinite(raw_created) or not raw_created.is_integer():
                raise QuestionStoreError("query_job_time_invalid")
            created = int(raw_created)
        else:
            created = _native_count(raw_created)
    except QuestionStoreError as error:
        raise QuestionStoreError("query_job_time_invalid") from error
    if created is None or not (
        _milliseconds(control_time(spec["recorded_at"])) <= created < _milliseconds(deadline)
        and created <= _milliseconds(observed_at)
    ):
        raise QuestionStoreError("query_job_time_invalid")
    returned = copy.deepcopy(native.get("configuration"))
    expected = spec["requested_configuration"]
    if type(returned) is not dict or type(returned.get("query")) is not dict:
        raise QuestionStoreError("query_job_config_invalid")
    if "jobType" in returned:
        if type(returned["jobType"]) is not str or returned["jobType"] != "QUERY":
            raise QuestionStoreError("query_job_config_invalid")
        returned.pop("jobType")
    if "dryRun" not in returned and expected.get("dryRun") is False:
        returned["dryRun"] = False
    query = returned["query"]
    expected_query = expected["query"]
    has_destination = "destinationTable" in query
    destination = query.pop("destinationTable", None)
    if "destinationTable" in expected_query or (
        has_destination
        and (
            type(destination) is not dict
            or set(destination) != {"projectId", "datasetId", "tableId"}
            or destination["projectId"] != _PROJECT
            or type(destination["datasetId"]) is not str
            or not re.fullmatch(r"_[A-Za-z0-9_]{1,1023}", destination["datasetId"])
            or type(destination["tableId"]) is not str
            or not re.fullmatch(r"[A-Za-z0-9_]{1,1024}", destination["tableId"])
        )
    ):
        raise QuestionStoreError("query_destination_invalid")
    if (
        "queryParameters" not in query
        and expected_query.get("queryParameters") == []
        and "parameterMode" not in query
        and "parameterMode" not in expected_query
    ):
        query["queryParameters"] = []
    expected_parameters = expected_query.get("queryParameters")
    actual_parameters = query.get("queryParameters")
    empty_sql = "AS match_count FROM UNNEST(ARRAY<INT64>[]) AS empty_row WHERE FALSE"
    sql = expected_query.get("query", "")
    unused_requested = "WITH requested AS (\n SELECT @sample_markets[OFFSET(position)] AS market,@sample_ids[OFFSET(position)] AS id\n FROM UNNEST(GENERATE_ARRAY(0,ARRAY_LENGTH(@sample_ids)-1)) position\n), eligible AS (\n"
    direct_index = (
        sql.startswith(unused_requested)
        and "CAST(DATE(evidence.collected_at) AS STRING) AS collected_date" in sql
        and "DATE(evidence.collected_at) IN UNNEST(@eligible_partition_dates)" in sql
        and "FROM UNNEST(@search_terms) term" in sql
        and not re.search(
            r"\brequested\b|@sample_ids\b|@sample_markets\b",
            sql[len(unused_requested) :],
            re.IGNORECASE,
        )
        and type(expected_parameters) is list
        and any(
            p
            == {
                "name": "direct_content_fallback",
                "parameterType": {"type": "BOOL"},
                "parameterValue": {"value": "true"},
            }
            for p in expected_parameters
        )
    )
    seed_priority = "IF(EXISTS(SELECT 1 FROM requested linked WHERE linked.market=evidence.market AND linked.id=evidence.id),0,1) AS seed_priority"
    zero_tail_v4_index = (
        sql.startswith(unused_requested)
        and "SELECT DISTINCT * FROM (SELECT 'raw_content' AS lane," in sql
        and "CAST(NULL AS INT64) AS seed_priority FROM UNNEST(ARRAY<INT64>[]) AS empty_row WHERE FALSE UNION ALL SELECT 'raw_content' AS lane,"
        in sql
        and "CAST(DATE(evidence.collected_at) AS STRING) AS collected_date" in sql
        and "DATE(evidence.collected_at) IN UNNEST(@eligible_partition_dates)" in sql
        and "FROM UNNEST(@search_terms) term" in sql
        and " AND @parent_context_digest IS NOT NULL AND @context_geo_policy = 'corroborated_foreign_local_v1'"
        in sql
        and "(SELECT COUNT(*) FROM eligible) AS full_matching_count" in sql
        and "ORDER BY seed_priority,platform_round,term_relevance DESC,source_platform,market,id,payload LIMIT @candidate_limit"
        in sql
        and seed_priority in sql
        and not re.search(
            r"\brequested\b|@sample_ids\b|@sample_markets\b",
            sql[len(unused_requested) :].replace(seed_priority, "", 1),
            re.IGNORECASE,
        )
        and type(expected_parameters) is list
        and all(
            {"name": name, "parameterType": {"type": kind}, "parameterValue": {"value": value}}
            in expected_parameters
            for name, kind, value in (
                ("candidate_limit", "INT64", "0"),
                ("direct_content_fallback", "BOOL", "true"),
            )
        )
    )
    if (
        ((empty_sql in sql and "FROM `" not in sql) or direct_index or zero_tail_v4_index)
        and type(expected_parameters) is list
        and type(actual_parameters) is list
        and len(expected_parameters) == len(actual_parameters)
    ):
        for actual, requested in zip(actual_parameters, expected_parameters, strict=True):
            if (
                type(requested) is dict
                and type(actual) is dict
                and requested.get("name")
                in {
                    "sample_dates",
                    "selected_dates",
                    "sample_markets",
                    "sample_ids",
                    "eligible_partition_dates",
                    "search_terms",
                }
                and (
                    not (direct_index or zero_tail_v4_index)
                    or requested.get("name") in ("sample_ids", "sample_markets")
                )
                and (
                    not zero_tail_v4_index
                    or requested.get("parameterType")
                    == {"type": "ARRAY", "arrayType": {"type": "STRING"}}
                )
                and requested.get("parameterType", {}).get("type") == "ARRAY"
                and requested.get("parameterValue") == {"arrayValues": []}
                and actual
                == {key: value for key, value in requested.items() if key != "parameterValue"}
            ):
                actual["parameterValue"] = {"arrayValues": []}
    if "maximumBillingTier" not in expected_query and "maximumBillingTier" in query:
        if type(query["maximumBillingTier"]) is not int or query["maximumBillingTier"] != 1:
            raise QuestionStoreError("query_job_config_invalid")
        query.pop("maximumBillingTier")
    for field, default in (
        ("priority", "INTERACTIVE"),
        ("createDisposition", "CREATE_IF_NEEDED"),
        ("writeDisposition", "WRITE_EMPTY"),
    ):
        if field not in expected_query and query.get(field) == default:
            query.pop(field)
    if (
        "writeDisposition" not in expected_query
        and query.get("writeDisposition") == "WRITE_TRUNCATE"
        and has_destination
        and job.statement_type == "SELECT"
    ):
        query.pop("writeDisposition")
    if canonical_bytes(returned) != canonical_bytes(expected):
        raise QuestionStoreError("query_job_config_invalid")
    if job.state == "DONE" and job.error_result is None and job.statement_type != "SELECT":
        raise QuestionStoreError("query_statement_invalid")
    return native


def _native_count(value):
    if value is None:
        return None
    if type(value) is int and value >= 0:
        return value
    if type(value) is str and re.fullmatch(r"[0-9]{1,20}", value):
        return int(value)
    raise QuestionStoreError("query_stats_invalid")


def _execute_fixed_route(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    plan,
    ordinal=1,
    candidate_limit,
    maximum_bytes_billed,
    now,
    route="released_candidates_v1",
    bridge_source=None,
    bridge_lane=None,
    selected_ids=None,
    release_run_id=None,
    evidence_limit=200,
    recovery_only=False,
    source_binding=None,
    candidate_rows=None,
    enriched_rows=None,
    index_rows=None,
    source_consumption_id=None,
    source_consumption_ids=None,
    source_manifest_sha256=None,
    partition_rows=None,
    geo_policy=False,
    parent_context=None,
    continuity_policy=False,
    cap_version=None,
):
    started = monotonic()
    now = utc_time(now)
    context = _planning_context(invocation, store, scope, runtime_identity, plan)
    if route.startswith("protected_context_"):
        from src.analysis.open_intelligence.general_question_snapshot import (
            _CURRENT_CONTEXT_CAP_VERSION,
            _context_cap_set,
        )

        if cap_version is None:
            cap_version = _CURRENT_CONTEXT_CAP_VERSION
        try:
            _context_cap_set(cap_version)
        except ValueError as error:
            raise QuestionStoreError("snapshot_cap_version_unknown") from error
    if (
        runtime_identity.get("service_account_email") != _IDENTITY
        or getattr(credentials, "service_account_email", None) != _IDENTITY
    ):
        raise QuestionStoreError("identity_invalid")
    if any(
        os.environ.get(key)
        for key in ("BIGQUERY_EMULATOR_HOST", "GOOGLE_API_USE_CLIENT_CERTIFICATE")
    ) or os.environ.get("GOOGLE_API_USE_MTLS_ENDPOINT", "auto") not in ("auto", "never"):
        raise QuestionStoreError("query_environment_invalid")
    try:
        if route == "released_candidates_v1":
            query = build_released_candidate_query(
                context["request"], plan, context["intake"], candidate_limit=candidate_limit
            )
        elif route == "released_evidence_v1":
            query = build_selected_source_query(
                context["request"],
                selected_ids,
                plan_window=plan["window"],
                candidate_limit=candidate_limit,
                evidence_limit=evidence_limit,
            )
        elif route == "released_review_v1":
            query = build_release_material_query(release_run_id)
        elif route == "current_source_copy_v1":
            query = build_current_source_copy_query()
        elif route == "protected_context_candidates_v1":
            query = build_context_candidate_query(
                context["request"],
                plan,
                context["intake"],
                source_binding,
                candidate_limit=candidate_limit,
            )
        elif route == "protected_context_partitions_v1":
            query = build_context_partition_query(
                context["request"], plan, context["intake"], source_binding
            )
        elif route in _CONTEXT_DATA_ROUTES:
            builder = (
                build_context_index_query
                if route.endswith(("_index_v1", "_index_v2", "_index_v3", "_index_v4"))
                else build_context_evidence_query
            )
            query = builder(
                context["request"],
                plan,
                context["intake"],
                source_binding,
                candidate_rows=candidate_rows,
                enriched_rows=enriched_rows,
                lane="enriched_content" if "_enriched_" in route else "raw_content",
                candidate_limit=candidate_limit,
                **(
                    {}
                    if route.endswith(("_index_v1", "_index_v2", "_index_v3", "_index_v4"))
                    else {"index_rows": index_rows}
                ),
                partition_rows=partition_rows,
                geo_policy=geo_policy,
                parent_context=parent_context,
                continuity_policy=continuity_policy,
            )
        elif route == "protected_context_result_v1":
            query = build_context_result_query(
                context["request"], plan, context["intake"], consumption_id=source_consumption_id
            )
        elif route == "protected_context_results_v1":
            query = build_context_result_batch_query(
                context["request"], plan, context["intake"], consumption_ids=source_consumption_ids
            )
        elif route == "protected_context_approval_v1":
            query = build_context_approval_query(
                context["request"], plan, context["intake"], manifest_sha256=source_manifest_sha256
            )
        elif route in BRIDGE_TEMPLATES:
            query = build_bridge_context_query(
                context["request"],
                plan,
                context["intake"],
                bridge_source,
                lane=bridge_lane,
                row_limit=candidate_limit,
            )
            if query.template_id != route:
                raise ValueError("query_route_invalid")
        else:
            raise ValueError("query_route_invalid")
    except ParentSourceUnavailable as error:
        raise QuestionStoreError(error.code) from error
    except ValueError as error:
        raise QuestionStoreError("candidate_query_invalid") from error
    if (
        type(ordinal) is not int
        or not 1 <= ordinal <= 8
        or type(maximum_bytes_billed) is not int
        or not 0 <= maximum_bytes_billed <= 1000000000
    ):
        raise QuestionStoreError("query_invalid")
    deadline = control_time(context["admission"]["deadline_at"])

    def current_time():
        return now + timedelta(seconds=monotonic() - started)

    def remaining():
        seconds = (deadline - current_time()).total_seconds()
        if seconds <= 0:
            raise QuestionStoreError("request_expired")
        return min(5.0, seconds)

    queries = GeneralQuestionQueries(store)
    request_id = context["request"]["request_id"]
    prior = queries.read_query(request_id, scope=scope, ordinal=ordinal)
    key = f"requests/{request_id}/queries/{ordinal}/execution.json"
    stored = store._objects.read(key)
    if recovery_only and (
        prior is None
        or stored is None
        or prior["receipt"] is None
        or prior["receipt"]["query_state"] != "succeeded"
    ):
        raise QuestionStoreError("discovery_unavailable")
    core = {
        "contract_version": "general_question_query_reservation_v1"
        if cap_version is None
        else "general_question_query_reservation_v2",
        "request_id": request_id,
        "run_id": context["request"]["run_id"],
        **{
            field: context["admission"][field]
            for field in ("request_digest", "intake_digest", "policy_digest", "deployment_digest")
        },
        "ordinal": ordinal,
        "template_id": query.template_id,
        "sql_digest": query.sql_digest,
        "parameters_digest": query.parameters_digest,
        "maximum_bytes_billed": maximum_bytes_billed,
        **({"cap_version": cap_version} if cap_version is not None else {}),
        "candidate_limit": candidate_limit,
    }
    digest = query_binding_digest(core)
    job_id = query_job_id(request_id, ordinal, digest)
    if prior is not None and stored is None:
        return {
            "job_id": job_id,
            "application_status": "unavailable",
            "reason": "query_execution_unknown",
            "native_job_state": None,
            "billed_bytes": None,
            "total_rows": None,
            "rows": [],
            "receipt": None,
        }
    recorded_at = (
        stored.value.get("recorded_at")
        if stored is not None
        else current_time().isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    recorded = control_time(recorded_at)
    if not control_time(context["admission"]["admitted_at"]) <= recorded < deadline:
        raise QuestionStoreError("query_execution_invalid")
    config, requested = _configuration(
        query, {"maximum_bytes_billed": maximum_bytes_billed, "reserved_at": recorded_at}, context
    )
    expected = {
        "contract_version": "general_question_query_execution_v1",
        "request_id": request_id,
        "ordinal": ordinal,
        "job_id": job_id,
        "query_digest": digest,
        "plan_digest": plan["plan_digest"],
        "creator_email": _IDENTITY,
        "requested_configuration": requested,
    }
    owner = False
    if stored is None:
        current, _, control = queries._context(request_id, scope)
        queries._live(current, control, current_time())
        _require_no_observed_overage(current["admission"]["execution"]["queries"])
        remaining()
        proposal = {**expected, "owner_nonce": str(uuid4()), "recorded_at": recorded_at}
        try:
            stored = store._objects.create(key, proposal)
        except QuestionStoreError as error:
            if error.code != "run_id_conflict":
                raise
            stored = store._objects.read(key)
        owner = stored is not None and stored.value == proposal
    if stored is not None and stored.value.get("recorded_at") != recorded_at:
        recorded_at = stored.value["recorded_at"]
        recorded = control_time(recorded_at)
        if not control_time(context["admission"]["admitted_at"]) <= recorded < deadline:
            raise QuestionStoreError("query_execution_invalid")
        config, requested = _configuration(
            query,
            {"maximum_bytes_billed": maximum_bytes_billed, "reserved_at": recorded_at},
            context,
        )
        expected["requested_configuration"] = requested
    if (
        stored is None
        or set(stored.value) != set(expected) | {"owner_nonce", "recorded_at"}
        or canonical_bytes({field: stored.value[field] for field in expected})
        != canonical_bytes(expected)
    ):
        raise QuestionStoreError("query_execution_invalid")
    require_request_id(stored.value["owner_nonce"])
    spec = stored.value
    reservation = queries.reserve_query(
        request_id,
        scope=scope,
        ordinal=ordinal,
        template_id=query.template_id,
        sql_digest=query.sql_digest,
        parameters_digest=query.parameters_digest,
        maximum_bytes_billed=maximum_bytes_billed,
        **({"cap_version": cap_version} if cap_version is not None else {}),
        candidate_limit=candidate_limit,
        now=current_time(),
    )
    if reservation["job_id"] != job_id or reservation["query_digest"] != digest:
        raise QuestionStoreError("query_execution_invalid")
    job = None
    adopted = None
    rows = []
    total = None
    raw_total = None
    material = None
    result = {
        "job_id": job_id,
        "application_status": "unavailable",
        "reason": None,
        "native_job_state": None,
        "native_error_reason": None,
        "billed_bytes": None,
        "total_rows": None,
        "rows": [],
        "receipt": None,
    }
    client = bigquery.Client(
        project=_PROJECT,
        location=_LOCATION,
        credentials=credentials,
        default_query_job_config=None,
        client_options={"api_endpoint": _ENDPOINT},
    )
    post_allowed = [False]
    original_http = client._http.request

    def guarded_http(method, url, **kwargs):
        target = urlparse(url)
        if target.scheme != "https" or target.netloc != "bigquery.googleapis.com":
            raise QuestionStoreError("query_environment_invalid")
        kwargs["allow_redirects"] = False
        if method == "POST":
            if not post_allowed[0] or not target.path.endswith(f"/projects/{_PROJECT}/jobs"):
                raise QuestionStoreError("query_resubmission_refused")
            body = json.loads(kwargs["data"])
            if body["jobReference"] != {
                "jobId": job_id,
                "projectId": _PROJECT,
                "location": _LOCATION,
            } or canonical_bytes(body["configuration"]) != canonical_bytes(requested):
                raise QuestionStoreError("query_job_config_invalid")
            post_allowed[0] = False
            kwargs["timeout"] = remaining()
        elif method == "GET":
            if target.path not in {
                f"/bigquery/v2/projects/{_PROJECT}/jobs/{job_id}",
                f"/bigquery/v2/projects/{_PROJECT}/queries/{job_id}",
            }:
                raise QuestionStoreError("query_http_path_invalid")
            seconds = (deadline - current_time()).total_seconds()
            if "/queries/" in target.path and seconds <= 0:
                raise QuestionStoreError("request_expired")
            kwargs["timeout"] = min(5.0, seconds) if seconds > 0 else 5.0
        else:
            raise QuestionStoreError("query_http_method_invalid")
        return original_http(method, url, **kwargs)

    client._http.request = guarded_http
    try:
        if (
            client.project != _PROJECT
            or client.location != _LOCATION
            or client.default_query_job_config is not None
        ):
            raise QuestionStoreError("query_environment_invalid")
        try:
            job = client.get_job(
                job_id, project=_PROJECT, location=_LOCATION, retry=None, timeout=5
            )
        except NotFound:
            if owner:
                current, _, control = queries._context(request_id, scope)
                queries._live(current, control, current_time())
                _require_no_observed_overage(current["admission"]["execution"]["queries"])
                GeneralQuestionCalls(store)._budget(current, "answering")
                post_allowed[0] = True
                with suppress(Exception):
                    client.query(
                        query.sql,
                        job_config=config,
                        job_id=job_id,
                        project=_PROJECT,
                        location=_LOCATION,
                        retry=None,
                        job_retry=None,
                        timeout=remaining(),
                        api_method=bigquery.enums.QueryApiMethod.INSERT,
                    )
                post_allowed[0] = False
                job = client.get_job(
                    job_id, project=_PROJECT, location=_LOCATION, retry=None, timeout=5
                )
            else:
                raise QuestionStoreError("query_job_unknown") from None
        adopted = _validated_job(job, spec, deadline=deadline, observed_at=current_time())
        if job.error_result is not None:
            result["application_status"] = "refused"
            result["reason"] = "query_server_failed"
        else:
            iterator = job.result(
                retry=None,
                job_retry=None,
                timeout=remaining(),
                max_results=query.transport_row_limit,
            )
            if hasattr(iterator, "api_request"):
                original_request = iterator.api_request

                def bounded_page(**kwargs):
                    return original_request(**kwargs, timeout=remaining())

                iterator.api_request = bounded_page
            raw_total = job._query_results._properties.get("totalRows")
            total = _native_count(raw_total)
            if total != iterator.total_rows:
                raise QuestionStoreError("query_stats_invalid")
            for row in iterator:
                remaining()
                if len(rows) >= query.transport_row_limit:
                    raise QuestionStoreError("query_row_limit_exceeded")
                rows.append(dict(row))
            adopted = None
            job = client.get_job(
                job_id, project=_PROJECT, location=_LOCATION, retry=None, timeout=remaining()
            )
            adopted = _validated_job(job, spec, deadline=deadline, observed_at=current_time())
            if job.state != "DONE":
                raise QuestionStoreError("query_job_not_complete")
            if job.error_result is not None:
                raise QuestionStoreError("query_server_failed")
            if type(total) is not int or total < 0 or total != len(rows):
                raise QuestionStoreError("query_result_incomplete")
            if total > query.transport_row_limit:
                raise QuestionStoreError("query_row_limit_exceeded")
            try:
                result["rows"] = json.loads(canonical_bytes(rows))
            except (TypeError, ValueError, UnicodeError) as error:
                raise QuestionStoreError("query_result_invalid") from error
            if route == "released_evidence_v1":
                try:
                    material = decode_selected_source_rows(result["rows"], query=query)
                    inspected = material["candidate_count"]
                    if type(inspected) is not int or not 0 <= inspected <= candidate_limit:
                        raise ValueError()
                except (ValueError, TypeError, KeyError) as error:
                    raise QuestionStoreError("source_material_invalid") from error
                result["material"] = material
                result["missing_checks"] = copy.deepcopy(material["missing_checks"])
                result["source_authority"] = False
            elif route == "released_review_v1":
                if len(result["rows"]) != 1 or type(result["rows"][0]) is not dict:
                    raise QuestionStoreError("release_material_invalid")
                result["material"] = copy.deepcopy(result["rows"][0])
                result["source_authority"] = False
            elif route == "current_source_copy_v1":
                try:
                    result["material"] = validate_current_source_copy_proof(
                        result["rows"], query=query
                    )
                except (TypeError, ValueError, KeyError) as error:
                    raise QuestionStoreError("current_source_copy_invalid") from error
                result["source_authority"] = False
            elif route in _CONTEXT_DATA_ROUTES:
                try:
                    material = decode_context_rows(result["rows"], query=query)
                except ParentSourceUnavailable as error:
                    raise QuestionStoreError(error.code) from error
                except (ValueError, TypeError, KeyError) as error:
                    raise QuestionStoreError("context_material_invalid") from error
                result["material"] = material
                result["source_authority"] = False
            elif route in BRIDGE_TEMPLATES:
                try:
                    material = decode_bridge_context_rows(result["rows"], query=query)
                except (ValueError, TypeError, KeyError) as error:
                    raise QuestionStoreError("context_material_invalid") from error
                result["material"] = material
                result["source_authority"] = False
            elif route in _CONTEXT_AUTH_ROUTES:
                try:
                    if route == "protected_context_partitions_v1":
                        result["material"] = decode_context_partition_rows(
                            result["rows"], query=query
                        )
                    elif route == "protected_context_results_v1":
                        result["material"] = decode_context_result_batch_rows(
                            result["rows"], consumption_ids=source_consumption_ids
                        )
                    elif route == "protected_context_result_v1":
                        result["material"] = decode_context_result_rows(
                            result["rows"], consumption_id=source_consumption_id
                        )
                    else:
                        result["material"] = decode_context_approval_rows(
                            result["rows"], manifest_sha256=source_manifest_sha256
                        )
                except (ValueError, TypeError, KeyError) as error:
                    raise QuestionStoreError("context_authority_invalid") from error
                result["source_authority"] = False
            result["application_status"] = "accepted"
    except QuestionStoreError as error:
        result["application_status"] = (
            "unavailable"
            if error.code in {"query_job_unknown", "query_job_not_complete"}
            else "refused"
        )
        result["reason"] = error.code
    except NotFound:
        result["reason"] = "query_job_unknown"
    except (TimeoutError, HTTPTimeout, DeadlineExceeded) as error:
        result["reason"] = "query_timeout"
        result["error_type"] = type(error).__name__
    except (ValueError, TypeError, KeyError) as error:
        result["application_status"] = "refused"
        result["reason"] = "query_job_invalid"
        result["error_type"] = type(error).__name__
    except Exception as error:
        result["reason"] = "query_transport_unavailable"
        result["error_type"] = type(error).__name__
    finally:
        with suppress(Exception):
            client.close()
    if adopted is not None:
        try:
            adopted = _validated_job(job, spec, deadline=deadline, observed_at=current_time())
        except QuestionStoreError as error:
            adopted = None
            if result["reason"] in (None, "query_timeout", "query_transport_unavailable"):
                result["reason"] = error.code
            result["application_status"] = "refused"
    if adopted is None:
        rows, total, raw_total = [], None, None
        material = None
        result.pop("material", None)
        result["rows"] = []
    native = adopted or {}
    status = native.get("status", {})
    result["native_job_state"] = status.get("state")
    result["native_error_reason"] = (status.get("errorResult") or {}).get("reason")
    if result["native_error_reason"] is not None:
        result["application_status"] = "refused"
        result["reason"] = "query_server_failed"
    native_statistics = copy.deepcopy(native.get("statistics", {}).get("query", {}))
    try:
        measured = _native_count(native_statistics.get("totalBytesBilled"))
    except QuestionStoreError:
        measured = None
        result["application_status"] = "refused"
        result["reason"] = "query_stats_invalid"
    result["billed_bytes"] = measured
    result["total_rows"] = total
    observed_count = (
        (material["candidate_count"] if material is not None else None)
        if route == "released_evidence_v1"
        or route in _CONTEXT_DATA_ROUTES
        or route in BRIDGE_TEMPLATES
        else 0
        if route in ("released_review_v1", "current_source_copy_v1")
        or route in _CONTEXT_AUTH_ROUTES
        else (total if type(total) is int and total >= 0 else None)
    )
    result["observed_candidate_count"] = observed_count
    if (type(measured) is int and measured > maximum_bytes_billed) or (
        observed_count is not None and observed_count > candidate_limit
    ):
        result["application_status"] = "refused"
        result["reason"] = "query_policy_overage"
    if result["application_status"] == "accepted" and (type(measured) is not int or measured < 0):
        result["application_status"] = "unavailable"
        result["reason"] = "query_billing_unknown"
    observation = {
        "contract_version": "general_question_query_observation_v1",
        "request_id": request_id,
        "ordinal": ordinal,
        "job_id": job_id,
        "query_digest": digest,
        "requested_configuration": spec["requested_configuration"],
        "native_job_reference": native.get("jobReference"),
        "native_configuration": native.get("configuration"),
        "native_creation_time": native.get("statistics", {}).get("creationTime"),
        "native_job_state": result["native_job_state"],
        "native_error_reason": result["native_error_reason"],
        "native_statistics": native_statistics if adopted is not None else None,
        "native_total_rows": raw_total,
        "fetched_row_count": len(rows) if adopted is not None else None,
        "native_statement_type": native_statistics.get("statementType"),
        "native_creator_email": native.get("user_email"),
        "application_status": result["application_status"],
        "reason": result["reason"],
        "received_at": current_time().isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    observation_digest = canonical_digest(observation)
    observation_key = (
        f"requests/{request_id}/queries/{ordinal}/observations/{observation_digest}.json"
    )
    saved = store._objects.create(observation_key, observation)
    result["observation"] = {
        "object_key": observation_key,
        "generation": str(saved.generation),
        "digest": observation_digest,
    }
    receipt = {
        field: reservation[field]
        for field in ("job_id", "template_id", "sql_digest", "parameters_digest")
    }
    outcome = (
        "succeeded"
        if result["application_status"] == "accepted"
        else "failed"
        if result["application_status"] == "refused"
        else "running"
    )
    if result["native_job_state"] not in ("PENDING", "RUNNING", "DONE"):
        outcome = "running"
    receipt.update(
        query_state=outcome,
        billed_bytes=measured
        if outcome != "running" and type(measured) is int and measured >= 0
        else None,
        observed_candidate_count=observed_count if outcome != "running" else None,
        result_digest=canonical_digest(result["rows"]) if outcome == "succeeded" else None,
    )
    try:
        result["receipt"] = queries.record_query_receipt(
            request_id, scope=scope, ordinal=ordinal, receipt=receipt
        )
    except QuestionStoreError as error:
        if error.code != "query_receipt_conflict":
            raise
        result["receipt"] = queries.read_query(request_id, scope=scope, ordinal=ordinal)["receipt"]
        if result["application_status"] == "accepted":
            result["application_status"] = "refused"
            result["reason"] = "query_terminal_conflict"
    if result["application_status"] != "accepted":
        result["rows"] = []
        result.pop("material", None)
    if route in _CONTEXT_DATA_ROUTES or route in _CONTEXT_AUTH_ROUTES or route in BRIDGE_TEMPLATES:
        result["prepared_query"] = query
    return result


def execute_released_candidate_query(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    plan,
    ordinal=1,
    candidate_limit,
    maximum_bytes_billed,
    now,
):
    return _execute_fixed_route(
        invocation,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
        credentials=credentials,
        plan=plan,
        ordinal=ordinal,
        candidate_limit=candidate_limit,
        maximum_bytes_billed=maximum_bytes_billed,
        now=now,
    )


def execute_selected_source_query(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    plan,
    selected_ids,
    candidate_limit,
    evidence_limit,
    maximum_bytes_billed,
    now,
):
    started = monotonic()
    now = utc_time(now)
    context = _planning_context(invocation, store, scope, runtime_identity, plan)
    queries = GeneralQuestionQueries(store)
    request_id = context["request"]["request_id"]
    prior = queries.read_query(request_id, scope=scope, ordinal=1)
    if (
        prior is None
        or prior["reservation"]["template_id"] != "released_candidates_v1"
        or prior["receipt"] is None
        or prior["receipt"]["query_state"] != "succeeded"
    ):
        raise QuestionStoreError("discovery_unavailable")
    reservation = prior["reservation"]
    recovered = _execute_fixed_route(
        invocation,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
        credentials=credentials,
        plan=plan,
        ordinal=1,
        candidate_limit=reservation["candidate_limit"],
        maximum_bytes_billed=reservation["maximum_bytes_billed"],
        now=now + timedelta(seconds=monotonic() - started),
        recovery_only=True,
    )
    if (
        recovered["application_status"] != "accepted"
        or recovered["job_id"] != reservation["job_id"]
        or recovered["receipt"] != prior["receipt"]
        or canonical_digest(recovered["rows"]) != prior["receipt"]["result_digest"]
    ):
        raise QuestionStoreError("discovery_unavailable")
    observed = []
    for row in recovered["rows"]:
        if (
            type(row) is not dict
            or row.get("client_scope_id") != context["request"]["client_scope_id"]
            or row.get("market") not in plan["markets"]
        ):
            raise QuestionStoreError("discovery_invalid")
        identity = tuple(row.get(key) for key in ("run_id", "signal_id", "market"))
        if any(type(value) is not str or not value for value in identity):
            raise QuestionStoreError("discovery_invalid")
        observed.append(identity)
    if (
        len(set(observed)) != len(observed)
        or type(selected_ids) not in (list, tuple)
        or not selected_ids
    ):
        raise QuestionStoreError("selection_invalid")
    selected = []
    for item in selected_ids:
        if type(item) is not dict or set(item) != {"run_id", "signal_id", "market"}:
            raise QuestionStoreError("selection_invalid")
        identity = tuple(item[key] for key in ("run_id", "signal_id", "market"))
        if (
            any(type(value) is not str for value in identity)
            or identity not in observed
            or identity in selected
        ):
            raise QuestionStoreError("selection_invalid")
        selected.append(identity)
    selected.sort()
    result = _execute_fixed_route(
        invocation,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
        credentials=credentials,
        plan=plan,
        ordinal=2,
        candidate_limit=candidate_limit,
        maximum_bytes_billed=maximum_bytes_billed,
        now=now + timedelta(seconds=monotonic() - started),
        route="released_evidence_v1",
        selected_ids=[
            dict(zip(("run_id", "signal_id", "market"), item, strict=True)) for item in selected
        ],
        evidence_limit=evidence_limit,
    )
    result["discovery_result_digest"] = prior["receipt"]["result_digest"]
    result["discovery_job_id"] = reservation["job_id"]
    result["source_authority"] = False
    return result


def execute_release_material_query(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    plan,
    run_id,
    ordinal=3,
    maximum_bytes_billed,
    now,
):
    return _execute_fixed_route(
        invocation,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
        credentials=credentials,
        plan=plan,
        ordinal=ordinal,
        candidate_limit=0,
        maximum_bytes_billed=maximum_bytes_billed,
        now=now,
        route="released_review_v1",
        release_run_id=run_id,
    )


def execute_current_source_copy_query(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    plan,
    ordinal=8,
    maximum_bytes_billed,
    now,
):
    return _execute_fixed_route(
        invocation,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
        credentials=credentials,
        plan=plan,
        ordinal=ordinal,
        candidate_limit=0,
        maximum_bytes_billed=maximum_bytes_billed,
        now=now,
        route="current_source_copy_v1",
    )


def execute_context_candidate_query(invocation, *, source_binding, **kwargs):
    return _execute_fixed_route(
        invocation, route="protected_context_candidates_v1", source_binding=source_binding, **kwargs
    )


def _owned_context_rows(
    store,
    scope,
    request,
    plan,
    intake,
    source_binding,
    rows,
    *,
    lane=None,
    candidate_rows=None,
    index_rows=None,
    enriched_rows=None,
    index=False,
    partition_rows=None,
    partitions=False,
    geo_policy=False,
    parent_context=None,
    continuity_policy=False,
):
    _, control = store._control()
    saved = control["requests"][request["request_id"]]["execution"]["queries"]
    matches = []
    digest = canonical_digest(rows)
    for entry in saved.values():
        receipt = entry["receipt"]
        reservation = entry
        if (
            receipt is None
            or receipt["query_state"] != "succeeded"
            or receipt["result_digest"] != digest
        ):
            continue
        if partitions:
            query = build_context_partition_query(request, plan, intake, source_binding)
        elif lane is None:
            query = build_context_candidate_query(
                request,
                plan,
                intake,
                source_binding,
                candidate_limit=reservation["candidate_limit"],
            )
        else:
            builder = build_context_index_query if index else build_context_evidence_query
            query = builder(
                request,
                plan,
                intake,
                source_binding,
                candidate_rows=candidate_rows,
                lane=lane,
                candidate_limit=reservation["candidate_limit"],
                enriched_rows=enriched_rows,
                **({} if index else {"index_rows": index_rows}),
                partition_rows=partition_rows,
                geo_policy=geo_policy,
                parent_context=parent_context,
                continuity_policy=continuity_policy,
            )
        if (
            reservation["template_id"] == query.template_id
            and reservation["sql_digest"] == query.sql_digest
            and reservation["parameters_digest"] == query.parameters_digest
        ):
            matches.append(reservation["ordinal"])
    if len(matches) != 1:
        raise QuestionStoreError("context_predecessor_unavailable")


def _execute_context_evidence_query(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    plan,
    source_binding,
    candidate_rows,
    lane,
    ordinal,
    candidate_limit,
    maximum_bytes_billed,
    now,
    enriched_rows=None,
    index_rows=None,
    enriched_index_rows=None,
    _index=False,
    partition_rows=None,
    geo_policy=False,
    parent_context=None,
    continuity_policy=False,
    cap_version=None,
):
    started = monotonic()
    now = utc_time(now)
    context = _planning_context(invocation, store, scope, runtime_identity, plan)
    _owned_context_rows(
        store, scope, context["request"], plan, context["intake"], source_binding, candidate_rows
    )
    if partition_rows is not None:
        _owned_context_rows(
            store,
            scope,
            context["request"],
            plan,
            context["intake"],
            source_binding,
            partition_rows,
            partitions=True,
        )
    if lane == "raw_content":
        _owned_context_rows(
            store,
            scope,
            context["request"],
            plan,
            context["intake"],
            source_binding,
            enriched_rows,
            lane="enriched_content",
            candidate_rows=candidate_rows,
            index_rows=enriched_index_rows,
            partition_rows=partition_rows,
            geo_policy=geo_policy,
            parent_context=parent_context,
            continuity_policy=continuity_policy,
        )
    elif lane != "enriched_content":
        raise QuestionStoreError("context_query_invalid")
    if not _index:
        _owned_context_rows(
            store,
            scope,
            context["request"],
            plan,
            context["intake"],
            source_binding,
            index_rows,
            lane=lane,
            candidate_rows=candidate_rows,
            enriched_rows=enriched_rows,
            index=True,
            partition_rows=partition_rows,
            geo_policy=geo_policy,
            parent_context=parent_context,
            continuity_policy=continuity_policy,
        )
    return _execute_fixed_route(
        invocation,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
        credentials=credentials,
        plan=plan,
        ordinal=ordinal,
        candidate_limit=candidate_limit,
        maximum_bytes_billed=maximum_bytes_billed,
        cap_version=cap_version,
        now=now + timedelta(seconds=monotonic() - started),
        source_binding=source_binding,
        candidate_rows=candidate_rows,
        enriched_rows=enriched_rows,
        index_rows=index_rows,
        partition_rows=partition_rows,
        geo_policy=geo_policy,
        parent_context=parent_context,
        continuity_policy=continuity_policy,
        route=f"protected_context_{'enriched' if lane == 'enriched_content' else 'raw'}{'_index' if _index else ''}_v{4 if continuity_policy else 3 if geo_policy else 2 if partition_rows is not None and any(row.get('sample_row_ids') for row in candidate_rows) else 1}",
    )


def execute_context_evidence_query(*args, index_rows, **kwargs):
    return _execute_context_evidence_query(*args, index_rows=index_rows, **kwargs)


def execute_context_index_query(*args, **kwargs):
    return _execute_context_evidence_query(*args, _index=True, **kwargs)


def execute_context_result_query(invocation, *, consumption_id, **kwargs):
    return _execute_fixed_route(
        invocation,
        route="protected_context_result_v1",
        source_consumption_id=consumption_id,
        candidate_limit=0,
        **kwargs,
    )


def execute_context_approval_query(invocation, *, manifest_sha256, **kwargs):
    return _execute_fixed_route(
        invocation,
        route="protected_context_approval_v1",
        source_manifest_sha256=manifest_sha256,
        candidate_limit=0,
        **kwargs,
    )


def execute_context_result_batch_query(invocation, *, consumption_ids, **kwargs):
    return _execute_fixed_route(
        invocation,
        route="protected_context_results_v1",
        source_consumption_ids=consumption_ids,
        candidate_limit=0,
        **kwargs,
    )


def execute_context_partition_query(
    invocation,
    *,
    source_binding,
    candidate_rows,
    store,
    scope,
    runtime_identity,
    plan,
    now,
    **kwargs,
):
    started = monotonic()
    context = _planning_context(invocation, store, scope, runtime_identity, plan)
    _owned_context_rows(
        store, scope, context["request"], plan, context["intake"], source_binding, candidate_rows
    )
    return _execute_fixed_route(
        invocation,
        route="protected_context_partitions_v1",
        source_binding=source_binding,
        candidate_limit=0,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
        plan=plan,
        now=now + timedelta(seconds=monotonic() - started),
        **kwargs,
    )


def execute_context_bridge_query(invocation, *, bridge_source, lane, template_id, **kwargs):
    """One capped read of one pinned bridge clone through the owned query ledger."""
    if template_id not in BRIDGE_TEMPLATES:
        raise QuestionStoreError("query_route_invalid")
    return _execute_fixed_route(
        invocation,
        route=template_id,
        bridge_source=bridge_source,
        bridge_lane=lane,
        **kwargs,
    )
