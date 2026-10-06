"""Collect bounded native snapshot rows and receipts, without publication authority."""

import copy
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from google.cloud import bigquery

from src.analysis.open_intelligence import pipeline
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_control import control_time
from src.analysis.open_intelligence.general_question_query_execution import (
    _native_count,
    _validated_job,
)
from src.analysis.open_intelligence.production_snapshot import (
    _deserialize_plan,
    _scope,
    _utc_timestamp,
    build_production_snapshot,
    validate_prepared_production_snapshot,
)
from src.analysis.open_intelligence.production_snapshot_client import _CaptureQuery
from src.analysis.open_intelligence.production_snapshot_rows import (
    _normalize_lane,
    _sort_rows,
    normalize_snapshot_rows,
    physical_fields,
)
from src.analysis.open_intelligence.production_snapshot_tables import (
    LANES,
    PROJECT,
    _reviewed_source_metadata,
    build_snapshot_plan,
    validate_snapshot_readback,
)


def _utc_now():
    return datetime.now(UTC)


class SnapshotCaptureError(ValueError):
    def __init__(self, receipts):
        super().__init__("snapshot_capture_refused")
        self.query_receipts = copy.deepcopy(receipts)


def _query(lane, table, cutoff, markets, keys):
    capture = _CaptureQuery()
    if lane in pipeline.EVIDENCE_COLUMNS_BY_TABLE:
        pipeline._load_evidence_rows(
            capture,
            table=lane,
            keys=keys,
            start_date=cutoff - timedelta(days=6),
            end_date=cutoff,
        )
        logical = ", ".join(
            f"evidence.`{field}`" for field in pipeline.EVIDENCE_COLUMNS_BY_TABLE[lane]
        )
        physical = ", ".join(f"evidence.`{field}`" for field in physical_fields(lane))
        sql = capture.sql.replace(logical, physical)
        ceiling = len(keys) + 1
    else:
        pipeline._load_source(
            capture,
            table=lane,
            columns=physical_fields(lane),
            date_column="proposed_date" if lane == "seed_candidates" else "trend_date",
            trend_date=cutoff,
            markets=markets,
            statuses=tuple(sorted(pipeline.ACCEPTED_SEED_CANDIDATE_STATUSES))
            if lane == "seed_candidates"
            else None,
        )
        sql, ceiling = capture.sql, pipeline.SOURCE_CEILINGS[lane]
    sql = sql.replace(
        f"\nFROM `{PROJECT}.trends_v2_staging.{lane}`",
        f", COUNT(*) OVER() AS capture_row_count\nFROM `{table}`",
    )
    return sql, capture.config, ceiling


