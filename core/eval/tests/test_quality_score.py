"""The weekly quality score (FEATURES.md 7, TRUST.md section 7): one number from 0 to 100 per week and market,
built from the trust metrics 42 already records, each part a Figure with its query. A part with too little data
is insufficient, never zero."""

import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from core.eval import quality_score, review
from core.eval.tests.test_forecast_score import good_cohort, row

WEEK = date(2026, 9, 21)
ISO_WEEK = "2026-W39"
NOW = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def score_file(tmp, day, *, questions=10, failed=(), thin=0, honest=0, false_refusals=0, valid=True, ids=None,
               with_ids=True):
    body = {
        "run": {"date": day, "valid": valid, "questions": questions, "mode": "replay", "live_credits_spent": 0},
        "failed_questions": list(failed),
        "thin": {"count": thin, "honest_boundaries": honest},
        "gates": {"false_full_refusals": {"count": false_refusals, "ids": [], "pass": not false_refusals}},
    }
    if with_ids:
        body["questions"] = {qid: {} for qid in (ids or [f"Q{i:02d}" for i in range(questions)])}
    path = Path(tmp) / f"score-{day}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def claim_labels(week, n, *, errors=0, market_cycle=("ZA", "NG", "KE")):
    out = []
    for i in range(n):
        out.append({"week": week, "kind": "claim", "id": f"{week}-c{i}", "market": market_cycle[i % len(market_cycle)],
                    "stratum": "random", "double": False, "non_english": False, "language": "",
                    "verdict": "contradicted" if i < errors else "supported", "useful": "", "reviewer": "ana",
                    "reason": ""})
    return out


def feedback(labels):
    return [{**review.to_feedback(lab), "at": NOW} for lab in labels]


def scorecard(market, precision, n, *, run_id="learn-1", ttd=4.0):
    return {"week_start": WEEK, "week_end": WEEK + timedelta(days=6), "market": market, "run_id": run_id,
            "rule_version": "scorecard-1",
            "precision": {"value": precision, "unit": "share", "query_id": "sc", "n": n},
            "time_to_detect": {"value": ttd, "unit": "days", "query_id": "sc", "n": 12}}


def by_market(out):
    return {r["market"]: r for r in out["rows"]}


# The SQL

