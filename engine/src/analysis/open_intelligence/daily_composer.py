"""The native composer and persist clients the daily compose stage dispatches over.

``build_native_composer`` answers ``clients["composer"](entry, cutoff=cutoff)``: it takes
the admitted capture entry, resolves it to the source snapshot the capture wrote through
the injected resolver, runs the zero write identity pipeline over that snapshot with the
warehouse as the cross checking delegate, measures every provenance component's factor
evidence through the replay's own collector and derivation, builds one evidence receipt
per component row with its geographic scope resolved by the scope kernel, evaluates
readiness under the daily rules and the explicit origin policy, and answers exactly the
mapping ``build_producing_batch`` takes beyond the fields the stage reserves. A component
is measured or skipped with named missing work; nothing here invents a metric, a
direction or a geography.

The geographic binding: the receipt's float carries the kernel's confidence only for a
local scope. A contextual or foreign scope is a measured decision that the behaviour is
not evidenced in the market, so its float is zero. An unknown scope has no evidence at
all, and the float field cannot say so, so the component is withheld from readiness with
the named reason ``geo_scope_unknown`` rather than written as a measured zero. The full
record of every resolved row (method, evidence reference, scope, confidence) rides beside
the receipts in ``ReceiptsWithGeography`` and in the composition facts, for the versioned
evidence relation of a later slice; no current relation carries it.

``build_native_persist`` answers ``clients["persist"](bridge, run_id=, signal_date=,
created_at=)``: it writes the producing batch and receipt through the daily-only atomic
adapter under a rule bundle the persistence validator admits on its daily profile path.
The adapter mutates the seeded mutex before reading target state, derives the row set
digest through the release SQL, and refuses a differing retry before target mutation.

Both clients are built by ``daily_native_clients.compose_clients`` and only where a
caller hands it a compose binding; no runtime carries one, so in a deployed run the
compose stage still refuses before either client is reached.

Nothing here opens a connection or constructs a cloud client. The replay and release
scripts are imported inside the calls that use their readers and contracts.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

from google.cloud import bigquery

from src.analysis.open_intelligence import live_quality, persistence
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.candidates import (
    QUALIFYING_SOURCE_FAMILIES,
    SOURCE_FAMILY_MAP_VERSION,
    _family,
)
from src.analysis.open_intelligence.capture_registry import validate_capture_entry
from src.analysis.open_intelligence.daily_stages import (
    StageRefusal,
    _closed_day,
)
from src.analysis.open_intelligence.geographic_scope import (
    UNKNOWN,
    GeographicScope,
    resolve_geographic_scope,
)
from src.analysis.open_intelligence.pipeline import (
    EVIDENCE_COLUMNS_BY_TABLE,
    TARGET_DATASET,
    TARGET_LOCATION,
    TARGET_PROJECT,
    run_dynamic_signal_identity_v3,
)
from src.analysis.open_intelligence.readiness import (
    EXPLICIT_ORIGIN_POLICY,
    ReadinessRules,
    evaluate_readiness,
)
from src.analysis.open_intelligence.rows import (
    EvidenceReceipt,
    ObservedSignalMetrics,
    _readiness_records,
)
from src.analysis.open_intelligence.source_inventory import CONNECTOR_PROFILES
from src.contracts.open_intelligence import ResolvedScope

COMPOSITION_RULES_PATH = "configs/open_intelligence_composition_rules_v3.yaml"
COMPOSER_CONTRACT_VERSION = "2.0.0"
DAILY_OBSERVATION_METHOD = "daily_source_snapshot_compose_v1"
DAILY_RULE_STATUS = persistence.DAILY_PROFILE_RULE_STATUS
# The factor evidence window the replay chain measures over, ending on the closed day.
FACTOR_WINDOW_DAYS = 14
GEO_UNKNOWN_REASON = "geo_scope_unknown"
# The geography methods that record the market a search was run for rather than a measured
# geography: the socialcrawl search stamps its own market under this method. Such a row is
# contextual evidence for its market and never a retained geography receipt.
SEARCH_FRAME_GEO_METHODS = frozenset({"wave1_seed_market_v1"})
# The geography methods of the reviewed source inventory (``CONNECTOR_PROFILES`` in
# source_inventory) under which the vendor itself aggregates what people located in the
# requested country did: a storefront chart, a country partition of search interest, the
# geo parameter of trending searches, top pageviews per country and the location parameter
# of a domain ranking. The market such a vendor was asked for is the population it
# measured, not a frame over global content, so it reaches the kernel as a vendor region
# cited by the row the vendor answered. Every other method selects content by a frame
# (market feed lists, query terms, subreddit lists, creator watchlists, source country and
# location filters, configured searches, a keyword database row written for every
# configured keyword whatever its volume, an editorial playlist an operator chose), and a
# frame is never a vendor region: a global story found through a local frame stays
# contextual or unknown, and so does a creator whose registration country is all it has.
VENDOR_MEASURED_GEOGRAPHY_METHODS = frozenset(
    {
        "country_partition",
        "geo_parameter",
        "location_parameter",
        "storefront_country",
        "top_per_country",
    }
)
VENDOR_MEASURED_SOURCES = frozenset(
    name
    for name, profile in CONNECTOR_PROFILES.items()
    if profile.geography_method in VENDOR_MEASURED_GEOGRAPHY_METHODS
)
# The family direction authority is the one the replay chain binds (approved 2026-09-03):
# ``live_quality.family_directions`` over the full term match counted by family and
# published date, which ``collect_component_factor_evidence`` already measures for every
# component as ``family_direction_counts``. A family is directional only where the
# collection exposure receipts prove it was collected completely on every day of the
# factor window, so a collection gap is never read as a decline.
EXPOSURE_TABLE = "collection_exposure_receipts_v1"
# Readiness is evaluated as the certifier evaluates it once the run's quality review
# receipt is registered; a run certified without one re-evaluates as unchecked and differs.
QUALITY_EVALUATED = True
_RECEIPT_TABLES = ("enriched_content", "raw_content")


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(code)
    return value


def _aware(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(code)
    return value


# Composer


class ReceiptsWithGeography(Mapping):
    """One component's receipts keyed by row id, with each row's geographic record beside it."""

    __slots__ = ("_receipts", "geography")

    def __init__(
        self, receipts: Mapping[str, EvidenceReceipt], geography: Mapping[str, GeographicScope]
    ) -> None:
        if set(receipts) != set(geography):
            raise ValueError("compose_geography_incomplete")
        self._receipts = MappingProxyType(dict(sorted(receipts.items())))
        self.geography = MappingProxyType(dict(sorted(geography.items())))

    def __getitem__(self, key: str) -> EvidenceReceipt:
        return self._receipts[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._receipts)

    def __len__(self) -> int:
        return len(self._receipts)

    def __repr__(self) -> str:
        return f"ReceiptsWithGeography({dict(self._receipts)!r})"


@dataclass(frozen=True)
class CompositionFacts:
    """What one composition measured about its own inputs, for the run receipt and the sidecar."""

    run_id: str
    result_id: str
    composed_at: datetime
    source_window_digest: str
    observation_start: date
    observation_end: date
    cluster_build_version: str
    rule_version: str
    complete_partitions: bool
    geography_by_component: Mapping[str, Mapping[str, GeographicScope]]
    # The family direction decision of every measured component, reasons included, so a
    # family left not applicable for want of exposure authority says so.
    direction_by_component: Mapping[str, Mapping[str, live_quality.FamilyDirection]] = (
        MappingProxyType({})
    )
    # The canonical digest of the collection exposure receipt rows this composition read at
    # or before its cutoff. It is an in memory record of the read set for this composition
    # only: the run receipt has no field for it, so it reaches no durable output, and it is
    # computed over the rows as read, so it is not comparable to R3's receipt readback.
    exposure_readback_digest: str | None = None


def geographic_sidecar(inputs: Mapping[str, object]) -> dict[str, dict[str, GeographicScope]]:
    """The geographic records riding beside a composer mapping's receipts, by component and row."""
    sidecar = {}
    for key, receipts in inputs["receipts_by_component"].items():  # type: ignore[attr-defined]
        if not isinstance(receipts, ReceiptsWithGeography):
            raise ValueError("compose_geography_missing")
        sidecar[key] = dict(receipts.geography)
    return sidecar


