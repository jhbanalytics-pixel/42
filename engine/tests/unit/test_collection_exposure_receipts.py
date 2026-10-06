from __future__ import annotations

import hashlib
import inspect
import io
import json
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from scripts.staging import issue_collection_exposure_receipts as issuer
from src.contracts.bigquery_ddl import parse_table_ddl

from tests.unit.test_execution_runtime_v2 import fixture as _fixture_v2
from tests.unit.test_execution_runtime_v2 import load as _load_v2

ROOT = Path(__file__).resolve().parents[2]


def _issued_exposure_authority():
    # A real v2 authority for collection_exposure_issue: its job, principal and image come
    # from the packaged origin the loader binds, which is what the issuer verifies.
    return _load_v2(_fixture_v2("collection_exposure_issue"))


def _fields(**overrides):
    fields = {
        "exposure_contract_version": "collection_exposure_receipt_v1",
        "source_family": "news",
        "exposure_date": date(2026, 9, 1),
        "collection_policy_digest": "a" * 64,
        "source_sha": "b" * 40,
        "image_digest": "sha256:" + "c" * 64,
        "config_digest": "d" * 64,
        "quota_authority_id": "e" * 64,
        "quota_applicability": "metered",
        "quota_unit": "requests",
        "quota_limit": 100,
        "quota_used": 75,
        "quota_exhausted": False,
        "capture_complete": True,
        "source_copy_receipt_refs": (
            {
                "copy_run_id": "copy_001",
                "source_table": "enriched_content",
                "source_set_digest": "f" * 64,
            },
        ),
        "issued_at": datetime(2026, 8, 30, 10, tzinfo=UTC),
        "issuer_identity": "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
    }
    fields.update(overrides)
    fields["receipt_digest"] = issuer.exposure_receipt_digest(fields)
    return fields


def test_collection_exposure_schema_is_exact_append_only_and_partitioned():
    sql = (
        (ROOT / "infra/bigquery_schemas/collection_exposure_receipts_v1.sql")
        .read_text(encoding="utf-8")
        .format(project="fixture-project", dataset="fixture_dataset")
    )
    parsed = parse_table_ddl(sql)
    assert tuple(field[0] for field in parsed["fields"]) == issuer.EXPOSURE_RECEIPT_FIELDS
    assert parsed["partition"] == "exposure_date"
    assert parsed["cluster"] == ("source_family",)
    refs = next(field for field in parsed["fields"] if field[0] == "source_copy_receipt_refs")
    assert refs[2] == "REPEATED"
    assert tuple(field[0] for field in refs[4]) == issuer.SOURCE_COPY_REF_FIELDS


def test_metered_and_unmetered_receipts_validate_without_issuance():
    assert issuer.validate_exposure_receipt(_fields())["quota_used"] == 75
    unmetered = _fields(
        source_family="rss",
        quota_applicability="unmetered",
        quota_unit=None,
        quota_limit=None,
        quota_used=None,
        quota_exhausted=False,
    )
    assert issuer.validate_exposure_receipt(unmetered)["quota_unit"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"quota_used": 101},
        {"quota_limit": -1},
        {"quota_applicability": "unmetered", "quota_unit": "requests"},
        {"quota_applicability": "unmetered", "quota_exhausted": True},
        {"source_copy_receipt_refs": ()},
        {
            "source_copy_receipt_refs": (
                {"copy_run_id": "z", "source_table": "raw_content", "source_set_digest": "f" * 64},
                {"copy_run_id": "a", "source_table": "raw_content", "source_set_digest": "e" * 64},
            )
        },
    ],
)
def test_invalid_exposure_receipt_refuses(overrides):
    with pytest.raises(issuer.ExposureRefusal):
        issuer.validate_exposure_receipt(_fields(**overrides))


def test_receipt_digest_excludes_issuance_metadata_and_refuses_non_nfc():
    first = _fields()
    second = _fields(
        issued_at=datetime(2026, 8, 30, 11, tzinfo=UTC),
        issuer_identity="different@example.invalid",
    )
    assert first["receipt_digest"] == second["receipt_digest"]
    with pytest.raises(issuer.ExposureRefusal, match="noncanonical_unicode"):
        issuer.exposure_receipt_digest(_fields(source_family="Cafe\u0301"))


