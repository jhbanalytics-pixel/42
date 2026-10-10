"""The export prints the words the Ask page is held to (W3R-4, finding 2 of the W3 re-review).

answer_meta_states.json holds, for each producer state, the wire value the API sends and the Short answer and status
lines GET /api/ask/{id}/export printed for the same record. The Ask page tests read the frontend copy and check the
page against those words. Nothing on this side read them, so a change to a word in export.py left the page tests
green. This test builds each record the way the page tests do, runs render_answer_html, and asserts the fixture's
export words, so a change to the words here fails here. The renderer judges nothing and reads the wire value the route
prepared, so the fixture's wire value goes in as the record carries it.

One source of truth: the fixture is the frontend copy, kept here byte for byte. When the frontend copy is in the same
tree the two are pinned equal, so neither can move alone."""
import copy
import hashlib
import html
import json
import re
from pathlib import Path

import pytest

from core.api import export

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "answer_meta_states.json"
FRONTEND = HERE.parents[2] / "app" / "frontend" / "src" / "ui" / "__tests__" / "fixtures"
STATES = json.loads(FIXTURE.read_text(encoding="utf-8"))
BASES = {"complete": FRONTEND / "ask42_complete.json", "partial": FRONTEND / "ask42_partial.json"}
FIXTURE_COUNT = 31
FIXTURE_SHA256 = "f5d66c7f7d3268044629290edb42eeed5368210a9054f8633adb1409d34570bd"


def record_of(entry):
    """The record the API sent for a case, built on the fixture the case was made from (as recordOf does in the
    page test)."""
    record = copy.deepcopy(json.loads(BASES[entry["base"]].read_text(encoding="utf-8")))
    record["status"] = entry["record_status"]
    answer = record["answer"]
    answer["status"] = entry["answer"]["status"]
    answer["short_answer"] = entry["answer"]["short_answer"]
    answer["gaps"] = copy.deepcopy(entry["answer"]["gaps"])
    if entry["answer"]["claims"] == 0:
        answer["claims"] = []
        answer["evidence"] = []
        answer["so_what"] = []
        answer["watch_next"] = []
    else:
        assert len(answer["claims"]) == entry["answer"]["claims"], entry["name"]
        assert len(answer["evidence"]) == entry["answer"]["evidence"], entry["name"]
    if "answer_meta" in entry:
        record["answer_meta"] = copy.deepcopy(entry["answer_meta"])
    return record


def words_of(page):
    """The status lines and the Short answer paragraphs of an export page, as the fixture holds them."""
    header = re.search(r"<header>(.*?)</header>", page, re.S).group(1)
    review = [html.unescape(t) for t in re.findall(r'<p class="review">(.*?)</p>', header, re.S)]
    section = re.search(r"<h2>Short answer</h2>(.*?)</section>", page, re.S).group(1)
    short = [["note" if css else "", html.unescape(t)] for css, t in re.findall(r"<p(?: class=\"(note)\")?>(.*?)</p>", section, re.S)]
    return {"review": review, "short": short}


def test_the_fixture_is_the_one_the_page_tests_read():
    assert len(STATES) == FIXTURE_COUNT
    assert len({e["name"] for e in STATES}) == FIXTURE_COUNT
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_the_copy_is_byte_equal_to_the_frontend_fixture_when_both_are_in_the_tree():
    frontend = FRONTEND / "answer_meta_states.json"
    if not frontend.exists():
        pytest.skip("the frontend copy is not in this tree; the pin applies once both are")
    assert frontend.read_bytes() == FIXTURE.read_bytes()


def test_the_states_cover_every_summary_state_and_every_execution_state():
    wires = [e["answer_meta"] for e in STATES if e.get("answer_meta", {}).get("check") == "verified"]
    assert {w["summary"]["state"] for w in wires} == {
        "shown", "shown_rewritten", "fixed_text", "removed", "blank_unexplained"}
    assert {w["execution"]["state"] for w in wires} == {
        "completed", "stopped_on_budget", "stopped_on_request", "refused_budget_spent"}
    assert {w["check"] for w in (e["answer_meta"] for e in STATES)} == {"verified", "legacy_unknown", "unverified"}


@pytest.mark.parametrize("entry", STATES, ids=[e["name"] for e in STATES])
def test_the_export_says_the_fixture_words_for_every_state(entry):
    page = export.render_answer_html(record_of(entry))
    assert words_of(page) == entry["export"]
