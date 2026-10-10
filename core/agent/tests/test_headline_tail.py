"""The short answer rewrite does not close with a restated claim ("with the interpretation that posts featured these
hashtags together"). Seen on a live answer: an inferred claim that only repeated the observed one was worded as an
interpretation and appended to the sentence."""
from datetime import datetime, timezone

from core.agent import writer
from core.agent.context import RunContext


class Model:
    def __init__(self, text):
        self.text, self.calls = text, []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        self.calls.append({"system": system, "user": user})
        return {"text": self.text}, {"input_tokens": 10, "output_tokens": 10, "usd": 0.0}


def answer():
    return {"claims": [
        {"id": "c1", "label": "single_source", "text": "Three TikTok posts in South Africa carried #funnyclip, #skits and #laughs.",
         "evidence_ids": ["tt_1"]},
        {"id": "c2", "label": "inferred", "text": "Posts featured the hashtags #funnyclip, #skits and #laughs together.",
         "evidence_ids": ["tt_1"]},
    ]}


def rewrite(text):
    ctx = RunContext(run_id="r_1", tier="T1", as_of=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc), market="ZA")
    model = Model(text)
    result = writer.rewrite_headline(model, answer(), answer(), ctx)
    return result, model


def test_a_closing_interpretation_clause_is_not_kept():
    (text, _, dispatched, _), _ = rewrite(
        "Three TikTok posts in South Africa carried #funnyclip, #skits and #laughs, with the interpretation that "
        "posts featured these hashtags together.")
    assert dispatched
    assert text == "Three TikTok posts in South Africa carried #funnyclip, #skits and #laughs."


def test_other_closing_restatements_are_not_kept_either():
    for tail in (", suggesting that posts featured these hashtags together", " and the inference is that they co-occur",
                 ", which can be read as these hashtags going together"):
        (text, _, _, _), _ = rewrite("Three TikTok posts in South Africa carried #funnyclip and #skits" + tail + ".")
        assert text == "Three TikTok posts in South Africa carried #funnyclip and #skits.", tail


def test_a_sentence_without_a_tail_is_unchanged():
    (text, _, _, _), _ = rewrite("Commenters said the clip was old footage of a former couple.")
    assert text == "Commenters said the clip was old footage of a former couple."


def test_the_instruction_no_longer_asks_for_an_interpretation_clause():
    _, model = rewrite("Three TikTok posts carried #funnyclip.")
    system = model.calls[0]["system"]
    assert "Word a claim labelled inferred as an interpretation" not in system
    assert "closing clause" in system
    assert "Posts featured the hashtags" in model.calls[0]["user"]  # the claims themselves are still sent
