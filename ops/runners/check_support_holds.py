"""Read-only check of why the brief's support check held items. Runs from anywhere (no repo imports):
    py -3.13 check_support_holds.py 2026-10-03 [brief_run_id]
Needs google-cloud-bigquery and gcloud ADC on this PC. SELECT only, each query capped at 1 GB billed.

Without a run id it reads the latest brief run of the day. Per market, per held item that G1 did not hold, it prints
the hold reason, how many claims the writer wrote (before and after the repair round), what the code checks did to
each claim (cuts, and K5 label lowerings), how many claims passed the support check, each failed claim with its
stored wording, the explanation sentence's own support check, the critic's answer as the payload keeps it, and
where the item's posts are located.

What is stored and what is not (core/brief/job.py _brief, check_reason; core/brief/payload.py _held_item):
claim_checks keeps claim_id, rule, verdict (pass, cut, downgrade, breach), checker and fixed wording only. A held
item's claims are not stored, so its claim text and label are not readable; the support check's own verdict
(supported, partial or unsupported) is stored only as pass or cut, and its reason sentence is not stored. K5 rows
show that a label was lowered, not from what to what. The critic's answer is stored as the market payload's critic
list since 7b47cd24. Model text printed here (the critic's answer) is cut to 160 characters with @handles and
double-quoted spans masked; no handles, post text or secrets are printed."""
import datetime as dt
import json
import re
import sys
from collections import Counter, defaultdict

from google.cloud import bigquery

P = "ogilvy-trends-v2"
AGENT = f"`{P}.intelligence_42_agent`"
CAP = 10**9
MARKETS = ("ZA", "NG", "KE")
REPAIR = "before repair: "
TEXT_MAX = 160
CRITIC_FIELDS = ("non_cultural_explanation", "ruled_out", "news_driven", "local_reaction", "local_why_now", "reason")
_HANDLE = re.compile(r"@[\w.]+")
_QUOTED = re.compile(r"[\"“][^\"“”]*[\"”]")


def q(c, sql, params, label):
    """Rows of one capped SELECT, or None with a one-line note when it fails."""
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=CAP, use_query_cache=True, query_parameters=params)
    try:
        return list(c.query(sql, job_config=cfg).result())
    except Exception as exc:
        print(f"[{label}: query failed ({type(exc).__name__}); its lines show as ?]")
        return None


def model_text(value, handles=()):
    """Model-written text, safe to print: known handles and @handles masked, quoted spans masked, cut to 160."""
    if value is None:
        return None
    s = " ".join(str(value).split())
    for h in sorted({h for h in handles if len(h) >= 3}, key=len, reverse=True):
        s = re.sub(re.escape(h), "[handle]", s, flags=re.I)
    s = _QUOTED.sub('"[quote]"', _HANDLE.sub("@[handle]", s))
    return s if len(s) <= TEXT_MAX else s[:TEXT_MAX - 3] + "..."


def claim_rows(rows):
    """The item's claim_checks rows split into (before repair, after repair), the prefix taken off the reason."""
    before, after = [], []
    for r in rows:
        reason = str(r.reason or "")
        if reason.startswith(REPAIR):
            before.append((r, reason[len(REPAIR):]))
        else:
            after.append((r, reason))
    return before, after


def written(rows):
    """Claim ids the writer wrote in one round: check_answer gives every claim exactly one K1 row."""
    return list(dict.fromkeys(r.claim_id for r, _ in rows if r.rule == "K1" and r.checker == "code" and r.claim_id))


def summarize(rows):
    """One held item's checks as the explanation step ran them (core/brief/explain.py explain_trend)."""
    before, after = claim_rows(rows)
    out = {"before": written(before), "after": written(after), "code_cuts": defaultdict(list),
           "lowered": Counter(), "support": [], "sentence": None, "critic_row": None, "other": []}
    for r, reason in after:
        if r.rule == "K4" and r.checker == "model":
            if r.claim_id:
                out["support"].append((r.claim_id, r.verdict, reason))
            else:
                out["sentence"] = (r.verdict, reason)
        elif r.rule == "critic":
            out["critic_row"] = (r.verdict, reason)
        elif r.rule == "K5" and r.verdict == "downgrade" and r.claim_id:
            out["lowered"][r.claim_id] += 1
        elif r.verdict in ("cut", "breach") and r.claim_id:
            out["code_cuts"][r.claim_id].append(f"{r.rule} {r.verdict}: {reason}")
        elif r.verdict in ("cut", "breach", "downgrade"):
            out["other"].append(f"{r.rule} {r.verdict}: {reason}")
    out["lowered_before"] = sum(1 for r, _ in before if r.rule == "K5" and r.verdict == "downgrade")
    return out


def where_posts(evidence, m):
    """Counts of the pack's posts by location, as the writer and the support check see them."""
    def flags(e):
        return {str(f).lower() for f in e.get("flags") or []}
    located = own = other = unknown = 0
    for e in evidence:
        mk = None if "market_assumed" in flags(e) else (str(e.get("market") or "").upper() or None)
        src = str(e.get("source_market") or "").upper() or None
        if mk == m:
            located += 1
        elif mk is not None:
            other += 1
        elif src == m:
            own += 1
        else:
            unknown += 1
    who = len({str(e.get("handle") or "").strip().lower() for e in evidence if str(e.get("handle") or "").strip()})
    plats = Counter(str(e.get("platform")) for e in evidence)
    return (f"{len(evidence)} posts from {who} creators ({', '.join(f'{k} {v}' for k, v in sorted(plats.items()))});"
            f" located in {m} {located}, own-feed (location unknown, found in {m}'s feeds) {own}, location unknown"
            f" and not from {m}'s feeds {unknown}, other market {other}")


