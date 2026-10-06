"""Centroids for hashtag and sound items (BUILD.md 3.3), so analogues match them on meaning, not label words.

Clustering gives topic items a centroid (core/understand/cluster.py). A hashtag or sound item gets one here: the mean
of the stored embeddings of its posts sighted in the WINDOW_DAYS ending d, once at least MIN_POSTS of them have a
vector. It runs in detect after aggregate, since aggregate writes post_items for d's posts and understand has
embedded them by then. No model is called: the embeddings are read from post_enrichment as they are.

cultural_map is versioned, as cluster_write.sql versions a topic: one MERGE closes the item's open row (valid_to set
to now, nothing else on it changes) and opens a new version (valid_from now) that copies every other column and
carries the new centroid. The close is a compare-and-swap on the open row's valid_from, so a row another writer
replaced in between is never closed. Nothing is deleted. Topic items, creators and other kinds are never read or
written, nor is a 'generic' hashtag: it rides on every kind of post (stoplist.yaml), so its mean sits near every
item and would rank first as an analogue of anything.

An item is written only when one of its posts was sighted on d, so a quiet item does not gain a version a day as its
window slides, and only when the rounded mean differs from the stored centroid, so a rerun of d writes nothing.
"""

from google.cloud import bigquery

from .sqlrun import AGENT, CORE, render

# Four weeks: long enough to hold a tag's last wave and its weekday rhythm, short enough that the centroid follows
# what the tag means now rather than what it meant a season ago.
WINDOW_DAYS = 28
# A mean of fewer posts is one or two posts' meaning, not the tag's; five keeps a single odd post at a fifth or less.
MIN_POSTS = 5
DECIMALS = 6     # as cluster.DECIMALS rounds a topic centroid

COLUMNS = ("item_id, kind, canonical_key, label, aliases, parent_item_id, centroid, first_seen, first_seen_market, "
           "first_seen_platform, last_seen, recurrences, lifecycle, status, rejected_until")

NOW_SQL = "SELECT CURRENT_TIMESTAMP() AS now"

# Parameters: @d DATE, @back INT64 (WINDOW_DAYS - 1), @min_posts INT64. post_enrichment holds an embed row and an
# enrich row per post; only a row with a vector is read, and one per post, the lowest first element first, as
# understand's cluster_posts.sql reads it.
ITEM_CENTROIDS_SQL = f"""
MERGE {{core}}.cultural_map t
USING (
  WITH win AS (
    SELECT o.post_id, MAX(o.observed_date) = @d AS today
    FROM {{core}}.post_observations o
    WHERE o.observed_date BETWEEN DATE_SUB(@d, INTERVAL @back DAY) AND @d
    GROUP BY o.post_id),
  cur AS (
    SELECT m.* FROM {{core}}.cultural_map m
    WHERE m.valid_to IS NULL AND m.kind IN ('hashtag', 'sound') AND IFNULL(m.status, '') != 'generic'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY m.item_id ORDER BY m.valid_from DESC) = 1),
  linked AS (
    SELECT DISTINCT pi.item_id, pi.post_id FROM {{core}}.post_items pi
    WHERE pi.item_id IN (SELECT cur.item_id FROM cur) AND pi.post_id IN (SELECT win.post_id FROM win)),
  vectors AS (
    SELECT e.post_id, e.embedding FROM {{core}}.post_enrichment e
    WHERE ARRAY_LENGTH(e.embedding) > 0 AND e.post_id IN (SELECT linked.post_id FROM linked)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY e.post_id ORDER BY e.embedding[OFFSET(0)]) = 1),
  enough AS (
    SELECT l.item_id FROM linked l
    JOIN win w ON w.post_id = l.post_id
    JOIN vectors v ON v.post_id = l.post_id
    GROUP BY l.item_id
    HAVING COUNT(*) >= @min_posts AND LOGICAL_OR(w.today)
      AND MIN(ARRAY_LENGTH(v.embedding)) = MAX(ARRAY_LENGTH(v.embedding))),
  dims AS (
    SELECT l.item_id, i, ROUND(AVG(x), {DECIMALS}) AS x
    FROM linked l
    JOIN enough g ON g.item_id = l.item_id
    JOIN vectors v ON v.post_id = l.post_id
    CROSS JOIN UNNEST(v.embedding) AS x WITH OFFSET AS i
    GROUP BY l.item_id, i),
  means AS (SELECT dm.item_id, ARRAY_AGG(dm.x ORDER BY dm.i) AS centroid FROM dims dm GROUP BY dm.item_id),
  changed AS (
    SELECT cur.item_id, cur.kind, cur.canonical_key, cur.label, cur.aliases, cur.parent_item_id,
      mn.centroid, cur.first_seen, cur.first_seen_market, cur.first_seen_platform, cur.last_seen, cur.recurrences,
      cur.lifecycle, cur.status, cur.rejected_until, cur.valid_from AS open_from
    FROM cur JOIN means mn ON mn.item_id = cur.item_id
    WHERE IFNULL(TO_JSON_STRING(cur.centroid), '') != TO_JSON_STRING(mn.centroid))
  SELECT c.item_id AS close_key, c.* FROM changed c
  UNION ALL
  SELECT CAST(NULL AS STRING) AS close_key, c.* FROM changed c
) s
ON t.item_id = s.close_key AND t.valid_to IS NULL AND t.valid_from = s.open_from
WHEN MATCHED THEN
  UPDATE SET valid_to = CURRENT_TIMESTAMP()
WHEN NOT MATCHED AND s.close_key IS NULL THEN
  INSERT ({COLUMNS}, valid_from, valid_to)
  VALUES (s.item_id, s.kind, s.canonical_key, s.label, s.aliases, s.parent_item_id, s.centroid, s.first_seen,
          s.first_seen_market, s.first_seen_platform, s.last_seen, s.recurrences, s.lifecycle, s.status,
          s.rejected_until, CURRENT_TIMESTAMP(), NULL)
"""

# The versions the MERGE opened: open hashtag and sound rows with a centroid, opened at or after @since.
WRITTEN_SQL = """
SELECT COUNT(*) AS n FROM {core}.cultural_map m
WHERE m.valid_to IS NULL AND m.kind IN ('hashtag', 'sound') AND ARRAY_LENGTH(m.centroid) > 0
  AND m.valid_from >= @since
"""


def _run(client, sql, params, core, agent):
    job = client.query(render(sql, core, agent), job_config=bigquery.QueryJobConfig(query_parameters=params))
    return [dict(row.items()) for row in job.result()]


def run_item_centroids(client, d, core=CORE, agent=AGENT):
    """Write d's hashtag and sound centroids into cultural_map; returns the count of new versions."""
    since = _run(client, NOW_SQL, [], core, agent)[0]["now"]
    _run(client, ITEM_CENTROIDS_SQL, [bigquery.ScalarQueryParameter("d", "DATE", d),
                                      bigquery.ScalarQueryParameter("back", "INT64", WINDOW_DAYS - 1),
                                      bigquery.ScalarQueryParameter("min_posts", "INT64", MIN_POSTS)], core, agent)
    written = _run(client, WRITTEN_SQL, [bigquery.ScalarQueryParameter("since", "TIMESTAMP", since)], core, agent)
    return {"centroids": written[0]["n"]}
