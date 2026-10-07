"""The short answer rewrite: when the support check blanks the short answer for a cut or narrowed claim it rested on,
one call writes a new one from the surviving claims, and the gate checks it as it checks a first draft's (live staging,
6 October: a partial answer with nine claims showed no top line and no gap saying why; 7 October: the same on a
complete answer whose claim was narrowed and kept)."""

import copy
import json

import pytest

from core.agent import ask, checks, writer
from core.agent.answer import validate_answer
from core.agent.model_budget import AskModelBudget
from core.agent.tests.test_ask import (NARROWED_CLAIM, REWRITTEN_HEADLINE, SEARCH_EMBED_USD, WRITER_OUT, FakeModel,
                                       Harness)
from core.agent.tests.test_ask_model_budget import configure_gemini
from core.agent.tests.test_ask_t2 import CriticModel, t2
from core.agent.writer import (FIELDS_SCHEMA, HEADLINE_GAP, HEADLINE_REWRITE_SCHEMA, K4_REWRITE_SCHEMA,
                               SUPPORT_SCHEMA, WRITER_SCHEMA)

CUT_CLAIM = WRITER_OUT["claims"][1]["text"]  # c2, which the support check cuts after one narrowing


class HeadlineModel(FakeModel):
    """FakeModel whose short answer rewrite returns out (or raises it), and whose field check flags the short answer
    as a forecast when forecast_headline is set."""

    def __init__(self, out=None, *, forecast_headline=False):
        super().__init__()
        self.out = {"text": REWRITTEN_HEADLINE} if out is None else out
        self.forecast_headline = forecast_headline

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is HEADLINE_REWRITE_SCHEMA:
            self.calls.append({"model": model, "schema": schema, "user": user, "system": system,
                               "max_tokens": max_tokens})
            if isinstance(self.out, Exception):
                raise self.out
            return copy.deepcopy(self.out), {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
        result, usage = super().complete_json(system=system, user=user, schema=schema, model=model,
                                              max_tokens=max_tokens)
        if schema is FIELDS_SCHEMA and self.forecast_headline:
            for entry in result["fields"]:
                if f"item {entry['index']} (short_answer;" in user:
                    entry["forecast_assertion"] = True
        return result, usage


def schemas(h):
    """The schema of each call, by name: K4's rewrite and the short answer rewrite share one shape."""
    names = {id(WRITER_SCHEMA): "writer", id(SUPPORT_SCHEMA): "support", id(K4_REWRITE_SCHEMA): "k4_rewrite",
             id(HEADLINE_REWRITE_SCHEMA): "headline", id(FIELDS_SCHEMA): "fields"}
    return [names.get(id(call["schema"]), "other") for call in h.model.calls]


def headline_rows(h):
    return [r for _, batch in h.tables.inserts for r in batch
            if r["claim_id"] == "short_answer" and r["reason"] in writer.HEADLINE_REASONS]


def test_a_blanked_short_answer_is_rewritten_once_from_the_surviving_claims_and_kept_when_it_passes():
    h = Harness(model=HeadlineModel())
    out = h.run()
    answer = out["answer"]

    assert validate_answer(answer) == []
    assert answer["status"] == "partial"  # never upgraded
    assert answer["short_answer"] == REWRITTEN_HEADLINE
    assert HEADLINE_GAP not in answer["gaps"]
    (rewrite,) = [call for call in h.model.calls if call["schema"] is HEADLINE_REWRITE_SCHEMA]
    assert rewrite["max_tokens"] == writer.HEADLINE_REWRITE_MAX_TOKENS
    # only the surviving claims reach it: not the cut c2 or c4, not the question, posts or ids
    for claim in WRITER_OUT["claims"]:
        assert (claim["text"] in rewrite["user"]) == (claim["id"] in ("c1", "c3"))
    assert "What is behind amapiano" not in rewrite["user"]
    assert "tt_1" not in rewrite["user"] and "q_" not in rewrite["user"]
    # the field check after it reads the rewrite as the short answer
    assert schemas(h).index("headline") < schemas(h).index("fields")
    field_call = h.model.calls[schemas(h).index("fields")]
    assert "item 0 (short_answer;" in field_call["user"] and REWRITTEN_HEADLINE in field_call["user"]
    assert [(r["rule"], r["verdict"], r["checker"], r["reason"]) for r in headline_rows(h)] == [
        ("K10", "pass", "code", writer.HEADLINE_REWRITTEN_REASON)]
    assert out["run"]["model_usd"] == pytest.approx(0.02 + 0.0135 + 6 * 0.0006 + SEARCH_EMBED_USD)


@pytest.mark.parametrize("text, rule", [
    ("Amapiano posts came from 9 creators on TikTok and X.", "K2"),  # a figure no surviving claim holds
    ("Amapiano posts came from creators on TikTok and X, and teenagers love it.", "K6"),
])
def test_a_rewrite_that_fails_a_code_check_leaves_the_short_answer_blank_with_the_gap(text, rule):
    h = Harness(model=HeadlineModel({"text": text}))
    answer = h.run()["answer"]

    assert validate_answer(answer) == []
    assert answer["status"] == "partial"
    assert answer["short_answer"] == ""
    assert text not in json.dumps([answer, h.events])
    assert answer["gaps"].count(HEADLINE_GAP) == 1
    rows = [r for _, batch in h.tables.inserts for r in batch]
    assert any(r["claim_id"] == "short_answer" and r["rule"] == rule and r["verdict"] == "cut" for r in rows)
    # the field check never read the text the code check removed
    field_call = h.model.calls[schemas(h).index("fields")]
    assert text not in field_call["user"]


def test_a_rewrite_the_field_check_flags_leaves_the_short_answer_blank_with_the_gap():
    h = Harness(model=HeadlineModel(forecast_headline=True))
    answer = h.run()["answer"]

    assert answer["status"] == "partial"
    assert answer["short_answer"] == ""
    assert REWRITTEN_HEADLINE not in json.dumps([answer, h.events])
    assert HEADLINE_GAP in answer["gaps"]
    rows = [r for _, batch in h.tables.inserts for r in batch]
    assert any(r["claim_id"] == "short_answer" and r["rule"] == "K9" and r["verdict"] == "cut" for r in rows)


@pytest.mark.parametrize("out, reason", [
    ({"text": "Amapiano posts came from creators on TikTok and X, and " + CUT_CLAIM[0].lower() + CUT_CLAIM[1:]},
     writer.HEADLINE_REPEATS_REASON),
    ({"text": "Amapiano posts came from creators on TikTok and X (c1)."}, writer.HEADLINE_REPEATS_REASON),
    ({"text": "  "}, writer.HEADLINE_UNUSABLE_REASON),
    ({"verdict": "supported"}, writer.HEADLINE_FAILED_REASON),  # does not parse as one text
])
def test_a_rewrite_held_back_by_code_or_failing_leaves_the_short_answer_blank_with_the_gap(out, reason):
    h = Harness(model=HeadlineModel(out))
    result = h.run()
    answer = result["answer"]

    assert validate_answer(answer) == []
    assert answer["status"] == "partial"
    assert answer["short_answer"] == ""
    assert HEADLINE_GAP in answer["gaps"]
    assert [r["reason"] for r in headline_rows(h)] == [reason]
    assert schemas(h).count("headline") == 1
    assert "fields" in schemas(h)  # the answer goes on to its field check
    field_call = h.model.calls[schemas(h).index("fields")]
    assert "(short_answer;" not in field_call["user"]
    # a dispatched call is booked: its usage when it reports one, else the most it could cost
    rewrite_usd = (ask.call_usd(ask.MODEL, writer.HEADLINE_REWRITE_INPUT_TOKENS, writer.HEADLINE_REWRITE_MAX_TOKENS)
                   if isinstance(out, Exception) else 0.0006)
    assert result["run"]["model_usd"] == pytest.approx(0.02 + 0.0135 + 5 * 0.0006 + rewrite_usd + SEARCH_EMBED_USD)


def test_no_rewrite_is_made_when_the_question_budget_has_no_room_for_it_and_the_field_check(monkeypatch):
    configure_gemini(monkeypatch)
    real = AskModelBudget.affords
    probes = []

    def nearly_spent(self, model, calls):
        # the hold has room for one full field check and nothing more when the rewrite is asked for
        field, _, _ = self._ceiling(model, ask.FIELD_INPUT_TOKENS, ask.FIELD_MAX_TOKENS)
        self.cap_micros = self._booked_micros + self._in_flight_micros + field
        probes.append(calls)
        return real(self, model, calls)

    monkeypatch.setattr(AskModelBudget, "affords", nearly_spent)
    h = Harness(model=HeadlineModel())
    out = h.run(tier="T1")
    answer = out["answer"]

    assert probes == [[(writer.HEADLINE_REWRITE_INPUT_TOKENS, writer.HEADLINE_REWRITE_MAX_TOKENS),
                       (ask.FIELD_INPUT_TOKENS, ask.FIELD_MAX_TOKENS)]]
    assert "headline" not in schemas(h)
    assert "fields" in schemas(h)  # the probe stopped nothing: the field check still ran
    assert answer["status"] == "partial" and answer["short_answer"] == ""
    assert HEADLINE_GAP in answer["gaps"]
    assert [r["reason"] for r in headline_rows(h)] == [writer.HEADLINE_BUDGET_REASON]
    assert not any("budget" in n for n in out["run"]["notices"])


def test_the_budget_probe_never_stops_later_calls():
    budget = AskModelBudget(0.003, 0.003, price_for_fn=lambda model: {"input": 1.0, "output": 1.0},
                            reserve_output_fn=lambda model, tokens: tokens)

    assert budget.affords("gemini-test", [(1000, 1000)])
    assert not budget.affords("gemini-test", [(1000, 1000), (1000, 1000)])
    assert not budget.affords("gemini-test", [(0, 1000)])
    assert not budget.stopped and budget.booked_usd == 0
    budget.reserve("gemini-test", 1000, 1000)  # a call that fits still reserves
    assert not budget.affords("gemini-test", [(1000, 1000)])


def test_fewer_than_two_survivors_keep_the_insufficient_evidence_path_with_no_rewrite():
    class CutsC3Too(HeadlineModel):
        def complete_json(self, *, system, user, schema, model, max_tokens):
            if schema is SUPPORT_SCHEMA and "challenge is spreading" in user:
                self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
                return {"verdict": "unsupported", "reason": "not shown", "demographic_inference": False,
                        "tone_claim": False, "forecast_assertion": False, "country_people": []}, \
                    {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
            return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)

    h = Harness(model=CutsC3Too())
    answer = h.run()["answer"]

    assert [c["id"] for c in answer["claims"]] == ["c1"]
    assert answer["status"] == "insufficient_evidence"
    assert answer["short_answer"] == checks.INSUFFICIENT
    assert "headline" not in schemas(h)
    assert HEADLINE_GAP not in answer["gaps"]
    assert headline_rows(h) == []


def test_an_answer_whose_short_answer_survives_makes_no_rewrite():
    class KeepsEveryClaim(HeadlineModel):
        writer_out = {**WRITER_OUT, "claims": [WRITER_OUT["claims"][0], WRITER_OUT["claims"][2]],
                      "so_what": WRITER_OUT["so_what"][:1]}

    h = Harness(model=KeepsEveryClaim())
    answer = h.run()["answer"]

    assert answer["short_answer"] == WRITER_OUT["short_answer"]
    assert "headline" not in schemas(h)
    assert HEADLINE_GAP not in answer["gaps"]


def test_a_t2_ask_makes_one_rewrite_and_a_critic_cut_it_rested_on_blanks_it_with_the_gap():
    survivor = {"id": "c5", "text": "Amapiano posts on X talked about braai gatherings and the speakers at every "
                                    "taxi rank.",
                "label": "single_source", "kind": "observation", "evidence_ids": ["x_1", "x_2"], "quotes": [],
                "numbers": []}

    class Model(CriticModel):
        writer_out = {**WRITER_OUT, "claims": [*WRITER_OUT["claims"], survivor]}

        def complete_json(self, *, system, user, schema, model, max_tokens):
            if schema is HEADLINE_REWRITE_SCHEMA:
                self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
                return {"text": survivor["text"]}, {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
            return super().complete_json(system=system, user=user, schema=schema, model=model,
                                         max_tokens=max_tokens)

    h = t2(model=Model({"c5": {"verdict": "cut"}}))
    answer = h.run(tier="T2")["answer"]

    assert schemas(h).count("headline") == 1
    assert [c["id"] for c in answer["claims"]] == ["c1", "c3"]
    assert answer["status"] == "partial" and answer["short_answer"] == ""
    assert answer["gaps"].count(HEADLINE_GAP) == 1
    assert survivor["text"] not in json.dumps(answer)


def test_the_gap_is_plain_and_reads_as_the_codes():
    text = " ".join(HEADLINE_GAP.values())
    assert "—" not in text and "–" not in text
    assert not checks._text_breaches(text)
    assert writer.writer_gaps([HEADLINE_GAP]) == []
    assert all(not checks._text_breaches(reason) and "—" not in reason for reason in writer.HEADLINE_REASONS)


# Live staging, 7 October: a complete answer with five claims, one of them narrowed by the support check and kept,
# showed no short answer, no context and no gap saying why. Nothing was cut, so the status stayed complete.
NARROWED_ORIGINAL = WRITER_OUT["claims"][1]["text"]


class NarrowingModel(HeadlineModel):
    """HeadlineModel whose writer leaves out c4, so the code checks cut nothing, and whose K4 rewrite narrows c2,
    which the support check then keeps: the answer stays complete with the short answer blanked."""

    writer_out = {**WRITER_OUT, "claims": WRITER_OUT["claims"][:3]}

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is K4_REWRITE_SCHEMA:
            self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
            return {"text": NARROWED_CLAIM}, {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
        return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)


NARROWED_SUMMARY_GAP = {
    "what": "One-line summary removed after a claim was narrowed",
    "searched": "the short answer text and the retained claims",
    "why": "the original summary could repeat the broader claim, and no checked replacement could take its place",
}
REMOVED_CONTEXT_GAP = {
    "what": "Background removed after a claim was narrowed or removed",
    "searched": "the background text",
    "why": "the background could repeat wording that did not pass the claim checks",
}
REMOVED_IMPLICATION_GAP = {
    "what": "An implication removed after a claim was narrowed or removed",
    "searched": "the implications and the claims they refer to",
    "why": "the implication relied on wording that did not pass the claim checks",
}


@pytest.mark.parametrize("tier", ["T0", "T1"])
def test_narrowed_blank_summary_keeps_exact_smoke_gap_and_names_the_narrowing(tier):
    h = Harness(model=NarrowingModel({"text": ""}))
    answer = h.run(tier=tier)["answer"]
    assert answer["gaps"].count(NARROWED_SUMMARY_GAP) == 1
    assert answer["gaps"].count(HEADLINE_GAP) == 1
    assert answer["status"] == "partial" and answer["short_answer"] == ""
    assert schemas(h).count("headline") == 1
    assert validate_answer(answer) == []


def test_narrowing_records_actual_context_and_implication_removal_without_extra_calls():
    class WithContext(NarrowingModel):
        writer_out = {**NarrowingModel.writer_out, "context": "An interpretation of the earlier post wording."}

    h = Harness(model=WithContext())
    answer = h.run()["answer"]
    assert answer["context"] == "" and answer["gaps"].count(REMOVED_CONTEXT_GAP) == 1
    assert [item["claim_ids"] for item in answer["so_what"]] == [["c1"]]
    assert answer["gaps"].count(REMOVED_IMPLICATION_GAP) == 1
    assert NARROWED_SUMMARY_GAP not in answer["gaps"] and HEADLINE_GAP not in answer["gaps"]
    assert answer["status"] == "complete" and answer["claims"][1]["text"] == NARROWED_CLAIM
    assert schemas(h).count("headline") == schemas(h).count("k4_rewrite") == 1
    assert schemas(h).count("support") == 4
    assert schemas(h).count("fields") == 1
    assert writer.writer_gaps([REMOVED_CONTEXT_GAP, REMOVED_IMPLICATION_GAP, NARROWED_SUMMARY_GAP]) == []


def test_narrowing_leaves_a_k10_downgrade_audit_without_cutting_the_checked_claim():
    h = Harness(model=NarrowingModel())
    answer = h.run()["answer"]
    rows = [r for _, batch in h.tables.inserts for r in batch]
    assert any(r["claim_id"] == "short_answer" and r["rule"] == "K10" and r["verdict"] == "downgrade"
               and r["reason"] == "the support check narrowed a claim to the wording its cited posts support; "
                                  "dependent answer text was cleared for rechecking" for r in rows)
    assert answer["status"] == "complete" and answer["claims"][1]["text"] == NARROWED_CLAIM
    assert answer["short_answer"] == REWRITTEN_HEADLINE


def test_narrowing_adds_no_context_or_implication_gap_when_neither_field_was_removed():
    class WithNoAffectedFields(NarrowingModel):
        writer_out = {**NarrowingModel.writer_out, "context": "", "so_what": WRITER_OUT["so_what"][:1]}

    answer = Harness(model=WithNoAffectedFields()).run()["answer"]
    assert REMOVED_CONTEXT_GAP not in answer["gaps"] and REMOVED_IMPLICATION_GAP not in answer["gaps"]
    assert answer["so_what"] == WRITER_OUT["so_what"][:1]


def test_a_complete_answer_whose_short_answer_a_narrowed_claim_blanked_is_rewritten_once_and_kept():
    h = Harness(model=NarrowingModel())
    answer = h.run()["answer"]

    assert validate_answer(answer) == []
    assert [c["id"] for c in answer["claims"]] == ["c1", "c2", "c3"]
    assert answer["claims"][1]["text"] == NARROWED_CLAIM
    assert answer["status"] == "complete"  # nothing was cut, and the rewrite passed every check
    assert answer["short_answer"] == REWRITTEN_HEADLINE
    assert HEADLINE_GAP not in answer["gaps"]
    assert schemas(h).count("headline") == 1
    assert schemas(h).index("headline") < schemas(h).index("fields")
    rewrite = next(call for call in h.model.calls if call["schema"] is HEADLINE_REWRITE_SCHEMA)
    # the narrowed words reach it, never the original ones
    assert NARROWED_CLAIM in rewrite["user"] and NARROWED_ORIGINAL not in rewrite["user"]
    field_call = h.model.calls[schemas(h).index("fields")]
    assert "item 0 (short_answer;" in field_call["user"] and REWRITTEN_HEADLINE in field_call["user"]
    assert [(r["verdict"], r["reason"]) for r in headline_rows(h)] == [("pass", writer.HEADLINE_REWRITTEN_REASON)]
    # the so_what item resting on the narrowed claim restated its unchecked words and stays withheld
    assert [s["claim_ids"] for s in answer["so_what"]] == [["c1"]]


@pytest.mark.parametrize("out, reason", [
    (RuntimeError("provider failed"), writer.HEADLINE_FAILED_REASON),
    ({"text": "  "}, writer.HEADLINE_UNUSABLE_REASON),
    # the narrowed claim's original words are held back as a cut claim's are
    ({"text": "Amapiano posts came from creators on TikTok and X, and people say amapiano is fading."},
     writer.HEADLINE_REPEATS_REASON),
])
def test_a_narrowed_claims_blank_short_answer_that_stays_blank_gets_the_gap_and_lowers_complete_to_partial(out,
                                                                                                         reason):
    h = Harness(model=NarrowingModel(out))
    answer = h.run()["answer"]

    assert validate_answer(answer) == []
    assert [c["id"] for c in answer["claims"]] == ["c1", "c2", "c3"]
    assert answer["status"] == "partial"
    assert answer["short_answer"] == ""
    assert answer["gaps"].count(HEADLINE_GAP) == 1
    assert [r["reason"] for r in headline_rows(h)] == [reason]
    assert schemas(h).count("headline") == 1
    assert NARROWED_ORIGINAL.lower().rstrip(".") not in json.dumps(answer).lower()
    assert "fields" in schemas(h)


def test_a_narrowed_claims_rewrite_the_field_check_flags_lowers_complete_to_partial_with_the_gap():
    h = Harness(model=NarrowingModel(forecast_headline=True))
    answer = h.run()["answer"]

    assert answer["status"] == "partial"
    assert answer["short_answer"] == ""
    assert REWRITTEN_HEADLINE not in json.dumps([answer, h.events])
    assert answer["gaps"].count(HEADLINE_GAP) == 1


def test_a_narrowed_claim_with_no_second_claim_keeps_the_insufficient_evidence_path_with_no_rewrite():
    class OneClaim(NarrowingModel):
        writer_out = {**WRITER_OUT, "claims": [WRITER_OUT["claims"][1]], "so_what": [], "watch_next": []}

    h = Harness(model=OneClaim())
    answer = h.run()["answer"]

    assert [c["text"] for c in answer["claims"]] == [NARROWED_CLAIM]
    assert answer["status"] == "insufficient_evidence"
    assert answer["short_answer"] == checks.INSUFFICIENT
    assert "headline" not in schemas(h)
    assert HEADLINE_GAP not in answer["gaps"]
    assert headline_rows(h) == []


def test_a_complete_answer_with_its_short_answer_intact_keeps_its_status_and_makes_no_rewrite():
    class NoNarrowing(NarrowingModel):
        def complete_json(self, *, system, user, schema, model, max_tokens):
            if schema is K4_REWRITE_SCHEMA:
                raise AssertionError("no claim needs narrowing")
            return super().complete_json(system=system, user=user, schema=schema, model=model,
                                         max_tokens=max_tokens)

        writer_out = {**WRITER_OUT, "claims": [WRITER_OUT["claims"][0], WRITER_OUT["claims"][2]],
                      "so_what": WRITER_OUT["so_what"][:1]}

    h = Harness(model=NoNarrowing())
    answer = h.run()["answer"]

    assert answer["status"] == "complete"
    assert answer["short_answer"] == WRITER_OUT["short_answer"]
    assert "headline" not in schemas(h)
    assert HEADLINE_GAP not in answer["gaps"]


@pytest.mark.parametrize("out, status, headline", [
    (None, "complete", REWRITTEN_HEADLINE),
    (RuntimeError("provider failed"), "partial", ""),
])
def test_a_t2_ask_treats_a_narrowed_claims_blank_short_answer_as_t1_does(out, status, headline):
    class Model(CriticModel):
        writer_out = NarrowingModel.writer_out

        def complete_json(self, *, system, user, schema, model, max_tokens):
            if schema is K4_REWRITE_SCHEMA:
                self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
                return {"text": NARROWED_CLAIM}, {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
            if schema is HEADLINE_REWRITE_SCHEMA:
                self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
                if isinstance(out, Exception):
                    raise out
                return {"text": REWRITTEN_HEADLINE}, {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
            return super().complete_json(system=system, user=user, schema=schema, model=model,
                                         max_tokens=max_tokens)

    h = t2(model=Model())
    answer = h.run(tier="T2")["answer"]

    assert validate_answer(answer) == []
    assert schemas(h).count("headline") == 1
    assert [c["id"] for c in answer["claims"]] == ["c1", "c2", "c3"]
    assert answer["status"] == status and answer["short_answer"] == headline
    assert answer["gaps"].count(HEADLINE_GAP) == (0 if headline else 1)
    assert answer["gaps"].count(NARROWED_SUMMARY_GAP) == (0 if headline else 1)


class Budgets(AskModelBudget):
    """AskModelBudget that keeps each question's budget, so a test can read what it booked."""

    made: list = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.unknown = []  # the reserve, in micros, of each call settled with unknown usage
        Budgets.made.append(self)

    def settle(self, reservation, usage, *, stop_unknown=True):
        if not self._known_usage(usage):
            self.unknown.append(reservation.ceiling_micros)
        return super().settle(reservation, usage, stop_unknown=stop_unknown)


@pytest.mark.parametrize("tier", ["T0", "T1"])
@pytest.mark.parametrize("model", [HeadlineModel, NarrowingModel], ids=["cut", "narrowed"])
def test_a_gemini_rewrite_that_fails_with_unknown_usage_books_its_reserve_and_the_field_check_still_runs(
        monkeypatch, tier, model):
    # The rewrite raises with no usage, so the budget cannot know what it billed. It books the rewrite's full reserve,
    # which the room asked for before it already held, and the field check after it runs on the rest.
    configure_gemini(monkeypatch)
    monkeypatch.setattr(Budgets, "made", [])
    monkeypatch.setattr(ask, "AskModelBudget", Budgets)
    h = Harness(model=model(RuntimeError("provider failed")))
    out = h.run(tier=tier)
    answer = out["answer"]

    (budget,) = Budgets.made
    assert validate_answer(answer) == []
    assert answer["status"] == "partial"  # from complete for a narrowed claim, else unchanged
    assert answer["short_answer"] == ""
    assert answer["gaps"].count(HEADLINE_GAP) == 1
    assert [r["reason"] for r in headline_rows(h)] == [writer.HEADLINE_FAILED_REASON]
    assert schemas(h).count("headline") == 1
    assert schemas(h)[-1] == "fields"  # the field check ran after the failed rewrite
    assert not budget.stopped and budget.stop_reason is None
    assert not any("budget" in n for n in out["run"]["notices"])
    # the rewrite is booked at its full reserve, within the room the probe held for it
    rewrite_usd = ask.call_usd(ask.MODEL, writer.HEADLINE_REWRITE_INPUT_TOKENS, writer.HEADLINE_REWRITE_MAX_TOKENS)
    (reserve,) = budget.unknown
    assert budget.conservative_usd == reserve / 1_000_000
    assert 0 < budget.conservative_usd <= rewrite_usd
    assert out["run"]["model_usd"] >= budget.booked_usd


class ServerFailed(Exception):
    code = 503  # a 5xx from Vertex with no usage on it


@pytest.mark.parametrize("room", [True, False], ids=["retry_fits", "retry_would_crowd_the_field_check"])
def test_a_gemini_rewrite_that_fails_on_googles_side_retries_only_within_the_field_checks_room(monkeypatch, room):
    configure_gemini(monkeypatch)
    monkeypatch.setattr("core.agent.gemini_research.RETRY_WAIT_S", 0)
    monkeypatch.setattr(Budgets, "made", [])
    monkeypatch.setattr(ask, "AskModelBudget", Budgets)
    real = AskModelBudget.affords
    probes = []

    def affords(self, model, calls):
        probes.append(calls)
        if not room and len(probes) == 2:  # the retry's probe: only the field check still fits
            field, _, _ = self._ceiling(model, ask.FIELD_INPUT_TOKENS, ask.FIELD_MAX_TOKENS)
            self.cap_micros = self._booked_micros + self._in_flight_micros + field
        return real(self, model, calls)

    monkeypatch.setattr(AskModelBudget, "affords", affords)
    h = Harness(model=NarrowingModel(ServerFailed("unavailable")))
    out = h.run(tier="T1")
    answer = out["answer"]

    (budget,) = Budgets.made
    assert len(probes) == 2 and probes[1][1:] == [(ask.FIELD_INPUT_TOKENS, ask.FIELD_MAX_TOKENS)]
    assert schemas(h).count("headline") == (2 if room else 1)
    assert schemas(h)[-1] == "fields"
    assert not budget.stopped
    assert answer["status"] == "partial" and answer["short_answer"] == ""
    assert answer["gaps"].count(HEADLINE_GAP) == 1
    # each try is booked at its full reserve
    rewrite_usd = ask.call_usd(ask.MODEL, writer.HEADLINE_REWRITE_INPUT_TOKENS, writer.HEADLINE_REWRITE_MAX_TOKENS)
    assert len(budget.unknown) == (2 if room else 1)
    assert budget.conservative_usd == sum(budget.unknown) / 1_000_000
    assert all(0 < reserve / 1_000_000 <= rewrite_usd for reserve in budget.unknown)
    assert out["run"]["model_usd"] >= budget.booked_usd
