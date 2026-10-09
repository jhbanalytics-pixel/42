"""The weekly quality score (FEATURES.md 7; TRUST.md sections 6 and 7): one number from 0 to 100 showing whether 42
is getting better.

    py -3.13 -m core.eval.quality_score [--week-start MONDAY] [--scores DIR] [--dry-run]

One row per market (ALL, ZA, NG, KE) per week. The score is the plain mean of the points of the parts that had
enough data; each part is a Figure {value, unit, query_id, result_hash, row_count, n, minimum, points, insufficient,
reason}. query_id names the read, result_hash is the sha256 of what it returned (the rows, sorted, or the score_run
file's bytes) and row_count how many rows or questions that was. A part below its minimum is insufficient: value
and points are None, never 0, and it does not count. With no sufficient part there is no score.

change against last week is given only when last week's current row counted the same parts AND its replay had the
same question count and question set hash, so a part joining or leaving, or a different question set, never reads
as improvement. The question set hash is the sha256 of the sorted question ids in the score_run file: score_run
does not record which questions file it read, so the ids it scored stand in for that file. previous_run_id names
last week's row whenever there was one, change or not.

Parts and their points:
    eval_pass_rate  share of replay questions passing every rubric check, from the latest valid score_run file
                    (score-<date>.json) dated in the week; points 100 x share; at least 10 questions (the Friday
                    subset). Scored across markets, so the ZA, NG and KE rows mark it insufficient.
    claim_support   error rate on randomly sampled published claims over four weeks ending with this one, from the
                    weekly review labels in intelligence_42_agent.feedback (review.score_week, cumulative block);
                    points 100 x (1 - error rate); at least 30 claims, and labels this week.
    honest_gaps     thin questions answered as an honest boundary, over thin questions plus answerable questions
                    falsely refused, from the same score_run file; points 100 x share; at least 5.
    forecast_skill  Brier skill against persistence pooled over every resolved forecast in the market
                    (forecast_score.pooled_skill); points 50 + 50 x skill held to 0 to 100, so 50 is no better
                    than persistence; at least 200 resolved.
    precision       share of top-3 cards the weekly random review marks Real, from L2's engine_scorecard, one
                    run per market: the run learn has just written where it wrote the market, else the latest;
                    ALL is the n-weighted mean; points 100 x share; at least 10 cards.
time_to_detect from engine_scorecard is shown under context and not scored: TRUST.md section 7 sets it no target.
engine_scorecard, feedback and forecasts are read only. A table that does not exist yet leaves its parts
insufficient and a note, never an error.

A real run appends to intelligence_42_agent.weekly_quality (sql/weekly_quality_table.sql, CREATE TABLE IF NOT
EXISTS); --dry-run prints the rows and creates and writes nothing.
"""

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.agent.context import result_hash
from core.eval import forecast_score, review
from core.eval.forecast_score import load, query_id, read, rows_hash

RESULTS = Path(__file__).resolve().parent / "results"
MARKETS = ("ALL", "ZA", "NG", "KE")
PARTS = ("eval_pass_rate", "claim_support", "honest_gaps", "forecast_skill", "precision")
MIN_EVAL_QUESTIONS = 10   # the Friday replay subset, core/eval/friday.yaml
MIN_CLAIMS = 30           # one week's random claim sample, TRUST.md section 6
# Starting choices, not set by the spec: recalibrate once a few weeks of scored data show how much these parts move
# week to week, as DATA.md does for its own thresholds.
MIN_HONEST = 5            # thin questions plus false refusals in one replay
MIN_CARDS = 10            # reviewed top-3 cards in one week
ACROSS = "the replay is scored across markets, not per market"
NO_SOURCE = {"result_hash": None, "row_count": 0}


def figure(value, unit, qid, n, minimum, *, points, source=NO_SOURCE, reason=None):
    """A part. Below its minimum, or without a value, it is insufficient: value and points None, never 0."""
    fig = {"value": value, "unit": unit, "query_id": qid, **source, "n": n, "minimum": minimum}
    if value is None or n < minimum:
        return {**fig, "value": None, "points": None, "insufficient": True,
                "reason": reason or f"n is {n}, below the minimum of {minimum}"}
    return {**fig, "points": points(value), "insufficient": False, "reason": None}


