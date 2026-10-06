"""The scorer turns a promptfoo export into the task 1.16 score file (rubric.md section 4)."""

import copy
import json
from collections import Counter
from pathlib import Path

import pytest
import yaml

from core.eval import score_run

EVAL = Path(__file__).resolve().parents[1]
SAMPLE = EVAL / "tests" / "fixtures" / "promptfoo_results_sample.json"
REAL = EVAL / "tests" / "fixtures" / "promptfoo_real_export.json"
SAMPLE_QUESTIONS = EVAL / "tests" / "fixtures" / "sample_questions.yaml"
REPLAY = {"mode": "replay", "live_credits_spent": 0, "date": "2026-10-01"}
LEGACY = "rests partly on legacy rows (memory only)"


def questions():
    return yaml.safe_load((EVAL / "questions.yaml").read_text(encoding="utf-8"))


def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def only(export, ids):
    out = copy.deepcopy(export)
    out["results"]["results"] = [r for r in out["results"]["results"] if r["vars"]["id"] in ids]
    return out


def ids(export):
    return [r["vars"]["id"] for r in export["results"]["results"]]


def component(entry, metric):
    return next(c for c in entry["gradingResult"]["componentResults"] if c["assertion"]["metric"] == metric)


def test_fixture_scores_as_hand_computed():
    s = score_run.score(sample(), questions(), run_meta=REPLAY, expected=ids(sample()))
    q = s["questions"]
    assert {k: v["outcome"] for k, v in q.items()} == {
        "NOW-01": "pass", "NOW-02": "pass", "NOW-03": "pass", "AUT-01": "pass",
        "AUT-03": "honest_boundary", "CMP-03": "fail", "BRD-01": "fail",
    }
    assert s["failed_questions"] == ["CMP-03", "BRD-01"]
    assert sorted(f["metric"] for f in q["CMP-03"]["failures"]) == [
        "citation_integrity", "grounding", "hard_fail_screen", "honesty", "usefulness",
    ]
    assert sorted(f["metric"] for f in q["BRD-01"]["failures"]) == [
        "claims_have_evidence", "honesty", "so_what", "specificity", "usefulness",
    ]
    assert all(f["reason"] for f in q["CMP-03"]["failures"] + q["BRD-01"]["failures"])
    assert q["NOW-03"]["ratings"] == {"usefulness": 3, "grounding": 4, "specificity": 3, "freshness": 3, "honesty": 3, "so_what": 3}

    fam = s["families"]
    assert fam["now"] == {"scored": 3, "median_usefulness": 4, "result": "pass"}
    assert fam["authenticity"] == {"scored": 2, "median_usefulness": 4, "result": "partial_family"}
    assert fam["compare"]["result"] == "partial_family"
    assert fam["brand"]["result"] == "partial_family"

    g = s["gates"]
    assert g["fabricated_citations"] == {"count": 1, "ids": ["CMP-03"], "pass": False}
    assert g["unsupported_assertions"] == {"count": 1, "ids": ["CMP-03"], "pass": False}
    assert g["false_full_refusals"] == {"count": 1, "ids": ["BRD-01"], "pass": False}
    sc = g["supported_completion"]
    assert (sc["denominator"], sc["completed"], sc["failed_ids"], sc["pass"]) == (6, 4, ["CMP-03", "BRD-01"], False)
    assert sc["rate"] == pytest.approx(4 / 6)

    assert s["thin"] == {"count": 1, "ids": ["AUT-03"], "honest_boundaries": 1, "passed": 1, "failed": 0}

    t = s["trust"]
    assert (t["citation_integrity"]["passed"], t["citation_integrity"]["checked"]) == (6, 7)
    assert t["citation_integrity"]["rate"] == pytest.approx(6 / 7)
    nr = t["number_pinning"]
    assert (nr["numbers"], nr["pinned"], nr["rate"]) == (4, 2, 0.5)

    assert s["run"]["valid"] is True
    assert s["run"]["pass"] is False


def test_thin_honest_boundary_is_a_thin_pass_not_a_failure():
    export = only(sample(), {"NOW-01", "NOW-02", "NOW-03", "AUT-01", "AUT-03"})
    s = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))
    assert s["questions"]["AUT-03"]["outcome"] == "honest_boundary"
    assert s["questions"]["AUT-03"]["pass"] is True
    assert s["failed_questions"] == []
    assert s["thin"]["honest_boundaries"] == 1
    assert s["gates"]["supported_completion"]["denominator"] == 4
    assert "AUT-03" not in s["gates"]["supported_completion"]["failed_ids"]
    assert all(g["pass"] for g in s["gates"].values())
    assert s["run"]["pass"] is True


