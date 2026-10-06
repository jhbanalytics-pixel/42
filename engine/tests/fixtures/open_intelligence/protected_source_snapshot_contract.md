# Protected source snapshot operation

Status: proposed for approval, 7 September 2026. Contract version: `open_intelligence_protected_source_snapshot_v1`. This document is not an execution approval or permission receipt.

## Purpose and scope

Connect the tested production snapshot capturer to the existing durable execution approval and result store. The operation creates fixed native snapshots, captures selected rows, verifies their receipts and retains the source material privately. It does not generate answers, certify cultural quality, publish a graph or release signals.

The initial authorization proposed here covers UTC cutoff dates 7 and 8 September 2026, after the 8 September frozen milestone has passed. Each cutoff permits one initial invocation, at most one recovery invocation and at most one canonical capture. Failed and unknown attempts consume these allowances; they are not free retries. Each invocation still needs its own valid exact-source execution manifest and consumption. A contract approval cannot substitute for that manifest or fabricate a human signature. No operation runs on the frozen phase0 branch.

| Binding | Exact proposed value |
|---|---|
| Operation | `source_snapshot_capture` |
| Cloud Run job | `projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging` |
| Runtime identity | Existing `trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com` |
| Command | `python scripts/staging/capture_protected_production_snapshot.py --cutoff-date <YYYY-MM-DD> --mode <initial|recover>`; argument order and the two dates are closed |
| Environment | Existing staging project/environment bindings; no provider secrets |
| Source lanes | `event_ledger`, `seed_graph`, `seed_candidates`, `enriched_content`, `raw_content`, under `ogilvy-trends-v2.trends_v2_dev` |
| Destination | Exactly `ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_<YYYYMMDD>_<lane>` for the admitted cutoff and five lanes |
| As-of | UTC midnight immediately after cutoff; actual later capture time is retained |
| Runtime ceiling | 600 seconds, zero task retries, one task, at most two vCPUs and 8 GiB memory |
| Capture ceiling | Existing 180 seconds, at most five SELECTs and 1,000,000,000 combined billed query bytes |
| Other paid work | Zero vendor credits and zero model calls |
| Source mutation | Zero source DML rows; no source-table update, clone replacement, r16 mutation or release write |
| Resource creation | At most five fixed snapshot creation submissions per cutoff across initial and recovery invocations, each create-only |

`max_rows_written=0` retains its source-DML unit. It does not mean five rows or claim that snapshot creation has no effect. The five DDL resources are a separate fixed plan invariant. Existing approval-consumption/result audit writes retain their existing accounting.

## Inputs and versioning

Reuse `ExecutionManifest` unchanged, with this operation added to its closed registry and explicit zero-vendor/zero-model limit validation. The sorted artifact names are exactly `build_provenance`, `capture_contract`, `capture_plan`, `recovery_context`, `source_metadata`, `storage_policy`. Their digests bind exact canonical bytes before execution.

`capture_plan` is an exact envelope `{contract_version,cutoff_date,client_scope_id,market_scope,snapshot_plan,creation_statements}`. Its literal version is `open_intelligence_protected_capture_plan_v1`; dates/scopes/markets follow the result types below and must equal the CLI and consumed manifest context. `snapshot_plan` uses the unchanged serialized `SnapshotPlan` shape from `production_snapshot._serialize_plan`, including its reviewed metadata digest. It does not acquire client-scope fields that its dataclass does not contain.

`creation_statements` is a five-element ordered array of exact objects `{lane:string,sql:string,sql_digest:sha256}`. Each SQL statement is derived from that lane's validated structural plan statement by adding only an explicit expiration at source-as-of plus 90 days. Its digest is the UTF-8 SQL byte digest. The structural plan SQL digest is not labelled as the executed DDL digest. Verify both the derivation and actual native job SQL. There is no default-expiration assumption or post-creation expiration mutation. The existing nine-field snapshot, legacy replay hashes and r16 copies remain unchanged.

`recovery_context` is exactly `null` for an initial invocation. For recovery it is exactly `{contract_version,initial_manifest_sha256,initial_consumption_id,initial_execution_name,initial_result_id,initial_result_digest}`. Version is `open_intelligence_source_capture_recovery_v1`; digests use lowercase SHA256, consumption/result IDs use their existing execution-store types, and execution name is the exact native Cloud Run resource. All referenced records must be read from the existing execution store and match, not accepted from the caller alone. Missing or unresolved initial result evidence refuses recovery. There is no third invocation under this contract.

`storage_policy` is exactly `{contract_version,allowance_id,bucket,input_prefix,output_prefix,snapshot_retention_days,object_retention_days,max_source_logical_bytes,max_artifact_bytes,reserved_micro_usd_per_cutoff,price_review}`. Version is `open_intelligence_source_capture_storage_v1`; allowance ID is `source_capture_20260907_20260908_v1`; bucket/prefix values are fixed below; day counts are exact integers 90; byte caps are 5368709120 and 536870912; reservation is 500000 micro-USD per cutoff. `price_review` contains exactly `{reviewed_at,expires_at,pricing_sources,assumptions,maximum_cycle_cost_micro_usd}`: UTC timestamps, sorted source URL strings, sorted explicit assumption strings and a nonnegative integer no greater than 500000. It must cover both invocations, all native/GCS retention including provider recovery retention, and all failed/unknown work at its full ceiling. Expiry is at most 24 hours after review. Required values cannot be supplied by source content or model output. Configuration reads may occur before consumption; no creation or source-bearing artifact write may occur then.

