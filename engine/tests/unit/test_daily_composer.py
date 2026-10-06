"""The native composer and persist clients of the daily compose stage, over fakes only."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from scripts.staging import replay_open_intelligence as replay
from src.analysis.open_intelligence import daily_composer, daily_stages, persistence
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.daily_cycle import execute_daily_cycle
from src.analysis.open_intelligence.daily_native_clients import (
    bigquery_warehouse,
    daily_readiness_rules,
)
from src.analysis.open_intelligence.geographic_scope import (
    ROW_CITING_METHODS,
    UNKNOWN,
    GeographicScope,
    local_observation_refs,
)
from src.analysis.open_intelligence.pipeline import (
    EVIDENCE_COLUMNS_BY_TABLE,
    SEED_GRAPH_COLUMNS,
    TARGET_DATASET,
)
from src.analysis.open_intelligence.readiness import EXPLICIT_ORIGIN_POLICY, ReadinessRules
from src.analysis.open_intelligence.run_receipts import build_run_receipt, run_receipt_digest
from src.analysis.open_intelligence.staging_source_profile import build_staging_source_profile
from src.contracts.open_intelligence import ResolvedScope

from tests.unit import test_daily_stages as stages
from tests.unit.test_dynamic_signal_persistence import _fake_json_value, _FakeBigQueryClient
from tests.unit.test_open_intelligence_release_profiles import STAGING_RUN_ID, staging_profile
from tests.unit.test_provenance_replay_v3 import Provider, seal

ENGINE_ROOT = Path(__file__).resolve().parents[2]
CUTOFF = stages.CUTOFF
DAY = date(2026, 9, 10)
COLLECTED = datetime(2026, 9, 10, 12, tzinfo=UTC)
PUBLISHED = datetime(2026, 9, 10, 10, tzinfo=UTC)
SOURCE_SHA = "a" * 40
RULES = daily_readiness_rules(CUTOFF)
TERMS = (
    ("alpha rhythm", "ev_alpha_01", "socialcrawl", "reddit", "socialcrawl", "reddit"),
    ("beta rhythm", "ev_beta_01", "youtube", "youtube", "google_youtube", "youtube"),
)


def scope(run_id: str = STAGING_RUN_ID) -> ResolvedScope:
    return ResolvedScope(
        "ogilvy_default", ("za", "ng", "ke"), "ogilvy", (), "open_intelligence", run_id, "2.0.0"
    )


def graph_row(term: str, platform: str, row_id: str) -> dict:
    row = dict.fromkeys(SEED_GRAPH_COLUMNS)
    row.update(
        market="za",
        term=term,
        term_type="token",
        platform=platform,
        trend_date=DAY,
        event_date=DAY,
        row_count=4,
        topic_groups=["music"],
        near_topics=[],
        co_occur_terms=[item[0] for item in TERMS],
        sample_row_ids=[row_id],
    )
    return row


def evidence_row(term, row_id, source, platform, vendor, channel, *, geo=True, title=None):
    row = dict.fromkeys(EVIDENCE_COLUMNS_BY_TABLE["enriched_content"])
    row.update(
        id=row_id,
        source=source,
        platform=platform,
        market="za",
        content_type="post" if platform == "reddit" else "video",
        title=f"{term} is everywhere today" if title is None else title,
        author_name="Creator One",
        author_handle_norm=f"creator_{platform}",
        url=f"https://example.invalid/{row_id}",
        published_at=PUBLISHED,
        collected_at=COLLECTED,
        regional_score=0.8,
    )
    if geo:
        # A channel_family_v2 row: its retained geography receipt is authoritative.
        row.update(
            vendor_family=vendor,
            channel_family=channel,
            source_family=channel,
            native_id=f"native_{row_id}",
            source_family_map_version="channel_family_v2",
            geo_method_id="wave1_geo_v1",
            geo_receipt_id=f"geo_{row_id}",
        )
    return row


def snapshot(*, rows=None, evidence=None, reverse=False) -> dict:
    tables = {
        key: []
        for key in (
            "event_ledger",
            "seed_graph",
            "seed_candidates",
            "enriched_content",
            "raw_content",
        )
    }
    if rows is None:
        rows = [graph_row(term, platform, row_id) for term, row_id, _, platform, _, _ in TERMS]
    if evidence is None:
        evidence = [evidence_row(*item) for item in TERMS]
    tables["seed_graph"] = list(reversed(rows)) if reverse else list(rows)
    tables["enriched_content"] = list(reversed(evidence)) if reverse else list(evidence)
    return seal(
        {
            "fixture_scope": "synthetic_test_only",
            "snapshot_id": "daily_source_snapshot_v3",
            "captured_at": "2026-09-10T23:59:00+00:00",
            "rows_by_table": tables,
            "row_counts_by_table": {},
            "table_digests": {},
            "section_counts": {},
            "section_identity_sets": {},
        }
    )


class _Job:
    def __init__(self, rows):
        self.rows = list(rows)

    def result(self, *args, **kwargs):
        return iter(self.rows)


class FakeWarehouse:
    """An in memory warehouse: the staging tables the pipeline and the factor collector read."""

    project = "ogilvy-trends-v2"
    location = "US"

    def __init__(self, value: dict, *, served: dict | None = None, exposures=()):
        self.snapshot = value
        # The collection exposure receipts the composer reads for the family direction
        # test; none by default, so no family is directional unless a test supplies them.
        self.exposures = [dict(item) for item in exposures]
        self._served = replay.ProvenanceSnapshotClient(value if served is None else served)
        self.enriched = [
            {
                **row,
                "collected_at": datetime.fromisoformat(row["collected_at"]),
                "published_at": (
                    None
                    if row["published_at"] is None
                    else datetime.fromisoformat(row["published_at"])
                ),
            }
            for row in (value if served is None else served)["rows_by_table"]["enriched_content"]
        ]
        self.sql: list[str] = []

    def query(self, sql, *, job_config, location, retry=None, job_retry=None):
        self.sql.append(sql)
        assert location == "US"
        if "term_patterns" in sql:
            return _Job(self._citing(sql))
        if "GROUP BY market, platform" in sql:
            return _Job(self._universe(sql))
        if "collection_exposure_receipts_v1" in sql:
            return _Job(self._exposed(sql))
        return self._served.query(sql, job_config=job_config, location=location)

    def _window(self, sql):
        start, end = re.search(
            r"BETWEEN '(\d{4}-\d{2}-\d{2})' AND '(\d{4}-\d{2}-\d{2})'", sql
        ).groups()
        return [
            row
            for row in self.enriched
            if date.fromisoformat(start) <= row["collected_at"].date() <= date.fromisoformat(end)
        ]

    def _citing(self, sql):
        terms = [
            bytes.fromhex(item).decode("utf-8")
            for item in re.findall(r"FROM_HEX\('([0-9a-f]+)'\) AS STRING\) AS term", sql)
        ]
        found = []
        for row in self._window(sql):
            haystack = " ".join(
                str(row.get(field) or "")[:limit]
                for field, limit in (("query_term", 10_000), ("title", 2000), ("text", 5000))
            ).lower()
            for term in terms:
                if re.search(r"\b" + re.escape(term) + r"\b", haystack):
                    found.append(
                        {
                            "term": term,
                            "market": row["market"],
                            "day": row["collected_at"].date(),
                            "id": row["id"],
                            "platform": row["platform"],
                            "author_handle_norm": row["author_handle_norm"],
                            "author_name": row["author_name"],
                            "url": row["url"],
                            "published_at": row["published_at"],
                            "regional_score": row["regional_score"],
                        }
                    )
        return found

    def _exposed(self, sql):
        start, end = re.search(
            r"BETWEEN DATE '(\d{4}-\d{2}-\d{2})' AND DATE '(\d{4}-\d{2}-\d{2})'", sql
        ).groups()
        (issued,) = re.search(r"issued_at <= TIMESTAMP '([^']+)'", sql).groups()
        return [
            item
            for item in self.exposures
            if date.fromisoformat(start) <= item["exposure_date"] <= date.fromisoformat(end)
            and item["issued_at"] <= datetime.fromisoformat(issued)
        ]

    def _universe(self, sql):
        pairs = sorted({(row["market"], row["platform"]) for row in self._window(sql)})
        return [{"market": market, "platform": platform} for market, platform in pairs]


class FakePersistClient(_FakeBigQueryClient):
    """The persistence suite's fake writer plus the receipt relation and the digest readback."""

    def __init__(self):
        super().__init__()
        self.receipts: list[dict] = []
        self.receipt_sql: list[str] = []
        self.mutex_rows = [{"mutex_id": "daily_composer", "fence_epoch": 0}]
        self.analysis_rows: list[dict] = []
        self.atomic_sql: list[str] = []

    def _digest(self) -> str:
        return hashlib.sha256(
            canonical_bytes(
                {table: _fake_json_value(rows) for table, rows in self.target_rows.items()}
            )
        ).hexdigest()

    def query(self, sql, *, job_config, location, retry=None, job_retry=None):
        if "daily_composition_atomic_v1" in sql:
            self.atomic_sql.append(sql)
            parameters = {
                item.name: (
                    list(item.values) if getattr(item, "values", None) is not None else item.value
                )
                for item in job_config.query_parameters
            }
            run_id = parameters["run_id"]
            before = copy.deepcopy((self.target_rows, self.receipts, self.mutex_rows))
            try:
                if (
                    len(self.mutex_rows) != 1
                    or self.mutex_rows[0].get("mutex_id") != "daily_composer"
                ):
                    raise RuntimeError("daily_mutex_invalid")
                self.mutex_rows[0]["fence_epoch"] += 1
                if self.analysis_rows:
                    raise RuntimeError("persist_batch_differs")
                staged = {table: [] for table in persistence.TABLE_BINDINGS}
                for rows, destination in self.loads:
                    identifier = (
                        f"`{destination.project}.{destination.dataset_id}.{destination.table_id}`"
                    )
                    if identifier in sql:
                        staged[destination.labels["open_intelligence_table"]] = copy.deepcopy(rows)
                existing = [row for row in self.receipts if row["run_id"] == run_id]
                if len(existing) > 1:
                    raise RuntimeError("persist_receipt_cardinality")
                for table in persistence.TABLE_BINDINGS:
                    target = [row for row in self.target_rows[table] if row["run_id"] == run_id]
                    target_by_key = {
                        tuple(_fake_json_value(persistence._natural_key(table, row))): row
                        for row in target
                    }
                    staged_by_key = {
                        tuple(_fake_json_value(persistence._natural_key(table, row))): row
                        for row in staged[table]
                    }
                    if any(key not in staged_by_key for key in target_by_key):
                        raise RuntimeError("persist_batch_differs")
                    if any(
                        _fake_json_value(target_by_key[key]) != _fake_json_value(staged_by_key[key])
                        for key in target_by_key
                    ):
                        raise RuntimeError("persist_batch_differs")
                    if existing and set(target_by_key) != set(staged_by_key):
                        raise RuntimeError("persist_batch_differs")
                inserted = dict.fromkeys(persistence.TABLE_BINDINGS, 0)
                if existing and (
                    existing[0]["row_set_digest"] != self._digest()
                    or existing[0]["display_release_state"] not in {"blocked", "enabled"}
                    or any(
                        existing[0][field] != parameters[field]
                        for field in persistence._DAILY_RETRY_BINDING_FIELDS
                    )
                ):
                    raise RuntimeError("persist_batch_differs")
                if not existing:
                    for table in persistence.TABLE_BINDINGS:
                        keys = {
                            tuple(_fake_json_value(persistence._natural_key(table, row)))
                            for row in self.target_rows[table]
                        }
                        for row in staged[table]:
                            if (
                                tuple(_fake_json_value(persistence._natural_key(table, row)))
                                not in keys
                            ):
                                self.target_rows[table].append(copy.deepcopy(row))
                                inserted[table] += 1
                    row = {
                        field: parameters[field]
                        for field in persistence.RUN_RECEIPT_ROW_FIELDS
                        if field != "row_set_digest"
                    }
                    row["row_set_digest"] = self._digest()
                    self.receipts.append(row)
                    existing = [row]
                returned = {
                    **existing[0],
                    **{f"_inserted_{table}": value for table, value in inserted.items()},
                }
                return _Job([returned])
            except Exception:
                self.target_rows, self.receipts, self.mutex_rows = before
                raise
        if persistence.RUN_RECEIPT_TABLE in sql:
            self.receipt_sql.append(sql)
            if sql.startswith("INSERT INTO"):
                row = {}
                for parameter in job_config.query_parameters:
                    values = getattr(parameter, "values", None)
                    row[parameter.name] = list(values) if values is not None else parameter.value
                self.receipts.append(row)
                return _Job([])
            run_id = re.search(r"WHERE run_id = '([^']*)'", sql).group(1)
            return _Job([dict(row) for row in self.receipts if row["run_id"] == run_id])
        if "AS row_set_digest" in sql:
            return _Job([{"row_set_digest": self._digest()}])
        return super().query(
            sql, job_config=job_config, location=location, retry=retry, job_retry=job_retry
        )


