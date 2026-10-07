"""The card title the brief writer gives (tester report, 6 Oct 2026: a card titled with its cluster label "northeast
governors, northeast, governors" was about Independence Day reflections).

The title comes from the same writer call as the explanation. Only an explanation that passed every check has its
title checked: the sentence's code checks on the claims that stand, then one support check against the posts they
cite. A title that fails, or whose check cannot run, is dropped and the card keeps its label; the title never changes
whether the explanation passes, what it says or which claims stand.
"""

import copy

import pytest

from core.brief import explain, job, payload
from core.brief.tests import test_brief_job as job_tests
from core.brief.tests.test_brief_busy import Busy
from core.brief.tests.test_brief_busy import run as run_busy
from core.brief.tests.test_brief_explain import FakeModel, good, run

TITLE = "Shaya step at holiday braais"


def titled(title=TITLE, draft=None):
    return dict(copy.deepcopy(draft or good()), title=title)


def title_calls(model):
    return [c for c in model.support_calls() if f"Label: {explain.TITLE_LABEL}" in c["user"]]


def same_outcome(with_title, without):
    keys = ("explanation", "explanation_claim_ids", "claims", "numbers_only", "reason", "specificity", "news_driven",
            "local_why_now_checked")
    assert {k: with_title[k] for k in keys} == {k: without[k] for k in keys}
    assert [r for r in with_title["checks"] if r["rule"] != explain.TITLE_RULE] == without["checks"]


def test_the_writer_is_asked_for_a_title_it_may_leave_out():
    assert explain.WRITER_SCHEMA["properties"]["title"] == {"type": "string"}
    assert "title" not in explain.WRITER_SCHEMA["required"]
    assert "Then write title: two to six words" in explain.WRITER_SYSTEM


def test_a_supported_title_is_written_from_the_same_writer_call_with_one_support_check():
    model = FakeModel([titled()])
    result = run(model)
    without = run(FakeModel([good()]))
    assert result["reason"] is None and result["title_written"] == TITLE
    assert len(model.writer_calls()) == 1
    assert len(model.support_calls()) == len(without["claims"]) + 2  # claims, sentence, title
    [call] = title_calls(model)
    assert TITLE in call["user"] and explain.TITLE_NOTE in call["user"]
    # The title is checked on the posts the sentence's claims cite, as the sentence is.
    for post_id in ("tt_1", "ig_2", "tt_3"):
        assert f"post {post_id} " in call["user"]
    assert result["checks"][-1] == {"claim_id": None, "rule": "title", "verdict": "pass", "checker": "model",
                                    "detail": "title: support check supported: fake"}
    assert result["usage_usd"] == pytest.approx(without["usage_usd"] + 0.01)
    same_outcome(result, without)


def test_an_unsupported_title_falls_back_to_the_label_and_the_card_still_passes():
    model = FakeModel([titled("Spring Day reflections")], verdicts={"Spring Day reflections": "unsupported"})
    result = run(model)
    assert result["reason"] is None and result["title_written"] is None
    assert result["checks"][-1]["rule"] == "title" and result["checks"][-1]["verdict"] == "cut"
    same_outcome(result, run(FakeModel([good()])))


@pytest.mark.parametrize("title, why", [
    ("Teens doing the shaya step", "banned term"),
    ("Shaya step in 7 places", "numeral '7'"),
    ("Hundreds of creators do the shaya step", "crowd wording"),
    ("The shaya step will spread", "future assertion"),
    ("Shaya step across Kenya", "Kenya"),
    ('The "shaya step" dance', "quotation marks"),
    ("Shaya step " + "dance " * 10, "longer than"),
])
def test_a_title_that_fails_the_sentences_code_checks_is_dropped_without_a_model_call(title, why):
    model = FakeModel([titled(title)])
    result = run(model)
    assert result["reason"] is None and result["title_written"] is None
    assert title_calls(model) == []
    row = result["checks"][-1]
    assert (row["rule"], row["verdict"], row["checker"]) == ("title", "cut", "code") and why in row["detail"]
    # A banned term in the title drops the title; it is never a breach that holds the card.
    assert not [r for r in result["checks"] if r["verdict"] == "breach"]
    same_outcome(result, run(FakeModel([good()])))


def test_a_pinned_number_may_stand_in_a_title():
    result = run(FakeModel([titled("31 creators in three days")]))
    assert result["title_written"] == "31 creators in three days"


