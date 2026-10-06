"""Vertex Gemini token-usage ledger: the one basis for what Gemini costs.

Every stage that fires a Vertex Gemini call folds its per-call token counts into
one row per ``(trend_date, consumer, market, gemini_model)`` in the shared
``gemini_usage`` table, and ``scripts/vertex_cost_watchdog.py`` costs the whole
engine off that table alone.

Why one basis
=============
The watchdog used to cost off three different bases at once: it summed token
columns on the three tables that happen to carry them (``trend_analysis``,
``daily_summary``, ``seed_insights``), read this ledger for the two stages that
persist no table, and ESTIMATED the reconcile shadow from a hardcoded per-call
token guess. Every consumer added a fourth way to be wrong, and two of them were
wrong at once: ``comment_sentiment`` wrote no ledger row at all, and
``seed_insights`` stamps one call's tokens onto every seed row it writes, so a
``SUM`` over that table priced a single call five to eight times.

One ledger, one row shape, one reader. A stage that bills and does not appear
here is a metering gap the watchdog now surfaces as a named discrepancy against
the billing export, instead of a quiet under-count.

Not everything Vertex bills fits this shape. ``src/enrichment/embedding_classifier.py``
calls Vertex ``embed_content``, which is priced per billable CHARACTER rather than
per token, so it stays out of this token-shaped ledger and is named in the
watchdog's discrepancy note instead.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Protocol

from src.contracts.open_intelligence import encode_identifier_part

logger = logging.getLogger(__name__)

GEMINI_USAGE_TABLE = "gemini_usage"
STAGING_PROJECT = "ogilvy-trends-v2"
STAGING_DATASET = "trends_v2_staging"
STAGING_LOCATION = "US"
STAGING_SERVICE_ACCOUNT = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
USAGE_EVENT_FIELDS: tuple[str, ...] = (
    "usage_id",
    "trend_date",
    "run_id",
    "consumer",
    "stage",
    "call_index",
    "market",
    "gemini_model",
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "recorded_at",
)

# Every stage that fires a Vertex Gemini call, in report order. The watchdog
# imports this same tuple to decide which consumers it EXPECTS to see, so a
# stage added to one side and forgotten on the other cannot silently go
# uncounted. Adding a new Gemini caller means adding it here.
USAGE_CONSUMERS: tuple[str, ...] = (
    "trend_analysis",
    "daily_summary",
    "seed_insights",
    "comment_sentiment",
    "driving_hashtags",
    "reconcile",
    "dynamic_signal_summary",
    "open_question_answer",
)

_NEW_CONSUMER_STAGES: dict[str, frozenset[str]] = {
    "dynamic_signal_summary": frozenset({"summary"}),
    "open_question_answer": frozenset({"planning", "answering"}),
}


@dataclass(frozen=True, slots=True)
class GeminiUsageEvent:
    usage_id: str
    trend_date: datetime.date
    run_id: str
    consumer: str
    stage: str
    call_index: int
    market: str | None
    gemini_model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    recorded_at: datetime.datetime


class GeminiUsagePersistenceError(RuntimeError):
    """A per-call event was not stored and read back exactly."""


class UsageEventStore(Protocol):
    def merge_and_read(self, event: GeminiUsageEvent) -> Sequence[Mapping[str, Any]]: ...


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _require_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")
    return value


def build_usage_event(
    *,
    trend_date: datetime.date,
    run_id: str,
    consumer: str,
    stage: str,
    call_index: int,
    market: str | None,
    gemini_model: str,
    prompt_tokens: int,
    completion_tokens: int,
    recorded_at: datetime.datetime,
) -> GeminiUsageEvent:
    """Build one immutable token delta for an approved new Gemini call."""
    if isinstance(trend_date, datetime.datetime) or not isinstance(trend_date, datetime.date):
        raise TypeError("trend_date must be a date")
    run_id = _require_nonempty_string(run_id, "run_id")
    consumer = _require_nonempty_string(consumer, "consumer")
    stage = _require_nonempty_string(stage, "stage")
    allowed_stages = _NEW_CONSUMER_STAGES.get(consumer)
    if allowed_stages is None or stage not in allowed_stages:
        raise ValueError(f"consumer {consumer!r} does not allow stage {stage!r}")
    call_index = _require_nonnegative_int(call_index, "call_index")
    if market is not None:
        market = _require_nonempty_string(market, "market")
    gemini_model = _require_nonempty_string(gemini_model, "gemini_model")
    prompt_tokens = _require_nonnegative_int(prompt_tokens, "prompt_tokens")
    completion_tokens = _require_nonnegative_int(completion_tokens, "completion_tokens")
    if not isinstance(recorded_at, datetime.datetime):
        raise TypeError("recorded_at must be a datetime")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone aware")
    recorded_at = recorded_at.astimezone(datetime.UTC)

    identifier_values = (run_id, consumer, stage, call_index, market, gemini_model)
    canonical = "|".join(encode_identifier_part(value) for value in identifier_values)
    usage_id = "usage_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return GeminiUsageEvent(
        usage_id=usage_id,
        trend_date=trend_date,
        run_id=run_id,
        consumer=consumer,
        stage=stage,
        call_index=call_index,
        market=market,
        gemini_model=gemini_model,
        calls=1,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        recorded_at=recorded_at,
    )


def _validate_staging_target(project: str, dataset: str) -> None:
    if project != STAGING_PROJECT or dataset != STAGING_DATASET:
        raise ValueError(
            f"Gemini usage events are staging only: {STAGING_PROJECT}.{STAGING_DATASET}"
        )


def _validate_staging_credentials(
    credentials: Any,
    adc_project: str | None,
    request_factory: Callable[[], Any],
) -> None:
    if adc_project != STAGING_PROJECT:
        raise ValueError("Gemini usage persistence requires the staging ADC project")
    quota_project = getattr(credentials, "quota_project_id", None)
    if quota_project not in (None, STAGING_PROJECT):
        raise ValueError("Gemini usage persistence requires the staging quota project")
    service_account = getattr(credentials, "service_account_email", None)
    if service_account == "default":
        credentials.refresh(request_factory())
        service_account = getattr(credentials, "service_account_email", None)
    if service_account != STAGING_SERVICE_ACCOUNT:
        raise ValueError("Gemini usage persistence requires the staging service account")


def _validate_staging_client(client: Any, credentials: Any) -> None:
    if client.project != STAGING_PROJECT or client.location != STAGING_LOCATION:
        raise ValueError("Gemini usage persistence client is outside the staging target")
    client_credentials = getattr(client, "_credentials", None)
    if client_credentials is not credentials:
        raise ValueError("Gemini usage persistence client credentials do not match staging ADC")
    if getattr(client_credentials, "service_account_email", None) != STAGING_SERVICE_ACCOUNT:
        raise ValueError("Gemini usage persistence client has the wrong staging identity")
    if getattr(client_credentials, "quota_project_id", None) not in (None, STAGING_PROJECT):
        raise ValueError("Gemini usage persistence client has the wrong staging quota project")


class BigQueryUsageEventStore:
    """Insert one event when absent, then read that exact event back."""

    _FIELDS = USAGE_EVENT_FIELDS
    _PARAMETER_TYPES: ClassVar[dict[str, str]] = {
        "usage_id": "STRING",
        "trend_date": "DATE",
        "run_id": "STRING",
        "consumer": "STRING",
        "stage": "STRING",
        "call_index": "INT64",
        "market": "STRING",
        "gemini_model": "STRING",
        "calls": "INT64",
        "prompt_tokens": "INT64",
        "completion_tokens": "INT64",
        "recorded_at": "TIMESTAMP",
    }

    def __init__(
        self,
        client: Any,
        *,
        project: str,
        dataset: str,
        credentials: Any | None = None,
    ) -> None:
        _validate_staging_target(project, dataset)
        credentials = getattr(client, "_credentials", None) if credentials is None else credentials
        _validate_staging_client(client, credentials)
        self._client = client
        self._credentials = credentials
        self._table = f"{project}.{dataset}.{GEMINI_USAGE_TABLE}"

    def merge_and_read(self, event: GeminiUsageEvent) -> list[dict[str, Any]]:
        from google.cloud import bigquery

        _validate_staging_client(self._client, self._credentials)
        values = asdict(event)
        parameters = [
            bigquery.ScalarQueryParameter(name, self._PARAMETER_TYPES[name], values[name])
            for name in self._FIELDS
        ]
        source_values = ",\n    ".join(f"@{name} AS {name}" for name in self._FIELDS)
        insert_fields = ", ".join(self._FIELDS)
        insert_values = ", ".join(f"source.{name}" for name in self._FIELDS)
        merge_sql = f"""
