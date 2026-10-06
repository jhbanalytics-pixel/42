"""Contract tests for the staging weekly outcome CLI."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence.outcome_reporting import (
    closed_cohort_record_digest,
    verify_closed_cohort_record,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "staging" / "run_open_intelligence_outcomes.py"
RULES = ROOT / "configs" / "open_intelligence_outcome_rules.yaml"
NOW = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
STATES = {
    "peaked": 0,
    "sustained": 1,
    "fizzled": 0,
    "noise": 0,
    "unresolved": 1,
}


def load_cli():
    spec = importlib.util.spec_from_file_location("weekly_outcome_cli", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_certified_rules(path: Path, **overrides: object) -> Path:
    values = {
        "rule_version": "outcome_rules_v1",
        "status": "replay_certified",
        "replay_receipt_id": "test_fixture_receipt",
        "approved_at": "2026-09-01T00:00:00Z",
        "approved_by": "test_fixture_approver",
        "expires_at": "2026-10-01T00:00:00Z",
    }
    values.update(overrides)
    path.write_text(
        "\n".join(f"{key}: {json.dumps(value)}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return path


def persistence_receipt(*, dry_run: bool) -> SimpleNamespace:
    counts = {
        "candidates": 0,
        "evidence": 0,
        "membership": 0,
        "lineage": 0,
        "predictions": 0,
        "outcomes": 2,
    }
    zero = dict.fromkeys(counts, 0)
    return SimpleNamespace(
        dry_run=dry_run,
        validated_counts=counts,
        inserted_counts=dict(zero) if dry_run else {**zero, "outcomes": 2},
        unchanged_counts=dict(zero),
        conflict_counts=dict(zero),
        cleanup_state="not_started" if dry_run else "complete",
    )


def outcome_result(run_id: str, *, dry_run: bool, content_marker: str = ""):
    return SimpleNamespace(
        evaluation_run_id=run_id,
        dry_run=dry_run,
        due_prediction_count=2,
        outcome_row_count=2,
        missing_prediction_ids=("pred_" + "a" * 64,),
        state_counts=STATES,
        persistence_result=persistence_receipt(dry_run=dry_run),
        source_content=content_marker,
    )


class ExactApplyClient:
    project = "ogilvy-trends-v2"
    location = "US"
    _credentials = SimpleNamespace(
        service_account_email=("trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"),
        quota_project_id=None,
    )


def test_entrypoint_help_exposes_only_the_approved_arguments() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0
    assert "--week-ending" in completed.stdout
    assert "--client-scope-id" in completed.stdout
    assert "--apply" in completed.stdout
    for forbidden in (
        "--project",
        "--dataset",
        "--model",
        "--source",
        "--credit",
        "--rule-version",
        "--approver",
        "--run-id",
    ):
        assert forbidden not in completed.stdout


@pytest.mark.parametrize("apply", [False, True])
def test_checked_in_uncertified_rules_refuse_before_client_or_reader(apply: bool) -> None:
    module = load_cli()
    calls: list[str] = []

    with pytest.raises(module.OutcomeCliRefusal, match="not replay certified"):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=apply,
            client_factory=lambda **kwargs: calls.append("client"),
            job_runner=lambda **kwargs: calls.append("reader"),
            clock=lambda: NOW,
            rule_config_path=RULES,
            legacy_v1=True,
        )

    assert calls == []


@pytest.mark.parametrize(
    ("week_ending", "clock"),
    [
        (date(2026, 9, 6), datetime(2026, 9, 6, 0, 0, tzinfo=UTC)),
        (date(2026, 9, 13), NOW),
    ],
)
def test_current_or_future_sunday_refuses_before_rules_client_or_reader(
    tmp_path: Path, week_ending: date, clock: datetime
) -> None:
    module = load_cli()
    calls: list[str] = []

    with pytest.raises(module.OutcomeCliRefusal, match="closed Sunday"):
        module.run_weekly_outcomes(
            week_ending=week_ending,
            client_scope_id="fixture_scope",
            apply=False,
            client_factory=lambda **kwargs: calls.append("client"),
            job_runner=lambda **kwargs: calls.append("reader"),
            clock=lambda: clock,
            rule_config_path=tmp_path / "must-not-be-read.yaml",
            legacy_v1=True,
        )

    assert calls == []


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        (None, "missing"),
        ("- invalid\n", "mapping"),
        ({"status": "uncertified"}, "not replay certified"),
        ({"expires_at": "2026-09-01T00:00:00Z"}, "expired"),
        ({"rule_version": "outcome_rules_v2"}, "rule version"),
    ],
)
def test_invalid_rule_bundles_refuse_before_client_use(
    tmp_path: Path, fixture: object, message: str
) -> None:
    module = load_cli()
    path = tmp_path / "rules.yaml"
    if fixture is None:
        pass
    elif isinstance(fixture, str):
        path.write_text(fixture, encoding="utf-8")
    else:
        write_certified_rules(path, **fixture)
    calls: list[str] = []

    with pytest.raises(module.OutcomeCliRefusal, match=message):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=False,
            client_factory=lambda **kwargs: calls.append("client"),
            job_runner=lambda **kwargs: calls.append("reader"),
            clock=lambda: NOW,
            rule_config_path=path,
            legacy_v1=True,
        )

    assert calls == []


def test_week_derivation_run_ids_order_and_empty_external_inputs(tmp_path: Path) -> None:
    module = load_cli()
    path = write_certified_rules(tmp_path / "rules.yaml")
    client = SimpleNamespace(project="ogilvy-trends-v2", location="US")
    calls = []

    def run_date(**kwargs):
        calls.append(kwargs)
        return outcome_result(kwargs["evaluation_run_id"], dry_run=True)

    payload = module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=False,
        client_factory=lambda **kwargs: client,
        job_runner=run_date,
        clock=lambda: NOW,
        rule_config_path=path,
        legacy_v1=True,
    )

    expected_dates = [
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
        "2026-09-03",
        "2026-09-04",
        "2026-09-05",
        "2026-09-06",
    ]
    assert [call["evaluation_date"].isoformat() for call in calls] == expected_dates
    assert [call["evaluation_run_id"] for call in calls] == [
        f"outcome_eval_v1_{value}" for value in expected_dates
    ]
    assert all(call["client"] is client for call in calls)
    assert all(call["dataset"] == "trends_v2_staging" for call in calls)
    assert all(call["client_scope_id"] == "fixture_scope" for call in calls)
    assert all(call["dry_run"] is True for call in calls)
    assert all(call["quality_findings_by_prediction"] == {} for call in calls)
    assert all(call["human_calibrations_by_prediction"] == {} for call in calls)
    assert all(call["evaluated_at"] == NOW for call in calls)
    assert payload["dates"][0]["evaluation_run_id"] == "outcome_eval_v1_2026-08-31"


def test_json_output_has_exact_fields_totals_and_no_client_content(tmp_path: Path) -> None:
    module = load_cli()
    path = write_certified_rules(tmp_path / "rules.yaml")
    marker = "CLIENT SECRET https://client.example/private author@example.com"

    payload = module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=False,
        client_factory=lambda **kwargs: SimpleNamespace(project="ogilvy-trends-v2", location="US"),
        job_runner=lambda **kwargs: outcome_result(
            kwargs["evaluation_run_id"], dry_run=True, content_marker=marker
        ),
        clock=lambda: NOW,
        rule_config_path=path,
        legacy_v1=True,
    )

    assert tuple(payload) == (
        "week_ending",
        "client_scope_id",
        "dry_run",
        "dates",
        "totals",
    )
    assert tuple(payload["dates"][0]) == (
        "evaluation_date",
        "evaluation_run_id",
        "due_predictions",
        "outcome_rows",
        "missing_predictions",
        "state_counts",
        "persistence",
    )
    assert tuple(payload["dates"][0]["state_counts"]) == tuple(STATES)
    assert payload["dates"][0]["persistence"] == {
        "validated_rows": 2,
        "inserted_rows": 0,
        "unchanged_rows": 0,
        "conflict_rows": 0,
        "cleanup_state": "not_started",
    }
    assert payload["totals"] == {
        "due_predictions": 14,
        "outcome_rows": 14,
        "missing_predictions": 7,
    }
    rendered = module.render_result(payload)
    assert rendered == json.dumps(payload, separators=(",", ":"))
    for forbidden in (
        marker,
        "client.example",
        "author@example.com",
        "pred_" + "a" * 64,
    ):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    "client",
    [
        SimpleNamespace(
            project="other-project",
            location="US",
            _credentials=ExactApplyClient._credentials,
        ),
        SimpleNamespace(
            project="ogilvy-trends-v2",
            location="EU",
            _credentials=ExactApplyClient._credentials,
        ),
        SimpleNamespace(
            project="ogilvy-trends-v2",
            location="US",
            _credentials=SimpleNamespace(
                service_account_email="wrong@example.com",
                quota_project_id=None,
            ),
        ),
        SimpleNamespace(
            project="ogilvy-trends-v2",
            location="US",
            _credentials=SimpleNamespace(
                service_account_email=(
                    "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
                ),
                quota_project_id="other-project",
            ),
        ),
    ],
)
def test_apply_refuses_nonexact_client_before_reader(tmp_path: Path, client: object) -> None:
    module = load_cli()
    path = write_certified_rules(tmp_path / "rules.yaml")
    calls = []

    with pytest.raises(module.OutcomeCliRefusal, match="exact staging"):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=True,
            client_factory=lambda **kwargs: client,
            job_runner=lambda **kwargs: calls.append(kwargs),
            clock=lambda: NOW,
            rule_config_path=path,
            legacy_v1=True,
        )

    assert calls == []


def test_apply_refreshes_default_metadata_identity_before_reader(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_cli()
    path = write_certified_rules(tmp_path / "rules.yaml")
    sentinel_request = object()

    class MetadataCredentials:
        service_account_email = "default"
        quota_project_id = None

        def refresh(self, request: object) -> None:
            assert request is sentinel_request
            self.service_account_email = module.WRITER_IDENTITY

    credentials = MetadataCredentials()
    client = SimpleNamespace(
        project="ogilvy-trends-v2",
        location="US",
        _credentials=credentials,
    )
    monkeypatch.setattr(module.persistence, "ComputeCredentials", MetadataCredentials)
    monkeypatch.setattr(module.persistence, "AuthRequest", lambda: sentinel_request)
    calls = []

    module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=True,
        client_factory=lambda **kwargs: client,
        job_runner=lambda **kwargs: (
            calls.append(kwargs) or outcome_result(kwargs["evaluation_run_id"], dry_run=False)
        ),
        clock=lambda: NOW,
        rule_config_path=path,
        legacy_v1=True,
    )

    assert credentials.service_account_email == module.WRITER_IDENTITY
    assert len(calls) == 7


def test_apply_passes_false_dry_run_to_each_date(tmp_path: Path) -> None:
    module = load_cli()
    path = write_certified_rules(tmp_path / "rules.yaml")
    dry_run_values = []

    def run_date(**kwargs):
        dry_run_values.append(kwargs["dry_run"])
        return outcome_result(kwargs["evaluation_run_id"], dry_run=False)

    payload = module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=True,
        client_factory=lambda **kwargs: ExactApplyClient(),
        job_runner=run_date,
        clock=lambda: NOW,
        rule_config_path=path,
        legacy_v1=True,
    )

    assert dry_run_values == [False] * 7
    assert payload["dry_run"] is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--client-scope-id", "fixture_scope"],
        ["--week-ending", "2026-09-06"],
        ["--week-ending", "2026-09-05", "--client-scope-id", "fixture_scope"],
        ["--week-ending", "bad", "--client-scope-id", "fixture_scope"],
        [
            "--week-ending",
            "2026-09-06",
            "--client-scope-id",
            "fixture_scope",
            "--project",
            "other",
        ],
    ],
)
def test_cli_rejects_missing_invalid_and_unapproved_arguments(argv: list[str]) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr


def test_cli_rejects_abbreviated_argument_names() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--week",
            "2026-09-06",
            "--client-scope-id",
            "fixture_scope",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "--week-ending" in completed.stderr
    assert "rule config" not in completed.stderr


def test_failed_date_stops_later_dates(tmp_path: Path) -> None:
    module = load_cli()
    path = write_certified_rules(tmp_path / "rules.yaml")
    calls = []

    def fail_second(**kwargs):
        calls.append(kwargs["evaluation_date"])
        if len(calls) == 2:
            raise RuntimeError("receipt invalid")
        return outcome_result(kwargs["evaluation_run_id"], dry_run=True)

    with pytest.raises(RuntimeError, match="receipt invalid"):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=False,
            client_factory=lambda **kwargs: SimpleNamespace(
                project="ogilvy-trends-v2", location="US"
            ),
            job_runner=fail_second,
            clock=lambda: NOW,
            rule_config_path=path,
            legacy_v1=True,
        )

    assert calls == [date(2026, 8, 31), date(2026, 9, 1)]


def test_apply_rerun_uses_reader_natural_key_and_skips_all_seven_dates(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_cli()
    path = write_certified_rules(tmp_path / "rules.yaml")
    stored_keys: set[tuple[str, date, str]] = set()
    persistence_calls = []

    def prediction_row(evaluation_date: date) -> dict[str, object]:
        index = evaluation_date.toordinal()
        prediction_id = f"pred_{index:064x}"
        signal_id = f"sig_{index:064x}"
        signal_date = evaluation_date - timedelta(days=7)
        return {
            "client_scope_id": "fixture_scope",
            "market_scope": ["za"],
            "brand_config_id": "fixture_brand",
            "audience_lens_ids": [],
            "theme_id": "fixture_theme",
            "run_id": f"prediction_{evaluation_date.isoformat()}",
            "contract_version": "2.0.0",
            "prediction_id": prediction_id,
            "signal_id": signal_id,
            "signal_date": signal_date,
            "market": "za",
            "discovery_mode": "dynamic",
            "source_families": ["reddit", "youtube"],
            "evidence_state": "ready",
            "first_seen_at": datetime.combine(
                signal_date - timedelta(days=1), datetime.min.time(), tzinfo=UTC
            ),
            "predicted_at": datetime.combine(signal_date, datetime.min.time(), tzinfo=UTC),
            "expected_trajectory": "sustained",
            "evaluation_date": evaluation_date,
            "baseline": {"velocity": 0.68, "breadth": 0.62, "evidence_family_count": 2},
            "promotion_target": {
                "velocity": 0.6,
                "breadth": 0.6,
                "evidence_family_count": 2,
            },
            "invalidation_condition": "Breadth falls below 0.60 before evaluation.",
            "cluster_build_version": "hybrid_graph_v1",
            "source_family_map_version": "channel_family_v1",
            "rule_version": "prediction_rules_v1",
            "display_eligible": False,
        }

    class QueryJob:
        def __init__(self, rows: tuple[object, ...]) -> None:
            self.rows = rows

        def result(self, *, max_results: int) -> tuple[object, ...]:
            return self.rows[:max_results]

    class StatefulClient(ExactApplyClient):
        def query(self, sql: str, *, job_config: object, location: str) -> QueryJob:
            assert location == "US"
            parameters = {parameter.name: parameter for parameter in job_config.query_parameters}
            if "signal_candidates_v2" in sql:
                return QueryJob(())
            if "signal_predictions_v2" in sql and "NOT EXISTS" in sql:
                evaluation_date = parameters["evaluation_date"].value
                run_id = parameters["evaluation_run_id"].value
                row = prediction_row(evaluation_date)
                key = (row["prediction_id"], evaluation_date, run_id)
                return QueryJob(()) if key in stored_keys else QueryJob((row,))
            if "signal_predictions_v2" in sql:
                evaluation_date = parameters["evaluation_date"].value
                return QueryJob((prediction_row(evaluation_date),))
            raise AssertionError("unexpected weekly outcome query")

    def persist_rows(**kwargs):
        persistence_calls.append(kwargs)
        batch = kwargs["batch"]
        for row in batch.outcomes:
            stored_keys.add((row["prediction_id"], row["evaluation_date"], row["run_id"]))
        counts = dict.fromkeys(module.persistence.TABLE_BINDINGS, 0)
        counts["outcomes"] = len(batch.outcomes)
        inserted = dict.fromkeys(module.persistence.TABLE_BINDINGS, 0)
        inserted["outcomes"] = len(batch.outcomes)
        return module.persistence.PersistenceResult(
            project=module.PROJECT,
            dataset=module.DATASET,
            dry_run=False,
            validated_counts=counts,
            inserted_counts=inserted,
            unchanged_counts=dict.fromkeys(module.persistence.TABLE_BINDINGS, 0),
            conflict_counts=dict.fromkeys(module.persistence.TABLE_BINDINGS, 0),
            statement_digests=dict.fromkeys(module.persistence.TABLE_BINDINGS, "a" * 64),
            cleanup_state="complete",
        )

    monkeypatch.setattr(module.persistence, "persist_open_intelligence_rows", persist_rows)
    client = StatefulClient()

    first = module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=True,
        client_factory=lambda **kwargs: client,
        job_runner=module.run_outcome_evaluation,
        clock=lambda: NOW,
        rule_config_path=path,
        legacy_v1=True,
    )
    second = module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=True,
        client_factory=lambda **kwargs: client,
        job_runner=module.run_outcome_evaluation,
        clock=lambda: NOW,
        rule_config_path=path,
        legacy_v1=True,
    )

    assert first["totals"]["due_predictions"] == 7
    assert first["totals"]["outcome_rows"] == 7
    assert len(persistence_calls) == 7
    assert second["totals"] == {
        "due_predictions": 0,
        "outcome_rows": 0,
        "missing_predictions": 0,
    }
    assert all(item["persistence"] is None for item in second["dates"])
    assert len(persistence_calls) == 7


def test_main_emits_exact_json_on_success(monkeypatch, capsys) -> None:
    module = load_cli()
    expected = {"status": "ok"}
    monkeypatch.setattr(module, "run_weekly_outcomes", lambda **kwargs: expected)

    assert module.main(["--week-ending", "2026-09-06", "--client-scope-id", "fixture_scope"]) == 0
    captured = capsys.readouterr()
    assert captured.out == '{"status":"ok"}\n'
    assert captured.err == ""


def test_main_redacts_unexpected_exception(monkeypatch, capsys) -> None:
    module = load_cli()
    secret = "private client payload"

    def fail(**kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(module, "run_weekly_outcomes", fail)

    assert module.main(["--week-ending", "2026-09-06", "--client-scope-id", "fixture_scope"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: weekly outcome job failed\n"
    assert secret not in captured.err


def write_review_records(path: Path) -> Path:
    from tests.unit.test_outcome_review_reader import document, record

    path.write_text(json.dumps(document(record())), encoding="utf-8")
    return path


def test_review_records_switch_the_caller_to_the_v2_evaluation_version(tmp_path: Path) -> None:
    module = load_cli()
    rules = write_certified_rules(tmp_path / "rules.yaml")
    records = write_review_records(tmp_path / "review_records.json")
    calls = []

    def run_date(**kwargs):
        calls.append(kwargs)
        result = outcome_result(kwargs["evaluation_run_id"], dry_run=True)
        result.evaluation_version = module.EVALUATION_VERSION_V2
        result.unreviewed_prediction_ids = ("pred_" + "b" * 64,)
        return result

    payload = module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=False,
        client_factory=lambda **kwargs: SimpleNamespace(project="ogilvy-trends-v2", location="US"),
        job_runner=run_date,
        clock=lambda: NOW,
        rule_config_path=rules,
        review_records_path=records,
    )

    assert calls[0]["evaluation_run_id"] == "outcome_eval_v2_2026-08-31"
    assert all(call["evaluation_version"] == module.EVALUATION_VERSION_V2 for call in calls)
    assert all(
        call["review_records"].contract_version == "outcome_review_records_v1" for call in calls
    )
    assert all(call["quality_findings_by_prediction"] == {} for call in calls)
    assert all(call["human_calibrations_by_prediction"] == {} for call in calls)
    assert payload["evaluation_version"] == module.EVALUATION_VERSION_V2
    assert len(payload["review_records_digest"]) == 64
    assert payload["dates"][0]["unreviewed_predictions"] == 1
    assert payload["totals"]["unreviewed_predictions"] == 7
    assert "review_records" not in json.dumps(payload["dates"])


def test_legacy_v1_never_claims_a_review_and_v2_refuses_bad_records(tmp_path: Path) -> None:
    module = load_cli()
    rules = write_certified_rules(tmp_path / "rules.yaml")
    calls = []

    def run_date(**kwargs):
        calls.append(kwargs)
        return outcome_result(kwargs["evaluation_run_id"], dry_run=True)

    payload = module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=False,
        client_factory=lambda **kwargs: SimpleNamespace(project="ogilvy-trends-v2", location="US"),
        job_runner=run_date,
        clock=lambda: NOW,
        rule_config_path=rules,
        legacy_v1=True,
    )
    assert "evaluation_version" not in payload
    assert all("review_records" not in call for call in calls)
    assert all("evaluation_version" not in call for call in calls)

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(module.OutcomeCliRefusal, match="review records"):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=False,
            client_factory=lambda **kwargs: SimpleNamespace(
                project="ogilvy-trends-v2", location="US"
            ),
            job_runner=run_date,
            clock=lambda: NOW,
            rule_config_path=rules,
            review_records_path=broken,
        )
    assert len(calls) == 7


def test_help_exposes_the_review_records_argument() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0
    assert "--review-records" in completed.stdout


def test_caller_refuses_to_run_without_review_records_unless_legacy_v1(tmp_path: Path) -> None:
    module = load_cli()
    rules = write_certified_rules(tmp_path / "rules.yaml")
    records = write_review_records(tmp_path / "review_records.json")
    calls = []
    clients = []

    def client_factory(**kwargs):
        client = SimpleNamespace(project="ogilvy-trends-v2", location="US")
        clients.append(client)
        return client

    def run_date(**kwargs):
        calls.append(kwargs)
        return outcome_result(kwargs["evaluation_run_id"], dry_run=True)

    with pytest.raises(module.OutcomeCliRefusal, match="review_records_required"):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=False,
            client_factory=client_factory,
            job_runner=run_date,
            clock=lambda: NOW,
            rule_config_path=rules,
        )
    with pytest.raises(module.OutcomeCliRefusal, match="legacy_v1_conflicts_with_review_records"):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=False,
            client_factory=client_factory,
            job_runner=run_date,
            clock=lambda: NOW,
            rule_config_path=rules,
            review_records_path=records,
            legacy_v1=True,
        )
    with pytest.raises(module.OutcomeCliRefusal, match="legacy v1 must be boolean"):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=False,
            client_factory=client_factory,
            job_runner=run_date,
            clock=lambda: NOW,
            rule_config_path=rules,
            legacy_v1="yes",
        )
    assert calls == []
    assert clients == []


def test_main_legacy_flag_prints_the_no_review_notice_and_passes_the_flag(
    monkeypatch, capsys
) -> None:
    module = load_cli()
    seen = []

    def fake_run(**kwargs):
        seen.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(module, "run_weekly_outcomes", fake_run)

    assert (
        module.main(
            ["--week-ending", "2026-09-06", "--client-scope-id", "fixture_scope", "--legacy-v1"]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == '{"status":"ok"}\n'
    assert captured.err == module.LEGACY_V1_NOTICE + "\n"
    assert "no review inputs" in module.LEGACY_V1_NOTICE
    assert seen[0]["legacy_v1"] is True
    assert seen[0]["review_records_path"] is None

    assert module.main(["--week-ending", "2026-09-06", "--client-scope-id", "fixture_scope"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert seen[1]["legacy_v1"] is False


def test_main_without_records_or_legacy_flag_refuses_by_name(monkeypatch, capsys, tmp_path) -> None:
    module = load_cli()
    rules = write_certified_rules(tmp_path / "rules.yaml")
    original = module.run_weekly_outcomes

    def run_with_certified_rules(**kwargs):
        return original(
            **kwargs,
            client_factory=lambda **_: SimpleNamespace(project="ogilvy-trends-v2", location="US"),
            clock=lambda: NOW,
            rule_config_path=rules,
        )

    monkeypatch.setattr(module, "run_weekly_outcomes", run_with_certified_rules)

    assert module.main(["--week-ending", "2026-09-06", "--client-scope-id", "fixture_scope"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: review_records_required\n"


def test_help_exposes_the_legacy_flag() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0
    assert "--legacy-v1" in completed.stdout


def measurement_document(**overrides: object) -> dict:
    families = ("news", "reddit", "youtube")
    document = {
        "contract_version": "outcome_source_measurements_v1",
        "status": "replay_certified",
        "rule_version": "outcome_source_measurement_rules_v1",
        "replay_receipt_id": "measurement_fixture_receipt",
        "approved_by": "measurement_fixture_approver",
        "approved_at": "2026-09-01T00:00:00Z",
        "expires_at": "2026-10-01T00:00:00Z",
        "minimum_due": 2,
        "source_yield_rules": {
            "efficiency_weight": 0.2,
            "unique_lift_weight": 0.1,
            "integrity_weight": 0.2,
            "geo_precision_weight": 0.15,
            "lead_time_weight": 0.1,
            "precision_weight": 0.15,
            "outcome_weight": 0.1,
            "receipts_per_credit_cap": 2.0,
            "maximum_lead_hours": 24.0,
            "minimum_integrity": 0.7,
            "minimum_geo_precision": 0.6,
            "maximum_false_discovery_rate": 0.6,
        },
        "credit_envelope": {
            "funding_math_status": "complete",
            "funded_increase_observed": True,
            "funded_increase_amount": 3000,
            "optional_funded_credits_used": 1000,
            "balance": 5000,
            "baseline_credits_per_day": 100.0,
            "runway_floor_days": 14,
            "monthly_optional_credit_cap": 2500,
            "monthly_optional_credits_used": 500,
        },
        # Deliberately above the envelope, so the envelope is what binds the
        # published total and a bound assertion on it can actually fail.
        "route_caps": {"news": 1500, "reddit": 1500, "youtube": 1500},
        "measurements": {
            family: {
                "status": "active",
                "calls": 10,
                "confirmed_credits": 20,
                "usable_receipts": 40,
                "unique_receipts": 20,
                "integrity": 0.9,
                "geo_precision": 0.8,
                "median_lead_hours": 6.0,
                "kill_test_passed": True,
            }
            for family in families
        },
    }
    document.update(overrides)
    return document


def write_measurements(path: Path, **overrides: object) -> Path:
    path.write_text(json.dumps(measurement_document(**overrides)), encoding="utf-8")
    return path


def cohort_rows(
    evaluation_date: date, run_id: str, scope: str = "fixture_scope"
) -> tuple[dict, ...]:
    return (
        {
            "prediction_id": f"pred_{evaluation_date.isoformat()}_1",
            "client_scope_id": scope,
            "run_id": run_id,
            "evaluation_date": evaluation_date,
            "outcome": "sustained",
            "source_families": ["news", "reddit"],
        },
        {
            "prediction_id": f"pred_{evaluation_date.isoformat()}_2",
            "client_scope_id": scope,
            "run_id": run_id,
            "evaluation_date": evaluation_date,
            "outcome": "unresolved",
            "source_families": ["news"],
        },
    )


def reviewed_runner(module, *, with_rows: bool = True, rows_for=None, calls=None):
    def run_date(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        result = outcome_result(kwargs["evaluation_run_id"], dry_run=True)
        result.evaluation_version = module.EVALUATION_VERSION_V2
        result.unreviewed_prediction_ids = ()
        if rows_for is not None:
            result.outcome_rows = rows_for(kwargs)
        else:
            result.outcome_rows = (
                cohort_rows(kwargs["evaluation_date"], kwargs["evaluation_run_id"])
                if with_rows
                else None
            )
        return result

    return run_date


def reviewed_week(module, tmp_path: Path, **overrides: object):
    return {
        "week_ending": date(2026, 9, 6),
        "client_scope_id": "fixture_scope",
        "apply": False,
        "client_factory": lambda **kwargs: SimpleNamespace(
            project="ogilvy-trends-v2", location="US"
        ),
        "job_runner": reviewed_runner(module),
        "clock": lambda: NOW,
        "rule_config_path": write_certified_rules(tmp_path / "rules.yaml"),
        "review_records_path": write_review_records(tmp_path / "review_records.json"),
        **overrides,
    }


def test_measurements_produce_a_bounded_allocation_fixed_to_the_requested_week(
    tmp_path: Path,
) -> None:
    module = load_cli()
    measurements = write_measurements(tmp_path / "measurements.json")

    payload = module.run_weekly_outcomes(
        **reviewed_week(module, tmp_path, measurements_path=measurements)
    )

    record = payload["measured_allocation"]
    # The digest is compared against sha256 of the file's own bytes, computed
    # here, not against the same production function over the same file.
    expected_digest = hashlib.sha256(measurements.read_bytes()).hexdigest()
    assert payload["source_measurements_digest"] == expected_digest
    assert record["measurement_document_digest"] == expected_digest
    assert record["client_scope_id"] == "fixture_scope"
    assert record["evaluation_version"] == "outcome_evaluation_v2"
    assert record["rule_version"] == "outcome_source_measurement_rules_v1"
    assert record["contract_version"] == "outcome_closed_cohort_v1"
    assert record["cohort_close"] == "2026-09-06"
    assert record["cohort"]["due"] == 14
    assert record["cohort"]["resolved"] == 7
    assert record["minimum_due"] == 5
    assert record["allocation"]["automatic_spend_enabled"] is False
    assert record["allocation"]["budget_basis"] == "measured"
    # The route caps total more than the envelope, so this bound is the
    # envelope's and an allocation that overran it would fail here.
    assert record["allocation"]["available_credits"] == 2000
    assert sum(item["credits"] for item in record["allocation"]["allocations"]) == 2000
    assert record["evaluation_run_ids"] == [
        f"outcome_eval_v2_{day.isoformat()}"
        for day in (date(2026, 8, 31) + timedelta(days=offset) for offset in range(7))
    ]
    verify_closed_cohort_record(record)
    assert record["record_digest"] == body_digest(record)
    assert (
        record["record_digest"]
        == module.run_weekly_outcomes(
            **reviewed_week(module, tmp_path, measurements_path=measurements)
        )["measured_allocation"]["record_digest"]
    )


def test_an_allocation_cannot_be_built_without_measurement_or_without_rows(tmp_path: Path) -> None:
    module = load_cli()

    with pytest.raises(module.OutcomeCliRefusal, match="source_measurements_empty"):
        module.run_weekly_outcomes(
            **reviewed_week(
                module,
                tmp_path,
                measurements_path=write_measurements(tmp_path / "empty.json", measurements={}),
            )
        )
    with pytest.raises(module.OutcomeCliRefusal, match="source_measurements_contract_invalid"):
        module.run_weekly_outcomes(
            **reviewed_week(
                module,
                tmp_path,
                measurements_path=write_measurements(
                    tmp_path / "wrong.json", contract_version="outcome_source_measurements_v9"
                ),
            )
        )
    with pytest.raises(module.OutcomeCliRefusal, match="outcome_rows_missing"):
        module.run_weekly_outcomes(
            **reviewed_week(
                module,
                tmp_path,
                measurements_path=write_measurements(tmp_path / "rowless.json"),
                job_runner=reviewed_runner(module, with_rows=False),
            )
        )
    with pytest.raises(module.OutcomeCliRefusal, match="legacy_v1_conflicts_with_measurements"):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=False,
            client_factory=lambda **kwargs: SimpleNamespace(
                project="ogilvy-trends-v2", location="US"
            ),
            job_runner=reviewed_runner(module),
            clock=lambda: NOW,
            rule_config_path=write_certified_rules(tmp_path / "legacy_rules.yaml"),
            measurements_path=write_measurements(tmp_path / "legacy.json"),
            legacy_v1=True,
        )


def test_a_week_without_measurements_carries_no_allocation_at_all(tmp_path: Path) -> None:
    module = load_cli()

    payload = module.run_weekly_outcomes(**reviewed_week(module, tmp_path))

    assert "measured_allocation" not in payload
    assert "source_measurements_digest" not in payload
    assert tuple(payload) == (
        "week_ending",
        "client_scope_id",
        "dry_run",
        "evaluation_version",
        "review_records_digest",
        "dates",
        "totals",
    )


def empty_date_result(module, run_id: str):
    return SimpleNamespace(
        evaluation_run_id=run_id,
        dry_run=True,
        due_prediction_count=0,
        outcome_row_count=0,
        missing_prediction_ids=(),
        state_counts=dict.fromkeys(STATES, 0),
        persistence_result=None,
        evaluation_version=module.EVALUATION_VERSION_V2,
        unreviewed_prediction_ids=(),
        outcome_rows=(),
    )


def test_a_cohort_that_stops_short_of_the_requested_week_still_closes_on_it(
    tmp_path: Path,
) -> None:
    module = load_cli()
    reviewed = reviewed_runner(module)

    def run_date(**kwargs):
        if kwargs["evaluation_date"] == date(2026, 9, 6):
            return empty_date_result(module, kwargs["evaluation_run_id"])
        return reviewed(**kwargs)

    payload = module.run_weekly_outcomes(
        **reviewed_week(
            module,
            tmp_path,
            measurements_path=write_measurements(tmp_path / "short.json"),
            job_runner=run_date,
        )
    )

    record = payload["measured_allocation"]
    assert record["cohort_close"] == "2026-09-06"
    assert record["evaluation_dates"][-1] == "2026-09-05"
    assert "cohort_close_not_reached" in record["gaps"]
    assert record["state"] == "incomplete"
    assert record["cohort"]["due"] == 12


def body_digest(record: dict) -> str:
    """Recompute the published digest here, from the record's own body."""
    body = {key: value for key, value in record.items() if key != "record_digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"status": "draft"}, "source_measurements_not_certified"),
        ({"status": ""}, "source_measurements_not_certified"),
        ({"rule_version": "outcome_source_measurement_rules_v9"}, "source_measurements_rule"),
        ({"replay_receipt_id": "  "}, "source_measurements_replay_receipt_id"),
        ({"approved_by": ""}, "source_measurements_approved_by"),
        ({"approved_at": "2026-09-30T00:00:00Z"}, "source_measurements_approval_in_the_future"),
        ({"expires_at": "2026-09-02T00:00:00Z"}, "source_measurements_certification_expired"),
        ({"approved_at": "not a time"}, "source_measurements_approved_at"),
        ({"expires_at": None}, "source_measurements_expires_at"),
    ],
)
def test_the_measurement_document_must_be_certified_like_the_rules_beside_it(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    """The numbers and the rules that judge them arrive under the same proof."""
    module = load_cli()

    with pytest.raises(module.OutcomeCliRefusal, match=message):
        module.run_weekly_outcomes(
            **reviewed_week(
                module,
                tmp_path,
                measurements_path=write_measurements(tmp_path / "uncertified.json", **overrides),
            )
        )


def test_a_certified_measurement_document_still_names_every_field_it_must_carry(
    tmp_path: Path,
) -> None:
    module = load_cli()

    assert module.SOURCE_MEASUREMENT_DOCUMENT_FIELDS == (
        "contract_version",
        "status",
        "rule_version",
        "replay_receipt_id",
        "approved_by",
        "approved_at",
        "expires_at",
        "minimum_due",
        "source_yield_rules",
        "credit_envelope",
        "route_caps",
        "measurements",
    )
    for dropped in module.SOURCE_MEASUREMENT_DOCUMENT_FIELDS:
        document = measurement_document()
        del document[dropped]
        path = tmp_path / f"without_{dropped}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        expected = (
            "source_measurements_contract_invalid"
            if dropped == "contract_version"
            else "source_measurements_fields_invalid"
        )
        with pytest.raises(module.OutcomeCliRefusal, match=expected):
            module.load_source_measurements(path, NOW)
    extra = measurement_document()
    extra["unexpected"] = 1
    path = tmp_path / "extra.json"
    path.write_text(json.dumps(extra), encoding="utf-8")
    with pytest.raises(module.OutcomeCliRefusal, match="source_measurements_fields_invalid"):
        module.load_source_measurements(path, NOW)