def build(value=None, *, served=None, run_id=STAGING_RUN_ID, exposures=()):
    value = snapshot() if value is None else value
    warehouse = FakeWarehouse(value, served=served, exposures=exposures)
    composer = daily_composer.build_native_composer(
        warehouse=warehouse,
        scope=scope(run_id),
        source_window_from_entry=lambda entry: value,
        readiness_rules=RULES,
        semantic_provider=Provider(),
        now=stages.Clock(),
    )
    client = FakePersistClient()
    bundle = daily_composer.daily_rule_bundle(
        staging_profile(),
        approved_by="recurring_grant:daily",
        approved_at=stages.STARTED,
        rule_version="composition_rules_v3",
    )
    persist = daily_composer.build_native_persist(
        client=client,
        dataset=TARGET_DATASET,
        rule_bundle=bundle,
        run_receipt_inputs=daily_composer.composition_receipt_inputs(
            composer, source_sha=SOURCE_SHA
        ),
    )
    return composer, persist, client, warehouse


def entry_for(capture: stages.FakeCapture, *, state="succeeded") -> dict:
    """A capture registry entry shaped as the capture stage admits it, over the stage fakes."""
    run = {
        "receipt": stages.collection_receipt(),
        "collection_started_at": stages.STARTED,
        "collection_completed_at": stages.COMPLETED,
    }
    profile = build_staging_source_profile(
        run,
        snapshot_as_of=stages.COMPLETED + timedelta(minutes=5),
        scope="ogilvy_default",
        schema_digest="2" * 64,
        source_digest="3" * 64,
        now=capture.captured_at,
    )
    return profile.capture_entry(
        operation_id="attempt:capture:1",
        result_id="res_" + "2" * 32,
        content_digest="1" * 64,
        image_digest="4" * 64,
        generation="7",
        completion_state=state,
        capture_available_at=capture.captured_at,
    )


def compose(composer, entry=None):
    entry = entry_for(stages.FakeCapture()) if entry is None else entry
    return composer(entry, cutoff=CUTOFF)


def bridge_for(inputs):
    inputs = dict(inputs)
    result = inputs.pop("result")
    return persistence.build_producing_batch(
        result,
        signal_date=DAY,
        created_at=stages.STARTED,
        independence_policy=EXPLICIT_ORIGIN_POLICY,
        **inputs,
    )


def composition_identity(inputs, bridge) -> dict:
    """What the D07 determinism rule holds fixed: memberships, labels and row digests."""
    result = inputs["result"].run
    # The snapshot's own digest is positional, the capture's identity rather than the
    # composer's, so the member id and the provenance envelope that carry it are compared
    # without it; every other byte of the composed rows is held fixed.
    volatile = {"created_at", "member_id", "source_provenance_json"}
    # The batch orders its rows by natural key, which for membership is the member id, so
    # the rows are compared in their own identity order rather than the batch's.
    orders = {"candidates": "signal_id", "evidence": "evidence_id", "membership": "member_identity"}
    stable_rows = {
        table: sorted(
            (
                {key: value for key, value in row.items() if key not in volatile}
                for row in getattr(bridge.batch, table)
            ),
            key=lambda row: str(row[orders[table]]),
        )
        for table in orders
    }
    provenance = {
        member: {
            key: replay.normalize_replay_v3_json(value)
            for key, value in envelope.items()
            if key != "source_snapshot_digest"
        }
        for member, envelope in inputs["result"].source_provenance_by_member.items()
    }
    return {
        "provenance": canonical_digest(provenance),
        "components": [
            {
                "members": sorted(component.member_identities),
                "label": component.label,
                "label_member": component.label_member_identity,
            }
            for component in result.components
        ],
        "memberships": {
            key: [item.member_identity for item in values]
            for key, values in result.evidence_projection.memberships_by_component.items()
        },
        "readiness": {key: value.state for key, value in inputs["readiness_by_component"].items()},
        "rows": canonical_digest(stable_rows),
        "geography": {
            key: {row: [g.method, g.evidence_ref, g.scope, g.confidence] for row, g in geo.items()}
            for key, geo in daily_composer.geographic_sidecar(inputs).items()
        },
    }


