"""Read back one ingest execution: its collection receipt and the raw rows it persisted.

A read only BigQuery tool for the owner, run under the owner's own login. It needs
bigquery.jobs.create on the project and table read on intelligence_42_sources_staging,
nothing else, and it refuses to run as a service account.

Given an ingest execution id and the closed observation day the run collected for, it
reads the execution's row in collection_receipts. There must be exactly one; its stored
digest must equal TO_HEX(SHA256(receipt_json)) computed in the warehouse; the text must
be the canonical receipt the ingest ledger admits; and the receipt must carry the
verified manifest authority, the ingest job's pinned policy and profile digests, the
given cutoff and all three markets collected, stored under the start day after the
cutoff. The same row is then run through the daily collect stage's own admission, so
the readback and the stage cannot disagree about what counts as this run's record. The
stage also binds the daily runtime's source commit and image; pass --source-sha and
--image-uri to bind them here too, otherwise the receipt says the admission covered the
digests only.

With the receipt's run id it reads raw_content for that pipeline_run_id, grouped by
market and source with the row count and MAX(collected_at), inside the two partition
days that start on the run's start day. The rows must sum to the receipt's raw count and
cover every market the receipt says was collected.

Every statement is dry run first and refused when the dry run exceeds the byte cap; the
real run carries the same cap as maximum_bytes_billed. A service error on either
statement is recorded as a refusal and the receipt is still written. With --dry-run nothing executes:
both statements are planned and their byte estimates recorded. The receipt is written
once and an existing path is refused before any query.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.staging.collect_42_sources import COLLECTED, VERIFIED_AUTHORITY
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_native_clients import (
    _admitted_ingest_receipt,
    build_provenance,
)
from src.analysis.open_intelligence.ingest_receipts import (
    INGEST_EXECUTION,
    PROJECT,
    RECEIPT_TABLE,
    SOURCE_DATASET,
    read_receipt_row,
)

RECEIPT_VERSION = "ingest_run_readback_v1"
RECEIPT_TABLE_ID = f"{PROJECT}.{SOURCE_DATASET}.{RECEIPT_TABLE}"
RAW_TABLE_ID = f"{PROJECT}.{SOURCE_DATASET}.raw_content"
# The digests infra/runtime/ingest-staging.json stamps into the job; a test holds them
# to that file and to the entry point's own profile.
EXPECTED_POLICY_SHA256 = "ff9c86565ec0aab5423fada601767eeadec6d290e417ad339d2b10646e5e26e7"
EXPECTED_PROFILE_SHA256 = "c300e0f66402b56f3e1a1532a66c6cfcdb58cb94d31ce6cf7f1223674ad5e03d"
DEFAULT_MAXIMUM_BYTES_BILLED = 256 * 1024**2
RAW_WINDOW_DAYS = 2

RECEIPT_SQL = (
    "SELECT execution_id, trend_date, receipt_json, receipt_sha256, recorded_at,"
    " TO_HEX(SHA256(receipt_json)) AS computed_sha256"
    f" FROM `{RECEIPT_TABLE_ID}` WHERE execution_id = @execution_id"
    " ORDER BY recorded_at"
)
RAW_SQL = (
    "SELECT market, source, COUNT(*) AS row_count, MAX(collected_at) AS last_collected_at"
    f" FROM `{RAW_TABLE_ID}`"
    " WHERE pipeline_run_id = @pipeline_run_id"
    " AND collected_at >= @window_start AND collected_at < @window_end"
    " GROUP BY market, source ORDER BY market, source"
)


class ReadRefused(Exception):
    pass


class _Refused(Exception):
    pass


def _instant(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _parameters(**values):
    from google.cloud import bigquery

    return [
        bigquery.ScalarQueryParameter(
            name, "TIMESTAMP" if isinstance(value, datetime) else "STRING", value
        )
        for name, value in values.items()
    ]


def _build(build):
    """The build as the daily stage accepts it: a git commit and a digest pinned image."""
    try:
        return build_provenance(build)
    except ValueError as error:
        raise ReadRefused(
            "build must name a git source sha and a digest pinned image uri"
        ) from error


class Reader:
    """Runs each statement as a dry run, refuses it above the cap, then runs it capped."""

    def __init__(self, client, *, maximum_bytes_billed, dry_run):
        self.client = client
        self.maximum_bytes_billed = maximum_bytes_billed
        self.dry_run = dry_run
        self.queries = []

    def _config(self, parameters, *, dry_run):
        from google.cloud import bigquery

        return bigquery.QueryJobConfig(
            dry_run=dry_run,
            use_query_cache=False,
            maximum_bytes_billed=self.maximum_bytes_billed,
            query_parameters=parameters,
        )

    def run(self, name, sql, parameters):
        from google.api_core.exceptions import GoogleAPIError
        from google.auth.exceptions import GoogleAuthError

        entry = {
            "name": name,
            "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "dry_run_bytes": None,
        }
        self.queries.append(entry)
        try:
            return self._run(entry, name, sql, parameters)
        except (GoogleAPIError, GoogleAuthError, TimeoutError) as error:
            entry["state"] = "failed"
            raise _Refused(f"query_failed:{name}:{type(error).__name__}") from error

    def _run(self, entry, name, sql, parameters):
        dry = self.client.query(sql, job_config=self._config(parameters, dry_run=True))
        planned = dry.total_bytes_processed
        entry["dry_run_bytes"] = planned
        if type(planned) is not int or planned < 0:
            entry.update(state="refused_no_estimate", dry_run_bytes=None)
            raise _Refused(f"no_byte_estimate:{name}")
        if planned > self.maximum_bytes_billed:
            entry["state"] = "refused_over_cap"
            raise _Refused(f"over_byte_cap:{name}")
        if self.dry_run:
            entry["state"] = "planned"
            return None
        job = self.client.query(sql, job_config=self._config(parameters, dry_run=False))
        rows = [dict(row.items()) if hasattr(row, "items") else dict(row) for row in job.result()]
        entry.update(
            state="succeeded",
            job_id=job.job_id,
            bytes_billed=job.total_bytes_billed,
            row_count=len(rows),
        )
        return rows


def check_receipt_rows(rows, *, execution_id, cutoff, build=None):
    """The receipt row's readback and the reasons it is not this run's verified record."""
    trend_date = (cutoff + timedelta(days=1)).isoformat()
    summary = {"row_count": len(rows)}
    if len(rows) == 0:
        return summary, ["receipt_row_missing"], None
    if len(rows) > 1:
        return summary, ["receipt_row_duplicated"], None
    row = rows[0]
    summary.update(
        execution_id=row.get("execution_id"),
        trend_date=row.get("trend_date"),
        recorded_at=_instant(row.get("recorded_at")),
        receipt_sha256=row.get("receipt_sha256"),
        computed_sha256=row.get("computed_sha256"),
    )
    reasons = []
    try:
        receipt = read_receipt_row(row)
    except ValueError:
        receipt = None
        reasons.append("receipt_row_invalid")
    if row.get("receipt_sha256") != row.get("computed_sha256"):
        reasons.append("receipt_sha256_mismatch")
    if receipt is None:
        summary["admitted_by_daily_stage"] = False
        return summary, reasons, None
    summary["receipt"] = receipt
    if receipt.get("execution_id") != execution_id:
        reasons.append("execution_id_mismatch")
    if receipt.get("authority_kind") != VERIFIED_AUTHORITY:
        reasons.append("authority_kind_not_verified")
    if receipt.get("policy_sha256") != EXPECTED_POLICY_SHA256:
        reasons.append("policy_sha256_mismatch")
    if receipt.get("profile_sha256") != EXPECTED_PROFILE_SHA256:
        reasons.append("profile_sha256_mismatch")
    if receipt.get("cutoff") != cutoff.isoformat():
        reasons.append("cutoff_mismatch")
    if row.get("trend_date") != trend_date:
        reasons.append("trend_date_mismatch")
    for field, value in (build or {}).items():
        if receipt.get(field) != value:
            reasons.append(f"{field}_mismatch")
    if receipt.get("complete") is not True:
        reasons.append("collection_incomplete")
    expected = {
        "policy_sha256": EXPECTED_POLICY_SHA256,
        "profile_sha256": EXPECTED_PROFILE_SHA256,
        "cutoff": cutoff.isoformat(),
        **(build or {}),
    }
    summary["admission_scope"] = "digests_and_build" if build else "digests_only"
    try:
        _admitted_ingest_receipt(row, execution_id=execution_id, expected=expected)
        summary["admitted_by_daily_stage"] = True
    except Exception:
        summary["admitted_by_daily_stage"] = False
        reasons.append("receipt_not_admitted")
    return summary, reasons, receipt


def check_raw_groups(rows, receipt, window):
    """raw_content grouped by market and source, held to the receipt's count and markets."""
    groups = [
        {
            "market": row["market"],
            "source": row["source"],
            "row_count": row["row_count"],
            "last_collected_at": _instant(row["last_collected_at"]),
        }
        for row in rows
    ]
    by_market = {}
    for group in groups:
        last = group["last_collected_at"]
        if last is not None and (
            group["market"] not in by_market or last > by_market[group["market"]]
        ):
            by_market[group["market"]] = last
    total = sum(group["row_count"] for group in groups)
    reasons = []
    if total == 0:
        reasons.append("raw_rows_absent")
    if total != receipt.get("raw_rows_persisted"):
        reasons.append("raw_count_mismatch")
    states = receipt.get("market_states") or {}
    for market in sorted(states):
        if states[market] == COLLECTED and market not in by_market:
            reasons.append(f"raw_market_missing:{market}")
    summary = {
        "pipeline_run_id": receipt.get("run_id"),
        "window": {
            "start": window["window_start"].isoformat(),
            "end": window["window_end"].isoformat(),
        },
        "groups": groups,
        "row_count_total": total,
        "receipt_raw_rows_persisted": receipt.get("raw_rows_persisted"),
        "last_collected_by_market": dict(sorted(by_market.items())),
    }
    return summary, reasons


