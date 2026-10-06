"""Unit tests for src/analysis/generate_daily_summary.py.

Pure-mock orchestration tests: BQ client + GeminiClient + insert_dataframe
all stubbed so no live spend or BQ write happens. Verifies the empty-
response retry path mirrors generate_briefs and the self-healing
idempotency check ignores rows that already exist but are empty.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from src.analysis.gemini_client import BriefResponse
from src.analysis.generate_daily_summary import (
    DailySummary,
    _fetch_brand24_insights,
    _is_empty_summary,
    generate_daily_summary,
)

_MODEL = "gemini-2.5-flash"


def _summary(
    *,
    summary_text: str = "Today across SSA, music topics led with Amapiano in ZA and Afrobeats in NG.",
    through_line: str = "Music remains the dominant cultural signal across markets.",
    call_to_action: str = "Brief creative on Amapiano and Afrobeats activations this week.",
    key_topics: list[str] | None = None,
    rising_topics: list[str] | None = None,
) -> DailySummary:
    return DailySummary(
        trend_date=date(2026, 5, 6),
        summary_text=summary_text,
        through_line=through_line,
        call_to_action=call_to_action,
        markets=["ke", "ng", "za"],
        brief_count=22,
        key_topics=key_topics or ["za/music_amapiano", "ng/music_afrobeats"],
        rising_topics=rising_topics or ["ke/politics_maandamano"],
        prompt_tokens=2000,
        completion_tokens=400,
        model=_MODEL,
    )


def _response(parsed: dict, *, raw_text: str = '{"summary_text":"x"}') -> BriefResponse:
    """Build a BriefResponse stub. raw_text defaults to JSON-shape so the
    cost-leak guard treats it as retry-eligible."""
    return BriefResponse(
        parsed=parsed,
        raw_text=raw_text,
        prompt_tokens=2000,
        completion_tokens=400,
        model=_MODEL,
    )


def _briefs_input(n: int = 22) -> dict:
    out = {}
    for i in range(n):
        market = ["za", "ng", "ke"][i % 3]
        out[(market, f"topic_{i}")] = {
            "status_tag": "Key" if i < 4 else "Rising",
            "description_rationale": f"Description for topic {i}",
        }
    return out


def test_is_empty_summary_flags_blank_narrative_fields():
    """Any blank narrative field should make _is_empty_summary return True."""
    assert _is_empty_summary(_summary()) is False
    assert _is_empty_summary(_summary(summary_text="")) is True
    assert _is_empty_summary(_summary(through_line="")) is True
    assert _is_empty_summary(_summary(call_to_action="")) is True
    # Whitespace-only counts as empty (matches generate_briefs._is_empty_brief).
    assert _is_empty_summary(_summary(summary_text="   ")) is True
    assert _is_empty_summary(_summary(through_line="\n\n")) is True


def test_retry_fires_when_first_response_empty_and_json_shaped():
    """First response empty + raw_text JSON-shaped triggers a retry call."""
    fake_gemini = MagicMock()
    empty_parsed = {
        "summary_text": "",
        "through_line": "",
        "call_to_action": "",
        "key_topics": [],
        "rising_topics": [],
    }
    full_parsed = {
        "summary_text": "Real summary across NG, ZA, KE.",
        "through_line": "Music + politics dominate today.",
        "call_to_action": "Brief creative this week.",
        "key_topics": ["za/music_amapiano"],
        "rising_topics": ["ke/politics_maandamano"],
    }
    fake_gemini.generate_brief.side_effect = [
        _response(empty_parsed),
        _response(full_parsed),
    ]

    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary.insert_dataframe") as fake_insert,
        patch("src.analysis.generate_daily_summary.logger", new=MagicMock()),
    ):
        result = generate_daily_summary(
            trend_date=date(2026, 5, 6),
            briefs_by_topic=_briefs_input(),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # Two Gemini calls: original + retry.
    assert fake_gemini.generate_brief.call_count == 2
    # Retry-result narrative survives.
    assert result is not None
    assert result.summary_text == "Real summary across NG, ZA, KE."
    # Token counts are summed across both calls (truthful spend ledger).
    assert result.prompt_tokens == 4000
    assert result.completion_tokens == 800
    # Persistence happened once.
    assert fake_insert.call_count == 1


def test_retry_skipped_when_raw_text_not_json_shaped():
    """Cost-leak guard: empty response with non-JSON raw_text means safety
    filter refusal. Skip retry. Then synthesize the deterministic fallback
    from briefs + scores (refusal is a Gemini state, not a data state, so
    the fallback path still has all it needs). Persist the fallback row."""
    fake_gemini = MagicMock()
    empty_parsed = {
        "summary_text": "",
        "through_line": "",
        "call_to_action": "",
        "key_topics": [],
        "rising_topics": [],
    }
    fake_gemini.generate_brief.return_value = _response(
        empty_parsed, raw_text="I cannot help with that."
    )

    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary.insert_dataframe") as fake_insert,
        patch("src.analysis.generate_daily_summary.logger", new=MagicMock()),
    ):
        result = generate_daily_summary(
            trend_date=date(2026, 5, 6),
            briefs_by_topic=_briefs_input(),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # Only one Gemini call: retry was skipped (cost-leak guard).
    assert fake_gemini.generate_brief.call_count == 1
    # Fallback was synthesized + persisted (better than blank email block).
    assert fake_insert.call_count == 1
    assert result is not None
    assert result.summary_text.strip() != ""


def test_deterministic_fallback_when_retry_also_empty():
    """If both Gemini calls return empty, the deterministic fallback
    summary is synthesized from briefs + trend_scores and persisted.
    Email digest top block must NEVER render blank because of empty
    Gemini output; the fallback is the backstop.
    """
    fake_gemini = MagicMock()
    empty_parsed = {
        "summary_text": "",
        "through_line": "",
        "call_to_action": "",
        "key_topics": [],
        "rising_topics": [],
    }
    fake_gemini.generate_brief.return_value = _response(empty_parsed)

    trend_scores = {
        ("za", "topic_0"): {"trend_score": 0.48, "velocity_score": 0.07, "item_count": 109},
        ("ng", "topic_1"): {"trend_score": 0.46, "velocity_score": 0.05, "item_count": 128},
        ("ke", "topic_2"): {"trend_score": 0.42, "velocity_score": 0.16, "item_count": 120},
    }

    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary.insert_dataframe") as fake_insert,
        patch("src.analysis.generate_daily_summary.logger", new=MagicMock()),
    ):
        result = generate_daily_summary(
            trend_date=date(2026, 5, 6),
            briefs_by_topic=_briefs_input(),
            trend_scores_by_topic=trend_scores,
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # Two Gemini calls (first + retry).
    assert fake_gemini.generate_brief.call_count == 2
    # Fallback synthesized + persisted.
    assert fake_insert.call_count == 1
    assert result is not None
    # Fallback narrative is non-empty in every required field.
    assert result.summary_text.strip() != ""
    assert result.through_line.strip() != ""
    assert result.call_to_action.strip() != ""
    # Token ledger reflects BOTH wasted Gemini calls.
    assert result.prompt_tokens == 4000  # 2000 + 2000
    assert result.completion_tokens == 800  # 400 + 400


def test_deterministic_fallback_synthesizes_summary_directly():
    """Sanity-check the fallback function on its own: builds a sensible
    summary with no Gemini calls at all."""
    from src.analysis.generate_daily_summary import _deterministic_fallback_summary

    briefs = {
        ("za", "music_amapiano"): {"status_tag": "Key"},
        ("ng", "music_afrobeats"): {"status_tag": "Key"},
        ("ke", "politics_maandamano"): {"status_tag": "Key"},
        ("ng", "diaspora_japa"): {"status_tag": "Rising"},
    }
    scores = {
        ("za", "music_amapiano"): {"trend_score": 0.48, "velocity_score": 0.07},
        ("ng", "music_afrobeats"): {"trend_score": 0.46, "velocity_score": 0.05},
        ("ke", "politics_maandamano"): {"trend_score": 0.42, "velocity_score": 0.16},
        ("ng", "diaspora_japa"): {"trend_score": 0.40, "velocity_score": 0.11},
    }

    fb = _deterministic_fallback_summary(
        trend_date=date(2026, 5, 6),
        briefs_by_topic=briefs,
        trend_scores_by_topic=scores,
        markets=["ke", "ng", "za"],
        last_response=None,
    )

    # through_line is a CULTURAL OBSERVATION, not a data-point readout.
    # With music dominance (2 of 4 topics + Key tier), the through-line
    # should name the cultural truth, not the topic id.
    assert "music" in fb.through_line.lower()
    # Forbidden phrasing that Thapelo flagged 6 May 2026 (data readout
    # versus cultural strategist voice).
    assert "leads cross-market today at" not in fb.through_line
    assert "0.4" not in fb.through_line  # no raw scores in narrative
    # call_to_action carries the highest-velocity Rising topic AND its
    # cultural driver phrase, not the velocity number.
    assert "diaspora_japa" in fb.call_to_action
    assert "Diaspora-reality" in fb.call_to_action
    assert "velocity" not in fb.call_to_action.lower()
    # summary_text references each market via its cultural label, not
    # uppercase market code.
    for label in ("Mzansi", "Naija", "Kenya"):
        assert label in fb.summary_text
    # No data-readout phrasing in summary_text.
    assert "Cross-trend signal" not in fb.summary_text
    assert "could not be generated" not in fb.summary_text
    assert "sourced from" not in fb.summary_text
    # key_topics + rising_topics populated.
    assert len(fb.key_topics) >= 3
    assert "ng/diaspora_japa" in fb.rising_topics
    # No Gemini -> tokens=0, model=deterministic-fallback.
    assert fb.prompt_tokens == 0
    assert fb.completion_tokens == 0
    assert fb.model == "deterministic-fallback"


def _b24_row(market: str, field: str, text: str):
    from types import SimpleNamespace

    return SimpleNamespace(market=market, query_term=f"1397483532:{field}", text=text)


def test_fetch_brand24_insights_assembles_per_market():
    """The four per-field rows re-assemble into one dict per market, za/ng/ke ordered."""
    client = MagicMock()
    client.project = "proj"
    client.query.return_value.result.return_value = [
        _b24_row("ng", "headline", "Naija H"),
        _b24_row("za", "headline", "Mzansi H"),
        _b24_row("za", "recommendations", "Anchor in football"),
        _b24_row("za", "bogus_field", "should be dropped"),
    ]
    out = _fetch_brand24_insights(client, "ds", date(2026, 6, 24))
    assert [d["market"] for d in out] == ["za", "ng"]  # za before ng, ordered
    za = out[0]
    assert za["headline"] == "Mzansi H"
    assert za["recommendations"] == "Anchor in football"
    assert "bogus_field" not in za  # only the 4 valid narrative fields kept


def test_fetch_brand24_insights_empty_on_query_failure():
    """A BQ failure degrades to no grounding, never blocks the summary."""
    client = MagicMock()
    client.project = "proj"
    client.query.side_effect = RuntimeError("bq down")
    assert _fetch_brand24_insights(client, "ds", date(2026, 6, 24)) == []


def test_genz_category_does_not_create_demographic_fallback_claim():
    from src.analysis.generate_daily_summary import _topic_group_to_cultural_driver

    driver = _topic_group_to_cultural_driver("genz_softlife").lower()
    assert "gen z" not in driver
    assert "young" not in driver
    assert "age" not in driver


def test_deterministic_fallback_rotates_through_line_across_days():
    """Same topic mix on two different trend_dates must NOT produce the
    same through_line. Thapelo flagged 7 May 2026: the fallback was
    emitting identical opening sentence for 4 days running because the
    template was a single string. The rotation is keyed on
    trend_date.toordinal() so consecutive days pick different
    phrasings."""
    from src.analysis.generate_daily_summary import _deterministic_fallback_summary

    briefs = {
        ("za", "music_amapiano"): {"status_tag": "Key"},
        ("ng", "music_afrobeats"): {"status_tag": "Key"},
        ("ke", "music_gengetone"): {"status_tag": "Key"},
        ("za", "music_other"): {"status_tag": "Rising"},
        ("ng", "music_other2"): {"status_tag": "Rising"},
    }
    scores = {
        ("za", "music_amapiano"): {"trend_score": 0.48, "velocity_score": 0.07},
        ("ng", "music_afrobeats"): {"trend_score": 0.46, "velocity_score": 0.05},
        ("ke", "music_gengetone"): {"trend_score": 0.42, "velocity_score": 0.16},
        ("za", "music_other"): {"trend_score": 0.30, "velocity_score": 0.04},
        ("ng", "music_other2"): {"trend_score": 0.28, "velocity_score": 0.03},
    }

    seen_lines = set()
    for day_offset in range(4):
        fb = _deterministic_fallback_summary(
            trend_date=date(2026, 5, 7 + day_offset),
            briefs_by_topic=briefs,
            trend_scores_by_topic=scores,
            markets=["ke", "ng", "za"],
            last_response=None,
        )
        seen_lines.add(fb.through_line)

    # 4 consecutive days with identical topic mix must produce >=2
    # distinct through_lines (rotation across templates).
    assert len(seen_lines) >= 2


def test_action_picker_prefers_positive_velocity_rising_topic():
    """Action this week must NOT point at a deflating Rising topic.
    Thapelo flagged 7 May 2026: 'Track za/politics_crises' picked while
    velocity was -0.024. Picker should prefer Rising topics with
    positive velocity_score; fall back to top trend_score Key topic if
    none positive."""
    from src.analysis.generate_daily_summary import _deterministic_fallback_summary

    briefs = {
        ("za", "politics_crises"): {"status_tag": "Rising"},
        ("ng", "diaspora_japa"): {"status_tag": "Rising"},
        ("ke", "music_gengetone"): {"status_tag": "Rising"},
    }
    # politics_crises has the highest velocity number absolute, but it
    # is NEGATIVE. diaspora_japa is positive at 0.05. genie should
    # pick diaspora_japa.
    scores = {
        ("za", "politics_crises"): {"trend_score": 0.30, "velocity_score": -0.02},
        ("ng", "diaspora_japa"): {"trend_score": 0.40, "velocity_score": 0.05},
        ("ke", "music_gengetone"): {"trend_score": 0.35, "velocity_score": 0.03},
    }

    fb = _deterministic_fallback_summary(
        trend_date=date(2026, 5, 7),
        briefs_by_topic=briefs,
        trend_scores_by_topic=scores,
        markets=["ke", "ng", "za"],
        last_response=None,
    )

    # Picked the highest-positive-velocity Rising topic (diaspora_japa
    # at 0.05), not the deflating one (politics_crises at -0.02).
    assert "diaspora_japa" in fb.call_to_action
    assert "politics_crises" not in fb.call_to_action


def test_topic_group_to_cultural_driver_known_prefixes():
    """Each known topic_group prefix maps to its cultural-driver phrase."""
    from src.analysis.generate_daily_summary import _topic_group_to_cultural_driver

    assert "Township-club" in _topic_group_to_cultural_driver("music_amapiano")
    assert "Lagos energy" in _topic_group_to_cultural_driver("music_afrobeats")
    assert "Nairobi street-pop" in _topic_group_to_cultural_driver("music_gengetone")
    assert "Daily-hustle finance" in _topic_group_to_cultural_driver("fintech_mpesa")
    assert "Diaspora-reality" in _topic_group_to_cultural_driver("diaspora_japa")
    # Prefix matching for politics_*.
    assert "Political beats" in _topic_group_to_cultural_driver("politics_tinubu")
    assert "Political beats" in _topic_group_to_cultural_driver("politics_maandamano")
    # Prefix matching for economy_*.
    assert "Economic pressure" in _topic_group_to_cultural_driver("economy_sapa_hustle")
    # Unknown topic falls back to a generic but non-fabricated phrase.
    fallback = _topic_group_to_cultural_driver("unknowncat_topic")
    assert "real cultural movement" in fallback


def test_idempotency_skip_when_non_empty_row_exists():
    """Cron rerun should skip Gemini entirely when a NON-empty row already
    exists for trend_date."""
    fake_gemini = MagicMock()

    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=True,
        ),
        patch("src.analysis.generate_daily_summary.insert_dataframe") as fake_insert,
        patch("src.analysis.generate_daily_summary.logger", new=MagicMock()),
    ):
        result = generate_daily_summary(
            trend_date=date(2026, 5, 6),
            briefs_by_topic=_briefs_input(),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # Zero Gemini spend.
    assert fake_gemini.generate_brief.call_count == 0
    assert fake_insert.call_count == 0
    assert result is None


def test_response_schema_carries_min_length_constraints():
    """Schema must enforce non-empty narrative server-side via min_length /
    min_items so Vertex rejects the schema-minimum empty response Gemini
    has been emitting. Belt for the deterministic fallback braces.
    Thapelo round-1 follow-up: Gemini returned empty 3 calls in a row on
    6 May 2026; min_length is the next layer of defense."""
    from src.analysis.prompts.daily_summary import RESPONSE_SCHEMA

    props = RESPONSE_SCHEMA["properties"]

    # summary_text: real paragraph required (softened from 200 to 120
    # on 7 May 2026 after observed empty-response failure mode; 120
    # still demands a real sentence or two without rejecting valid
    # short outputs).
    assert props["summary_text"]["min_length"] >= 100
    assert props["summary_text"]["max_length"] >= 800

    # through_line: real sentence required (not 1-3 words).
    assert props["through_line"]["min_length"] >= 15
    assert props["through_line"]["max_length"] <= 250

    # call_to_action: enough room for topic + driver + pivot.
    assert props["call_to_action"]["min_length"] >= 40
    assert props["call_to_action"]["max_length"] >= 200

    # Arrays must carry at least one entry; empty arrays not acceptable.
    assert props["key_topics"]["min_items"] == 1
    assert props["rising_topics"]["min_items"] == 1
    assert props["key_topics"]["max_items"] == 5
    assert props["rising_topics"]["max_items"] == 5

    # Per-item min_length on array entries forces a real
    # 'market/topic_group' string, not a single character.
    assert props["key_topics"]["items"]["min_length"] >= 4
    assert props["rising_topics"]["items"]["min_length"] >= 4


def test_force_true_deletes_ALL_existing_rows_for_trend_date():
    """force=True regen must wipe every existing row for the date before
    INSERT, not just empty placeholders. Without this, a force-regen on
    top of a populated-but-stale row leaves the old row in place and
    Looker queries that don't ORDER BY generated_at DESC can render
    stale text. Bug observed 6 May 2026: regen run produced second row
    in BQ alongside old fallback row from earlier in the day."""
    fake_gemini = MagicMock()
    full_parsed = {
        "summary_text": "Real summary across NG, ZA, KE.",
        "through_line": "Music + politics dominate.",
        "call_to_action": "Brief creative this week.",
        "key_topics": ["za/music_amapiano"],
        "rising_topics": ["ke/politics_maandamano"],
    }
    fake_gemini.generate_brief.return_value = _response(full_parsed)

    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary._delete_all_summary_for") as fake_delete_all,
        patch("src.analysis.generate_daily_summary._delete_empty_summary_for") as fake_delete_empty,
        patch("src.analysis.generate_daily_summary.insert_dataframe") as fake_insert,
        patch("src.analysis.generate_daily_summary.logger", new=MagicMock()),
    ):
        result = generate_daily_summary(
            trend_date=date(2026, 5, 6),
            briefs_by_topic=_briefs_input(),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            force=True,
        )

    # force=True path uses _delete_all_summary_for, NOT the empty-only delete.
    assert fake_delete_all.call_count == 1
    assert fake_delete_empty.call_count == 0
    assert fake_insert.call_count == 1
    assert result is not None


def test_delete_empty_placeholder_runs_on_normal_cron_not_just_force():
    """Bug D: previously the DELETE of empty placeholder rows ran only when
    force=True. A normal cron retry over an empty placeholder would then
    INSERT a duplicate row for the same trend_date. The DELETE must run
    every persist path, regardless of force flag, so cron retries
    self-heal cleanly.
    """
    fake_gemini = MagicMock()
    full_parsed = {
        "summary_text": "Real summary across NG, ZA, KE.",
        "through_line": "Music + politics dominate.",
        "call_to_action": "Brief creative.",
        "key_topics": ["za/music_amapiano"],
        "rising_topics": ["ke/politics_maandamano"],
    }
    fake_gemini.generate_brief.return_value = _response(full_parsed)

    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary._delete_empty_summary_for") as fake_delete,
        patch("src.analysis.generate_daily_summary.insert_dataframe") as fake_insert,
        patch("src.analysis.generate_daily_summary.logger", new=MagicMock()),
    ):
        result = generate_daily_summary(
            trend_date=date(2026, 5, 6),
            briefs_by_topic=_briefs_input(),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            # force defaults False; this is the normal cron path.
        )

    # DELETE-empty was called even though force is False.
    assert fake_delete.call_count == 1
    # INSERT followed.
    assert fake_insert.call_count == 1
    assert result is not None


def test_idempotency_does_not_block_when_only_empty_row_exists():
    """Self-heal: empty placeholder row from a prior failed run should NOT
    block a fresh regen on the next cron."""
    fake_gemini = MagicMock()
    full_parsed = {
        "summary_text": "Cross-market signal: music + politics led the day.",
        "through_line": "Music + politics dominate.",
        "call_to_action": "Brief creative.",
        "key_topics": ["za/music_amapiano"],
        "rising_topics": ["ke/politics_maandamano"],
    }
    fake_gemini.generate_brief.return_value = _response(full_parsed)

    with (
        # Empty row present, but the non-empty guard returns False so we
        # treat it as "regen needed".
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary.insert_dataframe") as fake_insert,
        patch("src.analysis.generate_daily_summary.logger", new=MagicMock()),
    ):
        result = generate_daily_summary(
            trend_date=date(2026, 5, 6),
            briefs_by_topic=_briefs_input(),
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    # Gemini was called and the regen result was persisted.
    assert fake_gemini.generate_brief.call_count == 1
    assert fake_insert.call_count == 1
    assert result is not None
    assert "music + politics" in result.summary_text.lower()


# ----------------------------------------------------------------------
# CTA grounding (FIX-CTA-GROUND): the Gemini path can name a topic outside
# the day's rising list, which trips the accuracy watchdog's
# action_pick_in_rising check. A post-validation step re-anchors the CTA on
# the engine's own top rising pick using the same match rule the watchdog
# uses, without a second Gemini call.


def test_cta_names_rising_matches_slash_and_space_forms():
    from src.analysis.generate_daily_summary import _cta_names_rising

    assert (
        _cta_names_rising(
            "Track ng/economy_sapa_hustle this week. Lean in.", ["ng/economy_sapa_hustle"]
        )
        is True
    )
    assert _cta_names_rising("Lean into something else", ["ng/economy_sapa_hustle"]) is False
    assert (
        _cta_names_rising("Track ng economy_sapa_hustle this week", ["ng/economy_sapa_hustle"])
        is True
    )


def test_cta_grounding_predicate_matches_watchdog_definition():
    from src.analysis.generate_daily_summary import _cta_names_rising

    def watchdog_rule(cta, rising_topics):
        cta_l = (cta or "").lower()
        rising = [str(t).lower() for t in (rising_topics or [])]
        matched = [t for t in rising if t in cta_l or t.replace("/", " ") in cta_l]
        return bool(matched)

    cases = [
        ("Track ng/economy_sapa_hustle this week.", ["ng/economy_sapa_hustle"]),
        ("Track ng economy_sapa_hustle this week.", ["ng/economy_sapa_hustle"]),
        ("Lean into something else", ["ng/economy_sapa_hustle"]),
        ("TRACK ZA/FINANCE_STOKVEL THIS WEEK", ["za/music_amapiano", "za/finance_stokvel"]),
        ("Watch the markets", []),
    ]
    for cta, rising in cases:
        assert _cta_names_rising(cta, rising) is watchdog_rule(cta, rising)


def _run_with_gemini_summary(parsed, *, briefs=None, scores=None):
    fake_gemini = MagicMock()
    fake_gemini.generate_brief.return_value = _response(parsed)
    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary.insert_dataframe"),
        patch("src.analysis.generate_daily_summary.logger", new=MagicMock()),
    ):
        return generate_daily_summary(
            trend_date=date(2026, 6, 9),
            briefs_by_topic=briefs if briefs is not None else _briefs_input(),
            trend_scores_by_topic=scores,
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
            persist=True,
            force=True,
        )


# Briefs whose topic keys match the rising tokens the Gemini stubs below
# return, so the re-anchor's "exists in briefs" filter has real candidates.
# In production the persisted rising_topics come from real engine topics;
# these fixtures keep the test internally consistent with that.
_GROUNDING_BRIEFS = {
    ("ng", "economy_sapa_hustle"): {"status_tag": "Rising"},
    ("za", "music_amapiano"): {"status_tag": "Rising"},
    ("ke", "politics_maandamano"): {"status_tag": "Rising"},
}
_GROUNDING_SCORES = {
    ("ng", "economy_sapa_hustle"): {"trend_score": 0.42, "velocity_score": 0.08},
    ("za", "music_amapiano"): {"trend_score": 0.40, "velocity_score": 0.05},
    ("ke", "politics_maandamano"): {"trend_score": 0.38, "velocity_score": 0.03},
}


def test_cta_grounding_rewrites_off_list_pick_2026_06_09():
    rising = ["ng/economy_sapa_hustle", "za/music_amapiano", "ke/politics_maandamano"]
    parsed = {
        "summary_text": "Today across SSA, stokvels surfaced in ZA conversation.",
        "through_line": "Money habits drive the ZA conversation.",
        "call_to_action": "Lean creative into za/finance_stokvel this week.",
        "key_topics": ["za/finance_stokvel", "ng/economy_sapa_hustle"],
        "rising_topics": rising,
    }
    result = _run_with_gemini_summary(parsed, briefs=_GROUNDING_BRIEFS, scores=_GROUNDING_SCORES)
    assert result is not None
    from src.analysis.generate_daily_summary import _cta_names_rising

    assert _cta_names_rising(result.call_to_action, result.rising_topics)
    assert "ng/economy_sapa_hustle" in result.call_to_action
    assert "finance_stokvel" not in result.call_to_action


def test_cta_grounding_noop_when_pick_already_in_rising():
    rising = ["za/music_amapiano", "ng/economy_sapa_hustle"]
    original_cta = "Track za/music_amapiano this week. The township-club sound carries the brief; lean into real venues, not stock club imagery."
    parsed = {
        "summary_text": "Amapiano leads ZA again with strong club rotation.",
        "through_line": "Music remains the cross-market through-line.",
        "call_to_action": original_cta,
        "key_topics": ["za/music_amapiano"],
        "rising_topics": rising,
    }
    result = _run_with_gemini_summary(parsed, briefs=_GROUNDING_BRIEFS, scores=_GROUNDING_SCORES)
    assert result is not None
    assert result.call_to_action == original_cta


def test_cta_grounding_then_watchdog_check_passes():
    rising = ["ng/economy_sapa_hustle", "za/music_amapiano"]
    parsed = {
        "summary_text": "Sapa-hustle economy talk drives NG conversation today.",
        "through_line": "Daily-hustle finance frames the NG story.",
        "call_to_action": "Lean creative into za/finance_stokvel this week.",
        "key_topics": ["za/finance_stokvel"],
        "rising_topics": rising,
    }
    result = _run_with_gemini_summary(parsed, briefs=_GROUNDING_BRIEFS, scores=_GROUNDING_SCORES)
    assert result is not None
    # The grounded CTA must name a rising topic by the SAME rule the watchdog's
    # check_action_pick_in_rising uses; _cta_names_rising mirrors that rule
    # exactly (the predicate is shared on purpose). Assert the pure predicate
    # rather than the BQ-backed watchdog so the test needs no GCP credentials
    # (the watchdog creates a BigQuery client, which fails on the CI runner).
    from src.analysis.generate_daily_summary import _cta_names_rising

    assert _cta_names_rising(result.call_to_action, result.rising_topics), result.call_to_action


def test_cta_grounding_skips_hallucinated_top_rising_picks_real_one():
    """Row 060: the top persisted rising entry can be a topic Gemini invented
    that does not exist in the briefs. The re-anchor must skip it and pick the
    first rising entry that BOTH parses and exists in the briefs, so the
    digest's most visible line never anchors on a hallucinated topic."""
    # Top entry (ke/ghost_topic) is not in the briefs; second entry is.
    rising = ["ke/ghost_topic", "ng/economy_sapa_hustle", "za/music_amapiano"]
    parsed = {
        "summary_text": "Off-list creative direction surfaced in the model output.",
        "through_line": "The model named a topic outside the rising list.",
        "call_to_action": "Lean creative into za/finance_stokvel this week.",
        "key_topics": ["za/finance_stokvel"],
        "rising_topics": rising,
    }
    result = _run_with_gemini_summary(parsed, briefs=_GROUNDING_BRIEFS, scores=_GROUNDING_SCORES)
    assert result is not None
    assert "ng/economy_sapa_hustle" in result.call_to_action
    # The hallucinated token must not be the anchor.
    assert "ghost_topic" not in result.call_to_action
    from src.analysis.generate_daily_summary import _cta_names_rising

    assert _cta_names_rising(result.call_to_action, result.rising_topics)


