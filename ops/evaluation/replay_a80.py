"""Offline replay of one retained day through the a80 understand and scope code.

Release B will run a80 understand and scope code live for the first time. This harness measures, from the frozen
snapshots of 5 to 8 October, what that code does to a day the earlier code already ran. Nothing here touches a network
or a warehouse: the inputs are seven parquet files per day and, for the control, the pinned detect states.

What it can and cannot replay. The scope rule is replayed exactly: the repo's own candidates statement
(core/brief/sql/brief.sql) runs on DuckDB through core/detect/tests/duck.py over a v_item_market_scope view that is a
pinned copy, never the checkout's view. The v1 copy is the view as it stood before a80 (scope_view_v1.sql, the text of
34725f1, equal to the definition read from the warehouse on 7 October). The a80 copy is the view at a80be1d
(scope_view_a80.sql), which the checkout's view has since moved on from. The clustering is not refitted, because the
snapshots hold no text and no embeddings. What a80 changes there is known, though: the earlier code reassigned every
HDBSCAN outlier to its nearest topic and kept it at probability zero, and a80 leaves the outlier unassigned. A cluster
member with probability zero in a retained day is therefore a reassigned outlier, and a80 would not have written that
row. The effect on links is replayed by dropping the cluster links whose every supporting member is such a row. The matching
change in a80 (a topical vote) needs centroids and keywords the snapshots do not carry and is not replayed.

Two inputs are missing from the snapshots and are approximated: post engagement (every post ties at zero, so the 12
post pack and the two per creator cap break ties by post id) and the raw creator id (the platform and creator key
hash stands in for it). The control measures how far that moves the scope rows.
"""
import hashlib
import json
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from core.brief import job
from core.detect import sqlrun
from core.detect.tests import duck

DATES = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
MARKETS = ("ZA", "NG", "KE")
PINNED_DAYS = ("2026-10-05", "2026-10-07")  # the days whose Nigerian detect state was retained
CLUSTER_VIA = ("cluster", "cluster_pan", "lineage")
# sha256 of SHA256SUMS.txt in the retained snapshot folder. The file lists a digest per snapshot file, so a digest of
# the list itself, pinned here, is what stops a changed file and its rewritten line from passing together.
PINNED_SUMS_SHA256 = "7288cba0aeab817b0af1e65c3d57b6acd60d67cb94261b87a2e15760731edede"
SCOPE_V1 = Path(__file__).with_name("scope_view_v1.sql")
# The v_item_market_scope statement of core/detect/sql/views.sql at a80be1d, the live staging commit, copied verbatim.
# The checkout's own view has moved on (it adds the news scope column and the locality members), so the "a80" arm
# reads this copy and not the checkout. A test compares the copy with that commit.
SCOPE_A80 = Path(__file__).with_name("scope_view_a80.sql")
# The understand run's own counts for the four days, from agent.runs (stage understand, status ok, counts.cluster):
# kind -> (clusters, members, today_posts, posts fitted on). The earlier code assigned every post sighted today.
RECORDED_UNDERSTAND = {
    "2026-10-05": {"ke": (78, 1550, 1550, 3513), "ng": (115, 2280, 2280, 4802), "pan": (114, 5345, 5345, 11510),
                   "za": (78, 1659, 1659, 3613)},
    "2026-10-06": {"ke": (74, 1682, 1682, 3696), "ng": (128, 2478, 2478, 5320), "pan": (100, 5630, 5630, 12300),
                   "za": (88, 1739, 1739, 3712)},
    "2026-10-07": {"ke": (85, 1799, 1799, 3960), "ng": (105, 2428, 2428, 5708), "pan": (115, 5868, 5868, 13153),
                   "za": (79, 1847, 1847, 3935)},
    "2026-10-08": {"ke": (90, 1822, 1822, 4193), "ng": (114, 2652, 2652, 6091), "pan": (117, 5995, 5995, 13774),
                   "za": (83, 1686, 1686, 3971)},
}
# The recorded brief run per day for Nigeria and the item the run merged into another card, if any.
RECORDED_BRIEFS = {
    "2026-10-05": ("brief-20261005-8e019e9c7270", []),
    "2026-10-07": ("brief-20261007-665e5edab6ab",
                   ["643928a31a050f920a9ae0c37a9729e5f166e7e6cbbc16244213d06e05a0ad37"]),
}


