"""Human reference check: did the briefs catch the cultural patterns a strategist spotted by hand, and did they keep the
news, sport, politics and celebrity events on the same sheet off Today?

    py -3.13 -m core.eval.reference_eval --briefs FILE [--cases FILE] [--json] [--show-titles]
    py -3.13 -m core.eval.reference_eval --from YYYY-MM-DD --to YYYY-MM-DD [--cases FILE] [--json] [--show-titles]

--briefs reads a JSON list of v_briefs_current rows (market, brief_date, payload) exported inside the approved environment.
--from/--to reads them from the warehouse, SELECT only, capped at 64 MiB billed like core/brief/holds_report.py.

Each case is matched against the items of its own market's briefs dated from its week's Monday through the 13 days after
(a pattern spotted late in a week may first reach a brief the next week). An item matches when its title, item_id or
merged item_ids name one of the case's tags, or contain every word of one of its term groups. Per case the best place
any brief put a matching item counts: published (cards or more) over held over moment over absent.

Outcomes:
    positive  caught (published), held (with the hold's rule and reason), moment, or missed (absent from every brief)
    negative  false_publication (published), held, moment, or absent; held, moment and absent are all correct

The two error kinds, positives missed or held and negatives published, are counted separately and never netted. The
bundled cases come from one curator's sheet of one week; their counts are a spot-check, not an accuracy figure for 42.
Output names cases, markets, item_ids, rules and reason codes; titles print only with --show-titles."""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

CASES = Path(__file__).resolve().parent / "fixtures" / "reference_weekly_moments.json"
WINDOW_DAYS = 13
SUBSTRING_MIN = 8
PROJECT = "ogilvy-trends-v2"
AGENT = "intelligence_42_agent"
MAX_BYTES = 64 * 1024 * 1024
BRIEFS = f"""SELECT market, CAST(brief_date AS STRING) brief_date, TO_JSON_STRING(payload) payload
FROM `{PROJECT}.{AGENT}.v_briefs_current` WHERE brief_date BETWEEN @a AND @b ORDER BY brief_date, market"""
RANK = {"published": 3, "held": 2, "moment": 1}
POSITIVE = {"published": "caught", "held": "held", "moment": "moment", None: "missed"}
NEGATIVE = {"published": "false_publication", "held": "held", "moment": "moment", None: "absent"}


def load_cases(path=CASES):
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def _fold(text):
    return re.sub(r"[^0-9a-z]", "", str(text or "").casefold())


def _words(text):
    return " ".join(re.findall(r"[0-9a-z]+", str(text or "").casefold()))


def matches(case, names):
    """True when any of the item's names carries one of the case's tags, or every word of one of its term groups."""
    folded = [_fold(n) for n in names if n]
    tokens = {w for n in names if n for w in _words(n).split()} | set(folded)
    spaced = [f" {_words(n)} " for n in names if n]
    for tag in case["match"].get("tags") or []:
        t = _fold(tag)
        # A short tag must be a whole word or name ("cng" never inside "dancing"); a long one may sit inside a folded
        # hashtag or key ("amapianochallenge" in "amapianochallenge2026").
        if t and (t in tokens or (len(t) >= SUBSTRING_MIN and any(t in f for f in folded))):
            return True
    for group in case["match"].get("terms") or []:
        if group and any(all(f" {_words(w)} " in s for w in group) for s in spaced):
            return True
    return False


def items(payload):
    """(place, names, item_id, title, rule, reason) for every item one market payload shows or holds."""
    payload = json.loads(payload) if isinstance(payload, str) else payload or {}
    for c in (payload.get("cards") or []) + (payload.get("more") or []):
        yield ("published", [c.get("title"), c.get("item_id"), *(c.get("also") or [])], c.get("item_id"),
               c.get("title"), None, None)
    for h in (payload.get("held_back") or {}).get("items") or []:
        yield ("held", [h.get("title"), h.get("item_id"), *(h.get("also") or [])], h.get("item_id"), h.get("title"),
               h.get("rule"), h.get("reason"))
    for m in payload.get("moments") or []:
        ids = list(m.get("item_ids") or [])
        yield ("moment", [m.get("name"), *ids], ids[0] if ids else None, m.get("name"), None, None)


