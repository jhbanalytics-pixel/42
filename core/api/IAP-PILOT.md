# IAP single colleague pilot

Status: source preparation only. `F42_AUTH_MODE` defaults to `passcode`, and this work changed no environment, IAM or deployment state. Effective IAP and IAM state and live identity protection remain unproven. The pilot identity remains a private input and is not stored here or in source. `done3.7` remains unproven.

## Scope and current boundary

Pilot one colleague on staging service `ogilvy-trends-v2 / us-central1 / f42-api` with their Google account and exact Ogilvy work address. Add only that address. Confirm actual Google organization membership; email domain alone is not proof. Keep `f42-agent` private with existing service authentication.

The source supports passcode access by default and a prepared `iap_readonly` mode. Health retains its existing fields and adds `auth_mode`, `passcode` and `checks.auth`; its overall `ok` also reflects whether the authentication gate is configured. It reports passcode mode only when `UI_PASSCODE` is set, IAP mode only when `IAP_AUDIENCE` is nonempty and `IAP_ALLOWED_EMAILS` passes exact-email syntax checks, and `unavailable` with `passcode: false` otherwise. That check does not prove the audience matches the deployed service. The current preparation made no environment or deployment changes, so it does not activate IAP.

In IAP mode, protected routes require the signed IAP assertion. Forwarded email and passcode headers do not establish identity. The gate allows protected `GET` and `HEAD` requests and returns `403 read_only_pilot` for declared protected mutations before their handlers run. The shell waits for `POST /api/auth/verify` with an empty JSON object before opening protected reads; the signed assertion is verified by the server, without a passcode body or header. The frontend then shows `Read-only access`; an IAP `401` shows sign-in unavailable with Reload and never falls back to the passcode form. Existing API helpers still attach `X-Passcode`, but the server does not use it in IAP mode.

This is source preparation only. Before activation, complete and review the backend and frontend proofs, complete a fresh harm-only source review, confirm the target service and exact audience by readback, obtain the pilot identity privately and verify organization membership, and record current and proposed IAM bindings. No identity, audience, environment value, IAM change or deployment is included here. Activation still requires Albert's explicit go.

## Prerequisite contract

| Area | Required before apply |
|---|---|
| Identity | Albert supplies the identity privately; omit it here and from the shared board. Confirm organization membership. Default Google OAuth requires membership; otherwise configure a custom OAuth client. |
| IAP assertion | Validate `x-goog-iap-jwt-assertion` signature, payload, exact audience, issuer `https://cloud.google.com/iap`, and expiry. Authorize only the allowlisted identity from the verified assertion. Do not trust an email header alone. |
| Audience | Read back `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME`. Region and service are `us-central1` and `f42-api`; project number and audience are unverified. |
| IAM | L1 prepares only service-resource bindings: `roles/run.invoker` for the IAP service agent and `roles/iap.httpsResourceAccessor` for the pilot. Preserve members. No owner/editor grants, removals, `allUsers`, domain access, or deletion. |
| Endpoint | Enable IAP on `f42-api`; verify the direct `run.app` URL stays protected for every ingress. Leave `f42-agent` unchanged. |

## Readback and acceptance gates

Before apply, L1 records current and proposed service resource, project number, ingress, IAP/OAuth settings, service-agent principal, and additive bindings. Complete and review the local backend and frontend checks before requesting activation. This plan does not authorize apply. After Albert's explicit go, Albert runs the prepared bootstrap and IAM steps in plain PowerShell; L1 owns any explicitly approved deployment. Read back effective config and IAM, confirm existing members remain, and verify the JWT audience against the deployed service. Missing target readback, audience, or private pilot identity blocks apply.

## Blocked inputs

Blocked: target and IAM readbacks; project number and audience; private identity and confirmed organization membership; reviewed source boundary and local proof. An outside-organization account needs custom OAuth. Broader IAP access blocks the one-colleague claim; do not remove existing bindings under this plan.

| Case | Expected result |
|---|---|
| Approved colleague, valid assertion | Google sign-in succeeds; app accepts verified identity for the exact audience. |
| No sign-in or unauthenticated request | Denied before protected content or API data. |
| Unlisted identity | Denied. |
| Forged email header or absent assertion | Rejected by the app. |
| Wrong audience or expired assertion | Rejected by the app. |
| Direct URL, deep link, export, and reload | IAP protects every route and file; reload cannot bypass it. |
| Existing `f42-agent` service flow | Continues through its existing private service authentication. |

Review these results before retiring the passcode. On any failure, stop, keep the stronger gate in place, and do not add a bypass or automatically disable IAP. No model spend or write journey is part of this pilot without a separate go. Never save or print an actual token.

## Sources and limits

The [Cloud Run IAP guide](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run) covers direct IAP on `run.app` and all ingress modes; its default OAuth client is for organization members. External users need custom OAuth. The [signed headers guide](https://docs.cloud.google.com/iap/docs/signed-headers-howto) requires assertion signature, payload, audience, and issuer validation. These docs do not prove deployed state.

Local state is sourced from `core/api/auth.py`, `core/api/app.py` and Albert's IAP decisions. No IAP configuration, IAM state, audience, or pilot identity has been read back for this plan.