def test_duplicate_family_date_refuses_even_when_content_is_equal():
    receipt = issuer.validate_exposure_receipt(_fields())
    with pytest.raises(issuer.ExposureRefusal, match="duplicate exposure natural key"):
        issuer.validate_exposure_receipt_set((receipt, receipt))


def test_durable_collection_exposure_consumes_before_query_and_records_result(monkeypatch):
    from types import SimpleNamespace

    events = []
    authority = _issued_exposure_authority()
    manifest = authority.manifest
    approval = authority.approval
    receipt = _fields(
        source_sha=manifest.source_sha,
        image_digest=manifest.image_uri.rsplit("@", 1)[1],
        issuer_identity=manifest.service_identity,
    )
    consumption = SimpleNamespace(consumption_id="exc_" + "b" * 64)
    result = SimpleNamespace(result_id="exr_" + "c" * 64)
    recorded_results = []

    def load(operation, *, mode, artifact_reader=None):
        events.append("load")
        assert operation == "collection_exposure_issue"
        assert mode == "new_consume"
        assert artifact_reader is issuer._execution_approval_artifact_bytes
        for name in (
            "issuer_contract",
            "source_copy_receipt_set",
            "vendor_quota_receipt",
            "config",
        ):
            assert issuer._execution_approval_artifact_bytes(name)
        return authority

    monkeypatch.setattr(
        issuer.execution_approval,
        "_consume_execution_authority",
        lambda _authority: events.append("consume") or consumption,
    )
    monkeypatch.setattr(
        issuer.execution_approval,
        "_record_execution_result",
        lambda *args, **kwargs: (
            recorded_results.append((args, kwargs)) or events.append("result") or result
        ),
    )

    def query_runner(sql):
        events.append("query")
        if sql.startswith("SELECT COUNT(*)"):
            return [{"existing": 0}]
        if sql.startswith("SELECT exposure_contract_version"):
            return [dict(receipt)]
        return []

    output = issuer._execute_durable_collection_exposure(
        receipts=(receipt,),
        query_runner=query_runner,
        authority_loader=load,
    )
    assert events[0:2] == ["load", "consume"]
    assert events.index("consume") < events.index("query") < events.index("result")
    assert output["execution_approval"] == {
        "manifest_sha256": approval.manifest_sha256,
        "approval_id": approval.approval_id,
        "consumption_id": consumption.consumption_id,
        "result_id": result.result_id,
    }
    assert output["run_id"] == "run_20260903_dynamic_apply_v2_r16"
    assert json.loads(recorded_results[0][0][3])["run_id"] == output["run_id"]
    # The ledger records one result per reference and the receipts key on family and day,
    # so the reference must carry the run: a second run over the same days is a new result.
    assert recorded_results[0][0][2] == (
        "bq://ogilvy-trends-v2.trends_v2_staging.collection_exposure_receipts_v1#"
        + output["run_id"]
        + "/"
        + output["inserted_natural_key_digest"]
    )
    assert issuer._DURABLE_ARTIFACT_CONTEXT is None


def test_issuer_contract_binds_the_exact_r3_run_and_source_window() -> None:
    artifacts = issuer._build_execution_artifacts((_fields(),))
    contract = json.loads(artifacts["issuer_contract"])
    assert contract["run_id"] == "run_20260903_dynamic_apply_v2_r16"
    assert contract["window_start"] == "2026-08-21"
    assert contract["window_end"] == "2026-09-03"


def test_exposure_receipt_outside_the_r3_window_refuses() -> None:
    with pytest.raises(issuer.ExposureRefusal, match="window"):
        issuer.validate_exposure_receipt_set((_fields(exposure_date=date(2026, 9, 4)),))