def skill_points(skill):
    return float(max(0.0, min(100.0, 50 + 50 * skill)))


def iso_week(d):
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def latest_run(scores, week_start):
    """(path, body) of the latest valid score_run file dated in the week, or None."""
    best = None
    for path in sorted(Path(scores).glob("score-*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        run = body.get("run") or {}
        try:
            day = date.fromisoformat(str(run.get("date")))
        except ValueError:
            continue
        if run.get("valid") and week_start <= day <= week_start + timedelta(days=6) and (best is None or day >= best[0]):
            best = (day, path, body)
    return best and best[1:]


def _file_source(path, body):
    return {"result_hash": "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "row_count": body["run"]["questions"]}


def replay_key(scores, week_start):
    """The week's replay question count and question set hash, the part of the change key the score files hold."""
    found = latest_run(scores, week_start)
    if not found:
        return {"questions": None, "question_set_hash": None}
    body = found[1]
    ids = body.get("questions")
    return {"questions": body["run"]["questions"],
            "question_set_hash": result_hash(sorted(ids)) if isinstance(ids, dict) else None}


def no_score_reason(scores):
    """Why no replay score was found: the folder is absent (core/eval/results is not tracked, so it is not in the
    job image f42-learn runs) or it holds no valid file dated in the week."""
    if not Path(scores).is_dir():
        return (f"the score_run folder {Path(scores).name} does not exist here (core/eval/results is not in the "
                "job image), so no replay score can be read")
    return "no valid score_run file dated in the week"


def eval_pass_rate(scores, week_start):
    unit = "share of replay questions passing every rubric check, latest valid score_run file in the week"
    found = latest_run(scores, week_start)
    if not found:
        return figure(None, unit, f"file:{Path(scores).as_posix()}/score-*.json", 0, MIN_EVAL_QUESTIONS,
                      points=None, reason=no_score_reason(scores))
    path, body = found
    questions = body["run"]["questions"]
    passed = questions - len(body["failed_questions"])
    return figure(passed / questions if questions else None, unit, f"file:{path.name}#/run,/failed_questions",
                  questions, MIN_EVAL_QUESTIONS, points=lambda v: 100 * v, source=_file_source(path, body))


def honest_gaps(scores, week_start):
    unit = "thin questions answered as an honest boundary, over thin questions plus false full refusals"
    found = latest_run(scores, week_start)
    if not found:
        return figure(None, unit, f"file:{Path(scores).as_posix()}/score-*.json", 0, MIN_HONEST,
                      points=None, reason=no_score_reason(scores))
    path, body = found
    n = body["thin"]["count"] + body["gates"]["false_full_refusals"]["count"]
    return figure(body["thin"]["honest_boundaries"] / n if n else None, unit,
                  f"file:{path.name}#/thin,/gates/false_full_refusals", n, MIN_HONEST, points=lambda v: 100 * v,
                  source=_file_source(path, body))


def claim_support(labels, week_start, market, qid, source):
    unit = "error rate on randomly sampled published claims (contradicted or unverified), four weeks"
    weeks = [iso_week(week_start - timedelta(days=7 * k)) for k in (3, 2, 1, 0)]
    mine = [lab for lab in labels if lab["week"] in weeks and market in ("ALL", lab["market"])]
    this = [lab for lab in mine if lab["week"] == weeks[-1]]
    if not this:
        return figure(None, unit, qid, 0, MIN_CLAIMS, points=None, source=source,
                      reason=f"no review labels for {weeks[-1]}")
    block = review.score_week(this, prior=[lab for lab in mine if lab["week"] != weeks[-1]])["cumulative"]
    return figure(block["error_rate"], unit, qid, block["n"], MIN_CLAIMS, points=lambda v: 100 * (1 - v),
                  source=source)


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def precision(card_rows, market, qid, source, note):
    unit = "share of top-3 cards the weekly random review marks Real (engine_scorecard)"
    if card_rows is None:
        return figure(None, unit, qid, 0, MIN_CARDS, points=None, reason=note)
    figs = [_json(r["precision"]) for r in card_rows if market in ("ALL", r["market"])]
    figs = [f for f in figs if f.get("value") is not None and (f.get("n") or 0) > 0]
    n = sum(f["n"] for f in figs)
    value = sum(f["value"] * f["n"] for f in figs) / n if n else None
    part = figure(value, unit, qid, n, MIN_CARDS, points=lambda v: 100 * v, source=source)
    regime = _regime([f.get("regime") for f in figs])
    return {**part, "regime": regime} if regime else part


def _regime(markers):
    """The locality regime a precision part rests on (C4 v3 section 11.4), from the markers of the Figures it pools: it may
    be compared with last week's only when every one of them says so. None when none carries a marker (rows written
    under the first scorecard rule), which leaves the comparison as it was."""
    markers = [m for m in markers if isinstance(m, dict)]
    if not markers:
        return None
    return {"comparable_with_previous_week": all(m.get("comparable_with_previous_week") is True for m in markers),
            "locality_basis": sorted({str(m.get("locality_basis")) for m in markers}),
            "previous_week_basis": sorted({str(m.get("previous_week_basis")) for m in markers})}


def time_to_detect(card_rows, market, qid, source, note):
    if card_rows is None or market == "ALL":
        return {"value": None, "unit": "days", "query_id": qid, **source, "n": 0, "points": None,
                "reason": note or "a median per market; not pooled across markets"}
    fig = next((_json(r["time_to_detect"]) for r in card_rows if r["market"] == market), {})
    return {"value": fig.get("value"), "unit": fig.get("unit") or "days", "query_id": qid, **source,
            "n": fig.get("n") or 0, "points": None,
            "reason": fig.get("reason") or "shown, not scored: TRUST.md section 7 sets no target"}


def _change(score, counted, replay, prev, regime=None):
    """(change, reason) against last week's current row: withheld unless the parts and the replay match, and unless the
    precision part's locality regime says the two weeks may be compared (the rule that wrote item_state.eligible did not
    change between them, 11.4)."""
    if score is None:
        return None, "no score this week"
    if not prev or prev["score"] is None:
        return None, "no score for last week"
    if "precision" in counted and regime and regime.get("comparable_with_previous_week") is not True:
        return None, ("the precision figure's locality regime is not comparable with last week's "
                      f"(this week {regime.get('locality_basis')}, last week {regime.get('previous_week_basis')})")
    if prev["counted"] != ",".join(counted):
        return None, f"last week counted {prev['counted'] or 'nothing'}, this week counted {','.join(counted)}"
    if (prev["questions"], prev["question_set_hash"]) != (replay["questions"], replay["question_set_hash"]):
        return None, (f"last week's replay had {prev['questions']} questions, question set "
                      f"{prev['question_set_hash']}; this week's had {replay['questions']}, question set "
                      f"{replay['question_set_hash']}")
    return score - prev["score"], None


def score_market(market, *, week_start, scores, replay, labels, feedback_qid, feedback_source, forecasts,
                 forecast_qid, forecast_source, cards, cards_qid, cards_source, cards_note, previous):
    if market == "ALL":
        parts = {"eval_pass_rate": eval_pass_rate(scores, week_start), "honest_gaps": honest_gaps(scores, week_start)}
    else:
        parts = {"eval_pass_rate": figure(None, "share of replay questions passing", "file:score-*.json", 0,
                                          MIN_EVAL_QUESTIONS, points=None, reason=ACROSS),
                 "honest_gaps": figure(None, "share of thin questions answered honestly", "file:score-*.json", 0,
                                       MIN_HONEST, points=None, reason=ACROSS)}
    parts["claim_support"] = claim_support(labels, week_start, market, feedback_qid, feedback_source)
    skill = forecast_score.pooled_skill([r for r in forecasts if market in ("ALL", r["market"])],
                                        query_id=forecast_qid, **forecast_source)
    skill["points"] = None if skill["value"] is None else skill_points(skill["value"])
    skill["insufficient"] = skill["value"] is None
    parts["forecast_skill"] = skill
    parts["precision"] = precision(cards, market, cards_qid, cards_source, cards_note)
    parts = {name: parts[name] for name in PARTS}

    counted = [name for name in PARTS if not parts[name]["insufficient"]]
    score = sum(parts[name]["points"] for name in counted) / len(counted) if counted else None
    prev = previous.get(market)
    change, change_reason = _change(score, counted, replay, prev, parts["precision"].get("regime"))
    return {"market": market, "score": score, "counted": counted, "change": change, "change_reason": change_reason,
            "previous_run_id": prev["run_id"] if prev else None, **replay, "parts": parts,
            "context": {"time_to_detect": time_to_detect(cards, market, cards_qid, cards_source, cards_note)}}


def _source(rows):
    return {"result_hash": rows_hash(rows), "row_count": len(rows)} if rows is not None else NO_SOURCE


def run(execute, week_start, *, scores=RESULTS, dry_run, now, scorecard_run_id=None):
    """scorecard_run_id pins precision to that engine_scorecard run for each market it wrote (learn passes the run
    it has just written); otherwise each market's latest run is read."""
    week_end = week_start + timedelta(days=6)
    forecasts, forecast_note, forecast_qid = forecast_score.closed_rows(execute, week_end + timedelta(days=1))
    feedback, feedback_note = read(execute, "review_feedback", {}, "intelligence_42_agent.feedback")
    cards, cards_note = read(execute, "engine_scorecard", {"week_start": week_start, "run_id": scorecard_run_id},
                             "intelligence_42_agent.engine_scorecard")
    cards_params = {"week_start": week_start.isoformat(), **({"run_id": scorecard_run_id} if scorecard_run_id else {})}
    before = week_start - timedelta(days=7)
    previous, _ = read(execute, "weekly_quality_previous", {"week_start": before}, "intelligence_42_agent.weekly_quality")
    notes = [n for n in (forecast_note, feedback_note, cards_note) if n]
    common = {"week_start": week_start, "scores": scores, "replay": replay_key(scores, week_start),
              "labels": review.review_labels(feedback or [], {"claim", "card"}),
              "feedback_qid": query_id("review_feedback"), "feedback_source": _source(feedback),
              "forecasts": forecasts or [], "forecast_qid": forecast_qid, "forecast_source": _source(forecasts),
              "cards": cards, "cards_qid": query_id("engine_scorecard", **cards_params),
              "cards_source": _source(cards), "cards_note": cards_note,
              "previous": {r["market"]: r for r in previous or []}}
    rows = [score_market(market, **common) for market in MARKETS]
    run_id = f"weekly-quality-{week_start.isoformat()}-{now:%Y%m%dT%H%M%SZ}"
    if not dry_run:
        execute(load("weekly_quality_table"), {})
        for r in rows:
            execute(load("weekly_quality_insert"), {
                "week_start": week_start, "week_end": week_end, "market": r["market"], "run_id": run_id,
                "scored_at": now, "score": r["score"], "counted": ",".join(r["counted"]), "change": r["change"],
                "previous_run_id": r["previous_run_id"], "questions": r["questions"],
                "question_set_hash": r["question_set_hash"],
                "parts": json.dumps(r["parts"]), "context": json.dumps(r["context"]),
                "notes": "; ".join(notes + [r["change_reason"]] if r["change_reason"] else notes)})
    return {"week_start": week_start, "week_end": week_end, "run_id": run_id, "dry_run": dry_run, "rows": rows,
            "notes": notes}


def main(argv=None):
    p = argparse.ArgumentParser(prog="py -3.13 -m core.eval.quality_score",
                                description="The weekly quality score, 0 to 100, with its parts (TRUST.md section 7).")
    p.add_argument("--week-start", type=forecast_score.monday,
                   help="Monday of the week to score (default: the last full week)")
    p.add_argument("--scores", default=str(RESULTS), help="folder of score_run score-<date>.json files")
    p.add_argument("--dry-run", action="store_true", help="read and print the score; create and write nothing")
    a = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    week = a.week_start or forecast_score.last_full_week(now.date())
    out = run(forecast_score.bigquery_execute(), week, scores=a.scores, dry_run=a.dry_run, now=now)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
