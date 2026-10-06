"""The offline route A producer of the bridge v3 capture inputs and manifest.

All operator files here are synthetic and unissued; the generation is the packaged bridge
candidate made active only inside the test, and the temporal rules are a fixture list.
"""

import hashlib
import json
import socket
from datetime import timedelta

import pytest
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.source_estate_bridge import HISTORY_TABLES, MARKETS

from tests.unit import bridge_route_fixture as fx


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network prohibited"))


def producer():
    from scripts.staging import produce_bridge_capture_inputs

    return produce_bridge_capture_inputs


def refused(
    tmp_path, monkeypatch, code, *, activate=True, rules=True, cutoff=fx.CUTOFF, **overrides
):
    if activate:
        fx.activate_bridge(monkeypatch)
    else:
        fx.activate_amendment_e(monkeypatch)
    if rules:
        fx.fixture_rules(monkeypatch)
    paths = fx.operator_files(tmp_path / "operator", **overrides)
    output = tmp_path / "produced"
    with pytest.raises(ValueError, match=rf"^{code}$"):
        producer().produce(
            fx.argv(paths, output, cutoff=cutoff),
            clock=lambda: fx.PRODUCED_AT,
            generation_loader=paths["catalogue"],
        )
    assert not output.exists()


def test_without_a_reviewed_temporal_rule_list_production_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(producer(), "REVIEWED_TEMPORAL_RULES", None)
    refused(tmp_path, monkeypatch, "bridge_temporal_rules_unreviewed", rules=False)


