"""Every route of f42-api and f42-agent against core/api/contract.md, in both directions, and the passcode gate.

Three claims, each checked route by route so a failure names the route:

  1. every route the code serves is written in the contract as `METHOD /path` (code names compare with the
     contract's after parameters are written as {}),
  2. every route the contract writes is served by f42-api or f42-agent,
  3. every f42-api route except the two the contract names answers 401 to a caller with no passcode.

The expected sets come from the contract text and from the two open routes named in its section 2, not from the
apps, so a route added to the code without the contract, or a gate dropped from a route, fails here.

KNOWN_OPEN lists the routes that are wrong today. Each is an strict xfail naming the work item and the lane that
owns the file to change. When the owner fixes the file the case turns into an unexpected pass and fails, so the
entry has to be deleted with the fix.
"""
import itertools
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness_routes as hr  # noqa: E402

ROOT = Path(__file__).resolve().parents[4]
CONTRACT = ROOT / "core" / "api" / "contract.md"

# Contract section 2: every /api route except these two needs the passcode.
OPEN_BY_CONTRACT = {("GET", "/api/health"), ("POST", "/api/auth/verify")}
# The two catch-alls of f42-api, pinned so a third one is noticed: unknown /api paths answer 404 in the app's own
# error shape, and every other GET is the built single page app.
EXPECTED_CATCH_ALLS = {
    "api": {(m, "/api/{}") for m in ("GET", "POST", "PUT", "PATCH", "DELETE")} | {("GET", "/{}")},
    "agent": set(),
}

KNOWN_OPEN = {
    hr.label("api", "POST", "/api/findings"): "R4 route enumeration: POST /api/findings is served and not written in "
        "contract.md (the contract is lane 9's file)",
    hr.label("agent", "POST", "/api/findings"): "R4 route enumeration: POST /api/findings is served and not written "
        "in contract.md (the contract is lane 9's file)",
    hr.label("api", "POST", "/api/schedules/{}/resume"): "R4 route enumeration: contract.md names this route only as "
        "the shorthand `/resume` after the pause route, not as `POST /api/schedules/{id}/resume` (lane 9)",
    hr.label("agent", "POST", "/api/schedules/{}/resume"): "R4 route enumeration: contract.md names this route only "
        "as the shorthand `/resume` after the pause route (lane 9)",
    hr.label("agent", "GET", "/docs"): "R4 route enumeration: f42-agent serves FastAPI's interactive docs; f42-api "
        "switches them off with docs_url=None (agent_app.py is lane 2's file)",
    hr.label("agent", "GET", "/docs/oauth2-redirect"): "R4 route enumeration: as GET /docs, agent_app.py (lane 2)",
    hr.label("agent", "GET", "/openapi.json"): "R4 route enumeration: as GET /docs, agent_app.py (lane 2)",
    hr.label("agent", "GET", "/redoc"): "R4 route enumeration: as GET /docs, agent_app.py (lane 2)",
}


def known_open(case):
    if case in KNOWN_OPEN:
        return pytest.mark.xfail(strict=True, reason=KNOWN_OPEN[case])
    return ()


@pytest.fixture(scope="module")
def apps():
    from core.api import agent_app, app as api_app

    return {"api": api_app.app, "agent": agent_app.app}


@pytest.fixture(scope="module")
def served(apps):
    return {name: hr.app_routes(app) for name, app in apps.items()}


@pytest.fixture(scope="module")
def documented():
    return hr.contract_routes(CONTRACT.read_text(encoding="utf-8"))


def cases_served():
    from core.api import agent_app, app as api_app

    out = []
    for service, app in (("api", api_app.app), ("agent", agent_app.app)):
        named, _ = hr.app_routes(app)
        for method, path in sorted(named):
            case = hr.label(service, method, path)
            out.append(pytest.param(service, method, path, id=case, marks=known_open(case)))
    return out


def cases_documented():
    return [pytest.param(method, path, id=f"{method} {path}")
            for method, path in sorted(hr.contract_routes(CONTRACT.read_text(encoding="utf-8")))]


def cases_gated():
    from core.api import app as api_app

    named, _ = hr.app_routes(api_app.app)
    return [pytest.param(method, path, id=f"{method} {path}")
            for method, path in sorted(named - OPEN_BY_CONTRACT)]


def test_the_enumeration_sees_both_services_and_the_contract(served, documented):
    # Floors, not totals: they fail if a change makes the enumeration read nothing, not when a route is added.
    assert len(served["api"][0]) >= 55 and len(served["agent"][0]) >= 35
    assert len(documented) >= 55
    assert OPEN_BY_CONTRACT <= served["api"][0]
    assert ("GET", "/health") in served["agent"][0]


