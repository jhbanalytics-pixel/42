"""Build the R3 collection exposure receipts from records, one per family and day.

Every claim in a receipt comes from a record: capture completeness from the pipeline run
table named in the policy, source-copy references from the staging copy receipts, quota
applicability from the policy file, and the issuing build from the caller's manifest values.
Nothing is typed into a receipt by hand. Read-only against BigQuery.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.staging.issue_collection_exposure_receipts import (
    EXPOSURE_CONTRACT_VERSION,
    EXPOSURE_RECEIPT_FIELDS,
    R3_RUN_ID,
    R3_WINDOW_END,
    R3_WINDOW_START,
    exposure_receipt_digest,
    validate_exposure_receipt_set,
)
from src.analysis.open_intelligence.brain_contract import (
    canonical_bytes,
    canonical_digest,
)

POLICY_PATH = ROOT / "configs" / "open_intelligence" / "collection_exposure_policy_r3.json"
ISSUER_IDENTITY = "trends-engine-oi-exposure@ogilvy-trends-v2.iam.gserviceaccount.com"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ReceiptBuildRefusal(ValueError):
    """The records do not support a receipt."""


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENT.fullmatch(value) is None:
        raise ReceiptBuildRefusal(f"policy {field} is not a safe identifier")
    return value


def _window(policy: Mapping[str, object]) -> tuple[date, ...]:
    start = date.fromisoformat(str(policy["window_start"]))
    end = date.fromisoformat(str(policy["window_end"]))
    if (start, end) != (R3_WINDOW_START, R3_WINDOW_END) or policy.get("run_id") != R3_RUN_ID:
        raise ReceiptBuildRefusal("policy window or run does not match the R3 contract")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _record_columns(policy: Mapping[str, object]) -> tuple[str, ...]:
    """Extra pipeline record columns a metered family reads its usage from."""
    columns = set()
    for family_policy in policy["families"].values():
        quota = family_policy["quota"]
        if quota["applicability"] == "metered":
            record = quota.get("record")
            if not isinstance(record, Mapping):
                raise ReceiptBuildRefusal("a metered family declares no quota record to back it")
            columns.add(_identifier(record["column"], "quota record column"))
            if record.get("usage_column") is not None:
                columns.add(_identifier(record["usage_column"], "quota usage column"))
    return tuple(sorted(columns))


def pipeline_record_sql(policy: Mapping[str, object]) -> str:
    record = policy["collection_record"]
    project = _identifier(record["project"].replace("-", "_"), "project") and record["project"]
    dataset = _identifier(record["dataset"], "dataset")
    table = _identifier(record["table"], "table")
    extra = "".join(f", {column}" for column in _record_columns(policy))
    return (
        f"SELECT DATE(started_at) AS run_day, market, status{extra} "
        f"FROM `{project}.{dataset}.{table}` "
        f"WHERE DATE(started_at) BETWEEN DATE '{R3_WINDOW_START.isoformat()}' "
        f"AND DATE '{R3_WINDOW_END.isoformat()}' AND status != 'email_audit'"
    )


def source_copy_sql(policy: Mapping[str, object]) -> str:
    record = policy["source_copy"]
    dataset = _identifier(record["dataset"], "dataset")
    table = _identifier(record["table"], "table")
    return (
        "SELECT copy_run_id, source_table, source_set_digest "
        f"FROM `{record['project']}.{dataset}.{table}` "
        f"WHERE window_start = DATE '{R3_WINDOW_START.isoformat()}' "
        f"AND window_end = DATE '{R3_WINDOW_END.isoformat()}' ORDER BY source_table"
    )


def _complete_days(
    rows: Sequence[Mapping[str, object]],
    markets: Sequence[str],
    complete_status: str,
) -> set[date]:
    complete_by_day: dict[date, set[str]] = {}
    for row in rows:
        day = row["run_day"]
        if isinstance(day, datetime):
            day = day.date()
        elif isinstance(day, str):
            day = date.fromisoformat(day)
        if row.get("status") == complete_status:
            complete_by_day.setdefault(day, set()).add(str(row.get("market")))
    return {day for day, seen in complete_by_day.items() if set(markets) <= seen}


def _metered_usage(
    rows: Sequence[Mapping[str, object]],
    markets: Sequence[str],
    complete_status: str,
    quota: Mapping[str, object],
) -> dict[date, tuple[int, bool]]:
    """Per day: units the record supports and whether every market collected.

    A market-day is collected when its pipeline row is success and the record
    column is above zero. Usage is the per-market-day unit cost the connector
    documents, times the collected market count, so it is derived from the
    record and the code, never typed.
    """
    record = quota["record"]
    column = _identifier(record["column"], "quota record column")
    # A record may carry the measured usage itself (SocialCrawl credits per
    # market run); then usage is the sum the record reports, never a typed
    # per-day figure. Otherwise the connector's documented unit cost applies.
    usage_column = record.get("usage_column")
    if usage_column is not None:
        usage_column = _identifier(usage_column, "quota usage column")
        units = None
    else:
        units = record["units_per_market_day"]
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise ReceiptBuildRefusal("units_per_market_day must be a positive integer")
    collected: dict[date, set[str]] = {}
    measured: dict[date, int] = {}
    for row in rows:
        day = row["run_day"]
        if isinstance(day, datetime):
            day = day.date()
        elif isinstance(day, str):
            day = date.fromisoformat(day)
        count = row.get(column)
        if row.get("status") == complete_status and isinstance(count, int) and count > 0:
            collected.setdefault(day, set()).add(str(row.get("market")))
            if usage_column is not None:
                used = row.get(usage_column)
                if isinstance(used, bool) or not isinstance(used, int) or used < 0:
                    raise ReceiptBuildRefusal(f"{usage_column} must be a nonnegative integer")
                measured[day] = measured.get(day, 0) + used
    return {
        day: (
            measured.get(day, 0) if usage_column is not None else units * len(seen),
            set(markets) <= seen,
        )
        for day, seen in collected.items()
    }


def _copy_refs(rows: Sequence[Mapping[str, object]]) -> tuple[dict[str, str], ...]:
    refs = tuple(
        {
            "copy_run_id": str(row["copy_run_id"]),
            "source_table": str(row["source_table"]),
            "source_set_digest": str(row["source_set_digest"]),
        }
        for row in rows
    )
    tables = {ref["source_table"] for ref in refs}
    if len(refs) != 4 or tables != {
        "event_ledger",
        "seed_graph",
        "seed_candidates",
        "enriched_content",
    }:
        raise ReceiptBuildRefusal("source-copy receipts for the window are incomplete")
    if len({ref["copy_run_id"] for ref in refs}) != 1:
        raise ReceiptBuildRefusal("source-copy receipts span more than one copy run")
    return tuple(sorted(refs, key=lambda ref: ref["source_table"]))


def build_receipts(
    *,
    policy: Mapping[str, object],
    query_runner: Callable[[str], Sequence[Mapping[str, object]]],
    source_sha: str,
    image_digest: str,
    issuer_identity: str,
    issued_at: datetime,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(source_sha, str) or _SHA.fullmatch(source_sha) is None:
        raise ReceiptBuildRefusal("source_sha must be a 40 character git sha")
    if not isinstance(image_digest, str) or _IMAGE.fullmatch(image_digest) is None:
        raise ReceiptBuildRefusal("image_digest must be a sha256 image digest")
    if issued_at.tzinfo is None:
        raise ReceiptBuildRefusal("issued_at must be timezone aware")
    days = _window(policy)
    record = policy["collection_record"]
    pipeline_rows = query_runner(pipeline_record_sql(policy))
    complete_days = _complete_days(
        pipeline_rows,
        record["markets"],
        record["complete_status"],
    )
    copy_refs = _copy_refs(query_runner(source_copy_sql(policy)))
    config_digest = canonical_digest(
        {"policy_version": policy["policy_version"], "collection_record": record}
    )
    receipts = []
    for family, family_policy in sorted(policy["families"].items()):
        quota = family_policy["quota"]
        applicability = quota["applicability"]
        if applicability == "metered":
            if not isinstance(quota.get("record"), Mapping):
                raise ReceiptBuildRefusal(
                    f"family {family} declares a metered quota with no quota record to back it"
                )
            usage = _metered_usage(
                pipeline_rows, record["markets"], record["complete_status"], quota
            )
            limit = quota["limit"]
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ReceiptBuildRefusal(f"family {family} quota limit must be a positive integer")
        elif applicability != "unmetered":
            raise ReceiptBuildRefusal(f"family {family} quota applicability is unsupported")
        collection_policy_digest = canonical_digest(
            {
                "policy_version": policy["policy_version"],
                "run_id": policy["run_id"],
                "window_start": policy["window_start"],
                "window_end": policy["window_end"],
                "family": family,
                "collection_sources": family_policy["collection_sources"],
                "evidence_tables": family_policy["evidence_tables"],
            }
        )
        quota_authority_id = canonical_digest({"family": family, "quota": quota})
        for day in days:
            if applicability == "metered":
                used, family_complete = usage.get(day, (0, False))
                quota_fields = {
                    "quota_applicability": "metered",
                    "quota_unit": quota["unit"],
                    "quota_limit": limit,
                    "quota_used": used,
                    "quota_exhausted": used >= limit,
                    "capture_complete": day in complete_days and family_complete and used < limit,
                }
            else:
                quota_fields = {
                    "quota_applicability": "unmetered",
                    "quota_unit": None,
                    "quota_limit": None,
                    "quota_used": None,
                    "quota_exhausted": False,
                    "capture_complete": day in complete_days,
                }
            receipt = {
                "exposure_contract_version": EXPOSURE_CONTRACT_VERSION,
                "source_family": family,
                "exposure_date": day,
                "collection_policy_digest": collection_policy_digest,
                "source_sha": source_sha,
                "image_digest": image_digest,
                "config_digest": config_digest,
                "quota_authority_id": quota_authority_id,
                **quota_fields,
                "source_copy_receipt_refs": copy_refs,
                "issued_at": issued_at.astimezone(UTC),
                "issuer_identity": issuer_identity,
            }
            receipt["receipt_digest"] = exposure_receipt_digest(receipt)
            receipts.append({field: receipt[field] for field in EXPOSURE_RECEIPT_FIELDS})
    return validate_exposure_receipt_set(receipts)


def render_receipts(receipts: Sequence[Mapping[str, object]]) -> bytes:
    return canonical_bytes(tuple(receipts))


def _bigquery_runner():
    from google.cloud import bigquery

    client = bigquery.Client(project="ogilvy-trends-v2", location="US")

    def run(sql: str):
        return [dict(row) for row in client.query(sql).result()]

    return run


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--issued-at", required=True, help="ISO 8601 with offset")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    receipts = build_receipts(
        policy=policy,
        query_runner=_bigquery_runner(),
        source_sha=args.source_sha,
        image_digest=args.image_digest,
        issuer_identity=ISSUER_IDENTITY,
        issued_at=datetime.fromisoformat(args.issued_at.replace("Z", "+00:00")),
    )
    args.output.write_bytes(render_receipts(receipts))
    complete = sum(1 for item in receipts if item["capture_complete"])
    print(
        json.dumps(
            {
                "receipts": len(receipts),
                "families": sorted({item["source_family"] for item in receipts}),
                "capture_complete": complete,
                "output": str(args.output),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