def compose_digest(*, reverse=False) -> str:
    composer, _persist, _client, _warehouse = build(snapshot(reverse=reverse))
    inputs = compose(composer)
    return canonical_digest(composition_identity(inputs, bridge_for(inputs)))


# The composer


def test_composer_answers_the_fake_composer_mapping_from_the_snapshot():
    composer, _persist, _client, warehouse = build()
    inputs = compose(composer)
    assert set(inputs) == {
        "result",
        "scope",
        "metrics_by_component",
        "receipts_by_component",
        "readiness_by_component",
        "readiness_rules",
        "missing_by_component",
        "quality_evaluated",
    }
    assert inputs["result"].run.persistence_result is None
    assert inputs["result"].run.component_count == 1
    assert inputs["readiness_rules"] == RULES
    assert inputs["quality_evaluated"] is True
    assert inputs["missing_by_component"] == {}
    (key,) = inputs["metrics_by_component"]
    metrics = inputs["metrics_by_component"][key]
    assert metrics.geo_confidence == 0.8
    assert metrics.velocity_score == 1.0
    receipts = inputs["receipts_by_component"][key]
    assert set(receipts) == {"ev_alpha_01", "ev_beta_01"}
    assert all(receipt.origin_resolved for receipt in receipts.values())
    assert {receipt.direction for receipt in receipts.values()} == {"not_applicable"}
    readiness = inputs["readiness_by_component"][key]
    assert readiness.independence_policy == EXPLICIT_ORIGIN_POLICY
    assert readiness.state == "thin"
    assert not any(
        sql.lstrip().startswith(("INSERT", "CREATE", "MERGE", "BEGIN", "DELETE", "UPDATE"))
        for sql in warehouse.sql
    )
    bridge = bridge_for(inputs)
    assert bridge.admitted == (key,)
    assert len(bridge.batch.candidates) == 1
    assert len(bridge.batch.evidence) == 2
    facts = composer.compositions[STAGING_RUN_ID]
    assert facts.composed_at == stages.STARTED
    assert facts.source_window_digest == inputs["result"].source_snapshot_digest
    assert facts.observation_start == DAY - timedelta(days=6)
    assert facts.observation_end == DAY


def test_readiness_rules_equal_the_certifier_daily_rules():
    # The daily floor is 0.6, the weakest tier the scope kernel can decide a local scope by
    # anywhere. The daily chain reaches it through a vendor measured source, and the
    # retained geography receipt at 0.9, so the geo confidence a row can carry is {0.0,
    # 0.6, 0.9}. The inclusive floor admits both local tiers and refuses every measured
    # contextual or foreign row, whose in market confidence the composer sets to zero. The
    # content marker at 0.4 is not a local tier and never was.
    # See the note beside DAILY_READINESS_GEO_FLOOR.
    assert (
        ReadinessRules(current_cutoff=datetime(2026, 9, 10, tzinfo=UTC), minimum_geo_confidence=0.6)
        == RULES
    )
    composer, *_ = build()
    assert compose(composer)["readiness_rules"] == daily_readiness_rules(CUTOFF)
    drifted = daily_composer.build_native_composer(
        warehouse=FakeWarehouse(snapshot()),
        scope=scope(),
        source_window_from_entry=lambda entry: snapshot(),
        readiness_rules=ReadinessRules(
            current_cutoff=RULES.current_cutoff, minimum_geo_confidence=0.8
        ),
        semantic_provider=Provider(),
        now=stages.Clock(),
    )
    with pytest.raises(daily_stages.StageRefusal, match="compose_readiness_rules_differ"):
        compose(drifted)


def test_composer_refuses_an_entry_the_stage_would_not_admit():
    composer, *_ = build()
    with pytest.raises(daily_stages.StageRefusal, match="compose_capture_not_admitted"):
        compose(composer, entry_for(stages.FakeCapture(), state="failed"))
    with pytest.raises(daily_stages.StageRefusal, match="compose_window_differs"):
        composer(entry_for(stages.FakeCapture()), cutoff=CUTOFF + timedelta(days=1))
    with pytest.raises(daily_stages.StageRefusal, match="compose_source_window_invalid"):
        daily_composer.build_native_composer(
            warehouse=FakeWarehouse(snapshot()),
            scope=scope(),
            source_window_from_entry=lambda entry: None,
            readiness_rules=RULES,
            semantic_provider=Provider(),
            now=stages.Clock(),
        )(entry_for(stages.FakeCapture()), cutoff=CUTOFF)


def test_warehouse_rows_that_differ_from_the_snapshot_cannot_compose():
    served = snapshot(evidence=[evidence_row(*TERMS[0]), evidence_row(*TERMS[1], title="other")])
    composer, *_ = build(snapshot(), served=served)
    with pytest.raises(replay.IncompleteReplayInput, match="source_provenance_snapshot_mismatch"):
        compose(composer)


def test_geo_sidecar_carries_one_record_per_receipt_and_the_float_is_local_only():
    composer, *_ = build()
    inputs = compose(composer)
    (key,) = inputs["receipts_by_component"]
    sidecar = daily_composer.geographic_sidecar(inputs)
    assert set(sidecar[key]) == set(inputs["receipts_by_component"][key])
    for row_id, record in sidecar[key].items():
        assert isinstance(record, GeographicScope)
        assert record == GeographicScope("retained_geo_receipt", f"geo_{row_id}", "local", 0.9)
        assert inputs["receipts_by_component"][key][row_id].geo_confidence == 0.9
    assert composer.compositions[STAGING_RUN_ID].geography_by_component[key] == sidecar[key]


def test_contextual_scope_writes_zero_and_keeps_its_record_beside_the_receipt():
    evidence = [
        evidence_row(*TERMS[0], geo=False, title="South Africa hums alpha rhythm"),
        evidence_row(*TERMS[1]),
    ]
    composer, *_ = build(snapshot(evidence=evidence))
    inputs = compose(composer)
    (key,) = inputs["receipts_by_component"]
    receipts = inputs["receipts_by_component"][key]
    assert receipts["ev_alpha_01"].geo_confidence == 0.0
    assert receipts.geography["ev_alpha_01"] == GeographicScope(
        "content_marker", "ev_alpha_01", "contextual", 0.4
    )
    assert receipts["ev_beta_01"].geo_confidence == 0.9


def test_unknown_scope_withholds_the_component_from_readiness_and_stays_unknown():
    evidence = [evidence_row(*TERMS[0], geo=False), evidence_row(*TERMS[1])]
    composer, *_ = build(snapshot(evidence=evidence))
    inputs = compose(composer)
    assert inputs["metrics_by_component"] == {}
    assert inputs["receipts_by_component"] == {}
    (key,) = inputs["missing_by_component"]
    assert inputs["missing_by_component"][key] == ("geo_scope_unknown",)
    facts = composer.compositions[STAGING_RUN_ID]
    assert facts.geography_by_component[key]["ev_alpha_01"] == GeographicScope(
        "none", None, "unknown", None
    )
    assert facts.geography_by_component[key]["ev_alpha_01"].confidence is None
    bridge = bridge_for(inputs)
    assert bridge.admitted == ()
    assert bridge.skipped == ((key, ("geo_scope_unknown",)),)
    assert bridge.batch.candidates == ()


