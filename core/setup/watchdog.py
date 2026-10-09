"""The 42 watchdog (BUILD.md task 2.10): checks the day's runs, rows and briefs in BigQuery and writes one
structured ERROR line per firing alert, "42 ALERT <name>: <reason>", which the log-based alert policies of
core/setup/monitoring.py turn into email.

    python -m core.setup.watchdog      as the Cloud Run job f42-watchdog, identity f42-brief, no secret mounted

Rules, all for today in SAST (RUN_DATE overrides the day):
  collection_missing  from 04:00, no ok collect runs row
  zero_rows           an ok collect, and a market with no post_observations
  brief_late          from 06:30, a market with no published briefs row
  model_spend         model spend booked in runs over 80% of MODEL_DAILY_USD: counts.model_usd on every stage's
                      runs plus record.run.model_usd on ask runs, once per run_id
  schema_drift        a route whose successful raw_responses today carry a top-level key never seen in the last
                      7 days, or lack one every response of those 7 days carried. Checked once a day, in the
                      07:00 run, because it reads 8 days of response bodies.
  agent_error_rate    over 5% of today's finished ask runs failed, once there are at least 20
  seeds_failed        the day's latest seeds runs row (the seed queue writer inside f42-detect) is failed
  agent_views_failed  the day's latest detect runs row has counts.agent_views.status 'failed': f42-detect could
                      not apply the views L4's API reads, and detect carried on without them
  understand_degraded the day's latest understand run is recorded ok but is partial (counts.partial true, any
                      partial_reason: as when the clusterer's stack is unusable and the job still exits 0) or
                      could not write a step (counts.embed_error, counts.enrich_error, or an error under a
                      market in counts.cluster). A failed run is job_failed's, not this rule's.
credits_low and reconcile come from the reconcile job's own lines, and job_failed from Cloud Run's job
failure logs.

Five more rules are log only (LOG_ONLY): they write the same "42 ALERT <name>:" line at WARNING (INFO for
brief_all_held), and no Cloud Monitoring policy matches them, so they add no cloud resource until someone adds one
to core/setup/monitoring.py:
  brief_empty         from 06:30, a market whose current brief is published or partial with no cards and nothing
                      held back
  brief_all_held      from 06:30, a market with no cards because every candidate was held: information, not a fault
  brief_data_issue    a market whose current brief is a data_issue row, which the brief job writes from 06:15 when
                      the upstream stage is not ok, and which finishes the brief run ok for the day
  stage_dead          a stage whose latest runs row for the day is still running past its chain.TIMEOUTS: the
                      run was killed and never wrote a final row
  watchdog_gap        the watchdog's own last runs row today is more than 90 minutes old, so it was not running
A log-only rule that cannot run is logged at WARNING and recorded in counts.log_only_errors, but it never fails
the job: a failed job reaches the job_failed policy and pages, which a log-only signal must not do.
Nothing here can tell that the watchdog is dead for good: only a Cloud Monitoring absence policy on its runs can,
and that is a new cloud resource.

Each alert fires at most once a day: every run appends one runs row, stage watchdog, whose counts.fired
lists what it fired, and the next run reads today's rows first and skips those rules. If those rows cannot
be read, every rule runs. A rule that cannot run is logged and the others still run; the row is then
failed and the job exits 1, which the job_failed policy reports. That row is the only thing it writes.
"""
import json
import sys
import uuid
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.collect import chain  # noqa: E402
from core.collect.parse import MARKETS  # noqa: E402
from core.config.caps import model_daily_usd  # noqa: E402

PROJECT = chain.PROJECT
CORE = f"{PROJECT}.intelligence_42_core"
AGENT = f"{PROJECT}.intelligence_42_agent"
STAGE = "watchdog"
ALERTS = ("collection_missing", "zero_rows", "brief_late", "model_spend", "schema_drift", "agent_error_rate",
          "seeds_failed", "agent_views_failed", "understand_degraded")
