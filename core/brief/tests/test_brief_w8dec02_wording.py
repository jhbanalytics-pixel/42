"""W8-DEC-02: an explanation that fails its checks is held with the reason shown. It is not published as numbers and
posts. No comment or docstring in core/brief may say the old thing, because the next reader trusts the comment over the
code. The phrases are fixed here."""

import re
from pathlib import Path

import pytest

BRIEF = Path(__file__).resolve().parents[1]
STALE = [
    "numbers and posts only",
    "as numbers and posts",
    "gives numbers and posts",
    "published as numbers",
    "keeps the card as numbers",
]
SOURCES = sorted(BRIEF.glob("*.py"))


def squashed(path):
    return re.sub(r"\s+", " ", re.sub(r"(?m)^\s*#", " ", path.read_text(encoding="utf-8"))).lower()


def test_the_scan_reads_the_brief_sources():
    assert {p.name for p in SOURCES} >= {"explain.py", "job.py", "payload.py", "gatectx.py"}


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_brief_source_says_a_failed_explanation_publishes_as_numbers(path):
    text = squashed(path)
    found = [phrase for phrase in STALE if phrase in text]
    assert not found, f"{path.name} still says {found}"