def _geo_confidence(scope: GeographicScope) -> float:
    """The float the current evidence row can carry for a resolved scope.

    Local carries the kernel's confidence. Contextual and foreign are measured decisions
    that the behaviour is not evidenced in the market, so the in market confidence is
    zero; the sidecar keeps the method and its tier. Unknown never reaches here.
    """
    if scope.scope == "local":
        return float(scope.confidence)  # type: ignore[arg-type]
    return 0.0


def _snapshot_rows(snapshot: Mapping[str, object]) -> dict[tuple[str, str], Mapping[str, object]]:
    """The snapshot's own evidence rows by (market, id), the record the geo kernel reads."""
    rows_by_table = snapshot.get("rows_by_table")
    if not isinstance(rows_by_table, Mapping):
        raise StageRefusal("compose_source_window_invalid")
    index: dict[tuple[str, str], Mapping[str, object]] = {}
    for table in _RECEIPT_TABLES:
        for row in rows_by_table.get(table, ()):
            if not isinstance(row, Mapping) or set(row) != set(EVIDENCE_COLUMNS_BY_TABLE[table]):
                raise StageRefusal("compose_source_window_invalid")
            index.setdefault((str(row["market"]), str(row["id"])), row)
    return index


def _resolve_geography(market: str, row_id: str, row: Mapping[str, object] | None):
    if row is None:
        return UNKNOWN
    common = {
        "market": market,
        "row_id": row_id,
        "title": row.get("title"),
        "text": row.get("text"),
        "url": row.get("url"),
    }
    if row.get("geo_method_id") in SEARCH_FRAME_GEO_METHODS:
        # The search's own market is what the query asked for, not where the behaviour
        # happened, so it reaches the kernel as a contextual frame and never as a receipt.
        common["search_frame_market"] = row.get("market")
        common["search_frame_receipt_id"] = row.get("geo_receipt_id")
    else:
        common["retained_geo_market"] = row.get("market")
        common["geo_method_id"] = row.get("geo_method_id")
        common["geo_receipt_id"] = row.get("geo_receipt_id")
    if row.get("source") in VENDOR_MEASURED_SOURCES:
        # The vendor answered this row for the population of its requested country, so
        # that country is a vendor region and the row the vendor answered is its receipt.
        # It decides below the blocklist, a retained receipt and the script marker, so a
        # collision or foreign content under a country chart is still not local.
        common["vendor_market"] = row.get("market")
        common["vendor_receipt_id"] = row_id
    topics = row.get("topic_groups") or ()
    for topic in topics if isinstance(topics, (list, tuple)) else ():
        scope = resolve_geographic_scope(topic_group=topic, **common)
        if scope.method == "blocklist_match":
            return scope
    return resolve_geographic_scope(**common)


