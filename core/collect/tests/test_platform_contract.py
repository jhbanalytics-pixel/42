"""Every platform the morning run can store on a post must be one the Ask answer contract accepts.

Ask's warehouse tool copies a stored post's platform (after PLATFORM_NAMES) into the evidence record, and the
answer is validated against core/eval/answer.schema.json and, in the app, against answerContract.js. A stored
platform missing from either fails the whole answer the moment a post of it is cited. The schema is pinned to
Release A (core/agent/tests/test_comment_parent_in_answer.py), so a route whose platform it lacks stays off.
"""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from core.agent.answer import validate_answer
from core.agent.answer_view import PLATFORM_WORDS
from core.agent.tools.socialcrawl import PLATFORM_NAMES
from core.collect import job, parse

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / "core" / "eval" / "answer.schema.json"
FIXTURE = ROOT / "core" / "eval" / "fixtures" / "answer_complete.json"
CONTRACTS = (ROOT / "core" / "eval" / "answer_contract.mjs", ROOT / "app" / "frontend" / "src" / "answerContract.js")
DAYS = (date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 1), date(2026, 10, 3))  # Monday to Saturday


def call_platforms(call):
    """The platforms a call's posts are stored under: its route's own, or each platform search/multi asks for."""
    family, platform = parse.ROUTES[call.route][0], parse.ROUTES[call.route][1]
    if family in ("board", "none", "restat"):
        return set()
    if call.route == "search/multi":
        return {p.strip() for p in str(call.params.get("platforms", "")).split(",") if p.strip()}
    return {platform} if platform else set()


def planned_post_platforms():
    found = set()
    for day in DAYS:
        planned = job.plan(day)
        for calls in planned["calls"].values():
            for call in calls:
                found |= call_platforms(call)
    return {PLATFORM_NAMES.get(p, p) for p in found}


def wide_post_platforms():
    """Row 16w's platforms from WIDE_TOP itself, so a route turned on is checked even on a day it has no room."""
    found = set()
    for route, count in job.WIDE_TOP:
        if count:
            found |= call_platforms(job.Call(job.WIDE_ROW, route, job.wide_params(route, "x", "ZA", DAYS[0]), "ZA",
                                             "expansion"))
    return {PLATFORM_NAMES.get(p, p) for p in found}


def schema_platforms():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return set(schema["$defs"]["evidence"]["properties"]["platform"]["enum"])


def contract_platforms(path):
    line = re.search(r"^const PLATFORMS = \[(.*?)\];$", path.read_text(encoding="utf-8"), re.M)
    assert line, path
    return set(re.findall(r"'([a-z_]+)'", line.group(1)))


@pytest.fixture(scope="module")
def stored():
    found = planned_post_platforms() | wide_post_platforms()
    assert {"tiktok", "instagram", "bluesky", "linkedin", "reddit"} <= found  # the plan was read
    return found


def test_an_answer_citing_a_post_of_any_platform_the_run_stores_passes_the_contract(stored):
    for platform in sorted(stored):
        answer = json.loads(FIXTURE.read_text(encoding="utf-8"))
        answer["evidence"][0]["platform"] = platform
        assert validate_answer(answer) == [], platform


def test_every_platform_the_run_stores_is_in_the_schema_and_both_contract_copies(stored):
    assert stored <= schema_platforms(), stored - schema_platforms()
    for path in CONTRACTS:
        assert stored <= contract_platforms(path), (path, stored - contract_platforms(path))


def test_pinterest_search_stays_off_while_the_contract_has_no_pinterest():
    assert "pinterest" not in schema_platforms()
    assert dict(job.WIDE_TOP)["pinterest/search"] == 0


def test_contract_copies_and_reader_labels_match_the_schema():
    for path in CONTRACTS:
        assert contract_platforms(path) == schema_platforms(), path
    assert set(PLATFORM_WORDS) == schema_platforms()