def test_the_cli_refusal_names_the_unreviewed_rules_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    fx.activate_bridge(monkeypatch)
    monkeypatch.setattr(producer(), "REVIEWED_TEMPORAL_RULES", None)
    paths = fx.operator_files(tmp_path / "operator")
    output = tmp_path / "produced"
    assert (
        producer().main(
            fx.argv(paths, output),
            clock=lambda: fx.PRODUCED_AT,
            generation_loader=paths["catalogue"],
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err) == {"error": "bridge_temporal_rules_unreviewed"}
    assert not output.exists()


def test_the_producer_builds_the_nine_inputs_and_the_route_a_manifest(tmp_path, monkeypatch):
    receipt, output = fx.produced(tmp_path, monkeypatch)
    inputs, manifest_raw = fx.read_outputs(output)
    assert sorted(path.name for path in (output / "inputs").iterdir()) == sorted(
        f"{name}.json" for name in producer().ARTIFACT_NAMES
    )
    manifest = json.loads(manifest_raw)
    assert manifest["operation"] == "source_snapshot_capture"
    assert manifest["arguments"] == [
        "scripts/staging/capture_protected_production_snapshot.py",
        "--cutoff-date",
        "2026-09-22",
        "--mode",
        "initial",
        "--grant",
        fx.GRANT_ID,
    ]
    assert manifest["job_resource"] == (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
    )
    assert manifest["service_identity"] == (
        "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert (manifest["source_sha"], manifest["build_resource"]) == (
        fx.SOURCE_SHA,
        fx.BUILD_RESOURCE,
    )
    assert manifest["image_uri"].endswith("@" + fx.IMAGE_DIGEST)
    assert manifest["timeout_seconds"] == 600
    assert manifest["limits"]["max_bytes_billed"] == 1_000_000_000
    # Every digest is the digest of the bytes written, never a value read from input.
    assert {item["name"]: item["sha256"] for item in manifest["input_artifacts"]} == {
        name: hashlib.sha256(raw).hexdigest() for name, raw in inputs.items()
    }
    assert receipt["manifest_sha256"] == hashlib.sha256(manifest_raw).hexdigest()
    assert inputs["recovery_context"] == b"null"
    assert inputs["capture_contract"] == fx.capture_contract()
    storage = json.loads(inputs["storage_policy"])
    assert storage["grant"]["allowed_cutoffs"] == ["2026-09-22"]
    assert storage["grant"]["grant_id"] == fx.GRANT_ID
    plan = json.loads(inputs["capture_plan"])
    assert plan["contract_version"] == "open_intelligence_protected_capture_plan_v3"
    assert plan["cutoff_date"] == "2026-09-22"
    profile = plan["snapshot_plan"]
    assert profile["profile_id"] == "staging_bridge_v3_20260922"
    assert profile["observation_window_end"] == "2026-09-23T00:00:00.000000Z"
    assert profile["collection_snapshot_as_of"] == fx.stamp(fx.COLLECTION_AS_OF)
    assert profile["source_estate_digest"] == fx.grant()["source_estate_digest"]
    assert [row["destination_table"] for row in profile["relation_bindings"]] == [
        f"ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260922_{row['lane']}"
        for row in profile["relation_bindings"]
    ]
    assert len(plan["creation_statements"]) == 7
    # The approval preparation over the written bytes regenerates the plan exactly.
    rule = json.loads(
        (
            fx.ENGINE_ROOT
            / "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json"
        ).read_bytes()
    )["operation_validation"]["source_snapshot_capture"]["arguments"]
    prepared = execution_approval.prepare_source_snapshot_capture_v3(
        {item["name"]: item["sha256"] for item in manifest["input_artifacts"]},
        artifact_reader=inputs.__getitem__,
        rule=rule,
        now=fx.PRODUCED_AT,
    )
    assert canonical_bytes(prepared) == inputs["capture_plan"]


def test_the_collection_receipt_is_the_funded_close_of_the_funded_execution(tmp_path, monkeypatch):
    chain = fx.funded_chain()
    _, output = fx.produced(tmp_path, monkeypatch, chain=chain)
    inputs, _ = fx.read_outputs(output)
    receipts = json.loads(inputs["collection_receipt_set"])["receipts"]
    # The run id is the execution's id, the digest the funded close's own result digest,
    # the collection spans the consumption to the recorded close.
    assert receipts == [fx.funded_receipt(chain)]
    assert (
        receipts[0]["collection_receipt_digest"]
        == hashlib.sha256(chain["result"].canonical_result_json.encode()).hexdigest()
    )


def test_a_produced_proposal_binds_at_capture_time(tmp_path, monkeypatch):
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        resolve_capture_bridge_evidence,
    )

    chain = fx.funded_chain()
    facts = dict(fx.history_facts(), entries=[])
    _, output = fx.produced(tmp_path, monkeypatch, chain=chain, history_completions=facts)
    inputs, _ = fx.read_outputs(output)
    readers = fx.funded_readers(chain)
    bound = resolve_capture_bridge_evidence(
        {name: inputs[name] for name in execution_approval._BRIDGE_ARTIFACT_PINS},
        profile=json.loads(inputs["capture_plan"])["snapshot_plan"],
        read_chain=readers.read_chain,
        read_funded_execution=readers.read_funded_execution,
        generation_loader=readers.generation_loader,
    )
    assert [item["execution_id"] for item in bound] == [fx.FUNDED_EXECUTION_ID]
    assert [item["pipeline_run_id"] for item in bound] == [fx.FUNDED_RUN]


# The cutoff is an explicit argument, bounded by what the funded run proves.


@pytest.mark.parametrize("cutoff", ["", "2026-9-22", "2026-02-30", "20260922", " 2026-09-22"])
def test_the_cutoff_date_must_be_a_calendar_date(tmp_path, monkeypatch, capsys, cutoff):
    fx.activate_bridge(monkeypatch)
    fx.fixture_rules(monkeypatch)
    paths = fx.operator_files(tmp_path / "operator")
    output = tmp_path / "produced"
    assert producer().main(fx.argv(paths, output, cutoff=cutoff)) == 2
    assert json.loads(capsys.readouterr().err) == {"error": "bridge_capture_inputs_cli_invalid"}
    assert not output.exists()