LOG_ONLY = ("brief_empty", "brief_all_held", "brief_data_issue", "stage_dead", "watchdog_gap")
SEVERITY = {"brief_all_held": "INFO"}
NEEDS_NOW = ("model_spend", "stage_dead", "watchdog_gap")
SPEND_SHARE = 0.8
COLLECT_BY = time(4, 0)
BRIEF_BY = time(6, 30)
DRIFT_AT, DRIFT_WINDOW = time(7, 0), timedelta(minutes=15)
ASK_MIN_RUNS, ASK_MAX_FAILED = 20, 0.05
# The longest a healthy watchdog goes between runs is the hour from 01:00 to 02:00; the rest is slack.
WATCHDOG_MAX_GAP = timedelta(minutes=90)
# The degraded writes of an understand run, spelled exactly as core/api/store.py DEGRADED_SQL reads them for
# Coverage (a test holds the two together); copied here so the job image does not import the API's store.
_CLUSTER_MARKETS = ("za", "ng", "ke", "pan")
UNDERSTAND_DEGRADED = "ARRAY(SELECT w FROM UNNEST([{}]) AS w WITH OFFSET o WHERE w IS NOT NULL ORDER BY o)".format(
    ", ".join(["IF(JSON_VALUE(r.counts, '$.embed_error') IS NOT NULL, 'embed', NULL)",
               "IF(JSON_VALUE(r.counts, '$.enrich_error') IS NOT NULL, 'enrich', NULL)"]
              + [f"IF(JSON_VALUE(r.counts, '$.cluster.{m}.error') IS NOT NULL, 'cluster:{m}', NULL)"
                 for m in _CLUSTER_MARKETS]))

# For deploy_jobs.py and schedule.py (task 2.10 leaves those files to their owner): the job, and its two
# Scheduler jobs in Africa/Johannesburg, every quarter hour from 02:00 to 07:45 and hourly otherwise.
JOB = {"name": "f42-watchdog", "module": "core.setup.watchdog", "identity": "f42-brief",
       "timeout": timedelta(minutes=5), "secret": False, "retries": 0}
SCHEDULES = (("f42-watchdog-quarter", "*/15 2-7 * * *"), ("f42-watchdog-hourly", "0 0,1,8-23 * * *"))


class Alert:
    def __init__(self, name, reason, severity=None):
        self.name, self.reason = name, reason
        self.severity = severity or ("ERROR" if name in ALERTS else SEVERITY.get(name, "WARNING"))

    def __eq__(self, other):
        return isinstance(other, Alert) and (self.name, self.reason, self.severity) == (
            other.name, other.reason, other.severity)

    def __repr__(self):
        return f"Alert({self.name!r}, {self.reason!r})"


def _collection_missing(store, d, clock):
    if clock >= COLLECT_BY and not store.collect_ok(d):
        return f"no ok collect run for {d} by {COLLECT_BY:%H:%M} SAST"


def _zero_rows(store, d, clock):
    if not store.collect_ok(d):
        return None
    counts = store.observations(d)
    empty = [m for m in MARKETS if not counts.get(m)]
    if empty:
        return f"collect for {d} ran ok but wrote no post_observations for {', '.join(empty)}"


def _brief_late(store, d, clock):
    if clock < BRIEF_BY:
        return None
    missing = [m for m in MARKETS if m not in store.published_markets(d)]
    if missing:
        return f"no published brief for {d} by {BRIEF_BY:%H:%M} SAST for {', '.join(missing)}"


def _model_spend(store, d, clock, now):
    usd = store.model_usd(d)
    cap = model_daily_usd(now=now)
    if usd > SPEND_SHARE * cap:
        return f"model spend for {d} is USD {usd:.2f}, over {SPEND_SHARE:.0%} of MODEL_DAILY_USD {cap:.0f}"


