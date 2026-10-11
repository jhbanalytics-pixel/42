"""Scoring for the offline Ask replay (core/eval/ask_replay.py): what each replayed answer is measured on, and which
of those measures mean anything without the paid staging run.

The measures are those of ASK-POWER-PLAN section 1 (the powerful answer and its ceilings). They are computed from the
answer and the run object the pipeline returned, never read from the fixture that produced them. Beside them sit the
four deterministic promptfoo asserts of promptfooconfig.yaml, run through node as the promptfoo run would run them, the
weekly review's risk ranking (review.py), and the score_run reading of the same answers (score_run.py), which grades
what code can grade and names the rest as ungraded.

Each score is tagged by what the replay can say about it. offline: the code under test did the work on the fixture and
a change in the code changes the score (trust gate, privacy hold, cost against the hold). fixture: the counting is the
code's but the number is the fixture's, so it proves the scorer and says nothing about the real store or model (depth,
spread, live platforms, credits, dollars). live: only the staging run can give it (the six rubric dimensions, the hard
fail screen, seconds, whether the cause reads as a cause)."""

import json
import re
import subprocess
from pathlib import Path

import yaml

from core.agent.context import result_hash
from core.agent.tools.socialcrawl import ITEM_STATUSES
from core.eval import review, score_run

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parents[1]

COMMENT_ID = re.compile(r"^[a-z0-9]+_comment_")
SOCIAL_PLATFORMS = {"tiktok", "instagram", "youtube", "reddit", "x", "threads", "bluesky", "facebook", "linkedin",
                    "telegram"}
SEARCH_SEGMENTS = {"search", "hashtag"}

# ASK-POWER-PLAN section 1, "Measures of a powerful answer". The ceilings are per tier: credits typical, model USD.
ON_TOPIC_POSTS = 100
CITED_POSTS, CITED_CREATORS, CITED_PLATFORMS = 8, 6, 2
LIVE_PLATFORMS = 3
COMMENT_CLAIMS = 2
CEILINGS = {"T1": {"credits": 45, "model_usd": 1.50}, "T2": {"credits": 150, "model_usd": 4.0}}

# The deterministic promptfoo asserts that must hold for every claim the gate let through. evidence_floor is a depth
# score and not a leak: a thin answer legitimately misses it.
LEAK_CHECKS = ("claims_have_evidence", "citation_integrity", "no_age_lens")

MEANING = {
    "trust_gate": "offline", "privacy": "offline", "hold": "offline", "contract": "offline",
    "answer_meta": "offline", "claim_outcomes": "offline",
    "on_topic_posts": "fixture", "cited_spread": "fixture", "live_platforms": "fixture", "comment_claims": "fixture",
    "model_usd": "fixture", "credits": "fixture", "within_hold": "fixture", "evidence_floor": "fixture",
    "rubric_dimensions": "live", "hard_fail_screen": "live", "seconds": "live", "cause_first": "live",
    "support_verdicts": "live",
}

# Reads {asserts, pairs} on stdin: runs every promptfoo assert on every (answer, vars) pair and prints the results.
RUNNER = """
import { readFileSync } from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
const out = input.pairs.map((pair) => {
  const output = JSON.stringify(pair.answer);
  const row = {};
  for (const a of input.asserts) {
    const result = new Function('output', 'context', a.body)(output, { vars: pair.vars });
    row[a.metric] = { pass: result.pass, reason: result.reason };
  }
  return row;
});
process.stdout.write(JSON.stringify(out));
"""


def _creator(record):
    handle = str(record.get("handle") or "").strip().lstrip("@").lower()
    return (str(record.get("platform") or "").lower(), handle)


def _is_comment(record):
    return bool(COMMENT_ID.match(str(record.get("id") or "")) or record.get("parent_id")
                or record.get("transcript_span"))


def cited(answer):
    """The posts the claims cite, once each, in order of first citation: how many, by how many creators on how many
    platforms, and how many claims rest on a comment or a transcript."""
    records = {r.get("id"): r for r in answer.get("evidence") or [] if isinstance(r, dict)}
    ids = list(dict.fromkeys(e for c in answer.get("claims") or [] for e in c.get("evidence_ids") or []
                             if e in records))
    found = [records[i] for i in ids]
    return {
        "ids": ids,
        "posts": len(ids),
        "creators": len({_creator(r) for r in found}),
        "platforms": len({str(r.get("platform") or "").lower() for r in found}),
        "comment_claims": sum(any(e in records and _is_comment(records[e]) for e in c.get("evidence_ids") or [])
                              for c in answer.get("claims") or []),
    }


def _live_platforms(run):
    """Platforms a live search call reached: a search or hashtag route of a social platform whose call answered, with
    items or without. A comments or transcript call is not a search, and a multi-platform route names no platform."""
    found = set()
    for row in run.get("source_status") or []:
        parts = str(row.get("route") or "").split("/")
        platform = {"twitter": "x"}.get(parts[0], parts[0])
        if (platform in SOCIAL_PLATFORMS and SEARCH_SEGMENTS & set(parts[1:])
                and row.get("status") in ITEM_STATUSES):
            found.add(platform)
    return found


