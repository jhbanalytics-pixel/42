"""The intraday pulse, collect side (FEATURES.md row 30a; ENGINE.md section 3, "Later").

The same collector on a second schedule, every 2 to 4 hours, re-reading the day's hot items so lane
L2 can call a Breaking state. The Breaking rule itself belongs to detect; this module only collects.

Hot items. Nothing in DATA.md or TRUST.md defines "hot" for the pulse, so it borrows the expansion rule
of DATA.md section 4: per market, the latest good detect run's item_state rows (from the last 3 days) in
New to 42, Spike, Emerging, Rising or Peaking with worth_pct of 0.8 or more, and also eligible (not held
back as likely coordinated or not local), ranked by worth_pct, top HOT_PER_MARKET. HOT_SQL reads them
with cultural_map's current label and canonical key.

Calls, in this order and all through the SocialCrawl client (rule 3) with the same-day cache off, since
the morning's identical calls would otherwise be served back from raw_responses:
- a running total for each hot hashtag (tiktok/hashtag) and TikTok sound (tiktok/song), markets
  interleaved by rank and each item once. The key sent is the one that maps back to the hot item's id
  through item_id_fn, so parse writes the total on that item. Other kinds have no counter route and are
  recorded no_route. Any label, key or query that gdelt.blocked() flags is never sent (rule 1);
- one local feed pull per market (tiktok/trending feed=local), whose pull_seq continues from the last
  stored pull of that list (writers.last_pulls), so the rank series runs on through the day.
Responses are parsed by core/collect/parse.py into posts, post_observations and item_counter_daily rows:
unit total counters that writers.deltas can turn into deltas, and rank rows with pull_seq.

Cap. The pulse spends only from its own share, "pulse", with the daily cap PULSE_DAILY from
core/config/caps.yaml, which mirrors docs/full-42/SETUP.md (60 credits a day); a missing cap reads as
0 and the pulse makes no call. With a cap, the day's pulse spend is read from the credit ledger at the
start, each call must fit under the cap at its hold, and what the client charged (the vendor's figure
when it reports more than the list price) is added after it; once the cap is reached the pulse stops.

Window. The pulse never starts or calls inside the 02:00 to 06:30 SAST chain window, and never after
22:30 or before 02:00 SAST, when a market's day changes under it (KE at 23:00 SAST, NG at 01:00 SAST).
It runs from 06:30 to 22:30 SAST; a pulse that reaches 22:30 stops.

Nothing here writes to BigQuery: run() returns the rows and one record per planned call, and the job
that wires the pulse appends them. The client appends raw_responses and the credit ledger, as it does
for every call.

    py -3.13 -m core.collect.pulse --plan   one pulse's calls and holds, no network
"""

import argparse
import math
import sys
from dataclasses import dataclass, replace
from datetime import date, time, timedelta

from core.collect import writers
from core.collect.gdelt import blocked
from core.collect.parse import GLOBAL, ROUTES, _protocol, parse
from core.collect.socialcrawl_client import PRICED, SAST, Refused, load_caps, quote_for

PULSE_SHARE = "pulse"
CAP_KEY = "PULSE_DAILY"
MARKETS = ("ZA", "NG", "KE")
HOT_STATES = ("new_to_42", "spike", "emerging", "rising", "peaking")
MIN_WORTH = 0.8
HOT_PER_MARKET = 5
HOT_DAYS = 3                              # the latest item_state day may be today, yesterday or the day before
CHAIN_WINDOW = (time(2, 0), time(6, 30))  # collect 02:00 to publish 06:30 SAST
DAY_END = time(22, 30)
FEED = "tiktok/trending"
COUNTER_ROUTES = {"hashtag": ("tiktok/hashtag", "hashtag"), "sound": ("tiktok/song", "clipId")}
OK = ("ok", "empty", "cached")
STOP = ("cap_reached", "insufficient_credits", "balance_floor")

HOT_SQL = f"""SELECT s.metric_date, s.market, s.item_id, s.kind, s.state, s.eligible, s.worth_pct,
  m.label, m.canonical_key
FROM `{writers.table("item_state")}` s
JOIN (
  SELECT r.run_date, ARRAY_AGG(r.run_id ORDER BY r.finished_at DESC LIMIT 1)[OFFSET(0)] AS run_id
  FROM `{writers.table("runs", writers.AGENT)}` r
  WHERE r.stage = 'detect' AND r.status = 'ok'
    AND r.run_date BETWEEN DATE_SUB(@d, INTERVAL {HOT_DAYS - 1} DAY) AND @d
  GROUP BY r.run_date) g ON g.run_date = s.metric_date AND g.run_id = s.run_id
LEFT JOIN `{writers.table("cultural_map")}` m ON m.item_id = s.item_id AND m.valid_to IS NULL
WHERE s.metric_date BETWEEN DATE_SUB(@d, INTERVAL {HOT_DAYS - 1} DAY) AND @d
  AND s.state IN UNNEST(@states) AND s.eligible AND s.worth_pct >= @min_worth"""


class OutsideWindow(Exception):
    pass


