"""The prefix_pattern_v1 argument rule: a fixed prefix, then literal or regex positions.

The daily contract revision binds the daily compose and the legacy chain replay by this
rule. The registry refuses at load any rule whose regex is unanchored, does not compile,
or is a catch all that would admit a switch, a space, a path or an empty value, so no
position can widen into an arbitrary argument. The approval path admits a vector only
of exactly the rule's length, with the prefix and every literal equal and every regex
position a full match. The v3 approve routine does not know this kind yet, so an
approval of either operation refuses in SQL until a later routine update.
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from src.analysis.open_intelligence import execution_approval, execution_origins

ENGINE = Path(__file__).resolve().parents[2]
CONTRACT = ENGINE / "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json"
# ASCII digits only: Python's \d also admits other scripts' decimal digits.
DATE = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
SHA = "a" * 40
LEGACY = ("scripts/staging/replay_open_intelligence.py", "--apply-run", "--trend-date")
COMPOSE = ("scripts/staging/compose_daily_open_intelligence.py", "--cutoff-date")


def rule(name):
    return json.loads(CONTRACT.read_bytes())["operation_validation"][name]["arguments"]


def admits(vector, value):
    return execution_approval._validate_policy_arguments(tuple(vector), value)


def loads(value):
    execution_origins._validate_arguments(value)


# The committed rules


def test_the_committed_rules_are_prefix_patterns():
    assert rule("daily_composition_apply") == {
        "kind": "prefix_pattern_v1",
        "pattern": [{"regex": DATE}],
        "prefix": list(COMPOSE),
    }
    assert rule("legacy_chain_replay") == {
        "kind": "prefix_pattern_v1",
        "pattern": [
            {"regex": DATE},
            {"literal": "--run-id"},
            {"regex": "^[a-z0-9_][a-z0-9_-]{0,127}$"},
            {"literal": "--source-sha"},
            {"regex": "^[0-9a-f]{40}$"},
        ],
        "prefix": list(LEGACY),
    }
    for name in ("daily_composition_apply", "legacy_chain_replay"):
        loads(rule(name))


def test_r3_apply_keeps_its_exact_argument_rule():
    assert rule("r3_apply") == {
        "kind": "exact",
        "value": ["scripts/staging/replay_open_intelligence.py"],
    }


# The approval path


def test_a_compose_vector_is_admitted_only_with_a_date():
    compose = rule("daily_composition_apply")
    assert admits([*COMPOSE, "2026-09-25"], compose) is True
    for vector in (
        [*COMPOSE],
        [*COMPOSE, "2026-9-25"],
        [*COMPOSE, "2026-09-25", "extra"],
        [*COMPOSE, "2026-09-25\n"],
        [COMPOSE[0], "--cutoff", "2026-09-25"],
        ["scripts/staging/replay_open_intelligence.py", "--cutoff-date", "2026-09-25"],
    ):
        assert admits(vector, compose) is False, vector


def test_a_legacy_replay_vector_is_admitted_only_in_its_exact_shape():
    legacy = rule("legacy_chain_replay")
    good = [*LEGACY, "2026-09-25", "--run-id", "run_20260925_legacy", "--source-sha", SHA]
    assert admits(good, legacy) is True
    for index, value in (
        (3, "25-09-2026"),
        (4, "--run"),
        (5, "Run With Spaces"),
        (5, "--review-receipt-file"),
        (6, "--source"),
        (7, SHA[:39]),
        (7, "A" * 40),
    ):
        vector = list(good)
        vector[index] = value
        assert admits(vector, legacy) is False, (index, value)
    assert admits([*good, "--review-receipt-file", "x"], legacy) is False
    assert admits(good[:-1], legacy) is False


# Fullwidth, Arabic-Indic, Extended Arabic-Indic and Devanagari digits: each a decimal
# digit to Python's \d, and none an ASCII digit.
NON_ASCII_DATES = (
    "\uff12\uff10\uff12\uff16-09-25",
    "\u0662\u0660\u0662\u0666-\u0660\u0669-\u0662\u0665",
    "\u06f2\u06f0\u06f2\u06f6-09-25",
    "2026-09-2\u096b",
)


@pytest.mark.parametrize("date", NON_ASCII_DATES)
def test_a_date_position_admits_ascii_digits_only(date):
    assert date.isascii() is False
    compose = rule("daily_composition_apply")
    assert admits([*COMPOSE, date], compose) is False
    legacy = rule("legacy_chain_replay")
    tail = ["--run-id", "run_20260925_legacy", "--source-sha", SHA]
    assert admits([*LEGACY, "2026-09-25", *tail], legacy) is True
    assert admits([*LEGACY, date, *tail], legacy) is False


# The registry load


def test_a_well_formed_rule_loads():
    loads({"kind": "prefix_pattern_v1", "prefix": ["a.py"], "pattern": [{"literal": "--x"}]})


@pytest.mark.parametrize(
    "regex",
    [
        "\\d{4}-\\d{2}-\\d{2}",
        "^\\d{4}-\\d{2}-\\d{2}",
        "\\d{4}-\\d{2}-\\d{2}$",
        "^.*$",
        "^.+$",
        "^\\S+$",
        "^[\\s\\S]*$",
        "^(.*)$",
        "^[^\\n]*$",
        "^[a-z-]+$",
        "^(",
    ],
)
def test_an_unanchored_invalid_or_catch_all_regex_is_refused_at_load(regex):
    value = deepcopy(rule("daily_composition_apply"))
    value["pattern"] = [{"regex": regex}]
    with pytest.raises(execution_origins.OriginRefusal, match="execution_origin_registry_invalid"):
        loads(value)


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(prefix=[]),
        lambda value: value.update(prefix=["a.py", ""]),
        lambda value: value.update(pattern=[]),
        lambda value: value.update(pattern=[{}]),
        lambda value: value.update(pattern=[{"literal": ""}]),
        lambda value: value.update(pattern=[{"literal": "--x", "regex": DATE}]),
        lambda value: value.update(pattern=[{"literal": 1}]),
        lambda value: value.update(pattern=[{"other": "x"}]),
        lambda value: value.update(pattern={"regex": DATE}),
        lambda value: value.update(extra=True),
        lambda value: value.pop("prefix"),
        lambda value: value.pop("pattern"),
    ],
)
def test_a_malformed_rule_is_refused_at_load(change):
    value = deepcopy(rule("daily_composition_apply"))
    change(value)
    with pytest.raises(execution_origins.OriginRefusal, match="execution_origin_registry_invalid"):
        loads(value)
