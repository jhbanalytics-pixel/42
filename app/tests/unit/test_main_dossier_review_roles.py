"""Review roles held together by one subject, and the enrolment log for unlisted subjects."""

from __future__ import annotations

import hashlib
import json
import logging

import pytest

from src.api import (
    dossier_approval,
    dossier_review_auth,
    dossier_review_store,
    dossier_store,
    main,
)
from tests.unit import dossier_pdf_fakes
from tests.unit.test_dossier_store import PREFIX
from tests.unit.test_main_dossier_review_routes import (
    ALLOWLIST,
    AUDIENCE,
    HOSTED_DOMAIN,
    ISSUER,
    NOW,
    ORIGIN,
    SUBJECTS,
    bind,
    claim_command,
    client,
    detail,
    relationship_command,
    untouched_storage,
)
from tests.unit.test_main_dossier_review_routes import (
    review_environment as review_environment_fixture,
)
from tests.unit.test_main_dossier_review_routes import workspace as workspace_fixture

_SHARED_FIXTURES = (review_environment_fixture, workspace_fixture)

BOTH = "100000000000000000010"
ROLES = ["dossier_editor", "claim_approver"]
UNLISTED = "100000000000000000099"


@pytest.fixture(autouse=True)
def review_environment(review_environment_fixture, monkeypatch):
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", json.dumps({**ALLOWLIST, BOTH: ROLES}))
    return review_environment_fixture


@pytest.fixture
def workspace(workspace_fixture):
    return workspace_fixture


def ref(subject):
    return "human:" + hashlib.sha256(subject.encode("utf-8")).hexdigest()[:8]


def session_for(monkeypatch, subject):
    browser = client()
    bound = bind(browser, monkeypatch, subject)
    assert bound.status_code == 200, bound.text
    return browser, {**ORIGIN, "X-Review-CSRF": bound.json()["csrf_token"]}, bound.json()


def decisions(ws, version):
    return dossier_review_store.list_decisions(
        bucket=ws.bucket,
        prefix=PREFIX,
        investigation_id=ws.scope.investigation_id,
        dossier_version=version,
    )


# The kernel


def validate(allowlist, subject=BOTH, **settings):
    return dossier_review_auth.validate_review_credential(
        token_claims={"aud": AUDIENCE, "iss": ISSUER, "exp": NOW + 60, "sub": subject, "hd": HOSTED_DOMAIN},
        now=NOW,
        expected_audience=AUDIENCE,
        expected_issuer=ISSUER,
        expected_hd=HOSTED_DOMAIN,
        subject_allowlist=allowlist,
        **settings,
    )


def test_a_listed_subject_holds_every_role_it_is_listed_with():
    assert validate({BOTH: ROLES}) == {"principal_ref": ref(BOTH), "role": "dossier_editor", "roles": ROLES}
    assert validate({BOTH: "claim_approver"}) == {"principal_ref": ref(BOTH), "role": "claim_approver"}


@pytest.mark.parametrize(
    "value",
    [[], ["claim_approver", "claim_approver"], ["claim_approver", "admin"], ["claim_approver", 1], "admin"],
)
def test_a_malformed_role_list_admits_no_one(value):
    with pytest.raises(dossier_review_auth.ReviewAuthError) as refused:
        validate({BOTH: value})
    assert (refused.value.status, refused.value.reason) == (403, "role_unknown")


def test_an_empty_allowlist_admits_sign_in_only_when_enrolment_is_asked_for():
    with pytest.raises(dossier_review_auth.ReviewAuthError) as closed:
        validate({})
    assert (closed.value.status, closed.value.reason) == (503, "allowlist_empty")
    with pytest.raises(dossier_review_auth.ReviewAuthError) as unlisted:
        validate({}, enrolment=True)
    assert (unlisted.value.status, unlisted.value.reason) == (403, "subject_not_allowlisted")


def test_record_review_authorises_from_the_role_set_and_records_the_acting_role():
    fields = dict(
        investigation_id="inv_fixture",
        dossier_version="b" * 64,
        resource="claim",
        resource_id="clm_1",
        resource_version="c" * 64,
        action="approve",
        expected_state="pending_review",
        idempotency_key="idem-1",
        support_review={"verdict": "supported", "receipt_ids": ["r1"], "note": ""},
        principal_ref="human:0123abcd",
        recorded_at="2026-09-13T08:00:00Z",
    )
    legacy = dossier_review_store.build_decision(**fields)
    assert "acting_role" not in legacy
    assert dossier_review_store.parse_decision(legacy) == legacy
    acted = dossier_review_store.build_decision(**fields, acting_role="claim_approver")
    assert acted["acting_role"] == "claim_approver"
    assert acted["decision_id"] != legacy["decision_id"]
    assert dossier_review_store.parse_decision(acted) == acted
    with pytest.raises(dossier_store.DossierRecordInvalid):
        dossier_review_store.build_decision(**fields, acting_role="dossier_editor")
    forged = dict(acted, acting_role="relationship_approver")
    with pytest.raises(dossier_store.DossierRecordInvalid):
        dossier_review_store.parse_decision(forged)


def test_record_review_refuses_a_role_set_that_does_not_hold_its_first_role():
    with pytest.raises(dossier_approval.ReviewRefused) as refused:
        dossier_approval.record_review(
            bucket=None,
            scope=None,
            principal={"principal_ref": "human:0123abcd", "role": "claim_approver", "roles": ["dossier_editor"]},
            command={},
        )
    assert refused.value.refusal.status == 403


# Through the handler


