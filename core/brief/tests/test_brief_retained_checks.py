"""W8-DEC-14: claim_checks keeps, for each failed support or sentence check, the verdict, the claim id, the SHA-256 of
the rejected span after NFKC and whitespace normalisation, and a reason code from a fixed enum. No post text, no model
free text. Offline: fake models, no network, no spend."""

import json
import re

import pytest

from core.brief import explain, job
from core.brief.tests.test_brief_explain import FakeModel, good, run
from core.brief.tests.test_brief_job import CHECK_KEYS, ScriptedModel, brief, checks, world, za1_checks
from core.trust import retained

RETAINED_KEYS = CHECK_KEYS | {"span_sha256", "reason_code"}
HEX64 = re.compile(r"[0-9a-f]{64}")
LEAK = "MODELFREETEXT-7731 thabo@gmail.com says the amapiano post was hers"

# Digests of the texts the fake models write, computed outside the code under test and pinned here.
EXPLAIN_C1 = "80fc228ba150a910acc3aad57abf08607197f399e1fc7b5bd805c9bdeacfec44"
EXPLAIN_SENTENCE = "9b75a183d13b21a4655a5688dc605eebad5d1898b4ac3286d0ca4cdb7536c485"
JOB_C1 = "d480c89e92c92224fec0e5c2efd39983257131df5b10593d8fddf08f82659ff5"
JOB_SENTENCE = "c5c7b49d4047dc08b88b860af2c371be22e45970534f31dadeaaa15636875b04"
JOB_C2 = "68e499315bb29ef7c5080999103c1fb07442587661fd454119ce33d97416a724"


class ReasonedModel(FakeModel):
    """FakeModel whose support checks answer with free text in their reason."""

    def complete_json(self, **kw):
        out, usage = super().complete_json(**kw)
        if "verdict" in kw["schema"]["properties"]:
            out = {**out, "reason": LEAK}
        return out, usage


# The explanation step: the digest rides on the check row.


def test_the_overflow_row_has_no_span_to_hash():
    from core.brief.tests.test_brief_explain import draft_with_claim_count

    result = run(FakeModel([draft_with_claim_count(good(), 6)]))
    [row] = result["checks"]
    assert "span_sha256" not in row
    assert job.retained_columns(row) == {"reason_code": "support_withheld_overflow"}


def test_a_cut_claim_keeps_the_digest_of_its_own_text():
    result = run(FakeModel([good(), good()], verdicts={"Creators post": "unsupported"}))
    cuts = [c for c in result["checks"] if c["rule"] == "K4" and c["claim_id"] == "c1" and c["verdict"] == "cut"]
    assert cuts
    assert {c["span_sha256"] for c in cuts} == {EXPLAIN_C1}


def test_a_cut_sentence_keeps_the_digest_of_the_sentence():
    result = run(FakeModel([good(), good()], verdicts={"pairs the shaya step": "unsupported"}))
    cuts = [c for c in result["checks"] if c["rule"] == "K4" and c["claim_id"] is None and c["verdict"] == "cut"]
    assert cuts
    assert {c["span_sha256"] for c in cuts} == {EXPLAIN_SENTENCE}


def test_a_critic_cut_keeps_the_digest_of_the_sentence():
    critic = {"non_cultural_explanation": "a paid campaign", "ruled_out": False, "local_why_now": True,
              "reason": LEAK}
    result = run(FakeModel([good()], critic=critic))
    [row] = [c for c in result["checks"] if c["rule"] == "critic"]
    assert row["verdict"] == "cut"
    assert row["span_sha256"] == EXPLAIN_SENTENCE


def test_a_passed_check_keeps_no_digest():
    result = run(FakeModel([good()]))
    assert result["reason"] is None
    assert result["checks"]
    assert not any("span_sha256" in c for c in result["checks"])


