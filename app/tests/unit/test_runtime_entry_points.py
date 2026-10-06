"""E03 runtime entry points: the checked in HTTP inventory against the real app.

docs/operations/runtime-entry-points.json records every route of the served
application with the guard it applies. The first group of tests rebuilds that
table from the application object and the handler source and fails on a route
the inventory lacks, an inventoried route that no longer exists, or a guard that
differs from the one the code applies, so the inventory cannot go stale.

The second group reads the inventory and calls every guarded route through the
test client without a credential and with a wrong, rotated, misplaced, forged
or expired one, and asserts the refusal carries no data. A new route lands in
these loops as soon as it is inventoried, and it cannot stay out of the
inventory because of the first group.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount
from src.api import (
    deployment_contract,
    general_question_routes,
    historical_workspace,
    main,
    workspace_scope,
)
from tests.unit import test_main_dossier_review_routes as review_routes
from tests.unit import test_general_question_oidc as oidc
from tests.unit.test_question_worker_result import EXPECTED
from tests.unit.test_workspace_scope import Bucket as RecordBucket
from tests.unit.test_workspace_scope import stored_record

REAL_RESOLVER = workspace_scope.resolve_workspace_scope
ROOT = Path(__file__).resolve().parents[3]
INVENTORY_PATH = ROOT / "docs" / "operations" / "runtime-entry-points.json"
INVENTORY = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
ROUTES = INVENTORY["http"]["routes"]

PASSCODE = "entry-points-passcode"
ROTATED = "entry-points-passcode-previous"
PASSCODE_REFUSAL = {"detail": "Missing or invalid passcode"}

# The functions whose call inside a handler, or inside a helper the handler
# reaches, is the guard the inventory records. Dependencies are read from the
# route itself; these are the checks the handlers run in their own bodies.
HANDLER_GUARDS = {
    "_require_same_origin": "same_origin",
    "_review_principal": "review_session",
    "_review_session_principal": "review_session",
    "_verify_review_credential": "review_credential",
    "verify_question_worker_authorization": "worker_oidc",
    "_card_token": "card_token",
    "resolve_workspace_scope": "workspace_scope",
}
GUARD_MODULES = (main, general_question_routes, historical_workspace)
GUARD_DEPTH = 4
INTENTIONALLY_PUBLIC = {
    ("GET", "/"),
    ("GET", "/api/health"),
    ("POST", "/api/auth/verify"),
}
PATH_PARAMETERS = {
    "investigation_id": "inv_example",
    "artifact_id": "art_0123456789abcdef",
    "handle": "creator",
    "topic_id": "topic",
    "term": "term",
    "request_id": "00000000-0000-4000-8000-000000000001",
    "query": "amapiano",
}


# Observation of the real application


def _function_index():
    index: dict[str, list] = {}
    for module in GUARD_MODULES:
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(node.name, []).append(node)
    return index


_INDEX = _function_index()


def _referenced_names(node):
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def observed_handler_guards(endpoint):
    """The guard functions the handler reaches, following helpers a few calls deep."""
    seen: set[str] = set()
    frontier = {endpoint.__name__}
    reached: set[str] = set()
    minted = compared = False
    for _ in range(GUARD_DEPTH):
        following: set[str] = set()
        for name in frontier - seen:
            seen.add(name)
            for node in _INDEX.get(name, ()):
                names = _referenced_names(node)
                if "_card_token" in names:
                    minted = True
                    compared = compared or "compare_digest" in names
                reached |= names
                following |= names
        frontier = following - seen
    guards = {HANDLER_GUARDS[name] for name in HANDLER_GUARDS if name in reached}
    if minted and not compared:
        # The function that computes a card token never compares one: it mints
        # a link for a caller another guard already admitted and checks nothing.
        guards = (guards - {"card_token"}) | {"card_token_mint"}
    return sorted(guards)


def _dependency_names(dependant):
    names = []
    for dependency in dependant.dependencies:
        names.append(dependency.call.__name__)
        names.extend(_dependency_names(dependency))
    return names


def access_of(dependencies, handler_guards):
    """The one word summary the inventory carries, derived from the two guard lists."""
    passcode = "require_passcode" in dependencies
    if "worker_oidc" in handler_guards:
        return "worker_oidc"
    if passcode and "review_session" in handler_guards:
        return "passcode_and_review_session"
    if passcode and "review_credential" in handler_guards:
        return "passcode_and_review_credential"
    if passcode:
        return "passcode"
    if "card_token" in handler_guards:
        return "card_token_or_passcode"
    return "public"


def observed_routes():
    rows = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        dependencies = sorted(set(_dependency_names(route.dependant)))
        guards = observed_handler_guards(route.endpoint)
        for method in sorted(route.methods):
            rows.append(
                {
                    "method": method,
                    "path": route.path,
                    "endpoint": f"{route.endpoint.__module__}:{route.endpoint.__name__}",
                    "dependencies": dependencies,
                    "handler_guards": guards,
                    "access": access_of(dependencies, guards),
                }
            )
    return rows


def observed_mounts():
    """Every app.mount in the module source, since a mount is conditional on a build."""
    mounts = []
    for node in ast.walk(ast.parse(inspect.getsource(main))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mount"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
        ):
            mounts.append(node.args[0].value)
    return sorted(mounts)


def _key(row):
    return (row["method"], row["path"])


def _inventoried(fields):
    return {_key(row): {field: row[field] for field in fields} for row in ROUTES}


# The inventory cannot go stale


def test_the_inventory_lists_exactly_the_routes_the_app_serves():
    observed = {_key(row) for row in observed_routes()}
    recorded = [_key(row) for row in ROUTES]
    assert len(recorded) == len(set(recorded)), "a route is inventoried twice"
    assert sorted(observed - set(recorded)) == [], "served but not inventoried"
    assert sorted(set(recorded) - observed) == [], "inventoried but not served"


def test_every_recorded_guard_is_the_guard_the_code_applies():
    fields = ("endpoint", "dependencies", "handler_guards", "access")
    recorded = _inventoried(fields)
    for row in observed_routes():
        assert recorded[_key(row)] == {field: row[field] for field in fields}, _key(row)


def test_only_the_three_intentional_routes_are_public_and_each_says_why():
    public = {_key(row) for row in ROUTES if row["access"] == "public"}
    assert public == INTENTIONALLY_PUBLIC
    for row in ROUTES:
        reason = row.get("public_reason")
        if row["access"] == "public":
            assert isinstance(reason, str) and reason, _key(row)
        else:
            assert reason is None, _key(row)


def test_the_mounts_and_framework_documentation_routes_match_the_inventory():
    assert [mount["path"] for mount in INVENTORY["http"]["mounts"]] == observed_mounts()
    assert INVENTORY["http"]["framework_docs"] == {
        "docs_url": main.app.docs_url,
        "redoc_url": main.app.redoc_url,
        "openapi_url": main.app.openapi_url,
    }
    served_mounts = [
        route.path for route in main.app.routes if isinstance(route, Mount)
    ]
    assert set(served_mounts) <= set(observed_mounts())


def test_the_worker_route_records_the_one_invoker_the_code_accepts():
    (worker,) = [row for row in ROUTES if row["access"] == "worker_oidc"]
    assert worker["invoker"] == deployment_contract.REQUIRED_SERVICE_ACCOUNT
    assert all("invoker" not in row for row in ROUTES if row is not worker)


# Unauthorised access at the real boundary


def _routes_with(predicate):
    return [row for row in ROUTES if predicate(row)]


def _path(row):
    return row["path"].format(
        **{
            name: value
            for name, value in PATH_PARAMETERS.items()
            if "{" + name + "}" in row["path"]
        }
    )


def _id(row):
    return f"{row['method']} {row['path']}"


PASSCODE_ROUTES = _routes_with(lambda row: "require_passcode" in row["dependencies"])
PASSCODE_ATTEMPTS = {
    "none": {},
    "empty": {"headers": {"X-Passcode": ""}},
    "wrong": {"headers": {"X-Passcode": PASSCODE + "x"}},
    "prefix": {"headers": {"X-Passcode": PASSCODE[:-1]}},
    "case_changed": {"headers": {"X-Passcode": PASSCODE.upper()}},
    "rotated": {"headers": {"X-Passcode": ROTATED}},
    "bearer_header": {"headers": {"Authorization": f"Bearer {PASSCODE}"}},
    "query_parameter": {"params": {"passcode": PASSCODE, "X-Passcode": PASSCODE}},
    "cookie": {"cookies": {"X-Passcode": PASSCODE, "passcode": PASSCODE}},
}


def _client(cookies=None):
    browser = TestClient(main.app, base_url="https://testserver")
    for name, value in (cookies or {}).items():
        browser.cookies.set(name, value)
    return browser


@pytest.fixture
def passcode_configured(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", PASSCODE)
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)


@pytest.mark.parametrize("attempt", sorted(PASSCODE_ATTEMPTS))
@pytest.mark.parametrize("row", PASSCODE_ROUTES, ids=_id)
def test_a_gated_route_refuses_every_wrong_or_misplaced_passcode(
    passcode_configured, row, attempt
):
    options = dict(PASSCODE_ATTEMPTS[attempt])
    browser = _client(options.pop("cookies", None))
    response = browser.request(row["method"], _path(row), **options)
    assert response.status_code == 401, response.text
    assert response.json() == PASSCODE_REFUSAL
    assert PASSCODE not in response.text


@pytest.mark.parametrize("row", PASSCODE_ROUTES, ids=_id)
def test_a_gated_route_fails_closed_without_a_configured_passcode(monkeypatch, row):
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    response = _client().request(
        row["method"], _path(row), headers={"X-Passcode": PASSCODE}
    )
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Passcode gate not configured"}


def test_the_passcode_loop_covers_every_route_that_is_not_public():
    covered = {_key(row) for row in PASSCODE_ROUTES}
    others = {
        _key(row)
        for row in ROUTES
        if row["access"] not in ("public", "worker_oidc", "card_token_or_passcode")
    }
    assert others <= covered
    assert len(covered) == len(ROUTES) - 5


# Review session routes: a valid passcode is not a reviewer


SESSION_ROUTES = _routes_with(
    lambda row: row["access"] == "passcode_and_review_session"
)


@pytest.fixture
def review_environment(monkeypatch):
    yield from review_routes.review_environment.__wrapped__(monkeypatch)


def _storage_never_read(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("storage was read")

    monkeypatch.setattr(main, "_investigation_bucket", refuse)
    monkeypatch.setattr(main, "_review_bucket", refuse)
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", refuse)


def _session_attempt(monkeypatch, review_environment, attempt):
    csrf = {"X-Review-CSRF": "x" * 43}
    if attempt == "no_session":
        return _client(), {**review_routes.ORIGIN, **csrf}
    if attempt == "forged_session":
        return (
            _client({"review_session": "forged-session-id"}),
            {**review_routes.ORIGIN, **csrf},
        )
    browser, ready = review_routes.reviewer(
        monkeypatch, review_routes.SUBJECTS["editor"]
    )
    review_environment["now"] = (
        review_routes.NOW + main.REVIEW_SESSION_MAX_AGE_SECONDS + 1
    )
    return browser, ready


@pytest.mark.parametrize("attempt", ["no_session", "forged_session", "expired_session"])
@pytest.mark.parametrize("row", SESSION_ROUTES, ids=_id)
def test_a_review_route_refuses_without_a_live_session_before_storage(
    monkeypatch, review_environment, row, attempt
):
    browser, headers = _session_attempt(monkeypatch, review_environment, attempt)
    _storage_never_read(monkeypatch)
    response = browser.request(row["method"], _path(row), json={}, headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {
        "code": "review_role_required",
        "message": "no principal holds a role",
    }
    assert main._review_sessions == {}


def test_the_review_session_loop_covers_five_routes():
    assert len(SESSION_ROUTES) == 5


CREDENTIAL_ROUTES = _routes_with(
    lambda row: row["access"] == "passcode_and_review_credential"
)


def _review_token(keys, nonce, **claims):
    """A review credential signed by the synthetic key the intercepted fetch serves."""
    now = int(time.time())
    values = {
        "aud": review_routes.AUDIENCE,
        "iss": review_routes.ISSUER,
        "sub": review_routes.SUBJECTS["editor"],
        "hd": review_routes.HOSTED_DOMAIN,
        "nonce": nonce,
        "iat": now - 5,
        "exp": now + 300,
    }
    values.update(claims)
    return oidc.token(keys, **values)


def _present_credential(monkeypatch, review_environment, keys, attempt):
    """Log in, then return a credential through the real pinned verifier."""
    review_environment["now"] = int(time.time())
    fetched = oidc.intercept(monkeypatch, keys)
    browser = _client()
    start = browser.post(review_routes.LOGIN, headers=review_routes.ORIGIN)
    assert start.status_code == 200, start.text
    issued = start.json()
    now = int(time.time())
    credential = {
        "valid": lambda: _review_token(keys, issued["nonce"]),
        "forged": lambda: _tampered(_review_token(keys, issued["nonce"])),
        "expired": lambda: _review_token(
            keys, issued["nonce"], iat=now - 600, exp=now - 300
        ),
        "other_audience": lambda: _review_token(
            keys, issued["nonce"], aud="someone-else.apps.test"
        ),
        "other_nonce": lambda: _review_token(keys, "n" * 43),
        "unlisted_subject": lambda: _review_token(
            keys, issued["nonce"], sub=review_routes.SUBJECTS["stranger"]
        ),
    }[attempt]()
    response = browser.post(
        SESSION_PATH,
        json={"credential": credential, "g_csrf_token": issued["csrf_token"]},
        headers=review_routes.ORIGIN,
    )
    return response, fetched


SESSION_PATH = review_routes.SESSION
CREDENTIAL_REFUSALS = {
    "forged": (400, "credential_invalid"),
    "expired": (400, "credential_invalid"),
    "other_audience": (400, "credential_invalid"),
    "other_nonce": (400, "login_mismatch"),
    "unlisted_subject": (403, "subject_not_allowlisted"),
}


@pytest.mark.parametrize("attempt", sorted(CREDENTIAL_REFUSALS))
@pytest.mark.parametrize("row", CREDENTIAL_ROUTES, ids=_id)
def test_the_session_route_binds_nothing_for_a_bad_signed_credential(
    monkeypatch, review_environment, worker_keys, row, attempt
):
    assert row["path"] == SESSION_PATH
    refused, fetched = _present_credential(
        monkeypatch, review_environment, worker_keys, attempt
    )
    status, message = CREDENTIAL_REFUSALS[attempt]
    assert refused.status_code == status, refused.text
    assert refused.json()["detail"]["message"] == message
    assert main._review_sessions == {}
    assert "review_session" not in refused.headers.get("set-cookie", "")
    assert len(fetched) == 1


def test_the_real_verifier_binds_a_valid_signed_credential(
    monkeypatch, review_environment, worker_keys
):
    """The refusals above come from the verifier and the binding rules, not the harness."""
    bound, fetched = _present_credential(
        monkeypatch, review_environment, worker_keys, "valid"
    )
    assert bound.status_code == 200, bound.text
    assert len(main._review_sessions) == 1
    assert len(fetched) == 1


# Same origin: a live reviewer session from another origin is refused


ORIGIN_ROUTES = _routes_with(lambda row: "same_origin" in row["handler_guards"])
CROSS_ORIGINS = {
    "other_site": {"Origin": "https://evil.test"},
    "suffix_host": {"Origin": "https://testserver.evil.test"},
    "plain_http": {"Origin": "http://testserver"},
    "no_origin": {},
    "cross_site_fetch": {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "cross-site",
    },
}


@pytest.mark.parametrize("origin", sorted(CROSS_ORIGINS))
@pytest.mark.parametrize("row", ORIGIN_ROUTES, ids=_id)
def test_a_same_origin_route_refuses_a_cross_origin_request_before_storage(
    monkeypatch, review_environment, row, origin
):
    browser, ready = review_routes.reviewer(
        monkeypatch, review_routes.SUBJECTS["editor"]
    )
    _storage_never_read(monkeypatch)
    logins, sessions = dict(main._review_logins), dict(main._review_sessions)
    headers = {**CROSS_ORIGINS[origin], "X-Review-CSRF": ready["X-Review-CSRF"]}
    response = browser.request(row["method"], _path(row), json={}, headers=headers)
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == {
        "code": "review_request_invalid",
        "message": "origin_mismatch",
    }
    assert "set-cookie" not in response.headers
    assert (main._review_logins, main._review_sessions) == (logins, sessions)


def test_the_same_origin_loop_covers_six_routes():
    assert len(ORIGIN_ROUTES) == 6


# Workspace scope: a record bound to another client scope is refused


SCOPE_ROUTES = _routes_with(lambda row: "workspace_scope" in row["handler_guards"])
SCOPE_INVALID = {
    "passcode": "Workspace is unavailable for this scope.",
    "passcode_and_review_session": "scope is unavailable",
}


def _scope_request(row, ws):
    """A request that passes every check the route makes before it resolves the scope."""
    suffix = row["path"].rsplit("}", 1)[1]
    return {
        "/status": {},
        "/claims/read": {},
        "/decision/read": {},
        "/read": {"params": {"artifact_version": "a" * 64}},
        "/historical/read": {"json": {"mode": "replay"}},
        "/dossier/read": {"params": {"contract_version": "intelligence_dossier_v1"}},
        "/dossier/review": {"json": review_routes.claim_command(ws)},
        "/dossier/selection": {
            "json": {
                "contract_version": "dossier_review_v1",
                "expected_dossier_version": ws.version,
                "selected_claim_ids": ["clm_1"],
            }
        },
        "/dossier/working": {"params": {"dossier_version": ws.version}},
        "/artifacts/prepare": {
            "json": {
                "contract_version": "dossier_artifact_v1",
                "dossier_version": ws.version,
                "format": "html",
            }
        },
        "/preview": {
            "json": {
                "contract_version": "dossier_artifact_preview_v1",
                "artifact_version": "a" * 64,
            }
        },
        "/export.html": {"params": {"artifact_version": "a" * 64}},
        "/export.pdf": {"params": {"artifact_version": "a" * 64}},
    }[suffix]


@pytest.mark.parametrize("row", SCOPE_ROUTES, ids=_id)
def test_a_workspace_scope_route_refuses_a_record_of_another_client_scope(
    monkeypatch, review_environment, row
):
    headers = {}
    if "review_session" in row["handler_guards"]:
        browser, headers = review_routes.reviewer(
            monkeypatch, review_routes.SUBJECTS["editor"]
        )
    else:
        browser = _client()
    ws = review_routes.workspace.__wrapped__(monkeypatch)
    record = stored_record(client_scope_id="bsa_pulse", brand_config_id="bsa")
    investigation_id = record["response"]["investigation_id"]
    name = f"open-intelligence/v2/staging/investigations/{investigation_id}.json"
    bucket = RecordBucket({name: json.dumps(record).encode()})
    monkeypatch.setattr(workspace_scope, "resolve_workspace_scope", REAL_RESOLVER)
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: bucket)

    def refuse(*args, **kwargs):
        raise AssertionError("storage past the scope record was read")

    monkeypatch.setattr(main, "_investigation_bucket", refuse)
    monkeypatch.setattr(main, "_review_bucket", refuse)
    path = row["path"].replace("{investigation_id}", investigation_id)
    path = path.replace("{artifact_id}", PATH_PARAMETERS["artifact_id"])
    response = browser.request(
        row["method"], path, headers=headers, **_scope_request(row, ws)
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == {
        "code": "scope_invalid",
        "message": SCOPE_INVALID[row["access"]],
    }
    assert bucket.lookups == [name]


def test_the_workspace_scope_loop_covers_thirteen_routes():
    assert len(SCOPE_ROUTES) == 13


# The worker route: only a Google signed token for the one invoker


WORKER_ROUTES = _routes_with(lambda row: row["access"] == "worker_oidc")


def _worker_context(monkeypatch):
    service = general_question_routes.GeneralQuestionRoutes(
        SimpleNamespace(storage_client=None),
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    monkeypatch.setattr(
        main.app.state, "general_question_routes", service, raising=False
    )
    monkeypatch.setattr(
        general_question_routes,
        "read_request_bytes",
        lambda *args, **kwargs: pytest.fail("request store was read"),
    )


@pytest.fixture(scope="module")
def worker_keys():
    """A synthetic signing key whose certificate the intercepted fetch serves."""
    return oidc.signing.__wrapped__()


def _tampered(value):
    """The same token with one signature character changed in the middle."""
    head, body, signature = value.split(".")
    middle = len(signature) // 2
    flipped = "B" if signature[middle] == "A" else "A"
    return ".".join(
        (head, body, signature[:middle] + flipped + signature[middle + 1 :])
    )


def _bearer(keys, **claims):
    return {"Authorization": "Bearer " + oidc.token(keys, **claims)}


def _worker_attempts(keys):
    now = int(time.time())
    invalid, forbidden = (401, "worker_oidc_invalid"), (403, "worker_oidc_forbidden")
    return {
        "none": ({}, invalid),
        "not_bearer": ({"Authorization": "Basic dXNlcjpwYXNz"}, invalid),
        "passcode": ({"X-Passcode": PASSCODE}, invalid),
        "forged_signature": (
            {"Authorization": "Bearer " + _tampered(oidc.token(keys))},
            invalid,
        ),
        "expired": (_bearer(keys, iat=now - 600, exp=now - 300), invalid),
        "other_audience": (_bearer(keys, aud="https://evil.invalid"), invalid),
        "other_invoker": (
            _bearer(keys, email="attacker@ogilvy-trends-v2.iam.gserviceaccount.com"),
            forbidden,
        ),
    }


WORKER_ATTEMPTS = (
    "none",
    "not_bearer",
    "passcode",
    "forged_signature",
    "expired",
    "other_audience",
    "other_invoker",
)


@pytest.mark.parametrize("attempt", WORKER_ATTEMPTS)
@pytest.mark.parametrize("row", WORKER_ROUTES, ids=_id)
def test_the_worker_route_refuses_anything_but_the_signed_invoker_token(
    monkeypatch, passcode_configured, worker_keys, row, attempt
):
    headers, (status, code) = _worker_attempts(worker_keys)[attempt]
    oidc.intercept(monkeypatch, worker_keys)
    _worker_context(monkeypatch)
    response = _client().post(row["path"], json=EXPECTED, headers=headers)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": code}


def test_the_worker_loop_covers_one_route():
    assert len(WORKER_ROUTES) == 1


# The printable card: a per card token or the passcode header, nothing else


CARD_ROUTES = _routes_with(lambda row: row["access"] == "card_token_or_passcode")


def _card_attempts():
    query = PATH_PARAMETERS["query"]
    return {
        "none": {},
        "empty_token": {"params": {"t": ""}},
        "wrong_token": {"params": {"t": "0" * 64}},
        "other_query_token": {"params": {"t": main._card_token("other", "all")}},
        "other_market_token": {"params": {"t": main._card_token(query, "za")}},
        "rotated_secret_token": {"params": {"t": _rotated_token(query)}},
        "wrong_header": {"headers": {"X-Passcode": ROTATED}},
    }


def _rotated_token(query):
    return hmac.new(
        ROTATED.encode("utf-8"), f"{query}|all".encode(), hashlib.sha256
    ).hexdigest()


CARD_ATTEMPTS = (
    "none",
    "empty_token",
    "wrong_token",
    "other_query_token",
    "other_market_token",
    "rotated_secret_token",
    "wrong_header",
)


@pytest.mark.parametrize("attempt", CARD_ATTEMPTS)
@pytest.mark.parametrize("row", CARD_ROUTES, ids=_id)
def test_the_card_refuses_a_token_for_anything_else_before_reading_evidence(
    monkeypatch, passcode_configured, row, attempt
):
    monkeypatch.setattr(main, "_ask_hits", {})
    monkeypatch.setattr(
        main,
        "_ask_payload_cached",
        lambda *args: pytest.fail("evidence was read"),
    )
    response = _client().request(row["method"], _path(row), **_card_attempts()[attempt])
    assert response.status_code == 401, response.text
    assert response.text == main.CARD_LOCKED_HTML


@pytest.mark.parametrize("row", CARD_ROUTES, ids=_id)
def test_the_card_fails_closed_without_a_configured_passcode(monkeypatch, row):
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setattr(main, "_ask_hits", {})
    monkeypatch.setattr(
        main, "_ask_payload_cached", lambda *args: pytest.fail("evidence was read")
    )
    query = PATH_PARAMETERS["query"]
    for options in (
        {},
        {"params": {"t": main._card_token(query, "all")}},
        {"headers": {"X-Passcode": ""}},
    ):
        response = _client().request(row["method"], _path(row), **options)
        assert response.status_code == 503, response.text
        assert response.text == main.CARD_LOCKED_HTML


def test_the_card_serves_with_its_own_token_so_the_refusals_are_the_guard(
    monkeypatch, passcode_configured
):
    (row,) = CARD_ROUTES
    query = PATH_PARAMETERS["query"]
    monkeypatch.setattr(main, "_ask_hits", {})
    monkeypatch.setattr(main, "_ask_payload_cached", lambda *args: {"served": args})
    monkeypatch.setattr(main.card_render, "render_card", lambda payload, now: "card")
    response = _client().get(_path(row), params={"t": main._card_token(query, "all")})
    assert (response.status_code, response.text) == (200, "card")


# The public routes serve no protected data


def test_the_public_routes_leak_no_secret_and_no_evidence(
    monkeypatch, passcode_configured
):
    monkeypatch.setattr(main, "_auth_hits", {})
    monkeypatch.setattr(
        main.bq, "_freshness", lambda: {"stamp_utc": None, "age_hours": None}
    )
    monkeypatch.setattr(main, "_market_cache", {})
    browser = _client()
    health = browser.get("/api/health")
    assert health.status_code == 200
    assert set(health.json()) == {"ok", "dataset", "passcode", "freshness"}
    assert health.json()["passcode"] is True
    refused = browser.post("/api/auth/verify", json={"passcode": ROTATED})
    assert (refused.status_code, refused.json()) == (401, PASSCODE_REFUSAL)
    index = browser.get("/")
    assert index.status_code == 200
    for response in (health, refused, index):
        assert PASSCODE not in response.text


@pytest.mark.parametrize(
    "path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]
)
def test_the_framework_documentation_routes_are_not_served(path):
    assert _client().get(path).status_code == 404
