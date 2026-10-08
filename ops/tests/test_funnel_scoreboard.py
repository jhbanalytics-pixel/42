"""The read-only funnel scoreboard on a DuckDB fixture world: the real views, the brief's own candidates SQL, no
BigQuery. Every moment below is built to stop at one named stage, and failed reads are injected to check that they
land in the unknown bucket and never as a proven drop."""
import csv
import hashlib
import json
from datetime import date, timedelta

import pytest
from sqlglot import exp

from core.detect.tests import duck
from core.detect.tests.fixtures import at, rid, run
from ops.runners import funnel_scoreboard as fs

D5, D6, D7 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
POST_DAY = date(2026, 10, 4)
POOL = 3


class Job:
    def __init__(self, rows=None, error=None):
        self.rows, self.error = rows, error

    def result(self):
        if self.error:
            raise self.error
        return self.rows


def query_kind(sql):
    for kind, marker in (("cohort", "AS posts_matched"), ("items", "AS item_posts"), ("rank", "AS sql_rank"),
                         ("daystate", "AS runs"), ("state", "v_item_state_current s\nWHERE"),
                         ("briefs", "TO_JSON_STRING(payload)")):
        if marker in sql:
            return kind
    raise AssertionError(f"a query this test does not know: {sql[:80]}")


def bound(param):
    """The Python value of a query parameter: duck._value expects parameter objects inside arrays, and an array of
    strings holds plain strings."""
    if hasattr(param, "struct_values"):
        return dict(param.struct_values)
    if hasattr(param, "values"):
        return [bound(v) if hasattr(v, "values") or hasattr(v, "struct_values") else v for v in param.values]
    return param.value


class DuckClient:
    """Runs the scoreboard's own SQL text on DuckDB. fail names the kinds of query that raise."""

    def __init__(self, con, fail=()):
        self.con, self.fail, self.sql, self.configs, self.kinds = con, set(fail), [], [], []

    def query(self, sql, job_config=None):
        kind = query_kind(sql)
        self.sql.append(sql)
        self.configs.append(job_config)
        self.kinds.append(kind)
        if kind in self.fail:
            return Job(error=RuntimeError("injected failure"))
        text = sql.replace(fs.CORE_Q, "core").replace(fs.AGENT_Q, "agent")
        params = {p.name: bound(p) for p in (job_config.query_parameters if job_config else [])}
        tree = duck._tree(text)
        used = {p.name for p in tree.find_all(exp.Parameter)}
        # BigQuery expands an array of structs into columns; DuckDB needs the recursive option for the same thing.
        text = tree.sql(dialect="duckdb").replace("SELECT * FROM UNNEST($moments)",
                                                  "SELECT UNNEST($moments, recursive := true)")
        cur = self.con.execute(text, {k: v for k, v in params.items() if k in used})
        names = [c[0] for c in cur.description]
        return Job([dict(zip(names, r)) for r in cur.fetchall()])


class World:
    def __init__(self, detect_days=(D5, D6, D7)):
        self.con = duck.connect()
        self.con.execute("ALTER TABLE core.post_enrichment ADD COLUMN screen_text VARCHAR")
        self.con.execute("ALTER TABLE core.post_enrichment ADD COLUMN video_notes VARCHAR")
        self.moments = []
        duck.load(self.con, "agent.runs", [run("detect", d) for d in detect_days]
                  + [run("brief", d) for d in (D5, D6, D7)])

    def post(self, pid, text="", *, market="NG", located=True, lane="sweep", lane_class="unbiased_rank",
             hashtags=(), screen_text=None, observe=True, items=(), geo="NG"):
        duck.load(self.con, "core.creators", [{"creator_id": f"c-{pid}", "platform": "tiktok",
                                               "handle": f"@c-{pid}", "coord_score": 0}])
        duck.load(self.con, "core.posts", [{
            "post_id": pid, "platform": "tiktok", "creator_id": f"c-{pid}", "text": text, "hashtags": list(hashtags),
            "published_at": at(POST_DAY, 9), "post_date": POST_DAY, "engagement": 10,
            "geo_market": geo if located else None, "geo_confidence": 0.9 if located else None,
            "geo_source": "ext_region" if located else None}])
        if screen_text:
            duck.load(self.con, "core.post_enrichment", [{"post_id": pid, "screen_text": screen_text}])
        if observe:
            duck.load(self.con, "core.post_observations", [{
                "post_id": pid, "observed_at": at(POST_DAY, 10), "observed_date": POST_DAY, "market": market,
                "platform": "tiktok", "lane": lane, "lane_class": lane_class, "run_id": "collect-fixture"}])
        for item in items:
            duck.load(self.con, "core.post_items", [{"post_id": pid, "item_id": item, "via": "hashtag"}])

    def item(self, item_id, worth, *, eligible=True, state="spike", market="NG", days=(D5, D6, D7), label=None):
        duck.load(self.con, "core.cultural_map", [{
            "item_id": item_id, "kind": "hashtag", "canonical_key": item_id, "label": label or f"#{item_id}",
            "first_seen": POST_DAY, "status": "active", "valid_from": at(POST_DAY - timedelta(days=400)),
            "valid_to": None}])
        for d in days:
            duck.load(self.con, "core.item_state", [{
                "metric_date": d, "market": market, "item_id": item_id, "kind": "hashtag", "state_raw": state,
                "state": state, "untested": False, "creators3": 5, "posts3": 10, "worth_raw": worth,
                "eligible": eligible, "geo_status": "local", "run_id": rid("detect", d), "rule_version": "r1"}])

    def brief(self, day, market="NG", *, cards=(), held=(), not_assessed=()):
        payload = {"status": "published", "cards": [dict(c) for c in cards], "more": [],
                   "held_back": {"count": len(held), "items": [dict(h) for h in held]},
                   "not_assessed": {"count": len(not_assessed), "items": [dict(n) for n in not_assessed]}}
        duck.load(self.con, "agent.briefs", [{
            "brief_date": day, "market": market, "run_id": rid("brief", day), "published_at": at(day, 4),
            "status": "published", "payload": json.dumps(payload)}])

    def moment(self, mid, word, *, market="NG", klass="", and_re="", day=D5, name=None):
        self.moments.append({"id": mid, "date": day.isoformat(), "market": market, "moment": name or f"{word} moment",
                             "any_re": rf"\b{word}\b", "and_re": and_re, "class": klass})


