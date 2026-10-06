import copy
import hashlib
import json

import pytest
from scripts.staging import replay_open_intelligence as replay
from src.contracts.open_intelligence import ResolvedScope

from tests.unit import test_pipeline_production_snapshot as pipeline_fixture
from tests.unit import test_production_snapshot as snapshot_fixture
from tests.unit import test_production_snapshot_rows as rows_fixture
from tests.unit.test_provenance_replay_v3 import Provider
from tests.unit.test_provenance_replay_v3 import artifact as legacy_artifact


def scope():
    return ResolvedScope(
        "ogilvy_default",
        rows_fixture.MARKETS,
        "ogilvy",
        (),
        "open_intelligence",
        "synthetic_production_v3",
        "2.0.0",
    )


def production_artifact():
    prepared = pipeline_fixture.prepared()
    result = pipeline_fixture.run(prepared, pipeline_fixture.NativeClient(prepared))
    return replay.adapt_production_result_to_replay_input(
        result,
        scope=scope(),
        production_snapshot=prepared,
        semantic_provider=Provider(),
    ), result


def reseal_artifact(value):
    value = copy.deepcopy(value)
    value.pop("replay_digest", None)
    value["replay_digest"] = replay._digest(value)
    return value


def test_production_graph_roundtrips_through_canonical_json_with_exact_digests():
    artifact, result = production_artifact()
    encoded = replay.serialize_production_replay_input(artifact)
    reloaded = json.loads(encoded)
    rebuilt = replay.validate_production_replay_input(reloaded, semantic_provider=Provider())

    assert replay.serialize_production_replay_input(rebuilt) == encoded
    assert rebuilt["production_snapshot"]["snapshot"]["source_digest"] == (
        result.source_snapshot_digest
    )
    assert replay._digest(rebuilt["source_provenance_by_member"]) == replay._digest(
        result.source_provenance_by_member
    )
    assert replay._digest(rebuilt["components_by_candidate"]) == replay._digest(
        replay._v3_result_fields(result)["components_by_candidate"]
    )
    assert rebuilt["production_snapshot"]["source_authority"] is False
    assert rebuilt["human_review_state"] == "pending"


@pytest.mark.parametrize("version", ["open_intelligence_replay_v3", "unknown", None])
def test_unknown_or_legacy_artifact_version_cannot_enter_production_replay(version):
    artifact, _result = production_artifact()
    artifact = json.loads(replay.serialize_production_replay_input(artifact))
    artifact["artifact_version"] = version
    with pytest.raises(ValueError, match="source_provenance_version_invalid"):
        replay.validate_production_replay_input(
            reseal_artifact(artifact), semantic_provider=Provider()
        )


def test_changed_production_row_refuses_even_after_outer_digests_are_resealed():
    artifact, _result = production_artifact()
    artifact = json.loads(replay.serialize_production_replay_input(artifact))
    prepared = artifact["production_snapshot"]
    prepared["snapshot"]["rows_by_table"]["enriched_content"][0]["text"] = "changed"
    artifact["production_snapshot"] = snapshot_fixture.reseal(prepared)

    with pytest.raises(ValueError, match="production_snapshot_invalid"):
        replay.validate_production_replay_input(
            reseal_artifact(artifact), semantic_provider=Provider()
        )


def test_resealed_graph_output_mismatch_refuses():
    artifact, _result = production_artifact()
    artifact = json.loads(replay.serialize_production_replay_input(artifact))
    artifact["components_by_candidate"] = {}

    with pytest.raises(ValueError, match="source_provenance_conflict:components_by_candidate"):
        replay.validate_production_replay_input(
            reseal_artifact(artifact), semantic_provider=Provider()
        )


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_production_replay_field_set_is_exact(mutation):
    artifact, _result = production_artifact()
    artifact = json.loads(replay.serialize_production_replay_input(artifact))
    if mutation == "extra":
        artifact["unexpected"] = None
    else:
        artifact.pop("current_window")
    with pytest.raises(ValueError, match="production replay"):
        replay.validate_production_replay_input(
            reseal_artifact(artifact), semantic_provider=Provider()
        )


def test_legacy_v3_serializer_bytes_remain_unchanged():
    legacy = json.loads(replay.serialize_replay_input_v3(legacy_artifact()))
    legacy["provider_outputs"][0]["runtime_ms"] = 0.0
    encoded = replay.serialize_replay_input_v3(legacy)
    assert len(encoded) == 12340
    assert hashlib.sha256(encoded.encode("utf-8")).hexdigest() == (
        "f3fd73944c174bc63ed882968e0a80bd5bd68fcc70942b4157135337abd2c53b"
    )