def test_post_consumption_failure_records_one_sanitized_terminal_result(monkeypatch):
    from types import SimpleNamespace

    authority = _issued_exposure_authority()
    manifest = authority.manifest
    consumption = SimpleNamespace(consumption_id="exc_" + "3" * 64)
    receipt = _fields(
        source_sha=manifest.source_sha,
        image_digest=manifest.image_uri.rsplit("@", 1)[1],
        issuer_identity=manifest.service_identity,
    )
    recorded = []
    monkeypatch.setattr(
        issuer.execution_approval,
        "_consume_execution_authority",
        lambda _authority: consumption,
    )
    monkeypatch.setattr(
        issuer.execution_approval,
        "_record_execution_result",
        lambda *args, **kwargs: (
            recorded.append((args, kwargs)) or SimpleNamespace(result_id="exr_" + "4" * 64)
        ),
    )

    def fail_query(_sql):
        raise RuntimeError("private warehouse detail")

    with pytest.raises(RuntimeError, match="private warehouse detail"):
        issuer._execute_durable_collection_exposure(
            receipts=(receipt,),
            query_runner=fail_query,
            authority_loader=lambda _operation, **_kwargs: authority,
        )

    assert len(recorded) == 1
    args = recorded[0][0]
    payload = json.loads(args[3])
    assert args[5] == "failed"
    assert payload == {
        "error_code": "collection_exposure_issue_failed",
        "run_id": "run_20260903_dynamic_apply_v2_r16",
        "status": "failed",
    }
    assert "private warehouse detail" not in args[3]
    assert issuer._DURABLE_ARTIFACT_CONTEXT is None


