"""The console dossier read resolves through the store, never through a field the scope never had.

The route follows the scoped current pointer to the exact immutable version,
projects it with the decisions the review store committed, and hands back the
existing eight-field client read. Every absence is a named state.
"""

from __future__ import annotations

import pytest

from src.api import (
    deployment_contract,
    dossier_resolver,
    dossier_review_store,
    dossier_store,
    main,
    workspace_scope,
)
from tests.unit import test_main_dossier_review_routes as review_routes
from tests.unit.test_dossier_resolver import dossier
from tests.unit.test_dossier_store import PREFIX, body_of, publication
from tests.unit.test_main_dossier_review_routes import (
    SUBJECTS,
    Bucket,
    claim_command,
    reviewer,
)


@pytest.fixture(autouse=True)
def review_environment(monkeypatch):
    """The review route environment, so a reviewer can bind a session."""
    yield from review_routes.review_environment.__wrapped__(monkeypatch)


@pytest.fixture
def workspace(monkeypatch):
    """One published dossier behind the real store, resolver and scope seam."""
    return review_routes.workspace.__wrapped__(monkeypatch)


VERSION = "intelligence_dossier_v1"
CLIENT_READ_FIELDS = (
    "contract_version",
    "investigation_id",
    "concise_answer",
    "claims",
    "evidence",
    "excluded_count",
    "excluded_reasons",
    "artifact_readiness",
)


def read(client, investigation_id):
    return client.get(
        f"/api/internal/v2/investigations/{investigation_id}/dossier/read"
        f"?contract_version={VERSION}"
    )


def test_the_read_resolves_the_current_version_through_the_store(
    monkeypatch, workspace
):
    browser, _ = reviewer(monkeypatch, SUBJECTS["claims"])
    response = read(browser, workspace.scope.investigation_id)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload) == CLIENT_READ_FIELDS
    assert payload["contract_version"] == VERSION
    assert payload["investigation_id"] == workspace.scope.investigation_id
    # Nothing is decided yet, so nothing projects as a finding and the
    # withheld claims are counted, not carried.
    assert payload["claims"] == []
    assert payload["excluded_count"] >= 1
    assert payload["artifact_readiness"]["state"] in {"approval_required", "blocked"}
    assert payload["evidence"] == []


def test_a_committed_decision_reaches_the_read_and_an_uncommitted_one_does_not(
    monkeypatch, workspace
):
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    approved = browser.post(
        workspace.review, json=claim_command(workspace), headers=ready
    )
    assert approved.status_code == 200, approved.text

    response = read(browser, workspace.scope.investigation_id)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["claim_id"] for item in payload["claims"]] == ["clm_1"]
    assert set(payload["claims"][0]) == {"claim_id", "kind", "text", "citations"}

    # A valid decision no state pointer committed is not a decision: it is
    # written through the store's own writer for clm_2 and projects nothing.
    recorded = next(
        name
        for name in workspace.bucket.objects
        if name.startswith(f"{PREFIX}decisions/")
    )
    committed = dossier_store._load(workspace.bucket.objects[recorded][0])
    command = claim_command(workspace, "clm_2")
    uncommitted = dossier_review_store.build_decision(
        investigation_id=workspace.scope.investigation_id,
        dossier_version=workspace.version,
        resource="claim",
        resource_id="clm_2",
        resource_version=command["resource_version"],
        action="approve",
        expected_state="pending_review",
        idempotency_key="idem-uncommitted",
        support_review=command["support_review"],
        principal_ref=committed["principal_ref"],
        recorded_at=committed["recorded_at"],
    )
    dossier_review_store.create_decision(
        bucket=workspace.bucket, prefix=PREFIX, decision=uncommitted
    )
    again = read(browser, workspace.scope.investigation_id)
    assert again.status_code == 200, again.text
    assert [item["claim_id"] for item in again.json()["claims"]] == ["clm_1"]


def test_the_read_never_returns_an_empty_ready_dossier(monkeypatch, workspace):
    """Break caught: the old read of a field the scope never carried."""
    browser, _ = reviewer(monkeypatch, SUBJECTS["claims"])
    empty = Bucket()
    monkeypatch.setattr(main, "_investigation_bucket", lambda: empty)
    response = read(browser, workspace.scope.investigation_id)
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "dossier_unavailable"
    assert detail["reason"] == "dossier_unpublished"
    assert "claims" not in response.json()


