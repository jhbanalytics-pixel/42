"""Score a promptfoo results export against rubric.md section 4 (task 1.16).

    py -3.13 -m core.eval.score_run <results.json> --live-credits 0 --questions core/eval/friday.yaml [--out core/eval/results/]

Writes score-<date>.json and score-<date>.md. --questions is required and every id in it
is expected: one absent from the export is a failed question with metric "missing".
An id exported more than once keeps every entry and fails if any entry fails.

The parser reads the promptfoo export (results version 3, checked against a real
promptfoo 0.123.1 run in tests/fixtures/promptfoo_real_export.json): results.results[]
entries with vars, response.output, failureReason and gradingResult.componentResults[]
(assertion.metric, pass, score, reason). That real run graded with javascript
stand-ins, so the llm-rubric component shape is still unverified.
"""

import argparse
import json
import math
import re
import statistics
import sys
from datetime import date
from pathlib import Path

import yaml

EVAL = Path(__file__).resolve().parent

# Grader score is rating / 5 (rubric.md section 4); minimum rating per dimension.
MIN_RATING = {"usefulness": 3, "grounding": 4, "specificity": 3, "freshness": 3, "honesty": 3, "so_what": 3}
CHECKS = ["claims_have_evidence", "citation_integrity", "evidence_floor", "no_age_lens"]
HARD_FAIL = "hard_fail_screen"
BOUNDARY = {"insufficient_evidence", "partial", "refused"}
RETURNED = {"complete", "partial"}
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
LEGACY_DAYS = 30
PROVIDER_ERROR = 2  # promptfoo ResultFailureReason: 0 none, 1 assert, 2 error
LEGACY = "rests partly on legacy rows (memory only)"


def _answer(entry):
    output = (entry.get("response") or {}).get("output")
    if isinstance(output, dict):
        return output
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _components(entry):
    """Every component per metric, in export order, so a repeat cannot hide an earlier failure."""
    found = {}
    for c in (entry.get("gradingResult") or {}).get("componentResults") or []:
        metric = (c.get("assertion") or {}).get("metric")
        if metric:
            found.setdefault(metric, []).append(c)
    return found


def _grader_score(c):
    x = c.get("score")
    ok = isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 0 <= x <= 1
    return x if ok else None


def _score_question(entry, question):
    v = {**(entry.get("testCase") or {}).get("vars", {}), **(entry.get("vars") or {}), **(question.get("vars") or {})}
    comps = _components(entry)
    answer = _answer(entry)
    status = (answer or {}).get("status")
    claims = (answer or {}).get("claims") or []
    failures = []
    # promptfoo also copies a failed assertion's reason into error (failureReason 1); only 2 is a provider error.
    if entry.get("failureReason") == PROVIDER_ERROR:
        failures.append({"metric": "transport", "reason": str(entry["error"])})

    ratings = {}
    for metric, minimum in MIN_RATING.items():
        if metric not in comps:
            failures.append({"metric": metric, "reason": "not graded"})
            continue
        for c in comps[metric]:
            x = _grader_score(c)
            if x is None:
                failures.append({"metric": metric, "reason": f"grader score out of range: {c.get('score')!r}, must be 0 to 1"})
                continue
            # rubric.md section 2: between two anchors, give the lower. Never round up.
            rating = math.floor(x * 5 + 1e-9)
            ratings[metric] = min(rating, ratings.get(metric, rating))
            if c.get("pass") is not True or rating < minimum:
                verdict = "grader failed it" if c.get("pass") is not True else "below minimum"
                failures.append({"metric": metric, "reason": f"{verdict}, rating {rating}, minimum {minimum}: {c.get('reason', '')}"})
    for metric in CHECKS + [HARD_FAIL]:
        if metric not in comps:
            failures.append({"metric": metric, "reason": "not graded"})
        for c in comps.get(metric, []):
            if c.get("pass") is not True:
                failures.append({"metric": metric, "reason": c.get("reason") or "failed"})

    # promptfoo's own verdict on the entry: a fail no metric above accounts for must still fail it.
    grading = entry.get("gradingResult") or {}
    if (entry.get("success") is False or grading.get("pass") is False) and all(f["reason"] == "not graded" for f in failures):
        reason = grading.get("reason") or entry.get("error") or "promptfoo failed the entry"
        failures.append({"metric": "promptfoo_verdict", "reason": f"promptfoo failed the entry, no scored metric explains it: {reason}"})

    label = v.get("expected_answerability")
    passed = not failures
    failed_metrics = {f["metric"] for f in failures}
    honest_boundary = passed and label == "thin" and status in BOUNDARY
    window_days = v.get("window_days")
    return {
        "family": v.get("family"),
        "markets": v.get("markets"),
        "window_days": window_days,
        "answerability": label,
        "status": status,
        "outcome": "honest_boundary" if honest_boundary else ("pass" if passed else "fail"),
        "pass": passed,
        "ratings": ratings,
        "failures": failures,
        "citation_integrity": "citation_integrity" in comps and "citation_integrity" not in failed_metrics,
        "fabricated_citation": "citation_integrity" in failed_metrics,
        "unsupported_assertion": HARD_FAIL in failed_metrics,
        # rubric.md hard fail 8: no claims where the question could be answered, guardrail included,
        # whatever the status. A transport failure returned no answer, so it is counted as transport.
        "false_full_refusal": label != "thin" and not claims and "transport" not in failed_metrics,
        # Claim support only (score_questions.supported_completion); usefulness is judged apart.
        "supported_complete": status in RETURNED and bool(claims) and not failed_metrics & {
            "transport", "claims_have_evidence", "citation_integrity", "grounding", HARD_FAIL,
        },
        "numbers": [n for c in claims for n in (c.get("numbers") or [])],
        "legacy_note": _legacy(window_days),
    }


