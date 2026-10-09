"""The nightly SocialCrawl credit reconciliation and balance runway check (docs/full-42/SETUP.md, "Cost and
credit guards"). The Cloud Run job f42-reconcile runs it as f42-collector at 23:30 SAST.

For the run date (yesterday in SAST, or RUN_DATE) it reads that day's credit_ledger rows with one
parameterised SELECT and the vendor's credits/transactions, page by page with the cursor, until a
transaction older than the day's start in SAST comes back or the history ends. Both routes are free and
go through the SocialCrawl client on the reserve share, so each page is ledgered at 0 credits, and the
balance floor does not refuse them, so the run still reads both when the balance is under it. Rows pair
by request id where both carry one, else by route and the nearest time within MATCH_WINDOW. The run
reports the ledger total, the vendor total, the difference and the rows either side could not pair.

It then reads credits/balance once and divides the credits above BALANCE_FLOOR by the ledger's trailing
30 day mean daily spend to get the days of runway left.

Two ERROR lines feed the log-based alerts, each "42 ALERT <name>: <reason>": "42 ALERT reconcile:" when
the absolute difference is over the larger of 2 credits and 2% of the vendor total, or the vendor
history could not be read for the whole day, and "42 ALERT credits_low:" when the runway is under 30
days. Every figure goes into the counts of one runs row with stage reconcile. reconcile is a side stage
of the chain: chain.begin() writes its running row and raises AlreadyDone when a run for the date is
already ok or still live, so a Scheduler retry exits 0 without reading anything, and chain.finish()
writes the final row. It starts no other job. Nothing else is written here; the client appends its own
credit_ledger and raw_responses rows.

    py -3.13 -m core.collect.reconcile --plan   what it would read and compare, no network
    python -m core.collect.reconcile            the Cloud Run job f42-reconcile
"""

import argparse
import json
import logging
import math
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

from core.collect import chain
from core.collect.socialcrawl_client import load_caps
from core.collect.stores import DATASET, BigQueryLedgerStore

log = logging.getLogger(__name__)

STAGE = "reconcile"
SHARE = "reserve"          # the client needs a share; both routes are free, so none of it is spent
PAGE_LIMIT = 50            # the page size the task 0.5 probe used
MAX_PAGES = 200            # 10,000 transactions: well over two days of the engine's calls
MATCH_WINDOW = timedelta(minutes=5)
ALERT_CREDITS = 2
ALERT_SHARE = 0.02
RUNWAY_DAYS = 30
TRAILING_DAYS = 30
SAMPLE = 20                # unmatched rows listed in counts, each side
READ_OK = ("ok", "empty", "cached")
TOP_UPS = frozenset({"purchase", "topup", "top_up", "top-up", "grant", "bonus", "subscription"})
REFUNDS = frozenset({"refund", "refunded"})

LEDGER_SQL = ("SELECT logged_at, run_id, job, route, calls, credits_charged, cache_hit "
              "FROM `{table}` WHERE trend_date = @day")


class LedgerDay(BigQueryLedgerStore):
    """credit_ledger with the one extra read reconcile needs: a day's rows."""

    def day_rows(self, day):
        from google.cloud import bigquery

        rows = self._query(LEDGER_SQL, [bigquery.ScalarQueryParameter("day", "DATE", day)])
        return [dict(r.items()) for r in rows]


class JsonFormatter(logging.Formatter):
    """One JSON object a line, so Cloud Logging reads the severity and the message starts with 42 ALERT."""

    def format(self, record):
        entry = {"severity": record.levelname, "message": record.getMessage()}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def run_day(env, now):
    """RUN_DATE if set, else yesterday in Africa/Johannesburg."""
    if env.get("RUN_DATE"):
        return date.fromisoformat(env["RUN_DATE"])
    return now.astimezone(chain.SAST).date() - timedelta(days=1)