def _published_at(observation, projected) -> datetime | None:
    if projected is not None:
        # A row the source never dated keeps an unknown publish time. When the engine
        # collected it says nothing about when it was published, so readiness reads the
        # row as undated and never as fresh.
        return projected.published_at
    timestamps = tuple(observation.observed_timestamps or ())
    if not timestamps:
        return None
    try:
        return datetime.fromisoformat(str(max(timestamps)).replace("Z", "+00:00"))
    except ValueError:
        return None


def _receipt_family(observation, projected) -> str | None:
    """The one qualifying family the row truly carries, never one it does not have."""
    if projected is not None:
        family = projected.channel_family
        return family if family in QUALIFYING_SOURCE_FAMILIES else None
    families = tuple(observation.source_families or ())
    family = next((item for item in families if item in QUALIFYING_SOURCE_FAMILIES), None)
    if family is not None:
        return family
    for platform in tuple(observation.platforms or ()):
        with contextlib.suppress(ValueError, TypeError):
            mapped = _family(platform)
            if mapped in QUALIFYING_SOURCE_FAMILIES:
                return mapped
    return None


def _exposure_complete(
    family: str, receipts: tuple[Mapping[str, object], ...], required_days: tuple[date, ...]
) -> bool:
    """The replay chain's exposure rule for one family, over the daily factor window.

    Every day of the window carries exactly one receipt for the family, the receipts share
    one collection policy and quota authority, every capture completed and no quota was
    exhausted. The rule is the one ``evaluate_r3_quality_authority`` applies to the R3
    window; only the window moves, to the one the daily factor evidence was measured over.
    """
    family_exposures = tuple(item for item in receipts if item["source_family"] == family)
    days = {item["exposure_date"] for item in family_exposures}
    invariant_exposure = {
        (
            item.get("collection_policy_digest"),
            item.get("quota_authority_id"),
            item.get("quota_applicability"),
            item.get("quota_unit"),
            item.get("quota_limit"),
        )
        for item in family_exposures
    }
    return (
        all(day in days for day in required_days)
        and len(family_exposures) == len(required_days)
        and len(invariant_exposure) == 1
        and all(
            item.get("capture_complete") is True and item.get("quota_exhausted") is False
            for item in family_exposures
        )
    )


