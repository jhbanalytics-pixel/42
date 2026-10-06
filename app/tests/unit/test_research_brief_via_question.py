"""Build cited brief through the reviewed question engine on staging.

Staging keeps the legacy brief writer closed. The availability record tells
the Build brief page that a cited brief can still be had there, by asking the
reviewed general question engine through Ask's own start path, and says so
without changing any field the page already reads.
"""

import pytest
from fastapi.testclient import TestClient

from src.api import main, research

client = TestClient(main.app)
STAGING = "open-intelligence-staging"


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


def test_staging_routes_the_brief_through_the_question_engine(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", STAGING)
    monkeypatch.delenv("BEHAVIOUR_SCAN_ENABLED", raising=False)
    body = client.get("/api/research/availability").json()
    assert body["brief_via_question"] is True
    assert body["brief_writing"] == {
        "available": False,
        "code": research.BRIEF_WRITING_UNAVAILABLE,
        "message": research.BRIEF_WRITING_UNAVAILABLE_MESSAGE,
    }
    assert body["behaviour_scan"]["available"] is False


def test_where_the_writer_runs_the_brief_is_not_routed_through_a_question(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("BEHAVIOUR_SCAN_ENABLED", "true")
    body = client.get("/api/research/availability").json()
    assert body["brief_via_question"] is False
    assert body["brief_writing"] == {"available": True, "code": None, "message": None}
    assert body["behaviour_scan"] == {"available": True, "code": None, "message": None}


def test_the_question_route_adds_no_model_path_to_research(monkeypatch):
    """Staging still refuses every brief generation route: the new field
    changes what the page offers, not what the research routes may run."""
    monkeypatch.setenv("DEPLOYMENT_PROFILE", STAGING)
    response = client.post(
        "/api/research/generate",
        json={"persona_id": "audience_neutral", "markets": ["za"]},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == research.BRIEF_WRITING_UNAVAILABLE