def _round(value):
    return round(value, 4) if isinstance(value, float) else value


def _measure(name, value, target, result, **extra):
    return {"name": name, "value": _round(value), "target": _round(target), "result": result,
            "meaning": MEANING[name], **{k: _round(v) for k, v in extra.items()}}


def _atleast(value, target):
    return "pass" if value >= target else "fail"


def measures(result):
    """The ASK-POWER-PLAN measures for one replayed question, in the plan's order. A measure that does not apply to
    the question is not_applicable; one a person has to read is needs_reading; one only staging can give is live_only."""
    answer, run = result["answer"], result["run"]
    tier = result.get("tier") or run.get("tier") or "T1"
    sums = cited(answer)
    on_topic = len(set(result.get("on_topic_ids") or []))
    thin = on_topic < ON_TOPIC_POSTS
    why = result.get("family") == "why"
    ceilings = CEILINGS[tier]
    live = _live_platforms(run)
    out = [
        _measure("on_topic_posts", on_topic, ON_TOPIC_POSTS, _atleast(on_topic, ON_TOPIC_POSTS)),
        _measure("cited_spread", {"posts": sums["posts"], "creators": sums["creators"], "platforms": sums["platforms"]},
                 {"posts": CITED_POSTS, "creators": CITED_CREATORS, "platforms": CITED_PLATFORMS},
                 "pass" if (sums["posts"] >= CITED_POSTS and sums["creators"] >= CITED_CREATORS
                            and sums["platforms"] >= CITED_PLATFORMS) else "fail"),
        _measure("live_platforms", len(live), LIVE_PLATFORMS,
                 _atleast(len(live), LIVE_PLATFORMS) if thin else "not_applicable", platforms=sorted(live)),
        _measure("comment_claims", sums["comment_claims"], COMMENT_CLAIMS,
                 _atleast(sums["comment_claims"], COMMENT_CLAIMS) if why else "not_applicable"),
    ]
    first = (answer.get("claims") or [{}])[0].get("text")
    out.append(_measure("cause_first", None, "a stated cause, or the plain words that the posts do not show why",
                        "needs_reading" if why else "not_applicable",
                        read={"short_answer": answer.get("short_answer"), "first_claim": first} if why else None))
    if "checks" in result:
        leaks = gate_leaks(result["checks"])
        out.append(_measure("trust_gate", leaks, [], "fail" if leaks else "pass"))
    usd, credits = float(run.get("model_usd") or 0.0), float(run.get("credits") or 0.0)
    hold = float(result["hold_usd"])
    out += [
        _measure("model_usd", usd, ceilings["model_usd"], "pass" if usd <= ceilings["model_usd"] else "fail"),
        _measure("credits", credits, ceilings["credits"], "pass" if credits <= ceilings["credits"] else "fail"),
        _measure("within_hold", usd, hold, "pass" if usd <= hold else "fail", hold_usd=hold),
        _measure("seconds", None, "240 at p50 and 420 at p90 for T1", "live_only"),
    ]
    return out


def outcomes(answer, claim_checks, events):
    """What the gate did to the drafted claims, from the persisted check rows and the claim events: how many were
    drafted, which were kept, which kept claims had a label lowered, and for each cut claim the rules that cut it and
    the reason a reader is shown."""
    drafted = list(dict.fromkeys(r["claim_id"] for r in claim_checks if r.get("claim_id") != "short_answer"))
    kept = [c["id"] for c in answer.get("claims") or []]
    lowered = {r["claim_id"] for r in claim_checks if r.get("verdict") == "downgrade"}
    shown = {e["claim"]["id"]: e.get("reason") for e in events
             if e.get("event") == "claim" and e.get("check") == "cut"}
    cuts = [{"claim_id": cid,
             "rules": list(dict.fromkeys(r["rule"] for r in claim_checks
                                         if r.get("claim_id") == cid and r.get("verdict") == "cut")),
             "reason": shown.get(cid)}
            for cid in drafted if cid not in kept]
    return {"drafted": len(drafted), "kept": kept, "downgraded": [c for c in kept if c in lowered], "cut": cuts}


def number_receipts(answer, receipts):
    """Whether each number a kept claim carries is really pinned. score_run counts a number as pinned when its hash is
    shaped like a sha256, which proves only that the writer can format hexadecimal. Here the hash is recomputed from
    the rows of the query's receipt and compared with the one the claim carries. A query with no receipt, and a receipt
    whose rows were cut short (the hash covers every row), cannot be recomputed and are reported as problems."""
    found = {"numbers": 0, "recomputed": 0, "problems": []}
    for claim in answer.get("claims") or []:
        for n in claim.get("numbers") or []:
            found["numbers"] += 1
            query_id, receipt = n.get("query_id"), receipts.get(n.get("query_id"))
            if receipt is None:
                found["problems"].append(f"{claim['id']}: {query_id} has no receipt")
            elif receipt["row_count"] > len(receipt["rows"]):
                found["problems"].append(f"{claim['id']}: the rows of {query_id} were cut short, so its hash "
                                         "cannot be recomputed")
            elif result_hash(receipt["rows"]) != n.get("result_hash"):
                found["problems"].append(f"{claim['id']}: the hash of {query_id} is not the hash of its rows")
            else:
                found["recomputed"] += 1
    return found


