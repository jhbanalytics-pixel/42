"""A plain fact from one creator in the market's own feeds is judged as a fact (Albert, 3 Oct).

K5 lowers a claim's label one step when a named market has source-market support only, so a one-creator observation
worded as seen in a market's feeds and citing a post from that market's own feeds is labelled inferred. The support
check passes an inferred claim only when it is worded as interpretation, so a true observation was cut for not
reading like one. The label stays inferred; the support check is told the label came only from the source-only
step and judges the claim as an observation: supported only when the cited posts state it or directly show it.
Genuinely interpretive claims stay inferred and still need interpretive wording. The evidence rules do not change.
"""

import copy
import re

from core.brief import explain
from core.brief.tests.test_brief_explain import (
    FENCE_CLOSE, FENCE_OPEN, FakeModel, k4_verdicts, ke_post, run_ke, source_market_draft,
    source_only_pack,
)
from core.trust import claims as trust_claims
from core.trust.claims import check_answer

FEED_FACT = 'Seen in Kenya\'s feeds, a TikTok post says "Football at the estate pitch tonight".'
NOTE = getattr(explain, "SOURCE_STEP_NOTE", "Label note: an observation, labelled inferred only because")


def records(pack):
    return {r["id"]: r for r in pack["evidence"]}


def feed_fact(**changes):
    claim = {"id": "k2", "text": FEED_FACT, "label": "inferred", "kind": "observation", "evidence_ids": ["tt_3"],
             "quotes": [{"evidence_id": "tt_3", "text": "Football at the estate pitch tonight"}], "numbers": []}
    claim.update(changes)
    return claim


def step_only(claim, pack):
    return trust_claims.inferred_only_by_source_step(claim, records(pack))


class RuleModel(FakeModel):
    """A support checker that follows SUPPORT_SYSTEM's label rules and nothing else: a claim labelled inferred
    passes only when worded as interpretation ("likely", "may", "suggests"), unless the prompt carries the
    source-step label note, when it is judged as an observation. Every other claim passes."""

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if "verdict" not in schema.get("properties", {}):
            return super().complete_json(system=system, user=user, schema=schema, model=model,
                                         max_tokens=max_tokens)
        self.calls.append({"system": system, "user": user, "support": True, "model": model,
                           "max_tokens": max_tokens})
        text = re.search(re.escape(FENCE_OPEN) + r"\n(.*?)\n" + re.escape(FENCE_CLOSE), user, re.S).group(1)
        label = re.search(r"^Label: (.*)$", user, re.M).group(1)
        interpretive = re.search(r"\b(?:likely|may|suggests?)\b", text, re.I)
        if label == "inferred" and not interpretive and NOTE not in user:
            return {"verdict": "unsupported", "reason": "labelled inferred but not worded as interpretation"}, \
                dict(self.usage)
        return {"verdict": "supported", "reason": "the cited post states it"}, dict(self.usage)


# Which claims count: one creator, own-feed source, feed wording, lowered only by the source-only step


def test_k5_still_labels_a_one_creator_own_feed_observation_inferred():
    pack = source_only_pack()
    answer = {"status": "complete", "short_answer": "", "claims": [feed_fact(label="single_source")],
              "evidence": pack["evidence"], "so_what": [], "watch_next": [], "gaps": [], "context": ""}
    checked, rows = check_answer(answer, window_start="2026-09-25T00:00:00+03:00",
                                 window_end="2026-09-28T23:59:59+03:00", market="KE")
    [claim] = checked["claims"]
    assert claim["label"] == "inferred"
    [k5] = [r for r in rows if r["rule"] == "K5"]
    assert k5["verdict"] == "downgrade" and "one step lower for a source-only place" in k5["detail"]


def test_a_one_creator_own_feed_observation_is_inferred_only_by_the_source_step():
    assert step_only(feed_fact(), source_only_pack()) is True


def test_an_interpretation_is_not_marked_as_lowered_only_by_the_source_step():
    claim = feed_fact(kind="interpretation", text="Seen in Kenya's feeds, estate football likely drives the tag.")
    assert step_only(claim, source_only_pack()) is False


