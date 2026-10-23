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

# Posts whose first sighting in a market is @d, with the raw fields items_for_post reads, and cluster_links: the
# items of @d's clusters the post is a member of (BUILD.md 2.2), each with an open cultural_map row, once per item and
# clustering run market ('za', 'ng', 'ke' for a market run, 'pan' for the pooled run), so the link keeps its scope.
FIRST_SIGHTED_SQL = """
SELECT ps.post_id, ps.platform, ps.hashtags, ps.sound_id, ps.creator_id, cr.display_name AS creator_name, fs.market,
       fs.first_day, cl.cluster_links
FROM (SELECT po.post_id, po.market, MIN(po.observed_date) first_day
      FROM {core}.post_observations po WHERE po.observed_date <= @d
      GROUP BY po.post_id, po.market) fs
JOIN {core}.posts ps ON ps.post_id = fs.post_id
LEFT JOIN {core}.creators cr ON cr.creator_id = ps.creator_id AND cr.platform = ps.platform
LEFT JOIN (SELECT ck.post_id, ARRAY_AGG(STRUCT(ck.item_id AS item_id, ck.run_market AS run_market)
                                        ORDER BY ck.item_id, ck.run_market) cluster_links
           FROM (SELECT DISTINCT mb.post_id, k.item_id, LOWER(k.market) run_market
                 FROM {core}.cluster_members mb
                 JOIN {core}.clusters k ON k.cluster_id = mb.cluster_id
                 JOIN (SELECT DISTINCT m.item_id FROM {core}.cultural_map m WHERE m.valid_to IS NULL) op
                   ON op.item_id = k.item_id
                 WHERE k.cluster_date = @d) ck
           GROUP BY ck.post_id) cl ON cl.post_id = ps.post_id
WHERE fs.first_day = @d
"""

POST_ITEMS_INSERT_SQL = """
INSERT INTO {core}.post_items (post_id, item_id, via)
SELECT DISTINCT n.post_id, n.item_id, n.via FROM UNNEST(@rows) n
WHERE NOT EXISTS (SELECT 1 FROM {core}.post_items p WHERE p.post_id = n.post_id AND p.item_id = n.item_id)
"""

# The dated, market-aware cluster links tvf_post_items counts (counted_links.sql): the same one row per post and item
# as POST_ITEMS_INSERT_SQL, with linked_on and link_market. A post and item already linked, a legacy row with neither
# field among them, is left as it is, so a rerun of a day aggregated before links carried their scope never adds a
# second row beside the legacy one.
CLUSTER_LINKS_INSERT_SQL = """
INSERT INTO {core}.post_items (post_id, item_id, via, linked_on, link_market)
SELECT DISTINCT n.post_id, n.item_id, n.via, n.linked_on, n.link_market FROM UNNEST(@rows) n
WHERE NOT EXISTS (SELECT 1 FROM {core}.post_items p WHERE p.post_id = n.post_id AND p.item_id = n.item_id)
"""

MERGE_SQL = """
MERGE {core}.cultural_map t
USING (SELECT n.item_id, n.kind, n.canonical_key, n.label, n.first_seen, n.first_seen_market,
         n.first_seen_platform, n.last_seen, n.status FROM UNNEST(@items) n) s
ON t.item_id = s.item_id
WHEN MATCHED AND t.valid_to IS NULL THEN
  UPDATE SET last_seen = GREATEST(IFNULL(t.last_seen, s.last_seen), s.last_seen),
    status = IF(t.status IN ('active', 'generic'), s.status, t.status),
    label = IF(t.kind = 'sound' AND s.label IS NOT NULL
               AND (t.label = REGEXP_EXTRACT(t.canonical_key, r'^[^:]*:(.*)$') OR REGEXP_CONTAINS(t.label, r'^[0-9]{8,}$'))
               AND s.label != IFNULL(REGEXP_EXTRACT(s.canonical_key, r'^[^:]*:(.*)$'), '')
               AND NOT REGEXP_CONTAINS(s.label, r'^[0-9]{8,}$'),
               s.label, t.label)
WHEN NOT MATCHED THEN
  INSERT (item_id, kind, canonical_key, label, first_seen, first_seen_market, first_seen_platform, last_seen,
          status, valid_from)
  VALUES (s.item_id, s.kind, s.canonical_key, s.label, s.first_seen, s.first_seen_market, s.first_seen_platform,
          s.last_seen, s.status, CURRENT_TIMESTAMP())
"""

POST_ITEM_FIELDS = (("post_id", "STRING"), ("item_id", "STRING"), ("via", "STRING"))
CLUSTER_LINK_FIELDS = POST_ITEM_FIELDS + (("linked_on", "DATE"), ("link_market", "STRING"))
CLUSTER_VIAS = ("cluster", "cluster_pan")
ITEM_FIELDS = (("item_id", "STRING"), ("kind", "STRING"), ("canonical_key", "STRING"), ("label", "STRING"),
               ("first_seen", "DATE"), ("first_seen_market", "STRING"), ("first_seen_platform", "STRING"),
               ("last_seen", "DATE"), ("status", "STRING"))


