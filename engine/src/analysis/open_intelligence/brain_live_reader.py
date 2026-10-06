"""Private, bounded live authority reader for the dark Intelligence Brain."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from google.cloud import bigquery

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.persistence import (
    ROW_FIELDS,
    TABLE_BINDINGS,
    BatchInvalid,
    OpenIntelligenceRowBatch,
    _versioned_fields,
    build_run_receipt_row,
    canonical_typed_json,
    row_set_digest_sql,
)
from src.analysis.open_intelligence.run_receipts import (
    RUN_RECEIPT_ROW_FIELDS,
    OpenIntelligenceRunReceipt,
    build_run_receipt,
    run_receipt_digest,
)
from src.contracts.open_intelligence import CONTRACT_VERSION

TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging"
TARGET_LOCATION = "US"
TARGET_IDENTITY = "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com"
RUNTIME_CONTRACT_VERSION = "42_live_intelligence_runtime_v1_0_2"
ROW_CEILING = 50_000
RECEIPT_RELATIONS = (
    "signal_candidates_v2",
    "signal_evidence_v2",
    "signal_membership_v2",
    "signal_lineage_v2",
    "signal_analysis_v2",
    "signal_predictions_v2",
    "signal_outcomes_v2",
)
RELATION_ORDER = (
    "open_intelligence_run_receipts_v1",
    *RECEIPT_RELATIONS,
    "open_intelligence_source_copy_manifest_v1",
    "open_intelligence_source_copy_receipts_v1",
)
TABLE_BY_RELATION = {value: key for key, value in TABLE_BINDINGS.items()}
ANALYSIS_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "analysis_id",
    "signal_id",
    "signal_date",
    "market",
    "evidence_state",
    "summary",
    "why_now",
    "possible_response",
    "limitations",
    "contradictions",
    "evidence_ids",
    "human_review_required",
    "model_version",
    "analyzed_at",
)
MANIFEST_FIELDS = (
    "copy_run_id",
    "target_table",
    "window_start",
    "window_end",
    "manifest_rows",
)
SOURCE_COPY_RECEIPT_FIELDS = (
    "copy_run_id",
    "source_table",
    "window_start",
    "window_end",
    "source_rows",
    "source_set_digest",
    "schema_digest",
    "filter_digest",
)
RELATION_FIELDS = MappingProxyType(
    {
        "open_intelligence_run_receipts_v1": RUN_RECEIPT_ROW_FIELDS,
        **{relation: ROW_FIELDS[table] for relation, table in TABLE_BY_RELATION.items()},
        "signal_analysis_v2": ANALYSIS_FIELDS,
        "open_intelligence_source_copy_manifest_v1": MANIFEST_FIELDS,
        "open_intelligence_source_copy_receipts_v1": SOURCE_COPY_RECEIPT_FIELDS,
    }
)


class LiveBrainReadRefusal(ValueError):
    """A live authority read could not prove its closed scope."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True, init=False)
class LiveBrainRead:
    receipt: OpenIntelligenceRunReceipt
    candidate: Mapping[str, object]
    evidence: tuple[Mapping[str, object], ...]
    membership: tuple[Mapping[str, object], ...]
    lineage: tuple[Mapping[str, object], ...]
    analysis: tuple[Mapping[str, object], ...]
    predictions: tuple[Mapping[str, object], ...]
    outcomes: tuple[Mapping[str, object], ...]
    row_projection_digests: tuple[tuple[str, str], ...]
    schema_digests: tuple[tuple[str, str], ...]
    snapshot_root: Mapping[str, object]
    snapshot_digest: str
    read_receipt: Mapping[str, object]

    def __new__(cls, *_args, **_kwargs):
        raise LiveBrainReadRefusal("readback_refused", "live read authority is invalid")


@dataclass(frozen=True, slots=True)
class _IssuedLiveBrainRead:
    owner: LiveBrainRead
    public_digest: str


def _plain_authority_value(value):
    if is_dataclass(value):
        return {
            field.name: _plain_authority_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: _plain_authority_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_authority_value(item) for item in value]
    return value


def _live_read_public(value: LiveBrainRead) -> dict[str, object]:
    return {
        field.name: _plain_authority_value(getattr(value, field.name))
        for field in fields(LiveBrainRead)
    }


