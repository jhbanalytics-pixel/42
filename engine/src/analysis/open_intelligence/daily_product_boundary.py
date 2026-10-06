"""The native boundary the daily products run over: captured source rows, sinks and models.

``native_product_io_factory`` is the history I/O factory ``DailyProducts`` requires. It is
called only after admission, with the admitted execution, the validated capture entry and
the closed cutoff, and it returns the boundary ``BoundedProductIO`` wraps:

``enriched`` is read once from the capture's own enriched snapshot table of the closed day,
through the dynamic meter already bound to the run's product budget, so the read is
metered, read only and capped like every other product query. The table must be one the
admitted capture declared, so a table of another day or estate is never read.
``engagement_weighted`` is not a stored column; it is recomputed with the enrichment
stage's own damping, anchored at the cutoff rather than the wall clock so a retry of the
same day computes the same rows.

``sink(table)`` appends through a load job with no transport retries and returns the
count the job reports; ``BoundedProductIO`` makes a same day retry idempotent by
replacing only the rows the operation recorded writing, before any stage runs.

``models`` is the Vertex SDK with transport retries disabled, created only when the
admitted origin funds at least one model call; otherwise every use refuses.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

DATASET = "trends_v2_staging"
MODEL_LOCATION = "global"
_MARKETS = ("za", "ng", "ke")


def _json_value(value):
    if isinstance(value, dict):
        return {name: _json_value(item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, date | datetime):
        return value.isoformat()
    if type(value).__module__ == "numpy" and hasattr(value, "item"):
        return _json_value(value.item())
    return value


class UnfundedModels:
    """The model seam of an origin that funds no model call."""

    def generate_content(self, **kwargs):
        raise ValueError("products_model_budget_unfunded")


def _engagement_weighted(frame, *, now):
    from src.ingestion.enrichment import engagement_per_day, engagement_weight_for
    from src.utils.config_loader import load_scoring

    scoring = load_scoring()
    weights = scoring.get("engagement_weights", {}) or {}
    cap = float(scoring.get("engagement_per_day_cap") or 0) or None

    def dampen(row):
        raw = float(row.get("engagement_total") or 0.0)
        if raw <= 0:
            return 0.0
        per_day = engagement_per_day(raw, row.get("published_at"), now=now)
        if cap is not None and per_day > cap:
            per_day = cap
        return per_day * engagement_weight_for(str(row.get("content_type") or ""), weights)

    if frame.empty:
        return frame.assign(engagement_weighted=[])
    return frame.assign(engagement_weighted=frame.apply(dampen, axis=1))


def captured_source_rows(meter, *, capture, cutoff):
    """The closed day's enriched rows from the capture's own snapshot, one per market and id."""
    import pandas as pd
    from google.cloud import bigquery

    from .production_snapshot_tables import snapshot_destination_table

    day = (cutoff - timedelta(microseconds=1)).date()
    table = snapshot_destination_table(day, "enriched_content")
    if table not in capture["snapshot_tables"]:
        raise ValueError("products_capture_source_undeclared")
    sql = (
        f"SELECT * FROM `{meter.project}.{DATASET}.{table}`\n"
        "WHERE DATE(collected_at) = @trend_date AND collected_at < @cutoff\n"
        "  AND market IN UNNEST(@markets)\n"
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY market, id ORDER BY collected_at DESC, "
        "source, platform) = 1"
    )
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("trend_date", "DATE", day),
            bigquery.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff),
            bigquery.ArrayQueryParameter("markets", "STRING", list(_MARKETS)),
        ]
    )
    frame = pd.DataFrame([dict(row) for row in meter.query(sql, job_config=config).result()])
    if frame.empty:
        from .daily_products import REQUIRED_SOURCE_COLUMNS

        frame = pd.DataFrame(columns=[*REQUIRED_SOURCE_COLUMNS, "engagement_total"])
    return _engagement_weighted(
        frame.drop(columns=["engagement_weighted"], errors="ignore"), now=cutoff
    )


class NativeProductBoundary:
    def __init__(self, *, warehouse, enriched, models):
        self.dataset = DATASET
        self.client = warehouse
        self.enriched = enriched
        self.models = models

    def sink(self, table):
        def write(rows):
            rows = [_json_value(dict(row)) for row in rows]
            if not rows:
                return 0
            from google.cloud import bigquery

            config = bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
            )
            job = self.client.load_table_from_json(
                rows,
                f"{self.client.project}.{DATASET}.{table}",
                job_config=config,
                num_retries=0,
                location=self.client.location,
                timeout=60,
            )
            job.result(retry=None, timeout=60)
            if getattr(job, "errors", None):
                raise ValueError("products_write_failed:" + table)
            return job.output_rows

        return write


def native_product_io_factory(*, warehouse, meter, model_sdk=None):
    """The factory ``DailyProducts`` calls once per admitted operation."""

    def factory(*, admission, capture, cutoff):
        manifest = admission.manifest
        if manifest.datasets != (DATASET,):
            raise ValueError("products_target_invalid")
        enriched = captured_source_rows(meter, capture=capture, cutoff=cutoff)
        if manifest.limits["max_model_calls"] > 0:
            from .daily_product_io import native_model_sdk

            build = native_model_sdk if model_sdk is None else model_sdk
            models = build(project=manifest.project, location=MODEL_LOCATION).models
        else:
            models = UnfundedModels()
        return NativeProductBoundary(warehouse=warehouse, enriched=enriched, models=models)

    return factory
