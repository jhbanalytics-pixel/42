"""Release parameterized on an approved immutable run profile.

The R3 profile stays a named constant whose replay bytes are pinned here against
digests captured from the script before the profile parameterization landed.
Every new refusal carries its own code.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from scripts.staging import release_open_intelligence_run as release
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.predictions import (
    PredictionRules,
    PromotedSignal,
    build_signal_prediction_rows,
)
from src.analysis.open_intelligence.run_receipts import build_run_receipt, run_receipt_digest

from tests.unit.test_open_intelligence_release import (
    RELEASED_AT,
    RUN_ID,
    _receipt_fields,
    _Recorder,
    _release_evidence,
    _run,
)

# Captured from the script at commit 613dea2, before the profile parameterization.
R3_TRANSACTION_SHA256 = "634c2e79949e1c691f1fc62238a12348a59f85796ee9517d310a831345706234"
R3_REPORT_SHA256 = "a3af37b75e7ba39e070f4902f95836574da735e02530c86828df049f688d0e6b"
R3_CALL_SEQUENCE_SHA256 = "8d1197b9aedd053d80a333f340c7dfd2bf87a98158183ed6d3b251385728a0c9"
R3_ADMISSION_SHA256 = "8c7c3690152e5382b69b3ea09bc7ff366b0b72fe51447e6c4f2ccdc58504a720"

STAGING_RUN_ID = "run_20260910_staging_v2_r1"
SIGNAL_ID = "sig_" + "1" * 64
PREDICTED_AT = datetime(2026, 9, 10, 6, 30, tzinfo=UTC)
FIRST_SEEN_AT = datetime(2026, 9, 9, 18, tzinfo=UTC)
DEFAULT_RULES = PredictionRules()


def staging_profile(**overrides) -> release.ReleaseRunProfile:
    fields = {
        "profile_name": "staging_2026_09_10",
        "run_id": STAGING_RUN_ID,
        "source_policy_digest": "e" * 64,
        "cutoff": "2026-09-10",
        "generation_pair": ("f" * 64, "9" * 64),
        "canary_namespace": False,
        "prediction_rules": PredictionRules(),
        "independence_policy": "explicit_origin_v2",
    }
    fields.update(overrides)
    return release.ReleaseRunProfile(**fields)


def staging_receipt_fields(**overrides) -> dict:
    fields = {
        "run_id": STAGING_RUN_ID,
        "signal_date": date(2026, 9, 10),
        "observation_start": date(2026, 9, 10),
        "observation_end": date(2026, 9, 10),
        "cluster_build_version": "hybrid_graph_v3",
        "completed_at": datetime(2026, 9, 10, 5, tzinfo=UTC),
    }
    fields.update(overrides)
    return _receipt_fields(**fields)


def candidate_row(**overrides) -> dict:
    row = {
        "client_scope_id": "ogilvy_default",
        "market_scope": ["za", "ng", "ke"],
        "brand_config_id": "brand_default",
        "audience_lens_ids": [],
        "theme_id": "theme_default",
        "run_id": STAGING_RUN_ID,
        "contract_version": "open_intelligence_v2",
        "signal_id": SIGNAL_ID,
        "signal_date": date(2026, 9, 10),
        "market": "za",
        "discovery_mode": "dynamic",
        "evidence_state": "ready",
        "velocity_score": 0.8,
        "breadth_score": 0.7,
        "cluster_build_version": "hybrid_graph_v3",
    }
    row.update(overrides)
    return row


def expected_prediction_rows(rules: PredictionRules = DEFAULT_RULES) -> tuple[dict, ...]:
    return build_signal_prediction_rows(
        promoted_signals=(PromotedSignal(candidate_row(), ("news", "reddit"), FIRST_SEEN_AT),),
        predicted_at=PREDICTED_AT,
        rules=rules,
    )


def capture_entry(markets=("za", "ng", "ke"), policy_digest="e" * 64, **overrides) -> dict:
    entry = {
        "profile_id": "42_daily",
        "profile_version": "42_daily_v1",
        "operation_id": "op_" + "1" * 32,
        "result_id": "res_" + "2" * 32,
        "source_kind": "staging_snapshot",
        "scope": "ogilvy_default",
        "source_tables": ["enriched_content"],
        "snapshot_tables": ["enriched_content_snapshot"],
        "markets": list(markets),
        "observation_window_end": datetime(2026, 9, 11, tzinfo=UTC),
        "collection_started_at": datetime(2026, 9, 11, 0, 5, tzinfo=UTC),
        "collection_completed_at": datetime(2026, 9, 11, 0, 30, tzinfo=UTC),
        "snapshot_as_of": datetime(2026, 9, 11, 0, 31, tzinfo=UTC),
        "capture_available_at": datetime(2026, 9, 11, 0, 32, tzinfo=UTC),
        "content_digest": "1" * 64,
        "schema_digest": "2" * 64,
        "policy_digest": policy_digest,
        "source_digest": "3" * 64,
        "image_digest": "4" * 64,
        "generation": "7",
        "completion_state": "succeeded",
    }
    entry.update(overrides)
    return entry


class _ProfileRecorder(_Recorder):
    def __init__(self, fields, *, candidates=None, predictions=None, **kwargs):
        super().__init__(fields, **kwargs)
        self.candidates = [candidate_row()] if candidates is None else candidates
        self.predictions = (
            [dict(row) for row in expected_prediction_rows()]
            if predictions is None
            else predictions
        )

    def __call__(self, sql: str):
        if sql.startswith("SELECT client_scope_id") and release.CANDIDATES_TABLE in sql:
            self.calls.append(sql)
            return [dict(row) for row in self.candidates]
        if sql.startswith("SELECT client_scope_id") and release.PREDICTIONS_TABLE in sql:
            self.calls.append(sql)
            return [dict(row) for row in self.predictions]
        result = super().__call__(sql)
        if release.RELEASE_RECORD_TABLE in sql and sql.startswith("SELECT run_id"):
            for row in result:
                row["run_id"] = self._fields["run_id"]
        return result


def profile_evidence(recorder, profile):
    blocked = run_receipt_digest(build_run_receipt(**recorder._fields))
    return replace(
        _release_evidence(blocked),
        profile_digest=profile.digest,
        independence_policy=profile.independence_policy,
    )


def run_profile(recorder, profile=None, *, evidence=None, source_authority=None):
    profile = staging_profile() if profile is None else profile
    return release._release_open_intelligence_run(
        run_id=profile.run_id,
        released_at=RELEASED_AT,
        query_runner=recorder,
        evidence=profile_evidence(recorder, profile) if evidence is None else evidence,
        profile=profile,
        source_authority=(capture_entry(),) if source_authority is None else source_authority,
    )


def refusal_code(recorder, profile=None, **kwargs) -> str:
    with pytest.raises(release.ReleaseRefusal) as caught:
        run_profile(recorder, profile, **kwargs)
    assert not any(sql.startswith("BEGIN TRANSACTION") for sql in recorder.calls)
    return caught.value.code


def test_r3_profile_is_a_named_constant_whose_replay_bytes_are_unchanged():
    assert release.R3_RUN_ID == "run_20260903_dynamic_apply_v2_r16"
    assert release.R3_PROFILE.run_id == release.R3_RUN_ID
    assert release.R3_PROFILE.profile_name == "r3"
    assert release.R3_PROFILE.cutoff == "2026-09-03"
    assert release.R3_PROFILE.source_policy_digest is None
    assert release.R3_PROFILE.generation_pair is None
    assert release.R3_PROFILE.canary_namespace is False
    assert release.R3_PROFILE.prediction_rules is None
    assert release.R3_PROFILE.independence_policy == "wave1_family_v1"
    assert release.RELEASE_PROFILES["r3"] is release.R3_PROFILE
    for explicit in (False, True):
        recorder = _Recorder(_receipt_fields())
        if explicit:
            report = release._release_open_intelligence_run(
                run_id=RUN_ID,
                released_at=RELEASED_AT,
                query_runner=recorder,
                evidence=_release_evidence(
                    run_receipt_digest(build_run_receipt(**recorder._fields))
                ),
                profile=release.R3_PROFILE,
            )
        else:
            report = _run(recorder)
        assert type(report) is release.ReleaseReport
        body = next(sql for sql in recorder.calls if sql.startswith("BEGIN TRANSACTION"))
        assert hashlib.sha256(body.encode()).hexdigest() == R3_TRANSACTION_SHA256
        assert hashlib.sha256(canonical_bytes(report)).hexdigest() == R3_REPORT_SHA256
        assert len(recorder.calls) == 8
        joined = "\n".join(recorder.calls)
        assert hashlib.sha256(joined.encode()).hexdigest() == R3_CALL_SEQUENCE_SHA256
    admission = release._release_admission_sql(RUN_ID)
    assert hashlib.sha256(admission.encode()).hexdigest() == R3_ADMISSION_SHA256


def test_cli_selects_a_registered_profile_by_name_and_defaults_to_r3():
    assert release._parse_cli([]) == ()
    assert release._parse_cli(["--profile", "r3"]) == ("r3",)
    assert release._select_profile(()) is release.R3_PROFILE
    assert release._select_profile(("r3",)) is release.R3_PROFILE
    for argv in (["--profile"], ["--profile", "r3", "extra"], ["--run-id", RUN_ID]):
        with pytest.raises(release.ReleaseRefusal):
            release._parse_cli(argv)
    with pytest.raises(release.ReleaseRefusal) as caught:
        release._select_profile(("unregistered",))
    assert caught.value.code == "release_profile_unknown"


def test_executable_boundary_reports_the_profile_refusal_code(capsys):
    assert release.run_executable(["--profile", "unregistered"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"error": "release_profile_unknown"}
    assert release.run_executable(["extra"]) == 1
    assert json.loads(capsys.readouterr().err) == {"error": "r3_release_authority_refused"}


def test_profile_is_immutable_and_its_digest_binds_every_field():
    profile = staging_profile()
    assert len(profile.digest) == 64
    assert profile.digest == canonical_digest(profile.binding())
    assert replace(profile, cutoff="2026-09-11").digest != profile.digest
    assert replace(profile, source_policy_digest="d" * 64).digest != profile.digest
    assert replace(profile, canary_namespace=True).digest != profile.digest
    with pytest.raises(FrozenInstanceError):
        profile.run_id = "other"
    for bad in (
        {"run_id": "Bad Run"},
        {"source_policy_digest": "xyz"},
        {"cutoff": "10-09-2026"},
        {"generation_pair": ("f" * 64,)},
        {"canary_namespace": "no"},
        {"profile_name": ""},
        {"prediction_rules": "weekly"},
        {"independence_policy": "wave1_family_v1"},
        {"independence_policy": "explicit_origin_v3"},
    ):
        with pytest.raises(release.ReleaseRefusal) as caught:
            staging_profile(**bad)
        assert caught.value.code == "release_profile_invalid"
    assert staging_profile().binding()["independence_policy"] == "explicit_origin_v2"
    assert release.R3_PROFILE.binding()["independence_policy"] == "wave1_family_v1"
    recorder = _ProfileRecorder(staging_receipt_fields())
    with pytest.raises(release.ReleaseRefusal) as caught:
        release._release_open_intelligence_run(
            run_id=STAGING_RUN_ID,
            released_at=RELEASED_AT,
            query_runner=recorder,
            evidence=profile_evidence(recorder, profile),
            profile="staging_2026_09_10",
        )
    assert caught.value.code == "release_profile_invalid"
    assert recorder.calls == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"status": "failed"}, "release_run_not_completed"),
        ({"complete_partitions": False}, "release_partitions_incomplete"),
        ({"candidate_count": 0}, "release_run_empty"),
        ({"evidence_count": 0}, "release_run_empty"),
        ({"membership_count": 0}, "release_run_empty"),
        ({"prediction_count": 0}, "release_run_empty"),
    ],
)
def test_receipt_state_refusals_carry_their_own_code(overrides, code):
    recorder = _ProfileRecorder(staging_receipt_fields(**overrides))
    assert refusal_code(recorder) == code


def test_drifted_release_evidence_refuses_with_its_own_code():
    recorder = _ProfileRecorder(staging_receipt_fields())
    profile = staging_profile()
    other_receipt = replace(
        profile_evidence(recorder, profile), blocked_run_receipt_digest="1" * 64
    )
    assert refusal_code(recorder, profile, evidence=other_receipt) == "release_evidence_differs"
    other_source = replace(profile_evidence(recorder, profile), source_sha="d" * 40)
    assert refusal_code(recorder, profile, evidence=other_source) == "release_evidence_differs"


def test_rollback_refuses_malformed_release_records():
    good = released("run_a", datetime(2026, 9, 8, tzinfo=UTC), "e" * 64, "2026-09-08")
    for bad in (
        "run_a",
        {**good, "run_id": "Bad Run"},
        {**good, "released_at": datetime(2026, 9, 8)},
        {**good, "source_policy_digest": "xyz"},
        {**good, "cutoff": "08-09-2026"},
        {**good, "cutoff": None},
    ):
        with pytest.raises(release.ReleaseRefusal) as caught:
            release.select_rollback_release(
                current_run_id="run_a",
                released_records=(bad,),
                compatible_source_policy_digests=("e" * 64,),
                newer_predictions=(),
                newer_results=(),
            )
        assert caught.value.code == "rollback_record_invalid"


def test_a_new_staging_run_releases_with_predictions_enrolled_before_display():
    recorder = _ProfileRecorder(staging_receipt_fields())
    profile = staging_profile()
    report = run_profile(recorder, profile)
    assert isinstance(report, release.ProfileReleaseReport)
    assert isinstance(report, release.ReleaseReport)
    assert report.run_id == STAGING_RUN_ID
    assert report.profile_name == profile.profile_name
    assert report.profile_digest == profile.digest
    assert report.source_policy_digest == "e" * 64
    assert report.cutoff == "2026-09-10"
    rows = expected_prediction_rows()
    assert report.prediction_output_digest == canonical_digest(list(rows))
    assert report.prediction_readback == {
        "prediction_rows": 1,
        "eligible_rows": 0,
        "promoted_signals": 1,
    }
    assert report.prediction_enrollment == {
        "horizon_days": 7,
        "predicted_at": [PREDICTED_AT],
        "evaluation_dates": [date(2026, 9, 17)],
        "baseline_fields": ["breadth", "evidence_family_count", "velocity"],
        "source_families": [["news", "reddit"]],
    }
    assert "no measured accuracy claim" in report.prediction_accuracy_note
    assert report.transaction_readback == {
        "candidate_rows": 1,
        "evidence_rows": 2,
        "membership_rows": 1,
        "lineage_rows": 0,
        "prediction_rows": 1,
        "outcome_rows": 0,
        "analysis_rows": 0,
        "qualifying_prediction_rows": 1,
    }
    transaction = [sql for sql in recorder.calls if sql.startswith("BEGIN TRANSACTION")]
    assert len(transaction) == 1
    body = transaction[0]
    assert "SET display_release_state = 'enabled'" in body
    assert "SET display_eligible = TRUE" not in body
    assert "display_release_state = 'blocked'" in body
    assert body.rstrip().endswith("COMMIT TRANSACTION;")
    enrollment_reads = [
        index
        for index, sql in enumerate(recorder.calls)
        if sql.startswith("SELECT client_scope_id")
    ]
    transaction_index = recorder.calls.index(body)
    assert len(enrollment_reads) == 2
    assert all(index < transaction_index for index in enrollment_reads)
    assert report.approved_by == "durable_execution_approval"
    assert report.approval_document == "release-profile-contract-v1"
    payload = json.loads(canonical_bytes(report))
    assert payload["prediction_output_digest"] == report.prediction_output_digest


def test_a_changed_profile_digest_refuses():
    recorder = _ProfileRecorder(staging_receipt_fields())
    profile = staging_profile()
    drifted = replace(profile_evidence(recorder, profile), profile_digest="a" * 64)
    assert refusal_code(recorder, profile, evidence=drifted) == "release_profile_digest_differs"
    missing = replace(profile_evidence(recorder, profile), profile_digest=None)
    assert refusal_code(recorder, profile, evidence=missing) == "release_profile_digest_differs"


def test_a_run_evaluated_under_another_independence_policy_refuses():
    recorder = _ProfileRecorder(staging_receipt_fields())
    profile = staging_profile()
    legacy = replace(profile_evidence(recorder, profile), independence_policy="wave1_family_v1")
    assert refusal_code(recorder, profile, evidence=legacy) == (
        "release_independence_policy_differs"
    )
    unknown = replace(profile_evidence(recorder, profile), independence_policy=None)
    assert refusal_code(recorder, profile, evidence=unknown) == (
        "release_independence_policy_differs"
    )
    assert release.R3_PROFILE.independence_policy == "wave1_family_v1"
    assert _release_evidence("0" * 64).independence_policy is None


def test_changed_membership_after_certification_refuses():
    recorder = _ProfileRecorder(
        staging_receipt_fields(), admission_overrides={"membership_rows": 2}
    )
    assert refusal_code(recorder) == "release_membership_changed_after_certification"

    class _RowSetDrift(_ProfileRecorder):
        def __call__(self, sql: str):
            result = super().__call__(sql)
            if " AS row_set_digest" in sql:
                return [{"row_set_digest": "0" * 64}]
            return result

    assert refusal_code(_RowSetDrift(staging_receipt_fields())) == (
        "release_membership_changed_after_certification"
    )


def test_a_duplicate_release_refuses():
    recorder = _ProfileRecorder(staging_receipt_fields(), existing_records=1)
    assert refusal_code(recorder) == "release_duplicate"
    already = _ProfileRecorder(staging_receipt_fields(display_release_state="enabled"))
    assert refusal_code(already) == "release_already_released"


def test_a_canary_namespace_never_releases_for_display():
    recorder = _ProfileRecorder(staging_receipt_fields())
    assert refusal_code(recorder, staging_profile(canary_namespace=True)) == (
        "release_canary_namespace"
    )
    assert recorder.calls == []
    canary_receipt = _ProfileRecorder(staging_receipt_fields(client_scope_id="qa_canary"))
    assert refusal_code(canary_receipt) == "release_canary_namespace"


def test_incomplete_source_authority_refuses():
    recorder = _ProfileRecorder(staging_receipt_fields())
    assert refusal_code(recorder, source_authority=()) == "release_source_authority_incomplete"
    partial = (capture_entry(markets=("za", "ng")),)
    assert refusal_code(recorder, source_authority=partial) == (
        "release_source_authority_incomplete"
    )
    other_policy = (capture_entry(policy_digest="d" * 64),)
    assert refusal_code(recorder, source_authority=other_policy) == (
        "release_source_authority_incomplete"
    )
    unfinished = (capture_entry(completion_state="partial"),)
    assert refusal_code(recorder, source_authority=unfinished) == (
        "release_source_authority_incomplete"
    )
    wrong_cutoff = (capture_entry(observation_window_end=datetime(2026, 9, 12, tzinfo=UTC)),)
    assert refusal_code(recorder, source_authority=wrong_cutoff) == (
        "release_source_authority_incomplete"
    )
    malformed = ({"markets": ["za"]},)
    assert refusal_code(recorder, source_authority=malformed) == (
        "release_source_authority_incomplete"
    )
    unbound = staging_profile(source_policy_digest=None)
    assert refusal_code(recorder, unbound) == "release_source_authority_incomplete"


def test_profile_identity_and_cutoff_must_match_the_receipt():
    recorder = _ProfileRecorder(staging_receipt_fields(run_id="run_20260910_other"))
    with pytest.raises(release.ReleaseRefusal) as caught:
        release._release_open_intelligence_run(
            run_id="run_20260910_other",
            released_at=RELEASED_AT,
            query_runner=recorder,
            evidence=profile_evidence(recorder, staging_profile()),
            profile=staging_profile(),
            source_authority=(capture_entry(),),
        )
    assert caught.value.code == "release_profile_identity_differs"
    shifted = _ProfileRecorder(
        staging_receipt_fields(
            signal_date=date(2026, 9, 11),
            observation_start=date(2026, 9, 11),
            observation_end=date(2026, 9, 11),
        )
    )
    assert refusal_code(shifted) == "release_profile_cutoff_differs"


def test_prediction_enrollment_refuses_missing_or_drifted_rows():
    missing = _ProfileRecorder(staging_receipt_fields(), predictions=[])
    assert refusal_code(missing) == "prediction_enrollment_incomplete"
    drifted_row = dict(expected_prediction_rows()[0])
    drifted_row["expected_trajectory"] = "fading"
    drifted = _ProfileRecorder(staging_receipt_fields(), predictions=[drifted_row])
    assert refusal_code(drifted) == "prediction_enrollment_differs"
    unpromoted = _ProfileRecorder(
        staging_receipt_fields(), candidates=[candidate_row(evidence_state="thin")]
    )
    assert refusal_code(unpromoted) == "prediction_enrollment_differs"
    with_horizon = staging_profile(prediction_rules=PredictionRules(evaluation_days=14))
    assert refusal_code(_ProfileRecorder(staging_receipt_fields()), with_horizon) == (
        "prediction_enrollment_differs"
    )


def test_generation_pair_must_match_the_active_pair():
    profile = staging_profile()
    active = SimpleNamespace(origin_registry_sha256="f" * 64, resource_manifest_sha256="9" * 64)
    assert release._require_profile_generation(profile, active) == ("f" * 64, "9" * 64)
    stale = SimpleNamespace(origin_registry_sha256="f" * 64, resource_manifest_sha256="8" * 64)
    with pytest.raises(release.ReleaseRefusal) as caught:
        release._require_profile_generation(profile, stale)
    assert caught.value.code == "release_generation_pair_differs"
    assert release._require_profile_generation(release.R3_PROFILE, stale) is None


def test_main_refuses_a_staging_profile_until_its_adapters_exist(monkeypatch):
    calls = []
    monkeypatch.setattr(
        release,
        "_operation_binding",
        lambda *args, **kwargs: calls.append(args) or (None, None),
    )
    monkeypatch.setattr(release.bigquery, "Client", lambda **_kwargs: object())
    monkeypatch.setitem(release.RELEASE_PROFILES, "staging_2026_09_10", staging_profile())
    monkeypatch.setattr(
        release.execution_generations,
        "active_generation",
        lambda: SimpleNamespace(
            origin_registry_sha256="f" * 64, resource_manifest_sha256="9" * 64, registry=None
        ),
    )
    with pytest.raises(release.ReleaseRefusal) as caught:
        release.main(["--profile", "staging_2026_09_10"])
    assert caught.value.code == "release_profile_adapter_unavailable"
    assert calls == []


def released(run_id, released_at, policy, cutoff):
    return {
        "run_id": run_id,
        "released_at": released_at,
        "source_policy_digest": policy,
        "cutoff": cutoff,
    }


def test_rollback_selects_a_compatible_older_release_and_keeps_newer_evidence():
    records = (
        released("run_a", datetime(2026, 9, 8, tzinfo=UTC), "e" * 64, "2026-09-08"),
        released("run_b", datetime(2026, 9, 9, tzinfo=UTC), "d" * 64, "2026-09-09"),
        released("run_c", datetime(2026, 9, 10, tzinfo=UTC), "e" * 64, "2026-09-10"),
    )
    newer_predictions = ({"run_id": "run_c", "prediction_id": "pred_1"},)
    newer_results = ({"run_id": "run_c", "result_id": "res_1"},)
    selection = release.select_rollback_release(
        current_run_id="run_c",
        released_records=records,
        compatible_source_policy_digests=("e" * 64,),
        newer_predictions=newer_predictions,
        newer_results=newer_results,
    )
    assert selection.display_run_id == "run_a"
    assert selection.source_policy_digest == "e" * 64
    assert selection.retained_run_ids == ("run_b", "run_c")
    assert selection.retained_predictions is newer_predictions
    assert selection.retained_results is newer_results
    assert selection.evidence_edits == ()
    assert newer_predictions == ({"run_id": "run_c", "prediction_id": "pred_1"},)
    assert newer_results == ({"run_id": "run_c", "result_id": "res_1"},)
    with pytest.raises(release.ReleaseRefusal) as caught:
        release.select_rollback_release(
            current_run_id="run_c",
            released_records=records,
            compatible_source_policy_digests=("a" * 64,),
            newer_predictions=newer_predictions,
            newer_results=newer_results,
        )
    assert caught.value.code == "rollback_no_compatible_release"
    with pytest.raises(release.ReleaseRefusal) as caught:
        release.select_rollback_release(
            current_run_id="run_a",
            released_records=records,
            compatible_source_policy_digests=("e" * 64,),
            newer_predictions=(),
            newer_results=(),
        )
    assert caught.value.code == "rollback_no_compatible_release"
    with pytest.raises(release.ReleaseRefusal) as caught:
        release.select_rollback_release(
            current_run_id="run_zz",
            released_records=records,
            compatible_source_policy_digests=("e" * 64,),
            newer_predictions=(),
            newer_results=(),
        )
    assert caught.value.code == "rollback_current_release_unknown"
    assert timedelta(days=1) == records[1]["released_at"] - records[0]["released_at"]


# D05: the live release records its run scoped decision, never an observation


def test_release_authority_paths_do_not_import_the_telemetry_sink():
    import importlib
    from pathlib import Path

    for name in (
        "scripts.staging.release_open_intelligence_run",
        "src.analysis.open_intelligence.readiness",
        "src.analysis.open_intelligence.rows",
        "src.analysis.open_intelligence.execution_approval",
    ):
        source = Path(importlib.import_module(name).__file__).read_text(encoding="utf-8")
        assert "coverage_telemetry" not in source, name


def test_live_profile_release_records_released_signals_and_an_unknown_observation_boundary(
    monkeypatch,
):
    from src.analysis.open_intelligence.coverage_telemetry_sink import (
        BoundaryTelemetry,
        MemoryDispositionSink,
    )

    profile = staging_profile()
    calls = []

    report = release.ProfileReleaseReport.__new__(release.ProfileReleaseReport)
    object.__setattr__(report, "run_id", profile.run_id)
    object.__setattr__(report, "transaction_readback", {"candidate_rows": 4, "membership_rows": 9})
    monkeypatch.setattr(
        release,
        "_release_open_intelligence_run",
        lambda **kwargs: (calls.append(kwargs), report)[1],
    )
    adapter = SimpleNamespace(
        compose=lambda _profile: ("authority",),
        certify=lambda _profile: "evidence",
        release=lambda _profile: lambda sql: [],
    )
    telemetry = BoundaryTelemetry(MemoryDispositionSink())
    released = release.release_live_profile(
        profile, adapter=adapter, released_at=RELEASED_AT, telemetry=telemetry
    )
    assert released is report
    assert calls[0]["run_id"] == profile.run_id
    assert telemetry.records == []
    assert telemetry.notes == [
        {
            "kind": "count",
            "unit": "released_signals",
            "value": 4,
            "market": None,
            "operation_id": profile.run_id,
            "observed_at": RELEASED_AT,
        },
        {
            "kind": "unavailable",
            "boundary": "release",
            "operation_id": profile.run_id,
            "reason_code": "release_decision_is_run_scoped",
            "market": None,
            "observed_at": None,
        },
    ]
    # The default call has no telemetry and behaves exactly as before.
    assert release.release_live_profile(profile, adapter=adapter, released_at=RELEASED_AT) is report