MERGE `{self._table}` AS target
USING (
  SELECT
    {source_values}
) AS source
ON target.usage_id = source.usage_id
WHEN NOT MATCHED THEN
  INSERT ({insert_fields})
  VALUES ({insert_values})
""".strip()
        merge_config = bigquery.QueryJobConfig(query_parameters=parameters)
        self._client.query(
            merge_sql,
            job_config=merge_config,
            location=STAGING_LOCATION,
        ).result()

        selected_fields = ", ".join(self._FIELDS)
        read_sql = f"""
SELECT {selected_fields}
FROM `{self._table}`
WHERE usage_id = @usage_id
""".strip()
        read_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("usage_id", "STRING", event.usage_id)]
        )
        rows = self._client.query(
            read_sql,
            job_config=read_config,
            location=STAGING_LOCATION,
        ).result()
        return [dict(row.items()) for row in rows]


def persist_usage_event(
    event: GeminiUsageEvent,
    *,
    store: UsageEventStore | None = None,
    project: str = STAGING_PROJECT,
    dataset: str = STAGING_DATASET,
    credentials_loader: Callable[..., tuple[Any, str | None]] | None = None,
    client_factory: Callable[..., Any] | None = None,
    request_factory: Callable[[], Any] | None = None,
) -> GeminiUsageEvent:
    """Persist and acknowledge one immutable event, or raise a typed error."""
    if not isinstance(event, GeminiUsageEvent):
        raise TypeError("event must be a GeminiUsageEvent")
    if store is None:
        _validate_staging_target(project, dataset)
        import google.auth
        from google.auth.transport.requests import Request
        from google.cloud import bigquery

        credentials_loader = credentials_loader or google.auth.default
        client_factory = client_factory or bigquery.Client
        request_factory = request_factory or Request
        credentials, adc_project = credentials_loader(scopes=bigquery.Client.SCOPE)
        _validate_staging_credentials(credentials, adc_project, request_factory)
        client = client_factory(
            project=project,
            location=STAGING_LOCATION,
            credentials=credentials,
        )
        _validate_staging_client(client, credentials)
        store = BigQueryUsageEventStore(
            client,
            project=project,
            dataset=dataset,
            credentials=credentials,
        )

    try:
        rows = list(store.merge_and_read(event))
    except Exception as exc:
        raise GeminiUsagePersistenceError(
            f"Gemini usage event persistence failed for {event.usage_id}"
        ) from exc
    if len(rows) != 1:
        raise GeminiUsagePersistenceError(
            f"Gemini usage event readback returned {len(rows)} rows for {event.usage_id}"
        )
    expected = asdict(event)
    observed = dict(rows[0])
    if observed != expected:
        raise GeminiUsagePersistenceError(
            f"Gemini usage event readback mismatch for {event.usage_id}"
        )
    return event


# A sink takes the built ledger rows and lands them somewhere. Defaults to the
# BigQuery writer; tests pass a list-appending sink so no test touches BQ.
UsageSink = Callable[[list[dict]], int]


def record_usage(tally: dict, market: str | None, response: Any) -> bool:
    """Fold one Gemini response's token counts into ``tally``; True when counted.

    Keyed by ``(market, model)`` so a window spanning a model cutover prices
    each side at its own rate. ``market`` is None for the cross-market stages
    (``daily_summary``, ``seed_insights``), whose tables carry a ``markets``
    array rather than one market.

    A response carrying NO usage metadata (a failed call, an empty model
    response, or a test stub) counts zero tokens and is NOT recorded: a
    zero-token row would inflate the ``calls`` column the watchdog reads while
    adding no spend, which is a worse lie than the silence it replaces.
    """
    prompt = int(getattr(response, "prompt_tokens", 0) or 0)
    completion = int(getattr(response, "completion_tokens", 0) or 0)
    if prompt <= 0 and completion <= 0:
        return False
    key = (market, getattr(response, "model", None) or None)
    entry = tally.setdefault(key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    entry["calls"] += 1
    entry["prompt_tokens"] += prompt
    entry["completion_tokens"] += completion
    return True


def usage_rows(trend_date: datetime.date, consumer: str, tally: dict) -> list[dict]:
    """Build the gemini_usage rows for one stage's tally. Pure."""
    rows: list[dict] = []
    now = datetime.datetime.now(datetime.UTC)
    for (market, model), t in sorted(
        tally.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")
    ):
        rows.append(
            {
                "usage_id": str(uuid.uuid4()),
                "trend_date": trend_date,
                "consumer": consumer,
                "market": market,
                "gemini_model": model,
                "calls": int(t["calls"]),
                "prompt_tokens": int(t["prompt_tokens"]),
                "completion_tokens": int(t["completion_tokens"]),
                "recorded_at": now,
            }
        )
    return rows