## Storage and spending proposal

Use one private bucket, `ogilvy-trends-v2-oi-source-artifacts-staging`, in US, with public access prevention and uniform bucket access. The name is proposed; existence, creation authority and policies must be verified before use. Do not repurpose the shared application cache or approval-signature bucket.

Input objects use `inputs/<sha256>/<artifact-name>.json`. Output objects use `captures/<initial-manifest-sha256>/<content-sha256>/capture.json`, including during recovery. SHA256 values are lowercase 64-character hex; the initial manifest comes from consumed authority and any validated recovery chain. Each output is canonical JSON with a maximum size of 512 MiB, written with `if_generation_match=0`. Preserve its generation, byte count, content digest and server creation time. Read the exact generation back and compare bytes. An ambiguous write is reconciled by exact object/generation/hash, never overwritten or copied into a new manifest prefix.

`capture.json` has exactly `{contract_version,initial_manifest_sha256,capture_plan,capture,creation_evidence}`. Version is `open_intelligence_protected_source_capture_artifact_v1`. `capture_plan` is the envelope above; `capture` is the complete detached output accepted by `validate_captured_production_snapshot`. `creation_evidence` contains exactly five ordered objects `{lane,manifest_sha256,consumption_id,execution_name,native_job,snapshot_metadata}`. String identities use the existing execution-store types; native_job and snapshot_metadata retain the full native API dictionaries and are independently validated against the approved executed DDL, principal, source/as-of and expiration. This is where full source-bearing capture and creation evidence lives. Generations in contract objects are positive JSON integers; native provider payloads retain their provider representation.

Retain output objects for 90 days from server creation and native snapshots for 90 days from source-as-of, without automatic renewal. Lifecycle expiry must be disclosed in retained receipts and the production decision package. Do not lock a bucket retention policy or change existing bucket policies. Longer retention or recurring daily capture requires a later decision.

Proposed additional allowance: USD 1 total, allocated as USD 0.50 to each cutoff, separate from the existing USD 10 model-evaluation allowance. Reserve the full per-cutoff amount atomically in the existing approval-consumption transaction when admitting its first initial invocation. Reuse that store's serialization lock and consumption/approval records, adding explicit operation checks rather than a parallel money or success ledger. The SQL procedure must enforce one initial and at most one linked recovery consumption per cutoff for this fixed allowance, across different manifest hashes and racing executions. It must also validate the exact CLI cutoff/mode and matching fixed contract/storage-policy bindings. Failures and unknown spend keep the reservation; this contract has no refund, replacement-cutoff or automatic retry path. A recovery reuses the same reservation and cannot reserve another USD 0.50. All capture SELECTs across a cutoff's initial/recovery chain share the same five-query/one-GB ceiling.

Before consumption, re-read actual size and current applicable prices. Refuse if prices, billing basis or size are unknown or the maximum complete cycle estimate exceeds USD 0.50. This price review must cover two 600-second invocations, the single bounded artifact, all snapshot/object retention and all query work. Do not claim the monetary ceiling is enforced by a billing alert. Cap each five-table source set at 5 GiB summed native logical bytes, and record that this measures storage size, not unique cultural records. No free-tier credit is assumed.

## Permissions and execution

Use existing permission where effective access is proved. Enumerate any missing resource grants in the exact deployment/IAM manifest before applying them. Runtime needs the fixed base snapshot/read permissions, staging snapshot creation/read permissions, its own bounded query jobs, exact job/build readback and existing approval procedures. On the new source-artifact bucket it needs object create/read only, with no delete, overwrite, public-access or IAM permission. Deployment and IAM administration stay with the operator. This contract does not authorize broad project roles or changes to production.

Load and validate the exact runtime job, identity, source SHA, image, build, artifact hashes and approval through `_load_execution_authority`, then consume through `_consume_execution_authority`. The existing operation cannot borrow `r3_apply`, migration, pilot or snapshot-repair approval.

Derive each initial creation job ID as `oi_v3_snapshot_<manifest-sha256>_<lane>`. Recovery refers to the original IDs for attempted lanes; only demonstrably unattempted lanes may use IDs derived from the new admitted recovery manifest. Use plain `CREATE SNAPSHOT TABLE`, never replace or a permissive already-exists clause. Reconcile an unknown submission acknowledgement using that exact native job and target; do not submit again. Verify completed job configuration, creator, target, as-of and snapshot metadata before proceeding. Preserve partial resources and diagnostics when ownership or billing is unresolved. A fresh recovery manifest may authorize only demonstrably unattempted lanes.

