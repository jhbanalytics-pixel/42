import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import general_question_routes, main
from src.api.question_worker_process import WorkerProcessError


ROUTE = "/api/internal/v2/fieldwork/question"
VERSION = "general_question_detail_v1"
RID = "00000000-0000-4000-8000-000000000001"
SCOPE = dict(
    client_scope_id="ogilvy_default",
    market_scope=["za"],
    brand_config_id=None,
    audience_lens_ids=[],
    theme_id=None,
)
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.setattr(general_question_routes, "_current_scope", lambda: SCOPE)


def service(monkeypatch, *, error=None, state="unconfirmed"):
    calls = []

    async def run(operation, payload, *, deadline):
        calls.append((operation, payload))
        assert operation == "observe"
        if error:
            raise WorkerProcessError(error)
        return SimpleNamespace(
            engine_reply=SimpleNamespace(
                reply={
                    "observed_state": state,
                    "reserved_microusd": 100000,
                    "missing_work": ["terminal_record_unavailable"]
                    if state == "unconfirmed"
                    else [],
                }
            )
        )

    monkeypatch.setattr(
        general_question_routes,
        "configured_service",
        lambda request: SimpleNamespace(
            worker=SimpleNamespace(run=run, storage_client=object())
        ),
    )
    return calls


@pytest.mark.parametrize(
    "query",
    [
        "",
        f"?contract_version={VERSION}",
        f"?contract_version={VERSION}&request_id=BAD",
        f"?contract_version={VERSION}&request_id={RID}&request_id={RID}",
        f"?contract_version={VERSION}&request_id={RID}&market=ng",
        f"?contract_version=unknown&request_id={RID}",
    ],
)
def test_invalid_detail_query_refuses_before_worker(monkeypatch, query):
    calls = service(monkeypatch)
    response = client.get(ROUTE + query)
    assert response.status_code == 400
    assert calls == []


def test_authentication_precedes_observation(monkeypatch):
    calls = service(monkeypatch)
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setenv("UI_PASSCODE", "fixture-passcode")
    response = client.get(f"{ROUTE}?contract_version={VERSION}&request_id={RID}")
    assert response.status_code == 401
    assert calls == []


@pytest.mark.parametrize(
    "error,status",
    [
        ("observation_request_unavailable", 404),
        ("observation_invalid", 503),
        ("worker_deadline_reached", 503),
    ],
)
def test_observer_errors_have_bounded_public_responses(monkeypatch, error, status):
    service(monkeypatch, error=error)
    response = client.get(f"{ROUTE}?contract_version={VERSION}&request_id={RID}")
    assert response.status_code == status
    assert error not in response.text


def test_unconfirmed_detail_preserves_question_without_start_or_status(monkeypatch):
    from src.api import question_worker_store

    calls = service(monkeypatch)
    request = dict(
        question="What changed?",
        history=[dict(role="user", text="Earlier question")],
        requested_window=None,
    )

    def read(storage_client, *, observe_input, observed_reply, deadline):
        assert observe_input["request_id"] == RID
        assert observe_input["scope"] == SCOPE
        return dict(
            request_bytes=json.dumps(request).encode(),
            intake_bytes=b'{"selected_market":"za"}',
            result_record=None,
            plan_window=None,
            # The stored request names no lens, which the reader reports rather
            # than omits, so the boundary record says general 42 explicitly.
            client_lens=None,
        )

    monkeypatch.setattr(question_worker_store, "read_observed_question_detail", read)
    response = client.get(f"{ROUTE}?contract_version={VERSION}&request_id={RID}")
    assert response.status_code == 200
    assert response.json() == dict(
        contract_version=VERSION,
        request_id=RID,
        observed_state="unconfirmed",
        question="What changed?",
        history=request["history"],
        selected_market="za",
        requested_window=None,
        response=None,
        window_from_plan=False,
        reserved_microusd=100000,
        missing_work=["terminal_record_unavailable"],
    )
    assert [item[0] for item in calls] == ["observe"]


DEFAULT_WINDOW = {"start": "2026-09-09", "end": "2026-09-22", "closed": True}
PLANNED_WINDOW = {"start": "2026-09-16", "end": "2026-09-22", "closed": True}