def capture_production_snapshot(
    cutoff_date,
    *,
    client_scope_id,
    market_scope,
    reviewed_metadata,
    delegate,
    expected_creator_email,
    coverage_receipt_refs=(),
):
    receipts = []
    started, start_time = monotonic(), _utc_now()
    deadline = start_time + timedelta(seconds=180)

    def current():
        return start_time + timedelta(seconds=monotonic() - started)

    def remaining():
        seconds = 180 - (monotonic() - started)
        if seconds <= 0:
            raise ValueError("snapshot_capture_deadline")
        return seconds

    try:
        client_scope_id, markets = _scope(client_scope_id, market_scope)
        if (
            getattr(delegate, "project", None) != PROJECT
            or not all(
                callable(getattr(delegate, name, None))
                for name in ("get_table", "query", "get_job")
            )
            or type(expected_creator_email) is not str
            or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", expected_creator_email) is None
        ):
            raise ValueError("snapshot_capture_context_invalid")
        plan = build_snapshot_plan(cutoff_date, now=start_time, source_metadata=reviewed_metadata)
        source_schema, _ = _reviewed_source_metadata(reviewed_metadata)
        tables = {item.lane: item.destination_table for item in plan.statements}
        metadata, readback = {}, []
        for lane in LANES:
            resource = delegate.get_table(
                tables[lane], retry=None, timeout=remaining()
            ).to_api_repr()
            if resource.get("location") != "US":
                raise ValueError("snapshot_capture_location_invalid")
            count = _native_count(resource.get("numRows"))
            if count is None:
                raise ValueError("snapshot_capture_count_unknown")
            metadata[lane] = resource
            readback.append(
                {"lane": lane, "table_id": tables[lane].rsplit(".", 1)[1], "row_count": count}
            )
        validate_snapshot_readback(
            plan,
            source_metadata=reviewed_metadata,
            snapshot_metadata=metadata,
            readback_rows=readback,
        )
        rows_by_table = {lane: [] for lane in LANES}
        billed_total = 0
        capture_id = uuid4().hex

        def normalize():
            return normalize_snapshot_rows(
                cutoff_date,
                source_as_of=plan.source_as_of,
                captured_at=current(),
                market_scope=markets,
                reviewed_metadata=reviewed_metadata,
                rows_by_table=rows_by_table,
            )

        def read(lane, keys=()):
            nonlocal billed_total
            if len(receipts) >= 5 or billed_total >= 1_000_000_000:
                raise ValueError("snapshot_capture_budget_exhausted")
            sql, config, ceiling = _query(lane, tables[lane], cutoff_date, markets, keys)
            config.use_query_cache = False
            config.dry_run = False
            config.maximum_bytes_billed = 1_000_000_000 - billed_total
            config.job_timeout_ms = max(1, int(remaining() * 1000))
            requested = config.to_api_repr()
            requested["query"]["query"] = sql
            record = {
                "lane": lane,
                "snapshot_table": tables[lane],
                "project": PROJECT,
                "location": "US",
                "job_id": f"oi_v3_capture_{capture_id}_{len(receipts) + 1}",
                "creator_email": expected_creator_email,
                "recorded_at": current().isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "requested_configuration": requested,
                "sql_digest": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                "configuration_digest": canonical_digest(requested),
                "total_bytes_billed": None,
                "status": "unresolved",
                "execution_authority": False,
            }
            receipts.append(record)
            spec = {
                key: record[key]
                for key in ("job_id", "creator_email", "recorded_at", "requested_configuration")
            }
            job = delegate.query(
                sql,
                job_config=config,
                job_id=record["job_id"],
                location="US",
                retry=None,
                job_retry=None,
                timeout=remaining(),
                api_method=bigquery.enums.QueryApiMethod.INSERT,
            )
            native = _validated_job(job, spec, deadline=deadline, observed_at=current())
            record.update(
                native_job=native,
                native_job_digest=canonical_digest(native),
                total_bytes_billed=_native_count(
                    native["statistics"].get("query", {}).get("totalBytesBilled")
                ),
            )
            if job.error_result is not None:
                raise ValueError("snapshot_capture_job_failed")
            iterator = job.result(
                retry=None, job_retry=None, timeout=remaining(), max_results=ceiling + 1
            )
            if hasattr(iterator, "api_request"):
                request = iterator.api_request
                iterator.api_request = lambda **kwargs: request(**kwargs, timeout=remaining())
            query_result = job._query_results._properties
            if query_result.get("jobReference") != {
                "projectId": PROJECT,
                "location": "US",
                "jobId": record["job_id"],
            }:
                raise ValueError("snapshot_capture_result_identity_invalid")
            total = _native_count(query_result.get("totalRows"))
            if total is None or total != iterator.total_rows or total > ceiling:
                raise ValueError("snapshot_capture_result_incomplete")
            values = []
            for row in iterator:
                remaining()
                if len(values) >= ceiling:
                    raise ValueError("snapshot_capture_result_incomplete")
                values.append(pipeline._row_mapping(row))
            final_job = delegate.get_job(
                record["job_id"], project=PROJECT, location="US", retry=None, timeout=remaining()
            )
            native = _validated_job(final_job, spec, deadline=deadline, observed_at=current())
            if final_job.state != "DONE" or final_job.error_result is not None:
                raise ValueError("snapshot_capture_job_failed")
            billed = _native_count(native["statistics"].get("query", {}).get("totalBytesBilled"))
            record.update(native_job=native, total_bytes_billed=billed)
            if billed is None or billed > config.maximum_bytes_billed:
                raise ValueError("snapshot_capture_billing_unavailable")
            billed_total += billed
            if len(values) != total:
                raise ValueError("snapshot_capture_result_incomplete")
            extras = ["match_count"] if lane in pipeline.EVIDENCE_COLUMNS_BY_TABLE else []
            expected_fields = list(physical_fields(lane)) + extras + ["capture_row_count"]
            schema = {field["name"]: field for field in source_schema[lane]["schema"]["fields"]}
            actual_schema = [field.to_api_repr() for field in iterator.schema]
            if [field["name"] for field in actual_schema] != expected_fields:
                raise ValueError("snapshot_capture_schema_invalid")
            aliases = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
            for field in actual_schema:
                expected = schema.get(field["name"], {"type": "INT64", "mode": "NULLABLE"})
                if field.get("mode", "NULLABLE") not in {"NULLABLE", "REQUIRED", "REPEATED"}:
                    raise ValueError("snapshot_capture_schema_invalid")
                if aliases.get(field["type"], field["type"]) != expected["type"] or (
                    (field.get("mode", "NULLABLE") == "REPEATED")
                    != (expected["mode"] == "REPEATED")
                ):
                    raise ValueError("snapshot_capture_schema_invalid")
            counts = Counter((row.get("market"), row.get("id")) for row in values)
            physical = []
            for row in values:
                if (
                    set(row) != set(expected_fields)
                    or type(row["capture_row_count"]) is not int
                    or row["capture_row_count"] != total
                ):
                    raise ValueError("snapshot_capture_filtered_count_invalid")
                if extras and (
                    type(row["match_count"]) is not int
                    or row["match_count"] != counts[(row["market"], row["id"])]
                ):
                    raise ValueError("snapshot_capture_match_count_invalid")
                physical.append({field: row[field] for field in physical_fields(lane)})
            rows_by_table[lane] = physical
            record.update(
                status="observed",
                filtered_row_count=total,
                fetched_row_count=len(values),
                result_digest=canonical_digest(physical),
                result_rows=json.loads(canonical_bytes(physical)),
                result_readback={
                    "jobReference": copy.deepcopy(query_result["jobReference"]),
                    "jobComplete": query_result.get("jobComplete"),
                    "totalRows": query_result["totalRows"],
                    "schema": actual_schema,
                    "capture_row_counts": [row["capture_row_count"] for row in values],
                    "match_counts": [row["match_count"] for row in values] if extras else [],
                },
                native_job_digest=canonical_digest(native),
                observed_at=current().isoformat(),
            )

        for lane in LANES[:3]:
            read(lane)
        candidates = normalize()
        keys = tuple(tuple(key) for key in candidates["requested_sample_keys"])
        if keys:
            read("enriched_content", keys)
            enriched = normalize()
            fallback = tuple(tuple(key) for key in enriched["missing_refs"])
            if fallback:
                read("raw_content", fallback)
        assembly = build_production_snapshot(
            cutoff_date=cutoff_date,
            captured_at=current(),
            client_scope_id=client_scope_id,
            market_scope=markets,
            reviewed_metadata=reviewed_metadata,
            plan=plan,
            snapshot_metadata=metadata,
            readback_rows=readback,
            rows_by_table=rows_by_table,
            coverage_receipt_refs=coverage_receipt_refs,
        )
        return {
            "assembly": assembly,
            "query_receipts": copy.deepcopy(receipts),
            "query_count": len(receipts),
            "total_bytes_billed": billed_total,
            "source_authority": False,
            "execution_authority": False,
        }
    except Exception as error:
        raise SnapshotCaptureError(receipts) from error