def test_the_cutoff_date_has_no_default(tmp_path, monkeypatch, capsys):
    paths = fx.operator_files(tmp_path / "operator")
    values = fx.argv(paths, tmp_path / "produced")
    assert values[0] == producer()._CLI_FLAGS[0]
    assert producer()._CLI_FLAGS[0][2:] == "cutoff-date"
    assert producer().main(values[2:]) == 2
    assert json.loads(capsys.readouterr().err) == {"error": "bridge_capture_inputs_cli_invalid"}
    assert "CUTOFF" not in vars(producer())


@pytest.mark.parametrize(
    "cutoff",
    ["2026-09-23", "2026-09-21", "2026-09-01"],
    ids=["window_still_open", "day_before", "earlier_day"],
)
def test_a_cutoff_the_funded_run_does_not_close_refuses(tmp_path, monkeypatch, cutoff):
    # The funded execution started on 2026-09-23, after the window of 2026-09-22 closed:
    # that is the one day it proves. The grant allows each cutoff, so only the run decides.
    refused(
        tmp_path,
        monkeypatch,
        "bridge_capture_cutoff_unproven",
        cutoff=cutoff,
        grant=fx.grant(allowed_cutoffs=sorted({cutoff, fx.CUTOFF})),
        history_completions=dict(fx.history_facts(), first_product_date="2026-09-01", entries=[]),
    )


# The funded execution closes the cutoff when it started in [cutoff+1 00:00Z, cutoff+2 00:00Z).
# Each edge to the microsecond: the fixture start is moved, the chain and collection are not.
# The upper edge of cutoff 2026-09-21 is the lower edge of the fixture's 2026-09-22.
_TICK = timedelta(microseconds=1)
_EDGES = [
    ("2026-09-22", -_TICK, False),
    ("2026-09-22", timedelta(0), True),
    ("2026-09-22", _TICK, True),
    ("2026-09-21", -_TICK, True),
    ("2026-09-21", timedelta(0), False),
    ("2026-09-21", _TICK, False),
]
_EDGE_IDS = [
    "lower_edge_before",
    "lower_edge_at",
    "lower_edge_after",
    "upper_edge_before",
    "upper_edge_at",
    "upper_edge_after",
]


@pytest.mark.parametrize(("cutoff", "offset", "closes"), _EDGES, ids=_EDGE_IDS)
def test_the_funded_start_closes_the_cutoff_only_inside_its_day(cutoff, offset, closes):
    chain = fx.funded_chain()
    execution = fx.funded_execution(chain, started=fx.WINDOW_END + offset)
    assert execution["startTime"] == fx.stamp(fx.WINDOW_END + offset)

    def receipt_set():
        return producer()._funded_receipt_set(
            chain["rows"], execution, cutoff=cutoff, generation_loader=chain["catalogue"]
        )

    if closes:
        receipts, _ = receipt_set()
        assert receipts["receipts"][0]["run_id"] == fx.FUNDED_EXECUTION_ID
    else:
        with pytest.raises(ValueError, match=r"^bridge_capture_cutoff_unproven$"):
            receipt_set()


def test_a_run_started_at_the_window_end_produces_the_proposal(tmp_path, monkeypatch):
    chain = fx.funded_chain()
    receipt, _ = fx.produced(
        tmp_path,
        monkeypatch,
        chain=chain,
        funded_execution=fx.funded_execution(chain, started=fx.WINDOW_END),
    )
    assert receipt["arguments"][2] == fx.CUTOFF


@pytest.mark.parametrize(
    ("cutoff", "offset"),
    [("2026-09-22", -_TICK), ("2026-09-21", timedelta(0))],
    ids=["lower_edge_before", "upper_edge_at"],
)
def test_a_run_started_outside_the_cutoff_day_produces_nothing(
    tmp_path, monkeypatch, cutoff, offset
):
    chain = fx.funded_chain()
    refused(
        tmp_path,
        monkeypatch,
        "bridge_capture_cutoff_unproven",
        cutoff=cutoff,
        chain=chain,
        funded_execution=fx.funded_execution(chain, started=fx.WINDOW_END + offset),
        grant=fx.grant(allowed_cutoffs=sorted({cutoff, fx.CUTOFF})),
        history_completions=dict(fx.history_facts(), first_product_date="2026-09-01", entries=[]),
    )