def test_a_located_one_creator_observation_the_writer_labelled_inferred_is_not_marked():
    pack = source_only_pack()
    pack["evidence"][2] = ke_post("tt_3", "tiktok", "@nairobi_ball", "Football at the estate pitch tonight", True)
    claim = feed_fact(text='Creators in Kenya post "Football at the estate pitch tonight" on TikTok.')
    assert step_only(claim, pack) is False


def test_a_two_creator_own_feed_observation_the_writer_labelled_inferred_is_not_marked():
    claim = feed_fact(text='Seen in Kenya\'s feeds, sports creators post "Football highlights from the weekend".',
                      evidence_ids=["yt_1", "th_2"],
                      quotes=[{"evidence_id": "yt_1", "text": "Football highlights from the weekend"}])
    assert step_only(claim, source_only_pack()) is False


def test_a_claim_with_a_higher_label_is_not_marked():
    assert step_only(feed_fact(label="single_source"), source_only_pack()) is False


def test_a_claim_naming_no_place_is_not_marked():
    claim = feed_fact(text='A TikTok post says "Football at the estate pitch tonight".')
    assert step_only(claim, source_only_pack()) is False


# What the support check is told


def test_the_support_prompt_notes_an_inferred_label_that_came_only_from_the_source_step():
    pack = source_only_pack()
    prompt = explain._support_user(feed_fact(), [records(pack)["tt_3"]], pack)
    assert re.search(r"^Label: inferred$", prompt, re.M)
    assert explain.SOURCE_STEP_NOTE in prompt
    assert explain.SOURCE_STEP_NOTE not in "".join(re.findall(re.escape(FENCE_OPEN) + r".*?" +
                                                              re.escape(FENCE_CLOSE), prompt, re.S))


def test_the_support_prompt_carries_no_note_for_an_interpretation():
    pack = source_only_pack()
    claim = feed_fact(kind="interpretation", text="Seen in Kenya's feeds, estate football likely drives the tag.")
    assert explain.SOURCE_STEP_NOTE not in explain._support_user(claim, [records(pack)["tt_3"]], pack)


def test_support_system_judges_a_noted_claim_as_an_observation_and_keeps_the_interpretation_rule():
    system = " ".join(explain.SUPPORT_SYSTEM.split())
    assert "worded as interpretation" in system
    note = " ".join(explain.SOURCE_STEP_NOTE.split())
    assert note.removeprefix("Label note: ") in system
    assert "checked as an observation" in system and "state it or directly show it" in system


# End to end through explain_trend


def test_a_one_creator_own_feed_fact_survives_the_support_check_and_keeps_its_inferred_label():
    model = RuleModel([source_market_draft()])
    result = run_ke(model, pack=source_only_pack())
    assert k4_verdicts(result)["k2"] == "pass"
    assert result["numbers_only"] is False and result["reason"] is None
    labels = {c["id"]: c["label"] for c in result["claims"]}
    assert labels["k2"] == "inferred"
    [k2_call] = [c for c in model.support_calls() if FEED_FACT in c["user"]]
    assert explain.SOURCE_STEP_NOTE in k2_call["user"]


def test_a_genuine_interpretation_still_needs_interpretive_wording():
    draft = source_market_draft()
    draft["claims"][2].update(text="Seen in Kenya's feeds, estate football is why the tag rose.",
                              label="inferred", kind="interpretation", quotes=[])
    draft["claims"][2]["evidence_ids"] = ["tt_3"]
    model = RuleModel([copy.deepcopy(draft)])
    result = run_ke(model, pack=source_only_pack())
    assert k4_verdicts(result)["k3"] == "cut"
    [call] = [c for c in model.support_calls() if "estate football is why" in c["user"]]
    assert explain.SOURCE_STEP_NOTE not in call["user"]

    draft["claims"][2]["text"] = "Seen in Kenya's feeds, estate football likely explains why the tag rose."
    assert k4_verdicts(run_ke(RuleModel([draft]), pack=source_only_pack()))["k3"] == "pass"

