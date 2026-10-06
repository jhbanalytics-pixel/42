# Read-only. py -3.13 check_brief_rerun.py   (needs google-cloud-bigquery and gcloud ADC on this PC)
from google.cloud import bigquery

P = "ogilvy-trends-v2"
RUNS = ["brief-20261002-8e6d18f0681a", "brief-20261002-7eb2fbfc5e53"]
D = "2026-10-02"
c = bigquery.Client(project=P)
cfg = lambda params: bigquery.QueryJobConfig(maximum_bytes_billed=10**9, query_parameters=params)
RUN_P = [bigquery.ArrayQueryParameter("runs", "STRING", RUNS), bigquery.ScalarQueryParameter("d", "DATE", D)]

def show(title, sql, params=RUN_P):
    print(f"\n== {title}")
    job = c.query(sql, job_config=cfg(params))
    for r in job.result():
        print(dict(r))
    print(f"(bytes billed {job.total_bytes_billed})")

# 1. The brief runs' counts: ingested = counts.ingest.by_market.<M>.posts (confirm finds parsed, cached ones too).
show("runs", f"""
SELECT run_id, status, started_at, finished_at,
  JSON_VALUE(counts, '$.credits') credits, JSON_VALUE(counts, '$.cards') cards, JSON_VALUE(counts, '$.held') held,
  TO_JSON_STRING(JSON_QUERY(counts, '$.ingest')) ingest, JSON_VALUE(counts, '$.confirm') confirm_note,
  TO_JSON_STRING(JSON_QUERY(counts, '$.regrown')) regrown,
  TO_JSON_STRING(JSON_QUERY(counts, '$.platforms_found')) platforms_found
FROM `{P}.intelligence_42_agent.runs`
WHERE run_date = @d AND stage = 'brief'
ORDER BY started_at, finished_at""")

# 2. Credit ledger for the day by job and lane (and run): confirm cap is 150 a day per trend_date.
show("credit_ledger by job, lane", f"""
SELECT job, lane, COUNT(*) rows_, SUM(calls) calls, COUNTIF(cache_hit) cache_hits,
  ROUND(SUM(credits_charged), 1) credits
FROM `{P}.intelligence_42_core.credit_ledger`
WHERE trend_date = @d GROUP BY job, lane ORDER BY job, lane""")
show("credit_ledger confirm share by run, market, route", f"""
SELECT run_id, market, route, COUNT(*) rows_, COUNTIF(cache_hit) cache_hits, ROUND(SUM(credits_charged), 1) credits
FROM `{P}.intelligence_42_core.credit_ledger`
WHERE trend_date = @d AND job = 'confirm' GROUP BY run_id, market, route ORDER BY run_id, market, route""")

# 3. The vanished items: market scope (core/brief/sql/market_scope.sql) as run 2 read it, and without the
#    confirm finds the two reruns linked (lane confirm, run_id in RUNS). market vs global decides whether the
#    item is in the brief at all (core/brief/job.py:635).
SCOPE = f"""
WITH items AS (
  SELECT item_id, label, m market FROM `{P}.intelligence_42_core.cultural_map`, UNNEST(['ZA','NG','KE']) m
  WHERE valid_to IS NULL AND CONCAT(m, ':', LOWER(TRIM(IFNULL(label, canonical_key), '#'))) IN (
    'NG:islamicvideo', 'NG:islamicreminder', 'NG:creatorsearchinsights', 'ZA:madlangacommission')
), seen AS (
  SELECT i.item_id, i.label, i.market, po.post_id, LOGICAL_OR(po.lane_class IN ('unbiased_rank','panel')) measured
  FROM items i
  JOIN `{P}.intelligence_42_core.post_items` pi ON pi.item_id = i.item_id
  JOIN `{P}.intelligence_42_core.post_observations` po ON po.post_id = pi.post_id AND po.market = i.market
  WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
    AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')
    AND (@with_reruns OR NOT (IFNULL(po.lane, '') = 'confirm' AND po.run_id IN UNNEST(@runs)))
  GROUP BY 1, 2, 3, 4
), ranked AS (
  SELECT s.*, ps.geo_market, ps.geo_confidence, ps.geo_source, IFNULL(ps.engagement, 0) eng,
    ROW_NUMBER() OVER (PARTITION BY s.item_id, s.market, IFNULL(ps.creator_id, ps.post_id)
                       ORDER BY s.measured DESC, IFNULL(ps.engagement, 0) DESC, ps.post_id) cr
  FROM seen s JOIN `{P}.intelligence_42_core.posts` ps ON ps.post_id = s.post_id
  WHERE DATE(ps.published_at, CASE s.market WHEN 'ZA' THEN 'Africa/Johannesburg' WHEN 'NG' THEN 'Africa/Lagos'
        ELSE 'Africa/Nairobi' END) BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
), src AS (
  SELECT DISTINCT v.post_id, sg.source_market
  FROM `{P}.intelligence_42_core.v_post_source_markets` v, UNNEST(v.source_sightings) sg
  WHERE sg.obs_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
), pack AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY item_id, market ORDER BY measured DESC, eng DESC, post_id) pr
  FROM ranked WHERE cr <= 2
)
SELECT label, pack.market, COUNT(DISTINCT pack.post_id) total_posts7,
  COUNT(DISTINCT IF(measured, pack.post_id, NULL)) measured,
  COUNT(DISTINCT IF((geo_market = pack.market AND IFNULL(geo_confidence, 0) >= 0.7
           AND geo_source IN ('ext_region', 'home_market', 'place_mention')) OR src.post_id IS NOT NULL,
           pack.post_id, NULL)) market_posts7
FROM pack LEFT JOIN src ON src.post_id = pack.post_id AND src.source_market = pack.market
WHERE pr <= 12 GROUP BY label, pack.market ORDER BY 2, 1"""
for flag in (True, False):
    try:
        show(f"market scope, with the reruns' confirm finds = {flag} (market if market_posts7*2 > total_posts7)",
             SCOPE, RUN_P + [bigquery.ScalarQueryParameter("with_reruns", "BOOL", flag)])
    except Exception as e:  # over 1 GB or a label that differs: say so, the first two answers stand alone
        print(f"scope query skipped: {type(e).__name__}: {e}")

# 4. Confirm finds the reruns wrote and linked, by run and market.
show("confirm finds written by the reruns", f"""
SELECT po.run_id, po.market, po.platform, COUNT(DISTINCT po.post_id) posts,
  COUNT(DISTINCT IF(pi.post_id IS NOT NULL, po.post_id, NULL)) linked_to_searched_item
FROM `{P}.intelligence_42_core.post_observations` po
LEFT JOIN `{P}.intelligence_42_core.post_items` pi ON pi.post_id = po.post_id AND pi.item_id = po.seed_key
WHERE po.observed_date = @d AND po.lane = 'confirm' AND po.run_id IN UNNEST(@runs)
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")
