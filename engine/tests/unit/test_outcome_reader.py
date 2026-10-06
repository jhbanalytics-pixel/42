"""Boundary tests for the staging-only outcome evaluation reader."""

from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, date, datetime, timedelta

import pytest
from src.analysis.open_intelligence.predictions import PREDICTION_ROW_FIELDS

_MODULE_NAME = "src.analysis.open_intelligence.outcome_reader"
outcome_reader = (
    importlib.import_module(_MODULE_NAME) if importlib.util.find_spec(_MODULE_NAME) else None
)

EVALUATION_DATE = date(2026, 9, 1)
PREDICTION_ID = "pred_" + "a" * 64
SIGNAL_ID = "sig_" + "b" * 64


def prediction_row(**overrides):
    row = {
        "client_scope_id": "fixture_scope",
        "market_scope": ["za"],
        "brand_config_id": "fixture_brand",
        "audience_lens_ids": [],
        "theme_id": "fixture_theme",
        "run_id": "prediction_run_001",
        "contract_version": "2.0.0",
        "prediction_id": PREDICTION_ID,
        "signal_id": SIGNAL_ID,
        "signal_date": date(2026, 8, 25),
        "market": "za",
        "discovery_mode": "dynamic",
        "source_families": ["reddit", "youtube"],
        "evidence_state": "ready",
        "first_seen_at": datetime(2026, 8, 22, 11, 0, tzinfo=UTC),
        "predicted_at": datetime(2026, 8, 25, 6, 50, tzinfo=UTC),
        "expected_trajectory": "sustained",
        "evaluation_date": EVALUATION_DATE,
        "baseline": {"velocity": 0.68, "breadth": 0.62, "evidence_family_count": 2},
        "promotion_target": {
            "velocity": 0.6,
            "breadth": 0.6,
            "evidence_family_count": 2,
        },
        "invalidation_condition": "Breadth falls below 0.60 before 2026-09-01.",
        "cluster_build_version": "hybrid_graph_v1",
        "source_family_map_version": "channel_family_v1",
        "rule_version": "prediction_rules_v1",
        "display_eligible": False,
    }
    row.update(overrides)
    assert tuple(row) == PREDICTION_ROW_FIELDS
    return row


def observation_row(prediction_id=PREDICTION_ID, day=26, **overrides):
    row = {
        "prediction_id": prediction_id,
        "observed_at": datetime(2026, 8, day, tzinfo=UTC),
        "velocity": 0.62,
        "breadth": 0.6,
        "evidence_family_count": 2,
    }
    row.update(overrides)
    return row


class Job:
    def __init__(self, rows):
        self.rows = rows
        self.max_results = None

    def result(self, *, max_results):
        self.max_results = max_results
        return self.rows


class Client:
    project = "ogilvy-trends-v2"

    def __init__(self, *row_sets):
        self.row_sets = list(row_sets)
        self.calls = []
        self.jobs = []

    def query(self, sql, *, job_config, location):
        self.calls.append((sql, job_config, location))
        job = Job(self.row_sets.pop(0))
        self.jobs.append(job)
        return job


def read(client, **overrides):
    values = {
        "client": client,
        "dataset": "trends_v2_staging",
        "evaluation_date": EVALUATION_DATE,
        "client_scope_id": "fixture_scope",
        "evaluation_run_id": "outcome_run_001",
    }
    values.update(overrides)
    return outcome_reader.read_due_outcome_inputs(**values)


def parameters(job_config):
    return {parameter.name: parameter for parameter in job_config.query_parameters}


def test_wrong_target_or_invalid_contract_values_fail_before_query():
    assert outcome_reader is not None, "outcome reader module is missing"
    client = Client(())

    with pytest.raises(ValueError, match="exact staging target"):
        read(client, dataset="trends_v2")
    client.project = "other-project"
    with pytest.raises(ValueError, match="exact staging target"):
        read(client)
    client.project = "ogilvy-trends-v2"
    with pytest.raises(ValueError, match="evaluation date"):
        read(client, evaluation_date=datetime(2026, 9, 1))
    with pytest.raises(ValueError, match="client scope"):
        read(client, client_scope_id="")
    with pytest.raises(ValueError, match="evaluation run"):
        read(client, evaluation_run_id="")
    assert client.calls == []