@pytest.mark.parametrize(
    "plan_digest,plan_window,reply_window,from_plan",
    [
        # No plan was stored: the reply carries the default window.
        (None, None, DEFAULT_WINDOW, False),
        # A clarification plan that named no window, then expiry or cancel:
        # the digest is bound, yet the reply carries the default window.
        ("a" * 64, None, DEFAULT_WINDOW, False),
        # A plan that named a window, and the reply carries it.
        ("a" * 64, PLANNED_WINDOW, PLANNED_WINDOW, True),
        ("a" * 64, DEFAULT_WINDOW, DEFAULT_WINDOW, True),
        # A plan window the reply does not carry is never claimed for it.
        ("a" * 64, PLANNED_WINDOW, DEFAULT_WINDOW, False),
    ],
)
def test_detail_says_whether_the_stored_window_came_from_a_plan(
    monkeypatch, plan_digest, plan_window, reply_window, from_plan
):
    """A reply written without a planned window carries a default, not a resolved one."""
    from src.api import question_worker_store

    service(monkeypatch, state="unavailable")
    stored = {
        "answer": "This request could not be completed within its execution window.",
        "sources": [],
        "error": True,
        "reason": "request_expired",
        "intelligence": {
            "status": "unavailable",
            "as_of": "2026-09-23T10:00:00+00:00",
            "window": reply_window,
        },
    }

    def read(storage_client, *, observe_input, observed_reply, deadline):
        return dict(
            request_bytes=json.dumps(
                dict(question="What changed?", history=[], requested_window=None)
            ).encode(),
            intake_bytes=b'{"selected_market":"za"}',
            result_record=dict(
                state="unavailable", plan_digest=plan_digest, response=stored
            ),
            plan_window=plan_window,
            client_lens=None,
        )

    monkeypatch.setattr(question_worker_store, "read_observed_question_detail", read)
    response = client.get(f"{ROUTE}?contract_version={VERSION}&request_id={RID}")
    assert response.status_code == 200
    body = response.json()
    assert body["window_from_plan"] is from_plan
    assert body["response"] == stored
    assert "plan_digest" not in body and "plan_window" not in body


def test_detail_is_private_and_read_only():
    assert ROUTE not in main.app.openapi().get("paths", {})
    for method in (client.post, client.put, client.patch, client.delete):
        assert method(
            f"{ROUTE}?contract_version={VERSION}&request_id={RID}"
        ).status_code in (404, 405)


def test_workspace_includes_question_inventory_when_investigations_are_unavailable(
    monkeypatch,
):
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures/workspaces/fieldwork_workspace_v2.json"
        ).read_text(encoding="utf-8")
    )["partial"]
    calls = []

    async def observe(request, request_id):
        calls.append(request_id)
        return dict(
            scope=SCOPE,
            coverage=fixture["operation_coverage"]["general_questions"],
            rows=fixture["research_operations"],
        )

    def unavailable():
        raise OSError("private detail")

    monkeypatch.setattr(general_question_routes, "read_question_observation", observe)
    monkeypatch.setattr(main, "_investigation_bucket", unavailable)
    response = client.get(
        "/api/internal/v2/fieldwork/read?contract_version=fieldwork_workspace_v2"
    )
    assert response.status_code == 200
    assert calls == [None]
    assert response.json()["operation_summary"]["known_total"] == 1
    assert response.json()["research_operations"][0]["status"] == "partial"
    assert response.json()["research_operation_count"] is None


LENS = dict(
    lens_binding_version="client_lens_binding_v1",
    client_lens_id="bsa_pulse_lens",
    configuration_digest="e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf",
)


def test_detail_names_the_client_lens_its_request_was_admitted_under(monkeypatch):
    """A reopened answer carries the lens its own stored request bytes bind, not the caller's."""
    from src.api import question_worker_store

    service(monkeypatch)

    def read(storage_client, *, observe_input, observed_reply, deadline):
        return dict(
            request_bytes=json.dumps(
                dict(question="What changed?", history=[], requested_window=None)
            ).encode(),
            intake_bytes=b'{"selected_market":"za"}',
            result_record=None,
            plan_window=None,
            client_lens=dict(LENS),
        )

    monkeypatch.setattr(question_worker_store, "read_observed_question_detail", read)
    response = client.get(f"{ROUTE}?contract_version={VERSION}&request_id={RID}")
    assert response.status_code == 200
    body = response.json()
    assert body["client_lens"] == LENS
    # Everything else keeps the general shape: the lens is the one added key.
    assert set(body) - {"client_lens"} == {
        "contract_version", "request_id", "observed_state", "question", "history",
        "selected_market", "requested_window", "response", "window_from_plan",
        "reserved_microusd", "missing_work",
    }
