# Proposal: carry the client lens on the investigation frame

Status: **approved by the build lead on 25 Sept 2026, under lead authority, with the conditions below; implemented on branch `feat/42-u02-frame-lens`.** It changes a stored format, so it was approved as a shared contract register addition before any code changed.

## Approval and conditions

The build lead approved this proposal on 25 Sept 2026 on four binding conditions:

1. Failure first tests prove that every existing stored frame, dossier, artifact and export id recomputes byte for byte, from retained fixtures, when there is no lens.
2. A lens outside the stored request's scope, or not enabled, is refused. It does not fall back to anything.
3. This change is its own head on top of PR 85 and is not mixed with U03.
4. This change stays out of the 26 Sept brief and goes into the next integration.

## Implementing commits

* `235b833d` retains the general 42 identities. The repository held one stored investigation id (`app/tests/fixtures/workspaces/investigation_plan_v1.json`) and no stored dossier, artifact or export bytes, so `app/tests/fixtures/frame_lens/general_42_identities.json` captures them at `7d16fd188c8a8e6ceda0cb851ee83d15725368b3`, before any code change. `app/tests/unit/test_frame_lens_general_identity.py` rebuilds every one and compares the bytes (condition 1). It passes before and after the change, and fails eight of its tests against a lens field that the id hashes naively.
* `e675a4c1` adds `app/tests/unit/test_frame_client_lens.py` before the code: 31 failures and 3 errors on that commit.
* `4af9c899` carries the lens on the frame, the stored response, the scope binding and the dossier scope digest, and resolves `client_lens_id` on `POST /api/v2/investigations` before any storage read, refusing a lens of another scope, an unknown lens and a disabled lens as `scope_invalid` with nothing created in their place (condition 2).
* `5695959c` names the lens in the Client Read HTML and the PDF made from it, through the Ask export's own wording: the registry label only for an enabled lens in the scope the investigation was read under, otherwise the stored id. The PDF renderer is not touched.
* `cc9e9940` extends `app/frontend/tests/browser/42-dossier-journey.pw.mjs` with a BSA lensed investigation from creation through review, approval and HTML and PDF export under the bsa_pulse scope, and the cross scope refusal. It fails on `e675a4c1` (the lens is refused as an unknown field).

Not in these commits, and still needed before the writer reaches users: the Build brief bridge sending the answer's lens (`handleBuildBriefFromAsk`), the investigation index projecting the lens for the Console, and a lensed frame proved through the engine dossier producer, which validates frames through the validator its caller injects, and the ops evaluation transport (condition 3 keeps the producer, which U03 is changing, out of this head).

Plan reference: U02 in the 42 product journeys plan ("Bind the client lens and its configuration digest through intake, parent context, plan/result, cache, dossier and artifact version adapters").

## Why

A question now carries its client lens through request, plan, answer, cache and history. `ScopeBinding` holds it beside the seven scope values, and the scope digest folds it in only when a lens is named, so general 42 digests are unchanged (`app/src/api/workspace_scope.py:145` to `:185`). The Fieldwork detail, the Console and the Ask export now show the stored request's own lens.

The last three rows of the boundary table (`app/src/api/workspace_scope.py:62`), investigation, artifact and export, are bound through the investigation frame. `InvestigationFrame` (`app/src/api/investigations.py:62`) has no lens field, and `ScopeBinding.from_frame` (`app/src/api/workspace_scope.py:195`) can therefore only produce a general 42 binding. A brief built from a BSA answer (`handleBuildBriefFromAsk`, `app/frontend/src/ConsoleWorkbench.jsx:438`) becomes a general 42 investigation, and its dossier, artifact and export say nothing of the lens.

## The field

Add one optional field to `InvestigationFrame`, after `output_mode`:

```python
client_lens: Mapping[str, str] | None = None
```

When set, it is exactly the lens binding envelope the question path already uses:

```json
{"lens_binding_version": "client_lens_binding_v1",
 "client_lens_id": "bsa_pulse_lens",
 "configuration_digest": "<64 lowercase hex>"}
```

Validation in `__post_init__`:

* `None` means general 42.
* Otherwise the value has exactly those three keys, the version is `client_lens_binding_v1`, the id is a non empty string and the digest is 64 lowercase hex characters. It is stored as an immutable mapping.
* The frame does not decide authorization. The creation route decides that (below). Reading a stored frame checks only the shape, so a stored investigation stays readable if the registry later disables or repins its lens.

Payload form (`investigation_frame_payload`, `app/src/api/investigations.py:206`): the `client_lens` key appears **only when a lens is named**. `investigation_frame_from_payload` (`:228`) accepts the current 15 key set, or those 15 plus `client_lens` holding a valid envelope. An explicit `"client_lens": null` is refused, so there is exactly one spelling of general 42 and it is the spelling every stored object already has.

## Effect on `investigation_id_for_frame`

`investigation_id_for_frame` (`app/src/api/investigations.py:166`) hashes every dataclass field. Adding a field with a `None` default would add `"client_lens": null` to the canonical JSON and change every existing investigation id. That is not acceptable, because ids name stored objects, pointers, dossier versions and approvals.

