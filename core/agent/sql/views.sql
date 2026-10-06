-- The agent's authorised views (DATA.md section 7). Applied in order by core/agent/apply_views.py.
-- Each statement ends with a semicolon; comments are whole lines.

-- item_state rows of the latest good detect run per metric_date (DATA.md section 3.1): the newest runs row with
-- stage 'detect' and status 'ok' for that run_date. label is the item's current cultural_map label (the newest
-- valid_from if two rows are still open), or item_id when the map has none.
CREATE VIEW IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.v_items_today` AS
SELECT s.*, COALESCE(cm.label, s.item_id) AS label, g.finished_at AS as_of
FROM `ogilvy-trends-v2.intelligence_42_core.item_state` s
JOIN (
  SELECT r.run_date, r.run_id, r.finished_at
  FROM `ogilvy-trends-v2.intelligence_42_agent.runs` r
  WHERE r.stage = 'detect' AND r.status = 'ok'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY r.run_date ORDER BY r.finished_at DESC, r.run_id DESC) = 1
) g
  ON g.run_date = s.metric_date AND g.run_id = s.run_id
LEFT JOIN (
  SELECT m.item_id, m.label
  FROM `ogilvy-trends-v2.intelligence_42_core.cultural_map` m
  WHERE m.valid_to IS NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY m.item_id ORDER BY m.valid_from DESC) = 1
) cm
  ON cm.item_id = s.item_id;

-- Every saved finding, with a flag for whether its review date has not yet passed.
CREATE VIEW IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.v_prior_findings` AS
SELECT f.finding_id, f.question, f.answer, f.as_of, f.claims, f.valid_from, f.valid_to, f.status,
  (f.valid_to IS NULL OR f.valid_to > CURRENT_TIMESTAMP()) AS `current`
FROM `ogilvy-trends-v2.intelligence_42_agent.findings` f;
