"""The dossier review routes: login binding, same origin, CSRF, the digest check and the state pointer."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import PreconditionFailed
from src.api import (
    dossier_approval,
    dossier_artifacts,
    dossier_resolver,
    dossier_review_store,
    dossier_store,
    intelligence_dossier,
    investigations,
    main,
    workspace_scope,
)
from tests.unit import dossier_pdf_fakes
from tests.unit.test_dossier_resolver import (
    RECEIPTS,
    claim_record,
    dossier,
    resolved_scope,
)
from tests.unit.test_dossier_store import (
    PREFIX,
    body_of,
    publication,
    rewrite_top_payload,
    top_payload,
)
from tests.unit.object_creator_bucket import listed_names

AUDIENCE = "review-client.apps.test"
ISSUER = "https://accounts.google.com"
HOSTED_DOMAIN = "ogilvy.test"
SUBJECTS = {
    "editor": "100000000000000000001",
    "claims": "100000000000000000002",
    "claims_two": "100000000000000000003",
    "relationships": "100000000000000000004",
    "client": "100000000000000000005",
    "stranger": "100000000000000000006",
    "editor_two": "100000000000000000007",
}
ALLOWLIST = {
    SUBJECTS["editor"]: "dossier_editor",
    SUBJECTS["claims"]: "claim_approver",
    SUBJECTS["claims_two"]: "claim_approver",
    SUBJECTS["relationships"]: "relationship_approver",
    SUBJECTS["client"]: "client_read_approver",
    SUBJECTS["editor_two"]: "dossier_editor",
}
NOW = 1_800_000_000
RECORDED = datetime(2026, 9, 13, 8, 0, 0, tzinfo=UTC)
ORIGIN = {"Origin": "https://testserver"}
LOGIN = "/api/internal/v2/dossier/review/login"
SESSION = "/api/internal/v2/dossier/review/session"


# A generation-aware bucket double
#
# The shape the review store tests use, plus one-shot hooks that run another
# writer in the middle of an upload, which is how a race is made deterministic.


class ReadBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        data, generation = bucket.objects[name]
        self.generation = generation
        self.size = len(data)

    def download_as_bytes(self, *, if_generation_match=None, **kwargs):
        current = self.bucket.objects.get(self.name)
        if current is None:
            raise PreconditionFailed("gone")
        data, generation = current
        if if_generation_match is not None and if_generation_match != generation:
            raise PreconditionFailed("generation moved")
        return data


class WriteBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.generation = None

    def upload_from_string(self, data, *, content_type, if_generation_match):
        for index, (matches, callback) in enumerate(self.bucket.before_upload):
            if matches(self.name):
                del self.bucket.before_upload[index]
                callback()
                break
        self.bucket.uploads.append((self.name, if_generation_match))
        assert content_type == (
            "application/pdf" if self.name.endswith(".pdf") else "application/json"
        )
        current = self.bucket.objects.get(self.name)
        current_generation = current[1] if current is not None else 0
        if if_generation_match != current_generation:
            raise PreconditionFailed("precondition")
        if self.name in self.bucket.failing:
            raise RuntimeError("storage unavailable")
        self.bucket.counter += 1
        self.bucket.objects[self.name] = (data, self.bucket.counter)
        self.generation = self.bucket.counter


class Bucket:
    name = "listening-post-staging-cache"

    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.counter = 100
        self.failing = set()
        self.read_failing = False
        self.list_failing = False
        self.before_upload = []

    def blob(self, name):
        return WriteBlob(self, name)

    def get_blob(self, name):
        if self.read_failing:
            raise RuntimeError("storage unavailable")
        return ReadBlob(self, name) if name in self.objects else None

    def list_blobs(self, *, prefix, max_results=None, delimiter=None):
        if self.list_failing:
            raise RuntimeError("listing unavailable")
        return [
            ReadBlob(self, name)
            for name in listed_names(self.objects, prefix, max_results, delimiter)
        ]

    def replace(self, name, data):
        self.counter += 1
        self.objects[name] = (data, self.counter)


# Fixtures


@pytest.fixture(autouse=True)
def review_environment(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.setenv("REVIEW_OAUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("REVIEW_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("REVIEW_HOSTED_DOMAIN", HOSTED_DOMAIN)
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", json.dumps(ALLOWLIST))
    clock = {"now": NOW}
    monkeypatch.setattr(main, "_review_now", lambda: clock["now"])
    monkeypatch.setattr(main, "_review_recorded_at", lambda: RECORDED)
    main._review_logins.clear()
    main._review_sessions.clear()
    main._review_bound_nonces.clear()
    yield clock
    main._review_logins.clear()
    main._review_sessions.clear()
    main._review_bound_nonces.clear()


def scope_missing(investigation_id):
    raise workspace_scope.WorkspaceScopeError(
        404, "scope_invalid", "Workspace is unavailable for this scope."
    )


@pytest.fixture
def workspace(monkeypatch):
    bucket = Bucket()
    value = dossier()
    created = dossier_store.create_dossier(
        bucket=bucket,
        prefix=PREFIX,
        body=body_of(value),
        publication=publication(),
        pointer_writer=lambda pointer: dossier_store.publish_pointer(
            pointer, bucket=bucket, prefix=PREFIX
        ),
    )
    scope = resolved_scope(value)
    dossier_pdf_fakes.install(monkeypatch)
    monkeypatch.setattr(main, "_investigation_bucket", lambda: bucket)
    monkeypatch.setattr(
        workspace_scope,
        "resolve_workspace_scope",
        lambda investigation_id: (
            scope
            if investigation_id == scope.investigation_id
            else scope_missing(investigation_id)
        ),
    )
    return SimpleNamespace(
        bucket=bucket,
        dossier=value,
        scope=scope,
        version=created["publication"]["dossier_version"],
        baseline=list(bucket.uploads),
        review=f"/api/internal/v2/investigations/{scope.investigation_id}/dossier/review",
    )


def make_bsa_workspace(monkeypatch, *, prohibited: bool):
    bucket = Bucket()
    value = dossier()
    bsa_frame = replace(resolved_scope(value).frame, brand_config_id="bsa")
    value["investigation_id"] = investigations.investigation_id_for_frame(bsa_frame)
    value["frame"] = investigations.investigation_frame_payload(bsa_frame)
    value["scope"] = {
        field: value["frame"][field]
        for field in dossier_store.SCOPE_FIELDS
    }
    if prohibited:
        value["admitted_answer"]["response"]["intelligence"]["claims"][0]["text"] = (
            "Election messaging reached undecided voters in Durban."
        )
    created = dossier_store.create_dossier(
        bucket=bucket,
        prefix=PREFIX,
        body=body_of(value),
        publication=publication(),
        pointer_writer=lambda pointer: dossier_store.publish_pointer(
            pointer, bucket=bucket, prefix=PREFIX
        ),
    )
    scope = workspace_scope.ResolvedWorkspaceScope(
        investigation_id=value["investigation_id"],
        frame=bsa_frame,
        response={},
        scope_digest=dossier_store.canonical_digest(value["scope"]),
    )
    dossier_pdf_fakes.install(monkeypatch)
    monkeypatch.setattr(main, "_investigation_bucket", lambda: bucket)
    monkeypatch.setattr(
        workspace_scope,
        "resolve_workspace_scope",
        lambda investigation_id: (
            scope if investigation_id == scope.investigation_id else scope_missing(investigation_id)
        ),
    )
    return SimpleNamespace(
        bucket=bucket,
        dossier=value,
        scope=scope,
        version=created["publication"]["dossier_version"],
        baseline=list(bucket.uploads),
        review=f"/api/internal/v2/investigations/{scope.investigation_id}/dossier/review",
    )


def client():
    return TestClient(main.app, base_url="https://testserver")


def bind(browser, monkeypatch, subject, *, claims=None, nonce=None, csrf=None):
    start = browser.post(LOGIN, headers=ORIGIN)
    assert start.status_code == 200, start.text
    issued = start.json()
    presented = {
        "aud": AUDIENCE,
        "iss": ISSUER,
        "exp": NOW + 60,
        "sub": subject,
        "hd": HOSTED_DOMAIN,
        "nonce": issued["nonce"] if nonce is None else nonce,
    }
    presented.update(claims or {})
    monkeypatch.setattr(
        main, "_verify_review_credential", lambda credential, audience: dict(presented)
    )
    return browser.post(
        SESSION,
        json={
            "credential": "signed-credential",
            "g_csrf_token": issued["csrf_token"] if csrf is None else csrf,
        },
        headers=ORIGIN,
    )


def reviewer(monkeypatch, subject):
    browser = client()
    bound = bind(browser, monkeypatch, subject)
    assert bound.status_code == 200, bound.text
    return browser, {**ORIGIN, "X-Review-CSRF": bound.json()["csrf_token"]}


def claim_command(ws, claim_id="clm_1", **overrides):
    value = {
        "contract_version": "dossier_review_v1",
        "dossier_version": ws.version,
        "resource": "claim",
        "resource_id": claim_id,
        "resource_version": dossier_store.canonical_digest(
            claim_record(ws.dossier, claim_id)
        ),
        "action": "approve",
        "expected_state": "pending_review",
        "idempotency_key": "idem-" + claim_id,
        "support_review": {
            "verdict": "supported",
            "receipt_ids": [RECEIPTS[0]],
            "note": "checked",
        },
    }
    value.update(overrides)
    return value


def relationship_command(ws, parent="clm_1", child="clm_2", **overrides):
    value = claim_command(
        ws,
        resource="relationship",
        resource_id=dossier_resolver.relationship_id(parent, child),
        resource_version=dossier_store.canonical_digest(
            {"claim_id": child, "parent_claim_id": parent}
        ),
        idempotency_key="idem-rel-" + child,
        support_review=None,
    )
    value.update(overrides)
    return value


def pointer_directory(ws):
    return dossier_review_store.state_pointer_directory(
        PREFIX, ws.scope.investigation_id, ws.version
    )


def pointer_name(ws, sequence=1):
    return dossier_review_store.state_pointer_entry_name(
        PREFIX, ws.scope.investigation_id, ws.version, sequence
    )


def stored_pointer(ws):
    return top_payload(ws.bucket, pointer_directory(ws))


def rewrite_pointer(ws, payload):
    """Test only: make the top state entry hold payload, planting entry 1 in an empty log."""
    names = sorted(name for name in ws.bucket.objects if name.startswith(pointer_directory(ws)))
    if names:
        rewrite_top_payload(ws.bucket, pointer_directory(ws), payload)
        return
    entry = {
        "contract_version": "dossier_review_state_entry_v1",
        "sequence": 1,
        "predecessor_sequence": None,
        "predecessor_sha256": None,
        "pointer": payload,
    }
    ws.bucket.replace(pointer_name(ws), dossier_store._canonical(entry))


def stored_artifact(ws):
    client_read = intelligence_dossier.read_dossier(
        ws.scope.investigation_id,
        dossier_resolver.projection_record(ws.dossier, []),
    )
    artifact = dossier_artifacts.build_artifact(
        investigation_id=ws.scope.investigation_id,
        scope_digest=ws.scope.scope_digest,
        dossier_version=ws.version,
        selected_claim_ids=[],
        client_read=client_read,
        format="html",
    )
    dossier_artifacts.create_artifact(bucket=ws.bucket, prefix=PREFIX, artifact=artifact)
    return artifact


def detail(response):
    return response.json()["detail"]


def cookie_flags(response, name):
    header = next(
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(name + "=")
    )
    return header.lower()


# Login and session binding


def test_the_login_issues_a_nonce_and_a_secure_csrf_cookie_on_the_same_origin():
    browser = client()
    start = browser.post(LOGIN, headers=ORIGIN)
    assert start.status_code == 200
    issued = start.json()
    assert set(issued) == {"contract_version", "nonce", "csrf_token", "expires_in"}
    assert issued["contract_version"] == "dossier_review_login_v1"
    flags = cookie_flags(start, "g_csrf_token")
    assert "secure" in flags
    assert "httponly" in flags
    assert "samesite=strict" in flags
    assert issued["csrf_token"] in start.headers.get_list("set-cookie")[0]
    cross = client().post(LOGIN, headers={"Origin": "https://evil.test"})
    assert (cross.status_code, detail(cross)["code"]) == (400, "review_request_invalid")


def test_a_verified_credential_binds_one_session_with_secure_cookies(monkeypatch):
    browser = client()
    bound = bind(browser, monkeypatch, SUBJECTS["claims"])
    assert bound.status_code == 200, bound.text
    assert set(bound.json()) == {
        "contract_version",
        "role",
        "roles",
        "csrf_token",
        "expires_at",
    }
    assert bound.json()["roles"] == ["claim_approver"]
    assert bound.json()["contract_version"] == "dossier_review_session_v1"
    assert bound.json()["role"] == "claim_approver"
    assert bound.json()["expires_at"] == NOW + main.REVIEW_SESSION_MAX_AGE_SECONDS
    flags = cookie_flags(bound, "review_session")
    assert "secure" in flags
    assert "httponly" in flags
    assert "samesite=strict" in flags
    session = next(iter(main._review_sessions.values()))
    assert session["principal"] == {
        "principal_ref": "human:"
        + main.hashlib.sha256(SUBJECTS["claims"].encode("utf-8")).hexdigest()[:8],
        "role": "claim_approver",
    }
    assert "sub" not in json.dumps(session)
    assert SUBJECTS["claims"] not in json.dumps(session)


@pytest.mark.parametrize(
    ("settings", "status", "reason"),
    [
        ({"nonce": "another-nonce"}, 400, "login_mismatch"),
        ({"csrf": "another-token"}, 400, "csrf_mismatch"),
        ({"claims": {"aud": "someone-else"}}, 400, "audience_mismatch"),
        ({"claims": {"exp": NOW - 1}}, 400, "token_expired"),
        ({"claims": {"hd": None}}, 403, "hosted_domain_mismatch"),
        ({"claims": {"sub": SUBJECTS["stranger"]}}, 403, "subject_not_allowlisted"),
    ],
)
def test_a_credential_outside_the_binding_rules_is_refused(
    monkeypatch, settings, status, reason
):
    browser = client()
    refused = bind(browser, monkeypatch, SUBJECTS["claims"], **settings)
    assert refused.status_code == status, refused.text
    assert detail(refused)["message"] == reason
    assert main._review_sessions == {}
    assert "review_session" not in refused.headers.get("set-cookie", "")


def test_an_expired_or_replayed_login_is_refused(monkeypatch, review_environment):
    browser = client()
    start = browser.post(LOGIN, headers=ORIGIN)
    issued = start.json()
    presented = {
        "aud": AUDIENCE,
        "iss": ISSUER,
        "exp": NOW + 10_000,
        "sub": SUBJECTS["claims"],
        "hd": HOSTED_DOMAIN,
        "nonce": issued["nonce"],
    }
    monkeypatch.setattr(
        main, "_verify_review_credential", lambda credential, audience: presented
    )
    body = {"credential": "signed-credential", "g_csrf_token": issued["csrf_token"]}
    review_environment["now"] = NOW + main.REVIEW_LOGIN_MAX_AGE_SECONDS + 1
    late = browser.post(SESSION, json=body, headers=ORIGIN)
    assert (late.status_code, detail(late)["message"]) == (400, "login_expired")
    review_environment["now"] = NOW
    bound = browser.post(SESSION, json=body, headers=ORIGIN)
    assert bound.status_code == 200
    again = client()
    again.cookies.set("g_csrf_token", issued["csrf_token"])
    replayed = again.post(SESSION, json=body, headers=ORIGIN)
    assert replayed.status_code == 400
    assert detail(replayed)["message"] in {"login_replayed", "login_unknown"}


def test_the_credential_return_needs_the_csrf_cookie_and_a_signed_credential(
    monkeypatch,
):
    browser = client()
    issued = browser.post(LOGIN, headers=ORIGIN).json()
    body = {"credential": "signed-credential", "g_csrf_token": issued["csrf_token"]}
    bare = client()
    missing = bare.post(SESSION, json=body, headers=ORIGIN)
    assert (missing.status_code, detail(missing)["message"]) == (400, "csrf_missing")

    def broken(credential, audience):
        raise ValueError("signature")

    monkeypatch.setattr(main, "_verify_review_credential", broken)
    unsigned = browser.post(SESSION, json=body, headers=ORIGIN)
    assert (unsigned.status_code, detail(unsigned)["message"]) == (
        400,
        "credential_invalid",
    )
    malformed = browser.post(SESSION, json={"credential": "x"}, headers=ORIGIN)
    assert malformed.status_code == 400


def test_missing_configuration_closes_review_before_any_login(monkeypatch):
    monkeypatch.delenv("REVIEW_SUBJECT_ALLOWLIST")
    closed = client().post(LOGIN, headers=ORIGIN)
    assert (closed.status_code, detail(closed)["code"]) == (
        503,
        "review_authority_unavailable",
    )
    assert main._review_authority().available is False
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", json.dumps(ALLOWLIST))
    assert main._review_authority().available is True
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", "{}")
    assert main._review_authority().available is False


# The review command: refusals before storage


def untouched_storage(monkeypatch):
    def storage(*args, **kwargs):
        raise AssertionError("storage was read")

    monkeypatch.setattr(main, "_investigation_bucket", storage)
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", storage)


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.test"},
        {"Origin": "https://testserver.evil.test"},
        {"Origin": "http://testserver"},
        {},
        {"Origin": "https://testserver", "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_origin_review_is_refused_before_storage(monkeypatch, headers):
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    untouched_storage(monkeypatch)
    sent = {**headers, "X-Review-CSRF": ready["X-Review-CSRF"]}
    refused = browser.post(
        "/api/internal/v2/investigations/inv_x/dossier/review", json={}, headers=sent
    )
    assert (refused.status_code, detail(refused)["code"]) == (
        400,
        "review_request_invalid",
    )
    assert detail(refused)["message"] == "origin_mismatch"


def test_missing_or_wrong_csrf_is_refused_before_storage(monkeypatch):
    browser, _ready = reviewer(monkeypatch, SUBJECTS["claims"])
    untouched_storage(monkeypatch)
    url = "/api/internal/v2/investigations/inv_x/dossier/review"
    missing = browser.post(url, json={}, headers=ORIGIN)
    assert (missing.status_code, detail(missing)["message"]) == (400, "csrf_missing")
    wrong = browser.post(url, json={}, headers={**ORIGIN, "X-Review-CSRF": "x" * 43})
    assert (wrong.status_code, detail(wrong)["message"]) == (400, "csrf_mismatch")


def test_passcode_only_and_service_identities_are_refused_before_storage(monkeypatch):
    untouched_storage(monkeypatch)
    url = "/api/internal/v2/investigations/inv_x/dossier/review"
    passcode_only = client().post(
        url, json={}, headers={**ORIGIN, "X-Passcode": "s3cret"}
    )
    assert (passcode_only.status_code, detail(passcode_only)["code"]) == (
        403,
        "review_role_required",
    )
    service = client().post(
        url,
        json={},
        headers={
            **ORIGIN,
            "Authorization": "Bearer aaaa.bbbb.cccc",
            "X-Review-CSRF": "x" * 43,
        },
    )
    assert (service.status_code, detail(service)["code"]) == (
        403,
        "review_role_required",
    )
    forged = client()
    forged.cookies.set("review_session", "forged-session-id")
    fixation = forged.post(url, json={}, headers={**ORIGIN, "X-Review-CSRF": "x" * 43})
    assert (fixation.status_code, detail(fixation)["code"]) == (
        403,
        "review_role_required",
    )


def test_an_expired_session_is_refused_before_storage(monkeypatch, review_environment):
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    untouched_storage(monkeypatch)
    review_environment["now"] = NOW + main.REVIEW_SESSION_MAX_AGE_SECONDS + 1
    refused = browser.post(
        "/api/internal/v2/investigations/inv_x/dossier/review", json={}, headers=ready
    )
    assert (refused.status_code, detail(refused)["code"]) == (
        403,
        "review_role_required",
    )
    assert main._review_sessions == {}


@pytest.mark.parametrize(
    "broken",
    [
        {"principal_ref": "human:5f2c9a1b"},
        {"contract_version": "intelligence_dossier_v1"},
        {"resource": "dossier"},
        {"resource_version": "c" * 63},
        {"support_review": None},
    ],
)
def test_a_command_outside_the_registered_shape_is_refused_before_storage(
    monkeypatch, workspace, broken
):
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    command = claim_command(workspace) | broken
    untouched_storage(monkeypatch)
    refused = browser.post(workspace.review, json=command, headers=ready)
    assert (refused.status_code, detail(refused)["code"]) == (
        400,
        "review_request_invalid",
    )
    body = browser.post(
        workspace.review,
        content=b"[]",
        headers={**ready, "Content-Type": "application/json"},
    )
    assert body.status_code == 400


# The review command: the positive path and the storage-side refusals


def test_a_valid_command_records_one_decision_and_advances_the_state_pointer(
    monkeypatch, workspace
):
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    command = claim_command(workspace)
    approved = browser.post(workspace.review, json=command, headers=ready)
    assert approved.status_code == 200, approved.text
    result = approved.json()
    assert tuple(result) == (
        "contract_version",
        "investigation_id",
        "dossier_version",
        "decision_id",
        "state",
    )
    assert result["contract_version"] == "dossier_review_v1"
    assert result["investigation_id"] == workspace.scope.investigation_id
    assert result["dossier_version"] == workspace.version
    assert result["state"] == "approved"
    decision_name = (
        f"{PREFIX}decisions/{workspace.scope.investigation_id}/{workspace.version}/"
        f"{result['decision_id']}.json"
    )
    decision = dossier_review_store.parse_decision(
        json.loads(workspace.bucket.objects[decision_name][0])
    )
    assert (
        decision["principal_ref"]
        == "human:"
        + main.hashlib.sha256(SUBJECTS["claims"].encode("utf-8")).hexdigest()[:8]
    )
    assert decision["resource_version"] == command["resource_version"]
    pointer = stored_pointer(workspace)
    assert pointer == {
        "contract_version": "dossier_review_state_v1",
        "investigation_id": workspace.scope.investigation_id,
        "dossier_version": workspace.version,
        "resources": {
            "claim:clm_1": {
                "state": "approved",
                "resource_version": command["resource_version"],
                "applied": [result["decision_id"]],
            }
        },
    }
    new_uploads = workspace.bucket.uploads[len(workspace.baseline) :]
    assert new_uploads == [(decision_name, 0), (pointer_name(workspace), 0)]
    assert approved.headers["cache-control"] == "private, no-store"
    replay = browser.post(workspace.review, json=command, headers=ready)
    assert replay.status_code == 200
    assert replay.json() == result
    assert workspace.bucket.uploads[len(workspace.baseline) :] == new_uploads
    moved = browser.post(
        workspace.review,
        json=command | {"idempotency_key": "idem-again"},
        headers=ready,
    )
    assert (moved.status_code, detail(moved)["code"]) == (
        409,
        "review_version_conflict",
    )


def test_a_resource_version_that_is_not_the_content_digest_is_refused_before_any_write(
    monkeypatch, workspace
):
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    wrong = claim_command(workspace, resource_version="e" * 64)
    refused = browser.post(workspace.review, json=wrong, headers=ready)
    assert (refused.status_code, detail(refused)["code"]) == (
        409,
        "review_version_conflict",
    )
    assert detail(refused)["message"] == "resource_version_mismatch"
    assert workspace.bucket.uploads == workspace.baseline
    assert pointer_name(workspace) not in workspace.bucket.objects
    unknown = claim_command(workspace, resource_id="clm_404")
    refused = browser.post(workspace.review, json=unknown, headers=ready)
    assert (refused.status_code, detail(refused)["message"]) == (
        409,
        "resource_version_mismatch",
    )
    internal = claim_command(workspace, "clm_8")
    refused = browser.post(workspace.review, json=internal, headers=ready)
    assert (refused.status_code, detail(refused)["message"]) == (
        409,
        "resource_version_mismatch",
    )
    assert workspace.bucket.uploads == workspace.baseline


@pytest.mark.parametrize(
    ("subject", "action", "expected_state"),
    [
        ("editor", "submit", "draft"),
        ("client", "approve", "pending_review"),
        ("client", "reject", "pending_review"),
    ],
)
def test_bsa_review_refuses_every_artifact_transition_without_moving_the_pointer(
    monkeypatch, subject, action, expected_state
):
    workspace = make_bsa_workspace(monkeypatch, prohibited=True)
    artifact = stored_artifact(workspace)
    baseline = list(workspace.bucket.uploads)
    browser, ready = reviewer(monkeypatch, SUBJECTS[subject])
    command = {
        "contract_version": "dossier_review_v1",
        "dossier_version": workspace.version,
        "resource": "artifact",
        "resource_id": artifact["artifact_id"],
        "resource_version": artifact["artifact_version"],
        "action": action,
        "expected_state": expected_state,
        "idempotency_key": f"bsa-{action}",
        "support_review": None,
    }

    refused = browser.post(workspace.review, json=command, headers=ready)

    assert (refused.status_code, detail(refused)["code"]) == (
        403,
        "client_purpose_review_override_refused",
    )
    assert workspace.bucket.uploads == baseline
    assert pointer_name(workspace) not in workspace.bucket.objects


def test_bsa_review_allows_a_permitted_artifact_transition(monkeypatch):
    workspace = make_bsa_workspace(monkeypatch, prohibited=False)
    artifact = stored_artifact(workspace)
    browser, ready = reviewer(monkeypatch, SUBJECTS["editor"])
    command = {
        "contract_version": "dossier_review_v1",
        "dossier_version": workspace.version,
        "resource": "artifact",
        "resource_id": artifact["artifact_id"],
        "resource_version": artifact["artifact_version"],
        "action": "submit",
        "expected_state": "draft",
        "idempotency_key": "bsa-permitted-submit",
        "support_review": None,
    }

    accepted = browser.post(workspace.review, json=command, headers=ready)

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "pending_review"


def test_the_relationship_digest_is_projected_the_same_way(monkeypatch, workspace):
    browser, ready = reviewer(monkeypatch, SUBJECTS["relationships"])
    wrong = relationship_command(workspace, resource_version="e" * 64)
    refused = browser.post(workspace.review, json=wrong, headers=ready)
    assert (refused.status_code, detail(refused)["message"]) == (
        409,
        "resource_version_mismatch",
    )
    assert workspace.bucket.uploads == workspace.baseline
    approved = browser.post(
        workspace.review, json=relationship_command(workspace), headers=ready
    )
    assert approved.status_code == 200, approved.text
    key = "relationship:" + dossier_resolver.relationship_id("clm_1", "clm_2")
    assert stored_pointer(workspace)["resources"][key]["state"] == "approved"
    versions = dossier_resolver.resource_versions(workspace.dossier)
    assert set(versions) == {"claim", "relationship"}
    assert set(versions["claim"]) == {"clm_1", "clm_2", "clm_3", "clm_4", "clm_5"}
    assert versions["relationship"] == {
        dossier_resolver.relationship_id(
            "clm_1", "clm_2"
        ): dossier_store.canonical_digest(
            {"claim_id": "clm_2", "parent_claim_id": "clm_1"}
        ),
        dossier_resolver.relationship_id(
            "clm_2", "clm_3"
        ): dossier_store.canonical_digest(
            {"claim_id": "clm_3", "parent_claim_id": "clm_2"}
        ),
    }


def test_the_wrong_role_and_a_stale_version_are_refused_without_writes(
    monkeypatch, workspace
):
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    role = browser.post(
        workspace.review, json=relationship_command(workspace), headers=ready
    )
    assert (role.status_code, detail(role)["code"]) == (403, "review_role_required")
    stale = browser.post(
        workspace.review,
        json=claim_command(workspace, dossier_version="f" * 64),
        headers=ready,
    )
    assert (stale.status_code, detail(stale)["code"]) == (
        409,
        "review_version_conflict",
    )
    assert workspace.bucket.uploads == workspace.baseline


def test_scope_and_storage_refusals_keep_the_registered_codes(monkeypatch, workspace):
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    other = browser.post(
        "/api/internal/v2/investigations/inv_other/dossier/review",
        json=claim_command(workspace),
        headers=ready,
    )
    assert (other.status_code, detail(other)["code"]) == (404, "scope_invalid")
    malformed = browser.post(
        "/api/internal/v2/investigations/not-an-id/dossier/review",
        json=claim_command(workspace),
        headers=ready,
    )
    assert (malformed.status_code, detail(malformed)["code"]) == (404, "scope_invalid")
    workspace.bucket.read_failing = True
    down = browser.post(workspace.review, json=claim_command(workspace), headers=ready)
    assert (down.status_code, detail(down)["code"]) == (
        503,
        "review_storage_unavailable",
    )
    workspace.bucket.read_failing = False
    workspace.bucket.failing.add(pointer_name(workspace))
    down = browser.post(workspace.review, json=claim_command(workspace), headers=ready)
    assert (down.status_code, detail(down)["code"]) == (
        503,
        "review_storage_unavailable",
    )
    monkeypatch.delenv("REVIEW_SUBJECT_ALLOWLIST")
    closed = browser.post(
        workspace.review, json=claim_command(workspace), headers=ready
    )
    assert (closed.status_code, detail(closed)["code"]) == (
        503,
        "review_authority_unavailable",
    )


# The race the state pointer settles


def test_two_writers_cannot_both_advance_the_state_pointer(monkeypatch, workspace):
    first, ready_first = reviewer(monkeypatch, SUBJECTS["claims"])
    second, ready_second = reviewer(monkeypatch, SUBJECTS["claims_two"])
    approve = claim_command(workspace, idempotency_key="idem-first")
    reject = claim_command(
        workspace,
        action="reject",
        idempotency_key="idem-second",
        support_review={"verdict": "unsupported", "receipt_ids": [], "note": "no"},
    )
    directory = (
        f"{PREFIX}decisions/{workspace.scope.investigation_id}/{workspace.version}/"
    )
    outcomes = {}

    def second_writer():
        # Runs after the first writer passed every check and is about to store
        # its decision, so both were told the resource was pending review.
        outcomes["second"] = second.post(
            workspace.review, json=reject, headers=ready_second
        )

    workspace.bucket.before_upload.append(
        (
            lambda name: (
                name.startswith(directory) and "/pointer/" not in name
            ),
            second_writer,
        )
    )
    outcomes["first"] = first.post(workspace.review, json=approve, headers=ready_first)
    assert outcomes["second"].status_code == 200, outcomes["second"].text
    assert outcomes["first"].status_code == 409
    assert detail(outcomes["first"])["code"] == "review_version_conflict"
    winner = outcomes["second"].json()["decision_id"]
    entry = stored_pointer(workspace)["resources"]["claim:clm_1"]
    assert entry["state"] == "rejected"
    assert entry["applied"] == [winner]
    stored = {
        name[len(directory) : -len(".json")]
        for name in workspace.bucket.objects
        if name.startswith(directory) and "/pointer/" not in name
    }
    assert winner in stored
    assert len(stored) == 2
    retry = first.post(workspace.review, json=approve, headers=ready_first)
    assert (retry.status_code, detail(retry)["code"]) == (
        409,
        "review_version_conflict",
    )
    assert stored_pointer(workspace)["resources"]["claim:clm_1"] == entry


def test_the_pointer_write_is_preconditioned_on_the_generation_it_read(workspace):
    bucket = workspace.bucket
    investigation_id = workspace.scope.investigation_id
    principal = "human:5f2c9a1b"

    def decision(claim_id, key):
        return dossier_review_store.build_decision(
            investigation_id=investigation_id,
            dossier_version=workspace.version,
            resource="claim",
            resource_id=claim_id,
            resource_version=dossier_store.canonical_digest(
                claim_record(workspace.dossier, claim_id)
            ),
            action="approve",
            expected_state="pending_review",
            idempotency_key=key,
            support_review={"verdict": "supported", "receipt_ids": [], "note": ""},
            principal_ref=principal,
            recorded_at="2026-09-13T08:00:00.000000Z",
        )

    settings = {
        "bucket": bucket,
        "prefix": PREFIX,
        "investigation_id": investigation_id,
        "dossier_version": workspace.version,
    }
    current = dossier_review_store.read_state_pointer(**settings)
    assert current == {
        "pointer": {
            "contract_version": "dossier_review_state_v1",
            "investigation_id": investigation_id,
            "dossier_version": workspace.version,
            "resources": {},
        },
        "generation": 0,
    }
    other = decision("clm_2", "idem-other")
    bucket.before_upload.append(
        (
            lambda name: "/pointer/" in name,
            lambda: dossier_review_store.advance_state_pointer(
                bucket=bucket, prefix=PREFIX, decision=other
            ),
        )
    )
    mine = decision("clm_1", "idem-mine")
    with pytest.raises(dossier_store.DossierConflict) as caught:
        dossier_review_store.advance_state_pointer(
            bucket=bucket, prefix=PREFIX, decision=mine, current=current
        )
    assert caught.value.code == "state_pointer_generation_moved"
    advanced = dossier_review_store.read_state_pointer(**settings)
    assert set(advanced["pointer"]["resources"]) == {"claim:clm_2"}
    assert advanced["generation"] > 0
    applied = dossier_review_store.advance_state_pointer(
        bucket=bucket, prefix=PREFIX, decision=mine
    )
    assert applied["applied"] is True
    again = dossier_review_store.advance_state_pointer(
        bucket=bucket, prefix=PREFIX, decision=mine
    )
    assert again["applied"] is False
    assert again["pointer"] == applied["pointer"]
    with pytest.raises(dossier_store.DossierConflict):
        dossier_review_store.advance_state_pointer(
            bucket=bucket, prefix=PREFIX, decision=decision("clm_1", "idem-late")
        )
    assert dossier_review_store.applied_decisions(
        [mine, other, decision("clm_1", "idem-late")], applied["pointer"]
    ) == [mine, other]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pointer: pointer.update(contract_version="dossier_pointer_v1"),
        lambda pointer: pointer.update(dossier_version="9" * 64),
        lambda pointer: pointer.update(
            resources={
                "dossier:x": {
                    "state": "approved",
                    "resource_version": "a" * 64,
                    "applied": ["b" * 64],
                }
            }
        ),
        lambda pointer: pointer.update(
            resources={
                "claim:clm_1": {"state": "approved", "resource_version": "a" * 64}
            }
        ),
        lambda pointer: pointer.update(
            resources={
                "claim:clm_1": {
                    "state": "approved",
                    "resource_version": "a" * 64,
                    "applied": [],
                }
            }
        ),
        lambda pointer: pointer.update(extra=True),
    ],
)
def test_a_state_pointer_outside_its_grammar_is_refused_on_read(workspace, mutate):
    pointer = {
        "contract_version": "dossier_review_state_v1",
        "investigation_id": workspace.scope.investigation_id,
        "dossier_version": workspace.version,
        "resources": {},
    }
    mutate(pointer)
    rewrite_pointer(workspace, pointer)
    with pytest.raises(dossier_store.DossierRecordInvalid):
        dossier_review_store.read_state_pointer(
            bucket=workspace.bucket,
            prefix=PREFIX,
            investigation_id=workspace.scope.investigation_id,
            dossier_version=workspace.version,
        )


def test_the_admitted_authority_is_the_route_supplied_object_and_nothing_in_the_module_changed():
    assert dossier_approval.authority_state().available is False
    admitted = main._review_authority()
    assert isinstance(admitted, dossier_approval.AuthorityState)
    assert admitted.available is True
    assert admitted.code == "review_authority_admitted"


def test_a_lost_race_on_another_resource_leaves_the_resource_movable(monkeypatch, workspace):
    first, ready_first = reviewer(monkeypatch, SUBJECTS["claims"])
    second, ready_second = reviewer(monkeypatch, SUBJECTS["claims_two"])
    on_one = claim_command(workspace, "clm_1", idempotency_key="idem-one")
    on_two = claim_command(workspace, "clm_2", idempotency_key="idem-two")
    directory = f"{PREFIX}decisions/{workspace.scope.investigation_id}/{workspace.version}/"
    outcomes = {}

    def first_writer():
        # Runs inside the second writer's pointer upload, after the second
        # writer read the pointer and stored its decision on clm_2.
        outcomes["first"] = first.post(workspace.review, json=on_one, headers=ready_first)

    workspace.bucket.before_upload.append(
        (lambda name: name.startswith(directory + "pointer/"), first_writer)
    )
    outcomes["second"] = second.post(workspace.review, json=on_two, headers=ready_second)
    assert outcomes["first"].status_code == 200, outcomes["first"].text
    assert outcomes["second"].status_code == 409
    assert detail(outcomes["second"])["code"] == "review_version_conflict"
    assert "retry" in detail(outcomes["second"])["message"]
    pointer = stored_pointer(workspace)
    assert set(pointer["resources"]) == {"claim:clm_1"}
    orphan = {
        name[len(directory) : -len(".json")]
        for name in workspace.bucket.objects
        if name.startswith(directory) and "/pointer/" not in name
    } - {outcomes["first"].json()["decision_id"]}
    assert len(orphan) == 1

    # A new key on the resource the loser was working on succeeds, for either principal.
    fresh = second.post(workspace.review, json=on_two | {"idempotency_key": "idem-two-again"}, headers=ready_second)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["state"] == "approved"
    entry = stored_pointer(workspace)["resources"]["claim:clm_2"]
    assert entry["applied"] == [fresh.json()["decision_id"]]
    assert fresh.json()["decision_id"] not in orphan
    # The orphan replay is now history: the resource moved without it.
    stale = second.post(workspace.review, json=on_two, headers=ready_second)
    assert (stale.status_code, detail(stale)["message"]) == (409, "resource state has moved")
    assert stored_pointer(workspace)["resources"]["claim:clm_2"] == entry
    blocked = first.post(workspace.review, json=on_two | {"idempotency_key": "idem-first-on-two"}, headers=ready_first)
    assert (blocked.status_code, detail(blocked)["message"]) == (409, "resource state has moved")


def test_the_same_key_recovers_a_decision_that_lost_only_the_generation(monkeypatch, workspace):
    first, ready_first = reviewer(monkeypatch, SUBJECTS["claims"])
    second, ready_second = reviewer(monkeypatch, SUBJECTS["claims_two"])
    on_one = claim_command(workspace, "clm_1", idempotency_key="idem-one")
    on_two = claim_command(workspace, "clm_2", idempotency_key="idem-two")
    directory = f"{PREFIX}decisions/{workspace.scope.investigation_id}/{workspace.version}/"
    workspace.bucket.before_upload.append(
        (
            lambda name: name.startswith(directory + "pointer/"),
            lambda: first.post(workspace.review, json=on_one, headers=ready_first),
        )
    )
    lost = second.post(workspace.review, json=on_two, headers=ready_second)
    assert lost.status_code == 409
    uploads = len(workspace.bucket.uploads)
    recovered = second.post(workspace.review, json=on_two, headers=ready_second)
    assert recovered.status_code == 200, recovered.text
    entry = stored_pointer(workspace)["resources"]["claim:clm_2"]
    assert entry["applied"] == [recovered.json()["decision_id"]]
    # The replay stored nothing new: one pointer move and no second decision object.
    assert [name for name, _ in workspace.bucket.uploads[uploads:]] == [pointer_name(workspace, 2)]
    assert set(stored_pointer(workspace)["resources"]) == {"claim:clm_1", "claim:clm_2"}
