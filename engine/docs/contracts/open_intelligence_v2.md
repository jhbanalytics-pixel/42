# Phase 0 Open Intelligence contract draft

Status: approved 2026-08-25. A1 through A8 accepted.

Plan source: `42-open-intelligence-plan-review-resolution-2026-08-25.md`.

Design source: `DESIGN-IS-2026-08-25-R2/04-handoff-prompt.md`.

Contract approval: 2026-08-25. Approved SHA256: `2fbd14b84b2da8a57334c9c708e7eddd6e83602c63ad6136ecc37468055798df`.

## 1. Scope envelope

The following seven fields are required by the approved plan on every v2 request, persisted signal, evidence plan, answer, and artifact:

| Field | JSON type | BigQuery type and mode | Rule |
|---|---|---|---|
| `client_scope_id` | string | STRING REQUIRED | Resolved client scope. No code default. |
| `market_scope` | array of strings | STRING REPEATED | One or more lower case market codes. |
| `brand_config_id` | string | STRING REQUIRED | Resolved brand configuration identifier. |
| `audience_lens_ids` | array of strings | STRING REPEATED | Empty is valid. Each value resolves through configuration. |
| `theme_id` | string | STRING REQUIRED | Resolved presentation theme identifier. |
| `run_id` | string | STRING REQUIRED | One immutable identifier for one logical run or request. Retries reuse it. |
| `contract_version` | string | STRING REQUIRED | Exact contract version carried by the producer. |

The field names are plan exact. Their types, required modes, lower case market convention, retry rule, version rule, and validation behavior are `NEEDS_ALBERT_APPROVAL` under A1.

Recommended invariant: after configuration resolution, none of the five scope fields may be null. `market_scope` must contain at least one unique value. `audience_lens_ids` may be empty. A row level `market`, when present, must be a member of `market_scope`. IDs use lower case snake case except opaque `run_id`, `signal_id`, and other hash identifiers.

Example resolved envelope:

```json
{
  "client_scope_id": "ogilvy_default",
  "market_scope": ["za", "ng", "ke"],
  "brand_config_id": "ogilvy_default",
  "audience_lens_ids": ["gen_z_inferred"],
  "theme_id": "ogilvy_42",
  "run_id": "run_20260825T063000Z_01",
  "contract_version": "2.0.0"
}
```

## 2. BigQuery namespace and table contracts

### 2.1 Namespace

Recommended staging data namespace, A3 `NEEDS_ALBERT_APPROVAL`:

| Purpose | Project | Dataset | Allowed consumers |
|---|---|---|---|
| Open Intelligence staging | `ogilvy-trends-v2` | `trends_v2_staging` | Staging engine writers, approved staging views, `listening-post-staging` readers |
| Canary QA | `ogilvy-trends-v2` | `trends_v2_staging_qa` | Canary job and QA assertions only |
| Production | Existing project and dataset | Existing names unchanged | Existing production readers only |

No Phase 0 query, view, migration, service, cache, or fixture may write to or replace a production object.

Deterministic identifier rule, A2 `NEEDS_ALBERT_APPROVAL`: null is encoded as `N:`. Every nonnull value, including an empty string, is UTF8 encoded as `V<decimal byte length>:<value>`. Encoded inputs are joined with one `|` byte in the exact field order stated by the owning table, then hashed with SHA256. The stored identifier is its table prefix plus all 64 lower case hexadecimal digest characters. Truncated digests are forbidden. Null and an empty string therefore have different canonical bytes.

### 2.2 `signal_candidates_v2`

Grain recommendation: one daily snapshot of one stable signal identity per market and run. Natural key: `(client_scope_id, signal_date, market, signal_id, run_id)`. A retry uses the same `run_id` and replaces no prior logical run. Key and identity rules are A2 `NEEDS_ALBERT_APPROVAL`.

`cluster_signature` is the full SHA256 digest of the retained member set. One canonical member is the ordered tuple `(candidate_type, canonical_value, source_family, platform, row_id)`, with each value encoded by the null safe rule above. Duplicate member byte strings are removed, the remaining member byte strings are sorted lexicographically, and the ordered list is length prefixed again before hashing. Initial `signal_id` uses ordered inputs `(client_scope_id, market, cluster_signature)`. A continuing lineage copies the prior `signal_id`; it never rehashes the new daily signature into a new identity. A merge or split assigns each new identity from the same ordered initial signal inputs using the resulting cluster signature and records every prior ID in `signal_lineage_v2`.

Partitioning and clustering: `PARTITION BY signal_date CLUSTER BY market, evidence_state`. This is exact plan behavior.

| Field | Type | Mode | Description |
|---|---|---|---|
| `client_scope_id` | STRING | REQUIRED | Resolved client scope. |
| `market_scope` | STRING | REPEATED | Markets resolved for the run. |
| `brand_config_id` | STRING | REQUIRED | Resolved brand configuration. |
| `audience_lens_ids` | STRING | REPEATED | Resolved audience lenses, possibly empty. |
| `theme_id` | STRING | REQUIRED | Resolved theme. |
| `run_id` | STRING | REQUIRED | Logical build run. |
| `contract_version` | STRING | REQUIRED | Contract used by the writer. |
| `signal_id` | STRING | REQUIRED | Stable signal identity. |
| `signal_date` | DATE | REQUIRED | Daily snapshot date and partition key. |
| `market` | STRING | REQUIRED | Lower case market code in `market_scope`. |
| `label` | STRING | REQUIRED | Most central observed phrase. Gemini does not choose membership. |
| `cluster_signature` | STRING | REQUIRED | Deterministic digest of canonical membership inputs. |
| `cluster_build_version` | STRING | REQUIRED | Deterministic clustering rule version. |
| `model_version` | STRING | NULLABLE | Model used only for a promoted summary. Null when no model summary ran. A2. |
| `discovery_mode` | STRING | REQUIRED | Recommended enum `dynamic`, `replay`, or `canary`. `canary` is valid only in QA. A2. |
| `topic_tags` | STRING | REPEATED | Optional legacy taxonomy relationships. Tags never decide identity. |
| `novelty_score` | FLOAT64 | REQUIRED | Bounded 0 to 1. A2. |
| `velocity_score` | FLOAT64 | REQUIRED | Bounded 0 to 1. A2. |
| `breadth_score` | FLOAT64 | REQUIRED | Bounded 0 to 1. A2. |
| `independence_score` | FLOAT64 | REQUIRED | Bounded 0 to 1. A2. |
| `historical_similarity` | FLOAT64 | NULLABLE | Bounded 0 to 1. Null when no valid historical comparison exists. A2. |
| `geo_confidence` | FLOAT64 | REQUIRED | Bounded 0 to 1. |
| `evidence_state` | STRING | REQUIRED | Exact plan enum `ready`, `thin`, `contradictory`, or `unchecked`. |
| `created_at` | TIMESTAMP | REQUIRED | Persist time in UTC. |

Family direction counts (approved by Albert on 2026-09-03). The direction test behind `ready` counts every row the window's term match cites for the component, by source family and by published date: current is the signal date and the two days before it, baseline is the window start through the day before that. The sampled evidence receipts remain the displayed evidence and are not the count. A family the match never cites counts zero and zero and stays `not_applicable`. The test itself is unchanged: at least five rows per family, two sided at 0.05 after adjustment, and readiness still needs two families with named, agreeing directions.

Example row:

```json
{
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "run_id": "run_fixture_001",
  "contract_version": "2.0.0",
  "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "signal_date": "2026-08-25",
  "market": "za",
  "label": "Weekend repair culture",
  "cluster_signature": "sha256:11ef7f41c83d9a20b31c42d53e64f750861972a83b94c05d16e27f38a49b5c60",
  "cluster_build_version": "hybrid_graph_v1",
  "model_version": null,
  "discovery_mode": "dynamic",
  "topic_tags": ["economy"],
  "novelty_score": 0.74,
  "velocity_score": 0.68,
  "breadth_score": 0.62,
  "independence_score": 0.71,
  "historical_similarity": 0.33,
  "geo_confidence": 0.94,
  "evidence_state": "ready",
  "created_at": "2026-08-25T06:45:00Z"
}
```

### 2.3 `signal_evidence_v2`

Grain recommendation: one observed evidence row attached to one signal snapshot. Natural key: `(client_scope_id, signal_date, market, signal_id, evidence_id, run_id)`. `evidence_id` is `ev_` followed by the full 64 lower case hexadecimal SHA256 digest of the length prefixed canonical serialization of `client_scope_id`, `market`, `source_family`, `platform`, `row_id`, and normalized URL. The deterministic key is A2 `NEEDS_ALBERT_APPROVAL`.

Partitioning recommendation: `PARTITION BY signal_date CLUSTER BY market, signal_id, source_family, evidence_state`. `signal_date`, `market`, display retention fields, `evidence_state`, and `created_at` are recommended additions needed for bounded reads and QA. A2 `NEEDS_ALBERT_APPROVAL`.

| Field | Type | Mode | Description |
|---|---|---|---|
| `client_scope_id` | STRING | REQUIRED | Resolved client scope. |
| `market_scope` | STRING | REPEATED | Markets resolved for the run. |
| `brand_config_id` | STRING | REQUIRED | Resolved brand configuration. |
| `audience_lens_ids` | STRING | REPEATED | Resolved audience lenses, possibly empty. |
| `theme_id` | STRING | REQUIRED | Resolved theme. |
| `run_id` | STRING | REQUIRED | Evidence extraction run. |
| `contract_version` | STRING | REQUIRED | Contract used by the writer. |
| `signal_date` | DATE | REQUIRED | Signal snapshot date. A2. |
| `market` | STRING | REQUIRED | Lower case market code. A2. |
| `signal_id` | STRING | REQUIRED | Parent signal identity. |
| `evidence_id` | STRING | REQUIRED | Stable evidence identifier. |
| `row_id` | STRING | REQUIRED | Source row identifier. |
| `source_family` | STRING | REQUIRED | Independent channel family from the current engine family map. |
| `platform` | STRING | REQUIRED | Normalized observed platform. |
| `url` | STRING | NULLABLE | Direct evidence URL. Null when the source provides none. |
| `published_at` | TIMESTAMP | NULLABLE | Source publish time. Null cannot qualify as current evidence. A2. |
| `claim_role` | STRING | REQUIRED | Recommended enum `identity`, `direction`, `context`, `contradiction`, or `geo`. A2. |
| `direction` | STRING | REQUIRED | Recommended enum `rising`, `stable`, `declining`, `conflicting`, or `not_applicable`. A2. |
| `geo_confidence` | FLOAT64 | REQUIRED | Bounded 0 to 1. |
| `source_label` | STRING | NULLABLE | Human readable source or host retained for evidence display. A2. |
| `author_label` | STRING | NULLABLE | Public author label when policy permits retention. A2. |
| `excerpt` | STRING | NULLABLE | Sanitized excerpt. Null when retention or policy prevents display. A2. |
| `metric_label` | STRING | NULLABLE | Observed metric with unit. Never inferred. A2. |
| `availability` | STRING | REQUIRED | Recommended enum `available`, `aged_out`, or `unavailable`. A2. |
| `evidence_state` | STRING | REQUIRED | State assigned to the parent evaluation. A2. |
| `created_at` | TIMESTAMP | REQUIRED | Persist time in UTC. A2. |

Example row:

```json
{
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "run_id": "run_fixture_001",
  "contract_version": "2.0.0",
  "signal_date": "2026-08-25",
  "market": "za",
  "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "evidence_id": "ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01",
  "row_id": "fixture_row_001",
  "source_family": "reddit",
  "platform": "reddit",
  "url": "https://example.invalid/posts/fixture-001",
  "published_at": "2026-08-24T19:20:00Z",
  "claim_role": "direction",
  "direction": "rising",
  "geo_confidence": 0.96,
  "source_label": "Fixture community",
  "author_label": "@fixture_maker",
  "excerpt": "A sanitized fixture excerpt showing the observed repair routine.",
  "metric_label": "128 interactions",
  "availability": "available",
  "evidence_state": "ready",
  "created_at": "2026-08-25T06:45:00Z"
}
```

Display retention rule, A2: `source_label`, `author_label`, `excerpt`, and `metric_label` may contain only sanitized public source material allowed by the source retention policy. When the backing content ages out, the retention job sets `availability = aged_out` and nulls URL, author, excerpt, and metric label while retaining IDs, source family, platform, timestamps, role, direction, geo confidence, and state for audit. `unavailable` uses the same nulling rule when policy or source failure prevents display. No display field is reconstructed from a model.

#### 2.3.1 Source identity pairs

Every projected evidence row resolves to one vendor family and one channel family before it is scored. Independence between two rows requires both a different vendor and a different channel, so one vendor can never manufacture independent family agreement by spanning channels. The resolver accepts exactly these pairs; any other observed shape refuses the run rather than being dropped or guessed.