def test_cta_grounding_skips_malformed_token_no_slash():
    """Row 060: a rising token without a slash partitions to an empty topic,
    which the old code printed as 'Track X/ this week'. The re-anchor must
    skip the slash-less token and pick a well-formed one."""
    rising = ["noslashtoken", "ng/economy_sapa_hustle"]
    parsed = {
        "summary_text": "Malformed rising token sits at the head of the list.",
        "through_line": "The first rising token is missing its market slash.",
        "call_to_action": "Lean creative into za/finance_stokvel this week.",
        "key_topics": ["za/finance_stokvel"],
        "rising_topics": rising,
    }
    result = _run_with_gemini_summary(parsed, briefs=_GROUNDING_BRIEFS, scores=_GROUNDING_SCORES)
    assert result is not None
    # Never emit the malformed 'X/' anchor.
    assert "noslashtoken/" not in result.call_to_action
    assert "/ this week" not in result.call_to_action
    assert "ng/economy_sapa_hustle" in result.call_to_action


def test_cta_grounding_falls_back_to_brief_rising_when_none_valid():
    """Row 060: when NO persisted rising entry parses-and-exists in the briefs,
    re-anchor on the deterministic Rising picker over the briefs, add that token
    to the persisted rising_topics, and never emit a malformed token. The
    watchdog (which reads the persisted rising_topics) then still passes."""
    # Every Gemini rising token is either malformed or absent from the briefs.
    rising = ["ghost", "ke/not_in_briefs", "another/missing_topic"]
    parsed = {
        "summary_text": "All model rising tokens are junk for this date.",
        "through_line": "None of the model rising tokens exist in the briefs.",
        "call_to_action": "Lean creative into za/finance_stokvel this week.",
        "key_topics": ["za/finance_stokvel"],
        "rising_topics": rising,
    }
    briefs = {
        ("za", "music_amapiano"): {"status_tag": "Key"},
        ("ng", "economy_sapa_hustle"): {"status_tag": "Rising"},
    }
    scores = {
        ("za", "music_amapiano"): {"trend_score": 0.48, "velocity_score": 0.06},
        ("ng", "economy_sapa_hustle"): {"trend_score": 0.40, "velocity_score": 0.09},
    }
    result = _run_with_gemini_summary(parsed, briefs=briefs, scores=scores)
    assert result is not None
    # Re-anchored on the only Rising topic in the briefs.
    assert "ng/economy_sapa_hustle" in result.call_to_action
    assert "/ this week" not in result.call_to_action or "ng/economy_sapa_hustle/" not in (
        result.call_to_action
    )
    # The chosen token was added to the persisted rising_topics so the
    # watchdog's CTA-vs-rising_topics check passes on the stored row.
    assert "ng/economy_sapa_hustle" in result.rising_topics
    from src.analysis.generate_daily_summary import _cta_names_rising

    assert _cta_names_rising(result.call_to_action, result.rising_topics)


