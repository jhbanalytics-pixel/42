from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType, SimpleNamespace

import pytest

RUN_ID = "run_live_001"
SIGNAL_ID = "sig_" + "a" * 64
SIGNAL_DATE = date(2026, 8, 27)
COMPLETED_AT = datetime(2026, 8, 30, 7, 40, tzinfo=UTC)
COPY_RUN_ID = "copy_live_001"


def _v3_rows():
    from src.analysis.open_intelligence import persistence

    from tests.unit.test_dynamic_signal_persistence import _provenance_membership

    receipt, rows = _receipt_and_rows()
    raw = _provenance_membership()["source_provenance_json"]
    member = rows["signal_membership_v2"][0]
    member.update(
        source_provenance_json=raw,
        source_families=("news",),
        vendor_families=("rss",),
        channel_families=("news",),
    )
    member["member_id"] = persistence._provenance_id(member, json.loads(raw))
    rows["signal_candidates_v2"][0]["cluster_build_version"] = "hybrid_graph_v3"
    rows["signal_predictions_v2"][0]["cluster_build_version"] = "hybrid_graph_v3"
    batch = persistence.OpenIntelligenceRowBatch(
        **{table: tuple(rows[name]) for table, name in persistence.TABLE_BINDINGS.items()},
        cluster_build_version="hybrid_graph_v3",
    )
    receipt_values = {
        key: value
        for key, value in asdict(receipt).items()
        if key
        in {
            "run_id",
            "client_scope_id",
            "market_scope",
            "signal_date",
            "observation_start",
            "observation_end",
            "observation_method",
            "source_window_digest",
            "cluster_build_version",
            "source_family_map_version",
            "rule_version",
            "status",
            "complete_partitions",
            "source_sha",
            "completed_at",
        }
    }
    receipt_values.update(
        cluster_build_version="hybrid_graph_v3", rule_version="composition_rules_v3"
    )
    receipt = persistence.build_run_receipt_row(batch, rows["signal_analysis_v2"], **receipt_values)
    rows["open_intelligence_run_receipts_v1"] = [asdict(receipt)]
    return rows


def test_live_reader_v3_round_trip_retains_envelope_and_schema_dispatch():
    from src.analysis.open_intelligence.brain_live_reader import (
        _schema_digest,
        read_live_brain_authority,
        validate_live_brain_read,
    )

    rows = _v3_rows()
    client = _Client(rows)
    read = read_live_brain_authority(client=client, run_id=RUN_ID, signal_id=SIGNAL_ID)
    assert validate_live_brain_read(read) is read
    assert (
        read.membership[0]["source_provenance_json"]
        == rows["signal_membership_v2"][0]["source_provenance_json"]
    )
    assert dict(read.schema_digests)["signal_membership_v2"] != _schema_digest(
        "signal_membership_v2"
    )
    digest_sql = next(sql for relation, sql, _, _ in client.queries if relation == "row_set_digest")
    assert "'hybrid_graph_v3'" in digest_sql


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_envelope",
        "null_envelope",
        "member_id",
        "summary",
        "candidate_missing",
        "candidate_duplicate",
        "candidate_version",
        "receipt_duplicate",
        "receipt_version",
    ],
)
def test_live_reader_v3_withholds_incomplete_or_conflicting_authority(mutation):
    from src.analysis.open_intelligence.brain_live_reader import (
        LiveBrainReadRefusal,
        read_live_brain_authority,
    )

    rows = _v3_rows()
    if mutation == "missing_envelope":
        rows["signal_membership_v2"][0].pop("source_provenance_json")
    elif mutation == "null_envelope":
        rows["signal_membership_v2"][0]["source_provenance_json"] = None
    elif mutation == "member_id":
        rows["signal_membership_v2"][0]["member_id"] = "mem_" + "f" * 64
    elif mutation == "summary":
        rows["signal_membership_v2"][0]["vendor_families"] = ("google",)
    elif mutation == "candidate_missing":
        rows["signal_candidates_v2"] = []
    elif mutation == "candidate_duplicate":
        rows["signal_candidates_v2"] *= 2
    elif mutation == "candidate_version":
        rows["signal_candidates_v2"][0]["cluster_build_version"] = "hybrid_graph_v2"
    elif mutation == "receipt_duplicate":
        rows["open_intelligence_run_receipts_v1"] *= 2
    else:
        rows["open_intelligence_run_receipts_v1"][0]["cluster_build_version"] = "unknown"
    with pytest.raises(LiveBrainReadRefusal):
        read_live_brain_authority(client=_Client(rows), run_id=RUN_ID, signal_id=SIGNAL_ID)