Proposed backward compatible derivation:

```python
payload = {f.name: getattr(frame, f.name) for f in fields(InvestigationFrame)
           if f.name != "client_lens"}
if frame.client_lens is not None:
    payload["client_lens"] = dict(frame.client_lens)
```

* A frame with no lens produces exactly the bytes, and therefore the id, it produces today.
* A frame with a lens produces a canonical payload with a key no existing payload has, so it cannot collide with any general 42 id.
* The same decision question under general 42 and under BSA becomes two investigations. That is intended: they run under different configurations.
* The same frame under the same lens and the same configuration digest is idempotent, as today. A repinned configuration digest is a new investigation, matching how a lensed question's identity already treats the lens.

The stored response record (`_validate_plan_ready_response`, `:269`) gains `client_lens` on the same terms: present only when named, checked against the frame.

## Binding through dossier, artifact and export

* **Scope binding.** `ScopeBinding.from_frame` passes `client_lens=frame.client_lens`. General 42 frames keep their current `scope_digest`, so every existing artifact manifest and approval that compares `scope_digest` stays valid. A lensed frame gets a different `scope_digest`, so an approval recorded under one lens can never match an artifact bound under another (`is_artifact_approved`, `app/src/api/dossier_artifacts.py:75`, already compares `scope_digest` exactly).
* **Creation.** `POST /api/v2/investigations` (`app/src/api/main.py:804`) accepts an optional `client_lens_id`. It is resolved with `client_lenses.resolve_client_lens` under the resolved scope, the same way `/api/chat/send` resolves it, before any storage read. An unauthorized or unknown lens is refused as `404 scope_invalid`, the refusal the route already uses for scope. Authorization stays with `client_scope_id`; a lens never widens it.
* **Ask to brief.** The Build brief bridge sends the lens of the answer it starts from. For a reopened answer that is the stored request's lens, which the Fieldwork detail now carries.
* **Dossier.** The dossier record's `frame` is the frame payload, so it carries the lens when named, and `dossier_store` (`app/src/api/dossier_store.py:284`) keeps recomputing the id from that frame. A record whose lens was altered fails as `dossier_identity_mismatch` with no new check.
* **Artifact and export.** The manifest's `scope_digest` binds the lens through `from_frame`. The Client Read HTML, and the PDF made from that same HTML, name the lens in the same words as the Ask export ("Client lens: Brand South Africa Pulse" and the configuration line) **only when a lens is named**, so general 42 export bytes and any pinned digests are unchanged. The PDF renderer is not touched.
* **Index.** `investigation_index` (`app/src/api/investigation_index.py:400`) keeps filtering by scope. It projects the lens so the Console can show it. It does not filter by the page's selected lens.

## Migration of stored objects

Storage is append only on GCS. Nothing is rewritten, moved or backfilled.

* Every stored investigation, dossier version, pointer, artifact manifest and review decision has no `client_lens` key. Under this proposal they read as general 42, which is what they are: no lens could be named on an investigation when they were written. Their ids, digests and approvals stay valid byte for byte.
* New lensed investigations are new objects under new ids.
* Deployment order: ship the reader first (accept the optional key everywhere a frame, response or dossier is read, including the engine dossier producer), then the writer (the creation route and the bridge). Rolling back the writer leaves every object written by it readable. Rolling back the reader is only safe while no lensed object exists.
* Downstream readers of the frame grammar need the same optional key before the writer ships: the engine dossier producer, the ops evaluation transport and anything listed in `docs/operations/downstream-compatibility.md`.

## Tests that would prove it

1. Identity pin: for every stored investigation fixture in the repo, `investigation_id_for_frame` returns the id it returned before the change, and the frame payload bytes are identical.
2. A named lens changes the id; a different configuration digest changes it again; the same lens and digest reproduce it.
3. Frame payload round trip with and without a lens; `investigation_frame_from_payload` refuses an explicit null, a missing key, an extra key, a wrong version, an empty id and an uppercase or short digest.
4. `ScopeBinding.from_frame`: general 42 digest equals the pinned current digest; a lensed frame's digest differs.
5. Creation route: BSA under the `bsa_pulse` scope creates a lensed investigation; BSA under `ogilvy_default`, an unknown lens and a disabled lens are refused before any storage read; the same body twice under one lens returns one id; the same body under two lenses returns two.
6. Dossier store: a lensed dossier validates and cold reads; the same record with its frame lens changed fails as `dossier_identity_mismatch`.
7. Artifact approval: an approval under a lensed `scope_digest` does not approve the same artifact version under the general binding, and the reverse.
8. Export: the Client Read HTML and PDF name the lens only when named; a general 42 export's bytes equal the pinned bytes.
9. Index and cross scope: a lensed investigation is listed with its lens under its own scope and refused under another scope before a private read, with a same scope positive control.
10. Browser: Build brief from a BSA answer opens a BSA investigation; reload and reopen keep the lens; the approved export names it; a general 42 brief from the same question text stays general.
11. Cold read of a copy of existing staging objects under the new reader, unchanged.
