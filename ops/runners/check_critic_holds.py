"""Read-only check of why the brief's critic held items. Runs from anywhere (no repo imports):
    py -3.13 check_critic_holds.py YYYY-MM-DD [brief_run_id]
Needs google-cloud-bigquery and gcloud ADC on this PC. SELECT only, each query capped at 1 GB billed.

The date is required. Without a run id it reads the latest brief run of the day (the latest published, then the
highest run id). A read that fails stops the report or shows ? for its part; it is never printed as an empty result. Per market per item (cards and held back) it prints the
status, the hold reason, the critic's verdict as claim_checks.reason records it, and the pack the critic saw: the
posts, where they are located, their flags, how many were seen only by the confirm search, how many have an
enrichment row (the only source of the sponsored flag) and how many carry a vendor sponsored label (never read by
the brief).

What is stored and what is not: claim_checks.reason keeps fixed wording only (core/brief/job.py check_reason), so
ruled_out, local_why_now and whether the news-driven or the event-driven path was met are read back from that
wording. The critic's own named explanation and reason sentence, and news_driven, scheduled_event and
local_reaction one by one, are not stored anywhere. An item whose first draft was cut on its why-now alone has two
critic rows: the first draft's, marked "before repair: ", and the final draft's. The verdict shown is the final
draft's; the first draft's is printed apart.
Prints titles, counts and fixed wording only: no handles, post text or model text."""
import datetime as dt
import json
import sys
from collections import Counter, defaultdict

from google.cloud import bigquery

P = "ogilvy-trends-v2"
CORE, AGENT = f"`{P}.intelligence_42_core`", f"`{P}.intelligence_42_agent`"
CAP = 10**9
MARKETS = ("ZA", "NG", "KE")
REPAIR = "before repair: "
NEWS_PASS = "Critic: news-driven, local creators react in their own words"
EVENT_PASS = "Critic: event-driven, local creators react in their own words"
RULED_OUT = "Critic: the simpler explanation was ruled out"
NOT_RULED_OUT = "Critic: a simpler explanation was not ruled out"
LWN_ONLY = "Critic: local why-now not shown"
NO_LWN = "; local why-now not shown"

c = None


def q(sql, params, label):
    """Rows of one capped SELECT, or None with a one-line note when it fails."""
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=CAP, use_query_cache=True, query_parameters=params)
    try:
        return list(c.query(sql, job_config=cfg).result())
    except Exception as exc:
        print(f"[{label}: query failed ({type(exc).__name__}); its columns show as ?]")
        return None


def critic_fields(reason):
    """(verdict, ruled_out, news_path_met, event_path_met, local_why_now, menu) from a critic row's fixed wording
    (the row's "before repair: " prefix already taken off). A path is met when the critic found the news or the
    scheduled event, local_reaction, and at least 2 reacting local creators, as one bit."""
    r = str(reason or "")
    menu = None
    head = next((h for h in (NEWS_PASS, EVENT_PASS, RULED_OUT) if r.startswith(h)), None)
    if head is not None:
        menu = r[len(head) + 2:] or None if r.startswith(head + ": ") else None
        return "pass", head == RULED_OUT, head == NEWS_PASS, head == EVENT_PASS, True, menu
    if r == LWN_ONLY:
        return ("cut", "true, or a news or event path met", "true, or ruled out or event path",
                "true, or ruled out or news path", False, None)
    if r.startswith(NOT_RULED_OUT):
        rest = r[len(NOT_RULED_OUT):]
        lwn = not rest.endswith(NO_LWN)
        rest = rest[:-len(NO_LWN)] if not lwn else rest
        menu = rest[2:] if rest.startswith(": ") else None
        return "cut", False, False, False, lwn, menu
    return "?", "?", "?", "?", "?", None


def critic_line(prefix, reason):
    v, ro, news, event, lwn, menu = critic_fields(reason)
    return (f"{prefix}: verdict {v} | ruled_out {ro} | news path met (news_driven and local_reaction and 2+ "
            f"reacting) {news} | event path met (scheduled_event and local_reaction and 2+ reacting) {event} | "
            f"local_why_now {lwn} | named, as menu item: {menu or 'unmatched or ambiguous'}"
            f" | named text, news_driven, scheduled_event, local_reaction, reason: not stored")


USAGE = "usage: py -3.13 check_critic_holds.py YYYY-MM-DD [brief_run_id]"
try:
    d = dt.date.fromisoformat(sys.argv[1])
except (IndexError, ValueError):
    sys.exit(USAGE)
