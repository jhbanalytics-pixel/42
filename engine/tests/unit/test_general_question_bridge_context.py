"""Ask reads a bridge v3 clone when no v1 protected context profile covers the window.

The bridge capture is the synthetic, unissued approval ledger world of the loader tests,
moved to cutoff 2026-09-22. The bridge reads run through the real query ledger and
executor against a fake BigQuery transport; no network, provider or native call is made.
"""

import copy
import json
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence import general_question_snapshot as snapshot
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

from tests.unit import test_general_question_query_execution as native
from tests.unit.bridge_shift_fixture import shifted_world
from tests.unit.test_protected_context_registry import document, fixture_document, local

AS_OF = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)
WINDOW = {"start": "2026-09-16", "end": "2026-09-22", "closed": True}
PROFILE = "staging_bridge_v3_20260922"
CLONE = "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260922_"
TREND_ROWS = [
    {"market": "za", "trend_date": "2026-09-21", "term": "load shedding"},
    {"market": "za", "trend_date": "2026-09-21", "term": "rugby"},
]
ENRICHED_ROWS = [
    {
        "id": "za-article-1",
        "market": "za",
        "source": "news",
        "source_family": "news",
        "platform": "web",
        "author_handle": None,
        "url": "https://example.test/load-shedding",
        "title": "Load shedding schedule",
        "text": "Load shedding returns to stage two across Gauteng this evening.",
        "published_at": "2026-09-22T08:00:00Z",
        "collected_at": "2026-09-22T09:00:00Z",
        # The run the capture's receipted funded execution stamped into the rows it wrote.
        "pipeline_run_id": "5f0c2a9e-3b1d-4c7a-9e2f-8d6b4a1c0e7f",
    }
]


def bridge():
    return shifted_world(2).test_bridge_ledger_route


ORIGINAL_DRAFT = native.runtime.plan_draft


def plan_draft(window=WINDOW):
    value = ORIGINAL_DRAFT()
    value["window"] = dict(window)
    value["requirements"][0]["search_terms"] = ["load shedding"]
    value["requirements"][0]["question"] = "Which admitted load shedding observations matter?"
    return value


def clients(w, **overrides):
    """The loader's native clients for the synthetic capture, as a production provider would."""
    route = bridge()
    loader = route.chain_route
    if "ledger" not in w:
        value = {
            "capture_clients": w["capture_clients"],
            "read_input": loader.reader(w["raw"]),
            "read_grant": loader.grant_reader(w["grant"]),
            "evidence": w["evidence"],
            "ledger_reader": None,
            "generation_loader": None,
            **loader.clone_readers(w["clones"]),
        }
        value.update(overrides)
        return value
    value = {
        "capture_clients": None,
        "read_input": loader.reader(w["ledger_raw"]),
        "read_grant": None,
        "evidence": w["evidence"],
        "ledger_reader": route.ledger_rows(w),
        "generation_loader": w["ledger"]["catalogue"],
        **loader.clone_readers(w["clones"]),
        "read_native_job": route.job_reader(w),
    }
    value.update(overrides)
    return value


class BridgeHTTP(native.HTTP):
    """Answers each bridge read with the rows its clone holds, keyed by the clone table."""

    def __init__(self, identity, *, bucket, tables):
        super().__init__(identity, bucket=bucket)
        self.tables = tables
        self.sql = []
        self.parameters = []

    def request(self, method, url, **kwargs):
        if method == "POST":
            query = json.loads(kwargs["data"])["configuration"]["query"]
            sql = query["query"]
            self.sql.append(sql)
            self.parameters.append(query.get("queryParameters"))
            (lane,) = [lane for lane in self.tables if f"`{CLONE}{lane}`" in sql]
            rows = [{"row_json": json.dumps(row)} for row in self.tables[lane]]

            def result(body):
                body.update(
                    totalRows=str(len(rows)),
                    schema={"fields": [{"name": "row_json", "type": "STRING"}]},
                    rows=[{"f": [{"v": row["row_json"]}]} for row in rows],
                )

            self.result_mutate = result
        return super().request(method, url, **kwargs)


def ask(
    monkeypatch,
    tmp_path,
    *,
    window=WINDOW,
    register=True,
    tables=None,
    chain=False,
    only=False,
    wired=True,
    world_changes=None,
):
    monkeypatch.setattr(native.f, "NOW", AS_OF)
    monkeypatch.setattr(native.runtime, "plan_draft", lambda: plan_draft(window))
    route = bridge()
    if chain:
        w = route.chain_route.world()
        if register:
            route.chain_route.register(monkeypatch, tmp_path, w["capture"])
    else:
        w = route.ledger_world(tmp_path / "bridge", **(world_changes or {}))
        if register:
            entry = route.register(monkeypatch, tmp_path, w)
            if only:
                local(
                    monkeypatch,
                    tmp_path,
                    {**document(), "entries": [entry]},
                )
    if not register:
        # No bridge row at all: a committed bridge row would cover the window too.
        local(monkeypatch, tmp_path, fixture_document())
    store, bucket, invocation, identity, credentials, plan = native.fixture()
    http = BridgeHTTP(
        identity,
        bucket=bucket,
        tables=tables
        if tables is not None
        else {
            "enriched_content": copy.deepcopy(ENRICHED_ROWS),
            "raw_content": [],
            "trend_analysis": copy.deepcopy(TREND_ROWS),
        },
    )
    native.install(monkeypatch, http)
    if wired:
        # Stands in for the production provider route A item 4 builds.
        from src.analysis.open_intelligence import general_question_context_admission as admission

        monkeypatch.setattr(
            admission, "PRODUCTION_BRIDGE_CLIENTS", lambda credentials, world=w: clients(world)
        )
    return {
        "world": w,
        "store": store,
        "bucket": bucket,
        "invocation": invocation,
        "identity": identity,
        "credentials": credentials,
        "plan": plan,
        "http": http,
    }


