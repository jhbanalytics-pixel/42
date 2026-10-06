"""Contract tests for the QA-only 42 semantic canary runner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import persistence, run_receipts

SCRIPT_PATH = Path("scripts/staging/run_42_semantic_canaries.py")
PERSISTENCE_CANARY = Path("scripts/staging/canary_open_intelligence_persistence.py")
PERSISTENCE_CANARY_SHA256 = "e2a66782e7a195504d4be4eb3fe771a389d8878d76bdf4407cff7f9e5b4d8bd6"
RUN_DATE = date(2026, 9, 14)
QA_DATASET = "trends_v2_staging_qa"
QA_IDENTITY = "intelligence-42-qa@ogilvy-trends-v2.iam.gserviceaccount.com"
IDENTIFIER = re.compile(r"(?:sig|ev|mem|pred|out)_[0-9a-f]{64}")
MANIFEST = {
    "identities": {
        "qa": f"//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/{QA_IDENTITY}"
    },
    "resources": [
        {
            "actions": ["read", "write"],
            "name": f"//bigquery.googleapis.com/projects/ogilvy-trends-v2/datasets/{QA_DATASET}",
        }
    ],
}
EXPECTED_STATES = {
    "coherent": ("ready", ()),
    "thin": ("thin", ("insufficient_qualifying_families",)),
    "opposing": ("contradictory", ("opposing_family_directions",)),
    "foreign": ("thin", ("insufficient_qualifying_families", "weak_geo_evidence")),
    "duplicate": ("rejected", ("duplicate row id",)),
    "demographic": ("rejected", ("unsupported demographic language",)),
    "causal": ("rejected", ("unsupported causal language",)),
}


def _module():
    assert SCRIPT_PATH.is_file(), "RED: the semantic canary entry point has not been created"
    spec = importlib.util.spec_from_file_location("semantic_canaries", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Client:
    """A BigQuery client fake that records every call it receives."""

    def __init__(self, identity: str = QA_IDENTITY, errors: list | None = None) -> None:
        self.project = "ogilvy-trends-v2"
        self.location = "US"
        self._credentials = SimpleNamespace(service_account_email=identity, quota_project_id=None)
        self.appends: list[tuple[str, list[dict]]] = []
        self.other_calls: list[str] = []
        self._errors = errors or []

    def insert_rows_json(self, table: str, rows: list[dict]) -> list:
        self.appends.append((table, [dict(row) for row in rows]))
        return list(self._errors)

    def __getattr__(self, name: str):
        self.other_calls.append(name)
        raise AttributeError(name)


def _ids(rows: dict[str, list[dict]]) -> set[str]:
    return {
        value
        for table_rows in rows.values()
        for row in table_rows
        for value in row.values()
        if isinstance(value, str) and IDENTIFIER.fullmatch(value)
    }


def test_persistence_canary_is_byte_identical_to_the_starting_commit() -> None:
    digest = hashlib.sha256(PERSISTENCE_CANARY.read_bytes()).hexdigest()
    assert digest == PERSISTENCE_CANARY_SHA256


def test_runner_is_separately_versioned_and_reuses_the_persistence_constructors() -> None:
    module = _module()
    assert module.CANARY_VERSION != module.persistence_canary.CANARY_VERSION
    assert module.CANARY_NAME != "oi_persistence_canary"
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "persistence_canary.fixture_rows(" in source
    assert "persistence_canary.fixture_rule(" in source
    for forbidden in ("delete_table", "_cleanup_proof", "persist_open_intelligence_rows", "send"):
        assert forbidden not in source, forbidden


def test_qa_target_contract_names_the_manifest_dataset_identity_and_retention() -> None:
    module = _module()
    assert module.QA_DATASET == QA_DATASET
    assert module.QA_IDENTITY == QA_IDENTITY
    assert module.QA_CLIENT_SCOPE_ID == run_receipts.QA_CLIENT_SCOPE_ID == "qa_canary"
    assert module.QA_RETENTION_DAYS == 90
    assert module.manifest_contract(MANIFEST) == {"dataset": QA_DATASET, "identity": QA_IDENTITY}
    proposal = os.environ.get("R03_CANONICAL_PROPOSAL_DIR")
    if proposal:
        live = json.loads(Path(proposal, "resource_manifest.json").read_text(encoding="utf-8"))
        assert module.manifest_contract(live) == {"dataset": QA_DATASET, "identity": QA_IDENTITY}


@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        ({"identities": {}, "resources": MANIFEST["resources"]}, "qa_identity_unnamed"),
        ({"identities": MANIFEST["identities"], "resources": []}, "qa_dataset_unnamed"),
        (
            {
                "identities": MANIFEST["identities"],
                "resources": [{"actions": ["read"], "name": MANIFEST["resources"][0]["name"]}],
            },
            "qa_dataset_unnamed",
        ),
        ("not a manifest", "qa_manifest_invalid"),
    ],
)
def test_manifest_without_a_qa_namespace_refuses_with_a_named_code(manifest, code) -> None:
    module = _module()
    with pytest.raises(module.QaTargetRefusal, match=code):
        module.manifest_contract(manifest)


@pytest.mark.parametrize(
    ("dataset", "identity", "code"),
    [
        ("trends_v2_staging", QA_IDENTITY, "qa_dataset_required"),
        ("trends_v2_staging_funded", QA_IDENTITY, "qa_dataset_required"),
        (QA_DATASET, persistence.TARGET_WRITER_IDENTITY, "qa_identity_required"),
        (
            QA_DATASET,
            "trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com",
            "qa_identity_required",
        ),
    ],
)
def test_target_guard_refuses_every_other_dataset_and_identity(dataset, identity, code) -> None:
    module = _module()
    with pytest.raises(module.QaTargetRefusal, match=code):
        module.validate_qa_target(dataset, identity)
    module.validate_qa_target(QA_DATASET, QA_IDENTITY)


def test_seven_cases_prove_their_states_through_the_engine_path() -> None:
    module = _module()
    assert tuple(EXPECTED_STATES) == module.CASES
    result = module.run_semantic_canaries(RUN_DATE, apply=False, manifest=MANIFEST)
    assert result["status"] == "passed"
    assert result["retention_days"] == 90
    for case, (state, reasons) in EXPECTED_STATES.items():
        outcome = result["cases"][case]
        assert outcome["expected"] == {"state": state, "reasons": list(reasons)}, case
        assert outcome["actual"] == outcome["expected"], case
        assert outcome["passed"] is True, case


def test_a_broken_expectation_raises_a_diagnostic_alarm() -> None:
    module = _module()
    broken = dict(module.EXPECTED_STATES)
    broken["thin"] = ("ready", ())
    result = module.run_semantic_canaries(RUN_DATE, apply=False, manifest=MANIFEST, expected=broken)
    assert result["status"] == "failed"
    assert result["cases"]["thin"]["passed"] is False
    assert result["cases"]["thin"]["actual"] == {
        "state": "thin",
        "reasons": ["insufficient_qualifying_families"],
    }
    assert [case for case, item in result["cases"].items() if not item["passed"]] == ["thin"]


def test_case_rows_carry_the_qa_identity_and_pass_the_ordinary_batch_validator() -> None:
    module = _module()
    persistence_ids = _ids(module.persistence_canary.fixture_rows(RUN_DATE))
    seen_run_ids = set()
    for case in module.CASES:
        rows = module.case_rows(RUN_DATE, case)
        if case != "duplicate":
            persistence.OpenIntelligenceRowBatch(**rows)
        assert _ids(rows).isdisjoint(persistence_ids), case
        for table_rows in rows.values():
            for row in table_rows:
                assert row["client_scope_id"] == "qa_canary"
                assert row["brand_config_id"] == "qa"
                assert row["theme_id"] == "qa"
                assert row["run_id"] == f"oi_semantic_canary_v1_{RUN_DATE.isoformat()}_{case}"
                assert row["market"] == "za"
                if "discovery_mode" in row:
                    assert row["discovery_mode"] == "canary"
        seen_run_ids.add(rows["candidates"][0]["run_id"])
        assert all(row["url"].startswith("https://example.invalid/") for row in rows["evidence"])
    assert len(seen_run_ids) == len(module.CASES)


def test_apply_appends_results_only_with_the_bounded_retention_and_no_cleanup() -> None:
    module = _module()
    client = _Client()
    result = module.run_semantic_canaries(
        RUN_DATE,
        apply=True,
        manifest=MANIFEST,
        client_factory=lambda **_: client,
        today=RUN_DATE,
        source_sha="a" * 40,
    )
    assert result["status"] == "passed"
    assert client.other_calls == []
    assert len(client.appends) == 1
    table, rows = client.appends[0]
    assert table == "ogilvy-trends-v2.trends_v2_staging_qa.canary_results_v2"
    assert [row["canary_id"] for row in rows] == [
        f"oi_semantic_canary_v1:{case}" for case in module.CASES
    ]
    assert result["retention_until"] == (RUN_DATE + timedelta(days=90)).isoformat()
    assert all(row["client_scope_id"] == "qa_canary" for row in rows)
    assert all(row["passed"] is True for row in rows)
    assert result["append"] == {"table": table, "appended": len(module.CASES)}


def test_apply_refuses_a_foreign_identity_an_expired_window_and_a_failed_append() -> None:
    module = _module()
    with pytest.raises(persistence.TargetInvalid):
        module.run_semantic_canaries(
            RUN_DATE,
            apply=True,
            manifest=MANIFEST,
            client_factory=lambda **_: _Client(identity=persistence.TARGET_WRITER_IDENTITY),
            today=RUN_DATE,
        )
    client = _Client()
    with pytest.raises(module.QaTargetRefusal, match="qa_retention_expired"):
        module.run_semantic_canaries(
            RUN_DATE,
            apply=True,
            manifest=MANIFEST,
            client_factory=lambda **_: client,
            today=RUN_DATE + timedelta(days=91),
        )
    assert client.appends == []
    with pytest.raises(module.QaAppendRefusal):
        module.run_semantic_canaries(
            RUN_DATE,
            apply=True,
            manifest=MANIFEST,
            client_factory=lambda **_: _Client(errors=[{"index": 0, "errors": ["boom"]}]),
            today=RUN_DATE,
        )


def test_dry_run_never_constructs_a_client_and_a_mismatched_manifest_refuses() -> None:
    module = _module()

    def factory(**_):
        raise AssertionError("dry run constructed a client")

    module.run_semantic_canaries(RUN_DATE, apply=False, manifest=MANIFEST, client_factory=factory)
    other = {
        "identities": {
            "qa": "//iam.googleapis.com/projects/p/serviceAccounts/other@p.iam.gserviceaccount.com"
        },
        "resources": MANIFEST["resources"],
    }
    with pytest.raises(module.QaTargetRefusal, match="qa_contract_mismatch"):
        module.run_semantic_canaries(RUN_DATE, apply=False, manifest=other)


def test_rendered_result_is_compact_json_naming_the_version_and_target() -> None:
    module = _module()
    result = module.run_semantic_canaries(
        RUN_DATE, apply=False, manifest=MANIFEST, source_sha="b" * 40
    )
    text = module.render_result(result)
    assert json.loads(text) == result
    assert result["canary"] == "oi_semantic_canary"
    assert result["canary_version"] == "v1"
    assert result["target"] == {
        "project": "ogilvy-trends-v2",
        "dataset": QA_DATASET,
        "identity": QA_IDENTITY,
        "client_scope_id": "qa_canary",
    }
    assert result["source_sha"] == "b" * 40


def test_cli_exits_nonzero_after_printing_a_failed_canary_diagnostic(monkeypatch, capsys) -> None:
    module = _module()
    failed = {
        "status": "failed",
        "cases": {"thin": {"passed": False, "actual": {"state": "thin"}}},
        "append": {"table": "qa.results", "appended": 7},
    }
    monkeypatch.setattr(module, "_load_manifest", lambda _path: MANIFEST)
    monkeypatch.setattr(module, "run_semantic_canaries", lambda *_args, **_kwargs: failed)
    monkeypatch.setattr(sys, "argv", ["run_42_semantic_canaries.py", "--date", "2026-09-14"])

    with pytest.raises(SystemExit) as error:
        module.main()

    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == failed


def test_cli_exits_zero_after_printing_a_passing_canary_result(monkeypatch, capsys) -> None:
    module = _module()
    passed = {"status": "passed", "cases": {}, "append": {"table": None, "appended": 0}}
    monkeypatch.setattr(module, "_load_manifest", lambda _path: MANIFEST)
    monkeypatch.setattr(module, "run_semantic_canaries", lambda *_args, **_kwargs: passed)
    monkeypatch.setattr(sys, "argv", ["run_42_semantic_canaries.py", "--date", "2026-09-14"])

    module.main()

    assert json.loads(capsys.readouterr().out) == passed


CANARY_RESULTS_DDL = Path("infra/bigquery_schemas/canary_results_v2.sql")
_DDL_COLUMN = re.compile(r"^\s{2}(?P<name>[a-z_]+)\s+(?P<type>[A-Z0-9<>]+)(?P<rest>[^\n]*)$", re.M)
STARTED_AT = datetime(2026, 9, 14, 4, 0, 0, tzinfo=UTC)


def _ddl_columns() -> dict[str, tuple[str, bool]]:
    text = CANARY_RESULTS_DDL.read_text(encoding="utf-8")
    return {
        match["name"]: (match["type"], "NOT NULL" in match["rest"])
        for match in _DDL_COLUMN.finditer(text)
    }


def _apply(module, **kwargs):
    client = _Client()
    result = module.run_semantic_canaries(
        RUN_DATE,
        apply=True,
        manifest=MANIFEST,
        client_factory=lambda **_: client,
        today=RUN_DATE,
        source_sha="a" * 40,
        clock=lambda: STARTED_AT,
        **kwargs,
    )
    return result, client


def test_results_land_in_the_migrated_qa_table_with_its_exact_columns() -> None:
    module = _module()
    columns = _ddl_columns()
    assert "canary_run_id" in columns
    result, client = _apply(module)
    table, rows = client.appends[0]
    assert table == "ogilvy-trends-v2.trends_v2_staging_qa.canary_results_v2"
    assert result["append"] == {"table": table, "appended": len(module.CASES)}
    run_ids = {row["canary_run_id"] for row in rows}
    assert len(run_ids) == 1
    for case, row in zip(module.CASES, rows, strict=True):
        assert set(row) == set(columns), case
        for name, (_type, required) in columns.items():
            if required:
                assert row[name] is not None, (case, name)
        assert row["run_id"] == f"oi_semantic_canary_v1_{RUN_DATE.isoformat()}_{case}"
        assert row["contract_version"] == "oi_semantic_canary_v1"
        assert row["market_scope"] == ["za"]
        assert row["audience_lens_ids"] == []
        assert row["brand_config_id"] == "qa"
        assert row["theme_id"] == "qa"
        state, reasons = EXPECTED_STATES[case]
        assert json.loads(row["expected_result"]) == {"state": state, "reasons": list(reasons)}
        assert json.loads(row["actual_result"]) == {"state": state, "reasons": list(reasons)}
        assert row["passed"] is True
        assert row["error_code"] is None
        assert row["started_at"] == STARTED_AT.isoformat()
        assert row["finished_at"] == STARTED_AT.isoformat()


def test_a_drill_break_fails_only_its_case_and_raises_the_alarm() -> None:
    module = _module()
    result = module.run_semantic_canaries(
        RUN_DATE, apply=False, manifest=MANIFEST, drill_break="coherent"
    )
    assert result["status"] == "failed"
    assert result["drill"] == {"case": "coherent"}
    assert [case for case, item in result["cases"].items() if not item["passed"]] == ["coherent"]
    assert result["cases"]["coherent"]["actual"] == {"state": "ready", "reasons": []}
    assert result["alarm"] == {
        "code": "qa_semantic_canary_failed",
        "severity": "ERROR",
        "failed_cases": ["coherent"],
        "drill": True,
    }


def test_a_real_mismatch_raises_the_alarm_without_the_drill_mark() -> None:
    module = _module()
    broken = dict(module.EXPECTED_STATES)
    broken["thin"] = ("ready", ())
    result = module.run_semantic_canaries(RUN_DATE, apply=False, manifest=MANIFEST, expected=broken)
    assert result["drill"] is None
    assert result["alarm"] == {
        "code": "qa_semantic_canary_failed",
        "severity": "ERROR",
        "failed_cases": ["thin"],
        "drill": False,
    }


def test_restoring_after_a_drill_reruns_the_unchanged_corpus_clean() -> None:
    module = _module()
    drilled = module.run_semantic_canaries(
        RUN_DATE, apply=False, manifest=MANIFEST, drill_break="causal"
    )
    restored = module.run_semantic_canaries(RUN_DATE, apply=False, manifest=MANIFEST)
    assert re.fullmatch(r"[0-9a-f]{64}", restored["corpus_sha256"])
    assert drilled["corpus_sha256"] == restored["corpus_sha256"]
    assert restored["status"] == "passed"
    assert restored["alarm"] is None
    assert restored["drill"] is None
    other_day = module.run_semantic_canaries(
        RUN_DATE + timedelta(days=1), apply=False, manifest=MANIFEST
    )
    assert other_day["corpus_sha256"] != restored["corpus_sha256"]


def test_appended_rows_name_the_drill_and_the_mismatch_apart() -> None:
    module = _module()
    _result, client = _apply(module, drill_break="thin")
    rows = {row["canary_id"].split(":", 1)[1]: row for row in client.appends[0][1]}
    assert rows["thin"]["passed"] is False
    assert rows["thin"]["error_code"] == "qa_drill_break"
    assert json.loads(rows["thin"]["actual_result"]) == {
        "state": "thin",
        "reasons": ["insufficient_qualifying_families"],
    }
    assert all(row["error_code"] is None for case, row in rows.items() if case != "thin")
    broken = dict(module.EXPECTED_STATES)
    broken["opposing"] = ("ready", ())
    _result, client = _apply(module, expected=broken)
    rows = {row["canary_id"].split(":", 1)[1]: row for row in client.appends[0][1]}
    assert rows["opposing"]["error_code"] == "semantic_expectation_mismatch"


def test_an_unknown_drill_case_refuses_before_any_client() -> None:
    module = _module()

    def factory(**_):
        raise AssertionError("refused drill constructed a client")

    with pytest.raises(module.QaTargetRefusal, match="qa_case_unknown"):
        module.run_semantic_canaries(
            RUN_DATE,
            apply=True,
            manifest=MANIFEST,
            client_factory=factory,
            today=RUN_DATE,
            drill_break="nonexistent",
        )


def test_cli_drill_writes_one_error_alarm_line_and_exits_nonzero(monkeypatch, capsys) -> None:
    module = _module()
    monkeypatch.setattr(module, "_load_manifest", lambda _path: MANIFEST)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_42_semantic_canaries.py", "--date", "2026-09-14", "--drill-break", "thin"],
    )
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1
    alarm = json.loads(lines[0])
    assert alarm == {
        "severity": "ERROR",
        "message": "qa_semantic_canary_failed",
        "canary": "oi_semantic_canary",
        "canary_version": "v1",
        "run_date": "2026-09-14",
        "failed_cases": ["thin"],
        "drill": True,
        "corpus_sha256": result["corpus_sha256"],
    }


def test_cli_passing_run_writes_no_alarm(monkeypatch, capsys) -> None:
    module = _module()
    monkeypatch.setattr(module, "_load_manifest", lambda _path: MANIFEST)
    monkeypatch.setattr(sys, "argv", ["run_42_semantic_canaries.py", "--date", "2026-09-14"])
    module.main()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "passed"
    assert captured.err == ""


def test_cli_defaults_the_date_to_the_current_utc_day(monkeypatch, capsys) -> None:
    module = _module()
    seen = {}

    def fake(run_date, **kwargs):
        seen["date"] = run_date
        return {"status": "passed", "cases": {}, "append": {"table": None, "appended": 0}}

    monkeypatch.setattr(module, "_load_manifest", lambda _path: MANIFEST)
    monkeypatch.setattr(module, "run_semantic_canaries", fake)
    monkeypatch.setattr(module, "_utc_today", lambda: date(2026, 9, 25))
    monkeypatch.setattr(sys, "argv", ["run_42_semantic_canaries.py"])
    module.main()
    assert seen["date"] == date(2026, 9, 25)
    assert type(seen["date"]) is date


def test_the_utc_day_comes_from_an_aware_utc_clock() -> None:
    module = _module()
    clock = lambda: datetime(2026, 9, 25, 23, 30, tzinfo=UTC)  # noqa: E731
    assert module._utc_today(clock) == date(2026, 9, 25)
    with pytest.raises(module.QaTargetRefusal, match="qa_date_invalid"):
        module._utc_today(lambda: datetime(2026, 9, 25, 23, 30))


def test_cli_refuses_an_unparseable_explicit_date(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_load_manifest", lambda _path: MANIFEST)
    monkeypatch.setattr(sys, "argv", ["run_42_semantic_canaries.py", "--date", "today"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
