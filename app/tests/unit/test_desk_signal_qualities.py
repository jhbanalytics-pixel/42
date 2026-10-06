"""Batch D: the engine's six scores reach the strategist as words.

The engine measures novelty, velocity, breadth, source independence, historical
similarity and geographic confidence as numbers on signal_candidates_v2. A
number is engine vocabulary: it invites a reader to rank on a scale nobody
published and to treat 0.61 as meaningfully above 0.58. The desk therefore
carries bands, in the strategist's language, and never the score that produced
them.
"""

from datetime import date

import pytest

from src.api import desk


def test_every_quality_band_is_a_word_a_strategist_already_uses():
    assert desk.SIGNAL_QUALITY_FIELDS == (
        "velocity",
        "novelty",
        "breadth",
        "independence",
        "history",
        "geo_confidence",
    )
    for field in desk.SIGNAL_QUALITY_FIELDS:
        for band in desk.QUALITY_BANDS[field]:
            assert band == band.lower()
            assert not any(character.isdigit() for character in band)


@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, "low"),
        (0.33, "low"),
        (0.34, "moderate"),
        (0.66, "moderate"),
        (0.67, "high"),
        (1.0, "high"),
    ],
)
def test_a_score_bands_at_its_published_boundaries(score, expected):
    assert desk.quality_band("novelty", score) == expected


def test_a_band_boundary_mutation_is_killed(monkeypatch):
    monkeypatch.setattr(desk, "_QUALITY_MODERATE_FLOOR", 0.33)
    with pytest.raises(AssertionError):
        assert desk.quality_band("novelty", 0.33) == "low"


def test_an_absent_or_unreadable_score_is_unmeasured_never_zero():
    """A missing measurement is not a low measurement. Rendering None as the
    bottom band would state a finding the run never made."""
    assert desk.quality_band("novelty", None) == "unmeasured"
    assert desk.quality_band("novelty", "") == "unmeasured"
    assert desk.quality_band("novelty", float("nan")) == "unmeasured"


def test_qualities_carry_bands_and_topic_tags_but_never_a_raw_score():
    row = {
        "velocity_score": 0.67,
        "novelty_score": 0.82,
        "breadth_score": 0.31,
        "independence_score": 0.55,
        "historical_similarity": None,
        "geo_confidence": 0.91,
        "topic_tags": ["street_football", "youth_culture"],
    }
    qualities = desk.signal_qualities(row)

    assert qualities == {
        "velocity": "high",
        "novelty": "high",
        "breadth": "low",
        "independence": "moderate",
        "history": "unmeasured",
        "geo_confidence": "high",
        "topic_tags": ["street_football", "youth_culture"],
    }
    serialized = repr(qualities)
    for leaked in ("0.82", "0.31", "0.55", "0.91", "score", "signal_candidates"):
        assert leaked not in serialized


def test_topic_tags_are_optional_labels_and_never_invented():
    measured = {
        "velocity_score": 0.5,
        "novelty_score": 0.5,
        "breadth_score": 0.5,
        "independence_score": 0.5,
        "historical_similarity": None,
        "geo_confidence": 0.5,
    }
    assert desk.signal_qualities(measured)["topic_tags"] == []
    assert desk.signal_qualities({**measured, "topic_tags": None})["topic_tags"] == []
    assert desk.signal_qualities({**measured, "topic_tags": "street_football"}) is None


def test_the_served_signal_carries_qualities_and_still_no_engine_vocabulary():
    """The contract member the client reads. Bands travel; scores, table names
    and the raw candidate row do not."""
    run = {
        "run_id": "run_20260827_dynamic_apply_v1",
        "signal_date": "2026-08-27",
        "market_scope": ("za",),
        "observation_start": "2026-08-01",
        "observation_end": "2026-08-26",
        "observation_method": "dynamic_source_copy_apply_v1",
    }
    row = {
        "contract_version": "desk_dynamic_signal_v2",
        "run_id": run["run_id"],
        "signal_date": date(2026, 8, 27),
        "market": "za",
        "signal_id": "sig_" + "a" * 64,
        "signal_name": "Street football owns the evening",
        "discovery_mode": "phrase",
        "evidence_state": "ready",
        "why_now": "Clips moved into a shared format.",
        "possible_response": "Brief one local pitch.",
        "receipts": [],
        "velocity_score": 0.67,
        "novelty_score": 0.9,
        "breadth_score": 0.2,
        "independence_score": 0.5,
        "historical_similarity": 0.7,
        "geo_confidence": 0.91,
        "topic_tags": ["street_football"],
    }

    served = desk._admitted_signal(row, run)
    assert served is not None
    signal = served["signal"]
    assert signal["qualities"] == {
        "velocity": "high",
        "novelty": "high",
        "breadth": "low",
        "independence": "moderate",
        "history": "high",
        "geo_confidence": "high",
        "topic_tags": ["street_football"],
    }
    for key in signal:
        assert "score" not in key
    body = repr(signal)
    for leaked in ("0.9", "0.2", "signal_candidates_v2", "trends_v2"):
        assert leaked not in body


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_version", "desk_dynamic_signal_v1"),
        ("velocity_score", None),
        ("velocity_score", "0.8"),
        ("novelty_score", -0.01),
        ("breadth_score", 1.01),
        ("independence_score", float("inf")),
        ("geo_confidence", float("nan")),
        ("topic_tags", ["repair", "repair"]),
    ],
)
def test_wrong_version_or_invalid_required_quality_poisons_the_signal(field, value):
    run = {
        "run_id": "run_20260827_dynamic_apply_v2",
        "signal_date": "2026-08-27",
        "market_scope": ("za",),
        "observation_start": "2026-08-14",
        "observation_end": "2026-08-27",
        "observation_method": "dynamic_source_copy_apply_v1",
    }
    row = {
        "contract_version": "desk_dynamic_signal_v2",
        "run_id": run["run_id"],
        "signal_date": date(2026, 8, 27),
        "market": "za",
        "signal_id": "sig_" + "a" * 64,
        "signal_name": "Repair tutorials moving into weekend routines",
        "discovery_mode": "phrase",
        "evidence_state": "unchecked",
        "why_now": None,
        "possible_response": None,
        "receipts": [],
        "velocity_score": 0.67,
        "novelty_score": 0.66,
        "breadth_score": 0.34,
        "independence_score": 0.33,
        "historical_similarity": None,
        "geo_confidence": 1.0,
        "topic_tags": ["repair", "participation"],
    }
    row[field] = value

    assert desk._admitted_signal(row, run) is None


