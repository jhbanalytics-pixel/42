"""Pure point-in-time Open Intelligence replay metrics and dry-run contract."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import combinations
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.candidates import (
    Observation,
    extract_event_observations,
    extract_seed_candidate_observations,
    extract_seed_graph_observations,
)
from src.analysis.open_intelligence.composition import (
    build_anchored_candidate_pairs,
    build_components_with_anchored_semantics,
    build_components_with_validated_semantics,
)
from src.analysis.open_intelligence.graph import (
    ANCHORED_CLUSTER_BUILD_VERSION,
    AnchoredGraphRules,
    GraphRules,
    SignalComponent,
    build_anchored_edges,
)
from src.analysis.open_intelligence.historical_analogue import (
    AnalogueReceipt,
    HistoricalAnalogueRules,
    HistoricalSignalSnapshot,
    find_historical_analogues,
)
from src.analysis.open_intelligence.lineage import (
    LineageRules,
    SignalSnapshot,
    build_signal_lineage_rows,
)
from src.analysis.open_intelligence.pipeline import (
    ACCEPTED_SEED_CANDIDATE_STATUSES,
    DynamicSignalRunResult,
    SourceWindow,
)
from src.analysis.open_intelligence.readiness import (
    EvidenceRecord,
    ReadinessRules,
    evaluate_readiness,
)
from src.contracts.open_intelligence import ResolvedScope

REPLAY_CUTOFF = date(2026, 8, 25)
EXPECTED_EVENT_LEDGER_ROWS = 18_693
QUERY_LIMIT = EXPECTED_EVENT_LEDGER_ROWS + 1
MARKETS = ("ke", "ng", "za")
REPLAY_INPUT_FIELDS = (
    "artifact_version",
    "contract_version",
    "cutoff",
    "scope",
    "source_snapshot",
    "known_event_set",
    "candidate_inputs",
    "observations",
    "components_by_candidate",
    "memberships_by_candidate",
    "receipts_by_member",
    "current_window",
    "history_window",
    "prior_snapshots",
    "provider_outputs",
    "human_review_state",
)
_SCOPE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
)
_SNAPSHOT_FIELDS = (
    "fixture_scope",
    "snapshot_id",
    "captured_at",
    "row_counts_by_table",
    "section_counts",
    "section_identity_sets",
    "table_digests",
    "rows_by_table",
    "source_digest",
)
_KNOWN_EVENT_SET_FIELDS = ("fixture_scope", "set_id", "events", "digest")
_KNOWN_EVENT_FIELDS = ("event_id", "market", "member_identities")
_WINDOW_FIELDS = (
    "start_date",
    "end_date",
    "markets",
    "source_tables",
    "complete_partitions",
)
_SOURCE_ROW_FIELDS = {
    "enriched_content": (
        "id",
        "vendor_family",
        "channel_family",
        "source_family",
        "market",
        "collected_at",
    ),
    "event_ledger": ("ledger_id", "trend_date", "market", "entity_key"),
    "raw_content": (
        "id",
        "vendor_family",
        "channel_family",
        "source_family",
        "market",
        "collected_at",
    ),
    "seed_candidates": (
        "candidate_id",
        "proposed_date",
        "market",
        "candidate_type",
        "candidate_value",
        "sample_row_ids",
    ),
    "seed_graph": ("market", "term", "trend_date", "sample_row_ids"),
    "signal_candidates_v2": ("signal_id", "signal_date", "market", "run_id"),
    "signal_membership_v2": (
        "member_id",
        "member_identity",
        "source_families",
        "vendor_families",
        "channel_families",
        "signal_id",
        "signal_date",
        "run_id",
    ),
}
_ADAPTER_SOURCE_ROW_FIELDS = {
    **_SOURCE_ROW_FIELDS,
    "enriched_content": (
        "id",
        "source",
        "platform",
        "vendor_family",
        "channel_family",
        "source_family",
        "market",
        "published_at",
        "collected_at",
    ),
    "event_ledger": (
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
    ),
    "raw_content": (
        "id",
        "source",
        "platform",
        "vendor_family",
        "channel_family",
        "source_family",
        "market",
        "published_at",
        "collected_at",
    ),
    "seed_candidates": (
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
    ),
    "seed_graph": (
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
    ),
}
_APPROVED_SOURCE_IDENTITY_PAIRS = frozenset(
    {
        ("rss", "news"),
        ("gdelt", "news"),
        ("google_youtube", "youtube"),
        ("socialcrawl", "short_video"),
        ("socialcrawl", "youtube"),
        ("socialcrawl", "reddit"),
    }
)
_CANDIDATE_FIELDS = {
    "event_ledger": ("candidate_id", "identity"),
    "seed_graph": ("candidate_id", "identity", "sample_row_ids"),
    "seed_candidates": ("candidate_id", "identity", "sample_row_ids"),
}
_OBSERVATION_FIELDS = ("identity", "source_max_observed_at")
_COMPONENT_FIELDS = ("market", "member_identities")
_MEMBERSHIP_FIELDS = ("member_id", "member_identity", "row_id")
_RECEIPT_FIELDS = ("row_id", "published_at", "collected_at")
_PRIOR_SNAPSHOT_FIELDS = (
    "signal_id",
    "signal_date",
    "market",
    "run_id",
    "member_identities",
)
_PROVIDER_OUTPUT_FIELDS = (
    "candidate_id",
    "provider_kind",
    "provider_version",
    "metering_status",
    "matrix_digest",
    "pair_count",
    "runtime_ms",
    "input_units",
    "estimated_cost",
    "missing_work",
)
_SEMANTIC_FIXTURE_FIELDS = (
    "fixture_scope",
    "artifact_version",
    "observations",
    "graph_rules",
    "lexical",
    "frozen_embedding",
)
_SEMANTIC_OBSERVATION_FIELDS = (
    "market",
    "term",
    "candidate_type",
    "row_ids",
    "source_families",
    "platforms",
    "aliases",
    "observed_dates",
    "topic_tags",
)
_LEXICAL_FIELDS = ("candidate", "tokenizer", "weighting", "config_digest")
_TOKENIZER_FIELDS = ("version", "normalization", "token_pattern", "stop_words")
_WEIGHTING_FIELDS = (
    "version",
    "term_weight",
    "alias_weight",
    "topic_weight",
)
_FROZEN_EMBEDDING_FIELDS = ("candidate", "model_label", "dimensions", "matrix")
_SEMANTIC_MATRIX_FIELDS = ("left", "right", "score")
_GRAPH_RULE_FIELDS = (
    "semantic_vote_floor",
    "component_similarity_floor",
    "temporal_overlap_days",
)
_METRIC_FIXTURE_FIELDS = (
    "fixture_scope",
    "artifact_version",
    "cutoff",
    "current_window",
    "history_window",
    "evidence_rows",
    "saturation_universes",
    "historical",
    "formulas",
    "formula_digest",
)
_METRIC_EVIDENCE_FIELDS = (
    "row_id",
    "source_family",
    "platform",
    "vendor_id",
    "current_count",
    "history_count",
    "qualifies_evidence",
    "geo_provenance",
)
_SATURATION_UNIVERSE_FIELDS = (
    "universe_id",
    "version",
    "source",
    "source_families",
    "platforms",
    "digest",
)
APPROVED_SATURATION_UNIVERSE_DIGESTS = MappingProxyType(
    {
        (
            "oi_fixture_universe_v1",
            "saturation_universe_v1",
        ): "6b92ebc591ef0dfa8494ec714379728177b11bb80445397fbec4ab39534d794e"
    }
)
_HISTORICAL_FIELDS = ("current", "candidates", "rules", "limit")
_ANALOGUE_SNAPSHOT_FIELDS = (
    "signal_id",
    "as_of",
    "market",
    "terms",
    "source_families",
    "trajectory_signature",
    "evidence_state",
    "receipts",
)
_ANALOGUE_RECEIPT_FIELDS = (
    "receipt_id",
    "signal_id",
    "published_at",
    "source_family",
    "market",
)
_ANALOGUE_RULE_FIELDS = (
    "term_weight",
    "source_weight",
    "trajectory_weight",
    "geography_weight",
    "minimum_similarity",
    "allow_cross_market",
    "minimum_trajectory_points",
    "eligible_evidence_states",
)
_FORMULA_FIELDS = (
    "formula_id",
    "formula_version",
    "metric_family",
    "operation",
    "rival_formula_id",
    "universe_id",
)
_MISSING_MEASUREMENT_REASONS = frozenset(
    {
        "missing_history",
        "incomplete_current_window",
        "incomplete_history_window",
        "zero_denominator",
        "no_qualifying_families",
        "no_qualifying_evidence",
        "geo_provenance_unavailable",
    }
)
_COVERAGE_FIXTURE_FIELDS = (
    "fixture_scope",
    "artifact_version",
    "cutoff",
    "rows",
    "required_upstream_evidence_fields",
    "fixture_digest",
)
_COVERAGE_ROW_FIELDS = (
    "row_id",
    "market",
    "query_group",
    "v2locations",
    "regional_score",
    "sentiment_lexicon_score",
    "tone_polarity",
    "engagement_total",
    "geo_evidence_id",
    "geo_provenance",
    "current_observed_at",
    "prior_observed_at",
    "current_count",
    "prior_count",
    "current_rate",
    "prior_rate",
)
_REQUIRED_COVERAGE_PROPOSALS = (
    "enriched_content.direction_baseline_at",
    "enriched_content.direction_observed_at",
    "enriched_content.geo_evidence_id",
    "enriched_content.geo_provenance",
)
_GRID_FIXTURE_FIELDS = (
    "fixture_scope",
    "artifact_version",
    "cutoff",
    "input_row_count",
    "observations",
    "semantic_providers",
    "graph_rule_candidates",
    "readiness_rule_candidates",
    "component_reviews",
    "known_events",
    "readiness_cases",
    "task5c_contract",
    "fixture_digest",
)
_GRID_PROVIDER_FIELDS = ("candidate_id", "version", "runtime_ms", "matrix")
_GRID_GRAPH_FIELDS = (
    "candidate_id",
    "version",
    "semantic_vote_floor",
    "component_similarity_floor",
    "temporal_overlap_days",
    "runtime_ms",
)
_GRID_READINESS_FIELDS = (
    "candidate_id",
    "version",
    "current_cutoff",
    "minimum_geo_confidence",
    "runtime_ms",
)
_GRID_REVIEW_FIELDS = (
    "signal_id",
    "review_market",
    "component_members",
    "membership_complete",
    "evidence_ready",
    "geo_proven",
)
_GRID_CASE_FIELDS = (
    "case_id",
    "quality_evaluated",
    "quality_failed",
    "factual_conflict",
    "records",
)
_GRID_EVIDENCE_FIELDS = (
    "row_id",
    "source_family",
    "direction",
    "published_at",
    "availability",
    "geo_confidence",
    "factual_conflict",
)
_GRID_TASK5C_FIELDS = (
    "qualifies_evidence",
    "current_window_complete",
    "history_window_complete",
    "missing_work",
)
_APPROVED_GRID_MISSING_WORK = (
    "incomplete_current_window",
    "incomplete_history_window",
    "task5c_nonqualifying_evidence",
)
_LINEAGE_FIXTURE_FIELDS = (
    "fixture_scope",
    "artifact_version",
    "scope",
    "current_signal_date",
    "created_at",
    "lookback_days",
    "rule_version",
    "prior_signal_ids",
    "current_signal_ids",
    "lineage_rules",
    "candidate_rows",
    "membership_rows",
    "fixture_digest",
)
_LINEAGE_SCOPE_FIELDS = (*_SCOPE_FIELDS, "contract_version")
_LINEAGE_CANDIDATE_FIELDS = (
    *_LINEAGE_SCOPE_FIELDS,
    "rule_version",
    "signal_id",
    "signal_date",
    "market",
    "label",
    "member_count",
    "retention_until",
)
_LINEAGE_MEMBERSHIP_FIELDS = (
    *_LINEAGE_SCOPE_FIELDS,
    "rule_version",
    "signal_id",
    "signal_date",
    "market",
    "member_id",
    "member_identity",
    "candidate_type",
    "canonical_value",
    "source_families",
    "platforms",
    "row_id",
    "qualifies_evidence",
    "retention_until",
)
_LINEAGE_RULE_FIELDS = ("overlap_floor",)
_TRAJECTORY_FIXTURE_FIELDS = (
    "fixture_scope",
    "artifact_version",
    "evaluation_date",
    "metric_rows",
    "candidates",
    "fixture_digest",
)
_TRAJECTORY_METRIC_FIELDS = (
    "metric_id",
    "observed_at",
    "velocity",
    "breadth",
    "missing_work",
)
_TRAJECTORY_CANDIDATE_FIELDS = (
    "candidate_id",
    "version",
    "source",
    "evaluation_days",
    "rules",
    "rules_digest",
)
_TRAJECTORY_RULE_FIELDS = (
    "trajectory_name",
    "minimum_velocity",
    "minimum_breadth",
    "target_velocity",
    "target_breadth",
    "invalidation_metric",
    "invalidation_threshold",
)
REPLAY_EVALUATION_FIELDS = (
    "artifact_version",
    "contract_version",
    "mode",
    "certified",
    "cutoff",
    "source_snapshot",
    "known_event_set",
    "provider_comparisons",
    "metric_comparisons",
    "geo_comparisons",
    "graph_rule_comparisons",
    "readiness_rule_comparisons",
    "trajectory_rule_comparisons",
    "replay_metrics",
    "review_sample_ids",
    "human_coherence_status",
    "recommendation",
    "missing_work",
    "artifact_digest",
)
_EVALUATION_SOURCE_FIELDS = ("replay_input_source_snapshot", "fixture_digests")
_APPROVED_EVALUATION_MISSING_WORK = (
    "direction_producer_unavailable",
    "embedding_metering_unavailable",
    "geo_provenance_unavailable",
    "incomplete_current_window",
    "incomplete_history_window",
    "task5c_nonqualifying_evidence",
    "velocity_score_unavailable",
)
_APPROVED_REVIEW_SAMPLE_IDS = ("sig_dup", "sig_ke", "sig_ng")
APPROVED_REPLAY_EVALUATION_FIXTURE_DIGESTS = MappingProxyType(
    {
        "coverage": "c7fa5e0be52ce4ad851509625d759d41fff943c279203c87988ae60f4029cf81",
        "grid": "579b676d8e190e750288e05fbd92e2e4ae97498583669b3b38a2fbff8f9dac4e",
        "lineage": "a50b4fd361fa7b2554d7ecb68133e0520edca55ed3c2a98f7843cf094da0ff59",
        "metric": "805b2050ab93ebd22a6c9713c74771b47afac776044e7102f750dfb185fc8f3c",
        "replay_input": "6f506f9d0323018a8e9acf72a986cff883aadfd514aff7977f805f686397c4c9",
        "replay_source": "533624d759300da6ae1d811c12dfd83cab1c69f90e8039ac5ece79043e386597",
        "semantic": "120cd03f40a05c8b54814dbac868cd5016e9290139648240a4c304d43cb03b37",
        "trajectory": "9ea3314efb2a8adc99a62ad4b630878d0f631c5a410b8ef98bee1e679d60393f",
    }
)
APPROVED_REPLAY_EVALUATION_OUTPUT_DIGESTS = MappingProxyType(
    {
        "geo": "31c02e7da31a8b1607ba580855554cd1d8c7b344c984c74e36356c928cb3907e",
        "graph": "56f6e5eede2dfb9205b76c9b409925bacfd5499e032d8064d1f28c4ab2e4f8bd",
        "lineage": "99d55defd049295706167796c9b339e79340460b9bbfcfb1b3f93db89b2dcd0c",
        "metric": "e0a60b3296ae12f3f41753ec1692acbc3d266ce00bc6890f31c3a69c48d4b2fa",
        "provider": "2d302c6a99f48f6a403030bfc977cdd71c4a951928e69b842e9d210b4787f06d",
        "readiness": "56f6e5eede2dfb9205b76c9b409925bacfd5499e032d8064d1f28c4ab2e4f8bd",
        "replay_metrics": "e739197478a2e255eb8c8431ee45528bfd0ccc20f8c0e222c05a1ee348743946",
        "trajectory": "51368fefcd549dc0becc9731e1cb045e49b1d9075dd9bbae275a6780e63ebbb2",
    }
)
APPROVED_PREDICTIONS_BASELINE_RULES_DIGEST = (
    "4266493be2c157c8d5fca47a152680f666d247772e6741d22168c1952d65a51d"
)
APPROVED_PREDICTIONS_BASELINE_METADATA = MappingProxyType(
    {
        "candidate_id": "baseline_predictions_v1",
        "version": "predictions_py_baseline_v1",
        "source": "predictions_py_baseline",
        "evaluation_days": 14,
        "rules_digest": APPROVED_PREDICTIONS_BASELINE_RULES_DIGEST,
    }
)
_SECTION_FIELDS = (
    "components",
    "event_ledger_candidates",
    "known_events",
    "memberships",
    "observations",
    "prior_snapshots",
    "provider_outputs",
    "receipts",
    "seed_candidates",
    "seed_graph_candidates",
)
_FORBIDDEN_FIXTURE_KEYS = frozenset({"certified", "certification", "recommendation"})
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STAGING_TABLE = re.compile(r"ogilvy-trends-v2\.trends_v2_staging\.[a-z0-9_]+\Z")


class IncompleteReplayInput(ValueError):
    pass


class FutureLeakageDetected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReplayInputBundle:
    artifact_version: str
    contract_version: str
    cutoff: date
    scope: Mapping[str, object]
    source_snapshot: Mapping[str, object]
    known_event_set: Mapping[str, object]
    candidate_inputs: Mapping[str, object]
    observations: tuple[Mapping[str, object], ...]
    components_by_candidate: Mapping[str, object]
    memberships_by_candidate: Mapping[str, object]
    receipts_by_member: Mapping[str, object]
    current_window: Mapping[str, object]
    history_window: Mapping[str, object]
    prior_snapshots: tuple[Mapping[str, object], ...]
    provider_outputs: tuple[Mapping[str, object], ...]
    human_review_state: str
    # Scoring source authority. Optional and last, so every existing replay,
    # graph, lineage, trajectory, historical and evaluation consumer is
    # unchanged. None, an empty mapping, an absent candidate or an incomplete
    # window all mean scoring authority is unavailable. None of them ever means
    # zero, and none of them authorizes a call to score_signal.
    scoring_sources_by_candidate: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ReplayCandidateInputs:
    event_ledger: tuple[object, ...]
    seed_graph: tuple[object, ...]
    seed_candidates: tuple[object, ...]
    candidate_only_receipts: Mapping[str, tuple[str, ...]]
    unresolved_sample_ids: tuple[str, ...]
    missing_work: tuple[str, ...]
    source_windows: Mapping[str, SourceWindow]


@dataclass(frozen=True, slots=True)
class ReplaySemanticCandidate:
    candidate_id: str
    provider_kind: str
    provider_version: str
    metering_status: str
    matrix_digest: str
    pair_count: int
    runtime_ms: float
    input_units: int | None
    estimated_cost: float | None
    missing_work: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplaySemanticComparison:
    candidates: tuple[ReplaySemanticCandidate, ...]
    recommendation: None = None


@dataclass(frozen=True, slots=True)
class MissingMeasurement:
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in _MISSING_MEASUREMENT_REASONS:
            raise ValueError("missing measurement reason is invalid")


@dataclass(frozen=True, slots=True)
class ReplayMetricFormulaResult:
    formula_id: str
    formula_version: str
    metric_family: str
    current_window: Mapping[str, object]
    history_window: Mapping[str, object]
    source_row_ids: tuple[str, ...]
    numerator: float | None
    denominator: float | None
    value: float | None
    missing_measurement: MissingMeasurement | None
    rival_formula_id: str


@dataclass(frozen=True, slots=True)
class ReplayMetricComparison:
    results: tuple[ReplayMetricFormulaResult, ...]
    recommendation: None = None


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    count: int
    row_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count != len(self.row_ids)
            or tuple(sorted(set(self.row_ids))) != self.row_ids
        ):
            raise ValueError("coverage record is invalid")


@dataclass(frozen=True, slots=True)
class ReplayCoverageArtifact:
    artifact_version: str
    total_projected_rows: CoverageRecord
    explicit_geo_evidence_rows: CoverageRecord
    unresolved_geo_rows: CoverageRecord
    factual_location_field_presence: CoverageRecord
    direction_eligible_rows: CoverageRecord
    direction_conflict_rows: CoverageRecord
    unavailable_producer_reasons: tuple[str, ...]
    required_upstream_evidence_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayGridCandidateResult:
    candidate_id: str
    provider_candidate_id: str
    graph_rule_candidate_id: str
    readiness_rule_candidate_id: str
    known_event_recall: Metric
    duplicate_clusters: Metric
    foreign_leakage: Metric
    membership_completeness: Metric
    evidence_coverage: Metric
    geo_coverage: Metric
    ready: Metric
    thin: Metric
    contradictory: Metric
    unchecked: Metric
    missing_work: tuple[str, ...]
    runtime_ms: float
    status: str
    display_eligible: bool


@dataclass(frozen=True, slots=True)
class ReplayGridArtifact:
    results: tuple[ReplayGridCandidateResult, ...]
    recommendation: None = None
    certified_rule: None = None


@dataclass(frozen=True, slots=True)
class ReplayLineageArtifact:
    prior_snapshots: tuple[SignalSnapshot, ...]
    current_snapshots: tuple[SignalSnapshot, ...]
    lineage_rows: tuple[Mapping[str, object], ...]
    missing_work: tuple[str, ...]
    status: str = "provisional"
    recommendation: None = None


@dataclass(frozen=True, slots=True)
class TrajectoryRule:
    trajectory_name: str
    minimum_velocity: float
    minimum_breadth: float
    target_velocity: float
    target_breadth: float
    invalidation_metric: str
    invalidation_threshold: float


@dataclass(frozen=True, slots=True)
class TrajectoryEvaluation:
    metric_id: str
    matched_trajectory: str | None
    target_velocity: float | None
    target_breadth: float | None
    invalidation_metric: str | None
    invalidation_threshold: float | None
    invalidation_at: str | None
    missing_work: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayTrajectoryCandidateResult:
    candidate_id: str
    version: str
    rules_digest: str
    evaluations: tuple[TrajectoryEvaluation, ...]
    matched: Metric
    missing: Metric
    missing_work: tuple[str, ...]
    status: str
    display_eligible: bool


@dataclass(frozen=True, slots=True)
class ReplayTrajectoryArtifact:
    results: tuple[ReplayTrajectoryCandidateResult, ...]
    recommendation: None = None
    certified_rule: None = None


@dataclass(frozen=True, slots=True)
class ReplayEvaluationArtifact:
    artifact_version: str
    contract_version: str
    mode: str
    certified: bool
    cutoff: date
    source_snapshot: Mapping[str, object]
    known_event_set: Mapping[str, object]
    provider_comparisons: ReplaySemanticComparison
    metric_comparisons: ReplayMetricComparison
    geo_comparisons: ReplayCoverageArtifact
    graph_rule_comparisons: ReplayGridArtifact
    readiness_rule_comparisons: ReplayGridArtifact
    trajectory_rule_comparisons: ReplayTrajectoryArtifact
    replay_metrics: Mapping[str, object]
    review_sample_ids: tuple[str, ...]
    human_coherence_status: str
    recommendation: None
    missing_work: tuple[str, ...]
    artifact_digest: str


def _typed(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise IncompleteReplayInput("canonical value must be finite")
        return {"type": "float", "value": value.hex()}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise IncompleteReplayInput("canonical timestamp must be timezone aware")
        return {"type": "datetime", "value": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "type": "dataclass",
            "value": [[field.name, _typed(getattr(value, field.name))] for field in fields(value)],
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise IncompleteReplayInput("canonical mapping keys must be strings")
        return {
            "type": "object",
            "value": [[key, _typed(value[key])] for key in sorted(value)],
        }
    if isinstance(value, (tuple, list)):
        return {"type": "array", "value": [_typed(item) for item in value]}
    raise IncompleteReplayInput("canonical value type is unsupported")


def canonical_typed_json(value: object) -> str:
    return json.dumps(_typed(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_typed_json(value).encode("utf-8")).hexdigest()


def replay_input_digest(value: object) -> str:
    return _digest(value)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise IncompleteReplayInput(f"{field} must be a mapping")
    return dict(value)


def _sequence(value: object, field: str) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise IncompleteReplayInput(f"{field} must be an array")
    return list(value)


def _exact_fields(value: Mapping[str, object], fields: tuple[str, ...], field: str) -> None:
    if tuple(value) != fields and set(value) != set(fields):
        raise IncompleteReplayInput(f"{field} fields are invalid")


def _date_value(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise IncompleteReplayInput(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise IncompleteReplayInput(f"{field} must be an ISO date") from error


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise IncompleteReplayInput(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise IncompleteReplayInput(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IncompleteReplayInput(f"{field} must be timezone aware")
    return parsed.astimezone(UTC)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in sorted(value.items())})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _valid_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _HEX_DIGEST.fullmatch(value) is None:
        raise IncompleteReplayInput(f"{field} digest is invalid")
    return value


def _window(value: object, field: str, cutoff: date) -> Mapping[str, object]:
    window = _mapping(value, field)
    _exact_fields(window, _WINDOW_FIELDS, field)
    start = _date_value(window["start_date"], f"{field} start date")
    end = _date_value(window["end_date"], f"{field} end date")
    markets = window["markets"]
    tables = window["source_tables"]
    if start > end or end > cutoff:
        raise IncompleteReplayInput(f"{field} is unbounded")
    if (
        not isinstance(markets, list)
        or tuple(markets) != tuple(sorted(set(markets)))
        or not markets
    ):
        raise IncompleteReplayInput(f"{field} markets are invalid")
    if any(market not in MARKETS for market in markets):
        raise IncompleteReplayInput(f"{field} markets are invalid")
    if not isinstance(tables, list) or tuple(tables) != tuple(sorted(set(tables))) or not tables:
        raise IncompleteReplayInput(f"{field} source tables are invalid")
    if any(
        not isinstance(table, str) or _STAGING_TABLE.fullmatch(table) is None for table in tables
    ):
        raise IncompleteReplayInput(f"{field} source tables are invalid")
    if window["complete_partitions"] is not True:
        raise IncompleteReplayInput(f"{field} must be complete")
    normalized = dict(window)
    normalized["start_date"] = start
    normalized["end_date"] = end
    return _freeze(normalized)  # type: ignore[return-value]


def _reject_future(value: object, cutoff_end: datetime) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                key
                in {
                    "captured_at",
                    "generated_at",
                    "source_max_observed_at",
                    "published_at",
                    "collected_at",
                    "observed_at",
                    "as_of",
                }
                and _timestamp(item, key) > cutoff_end
            ):
                raise FutureLeakageDetected(f"future timestamp in {key}")
            if (
                key in {"trend_date", "proposed_date", "signal_date"}
                and _date_value(item, key) > cutoff_end.date()
            ):
                raise FutureLeakageDetected(f"future date in {key}")
            _reject_future(item, cutoff_end)
    elif isinstance(value, list):
        for item in value:
            _reject_future(item, cutoff_end)


def _reject_forbidden_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _FORBIDDEN_FIXTURE_KEYS:
                raise IncompleteReplayInput(f"forbidden fixture key: {key}")
            _reject_forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_keys(item)


def _unique(values: list[object], key: str, field: str) -> None:
    identities = []
    for value in values:
        item = _mapping(value, field)
        identity = item.get(key)
        if not isinstance(identity, str) or not identity:
            raise IncompleteReplayInput(f"{field} identity is invalid")
        identities.append(identity)
    if len(identities) != len(set(identities)):
        raise IncompleteReplayInput(f"duplicate {field} identity")


def _nonnegative_number(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise IncompleteReplayInput(f"{field} is invalid")
    return float(value)


def _bounded_unit(value: object, field: str) -> float:
    measured = _nonnegative_number(value, field)
    if measured > 1:
        raise IncompleteReplayInput(f"{field} must be finite within zero and one")
    return measured


def validate_replay_semantic_candidate(value: object) -> ReplaySemanticCandidate:
    candidate = _mapping(value, "semantic candidate")
    _exact_fields(candidate, _PROVIDER_OUTPUT_FIELDS, "semantic candidate")
    for field in ("candidate_id", "provider_kind", "provider_version", "metering_status"):
        if not isinstance(candidate[field], str) or not candidate[field]:
            raise IncompleteReplayInput(f"semantic candidate {field} is invalid")
    digest = _valid_digest(candidate["matrix_digest"], "semantic candidate matrix")
    pair_count = candidate["pair_count"]
    if isinstance(pair_count, bool) or not isinstance(pair_count, int) or pair_count < 0:
        raise IncompleteReplayInput("semantic candidate pair count is invalid")
    runtime_ms = _nonnegative_number(candidate["runtime_ms"], "semantic candidate runtime")
    input_units = candidate["input_units"]
    if input_units is not None and (
        isinstance(input_units, bool) or not isinstance(input_units, int) or input_units < 0
    ):
        raise IncompleteReplayInput("semantic candidate input units are invalid")
    estimated_cost = candidate["estimated_cost"]
    if estimated_cost is not None:
        estimated_cost = _nonnegative_number(estimated_cost, "semantic candidate estimated cost")
    missing_work = tuple(_sequence(candidate["missing_work"], "semantic candidate missing work"))
    if tuple(sorted(set(missing_work))) != missing_work or any(
        not isinstance(item, str) or not item for item in missing_work
    ):
        raise IncompleteReplayInput("semantic candidate missing work is invalid")
    return ReplaySemanticCandidate(
        candidate_id=candidate["candidate_id"],
        provider_kind=candidate["provider_kind"],
        provider_version=candidate["provider_version"],
        metering_status=candidate["metering_status"],
        matrix_digest=digest,
        pair_count=pair_count,
        runtime_ms=runtime_ms,
        input_units=input_units,
        estimated_cost=estimated_cost,
        missing_work=missing_work,
    )


def _semantic_identity(observation: Observation) -> str:
    return f"{observation.market}|{observation.candidate_type}|{observation.term}"


def _semantic_observations(value: object) -> tuple[Observation, ...]:
    observations = []
    for raw in _sequence(value, "semantic observations"):
        row = _mapping(raw, "semantic observation")
        _exact_fields(row, _SEMANTIC_OBSERVATION_FIELDS, "semantic observation")
        try:
            observations.append(Observation(**row))
        except ValueError as error:
            raise IncompleteReplayInput("semantic observation is invalid") from error
    ordered = tuple(sorted(observations, key=_semantic_identity))
    identities = tuple(_semantic_identity(item) for item in ordered)
    if len(identities) != len(set(identities)):
        raise IncompleteReplayInput("duplicate semantic observation identity")
    return ordered


def _matrix_rows(value: object) -> tuple[dict[str, object], ...]:
    rows = []
    for raw in _sequence(value, "semantic matrix"):
        row = _mapping(raw, "semantic matrix row")
        _exact_fields(row, _SEMANTIC_MATRIX_FIELDS, "semantic matrix row")
        rows.append(row)
    return tuple(sorted(rows, key=lambda row: (str(row["left"]), str(row["right"]))))


@dataclass(frozen=True, slots=True)
class _FrozenSemanticProvider:
    version: str
    scores: Mapping[tuple[str, str], object]

    def similarities(
        self, _observations: tuple[Observation, ...]
    ) -> Mapping[tuple[str, str], object]:
        return self.scores


def _admit_matrix(
    rows: tuple[dict[str, object], ...],
    observations: tuple[Observation, ...],
    version: str,
    rules: GraphRules,
) -> None:
    scores = {(str(row["left"]), str(row["right"])): row["score"] for row in rows}
    if len(scores) != len(rows):
        raise ValueError("extra semantic pair")
    build_components_with_validated_semantics(
        observations,
        _FrozenSemanticProvider(version, MappingProxyType(scores)),
        rules,
    )


def _tokens(value: str, tokenizer: Mapping[str, object]) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFD", normalized)
        if unicodedata.category(character) != "Mn"
    )
    stop_words = set(tokenizer["stop_words"])
    return tuple(
        token
        for token in re.findall(str(tokenizer["token_pattern"]), normalized)
        if token not in stop_words
    )


def _lexical_features(
    observation: Observation,
    tokenizer: Mapping[str, object],
    weighting: Mapping[str, object],
) -> tuple[dict[str, float], int]:
    features: dict[str, float] = {}
    input_units = 0
    lanes = (
        ((observation.term,), float(weighting["term_weight"])),
        (observation.aliases, float(weighting["alias_weight"])),
        (observation.topic_tags, float(weighting["topic_weight"])),
    )
    for values, weight in lanes:
        for value in values:
            tokens = _tokens(value, tokenizer)
            input_units += len(tokens)
            for token in tokens:
                features[token] = max(features.get(token, 0.0), weight)
    return features, input_units


def _lexical_matrix(
    observations: tuple[Observation, ...],
    tokenizer: Mapping[str, object],
    weighting: Mapping[str, object],
) -> tuple[tuple[dict[str, object], ...], int]:
    features = {}
    input_units = 0
    for observation in observations:
        identity = _semantic_identity(observation)
        features[identity], units = _lexical_features(observation, tokenizer, weighting)
        input_units += units
    rows = []
    for left, right in combinations(observations, 2):
        if left.market != right.market:
            continue
        left_id = _semantic_identity(left)
        right_id = _semantic_identity(right)
        tokens = set(features[left_id]) | set(features[right_id])
        numerator = sum(
            min(features[left_id].get(token, 0), features[right_id].get(token, 0))
            for token in tokens
        )
        denominator = sum(
            max(features[left_id].get(token, 0), features[right_id].get(token, 0))
            for token in tokens
        )
        if denominator <= 0:
            raise IncompleteReplayInput("lexical pair union weight must be strictly positive")
        rows.append({"left": left_id, "right": right_id, "score": numerator / denominator})
    return tuple(rows), input_units


def compare_replay_semantic_candidates(value: object) -> ReplaySemanticComparison:
    fixture = _mapping(value, "semantic fixture")
    _exact_fields(fixture, _SEMANTIC_FIXTURE_FIELDS, "semantic fixture")
    if (
        fixture["fixture_scope"] != "synthetic_test_only"
        or fixture["artifact_version"] != "replay_semantic_candidates_v1"
    ):
        raise IncompleteReplayInput("semantic fixture identity is invalid")
    observations = _semantic_observations(fixture["observations"])
    rule_values = _mapping(fixture["graph_rules"], "semantic graph rules")
    _exact_fields(rule_values, _GRAPH_RULE_FIELDS, "semantic graph rules")
    rules = GraphRules(**rule_values)

    lexical = _mapping(fixture["lexical"], "lexical fixture")
    _exact_fields(lexical, _LEXICAL_FIELDS, "lexical fixture")
    tokenizer = _mapping(lexical["tokenizer"], "lexical tokenizer")
    weighting = _mapping(lexical["weighting"], "lexical weighting")
    _exact_fields(tokenizer, _TOKENIZER_FIELDS, "lexical tokenizer")
    _exact_fields(weighting, _WEIGHTING_FIELDS, "lexical weighting")
    if (
        tokenizer["normalization"] != "nfkc_casefold_ascii_alnum"
        or tokenizer["token_pattern"] != "[a-z0-9]+"
        or not isinstance(tokenizer["stop_words"], list)
        or tuple(sorted(set(tokenizer["stop_words"]))) != tuple(tokenizer["stop_words"])
    ):
        raise IncompleteReplayInput("lexical tokenizer is invalid")
    weights = tuple(
        _nonnegative_number(weighting[field], f"lexical {field}")
        for field in ("term_weight", "alias_weight", "topic_weight")
    )
    if not any(weight > 0 for weight in weights):
        raise IncompleteReplayInput("at least one lexical weight must be strictly positive")
    config_digest = _valid_digest(lexical["config_digest"], "lexical config")
    if config_digest != _digest({"tokenizer": tokenizer, "weighting": weighting}):
        raise IncompleteReplayInput("lexical config digest does not match")
    lexical_candidate = validate_replay_semantic_candidate(lexical["candidate"])
    expected_version = f"lexical_v1_{tokenizer['version']}_{weighting['version']}"
    if (
        lexical_candidate.provider_kind != "lexical_similarity"
        or lexical_candidate.provider_version != expected_version
        or lexical_candidate.metering_status != "fixture_complete"
        or lexical_candidate.estimated_cost != 0
        or lexical_candidate.missing_work
    ):
        raise IncompleteReplayInput("lexical provider kind or fixture facts are invalid")
    lexical_rows, input_units = _lexical_matrix(observations, tokenizer, weighting)
    _admit_matrix(lexical_rows, observations, lexical_candidate.provider_version, rules)
    if (
        lexical_candidate.matrix_digest != _digest(lexical_rows)
        or lexical_candidate.pair_count != len(lexical_rows)
        or lexical_candidate.input_units != input_units
    ):
        raise IncompleteReplayInput("lexical candidate matrix facts are inconsistent")

    frozen = _mapping(fixture["frozen_embedding"], "frozen embedding fixture")
    _exact_fields(frozen, _FROZEN_EMBEDDING_FIELDS, "frozen embedding fixture")
    frozen_candidate = validate_replay_semantic_candidate(frozen["candidate"])
    if (
        frozen_candidate.provider_kind != "frozen_embedding_matrix"
        or frozen_candidate.metering_status not in {"frozen_only", "incomplete"}
        or frozen_candidate.estimated_cost is not None
        or frozen_candidate.input_units is not None
        or "embedding_metering_unavailable" not in frozen_candidate.missing_work
        or not isinstance(frozen["model_label"], str)
        or not frozen["model_label"]
        or frozen["model_label"].replace("-", "_") not in frozen_candidate.provider_version
        or isinstance(frozen["dimensions"], bool)
        or not isinstance(frozen["dimensions"], int)
        or frozen["dimensions"] < 1
    ):
        raise IncompleteReplayInput("frozen embedding estimated cost or facts are invalid")
    frozen_rows = _matrix_rows(frozen["matrix"])
    _admit_matrix(frozen_rows, observations, frozen_candidate.provider_version, rules)
    if frozen_candidate.matrix_digest != _digest(frozen_rows) or frozen_candidate.pair_count != len(
        frozen_rows
    ):
        raise IncompleteReplayInput("frozen embedding matrix facts are inconsistent")
    return ReplaySemanticComparison(
        tuple(sorted((lexical_candidate, frozen_candidate), key=lambda item: item.candidate_id))
    )


def _metric_window(
    value: object,
    field: str,
    cutoff: date,
    expected_days: int,
) -> Mapping[str, object]:
    window = _mapping(value, field)
    _exact_fields(window, _WINDOW_FIELDS, field)
    start = _date_value(window["start_date"], f"{field} start date")
    end = _date_value(window["end_date"], f"{field} end date")
    if end > cutoff:
        raise FutureLeakageDetected("future metric window")
    if start > end or (end - start).days + 1 != expected_days:
        raise IncompleteReplayInput(f"{field} bounds are invalid")
    markets = window["markets"]
    tables = window["source_tables"]
    if (
        not isinstance(markets, list)
        or tuple(markets) != tuple(sorted(set(markets)))
        or not markets
        or any(market not in MARKETS for market in markets)
    ):
        raise IncompleteReplayInput(f"{field} markets are invalid")
    if (
        not isinstance(tables, list)
        or tuple(tables) != tuple(sorted(set(tables)))
        or not tables
        or any(
            not isinstance(table, str) or _STAGING_TABLE.fullmatch(table) is None
            for table in tables
        )
    ):
        raise IncompleteReplayInput(f"{field} source tables are invalid")
    if type(window["complete_partitions"]) is not bool:
        raise IncompleteReplayInput(f"{field} completeness is invalid")
    normalized = dict(window)
    normalized["start_date"] = start
    normalized["end_date"] = end
    return _freeze(normalized)  # type: ignore[return-value]


def _metric_strings(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    items = tuple(_sequence(value, field))
    if (
        (not allow_empty and not items)
        or tuple(sorted(set(items))) != items
        or any(not isinstance(item, str) or not item for item in items)
    ):
        raise IncompleteReplayInput(f"{field} is invalid")
    return items  # type: ignore[return-value]


def _metric_evidence_rows(value: object) -> tuple[dict[str, object], ...]:
    rows = []
    for raw in _sequence(value, "metric evidence rows"):
        row = _mapping(raw, "metric evidence row")
        _exact_fields(row, _METRIC_EVIDENCE_FIELDS, "metric evidence row")
        for field in ("row_id", "source_family", "platform", "vendor_id"):
            if not isinstance(row[field], str) or not row[field]:
                raise IncompleteReplayInput(f"metric evidence {field} is invalid")
        for field in ("current_count", "history_count"):
            _nonnegative_number(row[field], f"metric evidence {field}")
        if type(row["qualifies_evidence"]) is not bool:
            raise IncompleteReplayInput("metric evidence qualification is invalid")
        if row["geo_provenance"] is not None and (
            not isinstance(row["geo_provenance"], str) or not row["geo_provenance"]
        ):
            raise IncompleteReplayInput("metric evidence geo provenance is invalid")
        rows.append(row)
    ordered = tuple(sorted(rows, key=lambda row: str(row["row_id"])))
    if len({row["row_id"] for row in ordered}) != len(ordered):
        raise IncompleteReplayInput("duplicate metric evidence row ID")
    return ordered


def _analogue_receipt(value: object) -> AnalogueReceipt:
    row = _mapping(value, "analogue receipt")
    _exact_fields(row, _ANALOGUE_RECEIPT_FIELDS, "analogue receipt")
    return AnalogueReceipt(
        receipt_id=row["receipt_id"],
        signal_id=row["signal_id"],
        published_at=_timestamp(row["published_at"], "analogue receipt published at"),
        source_family=row["source_family"],
        market=row["market"],
    )


def _analogue_snapshot(value: object) -> HistoricalSignalSnapshot:
    row = _mapping(value, "analogue snapshot")
    _exact_fields(row, _ANALOGUE_SNAPSHOT_FIELDS, "analogue snapshot")
    return HistoricalSignalSnapshot(
        signal_id=row["signal_id"],
        as_of=_timestamp(row["as_of"], "analogue snapshot as of"),
        market=row["market"],
        terms=tuple(_sequence(row["terms"], "analogue terms")),
        source_families=tuple(_sequence(row["source_families"], "analogue families")),
        trajectory_signature=tuple(_sequence(row["trajectory_signature"], "analogue trajectory")),
        evidence_state=row["evidence_state"],
        receipts=tuple(_analogue_receipt(item) for item in _sequence(row["receipts"], "receipts")),
    )


def _historical_similarity(value: object, cutoff: date) -> tuple[float | None, tuple[str, ...]]:
    historical = _mapping(value, "metric historical fixture")
    _exact_fields(historical, _HISTORICAL_FIELDS, "metric historical fixture")
    _reject_future(historical, datetime.combine(cutoff, time.max, tzinfo=UTC))
    current = _analogue_snapshot(historical["current"])
    candidates = tuple(
        _analogue_snapshot(item)
        for item in _sequence(historical["candidates"], "historical candidates")
    )
    rule_values = _mapping(historical["rules"], "historical rules")
    _exact_fields(rule_values, _ANALOGUE_RULE_FIELDS, "historical rules")
    rules = HistoricalAnalogueRules(
        **{
            **rule_values,
            "eligible_evidence_states": tuple(rule_values["eligible_evidence_states"]),
        }
    )
    result = find_historical_analogues(current, candidates, rules, limit=historical["limit"])
    current_receipts = {receipt.receipt_id for receipt in current.receipts}
    if not result.matches:
        return None, tuple(sorted(current_receipts))
    maximum = max(match.similarity for match in result.matches)
    source_rows = tuple(
        sorted(
            current_receipts | {row_id for match in result.matches for row_id in match.receipt_ids}
        )
    )
    return maximum, source_rows


def _missing_formula_result(
    formula: Mapping[str, object],
    current_window: Mapping[str, object],
    history_window: Mapping[str, object],
    source_row_ids: tuple[str, ...],
    reason: str,
) -> ReplayMetricFormulaResult:
    return ReplayMetricFormulaResult(
        formula_id=formula["formula_id"],
        formula_version=formula["formula_version"],
        metric_family=formula["metric_family"],
        current_window=current_window,
        history_window=history_window,
        source_row_ids=source_row_ids,
        numerator=None,
        denominator=None,
        value=None,
        missing_measurement=MissingMeasurement(reason),
        rival_formula_id=formula["rival_formula_id"],
    )


def _measured_formula_result(
    formula: Mapping[str, object],
    current_window: Mapping[str, object],
    history_window: Mapping[str, object],
    source_row_ids: tuple[str, ...],
    numerator: float,
    denominator: float,
) -> ReplayMetricFormulaResult:
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator <= 0:
        return _missing_formula_result(
            formula,
            current_window,
            history_window,
            source_row_ids,
            "zero_denominator",
        )
    measured = numerator / denominator
    if not math.isfinite(measured):
        raise IncompleteReplayInput("metric formula value is nonfinite")
    return ReplayMetricFormulaResult(
        formula_id=formula["formula_id"],
        formula_version=formula["formula_version"],
        metric_family=formula["metric_family"],
        current_window=current_window,
        history_window=history_window,
        source_row_ids=source_row_ids,
        numerator=float(numerator),
        denominator=float(denominator),
        value=measured,
        missing_measurement=None,
        rival_formula_id=formula["rival_formula_id"],
    )


def compare_replay_metric_formulas(value: object) -> ReplayMetricComparison:
    fixture = _mapping(value, "metric fixture")
    _exact_fields(fixture, _METRIC_FIXTURE_FIELDS, "metric fixture")
    if (
        fixture["fixture_scope"] != "synthetic_test_only"
        or fixture["artifact_version"] != "replay_metric_formulas_v1"
    ):
        raise IncompleteReplayInput("metric fixture identity is invalid")
    cutoff = _date_value(fixture["cutoff"], "metric cutoff")
    current_window = _metric_window(fixture["current_window"], "metric current window", cutoff, 7)
    history_window = _metric_window(fixture["history_window"], "metric history window", cutoff, 28)
    if current_window["end_date"] != cutoff or history_window["end_date"] != current_window[
        "start_date"
    ] - timedelta(days=1):
        raise IncompleteReplayInput("metric window anchor is invalid")
    rows = _metric_evidence_rows(fixture["evidence_rows"])
    all_row_ids = tuple(str(row["row_id"]) for row in rows)
    qualifying = tuple(row for row in rows if row["qualifies_evidence"])
    qualifying_ids = tuple(str(row["row_id"]) for row in qualifying)

    universes = {}
    for raw in _sequence(fixture["saturation_universes"], "saturation universes"):
        universe = _mapping(raw, "saturation universe")
        _exact_fields(universe, _SATURATION_UNIVERSE_FIELDS, "saturation universe")
        for field in ("universe_id", "version", "source"):
            if not isinstance(universe[field], str) or not universe[field]:
                raise IncompleteReplayInput(f"saturation universe {field} is invalid")
        if universe["source"] != "independent_fixture_registry":
            raise IncompleteReplayInput("independent saturation universe is required")
        _metric_strings(universe["source_families"], "saturation source families", allow_empty=True)
        _metric_strings(universe["platforms"], "saturation platforms", allow_empty=True)
        facts = {key: item for key, item in universe.items() if key != "digest"}
        if _valid_digest(universe["digest"], "saturation universe") != _digest(facts):
            raise IncompleteReplayInput("saturation universe digest does not match")
        approved_digest = APPROVED_SATURATION_UNIVERSE_DIGESTS.get(
            (universe["universe_id"], universe["version"])
        )
        if approved_digest is None:
            raise IncompleteReplayInput("approved saturation universe identity is unknown")
        if universe["digest"] != approved_digest:
            raise IncompleteReplayInput("approved saturation universe digest does not match")
        if universe["universe_id"] in universes:
            raise IncompleteReplayInput("duplicate saturation universe ID")
        universes[universe["universe_id"]] = universe
    formulas = tuple(
        sorted(
            (
                _mapping(item, "metric formula")
                for item in _sequence(fixture["formulas"], "formulas")
            ),
            key=lambda item: str(item.get("formula_id")),
        )
    )
    for formula in formulas:
        _exact_fields(formula, _FORMULA_FIELDS, "metric formula")
        for field in _FORMULA_FIELDS[:-1]:
            if not isinstance(formula[field], str) or not formula[field]:
                raise IncompleteReplayInput(f"metric formula {field} is invalid")
    formula_ids = {formula["formula_id"] for formula in formulas}
    formulas_by_id = {formula["formula_id"]: formula for formula in formulas}
    if len(formula_ids) != len(formulas) or any(
        formula["rival_formula_id"] not in formula_ids
        or formula["rival_formula_id"] == formula["formula_id"]
        or formulas_by_id[formula["rival_formula_id"]]["rival_formula_id"] != formula["formula_id"]
        for formula in formulas
    ):
        raise IncompleteReplayInput("metric formula rivals must be nonself and symmetric")
    if any(
        (not isinstance(formula["universe_id"], str) or formula["universe_id"] not in universes)
        if formula["metric_family"] == "breadth"
        else formula["universe_id"] is not None
        for formula in formulas
    ):
        raise IncompleteReplayInput("metric formula universe reference is invalid")
    if _valid_digest(fixture["formula_digest"], "metric formula") != _digest(formulas):
        raise IncompleteReplayInput("metric formula digest does not match")

    operation_families = {
        "one_minus_max_historical_similarity": "novelty",
        "max_historical_similarity": "historical_similarity",
        "family_rate_ratio_mean": "velocity",
        "family_rate_delta_mean": "velocity",
        "combined_saturation": "breadth",
        "mean_saturation": "breadth",
        "one_minus_hhi": "independence",
        "one_minus_max_share": "independence",
        "all_receipt_coverage": "geo_coverage",
        "qualifying_receipt_coverage": "geo_coverage",
    }
    if any(
        operation_families.get(formula["operation"]) != formula["metric_family"]
        for formula in formulas
    ):
        raise IncompleteReplayInput("metric formula operation is invalid")

    historical_similarity, historical_ids = _historical_similarity(fixture["historical"], cutoff)
    counts_by_family: dict[str, list[float]] = {}
    for row in qualifying:
        values = counts_by_family.setdefault(str(row["source_family"]), [0.0, 0.0])
        values[0] += float(row["current_count"])
        values[1] += float(row["history_count"])
    active_families = {str(row["source_family"]) for row in qualifying if row["current_count"] > 0}
    active_platforms = {str(row["platform"]) for row in qualifying if row["current_count"] > 0}
    total_qualifying = sum(float(row["current_count"]) for row in qualifying)
    family_counts = tuple(
        sum(values[0] for family, values in counts_by_family.items() if family == key)
        for key in sorted(counts_by_family)
    )

    results = []
    for formula in formulas:
        if not current_window["complete_partitions"]:
            results.append(
                _missing_formula_result(
                    formula,
                    current_window,
                    history_window,
                    all_row_ids,
                    "incomplete_current_window",
                )
            )
            continue
        if not history_window["complete_partitions"]:
            results.append(
                _missing_formula_result(
                    formula,
                    current_window,
                    history_window,
                    all_row_ids,
                    "incomplete_history_window",
                )
            )
            continue
        operation = formula["operation"]
        if not qualifying and formula["metric_family"] in {
            "velocity",
            "breadth",
            "independence",
        }:
            results.append(
                _missing_formula_result(
                    formula,
                    current_window,
                    history_window,
                    all_row_ids,
                    "no_qualifying_evidence",
                )
            )
            continue
        if operation in {"one_minus_max_historical_similarity", "max_historical_similarity"}:
            if historical_similarity is None:
                results.append(
                    _missing_formula_result(
                        formula, current_window, history_window, historical_ids, "missing_history"
                    )
                )
            else:
                numerator = (
                    1 - historical_similarity
                    if operation == "one_minus_max_historical_similarity"
                    else historical_similarity
                )
                results.append(
                    _measured_formula_result(
                        formula, current_window, history_window, historical_ids, numerator, 1.0
                    )
                )
        elif operation in {"family_rate_ratio_mean", "family_rate_delta_mean"}:
            if not counts_by_family:
                results.append(
                    _missing_formula_result(
                        formula,
                        current_window,
                        history_window,
                        qualifying_ids or all_row_ids,
                        "no_qualifying_families",
                    )
                )
                continue
            current_days = 7.0
            history_days = 28.0
            prior_rates = [values[1] / history_days for values in counts_by_family.values()]
            if any(rate <= 0 for rate in prior_rates):
                results.append(
                    _missing_formula_result(
                        formula, current_window, history_window, qualifying_ids, "zero_denominator"
                    )
                )
                continue
            current_rates = [values[0] / current_days for values in counts_by_family.values()]
            values = (
                [current / prior for current, prior in zip(current_rates, prior_rates, strict=True)]
                if operation == "family_rate_ratio_mean"
                else [
                    current - prior
                    for current, prior in zip(current_rates, prior_rates, strict=True)
                ]
            )
            results.append(
                _measured_formula_result(
                    formula,
                    current_window,
                    history_window,
                    qualifying_ids,
                    sum(values),
                    float(len(values)),
                )
            )
        elif operation in {"combined_saturation", "mean_saturation"}:
            universe = universes[formula["universe_id"]]
            saturation_families = tuple(universe["source_families"])
            saturation_platforms = tuple(universe["platforms"])
            if operation == "combined_saturation":
                numerator = float(len(active_families) + len(active_platforms))
                denominator = float(len(saturation_families) + len(saturation_platforms))
            else:
                if not saturation_families or not saturation_platforms:
                    numerator, denominator = 0.0, 0.0
                else:
                    numerator = len(active_families) / len(saturation_families) + len(
                        active_platforms
                    ) / len(saturation_platforms)
                    denominator = 2.0
            results.append(
                _measured_formula_result(
                    formula,
                    current_window,
                    history_window,
                    qualifying_ids or all_row_ids,
                    numerator,
                    denominator,
                )
            )
        elif operation in {"one_minus_hhi", "one_minus_max_share"}:
            if not counts_by_family or total_qualifying <= 0:
                results.append(
                    _missing_formula_result(
                        formula,
                        current_window,
                        history_window,
                        qualifying_ids or all_row_ids,
                        "no_qualifying_families",
                    )
                )
                continue
            denominator = total_qualifying**2 if operation == "one_minus_hhi" else total_qualifying
            numerator = (
                denominator - sum(count**2 for count in family_counts)
                if operation == "one_minus_hhi"
                else denominator - max(family_counts)
            )
            results.append(
                _measured_formula_result(
                    formula,
                    current_window,
                    history_window,
                    qualifying_ids,
                    numerator,
                    denominator,
                )
            )
        else:
            selected = rows if operation == "all_receipt_coverage" else qualifying
            selected_ids = tuple(str(row["row_id"]) for row in selected) or all_row_ids
            geo_count = sum(row["geo_provenance"] is not None for row in selected)
            if not selected or geo_count == 0:
                results.append(
                    _missing_formula_result(
                        formula,
                        current_window,
                        history_window,
                        selected_ids,
                        "geo_provenance_unavailable",
                    )
                )
            else:
                results.append(
                    _measured_formula_result(
                        formula,
                        current_window,
                        history_window,
                        selected_ids,
                        float(geo_count),
                        float(len(selected)),
                    )
                )
    return ReplayMetricComparison(tuple(results))


def _coverage_record(row_ids: object) -> CoverageRecord:
    ordered = tuple(sorted(set(row_ids)))
    return CoverageRecord(len(ordered), ordered)


def _optional_coverage_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    return _nonnegative_number(value, field)


def _optional_signed_unit(value: object, field: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not -1 <= float(value) <= 1
    ):
        raise IncompleteReplayInput(f"{field} must be finite from negative one to one")
    return float(value)


def probe_replay_geo_direction_coverage(value: object) -> ReplayCoverageArtifact:
    fixture = _mapping(value, "coverage fixture")
    _exact_fields(fixture, _COVERAGE_FIXTURE_FIELDS, "coverage fixture")
    if (
        fixture["fixture_scope"] != "synthetic_test_only"
        or fixture["artifact_version"] != "replay_geo_direction_coverage_v1"
    ):
        raise IncompleteReplayInput("coverage fixture identity is invalid")
    cutoff = _timestamp(fixture["cutoff"], "coverage cutoff")
    raw_rows = tuple(
        sorted(
            (
                _mapping(item, "coverage row")
                for item in _sequence(fixture["rows"], "coverage rows")
            ),
            key=lambda row: str(row.get("row_id")),
        )
    )
    canonical_fixture = dict(fixture)
    canonical_fixture["rows"] = raw_rows
    canonical_fixture.pop("fixture_digest")
    if _valid_digest(fixture["fixture_digest"], "coverage fixture") != _digest(canonical_fixture):
        raise IncompleteReplayInput("coverage fixture digest does not match")
    proposals = tuple(
        _sequence(
            fixture["required_upstream_evidence_fields"],
            "required upstream evidence fields",
        )
    )
    if proposals != _REQUIRED_COVERAGE_PROPOSALS:
        raise IncompleteReplayInput("required upstream evidence field proposals are invalid")

    explicit_geo = []
    unresolved_geo = []
    locations = []
    direction_eligible = []
    direction_conflicts = []
    row_ids = []
    for row in raw_rows:
        _exact_fields(row, _COVERAGE_ROW_FIELDS, "coverage row")
        row_id = row["row_id"]
        if not isinstance(row_id, str) or not row_id or row_id in row_ids:
            raise IncompleteReplayInput("coverage row ID is invalid or duplicate")
        row_ids.append(row_id)
        if row["market"] not in MARKETS:
            raise IncompleteReplayInput("coverage market is invalid")
        if not isinstance(row["query_group"], str) or not row["query_group"]:
            raise IncompleteReplayInput("coverage query group is invalid")
        for field in (
            "v2locations",
            "geo_evidence_id",
            "geo_provenance",
            "current_observed_at",
            "prior_observed_at",
        ):
            if row[field] is not None and (not isinstance(row[field], str) or not row[field]):
                raise IncompleteReplayInput(f"coverage {field} is invalid")
        for field in (
            "regional_score",
            "engagement_total",
            "current_count",
            "prior_count",
            "current_rate",
            "prior_rate",
        ):
            _optional_coverage_number(row[field], f"coverage {field}")
        for field in ("sentiment_lexicon_score", "tone_polarity"):
            _optional_signed_unit(row[field], f"coverage {field}")

        current_at = (
            _timestamp(row["current_observed_at"], "current observed at")
            if row["current_observed_at"] is not None
            else None
        )
        prior_at = (
            _timestamp(row["prior_observed_at"], "prior observed at")
            if row["prior_observed_at"] is not None
            else None
        )
        if any(item is not None and item > cutoff for item in (prior_at, current_at)):
            raise FutureLeakageDetected("future coverage timestamp")
        if current_at is not None and prior_at is not None and prior_at >= current_at:
            raise IncompleteReplayInput("coverage temporal order is invalid")

        has_explicit_geo = bool(row["geo_evidence_id"] and row["geo_provenance"])
        (explicit_geo if has_explicit_geo else unresolved_geo).append(row_id)
        if row["v2locations"]:
            locations.append(row_id)

        temporal_fields = (
            row["current_observed_at"],
            row["prior_observed_at"],
            row["current_count"],
            row["prior_count"],
            row["current_rate"],
            row["prior_rate"],
        )
        if all(item is not None for item in temporal_fields):
            direction_eligible.append(row_id)
            count_delta = float(row["current_count"]) - float(row["prior_count"])
            rate_delta = float(row["current_rate"]) - float(row["prior_rate"])
            if count_delta * rate_delta < 0:
                direction_conflicts.append(row_id)
    return ReplayCoverageArtifact(
        artifact_version="replay_geo_direction_coverage_v1",
        total_projected_rows=_coverage_record(row_ids),
        explicit_geo_evidence_rows=_coverage_record(explicit_geo),
        unresolved_geo_rows=_coverage_record(unresolved_geo),
        factual_location_field_presence=_coverage_record(locations),
        direction_eligible_rows=_coverage_record(direction_eligible),
        direction_conflict_rows=_coverage_record(direction_conflicts),
        unavailable_producer_reasons=(
            "direction_producer_unavailable",
            "geo_provenance_unavailable",
        ),
        required_upstream_evidence_fields=proposals,
    )


def grid_fixture_digest(value: object) -> str:
    fixture = _mapping(value, "grid fixture")
    canonical = dict(fixture)
    canonical.pop("fixture_digest", None)
    canonical["observations"] = tuple(
        sorted(canonical["observations"], key=lambda row: (row["market"], row["term"]))
    )
    providers = []
    for raw in canonical["semantic_providers"]:
        provider = dict(raw)
        provider["matrix"] = tuple(
            sorted(provider["matrix"], key=lambda row: (row["left"], row["right"]))
        )
        providers.append(provider)
    canonical["semantic_providers"] = tuple(sorted(providers, key=lambda row: row["candidate_id"]))
    for field in (
        "graph_rule_candidates",
        "readiness_rule_candidates",
        "component_reviews",
        "known_events",
    ):
        identity = (
            "signal_id"
            if field == "component_reviews"
            else ("event_id" if field == "known_events" else "candidate_id")
        )
        canonical[field] = tuple(sorted(canonical[field], key=lambda row: row[identity]))
    cases = []
    for raw in canonical["readiness_cases"]:
        case = dict(raw)
        case["records"] = tuple(sorted(case["records"], key=lambda row: row["row_id"]))
        cases.append(case)
    canonical["readiness_cases"] = tuple(sorted(cases, key=lambda row: row["case_id"]))
    return _digest(canonical)


def _grid_text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise IncompleteReplayInput(f"grid {field} is invalid")
    return value


def _grid_metric(numerator: int, denominator: int) -> Metric:
    return Metric(numerator, denominator, numerator / denominator)


def evaluate_replay_graph_readiness_grid(value: object) -> ReplayGridArtifact:
    fixture = _mapping(value, "grid fixture")
    _exact_fields(fixture, _GRID_FIXTURE_FIELDS, "grid fixture")
    if (
        fixture["fixture_scope"] != "synthetic_test_only"
        or fixture["artifact_version"] != "replay_graph_readiness_grid_v1"
    ):
        raise IncompleteReplayInput("grid fixture identity is invalid")
    if _valid_digest(fixture["fixture_digest"], "grid fixture") != grid_fixture_digest(fixture):
        raise IncompleteReplayInput("grid fixture digest does not match")
    cutoff = _date_value(fixture["cutoff"], "grid cutoff")
    observations = _semantic_observations(fixture["observations"])

    providers = []
    for raw in _sequence(fixture["semantic_providers"], "grid providers"):
        provider = _mapping(raw, "grid provider")
        _exact_fields(provider, _GRID_PROVIDER_FIELDS, "grid provider")
        candidate_id = _grid_text(provider, "candidate_id")
        version = _grid_text(provider, "version")
        runtime = _nonnegative_number(provider["runtime_ms"], "grid provider runtime")
        matrix = _matrix_rows(provider["matrix"])
        providers.append((candidate_id, version, runtime, matrix))
    providers.sort()

    graph_candidates = []
    for raw in _sequence(fixture["graph_rule_candidates"], "graph candidates"):
        row = _mapping(raw, "graph candidate")
        _exact_fields(row, _GRID_GRAPH_FIELDS, "graph candidate")
        graph_candidates.append(
            (
                _grid_text(row, "candidate_id"),
                _grid_text(row, "version"),
                _nonnegative_number(row["runtime_ms"], "graph runtime"),
                GraphRules(
                    row["semantic_vote_floor"],
                    row["component_similarity_floor"],
                    row["temporal_overlap_days"],
                ),
            )
        )
    graph_candidates.sort(key=lambda item: item[0])

    readiness_candidates = []
    for raw in _sequence(fixture["readiness_rule_candidates"], "readiness candidates"):
        row = _mapping(raw, "readiness candidate")
        _exact_fields(row, _GRID_READINESS_FIELDS, "readiness candidate")
        readiness_candidates.append(
            (
                _grid_text(row, "candidate_id"),
                _grid_text(row, "version"),
                _nonnegative_number(row["runtime_ms"], "readiness runtime"),
                ReadinessRules(
                    _timestamp(row["current_cutoff"], "readiness cutoff"),
                    row["minimum_geo_confidence"],
                ),
            )
        )
    readiness_candidates.sort(key=lambda item: item[0])

    reviews = []
    for raw in _sequence(fixture["component_reviews"], "component reviews"):
        row = _mapping(raw, "component review")
        _exact_fields(row, _GRID_REVIEW_FIELDS, "component review")
        members = tuple(_sequence(row["component_members"], "component members"))
        if tuple(sorted(set(members))) != members:
            raise IncompleteReplayInput("component review members are invalid")
        for field in ("membership_complete", "evidence_ready", "geo_proven"):
            if type(row[field]) is not bool:
                raise IncompleteReplayInput(f"component review {field} is invalid")
        reviews.append((row, members))
    reviews.sort(key=lambda item: item[0]["signal_id"])

    known_events = tuple(
        KnownEvent(
            _grid_text(_mapping(raw, "known event"), "event_id"),
            _grid_text(_mapping(raw, "known event"), "market"),
            tuple(_mapping(raw, "known event")["member_identities"]),
        )
        for raw in _sequence(fixture["known_events"], "known events")
    )

    cases = []
    for raw in _sequence(fixture["readiness_cases"], "readiness cases"):
        case = _mapping(raw, "readiness case")
        _exact_fields(case, _GRID_CASE_FIELDS, "readiness case")
        records = []
        for evidence_raw in _sequence(case["records"], "readiness records"):
            evidence = _mapping(evidence_raw, "readiness record")
            _exact_fields(evidence, _GRID_EVIDENCE_FIELDS, "readiness record")
            records.append(
                EvidenceRecord(
                    row_id=evidence["row_id"],
                    source_family=evidence["source_family"],
                    direction=evidence["direction"],
                    published_at=(
                        _timestamp(evidence["published_at"], "readiness published at")
                        if evidence["published_at"] is not None
                        else None
                    ),
                    availability=evidence["availability"],
                    geo_confidence=evidence["geo_confidence"],
                    factual_conflict=evidence["factual_conflict"],
                )
            )
        cases.append((case, tuple(records)))
    cases.sort(key=lambda item: item[0]["case_id"])

    task5c = _mapping(fixture["task5c_contract"], "Task 5C grid contract")
    _exact_fields(task5c, _GRID_TASK5C_FIELDS, "Task 5C grid contract")
    if (
        task5c["qualifies_evidence"] is not False
        or task5c["current_window_complete"] is not False
        or task5c["history_window_complete"] is not False
    ):
        raise IncompleteReplayInput("Task 5C grid facts cannot be promoted")
    missing_work = tuple(task5c["missing_work"])
    if missing_work != _APPROVED_GRID_MISSING_WORK:
        raise IncompleteReplayInput("exact approved Task 5C missing work is required")

    results = []
    for provider_id, provider_version, provider_runtime, matrix in providers:
        scores = {(row["left"], row["right"]): row["score"] for row in matrix}
        provider = _FrozenSemanticProvider(provider_version, MappingProxyType(scores))
        for graph_id, _graph_version, graph_runtime, graph_rules in graph_candidates:
            components = build_components_with_validated_semantics(
                observations, provider, graph_rules
            )
            by_members = {component.member_identities: component for component in components}
            signals = []
            for review, members in reviews:
                if members not in by_members:
                    raise IncompleteReplayInput("review component is absent from graph candidate")
                signals.append(
                    ReplaySignal(
                        signal_id=review["signal_id"],
                        market=review["review_market"],
                        cluster_signature=_digest(members),
                        member_identities=members,
                        source_max_observed_at=datetime.combine(cutoff, time.max, tzinfo=UTC),
                        membership_complete=review["membership_complete"],
                        evidence_ready=review["evidence_ready"],
                        geo_proven=review["geo_proven"],
                    )
                )
            replay_metrics = evaluate_replay(
                cutoff=cutoff,
                input_row_count=fixture["input_row_count"],
                signals=tuple(signals),
                known_events=known_events,
                sample_per_market=1,
            )
            for (
                readiness_id,
                _readiness_version,
                readiness_runtime,
                readiness_rules,
            ) in readiness_candidates:
                states = []
                for case, records in cases:
                    states.append(
                        evaluate_readiness(
                            records,
                            readiness_rules,
                            quality_evaluated=case["quality_evaluated"],
                            quality_failed=case["quality_failed"],
                            factual_conflict=case["factual_conflict"],
                        ).state
                    )
                denominator = len(states)
                candidate_id = f"{provider_id}|{graph_id}|{readiness_id}"
                results.append(
                    ReplayGridCandidateResult(
                        candidate_id=candidate_id,
                        provider_candidate_id=provider_id,
                        graph_rule_candidate_id=graph_id,
                        readiness_rule_candidate_id=readiness_id,
                        known_event_recall=replay_metrics.known_event_recall,
                        duplicate_clusters=replay_metrics.duplicate_rate,
                        foreign_leakage=replay_metrics.foreign_leakage,
                        membership_completeness=replay_metrics.receipt_completeness,
                        evidence_coverage=replay_metrics.evidence_coverage,
                        geo_coverage=replay_metrics.geo_coverage,
                        ready=_grid_metric(states.count("ready"), denominator),
                        thin=_grid_metric(states.count("thin"), denominator),
                        contradictory=_grid_metric(states.count("contradictory"), denominator),
                        unchecked=_grid_metric(states.count("unchecked"), denominator),
                        missing_work=missing_work,
                        runtime_ms=provider_runtime + graph_runtime + readiness_runtime,
                        status="provisional",
                        display_eligible=False,
                    )
                )
    return ReplayGridArtifact(tuple(sorted(results, key=lambda item: item.candidate_id)))


def lineage_fixture_digest(value: object) -> str:
    fixture = _mapping(value, "lineage fixture")
    canonical = dict(fixture)
    canonical.pop("fixture_digest", None)
    canonical["candidate_rows"] = tuple(
        sorted(
            canonical["candidate_rows"],
            key=lambda row: (
                row["market"],
                row["signal_date"],
                row["signal_id"],
                row["run_id"],
            ),
        )
    )
    canonical["membership_rows"] = tuple(
        sorted(
            canonical["membership_rows"],
            key=lambda row: (
                row["market"],
                row["signal_date"],
                row["signal_id"],
                row["member_id"],
            ),
        )
    )
    return _digest(canonical)


def _lineage_unavailable() -> ReplayLineageArtifact:
    return ReplayLineageArtifact(
        prior_snapshots=(),
        current_snapshots=(),
        lineage_rows=(),
        missing_work=("lineage_membership_unavailable",),
    )


def _lineage_scope_values(row: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(
        tuple(row[field]) if field in {"market_scope", "audience_lens_ids"} else row[field]
        for field in _LINEAGE_SCOPE_FIELDS
    )


def _lineage_snapshot_key(row: Mapping[str, object]) -> tuple[object, ...]:
    return (
        row["market"],
        row["signal_id"],
        row["signal_date"],
        row["run_id"],
    )


def evaluate_replay_lineage_fixture(value: object) -> ReplayLineageArtifact:
    try:
        fixture = _mapping(value, "lineage fixture")
        _exact_fields(fixture, _LINEAGE_FIXTURE_FIELDS, "lineage fixture")
        if (
            fixture["fixture_scope"] != "synthetic_test_only"
            or fixture["artifact_version"] != "replay_lineage_reconstruction_v1"
        ):
            raise IncompleteReplayInput("lineage fixture identity is invalid")
        if _valid_digest(fixture["fixture_digest"], "lineage fixture") != lineage_fixture_digest(
            fixture
        ):
            raise IncompleteReplayInput("lineage fixture digest does not match")
        scope_row = _mapping(fixture["scope"], "lineage scope")
        _exact_fields(scope_row, _LINEAGE_SCOPE_FIELDS, "lineage scope")
        scope = ResolvedScope(
            client_scope_id=scope_row["client_scope_id"],
            market_scope=tuple(scope_row["market_scope"]),
            brand_config_id=scope_row["brand_config_id"],
            audience_lens_ids=tuple(scope_row["audience_lens_ids"]),
            theme_id=scope_row["theme_id"],
            run_id=scope_row["run_id"],
            contract_version=scope_row["contract_version"],
        )
        expected_scope = _lineage_scope_values(scope_row)
        current_date = _date_value(fixture["current_signal_date"], "current signal date")
        created_at = _timestamp(fixture["created_at"], "lineage created at")
        lookback = fixture["lookback_days"]
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback != 365:
            raise IncompleteReplayInput("lineage lookback must be 365 days")
        rule_version = fixture["rule_version"]
        if not isinstance(rule_version, str) or not rule_version:
            raise IncompleteReplayInput("lineage rule version is invalid")
        prior_signal_ids = _metric_strings(fixture["prior_signal_ids"], "prior signal IDs")
        current_signal_ids = _metric_strings(fixture["current_signal_ids"], "current signal IDs")
        if set(prior_signal_ids).intersection(current_signal_ids):
            raise IncompleteReplayInput("lineage snapshot roles overlap")
        rule_values = _mapping(fixture["lineage_rules"], "lineage rules")
        _exact_fields(rule_values, _LINEAGE_RULE_FIELDS, "lineage rules")
        rules = LineageRules(**rule_values)

        candidates = []
        candidate_by_key = {}
        for raw in _sequence(fixture["candidate_rows"], "lineage candidates"):
            row = _mapping(raw, "lineage candidate")
            _exact_fields(row, _LINEAGE_CANDIDATE_FIELDS, "lineage candidate")
            if _lineage_scope_values(row) != expected_scope:
                raise IncompleteReplayInput("lineage candidate scope is inconsistent")
            if (
                str(row["contract_version"]).split(".", 1)[0]
                != scope.contract_version.split(".", 1)[0]
            ):
                raise IncompleteReplayInput("lineage contract major is inconsistent")
            if row["rule_version"] != rule_version:
                raise IncompleteReplayInput("lineage rule version is inconsistent")
            signal_date = _date_value(row["signal_date"], "candidate signal date")
            retention = _date_value(row["retention_until"], "candidate retention date")
            if signal_date > current_date or retention < current_date:
                raise IncompleteReplayInput("lineage candidate date or retention is invalid")
            if (row["signal_id"] in prior_signal_ids and signal_date >= current_date) or (
                row["signal_id"] in current_signal_ids and signal_date != current_date
            ):
                raise IncompleteReplayInput("lineage snapshot role date is invalid")
            if signal_date < current_date and (current_date - signal_date).days > lookback:
                raise IncompleteReplayInput("lineage candidate is outside lookback")
            if row["market"] not in scope.market_scope:
                raise IncompleteReplayInput("lineage candidate market is outside scope")
            if not isinstance(row["label"], str) or not row["label"]:
                raise IncompleteReplayInput("lineage candidate label is invalid")
            member_count = row["member_count"]
            if (
                isinstance(member_count, bool)
                or not isinstance(member_count, int)
                or member_count < 1
            ):
                raise IncompleteReplayInput("lineage candidate member count is invalid")
            normalized = dict(row)
            normalized["signal_date"] = signal_date
            normalized["retention_until"] = retention
            key = _lineage_snapshot_key(normalized)
            if key in candidate_by_key:
                raise IncompleteReplayInput("duplicate lineage candidate identity")
            candidate_by_key[key] = normalized
            candidates.append(normalized)

        if {row["signal_id"] for row in candidates} != {
            *prior_signal_ids,
            *current_signal_ids,
        }:
            raise IncompleteReplayInput("lineage snapshot roles are incomplete")

        memberships_by_key: dict[tuple[object, ...], list[dict[str, object]]] = {}
        seen_member_ids = set()
        seen_member_identities = set()
        for raw in _sequence(fixture["membership_rows"], "lineage memberships"):
            row = _mapping(raw, "lineage membership")
            _exact_fields(row, _LINEAGE_MEMBERSHIP_FIELDS, "lineage membership")
            if _lineage_scope_values(row) != expected_scope:
                raise IncompleteReplayInput("lineage membership scope is inconsistent")
            if (
                str(row["contract_version"]).split(".", 1)[0]
                != scope.contract_version.split(".", 1)[0]
            ):
                raise IncompleteReplayInput("lineage membership contract major is inconsistent")
            if row["rule_version"] != rule_version:
                raise IncompleteReplayInput("lineage membership rule version is inconsistent")
            signal_date = _date_value(row["signal_date"], "membership signal date")
            retention = _date_value(row["retention_until"], "membership retention date")
            normalized = dict(row)
            normalized["signal_date"] = signal_date
            normalized["retention_until"] = retention
            key = _lineage_snapshot_key(normalized)
            candidate = candidate_by_key.get(key)
            if candidate is None or retention < current_date:
                raise IncompleteReplayInput(
                    "lineage membership natural key or retention is invalid"
                )
            for field in (*_LINEAGE_SCOPE_FIELDS, "rule_version", "signal_id", "market"):
                candidate_value = (
                    tuple(candidate[field])
                    if field in {"market_scope", "audience_lens_ids"}
                    else candidate[field]
                )
                member_value = (
                    tuple(normalized[field])
                    if field in {"market_scope", "audience_lens_ids"}
                    else normalized[field]
                )
                if candidate_value != member_value:
                    raise IncompleteReplayInput("lineage membership conflicts with candidate")
            if retention != candidate["retention_until"]:
                raise IncompleteReplayInput("lineage retention conflicts with candidate")
            member_id = row["member_id"]
            member_identity = row["member_identity"]
            global_member_key = (key, member_id)
            global_identity_key = (key, member_identity)
            if (
                global_member_key in seen_member_ids
                or global_identity_key in seen_member_identities
            ):
                raise IncompleteReplayInput("duplicate or conflicting lineage membership")
            seen_member_ids.add(global_member_key)
            seen_member_identities.add(global_identity_key)
            if type(row["qualifies_evidence"]) is not bool:
                raise IncompleteReplayInput("lineage membership qualification is invalid")
            expected_identity = f"{row['market']}|{row['candidate_type']}|{row['canonical_value']}"
            if member_identity != expected_identity:
                raise IncompleteReplayInput("lineage membership identity facts are inconsistent")
            _metric_strings(row["source_families"], "lineage source families")
            _metric_strings(row["platforms"], "lineage platforms")
            for field in ("member_id", "candidate_type", "canonical_value", "row_id"):
                if not isinstance(row[field], str) or not row[field]:
                    raise IncompleteReplayInput(f"lineage membership {field} is invalid")
            memberships_by_key.setdefault(key, []).append(normalized)

        snapshots = []
        for candidate in candidates:
            key = _lineage_snapshot_key(candidate)
            memberships = tuple(
                sorted(memberships_by_key.get(key, ()), key=lambda row: row["member_identity"])
            )
            if len(memberships) != candidate["member_count"]:
                raise IncompleteReplayInput("lineage membership set is incomplete")
            label_memberships = tuple(
                row for row in memberships if row["canonical_value"] == candidate["label"]
            )
            if not label_memberships:
                # Legacy lineage fixtures predate label_member_identity and use
                # narrative labels. They remain fixture-only and select the
                # existing deterministic first member; live R3 rows never use
                # this adapter path.
                label_memberships = (min(memberships, key=lambda row: row["member_identity"]),)
            if len(label_memberships) != 1:
                raise IncompleteReplayInput("lineage label member identity is ambiguous")
            component = SignalComponent(
                market=candidate["market"],
                member_identities=tuple(row["member_identity"] for row in memberships),
                terms=tuple(sorted({row["canonical_value"] for row in memberships})),
                row_receipts=tuple(sorted({row["row_id"] for row in memberships})),
                source_families=tuple(
                    sorted({item for row in memberships for item in row["source_families"]})
                ),
                platforms=tuple(sorted({item for row in memberships for item in row["platforms"]})),
                creator_ids=tuple(
                    sorted(
                        {
                            row["canonical_value"]
                            for row in memberships
                            if row["candidate_type"] == "creator"
                        }
                    )
                ),
                label=candidate["label"],
                label_member_identity=label_memberships[0]["member_identity"],
            )
            snapshots.append(
                SignalSnapshot(candidate["signal_id"], candidate["signal_date"], component)
            )
        if set(memberships_by_key) != set(candidate_by_key):
            raise IncompleteReplayInput("lineage membership snapshot identity is inconsistent")
        prior = tuple(
            sorted(
                (item for item in snapshots if item.signal_date < current_date),
                key=lambda item: item.signal_id,
            )
        )
        current = tuple(
            sorted(
                (item for item in snapshots if item.signal_date == current_date),
                key=lambda item: item.signal_id,
            )
        )
        if not prior or not current:
            raise IncompleteReplayInput("lineage prior and current snapshots are required")
        rows = build_signal_lineage_rows(
            prior_signals=prior,
            current_signals=current,
            scope=scope,
            signal_date=current_date,
            created_at=created_at,
            rules=rules,
        )
        return ReplayLineageArtifact(prior, current, rows, ())
    except (IncompleteReplayInput, KeyError, TypeError, ValueError):
        return _lineage_unavailable()


def trajectory_fixture_digest(value: object) -> str:
    fixture = _mapping(value, "trajectory fixture")
    canonical = dict(fixture)
    canonical.pop("fixture_digest", None)
    canonical["metric_rows"] = tuple(
        sorted(canonical["metric_rows"], key=lambda row: row["metric_id"])
    )
    return _digest(canonical)


def _trajectory_rule(value: object) -> TrajectoryRule:
    row = _mapping(value, "trajectory rule")
    _exact_fields(row, _TRAJECTORY_RULE_FIELDS, "trajectory rule")
    name = row["trajectory_name"]
    if not isinstance(name, str) or not name:
        raise IncompleteReplayInput("trajectory name is invalid")
    values = {
        field: _bounded_unit(row[field], f"trajectory {field}")
        for field in (
            "minimum_velocity",
            "minimum_breadth",
            "target_velocity",
            "target_breadth",
            "invalidation_threshold",
        )
    }
    invalidation_metric = row["invalidation_metric"]
    if invalidation_metric not in {"velocity", "breadth"}:
        raise IncompleteReplayInput("trajectory invalidation metric is invalid")
    return TrajectoryRule(
        trajectory_name=name,
        minimum_velocity=values["minimum_velocity"],
        minimum_breadth=values["minimum_breadth"],
        target_velocity=values["target_velocity"],
        target_breadth=values["target_breadth"],
        invalidation_metric=invalidation_metric,
        invalidation_threshold=values["invalidation_threshold"],
    )


def compare_replay_trajectory_candidates(value: object) -> ReplayTrajectoryArtifact:
    fixture = _mapping(value, "trajectory fixture")
    _exact_fields(fixture, _TRAJECTORY_FIXTURE_FIELDS, "trajectory fixture")
    if (
        fixture["fixture_scope"] != "synthetic_test_only"
        or fixture["artifact_version"] != "replay_trajectory_candidates_v1"
    ):
        raise IncompleteReplayInput("trajectory fixture identity is invalid")
    if _valid_digest(fixture["fixture_digest"], "trajectory fixture") != trajectory_fixture_digest(
        fixture
    ):
        raise IncompleteReplayInput("trajectory fixture digest does not match")
    evaluation_date = _date_value(fixture["evaluation_date"], "trajectory evaluation date")
    cutoff = datetime.combine(evaluation_date, time.max, tzinfo=UTC)

    metrics = []
    for raw in _sequence(fixture["metric_rows"], "trajectory metrics"):
        row = _mapping(raw, "trajectory metric")
        _exact_fields(row, _TRAJECTORY_METRIC_FIELDS, "trajectory metric")
        metric_id = row["metric_id"]
        if not isinstance(metric_id, str) or not metric_id:
            raise IncompleteReplayInput("trajectory metric ID is invalid")
        observed_at = _timestamp(row["observed_at"], "trajectory observed at")
        if observed_at > cutoff:
            raise FutureLeakageDetected("future trajectory metric")
        velocity = (
            None
            if row["velocity"] is None
            else _bounded_unit(row["velocity"], "trajectory velocity")
        )
        breadth = (
            None if row["breadth"] is None else _bounded_unit(row["breadth"], "trajectory breadth")
        )
        missing_work = tuple(_sequence(row["missing_work"], "trajectory missing work"))
        if tuple(sorted(set(missing_work))) != missing_work or any(
            not isinstance(item, str) or not item for item in missing_work
        ):
            raise IncompleteReplayInput("trajectory metric missing work is invalid")
        if (velocity is None or breadth is None) != bool(missing_work):
            raise IncompleteReplayInput("trajectory metric availability is inconsistent")
        metrics.append((metric_id, velocity, breadth, missing_work))
    metrics.sort(key=lambda item: item[0])
    if len({item[0] for item in metrics}) != len(metrics) or not metrics:
        raise IncompleteReplayInput("trajectory metric identities are invalid")

    candidates = tuple(
        _mapping(item, "trajectory candidate")
        for item in _sequence(fixture["candidates"], "trajectory candidates")
    )
    candidate_ids = tuple(row.get("candidate_id") for row in candidates)
    if tuple(sorted(candidate_ids)) != candidate_ids or len(set(candidate_ids)) != len(
        candidate_ids
    ):
        raise IncompleteReplayInput("trajectory candidate order is invalid")
    results = []
    baseline_count = 0
    for candidate in candidates:
        _exact_fields(candidate, _TRAJECTORY_CANDIDATE_FIELDS, "trajectory candidate")
        candidate_id = _grid_text(candidate, "candidate_id")
        version = _grid_text(candidate, "version")
        source = _grid_text(candidate, "source")
        if source not in {"predictions_py_baseline", "fixture_candidate"}:
            raise IncompleteReplayInput("trajectory candidate source is invalid")
        evaluation_days = candidate["evaluation_days"]
        if (
            isinstance(evaluation_days, bool)
            or not isinstance(evaluation_days, int)
            or evaluation_days < 1
        ):
            raise IncompleteReplayInput("trajectory evaluation days are invalid")
        raw_rules = tuple(_sequence(candidate["rules"], "trajectory rules"))
        rules_digest = _valid_digest(candidate["rules_digest"], "trajectory rules")
        if rules_digest != _digest(raw_rules):
            raise IncompleteReplayInput("trajectory rules digest does not match")
        baseline_identity = (
            candidate_id == APPROVED_PREDICTIONS_BASELINE_METADATA["candidate_id"]
            or source == APPROVED_PREDICTIONS_BASELINE_METADATA["source"]
        )
        if baseline_identity:
            baseline_count += 1
            observed_metadata = {
                "candidate_id": candidate_id,
                "version": version,
                "source": source,
                "evaluation_days": evaluation_days,
                "rules_digest": rules_digest,
            }
            if observed_metadata != dict(APPROVED_PREDICTIONS_BASELINE_METADATA):
                raise IncompleteReplayInput("approved baseline metadata does not match")
        rules = tuple(_trajectory_rule(item) for item in raw_rules)
        names = tuple(rule.trajectory_name for rule in rules)
        if len(set(names)) != len(names):
            raise IncompleteReplayInput("duplicate trajectory name")
        thresholds = tuple((rule.minimum_velocity, rule.minimum_breadth) for rule in rules)
        if tuple(sorted(thresholds, reverse=True)) != thresholds or thresholds[-1] != (0.0, 0.0):
            raise IncompleteReplayInput("trajectory rule order is invalid")

        evaluations = []
        combined_missing = set()
        for metric_id, velocity, breadth, missing_work in metrics:
            if missing_work:
                combined_missing.update(missing_work)
                evaluations.append(
                    TrajectoryEvaluation(
                        metric_id,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        missing_work,
                    )
                )
                continue
            matched = next(
                (
                    rule
                    for rule in rules
                    if velocity >= rule.minimum_velocity and breadth >= rule.minimum_breadth
                ),
                None,
            )
            if matched is None:
                raise IncompleteReplayInput("trajectory candidate has no fallback rule")
            evaluations.append(
                TrajectoryEvaluation(
                    metric_id=metric_id,
                    matched_trajectory=matched.trajectory_name,
                    target_velocity=matched.target_velocity,
                    target_breadth=matched.target_breadth,
                    invalidation_metric=matched.invalidation_metric,
                    invalidation_threshold=matched.invalidation_threshold,
                    invalidation_at=(evaluation_date + timedelta(days=evaluation_days)).isoformat(),
                    missing_work=(),
                )
            )
        denominator = len(evaluations)
        missing_count = sum(bool(item.missing_work) for item in evaluations)
        results.append(
            ReplayTrajectoryCandidateResult(
                candidate_id=candidate_id,
                version=version,
                rules_digest=rules_digest,
                evaluations=tuple(evaluations),
                matched=_grid_metric(denominator - missing_count, denominator),
                missing=_grid_metric(missing_count, denominator),
                missing_work=tuple(sorted(combined_missing)),
                status="provisional",
                display_eligible=False,
            )
        )
    if baseline_count != 1:
        raise IncompleteReplayInput("exactly one predictions baseline candidate is required")
    return ReplayTrajectoryArtifact(tuple(results))


def _evaluation_semantic_fixture_digest(value: object) -> str:
    fixture = _mapping(value, "evaluation semantic fixture")
    canonical = dict(fixture)
    canonical["observations"] = tuple(
        sorted(canonical["observations"], key=lambda row: (row["market"], row["term"]))
    )
    frozen = dict(canonical["frozen_embedding"])
    frozen["matrix"] = tuple(sorted(frozen["matrix"], key=lambda row: (row["left"], row["right"])))
    canonical["frozen_embedding"] = frozen
    return _digest(canonical)


def _evaluation_metric_fixture_digest(value: object) -> str:
    fixture = _mapping(value, "evaluation metric fixture")
    canonical = dict(fixture)
    canonical["evidence_rows"] = tuple(
        sorted(canonical["evidence_rows"], key=lambda row: row["row_id"])
    )
    canonical["formulas"] = tuple(sorted(canonical["formulas"], key=lambda row: row["formula_id"]))
    canonical["saturation_universes"] = tuple(
        sorted(canonical["saturation_universes"], key=lambda row: row["universe_id"])
    )
    return _digest(canonical)


def replay_evaluation_artifact_digest(value: object) -> str:
    if isinstance(value, ReplayEvaluationArtifact):
        fields_without_digest = {
            field: getattr(value, field)
            for field in REPLAY_EVALUATION_FIELDS
            if field != "artifact_digest"
        }
    else:
        payload = _mapping(value, "replay evaluation artifact")
        fields_without_digest = {
            field: payload[field]
            for field in REPLAY_EVALUATION_FIELDS
            if field != "artifact_digest" and field in payload
        }
    return _digest(fields_without_digest)


def _validate_full_replay_denominators(results: object) -> None:
    if not isinstance(results, tuple) or not results:
        raise IncompleteReplayInput("full replay metrics are invalid")
    for result in results:
        if not isinstance(result, ReplayGridCandidateResult):
            raise IncompleteReplayInput("full replay metrics are invalid")
        if (
            result.known_event_recall.denominator != 3
            or result.duplicate_clusters.denominator != 5
            or result.foreign_leakage.denominator != 5
            or result.membership_completeness.denominator != 5
            or result.evidence_coverage.denominator != 5
            or result.geo_coverage.denominator != 5
            or result.ready.denominator != 7
            or result.thin.denominator != 7
            or result.contradictory.denominator != 7
            or result.unchecked.denominator != 7
        ):
            raise IncompleteReplayInput("full replay denominator is required")


def validate_replay_evaluation_artifact(value: object) -> ReplayEvaluationArtifact:
    payload = _mapping(value, "replay evaluation artifact")
    if tuple(payload) != REPLAY_EVALUATION_FIELDS:
        raise IncompleteReplayInput("replay evaluation artifact field order is invalid")
    if (
        payload["artifact_version"] != "replay_evaluation_v1"
        or payload["contract_version"] != "2.0.0"
        or payload["mode"] != "fixture_dry_run"
        or payload["certified"] is not False
        or payload["human_coherence_status"] != "pending"
        or payload["recommendation"] is not None
    ):
        raise IncompleteReplayInput("fixture artifact state is invalid")
    if payload["cutoff"] != REPLAY_CUTOFF:
        raise IncompleteReplayInput("approved artifact cutoff is required")
    source_snapshot = _mapping(payload["source_snapshot"], "artifact source snapshot")
    _exact_fields(source_snapshot, _EVALUATION_SOURCE_FIELDS, "artifact source snapshot")
    fixture_digests = _mapping(source_snapshot["fixture_digests"], "fixture digests")
    if fixture_digests != dict(APPROVED_REPLAY_EVALUATION_FIXTURE_DIGESTS):
        raise IncompleteReplayInput("approved fixture source snapshot is required")
    replay_source = _mapping(
        source_snapshot["replay_input_source_snapshot"], "replay input source snapshot"
    )
    if replay_source.get("source_digest") != fixture_digests["replay_source"]:
        raise IncompleteReplayInput("approved fixture source snapshot is required")
    try:
        _validated_source_snapshot(replay_source, payload["cutoff"])
    except (IncompleteReplayInput, FutureLeakageDetected) as error:
        raise IncompleteReplayInput("nested source snapshot validation failed") from error
    if not isinstance(payload["provider_comparisons"], ReplaySemanticComparison):
        raise IncompleteReplayInput("provider comparison family is invalid")
    if not isinstance(payload["metric_comparisons"], ReplayMetricComparison):
        raise IncompleteReplayInput("metric comparison family is invalid")
    if not isinstance(payload["geo_comparisons"], ReplayCoverageArtifact):
        raise IncompleteReplayInput("geo comparison family is invalid")
    if not isinstance(payload["graph_rule_comparisons"], ReplayGridArtifact):
        raise IncompleteReplayInput("graph comparison family is invalid")
    if not isinstance(payload["readiness_rule_comparisons"], ReplayGridArtifact):
        raise IncompleteReplayInput("readiness comparison family is invalid")
    if not isinstance(payload["trajectory_rule_comparisons"], ReplayTrajectoryArtifact):
        raise IncompleteReplayInput("trajectory comparison family is invalid")
    replay_metrics = _mapping(payload["replay_metrics"], "replay metrics")
    if set(replay_metrics) != {"grid_results", "lineage_evaluation"}:
        raise IncompleteReplayInput("replay metric fields are invalid")
    _validate_full_replay_denominators(replay_metrics["grid_results"])
    if replay_metrics["grid_results"] != payload["graph_rule_comparisons"].results:
        raise IncompleteReplayInput("replay metric grid results are inconsistent")
    if not isinstance(replay_metrics["lineage_evaluation"], ReplayLineageArtifact):
        raise IncompleteReplayInput("lineage replay metric is invalid")
    observed_output_digests = {
        "geo": _digest(payload["geo_comparisons"]),
        "graph": _digest(payload["graph_rule_comparisons"]),
        "lineage": _digest(replay_metrics["lineage_evaluation"]),
        "metric": _digest(payload["metric_comparisons"]),
        "provider": _digest(payload["provider_comparisons"]),
        "readiness": _digest(payload["readiness_rule_comparisons"]),
        "replay_metrics": _digest(replay_metrics),
        "trajectory": _digest(payload["trajectory_rule_comparisons"]),
    }
    if observed_output_digests != dict(APPROVED_REPLAY_EVALUATION_OUTPUT_DIGESTS):
        raise IncompleteReplayInput("pinned constituent comparison is required")
    review_sample_ids = tuple(payload["review_sample_ids"])
    if review_sample_ids != _APPROVED_REVIEW_SAMPLE_IDS:
        raise IncompleteReplayInput("review sample IDs are invalid")
    missing_work = tuple(payload["missing_work"])
    if missing_work != _APPROVED_EVALUATION_MISSING_WORK:
        raise IncompleteReplayInput("artifact missing work is invalid")
    known_event_set = _mapping(payload["known_event_set"], "artifact known event set")
    _exact_fields(known_event_set, _KNOWN_EVENT_SET_FIELDS, "artifact known event set")
    if known_event_set["fixture_scope"] != "synthetic_test_only":
        raise IncompleteReplayInput("artifact known event fixture scope is invalid")
    if not isinstance(known_event_set["set_id"], str) or not known_event_set["set_id"]:
        raise IncompleteReplayInput("artifact known event set ID is invalid")
    known_events = _sequence(known_event_set["events"], "artifact known events")
    for event in known_events:
        _exact_fields(
            _mapping(event, "artifact known event"),
            _KNOWN_EVENT_FIELDS,
            "artifact known event",
        )
    _unique(known_events, "event_id", "artifact known event")
    known_digest = _valid_digest(known_event_set["digest"], "artifact known event")
    if known_digest != _digest(known_events):
        raise IncompleteReplayInput("known event content does not match digest")
    if known_digest != "d81ef0a749e70158201360864d0ea8098c29109023115d1ac05ebc8d6b1d781b":
        raise IncompleteReplayInput("artifact known event set is invalid")
    artifact_digest = _valid_digest(payload["artifact_digest"], "replay evaluation artifact")
    if artifact_digest != replay_evaluation_artifact_digest(payload):
        raise IncompleteReplayInput("replay evaluation artifact digest does not match")
    return ReplayEvaluationArtifact(
        artifact_version=payload["artifact_version"],
        contract_version=payload["contract_version"],
        mode=payload["mode"],
        certified=payload["certified"],
        cutoff=payload["cutoff"],
        source_snapshot=_freeze(source_snapshot),  # type: ignore[arg-type]
        known_event_set=_freeze(known_event_set),  # type: ignore[arg-type]
        provider_comparisons=payload["provider_comparisons"],
        metric_comparisons=payload["metric_comparisons"],
        geo_comparisons=payload["geo_comparisons"],
        graph_rule_comparisons=payload["graph_rule_comparisons"],
        readiness_rule_comparisons=payload["readiness_rule_comparisons"],
        trajectory_rule_comparisons=payload["trajectory_rule_comparisons"],
        replay_metrics=_freeze(replay_metrics),  # type: ignore[arg-type]
        review_sample_ids=review_sample_ids,
        human_coherence_status=payload["human_coherence_status"],
        recommendation=None,
        missing_work=missing_work,
        artifact_digest=artifact_digest,
    )


def build_replay_evaluation_artifact(
    *,
    replay_input: object,
    semantic_fixture: object,
    metric_fixture: object,
    coverage_fixture: object,
    grid_fixture: object,
    lineage_fixture: object,
    trajectory_fixture: object,
) -> ReplayEvaluationArtifact:
    replay_bundle = validate_replay_input(replay_input)
    provider_comparisons = compare_replay_semantic_candidates(semantic_fixture)
    metric_comparisons = compare_replay_metric_formulas(metric_fixture)
    geo_comparisons = probe_replay_geo_direction_coverage(coverage_fixture)
    grid_comparisons = evaluate_replay_graph_readiness_grid(grid_fixture)
    lineage_evaluation = evaluate_replay_lineage_fixture(lineage_fixture)
    trajectory_comparisons = compare_replay_trajectory_candidates(trajectory_fixture)
    observed_digests = {
        "coverage": _mapping(coverage_fixture, "coverage fixture")["fixture_digest"],
        "grid": _mapping(grid_fixture, "grid fixture")["fixture_digest"],
        "lineage": _mapping(lineage_fixture, "lineage fixture")["fixture_digest"],
        "metric": _evaluation_metric_fixture_digest(metric_fixture),
        "replay_input": _digest(replay_input),
        "replay_source": replay_bundle.source_snapshot["source_digest"],
        "semantic": _evaluation_semantic_fixture_digest(semantic_fixture),
        "trajectory": _mapping(trajectory_fixture, "trajectory fixture")["fixture_digest"],
    }
    if observed_digests != dict(APPROVED_REPLAY_EVALUATION_FIXTURE_DIGESTS):
        raise IncompleteReplayInput("approved fixture source snapshot is required")
    source_snapshot = MappingProxyType(
        {
            "replay_input_source_snapshot": replay_bundle.source_snapshot,
            "fixture_digests": MappingProxyType(observed_digests),
        }
    )
    reviews = tuple(
        _mapping(item, "component review")
        for item in _sequence(
            _mapping(grid_fixture, "grid fixture")["component_reviews"], "reviews"
        )
    )
    review_by_market = {}
    for review in reviews:
        member_market = str(review["component_members"][0]).split("|", 1)[0]
        if review["review_market"] == member_market:
            review_by_market.setdefault(member_market, []).append(review["signal_id"])
    review_sample_ids = tuple(sorted(min(values) for _, values in sorted(review_by_market.items())))
    missing = set()
    for candidate in provider_comparisons.candidates:
        missing.update(candidate.missing_work)
    missing.update(geo_comparisons.unavailable_producer_reasons)
    for result in grid_comparisons.results:
        missing.update(result.missing_work)
    missing.update(lineage_evaluation.missing_work)
    for result in trajectory_comparisons.results:
        missing.update(result.missing_work)
    missing_work = tuple(sorted(missing))
    payload = {
        "artifact_version": "replay_evaluation_v1",
        "contract_version": replay_bundle.contract_version,
        "mode": "fixture_dry_run",
        "certified": False,
        "cutoff": replay_bundle.cutoff,
        "source_snapshot": source_snapshot,
        "known_event_set": replay_bundle.known_event_set,
        "provider_comparisons": provider_comparisons,
        "metric_comparisons": metric_comparisons,
        "geo_comparisons": geo_comparisons,
        "graph_rule_comparisons": grid_comparisons,
        "readiness_rule_comparisons": grid_comparisons,
        "trajectory_rule_comparisons": trajectory_comparisons,
        "replay_metrics": MappingProxyType(
            {
                "grid_results": grid_comparisons.results,
                "lineage_evaluation": lineage_evaluation,
            }
        ),
        "review_sample_ids": review_sample_ids,
        "human_coherence_status": "pending",
        "recommendation": None,
        "missing_work": missing_work,
        "artifact_digest": "0" * 64,
    }
    payload["artifact_digest"] = replay_evaluation_artifact_digest(payload)
    return validate_replay_evaluation_artifact(payload)


def normalize_replay_v3_json(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: normalize_replay_v3_json(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise IncompleteReplayInput("v3 timestamp must be timezone aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise IncompleteReplayInput("v3 mapping keys must be strings")
        normalized = {}
        for key, item in value.items():
            if isinstance(item, str) and key in {
                "captured_at",
                "collected_at",
                "published_at",
                "as_of",
            }:
                item = _timestamp(item, key).astimezone(UTC).isoformat()
            elif isinstance(item, str) and key in {
                "cutoff",
                "trend_date",
                "event_date",
                "proposed_date",
                "start_date",
                "end_date",
                "signal_date",
            }:
                item = _date_value(item, key).isoformat()
            normalized[key] = normalize_replay_v3_json(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [normalize_replay_v3_json(item) for item in value]
    _typed(value)
    return value


def _v3_source_field_schema(table, field_names):
    types = {
        "INT64": {"row_count", "source_count"},
        "FLOAT64": {
            "views",
            "likes",
            "comments",
            "shares",
            "engagement_total",
            "regional_score",
            "search_velocity_score",
            "tone_avg",
            "tone_polarity",
            "sentiment_lexicon_score",
            "confidence",
            "score",
        },
        "ARRAY<STRING>": {
            "entity_aliases",
            "corroborating_sources",
            "topic_groups",
            "near_topics",
            "co_occur_terms",
            "sample_row_ids",
            "evidence_topics",
        },
        "DATE": {"trend_date", "event_date", "proposed_date"},
        "TIMESTAMP": {"as_of", "published_at", "collected_at"},
    }
    required = {
        "event_ledger": set(),
        "seed_graph": {"market", "term", "term_type", "platform", "trend_date"},
        "seed_candidates": {
            "candidate_id",
            "proposed_date",
            "market",
            "candidate_type",
            "candidate_value",
            "source",
            "lane",
            "status",
        },
        "enriched_content": {"id", "source", "platform", "market", "collected_at"},
        "raw_content": {"id", "source", "platform", "collected_at"},
    }[table]
    return {
        field: (
            next((kind for kind, names in types.items() if field in names), "STRING"),
            field not in required,
        )
        for field in field_names
    }


def validate_source_snapshot_v3(value, cutoff, *, markets):
    from src.analysis.open_intelligence import pipeline

    snapshot = _mapping(value, "v3 source snapshot")
    _exact_fields(snapshot, _SNAPSHOT_FIELDS, "v3 source snapshot")
    if _digest(snapshot) != _digest(normalize_replay_v3_json(snapshot)):
        raise IncompleteReplayInput("v3 source snapshot must use normalized JSON dates")
    for field in ("fixture_scope", "snapshot_id"):
        if not isinstance(snapshot[field], str) or not snapshot[field].strip():
            raise IncompleteReplayInput(f"v3 source snapshot {field} is invalid")
    captured = _timestamp(snapshot["captured_at"], "v3 captured at")
    if captured.date() > cutoff or captured.isoformat() != snapshot["captured_at"]:
        raise IncompleteReplayInput("v3 snapshot capture window is invalid")
    source_fields = {
        "event_ledger": pipeline.EVENT_COLUMNS,
        "seed_graph": pipeline.SEED_GRAPH_COLUMNS,
        "seed_candidates": pipeline.SEED_CANDIDATE_COLUMNS,
        **pipeline.EVIDENCE_COLUMNS_BY_TABLE,
    }
    rows_by_table = _mapping(snapshot["rows_by_table"], "v3 source rows")
    counts = _mapping(snapshot["row_counts_by_table"], "v3 source counts")
    digests = _mapping(snapshot["table_digests"], "v3 source digests")
    if any(set(item) != set(source_fields) for item in (rows_by_table, counts, digests)):
        raise IncompleteReplayInput("v3 source snapshot table scope is inconsistent")
    sections = _mapping(snapshot["section_counts"], "v3 source sections")
    identities = _mapping(snapshot["section_identity_sets"], "v3 source section identities")
    if set(sections) != set(source_fields) or set(identities) != set(source_fields):
        raise IncompleteReplayInput("v3 source section scope is inconsistent")
    for table, field_names in source_fields.items():
        rows = _sequence(rows_by_table[table], f"v3 {table} rows")
        if type(counts[table]) is not int or counts[table] != len(rows):
            raise IncompleteReplayInput("v3 source snapshot row count is inconsistent")
        ceiling = pipeline.SOURCE_CEILINGS.get(table, pipeline.MAX_REQUESTED_SAMPLE_KEYS + 1)
        if len(rows) > ceiling:
            raise IncompleteReplayInput("source_provenance_limit_exceeded")
        field_schema = _v3_source_field_schema(table, field_names)
        for row in rows:
            row = _mapping(row, f"v3 {table} row")
            _exact_fields(row, field_names, f"v3 {table} row")
            if row["market"] not in markets:
                raise IncompleteReplayInput("v3 source snapshot market is invalid")
            for field, (kind, nullable) in field_schema.items():
                item = row[field]
                if item is None:
                    if not nullable:
                        raise IncompleteReplayInput(f"v3 source {table}.{field} null is invalid")
                    continue
                if kind == "INT64":
                    valid = type(item) is int and -(2**63) <= item < 2**63
                elif kind == "FLOAT64":
                    valid = (
                        type(item) in (int, float)
                        and abs(item) <= float.fromhex("0x1.fffffffffffffp+1023")
                        and math.isfinite(item)
                    )
                elif kind == "ARRAY<STRING>":
                    valid = isinstance(item, (list, tuple)) and all(
                        isinstance(member, str) for member in item
                    )
                else:
                    valid = isinstance(item, str)
                if not valid:
                    raise IncompleteReplayInput(f"v3 source {table}.{field} type is invalid")
                if kind == "DATE":
                    _date_value(item, field)
                elif kind == "TIMESTAMP":
                    _timestamp(item, field)
            if table in pipeline.EVIDENCE_COLUMNS_BY_TABLE:
                for field in ("id", "source", "platform", "collected_at"):
                    if not isinstance(row[field], str) or not row[field].strip():
                        raise IncompleteReplayInput(f"v3 source {field} is missing")
                collected = _timestamp(row["collected_at"], "v3 collected at")
                if (
                    collected.isoformat() != row["collected_at"]
                    or collected > captured
                    or not cutoff - timedelta(days=6) <= collected.date() <= cutoff
                ):
                    raise IncompleteReplayInput("v3 source snapshot evidence window is invalid")
                if row["published_at"] is not None:
                    published = _timestamp(row["published_at"], "v3 published at")
                    if published > captured or published.isoformat() != row["published_at"]:
                        raise IncompleteReplayInput("v3 source published window is invalid")
                vendor, channel = row["vendor_family"], row["channel_family"]
                if (vendor is None) != (channel is None) or (
                    row["source_family"] is not None and row["source_family"] != channel
                ):
                    raise IncompleteReplayInput("v3 source pair binding is invalid")
            else:
                field = "proposed_date" if table == "seed_candidates" else "trend_date"
                if row[field] != cutoff.isoformat():
                    raise IncompleteReplayInput("v3 source snapshot candidate window is invalid")
                if (
                    table == "seed_candidates"
                    and row["status"] not in ACCEPTED_SEED_CANDIDATE_STATUSES
                ):
                    raise IncompleteReplayInput("v3 source candidate status is invalid")
                if (
                    table == "event_ledger"
                    and row["as_of"] is not None
                    and _timestamp(row["as_of"], "v3 event as of") > captured
                ):
                    raise IncompleteReplayInput("v3 source event is after capture")
                if (
                    table == "seed_graph"
                    and row["event_date"] is not None
                    and _date_value(row["event_date"], "v3 event date") > cutoff
                ):
                    raise IncompleteReplayInput("v3 source event date is after cutoff")
        source_identities = [f"{index:08d}:{_digest(row)}" for index, row in enumerate(rows)]
        if (
            type(sections[table]) is not int
            or sections[table] != len(rows)
            or _digest(identities[table]) != _digest(source_identities)
        ):
            raise IncompleteReplayInput("v3 source section identities are inconsistent")
        if _valid_digest(digests[table], "v3 table") != _digest(rows):
            raise IncompleteReplayInput("v3 source snapshot table digest mismatch")
    preimage = dict(snapshot)
    digest = preimage.pop("source_digest")
    if _valid_digest(digest, "v3 snapshot") != _digest(preimage):
        raise IncompleteReplayInput("source_provenance_snapshot_mismatch")
    return _freeze(snapshot)


class ProvenanceSnapshotClient:
    project = "ogilvy-trends-v2"

    def __init__(self, snapshot, *, delegate=None):
        self.snapshot = snapshot
        self.delegate = delegate

    def query(self, sql, *, job_config, location):
        from collections import Counter

        from src.analysis.open_intelligence import pipeline

        tables = re.findall(r"`ogilvy-trends-v2\.trends_v2_staging\.([a-z_]+)`", sql)
        if (
            len(tables) != 1
            or location != "US"
            or not sql.startswith(("SELECT ", "WITH requested AS"))
        ):
            raise IncompleteReplayInput("v3 source snapshot query scope is invalid")
        table = tables[0]
        rows = [dict(row) for row in self.snapshot["rows_by_table"][table]]
        params = {
            p.name: getattr(p, "values", getattr(p, "value", None))
            for p in job_config.query_parameters
        }
        if table in pipeline.EVIDENCE_COLUMNS_BY_TABLE:
            keys = set(zip(params["sample_markets"], params["sample_ids"], strict=True))
            rows = [row for row in rows if (row["market"], row["id"]) in keys]
            matches = Counter((row["market"], row["id"]) for row in rows)
            for row in rows:
                row["match_count"] = matches[(row["market"], row["id"])]
            order = ("market", "id", "collected_at", "source", "platform")
            limit = len(keys) + 1
        else:
            rows = [row for row in rows if row["market"] in params["markets"]]
            order = {
                "event_ledger": ("market", "entity_key", "ledger_id"),
                "seed_graph": ("market", "term", "term_type", "platform"),
                "seed_candidates": ("market", "candidate_value", "candidate_type", "candidate_id"),
            }[table]
            limit = pipeline.SOURCE_CEILINGS[table] + 1
        rows.sort(key=lambda row: tuple((row[key] is not None, row[key]) for key in order))
        expected = rows[:limit]
        if self.delegate is not None:
            job = self.delegate.query(sql, job_config=job_config, location=location)
            actual = tuple(pipeline._row_mapping(row) for row in job.result(max_results=limit))
            if _digest(normalize_replay_v3_json(actual)) != _digest(expected):
                raise IncompleteReplayInput("source_provenance_snapshot_mismatch:query_rows")
        typed_rows = []
        for row in expected:
            typed = dict(row)
            for key in ("collected_at", "published_at", "as_of"):
                if typed.get(key) is not None:
                    typed[key] = _timestamp(typed[key], key)
            for key in ("trend_date", "event_date", "proposed_date"):
                if typed.get(key) is not None:
                    typed[key] = _date_value(typed[key], key)
            typed_rows.append(typed)
        return _SnapshotRows(tuple(typed_rows))


@dataclass(frozen=True)
class _SnapshotRows:
    rows: tuple

    def result(self, *, max_results):
        return iter(self.rows[:max_results])


@dataclass(frozen=True, slots=True)
class CompositionRuleBundleV3:
    status: str
    rule_version: str
    approval_contract_sha256: str
    rule_config_sha256: str
    graph_rules: AnchoredGraphRules


def load_composition_rules_v3(path, *, require_certified):
    import yaml

    raw = Path(path).read_bytes()
    document = yaml.safe_load(raw)
    expected_fields = {
        "status",
        "rule_version",
        "cluster_build_version",
        "contract_version",
        "channel_taxonomy_version",
        "approval_contract_sha256",
        "replay_receipt_id",
        "approved_at",
        "approved_by",
        "expires_at",
        "graph_rules",
    }
    if not isinstance(document, Mapping) or set(document) != expected_fields:
        raise IncompleteReplayInput("composition v3 rule fields are invalid")
    if require_certified:
        raise IncompleteReplayInput("composition v3 rules are not replay certified")
    if (
        document["status"] != "uncertified"
        or document["rule_version"] != "composition_rules_v3"
        or document["cluster_build_version"] != "hybrid_graph_v3"
        or document["contract_version"] != "2.0.0"
        or document["channel_taxonomy_version"] != "channel_family_v2"
        or document["approval_contract_sha256"]
        != "ff7a1ac1f9a531fe4a48c2dc4a9f45c4b0a82a61c8367e0c416a3194bcdfab4a"
        or any(
            document[field] is not None
            for field in ("approved_at", "approved_by", "expires_at", "replay_receipt_id")
        )
    ):
        raise IncompleteReplayInput("composition v3 version or approval binding is invalid")
    values = document["graph_rules"]
    expected_rules = {
        "semantic_anchor_floor": 0.5,
        "temporal_overlap_days": 2,
        "pair_ceiling": 250000,
        "component_member_ceiling": 50,
    }
    if _digest(values) != _digest(expected_rules):
        raise IncompleteReplayInput("composition v3 numeric rule binding is invalid")
    return CompositionRuleBundleV3(
        document["status"],
        document["rule_version"],
        document["approval_contract_sha256"],
        hashlib.sha256(raw).hexdigest(),
        AnchoredGraphRules(**values),
    )


def _v3_provider_record(observations, provider, rules, runtime_ms):
    from src.analysis.open_intelligence.composition import _anchored_composition_inputs

    _ordered, pairs, scores = _anchored_composition_inputs(
        observations, provider, rules.graph_rules
    )
    matrix = [
        {"left": left, "right": right, "score": scores[(left, right)]} for left, right in pairs
    ]
    return {
        "candidate_id": "composition_rules_v3:" + rules.rule_config_sha256,
        "provider_kind": "lexical"
        if isinstance(provider, LexicalSimilarityProviderV2)
        else "frozen_test",
        "provider_version": provider.version,
        "metering_status": "not_applicable",
        "matrix_digest": _digest(matrix),
        "pair_count": len(pairs),
        "runtime_ms": runtime_ms,
        "input_units": None,
        "estimated_cost": None,
        "missing_work": [],
    }


def _v3_result_fields(result):
    run = result.run
    projection = run.evidence_projection
    if projection is None:
        raise IncompleteReplayInput("v3 evidence projection is missing")
    candidate_inputs = ReplayCandidateInputs(
        run.candidate_inputs.event_ledger,
        run.candidate_inputs.seed_graph,
        run.candidate_inputs.seed_candidates,
        projection.candidate_only_receipts,
        projection.unresolved_sample_ids,
        run.missing_work,
        run.source_windows,
    )
    return normalize_replay_v3_json(
        {
            "candidate_inputs": candidate_inputs,
            "observations": run.observations,
            "components_by_candidate": {
                _component_key_v3(component): component for component in run.components
            },
            "memberships_by_candidate": projection.memberships_by_component,
            "receipts_by_member": projection.receipts_by_member,
            "current_window": projection.source_window,
            "source_provenance_by_member": result.source_provenance_by_member,
        }
    )


def _component_key_v3(component):
    from src.analysis.open_intelligence.pipeline import _component_key

    return _component_key(component)


def adapt_provenance_result_to_replay_input(
    result,
    *,
    scope,
    source_snapshot,
    semantic_provider=None,
    provider_outputs=None,
    known_event_set=None,
    history_window=None,
    prior_snapshots=(),
    human_review_state="pending",
):
    from src.analysis.open_intelligence.pipeline import ProvenanceSignalRunResult

    if not isinstance(result, ProvenanceSignalRunResult) or result.run.run_id != scope.run_id:
        raise IncompleteReplayInput("v3 result or scope is invalid")
    if (
        result.run.contract_version != "2.0.0"
        or result.run.rule_version != "composition_rules_v3"
        or result.run.dry_run is not True
        or result.run.persistable_count != 0
        or result.run.persistence_result is not None
        or any(result.run.row_counts.values())
        or result.source_snapshot_digest != source_snapshot.get("source_digest")
    ):
        raise IncompleteReplayInput("v3 result must be a snapshot-bound zero-write run")
    provider = semantic_provider or LexicalSimilarityProviderV2()
    rules = load_composition_rules_v3(
        ROOT / "configs/open_intelligence_composition_rules_v3.yaml", require_certified=False
    )
    data = _v3_result_fields(result)
    cutoff = result.run.evidence_projection.source_window.end_date
    if provider_outputs is None:
        import time as clock

        started = clock.perf_counter()
        record = _v3_provider_record(result.run.observations, provider, rules, 0.0)
        record["runtime_ms"] = (clock.perf_counter() - started) * 1000
        provider_outputs = [record]
    if known_event_set is None:
        known_event_set = {
            "fixture_scope": source_snapshot["fixture_scope"],
            "set_id": "unavailable",
            "events": [],
            "digest": _digest([]),
        }
    if history_window is None:
        start = cutoff - timedelta(days=7)
        history_window = {
            "start_date": start.isoformat(),
            "end_date": start.isoformat(),
            "markets": sorted(scope.market_scope),
            "source_tables": list(result.run.evidence_projection.source_window.source_tables),
            "complete_partitions": False,
        }
    payload = {
        "artifact_version": "open_intelligence_replay_v3",
        "contract_version": "2.0.0",
        "cutoff": cutoff.isoformat(),
        "scope": _scope_mapping(scope),
        "source_snapshot": source_snapshot,
        "known_event_set": known_event_set,
        **data,
        "history_window": history_window,
        "prior_snapshots": prior_snapshots,
        "provider_outputs": provider_outputs,
        "human_review_state": human_review_state,
    }
    return validate_replay_input_v3(normalize_replay_v3_json(payload), semantic_provider=provider)


def validate_replay_input_v3(value, *, semantic_provider=None):
    from src.analysis.open_intelligence.pipeline import run_dynamic_signal_identity_v3

    payload = _mapping(value, "v3 replay input")
    _exact_fields(payload, (*REPLAY_INPUT_FIELDS, "source_provenance_by_member"), "v3 replay")
    if (
        payload["artifact_version"] != "open_intelligence_replay_v3"
        or payload["contract_version"] != "2.0.0"
    ):
        raise IncompleteReplayInput("source_provenance_version_invalid")
    cutoff = _date_value(payload["cutoff"], "v3 cutoff")
    scope_values = _mapping(payload["scope"], "v3 scope")
    _exact_fields(scope_values, _SCOPE_FIELDS, "v3 scope")
    scope = ResolvedScope(
        **{
            **scope_values,
            "market_scope": tuple(scope_values["market_scope"]),
            "audience_lens_ids": tuple(scope_values["audience_lens_ids"]),
            "contract_version": "2.0.0",
        }
    )
    snapshot = validate_source_snapshot_v3(
        payload["source_snapshot"], cutoff, markets=scope.market_scope
    )
    rules = load_composition_rules_v3(
        ROOT / "configs/open_intelligence_composition_rules_v3.yaml", require_certified=False
    )
    provider = semantic_provider or LexicalSimilarityProviderV2()
    result = run_dynamic_signal_identity_v3(
        cutoff,
        scope,
        ProvenanceSnapshotClient(snapshot),
        "trends_v2_staging",
        False,
        source_snapshot=snapshot,
        rule_bundle=rules,
        semantic_provider=provider,
    )
    expected = _v3_result_fields(result)
    for field, data in expected.items():
        if _digest(payload[field]) != _digest(data):
            raise IncompleteReplayInput(f"source_provenance_conflict:{field}")
    providers = _sequence(payload["provider_outputs"], "v3 providers")
    if len(providers) != 1:
        raise IncompleteReplayInput("v3 replay requires one bound provider output")
    validate_replay_semantic_candidate(providers[0])
    expected_provider = _v3_provider_record(
        result.run.observations, provider, rules, providers[0]["runtime_ms"]
    )
    if _digest(providers[0]) != _digest(expected_provider):
        raise IncompleteReplayInput("v3 provider output does not match reconstructed matrix")
    history = _mapping(payload["history_window"], "v3 history window")
    _exact_fields(history, _WINDOW_FIELDS, "v3 history window")
    start = _date_value(history["start_date"], "v3 history start")
    end = _date_value(history["end_date"], "v3 history end")
    if (
        start > end
        or end >= cutoff - timedelta(days=6)
        or history["complete_partitions"] is not False
        or _digest(history["markets"]) != _digest(sorted(scope.market_scope))
        or _digest(history["source_tables"]) != _digest(expected["current_window"]["source_tables"])
    ):
        raise IncompleteReplayInput("v3 history window is invalid")
    known = _mapping(payload["known_event_set"], "v3 known event set")
    _exact_fields(known, _KNOWN_EVENT_SET_FIELDS, "v3 known event set")
    if known["fixture_scope"] != snapshot["fixture_scope"]:
        raise IncompleteReplayInput("v3 known event source scope is invalid")
    if not isinstance(known["set_id"], str) or not known["set_id"].strip():
        raise IncompleteReplayInput("v3 known event set ID is invalid")
    events = _sequence(known["events"], "v3 known events")
    observed_members = {
        _semantic_identity(observation): observation.market
        for observation in result.run.observations
    }
    for event in events:
        event = _mapping(event, "v3 known event")
        _exact_fields(event, _KNOWN_EVENT_FIELDS, "v3 known event")
        members = _sequence(event["member_identities"], "v3 known event members")
        if (
            event["market"] not in scope.market_scope
            or not members
            or any(not isinstance(member, str) for member in members)
            or members != sorted(set(members))
            or any(observed_members.get(member) != event["market"] for member in members)
        ):
            raise IncompleteReplayInput("v3 known event source members are invalid")
    _unique(events, "event_id", "v3 known event")
    if _valid_digest(known["digest"], "v3 known event") != _digest(events):
        raise IncompleteReplayInput("v3 known event digest does not match events")
    if known["set_id"] == "unavailable" and events:
        raise IncompleteReplayInput("v3 unavailable known event set must be empty")
    if _sequence(payload["prior_snapshots"], "v3 prior snapshots"):
        raise IncompleteReplayInput("v3 prior snapshot source relation is unavailable")
    if payload["human_review_state"] != "pending":
        raise IncompleteReplayInput("v3 replay human review must remain pending")
    return _freeze(normalize_replay_v3_json(payload))


def serialize_replay_input_v3(value):
    payload = _mapping(value, "v3 replay input")
    _exact_fields(payload, (*REPLAY_INPUT_FIELDS, "source_provenance_by_member"), "v3 replay")
    if (
        payload["artifact_version"] != "open_intelligence_replay_v3"
        or payload["contract_version"] != "2.0.0"
    ):
        raise IncompleteReplayInput("source_provenance_version_invalid")
    return json.dumps(
        normalize_replay_v3_json(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


_PRODUCTION_REPLAY_FIELDS = (
    "artifact_version",
    "contract_version",
    "cutoff",
    "scope",
    "production_snapshot",
    "candidate_inputs",
    "observations",
    "components_by_candidate",
    "memberships_by_candidate",
    "receipts_by_member",
    "current_window",
    "history_window",
    "provider_outputs",
    "human_review_state",
    "source_provenance_by_member",
    "replay_digest",
)
_PRODUCTION_REPLAY_VERSION = "open_intelligence_production_replay_v1"


@dataclass(frozen=True)
class _ProductionReplayTableResource:
    value: Mapping[str, object]

    def to_api_repr(self):
        return copy.deepcopy(dict(self.value))


@dataclass(frozen=True)
class _ProductionReplayQueryJob:
    rows: tuple[object, ...]
    total_bytes_billed: int = 0

    def result(self, *, max_results, **_kwargs):
        return iter(self.rows[:max_results])


class _ProductionReplayDelegate:
    project = "ogilvy-trends-v2"

    def __init__(self, prepared):
        self._prepared = prepared
        self._snapshot = prepared["snapshot"]
        self._metadata = prepared["snapshot_metadata"]
        self._tables = {
            item["destination_table"]: item["lane"]
            for item in prepared["snapshot_plan"]["statements"]
        }

    def get_table(self, name, **_kwargs):
        lane = self._tables.get(name)
        if lane is None:
            raise IncompleteReplayInput("production replay table is invalid")
        resource = copy.deepcopy(self._metadata[lane])
        resource.setdefault("location", "US")
        return _ProductionReplayTableResource(resource)

    def query(self, sql, *, job_config, location, **_kwargs):
        matches = [(table, lane) for table, lane in self._tables.items() if f"`{table}`" in sql]
        if len(matches) != 1 or location != "US":
            raise IncompleteReplayInput("production replay query is invalid")
        table, lane = matches[0]
        logical = sql.replace(f"`{table}`", f"`ogilvy-trends-v2.trends_v2_staging.{lane}`")
        if lane in {"enriched_content", "raw_content"}:
            from src.analysis.open_intelligence.production_snapshot_rows import (
                ABSENT_NULLABLE_FIELDS,
            )

            for field in ABSENT_NULLABLE_FIELDS:
                logical = logical.replace(
                    f"CAST(NULL AS STRING) AS `{field}`", f"evidence.`{field}`"
                )
        rows = tuple(
            ProvenanceSnapshotClient(self._snapshot)
            .query(logical, job_config=job_config, location=location)
            .result(max_results=100_000)
        )
        return _ProductionReplayQueryJob(rows)


def _production_replay_result(prepared, cutoff, scope, provider):
    from src.analysis.open_intelligence.pipeline import run_dynamic_signal_identity_v3

    rules = load_composition_rules_v3(
        ROOT / "configs/open_intelligence_composition_rules_v3.yaml", require_certified=False
    )
    return run_dynamic_signal_identity_v3(
        cutoff,
        scope,
        _ProductionReplayDelegate(prepared),
        "trends_v2_staging",
        False,
        source_snapshot=prepared,
        rule_bundle=rules,
        semantic_provider=provider,
    ), rules


def _protected_snapshot_result_to_replay(
    *,
    manifest_sha256,
    consumption_id,
    result_id,
    result_digest,
    scope,
    objects,
    result_reader=None,
    approval_reader=None,
    semantic_provider=None,
):
    from src.analysis.open_intelligence.production_snapshot_capture import _read_protected_capture

    if not isinstance(scope, ResolvedScope):
        raise IncompleteReplayInput("protected snapshot replay scope is invalid")
    validated = _read_protected_capture(
        manifest_sha256=manifest_sha256,
        consumption_id=consumption_id,
        result_id=result_id,
        result_digest=result_digest,
        client_scope_id=scope.client_scope_id,
        market_scope=scope.market_scope,
        objects=objects,
        result_reader=result_reader,
        approval_reader=approval_reader,
    )
    prepared = validated["capture"]["assembly"]
    provider = semantic_provider or LexicalSimilarityProviderV2()
    result, _rules = _production_replay_result(
        prepared,
        date.fromisoformat(validated["binding"]["cutoff_date"]),
        scope,
        provider,
    )
    return adapt_production_result_to_replay_input(
        result,
        scope=scope,
        production_snapshot=prepared,
        semantic_provider=provider,
    )


def adapt_production_result_to_replay_input(
    result,
    *,
    scope,
    production_snapshot,
    semantic_provider=None,
    provider_outputs=None,
    history_window=None,
):
    from src.analysis.open_intelligence.pipeline import ProvenanceSignalRunResult
    from src.analysis.open_intelligence.production_snapshot import (
        validate_prepared_production_snapshot,
    )

    if not isinstance(result, ProvenanceSignalRunResult) or not isinstance(scope, ResolvedScope):
        raise IncompleteReplayInput("production replay result or scope is invalid")
    projection = result.run.evidence_projection
    if projection is None:
        raise IncompleteReplayInput("production replay evidence projection is missing")
    cutoff = projection.source_window.end_date
    prepared = validate_prepared_production_snapshot(
        production_snapshot,
        cutoff_date=cutoff,
        client_scope_id=scope.client_scope_id,
        market_scope=scope.market_scope,
    )
    if (
        result.run.run_id != scope.run_id
        or result.run.contract_version != "2.0.0"
        or result.run.rule_version != "composition_rules_v3"
        or result.run.dry_run is not True
        or result.run.persistable_count != 0
        or result.run.persistence_result is not None
        or any(result.run.row_counts.values())
        or result.source_snapshot_digest != prepared["snapshot"]["source_digest"]
        or prepared["source_authority"] is not False
    ):
        raise IncompleteReplayInput("production replay requires a snapshot-bound zero-write run")
    provider = semantic_provider or LexicalSimilarityProviderV2()
    rules = load_composition_rules_v3(
        ROOT / "configs/open_intelligence_composition_rules_v3.yaml", require_certified=False
    )
    data = _v3_result_fields(result)
    if provider_outputs is None:
        import time as clock

        started = clock.perf_counter()
        record = _v3_provider_record(result.run.observations, provider, rules, 0.0)
        record["runtime_ms"] = (clock.perf_counter() - started) * 1000
        provider_outputs = [record]
    if history_window is None:
        start = cutoff - timedelta(days=7)
        history_window = {
            "start_date": start.isoformat(),
            "end_date": start.isoformat(),
            "markets": sorted(scope.market_scope),
            "source_tables": list(projection.source_window.source_tables),
            "complete_partitions": False,
        }
    payload = normalize_replay_v3_json(
        {
            "artifact_version": _PRODUCTION_REPLAY_VERSION,
            "contract_version": "2.0.0",
            "cutoff": cutoff.isoformat(),
            "scope": _scope_mapping(scope),
            "production_snapshot": prepared,
            **data,
            "history_window": history_window,
            "provider_outputs": provider_outputs,
            "human_review_state": "pending",
        }
    )
    payload["replay_digest"] = _digest(payload)
    return validate_production_replay_input(payload, semantic_provider=provider)


def validate_production_replay_input(value, *, semantic_provider=None):
    from src.analysis.open_intelligence.production_snapshot import (
        validate_prepared_production_snapshot,
    )

    payload = _mapping(value, "production replay")
    _exact_fields(payload, _PRODUCTION_REPLAY_FIELDS, "production replay")
    if (
        payload["artifact_version"] != _PRODUCTION_REPLAY_VERSION
        or payload["contract_version"] != "2.0.0"
    ):
        raise IncompleteReplayInput("source_provenance_version_invalid")
    replay_digest = _valid_digest(payload["replay_digest"], "production replay")
    preimage = dict(payload)
    preimage.pop("replay_digest")
    if replay_digest != _digest(preimage):
        raise IncompleteReplayInput("production replay digest is invalid")
    cutoff = _date_value(payload["cutoff"], "production replay cutoff")
    scope_values = _mapping(payload["scope"], "production replay scope")
    _exact_fields(scope_values, _SCOPE_FIELDS, "production replay scope")
    scope = ResolvedScope(
        **{
            **scope_values,
            "market_scope": tuple(scope_values["market_scope"]),
            "audience_lens_ids": tuple(scope_values["audience_lens_ids"]),
            "contract_version": "2.0.0",
        }
    )
    prepared = validate_prepared_production_snapshot(
        payload["production_snapshot"],
        cutoff_date=cutoff,
        client_scope_id=scope.client_scope_id,
        market_scope=scope.market_scope,
    )
    if prepared["source_authority"] is not False:
        raise IncompleteReplayInput("production replay cannot grant source authority")
    provider = semantic_provider or LexicalSimilarityProviderV2()
    result, rules = _production_replay_result(prepared, cutoff, scope, provider)
    if result.source_snapshot_digest != prepared["snapshot"]["source_digest"]:
        raise IncompleteReplayInput("production replay source digest is inconsistent")
    expected = _v3_result_fields(result)
    for field, data in expected.items():
        if _digest(payload[field]) != _digest(data):
            raise IncompleteReplayInput(f"source_provenance_conflict:{field}")
    providers = _sequence(payload["provider_outputs"], "production replay providers")
    if len(providers) != 1:
        raise IncompleteReplayInput("production replay requires one bound provider output")
    validate_replay_semantic_candidate(providers[0])
    expected_provider = _v3_provider_record(
        result.run.observations, provider, rules, providers[0]["runtime_ms"]
    )
    if _digest(providers[0]) != _digest(expected_provider):
        raise IncompleteReplayInput(
            "production provider output does not match reconstructed matrix"
        )
    history = _mapping(payload["history_window"], "production replay history window")
    _exact_fields(history, _WINDOW_FIELDS, "production replay history window")
    start = _date_value(history["start_date"], "production replay history start")
    end = _date_value(history["end_date"], "production replay history end")
    if (
        start > end
        or end >= cutoff - timedelta(days=6)
        or history["complete_partitions"] is not False
        or _digest(history["markets"]) != _digest(sorted(scope.market_scope))
        or _digest(history["source_tables"]) != _digest(expected["current_window"]["source_tables"])
    ):
        raise IncompleteReplayInput("production replay history window is invalid")
    if payload["human_review_state"] != "pending":
        raise IncompleteReplayInput("production replay human review must remain pending")
    return _freeze(normalize_replay_v3_json(payload))


def serialize_production_replay_input(value):
    payload = _mapping(value, "production replay")
    _exact_fields(payload, _PRODUCTION_REPLAY_FIELDS, "production replay")
    if (
        payload["artifact_version"] != _PRODUCTION_REPLAY_VERSION
        or payload["contract_version"] != "2.0.0"
    ):
        raise IncompleteReplayInput("source_provenance_version_invalid")
    digest = _valid_digest(payload["replay_digest"], "production replay")
    preimage = dict(payload)
    preimage.pop("replay_digest")
    if digest != _digest(preimage):
        raise IncompleteReplayInput("production replay digest is invalid")
    return json.dumps(
        normalize_replay_v3_json(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validated_source_snapshot(
    value: object, cutoff: date
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    snapshot = _mapping(value, "source snapshot")
    _reject_forbidden_keys(snapshot)
    _exact_fields(snapshot, _SNAPSHOT_FIELDS, "source snapshot")
    if snapshot["fixture_scope"] != "synthetic_test_only":
        raise IncompleteReplayInput("fixture scope is invalid")
    cutoff_end = datetime.combine(cutoff, time.max, tzinfo=UTC)
    _reject_future(snapshot, cutoff_end)
    if not isinstance(snapshot["snapshot_id"], str) or not snapshot["snapshot_id"]:
        raise IncompleteReplayInput("snapshot ID is invalid")
    source_row_fields = (
        _ADAPTER_SOURCE_ROW_FIELDS
        if snapshot["snapshot_id"] == "task5c_adapter_snapshot_v1"
        else _SOURCE_ROW_FIELDS
    )
    counts = _mapping(snapshot["row_counts_by_table"], "row counts")
    section_counts = _mapping(snapshot["section_counts"], "section counts")
    section_identities = _mapping(snapshot["section_identity_sets"], "section identities")
    if set(section_counts) != set(_SECTION_FIELDS) or set(section_identities) != set(
        _SECTION_FIELDS
    ):
        raise IncompleteReplayInput("section receipt fields are invalid")
    table_digests = _mapping(snapshot["table_digests"], "table digests")
    rows_by_table = _mapping(snapshot["rows_by_table"], "rows by table")
    if (
        set(counts) != set(source_row_fields)
        or set(counts) != set(table_digests)
        or set(counts) != set(rows_by_table)
    ):
        raise IncompleteReplayInput("source snapshot table scope is inconsistent")
    for table in sorted(counts):
        rows = _sequence(rows_by_table[table], f"{table} rows")
        count = counts[table]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count != len(rows):
            raise IncompleteReplayInput(f"{table} row count is inconsistent")
        for row in rows:
            mapped_row = _mapping(row, f"{table} row")
            _exact_fields(
                mapped_row,
                source_row_fields[table],
                f"{table} row",
            )
            if "vendor_family" in mapped_row:
                vendor = mapped_row["vendor_family"]
                channel = mapped_row["channel_family"]
                source = mapped_row["source_family"]
                if (
                    not isinstance(vendor, str)
                    or not vendor
                    or not isinstance(channel, str)
                    or not channel
                    or source != channel
                    or (vendor, channel) not in _APPROVED_SOURCE_IDENTITY_PAIRS
                ):
                    raise IncompleteReplayInput(f"{table} source identity is invalid")
            if "vendor_families" in mapped_row:
                vendors = tuple(mapped_row["vendor_families"])
                channels = tuple(mapped_row["channel_families"])
                sources = tuple(mapped_row["source_families"])
                if (
                    not vendors
                    or channels != sources
                    or len(vendors) != len(channels)
                    or any(
                        pair not in _APPROVED_SOURCE_IDENTITY_PAIRS
                        for pair in zip(vendors, channels, strict=True)
                    )
                ):
                    raise IncompleteReplayInput(f"{table} source identity is invalid")
        observed_digest = _valid_digest(table_digests[table], f"{table} table")
        if observed_digest != _digest(rows):
            raise IncompleteReplayInput(f"{table} table digest does not match rows")
    observed_source_digest = _valid_digest(snapshot["source_digest"], "source")
    snapshot_without_digest = dict(snapshot)
    snapshot_without_digest.pop("source_digest")
    if observed_source_digest != _digest(snapshot_without_digest):
        raise IncompleteReplayInput("source digest does not match snapshot")
    return snapshot, counts, section_counts, section_identities, rows_by_table


def validate_replay_input(value: object) -> ReplayInputBundle:
    payload = _mapping(value, "replay input bundle")
    _reject_forbidden_keys(payload)
    _exact_fields(payload, REPLAY_INPUT_FIELDS, "bundle")
    if payload["artifact_version"] != "replay_input_v1":
        raise IncompleteReplayInput("artifact version is invalid")
    if not isinstance(payload["contract_version"], str) or not payload["contract_version"]:
        raise IncompleteReplayInput("contract version is invalid")
    cutoff = _date_value(payload["cutoff"], "cutoff")
    cutoff_end = datetime.combine(cutoff, time.max, tzinfo=UTC)
    _reject_future(payload, cutoff_end)

    scope = _mapping(payload["scope"], "scope")
    _exact_fields(scope, _SCOPE_FIELDS, "scope")
    markets = scope["market_scope"]
    if not isinstance(markets, list) or tuple(markets) != MARKETS:
        raise IncompleteReplayInput("scope markets are invalid")
    if not isinstance(scope["audience_lens_ids"], list):
        raise IncompleteReplayInput("scope audience lens IDs are invalid")
    for field in ("client_scope_id", "brand_config_id", "theme_id", "run_id"):
        if not isinstance(scope[field], str) or not scope[field]:
            raise IncompleteReplayInput(f"scope {field} is invalid")

    snapshot = _mapping(payload["source_snapshot"], "source snapshot")
    _exact_fields(snapshot, _SNAPSHOT_FIELDS, "source snapshot")
    if snapshot["fixture_scope"] != "synthetic_test_only":
        raise IncompleteReplayInput("fixture scope is invalid")
    _timestamp(snapshot["captured_at"], "captured at")
    if not isinstance(snapshot["snapshot_id"], str) or not snapshot["snapshot_id"]:
        raise IncompleteReplayInput("snapshot ID is invalid")
    source_row_fields = (
        _ADAPTER_SOURCE_ROW_FIELDS
        if snapshot["snapshot_id"] == "task5c_adapter_snapshot_v1"
        else _SOURCE_ROW_FIELDS
    )
    counts = _mapping(snapshot["row_counts_by_table"], "row counts")
    section_counts = _mapping(snapshot["section_counts"], "section counts")
    section_identities = _mapping(snapshot["section_identity_sets"], "section identities")
    if set(section_counts) != set(_SECTION_FIELDS) or set(section_identities) != set(
        _SECTION_FIELDS
    ):
        raise IncompleteReplayInput("section receipt fields are invalid")
    table_digests = _mapping(snapshot["table_digests"], "table digests")
    rows_by_table = _mapping(snapshot["rows_by_table"], "rows by table")
    if (
        set(counts) != set(source_row_fields)
        or set(counts) != set(table_digests)
        or set(counts) != set(rows_by_table)
    ):
        raise IncompleteReplayInput("source snapshot table scope is inconsistent")
    for table in sorted(counts):
        rows = _sequence(rows_by_table[table], f"{table} rows")
        count = counts[table]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count != len(rows):
            raise IncompleteReplayInput(f"{table} row count is inconsistent")
        for row in rows:
            mapped_row = _mapping(row, f"{table} row")
            _exact_fields(mapped_row, source_row_fields[table], f"{table} row")
            if tuple(mapped_row) != source_row_fields[table]:
                raise IncompleteReplayInput(f"{table} row field order is invalid")
            if "vendor_family" in mapped_row:
                vendor = mapped_row["vendor_family"]
                channel = mapped_row["channel_family"]
                source = mapped_row["source_family"]
                if (
                    not isinstance(vendor, str)
                    or not vendor
                    or not isinstance(channel, str)
                    or not channel
                    or source != channel
                    or (vendor, channel) not in _APPROVED_SOURCE_IDENTITY_PAIRS
                ):
                    raise IncompleteReplayInput(f"{table} source identity is invalid")
            if "vendor_families" in mapped_row:
                vendors = tuple(mapped_row["vendor_families"])
                channels = tuple(mapped_row["channel_families"])
                sources = tuple(mapped_row["source_families"])
                if (
                    not vendors
                    or channels != sources
                    or len(vendors) != len(channels)
                    or any(
                        pair not in _APPROVED_SOURCE_IDENTITY_PAIRS
                        for pair in zip(vendors, channels, strict=True)
                    )
                ):
                    raise IncompleteReplayInput(f"{table} source identity is invalid")
        observed_digest = _valid_digest(table_digests[table], f"{table} table")
        if observed_digest != _digest(rows):
            raise IncompleteReplayInput(f"{table} table digest does not match rows")
    known = _mapping(payload["known_event_set"], "known event set")
    _exact_fields(known, _KNOWN_EVENT_SET_FIELDS, "known event set")
    if known["fixture_scope"] != "synthetic_test_only":
        raise IncompleteReplayInput("known event fixture scope is invalid")
    if not isinstance(known["set_id"], str) or not known["set_id"]:
        raise IncompleteReplayInput("known event set ID is invalid")
    events = _sequence(known["events"], "known events")
    for event in events:
        _exact_fields(_mapping(event, "known event"), _KNOWN_EVENT_FIELDS, "known event")
    _unique(events, "event_id", "known event")
    observed_known_digest = _valid_digest(known["digest"], "known event")
    if observed_known_digest != _digest(events):
        raise IncompleteReplayInput("known event digest does not match events")

    candidate_inputs = _mapping(payload["candidate_inputs"], "candidate inputs")
    if set(candidate_inputs) != {"event_ledger", "seed_graph", "seed_candidates"}:
        raise IncompleteReplayInput("candidate input fields are invalid")
    for lane in candidate_inputs:
        lane_rows = _sequence(candidate_inputs[lane], f"{lane} candidate inputs")
        for candidate in lane_rows:
            _exact_fields(
                _mapping(candidate, f"{lane} candidate"),
                _CANDIDATE_FIELDS[lane],
                f"{lane} candidate",
            )
        _unique(lane_rows, "candidate_id", f"{lane} candidate")

    observations = _sequence(payload["observations"], "observations")
    for observation in observations:
        _exact_fields(_mapping(observation, "observation"), _OBSERVATION_FIELDS, "observation")
    _unique(observations, "identity", "observation")
    components = _mapping(payload["components_by_candidate"], "components by candidate")
    memberships = _mapping(payload["memberships_by_candidate"], "memberships by candidate")
    if set(components) != set(memberships):
        raise IncompleteReplayInput("component and membership candidates are inconsistent")
    for candidate_id, component_value in components.items():
        component = _mapping(component_value, "component")
        _exact_fields(component, _COMPONENT_FIELDS, "component")
        member_identities = component.get("member_identities")
        if (
            not isinstance(member_identities, list)
            or not member_identities
            or len(member_identities) != len(set(member_identities))
        ):
            raise IncompleteReplayInput("component member identities are invalid")
        membership_rows = _sequence(memberships[candidate_id], "memberships")
        for membership in membership_rows:
            _exact_fields(_mapping(membership, "membership"), _MEMBERSHIP_FIELDS, "membership")
        _unique(membership_rows, "member_id", "membership")
        if {
            membership["member_identity"]
            for membership in membership_rows
            if isinstance(membership, Mapping)
        } != set(member_identities):
            raise IncompleteReplayInput("component and membership identities are inconsistent")

    receipts = _mapping(payload["receipts_by_member"], "receipts by member")
    observation_identities = {
        item["identity"] for item in observations if isinstance(item, Mapping)
    }
    if not set(receipts) <= observation_identities:
        raise IncompleteReplayInput("receipt member identity is unknown")
    for values in receipts.values():
        receipt_rows = _sequence(values, "member receipts")
        for receipt in receipt_rows:
            _exact_fields(_mapping(receipt, "receipt"), _RECEIPT_FIELDS, "receipt")
        _unique(receipt_rows, "row_id", "receipt")

    current_window = _window(payload["current_window"], "current window", cutoff)
    history_window = _window(payload["history_window"], "history window", cutoff)
    if history_window["end_date"] >= current_window["start_date"]:
        raise IncompleteReplayInput("history and current windows overlap")

    prior_snapshots = _sequence(payload["prior_snapshots"], "prior snapshots")
    prior_keys = []
    for snapshot_value in prior_snapshots:
        prior = _mapping(snapshot_value, "prior snapshot")
        _exact_fields(prior, _PRIOR_SNAPSHOT_FIELDS, "prior snapshot")
        signal_date = _date_value(prior.get("signal_date"), "prior signal date")
        if signal_date >= current_window["start_date"]:
            raise FutureLeakageDetected("future prior snapshot")
        key = (prior.get("signal_id"), prior.get("signal_date"), prior.get("run_id"))
        prior_keys.append(key)
    if len(prior_keys) != len(set(prior_keys)):
        raise IncompleteReplayInput("duplicate prior snapshot identity")

    provider_outputs = _sequence(payload["provider_outputs"], "provider outputs")
    for provider in provider_outputs:
        validate_replay_semantic_candidate(provider)
    _unique(provider_outputs, "candidate_id", "provider output")

    lane_rows = {
        lane: _sequence(candidate_inputs[lane], f"{lane} candidate inputs")
        for lane in _CANDIDATE_FIELDS
    }
    membership_rows = [
        item for values in memberships.values() for item in _sequence(values, "memberships")
    ]
    receipt_rows = [
        item for values in receipts.values() for item in _sequence(values, "member receipts")
    ]
    observed_section_identities = {
        "components": tuple(sorted(components)),
        "event_ledger_candidates": tuple(
            sorted(item["candidate_id"] for item in lane_rows["event_ledger"])
        ),
        "known_events": tuple(sorted(item["event_id"] for item in events)),
        "memberships": tuple(sorted(item["member_id"] for item in membership_rows)),
        "observations": tuple(sorted(item["identity"] for item in observations)),
        "prior_snapshots": tuple(sorted("|".join(str(part) for part in key) for key in prior_keys)),
        "provider_outputs": tuple(sorted(item["candidate_id"] for item in provider_outputs)),
        "receipts": tuple(sorted(item["row_id"] for item in receipt_rows)),
        "seed_candidates": tuple(
            sorted(item["candidate_id"] for item in lane_rows["seed_candidates"])
        ),
        "seed_graph_candidates": tuple(
            sorted(item["candidate_id"] for item in lane_rows["seed_graph"])
        ),
    }
    observed_section_counts = {
        key: len(identities) for key, identities in observed_section_identities.items()
    }
    for key in _SECTION_FIELDS:
        declared_count = section_counts[key]
        declared_identities = section_identities[key]
        if (
            isinstance(declared_count, bool)
            or not isinstance(declared_count, int)
            or declared_count != observed_section_counts[key]
        ):
            raise IncompleteReplayInput(f"{key} section count is inconsistent")
        if (
            not isinstance(declared_identities, list)
            or tuple(declared_identities) != observed_section_identities[key]
        ):
            raise IncompleteReplayInput(f"{key} section identities are inconsistent")

    source_rows = {
        table: _sequence(rows_by_table[table], f"{table} rows") for table in rows_by_table
    }
    if len(lane_rows["event_ledger"]) != len(source_rows["event_ledger"]):
        raise IncompleteReplayInput("event ledger candidate count is inconsistent")
    if len(lane_rows["seed_graph"]) != len(source_rows["seed_graph"]):
        raise IncompleteReplayInput("seed graph candidate count is inconsistent")
    if len(lane_rows["seed_candidates"]) != len(source_rows["seed_candidates"]):
        raise IncompleteReplayInput("seed candidate count is inconsistent")
    candidate_identities = {item["identity"] for values in lane_rows.values() for item in values}
    if candidate_identities != observation_identities:
        raise IncompleteReplayInput("candidate and observation identities are inconsistent")
    component_identities = {
        identity for component in components.values() for identity in component["member_identities"]
    }
    if component_identities != observation_identities:
        raise IncompleteReplayInput("component and observation identities are inconsistent")
    source_receipt_ids = {
        row["id"] for table in ("enriched_content", "raw_content") for row in source_rows[table]
    }
    if {row["row_id"] for row in receipt_rows} - source_receipt_ids:
        raise IncompleteReplayInput("receipt and source identities are inconsistent")
    source_prior_keys = {
        (row["signal_id"], row["signal_date"], row["run_id"])
        for row in source_rows["signal_candidates_v2"]
    }
    if set(prior_keys) != source_prior_keys:
        raise IncompleteReplayInput("prior snapshot and source identities are inconsistent")
    if {row["member_id"] for row in membership_rows} != {
        row["member_id"] for row in source_rows["signal_membership_v2"]
    }:
        raise IncompleteReplayInput("membership and source identities are inconsistent")

    observed_source_digest = _valid_digest(snapshot["source_digest"], "source")
    snapshot_without_digest = dict(snapshot)
    snapshot_without_digest.pop("source_digest")
    if observed_source_digest != _digest(snapshot_without_digest):
        raise IncompleteReplayInput("source digest does not match snapshot")

    if payload["human_review_state"] != "pending":
        raise IncompleteReplayInput("synthetic human review must remain pending")

    return ReplayInputBundle(
        artifact_version=payload["artifact_version"],
        contract_version=payload["contract_version"],
        cutoff=cutoff,
        scope=_freeze(scope),  # type: ignore[arg-type]
        source_snapshot=_freeze(snapshot),  # type: ignore[arg-type]
        known_event_set=_freeze(known),  # type: ignore[arg-type]
        candidate_inputs=_freeze(candidate_inputs),  # type: ignore[arg-type]
        observations=_freeze(observations),  # type: ignore[arg-type]
        components_by_candidate=_freeze(components),  # type: ignore[arg-type]
        memberships_by_candidate=_freeze(memberships),  # type: ignore[arg-type]
        receipts_by_member=_freeze(receipts),  # type: ignore[arg-type]
        current_window=current_window,
        history_window=history_window,
        prior_snapshots=_freeze(prior_snapshots),  # type: ignore[arg-type]
        provider_outputs=_freeze(provider_outputs),  # type: ignore[arg-type]
        human_review_state=payload["human_review_state"],
    )


def _observation_identity(value: object) -> str:
    market = getattr(value, "market", None)
    candidate_type = getattr(value, "candidate_type", None)
    term = getattr(value, "term", None)
    if not all(isinstance(item, str) and item for item in (market, candidate_type, term)):
        raise IncompleteReplayInput("Task 5C observation identity is invalid")
    return f"{market}|{candidate_type}|{term}"


def _scope_mapping(scope: ResolvedScope) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "client_scope_id": scope.client_scope_id,
            "market_scope": tuple(scope.market_scope),
            "brand_config_id": scope.brand_config_id,
            "audience_lens_ids": tuple(scope.audience_lens_ids),
            "theme_id": scope.theme_id,
            "run_id": scope.run_id,
        }
    )


def _adapter_section_identities(bundle: ReplayInputBundle) -> dict[str, tuple[str, ...]]:
    candidate_inputs = bundle.candidate_inputs
    if not isinstance(candidate_inputs, ReplayCandidateInputs):
        raise IncompleteReplayInput("adapter candidate inputs are invalid")
    candidate_only = candidate_inputs.candidate_only_receipts
    event_ids = tuple(
        sorted(
            receipt
            for item in candidate_inputs.event_ledger
            for receipt in candidate_only.get(_observation_identity(item), ())
        )
    )
    graph_ids = tuple(sorted(_observation_identity(item) for item in candidate_inputs.seed_graph))
    seed_ids = tuple(
        sorted(
            receipt
            for item in candidate_inputs.seed_candidates
            for receipt in candidate_only.get(_observation_identity(item), ())
        )
    )
    membership_ids = tuple(
        sorted(
            item.member_id for values in bundle.memberships_by_candidate.values() for item in values
        )
    )
    receipt_ids = tuple(
        sorted(
            f"{identity}|{item.row_id}"
            for identity, values in bundle.receipts_by_member.items()
            for item in values
        )
    )
    known_ids = tuple(
        sorted(
            item["event_id"]
            for item in bundle.known_event_set.get("events", ())
            if isinstance(item, Mapping)
        )
    )
    prior_ids = tuple(
        sorted(
            "|".join(str(item[key]) for key in ("signal_id", "signal_date", "run_id"))
            for item in bundle.prior_snapshots
        )
    )
    provider_ids = tuple(
        sorted(
            item["candidate_id"]
            for item in bundle.provider_outputs
            if isinstance(item, Mapping) and isinstance(item.get("candidate_id"), str)
        )
    )
    return {
        "components": tuple(sorted(bundle.components_by_candidate)),
        "event_ledger_candidates": event_ids,
        "known_events": known_ids,
        "memberships": membership_ids,
        "observations": tuple(_observation_identity(item) for item in bundle.observations),
        "prior_snapshots": prior_ids,
        "provider_outputs": provider_ids,
        "receipts": receipt_ids,
        "seed_candidates": seed_ids,
        "seed_graph_candidates": graph_ids,
    }


def _fact_timestamp(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _timestamp(value, field)
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    raise IncompleteReplayInput(f"{field} must be timezone aware")


def _source_receipt_facts(row: Mapping[str, object], source_table: str) -> tuple[object, ...]:
    return (
        row.get("id"),
        row.get("market"),
        row.get("source"),
        row.get("platform"),
        _fact_timestamp(row.get("published_at"), "source receipt published at"),
        _fact_timestamp(row.get("collected_at"), "source receipt collected at"),
        source_table,
    )


def _projected_receipt_facts(receipt: object) -> tuple[object, ...]:
    return (
        getattr(receipt, "row_id", None),
        getattr(receipt, "market", None),
        getattr(receipt, "source", None),
        getattr(receipt, "platform", None),
        _fact_timestamp(getattr(receipt, "published_at", None), "projected receipt published at"),
        _fact_timestamp(getattr(receipt, "collected_at", None), "projected receipt collected at"),
        getattr(receipt, "source_table", None),
    )


def _validate_adapter_source_facts(
    candidate_inputs: ReplayCandidateInputs,
    receipts_by_member: Mapping[str, object],
    rows_by_table: Mapping[str, object],
) -> None:
    try:
        event_inputs = extract_event_observations(tuple(rows_by_table["event_ledger"]))
        graph_inputs = extract_seed_graph_observations(tuple(rows_by_table["seed_graph"]))
        candidate_rows = tuple(rows_by_table["seed_candidates"])
        if any(row.get("status") not in ACCEPTED_SEED_CANDIDATE_STATUSES for row in candidate_rows):
            raise ValueError("candidate status")
        seed_inputs = tuple(
            replace(item, candidate_score=None)
            for item in extract_seed_candidate_observations(candidate_rows)
        )
    except (KeyError, TypeError, ValueError) as error:
        raise IncompleteReplayInput("source snapshot candidate facts are invalid") from error
    if event_inputs != candidate_inputs.event_ledger:
        raise IncompleteReplayInput("source snapshot event facts are inconsistent")
    if graph_inputs != candidate_inputs.seed_graph:
        raise IncompleteReplayInput("source snapshot graph facts are inconsistent")
    if seed_inputs != candidate_inputs.seed_candidates or any(
        item.candidate_score is not None for item in candidate_inputs.seed_candidates
    ):
        raise IncompleteReplayInput("source snapshot candidate facts are inconsistent")

    source_receipts = {
        _source_receipt_facts(row, table)
        for table in ("enriched_content", "raw_content")
        for row in rows_by_table[table]
    }
    projected_receipts = {
        _projected_receipt_facts(receipt)
        for values in receipts_by_member.values()
        for receipt in values
    }
    if not projected_receipts.issubset(source_receipts):
        raise IncompleteReplayInput("source snapshot projected receipt facts are inconsistent")


def validate_replay_input_bundle(bundle: ReplayInputBundle) -> ReplayInputBundle:
    if not isinstance(bundle, ReplayInputBundle):
        raise IncompleteReplayInput("replay input bundle is invalid")
    if bundle.contract_version != "2.0.0":
        raise IncompleteReplayInput("bundle contract version is invalid")
    scope = _mapping(bundle.scope, "bundle scope")
    _exact_fields(scope, _SCOPE_FIELDS, "bundle scope")
    _snapshot, counts, section_counts, section_identities, rows_by_table = (
        _validated_source_snapshot(bundle.source_snapshot, bundle.cutoff)
    )
    if not isinstance(bundle.candidate_inputs, ReplayCandidateInputs):
        raise IncompleteReplayInput("adapter candidate inputs are invalid")
    if bundle.current_window.complete_partitions is not False:
        raise IncompleteReplayInput("adapter current window must remain incomplete")
    history = _mapping(bundle.history_window, "adapter history window")
    if history.get("complete_partitions") is not False:
        raise IncompleteReplayInput("adapter history window must remain incomplete")
    if any(
        item.qualifies_evidence
        for values in bundle.memberships_by_candidate.values()
        for item in values
    ):
        raise IncompleteReplayInput("adapter membership must remain nonqualifying")

    candidate_inputs = bundle.candidate_inputs
    _validate_adapter_source_facts(candidate_inputs, bundle.receipts_by_member, rows_by_table)
    if counts["event_ledger"] != len(candidate_inputs.event_ledger):
        raise IncompleteReplayInput("source snapshot event row count is inconsistent")
    if counts["seed_graph"] != len(candidate_inputs.seed_graph):
        raise IncompleteReplayInput("source snapshot graph row count is inconsistent")
    if counts["seed_candidates"] != len(candidate_inputs.seed_candidates):
        raise IncompleteReplayInput("source snapshot candidate row count is inconsistent")
    source_receipt_ids = {
        row["id"] for table in ("enriched_content", "raw_content") for row in rows_by_table[table]
    }
    projected_receipt_ids = {
        item.row_id for values in bundle.receipts_by_member.values() for item in values
    }
    if projected_receipt_ids - source_receipt_ids:
        raise IncompleteReplayInput("source snapshot receipt identities are inconsistent")
    event_source_ids = {row["ledger_id"] for row in rows_by_table["event_ledger"]}
    event_candidate_ids = set(_adapter_section_identities(bundle)["event_ledger_candidates"])
    if event_candidate_ids != event_source_ids:
        raise IncompleteReplayInput("source snapshot event identities are inconsistent")
    seed_source_ids = {row["candidate_id"] for row in rows_by_table["seed_candidates"]}
    seed_candidate_ids = set(_adapter_section_identities(bundle)["seed_candidates"])
    if seed_candidate_ids != seed_source_ids:
        raise IncompleteReplayInput("source snapshot candidate identities are inconsistent")
    graph_source_identities = {
        f"{row['market']}|keyword|{row['term']}" for row in rows_by_table["seed_graph"]
    }
    if set(_adapter_section_identities(bundle)["seed_graph_candidates"]) != graph_source_identities:
        raise IncompleteReplayInput("source snapshot graph identities are inconsistent")

    observed_sections = _adapter_section_identities(bundle)
    for key in _SECTION_FIELDS:
        if section_counts[key] != len(observed_sections[key]):
            raise IncompleteReplayInput(f"{key} section count is inconsistent")
        if tuple(section_identities[key]) != observed_sections[key]:
            raise IncompleteReplayInput(f"{key} section identities are inconsistent")
    if bundle.human_review_state != "pending":
        raise IncompleteReplayInput("adapter human review must remain pending")
    return bundle


def _validate_source_row_order(value: object) -> None:
    snapshot = _mapping(value, "source snapshot")
    source_row_fields = (
        _ADAPTER_SOURCE_ROW_FIELDS
        if snapshot.get("snapshot_id") == "task5c_adapter_snapshot_v1"
        else _SOURCE_ROW_FIELDS
    )
    rows_by_table = _mapping(snapshot.get("rows_by_table"), "source rows")
    for table, expected_fields in source_row_fields.items():
        for row in _sequence(rows_by_table.get(table), f"{table} rows"):
            if tuple(_mapping(row, f"{table} row")) != expected_fields:
                raise IncompleteReplayInput(f"{table} row field order is invalid")


def adapt_dynamic_result_to_replay_input(
    result: DynamicSignalRunResult,
    *,
    scope: ResolvedScope,
    source_snapshot: Mapping[str, object],
    known_event_set: Mapping[str, object],
    history_window: Mapping[str, object],
    prior_snapshots: tuple[Mapping[str, object], ...],
    provider_outputs: tuple[Mapping[str, object], ...],
    human_review_state: str,
) -> ReplayInputBundle:
    if not isinstance(result, DynamicSignalRunResult):
        raise IncompleteReplayInput("Task 5C result is invalid")
    if not isinstance(scope, ResolvedScope):
        raise IncompleteReplayInput("Task 5C scope is invalid")
    if result.run_id != scope.run_id:
        raise IncompleteReplayInput("Task 5C scope run ID does not match result")
    if result.contract_version != scope.contract_version:
        raise IncompleteReplayInput("Task 5C scope contract does not match result")
    if (
        result.dry_run is not True
        or result.persistable_count != 0
        or result.persistence_result is not None
        or not result.row_counts
        or any(value != 0 for value in result.row_counts.values())
    ):
        raise IncompleteReplayInput("Task 5C result must be a zero-write dry-run")
    projection = result.evidence_projection
    if projection is None:
        raise IncompleteReplayInput("Task 5C evidence projection is required")
    history = _mapping(history_window, "adapter history window")
    if history.get("complete_partitions") is not False:
        raise IncompleteReplayInput("adapter history window must remain incomplete")
    if projection.source_window.complete_partitions is not False or any(
        window.complete_partitions is not False for window in result.source_windows.values()
    ):
        raise IncompleteReplayInput("Task 5C windows must remain incomplete")
    memberships = {
        key: tuple(sorted(values, key=lambda item: item.member_identity))
        for key, values in sorted(projection.memberships_by_component.items())
    }
    if any(item.qualifies_evidence for values in memberships.values() for item in values):
        raise IncompleteReplayInput("Task 5C membership must remain nonqualifying")
    receipts = {
        key: tuple(sorted(values, key=lambda item: item.row_id))
        for key, values in sorted(projection.receipts_by_member.items())
    }
    observations = tuple(sorted(result.observations, key=_observation_identity))
    candidate_inputs = ReplayCandidateInputs(
        event_ledger=result.candidate_inputs.event_ledger,
        seed_graph=result.candidate_inputs.seed_graph,
        seed_candidates=result.candidate_inputs.seed_candidates,
        candidate_only_receipts=MappingProxyType(
            {
                key: tuple(values)
                for key, values in sorted(projection.candidate_only_receipts.items())
            }
        ),
        unresolved_sample_ids=tuple(projection.unresolved_sample_ids),
        missing_work=tuple(result.missing_work),
        source_windows=MappingProxyType(dict(sorted(result.source_windows.items()))),
    )
    components = {
        key: MappingProxyType(
            {
                "market": values[0].member_identity.split("|", 1)[0],
                "member_identities": tuple(item.member_identity for item in values),
            }
        )
        for key, values in memberships.items()
        if values
    }
    known = _freeze(_mapping(known_event_set, "adapter known event set"))
    prior = tuple(sorted(prior_snapshots, key=canonical_typed_json))
    providers = tuple(sorted(provider_outputs, key=canonical_typed_json))
    _validate_source_row_order(source_snapshot)
    bundle = ReplayInputBundle(
        artifact_version="replay_input_v1",
        contract_version=result.contract_version,
        cutoff=projection.source_window.end_date,
        scope=_scope_mapping(scope),
        source_snapshot=_freeze(_mapping(source_snapshot, "adapter source snapshot")),
        known_event_set=known,  # type: ignore[arg-type]
        candidate_inputs=candidate_inputs,  # type: ignore[arg-type]
        observations=observations,  # type: ignore[arg-type]
        components_by_candidate=MappingProxyType(components),
        memberships_by_candidate=MappingProxyType(memberships),
        receipts_by_member=MappingProxyType(receipts),
        current_window=projection.source_window,  # type: ignore[arg-type]
        history_window=_freeze(history),  # type: ignore[arg-type]
        prior_snapshots=prior,
        provider_outputs=providers,
        human_review_state=human_review_state,
    )
    return validate_replay_input_bundle(bundle)


@dataclass(frozen=True, slots=True)
class Metric:
    numerator: int
    denominator: int
    rate: float

    def __post_init__(self) -> None:
        if self.denominator < 1 or not 0 <= self.numerator <= self.denominator:
            raise ValueError("metric numerator and denominator are invalid")
        if self.rate != self.numerator / self.denominator:
            raise ValueError("metric rate does not match its denominator")

    def as_tuple(self) -> tuple[int, int, float]:
        return self.numerator, self.denominator, self.rate


@dataclass(frozen=True, slots=True)
class KnownEvent:
    event_id: str
    market: str
    member_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("known event ID is required")
        if self.market not in MARKETS:
            raise ValueError("known event market is invalid")
        identities = tuple(sorted(set(self.member_identities)))
        if not identities:
            raise ValueError("known event identities are required")
        object.__setattr__(self, "member_identities", identities)


@dataclass(frozen=True, slots=True)
class ReplaySignal:
    signal_id: str
    market: str
    cluster_signature: str
    member_identities: tuple[str, ...]
    source_max_observed_at: datetime
    membership_complete: bool
    evidence_ready: bool
    geo_proven: bool

    def __post_init__(self) -> None:
        if not isinstance(self.signal_id, str) or not self.signal_id:
            raise ValueError("signal ID is required")
        if self.market not in MARKETS:
            raise ValueError("signal market is invalid")
        if not isinstance(self.cluster_signature, str) or not self.cluster_signature:
            raise ValueError("cluster signature is required")
        identities = tuple(sorted(set(self.member_identities)))
        if not identities:
            raise ValueError("signal member identities are required")
        object.__setattr__(self, "member_identities", identities)
        if self.source_max_observed_at.tzinfo is None:
            raise ValueError("source observation time must be timezone aware")
        for field in ("membership_complete", "evidence_ready", "geo_proven"):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"{field} must be boolean")


@dataclass(frozen=True, slots=True)
class ReplayMetrics:
    cutoff: date
    row_count: int
    signal_count: int
    known_event_recall: Metric
    known_event_recall_by_market: Mapping[str, Metric]
    duplicate_rate: Metric
    foreign_leakage: Metric
    receipt_completeness: Metric
    evidence_coverage: Metric
    geo_coverage: Metric
    review_sample: tuple[ReplaySignal, ...]
    human_coherence_status: str = "pending"


def _metric(numerator: int, denominator: int) -> Metric:
    if denominator < 1:
        raise IncompleteReplayInput("metric denominator must be nonzero")
    return Metric(numerator, denominator, numerator / denominator)


def _review_sample(
    signals: tuple[ReplaySignal, ...],
    cutoff: date,
    sample_per_market: int,
) -> tuple[ReplaySignal, ...]:
    if (
        isinstance(sample_per_market, bool)
        or not isinstance(sample_per_market, int)
        or sample_per_market < 1
    ):
        raise ValueError("sample_per_market must be a positive integer")
    selected = []
    for market in MARKETS:
        market_signals = tuple(signal for signal in signals if signal.market == market)
        if len(market_signals) < sample_per_market:
            raise IncompleteReplayInput(
                f"review sample requires {sample_per_market} signals in every market"
            )
        ranked = sorted(
            market_signals,
            key=lambda signal: hashlib.sha256(
                f"{cutoff.isoformat()}|{market}|{signal.signal_id}".encode()
            ).hexdigest(),
        )
        selected.extend(ranked[:sample_per_market])
    return tuple(selected)


def evaluate_replay(
    *,
    cutoff: date,
    input_row_count: int,
    signals: tuple[ReplaySignal, ...],
    known_events: tuple[KnownEvent, ...],
    sample_per_market: int = 5,
) -> ReplayMetrics:
    if cutoff != REPLAY_CUTOFF:
        raise IncompleteReplayInput(f"certified cutoff must be {REPLAY_CUTOFF.isoformat()}")
    if input_row_count != EXPECTED_EVENT_LEDGER_ROWS:
        raise IncompleteReplayInput(
            f"certified replay requires exactly {EXPECTED_EVENT_LEDGER_ROWS} event rows"
        )
    ordered_signals = tuple(sorted(signals, key=lambda signal: (signal.market, signal.signal_id)))
    ordered_events = tuple(sorted(known_events, key=lambda event: (event.market, event.event_id)))
    if not ordered_signals or not ordered_events:
        raise IncompleteReplayInput("replay signals and known events must be nonempty")
    cutoff_end = datetime.combine(cutoff, time.max, tzinfo=UTC)
    for signal in ordered_signals:
        if signal.source_max_observed_at.astimezone(UTC) > cutoff_end:
            raise FutureLeakageDetected(f"future source timestamp for {signal.signal_id}")
    if any(not any(signal.market == market for signal in ordered_signals) for market in MARKETS):
        raise IncompleteReplayInput("certified replay requires signals in every market")
    if any(not any(event.market == market for event in ordered_events) for market in MARKETS):
        raise IncompleteReplayInput("certified replay requires known events in every market")
    signal_ids = tuple(signal.signal_id for signal in ordered_signals)
    if len(signal_ids) != len(set(signal_ids)):
        raise IncompleteReplayInput("duplicate signal ID")

    recall_by_market = {}
    for market in MARKETS:
        market_events = tuple(event for event in ordered_events if event.market == market)
        recalled = sum(
            any(
                signal.market == event.market
                and set(event.member_identities).intersection(signal.member_identities)
                for signal in ordered_signals
            )
            for event in market_events
        )
        recall_by_market[market] = _metric(recalled, len(market_events))
    recalled = sum(metric.numerator for metric in recall_by_market.values())

    cluster_counts: dict[tuple[str, str], int] = {}
    for signal in ordered_signals:
        key = (signal.market, signal.cluster_signature)
        cluster_counts[key] = cluster_counts.get(key, 0) + 1
    duplicate_count = sum(count - 1 for count in cluster_counts.values() if count > 1)
    foreign_count = sum(
        any(identity.split("|", 1)[0] != signal.market for identity in signal.member_identities)
        for signal in ordered_signals
    )
    signal_count = len(ordered_signals)
    return ReplayMetrics(
        cutoff=cutoff,
        row_count=input_row_count,
        signal_count=signal_count,
        known_event_recall=_metric(recalled, len(ordered_events)),
        known_event_recall_by_market=MappingProxyType(recall_by_market),
        duplicate_rate=_metric(duplicate_count, signal_count),
        foreign_leakage=_metric(foreign_count, signal_count),
        receipt_completeness=_metric(
            sum(signal.membership_complete for signal in ordered_signals), signal_count
        ),
        evidence_coverage=_metric(
            sum(signal.evidence_ready for signal in ordered_signals), signal_count
        ),
        geo_coverage=_metric(sum(signal.geo_proven for signal in ordered_signals), signal_count),
        review_sample=_review_sample(ordered_signals, cutoff, sample_per_market),
    )


def render_dry_run() -> str:
    return json.dumps(
        {
            "mode": "dry_run",
            "cutoff": REPLAY_CUTOFF.isoformat(),
            "expected_event_ledger_rows": EXPECTED_EVENT_LEDGER_ROWS,
            "query_limit": QUERY_LIMIT,
            "markets": list(MARKETS),
            "human_coherence_status": "pending",
            "certified": False,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


# --- The apply CLI ------------------------------------------------------------
#
# The one-shot Cloud Run job entry for the approved producing run. It refuses
# every identity except the staging service account, the exact mechanism the
# migration used, and it binds the run to the copied source window: a trend
# date outside it would read rows the manifest does not govern.


def run_dynamic_signal_identity_v2(*args, **kwargs):
    from src.analysis.open_intelligence import pipeline

    previous = pipeline.compose_components
    previous_rules = pipeline.GraphRules
    pipeline.compose_components = build_components_with_anchored_semantics
    pipeline.GraphRules = AnchoredGraphRules
    try:
        return pipeline.run_dynamic_signal_identity(*args, **kwargs)
    finally:
        pipeline.compose_components = previous
        pipeline.GraphRules = previous_rules


def composition_probe_invariants(components, edges, *, member_ceiling: int) -> dict[str, int]:
    component_sizes = [len(component.member_identities) for component in components]
    cross_market = sum(
        any(
            not identity.startswith(f"{component.market}|")
            for identity in component.member_identities
        )
        for component in components
    )
    unanchored = 0
    for edge in edges:
        votes = edge.votes
        anchored = votes.co_occurrence or votes.embedding_similarity or votes.shared_creator
        supported = votes.temporal_overlap or votes.independent_source_family
        if not (anchored and supported):
            unanchored += 1
    return {
        "max_component_member_count": max(component_sizes, default=0),
        "oversized_component_count": sum(size > member_ceiling for size in component_sizes),
        "cross_market_component_count": cross_market,
        "unanchored_edge_count": unanchored,
    }


def run_composition_v2_probe() -> str:
    from time import perf_counter

    from google.auth import default as load_default_credentials
    from google.auth.transport.requests import Request
    from google.cloud import bigquery
    from scripts.migrations.create_open_intelligence_v2 import (
        MigrationPlan,
        _authorized_credentials,
    )
    from scripts.staging.copy_open_intelligence_replay_sources import (
        COPY_RUN_ID,
        END_DATE,
        SERVICE_ACCOUNT,
        TARGET_DATASET,
    )
    from scripts.staging.copy_open_intelligence_replay_sources import (
        PROJECT as STAGING_PROJECT,
    )
    from src.contracts.open_intelligence import resolve_client_scope

    plan = MigrationPlan(
        target="staging",
        project=STAGING_PROJECT,
        dataset=TARGET_DATASET,
        location="US",
        service_account=SERVICE_ACCOUNT,
        statements=(),
        source_lab_upgrade_statements=(),
        rollback_statements=(),
    )
    credentials = _authorized_credentials(
        plan,
        lambda: load_default_credentials(),
        Request,
    )
    client = bigquery.Client(
        project=STAGING_PROJECT,
        credentials=credentials,
        location=plan.location,
    )
    scope = resolve_client_scope(
        run_id="run_20260827_composition_v2_probe",
        client_scope_id="ogilvy_default",
        market_scope=("za", "ng", "ke"),
        audience_lens_ids=(),
    )
    created_at = datetime.now(UTC)
    rule_bundle = load_composition_rules_v2(
        COMPOSITION_RULES_V2_PATH,
        created_at,
        require_certified=False,
    )
    provider = LexicalSimilarityProviderV2(pair_ceiling=rule_bundle.graph_rules.pair_ceiling)
    started = perf_counter()
    result = run_dynamic_signal_identity_v2(
        END_DATE,
        scope,
        client,
        TARGET_DATASET,
        False,
        rule_bundle=rule_bundle,
        semantic_provider=provider,
    )
    elapsed_seconds = round(perf_counter() - started, 3)
    if result.error_state is not None:
        raise ApplyRefusal(f"composition v2 probe failed: {result.error_state}")
    lexical_pairs = provider.candidate_pairs(result.observations)
    candidate_pairs = build_anchored_candidate_pairs(
        result.observations,
        lexical_pairs,
        rule_bundle.graph_rules.pair_ceiling,
    )
    scores = provider.similarities(result.observations, candidate_pairs)
    edges = build_anchored_edges(
        result.observations,
        candidate_pairs,
        scores,
        rule_bundle.graph_rules,
    )
    invariants = composition_probe_invariants(
        result.components,
        edges,
        member_ceiling=rule_bundle.graph_rules.component_member_ceiling,
    )
    try:
        import resource

        peak_memory_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3)
    except ImportError:
        peak_memory_mb = None
    report = {
        "contract_version": "composition_v2_probe_v1",
        "trend_date": END_DATE.isoformat(),
        "rule_version": rule_bundle.rule_version,
        "provider_version": provider.version,
        "input_counts": dict(result.input_counts),
        "observation_count": len(result.observations),
        "candidate_pair_count": len(candidate_pairs),
        "edge_count": len(edges),
        "component_count": len(result.components),
        **invariants,
        "peak_memory_mb": peak_memory_mb,
        "elapsed_seconds": elapsed_seconds,
        "persisted": False,
        "model_calls": 0,
        "funded_calls": 0,
    }
    report["receipt_id"] = (
        "composition_v2_probe_"
        + hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    return json.dumps(report, sort_keys=True, separators=(",", ":"))


def persist_producing_batch(
    *,
    client,
    batch,
    rule_bundle,
    dry_run: bool,
    writer_identity: str | None = None,
):
    if writer_identity is None:
        writer_identity = persistence.TARGET_WRITER_IDENTITY
    persistence_client = (
        persistence.PersistenceTarget(
            project=persistence.TARGET_PROJECT,
            dataset=persistence.TARGET_DATASET,
            location=persistence.TARGET_LOCATION,
            writer_identity=writer_identity,
        )
        if dry_run
        else client
    )
    return persistence.persist_open_intelligence_rows(
        project=persistence.TARGET_PROJECT,
        dataset=persistence.TARGET_DATASET,
        client=persistence_client,
        batch=batch,
        rule_bundle=rule_bundle,
        dry_run=dry_run,
        writer_identity=writer_identity,
    )


# The retained v1 apply identity, kept for historical inspection of the r16 chain only.
# A fresh run never binds to it: the protected apply takes its job, principal and image
# from the v2 origin row the issued authority carries.
R3_APPLY_IDENTITY = execution_approval._OPERATION_CONTRACTS["r3_apply"]["identity"]
_MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
_MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
_HISTORICAL_MODES = ("historical_read", "historical_replay")
_GENERATION_FIELDS = ("origin_registry_sha256", "resource_manifest_sha256")
_HEX_64 = re.compile(r"[0-9a-f]{64}")


def _operation_binding(operation, *, manifest_version, mode, registry):
    """One origin row and its exact binding for the operation, under an explicit mode.

    The caller names the manifest family and the mode; nothing is inferred from the
    registry, the environment or the operation name.
    """
    from src.analysis.open_intelligence.execution_origins import OriginRegistry, select_origin

    if type(registry) is not OriginRegistry or not isinstance(operation, str):
        raise ApplyRefusal("execution origin registry is invalid")
    matches = [
        origin
        for origin in registry.values()
        if origin.manifest_version == manifest_version and operation in origin.operation_bindings
    ]
    if len(matches) != 1:
        raise ApplyRefusal(f"{operation} has no single execution origin")
    origin = select_origin(
        manifest_version=matches[0].manifest_version,
        contract_sha256=matches[0].contract_sha256,
        mode=mode,
        registry=registry,
    )
    return origin, origin.operation_bindings[operation]


def _generation_pair(value):
    pair = tuple(getattr(value, name, None) for name in _GENERATION_FIELDS)
    if any(not isinstance(digest, str) or _HEX_64.fullmatch(digest) is None for digest in pair):
        return None
    return pair


def _require_runtime_profile(authority, operation, *, generation, binding):
    """Bind the issued authority to the v2 origin row its generation names.

    Job, principal, image regex and repository come from that row; the pair must be the
    one the fresh path resolved before the load, and the binding must be the same one.
    """
    from src.analysis.open_intelligence.execution_generations import TrustedGeneration

    issued = getattr(authority, "generation", None)
    if (
        type(issued) is not TrustedGeneration
        or _generation_pair(issued) != _generation_pair(generation)
        or getattr(authority, "operation", None) != operation
    ):
        raise ApplyRefusal(f"{operation} runtime generation differs from the active pair")
    origin, resolved = _operation_binding(
        operation, manifest_version=_MANIFEST_V2, mode="new_consume", registry=issued.registry
    )
    manifest = getattr(authority, "manifest", None)
    execution_name = getattr(authority, "execution_name", None)
    image_uri = getattr(authority, "image_uri", None)
    if (
        resolved != binding
        or getattr(authority, "job_resource", None) != resolved.job_resource
        or getattr(manifest, "job_resource", None) != resolved.job_resource
        or getattr(manifest, "service_identity", None) != resolved.service_identity
        or not isinstance(execution_name, str)
        or not execution_name.startswith(resolved.job_resource + "/executions/")
        or not isinstance(image_uri, str)
        or re.fullmatch(origin.image_uri_regex, image_uri) is None
        or getattr(manifest, "image_uri", None) != image_uri
    ):
        raise ApplyRefusal(f"{operation} runtime identity differs from its execution origin")
    return origin, resolved


def _same_generation(authority, consumption):
    pair = _generation_pair(getattr(authority, "generation", None))
    return pair is not None and pair == _generation_pair(consumption)


def _admit_chain_row(row, operation, *, mode, generation):
    """Admit one v2 result chain row through the trusted catalogue and the same pair."""
    from src.analysis.open_intelligence.execution_generations import load_trusted_generation
    from src.analysis.open_intelligence.execution_origins import OriginRefusal

    if mode not in _HISTORICAL_MODES:
        raise ApplyRefusal(f"{operation} result chain mode is not historical")
    pair = _generation_pair(SimpleNamespace(**{name: row.get(name) for name in _GENERATION_FIELDS}))
    if pair is None or pair != _generation_pair(generation):
        raise ApplyRefusal(f"{operation} result chain generation differs")
    try:
        admitted = load_trusted_generation(*pair)
    except OriginRefusal as error:
        raise ApplyRefusal(f"{operation} result chain generation is untrusted") from error
    manifest_json = row.get("canonical_manifest_json")
    try:
        manifest_payload = json.loads(manifest_json) if isinstance(manifest_json, str) else None
        manifest = execution_approval.validate_execution_manifest(
            manifest_payload, mode=mode, registry=admitted.registry
        )
        manifest_sha256 = execution_approval.manifest_sha256(
            manifest_payload, mode=mode, registry=admitted.registry
        )
    except (TypeError, ValueError, execution_approval.ApprovalRefusal) as error:
        raise ApplyRefusal(f"{operation} result chain manifest is invalid") from error
    _origin, binding = _operation_binding(
        operation, manifest_version=_MANIFEST_V2, mode=mode, registry=admitted.registry
    )
    execution_name = row.get("execution_name")
    if (
        manifest.manifest_version != _MANIFEST_V2
        or manifest.operation != operation
        or row.get("manifest_sha256") != manifest_sha256
        or manifest.job_resource != binding.job_resource
        or manifest.service_identity != binding.service_identity
        or row.get("job_resource") != binding.job_resource
        or row.get("source_sha") != manifest.source_sha
        or row.get("image_uri") != manifest.image_uri
        or not isinstance(execution_name, str)
        or not execution_name.startswith(binding.job_resource + "/executions/")
    ):
        raise ApplyRefusal(f"{operation} result chain binding differs")
    return manifest, binding


def _r3_apply_profile(*, mode):
    """The v2 apply binding from the packaged active generation, under an explicit mode."""
    from src.analysis.open_intelligence.execution_generations import active_generation

    generation = active_generation()
    origin, binding = _operation_binding(
        "r3_apply", manifest_version=_MANIFEST_V2, mode=mode, registry=generation.registry
    )
    return generation, origin, binding


R3_RUN_ID = "run_20260903_dynamic_apply_v2_r16"
# Approved 3 Sep 2026 (content-bound quality review): when this run's own
# review is pending, the review registered on the prior run is authority for
# this run when the rows are the same. Move both together, every run.
R3_PRIOR_RUN_ID = "run_20260903_dynamic_apply_v2_r14"
# The tables one source-copy run covers; the copier and the review procedure use the same four.
# The review packet is built from the run's own rows, so the human review can only follow the
# run. Until a receipt is registered the apply carries this marker: nothing is promotable and
# every persisted row stays blocked, which is the state the release step lifts.
PENDING_REVIEW_RECEIPT = MappingProxyType(
    {
        "review_contract_version": "dynamic_quality_review_v1",
        "run_id": R3_RUN_ID,
        "review_state": "pending",
    }
)


def _contract_ordered_review_receipt(receipt: Mapping[str, object]) -> dict[str, object]:
    """A stored receipt in the field order the review contract validates.

    The store keeps canonical JSON (sorted keys) with the review time as text;
    the validator demands REVIEW_RECEIPT_FIELDS order and a timezone aware
    datetime, so a receipt read back is reshaped before any check.
    """
    from src.analysis.open_intelligence import live_quality

    if set(receipt) != set(live_quality.REVIEW_RECEIPT_FIELDS):
        raise ApplyRefusal("quality review receipt fields are invalid")
    ordered = {field: receipt[field] for field in live_quality.REVIEW_RECEIPT_FIELDS}
    for field in (
        "reviewed_evidence_ids",
        "foreign_market_evidence_ids",
        "factual_conflict_evidence_ids",
        "uncertain_evidence_ids",
    ):
        ordered[field] = tuple(ordered[field])
    reviewed_at = ordered["reviewed_at"]
    if isinstance(reviewed_at, str):
        ordered["reviewed_at"] = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    return ordered


def _review_is_pending(receipt: Mapping[str, object]) -> bool:
    return isinstance(receipt, Mapping) and dict(receipt) == dict(PENDING_REVIEW_RECEIPT)


R3_SOURCE_COPY_TABLES = frozenset(
    {"event_ledger", "seed_graph", "seed_candidates", "enriched_content"}
)
R3_SIGNAL_DATE = date(2026, 9, 3)
R3_WINDOW_START = date(2026, 8, 21)
# Approved by Albert on 2026-09-03: readiness admits evidence on freshness and
# availability alone; geo confidence weakens decision strength through the
# APPROVED_GEO_FLOORS instead of erasing the evidence. On the first run of the
# 21 August to 3 September window 49 of 64 evidence rows carried no regional
# marker and no signal survived any admission floor from 0.4 up.
# This floor is the replay chain's own and stays at the approved value. The daily
# chain carries a separate floor of its own, ``DAILY_READINESS_GEO_FLOOR`` in
# daily_native_clients, raised off zero because its composer resolves every row
# through the geographic scope kernel: there an unresolved row is withheld from
# readiness rather than admitted with a zero, so a zero there means measured and
# not in market, which is the opposite of what a zero means here. Neither floor
# follows the other, and the two names say which chain each belongs to so that a
# reader cannot take one for the other.
REPLAY_READINESS_GEO_FLOOR = 0.0
R3_EXECUTION_PROOF_CONTRACT_VERSION = "r3-execution-proof-v1"
_R3_DURABLE_ARTIFACT_CONTEXT: object | None = None


def _execution_approval_artifact_bytes(name: str) -> bytes:
    if isinstance(_R3_DURABLE_ARTIFACT_CONTEXT, Mapping):
        content = _R3_DURABLE_ARTIFACT_CONTEXT.get(name)
    else:
        reader = getattr(_R3_DURABLE_ARTIFACT_CONTEXT, "read", None)
        content = reader(name) if callable(reader) else None
    if not isinstance(content, bytes):
        raise ApplyRefusal("r3 execution approval artifact is unavailable")
    return content


def _build_r3_execution_artifacts(
    *,
    review_receipt: Mapping[str, object],
    exposure_proof,
    exposure_receipts: tuple[Mapping[str, object], ...],
    completeness_rows: list[Mapping[str, object]],
) -> dict[str, bytes]:
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    natural_keys = tuple(
        (item["source_family"], item["exposure_date"]) for item in exposure_receipts
    )
    return {
        "r3_contract": canonical_bytes(
            {
                "contract_version": "dynamic_v2_quality_r3_v1",
                "run_id": R3_RUN_ID,
                "signal_date": R3_SIGNAL_DATE,
                "row_families": tuple(persistence.TABLE_BINDINGS),
            }
        ),
        "quality_review_receipt": canonical_bytes(review_receipt),
        "exposure_execution_proof": canonical_bytes(exposure_proof),
        "exposure_receipt_readback": canonical_bytes(exposure_receipts),
        "inserted_natural_key_set": canonical_bytes(natural_keys),
        "config": COMPOSITION_RULES_V2_PATH.read_bytes(),
        "source_window_receipt_set": canonical_bytes(tuple(completeness_rows)),
    }


class _R3ArtifactProvider:
    def __init__(self, client: object, *, version, mode, generation) -> None:
        self._client = client
        self._version = version
        self._mode = mode
        self._generation = generation
        self._artifacts: dict[str, bytes] | None = None
        self.review_receipt: Mapping[str, object] | None = None
        self.prior_review = None
        self.exposure_proof: Mapping[str, object] | None = None
        self.exposure_receipts: tuple[Mapping[str, object], ...] = ()
        self.source_window_receipts: tuple[Mapping[str, object], ...] = ()

    def _query(self, sql: str, *, parameters=(), max_results: int = 2):
        from google.cloud import bigquery

        job = self._client.query(
            sql,
            location="US",
            job_config=bigquery.QueryJobConfig(
                use_legacy_sql=False,
                query_parameters=list(parameters),
            ),
            retry=None,
            job_retry=None,
        )
        return tuple(job.result(max_results=max_results, retry=None, job_retry=None))

    def _quality_review(self) -> Mapping[str, object]:
        from google.api_core import exceptions as api_exceptions
        from google.cloud import bigquery
        from src.analysis.open_intelligence.brain_contract import canonical_bytes

        try:
            rows = self._query(
                "CALL `ogilvy-trends-v2.trends_v2_staging."
                "sp_read_open_intelligence_quality_review_receipt_v1`(@run_id)",
                parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", R3_RUN_ID),),
            )
        except api_exceptions.BadRequest as error:
            # The routine asserts exactly one registered receipt, so before the human
            # review it raises this named assertion rather than returning no rows.
            if "quality_review_receipt_unavailable" not in str(error):
                raise
            return dict(PENDING_REVIEW_RECEIPT)
        if not rows:
            return dict(PENDING_REVIEW_RECEIPT)
        if len(rows) != 1:
            raise ApplyRefusal("r3 quality review receipt authority is unavailable")
        row = dict(rows[0])
        canonical_json = row.get("canonical_review_receipt_json")
        if row.get("run_id") != R3_RUN_ID or not isinstance(canonical_json, str):
            raise ApplyRefusal("r3 quality review receipt authority differs")
        try:
            receipt = json.loads(canonical_json)
        except json.JSONDecodeError as error:
            raise ApplyRefusal("r3 quality review receipt authority is invalid") from error
        if (
            not isinstance(receipt, dict)
            or receipt.get("run_id") != R3_RUN_ID
            or canonical_bytes(receipt).decode("utf-8") != canonical_json
        ):
            raise ApplyRefusal("r3 quality review receipt authority is invalid")
        return receipt

    def _prior_quality_review(self):
        """The review registered on the prior run, with that run's own digests.

        Approved 3 Sep 2026. Returns None when the prior run carries no review;
        every digest comes from BigQuery over the prior run's persisted rows so
        the apply step can prove the receipt is genuine for those rows and that
        the rows equal the batch it is about to persist.
        """
        from google.api_core import exceptions as api_exceptions
        from google.cloud import bigquery
        from src.analysis.open_intelligence import live_quality
        from src.analysis.open_intelligence.brain_contract import canonical_bytes

        try:
            rows = self._query(
                "CALL `ogilvy-trends-v2.trends_v2_staging."
                "sp_read_open_intelligence_quality_review_receipt_v1`(@run_id)",
                parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", R3_PRIOR_RUN_ID),),
            )
        except api_exceptions.BadRequest as error:
            if "quality_review_receipt_unavailable" not in str(error):
                raise
            return None
        if len(rows) != 1:
            return None
        row = dict(rows[0])
        canonical_json = row.get("canonical_review_receipt_json")
        if row.get("run_id") != R3_PRIOR_RUN_ID or not isinstance(canonical_json, str):
            raise ApplyRefusal("prior quality review receipt authority differs")
        try:
            receipt = json.loads(canonical_json)
        except json.JSONDecodeError as error:
            raise ApplyRefusal("prior quality review receipt authority is invalid") from error
        if (
            not isinstance(receipt, dict)
            or receipt.get("run_id") != R3_PRIOR_RUN_ID
            or canonical_bytes(receipt).decode("utf-8") != canonical_json
        ):
            raise ApplyRefusal("prior quality review receipt authority is invalid")
        receipt = _contract_ordered_review_receipt(receipt)
        digests = self._query(
            "SELECT "
            f"{persistence.candidate_projection_digest_sql(R3_PRIOR_RUN_ID)} AS projection, "
            f"{live_quality.review_packet_digest_sql(R3_PRIOR_RUN_ID, sql_expression=False)} "
            "AS packet, "
            f"{persistence.candidate_projection_content_digest_sql(R3_PRIOR_RUN_ID)} "
            "AS content_projection, "
            f"{live_quality.review_packet_content_digest_sql(R3_PRIOR_RUN_ID, sql_expression=False)} "
            "AS content_packet"
        )
        if len(digests) != 1:
            raise ApplyRefusal("prior run control digest cardinality differs")
        digest_row = dict(digests[0])
        ids = self._query(
            "SELECT evidence_id FROM `ogilvy-trends-v2.trends_v2_staging.signal_evidence_v2` "
            "WHERE run_id = @run_id AND client_scope_id != 'qa_canary' "
            "ORDER BY market, signal_id, evidence_id",
            parameters=(bigquery.ScalarQueryParameter("run_id", "STRING", R3_PRIOR_RUN_ID),),
            max_results=50_001,
        )
        return live_quality.PriorRunReview(
            receipt=receipt,
            run_id=R3_PRIOR_RUN_ID,
            candidate_projection_digest=str(digest_row.get("projection")),
            packet_digest=str(digest_row.get("packet")),
            packet_evidence_ids=tuple(str(dict(item)["evidence_id"]) for item in ids),
            candidate_projection_content_digest=str(digest_row.get("content_projection")),
            review_packet_content_digest=str(digest_row.get("content_packet")),
        )

    def _exposure_result(self) -> Mapping[str, object]:
        from google.cloud import bigquery
        from src.analysis.open_intelligence.brain_contract import canonical_bytes

        operation = "collection_exposure_issue"
        if self._mode not in _HISTORICAL_MODES:
            raise ApplyRefusal("r3 exposure result chain mode is not historical")
        if self._version == execution_approval._RESULT_VERSION:
            # The retained v1 chain, read under a historical mode against the packaged
            # retained registry; the r16 exposure ran on the old job.
            rows = self._query(
                "CALL `ogilvy-trends-v2.trends_v2_staging_approvals."
                "sp_read_open_intelligence_execution_result_chain_v1`(@source_operation, @run_id)",
                parameters=(
                    bigquery.ScalarQueryParameter("source_operation", "STRING", operation),
                    bigquery.ScalarQueryParameter("run_id", "STRING", R3_RUN_ID),
                ),
            )
        elif self._version == execution_approval._RESULT_VERSION_V2:
            rows = self._query(
                "CALL `ogilvy-trends-v2.trends_v2_staging_approvals."
                "sp_read_open_intelligence_execution_result_chain_v2`"
                "(@p_source_operation, @p_run_id)",
                parameters=(
                    bigquery.ScalarQueryParameter("p_source_operation", "STRING", operation),
                    bigquery.ScalarQueryParameter("p_run_id", "STRING", R3_RUN_ID),
                ),
            )
        else:
            raise ApplyRefusal("r3 exposure result chain version is unknown")
        if len(rows) != 1:
            raise ApplyRefusal("r3 exposure execution proof authority is unavailable")
        row = dict(rows[0])
        if self._version == execution_approval._RESULT_VERSION_V2:
            _admit_chain_row(row, operation, mode=self._mode, generation=self._generation)
        else:
            _origin, binding = _operation_binding(
                operation,
                manifest_version=_MANIFEST_V1,
                mode=self._mode,
                registry=self._generation.registry,
            )
            execution_name = row.get("execution_name")
            if not isinstance(execution_name, str) or not execution_name.startswith(
                binding.job_resource + "/executions/"
            ):
                raise ApplyRefusal("r3 exposure execution proof authority differs")
        canonical_result_json = row.get("canonical_result_json")
        result_digest = row.get("result_digest")
        if (
            row.get("status") != "succeeded"
            or not isinstance(canonical_result_json, str)
            or not isinstance(result_digest, str)
            or hashlib.sha256(canonical_result_json.encode()).hexdigest() != result_digest
        ):
            raise ApplyRefusal("r3 exposure execution proof authority differs")
        try:
            payload = json.loads(canonical_result_json)
        except json.JSONDecodeError as error:
            raise ApplyRefusal("r3 exposure execution proof authority is invalid") from error
        if (
            not isinstance(payload, dict)
            or payload.get("run_id") != R3_RUN_ID
            or canonical_bytes(payload).decode("utf-8") != canonical_result_json
        ):
            raise ApplyRefusal("r3 exposure execution proof authority is invalid")
        return payload

    def _build(self) -> dict[str, bytes]:
        from google.cloud import bigquery
        from scripts.staging.copy_open_intelligence_replay_sources import (
            TARGET_DATASET,
        )
        from src.analysis.open_intelligence.brain_live_reader import (
            SOURCE_COPY_RECEIPT_FIELDS,
        )

        def query_runner(sql: str):
            return [dict(row) for row in self._query(sql, max_results=100)]

        fields_sql = ", ".join(f"`{field}`" for field in SOURCE_COPY_RECEIPT_FIELDS)
        source_window_rows = self._query(
            f"SELECT {fields_sql} FROM `ogilvy-trends-v2.{TARGET_DATASET}."
            "open_intelligence_source_copy_receipts_v1` "
            "WHERE window_start = @window_start AND window_end = @window_end "
            "ORDER BY source_table",
            parameters=(
                bigquery.ScalarQueryParameter("window_start", "DATE", R3_WINDOW_START),
                bigquery.ScalarQueryParameter("window_end", "DATE", R3_SIGNAL_DATE),
            ),
            max_results=10,
        )
        source_window_receipts = tuple(dict(row) for row in source_window_rows)
        if (
            len(source_window_receipts) != len(R3_SOURCE_COPY_TABLES)
            or {row.get("source_table") for row in source_window_receipts} != R3_SOURCE_COPY_TABLES
            or any(set(row) != set(SOURCE_COPY_RECEIPT_FIELDS) for row in source_window_receipts)
        ):
            raise ApplyRefusal("r3 source-copy receipt authority is unavailable")
        review = self._quality_review()
        prior_review = self._prior_quality_review() if _review_is_pending(review) else None
        exposure_proof = self._exposure_result()
        exposure_receipts = _load_r3_exposure_authority(query_runner)
        self.review_receipt = review
        self.prior_review = prior_review
        self.exposure_proof = exposure_proof
        self.exposure_receipts = exposure_receipts
        self.source_window_receipts = source_window_receipts
        return _build_r3_execution_artifacts(
            review_receipt=review,
            exposure_proof=exposure_proof,
            exposure_receipts=exposure_receipts,
            completeness_rows=list(source_window_receipts),
        )

    def read(self, name: str) -> bytes:
        if self._artifacts is None:
            self._artifacts = self._build()
        try:
            return self._artifacts[name]
        except KeyError as error:
            raise ApplyRefusal("r3 execution approval artifact is unavailable") from error


@dataclass(frozen=True, slots=True)
class R3ExecutionProof:
    execution_proof_contract_version: str
    r3_pre_execution_addendum_sha256: str
    execution_name: str
    job_resource: str
    image_digest: str
    source_sha: str
    service_identity: str
    command: str
    args: tuple[str, ...]
    config_digest: str
    run_id: str
    signal_date: date
    started_at: datetime
    completed_at: datetime
    status: str
    run_receipt_digest: str
    row_set_digest: str
    persisted_row_family_counts: tuple[tuple[str, int], ...]


def build_r3_execution_proof(
    *,
    pre_execution_addendum_sha256: str,
    authority: Mapping[str, object],
    applied_receipt,
    readback_receipt,
    persisted_counts: Mapping[str, int],
    readback_counts: Mapping[str, int],
) -> R3ExecutionProof:
    from src.analysis.open_intelligence.run_receipts import (
        OpenIntelligenceRunReceipt,
        run_receipt_digest,
    )

    if (
        not isinstance(pre_execution_addendum_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", pre_execution_addendum_sha256) is None
        or not isinstance(authority, Mapping)
        or not isinstance(applied_receipt, OpenIntelligenceRunReceipt)
        or not isinstance(readback_receipt, OpenIntelligenceRunReceipt)
    ):
        raise ApplyRefusal("r3 execution proof authority is invalid")
    if readback_receipt != applied_receipt:
        raise ApplyRefusal("r3 applied receipt readback differs")
    expected_order = (
        "candidates",
        "evidence",
        "membership",
        "lineage",
        "predictions",
        "outcomes",
        "analysis",
    )
    if (
        tuple(persisted_counts) != expected_order
        or tuple(readback_counts) != expected_order
        or dict(persisted_counts) != dict(readback_counts)
    ):
        raise ApplyRefusal("r3 persisted row-family counts readback differs")
    expected_counts = {
        "candidates": readback_receipt.candidate_count,
        "evidence": readback_receipt.evidence_count,
        "membership": readback_receipt.membership_count,
        "lineage": readback_receipt.lineage_count,
        "predictions": readback_receipt.prediction_count,
        "outcomes": 0,
        "analysis": readback_receipt.analysis_count,
    }
    if dict(readback_counts) != expected_counts:
        raise ApplyRefusal("r3 persisted row-family counts differ from receipt")
    required = (
        "execution_name",
        "job_resource",
        "image_digest",
        "source_sha",
        "service_identity",
        "command",
        "args",
        "config_digest",
        "started_at",
    )
    if any(authority.get(field) is None for field in required):
        raise ApplyRefusal("r3 execution proof control-plane readback is incomplete")
    if authority["source_sha"] != readback_receipt.source_sha:
        raise ApplyRefusal("r3 execution proof source SHA differs from receipt")
    started_at = authority["started_at"]
    completed_at = authority["completed_at"]
    if (
        not isinstance(started_at, datetime)
        or started_at.tzinfo is None
        or not isinstance(completed_at, datetime)
        or completed_at.tzinfo is None
        or completed_at < started_at
        or authority["status"] != "succeeded"
    ):
        raise ApplyRefusal("r3 immutable Execution completion is unavailable")
    return R3ExecutionProof(
        execution_proof_contract_version=R3_EXECUTION_PROOF_CONTRACT_VERSION,
        r3_pre_execution_addendum_sha256=pre_execution_addendum_sha256,
        execution_name=authority["execution_name"],
        job_resource=authority["job_resource"],
        image_digest=authority["image_digest"],
        source_sha=authority["source_sha"],
        service_identity=authority["service_identity"],
        command=authority["command"],
        args=tuple(authority["args"]),
        config_digest=authority["config_digest"],
        run_id=readback_receipt.run_id,
        signal_date=readback_receipt.signal_date,
        started_at=started_at,
        completed_at=completed_at,
        status=authority["status"],
        run_receipt_digest=run_receipt_digest(readback_receipt),
        row_set_digest=readback_receipt.row_set_digest,
        persisted_row_family_counts=tuple(readback_counts.items()),
    )


def render_r3_execution_proof(proof: R3ExecutionProof) -> str:
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    if not isinstance(proof, R3ExecutionProof):
        raise ApplyRefusal("r3 execution proof is invalid")
    return canonical_bytes(proof).decode("utf-8") + "\n"


def write_r3_execution_proof(proof: R3ExecutionProof, *, stdout=None) -> None:
    output = sys.stdout if stdout is None else stdout
    output.write(render_r3_execution_proof(proof))


def _read_r3_execution_proof(
    *,
    query_runner,
    report,
    authority,
    execution_approval_manifest_sha256: str,
) -> R3ExecutionProof:
    from src.analysis.open_intelligence.run_receipts import (
        RUN_RECEIPT_ROW_FIELDS,
        build_run_receipt,
    )

    run_id = report.run_id
    receipt_rows = list(
        query_runner(
            f"SELECT {', '.join(RUN_RECEIPT_ROW_FIELDS)} FROM "
            "`ogilvy-trends-v2.trends_v2_staging.open_intelligence_run_receipts_v1` "
            f"WHERE run_id = '{run_id}'"
        )
    )
    if len(receipt_rows) != 1:
        raise ApplyRefusal("r3 applied receipt readback is unavailable")
    fields = {field: receipt_rows[0].get(field) for field in RUN_RECEIPT_ROW_FIELDS}
    if isinstance(fields["market_scope"], list):
        fields["market_scope"] = tuple(fields["market_scope"])
    try:
        readback_receipt = build_run_receipt(**fields)
    except ValueError as error:
        raise ApplyRefusal("r3 applied receipt readback is invalid") from error
    counts = {}
    for family, table in persistence.TABLE_BINDINGS.items():
        rows = list(
            query_runner(
                f"SELECT COUNT(*) AS row_count FROM `ogilvy-trends-v2.trends_v2_staging.{table}` "
                f"WHERE run_id = '{run_id}'"
            )
        )
        counts[family] = int(rows[0].get("row_count", -1)) if len(rows) == 1 else -1
    analysis_rows = list(
        query_runner(
            "SELECT COUNT(*) AS row_count FROM "
            "`ogilvy-trends-v2.trends_v2_staging.signal_analysis_v2` "
            f"WHERE run_id = '{run_id}'"
        )
    )
    counts["analysis"] = (
        int(analysis_rows[0].get("row_count", -1)) if len(analysis_rows) == 1 else -1
    )
    persisted_counts = {**dict(report.persisted_counts), "analysis": report.receipt.analysis_count}
    return build_r3_execution_proof(
        pre_execution_addendum_sha256=execution_approval_manifest_sha256,
        authority=authority,
        applied_receipt=report.receipt,
        readback_receipt=readback_receipt,
        persisted_counts=persisted_counts,
        readback_counts=counts,
    )


def _load_human_review_receipt(path: str | None) -> Mapping[str, object]:
    from src.analysis.open_intelligence.live_quality import REVIEW_RECEIPT_FIELDS

    if not isinstance(path, str) or not path:
        raise ApplyRefusal("r3 human review receipt path is unavailable")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ApplyRefusal("r3 human review receipt is unreadable") from error
    if not isinstance(payload, dict) or set(payload) != set(REVIEW_RECEIPT_FIELDS):
        raise ApplyRefusal("r3 human review receipt fields are invalid")
    payload = {field: payload[field] for field in REVIEW_RECEIPT_FIELDS}
    for field in (
        "reviewed_evidence_ids",
        "foreign_market_evidence_ids",
        "factual_conflict_evidence_ids",
        "uncertain_evidence_ids",
    ):
        if not isinstance(payload[field], list):
            raise ApplyRefusal(f"r3 human review receipt {field} is invalid")
        payload[field] = tuple(payload[field])
    reviewed_at = payload.get("reviewed_at")
    if not isinstance(reviewed_at, str):
        raise ApplyRefusal("r3 human review receipt timestamp is invalid")
    try:
        payload["reviewed_at"] = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApplyRefusal("r3 human review receipt timestamp is invalid") from error
    return MappingProxyType(payload)


def _load_r3_exposure_execution_proof(path: str | None):
    from scripts.staging.issue_collection_exposure_receipts import ExposureExecutionProof

    if not isinstance(path, str) or not path:
        raise ApplyRefusal("r3 exposure execution proof path is unavailable")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ApplyRefusal("r3 exposure execution proof is unreadable") from error
    if not isinstance(payload, dict):
        raise ApplyRefusal("r3 exposure execution proof is invalid")
    try:
        payload["args"] = tuple(payload["args"])
        payload["inserted_natural_keys"] = tuple(
            (family, date.fromisoformat(day)) for family, day in payload["inserted_natural_keys"]
        )
        for field in ("started_at", "completed_at"):
            payload[field] = datetime.fromisoformat(payload[field].replace("Z", "+00:00"))
        proof = ExposureExecutionProof(**payload)
    except (KeyError, TypeError, ValueError) as error:
        raise ApplyRefusal("r3 exposure execution proof is invalid") from error
    return proof


def _load_r3_exposure_authority(query_runner) -> tuple[Mapping[str, object], ...]:
    from scripts.staging.issue_collection_exposure_receipts import (
        EXPOSURE_RECEIPT_FIELDS,
        validate_exposure_receipt_set,
    )

    rows = list(
        query_runner(
            f"SELECT {', '.join(EXPOSURE_RECEIPT_FIELDS)} FROM "
            "`ogilvy-trends-v2.trends_v2_staging.collection_exposure_receipts_v1` "
            f"WHERE exposure_date BETWEEN DATE '{R3_WINDOW_START.isoformat()}' AND DATE '{R3_SIGNAL_DATE.isoformat()}' "
            "ORDER BY source_family, exposure_date"
        )
    )
    validated = validate_exposure_receipt_set(rows)
    return validated


@dataclass(frozen=True, slots=True)
class R3QualityAuthority:
    metrics_by_component: Mapping[str, object]
    receipts_by_component: Mapping[str, Mapping[str, object]]
    readiness_by_component: Mapping[str, object]
    readiness_rules: object
    missing_by_component: Mapping[str, tuple[str, ...]]
    promotion_results_by_signal_id: Mapping[str, object]
    quality_failed_by_component: Mapping[str, bool] = MappingProxyType({})
    factual_conflict_by_component: Mapping[str, bool] = MappingProxyType({})


def evaluate_r3_quality_authority(
    *,
    result: object,
    scope: object,
    assembled: AssembledComponentInputs,
    signal_date: date,
    created_at: datetime,
    exposure_receipts: tuple[Mapping[str, object], ...],
    review_receipt: Mapping[str, object],
    source_window_digest: str,
    evidence_by_component: Mapping[str, object],
    prior_review=None,
) -> R3QualityAuthority:
    from scripts.staging.scoring_qualification import approved_decision_strength_rules
    from src.analysis.open_intelligence import live_quality
    from src.analysis.open_intelligence.rows import build_dynamic_signal_rows

    exposure_keys = {(item["source_family"], item["exposure_date"]) for item in exposure_receipts}
    required_days = tuple(R3_WINDOW_START + timedelta(days=offset) for offset in range(14))
    metrics = dict(assembled.metrics_by_component)
    receipts_by_component: dict[str, Mapping[str, object]] = {}
    readiness_by_component: dict[str, object] = {}
    missing = dict(assembled.missing_by_component)
    technical_by_component = {}
    quality_failed_by_component: dict[str, bool] = {}
    factual_conflict_by_component: dict[str, bool] = {}
    memberships_by_component = {}
    for component in getattr(result, "components", ()):
        key = persistence.component_bridge_key(component)
        if key not in metrics or key not in assembled.receipts_by_component:
            continue
        receipts = dict(assembled.receipts_by_component[key])
        # Approved by Albert on 2026-09-03: the direction test counts the full term
        # match for the component by family and published date; the sampled
        # receipts stay the displayed evidence. A family the match never cites
        # counts 0 and 0 and stays not_applicable.
        factor_evidence = evidence_by_component.get(key)
        direction_counts = {
            family: (current, baseline)
            for family, current, baseline in getattr(factor_evidence, "family_direction_counts", ())
        }
        counts = {}
        exposure_complete = {}
        for family in component.source_families:
            family_exposures = tuple(
                item for item in exposure_receipts if item["source_family"] == family
            )
            counts[family] = direction_counts.get(family, (0, 0))
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
            exposure_complete[family] = (
                all((family, day) in exposure_keys for day in required_days)
                and len(family_exposures) == len(required_days)
                and len(invariant_exposure) == 1
                and all(
                    item.get("capture_complete") is True and item.get("quota_exhausted") is False
                    for item in family_exposures
                )
            )
        directions = live_quality.family_directions(counts, exposure_complete=exposure_complete)
        directed_receipts = {
            row_id: replace(
                receipt,
                direction=(directions[receipt.source_family].direction or "not_applicable"),
            )
            for row_id, receipt in receipts.items()
        }
        projected = persistence._projected_memberships(result, component)
        membership_maps = tuple(
            {
                "member_identity": item.member_identity,
                "canonical_value": item.canonical_value,
                "candidate_type": item.candidate_type,
                "qualifies_evidence": any(
                    receipt.member_identity == item.member_identity
                    and receipt.availability == "available"
                    for receipt in directed_receipts.values()
                ),
            }
            for item in projected
        )
        technical = live_quality.evaluate_technical_quality(
            component=component,
            receipts=directed_receipts,
            memberships=membership_maps,
            as_of=datetime.combine(signal_date, time(23, 59, 59), tzinfo=UTC),
        )
        records = [
            EvidenceRecord(
                row_id=item.row_id,
                source_family=item.source_family,
                direction=item.direction,
                published_at=item.published_at,
                availability=item.availability,
                geo_confidence=item.geo_confidence,
                factual_conflict=item.factual_conflict,
            )
            for item in directed_receipts.values()
        ]
        conflict = any(item.factual_conflict for item in directed_receipts.values())
        readiness = evaluate_readiness(
            records,
            assembled.readiness_rules,
            quality_evaluated=True,
            quality_failed=not technical.passed,
            factual_conflict=conflict,
        )
        receipts_by_component[key] = MappingProxyType(directed_receipts)
        readiness_by_component[key] = readiness
        technical_by_component[key] = technical
        quality_failed_by_component[key] = not technical.passed
        factual_conflict_by_component[key] = conflict
        memberships_by_component[key] = membership_maps

    provisional = persistence.build_producing_batch(
        result,
        scope=scope,
        signal_date=signal_date,
        created_at=created_at,
        metrics_by_component=metrics,
        receipts_by_component=receipts_by_component,
        readiness_by_component=readiness_by_component,
        readiness_rules=assembled.readiness_rules,
        missing_by_component=missing,
        quality_evaluated=True,
        quality_failed_by_component=quality_failed_by_component,
        factual_conflict_by_component=factual_conflict_by_component,
    )
    packet = live_quality.build_review_packet(R3_RUN_ID, provisional.batch)
    reviewed_projection_digest = live_quality.candidate_projection_digest(
        R3_RUN_ID, provisional.batch
    )
    review_pending = _review_is_pending(review_receipt)
    review_authority = (
        None
        if review_pending
        else live_quality.validate_review_receipt(
            review_receipt,
            packet=packet,
            source_window_digest=source_window_digest,
            candidate_projection_digest=reviewed_projection_digest,
        )
    )
    if review_pending and prior_review is not None:
        # Approved 3 Sep 2026: the prior run's registered review is authority for
        # this run when its rows are the same rows. No match reads as pending.
        review_authority = live_quality.content_review_authority(
            prior_review,
            source_window_digest=source_window_digest,
            batch=provisional.batch,
        )
    promotions = {}
    candidates_by_key = {}
    for component in getattr(result, "components", ()):
        key = persistence.component_bridge_key(component)
        if key not in readiness_by_component:
            continue
        candidate, _evidence = build_dynamic_signal_rows(
            component=component,
            scope=scope,
            signal_date=signal_date,
            created_at=created_at,
            metrics=metrics[key],
            receipts=receipts_by_component[key],
            readiness=readiness_by_component[key],
            readiness_rules=assembled.readiness_rules,
            quality_evaluated=True,
            quality_failed=quality_failed_by_component[key],
            factual_conflict=factual_conflict_by_component[key],
        )
        candidates_by_key[key] = candidate
        promotion = live_quality.score_quality_candidate(
            candidate=candidate,
            readiness=readiness_by_component[key],
            receipts=receipts_by_component[key],
            memberships=memberships_by_component[key],
            technical_quality=technical_by_component[key],
            review_authority=review_authority,
            rules=approved_decision_strength_rules(),
            directional_conflict=readiness_by_component[key].state == "contradictory",
        )
        promotions[candidate["signal_id"]] = promotion

    final = persistence.build_producing_batch(
        result,
        scope=scope,
        signal_date=signal_date,
        created_at=created_at,
        metrics_by_component=metrics,
        receipts_by_component=receipts_by_component,
        readiness_by_component=readiness_by_component,
        readiness_rules=assembled.readiness_rules,
        missing_by_component=missing,
        quality_evaluated=True,
        promotion_results_by_signal_id=promotions,
        quality_failed_by_component=quality_failed_by_component,
        factual_conflict_by_component=factual_conflict_by_component,
    )
    for family, field in (
        (final.batch.candidates, "signal_id"),
        (final.batch.evidence, "evidence_id"),
        (final.batch.membership, "member_id"),
    ):
        identities = tuple(row[field] for row in family)
        if len(identities) != len(set(identities)):
            raise ApplyRefusal(f"r3 technical quality found duplicate {field}")
    live_quality.validate_prediction_review_binding(provisional.batch, final.batch)
    if (
        live_quality.candidate_projection_digest(R3_RUN_ID, final.batch)
        != reviewed_projection_digest
    ):
        raise ApplyRefusal("r3 reviewed projection changed during scoring")
    if not review_pending:
        live_quality.validate_review_receipt(
            review_receipt,
            packet=live_quality.build_review_packet(R3_RUN_ID, final.batch),
            source_window_digest=source_window_digest,
            candidate_projection_digest=reviewed_projection_digest,
        )
    elif review_authority is not None and (
        live_quality.content_review_authority(
            prior_review, source_window_digest=source_window_digest, batch=final.batch
        )
        is None
    ):
        raise ApplyRefusal("r3 prior review authority lost during scoring")
    return R3QualityAuthority(
        metrics_by_component=MappingProxyType(metrics),
        receipts_by_component=MappingProxyType(receipts_by_component),
        readiness_by_component=MappingProxyType(readiness_by_component),
        readiness_rules=assembled.readiness_rules,
        missing_by_component=MappingProxyType(missing),
        promotion_results_by_signal_id=MappingProxyType(promotions),
        quality_failed_by_component=MappingProxyType(quality_failed_by_component),
        factual_conflict_by_component=MappingProxyType(factual_conflict_by_component),
    )


def _run_apply_cli_impl(
    argv: list[str] | None = None,
    *,
    _terminal_context: list[object] | None = None,
) -> str:
    import argparse

    from google.auth import default as load_default_credentials
    from google.auth.transport.requests import Request
    from google.cloud import bigquery
    from scripts.migrations.create_open_intelligence_v2 import (
        MigrationPlan,
        _authorized_credentials,
    )
    from scripts.staging.copy_open_intelligence_replay_sources import (
        COPY_RUN_ID,
        END_DATE,
        SERVICE_ACCOUNT,
        START_DATE,
        TARGET_DATASET,
    )
    from scripts.staging.copy_open_intelligence_replay_sources import (
        PROJECT as STAGING_PROJECT,
    )
    from src.contracts.open_intelligence import resolve_client_scope

    values = tuple(argv or ())
    protected = not values
    if protected:
        trend_date = R3_SIGNAL_DATE
        run_id = R3_RUN_ID
        source_sha_input = None
        review_receipt_file = None
        exposure_proof_file = None
    else:
        parser = argparse.ArgumentParser(prog="replay_open_intelligence --apply-run")
        parser.add_argument("--apply-run", action="store_true", required=True)
        parser.add_argument("--trend-date", required=True)
        parser.add_argument("--run-id", required=True)
        parser.add_argument("--source-sha", required=True)
        parser.add_argument(
            "--review-receipt-file", default=os.environ.get("OI_R3_REVIEW_RECEIPT_FILE")
        )
        parser.add_argument(
            "--exposure-execution-proof-file",
            default=os.environ.get("OI_R3_EXPOSURE_EXECUTION_PROOF_FILE"),
        )
        args = parser.parse_args(values)
        trend_date = date.fromisoformat(args.trend_date)
        run_id = args.run_id
        source_sha_input = args.source_sha
        review_receipt_file = args.review_receipt_file
        exposure_proof_file = args.exposure_execution_proof_file
    if (
        run_id != R3_RUN_ID
        or trend_date != R3_SIGNAL_DATE
        or (
            source_sha_input is not None and re.fullmatch(r"[0-9a-f]{40}", source_sha_input) is None
        )
    ):
        raise ApplyRefusal("r3 run identity, signal date or source SHA is invalid")
    if not START_DATE <= trend_date <= END_DATE:
        raise ApplyRefusal(
            f"trend date {trend_date} is outside the copied window {START_DATE}..{END_DATE}"
        )

    # The packaged active pair names the apply job and principal before any read; the
    # issued authority must carry the same pair and the same binding after the load.
    fresh_generation, _apply_origin, apply_binding = _r3_apply_profile(mode="new_consume")
    plan = MigrationPlan(
        target="staging",
        project=STAGING_PROJECT,
        dataset=TARGET_DATASET,
        location="US",
        service_account=apply_binding.service_identity if protected else SERVICE_ACCOUNT,
        statements=(),
        source_lab_upgrade_statements=(),
        rollback_statements=(),
    )
    credentials = _authorized_credentials(
        plan,
        lambda: load_default_credentials(),
        Request,
    )
    client = bigquery.Client(
        project=STAGING_PROJECT,
        credentials=credentials,
        location=plan.location,
    )

    def query_runner(sql: str):
        return [
            dict(row)
            for row in client.query(sql, retry=None, job_retry=None).result(
                retry=None,
                job_retry=None,
            )
        ]

    global _R3_DURABLE_ARTIFACT_CONTEXT
    if protected:
        provider = _R3ArtifactProvider(
            client,
            version=execution_approval._RESULT_VERSION_V2,
            mode="historical_read",
            generation=fresh_generation,
        )
        _R3_DURABLE_ARTIFACT_CONTEXT = provider
        try:
            durable_authority = execution_approval._load_execution_authority(
                "r3_apply", mode="new_consume", artifact_reader=provider.read
            )
            _require_runtime_profile(
                durable_authority, "r3_apply", generation=fresh_generation, binding=apply_binding
            )
            durable_consumption = execution_approval._consume_execution_authority(durable_authority)
            if _terminal_context is not None:
                _terminal_context.clear()
                _terminal_context.extend((durable_authority, durable_consumption))
        finally:
            _R3_DURABLE_ARTIFACT_CONTEXT = None
        if (
            provider.review_receipt is None
            or provider.exposure_proof is None
            or not provider.exposure_receipts
            or not provider.source_window_receipts
        ):
            raise ApplyRefusal("r3 execution approval artifacts are incomplete")
        review_receipt = provider.review_receipt
        prior_review = provider.prior_review
        exposure_proof = provider.exposure_proof
        exposure_receipts = provider.exposure_receipts
        source_sha = durable_authority.source_sha
    else:
        completeness_rows = list(
            query_runner(
                build_partition_completeness_query(
                    project=STAGING_PROJECT,
                    dataset=TARGET_DATASET,
                    copy_run_id=COPY_RUN_ID,
                )
            )
        )
        if not partition_completeness_proven(completeness_rows):
            raise ApplyRefusal("r3 source-copy completeness authority is unavailable")
        exposure_proof = _load_r3_exposure_execution_proof(exposure_proof_file)
        exposure_receipts = _load_r3_exposure_authority(query_runner)
        review_receipt = _load_human_review_receipt(review_receipt_file)
        prior_review = None
        _R3_DURABLE_ARTIFACT_CONTEXT = _build_r3_execution_artifacts(
            review_receipt=review_receipt,
            exposure_proof=exposure_proof,
            exposure_receipts=exposure_receipts,
            completeness_rows=completeness_rows,
        )
        try:
            durable_authority = execution_approval._load_execution_authority(
                "r3_apply", mode="new_consume"
            )
            _require_runtime_profile(
                durable_authority, "r3_apply", generation=fresh_generation, binding=apply_binding
            )
            durable_consumption = execution_approval._consume_execution_authority(durable_authority)
            if _terminal_context is not None:
                _terminal_context.clear()
                _terminal_context.extend((durable_authority, durable_consumption))
        finally:
            _R3_DURABLE_ARTIFACT_CONTEXT = None
        if durable_authority.source_sha != source_sha_input:
            raise ApplyRefusal("r3 source SHA differs from durable authority")
        source_sha = source_sha_input
    if protected:
        completeness_rows = list(
            query_runner(
                build_partition_completeness_query(
                    project=STAGING_PROJECT,
                    dataset=TARGET_DATASET,
                    copy_run_id=COPY_RUN_ID,
                )
            )
        )
        if not partition_completeness_proven(completeness_rows):
            raise ApplyRefusal("r3 source-copy completeness authority is unavailable")
    source_window_digest = _source_window_digest(completeness_rows, COPY_RUN_ID)

    def persist_runner(*, batch, dry_run):
        return persist_producing_batch(
            client=client,
            batch=batch,
            rule_bundle=rule_bundle,
            dry_run=dry_run,
            writer_identity=(
                apply_binding.service_identity if protected else persistence.TARGET_WRITER_IDENTITY
            ),
        )

    scope = resolve_client_scope(
        run_id=run_id,
        client_scope_id="ogilvy_default",
        market_scope=("za", "ng", "ke"),
        audience_lens_ids=(),
    )
    created_at = datetime.now(UTC)
    rule_bundle = load_composition_rules_v2(
        COMPOSITION_RULES_V2_PATH,
        created_at,
        require_certified=True,
    )
    result = run_dynamic_signal_identity_v2(
        trend_date,
        scope,
        client,
        TARGET_DATASET,
        False,
        rule_bundle=rule_bundle,
        semantic_provider=LexicalSimilarityProviderV2(
            pair_ceiling=rule_bundle.graph_rules.pair_ceiling
        ),
    )
    evidence_by_component = collect_component_factor_evidence(
        query_runner=query_runner,
        components=result.components,
        project=STAGING_PROJECT,
        dataset=TARGET_DATASET,
        trend_date=trend_date,
        window_start=START_DATE,
    )
    assembled = assemble_component_inputs(
        result=result,
        evidence_by_component=evidence_by_component,
        trend_date=trend_date,
    )
    quality = evaluate_r3_quality_authority(
        result=result,
        scope=scope,
        assembled=assembled,
        signal_date=trend_date,
        created_at=created_at,
        exposure_receipts=exposure_receipts,
        review_receipt=review_receipt,
        source_window_digest=source_window_digest,
        evidence_by_component=evidence_by_component,
        prior_review=prior_review,
    )
    report = apply_open_intelligence_run(
        result=result,
        scope=scope,
        signal_date=trend_date,
        observation_start=START_DATE,
        created_at=created_at,
        project=STAGING_PROJECT,
        dataset=TARGET_DATASET,
        rule_version=rule_bundle.rule_version,
        cluster_build_version=ANCHORED_CLUSTER_BUILD_VERSION,
        source_sha=source_sha,
        query_runner=query_runner,
        persist_runner=persist_runner,
        metrics_by_component=quality.metrics_by_component,
        receipts_by_component=quality.receipts_by_component,
        readiness_by_component=quality.readiness_by_component,
        readiness_rules=quality.readiness_rules,
        quality_evaluated=True,
        promotion_results_by_signal_id=quality.promotion_results_by_signal_id,
        quality_failed_by_component=quality.quality_failed_by_component,
        factual_conflict_by_component=quality.factual_conflict_by_component,
        expected_source_window_digest=source_window_digest,
        missing_reasons_by_component=quality.missing_by_component,
    )
    manifest = durable_authority.manifest
    execution_authority = {
        "execution_name": durable_authority.execution_name,
        "job_resource": durable_authority.job_resource,
        "image_digest": manifest.image_uri.rsplit("@", 1)[1],
        "source_sha": durable_authority.source_sha,
        "service_identity": manifest.service_identity,
        "command": " ".join(manifest.command),
        "args": manifest.arguments,
        "config_digest": dict(manifest.input_artifacts)["config"],
        "started_at": durable_authority.approval.approved_at,
        "completed_at": datetime.now(UTC),
        "status": "succeeded",
    }
    proof = _read_r3_execution_proof(
        query_runner=query_runner,
        report=report,
        authority=execution_authority,
        execution_approval_manifest_sha256=durable_authority.approval.manifest_sha256,
    )
    canonical_result_json = render_r3_execution_proof(proof).rstrip("\n")
    result_digest = hashlib.sha256(canonical_result_json.encode()).hexdigest()
    result = execution_approval._record_execution_result(
        durable_authority,
        durable_consumption,
        f"bq://{STAGING_PROJECT}.{TARGET_DATASET}.open_intelligence_run_receipts_v1#{run_id}",
        canonical_result_json,
        result_digest,
        "succeeded",
    )
    if _terminal_context is not None:
        _terminal_context.clear()
    payload = json.loads(canonical_result_json)
    payload["execution_approval"] = {
        "manifest_sha256": durable_authority.approval.manifest_sha256,
        "approval_id": durable_authority.approval.approval_id,
        "consumption_id": durable_consumption.consumption_id,
        "result_id": result.result_id,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def run_apply_cli(argv: list[str] | None = None) -> str:
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    terminal_context: list[object] = []
    try:
        return _run_apply_cli_impl(argv, _terminal_context=terminal_context)
    except Exception as error:
        if execution_approval._is_unresolved_result(error):
            terminal_context.clear()
            raise
        if len(terminal_context) == 2 and _same_generation(*terminal_context):
            authority, consumption = terminal_context
            failure_payload = {
                "error_code": "r3_apply_failed",
                "run_id": R3_RUN_ID,
                "status": "failed",
            }
            failure_json = canonical_bytes(failure_payload).decode("utf-8")
            failure_digest = hashlib.sha256(failure_json.encode()).hexdigest()
            execution_approval._record_execution_result(
                authority,
                consumption,
                f"bq://ogilvy-trends-v2.trends_v2_staging."
                f"open_intelligence_run_receipts_v1#{R3_RUN_ID}:failed",
                failure_json,
                failure_digest,
                "failed",
            )
        raise


# --- The apply mode -----------------------------------------------------------
#
# Approved by 42-noncanary-run-release-approval-request-2026-08-29.md sections
# 3.2 and 3.3. One producing run against the isolated staging dataset: the
# pipeline runs with persist off, the bridge turns its components into rows or
# named skips, partition completeness is proved in SQL against the copy
# manifest, the batch is dry-run then applied, the write is read back exactly,
# and the receipt goes in last, always blocked. The pipeline's own refusal to
# persist stays untouched; this mode never asks it to.

from src.analysis.open_intelligence import persistence

APPLY_OBSERVATION_METHOD = "dynamic_source_copy_apply_v1"


class ApplyRefusal(ValueError):
    """The apply cannot proceed honestly."""


def build_partition_completeness_query(*, project: str, dataset: str, copy_run_id: str) -> str:
    """Count-and-digest match between the copy manifest and the target tables.

    One row per copied table: how many rows the manifest binds to this copy
    run, and how many of those exist in the target with the exact manifest
    content hash. Computed entirely in SQL so no truncated client view can
    misreport it.
    """
    from scripts.staging.copy_open_intelligence_replay_sources import (
        _content_hash_expr,
        _copy_id_expr,
        _table_contracts,
    )

    manifest = f"`{project}.{dataset}.open_intelligence_source_copy_manifest_v1`"
    parts = []
    for name, table in sorted(_table_contracts().items()):
        target = f"`{project}.{dataset}.{name}`"
        parts.append(
            f"SELECT '{name}' AS source_table, "
            f"COUNT(*) AS manifest_rows, "
            f"COUNTIF(target_match.copy_row_id IS NOT NULL) AS matched_rows "
            f"FROM {manifest} manifest "
            f"LEFT JOIN (SELECT {_copy_id_expr(table, 'target')} AS copy_row_id, "
            f"{_content_hash_expr(table, 'target')} AS content_hash "
            f"FROM {target} target) target_match "
            f"ON target_match.copy_row_id = manifest.copy_row_id "
            f"AND target_match.content_hash = manifest.source_content_sha256 "
            f"WHERE manifest.copy_run_id = '{copy_run_id}' "
            f"AND manifest.target_table = '{name}'"
        )
    return " UNION ALL ".join(parts)


def partition_completeness_proven(rows: object) -> bool:
    """True only on an exact nonzero match for every table.

    An empty manifest proves nothing: vacuous truth is not proof, and a
    completeness flag written off zero evidence would be exactly the invented
    readiness this programme exists to refuse.
    """
    total = 0
    checked = 0
    for row in rows or ():
        manifest_rows = row.get("manifest_rows")
        matched_rows = row.get("matched_rows")
        if not isinstance(manifest_rows, int) or not isinstance(matched_rows, int):
            return False
        if manifest_rows != matched_rows:
            return False
        total += manifest_rows
        checked += 1
    return checked > 0 and total > 0


def _source_window_digest(completeness_rows, copy_run_id: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "copy_run_id": copy_run_id,
                "completeness": sorted(
                    (
                        str(row.get("source_table")),
                        int(row.get("manifest_rows", -1)),
                        int(row.get("matched_rows", -1)),
                    )
                    for row in completeness_rows
                ),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ApplyReport:
    run_id: str
    receipt: object
    admitted: tuple[str, ...]
    skipped: tuple[tuple[str, tuple[str, ...]], ...]
    persisted_counts: Mapping[str, int]


def _receipt_insert_statement(project: str, dataset: str, receipt: object) -> str:
    from src.analysis.open_intelligence.run_receipts import RUN_RECEIPT_ROW_FIELDS

    def _sql_value(value: object) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, tuple):
            inner = ", ".join(_sql_value(item) for item in value)
            return f"[{inner}]"
        if isinstance(value, datetime):
            stamp = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
            return f"TIMESTAMP '{stamp}+00'"
        if isinstance(value, date):
            return f"DATE '{value.isoformat()}'"
        text = str(value).replace("\\", "\\\\").replace("'", "\\'")
        return f"'{text}'"

    columns = ", ".join(RUN_RECEIPT_ROW_FIELDS)
    values = ", ".join(_sql_value(getattr(receipt, field)) for field in RUN_RECEIPT_ROW_FIELDS)
    return (
        f"INSERT INTO `{project}.{dataset}.{persistence.RUN_RECEIPT_TABLE}` "
        f"({columns}) VALUES ({values})"
    )


def apply_open_intelligence_run(
    *,
    result: object,
    scope: object,
    signal_date: date,
    observation_start: date,
    created_at: datetime,
    project: str,
    dataset: str,
    rule_version: str,
    cluster_build_version: str,
    source_sha: str,
    query_runner: object,
    persist_runner: object,
    copy_run_id: str | None = None,
    metrics_by_component: Mapping[str, object] | None = None,
    receipts_by_component: Mapping[str, object] | None = None,
    readiness_by_component: Mapping[str, object] | None = None,
    readiness_rules: object | None = None,
    quality_evaluated: bool = False,
    promotion_results_by_signal_id: Mapping[str, object] | None = None,
    quality_failed_by_component: Mapping[str, bool] | None = None,
    factual_conflict_by_component: Mapping[str, bool] | None = None,
    expected_source_window_digest: str | None = None,
    missing_reasons_by_component: Mapping[str, tuple[str, ...]] | None = None,
) -> ApplyReport:
    from scripts.staging.copy_open_intelligence_replay_sources import COPY_RUN_ID
    from scripts.staging.scoring_qualification import unapproved_component_reasons
    from src.analysis.open_intelligence.run_receipts import run_receipt_digest

    if getattr(result, "error_state", None) is not None:
        raise ApplyRefusal(f"pipeline result carries an error state: {result.error_state}")
    if getattr(result, "persistence_result", None) is not None:
        raise ApplyRefusal("the pipeline must not have persisted anything itself")
    run_id = getattr(scope, "run_id", None)
    # A strict charset, validated before any query exists. Found by
    # independent review: the identity was interpolated into guard and
    # readback SQL while the insert escaped it, so a quote in the id made
    # the guard check a different identity than the one stored.
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9_][a-z0-9_-]{0,127}", run_id):
        raise ApplyRefusal("scope run id is invalid")
    if not isinstance(cluster_build_version, str) or not cluster_build_version:
        raise ApplyRefusal("cluster build version is invalid")
    if run_id == R3_RUN_ID and quality_evaluated is not True:
        raise ApplyRefusal("r3 quality authority is unavailable")
    components = getattr(result, "components", ())
    if isinstance(components, tuple) and {
        getattr(component, "build_version", None) for component in components
    } not in (set(), {cluster_build_version}):
        raise ApplyRefusal("cluster build version does not match pipeline components")
    copy_run_id = copy_run_id or COPY_RUN_ID

    # One run, one receipt. A repeated run identity is refused before any
    # work, not silently overwritten and not silently duplicated.
    existing = list(
        query_runner(
            f"SELECT COUNT(*) AS existing FROM "
            f"`{project}.{dataset}.{persistence.RUN_RECEIPT_TABLE}` "
            f"WHERE run_id = '{run_id}'"
        )
    )
    if existing and int(existing[0].get("existing", 0)) > 0:
        raise ApplyRefusal(f"a receipt already exists for run {run_id}")

    metrics_by_component = dict(metrics_by_component or {})
    registry_reasons = unapproved_component_reasons()
    supplied_reasons = dict(missing_reasons_by_component or {})
    missing_by_component = {
        persistence.component_bridge_key(component): supplied_reasons.get(
            persistence.component_bridge_key(component), registry_reasons
        )
        for component in components
        if persistence.component_bridge_key(component) not in metrics_by_component
    }
    bridge = persistence.build_producing_batch(
        result,
        scope=scope,
        signal_date=signal_date,
        created_at=created_at,
        metrics_by_component=metrics_by_component,
        receipts_by_component=dict(receipts_by_component or {}),
        readiness_by_component=dict(readiness_by_component or {}),
        readiness_rules=readiness_rules,
        missing_by_component=missing_by_component,
        quality_evaluated=quality_evaluated,
        promotion_results_by_signal_id=dict(promotion_results_by_signal_id or {}),
        quality_failed_by_component=quality_failed_by_component,
        factual_conflict_by_component=factual_conflict_by_component,
    )

    completeness_rows = list(
        query_runner(
            build_partition_completeness_query(
                project=project, dataset=dataset, copy_run_id=copy_run_id
            )
        )
    )
    proven = partition_completeness_proven(completeness_rows)
    source_window_digest = _source_window_digest(completeness_rows, copy_run_id)
    if (
        expected_source_window_digest is not None
        and source_window_digest != expected_source_window_digest
    ):
        raise ApplyRefusal("source window changed after quality evaluation")

    persisted_counts: dict[str, int] = dict.fromkeys(persistence.TABLE_BINDINGS, 0)
    batch_total = sum(len(getattr(bridge.batch, table)) for table in persistence.TABLE_BINDINGS)
    if proven and batch_total == 0:
        # Nothing to write. Persisting an empty batch would still demand a
        # replay-certified rule bundle, and none exists; a zero-row run does
        # not need one, but the readback law still holds: prove the target
        # carries no rows for this run before any receipt may say so.
        for _table_key, table_name in (
            ("candidates", "signal_candidates_v2"),
            ("evidence", "signal_evidence_v2"),
            ("membership", "signal_membership_v2"),
            ("lineage", "signal_lineage_v2"),
            ("predictions", "signal_predictions_v2"),
            ("outcomes", "signal_outcomes_v2"),
        ):
            rows = list(
                query_runner(
                    f"SELECT COUNT(*) AS row_count FROM "
                    f"`{project}.{dataset}.{table_name}` "
                    f"WHERE run_id = '{run_id}'"
                )
            )
            found = int(rows[0].get("row_count", -1)) if rows else -1
            if found != 0:
                raise ApplyRefusal(
                    f"readback mismatch: {table_name} holds {found} rows for "
                    f"run {run_id}, batch holds 0"
                )
    elif proven:
        dry = persist_runner(batch=bridge.batch, dry_run=True)
        if getattr(dry, "dry_run", None) is not True:
            raise ApplyRefusal("the dry run did not run as a dry run")
        applied = persist_runner(batch=bridge.batch, dry_run=False)
        inserted = dict(getattr(applied, "inserted_counts", {}) or {})
        expected = {
            table: len(getattr(bridge.batch, table)) for table in persistence.TABLE_BINDINGS
        }
        # Exact readback: the write must account for every row in the batch,
        # no more and no fewer, before any receipt may exist.
        if inserted != expected:
            raise ApplyRefusal(f"readback mismatch: wrote {inserted}, batch holds {expected}")
        persisted_counts = expected

    status = "completed" if proven else "failed"
    from src.analysis.open_intelligence.candidates import SOURCE_FAMILY_MAP_VERSION

    # A failed run wrote nothing, so its receipt counts nothing. Carrying
    # the in-memory batch counts would claim rows the tables do not hold.
    receipt_batch = (
        bridge.batch if proven else persistence.OpenIntelligenceRowBatch((), (), (), (), (), ())
    )
    # The receipt carries the digest the written tables report through the same SQL the
    # proof and release recompute; a digest rendered in Python would never match it.
    digest_rows = list(
        query_runner(
            f"SELECT {persistence.row_set_digest_sql(run_id, project=project, dataset=dataset)} "
            "AS row_set_digest"
        )
    )
    reported_digest = digest_rows[0].get("row_set_digest") if len(digest_rows) == 1 else None
    if (
        not isinstance(reported_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", reported_digest) is None
    ):
        raise ApplyRefusal("row set digest readback is unavailable")
    receipt = persistence.build_run_receipt_row(
        receipt_batch,
        (),
        row_set_digest=reported_digest,
        run_id=run_id,
        client_scope_id=scope.client_scope_id,
        market_scope=tuple(scope.market_scope),
        signal_date=signal_date,
        observation_start=observation_start,
        observation_end=signal_date,
        observation_method=APPLY_OBSERVATION_METHOD,
        source_window_digest=source_window_digest,
        cluster_build_version=cluster_build_version,
        source_family_map_version=SOURCE_FAMILY_MAP_VERSION,
        rule_version=rule_version,
        status=status,
        complete_partitions=proven,
        source_sha=source_sha,
        completed_at=created_at,
    )
    query_runner(_receipt_insert_statement(project, dataset, receipt))
    # The digest is computed so the release step can refuse a receipt that
    # changed between read and release.
    run_receipt_digest(receipt)
    return ApplyReport(
        run_id=run_id,
        receipt=receipt,
        admitted=bridge.admitted,
        skipped=bridge.skipped,
        persisted_counts=MappingProxyType(persisted_counts),
    )


# --- Factor evidence collection and component assembly ------------------------
#
# Approved by 42-raw-factor-formula-approval-request-2026-08-29.md. Citing
# evidence for the approved factors comes from the copied staging
# enriched_content rows through two bounded queries: one for every member
# term across the run window, one for the per-market family universe. The
# event ledger participates through composition, not through these ratios;
# that narrowing is deliberate and named here rather than hidden.

from scripts.staging import scoring_qualification
from src.analysis.open_intelligence.candidates import QUALIFYING_SOURCE_FAMILIES, _family
from src.analysis.open_intelligence.rows import EvidenceReceipt


def _component_terms(component: object) -> tuple[str, ...]:
    return tuple(sorted({term.strip().lower() for term in component.terms if term.strip()}))


def collect_component_factor_evidence(
    *,
    query_runner,
    components,
    project: str,
    dataset: str,
    trend_date: date,
    window_start: date,
) -> Mapping[str, scoring_qualification.ComponentFactorEvidence]:
    """One ComponentFactorEvidence per component, from two bounded queries."""
    terms = sorted({term for component in components for term in _component_terms(component)})
    if not terms:
        return MappingProxyType({})
    # A row cites a term when the exact term appears word-bounded in its
    # query term, title or text. The approved wording is "rows whose entity
    # or term matches a member identity"; a post's title and body are where
    # an entity shows up, and query_term alone reaches mostly search rows.
    # Attribute terms in BigQuery and return compact evidence rows. Pulling
    # title and body text into Python duplicates multi-term rows and exceeded
    # the Cloud Run memory ceiling on the closed replay window. Hex literals
    # keep arbitrary engine terms out of SQL quoting.
    term_structs = ", ".join(
        "STRUCT("
        f"CAST(FROM_HEX('{term.encode('utf-8').hex()}') AS STRING) AS term, "
        f"CAST(FROM_HEX('{re.escape(term).encode('utf-8').hex()}') AS STRING) AS pattern"
        ")"
        for term in terms
    )
    citing = list(
        query_runner(
            f"WITH term_patterns AS ("
            f"SELECT term, pattern FROM UNNEST([{term_structs}])"
            f"), source_rows AS ("
            f"SELECT market, DATE(collected_at) AS day, id, platform, "
            f"author_handle_norm, author_name, url, published_at, regional_score, "
            f"LOWER(CONCAT(IFNULL(query_term, ''), ' ', "
            f"SUBSTR(IFNULL(title, ''), 1, 2000), ' ', "
            f"SUBSTR(IFNULL(text, ''), 1, 5000))) AS haystack "
            f"FROM `{project}.{dataset}.enriched_content` "
            f"WHERE DATE(collected_at) BETWEEN '{window_start.isoformat()}' "
            f"AND '{trend_date.isoformat()}' "
            f"AND market IN ('za', 'ng', 'ke')"
            f") SELECT patterns.term AS term, source.market, source.day, "
            f"source.id, source.platform, source.author_handle_norm, "
            f"source.author_name, source.url, source.published_at, source.regional_score "
            f"FROM source_rows AS source JOIN term_patterns AS patterns "
            f"ON REGEXP_CONTAINS(source.haystack, "
            f"CONCAT(r'\\b', patterns.pattern, r'\\b'))"
        )
    )
    universe_rows = list(
        query_runner(
            f"SELECT market, platform FROM `{project}.{dataset}.enriched_content` "
            f"WHERE DATE(collected_at) BETWEEN '{window_start.isoformat()}' "
            f"AND '{trend_date.isoformat()}' "
            f"AND market IN ('za', 'ng', 'ke') "
            f"GROUP BY market, platform"
        )
    )

    # The enriched platform vocabulary spells some engine families
    # differently; aliases translate spelling only, never invent a family.
    platform_aliases = {"google_search": "trends"}

    def _mapped_family(platform: object) -> str | None:
        if not isinstance(platform, str) or not platform.strip():
            return None
        try:
            family = _family(platform_aliases.get(platform.strip().lower(), platform))
        except ValueError:
            return None
        return family if family in QUALIFYING_SOURCE_FAMILIES else None

    universe: dict[str, set[str]] = {}
    for row in universe_rows:
        family = _mapped_family(row.get("platform"))
        market = row.get("market")
        if family and isinstance(market, str):
            universe.setdefault(market, set()).add(family)

    by_market_term: dict[tuple[str, str], list[dict]] = {}
    for row in citing:
        key = (str(row.get("market")), str(row.get("term")))
        by_market_term.setdefault(key, []).append(dict(row))

    evidence: dict[str, scoring_qualification.ComponentFactorEvidence] = {}
    for component in components:
        component_terms = _component_terms(component)
        rows = [
            row
            for term in component_terms
            for row in by_market_term.get((component.market, term), ())
        ]
        current = [row for row in rows if row.get("day") == trend_date]
        current_ids = tuple(sorted({str(row["id"]) for row in current}))
        direction_rows: dict[str, tuple[set[str], set[str]]] = {}
        for row in rows:
            family = _mapped_family(row.get("platform"))
            published = row.get("published_at")
            if family is None or not isinstance(published, datetime):
                continue
            published_day = published.date()
            bucket = direction_rows.setdefault(family, (set(), set()))
            if trend_date - timedelta(days=2) <= published_day <= trend_date:
                bucket[0].add(str(row["id"]))
            elif window_start <= published_day <= trend_date - timedelta(days=3):
                bucket[1].add(str(row["id"]))
        window_ids = tuple(sorted({str(row["id"]) for row in rows}))
        families = tuple(
            sorted({family for row in current if (family := _mapped_family(row.get("platform")))})
        )
        # The approved wording is "distinct creator or author identities":
        # the normalized handle where one exists, the author name otherwise,
        # so a bylined news row is attributable without a social handle.
        creators = tuple(
            sorted(
                {
                    str(voice).strip().lower()
                    for row in current
                    if (voice := row.get("author_handle_norm") or row.get("author_name"))
                    and str(voice).strip()
                }
            )
        )
        clean = tuple(
            sorted(
                {
                    str(row["id"])
                    for row in current
                    if row.get("url") and row.get("published_at") is not None
                }
            )
        )
        confirmed = tuple(
            sorted(
                {
                    str(row["id"])
                    for row in current
                    if isinstance(row.get("regional_score"), (int, float))
                    and not isinstance(row.get("regional_score"), bool)
                    and row["regional_score"] > 0
                }
            )
        )
        # Ruled by Albert on 2026-09-04: the geo gate reads the best citing
        # post's regional score over the window against the market floor.
        best_regional = max(
            (
                float(row["regional_score"])
                for row in rows
                if isinstance(row.get("regional_score"), (int, float))
                and not isinstance(row.get("regional_score"), bool)
            ),
            default=0.0,
        )
        best_regional = min(max(best_regional, 0.0), 1.0)
        current_terms = tuple(sorted({str(row["term"]) for row in current}))
        history_terms = tuple(
            sorted({str(row["term"]) for row in rows if row.get("day") != trend_date})
        )
        evidence[persistence.component_bridge_key(component)] = (
            scoring_qualification.ComponentFactorEvidence(
                current_rows=current_ids,
                window_rows=window_ids,
                families_current=families,
                family_universe=tuple(sorted(universe.get(component.market, ()))),
                creators_current=creators,
                clean_current=clean,
                geo_confirmed_current=confirmed,
                current_member_terms=current_terms,
                history_member_terms=history_terms,
                family_direction_counts=tuple(
                    (family, len(row_sets[0]), len(row_sets[1]))
                    for family, row_sets in sorted(direction_rows.items())
                ),
                geo_best_regional_score=best_regional,
            )
        )
    return MappingProxyType(evidence)


@dataclass(frozen=True)
class AssembledComponentInputs:
    metrics_by_component: Mapping[str, object]
    receipts_by_component: Mapping[str, Mapping[str, object]]
    readiness_by_component: Mapping[str, object]
    readiness_rules: object
    missing_by_component: Mapping[str, tuple[str, ...]]


def _receipt_for_row(
    row_id: str,
    member_identity: str,
    observation,
    projected,
):
    """One honest receipt for one component row.

    Every field is an engine-recorded fact: member, family and platform come
    from the observation that carried the row; url, author, publication time
    and geo provenance come from the projected enriched row when one exists.
    A row with no geo provenance carries zero geo confidence, which counts
    against readiness rather than for it.
    """
    families = tuple(observation.source_families or ())
    platforms = tuple(observation.platforms or ())
    # Every family in the observation is a true fact of this row (an event
    # row's corroborating sources, mapped by the engine's own family map).
    # The receipt carries one; the deterministic first qualifying one is
    # chosen, never a family the row does not actually have.
    family = next((item for item in families if item in QUALIFYING_SOURCE_FAMILIES), None)
    if family is None:
        # Graph observations carry platforms but no family; the engine's own
        # family map translates them, exactly as candidate extraction does.
        for candidate_platform in platforms:
            try:
                mapped = _family(candidate_platform)
            except (ValueError, TypeError):
                continue
            if mapped in QUALIFYING_SOURCE_FAMILIES:
                family = mapped
                break
    if projected is not None:
        with contextlib.suppress(ValueError, TypeError):
            family = _family(projected.platform)
    if family not in QUALIFYING_SOURCE_FAMILIES:
        return None
    platform = (
        projected.platform if projected is not None else (platforms[0] if platforms else family)
    )
    regional = getattr(projected, "regional_score", None) if projected else None
    geo = (
        float(regional)
        if isinstance(regional, (int, float)) and not isinstance(regional, bool)
        else 0.0
    )
    geo = min(max(geo, 0.0), 1.0)
    # Observation timestamps are canonical strings; the latest one parses
    # into the receipt's publication time. An unparseable value stays None
    # rather than becoming a guess.
    published_at = None
    if projected is not None and projected.published_at is not None:
        published_at = projected.published_at
    elif projected is not None and getattr(projected, "collected_at", None) is not None:
        # A row the source never dated (the youtube channel scrape carries no
        # upload date) is still a dated observation: the engine recorded when it
        # collected the row. That time is the receipt's publication floor, so
        # the row stays an available, timestamped receipt rather than failing
        # technical quality for every component it touches.
        published_at = projected.collected_at
    else:
        timestamps = tuple(observation.observed_timestamps or ())
        if timestamps:
            try:
                published_at = datetime.fromisoformat(str(max(timestamps)).replace("Z", "+00:00"))
            except ValueError:
                published_at = None
    if published_at is not None and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    try:
        return EvidenceReceipt(
            member_identity=member_identity,
            row_id=row_id,
            source_family=family,
            platform=platform,
            url=projected.url if projected is not None else None,
            published_at=published_at,
            claim_role="context",
            direction="not_applicable",
            geo_confidence=geo,
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
        )
    except ValueError:
        return None


def assemble_component_inputs(
    *,
    result,
    evidence_by_component: Mapping[str, object],
    trend_date: date,
) -> AssembledComponentInputs:
    """Metrics, receipts and readiness for every admissible component.

    A component enters metrics_by_component only when its factor derivation
    measured AND its exact receipt cover exists AND readiness evaluates;
    anything short of that is skipped with the derivation's or assembly's
    own named reasons. There is no third path.
    """
    readiness_rules = ReadinessRules(
        current_cutoff=datetime.combine(trend_date - timedelta(days=2), time.min, tzinfo=UTC),
        minimum_geo_confidence=REPLAY_READINESS_GEO_FLOOR,
    )
    projection = getattr(result, "evidence_projection", None)
    receipts_by_member = getattr(projection, "receipts_by_member", {}) or {}
    observation_by_member = {
        f"{observation.market}|{observation.candidate_type}|{observation.term}": observation
        for observation in getattr(result, "observations", ())
    }

    metrics_by_component: dict[str, object] = {}
    receipts_by_component: dict[str, Mapping[str, object]] = {}
    readiness_by_component: dict[str, object] = {}
    missing_by_component: dict[str, tuple[str, ...]] = {}

    for component in getattr(result, "components", ()):
        key = persistence.component_bridge_key(component)
        evidence = evidence_by_component.get(key)
        if evidence is None:
            missing_by_component[key] = ("factor_evidence_unavailable",)
            continue
        raw_metrics, reasons = scoring_qualification.derive_component_factor_metrics(evidence)
        if raw_metrics is None:
            missing_by_component[key] = reasons
            continue
        receipts: dict[str, object] = {}
        for member in component.member_identities:
            observation = observation_by_member.get(member)
            if observation is None:
                continue
            projected_rows = {row.row_id: row for row in receipts_by_member.get(member, ())}
            for row_id in observation.row_ids:
                if row_id not in set(component.row_receipts):
                    continue
                receipt = _receipt_for_row(row_id, member, observation, projected_rows.get(row_id))
                if receipt is not None:
                    receipts[row_id] = receipt
        if set(receipts) != set(component.row_receipts):
            missing_by_component[key] = ("evidence_receipts_unavailable",)
            continue
        if {receipt.member_identity for receipt in receipts.values()} != set(
            component.member_identities
        ):
            missing_by_component[key] = ("evidence_receipts_unavailable",)
            continue
        if any(
            receipt.source_family not in component.source_families for receipt in receipts.values()
        ):
            missing_by_component[key] = ("evidence_receipts_unavailable",)
            continue
        from src.analysis.open_intelligence.rows import ObservedSignalMetrics

        metrics = ObservedSignalMetrics(**dict(raw_metrics))
        records = [
            EvidenceRecord(
                row_id=item.row_id,
                source_family=item.source_family,
                direction=item.direction,
                published_at=item.published_at,
                availability=item.availability,
                geo_confidence=item.geo_confidence,
                factual_conflict=item.factual_conflict,
            )
            for item in receipts.values()
        ]
        # No quality evaluation pass exists yet, and saying one ran would be
        # the exact invented readiness this programme refuses. Unchecked is
        # the honest state until a quality pass is built and approved.
        readiness = evaluate_readiness(records, readiness_rules, quality_evaluated=False)
        metrics_by_component[key] = metrics
        receipts_by_component[key] = MappingProxyType(receipts)
        readiness_by_component[key] = readiness
    return AssembledComponentInputs(
        metrics_by_component=MappingProxyType(metrics_by_component),
        receipts_by_component=MappingProxyType(receipts_by_component),
        readiness_by_component=MappingProxyType(readiness_by_component),
        readiness_rules=readiness_rules,
        missing_by_component=MappingProxyType(missing_by_component),
    )


# --- Composition authority ----------------------------------------------------
#
# Approved by 42-composition-authority-approval-request-2026-08-29.md:
# lexical_similarity_v1 as the deterministic semantic provider, and
# composition_rules_v1 as a replay-certified rule bundle recorded in configs
# beside its certification so content and approval cannot drift apart.

COMPOSITION_RULES_PATH = ROOT / "configs" / "open_intelligence_composition_rules.yaml"
COMPOSITION_RULES_V2_PATH = ROOT / "configs" / "open_intelligence_composition_rules_v2.yaml"

_LEXICAL_TOKENIZER_V1 = MappingProxyType(
    {
        "version": "tokenizer_v1",
        "normalization": "nfkc_casefold_ascii_alnum",
        "token_pattern": "[a-z0-9]+",
        "stop_words": (),
    }
)
_LEXICAL_WEIGHTING_V1 = MappingProxyType(
    {
        "version": "weights_v1",
        "term_weight": 1.0,
        "alias_weight": 0.5,
        "topic_weight": 0.25,
    }
)


@dataclass(frozen=True)
class LexicalSimilarityProvider:
    """The deterministic, model-free semantic provider.

    Exactly the lexical mechanism the replay fixtures validate: NFKC
    casefolded alphanumeric tokens, weighted min-over-max overlap per
    same-market pair. Every similarity is recomputable from the two
    observations alone. A pair with no token weight at all is simply
    absent, which composition reads as no vote, never as a crash.
    """

    version: str = "lexical_similarity_v1"

    def similarities(self, observations):
        features = {}
        for observation in observations:
            identity = _semantic_identity(observation)
            features[identity], _units = _lexical_features(
                observation, _LEXICAL_TOKENIZER_V1, _LEXICAL_WEIGHTING_V1  # gitleaks:allow, versioned algorithm identifiers
            )
        scores = {}
        for left, right in combinations(observations, 2):
            if left.market != right.market:
                continue
            left_id = _semantic_identity(left)
            right_id = _semantic_identity(right)
            tokens = set(features[left_id]) | set(features[right_id])
            numerator = sum(
                min(features[left_id].get(token, 0.0), features[right_id].get(token, 0.0))
                for token in tokens
            )
            denominator = sum(
                max(features[left_id].get(token, 0.0), features[right_id].get(token, 0.0))
                for token in tokens
            )
            if denominator <= 0:
                continue
            scores[(left_id, right_id)] = numerator / denominator
        return MappingProxyType(scores)


@dataclass(frozen=True)
class LexicalSimilarityProviderV2:
    version: str = "lexical_similarity_v2"
    pair_ceiling: int = 250_000

    def __post_init__(self):
        if (
            isinstance(self.pair_ceiling, bool)
            or not isinstance(self.pair_ceiling, int)
            or self.pair_ceiling < 1
        ):
            raise ValueError("pair ceiling must be a positive integer")

    def _features(self, observations):
        return {
            _semantic_identity(observation): _lexical_features(
                observation,
                _LEXICAL_TOKENIZER_V1,
                _LEXICAL_WEIGHTING_V1,
            )[0]
            for observation in observations
        }

    def candidate_pairs(self, observations):
        token_index: dict[tuple[str, str], set[str]] = {}
        for observation in observations:
            identity = _semantic_identity(observation)
            anchor_tokens = {
                token
                for value in (observation.term, *observation.aliases)
                for token in _tokens(value, _LEXICAL_TOKENIZER_V1)
            }
            for token in anchor_tokens:
                token_index.setdefault((observation.market, token), set()).add(identity)
        pairs: set[tuple[str, str]] = set()
        counts: dict[str, int] = {}
        for (market, _token), identities in sorted(token_index.items()):
            for pair in combinations(sorted(identities), 2):
                if pair in pairs:
                    continue
                pairs.add(pair)
                counts[market] = counts.get(market, 0) + 1
                if counts[market] > self.pair_ceiling:
                    raise ValueError(
                        "composition_pair_ceiling_exceeded:"
                        f"market={market}:token={_token}:members={len(identities)}:"
                        f"pairs={counts[market]}"
                    )
        return tuple(sorted(pairs))

    def similarities(self, observations, candidate_pairs):
        features = self._features(observations)
        scores = {}
        for pair in candidate_pairs:
            left_id, right_id = pair
            if left_id not in features or right_id not in features:
                raise ValueError("candidate pair identity is unknown")
            tokens = set(features[left_id]) | set(features[right_id])
            numerator = sum(
                min(features[left_id].get(token, 0.0), features[right_id].get(token, 0.0))
                for token in tokens
            )
            denominator = sum(
                max(features[left_id].get(token, 0.0), features[right_id].get(token, 0.0))
                for token in tokens
            )
            if denominator <= 0:
                raise ValueError("candidate pair has no lexical feature weight")
            scores[pair] = numerator / denominator
        return MappingProxyType(scores)


@dataclass(frozen=True)
class CompositionRuleBundle:
    status: str
    rule_version: str
    replay_receipt_id: str
    approved_at: datetime
    approved_by: str
    expires_at: datetime
    graph_rules: GraphRules


@dataclass(frozen=True)
class CompositionRuleBundleV2:
    status: str
    rule_version: str
    approval_contract_sha256: str
    replay_receipt_id: str | None
    approved_at: datetime
    approved_by: str
    expires_at: datetime
    graph_rules: AnchoredGraphRules


def _composition_text(document, field):
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ApplyRefusal(f"composition rule {field} is missing")
    return value.strip()


def _composition_timestamp(value, field):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ApplyRefusal(f"composition rule {field} is malformed") from error
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise ApplyRefusal(f"composition rule {field} is malformed")


def load_composition_rules(path, now: datetime) -> CompositionRuleBundle:
    import yaml

    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ApplyRefusal("composition rule config is unreadable") from error
    if not isinstance(document, Mapping):
        raise ApplyRefusal("composition rule config must be a mapping")
    if document.get("status") != "replay_certified":
        raise ApplyRefusal("composition rules are not replay certified")
    rule_version = _composition_text(document, "rule_version")
    if rule_version != "composition_rules_v1":
        raise ApplyRefusal("composition rule version is not composition_rules_v1")
    approved_at = _composition_timestamp(document.get("approved_at"), "approved_at")
    expires_at = _composition_timestamp(document.get("expires_at"), "expires_at")
    if approved_at > now:
        raise ApplyRefusal("composition rule approval is in the future")
    if expires_at <= now:
        raise ApplyRefusal("composition rule certification is expired")
    values = document.get("graph_rules")
    if not isinstance(values, Mapping) or set(values) != {
        "semantic_vote_floor",
        "component_similarity_floor",
        "temporal_overlap_days",
    }:
        raise ApplyRefusal("composition graph rules are malformed")
    try:
        graph_rules = GraphRules(
            values["semantic_vote_floor"],
            values["component_similarity_floor"],
            values["temporal_overlap_days"],
        )
    except ValueError as error:
        raise ApplyRefusal("composition graph rules are invalid") from error
    return CompositionRuleBundle(
        status="replay_certified",
        rule_version=rule_version,
        replay_receipt_id=_composition_text(document, "replay_receipt_id"),
        approved_at=approved_at,
        approved_by=_composition_text(document, "approved_by"),
        expires_at=expires_at,
        graph_rules=graph_rules,
    )


def load_composition_rules_v2(
    path,
    now: datetime,
    *,
    require_certified: bool,
) -> CompositionRuleBundleV2:
    import yaml

    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ApplyRefusal("composition v2 rule config is unreadable") from error
    if not isinstance(document, Mapping):
        raise ApplyRefusal("composition v2 rule config must be a mapping")
    status = document.get("status")
    if status not in {"implementation_approved", "replay_certified"}:
        raise ApplyRefusal("composition v2 rule status is invalid")
    if require_certified and status != "replay_certified":
        raise ApplyRefusal("composition v2 rules are not replay certified")
    rule_version = _composition_text(document, "rule_version")
    if rule_version != "composition_rules_v2":
        raise ApplyRefusal("composition v2 rule version is invalid")
    approval_hash = _composition_text(document, "approval_contract_sha256")
    if approval_hash != "7c2a2ca0fce4b2fe5fd57e45b2b17ff96dcd1e0ec2db70dc17fa4f63d4336ca2":
        raise ApplyRefusal("composition v2 approval contract digest is invalid")
    replay_receipt_id = document.get("replay_receipt_id")
    if replay_receipt_id is not None and (
        not isinstance(replay_receipt_id, str) or not replay_receipt_id.strip()
    ):
        raise ApplyRefusal("composition v2 replay receipt is malformed")
    if require_certified and not replay_receipt_id:
        raise ApplyRefusal("composition v2 replay receipt is missing")
    approved_at = _composition_timestamp(document.get("approved_at"), "approved_at")
    expires_at = _composition_timestamp(document.get("expires_at"), "expires_at")
    if approved_at > now:
        raise ApplyRefusal("composition v2 approval is in the future")
    if expires_at <= now:
        raise ApplyRefusal("composition v2 certification is expired")
    values = document.get("graph_rules")
    if not isinstance(values, Mapping) or set(values) != {
        "semantic_anchor_floor",
        "temporal_overlap_days",
        "pair_ceiling",
        "component_member_ceiling",
    }:
        raise ApplyRefusal("composition v2 graph rules are malformed")
    try:
        graph_rules = AnchoredGraphRules(
            values["semantic_anchor_floor"],
            values["temporal_overlap_days"],
            values["pair_ceiling"],
            values["component_member_ceiling"],
        )
    except ValueError as error:
        raise ApplyRefusal("composition v2 graph rules are invalid") from error
    return CompositionRuleBundleV2(
        status=status,
        rule_version=rule_version,
        approval_contract_sha256=approval_hash,
        replay_receipt_id=replay_receipt_id,
        approved_at=approved_at,
        approved_by=_composition_text(document, "approved_by"),
        expires_at=expires_at,
        graph_rules=graph_rules,
    )


if __name__ == "__main__":
    import sys

    if "--apply-run" in sys.argv[1:]:
        sys.stdout.write(run_apply_cli(sys.argv[1:]))
    elif (
        not sys.argv[1:]
        and os.environ.get("CLOUD_RUN_JOB")
        == (_r3_apply_profile(mode="new_consume")[2].job_resource.rsplit("/", 1)[1])
    ):
        sys.stdout.write(run_apply_cli([]))
    else:
        print(render_dry_run())
