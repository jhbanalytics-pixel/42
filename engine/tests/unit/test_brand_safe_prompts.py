"""Brand-safety + voice tests for src/analysis/prompts/trend_brief.py.

The UEFA fix: every generated image and audio prompt must carry a hard
EXCLUDE clause so an influencer cannot paste in a prompt that leaks a
competitor logo, a real team kit, or a real wordmark. The instruction to
the creator must say every word is required and the exclusions are hard
rules, must never say "verbatim", and must use "exclude" not "avoid" for
the exclusions. The brief carries a human ``headline`` and a populated
``risk_flags`` list. The desk voice bans dashes of any kind.

Pure-function tests, no mocking. They read the constants and the prompt
string the builder produces.
"""

from __future__ import annotations

from src.analysis.prompts.trend_brief import (
    IMAGE_EXCLUSION_CLAUSE,
    RESPONSE_SCHEMA,
    VOICE_INSTRUCTION,
    build_brief_prompt,
)


def _prompt() -> str:
    return build_brief_prompt(
        market="ng",
        topic_group="music_afrobeats",
        trend_score=0.5,
        velocity_score=0.1,
        item_count=50,
        source_diversity=10,
        creator_spread=20,
        tone_avg=0.5,
        sample_rows=[],
        top_creators=[],
    )


def test_exclusion_clause_is_hard_and_complete():
    c = IMAGE_EXCLUSION_CLAUSE.lower()
    assert "exclude" in c
    assert "no brand logos" in c
    assert "no competitor" in c
    assert "no real-person" in c
    assert "no recognisable" in c
    assert "kits" in c
    assert "no real fonts" in c
    # mandatory / hard, never the soft word "avoid"
    assert "mandatory" in c
    assert "avoid" not in c


def test_exclusion_clause_rides_both_image_and_audio_prompts():
    """The clause is appended to the nano_banana_prompt schema description
    and an equivalent hard-exclusion line rides the lyria_prompt, so neither
    surface can be generated without the brand-safety rules attached.
    """
    nano_desc = RESPONSE_SCHEMA["properties"]["nano_banana_prompt"]["description"]
    lyria_desc = RESPONSE_SCHEMA["properties"]["lyria_prompt"]["description"]
    assert IMAGE_EXCLUSION_CLAUSE in nano_desc
    # the audio prompt carries an equivalent hard exclusion (no logos / no marks)
    lyria_low = lyria_desc.lower()
    assert "exclude" in lyria_low
    assert "no brand" in lyria_low or "no competitor" in lyria_low


def test_brief_prompt_forbids_verbatim_language():
    p = _prompt().lower()
    assert "verbatim" not in p


def test_brief_prompt_says_every_word_is_a_hard_rule():
    p = _prompt().lower()
    assert "every word" in p
    assert "hard rule" in p


def test_creator_instruction_uses_exclude_not_avoid_for_exclusions():
    """The influencer-facing instruction frames the exclusions with the hard
    word 'exclude', never the soft word 'avoid'. The schema may still use
    'avoid' for plain voice guidance (avoid fluff), so we check the exclusion
    instruction specifically by reading IMAGE_EXCLUSION_CLAUSE.
    """
    assert "exclude" in IMAGE_EXCLUSION_CLAUSE.lower()
    assert "avoid" not in IMAGE_EXCLUSION_CLAUSE.lower()


def test_headline_field_in_schema():
    props = RESPONSE_SCHEMA["properties"]
    assert "headline" in props
    assert props["headline"]["type"] == "STRING"
    assert "headline" in RESPONSE_SCHEMA["required"]
    desc = props["headline"]["description"].lower()
    # a human headline, not the topic slug
    assert "slug" in desc or "plain" in desc or "human" in desc


def test_risk_flags_field_in_schema_and_populated_not_hardcoded_empty():
    props = RESPONSE_SCHEMA["properties"]
    assert "risk_flags" in props
    assert props["risk_flags"]["type"] == "ARRAY"
    assert props["risk_flags"]["items"]["type"] == "STRING"
    assert "risk_flags" in RESPONSE_SCHEMA["required"]
    desc = props["risk_flags"]["description"].lower()
    # the model populates it; [] only when genuinely none
    assert "brand-safety" in desc or "brand safety" in desc
    assert "[]" in desc or "empty" in desc
    assert "only when" in desc or "genuinely none" in desc


def test_voice_instruction_bans_dashes_and_filler():
    v = VOICE_INSTRUCTION.lower()
    assert "no dash" in v or "no em dash" in v
    assert "declarative" in v or "sharp" in v
    assert "no hedging" in v or "no filler" in v


def test_voice_instruction_in_system_prompt():
    from src.analysis.prompts.trend_brief import SYSTEM_INSTRUCTION

    assert VOICE_INSTRUCTION in SYSTEM_INSTRUCTION


def test_general_brief_framing_preserves_asset_safety_contract():
    from src.analysis.prompts.trend_brief import SYSTEM_INSTRUCTION

    assert "Ogilvy x Google" not in SYSTEM_INSTRUCTION
    assert "evidence-grounded cultural understanding" in SYSTEM_INSTRUCTION
    assert (
        IMAGE_EXCLUSION_CLAUSE in RESPONSE_SCHEMA["properties"]["nano_banana_prompt"]["description"]
    )
    assert "exclude" in RESPONSE_SCHEMA["properties"]["lyria_prompt"]["description"].lower()
