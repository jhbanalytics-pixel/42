"""Rollback selects an earlier compatible release by reading, and never writes.

The fake store holds every table the rollback reads and the evidence tables it
must never touch. Each test snapshots the store before the rollback and compares
it after, so a rollback that edited, deleted or appended a row fails here.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime

import pytest
from scripts.staging import release_open_intelligence_run as release

POLICY_OLD = "e" * 64
POLICY_NEW = "d" * 64
IMAGE_OLD = "a" * 40
IMAGE_NEW = "b" * 40
CONTRACT = "open_intelligence_run_receipt_v1"


def profile(run_id: str, policy: str) -> release.ReleaseRunProfile:
    return release.ReleaseRunProfile(
        profile_name=f"p_{run_id}",
        run_id=run_id,
        source_policy_digest=policy,
        cutoff="2026-09-08",
        generation_pair=None,
        canary_namespace=False,
        independence_policy=release.EXPLICIT_ORIGIN_POLICY,
    )


class FakeStore:
    """Rows by table; answers the rollback's SELECTs and records every statement."""

    def __init__(self, runs):
        self.statements: list[str] = []
        self.tables: dict[str, list[dict]] = {
            release.RELEASE_RECORD_TABLE: [],
            release.RECEIPT_TABLE: [],
            release.PREDICTIONS_TABLE: [],
            release.OUTCOMES_TABLE: [],
            release.EVIDENCE_TABLE: [],
            release.CANDIDATES_TABLE: [],
            release.MEMBERSHIP_TABLE: [],
        }
        for run_id, day, image, contract, state, *hour in runs:
            self.tables[release.RELEASE_RECORD_TABLE].append(
                {
                    "run_id": run_id,
                    "released_at": datetime(2026, 9, day, *(hour or [12]), tzinfo=UTC),
                    "release_contract_version": release.RELEASE_CONTRACT_VERSION,
                }
            )
            self.tables[release.RECEIPT_TABLE].append(
                {
                    "run_id": run_id,
                    "run_contract_version": contract,
                    "source_sha": image,
                    "observation_end": date(2026, 9, day - 1),
                    "display_release_state": state,
                }
            )
            self.tables[release.PREDICTIONS_TABLE].append(
                {"run_id": run_id, "prediction_id": f"pred_{run_id}"}
            )
            # An outcome's run_id is the evaluation run that scored the prediction.
            self.tables[release.OUTCOMES_TABLE].append(
                {
                    "run_id": f"eval_{run_id}",
                    "prediction_id": f"pred_{run_id}",
                    "outcome_id": f"out_{run_id}",
                }
            )
            for table in (
                release.EVIDENCE_TABLE,
                release.CANDIDATES_TABLE,
                release.MEMBERSHIP_TABLE,
            ):
                self.tables[table].append({"run_id": run_id, "row": f"{table}_{run_id}"})

    def __call__(self, sql: str):
        self.statements.append(sql)
        table = re.search(r"`[^`]+\.([a-z0-9_]+)`", sql).group(1)
        rows = self.tables[table]
        listed = re.search(r"WHERE ([a-z_]+) IN \(([^)]*)\)", sql)
        if listed is not None:
            column = listed.group(1)
            wanted = set(re.findall(r"'([^']+)'", listed.group(2)))
            rows = [row for row in rows if row[column] in wanted]
        # Copies, so a caller that mutated a returned row could not reach the store.
        return [dict(row) for row in rows]


RUNS = (
    ("run_a", 8, IMAGE_OLD, CONTRACT, "enabled"),
    ("run_b", 9, IMAGE_OLD, CONTRACT, "enabled"),
    ("run_c", 10, IMAGE_NEW, CONTRACT, "enabled"),
    ("run_d", 11, IMAGE_OLD, CONTRACT, "enabled"),
    ("run_e", 12, IMAGE_NEW, CONTRACT, "enabled"),
)
PROFILES = {
    "p_a": profile("run_a", POLICY_OLD),
    "p_b": profile("run_b", POLICY_OLD),
    "p_c": profile("run_c", POLICY_NEW),
    "p_d": profile("run_d", POLICY_NEW),
    "p_e": profile("run_e", POLICY_NEW),
}


def rollback(store, **overrides):
    arguments = {
        "current_run_id": "run_e",
        "query_runner": store,
        "compatible_source_policy_digests": (POLICY_OLD,),
        "compatible_source_shas": (IMAGE_OLD,),
        "compatible_run_contract_versions": (CONTRACT,),
        "profiles": PROFILES,
        **overrides,
    }
    return release.read_rollback_selection(**arguments)


def test_selection_picks_the_newest_earlier_compatible_release():
    store = FakeStore(RUNS)
    selection = rollback(store)
    # run_d has the old image but the new policy, run_c the new image: both skipped.
    assert selection.display_run_id == "run_b"
    assert selection.source_policy_digest == POLICY_OLD
    assert selection.retained_run_ids == ("run_c", "run_d", "run_e")
    assert selection.served is False
    assert selection.evidence_edits == ()