def _legacy(window_days):
    return LEGACY if isinstance(window_days, (int, float)) and window_days > LEGACY_DAYS else None


def _merge(runs):
    """One id exported several times: keep every entry; any failing entry fails the id."""
    if len(runs) == 1:
        return {**runs[0], "entries": 1}
    first_fail = next((r for r in runs if not r["pass"]), runs[0])
    ratings = {}
    for r in runs:
        for metric, rating in r["ratings"].items():
            ratings[metric] = min(rating, ratings.get(metric, rating))
    if all(r["outcome"] == "honest_boundary" for r in runs):
        outcome = "honest_boundary"
    else:
        outcome = "pass" if all(r["pass"] for r in runs) else "fail"
    return {
        **runs[0],
        "status": first_fail["status"],
        "outcome": outcome,
        "pass": all(r["pass"] for r in runs),
        "entries": len(runs),
        "ratings": ratings,
        "failures": [{**f, "entry": i} for i, r in enumerate(runs, 1) for f in r["failures"]],
        "citation_integrity": all(r["citation_integrity"] for r in runs),
        "fabricated_citation": any(r["fabricated_citation"] for r in runs),
        "unsupported_assertion": any(r["unsupported_assertion"] for r in runs),
        "false_full_refusal": any(r["false_full_refusal"] for r in runs),
        "supported_complete": all(r["supported_complete"] for r in runs),
        "numbers": [n for r in runs for n in r["numbers"]],
    }


def _missing(question):
    v = question["vars"]
    return {
        "family": v.get("family"),
        "markets": v.get("markets"),
        "window_days": v.get("window_days"),
        "answerability": v.get("expected_answerability"),
        "status": None,
        "outcome": "fail",
        "pass": False,
        "entries": 0,
        "ratings": {},
        "failures": [{"metric": "missing", "reason": "expected by the questions file, absent from the export"}],
        "citation_integrity": False,
        "fabricated_citation": False,
        "unsupported_assertion": False,
        "false_full_refusal": False,
        "supported_complete": False,
        "numbers": [],
        "legacy_note": _legacy(v.get("window_days")),
    }


def _gate(ids):
    return {"count": len(ids), "ids": ids, "pass": not ids}