@pytest.mark.parametrize("minimum_due", [0, -1, True, 1.0, "2", None])
def test_a_minimum_due_below_one_is_never_admitted(tmp_path: Path, minimum_due: object) -> None:
    module = load_cli()
    path = write_measurements(tmp_path / "minimum.json", minimum_due=minimum_due)

    with pytest.raises(module.OutcomeCliRefusal, match="source_measurements_fields_invalid"):
        module.load_source_measurements(path, NOW)


def test_the_documents_minimum_due_is_the_one_the_record_uses(tmp_path: Path) -> None:
    module = load_cli()

    payload = module.run_weekly_outcomes(
        **reviewed_week(
            module,
            tmp_path,
            measurements_path=write_measurements(tmp_path / "raised.json", minimum_due=99),
        )
    )

    record = payload["measured_allocation"]
    assert record["minimum_due"] == 99
    assert record["cohort"]["due"] == 14
    assert "cohort_below_minimum_due" in record["gaps"]
    assert record["effectiveness_gate"] is False


def test_the_document_is_read_once_and_the_digest_is_of_the_bytes_admitted(
    tmp_path: Path, monkeypatch
) -> None:
    """Substituting the content between reads cannot separate document from digest."""
    module = load_cli()
    path = write_measurements(tmp_path / "once.json")
    admitted = path.read_bytes()
    substituted = json.dumps(measurement_document(minimum_due=11)).encode("utf-8")
    assert substituted != admitted
    reads: list[bytes] = []
    real_read = Path.read_bytes
    real_write = Path.write_bytes

    def counting(self: Path) -> bytes:
        data = real_read(self)
        if Path(self) == path:
            reads.append(data)
            real_write(path, substituted)
        return data

    monkeypatch.setattr(Path, "read_bytes", counting)
    bundle = module.load_source_measurements(path, NOW)

    assert len(reads) == 1
    assert bundle.minimum_due == 2
    assert bundle.document_digest == hashlib.sha256(admitted).hexdigest()