def _live_read_authority_store():
    issued: dict[int, _IssuedLiveBrainRead] = {}

    def issue(owner: LiveBrainRead) -> None:
        if type(owner) is not LiveBrainRead or id(owner) in issued:
            raise LiveBrainReadRefusal("readback_refused", "live read authority is invalid")
        issued[id(owner)] = _IssuedLiveBrainRead(owner, canonical_digest(_live_read_public(owner)))

    def lookup(owner: object) -> _IssuedLiveBrainRead:
        authority = issued.get(id(owner))
        if type(authority) is not _IssuedLiveBrainRead or authority.owner is not owner:
            raise LiveBrainReadRefusal("readback_refused", "live read authority is invalid")
        return authority

    return issue, lookup


_issue_live_read_authority, _lookup_live_read_authority = _live_read_authority_store()


def _create_live_brain_read(*values) -> LiveBrainRead:
    if len(values) != len(fields(LiveBrainRead)):
        raise LiveBrainReadRefusal("readback_refused", "live read authority is invalid")
    value = object.__new__(LiveBrainRead)
    for field, item in zip(fields(LiveBrainRead), values, strict=True):
        object.__setattr__(value, field.name, item)
    _issue_live_read_authority(value)
    return value


def _mapping(
    row: object, relation: str, cluster_build_version: str | None = None
) -> dict[str, object]:
    if isinstance(row, Mapping):
        mapped = dict(row)
    else:
        items = getattr(row, "items", None)
        if not callable(items):
            raise LiveBrainReadRefusal("readback_refused", f"{relation} row is not a mapping")
        mapped = dict(items())
    expected = set(RELATION_FIELDS[relation])
    if relation == "signal_membership_v2":
        expected = set(_versioned_fields("membership", cluster_build_version))
        if (
            cluster_build_version != "hybrid_graph_v3"
            and "source_provenance_json" in mapped
            and mapped.pop("source_provenance_json") is not None
        ):
            raise LiveBrainReadRefusal("readback_refused", "source_provenance_version_invalid")
    if set(mapped) != expected:
        raise LiveBrainReadRefusal("readback_refused", f"{relation} fields are invalid")
    return mapped


def _client_identity(client: object) -> str | None:
    credentials = getattr(client, "_credentials", None)
    return getattr(credentials, "service_account_email", None)


def _validate_client(client: object) -> None:
    if (
        getattr(client, "project", None) != TARGET_PROJECT
        or getattr(client, "location", TARGET_LOCATION) != TARGET_LOCATION
        or _client_identity(client) != TARGET_IDENTITY
    ):
        raise LiveBrainReadRefusal("identity_refused", "exact staging identity is required")


def _config(parameters: list[bigquery.QueryParameter]) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(use_legacy_sql=False, query_parameters=parameters)


def _read(
    client: object,
    relation: str,
    where: str,
    parameters: list[bigquery.QueryParameter],
    cluster_build_version: str | None = None,
) -> tuple[tuple[Mapping[str, object], ...], str]:
    fields = ", ".join(f"`{field}`" for field in RELATION_FIELDS[relation])
    if relation == "signal_membership_v2":
        fields += ", `source_provenance_json`"
    sql = (
        f"SELECT {fields} FROM `{TARGET_PROJECT}.{TARGET_DATASET}.{relation}` "
        f"WHERE {where} ORDER BY TO_JSON_STRING(STRUCT({fields})) LIMIT {ROW_CEILING + 1}"
    )
    job = client.query(sql, job_config=_config(parameters), location=TARGET_LOCATION)
    if getattr(job, "statement_type", None) != "SELECT":
        raise LiveBrainReadRefusal("side_effect_refused", "only SELECT jobs are permitted")
    rows = tuple(job.result(max_results=ROW_CEILING + 1))
    if len(rows) > ROW_CEILING:
        raise LiveBrainReadRefusal("readback_refused", f"{relation} exceeded its row ceiling")
    return tuple(
        MappingProxyType(_mapping(row, relation, cluster_build_version)) for row in rows
    ), str(job.job_id)