def window(day):
    """The run date's SAST day as UTC instants, start inclusive and end exclusive."""
    start = datetime.combine(day, time(0), chain.SAST).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _instant(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _whole(value):
    return int(value) if float(value).is_integer() else round(value, 4)


def vendor_route(txn):
    """The ledger's platform/endpoint route for a vendor transaction."""
    platform = str(txn.get("platform") or "").strip().strip("/")
    endpoint = str(txn.get("endpoint") or "").strip().strip("/")
    if endpoint.lower().startswith("v1/"):
        endpoint = endpoint[3:]
    if not platform or endpoint.startswith(platform + "/"):
        return endpoint
    return f"{platform}/{endpoint}"


def vendor_spend(txn):
    """Credits a transaction spent, whatever sign the vendor gives amount: a refund is negative, a top-up
    or an unreadable amount is None."""
    amount = _number(txn.get("amount"))
    kind = str(txn.get("type") or "").strip().lower()
    if amount is None or kind in TOP_UPS:
        return None
    return -abs(amount) if kind in REFUNDS else abs(amount)


def match(ledger, vendor):
    """Pair ledger rows with vendor rows: request id first, then the same route at the nearest time within
    MATCH_WINDOW. Each row is {route, at, credits}, plus request_id where it has one and id on vendor rows."""
    left = list(vendor)
    pairs, by_request_id, unmatched = [], 0, []
    ids = {v["request_id"]: v for v in left if v.get("request_id")}
    rest = []
    for row in ledger:
        hit = ids.pop(row["request_id"], None) if row.get("request_id") else None
        if hit is not None:
            left.remove(hit)
            pairs.append((row, hit))
            by_request_id += 1
        else:
            rest.append(row)
    for row in sorted(rest, key=lambda r: r["at"]):
        near = [v for v in left if v["route"] == row["route"] and abs(v["at"] - row["at"]) <= MATCH_WINDOW]
        if not near:
            unmatched.append(row)
            continue
        hit = min(near, key=lambda v: abs(v["at"] - row["at"]))
        left.remove(hit)
        pairs.append((row, hit))
    mismatches = [{"route": r["route"], "ledger": r["credits"], "vendor": v["credits"], "vendor_id": v.get("id")}
                  for r, v in pairs if r["credits"] != v["credits"]]
    return {"matched": len(pairs), "by_request_id": by_request_id, "unmatched_ledger": unmatched,
            "unmatched_vendor": left, "price_mismatches": mismatches}


def read_transactions(client, start):
    """Vendor transactions, newest first, until one older than start or the end of the history."""
    items, cursor, pages = [], None, 0
    while pages < MAX_PAGES:
        params = {"limit": PAGE_LIMIT} if cursor is None else {"limit": PAGE_LIMIT, "cursor": cursor}
        result = client.call("credits/transactions", params, use_cache=False)
        pages += 1
        if result.status not in READ_OK:
            return {"items": items, "pages": pages, "covered": False,
                    "error": f"credits/transactions {result.status}: {result.reason}"}
        items.extend(result.items)
        times = [t for t in (_instant(i.get("created_at")) for i in result.items if isinstance(i, dict)) if t]
        body = result.body or {}
        paging = body.get("pagination") or {}
        cursor = paging.get("next_cursor") or (body.get("data") or {}).get("next_cursor")
        if (times and min(times) < start) or not cursor or paging.get("has_more") is False:
            return {"items": items, "pages": pages, "covered": True, "error": ""}
    return {"items": items, "pages": pages, "covered": False, "error": ""}


def runway(balance, floor, mean_daily):
    """Days of mean daily spend left above the floor; None when nothing is being spent."""
    above = balance - floor
    if above <= 0:
        return 0.0
    if mean_daily <= 0:
        return None
    return round(above / mean_daily, 2)


def _ledger_side(rows):
    total, paired = 0, []
    for r in rows:
        credits = _number(r.get("credits_charged") or 0)
        if credits is None:
            raise ValueError(f"credit_ledger holds a non-finite credits_charged: {r.get('credits_charged')}")
        total += credits
        if credits:
            row = {"route": r.get("route"), "at": _instant(r.get("logged_at")), "credits": _whole(credits),
                   "run_id": r.get("run_id")}
            if r.get("request_id"):
                row["request_id"] = r["request_id"]
            paired.append(row)
    return total, [r for r in paired if r["at"] is not None]


def _vendor_side(items, start, end):
    total, rows, kinds, skipped, untimed = 0, [], {}, 0, 0
    for t in items:
        if not isinstance(t, dict):
            continue
        at = _instant(t.get("created_at"))
        if at is None:
            untimed += 1
            continue
        if not start <= at < end:
            continue
        kind = str(t.get("type") or "")
        kinds[kind] = kinds.get(kind, 0) + 1
        spend = vendor_spend(t)
        if spend is None:
            skipped += 1
            continue
        total += spend
        if spend:
            rows.append({"id": t.get("id"), "request_id": t.get("request_id"), "route": vendor_route(t),
                         "at": at, "credits": _whole(spend)})
    return total, rows, {"vendor_types": kinds, "vendor_not_spend": skipped, "vendor_untimed": untimed}


def _sample(rows):
    return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()} for r in rows[:SAMPLE]]