def test_the_same_values_written_two_ways_are_two_documents(tmp_path: Path) -> None:
    module = load_cli()
    compact = write_measurements(tmp_path / "compact.json")
    spaced = tmp_path / "spaced.json"
    spaced.write_text(json.dumps(measurement_document(), indent=2), encoding="utf-8")

    assert module.load_source_measurements(spaced, NOW).minimum_due == 2
    assert module.source_measurements_digest(spaced) != module.source_measurements_digest(compact)
    assert (
        module.source_measurements_digest(compact)
        == hashlib.sha256(compact.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda row, day, run_id: {**row, "run_id": "outcome_eval_v2_2020-01-05"},
            "evaluation_run_id_unexpected",
        ),
        (lambda row, day, run_id: {**row, "run_id": "   "}, "evaluation_run_id_required"),
        (
            lambda row, day, run_id: {**row, "client_scope_id": "another_client"},
            "client_scope_mismatch",
        ),
        (
            lambda row, day, run_id: {k: v for k, v in row.items() if k != "client_scope_id"},
            "client_scope_required",
        ),
        (
            lambda row, day, run_id: {**row, "evaluation_date": day + timedelta(days=1)},
            "outcome_row_date_unexpected",
        ),
    ],
)
def test_a_row_the_run_did_not_write_never_reaches_the_record(
    tmp_path: Path, mutate, message: str
) -> None:
    """Provenance is checked against what the caller asked for, not against the rows."""
    module = load_cli()

    def rows_for(kwargs: dict) -> tuple[dict, ...]:
        day = kwargs["evaluation_date"]
        run_id = kwargs["evaluation_run_id"]
        rows = cohort_rows(day, run_id)
        if day != date(2026, 9, 6):
            return rows
        return (mutate(rows[0], day, run_id), *rows[1:])

    with pytest.raises(module.OutcomeCliRefusal, match=message):
        module.run_weekly_outcomes(
            **reviewed_week(
                module,
                tmp_path,
                measurements_path=write_measurements(tmp_path / "provenance.json"),
                job_runner=reviewed_runner(module, rows_for=rows_for),
            )
        )