def test_live_reader_legacy_null_column_retains_snapshot_bytes():
    from src.analysis.open_intelligence.brain_live_reader import read_live_brain_authority

    _, rows = _receipt_and_rows()
    first = read_live_brain_authority(client=_Client(rows), run_id=RUN_ID, signal_id=SIGNAL_ID)
    rows["signal_membership_v2"][0]["source_provenance_json"] = None
    second = read_live_brain_authority(client=_Client(rows), run_id=RUN_ID, signal_id=SIGNAL_ID)
    assert first.snapshot_digest == second.snapshot_digest
    assert first.row_projection_digests == second.row_projection_digests


def _scope():
    return {
        "client_scope_id": "live_scope",
        "market_scope": ["za"],
        "brand_config_id": "live_brand",
        "audience_lens_ids": [],
        "theme_id": "live_theme",
        "run_id": RUN_ID,
        "contract_version": "2.0.0",
    }


def _candidate():
    return {
        **_scope(),
        "signal_id": SIGNAL_ID,
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "label": "live signal",
        "label_member_identity": "za|keyword|live signal",
        "cluster_signature": "b" * 64,
        "cluster_build_version": "hybrid_graph_v2",
        "model_version": None,
        "discovery_mode": "dynamic",
        "topic_tags": ["culture"],
        "novelty_score": 0.61,
        "velocity_score": 0.72,
        "breadth_score": 0.53,
        "independence_score": 0.44,
        "historical_similarity": None,
        "geo_confidence": 0.88,
        "evidence_state": "ready",
        "created_at": COMPLETED_AT - timedelta(minutes=5),
    }


def _evidence():
    return {
        **_scope(),
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "signal_id": SIGNAL_ID,
        "evidence_id": "ev_" + "c" * 64,
        "row_id": "row_live_001",
        "source_family": "news",
        "vendor_family": "rss",
        "channel_family": "news",
        "platform": "web",
        "url": "https://example.invalid/live",
        "published_at": COMPLETED_AT - timedelta(days=1),
        "claim_role": "identity",
        "direction": "rising",
        "geo_confidence": 0.88,
        "source_label": "Live source",
        "author_label": None,
        "excerpt": "Observed source excerpt.",
        "metric_label": None,
        "availability": "available",
        "evidence_state": "ready",
        "created_at": COMPLETED_AT - timedelta(minutes=5),
    }


def _membership():
    return {
        **_scope(),
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "signal_id": SIGNAL_ID,
        "member_id": "mem_" + "d" * 64,
        "member_identity": "za|keyword|live signal",
        "candidate_type": "keyword",
        "canonical_value": "live signal",
        "source_families": ["news"],
        "vendor_families": ["rss"],
        "channel_families": ["news"],
        "platforms": ["web"],
        "row_id": "row_live_001",
        "qualifies_evidence": True,
        "created_at": COMPLETED_AT - timedelta(minutes=5),
    }


def _lineage():
    return {
        **_scope(),
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "from_signal_id": "sig_" + "e" * 64,
        "to_signal_id": SIGNAL_ID,
        "relation": "continues",
        "overlap_score": 0.8,
        "created_at": COMPLETED_AT - timedelta(minutes=5),
    }