def test_due_query_is_explicit_parameterized_bounded_and_skips_existing_run():
    assert outcome_reader is not None, "outcome reader module is missing"
    client = Client(())

    result = read(client)

    assert result.predictions == ()
    assert result.observations_by_prediction == {}
    assert result.missing_prediction_ids == ()
    assert len(client.calls) == 1
    sql, config, location = client.calls[0]
    assert location == "US"
    assert "SELECT p.client_scope_id, p.market_scope" in sql
    assert "p.*" not in sql
    assert "FROM `ogilvy-trends-v2.trends_v2_staging.signal_predictions_v2` AS p" in sql
    assert "p.evaluation_date = @evaluation_date" in sql
    assert "p.client_scope_id = @client_scope_id" in sql
    assert "AND NOT EXISTS" in sql
    assert "o.prediction_id = p.prediction_id" in sql
    assert "o.evaluation_date = p.evaluation_date" in sql
    assert "o.run_id = @evaluation_run_id" in sql
    assert "ORDER BY p.market, p.prediction_id" in sql
    assert f"LIMIT {outcome_reader.PREDICTION_CEILING + 1}" in sql
    params = parameters(config)
    assert params["evaluation_date"].value == EVALUATION_DATE
    assert params["client_scope_id"].value == "fixture_scope"
    assert params["evaluation_run_id"].value == "outcome_run_001"
    assert client.jobs[0].max_results == outcome_reader.PREDICTION_CEILING + 1


def test_reader_returns_exact_predictions_and_daily_observations():
    assert outcome_reader is not None, "outcome reader module is missing"
    prediction = prediction_row()
    observations = tuple(
        observation_row(observed_at=datetime(2026, 8, 26, tzinfo=UTC) + timedelta(days=offset))
        for offset in range(7)
    )
    client = Client((prediction,), observations)

    result = read(client)

    assert result.predictions == (prediction,)
    assert tuple(result.observations_by_prediction) == (PREDICTION_ID,)
    assert len(result.observations_by_prediction[PREDICTION_ID]) == 7
    assert result.observations_by_prediction[PREDICTION_ID][0].observed_at == datetime(
        2026, 8, 26, tzinfo=UTC
    )
    assert result.missing_prediction_ids == ()
    assert len(client.calls) == 2
    sql, config, location = client.calls[1]
    assert location == "US"
    assert "signal_candidates_v2` AS c" in sql
    assert "signal_evidence_v2` AS e" in sql
    assert "c.client_scope_id = p.client_scope_id" in sql
    assert "c.market = p.market" in sql
    assert "c.signal_id = p.signal_id" in sql
    assert "e.client_scope_id = c.client_scope_id" in sql
    assert "e.signal_date = c.signal_date" in sql
    assert "e.market = c.market" in sql
    assert "e.signal_id = c.signal_id" in sql
    assert "e.run_id = c.run_id" in sql
    assert "e.availability = 'available'" in sql
    assert "e.published_at IS NOT NULL" in sql
    assert "c.signal_date > p.signal_date" in sql
    assert "c.signal_date <= p.evaluation_date" in sql
    assert "COUNT(DISTINCT IF(" in sql
    assert "ORDER BY p.prediction_id, observed_at, c.run_id" in sql
    assert f"LIMIT {outcome_reader.OBSERVATION_CEILING + 1}" in sql
    params = parameters(config)
    assert params["prediction_ids"].values == [PREDICTION_ID]
    assert client.jobs[1].max_results == outcome_reader.OBSERVATION_CEILING + 1


def test_due_prediction_without_observations_is_explicitly_missing():
    client = Client((prediction_row(),), ())

    result = read(client)

    assert result.observations_by_prediction[PREDICTION_ID] == ()
    assert result.missing_prediction_ids == (PREDICTION_ID,)


def test_prediction_ceiling_stops_before_observation_query():
    assert outcome_reader is not None, "outcome reader module is missing"
    rows = tuple(
        prediction_row(prediction_id=f"pred_{index:064x}", signal_id=f"sig_{index:064x}")
        for index in range(outcome_reader.PREDICTION_CEILING + 1)
    )
    client = Client(rows)

    with pytest.raises(outcome_reader.OutcomeReadCeilingExceeded, match="prediction"):
        read(client)
    assert len(client.calls) == 1