def test_weekly_quality_table_is_append_only_in_the_agent_dataset():
    text = (SQL_DIR / "weekly_quality_table.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.weekly_quality`" in text


# Parts from the promptfoo score files

def test_eval_pass_rate_reads_the_latest_valid_score_file_in_the_week(tmp_path):
    score_file(tmp_path, "2026-09-22", failed=["A", "B", "C"])
    score_file(tmp_path, "2026-09-25", failed=["A"])
    score_file(tmp_path, "2026-09-26", failed=[], valid=False)       # invalid, ignored
    score_file(tmp_path, "2026-09-29", failed=[])                    # next week, ignored
    part = quality_score.eval_pass_rate(tmp_path, WEEK)
    assert part["value"] == pytest.approx(0.9) and part["n"] == 10
    assert part["query_id"] == "file:score-2026-09-25.json#/run,/failed_questions"
    path = tmp_path / "score-2026-09-25.json"
    assert part["result_hash"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert part["row_count"] == 10
    assert part["points"] == pytest.approx(90.0)
    assert part["insufficient"] is False


def test_eval_pass_rate_without_a_run_or_under_ten_questions_is_insufficient(tmp_path):
    none = quality_score.eval_pass_rate(tmp_path, WEEK)
    assert none["insufficient"] is True and none["value"] is None and none["points"] is None and none["reason"]
    score_file(tmp_path, "2026-09-25", questions=6, failed=[])
    small = quality_score.eval_pass_rate(tmp_path, WEEK)
    assert small["insufficient"] is True and small["n"] == 6 and small["value"] is None


def test_honest_gaps_counts_honest_boundaries_against_thin_questions_and_false_refusals(tmp_path):
    score_file(tmp_path, "2026-09-25", questions=30, thin=6, honest=5, false_refusals=2)
    part = quality_score.honest_gaps(tmp_path, WEEK)
    assert part["value"] == pytest.approx(5 / 8) and part["n"] == 8
    assert part["points"] == pytest.approx(62.5)


def test_honest_gaps_with_no_thin_question_is_insufficient(tmp_path):
    score_file(tmp_path, "2026-09-25", questions=10, thin=0, honest=0)
    part = quality_score.honest_gaps(tmp_path, WEEK)
    assert part["insufficient"] is True and part["value"] is None


# Parts from the warehouse

def test_claim_support_uses_four_weeks_of_random_review_labels(warehouse, tmp_path):
    wh = warehouse()
    wh.insert("feedback", feedback(claim_labels("2026-W36", 30) + claim_labels("2026-W39", 30, errors=3)
                                   + claim_labels("2026-W35", 30, errors=30)))   # five weeks back, not read
    wh.insert("feedback", [{"who": "app", "what": "tapped Real", "reason": "", "at": NOW}])  # a tap, skipped
    out = quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    part = by_market(out)["ALL"]["parts"]["claim_support"]
    assert part["value"] == pytest.approx(3 / 60) and part["n"] == 60
    assert part["points"] == pytest.approx(95.0)
    assert part["query_id"] == "core/eval/sql/review_feedback.sql"
    assert part["row_count"] == 90 and part["result_hash"].startswith("sha256:")
    za = by_market(out)["ZA"]["parts"]["claim_support"]
    assert za["n"] == 20 and za["insufficient"] is True


def test_claim_support_with_no_labels_this_week_is_insufficient(warehouse, tmp_path):
    wh = warehouse()
    wh.insert("feedback", feedback(claim_labels("2026-W38", 60)))
    out = quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    part = by_market(out)["ALL"]["parts"]["claim_support"]
    assert part["insufficient"] is True and part["value"] is None and "2026-W39" in part["reason"]


def test_forecast_skill_is_pooled_per_market_and_needs_200_resolved(warehouse, tmp_path):
    wh = warehouse()
    wh.insert("forecasts", good_cohort() + good_cohort(market="NG", rule="ask_v1")[:40])
    out = quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    rows = by_market(out)
    assert rows["ALL"]["parts"]["forecast_skill"]["n"] == 240
    assert rows["ZA"]["parts"]["forecast_skill"]["value"] == pytest.approx(0.87)
    assert rows["ZA"]["parts"]["forecast_skill"]["points"] == pytest.approx(93.5)
    assert rows["NG"]["parts"]["forecast_skill"]["insufficient"] is True
    assert rows["ALL"]["parts"]["forecast_skill"]["query_id"] == "core/eval/sql/forecast_rows.sql?d=2026-09-28"


def test_precision_comes_from_the_latest_engine_scorecard_run_and_time_to_detect_is_shown_not_scored(
        warehouse, tmp_path):
    wh = warehouse()
    wh.insert("engine_scorecard", [scorecard("ZA", 0.5, 20, run_id="learn-1"), scorecard("ZA", 0.8, 20, run_id="learn-2"),
                                   scorecard("NG", 0.6, 30, run_id="learn-2"), scorecard("KE", None, 0)])
    out = quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    rows = by_market(out)
    assert rows["ZA"]["parts"]["precision"]["value"] == pytest.approx(0.8)
    assert rows["ALL"]["parts"]["precision"]["value"] == pytest.approx((0.8 * 20 + 0.6 * 30) / 50)
    assert rows["ALL"]["parts"]["precision"]["n"] == 50
    assert rows["KE"]["parts"]["precision"]["insufficient"] is True
    assert rows["ZA"]["parts"]["precision"]["query_id"] == "core/eval/sql/engine_scorecard.sql?week_start=2026-09-21"
    assert rows["ZA"]["parts"]["precision"]["row_count"] == 3
    assert rows["ALL"]["parts"]["precision"]["result_hash"].startswith("sha256:")
    ttd = rows["ZA"]["context"]["time_to_detect"]
    assert ttd["value"] == 4.0 and ttd["points"] is None
    assert "time_to_detect" not in rows["ZA"]["counted"]



def test_precision_reads_the_pinned_scorecard_run_over_a_later_sorting_one(warehouse, tmp_path):
    # learn scores the run it has just written; a failed earlier attempt can carry a run_id that sorts after it.
    wh = warehouse()
    wh.insert("engine_scorecard", [scorecard("ZA", 0.5, 20, run_id="learn-1"), scorecard("ZA", 0.8, 20, run_id="learn-2"),
                                   scorecard("NG", 0.6, 30, run_id="learn-2")])
    out = quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW, scorecard_run_id="learn-1")
    rows = by_market(out)
    assert rows["ZA"]["parts"]["precision"]["value"] == pytest.approx(0.5)
    assert rows["NG"]["parts"]["precision"]["value"] == pytest.approx(0.6), "a market the run did not write keeps its latest"
    assert rows["ALL"]["parts"]["precision"]["n"] == 50
    assert rows["ZA"]["parts"]["precision"]["query_id"] == \
        "core/eval/sql/engine_scorecard.sql?run_id=learn-1&week_start=2026-09-21"


def test_an_absent_engine_scorecard_is_skipped_with_a_note(warehouse, tmp_path):
    wh = warehouse("forecasts", "feedback")
    out = quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    part = by_market(out)["ALL"]["parts"]["precision"]
    assert part["insufficient"] is True and "engine_scorecard" in part["reason"]
    assert any("engine_scorecard" in n for n in out["notes"])


# The number

def test_score_is_the_mean_of_sufficient_parts_and_insufficient_parts_never_count_as_zero(warehouse, tmp_path):
    wh = warehouse()
    score_file(tmp_path, "2026-09-25", failed=["A"])                               # 90 points
    wh.insert("forecasts", good_cohort())                                           # skill 0.87, 93.5 points
    out = quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    top = by_market(out)["ALL"]
    assert top["counted"] == ["eval_pass_rate", "forecast_skill"]
    assert top["score"] == pytest.approx((90 + 93.5) / 2)
    for name in ("claim_support", "honest_gaps", "precision"):
        assert top["parts"][name]["insufficient"] is True
        assert top["parts"][name]["value"] is None and top["parts"][name]["points"] is None
    for part in top["parts"].values():
        assert {"value", "unit", "query_id", "result_hash", "row_count", "n"} <= set(part)
        assert part["query_id"]


def test_no_sufficient_part_means_no_score(warehouse, tmp_path):
    out = quality_score.run(warehouse().execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    for r in out["rows"]:
        assert r["score"] is None and r["counted"] == [] and r["change"] is None


def test_skill_points_run_from_0_at_minus_one_to_100_at_one():
    assert quality_score.skill_points(0.0) == 50.0
    assert quality_score.skill_points(1.0) == 100.0
    assert quality_score.skill_points(-3.0) == 0.0


# Week on week

def test_change_is_reported_only_against_the_same_counted_parts(warehouse, tmp_path):
    wh = warehouse()
    score_file(tmp_path, "2026-09-18", failed=["A", "B"])                           # last week: 80
    score_file(tmp_path, "2026-09-25", failed=["A"])                                # this week: 90
    quality_score.run(wh.execute, WEEK - timedelta(days=7), scores=tmp_path, dry_run=False, now=NOW)
    out = quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    top = by_market(out)["ALL"]
    assert top["change"] == pytest.approx(10.0)
    assert top["previous_run_id"] == "weekly-quality-2026-09-14-20260928T080000Z"
    wh.insert("forecasts", good_cohort())                                           # a new part joins this week
    out = quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    top = by_market(out)["ALL"]
    assert top["change"] is None and "counted" in top["change_reason"]


def test_change_is_withheld_when_the_replay_question_set_differs(warehouse, tmp_path):
    wh = warehouse()
    score_file(tmp_path, "2026-09-18", failed=["A", "B"])
    score_file(tmp_path, "2026-09-25", failed=["A"], ids=[f"R{i:02d}" for i in range(10)])  # same count, new ids
    quality_score.run(wh.execute, WEEK - timedelta(days=7), scores=tmp_path, dry_run=False, now=NOW)
    top = by_market(quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW))["ALL"]
    assert top["score"] == pytest.approx(90.0) and top["counted"] == ["eval_pass_rate"]
    assert top["change"] is None and "question set" in top["change_reason"]
    assert top["previous_run_id"] == "weekly-quality-2026-09-14-20260928T080000Z"


def test_change_is_withheld_when_the_replay_question_count_differs(warehouse, tmp_path):
    wh = warehouse()
    # no question ids in either file, so both question set hashes are None and the count alone differs
    score_file(tmp_path, "2026-09-18", questions=12, failed=["A", "B"], with_ids=False)
    score_file(tmp_path, "2026-09-25", questions=10, failed=["A"], with_ids=False)
    quality_score.run(wh.execute, WEEK - timedelta(days=7), scores=tmp_path, dry_run=False, now=NOW)
    top = by_market(quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW))["ALL"]
    assert top["change"] is None and "12 questions" in top["change_reason"]