def build_world(**options):
    w = World(**options)
    # A: five local posts, an eligible item ranked first, published as a card.
    for n in range(5):
        w.post(f"a{n}", f"alpha clip {n}", items=("itemA",))
    w.item("itemA", 0.90)
    w.moment("A", "alpha", klass="social")
    # B: four local posts, an eligible item ranked third, held by G10.
    for n in range(4):
        w.post(f"b{n}", f"beta clip {n}", items=("itemB",))
    w.item("itemB", 0.80)
    w.moment("B", "beta", klass="social")
    # C: three local posts, eligible, ranked fourth: outside a pool of three.
    for n in range(3):
        w.post(f"c{n}", f"gamma clip {n}", items=("itemC",))
    w.item("itemC", 0.70)
    w.moment("C", "gamma", klass="news")
    # D: three local posts, an item_state row that is not eligible.
    for n in range(3):
        w.post(f"d{n}", f"delta clip {n}", items=("itemD",))
    w.item("itemD", 0.60, eligible=False, state="quiet")
    w.moment("D", "delta", klass="news")
    # E: three local posts linked to an item that has no item_state row on any day.
    for n in range(3):
        w.post(f"e{n}", f"epsilon clip {n}", items=("itemE",))
    w.moment("E", "epsilon", klass="social")
    # F: three local posts, eligible, ranked second, in the pool, and the brief recorded it as not assessed.
    for n in range(3):
        w.post(f"f{n}", f"zeta clip {n}", items=("itemF",))
    w.item("itemF", 0.85)
    w.moment("F", "zeta", klass="social")
    # G: nothing matches.
    w.moment("G", "nomatchword", klass="news")
    # H: two posts match but were only ever observed in ZA.
    for n in range(2):
        w.post(f"h{n}", f"eta clip {n}", market="ZA", geo="ZA")
    w.moment("H", "eta", klass="news")
    # I: two posts collected in NG with no location and no item.
    for n in range(2):
        w.post(f"i{n}", f"theta clip {n}", located=False)
    w.moment("I", "theta", klass="news")
    # J: two located posts on one item: below the link rule's three posts.
    for n in range(2):
        w.post(f"j{n}", f"iota clip {n}", items=("itemJ",))
    w.item("itemJ", 0.5)
    w.moment("J", "iota", klass="social")
    # K: three matching posts on a generic item of forty: under the link rule's share.
    for n in range(3):
        w.post(f"k{n}", f"kappa clip {n}", items=("itemGen",))
    for n in range(37):
        w.post(f"gen{n}", f"unrelated filler {n}", items=("itemGen",))
    w.item("itemGen", 0.4)
    w.moment("K", "kappa", klass="social")
    # L: matches by hashtag twice and by on-screen text once, never by the post text.
    w.post("l0", "plain words", hashtags=("lambda",), items=("itemL",))
    w.post("l1", "plain words", hashtags=("lambda",), items=("itemL",))
    w.post("l2", "plain words", screen_text="a lambda banner", items=("itemL",))
    w.moment("L", "lambda", klass="social")
    # N: three posts, observed only in a placebo lane.
    for n in range(3):
        w.post(f"n{n}", f"nu clip {n}", lane="placebo", lane_class="search_presence")
    w.moment("N", "nu", klass="news")
    # M: a ZA moment whose posts are linked to an item; there is no ZA item_state on any day.
    for n in range(3):
        w.post(f"m{n}", f"mu clip {n}", market="ZA", geo="ZA", items=("itemM",))
    w.moment("M", "mu", market="ZA", klass="social")

    # P: three NG posts on an item that has twenty more posts, all observed in ZA: the share is the market's own.
    for n in range(3):
        w.post(f"p{n}", f"pword clip {n}", items=("itemP",))
    for n in range(20):
        w.post(f"pz{n}", f"elsewhere filler {n}", market="ZA", geo="ZA", items=("itemP",))
    w.moment("P", "pword", klass="social")
    # Q: three matching posts on an item of twelve in NG: exactly the link rule's share of a quarter.
    for n in range(3):
        w.post(f"q{n}", f"qword clip {n}", items=("itemQ",))
    for n in range(9):
        w.post(f"qf{n}", f"quiet filler {n}", items=("itemQ",))
    w.moment("Q", "qword", klass="social")

    held = {"item_id": "itemB", "title": "Beta topic", "rule": "G10", "reason": "explanation_failed"}
    card = {"item_id": "itemA", "title": "Alpha topic"}
    recorded = {"item_id": "itemF", "title": "Zeta topic", "status": "not_assessed", "reason": "judged_limit_reached",
                "sql_rank": 2, "pool_rank": 2, "market_scope": "market"}
    w.brief(D5, cards=[card], held=[held], not_assessed=[recorded])
    w.brief(D6)
    w.brief(D7)
    return w