def test_observation_ceiling_duplicate_dates_unknown_predictions_and_out_of_window_fail_closed():
    assert outcome_reader is not None, "outcome reader module is missing"
    prediction = prediction_row()
    too_many = tuple(
        observation_row(observed_at=datetime(2026, 8, 26, tzinfo=UTC) + timedelta(minutes=i))
        for i in range(outcome_reader.OBSERVATION_CEILING + 1)
    )
    with pytest.raises(outcome_reader.OutcomeReadCeilingExceeded, match="observation"):
        read(Client((prediction,), too_many))

    duplicate_date = (observation_row(), observation_row(velocity=0.5))
    with pytest.raises(ValueError, match="duplicate observation date"):
        read(Client((prediction,), duplicate_date))

    with pytest.raises(ValueError, match="unknown prediction"):
        read(Client((prediction,), (observation_row(prediction_id="pred_" + "f" * 64),)))

    with pytest.raises(ValueError, match="outside prediction window"):
        read(Client((prediction,), (observation_row(day=25),)))


def test_duplicate_prediction_ids_and_malformed_rows_fail_closed():
    assert outcome_reader is not None, "outcome reader module is missing"
    prediction = prediction_row()

    with pytest.raises(ValueError, match="duplicate prediction"):
        read(Client((prediction, dict(prediction))))
    with pytest.raises(ValueError, match="prediction fields"):
        read(Client(({**prediction, "extra": True},)))
    malformed = prediction_row(
        prediction_id="bad-id",
        signal_id="bad-signal",
        market_scope=[],
        market="ZA",
        source_families=["unsupported"],
        evidence_state="thin",
        predicted_at=datetime(2026, 8, 25, 6, 50),
        baseline={"velocity": 1.5, "breadth": 0.62, "evidence_family_count": 2},
    )
    with pytest.raises(ValueError, match="prediction"):
        read(Client((malformed,), ()))
    with pytest.raises(ValueError, match="observation fields"):
        read(Client((prediction,), ({**observation_row(), "extra": True},)))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run_id", "", "run id"),
        ("first_seen_at", datetime(2026, 8, 22, 11, 0), "first seen"),
        ("expected_trajectory", "bogus", "trajectory"),
        ("invalidation_condition", "", "invalidation"),
        ("cluster_build_version", "", "cluster build"),
        ("rule_version", "", "rule version"),
        ("display_eligible", "yes", "display eligible"),
    ],
)
def test_every_required_prediction_field_is_validated(field, value, message):
    assert outcome_reader is not None, "outcome reader module is missing"
    row = prediction_row(**{field: value})

    with pytest.raises(ValueError, match=message):
        read(Client((row,), ()))


CUTOFF = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)


def observation_row_v2(prediction_id=PREDICTION_ID, offset=0, **overrides):
    observed_at = datetime(2026, 8, 26, tzinfo=UTC) + timedelta(days=offset)
    row = observation_row(prediction_id, observed_at=observed_at)
    row["available_at"] = observed_at + timedelta(days=1, minutes=40)
    row["observation_run_id"] = f"daily_run_{observed_at.date().isoformat()}"
    row.update(overrides)
    return row


def test_old_observation_query_text_is_retained_byte_for_byte():
    import hashlib

    assert outcome_reader.OBSERVATION_QUERY_VERSION_V1 == "outcome_observation_query_v1"
    assert outcome_reader.OBSERVATION_QUERY_VERSION_V2 == "outcome_observation_query_v2"
    old = outcome_reader._observation_sql()
    assert (
        hashlib.sha256(old.encode("utf-8")).hexdigest()
        == "b36ffab474f3c865f0817c862f87575821fcb1bdcc0023955eb2b7874621d875"
    )