def build(values, **overrides):
    arguments = {
        "store": values["store"],
        "scope": native.f.scope(),
        "runtime_identity": values["identity"],
        "credentials": values["credentials"],
        "plan": values["plan"],
        "candidate_limit": 100,
        "evidence_limit": 200,
        "discovery_bytes": 100_000_000,
        "source_bytes": 300_000_000,
        "release_bytes": 50_000_000,
        "now": AS_OF,
        "bridge_clients": lambda credentials: clients(values["world"]),
    }
    arguments.update(overrides)
    return snapshot.build_general_question_snapshot(values["invocation"], **arguments)


def records(values, count):
    from src.analysis.open_intelligence.general_question_queries import GeneralQuestionQueries

    queries = GeneralQuestionQueries(values["store"])
    return {
        ordinal: queries.read_query(
            values["invocation"]["request_id"], scope=native.f.scope(), ordinal=ordinal
        )
        for ordinal in range(1, count + 1)
    }


def restore(values, stored, query_records):
    context = values["store"].read_request(
        values["invocation"]["request_id"], scope=native.f.scope()
    )
    return snapshot.validate_stored_general_question_snapshot(
        stored,
        request=context["request"],
        intake=context["intake"],
        plan=values["plan"],
        query_records=query_records,
    )


def resign(stored):
    stored["snapshot_digest"] = canonical_digest(
        {key: value for key, value in stored.items() if key != "snapshot_digest"}
    )
    return stored


@pytest.fixture
def admitted(monkeypatch, tmp_path):
    values = ask(monkeypatch, tmp_path)
    result = build(values)
    assert result["status"] == "admitted", result
    return values, result["snapshot"]


def test_an_admitted_bridge_clone_answers_a_window_ending_on_its_cutoff(admitted):
    values, stored = admitted
    assert stored["window"] == WINDOW
    provenance = stored["provenance"]
    assert provenance["profile"] == "protected_bridge_context_v1"
    source = provenance["bridge_source"]
    assert source["authority"] == "execution_ledger"
    assert source["registry_entry"]["profile_id"] == PROFILE
    assert source["registry_entry"]["cutoff_date"] == "2026-09-22"
    result = values["world"]["ledger"]["result"]
    assert source["binding"]["result_id"] == result.result_id
    assert source["binding"]["market_scope"] == ["za"]
    # Every read named one pinned clone and went through the query ledger.
    sql = values["http"].sql
    assert len(sql) == 3
    assert all(statement.count(f"`{CLONE}") == 1 for statement in sql)
    assert {record["reservation"]["template_id"] for record in records(values, 3).values()} == {
        "protected_context_bridge_collection_v1",
        "protected_context_bridge_history_v1",
    }
    # Each receipt cites its row and the capture that admitted it.
    by_row = {receipt["source_row_id"]: receipt for receipt in stored["receipts"]}
    article = by_row["za-article-1"]
    assert article["published_at"] == "2026-09-22T08:00:00+00:00"
    assert any(result.result_id in line and PROFILE in line for line in article["limitations"])
    trend = [
        receipt for receipt in stored["receipts"] if receipt["source_label"] == "trend_analysis"
    ]
    assert len(trend) == 1
    assert "load shedding" in trend[0]["excerpt"]
    admission = provenance["admission"]
    cited = {item["receipt_id"]: item for item in admission["provenance"]["row_provenance"]}
    assert set(cited) == {receipt["receipt_id"] for receipt in stored["receipts"]}
    for item in cited.values():
        assert item["result_id"] == result.result_id
        assert item["registry_entry_digest"] == canonical_digest(source["registry_entry"])
    assert cited[trend[0]["receipt_id"]]["completion_entry_digest"]
    assert stored["fulfilled_requirement_ids"] == ["synthetic_mobility"]
    assert stored["missing_work"] == []
    # Unavailable history cells are named limitations, never zero rows.
    assert any(
        "trend_scores" in line and "outside_history_completion_set" in line
        for line in stored["limitations"]
    )
    assert any("seed_candidates" in line for line in stored["limitations"])


def test_the_admitted_bridge_snapshot_is_answerable_and_reads_back(admitted):
    from src.analysis.open_intelligence.general_question_answer import _answer_model_view

    values, stored = admitted
    context = values["store"].read_request(
        values["invocation"]["request_id"], scope=native.f.scope()
    )
    _answer_model_view(context["request"], context["intake"], values["plan"], stored)
    assert restore(values, stored, records(values, 3)) == stored
    written = values["store"]._objects.read(
        f"requests/{values['invocation']['request_id']}/snapshot.json"
    )
    assert written.value == stored


def test_bridge_reads_keep_the_existing_cost_and_evidence_limits(admitted):
    values, stored = admitted
    limits = stored["provenance"]["limits"]
    assert limits["effective_evidence_limit"] == snapshot.SUPPLEMENT_EVIDENCE_LIMIT
    caps = limits["query_caps"]
    assert caps == snapshot._context_query_caps(100_000_000, 300_000_000, 50_000_000)
    reserved = {
        record["reservation"]["template_id"] + ":" + str(record["reservation"]["ordinal"]): record[
            "reservation"
        ]["maximum_bytes_billed"]
        for record in records(values, 3).values()
    }
    assert sorted(reserved.values()) == sorted([caps["enriched"], caps["raw"], caps["candidate"]])


