"""The blind test harness picks a model per role from replayed questions (BUILD.md task 1.18), with fake models only."""

import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.agent.ask import SPEND_SQL
from core.agent.critic import CRITIC_SCHEMA
from core.agent.writer import WRITER_SCHEMA
from core.eval import blind_test
from core.llm.provider import GEMINI_LIST_PRICES, price_for

EVAL = Path(__file__).resolve().parents[1]
BLIND = EVAL / "tests" / "fixtures" / "blind"
# Three Gemini candidate ids at three list prices (registered below), plus the default model, gemini-3.8-flash.
MID, TOP, LOW, GEMINI = "gemini-9-mid", "gemini-9-5-pro", "gemini-4-5-lite", "gemini-3.8-flash"
CANDIDATE_PRICES = {MID: {"input": 3.00, "output": 15.00}, TOP: {"input": 5.00, "output": 25.00},
                    LOW: {"input": 1.00, "output": 5.00}}
NAMES = re.compile(r"gemini", re.I)
ANSWER_BLOCK = re.compile(r"Answer ([A-Z])\n<untrusted_content>\n(.*?)\n</untrusted_content>", re.S)


@pytest.fixture(autouse=True)
def three_candidates(monkeypatch):
    """Every test prices the three candidates at their own list prices: no MODEL_PROVIDER and no GEMINI_PRICE_*
    override, which would price every Gemini id alike."""
    for name in ("MODEL_PROVIDER", "GEMINI_PRICE_INPUT_PER_M", "GEMINI_PRICE_OUTPUT_PER_M"):
        monkeypatch.delenv(name, raising=False)
    for model, price in CANDIDATE_PRICES.items():
        monkeypatch.setitem(GEMINI_LIST_PRICES, model, price)


def records():
    return blind_test.load_replay(BLIND)


def after_answer_record():
    return {**records()[0], "replay_metadata": {"snapshot_phase": "after_answer", "research_note_available": False}}


def ids_in(user):
    return re.findall(r'post \{"id": "([^"]+)"', user)


def good_writer(user):
    ids = ids_in(user)
    return {"short_answer": "Posts in the pack describe the change.", "claims": [
        {"id": "c1", "text": "Posters describe the change on more than one platform", "label": "observed",
         "kind": "observation", "evidence_ids": ids[:2], "quotes": [], "numbers": []},
        {"id": "c2", "text": "One poster describes the change at length", "label": "single_source",
         "kind": "observation", "evidence_ids": ids[:1], "quotes": [], "numbers": []}],
        "so_what": [], "watch_next": [], "gaps": [], "context": ""}


def named_writer(user):
    out = good_writer(user)
    out["context"] = "Written by Gemini Pro from Google"  # no digits, so the code gate keeps it
    return out


def weak_writer(user):
    ids = ids_in(user)
    return {"short_answer": "", "claims": [
        {"id": "c1", "text": "X carried 900 posts on it", "label": "observed", "kind": "observation",
         "evidence_ids": ids[:1], "quotes": [], "numbers": [{"value": 900, "unit": "posts", "query_id": "q_9"}]}],
        "so_what": [], "watch_next": [], "gaps": [], "context": ""}


def pinned_writer(user):
    ids = ids_in(user)
    return {"short_answer": "", "claims": [
        {"id": "c1", "text": "TikTok carried 1,250 posts on the car wash sessions", "label": "single_source",
         "kind": "observation", "evidence_ids": ids[:1], "quotes": [],
         "numbers": [{"value": 1250, "unit": "posts", "query_id": "q_1"}]},
        {"id": "c2", "text": "X carried 900 posts on the car wash sessions", "label": "single_source",
         "kind": "observation", "evidence_ids": ids[1:2], "quotes": [],
         "numbers": [{"value": 900, "unit": "posts", "query_id": "q_9"}]}],
        "so_what": [], "watch_next": [], "gaps": [], "context": ""}


def verdict(cid, v, label="", query=""):
    return {"claim_id": cid, "verdict": v, "label": label, "reason": "checked against the cited post", "quote": "",
            "query": query}


def sharp_critic(user):
    return {"verdicts": [verdict("c1", "keep"), verdict("c2", "keep"), verdict("c3", "cut"), verdict("c4", "cut"),
                         verdict("c5", "downgrade", "single_source")],
            "missing_perspectives": [], "followups": [], "overall_risk": "medium"}