def test_the_session_names_the_first_role_and_every_role(monkeypatch):
    _browser, _ready, both = session_for(monkeypatch, BOTH)
    assert set(both) == {"contract_version", "role", "roles", "csrf_token", "expires_at"}
    assert (both["role"], both["roles"]) == ("dossier_editor", ROLES)
    _browser, _ready, single = session_for(monkeypatch, SUBJECTS["claims"])
    assert (single["role"], single["roles"]) == ("claim_approver", ["claim_approver"])


def test_one_subject_acts_in_each_role_it_holds_and_the_record_names_which(monkeypatch, workspace):
    dossier_pdf_fakes.install(monkeypatch)
    browser, ready, _bound = session_for(monkeypatch, BOTH)
    selection = browser.post(
        f"/api/internal/v2/investigations/{workspace.scope.investigation_id}/dossier/selection",
        json={
            "contract_version": "dossier_review_v1",
            "expected_dossier_version": workspace.version,
            "selected_claim_ids": ["clm_1"],
        },
        headers=ready,
    )
    assert selection.status_code == 200, selection.text
    version = selection.json()["dossier_version"]
    approved = browser.post(
        workspace.review, json=claim_command(workspace, dossier_version=version), headers=ready
    )
    assert approved.status_code == 200, approved.text
    outside = browser.post(
        workspace.review, json=relationship_command(workspace, dossier_version=version), headers=ready
    )
    assert (outside.status_code, detail(outside)["code"]) == (403, "review_role_required")
    prepared = browser.post(
        f"/api/internal/v2/investigations/{workspace.scope.investigation_id}/artifacts/prepare",
        json={"contract_version": "dossier_artifact_v1", "dossier_version": version, "format": "html"},
        headers=ready,
    )
    assert prepared.status_code == 200, prepared.text
    recorded = {item["resource"]: item for item in decisions(workspace, version)}
    assert set(recorded) == {"claim", "artifact"}
    assert recorded["claim"]["acting_role"] == "claim_approver"
    assert recorded["artifact"]["acting_role"] == "dossier_editor"
    assert recorded["claim"]["principal_ref"] == recorded["artifact"]["principal_ref"] == ref(BOTH)


@pytest.mark.parametrize(
    "value", [[], ["claim_approver", "claim_approver"], ["claim_approver", "admin"], [1]]
)
def test_a_malformed_role_list_closes_review_configuration(monkeypatch, value):
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", json.dumps({BOTH: value}))
    closed = client().post("/api/internal/v2/dossier/review/login", headers=ORIGIN)
    assert (closed.status_code, detail(closed)["code"]) == (503, "review_authority_unavailable")


# Enrolment


def enrolment_records(caplog):
    return [record for record in caplog.records if "review_enrolment_refused" in record.getMessage()]


def refuse_unlisted(monkeypatch, caplog, **settings):
    browser = client()
    with caplog.at_level(logging.DEBUG):
        refused = bind(
            browser,
            monkeypatch,
            UNLISTED,
            claims={"email": "reviewer@ogilvy.test", "name": "Rae Viewer", "email_verified": True},
            **settings,
        )
    return refused


@pytest.mark.parametrize("allowlist", [{}, ALLOWLIST])
def test_an_unlisted_subject_is_refused_and_logged_once_for_enrolment(monkeypatch, caplog, allowlist):
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", json.dumps(allowlist))
    untouched_storage(monkeypatch)
    refused = refuse_unlisted(monkeypatch, caplog)
    assert (refused.status_code, detail(refused)["code"]) == (403, "review_role_required")
    assert "review_session" not in refused.headers.get("set-cookie", "")
    assert main._review_sessions == {}
    (line,) = enrolment_records(caplog)
    assert line.name == "listening_post.api"
    assert line.getMessage() == f"review_enrolment_refused subject={UNLISTED} hd={HOSTED_DOMAIN}"
    everything = " ".join(record.getMessage() for record in caplog.records)
    for secret in ("signed-credential", "reviewer@ogilvy.test", "Rae Viewer", "@"):
        assert secret not in everything


def test_an_enrolment_refusal_still_needs_the_issued_nonce(monkeypatch, caplog):
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", "{}")
    refused = refuse_unlisted(monkeypatch, caplog, nonce="another-nonce")
    assert (refused.status_code, detail(refused)["message"]) == (400, "login_mismatch")
    assert enrolment_records(caplog) == []


def test_an_empty_allowlist_opens_sign_in_but_no_command(monkeypatch, caplog):
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", "{}")
    started = client().post("/api/internal/v2/dossier/review/login", headers=ORIGIN)
    assert started.status_code == 200
    assert main._review_authority().available is False
    untouched_storage(monkeypatch)
    command = client().post(
        "/api/internal/v2/investigations/inv_x/dossier/review", json={}, headers={**ORIGIN, "X-Review-CSRF": "x" * 43}
    )
    assert (command.status_code, detail(command)["code"]) == (403, "review_role_required")
    for missing in ("REVIEW_OAUTH_AUDIENCE", "REVIEW_HOSTED_DOMAIN"):
        with monkeypatch.context() as scoped:
            scoped.delenv(missing)
            closed = client().post("/api/internal/v2/dossier/review/login", headers=ORIGIN)
            assert (closed.status_code, detail(closed)["code"]) == (503, "review_authority_unavailable")


def test_an_unlisted_subject_outside_the_identifier_grammar_is_logged_without_it(monkeypatch, caplog):
    monkeypatch.setenv("REVIEW_SUBJECT_ALLOWLIST", "{}")
    browser = client()
    with caplog.at_level(logging.DEBUG):
        refused = bind(browser, monkeypatch, "1 hd=evil.example")
    assert (refused.status_code, detail(refused)["code"]) == (403, "review_role_required")
    everything = [record.getMessage() for record in caplog.records]
    assert not [line for line in everything if "hd=evil.example" in line]
    (line,) = enrolment_records(caplog)
    assert line.getMessage() == "review_enrolment_refused subject=invalid"
