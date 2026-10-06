"""Regression test: the README scoring table must match scoring.yaml.

engine/README.md documents the composite in a markdown table under the
"## Scoring model" heading. This test parses that table and a composite
order sentence directly out of the README, loads configs/scoring.yaml
through the same loader the engine uses (src.utils.config_loader.load_scoring),
and asserts the two agree. It exists because the table drifted once already:
the README kept claiming a genz_score weight of 0.17 after scoring.yaml
moved that weight, and watchlist_score, to 0.00 on 6 September 2026.

Table format this test relies on: inside the "## Scoring model" section, a
markdown table whose first column is the signal name wrapped in backticks
(for example `` `velocity` ``) and whose second column is the weight
written as a decimal (for example `0.20`). Any other README table is
ignored, because parsing is scoped to the text between the
"## Scoring model" heading and the next "## " heading.

The composite formula is checked against a second, separate line in that
same section, "Composite order: `a`, `b`, ...", listing, in backticks and
in order, exactly the signals that carry a nonzero weight in scoring.yaml,
in the order scripts/run_rss_now.py sums them into the composite (the
velocity/diversity/engagement/creator_spread/regional_score/
search_velocity_score/slang_score block, then the conditional momentum
carve-in, then tone_score; see compute_trend_scores in run_rss_now.py,
the signal_sum and composite assignments). gcam_score is excluded from
that list because its configured weight is 0.00.
"""

import re
from pathlib import Path

from src.utils.config_loader import load_scoring

ENGINE_ROOT = Path(__file__).resolve().parents[2]
README_PATH = ENGINE_ROOT / "README.md"

SCORING_HEADING = "## Scoring model"

# Derived by reading the composite assembly in scripts/run_rss_now.py
# (compute_trend_scores, the signal_sum / composite block): these are
# exactly the w.get(...) keys the composite sums, in the order they are
# summed. momentum is a conditional carve-in from the velocity weight.
# genz_score and watchlist_score are never referenced there at all.
EXPECTED_COMPOSITE_ORDER = [
    "velocity",
    "diversity",
    "engagement",
    "creator_spread",
    "regional_score",
    "search_velocity_score",
    "slang_score",
    "momentum",
    "tone_score",
]

TABLE_ROW_RE = re.compile(r"^\|\s*`([A-Za-z0-9_]+)`\s*\|\s*([0-9]+\.[0-9]+)\s*\|", re.MULTILINE)
COMPOSITE_ORDER_RE = re.compile(r"^Composite order:\s*(.+)$", re.MULTILINE)
BACKTICK_TOKEN_RE = re.compile(r"`([A-Za-z0-9_]+)`")


def _scoring_section(readme_text: str) -> str:
    """Return the README text between "## Scoring model" and the next "## " heading."""
    lines = readme_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == SCORING_HEADING:
            start = i + 1
            break
    assert start is not None, f'README is missing the "{SCORING_HEADING}" heading'
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end])


def _readme_weights(section_text: str) -> dict[str, float]:
    rows = TABLE_ROW_RE.findall(section_text)
    assert rows, "no `signal` | weight rows found in the Scoring model table"
    return {name: float(weight) for name, weight in rows}


def _readme_composite_order(section_text: str) -> list[str]:
    match = COMPOSITE_ORDER_RE.search(section_text)
    assert match, 'no "Composite order: ..." line found in the Scoring model section'
    return BACKTICK_TOKEN_RE.findall(match.group(1))


def test_readme_scoring_table_matches_scoring_yaml_weights():
    """Every signal name and weight in the README table must equal scoring.yaml.

    Checks rows in the order they appear in the README table first, so a
    stale weight on an early row (for example genz_score) fails with its
    own value rather than being masked by a generic set-difference message.
    """
    readme_text = README_PATH.read_text(encoding="utf-8")
    section_text = _scoring_section(readme_text)
    readme_weights = _readme_weights(section_text)

    config_weights = {k: float(v) for k, v in load_scoring()["weights"].items()}

    for name, readme_weight in readme_weights.items():
        assert name in config_weights, f"{name} is in the README table but not in scoring.yaml"
        assert readme_weight == config_weights[name], (
            f"{name}: README says {readme_weight}, scoring.yaml says {config_weights[name]}"
        )

    assert set(readme_weights) == set(config_weights), (
        f"README signal set {sorted(readme_weights)} != "
        f"scoring.yaml signal set {sorted(config_weights)}"
    )


def test_readme_composite_order_matches_code_and_nonzero_config_weights():
    """The composite order line must list exactly the nonzero-weight signals,
    in the order scripts/run_rss_now.py sums them into the composite."""
    config_weights = {k: float(v) for k, v in load_scoring()["weights"].items()}
    nonzero_in_config = {name for name, weight in config_weights.items() if weight != 0.0}

    assert set(EXPECTED_COMPOSITE_ORDER) == nonzero_in_config, (
        "the hardcoded composite order in this test no longer matches the "
        "nonzero-weight signals in scoring.yaml; re-read the composite in "
        "scripts/run_rss_now.py and update EXPECTED_COMPOSITE_ORDER"
    )

    readme_text = README_PATH.read_text(encoding="utf-8")
    section_text = _scoring_section(readme_text)
    readme_order = _readme_composite_order(section_text)

    assert readme_order == EXPECTED_COMPOSITE_ORDER, (
        f"README composite order {readme_order} != code composite order {EXPECTED_COMPOSITE_ORDER}"
    )


