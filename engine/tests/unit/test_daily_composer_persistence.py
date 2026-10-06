"""Daily-only atomic persistence contract over one transaction-aware fake."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import daily_composer, daily_stages, persistence
from src.analysis.open_intelligence.run_receipts import RUN_RECEIPT_ROW_FIELDS

from tests.unit import test_daily_composer as composer_test
from tests.unit.test_dynamic_signal_persistence import _outcome


class AtomicClient(composer_test.FakePersistClient):
    """Model the combined script as one snapshot with commit or rollback."""

    def __init__(self, *, target_rows=None, receipts=None, analysis_rows=None):
        super().__init__()
        if target_rows is not None:
            self.target_rows = copy.deepcopy(target_rows)
        self.receipts = copy.deepcopy(receipts or [])
        self.analysis_rows = copy.deepcopy(analysis_rows or [])
        self.mutex_rows = [{"mutex_id": "daily_composer", "fence_epoch": 0}]
        self.atomic_sql: list[str] = []
        self.fail_after_family: str | None = None
        self.fail_receipt = False
        self.mutex_conflict = False

    def _staged(self, sql):
        staged = {table: [] for table in persistence.TABLE_BINDINGS}
        for rows, destination in self.loads:
            identifier = f"`{destination.project}.{destination.dataset_id}.{destination.table_id}`"
            if identifier in sql:
                staged[destination.labels["open_intelligence_table"]] = copy.deepcopy(rows)
        return staged

    @staticmethod
    def _run_rows(rows, run_id):
        return [row for row in rows if row["run_id"] == run_id]

    @staticmethod
    def _same(table, left, right):
        return composer_test._fake_json_value(left) == composer_test._fake_json_value(right)

    @staticmethod
    def _key(table, row):
        return tuple(composer_test._fake_json_value(persistence._natural_key(table, row)))

    def _atomic(self, sql, job_config):
        self.atomic_sql.append(sql)
        parameters = {
            item.name: (
                list(item.values) if getattr(item, "values", None) is not None else item.value
            )
            for item in job_config.query_parameters
        }
        run_id = parameters["run_id"]
        before = (
            copy.deepcopy(self.target_rows),
            copy.deepcopy(self.receipts),
            copy.deepcopy(self.mutex_rows),
        )
        try:
            if self.mutex_conflict:
                raise RuntimeError("transaction aborted by concurrent update")
            if (
                len(self.mutex_rows) != 1
                or self.mutex_rows[0].get("mutex_id") != "daily_composer"
                or not isinstance(self.mutex_rows[0].get("fence_epoch"), int)
            ):
                raise RuntimeError("daily_mutex_invalid")
            self.mutex_rows[0]["fence_epoch"] += 1
            if self.analysis_rows:
                raise RuntimeError("persist_batch_differs")
            staged = self._staged(sql)
            existing = [row for row in self.receipts if row["run_id"] == run_id]
            if len(existing) > 1:
                raise RuntimeError("persist_receipt_cardinality")
            for table in persistence.TABLE_BINDINGS:
                target = self._run_rows(self.target_rows[table], run_id)
                staged_rows = staged[table]
                target_by_key = {self._key(table, row): row for row in target}
                staged_by_key = {self._key(table, row): row for row in staged_rows}
                if any(key not in staged_by_key for key in target_by_key):
                    raise RuntimeError("persist_batch_differs")
                if any(
                    not self._same(table, target_by_key[key], staged_by_key[key])
                    for key in target_by_key
                ):
                    raise RuntimeError("persist_batch_differs")
                if existing and set(target_by_key) != set(staged_by_key):
                    raise RuntimeError("persist_batch_differs")
            if existing and (
                existing[0]["row_set_digest"] != self._digest()
                or existing[0]["display_release_state"] not in {"blocked", "enabled"}
                or any(
                    existing[0][field] != parameters[field]
                    for field in persistence._DAILY_RETRY_BINDING_FIELDS
                )
            ):
                raise RuntimeError("persist_batch_differs")
            if existing:
                return composer_test._Job(existing)
            for table in persistence.TABLE_BINDINGS:
                target_keys = {self._key(table, row) for row in self.target_rows[table]}
                for row in staged[table]:
                    if self._key(table, row) not in target_keys:
                        self.target_rows[table].append(copy.deepcopy(row))
                if self.fail_after_family == table:
                    raise RuntimeError("injected family failure")
            if self.fail_receipt:
                raise RuntimeError("injected receipt failure")
            row = {
                field: parameters[field]
                for field in RUN_RECEIPT_ROW_FIELDS
                if field != "row_set_digest"
            }
            row["row_set_digest"] = self._digest()
            self.receipts.append(row)
            return composer_test._Job([row])
        except Exception:
            self.target_rows, self.receipts, self.mutex_rows = before
            raise

    def query(self, sql, *, job_config, location, retry=None, job_retry=None):
        if "daily_composition_atomic_v1" in sql:
            return self._atomic(sql, job_config)
        return super().query(
            sql, job_config=job_config, location=location, retry=retry, job_retry=job_retry
        )


def setup(client=None):
    composer, _old, _client, _warehouse = composer_test.build()
    client = AtomicClient() if client is None else client
    bundle = daily_composer.daily_rule_bundle(
        composer_test.staging_profile(),
        approved_by="recurring_grant:daily",
        approved_at=composer_test.stages.STARTED,
        rule_version="composition_rules_v3",
    )
    persist = daily_composer.build_native_persist(
        client=client,
        dataset=composer_test.TARGET_DATASET,
        rule_bundle=bundle,
        run_receipt_inputs=daily_composer.composition_receipt_inputs(
            composer, source_sha=composer_test.SOURCE_SHA
        ),
    )
    bridge = composer_test.bridge_for(composer_test.compose(composer))
    return persist, bridge, client


def call(persist, bridge):
    return persist(
        bridge,
        run_id=composer_test.STAGING_RUN_ID,
        signal_date=composer_test.DAY,
        created_at=composer_test.stages.COMPLETED,
    )


def test_daily_writer_is_one_mutex_first_transaction_and_exact_retry_is_a_noop():
    persist, bridge, client = setup()
    receipt = call(persist, bridge)
    before = copy.deepcopy(client.target_rows)
    assert call(persist, bridge) == receipt
    assert client.target_rows == before
    assert len(client.receipts) == 1
    sql = client.atomic_sql[0]
    assert sql.count("BEGIN TRANSACTION") == sql.count("COMMIT TRANSACTION") == 1
    assert sql.index(
        "UPDATE `ogilvy-trends-v2.trends_v2_staging.open_intelligence_daily_persist_mutex_v1`"
    ) < sql.index("open_intelligence_run_receipts_v1")
    assert (
        "DELETE FROM `ogilvy-trends-v2.trends_v2_staging.open_intelligence_daily_persist_mutex_v1`"
        not in sql
    )
    assert (
        "INSERT INTO `ogilvy-trends-v2.trends_v2_staging.open_intelligence_daily_persist_mutex_v1`"
        not in sql
    )


def test_changed_batch_and_empty_staged_family_refuse_without_mutation():
    persist, bridge, client = setup()
    call(persist, bridge)
    changed = persistence.OpenIntelligenceRowBatch(
        tuple({**row, "signal_id": "sig_changed"} for row in bridge.batch.candidates),
        tuple({**row, "signal_id": "sig_changed"} for row in bridge.batch.evidence),
        tuple({**row, "signal_id": "sig_changed"} for row in bridge.batch.membership),
        bridge.batch.lineage,
        bridge.batch.predictions,
        bridge.batch.outcomes,
        cluster_build_version=bridge.batch.cluster_build_version,
    )
    before = copy.deepcopy(client.target_rows)
    with pytest.raises(daily_stages.StageRefusal, match="persist_batch_differs"):
        call(persist, persistence.ProducingBridgeResult(changed, bridge.admitted, bridge.skipped))
    assert client.target_rows == before
    empty = persistence.ProducingBridgeResult(
        persistence.OpenIntelligenceRowBatch(
            (), (), (), (), (), (), cluster_build_version="hybrid_graph_v3"
        ),
        (),
        (),
    )
    with pytest.raises(daily_stages.StageRefusal, match="persist_batch_differs"):
        call(persist, empty)
    assert client.target_rows == before


def test_partial_recovery_commits_missing_rows_and_failures_roll_back_all_families():
    persist, bridge, client = setup()
    client.target_rows["candidates"] = [dict(bridge.batch.candidates[0])]
    receipt = call(persist, bridge)
    assert receipt.candidate_count == 1
    assert [len(client.target_rows[name]) for name in persistence.TABLE_BINDINGS] == [
        1,
        2,
        2,
        0,
        0,
        0,
    ]
    failing = AtomicClient()
    persist2, bridge2, failing = setup(failing)
    failing.fail_after_family = "evidence"
    with pytest.raises(persistence.PersistenceError):
        call(persist2, bridge2)
    assert all(not rows for rows in failing.target_rows.values())
    assert failing.receipts == []
    assert failing.mutex_rows[0]["fence_epoch"] == 0


def test_receipt_failure_mutex_conflict_and_analysis_or_outcomes_refuse_cleanly():
    for attribute in ("fail_receipt", "mutex_conflict"):
        client = AtomicClient()
        setattr(client, attribute, True)
        persist, bridge, client = setup(client)
        expected = (
            persistence.ConcurrentWriteConflict
            if attribute == "mutex_conflict"
            else persistence.PersistenceError
        )
        with pytest.raises(expected):
            call(persist, bridge)
        assert all(not rows for rows in client.target_rows.values())
        assert client.receipts == []
    analysis = AtomicClient(analysis_rows=[{"run_id": composer_test.STAGING_RUN_ID}])
    persist, bridge, analysis = setup(analysis)
    with pytest.raises(daily_stages.StageRefusal, match="persist_batch_differs"):
        call(persist, bridge)
    outcomes = persistence.OpenIntelligenceRowBatch(
        bridge.batch.candidates,
        bridge.batch.evidence,
        bridge.batch.membership,
        bridge.batch.lineage,
        bridge.batch.predictions,
        (_outcome(),),
        cluster_build_version=bridge.batch.cluster_build_version,
    )
    with pytest.raises(daily_stages.StageRefusal, match="outcomes"):
        call(persist, persistence.ProducingBridgeResult(outcomes, (), ()))


@pytest.mark.parametrize(
    "mutex_rows",
    [
        [],
        [{"mutex_id": "other", "fence_epoch": 0}],
        [
            {"mutex_id": "daily_composer", "fence_epoch": 0},
            {"mutex_id": "daily_composer", "fence_epoch": 1},
        ],
    ],
)
def test_missing_malformed_or_duplicate_mutex_refuses_without_writes(mutex_rows):
    client = AtomicClient()
    client.mutex_rows = copy.deepcopy(mutex_rows)
    persist, bridge, client = setup(client)
    with pytest.raises(persistence.TargetInvalid, match="mutex"):
        call(persist, bridge)
    assert all(not rows for rows in client.target_rows.values())
    assert client.receipts == []


def test_incompatible_partial_row_rolls_back_the_mutex_and_preserves_target():
    persist, bridge, client = setup()
    conflicting = dict(bridge.batch.candidates[0])
    conflicting["label"] = "conflicting partial label"
    client.target_rows["candidates"] = [conflicting]
    before = copy.deepcopy(client.target_rows)
    with pytest.raises(daily_stages.StageRefusal, match="persist_batch_differs"):
        call(persist, bridge)
    assert client.target_rows == before
    assert client.mutex_rows[0]["fence_epoch"] == 0


def test_corrupt_existing_receipt_refuses_without_target_mutation():
    persist, bridge, client = setup()
    call(persist, bridge)
    client.receipts[0]["evidence_count"] += 1
    before = copy.deepcopy(client.target_rows)
    with pytest.raises(daily_stages.StageRefusal, match="persist_batch_differs"):
        call(persist, bridge)
    assert client.target_rows == before


IMMUTABLE_RECEIPT_MUTATIONS = [
    ("run_contract_version", "other_contract"),
    ("client_scope_id", "other_scope"),
    ("market_scope", ["za"]),
    ("signal_date", composer_test.DAY - timedelta(days=1)),
    ("observation_start", composer_test.DAY - timedelta(days=8)),
    ("observation_end", composer_test.DAY - timedelta(days=1)),
    ("observation_method", "other_method"),
    ("source_window_digest", "f" * 64),
    ("cluster_build_version", "hybrid_graph_other"),
    ("source_family_map_version", "other_family_map"),
    ("rule_version", "other_rule"),
    ("status", "failed"),
    ("complete_partitions", False),
    ("candidate_count", 2),
    ("evidence_count", 3),
    ("membership_count", 3),
    ("lineage_count", 1),
    ("analysis_count", 1),
    ("prediction_count", 1),
    ("row_set_digest", "f" * 64),
    ("source_sha", "f" * 40),
    ("completed_at", composer_test.stages.COMPLETED + timedelta(seconds=1)),
]


@pytest.mark.parametrize(("field", "changed"), IMMUTABLE_RECEIPT_MUTATIONS)
def test_every_immutable_receipt_binding_refuses_corruption_without_target_mutation(field, changed):
    persist, bridge, client = setup()
    call(persist, bridge)
    client.receipts[0][field] = changed
    before = copy.deepcopy(client.target_rows)
    with pytest.raises(daily_stages.StageRefusal, match="persist_batch_differs"):
        call(persist, bridge)
    assert client.target_rows == before
    assert client.mutex_rows[0]["fence_epoch"] == 1


def test_released_receipt_is_a_legitimate_later_retry_and_keeps_original_completion_time():
    persist, bridge, client = setup()
    first = call(persist, bridge)
    client.receipts[0]["display_release_state"] = "enabled"
    before = copy.deepcopy(client.target_rows)
    later = call(persist, bridge)
    assert later.display_release_state == "enabled"
    assert later.completed_at == first.completed_at
    assert client.target_rows == before


def test_invalid_release_state_and_batch_run_identity_refuse_before_target_mutation():
    persist, bridge, client = setup()
    call(persist, bridge)
    client.receipts[0]["display_release_state"] = "unknown"
    before = copy.deepcopy(client.target_rows)
    with pytest.raises(daily_stages.StageRefusal, match="persist_batch_differs"):
        call(persist, bridge)
    assert client.target_rows == before

    persist, bridge, fresh = setup()
    mismatched = persistence.OpenIntelligenceRowBatch(
        tuple({**row, "run_id": "run_other"} for row in bridge.batch.candidates),
        tuple({**row, "run_id": "run_other"} for row in bridge.batch.evidence),
        tuple({**row, "run_id": "run_other"} for row in bridge.batch.membership),
        bridge.batch.lineage,
        bridge.batch.predictions,
        bridge.batch.outcomes,
        cluster_build_version=bridge.batch.cluster_build_version,
    )
    with pytest.raises(daily_stages.StageRefusal, match="daily batch scope differs"):
        call(persist, persistence.ProducingBridgeResult(mismatched, (), ()))
    assert all(not rows for rows in fresh.target_rows.values())


def test_cleanup_failure_carries_the_committed_receipt():
    client = AtomicClient()
    client.delete_error = RuntimeError("cleanup denied")
    persist, bridge, client = setup(client)
    with pytest.raises(persistence.DailyPersistenceCleanupFailure) as raised:
        call(persist, bridge)
    assert raised.value.committed_receipt.run_id == composer_test.STAGING_RUN_ID
    assert len(client.receipts) == 1


def test_transaction_uses_typed_empty_relations_all_six_families_and_sql_digest():
    persist, bridge, client = setup()
    call(persist, bridge)
    sql = client.atomic_sql[0]
    for family, target in persistence.TABLE_BINDINGS.items():
        assert family in sql
        assert target in sql
    assert "signal_analysis_v2" in sql
    assert "row_set_digest" in sql
    assert "SHA256" in sql