def test_absent_rows_are_refused_while_a_day_that_wrote_none_is_admitted(tmp_path: Path) -> None:
    module = load_cli()

    with pytest.raises(module.OutcomeCliRefusal, match="outcome_rows_missing"):
        module.run_weekly_outcomes(
            **reviewed_week(
                module,
                tmp_path,
                measurements_path=write_measurements(tmp_path / "absent.json"),
                job_runner=reviewed_runner(module, with_rows=False),
            )
        )


def test_rows_are_carried_only_when_the_measurement_path_is_requested(tmp_path: Path) -> None:
    module = load_cli()
    with_measurements: list[dict] = []
    without: list[dict] = []

    module.run_weekly_outcomes(
        **reviewed_week(
            module,
            tmp_path,
            measurements_path=write_measurements(tmp_path / "carry.json"),
            job_runner=reviewed_runner(module, calls=with_measurements),
        )
    )
    module.run_weekly_outcomes(
        **reviewed_week(module, tmp_path, job_runner=reviewed_runner(module, calls=without))
    )

    assert len(with_measurements) == 7
    assert all(call["carry_outcome_rows"] is True for call in with_measurements)
    assert len(without) == 7
    assert all("carry_outcome_rows" not in call for call in without)


def real_predictions(evaluation_date: date, index_base: int) -> tuple[dict, ...]:
    from tests.unit.test_outcome_reader import prediction_row

    delta = (evaluation_date - date(2026, 9, 1)).days
    return tuple(
        prediction_row(
            prediction_id=f"pred_{index_base + offset:064x}",
            signal_id=f"sig_{index_base + offset:064x}",
            signal_date=date(2026, 8, 25) + timedelta(days=delta),
            first_seen_at=datetime(2026, 8, 22, 11, 0, tzinfo=UTC) + timedelta(days=delta),
            predicted_at=datetime(2026, 8, 25, 6, 50, tzinfo=UTC) + timedelta(days=delta),
            evaluation_date=evaluation_date,
            source_families=families,
        )
        for offset, families in enumerate((["news", "reddit"], ["reddit", "youtube"]))
    )