def test_a_citation_integrity_fail_trips_the_run_gate():
    export = only(sample(), {"NOW-01", "NOW-02", "NOW-03", "AUT-01"})
    assert score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))["run"]["pass"] is True
    c = component(export["results"]["results"][1], "citation_integrity")
    c["pass"], c["score"], c["reason"] = False, 0, "c1: quote not found verbatim in record tt_2"
    s = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))
    assert s["gates"]["fabricated_citations"] == {"count": 1, "ids": ["NOW-02"], "pass": False}
    assert s["failed_questions"] == ["NOW-02"]
    assert s["run"]["pass"] is False


def test_live_credits_in_a_replay_invalidate_the_run():
    export = only(sample(), {"NOW-01", "NOW-02", "NOW-03", "AUT-01"})
    s = score_run.score(export, questions(), run_meta={**REPLAY, "live_credits_spent": 3}, expected=ids(export))
    assert s["run"]["valid"] is False
    assert s["run"]["pass"] is False
    assert any("live credits" in r for r in s["run"]["invalid_reasons"])
    missing = score_run.score(export, questions(), run_meta={"mode": "replay"}, expected=ids(export))
    assert missing["run"]["valid"] is False
    live = score_run.score(export, questions(), run_meta={"mode": "live", "live_credits_spent": 0}, expected=ids(export))
    assert live["run"]["valid"] is False


def test_markdown_names_every_failed_id_before_families_and_gates():
    s = score_run.score(sample(), questions(), run_meta=REPLAY, expected=ids(sample()))
    md = score_run.render_markdown(s)
    failed = md.index("## Failed questions")
    families = md.index("## Families")
    gates = md.index("## Run gates")
    assert failed < families < gates
    for qid in s["failed_questions"]:
        assert qid in md[failed:families]
    assert "citation_integrity" in md[failed:families]
    assert "INVALID" not in md


def test_legacy_note_on_a_90_day_question_and_the_run_summary():
    s = score_run.score(sample(), questions(), run_meta=REPLAY, expected=ids(sample()))
    assert s["questions"]["CMP-03"]["window_days"] == 90
    assert LEGACY in s["questions"]["CMP-03"]["legacy_note"]
    assert s["questions"]["NOW-01"]["legacy_note"] is None
    assert s["questions"]["BRD-01"]["legacy_note"] is None  # exactly 30 days is inside the new corpus
    assert LEGACY in s["run"]["legacy_note"]
    assert "CMP-03" in s["run"]["legacy_note"]
    assert LEGACY in score_run.render_markdown(s)


def test_family_with_three_questions_and_low_median_fails():
    export = only(sample(), {"NOW-01", "NOW-02", "NOW-03"})
    for entry in export["results"]["results"]:
        c = component(entry, "usefulness")
        c["score"] = 0.6
    s = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))
    assert s["families"]["now"] == {"scored": 3, "median_usefulness": 3, "result": "fail"}
    assert s["failed_questions"] == []
    assert s["run"]["pass"] is False


def test_number_pinning_is_null_with_a_reason_when_no_numbers():
    export = only(sample(), {"AUT-01", "AUT-03"})
    s = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))
    assert "number_reproducibility" not in s["trust"]
    nr = s["trust"]["number_pinning"]
    assert nr["rate"] is None
    assert nr["reason"]


def test_cli_writes_json_and_markdown(tmp_path):
    rc = score_run.main([str(SAMPLE), "--questions", str(SAMPLE_QUESTIONS), "--out", str(tmp_path),
                         "--live-credits", "0", "--date", "2026-10-01"])
    assert rc == 0
    data = json.loads((tmp_path / "score-2026-10-01.json").read_text(encoding="utf-8"))
    assert data["run"]["mode"] == "replay"
    assert data["run"]["live_credits_spent"] == 0
    assert data["failed_questions"] == ["CMP-03", "BRD-01"]
    assert "## Failed questions" in (tmp_path / "score-2026-10-01.md").read_text(encoding="utf-8")


