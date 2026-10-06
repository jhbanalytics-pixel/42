import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


TABLE = "`ogilvy-trends-v2.intelligence_42_core.post_observations`"
MARKETS = ("ZA", "NG", "KE")
EXPECTED_BUILDER_EMAIL = "f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com"
MAX_BYTES_BILLED = 5 * 1024**3

_PROTOCOL_RULES = (
    (
        "tiktok/trending",
        r"^tiktok/trending\?feed=local&region=(ZA|NG|KE)$",
    ),
    (
        "youtube/videos/trending",
        r"^youtube/videos/trending\?(?:category=[0-9]+&)?(?:language=[A-Za-z0-9-]+&)?(?:max_results=[0-9]+&)?region=(ZA|NG|KE)$",
    ),
    (
        "tiktok/search/hashtag",
        r"^tiktok/search/hashtag\?(?:max_age_days=[0-9]+&)?(?:min_views=[0-9]+&)?region=(ZA|NG|KE)(?:&sort_rows=[A-Za-z0-9_-]+)?$",
    ),
)
_REGION_EXTRACT_PATTERN = r"region=(ZA|NG|KE)"
_ROW_FIELDS = (
    "post_id",
    "observed_at",
    "observed_date",
    "market",
    "source_region",
    "platform",
    "route",
    "series",
    "protocol",
    "lane",
    "lane_class",
    "seed_key",
    "pull_seq",
    "rank",
    "views",
    "likes",
    "comments",
    "shares",
    "run_id",
)


def source_market_candidate(row):
    if row.get("source_market") is not None:
        return None
    protocol = row.get("protocol")
    if not isinstance(protocol, str):
        return None
    for known_route, pattern in _PROTOCOL_RULES:
        if row.get("route") != known_route:
            continue
        match = re.fullmatch(pattern, protocol)
        if match is None:
            return None
        candidate = match.group(1)
        if row.get("market") != candidate:
            return None
        source_region = row.get("source_region")
        if source_region is not None and source_region != candidate:
            return None
        return candidate
    return None


def _sql_candidate(alias):
    cases = []
    for route, pattern in _PROTOCOL_RULES:
        cases.append(
            f"WHEN {alias}.route = '{route}' "
            f"AND REGEXP_CONTAINS({alias}.protocol, r'{pattern}') "
            f"THEN REGEXP_EXTRACT({alias}.protocol, r'{_REGION_EXTRACT_PATTERN}')"
        )
    return "CASE " + " ".join(cases) + " END"


def _partition_predicate(alias):
    return (
        f"{alias}.observed_date <= @cutoff_date "
        f"AND {alias}.observed_at < @cutoff_timestamp"
    )


def _partition_date_cap(cutoff_timestamp):
    # observed_date is market-local, and Kenya is the latest supported zone at UTC+3.
    return cutoff_timestamp.astimezone(ZoneInfo("Africa/Nairobi")).date()


def build_update_sql():
    candidate = _sql_candidate("o")
    partition = _partition_predicate("o")
    return f"""UPDATE {TABLE} AS o
SET source_market = {candidate}
WHERE o.source_market IS NULL
  AND {partition}
  AND ({candidate}) IS NOT NULL
  AND o.market = ({candidate})
  AND (o.source_region IS NULL OR o.source_region = ({candidate}))"""


def build_preview_sql():
    candidate = _sql_candidate("o")
    partition = _partition_predicate("o")
    return f"""WITH candidates AS (
  SELECT o.post_id, o.source_market, o.source_region, o.market,
    {candidate} AS candidate
  FROM {TABLE} AS o
  WHERE {partition}
)
SELECT candidate AS source_market, COUNT(*) AS sightings,
  COUNT(DISTINCT post_id) AS posts
FROM candidates
WHERE source_market IS NULL
  AND candidate IS NOT NULL
  AND market = candidate
  AND (source_region IS NULL OR source_region = candidate)
GROUP BY candidate
ORDER BY candidate"""


def _struct(alias, fields):
    return "STRUCT(" + ", ".join(f"{alias}.{field}" for field in fields) + ")"


