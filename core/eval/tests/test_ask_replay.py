"""The ten benchmark questions of ASK-POWER-PLAN section 3, replayed offline through run_ask.

Every expectation in this file was written from the design of its fixture (which defect each question is seeded with,
which rule must catch it) and from the plan's figures, and none is read from a fixture or from the replay's own
output. If a pin and the machinery disagree, the machinery is the thing to look at first."""

import csv
import io
import json
from datetime import date
from pathlib import Path

import pytest

from core.agent import ask
from core.eval import ask_replay, quality_score, review
from core.eval import ask_replay_score as score

FIXTURES = Path(ask_replay.__file__).parent / "ask_replay_fixtures"

# ASK-POWER-PLAN section 3: three markets, four why questions, the humour question last as the regression.
THE_TEN = [("WHY-01", "ZA"), ("WHY-02", "NG"), ("WHY-03", "KE"), ("NOW-01", "ZA"), ("NOW-03", "KE"),
           ("RISE-02", "NG"), ("CRE-01", "NG"), ("BRD-03", "KE"), ("AUT-01", "NG"), ("HUMOUR-ZA", "ZA")]

# status, kept claim ids, cut claims with the rules that cut them (a code rule that cuts also writes a K10 row),
# kept claims whose label was lowered, and what the privacy projection withholds once the creators in hidden_after
# are hidden (posts, claims).
EXPECT = {
    "WHY-01": dict(status="partial", kept=["c1", "c2", "c3", "c4"], cut={"c5": ["K4"], "c6": ["K6", "K10"]},
                   lowered=[], withheld=(1, 1), suppressed=0),
    "WHY-02": dict(status="partial", kept=["c1", "c2", "c3", "c6"], cut={"c4": ["K2", "K10"], "c5": ["K1", "K10"]},
                   lowered=["c6"], withheld=(0, 0), suppressed=1),
    # c1 and c3 cite Sheng or Swahili posts, and no cleared native-speaker review exists for Kenya: the tone cap lowers
    # a corroborated label to single_source (TRUST section 6). c6 is narrowed by the K4 rewrite and keeps its label.
    "WHY-03": dict(status="partial", kept=["c1", "c2", "c3", "c5", "c6"], cut={"c4": ["K9", "K10"]},
                   lowered=["c1", "c3"], withheld=(1, 1), suppressed=0),
    "NOW-01": dict(status="complete", kept=["c1", "c2", "c3", "c4", "c5"], cut={}, lowered=[], withheld=(0, 0),
                   suppressed=0),
    "NOW-03": dict(status="partial", kept=["c1", "c2", "c4"], cut={"c3": ["K1", "K10"]}, lowered=[], withheld=(0, 0),
                   suppressed=1),
    "RISE-02": dict(status="partial", kept=["c1", "c2", "c3"], cut={"c4": ["K4"], "c5": ["K3", "K10"]},
                    lowered=[], withheld=(0, 0), suppressed=0),
    "CRE-01": dict(status="partial", kept=["c1", "c2", "c3"], cut={"c4": ["K2", "K10"], "c5": ["K6", "K10"]},
                   lowered=[], withheld=(1, 1), suppressed=0),
    "BRD-03": dict(status="partial", kept=["c1", "c2", "c3"], cut={"c4": ["K3", "K10"]}, lowered=[],
                   withheld=(0, 0), suppressed=0),
    "AUT-01": dict(status="partial", kept=["c1", "c2", "c3", "c5"], cut={"c4": ["K4"]}, lowered=["c5"],
                   withheld=(1, 1), suppressed=2),
    "HUMOUR-ZA": dict(status="complete", kept=["c1", "c2", "c3", "c4", "c5", "c6", "c7"], cut={}, lowered=[],
                      withheld=(0, 0), suppressed=0),
}

