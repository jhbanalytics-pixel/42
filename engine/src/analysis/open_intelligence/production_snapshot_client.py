"""Bounded native snapshot reads; no capture, release or execution authority."""

import copy
import re
from datetime import timedelta
from threading import Lock

from google.cloud import bigquery
from scripts.staging.replay_open_intelligence import (
    ProvenanceSnapshotClient,
    _digest,
    _SnapshotRows,
)

from src.analysis.open_intelligence import pipeline
from src.analysis.open_intelligence.production_snapshot import (
    _deserialize_plan,
    validate_prepared_production_snapshot,
)
from src.analysis.open_intelligence.production_snapshot_rows import ABSENT_NULLABLE_FIELDS, _value
from src.analysis.open_intelligence.production_snapshot_tables import (
    LANES,
    PROJECT,
    _reviewed_source_metadata,
    validate_snapshot_readback,
)


class _CaptureQuery:
    def query(self, sql, *, job_config, location):
        self.sql, self.config = sql, job_config
        return _SnapshotRows(())


def _validate_live_snapshot_metadata(prepared, delegate):
    if (
        getattr(delegate, "project", None) != PROJECT
        or not callable(getattr(delegate, "get_table", None))
        or not callable(getattr(delegate, "query", None))
    ):
        raise ValueError("snapshot_client_delegate_invalid")
    try:
        plan = _deserialize_plan(prepared["snapshot_plan"])
        tables = {item.lane: item.destination_table for item in plan.statements}
        schema, _ = _reviewed_source_metadata(prepared["source_metadata_utf8"].encode("utf-8"))
        metadata = {}
        for lane in LANES:
            resource = delegate.get_table(tables[lane], retry=None, timeout=30).to_api_repr()
            if resource.get("location") != "US":
                raise ValueError("snapshot_client_location_invalid")
            metadata[lane] = resource
        common = {
            "source_metadata": prepared["source_metadata_utf8"].encode("utf-8"),
            "readback_rows": prepared["readback_rows"],
        }
        actual = validate_snapshot_readback(plan, snapshot_metadata=metadata, **common)
        expected = validate_snapshot_readback(
            plan, snapshot_metadata=prepared["snapshot_metadata"], **common
        )
        if _digest(actual) != _digest(expected):
            raise ValueError("snapshot_client_metadata_mismatch")
        return plan, tables, schema
    except Exception as error:
        raise ValueError("snapshot_client_metadata_invalid") from error