c = bigquery.Client(project=P)
D = [bigquery.ScalarQueryParameter("d", "DATE", d)]

# One status per run: the run's newest non-duplicate row, a finished row before an unfinished one at the same time.
runs = q(f"""
SELECT b.run_id, MIN(b.published_at) published_at,
  STRING_AGG(DISTINCT CONCAT(b.market, ' ', b.status), ', ' ORDER BY CONCAT(b.market, ' ', b.status)) markets,
  MIN(r.status) run_status
FROM {AGENT}.briefs b
LEFT JOIN (
  SELECT run_id, status FROM {AGENT}.runs
  WHERE run_date = @d AND stage = 'brief' AND status != 'skipped_duplicate'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id
    ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC, status) = 1) r
  ON r.run_id = b.run_id
WHERE b.brief_date = @d
GROUP BY b.run_id ORDER BY published_at, b.run_id""", D, "briefs runs")
if runs is None:
    sys.exit(f"brief runs read failed for {d}; nothing to report")
print(f"== brief runs for {d}")
for r in runs:
    print(f"  {r.run_id}  published {r.published_at:%H:%M:%S} UTC  run {r.run_status}  {r.markets}")
if not runs:
    sys.exit("no brief rows for that date")
rid = sys.argv[2] if len(sys.argv) > 2 else runs[-1].run_id
print(f"== reading run {rid}")
R = D + [bigquery.ScalarQueryParameter("rid", "STRING", rid)]

payload_rows = q(f"""
SELECT market, TO_JSON_STRING(payload) p FROM {AGENT}.briefs
WHERE brief_date = @d AND run_id = @rid""", R, "briefs payload")
if payload_rows is None:
    sys.exit(f"briefs payload read failed for run {rid}; nothing to report")
payloads = {r.market: json.loads(r.p) for r in payload_rows}

check_rows = q(f"""
SELECT answer_or_brief_id, claim_id, rule, verdict, checker, reason FROM {AGENT}.claim_checks
WHERE run_id = @rid""", [R[1]], "claim_checks")
checks = None
if check_rows is not None:
    checks = defaultdict(list)
    for r in check_rows:
        checks[r.answer_or_brief_id].append(r)

items = []  # (market, kind, item dict)
for m in MARKETS:
    p = payloads.get(m) or {}
    for k in ("cards", "more"):
        items += [(m, "card", x) for x in p.get(k) or []]
    items += [(m, "held", x) for x in (p.get("held_back") or {}).get("items") or []]
ids = sorted({e["id"] for _, _, x in items for e in x.get("evidence") or [] if e.get("id")})
I = [bigquery.ArrayQueryParameter("ids", "STRING", ids)]

lanes = q(f"""
SELECT po.post_id, po.market, LOGICAL_OR(IFNULL(po.lane, '') != 'confirm') beyond_confirm,
  LOGICAL_OR(po.lane_class IN ('unbiased_rank', 'panel')) measured
FROM {CORE}.post_observations po
WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d AND po.post_id IN UNNEST(@ids)
  AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
GROUP BY po.post_id, po.market""", D + I, "observation lanes")
lane = None if lanes is None else {(r.post_id, r.market): r for r in lanes}

enr = q(f"""
SELECT post_id, LOGICAL_OR(tone IS NOT NULL) enriched, LOGICAL_OR(IFNULL(sponsored, FALSE)) sponsored
FROM {CORE}.post_enrichment WHERE post_id IN UNNEST(@ids) GROUP BY post_id""", I, "post_enrichment")
enr = None if enr is None else {r.post_id: r for r in enr}

ven = q(f"""
SELECT post_id, LOWER(TO_JSON_STRING(JSON_QUERY(vendor_labels, '$.sponsored'))) v
FROM {CORE}.posts
WHERE post_date BETWEEN DATE_SUB(@d, INTERVAL 30 DAY) AND DATE_ADD(@d, INTERVAL 1 DAY) AND post_id IN UNNEST(@ids)""", D + I, "posts vendor_labels")
ven = None if ven is None else {r.post_id: r.v for r in ven}


def n(ok, s):
    return "?" if ok is None else s