def test_a_refused_bridge_clone_yields_a_coded_coverage_gap(monkeypatch, tmp_path):
    values = ask(monkeypatch, tmp_path)
    result = build(
        values,
        bridge_clients=lambda credentials: clients(
            values["world"], read_native_job=lambda job_id: None
        ),
    )
    assert result == {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "coverage_gap",
        "snapshot": None,
        "reason": "coverage_incomplete",
        "missing_work": ["bridge_source_invalid"],
    }
    assert values["http"].calls == []
    assert (
        values["store"]._objects.read(
            f"requests/{values['invocation']['request_id']}/snapshot.json"
        )
        is None
    )


def test_a_window_past_the_bridge_ceiling_is_an_uncovered_gap(monkeypatch, tmp_path):
    values = ask(monkeypatch, tmp_path, window={**WINDOW, "end": "2026-09-23", "closed": False})
    result = build(values)
    assert result["status"] == "coverage_gap"
    assert result["missing_work"] == ["bridge_source_uncovered"]
    assert values["http"].calls == []


def test_an_uncovered_window_builds_no_bridge_clients(monkeypatch, tmp_path):
    values = ask(monkeypatch, tmp_path, window={**WINDOW, "end": "2026-09-23", "closed": False})
    built = []
    result = build(values, bridge_clients=lambda credentials: built.append(credentials))
    assert result["missing_work"] == ["bridge_source_uncovered"]
    assert built == []


def test_without_a_bridge_row_the_window_stays_uncovered(monkeypatch, tmp_path):
    values = ask(monkeypatch, tmp_path, register=False)
    result = build(values)
    assert result["status"] == "coverage_gap"
    assert result["missing_work"] == ["bridge_source_uncovered"]


def test_the_default_provider_refuses_until_production_clients_are_wired(monkeypatch, tmp_path):
    values = ask(monkeypatch, tmp_path, wired=False)
    result = build(values, bridge_clients=None)
    assert result["status"] == "coverage_gap"
    assert result["missing_work"] == ["bridge_clients_unavailable"]
    assert values["http"].calls == []


def test_a_provider_that_omits_a_client_is_refused(monkeypatch, tmp_path):
    values = ask(monkeypatch, tmp_path)
    partial = clients(values["world"])
    partial.pop("read_clone")
    result = build(values, bridge_clients=lambda credentials: partial)
    assert result["missing_work"] == ["bridge_clients_unavailable"]


def test_a_row_outside_the_admitted_cells_refuses_the_whole_read(monkeypatch, tmp_path):
    rows = [*TREND_ROWS, {"market": "za", "trend_date": "2026-09-20", "term": "load shedding"}]
    values = ask(
        monkeypatch,
        tmp_path,
        tables={"enriched_content": [], "raw_content": [], "trend_analysis": rows},
    )
    result = build(values)
    assert result["status"] == "coverage_gap"
    assert result["missing_work"] == ["bridge_context_invalid"]
    assert (
        values["store"]._objects.read(
            f"requests/{values['invocation']['request_id']}/snapshot.json"
        )
        is None
    )


def test_no_matching_bridge_row_is_insufficient_evidence(monkeypatch, tmp_path):
    values = ask(
        monkeypatch,
        tmp_path,
        tables={
            "enriched_content": [],
            "raw_content": [],
            "trend_analysis": [{"market": "za", "trend_date": "2026-09-21", "term": "rugby"}],
        },
    )
    result = build(values)
    assert result["status"] == "coverage_gap"
    assert result["reason"] == "evidence_insufficient"


def _forge(stored, change):
    forged = copy.deepcopy(stored)
    change(forged["provenance"]["bridge_source"], forged)
    return resign(forged)


def _other_result(source, _stored):
    payload = json.loads(source["result_json"])
    payload["snapshot_digest"] = "0" * 64
    source["result_json"] = canonical_bytes(payload).decode()


def _flip_authority(source, _stored):
    source["authority"] = "daily_chain"


def _other_entry(source, _stored):
    source["registry_entry"]["result_id"] = "exr_" + "1" * 64
    source["binding"]["result_id"] = "exr_" + "1" * 64


def _binding_digest(source, _stored):
    source["binding"]["registry_entry_digest"] = "2" * 64


def _extra_cell(source, _stored):
    (entry,) = [
        entry
        for entry in source["history_completion_set"]["entries"]
        if (entry["lane"], entry["market"], entry["product_date"])
        == ("trend_analysis", "za", "2026-09-22")
    ]
    assert entry["state"] == "unavailable"
    entry.update(state="empty", row_count=0, reason_code=None)


def _other_snapshot(source, _stored):
    source["binding"]["snapshot_digest"] = "3" * 64


def _other_scope(source, _stored):
    source["binding"]["market_scope"] = ["ng", "za"]


def _v1_claim(source, stored):
    stored["provenance"]["profile"] = "protected_context_v3"


@pytest.mark.parametrize(
    "change",
    [
        _other_result,
        _flip_authority,
        _other_entry,
        _binding_digest,
        _extra_cell,
        _other_snapshot,
        _other_scope,
        _v1_claim,
    ],
)
def test_a_stored_snapshot_forging_a_bridge_source_fails_readback(admitted, change):
    values, stored = admitted
    query_records = records(values, 3)
    with pytest.raises(ValueError, match=r"^snapshot_invalid$"):
        restore(values, _forge(stored, change), query_records)