def _search_frame_row(title):
    # A row as the socialcrawl search stores it: the search's own market rides in the
    # geography columns under the search frame method, with nothing else measured about
    # where the behaviour happened.
    return {
        "market": "ng",
        "title": title,
        "text": None,
        "url": "https://example.invalid/world/row_ng_1",
        "geo_method_id": "wave1_seed_market_v1",
        "geo_receipt_id": "geo_row_ng_1",
        "topic_groups": ("music",),
    }


def test_an_international_story_about_nigeria_under_the_nigerian_search_is_contextual():
    # A global story may be contextual evidence; it is not automatically local behaviour.
    title = "World markets slide as Nigeria central bank holds its policy rate"
    record = daily_composer._resolve_geography("ng", "row_ng_1", _search_frame_row(title))
    assert record.scope == "contextual"
    assert record == GeographicScope("content_marker", "row_ng_1", "contextual", 0.4)
    assert daily_composer._geo_confidence(record) == 0.0


def test_foreign_script_under_the_nigerian_search_is_not_local():
    title = "\u041b\u0430\u0433\u043e\u0441 \u043d\u043e\u0432\u043e\u0441\u0442\u0438"
    record = daily_composer._resolve_geography("ng", "row_ng_1", _search_frame_row(title))
    assert record.scope != "local"
    assert record == GeographicScope("script_marker", "row_ng_1", "foreign", 0.7)
    assert daily_composer._geo_confidence(record) == 0.0


def test_the_search_market_alone_is_contextual_evidence_not_a_local_receipt():
    title = "street style this weekend, everyone is out"
    record = daily_composer._resolve_geography("ng", "row_ng_1", _search_frame_row(title))
    assert record == GeographicScope("search_frame", "geo_row_ng_1", "contextual", 0.3)
    assert daily_composer._geo_confidence(record) == 0.0
    assert local_observation_refs([record]) == ()


def test_a_measured_geography_receipt_still_decides_local():
    # Only the search frame stops reading as a receipt; a measured geography does not.
    row = dict(_search_frame_row("street style this weekend"), geo_method_id="wave1_geo_v1")
    record = daily_composer._resolve_geography("ng", "row_ng_1", row)
    assert record == GeographicScope("retained_geo_receipt", "geo_row_ng_1", "local", 0.9)


def test_an_undated_row_keeps_an_unknown_publish_time_and_is_not_fresh_at_readiness():
    undated = evidence_row(*TERMS[0])
    undated["published_at"] = None
    composer, *_ = build(snapshot(evidence=[undated, evidence_row(*TERMS[1])]))
    inputs = compose(composer)
    (key,) = inputs["receipts_by_component"]
    receipts = inputs["receipts_by_component"][key]
    assert receipts["ev_alpha_01"].published_at is None
    assert receipts["ev_beta_01"].published_at == PUBLISHED
    readiness = inputs["readiness_by_component"][key]
    assert "missing_published_at" in readiness.reasons
    assert readiness.state != "ready"


def test_the_producing_batch_carries_the_geographic_record_of_every_admitted_row():
    # The geographic sidecar reaches the producing batch: the record of every admitted
    # row rides beside the rows the batch writes, keyed by component and row, so the
    # daily path carries the kernel's method and evidence reference even though no
    # current relation has a column for them. A caller whose receipts carry no geography
    # at all, the replay path among them, gets an empty sidecar and nothing else changes.
    composer, *_ = build()
    inputs = compose(composer)
    bridge = bridge_for(inputs)
    sidecar = daily_composer.geographic_sidecar(inputs)
    assert set(bridge.geography) == set(bridge.admitted)
    assert bridge.geography == sidecar
    (key,) = bridge.geography
    assert set(bridge.geography[key]) == {"ev_alpha_01", "ev_beta_01"}
    assert bridge.geography[key]["ev_alpha_01"] == GeographicScope(
        "retained_geo_receipt", "geo_ev_alpha_01", "local", 0.9
    )
    plain = dict(inputs)
    plain["receipts_by_component"] = {
        component: dict(receipts) for component, receipts in inputs["receipts_by_component"].items()
    }
    assert bridge_for(plain).geography == {}


class PartialGeography(dict):
    """Receipts that carry a geographic record for only some of their own rows."""

    def __init__(self, receipts, geography):
        super().__init__(receipts)
        self.geography = geography


def test_a_producing_batch_refuses_a_geographic_record_that_does_not_cover_its_rows():
    # The batch validates the sidecar it carries rather than trusting the type that
    # carried it: a receipts mapping whose geographic record misses one of its own rows
    # would write rows whose geography nothing states, so nothing is written at all.
    composer, *_ = build()
    inputs = compose(composer)
    (key,) = inputs["receipts_by_component"]
    receipts = inputs["receipts_by_component"][key]
    short = dict(receipts.geography)
    short.pop("ev_alpha_01")
    for geography in (short, {**dict(receipts.geography), "ev_extra_01": UNKNOWN}, None):
        broken = dict(inputs)
        broken["receipts_by_component"] = {key: PartialGeography(dict(receipts), geography)}
        with pytest.raises(persistence.BatchInvalid, match="geographic record"):
            persistence.build_producing_batch(
                broken.pop("result"),
                signal_date=DAY,
                created_at=stages.STARTED,
                independence_policy=EXPLICIT_ORIGIN_POLICY,
                **broken,
            )


def test_a_producing_batch_refuses_a_geographic_record_that_states_no_scope():
    # Coverage was validated and content was not: a record that covered every row of its
    # component while stating None for each of them, or a bare string, or a plain dict, or
    # an unresolved record, passed, and the batch wrote rows whose geography nothing
    # states. That is exactly what the validator's own docstring promises cannot happen.
    composer, *_ = build()
    inputs = compose(composer)
    (key,) = inputs["receipts_by_component"]
    receipts = inputs["receipts_by_component"][key]
    real = dict(receipts.geography)
    rows = list(real)
    assert len(rows) == 2

    def build_with(geography):
        broken = dict(inputs)
        broken["receipts_by_component"] = {key: PartialGeography(dict(receipts), geography)}
        return persistence.build_producing_batch(
            broken.pop("result"),
            signal_date=DAY,
            created_at=stages.STARTED,
            independence_policy=EXPLICIT_ORIGIN_POLICY,
            **broken,
        )

    for geography in (
        dict.fromkeys(real, None),
        dict.fromkeys(real, "local"),
        {row: {"scope": "local", "confidence": 0.9} for row in real},
        {row: (real[row].method, real[row].scope) for row in real},
        dict.fromkeys(real, UNKNOWN),
        {**real, rows[0]: UNKNOWN},
    ):
        with pytest.raises(persistence.BatchInvalid, match="resolved scope"):
            build_with(geography)
    # An unknown record is refused rather than carried: the composer withholds a row it
    # cannot resolve at component level with geo_scope_unknown, so a component that
    # reaches a batch has no unknown row and a caller presenting one is presenting a row
    # whose geography nothing states under a record that looks like one.
    assert set(build_with(real).geography[key]) == set(real)


