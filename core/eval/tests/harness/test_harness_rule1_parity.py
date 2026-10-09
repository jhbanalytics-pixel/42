"""Rule 1 (no age lens, TRUST.md K6): the brief's term list and Ask's term list must agree.

The brief gate keeps its terms in core/trust/claims.py (_BREACH_TERMS, read through _breach_term). Ask keeps its
own in core/agent/checks.py (AGE_PATTERNS and DEMOGRAPHIC, read through _text_breaches). A third copy of Ask's list
sits in core/eval/promptfooconfig.yaml, as JavaScript. The two lists are written separately and cover different
words, so these tests judge both against one corpus (rule1_terms.py) instead of comparing their source:

  * each list flags every phrase the rule names and lets every look-alike through,
  * the two lists give the same answer on every phrase,
  * Ask's list and the promptfoo copy are the same patterns.

The corpus labels come from the rule, never from either detector. KNOWN_OPEN holds the cases that are wrong today
(work item N15, lanes 1 and 4 own the two lists). Each is a strict xfail: when a lane fixes its list the case
passes unexpectedly and fails, so the entry has to be removed with the fix.
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rule1_terms  # noqa: E402

ROOT = Path(__file__).resolve().parents[4]
PROMPTFOO = ROOT / "core" / "eval" / "promptfooconfig.yaml"

ASK_LANE = "N15 (lane 1 owns core/agent/checks.py)"
BRIEF_LANE = "N15 (lane 4 owns core/trust/claims.py)"
BOTH_LANES = "N15 (lanes 1 and 4: the two lists must agree)"

# The wrong answers on the base commit, found by running the corpus against both lists. Each is one strict xfail.
# Ask's list lets these through, or flags them when it should not:
ASK_WRONG = (
    'baby',
    'skews female', 'mostly women', 'predominantly male', 'likely female', 'LSM 5', "Children's Day",
)
# The brief gate's list lets these through, or flags them when it should not:
BRIEF_WRONG = (
    'born-frees', 'digital natives', 'iGen', '#genzrevolution', 'GenZProtests', 'toddlers', 'babies', 'infants',
    'old people', 'middle-aged', 'matriculants', 'school leavers', 'first-time voters', 'grandmothers',
    'retirees', 'minors', 'baby', 'early twenties', 'twenty-somethings',
    'thirty-somethings', 'the 25-34s', 'born in 1998', 'grew up in the 90s', 'middle class', 'working-class',
    'demographics', 'life stage', 'high income', 'low-income households', 'June 18-24', '25-30 minutes',
)
# The two lists answer differently on these:
DISAGREE = (
    'born-frees', 'digital natives', 'iGen', '#genzrevolution', 'GenZProtests', 'toddlers', 'babies',
    'infants', 'old people', 'middle-aged', 'matriculants',
    'school leavers', 'first-time voters', 'grandmothers', 'retirees', 'minors', 'early twenties',
    'twenty-somethings', 'thirty-somethings', 'the 25-34s', 'born in 1998', 'grew up in the 90s',
    'middle class', 'working-class', 'demographics', 'life stage', 'skews female', 'mostly women',
    'predominantly male', 'likely female', 'LSM 5', 'high income', 'low-income households', "Children's Day",
    'June 18-24', '25-30 minutes',
)

KNOWN_OPEN = {
    **{("ask", phrase): ASK_LANE for phrase in ASK_WRONG},
    **{("brief", phrase): BRIEF_LANE for phrase in BRIEF_WRONG},
    **{("agree", phrase): BOTH_LANES for phrase in DISAGREE},
}


def ask_flags(text):
    from core.agent import checks

    return bool([hit for hit in checks._text_breaches(text) if hit != "Google Trends or search volume"])


def brief_flags(text):
    from core.trust import claims

    return claims._breach_term(text, frozenset()) is not None


DETECTORS = {"ask": ask_flags, "brief": brief_flags}


def marks(check, phrase):
    reason = KNOWN_OPEN.get((check, phrase))
    return [pytest.mark.xfail(strict=True, reason=reason)] if reason else []


def disposition_cases():
    return [pytest.param(detector, phrase, must_flag, id=f"{detector}: {phrase}", marks=marks(detector, phrase))
            for phrase, must_flag, _group in rule1_terms.phrases() for detector in DETECTORS]


def agreement_cases():
    return [pytest.param(phrase, id=f"agree: {phrase}", marks=marks("agree", phrase))
            for phrase, _must_flag, _group in rule1_terms.phrases()]


@pytest.mark.parametrize("detector,phrase,must_flag", disposition_cases())
def test_each_list_flags_what_the_rule_names_and_passes_what_it_allows(detector, phrase, must_flag):
    flagged = DETECTORS[detector](phrase)
    assert flagged is must_flag, f"{detector} {'passes' if must_flag else 'flags'} {phrase!r}"


@pytest.mark.parametrize("phrase", agreement_cases())
def test_the_brief_list_and_the_ask_list_give_the_same_answer(phrase):
    ask, brief = ask_flags(phrase), brief_flags(phrase)
    assert ask == brief, f"{phrase!r}: ask {'flags' if ask else 'passes'}, brief {'flags' if brief else 'passes'}"


def promptfoo_patterns():
    text = PROMPTFOO.read_text(encoding="utf-8")
    start = text.index("const patterns = [")
    block = text[start:text.index("];", start)]
    found = []
    for line in block.splitlines()[1:]:
        line = line.strip()
        if line.startswith("/"):
            assert line.endswith("/i,"), line[-30:]
            found.append(line[1:-3])
    return found


def unescaped(pattern):
    """A Python raw-string pattern and a JavaScript regex literal write a quote differently; compare them alike."""
    return pattern.replace('\\"', '"').replace("\\'", "'")


def test_ask_patterns_and_the_promptfoo_copy_are_the_same_list_in_the_same_order():
    from core.agent import checks

    ask = [unescaped(p.pattern) for p in checks.AGE_PATTERNS]
    copy = [unescaped(p) for p in promptfoo_patterns()]
    assert len(ask) >= 40 and len(copy) >= 40
    assert ask == copy


def test_the_corpus_is_large_enough_to_judge_a_list():
    rows = rule1_terms.phrases()
    flag = [r for r in rows if r[1]]
    allow = [r for r in rows if not r[1]]
    assert len(flag) >= 80 and len(allow) >= 20
    assert {group for _, _, group in rows} >= {"generation label", "age figure", "inferred demographic"}


class TestTheComparisonCanFail:
    def test_a_list_that_misses_a_phrase_fails_the_disposition_test(self, monkeypatch):
        monkeypatch.setitem(DETECTORS, "ask", lambda text: False)
        with pytest.raises(AssertionError, match="ask passes 'Gen Z'"):
            test_each_list_flags_what_the_rule_names_and_passes_what_it_allows("ask", "Gen Z", True)

    def test_a_list_that_flags_a_look_alike_fails_the_disposition_test(self, monkeypatch):
        monkeypatch.setitem(DETECTORS, "brief", lambda text: True)
        with pytest.raises(AssertionError, match="brief flags 'kidney'"):
            test_each_list_flags_what_the_rule_names_and_passes_what_it_allows("brief", "kidney", False)

    def test_two_lists_that_disagree_fail_the_agreement_test(self, monkeypatch):
        monkeypatch.setitem(globals(), "brief_flags", lambda text: not ask_flags(text))
        with pytest.raises(AssertionError, match="ask flags, brief passes"):
            test_the_brief_list_and_the_ask_list_give_the_same_answer("Gen Z")

    def test_a_promptfoo_copy_with_one_pattern_changed_is_not_equal(self):
        from core.agent import checks

        copy = [unescaped(p) for p in promptfoo_patterns()]
        copy[0] = copy[0].replace("alpha", "alfa")
        assert [unescaped(p.pattern) for p in checks.AGE_PATTERNS] != copy

    def test_the_yaml_the_copy_is_read_from_still_loads(self):
        assert yaml.safe_load(PROMPTFOO.read_text(encoding="utf-8"))