def test_the_title_row_keeps_no_digest():
    draft = good(title="Shaya step at the braai")
    result = run(FakeModel([draft], verdicts={"Shaya step at the braai": "unsupported"}))
    titles = [c for c in result["checks"] if c["rule"] == explain.TITLE_RULE]
    assert titles
    assert not any("span_sha256" in c for c in titles)


def test_a_digest_survives_the_before_repair_marking():
    result = run(FakeModel([good(), good()], verdicts={"Creators post": "unsupported"}))
    early = [c for c in result["checks"] if c["detail"].startswith(explain.REPAIR) and c["verdict"] == "cut"]
    assert early
    assert all(HEX64.fullmatch(c["span_sha256"]) for c in early if c["rule"] == "K4")


def test_a_row_holds_the_digest_and_never_the_span_or_the_models_words():
    result = run(ReasonedModel([good(), good()], verdicts={"Creators post": "unsupported"}))
    for c in result["checks"]:
        if "span_sha256" in c:
            assert HEX64.fullmatch(c["span_sha256"])
    stored = json.dumps([{k: v for k, v in c.items() if k in ("span_sha256", "reason_code")}
                         for c in result["checks"]])
    assert "shaya step" not in stored and "MODELFREETEXT" not in stored


# The job: the stored row.


def retained_rows(r):
    return [c for c in za1_checks(r) if "span_sha256" in c or "reason_code" in c]


def test_a_failed_claim_support_row_is_stored_with_digest_and_code():
    r = brief(world(n=1), model=ScriptedModel(claim_support={"Local creators are posting": ("unsupported", LEAK)}))
    rows = [c for c in retained_rows(r) if c["claim_id"] == "c1"]
    assert rows
    for c in rows:
        assert set(c) == RETAINED_KEYS
        assert (c["rule"], c["verdict"], c["checker"]) == ("K4", "cut", "model")
        assert c["reason_code"] == "support_claim_unsupported"
        assert c["span_sha256"] == JOB_C1


def test_a_partial_verdict_has_its_own_code():
    r = brief(world(n=1), model=ScriptedModel(claim_support={"earliest post in the pack": ("partial", LEAK)}))
    rows = [c for c in retained_rows(r) if c["claim_id"] == "c2"]
    assert rows
    assert {(c["reason_code"], c["span_sha256"]) for c in rows} == {("support_claim_partial", JOB_C2)}


def test_a_failed_sentence_check_is_stored_with_digest_and_code():
    r = brief(world(n=1), model=ScriptedModel(sentence=("unsupported", LEAK)))
    rows = [c for c in retained_rows(r) if c["claim_id"] is None and c["rule"] == "K4"]
    assert rows
    assert {(c["reason_code"], c["span_sha256"]) for c in rows} == {("support_sentence_unsupported", JOB_SENTENCE)}


def test_a_critic_cut_is_stored_with_digest_and_code():
    r = brief(world(n=1), model=ScriptedModel(critic=("a scraping artefact", LEAK)))
    [row] = [c for c in retained_rows(r) if c["rule"] == "critic"]
    assert set(row) == RETAINED_KEYS
    assert row["reason_code"] == "critic_rival_not_ruled_out"
    assert row["span_sha256"] == JOB_SENTENCE


def test_passed_rows_keep_exactly_the_original_columns():
    r = brief(world(n=1), model=ScriptedModel(claim_support={"Local creators are posting": ("unsupported", LEAK)}))
    passed = [c for c in za1_checks(r) if c["verdict"] == "pass"]
    assert passed
    assert all(set(c) == CHECK_KEYS for c in passed)


def test_every_stored_retained_value_is_a_digest_or_a_code_in_the_enum():
    models = [ScriptedModel(claim_support={"Local creators are posting": ("unsupported", LEAK),
                                           "earliest post in the pack": ("partial", LEAK)}),
              ScriptedModel(sentence=("unsupported", LEAK)),
              ScriptedModel(critic=("a scraping artefact", LEAK))]
    seen = 0
    for model in models:
        for c in checks(brief(world(n=1), model=model)):
            if "span_sha256" in c:
                assert HEX64.fullmatch(c["span_sha256"])
                seen += 1
            if "reason_code" in c:
                assert c["reason_code"] in retained.REASON_CODES
    assert seen