def mixed_critic(user):
    return {"verdicts": [verdict("c1", "keep"), verdict("c2", "cut"),
                         verdict("c3", "needs_evidence", query="car wash prices"), verdict("c4", "cut"),
                         verdict("c5", "downgrade", "single_source")],
            "missing_perspectives": [], "followups": [], "overall_risk": "high"}


def fair_grader(system, user):
    grades = []
    for label, body in ANSWER_BLOCK.findall(user):
        rating = 5 if len(json.loads(body).get("claims") or []) >= 2 else 2
        grades.append({"label": label, **{d: rating for d in blind_test.DIMENSIONS}, "hard_fail": False,
                       "reason": f"Rating {rating}/5"})
    return {"grades": grades}


class FakeModel:
    """Answers each schema from a per-model function and records every call."""

    def __init__(self, writers=None, critics=None, grader=fair_grader):
        self.writers = writers or {MID: good_writer, TOP: named_writer, LOW: weak_writer}
        self.critics = critics or {MID: sharp_critic, TOP: sharp_critic, LOW: sharp_critic}
        self.grader = grader
        self.calls = []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        self.calls.append({"system": system, "user": user, "schema": schema, "model": model})
        if schema is WRITER_SCHEMA:
            out = self.writers[model](user)
        elif schema is CRITIC_SCHEMA:
            out = self.critics[model](user)
        elif schema is blind_test.GRADER_SCHEMA:
            out = self.grader(system, user)
        else:
            raise AssertionError("unexpected schema")
        return out, {"input_tokens": 1000, "output_tokens": 500, "usd": fake_usd(model)}


def fake_usd(model):
    price = price_for(model)
    return (1000 * price["input"] + 500 * price["output"]) / 1_000_000


NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone(timedelta(hours=2)))


def run(model, rows=None, **kw):
    kw.setdefault("models", [MID, TOP, LOW])
    kw.setdefault("record_run", (rows if rows is not None else []).append)
    kw.setdefault("now", lambda: NOW)
    return blind_test.run_blind(records(), model=model, **kw)


def test_replay_loads_one_record_per_file_with_a_context():
    loaded = records()
    assert [r["id"] for r in loaded] == ["KE-01", "ZA-01"]
    ctx = blind_test.context_for(loaded[1])
    assert ctx.market == "ZA" and set(ctx.evidence) == {"tt_7400000000000000001", "x_1840000000000000002"}
    assert ctx.queries["q_1"]["rows"][0]["posts"] == 1250


def test_load_replay_rejects_after_answer_saved_attempts(tmp_path):
    path = tmp_path / "after-answer.json"
    path.write_text(json.dumps(after_answer_record()), encoding="utf-8")

    with pytest.raises(ValueError, match="code and provenance replay only; original writer inputs are unavailable"):
        blind_test.load_replay(path)


def test_cli_rejects_after_answer_before_model_or_warehouse_dispatch(monkeypatch, tmp_path):
    path = tmp_path / "after-answer.json"
    path.write_text(json.dumps(after_answer_record()), encoding="utf-8")
    monkeypatch.setattr(blind_test, "Routed", lambda: pytest.fail("model constructed before input guard"))
    monkeypatch.setattr(blind_test, "bigquery_runs_writer",
                        lambda: pytest.fail("warehouse constructed before input guard"))

    with pytest.raises(ValueError, match="original writer inputs are unavailable"):
        blind_test.main(["--replay", str(path)])


def test_after_answer_snapshot_stays_available_for_context_and_provenance_checks():
    record = after_answer_record()
    ctx = blind_test.context_for(record)
    query = record["queries"]["q_1"]

    assert ctx.run_id == record["run_id"] and ctx.evidence
    assert blind_test.ReplayWarehouse(record).run(query["sql"], query.get("params"), 0) == query["rows"]


def test_run_blind_rejects_after_answer_before_pricing_or_model_dispatch(monkeypatch):
    model, rows = FakeModel(), []
    monkeypatch.setattr(blind_test, "_price", lambda name: pytest.fail("priced before input guard"))

    with pytest.raises(ValueError, match="original writer inputs are unavailable"):
        blind_test.run_blind([after_answer_record()], model=model, record_run=rows.append)

    assert model.calls == [] and rows == []