@pytest.fixture(scope="module")
def world():
    return build_world()


def write_moments(tmp_path, moments):
    path = tmp_path / "moments.csv"
    cols = ("id", "date", "market", "moment", "any_re", "and_re", "class")
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows([{k: m[k] for k in cols} for m in moments])
    return path


def run_scoreboard(tmp_path, world_, *, fail=(), extra=(), cutoff="2026-10-07", moments=None, client=None):
    path = write_moments(tmp_path, moments if moments is not None else world_.moments)
    client = client or DuckClient(world_.con, fail)
    lines = []
    out_dir = tmp_path / "readback"
    code = fs.main([str(path), "--cutoff", cutoff, "--pool", str(POOL), "--out-dir", str(out_dir), *extra],
                   client=client, out=lines.append)
    [csv_path] = out_dir.glob("*.csv")
    [json_path] = out_dir.glob("*.json")
    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = {r["id"]: r for r in csv.DictReader(f)}
    detail = json.loads(json_path.read_text(encoding="utf-8"))
    return code, rows, detail, lines, client


@pytest.fixture(scope="module")
def default_run(world, tmp_path_factory):
    return run_scoreboard(tmp_path_factory.mktemp("scoreboard"), world)


# Stages reached, moment by moment.

EXPECTED = {
    "A": "CARD", "B": "HELD", "C": "ELIGIBLE", "D": "STATED", "E": "ITEMISED", "F": "POOLED", "G": "NOT_COLLECTED",
    "H": "NOT_COLLECTED", "I": "COLLECTED", "J": "LOCATED", "K": "LOCATED", "L": "ITEMISED", "M": "ITEMISED",
    "N": "NOT_COLLECTED", "P": "ITEMISED", "Q": "ITEMISED",
}


@pytest.mark.parametrize("mid, stage", sorted(EXPECTED.items()))
def test_each_moment_stops_at_the_stage_it_was_built_to_stop_at(default_run, mid, stage):
    _code, rows, _detail, _lines, _client = default_run
    assert rows[mid]["furthest_stage"] == stage


def test_the_card_moment_reports_every_stage_it_passed(default_run):
    r = default_run[1]["A"]
    assert (r["posts_matched"], r["posts_collected"], r["creators"], r["located"], r["unitemised_posts"]) == (
        "5", "5", "5", "5", "0")
    assert (r["items_linked"], r["items_counted"], r["best_item"]) == ("1", "1", "itemA")
    assert (r["state"], r["eligible"], r["sql_rank"], r["rank_source"]) == ("spike", "True", "1",
                                                                          "recomputed retrospectively")
    assert (r["market_scope"], r["scope_bucket"], r["in_pool"], r["judged"], r["card"]) == (
        "market", "0", "True", "True", "True")
    assert r["furthest_day"] == "2026-10-05" and r["best_item_label"] == "Alpha topic"
    assert r["next_stage"] == "" and r["next_stage_status"] == "reached the last stage"


def test_a_held_moment_names_the_hold_and_why_it_did_not_reach_a_card(default_run):
    r = default_run[1]["B"]
    assert r["hold_reason"] == "G10 explanation_failed" and r["judged"] == "True" and r["card"] == "False"
    assert r["sql_rank"] == "3" and r["in_pool"] == "True"
    assert (r["next_stage"], r["next_stage_status"]) == ("CARD", "held G10 explanation_failed")


def test_a_pool_miss_shows_its_rank_scope_and_the_pool_it_missed(default_run):
    r = default_run[1]["C"]
    assert (r["sql_rank"], r["market_scope"], r["scope_bucket"], r["in_pool"], r["judged"]) == (
        "4", "market", "0", "False", "False")
    assert r["next_stage"] == "POOLED"
    assert r["next_stage_status"] == "dropped: SQL rank 4 is outside the pool of 3"
    assert r["unknown_reasons"] == ""


def test_an_item_that_is_not_eligible_stops_at_stated_with_its_state(default_run):
    r = default_run[1]["D"]
    assert (r["state"], r["eligible"]) == ("quiet", "False")
    assert r["next_stage"] == "ELIGIBLE" and r["next_stage_status"] == "dropped: not eligible (state quiet)"


def test_an_item_with_no_state_row_on_a_day_that_has_state_rows_is_a_proven_drop(default_run):
    r = default_run[1]["E"]
    assert (r["items_counted"], r["state"]) == ("1", "")
    assert r["next_stage"] == "STATED"
    assert r["next_stage_status"].startswith("dropped: no item_state row for this item on this day (")
    assert r["unknown_reasons"] == ""


def test_an_in_pool_item_the_brief_recorded_as_not_assessed_uses_the_recorded_rank(default_run):
    r = default_run[1]["F"]
    assert (r["sql_rank"], r["rank_source"], r["not_assessed_reason"]) == ("2", "recorded in the brief",
                                                                          "judged_limit_reached")
    assert r["next_stage"] == "HELD"
    assert r["next_stage_status"] == "dropped: in the pool but not judged (judged_limit_reached)"


def test_posts_seen_only_outside_the_market_are_not_collected_and_say_so(default_run):
    r = default_run[1]["H"]
    assert (r["posts_matched"], r["posts_collected"]) == ("2", "0")
    assert r["next_stage_status"] == ("dropped: 2 matching posts exist but none was observed in NG by the cutoff "
                                      "outside the excluded lanes")