def test_no_post_text_and_no_model_text_is_stored_beside_the_digest():
    r = brief(world(n=1), model=ScriptedModel(claim_support={"Local creators are posting": ("unsupported", LEAK)},
                                              sentence=("unsupported", LEAK)))
    rows = retained_rows(r)
    assert rows
    stored = json.dumps(rows)
    for fragment in ("MODELFREETEXT", "thabo", "gmail", "Local creators", "videos with this tag", "dance clips",
                     "Dancing to the new sound"):
        assert fragment not in stored
    for c in rows:
        assert HEX64.fullmatch(c["span_sha256"])
        for key, value in c.items():
            assert key in CHECK_KEYS | {"span_sha256", "reason_code"}
            assert isinstance(value, str) or value is None


# The insert path decides, not the row: a digest is shape checked, a code is recomputed.


def failed_claim_row(**extra):
    return {"claim_id": "c1", "rule": "K4", "verdict": "cut", "checker": "model",
            "detail": "support check unsupported: " + LEAK, **extra}


def test_a_forged_code_and_a_text_in_place_of_a_digest_do_not_get_through():
    row = failed_claim_row(span_sha256="the post said hello", reason_code="free text from the model")
    assert job.retained_columns(row) == {"reason_code": "support_claim_unsupported"}


def test_a_valid_digest_is_kept_and_the_code_comes_from_the_row_not_from_the_caller():
    row = failed_claim_row(span_sha256=JOB_C1, reason_code="support_sentence_partial")
    assert job.retained_columns(row) == {"span_sha256": JOB_C1, "reason_code": "support_claim_unsupported"}


@pytest.mark.parametrize("row", [
    {"claim_id": "c1", "rule": "K4", "verdict": "pass", "checker": "model", "detail": "support check supported: x"},
    {"claim_id": "c1", "rule": "K1", "verdict": "cut", "checker": "code", "detail": "unresolved evidence ids"},
    {"claim_id": None, "rule": explain.TITLE_RULE, "verdict": "cut", "checker": "model", "detail": "title: x"},
    {"claim_id": None, "rule": "K5", "verdict": "downgrade", "checker": "code", "detail": "label lowered"},
    {"claim_id": None, "rule": "K10", "verdict": "downgrade", "checker": "code", "detail": "status partial"},
])
def test_other_rows_keep_nothing(row):
    assert job.retained_columns({**row, "span_sha256": JOB_C1, "reason_code": "support_claim_unsupported"}) == {}