# Writing

def test_dry_run_writes_nothing(warehouse, tmp_path):
    wh = warehouse()
    score_file(tmp_path, "2026-09-25", failed=["A"])
    quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW)
    assert not wh.exists("weekly_quality")
    assert wh.statements and not any(isinstance(st, (exp.Insert, exp.Create))
                                     for s in wh.statements for st in sqlglot.parse(s, read="bigquery"))


def test_a_real_run_appends_one_row_per_market_every_run(warehouse, tmp_path):
    wh = warehouse()
    score_file(tmp_path, "2026-09-25", failed=["A"])
    quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=False, now=NOW)
    quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=False, now=NOW + timedelta(hours=1))
    stored = wh.rows("weekly_quality")
    assert sorted(r["market"] for r in stored) == ["ALL", "ALL", "KE", "KE", "NG", "NG", "ZA", "ZA"]
    top = [r for r in stored if r["market"] == "ALL"][0]
    assert top["week_start"] == WEEK and top["week_end"] == date(2026, 9, 27)
    assert top["score"] == pytest.approx(90.0)
    parts = json.loads(top["parts"]) if isinstance(top["parts"], str) else top["parts"]
    assert parts["eval_pass_rate"]["value"] == pytest.approx(0.9)
    assert parts["claim_support"]["insufficient"] is True
    assert top["questions"] == 10 and top["question_set_hash"].startswith("sha256:")
    assert top["previous_run_id"] is None


# The CLI

def test_cli_help_works():
    done = subprocess.run([sys.executable, "-m", "core.eval.quality_score", "--help"],
                          capture_output=True, cwd=Path(__file__).resolve().parents[3])
    assert done.returncode == 0
    assert b"--dry-run" in done.stdout and b"--scores" in done.stdout