def _prediction():
    return {
        **_scope(),
        "prediction_id": "pred_" + "f" * 64,
        "signal_id": SIGNAL_ID,
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "discovery_mode": "dynamic",
        "source_families": ["news"],
        "evidence_state": "ready",
        "first_seen_at": COMPLETED_AT - timedelta(days=2),
        "predicted_at": COMPLETED_AT - timedelta(minutes=10),
        "expected_trajectory": "growing",
        "evaluation_date": SIGNAL_DATE + timedelta(days=7),
        "baseline": {"velocity": 0.72, "breadth": 0.53, "evidence_family_count": 1},
        "promotion_target": {"velocity": 0.8, "breadth": 0.6, "evidence_family_count": 2},
        "invalidation_condition": "velocity below baseline",
        "cluster_build_version": "hybrid_graph_v2",
        "source_family_map_version": "channel_family_v1",
        # The prediction contract's own rule version, never the run's.
        "rule_version": "prediction_rules_v1",
        "display_eligible": False,
    }


def _outcome(*, evaluated_at=COMPLETED_AT - timedelta(minutes=1), run_id=RUN_ID):
    return {
        **_scope(),
        "run_id": run_id,
        "outcome_id": "out_" + "1" * 64,
        "prediction_id": "pred_" + "f" * 64,
        "signal_id": SIGNAL_ID,
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "discovery_mode": "dynamic",
        "source_families": ["news"],
        "source_family_map_version": "channel_family_v1",
        "evaluation_date": SIGNAL_DATE + timedelta(days=7),
        "evaluated_at": evaluated_at,
        "outcome": "sustained",
        "observed_velocity": 0.8,
        "observed_breadth": 0.6,
        "observed_evidence_family_count": 2,
        "human_calibration_label": None,
        "human_reviewed_at": None,
        "resolution_reason": "thresholds_met",
        "rule_version": "outcome_rules_v1",
    }


def _analysis():
    return {
        **_scope(),
        "analysis_id": "analysis_live_001",
        "signal_id": SIGNAL_ID,
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "evidence_state": "ready",
        "summary": "Prior claim material.",
        "why_now": "Prior claim material.",
        "possible_response": None,
        "limitations": [],
        "contradictions": [],
        "evidence_ids": ["ev_" + "c" * 64],
        "human_review_required": False,
        "model_version": None,
        "analyzed_at": COMPLETED_AT - timedelta(minutes=2),
    }