def test_the_cutoff_names_the_profile_and_the_vector(tmp_path, monkeypatch):
    receipt, output = fx.produced(tmp_path, monkeypatch)
    plan = json.loads(fx.read_outputs(output)[0]["capture_plan"])
    assert receipt["arguments"][1:3] == [producer()._CLI_FLAGS[0], fx.CUTOFF]
    assert plan["cutoff_date"] == fx.CUTOFF
    assert plan["snapshot_plan"]["profile_id"] == "staging_bridge_v3_20260922"


def test_every_history_cell_is_explicit_and_uncovered_days_are_unrecorded(tmp_path, monkeypatch):
    receipt, output = fx.produced(tmp_path, monkeypatch)
    inputs, _ = fx.read_outputs(output)
    entries = json.loads(inputs["history_completion_set"])["entries"]
    days = ["2026-09-20", "2026-09-21", "2026-09-22"]
    assert [(row["lane"], row["market"], row["product_date"]) for row in entries] == [
        (lane, market, day) for lane in HISTORY_TABLES for market in MARKETS for day in days
    ]
    supplied = {
        (row["lane"], row["market"], row["product_date"]): row
        for row in fx.history_facts()["entries"]
    }
    for row in entries:
        key = (row["lane"], row["market"], row["product_date"])
        if key in supplied:
            assert row == supplied[key]
        else:
            assert row["state"] == "unavailable"
            assert row["reason_code"] == "native_completion_unrecorded"
            assert all(
                row[name] is None
                for name in (
                    "result_ref",
                    "product_receipt_digest",
                    "output_digest",
                    "row_count",
                    "completed_at",
                    "available_at",
                )
            )
    assert receipt["unavailable_history_entries"] == len(entries) - len(supplied) == 41


def test_the_producer_writes_only_new_files(tmp_path, monkeypatch):
    fx.activate_bridge(monkeypatch)
    fx.fixture_rules(monkeypatch)
    paths = fx.operator_files(tmp_path / "operator")
    output = tmp_path / "produced"
    output.mkdir()
    (output / "keep.txt").write_bytes(b"operator notes")
    with pytest.raises(ValueError, match=r"^bridge_capture_inputs_output_exists$"):
        producer().produce(
            fx.argv(paths, output),
            clock=lambda: fx.PRODUCED_AT,
            generation_loader=paths["catalogue"],
        )
    assert [path.name for path in output.iterdir()] == ["keep.txt"]
    assert (output / "keep.txt").read_bytes() == b"operator notes"


def test_the_active_pair_must_be_the_bridge_generation(tmp_path, monkeypatch):
    refused(tmp_path, monkeypatch, "bridge_capture_generation_inactive", activate=False)


def test_the_capture_contract_must_be_the_one_the_grant_names(tmp_path, monkeypatch):
    refused(
        tmp_path,
        monkeypatch,
        "bridge_capture_contract_invalid",
        capture_contract=fx.capture_contract() + b"\n",
    )


def _funded(state="succeeded", **changes):
    def build():
        chain = fx.funded_chain()
        return {"chain": chain, "funded_execution": fx.funded_execution(chain, state, **changes)}

    return build