def score(results_json, questions, *, run_meta, expected):
    """expected: ids that must be in the export; each one absent is a failed question."""
    by_id = {q["vars"]["id"]: q for q in questions}
    entries = (results_json.get("results") or {}).get("results") or []
    runs = {}
    for entry in entries:
        qid = (entry.get("vars") or (entry.get("testCase") or {}).get("vars") or {})["id"]
        runs.setdefault(qid, []).append(_score_question(entry, by_id.get(qid, {})))
    scored = {qid: _merge(r) for qid, r in runs.items()}
    for qid in expected:
        if qid not in scored:
            scored[qid] = _missing(by_id[qid])

    families = {}
    for q in scored.values():
        families.setdefault(q["family"], []).append(q["ratings"].get("usefulness"))
    family_scores = {}
    for fam, values in families.items():
        rated = [x for x in values if x is not None]
        median = statistics.median(rated) if rated else None
        if len(rated) < 3:
            result = "partial_family"
        else:
            result = "pass" if median >= 4 else "fail"
        family_scores[fam] = {"scored": len(rated), "median_usefulness": median, "result": result}

    answerable = [k for k, q in scored.items() if q["answerability"] != "thin"]
    completed = [k for k in answerable if scored[k]["supported_complete"]]
    failed_completion = [k for k in answerable if k not in completed]
    gates = {
        "fabricated_citations": _gate([k for k, q in scored.items() if q["fabricated_citation"]]),
        "unsupported_assertions": _gate([k for k, q in scored.items() if q["unsupported_assertion"]]),
        "false_full_refusals": _gate([k for k, q in scored.items() if q["false_full_refusal"]]),
        "supported_completion": {
            "denominator": len(answerable),
            "completed": len(completed),
            "rate": len(completed) / len(answerable) if answerable else None,
            "failed_ids": failed_completion,
            "pass": bool(answerable) and not failed_completion,
        },
    }

    thin = [k for k, q in scored.items() if q["answerability"] == "thin"]
    thin_summary = {
        "count": len(thin),
        "ids": thin,
        "honest_boundaries": sum(scored[k]["outcome"] == "honest_boundary" for k in thin),
        "passed": sum(scored[k]["pass"] for k in thin),
        "failed": sum(not scored[k]["pass"] for k in thin),
    }

    numbers = [n for q in scored.values() for n in q.pop("numbers")]
    pinned = sum(
        bool(n.get("query_id")) and bool(n.get("run_id")) and bool(HASH.match(str(n.get("result_hash") or "")))
        for n in numbers
    )
    checked = sum(q["entries"] > 0 for q in scored.values())
    cit_passed = sum(q["citation_integrity"] for q in scored.values())
    trust = {
        "citation_integrity": {
            "rate": cit_passed / checked if checked else None,
            "passed": cit_passed,
            "checked": checked,
            "target": 1.0,
            "unit": "answers whose every citation resolves and every quote matches (TRUST.md section 7)",
        },
        "number_pinning": {
            "rate": pinned / len(numbers) if numbers else None,
            "pinned": pinned,
            "numbers": len(numbers),
            "target": 1.0,
            "unit": "claims[].numbers carrying query_id, run_id and a sha256 result_hash; the re-run itself is not done offline",
            "reason": None if numbers else "no answer carried claims[].numbers",
        },
    }

    invalid = []
    if run_meta.get("mode") != "replay":
        invalid.append(f"mode is {run_meta.get('mode')!r}, the evaluation must run in replay")
    credits = run_meta.get("live_credits_spent")
    if credits is None:
        invalid.append("live credits spent were not recorded")
    elif credits != 0:
        invalid.append(f"{credits} live credits spent in a replay, must be 0")

    legacy_ids = [k for k, q in scored.items() if q["legacy_note"]]
    failed = [k for k, q in scored.items() if not q["pass"]]
    run = {
        "date": run_meta.get("date"),
        "mode": run_meta.get("mode"),
        "live_credits_spent": credits,
        "valid": not invalid,
        "invalid_reasons": invalid,
        "pass": not invalid
        and not failed
        and all(f["result"] != "fail" for f in family_scores.values())
        and all(g["pass"] for g in gates.values()),
        "questions": len(scored),
        "legacy_note": f"{len(legacy_ids)} of {len(scored)} questions have windows over {LEGACY_DAYS} days; each {LEGACY}: {', '.join(legacy_ids)}"
        if legacy_ids
        else None,
        "answerability_source": "questions.yaml expected_answerability (author prior)",
    }
    return {
        "run": run,
        "failed_questions": failed,
        "questions": scored,
        "families": family_scores,
        "gates": gates,
        "thin": thin_summary,
        "trust": trust,
    }


def _cell(text):
    return " ".join(str(text).split()).replace("|", "/")


def _rate(x):
    return "n/a" if x is None else f"{x:.0%}"