def _instrument_row(**over):
    row = {
        "contract_version": "desk_dynamic_signal_v2",
        "run_id": "run_20260903_dynamic_apply_v2_r16",
        "signal_date": date(2026, 9, 3),
        "market": "ke",
        "signal_id": "sig_" + "e" * 64,
        "signal_name": "nganya",
        "discovery_mode": "phrase",
        "evidence_state": "ready",
        "why_now": None,
        "possible_response": None,
        "receipts": [],
        "velocity_score": 0.5,
        "novelty_score": 0.5,
        "breadth_score": 0.5,
        "independence_score": 0.5,
        "historical_similarity": None,
        "geo_confidence": 0.4,
        "topic_tags": ["matatu"],
    }
    row.update(over)
    return row


_INSTRUMENT_RUN = {
    "run_id": "run_20260903_dynamic_apply_v2_r16",
    "signal_date": "2026-09-03",
    "market_scope": ("za", "ng", "ke"),
    "observation_start": "2026-08-21",
    "observation_end": "2026-09-03",
    "observation_method": "dynamic_source_copy_apply_v1",
}

_SUMMARY = {
    "contractVersion": "1.0.0",
    "state": "ready",
    "receipts": [],
    "independence": {
        "status": "validated",
        "familyCount": 2,
        "groupingAuthority": "channel_family_v2",
    },
    "direction": {
        "status": "agree",
        "supportingReceiptIds": [],
        "opposingReceiptIds": [],
    },
    "window": {
        "start": "2026-08-21",
        "end": "2026-09-03",
        "method": "dynamic_source_copy_apply_v1",
        "closed": True,
    },
    "checkedAt": "2026-09-04T13:24:42.586Z",
    "limitations": [],
}


def test_instrument_members_pass_through_untouched_when_the_view_carries_them():
    # 5 Sep 2026: the engine view derives evidence_summary and ribbon_series; the
    # served signal carries them exactly, and a null ribbon (withheld) stays null.
    strands = [
        {
            "familyId": "youtube",
            "points": [{"at": "2026-08-21T00:00:00Z", "value": 0.0}],
        }
    ]
    served = desk._admitted_signal(
        _instrument_row(evidence_summary=dict(_SUMMARY), ribbon_series=strands),
        _INSTRUMENT_RUN,
    )
    assert served["signal"]["evidence_summary"] == _SUMMARY
    assert served["signal"]["ribbon_series"] == strands
    withheld = desk._admitted_signal(
        _instrument_row(evidence_summary=dict(_SUMMARY), ribbon_series=None),
        _INSTRUMENT_RUN,
    )
    assert withheld["signal"]["ribbon_series"] is None


def test_a_view_without_the_instrument_members_is_served_as_before():
    served = desk._admitted_signal(_instrument_row(), _INSTRUMENT_RUN)
    assert served is not None
    assert "evidence_summary" not in served["signal"]
    assert "ribbon_series" not in served["signal"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_summary", "ready"),
        ("evidence_summary", {"contractVersion": "0.9.0"}),
        ("ribbon_series", "youtube"),
        ("ribbon_series", {"familyId": "youtube"}),
    ],
)
def test_a_malformed_instrument_member_poisons_the_signal(field, value):
    row = _instrument_row(evidence_summary=dict(_SUMMARY), ribbon_series=[])
    row[field] = value
    assert desk._admitted_signal(row, _INSTRUMENT_RUN) is None


def test_v2_reader_uses_the_engine_view_and_preserves_prediction_order(monkeypatch):
    captured = {}

    def query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(desk.bq, "_run_query", query)
    desk.bq.fetch_dynamic_run_signals("run_20260827_dynamic_apply_v2")

    assert "v_desk_dynamic_signals_v2" in captured["sql"]
    assert "signal_candidates_v2" not in captured["sql"]
    assert "ORDER BY v.predicted_at DESC, v.prediction_id ASC" in captured["sql"]