| Vendor family | Channel family | Observed shape that resolves to it |
|---|---|---|
| `rss` | `news` | source `rss`, or platform `web` or `news` with article content |
| `gdelt` | `news` | source beginning `gdelt`, or content type `gdelt_gkg` under any publisher domain |
| `google_youtube` | `youtube` | platform `youtube` outside SocialCrawl |
| `socialcrawl` | `short_video` | SocialCrawl TikTok and Instagram routes |
| `socialcrawl` | `youtube` | SocialCrawl YouTube routes |
| `socialcrawl` | `reddit` | SocialCrawl Reddit routes, and any Reddit platform row |
| `socialcrawl` | `twitter` | SocialCrawl Twitter routes |
| `socialcrawl` | `threads` | SocialCrawl Threads routes |
| `socialcrawl` | `news` | SocialCrawl news routes |
| `google_trends` | `search` | sources `google trends`, `bigquery_trends` and `google_trends_rss` on platform `google_search` |
| `apple_music` | `music` | source `apple_music` chart rows |
| `brand24` | `brand24` | every retained Brand24 row, whatever its platform |

Retained Brand24 evidence is legacy: it resolves to the single `brand24` family on both axes, that family is excluded from the active family set used by readiness and predictions, and it can never supply an independent channel. The qualifying channel family set is `apps`, `bluesky`, `brand24`, `ensemble`, `music`, `news`, `reddit`, `search`, `short_video`, `threads`, `twitter`, `web_attention`, `wikipedia` and `youtube`; the attribution map version stays `channel_family_v2` because this table only adds shapes and changes no existing row's attribution.

### 2.4 `signal_lineage_v2`

Grain recommendation: one directed lineage edge created by one run. Natural key: `(client_scope_id, signal_date, market, from_signal_id, to_signal_id, relation, run_id)`. `overlap_score` is 0 to 1 and measures member overlap at the lineage decision boundary. A2 `NEEDS_ALBERT_APPROVAL`.

Partitioning recommendation: `PARTITION BY signal_date CLUSTER BY market, relation, from_signal_id, to_signal_id`. A2 `NEEDS_ALBERT_APPROVAL`.

| Field | Type | Mode | Description |
|---|---|---|---|
| `client_scope_id` | STRING | REQUIRED | Resolved client scope. |
| `market_scope` | STRING | REPEATED | Markets resolved for the run. |
| `brand_config_id` | STRING | REQUIRED | Resolved brand configuration. |
| `audience_lens_ids` | STRING | REPEATED | Resolved audience lenses, possibly empty. |
| `theme_id` | STRING | REQUIRED | Resolved theme. |
| `run_id` | STRING | REQUIRED | Lineage build run. |
| `contract_version` | STRING | REQUIRED | Contract used by the writer. |
| `signal_date` | DATE | REQUIRED | Date on which the relation was observed. A2. |
| `market` | STRING | REQUIRED | Lower case market code. A2. |
| `from_signal_id` | STRING | REQUIRED | Earlier or contributing identity. |
| `to_signal_id` | STRING | REQUIRED | Continuing or resulting identity. |
| `relation` | STRING | REQUIRED | Exact plan enum `continues`, `merges_into`, or `splits_into`. |
| `overlap_score` | FLOAT64 | REQUIRED | Bounded 0 to 1. |
| `created_at` | TIMESTAMP | REQUIRED | Persist time in UTC. |

Example row:

```json
{
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "run_id": "run_fixture_002",
  "contract_version": "2.0.0",
  "signal_date": "2026-08-26",
  "market": "za",
  "from_signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "to_signal_id": "sig_f5b3f9bf3f4b49c2a33b4c5d6e7f8091a2b3c4d5e6f7081923456789abcdef02",
  "relation": "continues",
  "overlap_score": 0.82,
  "created_at": "2026-08-26T06:44:00Z"
}
```

### 2.5 `signal_analysis_v2`

The table name is exact plan scope. The plan does not approve its fields. The following complete shape is A2 `NEEDS_ALBERT_APPROVAL`.

Grain recommendation: one evidence bounded analysis for one signal snapshot and run. Natural key: `(client_scope_id, signal_date, market, signal_id, analysis_id)`. `analysis_id` uses ordered inputs `(client_scope_id, signal_date, market, signal_id, run_id)`. Partition by `signal_date`. Cluster by `market, evidence_state, human_review_required, signal_id`.

| Field | Type | Mode | Description |
|---|---|---|---|
| `client_scope_id` | STRING | REQUIRED | Resolved client scope. |
| `market_scope` | STRING | REPEATED | Markets resolved for the run. |
| `brand_config_id` | STRING | REQUIRED | Resolved brand configuration. |
| `audience_lens_ids` | STRING | REPEATED | Resolved audience lenses, possibly empty. |
| `theme_id` | STRING | REQUIRED | Resolved theme. |
| `run_id` | STRING | REQUIRED | Analysis run. |
| `contract_version` | STRING | REQUIRED | Contract used by the writer. |
| `analysis_id` | STRING | REQUIRED | Deterministic analysis identifier. |
| `signal_id` | STRING | REQUIRED | Parent signal. |
| `signal_date` | DATE | REQUIRED | Signal snapshot date. |
| `market` | STRING | REQUIRED | Lower case market code. |
| `evidence_state` | STRING | REQUIRED | `ready`, `thin`, `contradictory`, or `unchecked`. |
| `summary` | STRING | REQUIRED | Plain language signal read. |
| `why_now` | STRING | REQUIRED | Dated explanation limited to supported evidence. |
| `possible_response` | STRING | NULLABLE | One bounded strategist response. Null when evidence cannot support one. |
| `limitations` | STRING | REPEATED | Named missing evidence and scope limits. |
| `contradictions` | STRING | REPEATED | Named disagreements. Empty when none qualify. |
| `evidence_ids` | STRING | REPEATED | Evidence used by the analysis. At least one for a nonempty claim. |
| `human_review_required` | BOOL | REQUIRED | True for election scoped output before export. |
| `model_version` | STRING | NULLABLE | Exact model identifier. Null for deterministic fallback. |
| `analyzed_at` | TIMESTAMP | REQUIRED | Analysis time in UTC. |

Example row:

```json
{
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "run_id": "run_fixture_001",
  "contract_version": "2.0.0",
  "analysis_id": "an_103edd8fc6ec4c06a44b5c6d7e8f9012a3b4c5d6e7f809123456789abcdef034",
  "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "signal_date": "2026-08-25",
  "market": "za",
  "evidence_state": "ready",
  "summary": "Repair tutorials are moving from isolated tips into shared weekend routines.",
  "why_now": "Two independent source families rose against their own trailing baselines this week.",
  "possible_response": "Show practical repair steps and cite the local makers carrying them.",
  "limitations": ["No representative polling or demographic measurement."],
  "contradictions": [],
  "evidence_ids": ["ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01", "ev_80b6b168cc1849c7a55b6c7d8e9f0123a4b5c6d7e8f90123456789abcdef0123"],
  "human_review_required": false,
  "model_version": "gemini-3.5-flash",
  "analyzed_at": "2026-08-25T06:48:00Z"
}
```

### 2.6 `signal_predictions_v2`

The table name and the need for timestamped predictions are plan exact. The following fields, types, enums, and thresholds are A2 `NEEDS_ALBERT_APPROVAL`.

Grain recommendation: one immutable prediction made when a signal is promoted. Natural key: `prediction_id`. `prediction_id` uses ordered inputs `(client_scope_id, signal_date, market, signal_id, run_id, rule_version)`. Partition by `evaluation_date`. Cluster by `market, discovery_mode, expected_trajectory, evidence_state`.

| Field | Type | Mode | Description |
|---|---|---|---|
| `client_scope_id` | STRING | REQUIRED | Resolved client scope. |
| `market_scope` | STRING | REPEATED | Markets resolved for the run. |
| `brand_config_id` | STRING | REQUIRED | Resolved brand configuration. |
| `audience_lens_ids` | STRING | REPEATED | Resolved audience lenses, possibly empty. |
| `theme_id` | STRING | REQUIRED | Resolved theme. |
| `run_id` | STRING | REQUIRED | Prediction run. |
| `contract_version` | STRING | REQUIRED | Contract used by the writer. |
| `prediction_id` | STRING | REQUIRED | Deterministic immutable prediction identifier. |
| `signal_id` | STRING | REQUIRED | Promoted signal. |
| `signal_date` | DATE | REQUIRED | Promotion snapshot date. |
| `market` | STRING | REQUIRED | Lower case market code. |
| `discovery_mode` | STRING | REQUIRED | Same vocabulary as candidate discovery mode. |
| `source_families` | STRING | REPEATED | Qualifying independent source families at prediction time. |
| `evidence_state` | STRING | REQUIRED | Evidence state at prediction time. |
| `first_seen_at` | TIMESTAMP | REQUIRED | First observed qualifying evidence time. |
| `predicted_at` | TIMESTAMP | REQUIRED | Prediction creation time. |
| `expected_trajectory` | STRING | REQUIRED | Recommended enum `peaked`, `sustained`, `growing`, or `fading`. |
| `evaluation_date` | DATE | REQUIRED | Date after which the outcome job may score this row. |
| `baseline` | STRUCT | REQUIRED | Observed baseline at prediction time. Every child mode is listed below. |
| `promotion_target` | STRUCT | REQUIRED | Declared target. Every child mode is listed below. |
| `invalidation_condition` | STRING | REQUIRED | One measurable condition that would invalidate the prediction. |
| `cluster_build_version` | STRING | REQUIRED | Identity build version used at prediction time. |
| `source_family_map_version` | STRING | REQUIRED | Channel family mapping version used for attribution. |
| `rule_version` | STRING | REQUIRED | Deterministic prediction rule version. |
| `display_eligible` | BOOL | REQUIRED | System readiness gate. False until the weekly outcome loop is deployed and proven, then true for new eligible predictions without waiting for their own outcomes. |

Nested prediction fields:

| Parent | Child | Type | Mode | Validation |
|---|---|---|---|---|
| `baseline` | `velocity` | FLOAT64 | REQUIRED | Finite and bounded 0 to 1 |
| `baseline` | `breadth` | FLOAT64 | REQUIRED | Finite and bounded 0 to 1 |
| `baseline` | `evidence_family_count` | INT64 | REQUIRED | Integer at least 0 |
| `promotion_target` | `velocity` | FLOAT64 | REQUIRED | Finite and bounded 0 to 1 |
| `promotion_target` | `breadth` | FLOAT64 | REQUIRED | Finite and bounded 0 to 1 |
| `promotion_target` | `evidence_family_count` | INT64 | REQUIRED | Integer at least 0 |

The writer validates every required child before BigQuery load. A nonnull STRUCT containing a null child fails the contract.

Example row:

```json
{
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "run_id": "run_fixture_001",
  "contract_version": "2.0.0",
  "prediction_id": "pred_f97dbf9802484ad9a66b7c8d9e0f1234a5b6c7d8e9f0123456789abcdef01234",
  "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "signal_date": "2026-08-25",
  "market": "za",
  "discovery_mode": "dynamic",
  "source_families": ["reddit", "youtube"],
  "evidence_state": "ready",
  "first_seen_at": "2026-08-22T11:00:00Z",
  "predicted_at": "2026-08-25T06:50:00Z",
  "expected_trajectory": "sustained",
  "evaluation_date": "2026-09-01",
  "baseline": {"velocity": 0.68, "breadth": 0.62, "evidence_family_count": 2},
  "promotion_target": {"velocity": 0.60, "breadth": 0.60, "evidence_family_count": 2},
  "invalidation_condition": "Breadth falls below 0.35 before the evaluation date.",
  "cluster_build_version": "hybrid_graph_v1",
  "source_family_map_version": "channel_family_v1",
  "rule_version": "prediction_rules_v1",
  "display_eligible": false
}
```

### 2.7 `signal_outcomes_v2`

The table name and outcome vocabulary are plan exact. The remaining shape is A2 `NEEDS_ALBERT_APPROVAL`.

Grain recommendation: exactly one immutable scored result per prediction and evaluation run. Natural key: `(prediction_id, evaluation_date, run_id)`. `outcome_id` uses ordered inputs `(prediction_id, evaluation_date, run_id)`. Partition by `evaluation_date`. Cluster by `market, outcome, discovery_mode`.