CODE_CASES = [
    ("K4", "c1", "cut", "model", "support check unsupported: x", "support_claim_unsupported"),
    ("K4", "c1", "cut", "model", "before repair: support check partial: x", "support_claim_partial"),
    ("K4", "c1", "cut", "model", "support check something: x", "support_claim_other"),
    ("K4", None, "cut", "model", "explanation sentence support check unsupported: x", "support_sentence_unsupported"),
    ("K4", None, "cut", "model", "explanation sentence support check partial: x", "support_sentence_partial"),
    ("K4", None, "cut", "model", "explanation sentence support check ???: x", "support_sentence_other"),
    ("K4", "c1", "cut", "code", "crowd wording: multiple creators", "support_claim_crowd_wording"),
    ("K4", None, "cut", "code", "short_answer: crowd wording: multiple creators", "support_sentence_crowd_wording"),
    ("K4", None, "cut", "code", "writer returned 6 claims; maximum is 5, so support checks were withheld",
     "support_withheld_overflow"),
    ("K4", "c1", "cut", "model", "check did not complete", "check_incomplete"),
    ("critic", None, "cut", "model", "check did not complete", "check_incomplete"),
    ("K2", None, "cut", "code", "short_answer: number 5 not pinned", "sentence_number_unpinned"),
    ("K3", None, "cut", "code", "short_answer: cites a post outside the window", "sentence_place_outside_window"),
    ("K3", None, "cut", "code", "short_answer: cites a post that is located in KE, not ZA",
     "sentence_place_other_market"),
    ("K3", None, "cut", "code", "short_answer: names Kenya but cites no record located there or from its feeds",
     "sentence_place_unlocated"),
    ("K3", None, "cut", "code", "short_answer: names Kenya in feed wording but cites no record with source market KE",
     "sentence_place_feed_wording"),
    ("K3", None, "cut", "code", "short_answer: describes it on source-market evidence without feed-scoped wording",
     "sentence_place_source_only"),
    ("K3", None, "cut", "code", "short_answer: something else", "sentence_place_other"),
    ("K6", None, "cut", "code", "short_answer: banned term", "sentence_banned_term"),
    ("K6", None, "breach", "code", "short_answer: banned term", "sentence_banned_term"),
    ("K8", None, "cut", "code", "short_answer: unmarked translation", "sentence_translation"),
    ("K9", None, "cut", "code", "short_answer: direct future assertion held while forecast promotion is off",
     "sentence_future_assertion"),
    ("K1", None, "cut", "code", "short_answer: quote not found", "sentence_quote"),
    ("specificity", None, "cut", "code", "short_answer: local specificity: x", "sentence_specificity"),
    ("critic", None, "cut", "model",
     "critic: simplest non-cultural explanation: a paid campaign; not ruled out: r; local why-now checked",
     "critic_rival_not_ruled_out"),
    ("critic", None, "cut", "model",
     "critic: simplest non-cultural explanation: a paid campaign; not ruled out: r; local why-now not checked",
     "critic_rival_and_why_now"),
    ("critic", None, "cut", "model",
     "critic: simplest non-cultural explanation: a paid campaign; ruled out: r; local why-now not checked",
     "critic_why_now_not_shown"),
]


@pytest.mark.parametrize("rule,claim,verdict,checker,detail,code", CODE_CASES)
def test_each_failed_check_outcome_has_its_code(rule, claim, verdict, checker, detail, code):
    row = {"claim_id": claim, "rule": rule, "verdict": verdict, "checker": checker, "detail": detail}
    assert job.retained_columns(row)["reason_code"] == code
    assert code in retained.REASON_CODES


def test_the_enum_holds_exactly_the_codes_the_cases_reach():
    assert retained.REASON_CODES == {case[-1] for case in CODE_CASES} | {"unclassified"}


def test_a_row_the_code_cannot_place_gets_the_catch_all():
    row = {"claim_id": None, "rule": "K7", "verdict": "cut", "checker": "code", "detail": "x"}
    assert job.retained_columns({**row, "span_sha256": JOB_C1})["reason_code"] == "unclassified"


def test_the_rules_the_trust_module_leaves_out_include_the_titles_rule():
    assert explain.TITLE_RULE in retained.NOT_RETAINED_RULES


# The critic's reason code and wording come from the structured keys explain puts on the row, not from the detail
# string, which embeds the model's own explanation text.

STANDING_WORDS = ("not ruled out", "ruled out", "news-driven with local reaction", "event-driven with local reaction")
HOSTILE = ("a paid campaign; not ruled out: maybe", "a paid campaign; ruled out: yes; local why-now checked",
           "a paid campaign; news-driven with local reaction: x; local why-now checked")