def validate_captured_production_snapshot(
    value, *, cutoff_date, client_scope_id, market_scope, reviewed_metadata, expected_creator_email
):
    """Reconstruct recorded observations only; no host or persistence attestation."""
    try:
        if (
            type(value) is not dict
            or set(value)
            != {
                "assembly",
                "query_receipts",
                "query_count",
                "total_bytes_billed",
                "source_authority",
                "execution_authority",
            }
            or value["source_authority"] is not False
            or value["execution_authority"] is not False
        ):
            raise ValueError()
        if (
            type(expected_creator_email) is not str
            or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", expected_creator_email) is None
        ):
            raise ValueError()
        client_scope_id, markets = _scope(client_scope_id, market_scope)
        assembly = validate_prepared_production_snapshot(
            value["assembly"],
            cutoff_date=cutoff_date,
            client_scope_id=client_scope_id,
            market_scope=markets,
        )
        if (
            type(reviewed_metadata) is not bytes
            or assembly["source_metadata_utf8"].encode("utf-8") != reviewed_metadata
        ):
            raise ValueError()
        captured_at = _utc_timestamp(assembly["snapshot"]["captured_at"])
        plan = _deserialize_plan(assembly["snapshot_plan"])
        schemas, _ = _reviewed_source_metadata(reviewed_metadata)
        tables = {item.lane: item.destination_table for item in plan.statements}
        receipts = value["query_receipts"]
        if (
            type(receipts) is not list
            or not 3 <= len(receipts) <= 5
            or (type(value["query_count"]) is not int or value["query_count"] != len(receipts))
        ):
            raise ValueError()
        rows_by_table = {lane: [] for lane in LANES}
        total_billed, index = 0, 0
        first_recorded = control_time(receipts[0]["recorded_at"])
        if not plan.source_as_of <= first_recorded <= plan.source_as_of + timedelta(days=7):
            raise ValueError()
        previous_observed, capture_id = first_recorded, None
        deadline = first_recorded + timedelta(seconds=180)
        if captured_at > deadline:
            raise ValueError()

        def accept(lane, keys=()):
            nonlocal total_billed, index, previous_observed, capture_id
            if index >= len(receipts):
                raise ValueError()
            receipt = receipts[index]
            index += 1
            if type(receipt) is not dict or set(receipt) != {
                "lane",
                "snapshot_table",
                "project",
                "location",
                "job_id",
                "creator_email",
                "recorded_at",
                "requested_configuration",
                "sql_digest",
                "configuration_digest",
                "total_bytes_billed",
                "status",
                "execution_authority",
                "native_job",
                "native_job_digest",
                "filtered_row_count",
                "fetched_row_count",
                "result_digest",
                "result_rows",
                "result_readback",
                "observed_at",
            }:
                raise ValueError()
            if (
                receipt["lane"] != lane
                or receipt["snapshot_table"] != tables[lane]
                or receipt["project"] != PROJECT
                or receipt["location"] != "US"
                or receipt["creator_email"] != expected_creator_email
                or receipt["status"] != "observed"
                or receipt["execution_authority"] is not False
            ):
                raise ValueError()
            match = re.fullmatch(r"oi_v3_capture_([0-9a-f]{32})_([1-5])", receipt["job_id"])
            if match is None or int(match[2]) != index:
                raise ValueError()
            if capture_id is None:
                capture_id = match[1]
            if capture_id != match[1]:
                raise ValueError()
            recorded = control_time(receipt["recorded_at"])
            observed = _utc_timestamp(receipt["observed_at"])
            if not previous_observed <= recorded <= observed <= captured_at:
                raise ValueError()
            previous_observed = observed
            sql, config, ceiling = _query(lane, tables[lane], cutoff_date, markets, keys)
            timeout = receipt["requested_configuration"].get("jobTimeoutMs")
            if (
                type(timeout) is not str
                or re.fullmatch(r"[1-9][0-9]{0,5}", timeout) is None
                or int(timeout) > 180000
            ):
                raise ValueError()
            if total_billed >= 1_000_000_000:
                raise ValueError()
            config.use_query_cache, config.dry_run = False, False
            config.maximum_bytes_billed = 1_000_000_000 - total_billed
            config.job_timeout_ms = int(timeout)
            expected_config = config.to_api_repr()
            expected_config["query"]["query"] = sql
            if (
                canonical_bytes(receipt["requested_configuration"])
                != canonical_bytes(expected_config)
                or receipt["configuration_digest"] != canonical_digest(expected_config)
                or receipt["sql_digest"] != hashlib.sha256(sql.encode("utf-8")).hexdigest()
                or receipt["native_job_digest"] != canonical_digest(receipt["native_job"])
            ):
                raise ValueError()
            job = bigquery.QueryJob.from_api_repr(
                copy.deepcopy(receipt["native_job"]), SimpleNamespace(project=PROJECT)
            )
            native = _validated_job(job, receipt, deadline=deadline, observed_at=observed)
            if job.state != "DONE" or job.error_result is not None:
                raise ValueError()
            billed = _native_count(native["statistics"].get("query", {}).get("totalBytesBilled"))
            if (
                billed is None
                or type(receipt["total_bytes_billed"]) is not int
                or receipt["total_bytes_billed"] != billed
                or billed > config.maximum_bytes_billed
            ):
                raise ValueError()
            total_billed += billed
            rows = receipt["result_rows"]
            if (
                type(rows) is not list
                or len(rows) > ceiling
                or receipt["result_digest"] != canonical_digest(rows)
            ):
                raise ValueError()
            for field in ("filtered_row_count", "fetched_row_count"):
                if type(receipt[field]) is not int or receipt[field] != len(rows):
                    raise ValueError()
            readback = receipt["result_readback"]
            if (
                type(readback) is not dict
                or set(readback)
                != {
                    "jobReference",
                    "jobComplete",
                    "totalRows",
                    "schema",
                    "capture_row_counts",
                    "match_counts",
                }
                or readback["jobReference"]
                != {
                    "projectId": PROJECT,
                    "location": "US",
                    "jobId": receipt["job_id"],
                }
                or readback["jobComplete"] is not True
                or _native_count(readback["totalRows"]) != len(rows)
            ):
                raise ValueError()
            counts = readback["capture_row_counts"]
            if (
                type(counts) is not list
                or len(counts) != len(rows)
                or any(type(n) is not int or n != len(rows) for n in counts)
            ):
                raise ValueError()
            extras = ["match_count"] if lane in pipeline.EVIDENCE_COLUMNS_BY_TABLE else []
            schema = readback["schema"]
            expected_fields = list(physical_fields(lane)) + extras + ["capture_row_count"]
            if type(schema) is not list or [field["name"] for field in schema] != expected_fields:
                raise ValueError()
            source_fields = {field["name"]: field for field in schemas[lane]["schema"]["fields"]}
            aliases = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
            for field in schema:
                expected = source_fields.get(field["name"], {"type": "INT64", "mode": "NULLABLE"})
                if field.get("mode", "NULLABLE") not in {"NULLABLE", "REQUIRED", "REPEATED"}:
                    raise ValueError()
                if aliases.get(field["type"], field["type"]) != expected["type"] or (
                    (field.get("mode", "NULLABLE") == "REPEATED")
                    != (expected["mode"] == "REPEATED")
                ):
                    raise ValueError()
            for row in rows:
                if type(row) is not dict or set(row) != set(physical_fields(lane)):
                    raise ValueError()
            match_counts = readback["match_counts"]
            matches = Counter((row.get("market"), row.get("id")) for row in rows)
            expected_counts = (
                [matches[(row["market"], row["id"])] for row in rows] if extras else []
            )
            if (
                type(match_counts) is not list
                or any(type(n) is not int for n in match_counts)
                or match_counts != expected_counts
            ):
                raise ValueError()
            normalized_rows = _sort_rows(
                lane, _normalize_lane(lane, rows, schemas, markets, cutoff_date, plan.source_as_of)
            )
            if canonical_bytes(normalized_rows) != canonical_bytes(
                assembly["row_material"]["physical_rows_by_table"][lane]
            ):
                raise ValueError()
            rows_by_table[lane] = normalized_rows

        for lane in LANES[:3]:
            accept(lane)
        keys = tuple(tuple(key) for key in assembly["row_material"]["requested_sample_keys"])
        if keys:
            accept("enriched_content", keys)
            enriched_present = {
                (row["market"], row["id"]) for row in rows_by_table["enriched_content"]
            }
            fallback = tuple(key for key in keys if key not in enriched_present)
            if fallback:
                accept("raw_content", fallback)
        if (
            index != len(receipts)
            or type(value["total_bytes_billed"]) is not int
            or total_billed != value["total_bytes_billed"]
        ):
            raise ValueError()
        if any(
            not rows_by_table[lane] and assembly["row_material"]["physical_rows_by_table"][lane]
            for lane in LANES
        ):
            raise ValueError()
        return copy.deepcopy(value)
    except Exception as error:
        raise ValueError("snapshot_capture_validation_failed") from error