def _schema_drift(store, d, clock):
    start = datetime.combine(d, DRIFT_AT)
    if not start <= datetime.combine(d, clock) < start + DRIFT_WINDOW:
        return None
    added, gone = defaultdict(list), defaultdict(list)
    for r in store.route_keys(d):
        if not (r["today_total"] and r["prior_total"]):
            continue
        if r["today_rows"] and not r["prior_rows"]:
            added[r["route"]].append(r["key"])
        elif not r["today_rows"] and r["prior_rows"] == r["prior_total"]:
            gone[r["route"]].append(r["key"])
    parts = []
    for route in sorted(set(added) | set(gone)):
        change = [f"new {', '.join(sorted(added[route]))}" if added[route] else "",
                  f"missing {', '.join(sorted(gone[route]))}" if gone[route] else ""]
        parts.append(f"{route} ({'; '.join(c for c in change if c)})")
    if parts:
        return f"top-level keys changed against the last 7 days: {'; '.join(parts)}"


def _agent_error_rate(store, d, clock):
    total, failed = store.ask_runs(d)
    if total >= ASK_MIN_RUNS and failed > ASK_MAX_FAILED * total:
        return f"{failed} of {total} ask runs for {d} failed, over {ASK_MAX_FAILED:.0%}"


def _seeds_failed(store, d, clock):
    latest = store.seeds_latest(d)
    if latest and latest["status"] == "failed":
        return f"the latest seeds run for {d}, {latest['run_id']}, failed"


def _agent_views_failed(store, d, clock):
    latest = store.detect_latest(d)
    if latest and latest["agent_views_status"] == "failed":
        error = latest["agent_views_error"]
        return (f"the latest detect run for {d}, {latest['run_id']}, failed to apply the agent views"
                + (f": {error}" if error else ""))


def _markets_without_cards(store, d, clock, held):
    if clock < BRIEF_BY:
        return []
    state = store.brief_state(d)
    return [(m, state[m]) for m in MARKETS if m in state and state[m]["status"] != "data_issue"
            and not state[m]["cards"] and bool(state[m]["held"]) is held]


def _brief_empty(store, d, clock):
    empty = _markets_without_cards(store, d, clock, held=False)
    if empty:
        return (f"the brief for {d} is published with no cards and nothing held back for "
                f"{', '.join(m for m, _ in empty)}")


def _brief_all_held(store, d, clock):
    held = _markets_without_cards(store, d, clock, held=True)
    if held:
        shown = ", ".join(f"{m} {state['held']} held" for m, state in held)
        return f"the brief for {d} has no cards because every candidate was held back: {shown}"


def _brief_data_issue(store, d, clock):
    state = store.brief_state(d)
    bad = [m for m in MARKETS if m in state and state[m]["status"] == "data_issue"]
    if bad:
        return (f"the current brief for {d} is a data issue row for {', '.join(bad)}: the brief job published it "
                "without a full upstream, and its run is recorded ok")


PARTIAL_WORDS = {"cluster_stack_failed": "topic grouping failed, so no topic items were judged today"}
STEP_WORDS = {"embed": "embeddings", "enrich": "enrichment"}
ERROR_CHARS = 160


def _short(text):
    one = " ".join(str(text).split())
    return one if len(one) <= ERROR_CHARS else one[:ERROR_CHARS - 3] + "..."


def _step_words(step):
    if step.startswith("cluster:"):
        return f"topic clusters for {step.split(':', 1)[1].upper()}"
    return STEP_WORDS.get(step, step)


def _understand_degraded(store, d, clock):
    latest = store.understand_latest(d)
    if not latest or latest["status"] != "ok":
        return None
    parts = []
    if latest.get("partial"):
        code = latest.get("partial_reason")
        said = PARTIAL_WORDS.get(code) if code else None
        text = f"{said} ({code})" if said else code or "no reason recorded"
        if latest.get("partial_error"):
            text += f": {_short(latest['partial_error'])}"
        parts.append(f"it is partial, {text}")
    steps = list(latest.get("degraded") or [])
    if latest.get("embed_error") and "embed" not in steps:
        steps.insert(0, "embed")
    if steps:
        words = [_step_words(s) + (f" ({_short(latest['embed_error'])})" if s == "embed" else "") for s in steps]
        parts.append(f"it could not write {', '.join(words)}")
    if parts:
        return f"the latest understand run for {d}, {latest['run_id']}, finished ok but " + "; ".join(parts)