def _component_directions(
    component, evidence, exposures: tuple[Mapping[str, object], ...], trend_date: date
) -> dict[str, live_quality.FamilyDirection]:
    """The approved family direction of every family the component carries."""
    required_days = tuple(
        trend_date - timedelta(days=FACTOR_WINDOW_DAYS - 1 - offset)
        for offset in range(FACTOR_WINDOW_DAYS)
    )
    counted = {
        family: (current, baseline)
        for family, current, baseline in getattr(evidence, "family_direction_counts", ())
    }
    families = tuple(sorted(component.source_families))
    return live_quality.family_directions(
        {family: counted.get(family, (0, 0)) for family in families},
        exposure_complete={
            family: _exposure_complete(family, exposures, required_days) for family in families
        },
    )


def _receipt(
    row_id: str,
    member: str,
    observation,
    projected,
    geography: GeographicScope,
    directions: Mapping[str, live_quality.FamilyDirection] = MappingProxyType({}),
) -> EvidenceReceipt | None:
    family = _receipt_family(observation, projected)
    if family is None:
        return None
    decided = directions.get(family)
    direction = decided.direction if decided is not None else None
    platforms = tuple(observation.platforms or ())
    platform = (
        projected.platform if projected is not None else (platforms[0] if platforms else family)
    )
    try:
        return EvidenceReceipt(
            member_identity=member,
            row_id=row_id,
            source_family=family,
            platform=platform,
            url=projected.url if projected is not None else None,
            published_at=_published_at(observation, projected),
            claim_role="context",
            # The approved family direction test decides; a family it could not decide,
            # for want of counts or of exposure authority, stays not applicable rather
            # than becoming a guess.
            direction=direction or "not_applicable",
            geo_confidence=_geo_confidence(geography),
            source_label=projected.source if projected is not None else None,
            author_label=(
                (projected.author_handle_norm or projected.author_name)
                if projected is not None
                else None
            ),
            excerpt=(
                (projected.title or projected.text or None) if projected is not None else None
            ),
            metric_label=None,
            availability="available",
            vendor_family=projected.vendor_family if projected is not None else None,
            channel_family=projected.channel_family if projected is not None else None,
        )
    except ValueError:
        return None