def test_a_failing_explanation_is_held_as_before_and_its_title_is_never_checked():
    draft = good()
    draft["claims"][2]["text"] = "The shaya step will spread to more creators."
    draft["explanation"] = "The shaya step is likely to spread to more creators this week."
    model = FakeModel([titled(draft=draft), titled(draft=draft)])
    result = run(model)
    plain = run(FakeModel([draft, draft]))
    assert result["reason"] == plain["reason"] == "failed_checks"
    assert result["title_written"] is None and title_calls(model) == []
    assert result["checks"] == plain["checks"]


def test_the_title_check_refused_by_the_cap_drops_the_title_not_the_card(monkeypatch):
    model = FakeModel([titled()])
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 1000.0 if len(model.calls) < 6 else 0.0)
    result = run(model)
    assert result["reason"] is None and result["title_written"] is None
    assert len(model.calls) == 6 and title_calls(model) == []
    assert result["checks"][-1]["detail"] == "title: support check not run: model cap"


def test_a_model_error_on_the_title_check_drops_the_title_not_the_card():
    class TitleFails(FakeModel):
        def complete_json(self, **kw):
            if f"Label: {explain.TITLE_LABEL}" in kw["user"]:
                raise RuntimeError("upstream timeout")
            return super().complete_json(**kw)

    result = run(TitleFails([titled()]))
    assert result["reason"] is None and result["title_written"] is None
    assert result["checks"][-1]["detail"] == "title: support check not run: RuntimeError: upstream timeout"


def test_a_failed_title_row_never_names_a_hold():
    rows = [{"claim_id": None, "rule": "title", "verdict": "cut", "checker": "code", "detail": "title: x"},
            {"claim_id": None, "rule": "critic", "verdict": "cut", "checker": "model",
             "detail": "critic: simplest non-cultural explanation: a paid campaign; not ruled out: x; "
                       "local why-now checked"}]
    assert job.failed_reason({"reason": "failed_checks", "checks": rows}).startswith("Critic:")
    assert job.check_reason(rows[0]) == "Title check: the written title did not pass, so the card keeps its label"


# The payload and the job


def test_the_payload_keeps_the_label_as_title_and_stores_the_written_title_on_explained_cards_only():
    explained = {"title": "northeast governors, northeast, governors", "title_written": " Independence Day reflections "}
    assert payload._title_written(explained, True) == "Independence Day reflections"
    assert payload._title_written(explained, False) is None
    assert payload._title_written({"title": "#x"}, True) is None


class Titling(job_tests.FakeModel):
    """job_tests.FakeModel whose drafts carry a title; refuse(n) says which title checks are refused as busy."""

    def __init__(self, refuse=lambda n: False, **kw):
        super().__init__(**kw)
        self.refuse, self.title_checks = refuse, 0

    def complete_json(self, **kw):
        if f"Label: {explain.TITLE_LABEL}" in kw["user"]:
            self.title_checks += 1
            if self.refuse(self.title_checks):
                self.calls.append({"user": kw["user"], "support": True, "critic": False, "refused": True})
                raise Busy()
        out, usage = super().complete_json(**kw)
        if "explanation" in kw["schema"]["properties"]:
            out = dict(out, title="Dance clips shared at home")
        return out, usage


def test_a_brief_with_written_titles_publishes_the_same_cards_and_holds():
    world = lambda: job_tests.world(n=2)  # noqa: E731
    plain, titled_run = job_tests.brief(world()), job_tests.brief(world(), model=Titling())
    for m in job_tests.MARKETS:
        before, after = job_tests.payload(plain, m), job_tests.payload(titled_run, m)
        assert [c["item_id"] for c in job_tests.all_cards(after)] == [c["item_id"] for c in job_tests.all_cards(before)]
        assert after["held_back"] == before["held_back"] and after["status"] == before["status"]
        for card, old in zip(job_tests.all_cards(after), job_tests.all_cards(before)):
            assert card["title"] == old["title"]
            assert card["title_written"] == ("Dance clips shared at home" if card["explained"] else None)
            assert old["title_written"] is None
    assert titled_run.counts["model_usd"] > plain.counts["model_usd"]


def test_a_busy_model_on_the_title_check_leaves_the_card_explained_with_its_label():
    tries = job.BUSY_RETRIES + 1
    model = Titling(refuse=lambda n: n <= tries)  # the first title check runs out of tries
    r = run_busy(job_tests.world(n=1, markets=("ZA",)), model=model, sleep=lambda s: None)
    [card] = job_tests.all_cards(job_tests.payload(r, "ZA"))
    assert card["explanation_status"] == "explained" and card["title_written"] is None
    assert card["failed_reason"] is None
    # The refusals are booked as any call's are; one call ran out of tries and the breaker did not trip.
    assert r.counts["model_busy"]["gave_up"] == 1 and r.counts["model_busy"]["tripped"] is None
