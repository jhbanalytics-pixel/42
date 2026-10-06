"""f42-scheduled-asks: re-asks each due schedule once a day at 07:00 SAST (core/api/contract.md section 14.2).

Money first: before each ask the job reads today's spend again and asks only when the SCHEDULED_DAILY share, the
ASK_DAILY credits and the MODEL_DAILY_USD dollars left each cover the saved tier's maximum. Otherwise it skips the
schedule with a runs row (stage scheduled) that names the reason; it never drops to a lower tier. Without the
SCHEDULED_DAILY row in core/config/caps.yaml the job is a dry run: it prints what it would ask and asks nothing.

Run: py -3.13 -m core.api.scheduled [--live]
"""
import argparse
import json
import logging
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path

from core.api import agent_app, investigations

log = logging.getLogger("f42-scheduled-asks")

CAPS_FILE = Path(__file__).resolve().parents[1] / "config" / "caps.yaml"
# Each tier's maximum spend per ask: credits from AGENT.md's effort tiers, dollars from the max_budget_usd that L3's
# core/agent/context.py TIERS sets per tier (AGENT.md: "max_turns and max_budget_usd set per tier").
TIERS = {"T0": {"credits": 10, "model_usd": 0.50}, "T1": {"credits": 60, "model_usd": 2.00}}
SHARE_SPENT = "Scheduled share spent"
DAILY_SPENT = "Daily questions spent"
MODEL_SPENT = "Model budget spent"
SPEND_UNKNOWN = "Today's spend could not be read"
RUNS_UNKNOWN = "Today's runs could not be read"
ALREADY_ASKED = "Already asked today"
STARTED = "Ask started"
START_UNRECORDED = "The ask's start could not be recorded"
ASK_FAILED = "The ask failed"
NO_RESERVATION = {"credits": 0, "model_usd": 0.0}


def today():
    return agent_app.now().date()


def run_id(day, schedule_id):
    return f"sched-{day.isoformat()}-{schedule_id}"


def due(schedules, day):
    """Active schedules due on `day`: daily every day, weekly_monday on Mondays, paused never."""
    return [s for s in schedules if s.get("status") == "active"
            and (s.get("cadence") == "daily" or (s.get("cadence") == "weekly_monday" and day.weekday() == 0))]


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def precheck(tier, spent, share):
    """None when every budget left covers the tier's maximum, else the skip reason. Unknown spend is never money.

    `spent` is {"scheduled": credits, "credits": credits under ASK_DAILY, "model_usd": dollars}; `share` is
    SCHEDULED_DAILY, or None in a dry run without it, when the share is not checked."""
    most = TIERS[tier]
    if not spent or not all(_number(spent.get(k)) for k in ("scheduled", "credits", "model_usd")):
        return SPEND_UNKNOWN
    if share is not None and share - spent["scheduled"] < most["credits"]:
        return SHARE_SPENT
    left = investigations.budget_left(spent, NO_RESERVATION)
    if left["credits"] < most["credits"]:
        return DAILY_SPENT
    if left["model_usd"] < most["model_usd"]:
        return MODEL_SPENT
    return None


def _created(schedule):
    at = schedule["created_at"]
    return (datetime.fromisoformat(at) if isinstance(at, str) else at, schedule["schedule_id"])