def read_ingest_run(
    client, *, execution_id, cutoff, maximum_bytes_billed, dry_run=False, build=None
):
    if not isinstance(execution_id, str) or INGEST_EXECUTION.fullmatch(execution_id) is None:
        raise ReadRefused("execution id must name an intelligence-42-ingest-staging execution")
    if type(cutoff) is not date:
        raise ReadRefused("cutoff must be a date")
    if type(maximum_bytes_billed) is not int or maximum_bytes_billed <= 0:
        raise ReadRefused("maximum_bytes_billed must be a positive integer")
    if build is not None:
        build = _build(build)
    reader = Reader(client, maximum_bytes_billed=maximum_bytes_billed, dry_run=dry_run)
    start = datetime.combine(cutoff + timedelta(days=1), time.min, UTC)
    window = {"window_start": start, "window_end": start + timedelta(days=RAW_WINDOW_DAYS)}
    output = {
        "contract_version": RECEIPT_VERSION,
        "mode": "dry_run" if dry_run else "read",
        "execution_id": execution_id,
        "cutoff": cutoff.isoformat(),
        "expected": {
            "trend_date": start.date().isoformat(),
            "authority_kind": VERIFIED_AUTHORITY,
            "policy_sha256": EXPECTED_POLICY_SHA256,
            "profile_sha256": EXPECTED_PROFILE_SHA256,
            **(build or {}),
        },
        "tables": {"receipts": RECEIPT_TABLE_ID, "raw_content": RAW_TABLE_ID},
        "maximum_bytes_billed": maximum_bytes_billed,
        "receipt_row": None,
        "raw_content": None,
    }
    reasons = []
    try:
        rows = reader.run("receipts", RECEIPT_SQL, _parameters(execution_id=execution_id))
        if dry_run:
            reader.run("raw", RAW_SQL, _parameters(pipeline_run_id="", **window))
        else:
            summary, found, receipt = check_receipt_rows(
                rows, execution_id=execution_id, cutoff=cutoff, build=build
            )
            output["receipt_row"] = summary
            reasons += found
            if receipt is not None:
                raw = reader.run(
                    "raw", RAW_SQL, _parameters(pipeline_run_id=receipt["run_id"], **window)
                )
                output["raw_content"], found = check_raw_groups(raw, receipt, window)
                reasons += found
    except _Refused as refusal:
        reasons.append(str(refusal))
    state = "refused" if reasons else "planned" if dry_run else "verified"
    output.update(
        state=state,
        reasons=reasons,
        queries=reader.queries,
        dry_run_bytes_total=sum(q["dry_run_bytes"] or 0 for q in reader.queries),
        bytes_billed_total=sum(q.get("bytes_billed") or 0 for q in reader.queries),
    )
    return output