class ReplayInputError(Exception):
    """An input is not the one this replay was written against."""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_snapshots(snap_dir, sums_sha256=PINNED_SUMS_SHA256):
    """Recompute the digest of SHA256SUMS.txt and of every file it lists. Returns the number of files verified.
    The list is trusted only after its own digest matches the pinned one."""
    snap_dir = Path(snap_dir)
    sums = snap_dir / "SHA256SUMS.txt"
    if _sha256(sums) != sums_sha256:
        raise ReplayInputError("SHA256SUMS.txt does not match the pinned digest")
    bad, count = [], 0
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.strip().lstrip("*")
        path = (snap_dir / name).resolve()
        if snap_dir.resolve() not in path.parents:
            raise ReplayInputError(f"{name} leaves the snapshot folder")
        if not path.is_file() or _sha256(path) != digest.lower():
            bad.append(name)
        count += 1
    if bad:
        raise ReplayInputError("checksum failed for " + ", ".join(bad))
    return count


def _parquet(snap_dir, day, table):
    return f"read_parquet('{(Path(snap_dir) / day / (table + '.parquet')).as_posix()}')"


def load_world(snap_dir, day, links="recorded"):
    """The retained day in the DuckDB schemas of core/detect/tests/duck.py. links is "recorded" for the post_items
    the day had, or "a80" for those less the cluster links no positive-probability member supports."""
    if links not in ("recorded", "a80"):
        raise ValueError(f"links must be recorded or a80, got {links!r}")
    con = duck.connect()
    p = lambda table: _parquet(snap_dir, day, table)  # noqa: E731
    if con.execute(f"SELECT COUNT(*) FROM {p('suppressed_creators')}").fetchone()[0]:
        raise ReplayInputError("suppressed creators are held as hashes of the creator id and cannot be mapped")
    con.execute(f"""INSERT INTO core.post_observations
      (post_id, observed_at, observed_date, market, platform, route, lane, lane_class)
      SELECT post_id, observed_at, observed_date, market, platform, route, lane, lane_class FROM {p('observations')}""")
    con.execute(f"""INSERT INTO core.posts
      (post_id, platform, creator_id, published_at, post_date, geo_market, geo_confidence, geo_source)
      SELECT post_id, platform, creator_key_hash, published_at, post_date, geo_market, geo_confidence, geo_source
      FROM {p('posts')}""")
    con.execute(f"INSERT INTO core.post_items (post_id, item_id, via) SELECT post_id, item_id, via FROM {p('post_items')}")
    con.execute(f"""INSERT INTO core.source_market_fixture (post_id, source_markets, source_sightings)
      SELECT post_id, list(DISTINCT source_market ORDER BY source_market),
        list(CAST(ROW(source_market, source_region, route, NULL, observed_at, observed_date) AS
                  STRUCT(source_market VARCHAR, source_region VARCHAR, route VARCHAR, protocol VARCHAR,
                         observed_at TIMESTAMPTZ, obs_date DATE))
             ORDER BY observed_date, observed_at, source_market, source_region, route)
      FROM (SELECT DISTINCT post_id, source_market, source_region, route, observed_at, observed_date
            FROM {p('observations')}
            WHERE post_id IS NOT NULL AND source_market IN ('ZA', 'NG', 'KE')
              AND observed_at IS NOT NULL AND observed_date IS NOT NULL)
      GROUP BY post_id""")
    con.execute(f"""INSERT INTO core.cultural_map
      (item_id, kind, canonical_key, label, first_seen, first_seen_market, last_seen, status, valid_from, valid_to)
      SELECT item_id, kind, canonical_key, label, first_seen, first_seen_market, last_seen, status, valid_from,
        valid_to FROM {p('cultural_map')}""")
    con.execute(f"""INSERT INTO core.clusters (cluster_date, cluster_id, market, item_id, match_kind)
      SELECT cluster_date, cluster_id, market, item_id, match_kind FROM {p('clusters')}""")
    con.execute(f"""INSERT INTO core.cluster_members (cluster_id, post_id, probability)
      SELECT cluster_id, post_id, probability FROM {p('cluster_members')}""")
    if links == "a80":
        via = ", ".join(f"'{v}'" for v in CLUSTER_VIA)
        support = """SELECT 1 FROM core.cluster_members cm JOIN core.clusters c ON c.cluster_id = cm.cluster_id
                     WHERE c.item_id = pi.item_id AND cm.post_id = pi.post_id AND cm.probability IS NOT NULL"""
        con.execute(f"""DELETE FROM core.post_items pi WHERE pi.via IN ({via})
          AND EXISTS ({support}) AND NOT EXISTS ({support} AND cm.probability <> 0)""")
    return con