def _source_window_digest():
    payload = {
        "copy_run_id": COPY_RUN_ID,
        "completeness": [("enriched_content", 1, 1)],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _receipt_and_rows():
    from src.analysis.open_intelligence import persistence

    batch = persistence.OpenIntelligenceRowBatch(
        (_candidate(),),
        (_evidence(),),
        (_membership(),),
        (_lineage(),),
        (_prediction(),),
        (_outcome(),),
    )
    receipt = persistence.build_run_receipt_row(
        batch,
        (_analysis(),),
        run_id=RUN_ID,
        client_scope_id="live_scope",
        market_scope=("za",),
        signal_date=SIGNAL_DATE,
        observation_start=SIGNAL_DATE,
        observation_end=SIGNAL_DATE,
        observation_method="dynamic_source_copy_apply_v1",
        source_window_digest=_source_window_digest(),
        cluster_build_version="hybrid_graph_v2",
        source_family_map_version="channel_family_v1",
        rule_version="rules_v1",
        status="completed",
        complete_partitions=True,
        source_sha="5" * 40,
        completed_at=COMPLETED_AT,
    )
    rows = {
        "open_intelligence_run_receipts_v1": [asdict(receipt)],
        "signal_candidates_v2": [dict(batch.candidates[0])],
        "signal_evidence_v2": [dict(batch.evidence[0])],
        "signal_membership_v2": [dict(batch.membership[0])],
        "signal_lineage_v2": [dict(batch.lineage[0])],
        "signal_analysis_v2": [_analysis()],
        "signal_predictions_v2": [dict(batch.predictions[0])],
        "signal_outcomes_v2": [dict(batch.outcomes[0])],
        "open_intelligence_source_copy_manifest_v1": [
            {
                "copy_run_id": COPY_RUN_ID,
                "target_table": "enriched_content",
                "window_start": SIGNAL_DATE,
                "window_end": SIGNAL_DATE,
                "manifest_rows": 1,
            }
        ],
        "open_intelligence_source_copy_receipts_v1": [
            {
                "copy_run_id": COPY_RUN_ID,
                "source_table": "enriched_content",
                "window_start": SIGNAL_DATE,
                "window_end": SIGNAL_DATE,
                "source_rows": 1,
                "source_set_digest": "7" * 64,
                "schema_digest": "8" * 64,
                "filter_digest": "9" * 64,
            }
        ],
    }
    return receipt, rows


class _Job:
    statement_type = "SELECT"

    def __init__(self, rows, job_id):
        self._rows = rows
        self.job_id = job_id

    def result(self, max_results=None):
        return tuple(self._rows[:max_results])


class _Client:
    project = "ogilvy-trends-v2"
    location = "US"
    _credentials = SimpleNamespace(
        service_account_email=("trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com")
    )

    def __init__(self, rows, reported_digest=None):
        self.rows = rows
        self.queries = []
        self.reported_digest = reported_digest

    def query(self, sql, job_config=None, location=None):
        if "AS row_set_digest" in sql:
            # The tables report the digest of the rows they hold through the digest SQL; the
            # fake derives it from its own rows the way the receipt builder does.
            from src.analysis.open_intelligence import persistence
            from src.analysis.open_intelligence.brain_contract import (
                canonical_bytes,
                canonical_digest,
            )

            reported = self.reported_digest
            if reported is None:
                families = {
                    table: [
                        persistence.canonical_typed_json(
                            table,
                            row,
                            cluster_build_version=self.rows["open_intelligence_run_receipts_v1"][0][
                                "cluster_build_version"
                            ],
                        )
                        for row in sorted(
                            (dict(r) for r in self.rows[name]),
                            key=lambda r: tuple(str(r[k]) for k in persistence.NATURAL_KEYS[table]),
                        )
                    ]
                    for table, name in persistence.TABLE_BINDINGS.items()
                }
                families["analysis"] = sorted(
                    canonical_bytes(dict(row)).decode("utf-8")
                    for row in self.rows.get("signal_analysis_v2", ())
                )
                reported = canonical_digest(families)
            self.queries.append(("row_set_digest", sql, job_config, location))
            return _Job([{"row_set_digest": reported}], f"job_{len(self.queries)}")
        relation = next(name for name in self.rows if f".{name}`" in sql)
        self.queries.append((relation, sql, job_config, location))
        rows = list(self.rows[relation])
        if relation == "signal_outcomes_v2":
            cutoff = COMPLETED_AT if "@completed_at" in sql else None
            rows = [row for row in rows if cutoff is None or row["evaluated_at"] <= cutoff]
        return _Job(rows, f"job_{len(self.queries)}")


def test_live_reader_verifies_run_before_later_authority_and_runs_dark_brain():
    from src.analysis.open_intelligence.brain import _run_intelligence_brain_from_authority
    from src.analysis.open_intelligence.brain_authority import (
        _issue_live_brain_evidence_snapshot,
        _issue_live_brain_runtime_request,
    )
    from src.analysis.open_intelligence.brain_live_reader import read_live_brain_authority

    _receipt, rows = _receipt_and_rows()
    client = _Client(rows)
    read = read_live_brain_authority(client=client, run_id=RUN_ID, signal_id=SIGNAL_ID)
    runtime = _issue_live_brain_runtime_request(read, research_depth="briefing")
    snapshot = _issue_live_brain_evidence_snapshot(runtime, read)
    result = _run_intelligence_brain_from_authority(runtime, snapshot)

    relations = [item[0] for item in client.queries]
    assert relations[:9] == [
        "open_intelligence_run_receipts_v1",
        "signal_candidates_v2",
        "signal_evidence_v2",
        "signal_membership_v2",
        "signal_lineage_v2",
        "signal_analysis_v2",
        "signal_predictions_v2",
        "signal_outcomes_v2",
        "row_set_digest",
    ]
    assert relations[9:] == [
        "signal_outcomes_v2",
        "open_intelligence_source_copy_manifest_v1",
        "open_intelligence_source_copy_receipts_v1",
    ]
    # Every row read is parameterised; the digest read embeds the run id the receipt row
    # already bound, validated by the digest builder's own grammar.
    assert all(
        query[2].query_parameters for query in client.queries if query[0] != "row_set_digest"
    )
    assert all(query[3] == "US" for query in client.queries)
    assert read.read_receipt["run_receipt_verified"] is True
    assert result.overall_admission.availability_state == "unavailable"
    assert result.overall_admission.ready_for_downstream is False
    assert result.analyst.why_now.state == "unavailable"
    assert result.editor.red_thread.state == "unavailable"
    assert {node.node_type for node in result.evidence_graph.nodes} == {
        "signal",
        "source",
        "market",
        "observation",
        "receipt",
    }
    assert result.model_usage_receipt_ids == ()


def test_receipt_mismatch_refuses_before_later_outcome_or_source_copy_reads():
    from src.analysis.open_intelligence.brain_live_reader import (
        LiveBrainReadRefusal,
        read_live_brain_authority,
    )

    _receipt, rows = _receipt_and_rows()
    rows["signal_evidence_v2"][0]["row_id"] = "row_changed"
    client = _Client(rows)

    with pytest.raises(LiveBrainReadRefusal, match="row_set_digest"):
        read_live_brain_authority(client=client, run_id=RUN_ID, signal_id=SIGNAL_ID)

    assert [item[0] for item in client.queries] == [
        "open_intelligence_run_receipts_v1",
        "signal_candidates_v2",
        "signal_evidence_v2",
        "signal_membership_v2",
        "signal_lineage_v2",
        "signal_analysis_v2",
        "signal_predictions_v2",
        "signal_outcomes_v2",
        "row_set_digest",
    ]


def test_live_reader_uses_the_dedicated_brain_identity():
    from src.analysis.open_intelligence import brain_live_reader

    assert brain_live_reader.TARGET_IDENTITY == (
        "trends-engine-oi-brain@ogilvy-trends-v2.iam.gserviceaccount.com"
    )


def test_reader_refuses_future_evidence_and_nonselect_jobs():
    from src.analysis.open_intelligence.brain_live_reader import (
        LiveBrainReadRefusal,
        read_live_brain_authority,
    )

    _receipt, rows = _receipt_and_rows()
    rows["signal_evidence_v2"][0]["published_at"] = COMPLETED_AT + timedelta(seconds=1)
    client = _Client(rows)
    with pytest.raises(LiveBrainReadRefusal, match="future"):
        read_live_brain_authority(client=client, run_id=RUN_ID, signal_id=SIGNAL_ID)

    _receipt, rows = _receipt_and_rows()
    client = _Client(rows)
    original = client.query

    def nonselect(*args, **kwargs):
        job = original(*args, **kwargs)
        job.statement_type = "INSERT"
        return job

    client.query = nonselect
    with pytest.raises(LiveBrainReadRefusal, match="SELECT"):
        read_live_brain_authority(client=client, run_id=RUN_ID, signal_id=SIGNAL_ID)


def test_fixture_result_digest_is_unchanged_after_live_capabilities_exist():
    from src.analysis.open_intelligence.brain import run_intelligence_brain
    from src.analysis.open_intelligence.brain_contract import IntelligenceBrainIntent

    intent = IntelligenceBrainIntent(
        "intelligence_brain_v1",
        "inv_fixture_01",
        "sig_" + "a" * 64,
        "investigation",
        "What changed?",
        None,
    )
    assert run_intelligence_brain(intent).result_digest == (
        "f44cb165b1a793ebdb4aad74071c492a108c77b1cfcc08ff252be97ad85c58c6"
    )


def test_cli_execute_uses_live_reader_and_reports_zero_side_effects(monkeypatch):
    from scripts.staging import run_live_intelligence_brain as cli

    from tests.unit.test_intelligence_brain_live_cli import _mock_durable_brain

    _receipt, rows = _receipt_and_rows()
    client = _Client(rows)
    monkeypatch.setattr(cli.bigquery, "Client", lambda **_kwargs: client)
    _mock_durable_brain(monkeypatch, cli, _receipt.source_sha)

    payload = cli._execute(
        run_id=RUN_ID,
        signal_id=SIGNAL_ID,
        research_depth="briefing",
        runtime_identity_verifier=lambda _args: type(
            "Identity", (), {"source_sha": _receipt.source_sha}
        )(),
        bigquery_factory=lambda: client,
    )

    assert payload["model_calls"] == 0
    assert payload["persisted"] is False
    assert payload["brain_result"]["overall_admission"]["ready_for_downstream"] is False
    assert all(item[2].use_legacy_sql is False for item in client.queries)


def _issued_read():
    from src.analysis.open_intelligence.brain_live_reader import read_live_brain_authority

    _receipt, rows = _receipt_and_rows()
    return read_live_brain_authority(client=_Client(rows), run_id=RUN_ID, signal_id=SIGNAL_ID)


def _assert_read_and_runtime_refuse(read):
    from src.analysis.open_intelligence.brain_authority import _issue_live_brain_runtime_request
    from src.analysis.open_intelligence.brain_live_reader import (
        LiveBrainReadRefusal,
        validate_live_brain_read,
    )

    with pytest.raises(LiveBrainReadRefusal, match="authority"):
        validate_live_brain_read(read)
    with pytest.raises(LiveBrainReadRefusal, match="authority"):
        _issue_live_brain_runtime_request(read, research_depth="briefing")


def test_live_read_rejects_candidate_replacement_before_runtime_issuance():
    read = _issued_read()
    candidate = dict(read.candidate)
    candidate["signal_id"] = "sig_" + "9" * 64
    object.__setattr__(read, "candidate", MappingProxyType(candidate))

    _assert_read_and_runtime_refuse(read)


def test_live_read_rejects_projection_replacement_before_runtime_issuance():
    read = _issued_read()
    replacement = (*read.row_projection_digests[:-1], ("foreign", "a" * 64))
    object.__setattr__(read, "row_projection_digests", replacement)

    _assert_read_and_runtime_refuse(read)


def test_live_read_rejects_read_receipt_replacement_before_runtime_issuance():
    read = _issued_read()
    receipt = dict(read.read_receipt)
    receipt["run_receipt_verified"] = False
    object.__setattr__(read, "read_receipt", MappingProxyType(receipt))

    _assert_read_and_runtime_refuse(read)


def test_live_read_rejects_coherent_public_rehash_before_runtime_issuance():
    from src.analysis.open_intelligence.brain_contract import canonical_digest

    read = _issued_read()
    candidate = dict(read.candidate)
    candidate["signal_id"] = "sig_" + "8" * 64
    root = dict(read.snapshot_root)
    scope = dict(root["resolved_scope"])
    scope["signal_id"] = candidate["signal_id"]
    root["resolved_scope"] = scope
    object.__setattr__(read, "candidate", MappingProxyType(candidate))
    object.__setattr__(read, "snapshot_root", MappingProxyType(root))
    object.__setattr__(read, "snapshot_digest", canonical_digest(root))

    _assert_read_and_runtime_refuse(read)


def test_live_read_rejects_direct_construction():
    from src.analysis.open_intelligence.brain_live_reader import (
        LiveBrainRead,
        LiveBrainReadRefusal,
    )

    read = _issued_read()
    fields = tuple(getattr(read, field) for field in read.__dataclass_fields__)
    with pytest.raises(LiveBrainReadRefusal, match="authority"):
        LiveBrainRead(*fields)


def test_live_read_has_no_writable_registry_or_registry_injection_path(monkeypatch):
    from src.analysis.open_intelligence import brain_live_reader

    assert not isinstance(getattr(brain_live_reader, "_ISSUED_READS", None), dict)
    assert not isinstance(getattr(brain_live_reader, "_LIVE_READ_REGISTRY", None), dict)
    monkeypatch.setattr(brain_live_reader, "_ISSUED_READS", {}, raising=False)

    read = _issued_read()
    assert brain_live_reader.validate_live_brain_read(read) is read


def test_reader_binds_the_accepted_row_contract_version_not_the_canary_one():
    # The R3 apply writes rows under the accepted v2 contract; a reader bound to any other
    # version reads zero rows for every relation and refuses the run as a count mismatch.
    from src.analysis.open_intelligence import brain_live_reader as reader
    from src.contracts.open_intelligence import CONTRACT_VERSION

    receipt = SimpleNamespace(client_scope_id="live_scope", run_id=RUN_ID, signal_date=SIGNAL_DATE)
    bound = {p.name: p.value for p in reader._run_parameters(receipt)}
    assert bound["contract_version"] == CONTRACT_VERSION == "2.0.0"


def test_reader_summarises_the_copy_manifest_per_table_instead_of_reading_every_row():
    # The copy manifest holds one row per copied source row, hundreds of thousands for a
    # fourteen day window, far past the reader's row ceiling. The authority check only needs
    # the count per target table, so the reader asks the warehouse for that summary.
    from src.analysis.open_intelligence.brain_live_reader import read_live_brain_authority

    _receipt, rows = _receipt_and_rows()
    client = _Client(rows)
    read_live_brain_authority(client=client, run_id=RUN_ID, signal_id=SIGNAL_ID)
    manifest_queries = [
        sql
        for relation, sql, _config, _location in client.queries
        if relation == "open_intelligence_source_copy_manifest_v1"
    ]
    assert len(manifest_queries) == 1
    sql = manifest_queries[0]
    assert "COUNT(*) AS manifest_rows" in sql
    assert "GROUP BY" in sql
    assert "copy_row_id" not in sql


def test_reader_compares_market_scope_as_a_set_not_an_order():
    # The receipt keeps the scope's market order and the persisted rows carry it sorted; the
    # market scope is a set, so the live read compares it as one.
    from src.analysis.open_intelligence.brain_live_reader import read_live_brain_authority

    receipt, rows = _receipt_and_rows()
    for name in (
        "signal_candidates_v2",
        "signal_evidence_v2",
        "signal_membership_v2",
        "signal_lineage_v2",
        "signal_analysis_v2",
        "signal_predictions_v2",
        "signal_outcomes_v2",
    ):
        for row in rows[name]:
            row["market_scope"] = ["ng", "za"]
    for row in rows["open_intelligence_run_receipts_v1"]:
        row["market_scope"] = ["za", "ng"]
    # Only the order differs, so the tables report the receipt's own digest.
    client = _Client(rows, reported_digest=receipt.row_set_digest)
    read = read_live_brain_authority(client=client, run_id=RUN_ID, signal_id=SIGNAL_ID)
    assert tuple(read.receipt.market_scope) == ("za", "ng")


def test_live_reader_accepts_a_prediction_row_carrying_the_prediction_rule_version():
    # A prediction row carries the prediction contract's own rule version
    # (PredictionRules.rule_version), not the run's composition rule version;
    # the first released run (r10, 4 Sep 2026) refused every Brain read on
    # "prediction authority mismatch" for exactly that reason.
    from src.analysis.open_intelligence.brain_live_reader import (
        LiveBrainReadRefusal,
        read_live_brain_authority,
    )
    from src.analysis.open_intelligence.predictions import PredictionRules

    receipt, rows = _receipt_and_rows()
    assert rows["signal_predictions_v2"][0]["rule_version"] == PredictionRules().rule_version
    assert receipt.rule_version != PredictionRules().rule_version
    read = read_live_brain_authority(client=_Client(rows), run_id=RUN_ID, signal_id=SIGNAL_ID)
    assert read.predictions[0]["rule_version"] == "prediction_rules_v1"
    foreign = dict(rows["signal_predictions_v2"][0], rule_version="somebody_elses_rules_v9")
    rows["signal_predictions_v2"] = [foreign]
    with pytest.raises(LiveBrainReadRefusal, match="prediction authority mismatch"):
        read_live_brain_authority(
            client=_Client(rows, reported_digest=receipt.row_set_digest),
            run_id=RUN_ID,
            signal_id=SIGNAL_ID,
        )
