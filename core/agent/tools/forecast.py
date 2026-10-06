"""The agent's log_forecast tool (AGENT.md, Tools; DATA.md sections 2 and 6).

One guarded append to intelligence_42_agent.forecasts, beside save_finding the agent's only write. The forecast is
scored later against persistence, so the row carries the persistence baseline read from v_items_today on the issue
date: reach_rising holds when the item is Rising or Peaking now, cross_market when markets_hot is 2 or more now, and
persist_50 always.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta

from core.agent.answer import normalise
from core.agent.checks import SPELLED, _text_breaches
from core.agent.context import Refused, RunContext
from core.agent.tools.dates import resolve_dates
from core.agent.tools.sql_query import Warehouse, _run_query, sql_query

FORECASTS_TABLE = "intelligence_42_agent.forecasts"
RULE = "ask_v1"
TARGETS = ("reach_rising", "cross_market", "persist_50")
HORIZONS = (7, 14)
MARKETS = ("ZA", "NG", "KE")
REACH_STATES = ("rising", "peaking")
BASELINE_DAYS = 2
# A figure with the letters touching it: 7k, 14x, 0.6m, R7 and 7days are never the bare horizon or probability.
_NUMERAL = re.compile(r"(?P<prefix>[^\W\d_]*)(?P<num>\d+(?:[.,]\d+)*|\.\d+)(?P<pct>\s*%)?(?P<suffix>[^\W\d_]*)")


def _persistence(target: str, row: dict) -> bool | None:
    if target == "persist_50":
        return True
    if target == "reach_rising":
        state = row.get("state")
        return None if state is None else str(state).lower() in REACH_STATES
    hot = row.get("markets_hot")
    return None if hot is None else int(hot) >= 2


def _stray_numerals(statement: str, horizon: int, probability: float) -> list[str]:
    """Numerals other than the bare horizon, the probability as a decimal, or the probability as a percent. A figure
    with a letter touching it is always stray, and so is every spelled figure the checks read (a million, forty
    creators, one in five, double the)."""
    stray = []
    for match in _NUMERAL.finditer(statement):
        number, percent = match.group("num"), bool(match.group("pct"))
        value = float(number) if "," not in number else None
        bare = not (match.group("prefix") or match.group("suffix"))
        if bare and value is not None and (abs(value - probability * 100) < 1e-6 if percent
                                           else value in (horizon, probability)):
            continue
        stray.append(match.group(0).strip())
    return stray + [m.group(0) for m in SPELLED.finditer(normalise(statement))]


def log_forecast(ctx: RunContext, warehouse: Warehouse, writer, item_id, market, target,
                 horizon_days, probability, statement, evidence_ids) -> dict:
    if not isinstance(item_id, str) or not item_id.strip():
        raise Refused("item_id must name an item in v_items_today.")
    if market not in MARKETS:
        raise Refused(f"market must be one of {', '.join(MARKETS)}; got {market!r}.")
    if target not in TARGETS:
        raise Refused(f"target must be one of {', '.join(TARGETS)}; got {target!r}.")
    if type(horizon_days) is float and horizon_days in HORIZONS:
        horizon_days = int(horizon_days)  # a JSON 7 can arrive as 7.0 from a function-calling model
    if type(horizon_days) is not int or horizon_days not in HORIZONS:
        raise Refused(f"horizon_days must be 7 or 14; got {horizon_days!r}.")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not 0 < probability < 1:
        raise Refused(f"probability must be a number strictly between 0 and 1; got {probability!r}.")
    if not isinstance(statement, str) or not statement.strip():
        raise Refused("A forecast needs a statement.")
    if breaches := _text_breaches(statement):
        raise Refused(f"The statement breaches the trust gate ({', '.join(breaches)}). Describe audiences by "
                      f"language, place, interest, community or creator type.")
    if _stray_numerals(statement, horizon_days, probability):
        raise Refused("The statement may carry no numeral but the horizon in days and the probability, as a percent "
                      "or a decimal. Put any other figure in a claim with its query.")
    evidence_ids = list(evidence_ids or [])
    if not evidence_ids:
        raise Refused("A forecast needs at least one evidence id.")
    unknown = [e for e in evidence_ids if e not in ctx.evidence]
    if unknown:
        raise Refused(f"Evidence ids not seen in this run: {', '.join(map(str, unknown))}.")

    issue_date = resolve_dates("today", ctx.as_of)[1]
    key = f"{item_id}|{market}|{target}|{issue_date.isoformat()}|{horizon_days}|{RULE}"
    forecast_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
    if forecast_id in ctx.forecasts:
        return {"forecast_id": forecast_id}

    try:
        # Only the id: DATA.md section 6 keeps a stored forecast's prob and outcome from the agent.
        # The agent's sql_query refuses this table (TRUST.md K9); this one read may name it.
        logged = _run_query(ctx, warehouse, f"SELECT f.forecast_id FROM {FORECASTS_TABLE} f "
                            "WHERE f.forecast_id = @forecast_id LIMIT 1",
                            purpose=f"log_forecast dedup: {item_id}",
                            params={"forecast_id": forecast_id}, hidden_ok=(FORECASTS_TABLE,))["rows"]
    except Exception as e:
        raise Refused(f"Could not check {FORECASTS_TABLE} for this forecast ({type(e).__name__}), so it is not "
                      f"logged rather than risk a duplicate.") from None
    if any(r.get("forecast_id") == forecast_id for r in logged):
        # Recorded as already logged and nothing more, so a second call in this run needs no read.
        ctx.forecasts[forecast_id] = {"forecast_id": forecast_id, "already_logged": True}
        ctx.emit("log_forecast", forecast_id=forecast_id, item_id=item_id, target=target, horizon=horizon_days,
                 evidence_ids=evidence_ids, already_logged=True)
        return {"forecast_id": forecast_id}

    earliest = issue_date - timedelta(days=BASELINE_DAYS)
    sql = (
        "SELECT t.metric_date, t.state, t.markets_hot "
        "FROM intelligence_42_agent.v_items_today t "
        "WHERE t.item_id = @item_id AND t.market = @market "
        "AND t.metric_date BETWEEN @earliest AND @issue_date "
        "ORDER BY t.metric_date DESC LIMIT 1"
    )
    params = {"item_id": item_id, "market": market, "earliest": earliest, "issue_date": issue_date}
    rows = sql_query(ctx, warehouse, sql, purpose=f"log_forecast baseline: {item_id} {market}", params=params)["rows"]
    baseline = rows[0].get("metric_date") if rows else None
    if not rows or (baseline is not None and date.fromisoformat(str(baseline)[:10]) < earliest):
        raise Refused(f"{item_id} has no row in v_items_today for {market} within {BASELINE_DAYS} days of "
                      f"{issue_date}, so there is no current baseline to score against.")

    row = {
        "forecast_id": forecast_id,
        "item_id": item_id,
        "market": market,
        "target": target,
        "issue_date": issue_date.isoformat(),
        "horizon": horizon_days,
        "rule": RULE,
        "prob": float(probability),
        "predicted_arrival": probability >= 0.5,
        "persistence_arrival": _persistence(target, rows[0]),
        "resolve_date": (issue_date + timedelta(days=horizon_days)).isoformat(),
        "observed_arrival": None,
    }
    writer.insert(FORECASTS_TABLE, [row], row_ids=[forecast_id])
    ctx.forecasts[forecast_id] = {"forecast_id": forecast_id, "statement": statement, "evidence_ids": evidence_ids,
                                  "target": target, "horizon": horizon_days, "probability": float(probability)}
    ctx.emit("log_forecast", forecast_id=forecast_id, item_id=item_id, target=target, horizon=horizon_days,
             evidence_ids=evidence_ids)
    return {"forecast_id": forecast_id}