def _run_id(day):
    return f"replay-detect-{day}"


def _ensure_run(con, day):
    """One good detect run for the day: v_good_runs shows only the latest, so every state row shares its id."""
    if not con.execute("SELECT COUNT(*) FROM agent.runs WHERE run_id = ?", [_run_id(day)]).fetchone()[0]:
        con.execute("""INSERT INTO agent.runs (run_id, stage, run_date, status, started_at, finished_at)
                       VALUES (?, 'detect', ?, 'ok', TIMESTAMPTZ '2026-01-01 00:00:00+00',
                               TIMESTAMPTZ '2026-01-01 00:01:00+00')""", [_run_id(day), day])
    return _run_id(day)


def add_derived_state_keys(con, day, markets):
    """item_state key rows (no pinned values) for every item with a post the scope view would see in a market, for
    the days and markets whose recorded detect state was not retained."""
    run_id = _ensure_run(con, day)
    marks = ", ".join(f"'{m}'" for m in markets)
    con.execute(f"""INSERT INTO core.item_state (metric_date, market, item_id, run_id)
      SELECT DISTINCT DATE '{day}', po.market, pi.item_id, '{run_id}'
      FROM core.post_observations po JOIN core.post_items pi ON pi.post_id = po.post_id
      WHERE po.market IN ({marks}) AND po.observed_date BETWEEN DATE '{day}' - INTERVAL 6 DAY AND DATE '{day}'
        AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live')""")