def report(d, rid, payloads, checks):
    lines = []
    for m in MARKETS:
        p = payloads.get(m)
        if p is None:
            lines.append(f"\n== {m}: no briefs row in this run")
            continue
        held = (p.get("held_back") or {}).get("items") or []
        g1 = [x for x in held if x.get("rule") == "G1"]
        rest = [x for x in held if x.get("rule") != "G1"]
        critic = {c.get("item_id"): c for c in p.get("critic") or [] if isinstance(c, dict)}
        lines.append(f"\n== {m}: status {p.get('status')}, cards {len(p.get('cards') or []) + len(p.get('more') or [])}"
                     f", held {len(held)} (G1 {len(g1)}, other {len(rest)})")
        for x in rest:
            iid = x.get("item_id")
            ev = x.get("evidence") or []
            handles = [str(e.get("handle") or "").strip().lstrip("@") for e in ev]
            lines.append(f"\n{m} {x.get('title')}  [{iid}]")
            lines.append(f"  hold: {x.get('rule')} {x.get('reason')}: {x.get('reason_text')}"
                         f" | failed_reason {x.get('failed_reason')}")
            lines.append(f"  posts: {where_posts(ev, m)}")
            rows = None if checks is None else checks.get(f"{rid}:{m}:{iid}", [])
            if rows is None:
                lines.append("  checks: ? (claim_checks not read)")
                continue
            if not rows:
                lines.append("  checks: none (no explanation ran for this item)")
                continue
            s = summarize(rows)
            repaired = f"{len(s['before'])} before repair, then " if s["before"] else "no repair round, "
            low = sum(s["lowered"].values())
            lines.append(f"  claims written: {repaired}{len(s['after'])} | K5 label lowered {low}"
                         f" times after repair on {len(s['lowered'])} claims ({s['lowered_before']} before repair)")
            for cid in s["after"]:
                if s["code_cuts"].get(cid):
                    lines.append(f"    {cid} cut by code before support: {'; '.join(s['code_cuts'][cid])}")
            passed = sum(1 for _, v, _ in s["support"] if v == "pass")
            lines.append(f"  support check: {len(s['support'])} claims checked, {passed} passed (an explanation needs"
                         f" at least 2, including every claim the sentence rests on)")
            for cid, v, reason in s["support"]:
                if v != "pass":
                    low = f"lowered x{s['lowered'][cid]}" if s["lowered"].get(cid) else "not lowered"
                    lines.append(f"    {cid} {v}: label not stored, K5 {low} | checker reason not stored; stored:"
                                 f" {reason}")
            sentence = "not reached" if s["sentence"] is None else f"{s['sentence'][0]}: {s['sentence'][1]}"
            lines.append(f"  sentence support check: {sentence}")
            for o in s["other"]:
                lines.append(f"    other: {o}")
            cr = critic.get(iid)
            if s["critic_row"] is None and cr is None:
                lines.append("  critic: not reached")
            else:
                if s["critic_row"] is not None:
                    lines.append(f"  critic row: {s['critic_row'][0]}: {s['critic_row'][1]}")
                if cr is None:
                    lines.append("  critic answer: not in the payload (run before 7b47cd24, or not reached)")
                else:
                    flags = ", ".join(f"{k} {cr.get(k)}" for k in CRITIC_FIELDS[1:5])
                    lines.append(f"  critic answer: {flags}")
                    lines.append(f"    named: {model_text(cr.get('non_cultural_explanation'), handles)}")
                    lines.append(f"    reason: {model_text(cr.get('reason'), handles)}")
    return lines


def main(argv, c=None):
    c = c or bigquery.Client(project=P)
    d = dt.date.fromisoformat(argv[1] if len(argv) > 1 else "2026-10-03")
    D = [bigquery.ScalarQueryParameter("d", "DATE", d)]
    runs = q(c, f"""
SELECT b.run_id, MIN(b.published_at) published_at, STRING_AGG(CONCAT(b.market, ' ', b.status), ', '
         ORDER BY b.market) markets, ANY_VALUE(r.status) run_status
FROM {AGENT}.briefs b
LEFT JOIN {AGENT}.runs r ON r.run_id = b.run_id AND r.run_date = @d AND r.stage = 'brief'
WHERE b.brief_date = @d
GROUP BY b.run_id ORDER BY published_at""", D, "brief runs") or []
    print(f"== brief runs for {d}")
    for r in runs:
        print(f"  {r.run_id}  published {r.published_at:%H:%M:%S} UTC  run {r.run_status}  {r.markets}")
    if not runs:
        print("no brief rows for that date")
        return 1
    rid = argv[2] if len(argv) > 2 else runs[-1].run_id
    print(f"== reading run {rid}")
    R = [bigquery.ScalarQueryParameter("rid", "STRING", rid)]
    payloads = {r.market: json.loads(r.p) for r in q(c, f"""
SELECT market, TO_JSON_STRING(payload) p FROM {AGENT}.briefs
WHERE brief_date = @d AND run_id = @rid""", D + R, "briefs payload") or []}
    rows = q(c, f"""
SELECT answer_or_brief_id, claim_id, rule, verdict, checker, reason FROM {AGENT}.claim_checks
WHERE run_id = @rid""", R, "claim_checks")
    checks = None
    if rows is not None:
        checks = defaultdict(list)
        for r in rows:
            checks[r.answer_or_brief_id].append(r)
    for line in report(d, rid, payloads, checks):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