def render_markdown(s):
    run = s["run"]
    lines = [f"# 42 evaluation score, {run['date']}", ""]
    verdict = "PASS" if run["pass"] else "FAIL"
    validity = "valid" if run["valid"] else "INVALID: " + "; ".join(run["invalid_reasons"])
    lines += [
        f"Run: {verdict}. Mode {run['mode']}, live credits spent {run['live_credits_spent']}, {validity}.",
        f"{run['questions']} questions scored; answerability from {run['answerability_source']}.",
    ]
    if run["legacy_note"]:
        lines.append(f"Legacy note: {run['legacy_note']}.")
    lines += ["", "## Failed questions", ""]
    if not s["failed_questions"]:
        lines.append("None.")
    for qid in s["failed_questions"]:
        q = s["questions"][qid]
        lines.append(f"- {qid} ({q['family']}, status {q['status']})")
        lines += [f"  - {f['metric']}{' (entry ' + str(f['entry']) + ')' if 'entry' in f else ''}: {_cell(f['reason'])}" for f in q["failures"]]
    lines += ["", "## Families", "", "| Family | Scored | Median usefulness | Result |", "|---|---|---|---|"]
    for fam, f in s["families"].items():
        lines.append(f"| {fam} | {f['scored']} | {f['median_usefulness']} | {f['result']} |")
    g = s["gates"]
    sc = g["supported_completion"]
    lines += ["", "## Run gates", "", "| Gate | Value | Result |", "|---|---|---|"]
    for name in ["fabricated_citations", "unsupported_assertions", "false_full_refusals"]:
        ids = ", ".join(g[name]["ids"])
        lines.append(f"| {name} | {g[name]['count']}{' (' + ids + ')' if ids else ''} | {'pass' if g[name]['pass'] else 'FAIL'} |")
    missed = ", ".join(sc["failed_ids"])
    lines.append(
        f"| supported_completion | {sc['completed']} of {sc['denominator']}{' (missed ' + missed + ')' if missed else ''} | {'pass' if sc['pass'] else 'FAIL'} |"
    )
    t = s["thin"]
    lines += [
        "",
        "## Thin questions",
        "",
        f"{t['count']} thin ({', '.join(t['ids']) or 'none'}): {t['honest_boundaries']} honest boundaries, "
        f"{t['passed']} passed, {t['failed']} failed. Not in the supported denominator.",
    ]
    ci, nr = s["trust"]["citation_integrity"], s["trust"]["number_pinning"]
    lines += [
        "",
        "## Trust metrics",
        "",
        f"- Citation integrity: {_rate(ci['rate'])} ({ci['passed']} of {ci['checked']} {ci['unit']}), target 100%",
        f"- Number pinning (no re-run): {_rate(nr['rate'])} ({nr['pinned']} of {nr['numbers']} {nr['unit']}), target 100%"
        + (f"; {nr['reason']}" if nr["reason"] else ""),
        "",
        "## All questions",
        "",
        "| Id | Family | Window days | Outcome | Usefulness | Note |",
        "|---|---|---|---|---|---|",
    ]
    for qid, q in s["questions"].items():
        lines.append(
            f"| {qid} | {q['family']} | {q['window_days']} | {q['outcome']} | {q['ratings'].get('usefulness', 'n/a')} | {q['legacy_note'] or ''} |"
        )
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Score a promptfoo results export (rubric.md section 4).")
    p.add_argument("results")
    p.add_argument("--out", default=str(EVAL / "results"))
    p.add_argument("--questions", required=True, help="questions file; every id in it is expected in the export")
    p.add_argument("--live-credits", type=int, required=True, help="live SocialCrawl credits the run spent, from the ledger")
    p.add_argument("--mode", default="replay")
    p.add_argument("--date", default=date.today().isoformat())
    a = p.parse_args(argv)
    results = json.loads(Path(a.results).read_text(encoding="utf-8"))
    questions = yaml.safe_load(Path(a.questions).read_text(encoding="utf-8"))
    expected = [q["vars"]["id"] for q in questions]
    s = score(results, questions, run_meta={"mode": a.mode, "live_credits_spent": a.live_credits, "date": a.date}, expected=expected)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"score-{a.date}.json").write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / f"score-{a.date}.md").write_text(render_markdown(s), encoding="utf-8")
    print(f"{'PASS' if s['run']['pass'] else 'FAIL'}{'' if s['run']['valid'] else ' (INVALID)'}: {out / f'score-{a.date}.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
