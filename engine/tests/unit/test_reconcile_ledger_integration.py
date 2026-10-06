"""End-to-end fixtures wiring event_ledger -> reconcile on live-shaped data.

The two modules are unit-tested in isolation elsewhere (test_event_ledger.py,
test_reconcile.py). This file is the seam: it builds a real ledger (via
build_event_ledger, with only _fetch_factual_rows mocked) from row shapes
modelled on the 2026-07-04 live pull (Springboks vs England, Super Eagles,
Cyril Ramaphosa), then reconciles brief-shaped claim strings against it, so a
regression in either module's contract shows up here even if each module's
own tests stay green.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from src.analysis.event_ledger import build_event_ledger
from src.analysis.reconcile import reconcile_claims

TREND_DATE = date(2026, 7, 4)
ISSUE_DATE = TREND_DATE.isoformat()


def _gdelt_row(persons: str, published=None):
    return {
        "source": "gdelt",
        "content_type": "gdelt_gkg",
        "title": "",
        "text": f"TAX_FNCACT_LEADER WB_1249_SPORT {persons}",
        "query_term": "",
        "v2persons": f"{persons},1",
        "v2orgs": "",
        "v2locations": "",
        "v2tone": "0.0",
        "search_velocity_score": 0.0,
        "published_at": published or datetime(2026, 7, 4, 9, 0, tzinfo=UTC),
    }


def _rss_row(title: str, published=None):
    return {
        "source": "rss",
        "content_type": "article",
        "title": title,
        "text": "",
        "query_term": "",
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
        "v2tone": "",
        "search_velocity_score": 0.0,
        "published_at": published or datetime(2026, 7, 4, 10, 0, tzinfo=UTC),
    }


def _trends_row(term: str, velocity: float, published=None):
    return {
        "source": "bigquery_trends",
        "content_type": "search_term",
        "title": "",
        "text": "",
        "query_term": term,
        "v2persons": "",
        "v2orgs": "",
        "v2locations": "",
        "v2tone": "",
        "search_velocity_score": velocity,
        "published_at": published or datetime(2026, 7, 4, 8, 0, tzinfo=UTC),
    }


def test_springboks_vs_england_fixture_corroborates_a_matching_brief_claim(monkeypatch):
    import src.analysis.event_ledger as el

    rows = [
        _gdelt_row("Springboks"),
        _trends_row("springboks vs england", 0.9),
        _rss_row("Springboks vs England: full match preview"),
    ]
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: rows)
    ledger = build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")

    claim = "Springboks vs England this weekend is the headline fixture"
    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    assert out[0]["action"] == "corroborated"
    # The trends anchor "springboks vs england" is the longer, more specific
    # anchor phrase, so it wins the match over the shorter "springboks" one.
    assert out[0]["matched_entity_key"] == "springboks vs england"


def test_super_eagles_resolved_win_does_not_falsely_go_stale(monkeypatch):
    # A claim about the Super Eagles that carries no future marker must not be
    # flagged stale even though the matched event is resolved: reconcile only
    # corrects a claim that is WRONG about the future, not one already
    # agreeing with the past.
    import src.analysis.event_ledger as el

    rows = [
        _gdelt_row("Super Eagles"),
        _rss_row("Super Eagles thrash Zimbabwe to book World Cup spot"),
    ]
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: rows)
    ledger = build_event_ledger(TREND_DATE, "ng", client=None, bq_client=object(), dataset="ds")

    claim = "Super Eagles fans celebrate the World Cup qualifying win"
    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    assert out[0]["action"] != "stale"
    assert out[0]["matched_entity_key"] == "super eagles"


def test_ramaphosa_stale_claim_corrects_to_ledger_state_text(monkeypatch):
    # A brief still describing an upcoming Ramaphosa address, when the ledger
    # (two factual families, fresh, high confidence) has since resolved it via
    # the reported-speech pattern, should correct verbatim to state_text.
    import src.analysis.event_ledger as el

    rows = [
        _gdelt_row("Cyril Ramaphosa"),
        _rss_row("President Cyril Ramaphosa told the nation the crisis is over"),
    ]
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: rows)
    ledger = build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")

    claim = "Ahead of his upcoming address, Ramaphosa is set to speak on the crisis"
    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    assert out[0]["action"] == "stale"
    assert out[0]["claim_after"] == "President Cyril Ramaphosa told the nation the crisis is over"
    assert out[0]["receipt_ids"]


def test_filter_claims_to_ledger_keeps_only_anchored_claims():
    """The pre-filter reads _match_event's result; pin its contract.

    _match_event returns (event, matched_phrase), and a tuple is always truthy,
    so a caller that forgets to unpack it silently keeps every claim.
    """
    from src.analysis.claim_gathering import _filter_claims_to_ledger

    ledger = [
        {
            "entity_key": "super eagles",
            "entity_aliases": ["Eagles"],
            "event_kind": "entity",
            "state_label": "resolved",
            "state_text": "Super Eagles beat Ghana",
            "as_of": "2026-07-03",
            "corroborating_sources": ["rss", "gdelt"],
            "source_count": 2,
            "confidence": 0.8,
        }
    ]
    kept = _filter_claims_to_ledger(
        ["Super Eagles fans celebrate", "A quiet local story about nothing"], ledger
    )
    assert kept == ["Super Eagles fans celebrate"]


def test_headline_only_evidence_anchors_a_body_claim(monkeypatch):
    """Two headline-only rows (rss + youtube), no GDELT/trends anchor.

    Before name-anchor extraction this evidence died at clustering and the
    claim could never anchor; now the extracted 'Cyril Ramaphosa' anchor
    clusters across two families and binds the brief body claim.
    """
    import src.analysis.event_ledger as el

    youtube = dict(_rss_row("Cyril Ramaphosa cabinet reshuffle explained"))
    youtube["source"] = "youtube"
    youtube["content_type"] = "video"
    rows = [
        _rss_row("Cyril Ramaphosa announces cabinet reshuffle"),
        youtube,
    ]
    monkeypatch.setattr(el, "_fetch_factual_rows", lambda *a, **k: rows)
    ledger = build_event_ledger(TREND_DATE, "za", client=None, bq_client=object(), dataset="ds")
    keys = {r["entity_key"] for r in ledger}
    assert "cyril ramaphosa" in keys

    claim = "President Cyril Ramaphosa announced a cabinet reshuffle this week."
    actions = reconcile_claims([claim], ledger, ISSUE_DATE)
    assert len(actions) == 1
    assert actions[0]["action"] != "no_match"
    assert actions[0]["matched_entity_key"] == "cyril ramaphosa"
