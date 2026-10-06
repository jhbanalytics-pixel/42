"""Bounded, zero-write dynamic signal identity pipeline."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from types import MappingProxyType

from google.cloud import bigquery

from src.analysis.open_intelligence.candidates import (
    APPROVED_MARKETS,
    CandidateInputs,
    Observation,
    canonicalize_observations_with_mapping,
    extract_event_observations,
    extract_seed_candidate_observations,
    extract_seed_graph_observations,
    resolve_source_identity,
    resolve_wave1_market,
)
from src.analysis.open_intelligence.composition import (
    SemanticSimilarityProvider,
    build_components_with_validated_semantics,
)
from src.analysis.open_intelligence.graph import GraphRules, SignalComponent
from src.contracts.open_intelligence import ResolvedScope, encode_identifier_part

TARGET_PROJECT = "ogilvy-trends-v2"
TARGET_DATASET = "trends_v2_staging"
TARGET_LOCATION = "US"
ACCEPTED_SEED_CANDIDATE_STATUSES = frozenset({"approved", "applied"})
SOURCE_CEILINGS = MappingProxyType(
    {"event_ledger": 25_000, "seed_graph": 15_000, "seed_candidates": 45}
)
ROW_TABLES = ("candidates", "evidence", "membership", "lineage", "predictions")
MAX_SAMPLE_IDS_PER_ROW = 5
MAX_REQUESTED_SAMPLE_KEYS = 75_225

EVENT_COLUMNS = (
    "ledger_id",
    "trend_date",
    "market",
    "entity_key",
    "entity_aliases",
    "event_kind",
    "state_label",
    "as_of",
    "corroborating_sources",
    "source_count",
    "confidence",
)
SEED_GRAPH_COLUMNS = (
    "market",
    "term",
    "term_type",
    "platform",
    "trend_date",
    "event_date",
    "row_count",
    "topic_groups",
    "near_topics",
    "co_occur_terms",
    "sample_row_ids",
)
SEED_CANDIDATE_COLUMNS = (
    "candidate_id",
    "proposed_date",
    "market",
    "candidate_type",
    "candidate_value",
    "source",
    "lane",
    "score",
    "evidence_topics",
    "sample_row_ids",
    "status",
)
COMMON_EVIDENCE_COLUMNS = (
    "id",
    "source",
    "platform",
    "market",
    "content_type",
    "query_group",
    "query_term",
    "author_name",
    "author_handle",
    "title",
    "text",
    "url",
    "published_at",
    "collected_at",
    "hashtags",
    "views",
    "likes",
    "comments",
    "shares",
    "engagement_total",
    "pipeline_run_id",
    "v2tone",
    "v2persons",
    "v2orgs",
    "v2locations",
    "v2gcam",
)
WAVE1_AUTHORITY_COLUMNS = (
    "endpoint",
    "vendor_family",
    "channel_family",
    "source_family",
    "geo_method_id",
    "geo_receipt_id",
    "native_id",
    "source_family_map_version",
)
ENRICHED_EVIDENCE_COLUMNS = (
    *COMMON_EVIDENCE_COLUMNS,
    "author_handle_norm",
    "regional_score",
    "slang_terms",
    "search_velocity_score",
    "tone_avg",
    "tone_polarity",
    "topic_groups",
    "classification_layer",
    "sentiment_lexicon_score",
)
RAW_EVIDENCE_COLUMNS = COMMON_EVIDENCE_COLUMNS
EVIDENCE_COLUMNS_BY_TABLE = MappingProxyType(
    {
        "enriched_content": (*ENRICHED_EVIDENCE_COLUMNS, *WAVE1_AUTHORITY_COLUMNS),
        "raw_content": (*RAW_EVIDENCE_COLUMNS, *WAVE1_AUTHORITY_COLUMNS),
    }
)
APPROVED_SOURCE_TABLES = frozenset(
    f"{TARGET_PROJECT}.{TARGET_DATASET}.{table}"
    for table in (
        "event_ledger",
        "seed_graph",
        "seed_candidates",
        "enriched_content",
        "raw_content",
    )
)


class SourceRowCeilingExceeded(ValueError):
    pass


class CompositionDependencyMissing(ValueError):
    pass


class EvidenceProjectionAmbiguous(ValueError):
    pass


class EvidenceProjectionLimitExceeded(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceWindow:
    start_date: date
    end_date: date
    markets: tuple[str, ...]
    source_tables: tuple[str, ...]
    complete_partitions: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.start_date, datetime)
            or not isinstance(self.start_date, date)
            or isinstance(self.end_date, datetime)
            or not isinstance(self.end_date, date)
            or self.start_date > self.end_date
        ):
            raise ValueError("source window dates are invalid")
        if (
            not isinstance(self.markets, tuple)
            or not self.markets
            or tuple(sorted(set(self.markets))) != self.markets
            or any(
                not isinstance(market, str)
                or market != market.lower()
                or market not in APPROVED_MARKETS
                for market in self.markets
            )
        ):
            raise ValueError("source window markets are invalid")
        if (
            not isinstance(self.source_tables, tuple)
            or not self.source_tables
            or tuple(sorted(set(self.source_tables))) != self.source_tables
            or any(table not in APPROVED_SOURCE_TABLES for table in self.source_tables)
        ):
            raise ValueError("source window tables are invalid")
        if type(self.complete_partitions) is not bool:
            raise ValueError("source window completeness is invalid")


@dataclass(frozen=True, slots=True)
class ProductionSourceWindow(SourceWindow):
    def __post_init__(self) -> None:
        SourceWindow(
            self.start_date,
            self.end_date,
            self.markets,
            tuple(sorted(APPROVED_SOURCE_TABLES)),
            self.complete_partitions,
        )
        allowed = {
            f"{TARGET_PROJECT}.{TARGET_DATASET}.open_intelligence_v3_source_"
            f"{self.end_date:%Y%m%d}_{table.rsplit('.', 1)[-1]}"
            for table in APPROVED_SOURCE_TABLES
        }
        if (
            not isinstance(self.source_tables, tuple)
            or not self.source_tables
            or tuple(sorted(set(self.source_tables))) != self.source_tables
            or any(table not in allowed for table in self.source_tables)
        ):
            raise ValueError("production source window tables are invalid")


@dataclass(frozen=True, slots=True)
class ProjectedEvidenceRow:
    row_id: str
    market: str
    source: str
    platform: str
    vendor_family: str
    channel_family: str
    content_type: str | None
    query_group: str | None
    query_term: str | None
    author_name: str | None
    author_handle: str | None
    author_handle_norm: str | None
    title: str | None
    text: str | None
    url: str | None
    published_at: datetime | None
    collected_at: datetime
    hashtags: str | None
    views: float | None
    likes: float | None
    comments: float | None
    shares: float | None
    engagement_total: float | None
    regional_score: float | None
    search_velocity_score: float | None
    slang_terms: str | None
    pipeline_run_id: str | None
    v2tone: str | None
    v2persons: str | None
    v2orgs: str | None
    v2locations: str | None
    v2gcam: str | None
    tone_avg: float | None
    tone_polarity: float | None
    topic_groups: tuple[str, ...] | None
    classification_layer: str | None
    sentiment_lexicon_score: float | None
    source_table: str


@dataclass(frozen=True, slots=True)
class MembershipReceipt:
    member_id: str
    member_identity: str
    candidate_type: str
    canonical_value: str
    source_families: tuple[str, ...]
    platforms: tuple[str, ...]
    row_id: str
    qualifies_evidence: bool
    vendor_families: tuple[str, ...] = ()
    channel_families: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceProjection:
    candidate_inputs: CandidateInputs
    memberships_by_component: Mapping[str, tuple[MembershipReceipt, ...]]
    receipts_by_member: Mapping[str, tuple[ProjectedEvidenceRow, ...]]
    candidate_only_receipts: Mapping[str, tuple[str, ...]]
    unresolved_sample_ids: tuple[str, ...]
    missing_work: tuple[str, ...]
    source_window: SourceWindow


@dataclass(frozen=True, slots=True)
class DynamicSignalRunResult:
    run_id: str
    contract_version: str
    rule_version: str | None
    source_windows: Mapping[str, SourceWindow]
    input_counts: Mapping[str, int]
    observation_count: int
    component_count: int
    persistable_count: int
    row_counts: Mapping[str, int]
    skipped_components: tuple[object, ...]
    missing_work: tuple[str, ...]
    dry_run: bool
    persistence_result: object | None
    error_state: str | None
    candidate_inputs: CandidateInputs
    observations: tuple[Observation, ...]
    components: tuple[SignalComponent, ...]
    evidence_projection: EvidenceProjection | None


@dataclass(frozen=True, slots=True)
class ProvenanceSignalRunResult:
    run: DynamicSignalRunResult
    source_provenance_by_member: Mapping[str, Mapping[str, object]]
    source_snapshot_digest: str


def _validate_scope(scope: ResolvedScope) -> tuple[str, ...]:
    if not isinstance(scope, ResolvedScope):
        raise ValueError("resolved scope is required")
    markets = tuple(sorted(scope.market_scope))
    if (
        not markets
        or len(markets) != len(set(markets))
        or any(market != market.lower() or market not in APPROVED_MARKETS for market in markets)
    ):
        raise ValueError("resolved scope markets are invalid")
    return markets


def _validate_target(client: object, dataset: str) -> None:
    if getattr(client, "project", None) != TARGET_PROJECT or dataset != TARGET_DATASET:
        raise ValueError("exact staging target is required")


def _query_parameters(
    trend_date: date,
    markets: tuple[str, ...],
    *,
    statuses: tuple[str, ...] | None = None,
) -> bigquery.QueryJobConfig:
    parameters: list[bigquery.QueryParameter] = [
        bigquery.ScalarQueryParameter("start_date", "DATE", trend_date),
        bigquery.ScalarQueryParameter("end_date", "DATE", trend_date),
        bigquery.ArrayQueryParameter("markets", "STRING", list(markets)),
    ]
    if statuses is not None:
        parameters.append(bigquery.ArrayQueryParameter("statuses", "STRING", list(statuses)))
    return bigquery.QueryJobConfig(use_legacy_sql=False, query_parameters=parameters)


def _row_mapping(row: object) -> dict[str, object]:
    if isinstance(row, Mapping):
        return dict(row)
    items = getattr(row, "items", None)
    if not callable(items):
        raise ValueError("source row is not a mapping")
    return dict(items())


def _load_source(
    client: object,
    *,
    table: str,
    columns: tuple[str, ...],
    date_column: str,
    trend_date: date,
    markets: tuple[str, ...],
    statuses: tuple[str, ...] | None = None,
) -> tuple[dict[str, object], ...]:
    ceiling = SOURCE_CEILINGS[table]
    status_clause = "\n  AND status IN UNNEST(@statuses)" if statuses is not None else ""
    order_columns = {
        "event_ledger": "market, entity_key, ledger_id",
        "seed_graph": "market, term, term_type, platform",
        "seed_candidates": "market, candidate_value, candidate_type, candidate_id",
    }
    sql = (
        f"SELECT {', '.join(columns)}\n"
        f"FROM `{TARGET_PROJECT}.{TARGET_DATASET}.{table}`\n"
        f"WHERE {date_column} BETWEEN @start_date AND @end_date\n"
        "  AND market IN UNNEST(@markets)"
        f"{status_clause}\n"
        f"ORDER BY {order_columns[table]}\n"
        f"LIMIT {ceiling + 1}"
    )
    job = client.query(
        sql,
        job_config=_query_parameters(trend_date, markets, statuses=statuses),
        location=TARGET_LOCATION,
    )
    rows = tuple(_row_mapping(row) for row in job.result(max_results=ceiling + 1))
    if len(rows) > ceiling:
        raise SourceRowCeilingExceeded(
            f"{table} exceeded configured ceiling {ceiling}; observed at least {len(rows)}"
        )
    return rows


def _empty_result(
    *,
    scope: ResolvedScope,
    windows: Mapping[str, SourceWindow],
    input_counts: Mapping[str, int],
    candidate_inputs: CandidateInputs,
    observations: tuple[Observation, ...],
    rule_version: str | None,
    missing_work: tuple[str, ...],
    components: tuple[SignalComponent, ...] = (),
    error_state: str | None = None,
    evidence_projection: EvidenceProjection | None = None,
) -> DynamicSignalRunResult:
    return DynamicSignalRunResult(
        run_id=scope.run_id,
        contract_version=scope.contract_version,
        rule_version=rule_version,
        source_windows=MappingProxyType(dict(windows)),
        input_counts=MappingProxyType(dict(input_counts)),
        observation_count=len(observations),
        component_count=len(components),
        persistable_count=0,
        row_counts=MappingProxyType(dict.fromkeys(ROW_TABLES, 0)),
        skipped_components=(),
        missing_work=missing_work,
        dry_run=True,
        persistence_result=None,
        error_state=error_state,
        candidate_inputs=candidate_inputs,
        observations=observations,
        components=components,
        evidence_projection=evidence_projection,
    )


def _observation_identity(observation: Observation) -> str:
    return f"{observation.market}|{observation.candidate_type}|{observation.term}"


def _nested_identifier(values: tuple[str, ...]) -> str:
    return "".join(encode_identifier_part(value) for value in sorted(values))


def _digest_identifier(prefix: str, *parts: str | None) -> str:
    canonical = "".join(encode_identifier_part(value) for value in parts)
    return prefix + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _seed_graph_row_id(row: Mapping[str, object]) -> str:
    trend_date = row.get("trend_date")
    if isinstance(trend_date, datetime) or not isinstance(trend_date, date):
        raise ValueError("seed graph trend date is invalid")
    return _digest_identifier(
        "seed_graph_",
        str(row.get("market")),
        str(row.get("term")),
        str(row.get("term_type")),
        str(row.get("platform")),
        trend_date.isoformat(),
    )


def _membership_row_id(candidate_receipts: tuple[str, ...]) -> str:
    if not candidate_receipts:
        raise ValueError("candidate membership receipt is unavailable")
    if len(candidate_receipts) == 1:
        return candidate_receipts[0]
    return _digest_identifier("candidate_set_", _nested_identifier(candidate_receipts))


def _member_id(
    scope: ResolvedScope,
    trend_date: date,
    observation: Observation,
    row_id: str,
) -> str:
    return _digest_identifier(
        "mem_",
        scope.client_scope_id,
        trend_date.isoformat(),
        observation.market,
        observation.candidate_type,
        observation.term,
        _nested_identifier(observation.source_families),
        _nested_identifier(observation.platforms),
        row_id,
    )


def _component_key(component: SignalComponent) -> str:
    return _digest_identifier("component_", _nested_identifier(component.member_identities))


def _source_table(table: str) -> str:
    return f"{TARGET_PROJECT}.{TARGET_DATASET}.{table}"


def _sample_ids(row: Mapping[str, object], source: str) -> tuple[str, ...]:
    raw = row.get("sample_row_ids")
    if not isinstance(raw, (tuple, list)) or any(
        not isinstance(item, str) or not item for item in raw
    ):
        raise EvidenceProjectionLimitExceeded(f"{source} sample IDs are invalid")
    if len(raw) > MAX_SAMPLE_IDS_PER_ROW:
        raise EvidenceProjectionLimitExceeded(
            f"{source} sample ID count exceeds {MAX_SAMPLE_IDS_PER_ROW}"
        )
    return tuple(sorted(set(raw)))


def _evidence_query_config(
    keys: tuple[tuple[str, str], ...],
    start_date: date,
    end_date: date,
) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        use_legacy_sql=False,
        query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
            bigquery.ArrayQueryParameter("sample_markets", "STRING", [key[0] for key in keys]),
            bigquery.ArrayQueryParameter("sample_ids", "STRING", [key[1] for key in keys]),
        ],
    )


def _load_evidence_rows(
    client: object,
    *,
    table: str,
    keys: tuple[tuple[str, str], ...],
    start_date: date,
    end_date: date,
) -> tuple[dict[str, object], ...]:
    if not keys:
        return ()
    columns = EVIDENCE_COLUMNS_BY_TABLE[table]
    sql = (
        "WITH requested AS (\n"
        "  SELECT @sample_markets[OFFSET(position)] AS market, "
        "@sample_ids[OFFSET(position)] AS id\n"
        "  FROM UNNEST(GENERATE_ARRAY(0, ARRAY_LENGTH(@sample_ids) - 1)) AS position\n"
        ")\n"
        f"SELECT {', '.join(f'evidence.`{column}`' for column in columns)},\n"
        "  COUNT(*) OVER (PARTITION BY evidence.market, evidence.id) AS match_count\n"
        f"FROM `{TARGET_PROJECT}.{TARGET_DATASET}.{table}` AS evidence\n"
        "JOIN requested ON evidence.market = requested.market AND evidence.id = requested.id\n"
        "WHERE DATE(evidence.collected_at) BETWEEN @start_date AND @end_date\n"
        "ORDER BY evidence.market, evidence.id, evidence.collected_at, "
        "evidence.source, evidence.platform\n"
        f"LIMIT {len(keys) + 1}"
    )
    job = client.query(
        sql,
        job_config=_evidence_query_config(keys, start_date, end_date),
        location=TARGET_LOCATION,
    )
    return tuple(_row_mapping(row) for row in job.result(max_results=len(keys) + 1))


def _optional_topics(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError("evidence topic groups are invalid")
    return tuple(sorted(set(value)))


def _projected_evidence_row(row: Mapping[str, object], source_table: str) -> ProjectedEvidenceRow:
    collected_at = row.get("collected_at")
    if not isinstance(collected_at, datetime):
        raise ValueError("evidence collected_at is invalid")
    source = str(row["source"])
    platform = str(row["platform"])
    vendor, channel = resolve_source_identity(
        source=source,
        platform=platform,
        content_type=row.get("content_type"),
        endpoint=row.get("endpoint"),
        vendor_family=row.get("vendor_family"),
        channel_family=row.get("channel_family"),
    )
    return ProjectedEvidenceRow(
        row_id=str(row["id"]),
        market=str(row["market"]),
        source=source,
        platform=platform,
        content_type=row.get("content_type"),
        query_group=row.get("query_group"),
        query_term=row.get("query_term"),
        author_name=row.get("author_name"),
        author_handle=row.get("author_handle"),
        author_handle_norm=row.get("author_handle_norm"),
        title=row.get("title"),
        text=row.get("text"),
        url=row.get("url"),
        published_at=row.get("published_at"),
        collected_at=collected_at,
        hashtags=row.get("hashtags"),
        views=row.get("views"),
        likes=row.get("likes"),
        comments=row.get("comments"),
        shares=row.get("shares"),
        engagement_total=row.get("engagement_total"),
        regional_score=row.get("regional_score"),
        search_velocity_score=row.get("search_velocity_score"),
        slang_terms=row.get("slang_terms"),
        pipeline_run_id=row.get("pipeline_run_id"),
        v2tone=row.get("v2tone"),
        v2persons=row.get("v2persons"),
        v2orgs=row.get("v2orgs"),
        v2locations=row.get("v2locations"),
        v2gcam=row.get("v2gcam"),
        tone_avg=row.get("tone_avg"),
        tone_polarity=row.get("tone_polarity"),
        topic_groups=_optional_topics(row.get("topic_groups")),
        classification_layer=row.get("classification_layer"),
        sentiment_lexicon_score=row.get("sentiment_lexicon_score"),
        source_table=source_table,
        vendor_family=vendor,
        channel_family=channel,
    )


def _unique_evidence_rows(
    rows: tuple[dict[str, object], ...],
    source_table: str,
    *,
    skip_unsupported_identity: bool = False,
) -> tuple[dict[tuple[str, str], ProjectedEvidenceRow], set[tuple[str, str]]]:
    projected = {}
    ambiguous = set()
    for row in rows:
        count = row.get("match_count")
        key = (str(row.get("market")), str(row.get("id")))
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"{source_table} match count is invalid")
        if count > 1:
            ambiguous.add(key)
            projected.pop(key, None)
            continue
        if key in projected:
            ambiguous.add(key)
            projected.pop(key, None)
            continue
        if key in ambiguous:
            continue
        if row.get("source_family_map_version") == "channel_family_v2":
            # Storage keeps the admitted market after transport geography is removed.
            if "retained_geo_market" in row and row["retained_geo_market"] != row.get("market"):
                raise ValueError("stored and transport geography disagree")
            market = resolve_wave1_market(
                vendor_market=row.get("vendor_market"),
                retained_geo_market=row.get("market"),
                geo_method_id=row.get("geo_method_id"),
                geo_receipt_id=row.get("geo_receipt_id"),
            )
            if market is None:
                continue
            if any(
                not isinstance(row.get(field), str) or not row[field].strip()
                for field in (
                    "native_id",
                    "url",
                    "vendor_family",
                    "channel_family",
                    "source_family",
                )
            ):
                raise ValueError("stored Wave 1 authority is incomplete")
            if row["source_family"] != row["channel_family"]:
                raise ValueError("stored source and channel families disagree")
        if skip_unsupported_identity:
            try:
                resolve_source_identity(
                    source=row.get("source"),
                    platform=row.get("platform"),
                    content_type=row.get("content_type"),
                    endpoint=row.get("endpoint"),
                    vendor_family=row.get("vendor_family"),
                    channel_family=row.get("channel_family"),
                )
            except ValueError:
                continue
        projected[key] = _projected_evidence_row(row, source_table)
    return projected, ambiguous


def _project_evidence(
    client: object,
    *,
    scope: ResolvedScope,
    trend_date: date,
    markets: tuple[str, ...],
    candidate_inputs: CandidateInputs,
    observations: tuple[Observation, ...],
    canonical_identity_by_observation: Mapping[Observation, str | None],
    components: tuple[SignalComponent, ...],
    event_sources: tuple[tuple[dict[str, object], Observation], ...],
    graph_sources: tuple[tuple[dict[str, object], Observation], ...],
    candidate_sources: tuple[tuple[dict[str, object], Observation], ...],
    skip_unsupported_identity: bool = False,
) -> EvidenceProjection:
    observations_by_identity = {_observation_identity(item): item for item in observations}
    retained = (
        {identity for component in components for identity in component.member_identities}
        if components
        else set(observations_by_identity)
    )
    candidate_receipts_by_member: dict[str, set[str]] = {}
    sample_keys_by_member: dict[str, set[tuple[str, str]]] = {}
    sample_keys: set[tuple[str, str]] = set()
    candidate_keys: set[tuple[str, str]] = set()

    def add_candidate_source(
        observation: Observation,
        candidate_row_id: str,
        sample_row_ids: tuple[str, ...],
    ) -> None:
        identity = _observation_identity(observation)
        if identity not in retained:
            return
        candidate_receipts_by_member.setdefault(identity, set()).add(candidate_row_id)
        candidate_keys.add((observation.market, candidate_row_id))
        member_samples = sample_keys_by_member.setdefault(identity, set())
        for row_id in sample_row_ids:
            key = (observation.market, row_id)
            member_samples.add(key)
            sample_keys.add(key)

    for row, source_observation in event_sources:
        observation = observations_by_identity.get(
            canonical_identity_by_observation[source_observation]
        )
        if observation is not None:
            add_candidate_source(observation, str(row["ledger_id"]), ())
    for row, source_observation in graph_sources:
        observation = observations_by_identity.get(
            canonical_identity_by_observation[source_observation]
        )
        if observation is not None:
            add_candidate_source(
                observation,
                _seed_graph_row_id(row),
                _sample_ids(row, "seed_graph"),
            )
    for row, source_observation in candidate_sources:
        observation = observations_by_identity.get(
            canonical_identity_by_observation[source_observation]
        )
        if observation is not None:
            candidate_id = str(row["candidate_id"])
            add_candidate_source(
                observation,
                candidate_id,
                _sample_ids(row, "seed_candidates"),
            )

    overlap = sample_keys.intersection(candidate_keys)
    if overlap:
        raise EvidenceProjectionLimitExceeded(
            f"candidate and sample roles overlap at {min(overlap)}"
        )
    if len(sample_keys) > MAX_REQUESTED_SAMPLE_KEYS:
        raise EvidenceProjectionLimitExceeded(
            f"requested sample keys exceed {MAX_REQUESTED_SAMPLE_KEYS}"
        )
    start_date = trend_date - timedelta(days=6)
    ordered_keys = tuple(sorted(sample_keys))
    enriched_rows = _load_evidence_rows(
        client,
        table="enriched_content",
        keys=ordered_keys,
        start_date=start_date,
        end_date=trend_date,
    )
    enriched, enriched_ambiguous = _unique_evidence_rows(
        enriched_rows,
        "enriched_content",
        skip_unsupported_identity=skip_unsupported_identity,
    )
    enriched_present = {(row["market"], row["id"]) for row in enriched_rows}
    unresolved_for_raw = tuple(
        key
        for key in ordered_keys
        if key not in enriched
        and key not in enriched_ambiguous
        and (not skip_unsupported_identity or key not in enriched_present)
    )
    raw, raw_ambiguous = _unique_evidence_rows(
        _load_evidence_rows(
            client,
            table="raw_content",
            keys=unresolved_for_raw,
            start_date=start_date,
            end_date=trend_date,
        ),
        "raw_content",
        skip_unsupported_identity=skip_unsupported_identity,
    )
    ambiguous = enriched_ambiguous | raw_ambiguous
    for key in ambiguous:
        enriched.pop(key, None)
        raw.pop(key, None)
    rows_by_key = {**raw, **enriched}
    missing = tuple(key for key in ordered_keys if key not in rows_by_key)
    receipts_by_member = {
        identity: tuple(
            rows_by_key[key]
            for key in sorted(sample_keys_by_member.get(identity, ()))
            if key in rows_by_key
        )
        for identity in sorted(retained)
    }
    candidate_only_receipts = {
        identity: tuple(sorted(candidate_receipts_by_member.get(identity, ())))
        for identity in sorted(retained)
    }
    memberships_by_component = {}
    for component in sorted(components, key=_component_key):
        memberships = []
        for identity in sorted(component.member_identities):
            observation = observations_by_identity[identity]
            candidate_receipts = candidate_only_receipts[identity]
            row_id = _membership_row_id(candidate_receipts)
            memberships.append(
                MembershipReceipt(
                    member_id=_member_id(scope, trend_date, observation, row_id),
                    member_identity=identity,
                    candidate_type=observation.candidate_type,
                    canonical_value=observation.term,
                    source_families=tuple(sorted(observation.source_families)),
                    platforms=tuple(sorted(observation.platforms)),
                    row_id=row_id,
                    qualifies_evidence=False,
                    vendor_families=(
                        (observation.vendor_family,)
                        if observation.vendor_family is not None
                        else tuple(
                            "google_youtube" if family == "youtube" else family
                            for family in sorted(observation.source_families)
                        )
                    ),
                    channel_families=(
                        (observation.channel_family,)
                        if observation.channel_family is not None
                        else tuple(sorted(observation.source_families))
                    ),
                )
            )
        memberships_by_component[_component_key(component)] = tuple(
            sorted(memberships, key=lambda item: item.member_identity)
        )
    missing_work = []
    if ambiguous:
        missing_work.append("ambiguous_sample_id")
    if missing:
        missing_work.append("evidence_projection_incomplete")
    return EvidenceProjection(
        candidate_inputs=candidate_inputs,
        memberships_by_component=MappingProxyType(dict(sorted(memberships_by_component.items()))),
        receipts_by_member=MappingProxyType(receipts_by_member),
        candidate_only_receipts=MappingProxyType(candidate_only_receipts),
        unresolved_sample_ids=tuple(sorted({row_id for _, row_id in missing})),
        missing_work=tuple(missing_work),
        source_window=SourceWindow(
            start_date,
            trend_date,
            markets,
            tuple(_source_table(table) for table in ("enriched_content", "raw_content")),
            False,
        ),
    )


compose_components = build_components_with_validated_semantics


def _fail_closed_projection_work(
    projection: EvidenceProjection,
    observations: tuple[Observation, ...],
) -> tuple[str, ...]:
    missing = set(projection.missing_work)
    missing.add("evidence_receipt_policy_unavailable")
    missing.add("geo_provenance_unavailable")
    if not projection.source_window.complete_partitions:
        missing.add("incomplete_current_window")
    if observations:
        missing.add("no_qualifying_evidence")
    return tuple(sorted(missing))


def run_dynamic_signal_identity_v3(
    trend_date: date,
    scope: ResolvedScope,
    client: object,
    dataset: str,
    persist: bool,
    *,
    source_snapshot: Mapping[str, object],
    rule_bundle: object | None = None,
    semantic_provider: object | None = None,
    metric_provider: object | None = None,
) -> ProvenanceSignalRunResult:
    from scripts.staging.replay_open_intelligence import (
        ROOT,
        CompositionRuleBundleV3,
        LexicalSimilarityProviderV2,
        ProvenanceSnapshotClient,
        _digest,
        load_composition_rules_v3,
        validate_source_snapshot_v3,
    )

    from src.analysis.open_intelligence.composition import (
        build_components_with_provenance_semantics,
    )
    from src.analysis.open_intelligence.graph import AnchoredGraphRules, _provenance_observations
    from src.analysis.open_intelligence.source_provenance import (
        bind_source_provenance,
        provenance_member_id,
    )

    if persist:
        raise CompositionDependencyMissing("v3 persistence is prohibited at the runner boundary")
    if metric_provider is not None:
        raise CompositionDependencyMissing("v3 metric provider is not implemented")
    if isinstance(trend_date, datetime) or not isinstance(trend_date, date):
        raise ValueError("trend_date must be a date")
    markets = _validate_scope(scope)
    _validate_target(client, dataset)
    if scope.contract_version != "2.0.0":
        raise ValueError("source_provenance_version_invalid")
    from src.analysis.open_intelligence.production_snapshot import ASSEMBLY_CONTRACT_VERSION

    production = (
        isinstance(source_snapshot, Mapping)
        and source_snapshot.get("assembly_contract_version") == ASSEMBLY_CONTRACT_VERSION
    )
    if production:
        from src.analysis.open_intelligence.production_snapshot_client import (
            ProductionSnapshotClient,
        )

        bound_client = ProductionSnapshotClient(
            source_snapshot,
            cutoff_date=trend_date,
            client_scope_id=scope.client_scope_id,
            market_scope=markets,
            delegate=client,
        )
        snapshot = bound_client.snapshot
    else:
        snapshot = validate_source_snapshot_v3(source_snapshot, trend_date, markets=markets)
        bound_client = ProvenanceSnapshotClient(snapshot, delegate=client)
    if semantic_provider is None:
        semantic_provider = LexicalSimilarityProviderV2()
    if rule_bundle is not None and (
        not isinstance(rule_bundle, CompositionRuleBundleV3)
        or _digest(rule_bundle)
        != _digest(
            load_composition_rules_v3(
                ROOT / "configs/open_intelligence_composition_rules_v3.yaml",
                require_certified=False,
            )
        )
        or getattr(rule_bundle, "rule_version", None) != "composition_rules_v3"
        or not isinstance(getattr(rule_bundle, "graph_rules", None), AnchoredGraphRules)
        or getattr(rule_bundle, "status", None) != "uncertified"
    ):
        raise ValueError("source_provenance_version_invalid")
    event_rows = _load_source(
        bound_client,
        table="event_ledger",
        columns=EVENT_COLUMNS,
        date_column="trend_date",
        trend_date=trend_date,
        markets=markets,
    )
    graph_rows = _load_source(
        bound_client,
        table="seed_graph",
        columns=SEED_GRAPH_COLUMNS,
        date_column="trend_date",
        trend_date=trend_date,
        markets=markets,
    )
    candidate_rows = _load_source(
        bound_client,
        table="seed_candidates",
        columns=SEED_CANDIDATE_COLUMNS,
        date_column="proposed_date",
        trend_date=trend_date,
        markets=markets,
        statuses=tuple(sorted(ACCEPTED_SEED_CANDIDATE_STATUSES)),
    )
    event_sources = tuple((row, extract_event_observations((row,))[0]) for row in event_rows)
    graph_sources = tuple(
        (row, obs) for row in graph_rows for obs in extract_seed_graph_observations((row,))
    )
    candidate_sources = tuple(
        (row, replace(extract_seed_candidate_observations((row,))[0], candidate_score=None))
        for row in candidate_rows
    )
    inputs = CandidateInputs(
        tuple(obs for _, obs in event_sources),
        tuple(obs for _, obs in graph_sources),
        tuple(obs for _, obs in candidate_sources),
    )
    observations, identities = canonicalize_observations_with_mapping(inputs.observations)
    identity_map = dict(zip(inputs.observations, identities, strict=True))
    member_samples = {_observation_identity(obs): set() for obs in observations}
    for lane, sources in (("seed_graph", graph_sources), ("seed_candidates", candidate_sources)):
        for row, obs in sources:
            identity = identity_map[obs]
            if identity in member_samples:
                member_samples[identity].update(_sample_ids(row, lane))
    projection = _project_evidence(
        bound_client,
        scope=scope,
        trend_date=trend_date,
        markets=markets,
        candidate_inputs=inputs,
        observations=observations,
        canonical_identity_by_observation=identity_map,
        components=(),
        event_sources=event_sources,
        graph_sources=graph_sources,
        candidate_sources=candidate_sources,
        skip_unsupported_identity=True,
    )
    provenance = bind_source_provenance(
        observations=observations,
        member_samples={key: tuple(sorted(values)) for key, values in member_samples.items()},
        projection=projection,
        source_snapshot=snapshot,
    )
    if production:
        projection = replace(
            projection,
            source_window=ProductionSourceWindow(
                projection.source_window.start_date,
                projection.source_window.end_date,
                projection.source_window.markets,
                tuple(
                    bound_client.source_table(table)
                    for table in ("enriched_content", "raw_content")
                ),
                False,
            ),
        )
    observations, _ = _provenance_observations(observations, provenance)
    components = ()
    missing = set(_fail_closed_projection_work(projection, observations))
    if production:
        from src.analysis.open_intelligence.production_snapshot import _EXTERNAL_AUTHORITY_REQUIRED

        missing.update(snapshot["coverage_limitations"])
        missing.update(_EXTERNAL_AUTHORITY_REQUIRED)
    missing.add("composition_rules_uncertified")
    missing.add("metric_projection_unavailable")
    if rule_bundle is None:
        missing.add("composition_rule_bundle_unavailable")
    if semantic_provider is None:
        missing.add("semantic_provider_unavailable")
    if metric_provider is None:
        missing.add("metric_provider_unavailable")
    if rule_bundle is not None and semantic_provider is not None:
        components = build_components_with_provenance_semantics(
            observations,
            semantic_provider,
            rule_bundle.graph_rules,
            source_provenance_by_member=provenance,
        )
    by_identity = {_observation_identity(obs): obs for obs in observations}
    memberships = {}
    for component in components:
        members = []
        for identity in component.member_identities:
            obs = by_identity[identity]
            envelope = provenance[identity]
            row_id = _membership_row_id(projection.candidate_only_receipts[identity])
            members.append(
                MembershipReceipt(
                    member_id=provenance_member_id(
                        _member_id(scope, trend_date, obs, row_id), envelope
                    ),
                    member_identity=identity,
                    candidate_type=obs.candidate_type,
                    canonical_value=obs.term,
                    source_families=tuple(obs.source_families),
                    platforms=tuple(obs.platforms),
                    row_id=row_id,
                    qualifies_evidence=False,
                    vendor_families=tuple(sorted({p["vendor_family"] for p in envelope["pairs"]})),
                    channel_families=tuple(
                        sorted({p["channel_family"] for p in envelope["pairs"]})
                    ),
                )
            )
        memberships[_component_key(component)] = tuple(
            sorted(members, key=lambda m: m.member_identity)
        )
    projection = replace(projection, memberships_by_component=MappingProxyType(memberships))
    windows = {
        table: (ProductionSourceWindow if production else SourceWindow)(
            trend_date,
            trend_date,
            markets,
            (bound_client.source_table(table) if production else _source_table(table),),
            False,
        )
        for table in SOURCE_CEILINGS
    }
    counts = {
        "event_ledger": len(event_rows),
        "seed_graph": len(graph_rows),
        "seed_candidates": len(candidate_rows),
    }
    error_state = (
        "source_provenance_missing"
        if any(item["resolution_state"] != "resolved" for item in provenance.values())
        else None
    )
    result = _empty_result(
        scope=scope,
        windows=windows,
        input_counts=counts,
        candidate_inputs=inputs,
        observations=observations,
        rule_version=getattr(rule_bundle, "rule_version", None),
        missing_work=tuple(sorted(missing)),
        components=components,
        error_state=error_state,
        evidence_projection=projection,
    )
    return ProvenanceSignalRunResult(result, provenance, snapshot["source_digest"])


def run_dynamic_signal_identity(
    trend_date: date,
    scope: ResolvedScope,
    client: object,
    dataset: str,
    persist: bool,
    *,
    rule_bundle: object | None = None,
    semantic_provider: SemanticSimilarityProvider | None = None,
    metric_provider: object | None = None,
) -> DynamicSignalRunResult:
    if isinstance(trend_date, datetime) or not isinstance(trend_date, date):
        raise ValueError("trend_date must be a date")
    markets = _validate_scope(scope)
    _validate_target(client, dataset)
    if persist:
        raise CompositionDependencyMissing("persistence is unavailable in Task 5C.1")

    event_rows = _load_source(
        client,
        table="event_ledger",
        columns=EVENT_COLUMNS,
        date_column="trend_date",
        trend_date=trend_date,
        markets=markets,
    )
    graph_rows = _load_source(
        client,
        table="seed_graph",
        columns=SEED_GRAPH_COLUMNS,
        date_column="trend_date",
        trend_date=trend_date,
        markets=markets,
    )
    candidate_rows = _load_source(
        client,
        table="seed_candidates",
        columns=SEED_CANDIDATE_COLUMNS,
        date_column="proposed_date",
        trend_date=trend_date,
        markets=markets,
        statuses=tuple(sorted(ACCEPTED_SEED_CANDIDATE_STATUSES)),
    )
    candidate_rows = tuple(
        row for row in candidate_rows if row.get("status") in ACCEPTED_SEED_CANDIDATE_STATUSES
    )
    windows = {
        table: SourceWindow(
            trend_date,
            trend_date,
            markets,
            (_source_table(table),),
            False,
        )
        for table in SOURCE_CEILINGS
    }
    counts = {
        "event_ledger": len(event_rows),
        "seed_graph": len(graph_rows),
        "seed_candidates": len(candidate_rows),
    }
    try:
        event_sources = tuple((row, extract_event_observations((row,))[0]) for row in event_rows)
        graph_sources = tuple(
            (row, observation)
            for row in graph_rows
            for observation in extract_seed_graph_observations((row,))
        )
        candidate_sources = tuple(
            (
                row,
                replace(
                    extract_seed_candidate_observations((row,))[0],
                    candidate_score=None,
                ),
            )
            for row in candidate_rows
        )
        candidate_inputs = CandidateInputs(
            tuple(item[1] for item in event_sources),
            tuple(item[1] for item in graph_sources),
            tuple(item[1] for item in candidate_sources),
        )
        identity_observations = candidate_inputs.observations
        observations, canonical_identities = canonicalize_observations_with_mapping(
            identity_observations
        )
        canonical_identity_by_observation = dict(
            zip(identity_observations, canonical_identities, strict=True)
        )
    except ValueError as error:
        message = str(error)
        detail = "source_family" if "source family" in message else "canonical_observation"
        return _empty_result(
            scope=scope,
            windows=windows,
            input_counts=counts,
            candidate_inputs=CandidateInputs(),
            observations=(),
            rule_version=getattr(rule_bundle, "rule_version", None),
            missing_work=("candidate_input_invalid",),
            error_state=f"candidate_input_invalid:{detail}",
        )
    missing = []
    if rule_bundle is None:
        missing.append("composition_rule_bundle_unavailable")
    if semantic_provider is None:
        missing.append("semantic_provider_unavailable")
    if missing:
        evidence_projection = _project_evidence(
            client,
            scope=scope,
            trend_date=trend_date,
            markets=markets,
            candidate_inputs=candidate_inputs,
            observations=observations,
            canonical_identity_by_observation=canonical_identity_by_observation,
            components=(),
            event_sources=event_sources,
            graph_sources=graph_sources,
            candidate_sources=candidate_sources,
        )
        missing.extend(_fail_closed_projection_work(evidence_projection, observations))
        error_state = None
        if evidence_projection.unresolved_sample_ids:
            error_state = (
                "evidence_projection_incomplete:ambiguous_sample_id"
                if "ambiguous_sample_id" in evidence_projection.missing_work
                else "evidence_projection_incomplete:missing_sample_id"
            )
        return _empty_result(
            scope=scope,
            windows=windows,
            input_counts=counts,
            candidate_inputs=candidate_inputs,
            observations=observations,
            rule_version=getattr(rule_bundle, "rule_version", None),
            missing_work=tuple(sorted(missing)),
            error_state=error_state,
            evidence_projection=evidence_projection,
        )

    graph_rules = getattr(rule_bundle, "graph_rules", None)
    if not isinstance(graph_rules, GraphRules):
        raise CompositionDependencyMissing("composition graph rules are unavailable")
    components = compose_components(observations, semantic_provider, graph_rules)
    evidence_projection = _project_evidence(
        client,
        scope=scope,
        trend_date=trend_date,
        markets=markets,
        candidate_inputs=candidate_inputs,
        observations=observations,
        canonical_identity_by_observation=canonical_identity_by_observation,
        components=components,
        event_sources=event_sources,
        graph_sources=graph_sources,
        candidate_sources=candidate_sources,
    )
    remaining = list(_fail_closed_projection_work(evidence_projection, observations))
    error_state = None
    if evidence_projection.unresolved_sample_ids:
        error_state = (
            "evidence_projection_incomplete:ambiguous_sample_id"
            if "ambiguous_sample_id" in evidence_projection.missing_work
            else "evidence_projection_incomplete:missing_sample_id"
        )
    if metric_provider is None:
        remaining.append("metric_provider_unavailable")
    return _empty_result(
        scope=scope,
        windows=windows,
        input_counts=counts,
        candidate_inputs=candidate_inputs,
        observations=observations,
        rule_version=getattr(rule_bundle, "rule_version", None),
        missing_work=tuple(sorted(remaining)),
        components=components,
        error_state=error_state,
        evidence_projection=evidence_projection,
    )