def test_fallback_cta_names_rising_token_when_leaning_on_key_topic():
    """Row 059: when no Rising topic has positive velocity, the deterministic
    fallback leans creative into the top Key topic but must still name a rising
    token so the watchdog's action_pick_in_rising check passes on the persisted
    row (the fallback sets rising_topics from the Rising briefs)."""
    from src.analysis.generate_daily_summary import (
        _cta_names_rising,
        _deterministic_fallback_summary,
    )

    briefs = {
        ("za", "music_amapiano"): {"status_tag": "Key"},
        ("ng", "politics_tinubu"): {"status_tag": "Rising"},
        ("ke", "politics_maandamano"): {"status_tag": "Rising"},
    }
    # Both Rising topics are flat/deflating (no positive velocity), so the
    # picker leans on the Key topic for creative direction.
    scores = {
        ("za", "music_amapiano"): {"trend_score": 0.50, "velocity_score": 0.04},
        ("ng", "politics_tinubu"): {"trend_score": 0.30, "velocity_score": -0.02},
        ("ke", "politics_maandamano"): {"trend_score": 0.28, "velocity_score": 0.0},
    }

    fb = _deterministic_fallback_summary(
        trend_date=date(2026, 5, 7),
        briefs_by_topic=briefs,
        trend_scores_by_topic=scores,
        markets=["ke", "ng", "za"],
        last_response=None,
    )

    # Creative direction still leans on the Key topic.
    assert "music_amapiano" in fb.call_to_action
    # And the CTA names a rising token so the watchdog passes on the row.
    assert _cta_names_rising(fb.call_to_action, fb.rising_topics), fb.call_to_action