def test_posts_seen_only_in_an_excluded_lane_are_not_collected(default_run):
    r = default_run[1]["N"]
    assert (r["posts_matched"], r["posts_collected"]) == ("3", "0")


def test_a_moment_with_no_matching_post_is_a_proven_not_collected(default_run):
    r = default_run[1]["G"]
    assert (r["posts_matched"], r["posts_collected"]) == ("0", "0")
    assert r["next_stage_status"] == "dropped: no post matching the moment was found"
    assert r["unknown_reasons"] == ""


def test_collected_posts_with_no_location_and_no_item_stop_at_collected(default_run):
    r = default_run[1]["I"]
    assert (r["posts_collected"], r["located"], r["unitemised_posts"], r["items_linked"]) == ("2", "0", "2", "0")
    assert r["next_stage"] == "LOCATED" and r["next_stage_status"] == "dropped: none of the posts is located in the market"


def test_an_item_below_the_link_rule_is_reported_but_does_not_count(default_run):
    j, k = default_run[1]["J"], default_run[1]["K"]
    assert (j["items_linked"], j["items_counted"]) == ("1", "0")
    assert j["next_stage"] == "ITEMISED" and "no item meets the link rule (items linked 1, the most matching posts in one is 2" in j["next_stage_status"]
    assert (k["items_linked"], k["items_counted"]) == ("1", "0")
    assert "the most matching posts in one is 3" in k["next_stage_status"]


def test_hashtags_and_on_screen_text_match_not_only_the_post_text(default_run):
    r = default_run[1]["L"]
    assert (r["posts_matched"], r["posts_collected"], r["items_counted"]) == ("3", "3", "1")


def test_a_market_with_no_state_rows_at_all_puts_the_stage_in_the_unknown_bucket(default_run):
    r = default_run[1]["M"]
    assert r["furthest_stage"] == "ITEMISED" and r["next_stage"] == "STATED"
    assert r["next_stage_status"] == "unknown: no item_state rows for this market on this day"
    assert "STATED: no item_state rows for this market on this day" in r["unknown_reasons"]


# Denominators.


def test_the_summary_gives_every_denominator_and_they_add_up(default_run):
    summary = default_run[2]["summary"]
    allm = summary["all"]
    assert allm["moments"] == 16 and sum(allm["stopped_at"].values()) == 16
    assert allm["stopped_at"] == {"NOT_COLLECTED": 3, "COLLECTED": 1, "LOCATED": 2, "ITEMISED": 5, "STATED": 1,
                                  "ELIGIBLE": 1, "POOLED": 1, "HELD": 1, "CARD": 1, "UNKNOWN": 0}
    assert allm["reached_at_least"] == {"COLLECTED": 13, "LOCATED": 12, "ITEMISED": 10, "STATED": 5, "ELIGIBLE": 4,
                                        "POOLED": 3, "HELD": 2, "CARD": 1}
    assert allm["with_unknown_bucket"] == 1
    assert summary["market NG"]["moments"] == 15 and summary["market ZA"]["moments"] == 1
    assert "market KE" not in summary
    assert summary["class social"]["moments"] + summary["class news"]["moments"] == 16


def test_a_class_column_splits_the_denominator_and_an_absent_one_is_unclassified(world, tmp_path):
    moments = [dict(m, **{"class": ""}) for m in world.moments if m["id"] in ("A", "G")]
    _code, _rows, detail, _lines, _client = run_scoreboard(tmp_path, world, moments=moments)
    assert detail["summary"]["class unclassified"]["moments"] == 2


# Failed reads are unknown, never a proven drop.


def test_a_failed_briefs_read_turns_a_card_into_an_unknown_pool_stage(world, tmp_path):
    code, rows, detail, lines, _client = run_scoreboard(tmp_path, world, fail=("briefs",))
    assert code == 1
    assert rows["A"]["furthest_stage"] == "POOLED"
    assert rows["A"]["next_stage_status"] == "unknown: briefs read failed"
    assert "HELD: briefs read failed" in rows["A"]["unknown_reasons"]
    assert rows["B"]["furthest_stage"] == "POOLED" and "briefs read failed" in rows["B"]["next_stage_status"]
    assert "briefs" in detail["read_errors"] and any(line.startswith("ERROR briefs read failed") for line in lines)


def test_a_failed_cohort_read_makes_every_moment_unknown_not_not_collected(world, tmp_path):
    code, rows, detail, _lines, _client = run_scoreboard(tmp_path, world, fail=("cohort",))
    assert code == 1 and {r["furthest_stage"] for r in rows.values()} == {"UNKNOWN"}
    assert detail["summary"]["all"]["stopped_at"]["UNKNOWN"] == 16
    assert detail["summary"]["all"]["stopped_at"]["NOT_COLLECTED"] == 0
    assert rows["G"]["posts_matched"] == "" and rows["G"]["next_stage_status"] == "unknown: cohort read failed"


def test_a_failed_items_read_leaves_the_stage_at_what_the_cohort_proved(world, tmp_path):
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path, world, fail=("items",))
    assert rows["A"]["furthest_stage"] == "LOCATED"
    assert rows["A"]["next_stage_status"] == "unknown: items read failed"
    assert rows["A"]["items_linked"] == "" and rows["G"]["furthest_stage"] == "NOT_COLLECTED"


