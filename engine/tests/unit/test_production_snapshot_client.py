import copy
import importlib
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from google.cloud import bigquery
from scripts.staging.replay_open_intelligence import ProvenanceSnapshotClient
from src.analysis.open_intelligence import pipeline

from tests.unit import test_production_snapshot as fixture


def module():
    return importlib.import_module("src.analysis.open_intelligence.production_snapshot_client")


class Delegate:
    project = "ogilvy-trends-v2"

    def __init__(self, prepared):
        self.prepared = prepared
        self.tables = []
        self.queries = []
        self.billing = 1
        self.change_table = lambda value: value
        self.change_rows = lambda value: value
        self.pending = None

    def get_table(self, name, **kwargs):
        self.tables.append(name)
        lane = next(
            item["lane"]
            for item in self.prepared["snapshot_plan"]["statements"]
            if item["destination_table"] == name
        )
        value = copy.deepcopy(self.prepared["snapshot_metadata"][lane])
        value["location"] = "US"
        return bigquery.Table.from_api_repr(self.change_table(value))

    def query(self, sql, **kwargs):
        self.queries.append((sql, kwargs))
        rows = self.change_rows(copy.deepcopy(self.pending))
        return SimpleNamespace(
            total_bytes_billed=self.billing,
            result=lambda **kwargs: iter(rows),
        )


def setup(prepared=None):
    prepared = fixture.build() if prepared is None else prepared
    delegate = Delegate(prepared)
    client = module().ProductionSnapshotClient(
        prepared,
        cutoff_date=fixture.row_fixture.CUTOFF,
        client_scope_id=fixture.CLIENT_SCOPE_ID,
        market_scope=fixture.row_fixture.MARKETS,
        delegate=delegate,
    )
    return client, delegate, prepared


def request(client, lane):
    class Capture:
        def query(self, sql, *, job_config, location):
            self.sql, self.config = sql, job_config
            return SimpleNamespace(result=lambda **kwargs: iter(()))

    capture = Capture()
    cutoff = fixture.row_fixture.CUTOFF
    if lane in pipeline.EVIDENCE_COLUMNS_BY_TABLE:
        pipeline._load_evidence_rows(
            capture,
            table=lane,
            keys=(("za", "raw"), ("za", "enriched")),
            start_date=cutoff - timedelta(days=6),
            end_date=cutoff,
        )
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
            trend_date=cutoff,
            markets=fixture.row_fixture.MARKETS,
            statuses=tuple(sorted(pipeline.ACCEPTED_SEED_CANDIDATE_STATUSES))
            if lane == "seed_candidates"
            else None,
        )
    client_delegate = client._delegate
    expected = ProvenanceSnapshotClient(client.snapshot).query(
        capture.sql, job_config=capture.config, location="US"
    )
    client_delegate.pending = list(expected.result(max_results=100000))
    return capture.sql, capture.config


def run(client, lane="event_ledger"):
    sql, config = request(client, lane)
    return tuple(client.query(sql, job_config=config, location="US").result(max_results=100000))


def test_all_native_tables_verified_then_five_actual_pipeline_reads_redirected():
    client, delegate, prepared = setup()
    refs = [item["destination_table"] for item in prepared["snapshot_plan"]["statements"]]
    assert delegate.tables == refs
    assert client.project == delegate.project
    for lane, reference in zip(fixture.subject.LANES, refs, strict=True):
        assert client.source_table(lane) == reference
        run(client, lane)
        sql, kwargs = delegate.queries[-1]
        assert f"FROM `{reference}`" in sql
        assert f"FROM `ogilvy-trends-v2.trends_v2_staging.{lane}`" not in sql
        assert (
            kwargs["job_config"].maximum_bytes_billed == 1_000_000_000 - len(delegate.queries) + 1
        )
        assert kwargs["job_config"].use_query_cache is False
        assert kwargs["retry"] is None
        assert kwargs["job_retry"] is None
        if lane in pipeline.EVIDENCE_COLUMNS_BY_TABLE:
            for field in module().ABSENT_NULLABLE_FIELDS:
                assert f"CAST(NULL AS STRING) AS `{field}`" in sql
    assert len(delegate.queries) == 5
    with pytest.raises(ValueError, match="snapshot_client"):
        run(client)
    assert len(delegate.queries) == 5


@pytest.mark.parametrize(
    "mutation", ["table", "base", "time", "schema", "rows", "etag", "location", "expiry"]
)
def test_direct_native_metadata_changes_refuse_before_queries(mutation):
    prepared = fixture.build()
    delegate = Delegate(prepared)

    def mutate(value):
        if mutation == "table":
            value["type"] = "TABLE"
        if mutation == "base":
            value["snapshotDefinition"]["baseTableReference"]["datasetId"] = "foreign"
        if mutation == "time":
            value["snapshotDefinition"]["snapshotTime"] = "2030-01-02T00:00:00Z"
        if mutation == "schema":
            value["schema"]["fields"][0]["type"] = "BOOL"
        if mutation == "rows":
            value["numRows"] = "9"
        if mutation == "etag":
            value["etag"] = "changed"
        if mutation == "location":
            value["location"] = "EU"
        if mutation == "expiry":
            value["expirationTime"] = "1999999999999"
        return value

    delegate.change_table = mutate
    with pytest.raises(ValueError):
        module().ProductionSnapshotClient(
            prepared,
            cutoff_date=fixture.row_fixture.CUTOFF,
            client_scope_id=fixture.CLIENT_SCOPE_ID,
            market_scope=fixture.row_fixture.MARKETS,
            delegate=delegate,
        )
    assert delegate.queries == []