# --- docs/scoring-methodology.md and docs/architecture/dataflow.svg --------
#
# These two files describe the same composite in prose and in a diagram.
# Both drifted the same way the README did (a stale genz_score weight of
# 0.17), so they get the same drift guard: any decimal written next to a
# known signal name must equal that signal's scoring.yaml weight.

DOCS_DIR = ENGINE_ROOT / "docs"
SCORING_METHODOLOGY_PATH = DOCS_DIR / "scoring-methodology.md"
DATAFLOW_SVG_PATH = DOCS_DIR / "architecture" / "dataflow.svg"

DOC_TARGETS = {
    "docs/scoring-methodology.md": SCORING_METHODOLOGY_PATH,
    "docs/architecture/dataflow.svg": DATAFLOW_SVG_PATH,
}

# Prose/diagram alias -> the scoring.yaml weight key it refers to. Matched on
# a word boundary, so "genz" never matches inside "genz_score" and vice
# versa (underscore is a word character, so there is no boundary between
# "genz" and "_score"): whichever spelling a document uses, only that one
# fires, so a value is never checked twice under two aliases.
SIGNAL_ALIASES = {
    "velocity": "velocity",
    "momentum": "momentum",
    "genz_score": "genz_score",
    "genz": "genz_score",
    "gen z": "genz_score",
    "watchlist_score": "watchlist_score",
    "watchlist": "watchlist_score",
    "engagement": "engagement",
    "slang_score": "slang_score",
    "slang": "slang_score",
    "diversity": "diversity",
    "regional_score": "regional_score",
    "regional": "regional_score",
    "creator_spread": "creator_spread",
    "search_velocity_score": "search_velocity_score",
    "search_velocity": "search_velocity_score",
    "tone_score": "tone_score",
    "gcam_score": "gcam_score",
    "gcam": "gcam_score",
}

# Gap allowed between an alias and the decimal it names. Wide enough for
# "watchlist_score retained at 0.00" (13 chars) and "Gen Z density 0.17"
# (9 chars); narrow enough, and newline-excluded (plain "." has no DOTALL),
# to stay clear of unrelated same-line statistics such as
# scoring-methodology.md's "engagement-per-day (row level, 14-day): p50 4,
# p90 23,960, p95 150,201, p99 6.68M", where the nearest decimal is dozens
# of characters past "engagement".
_ALIAS_TO_DECIMAL_GAP = 25


def _weight_claims_near_aliases(text: str) -> list[tuple[str, str, float]]:
    """Return (alias, canonical_signal, claimed_weight) for every place in
    text where a known signal alias is directly followed by a decimal."""
    claims = []
    for alias, canonical in SIGNAL_ALIASES.items():
        pattern = re.compile(
            rf"\b{re.escape(alias)}\b.{{0,{_ALIAS_TO_DECIMAL_GAP}}}?(\d+\.\d+)", re.IGNORECASE
        )
        for match in pattern.finditer(text):
            claims.append((alias, canonical, float(match.group(1))))
    return claims


def test_scoring_methodology_and_dataflow_svg_weights_match_config():
    """Any decimal written next to a known signal name in the methodology doc
    or the architecture diagram must equal that signal's scoring.yaml weight."""
    config_weights = {k: float(v) for k, v in load_scoring()["weights"].items()}

    mismatches = []
    for label, path in DOC_TARGETS.items():
        text = path.read_text(encoding="utf-8")
        for alias, canonical, claimed in _weight_claims_near_aliases(text):
            expected = config_weights.get(canonical)
            if expected is None:
                mismatches.append(f"{label}: {alias!r} names unknown signal {canonical!r}")
            elif claimed != expected:
                mismatches.append(
                    f"{label}: {alias!r} claims {claimed}, scoring.yaml {canonical} is {expected}"
                )

    assert not mismatches, "stale weight claim(s) in docs:\n" + "\n".join(mismatches)


def test_no_legacy_genz_017_weight_in_docs():
    """The retired genz_score weight of 0.17 must not reappear next to genz,
    in either order, in the methodology doc or the architecture diagram."""
    genz = r"\bgen\s*z\b"
    pattern = re.compile(rf"{genz}.{{0,30}}?0\.17|0\.17.{{0,30}}?{genz}", re.IGNORECASE)

    hits = []
    for label, path in DOC_TARGETS.items():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            hits.append(f"{label}: {match.group(0)!r}")

    assert not hits, "found the retired genz 0.17 weight claim:\n" + "\n".join(hits)