def run(schedules, day, spend_reader, ask, record_skip, share, dry=False, asked=frozenset(), record_start=None):
    """Ask each due schedule, oldest first, one at a time. Returns one outcome per due schedule.

    ask(schedule, run_id) runs one ask and may return its Ask record: a record with status failed (agent_app.execute
    catches the failure and writes the failed ask row itself) is reported failed with the record's error message,
    never asked, and needs no second row; record_skip(schedule, run_id, reason, status) writes a skip or failure;
    record_start(schedule, run_id) writes the started row just before each ask, so a job killed mid-ask still leaves
    a row, and when that write fails the schedule is not asked. A dry run (or a missing share) reads spend but asks
    and records nothing. `asked` holds the sched- run_ids already asked or started today, so a retried job never asks
    a schedule twice; None means they could not be read, and nothing is asked."""
    dry = dry or share is None
    out = []
    for schedule in sorted(due(schedules, day), key=_created):
        rid = run_id(day, schedule["schedule_id"])
        if asked is not None and rid in asked:
            out.append({"schedule_id": schedule["schedule_id"], "run_id": rid, "tier": schedule["tier"],
                        "question": schedule["question"], "status": "already_asked", "reason": ALREADY_ASKED})
            continue
        try:
            spent = spend_reader()
        except Exception as exc:
            log.warning("reading today's spend failed (%s)", type(exc).__name__)
            spent = None
        reason = RUNS_UNKNOWN if asked is None else precheck(schedule["tier"], spent, share)
        status = ("would_skip" if reason else "would_ask") if dry else ("skipped" if reason else "asked")
        if not dry and reason:
            _record(record_skip, schedule, rid, reason, "skipped")
        elif not dry:
            try:
                if record_start is not None:
                    record_start(schedule, rid)
            except Exception:
                # Without the started row a killed ask could be asked again by a retry, so it is not asked.
                log.exception("scheduled run %s: its started row was not written", rid)
                status, reason = "failed", START_UNRECORDED
                _record(record_skip, schedule, rid, reason, "failed")
            else:
                try:
                    record = ask(schedule, rid)
                except Exception as exc:
                    log.exception("scheduled ask %s failed", rid)
                    status, reason = "failed", f"{ASK_FAILED} ({type(exc).__name__})"
                    _record(record_skip, schedule, rid, reason, "failed")
                else:
                    if isinstance(record, dict) and record.get("status") == "failed":
                        log.warning("scheduled ask %s failed", rid)
                        status, reason = "failed", (record.get("error") or {}).get("message") or ASK_FAILED
        out.append({"schedule_id": schedule["schedule_id"], "run_id": rid, "tier": schedule["tier"],
                    "question": schedule["question"], "status": status, "reason": reason})
    return out


def _record(record_skip, schedule, rid, reason, status):
    try:
        record_skip(schedule, rid, reason, status)
    except Exception:
        log.exception("scheduled run %s: its %s row was not written", rid, status)


def read_share(path=None):
    """SCHEDULED_DAILY in credits from core/config/caps.yaml, or None until Albert sets that row."""
    import yaml
    path = Path(path or CAPS_FILE)
    if not path.is_file():
        return None
    value = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("SCHEDULED_DAILY")
    return value if _number(value) else None


def scheduled_in_rows(rows, day):
    """Credits the scheduled asks spent on `day`, from runs rows held in memory: stage ask, run_id sched-."""
    credits = {}
    for row in rows:
        if (row.get("run_date") == day and row.get("stage") == "ask"
                and str(row.get("run_id", "")).startswith("sched-") and _number(row.get("credits"))):
            credits[row["run_id"]] = max(credits.get(row["run_id"], 0), row["credits"])
    return sum(credits.values())


def scheduled_in_bigquery(client, project, day):
    """Credits the scheduled asks spent on `day`: the larger of the credit_ledger sum (job ask, sched- run_id) and the
    credits on the sched- runs rows. run_ask passes the request's sched- run_id on to the SocialCrawl client, so the
    ledger rows carry it; the runs rows (live_ask pins it too) still cover rows written before run_ask did."""
    from google.cloud import bigquery
    params = [bigquery.ScalarQueryParameter("d", "DATE", day)]
    ledger = investigations._rows(
        client, f"SELECT SUM(l.credits_charged) AS credits FROM `{project}.intelligence_42_core.credit_ledger` l "
                "WHERE l.trend_date = @d AND l.job = 'ask' AND STARTS_WITH(l.run_id, 'sched-')", params)
    runs = investigations._rows(
        client, f"SELECT SUM(u.credits) AS credits FROM (SELECT r.run_id, MAX(r.credits) AS credits "
                f"FROM `{project}.intelligence_42_agent.runs` r WHERE r.run_date = @d AND r.stage = 'ask' "
                "AND STARTS_WITH(r.run_id, 'sched-') GROUP BY r.run_id) u", params)
    return max(float(ledger[0]["credits"] or 0) if ledger else 0.0, float(runs[0]["credits"] or 0) if runs else 0.0)


def asked_today(day):
    """The sched- run_ids with a stage ask runs row or a started row (stage scheduled, status started) on `day`,
    or None when they could not be read."""
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery
        try:
            rows = investigations._rows(
                agent_app.bigquery_client(),
                f"SELECT DISTINCT r.run_id FROM `{agent_app.project()}.intelligence_42_agent.runs` r "
                "WHERE r.run_date = @d AND (r.stage = 'ask' OR (r.stage = 'scheduled' AND r.status = 'started')) "
                "AND STARTS_WITH(r.run_id, 'sched-')",
                [bigquery.ScalarQueryParameter("d", "DATE", day)])
        except Exception as exc:
            log.warning("reading today's scheduled runs failed (%s)", type(exc).__name__)
            return None
        return {r["run_id"] for r in rows}
    with agent_app._lock:
        rows = list(agent_app.SINK)
    return {r["run_id"] for r in rows if r.get("run_date") == day
            and (r.get("stage") == "ask" or (r.get("stage") == "scheduled" and r.get("status") == "started"))
            and str(r.get("run_id", "")).startswith("sched-")}