def test_public_main_accepts_only_argv_and_uses_durable_path(monkeypatch, tmp_path, capsys):
    assert tuple(inspect.signature(issuer.main).parameters) == ("argv",)
    receipt = _fields()
    receipt_file = tmp_path / "durable-receipts.json"
    receipt_file.write_text(
        issuer.canonical_bytes((receipt,)).decode("utf-8"),
        encoding="utf-8",
    )
    events = []

    class Client:
        pass

    monkeypatch.setattr("google.cloud.bigquery.Client", lambda **_kwargs: Client())
    monkeypatch.setattr(
        issuer,
        "_execute_durable_collection_exposure",
        lambda **_kwargs: (
            events.append("durable")
            or {
                "execution_proof_contract_version": issuer.EXECUTION_PROOF_CONTRACT_VERSION,
                "execution_approval": {
                    "manifest_sha256": "1" * 64,
                    "approval_id": "exa_" + "2" * 64,
                    "consumption_id": "exc_" + "3" * 64,
                    "result_id": "exr_" + "4" * 64,
                },
            }
        ),
    )
    assert issuer.main(["--receipt-file", str(receipt_file.resolve())]) == 0
    assert events == ["durable"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_approval"]["result_id"].startswith("exr_")


def test_contracted_argument_free_invocation_reads_the_receipt_object_for_its_manifest(
    monkeypatch, capsys
):
    # The execution contract pins the container arguments to the script alone, and every
    # receipt must carry the issuing build's own source SHA and image digest, which a file
    # inside that image cannot know. So the durable path learns its manifest digest from the
    # job annotation and reads the receipt object published for that manifest.
    receipt = _fields()
    payload = issuer.canonical_bytes((receipt,))
    seen = []
    monkeypatch.setattr(issuer, "_durable_manifest_sha256", lambda: "9" * 64)
    monkeypatch.setattr(
        issuer,
        "_read_durable_receipt_bytes",
        lambda manifest_sha256: seen.append(manifest_sha256) or payload,
    )

    class Client:
        pass

    monkeypatch.setattr("google.cloud.bigquery.Client", lambda **_kwargs: Client())
    monkeypatch.setattr(
        issuer,
        "_execute_durable_collection_exposure",
        lambda **kwargs: (
            seen.append(kwargs["receipts"])
            or {
                "execution_proof_contract_version": issuer.EXECUTION_PROOF_CONTRACT_VERSION,
                "execution_approval": {
                    "manifest_sha256": "1" * 64,
                    "approval_id": "exa_" + "2" * 64,
                    "consumption_id": "exc_" + "3" * 64,
                    "result_id": "exr_" + "4" * 64,
                },
            }
        ),
    )
    assert issuer.main([]) == 0
    assert seen[0] == "9" * 64
    assert len(seen[1]) == 1
    assert json.loads(capsys.readouterr().out)["execution_approval"]["result_id"].startswith("exr_")
    assert issuer.durable_receipt_object_name("9" * 64) == (
        "exposure/receipts/" + "9" * 64 + ".json"
    )
    assert issuer.DURABLE_RECEIPT_BUCKET == "ogilvy-trends-v2-execution-approvals-staging"
    with pytest.raises(issuer.ExposureRefusal, match="grammar"):
        issuer.main(["--receipt-file"])
    with pytest.raises(issuer.ExposureRefusal, match="grammar"):
        issuer.main(["extra", "argument", "here"])


def test_durable_manifest_digest_comes_from_the_job_annotation(monkeypatch):
    payload = {
        "execution": {"name": "projects/p/locations/l/jobs/j/executions/e"},
        "job": {"template": {"annotations": {"42.ogilvy/execution-approval-sha256": "a" * 64}}},
    }
    monkeypatch.setattr(issuer.execution_approval, "_default_execution_reader", lambda: payload)
    assert issuer._durable_manifest_sha256() == "a" * 64
    payload["job"]["template"]["annotations"]["42.ogilvy/execution-approval-sha256"] = "short"
    with pytest.raises(issuer.ExposureRefusal, match="manifest"):
        issuer._durable_manifest_sha256()


def test_internal_refusals_name_their_cause_without_leaking_detail(capsys):
    # Execution d9tss printed only issuer_internal_refusal; the approval refusal code behind it
    # is not secret and is the one fact an operator needs.

    from src.analysis.open_intelligence import execution_approval

    def approval_boundary(_values):
        raise execution_approval.ApprovalRefusal("execution_approval_artifact_mismatch")

    def generic_boundary(_values):
        raise RuntimeError("private detail that must not surface")

    out = io.StringIO()
    assert issuer.run_executable([], runner=approval_boundary, stderr=out) == 1
    payload = json.loads(out.getvalue())
    assert payload == {
        "error": "issuer_internal_refusal",
        "refusal": "execution_approval_artifact_mismatch",
    }
    out = io.StringIO()
    assert issuer.run_executable([], runner=generic_boundary, stderr=out) == 1
    payload = json.loads(out.getvalue())
    assert payload == {"error": "issuer_internal_refusal", "refusal": "RuntimeError"}
    assert "private detail" not in out.getvalue()


# Named exposure profiles. The R3 profile keeps its window, run and bytes; a daily date's
# window validates only under the daily profile, named explicitly and never read off a date.

DAILY_SIGNAL = date(2026, 9, 10)
DAILY_WINDOW = tuple(DAILY_SIGNAL - timedelta(days=offset) for offset in range(13, -1, -1))
R3_ARTIFACT_SHA256 = {
    "config": "ac6b9e8785ddc0681eb0eb230ffbbe70430a0c662da617fc3d5ed2c6afa568a6",
    "issuer_contract": "74679c486d37e48afc0b74571bc2b7fb7a518bd35408ab39bfb58a995ed1776c",
    "source_copy_receipt_set": "191db8eae3be4517c55896edb2ee5d0f3f1be85eaf4cb394c330ba70b40469d0",
    "vendor_quota_receipt": "5c39e2379b018d8c90ce5b71bd0704ca4ab92f04564dd85516a1a17445afba9e",
}


def _daily_set(days=DAILY_WINDOW, family="news"):
    return tuple(_fields(source_family=family, exposure_date=day) for day in days)


def test_the_r3_profile_keeps_its_window_run_and_artifact_bytes():
    profile = issuer.R3_EXPOSURE_PROFILE
    assert profile.name == "r3_exposure_v1"
    assert profile.run_id == "run_20260903_dynamic_apply_v2_r16"
    assert (profile.window_start, profile.window_end) == (date(2026, 8, 21), date(2026, 9, 3))
    # Measured on the tree before profiles existed: the approved R3 artifacts are unmoved.
    artifacts = issuer._build_execution_artifacts((_fields(),))
    assert {name: hashlib.sha256(raw).hexdigest() for name, raw in artifacts.items()} == (
        R3_ARTIFACT_SHA256
    )
    assert issuer._build_execution_artifacts((_fields(),), profile=profile) == artifacts
    receipts = (_fields(exposure_date=date(2026, 8, 21)), _fields(exposure_date=date(2026, 9, 3)))
    assert issuer.validate_exposure_receipt_set(receipts) == (
        issuer.validate_exposure_receipt_window(receipts, profile=profile)
    )
    with pytest.raises(issuer.ExposureRefusal, match="outside the R3 source window"):
        issuer.validate_exposure_receipt_set((_fields(exposure_date=date(2026, 8, 20)),))


def test_a_daily_window_validates_only_under_the_daily_profile():
    profile = issuer.daily_exposure_profile(DAILY_SIGNAL)
    assert profile.name == "daily_exposure_v1"
    assert profile.run_id == "daily_exposure_20260910"
    assert (profile.window_start, profile.window_end) == (date(2026, 8, 28), DAILY_SIGNAL)
    validated = issuer.validate_exposure_receipt_window(_daily_set(), profile=profile)
    assert tuple(item["exposure_date"] for item in validated) == DAILY_WINDOW
    with pytest.raises(issuer.ExposureRefusal, match="outside the R3 source window"):
        issuer.validate_exposure_receipt_set(_daily_set())


def test_the_daily_window_is_the_composer_factor_window():
    from src.analysis.open_intelligence import daily_composer

    assert issuer.DAILY_EXPOSURE_WINDOW_DAYS == daily_composer.FACTOR_WINDOW_DAYS == 14


def test_a_daily_receipt_is_refused_under_the_r3_profile():
    with pytest.raises(issuer.ExposureRefusal, match="outside the R3 source window"):
        issuer.validate_exposure_receipt_window(
            (_fields(exposure_date=DAILY_SIGNAL),), profile=issuer.R3_EXPOSURE_PROFILE
        )


@pytest.mark.parametrize(
    "day", [DAILY_SIGNAL + timedelta(days=1), DAILY_SIGNAL - timedelta(days=14)]
)
def test_a_receipt_beyond_the_daily_window_is_refused(day):
    with pytest.raises(issuer.ExposureRefusal, match="outside the daily source window"):
        issuer.validate_exposure_receipt_window(
            (_fields(exposure_date=day),), profile=issuer.daily_exposure_profile(DAILY_SIGNAL)
        )


def test_a_gap_in_one_family_of_the_daily_window_is_refused():
    gapped = _daily_set(days=DAILY_WINDOW[:5] + DAILY_WINDOW[6:])
    profile = issuer.daily_exposure_profile(DAILY_SIGNAL)
    with pytest.raises(issuer.ExposureRefusal, match="gap in the daily source window"):
        issuer.validate_exposure_receipt_window(gapped, profile=profile)
    # One contiguous run per family is not a gap, and families are judged apart: a single
    # closed day, as the daily stage issues it, validates beside a whole window.
    single = (_fields(source_family="music", exposure_date=DAILY_SIGNAL),)
    assert issuer.validate_exposure_receipt_window(_daily_set() + single, profile=profile)


def test_a_set_mixing_the_r3_and_daily_windows_is_refused_under_either_profile():
    mixed = (_fields(exposure_date=date(2026, 8, 25)), _fields(exposure_date=DAILY_SIGNAL))
    with pytest.raises(issuer.ExposureRefusal, match="outside the R3 source window"):
        issuer.validate_exposure_receipt_set(mixed)
    with pytest.raises(issuer.ExposureRefusal, match="outside the daily source window"):
        issuer.validate_exposure_receipt_window(
            mixed, profile=issuer.daily_exposure_profile(DAILY_SIGNAL)
        )


@pytest.mark.parametrize(
    "profile", [None, "daily_exposure_v1", {"name": "daily_exposure_v1"}, DAILY_SIGNAL]
)
def test_the_profile_is_named_explicitly_and_never_read_off_a_date(profile):
    with pytest.raises(issuer.ExposureRefusal, match="exposure profile must be named"):
        issuer.validate_exposure_receipt_window(_daily_set(), profile=profile)
    with pytest.raises(TypeError):
        issuer.validate_exposure_receipt_window(_daily_set())


@pytest.mark.parametrize("value", [None, "2026-09-10", datetime(2026, 9, 10, tzinfo=UTC)])
def test_a_daily_profile_needs_a_calendar_date(value):
    with pytest.raises(issuer.ExposureRefusal, match="daily exposure signal date"):
        issuer.daily_exposure_profile(value)


def _artifact(profile, receipts):
    rows = [
        {
            **dict(item),
            "exposure_date": item["exposure_date"].isoformat(),
            "issued_at": item["issued_at"].isoformat(),
            "source_copy_receipt_refs": [dict(ref) for ref in item["source_copy_receipt_refs"]],
        }
        for item in receipts
    ]
    return json.dumps(rows if profile is None else {"exposure_profile": profile, "receipts": rows})


def test_the_receipt_artifact_names_its_profile_and_a_bare_list_stays_r3():
    profile, receipts = issuer._parse_profiled_receipt_artifact(
        _artifact({"name": "daily_exposure_v1", "signal_date": "2026-09-10"}, _daily_set())
    )
    assert profile == issuer.daily_exposure_profile(DAILY_SIGNAL)
    assert len(receipts) == 14
    profile, receipts = issuer._parse_profiled_receipt_artifact(_artifact(None, (_fields(),)))
    assert profile == issuer.R3_EXPOSURE_PROFILE
    assert receipts == issuer._parse_receipt_artifact(_artifact(None, (_fields(),)))
    # The bare list parser is the R3 one and refuses a daily artifact.
    with pytest.raises(issuer.ExposureRefusal):
        issuer._parse_receipt_artifact(
            _artifact({"name": "daily_exposure_v1", "signal_date": "2026-09-10"}, _daily_set())
        )


@pytest.mark.parametrize(
    "profile",
    [
        {"name": "daily_exposure_v2", "signal_date": "2026-09-10"},
        {"name": "daily_exposure_v1"},
        {"name": "daily_exposure_v1", "signal_date": "2026-09-10", "window_days": 30},
        {"name": "r3_exposure_v1", "signal_date": "2026-09-10"},
        "daily_exposure_v1",
    ],
)
def test_an_artifact_profile_that_is_not_exact_is_refused(profile):
    with pytest.raises(issuer.ExposureRefusal, match="exposure profile"):
        issuer._parse_profiled_receipt_artifact(_artifact(profile, _daily_set()))


def test_a_daily_issuance_binds_its_own_profile_into_contract_proof_and_reference(monkeypatch):
    from types import SimpleNamespace

    authority = _issued_exposure_authority()
    manifest = authority.manifest
    profile = issuer.daily_exposure_profile(DAILY_SIGNAL)
    receipts = tuple(
        _fields(
            exposure_date=day,
            source_sha=manifest.source_sha,
            image_digest=manifest.image_uri.rsplit("@", 1)[1],
            issuer_identity=manifest.service_identity,
        )
        for day in DAILY_WINDOW[-1:]
    )
    seen = {}
    recorded = []

    def load(operation, *, mode, artifact_reader=None):
        seen["contract"] = json.loads(artifact_reader("issuer_contract"))
        return authority

    monkeypatch.setattr(
        issuer.execution_approval,
        "_consume_execution_authority",
        lambda _authority: SimpleNamespace(consumption_id="exc_" + "b" * 64),
    )
    monkeypatch.setattr(
        issuer.execution_approval,
        "_record_execution_result",
        lambda *args, **kwargs: (
            recorded.append(args) or SimpleNamespace(result_id="exr_" + "c" * 64)
        ),
    )

    def query_runner(sql):
        if sql.startswith("SELECT COUNT(*)"):
            return [{"existing": 0}]
        if sql.startswith("SELECT exposure_contract_version"):
            return [dict(item) for item in receipts]
        return []

    output = issuer._execute_durable_collection_exposure(
        receipts=receipts, query_runner=query_runner, authority_loader=load, profile=profile
    )
    assert seen["contract"]["exposure_profile"] == "daily_exposure_v1"
    assert seen["contract"]["run_id"] == "daily_exposure_20260910"
    assert (seen["contract"]["window_start"], seen["contract"]["window_end"]) == (
        "2026-08-28",
        "2026-09-10",
    )
    assert output["run_id"] == "daily_exposure_20260910"
    assert recorded[0][2].endswith(
        "#daily_exposure_20260910/" + output["inserted_natural_key_digest"]
    )
    # The same daily receipts cannot be issued under the R3 profile.
    with pytest.raises(issuer.ExposureRefusal, match="outside the R3 source window"):
        issuer._execute_durable_collection_exposure(
            receipts=receipts, query_runner=query_runner, authority_loader=load
        )
