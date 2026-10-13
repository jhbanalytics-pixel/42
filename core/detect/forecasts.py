"""Engine forecasts, detect side (DATA.md section 6; TRUST.md K9; ENGINE.md schedule, detect at 05:00 and
forecast scoring in Monday's learn).

run_forecasts(client, d, run_id) does two things each day and only appends to intelligence_42_agent.forecasts:
- Resolve: every open forecast whose window (issue_date + 1 to resolve_date) closed before d gets one new row,
  the issue row with observed_arrival set, read through the latest good runs. Validity is per market, platform,
  route and day (TRUST.md A4), so a forecast stays open until every window day is good for every market and
  platform it reads: every route of that platform in the measured lanes (unbiased_rank, unbiased_counter, panel)
  valid in the day's latest good collect run, and a good detect and stats run for the day. Search, placebo and
  watchlist routes never hold a forecast. In its own market every target reads the main series' platform (G1) and
  every platform the item has a series on there from the issue day to resolve_date, because state.sql derives
  Rising from all of them. cross_market also reads, in each other market, every platform the item has a series on
  there and the main platform where that platform has measured health rows in the window (Apple Music, for one,
  is collected in ZA only). persist_50 also needs a value of the main series on every day from issue_date - 6 to
  resolve_date, so a NULL or missing day leaves it open and never lowers v7. A forecast with no main series stays
  open. Agent forecasts (log_forecast, rule ask_v1) are resolved the same way.
  Known gaps, left open: significance in another market or in GLOBAL that feeds Rising in state.sql (its other
  CTE, 3 days) is not checked for validity (Albert's call), and aggregate and coaction runs are not required for a
  window day to count as good.
- Issue: 7-day and 14-day forecasts for the three targets, for every Emerging and Rising item in ZA, NG and KE
  plus a control sample of about 10% of the other items with a state (in_control), each with its target's
  persistence baseline. GLOBAL never gets a forecast. A forecast already issued on d is not issued again.
  Watched means item_state.state or item_state.base_state is emerging or rising, so a Seasonal or Recurring row
  resting on Emerging or Rising is watched too. base_state (DATA.md 3.7) is the state before that override; it
  is NULL on rows written before the column existed, and those Seasonal and Recurring rows are not watched
  (they can still fall in the control sample). The features and persistence baselines still read state.

Targets, persistence baselines and observed arrival (DATA.md section 6):
- reach_rising: persistence, Rising or Peaking now; observed, Rising or Peaking on a window day.
- cross_market: persistence, markets_hot >= 2 now; observed, another market (not GLOBAL) with a significant day
  (q <= 0.05) in the window, not held as Likely coordinated there that day.
- persist_50: persistence, always; observed, v7 of the issue day's main series stays at half the issue day's v7
  or more on every window day.

forecast_id is the sha256 of item, market, target, issue date, horizon and rule, the key log_forecast uses, so an
engine row and an agent row never share an id. The current row of a forecast is v_forecasts_current in
sql/forecasts.sql.

Hidden until promoted (TRUST.md K9): forecasts must stay hidden from users and the agent until weekly_score says
they beat persistence. Nothing here enforces that. No brief or app reader of intelligence_42_agent.forecasts or
v_forecasts_current exists, and the agent's sql_query refuses both and forecast_score. Monday's learn job
(core/detect/learn.py) records promotion_eligible per rule, target and horizon in
intelligence_42_agent.forecast_score through core.eval.forecast_score, which applies the same rule as score() below
to every rule, adds skill, and is the scorer the weekly quality score reads. The one reader of promotion_eligible is
core/agent/forecast_promotion.py: Ask's K9 check publishes a forecast logged in the run only when its rule, target
and horizon is promoted.

weekly_score imports the packaged core.eval.forecast_cohort helper and scores the engine rule alone; learn does not
call it.

The rule (RULE, logistic_v1) is a logistic rule: prob = 1 / (1 + exp(-(intercept + sum of coefficient x
feature))), predicted_arrival = prob >= 0.5. DATA.md, ENGINE.md, TRUST.md and SPEC.md give no features or
coefficients, so COEFFICIENTS is a hand-set starting prior, to be replaced by BQML LOGISTIC_REG once 500 forecasts
have resolved (DATA.md section 6). Features, from item_state on the issue day and the main series' series_test row:
- rising_now: 1 when the state is Rising or Peaking (the reach_rising baseline)
- emerging: 1 when the state is Emerging
- fading: 1 when the state is Fading
- sig_days3: significant days of the last 3 (0 to 3)
- hot_other: markets_hot - 1, at least 0 (other markets significant in 14 days)
- creators: ln(1 + creators3)
- vel: the main series' velocity, clipped to [-2, 2], 0 when unknown
- worth: worth_pct, 0.5 when unknown
- long: 1 for the 14-day horizon
"""