After initial creation, call `capture_production_snapshot` and `validate_captured_production_snapshot`. Preserve native result schemas, pre-LIMIT counts, duplicate match counts, physical/adapted digests, selected-key/fallback rules and missingness. Stored observations remain unprivileged; operation success proves capture and storage, not collection completeness or cultural validity.

Recovery reuses an existing verified capture/artifact, its original captured_at and object generation, without rerunning the collector or renewing retention. An ambiguous upload uses the original artifact_attempt URI/digest. If the original bytes cannot be recovered and proved, refuse rather than silently recapture. Recovery may finish demonstrably unattempted creation lanes and perform the first capture only when authoritative initial result evidence proves that no capture SELECT or artifact attempt started. Any uncertain earlier query prevents new queries. These restrictions intentionally preserve unresolved failure rather than inventing free or identity-changing recovery.

## Result and failures

Use the existing outer `ExecutionResult` and its transaction/readback machinery. Keep `result_id`, manifest digest, consumption ID, execution name and completion timestamp in that outer record. Do not put `result_id` inside its own hashed payload.

The compact result payload contains exactly these fields:

| Field | Type and rule |
|---|---|
| `contract_version` | Literal `open_intelligence_protected_source_snapshot_v1` |
| `cutoff_date` | ISO date, one admitted cutoff |
| `client_scope_id` | Exact nonempty admitted scope |
| `market_scope` | Sorted unique nonempty array from `ke,ng,za` |
| `source_as_of` | Exact UTC cutoff-close timestamp |
| `captured_at` | Original completed capture timestamp; null on failure before capture |
| `snapshot_plan_digest` | Lowercase SHA256 |
| `snapshot_digest` | Lowercase source snapshot digest; null before successful capture |
| `capture_receipt_digest` | Canonical capture receipt digest; null before successful validation |
| `creation_records` | Ordered array, one per attempted lane, each exactly `{lane:string,destination:string,job_id:string,native_job_digest:string|null,state:succeeded|failed|unresolved}` |
| `artifact_attempt` | Null before serialization, otherwise exactly `{uri:string,size_bytes:positive-int,sha256:string,captured_at:UTC-timestamp}`; retained before upload to support exact reconciliation |
| `stored_artifact` | Null until readback, otherwise exactly `{uri:string,generation:positive-int,size_bytes:positive-int,sha256:string,created_at:UTC-timestamp}` |
| `query_count` | Exact nonnegative integer, at most five capture SELECTs |
| `total_bytes_billed` | Exact nonnegative integer, or null when unknown; never substitute zero |
| `limitations` | Sorted controlled codes, preserving upstream/streaming exclusions and incomplete evidence |
| `missing_checks` | Sorted controlled failure/recovery codes; empty only for the operation's own completed checks |

Example outcome: a completed cutoff-7-September capture has five succeeded creation records, a generation-bound private `capture.json`, its original capture timestamp and zero missing operation checks. Its upstream collection and cultural-quality limitations remain. Failed/partial results preserve unresolved jobs and a null stored artifact where appropriate; they never mint a usable-source or release flag.

Illustrative failure payload, not an execution receipt:

```json
{
  "contract_version": "open_intelligence_protected_source_snapshot_v1",
  "cutoff_date": "2026-09-07",
  "client_scope_id": "ogilvy_default",
  "market_scope": ["ke", "ng", "za"],
  "source_as_of": "2026-09-08T00:00:00+00:00",
  "captured_at": null,
  "snapshot_plan_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "snapshot_digest": null,
  "capture_receipt_digest": null,
  "creation_records": [
    {
      "lane": "event_ledger",
      "destination": "ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_event_ledger",
      "job_id": "oi_v3_snapshot_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_event_ledger",
      "native_job_digest": null,
      "state": "unresolved"
    }
  ],
  "artifact_attempt": null,
  "stored_artifact": null,
  "query_count": 0,
  "total_bytes_billed": null,
  "limitations": ["upstream_collection_completeness_unproven"],
  "missing_checks": ["snapshot_creation_acknowledgement_unknown"]
}
```

Refusals use controlled codes for approval mismatch, source metadata mismatch, destination ownership conflict, unknown creation acknowledgement, failed creation, unavailable capture billing, incomplete capture, artifact conflict and result-readback failure. Diagnostics contain identities, counts and hashes, not source text or credentials. A nonzero exit and a recorded failure remain failures even if some native resources exist.

## Required implementation proof and live gate

Before live execution: failure-first tests for approval-before-write, wrong source/image/identity, two-date limits, wrong target/as-of/expiration, partial creation, unknown acknowledgement, deterministic recovery, query/billing ceilings, create-only artifact collision, exact-generation readback and result recording. Use the actual BigQuery and GCS SDK boundary where practical, then independent review and full tests on settled source.

Approval of this proposed contract permits its bounded implementation and preparation. It does not lift the 8 September milestone, certify source quality, deploy an unreviewed image, apply unlisted IAM grants or replace the exact per-execution approval mechanism. The final source/image/resource/IAM/price manifest must be concrete and accepted before live use.