def build_summary_sql():
    candidate = _sql_candidate("o")
    partition = _partition_predicate("o")
    key_struct = _struct("e", _ROW_FIELDS)
    actual_struct = _struct("e", _ROW_FIELDS + ("source_market",))
    expected_struct = "STRUCT(" + ", ".join(
        [*(f"e.{field}" for field in _ROW_FIELDS), "e.expected_source_market AS source_market"]
    ) + ")"
    return f"""WITH candidates AS (
  SELECT o.*, {candidate} AS candidate
  FROM {TABLE} AS o
  WHERE {partition}
), evaluated AS (
  SELECT *,
    source_market IS NULL AND candidate IS NOT NULL AND market = candidate
      AND (source_region IS NULL OR source_region = candidate) AS eligible,
    IF(source_market IS NULL AND candidate IS NOT NULL AND market = candidate
      AND (source_region IS NULL OR source_region = candidate), candidate, source_market)
      AS expected_source_market
  FROM candidates
), fingerprints AS (
  SELECT source_market, eligible,
    FARM_FINGERPRINT(TO_JSON_STRING({key_struct})) AS rowkey_fingerprint,
    FARM_FINGERPRINT(TO_JSON_STRING({actual_struct})) AS actual_source_fingerprint,
    FARM_FINGERPRINT(TO_JSON_STRING({expected_struct})) AS expected_source_fingerprint
  FROM evaluated AS e
)
SELECT COUNT(*) AS row_count,
  COUNTIF(source_market IS NULL) AS null_count,
  COUNTIF(source_market IS NOT NULL) AS nonnull_count,
  COUNTIF(eligible) AS eligible_count,
  COUNTIF(source_market IS NOT NULL AND source_market NOT IN ('ZA', 'NG', 'KE')) AS invalid_nonnull_count,
  COALESCE(BIT_XOR(rowkey_fingerprint), 0) AS rowkey_xor,
  COALESCE(SUM(CAST(rowkey_fingerprint AS BIGNUMERIC)), CAST(0 AS BIGNUMERIC)) AS rowkey_sum,
  COALESCE(BIT_XOR(actual_source_fingerprint), 0) AS actual_source_xor,
  COALESCE(SUM(CAST(actual_source_fingerprint AS BIGNUMERIC)), CAST(0 AS BIGNUMERIC)) AS actual_source_sum,
  COALESCE(BIT_XOR(expected_source_fingerprint), 0) AS expected_source_xor,
  COALESCE(SUM(CAST(expected_source_fingerprint AS BIGNUMERIC)), CAST(0 AS BIGNUMERIC)) AS expected_source_sum
FROM fingerprints"""


def readback_matches(before, after, rows_updated):
    if rows_updated is None:
        return False
    eligible = int(before["eligible_count"])
    return (
        int(rows_updated) == eligible
        and int(after["row_count"]) == int(before["row_count"])
        and int(after["null_count"]) == int(before["null_count"]) - eligible
        and int(after["nonnull_count"]) == int(before["nonnull_count"]) + eligible
        and int(after["eligible_count"]) == 0
        and int(after["invalid_nonnull_count"]) == int(before["invalid_nonnull_count"])
        and int(after["rowkey_xor"]) == int(before["rowkey_xor"])
        and int(after["rowkey_sum"]) == int(before["rowkey_sum"])
        and int(after["actual_source_xor"]) == int(before["expected_source_xor"])
        and int(after["actual_source_sum"]) == int(before["expected_source_sum"])
    )


def resolve_cutoff_timestamp(value, *, apply, now=None):
    now = now or datetime.now(ZoneInfo("Africa/Johannesburg"))
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_utc = now.astimezone(timezone.utc)
    if value is None:
        if apply:
            raise ValueError("apply requires the exact --cutoff-timestamp from a read-only preview")
        return now_utc
    cutoff = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("--cutoff-timestamp must include a timezone offset")
    cutoff = cutoff.astimezone(timezone.utc)
    if cutoff > now_utc:
        raise ValueError("--cutoff-timestamp must not be in the future")
    return cutoff


def _attempt_job_id(cutoff_timestamp):
    cutoff_key = cutoff_timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    digest = hashlib.sha256(cutoff_key.encode("utf-8")).hexdigest()[:32]
    return f"source_market_backfill_{digest}"


def assert_builder_identity(credentials):
    identity = getattr(credentials, "service_account_email", None)
    if identity != EXPECTED_BUILDER_EMAIL:
        raise RuntimeError(
            "refusing to create the BigQuery client: ADC identity must be "
            f"{EXPECTED_BUILDER_EMAIL}"
        )
    return identity