def test_v2_observation_query_differs_from_v1_only_in_the_versioned_clauses():
    old = outcome_reader._observation_sql()
    new = outcome_reader._observation_sql_v2()
    table = "`ogilvy-trends-v2.trends_v2_staging"
    expected = (
        "WITH eligible_runs AS (\n"
        "  SELECT r.client_scope_id, run_market AS market, r.signal_date, r.run_id,\n"
        "    r.completed_at, r.cluster_build_version, r.source_family_map_version,\n"
        "    ROW_NUMBER() OVER (\n"
        "      PARTITION BY r.client_scope_id, run_market, r.signal_date,\n"
        "        r.cluster_build_version, r.source_family_map_version\n"
        "      ORDER BY r.completed_at, r.run_id\n"
        "    ) AS eligible_rank\n"
        f"  FROM {table}.open_intelligence_run_receipts_v1` AS r,\n"
        "    UNNEST(r.market_scope) AS run_market\n"
        "  WHERE r.status = 'completed'\n"
        "    AND r.complete_partitions\n"
        "    AND r.completed_at <= @evaluation_cutoff\n"
        ")\n" + old
    )
    expected = expected.replace(
        "    e.availability = 'available' AND e.published_at IS NOT NULL,\n",
        "    e.availability = 'available' AND e.published_at IS NOT NULL\n"
        "      AND e.published_at < TIMESTAMP(DATE_ADD(c.signal_date, INTERVAL 1 DAY)),\n",
    )
    expected = expected.replace(
        "  )) AS evidence_family_count\n",
        "  )) AS evidence_family_count,\n"
        "  s.completed_at AS available_at, s.run_id AS observation_run_id\n",
    )
    expected = expected.replace(
        f"FROM {table}.signal_predictions_v2` AS p\n"
        f"JOIN {table}.signal_candidates_v2` AS c\n"
        "  ON c.client_scope_id = p.client_scope_id\n"
        "  AND c.market = p.market\n"
        "  AND c.signal_id = p.signal_id\n",
        f"FROM {table}.signal_predictions_v2` AS p\n"
        "JOIN eligible_runs AS s\n"
        "  ON s.client_scope_id = p.client_scope_id\n"
        "  AND s.market = p.market\n"
        "  AND s.eligible_rank = 1\n"
        "  AND s.signal_date > p.signal_date\n"
        "  AND s.signal_date <= p.evaluation_date\n"
        "  AND s.cluster_build_version = p.cluster_build_version\n"
        "  AND s.source_family_map_version = p.source_family_map_version\n"
        f"JOIN {table}.signal_candidates_v2` AS c\n"
        "  ON c.client_scope_id = p.client_scope_id\n"
        "  AND c.market = p.market\n"
        "  AND c.signal_id = p.signal_id\n"
        "  AND c.signal_date = s.signal_date\n"
        "  AND c.run_id = s.run_id\n",
    )
    expected = expected.replace(
        "WHERE p.prediction_id IN UNNEST(@prediction_ids)\n"
        "  AND c.signal_date > p.signal_date\n"
        "  AND c.signal_date <= p.evaluation_date\n",
        "WHERE p.prediction_id IN UNNEST(@prediction_ids)\n",
    )
    expected = expected.replace(
        "GROUP BY p.prediction_id, observed_at, c.run_id, velocity, breadth\n",
        "GROUP BY p.prediction_id, observed_at, c.run_id, velocity, breadth, available_at,"
        " observation_run_id\n",
    )
    assert new == expected
    assert new.count("@evaluation_cutoff") == 1
    assert old.count("@evaluation_cutoff") == 0


def test_v2_read_binds_the_cutoff_and_returns_availability_stamped_observations():
    prediction = prediction_row()
    rows = tuple(observation_row_v2(offset=offset) for offset in range(7))
    client = Client((prediction,), rows)

    result = read(
        client,
        observation_query_version=outcome_reader.OBSERVATION_QUERY_VERSION_V2,
        evaluation_cutoff=CUTOFF,
    )

    assert result.observation_query_version == outcome_reader.OBSERVATION_QUERY_VERSION_V2
    first = result.observations_by_prediction[PREDICTION_ID][0]
    assert first.observed_at == datetime(2026, 8, 26, tzinfo=UTC)
    assert first.available_at == datetime(2026, 8, 27, 0, 40, tzinfo=UTC)
    assert first.run_id == "daily_run_2026-08-26"
    sql, config, _ = client.calls[1]
    assert sql == outcome_reader._observation_sql_v2()
    params = parameters(config)
    assert params["prediction_ids"].values == [PREDICTION_ID]
    assert params["evaluation_cutoff"].value == CUTOFF
    assert params["evaluation_cutoff"].type_ == "TIMESTAMP"
    v1 = read(Client((prediction,), (observation_row(),)))
    assert v1.observation_query_version == outcome_reader.OBSERVATION_QUERY_VERSION_V1
    assert v1.observations_by_prediction[PREDICTION_ID][0].available_at is None