def test_the_real_evaluator_rows_reach_the_measured_allocation(tmp_path: Path, monkeypatch) -> None:
    """No injected runner: the default orchestrator carries the rows it wrote."""
    from types import MappingProxyType

    from src.analysis.open_intelligence import outcome_job, outcome_reader, outcomes, persistence

    from tests.unit.test_outcome_review_reader import document, record

    module = load_cli()
    week = tuple(date(2026, 8, 31) + timedelta(days=offset) for offset in range(7))
    predictions_by_day = {
        day: real_predictions(day, 1 + 2 * index) for index, day in enumerate(week)
    }

    def observations(day: date) -> tuple:
        delta = (day - date(2026, 9, 1)).days
        return tuple(
            outcomes.OutcomeObservation(
                observed_at=datetime(2026, 8, 26, tzinfo=UTC) + timedelta(days=delta + offset),
                velocity=0.62,
                breadth=0.6,
                evidence_family_count=2,
            )
            for offset in range(7)
        )

    def fake_read(**kwargs):
        rows = predictions_by_day[kwargs["evaluation_date"]]
        return outcome_reader.OutcomeReadResult(
            predictions=rows,
            observations_by_prediction=MappingProxyType(
                {row["prediction_id"]: observations(kwargs["evaluation_date"]) for row in rows}
            ),
            missing_prediction_ids=(),
        )

    counts = dict.fromkeys(persistence.TABLE_BINDINGS, 0)
    receipt = persistence.PersistenceResult(
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging",
        dry_run=True,
        validated_counts={**counts, "outcomes": 2},
        inserted_counts=dict(counts),
        unchanged_counts=dict(counts),
        conflict_counts=dict(counts),
        statement_digests=dict.fromkeys(persistence.TABLE_BINDINGS, "a" * 64),
        cleanup_state="not_started",
    )
    monkeypatch.setattr(outcome_job, "read_due_outcome_inputs", fake_read)
    monkeypatch.setattr(
        outcome_job.persistence, "persist_open_intelligence_rows", lambda **kwargs: receipt
    )

    reviews = tmp_path / "full_review_records.json"
    reviews.write_text(
        json.dumps(
            document(
                *(
                    record(
                        prediction_id=row["prediction_id"],
                        evaluation_date=day.isoformat(),
                        quality_findings=[],
                        human_calibration=None,
                    )
                    for day, rows in predictions_by_day.items()
                    for row in rows
                )
            )
        ),
        encoding="utf-8",
    )

    payload = module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=False,
        client_factory=lambda **kwargs: SimpleNamespace(project="ogilvy-trends-v2", location="US"),
        clock=lambda: NOW,
        rule_config_path=write_certified_rules(tmp_path / "real_rules.yaml"),
        review_records_path=reviews,
        measurements_path=write_measurements(tmp_path / "real_measurements.json"),
    )

    record_payload = payload["measured_allocation"]
    assert record_payload["cohort"]["due"] == 14
    assert record_payload["cohort"]["resolved"] == 14
    assert record_payload["cohort"]["conditional_fdr"] == 0.0
    assert record_payload["client_scope_id"] == "fixture_scope"
    assert record_payload["evaluation_dates"] == [day.isoformat() for day in week]
    assert set(record_payload["by_source_family"]) == {"news", "reddit", "youtube"}
    assert record_payload["state"] == "complete"
    assert record_payload["gaps"] == []
    assert record_payload["allocation"]["unmeasured_endpoint_ids"] == []
    assert sum(item["credits"] for item in record_payload["allocation"]["allocations"]) == 2000
    verify_closed_cohort_record(record_payload)
    assert record_payload["record_digest"] == closed_cohort_record_digest(
        {key: value for key, value in record_payload.items() if key != "record_digest"}
    )