@pytest.mark.parametrize("text", HOSTILE)
@pytest.mark.parametrize("ruled_out", [True, False])
@pytest.mark.parametrize("why_now", [True, False])
def test_the_critic_code_does_not_depend_on_the_models_explanation_text(text, ruled_out, why_now):
    out = {"non_cultural_explanation": text, "ruled_out": ruled_out, "local_why_now": why_now, "reason": "r"}
    plain = {**out, "non_cultural_explanation": "a paid campaign"}
    row, reference = explain._critic_row(out, 0), explain._critic_row(plain, 0)
    assert job.retained_columns(row).get("reason_code") == job.retained_columns(reference).get("reason_code")
    assert job.check_reason(row) == job.check_reason(reference)


def test_the_critic_row_carries_its_standing_and_why_now_as_keys():
    out = {"non_cultural_explanation": "x", "ruled_out": True, "local_why_now": False, "reason": "r"}
    row = explain._critic_row(out, 0)
    assert row["standing"] == "ruled out" and row["local_why_now"] is False
    assert row["standing"] in STANDING_WORDS


def test_the_standing_and_why_now_keys_are_not_stored():
    r = brief(world(n=2), model=ScriptedModel(critic=("a scraping artefact", "all collected in one sweep")))
    assert checks(r) and all("standing" not in c and "local_why_now" not in c for c in checks(r))


def test_a_standing_key_outside_the_four_words_is_not_trusted():
    row = {"claim_id": None, "rule": "critic", "verdict": "cut", "checker": "model", "standing": "anything",
           "local_why_now": False, "detail": "critic: simplest non-cultural explanation: x; not ruled out: r; "
                                              "local why-now not checked"}
    assert job._critic_parts(row) == ("not ruled out", False)


# Which span each failed code row digests. The expected digests are worked out here, from the texts the fake models
# write, with hashlib directly: NFKC, whitespace runs as one space, SHA-256.


def digest_of(text):
    import hashlib
    import unicodedata
    return hashlib.sha256(" ".join(unicodedata.normalize("NFKC", text).split()).encode("utf-8")).hexdigest()


def test_a_crowd_wording_cut_on_a_claim_keeps_the_digest_of_that_claim_not_the_sentence():
    from core.brief.tests.test_brief_writer_claims import one_creator_pack

    claim_text = 'Multiple creators post the "shaya step" dance.'
    draft = good()
    draft["claims"][0]["text"] = claim_text
    draft["claims"][0]["number_ids"] = []
    result = run(FakeModel([draft, draft]), pack=one_creator_pack())
    rows = [r for r in result["checks"] if r["claim_id"] == "c1" and r["rule"] == "K4" and r["checker"] == "code"
            and r["verdict"] == "cut"]
    assert rows and all("crowd wording" in r["detail"] for r in rows)
    assert {r["span_sha256"] for r in rows} == {digest_of(claim_text)}
    assert digest_of(claim_text) != digest_of(draft["explanation"])


def test_a_crowd_wording_cut_on_the_sentence_keeps_the_digest_of_the_sentence():
    from core.brief.tests.test_brief_writer_claims import one_creator_pack

    sentence = "Multiple creators pair the shaya step with braais, likely because of the holiday weekend."
    draft = good(explanation=sentence)
    result = run(FakeModel([draft, draft]), pack=one_creator_pack())
    rows = [r for r in result["checks"] if r["claim_id"] is None and r["rule"] == "K4" and r["checker"] == "code"]
    assert rows and {r["span_sha256"] for r in rows} == {digest_of(sentence)}


def test_a_failed_sentence_place_check_keeps_the_digest_of_the_sentence():
    sentence = "31 creators in Nigeria are posting the shaya step, likely because of the Heritage Day weekend."
    result = run(FakeModel([good(explanation=sentence), good(explanation=sentence)]))
    rows = [r for r in result["checks"] if r["claim_id"] is None and r["rule"] == "K3" and r["verdict"] == "cut"]
    assert rows
    assert {r["span_sha256"] for r in rows} == {digest_of(sentence)}
    assert {job.retained_columns(r)["reason_code"] for r in rows} <= retained.REASON_CODES