def reconcile(day, client, ledger, caps):
    """Every figure for the run date, and the alert lines it raises."""
    start, end = window(day)
    floor = caps["BALANCE_FLOOR"]
    counts = {"run_date": day.isoformat(), "window": [start.isoformat(), end.isoformat()], "floor": floor}

    got = client.call("credits/balance", {}, use_cache=False)
    balance, balance_error = None, ""
    if got.status in READ_OK:
        balance = _number(((got.body or {}).get("data") or {}).get("balance"))
        if balance is None:
            balance_error = "credits/balance carried no readable balance"
    else:
        balance_error = f"credits/balance {got.status}: {got.reason}"
    vendor = read_transactions(client, start)

    ledger_total, ledger_rows = _ledger_side(ledger.day_rows(day))
    vendor_total, vendor_rows, vendor_notes = _vendor_side(vendor["items"], start, end)
    paired = match(ledger_rows, vendor_rows)
    difference = ledger_total - vendor_total
    spend_30d = float(ledger.spent(day - timedelta(days=TRAILING_DAYS - 1), day))
    first = ledger.first_day()
    spend_days = 1 if first is None else min(TRAILING_DAYS, max(1, (day - first).days + 1))
    mean = spend_30d / spend_days
    days_left = None if balance is None else runway(balance, floor, mean)

    counts.update({
        "ledger_rows": len(ledger_rows), "ledger_total": _whole(ledger_total),
        "vendor_rows": len(vendor_rows), "vendor_total": _whole(vendor_total), "difference": _whole(difference),
        "matched": paired["matched"], "matched_by_request_id": paired["by_request_id"],
        "unmatched_ledger": len(paired["unmatched_ledger"]),
        "unmatched_ledger_credits": _whole(sum(r["credits"] for r in paired["unmatched_ledger"])),
        "unmatched_ledger_sample": _sample(paired["unmatched_ledger"]),
        "unmatched_vendor": len(paired["unmatched_vendor"]),
        "unmatched_vendor_credits": _whole(sum(r["credits"] for r in paired["unmatched_vendor"])),
        "unmatched_vendor_sample": _sample(paired["unmatched_vendor"]),
        "price_mismatches": len(paired["price_mismatches"]),
        "price_mismatch_sample": paired["price_mismatches"][:SAMPLE],
        "vendor_pages": vendor["pages"], "vendor_covered": vendor["covered"], "vendor_error": vendor["error"],
        **vendor_notes,
        "balance": None if balance is None else _whole(balance), "balance_error": balance_error,
        "spend_30d": round(spend_30d, 4), "spend_days": spend_days, "mean_daily_spend": round(mean, 4), "runway_days": days_left,
    })

    reasons = []
    if abs(difference) > max(ALERT_CREDITS, ALERT_SHARE * abs(vendor_total)):
        reasons.append(f"ledger {counts['ledger_total']} against vendor {counts['vendor_total']}, "
                       f"difference {counts['difference']}, {counts['unmatched_ledger']} ledger and "
                       f"{counts['unmatched_vendor']} vendor rows unmatched")
    if not vendor["covered"]:
        reasons.append("vendor transactions not covered for the day"
                       + (f" ({vendor['error']})" if vendor["error"] else f" after {vendor['pages']} pages"))
    if balance_error:
        reasons.append(balance_error)
    lines = []
    if reasons:
        lines.append(("reconcile", f"42 ALERT reconcile: {day.isoformat()} " + "; ".join(reasons)))
    if days_left is not None and days_left < RUNWAY_DAYS:
        shown = counts["balance"] if balance is not None else balance_error
        lines.append(("credits_low", f"42 ALERT credits_low: {day.isoformat()} balance {shown}, floor {floor}, "
                      f"mean daily spend {counts['mean_daily_spend']:g} over {spend_days} days, "
                      f"runway {days_left:g} days"))
    counts["alerts"] = [name for name, _ in lines]
    return counts, [line for _, line in lines]


