"""GDELT GKG connector via the BigQuery public dataset.

Queries gdelt-bq.gdeltv2.gkg_partitioned for news and entity records per
market. Each row becomes one raw_content row with content_type="gdelt_gkg".
Engagement fields are zero (GKG has no likes, views, comments, shares).

Replaces the DOC 2.0 HTTP API connector. The DOC 2.0 endpoint rate-limits
at 1 request per 5 seconds, returns only 50-100 rows per call, and the
22 Apr 2026 scheduled run surfaced 4-5 ConnectionError / ReadTimeout
failures per market. The GKG public dataset is free, rate-limit-free, and
carries richer signal (themes, entities, tone).

GDELT uses FIPS 10-4 country codes, not ISO 3166. ZA -> SF, NG -> NI,
KE -> KE. Preserved here as a class attribute so the mapping stays in one
place.
"""

import hashlib
import json
import re
import weakref
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd
from google.api_core.exceptions import BadRequest, NotFound
from google.cloud import bigquery

from src.ingestion.connectors.base import BaseConnector
from src.utils import bigquery as bq_utils
from src.utils.config_loader import load_sources

_SQL_PATH = Path(__file__).resolve().parents[3] / "infra" / "bigquery_queries" / "gdelt_gkg.sql"
_EVENTS_SQL_PATH = (
    Path(__file__).resolve().parents[3] / "infra" / "bigquery_queries" / "gdelt_events.sql"
)

# GCAM dimension codes verified against the live `gdelt-bq.gdeltv2.gkg_partitioned`
# dataset on 28 May 2026 (SF partition, last 24h). Column name is `GCAM`, not
# `V2GCAM` despite some legacy references. Format is comma-separated
# "<code>:<value>" tokens. Integer-valued tokens are prefixed `c` (counts),
# float-valued tokens prefixed `v` (normalised aggregates).
#
# The five dimensions below are the float-valued aggregate scores most useful
# for stakeholder narrative. Codes from the GDELT GCAM Master Codebook:
#   v10.1 - Hedonometer happiness (Dodds et al.), 1-9 scale, neutral ~5.0
#   v10.2 - Hedonometer happiness, secondary normalisation
#   v19.1 - LIWC-style positive emotion proxy aggregate
#   v19.9 - LIWC-style negative emotion proxy aggregate
#   v20.1 - SentiWordNet positive polarity aggregate
# The task spec referenced anger/anxiety/joy/sadness/trust at v10.1-v10.5;
# v10.3/4/5 do NOT exist in the live dataset (probe returned only v10.1 and
# v10.2 under the v10 family). The WordNet-Affect emotion families live under
# the c8.X prefix as integer counts; mapping those to scalar narrative dims
# requires per-row normalisation by the c1.2 word count which is out of scope
# for this connector. The five aggregates below are the highest-signal
# float-valued GCAM dims available at row level.
_GCAM_TARGET_DIMS: ClassVar[tuple[str, ...]] = (
    "v10.1",
    "v10.2",
    "v19.1",
    "v19.9",
    "v20.1",
)

GDELT_EVENTS_SCHEMA = (
    ("GLOBALEVENTID", "INT64", "REQUIRED"),
    ("event_date", "DATE", "REQUIRED"),
    ("actor1_country_code", "STRING", "NULLABLE"),
    ("actor2_country_code", "STRING", "NULLABLE"),
    ("event_code", "STRING", "NULLABLE"),
    ("event_root_code", "STRING", "NULLABLE"),
    ("goldstein_scale", "FLOAT64", "NULLABLE"),
    ("mention_count", "INT64", "REQUIRED"),
    ("source_count", "INT64", "REQUIRED"),
    ("article_count", "INT64", "REQUIRED"),
    ("average_tone", "FLOAT64", "NULLABLE"),
    ("action_geo_name", "STRING", "NULLABLE"),
    ("action_geo_latitude", "FLOAT64", "NULLABLE"),
    ("action_geo_longitude", "FLOAT64", "NULLABLE"),
    ("source_url", "STRING", "NULLABLE"),
    ("date_added", "TIMESTAMP", "REQUIRED"),
)
GDELT_EVENT_MARKET_SCHEMA = (
    ("GLOBALEVENTID", "INT64", "REQUIRED"),
    ("market", "STRING", "REQUIRED"),
    ("evidence_role", "STRING", "REQUIRED"),
    ("receipt_id", "STRING", "REQUIRED"),
)
GDELT_GCAM_SCHEMA = (
    ("document_url", "STRING", "REQUIRED"),
    ("published_at", "TIMESTAMP", "REQUIRED"),
    ("v10_1", "FLOAT64", "NULLABLE"),
    ("v10_2", "FLOAT64", "NULLABLE"),
    ("v19_1", "FLOAT64", "NULLABLE"),
    ("v19_9", "FLOAT64", "NULLABLE"),
    ("v20_1", "FLOAT64", "NULLABLE"),
)
_WAVE1_PROJECT = "ogilvy-trends-v2"
_WAVE1_LOCATION = "US"
_WAVE1_DRY_RUN_PRINCIPAL = "jhb.analytics@gmail.com"
_WAVE1_MANIFEST_CEILING = 50_000_000_000
_DRY_RUN_RECEIPT_FIELDS = (
    "dry_run_receipt_id",
    "name",
    "sql_digest",
    "start_date",
    "end_date",
    "total_bytes_processed",
    "created_at",
    "ended_at",
    "principal_email",
    "etag",
    "project",
    "location",
)


@dataclass(frozen=True, slots=True)
class GDELTDryRunPlan:
    name: str
    sql: str
    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True)
