-- label_shared_members.sql: for each day, the label changes between consecutive days of one item and market, and how
-- many of them share at least 1, 2 and 3 member posts (C4 v3 appendix L, question Q14). Run on the retained clusters
-- during the shadow period; any change to MIN_SHARED_MEMBERS is a new decision. Parameters @start and @end are
-- the first and last label-change day. Read only.
WITH pairs AS (
  SELECT t.cluster_date, t.cluster_id, p.cluster_id prior_id
  FROM {core}.clusters t
  JOIN {core}.clusters p
    ON p.item_id = t.item_id AND p.market = t.market AND p.cluster_id != t.cluster_id
   AND p.cluster_date = DATE_SUB(t.cluster_date, INTERVAL 1 DAY)
  WHERE t.cluster_date BETWEEN @start AND @end AND t.match_kind IN ('match', 'recurrence')
    AND IFNULL(t.label, '') != IFNULL(p.label, '')),
shared AS (
  SELECT q.cluster_date, q.cluster_id,
    (SELECT COUNT(*) FROM {core}.cluster_members m1
     JOIN {core}.cluster_members m2 ON m2.post_id = m1.post_id AND m2.cluster_id = q.prior_id
     WHERE m1.cluster_id = q.cluster_id) shared_members
  FROM pairs q)
SELECT s.cluster_date, COUNT(*) label_changes,
  COUNTIF(s.shared_members >= 1) at_1, COUNTIF(s.shared_members >= 2) at_2, COUNTIF(s.shared_members >= 3) at_3
FROM shared s GROUP BY s.cluster_date ORDER BY s.cluster_date