@dataclass
class Call:
    route: str
    params: dict
    market: str
    lane: str
    item_id: str | None = None
    seed_key: str | None = None
    kind: str | None = None
    pull_seq: int | None = None

    @property
    def method(self):
        return PRICED[self.route].method

    def hold(self):
        return quote_for(self.route, self.method, self.params)


def window_reason(moment):
    """Why a pulse may not run at moment, or ""."""
    now = moment.astimezone(SAST).time()
    if CHAIN_WINDOW[0] <= now <= CHAIN_WINDOW[1]:
        return "inside the morning chain window, 02:00 to 06:30 SAST"
    if now > DAY_END or now < CHAIN_WINDOW[0]:
        return "after 22:30 or before 02:00 SAST, when a market's day changes (KE 23:00, NG 01:00 SAST)"
    return ""


def pulse_cap(caps=None):
    """The pulse share's daily cap from caps.yaml; 0 while SETUP.md sets none."""
    value = (load_caps() if caps is None else caps).get(CAP_KEY, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{CAP_KEY} must be a whole number of credits of 0 or more, not {value!r}")
    return value


def read_hot(bq, day):
    """HOT_SQL for day: a parameterised read of item_state, runs and cultural_map."""
    from google.cloud import bigquery

    rows = writers._query(bq, HOT_SQL, [
        bigquery.ScalarQueryParameter("d", "DATE", day),
        bigquery.ArrayQueryParameter("states", "STRING", list(HOT_STATES)),
        bigquery.ScalarQueryParameter("min_worth", "FLOAT64", MIN_WORTH)])
    return [dict(r.items()) for r in rows]


def _day(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def hot_items(rows, day, n=HOT_PER_MARKET):
    """{market: rows} of each market's hot items on its latest item_state day in the last HOT_DAYS days."""
    oldest = day - timedelta(days=HOT_DAYS - 1)
    fresh = [r for r in rows if r.get("market") in MARKETS and oldest <= _day(r["metric_date"]) <= day]
    out = {}
    for market in MARKETS:
        mine = [r for r in fresh if r["market"] == market]
        latest = max((_day(r["metric_date"]) for r in mine), default=None)
        picked = {}
        for r in mine:
            worth = r.get("worth_pct")
            if (_day(r["metric_date"]) == latest and r.get("state") in HOT_STATES and r.get("eligible") is True
                    and isinstance(worth, (int, float)) and worth >= MIN_WORTH):
                picked.setdefault(r["item_id"], r)
        out[market] = sorted(picked.values(), key=lambda r: (-r["worth_pct"], r["item_id"]))[:n]
    return out


def _key(item, item_id_fn):
    """The counter route's key that maps back to the item's id, from its canonical key or label, or None."""
    canonical = item.get("canonical_key")
    tries = [canonical, str(canonical).split(":", 1)[1] if canonical and ":" in str(canonical) else None,
             item.get("label")]
    for raw in tries:
        key = str(raw).strip().lstrip("#") if raw not in (None, "") else ""
        if not key or any(ch.isspace() for ch in key):
            continue
        try:
            if item_id_fn(item["kind"], key, "tiktok") == item["item_id"]:
                return key
        except ValueError:
            continue
    return None


def _skip(item, market, status, reason, key=None):
    return {"route": None, "market": market, "item_id": item.get("item_id"), "kind": item.get("kind"),
            "seed_key": key, "pull_seq": None, "status": status, "reason": reason, "credits_charged": 0}


def pulse_calls(hot, item_id_fn):
    """(calls, skipped): the counter re-reads, markets interleaved by rank, then one feed pull per market."""
    calls, skipped, seen = [], [], set()
    for rank in range(max((len(v) for v in hot.values()), default=0)):
        for market in MARKETS:
            if rank >= len(hot.get(market, [])):
                continue
            item = hot[market][rank]
            if item["item_id"] in seen:
                continue
            seen.add(item["item_id"])
            if any(blocked(item[f]) for f in ("label", "canonical_key") if item.get(f) not in (None, "")):
                skipped.append(_skip(item, market, "rule_1", "label or key carries an age lens (rule 1)"))
                continue
            if item.get("kind") not in COUNTER_ROUTES:
                skipped.append(_skip(item, market, "no_route", f"no counter route for kind {item.get('kind')}"))
                continue
            key = _key(item, item_id_fn)
            if key is None:
                skipped.append(_skip(item, market, "no_key", "no key maps back to the item id"))
                continue
            if blocked(key):
                skipped.append(_skip(item, market, "rule_1", "query carries an age lens (rule 1)"))
                continue
            route, name = COUNTER_ROUTES[item["kind"]]
            calls.append(Call(route, {name: key}, GLOBAL, "watchlist", item["item_id"], key, item["kind"]))
    calls += [Call(FEED, {"region": m, "feed": "local"}, m, "sweep") for m in MARKETS]
    return calls, skipped


def _feed_key(call):
    return (call.market, ROUTES[call.route][2], _protocol(call.route, call.params, ROUTES[call.route][4]))


def run(run_id, *, rows, client, clock, item_id_fn, geo_fn, last_pulls=None, caps=None):
    """One pulse. rows are read_hot's item_state rows, client a SocialCrawlClient on the pulse share, last_pulls
    writers.last_pulls. Raises OutsideWindow before anything when the window is shut. Writes nothing."""
    from core.collect.job import safe_geo

    now = clock()
    why = window_reason(now)
    if why:
        raise OutsideWindow(why)
    day = now.astimezone(SAST).date()
    cap = pulse_cap(caps)
    calls, skipped = pulse_calls(hot_items(rows, day), item_id_fn)
    out = {"run_id": run_id, "posts": [], "observations": [], "counters": [], "records": list(skipped),
           "credits": 0, "stopped": None}

    def record(call, status, reason="", charged=0):
        out["records"].append({"route": call.route, "market": call.market, "item_id": call.item_id,
                               "kind": call.kind, "seed_key": call.seed_key, "pull_seq": call.pull_seq,
                               "status": status, "reason": reason, "credits_charged": charged})

    if cap == 0:
        for call in calls:
            record(call, "no_cap", f"{CAP_KEY} is not set in docs/full-42/SETUP.md, so the pulse spends nothing")
        return out
    if client is None or getattr(client, "share", None) != PULSE_SHARE:
        raise ValueError(f"the pulse spends only from the {PULSE_SHARE} share, not {getattr(client, 'share', None)!r}")
    spent = float(client.ledger.spent(day, day, job=PULSE_SHARE))
    if not math.isfinite(spent):
        raise ValueError("the ledger's pulse spend today is not a finite number")
    halt = ("not_made", f"pulse cap reached: {spent:g} charged today against {cap}") if spent >= cap else None
    out["stopped"] = halt[1] if halt else None
    geo, pulls = safe_geo(geo_fn), dict(last_pulls or {})

    for call in calls:
        if halt:
            record(call, halt[0], halt[1])
            continue
        why = window_reason(clock())
        if why:
            halt = ("window_closed", why)
            out["stopped"] = why
            record(call, *halt)
            continue
        try:
            hold = call.hold()
        except Refused:
            hold = 0  # the client refuses it and says why
        if spent + hold > cap:
            record(call, "over_pulse_cap", f"{spent:g} charged today plus {hold} held is over {cap}")
            continue
        if call.route == FEED:
            key = _feed_key(call)
            pulls[key] = pulls.get(key, 0) + 1
            call = replace(call, pull_seq=pulls[key])
        result = client.call(call.route, call.params, method=call.method, market=call.market, item_id=call.item_id,
                             seed_key=call.seed_key, lane=call.lane, use_cache=False)
        fetched = clock()
        charged = result.credits_charged or 0
        spent += charged
        out["credits"] += charged
        if result.status in STOP:
            halt = ("not_made", result.status)
        elif spent >= cap:
            halt = ("not_made", f"pulse cap reached: {spent:g} charged today against {cap}")
        if halt:
            out["stopped"] = halt[1]
        if result.status in OK and result.body is not None:
            parsed = parse(call.route, call.params, call.market, result.body, fetched, run_id, item_id_fn=item_id_fn,
                           geo_fn=geo, lane=call.lane, seed_key=call.seed_key, pull_seq=call.pull_seq)
            for name in ("posts", "observations", "counters"):
                out[name] += parsed[name]
        record(call, result.status, result.reason, charged)
    return out


# The plan printout

def print_plan(caps=None):
    cap = pulse_cap(caps)
    counter = max(quote_for(route, "GET", {name: "x"}) for route, name in COUNTER_ROUTES.values())
    feed = quote_for(FEED, "GET", {"region": "ZA", "feed": "local"})
    print(f"pulse plan: up to {HOT_PER_MARKET} hot items per market (latest item_state, states "
          f"{', '.join(HOT_STATES)}, worth_pct {MIN_WORTH} or more, eligible), then one local feed pull per market")
    print(f"runs from 06:30 to 22:30 SAST only, never inside the 02:00 to 06:30 chain window; "
          f"share {PULSE_SHARE}, cap {cap} credits a day ({CAP_KEY})")
    total = 0
    for market in MARKETS:
        print(market)
        for i in range(1, HOT_PER_MARKET + 1):
            print(f"  tiktok/hashtag or tiktok/song  GLOBAL  {counter:>3}  <{market} hot item {i}>, when a hashtag or sound")
        print(f"  {FEED}  {market}  {feed:>3}  region={market} feed=local, pull_seq continues from the last stored pull")
        total += HOT_PER_MARKET * counter + feed
    print(f"at most {total} credits a pulse ({len(MARKETS) * HOT_PER_MARKET} counter reads, each item once)")
    if cap == 0:
        print(f"{CAP_KEY} is not set in docs/full-42/SETUP.md, so the cap is 0 and no call is made")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", help="print one pulse's calls and holds; no network")
    args = parser.parse_args(argv)
    if not args.plan:
        parser.error("the pulse runs inside its job once that is wired; --plan prints one pulse's calls")
    print_plan()
    return 0


if __name__ == "__main__":
    sys.exit(main())