def read_spend():
    """Today's spend: the scheduled share, credits under ASK_DAILY and model dollars under MODEL_DAILY_USD."""
    day = today().isoformat()
    spent = agent_app.spent_today()
    if os.environ.get("F42_DATA") == "bigquery":
        try:
            spent["scheduled"] = scheduled_in_bigquery(agent_app.bigquery_client(), agent_app.project(), day)
        except Exception as exc:
            log.warning("reading the scheduled share failed (%s); treating it as unknown", type(exc).__name__)
            spent["scheduled"] = None
    else:
        with agent_app._lock:
            rows = list(agent_app.SINK)
        spent["scheduled"] = scheduled_in_rows(rows, day)
    return spent


def live_ask(schedule, rid):
    """One live ask through run_ask, recorded to runs like any Ask, with schedule_id and the sched- run_id."""
    run_ask = agent_app.resolve_run_ask()
    if run_ask is None:
        raise RuntimeError("core.agent.run_ask is not importable")
    market = schedule["market"]
    request = {"ask_id": f"a_{agent_app.now(market).strftime('%Y%m%d')}_{secrets.token_hex(4)}",
               "question": schedule["question"], "market": market, "parent_id": None, "tier": schedule["tier"],
               "mode": "live", "wait": False, "from_card": None, "run_id": rid,
               "schedule_id": schedule["schedule_id"]}

    def pinned(req, emit, should_stop):
        # The runs row keeps the sched- run_id, so the scheduled share and GET /api/schedules find it.
        try:
            result = run_ask(req, emit, should_stop)
        except Exception as exc:
            partial = getattr(exc, "run", None)
            try:
                exc.run = {**(partial if isinstance(partial, dict) else {}), "run_id": rid}
            except Exception:
                pass
            raise
        return {**result, "run": {**(result.get("run") or {}), "run_id": rid}}

    ask = agent_app.Ask(request)
    ask.record["schedule_id"] = schedule["schedule_id"]
    agent_app.execute(ask, pinned)
    return ask.snapshot()


def record_skip(schedule, rid, reason, status="skipped"):
    """A runs row (stage scheduled) naming the schedule and why it was not asked, or that its ask started.

    A started row has no finished_at, so GET /api/schedules shows it only until that day's ask row lands."""
    at = agent_app.now().isoformat()
    row = {"run_id": rid, "stage": "scheduled", "run_date": rid[6:16], "status": status, "started_at": at,
           "finished_at": None if status == "started" else at, "question": schedule["question"], "tier": schedule["tier"], "credits": 0,
           "seconds": 0, "outcome": reason, "answer": None,
           "record": json.dumps({"schedule_id": schedule["schedule_id"], "reason": reason})}
    if not agent_app.append("runs", agent_app.SINK, row):
        raise RuntimeError("the runs row was not written")


def record_start(schedule, rid):
    """The started row, written before each ask: a retry skips any schedule that has one today."""
    record_skip(schedule, rid, STARTED, "started")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scheduled questions (contract.md 14.2)")
    parser.add_argument("--live", action="store_true", help="ask for real; needs SCHEDULED_DAILY in caps.yaml")
    args = parser.parse_args(argv)
    share = read_share()
    live = args.live and share is not None
    if args.live and share is None:
        print("SCHEDULED_DAILY is not set in core/config/caps.yaml, so this is a dry run.")
    day = today()
    outcomes = run(agent_app.current_schedules(), day, read_spend, live_ask, record_skip, share, dry=not live,
                   asked=asked_today(day.isoformat()), record_start=record_start)
    failed = sum(o["status"] == "failed" for o in outcomes)
    print(f"{'Live run' if live else 'Dry run'} for {day.isoformat()}: {len(outcomes)} schedules due"
          + (f", {failed} failed." if failed else "."))
    for o in outcomes:
        why = f": {o['reason']}" if o["reason"] else ""
        # No question text: stdout lands in Cloud Logging, and a user can type a handle into a question.
        print(f"{o['status']} {o['schedule_id']} ({o['tier']}){why}")
    # A failed ask fails the job, as other jobs do; its one retry asks nothing already asked or started today.
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
