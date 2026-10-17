"""Every platform collect can store on a post must be one the Ask answer contract accepts.

Ask's warehouse tool copies a stored post's platform (after PLATFORM_NAMES) into the evidence record, and the
answer is validated against core/eval/answer.schema.json and, in the app, against answerContract.js. A stored
platform missing from either fails the whole answer the moment a post of it is cited.
"""

import json
import re
from pathlib import Path

from core.agent.answer import validate_answer
from core.agent.answer_view import PLATFORM_WORDS
from core.agent.tools.socialcrawl import PLATFORM_NAMES
from core.collect import parse
from core.collect.socialcrawl_client import MULTI_DEFAULT

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = ROOT / "core" / "eval" / "answer.schema.json"
FIXTURE = ROOT / "core" / "eval" / "fixtures" / "answer_complete.json"
CONTRACTS = (ROOT / "core" / "eval" / "answer_contract.mjs", ROOT / "app" / "frontend" / "src" / "answerContract.js")


def stored_post_platforms():
    """Platforms parse() can write on a posts row: each post-bearing route's own platform, every host a URL can
    name and every platform search/multi asks for, in the name Ask shows."""
    found = {meta[1] for meta in parse.ROUTES.values() if meta[1] and meta[0] not in ("board", "none")}
    found |= set(parse.HOSTS.values()) | set(MULTI_DEFAULT)
    return {PLATFORM_NAMES.get(p, p) for p in found}


def schema_platforms():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return set(schema["$defs"]["evidence"]["properties"]["platform"]["enum"])


def contract_platforms(path):
    line = re.search(r"^const PLATFORMS = \[(.*?)\];$", path.read_text(encoding="utf-8"), re.M)
    assert line, path
    return set(re.findall(r"'([a-z_]+)'", line.group(1)))


def test_an_answer_citing_a_post_of_any_stored_platform_passes_the_contract():
    for platform in sorted(stored_post_platforms()):
        answer = json.loads(FIXTURE.read_text(encoding="utf-8"))
        answer["evidence"][0]["platform"] = platform
        assert validate_answer(answer) == [], platform


def test_every_stored_platform_is_in_the_answer_schema():
    assert "pinterest" in stored_post_platforms()
    assert stored_post_platforms() <= schema_platforms(), stored_post_platforms() - schema_platforms()


def test_every_stored_platform_is_in_both_contract_copies():
    for path in CONTRACTS:
        assert stored_post_platforms() <= contract_platforms(path), (path, stored_post_platforms() - contract_platforms(path))


def test_contract_copies_and_reader_labels_match_the_schema():
    for path in CONTRACTS:
        assert contract_platforms(path) == schema_platforms(), path
    assert set(PLATFORM_WORDS) == schema_platforms()
