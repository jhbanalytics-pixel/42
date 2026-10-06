"""Build cited brief on the staging profile.

Staging refuses the legacy model seam by contract (test_legacy_vertex_guard.py),
and the release controller pins a closed environment without the behaviour
scan flag. These tests pin how that contract reaches the reader: in plain
words, before any work starts, and never as a raw refusal code.
"""

import re

import pytest
from fastapi.testclient import TestClient

from src.api import main, research, synth

client = TestClient(main.app)
RAW_CODE = re.compile(r"[a-z]+_[a-z_]+")
STAGING = "open-intelligence-staging"


class BombClient:
    @property
    def models(self):
        raise AssertionError("legacy Vertex client was reached")


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.setenv("CACHE_BUCKET", "")
    monkeypatch.setattr(synth, "_client", BombClient())
    synth._CACHE.clear()
    main._research_hits.clear()
    with main._research_jobs_lock:
        main._research_jobs.clear()
    with main._generation_capacity_lock:
        main._generation_active = 0
    yield
    synth._CACHE.clear()
    main._research_hits.clear()


def staging(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", STAGING)


def plain(message):
    assert isinstance(message, str) and message.strip()
    assert RAW_CODE.search(message) is None, message
    assert "legacy" not in message.lower()
    assert "vertex" not in message.lower()
    return message


def test_availability_reports_brief_writing_off_on_staging_in_plain_words(monkeypatch):
    staging(monkeypatch)
    monkeypatch.delenv("BEHAVIOUR_SCAN_ENABLED", raising=False)
    response = client.get("/api/research/availability")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"brief_writing", "behaviour_scan", "brief_via_question"}
    assert body["brief_writing"]["available"] is False
    assert body["brief_writing"]["code"] == "brief_writing_unavailable"
    assert "staging" in plain(body["brief_writing"]["message"]).lower()
    assert "Ask" in body["brief_writing"]["message"]
    assert body["behaviour_scan"]["available"] is False
    plain(body["behaviour_scan"]["message"])


def test_availability_reports_brief_writing_on_outside_staging(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("BEHAVIOUR_SCAN_ENABLED", "true")
    body = client.get("/api/research/availability").json()
    assert body == {
        "brief_writing": {"available": True, "code": None, "message": None},
        "behaviour_scan": {"available": True, "code": None, "message": None},
        "brief_via_question": False,
    }


def test_availability_requires_the_passcode(monkeypatch):
    staging(monkeypatch)
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setenv("UI_PASSCODE", "fixture-passcode")
    assert client.get("/api/research/availability").status_code == 401


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/research/generate", {"persona_id": "audience_neutral", "markets": ["za"]}),
        (
            "/api/research/generate-batch",
            {
                "persona_id": "audience_neutral",
                "behaviours": [{"market": "za", "label": "Street football"}],
            },
        ),
        (
            "/api/research/generate-consolidated",
            {
                "persona_id": "audience_neutral",
                "markets": ["za"],
                "behaviours": [{"market": "za", "label": "Street football"}],
            },
        ),
    ],
)
def test_staging_refuses_brief_generation_before_any_job_starts(monkeypatch, path, payload):
    staging(monkeypatch)
    started = []
    monkeypatch.setattr(
        main, "_start_generation_thread", lambda *args: started.append(args)
    )
    response = client.post(path, json=payload)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "brief_writing_unavailable"
    plain(detail["message"])
    assert started == []
    with main._generation_capacity_lock:
        assert main._generation_active == 0


def test_behaviour_scan_off_answers_with_a_plain_reason(monkeypatch):
    monkeypatch.delenv("BEHAVIOUR_SCAN_ENABLED", raising=False)
    response = client.get("/api/research/behaviours?markets=za")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "behaviour_scan_off"
    assert "Pick topics manually" in plain(detail["message"])


def _inference_evidence(*_args, **_kwargs):
    return {
        "persona_id": "audience_neutral",
        "persona_label": "Audience neutral",
        "markets": ["za"],
        "refs": [{"id": "ref_1", "text": "A cited post."}],
        "quality_gate": {"level": "strong", "allow_inference": True},
        "signal_quality": "strong",
        "degraded_reasons": [],
    }