def _read_manifest_summary(
    client: object,
    where: str,
    parameters: list[bigquery.QueryParameter],
) -> tuple[tuple[Mapping[str, object], ...], str]:
    """One row per copy run and target table: the manifest holds one row per copied source
    row, far past the row ceiling, and the authority check only needs the count per table."""
    relation = "open_intelligence_source_copy_manifest_v1"
    keys = "copy_run_id, target_table, window_start, window_end"
    sql = (
        f"SELECT {keys}, COUNT(*) AS manifest_rows "
        f"FROM `{TARGET_PROJECT}.{TARGET_DATASET}.{relation}` "
        f"WHERE {where} GROUP BY {keys} ORDER BY {keys} LIMIT {ROW_CEILING + 1}"
    )
    job = client.query(sql, job_config=_config(parameters), location=TARGET_LOCATION)
    if getattr(job, "statement_type", None) != "SELECT":
        raise LiveBrainReadRefusal("side_effect_refused", "only SELECT jobs are permitted")
    rows = tuple(job.result(max_results=ROW_CEILING + 1))
    if len(rows) > ROW_CEILING:
        raise LiveBrainReadRefusal("readback_refused", f"{relation} exceeded its row ceiling")
    return tuple(MappingProxyType(_mapping(row, relation)) for row in rows), str(job.job_id)


def _run_parameters(receipt: OpenIntelligenceRunReceipt) -> list[bigquery.QueryParameter]:
    return [
        bigquery.ScalarQueryParameter("client_scope_id", "STRING", receipt.client_scope_id),
        bigquery.ScalarQueryParameter("run_id", "STRING", receipt.run_id),
        bigquery.ScalarQueryParameter("signal_date", "DATE", receipt.signal_date),
        bigquery.ScalarQueryParameter("contract_version", "STRING", CONTRACT_VERSION),
    ]


def _canonical_analysis(row: Mapping[str, object]) -> str:
    return canonical_bytes(dict(row)).decode("utf-8")


def _projection_digest(
    relation: str, rows: tuple[Mapping[str, object], ...], cluster_build_version: str | None = None
) -> str:
    table = TABLE_BY_RELATION.get(relation)
    if table is not None:
        values = sorted(
            canonical_typed_json(table, row, cluster_build_version=cluster_build_version)
            for row in rows
        )
    else:
        values = sorted(canonical_bytes(dict(row)).decode("utf-8") for row in rows)
    return canonical_digest((relation, values))


def _schema_digest(relation: str, cluster_build_version: str | None = None) -> str:
    fields = (
        _versioned_fields("membership", cluster_build_version)
        if relation == "signal_membership_v2"
        else RELATION_FIELDS[relation]
    )
    return canonical_digest((relation, fields))


def _validate_future(rows: tuple[Mapping[str, object], ...], as_of: datetime) -> None:
    for row in rows:
        for value in row.values():
            if isinstance(value, datetime) and value.astimezone(UTC) > as_of:
                raise LiveBrainReadRefusal("future_data_refused", "future row timestamp refused")