# Measure results the plan's thresholds give each fixture: which of the five counted measures pass. The depth of the
# store and the spread of the citations are the fixture's, so these pins fix the fixtures' shapes: two strong
# answers, a thin one with three live platforms, a thin one that searched two, and the humour regression, which must
# fail every depth measure it fails today.
MEASURES = {
    "WHY-01": dict(on_topic_posts="pass", cited_spread="pass", live_platforms="not_applicable", comment_claims="pass"),
    "WHY-02": dict(on_topic_posts="fail", cited_spread="pass", live_platforms="pass", comment_claims="fail"),
    "WHY-03": dict(on_topic_posts="fail", cited_spread="pass", live_platforms="pass", comment_claims="pass"),
    "NOW-01": dict(on_topic_posts="pass", cited_spread="pass", live_platforms="not_applicable",
                   comment_claims="not_applicable"),
    "NOW-03": dict(on_topic_posts="fail", cited_spread="pass", live_platforms="fail", comment_claims="not_applicable"),
    "RISE-02": dict(on_topic_posts="fail", cited_spread="pass", live_platforms="fail", comment_claims="not_applicable"),
    "CRE-01": dict(on_topic_posts="fail", cited_spread="pass", live_platforms="fail", comment_claims="not_applicable"),
    "BRD-03": dict(on_topic_posts="pass", cited_spread="pass", live_platforms="not_applicable",
                   comment_claims="not_applicable"),
    "AUT-01": dict(on_topic_posts="fail", cited_spread="pass", live_platforms="fail", comment_claims="not_applicable"),
    "HUMOUR-ZA": dict(on_topic_posts="fail", cited_spread="fail", live_platforms="fail",
                      comment_claims="not_applicable"),
}


@pytest.fixture(scope="module")
def replayed():
    results, view = ask_replay.replay()
    return {r["id"]: r for r in results}, view


def test_the_ten_are_the_plan_ten_in_the_plan_order():
    assert ask_replay.ORDER == [q for q, _ in THE_TEN]
    for qid, market in THE_TEN:
        assert ask_replay.load(qid)["id"] == qid


def test_nine_questions_are_read_from_questions_yaml_and_the_humour_one_is_the_diagnosis_question(replayed):
    results, _ = replayed
    for qid, market in THE_TEN:
        fixture = json.loads((FIXTURES / f"{qid}.json").read_text(encoding="utf-8"))
        assert ("question" in fixture) == (qid == "HUMOUR-ZA"), qid
        assert results[qid]["market"] == market
    assert results["HUMOUR-ZA"]["vars"]["question"] == "#humor in South Africa this week"
    assert results["WHY-03"]["vars"]["question"].startswith("AFCON 2027 qualifiers kicked off on 21 September")


@pytest.mark.parametrize("qid", [q for q, _ in THE_TEN])
def test_each_question_ends_as_pinned(qid, replayed):
    r = replayed[0][qid]
    want = EXPECT[qid]
    assert r["error"] is None, r["error"]
    assert r["answer"]["status"] == want["status"]
    assert r["outcomes"]["kept"] == want["kept"]
    assert {c["claim_id"]: c["rules"] for c in r["outcomes"]["cut"]} == want["cut"]
    assert r["outcomes"]["downgraded"] == want["lowered"]
    assert all(c["reason"] for c in r["outcomes"]["cut"])  # a cut claim always says why
    assert (r["privacy"]["withheld"]["posts"], r["privacy"]["withheld"]["claims"]) == want["withheld"]
    assert r["privacy"]["suppressed_at_read"] == want["suppressed"]


@pytest.mark.parametrize("qid", [q for q, _ in THE_TEN])
def test_each_question_scores_as_pinned(qid, replayed):
    got = {m["name"]: m["result"] for m in replayed[0][qid]["measures"]}
    for name, result in MEASURES[qid].items():
        assert got[name] == result, (qid, name)
    assert got["within_hold"] == "pass" and got["seconds"] == "live_only"


def test_no_question_breaches(replayed):
    results, _ = replayed
    assert {q: r["breaches"] for q, r in results.items() if r["breaches"]} == {}