@pytest.mark.parametrize(
    "reason, status, code",
    [
        ("dossier_storage_unavailable", 503, "workspace_unavailable"),
        ("dossier_scope_mismatch", 404, "scope_invalid"),
        ("dossier_request_invalid", 400, "workspace_request_invalid"),
        ("dossier_version_absent", 404, "dossier_unavailable"),
        ("dossier_generation_mismatch", 404, "dossier_unavailable"),
        ("dossier_publication_missing", 404, "dossier_unavailable"),
        ("dossier_body_conflict", 404, "dossier_unavailable"),
    ],
)
def test_every_named_absence_keeps_its_name(
    monkeypatch, workspace, reason, status, code
):
    browser, _ = reviewer(monkeypatch, SUBJECTS["claims"])
    seen = []

    def unavailable(scope, dossier_version=None, *, bucket=None, prefix=PREFIX):
        seen.append(
            (scope.investigation_id, dossier_version, bucket is workspace.bucket)
        )
        return dossier_resolver._unavailable(
            scope.investigation_id, dossier_version, reason
        )

    monkeypatch.setattr(dossier_resolver, "resolve_dossier", unavailable)
    response = read(browser, workspace.scope.investigation_id)
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    if code == "dossier_unavailable":
        assert detail["reason"] == reason
    assert seen == [(workspace.scope.investigation_id, None, True)]


def test_a_pointer_from_another_scope_reveals_nothing(monkeypatch, workspace):
    browser, _ = reviewer(monkeypatch, SUBJECTS["claims"])
    other = workspace_scope.ResolvedWorkspaceScope(
        investigation_id=workspace.scope.investigation_id,
        frame=workspace.scope.frame,
        response=workspace.scope.response,
        scope_digest="0" * 64,
    )
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", lambda _id: other)
    response = read(browser, workspace.scope.investigation_id)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "scope_invalid"


def test_a_moved_generation_and_a_lost_publication_are_unavailable_not_ready(
    monkeypatch, workspace
):
    browser, _ = reviewer(monkeypatch, SUBJECTS["claims"])
    name = dossier_store.dossier_object_name(
        PREFIX, workspace.scope.investigation_id, workspace.version
    )
    body = workspace.bucket.objects[name][0]
    workspace.bucket.replace(name, body)
    moved = read(browser, workspace.scope.investigation_id)
    assert moved.status_code == 404
    assert moved.json()["detail"]["reason"] == "dossier_generation_mismatch"

    fresh = Bucket()
    dossier_store.create_dossier(
        bucket=fresh,
        prefix=PREFIX,
        body=body_of(dossier()),
        publication=publication(),
        pointer_writer=lambda pointer: dossier_store.publish_pointer(
            pointer, bucket=fresh, prefix=PREFIX
        ),
    )
    del fresh.objects[
        dossier_store.publication_object_name(
            PREFIX, workspace.scope.investigation_id, workspace.version
        )
    ]
    monkeypatch.setattr(main, "_investigation_bucket", lambda: fresh)
    lost = read(browser, workspace.scope.investigation_id)
    assert lost.status_code == 404
    assert lost.json()["detail"]["reason"] == "dossier_publication_missing"


def test_storage_failures_are_unavailable_and_distinct_from_absence(
    monkeypatch, workspace
):
    browser, _ = reviewer(monkeypatch, SUBJECTS["claims"])

    def no_bucket():
        raise deployment_contract.DeploymentContractError(
            "storage client is unavailable"
        )

    monkeypatch.setattr(main, "_investigation_bucket", no_bucket)
    response = read(browser, workspace.scope.investigation_id)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "workspace_unavailable"

    monkeypatch.setattr(main, "_investigation_bucket", lambda: workspace.bucket)
    workspace.bucket.list_failing = True
    listing = read(browser, workspace.scope.investigation_id)
    assert listing.status_code == 503
    assert listing.json()["detail"]["code"] == "workspace_unavailable"


def test_the_scope_object_is_not_enlarged_by_the_route(workspace):
    assert set(workspace_scope.ResolvedWorkspaceScope.__slots__) == {
        "investigation_id",
        "frame",
        "response",
        "scope_digest",
    }
    assert not hasattr(workspace.scope, "dossier_record")


def test_the_read_touches_no_object(monkeypatch, workspace):
    browser, _ = reviewer(monkeypatch, SUBJECTS["claims"])
    before = list(workspace.bucket.uploads)
    assert read(browser, workspace.scope.investigation_id).status_code == 200
    assert workspace.bucket.uploads == before