def _utc(value):
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _stage_dead(store, d, clock, now):
    dead = []
    for row in store.stage_latest(d):
        limit = chain.TIMEOUTS.get(row["stage"])
        started = _utc(row["started_at"])
        if row["status"] == "running" and limit is not None and now - started > limit:
            minutes = int(limit.total_seconds() // 60)
            dead.append(f"{row['stage']} run {row['run_id']} started {started.astimezone(chain.SAST):%H:%M} SAST "
                        f"is still running past its {minutes} minute timeout")
    if dead:
        return f"{'; '.join(dead)}: the run was killed and wrote no final row"


def _watchdog_gap(store, d, clock, now):
    if d != now.astimezone(chain.SAST).date():
        return None
    last = store.watchdog_last(d)
    since = _utc(last) if last else datetime.combine(d, time(0), chain.SAST)
    if now - since > WATCHDOG_MAX_GAP:
        return (f"the watchdog has not run since {since.astimezone(chain.SAST):%H:%M} SAST"
                f"{'' if last else ' (the start of the day)'}, over {int(WATCHDOG_MAX_GAP.total_seconds() // 60)} "
                "minutes ago")


RULES = {"collection_missing": _collection_missing, "zero_rows": _zero_rows, "brief_late": _brief_late,
         "model_spend": _model_spend, "schema_drift": _schema_drift, "agent_error_rate": _agent_error_rate,
         "seeds_failed": _seeds_failed, "agent_views_failed": _agent_views_failed, "brief_empty": _brief_empty,
         "brief_all_held": _brief_all_held, "brief_data_issue": _brief_data_issue,
         "understand_degraded": _understand_degraded, "stage_dead": _stage_dead, "watchdog_gap": _watchdog_gap}


class _BriefStateOnce:
    """The store, but brief_state answers from the first read: three rules ask the same question."""

    def __init__(self, store):
        self._store, self._brief_state = store, {}

    def __getattr__(self, name):
        return getattr(self._store, name)

    def brief_state(self, d):
        if d not in self._brief_state:
            self._brief_state[d] = self._store.brief_state(d)
        return self._brief_state[d]


def check(now, store, errors=None, skip=()):
    """The alerts firing at now, in ALERTS order, leaving out the rules named in skip. A rule that raises is
    recorded in errors, if given, else re-raised."""
    store = _BriefStateOnce(store)
    d = chain.today(now)
    clock = now.astimezone(chain.SAST).time()
    alerts = []
    for name in ALERTS + LOG_ONLY:
        if name in skip:
            continue
        try:
            if name in NEEDS_NOW:
                reason = RULES[name](store, d, clock, now)
            else:
                reason = RULES[name](store, d, clock)
        except Exception as exc:
            if errors is None:
                raise
            errors.append((name, exc))
            continue
        if reason:
            alerts.append(Alert(name, reason))
    return alerts


def _line(severity, message, **extra):
    print(json.dumps({"severity": severity, "message": message, **extra}), flush=True)


def main(now=None, store=None, runs=None):
    now = now or datetime.now(timezone.utc)
    if store is None:
        from google.cloud import bigquery

        client = bigquery.Client(project=PROJECT)
        store, runs = BigQueryStore(client), runs or chain.BigQueryRunsStore(client)
    runs = runs or chain.default_runs()
    d = chain.today(now)
    # Not chain.begin(): it refuses a second ok run of a stage on the same day, and the watchdog runs all day.
    run = chain.Run(f"{STAGE}-{d:%Y%m%d}-{uuid.uuid4().hex[:12]}", STAGE, d,
                    datetime.now(timezone.utc).isoformat(), runs)
    errors, done = [], set()
    try:
        done = store.fired_today(d)
    except Exception as exc:
        errors.append(("fired_today", exc))
        _line("ERROR", f"42 watchdog: could not read today's fired alerts, so every rule runs: "
                       f"{type(exc).__name__}: {exc}")
    alerts = check(now, store, errors, skip=done)
    for a in alerts:
        _line(a.severity, f"42 ALERT {a.name}: {a.reason}", alert=a.name)
    soft = [(n, e) for n, e in errors if n in LOG_ONLY]
    errors = [(n, e) for n, e in errors if n not in LOG_ONLY]
    for name, exc in soft:
        _line("WARNING", f"42 watchdog: log-only rule {name} could not run: {type(exc).__name__}: {exc}")
    for name, exc in errors:
        if name != "fired_today":
            _line("ERROR", f"42 watchdog: rule {name} could not run: {type(exc).__name__}: {exc}")
    if not alerts and not errors and not soft:
        _line("INFO", f"42 watchdog: no new alert for {d}; already fired today: {', '.join(sorted(done)) or 'none'}")
    counts = {"fired": [a.name for a in alerts], "already_fired": sorted(done), "errors": [n for n, _ in errors],
              "log_only_errors": [n for n, _ in soft]}
    error = "; ".join(f"{n}: {type(e).__name__}: {e}" for n, e in errors)[:1000] or None
    chain.finish(run, "failed" if errors else "ok", counts, error, runs=runs)
    return 1 if errors else 0


_LATEST = ("ARRAY_AGG(r.status ORDER BY COALESCE(r.finished_at, r.started_at) DESC, "
           "r.finished_at IS NOT NULL DESC LIMIT 1)[OFFSET(0)]")

SQL = {
    "collect_ok": f"""SELECT COUNT(*) n FROM `{AGENT}.runs`
WHERE run_date = @d AND stage = 'collect' AND status = 'ok'""",
    "observations": f"""SELECT market, COUNT(*) n FROM `{CORE}.post_observations`
WHERE observed_date = @d GROUP BY market""",
    "published_markets": f"""SELECT DISTINCT market FROM `{AGENT}.briefs`
WHERE brief_date = @d AND published_at IS NOT NULL""",
    # A run appends several rows and only the finished one carries the figure, so MAX once per run_id.
    "model_usd": f"""SELECT IFNULL(SUM(u.usd), 0) usd FROM (
  SELECT r.run_id,
    IFNULL(MAX(CAST(JSON_VALUE(r.counts, '$.model_usd') AS FLOAT64)), 0)
    + IFNULL(MAX(IF(r.stage = 'ask', CAST(JSON_VALUE(r.record, '$.run.model_usd') AS FLOAT64), NULL)), 0) usd
  FROM `{AGENT}.runs` r
  WHERE r.run_date = @d
  GROUP BY r.run_id) u""",
    # Partitions are UTC days; the day and the 7 before it are SAST days, so one UTC day either side is read.
    "route_keys": f"""WITH r AS (
  SELECT route, DATE(fetched_at, 'Africa/Johannesburg') = @d is_today, JSON_KEYS(body, 1) ks
  FROM `{CORE}.raw_responses`
  WHERE DATE(fetched_at) BETWEEN DATE_SUB(@d, INTERVAL 8 DAY) AND DATE_ADD(@d, INTERVAL 1 DAY)
    AND DATE(fetched_at, 'Africa/Johannesburg') BETWEEN DATE_SUB(@d, INTERVAL 7 DAY) AND @d
    AND http_status BETWEEN 200 AND 299 AND JSON_TYPE(body) = 'object'),
t AS (SELECT route, COUNTIF(is_today) today_total, COUNTIF(NOT is_today) prior_total FROM r GROUP BY route)
SELECT r.route, k key, COUNTIF(r.is_today) today_rows, COUNTIF(NOT r.is_today) prior_rows,
  ANY_VALUE(t.today_total) today_total, ANY_VALUE(t.prior_total) prior_total
FROM r CROSS JOIN UNNEST(r.ks) k JOIN t ON t.route = r.route
GROUP BY r.route, k""",
    "ask_runs": f"""SELECT COUNT(*) total, COUNTIF(status = 'failed') failed FROM (
  SELECT r.run_id, {_LATEST} status
  FROM `{AGENT}.runs` r
  WHERE r.run_date = @d AND r.stage = 'ask'
  GROUP BY r.run_id)
WHERE status NOT IN ('running', '{chain.SKIPPED}')""",
    "seeds_latest": f"""SELECT run_id, status FROM `{AGENT}.runs`
WHERE run_date = @d AND stage = 'seeds' AND status != '{chain.SKIPPED}'
ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC LIMIT 1""",
    "detect_latest": f"""SELECT run_id, JSON_VALUE(counts, '$.agent_views.status') agent_views_status,
  JSON_VALUE(counts, '$.agent_views.error') agent_views_error
FROM `{AGENT}.runs`
WHERE run_date = @d AND stage = 'detect' AND status != '{chain.SKIPPED}'
ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC LIMIT 1""",
    # The current brief of each market (the latest published_at among rows whose run is ok), with its card and
    # held counts; the payload is the brief job's market payload (core/brief/payload.py).
    "brief_state": f"""SELECT market, status,
  IFNULL(ARRAY_LENGTH(JSON_QUERY_ARRAY(payload, '$.cards')), 0) cards,
  IFNULL(CAST(JSON_VALUE(payload, '$.held_back.count') AS INT64), 0) held
FROM `{AGENT}.v_briefs_current`
WHERE brief_date = @d
QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY published_at DESC) = 1""",
    "understand_latest": f"""SELECT run_id, status, {UNDERSTAND_DEGRADED} degraded,
  JSON_VALUE(r.counts, '$.embed_error') embed_error,
  IFNULL(JSON_VALUE(r.counts, '$.partial') = 'true', FALSE) partial,
  JSON_VALUE(r.counts, '$.partial_reason') partial_reason,
  JSON_VALUE(r.counts, '$.partial_error') partial_error
FROM `{AGENT}.runs` r
WHERE run_date = @d AND stage = 'understand' AND status != '{chain.SKIPPED}'
ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC LIMIT 1""",
    "stage_latest": f"""SELECT stage, run_id, status, started_at FROM `{AGENT}.runs`
WHERE run_date = @d AND stage IN ({', '.join(repr(s) for s in chain.TIMEOUTS)}) AND status != '{chain.SKIPPED}'
QUALIFY ROW_NUMBER() OVER (PARTITION BY stage
  ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC) = 1""",
    "watchdog_last": f"""SELECT MAX(COALESCE(finished_at, started_at)) last_at FROM `{AGENT}.runs`
WHERE run_date = @d AND stage = '{STAGE}'""",
    "fired_today": f"""SELECT DISTINCT name FROM `{AGENT}.runs` r, UNNEST(JSON_VALUE_ARRAY(r.counts, '$.fired')) name
WHERE r.run_date = @d AND r.stage = '{STAGE}'""",
}


class BigQueryStore:
    """Answers each rule's question with one small parameterised SELECT."""

    def __init__(self, client):
        self._client = client

    def _rows(self, name, d):
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", d)])
        return [dict(r.items()) for r in self._client.query(SQL[name], job_config=config).result()]

    def collect_ok(self, d):
        return self._rows("collect_ok", d)[0]["n"] > 0

    def observations(self, d):
        return {r["market"]: r["n"] for r in self._rows("observations", d)}

    def published_markets(self, d):
        return {r["market"] for r in self._rows("published_markets", d)}

    def model_usd(self, d):
        return float(self._rows("model_usd", d)[0]["usd"] or 0)

    def route_keys(self, d):
        return self._rows("route_keys", d)

    def ask_runs(self, d):
        row = self._rows("ask_runs", d)[0]
        return row["total"], row["failed"]

    def seeds_latest(self, d):
        rows = self._rows("seeds_latest", d)
        return rows[0] if rows else None

    def detect_latest(self, d):
        rows = self._rows("detect_latest", d)
        return rows[0] if rows else None

    def brief_state(self, d):
        return {r["market"]: {"status": r["status"], "cards": r["cards"] or 0, "held": r["held"] or 0}
                for r in self._rows("brief_state", d)}

    def understand_latest(self, d):
        rows = self._rows("understand_latest", d)
        return rows[0] if rows else None

    def stage_latest(self, d):
        return self._rows("stage_latest", d)

    def watchdog_last(self, d):
        return self._rows("watchdog_last", d)[0]["last_at"]

    def fired_today(self, d):
        return {r["name"] for r in self._rows("fired_today", d)}


if __name__ == "__main__":
    sys.exit(main())
