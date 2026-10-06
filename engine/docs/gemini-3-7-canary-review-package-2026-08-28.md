# Gemini 3.7 evidence-consumer canary review package

Date: 28 August 2026

Branch: `feat/evidence-canary`

Base: `a963265e546b166fc9413fc39bdcc97686c2d322`

Status: local implementation proof only. No model, Vertex, GCP, Secret Manager, BigQuery, deployment, production or remote call was made.

## Approved inputs

| Artifact | SHA256 |
|---|---|
| Consumer canary contract | `36c7100880d754e3b435fce7b03ff18742387c92c3d2b7b80adbc049e98accba` |
| Caller appendix | `457dde734f9b77b83fc466ab857c7688a48dbba45ffcb6ebc7c105c6c028f8e7` |
| Canonical caller manifests | `0f90586306fae9932b88424a16699182e4fc3ecbda47015a63bb3491dbb833c7` |

## Implemented boundaries

- `google-genai==2.20.0` is pinned in `pyproject.toml`.
- Immutable policy resolves baseline and canary model, stage, typed thinking level and ceilings.
- Consumer rollback returns a new registry and changes only the selected consumer's canary model.
- Pricing uses the three approved effective-date schedules and rejects naive, non-UTC, unknown and uncovered lookups.
- Reservation and terminal dataclasses preserve the approved field contracts.
- Reservation identity is a full canonical SHA256. Reservations must write and read back before counting.
- Memory and structured logging sinks enforce append-only reservation then terminal order. The structured sink fixes log name `open-intelligence-gemini-canary` and stores metadata only.
- Joined receipts revalidate structured content. Run reconciliation blocks duplicates, mismatches and orphans. A missing terminal remains `incomplete_uncertain` with `terminal_missing` and no manufactured event.
- The exact 11-task, 23-stage companion is copied byte-for-byte into the canary config and validated on load.
- All claim, requirement, recommendation and section slot formulas reproduce and remain cross-task unique.
- Local request objects use `HttpOptions(api_version="v1")`, typed `ThinkingLevel.MEDIUM` or `HIGH`, JSON schema output and no deprecated sampling or penalty controls.
- Count-token and generation configs use the same system instruction, response schema, MIME type, output ceiling and thinking configuration.
- Three exact prompt modules reproduce every frozen contents string and all six instruction and schema digests.
- Summary, planning and answering callers use local dependency injection, exact manifest inputs, schema validation, membership validation, complete usage metadata and no retry.
- Complete usage preserves the existing `GeminiUsageEvent` fields. Completion tokens equal candidate plus thought tokens.
- Invalid output produces no successful usage event. Missing usage metadata writes a `partial` terminal.

## TDD receipts

RED: the first focused run reported 32 failures for the missing dependency pin, modules, manifest, policies, pricing, receipts, requests, prompts and callers.

GREEN: the settled focused suite reported 47 passed.

Mutation harness: 15 passed. It covers manifest drift, reservation and terminal readback, orphan and missing terminals, contract mismatch, foreign claim and requirement IDs, foreign recommendation and section IDs, unsupported audience lens, causal assertion kind, election review and export, and incomplete telemetry.

Full suite: 3,993 passed with one documented xfail after the settled implementation.

Ruff: repository-wide check passed.

Format: all 16 changed Python files passed. The repository-wide format check also reports 21 unchanged pre-existing files that would be reformatted, so no repository-wide format-clean claim is made.

Request constructor audit: 46 requests, covering all 23 stages in baseline and canary lanes, constructed locally. API version was `v1`, thinking enums were typed, count and generation controls matched, and forbidden control count was zero.

Manifest: 11 tasks, 23 stages and SHA256 `0f90586306fae9932b88424a16699182e4fc3ecbda47015a63bb3491dbb833c7` reproduced.

Compatibility: `src/analysis/gemini_client.py`, `src/contracts/open_intelligence_budget.py` and `src/utils/gemini_usage.py` have no source diff from the base. The unchanged usage-event field tuple is covered directly.

## Review focus

Review the reservation-before-count ordering, terminal error path, structured readback boundary, cumulative planning and answering budget, output ID membership, election gates, manifest file force-add, and the absence of forbidden generation controls.

The local interpreter currently has `google-genai 2.9.0`. It exposes the required typed `ThinkingLevel`, `ThinkingConfig`, `HttpOptions`, `CountTokensConfig`, `GenerationConfig` and `GenerateContentConfig` shapes. The source pin is 2.20.0, but an installed 2.20.0 environment and immutable staging image remain unproven.

## Unproven

No live token count, generation, reservation log, terminal log, usage persistence, billing reconciliation, latency, model quality, stability, blind review, deployment, rollback deployment or promotion proof exists. Logging retention and service-account permissions remain live gates.