def carried(module, rows, **overrides: object):
    values = {
        "evaluation_run_id": "outcome_eval_v2_2026-09-06",
        "evaluation_date": date(2026, 9, 6),
        "client_scope_id": "fixture_scope",
    }
    values.update(overrides)
    result = SimpleNamespace(
        outcome_rows=rows, outcome_row_count=None if rows is None else len(rows)
    )
    return module._carried_outcome_rows(result, **values)


def test_the_carried_rows_are_checked_against_what_this_day_asked_for(tmp_path: Path) -> None:
    """The guard stands on its own: another day's run of the same week is still another run."""
    module = load_cli()
    rows = cohort_rows(date(2026, 9, 6), "outcome_eval_v2_2026-09-06")

    assert carried(module, rows) == rows

    # A run identifier from the day before is a real identifier of this same
    # week, so only this day's own guard can refuse it.
    with pytest.raises(module.OutcomeCliRefusal, match="evaluation_run_id_unexpected"):
        carried(module, cohort_rows(date(2026, 9, 6), "outcome_eval_v2_2026-09-05"))
    with pytest.raises(module.OutcomeCliRefusal, match="evaluation_run_id_required"):
        carried(module, ({**rows[0], "run_id": " \t "},))
    with pytest.raises(module.OutcomeCliRefusal, match="client_scope_mismatch"):
        carried(module, cohort_rows(date(2026, 9, 6), "outcome_eval_v2_2026-09-06", "other"))
    with pytest.raises(module.OutcomeCliRefusal, match="client_scope_required"):
        carried(
            module, ({key: value for key, value in rows[0].items() if key != "client_scope_id"},)
        )
    with pytest.raises(module.OutcomeCliRefusal, match="outcome_row_date_unexpected"):
        carried(module, ({**rows[0], "evaluation_date": date(2026, 9, 5)},))
    with pytest.raises(module.OutcomeCliRefusal, match="outcome_row_date_unexpected"):
        carried(module, ({**rows[0], "evaluation_date": "not a day"},))
    with pytest.raises(module.OutcomeCliRefusal, match="outcome_rows_missing"):
        carried(module, None)


def test_the_command_line_refuses_a_record_whose_digest_does_not_verify(
    tmp_path: Path, monkeypatch
) -> None:
    """The publication gate is what catches a record that moved after it was built."""
    module = load_cli()
    built = module.closed_cohort_record

    def moved(*args: object, **kwargs: object) -> dict:
        record = built(*args, **kwargs)
        record["cohort"] = {**record["cohort"], "due": 99}
        return record

    monkeypatch.setattr(module, "closed_cohort_record", moved)

    with pytest.raises(module.OutcomeCliRefusal, match="closed_cohort_record_digest_mismatch"):
        module.run_weekly_outcomes(
            **reviewed_week(
                module,
                tmp_path,
                measurements_path=write_measurements(tmp_path / "moved.json"),
            )
        )


GRANTED_AT = NOW - timedelta(hours=1)


class BoundClient(ExactApplyClient):
    """A staging client whose outcome table is the store the fake runner writes to."""

    def __init__(self, store: list, *, readback=None) -> None:
        self.store = store
        self.readback = readback
        self.queries: list[str] = []

    def query(self, sql: str, *, job_config: object, location: str):
        assert location == "US"
        self.queries.append(sql)
        params = {parameter.name: parameter for parameter in job_config.query_parameters}
        scope = params["client_scope_id"].value
        runs = set(params["evaluation_run_ids"].values)
        rows = [
            row for row in self.store if row["client_scope_id"] == scope and row["run_id"] in runs
        ]
        # The filter models a corrupted readback of written rows; the pre-write
        # store check reads an empty cohort and is left as it is.
        if self.readback is not None and rows:
            rows = self.readback(rows)
        return SimpleNamespace(result=lambda *, max_results: tuple(rows[:max_results]))