def add_pinned_states(con, day, market, rows):
    """Replace the market's item_state rows for the day with the pinned ones (item_id, eligible, creators3, posts3,
    worth_raw and optionally state)."""
    run_id = _ensure_run(con, day)
    con.execute("DELETE FROM core.item_state WHERE metric_date = ? AND market = ?", [day, market])
    for row in rows:
        con.execute("""INSERT INTO core.item_state
          (metric_date, market, item_id, eligible, creators3, posts3, worth_raw, state, run_id)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [day, market, row["item_id"], row["eligible"], row.get("creators3"), row.get("posts3"),
                     row.get("worth_raw"), row.get("state"), run_id])


def scope_view_sql(variant):
    """The v_item_market_scope statement as rendered for the DuckDB schemas: "v1" is the pinned copy from before a80,
    "a80" is the pinned copy from a80be1d, not the view in this checkout."""
    if variant == "v1":
        return sqlrun.render(SCOPE_V1.read_text(encoding="utf-8"), "core", "agent").strip()
    if variant == "a80":
        return sqlrun.render(SCOPE_A80.read_text(encoding="utf-8"), "core", "agent").strip()
    raise ValueError(f"scope variant must be v1 or a80, got {variant!r}")


def scope_frame(con, variant):
    """v_item_market_scope under one rule, for every item_state key in the world, as a DataFrame."""
    con.execute(duck.create_statement(scope_view_sql(variant)))
    frame = con.execute("SELECT * FROM core.v_item_market_scope ORDER BY metric_date, market, item_id").df()
    frame["metric_date"] = pd.to_datetime(frame["metric_date"])
    return frame


def use_scope(con, frame):
    """Make v_item_market_scope read the given rows, so the candidates statement orders by them."""
    con.register("scope_rows", frame)
    con.execute("CREATE OR REPLACE TABLE core.scope_fixed AS SELECT * FROM scope_rows")
    con.unregister("scope_rows")
    con.execute("CREATE OR REPLACE VIEW core.v_item_market_scope AS SELECT * FROM core.scope_fixed")


def judged_ten(con, day, market):
    """The ten items the brief would judge: brief.sql's candidates statement, then the job's own _ranked, first ten."""
    rows = job._query(duck.Client(con), "candidates", {"d": date.fromisoformat(day), "market": market}, "core", "agent")
    return [r["item_id"] for r in job._ranked(rows)][:10]


def noise_table(snap_dir, day):
    """Per run kind (za, ng, ke, pan) of the day's clusters: clusters, members, members at probability zero (the
    outliers the earlier code reassigned), members left, clusters with no positive member, the count of members in
    (0, 0.001) and the smallest positive probability. The last two test the reading that zero means reassigned."""
    with duckdb.connect() as con:
        return con.execute(f"""
        WITH m AS (
          SELECT regexp_extract(c.cluster_id, '^[0-9]{{8}}-([a-z]+)-', 1) kind, c.cluster_id, cm.probability
          FROM {_parquet(snap_dir, day, 'cluster_members')} cm
          JOIN {_parquet(snap_dir, day, 'clusters')} c ON c.cluster_id = cm.cluster_id
          WHERE c.cluster_date = DATE '{day}'),
        per AS (SELECT kind, cluster_id, COUNT(*) FILTER (WHERE probability <> 0) positive FROM m GROUP BY 1, 2)
        SELECT m.kind, COUNT(DISTINCT m.cluster_id) clusters, COUNT(*) members,
          COUNT(*) FILTER (WHERE probability = 0) noise_members,
          COUNT(*) FILTER (WHERE probability <> 0) kept_members,
          (SELECT COUNT(*) FROM per WHERE per.kind = m.kind AND per.positive = 0) vanishing_clusters,
          COUNT(*) FILTER (WHERE probability > 0 AND probability < 0.001) tiny_positive,
          MIN(probability) FILTER (WHERE probability > 0) min_positive
        FROM m GROUP BY m.kind ORDER BY m.kind""").df()


def _top_bucket(scope, total, market):
    return (scope == "market") & (total >= 3) & (market >= 2)


def _bucket0(frame):
    return _top_bucket(frame["market_scope"], frame["total_posts7"], frame["market_posts7"])


def scope_counts(frame):
    """Keys, market and global scope, the top candidate bucket (market scope with 3 or more scoped posts and 2 or
    more market posts, as brief.sql orders), the summed post counts and the keys with no scoped post at all."""
    return {"keys": int(len(frame)), "market": int((frame["market_scope"] == "market").sum()),
            "global": int((frame["market_scope"] == "global").sum()), "bucket0": int(_bucket0(frame).sum()),
            "market_posts7": int(frame["market_posts7"].sum()), "total_posts7": int(frame["total_posts7"].sum()),
            "empty": int((frame["total_posts7"] == 0).sum())}


def scope_changes(before, after):
    """How the keys both frames hold moved between the two rules, and the keys only one of them holds."""
    cols = ["metric_date", "market", "item_id"]
    both = before.merge(after, on=cols, suffixes=("_b", "_a"))
    b0b = _top_bucket(both["market_scope_b"], both["total_posts7_b"], both["market_posts7_b"])
    b0a = _top_bucket(both["market_scope_a"], both["total_posts7_a"], both["market_posts7_a"])
    keys = lambda f: set(map(tuple, f[cols].values.tolist()))  # noqa: E731
    return {"shared": int(len(both)),
            "market_to_global": int(((both["market_scope_b"] == "market") & (both["market_scope_a"] == "global")).sum()),
            "global_to_market": int(((both["market_scope_b"] == "global") & (both["market_scope_a"] == "market")).sum()),
            "bucket0_lost": int((b0b & ~b0a).sum()), "bucket0_gained": int((~b0b & b0a).sum()),
            "only_before": len(keys(before) - keys(after)), "only_after": len(keys(after) - keys(before))}


def ten_changes(base, other):
    """Who left and who came in when the judged list moves from base to other."""
    return {"same_order": list(base) == list(other), "same_set": set(base) == set(other),
            "changed": len(set(base) - set(other)), "entering": [i for i in other if i not in set(base)],
            "leaving": [i for i in base if i not in set(other)]}


def retained_scope_frame(rows, day, market):
    """The scope rows read from the warehouse on 7 October (carried in the pinned state rows) as a scope frame."""
    return pd.DataFrame([{"metric_date": pd.Timestamp(day), "item_id": r["item_id"], "market": market,
                          "market_scope": r["scope"]["market_scope"], "market_posts7": r["scope"]["market_posts7"],
                          "total_posts7": r["scope"]["total_posts7"], "market_share7": r["scope"]["market_share7"]}
                         for r in rows])


def load_states(states_path, day):
    return json.loads(Path(states_path).read_text(encoding="utf-8"))["days"][day]


def pinned_world(snap_dir, states_path, day, market="NG", links="recorded"):
    """The day's snapshot with the market's item_state replaced by the pinned detect state of that day."""
    rows = load_states(states_path, day)
    manifest = json.loads((Path(snap_dir) / f"manifest-{day}.json").read_text(encoding="utf-8"))
    runs = {r["run_id"] for r in rows}
    if runs != {manifest["detect_run_id"]}:
        raise ReplayInputError(f"pinned states are from {sorted(runs)}, the snapshot is of {manifest['detect_run_id']}")
    con = load_world(snap_dir, day, links=links)
    add_pinned_states(con, day, market, rows)
    return con


def control_world(snap_dir, states_path, day, market="NG", links="recorded"):
    """The recorded day as it ran before a80: the pinned detect state and the scope rows read from the warehouse on
    7 October, over the snapshot's posts, so candidates reads the snapshot's local_first and nothing else is replaced."""
    con = pinned_world(snap_dir, states_path, day, market, links)
    use_scope(con, retained_scope_frame(load_states(states_path, day), day, market))
    return con


def recorded_ten(briefs_path, day):
    """Cards, held items and the merged item of the day's recorded Nigerian brief."""
    run_id, merged = RECORDED_BRIEFS[day]
    for row in json.loads(Path(briefs_path).read_text(encoding="utf-8"))["rows"]:
        if row["run_id"] == run_id:
            payload = json.loads(row["payload"])
            return ([c["item_id"] for c in payload["cards"]]
                    + [h["item_id"] for h in payload["held_back"]["items"]] + merged)
    raise ReplayInputError(f"{run_id} is not in {briefs_path}")