| Field | Type | Mode | Description |
|---|---|---|---|
| `client_scope_id` | STRING | REQUIRED | Resolved client scope. |
| `market_scope` | STRING | REPEATED | Markets resolved for the run. |
| `brand_config_id` | STRING | REQUIRED | Resolved brand configuration. |
| `audience_lens_ids` | STRING | REPEATED | Resolved audience lenses, possibly empty. |
| `theme_id` | STRING | REQUIRED | Resolved theme. |
| `run_id` | STRING | REQUIRED | Outcome evaluation run. |
| `contract_version` | STRING | REQUIRED | Contract used by the writer. |
| `outcome_id` | STRING | REQUIRED | Deterministic outcome identifier. |
| `prediction_id` | STRING | REQUIRED | Parent prediction. |
| `signal_id` | STRING | REQUIRED | Evaluated signal. |
| `signal_date` | DATE | REQUIRED | Original promotion date. |
| `market` | STRING | REQUIRED | Lower case market code. |
| `discovery_mode` | STRING | REQUIRED | Discovery mode at prediction time. |
| `source_families` | STRING | REPEATED | Qualifying families copied from the prediction for attribution. No aggregate sentinel. A2. |
| `source_family_map_version` | STRING | REQUIRED | Channel family mapping version used for attribution. |
| `evaluation_date` | DATE | REQUIRED | Contracted evaluation date. |
| `evaluated_at` | TIMESTAMP | REQUIRED | Actual evaluation time. |
| `outcome` | STRING | REQUIRED | Exact plan enum `peaked`, `sustained`, `fizzled`, `noise`, or `unresolved`. |
| `observed_velocity` | FLOAT64 | NULLABLE | Metric at evaluation. |
| `observed_breadth` | FLOAT64 | NULLABLE | Metric at evaluation. |
| `observed_evidence_family_count` | INT64 | NULLABLE | Qualifying families at evaluation. |
| `human_calibration_label` | STRING | NULLABLE | Human sample label when present. No model judgment. |
| `human_reviewed_at` | TIMESTAMP | NULLABLE | Human calibration time. |
| `resolution_reason` | STRING | REQUIRED | Named rule or insufficiency that produced the outcome. |
| `rule_version` | STRING | REQUIRED | Outcome rule version. |

Example row:

```json
{
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "run_id": "run_fixture_outcome_001",
  "contract_version": "2.0.0",
  "outcome_id": "out_0db75ba45f3546cba77b8c9d0e1f2345a6b7c8d9e0f123456789abcdef012345",
  "prediction_id": "pred_f97dbf9802484ad9a66b7c8d9e0f1234a5b6c7d8e9f0123456789abcdef01234",
  "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "signal_date": "2026-08-25",
  "market": "za",
  "discovery_mode": "dynamic",
  "source_families": ["reddit", "youtube"],
  "source_family_map_version": "channel_family_v1",
  "evaluation_date": "2026-09-01",
  "evaluated_at": "2026-09-01T07:00:00Z",
  "outcome": "sustained",
  "observed_velocity": 0.64,
  "observed_breadth": 0.66,
  "observed_evidence_family_count": 3,
  "human_calibration_label": null,
  "human_reviewed_at": null,
  "resolution_reason": "Breadth and evidence remained above the declared floor for the evaluation window.",
  "rule_version": "outcome_rules_v1"
}
```

Global outcome metrics count one base row per prediction. Family metrics unnest `source_families` only inside the query grouped by source family. No writer creates a second family specific row for the same prediction.

### 2.8 `source_performance_daily_v2`

The table name, Source Lab statuses, and status specific metric rules are plan exact. Grain, fields, canonical route identity, and price representation are A2 `NEEDS_ALBERT_APPROVAL`.

Grain recommendation: one daily row per vendor route and observed market. Catalog only routes use `market = null`. Canonical route identity is the upper case HTTP method, one space, and normalized route path. `endpoint_id` is `endpoint_` plus the full SHA256 digest of the length prefixed canonical serialization of HTTP method and route path. `source_performance_id` is `srcperf_` plus the full SHA256 digest using ordered inputs `(client_scope_id, metric_date, vendor, http_method, route_path, market)`. Null market uses the canonical `N:` encoding, never a string sentinel. Natural key: `source_performance_id`. Partition by `metric_date`. Cluster by `vendor, status, source_family, market`.

| Field | Type | Mode | Description |
|---|---|---|---|
| `client_scope_id` | STRING | REQUIRED | Resolved client scope. |
| `market_scope` | STRING | REPEATED | Markets resolved for the run. |
| `brand_config_id` | STRING | REQUIRED | Resolved brand configuration. |
| `audience_lens_ids` | STRING | REPEATED | Resolved audience lenses, possibly empty. |
| `theme_id` | STRING | REQUIRED | Resolved theme. |
| `run_id` | STRING | REQUIRED | Performance snapshot run. |
| `contract_version` | STRING | REQUIRED | Contract used by the writer. |
| `source_performance_id` | STRING | REQUIRED | Deterministic row identifier. |
| `metric_date` | DATE | REQUIRED | Daily metric date. |
| `vendor` | STRING | REQUIRED | Vendor identifier. |
| `http_method` | STRING | REQUIRED | Upper case HTTP method. |
| `route_path` | STRING | REQUIRED | Canonical route path beginning with `/`. |
| `endpoint_id` | STRING | REQUIRED | Digest derived from `http_method` plus `route_path`. |
| `route_role` | STRING | REQUIRED | Exact A2 enum `evidence`, `utility_balance`, `utility_catalog`, `identity_discovery`, `identity_hygiene`, or `inventory`. |
| `source_family` | STRING | REQUIRED | Normalized independent source family. |
| `market` | STRING | NULLABLE | Lower case market when metrics are market specific. |
| `status` | STRING | REQUIRED | Exact plan enum `active`, `pilot`, `available_unwired`, `blocked`, `permanently_rejected`, or `inventory_only`. |
| `calls` | INT64 | NULLABLE | Allowed only for active or pilot. |
| `credits` | NUMERIC | NULLABLE | Allowed only for active or pilot. |
| `rows` | INT64 | NULLABLE | Distinct items the route returned; present for active and permanently_rejected inventory routes. For identity roles, normalized result count after deterministic normalization and deduplication. |
| `integrity` | FLOAT64 | NULLABLE | Bounded 0 to 1. Allowed only for active or pilot. |
| `geo_precision` | FLOAT64 | NULLABLE | Bounded 0 to 1. Allowed only for active or pilot. |
| `unique_lift` | FLOAT64 | NULLABLE | Marginal downstream row count, candidate terms plus evidence rows only this route produced; present for active and permanently_rejected inventory routes. |
| `last_success_at` | TIMESTAMP | NULLABLE | Allowed only for active or pilot. |
| `downstream_consumers` | STRING | REPEATED | Allowed only for active or pilot. |
| `official_capability` | STRING | REQUIRED | Sanitized official capability statement. |
| `official_parameters` | STRING | REPEATED | Sanitized official parameters. |
| `official_price_components` | STRUCT | REPEATED | Exact price components. Required for `available_unwired`; nested modes are listed below. |
| `blocking_reason` | STRING | NULLABLE | Required only for blocked. |
| `kill_test_result` | STRING | NULLABLE | Required for `permanently_rejected`: the kill test reason, equal to `blocking_reason`. |
| `review_date` | DATE | NULLABLE | Null. Reserved for a reviewed rejection, which no writer produces. |
| `last_checked_at` | TIMESTAMP | REQUIRED | Catalog or live drift check time. |
| `balance` | NUMERIC | NULLABLE | Free balance observation on the canonical balance route only. |
| `observed_at` | TIMESTAMP | NULLABLE | Balance observation time on the canonical balance route. |
| `balance_read_status` | STRING | NULLABLE | `ok` or `unknown` on the canonical balance route. |
| `recent_deductions` | NUMERIC | NULLABLE | Measured deductions bridging the prior and current successful balance observations. |
| `deductions_interval` | STRUCT | NULLABLE | Exact measurement interval with nested modes below. |
| `funding_math_status` | STRING | NULLABLE | `complete` or `unknown` on the balance route. |
| `funded_increase_amount` | NUMERIC | NULLABLE | Derived top up amount when funding math is complete. |
| `funded_increase_observed` | BOOL | NULLABLE | Fail closed funding gate. False on unknown, null on nonbalance routes. |
| `selected_top_up_credits` | NUMERIC | NULLABLE | Commercially selected top up amount, not proof of payment or funding. |
| `baseline_credits_per_day` | NUMERIC | NULLABLE | Measured completed day baseline. |
| `baseline_window` | STRUCT | NULLABLE | Measured baseline window with nested modes below. |
| `runway_days` | NUMERIC | NULLABLE | Measured runway. |
| `monthly_optional_credit_cap` | NUMERIC | NULLABLE | Required balance route control, fixed at 25,000 until a new approval. |
| `monthly_optional_credits_used` | NUMERIC | NULLABLE | Measured optional lane credits in the current calendar month. |
| `optional_calls_enabled` | BOOL | NULLABLE | Required on the balance route, null on other routes. |

Nested official price component fields:

| Child | Type | Mode | Rule |
|---|---|---|---|
| `meter` | STRING | REQUIRED | Stable meter name such as request, item, result, or monthly tier |
| `billing_basis` | STRING | REQUIRED | Recommended enum `flat`, `per_item`, `range`, or `tiered` |
| `currency` | STRING | REQUIRED | Currency or credit unit code |
| `unit` | STRING | REQUIRED | Human readable billed unit |
| `quantity` | NUMERIC | REQUIRED | Quantity to which the price applies, greater than 0 |
| `source_url` | STRING | REQUIRED | Official price source URL |
| `checked_at` | TIMESTAMP | REQUIRED | Verification time in UTC |
| `amount` | NUMERIC | NULLABLE | Exact amount for flat or per item pricing |
| `minimum_amount` | NUMERIC | NULLABLE | Inclusive minimum for a range |
| `maximum_amount` | NUMERIC | NULLABLE | Inclusive maximum for a range |
| `tier_condition` | STRING | NULLABLE | Required condition for a tiered component |

Nested balance window fields are `start_date DATE REQUIRED`, `end_date DATE REQUIRED`, and `completed_days INT64 REQUIRED`. Nested deduction interval fields are `start_at TIMESTAMP REQUIRED` and `end_at TIMESTAMP REQUIRED`. A nonnull balance window or deduction interval with any null child fails the contract.

At least one of `amount` or the complete `minimum_amount` and `maximum_amount` pair is required. A tiered component also requires `tier_condition`. `available_unwired` requires at least one price component. If official price cannot be established, the route is `blocked` with a nonempty `blocking_reason`.

Example row:

```json
{
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "run_id": "run_fixture_source_001",
  "contract_version": "2.0.0",
  "source_performance_id": "srcperf_642bd7cfb89e4d83a88b9c0d1e2f3456a7b8c9d0e1f23456789abcdef0123456",
  "metric_date": "2026-08-25",
  "vendor": "fixture_vendor",
  "http_method": "GET",
  "route_path": "/v1/fixture/search",
  "endpoint_id": "endpoint_975bfad07a864e13b98cad1e20f34657a8b9c0d1e2f3456789abcdef01234569",
  "route_role": "evidence",
  "source_family": "other",
  "market": null,
  "status": "available_unwired",
  "calls": null,
  "credits": null,
  "rows": null,
  "integrity": null,
  "geo_precision": null,
  "unique_lift": null,
  "last_success_at": null,
  "downstream_consumers": [],
  "official_capability": "Searches public posts by supplied text.",
  "official_parameters": ["query", "page_cursor"],
  "official_price_components": [{"meter": "request", "billing_basis": "flat", "currency": "credit", "unit": "request", "quantity": 1, "source_url": "https://example.invalid/pricing", "checked_at": "2026-08-25T06:00:00Z", "amount": 1, "minimum_amount": null, "maximum_amount": null, "tier_condition": null}],
  "blocking_reason": null,
  "kill_test_result": null,
  "review_date": null,
  "last_checked_at": "2026-08-25T06:00:00Z",
  "balance": null,
  "observed_at": null,
  "balance_read_status": null,
  "recent_deductions": null,
  "deductions_interval": null,
  "funding_math_status": null,
  "funded_increase_amount": null,
  "funded_increase_observed": null,
  "selected_top_up_credits": null,
  "baseline_credits_per_day": null,
  "baseline_window": null,
  "runway_days": null,
  "monthly_optional_credit_cap": null,
  "monthly_optional_credits_used": null,
  "optional_calls_enabled": null
}
```

Status nullability rule:

| Status | Observed metrics | Official fields | Status detail |
|---|---|---|---|
| `active`, role `evidence` | Required except `unique_lift`, which may be null until a comparison completes | Required | No blocking or kill result |
| `pilot`, role `evidence` | Required except `unique_lift`, which may be null until a comparison completes | Required | Separated from production by status |
| `active`, role `utility_balance` | Calls required, credits required and exactly 0, last success required, downstream consumers required; rows, integrity, geo precision, and unique lift forced null | Capability required; price components may be empty because the route is free | Balance, deduction, funding, baseline, runway, monthly cap, and optional lane rules apply |
| `active`, role `utility_catalog` | Calls required, credits required and exactly 0, last success required, downstream consumers required; evidence metrics forced null | Capability and parameters required | Catalog count and digest are separate typed artifacts |
| `active` or `pilot`, role `identity_discovery` or `identity_hygiene` | Calls, credits, rows, last success, and downstream consumers required; rows equal normalized result count; evidence integrity, geo precision, and unique lift forced null | Capability, parameters, and price components required | Identity result contracts below apply |
| `available_unwired` | All observed metrics null | Capability, parameters, and at least one price component required | No fabricated metrics |
| `blocked` | All observed metrics null | Required | `blocking_reason` required |
| `permanently_rejected` | `rows` (the observation count) and `unique_lift` (the marginal downstream count) both present and never both positive: `rows` is 0 for `zero_unique_observations`, `unique_lift` is 0 for `zero_marginal_downstream_rows`; other observed metrics null | Required | `blocking_reason` and `kill_test_result` both equal the kill reason; `review_date` null |
| `inventory_only` | All observed metrics null | Required | Catalog row without a measurement |

