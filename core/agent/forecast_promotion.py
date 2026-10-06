"""The one read of promoted forecasts (TRUST.md K9; DATA.md section 6).

Forecasts stay hidden from users and the agent until they beat persistence. Monday's learn run writes
promotion_eligible per rule, target and horizon to intelligence_42_agent.forecast_score, append-only, the newest
scored_at current. This fixed query returns the open forecasts, among the ids asked for, whose rule, target and
horizon has a current forecast_score row with promotion_eligible true and that themselves predict arrival, since
every publishable line says the item is expected to arrive; a forecast against arrival stays cut. It never returns
prob or the outcome, the model never writes or sees it, and the agent's sql_query still refuses all three forecast
tables. Any failure promotes nothing, so the forecast stays cut. A promoted forecast is published only as
publishable_line of its stored row, so a logged statement about another item, target or horizon never rides on a
promoted cohort.
"""

from __future__ import annotations

from datetime import date

from core.agent.tools.sql_query import MAX_BYTES_BILLED

# Forecast ids are sha256 hex, so a comma list is exact; the warehouse takes scalar parameters only. The item's name
# is its current label in v_items_today, read for the stored item and market, never the model's words.
PROMOTED_SQL = (
    "SELECT c.forecast_id, c.item_id, c.market, c.rule, c.target, c.horizon, c.issue_date, c.resolve_date, "
    "l.label, p.run_id AS score_run_id, p.scored_at "
    "FROM intelligence_42_core.v_forecasts_current c "
    "JOIN (SELECT s.rule, s.target, s.horizon, s.promotion_eligible, s.run_id, s.scored_at "
    "FROM intelligence_42_agent.forecast_score s "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY s.rule, s.target, s.horizon "
    "ORDER BY s.scored_at DESC, s.run_id DESC) = 1) p "
    "ON p.rule = c.rule AND p.target = c.target AND p.horizon = c.horizon "
    "LEFT JOIN (SELECT t.item_id, t.market, t.label FROM intelligence_42_agent.v_items_today t "
    "WHERE t.metric_date BETWEEN DATE_SUB(@today, INTERVAL 16 DAY) AND @today "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY t.item_id, t.market ORDER BY t.metric_date DESC) = 1) l "
    "ON l.item_id = c.item_id AND l.market = c.market "
    "WHERE p.promotion_eligible IS TRUE AND c.predicted_arrival IS TRUE AND c.observed_arrival IS NULL AND c.resolve_date >= @today "
    "AND c.forecast_id IN UNNEST(SPLIT(@forecast_ids, ',')) "
    "ORDER BY c.forecast_id"
)
MARKET_NAMES = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}
PERIODS = {7: "the next week", 14: "the next fortnight"}
# What each target means (DATA.md section 6), worded with no numeral so the line needs no query of its own.
TEMPLATES = {
    "reach_rising": "{label} is expected to keep rising in {market} over {period}.",
    "cross_market": "{label} is expected to spread from {market} to another market over {period}.",
    "persist_50": "{label} is expected to hold at least half its current daily pace in {market} over {period}.",
}


def publishable_line(row: dict) -> str | None:
    """The only line a promoted forecast may be published as, built from its stored row: the item's label, the market,
    what the target means and the horizon. None when the item has no name of its own or the row is not one the
    template knows."""
    label = str(row.get("label") or "").strip()
    template = TEMPLATES.get(row.get("target"))
    market = MARKET_NAMES.get(row.get("market"))
    period = PERIODS.get(row.get("horizon"))
    if not label or label == row.get("item_id") or not (template and market and period):
        return None
    return template.format(label=label, market=market, period=period)


def promoted_forecasts(warehouse, forecast_ids, today: date) -> dict:
    """forecast_id -> its row, for each asked-for id that is open on today and in a promoted cohort. No ids, no read."""
    wanted = sorted({f for f in forecast_ids or [] if isinstance(f, str) and f and "," not in f})
    if not wanted:
        return {}
    try:
        rows = warehouse.run(PROMOTED_SQL, {"forecast_ids": ",".join(wanted), "today": today}, MAX_BYTES_BILLED)
    except Exception:  # the warehouse is a system boundary; a failed read keeps every forecast held
        return {}
    return {r["forecast_id"]: r for r in rows or []
            if isinstance(r, dict) and r.get("forecast_id") in wanted}