def persist_gemini_usage(rows: list[dict]) -> int:
    """Append usage rows to the gemini_usage ledger; return rows written.

    Non-fatal by contract, like the producers that call it: a missing table or
    a BQ outage logs and returns 0 rather than failing a stage whose output
    already shipped. An empty row list never touches BigQuery.
    """
    if not rows:
        return 0
    try:
        import pandas as pd

        from src.utils.bigquery import insert_dataframe

        return int(insert_dataframe(pd.DataFrame(rows), GEMINI_USAGE_TABLE))
    except Exception as exc:  # non-fatal: cost accounting must not break the pass
        logger.warning("Gemini usage ledger write failed (non-fatal): %s", exc)
        return 0


__all__ = [
    "GEMINI_USAGE_TABLE",
    "STAGING_DATASET",
    "STAGING_LOCATION",
    "STAGING_PROJECT",
    "STAGING_SERVICE_ACCOUNT",
    "USAGE_CONSUMERS",
    "USAGE_EVENT_FIELDS",
    "BigQueryUsageEventStore",
    "GeminiUsageEvent",
    "GeminiUsagePersistenceError",
    "UsageEventStore",
    "UsageSink",
    "build_usage_event",
    "persist_gemini_usage",
    "persist_usage_event",
    "record_usage",
    "usage_rows",
]