Identity route evidence and contract, A2:

The following facts come from zero credit vendor documentation checked on 25 August 2026, with independent price and return-field support from the vendor email dated the same day. They are evidence about route identity, parameters, return fields, and pricing. They do not authorize a paid call, config change, or creator replacement.

| Canonical route | Route role | Required params | Optional params | Documented price | Paid implementation gate |
|---|---|---|---|---|---|
| `GET /v1/instagram/search/profiles` | `identity_discovery` | `query` | None documented | 1 credit per call | Live response and status, pagination, empty result, refund receipt, and field optionality verification |
| `GET /v1/tiktok/search/users` | `identity_discovery` | `query` | None documented | 1 credit per call | Live response and status, pagination, empty result, refund receipt, and field optionality verification |
| `GET /v1/threads/search/users` | `identity_discovery` | `query` | None documented | 1 credit per call | Live response and status, pagination, empty result, refund receipt, and field optionality verification |
| `GET /v1/prism/handle-audit` | `identity_hygiene` | `handle` | `platforms`, `sample` | 5 credits for up to four platforms, plus 1 credit for each extra platform; full refund when every requested platform is a definitive miss or cannot yield usable profile evidence | Live response and status, pagination, empty result, refund receipt, and field optionality verification |

The four method and path pairs are frozen for canonical `endpoint_id` derivation. Paid calls remain blocked only on the listed live behavior gates.

Identity discovery is a search surface, not an audit. Its sanitized output contract is:

```json
{
  "route_role": "identity_discovery",
  "query": "fixture_creator",
  "platform": "instagram",
  "candidates": [
    {"candidate_handle": "fixture_candidate", "follower_count": 1200, "verified": true, "profile_evidence_state": "live", "post_evidence_state": "not_supported", "days_since_last_post": null}
  ],
  "rows": 1
}
```

`candidate_handle STRING REQUIRED`, `follower_count INT64 NULLABLE`, and `verified BOOL REQUIRED` are vendor observations. `rows INT64 REQUIRED` is the normalized, deduplicated candidate count. Follower count ranks candidates only. It is never sufficient identity proof and never rewrites a configured creator.

Identity hygiene is an audit surface, not a discovery search. Its sanitized output contract is:

```json
{
  "route_role": "identity_hygiene",
  "identity_ref": "fixture_identity_001",
  "requested_platforms": ["instagram", "tiktok"],
  "platform_results": [
    {"platform": "instagram", "found": true, "profile_evidence_state": "live", "post_evidence_state": "sampled", "days_since_last_post": 45},
    {"platform": "tiktok", "found": false, "profile_evidence_state": "not_found", "post_evidence_state": "not_found", "days_since_last_post": null}
  ],
  "credits_charged": 5,
  "rows": 2,
  "identity_health_state": "handle_drift"
}
```

`requested_platforms` contains one or more unique platforms. Each result has `platform STRING REQUIRED`, `found BOOL NULLABLE`, `profile_evidence_state STRING REQUIRED`, `post_evidence_state STRING REQUIRED`, and `days_since_last_post INT64 NULLABLE`. `rows INT64 REQUIRED` is the normalized platform result count.

Profile evidence state is exactly `live`, `stale`, `not_found`, or `unavailable`. `found` is true for live or stale, false for not found, and null for unavailable. Post evidence state is exactly `sampled`, `observed_empty`, `not_supported`, `not_found`, or `unavailable`. `days_since_last_post` is required and nonnegative only when post evidence is sampled. It remains null whenever post evidence cannot support recency and is never coerced to zero.

`credits_charged NUMERIC REQUIRED` follows the documented price: 5 credits for up to four requested platforms plus 1 credit for each extra platform. A full refund sets it to 0 only when every requested platform is either a definitive miss with profile evidence `not_found` or cannot yield usable profile evidence with state `unavailable`. A refund is a billing receipt, not evidence that a profile exists.

The identity health enum is exact within A2:

| State | Rule | Allowed consequence |
|---|---|---|
| `active` | Configured handle is found and every found platform has 0 to 29 days since last post | Keep identity, no mutation |
| `quiet` | Configured handle is found and every found platform is 30 to 89 days since last post | Retain identity and reduce freshness expectations only |
| `dead` | Configured handle is found and every found platform is at least 90 days since last post | Flag for human review, no automatic deletion |
| `handle_drift` | Configured handle is not found on at least one expected platform after a successful audit | Run identity discovery as a separate search and present candidates for review |
| `upstream_exhausted` | Vendor returns `UPSTREAM_ERROR` after its retry and fallback sequence | Make no creator status conclusion; requeue under the rule below |
| `unknown` | Profile or post evidence is unavailable, unsupported, empty, conflicting, or otherwise cannot support an identity or recency decision | Fail closed, keep configuration unchanged, and surface the missing evidence |

`active`, `quiet`, and `dead` require usable profile evidence and sampled post evidence with nonnull recency for every expected platform. State precedence is `upstream_exhausted`, then `unknown`, then `handle_drift`, then `dead`, `quiet`, or `active`. A missing expected platform or unusable recency therefore cannot be hidden by activity on another platform.

A discovered replacement requires `verified = true`, independent evidence that it represents the configured identity, and an explicit human approval event before any config mutation. A larger follower count cannot satisfy any of those gates by itself. No Phase 0 operation mutates creator configuration.

Vendor retry guidance is an A2 operational rule. An `UPSTREAM_ERROR` reaching the engine has already exhausted four vendor attempts and a second independent source inside 48 seconds. The engine must not retry inline. It may enqueue the same normalized request once at run end with the same idempotency key. If that requeue also returns `UPSTREAM_ERROR`, the result is `upstream_exhausted` and no further call occurs in that run.

## 3. QA namespace and canary isolation

The approved canary cases are:

| Canary ID | Input condition | Required result |
|---|---|---|
| `coherent_multi_source` | Coherent signal with at least two qualifying families | Discovered and `ready` |
| `single_source` | One qualifying family | `thin` |
| `opposing_directions` | Two qualifying families disagree on direction | `contradictory` |
| `foreign_market_collision` | Foreign market collision | Rejected before client output |
| `duplicate_identity` | Duplicate signal identities | Merged with lineage receipt |
| `unsupported_claims` | Demographic or causal assertion without measured evidence | Blocked |

Recommended isolation, A3 `NEEDS_ALBERT_APPROVAL`:

1. The QA dataset is `trends_v2_staging_qa` and contains schema identical copies of all seven v2 tables.
2. Every QA row uses `client_scope_id = qa_canary`, `theme_id = qa`, and `discovery_mode = canary` where the field exists.
3. Main staging writers reject `client_scope_id = qa_canary`.
4. Staging compatibility views read only `trends_v2_staging` and contain an explicit `client_scope_id != 'qa_canary'` guard even though QA is a separate dataset.
5. No authorized view crosses from the QA dataset into main staging or production.
6. Canary fixtures use `.invalid` URLs and synthetic row identifiers.
7. A QA result table is approved in principle by the isolated canary requirement. Its recommended name `canary_results_v2` and exact shape remain part of A3.

Dataset and IAM recommendation, A3:

| Resource or principal | Exact recommended value | Grant or retention |
|---|---|---|
| Main staging dataset | `ogilvy-trends-v2.trends_v2_staging` | Location `US`; no dataset default table expiry; no partition expiry on main v2 tables |
| QA dataset | `ogilvy-trends-v2.trends_v2_staging_qa` | Location `US`; no dataset default table expiry; 90 day partition retention on every QA v2 table and `canary_results_v2` |
| Engine staging identity | `trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com` | Dataset scoped `roles/bigquery.dataEditor` on main staging; project scoped `roles/bigquery.jobUser` |
| Canary identity | `trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com` | Dataset scoped `roles/bigquery.dataEditor` on QA only; project scoped `roles/bigquery.jobUser` |
| Listening Post staging identity | `listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com` | Dataset scoped `roles/bigquery.dataViewer` on main staging; project scoped `roles/bigquery.jobUser` |

Production service identities receive no staging or QA data editor role. The engine staging identity receives no QA write role. The canary identity receives no main staging or production role. The Listening Post identity receives no QA or production data role through this contract. No authorized view crosses from QA into staging or production. Live DDL remains stopped until both datasets and the three dedicated identities are approved and provisioned.

Recommended `canary_results_v2` fields: the seven scope envelope fields, `canary_run_id STRING REQUIRED`, `canary_id STRING REQUIRED`, `expected_result STRING REQUIRED`, `actual_result STRING NULLABLE`, `passed BOOL REQUIRED`, `started_at TIMESTAMP REQUIRED`, `finished_at TIMESTAMP NULLABLE`, and `error_code STRING NULLABLE`. Partition by `DATE(started_at)`, retain partitions for 90 days, and cluster by `canary_id, passed`.

Example canary result:

```json
{
  "client_scope_id": "qa_canary",
  "market_scope": ["za"],
  "brand_config_id": "qa",
  "audience_lens_ids": [],
  "theme_id": "qa",
  "canary_run_id": "canary_20260825_001",
  "canary_id": "single_source",
  "contract_version": "2.0.0",
  "run_id": "run_canary_001",
  "expected_result": "thin",
  "actual_result": "thin",
  "passed": true,
  "started_at": "2026-08-25T05:00:00Z",
  "finished_at": "2026-08-25T05:00:03Z",
  "error_code": null
}
```

## 4. Evidence object contract

The plan requires one named object contract and removal of positional receipt arrays after compatibility fixtures pass. The persisted evidence fields are plan exact. The UI and view fields below are A5 `NEEDS_ALBERT_APPROVAL`.

The persisted and view object contains every field below except `citation_label`. `citation_label` is answer local. It is assigned only while one answer is serialized and is never stored in BigQuery or exposed by a global view.

```json
{
  "evidence_id": "ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01",
  "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "row_id": "fixture_row_001",
  "source_family": "reddit",
  "platform": "reddit",
  "source_label": "Fixture community",
  "author_label": "@fixture_maker",
  "excerpt": "A sanitized fixture excerpt showing the observed repair routine.",
  "metric_label": "128 interactions",
  "url": "https://example.invalid/posts/fixture-001",
  "published_at": "2026-08-24T19:20:00Z",
  "claim_role": "direction",
  "direction": "rising",
  "geo_confidence": 0.96,
  "citation_label": "[E1]",
  "availability": "available"
}
```

Field rules:

| Field | Type | Nullable | Rule |
|---|---|---|---|
| Persisted evidence fields | As section 2.3 | As section 2.3 | Values copy from the evidence row. |
| `source_label` | string | Yes | Human readable source or host when retention permits. |
| `author_label` | string | Yes | Null when the source has no public named author. |
| `excerpt` | string | Yes | Sanitized source excerpt. Null when retention or policy prevents display. |
| `metric_label` | string | Yes | Observed metric with unit. Never inferred. |
| `citation_label` | string | No | Answer serialization only. Stable within one answer and never persisted. |
| `availability` | string | No | Recommended enum `available`, `aged_out`, or `unavailable`. |

Compatibility boundary: current Listening Post `receipts` are positional six item arrays. A v2 adapter may read those arrays only to create a temporary object fixture. No new v2 writer may emit a positional array. The adapter is deleted when the named object fixtures pass. This is an approved migration direction. The object field additions and deletion release are A5.

## 5. Client scope configuration

Recommended file and shape, A7 `NEEDS_ALBERT_APPROVAL`: `configs/client_scopes.yaml` is the only default resolver for engine jobs. Listening Post receives the resolved object from the API and may not duplicate defaults in JavaScript.

```yaml
contract_version: "2.0.0"
default_scope_id: ogilvy_default
scopes:
  ogilvy_default:
    client_scope_id: ogilvy_default
    market_scope: [za, ng, ke]
    brand_config_id: ogilvy_default
    audience_lens_ids: [gen_z_inferred]
    theme_id: ogilvy_42
    enabled: true
  fixture_scope:
    client_scope_id: fixture_scope
    market_scope: [za]
    brand_config_id: fixture_brand
    audience_lens_ids: []
    theme_id: fixture_theme
    enabled: true
```

Resolution rules:

1. A caller supplied `client_scope_id` must resolve to one enabled scope.
2. Omitted scope resolves through `default_scope_id` only at the API or job boundary.
3. Resolved values are copied into every artifact. Consumers do not look up mutable defaults after the artifact is written.
4. A caller may narrow `market_scope` to a nonempty subset of the configured values. Expansion is rejected.
5. A caller may narrow `audience_lens_ids` to a configured subset. Unknown lenses are rejected.
6. No BSA or future client identifier is added until its own scope is approved. The config shape supports it without a code fork.

## 6. API contracts reserved by Phase 0