def _kinds(con):
    return dict(con.execute("SELECT item_id, kind FROM core.cultural_map WHERE valid_to IS NULL").fetchall())


def _grouped(frame, kinds):
    group = frame["item_id"].map(lambda i: "topic" if kinds.get(i) == "topic" else "other")
    return {"all": frame, "topic": frame[group == "topic"], "other": frame[group == "other"]}


def replay_day(snap_dir, day):
    """Noise and scope for one day over every item with a linked post sighted in the market (not the detect state,
    which is retained for Nigeria on 5 and 7 October only; see replay_pinned). Scope is read under the v1 rule and
    the a80 rule on the recorded links, and under the a80 rule on the links a80 would have written."""
    noise = noise_table(snap_dir, day).set_index("kind")
    understand = {}
    for kind, row in noise.iterrows():
        members, noisy = int(row["members"]), int(row["noise_members"])
        understand[kind] = {"clusters": int(row["clusters"]), "members": members, "noise_members": noisy,
                            "kept_members": int(row["kept_members"]), "noise_share": noisy / members,
                            "vanishing_clusters": int(row["vanishing_clusters"]),
                            "tiny_positive": int(row["tiny_positive"]), "min_positive": float(row["min_positive"])}
    worlds = {}
    for links in ("recorded", "a80"):
        con = load_world(snap_dir, day, links=links)
        add_derived_state_keys(con, day, MARKETS)
        worlds[links] = con
    kinds = _kinds(worlds["recorded"])
    frames = {"v1_recorded": scope_frame(worlds["recorded"], "v1"), "a80_recorded": scope_frame(worlds["recorded"], "a80"),
              "a80_a80links": scope_frame(worlds["a80"], "a80")}
    for con in worlds.values():
        con.close()
    scope = {}
    for market in MARKETS:
        groups = {name: _grouped(f[f["market"] == market], kinds) for name, f in frames.items()}
        scope[market] = {}
        for group in ("all", "topic", "other"):
            v1, a80, a80l = (groups[n][group] for n in ("v1_recorded", "a80_recorded", "a80_a80links"))
            scope[market][group] = {
                "v1_recorded": scope_counts(v1), "a80_recorded": scope_counts(a80), "a80_a80links": scope_counts(a80l),
                "rule_effect": scope_changes(v1, a80), "links_effect": scope_changes(a80, a80l),
                "total_effect": scope_changes(v1, a80l)}
    return {"day": day, "understand": understand, "scope": scope}


def _pool(con, day, market):
    rows = job._query(duck.Client(con), "candidates", {"d": date.fromisoformat(day), "market": market}, "core", "agent")
    return [r["item_id"] for r in job._ranked(rows)]