def test_a_real_promptfoo_export_scores_one_pass_and_one_citation_fail():
    # Trimmed from a real `promptfoo eval -o` run: NOW-01 twice through a local provider that
    # returns fixtures/answer_complete.json, the second with one quote made non-verbatim.
    real = json.loads(REAL.read_text(encoding="utf-8"))
    entries = real["results"]["results"]
    assert [e["vars"]["variant"] for e in entries] == ["pass", "bad_quote"]
    by_variant = {}
    for e in entries:
        export = copy.deepcopy(real)
        export["results"]["results"] = [e]
        by_variant[e["vars"]["variant"]] = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))

    good = by_variant["pass"]["questions"]["NOW-01"]
    assert good["outcome"] == "pass"
    assert good["failures"] == []
    assert good["status"] == "complete"
    assert good["ratings"] == {"usefulness": 4, "grounding": 4, "specificity": 4, "freshness": 4, "honesty": 4, "so_what": 4}
    assert good["citation_integrity"] is True
    assert by_variant["pass"]["trust"]["number_pinning"]["pinned"] == 1
    assert by_variant["pass"]["run"]["pass"] is True

    bad = by_variant["bad_quote"]
    q = bad["questions"]["NOW-01"]
    assert q["outcome"] == "fail"
    assert [f["metric"] for f in q["failures"]] == ["citation_integrity"]
    assert "quote not found verbatim in record tt_7431" in q["failures"][0]["reason"]
    assert bad["gates"]["fabricated_citations"] == {"count": 1, "ids": ["NOW-01"], "pass": False}
    assert bad["run"]["pass"] is False


def test_a_provider_error_in_the_real_shape_is_transport():
    # promptfoo 0.123.1 on a provider error: failureReason 2, response.error, gradingResult null.
    real = json.loads(REAL.read_text(encoding="utf-8"))
    e = real["results"]["results"][0]
    e.update(failureReason=2, error="HTTP 503: upstream unavailable", response={"error": "HTTP 503: upstream unavailable"}, gradingResult=None)
    real["results"]["results"] = [e]
    q = score_run.score(real, questions(), run_meta=REPLAY, expected=ids(real))["questions"]["NOW-01"]
    assert q["outcome"] == "fail"
    assert q["failures"][0] == {"metric": "transport", "reason": "HTTP 503: upstream unavailable"}
    assert q["supported_complete"] is False


def test_friday_subset_is_ten_verbatim_questions_one_per_family():
    full = {q["vars"]["id"]: q for q in questions()}
    friday = yaml.safe_load((EVAL / "friday.yaml").read_text(encoding="utf-8"))
    assert len(friday) == 10
    for entry in friday:
        assert entry == full[entry["vars"]["id"]]
    assert Counter(e["vars"]["family"] for e in friday) == Counter({f: 1 for f in {q["vars"]["family"] for q in full.values()}})
    assert all(e["vars"]["expected_answerability"] == "answerable" for e in friday)


def only_now(export):
    return only(export, {"NOW-01", "NOW-02", "NOW-03", "AUT-01"})


@pytest.mark.parametrize("grade", [0.78, 0.8])
def test_a_grader_fail_on_grounding_fails_whatever_the_score_rounds_to(grade):
    export = only_now(sample())
    assert score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))["run"]["pass"] is True
    c = component(export["results"]["results"][0], "grounding")
    c["pass"], c["score"], c["reason"] = False, grade, "c2 overstates its record"
    s = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))
    q = s["questions"]["NOW-01"]
    assert q["outcome"] == "fail"
    assert [f["metric"] for f in q["failures"]] == ["grounding"]
    assert "c2 overstates its record" in q["failures"][0]["reason"]
    assert q["ratings"]["grounding"] == (3 if grade == 0.78 else 4)
    assert s["failed_questions"] == ["NOW-01"]
    assert s["run"]["pass"] is False


def test_a_score_between_anchors_takes_the_lower_rating():
    export = only_now(sample())
    c = component(export["results"]["results"][0], "grounding")
    c["score"] = 0.78  # pass left True: the rating alone must fail it
    q = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))["questions"]["NOW-01"]
    assert q["ratings"]["grounding"] == 3
    assert q["outcome"] == "fail"