def test_a_partial_claim_is_narrowed_to_what_its_post_supports_and_kept(replayed):
    claims = {c["id"]: c for c in replayed[0]["WHY-03"]["answer"]["claims"]}
    assert claims["c6"]["text"] == "One X post complains that qualifier ticket prices are too high for a normal fan."
    assert "Most of the cynicism is about ticket prices" not in json.dumps(replayed[0]["WHY-03"]["answer"])


def test_the_hold_is_the_figure_albert_restates_and_every_ask_stays_inside_it(replayed):
    assert ask.hold_usd("T1", ask.MODEL) == pytest.approx(4.92, abs=0.005)
    assert ask.hold_usd("T2", ask.MODEL) == pytest.approx(11.33, abs=0.005)
    for qid, r in replayed[0].items():
        assert r["tier"] == "T1" and r["run"]["tier"] == "T1"
        assert r["hold_usd"] == pytest.approx(4.916, abs=0.001)
        assert 0 < r["run"]["model_usd"] < r["hold_usd"], qid


def test_a_hidden_creator_is_gone_from_the_projection_and_never_in_the_live_answer(replayed):
    for qid, r in replayed[0].items():
        assert r["visible_before"] == [] and r["visible_after"] == [], qid
    why1 = replayed[0]["WHY-01"]
    assert why1["privacy"]["state"] == "applied" and why1["privacy"]["hidden_after"] == 1
    assert "@bucketbrigade_za" in json.dumps(why1["answer"]["evidence"])  # shown before the hide
    assert "@bucketbrigade_za" not in json.dumps(why1["projected"])  # gone after it
    assert replayed[0]["NOW-01"]["privacy"]["state"] == "none"


def test_the_humour_regression_keeps_the_failing_shape_it_has_today(replayed):
    r = replayed[0]["HUMOUR-ZA"]
    sums = score.cited(r["answer"])
    assert (sums["posts"], sums["creators"], sums["platforms"]) == (3, 3, 1)
    assert len(r["answer"]["claims"]) == 7  # seven claims on three posts, as the export read
    assert len(r["on_topic_ids"]) == 3


def test_score_run_grades_what_code_can_and_names_the_rest(replayed):
    _, view = replayed
    assert view["trust"]["citation_integrity"] == {"passed": 10, "checked": 10}
    assert view["fabricated_citations"] == [] and view["false_full_refusals"] == []
    assert view["trust"]["number_pinning"]["pinned"] == view["trust"]["number_pinning"]["numbers"] > 0
    assert view["ungraded"] == ["freshness", "grounding", "hard_fail_screen", "honesty", "so_what", "specificity",
                                "usefulness"]


def test_the_replay_is_repeatable(replayed):
    results, view = replayed
    first = ask_replay.summary(list(results.values()), view)
    again, again_view = ask_replay.replay()
    assert ask_replay.summary(again, again_view) == first


def test_main_writes_the_report_and_exits_clean(tmp_path, capsys):
    assert ask_replay.main([str(tmp_path)]) == 0
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["answers", "ask-replay.json", "ask-replay.md", "review-sheet.csv"]
    assert sorted(p.name for p in (tmp_path / "answers").iterdir()) == sorted(f"{q}.json" for q, _ in THE_TEN)
    body = json.loads((tmp_path / "ask-replay.json").read_text(encoding="utf-8"))
    assert [q["id"] for q in body["questions"]] == [q for q, _ in THE_TEN]
    assert body["breached"] == [] and body["version"] == 1
    md = (tmp_path / "ask-replay.md").read_text(encoding="utf-8")
    assert "Breaches: none." in md and "Only the live evaluation can give" in md
    assert "CLEAN" in capsys.readouterr().out
    # a replay folder is never mistaken for a weekly score file
    assert score.score_file_names(tmp_path) == []
    assert quality_score.latest_run(tmp_path, date(2026, 10, 5)) is None


