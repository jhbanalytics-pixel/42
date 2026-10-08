"""The aggregate step of detect (DATA.md section 3.4): items from raw post fields, then item_daily for one day.

run_aggregate writes only by append (INSERT) and by MERGE on cultural_map, each in chunks of CHUNK rows.
It never creates, replaces or removes a table or a row.
"""

from pathlib import Path

from google.cloud import bigquery

from .items import is_generic, items_for_post
from .sqlrun import AGENT, CORE, render

AGGREGATE_SQL = Path(__file__).parent / "sql" / "aggregate.sql"

# Rows per post_items INSERT or cultural_map MERGE, so no one request nears BigQuery's 10 MB limit.
CHUNK = 5000

# Posts whose first sighting in a market is @d, with the raw fields items_for_post reads, and cluster_items: the
# items of @d's clusters the post is a member of (BUILD.md 2.2), each with an open cultural_map row.
FIRST_SIGHTED_SQL = """
SELECT ps.post_id, ps.platform, ps.hashtags, ps.sound_id, ps.creator_id, fs.market, fs.first_day, cl.cluster_items
FROM (SELECT po.post_id, po.market, MIN(po.observed_date) first_day
      FROM {core}.post_observations po WHERE po.observed_date <= @d
      GROUP BY po.post_id, po.market) fs
JOIN {core}.posts ps ON ps.post_id = fs.post_id
LEFT JOIN (SELECT mb.post_id, ARRAY_AGG(DISTINCT k.item_id ORDER BY k.item_id) cluster_items
           FROM {core}.cluster_members mb
           JOIN {core}.clusters k ON k.cluster_id = mb.cluster_id
           JOIN (SELECT DISTINCT m.item_id FROM {core}.cultural_map m WHERE m.valid_to IS NULL) op
             ON op.item_id = k.item_id
           WHERE k.cluster_date = @d
           GROUP BY mb.post_id) cl ON cl.post_id = ps.post_id
WHERE fs.first_day = @d
"""

POST_ITEMS_INSERT_SQL = """
INSERT INTO {core}.post_items (post_id, item_id, via)
SELECT DISTINCT n.post_id, n.item_id, n.via FROM UNNEST(@rows) n
WHERE NOT EXISTS (SELECT 1 FROM {core}.post_items p WHERE p.post_id = n.post_id AND p.item_id = n.item_id)
"""

MERGE_SQL = """
MERGE {core}.cultural_map t
USING (SELECT n.item_id, n.kind, n.canonical_key, n.label, n.first_seen, n.first_seen_market,
         n.first_seen_platform, n.last_seen, n.status FROM UNNEST(@items) n) s
ON t.item_id = s.item_id
WHEN MATCHED AND t.valid_to IS NULL THEN
  UPDATE SET last_seen = GREATEST(IFNULL(t.last_seen, s.last_seen), s.last_seen),
    status = IF(t.status IN ('active', 'generic'), s.status, t.status)
WHEN NOT MATCHED THEN
  INSERT (item_id, kind, canonical_key, label, first_seen, first_seen_market, first_seen_platform, last_seen,
          status, valid_from)
  VALUES (s.item_id, s.kind, s.canonical_key, s.label, s.first_seen, s.first_seen_market, s.first_seen_platform,
          s.last_seen, s.status, CURRENT_TIMESTAMP())
"""

POST_ITEM_FIELDS = (("post_id", "STRING"), ("item_id", "STRING"), ("via", "STRING"))
ITEM_FIELDS = (("item_id", "STRING"), ("kind", "STRING"), ("canonical_key", "STRING"), ("label", "STRING"),
               ("first_seen", "DATE"), ("first_seen_market", "STRING"), ("first_seen_platform", "STRING"),
               ("last_seen", "DATE"), ("status", "STRING"))