def test_a_failed_state_read_is_unknown_not_a_missing_item_state_row(world, tmp_path):
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path, world, fail=("state",))
    assert rows["E"]["next_stage_status"] == "unknown: item_state read failed"
    assert rows["E"]["furthest_stage"] == "ITEMISED"


def test_a_failed_day_row_count_read_is_unknown_not_a_missing_item_state_row(world, tmp_path):
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path, world, fail=("daystate",))
    assert rows["E"]["next_stage_status"] == "unknown: item_state read failed"


def test_a_failed_rank_read_is_unknown_not_a_pool_miss(world, tmp_path):
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path, world, fail=("rank",))
    assert rows["C"]["furthest_stage"] == "ELIGIBLE"
    assert rows["C"]["next_stage_status"] == "unknown: rank read failed"
    assert rows["C"]["sql_rank"] == "" and rows["C"]["in_pool"] == ""
    assert rows["F"]["sql_rank"] == "2" and rows["F"]["rank_source"] == "recorded in the brief"


def test_a_day_with_no_detect_run_is_unknown_for_a_moment_that_stopped_before_state():
    w = build_world(detect_days=(D5, D6))
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        _code, rows, _detail, _lines, _client = run_scoreboard(Path(tmp), w)
    e = rows["E"]
    assert e["furthest_stage"] == "ITEMISED"
    assert "STATED: no item_state rows for this market on this day" in e["unknown_reasons"]
    assert rows["A"]["furthest_stage"] == "CARD"


def test_a_missing_brief_day_does_not_hide_a_proven_drop_but_does_bound_a_pooled_item(tmp_path):
    w = build_world()
    w.con.execute("DELETE FROM agent.briefs WHERE brief_date = DATE '2026-10-07'")
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path, w)
    assert rows["F"]["furthest_stage"] == "POOLED"
    assert "HELD: no brief for this market on this day" in rows["F"]["unknown_reasons"]
    assert rows["C"]["unknown_reasons"] == ""
    assert rows["A"]["furthest_stage"] == "CARD"


def test_a_cutoff_before_the_moment_leaves_no_evaluation_day_and_says_so(world, tmp_path):
    moments = [m for m in world.moments if m["id"] == "A"]
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path, world, cutoff="2026-10-04", moments=moments)
    assert rows["A"]["furthest_stage"] == "ITEMISED"
    assert rows["A"]["next_stage_status"] == "unknown: no evaluation day on or before the cutoff"
    assert "no evaluation day on or before the cutoff" in rows["A"]["unknown_reasons"]


# Parameters are recorded; the cutoff is required.


def test_the_run_saves_every_parameter_the_match_rule_and_a_digest_of_the_moments(world, tmp_path):
    _code, _rows, detail, _lines, client = run_scoreboard(tmp_path, world, extra=("--exclude-lane", "placebo"))
    params = detail["parameters"]
    assert params == {"cutoff": "2026-10-07", "post_before": 2, "post_after": 3, "brief_days": 3,
                      "exclude_lanes": ["placebo"], "located_min_confidence": 0.7, "item_min_posts": 3,
                      "item_min_share": 0.25, "item_cap": 50, "pool": POOL,
                      "max_bytes": fs.MAX_BYTES}
    assert detail["match_rule"] == fs.MATCH_RULE and detail["rank_basis"] == fs.RANK_BASIS
    digest = hashlib.sha256((tmp_path / "moments.csv").read_bytes()).hexdigest()
    assert detail["moments_sha256"] == digest
    bound = {q.name: getattr(q, "value", None) for q in client.configs[0].query_parameters}
    assert bound["cutoff"] == date(2026, 10, 7) and bound["start"] == date(2026, 10, 3)
    assert bound["end"] == date(2026, 10, 8)


def test_the_recorded_excluded_lanes_are_the_ones_in_the_sql(world, tmp_path):
    _code, _rows, detail, _lines, client = run_scoreboard(tmp_path, world, extra=("--exclude-lane", "placebo"))
    cohort = next(sql for sql, kind in zip(client.sql, client.kinds) if kind == "cohort")
    assert "NOT IN ('placebo')" in cohort and "agent_live" not in cohort
    assert detail["parameters"]["exclude_lanes"] == ["placebo"]


def test_the_cutoff_has_no_default(world, tmp_path, capsys):
    path = write_moments(tmp_path, world.moments)
    client = DuckClient(world.con)
    with pytest.raises(SystemExit) as stopped:
        fs.main([str(path)], client=client, out=lambda line: None)
    assert stopped.value.code == 2 and client.sql == []
    assert "--cutoff" in capsys.readouterr().err


def test_a_changed_pool_changes_the_stage_it_is_recorded_with(world, tmp_path):
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path, world, extra=("--pool", "4"))
    assert rows["C"]["furthest_stage"] == "POOLED"


def test_a_stricter_link_rule_drops_an_item_and_a_looser_one_admits_it(world, tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path / "one", world, extra=("--item-min-posts", "2"))
    assert rows["J"]["items_counted"] == "1"
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path / "two", world, extra=("--item-min-share", "0.05"))
    assert rows["K"]["items_counted"] == "1"


# Read-only.


def test_every_statement_run_is_a_capped_select(default_run):
    _code, _rows, _detail, _lines, client = default_run
    assert client.sql
    for sql in client.sql:
        fs.read_only(sql)
    assert all(cfg.maximum_bytes_billed == fs.MAX_BYTES for cfg in client.configs)
    assert {"cohort", "items", "state", "daystate", "rank", "briefs"} <= set(client.kinds)