def bound_runner(module, store: list, *, calls=None, rows_for=None):
    def run_date(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        dry = kwargs["dry_run"]
        result = outcome_result(kwargs["evaluation_run_id"], dry_run=dry)
        result.evaluation_version = module.EVALUATION_VERSION_V2
        result.unreviewed_prediction_ids = ()
        rows = (
            rows_for(kwargs)
            if rows_for is not None
            else cohort_rows(kwargs["evaluation_date"], kwargs["evaluation_run_id"])
        )
        verified = kwargs.get("verified_result")
        if not dry and verified is not None:
            rows = verified.outcome_rows
        result.outcome_rows = rows
        if not dry:
            store.extend(dict(row) for row in rows)
        return result

    return run_date


def consumed_name(receipt_digest: str, grant_id: str = "grant_fixture_001") -> str:
    return f"consumed-{grant_id}-{receipt_digest[:16]}.json"


def closed_week(module, tmp_path: Path, store: list, **overrides: object) -> dict:
    measurements = tmp_path / "bound_measurements.json"
    if not measurements.exists():
        write_measurements(measurements)
    clients: list = overrides.pop("clients", [])
    readback_filter = overrides.pop("readback_filter", None)

    def factory(**kwargs):
        client = BoundClient(store, readback=readback_filter)
        clients.append(client)
        return client

    return {
        **reviewed_week(module, tmp_path),
        "client_factory": factory,
        "job_runner": bound_runner(module, store),
        "measurements_path": measurements,
        **overrides,
    }


def write_receipt(tmp_path: Path, receipt: dict, name: str = "receipt.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def write_grant(tmp_path: Path, receipt_digest: str, name: str = "grant.json", **overrides) -> Path:
    grant = {
        "grant_id": "grant_fixture_001",
        "dry_run_receipt_sha256": receipt_digest,
        "granted_at": GRANTED_AT.isoformat(),
        "expires_at": (GRANTED_AT + timedelta(hours=8)).isoformat(),
        "grantor": "grant_fixture_grantor",
    }
    grant.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(grant), encoding="utf-8")
    return path


def dry_receipt(module, tmp_path: Path) -> dict:
    return module.run_weekly_outcomes(**closed_week(module, tmp_path, []))["dry_run_receipt"]


def authority(module, tmp_path: Path, **grant_overrides) -> dict:
    receipt = dry_receipt(module, tmp_path)
    return {
        "dry_run_receipt_path": write_receipt(tmp_path, receipt),
        "write_grant_path": write_grant(tmp_path, receipt["receipt_digest"], **grant_overrides),
        "consumed_grant_path": tmp_path / "consumed" / consumed_name(receipt["receipt_digest"]),
        "allocation_out_path": tmp_path / "allocation.json",
    }


def test_closed_cohort_dry_run_emits_a_canonical_receipt(tmp_path: Path) -> None:
    module = load_cli()

    payload = module.run_weekly_outcomes(**closed_week(module, tmp_path, []))

    receipt = payload["dry_run_receipt"]
    body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert receipt["receipt_digest"] == hashlib.sha256(canonical).hexdigest()
    assert receipt["contract_version"] == "outcome_dry_run_receipt_v1"
    assert receipt["week_ending"] == "2026-09-06"
    assert receipt["client_scope_id"] == "fixture_scope"
    assert receipt["evaluation_run_ids"] == [
        f"outcome_eval_v2_{(date(2026, 8, 31) + timedelta(days=offset)).isoformat()}"
        for offset in range(7)
    ]
    assert receipt["dates"][0] == {
        "evaluation_date": "2026-08-31",
        "evaluation_run_id": "outcome_eval_v2_2026-08-31",
        "due": 2,
        "resolved": 1,
        "unresolved": 1,
        "state_counts": STATES,
    }
    assert len(receipt["prediction_ids"]) == 14
    assert receipt["prediction_ids"] == sorted(receipt["prediction_ids"])
    assert receipt["review_records_digest"] == payload["review_records_digest"]
    assert receipt["source_measurements_digest"] == payload["source_measurements_digest"]
    again = module.run_weekly_outcomes(**closed_week(module, tmp_path, []))
    assert again["dry_run_receipt"] == receipt


def test_bound_apply_recomputes_consumes_writes_reads_back_and_keeps_the_allocation(
    tmp_path: Path,
) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    store: list = []
    calls: list = []
    clients: list = []
    args = closed_week(
        module, tmp_path, store, clients=clients, job_runner=None, apply=True, **bound
    )
    args["job_runner"] = bound_runner(module, store, calls=calls)

    payload = module.run_weekly_outcomes(**args)

    assert [call["dry_run"] for call in calls] == [True] * 7 + [False] * 7
    assert len(clients) == 2
    assert calls[0]["client"] is clients[0]
    assert calls[7]["client"] is clients[1]
    assert len(store) == 14
    assert payload["dry_run"] is False
    consumed = json.loads(bound["consumed_grant_path"].read_text(encoding="utf-8"))
    receipt = json.loads(bound["dry_run_receipt_path"].read_text(encoding="utf-8"))
    assert consumed["grant_id"] == "grant_fixture_001"
    assert consumed["dry_run_receipt_sha256"] == receipt["receipt_digest"]
    assert payload["write_authority"] == {
        "grant_id": "grant_fixture_001",
        "dry_run_receipt_sha256": receipt["receipt_digest"],
    }
    record = payload["measured_allocation"]
    assert payload["readback"] == {
        "due": 14,
        "resolved": 7,
        "unresolved": 7,
        "allocation_proposal_digest": record["record_digest"],
    }
    assert "signal_outcomes_v2" in clients[1].queries[0]
    kept = json.loads(bound["allocation_out_path"].read_text(encoding="utf-8"))
    verify_closed_cohort_record(kept)
    assert kept == record
    tampered = tmp_path / "tampered.json"
    tampered.write_text(
        json.dumps({**kept, "cohort": {**kept["cohort"], "unresolved": 0}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="closed_cohort_record_digest_mismatch"):
        verify_closed_cohort_record(json.loads(tampered.read_text(encoding="utf-8")))


def test_a_second_apply_with_the_same_consumed_path_refuses(tmp_path: Path) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    store: list = []
    module.run_weekly_outcomes(**closed_week(module, tmp_path, store, apply=True, **bound))
    written = len(store)
    bound["allocation_out_path"] = tmp_path / "allocation_second.json"

    with pytest.raises(module.OutcomeCliRefusal, match="write_grant_consumed"):
        module.run_weekly_outcomes(**closed_week(module, tmp_path, store, apply=True, **bound))
    assert len(store) == written


def test_apply_refuses_when_live_reads_no_longer_match_the_receipt(tmp_path: Path) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    store: list = []
    calls: list = []
    clients: list = []

    def rows_for(kwargs: dict) -> tuple[dict, ...]:
        rows = cohort_rows(kwargs["evaluation_date"], kwargs["evaluation_run_id"])
        return ({**rows[0], "outcome": "noise"}, rows[1])

    args = closed_week(module, tmp_path, store, clients=clients, apply=True, **bound)
    args["job_runner"] = bound_runner(module, store, calls=calls, rows_for=rows_for)
    with pytest.raises(module.OutcomeCliRefusal, match="dry_run_receipt_mismatch"):
        module.run_weekly_outcomes(**args)
    assert all(call["dry_run"] is True for call in calls)
    assert len(clients) == 1
    assert store == []
    assert not bound["consumed_grant_path"].exists()


def test_apply_refuses_a_receipt_the_grant_does_not_name_or_that_was_edited(
    tmp_path: Path,
) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    receipt = json.loads(bound["dry_run_receipt_path"].read_text(encoding="utf-8"))
    edited = write_receipt(tmp_path, {**receipt, "prediction_ids": []}, "edited.json")
    other = write_grant(tmp_path, "b" * 64, "other_grant.json")
    for overrides in (
        {"dry_run_receipt_path": edited},
        {"write_grant_path": other},
    ):
        calls: list = []
        with pytest.raises(module.OutcomeCliRefusal, match="dry_run_receipt_mismatch"):
            module.run_weekly_outcomes(
                **closed_week(
                    module,
                    tmp_path,
                    [],
                    apply=True,
                    clients=calls,
                    **{**bound, **overrides},
                )
            )
        assert calls == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"expires_at": (NOW - timedelta(seconds=1)).isoformat()}, "write_grant_expired"),
        (
            {
                "granted_at": (NOW + timedelta(minutes=5)).isoformat(),
                "expires_at": (NOW + timedelta(hours=2)).isoformat(),
            },
            "write_grant_expired",
        ),
        (
            {"expires_at": (GRANTED_AT + timedelta(hours=8, seconds=1)).isoformat()},
            "write_grant_invalid",
        ),
        ({"granted_at": "2026-09-07T04:00:00"}, "write_grant_invalid"),
        ({"expires_at": GRANTED_AT.isoformat()}, "write_grant_invalid"),
        ({"grant_id": " "}, "write_grant_invalid"),
        ({"grantor": None}, "write_grant_invalid"),
        ({"dry_run_receipt_sha256": "not a digest"}, "write_grant_invalid"),
        ({"extra": "field"}, "write_grant_invalid"),
    ],
)
def test_apply_refuses_expired_future_and_malformed_grants(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    module = load_cli()
    bound = authority(module, tmp_path, **overrides)
    clients: list = []

    with pytest.raises(module.OutcomeCliRefusal, match=message):
        module.run_weekly_outcomes(
            **closed_week(module, tmp_path, [], apply=True, clients=clients, **bound)
        )
    assert clients == []
    assert not bound["consumed_grant_path"].exists()


def test_an_unreadable_grant_is_invalid(tmp_path: Path) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    bound["write_grant_path"].write_text("{", encoding="utf-8")

    with pytest.raises(module.OutcomeCliRefusal, match="write_grant_invalid"):
        module.run_weekly_outcomes(**closed_week(module, tmp_path, [], apply=True, **bound))


@pytest.mark.parametrize(
    ("dropped", "message"),
    [
        ("dry_run_receipt_path", "dry_run_receipt_required"),
        ("write_grant_path", "write_grant_required"),
        ("consumed_grant_path", "consumed_grant_path_required"),
        ("allocation_out_path", "allocation_out_required"),
    ],
)
def test_a_reviewed_apply_requires_every_piece_of_write_authority(
    tmp_path: Path, dropped: str, message: str
) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    del bound[dropped]
    clients: list = []

    with pytest.raises(module.OutcomeCliRefusal, match=message):
        module.run_weekly_outcomes(
            **closed_week(module, tmp_path, [], apply=True, clients=clients, **bound)
        )
    assert clients == []


def test_write_authority_is_refused_outside_a_closed_cohort_apply(tmp_path: Path) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)

    with pytest.raises(module.OutcomeCliRefusal, match="write_authority_requires_apply"):
        module.run_weekly_outcomes(**closed_week(module, tmp_path, [], **bound))
    args = closed_week(module, tmp_path, [], apply=True, **bound)
    del args["measurements_path"]
    with pytest.raises(module.OutcomeCliRefusal, match="closed_cohort_measurements_required"):
        module.run_weekly_outcomes(**args)
    with pytest.raises(module.OutcomeCliRefusal, match="legacy_v1_conflicts_with_write_grant"):
        module.run_weekly_outcomes(
            week_ending=date(2026, 9, 6),
            client_scope_id="fixture_scope",
            apply=True,
            client_factory=lambda **kwargs: ExactApplyClient(),
            job_runner=lambda **kwargs: None,
            clock=lambda: NOW,
            rule_config_path=write_certified_rules(tmp_path / "legacy_rules.yaml"),
            legacy_v1=True,
            **bound,
        )


def test_an_existing_consumed_path_or_allocation_out_refuses_before_any_client(
    tmp_path: Path,
) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    bound["allocation_out_path"].write_text("{}", encoding="utf-8")
    clients: list = []
    with pytest.raises(module.OutcomeCliRefusal, match="allocation_out_exists"):
        module.run_weekly_outcomes(
            **closed_week(module, tmp_path, [], apply=True, clients=clients, **bound)
        )
    bound["allocation_out_path"] = tmp_path / "fresh_allocation.json"
    bound["consumed_grant_path"].parent.mkdir(parents=True, exist_ok=True)
    bound["consumed_grant_path"].write_text("{}", encoding="utf-8")
    with pytest.raises(module.OutcomeCliRefusal, match="write_grant_consumed"):
        module.run_weekly_outcomes(
            **closed_week(module, tmp_path, [], apply=True, clients=clients, **bound)
        )
    assert clients == []


@pytest.mark.parametrize(
    "readback_filter",
    [
        lambda rows: rows[:-1],
        lambda rows: [{**rows[0], "outcome": "unresolved"}, *rows[1:]],
    ],
)
def test_a_readback_that_differs_from_the_write_refuses(tmp_path: Path, readback_filter) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    store: list = []

    with pytest.raises(module.OutcomeCliRefusal, match="readback_mismatch"):
        module.run_weekly_outcomes(
            **closed_week(
                module, tmp_path, store, apply=True, readback_filter=readback_filter, **bound
            )
        )
    assert len(store) == 14
    assert bound["consumed_grant_path"].exists()
    assert not bound["allocation_out_path"].exists()