def test_the_review_sheet_has_the_weekly_columns_and_refuses_to_be_read_unfilled(tmp_path):
    results, _ = ask_replay.replay(["NOW-01", "WHY-01"])
    sheet = ask_replay.review_sheet(results)
    rows = list(csv.DictReader(io.StringIO(sheet)))
    assert list(rows[0]) == review.SHEET_COLUMNS
    assert {r["market"] for r in rows} == {"ZA"} and all(r["id"].split(":")[0] in {"NOW-01", "WHY-01"} for r in rows)
    with pytest.raises(ValueError, match="verdict is blank"):
        review.read_sheet(sheet)


def test_a_loosened_gate_cannot_pass_the_replay(monkeypatch):
    """With the trust checks replaced by a pass-through, the claim with an age lens survives the gate and the
    independent promptfoo assert catches it: the replay exits 1 for it."""
    def pass_everything(draft, ctx, warehouse, *, window, markets):
        rows = [{"claim_id": c["id"], "rule": "K10", "verdict": "pass", "reason": "", "checker": "code"}
                for c in draft["claims"]]
        return draft, rows

    monkeypatch.setattr(ask, "check_answer", pass_everything)
    results, _ = ask_replay.replay(["WHY-01"])
    kinds = [b["kind"] for b in results[0]["breaches"]]
    assert "gate_leak" in kinds


def test_a_fixture_key_that_means_nothing_is_refused(tmp_path, monkeypatch):
    bad = json.loads((FIXTURES / "NOW-01.json").read_text(encoding="utf-8"))
    bad["hiden_after"] = []
    (tmp_path / "NOW-01.json").write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setattr(ask_replay, "FIXTURES", tmp_path)
    with pytest.raises(ValueError, match="unknown keys"):
        ask_replay.load("NOW-01")


def test_a_call_the_fixture_did_not_record_fails_instead_of_inventing_a_result():
    client = ask_replay.ReplayClient({"tiktok/search": {"items": []}})
    with pytest.raises(KeyError, match="recorded no result for twitter/search"):
        client.quote("twitter/search", {})
    model = ask_replay.ReplayModel({"writer": {"claims": []}}, {})
    with pytest.raises(AssertionError, match="no reply"):
        model.complete_json(system="", user="", schema={"type": "object"}, model="m", max_tokens=1)


def healthy():
    """A replayed question with nothing wrong, for the breach tests: every field breaches() reads, at a healthy value."""
    ok = {"pass": True, "reason": ""}
    return {"error": None, "breaches": [], "contract": [], "meta_problem": None, "visible_before": [],
            "numbers": {"numbers": 0, "recomputed": 0, "problems": []},
            "visible_after": [], "hold_usd": 4.916, "run": {"model_usd": 0.3},
            "checks": {m: dict(ok) for m in score.LEAK_CHECKS}}


def test_a_healthy_result_has_no_breach():
    assert ask_replay.breaches(healthy()) == []


@pytest.mark.parametrize("field,value,kind", [
    ("contract", ["claims.0.label: not one of the labels"], "contract"),
    ("meta_problem", "digest", "answer_meta"),
    ("visible_before", ["@someone"], "privacy"),
    ("visible_after", ["@someone"], "privacy"),
])
def test_each_fault_is_named_by_its_kind(field, value, kind):
    r = healthy()
    r[field] = value
    assert [b["kind"] for b in ask_replay.breaches(r)] == [kind]


def test_a_number_whose_hash_is_not_its_rows_hash_is_a_breach():
    r = healthy()
    r["numbers"] = {"numbers": 1, "recomputed": 0, "problems": ["c1: the hash of q_1 is not the hash of its rows"]}
    got = ask_replay.breaches(r)
    assert [b["kind"] for b in got] == ["number_hash"] and "q_1" in got[0]["detail"]


def test_spend_over_the_hold_is_a_breach_and_spend_at_the_hold_is_not():
    r = healthy()
    r["run"]["model_usd"] = r["hold_usd"]
    assert ask_replay.breaches(r) == []
    r["run"]["model_usd"] = r["hold_usd"] + 0.0001
    assert [b["kind"] for b in ask_replay.breaches(r)] == ["over_hold"]


