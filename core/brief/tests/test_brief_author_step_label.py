"""An observation K5 lowers to inferred for its authors alone is judged as an observation (18 Oct 2026).

K5 caps an observation's label at inferred when none of its cited posts has an independent author: no handle, or a
brand, paid, sponsored, near-duplicate, flagged or generated post. The support check passes an inferred claim only
when it is worded as interpretation, so a plain fact about a sponsored or unnamed post was cut for not reading like
one. The label stays inferred; the support check is told the label came only from the author step and judges the
claim as an observation, as it does for the source-only step (test_brief_source_step_label.py). Interpretations and
claims lowered for any other reason are not marked. The evidence rules do not change.
"""

import re

from core.brief import explain
from core.brief.tests.test_brief_explain import FENCE_CLOSE, FENCE_OPEN, make_pack
from core.trust import claims as trust_claims
from core.trust.claims import check_answer

FACT = 'A sponsored TikTok post says "shaya step at every braai".'


def pack():
    p = make_pack()
    p["evidence"][2]["flags"] = ["sponsored"]
    p["evidence"][2]["sponsor_checked"] = True
    return p


def records(p):
    return {r["id"]: r for r in p["evidence"]}


def fact(**changes):
    claim = {"id": "k2", "text": FACT, "label": "inferred", "kind": "observation", "evidence_ids": ["tt_3"],
             "quotes": [{"evidence_id": "tt_3", "text": "shaya step at every braai"}], "numbers": []}
    claim.update(changes)
    return claim


def marked(claim, p):
    return trust_claims.inferred_only_by_author_step(claim, records(p))


def test_k5_still_caps_a_sponsored_posts_observation_at_inferred():
    p = pack()
    answer = {"status": "complete", "short_answer": "", "claims": [fact(label="single_source")],
              "evidence": p["evidence"], "so_what": [], "watch_next": [], "gaps": [], "context": ""}
    checked, rows = check_answer(answer, window_start="2026-09-23T00:00:00+02:00",
                                 window_end="2026-09-26T23:59:59+02:00", market="ZA")
    [claim] = checked["claims"]
    assert claim["label"] == "inferred"
    [k5] = [r for r in rows if r["rule"] == "K5"]
    assert k5["verdict"] == "downgrade" and "above allowed inferred" in k5["detail"]


def test_a_sponsored_posts_observation_is_inferred_only_by_the_author_step():
    assert marked(fact(), pack()) is True


def test_a_post_without_a_handle_is_the_author_step_too():
    p = make_pack()
    p["evidence"][2]["handle"] = None
    assert marked(fact(), p) is True


def test_an_interpretation_a_higher_label_an_independent_author_and_a_missing_record_are_not_marked():
    assert marked(fact(kind="interpretation", text="Sponsored posts likely drive the step."), pack()) is False
    assert marked(fact(label="single_source"), pack()) is False
    assert marked(fact(), make_pack()) is False  # an independent author allows single_source, so no author step
    assert marked(fact(evidence_ids=["nope"]), pack()) is False


def test_a_source_step_claim_is_still_the_source_step_not_the_author_step():
    from core.brief.tests.test_brief_explain import source_only_pack
    from core.brief.tests.test_brief_source_step_label import feed_fact

    p = source_only_pack()
    assert trust_claims.inferred_only_by_source_step(feed_fact(), records(p)) is True
    assert marked(feed_fact(), p) is False


def test_the_support_prompt_notes_the_author_step_outside_the_fence_and_only_then():
    p = pack()
    prompt = explain._support_user(fact(), [records(p)["tt_3"]], p)
    assert re.search(r"^Label: inferred$", prompt, re.M)
    assert explain.AUTHOR_STEP_NOTE in prompt and explain.SOURCE_STEP_NOTE not in prompt
    assert explain.AUTHOR_STEP_NOTE not in "".join(re.findall(re.escape(FENCE_OPEN) + r".*?" + re.escape(FENCE_CLOSE),
                                                              prompt, re.S))
    plain = explain._support_user(fact(), [records(make_pack())["tt_3"]], make_pack())
    assert "Label note" not in plain


def test_support_system_judges_the_noted_claim_as_an_observation_and_keeps_the_interpretation_rule():
    system = " ".join(explain.SUPPORT_SYSTEM.split())
    assert "worded as interpretation" in system
    assert " ".join(explain.AUTHOR_STEP_NOTE.split()).removeprefix("Label note: ") in system
    assert "is checked the same way, as an observation" in system
    assert "not because it reads anything into them" in system