def test_friday_ids_missing_from_the_export_fail_the_run_by_name(tmp_path):
    header = (EVAL / "friday.yaml").read_text(encoding="utf-8").split("\n- ")[0]
    assert "--questions core/eval/friday.yaml" in header
    friday = yaml.safe_load((EVAL / "friday.yaml").read_text(encoding="utf-8"))
    missing = [e["vars"]["id"] for e in friday if e["vars"]["id"] not in {"NOW-01", "AUT-01"}]
    assert len(missing) == 8
    export = tmp_path / "results.json"
    export.write_text(json.dumps(only(sample(), {"NOW-01", "AUT-01"})), encoding="utf-8")
    rc = score_run.main([str(export), "--questions", str(EVAL / "friday.yaml"), "--out", str(tmp_path),
                         "--live-credits", "0", "--date", "2026-10-01"])
    assert rc == 0
    s = json.loads((tmp_path / "score-2026-10-01.json").read_text(encoding="utf-8"))
    assert s["failed_questions"] == missing
    for qid in missing:
        assert s["questions"][qid]["outcome"] == "fail"
        assert [f["metric"] for f in s["questions"][qid]["failures"]] == ["missing"]
    sc = s["gates"]["supported_completion"]
    assert (sc["denominator"], sc["completed"], sc["failed_ids"], sc["pass"]) == (10, 2, missing, False)
    assert s["run"]["questions"] == 10
    assert s["run"]["pass"] is False
    md = (tmp_path / "score-2026-10-01.md").read_text(encoding="utf-8")
    failed_section = md[md.index("## Failed questions"):md.index("## Families")]
    for qid in missing:
        assert qid in failed_section


def test_every_duplicate_entry_is_kept_and_any_failure_fails_the_id():
    real = json.loads(REAL.read_text(encoding="utf-8"))
    real["results"]["results"].reverse()
    assert [e["vars"]["variant"] for e in real["results"]["results"]] == ["bad_quote", "pass"]
    s = score_run.score(real, questions(), run_meta=REPLAY, expected=ids(real))
    q = s["questions"]["NOW-01"]
    assert q["outcome"] == "fail"
    assert [f["metric"] for f in q["failures"]] == ["citation_integrity"]
    assert "quote not found verbatim in record tt_7431" in q["failures"][0]["reason"]
    assert q["entries"] == 2
    assert s["failed_questions"] == ["NOW-01"]
    assert s["gates"]["fabricated_citations"] == {"count": 1, "ids": ["NOW-01"], "pass": False}
    assert s["gates"]["supported_completion"]["failed_ids"] == ["NOW-01"]
    assert s["run"]["pass"] is False


def test_both_duplicate_failures_are_recorded():
    real = json.loads(REAL.read_text(encoding="utf-8"))
    bad = copy.deepcopy(real["results"]["results"][1])
    real["results"]["results"] = [bad, copy.deepcopy(bad)]
    q = score_run.score(real, questions(), run_meta=REPLAY, expected=ids(real))["questions"]["NOW-01"]
    assert [f["metric"] for f in q["failures"]] == ["citation_integrity", "citation_integrity"]
    assert [f["entry"] for f in q["failures"]] == [1, 2]


@pytest.mark.parametrize("status", ["complete", "partial", "insufficient_evidence"])
def test_a_guardrail_answer_with_no_claims_is_a_false_full_refusal(status):
    export = only(sample(), {"NOW-01"})
    e = export["results"]["results"][0]
    e["vars"] = {**e["vars"], "id": "RISE-01", "family": "rising", "expected_answerability": "guardrail"}
    answer = json.loads(e["response"]["output"])
    answer.update(status=status, claims=[])
    e["response"]["output"] = json.dumps(answer)
    s = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))
    assert s["questions"]["RISE-01"]["answerability"] == "guardrail"
    assert s["gates"]["false_full_refusals"] == {"count": 1, "ids": ["RISE-01"], "pass": False}
    assert s["run"]["pass"] is False


def test_markdown_calls_the_number_check_pinning_and_the_legacy_note_is_spaced():
    s = score_run.score(sample(), questions(), run_meta=REPLAY, expected=ids(sample()))
    assert f"{LEGACY}: CMP-03" in s["run"]["legacy_note"]
    md = score_run.render_markdown(s)
    assert "Number pinning (no re-run)" in md
    assert "Number reproducibility" not in md