def _renamed():
    chain = fx.funded_chain()
    execution = fx.funded_execution(chain)
    execution["name"] = execution["name"][:-5] + "zzzzz"
    return {"chain": chain, "funded_execution": execution}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        (_funded("failed"), "bridge_funded_execution_not_succeeded"),
        (_funded("running"), "bridge_funded_execution_running"),
        (_renamed, "bridge_funded_execution_differs"),
        (
            _funded(completed=fx.stamp(fx.PRODUCED_AT + timedelta(minutes=1))),
            "bridge_capture_collection_not_closed",
        ),
        (lambda: {"funded_execution": {"name": "x"}}, "bridge_funded_execution_invalid"),
        (lambda: {"funded_chain": [{"approval_count": 1}]}, "bridge_funded_chain_invalid"),
        (
            lambda: {"chain": fx.funded_chain(status="failed")},
            "bridge_funded_result_not_succeeded",
        ),
        (
            lambda: {"chain": fx.funded_chain(execution_id="bridge-capture")},
            "bridge_funded_execution_differs",
        ),
    ],
    ids=[
        "failed",
        "running",
        "other_execution",
        "not_closed",
        "execution_shape",
        "chain_shape",
        "close_failed",
        "chain_of_another_execution",
    ],
)
def test_the_collection_must_be_the_succeeded_funded_execution(
    tmp_path, monkeypatch, overrides, code
):
    refused(tmp_path, monkeypatch, code, **overrides())


def _facts(change):
    value = fx.history_facts()
    change(value)
    return value


@pytest.mark.parametrize(
    "facts",
    [
        _facts(lambda v: v["entries"].append(dict(v["entries"][0]))),
        _facts(lambda v: v["entries"][0].update(product_date="2026-09-19")),
        _facts(lambda v: v["entries"][0].update(product_date="2026-09-23")),
        _facts(lambda v: v["entries"][0].update(state="unavailable")),
        _facts(lambda v: v.update(first_product_date="2026-09-23")),
        _facts(lambda v: v.update(contract_version="42_bridge_history_completion_set_v1")),
        # Facts outside the history grid refuse; they are never dropped.
        _facts(
            lambda v: v["entries"].append(
                fx.completion_fact("raw_content", "za", "2026-09-21", "completed")
            )
        ),
        _facts(
            lambda v: v["entries"].append(
                fx.completion_fact("trend_analysis", "us", "2026-09-21", "completed")
            )
        ),
    ],
    ids=[
        "duplicate",
        "before_window",
        "after_cutoff",
        "supplied_unavailable",
        "late",
        "version",
        "collection_lane",
        "other_market",
    ],
)
def test_completion_facts_outside_the_window_or_contract_refuse(tmp_path, monkeypatch, facts):
    refused(tmp_path, monkeypatch, "bridge_history_completions_invalid", history_completions=facts)


def test_a_completion_available_after_the_cutoff_refuses_in_the_profile(tmp_path, monkeypatch):
    facts = fx.history_facts()
    facts["entries"][0]["available_at"] = "2026-09-23T00:00:00.000001Z"
    refused(
        tmp_path, monkeypatch, "source_bridge_history_window_invalid", history_completions=facts
    )


def test_a_grant_that_does_not_allow_the_cutoff_refuses(tmp_path, monkeypatch):
    refused(
        tmp_path,
        monkeypatch,
        "bridge_capture_grant_invalid",
        grant=fx.grant(allowed_cutoffs=["2026-09-21"]),
    )


@pytest.mark.parametrize("flags", [[], ["--output-dir", "relative"]])
def test_the_command_line_is_exact(flags, capsys):
    assert producer().main(flags) == 2
    assert json.loads(capsys.readouterr().err) == {"error": "bridge_capture_inputs_cli_invalid"}


def test_the_produced_plan_passes_every_destination_check_of_the_routine(tmp_path, monkeypatch):
    from tests.unit.test_source_bridge_approval_preparation import (
        routine_destination_checks_pass,
    )

    _, output = fx.produced(tmp_path, monkeypatch)
    inputs, _ = fx.read_outputs(output)
    plan = json.loads(inputs["capture_plan"])
    rule = json.loads(
        (
            fx.ENGINE_ROOT
            / "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json"
        ).read_bytes()
    )["operation_validation"]["source_snapshot_capture"]
    assert routine_destination_checks_pass(plan, rule["datasets"])