def test_run_writer_rejects_after_answer_before_model_or_budget_dispatch():
    model, guard = FakeModel(), blind_test.Guard(1.0)

    with pytest.raises(ValueError, match="original writer inputs are unavailable"):
        blind_test.run_writer(model, MID, after_answer_record(), lambda: 0.0, guard)

    assert model.calls == [] and guard.spent == 0.0


def test_writer_dispatches_in_negative_control_when_input_guard_is_removed(monkeypatch):
    model, guard = FakeModel(), blind_test.Guard(1.0)
    monkeypatch.setattr(blind_test, "_require_original_writer_inputs", lambda record: None)

    blind_test.run_writer(model, MID, after_answer_record(), lambda: 0.0, guard)

    assert len(model.calls) == 1 and model.calls[0]["schema"] is WRITER_SCHEMA


def test_grader_input_carries_no_model_name_and_the_mapping_stays_in_the_results():
    model = FakeModel()
    results = run(model, roles=["writer"])
    graded = [c for c in model.calls if c["schema"] is blind_test.GRADER_SCHEMA]
    assert len(graded) == 2  # one blind call per question
    for call in graded:
        assert not NAMES.search(call["system"] + call["user"])
        assert "Usefulness" in call["system"] and "Grounding" in call["system"]  # the rubric.md criteria
        assert re.findall(r"^Answer ([A-Z])$", call["user"], re.M) == ["A", "B", "C"]
    assert any("[model]" in call["user"] for call in graded)  # the name reached the grader input and was blanked
    labels = results["grading"]["ZA-01"]["labels"]
    assert sorted(labels.values()) == sorted([MID, TOP, LOW])


def test_anonymise_blanks_model_ids_and_family_words():
    text = blind_test.anonymise("gemini-9-5-pro and Gemini Pro wrote this; gemini-4-5-lite did not",
                                [MID, TOP, LOW])
    assert not NAMES.search(text) and "9-5" not in text and "4-5" not in text


def test_seeded_shuffle_is_stable_and_ignores_input_order():
    models = [MID, TOP, LOW]
    first = blind_test.blind_order(models, 7, "ZA-01")
    assert first == blind_test.blind_order(list(reversed(models)), 7, "ZA-01")
    assert first == blind_test.blind_order(models, 7, "ZA-01")
    assert sorted(first) == sorted(models)
    orders = {tuple(blind_test.blind_order(models, seed, "ZA-01")) for seed in range(20)}
    assert len(orders) > 1  # the seed does move the order


def row(model, score, gate=1.0, usd=None):
    usd = {MID: 0.10, TOP: 0.20, LOW: 0.03}[model] if usd is None else usd
    return {"model": model, "rubric_score": score, "gate_pass_rate": gate, "usd_per_question": usd}


def test_choice_takes_the_cheapest_model_within_the_margin():
    assert blind_test.choose([row(MID, 0.80), row(LOW, 0.76)])[0] == LOW
    assert blind_test.choose([row(MID, 0.80), row(LOW, 0.74)])[0] == MID
    # within the margin but a worse gate pass rate than the best is not eligible
    assert blind_test.choose([row(MID, 0.80), row(LOW, 0.79, gate=0.5)])[0] == MID
    # the dearest model wins only when nothing cheaper is within the margin
    assert blind_test.choose([row(MID, 0.80), row(TOP, 0.90), row(LOW, 0.60)])[0] == TOP
    assert blind_test.choose([row(MID, None), row(LOW, None)])[0] is None


def test_spend_over_the_run_cap_refuses_before_any_call():
    model, rows = FakeModel(), []
    with pytest.raises(blind_test.SpendRefused, match="--max-usd"):
        run(model, rows, max_usd=0.0001)
    assert model.calls == [] and rows == []  # nothing spent, so no runs row


def test_cap_is_eval_model_usd_and_max_usd_only_lowers_it(tmp_path):
    assert blind_test.eval_cap(tmp_path / "missing.yaml") == 40.0  # SETUP.md caps table
    caps = tmp_path / "caps.yaml"
    caps.write_text("MODEL_DAILY_USD: 20\nEVAL_MODEL_USD: 12.5\n", encoding="utf-8")
    assert blind_test.eval_cap(caps) == 12.5
    caps.write_text("MODEL_DAILY_USD: 20\n", encoding="utf-8")
    assert blind_test.eval_cap(caps) == 40.0
    assert blind_test.parser().parse_args(["--replay", "x"]).max_usd == blind_test.eval_cap()
    assert run(FakeModel(), max_usd=1000.0)["cap_usd"] == 40.0
    assert run(FakeModel(), max_usd=3.0)["cap_usd"] == 3.0