class ProductionSnapshotClient:
    project = PROJECT

    def __init__(self, prepared, *, cutoff_date, client_scope_id, market_scope, delegate):
        self._prepared = validate_prepared_production_snapshot(
            prepared,
            cutoff_date=cutoff_date,
            client_scope_id=client_scope_id,
            market_scope=market_scope,
        )
        self._delegate = delegate
        self._cutoff = cutoff_date
        self._markets = tuple(market_scope)
        self._plan, self._tables, self._schema = _validate_live_snapshot_metadata(
            self._prepared, delegate
        )
        self._snapshot = self._prepared["snapshot"]
        self._expected = ProvenanceSnapshotClient(self._snapshot)
        self._lock = Lock()
        self._queries = 0
        self._billed = 0
        self._blocked = False

    @property
    def snapshot(self):
        return copy.deepcopy(self._snapshot)

    def source_table(self, logical):
        if type(logical) is not str or logical not in self._tables:
            raise ValueError("snapshot_client_table_invalid")
        return self._tables[logical]

    def _admit_query(self, sql, config, location):
        if (
            type(sql) is not str
            or location != "US"
            or not isinstance(config, bigquery.QueryJobConfig)
        ):
            raise ValueError("snapshot_client_query_invalid")
        matches = re.findall(r"`ogilvy-trends-v2\.trends_v2_staging\.([a-z_]+)`", sql)
        if len(matches) != 1 or matches[0] not in LANES:
            raise ValueError("snapshot_client_query_invalid")
        lane = matches[0]
        params = {
            p.name: getattr(p, "values", getattr(p, "value", None)) for p in config.query_parameters
        }
        capture = _CaptureQuery()
        if lane in pipeline.EVIDENCE_COLUMNS_BY_TABLE:
            markets, ids = params.get("sample_markets"), params.get("sample_ids")
            if (
                type(markets) not in (tuple, list)
                or type(ids) not in (tuple, list)
                or not ids
                or len(markets) != len(ids)
                or len(ids) > pipeline.MAX_REQUESTED_SAMPLE_KEYS
                or any(type(m) is not str or m not in self._markets for m in markets)
                or any(type(value) is not str or not value for value in ids)
            ):
                raise ValueError("snapshot_client_query_invalid")
            keys = tuple(zip(markets, ids, strict=True))
            if len(set(keys)) != len(keys):
                raise ValueError("snapshot_client_query_invalid")
            pipeline._load_evidence_rows(
                capture,
                table=lane,
                keys=keys,
                start_date=self._cutoff - timedelta(days=6),
                end_date=self._cutoff,
            )
            limit = len(keys) + 1
        else:
            pipeline._load_source(
                capture,
                table=lane,
                columns={
                    "event_ledger": pipeline.EVENT_COLUMNS,
                    "seed_graph": pipeline.SEED_GRAPH_COLUMNS,
                    "seed_candidates": pipeline.SEED_CANDIDATE_COLUMNS,
                }[lane],
                date_column="proposed_date" if lane == "seed_candidates" else "trend_date",
                trend_date=self._cutoff,
                markets=self._markets,
                statuses=tuple(sorted(pipeline.ACCEPTED_SEED_CANDIDATE_STATUSES))
                if lane == "seed_candidates"
                else None,
            )
            limit = pipeline.SOURCE_CEILINGS[lane] + 1
        if sql != capture.sql or config.to_api_repr() != capture.config.to_api_repr():
            raise ValueError("snapshot_client_query_invalid")
        rewritten = sql.replace(f"`{PROJECT}.trends_v2_staging.{lane}`", f"`{self._tables[lane]}`")
        if lane in pipeline.EVIDENCE_COLUMNS_BY_TABLE:
            for field in ABSENT_NULLABLE_FIELDS:
                rewritten = rewritten.replace(
                    f"evidence.`{field}`", f"CAST(NULL AS STRING) AS `{field}`"
                )
        return lane, rewritten, limit

    def _normalized(self, lane, rows):
        fields = {item["name"]: item for item in self._schema[lane]["schema"]["fields"]}
        if lane in pipeline.EVIDENCE_COLUMNS_BY_TABLE:
            fields.update(
                {name: {"type": "STRING", "mode": "NULLABLE"} for name in ABSENT_NULLABLE_FIELDS}
            )
            fields["match_count"] = {"type": "INT64", "mode": "REQUIRED"}
        output = []
        for raw in rows:
            row = pipeline._row_mapping(raw)
            normalized = {}
            for key, value in row.items():
                if key not in fields:
                    raise ValueError("snapshot_client_rows_invalid")
                typed = _value(value, fields[key])
                normalized[key] = typed if fields[key]["type"] in {"DATE", "TIMESTAMP"} else value
            output.append(normalized)
        return output

    def query(self, sql, *, job_config, location):
        with self._lock:
            if self._blocked or self._queries >= 5 or self._billed >= 1_000_000_000:
                raise ValueError("snapshot_client_budget_unavailable")
            lane, native_sql, limit = self._admit_query(sql, job_config, location)
            expected = self._expected.query(sql, job_config=job_config, location=location)
            expected_rows = tuple(expected.result(max_results=limit))
            config = bigquery.QueryJobConfig.from_api_repr(job_config.to_api_repr())
            remaining = 1_000_000_000 - self._billed
            config.maximum_bytes_billed = remaining
            config.use_query_cache = False
            self._queries += 1
            try:
                job = self._delegate.query(
                    native_sql,
                    job_config=config,
                    location="US",
                    retry=None,
                    job_retry=None,
                    timeout=30,
                )
                actual = tuple(job.result(max_results=limit, retry=None, timeout=30))
                billed = job.total_bytes_billed
                if type(billed) is not int or not 0 <= billed <= remaining:
                    raise ValueError("snapshot_client_billing_unavailable")
                self._billed += billed
                if _digest(self._normalized(lane, actual)) != _digest(
                    self._normalized(lane, expected_rows)
                ):
                    raise ValueError("snapshot_client_rows_mismatch")
            except Exception as error:
                self._blocked = True
                raise ValueError("snapshot_client_read_failed") from error
            return _SnapshotRows(expected_rows)