def _builder_client():
    from google.auth import default

    credentials, _ = default()
    assert_builder_identity(credentials)
    from google.cloud import bigquery

    return bigquery.Client(project="ogilvy-trends-v2", credentials=credentials)


def _job_config(cutoff_timestamp, *, dry_run=False):
    from google.cloud import bigquery

    cutoff_timestamp = cutoff_timestamp.astimezone(timezone.utc)
    return bigquery.QueryJobConfig(
        dry_run=dry_run,
        maximum_bytes_billed=MAX_BYTES_BILLED,
        query_parameters=[
            bigquery.ScalarQueryParameter("cutoff_timestamp", "TIMESTAMP", cutoff_timestamp),
            bigquery.ScalarQueryParameter("cutoff_date", "DATE", _partition_date_cap(cutoff_timestamp)),
        ],
    )


def _preflight(client, name, sql, cutoff_timestamp):
    job = client.query(sql, job_config=_job_config(cutoff_timestamp, dry_run=True))
    estimated_bytes = job.total_bytes_processed
    if estimated_bytes is None:
        raise RuntimeError(f"{name} dry-run estimate is unavailable; refusing to execute")
    estimated_bytes = int(estimated_bytes)
    if estimated_bytes > MAX_BYTES_BILLED:
        raise RuntimeError(f"{name} dry-run estimate exceeds the 5 GiB query cap")
    query_id = getattr(job, "job_id", None)
    return {
        "statement": name,
        "query_id": query_id,
        "query_id_persistence": "nonpersistent_dry_run",
        "estimated_bytes": estimated_bytes,
        "max_bytes_billed": MAX_BYTES_BILLED,
    }


def _execute(client, name, sql, cutoff_timestamp, estimated_bytes, *, job_id=None, dml=False):
    options = {"job_config": _job_config(cutoff_timestamp)}
    if job_id is not None:
        options["job_id"] = job_id
    try:
        job = client.query(sql, **options)
        rows = list(job.result())
    except Exception as exc:
        if job_id is not None:
            raise RuntimeError(
                f"apply attempt {job_id} has no definitive completion receipt; "
                "stop and inspect before any retry or rollback"
            ) from exc
        raise

    query_id = getattr(job, "job_id", None)
    if not query_id:
        if job_id is not None:
            raise RuntimeError(
                f"apply attempt {job_id} completed without an actual job ID; "
                "stop and inspect before any retry or rollback"
            )
        raise RuntimeError(f"{name} actual job ID is unavailable")
    if job_id is not None and query_id != job_id:
        raise RuntimeError(
            f"apply attempt {job_id} returned a different actual job ID {query_id}; "
            "stop and inspect before any retry or rollback"
        )
    bytes_processed = job.total_bytes_processed
    if bytes_processed is None:
        if job_id is not None:
            raise RuntimeError(
                f"apply attempt {job_id} completed without a bytes-processed receipt; "
                "stop and inspect before any retry or rollback"
            )
        raise RuntimeError(f"{name} actual bytes processed are unavailable")
    record = {
        "statement": name,
        "query_id": query_id,
        "estimated_bytes": int(estimated_bytes),
        "bytes_processed": int(bytes_processed),
        "affected_rows": None,
    }
    if not dml:
        return rows, record, None
    affected_rows = job.num_dml_affected_rows
    if affected_rows is None:
        raise RuntimeError(
            f"apply attempt {job_id} completed without an affected-row receipt; "
            "stop and inspect before any retry or rollback"
        )
    record["affected_rows"] = int(affected_rows)
    return rows, record, int(affected_rows)


def _summary_from_rows(rows):
    if len(rows) != 1:
        raise RuntimeError("summary query did not return exactly one row")
    return dict(rows[0].items())