class GDELTDryRunReceipt:
    dry_run_receipt_id: str
    name: str
    sql_digest: str
    start_date: date
    end_date: date
    total_bytes_processed: int
    created_at: datetime
    ended_at: datetime
    principal_email: str
    etag: str
    project: str
    location: str

    def __post_init__(self) -> None:
        _validate_wave1_gdelt_dry_run_receipt(self)


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class GDELTWave1ManifestEntry:
    manifest_sha256: str
    name: str
    dry_run_receipt_id: str
    sql_digest: str
    total_bytes_processed: int
    maximum_bytes_billed: int
    binding_digest: str

    def __init__(self, *_args, **_kwargs):
        from src.analysis.open_intelligence.funded_lane import Wave1ExecutionBlocked

        raise Wave1ExecutionBlocked("GDELT Wave 1 manifest entries must be approval-bound")


def _wave1_manifest_entry_state():
    registry: dict[int, tuple[weakref.ReferenceType, str]] = {}

    def payload(value: GDELTWave1ManifestEntry) -> dict[str, object]:
        return {
            "manifest_sha256": value.manifest_sha256,
            "name": value.name,
            "dry_run_receipt_id": value.dry_run_receipt_id,
            "sql_digest": value.sql_digest,
            "total_bytes_processed": value.total_bytes_processed,
            "maximum_bytes_billed": value.maximum_bytes_billed,
        }

    def digest(value: GDELTWave1ManifestEntry) -> str:
        encoded = json.dumps(
            payload(value), allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def issue(
        manifest_sha256: str,
        name: str,
        dry_run_receipt_id: str,
        sql_digest: str,
        total_bytes_processed: int,
        maximum_bytes_billed: int,
    ) -> GDELTWave1ManifestEntry:
        entry = object.__new__(GDELTWave1ManifestEntry)
        for field, value in {
            "manifest_sha256": manifest_sha256,
            "name": name,
            "dry_run_receipt_id": dry_run_receipt_id,
            "sql_digest": sql_digest,
            "total_bytes_processed": total_bytes_processed,
            "maximum_bytes_billed": maximum_bytes_billed,
        }.items():
            object.__setattr__(entry, field, value)
        binding_digest = digest(entry)
        object.__setattr__(entry, "binding_digest", binding_digest)
        identity = id(entry)

        def cleanup(_reference):
            registry.pop(identity, None)

        registry[identity] = (weakref.ref(entry, cleanup), binding_digest)
        return entry

    def require(value: object, manifest_sha256: str) -> GDELTWave1ManifestEntry:
        from src.analysis.open_intelligence.funded_lane import Wave1ExecutionBlocked

        if type(value) is not GDELTWave1ManifestEntry:
            raise Wave1ExecutionBlocked("GDELT Wave 1 manifest entry is unavailable")
        try:
            binding_digest = digest(value)
            registered = registry.get(id(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise Wave1ExecutionBlocked("GDELT Wave 1 manifest entry is unavailable") from error
        if (
            registered is None
            or registered[0]() is not value
            or registered[1] != binding_digest
            or value.binding_digest != binding_digest
            or value.manifest_sha256 != manifest_sha256
        ):
            raise Wave1ExecutionBlocked("GDELT Wave 1 manifest entry is unavailable")
        return value

    return issue, require


_issue_wave1_gdelt_manifest_entry, _require_wave1_gdelt_manifest_entry = (
    _wave1_manifest_entry_state()
)
del _wave1_manifest_entry_state


def _receipt_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dry_run_receipt_content(receipt: GDELTDryRunReceipt) -> dict[str, object]:
    return {
        "name": receipt.name,
        "sql_digest": receipt.sql_digest,
        "start_date": receipt.start_date.isoformat(),
        "end_date": receipt.end_date.isoformat(),
        "total_bytes_processed": receipt.total_bytes_processed,
        "created_at": _receipt_timestamp(receipt.created_at),
        "ended_at": _receipt_timestamp(receipt.ended_at),
        "principal_email": receipt.principal_email,
        "etag": receipt.etag,
        "project": receipt.project,
        "location": receipt.location,
    }


def _dry_run_receipt_id(receipt: GDELTDryRunReceipt) -> str:
    canonical = json.dumps(
        _dry_run_receipt_content(receipt),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "gdry_" + hashlib.sha256(canonical).hexdigest()


def _validate_wave1_gdelt_dry_run_receipt(
    receipt: object,
) -> GDELTDryRunReceipt:
    if type(receipt) is not GDELTDryRunReceipt:
        raise ValueError("GDELT dry-run receipt is invalid")
    if (
        receipt.name not in {"events", "gcam"}
        or not isinstance(receipt.sql_digest, str)
        or len(receipt.sql_digest) != 64
        or any(character not in "0123456789abcdef" for character in receipt.sql_digest)
        or type(receipt.start_date) is not date
        or type(receipt.end_date) is not date
        or receipt.start_date > receipt.end_date
        or isinstance(receipt.total_bytes_processed, bool)
        or not isinstance(receipt.total_bytes_processed, int)
        or receipt.total_bytes_processed <= 0
        or type(receipt.created_at) is not datetime
        or receipt.created_at.tzinfo is None
        or type(receipt.ended_at) is not datetime
        or receipt.ended_at.tzinfo is None
        or receipt.ended_at < receipt.created_at
        or any(
            not isinstance(value, str) or not value or value.strip() != value
            for value in (receipt.principal_email, receipt.etag)
        )
        or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", receipt.principal_email) is None
        or receipt.principal_email != _WAVE1_DRY_RUN_PRINCIPAL
        or receipt.project != _WAVE1_PROJECT
        or receipt.location != _WAVE1_LOCATION
        or not isinstance(receipt.dry_run_receipt_id, str)
        or receipt.dry_run_receipt_id != _dry_run_receipt_id(receipt)
    ):
        raise ValueError("GDELT dry-run receipt is invalid")
    return receipt


def _parse_receipt_date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("GDELT dry-run receipt is invalid")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("GDELT dry-run receipt is invalid")
    return parsed


def _parse_receipt_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("GDELT dry-run receipt is invalid")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if _receipt_timestamp(parsed) != value:
        raise ValueError("GDELT dry-run receipt is invalid")
    return parsed


def _dry_run_receipt_from_mapping(value: object) -> GDELTDryRunReceipt:
    if not isinstance(value, dict) or set(value) != set(_DRY_RUN_RECEIPT_FIELDS):
        raise ValueError("GDELT dry-run receipt is invalid")
    try:
        return GDELTDryRunReceipt(
            dry_run_receipt_id=value["dry_run_receipt_id"],
            name=value["name"],
            sql_digest=value["sql_digest"],
            start_date=_parse_receipt_date(value["start_date"]),
            end_date=_parse_receipt_date(value["end_date"]),
            total_bytes_processed=value["total_bytes_processed"],
            created_at=_parse_receipt_timestamp(value["created_at"]),
            ended_at=_parse_receipt_timestamp(value["ended_at"]),
            principal_email=value["principal_email"],
            etag=value["etag"],
            project=value["project"],
            location=value["location"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("GDELT dry-run receipt is invalid") from error


def build_wave1_gdelt_dry_run_plans(start_date: date, end_date: date):
    if not isinstance(start_date, date) or not isinstance(end_date, date) or start_date > end_date:
        raise ValueError("GDELT dry-run window is invalid")
    events = """SELECT
  CAST(GLOBALEVENTID AS INT64) AS GLOBALEVENTID,
  PARSE_DATE('%Y%m%d', CAST(SQLDATE AS STRING)) AS event_date,
  CAST(Actor1CountryCode AS STRING) AS actor1_country_code,
  CAST(Actor2CountryCode AS STRING) AS actor2_country_code,
  CAST(ActionGeo_CountryCode AS STRING) AS action_geo_country_code,
  CAST(EventCode AS STRING) AS event_code,
  CAST(EventRootCode AS STRING) AS event_root_code,
  CAST(GoldsteinScale AS FLOAT64) AS goldstein_scale,
  CAST(NumMentions AS INT64) AS mention_count,
  CAST(NumSources AS INT64) AS source_count,
  CAST(NumArticles AS INT64) AS article_count,
  CAST(AvgTone AS FLOAT64) AS average_tone,
  CAST(ActionGeo_FullName AS STRING) AS action_geo_name,
  CAST(ActionGeo_Lat AS FLOAT64) AS action_geo_latitude,
  CAST(ActionGeo_Long AS FLOAT64) AS action_geo_longitude,
  CAST(SOURCEURL AS STRING) AS source_url,
  PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATEADDED AS STRING)) AS date_added
FROM `gdelt-bq.gdeltv2.events_partitioned`
WHERE _PARTITIONDATE BETWEEN @start_date AND @end_date
  AND (
    Actor1CountryCode IN ('SF', 'NI', 'KE')
    OR Actor2CountryCode IN ('SF', 'NI', 'KE')
    OR ActionGeo_CountryCode IN ('SF', 'NI', 'KE')
  )"""
    gcam = r"""SELECT
  CAST(DocumentIdentifier AS STRING) AS document_url,
  PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATE AS STRING)) AS published_at,
  SAFE_CAST(REGEXP_EXTRACT(GCAM, r'(?:^|,)v10\.1:([-0-9.]+)') AS FLOAT64) AS v10_1,
  SAFE_CAST(REGEXP_EXTRACT(GCAM, r'(?:^|,)v10\.2:([-0-9.]+)') AS FLOAT64) AS v10_2,
  SAFE_CAST(REGEXP_EXTRACT(GCAM, r'(?:^|,)v19\.1:([-0-9.]+)') AS FLOAT64) AS v19_1,
  SAFE_CAST(REGEXP_EXTRACT(GCAM, r'(?:^|,)v19\.9:([-0-9.]+)') AS FLOAT64) AS v19_9,
  SAFE_CAST(REGEXP_EXTRACT(GCAM, r'(?:^|,)v20\.1:([-0-9.]+)') AS FLOAT64) AS v20_1
FROM `gdelt-bq.gdeltv2.gkg_partitioned`
WHERE _PARTITIONDATE BETWEEN @start_date AND @end_date
  AND EXISTS (
    SELECT 1
    FROM UNNEST(SPLIT(COALESCE(V2Locations, ''), ';')) AS location
    WHERE REGEXP_CONTAINS(location, r'^[1-5]#[^#]*#(?:SF|NI|KE)#')
  )"""
    return (
        GDELTDryRunPlan("events", events, start_date, end_date),
        GDELTDryRunPlan("gcam", gcam, start_date, end_date),
    )


def _wave1_plan(name: str) -> GDELTDryRunPlan:
    plans = {
        plan.name: plan
        for plan in build_wave1_gdelt_dry_run_plans(date(2026, 8, 14), date(2026, 8, 27))
    }
    try:
        return plans[name]
    except KeyError as exc:
        raise ValueError("GDELT Wave 1 plan is unsupported") from exc


def _wave1_query_config(
    plan: GDELTDryRunPlan, *, dry_run: bool, maximum_bytes_billed: int | None = None
):
    config = bigquery.QueryJobConfig(
        dry_run=dry_run,
        use_query_cache=False,
        query_parameters=(
            bigquery.ScalarQueryParameter("start_date", "DATE", plan.start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", plan.end_date),
        ),
    )
    if maximum_bytes_billed is not None:
        config.maximum_bytes_billed = maximum_bytes_billed
    return config


def execute_wave1_gdelt_dry_run(client, name: str) -> GDELTDryRunReceipt:
    plan = _wave1_plan(name)
    config = _wave1_query_config(plan, dry_run=True)
    if (
        config.dry_run is not True
        or config.destination is not None
        or config.write_disposition is not None
    ):
        raise ValueError("GDELT dry-run configuration is unsafe")
    job = client.query(plan.sql, job_config=config, location="US")
    values = {
        "name": plan.name,
        "sql_digest": hashlib.sha256(plan.sql.encode("utf-8")).hexdigest(),
        "start_date": plan.start_date,
        "end_date": plan.end_date,
        "total_bytes_processed": getattr(job, "total_bytes_processed", None),
        "created_at": getattr(job, "created", None),
        "ended_at": getattr(job, "ended", None),
        "principal_email": getattr(job, "user_email", None),
        "etag": getattr(job, "etag", None),
        "project": getattr(job, "project", None),
        "location": getattr(job, "location", None),
    }
    if getattr(job, "state", None) != "DONE":
        raise ValueError("GDELT dry-run receipt is invalid")
    if getattr(job, "job_id", None) is not None:
        raise ValueError("GDELT dry-run receipt is invalid")
    try:
        provisional = object.__new__(GDELTDryRunReceipt)
        for field, value in {"dry_run_receipt_id": "", **values}.items():
            object.__setattr__(provisional, field, value)
        receipt_id = _dry_run_receipt_id(provisional)
        return GDELTDryRunReceipt(dry_run_receipt_id=receipt_id, **values)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("GDELT dry-run receipt is invalid") from error


def wave1_maximum_bytes_billed(dry_run_bytes: int, source_lab_ceiling: int) -> int:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (dry_run_bytes, source_lab_ceiling)
    ):
        raise ValueError("GDELT byte ceilings must be positive integers")
    return min(source_lab_ceiling, (dry_run_bytes * 12 + 9) // 10)


def _bind_wave1_gdelt_manifest_entry_impl(
    execution_capability: object,
    gdelt_dry_run_artifact: bytes,
    name: str,
    *,
    _issuer,
) -> GDELTWave1ManifestEntry:
    from src.analysis.open_intelligence.funded_lane import (
        Wave1ExecutionBlocked,
        _require_wave1_execution_capability,
    )

    capability = _require_wave1_execution_capability(
        execution_capability, action=f"gdelt_manifest_entry:{name}"
    )
    try:
        manifest_sha256 = capability.manifest_sha256
        artifact_sha256 = capability.gdelt_dry_run_set_sha256
        maximum_bytes_billed = capability.maximum_bytes_billed
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise Wave1ExecutionBlocked("GDELT Wave 1 manifest is invalid") from error
    if (
        type(gdelt_dry_run_artifact) is not bytes
        or hashlib.sha256(gdelt_dry_run_artifact).hexdigest() != artifact_sha256
        or isinstance(maximum_bytes_billed, bool)
        or not isinstance(maximum_bytes_billed, int)
        or maximum_bytes_billed != _WAVE1_MANIFEST_CEILING
    ):
        raise Wave1ExecutionBlocked("GDELT Wave 1 manifest is invalid")
    try:
        artifact = json.loads(gdelt_dry_run_artifact.decode("utf-8"))
        canonical = json.dumps(
            artifact,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise Wave1ExecutionBlocked("GDELT Wave 1 dry-run artifact is invalid") from error
    if (
        canonical != gdelt_dry_run_artifact
        or not isinstance(artifact, dict)
        or set(artifact) != {"contract_version", "receipts"}
        or artifact["contract_version"] != "wave1-gdelt-dry-run-set-v1"
        or not isinstance(artifact["receipts"], list)
    ):
        raise Wave1ExecutionBlocked("GDELT Wave 1 dry-run artifact is invalid")
    receipts = artifact["receipts"]
    if len(receipts) != 2 or tuple(
        item.get("name") for item in receipts if isinstance(item, dict)
    ) != ("events", "gcam"):
        raise Wave1ExecutionBlocked("GDELT Wave 1 dry-run artifact is invalid")
    selected = None
    for item in receipts:
        try:
            receipt = _dry_run_receipt_from_mapping(item)
        except ValueError as error:
            raise Wave1ExecutionBlocked("GDELT Wave 1 dry-run artifact is invalid") from error
        if receipt.name == name:
            selected = receipt
    if selected is None:
        raise Wave1ExecutionBlocked("GDELT Wave 1 manifest entry is unavailable")
    if tuple(receipt.name for receipt in map(_dry_run_receipt_from_mapping, receipts)) != (
        "events",
        "gcam",
    ):
        raise Wave1ExecutionBlocked("GDELT Wave 1 dry-run artifact is invalid")
    try:
        plan = _wave1_plan(selected.name)
    except ValueError as error:
        raise Wave1ExecutionBlocked("GDELT Wave 1 manifest entry is unavailable") from error
    if hashlib.sha256(plan.sql.encode("utf-8")).hexdigest() != selected.sql_digest:
        raise Wave1ExecutionBlocked("GDELT Wave 1 SQL digest is not approval-bound")
    return _issuer(
        manifest_sha256,
        selected.name,
        selected.dry_run_receipt_id,
        selected.sql_digest,
        selected.total_bytes_processed,
        maximum_bytes_billed,
    )


def _make_wave1_gdelt_manifest_binder(issuer, implementation):
    def bind(
        execution_capability: object,
        gdelt_dry_run_artifact: bytes,
        name: str,
    ) -> GDELTWave1ManifestEntry:
        return implementation(
            execution_capability,
            gdelt_dry_run_artifact,
            name,
            _issuer=issuer,
        )

    return bind


bind_wave1_gdelt_manifest_entry = _make_wave1_gdelt_manifest_binder(
    _issue_wave1_gdelt_manifest_entry,
    _bind_wave1_gdelt_manifest_entry_impl,
)
del _issue_wave1_gdelt_manifest_entry
del _bind_wave1_gdelt_manifest_entry_impl
del _make_wave1_gdelt_manifest_binder


def execute_wave1_gdelt_query(
    client,
    manifest_entry: object,
    dry_run_receipt: GDELTDryRunReceipt,
    *,
    execution_capability: object,
):
    from src.analysis.open_intelligence.funded_lane import (
        Wave1ExecutionBlocked,
        _require_wave1_execution_capability,
        wave1_pilot_service_identity,
    )

    capability = _require_wave1_execution_capability(
        execution_capability, action="gdelt_result_query"
    )
    entry = _require_wave1_gdelt_manifest_entry(manifest_entry, capability.manifest_sha256)
    credentials = getattr(client, "_credentials", None)
    if (
        getattr(client, "project", None) != _WAVE1_PROJECT
        or getattr(client, "location", None) != _WAVE1_LOCATION
        or getattr(credentials, "service_account_email", None) != wave1_pilot_service_identity()
        or getattr(credentials, "quota_project_id", None) not in (None, _WAVE1_PROJECT)
    ):
        raise Wave1ExecutionBlocked("GDELT Wave 1 result query target is invalid")
    try:
        receipt = _validate_wave1_gdelt_dry_run_receipt(dry_run_receipt)
    except ValueError as error:
        raise Wave1ExecutionBlocked("GDELT Wave 1 dry-run receipt is not approval-bound") from error
    if (
        receipt.dry_run_receipt_id,
        receipt.name,
        receipt.sql_digest,
        receipt.total_bytes_processed,
    ) != (
        entry.dry_run_receipt_id,
        entry.name,
        entry.sql_digest,
        entry.total_bytes_processed,
    ):
        raise Wave1ExecutionBlocked("GDELT Wave 1 dry-run receipt is not approval-bound")
    plan = _wave1_plan(entry.name)
    if hashlib.sha256(plan.sql.encode("utf-8")).hexdigest() != entry.sql_digest:
        raise Wave1ExecutionBlocked("GDELT Wave 1 SQL digest is not approval-bound")
    maximum_bytes_billed = wave1_maximum_bytes_billed(
        receipt.total_bytes_processed, entry.maximum_bytes_billed
    )
    config = _wave1_query_config(plan, dry_run=False, maximum_bytes_billed=maximum_bytes_billed)
    return client.query(plan.sql, job_config=config, location="US")


class GDELTConnector(BaseConnector):
    """Fetches news and entity records from the GDELT GKG BigQuery public dataset."""

    SOURCE_NAME = "gdelt"
    PLATFORM = "news"

    # GDELT uses FIPS 10-4 country codes. ZA -> SF, NG -> NI, KE -> KE.
    FIPS_CODES: ClassVar[dict[str, str]] = {"za": "SF", "ng": "NI", "ke": "KE"}

    # Theme prefix to query_group mapping. Kept deliberately small; the
    # enrichment step runs detect_topic over the full row afterwards, so
    # this is only a coarse fallback. Longest prefix wins on lookup.
    THEME_TO_GROUP: ClassVar[dict[str, str]] = {
        "WB_": "economy",
        "ECON_": "economy",
        "LEADER": "politics",
        "ELECTION": "politics",
        "PROTEST": "protest",
        "RELIGION": "religion",
        "EDUCATION": "education",
        "CRIME": "crime",
        "HEALTH": "health",
        "SPORT": "sport",
    }

    DEFAULT_DAYS = 1
    DEFAULT_LIMIT = 2000

    def fetch(self, **kwargs: Any) -> pd.DataFrame:
        """Fetch GDELT GKG rows, plus V2 Events rows when the dark flag is on.

        GKG is the always-on path. Events ride alongside it behind the dark
        ``gdelt_events_enabled`` flag: ``fetch_events()`` self-guards and
        returns an empty frame when the flag is off, so the output is
        byte-identical to GKG-only until the flag flips. This is the only
        production caller of ``fetch_events()``.
        """
        if {
            "execution_capability",
            "manifest_entry",
            "dry_run_receipt",
            "maximum_bytes_billed",
        }.intersection(kwargs):
            raise ValueError("Normal GDELT fetch cannot accept Wave 1 authority")
        gkg = self._fetch_gkg(**kwargs)
        events = self.fetch_events()
        if events.empty:
            return gkg
        if gkg.empty:
            return events
        return pd.concat([gkg, events], ignore_index=True)

    def _fetch_gkg(self, **kwargs: Any) -> pd.DataFrame:
        """Fetch GDELT GKG rows for this connector's market.

        Reads the SQL from infra/bigquery_queries/gdelt_gkg.sql and binds
        @country_fips, @days, @limit from the sources.yaml gdelt block
        (or the class defaults).

        Returns an empty 16-column DataFrame when the config is inactive,
        when the market has no FIPS mapping, when the query fails, or when
        the dataset has no rows for the market.
        """
        config = load_sources().get("gdelt", {}) or {}

        if config.get("active", True) is False:
            self.logger.info(
                "gdelt inactive in sources.yaml, skipping (market=%s)",
                self.market,
            )
            return self.empty_dataframe()

        country_fips = self.FIPS_CODES.get(self.market.lower())
        if not country_fips:
            self.logger.warning(
                "No FIPS code mapping for market=%s (supported: %s), skipping",
                self.market,
                list(self.FIPS_CODES.keys()),
            )
            return self.empty_dataframe()

        days = int(config.get("days", self.DEFAULT_DAYS))
        limit = int(config.get("limit", self.DEFAULT_LIMIT))

        try:
            sql = _SQL_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.logger.error("SQL file missing at %s", _SQL_PATH)
            return self.empty_dataframe()

        # 28 May 2026: optional GCAM column. {gcam_column} placeholder in the
        # SQL is either ", GCAM AS gcam" (enabled) or "" (disabled). Lives
        # outside the bound-parameter system because BigQuery does not allow
        # parameterising column lists. Per-market flag at gdelt.gcam_enabled
        # so the column scan is only paid for markets that consume it.
        gcam_enabled = bool(config.get("gcam_enabled", False))
        sql = sql.replace("{gcam_column}", ",\n  GCAM AS gcam" if gcam_enabled else "")

        # Phase 2 (27 May 2026, reworked 29 May 2026): optional slang pattern.
        # Reads ``gdelt.slang_filter_enabled`` + per-market term list from
        # ``gdelt_slang.<market>`` in sources.yaml. Pattern is built
        # lower-cased; the SQL lowercases V2Persons so the match is
        # case-insensitive. As of 29 May the pattern is a SORT PRIORITY, not a
        # WHERE exclusion: every country row is kept up to @limit and
        # slang-matching rows are ranked first. The earlier exclusion model
        # cratered volume (NG 402 -> 46) by subtracting country news, which
        # classifies at ~76% downstream and is the highest-signal source.
        # Per-market term lists carry NAMED ENTITIES (Tinubu, Davido, Ruto,
        # Tyla) alongside cultural slang -- V2Persons surfaces named-entity
        # matches so those rows float to the top of the window.
        slang_pattern = ""
        sources = load_sources()
        if config.get("slang_filter_enabled"):
            terms = (sources.get("gdelt_slang", {}) or {}).get(self.market.lower(), []) or []
            slang_pattern = self._build_slang_pattern(terms)
            if not slang_pattern:
                self.logger.warning(
                    "gdelt.slang_filter_enabled set but no terms for market=%s",
                    self.market,
                )

        params = {
            "country_fips": country_fips,
            "days": days,
            "limit": limit,
            "slang_pattern": slang_pattern,
        }

        try:
            results = bq_utils.run_query(sql, params=params)
        except (BadRequest, NotFound) as e:
            self.logger.error(
                "BigQuery query failed for market=%s: %s",
                self.market,
                str(e)[:300],
            )
            return self.empty_dataframe()
        except Exception as e:
            self.logger.error(
                "Unexpected error running GDELT GKG query for market=%s: %s",
                self.market,
                str(e)[:300],
            )
            return self.empty_dataframe()

        if results is None or results.empty:
            self.logger.warning(
                "GDELT GKG returned 0 rows for market=%s (country_fips=%s)",
                self.market,
                country_fips,
            )
            return self.empty_dataframe()

        records = results.to_dict(orient="records")
        rows = [self._normalise_row(r, gcam_enabled=gcam_enabled) for r in records]
        df = pd.DataFrame(rows, columns=self.empty_dataframe().columns.tolist())
        df = df.drop_duplicates(subset=["url"])

        # 28 May 2026: parse GCAM dims for log surfacing when the column is
        # pulled. Wave 0.2 (19 Jun 2026): the parsed dims now also persist on
        # v2gcam via _normalise_row (see below) once the base RAW_COLUMNS schema
        # and the BQ column add land. The flag stays dark on ship, so the SQL
        # omits the GCAM column and v2gcam is empty until gdelt.gcam_enabled
        # flips. This log line still proves the signal end-to-end.
        if gcam_enabled and records:
            parsed = [self._parse_gcam(str(r.get("gcam") or "")) for r in records]
            non_empty = [p for p in parsed if p]
            if non_empty:
                self.logger.info(
                    "GCAM parsed for %d/%d rows (market=%s, dims=%s)",
                    len(non_empty),
                    len(parsed),
                    self.market,
                    list(_GCAM_TARGET_DIMS),
                )

        return df

    # -----------------------------------------------------------------------
    # Events fetch (28 May 2026)
    # -----------------------------------------------------------------------

    # GDELT Events table uses CAMEO 3-letter country codes, NOT FIPS 10-4.
    # Verified 28 May 2026 against bigquery-public-data.gdeltv2.events:
    # SAF/NGA/KEN return live rows, SF/NI/KE return ~0 rows. Spec called for
    # FIPS reuse; live probe contradicted spec.
    CAMEO_CODES: ClassVar[dict[str, str]] = {"za": "SAF", "ng": "NGA", "ke": "KEN"}

    # CAMEO root code -> short human phrase. Used to compose synthetic title
    # and text on events rows. Mapping covers the default root filter
    # (14, 15, 17, 18, 19) plus the cooperation roots a future operator may
    # opt into.
    EVENT_ROOT_PHRASES: ClassVar[dict[str, str]] = {
        "01": "made public statement to",
        "02": "appealed to",
        "03": "expressed intent to cooperate with",
        "04": "consulted with",
        "05": "engaged diplomatically with",
        "06": "engaged materially with",
        "07": "provided aid to",
        "08": "yielded to",
        "09": "investigated",
        "10": "demanded action from",
        "11": "disapproved of",
        "12": "rejected",
        "13": "threatened",
        "14": "protested against",
        "15": "exhibited force against",
        "16": "reduced relations with",
        "17": "coerced",
        "18": "assaulted",
        "19": "fought",
        "20": "engaged in unconventional mass violence against",
    }

    DEFAULT_EVENT_ROOT_FILTER = "^(14|15|17|18|19)$"
    DEFAULT_EVENT_DAYS = 1
    DEFAULT_EVENT_LIMIT = 500

    def fetch_events(self) -> pd.DataFrame:
        """Fetch GDELT V2 Events rows for this connector's market.

        Reads SQL from infra/bigquery_queries/gdelt_events.sql and binds
        @country_code (CAMEO), @date_from, @date_to, @event_root_filter,
        @lim from the sources.yaml ``gdelt`` block.

        Returns an empty 20-column DataFrame when the flag is off, the
        market has no CAMEO mapping, the query fails, or no rows match.
        """
        config = load_sources().get("gdelt", {}) or {}

        if not config.get("gdelt_events_enabled", False):
            self.logger.info("gdelt_events disabled, skipping (market=%s)", self.market)
            return self.empty_dataframe()

        country_code = self.CAMEO_CODES.get(self.market.lower())
        if not country_code:
            self.logger.warning(
                "No CAMEO code mapping for market=%s (supported: %s), skipping events",
                self.market,
                list(self.CAMEO_CODES.keys()),
            )
            return self.empty_dataframe()

        days = int(config.get("event_days", self.DEFAULT_EVENT_DAYS))
        limit = int(config.get("event_limit", self.DEFAULT_EVENT_LIMIT))
        event_root_filter = str(
            config.get("gdelt_event_root_filter", self.DEFAULT_EVENT_ROOT_FILTER)
        )

        from datetime import timedelta as _timedelta

        # Use the UTC date, not the box-local date. On a UTC+2 host the local
        # date rolls over two hours early, shifting both window bounds a day and
        # skewing the SQLDATE filter plus the _PARTITIONTIME prune.
        today = datetime.now(UTC).date()
        date_to = int(today.strftime("%Y%m%d"))
        date_from = int((today - _timedelta(days=days)).strftime("%Y%m%d"))

        try:
            sql = _EVENTS_SQL_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.logger.error("Events SQL missing at %s", _EVENTS_SQL_PATH)
            return self.empty_dataframe()

        params = {
            "country_code": country_code,
            "date_from": date_from,
            "date_to": date_to,
            "event_root_filter": event_root_filter,
            "lim": limit,
        }

        try:
            results = bq_utils.run_query(sql, params=params)
        except (BadRequest, NotFound) as e:
            self.logger.error(
                "GDELT events query failed for market=%s: %s",
                self.market,
                str(e)[:300],
            )
            return self.empty_dataframe()
        except Exception as e:
            self.logger.error(
                "Unexpected error running GDELT events query for market=%s: %s",
                self.market,
                str(e)[:300],
            )
            return self.empty_dataframe()

        if results is None or results.empty:
            self.logger.warning(
                "GDELT events returned 0 rows for market=%s (CAMEO=%s)",
                self.market,
                country_code,
            )
            return self.empty_dataframe()

        rows = [self._normalise_event_row(r) for r in results.to_dict(orient="records")]
        # Dedup on GLOBALEVENTID, not url. SOURCEURL is empty for non-web events
        # and shared across events from one article, so a url-based dedup
        # collapses distinct GLOBALEVENTIDs into one survivor. Carry the id as a
        # transient column, dedup on it, then drop it before returning so the
        # frame stays at the canonical schema.
        df = pd.DataFrame(
            rows, columns=[*self.empty_dataframe().columns.tolist(), "_global_event_id"]
        )
        df = df.drop_duplicates(subset=["_global_event_id"])
        df = df.drop(columns=["_global_event_id"])
        return df

    def _normalise_event_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Map one Events row to raw_content schema with content_type='gdelt_event'."""
        actor1 = str(row.get("Actor1CountryCode") or "").strip()
        actor2 = str(row.get("Actor2CountryCode") or "").strip()
        event_root = str(row.get("EventRootCode") or "").zfill(2)
        location = str(row.get("ActionGeo_FullName") or "").strip()
        url = str(row.get("SOURCEURL") or "")
        event_code = str(row.get("EventCode") or "")

        phrase = self.EVENT_ROOT_PHRASES.get(event_root, "interacted with")
        title = f"{event_code} {location}".strip() or event_code or "gdelt_event"
        text_parts = [actor1, phrase, actor2]
        text = " ".join(p for p in text_parts if p)
        if location:
            text = f"{text} at {location}"

        # Engagement: views=NumArticles, likes=NumSources, comments=NumMentions
        def _f(key: str) -> float:
            try:
                return float(row.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        # Compose v2tone string from AvgTone scalar so existing v2tone parser
        # (tone_avg / tone_polarity) downstream gets a non-empty value.
        avg_tone = row.get("AvgTone")
        v2tone = f"{float(avg_tone):.4f},0,0,0,0,0,0" if avg_tone is not None else ""

        return {
            "source": "gdelt",
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "gdelt_event",
            "query_group": f"gdelt_event_root_{event_root}",
            "query_term": event_code,
            "author_name": "",
            "author_handle": "",
            "title": title,
            "text": text,
            "url": url,
            "published_at": self._parse_sqldate(row.get("SQLDATE")),
            "views": _f("NumArticles"),
            "likes": _f("NumSources"),
            "comments": _f("NumMentions"),
            "shares": 0.0,
            "v2tone": v2tone,
            "v2persons": "",
            "v2orgs": "",
            "v2locations": location,
            # GCAM is a GKG-only signal; Events rows never carry it. Empty
            # string keeps the column aligned with the other v2* fields.
            "v2gcam": "",
            # Transient dedup key, dropped before the frame leaves fetch_events.
            "_global_event_id": str(row.get("GLOBALEVENTID") or ""),
        }

    @staticmethod
    def _parse_sqldate(value: Any) -> Any:
        """Parse YYYYMMDD int into a tz-aware UTC datetime at 00:00."""
        if value is None:
            return None
        try:
            raw = str(int(value))
        except (TypeError, ValueError):
            return None
        if len(raw) != 8:
            return None
        try:
            parsed = datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC)

    # -----------------------------------------------------------------------
    # GCAM parser (28 May 2026)
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_gcam(raw: str) -> dict[str, float]:
        """Parse a GCAM comma-separated string and return the target dims.

        GCAM format: comma-separated ``<code>:<value>`` tokens. The leading
        token is always ``wc:<int>`` (word count). Float-valued tokens are
        prefixed ``v`` (e.g. ``v10.1:0.252...``); integer-count tokens are
        prefixed ``c``. Returns a dict keyed by the codes in
        _GCAM_TARGET_DIMS; codes missing from the row are absent from the
        return value (NOT zeroed) so callers can distinguish "absent" from
        "zero". Malformed tokens are skipped silently.
        """
        if not raw:
            return {}
        targets = set(_GCAM_TARGET_DIMS)
        out: dict[str, float] = {}
        for tok in raw.split(","):
            if ":" not in tok:
                continue
            code, _, val = tok.partition(":")
            code = code.strip()
            if code not in targets:
                continue
            try:
                out[code] = float(val)
            except (TypeError, ValueError):
                continue
        return out

    @classmethod
    def _gcam_to_persist_string(cls, raw: str) -> str:
        """Serialise the target GCAM dims back to a compact ``code:value`` string.

        Reuses ``_parse_gcam`` (which keeps only ``_GCAM_TARGET_DIMS`` and drops
        malformed tokens) then re-joins the survivors as comma-separated
        ``code:value`` pairs in the fixed _GCAM_TARGET_DIMS order. This is the
        value persisted to the v2gcam STRING column: a deterministic,
        target-only projection of the raw vendor string, not the full ~7KB
        GCAM blob. Returns '' when nothing parses so the column stays empty
        rather than carrying noise.
        """
        parsed = cls._parse_gcam(raw)
        if not parsed:
            return ""
        return ",".join(f"{code}:{parsed[code]}" for code in _GCAM_TARGET_DIMS if code in parsed)

    def _normalise_row(self, row: dict[str, Any], gcam_enabled: bool = False) -> dict[str, Any]:
        """Map one GKG row to the raw schema, keeping tone/persons/orgs/locations.

        When ``gcam_enabled`` is true, the parsed GCAM target dims are persisted
        on v2gcam as a ``code:value`` string. When false (the dark default) the
        SQL never selects the GCAM column, so ``row`` has no ``gcam`` key and
        v2gcam stays empty, keeping behaviour byte-identical to pre-Wave-0.2.
        """
        themes_raw = str(row.get("themes") or "")
        first_theme = themes_raw.split(";", 1)[0] if themes_raw else ""
        query_group = self._theme_to_group(first_theme)

        published_at = self._parse_gdelt_date(row.get("DATE"))

        source = str(row.get("source") or "")
        url = str(row.get("url") or "")
        v2tone = str(row.get("tone") or "")
        v2persons = str(row.get("persons") or "")
        v2orgs = str(row.get("orgs") or "")
        v2locations = str(row.get("locations") or "")
        # Wave 0.2: persist the parsed GCAM target dims only when the column was
        # selected (flag on). Dark default leaves this empty.
        v2gcam = self._gcam_to_persist_string(str(row.get("gcam") or "")) if gcam_enabled else ""

        return {
            "source": source,
            "platform": self.PLATFORM,
            "market": self.market,
            "content_type": "gdelt_gkg",
            "query_group": query_group,
            "query_term": first_theme,
            "author_name": "",
            "author_handle": "",
            "title": "",
            # Synthesise text from GKG themes + persons + orgs + source so
            # enrichment's full_text (title + text) isn't empty. Without this
            # GDELT rows score 0 on regional / genz / slang because there's
            # nothing to match against. GKG gives no article body, so this
            # is the best available fallback. Semicolons become spaces so
            # multi-word entities are tokenised correctly.
            "text": f"{themes_raw} {row.get('persons') or ''} {row.get('orgs') or ''} {source}".replace(
                ";", " "
            ).strip(),
            "url": url,
            "published_at": published_at,
            "views": 0.0,
            "likes": 0.0,
            "comments": 0.0,
            "shares": 0.0,
            "v2tone": v2tone,
            "v2persons": v2persons,
            "v2orgs": v2orgs,
            "v2locations": v2locations,
            "v2gcam": v2gcam,
        }

    @classmethod
    def _theme_to_group(cls, theme: str) -> str:
        """Map a GKG theme code to a coarse query_group bucket.

        Longest-prefix wins so that ELECTION beats a shorter match. Unknown
        themes fall through to "gdelt_other".
        """
        if not theme:
            return "gdelt_other"
        for prefix in sorted(cls.THEME_TO_GROUP, key=len, reverse=True):
            if theme.startswith(prefix):
                return cls.THEME_TO_GROUP[prefix]
        return "gdelt_other"

    @staticmethod
    def _build_slang_pattern(terms: list[str]) -> str:
        """Build a case-insensitive REGEXP alternation from a list of slang terms.

        Each term is escaped (so 'jua kali' with a space matches literally)
        and wrapped in a non-capturing group. Returns '' when terms is
        empty so the SQL fall-through preserves country-only behaviour.
        """
        import re as _re

        cleaned = [str(t).strip().lower() for t in terms if str(t).strip()]
        if not cleaned:
            return ""
        return "(?:" + "|".join(_re.escape(t) for t in cleaned) + ")"

    @staticmethod
    def _parse_gdelt_date(value: Any) -> Any:
        """Parse a GKG DATE (YYYYMMDDHHMMSS int) to a tz-aware UTC datetime."""
        if value is None:
            return None
        try:
            raw = str(int(value))
        except (TypeError, ValueError):
            return None
        if len(raw) != 14:
            return None
        try:
            parsed = datetime.strptime(raw, "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC)