@pytest.mark.parametrize("mutation", ["location", "rows", "schema"])
def test_metadata_only_helper_rejects_fresh_native_changes(mutation):
    prepared = fixture.build()
    delegate = Delegate(prepared)

    def mutate(value):
        if mutation == "location":
            value["location"] = "EU"
        elif mutation == "rows":
            value["numRows"] = "9"
        else:
            value["schema"]["fields"][0]["type"] = "BOOL"
        return value

    delegate.change_table = mutate
    with pytest.raises(ValueError, match="snapshot_client_metadata_invalid"):
        module()._validate_live_snapshot_metadata(prepared, delegate)
    assert delegate.queries == []


@pytest.mark.parametrize("mutation", ["project", "get_table", "query"])
def test_metadata_only_helper_requires_the_existing_delegate_boundary(mutation):
    prepared = fixture.build()
    delegate = Delegate(prepared)
    if mutation == "project":
        delegate.project = "foreign"
    else:
        setattr(delegate, mutation, None)
    with pytest.raises(ValueError, match="snapshot_client_delegate_invalid"):
        module()._validate_live_snapshot_metadata(prepared, delegate)


@pytest.mark.parametrize("billing", [None, True, -1, 1_000_000_001])
def test_unknown_or_invalid_billing_prevents_further_reads(billing):
    client, delegate, _ = setup()
    delegate.billing = billing
    with pytest.raises(ValueError, match="snapshot_client"):
        run(client)
    with pytest.raises(ValueError, match="snapshot_client"):
        run(client, "seed_graph")
    assert len(delegate.queries) == 1


def test_exhausted_byte_allowance_prevents_next_submission():
    client, delegate, _ = setup()
    delegate.billing = 1_000_000_000
    run(client)
    with pytest.raises(ValueError, match="snapshot_client"):
        run(client, "seed_graph")
    assert len(delegate.queries) == 1


@pytest.mark.parametrize("mutation", ["id_type", "bool_number", "extra", "missing", "value"])
def test_native_rows_must_match_typed_snapshot_without_string_coercion(mutation):
    client, delegate, _ = setup()

    def mutate(rows):
        if mutation == "id_type":
            rows[0]["ledger_id"] = 123
        if mutation == "bool_number":
            field = next(key for key, value in rows[0].items() if type(value) is int)
            rows[0][field] = True
        if mutation == "extra":
            rows[0]["extra"] = None
        if mutation == "missing":
            rows = []
        if mutation == "value":
            rows[0]["ledger_id"] += "changed"
        return rows

    delegate.change_rows = mutate
    with pytest.raises(ValueError, match="snapshot_client"):
        run(client)


def test_only_declared_dates_timestamps_normalize_and_string_ids_stay_strings():
    client, delegate, _ = setup()

    def equivalent(rows):
        for row in rows:
            for key, value in row.items():
                if isinstance(value, datetime):
                    row[key] = value.isoformat().replace("+00:00", "Z")
                elif isinstance(value, date):
                    row[key] = value.isoformat()
        return rows

    delegate.change_rows = equivalent
    rows = run(client)
    assert isinstance(rows[0]["trend_date"], date)
    assert type(rows[0]["ledger_id"]) is str


@pytest.mark.parametrize("mutation", ["sql", "parameter", "location", "job_config"])
def test_nonpipeline_query_or_scope_refuses_before_native_submission(mutation):
    client, delegate, _ = setup()
    sql, config = request(client, "event_ledger")
    location = "US"
    if mutation == "sql":
        sql += "; DELETE FROM `foreign.table` WHERE TRUE"
    if mutation == "parameter":
        config.query_parameters = []
    if mutation == "location":
        location = "EU"
    if mutation == "job_config":
        config.destination = "ogilvy-trends-v2.trends_v2_staging.output"
    with pytest.raises(ValueError, match="snapshot_client"):
        client.query(sql, job_config=config, location=location)
    assert delegate.queries == []


def test_uncertain_query_failure_blocks_further_native_reads(monkeypatch):
    client, delegate, _ = setup()
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("synthetic provider failure")

    monkeypatch.setattr(delegate, "query", fail)
    with pytest.raises(ValueError, match="snapshot_client_read_failed"):
        run(client)
    with pytest.raises(ValueError, match="snapshot_client_budget_unavailable"):
        run(client)
    assert len(calls) == 1


def test_snapshot_property_is_detached_and_foreign_scope_refuses_before_metadata():
    client, delegate, prepared = setup()
    original = client.snapshot
    exposed = client.snapshot
    exposed["rows_by_table"]["event_ledger"][0]["ledger_id"] = "changed"
    assert client.snapshot == original
    delegate.tables.clear()
    with pytest.raises(ValueError, match="production_snapshot_invalid"):
        module().ProductionSnapshotClient(
            prepared,
            cutoff_date=fixture.row_fixture.CUTOFF,
            client_scope_id="foreign",
            market_scope=fixture.row_fixture.MARKETS,
            delegate=delegate,
        )
    assert delegate.tables == []


@pytest.mark.parametrize("row_id", ["00123", "false", "2030-01-02"])
def test_date_and_number_looking_string_ids_are_not_reinterpreted(row_id):
    rows = fixture.inputs()["rows_by_table"]
    rows["event_ledger"][0]["ledger_id"] = row_id
    client, _, _ = setup(fixture.build(rows_by_table=rows))
    assert run(client)[0]["ledger_id"] == row_id