def test_a_proposal_the_approval_preparation_refuses_is_never_written(
    tmp_path, monkeypatch, capsys
):
    fx.activate_bridge(monkeypatch)
    fx.fixture_rules(monkeypatch)

    def refuse(*args, **kwargs):
        raise execution_approval.ApprovalRefusal("source_snapshot_plan_invalid")

    monkeypatch.setattr(execution_approval, "prepare_source_snapshot_capture_v3", refuse)
    paths = fx.operator_files(tmp_path / "operator")
    output = tmp_path / "produced"
    assert (
        producer().main(
            fx.argv(paths, output),
            clock=lambda: fx.PRODUCED_AT,
            generation_loader=paths["catalogue"],
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err) == {"error": "source_snapshot_plan_invalid"}
    assert not output.exists()


# The reviewed rule list covers exactly the keys the funded Wave 1 SocialCrawl writer can put
# into raw_content, and each basis follows from how the writer fills published_at. The
# capture also carries other connectors' rows; they have no key, so no rule covers them.
EVENT_INSTANT_KEYS = {
    ("reddit", "reddit/post/comments", "reddit/comment"),
    ("short_video", "instagram/audio/reels", "instagram/post"),
    ("short_video", "instagram/search/reels", "instagram/post"),
    ("short_video", "tiktok/song/videos", "tiktok/post"),
    ("youtube", "youtube/shorts/trending", "youtube/post"),
}
# The writer accepts any ISO string as published_at (socialcrawl.py:1096), so nothing shows
# that a YouTube comment time is an exact instant rather than one the vendor derived from a
# relative age; that key stays a collection observation until a probe shows exact times.
APPROXIMATE_TIME_KEYS = {("youtube", "youtube/video/comments", "youtube/comment")}


def reviewed():
    return producer().REVIEWED_TEMPORAL_RULES


def rule_key(rule):
    return (rule["source_family"], rule["endpoint"], rule["content_type"])


def written_row(route, envelope, published_at="2026-09-21T10:00:00Z"):
    """One row as the funded Wave 1 writer shapes and adapts it for raw_content."""
    from types import SimpleNamespace

    from src.analysis.open_intelligence.candidates import adapt_wave1_evidence_rows
    from src.ingestion.connectors.socialcrawl import (
        WAVE1_ROUTE_SPECS,
        SocialCrawlConnector,
        Wave1RouteRequest,
    )

    node = {"id": f"{route}-{envelope}", "url": f"https://example.invalid/{route}/{envelope}"}
    if published_at is not None:
        node["published_at"] = published_at
    params = dict.fromkeys(WAVE1_ROUTE_SPECS[route].required_parameters, "seed")
    request = Wave1RouteRequest(route, params, "qualification", market="za")
    context = SimpleNamespace(execution_capability=SimpleNamespace(run_id="run_rules"))
    row = SocialCrawlConnector._wave1_row_from_item({envelope: node}, request, context)
    (adapted,) = adapt_wave1_evidence_rows([row])
    return adapted


def test_the_repository_carries_the_reviewed_temporal_rule_list():
    rules = reviewed()
    assert rules is not None
    assert rules["contract_version"] == "source_observation_semantics_v1"
    assert len(rules["rules"]) == 16


def test_the_reviewed_list_passes_the_consumer_validator():
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        parse_bridge_artifacts,
    )

    from tests.unit.test_source_estate_bridge_contract import bridge_artifacts

    artifacts = bridge_artifacts()
    artifacts["temporal_rules"] = reviewed()
    parsed = parse_bridge_artifacts(artifacts)
    assert parsed["temporal_rules"] == json.loads(canonical_bytes(reviewed()))


def test_the_reviewed_list_covers_exactly_the_keys_the_funded_wave1_writer_can_write():
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    written = {
        rule_key(written_row(route, envelope))
        for route in WAVE1_ROUTE_SPECS
        for envelope in ("post", "comment")
    }
    assert {rule_key(rule) for rule in reviewed()["rules"]} == written