import hashlib
import math
import re
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from . import aggregate, sqlrun
from .sqlrun import AGENT, CORE, query

SQL = Path(__file__).parent / "sql" / "forecasts.sql"
_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)

RULE = "logistic_v1"
TARGETS = ("reach_rising", "cross_market", "persist_50")
HORIZONS = (7, 14)
WATCHED = ("emerging", "rising")
CONTROL_SHARE = 0.10
LOOKBACK_DAYS = 45                    # a forecast still open this long after issue is left unresolved
MINIMUM_COMPARABLE_SAMPLE = 200       # DATA.md section 6
CHUNK = 2000

FEATURES = ("rising_now", "emerging", "fading", "sig_days3", "hot_other", "creators", "vel", "worth", "long")
COEFFICIENTS = {                      # starting prior, set by hand; see the module docstring
    "reach_rising": {"intercept": -2.0, "rising_now": 2.5, "emerging": 0.8, "sig_days3": 0.4, "creators": 0.3,
                     "vel": 0.8, "worth": 1.0, "long": 0.4},
    "cross_market": {"intercept": -3.0, "hot_other": 1.8, "rising_now": 0.6, "sig_days3": 0.2, "creators": 0.3,
                     "worth": 0.8, "long": 0.4},
    "persist_50": {"intercept": 0.0, "rising_now": 0.8, "emerging": 0.4, "fading": -1.0, "sig_days3": 0.3,
                   "creators": 0.2, "vel": 1.0, "worth": 0.5, "long": -0.6},
}

FORECAST_FIELDS = (("forecast_id", "STRING"), ("item_id", "STRING"), ("market", "STRING"), ("target", "STRING"),
                   ("issue_date", "DATE"), ("horizon", "INT64"), ("rule", "STRING"), ("prob", "FLOAT64"),
                   ("predicted_arrival", "BOOL"), ("persistence_arrival", "BOOL"), ("resolve_date", "DATE"),
                   ("observed_arrival", "BOOL"))


def statements():
    """The named statements of forecasts.sql, dataset placeholders left in."""
    out = {}
    for piece in sqlrun.split(SQL.read_text(encoding="utf-8")):
        m = _NAME.search(piece)
        out[m.group(1)] = sqlrun._strip_leading_comments(piece[m.end():])
    return out