@pytest.mark.parametrize("service,method,path", cases_served())
def test_every_route_the_code_serves_is_written_in_the_contract(documented, service, method, path):
    assert (method, path) in documented, f"{service} serves {method} {path} and contract.md does not write it"


@pytest.mark.parametrize("method,path", cases_documented())
def test_every_route_the_contract_writes_is_served(served, method, path):
    assert (method, path) in served["api"][0] | served["agent"][0], (
        f"contract.md writes {method} {path} and neither service serves it")


def test_the_catch_all_routes_are_the_two_expected_ones(served):
    assert served["api"][1] == EXPECTED_CATCH_ALLS["api"]
    assert served["agent"][1] == EXPECTED_CATCH_ALLS["agent"]


CALLERS = itertools.count(1)


def caller():
    """A caller address of its own for each call, so the failed-passcode limiter never turns a 401 into a 429."""
    return {"X-Forwarded-For": f"203.0.113.{next(CALLERS) % 250 + 1}"}


@pytest.fixture
def passcode_mode(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "a-passcode-nobody-sends")
    monkeypatch.delenv("F42_AUTH_MODE", raising=False)
    monkeypatch.delenv("AGENT_URL", raising=False)


@pytest.mark.parametrize("method,path", cases_gated())
def test_every_api_route_but_the_two_open_ones_answers_401_without_a_passcode(passcode_mode, apps, method, path):
    client = TestClient(apps["api"], raise_server_exceptions=False)
    response = client.request(method, hr.concrete(path), headers=caller())
    assert response.status_code == 401, f"{method} {path} answered {response.status_code} with no passcode"
    assert response.json()["error"] == "unauthorized"


@pytest.mark.parametrize("method,path", cases_gated())
def test_a_wrong_passcode_is_refused_the_same_way(passcode_mode, apps, method, path):
    client = TestClient(apps["api"], raise_server_exceptions=False)
    response = client.request(method, hr.concrete(path), headers={**caller(), "X-Passcode": "wrong"})
    assert response.status_code == 401


def small_app(extra_routes=()):
    """A tiny app shaped like f42-api: a gated router, an open health route and whatever routes are asked for."""
    from core.api import auth

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(auth.ApiError)
    async def api_error(_request, error):
        return JSONResponse({"error": error.error, "message": error.message}, status_code=error.status)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    gated = APIRouter(dependencies=[Depends(auth.require_passcode)])

    @gated.get("/api/things/{thing_id}")
    def thing(thing_id: str):
        return {"id": thing_id}

    app.include_router(gated)
    for method, path in extra_routes:
        app.add_api_route(path, lambda: JSONResponse({"ok": True}), methods=[method])
    return app


class TestTheComparisonCanFail:
    CONTRACT = "`GET /api/health` and `GET /api/things/{id}`, then `POST /api/gone?x=1`."

    def test_contract_tokens_are_read_with_parameters_and_queries_normalised(self):
        assert hr.contract_routes(self.CONTRACT) == {
            ("GET", "/api/health"), ("GET", "/api/things/{}"), ("POST", "/api/gone")}

    def test_a_route_the_contract_does_not_write_is_reported(self):
        named, _ = hr.app_routes(small_app([("POST", "/api/extra/{extra_id}")]))
        assert hr.undocumented(named, hr.contract_routes(self.CONTRACT)) == [("POST", "/api/extra/{}")]

    def test_a_route_the_contract_writes_and_nothing_serves_is_reported(self):
        named, _ = hr.app_routes(small_app())
        assert hr.missing(hr.contract_routes(self.CONTRACT), named) == [("POST", "/api/gone")]

    def test_a_method_that_differs_is_a_different_route(self):
        named, _ = hr.app_routes(small_app([("PUT", "/api/things/{thing_id}")]))
        assert ("PUT", "/api/things/{}") in hr.undocumented(named, hr.contract_routes(self.CONTRACT))

    def test_a_catch_all_is_counted_apart_from_the_named_routes(self):
        app = small_app()
        app.add_api_route("/{rest:path}", lambda: None, methods=["GET"])
        named, catch_alls = hr.app_routes(app)
        assert ("GET", "/{}") in catch_alls and ("GET", "/{}") not in named

    def test_a_gated_route_answers_401_and_an_ungated_one_does_not(self, passcode_mode):
        app = small_app([("GET", "/api/loose")])
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/api/things/x", headers=caller()).status_code == 401
        assert client.get("/api/loose").status_code == 200

    def test_concrete_fills_every_parameter(self):
        assert hr.concrete("/api/a/{}/b/{}") == "/api/a/x/b/x"