def test_the_guard_stops_before_a_call_that_would_pass_the_cap_and_still_writes_what_it_scored(tmp_path):
    model, rows = FakeModel(), []
    first = blind_test.writer_call_usd(MID, records()[0])
    results = run(model, rows, max_usd=first + 0.02)  # the first call fits; TOP's upper bound after it does not
    assert len(model.calls) == 1 and results["stopped"] == "eval cap"
    writer = results["roles"]["writer"]
    assert [q["model"] for q in writer["per_question"] if not q.get("not_run")] == [MID]
    skipped = [q for q in writer["per_question"] if q.get("not_run")]
    assert len(skipped) == 5 and all(q["not_run"] == "eval cap" for q in skipped)
    assert all(g["not_run"] == "eval cap" for g in results["grading"].values())
    assert all(q["not_run"] == "eval cap" for q in results["roles"]["critic"]["per_question"])
    assert writer["chosen"] is None and results["roles"]["critic"]["chosen"] is None
    md, js = blind_test.write_results(results, tmp_path, "2026-09-29")
    assert "eval cap" in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["stopped"] == "eval cap"
    assert len(rows) == 1 and rows[0]["status"] == "stopped"


def test_the_run_books_one_eval_row_apart_from_the_daily_model_cap():
    rows = []
    results = run(FakeModel(), rows)
    assert len(rows) == 1
    row_ = rows[0]
    assert set(row_) == {"run_id", "stage", "run_date", "status", "started_at", "finished_at", "counts"}
    assert row_["stage"] == "eval" and row_["status"] == "ok" and row_["run_date"] == date(2026, 9, 29)
    assert row_["run_id"] == results["run_id"]
    counts = json.loads(row_["counts"])
    assert counts["eval_usd"] == pytest.approx(results["spent_usd"]) and counts["eval_usd"] > 0
    assert "model_usd" not in counts and counts["stopped"] is None and counts["live_credits"] == 0
    # Ask's daily spend read counts model_usd only, so this row never reaches MODEL_DAILY_USD
    assert "model_usd" in SPEND_SQL and "eval_usd" not in SPEND_SQL
    assert blind_test.EVAL_ROW_SQL.startswith("INSERT INTO `ogilvy-trends-v2.intelligence_42_agent.runs`")
    assert "'eval'" in blind_test.EVAL_ROW_SQL and "PARSE_JSON(@counts)" in blind_test.EVAL_ROW_SQL