def test_selection_skips_an_incompatible_image_contract_and_policy():
    store = FakeStore(
        (
            ("run_a", 8, IMAGE_OLD, CONTRACT, "enabled"),
            ("run_b", 9, IMAGE_NEW, CONTRACT, "enabled"),
            ("run_c", 10, IMAGE_OLD, "open_intelligence_run_receipt_v2", "enabled"),
            ("run_d", 11, IMAGE_OLD, CONTRACT, "enabled"),
            ("run_e", 12, IMAGE_NEW, CONTRACT, "enabled"),
        )
    )
    profiles = {
        **PROFILES,
        "p_b": profile("run_b", POLICY_OLD),
        "p_c": profile("run_c", POLICY_OLD),
    }
    # run_d: new policy. run_c: another run contract. run_b: another image.
    assert rollback(store, profiles=profiles).display_run_id == "run_a"
    widened = rollback(store, profiles=profiles, compatible_source_shas=(IMAGE_OLD, IMAGE_NEW))
    assert widened.display_run_id == "run_b"


def test_a_run_no_profile_names_or_a_blocked_receipt_is_never_selected():
    store = FakeStore(
        (
            ("run_a", 8, IMAGE_OLD, CONTRACT, "enabled"),
            ("run_b", 9, IMAGE_OLD, CONTRACT, "blocked"),
            ("run_x", 10, IMAGE_OLD, CONTRACT, "enabled"),
            ("run_e", 12, IMAGE_NEW, CONTRACT, "enabled"),
        )
    )
    # run_x has no registered profile, so its source policy is unknown.
    selection = rollback(store)
    assert selection.display_run_id == "run_a"
    assert selection.retained_run_ids == ("run_b", "run_e", "run_x")


def test_rollback_refuses_with_a_named_code_when_no_release_is_compatible():
    store = FakeStore(RUNS)
    before = copy.deepcopy(store.tables)
    with pytest.raises(release.ReleaseRefusal) as caught:
        rollback(store, compatible_source_shas=("c" * 40,))
    assert caught.value.code == "rollback_no_compatible_release"
    with pytest.raises(release.ReleaseRefusal) as caught:
        rollback(store, current_run_id="run_a")
    assert caught.value.code == "rollback_no_compatible_release"
    with pytest.raises(release.ReleaseRefusal) as caught:
        rollback(store, current_run_id="run_zz")
    assert caught.value.code == "rollback_current_release_unknown"
    with pytest.raises(release.ReleaseRefusal) as caught:
        rollback(store, compatible_source_shas=("not a sha",))
    assert caught.value.code == "rollback_compatibility_invalid"
    assert store.tables == before


def test_a_release_record_without_a_receipt_refuses_rather_than_guesses():
    store = FakeStore(RUNS)
    store.tables[release.RECEIPT_TABLE] = [
        row for row in store.tables[release.RECEIPT_TABLE] if row["run_id"] != "run_c"
    ]
    with pytest.raises(release.ReleaseRefusal) as caught:
        rollback(store)
    assert caught.value.code == "rollback_record_invalid"


def test_newer_runs_results_and_predictions_are_retained_untouched():
    store = FakeStore(RUNS)
    before = copy.deepcopy(store.tables)
    selection = rollback(store)
    assert store.tables == before
    assert selection.retained_predictions == tuple(
        {"run_id": run_id, "prediction_id": f"pred_{run_id}"}
        for run_id in ("run_c", "run_d", "run_e")
    )
    assert selection.retained_results == tuple(
        {
            "run_id": f"eval_{run_id}",
            "prediction_id": f"pred_{run_id}",
            "outcome_id": f"out_{run_id}",
        }
        for run_id in ("run_c", "run_d", "run_e")
    )
    # Every newer run keeps its release record and its receipt state.
    released = {row["run_id"] for row in store.tables[release.RELEASE_RECORD_TABLE]}
    assert set(selection.retained_run_ids) <= released
    assert {
        row["run_id"]: row["display_release_state"] for row in store.tables[release.RECEIPT_TABLE]
    } == {run_id: "enabled" for run_id, *_ in RUNS}