def test_staging_research_job_that_needs_the_writer_fails_in_plain_words(monkeypatch):
    staging(monkeypatch)
    monkeypatch.setattr(research, "build_research_evidence", _inference_evidence)
    result = research.run_research_job("audience_neutral", ["za"], None)
    assert result["error"] is True
    assert result["code"] == "brief_writing_unavailable"
    assert plain(result["message"]) != "Synthesis unavailable for this evidence set."


def test_nonstaging_synthesis_failure_keeps_its_existing_message(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "production")
    monkeypatch.setattr(research, "build_research_evidence", _inference_evidence)
    monkeypatch.setattr(synth, "synthesize_research", lambda evidence: None)
    result = research.run_research_job("audience_neutral", ["za"], None)
    assert result["message"] == "Synthesis unavailable for this evidence set."
    assert "code" not in result


def _consolidated_evidence(*_args, **_kwargs):
    return {
        "persona_id": "audience_neutral",
        "persona_label": "Audience neutral",
        "markets": ["za"],
        "refs": [],
        "behaviour_sections": [],
        "degraded_reasons": [],
    }


def test_staging_consolidated_job_never_shows_the_raw_refusal(monkeypatch):
    staging(monkeypatch)
    monkeypatch.setattr(research, "build_consolidated_evidence", _consolidated_evidence)
    key = ("research", "research_consolidated_" + "a" * 32)
    with main._research_jobs_lock:
        main._research_jobs.add(key)
    main._run_research_consolidated_job(key, "audience_neutral", None, ["za"], [{"market": "za"}])
    status = client.get("/api/research/status?job_id=" + key[1]).json()
    assert status["error"] is True
    assert status["code"] == "brief_writing_unavailable"
    assert "legacy_vertex_generation_refused" not in plain(status["message"])


@pytest.mark.parametrize(
    "runner",
    ["_run_research_job", "_run_research_batch_job", "_run_research_consolidated_job"],
)
def test_an_unexpected_job_failure_does_not_show_exception_text(monkeypatch, runner):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "production")

    def explode(*_args, **_kwargs):
        raise RuntimeError("internal_state_token at /srv/secret/path")

    monkeypatch.setattr(research, "run_research_job", explode)
    monkeypatch.setattr(research, "run_consolidated_research_job", explode)
    key = ("research", "research_" + "b" * 32)
    with main._research_jobs_lock:
        main._research_jobs.add(key)
    target = getattr(main, runner)
    if runner == "_run_research_job":
        target(key, "audience_neutral", ["za"], None)
    elif runner == "_run_research_batch_job":
        target(key, "audience_neutral", None, [{"market": "za"}])
    else:
        target(key, "audience_neutral", None, ["za"], [{"market": "za"}])
    status = client.get("/api/research/status?job_id=" + key[1]).json()
    assert status["error"] is True
    assert "internal_state_token" not in status["message"]
    assert "/srv/secret/path" not in status["message"]
    plain(status["message"])


def test_an_unknown_persona_keeps_its_readable_reason(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "production")

    def unknown(*_args, **_kwargs):
        raise research.PersonaResolveError("Unknown or disabled persona: ghost")

    monkeypatch.setattr(research, "run_research_job", unknown)
    key = ("research", "research_" + "c" * 32)
    with main._research_jobs_lock:
        main._research_jobs.add(key)
    main._run_research_job(key, "ghost", ["za"], None)
    status = client.get("/api/research/status?job_id=" + key[1]).json()
    assert status["message"] == "Unknown or disabled persona: ghost"


def test_the_staging_guard_itself_is_unchanged(monkeypatch):
    staging(monkeypatch)
    with pytest.raises(RuntimeError, match="legacy_vertex_generation_refused"):
        synth._call_model("synthetic")
    assert synth.legacy_generation_refused() is True
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "production")
    assert synth.legacy_generation_refused() is False