def window(case):
    start = date.fromisoformat(case["week"])
    return start, start + timedelta(days=WINDOW_DAYS)


def score(cases, briefs):
    """One result per case: outcome, the best match (place, brief_date, item_id, title, rule, reason) and the briefs
    it could have appeared in. A case with no brief of its market in its window is not_checked, never missed."""
    out = []
    for case in cases:
        start, end = window(case)
        mine = [b for b in briefs if b["market"] == case["market"]
                and start <= date.fromisoformat(str(b["brief_date"])[:10]) <= end]
        best = None
        for b in mine:
            for place, names, item_id, title, rule, reason in items(b["payload"]):
                if matches(case, names) and (best is None or RANK[place] > RANK[best["place"]]):
                    best = {"place": place, "brief_date": str(b["brief_date"])[:10], "item_id": item_id,
                            "title": title, "rule": rule, "reason": reason}
        table = POSITIVE if case["label"] == "positive" else NEGATIVE
        outcome = "not_checked" if not mine else table[best["place"] if best else None]
        out.append({"id": case["id"], "market": case["market"], "label": case["label"], "kind": case["kind"],
                    "outcome": outcome, "briefs": len(mine), "match": best})
    return out


def summary(results):
    """Counts per label and outcome, with each label's denominator of checked cases."""
    s = {}
    for label in ("positive", "negative"):
        rows = [r for r in results if r["label"] == label]
        checked = [r for r in rows if r["outcome"] != "not_checked"]
        s[label] = {"cases": len(rows), "checked": len(checked), "outcomes": dict(Counter(r["outcome"] for r in rows))}
    return s


def read_briefs(a, b):
    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT, location="US")
    qp = [bigquery.ScalarQueryParameter("a", "DATE", a), bigquery.ScalarQueryParameter("b", "DATE", b)]
    dry = client.query(BRIEFS, job_config=bigquery.QueryJobConfig(query_parameters=qp, dry_run=True,
                                                                  use_query_cache=False))
    if dry.total_bytes_processed > MAX_BYTES:
        raise SystemExit(f"refused: query would read {dry.total_bytes_processed} bytes, over {MAX_BYTES}")
    job = client.query(BRIEFS, job_config=bigquery.QueryJobConfig(query_parameters=qp, maximum_bytes_billed=MAX_BYTES))
    return [dict(r) for r in job.result()]


def report(results, show_titles=False):
    lines = []
    for r in results:
        m = r["match"] or {}
        where = ""
        if m:
            where = f" {m['brief_date']} {m['place']} {m['item_id']}"
            if m["rule"] or m["reason"]:
                where += f" rule={m['rule']} reason={m['reason']}"
            if show_titles:
                where += f" | {m['title']}"
        lines.append(f"{r['id']} {r['market']} {r['label']} {r['kind']}: {r['outcome']} (briefs {r['briefs']}){where}")
    for label, s in summary(results).items():
        counts = ", ".join(f"{k} {v}" for k, v in sorted(s["outcomes"].items()))
        lines.append(f"{label}: {s['checked']} of {s['cases']} checked; {counts}")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--cases", default=str(CASES))
    p.add_argument("--briefs")
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--json", action="store_true")
    p.add_argument("--show-titles", action="store_true")
    args = p.parse_args(argv)
    if args.briefs:
        briefs = json.loads(Path(args.briefs).read_text(encoding="utf-8"))
    elif args.start and args.end:
        briefs = read_briefs(date.fromisoformat(args.start), date.fromisoformat(args.end))
    else:
        p.error("give --briefs FILE or --from and --to")
    results = score(load_cases(args.cases), briefs)
    if args.json:
        if not args.show_titles:
            for r in results:
                if r["match"]:
                    r["match"].pop("title", None)
        print(json.dumps({"results": results, "summary": summary(results)}, indent=1))
    else:
        print(report(results, args.show_titles))
    return 0


if __name__ == "__main__":
    sys.exit(main())