def run(day, *, runs, ledger, make_client, caps):
    """One reconcile run: a running row, the figures, the alert lines, the final row. Returns the exit code.
    A Scheduler retry of a run already ok or still live is AlreadyDone and exits 0 without reading."""
    try:
        run = chain.begin(STAGE, day, runs=runs)
    except chain.AlreadyDone as exc:
        print(f"nothing to reconcile: {exc}")
        return 0
    try:
        counts, lines = reconcile(day, make_client(run.run_id), ledger, caps)
    except Exception as exc:
        log.exception("reconcile %s failed", run.run_id)
        chain.finish(run, "failed", {}, f"{type(exc).__name__}: {exc}"[:1000], runs=runs)
        return 1
    for line in lines:
        log.error(line)
    failed = counts["vendor_error"] != ""
    chain.finish(run, "failed" if failed else "ok", counts, counts["vendor_error"] or None, runs=runs)
    print(json.dumps({"run_id": run.run_id, **counts}))
    return 1 if failed else 0


def print_plan(day, caps):
    start, end = window(day)
    table = f"{chain.PROJECT}.{DATASET}.credit_ledger"
    first = day - timedelta(days=TRAILING_DAYS - 1)
    print(f"reconcile plan for {day.isoformat()} (SAST day {start.isoformat()} to {end.isoformat()})")
    print(f"1. read credit_ledger for the day: {LEDGER_SQL.format(table=table)} with @day = {day.isoformat()}")
    print(f"2. GET credits/balance through the SocialCrawl client on the {SHARE} share (free, ledgered at 0, "
          "not refused by the balance floor)")
    print(f"3. GET credits/transactions limit={PAGE_LIMIT}, then limit={PAGE_LIMIT} and cursor=<next_cursor>, until a "
          f"transaction older than {start.isoformat()} or the end of the history, at most {MAX_PAGES} pages "
          "(free, ledgered at 0)")
    print(f"4. keep transactions from {start.isoformat()} to {end.isoformat()}; top-ups are left out, refunds count "
          "negative")
    print(f"5. pair rows by request id, else by route within {int(MATCH_WINDOW.total_seconds())} s; report ledger total, "
          "vendor total, difference, unmatched ledger rows and unmatched vendor rows")
    print(f"6. read the ledger's spend from {first.isoformat()} to {day.isoformat()}: runway = (balance - "
          f"BALANCE_FLOOR {caps['BALANCE_FLOOR']}) / ({TRAILING_DAYS} day spend / {TRAILING_DAYS})")
    print(f"alerts: the reconcile alert when |difference| > the larger of {ALERT_CREDITS} credits and "
          f"{ALERT_SHARE:.0%} of the vendor total, or the day is not covered; the credits_low alert when runway < "
          f"{RUNWAY_DAYS} days")
    print(f"writes: one runs row per state in {chain.PROJECT}.{chain.DATASET}.runs, stage {STAGE}, through "
          "chain.begin and chain.finish (a retry of a run already ok or live exits 0 as AlreadyDone); the client "
          "appends its credit_ledger and raw_responses rows. No network in --plan.")


def main(argv=None, *, env=None, bq=None, runs=None, http=None, clock=None):
    env = os.environ if env is None else env
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--plan", action="store_true", help="print what it would read and compare; no network")
    opts = args.parse_args(argv)
    clock = clock or (lambda: datetime.now(timezone.utc))
    day = run_day(env, clock())
    caps = load_caps()
    if opts.plan:
        print_plan(day, caps)
        return 0

    from core.collect.socialcrawl_client import SocialCrawlClient, requests_http
    from core.collect.stores import BigQueryRawStore

    if bq is None:
        from google.cloud import bigquery

        bq = bigquery.Client(project=chain.PROJECT)
    ledger = LedgerDay(bq, chain.PROJECT)
    runs = runs or chain.BigQueryRunsStore(bq)

    def make_client(run_id):
        return SocialCrawlClient(share=SHARE, run_id=run_id, mode="live", ledger=ledger,
                                 raw=BigQueryRawStore(bq, chain.PROJECT), http=http or requests_http,
                                 clock=clock, caps=caps)

    return run(day, runs=runs, ledger=ledger, make_client=make_client, caps=caps)


if __name__ == "__main__":
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    raise SystemExit(main())