class NativeComposer:
    """The compose stage's composer over the zero write pipeline; ``compositions`` is its sidecar."""

    def __init__(
        self,
        *,
        warehouse,
        scope: ResolvedScope,
        source_window_from_entry: Callable[[Mapping[str, object]], Mapping[str, object]],
        readiness_rules: ReadinessRules,
        semantic_provider,
        now: Callable[[], datetime],
    ) -> None:
        if not isinstance(scope, ResolvedScope):
            raise ValueError("compose_scope_invalid")
        if scope.contract_version != COMPOSER_CONTRACT_VERSION:
            raise ValueError("compose_scope_invalid")
        if not isinstance(readiness_rules, ReadinessRules):
            raise ValueError("compose_readiness_rules_invalid")
        if not callable(source_window_from_entry) or not callable(now):
            raise ValueError("compose_binding_invalid")
        if semantic_provider is None:
            raise ValueError("compose_semantic_provider_invalid")
        self.warehouse = warehouse
        self.scope = scope
        self.readiness_rules = readiness_rules
        self._resolve = source_window_from_entry
        self._provider = semantic_provider
        self._now = now
        self.compositions: dict[str, CompositionFacts] = {}

    def _query_runner(self, sql: str) -> list[dict]:
        job = self.warehouse.query(
            sql,
            job_config=bigquery.QueryJobConfig(use_legacy_sql=False),
            location=TARGET_LOCATION,
        )
        return [dict(row) for row in job.result()]

    def _admit(self, entry: object, cutoff: datetime) -> tuple[dict, date]:
        from src.analysis.open_intelligence.daily_native_clients import daily_readiness_rules

        admitted = validate_capture_entry(entry)
        if admitted["completion_state"] != "succeeded":
            raise StageRefusal("compose_capture_not_admitted")
        if admitted["observation_window_end"] != _aware(cutoff, "compose_cutoff_invalid"):
            raise StageRefusal("compose_window_differs")
        if admitted["scope"] != self.scope.client_scope_id or tuple(
            sorted(admitted["markets"])
        ) != tuple(sorted(self.scope.market_scope)):
            raise StageRefusal("compose_scope_differs")
        if self.readiness_rules != daily_readiness_rules(cutoff):
            raise StageRefusal("compose_readiness_rules_differ")
        return admitted, _closed_day(cutoff)

    def _exposure_receipts(
        self, trend_date: date, cutoff: datetime
    ) -> tuple[tuple[Mapping[str, object], ...], str]:
        """The exposure receipts of the factor window issued by the cutoff, and their readback.

        The read is bound to the composition cutoff, so a receipt issued after it is not
        authority for this composition. The canonical digest of every row read at or before
        the cutoff, malformed rows included, is returned beside the receipts and kept in
        memory on the composition facts; it is not persisted.

        A malformed receipt or two receipts for one day are never skipped: that family has
        no exposure authority, so all of its receipts are dropped,
        so it cannot prove a complete window and stays not applicable. A row that does not
        even name its family withholds authority from every family. A refused read is not
        an empty table and is raised.
        """
        from scripts.staging.issue_collection_exposure_receipts import (
            EXPOSURE_RECEIPT_FIELDS,
            validate_exposure_receipt,
        )

        bound = _aware(cutoff, "compose_cutoff_invalid").astimezone(UTC)
        start = trend_date - timedelta(days=FACTOR_WINDOW_DAYS - 1)
        rows = self._query_runner(
            f"SELECT {', '.join(EXPOSURE_RECEIPT_FIELDS)} FROM "
            f"`{TARGET_PROJECT}.{TARGET_DATASET}.{EXPOSURE_TABLE}` "
            f"WHERE exposure_date BETWEEN DATE '{start.isoformat()}' "
            f"AND DATE '{trend_date.isoformat()}' "
            f"AND issued_at <= TIMESTAMP '{bound.isoformat(sep=' ')}' "
            "ORDER BY source_family, exposure_date"
        )
        read = []
        for row in rows:
            issued_at = row.get("issued_at") if isinstance(row, Mapping) else None
            if (
                isinstance(issued_at, datetime)
                and issued_at.tzinfo is not None
                and issued_at > bound
            ):
                # The read is bound in SQL; a row past the cutoff is never authority.
                continue
            read.append(
                {field: row.get(field) for field in EXPOSURE_RECEIPT_FIELDS}
                if isinstance(row, Mapping)
                else {}
            )
        try:
            readback = sorted(canonical_bytes(item) for item in read)
        except (TypeError, ValueError) as error:
            raise StageRefusal("compose_exposure_readback_invalid") from error
        readback_digest = canonical_digest(tuple(item.decode("utf-8") for item in readback))
        receipts: list[Mapping[str, object]] = []
        refused: set[str] = set()
        keys: set[tuple[str, date]] = set()
        for row in read:
            family = row.get("source_family")
            if not isinstance(family, str) or not family:
                return (), readback_digest
            try:
                receipt = validate_exposure_receipt(
                    {field: row[field] for field in EXPOSURE_RECEIPT_FIELDS}
                )
            except (KeyError, TypeError, ValueError):
                refused.add(family)
                continue
            key = (receipt["source_family"], receipt["exposure_date"])
            if key in keys:
                refused.add(family)
                continue
            keys.add(key)
            receipts.append(receipt)
        return (
            tuple(item for item in receipts if item["source_family"] not in refused),
            readback_digest,
        )

    def _components(self, result, snapshot, trend_date: date, cutoff: datetime):
        from scripts.staging import scoring_qualification
        from scripts.staging.replay_open_intelligence import collect_component_factor_evidence

        run = result.run
        evidence_by_component = collect_component_factor_evidence(
            query_runner=self._query_runner,
            components=run.components,
            project=TARGET_PROJECT,
            dataset=TARGET_DATASET,
            trend_date=trend_date,
            window_start=trend_date - timedelta(days=FACTOR_WINDOW_DAYS - 1),
        )
        rows_by_key = _snapshot_rows(snapshot)
        exposures, exposure_readback_digest = self._exposure_receipts(trend_date, cutoff)
        receipts_by_member = run.evidence_projection.receipts_by_member
        observation_by_member = {
            f"{item.market}|{item.candidate_type}|{item.term}": item for item in run.observations
        }
        metrics_by_component: dict[str, ObservedSignalMetrics] = {}
        receipts_by_component: dict[str, ReceiptsWithGeography] = {}
        readiness_by_component: dict[str, object] = {}
        missing_by_component: dict[str, tuple[str, ...]] = {}
        geography_by_component: dict[str, Mapping[str, GeographicScope]] = {}
        direction_by_component: dict[str, Mapping[str, live_quality.FamilyDirection]] = {}
        for component in run.components:
            key = persistence.component_bridge_key(component)
            evidence = evidence_by_component.get(key)
            if evidence is None:
                missing_by_component[key] = ("factor_evidence_unavailable",)
                continue
            raw_metrics, reasons = scoring_qualification.derive_component_factor_metrics(evidence)
            if raw_metrics is None:
                missing_by_component[key] = reasons
                continue
            directions = _component_directions(component, evidence, exposures, trend_date)
            direction_by_component[key] = MappingProxyType(directions)
            receipts: dict[str, EvidenceReceipt] = {}
            geography: dict[str, GeographicScope] = {}
            expected_rows = set(component.row_receipts)
            for member in component.member_identities:
                observation = observation_by_member.get(member)
                if observation is None:
                    continue
                projected_rows = {row.row_id: row for row in receipts_by_member.get(member, ())}
                for row_id in observation.row_ids:
                    if row_id not in expected_rows:
                        continue
                    scope = _resolve_geography(
                        component.market, row_id, rows_by_key.get((component.market, row_id))
                    )
                    receipt = _receipt(
                        row_id, member, observation, projected_rows.get(row_id), scope, directions
                    )
                    if receipt is not None:
                        receipts[row_id] = receipt
                        geography[row_id] = scope
            if (
                set(receipts) != expected_rows
                or {item.member_identity for item in receipts.values()}
                != set(component.member_identities)
                or any(
                    item.source_family not in component.source_families
                    for item in receipts.values()
                )
            ):
                missing_by_component[key] = ("evidence_receipts_unavailable",)
                continue
            if any(item.scope == "unknown" for item in geography.values()):
                geography_by_component[key] = MappingProxyType(dict(sorted(geography.items())))
                missing_by_component[key] = (GEO_UNKNOWN_REASON,)
                continue
            bound = ReceiptsWithGeography(receipts, geography)
            readiness = evaluate_readiness(
                _readiness_records(tuple(bound.values()), EXPLICIT_ORIGIN_POLICY),
                self.readiness_rules,
                quality_evaluated=QUALITY_EVALUATED,
                independence_policy=EXPLICIT_ORIGIN_POLICY,
            )
            metrics_by_component[key] = ObservedSignalMetrics(**dict(raw_metrics))
            receipts_by_component[key] = bound
            readiness_by_component[key] = readiness
            geography_by_component[key] = bound.geography
        return (
            metrics_by_component,
            receipts_by_component,
            readiness_by_component,
            missing_by_component,
            geography_by_component,
            direction_by_component,
            exposure_readback_digest,
        )

    def __call__(self, entry: object, *, cutoff: datetime) -> dict[str, object]:
        from scripts.staging.replay_open_intelligence import ROOT, load_composition_rules_v3

        admitted, trend_date = self._admit(entry, cutoff)
        snapshot = self._resolve(admitted)
        if not isinstance(snapshot, Mapping):
            raise StageRefusal("compose_source_window_invalid")
        rule_bundle = load_composition_rules_v3(
            ROOT / COMPOSITION_RULES_PATH, require_certified=False
        )
        result = run_dynamic_signal_identity_v3(
            trend_date,
            self.scope,
            self.warehouse,
            TARGET_DATASET,
            False,
            source_snapshot=snapshot,
            rule_bundle=rule_bundle,
            semantic_provider=self._provider,
        )
        if result.run.error_state is not None:
            raise StageRefusal(f"compose_result_error:{result.run.error_state}")
        if result.run.persistence_result is not None:
            raise StageRefusal("compose_pipeline_wrote")
        metrics, receipts, readiness, missing, geography, direction, exposure_readback = (
            self._components(result, snapshot, trend_date, cutoff)
        )
        window = result.run.evidence_projection.source_window
        self.compositions[self.scope.run_id] = CompositionFacts(
            run_id=self.scope.run_id,
            result_id=admitted["result_id"],
            composed_at=_aware(self._now(), "clock_invalid"),
            source_window_digest=result.source_snapshot_digest,
            observation_start=window.start_date,
            observation_end=window.end_date,
            cluster_build_version="hybrid_graph_v3",
            rule_version=result.run.rule_version,
            complete_partitions=admitted["completion_state"] == "succeeded",
            geography_by_component=MappingProxyType(geography),
            direction_by_component=MappingProxyType(direction),
            exposure_readback_digest=exposure_readback,
        )
        return {
            "result": result,
            "scope": self.scope,
            "metrics_by_component": MappingProxyType(metrics),
            "receipts_by_component": MappingProxyType(receipts),
            "readiness_by_component": MappingProxyType(readiness),
            "readiness_rules": self.readiness_rules,
            "missing_by_component": MappingProxyType(missing),
            "quality_evaluated": QUALITY_EVALUATED,
        }