def scope_agreement(retained, recomputed):
    """How closely the v1 view rebuilt from the snapshot reproduces the scope rows read from the warehouse."""
    both = retained.merge(recomputed, on=["metric_date", "market", "item_id"], suffixes=("_r", "_s"))
    cols = ["market_scope", "market_posts7", "total_posts7"]
    return {"keys": int(len(retained)), "equal": int(len(both[(both[[c + "_r" for c in cols]].values
                                                              == both[[c + "_s" for c in cols]].values).all(axis=1)])),
            "same_scope": int((both["market_scope_r"] == both["market_scope_s"]).sum()),
            "same_top_bucket": int((_top_bucket(both["market_scope_r"], both["total_posts7_r"], both["market_posts7_r"])
                                    == _top_bucket(both["market_scope_s"], both["total_posts7_s"],
                                                   both["market_posts7_s"])).sum())}


def replay_pinned(snap_dir, states_path, day, briefs_path=None, market="NG"):
    """The judged ten for the market on a day whose detect state was retained: the control (the scope rows read from
    the warehouse), then the v1 view rebuilt from the snapshot, the a80 view on the recorded links, the v1 view on
    the links a80 would have written and the a80 view on those links, each through brief.sql's candidates statement."""
    retained = retained_scope_frame(load_states(states_path, day), day, market)
    control = control_world(snap_dir, states_path, day, market)
    control_ten, control_pool = judged_ten(control, day, market), _pool(control, day, market)
    control.close()
    recorded = recorded_ten(briefs_path, day) if briefs_path and day in RECORDED_BRIEFS else None
    out = {"day": day, "market": market,
           "control": {"ten": control_ten, "pool90": control_pool, "recorded_ten": recorded,
                       "matches_recorded": None if recorded is None else set(control_ten) == set(recorded)},
           "variants": {}}
    frames = {}
    plan = (("v1_recomputed", "recorded", "v1"), ("a80_rule", "recorded", "a80"), ("v1_a80links", "a80", "v1"),
            ("a80_full", "a80", "a80"))
    for name, links, variant in plan:
        con = pinned_world(snap_dir, states_path, day, market, links)
        frame = scope_frame(con, variant)
        frame = frame[frame["market"] == market]
        use_scope(con, frame)
        pool = _pool(con, day, market)
        ten = pool[:10]
        con.close()
        out["variants"][name] = {"ten": ten, "pool90": pool, "vs_control": ten_changes(control_ten, ten),
                                 "pool_vs_control": ten_changes(control_pool, pool),
                                 "scope": scope_counts(frame), "scope_vs_retained": scope_changes(retained, frame)}
        if name == "v1_recomputed":
            out["control"]["scope_agreement"] = scope_agreement(retained, frame)
        base = out["variants"]["v1_recomputed"]
        out["variants"][name]["vs_v1_recomputed"] = ten_changes(base["ten"], ten)
        out["variants"][name]["pool_vs_v1_recomputed"] = ten_changes(base["pool90"], pool)
        frames[name] = frame
        out["variants"][name]["scope_vs_v1_recomputed"] = scope_changes(frames["v1_recomputed"], frame)
    return out


def main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Replay retained days through the a80 understand and scope code.")
    parser.add_argument("--snapshots", required=True, help="folder holding SHA256SUMS.txt and one folder per day")
    parser.add_argument("--states", required=True, help="rank-analysis.json with the pinned Nigerian detect states")
    parser.add_argument("--briefs", required=True, help="the retained briefs rows, for the recorded ten")
    parser.add_argument("--out", required=True, help="where to write the result json")
    args = parser.parse_args(argv)
    snap = Path(args.snapshots)
    result = {"checksums_verified": verify_snapshots(snap),
              "states_sha256": _sha256(args.states), "briefs_sha256": _sha256(args.briefs),
              "days": {}, "pinned": {}}
    for day in DATES:
        result["days"][day] = replay_day(snap, day)
        recorded = RECORDED_UNDERSTAND[day]
        for kind, cell in result["days"][day]["understand"].items():
            cell["recorded"] = dict(zip(("clusters", "members", "today_posts", "posts"), recorded[kind]))
    for day in PINNED_DAYS:
        result["pinned"][day] = replay_pinned(snap, args.states, day, args.briefs)
    Path(args.out).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()