def test_evidence_is_never_read_or_rewritten_and_every_statement_is_a_select():
    store = FakeStore(RUNS)
    evidence = copy.deepcopy(store.tables[release.EVIDENCE_TABLE])
    rollback(store)
    assert store.tables[release.EVIDENCE_TABLE] == evidence
    assert store.statements
    assert all(sql.startswith("SELECT ") for sql in store.statements)
    for table in (release.EVIDENCE_TABLE, release.CANDIDATES_TABLE, release.MEMBERSHIP_TABLE):
        assert not any(table in sql for sql in store.statements)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t (run_id) VALUES ('x')",
        "UPDATE t SET display_release_state = 'enabled'",
        "SELECT 1; DELETE FROM t WHERE TRUE",
        "SELECT run_id FROM t WHERE run_id IN (SELECT run_id FROM u) AND MERGE",
        "SELECT 'a'; DROP TABLE t",
        "SELECT run_id FROM t WHERE run_id = 'x' OR 1=1 ' ; DELETE FROM t",
        "SELECT run_id FROM `p.d.t`\nUPDATE t SET a = 1",
        # A stray quote inside a form the guard does not strip must not swallow a
        # second statement: the separator is refused on the raw text.
        'SELECT "\'", 1; DELETE FROM t WHERE TRUE; SELECT "\'"',
        "SELECT 1 -- '\n; DELETE FROM t WHERE TRUE -- '",
        "SELECT 1 # '\n; DELETE FROM t WHERE TRUE # '",
        "SELECT 1 /* ' */; DROP TABLE t; /* ' */",
        "SELECT '''a'b''', 1; DELETE FROM t WHERE TRUE --'",
        "SELECT r'a\\', 1; DELETE FROM t --'",
        # The same forms without a separator are refused on their own syntax.
        'SELECT "x" FROM t',
        "SELECT 1 -- note",
        "SELECT 1 # note",
        "SELECT 1 /* note */",
        "SELECT '''a''' FROM t",
    ],
)
def test_the_rollback_reader_refuses_any_write(sql):
    calls = []
    reader = release._rollback_reads_only(lambda statement: calls.append(statement) or [])
    with pytest.raises(release.ReleaseRefusal) as caught:
        reader(sql)
    assert caught.value.code == "rollback_write_refused"
    assert calls == []


def test_the_source_estate_document_carries_the_recovery_and_every_unresolved_source():
    from pathlib import Path

    from src.analysis.open_intelligence.source_inventory import (
        STATE_QUALIFIED_LIVE,
        source_estate_document,
        staging_source_estate,
    )

    document = source_estate_document()
    unresolved = document.split("## Unresolved rows", 1)[1].split("## Safe recovery", 1)[0]
    rows, _digest = staging_source_estate()
    for row in rows:
        if row["state"] != STATE_QUALIFIED_LIVE:
            assert f"| {row['source']} | " in unresolved
    recovery = document.split("## Safe recovery", 1)[1]
    script = Path(release.__file__).read_text(encoding="utf-8")
    codes = set(re.findall(r"`(rollback_[a-z_]+)`", recovery))
    assert codes == set(re.findall(r'code="(rollback_[a-z_]+)"', script))
    assert "read_rollback_selection" in recovery


def test_outcomes_are_read_by_prediction_not_by_the_evaluation_run_id():
    store = FakeStore(RUNS)
    # An evaluation run that happens to share a discovery run id scores an older
    # prediction; filtering outcomes on run_id would wrongly retain it.
    store.tables[release.OUTCOMES_TABLE].append(
        {"run_id": "run_e", "prediction_id": "pred_run_a", "outcome_id": "out_decoy"}
    )
    selection = rollback(store)
    assert {row["outcome_id"] for row in selection.retained_results} == {
        "out_run_c",
        "out_run_d",
        "out_run_e",
    }
    outcome_reads = [sql for sql in store.statements if release.OUTCOMES_TABLE in sql]
    assert len(outcome_reads) == 1
    assert "WHERE prediction_id IN (" in outcome_reads[0]


def test_a_release_tied_with_the_target_on_released_at_is_retained():
    store = FakeStore(
        (
            ("run_a", 8, IMAGE_OLD, CONTRACT, "enabled"),
            ("run_b", 9, IMAGE_OLD, CONTRACT, "enabled"),
            ("run_c", 9, IMAGE_NEW, CONTRACT, "enabled"),
            ("run_e", 12, IMAGE_NEW, CONTRACT, "enabled"),
        )
    )
    selection = rollback(store)
    assert selection.display_run_id == "run_b"
    assert selection.retained_run_ids == ("run_c", "run_e")
    assert {row["run_id"] for row in selection.retained_predictions} == {"run_c", "run_e"}


def test_a_hyphenated_run_id_naming_a_keyword_is_read_not_refused():
    runs = (
        ("r4-update-fix", 8, IMAGE_OLD, CONTRACT, "enabled"),
        ("r5_delete_me", 9, IMAGE_OLD, CONTRACT, "enabled"),
        ("r6-merge", 10, IMAGE_NEW, CONTRACT, "enabled"),
    )
    store = FakeStore(runs)
    profiles = {
        "p_4": profile("r4-update-fix", POLICY_OLD),
        "p_5": profile("r5_delete_me", POLICY_NEW),
        "p_6": profile("r6-merge", POLICY_NEW),
    }
    selection = rollback(store, current_run_id="r6-merge", profiles=profiles)
    assert selection.display_run_id == "r4-update-fix"
    assert selection.retained_run_ids == ("r5_delete_me", "r6-merge")
    reader = release._rollback_reads_only(lambda statement: [])
    assert reader("SELECT run_id FROM `p.d.t` WHERE run_id IN ('update', 'x-drop-y')") == []