def build_native_composer(
    *, warehouse, scope, source_window_from_entry, readiness_rules, semantic_provider, now
) -> NativeComposer:
    """The compose stage's composer client; see ``NativeComposer``."""
    return NativeComposer(
        warehouse=warehouse,
        scope=scope,
        source_window_from_entry=source_window_from_entry,
        readiness_rules=readiness_rules,
        semantic_provider=semantic_provider,
        now=now,
    )


# Persist


@dataclass(frozen=True)
class DailyRuleBundle:
    """The daily profile's rules, certified by the profile the release stage admits."""

    status: str
    rule_version: str
    profile_name: str
    profile_digest: str
    source_policy_digest: str
    approved_by: str
    approved_at: datetime


def daily_rule_bundle(
    release_profile, *, approved_by: str, approved_at: datetime, rule_version: str
) -> DailyRuleBundle:
    """The rule bundle of one daily release profile, validated as persistence admits it."""
    bundle = DailyRuleBundle(
        status=DAILY_RULE_STATUS,
        rule_version=rule_version,
        profile_name=getattr(release_profile, "profile_name", None),  # type: ignore[arg-type]
        profile_digest=getattr(release_profile, "digest", None),  # type: ignore[arg-type]
        source_policy_digest=getattr(release_profile, "source_policy_digest", None),  # type: ignore[arg-type]
        approved_by=approved_by,
        approved_at=approved_at,
    )
    persistence.validate_rule_bundle(bundle)
    return bundle


