"""Unit tests for the PULSE Intelligence Core SHADOW wiring in run_rss_now.

Covers two things:

1. The RECONCILE_ENABLED gate is inert by default. With the flag unset or
   "false" the new stage is skipped: build_and_persist_ledger and
   reconcile_claims are NOT called. With the flag "true" the stage runs and
   both are called (against a mocked ledger / persist).
2. The shadow helper computes + persists ONLY: it builds the ledger, reconciles
   each market's claims, persists the action rows, and returns a per-market
   summary, without mutating briefs_by_topic.
"""

from datetime import date
from unittest.mock import patch

import pytest


@pytest.fixture
def load_run_module():
    """Import run_rss_now fresh so monkey-patches do not leak."""
    import importlib
    import sys

    sys.path.insert(0, "scripts")
    import run_rss_now  # type: ignore

    importlib.reload(run_rss_now)
    return run_rss_now


def _briefs():
    return {
        ("za", "sport"): {
            "market": "za",
            "topic": "sport",
            "headline": "Bafana to face Nigeria this weekend",
            "trend_synthesis": (
                "Bafana prepare for the clash against Nigeria this coming weekend. "
                "Fans are hopeful."
            ),
            "cultural_context": "Run a creator challenge before match day.",
            "description_rationale": (
                "Bafana prepare for the clash against Nigeria this coming weekend. "
                "Fans are hopeful."
            ),
            "social_refs": [
                "https://example.com/a | Web | Bafana Bafana vs Nigeria fixture set",
                "https://example.com/b | TikTok | Fans react to the squad drop",
                "https://example.com/c | Google Search | bafana bafana nigeria fixture",
                "https://example.com/d | YouTube | Bafana vs Nigeria Match Highlights",
                "https://example.com/e | YouTube | Amapiano Sunset Mix 2025",
                "https://example.com/f | Twitter | Jadon Sancho specifically requested one-on-one training with Benni McCarthy",
            ],
        },
        ("ng", "music"): {
            "market": "ng",
            "topic": "music",
            "headline": "Burna Boy drops a new single",
            "trend_synthesis": "The single trends nationwide.",
            "cultural_context": "Create an Afrobeats lookbook.",
            "description_rationale": "The single trends nationwide.",
            "social_refs": [
                "https://example.com/d | Web | Burna Boy drops new single",
            ],
        },
    }


# --- claim gathering -------------------------------------------------------


def test_claims_for_market_uses_headline_and_body_sentences(load_run_module):
    claims = load_run_module._reconcile_claims_for_market("za", _briefs())
    # Every headline is a claim: the digest renders it.
    assert "Bafana to face Nigeria this weekend" in claims
    # Body sentences of claim size are claims.
    assert "Bafana prepare for the clash against Nigeria this coming weekend." in claims
    # A too-short body fragment carries no anchor signal and is dropped.
    assert "Fans are hopeful." not in claims
    # Source-ref titles are evidence inputs, never published claims.
    assert "Bafana Bafana vs Nigeria fixture set" not in claims
    assert "bafana bafana nigeria fixture" not in claims
    assert "Bafana vs Nigeria Match Highlights" not in claims
    assert "Fans react to the squad drop" not in claims
    # Activation copy (cultural_context) is not part of the claim surface.
    assert "Run a creator challenge before match day." not in claims
    # ng brief must not leak into za's claim list.
    assert all("Burna Boy" not in c for c in claims)


def test_claims_for_market_keeps_editorial_headline_without_fixture_markers(load_run_module):
    claims = load_run_module._reconcile_claims_for_market("ng", _briefs())
    assert "Burna Boy drops a new single" in claims