def test_a_producing_batch_refuses_an_exchange_of_records_between_its_own_rows():
    # This pinned an exchange of two records between two rows as passing, and called it
    # unreachable. It was not. Two checks close every exchange that changes a row's in
    # market confidence and every exchange involving a row citing method, and this
    # expectation is updated rather than preserved because it asserted a weakness rather
    # than a behaviour: the blocklist match, the script marker and the content marker are
    # each resolved with the row and never with a receipt id, so a mismatch between the
    # evidence reference and the row the record is filed under is detectable on its face;
    # and the float the batch writes for a row is a function of that row's own record, so
    # the composer's confidence for the scope must equal the in market confidence the
    # receipt beside it carries.
    contextual = [
        evidence_row(*TERMS[0], geo=False, title="South Africa hums alpha rhythm"),
        evidence_row(*TERMS[1]),
    ]
    composer, *_ = build(snapshot(evidence=contextual))
    inputs = compose(composer)
    (key,) = inputs["receipts_by_component"]
    receipts = inputs["receipts_by_component"][key]
    real = dict(receipts.geography)
    rows = sorted(real)
    assert len(rows) == 2

    def build_with(geography):
        broken = dict(inputs)
        broken["receipts_by_component"] = {key: PartialGeography(dict(receipts), geography)}
        return persistence.build_producing_batch(
            broken.pop("result"),
            signal_date=DAY,
            created_at=stages.STARTED,
            independence_policy=EXPLICIT_ORIGIN_POLICY,
            **broken,
        )

    # The honest pair: one contextual row decided by the content marker citing itself at
    # zero, one local row decided by its retained geography receipt at 0.9.
    assert real[rows[0]] == GeographicScope("content_marker", rows[0], "contextual", 0.4)
    assert real[rows[1]] == GeographicScope("retained_geo_receipt", f"geo_{rows[1]}", "local", 0.9)
    assert receipts[rows[0]].geo_confidence == 0.0
    assert receipts[rows[1]].geo_confidence == 0.9
    assert set(build_with(real).geography[key]) == set(real)
    # Exchanged, the content marker's record is filed under a row it does not cite, and
    # the floats no longer match the rows they are filed under; both checks have something
    # to say and the batch refuses before it writes anything.
    swapped = {rows[0]: real[rows[1]], rows[1]: real[rows[0]]}
    assert swapped != real
    with pytest.raises(persistence.BatchInvalid, match="a component geographic record must"):
        build_with(swapped)
    # A record kept in place but citing another row is refused on the reference alone.
    with pytest.raises(persistence.BatchInvalid, match="cite the row it is filed under"):
        build_with({**real, rows[0]: GeographicScope("content_marker", rows[1], "contextual", 0.4)})
    # And a record whose scope disagrees with the float its own row carries is refused on
    # the confidence alone, with no row citing method involved on either side.
    with pytest.raises(persistence.BatchInvalid, match="in market confidence its own row"):
        build_with(
            {
                **real,
                rows[1]: GeographicScope("retained_geo_receipt", f"geo_{rows[1]}", "foreign", 0.9),
            }
        )


def test_the_residual_after_the_exchange_checks_is_a_permutation_that_states_nothing_new():
    # What survives both checks, pinned as the residual and not as a weakness. Two rows
    # both local by a retained geography receipt carry records alike in everything the
    # kernel holds: the same method, the same scope, the same tier, each citing its own
    # receipt id rather than its row. Exchanging those two changes no float, since both are
    # 0.9, and no observation reference, since the references are read as a set and the set
    # is unchanged. Separating them needs a field tying a receipt id to its row, which no
    # relation on this path carries.
    composer, *_ = build()
    inputs = compose(composer)
    (key,) = inputs["receipts_by_component"]
    receipts = inputs["receipts_by_component"][key]
    real = dict(receipts.geography)
    rows = sorted(real)
    assert len(rows) == 2
    assert {record.scope for record in real.values()} == {"local"}
    assert {record.method for record in real.values()} == {"retained_geo_receipt"}
    assert {record.confidence for record in real.values()} == {0.9}
    assert "retained_geo_receipt" not in ROW_CITING_METHODS
    swapped = {rows[0]: real[rows[1]], rows[1]: real[rows[0]]}
    assert swapped != real
    broken = dict(inputs)
    broken["receipts_by_component"] = {key: PartialGeography(dict(receipts), swapped)}
    bridge = persistence.build_producing_batch(
        broken.pop("result"),
        signal_date=DAY,
        created_at=stages.STARTED,
        independence_policy=EXPLICIT_ORIGIN_POLICY,
        **broken,
    )
    assert set(bridge.geography[key]) == set(real)
    # It states nothing new: the observation references the readiness layer reads are a set
    # and the permutation leaves it unchanged.
    assert local_observation_refs(swapped.values()) == local_observation_refs(real.values())


def test_input_permutation_yields_identical_memberships_labels_and_digests():
    assert compose_digest() == compose_digest(reverse=True)


def test_a_different_hash_seed_yields_the_same_composition():
    seed = "0" if os.environ.get("PYTHONHASHSEED") not in (None, "0") else "4242"
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ENGINE_ROOT)}
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.unit.test_daily_composer import compose_digest; print(compose_digest())",
        ],
        cwd=ENGINE_ROOT,
        env=env,
        capture_output=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")[-4000:]
    assert completed.stdout.decode("utf-8").strip() == compose_digest()


# The persist client


def persist_batch(composer, persist, inputs=None):
    inputs = compose(composer) if inputs is None else inputs
    bridge = bridge_for(inputs)
    receipt = persist(bridge, run_id=STAGING_RUN_ID, signal_date=DAY, created_at=stages.COMPLETED)
    return bridge, receipt


def test_persist_writes_the_batch_and_records_the_receipt_the_stage_reads_back():
    composer, persist, client, _warehouse = build()
    _bridge, receipt = persist_batch(composer, persist)
    assert receipt.run_id == STAGING_RUN_ID
    assert receipt.status == "completed"
    assert receipt.complete_partitions is True
    assert receipt.display_release_state == "blocked"
    assert receipt.candidate_count == 1
    assert receipt.evidence_count == 2
    assert receipt.membership_count == 2
    assert receipt.cluster_build_version == "hybrid_graph_v3"
    assert receipt.rule_version == "composition_rules_v3"
    assert receipt.observation_method == daily_composer.DAILY_OBSERVATION_METHOD
    assert (
        receipt.source_window_digest == composer.compositions[STAGING_RUN_ID].source_window_digest
    )
    assert receipt.row_set_digest == client._digest()
    assert receipt.source_sha == SOURCE_SHA
    assert [len(rows) for rows in client.target_rows.values()][:3] == [1, 2, 2]
    read_back = daily_stages._read_optional_receipt(STAGING_RUN_ID, bigquery_warehouse(client))
    assert read_back == receipt
    assert run_receipt_digest(read_back) == run_receipt_digest(receipt)


def test_persist_is_idempotent_for_the_same_batch_and_refuses_a_differing_one():
    composer, persist, client, _warehouse = build()
    inputs = compose(composer)
    bridge, receipt = persist_batch(composer, persist, inputs)
    loads = len(client.loads)
    again = persist(bridge, run_id=STAGING_RUN_ID, signal_date=DAY, created_at=stages.COMPLETED)
    assert again == receipt
    assert len(client.receipts) == 1
    assert [len(rows) for rows in client.target_rows.values()][:3] == [1, 2, 2]
    # The write path staged the same rows again and inserted none of them.
    assert len(client.loads) == 2 * loads
    assert len(client.atomic_sql) == 2
    assert all(
        sql.count(
            f"INSERT INTO `ogilvy-trends-v2.trends_v2_staging.{persistence.RUN_RECEIPT_TABLE}`"
        )
        == 1
        for sql in client.atomic_sql
    )
    empty = persistence.ProducingBridgeResult(
        batch=persistence.OpenIntelligenceRowBatch(
            (), (), (), (), (), (), cluster_build_version="hybrid_graph_v3"
        ),
        admitted=(),
        skipped=(),
    )
    with pytest.raises(daily_stages.StageRefusal, match="persist_batch_differs"):
        persist(empty, run_id=STAGING_RUN_ID, signal_date=DAY, created_at=stages.COMPLETED)
    assert len(client.loads) == 2 * loads
    assert len(client.receipts) == 1
    assert [len(rows) for rows in client.target_rows.values()] == [1, 2, 2, 0, 0, 0]
    changed = persistence.OpenIntelligenceRowBatch(
        tuple({**row, "signal_id": "sig_changed"} for row in bridge.batch.candidates),
        tuple({**row, "signal_id": "sig_changed"} for row in bridge.batch.evidence),
        tuple({**row, "signal_id": "sig_changed"} for row in bridge.batch.membership),
        bridge.batch.lineage,
        bridge.batch.predictions,
        bridge.batch.outcomes,
        cluster_build_version=bridge.batch.cluster_build_version,
    )
    changed_bridge = persistence.ProducingBridgeResult(
        batch=changed, admitted=bridge.admitted, skipped=bridge.skipped
    )
    target_before = _fake_json_value(client.target_rows)
    with pytest.raises(daily_stages.StageRefusal, match="persist_batch_differs"):
        persist(changed_bridge, run_id=STAGING_RUN_ID, signal_date=DAY, created_at=stages.COMPLETED)
    assert _fake_json_value(client.target_rows) == target_before