def items_rows(posts):
    """(post_items rows, cultural_map rows) for posts carrying post_id, platform, hashtags, sound_id,
    creator_id, market and first_day, and optionally cluster_links. One post_items row per post and item;
    one cultural_map row per item, first seen on its earliest post (ties by market, platform, post_id), last
    seen on its latest, with status 'generic' when items.is_generic holds and 'active' otherwise. A cluster
    item gets a post_items row dated first_day (the clusters' date) with the via and link_market of
    cluster_scopes, and no cultural_map row, since understand writes its row.
    An item of any kind whose label gdelt.blocked() flags (rule 1), a hashtag, a creator handle or a sound id,
    keeps its post_items row and gets no cultural_map row, as collect's cultural_map_rows leaves it out, so it
    is never named. A creator is also refused by its display name (post["creator_name"]), the text collect checks;
    a sound is checked by its id only, since no sound title is on posts. A row already open in cultural_map is
    not looked at or retired here."""
    post_items, items, seen, refused = [], {}, set(), set()
    order = sorted(posts, key=lambda p: (p["first_day"], p["market"], p["platform"] or "", p["post_id"]))
    for post in order:
        for cluster_item, via, link_market in cluster_scopes(post.get("cluster_links")):
            if (post["post_id"], cluster_item) not in seen:
                seen.add((post["post_id"], cluster_item))
                post_items.append({"post_id": post["post_id"], "item_id": cluster_item, "via": via,
                                   "linked_on": post["first_day"], "link_market": link_market})
        for it in items_for_post(post):
            if (post["post_id"], it["item_id"]) not in seen:
                seen.add((post["post_id"], it["item_id"]))
                post_items.append({"post_id": post["post_id"], "item_id": it["item_id"], "via": it["via"]})
            row = items.get(it["item_id"])
            if it["item_id"] in refused:
                continue
            if row is None and blocked_item(it, post):
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


def cluster_scopes(links):
    """[(item_id, via, link_market)], one per item, from a post's cluster_links ({item_id, run_market}). An item only a
    market run gave the post is a local link of that market (via 'cluster', link_market upper case as
    post_observations holds it); an item only the pooled run gave it is a pooled link (via 'cluster_pan', no market).
    post_items keeps one row per post and item, so an item two runs gave the post (a market run and the pooled run, or
    two market runs) is one link with no market, counted in every market the post is sighted in, as the pooled run
    counts it; via stays 'cluster' when a market run is among them, so it keeps the market run's precedence."""
    runs = {}
    for link in links or []:
        runs.setdefault(link["item_id"], set()).add(str(link["run_market"]).lower())
    out = []
    for item in sorted(runs):
        markets = runs[item] - {"pan"}
        via = "cluster" if markets else "cluster_pan"
        market = next(iter(markets)).upper() if len(markets) == 1 and "pan" not in runs[item] else None
        out.append((item, via, market))
    return out


def blocked_item(it, post):
    """True when rule 1 refuses the item: its label, and for a creator also the display name collect names it by.
    A sound's title is not on posts, so a sound is checked by its id only."""
    from core.collect.gdelt import blocked

    return blocked(it["label"]) or bool(it["kind"] == "creator" and post.get("creator_name")
                                        and blocked(post["creator_name"]))


def status(kind, canonical_key):
    """The cultural_map status an item enters with: 'generic' for a stoplisted item, else 'active'."""
    return "generic" if is_generic(kind, canonical_key) else "active"


def cultural_map_merge_sql():
    """MERGE of @items into cultural_map: new items are inserted with their status; an open existing row
    has last_seen moved forward, and an 'active' or 'generic' status follows the stoplist both ways ('generic'
    when stoplisted, back to 'active' when taken off). Any other status such as 'rejected' never changes;
    closed rows are left alone. An open sound row whose label is still its raw id (the id part of its canonical
    key, or 8 or more digits) takes the incoming label when that one is readable (neither its own id nor 8 or
    more digits), so a sound first inserted by its id is named once collect reads its title; a readable label
    is never replaced. Callers pass only labels that passed rule 1 (gdelt.blocked)."""
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
    for sql, fields, rows in (
            (POST_ITEMS_INSERT_SQL, POST_ITEM_FIELDS, [r for r in post_items if r["via"] not in CLUSTER_VIAS]),
            (CLUSTER_LINKS_INSERT_SQL, CLUSTER_LINK_FIELDS, [r for r in post_items if r["via"] in CLUSTER_VIAS])):
        for i in range(0, len(rows), CHUNK):
            _run(client, sql, [_struct_array("rows", rows[i:i + CHUNK], fields)], core, agent)
    for i in range(0, len(items), CHUNK):
        _run(client, cultural_map_merge_sql(), [_struct_array("items", items[i:i + CHUNK], ITEM_FIELDS)],
             core, agent)
    _run(client, aggregate_sql(), [day, bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
                                   bigquery.ScalarQueryParameter("rule_version", "STRING", rule_version)],
         core, agent)
    return {"posts": len(posts), "post_items": len(post_items), "items": len(items)}