@pytest.mark.parametrize("sql", ["DELETE FROM t", "SELECT 1; DROP TABLE t", "WITH x AS (SELECT 1) INSERT INTO t SELECT 1",
                                 "  update t set a = 1", "SELECT 1 -- ok\n; ALTER TABLE t ADD COLUMN c INT64",
                                 "CALL proc()"])
def test_the_text_check_refuses_anything_but_a_select(sql):
    with pytest.raises(SystemExit):
        fs.read_only(sql)


def test_the_text_check_ignores_write_words_in_comments_and_quoted_text():
    fs.read_only("SELECT 'update me', \"drop\" FROM `project.dataset.create_table` -- delete everything\n")


def test_the_scoreboard_never_builds_its_own_client_when_one_is_given(world, tmp_path, monkeypatch):
    monkeypatch.setattr(fs.bigquery, "Client", lambda **kwargs: (_ for _ in ()).throw(AssertionError("built")))
    run_scoreboard(tmp_path, world)


# The rank is the brief's own.


def test_the_recomputed_rank_equals_the_ranks_the_briefs_own_candidates_query_gives(world):
    from core.brief import job

    brief_rows = duck.query(world.con, job.QUERIES["candidates"], {"d": D5, "market": "NG"})
    by_brief = {r["item_id"]: r["_selection_sql_rank"] for r in brief_rows}
    ours = duck.query(world.con, fs.rank_sql().replace(fs.CORE_Q, "{core}").replace(fs.AGENT_Q, "{agent}"),
                      {"d": D5, "market": "NG", "item_ids": sorted(by_brief)})
    assert {r["item_id"]: r["sql_rank"] for r in ours} == by_brief
    assert {r["item_id"]: r["sql_rank"] for r in ours}["itemA"] == 1


def test_the_rank_query_has_no_limit_and_gives_ranks_past_the_pool(world):
    sql = fs.rank_sql()
    assert "LIMIT 90" not in sql and "LIMIT" not in sql.replace("LIMIT 1)", "")
    rows = duck.query(world.con, sql.replace(fs.CORE_Q, "{core}").replace(fs.AGENT_Q, "{agent}"),
                      {"d": D5, "market": "NG", "item_ids": ["itemC", "itemD"]})
    assert {r["item_id"]: r["sql_rank"] for r in rows} == {"itemC": 4, "itemD": 7}
    assert rows[0]["market_scope"] == "market" and rows[0]["total_posts7"] >= 3


def test_the_scope_bucket_follows_the_briefs_case():
    assert fs.scope_bucket("market", 3, 2) == 0
    assert fs.scope_bucket("market", 12, 6) == 0
    assert fs.scope_bucket("market", 2, 2) == 1
    assert fs.scope_bucket("market", 4, 1) == 1
    assert fs.scope_bucket("global", 8, 4) == 2
    assert fs.scope_bucket(None, None, None) == 2
    assert fs.scope_bucket("market", None, None) is None