def test_a_run_that_crashes_still_books_what_it_spent(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("gate broke")

    monkeypatch.setattr(blind_test, "check_answer", broken)
    rows = []
    with pytest.raises(RuntimeError):
        run(FakeModel(), rows)
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert json.loads(rows[0]["counts"])["eval_usd"] > 0  # the writer call before the crash was billed


def test_docstring_names_the_grader_family_and_the_orchestrator_replay():
    assert "same family as the candidates" in blind_test.__doc__ and "tool-level replay" in blind_test.__doc__


def test_estimate_grows_with_models_and_refuses_an_unpriced_one():
    one = blind_test.estimate_usd(records(), ["writer", "critic"], [LOW], MID)
    three = blind_test.estimate_usd(records(), ["writer", "critic"], [MID, TOP, LOW], MID)
    assert 0 < one < three
    model = FakeModel()
    with pytest.raises(blind_test.SpendRefused, match="no price"):
        run(model, models=[MID, "gemini-3.5-flash"])
    assert model.calls == []


def test_critic_scoring_counts_the_seeded_bad_claims():
    model = FakeModel(critics={MID: sharp_critic, TOP: mixed_critic, LOW: mixed_critic})
    results = run(model, roles=["critic"])
    per = {q["model"]: q for q in results["roles"]["critic"]["per_question"]}
    assert set(per) == {MID, TOP, LOW}  # KE-01 has no critic case, so only ZA-01 is scored
    assert per[MID]["caught"] == ["c3", "c4", "c5"] and per[MID]["wrongly_cut"] == []
    assert per[TOP]["caught"] == ["c4", "c5"] and per[TOP]["missed"] == ["c3"]
    assert per[TOP]["wrongly_cut"] == ["c2"]
    rows = {r["model"]: r for r in results["roles"]["critic"]["rows"]}
    assert rows[MID]["rubric_score"] == 1.0 and rows[TOP]["rubric_score"] == pytest.approx(0.6)
    assert results["roles"]["critic"]["chosen"] == MID


def test_writer_gate_counts_cuts_pins_and_rule_verdicts():
    model = FakeModel(writers={MID: pinned_writer})
    results = run(model, roles=["writer"], models=[MID])
    za = next(q for q in results["roles"]["writer"]["per_question"] if q["question_id"] == "ZA-01")
    assert za["claims"] == 2 and za["cut"] == 1
    assert za["numbers"] == 2 and za["pinned"] == 1
    assert za["k_cuts"] == {"K2": 1}
    assert za["gate_pass"] is False


def test_results_files_hold_a_table_per_role_and_a_chosen_model(tmp_path):
    results = run(FakeModel())
    md, js = blind_test.write_results(results, tmp_path, "2026-09-29")
    assert md.name == "blind_2026-09-29_100000.md" and js.name == "blind_2026-09-29_100000.json"
    text = md.read_text(encoding="utf-8")
    assert "| model | gate pass rate | rubric score | USD per question | seconds per question |" in text
    assert "## writer" in text and "## critic" in text and "## orchestrator" in text
    assert f"Chosen: {MID}" in text  # TOP ties MID at a higher price, LOW's answers are cut
    assert "not isolable offline" in text
    assert not re.search(r"[\u2013\u2014]|\s-{2}\s", text)
    saved = json.loads(js.read_text(encoding="utf-8"))
    assert saved["roles"]["writer"]["chosen"] == MID
    assert saved["roles"]["orchestrator"]["status"] == "not_isolable_offline"
    assert saved["spent_usd"] > 0 and saved["estimate_usd"] >= saved["spent_usd"]
    writer = {r["model"]: r for r in saved["roles"]["writer"]["rows"]}
    assert writer[LOW]["gate_pass_rate"] == 0.0 and writer[MID]["gate_pass_rate"] == 1.0


def test_cli_help_works_from_any_directory(tmp_path):
    done = subprocess.run([sys.executable, str(EVAL / "blind_test.py"), "--help"], cwd=tmp_path,
                          capture_output=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    assert "--max-usd" in done.stdout and "--grader" in done.stdout


def test_a_grading_skipped_at_the_cap_blocks_the_writer_choice():
    rows = []
    results = run(FakeModel(), rows, roles=["writer"], grader=TOP, max_usd=0.2675)  # the reviewer's probe
    assert results["stopped"] == "eval cap" and results["roles"]["writer"]["chosen"] is None
    assert json.loads(rows[0]["counts"])["not_run"] >= 1


def test_only_the_last_grading_skipped_still_blocks_the_choice_and_is_counted():
    rows, loaded = [], records()
    spent = 2 * sum(fake_usd(m) for m in (MID, TOP, LOW)) + fake_usd(TOP)  # before the second grading
    cap = spent + blind_test.grader_call_usd(TOP, loaded[1], 3) - 1e-4
    results = run(FakeModel(), rows, roles=["writer"], grader=TOP, max_usd=cap)
    writer = results["roles"]["writer"]
    assert not any(q.get("not_run") for q in writer["per_question"])  # all six writer cells ran
    assert "not_run" not in results["grading"]["KE-01"] and results["grading"]["ZA-01"]["not_run"] == "eval cap"
    assert writer["chosen"] is None
    assert json.loads(rows[0]["counts"])["not_run"] == 1


def test_the_writer_bound_counts_the_note_gaps_failed_sources_and_question_at_two_characters_a_token():
    record = records()[1]
    base = blind_test.writer_call_usd(MID, record)
    at_two = 10_000 / 2 * price_for(MID)["input"] / 1_000_000
    long = "x" * 10_000
    for field, value in (("note", long), ("gaps", [{"what": long, "searched": "", "why": ""}]),
                         ("failed_sources", [{"route": long, "status": "error"}]), ("question", long)):
        assert blind_test.writer_call_usd(MID, {**record, field: value}) - base >= at_two * 0.99, field


def test_grader_spend_is_booked_before_its_output_is_read():
    model = FakeModel(grader=lambda system, user: ["not", "a", "grade"])
    results = run(model, roles=["writer"])
    assert all(s == 0.0 for s in results["grading"]["ZA-01"]["scores"].values())  # ungraded scores zero
    assert sorted(results["grading"]["ZA-01"]["ungraded"]) == sorted([MID, TOP, LOW])
    assert results["spent_usd"] == pytest.approx(2 * sum(fake_usd(m) for m in (MID, TOP, LOW))
                                                 + 2 * fake_usd(blind_test.DEFAULT_GRADER))


def test_writer_spend_is_booked_when_its_post_processing_raises():
    model = FakeModel(writers={MID: lambda user: {"claims": ["not a claim"]}})
    results = run(model, roles=["writer"], models=[MID])
    cell = results["roles"]["writer"]["per_question"][0]
    assert cell["error"] == "AttributeError" and cell["usd"] == pytest.approx(fake_usd(MID))
    assert results["spent_usd"] == pytest.approx(2 * fake_usd(MID))  # no answer survives, so no grader call


def test_results_are_kept_when_the_runs_row_write_fails(tmp_path):
    def broken(row):
        raise RuntimeError("warehouse down")

    results = run(FakeModel(), record_run=broken)
    assert results["runs_row_error"] == "RuntimeError"
    md, _ = blind_test.write_results(results, tmp_path, "2026-09-29")
    assert "runs row" in md.read_text(encoding="utf-8")


def test_an_interrupt_books_stopped_never_ok_and_carries_what_was_scored():
    def interrupted(user):
        raise KeyboardInterrupt

    rows = []
    with pytest.raises(KeyboardInterrupt) as caught:
        run(FakeModel(writers={MID: good_writer, TOP: good_writer, LOW: interrupted}), rows)
    assert len(rows) == 1 and rows[0]["status"] == "stopped"
    assert caught.value.results["spent_usd"] == pytest.approx(fake_usd(MID) + fake_usd(TOP))


def downgrading_critic(user):
    return {"verdicts": [verdict("c1", "keep"), verdict("c2", "keep"), verdict("c3", "downgrade", "single_source"),
                         verdict("c4", "cut"), verdict("c5", "downgrade", "single_source")],
            "missing_perspectives": [], "followups": [], "overall_risk": "medium"}


def test_a_downgrade_catches_only_an_unpinned_number():
    model = FakeModel(critics={MID: downgrading_critic})
    cell = run(model, roles=["critic"], models=[MID])["roles"]["critic"]["per_question"][0]
    assert cell["caught"] == ["c4", "c5"] and cell["missed"] == ["c3"]  # c3 is no_support: a downgrade is not enough


def test_anonymise_blanks_version_tails_but_keeps_the_answer_figures():
    text = blind_test.anonymise("Gemini 5.5 Pro, Gemini 4.5 Lite, Gemini 5 and Gemini 3.8 Flash; gemini-3.8-flash; "
                                "TikTok carried 1,250 posts and 4.5 times more", [MID, TOP, LOW, GEMINI])
    assert not NAMES.search(text) and "5.5" not in text and "3.8" not in text and "Flash" not in text
    assert not re.search(r"\[model\]\s*[\d.]", text)
    assert "1,250 posts" in text and "4.5 times" in text


def test_a_second_run_the_same_day_does_not_overwrite_the_first(tmp_path):
    first = blind_test.write_results(run(FakeModel()), tmp_path, "2026-09-29")[0]
    later = run(FakeModel(), now=lambda: NOW + timedelta(minutes=7, seconds=5))
    second = blind_test.write_results(later, tmp_path, "2026-09-29")[0]
    assert first.name == "blind_2026-09-29_100000.md" and second.name == "blind_2026-09-29_100705.md"
    assert first.exists() and second.exists()


def test_the_default_candidates_are_the_priced_gemini_models(monkeypatch):
    for name in ("GEMINI_MODEL", "GEMINI_FAST_MODEL", "GEMINI_THINKING_HEADROOM"):
        monkeypatch.delenv(name, raising=False)
    assert blind_test.default_models() == [GEMINI] and blind_test.DEFAULT_GRADER == GEMINI
    monkeypatch.setenv("GEMINI_FAST_MODEL", LOW)
    assert blind_test.default_models() == [GEMINI, LOW]
    monkeypatch.delenv("GEMINI_FAST_MODEL")
    assert GEMINI in blind_test.parser().parse_args(["--replay", "x"]).models
    # Gemini's thinking headroom is reserved on top of the writer's output budget
    assert blind_test.writer_call_usd(GEMINI, records()[0]) > 8000 * price_for(GEMINI)["output"] / 1_000_000
    models = [GEMINI, MID, LOW]
    model = FakeModel(writers={m: good_writer for m in models}, critics={m: sharp_critic for m in models})
    results = run(model, models=models)
    assert GEMINI in {r["model"] for r in results["roles"]["writer"]["rows"]}
    graded = [c for c in model.calls if c["schema"] is blind_test.GRADER_SCHEMA]
    assert graded and not any(NAMES.search(c["system"] + c["user"]) for c in graded)


def thin_writer(user):
    out = good_writer(user)
    out["claims"] = out["claims"][1:]  # one claim that passes the gate
    return out


def silent_on_thin(system, user):
    """Grades every answer but a one-claim answer to the Johannesburg question."""
    graded = fair_grader(system, user)["grades"]
    bodies = dict(ANSWER_BLOCK.findall(user))
    return {"grades": [g for g in graded
                       if not ("Johannesburg" in user and len(json.loads(bodies[g["label"]])["claims"]) < 2)]}


def test_a_model_with_an_ungraded_answer_is_not_chosen():
    thin_once = lambda user: good_writer(user) if "Nairobi" in user else thin_writer(user)  # noqa: E731
    model = FakeModel(writers={MID: good_writer, TOP: good_writer, LOW: thin_once}, grader=silent_on_thin)
    results = run(model, roles=["writer"])
    writer = results["roles"]["writer"]
    rows = {r["model"]: r for r in writer["rows"]}
    assert rows[LOW]["gate_pass_rate"] == 1.0 and rows[LOW]["ungraded"] == 1
    assert rows[LOW]["rubric_score"] == pytest.approx(0.5)  # the ungraded answer scores zero, never dropped
    assert writer["ungraded"] == 1 and writer["chosen"] is None and "ungraded" in writer["reason"]


def test_a_missing_or_out_of_range_grade_scores_zero():
    good = {**{d: 5 for d in blind_test.DIMENSIONS}, "hard_fail": False}
    assert blind_test._grade_score(good) == 1.0
    assert blind_test._grade_score(None) == 0.0
    assert blind_test._grade_score({**good, "usefulness": 7}) == 0.0
    assert blind_test._grade_score({**good, "hard_fail": "no"}) == 0.0


def test_the_shared_family_note_says_the_grader_shares_the_only_family_wired():
    note = run(FakeModel(), roles=["writer"])["grader_note"]
    assert note == "the grader shares the gemini family with a candidate, the only family wired"


def test_a_replayed_forecast_marked_promoted_is_still_cut_without_a_fresh_promotion_read():
    from core.agent.checks import check_answer

    record = records()[0]
    eid = record["evidence"][0]["id"]
    line = "Matatu art is expected to keep rising in Kenya over the next week."
    record["gate_controls"] = {"forecasts": {"f_1": {"forecast_id": "f_1", "statement": line, "evidence_ids": [eid],
                                                     "target": "reach_rising", "horizon": 7, "probability": 0.7,
                                                     "promoted": True, "line": line}}}
    ctx = blind_test.context_for(record)
    draft = {"status": "complete", "as_of": record["as_of"], "short_answer": "",
             "claims": [{"id": "c1", "text": line, "label": "inferred", "kind": "interpretation",
                         "evidence_ids": [eid]}],
             "evidence": [record["evidence"][0]], "so_what": [], "watch_next": [], "gaps": []}

    answer, verdicts = check_answer(draft, ctx, blind_test.ReplayWarehouse(record), window=blind_test._window(record),
                                    markets=record["markets"])

    assert answer["claims"] == []
    assert [v["verdict"] for v in verdicts if v["claim_id"] == "c1" and v["rule"] == "K9"] == ["cut"]