def promptfoo_asserts():
    config = yaml.safe_load((EVAL / "promptfooconfig.yaml").read_text(encoding="utf-8"))
    found = [{"metric": a["metric"], "body": a["value"]} for a in config["defaultTest"]["assert"]
             if a["type"] == "javascript"]
    if [a["metric"] for a in found] != ["claims_have_evidence", "citation_integrity", "evidence_floor",
                                        "no_age_lens"]:
        raise RuntimeError("promptfooconfig.yaml no longer carries the four deterministic asserts the replay runs")
    return found


def deterministic_checks(pairs):
    """The four deterministic promptfoo asserts on each (answer, question vars) pair, one node run for all of them.
    Returns one {metric: {pass, reason}} per pair."""
    payload = {"asserts": promptfoo_asserts(), "pairs": [{"answer": a, "vars": v} for a, v in pairs]}
    done = subprocess.run(["node", "--input-type=module", "-e", RUNNER], input=json.dumps(payload),
                          capture_output=True, encoding="utf-8", cwd=ROOT)
    if done.returncode != 0:
        raise RuntimeError(f"the promptfoo asserts did not run: {done.stderr.strip()[:500]}")
    return json.loads(done.stdout)


def gate_leaks(checks):
    """The independent checks a claim that the trust gate let through still fails. Empty is the only healthy value:
    the gate and these asserts are written apart, so a disagreement means the gate was loosened or the two drifted."""
    return [m for m in LEAK_CHECKS if checks.get(m, {}).get("pass") is not True]


def review_flags(answer, market):
    """The claims in the order a reader should take them, by the weekly review's own risk score, and which of them
    quote a non-English post. scope is the review scope of the market (review.MARKETS)."""
    rows = []
    for claim in answer.get("claims") or []:
        quotes = " / ".join(q.get("text", "") for q in claim.get("quotes") or [])
        adapted = {"text": claim.get("text"), "quote": quotes,
                   "numbers": [{"query": n.get("query_id")} for n in claim.get("numbers") or []]}
        rows.append({"claim_id": claim["id"], "risk": review.risk_score(adapted),
                     "non_english": review.non_english(quotes), "language": review.language_of(quotes)})
    rows.sort(key=lambda r: -r["risk"])  # a stable sort keeps the answer's own order inside a risk level
    return {"scope": review.MARKETS[market], "read_first": rows}


def score_run_view(items):
    """What score_run makes of these answers. items are (id, vars, answer, deterministic checks). Only the four
    deterministic asserts exist offline, so the rating dimensions and the hard fail screen come back as 'not graded':
    they are named in ungraded and left out of failures, which holds only what code could judge."""
    entries, questions, ids = [], [], []
    for qid, vars_, ans, checks in items:
        comps = [{"assertion": {"metric": m}, "pass": r["pass"], "score": 1 if r["pass"] else 0,
                  "reason": r["reason"]} for m, r in checks.items()]
        entries.append({"vars": vars_, "response": {"output": ans}, "gradingResult": {"componentResults": comps}})
        questions.append({"vars": vars_})
        ids.append(qid)
    scored = score_run.score({"results": {"results": entries}}, questions,
                             run_meta={"mode": "replay", "live_credits_spent": 0, "date": "replay"}, expected=ids)
    ungraded, failures = set(), {}
    for qid in ids:
        failures[qid] = []
        for f in scored["questions"][qid]["failures"]:
            if f["reason"] == "not graded":
                ungraded.add(f["metric"])
            else:
                failures[qid].append(f)
    trust = scored["trust"]
    return {
        "trust": {"citation_integrity": {"passed": trust["citation_integrity"]["passed"],
                                         "checked": trust["citation_integrity"]["checked"]},
                  "number_pinning": {"pinned": trust["number_pinning"]["pinned"],
                                     "numbers": trust["number_pinning"]["numbers"]}},
        "fabricated_citations": scored["gates"]["fabricated_citations"]["ids"],
        "false_full_refusals": scored["gates"]["false_full_refusals"]["ids"],
        "ungraded": sorted(ungraded),
        "failures": failures,
    }


def score_file_names(folder):
    """Files in folder that quality_score would read as a weekly replay score (score-*.json). The replay writes none:
    its answers carry no rating from a grader, and a score file made of them would set the weekly pass rate."""
    return sorted(p.name for p in Path(folder).glob("score-*.json"))