def test_v2_read_refuses_missing_cutoff_late_availability_and_unstamped_rows():
    prediction = prediction_row()
    version = outcome_reader.OBSERVATION_QUERY_VERSION_V2
    with pytest.raises(ValueError, match="evaluation cutoff"):
        read(Client((prediction,), ()), observation_query_version=version)
    with pytest.raises(ValueError, match="evaluation cutoff cannot precede"):
        read(
            Client((prediction,), ()),
            observation_query_version=version,
            evaluation_cutoff=datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="unsupported observation query version"):
        read(Client((prediction,), ()), observation_query_version="outcome_observation_query_v9")
    late = observation_row_v2(available_at=CUTOFF + timedelta(seconds=1))
    with pytest.raises(ValueError, match="after the evaluation cutoff"):
        read(
            Client((prediction,), (late,)),
            observation_query_version=version,
            evaluation_cutoff=CUTOFF,
        )
    with pytest.raises(ValueError, match="observation fields"):
        read(
            Client((prediction,), (observation_row(),)),
            observation_query_version=version,
            evaluation_cutoff=CUTOFF,
        )
    duplicate_day = (
        observation_row_v2(),
        observation_row_v2(observation_run_id="daily_run_corrected"),
    )
    with pytest.raises(ValueError, match="duplicate observation date"):
        read(
            Client((prediction,), duplicate_day),
            observation_query_version=version,
            evaluation_cutoff=CUTOFF,
        )
    with pytest.raises(ValueError, match="observation fields"):
        read(Client((prediction,), (observation_row_v2(),)))


def test_v2_run_ranking_is_taken_among_runs_matching_the_prediction_build():
    """The match precedes the ranking.

    Fixture the probe renders as mixed_build_day: the earliest completed run of the
    day carries hybrid_graph_v2, the next carries the prediction's hybrid_graph_v1.
    The window partitions by build and family map, so the v1 run is rank one in its
    own partition and the join selects it; under a scope, market and day partition
    the v2 run would take rank one and the day would vanish.
    """
    sql = outcome_reader._observation_sql_v2()
    window_start = sql.index("ROW_NUMBER() OVER (")
    window_end = sql.index(") AS eligible_rank")
    window = sql[window_start:window_end]
    partition = window[window.index("PARTITION BY") : window.index("ORDER BY")]
    for column in (
        "r.client_scope_id",
        "run_market",
        "r.signal_date",
        "r.cluster_build_version",
        "r.source_family_map_version",
    ):
        assert column in partition
    assert "ORDER BY r.completed_at, r.run_id" in window
    join_start = sql.index("JOIN eligible_runs AS s")
    join_end = sql.index("JOIN `", join_start + 1)
    join = sql[join_start:join_end]
    assert "AND s.eligible_rank = 1" in join
    assert "AND s.cluster_build_version = p.cluster_build_version" in join
    assert "AND s.source_family_map_version = p.source_family_map_version" in join
    assert "rule_version" not in sql
    assert "observation_method" not in sql
    assert "display_release_state" not in sql


def test_v2_read_refuses_two_runs_for_the_same_snapshot_day():
    """Two eligible runs for one snapshot day are refused, never ranked into one."""
    first = prediction_row()
    second_id = "pred_" + "c" * 64
    second = prediction_row(prediction_id=second_id, signal_id="sig_" + "d" * 64)
    version = outcome_reader.OBSERVATION_QUERY_VERSION_V2
    tied = (
        observation_row_v2(),
        observation_row_v2(second_id, observation_run_id="daily_run_2026-08-26_tied"),
    )
    with pytest.raises(ValueError, match="duplicate_snapshot"):
        read(
            Client((first, second), tied),
            observation_query_version=version,
            evaluation_cutoff=CUTOFF,
        )
    same_run = (observation_row_v2(), observation_row_v2(second_id))
    result = read(
        Client((first, second), same_run),
        observation_query_version=version,
        evaluation_cutoff=CUTOFF,
    )
    assert result.observations_by_prediction[second_id][0].run_id == "daily_run_2026-08-26"
    other_build = prediction_row(
        prediction_id=second_id,
        signal_id="sig_" + "d" * 64,
        cluster_build_version="hybrid_graph_v2",
    )
    result = read(
        Client((first, other_build), tied),
        observation_query_version=version,
        evaluation_cutoff=CUTOFF,
    )
    assert result.observations_by_prediction[second_id][0].run_id == "daily_run_2026-08-26_tied"
    duplicate_day = (
        observation_row_v2(),
        observation_row_v2(observation_run_id="daily_run_corrected"),
    )
    with pytest.raises(ValueError, match="duplicate_snapshot"):
        read(
            Client((first,), duplicate_day),
            observation_query_version=version,
            evaluation_cutoff=CUTOFF,
        )


RUN_IDS = ("outcome_eval_v2_2026-08-31", "outcome_eval_v2_2026-09-01")