def test_only_platform_timestamped_posts_and_comments_are_event_instants():
    for rule in reviewed()["rules"]:
        if rule_key(rule) in EVENT_INSTANT_KEYS:
            assert (rule["time_basis"], rule["event_field"]) == (
                "native_event_instant",
                "published_at",
            )
        else:
            assert (rule["time_basis"], rule["event_field"]) == ("collection_observation", None)
        assert rule["missing_time_action"] == rule["future_time_action"]
        assert rule["future_time_action"] == "withhold_event_time_claim"
    assert {rule_key(rule) for rule in reviewed()["rules"]} >= EVENT_INSTANT_KEYS
    assert {rule_key(rule) for rule in reviewed()["rules"]} >= APPROXIMATE_TIME_KEYS


@pytest.mark.parametrize(
    "route,envelope", [(key[1], key[2].split("/")[1]) for key in sorted(EVENT_INSTANT_KEYS)]
)
def test_an_event_instant_takes_the_items_own_timestamp_never_the_collection_time(route, envelope):
    assert written_row(route, envelope)["published_at"] == "2026-09-21T10:00:00+00:00"
    assert written_row(route, envelope, published_at=None)["published_at"] == ""


def test_the_producer_runs_under_the_reviewed_list(tmp_path, monkeypatch):
    fx.activate_bridge(monkeypatch)
    paths = fx.operator_files(tmp_path / "operator")
    output = tmp_path / "produced"
    producer().produce(
        fx.argv(paths, output), clock=lambda: fx.PRODUCED_AT, generation_loader=paths["catalogue"]
    )
    inputs, _ = fx.read_outputs(output)
    assert inputs["temporal_rules"] == canonical_bytes(reviewed())
    plan = json.loads(inputs["capture_plan"])
    assert plan["snapshot_plan"]["temporal_rules_digest"] == canonical_digest(reviewed())


def _mutate(rules, mutation):
    changed = json.loads(canonical_bytes(rules))
    first = changed["rules"][0]
    event = next(r for r in changed["rules"] if r["time_basis"] == "native_event_instant")
    observed = next(r for r in changed["rules"] if r["time_basis"] == "collection_observation")
    if mutation == "event_field_on_collection_observation":
        observed["event_field"] = "published_at"
    elif mutation == "event_instant_without_field":
        event["event_field"] = None
    elif mutation == "missing_time_counts":
        first["missing_time_action"] = "use_collection_time"
    elif mutation == "future_time_counts":
        event["future_time_action"] = "use_collection_time"
    elif mutation == "unknown_basis":
        first["time_basis"] = "native_publish_day"
    elif mutation == "duplicate":
        changed["rules"].append(dict(first))
    elif mutation == "unsorted":
        changed["rules"].reverse()
    elif mutation == "empty":
        changed["rules"] = []
    elif mutation == "extra_field":
        first["window_days"] = 7
    else:
        changed["contract_version"] = "source_observation_semantics_v0"
    return changed


MUTATIONS = [
    "event_field_on_collection_observation",
    "event_instant_without_field",
    "missing_time_counts",
    "future_time_counts",
    "unknown_basis",
    "duplicate",
    "unsorted",
    "empty",
    "extra_field",
    "version",
]


@pytest.mark.parametrize("mutation", MUTATIONS)
def test_a_mutated_list_fails_the_consumer_validator(mutation):
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        parse_bridge_artifacts,
    )

    from tests.unit.test_source_estate_bridge_contract import bridge_artifacts

    artifacts = bridge_artifacts()
    artifacts["temporal_rules"] = _mutate(reviewed(), mutation)
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        parse_bridge_artifacts(artifacts)


@pytest.mark.parametrize("mutation", ["missing_time_counts", "unsorted"])
def test_the_producer_refuses_a_mutated_list(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(producer(), "REVIEWED_TEMPORAL_RULES", _mutate(reviewed(), mutation))
    refused(tmp_path, monkeypatch, "bridge_capture_artifacts_invalid", rules=False)
