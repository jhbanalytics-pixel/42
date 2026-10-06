"""Contract tests for the Open Intelligence BigQuery DDL package."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import scripts.setup_bigquery as setup
from src.contracts.bigquery_ddl import parse_table_ddl

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCHEMAS_DIR = _ROOT / "infra" / "bigquery_schemas"

Field = tuple[str, str, str, str | None, tuple]

_SCOPE_FIELDS: tuple[Field, ...] = (
    ("client_scope_id", "STRING", "REQUIRED", "Resolved client scope.", ()),
    ("market_scope", "STRING", "REPEATED", "Markets resolved for the run.", ()),
    (
        "brand_config_id",
        "STRING",
        "REQUIRED",
        "Resolved brand configuration.",
        (),
    ),
    (
        "audience_lens_ids",
        "STRING",
        "REPEATED",
        "Resolved audience lenses, possibly empty.",
        (),
    ),
    ("theme_id", "STRING", "REQUIRED", "Resolved theme.", ()),
)

_CONTRACTS = {
    "signal_candidates_v2.sql": {
        "fields": (
            ("client_scope_id", "STRING", "REQUIRED", "Resolved client scope.", ()),
            ("market_scope", "STRING", "REPEATED", "Markets resolved for the run.", ()),
            ("brand_config_id", "STRING", "REQUIRED", "Resolved brand configuration.", ()),
            (
                "audience_lens_ids",
                "STRING",
                "REPEATED",
                "Resolved audience lenses, possibly empty.",
                (),
            ),
            ("theme_id", "STRING", "REQUIRED", "Resolved theme.", ()),
            ("run_id", "STRING", "REQUIRED", "Logical build run.", ()),
            ("contract_version", "STRING", "REQUIRED", "Contract used by the writer.", ()),
            ("signal_id", "STRING", "REQUIRED", "Stable signal identity.", ()),
            ("signal_date", "DATE", "REQUIRED", "Daily snapshot date and partition key.", ()),
            ("market", "STRING", "REQUIRED", "Lower case market code in market_scope.", ()),
            (
                "label",
                "STRING",
                "REQUIRED",
                "Most central observed phrase. Gemini does not choose membership.",
                (),
            ),
            (
                "cluster_signature",
                "STRING",
                "REQUIRED",
                "Deterministic digest of canonical membership inputs.",
                (),
            ),
            (
                "cluster_build_version",
                "STRING",
                "REQUIRED",
                "Deterministic clustering rule version.",
                (),
            ),
            (
                "model_version",
                "STRING",
                "NULLABLE",
                "Model used only for a promoted summary. Null when no model summary ran.",
                (),
            ),
            (
                "discovery_mode",
                "STRING",
                "REQUIRED",
                "Dynamic, replay, or canary. Canary is valid only in QA.",
                (),
            ),
            (
                "topic_tags",
                "STRING",
                "REPEATED",
                "Optional legacy taxonomy relationships. Tags never decide identity.",
                (),
            ),
            ("novelty_score", "FLOAT64", "REQUIRED", "Bounded 0 to 1.", ()),
            ("velocity_score", "FLOAT64", "REQUIRED", "Bounded 0 to 1.", ()),
            ("breadth_score", "FLOAT64", "REQUIRED", "Bounded 0 to 1.", ()),
            ("independence_score", "FLOAT64", "REQUIRED", "Bounded 0 to 1.", ()),
            (
                "historical_similarity",
                "FLOAT64",
                "NULLABLE",
                "Bounded 0 to 1. Null when no valid historical comparison exists.",
                (),
            ),
            ("geo_confidence", "FLOAT64", "REQUIRED", "Bounded 0 to 1.", ()),
            (
                "evidence_state",
                "STRING",
                "REQUIRED",
                "Ready, thin, contradictory, or unchecked.",
                (),
            ),
            ("created_at", "TIMESTAMP", "REQUIRED", "Persist time in UTC.", ()),
            (
                "label_member_identity",
                "STRING",
                "NULLABLE",
                "Exact winning observation identity that owns the candidate label.",
                (),
            ),
        ),
        "partition": "signal_date",
        "cluster": ("market", "evidence_state"),
        "expiration": None,
        "description": "One daily snapshot of one stable signal identity per market and run. Natural key: (client_scope_id, signal_date, market, signal_id, run_id).",
    },
    "signal_evidence_v2.sql": {
        "fields": (
            ("client_scope_id", "STRING", "REQUIRED", "Resolved client scope.", ()),
            ("market_scope", "STRING", "REPEATED", "Markets resolved for the run.", ()),
            ("brand_config_id", "STRING", "REQUIRED", "Resolved brand configuration.", ()),
            (
                "audience_lens_ids",
                "STRING",
                "REPEATED",
                "Resolved audience lenses, possibly empty.",
                (),
            ),
            ("theme_id", "STRING", "REQUIRED", "Resolved theme.", ()),
            ("run_id", "STRING", "REQUIRED", "Evidence extraction run.", ()),
            ("contract_version", "STRING", "REQUIRED", "Contract used by the writer.", ()),
            ("signal_date", "DATE", "REQUIRED", "Signal snapshot date.", ()),
            ("market", "STRING", "REQUIRED", "Lower case market code.", ()),
            ("signal_id", "STRING", "REQUIRED", "Parent signal identity.", ()),
            ("evidence_id", "STRING", "REQUIRED", "Stable evidence identifier.", ()),
            ("row_id", "STRING", "REQUIRED", "Source row identifier.", ()),
            (
                "source_family",
                "STRING",
                "REQUIRED",
                "Independent channel family from the current engine family map.",
                (),
            ),
            ("platform", "STRING", "REQUIRED", "Normalized observed platform.", ()),
            (
                "url",
                "STRING",
                "NULLABLE",
                "Direct evidence URL. Null when the source provides none.",
                (),
            ),
            (
                "published_at",
                "TIMESTAMP",
                "NULLABLE",
                "Source publish time. Null cannot qualify as current evidence.",
                (),
            ),
            (
                "claim_role",
                "STRING",
                "REQUIRED",
                "Identity, direction, context, contradiction, or geo.",
                (),
            ),
            (
                "direction",
                "STRING",
                "REQUIRED",
                "Rising, stable, declining, conflicting, or not_applicable.",
                (),
            ),
            ("geo_confidence", "FLOAT64", "REQUIRED", "Bounded 0 to 1.", ()),
            (
                "source_label",
                "STRING",
                "NULLABLE",
                "Human readable source or host retained for evidence display.",
                (),
            ),
            (
                "author_label",
                "STRING",
                "NULLABLE",
                "Public author label when policy permits retention.",
                (),
            ),
            (
                "excerpt",
                "STRING",
                "NULLABLE",
                "Sanitized excerpt. Null when retention or policy prevents display.",
                (),
            ),
            (
                "metric_label",
                "STRING",
                "NULLABLE",
                "Observed metric with unit. Never inferred.",
                (),
            ),
            ("availability", "STRING", "REQUIRED", "Available, aged_out, or unavailable.", ()),
            (
                "evidence_state",
                "STRING",
                "REQUIRED",
                "State assigned to the parent evaluation.",
                (),
            ),
            ("created_at", "TIMESTAMP", "REQUIRED", "Persist time in UTC.", ()),
            (
                "vendor_family",
                "STRING",
                "NULLABLE",
                "Canonical data supplier family. Distinct from channel family.",
                (),
            ),
            (
                "channel_family",
                "STRING",
                "NULLABLE",
                "Canonical evidence channel. Equals source_family compatibility alias.",
                (),
            ),
        ),
        "partition": "signal_date",
        "cluster": ("market", "signal_id", "source_family", "evidence_state"),
        "expiration": None,
        "description": "One observed evidence row attached to one signal snapshot. Natural key: (client_scope_id, signal_date, market, signal_id, evidence_id, run_id).",
    },
    "signal_membership_v2.sql": {
        "fields": (
            ("client_scope_id", "STRING", "REQUIRED", "Resolved client scope.", ()),
            ("market_scope", "STRING", "REPEATED", "Markets resolved for the run.", ()),
            ("brand_config_id", "STRING", "REQUIRED", "Resolved brand configuration.", ()),
            (
                "audience_lens_ids",
                "STRING",
                "REPEATED",
                "Resolved audience lenses, possibly empty.",
                (),
            ),
            ("theme_id", "STRING", "REQUIRED", "Resolved theme.", ()),
            ("run_id", "STRING", "REQUIRED", "Membership snapshot run.", ()),
            ("contract_version", "STRING", "REQUIRED", "Contract used by the writer.", ()),
            ("signal_date", "DATE", "REQUIRED", "Signal snapshot date and partition key.", ()),
            ("market", "STRING", "REQUIRED", "Lower case market code in market_scope.", ()),
            ("signal_id", "STRING", "REQUIRED", "Parent signal identity.", ()),
            (
                "member_id",
                "STRING",
                "REQUIRED",
                "Stable mem_ identifier derived from the complete member facts.",
                (),
            ),
            (
                "member_identity",
                "STRING",
                "REQUIRED",
                "Stable observation identity used by component membership.",
                (),
            ),
            ("candidate_type", "STRING", "REQUIRED", "Accepted candidate observation type.", ()),
            (
                "canonical_value",
                "STRING",
                "REQUIRED",
                "Canonical candidate value used for identity.",
                (),
            ),
            (
                "source_families",
                "STRING",
                "REPEATED",
                "Complete sorted independent source families for this member.",
                (),
            ),
            ("platforms", "STRING", "REPEATED", "Complete sorted platforms for this member.", ()),
            ("row_id", "STRING", "REQUIRED", "Real aggregate or row level source identifier.", ()),
            (
                "qualifies_evidence",
                "BOOL",
                "REQUIRED",
                "Whether this member has an accepted row level evidence receipt.",
                (),
            ),
            ("created_at", "TIMESTAMP", "REQUIRED", "Persist time in UTC.", ()),
            (
                "vendor_families",
                "STRING",
                "REPEATED",
                "Complete sorted data supplier families for this member.",
                (),
            ),
            (
                "channel_families",
                "STRING",
                "REPEATED",
                "Complete sorted channel families. Equals source_families compatibility aliases.",
                (),
            ),
            (
                "source_provenance_json",
                "STRING",
                "NULLABLE",
                "Canonical sampled source provenance envelope for hybrid_graph_v3. Null for legacy rows.",
                (),
            ),
        ),
        "partition": "signal_date",
        "cluster": ("market", "signal_id", "candidate_type"),
        "expiration": 400,
        "description": "One immutable member of one V3 signal snapshot and run, retaining the complete stable membership facts needed for identity and lineage replay.",
    },
    "signal_lineage_v2.sql": {
        "fields": (
            *_SCOPE_FIELDS,
            ("run_id", "STRING", "REQUIRED", "Lineage build run.", ()),
            (
                "contract_version",
                "STRING",
                "REQUIRED",
                "Contract used by the writer.",
                (),
            ),
            (
                "signal_date",
                "DATE",
                "REQUIRED",
                "Date on which the relation was observed.",
                (),
            ),
            ("market", "STRING", "REQUIRED", "Lower case market code.", ()),
            (
                "from_signal_id",
                "STRING",
                "REQUIRED",
                "Earlier or contributing identity.",
                (),
            ),
            (
                "to_signal_id",
                "STRING",
                "REQUIRED",
                "Continuing or resulting identity.",
                (),
            ),
            (
                "relation",
                "STRING",
                "REQUIRED",
                "Continues, merges_into, or splits_into.",
                (),
            ),
            ("overlap_score", "FLOAT64", "REQUIRED", "Bounded 0 to 1.", ()),
            ("created_at", "TIMESTAMP", "REQUIRED", "Persist time in UTC.", ()),
        ),
        "partition": "signal_date",
        "cluster": ("market", "relation", "from_signal_id", "to_signal_id"),
        "expiration": None,
        "description": "One directed lineage edge created by one run. Natural key: (client_scope_id, signal_date, market, from_signal_id, to_signal_id, relation, run_id).",
    },
    "signal_analysis_v2.sql": {
        "fields": (
            *_SCOPE_FIELDS,
            ("run_id", "STRING", "REQUIRED", "Analysis run.", ()),
            (
                "contract_version",
                "STRING",
                "REQUIRED",
                "Contract used by the writer.",
                (),
            ),
            (
                "analysis_id",
                "STRING",
                "REQUIRED",
                "Deterministic analysis identifier.",
                (),
            ),
            ("signal_id", "STRING", "REQUIRED", "Parent signal.", ()),
            ("signal_date", "DATE", "REQUIRED", "Signal snapshot date.", ()),
            ("market", "STRING", "REQUIRED", "Lower case market code.", ()),
            (
                "evidence_state",
                "STRING",
                "REQUIRED",
                "Ready, thin, contradictory, or unchecked.",
                (),
            ),
            ("summary", "STRING", "REQUIRED", "Plain language signal read.", ()),
            (
                "why_now",
                "STRING",
                "REQUIRED",
                "Dated explanation limited to supported evidence.",
                (),
            ),
            (
                "possible_response",
                "STRING",
                "NULLABLE",
                "One bounded strategist response. Null when evidence cannot support one.",
                (),
            ),
            (
                "limitations",
                "STRING",
                "REPEATED",
                "Named missing evidence and scope limits.",
                (),
            ),
            (
                "contradictions",
                "STRING",
                "REPEATED",
                "Named disagreements. Empty when none qualify.",
                (),
            ),
            (
                "evidence_ids",
                "STRING",
                "REPEATED",
                "Evidence used by the analysis. At least one for a nonempty claim.",
                (),
            ),
            (
                "human_review_required",
                "BOOL",
                "REQUIRED",
                "True for election scoped output before export.",
                (),
            ),
            (
                "model_version",
                "STRING",
                "NULLABLE",
                "Exact model identifier. Null for deterministic fallback.",
                (),
            ),
            ("analyzed_at", "TIMESTAMP", "REQUIRED", "Analysis time in UTC.", ()),
        ),
        "partition": "signal_date",
        "cluster": ("market", "evidence_state", "human_review_required", "signal_id"),
        "expiration": None,
        "description": "One evidence bounded analysis for one signal snapshot and run. Natural key: (client_scope_id, signal_date, market, signal_id, analysis_id).",
    },
    "signal_predictions_v2.sql": {
        "fields": (
            *_SCOPE_FIELDS,
            ("run_id", "STRING", "REQUIRED", "Prediction run.", ()),
            (
                "contract_version",
                "STRING",
                "REQUIRED",
                "Contract used by the writer.",
                (),
            ),
            (
                "prediction_id",
                "STRING",
                "REQUIRED",
                "Deterministic immutable prediction identifier.",
                (),
            ),
            ("signal_id", "STRING", "REQUIRED", "Promoted signal.", ()),
            ("signal_date", "DATE", "REQUIRED", "Promotion snapshot date.", ()),
            ("market", "STRING", "REQUIRED", "Lower case market code.", ()),
            (
                "discovery_mode",
                "STRING",
                "REQUIRED",
                "Same vocabulary as candidate discovery mode.",
                (),
            ),
            (
                "source_families",
                "STRING",
                "REPEATED",
                "Qualifying independent source families at prediction time.",
                (),
            ),
            (
                "evidence_state",
                "STRING",
                "REQUIRED",
                "Evidence state at prediction time.",
                (),
            ),
            (
                "first_seen_at",
                "TIMESTAMP",
                "REQUIRED",
                "First observed qualifying evidence time.",
                (),
            ),
            (
                "predicted_at",
                "TIMESTAMP",
                "REQUIRED",
                "Prediction creation time.",
                (),
            ),
            (
                "expected_trajectory",
                "STRING",
                "REQUIRED",
                "Peaked, sustained, growing, or fading.",
                (),
            ),
            (
                "evaluation_date",
                "DATE",
                "REQUIRED",
                "Date after which the outcome job may score this row.",
                (),
            ),
            (
                "baseline",
                "RECORD",
                "REQUIRED",
                "Observed baseline at prediction time. Every child mode is listed below.",
                (
                    ("velocity", "FLOAT64", "REQUIRED", None, ()),
                    ("breadth", "FLOAT64", "REQUIRED", None, ()),
                    ("evidence_family_count", "INT64", "REQUIRED", None, ()),
                ),
            ),
            (
                "promotion_target",
                "RECORD",
                "REQUIRED",
                "Declared target. Every child mode is listed below.",
                (
                    ("velocity", "FLOAT64", "REQUIRED", None, ()),
                    ("breadth", "FLOAT64", "REQUIRED", None, ()),
                    ("evidence_family_count", "INT64", "REQUIRED", None, ()),
                ),
            ),
            (
                "invalidation_condition",
                "STRING",
                "REQUIRED",
                "One measurable condition that would invalidate the prediction.",
                (),
            ),
            (
                "cluster_build_version",
                "STRING",
                "REQUIRED",
                "Identity build version used at prediction time.",
                (),
            ),
            (
                "source_family_map_version",
                "STRING",
                "REQUIRED",
                "Channel family mapping version used for attribution.",
                (),
            ),
            (
                "rule_version",
                "STRING",
                "REQUIRED",
                "Deterministic prediction rule version.",
                (),
            ),
            (
                "display_eligible",
                "BOOL",
                "REQUIRED",
                "System readiness gate. False until the weekly outcome loop is deployed and proven, then true for new eligible predictions without waiting for their own outcomes.",
                (),
            ),
        ),
        "partition": "evaluation_date",
        "cluster": ("market", "discovery_mode", "expected_trajectory", "evidence_state"),
        "expiration": None,
        "description": "One immutable prediction made when a signal is promoted. Natural key: prediction_id.",
    },
    "signal_outcomes_v2.sql": {
        "fields": (
            *_SCOPE_FIELDS,
            ("run_id", "STRING", "REQUIRED", "Outcome evaluation run.", ()),
            (
                "contract_version",
                "STRING",
                "REQUIRED",
                "Contract used by the writer.",
                (),
            ),
            (
                "outcome_id",
                "STRING",
                "REQUIRED",
                "Deterministic outcome identifier.",
                (),
            ),
            ("prediction_id", "STRING", "REQUIRED", "Parent prediction.", ()),
            ("signal_id", "STRING", "REQUIRED", "Evaluated signal.", ()),
            ("signal_date", "DATE", "REQUIRED", "Original promotion date.", ()),
            ("market", "STRING", "REQUIRED", "Lower case market code.", ()),
            (
                "discovery_mode",
                "STRING",
                "REQUIRED",
                "Discovery mode at prediction time.",
                (),
            ),
            (
                "source_families",
                "STRING",
                "REPEATED",
                "Qualifying families copied from the prediction for attribution. No aggregate sentinel.",
                (),
            ),
            (
                "source_family_map_version",
                "STRING",
                "REQUIRED",
                "Channel family mapping version used for attribution.",
                (),
            ),
            (
                "evaluation_date",
                "DATE",
                "REQUIRED",
                "Contracted evaluation date.",
                (),
            ),
            ("evaluated_at", "TIMESTAMP", "REQUIRED", "Actual evaluation time.", ()),
            (
                "outcome",
                "STRING",
                "REQUIRED",
                "Peaked, sustained, fizzled, noise, or unresolved.",
                (),
            ),
            (
                "observed_velocity",
                "FLOAT64",
                "NULLABLE",
                "Metric at evaluation.",
                (),
            ),
            (
                "observed_breadth",
                "FLOAT64",
                "NULLABLE",
                "Metric at evaluation.",
                (),
            ),
            (
                "observed_evidence_family_count",
                "INT64",
                "NULLABLE",
                "Qualifying families at evaluation.",
                (),
            ),
            (
                "human_calibration_label",
                "STRING",
                "NULLABLE",
                "Human sample label when present. No model judgment.",
                (),
            ),
            (
                "human_reviewed_at",
                "TIMESTAMP",
                "NULLABLE",
                "Human calibration time.",
                (),
            ),
            (
                "resolution_reason",
                "STRING",
                "REQUIRED",
                "Named rule or insufficiency that produced the outcome.",
                (),
            ),
            ("rule_version", "STRING", "REQUIRED", "Outcome rule version.", ()),
        ),
        "partition": "evaluation_date",
        "cluster": ("market", "outcome", "discovery_mode"),
        "expiration": None,
        "description": "Exactly one immutable scored result per prediction and evaluation run. Natural key: (prediction_id, evaluation_date, run_id).",
    },
    "source_performance_daily_v2.sql": {
        "fields": (
            *_SCOPE_FIELDS,
            ("run_id", "STRING", "REQUIRED", "Performance snapshot run.", ()),
            (
                "contract_version",
                "STRING",
                "REQUIRED",
                "Contract used by the writer.",
                (),
            ),
            (
                "source_performance_id",
                "STRING",
                "REQUIRED",
                "Deterministic row identifier.",
                (),
            ),
            ("metric_date", "DATE", "REQUIRED", "Daily metric date.", ()),
            ("vendor", "STRING", "REQUIRED", "Vendor identifier.", ()),
            ("http_method", "STRING", "REQUIRED", "Upper case HTTP method.", ()),
            (
                "route_path",
                "STRING",
                "REQUIRED",
                "Canonical route path beginning with slash.",
                (),
            ),
            (
                "endpoint_id",
                "STRING",
                "REQUIRED",
                "Digest derived from http_method plus route_path.",
                (),
            ),
            (
                "route_role",
                "STRING",
                "REQUIRED",
                "Evidence, utility_balance, utility_catalog, identity_discovery, identity_hygiene, or inventory.",
                (),
            ),
            (
                "source_family",
                "STRING",
                "REQUIRED",
                "Normalized independent source family.",
                (),
            ),
            (
                "market",
                "STRING",
                "NULLABLE",
                "Lower case market when metrics are market specific.",
                (),
            ),
            (
                "status",
                "STRING",
                "REQUIRED",
                "Active, pilot, available_unwired, blocked, permanently_rejected, or inventory_only.",
                (),
            ),
            ("calls", "INT64", "NULLABLE", "Allowed only for active or pilot.", ()),
            (
                "credits",
                "NUMERIC",
                "NULLABLE",
                "Allowed only for active or pilot.",
                (),
            ),
            (
                "rows",
                "INT64",
                "NULLABLE",
                "Distinct items the route returned; present for active and permanently_rejected inventory routes. For identity roles, normalized result count after deterministic normalization and deduplication.",
                (),
            ),
            (
                "integrity",
                "FLOAT64",
                "NULLABLE",
                "Bounded 0 to 1. Allowed only for active or pilot.",
                (),
            ),
            (
                "geo_precision",
                "FLOAT64",
                "NULLABLE",
                "Bounded 0 to 1. Allowed only for active or pilot.",
                (),
            ),
            (
                "unique_lift",
                "FLOAT64",
                "NULLABLE",
                "Marginal downstream row count, candidate terms plus evidence rows only this route produced; present for active and permanently_rejected inventory routes.",
                (),
            ),
            (
                "last_success_at",
                "TIMESTAMP",
                "NULLABLE",
                "Allowed only for active or pilot.",
                (),
            ),
            (
                "downstream_consumers",
                "STRING",
                "REPEATED",
                "Allowed only for active or pilot.",
                (),
            ),
            (
                "official_capability",
                "STRING",
                "REQUIRED",
                "Sanitized official capability statement.",
                (),
            ),
            (
                "official_parameters",
                "STRING",
                "REPEATED",
                "Sanitized official parameters.",
                (),
            ),
            (
                "official_price_components",
                "RECORD",
                "REPEATED",
                "Exact price components. Required for available_unwired; nested modes are listed below.",
                (
                    ("meter", "STRING", "REQUIRED", None, ()),
                    ("billing_basis", "STRING", "REQUIRED", None, ()),
                    ("currency", "STRING", "REQUIRED", None, ()),
                    ("unit", "STRING", "REQUIRED", None, ()),
                    ("quantity", "NUMERIC", "REQUIRED", None, ()),
                    ("source_url", "STRING", "REQUIRED", None, ()),
                    ("checked_at", "TIMESTAMP", "REQUIRED", None, ()),
                    ("amount", "NUMERIC", "NULLABLE", None, ()),
                    ("minimum_amount", "NUMERIC", "NULLABLE", None, ()),
                    ("maximum_amount", "NUMERIC", "NULLABLE", None, ()),
                    ("tier_condition", "STRING", "NULLABLE", None, ()),
                ),
            ),
            (
                "blocking_reason",
                "STRING",
                "NULLABLE",
                "Required only for blocked.",
                (),
            ),
            (
                "kill_test_result",
                "STRING",
                "NULLABLE",
                "Required for permanently_rejected: the kill test reason, equal to blocking_reason.",
                (),
            ),
            (
                "review_date",
                "DATE",
                "NULLABLE",
                "Null. Reserved for a reviewed rejection, which no writer produces.",
                (),
            ),
            (
                "last_checked_at",
                "TIMESTAMP",
                "REQUIRED",
                "Catalog or live drift check time.",
                (),
            ),
            (
                "balance",
                "NUMERIC",
                "NULLABLE",
                "Free balance observation on the canonical balance route only.",
                (),
            ),
            (
                "observed_at",
                "TIMESTAMP",
                "NULLABLE",
                "Balance observation time on the canonical balance route.",
                (),
            ),
            (
                "balance_read_status",
                "STRING",
                "NULLABLE",
                "Ok or unknown on the canonical balance route.",
                (),
            ),
            (
                "recent_deductions",
                "NUMERIC",
                "NULLABLE",
                "Measured deductions bridging the prior and current successful balance observations.",
                (),
            ),
            (
                "deductions_interval",
                "RECORD",
                "NULLABLE",
                "Exact measurement interval with nested modes below.",
                (
                    ("start_at", "TIMESTAMP", "REQUIRED", None, ()),
                    ("end_at", "TIMESTAMP", "REQUIRED", None, ()),
                ),
            ),
            (
                "funding_math_status",
                "STRING",
                "NULLABLE",
                "Complete or unknown on the balance route.",
                (),
            ),
            (
                "funded_increase_amount",
                "NUMERIC",
                "NULLABLE",
                "Derived top up amount when funding math is complete.",
                (),
            ),
            (
                "funded_increase_observed",
                "BOOL",
                "NULLABLE",
                "Fail closed funding gate. False on unknown, null on nonbalance routes.",
                (),
            ),
            (
                "selected_top_up_credits",
                "NUMERIC",
                "NULLABLE",
                "Commercially selected top up amount, not proof of payment or funding.",
                (),
            ),
            (
                "baseline_credits_per_day",
                "NUMERIC",
                "NULLABLE",
                "Measured completed day baseline.",
                (),
            ),
            (
                "baseline_window",
                "RECORD",
                "NULLABLE",
                "Measured baseline window with nested modes below.",
                (
                    ("start_date", "DATE", "REQUIRED", None, ()),
                    ("end_date", "DATE", "REQUIRED", None, ()),
                    ("completed_days", "INT64", "REQUIRED", None, ()),
                ),
            ),
            ("runway_days", "NUMERIC", "NULLABLE", "Measured runway.", ()),
            (
                "monthly_optional_credit_cap",
                "NUMERIC",
                "NULLABLE",
                "Required balance route control, fixed at 25,000 until a new approval.",
                (),
            ),
            (
                "monthly_optional_credits_used",
                "NUMERIC",
                "NULLABLE",
                "Measured optional lane credits in the current calendar month.",
                (),
            ),
            (
                "optional_calls_enabled",
                "BOOL",
                "NULLABLE",
                "Required on the balance route, null on other routes.",
                (),
            ),
            (
                "platform",
                "STRING",
                "NULLABLE",
                "Canonical vendor platform for inventory rows.",
                (),
            ),
            (
                "resource",
                "STRING",
                "NULLABLE",
                "Canonical vendor resource for inventory rows.",
                (),
            ),
            (
                "catalog_digest",
                "STRING",
                "NULLABLE",
                "Approved catalog snapshot digest for inventory rows.",
                (),
            ),
            (
                "official_credits",
                "NUMERIC",
                "NULLABLE",
                "Vendor documented credit quantity for the inventory route.",
                (),
            ),
            (
                "official_credits_label",
                "STRING",
                "NULLABLE",
                "Vendor documented credit label for the inventory route.",
                (),
            ),
            (
                "official_archetype",
                "STRING",
                "NULLABLE",
                "Vendor documented route archetype.",
                (),
            ),
            (
                "catalog_paginated",
                "BOOL",
                "NULLABLE",
                "Vendor catalog pagination flag.",
                (),
            ),
            (
                "catalog_metered",
                "BOOL",
                "NULLABLE",
                "Vendor catalog metering flag.",
                (),
            ),
            (
                "cache_ttl_seconds",
                "INT64",
                "NULLABLE",
                "Vendor catalog cache lifetime in seconds.",
                (),
            ),
            (
                "delivery",
                "STRING",
                "NULLABLE",
                "Vendor documented delivery mode.",
                (),
            ),
            (
                "docs_url",
                "STRING",
                "NULLABLE",
                "Sanitized official documentation URL.",
                (),
            ),
            ("vendor_family", "STRING", "NULLABLE", "Canonical supplier family.", ()),
            (
                "channel_family",
                "STRING",
                "NULLABLE",
                "Canonical channel family. Equals source_family compatibility alias.",
                (),
            ),
            (
                "funded_lane",
                "STRING",
                "NULLABLE",
                "Funded execution lane when a route is piloted under a bounded stage.",
                (),
            ),
        ),
        "partition": "metric_date",
        "cluster": ("vendor", "status", "source_family", "market"),
        "expiration": None,
        "description": "One daily row per vendor route and observed market. Catalog only routes use null market. Natural key: source_performance_id.",
    },
    "canary_results_v2.sql": {
        "fields": (
            *_SCOPE_FIELDS,
            ("run_id", "STRING", "REQUIRED", "Logical canary run.", ()),
            (
                "contract_version",
                "STRING",
                "REQUIRED",
                "Contract used by the writer.",
                (),
            ),
            (
                "canary_run_id",
                "STRING",
                "REQUIRED",
                "Immutable identifier for one canary execution.",
                (),
            ),
            ("canary_id", "STRING", "REQUIRED", "Approved canary case identifier.", ()),
            (
                "expected_result",
                "STRING",
                "REQUIRED",
                "Expected bounded canary result.",
                (),
            ),
            (
                "actual_result",
                "STRING",
                "NULLABLE",
                "Observed bounded canary result.",
                (),
            ),
            ("passed", "BOOL", "REQUIRED", "Whether actual matched expected.", ()),
            ("started_at", "TIMESTAMP", "REQUIRED", "Canary start time in UTC.", ()),
            (
                "finished_at",
                "TIMESTAMP",
                "NULLABLE",
                "Canary finish time in UTC.",
                (),
            ),
            ("error_code", "STRING", "NULLABLE", "Bounded error code when failed.", ()),
        ),
        "partition": "DATE(started_at)",
        "cluster": ("canary_id", "passed"),
        "expiration": 90,
        "description": "One isolated QA result for one approved canary case and canary run.",
    },
    "gdelt_events_wave1_v1.sql": {
        "fields": (
            ("GLOBALEVENTID", "INT64", "REQUIRED", "Canonical GDELT event identifier.", ()),
            ("event_date", "DATE", "REQUIRED", "GDELT event date.", ()),
            ("actor1_country_code", "STRING", "NULLABLE", "Nullable first actor country code.", ()),
            (
                "actor2_country_code",
                "STRING",
                "NULLABLE",
                "Nullable second actor country code.",
                (),
            ),
            ("event_code", "STRING", "NULLABLE", "Nullable CAMEO event code.", ()),
            ("event_root_code", "STRING", "NULLABLE", "Nullable CAMEO root event code.", ()),
            ("goldstein_scale", "FLOAT64", "NULLABLE", "Nullable Goldstein scale.", ()),
            ("mention_count", "INT64", "REQUIRED", "Observed mention count.", ()),
            ("source_count", "INT64", "REQUIRED", "Observed source count.", ()),
            ("article_count", "INT64", "REQUIRED", "Observed article count.", ()),
            ("average_tone", "FLOAT64", "NULLABLE", "Nullable GDELT average tone.", ()),
            ("action_geo_name", "STRING", "NULLABLE", "Nullable action geography label.", ()),
            (
                "action_geo_latitude",
                "FLOAT64",
                "NULLABLE",
                "Nullable action geography latitude.",
                (),
            ),
            (
                "action_geo_longitude",
                "FLOAT64",
                "NULLABLE",
                "Nullable action geography longitude.",
                (),
            ),
            ("source_url", "STRING", "NULLABLE", "Nullable source article URL.", ()),
            ("date_added", "TIMESTAMP", "REQUIRED", "GDELT ingestion timestamp.", ()),
        ),
        "partition": "event_date",
        "cluster": ("event_root_code",),
        "expiration": None,
        "description": "Wave 1 GDELT event facts at one row per GLOBALEVENTID.",
    },
    "gdelt_event_market_wave1_v1.sql": {
        "fields": (
            ("GLOBALEVENTID", "INT64", "REQUIRED", "Parent GDELT event identifier.", ()),
            ("market", "STRING", "REQUIRED", "Retained lower-case market code.", ()),
            ("evidence_role", "STRING", "REQUIRED", "Named market evidence role.", ()),
            (
                "receipt_id",
                "STRING",
                "REQUIRED",
                "Immutable market evidence receipt identifier.",
                (),
            ),
        ),
        "partition": "_PARTITIONDATE",
        "cluster": ("market", "evidence_role"),
        "expiration": None,
        "description": "Wave 1 retained market evidence at GLOBALEVENTID and market grain.",
    },
    "gdelt_gcam_wave1_v1.sql": {
        "fields": (
            ("document_url", "STRING", "REQUIRED", "Canonical GKG document URL.", ()),
            ("published_at", "TIMESTAMP", "REQUIRED", "GKG document publication timestamp.", ()),
            ("v10_1", "FLOAT64", "NULLABLE", "Nullable GCAM v10.1 value.", ()),
            ("v10_2", "FLOAT64", "NULLABLE", "Nullable GCAM v10.2 value.", ()),
            ("v19_1", "FLOAT64", "NULLABLE", "Nullable GCAM v19.1 value.", ()),
            ("v19_9", "FLOAT64", "NULLABLE", "Nullable GCAM v19.9 value.", ()),
            ("v20_1", "FLOAT64", "NULLABLE", "Nullable GCAM v20.1 value.", ()),
        ),
        "partition": "DATE(published_at)",
        "cluster": ("document_url",),
        "expiration": None,
        "description": "Wave 1 GDELT GCAM dimensions at document URL and published timestamp grain.",
    },
    "open_intelligence_run_receipts_v1.sql": {
        "fields": (
            (
                "run_contract_version",
                "STRING",
                "REQUIRED",
                "Run receipt contract used by the writer.",
                (),
            ),
            (
                "run_id",
                "STRING",
                "REQUIRED",
                "Engine issued immutable run identity.",
                (),
            ),
            ("client_scope_id", "STRING", "REQUIRED", "Resolved client scope.", ()),
            ("market_scope", "STRING", "REPEATED", "Markets resolved for the run.", ()),
            (
                "signal_date",
                "DATE",
                "REQUIRED",
                "Candidate snapshot date. Equals observation_end.",
                (),
            ),
            (
                "observation_start",
                "DATE",
                "REQUIRED",
                "Inclusive closed source window start.",
                (),
            ),
            (
                "observation_end",
                "DATE",
                "REQUIRED",
                "Inclusive closed source window end.",
                (),
            ),
            (
                "observation_method",
                "STRING",
                "REQUIRED",
                "Exact engine observation method identifier.",
                (),
            ),
            (
                "source_window_digest",
                "STRING",
                "REQUIRED",
                "Digest of the exact closed source window.",
                (),
            ),
            (
                "cluster_build_version",
                "STRING",
                "REQUIRED",
                "Identity build version used by the run.",
                (),
            ),
            (
                "source_family_map_version",
                "STRING",
                "REQUIRED",
                "Channel family mapping version used for attribution.",
                (),
            ),
            (
                "rule_version",
                "STRING",
                "REQUIRED",
                "Deterministic rule version used by the run.",
                (),
            ),
            ("status", "STRING", "REQUIRED", "Exactly completed or failed.", ()),
            (
                "complete_partitions",
                "BOOL",
                "REQUIRED",
                "True only when every required partition was written and read back.",
                (),
            ),
            (
                "display_release_state",
                "STRING",
                "REQUIRED",
                "Exactly blocked or enabled. Blocked by default.",
                (),
            ),
            (
                "candidate_count",
                "INT64",
                "REQUIRED",
                "Candidate rows written by the run.",
                (),
            ),
            (
                "evidence_count",
                "INT64",
                "REQUIRED",
                "Evidence rows written by the run.",
                (),
            ),
            (
                "membership_count",
                "INT64",
                "REQUIRED",
                "Membership rows written by the run.",
                (),
            ),
            (
                "lineage_count",
                "INT64",
                "REQUIRED",
                "Lineage rows written by the run.",
                (),
            ),
            (
                "analysis_count",
                "INT64",
                "REQUIRED",
                "Analysis rows written by the run.",
                (),
            ),
            (
                "prediction_count",
                "INT64",
                "REQUIRED",
                "Prediction rows written by the run.",
                (),
            ),
            (
                "row_set_digest",
                "STRING",
                "REQUIRED",
                "Digest of the natural keys of every row family written by the run.",
                (),
            ),
            (
                "source_sha",
                "STRING",
                "REQUIRED",
                "Full forty character lower case commit SHA of the writing source.",
                (),
            ),
            (
                "completed_at",
                "TIMESTAMP",
                "REQUIRED",
                "Engine completion time.",
                (),
            ),
        ),
        "partition": "signal_date",
        "cluster": ("client_scope_id", "status", "display_release_state"),
        "expiration": None,
        "description": (
            "One immutable completion marker for one exact engine run, written last "
            "after readback of every row family. Natural key: run_id."
        ),
    },
}

_CONTRACTS.update(
    {
        "collection_exposure_receipts_v1.sql": {
            "fields": (
                (
                    "exposure_contract_version",
                    "STRING",
                    "REQUIRED",
                    "Exactly collection_exposure_receipt_v1.",
                    (),
                ),
                ("source_family", "STRING", "REQUIRED", "Qualifying source family.", ()),
                ("exposure_date", "DATE", "REQUIRED", "Closed exposure date.", ()),
                (
                    "collection_policy_digest",
                    "STRING",
                    "REQUIRED",
                    "Canonical collection policy digest.",
                    (),
                ),
                ("source_sha", "STRING", "REQUIRED", "Immutable issuer source SHA.", ()),
                ("image_digest", "STRING", "REQUIRED", "Immutable issuer image digest.", ()),
                (
                    "config_digest",
                    "STRING",
                    "REQUIRED",
                    "Canonical collection configuration digest.",
                    (),
                ),
                (
                    "quota_authority_id",
                    "STRING",
                    "REQUIRED",
                    "Digest of the approved vendor quota receipt.",
                    (),
                ),
                ("quota_applicability", "STRING", "REQUIRED", "Exactly metered or unmetered.", ()),
                ("quota_unit", "STRING", "NULLABLE", "Vendor native quota unit when metered.", ()),
                ("quota_limit", "INT64", "NULLABLE", "Vendor quota limit when metered.", ()),
                ("quota_used", "INT64", "NULLABLE", "Vendor quota used when metered.", ()),
                (
                    "quota_exhausted",
                    "BOOL",
                    "REQUIRED",
                    "Whether the approved quota was exhausted.",
                    (),
                ),
                (
                    "capture_complete",
                    "BOOL",
                    "REQUIRED",
                    "Whether source capture was complete.",
                    (),
                ),
                (
                    "source_copy_receipt_refs",
                    "RECORD",
                    "REPEATED",
                    "Sorted exact source-copy receipt references.",
                    (
                        ("copy_run_id", "STRING", "REQUIRED", None, ()),
                        ("source_table", "STRING", "REQUIRED", None, ()),
                        ("source_set_digest", "STRING", "REQUIRED", None, ()),
                    ),
                ),
                ("issued_at", "TIMESTAMP", "REQUIRED", "Issuer completion time in UTC.", ()),
                ("issuer_identity", "STRING", "REQUIRED", "Exact staging service identity.", ()),
                (
                    "receipt_digest",
                    "STRING",
                    "REQUIRED",
                    "Canonical receipt digest excluding issuance metadata.",
                    (),
                ),
            ),
            "partition": "exposure_date",
            "cluster": ("source_family",),
            "expiration": None,
            "description": (
                "Append-only collection exposure authority. Natural key: "
                "(source_family, exposure_date)."
            ),
        },
        "open_intelligence_quality_release_records_v2.sql": {
            "fields": (
                ("run_id", "STRING", "REQUIRED", "Exact immutable released run identity.", ()),
                (
                    "run_receipt_digest",
                    "STRING",
                    "REQUIRED",
                    "Canonical post-release run receipt digest.",
                    (),
                ),
                ("source_window_digest", "STRING", "REQUIRED", "Exact source window digest.", ()),
                (
                    "candidate_projection_digest",
                    "STRING",
                    "REQUIRED",
                    "Canonical quality projection digest.",
                    (),
                ),
                (
                    "packet_digest",
                    "STRING",
                    "REQUIRED",
                    "Canonical complete review packet digest.",
                    (),
                ),
                (
                    "review_receipt_digest",
                    "STRING",
                    "REQUIRED",
                    "Approved complete human review receipt digest.",
                    (),
                ),
                (
                    "approval_addendum_sha256",
                    "STRING",
                    "REQUIRED",
                    "Approved r3-release-addendum-v1 SHA256.",
                    (),
                ),
                ("released_at", "TIMESTAMP", "REQUIRED", "Release transaction time in UTC.", ()),
                (
                    "release_contract_version",
                    "STRING",
                    "REQUIRED",
                    "Exactly open_intelligence_quality_release_v2.",
                    (),
                ),
            ),
            "partition": "DATE(released_at)",
            "cluster": ("run_id",),
            "expiration": None,
            "description": "Append-only quality-owned display authority. One immutable row per run.",
        },
    }
)


@pytest.mark.parametrize("filename", tuple(_CONTRACTS))
def test_open_intelligence_ddl_matches_accepted_contract(filename: str):
    path = _SCHEMAS_DIR / filename
    assert path.is_file(), f"missing accepted schema: {filename}"
    dataset = "trends_v2_staging_qa" if filename == "canary_results_v2.sql" else "trends_v2_staging"
    sql = setup.render_schema_sql(filename, "ogilvy-trends-v2", dataset)
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "`ogilvy-trends-v2." in sql
    assert parse_table_ddl(sql) == _CONTRACTS[filename]


@pytest.mark.parametrize(
    ("filename", "required_token"),
    [
        ("signal_predictions_v2.sql", "velocity FLOAT64 NOT NULL"),
        ("source_performance_daily_v2.sql", "meter STRING NOT NULL"),
    ],
)
def test_nested_required_mode_parity_fails_without_not_null_token(
    filename: str, required_token: str
):
    sql = setup.render_schema_sql(filename, "ogilvy-trends-v2", "trends_v2_staging")
    assert required_token in sql
    mutated = sql.replace(required_token, required_token.removesuffix(" NOT NULL"), 1)
    with pytest.raises(AssertionError):
        assert parse_table_ddl(mutated) == _CONTRACTS[filename]


def test_parser_rejects_unrendered_identifiers():
    sql = (_SCHEMAS_DIR / "signal_candidates_v2.sql").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="fully rendered"):
        parse_table_ddl(sql)


def test_parser_rejects_unknown_table_options():
    sql = setup.render_schema_sql(
        "signal_candidates_v2.sql",
        "ogilvy-trends-v2",
        "trends_v2_staging",
    )
    mutated = sql.replace(
        "OPTIONS (\n  description",
        "OPTIONS (\n  friendly_name = 'unapproved',\n  description",
        1,
    )
    with pytest.raises(ValueError, match="table OPTIONS"):
        parse_table_ddl(mutated)


def test_parser_rejects_non_idempotent_create_table_grammar():
    sql = setup.render_schema_sql(
        "signal_candidates_v2.sql",
        "ogilvy-trends-v2",
        "trends_v2_staging",
    )
    mutated = sql.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)
    with pytest.raises(ValueError, match="CREATE TABLE IF NOT EXISTS"):
        parse_table_ddl(mutated)


def test_reserved_rows_field_is_quoted_and_normalized_to_contract_name():
    sql = (_SCHEMAS_DIR / "source_performance_daily_v2.sql").read_text(encoding="utf-8")
    assert "`rows` INT64" in sql
    rendered = setup.render_schema_sql(
        "source_performance_daily_v2.sql",
        "ogilvy-trends-v2",
        "trends_v2_staging",
    )
    parsed_names = [field[0] for field in parse_table_ddl(rendered)["fields"]]
    assert "rows" in parsed_names
    assert "`rows`" not in parsed_names


_QA_ONLY_SCHEMAS = frozenset({"canary_results_v2.sql"})
_STAGING_ONLY_SCHEMAS = frozenset(
    {
        "open_intelligence_run_receipts_v1.sql",
        "gdelt_events_wave1_v1.sql",
        "gdelt_event_market_wave1_v1.sql",
        "gdelt_gcam_wave1_v1.sql",
    }
)


@pytest.mark.parametrize(
    "filename",
    tuple(name for name in _CONTRACTS if name not in _QA_ONLY_SCHEMAS),
)
def test_fully_rendered_main_staging_sql_has_approved_partition_retention(
    filename: str,
):
    rendered = setup.render_schema_sql(filename, "ogilvy-trends-v2", "trends_v2_staging")
    assert "{project}" not in rendered
    assert "{dataset}" not in rendered
    assert f"`ogilvy-trends-v2.trends_v2_staging.{filename[:-4]}`" in rendered
    parsed = parse_table_ddl(rendered)
    assert parsed == _CONTRACTS[filename]


@pytest.mark.parametrize(
    "filename",
    tuple(
        name
        for name in _CONTRACTS
        if name not in _QA_ONLY_SCHEMAS and name not in _STAGING_ONLY_SCHEMAS
    ),
)
def test_fully_rendered_qa_sql_has_approved_partition_retention(filename: str):
    rendered = setup.render_schema_sql(filename, "ogilvy-trends-v2", "trends_v2_staging_qa")
    assert "{project}" not in rendered
    assert "{dataset}" not in rendered
    assert f"`ogilvy-trends-v2.trends_v2_staging_qa.{filename[:-4]}`" in rendered
    parsed = parse_table_ddl(rendered)
    expected = {**_CONTRACTS[filename], "expiration": 90}
    assert parsed == expected


def test_fully_rendered_canary_results_has_approved_partition_retention():
    rendered = setup.render_schema_sql(
        "canary_results_v2.sql", "ogilvy-trends-v2", "trends_v2_staging_qa"
    )
    parsed = parse_table_ddl(rendered)
    assert parsed == _CONTRACTS["canary_results_v2.sql"]


def test_production_schema_rendering_is_placeholder_replacement_only():
    filename = "raw_content.sql"
    raw = (_SCHEMAS_DIR / filename).read_text(encoding="utf-8")
    expected = raw.replace("{project}", "ogilvy-trends-v2").replace("{dataset}", "trends_v2_dev")
    assert setup.render_schema_sql(filename, "ogilvy-trends-v2", "trends_v2_dev") == expected


def test_wave1_authority_fields_are_exact_nullable_described_schema_tails():
    expected = (
        ("endpoint", "exact retained vendor route identifier"),
        ("vendor_family", "independent vendor collection authority"),
        ("channel_family", "evidence channel authority"),
        ("source_family", "compatibility alias equal to channel_family for Wave 1 rows"),
        ("geo_method_id", "retained row-level geo method identifier"),
        ("geo_receipt_id", "retained row-level geo authority receipt identifier"),
        ("native_id", "platform-native content identity used before URL deduplication"),
        ("source_family_map_version", "exact source identity map version"),
    )
    for filename in ("raw_content.sql", "enriched_content.sql"):
        sql = setup.render_schema_sql(filename, "ogilvy-trends-v2", "trends_v2_staging")
        fields = tuple(
            re.findall(
                r"^  ([a-z_]+) STRING OPTIONS\(description = '([^']+)'\)(?:,)?$",
                sql,
                re.M,
            )
        )
        assert fields[-8:] == expected
        assert all(
            re.search(rf"^  {name} STRING OPTIONS", sql, re.M)
            and re.search(rf"^  {name} STRING NOT NULL", sql, re.M) is None
            and re.search(rf"^  {name} STRING DEFAULT", sql, re.M) is None
            for name, _description in expected
        )