def test_a_failed_independent_check_is_a_gate_leak_and_an_error_stops_the_scoring():
    r = healthy()
    r["checks"]["citation_integrity"] = {"pass": False, "reason": "quote not found verbatim in record p1"}
    got = ask_replay.breaches(r)
    assert [b["kind"] for b in got] == ["gate_leak"] and "citation_integrity" in got[0]["detail"]
    err = {"error": "RuntimeError: boom", "breaches": [{"kind": "error", "detail": "RuntimeError: boom"}]}
    assert ask_replay.breaches(err) == [{"kind": "error", "detail": "RuntimeError: boom"}]


def test_a_tampered_summary_state_fails_the_replay(monkeypatch):
    real = ask.run_ask

    def tampered(*args, **kwargs):
        out = real(*args, **kwargs)
        out["answer_meta"] = {**out["answer_meta"], "digest": "sha256:" + "0" * 64}
        return out

    monkeypatch.setattr(ask, "run_ask", tampered)
    results, _ = ask_replay.replay(["NOW-01"])
    assert [b["kind"] for b in results[0]["breaches"]] == ["answer_meta"]
    assert "digest" in results[0]["breaches"][0]["detail"]


def test_a_projection_that_hides_nobody_fails_the_replay(monkeypatch):
    monkeypatch.setattr(ask_replay.privacy, "project_record", lambda record, store, hidden=None, **kw: record)
    results, _ = ask_replay.replay(["WHY-01"])
    assert [b["kind"] for b in results[0]["breaches"]] == ["privacy"]
    assert "@bucketbrigade_za" in results[0]["breaches"][0]["detail"]


def test_main_replays_only_the_ids_it_is_given(tmp_path, capsys):
    assert ask_replay.main([str(tmp_path), "NOW-01", "HUMOUR-ZA"]) == 0
    assert sorted(p.name for p in (tmp_path / "answers").iterdir()) == ["HUMOUR-ZA.json", "NOW-01.json"]
    assert [q["id"] for q in json.loads((tmp_path / "ask-replay.json").read_text(encoding="utf-8"))["questions"]] == [
        "NOW-01", "HUMOUR-ZA"]
    capsys.readouterr()


def test_main_exits_one_when_a_question_breaches(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ask_replay.privacy, "project_record", lambda record, store, hidden=None, **kw: record)
    assert ask_replay.main([str(tmp_path), "WHY-01"]) == 1
    assert "BREACH" in capsys.readouterr().out


def test_the_markdown_names_the_promptfoo_asserts_and_their_reasons(replayed):
    results, view = replayed
    md = ask_replay.render_markdown(ask_replay.summary(list(results.values()), view))
    humour = md.split("## HUMOUR-ZA")[1]
    assert "- Promptfoo asserts: claims_have_evidence pass, citation_integrity pass, evidence_floor FAIL" in humour
    assert "3 in-window posts cited, minimum 8" in humour  # the floor's own reason, which says why
    now = md.split("## NOW-01")[1].split("## ")[0]
    assert "- Promptfoo asserts: claims_have_evidence pass, citation_integrity pass, evidence_floor pass" in now


def test_every_kept_number_has_a_hash_recomputed_from_its_rows(replayed):
    results, _ = replayed
    total = sum(r["numbers"]["numbers"] for r in results.values())
    assert total > 0
    assert sum(r["numbers"]["recomputed"] for r in results.values()) == total
    assert all(r["numbers"]["problems"] == [] for r in results.values())


def test_a_forged_but_well_formed_hash_passes_score_run_and_fails_the_replay(monkeypatch):
    real = ask.run_ask

    def forged(*args, **kwargs):
        out = real(*args, **kwargs)
        out["answer"]["claims"][0]["numbers"][0]["result_hash"] = "sha256:" + "c" * 64
        return out

    monkeypatch.setattr(ask, "run_ask", forged)
    results, view = ask_replay.replay(["NOW-01"])
    assert view["trust"]["number_pinning"]["pinned"] == view["trust"]["number_pinning"]["numbers"]  # shape only
    assert [b["kind"] for b in results[0]["breaches"]] == ["number_hash"]