def forecast_id(item_id, market, target, issue_date, horizon, rule=RULE):
    key = f"{item_id}|{market}|{target}|{issue_date.isoformat()}|{horizon}|{rule}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def in_control(item_id, d):
    """The seeded control draw: the first 32 bits of sha256(item_id|date) below CONTROL_SHARE of their range."""
    digest = hashlib.sha256(f"{item_id}|{d.isoformat()}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 16 ** 8 < CONTROL_SHARE


def features(row, horizon):
    st = row.get("state")
    vel = row.get("main_vel")
    worth = row.get("worth_pct")
    return {"rising_now": float(st in ("rising", "peaking")), "emerging": float(st == "emerging"),
            "fading": float(st == "fading"), "sig_days3": float(row.get("sig_days3") or 0),
            "hot_other": float(max((row.get("markets_hot") or 0) - 1, 0)),
            "creators": math.log1p(row.get("creators3") or 0),
            "vel": 0.0 if vel is None else max(-2.0, min(2.0, float(vel))),
            "worth": 0.5 if worth is None else float(worth), "long": float(horizon == 14)}


def prob(target, horizon, row):
    coef = COEFFICIENTS[target]
    x = features(row, horizon)
    z = coef["intercept"] + sum(c * x[f] for f, c in coef.items() if f != "intercept")
    return 1.0 / (1.0 + math.exp(-z))


def persistence(target, row):
    if target == "reach_rising":
        return row.get("state") in ("rising", "peaking")
    if target == "cross_market":
        return (row.get("markets_hot") or 0) >= 2
    return True


def watched(c):
    return c.get("state") in WATCHED or c.get("base_state") in WATCHED


def issue_rows(d, candidates):
    """Six rows (three targets by two horizons) per watched or control candidate; others get none."""
    out = []
    for c in candidates:
        if c["market"] not in ("ZA", "NG", "KE"):
            continue
        if not watched(c) and not in_control(c["item_id"], d):
            continue
        for target in TARGETS:
            for h in HORIZONS:
                p = prob(target, h, c)
                out.append({"forecast_id": forecast_id(c["item_id"], c["market"], target, d, h),
                            "item_id": c["item_id"], "market": c["market"], "target": target, "issue_date": d,
                            "horizon": h, "rule": RULE, "prob": p, "predicted_arrival": p >= 0.5,
                            "persistence_arrival": persistence(target, c),
                            "resolve_date": d + timedelta(days=h), "observed_arrival": None})
    return out


def rows_param(rows):
    return aggregate._struct_array("rows", rows, FORECAST_FIELDS)


def _append(client, rows, core, agent):
    for i in range(0, len(rows), CHUNK):
        aggregate._run(client, statements()["append"], [rows_param(rows[i:i + CHUNK])], core, agent)


def apply_current_view(client, core=CORE, agent=AGENT):
    client.query(sqlrun.render(statements()["current_view"], core, agent)).result()


def run_forecasts(client, d, run_id, core=CORE, agent=AGENT):
    """Resolve the forecasts whose window closed before d, then issue d's forecasts from the item_state rows of
    detect run run_id. Returns counts."""
    st = statements()
    apply_current_view(client, core, agent)
    since = d - timedelta(days=LOOKBACK_DAYS)
    resolved = query(client, st["resolve"], {"d": d, "since": since}, core, agent)
    _append(client, resolved, core, agent)
    cands = query(client, st["candidates"], {"d": d, "run_id": run_id}, core, agent)
    done = {r["forecast_id"] for r in query(client, st["issued"], {"d": d}, core, agent)}
    rows = issue_rows(d, cands)
    new = list({r["forecast_id"]: r for r in rows if r["forecast_id"] not in done}.values())
    _append(client, new, core, agent)
    seen = {(c["item_id"], c["market"]) for c in cands if watched(c)}
    control = {(r["item_id"], r["market"]) for r in rows} - seen
    return {"candidates": len(cands), "watched": len(seen), "control": len(control), "issued": len(new),
            "resolved": len(resolved)}


def _brier(pairs):
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def score(rows, minimum_comparable_sample=MINIMUM_COMPARABLE_SAMPLE):
    """Score each target and horizon cohort through forecast_cohort.review_arrival_cohort, with Brier scores for
    the rule and for persistence over the resolved rows. A cohort is promotion eligible when review_arrival_cohort
    says so (fewer errors than persistence over at least the minimum comparable sample) and its Brier score is
    below persistence's. promotion_eligible is true only when there are cohorts and every one is eligible."""
    from core.eval.forecast_cohort import review_arrival_cohort

    groups = defaultdict(list)
    for r in rows:
        groups[(r["target"], r["horizon"])].append(r)
    cohorts = {}
    for (target, horizon), rs in sorted(groups.items()):
        review = review_arrival_cohort([{
            "forecast_id": r["forecast_id"], "forecast_issued": f"{r['issue_date'].isoformat()}T00:00:00Z",
            "predicted_arrival": r["predicted_arrival"], "persistence_arrival": r["persistence_arrival"],
            "observed_arrival": r["observed_arrival"], "horizon": horizon,
            "provenance": {"rule": r["rule"], "target": target, "item_id": r["item_id"], "market": r["market"]},
        } for r in rs], minimum_comparable_sample=minimum_comparable_sample)
        done = [r for r in rs if r["observed_arrival"] is not None]
        brier = _brier([(r["prob"], float(r["observed_arrival"])) for r in done])
        base = _brier([(float(r["persistence_arrival"]), float(r["observed_arrival"])) for r in done])
        review["brier"], review["persistence_brier"] = brier, base
        review["promotion_eligible"] = bool(review["promotion_eligible"] and brier is not None and brier < base)
        cohorts[f"{target}/{horizon}"] = review
    return {"cohorts": cohorts,
            "promotion_eligible": bool(cohorts) and all(c["promotion_eligible"] for c in cohorts.values())}


def weekly_score(client, d, core=CORE, agent=AGENT):
    """The weekly score of the engine rule's forecasts whose window closed before d. The scheduled weekly scoring
    is core.eval.forecast_score, run by Monday's learn job."""
    return score(query(client, statements()["scoring"], {"d": d, "rule": RULE}, core, agent))