def test_claims_are_deduped_and_capped(load_run_module):
    briefs = {}
    for i in range(60):
        briefs[("za", f"topic{i}")] = {
            "market": "za",
            "topic": f"topic{i}",
            "headline": f"Headline number {i} lands across South African feeds",
            "trend_synthesis": "",
            "description_rationale": "",
            "social_refs": [],
        }
    claims = load_run_module._reconcile_claims_for_market("za", briefs)
    assert len(claims) == 40
    # Duplicate headlines collapse.
    dup = {
        ("za", "a"): {"market": "za", "headline": "Same headline for both topics today"},
        ("za", "b"): {"market": "za", "headline": "Same headline for both topics today"},
    }
    assert load_run_module._reconcile_claims_for_market("za", dup) == [
        "Same headline for both topics today"
    ]


def test_body_sentence_splitter_bounds():
    from src.analysis.claim_gathering import _body_sentences_for_reconcile

    brief = {
        "description_rationale": (
            "Springboks beat England at Ellis Park on Saturday. Short one. " + "word " * 45 + "end."
        ),
        "trend_synthesis": "Springboks beat England at Ellis Park on Saturday. Another real sentence about the squad rotation plans.",
    }
    sentences = _body_sentences_for_reconcile(brief)
    assert "Springboks beat England at Ellis Park on Saturday." in sentences
    assert "Another real sentence about the squad rotation plans." in sentences
    # Deduped across the two fields.
    assert sentences.count("Springboks beat England at Ellis Park on Saturday.") == 1
    # Too short and too long both dropped.
    assert "Short one." not in sentences
    assert not any(len(s.split()) > 40 for s in sentences)


def test_claims_for_market_empty_when_no_brief_matches(load_run_module):
    assert load_run_module._reconcile_claims_for_market("ke", _briefs()) == []


# --- shadow helper ---------------------------------------------------------


def test_reconcile_shadow_builds_reconciles_persists(load_run_module):
    briefs = _briefs()
    ledger = {"za": [{"entity_key": "bafana"}], "ng": [], "ke": []}

    with (
        patch("src.analysis.gemini_client.GeminiClient"),
        patch(
            "src.analysis.event_ledger.build_and_persist_ledger", return_value=ledger
        ) as m_ledger,
        patch(
            "src.analysis.reconcile.reconcile_claims",
            return_value=[{"action": "stale"}, {"action": "labelled"}],
        ) as m_reconcile,
        patch("src.analysis.reconcile.persist_reconcile_actions", return_value=2) as m_persist,
    ):
        summary = load_run_module._reconcile_shadow(date(2026, 6, 27), "rid", briefs)

    m_ledger.assert_called_once()
    # za + ng have claims, ke has none -> reconcile + persist called twice.
    assert m_reconcile.call_count == 2
    assert m_persist.call_count == 2
    assert summary["za"][1] == 1  # one stale
    assert summary["za"][2] == 1  # one labelled
    assert summary["ke"] == (0, 0, 0)
    # SHADOW: briefs untouched.
    assert briefs == _briefs()


def test_reconcile_shadow_filters_unanchorable_claims_before_reconcile(load_run_module):
    briefs = _briefs()
    ledger = {
        "za": [{"entity_key": "bafana bafana", "entity_aliases": ["Bafana"]}],
        "ng": [],
        "ke": [],
    }

    with (
        patch("src.analysis.gemini_client.GeminiClient"),
        patch("src.analysis.event_ledger.build_and_persist_ledger", return_value=ledger),
        patch(
            "src.analysis.reconcile.reconcile_claims",
            return_value=[{"action": "labelled"}],
        ) as m_reconcile,
        patch("src.analysis.reconcile.persist_reconcile_actions", return_value=1),
    ):
        summary = load_run_module._reconcile_shadow(date(2026, 6, 27), "rid", briefs)

    claims = m_reconcile.call_args_list[0].args[0]
    assert (
        "Jadon Sancho specifically requested one-on-one training with Benni McCarthy" not in claims
    )
    assert "Bafana to face Nigeria this weekend" in claims
    assert summary["za"][0] == len(claims)