Phase 0 freezes these contracts and fixtures. It does not need to expose live endpoints. Endpoint names, async lifecycle, intent enum, error behavior, and token enforcement are A4 `NEEDS_ALBERT_APPROVAL`.

### 6.1 Open question submission

Recommended endpoint: `POST /api/v2/questions`.

Request:

```json
{
  "contract_version": "2.0.0",
  "run_id": "question_run_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "question": "What is newly emerging without a supplied keyword?",
  "decision": "Choose one culture story to investigate this week.",
  "window": {"start_date": "2026-08-18", "end_date": "2026-08-25"},
  "output_form": "cited_brief"
}
```

Types and nullability:

| Field | Type | Required | Rule |
|---|---|---|---|
| Scope envelope | Section 1 | Yes | Must resolve before work begins. |
| `question` | string | Yes | Natural language question. Nonempty after trim. |
| `decision` | string | No | Recommended. Null means router must state the decision gap. A4. |
| `window.start_date` | ISO date | Yes | Inclusive. |
| `window.end_date` | ISO date | Yes | Inclusive and not before start. |
| `output_form` | string | Yes | Recommended enum `cited_brief`, `comparison`, or `evidence_plan`. A4. |

Accepted response, HTTP 202:

```json
{
  "contract_version": "2.0.0",
  "run_id": "question_run_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "question_id": "q_7fa45438f3664c66",
  "status": "pending",
  "stage": null,
  "evidence_plan": null,
  "answer": null,
  "error": null,
  "budget": {"input_ceiling": 32000, "output_ceiling": 4000, "input_used": 0, "output_used": 0, "remaining_max_output_tokens": 4000, "exhausted": false, "metering_status": "ready"},
  "missing_work": [],
  "created_at": "2026-08-25T07:10:00Z",
  "updated_at": "2026-08-25T07:10:00Z",
  "status_url": "/api/v2/questions/q_7fa45438f3664c66/status"
}
```

The same `run_id` and normalized request are idempotent and return the existing `question_id`. The same `run_id` with a different normalized request returns `run_id_conflict`.

### 6.2 Question status and answer

Recommended polling endpoint: `POST /api/v2/questions/{question_id}/status`. POST is used so the required scope context remains a typed JSON body rather than a comma encoded query or custom header set.

Every polling request carries the same resolved seven field envelope as submission:

```json
{
  "contract_version": "2.0.0",
  "run_id": "question_run_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme"
}
```

The context must exactly match the stored question context before any resource is returned. A mismatch returns the same nonretryable `scope_invalid` transport error used for an unknown scope and does not reveal whether the question exists in another scope.

The async state machine is exactly `pending`, `running`, then one of `complete`, `partial`, or `failed`. Every state contains `question_id`, the seven field scope envelope, `status`, `created_at`, `updated_at`, `budget`, and `missing_work`. `pending` uses the accepted response shape in section 6.1. `running` adds `stage`, whose enum is `planning` or `answering`. `complete` contains an answer and no error. `partial` contains grounded answer sections and named missing work. `failed` contains no answer, a typed error, the final budget state, and named missing work. Terminal question resources return HTTP 200. Immediate request validation, scope, capacity, and dependency failures use the transport errors in section 13 and do not create a question resource.

Running response example:

```json
{
  "contract_version": "2.0.0",
  "run_id": "question_run_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "question_id": "q_7fa45438f3664c66",
  "status": "running",
  "stage": "planning",
  "evidence_plan": null,
  "answer": null,
  "error": null,
  "budget": {"input_ceiling": 32000, "output_ceiling": 4000, "input_used": 2100, "output_used": 180, "remaining_max_output_tokens": 3820, "exhausted": false, "metering_status": "ready"},
  "missing_work": [],
  "created_at": "2026-08-25T07:10:00Z",
  "updated_at": "2026-08-25T07:10:04Z"
}
```

Evidence plan object:

```json
{
  "plan_id": "ep_20921c47fd734479",
  "intent": "landscape",
  "decision": "Choose one culture story to investigate this week.",
  "markets": ["za"],
  "window": {"start_date": "2026-08-18", "end_date": "2026-08-25"},
  "audience_lenses": [],
  "source_families": ["news", "reddit", "search", "youtube"],
  "historical_comparison": {"required": true, "window_days": 365},
  "evidence_requirements": ["two independent source families", "geo gate passed", "current receipts"],
  "output_form": "cited_brief",
  "evidence_state": "ready",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "run_id": "question_run_001",
  "contract_version": "2.0.0"
}
```

The plan fields are exact approved requirements. Object names, types, and `plan_id` are A4.

`intent` is exactly one of `landscape`, `explanation`, `comparison`, `trajectory`, `audience`, `creator`, `brand_role`, `whitespace`, `risk`, `historical_analogue`, `campaign_opportunity`, `source_coverage`, or `custom`. Intent selects tools and evidence only. It never restricts which natural language question can exist. A question outside the named intents routes as `custom` rather than failing or becoming a hidden template.

Cited answer envelope:

```json
{
  "contract_version": "2.0.0",
  "run_id": "question_run_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "question_id": "q_7fa45438f3664c66",
  "status": "complete",
  "evidence_plan": {
    "plan_id": "ep_20921c47fd734479",
    "intent": "landscape",
    "decision": "Choose one culture story to investigate this week.",
    "markets": ["za"],
    "window": {"start_date": "2026-08-18", "end_date": "2026-08-25"},
    "audience_lenses": [],
    "source_families": ["news", "reddit", "search", "youtube"],
    "historical_comparison": {"required": true, "window_days": 365},
    "evidence_requirements": ["two independent source families", "geo gate passed", "current receipts"],
    "output_form": "cited_brief",
    "evidence_state": "ready",
    "client_scope_id": "fixture_scope",
    "market_scope": ["za"],
    "brand_config_id": "fixture_brand",
    "audience_lens_ids": [],
    "theme_id": "fixture_theme",
    "run_id": "question_run_001",
    "contract_version": "2.0.0"
  },
  "answer": {
    "title": "One emerging move to investigate",
    "scope": "South Africa, 18 to 25 August 2026",
    "sections": [
      {"section_id": "what_changed", "heading": "What changed", "body": "Repair tutorials formed a coherent cross source routine this week [E1] [E2].", "evidence_ids": ["ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01", "ev_80b6b168cc1849c7a55b6c7d8e9f0123a4b5c6d7e8f90123456789abcdef0123"]},
      {"section_id": "limitations", "heading": "What this cannot conclude", "body": "The evidence does not measure population prevalence or demographics.", "evidence_ids": []}
    ]
  },
  "evidence": [
    {"evidence_id": "ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01", "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef", "row_id": "fixture_row_001", "source_family": "reddit", "platform": "reddit", "source_label": "Fixture community", "author_label": "@fixture_maker", "excerpt": "A sanitized fixture excerpt showing the observed repair routine.", "metric_label": "128 interactions", "url": "https://example.invalid/posts/fixture-001", "published_at": "2026-08-24T19:20:00Z", "claim_role": "direction", "direction": "rising", "geo_confidence": 0.96, "citation_label": "[E1]", "availability": "available"},
    {"evidence_id": "ev_80b6b168cc1849c7a55b6c7d8e9f0123a4b5c6d7e8f90123456789abcdef0123", "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef", "row_id": "fixture_row_002", "source_family": "youtube", "platform": "youtube", "source_label": "Fixture channel", "author_label": "@fixture_channel", "excerpt": "A second sanitized fixture excerpt from an independent family.", "metric_label": "42 comments", "url": "https://example.invalid/videos/fixture-002", "published_at": "2026-08-24T21:00:00Z", "claim_role": "direction", "direction": "rising", "geo_confidence": 0.93, "citation_label": "[E2]", "availability": "available"}
  ],
  "evidence_state": "ready",
  "contradictions": [],
  "missing_work": [],
  "human_review_required": false,
  "error": null,
  "budget": {"input_ceiling": 32000, "output_ceiling": 4000, "input_used": 18420, "output_used": 1920, "remaining_max_output_tokens": 2080, "exhausted": false, "metering_status": "persisted"},
  "created_at": "2026-08-25T07:10:00Z",
  "updated_at": "2026-08-25T07:12:00Z"
}
```

Every substantive sentence must map to at least one `evidence_id`, except explicit limitations, scope labels, and user supplied decisions. Every citation must resolve to one evidence object in the same response. Political output sets `human_review_required = true` and blocks export or sharing until a human approval event exists. Social conversation is never described as representative polling.

Failed question resource example:

```json
{
  "contract_version": "2.0.0",
  "run_id": "question_run_004",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "question_id": "q_9d0112b6df584199",
  "status": "failed",
  "stage": "answering",
  "evidence_plan": null,
  "answer": null,
  "error": {"code": "metering_persistence_failed", "message": "Usage could not be persisted, so new model calls stopped for this run.", "retryable": false, "missing_fields": [], "retry_after_seconds": null},
  "budget": {"input_ceiling": 32000, "output_ceiling": 4000, "input_used": 2100, "output_used": 180, "remaining_max_output_tokens": 3820, "exhausted": false, "metering_status": "failed"},
  "missing_work": ["Answer generation did not run after the metering fault."],
  "created_at": "2026-08-25T07:10:00Z",
  "updated_at": "2026-08-25T07:10:05Z"
}
```

### 6.3 Dynamic signal read fixture

Recommended fixture response used by Phase 2 before live rows exist:

```json
{
  "contract_version": "2.0.0",
  "run_id": "run_fixture_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "signal": {
    "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
    "signal_date": "2026-08-25",
    "market": "za",
    "label": "Weekend repair culture",
    "why_now": "Two source families rose against their trailing baselines.",
    "possible_response": "Show practical repair steps and cite local makers.",
    "novelty_score": 0.74,
    "velocity_score": 0.68,
    "breadth_score": 0.62,
    "independence_score": 0.71,
    "historical_similarity": 0.33,
    "geo_confidence": 0.94,
    "evidence_state": "ready",
    "topic_tags": ["economy"],
    "evidence_ids": ["ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01", "ev_80b6b168cc1849c7a55b6c7d8e9f0123a4b5c6d7e8f90123456789abcdef0123"]
  },
  "evidence": [
    {"evidence_id": "ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01", "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef", "row_id": "fixture_row_001", "source_family": "reddit", "platform": "reddit", "source_label": "Fixture community", "author_label": "@fixture_maker", "excerpt": "A sanitized fixture excerpt showing the observed repair routine.", "metric_label": "128 interactions", "url": "https://example.invalid/posts/fixture-001", "published_at": "2026-08-24T19:20:00Z", "claim_role": "direction", "direction": "rising", "geo_confidence": 0.96, "availability": "available"},
    {"evidence_id": "ev_80b6b168cc1849c7a55b6c7d8e9f0123a4b5c6d7e8f90123456789abcdef0123", "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef", "row_id": "fixture_row_002", "source_family": "youtube", "platform": "youtube", "source_label": "Fixture channel", "author_label": "@fixture_channel", "excerpt": "A second sanitized fixture excerpt from an independent family.", "metric_label": "42 comments", "url": "https://example.invalid/videos/fixture-002", "published_at": "2026-08-24T21:00:00Z", "claim_role": "direction", "direction": "rising", "geo_confidence": 0.93, "availability": "available"}
  ]
}
```

### 6.4 Source Lab fixture

`source_lab_all_statuses` is a synthetic sanitized shape fixture only. It does not claim to be the live SocialCrawl catalog and cannot supply a live route count or production capability assertion.

The frozen fixture contains one synthetic row for each of the six statuses. The compact example below shows the `available_unwired` row; the same table contract governs the other four rows.

```json
{
  "contract_version": "2.0.0",
  "run_id": "run_fixture_source_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "catalog_date": "2026-08-25",
  "catalog_digest": "sha256:7114b2f944a1d4a2b55c6d7e8f901234a5b6c7d8e9f0123456789abcdef01234",
  "synthetic_route_count": 1,
  "fixture_source": "synthetic_shape_only",
  "sources": [
    {
      "vendor": "fixture_vendor",
      "http_method": "GET",
      "route_path": "/v1/fixture/search",
      "endpoint_id": "endpoint_975bfad07a864e13b98cad1e20f34657a8b9c0d1e2f3456789abcdef01234569",
      "route_role": "evidence",
      "status": "available_unwired",
      "official_capability": "Searches public posts by supplied text.",
      "official_parameters": ["query", "page_cursor"],
      "official_price_components": [{"meter": "request", "billing_basis": "flat", "currency": "credit", "unit": "request", "quantity": 1, "source_url": "https://example.invalid/pricing", "checked_at": "2026-08-25T06:00:00Z", "amount": 1, "minimum_amount": null, "maximum_amount": null, "tier_condition": null}],
      "observed_metrics": null,
      "blocking_reason": null,
      "last_checked_at": "2026-08-25T06:00:00Z"
    }
  ]
}
```

Pinned live catalog status:

```json
{
  "artifact_id": "socialcrawl_live_catalog_v2",
  "status": "blocked",
  "freshness_reported_route_count": 400,
  "catalog_output_unique_route_count": 381,
  "canonical_live_route_count": null,
  "canonical_live_digest": null,
  "blocking_reason": "The live freshness surface reports 400 routes while the explicit live catalog output contains 381 unique method and path pairs.",
  "required_resolution": "Repair or upgrade discovery, then rederive from the unformatted live response before freeze."
}
```

The live identity is the unique set of canonical `<METHOD> <PATH>` pairs. Its digest is SHA256 over UTF8, LF terminated, lexicographically sorted unique lines in that exact form. CI compares the synthetic fixture only. The scheduled read only integration check remains blocked from setting a pinned live count or digest until the 400 versus 381 discrepancy is resolved.

### 6.5 SocialCrawl balance and funding gate

The balance endpoint is a free read. Its response gates paid pilots and optional lanes. Shape and decision behavior are A2 `NEEDS_ALBERT_APPROVAL` as part of the Source Lab and source performance package. The balance, baseline, runway, and timestamps in the JSON below are synthetic schema examples.

```json
{
  "contract_version": "2.0.0",
  "run_id": "run_fixture_balance_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "balance": 12000,
  "observed_at": "2026-01-15T06:00:00Z",
  "read_status": "ok",
  "recent_deductions": null,
  "deductions_interval": null,
  "funding_math_status": "unknown",
  "funded_increase_amount": null,
  "funded_increase_observed": false,
  "selected_top_up_credits": 250000,
  "baseline_credits_per_day": 400,
  "baseline_window": {"start_date": "2026-01-01", "end_date": "2026-01-14", "completed_days": 14},
  "runway_days": 30,
  "monthly_optional_credit_cap": 25000,
  "monthly_optional_credits_used": null,
  "optional_calls_enabled": false
}
```

Field rules:

| Field | Type | Mode | Rule |
|---|---|---|---|
| `balance` | NUMERIC | NULLABLE | Finite nonnegative value only when read status is ok |
| `observed_at` | TIMESTAMP | REQUIRED | Observation time in UTC |
| `read_status` | STRING | REQUIRED | Exact enum `ok` or `unknown` |
| `recent_deductions` | NUMERIC | NULLABLE | Measured intervening deductions from the vendor response |
| `deductions_interval` | STRUCT | NULLABLE | Exact interval covered by recent deductions |
| `funding_math_status` | STRING | REQUIRED | Exact enum `complete` or `unknown` |
| `funded_increase_amount` | NUMERIC | NULLABLE | Current balance plus deductions minus prior balance when the interval bridges |
| `funded_increase_observed` | BOOL | REQUIRED | True only when complete funding math yields a positive increase; false on unknown |
| `selected_top_up_credits` | NUMERIC | NULLABLE | Selected commercial amount only, never payment proof |
| `baseline_credits_per_day` | NUMERIC | NULLABLE | Measured mean over completed baseline days, never a config estimate |
| `baseline_window` | STRUCT | NULLABLE | Exact completed observation window |
| `runway_days` | NUMERIC | NULLABLE | `balance / baseline_credits_per_day` only when both are valid and baseline is positive |
| `monthly_optional_credit_cap` | NUMERIC | REQUIRED | 25,000 credits until a new approval changes the cap |
| `monthly_optional_credits_used` | NUMERIC | NULLABLE | Measured current calendar month optional lane use |
| `optional_calls_enabled` | BOOL | REQUIRED | False on unknown, before funding, or below the runway floor |

Nested baseline window fields are `start_date DATE REQUIRED`, `end_date DATE REQUIRED`, and `completed_days INT64 REQUIRED`. Nested deduction interval fields are `start_at TIMESTAMP REQUIRED` and `end_at TIMESTAMP REQUIRED`. The reader uses `data.balance` only after the vendor response states `success = true`; top level `credits_remaining` is not a substitute for the balance field.

Let the prior successful observation be `(prior_balance, prior_observed_at)`. Funding math is complete only when the deduction interval starts at `prior_observed_at`, ends at the current `observed_at`, and contains no gap or overlap. Then `funded_increase_amount = balance + recent_deductions - prior_balance`, and `funded_increase_observed` is true only when that amount is greater than 0. If either balance read is invalid or the deduction interval does not bridge, the evidence conclusion is unknown: `funding_math_status = unknown` and `funded_increase_amount = null`. The authorization gate fails closed with `funded_increase_observed = false`.

Commercial direction evidence from the 25 August 2026 vendor email is recorded only as `selected_top_up_credits = 250000`. The selected amount is not proof of payment, funding, or available balance. Execution requires a fresh free balance read and a complete deduction bridge. Live execution observations belong in the root progress ledger, never in durable schema examples or frozen fixtures. Paid pilots remain off until that fresh evidence makes the increase observable. Optional calls stop automatically when runway falls below 14 measured baseline days or when the next optional call would take measured calendar month use above the 25,000 credit cap. Unknown monthly use also disables optional calls. Baseline calls continue while credits remain and the low balance alert names the projected exhaustion date and required decision.

### 6.6 PULSE watched signal line fixture

The first PULSE contract is a fixture only in Phase 0. PULSE remains on its current production source during shadow. Shape is A8 `NEEDS_ALBERT_APPROVAL`.

```json
{
  "contract_version": "2.0.0",
  "run_id": "run_fixture_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "signal_name": "Weekend repair culture",
  "continuity_day": 4,
  "what_changed": "A second independent source family joined the signal.",
  "evidence_state": "ready",
  "deep_link": "/signals/sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef?market=za",
  "material_change": "source_family_agreement"
}
```

## 7. Fixture freeze contract

Recommended canonical directory, manifest, names, and copy rule are A6 `NEEDS_ALBERT_APPROVAL`.

Canonical engine directory: `tests/fixtures/open_intelligence/v2/`.

Listening Post copy: `tests/fixtures/open_intelligence/v2/`. Its manifest digest must equal the engine manifest digest before frontend tests run. Neither repository generates fixtures from live client data.

Every JSON fixture has this outer envelope:

```json
{
  "fixture_id": "golden_01_emerging_without_keyword",
  "fixture_kind": "golden_task",
  "contract_version": "2.0.0",
  "sanitized": true,
  "frozen_at": "2026-08-25T00:00:00Z",
  "payload": {}
}
```

Required fixture IDs:

| Fixture ID | Contract exercised |
|---|---|
| `dynamic_signal_ready` | Dynamic signal and object evidence |
| `evidence_ready` | Ready rules |
| `evidence_thin` | Thin rules |
| `evidence_contradictory` | Contradictory rules |
| `evidence_unchecked` | Unchecked rules |
| `lineage_merge` | `merges_into` edge |
| `lineage_split` | `splits_into` edges |
| `source_lab_all_statuses` | Synthetic sanitized shape for five Source Lab statuses and nullability. Not a live catalog fixture. |
| `evidence_plan_election` | Full visible plan |
| `cited_answer_election` | Political safety, citations, limits, brand role |
| `pulse_watched_signal_line` | PULSE line contract |
| `golden_01_emerging_without_keyword` | New signal without supplied keyword |
| `golden_02_why_moving` | Movement explanation and proof |
| `golden_03_cross_market_difference` | ZA, NG, and KE comparison |
| `golden_04_carriers` | First carriers and spreaders |
| `golden_05_history` | Prior analogue, difference, and what follows |
| `golden_06_brand_role` | Credible brand role and occupied spaces |
| `golden_07_audience_lens` | Measured or inferred lens with confidence |
| `golden_08_source_agreement` | Source agreement, disagreement, and absence |
| `golden_09_source_gap` | Dark or underused source surface |
| `golden_10_custom` | Custom question outside named intents |
| `golden_11_election_brand_role` | Election acceptance task and human review gate |

Manifest example:

```json
{
  "contract_version": "2.0.0",
  "fixture_count": 22,
  "fixtures": [
    {"fixture_id": "dynamic_signal_ready", "sha256": "7114b2f944a1d4a2b55c6d7e8f901234a5b6c7d8e9f0123456789abcdef01234"}
  ],
  "manifest_sha256": "4bd2ed78ecf6d2aba66b7c8d9e0f1234a5b6c7d8e9f0123456789abcdef01234"
}
```

The manifest records all 22 fixtures. The abbreviated example shows one row only. Every fixture, catalog, and manifest digest is exactly 64 lower case hexadecimal characters. The blocked live SocialCrawl catalog is not one of the 22 frozen fixtures and cannot enter the manifest until the 400 versus 381 discrepancy is resolved.

## 8. Contract version behavior

Recommended version rule, A1 `NEEDS_ALBERT_APPROVAL`:

1. Initial version is `2.0.0`.
2. Major changes remove or reinterpret a field, change a type or nullability, change an enum meaning, change a key, or change budget exhaustion semantics.
3. Minor changes add an optional field or a new enum value that old consumers explicitly treat as unknown.
4. Patch changes fix descriptions, examples, or validation without changing accepted payloads.
5. API requests must send a supported major version. Missing or unsupported versions fail before work begins.
6. BigQuery rows retain the version written at creation. Migrations do not rewrite history only to bump a version.
7. Fixtures are immutable within a version. Any content change produces a new digest and at least a patch version.
8. Compatibility views are pinned to one major version and expose `contract_version` explicitly.
9. Producers may not silently downgrade. Consumers may reject a higher major with `unsupported_contract_version`.

## 9. Token budgets and partial results

The plan requires declared input and output ceilings before the first new Vertex call. The numbers, enforcement, and lifecycle are A4 `NEEDS_ALBERT_APPROVAL`.

Recommended ceilings:

| Consumer | Unit | Input ceiling | Output ceiling | Retry rule |
|---|---|---:|---:|---|
| `dynamic_signal_summary` | One promoted signal summary | 8,000 tokens | 800 tokens | No automatic model retry after budget exhaustion |
| `open_question_answer` | Planning plus answer for one question | 32,000 tokens | 4,000 tokens | No unmetered retry |

Recommended open question allocation: planning may use at most 8,000 input and 800 output tokens. Answer generation receives the unused total budget, but the combined calls may never exceed 32,000 input and 4,000 output tokens.

Every question resource budget object contains `input_ceiling INT64`, `output_ceiling INT64`, `input_used INT64`, `output_used INT64`, `remaining_max_output_tokens INT64`, `exhausted BOOL`, and `metering_status STRING`. `remaining_max_output_tokens` is recalculated after every persisted call as the nonnegative output ceiling minus candidate plus thought tokens already used. `metering_status` is exactly `ready`, `persisted`, or `failed`.

Each new consumer model call writes exactly one idempotent delta event to `gemini_usage`. New nullable physical columns are `run_id STRING`, `stage STRING`, and `call_index INT64` because BigQuery cannot add required fields to the existing table. The new consumers require all three values before write. Existing consumers keep their current aggregate rows until separately migrated.

For new consumer rows, `usage_id` is `usage_` plus the full SHA256 digest using ordered inputs `(run_id, consumer, stage, call_index, market, gemini_model)` and the null safe encoding in section 2.1. `call_index` is zero based within one run, consumer, and stage. Stage is exactly `summary` for `dynamic_signal_summary`, or `planning` and `answering` for `open_question_answer`.

One usage delta example:

```json
{
  "usage_id": "usage_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "run_id": "question_run_001",
  "stage": "planning",
  "call_index": 0,
  "trend_date": "2026-08-25",
  "consumer": "open_question_answer",
  "market": "za",
  "gemini_model": "gemini-3.5-flash",
  "calls": 1,
  "prompt_tokens": 2100,
  "completion_tokens": 180,
  "recorded_at": "2026-08-25T07:10:04Z"
}
```

The row contains only that call's delta. `calls` is always 1. `completion_tokens` is candidate tokens plus thought tokens for that response. It is never a cumulative run snapshot. Persistence uses `MERGE` on `usage_id`; retrying an ambiguous write reuses the same event and cannot append a duplicate. The writer reads back the event or receives an explicit merge acknowledgement before another new consumer call starts. Cost, ceiling, and billing reconciliation queries sum unique usage events, never cumulative snapshots.

`dynamic_signal_summary` and `open_question_answer` join the existing consumer registry before their first call. Billing reconciliation and the cost watchdog must read both before calls are enabled.

Partial result behavior, A4 `NEEDS_ALBERT_APPROVAL`:

1. Before every model call, the caller must use `count_tokens` on the exact next request and compare it with the remaining cumulative input allowance.
2. A prompt above the remaining input allowance is deterministically reduced at the evidence selection boundary or rejected. It is never sent over budget.
3. Every call sets `max_output_tokens` to the smaller of the stage allowance and the exact remaining cumulative output allowance. A call with no remaining output allowance is not sent.
4. Completion usage is candidate tokens plus thought tokens. Tool use and cached content are forbidden until their usage counters are added to the ledger and cost contract.
5. The exact response counters and deterministic event inputs are retained until the per call delta event is merged and acknowledged.
6. If persistence fails, the writer may retry that same delta event with the same `usage_id`, but it may not repeat the model call. The run records `metering_persistence_failed`, disables every further `dynamic_signal_summary` and `open_question_answer` call for that run, and returns the grounded work already available as partial or failed.
7. A metering persistence fault makes the watchdog and billing gap gate fail. New consumer calls stay disabled until the ledger is writable and reconciliation is green.
8. Any completed grounded sections return with `status = partial` when budget or metering stops further work.
9. `missing_work` names each omitted section or source check.
10. `budget.exhausted = true` only for a ceiling stop. Metering failure uses `metering_status = failed` without falsely marking the budget exhausted.
11. Citations remain mandatory for returned claims.
12. The system does not pad a partial result with inference, retry at a higher limit, or hide the missing work.
13. If no grounded section exists, the terminal status is `failed` with `budget_exhausted_no_grounded_result` or `metering_persistence_failed` as applicable.

