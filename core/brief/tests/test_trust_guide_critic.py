"""The user trust guide states the critic rule the brief applies (core/brief/quality.py), W07-03."""

import re
from pathlib import Path

from core.brief import quality

ROOT = Path(__file__).resolve().parents[3]
GUIDE = (ROOT / "docs" / "guide" / "trust.md").read_text(encoding="utf-8")
SPEC = (ROOT / "docs" / "full-42" / "TRUST.md").read_text(encoding="utf-8")
WORDS = {2: "two", 3: "three"}


def critic_paragraph():
    return next(p for p in GUIDE.split("\n\n") if p.startswith("**Critic.**"))


def test_the_guide_no_longer_promises_an_event_reaction_exception():
    text = critic_paragraph()
    assert "can still pass when at least two distinct local creators are reacting" not in text
    assert re.search(r"news or scheduled event[^.]*held", text)
    reacting_alone = {"ruled_out": False, "news_driven": True, "scheduled_event": False, "local_reaction": True,
                      "local_why_now": True, "reason": "r", "non_cultural_explanation": "a news story"}
    assert not quality.cultural_critic_passes(reacting_alone, {"creators": 5, "days": 3, "post_ids": list("abcde")})


def test_the_guide_states_the_inferred_adoption_floor_the_code_counts():
    text = critic_paragraph()
    creators, days = WORDS[quality.ADOPTION_MIN_CREATORS], WORDS[quality.ADOPTION_MIN_DAYS]
    assert f"{creators} or more independent local creators on {days} or more days" in text
    assert "ruled out" in text and "Inferred" in text
    assert (f"{quality.ADOPTION_MIN_CREATORS} or more independent local creators on {quality.ADOPTION_MIN_DAYS} or "
            "more days") in SPEC