def items_rows(posts):
    """(post_items rows, cultural_map rows) for posts carrying post_id, platform, hashtags, sound_id,
    creator_id, market and first_day, and optionally cluster_items. One post_items row per post and item;
    one cultural_map row per item, first seen on its earliest post (ties by market, platform, post_id), last
    seen on its latest, with status 'generic' when items.is_generic holds and 'active' otherwise. A cluster
    item gets a post_items row with via 'cluster' and no cultural_map row, since understand writes its row.
    An item of any kind whose label gdelt.blocked() flags (rule 1), a hashtag, a creator handle or a sound id,
    keeps its post_items row and gets no cultural_map row, as collect's cultural_map_rows leaves it out, so it
    is never named. A row already open in cultural_map is not looked at or retired here."""
    from core.collect.gdelt import blocked

    post_items, items, seen, refused = [], {}, set(), set()
    order = sorted(posts, key=lambda p: (p["first_day"], p["market"], p["platform"] or "", p["post_id"]))
    for post in order:
        for cluster_item in post.get("cluster_items") or []:
            if (post["post_id"], cluster_item) not in seen:
                seen.add((post["post_id"], cluster_item))
                post_items.append({"post_id": post["post_id"], "item_id": cluster_item, "via": "cluster"})
        for it in items_for_post(post):
            if (post["post_id"], it["item_id"]) not in seen:
                seen.add((post["post_id"], it["item_id"]))
                post_items.append({"post_id": post["post_id"], "item_id": it["item_id"], "via": it["via"]})
            row = items.get(it["item_id"])
            if it["item_id"] in refused:
                continue
            if row is None and blocked(it["label"]):
                refused.add(it["item_id"])
                continue
            if row is None:
                items[it["item_id"]] = {
                    "item_id": it["item_id"], "kind": it["kind"], "canonical_key": it["canonical_key"],
                    "label": it["label"], "first_seen": post["first_day"], "first_seen_market": post["market"],
                    "first_seen_platform": post["platform"], "last_seen": post["first_day"],
                    "status": status(it["kind"], it["canonical_key"])}
            else:
                row["last_seen"] = max(row["last_seen"], post["first_day"])
    return post_items, list(items.values())


def status(kind, canonical_key):
    """The cultural_map status an item enters with: 'generic' for a stoplisted item, else 'active'."""
    return "generic" if is_generic(kind, canonical_key) else "active"


def cultural_map_merge_sql():
    """MERGE of @items into cultural_map: new items are inserted with their status; an open existing row
    has last_seen moved forward, and an 'active' or 'generic' status follows the stoplist both ways ('generic'
    when stoplisted, back to 'active' when taken off). Any other status such as 'rejected' never changes;
    closed rows are left alone."""
    return MERGE_SQL


def aggregate_sql():
    lines = AGGREGATE_SQL.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.startswith("--"))


def _struct_array(name, rows, fields):
    return bigquery.ArrayQueryParameter(name, "STRUCT", [
        bigquery.StructQueryParameter(None, *[bigquery.ScalarQueryParameter(f, t, row[f]) for f, t in fields])
        for row in rows])


def _run(client, sql, params, core, agent):
    job = client.query(render(sql, core, agent), job_config=bigquery.QueryJobConfig(query_parameters=params))
    return [dict(row.items()) for row in job.result()]


def run_aggregate(client, d, run_id, rule_version, core=CORE, agent=AGENT):
    """Itemise posts first sighted on d, merge their items into cultural_map, then append item_daily for d."""
    day = bigquery.ScalarQueryParameter("d", "DATE", d)
    posts = _run(client, FIRST_SIGHTED_SQL, [day], core, agent)
    post_items, items = items_rows(posts)
    for i in range(0, len(post_items), CHUNK):
        _run(client, POST_ITEMS_INSERT_SQL, [_struct_array("rows", post_items[i:i + CHUNK], POST_ITEM_FIELDS)],
             core, agent)
    for i in range(0, len(items), CHUNK):
        _run(client, cultural_map_merge_sql(), [_struct_array("items", items[i:i + CHUNK], ITEM_FIELDS)],
             core, agent)
    _run(client, aggregate_sql(), [day, bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
                                   bigquery.ScalarQueryParameter("rule_version", "STRING", rule_version)],
         core, agent)
    return {"posts": len(posts), "post_items": len(post_items), "items": len(items)}
