"""Bounded staging reader for due outcome evaluation inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType

from google.cloud import bigquery

from src.analysis.open_intelligence.outcomes import OutcomeObservation, validate_prediction_row
from src.analysis.open_intelligence.predictions import PREDICTION_ROW_FIELDS

TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging"
TARGET_LOCATION = "US"
PREDICTION_CEILING = 500
OBSERVATION_CEILING = 5000
OBSERVATION_FIELDS = (
    "prediction_id",
    "observed_at",
    "velocity",
    "breadth",
    "evidence_family_count",
)
OBSERVATION_QUERY_VERSION_V1 = "outcome_observation_query_v1"
OBSERVATION_QUERY_VERSION_V2 = "outcome_observation_query_v2"
OBSERVATION_QUERY_VERSIONS = (OBSERVATION_QUERY_VERSION_V1, OBSERVATION_QUERY_VERSION_V2)
OBSERVATION_FIELDS_V2 = (*OBSERVATION_FIELDS, "available_at", "observation_run_id")
RUN_RECEIPT_TABLE = "open_intelligence_run_receipts_v1"
# The read back after one authorised write selects the outcome rows of the week's
# evaluation runs only: seven daily runs, each bounded by the prediction ceiling.
READBACK_FIELDS = (
    "prediction_id",
    "client_scope_id",
    "run_id",
    "evaluation_date",
    "outcome",
    "source_families",
)
READBACK_CEILING = PREDICTION_CEILING * 7
# signal_outcomes_v2 is partitioned by evaluation_date; a readback names the
# cohort week it wrote, so it never scans beyond seven partitions.
READBACK_MAXIMUM_DAYS = 7


class OutcomeReadCeilingExceeded(ValueError):
    """A bounded outcome reader query returned ceiling plus one rows."""


@dataclass(frozen=True, slots=True)
class OutcomeReadResult:
    predictions: tuple[Mapping[str, object], ...]
    observations_by_prediction: Mapping[str, tuple[OutcomeObservation, ...]]
    missing_prediction_ids: tuple[str, ...]
    observation_query_version: str = OBSERVATION_QUERY_VERSION_V1


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _date(value: object, field: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{field} must be a date")
    return value


def _row_mapping(row: object, field: str) -> dict[str, object]:
    if isinstance(row, Mapping):
        return dict(row)
    items = getattr(row, "items", None)
    if not callable(items):
        raise ValueError(f"{field} is not a mapping")
    return dict(items())


def _validate_target(client: object, dataset: object) -> None:
    if getattr(client, "project", None) != TARGET_PROJECT or dataset != TARGET_DATASET:
        raise ValueError("exact staging target is required")


def _prediction_config(
    evaluation_date: date, client_scope_id: str, evaluation_run_id: str
) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        use_legacy_sql=False,
        query_parameters=[
            bigquery.ScalarQueryParameter("evaluation_date", "DATE", evaluation_date),
            bigquery.ScalarQueryParameter("client_scope_id", "STRING", client_scope_id),
            bigquery.ScalarQueryParameter("evaluation_run_id", "STRING", evaluation_run_id),
        ],
    )


def _observation_config(prediction_ids: tuple[str, ...]) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        use_legacy_sql=False,
        query_parameters=[
            bigquery.ArrayQueryParameter("prediction_ids", "STRING", list(prediction_ids))
        ],
    )


def _observation_config_v2(
    prediction_ids: tuple[str, ...], evaluation_cutoff: datetime
) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        use_legacy_sql=False,
        query_parameters=[
            bigquery.ArrayQueryParameter("prediction_ids", "STRING", list(prediction_ids)),
            bigquery.ScalarQueryParameter("evaluation_cutoff", "TIMESTAMP", evaluation_cutoff),
        ],
    )


def _prediction_sql() -> str:
    columns = ", ".join(f"p.{field}" for field in PREDICTION_ROW_FIELDS)
    return (
        f"SELECT {columns}\n"
        f"FROM `{TARGET_PROJECT}.{TARGET_DATASET}.signal_predictions_v2` AS p\n"
        "WHERE p.evaluation_date = @evaluation_date\n"
        "  AND p.client_scope_id = @client_scope_id\n"
        "  AND NOT EXISTS (\n"
        "    SELECT 1\n"
        f"    FROM `{TARGET_PROJECT}.{TARGET_DATASET}.signal_outcomes_v2` AS o\n"
        "    WHERE o.prediction_id = p.prediction_id\n"
        "      AND o.evaluation_date = p.evaluation_date\n"
        "      AND o.run_id = @evaluation_run_id\n"
        "  )\n"
        "ORDER BY p.market, p.prediction_id\n"
        f"LIMIT {PREDICTION_CEILING + 1}"
    )


def _observation_sql() -> str:
    return (
        "SELECT p.prediction_id, TIMESTAMP(c.signal_date) AS observed_at,\n"
        "  c.velocity_score AS velocity, c.breadth_score AS breadth,\n"
        "  COUNT(DISTINCT IF(\n"
        "    e.availability = 'available' AND e.published_at IS NOT NULL,\n"
        "    e.source_family, NULL\n"
        "  )) AS evidence_family_count\n"
        f"FROM `{TARGET_PROJECT}.{TARGET_DATASET}.signal_predictions_v2` AS p\n"
        f"JOIN `{TARGET_PROJECT}.{TARGET_DATASET}.signal_candidates_v2` AS c\n"
        "  ON c.client_scope_id = p.client_scope_id\n"
        "  AND c.market = p.market\n"
        "  AND c.signal_id = p.signal_id\n"
        f"LEFT JOIN `{TARGET_PROJECT}.{TARGET_DATASET}.signal_evidence_v2` AS e\n"
        "  ON e.client_scope_id = c.client_scope_id\n"
        "  AND e.signal_date = c.signal_date\n"
        "  AND e.market = c.market\n"
        "  AND e.signal_id = c.signal_id\n"
        "  AND e.run_id = c.run_id\n"
        "WHERE p.prediction_id IN UNNEST(@prediction_ids)\n"
        "  AND c.signal_date > p.signal_date\n"
        "  AND c.signal_date <= p.evaluation_date\n"
        "GROUP BY p.prediction_id, observed_at, c.run_id, velocity, breadth\n"
        "ORDER BY p.prediction_id, observed_at, c.run_id\n"
        f"LIMIT {OBSERVATION_CEILING + 1}"
    )


def _observation_sql_v2() -> str:
    """Observation query under outcome_observation_query_v2.

    One completed daily snapshot per client scope, market and day is selected
    from the run receipt table: complete partitions, completed no later than the
    evaluation cutoff, and the first run by completion time among the runs that
    carry the prediction's identity build and family map. The match precedes the
    ranking: the window is partitioned by build and family map as well as scope,
    market and day, so an earlier run under another build never blanks the day.
    Candidates and evidence join to that run only, and a receipt counts only when
    published within its own snapshot day. The v1 text above is retained byte for
    byte for the retained probe.
    """
    table = f"`{TARGET_PROJECT}.{TARGET_DATASET}"
    return (
        "WITH eligible_runs AS (\n"
        "  SELECT r.client_scope_id, run_market AS market, r.signal_date, r.run_id,\n"
        "    r.completed_at, r.cluster_build_version, r.source_family_map_version,\n"
        "    ROW_NUMBER() OVER (\n"
        "      PARTITION BY r.client_scope_id, run_market, r.signal_date,\n"
        "        r.cluster_build_version, r.source_family_map_version\n"
        "      ORDER BY r.completed_at, r.run_id\n"
        "    ) AS eligible_rank\n"
        f"  FROM {table}.{RUN_RECEIPT_TABLE}` AS r,\n"
        "    UNNEST(r.market_scope) AS run_market\n"
        "  WHERE r.status = 'completed'\n"
        "    AND r.complete_partitions\n"
        "    AND r.completed_at <= @evaluation_cutoff\n"
        ")\n"
        "SELECT p.prediction_id, TIMESTAMP(c.signal_date) AS observed_at,\n"
        "  c.velocity_score AS velocity, c.breadth_score AS breadth,\n"
        "  COUNT(DISTINCT IF(\n"
        "    e.availability = 'available' AND e.published_at IS NOT NULL\n"
        "      AND e.published_at < TIMESTAMP(DATE_ADD(c.signal_date, INTERVAL 1 DAY)),\n"
        "    e.source_family, NULL\n"
        "  )) AS evidence_family_count,\n"
        "  s.completed_at AS available_at, s.run_id AS observation_run_id\n"
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
        "  AND c.run_id = s.run_id\n"
        f"LEFT JOIN {table}.signal_evidence_v2` AS e\n"
        "  ON e.client_scope_id = c.client_scope_id\n"
        "  AND e.signal_date = c.signal_date\n"
        "  AND e.market = c.market\n"
        "  AND e.signal_id = c.signal_id\n"
        "  AND e.run_id = c.run_id\n"
        "WHERE p.prediction_id IN UNNEST(@prediction_ids)\n"
        "GROUP BY p.prediction_id, observed_at, c.run_id, velocity, breadth, available_at,"
        " observation_run_id\n"
        "ORDER BY p.prediction_id, observed_at, c.run_id\n"
        f"LIMIT {OBSERVATION_CEILING + 1}"
    )


def _validate_prediction(
    row: object,
    *,
    evaluation_date: date,
    client_scope_id: str,
) -> Mapping[str, object]:
    mapped = _row_mapping(row, "prediction row")
    if set(mapped) != set(PREDICTION_ROW_FIELDS):
        raise ValueError("prediction fields are invalid")
    contract = validate_prediction_row(mapped)
    prediction_id = contract["prediction_id"]
    if contract["evaluation_date"] != evaluation_date:
        raise ValueError("prediction evaluation date is invalid")
    if contract["client_scope_id"] != client_scope_id:
        raise ValueError("prediction client scope is invalid")
    signal_date = contract["signal_date"]
    if signal_date >= evaluation_date:
        raise ValueError("prediction window is invalid")
    mapped["prediction_id"] = prediction_id
    return MappingProxyType({field: mapped[field] for field in PREDICTION_ROW_FIELDS})


def _admit_snapshot(
    snapshots: dict[tuple[object, ...], str],
    prediction: Mapping[str, object],
    observed_date: date,
    observation_run_id: str,
) -> None:
    """Hold one run per snapshot day and refuse a second one.

    The key is the one the v2 query ranks within: client scope, market, day,
    identity build and family map. Two runs under one key are a duplicate daily
    snapshot, refused by name rather than ranked into a single winner.
    """
    key = (
        prediction["client_scope_id"],
        prediction["market"],
        observed_date,
        prediction["cluster_build_version"],
        prediction["source_family_map_version"],
    )
    held = snapshots.setdefault(key, observation_run_id)
    if held != observation_run_id:
        raise ValueError("duplicate_snapshot")


def read_due_outcome_inputs(
    *,
    client: object,
    dataset: str,
    evaluation_date: date,
    client_scope_id: str,
    evaluation_run_id: str,
    observation_query_version: str = OBSERVATION_QUERY_VERSION_V1,
    evaluation_cutoff: datetime | None = None,
) -> OutcomeReadResult:
    """Read due predictions and exact-window observations from staging only."""
    _validate_target(client, dataset)
    evaluation_date = _date(evaluation_date, "evaluation date")
    client_scope_id = _text(client_scope_id, "client scope")
    evaluation_run_id = _text(evaluation_run_id, "evaluation run")
    if observation_query_version not in OBSERVATION_QUERY_VERSIONS:
        raise ValueError("unsupported observation query version")
    versioned = observation_query_version == OBSERVATION_QUERY_VERSION_V2
    if versioned:
        if (
            not isinstance(evaluation_cutoff, datetime)
            or evaluation_cutoff.tzinfo is None
            or evaluation_cutoff.utcoffset() is None
        ):
            raise ValueError("evaluation cutoff must be a timezone aware timestamp")
        evaluation_cutoff = evaluation_cutoff.astimezone(UTC)
        if evaluation_cutoff.date() < evaluation_date:
            raise ValueError("evaluation cutoff cannot precede the evaluation date")
    elif evaluation_cutoff is not None:
        raise ValueError("evaluation cutoff requires outcome_observation_query_v2")
    observation_fields = OBSERVATION_FIELDS_V2 if versioned else OBSERVATION_FIELDS

    prediction_job = client.query(
        _prediction_sql(),
        job_config=_prediction_config(evaluation_date, client_scope_id, evaluation_run_id),
        location=TARGET_LOCATION,
    )
    prediction_rows = tuple(prediction_job.result(max_results=PREDICTION_CEILING + 1))
    if len(prediction_rows) > PREDICTION_CEILING:
        raise OutcomeReadCeilingExceeded(f"prediction rows exceeded ceiling {PREDICTION_CEILING}")
    predictions = tuple(
        _validate_prediction(
            row,
            evaluation_date=evaluation_date,
            client_scope_id=client_scope_id,
        )
        for row in prediction_rows
    )
    prediction_ids = tuple(row["prediction_id"] for row in predictions)
    if len(prediction_ids) != len(set(prediction_ids)):
        raise ValueError("duplicate prediction id")
    if not predictions:
        return OutcomeReadResult((), MappingProxyType({}), (), observation_query_version)

    observation_job = client.query(
        _observation_sql_v2() if versioned else _observation_sql(),
        job_config=(
            _observation_config_v2(prediction_ids, evaluation_cutoff)
            if versioned
            else _observation_config(prediction_ids)
        ),
        location=TARGET_LOCATION,
    )
    observation_rows = tuple(observation_job.result(max_results=OBSERVATION_CEILING + 1))
    if len(observation_rows) > OBSERVATION_CEILING:
        raise OutcomeReadCeilingExceeded(f"observation rows exceeded ceiling {OBSERVATION_CEILING}")

    predictions_by_id = {row["prediction_id"]: row for row in predictions}
    grouped: dict[str, list[OutcomeObservation]] = {
        prediction_id: [] for prediction_id in prediction_ids
    }
    seen_dates: dict[str, set[date]] = {prediction_id: set() for prediction_id in prediction_ids}
    snapshots: dict[tuple[object, ...], str] = {}
    for raw_row in observation_rows:
        row = _row_mapping(raw_row, "observation row")
        if set(row) != set(observation_fields):
            raise ValueError("observation fields are invalid")
        prediction_id = _text(row["prediction_id"], "observation prediction id")
        prediction = predictions_by_id.get(prediction_id)
        if prediction is None:
            raise ValueError("observation references unknown prediction")
        observed_at = row["observed_at"]
        if (
            not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
        ):
            raise ValueError("observation timestamp must be timezone aware")
        observed_at = observed_at.astimezone(UTC)
        observed_date = observed_at.date()
        if not prediction["signal_date"] < observed_date <= prediction["evaluation_date"]:
            raise ValueError("observation is outside prediction window")
        if observed_date in seen_dates[prediction_id]:
            if versioned:
                raise ValueError("duplicate_snapshot: duplicate observation date")
            raise ValueError("duplicate observation date")
        seen_dates[prediction_id].add(observed_date)
        availability: dict[str, object] = {}
        if versioned:
            available_at = row["available_at"]
            if (
                not isinstance(available_at, datetime)
                or available_at.tzinfo is None
                or available_at.utcoffset() is None
            ):
                raise ValueError("observation availability must be timezone aware")
            available_at = available_at.astimezone(UTC)
            if available_at > evaluation_cutoff:
                raise ValueError("observation availability is after the evaluation cutoff")
            observation_run_id = _text(row["observation_run_id"], "observation run id")
            _admit_snapshot(snapshots, prediction, observed_date, observation_run_id)
            availability = {
                "available_at": available_at,
                "run_id": observation_run_id,
            }
        grouped[prediction_id].append(
            OutcomeObservation(
                observed_at=observed_at,
                velocity=row["velocity"],
                breadth=row["breadth"],
                evidence_family_count=row["evidence_family_count"],
                **availability,
            )
        )

    normalized = MappingProxyType(
        {
            prediction_id: tuple(sorted(grouped[prediction_id], key=lambda item: item.observed_at))
            for prediction_id in prediction_ids
        }
    )
    missing = tuple(
        prediction_id for prediction_id in prediction_ids if not normalized[prediction_id]
    )
    return OutcomeReadResult(predictions, normalized, missing, observation_query_version)


def _readback_sql() -> str:
    columns = ", ".join(f"o.{field}" for field in READBACK_FIELDS)
    return (
        f"SELECT {columns}\n"
        f"FROM `{TARGET_PROJECT}.{TARGET_DATASET}.signal_outcomes_v2` AS o\n"
        "WHERE o.client_scope_id = @client_scope_id\n"
        "  AND o.run_id IN UNNEST(@evaluation_run_ids)\n"
        "  AND o.evaluation_date BETWEEN @evaluation_date_from AND @evaluation_date_to\n"
        "ORDER BY o.run_id, o.prediction_id\n"
        f"LIMIT {READBACK_CEILING + 1}"
    )


def read_written_outcomes(
    *,
    client: object,
    dataset: str,
    client_scope_id: str,
    evaluation_run_ids: tuple[str, ...],
    evaluation_date_from: date,
    evaluation_date_to: date,
) -> tuple[Mapping[str, object], ...]:
    """Read back the outcome rows one authorised write produced.

    Rows are selected by the exact evaluation runs and client scope the write
    used, and every row is checked against both, so a row of another run or
    another client can never be counted as this write's result. The read is
    bounded to the evaluation dates of the cohort week, at most seven.
    """
    _validate_target(client, dataset)
    if (
        isinstance(evaluation_date_from, datetime)
        or isinstance(evaluation_date_to, datetime)
        or not isinstance(evaluation_date_from, date)
        or not isinstance(evaluation_date_to, date)
        or evaluation_date_from > evaluation_date_to
        or (evaluation_date_to - evaluation_date_from).days >= READBACK_MAXIMUM_DAYS
    ):
        raise ValueError("readback evaluation date range is invalid")
    client_scope_id = _text(client_scope_id, "client scope")
    if not isinstance(evaluation_run_ids, (tuple, list)) or not evaluation_run_ids:
        raise ValueError("evaluation run ids must be a nonempty sequence")
    run_ids = tuple(_text(run_id, "evaluation run") for run_id in evaluation_run_ids)
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("evaluation run ids must be distinct")
    job = client.query(
        _readback_sql(),
        job_config=bigquery.QueryJobConfig(
            use_legacy_sql=False,
            query_parameters=[
                bigquery.ScalarQueryParameter("client_scope_id", "STRING", client_scope_id),
                bigquery.ArrayQueryParameter("evaluation_run_ids", "STRING", list(run_ids)),
                bigquery.ScalarQueryParameter("evaluation_date_from", "DATE", evaluation_date_from),
                bigquery.ScalarQueryParameter("evaluation_date_to", "DATE", evaluation_date_to),
            ],
        ),
        location=TARGET_LOCATION,
    )
    raw_rows = tuple(job.result(max_results=READBACK_CEILING + 1))
    if len(raw_rows) > READBACK_CEILING:
        raise OutcomeReadCeilingExceeded(f"readback rows exceeded ceiling {READBACK_CEILING}")
    expected_runs = set(run_ids)
    seen: set[tuple[str, str]] = set()
    rows = []
    for raw_row in raw_rows:
        row = _row_mapping(raw_row, "readback row")
        if set(row) != set(READBACK_FIELDS):
            raise ValueError("readback fields are invalid")
        if _text(row["client_scope_id"], "readback client scope") != client_scope_id:
            raise ValueError("readback client scope is invalid")
        run_id = _text(row["run_id"], "readback run")
        if run_id not in expected_runs:
            raise ValueError("readback run is not one of the requested runs")
        prediction_id = _text(row["prediction_id"], "readback prediction id")
        written_date = _date(row["evaluation_date"], "readback evaluation date")
        if not evaluation_date_from <= written_date <= evaluation_date_to:
            raise ValueError("readback evaluation date is outside the requested range")
        if (prediction_id, run_id) in seen:
            raise ValueError("duplicate readback outcome")
        seen.add((prediction_id, run_id))
        rows.append(MappingProxyType({field: row[field] for field in READBACK_FIELDS}))
    return tuple(rows)