Example partial answer:

```json
{
  "contract_version": "2.0.0",
  "run_id": "question_run_002",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "question_id": "q_a1d0fbdb7ee64ad0",
  "status": "partial",
  "stage": "answering",
  "evidence_plan": {
    "plan_id": "ep_partial_001",
    "intent": "historical_analogue",
    "decision": "Choose one culture story to investigate this week.",
    "markets": ["za"],
    "window": {"start_date": "2026-08-18", "end_date": "2026-08-25"},
    "audience_lenses": [],
    "source_families": ["reddit"],
    "historical_comparison": {"required": true, "window_days": 365},
    "evidence_requirements": ["current receipt", "historical comparison"],
    "output_form": "cited_brief",
    "evidence_state": "thin",
    "client_scope_id": "fixture_scope",
    "market_scope": ["za"],
    "brand_config_id": "fixture_brand",
    "audience_lens_ids": [],
    "theme_id": "fixture_theme",
    "run_id": "question_run_002",
    "contract_version": "2.0.0"
  },
  "answer": {"sections": [{"section_id": "what_changed", "body": "One bounded cited result [E1].", "evidence_ids": ["ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01"]}]},
  "evidence": [
    {"evidence_id": "ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01", "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef", "row_id": "fixture_row_001", "source_family": "reddit", "platform": "reddit", "source_label": "Fixture community", "author_label": "@fixture_maker", "excerpt": "A sanitized fixture excerpt showing the observed repair routine.", "metric_label": "128 interactions", "url": "https://example.invalid/posts/fixture-001", "published_at": "2026-08-24T19:20:00Z", "claim_role": "direction", "direction": "rising", "geo_confidence": 0.96, "citation_label": "[E1]", "availability": "available"}
  ],
  "evidence_state": "thin",
  "missing_work": ["Historical comparison was not completed within the token ceiling."],
  "human_review_required": false,
  "error": null,
  "budget": {"input_ceiling": 32000, "output_ceiling": 4000, "input_used": 31920, "output_used": 3870, "remaining_max_output_tokens": 130, "exhausted": true, "metering_status": "persisted"},
  "created_at": "2026-08-25T07:10:00Z",
  "updated_at": "2026-08-25T07:13:00Z"
}
```

## 10. Staging compatibility views

The approved boundary is additive tables, staging only view consumers, and no production reader change. Evidence object and view fields are A5 `NEEDS_ALBERT_APPROVAL`. The other compatibility view names and shapes are included in the same approval group.

Recommended views in `trends_v2_staging`:

| View | Grain | Source | Initial consumer |
|---|---|---|---|
| `v_signal_board_v2` | One latest signal snapshot per client, market, and signal | Candidates plus latest analysis | 42 staging Today and Explore adapter |
| `v_signal_evidence_v2` | One evidence object row | Evidence joined to retained sanitized display fields | 42 staging signal detail and evidence drawer |
| `v_source_lab_v2` | One latest endpoint and market row | Source performance daily | 42 staging Source Lab fixtures and later live route |
| `v_open_intelligence_health_v2` | One run and market health row | Candidate, evidence, analysis, and prediction counts | Shadow watchdog only |

`v_signal_board_v2` output fields:

```text
contract_version, run_id, client_scope_id, market_scope, brand_config_id,
audience_lens_ids, theme_id, signal_id, signal_date, market, label,
cluster_build_version, discovery_mode, topic_tags, novelty_score,
velocity_score, breadth_score, independence_score, historical_similarity,
geo_confidence, evidence_state, summary, why_now, possible_response,
limitations, contradictions, evidence_ids, human_review_required, created_at
```

Example flat board row:

```json
{
  "contract_version": "2.0.0",
  "run_id": "run_fixture_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "signal_date": "2026-08-25",
  "market": "za",
  "label": "Weekend repair culture",
  "cluster_build_version": "hybrid_graph_v1",
  "discovery_mode": "dynamic",
  "topic_tags": ["economy"],
  "novelty_score": 0.74,
  "velocity_score": 0.68,
  "breadth_score": 0.62,
  "independence_score": 0.71,
  "historical_similarity": 0.33,
  "geo_confidence": 0.94,
  "evidence_state": "ready",
  "summary": "Repair tutorials are moving into shared weekend routines.",
  "why_now": "Two source families rose against their own trailing baselines.",
  "possible_response": "Show practical repair steps and cite local makers.",
  "limitations": ["No representative polling or demographic measurement."],
  "contradictions": [],
  "evidence_ids": ["ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01"],
  "human_review_required": false,
  "created_at": "2026-08-25T06:48:00Z"
}
```

`v_signal_evidence_v2` output fields:

```text
contract_version, run_id, client_scope_id, market_scope, brand_config_id,
audience_lens_ids, theme_id, signal_date, market, signal_id, evidence_id,
row_id, source_family, platform, source_label, author_label, excerpt,
metric_label, url, published_at, claim_role, direction, geo_confidence,
evidence_state, availability, created_at
```

Example flat evidence view row:

```json
{
  "contract_version": "2.0.0",
  "run_id": "run_fixture_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "signal_date": "2026-08-25",
  "market": "za",
  "signal_id": "sig_7cc4d6a8d7c74207a11b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef",
  "evidence_id": "ev_7ccf5f3e3bd34e41a22b3c4d5e6f708193a4b5c6d7e8f90123456789abcdef01",
  "row_id": "fixture_row_001",
  "source_family": "reddit",
  "platform": "reddit",
  "source_label": "Fixture community",
  "author_label": "@fixture_maker",
  "excerpt": "A sanitized fixture excerpt showing the observed repair routine.",
  "metric_label": "128 interactions",
  "url": "https://example.invalid/posts/fixture-001",
  "published_at": "2026-08-24T19:20:00Z",
  "claim_role": "direction",
  "direction": "rising",
  "geo_confidence": 0.96,
  "evidence_state": "ready",
  "availability": "available",
  "created_at": "2026-08-25T06:45:00Z"
}
```

`v_source_lab_v2` output fields:

```text
contract_version, run_id, client_scope_id, market_scope, brand_config_id,
audience_lens_ids, theme_id, source_performance_id, metric_date, vendor, http_method, route_path, endpoint_id, route_role,
source_family, market, status, calls, credits, rows, integrity,
geo_precision, unique_lift, last_success_at, downstream_consumers,
official_capability, official_parameters, official_price_components, blocking_reason,
kill_test_result, review_date, last_checked_at, balance, observed_at,
balance_read_status, recent_deductions, deductions_interval, funding_math_status,
funded_increase_amount, funded_increase_observed, selected_top_up_credits,
baseline_credits_per_day, baseline_window, runway_days,
monthly_optional_credit_cap, monthly_optional_credits_used, optional_calls_enabled
```

Example flat Source Lab view row:

```json
{
  "contract_version": "2.0.0",
  "run_id": "run_fixture_source_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "source_performance_id": "srcperf_642bd7cfb89e4d83a88b9c0d1e2f3456a7b8c9d0e1f23456789abcdef0123456",
  "metric_date": "2026-08-25",
  "vendor": "fixture_vendor",
  "http_method": "GET",
  "route_path": "/v1/fixture/search",
  "endpoint_id": "endpoint_975bfad07a864e13b98cad1e20f34657a8b9c0d1e2f3456789abcdef01234569",
  "route_role": "evidence",
  "source_family": "other",
  "market": null,
  "status": "available_unwired",
  "calls": null,
  "credits": null,
  "rows": null,
  "integrity": null,
  "geo_precision": null,
  "unique_lift": null,
  "last_success_at": null,
  "downstream_consumers": [],
  "official_capability": "Searches public posts by supplied text.",
  "official_parameters": ["query", "page_cursor"],
  "official_price_components": [{"meter": "request", "billing_basis": "flat", "currency": "credit", "unit": "request", "quantity": 1, "source_url": "https://example.invalid/pricing", "checked_at": "2026-08-25T06:00:00Z", "amount": 1, "minimum_amount": null, "maximum_amount": null, "tier_condition": null}],
  "blocking_reason": null,
  "kill_test_result": null,
  "review_date": null,
  "last_checked_at": "2026-08-25T06:00:00Z",
  "balance": null,
  "observed_at": null,
  "balance_read_status": null,
  "recent_deductions": null,
  "deductions_interval": null,
  "funding_math_status": null,
  "funded_increase_amount": null,
  "funded_increase_observed": null,
  "selected_top_up_credits": null,
  "baseline_credits_per_day": null,
  "baseline_window": null,
  "runway_days": null,
  "monthly_optional_credit_cap": null,
  "monthly_optional_credits_used": null,
  "optional_calls_enabled": null
}
```

`v_open_intelligence_health_v2` output fields:

```text
contract_version, run_id, client_scope_id, market_scope, brand_config_id,
audience_lens_ids, theme_id, signal_date, market,
candidate_count, ready_count, thin_count, contradictory_count,
unchecked_count, evidence_count, analysis_count, prediction_count,
latest_created_at
```

Example health row:

```json
{
  "contract_version": "2.0.0",
  "run_id": "run_fixture_001",
  "client_scope_id": "fixture_scope",
  "market_scope": ["za"],
  "brand_config_id": "fixture_brand",
  "audience_lens_ids": [],
  "theme_id": "fixture_theme",
  "signal_date": "2026-08-25",
  "market": "za",
  "candidate_count": 4,
  "ready_count": 2,
  "thin_count": 1,
  "contradictory_count": 1,
  "unchecked_count": 0,
  "evidence_count": 13,
  "analysis_count": 4,
  "prediction_count": 2,
  "latest_created_at": "2026-08-25T06:50:00Z"
}
```

Per view ordering contract, A5:

| View | Partition or group grain | Ordering and collapse rule |
|---|---|---|
| `v_signal_board_v2` candidate | `client_scope_id, market, signal_id` | Choose descending `signal_date`, then descending `created_at`, then descending `run_id` |
| `v_signal_board_v2` matching analysis | Exact selected candidate tuple `client_scope_id, signal_date, market, signal_id, run_id` | Equality join only. At most one analysis row. No timestamp based cross-run selection. |
| `v_signal_evidence_v2` | Evidence natural key from section 2.3 | No latest collapse. Preserve every natural key row. |
| `v_source_lab_v2` | `client_scope_id, vendor, http_method, route_path, COALESCE(market, 'global')` | Choose descending `metric_date`, then descending `last_checked_at`, then descending `run_id` |
| `v_open_intelligence_health_v2` | `contract_version, run_id, client_scope_id, signal_date, market` | Group at the stated grain. No latest collapse. |

The board analysis join is exactly `analysis.client_scope_id = candidate.client_scope_id`, `analysis.signal_date = candidate.signal_date`, `analysis.market = candidate.market`, `analysis.signal_id = candidate.signal_id`, and `analysis.run_id = candidate.run_id`. Because `analysis_id` is deterministic from that tuple, zero or one matching analysis row is valid. More than one is a contract violation and fails the view verification gate; the view never resolves it by timestamp. When the same-run analysis is absent, the board still returns the selected candidate. `summary`, `why_now`, `possible_response`, `limitations`, `contradictions`, `evidence_ids`, and `human_review_required` are null, while `evidence_state` comes from the candidate. Analysis from another run is never joined.

Compatibility boundaries:

1. Existing `trend_scores`, `trend_analysis`, `daily_summary`, `v_trend_briefs`, PULSE, and Looker objects remain unchanged.
2. Curated categories continue to read the current path during shadow.
3. Dynamic discoveries read only these versioned views or frozen fixtures.
4. The v2 views do not imitate `query_group`, `trend_score`, positional receipts, Brand24 fields, unsupported demographics, or raw engine labels.
5. An explicit staging adapter maps view fields to UI objects. No production adapter or reader changes in Phase 0.
6. Views name every selected column. They do not use `SELECT *`.
7. Views reject QA scope and apply only the per view ordering contract above.
8. PULSE receives only its frozen fixture in Phase 0. Daily summary and Looker receive parity queries only, not source switches.

## 11. Idempotent migration rules

Recommended migration behavior, A3 `NEEDS_ALBERT_APPROVAL`:

1. Fresh setup DDL and migration DDL ship together. Every new schema file appears in `SCHEMA_ORDER` in dependency order.
2. Table creation uses `CREATE TABLE IF NOT EXISTS` with exact partitioning, clustering, descriptions, and fields.
3. View creation uses `CREATE OR REPLACE VIEW` only in staging and only after all referenced tables exist.
4. A dry run prints the fully resolved project, dataset, table names, and SQL. It performs no query job.
5. Apply mode accepts only project `ogilvy-trends-v2`, the exact approved staging or QA dataset, location `US`, and the matching dedicated writer identity. Suffix matching alone is insufficient.
6. Reapplying DDL produces no schema or data change.
7. Writers use deterministic IDs and one of two approved patterns: `MERGE` on the natural key for mutable daily snapshots, or append after an existing key check for immutable predictions and outcomes.
8. Temporary merge tables live in the same staging dataset, have a one hour expiry, and are deleted in a `finally` block, matching the current BigQuery helper convention.
9. Existing rows are never truncated. No Phase 0 migration deletes or rewrites production rows.
10. `CREATE TABLE IF NOT EXISTS` success is never treated as schema proof because it has no effect when the object already exists. Schema verification uses the BigQuery table resource read back and compares field name, type, top level and nested mode, partition field, partition retention, clustering order, dataset location, default table expiry, and descriptions against the accepted contract.
11. Fixture verification compares every SHA256 digest in both repositories.
12. Contract verification compares API examples, view fields, and fixture shapes against the accepted contract before Phase 0 exits.
13. A future additive field on an existing table ships through a separate `ADD COLUMN IF NOT EXISTS` migration and must be NULLABLE or REPEATED. A create DDL edit alone is not a migration.
14. Live apply remains stopped until A3 is approved and the datasets, dedicated identities, role grants, retention, cache, and deployment target are provisioned and read back.

## 12. Staging service isolation

Recommended deployment contract, A3 `NEEDS_ALBERT_APPROVAL`:

| Boundary | Required staging value | Rejected value |
|---|---|---|
| Engine job | `trends-engine-open-intelligence-staging` | Any production job or implicit target |
| Project and region | `ogilvy-trends-v2`, `us-central1` | Inherited CLI project or region |
| Engine identity | `trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com` | Default compute or a production job identity |
| Engine environment | `TRENDS_ENV=staging` | `prod` or implicit default |
| Engine dataset base | `BIGQUERY_DATASET=trends_v2` resolving to `trends_v2_staging` | `trends_v2` direct production resolution |
| Engine command | `python` with args `scripts/run_rss_now.py` | Inherited command or another ops entrypoint |
| Engine task timeout | `10800` seconds | Inherited timeout |
| Engine image | `us-central1-docker.pkg.dev/ogilvy-trends-v2/pipeline/trends-engine@sha256:<64 lower case hex>` | Mutable tag or seven character source tag |
| Engine labels | `environment=staging` and `source-sha=<full 40 character approved commit>` on resource and template | Missing label, short SHA, or labels at only one location |
| Listening Post dataset | `BQ_DATASET=trends_v2_staging` | Production dataset |
| Listening Post service | `listening-post-staging` | `listening-post` |
| Listening Post identity | `listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com` | Default compute service account |
| Cache bucket | Dedicated staging bucket, recommended `listening-post-staging-cache` | Production cache bucket |
| Cache key prefix | `open-intelligence/v2/staging/` | Empty or production prefix |
| Application source label | `open-intelligence-staging` | Production source label |
| Cloud Run source label | Full 40 character approved commit in `source-sha` on resource and template | Short or absent source SHA |
| Vendor secrets | None required for Phase 0 fixtures and schemas | Retired source secret or paid vendor secret |
| QA dataset | `trends_v2_staging_qa` | Main staging or production dataset |
| QA identity | `trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com` | Engine staging or production identity |

Current live context is not the target contract. `listening-post-staging` currently carries full `source-sha=22d83bd62ec96746817ba13f44da72298fe9cf55`, uses `listening-post-staging-cache`, reads `trends_v2_dev`, and runs as the default compute service account. Any future staging update must preserve a full source SHA at both label locations while replacing the dev dataset and default identity with the approved dedicated values above.

The dedicated engine staging deployment definition must create the named job when absent and update that exact job when present. It may not invoke the current production deploy loop. Build output is resolved to an immutable image digest before the job definition is written. The deployment record stores the full approved commit separately from the image digest.

Before any execution is allowed, a read back must prove the exact project, region, job name, service account, command, args, timeout, environment, dataset, immutable image digest, resource labels, and template labels. Any mismatch is a stop. The Listening Post staging service must also fail startup when its service name, dataset, identity, cache bucket, application source label, or full Cloud Run source SHA does not match the staging allowlist. Synthetic fixtures are served only when explicit fixture mode is enabled and the source label states fixture. Fixture cache keys include contract version and the full manifest digest so they cannot collide with live staging rows.

## 13. Error contract

Recommended uniform v2 error envelope, A4 `NEEDS_ALBERT_APPROVAL`:

```json
{
  "contract_version": "2.0.0",
  "run_id": "question_run_003",
  "status": "failed",
  "error": {
    "code": "scope_invalid",
    "message": "The requested market is outside the configured client scope.",
    "retryable": false,
    "missing_fields": [],
    "retry_after_seconds": null
  }
}
```

| HTTP | Code | Trigger | Retryable |
|---:|---|---|---|
| 400 | `request_invalid` | Malformed dates, empty question, invalid enum, or duplicate market | No |
| 400 | `scope_invalid` | Unknown scope or expansion beyond configured scope | No |
| 400 | `run_id_conflict` | Same run ID with different normalized input | No |
| 409 | `unsupported_contract_version` | Unsupported major version | No |
| 409 | `fixture_digest_mismatch` | Engine and UI fixture manifests differ | No |
| 422 | `scope_incomplete` | Required election, market, or window detail is missing | Yes after caller supplies the named field |
| 429 | `capacity_busy` | Concurrency ceiling reached | Yes, with retry delay |
| 500 | `contract_violation` | Producer output fails its accepted schema | No automatic model retry |
| 502 | `model_unavailable` | Model failed before any grounded result | Yes within caller initiated retry policy |
| 503 | `dependency_unavailable` | Required BigQuery or evidence dependency unavailable | Yes |

Evidence state `unchecked` and answer status `partial` are successful typed states, not transport errors. Terminal `failed` question resources, including `budget_exhausted_no_grounded_result` and `metering_persistence_failed`, return HTTP 200 in the exact resource shape from section 6.2. They are not entries in the immediate transport error table.

## 14. Compatibility and policy boundaries

1. Dynamic graph membership is deterministic. Gemini may summarize retained promoted clusters but may not choose membership.
2. Evidence readiness follows the exact approved rules for ready, thin, contradictory, and unchecked.
3. Direction is computed per source family against its own trailing baseline.
4. Fixed categories remain explicit curated legacy references and never become dynamic identity.
5. No new system becomes board identity until evidence qualified parity passes for at least 10 completed daily runs across ZA, NG, and KE and all other named quality gates pass.
6. Current production PULSE, Looker, daily summary, watchdog decisions, tables, services, jobs, revisions, and caches remain untouched.
7. Historical Brand24 rows may remain labelled legacy. No active v2 runtime reader or fixture depends on Brand24.
8. Audience claims identify measured or inferred status, source, window, and confidence. No age or gender claim is permitted without a measured demographic source.
9. Election scoped output sets `human_review_required = true`, states that social conversation is not representative polling, and blocks export or sharing until human approval.
10. Canaries never enter raw client content, briefs, PULSE, 42 boards, or compatibility views.
11. A quiet live week is healthy only when the isolated canaries pass.

## 15. Approval matrix

### 15.1 Exact plan items, no new approval choice introduced

| Item | Exact requirement retained |
|---|---|
| Seven v2 table names | Candidates, evidence, lineage, analysis, predictions, outcomes, source performance daily |
| Scope field names | Seven exact fields in section 1 |
| Candidate field names | Exact approved candidate list plus scope fields |
| Candidate physical layout | Partition by signal date, cluster by market and evidence state |
| Evidence core field names | Exact approved evidence list |
| Lineage core field names and relation values | Continues, merges into, splits into |
| Evidence states and rules | Ready, thin, contradictory, unchecked |
| Metering consumers | Dynamic signal summary and open question answer |
| Model boundary | No Gemini for membership or clustering |
| Source Lab statuses and metric restrictions | Active, pilot, available unwired, blocked, permanently rejected, inventory only |
| Canary cases and isolation purpose | Six approved cases in a dedicated QA namespace |
| Compatibility fence | Staging views only initially, production readers unchanged |
| Fixture subjects | Dynamic signal, evidence readiness, lineage merge, lineage split, Source Lab, evidence plan, cited answer, PULSE line, every golden task |
| Political safety | Human review before export or sharing and no polling claim |
| Partial result principle | Return cited partial work and name missing work when budget is exhausted |

### 15.2 Decisions requiring Albert approval

There are eight approval required decision groups. Every unapproved field, mode, enum, key, limit, view, fixture, identity, and deployment value in this draft is owned by exactly one group.

| ID | Recommended ruling | Why approval is required |
|---|---|---|
| A1 | Approve contract `2.0.0`, required scope envelope modes, lower case market convention, retry identity, and version behavior | The plan fixes field names but not types, modes, casing, or version semantics |
| A2 | Approve the complete BigQuery schema package for all seven tables, null safe full digest IDs and ordered inputs, keys, table grains, enums, nested field modes, partitions, clustering, evidence retention fields, one row outcomes, source family map versioning, canonical SocialCrawl GET routes and parameters, route roles, profile and post evidence states, normalized row counts, refund rules, identity health contracts, price components, deduction bridge funding math, vendor retry rule, 14 day runway floor, and 25,000 monthly optional cap | The plan fully defines only part of three tables and gives concepts for the remaining storage and vendor contracts |
| A3 | Approve staging and QA datasets, `US` location, retention, dedicated identities and IAM, migration allowlist, isolated cache, dedicated engine job, immutable image and full source SHA labels, read back gates, and production fence | These infrastructure values are required before live DDL or deployment and are not approved by the plan |
| A4 | Approve question routes, seven field polling context, exact intent enum, async state resources, transport and terminal errors, token ceilings, mandatory `count_tokens`, remaining `max_output_tokens`, thought token accounting, one deterministic delta usage event per call, metering stop behavior, and partial result contract | The plan gives required behavior but not exact API or enforceable cost shapes |
| A5 | Approve the stored evidence display fields, answer local citation labels, positional receipt removal boundary, four flat staging compatibility view shapes, and their per view ordering rules | The plan requires a named evidence object and staging views but does not fix their complete caller shapes or ordering |
| A6 | Approve the 22 synthetic sanitized fixture IDs, directories, envelopes, full SHA256 manifest, cross repository digest gate, and blocked separation from the unresolved live SocialCrawl catalog | Fixture subjects are approved but names, storage, digest rules, and blocked live artifact handling are not |
| A7 | Approve `configs/client_scopes.yaml` and its default, narrowing, and artifact copy rules | Scope configuration is required but its file and resolution shape are unspecified |
| A8 | Approve the PULSE watched signal fixture object | The plan names its required content but not the exact durable consumer object |

## 16. Current code conflicts and concerns

No unresolved load bearing conflict required stopping a surface. The following boundaries need implementation checks after contract approval:

1. Current Listening Post topic receipts are positional arrays. The approved plan requires named evidence objects and later removal of `receiptKey`. The compatibility adapter must be temporary and fixture tested.
2. Current Listening Post errors use both FastAPI `detail` objects and a custom capacity object. A v2 endpoint cannot inherit both without an explicit error contract.
3. Current engine schemas do not contain any of the seven v2 tables. Only candidate, evidence, and lineage core fields are explicit in the plan. Analysis, prediction, outcome, and source performance shapes are new decisions in this draft.
4. Current engine dataset resolution already supports `TRENDS_ENV=staging`, but the accepted migration must refuse production targets rather than trusting operator intent.
5. Current Listening Post staging uses the dedicated cache but still reads `trends_v2_dev` and runs as the default compute identity. No staging deployment may proceed until A3 is accepted and the dataset and identity are replaced.
6. Current source family behavior is defined in the engine channel family map. Contract tests must prove a single vendor cannot manufacture independent family agreement and must label retained historical Brand24 evidence as legacy.
7. The live Source Lab catalog remains blocked because one live surface reports 400 routes while the explicit catalog output contains 381 unique method and path pairs. The synthetic shape fixture is not a live catalog claim.
8. Token ceilings are not approved. No new Vertex consumer may be called before A4, metering registration, per call persistence, watchdog coverage, and billing reconciliation are green.
9. Zero credit documentation freezes the four identity method and path pairs, parameters, evidence states, and documented prices. Paid implementation remains blocked on live response and status, pagination, empty result, refund receipt, and field optionality verification.
10. The selected commercial amount alone does not satisfy the execution gate. Every execution requires a fresh free balance read and complete deduction bridge. The durable contract does not record a live balance observation or mark payment and funding complete.
11. The current Gemini usage helper emits random cumulative append rows. New consumers require the accepted deterministic per call delta path before any model call.