def test_an_empty_source_window_persists_a_receipt_naming_zero_rows():
    composer, persist, client, _warehouse = build(snapshot(rows=[], evidence=[]))
    inputs = compose(composer)
    assert inputs["result"].run.component_count == 0
    assert inputs["missing_by_component"] == {}
    bridge, receipt = persist_batch(composer, persist, inputs)
    assert bridge.batch.candidates == ()
    assert receipt.candidate_count == 0
    assert receipt.evidence_count == 0
    assert receipt.status == "completed"
    assert client.loads == []
    assert len(client.receipts) == 1


def test_the_daily_rule_bundle_is_admitted_beside_the_replay_path_not_instead_of_it():
    bundle = daily_composer.daily_rule_bundle(
        staging_profile(),
        approved_by="recurring_grant:daily",
        approved_at=stages.STARTED,
        rule_version="composition_rules_v3",
    )
    assert bundle.status == "daily_profile_certified"
    assert bundle.profile_digest == staging_profile().digest
    persistence.validate_rule_bundle(bundle)
    for field, value in (
        ("profile_digest", "not-a-digest"),
        ("approved_at", datetime(2026, 9, 11)),
        ("approved_by", ""),
        ("rule_version", None),
    ):
        broken = type(bundle)(**{**bundle.__dict__, field: value})
        with pytest.raises(persistence.RuleInvalid):
            persistence.validate_rule_bundle(broken)
    uncertified = replay.load_composition_rules_v3(
        ENGINE_ROOT / daily_composer.COMPOSITION_RULES_PATH, require_certified=False
    )
    with pytest.raises(persistence.RuleInvalid, match="replay_certified"):
        persistence.validate_rule_bundle(uncertified)
    with pytest.raises(persistence.RuleInvalid):
        daily_composer.build_native_persist(
            client=FakePersistClient(),
            dataset=TARGET_DATASET,
            rule_bundle=uncertified,
            run_receipt_inputs=lambda run_id: {},
        )
    composer, persist, client, _warehouse = build()
    inputs = compose(composer)
    other = daily_composer.build_native_persist(
        client=client,
        dataset=TARGET_DATASET,
        rule_bundle=type(bundle)(**{**bundle.__dict__, "rule_version": "composition_rules_v2"}),
        run_receipt_inputs=daily_composer.composition_receipt_inputs(
            composer, source_sha=SOURCE_SHA
        ),
    )
    with pytest.raises(daily_stages.StageRefusal, match="persist_rule_version_differs"):
        other(bridge_for(inputs), run_id=STAGING_RUN_ID, signal_date=DAY, created_at=stages.STARTED)
    assert client.receipts == []
    del persist


# The compose stage end to end


def test_the_compose_stage_admits_the_native_composer_and_persist_end_to_end(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "staging")
    monkeypatch.setenv("BIGQUERY_DATASET", "intelligence_42_sources_staging")
    composer, persist, client, warehouse = build()
    collector = stages.FakeCollector(stages.FakeLedger())
    store = stages.FakeStore()
    clients = {
        "clock": stages.Clock(),
        "store": store,
        "collection_profile": {
            "environment": "staging",
            "source_dataset": "intelligence_42_sources_staging",
        },
        "collector": collector,
        "collection_ledger": collector.ledger,
        "source_runs": stages.FakeSourceRuns(),
        "generation": stages.generation(capture_v2=True),
        "capture": stages.FakeCapture(),
        "composer": composer,
        "persist": persist,
        "warehouse": bigquery_warehouse(client),
        "certifier": stages.FakeCertifier(),
        "release_profile": staging_profile(),
    }
    handlers = daily_stages.build_stage_handlers(clients, profile=stages.kernel_profile())
    result = execute_daily_cycle(
        operation_id=stages.OPERATION_ID,
        cutoff_utc=CUTOFF,
        profile=stages.kernel_profile(),
        authority=stages.IssuingAuthority(deny=("certify", "release")),
        store=store,
        stages=handlers,
    )
    assert result["stage"] == "certify"
    compose_record = store.records[(stages.OPERATION_ID, "compose")]
    assert compose_record["state"] == "succeeded", compose_record
    assert compose_record["retry_safe"] is False
    assert len(client.receipts) == 1
    receipt = build_run_receipt(
        **{
            **{name: client.receipts[0][name] for name in client.receipts[0]},
            "market_scope": tuple(client.receipts[0]["market_scope"]),
        }
    )
    assert compose_record["output_digest"] == run_receipt_digest(receipt)
    assert receipt.candidate_count == 1
    assert receipt.signal_date == DAY
    assert [len(rows) for rows in client.target_rows.values()][:3] == [1, 2, 2]
    assert any("term_patterns" in sql for sql in warehouse.sql)
    again = handlers["compose"](
        manifest=stages.manifest(
            "compose", predecessor=store.records[(stages.OPERATION_ID, "capture")]["output_digest"]
        ),
        authority_receipt=stages.receipt_for("compose"),
    )
    assert again["state"] == "succeeded"
    assert again["output_digest"] == compose_record["output_digest"]
    assert len(client.receipts) == 1
    assert len(composer.compositions) == 1


# D07 readiness on honest evidence: direction from the approved family direction test and
# its exposure authority, geography from a vendor that measured the market's own people.


def exposure_receipt(family, day, **overrides):
    from scripts.staging import issue_collection_exposure_receipts as issuer

    fields = {
        "exposure_contract_version": "collection_exposure_receipt_v1",
        "source_family": family,
        "exposure_date": day,
        "collection_policy_digest": "a" * 64,
        "source_sha": "b" * 40,
        "image_digest": "sha256:" + "c" * 64,
        "config_digest": "d" * 64,
        "quota_authority_id": "e" * 64,
        "quota_applicability": "unmetered",
        "quota_unit": None,
        "quota_limit": None,
        "quota_used": None,
        "quota_exhausted": False,
        "capture_complete": True,
        "source_copy_receipt_refs": (
            {
                "copy_run_id": "copy_001",
                "source_table": "enriched_content",
                "source_set_digest": "f" * 64,
            },
        ),
        "issued_at": datetime(2026, 9, 10, 23, tzinfo=UTC),
        "issuer_identity": "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com",
    }
    fields.update(overrides)
    fields["receipt_digest"] = issuer.exposure_receipt_digest(fields)
    return fields


def window_exposures(*families, days=daily_composer.FACTOR_WINDOW_DAYS):
    """Complete exposure authority for each family over the daily factor window."""
    return [
        exposure_receipt(family, DAY - timedelta(days=offset))
        for family in families
        for offset in range(days)
    ]


# A song on the Apple Music storefront chart and the same phrase in Google's trending
# searches for the market: two vendors, two channels, each measuring the market's people.
CHART_TERMS = (
    ("gamma chorus", "ev_gamma_01", "apple_music", "apple_music", "music"),
    ("delta chorus", "ev_delta_01", "google_trends_rss", "google_search", "search"),
)


def chart_snapshot(*, sources=None, titles=None, rising=5, baseline=0):
    """One component over two vendor measured rows, with the full term match behind it.

    ``rising`` rows per family cite the term inside the last three days and ``baseline``
    rows cite it in the rest of the window, which is what the direction test counts.
    """
    terms = [
        (term, row_id, *(chosen or (source, platform)), family)
        for (term, row_id, source, platform, family), chosen in zip(
            CHART_TERMS, sources or (None, None), strict=True
        )
    ]
    graph = [
        dict(
            graph_row(term, family, row_id),
            co_occur_terms=[item[0] for item in CHART_TERMS],
        )
        for term, row_id, _source, _platform, family in terms
    ]
    evidence = []
    for index, (term, row_id, source, platform, _family) in enumerate(terms):
        title = None if titles is None else titles[index]
        evidence.append(
            evidence_row(term, row_id, source, platform, None, None, geo=False, title=title)
        )
        for count in range(1, rising):
            evidence.append(
                evidence_row(term, f"{row_id}_r{count}", source, platform, None, None, geo=False)
            )
        for count in range(baseline):
            old = evidence_row(term, f"{row_id}_b{count}", source, platform, None, None, geo=False)
            old["published_at"] = PUBLISHED - timedelta(days=6)
            old["collected_at"] = COLLECTED - timedelta(days=6)
            evidence.append(old)
    return snapshot(rows=graph, evidence=evidence)


def only_component(inputs):
    (key,) = inputs["readiness_by_component"]
    return key, inputs["readiness_by_component"][key], inputs["receipts_by_component"][key]


