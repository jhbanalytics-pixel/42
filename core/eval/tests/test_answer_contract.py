"""One answer contract, three checkers: answer.py, the promptfoo asserts and answer_contract.mjs must agree."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from core.agent.answer import validate_answer

ROOT = Path(__file__).resolve().parents[3]
EVAL = ROOT / "core" / "eval"
CONTRACT = EVAL / "answer_contract.mjs"
APP_COPY = ROOT / "app" / "frontend" / "src" / "answerContract.js"
FIXTURE = EVAL / "fixtures" / "answer_complete.json"

# Reads {module, asserts, vars, answer} on stdin and prints the JS verdict plus every promptfoo assert result.
RUNNER = """
import { readFileSync } from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
const { validateAnswer } = await import(input.module);
const output = JSON.stringify(input.answer);
const asserts = {};
for (const a of input.asserts) {
  const result = new Function('output', 'context', a.body)(output, { vars: input.vars });
  asserts[a.metric] = { pass: result.pass, reason: result.reason };
}
process.stdout.write(JSON.stringify({ js: validateAnswer(input.answer), asserts }));
"""


def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def promptfoo_asserts():
    config = yaml.safe_load((EVAL / "promptfooconfig.yaml").read_text(encoding="utf-8"))
    found = [{"metric": a["metric"], "body": a["value"]} for a in config["defaultTest"]["assert"] if a["type"] == "javascript"]
    assert len(found) == 4, [a["metric"] for a in found]
    return found


def now01_vars():
    questions = yaml.safe_load((EVAL / "questions.yaml").read_text(encoding="utf-8"))
    return next(t["vars"] for t in questions if t["vars"]["id"] == "NOW-01")


def run_node(answer):
    payload = {"module": CONTRACT.as_uri(), "asserts": promptfoo_asserts(), "vars": now01_vars(), "answer": answer}
    done = subprocess.run(
        ["node", "--input-type=module", "-e", RUNNER],
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def unknown_top_level_key(a):
    a["tier"] = "deep"


def bad_label(a):
    a["claims"][0]["label"] = "likely"


def claim_with_no_evidence_ids(a):
    a["claims"][0]["evidence_ids"] = []
    a["claims"][0].pop("quotes")


def unresolvable_evidence_id(a):
    a["claims"][1]["evidence_ids"].append("tt_missing")


def non_verbatim_quote(a):
    a["claims"][0]["quotes"][0]["text"] += " and it was the best one"


def platform_google_trends(a):
    a["evidence"][0]["platform"] = "google_trends"


def bad_result_hash(a):
    a["claims"][0]["numbers"][0]["result_hash"] = "sha256:abc123"


def proposal_without_falsifier(a):
    a["claims"][2]["kind"] = "proposal"
    a["claims"][2]["basis"] = "Two cited posts reuse Heritage Day outfits"


def complete_with_no_claims(a):
    a["claims"] = []
    a["so_what"] = []
    a["watch_next"] = []


def run_metadata_in_evidence(a):
    a["evidence"][0]["credits"] = 12


# Each mutation, with the promptfoo assert that must also fail on it (None where promptfoo does not cover it).
def claim_id_is_a_list(a):
    a["claims"][0]["id"] = ["c1"]
    a["so_what"][0]["claim_ids"] = [["c1"]]


def compact_iso_as_of(a):
    a["as_of"] = "20260928T061000+0200"


MEDIA = {
    "thumbnail_url": "https://example.com/tt_7431.jpg",
    "duration_s": 21.0,
    "transcript_span": {"start_s": 4.2, "end_s": 9.8, "text": "I can't be the only one"},
    "creator_tier": "micro",
}


def with_media_fields(a):
    a["evidence"][0].update(json.loads(json.dumps(MEDIA)))


def media_mutation(key, value):
    def mutate(a):
        with_media_fields(a)
        a["evidence"][0][key] = value

    mutate.__name__ = f"media_{key}_{type(value).__name__}_{json.dumps(value)[:20]}"
    return mutate


def media_clip_url_extra_field(a):
    with_media_fields(a)
    a["evidence"][0]["clip_url"] = "https://example.com/tt_7431.mp4"


MEDIA_MUTATIONS = [
    media_mutation("thumbnail_url", 42),
    media_mutation("thumbnail_url", "tt_7431.jpg"),
    media_mutation("duration_s", "21s"),
    media_mutation("duration_s", -1),
    media_mutation("duration_s", True),
    media_mutation("transcript_span", "4.2 to 9.8"),
    media_mutation("transcript_span", {"start_s": 4.2, "text": "x"}),
    media_mutation("transcript_span", {"start_s": 4.2, "end_s": 9.8, "text": "x", "speaker": "host"}),
    media_mutation("transcript_span", {"start_s": -1, "end_s": 9.8, "text": "x"}),
    media_mutation("creator_tier", 3),
    media_mutation("creator_tier", ""),
    media_clip_url_extra_field,
]


MUTATIONS = [
    (unknown_top_level_key, None),
    (bad_label, None),
    (claim_with_no_evidence_ids, "claims_have_evidence"),
    (unresolvable_evidence_id, "citation_integrity"),
    (non_verbatim_quote, "citation_integrity"),
    (platform_google_trends, "citation_integrity"),
    (bad_result_hash, None),
    (proposal_without_falsifier, None),
    (complete_with_no_claims, None),
    (run_metadata_in_evidence, None),
    (claim_id_is_a_list, None),
    (compact_iso_as_of, None),
    *[(m, None) for m in MEDIA_MUTATIONS],
]


def proposal_with_basis_and_falsifier(a):
    proposal_without_falsifier(a)
    a["claims"][2]["falsifier"] = "Fewer than five #heritagefits rewear posts in the next seven days"


def curly_quotes_and_extra_whitespace(a):
    a["claims"][0]["quotes"].append({"evidence_id": "tt_7431", "text": "  I can’t   be the\n only one "})


VALID_VARIANTS = [proposal_with_basis_and_falsifier, curly_quotes_and_extra_whitespace, with_media_fields]


def test_fixture_passes_python_js_and_promptfoo():
    answer = fixture()
    assert validate_answer(answer) == []
    result = run_node(answer)
    assert result["js"] == {"ok": True, "problems": []}
    assert set(result["asserts"]) == {"claims_have_evidence", "citation_integrity", "evidence_floor", "no_age_lens"}
    for metric, verdict in result["asserts"].items():
        assert verdict["pass"] is True, f"{metric}: {verdict['reason']}"


@pytest.mark.parametrize("mutate, metric", MUTATIONS, ids=[m.__name__ for m, _ in MUTATIONS])
def test_mutation_rejected_by_python_and_js(mutate, metric):
    answer = fixture()
    mutate(answer)
    python_problems = validate_answer(answer)
    result = run_node(answer)
    assert python_problems, "Python accepted it"
    assert result["js"]["ok"] is False and result["js"]["problems"], "JS accepted it"
    if metric:
        assert result["asserts"][metric]["pass"] is False, result["asserts"][metric]["reason"]


@pytest.mark.parametrize("mutate", VALID_VARIANTS, ids=[m.__name__ for m in VALID_VARIANTS])
def test_valid_variant_accepted_by_python_and_js(mutate):
    answer = fixture()
    mutate(answer)
    assert validate_answer(answer) == []
    assert run_node(answer)["js"] == {"ok": True, "problems": []}


def test_media_fields_pass_python_js_and_promptfoo():
    answer = fixture()
    with_media_fields(answer)
    assert validate_answer(answer) == []
    result = run_node(answer)
    assert result["js"] == {"ok": True, "problems": []}
    for metric, verdict in result["asserts"].items():
        assert verdict["pass"] is True, f"{metric}: {verdict['reason']}"


@pytest.mark.parametrize("mutate", MEDIA_MUTATIONS, ids=[m.__name__ for m in MEDIA_MUTATIONS])
def test_media_mutation_named_by_python_and_js(mutate):
    answer = fixture()
    mutate(answer)
    field = next(k for k in (*MEDIA, "clip_url") if k in mutate.__name__)
    assert any(field in p for p in validate_answer(answer)), "Python did not name the field"
    assert any(field in p for p in run_node(answer)["js"]["problems"]), "JS did not name the field"


@pytest.mark.parametrize("source_market", ["ZA", "NG", "KE", None])
def test_source_market_values_are_accepted_by_python_and_js(source_market):
    answer = fixture()
    answer["evidence"][0]["source_market"] = source_market
    assert validate_answer(answer) == []
    assert run_node(answer)["js"] == {"ok": True, "problems": []}


@pytest.mark.parametrize("source_market", ["za", "UG", 17, False])
def test_invalid_source_market_is_rejected_by_python_and_js(source_market):
    answer = fixture()
    answer["evidence"][0]["source_market"] = source_market
    python_problems = validate_answer(answer)
    js_problems = run_node(answer)["js"]["problems"]
    assert any("source_market" in problem for problem in python_problems)
    assert any("source_market" in problem for problem in js_problems)


def test_app_copy_is_byte_identical():
    if not APP_COPY.exists():
        pytest.skip("app adapter copy not landed yet (lane L4)")
    assert APP_COPY.read_bytes() == CONTRACT.read_bytes()


# The no_age_lens assert and checks.py's AGE_PATTERNS must flag the same phrases.
AGE_RUNNER = """
import { readFileSync } from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
const check = new Function('output', 'context', input.body);
const flagged = input.phrases.map((p) => check(JSON.stringify({ short_answer: p }), {}).pass === false);
process.stdout.write(JSON.stringify(flagged));
"""

AGE_PHRASES = [
    ("Gen Z", True), ("Gen Zers", True), ("Gen-Zers", True), ("GenZers", True), ("Gen Xers", True),
    ("Gen Z's", True), ("Gen Zs", True), ("Gen‐Z", True), ("Gen‑Z", True), ("Gen‑Zers", True),
    ("Gen\u2013Z", True), ("Gen\u2014Z", True),
    ("Generation Alpha", True), ("genz", True), ("millennials", True), ("18-24 year olds", True),
    ("a generation of content", False), ("a general habit", False), ("genuine fans", False),
    ("Gen Z" + "a", False), ("plate ratings", False),
    ("Young South Africans", True), ("youth culture", True), ("young adults", True), ("kids", True),
    ("adolescents", True), ("older audiences", True), ("18-24s", True), ("children", True), ("the elderly", True),
    ("seniors", True), ("pensioners", True), ("youngsters", True), ("under 25", True), ("over-18s", True),
    ("skews young", True),
    ("Youth Day march", False), ("a young brand", False), ("Youth Month", False), ("June 16 to 24 events", False),
    ("older posts", False), ("posts older than a week", False), ("over 50 posts", False), ("over 40%", False),
    # Round 5: under or over a number is an age only at the end of the phrase or before an age or audience word.
    ("over 2 million views", False), ("under 5 million", False), ("over 20 plates", False),
    ("over 50 reposts", False), ("over 10 tweets", False), ("over 3 weekends", False), ("over 2m views", False),
    ("over 10 years of Sundays", False), ("10-15s clips", False), ("15-30s videos", False),
    ("over 50s", True), ("under 25", True), ("over 18 year olds", True), ("under 18-year-olds", True),
    ("over 18 years of age", True), ("users under 30", True), ("users over 30 in Soweto", True),
    ("fans under 25 in Lagos", True), ("fans over 2 million", False), ("viewers over 3 weeks", False), ("over 18+", True), ("under 30 fans", True),
    ("under 25.", True),
    # Round 5: more age forms.
    ("teen", True), ("teens", True), ("teenage", True), ("teenagers", True), ("tweens", True), ("preteens", True),
    ("pre-teens", True), ("the 25-34s", True), ("the 35\u201344s", True), ("18+ audiences", True), ("18+", True),
    ("early teens", True), ("mid twenties", True), ("late thirties", True), ("early forties", True),
    ("late 20s", True), ("mid-30s", True), ("early 40s", True), ("30-somethings", True), ("20 somethings", True),
    ("fifty-somethings", True), ("under-twenties", True), ("under-thirties", True), ("under-forties", True),
    ("youthful", True),
    # Round 6: more age and generation terms, and the words around them that are not ages.
    ("teenaged", True), ("old people", True), ("old folks", True), ("old heads", True), ("old-timers", True),
    ("old timers", True), ("born-free", True), ("born-frees", True), ("born frees", True),
    ("the born free generation", True), ("minors", True), ("retirees", True), ("retiree", True),
    ("millenials", True), ("milennials", True), ("millennial", True), ("nineties babies", True),
    ("noughties kids", True), ("90s babies", True), ("'90s kids", True), ("2000s babies", True),
    ("1990s kids", True), ("80s baby", True), ("digital natives", True), ("a digital native", True),
    ("a generation that grew up online", True), ("a generation who grew up online", True),
    ("an old plate", False), ("old-school plates", False), ("a minor change", False), ("the 1990s", False),
    ("90s music", False), ("nineties house", False), ("born in Soweto", False), ("digital content", False),
    ("a generation of creators", False), ("the old guard of amapiano", False),
    # Round 7: the last age and generation terms, and their neighbours that stay clean.
    ("middle-aged", True), ("middle aged", True), ("middleaged", True), ("#GenZProtests", True),
    ("#GenZRevolution", True), ("#genz254", True), ("#GenAlphaTok", True), ("GenZProtests", True),
    ("schoolchildren", True), ("schoolkids", True), ("toddlers", True), ("infants", True), ("babies", True),
    ("vijana", True), ("wazee", True), ("pikin", True), ("the next generation", True), ("a generation", True),
    ("this generation", True), ("the new generation", True), ("the next-generation crowd", True),
    ("generation of power", False), ("the generation of power", False), ("a new generation of plates", False),
    ("Genzebe", False), ("fur babies", False), ("sugar babies", False), ("a middle ground", False),
    ("the Gen Alphabet book", False), ("a pikinini", False), ("a generation ago", False),
    # Round 10: life stage terms, and the words around them that are not ages.
    ("matric learners", True), ("Matric pupils", True), ("matric students", True), ("a matric learner", True),
    ("matriculants", True), ("a matriculant", True), ("first-time voters", True), ("first time voters", True),
    ("a first-time voter", True), ("school leavers", True), ("school-leavers", True), ("a school leaver", True),
    ("matric results", False), ("the matric exam", False), ("first-time buyers", False), ("school holidays", False),
    ("voters", False), ("leavers", False),
]


def test_no_age_lens_and_checks_agree_on_every_phrase():
    from core.agent.checks import AGE_PATTERNS

    body = next(a["body"] for a in promptfoo_asserts() if a["metric"] == "no_age_lens")
    done = subprocess.run(
        ["node", "--input-type=module", "-e", AGE_RUNNER],
        input=json.dumps({"body": body, "phrases": [p for p, _ in AGE_PHRASES]}),
        capture_output=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    assert done.returncode == 0, done.stderr
    in_eval = dict(zip([p for p, _ in AGE_PHRASES], json.loads(done.stdout)))
    in_checks = {p: any(pattern.search(p) for pattern in AGE_PATTERNS) for p, _ in AGE_PHRASES}
    expected = dict(AGE_PHRASES)
    assert in_eval == expected
    assert in_checks == expected


YOUTHS = "Our youths deserve better than this"


# A post's own words are allowed as quotation (rubric.md hard fail 2): an age term inside a quoted span verbatim in a
# record the claim cites is skipped by no_age_lens exactly as checks._text_breaches skips it with the claim's records.
# in_records names the evidence record that holds the post's words: tt_7431 is cited by claim c1, tt_7502 only by c2.
@pytest.mark.parametrize("claim_text, in_records, flagged", [
    (f'One post reads "{YOUTHS}"', "tt_7431", False),
    (f"One post reads “{YOUTHS}”", "tt_7431", False),
    (f"One post reads '{YOUTHS}'", "tt_7431", False),
    (f"One post reads '{YOUTHS}'", None, True),
    (f"One post reads {YOUTHS}", "tt_7431", True),
    (f'One post reads "{YOUTHS}"', None, True),
    (f'One post reads "{YOUTHS}" and speaks for teens', "tt_7431", True),
    (f'One post reads "{YOUTHS}"', "tt_7502", True),
])
def test_no_age_lens_and_checks_agree_on_quoted_post_words(claim_text, in_records, flagged):
    from core.agent.checks import _text_breaches

    answer = fixture()
    for record in answer["evidence"]:
        if record["id"] == in_records:
            record["text"] += f" {YOUTHS}. Fix the roads."
    answer["claims"][0]["text"] = claim_text
    cited = [r for r in answer["evidence"] if r["id"] in answer["claims"][0]["evidence_ids"]]
    in_checks = "age or generation term" in _text_breaches(claim_text, cited)
    in_eval = run_node(answer)["asserts"]["no_age_lens"]["pass"] is False
    assert in_checks is flagged
    assert in_eval is flagged


UNCITED = {"id": "tt_7600", "platform": "tiktok", "handle": "@roads.watch",
           "url": "https://www.tiktok.com/@roads.watch/video/7600", "posted_at": "2026-09-27T10:00:00+02:00",
           "market": "ZA", "text": f"{YOUTHS}. Fix the roads.", "engagement": {}, "flags": []}


# The short answer and the other answer-level fields may quote any record a claim cites, and no other record.
@pytest.mark.parametrize("in_records, flagged", [("tt_7502", False), ("tt_7600", True)])
def test_no_age_lens_and_checks_agree_on_short_answer_quotes(in_records, flagged):
    from core.agent.checks import _text_breaches

    answer = fixture()
    answer["evidence"].append(json.loads(json.dumps(UNCITED)))
    for record in answer["evidence"]:
        if record["id"] == in_records and record["id"] != "tt_7600":
            record["text"] += f" {YOUTHS}. Fix the roads."
    answer["short_answer"] = f'One post reads "{YOUTHS}".'
    cited_ids = {e for c in answer["claims"] for e in c["evidence_ids"]}
    cited = [r for r in answer["evidence"] if r["id"] in cited_ids]
    in_checks = "age or generation term" in _text_breaches(answer["short_answer"], cited)
    in_eval = run_node(answer)["asserts"]["no_age_lens"]["pass"] is False
    assert in_checks is flagged
    assert in_eval is flagged


# A so_what or watch_next item may quote only the records its own claims cite, as checks._field_problems scans it
# against the claims the item rests on. YOUTHS is added to tt_7502, which only c2 cites.
@pytest.mark.parametrize("section", ["so_what", "watch_next"])
@pytest.mark.parametrize("claim_ids, flagged", [(["c2"], False), (["c1"], True), (["c1", "c2"], False), ([], True)])
def test_no_age_lens_and_checks_agree_on_item_quotes_against_the_items_own_claims(section, claim_ids, flagged):
    from core.agent.checks import _field_problems

    answer = fixture()
    for record in answer["evidence"]:
        if record["id"] == "tt_7502":
            record["text"] += f" {YOUTHS}. Fix the roads."
    item = answer[section][0]
    item["text"] = f'One post reads "{YOUTHS}".'
    item["claim_ids"] = claim_ids
    evidence = {r["id"]: r for r in answer["evidence"]}
    resting = [c for c in answer["claims"] if c["id"] in claim_ids]
    in_checks = "age or generation term" in _field_problems(item["text"], resting, evidence).get("K6", "")
    in_eval = run_node(answer)["asserts"]["no_age_lens"]["pass"] is False
    assert in_checks is flagged
    assert in_eval is flagged


@pytest.mark.parametrize("phrase", ["Google Trends", "google_trends", "google-trends", "googletrends", "google.trends",
                                    "trends.google.com", "Google‐Trends", "google trend"])
def test_citation_integrity_and_checks_flag_every_google_trends_spelling(phrase):
    from core.agent.checks import SEARCH_VOLUME

    answer = fixture()
    answer["claims"][0]["text"] += f" The {phrase} route rose too."
    assert SEARCH_VOLUME.search(phrase)
    verdict = run_node(answer)["asserts"]["citation_integrity"]
    assert verdict["pass"] is False and "Google Trends" in verdict["reason"]


QUOTE_MARKS = ["«»", "„‟", "‹›", "‚‛"]


@pytest.mark.parametrize("marks", QUOTE_MARKS)
def test_every_quote_mark_normalises_the_same_in_python_js_and_promptfoo(marks):
    answer = fixture()
    record = answer["evidence"][0]
    straight = '"' if marks in QUOTE_MARKS[:2] else "'"
    record["text"] += f" {straight}so good{straight}"
    answer["claims"][0]["quotes"].append({"evidence_id": record["id"], "text": f"{marks[0]}so good{marks[1]}"})
    assert validate_answer(answer) == []
    result = run_node(answer)
    assert result["js"] == {"ok": True, "problems": []}
    assert result["asserts"]["citation_integrity"]["pass"] is True, result["asserts"]["citation_integrity"]["reason"]


def test_en_dashes_in_patterns_are_written_as_escapes():
    from core.agent import checks

    tests = Path(checks.__file__).parent / "tests"
    for path in (EVAL / "promptfooconfig.yaml", Path(checks.__file__), Path(__file__), tests / "test_checks.py"):
        assert chr(0x2013) not in path.read_text(encoding="utf-8"), path