def composition_receipt_inputs(
    composer: NativeComposer, *, source_sha: str, observation_method: str = DAILY_OBSERVATION_METHOD
) -> Callable[[str], dict[str, object]]:
    """Run receipt inputs read from the composer's own facts of the run being persisted."""
    _text(source_sha, "persist_source_sha_invalid")
    _text(observation_method, "persist_observation_method_invalid")

    def inputs(run_id: str) -> dict[str, object]:
        facts = composer.compositions.get(run_id)
        if facts is None:
            raise StageRefusal("persist_composition_unavailable")
        return {
            "client_scope_id": composer.scope.client_scope_id,
            "market_scope": tuple(composer.scope.market_scope),
            "observation_start": facts.observation_start,
            "observation_method": observation_method,
            "source_window_digest": facts.source_window_digest,
            "cluster_build_version": facts.cluster_build_version,
            "rule_version": facts.rule_version,
            "source_family_map_version": SOURCE_FAMILY_MAP_VERSION,
            "source_sha": source_sha,
            "complete_partitions": facts.complete_partitions,
        }

    return inputs


_RECEIPT_INPUT_FIELDS = frozenset(
    {
        "client_scope_id",
        "market_scope",
        "observation_start",
        "observation_method",
        "source_window_digest",
        "cluster_build_version",
        "rule_version",
        "source_family_map_version",
        "source_sha",
        "complete_partitions",
    }
)