def written_row(prediction_id=PREDICTION_ID, run_id=RUN_IDS[0], **overrides):
    row = {
        "prediction_id": prediction_id,
        "client_scope_id": "fixture_scope",
        "run_id": run_id,
        "evaluation_date": date.fromisoformat(run_id.rsplit("_", 1)[1]),
        "outcome": "sustained",
        "source_families": ["news", "reddit"],
    }
    row.update(overrides)
    return row


def read_back(client, **overrides):
    values = {
        "client": client,
        "dataset": "trends_v2_staging",
        "client_scope_id": "fixture_scope",
        "evaluation_run_ids": RUN_IDS,
        "evaluation_date_from": date(2026, 8, 31),
        "evaluation_date_to": date(2026, 9, 6),
    }
    values.update(overrides)
    return outcome_reader.read_written_outcomes(**values)


def test_readback_selects_written_outcomes_by_run_ids_and_scope():
    rows = (written_row(), written_row("pred_" + "c" * 64, RUN_IDS[1], outcome="unresolved"))
    client = Client(rows)

    result = read_back(client)

    assert result == rows
    assert len(client.calls) == 1
    sql, config, location = client.calls[0]
    assert location == "US"
    assert "FROM `ogilvy-trends-v2.trends_v2_staging.signal_outcomes_v2` AS o" in sql
    assert "o.client_scope_id = @client_scope_id" in sql
    assert "o.run_id IN UNNEST(@evaluation_run_ids)" in sql
    assert "o.*" not in sql
    assert f"LIMIT {outcome_reader.READBACK_CEILING + 1}" in sql
    params = parameters(config)
    assert params["client_scope_id"].value == "fixture_scope"
    assert params["evaluation_run_ids"].values == list(RUN_IDS)
    assert client.jobs[0].max_results == outcome_reader.READBACK_CEILING + 1


def test_readback_refuses_foreign_duplicate_malformed_and_unbounded_rows():
    with pytest.raises(ValueError, match="exact staging target"):
        read_back(Client(()), dataset="trends_v2")
    with pytest.raises(ValueError, match="evaluation run"):
        read_back(Client(()), evaluation_run_ids=())
    with pytest.raises(ValueError, match="evaluation run"):
        read_back(Client(()), evaluation_run_ids=(RUN_IDS[0], RUN_IDS[0]))
    with pytest.raises(ValueError, match="readback client scope"):
        read_back(Client((written_row(client_scope_id="other"),)))
    with pytest.raises(ValueError, match="readback run"):
        read_back(Client((written_row(run_id="outcome_eval_v2_2026-09-02"),)))
    with pytest.raises(ValueError, match="duplicate readback"):
        read_back(Client((written_row(), written_row())))
    with pytest.raises(ValueError, match="readback fields"):
        read_back(Client(({**written_row(), "extra": 1},)))
    with pytest.raises(ValueError, match="readback evaluation date"):
        read_back(Client((written_row(evaluation_date="2026-08-31"),)))
    too_many = tuple(
        written_row(f"pred_{index:064x}") for index in range(outcome_reader.READBACK_CEILING + 1)
    )
    with pytest.raises(outcome_reader.OutcomeReadCeilingExceeded, match="readback"):
        read_back(Client(too_many))


def test_readback_is_bounded_to_a_parameterised_evaluation_week():
    client = Client((written_row(),))

    read_back(client)

    sql, config, _ = client.calls[0]
    assert "o.evaluation_date BETWEEN @evaluation_date_from AND @evaluation_date_to" in sql
    assert "2026-08-31" not in sql
    params = parameters(config)
    assert params["evaluation_date_from"].type_ == "DATE"
    assert params["evaluation_date_from"].value == date(2026, 8, 31)
    assert params["evaluation_date_to"].type_ == "DATE"
    assert params["evaluation_date_to"].value == date(2026, 9, 6)


def test_readback_refuses_an_unbounded_or_reversed_range_and_rows_outside_it():
    for start, end in (
        (date(2026, 9, 6), date(2026, 8, 31)),
        (date(2026, 8, 30), date(2026, 9, 6)),
        ("2026-08-31", date(2026, 9, 6)),
        (date(2026, 8, 31), datetime(2026, 9, 6, tzinfo=UTC)),
        (None, date(2026, 9, 6)),
    ):
        client = Client(())
        with pytest.raises(ValueError, match="readback evaluation date range"):
            read_back(client, evaluation_date_from=start, evaluation_date_to=end)
        assert client.calls == []
    with pytest.raises(ValueError, match="readback evaluation date is outside"):
        read_back(Client((written_row(evaluation_date=date(2026, 9, 7)),)))