def _read_protected_capture(
    *,
    manifest_sha256,
    consumption_id,
    result_id,
    result_digest,
    client_scope_id,
    market_scope,
    objects,
    result_reader=None,
    approval_reader=None,
):
    from functools import partial

    from scripts.staging import capture_protected_production_snapshot as protected

    from src.analysis.open_intelligence import execution_approval
    from src.analysis.open_intelligence.production_snapshot_storage import SourceCaptureObjects
    from src.analysis.open_intelligence.production_snapshot_tables import (
        DESTINATION_DATASET,
        _creation_job,
        _creation_recovery,
        _validate_failed_creation_evidence,
        retained_origin_registry,
        retained_v1_approval,
        retained_v1_result,
    )

    code = "protected_snapshot_result_invalid"
    if (
        type(manifest_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
        or type(result_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", result_digest) is None
        or type(consumption_id) is not str
        or re.fullmatch(r"exc_[0-9a-f]{64}", consumption_id) is None
        or type(result_id) is not str
        or re.fullmatch(r"exr_[0-9a-f]{64}", result_id) is None
        or not isinstance(objects, SourceCaptureObjects)
    ):
        raise ValueError(code)
    client_scope_id, markets = _scope(client_scope_id, market_scope)
    # Default readers are the retained v1 readers under historical replay, the same
    # binding the capture command builds; a reader without version and mode is unreachable.
    read_result = (
        partial(
            execution_approval._default_result_reader,
            version=execution_approval._RESULT_VERSION,
            mode="historical_replay",
        )
        if result_reader is None
        else result_reader
    )
    read_approval = (
        partial(
            execution_approval._default_approval_reader,
            version=execution_approval._APPROVAL_VERSION,
            mode="historical_replay",
        )
        if approval_reader is None
        else approval_reader
    )
    retained_registry = retained_origin_registry()
    rows = read_result(consumption_id)
    if type(rows) not in (list, tuple) or len(rows) != 1:
        raise ValueError(code)
    result = retained_v1_result(rows[0], code, mode="historical_replay", registry=retained_registry)
    if (
        result.result_id != result_id
        or result.result_digest != result_digest
        or result.manifest_sha256 != manifest_sha256
        or result.consumption_id != consumption_id
        or result.operation != "source_snapshot_capture"
        or result.status != "succeeded"
    ):
        raise ValueError(code)
    approval, manifest = retained_v1_approval(
        read_approval(manifest_sha256),
        code,
        mode="historical_replay",
        registry=retained_registry,
    )
    if (
        approval.manifest_sha256 != manifest_sha256
        or approval.approval_id != result.approval_id
        or manifest.operation != "source_snapshot_capture"
        or approval.operation != manifest.operation
        or manifest.contract_sha256 != protected._CONTRACT_SHA256
        or manifest.service_identity != protected._IDENTITY
        or result.result_reference != result.execution_name + "#source-snapshot"
        or not result.execution_name.startswith(manifest.job_resource + "/executions/")
        or not approval.approved_at
        <= result.completed_at
        <= min(approval.expires_at, manifest.expires_at)
    ):
        raise ValueError(code)
    artifact_digests = dict(manifest.input_artifacts)
    artifacts = {
        name: objects.read_input(name, artifact_digests[name], timeout=30)
        for name in sorted(protected._INPUTS)
    }
    cutoff, mode = protected._parse_cli(list(manifest.arguments[1:]))
    inputs = protected._validate_inputs(artifacts, cutoff, mode, result.completed_at)
    context = {
        "contract_version": "open_intelligence_source_capture_recovery_v1",
        **{
            "initial_" + name: getattr(result, name)
            for name in (
                "manifest_sha256",
                "consumption_id",
                "execution_name",
                "result_id",
                "result_digest",
            )
        },
    }
    payload = protected._initial_payload(lambda _: rows[0], context, inputs)
    if (
        payload["client_scope_id"] != client_scope_id
        or tuple(payload["market_scope"]) != markets
        or payload["missing_checks"] != []
        or payload["stored_artifact"] is None
        or payload["artifact_attempt"] is None
        or payload["total_bytes_billed"] is None
    ):
        raise ValueError(code)
    raw, stored = objects.read_capture(
        payload["artifact_attempt"], payload["stored_artifact"], timeout=30
    )
    artifact = json.loads(raw)
    initial_manifest = protected._initial_capture_manifest(inputs["recovery"], manifest_sha256)
    if (
        artifact["initial_manifest_sha256"] != initial_manifest
        or canonical_bytes(artifact["capture_plan"]) != artifacts["capture_plan"]
        or stored != payload["stored_artifact"]
        or _utc_timestamp(stored["created_at"]) > result.completed_at
    ):
        raise ValueError(code)
    capture = validate_captured_production_snapshot(
        artifact["capture"],
        cutoff_date=cutoff,
        client_scope_id=client_scope_id,
        market_scope=markets,
        reviewed_metadata=artifacts["source_metadata"],
        expected_creator_email=manifest.service_identity,
    )
    snapshot = capture["assembly"]["snapshot"]
    if (
        canonical_digest(capture) != payload["capture_receipt_digest"]
        or snapshot["source_digest"] != payload["snapshot_digest"]
        or snapshot["captured_at"] != payload["captured_at"]
        or snapshot["source_as_of"] != payload["source_as_of"]
        or capture["query_count"] != payload["query_count"]
        or capture["total_bytes_billed"] != payload["total_bytes_billed"]
        or payload["limitations"] != sorted(set(snapshot["coverage_limitations"]))
        or _utc_timestamp(snapshot["captured_at"]) > result.completed_at
    ):
        raise ValueError(code)
    records, evidence = payload["creation_records"], artifact["creation_evidence"]
    if type(records) is not list or len(records) != 5 or len(evidence) not in {5, 6}:
        raise ValueError(code)
    if (
        inputs["recovery"] is not None
        and inputs["recovery"]["contract_version"]
        in {
            "open_intelligence_source_capture_recovery_v2",
            "open_intelligence_source_capture_recovery_v3",
            "open_intelligence_source_capture_recovery_v4",
        }
        and len(evidence) != 6
    ):
        raise ValueError(code)
    current_owner = (manifest_sha256, consumption_id, result.execution_name)
    original, original_records, replacement_index = None, [], None
    ancestor, ancestor_records, origin, origin_records = None, [], None, []
    recovery = inputs["recovery"]
    continuation = recovery is not None and recovery["contract_version"] in {
        "open_intelligence_source_capture_recovery_v3",
        "open_intelligence_source_capture_recovery_v4",
    }
    contexts = [] if recovery is None else [recovery]
    if continuation:
        contexts.append(recovery["ancestor_recovery_context"])
        if recovery["contract_version"] == "open_intelligence_source_capture_recovery_v4":
            contexts.append(recovery["ancestor_recovery_context"]["ancestor_recovery_context"])
    baseline_rows = {}
    for context in contexts:
        rows = read_result(context["initial_consumption_id"])
        if type(rows) not in (list, tuple) or len(rows) != 1:
            raise ValueError(code)
        baseline = retained_v1_result(
            rows[0], code, mode="historical_replay", registry=retained_registry
        )
        if baseline.result_id in baseline_rows or any(
            getattr(baseline, name) != context["initial_" + name]
            for name in (
                "manifest_sha256",
                "consumption_id",
                "execution_name",
                "result_id",
                "result_digest",
            )
        ):
            raise ValueError(code)
        baseline_rows[baseline.result_id] = rows[0]
        if context["contract_version"] in {
            "open_intelligence_source_capture_recovery_v3",
            "open_intelligence_source_capture_recovery_v4",
        }:
            nested_context = context["ancestor_recovery_context"]
            predecessor_approval, predecessor_manifest = retained_v1_approval(
                read_approval(baseline.manifest_sha256),
                code,
                mode="historical_replay",
                registry=retained_registry,
            )
            predecessor_artifacts = dict(predecessor_manifest.input_artifacts)
            if (
                predecessor_approval.manifest_sha256 != baseline.manifest_sha256
                or predecessor_approval.approval_id != baseline.approval_id
                or predecessor_manifest.operation != "source_snapshot_capture"
                or predecessor_manifest.contract_sha256 != manifest.contract_sha256
                or predecessor_manifest.service_identity != manifest.service_identity
                or predecessor_manifest.arguments
                != (
                    "scripts/staging/capture_protected_production_snapshot.py",
                    "--cutoff-date",
                    cutoff.isoformat(),
                    "--mode",
                    "recover",
                )
                or predecessor_artifacts["recovery_context"] != canonical_digest(nested_context)
                or any(
                    predecessor_artifacts[name] != artifact_digests[name]
                    for name in ("capture_contract", "capture_plan", "source_metadata")
                )
            ):
                raise ValueError(code)
    if recovery is not None:
        original, original_records = _creation_recovery(
            recovery, baseline_rows.get, inputs["envelope"], inputs["plan"], result.completed_at
        )
    if continuation:
        ancestor, ancestor_records = _creation_recovery(
            contexts[1], baseline_rows.get, inputs["envelope"], inputs["plan"], result.completed_at
        )
        if len(contexts) == 3:
            origin, origin_records = _creation_recovery(
                contexts[2],
                baseline_rows.get,
                inputs["envelope"],
                inputs["plan"],
                result.completed_at,
            )
    if len(evidence) == 6:
        if original is None:
            raise ValueError(code)
        failed_owner = origin or ancestor or original
        failed_records = (
            origin_records if origin else ancestor_records if ancestor else original_records
        )
        failed_index = _validate_failed_creation_evidence(
            failed_owner,
            failed_records,
            evidence[0],
            inputs["plan"],
            inputs["envelope"]["creation_statements"],
            manifest.service_identity,
            contexts[-1],
        )
        if not continuation:
            replacement_index = failed_index
        evidence = evidence[1:]
    retained_bytes = 0
    for index, (statement, ddl, record, entry) in enumerate(
        zip(
            inputs["plan"].statements,
            inputs["envelope"]["creation_statements"],
            records,
            evidence,
            strict=True,
        )
    ):
        previous = original_records[index] if index < len(original_records) else None
        if index == replacement_index:
            previous = None
        if previous is not None and previous["state"] == "failed":
            raise ValueError(code)
        previous_owner = next(
            (
                owner
                for owner in (original, ancestor, origin)
                if owner is not None
                and previous is not None
                and previous["job_id"] == f"oi_v3_snapshot_{owner.manifest_sha256}_{statement.lane}"
            ),
            original,
        )
        owner = (
            (
                previous_owner.manifest_sha256,
                previous_owner.consumption_id,
                previous_owner.execution_name,
            )
            if previous is not None
            else current_owner
        )
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
            or type(record) is not dict
            or set(record) != {"lane", "destination", "job_id", "native_job_digest", "state"}
            or entry["lane"] != statement.lane
            or record["lane"] != statement.lane
            or (entry["manifest_sha256"], entry["consumption_id"], entry["execution_name"]) != owner
            or record["state"] != "succeeded"
            or record["destination"] != statement.destination_table
            or record["job_id"] != f"oi_v3_snapshot_{entry['manifest_sha256']}_{statement.lane}"
            or record["native_job_digest"] != canonical_digest(entry["native_job"])
            or (
                previous is not None
                and previous["state"] == "succeeded"
                and previous["native_job_digest"] != record["native_job_digest"]
            )
            or entry["snapshot_metadata"]
            != capture["assembly"]["snapshot_metadata"][statement.lane]
        ):
            raise ValueError(code)
        native = _creation_job(
            bigquery.QueryJob.from_api_repr(
                copy.deepcopy(entry["native_job"]), SimpleNamespace(project=PROJECT)
            ),
            native=entry["native_job"],
            job_id=record["job_id"],
            sql=ddl["sql"],
            identity=manifest.service_identity,
            earliest=inputs["plan"].source_as_of,
            latest=result.completed_at,
            target={
                "projectId": PROJECT,
                "datasetId": DESTINATION_DATASET,
                "tableId": statement.destination_table.rsplit(".", 1)[1],
            },
        )
        metadata = entry["snapshot_metadata"]
        size = metadata.get("numBytes")
        if (
            native["status"]["state"] != "DONE"
            or native["status"].get("errorResult")
            or native["statistics"]["query"].get("totalBytesBilled") != "0"
            or metadata.get("expirationTime")
            != str(int((inputs["plan"].source_as_of + timedelta(days=90)).timestamp() * 1000))
            or type(size) is not str
            or re.fullmatch(r"[0-9]+", size) is None
        ):
            raise ValueError(code)
        retained_bytes += int(size)
    if retained_bytes > 5368709120:
        raise ValueError(code)
    binding = {
        name: getattr(result, name)
        for name in (
            "operation",
            "manifest_sha256",
            "consumption_id",
            "result_id",
            "result_digest",
            "approval_id",
            "execution_name",
        )
    }
    binding.update(
        source_sha=manifest.source_sha, image_uri=manifest.image_uri, stored_artifact=stored
    )
    binding.update(
        {
            name: payload[name]
            for name in (
                "cutoff_date",
                "source_as_of",
                "captured_at",
                "snapshot_digest",
                "capture_receipt_digest",
                "snapshot_plan_digest",
                "client_scope_id",
                "market_scope",
            )
        }
    )
    binding["recovery_context"] = copy.deepcopy(inputs["recovery"])
    return copy.deepcopy({"capture": capture, "binding": binding})