def test_a_brief_sql_that_loses_the_shape_this_needs_stops_the_run(tmp_path, monkeypatch):
    broken = tmp_path / "brief.sql"
    broken.write_text("-- name: candidates\nWITH seen AS (SELECT 1)\nSELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(fs, "BRIEF_SQL_FILE", broken)
    with pytest.raises(SystemExit) as stopped:
        fs.rank_sql()
    assert "no longer has the candidates shape" in str(stopped.value)


def test_the_pool_default_is_the_briefs_pool():
    from core.brief import job

    assert fs.Params(cutoff=D7).pool == job.POOL


@pytest.mark.parametrize("changes", [{"pool": -1}, {"item_min_share": 1.5}, {"exclude_lanes": ("Bad Lane",)},
                                     {"post_before": 1.5}])
def test_bad_parameters_are_refused(changes):
    with pytest.raises(ValueError):
        fs.Params(cutoff=D7, **changes)


def test_moments_are_validated_and_ids_may_not_repeat(tmp_path):
    path = tmp_path / "m.csv"
    path.write_text("id,date,market,moment,any_re,and_re\nX,2026-10-05,NG,one,a,\nX,2026-10-06,NG,two,b,\n",
                    encoding="utf-8")
    with pytest.raises(SystemExit):
        fs.load_moments(path)
    path.write_text("id,date,market,moment,any_re,and_re\nX,2026-10-05,US,one,a,\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        fs.load_moments(path)


# Assembly on hand-made facts.


def hand_moment(mid="X", market="NG", day=D5):
    return {"id": mid, "date": day.isoformat(), "d": day, "market": market, "moment": "hand", "class": "social",
            "any_re": "x", "and_re": ""}


def hand_facts(items, state=None, rank=None, briefs=None, day=D5, linked=None):
    rows = [{"id": "X", "item_id": i, "matched_posts": m, "item_posts": t, "items_linked": linked or len(items)}
            for i, m, t in items]
    return {"cohort": {"X": {"posts_matched": 9, "posts_collected": 9, "creators": 5, "located": 5, "unitemised": 0}},
            "items": {"X": rows}, "state": state or {}, "rank": rank or {}, "briefs": briefs}


def state_for(*ids, day=D5, eligible=True):
    return {(day, "NG"): {"rows": {i: {"state": "spike", "eligible": eligible} for i in ids}, "day_rows": len(ids)}}


def rank_for(ranks, day=D5):
    return {(day, "NG"): {i: {"sql_rank": r, "market_scope": "market", "total_posts7": 6, "market_posts7": 4,
                              "label": i} for i, r in ranks.items()}}


EMPTY_BRIEF = {("2026-10-05", "NG"): {"cards": {}, "held": {}, "not_assessed": {}}}


def test_two_items_at_the_same_stage_report_the_better_ranked_one():
    facts = hand_facts([("i1", 5, 5), ("i2", 5, 5)], state_for("i1", "i2"), rank_for({"i1": 5, "i2": 2}), EMPTY_BRIEF)
    row = fs.assess_moment(hand_moment(), facts, fs.Params(cutoff=D5, brief_days=0))
    assert (row["furthest_stage"], row["best_item"], row["sql_rank"]) == ("POOLED", "i2", 2)
    assert row["items_counted"] == 2 and len(row["evidence"]) == 2


def test_an_item_list_cut_at_the_cap_goes_to_the_unknown_bucket_when_the_cut_rows_could_still_count():
    facts = hand_facts([("i1", 5, 5), ("i2", 4, 4)], state_for("i1", "i2"), rank_for({"i1": 5, "i2": 2}),
                       EMPTY_BRIEF, linked=80)
    row = fs.assess_moment(hand_moment(), facts, fs.Params(cutoff=D5, brief_days=0, item_cap=2))
    assert "ITEMISED: item list cut at 2 of 80 linked items" in row["unknown_reasons"]


def test_an_item_list_cut_below_the_link_rule_loses_nothing_and_is_not_unknown():
    facts = hand_facts([("i1", 5, 5), ("i2", 2, 2)], state_for("i1"), rank_for({"i1": 1}), EMPTY_BRIEF, linked=80)
    row = fs.assess_moment(hand_moment(), facts, fs.Params(cutoff=D5, brief_days=0, item_cap=2))
    assert "item list cut" not in row["unknown_reasons"]


def test_the_earlier_day_wins_a_tie_on_stage_and_rank():
    day6 = D6
    state = {**state_for("i1"), **state_for("i1", day=day6)}
    rank = {**rank_for({"i1": 3}), **rank_for({"i1": 3}, day=day6)}
    briefs = {**EMPTY_BRIEF, ("2026-10-06", "NG"): {"cards": {}, "held": {}, "not_assessed": {}}}
    row = fs.assess_moment(hand_moment(), hand_facts([("i1", 5, 5)], state, rank, briefs),
                           fs.Params(cutoff=D6, brief_days=1))
    assert row["furthest_day"] == "2026-10-05"


def test_a_card_on_a_later_day_beats_a_hold_on_an_earlier_one():
    state = {**state_for("i1"), **state_for("i1", day=D6)}
    rank = {**rank_for({"i1": 1}), **rank_for({"i1": 1}, day=D6)}
    briefs = {("2026-10-05", "NG"): {"cards": {}, "held": {"i1": {"rule": "G6", "reason": "not_local"}},
                                     "not_assessed": {}},
              ("2026-10-06", "NG"): {"cards": {"i1": {"title": "T"}}, "held": {}, "not_assessed": {}}}
    row = fs.assess_moment(hand_moment(), hand_facts([("i1", 5, 5)], state, rank, briefs),
                           fs.Params(cutoff=D6, brief_days=1))
    assert (row["furthest_stage"], row["furthest_day"], row["card"], row["hold_reason"]) == (
        "CARD", "2026-10-06", True, None)


def test_proof_of_a_brief_judgement_stands_even_when_state_and_rank_could_not_be_read():
    briefs = {("2026-10-05", "NG"): {"cards": {"i1": {"title": "T"}}, "held": {}, "not_assessed": {}}}
    row = fs.assess_moment(hand_moment(), hand_facts([("i1", 5, 5)], {(D5, "NG"): None}, {(D5, "NG"): None}, briefs),
                           fs.Params(cutoff=D5, brief_days=0))
    assert row["furthest_stage"] == "CARD" and row["card"] is True
    assert row["unknown_reasons"] == ""  # nothing above a card is left unproven


def test_a_failed_brief_read_leaves_judged_and_card_unknown_not_false(world, tmp_path):
    _code, rows, _detail, _lines, _client = run_scoreboard(tmp_path, world, fail=("briefs",))
    assert rows["A"]["judged"] == "" and rows["A"]["card"] == "" and rows["A"]["in_pool"] == "True"
    assert rows["C"]["judged"] == "False" and rows["C"]["in_pool"] == "False"


def test_the_csv_has_exactly_the_documented_columns_and_the_json_keeps_each_items_evidence(default_run):
    _code, rows, detail, _lines, _client = default_run
    assert list(rows["A"]) == list(fs.CSV_COLUMNS)
    [a] = [r for r in detail["rows"] if r["id"] == "A"]
    assert {e["day"] for e in a["evidence"]} == {"2026-10-05", "2026-10-06", "2026-10-07"}
    assert {e["stage"] for e in a["evidence"]} == {"CARD", "POOLED"}


def test_a_summary_that_does_not_add_up_stops_the_run():
    rows = [{"furthest_stage": "BOGUS", "market": "NG", "class": "social", "unknown_reasons": ""}]
    with pytest.raises(SystemExit):
        fs.summarize(rows)


def test_nothing_prints_a_handle_or_post_text(default_run):
    text = "\n".join(default_run[3])
    assert "@c-" not in text and "clip" not in text


def test_an_items_share_is_counted_in_the_moments_own_market_only(default_run):
    r = default_run[1]["P"]
    assert (r["posts_collected"], r["items_linked"], r["items_counted"]) == ("3", "1", "1")
    assert r["furthest_stage"] == "ITEMISED"


def test_an_item_at_exactly_the_link_rules_share_counts(default_run):
    r = default_run[1]["Q"]
    assert (r["posts_collected"], r["items_linked"], r["items_counted"]) == ("3", "1", "1")


def brief_with(cards=None, held=None, more=None):
    payload = {"cards": [dict(item_id=i, title="T") for i in cards or []],
               "more": [dict(item_id=i, title="T") for i in more or []],
               "held_back": {"items": [dict(item_id=i, rule="G10", reason="explanation_failed") for i in held or []]}}
    return fs.brief_index(payload)


def stage_with(index):
    facts = hand_facts([("i1", 5, 5)], state_for("i1"), rank_for({"i1": 1}), {("2026-10-05", "NG"): index})
    return fs.assess_moment(hand_moment(), facts, fs.Params(cutoff=D5, brief_days=0))


def test_an_item_both_carded_and_held_in_one_brief_counts_as_a_card():
    assert stage_with(brief_with(cards=["i1"], held=["i1"]))["furthest_stage"] == "CARD"


def test_a_card_in_the_more_list_is_a_card():
    row = stage_with(brief_with(more=["i1"]))
    assert row["furthest_stage"] == "CARD" and row["card"] is True


# Replay of the 7 October trace (42-handoff/t4-moments/REPORT.md and facts.json). The states, eligibility, SQL ranks and
# scopes below are the ones that trace reconstructed for the BBNaija topic and the Timini card on 5 and 7 October; the
# per-item post counts are arbitrary inputs that only satisfy the link rule.

BBNAIJA = "1b6715ab16faf660a2c440ed13d03d35d6152838b6e83843b2934a06f21684db"
TIMINI = "306b75a27e592b0d4fcbb6923937bd5408b8c6e1b022e4db00c8371cd11a9eba"


def t4_facts(item_id, days):
    """days: {date: (state, eligible, sql_rank, market_scope, total7, market7, brief index or None)}."""
    state, rank, briefs = {}, {}, {}
    for day, (st, elig, sql_rank, scope, total, market_posts, brief) in days.items():
        state[(day, "NG")] = {"rows": {item_id: {"state": st, "eligible": elig}}, "day_rows": 150}
        rank[(day, "NG")] = {item_id: {"sql_rank": sql_rank, "market_scope": scope, "total_posts7": total,
                                       "market_posts7": market_posts, "label": "temi, teminators, nkem"}}
        if brief is not None:
            briefs[(day.isoformat(), "NG")] = brief
    return {"cohort": {"X": {"posts_matched": 139, "posts_collected": 139, "creators": 59, "located": 89,
                             "unitemised": 0}},
            "items": {"X": [{"id": "X", "item_id": item_id, "matched_posts": 40, "item_posts": 45,
                             "items_linked": 1}]},
            "state": state, "rank": rank, "briefs": briefs}


def test_t4_replay_the_bbnaija_topic_was_held_on_5_october(tmp_path):
    held = fs.brief_index({"held_back": {"items": [{"item_id": BBNAIJA, "rule": "G10",
                                                   "reason": "explanation_failed", "title": "temi, teminators, nkem"}]}})
    facts = t4_facts(BBNAIJA, {D5: ("spike", True, 1, "market", 12, 7, held)})
    row = fs.assess_moment(hand_moment(day=D5), facts, fs.Params(cutoff=D5, brief_days=0, pool=90))
    assert (row["furthest_stage"], row["hold_reason"], row["sql_rank"], row["scope_bucket"]) == (
        "HELD", "G10 explanation_failed", 1, 0)
    assert (row["posts_collected"], row["creators"], row["located"]) == (139, 59, 89)


def test_t4_replay_the_same_topic_on_7_october_is_eligible_but_ranked_outside_the_pool():
    day = D7
    facts = t4_facts(BBNAIJA, {day: ("new_to_42", True, 149, "global", 8, 4, {"cards": {}, "held": {},
                                                                              "not_assessed": {}})})
    row = fs.assess_moment(hand_moment(day=day), facts, fs.Params(cutoff=day, brief_days=0, pool=90))
    assert row["furthest_stage"] == "ELIGIBLE"
    assert (row["sql_rank"], row["market_scope"], row["scope_bucket"], row["in_pool"]) == (149, "global", 2, False)
    assert row["next_stage_status"] == "dropped: SQL rank 149 is outside the pool of 90"


def test_t4_replay_the_timini_card_on_5_october_and_its_unjudged_rank_18_on_7_october():
    card = fs.brief_index({"cards": [{"item_id": TIMINI, "title": "timiniegbuson"}]})
    five = fs.assess_moment(hand_moment(day=D5), t4_facts(TIMINI, {D5: ("on_the_boards", True, 5, "market", 12, 12,
                                                                        card)}),
                            fs.Params(cutoff=D5, brief_days=0))
    assert (five["furthest_stage"], five["card"], five["sql_rank"]) == ("CARD", True, 5)
    seven = fs.assess_moment(hand_moment(day=D7), t4_facts(TIMINI, {D7: ("on_the_boards", True, 18, "market", 12, 12,
                                                                          {"cards": {}, "held": {},
                                                                           "not_assessed": {}})}),
                             fs.Params(cutoff=D7, brief_days=0))
    assert seven["furthest_stage"] == "POOLED" and seven["judged"] is False
    assert seven["next_stage_status"] == "dropped: in the pool but the brief records no card or hold for it"