def test_v2_passes_no_external_review_inputs_and_only_v1_passes_empty_maps(
    tmp_path: Path,
) -> None:
    from types import MappingProxyType

    module = load_cli()
    v2_calls: list = []
    module.run_weekly_outcomes(
        **reviewed_week(module, tmp_path, job_runner=reviewed_runner(module, calls=v2_calls))
    )
    for call in v2_calls:
        assert call["quality_findings_by_prediction"] is module.NO_EXTERNAL_REVIEW_INPUTS
        assert call["human_calibrations_by_prediction"] is module.NO_EXTERNAL_REVIEW_INPUTS
    assert isinstance(module.NO_EXTERNAL_REVIEW_INPUTS, MappingProxyType)
    assert len(module.NO_EXTERNAL_REVIEW_INPUTS) == 0
    v1_calls: list = []
    module.run_weekly_outcomes(
        week_ending=date(2026, 9, 6),
        client_scope_id="fixture_scope",
        apply=False,
        client_factory=lambda **kwargs: SimpleNamespace(project="ogilvy-trends-v2", location="US"),
        job_runner=lambda **kwargs: (
            v1_calls.append(kwargs) or outcome_result(kwargs["evaluation_run_id"], dry_run=True)
        ),
        clock=lambda: NOW,
        rule_config_path=write_certified_rules(tmp_path / "v1_rules.yaml"),
        legacy_v1=True,
    )
    for call in v1_calls:
        assert call["quality_findings_by_prediction"] is not module.NO_EXTERNAL_REVIEW_INPUTS
        assert type(call["quality_findings_by_prediction"]) is dict


def test_main_apply_requires_a_receipt_and_a_grant(monkeypatch, capsys) -> None:
    module = load_cli()
    seen: list = []
    monkeypatch.setattr(module, "run_weekly_outcomes", lambda **kwargs: seen.append(kwargs))
    base = ["--week-ending", "2026-09-06", "--client-scope-id", "fixture_scope", "--apply"]

    assert module.main(base) == 1
    assert capsys.readouterr().err == "error: dry_run_receipt_required\n"
    assert module.main([*base, "--dry-run-receipt", "r.json"]) == 1
    assert capsys.readouterr().err == "error: write_grant_required\n"
    assert seen == []


def test_main_passes_every_write_authority_path(monkeypatch, capsys) -> None:
    module = load_cli()
    seen: list = []

    def fake_run(**kwargs):
        seen.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(module, "run_weekly_outcomes", fake_run)
    argv = [
        "--week-ending", "2026-09-06", "--client-scope-id", "fixture_scope", "--apply",
        "--review-records", "records.json", "--measurements", "m.json",
        "--dry-run-receipt", "r.json", "--write-grant", "g.json",
        "--consumed-grant-path", "c.json", "--allocation-out", "a.json",
    ]  # fmt: skip

    assert module.main(argv) == 0
    assert seen[0]["dry_run_receipt_path"] == Path("r.json")
    assert seen[0]["write_grant_path"] == Path("g.json")
    assert seen[0]["consumed_grant_path"] == Path("c.json")
    assert seen[0]["allocation_out_path"] == Path("a.json")


def test_the_same_grant_under_a_new_consumed_path_refuses(tmp_path: Path) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    digest = json.loads(bound["dry_run_receipt_path"].read_text(encoding="utf-8"))["receipt_digest"]
    assert bound["consumed_grant_path"].name == consumed_name(digest)
    store: list = []
    module.run_weekly_outcomes(**closed_week(module, tmp_path, store, apply=True, **bound))
    written = len(store)
    for second in (
        tmp_path / "consumed" / "another_marker.json",
        tmp_path / "elsewhere" / "grant_fixture_001.json",
        tmp_path / "consumed" / consumed_name("c" * 64),
        tmp_path / "consumed" / consumed_name(digest, "grant_fixture_002"),
    ):
        clients: list = []
        again = {
            **bound,
            "consumed_grant_path": second,
            "allocation_out_path": tmp_path / f"allocation_{second.parent.name}.json",
        }
        with pytest.raises(module.OutcomeCliRefusal, match="write_grant_consumed"):
            module.run_weekly_outcomes(
                **closed_week(module, tmp_path, store, apply=True, clients=clients, **again)
            )
        assert clients == []
        assert not second.exists()
    assert len(store) == written


def test_the_consumed_marker_name_is_derived_from_the_grant_and_its_receipt(
    tmp_path: Path,
) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    grant = module.load_write_grant(bound["write_grant_path"], NOW)
    digest = grant.dry_run_receipt_sha256

    assert module.consumed_marker_name(grant) == consumed_name(digest)
    unsafe = write_grant(tmp_path, digest, "unsafe_grant.json", grant_id="../grant")
    with pytest.raises(module.OutcomeCliRefusal, match="write_grant_invalid"):
        module.load_write_grant(unsafe, NOW)


def test_the_consumed_marker_exists_before_the_first_write(tmp_path: Path) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    store: list = []
    seen: list = []
    runner = bound_runner(module, store)

    def observed(**kwargs):
        if not kwargs["dry_run"]:
            seen.append(bound["consumed_grant_path"].exists())
        return runner(**kwargs)

    module.run_weekly_outcomes(
        **closed_week(module, tmp_path, store, apply=True, job_runner=observed, **bound)
    )
    assert len(seen) == 7
    assert seen[0] is True
    assert all(seen)


def test_the_write_persists_the_rows_the_receipt_check_verified(tmp_path: Path) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    store: list = []
    calls: list = []
    checked: dict = {}

    def drifted(kwargs: dict) -> tuple[dict, ...]:
        rows = cohort_rows(kwargs["evaluation_date"], kwargs["evaluation_run_id"])
        if kwargs["dry_run"]:
            return rows
        return ({**rows[0], "outcome": "noise"}, rows[1])

    runner = bound_runner(module, store, calls=calls, rows_for=drifted)

    def recording(**kwargs):
        result = runner(**kwargs)
        if kwargs["dry_run"]:
            checked[kwargs["evaluation_run_id"]] = result
        return result

    payload = module.run_weekly_outcomes(
        **closed_week(module, tmp_path, store, apply=True, job_runner=recording, **bound)
    )

    writes = [call for call in calls if not call["dry_run"]]
    assert len(writes) == 7
    for call in writes:
        assert call["verified_result"] is checked[call["evaluation_run_id"]]
    expected = [dict(row) for result in checked.values() for row in result.outcome_rows]
    assert store == expected
    assert all(row["outcome"] != "noise" for row in store)
    assert payload["readback"]["due"] == 14


def test_a_write_that_does_not_persist_the_verified_rows_refuses(tmp_path: Path) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    store: list = []

    def run_date(**kwargs):
        kwargs = {key: value for key, value in kwargs.items() if key != "verified_result"}
        rows = cohort_rows(kwargs["evaluation_date"], kwargs["evaluation_run_id"])
        if not kwargs["dry_run"]:
            rows = ({**rows[0], "source_families": ["news"]}, rows[1])
        return bound_runner(module, store, rows_for=lambda _: rows)(**kwargs)

    with pytest.raises(module.OutcomeCliRefusal, match="write_rows_unverified"):
        module.run_weekly_outcomes(
            **closed_week(module, tmp_path, store, apply=True, job_runner=run_date, **bound)
        )
    assert not bound["allocation_out_path"].exists()


def test_the_readback_is_bounded_to_the_cohort_week(tmp_path: Path) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    store: list = []
    seen: list = []

    def readback(**kwargs):
        seen.append(kwargs)
        return tuple(store)

    module.run_weekly_outcomes(
        **closed_week(module, tmp_path, store, apply=True, outcome_readback=readback, **bound)
    )
    assert seen[0]["evaluation_date_from"] == date(2026, 8, 31)
    assert seen[0]["evaluation_date_to"] == date(2026, 9, 6)


def test_the_same_grant_and_marker_name_in_another_directory_refuses_on_the_store(
    tmp_path: Path,
) -> None:
    module = load_cli()
    bound = authority(module, tmp_path)
    store: list = []
    module.run_weekly_outcomes(**closed_week(module, tmp_path, store, apply=True, **bound))
    written = [dict(row) for row in store]
    second = tmp_path / "other_dir" / bound["consumed_grant_path"].name
    again = {
        **bound,
        "consumed_grant_path": second,
        "allocation_out_path": tmp_path / "allocation_other.json",
    }
    with pytest.raises(module.OutcomeCliRefusal) as refusal:
        module.run_weekly_outcomes(**closed_week(module, tmp_path, store, apply=True, **again))
    assert str(refusal.value) == "write_grant_consumed_in_store"
    assert store == written
    assert not second.exists()
    assert not again["allocation_out_path"].exists()