for m in MARKETS:
    rows = [(k, x) for mm, k, x in items if mm == m]
    print(f"\n== {m}: {sum(k == 'card' for k, _ in rows)} cards, {sum(k == 'held' for k, _ in rows)} held "
          f"(status {(payloads.get(m) or {}).get('status')})")
    for kind, x in rows:
        if kind == "card":
            head = (f"card, {x.get('explanation_status')}, news_driven {x.get('news_driven')}"
                    f", failed_reason {x.get('failed_reason')}")
        else:
            head = f"held {x.get('rule')} {x.get('reason')}: {x.get('reason_text')} | failed_reason {x.get('failed_reason')}"
        print(f"\n{m} {x.get('title')}  [{x.get('item_id')}]\n  {head}")
        rows_ = None if checks is None else list(checks.get(f"{rid}:{m}:{x.get('item_id')}", []))
        crit_all = [r for r in rows_ or [] if r.rule == "critic"]
        crit = [r for r in crit_all if not str(r.reason or "").startswith(REPAIR)]
        first = sorted({str(r.reason)[len(REPAIR):] for r in crit_all if str(r.reason or "").startswith(REPAIR)})
        final = sorted({str(r.reason or "") for r in crit})
        if rows_ is None:
            print("  critic: ? (claim_checks read failed, so the critic's row is unknown)")
        elif len(final) == 1:
            print(critic_line("  critic", final[0]))
            print(f"  critic reason (fixed wording): {final[0]}")
        elif final:
            print(f"  critic: {len(final)} different final-draft critic rows, none taken for the verdict")
            for reason in final:
                print(f"  critic reason (fixed wording): {reason}")
        elif first:
            print("  critic: no final-draft critic row (only the first draft's critic row was stored)")
        else:
            print("  critic: not reached (no critic row for this item in this run)")
        for reason in first:
            print(critic_line("  first-draft critic (before repair)", reason))
            print(f"  first-draft critic reason (fixed wording): {reason}")
        cuts = Counter(f"{r.rule} {r.reason}" for r in rows_ or [] if r.verdict == "cut" and r.rule not in ("critic", "title")
                       and not str(r.reason or "").startswith(REPAIR))
        before = sum(1 for r in rows_ or [] if str(r.reason or "").startswith(REPAIR))
        if cuts or before:
            print(f"  other cuts after repair: {'; '.join(f'{k} x{v}' for k, v in cuts.items()) or 'none'}"
                  f" ({before} before-repair rows)")
        ev = x.get("evidence") or []
        flags = Counter(f for e in ev for f in e.get("flags") or [])
        anyf = sum(1 for e in ev if e.get("flags"))
        nonloc = sum(1 for e in ev if set(e.get("flags") or []) - {"market_assumed"})
        located = sum(1 for e in ev if e.get("market") == m)
        own = sum(1 for e in ev if e.get("market") is None and e.get("source_market") == m)
        other = sum(1 for e in ev if e.get("market") not in (None, m))
        unknown = len(ev) - located - own - other
        who = len({str(e.get("handle") or "").strip().lower() for e in ev if str(e.get("handle") or "").strip()})
        plats = Counter(e.get("platform") for e in ev)
        print(f"  posts {'the critic saw' if crit_all else 'in the pack'}: {len(ev)} from {who} creators "
              f"({', '.join(f'{k} {v}' for k, v in sorted(plats.items(), key=str))}); located in {m} {located}, "
              f"own-feed {own}, unknown elsewhere {unknown}, other market {other}")
        print(f"  flags: any {anyf}, any besides market_assumed {nonloc}; "
              f"{', '.join(f'{k} {v}' for k, v in sorted(flags.items())) or 'none'}")
        conf = None if lane is None else sum(1 for e in ev if (e["id"], m) in lane and not lane[(e["id"], m)].beyond_confirm)
        conf_nl = None if lane is None else sum(1 for e in ev if (e["id"], m) in lane
                                                and not lane[(e["id"], m)].beyond_confirm and e.get("market") != m
                                                and e.get("source_market") != m)
        meas = None if lane is None else sum(1 for e in ev if (e["id"], m) in lane and lane[(e["id"], m)].measured)
        en = None if enr is None else sum(1 for e in ev if e["id"] in enr and enr[e["id"]].enriched)
        vs = None if ven is None else sum(1 for e in ev if ven.get(e["id"]) not in (None, "null"))
        vt = None if ven is None else sum(1 for e in ev if ven.get(e["id"]) == "true")
        print(f"  lanes: confirm-only {n(conf, conf)} (of those not local {n(conf_nl, conf_nl)}), measured "
              f"{n(meas, meas)} | enriched {n(en, en)} of {len(ev)} (sponsored is read only from enrichment) | "
              f"vendor sponsored label present {n(vs, vs)}, true {n(vt, vt)}")