def write_receipt(receipt, path):
    with Path(path).open("xb") as handle:
        handle.write(canonical_bytes(receipt))


def _owner_client():
    from google.cloud import bigquery

    return bigquery.Client(project=PROJECT)


def main(argv=None, *, client_factory=_owner_client):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--cutoff", required=True, help="closed observation day, YYYY-MM-DD")
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--maximum-bytes-billed", type=int, default=DEFAULT_MAXIMUM_BYTES_BILLED)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-sha", help="the daily runtime's source commit, with --image-uri")
    parser.add_argument("--image-uri", help="the daily runtime's engine image, with --source-sha")
    args = parser.parse_args(argv)
    if Path(args.receipt).exists():
        raise SystemExit(f"receipt path already exists: {args.receipt}")
    if not Path(args.receipt).parent.is_dir():
        raise SystemExit(f"receipt folder does not exist: {Path(args.receipt).parent}")
    build = None
    if args.source_sha is not None or args.image_uri is not None:
        try:
            build = _build({"source_sha": args.source_sha, "image_uri": args.image_uri})
        except ReadRefused as refusal:
            raise SystemExit(str(refusal)) from refusal
    try:
        cutoff = date.fromisoformat(args.cutoff)
    except ValueError as error:
        raise SystemExit(f"cutoff is not a date: {args.cutoff}") from error
    client = client_factory()
    if getattr(getattr(client, "_credentials", None), "service_account_email", None):
        raise SystemExit("this readback runs as the owner, not as a service account")
    try:
        receipt = read_ingest_run(
            client,
            execution_id=args.execution_id,
            cutoff=cutoff,
            maximum_bytes_billed=args.maximum_bytes_billed,
            dry_run=args.dry_run,
            build=build,
        )
    except ReadRefused as refusal:
        raise SystemExit(str(refusal)) from refusal
    write_receipt(receipt, args.receipt)
    print(
        json.dumps(
            {
                "state": receipt["state"],
                "reasons": receipt["reasons"],
                "dry_run_bytes_total": receipt["dry_run_bytes_total"],
                "bytes_billed_total": receipt["bytes_billed_total"],
            }
        )
    )
    return 0 if receipt["state"] in ("verified", "planned") else 1


if __name__ == "__main__":
    raise SystemExit(main())