def test_local_directional_independent_evidence_reaches_ready():
    composer, *_ = build(chart_snapshot(), exposures=window_exposures("music", "search"))
    inputs = compose(composer)
    assert inputs["missing_by_component"] == {}
    key, readiness, receipts = only_component(inputs)
    assert readiness.state == "ready"
    assert readiness.reasons == ("independence_policy:explicit_origin_v2",)
    assert readiness.direction_by_family == {"music": "rising", "search": "rising"}
    assert readiness.independent_pairs == (("ev_delta_01", "ev_gamma_01"),)
    assert {row: item.direction for row, item in receipts.items()} == {
        "ev_delta_01": "rising",
        "ev_gamma_01": "rising",
    }
    # The storefront chart and the trending searches each decide local by the vendor
    # region at its own tier, cited by the row the vendor answered, never by a frame.
    for row_id in ("ev_gamma_01", "ev_delta_01"):
        assert receipts.geography[row_id] == GeographicScope("vendor_region", row_id, "local", 0.6)
        assert receipts[row_id].geo_confidence == 0.6
    directions = composer.compositions[STAGING_RUN_ID].direction_by_component[key]
    assert {family: item.direction for family, item in directions.items()} == {
        "music": "rising",
        "search": "rising",
    }
    # Readiness is not release: the producing batch admits the ready candidate and the
    # certifier re-evaluates the stored rows under the same rules.
    bridge = bridge_for(inputs)
    assert bridge.admitted == (key,)
    (candidate,) = bridge.batch.candidates
    assert candidate["evidence_state"] == "ready"


def test_the_ready_composition_is_order_and_hash_seed_independent():
    exposures = window_exposures("music", "search")
    first, *_ = build(chart_snapshot(), exposures=exposures)
    second, *_ = build(chart_snapshot(), exposures=list(reversed(exposures)))
    one, two = compose(first), compose(second)
    assert canonical_digest(composition_identity(one, bridge_for(one))) == canonical_digest(
        composition_identity(two, bridge_for(two))
    )


def test_without_exposure_authority_no_direction_is_asserted_and_it_stays_thin():
    composer, *_ = build(chart_snapshot())
    key, readiness, receipts = only_component(compose(composer))
    assert readiness.state == "thin"
    assert "insufficient_directional_agreement" in readiness.reasons
    assert {item.direction for item in receipts.values()} == {"not_applicable"}
    directions = composer.compositions[STAGING_RUN_ID].direction_by_component[key]
    assert {item.reason for item in directions.values()} == {
        "family_exposure_authority_unavailable"
    }


@pytest.mark.parametrize(
    "exposures",
    [
        # One day short for music: a collection gap is never read as a trend.
        window_exposures("search") + window_exposures("music", days=13),
        # A capture that did not complete on one day.
        window_exposures("search")
        + window_exposures("music", days=13)
        + [exposure_receipt("music", DAY - timedelta(days=13), capture_complete=False)],
        # A quota exhausted on one day.
        window_exposures("search")
        + window_exposures("music", days=13)
        + [
            exposure_receipt(
                "music",
                DAY - timedelta(days=13),
                quota_applicability="metered",
                quota_unit="requests",
                quota_limit=10,
                quota_used=10,
                quota_exhausted=True,
            )
        ],
        # A receipt whose digest does not match is not authority.
        window_exposures("search")
        + window_exposures("music", days=13)
        + [dict(exposure_receipt("music", DAY - timedelta(days=13)), receipt_digest="0" * 64)],
        # Two receipts for one day of one family.
        [
            *window_exposures("search", "music"),
            exposure_receipt("music", DAY, config_digest="9" * 64),
        ],
    ],
)
def test_incomplete_exposure_leaves_that_family_not_applicable(exposures):
    composer, *_ = build(chart_snapshot(), exposures=exposures)
    key, readiness, _receipts = only_component(compose(composer))
    assert readiness.state == "thin"
    assert readiness.direction_by_family["music"] == "not_applicable"
    assert readiness.direction_by_family["search"] == "rising"
    directions = composer.compositions[STAGING_RUN_ID].direction_by_component[key]
    assert directions["music"].reason == "family_exposure_authority_unavailable"


def test_too_few_citing_rows_is_an_unknown_direction_not_a_trend():
    composer, *_ = build(chart_snapshot(rising=2), exposures=window_exposures("music", "search"))
    _key, readiness, receipts = only_component(compose(composer))
    assert readiness.state == "thin"
    assert set(readiness.direction_by_family.values()) == {"not_applicable"}
    assert {item.direction for item in receipts.values()} == {"not_applicable"}


def test_a_flat_family_against_its_baseline_is_not_directional():
    composer, *_ = build(
        chart_snapshot(rising=3, baseline=11), exposures=window_exposures("music", "search")
    )
    _key, readiness, _receipts = only_component(compose(composer))
    assert readiness.state != "ready"
    assert set(readiness.direction_by_family.values()) == {"not_applicable"}


def rising_rows(terms, *, method_id="wave1_geo_v1"):
    """Five fresh rows per term citing it, the first being the component's own row."""
    evidence = []
    for term, row_id, source, platform, vendor, channel in terms:
        for count in range(5):
            row = evidence_row(
                term,
                row_id if count == 0 else f"{row_id}_r{count}",
                source,
                platform,
                vendor,
                channel,
            )
            row["geo_method_id"] = method_id
            evidence.append(row)
    return evidence


def test_the_search_market_alone_cannot_reach_ready_whatever_the_direction():
    # The same shape collected through the socialcrawl search: its market rides only as
    # the search frame, so every row is contextual and none clears the geography floor.
    graph = [
        dict(graph_row(term, platform, row_id), co_occur_terms=[item[0] for item in TERMS])
        for term, row_id, _source, platform, _vendor, _channel in TERMS
    ]
    evidence = rising_rows(TERMS, method_id="wave1_seed_market_v1")
    composer, *_ = build(
        snapshot(rows=graph, evidence=evidence),
        exposures=window_exposures("reddit", "youtube"),
    )
    _key, readiness, receipts = only_component(compose(composer))
    assert readiness.state == "thin"
    assert "weak_geo_evidence" in readiness.reasons
    assert {item.method for item in receipts.geography.values()} == {"search_frame"}
    assert {item.geo_confidence for item in receipts.values()} == {0.0}
    assert readiness.qualifying_row_ids == ()


def test_a_global_story_beside_a_local_chart_is_context_not_local_support():
    # News about the market, found through a frame, is contextual evidence and never a
    # second local family, however many rows cite it.
    composer, *_ = build(
        chart_snapshot(
            sources=(("apple_music", "apple_music"), ("rss", "news")),
            titles=(None, "South Africa watches delta chorus climb abroad"),
        ),
        exposures=window_exposures("music", "news"),
    )
    _key, readiness, receipts = only_component(compose(composer))
    assert receipts.geography["ev_delta_01"] == GeographicScope(
        "content_marker", "ev_delta_01", "contextual", 0.4
    )
    assert receipts["ev_delta_01"].geo_confidence == 0.0
    assert readiness.state == "thin"
    assert readiness.qualifying_families == ("music",)


def test_two_families_of_one_origin_cannot_reach_ready():
    # Local, fresh and rising in two families, but both collected through one vendor: two
    # family labels over one origin are never an independent pair.
    terms = (
        ("alpha rhythm", "ev_alpha_01", "socialcrawl", "reddit", "socialcrawl", "reddit"),
        ("beta rhythm", "ev_beta_01", "socialcrawl", "tiktok", "socialcrawl", "short_video"),
    )
    graph = [
        dict(graph_row(term, platform, row_id), co_occur_terms=[item[0] for item in terms])
        for term, row_id, _source, platform, _vendor, _channel in terms
    ]
    composer, *_ = build(
        snapshot(rows=graph, evidence=rising_rows(terms)),
        exposures=window_exposures("reddit", "short_video"),
    )
    inputs = compose(composer)
    # The graph's own pair rule already refuses to join them, so no candidate forms and
    # nothing reaches readiness; the readiness suite holds the same rule for records that
    # do reach it (one origin under two family labels stays thin).
    assert inputs["result"].run.component_count == 0
    assert inputs["readiness_by_component"] == {}
    assert bridge_for(inputs).batch.candidates == ()


