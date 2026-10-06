import importlib

import pytest
from fastapi.testclient import TestClient


class BombClient:
    @property
    def models(self):
        raise AssertionError("legacy Vertex client was reached")


def modules():
    return tuple(
        importlib.import_module(name) for name in ("src.api.synth", "src.api.chat")
    )


def staging(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "open-intelligence-staging")
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


def test_staging_profile_refuses_both_direct_vertex_seams_before_existing_client(
    monkeypatch,
):
    synth, chat = modules()
    staging(monkeypatch)
    monkeypatch.setattr(synth, "_client", BombClient())
    monkeypatch.setattr(chat, "_client", BombClient())

    with pytest.raises(RuntimeError, match="legacy_vertex_generation_refused"):
        synth._call_model("synthetic")
    with pytest.raises(RuntimeError, match="legacy_vertex_generation_refused"):
        chat._get_client()


def test_nonstaging_direct_client_behavior_is_preserved(monkeypatch):
    synth, chat = modules()
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "production")

    class Models:
        def generate_content(self, **_kwargs):
            return type("Response", (), {"text": "synthetic response"})()

    client = type("Client", (), {"models": Models()})()
    monkeypatch.setattr(synth, "_client", client)
    monkeypatch.setattr(chat, "_client", client)
    assert synth._call_model("synthetic") == "synthetic response"
    assert chat._get_client() is client


def test_staging_http_refine_returns_controlled_failure_without_model_success(
    monkeypatch,
):
    synth, _chat = modules()
    main = importlib.import_module("src.api.main")
    staging(monkeypatch)
    monkeypatch.setattr(synth, "_client", BombClient())
    main._ask_hits.clear()
    response = TestClient(main.app).post(
        "/api/ask/refine",
        json={
            "topic": {"generated": True, "region": "ZA", "brief": {}},
            "instruction": "Tighten the synthetic wording",
        },
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "Brief refinement is unavailable right now"}


def test_staging_legacy_fallbacks_return_no_generated_success(monkeypatch):
    synth, _chat = modules()
    staging(monkeypatch)
    monkeypatch.setattr(synth, "_client", BombClient())
    assert synth.generate_opportunity("topic", "2026-09-07", "signal", "context") == ""
    assert synth.synthesize_brief("synthetic", "za") is None
    assert (
        synth.refine_brief_prose(
            {"generated": True, "region": "ZA", "topic": "Synthetic", "brief": {}},
            "Tighten it",
        )
        is None
    )
    assert (
        synth.synthesize_research(
            {
                "persona_label": "Synthetic",
                "markets": ["za"],
                "refs": [],
                "quality_gate": {},
            }
        )
        is None
    )


def test_staging_cached_brief_and_durable_chat_do_not_touch_legacy_vertex(monkeypatch):
    synth, chat = modules()
    main = importlib.import_module("src.api.main")
    staging(monkeypatch)
    monkeypatch.setattr(synth, "_client", BombClient())
    monkeypatch.setattr(chat, "_client", BombClient())
    main._ask_hits.clear()
    synth._CACHE.clear()
    synth.cache_set(
        ("brief", "cached query", "za"), {"generated": True, "cached": True}
    )
    client = TestClient(main.app)
    cached = client.post(
        "/api/ask/brief", json={"query": "cached query", "region": "za"}
    )
    assert cached.status_code == 200
    assert cached.json() == {"generated": True, "cached": True}

    async def start_route(_request, value):
        assert value["message"] == "synthetic question"
        return {"job_id": "chat_" + "1" * 32}

    monkeypatch.setattr(main.general_question_routes, "start_route", start_route)
    durable = client.post(
        "/api/chat/send",
        json={"message": "synthetic question", "history": [], "market": "za"},
    )
    assert durable.status_code == 202
    assert durable.json() == {"job_id": "chat_" + "1" * 32}


def test_consolidated_research_cannot_turn_staging_refusal_into_thin_success(
    monkeypatch,
):
    synth, _chat = modules()
    staging(monkeypatch)
    monkeypatch.setattr(synth, "_client", BombClient())
    with pytest.raises(RuntimeError, match="legacy_vertex_generation_refused"):
        synth.synthesize_consolidated_research(
            {
                "persona_label": "Synthetic",
                "markets": ["za"],
                "refs": [],
                "behaviour_sections": [],
            }
        )