def test_the_cli_refuses_to_score_without_a_questions_file(tmp_path):
    with pytest.raises(SystemExit):
        score_run.main([str(SAMPLE), "--out", str(tmp_path), "--live-credits", "0", "--date", "2026-10-01"])
    assert not list(tmp_path.iterdir())


def test_sample_questions_are_the_sample_export_ids_verbatim():
    full = {q["vars"]["id"]: q for q in questions()}
    picked = yaml.safe_load(SAMPLE_QUESTIONS.read_text(encoding="utf-8"))
    assert [e["vars"]["id"] for e in picked] == [r["vars"]["id"] for r in sample()["results"]["results"]]
    for entry in picked:
        assert entry == full[entry["vars"]["id"]]


def test_score_requires_the_expected_ids():
    with pytest.raises(TypeError):
        score_run.score(sample(), questions(), run_meta=REPLAY)


@pytest.mark.parametrize("metric, early, rating", [
    ("grounding", {"pass": False, "score": 1.0, "reason": "c1 unsupported by tt_2"}, 5),
    ("grounding", {"pass": True, "score": 0.4, "reason": "c1 unsupported by tt_2"}, 2),
    ("citation_integrity", {"pass": False, "score": 0, "reason": "c1 unsupported by tt_2"}, None),
])
def test_a_later_passing_duplicate_metric_does_not_hide_an_earlier_failing_one(metric, early, rating):
    export = only_now(sample())
    comps = export["results"]["results"][0]["gradingResult"]["componentResults"]
    later = component(export["results"]["results"][0], metric)
    comps.insert(0, {**copy.deepcopy(later), **early})
    s = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))
    q = s["questions"]["NOW-01"]
    assert q["outcome"] == "fail"
    assert [f["metric"] for f in q["failures"]] == [metric]
    assert "c1 unsupported by tt_2" in q["failures"][0]["reason"]
    if rating is not None:
        assert q["ratings"][metric] == rating
    assert s["run"]["pass"] is False


def test_the_lowest_duplicate_rating_is_kept():
    export = only_now(sample())
    comps = export["results"]["results"][0]["gradingResult"]["componentResults"]
    later = component(export["results"]["results"][0], "usefulness")
    comps.insert(0, {**copy.deepcopy(later), "score": 0.8})
    q = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))["questions"]["NOW-01"]
    assert later["score"] == 1.0
    assert q["ratings"]["usefulness"] == 4
    assert q["outcome"] == "pass"


@pytest.mark.parametrize("where", ["success", "gradingResult"])
def test_a_promptfoo_fail_no_metric_explains_is_recorded(where):
    export = only_now(sample())
    e = export["results"]["results"][0]
    if where == "success":
        e.update(success=False, failureReason=1)
    else:
        e["gradingResult"].update({"pass": False, "reason": "is-json: output is not valid JSON"})
    s = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))
    q = s["questions"]["NOW-01"]
    assert q["outcome"] == "fail"
    assert [f["metric"] for f in q["failures"]] == ["promptfoo_verdict"]
    assert q["failures"][0]["reason"]
    assert s["failed_questions"] == ["NOW-01"]
    assert s["run"]["pass"] is False


def test_a_promptfoo_fail_a_metric_explains_adds_no_verdict_failure():
    real = json.loads(REAL.read_text(encoding="utf-8"))
    real["results"]["results"] = real["results"]["results"][1:]
    assert real["results"]["results"][0]["success"] is False
    q = score_run.score(real, questions(), run_meta=REPLAY, expected=ids(real))["questions"]["NOW-01"]
    assert [f["metric"] for f in q["failures"]] == ["citation_integrity"]


@pytest.mark.parametrize("bad", [6, 3, -0.1, float("nan"), float("inf"), "high"])
def test_a_grader_score_outside_zero_to_one_is_a_failure(bad):
    export = only_now(sample())
    c = component(export["results"]["results"][0], "grounding")
    c["score"] = bad  # pass left True: the grader said pass, the score is still unusable
    s = score_run.score(export, questions(), run_meta=REPLAY, expected=ids(export))
    q = s["questions"]["NOW-01"]
    assert q["outcome"] == "fail"
    assert [f["metric"] for f in q["failures"]] == ["grounding"]
    assert q["failures"][0]["reason"].startswith("grader score out of range")
    assert "grounding" not in q["ratings"]
    assert s["run"]["pass"] is False