def test_foreign_content_on_a_country_chart_is_not_local():
    composer, *_ = build(
        chart_snapshot(titles=("\u041b\u0430\u0433\u043e\u0441 gamma chorus", None)),
        exposures=window_exposures("music", "search"),
    )
    _key, readiness, receipts = only_component(compose(composer))
    assert receipts.geography["ev_gamma_01"] == GeographicScope(
        "script_marker", "ev_gamma_01", "foreign", 0.7
    )
    assert readiness.state != "ready"


def test_only_vendor_measured_inventory_methods_name_a_vendor_region():
    from src.analysis.open_intelligence.source_inventory import CONNECTOR_PROFILES

    assert {
        "app_charts",
        "apple_music",
        "bigquery_trends",
        "cloudflare_radar",
        "google_trends_rss",
        "wikipedia",
    } == daily_composer.VENDOR_MEASURED_SOURCES
    framed = {
        name: profile.geography_method
        for name, profile in CONNECTOR_PROFILES.items()
        if name not in daily_composer.VENDOR_MEASURED_SOURCES
    }
    # Every source selected by a frame stays out, the socialcrawl search among them.
    assert framed == {
        "audiomack": "genre_list_per_market",
        "bluesky": "per_market_terms",
        "ensemble": "market_terms_and_creator_watchlist",
        "gdelt": "source_country_and_location_filters",
        "pulsar": "configured_search_hashes",
        "reddit": "market_subreddit_list",
        "rss": "market_feed_list",
        "semrush": "country_database",
        "socialcrawl": "market_terms_creator_watchlist_geo_verify",
        "spotify": "market_playlist",
        "youtube": "region_code_plus_query_terms",
        "youtube_scrape": "query_terms_per_market",
    }
    # A framed row keeps the kernel's own answer: unknown, never a vendor region.
    row = {"market": "za", "source": "rss", "title": "street style", "text": None, "url": None}
    assert daily_composer._resolve_geography("za", "row_1", row) == UNKNOWN
    # A keyword database row exists because a keyword was asked about, whatever its
    # volume, and an editorial playlist is curated rather than measured listening, so
    # both stay frames and never become a vendor region.
    assert daily_composer._resolve_geography("za", "row_1", dict(row, source="semrush")) == UNKNOWN
    assert daily_composer._resolve_geography("za", "row_1", dict(row, source="spotify")) == UNKNOWN
    chart = dict(row, source="apple_music")
    assert daily_composer._resolve_geography("za", "row_1", chart) == GeographicScope(
        "vendor_region", "row_1", "local", 0.6
    )


def exposure_facts(exposures):
    composer, *_ = build(chart_snapshot(), exposures=exposures)
    inputs = compose(composer)
    key, readiness, _receipts = only_component(inputs)
    return composer.compositions[STAGING_RUN_ID], key, readiness


def test_exposure_read_is_bound_to_the_cutoff_and_a_later_receipt_is_ignored():
    late = [
        exposure_receipt(
            "music", DAY - timedelta(days=offset), issued_at=CUTOFF + timedelta(hours=1)
        )
        for offset in range(daily_composer.FACTOR_WINDOW_DAYS)
    ]
    facts, key, readiness = exposure_facts(window_exposures("search") + late)
    assert readiness.direction_by_family["music"] == "not_applicable"
    assert readiness.direction_by_family["search"] == "rising"
    assert facts.direction_by_component[key]["music"].reason == (
        "family_exposure_authority_unavailable"
    )
    # The receipts issued after the cutoff are not part of what the composition read.
    alone, *_ = exposure_facts(window_exposures("search"))
    assert facts.exposure_readback_digest == alone.exposure_readback_digest
    # A receipt issued exactly at the cutoff is authority.
    at_cutoff = [
        exposure_receipt("music", item["exposure_date"], issued_at=CUTOFF) for item in late
    ]
    _facts, _key, ready = exposure_facts(window_exposures("search") + at_cutoff)
    assert ready.direction_by_family["music"] == "rising"


def test_exposure_readback_digest_is_recorded_and_moves_with_the_read_set():
    exposures = window_exposures("music", "search")
    facts, *_ = exposure_facts(exposures)
    again, *_ = exposure_facts(list(reversed(exposures)))
    assert isinstance(facts.exposure_readback_digest, str)
    assert len(facts.exposure_readback_digest) == 64
    # The same set read in another order is the same readback.
    assert again.exposure_readback_digest == facts.exposure_readback_digest
    fewer, *_ = exposure_facts(exposures[:-1])
    changed, *_ = exposure_facts(
        [
            *exposures[:-1],
            exposure_receipt("search", exposures[-1]["exposure_date"], quota_authority_id="7" * 64),
        ]
    )
    none, *_ = exposure_facts(())
    digests = {
        facts.exposure_readback_digest,
        fewer.exposure_readback_digest,
        changed.exposure_readback_digest,
        none.exposure_readback_digest,
    }
    assert len(digests) == 4


@pytest.mark.parametrize(
    "extra",
    [
        # A malformed row beside a complete window: never skipped while 14 valid days count.
        dict(exposure_receipt("music", DAY), receipt_digest="0" * 64),
        dict(exposure_receipt("music", DAY), quota_applicability="sometimes"),
        # A second receipt for a day the window already covers, identical but for its issue.
        exposure_receipt("music", DAY, issued_at=datetime(2026, 9, 10, 22, tzinfo=UTC)),
    ],
)
def test_a_malformed_or_duplicate_receipt_refuses_that_family_exposure_authority(extra):
    facts, key, readiness = exposure_facts([*window_exposures("music", "search"), extra])
    assert readiness.state == "thin"
    assert readiness.direction_by_family["music"] == "not_applicable"
    assert readiness.direction_by_family["search"] == "rising"
    assert facts.direction_by_component[key]["music"].reason == (
        "family_exposure_authority_unavailable"
    )
    complete, *_ = exposure_facts(window_exposures("music", "search"))
    assert facts.exposure_readback_digest != complete.exposure_readback_digest


def test_a_receipt_row_without_a_family_refuses_every_family():
    unnamed = dict(exposure_receipt("music", DAY), source_family=None)
    _facts, _key, readiness = exposure_facts([*window_exposures("music", "search"), unnamed])
    assert readiness.direction_by_family == {
        "music": "not_applicable",
        "search": "not_applicable",
    }


class UnboundExposureWarehouse(FakeWarehouse):
    """A warehouse that ignores the issued_at bound, so only the composer's own guard applies."""

    def _exposed(self, sql):
        start, end = re.search(
            r"BETWEEN DATE '(\d{4}-\d{2}-\d{2})' AND DATE '(\d{4}-\d{2}-\d{2})'", sql
        ).groups()
        return [
            item
            for item in self.exposures
            if date.fromisoformat(start) <= item["exposure_date"] <= date.fromisoformat(end)
        ]


def test_a_receipt_past_the_cutoff_is_ignored_even_when_the_read_returns_it():
    late = [
        exposure_receipt(
            "music", DAY - timedelta(days=offset), issued_at=CUTOFF + timedelta(microseconds=1)
        )
        for offset in range(daily_composer.FACTOR_WINDOW_DAYS)
    ]
    exposures = window_exposures("search") + late
    composer, *_ = build(chart_snapshot(), exposures=exposures)
    composer.warehouse = UnboundExposureWarehouse(chart_snapshot(), exposures=exposures)
    key, readiness, _receipts = only_component(compose(composer))
    assert readiness.direction_by_family["music"] == "not_applicable"
    assert readiness.direction_by_family["search"] == "rising"
    facts = composer.compositions[STAGING_RUN_ID]
    assert facts.direction_by_component[key]["music"].reason == (
        "family_exposure_authority_unavailable"
    )
    # The rows past the cutoff are not part of the recorded read set.
    alone, *_ = exposure_facts(window_exposures("search"))
    assert facts.exposure_readback_digest == alone.exposure_readback_digest
    # The unbound fake did return them, so the composer's guard is what dropped them.
    (read,) = [sql for sql in composer.warehouse.sql if "issued_at <= TIMESTAMP" in sql]
    assert len(composer.warehouse._exposed(read)) == len(exposures)
