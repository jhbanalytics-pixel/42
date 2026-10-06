"""Deterministic candidate and evidence rows for dynamic discovery."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime
from urllib.parse import urlsplit, urlunsplit

from src.analysis.open_intelligence.candidates import (
    QUALIFYING_SOURCE_FAMILIES,
    validate_source_identity,
)
from src.analysis.open_intelligence.graph import SignalComponent
from src.analysis.open_intelligence.readiness import (
    DEFAULT_INDEPENDENCE_POLICY,
    EXPLICIT_ORIGIN_POLICY,
    INDEPENDENCE_POLICIES,
    EvidenceRecord,
    ReadinessResult,
    ReadinessRules,
    evaluate_readiness,
)
from src.contracts.open_intelligence import ResolvedScope, encode_identifier_part

CANDIDATE_ROW_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "signal_id",
    "signal_date",
    "market",
    "label",
    "cluster_signature",
    "cluster_build_version",
    "model_version",
    "discovery_mode",
    "topic_tags",
    "novelty_score",
    "velocity_score",
    "breadth_score",
    "independence_score",
    "historical_similarity",
    "geo_confidence",
    "evidence_state",
    "created_at",
    "label_member_identity",
)
EVIDENCE_ROW_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "signal_date",
    "market",
    "signal_id",
    "evidence_id",
    "row_id",
    "source_family",
    "platform",
    "url",
    "published_at",
    "claim_role",
    "direction",
    "geo_confidence",
    "source_label",
    "author_label",
    "excerpt",
    "metric_label",
    "availability",
    "evidence_state",
    "created_at",
    "vendor_family",
    "channel_family",
)
_DIRECTIONS = frozenset({"rising", "stable", "declining", "conflicting", "not_applicable"})
_CLAIM_ROLES = frozenset({"identity", "direction", "context", "contradiction", "geo"})
_AVAILABILITY = frozenset({"available", "aged_out", "unavailable"})
_DISCOVERY_MODES = frozenset({"dynamic", "replay", "canary"})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _normalized_url(value: object) -> str | None:
    url = _optional_text(value, "url")
    if url is None:
        return None
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("url must be an absolute http or https URL")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("url port is invalid") from error
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parsed.path or "/", parsed.query, ""))


def _utc_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone aware")
    return value.astimezone(UTC)


def _bounded_score(value: object, field: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{field} must be finite within zero and one")
    return float(value)


def _exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be boolean")
    return value


def _identifier(prefix: str, *parts: str | None) -> str:
    canonical = "|".join(encode_identifier_part(value) for value in parts)
    return prefix + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _member_parts(identity: str, market: str) -> tuple[str, str]:
    pieces = identity.split("|", 2)
    if len(pieces) != 3 or pieces[0] != market or not pieces[1] or not pieces[2]:
        raise ValueError("member identity is invalid")
    return pieces[1], pieces[2]


def _topic_tags(values: object) -> list[str]:
    if not isinstance(values, (tuple, list, set, frozenset)):
        raise ValueError("topic tags must be a collection")
    output = {_text(value, "topic tag") for value in values}
    return sorted(output)


@dataclass(frozen=True, slots=True)
class ObservedSignalMetrics:
    novelty_score: float
    velocity_score: float
    breadth_score: float
    independence_score: float
    historical_similarity: float | None
    geo_confidence: float

    def __post_init__(self) -> None:
        for field in (
            "novelty_score",
            "velocity_score",
            "breadth_score",
            "independence_score",
            "geo_confidence",
        ):
            object.__setattr__(
                self, field, _bounded_score(getattr(self, field), field.replace("_", " "))
            )
        object.__setattr__(
            self,
            "historical_similarity",
            _bounded_score(self.historical_similarity, "historical similarity", nullable=True),
        )


@dataclass(frozen=True, slots=True)
class EvidenceReceipt:
    member_identity: str
    row_id: str
    source_family: str
    platform: str
    url: str | None
    published_at: datetime | None
    claim_role: str
    direction: str
    geo_confidence: float
    source_label: str | None
    author_label: str | None
    excerpt: str | None
    metric_label: str | None
    availability: str
    factual_conflict: bool = False
    vendor_family: str | None = None
    channel_family: str | None = None
    # True only for an explicit identity that passed validate_source_identity. A receipt
    # without one resolves a family named placeholder vendor for its persisted row, and
    # that placeholder is not a source origin: the independence gate treats it as unknown.
    origin_resolved: bool = dataclass_field(init=False, default=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "member_identity", _text(self.member_identity, "member identity"))
        object.__setattr__(self, "row_id", _text(self.row_id, "row id"))
        if self.source_family not in QUALIFYING_SOURCE_FAMILIES:
            raise ValueError("source family is unsupported")
        explicit_identity = self.vendor_family is not None or self.channel_family is not None
        channel = self.channel_family or self.source_family
        vendor = self.vendor_family
        if vendor is None:
            vendor = "google_youtube" if channel == "youtube" else channel
        legacy_identity = vendor == channel == self.source_family
        if explicit_identity and not legacy_identity:
            vendor, channel = validate_source_identity(
                vendor,
                channel,
                fixture=str(vendor).startswith("staging_fixture_"),
            )
        else:
            vendor = _text(vendor, "vendor family")
            channel = _text(channel, "channel family")
        object.__setattr__(self, "vendor_family", vendor)
        object.__setattr__(self, "channel_family", channel)
        object.__setattr__(self, "origin_resolved", explicit_identity and not legacy_identity)
        if self.source_family != channel:
            raise ValueError("source family must equal channel family")
        object.__setattr__(self, "platform", _text(self.platform, "platform"))
        object.__setattr__(self, "url", _normalized_url(self.url))
        if self.published_at is not None:
            object.__setattr__(
                self, "published_at", _utc_timestamp(self.published_at, "published at")
            )
        if self.claim_role not in _CLAIM_ROLES:
            raise ValueError("claim role is unsupported")
        if self.direction not in _DIRECTIONS:
            raise ValueError("direction is unsupported")
        object.__setattr__(
            self, "geo_confidence", _bounded_score(self.geo_confidence, "geo confidence")
        )
        for field in ("source_label", "author_label", "excerpt", "metric_label"):
            object.__setattr__(
                self, field, _optional_text(getattr(self, field), field.replace("_", " "))
            )
        if self.availability not in _AVAILABILITY:
            raise ValueError("availability is unsupported")
        if type(self.factual_conflict) is not bool:
            raise ValueError("factual conflict must be boolean")

    @property
    def source_origin(self) -> tuple[str, str] | None:
        if not self.origin_resolved:
            return None
        return (self.vendor_family, self.channel_family)


def _validated_receipts(
    component: SignalComponent, receipts: object
) -> tuple[EvidenceReceipt, ...]:
    if not isinstance(receipts, Mapping):
        raise ValueError("receipt mapping is required")
    items: list[EvidenceReceipt] = []
    for key, value in receipts.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, EvidenceReceipt)
            or key != value.row_id
        ):
            raise ValueError("receipt key must equal row id")
        items.append(value)
    expected_rows = set(component.row_receipts)
    actual_rows = set(receipts)
    if expected_rows - actual_rows:
        raise ValueError("missing receipt for component row")
    if actual_rows - expected_rows:
        raise ValueError("unexpected receipt for component row")
    members = set(component.member_identities)
    if any(item.member_identity not in members for item in items):
        raise ValueError("receipt member identity is not in component")
    if {item.member_identity for item in items} != members:
        raise ValueError("missing receipt for component member")
    if any(item.source_family not in component.source_families for item in items):
        raise ValueError("receipt source family is not in component")
    return tuple(sorted(items, key=lambda item: item.row_id))


def _readiness_records(
    receipts: tuple[EvidenceReceipt, ...],
    independence_policy: str = DEFAULT_INDEPENDENCE_POLICY,
) -> tuple[EvidenceRecord, ...]:
    if independence_policy not in INDEPENDENCE_POLICIES:
        raise ValueError("independence policy is unsupported")
    records = []
    for receipt in receipts:
        # Under the explicit origin rule a placeholder identity is unknown origin, so only
        # source_origin is handed on. The legacy rule never saw an identity: its records
        # are built exactly as the 613dea2 row builder built them.
        origin = receipt.source_origin if independence_policy == EXPLICIT_ORIGIN_POLICY else None
        records.append(
            EvidenceRecord(
                row_id=receipt.row_id,
                source_family=receipt.source_family,
                direction=receipt.direction,
                published_at=receipt.published_at,
                availability=receipt.availability,
                geo_confidence=receipt.geo_confidence,
                factual_conflict=receipt.factual_conflict,
                vendor_family=None if origin is None else origin[0],
                channel_family=None if origin is None else origin[1],
            )
        )
    return tuple(records)


def _cluster_signature(component: SignalComponent, receipts: tuple[EvidenceReceipt, ...]) -> str:
    members: set[str] = set()
    for receipt in receipts:
        candidate_type, canonical_value = _member_parts(receipt.member_identity, component.market)
        members.add(
            "|".join(
                encode_identifier_part(value)
                for value in (
                    candidate_type,
                    canonical_value,
                    receipt.source_family,
                    receipt.platform,
                    receipt.row_id,
                )
            )
        )
    canonical = "|".join(encode_identifier_part(member) for member in sorted(members))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scope_values(scope: ResolvedScope) -> dict[str, object]:
    return {
        "client_scope_id": scope.client_scope_id,
        "market_scope": list(scope.market_scope),
        "brand_config_id": scope.brand_config_id,
        "audience_lens_ids": list(scope.audience_lens_ids),
        "theme_id": scope.theme_id,
        "run_id": scope.run_id,
        "contract_version": scope.contract_version,
    }


def build_dynamic_signal_rows(
    *,
    component: SignalComponent,
    scope: ResolvedScope,
    signal_date: date,
    created_at: datetime,
    metrics: ObservedSignalMetrics,
    receipts: Mapping[str, EvidenceReceipt],
    readiness: ReadinessResult,
    readiness_rules: ReadinessRules,
    quality_evaluated: bool = True,
    quality_failed: bool = False,
    factual_conflict: bool = False,
    topic_tags: tuple[str, ...] | list[str] | set[str] | frozenset[str] = (),
    discovery_mode: str = "dynamic",
    model_version: str | None = None,
    independence_policy: str = DEFAULT_INDEPENDENCE_POLICY,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Build exact v2 row dictionaries from already-observed signal evidence.

    The independence policy comes from the run profile of the caller; the default is
    the legacy rule so every retained run and replay keeps its outputs.
    """
    if not isinstance(component, SignalComponent):
        raise ValueError("signal component is invalid")
    if not isinstance(scope, ResolvedScope):
        raise ValueError("resolved scope is invalid")
    if isinstance(signal_date, datetime) or not isinstance(signal_date, date):
        raise ValueError("signal date is invalid")
    if component.market not in scope.market_scope:
        raise ValueError("component market is outside market scope")
    created_at = _utc_timestamp(created_at, "created at")
    if not isinstance(metrics, ObservedSignalMetrics):
        raise ValueError("observed metrics are invalid")
    if not isinstance(readiness, ReadinessResult):
        raise ValueError("readiness result is invalid")
    factual_conflict = _exact_bool(factual_conflict, "factual conflict")
    if discovery_mode not in _DISCOVERY_MODES:
        raise ValueError("discovery mode is unsupported")
    model_version = _optional_text(model_version, "model version")
    ordered_receipts = _validated_receipts(component, receipts)
    recomputed_readiness = evaluate_readiness(
        _readiness_records(ordered_receipts, independence_policy),
        readiness_rules,
        quality_evaluated=quality_evaluated,
        quality_failed=quality_failed,
        factual_conflict=factual_conflict,
        independence_policy=independence_policy,
    )
    if recomputed_readiness != readiness:
        raise ValueError("readiness result does not match retained receipts")
    signature = _cluster_signature(component, ordered_receipts)
    signal_id = _identifier("sig_", scope.client_scope_id, component.market, signature)
    scope_values = _scope_values(scope)
    candidate = {
        **scope_values,
        "signal_id": signal_id,
        "signal_date": signal_date,
        "market": component.market,
        "label": component.label,
        "cluster_signature": signature,
        "cluster_build_version": component.build_version,
        "model_version": model_version,
        "discovery_mode": discovery_mode,
        "topic_tags": _topic_tags(topic_tags),
        "novelty_score": metrics.novelty_score,
        "velocity_score": metrics.velocity_score,
        "breadth_score": metrics.breadth_score,
        "independence_score": metrics.independence_score,
        "historical_similarity": metrics.historical_similarity,
        "geo_confidence": metrics.geo_confidence,
        "evidence_state": readiness.state,
        "created_at": created_at,
        "label_member_identity": component.label_member_identity,
    }
    evidence_rows = tuple(
        {
            **scope_values,
            "signal_date": signal_date,
            "market": component.market,
            "signal_id": signal_id,
            # Evidence belongs to the signal that cites it (4 Sep 2026): one row can
            # sit in two clusters of a market, and each citation is its own row.
            "evidence_id": _identifier(
                "ev_",
                scope.client_scope_id,
                component.market,
                signal_id,
                receipt.source_family,
                receipt.platform,
                receipt.row_id,
                receipt.url,
            ),
            "row_id": receipt.row_id,
            "source_family": receipt.source_family,
            "platform": receipt.platform,
            "url": receipt.url,
            "published_at": receipt.published_at,
            "claim_role": receipt.claim_role,
            "direction": receipt.direction,
            "geo_confidence": receipt.geo_confidence,
            "source_label": receipt.source_label,
            "author_label": receipt.author_label,
            "excerpt": receipt.excerpt,
            "metric_label": receipt.metric_label,
            "availability": receipt.availability,
            "evidence_state": readiness.state,
            "created_at": created_at,
            "vendor_family": receipt.vendor_family,
            "channel_family": receipt.channel_family,
        }
        for receipt in ordered_receipts
    )
    if tuple(candidate) != CANDIDATE_ROW_FIELDS or any(
        tuple(row) != EVIDENCE_ROW_FIELDS for row in evidence_rows
    ):
        raise AssertionError("dynamic signal row field order drift")
    return candidate, evidence_rows