def build_native_persist(
    *, client, dataset, rule_bundle, run_receipt_inputs, authority_receipt_from_run=None
):
    """The compose stage's persist client; see the module docstring for its rules."""
    persistence.validate_rule_bundle(rule_bundle)
    if dataset != TARGET_DATASET:
        raise persistence.TargetInvalid("only the exact approved staging target is allowed")
    if not callable(run_receipt_inputs):
        raise ValueError("persist_receipt_inputs_invalid")

    def _facts(run_id: str) -> dict[str, object]:
        facts = dict(run_receipt_inputs(run_id))
        if set(facts) != _RECEIPT_INPUT_FIELDS:
            raise StageRefusal("persist_receipt_inputs_invalid")
        return facts

    def persist(bridge, *, run_id: str, signal_date: date, created_at: datetime):
        writer_options = {}
        if authority_receipt_from_run is not None:
            from .daily_products import admit_execution

            authority_receipt = authority_receipt_from_run(run_id)
            admitted = admit_execution(authority_receipt)
            if admitted.manifest.project != TARGET_PROJECT:
                raise persistence.TargetInvalid("product authority project differs")
            writer_options["authority_receipt"] = authority_receipt
        batch = getattr(bridge, "batch", None)
        if not isinstance(batch, persistence.OpenIntelligenceRowBatch):
            raise StageRefusal("persist_batch_invalid")
        facts = _facts(_text(run_id, "persist_run_invalid"))
        if facts["cluster_build_version"] != batch.cluster_build_version:
            raise StageRefusal("persist_batch_differs")
        if facts["rule_version"] != rule_bundle.rule_version:
            raise StageRefusal("persist_rule_version_differs")
        try:
            return persistence.persist_daily_composition(
                project=TARGET_PROJECT,
                dataset=dataset,
                client=client,
                batch=batch,
                rule_bundle=rule_bundle,
                receipt_fields={
                    **facts,
                    "run_id": run_id,
                    "signal_date": signal_date,
                    "completed_at": _aware(created_at, "persist_created_at_invalid"),
                },
                **writer_options,
            )
        except persistence.BatchInvalid as error:
            raise StageRefusal(str(error)) from error

    return persist


__all__ = [
    "COMPOSER_CONTRACT_VERSION",
    "DAILY_OBSERVATION_METHOD",
    "DAILY_RULE_STATUS",
    "EXPOSURE_TABLE",
    "FACTOR_WINDOW_DAYS",
    "GEO_UNKNOWN_REASON",
    "QUALITY_EVALUATED",
    "VENDOR_MEASURED_GEOGRAPHY_METHODS",
    "VENDOR_MEASURED_SOURCES",
    "CompositionFacts",
    "DailyRuleBundle",
    "NativeComposer",
    "ReceiptsWithGeography",
    "build_native_composer",
    "build_native_persist",
    "composition_receipt_inputs",
    "daily_rule_bundle",
    "geographic_sidecar",
]