def test_a_bridge_snapshot_whose_registry_row_is_gone_fails_readback(
    admitted, monkeypatch, tmp_path
):
    values, stored = admitted
    query_records = records(values, 3)
    reverted = tmp_path / "reverted"
    reverted.mkdir()
    local(monkeypatch, reverted, fixture_document())
    with pytest.raises(ValueError, match=r"^snapshot_invalid$"):
        restore(values, stored, query_records)


def test_a_bridge_snapshot_with_a_substituted_row_fails_readback(admitted):
    values, stored = admitted
    query_records = records(values, 3)
    forged = copy.deepcopy(stored)
    for capture in forged["provenance"]["captures"].values():
        for row in capture["rows"]:
            row["row_json"] = row["row_json"].replace("rugby", "fuel")
    with pytest.raises(ValueError, match=r"^snapshot_invalid$"):
        restore(values, resign(forged), query_records)


def test_the_source_window_ceiling_moves_to_the_bridge_cutoff(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import general_question_context_admission as admission

    source_window_hint = admission.source_window_hint

    route = bridge()
    w = route.ledger_world(tmp_path / "bridge")
    assert source_window_hint("2026-09-23T01:00:00Z")["cutoff_date"] == "2026-09-07"
    entry = route.register(monkeypatch, tmp_path, w)
    monkeypatch.setattr(admission, "PRODUCTION_BRIDGE_CLIENTS", lambda credentials: clients(w))
    hint = source_window_hint("2026-09-23T01:00:00Z")
    assert hint == {
        "contract_version": "general_question_source_window_hint_v1",
        **{
            key: entry[key]
            for key in (
                "profile_id",
                "cutoff_date",
                "manifest_sha256",
                "consumption_id",
                "result_id",
                "result_digest",
            )
        },
    }
    # The ceiling is a closed day before as_of; on the cutoff day itself it stays at 7 Sept.
    assert source_window_hint("2026-09-22T23:00:00Z")["cutoff_date"] == "2026-09-07"
    assert source_window_hint("2026-09-23T01:00:00Z", retained=hint, validate_retained=True) == hint
    forged = {**hint, "result_digest": "4" * 64}
    with pytest.raises(ValueError, match=r"^protected_context_invalid$"):
        source_window_hint("2026-09-23T01:00:00Z", retained=forged, validate_retained=True)


def test_a_bridge_cutoff_moves_no_ceiling_until_bridge_reads_are_wired(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import general_question_context_admission as admission

    values = ask(monkeypatch, tmp_path, window=WINDOW, wired=False)
    # Unwired, the planner would offer a week Ask then refuses, so the ceiling stays put.
    assert admission.source_window_hint("2026-09-23T01:00:00Z")["cutoff_date"] == "2026-09-07"
    assert build(values, bridge_clients=None)["missing_work"] == ["bridge_clients_unavailable"]
    # Wiring the one production provider moves the ceiling and serves the default read.
    monkeypatch.setattr(
        admission, "PRODUCTION_BRIDGE_CLIENTS", lambda credentials: clients(values["world"])
    )
    assert admission.source_window_hint("2026-09-23T01:00:00Z")["cutoff_date"] == "2026-09-22"
    assert build(values, bridge_clients=None)["status"] == "admitted"


def test_the_planner_defaults_to_the_bridge_week_and_this_week_stays_refused(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.general_question_plan import (
        build_question_planning_context,
    )

    values = ask(monkeypatch, tmp_path, window={**WINDOW, "end": "2026-09-23", "closed": False})
    context = values["store"].read_request(
        values["invocation"]["request_id"], scope=native.f.scope()
    )
    from src.analysis.open_intelligence.general_question_policy import build_intake_context

    intake = build_intake_context(context["request"], selected_market=None, source_window=True)
    planning = build_question_planning_context(context["request"], intake)
    assert planning["source_window_ceiling"] == "2026-09-22"
    assert planning["default_observation_window"] == {
        "start": "2026-09-09",
        "end": "2026-09-22",
        "closed": True,
    }
    # A window running past that ceiling ("this week") is refused as uncovered.
    assert build(values)["missing_work"] == ["bridge_source_uncovered"]


def _padded_result(source, _stored):
    payload = json.loads(source["result_json"])
    payload["query_count"] += 1
    source["result_json"] = canonical_bytes(payload).decode()


def _out_of_scope_entry(source, _stored):
    (entry,) = [
        entry
        for entry in source["history_completion_set"]["entries"]
        if (entry["lane"], entry["market"], entry["product_date"])
        == ("trend_scores", "ke", "2026-09-22")
    ]
    entry["reason_code"] = "native_completion_unrecorded"


@pytest.mark.parametrize("change", [_padded_result, _out_of_scope_entry])
def test_a_stored_bridge_source_must_hash_to_what_the_registry_pins(admitted, change):
    # Neither change touches a value the reads or the admission are rebuilt from, so only
    # the digest bindings to the registry pin can refuse them.
    values, stored = admitted
    query_records = records(values, 3)
    with pytest.raises(ValueError, match=r"^snapshot_invalid$"):
        restore(values, _forge(stored, change), query_records)


def bound_reads(values, stored):
    from src.analysis.open_intelligence.general_question_context_admission import (
        restore_bridge_source,
    )
    from src.analysis.open_intelligence.general_question_context_queries import (
        bridge_read_lanes,
        build_bridge_context_query,
    )

    context = values["store"].read_request(
        values["invocation"]["request_id"], scope=native.f.scope()
    )
    source = restore_bridge_source(
        stored["provenance"]["bridge_source"], request=context["request"], plan=values["plan"]
    )
    lanes = bridge_read_lanes(source, values["plan"])
    limits = stored["provenance"]["limits"]
    queries = {
        lane: build_bridge_context_query(
            context["request"],
            values["plan"],
            context["intake"],
            source,
            lane=lane,
            row_limit=snapshot._bridge_row_limit(
                lane, limits["candidate_limit"], limits["effective_evidence_limit"]
            ),
        )
        for lane in lanes
    }
    by_ordinal = records(values, len(lanes))
    return {
        "request": context["request"],
        "plan": values["plan"],
        "intake": context["intake"],
        "source": source,
        "queries": queries,
        "captures": copy.deepcopy(stored["provenance"]["captures"]),
        "query_records": {
            lane: {part: dict(by_ordinal[ordinal][part]) for part in ("reservation", "receipt")}
            for ordinal, lane in enumerate(lanes, 1)
        },
        "evidence_limit": limits["effective_evidence_limit"],
    }


def admit(reads):
    from src.analysis.open_intelligence.general_question_context_admission import (
        admit_bridge_context,
    )

    return admit_bridge_context(
        reads["request"],
        reads["plan"],
        reads["intake"],
        source=reads["source"],
        queries=reads["queries"],
        captures=reads["captures"],
        query_records=reads["query_records"],
        evidence_limit=reads["evidence_limit"],
    )


def _rows_digest(reads):
    reads["captures"]["trend_analysis"]["rows"].pop()


def _receipt_split(reads):
    reads["captures"]["trend_analysis"]["receipt"] = {
        **reads["captures"]["trend_analysis"]["receipt"],
        "job_id": "another_job",
    }


def _limit(reads):
    reads["query_records"]["trend_analysis"]["reservation"]["candidate_limit"] = 7


def _template(reads):
    import dataclasses

    reads["queries"]["trend_analysis"] = dataclasses.replace(
        reads["queries"]["trend_analysis"], template_id="protected_context_raw_v1"
    )


def _other_binding(reads):
    import dataclasses

    reads["source"] = dataclasses.replace(
        reads["source"], binding={**reads["source"].binding, "request_as_of": "x"}
    )


def _missing_lane(reads):
    reads["captures"].pop("raw_content")


def _not_loaded(reads):
    import dataclasses

    reads["source"] = dataclasses.asdict(reads["source"])


def _unfinished(reads):
    for part in ("receipt",):
        reads["query_records"]["trend_analysis"][part] = {
            **reads["query_records"]["trend_analysis"][part],
            "query_state": "running",
        }
    reads["captures"]["trend_analysis"]["receipt"] = reads["query_records"]["trend_analysis"][
        "receipt"
    ]


@pytest.mark.parametrize(
    "change",
    [
        _rows_digest,
        _receipt_split,
        _limit,
        _template,
        _other_binding,
        _missing_lane,
        _not_loaded,
        _unfinished,
    ],
)
def test_the_bridge_admission_takes_only_reads_bound_to_their_records(admitted, change):
    from src.analysis.open_intelligence.general_question_context_admission import (
        BRIDGE_ADMISSION_VERSION,
    )

    values, stored = admitted
    reads = bound_reads(values, stored)
    expected = admit(reads)
    assert expected["contract_version"] == BRIDGE_ADMISSION_VERSION
    assert expected == stored["provenance"]["admission"]
    change(reads)
    with pytest.raises(ValueError, match=r"^bridge_context_invalid$"):
        admit(reads)


def test_a_source_that_does_not_restore_to_what_was_loaded_is_refused(monkeypatch, tmp_path):
    import dataclasses

    from src.analysis.open_intelligence import general_question_context_admission as admission

    restore_source = admission.restore_bridge_source

    def fewer_cells(record, **kwargs):
        restored = restore_source(record, **kwargs)
        return dataclasses.replace(restored, cells=restored.cells[1:])

    monkeypatch.setattr(admission, "restore_bridge_source", fewer_cells)
    values = ask(monkeypatch, tmp_path)
    result = build(values)
    assert result["missing_work"] == ["bridge_source_invalid"]
    assert values["http"].calls == []


def test_rows_outside_the_window_are_excluded_by_name(monkeypatch, tmp_path):
    early = {
        **ENRICHED_ROWS[0],
        "id": "za-article-early",
        "published_at": "2026-09-10T08:00:00Z",
    }
    values = ask(
        monkeypatch,
        tmp_path,
        tables={
            "enriched_content": [copy.deepcopy(ENRICHED_ROWS[0]), early],
            "raw_content": [],
            "trend_analysis": copy.deepcopy(TREND_ROWS),
        },
    )
    result = build(values)
    assert result["status"] == "admitted", result
    stored = result["snapshot"]
    assert "za-article-early" not in {receipt["source_row_id"] for receipt in stored["receipts"]}
    assert any(line.startswith("Excluded 1 collected records") for line in stored["limitations"])
    assert restore(values, stored, records(values, 3)) == stored


def _row(identifier, **changes):
    return {**copy.deepcopy(ENRICHED_ROWS[0]), "id": identifier, **changes}


def test_collected_rows_of_runs_the_capture_did_not_bind_are_excluded_by_name(
    monkeypatch, tmp_path
):
    # A snapshot clones the whole live table, so rows other runs wrote sit inside it: a
    # replay copy collected inside the window, a row of a run no receipt binds, and a row
    # that names no run. Only the bound funded execution's own rows are admitted.
    forged = [
        _row("za-replay-copy", pipeline_run_id="replay_20260821_copy"),
        _row("za-unbound-run", pipeline_run_id="0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"),
        _row("za-no-run", pipeline_run_id=None),
        {key: value for key, value in _row("za-absent-run").items() if key != "pipeline_run_id"},
    ]
    values = ask(
        monkeypatch,
        tmp_path,
        tables={
            "enriched_content": [copy.deepcopy(ENRICHED_ROWS[0]), *forged[:2]],
            "raw_content": copy.deepcopy(forged[2:]),
            "trend_analysis": copy.deepcopy(TREND_ROWS),
        },
    )
    result = build(values)
    assert result["status"] == "admitted", result
    stored = result["snapshot"]
    admitted = {receipt["source_row_id"] for receipt in stored["receipts"]}
    assert "za-article-1" in admitted
    assert not admitted & {row["id"] for row in forged}
    assert (
        "Excluded 4 collected records that no receipted collection run of this capture wrote."
        in stored["limitations"]
    )
    assert restore(values, stored, records(values, 3)) == stored


def test_a_capture_of_two_funded_runs_admits_both_runs_rows_and_no_third(monkeypatch, tmp_path):
    route = bridge()
    first, second = route.FUNDED_RUN, route.SECOND_RUN
    third = "9a4b6c8d-1e3f-4a5b-8c7d-2e4f6a8b0c1d"
    values = ask(
        monkeypatch,
        tmp_path,
        world_changes={"second_run": second},
        tables={
            "enriched_content": [
                _row("za-first-run", pipeline_run_id=first),
                _row("za-third-run", pipeline_run_id=third),
            ],
            "raw_content": [_row("za-second-run", pipeline_run_id=second)],
            "trend_analysis": copy.deepcopy(TREND_ROWS),
        },
    )
    result = build(values)
    assert result["status"] == "admitted", result
    stored = result["snapshot"]
    admitted = {receipt["source_row_id"] for receipt in stored["receipts"]}
    assert {"za-first-run", "za-second-run"} <= admitted
    assert "za-third-run" not in admitted
    assert (
        "Excluded 1 collected records that no receipted collection run of this capture wrote."
        in stored["limitations"]
    )
    # Every collection read the Ask issued binds both runs, and only them.
    reads = [
        parameters
        for sql, parameters in zip(values["http"].sql, values["http"].parameters, strict=True)
        if "pipeline_run_id IN UNNEST(@run_ids)" in sql
    ]
    assert len(reads) == 2
    for parameters in reads:
        (runs,) = [item for item in parameters if item["name"] == "run_ids"]
        assert [item["value"] for item in runs["parameterValue"]["arrayValues"]] == sorted(
            (first, second)
        )
    assert restore(values, stored, records(values, len(stored["receipts"]))) == stored


def test_a_capture_whose_only_rows_are_unbound_admits_no_collected_evidence(monkeypatch, tmp_path):
    values = ask(
        monkeypatch,
        tmp_path,
        tables={
            "enriched_content": [_row("za-replay-copy", pipeline_run_id="replay_run")],
            "raw_content": [],
            "trend_analysis": copy.deepcopy(TREND_ROWS),
        },
    )
    result = build(values)
    assert result["status"] == "admitted", result
    stored = result["snapshot"]
    assert "za-replay-copy" not in {receipt["source_row_id"] for receipt in stored["receipts"]}
    assert any(
        line.startswith("Excluded 1 collected records that no") for line in stored["limitations"]
    )


def test_the_daily_chain_route_admits_collected_rows_without_a_run_filter(monkeypatch, tmp_path):
    values = ask(
        monkeypatch,
        tmp_path,
        chain=True,
        tables={
            "enriched_content": [_row("za-chain-row", pipeline_run_id=None)],
            "raw_content": [],
            "trend_analysis": copy.deepcopy(TREND_ROWS),
        },
    )
    result = build(values)
    assert result["status"] == "admitted", result
    assert "za-chain-row" in {
        receipt["source_row_id"] for receipt in result["snapshot"]["receipts"]
    }


def test_a_read_over_its_cap_is_named_and_one_further_over_is_refused(monkeypatch, tmp_path):
    second = {**ENRICHED_ROWS[0], "id": "za-article-2", "collected_at": "2026-09-22T10:00:00Z"}
    values = ask(
        monkeypatch,
        tmp_path,
        tables={
            "enriched_content": [copy.deepcopy(ENRICHED_ROWS[0]), second],
            "raw_content": [],
            "trend_analysis": copy.deepcopy(TREND_ROWS),
        },
    )
    result = build(values, evidence_limit=1)
    assert result["status"] == "admitted", result
    stored = result["snapshot"]
    assert any(
        line.startswith("Bridge enriched_content read was capped at the newest 1")
        for line in stored["limitations"]
    )
    assert len(stored["receipts"]) == 1
    assert restore(values, stored, records(values, 3)) == stored

    third = {**second, "id": "za-article-3"}
    values = ask(
        monkeypatch,
        tmp_path / "over",
        tables={
            "enriched_content": [copy.deepcopy(ENRICHED_ROWS[0]), second, third],
            "raw_content": [],
            "trend_analysis": copy.deepcopy(TREND_ROWS),
        },
    )
    # The transport asked for at most the cap plus one row, so a longer result is refused.
    result = build(values, evidence_limit=1)
    assert result["status"] == "refused"
    assert result["reason"] == "query_result_incomplete"
    assert result["snapshot"] is None


def test_a_registry_holding_only_a_bridge_row_still_reads_the_bridge(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import protected_context_registry as registry

    values = ask(monkeypatch, tmp_path, only=True)
    assert list(registry._PROTECTED_CONTEXT_PROFILES) == []
    result = build(values)
    assert result["status"] == "admitted", result
    assert result["snapshot"]["provenance"]["profile"] == "protected_bridge_context_v1"


def test_stored_caps_must_be_the_ones_the_reads_were_reserved_under(admitted):
    values, stored = admitted
    query_records = records(values, 3)
    forged = copy.deepcopy(stored)
    forged["provenance"]["limits"]["query_caps"]["candidate"] -= 1
    with pytest.raises(ValueError, match=r"^snapshot_invalid$"):
        restore(values, resign(forged), query_records)


def test_a_daily_chain_bridge_clone_answers_and_reads_back(monkeypatch, tmp_path):
    values = ask(monkeypatch, tmp_path, chain=True)
    result = build(values)
    assert result["status"] == "admitted", result
    stored = result["snapshot"]
    source = stored["provenance"]["bridge_source"]
    assert source["authority"] == "daily_chain"
    assert source["binding"]["result_id"] == values["world"]["capture"].result["result_id"]
    query_records = records(values, 3)
    assert restore(values, stored, query_records) == stored
    for change in (_flip_to_ledger, _chain_payload):
        with pytest.raises(ValueError, match=r"^snapshot_invalid$"):
            restore(values, _forge(stored, change), query_records)


def _flip_to_ledger(source, _stored):
    source["authority"] = "execution_ledger"


def _chain_payload(source, _stored):
    envelope = json.loads(source["result_json"])
    envelope["operation_payload"]["query_count"] += 1
    source["result_json"] = canonical_bytes(envelope).decode()


def _bridge_queries(values, stored, source):
    from src.analysis.open_intelligence.general_question_context_queries import (
        bridge_read_lanes,
        build_bridge_context_query,
    )

    context = values["store"].read_request(
        values["invocation"]["request_id"], scope=native.f.scope()
    )
    limits = stored["provenance"]["limits"]
    return {
        lane: build_bridge_context_query(
            context["request"],
            values["plan"],
            context["intake"],
            source,
            lane=lane,
            row_limit=snapshot._bridge_row_limit(
                lane, limits["candidate_limit"], limits["effective_evidence_limit"]
            ),
        )
        for lane in bridge_read_lanes(source, values["plan"])
    }


def test_a_history_row_for_an_admitted_cell_before_the_window_refuses(admitted):
    import dataclasses

    values, stored = admitted
    reads = bound_reads(values, stored)
    source = reads["source"]
    (cell,) = [
        cell
        for cell in source.cells
        if cell["lane"] == "trend_analysis" and cell["state"] == "completed"
    ]
    early = {**cell, "product_date": "2026-09-10", "row_count": 1}
    reads["source"] = dataclasses.replace(source, cells=(*source.cells, early))
    # A read for 16 to 22 Sept never selects the 10 Sept cell, so the reads are unchanged
    # and only the row admission can see the row lies outside the window.
    assert _bridge_queries(values, stored, reads["source"]) == reads["queries"]
    rows = reads["captures"]["trend_analysis"]["rows"]
    rows.append(
        {
            "row_json": json.dumps(
                {"market": "za", "trend_date": "2026-09-10", "term": "load shedding"}
            )
        }
    )
    receipt = {
        **reads["captures"]["trend_analysis"]["receipt"],
        "result_digest": canonical_digest(rows),
    }
    reads["captures"]["trend_analysis"]["receipt"] = receipt
    reads["query_records"]["trend_analysis"]["receipt"] = receipt
    with pytest.raises(ValueError, match=r"^bridge_context_invalid$"):
        admit(reads)


def rebuilt_forgery(values, stored, change):
    """Forge the stored bridge source, then rebuild the reads, the admission and the snapshot
    from it the way readback does, so no later comparison differs and only the source guard
    that ``change`` defeats can refuse it."""
    import dataclasses

    from src.analysis.open_intelligence.general_question_context_admission import (
        restore_bridge_source,
    )

    context = values["store"].read_request(
        values["invocation"]["request_id"], scope=native.f.scope()
    )
    forged = copy.deepcopy(stored)
    record = forged["provenance"]["bridge_source"]
    change(record)
    restored = restore_bridge_source(
        stored["provenance"]["bridge_source"], request=context["request"], plan=values["plan"]
    )
    reads = bound_reads(values, stored)
    reads["source"] = dataclasses.replace(
        restored,
        registry_entry=copy.deepcopy(record["registry_entry"]),
        binding=copy.deepcopy(record["binding"]),
    )
    reads["queries"] = _bridge_queries(values, stored, reads["source"])
    forged["provenance"]["admission"] = admit(reads)
    projected = snapshot._project_context_snapshot(
        {
            "request": context["request"],
            "intake": context["intake"],
            "admission": {
                "policy_digest": stored["policy_digest"],
                "deployment_digest": stored["deployment_digest"],
            },
        },
        values["plan"],
        forged["provenance"]["admission"],
        forged["provenance"],
    )
    return projected, records(values, len(reads["queries"]))


def test_a_rebuilt_forgery_of_an_unchanged_source_is_the_stored_snapshot(admitted):
    values, stored = admitted
    same, query_records = rebuilt_forgery(values, stored, lambda record: None)
    assert same == stored
    assert restore(values, same, query_records) == stored


def _rebuilt_registry_row(record):
    record["registry_entry"]["source_as_of"] = "2026-09-01T00:00:00Z"
    record["binding"]["registry_entry_digest"] = canonical_digest(record["registry_entry"])


def _rebuilt_result_id(record):
    record["binding"]["result_id"] = "exr_" + "1" * 64


def _rebuilt_snapshot_digest(record):
    record["binding"]["snapshot_digest"] = "3" * 64


def _rebuilt_purpose(record):
    record["binding"]["purpose"] = "backfill"


def _rebuilt_available_after_as_of(record):
    record["binding"]["available_at"] = "2026-09-23T02:00:00.000000Z"


def _rebuilt_available_before_capture(record):
    record["binding"]["available_at"] = "2026-09-20T00:00:00.000000Z"


@pytest.mark.parametrize(
    "change",
    [
        # Only the registry row equality refuses a row the committed registry does not hold.
        _rebuilt_registry_row,
        # Only the binding field check refuses a binding the pinned result does not prove.
        _rebuilt_result_id,
        _rebuilt_snapshot_digest,
        _rebuilt_purpose,
        # Only the availability bounds refuse a capture available after the question or
        # before it was captured.
        _rebuilt_available_after_as_of,
        _rebuilt_available_before_capture,
    ],
)
def test_a_forged_source_with_a_rebuilt_admission_fails_readback(admitted, change):
    values, stored = admitted
    forged, query_records = rebuilt_forgery(values, stored, change)
    assert forged != stored
    with pytest.raises(ValueError, match=r"^snapshot_invalid$"):
        restore(values, forged, query_records)


def test_a_bridge_snapshot_with_an_extra_provenance_key_fails_readback(admitted):
    # The projection carries the provenance through as stored, so only the key set refuses.
    values, stored = admitted
    query_records = records(values, 3)
    forged = copy.deepcopy(stored)
    forged["provenance"]["source_binding"] = {"profile_id": PROFILE}
    with pytest.raises(ValueError, match=r"^snapshot_invalid$"):
        restore(values, resign(forged), query_records)


@pytest.mark.parametrize(
    "case",
    [
        test_a_window_past_the_bridge_ceiling_is_an_uncovered_gap,
        test_without_a_bridge_row_the_window_stays_uncovered,
    ],
    ids=lambda case: case.__name__,
)
def test_bridge_asks_hold_beside_a_committed_bridge_row(monkeypatch, tmp_path, case):
    from tests.unit.test_protected_context_registry import rehearse_pin

    rehearse_pin(monkeypatch, tmp_path)
    case(monkeypatch, tmp_path)


def test_an_admitted_bridge_clone_answers_beside_a_committed_bridge_row(monkeypatch, tmp_path):
    from tests.unit.test_protected_context_registry import rehearse_pin

    rehearse_pin(monkeypatch, tmp_path)
    values = ask(monkeypatch, tmp_path)
    result = build(values)
    assert result["status"] == "admitted", result
    test_an_admitted_bridge_clone_answers_a_window_ending_on_its_cutoff(
        (values, result["snapshot"])
    )


def trial_app(monkeypatch):
    """This module moved whole to the trial pin cutoff, its capture the trial capture.

    The trial capture is the synthetic ledger world rendered from its receipt at the cutoff
    the next pin commit adds. The moved route registers it into the real committed
    document, so Ask reads it beside every committed row.
    """
    import sys
    import types
    from datetime import date
    from pathlib import Path

    from tests.unit.bridge_shift_fixture import shifted_source
    from tests.unit.test_protected_context_registry import trial_cutoff, trial_world

    total = (trial_cutoff() - date.fromisoformat("2026-09-20")).days
    route = trial_world(monkeypatch).test_bridge_ledger_route
    monkeypatch.setattr(route, "fixture_document", document)
    name = f"tests.unit._bridge_app_shift_{total}"
    if name not in sys.modules:
        source = shifted_source(Path(__file__).read_text(encoding="utf-8"), total - 2)
        source = source.replace("return shifted_world(2).", f"return shifted_world({total}).")
        module = types.ModuleType(name)
        module.__file__ = __file__
        sys.modules[name] = module
        exec(compile(source, __file__, "exec"), module.__dict__)
    app = sys.modules[name]
    assert app.bridge() is route
    return app


def test_ask_admits_a_trial_pin_row_read_beside_every_committed_row(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import protected_context_registry as r

    from tests.unit.test_protected_context_registry import trial_cutoff

    app = trial_app(monkeypatch)
    values = app.ask(monkeypatch, tmp_path)
    assert r.bridge_entries()[0].cutoff_date == trial_cutoff().isoformat()
    assert len(r.load_protected_context_registry()) == len(document()["entries"]) + 1
    result = app.build(values)
    assert result["status"] == "admitted", result
    source = result["snapshot"]["provenance"]["bridge_source"]
    assert source["registry_entry"]["cutoff_date"] == trial_cutoff().isoformat()
    app.test_an_admitted_bridge_clone_answers_a_window_ending_on_its_cutoff(
        (values, result["snapshot"])
    )


def test_a_window_past_a_trial_pin_row_is_an_uncovered_gap(monkeypatch, tmp_path):
    app = trial_app(monkeypatch)
    app.test_a_window_past_the_bridge_ceiling_is_an_uncovered_gap(monkeypatch, tmp_path)
