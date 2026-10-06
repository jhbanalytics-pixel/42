import copy
import json
import re
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from scripts.staging import replay_open_intelligence as replay
from src.analysis.open_intelligence import pipeline, production_snapshot
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.contracts.open_intelligence import ResolvedScope

from tests.unit import test_production_snapshot_rows as rows_fixture
from tests.unit import test_production_snapshot_tables as tables_fixture
from tests.unit.test_provenance_replay_v3 import CONFIG, Provider


def prepared():
    rows = {lane: [] for lane in tables_fixture.subject.LANES}
    for term, source, platform in (
        ("alpha rhythm", "rss", "news"),
        ("beta rhythm", "youtube", "youtube"),
    ):
        graph = rows_fixture.row("seed_graph", row_id=term, samples=(term,))
        graph.update(
            term=term,
            term_type="token",
            platform=platform,
            row_count=4,
            topic_groups=["music"],
            near_topics=[],
            co_occur_terms=["alpha rhythm", "beta rhythm"],
        )
        rows["seed_graph"].append(graph)
        row = rows_fixture.row("enriched_content", row_id=term)
        row.update(
            source=source,
            platform=platform,
            content_type="article" if source == "rss" else "video",
            url="https://example.invalid/" + term.replace(" ", "-"),
        )
        rows["enriched_content"].append(row)
    plan = tables_fixture.build_plan()
    metadata = tables_fixture.snapshot_metadata(plan)
    readback = tables_fixture.readback(plan)
    for item in readback:
        count = len(rows[item["lane"]])
        item["row_count"] = count
        metadata[item["lane"]]["numRows"] = str(count)
    return production_snapshot.build_production_snapshot(
        cutoff_date=rows_fixture.CUTOFF,
        captured_at=rows_fixture.CAPTURED_AT,
        client_scope_id="ogilvy_default",
        market_scope=rows_fixture.MARKETS,
        reviewed_metadata=tables_fixture.source_metadata_bytes(),
        plan=plan,
        snapshot_metadata=metadata,
        readback_rows=readback,
        rows_by_table=rows,
    )


class NativeClient:
    project = "ogilvy-trends-v2"

    def __init__(self, value):
        self.value = value
        self.sql = []
        self.metadata_reads = []

    def get_table(self, name, **kwargs):
        self.metadata_reads.append(name)
        lane = next(lane for lane in tables_fixture.subject.LANES if name.endswith("_" + lane))
        return SimpleNamespace(
            to_api_repr=lambda: {
                **copy.deepcopy(self.value["snapshot_metadata"][lane]),
                "location": "US",
            }
        )

    def query(self, sql, *, job_config, location, **kwargs):
        self.sql.append(sql)
        logical = re.sub(r"open_intelligence_v3_source_\d{8}_", "", sql)
        for field in rows_fixture.subject.ABSENT_NULLABLE_FIELDS:
            logical = logical.replace(f"CAST(NULL AS STRING) AS `{field}`", f"evidence.`{field}`")
        result = replay.ProvenanceSnapshotClient(self.value["snapshot"]).query(
            logical, job_config=job_config, location=location
        )
        return SimpleNamespace(
            result=lambda **kw: iter(result.rows),
            state="DONE",
            error_result=None,
            total_bytes_billed=10_000_000,
            total_bytes_processed=1000,
            job_id=f"synthetic_native_{len(self.sql)}",
            query=sql,
            location=location,
            project=self.project,
            user_email="trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        )


def run(value, client):
    scope = ResolvedScope(
        "ogilvy_default",
        rows_fixture.MARKETS,
        "ogilvy",
        (),
        "open_intelligence",
        "synthetic_production_v3",
        "2.0.0",
    )
    return pipeline.run_dynamic_signal_identity_v3(
        rows_fixture.CUTOFF,
        scope,
        client,
        "trends_v2_staging",
        False,
        source_snapshot=value,
        rule_bundle=replay.load_composition_rules_v3(CONFIG, require_certified=False),
        semantic_provider=Provider(),
    )


@pytest.mark.parametrize("canonical_reload", [False, True])
def test_production_snapshot_reaches_graph_through_native_readback(canonical_reload):
    value = prepared()
    if canonical_reload:
        value = json.loads(canonical_bytes(value))
    client = NativeClient(value)
    result = run(value, client)
    assert result.run.component_count == 1
    assert result.source_snapshot_digest == value["snapshot"]["source_digest"]
    assert result.run.persistable_count == 0
    assert {
        "normalized_row_query_execution_proof_required",
        "snapshot_creation_execution_proof_required",
        "snapshot_storage_execution_proof_required",
    } <= set(result.run.missing_work)
    assert len(client.metadata_reads) == 5
    assert client.sql
    assert all("open_intelligence_v3_source_" in sql for sql in client.sql)
    assert all(
        "open_intelligence_v3_source_" in table
        for table in result.run.evidence_projection.source_window.source_tables
    )
    assert all(
        "open_intelligence_v3_source_" in table
        for window in result.run.source_windows.values()
        for table in window.source_tables
    )


def test_production_snapshot_cannot_borrow_mutable_table_metadata():
    value = prepared()
    client = NativeClient(value)
    client.value = copy.deepcopy(value)
    client.value["snapshot_metadata"]["seed_graph"]["type"] = "TABLE"
    with pytest.raises(ValueError):
        run(value, client)
    assert client.sql == []


def test_native_capture_canonical_reload_reaches_graph_and_replay(monkeypatch):
    from tests.unit import test_production_snapshot_capture as capture_fixture
    from tests.unit.test_production_snapshot_replay import scope

    source = prepared()
    http = capture_fixture.HTTP()
    http.inputs["snapshot_metadata"] = source["snapshot_metadata"]
    http.rows = copy.deepcopy(source["row_material"]["physical_rows_by_table"])
    for lane, rows in http.rows.items():
        schema = rows_fixture.field_schema(lane)
        for row in rows:
            for name, field in schema.items():
                if row.get(name) is not None and field["type"] in ("DATE", "TIMESTAMP"):
                    parser = (
                        date.fromisoformat if field["type"] == "DATE" else datetime.fromisoformat
                    )
                    row[name] = parser(row[name])
    captured = capture_fixture.run(monkeypatch, http)
    captured = capture_fixture.module().validate_captured_production_snapshot(
        json.loads(canonical_bytes(captured)),
        cutoff_date=rows_fixture.CUTOFF,
        client_scope_id="ogilvy_default",
        market_scope=rows_fixture.MARKETS,
        reviewed_metadata=tables_fixture.source_metadata_bytes(),
        expected_creator_email=capture_fixture.CREATOR,
    )
    value = json.loads(canonical_bytes(captured["assembly"]))
    result = run(value, NativeClient(value))
    artifact = replay.adapt_production_result_to_replay_input(
        result,
        scope=scope(),
        production_snapshot=value,
        semantic_provider=Provider(),
    )
    encoded = replay.serialize_production_replay_input(artifact)
    rebuilt = replay.validate_production_replay_input(
        json.loads(encoded), semantic_provider=Provider()
    )
    assert replay.serialize_production_replay_input(rebuilt) == encoded
    assert result.source_snapshot_digest == captured["assembly"]["snapshot"]["source_digest"]
    assert rebuilt["production_snapshot"]["source_authority"] is False
    assert captured["execution_authority"] is False
    assert result.run.persistable_count == 0
    assert result.run.component_count == 1
    assert "snapshot_storage_execution_proof_required" in result.run.missing_work