# --- the gate is inert by default -----------------------------------------


def _stage(run_mod, briefs_by_topic):
    """Replicate the exact RECONCILE_ENABLED guard the run() stage uses."""
    import os

    if os.environ.get("RECONCILE_ENABLED", "false").lower() == "true" and briefs_by_topic:
        run_mod._reconcile_shadow(date(2026, 6, 27), "rid", briefs_by_topic)


def test_stage_inert_when_flag_unset(load_run_module, monkeypatch):
    monkeypatch.delenv("RECONCILE_ENABLED", raising=False)
    with (
        patch("src.analysis.event_ledger.build_and_persist_ledger") as m_ledger,
        patch("src.analysis.reconcile.reconcile_claims") as m_reconcile,
        patch("src.analysis.reconcile.persist_reconcile_actions") as m_persist,
    ):
        _stage(load_run_module, _briefs())

    m_ledger.assert_not_called()
    m_reconcile.assert_not_called()
    m_persist.assert_not_called()


def test_stage_inert_when_flag_false(load_run_module, monkeypatch):
    monkeypatch.setenv("RECONCILE_ENABLED", "false")
    with (
        patch("src.analysis.event_ledger.build_and_persist_ledger") as m_ledger,
        patch("src.analysis.reconcile.reconcile_claims") as m_reconcile,
    ):
        _stage(load_run_module, _briefs())

    m_ledger.assert_not_called()
    m_reconcile.assert_not_called()


def test_stage_runs_when_flag_true(load_run_module, monkeypatch):
    monkeypatch.setenv("RECONCILE_ENABLED", "true")
    ledger = {"za": [], "ng": [], "ke": []}
    with (
        patch("src.analysis.gemini_client.GeminiClient"),
        patch(
            "src.analysis.event_ledger.build_and_persist_ledger", return_value=ledger
        ) as m_ledger,
        patch("src.analysis.reconcile.reconcile_claims", return_value=[]) as m_reconcile,
        patch("src.analysis.reconcile.persist_reconcile_actions", return_value=0),
    ):
        _stage(load_run_module, _briefs())

    m_ledger.assert_called_once()
    assert m_reconcile.call_count == 2  # za + ng have claims


# --- Gemini client wiring ---------------------------------------------------


def test_reconcile_shadow_passes_gemini_client_when_available(load_run_module):
    briefs = _briefs()
    ledger = {"za": [], "ng": [], "ke": []}
    fake_client = object()

    with (
        patch("src.analysis.gemini_client.GeminiClient", return_value=fake_client) as m_client,
        patch(
            "src.analysis.event_ledger.build_and_persist_ledger", return_value=ledger
        ) as m_ledger,
        patch("src.analysis.reconcile.reconcile_claims", return_value=[]),
        patch("src.analysis.reconcile.persist_reconcile_actions", return_value=0),
    ):
        load_run_module._reconcile_shadow(date(2026, 6, 27), "rid", briefs)

    m_client.assert_called_once()
    m_ledger.assert_called_once_with(date(2026, 6, 27), client=fake_client)


def test_reconcile_shadow_falls_back_when_gemini_client_raises(load_run_module):
    briefs = _briefs()
    ledger = {"za": [], "ng": [], "ke": []}

    with (
        patch("src.analysis.gemini_client.GeminiClient", side_effect=RuntimeError("no creds")),
        patch(
            "src.analysis.event_ledger.build_and_persist_ledger", return_value=ledger
        ) as m_ledger,
        patch("src.analysis.reconcile.reconcile_claims", return_value=[]),
        patch("src.analysis.reconcile.persist_reconcile_actions", return_value=0),
    ):
        summary = load_run_module._reconcile_shadow(date(2026, 6, 27), "rid", briefs)

    m_ledger.assert_called_once_with(date(2026, 6, 27), client=None)
    assert summary["za"][1:] == (0, 0)  # no stale/labelled actions from the empty reconcile