def _source_copy_authority(
    receipt: OpenIntelligenceRunReceipt,
    manifest: tuple[Mapping[str, object], ...],
    copy_receipts: tuple[Mapping[str, object], ...],
) -> None:
    copy_runs = {row["copy_run_id"] for row in (*manifest, *copy_receipts)}
    if len(copy_runs) != 1 or not manifest or not copy_receipts:
        raise LiveBrainReadRefusal("readback_refused", "source_window_authority_unavailable")
    copy_run_id = next(iter(copy_runs))
    completeness = []
    for copy_receipt in copy_receipts:
        source_table = str(copy_receipt["source_table"])
        manifest_rows = sum(
            int(row["manifest_rows"]) for row in manifest if row["target_table"] == source_table
        )
        source_rows = int(copy_receipt["source_rows"])
        if manifest_rows != source_rows:
            raise LiveBrainReadRefusal("readback_refused", "source_window_authority_unavailable")
        completeness.append((source_table, manifest_rows, source_rows))
    expected = hashlib.sha256(
        json.dumps(
            {"copy_run_id": copy_run_id, "completeness": sorted(completeness)},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if expected != receipt.source_window_digest:
        raise LiveBrainReadRefusal("readback_refused", "source_window_digest mismatch")


def _same_scope(row: Mapping[str, object], receipt: OpenIntelligenceRunReceipt) -> bool:
    return (
        row.get("client_scope_id") == receipt.client_scope_id
        and row.get("run_id") == receipt.run_id
        and row.get("signal_date") == receipt.signal_date
    )


def _signal_rows(
    rows: tuple[Mapping[str, object], ...], signal_id: str, receipt: OpenIntelligenceRunReceipt
) -> tuple[Mapping[str, object], ...]:
    output = []
    for row in rows:
        linked = row.get("signal_id") == signal_id or row.get("to_signal_id") == signal_id
        if linked:
            if not _same_scope(row, receipt):
                raise LiveBrainReadRefusal("signal_refused", "cross-run signal row refused")
            output.append(row)
    return tuple(output)


def validate_live_brain_read(value: object) -> LiveBrainRead:
    if type(value) is not LiveBrainRead:
        raise LiveBrainReadRefusal("readback_refused", "live read authority is invalid")
    issued = _lookup_live_read_authority(value)
    if issued.public_digest != canonical_digest(_live_read_public(value)):
        raise LiveBrainReadRefusal("readback_refused", "live read authority is invalid")
    scope = value.snapshot_root.get("resolved_scope")
    candidate = value.candidate
    if not isinstance(scope, Mapping):
        raise LiveBrainReadRefusal("readback_refused", "live read scope is invalid")
    scope_equalities = (
        (value.receipt.run_id, scope.get("run_id"), candidate.get("run_id")),
        (
            value.receipt.client_scope_id,
            scope.get("client_scope_id"),
            candidate.get("client_scope_id"),
        ),
        (
            # The market scope is a set: the receipt keeps the scope's order and the rows
            # carry it sorted.
            tuple(sorted(value.receipt.market_scope)),
            tuple(sorted(scope.get("market_scope", ()))),
            tuple(sorted(candidate.get("market_scope", ()))),
        ),
        (scope.get("signal_id"), candidate.get("signal_id")),
        (scope.get("brand_config_id"), candidate.get("brand_config_id")),
        (tuple(scope.get("audience_lens_ids", ())), tuple(candidate.get("audience_lens_ids", ()))),
        (scope.get("theme_id"), candidate.get("theme_id")),
        (scope.get("contract_version"), candidate.get("contract_version")),
    )
    if any(
        any(
            _plain_authority_value(item) != _plain_authority_value(values[0]) for item in values[1:]
        )
        for values in scope_equalities
    ):
        raise LiveBrainReadRefusal("readback_refused", "live read scope is invalid")
    if (
        value.snapshot_digest != canonical_digest(dict(value.snapshot_root))
        or tuple(value.snapshot_root.get("row_projection_digests", ()))
        != value.row_projection_digests
        or tuple(value.snapshot_root.get("schema_digests", ())) != value.schema_digests
        or value.snapshot_root.get("run_receipt_digest") != run_receipt_digest(value.receipt)
        or value.read_receipt.get("run_receipt_verified") is not True
        or value.read_receipt.get("model_calls") != 0
        or value.read_receipt.get("persisted") is not False
    ):
        raise LiveBrainReadRefusal("readback_refused", "live read authority is invalid")
    return value


def _validate_complete_run_rows(
    receipt: OpenIntelligenceRunReceipt,
    run_rows: Mapping[str, tuple[Mapping[str, object], ...]],
) -> OpenIntelligenceRowBatch:
    for rows in run_rows.values():
        _validate_future(rows, receipt.completed_at)

    try:
        batch = OpenIntelligenceRowBatch(
            run_rows["signal_candidates_v2"],
            run_rows["signal_evidence_v2"],
            run_rows["signal_membership_v2"],
            run_rows["signal_lineage_v2"],
            run_rows["signal_predictions_v2"],
            run_rows["signal_outcomes_v2"],
            cluster_build_version=receipt.cluster_build_version,
        )
        recomputed = build_run_receipt_row(
            batch,
            run_rows["signal_analysis_v2"],
            run_id=receipt.run_id,
            client_scope_id=receipt.client_scope_id,
            market_scope=receipt.market_scope,
            signal_date=receipt.signal_date,
            observation_start=receipt.observation_start,
            observation_end=receipt.observation_end,
            observation_method=receipt.observation_method,
            source_window_digest=receipt.source_window_digest,
            cluster_build_version=receipt.cluster_build_version,
            source_family_map_version=receipt.source_family_map_version,
            rule_version=receipt.rule_version,
            status=receipt.status,
            complete_partitions=receipt.complete_partitions,
            source_sha=receipt.source_sha,
            completed_at=receipt.completed_at,
        )
    except (ValueError, BatchInvalid) as error:
        raise LiveBrainReadRefusal("receipt_refused", "row_set_digest invalid") from error
    count_fields = (
        "candidate_count",
        "evidence_count",
        "membership_count",
        "lineage_count",
        "analysis_count",
        "prediction_count",
    )
    if any(getattr(receipt, field) != getattr(recomputed, field) for field in count_fields):
        raise LiveBrainReadRefusal("receipt_refused", "receipt row count mismatch")
    for candidate in run_rows["signal_candidates_v2"]:
        if candidate["cluster_build_version"] != receipt.cluster_build_version:
            raise LiveBrainReadRefusal("receipt_refused", "cluster version mismatch")
    # A prediction row carries the prediction contract's own rule version, not
    # the run's composition rule version (4 Sep 2026, first released run).
    from src.analysis.open_intelligence.predictions import PredictionRules

    prediction_rule_version = PredictionRules().rule_version
    for prediction in run_rows["signal_predictions_v2"]:
        if (
            prediction["cluster_build_version"] != receipt.cluster_build_version
            or prediction["source_family_map_version"] != receipt.source_family_map_version
            or prediction["rule_version"] != prediction_rule_version
        ):
            raise LiveBrainReadRefusal("receipt_refused", "prediction authority mismatch")

    return batch


def read_live_brain_authority(*, client: object, run_id: str, signal_id: str) -> LiveBrainRead:
    """Read one exact completed run and issue a frozen live Brain authority."""
    _validate_client(client)
    if not isinstance(run_id, str) or not run_id or not isinstance(signal_id, str) or not signal_id:
        raise LiveBrainReadRefusal("invalid_arguments", "run and signal ids are required")
    jobs: list[tuple[str, str]] = []
    receipt_rows, job_id = _read(
        client,
        "open_intelligence_run_receipts_v1",
        "run_id = @run_id",
        [bigquery.ScalarQueryParameter("run_id", "STRING", run_id)],
    )
    jobs.append(("open_intelligence_run_receipts_v1", job_id))
    if len(receipt_rows) != 1:
        raise LiveBrainReadRefusal("receipt_refused", "one run receipt is required")
    try:
        receipt = build_run_receipt(**dict(receipt_rows[0]))
    except ValueError as error:
        raise LiveBrainReadRefusal("receipt_refused", "run receipt is invalid") from error
    if receipt.status != "completed" or not receipt.complete_partitions:
        raise LiveBrainReadRefusal("receipt_refused", "completed partitions are required")
    try:
        _versioned_fields("membership", receipt.cluster_build_version)
    except (ValueError, BatchInvalid) as error:
        raise LiveBrainReadRefusal(
            "receipt_refused", "source_provenance_version_invalid"
        ) from error

    where = (
        "client_scope_id = @client_scope_id AND run_id = @run_id "
        "AND signal_date = @signal_date AND contract_version = @contract_version"
    )
    run_rows: dict[str, tuple[Mapping[str, object], ...]] = {}
    for relation in RECEIPT_RELATIONS[:-1]:
        rows, job_id = _read(
            client, relation, where, _run_parameters(receipt), receipt.cluster_build_version
        )
        run_rows[relation] = rows
        jobs.append((relation, job_id))
    outcome_where = where + " AND evaluated_at <= @completed_at"
    outcome_parameters = [
        *_run_parameters(receipt),
        bigquery.ScalarQueryParameter("completed_at", "TIMESTAMP", receipt.completed_at),
    ]
    receipt_outcomes, job_id = _read(
        client, "signal_outcomes_v2", outcome_where, outcome_parameters
    )
    run_rows["signal_outcomes_v2"] = receipt_outcomes
    jobs.append(("signal_outcomes_v2", job_id))
    _validate_complete_run_rows(receipt, run_rows)
    # The receipt's digest is the one the written tables report through the digest SQL, so
    # it is checked the same way; a digest rendered in Python would never match it.
    digest_job = client.query(
        f"SELECT {row_set_digest_sql(receipt.run_id, cluster_build_version=receipt.cluster_build_version)} AS row_set_digest",
        job_config=_config([]),
        location=TARGET_LOCATION,
    )
    if getattr(digest_job, "statement_type", None) != "SELECT":
        raise LiveBrainReadRefusal("side_effect_refused", "only SELECT jobs are permitted")
    digest_rows = tuple(digest_job.result(max_results=2))
    reported = dict(digest_rows[0]).get("row_set_digest") if len(digest_rows) == 1 else None
    if reported != receipt.row_set_digest:
        raise LiveBrainReadRefusal("receipt_refused", "row_set_digest mismatch")
    jobs.append(("row_set_digest", str(digest_job.job_id)))
    candidates = _signal_rows(run_rows["signal_candidates_v2"], signal_id, receipt)
    if len(candidates) != 1:
        raise LiveBrainReadRefusal("signal_refused", "signal_identity_cardinality_invalid")
    candidate = candidates[0]
    if candidate["audience_lens_ids"]:
        raise LiveBrainReadRefusal("signal_refused", "audience lenses are not neutral")

    later_outcomes, job_id = _read(
        client,
        "signal_outcomes_v2",
        where + " AND signal_id = @signal_id AND evaluated_at <= @as_of",
        [
            *_run_parameters(receipt),
            bigquery.ScalarQueryParameter("signal_id", "STRING", signal_id),
            bigquery.ScalarQueryParameter("as_of", "TIMESTAMP", receipt.completed_at),
        ],
    )
    jobs.append(("signal_outcomes_v2", job_id))
    later_outcomes = _signal_rows(later_outcomes, signal_id, receipt)
    _validate_future(later_outcomes, receipt.completed_at)
    copy_where = "window_start = @window_start AND window_end = @window_end"
    copy_parameters = [
        bigquery.ScalarQueryParameter("window_start", "DATE", receipt.observation_start),
        bigquery.ScalarQueryParameter("window_end", "DATE", receipt.observation_end),
    ]
    manifest, job_id = _read_manifest_summary(client, copy_where, copy_parameters)
    jobs.append(("open_intelligence_source_copy_manifest_v1", job_id))
    copy_receipts, job_id = _read(
        client, "open_intelligence_source_copy_receipts_v1", copy_where, copy_parameters
    )
    jobs.append(("open_intelligence_source_copy_receipts_v1", job_id))
    _source_copy_authority(receipt, manifest, copy_receipts)

    evidence = _signal_rows(run_rows["signal_evidence_v2"], signal_id, receipt)
    membership = _signal_rows(run_rows["signal_membership_v2"], signal_id, receipt)
    lineage = _signal_rows(run_rows["signal_lineage_v2"], signal_id, receipt)
    analysis = _signal_rows(run_rows["signal_analysis_v2"], signal_id, receipt)
    predictions = _signal_rows(run_rows["signal_predictions_v2"], signal_id, receipt)
    if any(
        row["market"] != candidate["market"]
        for row in (*evidence, *membership, *analysis, *predictions)
    ):
        raise LiveBrainReadRefusal("signal_refused", "signal market mismatch")
    if any(row["published_at"] is None for row in evidence):
        raise LiveBrainReadRefusal("signal_refused", "current evidence timestamp is required")

    projections = {
        "open_intelligence_run_receipts_v1": receipt_rows,
        **run_rows,
        "signal_outcomes_v2": later_outcomes,
        "open_intelligence_source_copy_manifest_v1": manifest,
        "open_intelligence_source_copy_receipts_v1": copy_receipts,
    }
    row_projection_digests = tuple(
        (
            relation,
            _projection_digest(
                relation, tuple(projections[relation]), receipt.cluster_build_version
            ),
        )
        for relation in RELATION_ORDER
    )
    schema_digests = tuple(
        (relation, _schema_digest(relation, receipt.cluster_build_version))
        for relation in RELATION_ORDER
    )
    resolved_scope = {
        "project": TARGET_PROJECT,
        "dataset": TARGET_DATASET,
        "client_scope_id": receipt.client_scope_id,
        "market_scope": receipt.market_scope,
        "brand_config_id": candidate["brand_config_id"],
        "audience_lens_ids": tuple(candidate["audience_lens_ids"]),
        "theme_id": candidate["theme_id"],
        "run_id": receipt.run_id,
        "signal_id": signal_id,
        "contract_version": candidate["contract_version"],
    }
    root_values = {
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "run_receipt_digest": run_receipt_digest(receipt),
        "row_projection_digests": row_projection_digests,
        "historical_read_receipt_digests": (),
        "resolved_scope": resolved_scope,
        "as_of": receipt.completed_at,
        "reader_versions": ("brain_live_reader_v1",),
        "schema_digests": schema_digests,
    }
    snapshot_digest = canonical_digest(root_values)
    root = MappingProxyType(root_values)
    read_receipt = MappingProxyType(
        {
            "run_receipt_verified": True,
            "display_release_state": receipt.display_release_state,
            "job_ids": tuple(jobs),
            "schema_digests": schema_digests,
            "model_calls": 0,
            "persisted": False,
        }
    )
    value = _create_live_brain_read(
        receipt,
        candidate,
        evidence,
        membership,
        lineage,
        analysis,
        predictions,
        later_outcomes,
        row_projection_digests,
        schema_digests,
        root,
        snapshot_digest,
        read_receipt,
    )
    return validate_live_brain_read(value)


__all__ = []