def test_fallback_cta_no_direction_when_no_rising_and_no_key():
    """Row 059 edge: with neither a Rising topic nor a Key topic to lean on,
    the fallback still produces a non-empty, non-malformed CTA."""
    from src.analysis.generate_daily_summary import _deterministic_fallback_summary

    fb = _deterministic_fallback_summary(
        trend_date=date(2026, 5, 7),
        briefs_by_topic={},
        trend_scores_by_topic={},
        markets=["ke", "ng", "za"],
        last_response=None,
    )
    assert fb.call_to_action.strip() != ""
    assert "/ this week" not in fb.call_to_action


def test_drop_stalled_rising_keeps_only_positive_velocity():
    """The velocity gate drops rising tokens whose score row shows flat or
    negative velocity, keeps positive-velocity ones, and leaves tokens with no
    score row in place for the downstream re-anchor to validate. Order holds."""
    from src.analysis.generate_daily_summary import _drop_stalled_rising

    scores = {
        ("za", "music_amapiano"): {"velocity_score": 0.12},
        ("ng", "politics_crises"): {"velocity_score": -0.03},
        ("ke", "diaspora_japa"): {"velocity_score": 0.0},
        ("za", "fashion_ankara"): {"velocity_score": 0.05},
    }
    rising = [
        "za/music_amapiano",  # positive, kept
        "ng/politics_crises",  # negative score row, dropped
        "ke/diaspora_japa",  # zero score row, dropped
        "ke/unknown_topic",  # no score row, kept (not provably stalled)
        "malformed-no-slash",  # no score row, kept
        "za/fashion_ankara",  # positive, kept
    ]
    out = _drop_stalled_rising(rising, scores)
    assert out == [
        "za/music_amapiano",
        "ke/unknown_topic",
        "malformed-no-slash",
        "za/fashion_ankara",
    ]


