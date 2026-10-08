"""Writer law 5 (core/brief/explain.py WRITER_SYSTEM) names the words K6 breaches on, so the writer is told what the
check will cut instead of finding out from a held card. Prompt text only: no check changes."""

import re

from core.brief.explain import WRITER_SYSTEM
from core.trust.claims import _k6_term

# The terms law 5 has to name, fixed here. Each must be one K6 really breaches on.
NAMED = ["Gen Z", "millennials", "boomers", "teens", "youth", "kids", "children", "elderly", "pensioners",
         "aged 18", "18-24", "mostly women", "skews young", "income bracket", "Google Trends"]


def law5():
    """Law 5 and the lines under it, up to the next numbered law."""
    lines = WRITER_SYSTEM.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("5 "))
    end = next(i for i in range(start + 1, len(lines)) if re.match(r"\d+ ", lines[i]))
    return "\n".join(lines[start:end])


def test_law_5_keeps_its_rule_and_names_the_banned_terms():
    line = law5()
    assert line.startswith("5 Never infer age. Describe people only by language, place, interest, community and creator type.")
    for term in NAMED:
        assert term in line, term


def test_every_term_law_5_names_is_one_k6_breaches_on():
    for term in NAMED:
        assert _k6_term(term, set()), term