def run(client, *, cutoff_timestamp, apply=False):
    if cutoff_timestamp.tzinfo is None or cutoff_timestamp.utcoffset() is None:
        raise ValueError("cutoff_timestamp must be timezone-aware")
    cutoff_timestamp = cutoff_timestamp.astimezone(timezone.utc)
    update_sql = build_update_sql()
    preview_sql = build_preview_sql()
    summary_sql = build_summary_sql()
    statements = (
        ("update", update_sql),
        ("preview", preview_sql),
        ("summary", summary_sql),
    )
    preflight = {
        name: _preflight(client, name, sql, cutoff_timestamp)
        for name, sql in statements
    }
    estimated_run_bytes = (
        preflight["update"]["estimated_bytes"]
        + preflight["preview"]["estimated_bytes"]
        + 2 * preflight["summary"]["estimated_bytes"]
    )
    if estimated_run_bytes > MAX_BYTES_BILLED:
        raise RuntimeError("combined preview, update, and readback estimate exceeds the 5 GiB run cap")

    preview_rows, preview_record, _ = _execute(
        client,
        "preview",
        preview_sql,
        cutoff_timestamp,
        preflight["preview"]["estimated_bytes"],
    )
    query_records = [preview_record]
    preview = [dict(row.items()) for row in preview_rows]
    preview_count = sum(int(row["sightings"]) for row in preview)
    result = {
        "cutoff_timestamp_exclusive": cutoff_timestamp.isoformat(),
        "cutoff_date_partition_cap": _partition_date_cap(cutoff_timestamp).isoformat(),
        "apply_requested": apply,
        "coverage_scope": {
            "routes": [route for route, _ in _PROTOCOL_RULES],
            "markets": list(MARKETS),
            "note": "Only these verified stored route protocols are classified; unsupported feeds remain null.",
        },
        "preview": preview,
        "preview_sightings": preview_count,
        "preflight_jobs": list(preflight.values()),
        "estimated_run_bytes": estimated_run_bytes,
        "max_bytes_billed_per_query": MAX_BYTES_BILLED,
        "query_jobs": query_records,
    }
    if not apply:
        return result

    before_rows, before_record, _ = _execute(
        client,
        "summary_before",
        summary_sql,
        cutoff_timestamp,
        preflight["summary"]["estimated_bytes"],
    )
    query_records.append(before_record)
    before = _summary_from_rows(before_rows)
    if int(before["eligible_count"]) != preview_count:
        raise RuntimeError("eligible row count changed after the read-only preview; no update was attempted")
    if preview_count == 0:
        result["rows_updated"] = 0
        result["readback"] = "no eligible rows"
        result["attempt_receipt"] = {"status": "no_update_required"}
        return result

    attempt_id = _attempt_job_id(cutoff_timestamp)
    result["attempt_receipt"] = {
        "attempt_id": attempt_id,
        "update_job_id": attempt_id,
        "status": "submitted",
    }
    try:
        _, update_record, rows_updated = _execute(
            client,
            "update",
            update_sql,
            cutoff_timestamp,
            preflight["update"]["estimated_bytes"],
            job_id=attempt_id,
            dml=True,
        )
        query_records.append(update_record)
        result["rows_updated"] = rows_updated
        after_rows, after_record, _ = _execute(
            client,
            "summary_after",
            summary_sql,
            cutoff_timestamp,
            preflight["summary"]["estimated_bytes"],
        )
        query_records.append(after_record)
        after = _summary_from_rows(after_rows)
    except Exception as exc:
        receipt = {
            "attempt_id": attempt_id,
            "preflight_jobs": list(preflight.values()),
            "query_jobs": query_records,
            "update_estimated_bytes": preflight["update"]["estimated_bytes"],
        }
        raise RuntimeError(
            f"apply attempt {attempt_id} needs operator inspection; do not retry or roll back automatically; "
            f"receipt={json.dumps(receipt, sort_keys=True)}"
        ) from exc
    if not readback_matches(before, after, rows_updated):
        receipt = {
            "attempt_id": attempt_id,
            "preflight_jobs": list(preflight.values()),
            "query_jobs": query_records,
            "rows_updated": rows_updated,
        }
        raise RuntimeError(
            f"apply attempt {attempt_id} failed frozen-set readback; "
            f"stop for inspection with no retry or rollback; receipt={json.dumps(receipt, sort_keys=True)}"
        )
    result["attempt_receipt"]["status"] = "readback_passed"
    result["readback"] = "passed"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--cutoff-timestamp",
        help="Exclusive observed_at ISO timestamp; apply requires the exact value from a read-only preview",
    )
    args = parser.parse_args(argv)
    try:
        cutoff_timestamp = resolve_cutoff_timestamp(args.cutoff_timestamp, apply=args.apply)
    except ValueError as exc:
        parser.error(str(exc))

    client = _builder_client()
    result = run(client, cutoff_timestamp=cutoff_timestamp, apply=args.apply)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