def test_gemini_cta_cannot_name_a_stalled_rising_topic():
    """End to end on the Gemini path: when the model names a flat-velocity
    Rising topic, the velocity gate drops it from rising_topics and the CTA is
    re-anchored on a positive-velocity rising pick, so the stalled topic is
    never the named action."""
    from src.analysis.generate_daily_summary import _cta_names_rising

    fake_gemini = MagicMock()
    # Model names a stalled (zero-velocity) Rising topic in both the CTA and the
    # rising_topics list, and a separate positive-velocity Rising topic exists.
    parsed = {
        "summary_text": "Real summary across NG, ZA, KE today.",
        "through_line": "Mixed signals across the three markets.",
        "call_to_action": "Track ke/politics_crises this week for creative.",
        "key_topics": ["za/music_amapiano"],
        "rising_topics": ["ke/politics_crises", "ng/diaspora_japa"],
    }
    fake_gemini.generate_brief.side_effect = [_response(parsed)]

    briefs = {
        ("za", "music_amapiano"): {"status_tag": "Key"},
        ("ke", "politics_crises"): {"status_tag": "Rising"},
        ("ng", "diaspora_japa"): {"status_tag": "Rising"},
    }
    scores = {
        ("za", "music_amapiano"): {"trend_score": 0.50, "velocity_score": 0.08},
        ("ke", "politics_crises"): {"trend_score": 0.40, "velocity_score": 0.0},
        ("ng", "diaspora_japa"): {"trend_score": 0.38, "velocity_score": 0.09},
    }

    with (
        patch(
            "src.analysis.generate_daily_summary._existing_non_empty_summary_for",
            return_value=False,
        ),
        patch("src.analysis.generate_daily_summary.insert_dataframe"),
        patch("src.analysis.generate_daily_summary.logger", new=MagicMock()),
    ):
        result = generate_daily_summary(
            trend_date=date(2026, 5, 6),
            briefs_by_topic=briefs,
            trend_scores_by_topic=scores,
            gemini_client=fake_gemini,
            bq_client=MagicMock(),
            dataset="trends_v2_dev",
        )

    assert result is not None
    # Stalled topic dropped from the persisted rising list.
    assert "ke/politics_crises" not in result.rising_topics
    # CTA no longer names the stalled topic, and is re-anchored on the
    # positive-velocity rising topic.
    assert "politics_crises" not in result.call_to_action
    assert "ng/diaspora_japa" in result.call_to_action
    # The CTA still grounds against the (now velocity-clean) rising list.
    assert _cta_names_rising(result.call_to_action, result.rising_topics)
