-- The current row of every forecast whose window closed before @d, engine (logistic_v1) and Ask (ask_v1) alike.
-- The current row is the resolution row once one exists, else the issue row: the order L2's v_forecasts_current
-- uses, read from the table itself so scoring does not depend on that view being applied.
SELECT f.forecast_id, f.item_id, f.market, f.target, f.issue_date, f.horizon, f.rule, f.prob,
  f.predicted_arrival, f.persistence_arrival, f.resolve_date, f.observed_arrival
FROM `ogilvy-trends-v2.intelligence_42_agent.forecasts` AS f
WHERE f.forecast_id IS NOT NULL AND f.resolve_date < @d
QUALIFY ROW_NUMBER() OVER (PARTITION BY f.forecast_id ORDER BY f.observed_arrival IS NULL, f.issue_date) = 1
