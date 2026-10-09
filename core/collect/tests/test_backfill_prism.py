"""The one-off prism/profiles and tiktok/song backfill (core/collect/backfill_prism.py), offline.

Stored raw_responses rows are fed to the module in memory. Its INSERT statements run on DuckDB through a fake
client that rewrites only the table names and the @rows parameter, so the NOT EXISTS keys are tested as SQL, not
as Python. No test reaches BigQuery.
"""

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from core.collect import backfill_prism as bf
from core.collect import curated_creators, job, local_sources, writers
from core.collect.parse import COUNTER_COLUMNS, CREATOR_COLUMNS, OBSERVATION_COLUMNS, POST_COLUMNS
from core.collect.tests.test_parse import FakeGeo, fake_item_id

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DAY = date(2026, 10, 3)
CONFIG = job.load_config()
MANIFEST = curated_creators.load_manifest()
RAW_TABLE = "ogilvy-trends-v2.intelligence_42_core.raw_responses"
DUCK_TYPES = {"STRING": "VARCHAR", "INT64": "BIGINT", "FLOAT64": "DOUBLE", "TIMESTAMP": "TIMESTAMPTZ", "DATE": "DATE",
              "BOOL": "BOOLEAN", "ARRAY": "VARCHAR[]", "JSON": "JSON"}
TABLE_TYPES = {"posts": writers.POST_TYPES, "creators": writers.CREATOR_TYPES, "post_observations": bf.OBSERVATION_TYPES,
               "item_counter_daily": bf.COUNTER_TYPES}


def at(day, hour=0, minute=30):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def prism_body(items, tag, published="2026-10-03T08:00:00Z", posts_each=2, target=True):
    rows = []
    for e in items:
        posts = [{"post": {"id": f"{tag}-{e['handle']}-{n}", "url": f"https://example.invalid/{e['handle']}/{tag}/{n}",
                           "published_at": published, "author": {"username": e["handle"]},
                           "content": {"text": "caption"},
                           "engagement": {"views": 100 + n, "likes": 10, "comments": 2, "shares": 1}}}
                 for n in range(posts_each)]
        row = {"status": "ok", "platform": e["platform"],
               "data": {"author": {"username": e["handle"], "followers": 5000, "verified": False}},
               "posts": {"status": "ok", "items": posts}}
        if target:
            row["target"] = {"platform": e["platform"], "handle": e["handle"]}
        rows.append(row)
    return {"success": True, "data": {"results": rows}}


def song_body(sound, uses):
    body = json.loads(json.dumps(json.loads((FIXTURES / "parse_stored_song.json").read_text(encoding="utf-8"))
                                 ["tiktok_song_ZA"]["body"]))
    post = body["data"]["post"]
    post["id"], post["ext"]["music_id"], post["ext"]["use_count"] = sound, sound, uses
    return body


def raw(route, market, fetched, body, *, run_id="r-raw", seed_key=None, lane="panel", job_name="collect"):
    return {"run_id": run_id, "job": job_name, "market": market, "route": route, "lane": lane, "seed_key": seed_key,
            "fetched_at": fetched, "body": json.dumps(body)}


def desk_items(market):
    call = next(c for c in job.panel_calls(market, DAY, CONFIG) if c.route == "prism/profiles")
    return call, call.params["items"]


def gossip_fetch(market):
    return next(f for f in local_sources.plan(DAY) if f.route == "prism/profiles" and f.market == market)


def curated_calls(market, limit=60):
    return [c for c in job.panel_calls(market, DAY, CONFIG, MANIFEST, limit)
            if c.route == "prism/profiles" and c.params["items"] != desk_items(market)[1]]


def scenario():
    """Desk, gossip and two curated batches in three markets, one unlisted panel, three sound reads."""
    rows = []
    for m_index, market in enumerate(("ZA", "NG", "KE")):
        base = at(DAY, 0, 30 + m_index)
        call, items = desk_items(market)
        rows.append(raw("prism/profiles", market, base, prism_body(items, f"desk{market}"), run_id=f"r-{market}"))
        gossip = gossip_fetch(market)
        rows.append(raw("prism/profiles", market, base + timedelta(minutes=5),
                        prism_body(gossip.params["items"], f"gos{market}"), run_id=f"r-{market}"))
        for n, c in enumerate(curated_calls(market)):
            rows.append(raw("prism/profiles", market, base + timedelta(minutes=10 + n),
                            prism_body(c.params["items"], f"cur{market}{n}"), run_id=f"r-{market}"))
    # The same desk read again the next night: the same posts, a new observation each.
    rows.append(raw("prism/profiles", "ZA", at(DAY + timedelta(days=1), 0, 30), prism_body(desk_items("ZA")[1], "deskZA"),
                    run_id="r-ZA-next"))
    odd = [{"platform": "instagram", "handle": "not-in-any-list"}]
    rows.append(raw("prism/profiles", "NG", at(DAY, 0, 50), prism_body(odd, "odd"), run_id="r-NG"))
    rows.append(raw("tiktok/song", "ZA", at(date(2026, 9, 30), 17, 49), song_body("S1", 100), seed_key="S1",
                    lane="watchlist", run_id="r-s1"))
    rows.append(raw("tiktok/song", "NG", at(date(2026, 10, 1), 17, 49), song_body("S1", 130), seed_key="S1",
                    lane="watchlist", run_id="r-s2"))
    rows.append(raw("tiktok/song", "KE", at(date(2026, 10, 1), 17, 50), song_body("S2", 50), seed_key="S2",
                    lane="watchlist", run_id="r-s3"))
    return rows


CURATED = {m: len(curated_calls(m)) for m in ("ZA", "NG", "KE")}
PRISM_CALLS = 4 + 3 + sum(CURATED.values()) + 1


def plan_of(rows, run_id="bf-test", **kw):
    return bf.build(rows, run_id=run_id, item_id_fn=fake_item_id, geo_fn=FakeGeo(), **kw)


class Job:
    def __init__(self, rows=None, size=None, billed=None, affected=None, job_id="job"):
        self._rows, self.total_bytes_processed, self.total_bytes_billed = rows or [], size, billed
        self.num_dml_affected_rows, self.job_id = affected, job_id

    def result(self, **kw):
        return self._rows


class DuckBQ:
    """BigQuery as the module uses it, on DuckDB: raw_responses reads come from a list, DML runs for real."""

    def __init__(self, raw_rows=(), dry_bytes=1_000_000):
        self.raw_rows, self.dry_bytes, self.log = list(raw_rows), dry_bytes, []
        self.con = duckdb.connect()
        self.con.execute("CREATE SCHEMA core")
        for table, types in TABLE_TYPES.items():
            self.con.execute(f"CREATE TABLE core.{table} (" + ", ".join(
                f"{c} {DUCK_TYPES[t]}" for c, t in types.items()) + ")")

    def table(self, name):
        cur = self.con.execute(f"SELECT * FROM core.{name} ORDER BY ALL")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def count(self, name):
        return self.con.execute(f"SELECT COUNT(*) FROM core.{name}").fetchone()[0]

    def put(self, name, **row):
        types = TABLE_TYPES[name]
        values = {c: row.get(c) for c in types}
        self.con.execute(f"INSERT INTO core.{name} VALUES ({', '.join('?' for _ in values)})", list(values.values()))

    def query(self, sql, job_config=None, **kw):
        from google.cloud import bigquery

        config = job_config or bigquery.QueryJobConfig()
        params = {p.name: p for p in config.query_parameters or []}
        entry = {"sql": sql, "dry_run": bool(config.dry_run), "max_billed": config.maximum_bytes_billed,
                 "params": sorted(params), "kw": kw}
        self.log.append(entry)
        if config.dry_run:
            return Job(size=self.dry_bytes)
        if RAW_TABLE in sql:
            since, until = params["since"].value, params["until"].value
            routes, jobs = set(params["routes"].values), set(params["jobs"].values)
            rows = [dict(r, fetched_at=r["fetched_at"]) for r in self.raw_rows
                    if since <= r["fetched_at"].date() <= until and r["route"] in routes and r["job"] in jobs]
            return Job(rows, size=self.dry_bytes, billed=self.dry_bytes)
        text = re.sub(r"`ogilvy-trends-v2\.intelligence_42_core\.(\w+)`", r"core.\1", sql)
        if "@rows" in text:
            text = text.replace("(SELECT * FROM UNNEST(@rows))", "rows_tmp")
            table = next(t for t in TABLE_TYPES if f"core.{t}" in text)
            types = TABLE_TYPES[table]
            self.con.execute("DROP TABLE IF EXISTS rows_tmp")
            self.con.execute("CREATE TEMP TABLE rows_tmp (" + ", ".join(f"{c} {DUCK_TYPES[t]}" for c, t in types.items()) + ")")
            for struct in params["rows"].values:
                vals = [v.values if hasattr(v, "values") else v for v in (struct.struct_values[c] for c in types)]
                self.con.execute(f"INSERT INTO rows_tmp VALUES ({', '.join('?' for _ in vals)})", vals)
        text = re.sub(r"SAFE\.PARSE_JSON\((S\.\w+)\)", r"TRY_CAST(\1 AS JSON)", text)
        for name in ("lo", "hi"):
            if f"@{name}" in text:
                text = text.replace(f"@{name}", f"DATE '{params[name].value}'")
        cur = self.con.execute(text)
        first = cur.fetchone()
        if text.lstrip().upper().startswith("INSERT"):
            return Job(size=self.dry_bytes, billed=self.dry_bytes, affected=first[0], job_id=f"job{len(self.log)}")
        return Job([{"n": first[0]}], size=self.dry_bytes, billed=self.dry_bytes, job_id=f"job{len(self.log)}")


def run_cli(bq, *args, receipts=None):
    argv = list(args)
    if receipts is not None:
        argv += ["--receipts-dir", str(receipts)]
    return bf.main(argv, client=bq, fns=(fake_item_id, FakeGeo()))


def apply_args(run_id="bf-run-1"):
    return ["--apply", "--run-id", run_id]


# window

@pytest.mark.parametrize("since,until", [
    (date(2026, 9, 27), date(2026, 10, 7)), (date(2026, 9, 28), date(2026, 10, 8)),
    (date(2026, 10, 5), date(2026, 10, 4)), (date(2026, 11, 1), date(2026, 11, 2))])
def test_a_range_outside_the_window_is_refused(since, until):
    with pytest.raises(bf.Refused):
        bf.check_window(since, until)


def test_the_whole_window_and_a_part_of_it_pass():
    assert bf.check_window(date(2026, 9, 28), date(2026, 10, 7)) == (date(2026, 9, 28), date(2026, 10, 7))
    assert bf.check_window(date(2026, 10, 1), date(2026, 10, 1)) == (date(2026, 10, 1), date(2026, 10, 1))


def test_apply_outside_the_window_touches_nothing(tmp_path):
    bq = DuckBQ(scenario())
    assert run_cli(bq, *apply_args(), "--since", "2026-09-27", receipts=tmp_path / "r") != 0
    assert run_cli(bq, *apply_args(), "--until", "2026-10-08", receipts=tmp_path / "r") != 0
    assert bq.log == [] and not (tmp_path / "r").exists()


def test_dry_run_outside_the_window_is_refused_too(tmp_path):
    bq = DuckBQ(scenario())
    assert run_cli(bq, "--since", "2026-09-01") != 0
    assert bq.log == []


def test_a_stored_row_fetched_outside_the_window_stops_the_run_before_any_write(tmp_path):
    rows = scenario() + [raw("tiktok/song", "ZA", at(date(2026, 10, 8), 3), song_body("S9", 5), seed_key="S9",
                             lane="watchlist")]
    bq = DuckBQ(rows)
    bq.query = _ignore_dates(bq)
    with pytest.raises(bf.Refused):
        bf.run(bq, since=date(2026, 9, 28), until=date(2026, 10, 7), apply=True, run_id="bf-run-1",
               receipts_dir=tmp_path / "r", fns=(fake_item_id, FakeGeo()))
    assert not any(e["sql"].lstrip().upper().startswith("INSERT") for e in bq.log)


def _ignore_dates(bq):
    """A read that returns every stored row whatever the dates: the module must not trust the SQL's filter."""
    original = bq.query

    def query(sql, job_config=None, **kw):
        if RAW_TABLE in sql and not job_config.dry_run:
            bq.log.append({"sql": sql, "dry_run": False, "max_billed": job_config.maximum_bytes_billed,
                           "params": [], "kw": kw})
            return Job([dict(r) for r in bq.raw_rows])
        return original(sql, job_config=job_config, **kw)
    return query


def test_a_stored_row_of_another_route_stops_the_run_too(tmp_path):
    rows = scenario() + [raw("tiktok/trending", "ZA", at(DAY, 1), {"success": True, "data": {}}, lane="sweep")]
    bq = DuckBQ(rows)
    bq.query = _ignore_dates(bq)
    with pytest.raises(bf.Refused):
        bf.run(bq, since=date(2026, 9, 28), until=date(2026, 10, 7), apply=True, run_id="bf-run-1",
               receipts_dir=tmp_path / "r", fns=(fake_item_id, FakeGeo()))


# run id and modes

@pytest.mark.parametrize("args", [["--apply"], ["--apply", "--run-id", ""], ["--apply", "--run-id", "a b"],
                                  ["--apply", "--run-id", "x"]])
def test_apply_needs_a_run_id(args, tmp_path):
    bq = DuckBQ(scenario())
    assert run_cli(bq, *args, receipts=tmp_path / "r") != 0
    assert bq.log == []


def test_apply_needs_a_receipts_directory():
    bq = DuckBQ(scenario())
    assert run_cli(bq, *apply_args()) != 0
    assert bq.log == []


def test_dry_run_and_apply_are_exclusive():
    bq = DuckBQ(scenario())
    with pytest.raises(SystemExit):
        run_cli(bq, "--dry-run", "--apply", "--run-id", "bf-run-1")


def test_a_receipts_directory_that_holds_a_run_is_not_reused(tmp_path):
    bq = DuckBQ(scenario())
    assert run_cli(bq, *apply_args("bf-run-1"), receipts=tmp_path / "r") == 0
    before = bq.log.copy()
    assert run_cli(bq, *apply_args("bf-run-2"), receipts=tmp_path / "r") != 0
    assert bq.log == before
    assert json.loads((tmp_path / "r" / "summary.json").read_text())["run_id"] == "bf-run-1"


def test_dry_run_is_the_default_and_writes_nothing(tmp_path, capsys):
    bq = DuckBQ(scenario())
    assert run_cli(bq) == 0
    assert not any(e["sql"].lstrip().upper().startswith("INSERT") and not e["dry_run"] for e in bq.log)
    assert any(e["sql"].lstrip().upper().startswith("INSERT") and e["dry_run"] for e in bq.log)
    assert all(bq.count(t) == 0 for t in TABLE_TYPES)
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run" and report["raw_rows"] == {"prism/profiles": PRISM_CALLS, "tiktok/song": 3}
    assert list(tmp_path.iterdir()) == []


def test_dry_run_reports_counts_estimates_and_what_would_be_new(capsys):
    bq = DuckBQ(scenario(), dry_bytes=777)
    run_cli(bq)
    report = json.loads(capsys.readouterr().out)
    expected = plan_of(scenario())
    assert report["rows"] == {"posts": len(writers.dedupe_posts(expected.posts)),
                              "creators": len(writers.dedupe_creators(expected.creators)),
                              "post_observations": len(expected.observations),
                              "item_counter_daily": len(expected.counters)}
    assert report["would_insert"] == report["rows"]
    assert report["estimated_bytes"]["read"] == 777 and report["estimated_bytes"]["posts"] == 777
    assert set(report["estimated_bytes"]) == {"read", "posts", "creators", "post_observations", "item_counter_daily"}
    assert report["classes"] == {"desk": 4, "gossip": 3, "curated": sum(CURATED.values()),
                              "unclassified": 1, "song": 3}


def test_dry_run_after_an_apply_says_nothing_would_be_new(tmp_path, capsys):
    bq = DuckBQ(scenario())
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r") == 0
    capsys.readouterr()
    run_cli(bq)
    report = json.loads(capsys.readouterr().out)
    assert report["would_insert"] == {t: 0 for t in TABLE_TYPES} and sum(report["rows"].values()) > 0


# cost caps

def test_every_query_carries_a_byte_cap_and_every_statement_is_dry_run_before_any_is_run(tmp_path):
    bq = DuckBQ(scenario())
    run_cli(bq, *apply_args(), receipts=tmp_path / "r")
    live = [e for e in bq.log if not e["dry_run"]]
    assert all(isinstance(e["max_billed"], int) and 0 < e["max_billed"] <= bf.WRITE_CAP for e in bq.log)
    assert live
    reads = [e for e in live if RAW_TABLE in e["sql"]]
    assert reads and all(e["max_billed"] == bf.READ_CAP for e in reads)
    inserts = [i for i, e in enumerate(bq.log) if e["sql"].lstrip().upper().startswith("INSERT")]
    first_live = next(i for i in inserts if not bq.log[i]["dry_run"])
    assert all(bq.log[i]["dry_run"] for i in inserts if i < first_live) and first_live > 0
    for e in live:
        assert sum(1 for d in bq.log if d["dry_run"] and d["sql"] == e["sql"]) >= 1


def test_a_dry_run_estimate_over_the_cap_refuses_before_anything_is_billed(tmp_path):
    bq = DuckBQ(scenario(), dry_bytes=bf.WRITE_CAP + 1)
    with pytest.raises(bf.Refused):
        bf.run(bq, since=date(2026, 9, 28), until=date(2026, 10, 7), apply=True, run_id="bf-run-1",
               receipts_dir=tmp_path / "r", fns=(fake_item_id, FakeGeo()))
    assert not any(not e["dry_run"] for e in bq.log)
    assert bq.count("posts") == 0


def test_a_dry_run_that_reports_no_size_is_refused(tmp_path):
    bq = DuckBQ(scenario(), dry_bytes=None)
    with pytest.raises(bf.Refused):
        bf.run(bq, since=date(2026, 9, 28), until=date(2026, 10, 7), apply=False, run_id=None, receipts_dir=None,
               fns=(fake_item_id, FakeGeo()))


def test_the_client_is_set_up_as_the_held_reasons_reader_does(monkeypatch):
    import google.auth
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    seen = {}
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: ("source", None))

    class Creds:
        def __init__(self, **kw):
            seen["creds"] = kw
    monkeypatch.setattr(impersonated_credentials, "Credentials", Creds)
    monkeypatch.setattr(bigquery, "Client", lambda **kw: seen.setdefault("client", kw) and "client")
    assert bf.make_client() == "client"
    assert seen["creds"]["target_principal"] == "f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert seen["creds"]["target_scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]
    assert seen["client"]["project"] == "ogilvy-trends-v2" and seen["client"]["location"] == "US"


# insert only

STATEMENT_WORDS = re.compile(r"\b(UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|REPLACE|EXPORT)\b", re.I)


def test_no_statement_template_updates_or_deletes():
    for table in TABLE_TYPES:
        for sql in (bf.insert_sql(table), bf.count_sql(table)):
            assert not STATEMENT_WORDS.search(sql), sql
    assert not STATEMENT_WORDS.search(bf.READ_SQL)


def test_every_executed_statement_is_a_select_or_an_insert(tmp_path):
    bq = DuckBQ(scenario())
    run_cli(bq, *apply_args(), receipts=tmp_path / "r")
    kinds = {e["sql"].lstrip().split()[0].upper() for e in bq.log}
    assert kinds == {"SELECT", "INSERT"}
    assert not any(STATEMENT_WORDS.search(e["sql"]) for e in bq.log)


@pytest.mark.parametrize("word", ["UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER", "CREATE", "REPLACE", "EXPORT"])
def test_the_guard_refuses_each_changing_word_inside_an_insert(word):
    with pytest.raises(bf.Refused):
        bf.assert_insert_only(f"INSERT INTO t SELECT 1 FROM (SELECT 1) WHERE x IN ({word.lower()} y)")


def test_a_statement_that_is_not_a_select_or_an_insert_is_refused_by_the_guard():
    for sql in ("UPDATE t SET a = 1", "DELETE FROM t WHERE TRUE", "MERGE t USING s ON TRUE",
                "INSERT INTO t SELECT 1; DELETE FROM t WHERE TRUE", "SELECT 1; DROP TABLE t",
                "INSERT INTO t SELECT 1 WHERE EXISTS (SELECT 1) " + "-" * 2 + " update"):
        with pytest.raises(bf.Refused):
            bf.assert_insert_only(sql)
    bf.assert_insert_only(bf.insert_sql("posts"))
    bf.assert_insert_only(bf.READ_SQL)


def test_existing_rows_are_never_changed(tmp_path):
    expected = plan_of(scenario())
    post = writers.dedupe_posts(expected.posts)[0]
    creator = writers.dedupe_creators(expected.creators)[0]
    obs = expected.observations[0]
    counter = expected.counters[0]
    bq = DuckBQ(scenario())
    bq.put("posts", post_id=post["post_id"], platform="old", views=1, post_date=date(2026, 1, 1))
    bq.put("creators", creator_id=creator["creator_id"], platform=creator["platform"], handle="old", followers=1)
    bq.put("post_observations", **{**{k: obs[k] for k in OBSERVATION_COLUMNS}, "views": -1,
                                   "observed_at": datetime.fromisoformat(obs["observed_at"]),
                                   "observed_date": date.fromisoformat(obs["observed_date"])})
    bq.put("item_counter_daily", **{**{k: counter[k] for k in COUNTER_COLUMNS}, "value": -1.0,
                                    "obs_date": date.fromisoformat(counter["obs_date"]),
                                    "observed_at": datetime.fromisoformat(counter["observed_at"]),
                                    "available_at": datetime.fromisoformat(counter["available_at"])})
    before = {t: bq.table(t) for t in TABLE_TYPES}
    run_cli(bq, *apply_args(), receipts=tmp_path / "r")
    for t in TABLE_TYPES:
        for row in before[t]:
            assert row in bq.table(t), (t, row)
    assert bq.count("posts") == len(writers.dedupe_posts(expected.posts))
    assert bq.count("creators") == len(writers.dedupe_creators(expected.creators))
    assert bq.count("post_observations") == len(expected.observations)
    assert bq.count("item_counter_daily") == len(expected.counters)


# idempotence

def test_a_second_apply_writes_nothing_whatever_its_run_id(tmp_path):
    bq = DuckBQ(scenario())
    assert run_cli(bq, *apply_args("bf-run-1"), receipts=tmp_path / "one") == 0
    first = {t: bq.table(t) for t in TABLE_TYPES}
    assert all(first[t] for t in TABLE_TYPES)
    assert run_cli(bq, *apply_args("bf-run-2"), receipts=tmp_path / "two") == 0
    assert {t: bq.table(t) for t in TABLE_TYPES} == first
    statements = [json.loads(line) for line in (tmp_path / "two" / "statements.jsonl").read_text().splitlines()]
    assert statements and all(s["inserted"] == 0 for s in statements)
    assert sum(s["inserted"] for s in
               (json.loads(line) for line in (tmp_path / "one" / "statements.jsonl").read_text().splitlines())) > 0


def test_a_rerun_after_a_partial_apply_fills_only_the_gap(tmp_path):
    bq = DuckBQ(scenario())
    run_cli(bq, *apply_args("bf-run-1"), receipts=tmp_path / "one")
    full = {t: bq.table(t) for t in TABLE_TYPES}
    bq.con.execute("DELETE FROM core.post_observations WHERE post_id IN "
                   "(SELECT post_id FROM core.post_observations ORDER BY post_id LIMIT 3)")
    gap = len(full["post_observations"]) - bq.count("post_observations")
    assert gap > 0
    assert run_cli(bq, *apply_args("bf-run-2"), receipts=tmp_path / "two") == 0
    statements = [json.loads(line) for line in (tmp_path / "two" / "statements.jsonl").read_text().splitlines()]
    assert {s["table"]: s["inserted"] for s in statements if s["inserted"]} == {"post_observations": gap}
    assert [{k: v for k, v in r.items() if k != "run_id"} for r in bq.table("post_observations")] == [
        {k: v for k, v in r.items() if k != "run_id"} for r in full["post_observations"]]
    for t in ("posts", "creators", "item_counter_daily"):
        assert bq.table(t) == full[t]


def test_the_keys_do_not_depend_on_the_run_id():
    one, two = plan_of(scenario(), run_id="bf-run-1"), plan_of(scenario(), run_id="bf-run-2")

    def keys(plan):
        return ({r["post_id"] for r in plan.posts},
                {(r["post_id"], r["observed_at"], r["route"], r["market"], r["protocol"]) for r in plan.observations},
                {(r["obs_date"], r["market"], r["item_id"], r["series"], r["protocol"], r["unit"], r["observed_at"])
                 for r in plan.counters})
    assert keys(one) == keys(two)


# tokens and series

def test_every_prism_row_takes_the_v2_panel_token_and_every_song_row_the_v2_song_token():
    plan = plan_of(scenario())
    prism = [o for o in plan.observations if o["route"] == "prism/profiles"]
    assert prism and all(re.fullmatch(r"panel:[0-9a-f]{12}:v2", o["protocol"]) for o in prism)
    assert plan.counters and {c["protocol"] for c in plan.counters} == {"tiktok/song?proto=v2"}
    assert {o["protocol"] for o in plan.observations} == {o["protocol"] for o in prism}


def test_the_recovered_protocols_are_the_ones_the_live_job_gives_the_same_calls():
    plan = plan_of(scenario())
    live = set()
    for market in ("ZA", "NG", "KE"):
        call, _ = desk_items(market)
        live.add((market, "panel_culture_desk", call.series_protocol()))
        live.add((market, "panel_ig_gossip", gossip_fetch(market).protocol()))
        curated = curated_calls(market)
        assert len(curated) > 1 and len({c.series_protocol() for c in curated}) == 1
        live.add((market, "panel_culture_desk", curated[0].series_protocol()))
    got = {(o["market"], o["series"], o["protocol"]) for o in plan.observations}
    own = bf.panel_protocol([{"platform": "instagram", "handle": "not-in-any-list"}])
    assert got - live == {("NG", "panel_culture_desk", own)}
    assert live <= got


def test_the_ZA_desk_token_is_the_pinned_one():
    plan = plan_of(scenario())
    assert any(o["protocol"] == "panel:9a3c154daf73:v2" and o["market"] == "ZA" for o in plan.observations)


def test_gossip_observations_take_the_gossip_series_and_the_others_the_desk_series():
    plan = plan_of(scenario())
    gossip_posts = {pid for c in plan.calls if c["class"] == "gossip" for pid in c["post_ids"]}
    other_posts = {pid for c in plan.calls if c["class"] != "gossip" for pid in c["post_ids"]}
    assert gossip_posts and other_posts
    assert {o["series"] for o in plan.observations if o["post_id"] in gossip_posts} == {"panel_ig_gossip"}
    assert {o["series"] for o in plan.observations if o["post_id"] in other_posts} == {"panel_culture_desk"}


def test_a_song_read_is_a_total_on_the_day_and_a_delta_from_the_second_day():
    plan = plan_of(scenario())
    totals = {(c["obs_date"], c["item_id"]): c["value"] for c in plan.counters if c["unit"] == "total"}
    deltas = {(c["obs_date"], c["item_id"]): c["value"] for c in plan.counters if c["unit"] == "delta"}
    assert totals == {("2026-09-30", "sound|tiktok:S1"): 100.0, ("2026-10-01", "sound|tiktok:S1"): 130.0,
                      ("2026-10-01", "sound|tiktok:S2"): 50.0}
    assert deltas == {("2026-10-01", "sound|tiktok:S1"): 30.0}
    assert {(c["market"], c["series"], c["platform"], c["lane_class"], c["source"]) for c in plan.counters} == {
        ("GLOBAL", "counter_tiktok_sound", "tiktok", "unbiased_counter", "live")}
    assert {c["pull_seq"] for c in plan.counters if c["unit"] == "delta"} == {None}


def test_tiktok_song_writes_counters_only():
    plan = plan_of([r for r in scenario() if r["route"] == "tiktok/song"])
    assert plan.posts == [] and plan.observations == [] and plan.creators == []
    assert len(plan.counters) == 4


def test_posts_a_song_page_carries_are_counted_and_not_written():
    body = song_body("S1", 100)
    body["data"]["items"] = [{"post": {"id": "v1", "url": "https://example.invalid/v1", "published_at": "2026-09-29T10:00:00Z",
                                       "author": {"username": "someone"}}}]
    plan = plan_of([raw("tiktok/song", "ZA", at(date(2026, 9, 30), 17, 49), body, seed_key="S1", lane="watchlist")])
    assert plan.posts == [] and plan.observations == [] and plan.creators == []
    assert [c["unit"] for c in plan.counters] == ["total"] and plan.calls[0]["dropped"] >= 2


# source_market

def test_source_market_is_the_calls_market_for_the_desk_and_the_curated_panels():
    plan = plan_of(scenario())
    posts_by_class = _posts_by_class(plan)
    for klass in ("desk", "curated"):
        rows = [o for o in plan.observations if o["post_id"] in posts_by_class[klass]]
        assert rows and all(o["source_market"] == o["market"] and o["source_region"] is None for o in rows)
    assert {o["market"] for o in plan.observations if o["post_id"] in posts_by_class["desk"]} == {"ZA", "NG", "KE"}


def _posts_by_class(plan):
    out = {}
    for entry in plan.calls:
        out.setdefault(entry["class"], set()).update(entry["post_ids"])
    return out


def test_source_market_comes_from_the_call_not_from_the_posts_geo():
    call, items = desk_items("NG")
    body = prism_body(items, "geo")
    for result in body["data"]["results"]:
        result["data"]["author"]["location"] = "Johannesburg"
    plan = plan_of([raw("prism/profiles", "NG", at(DAY, 1), body)])
    assert {p["geo_market"] for p in plan.posts} == {"ZA"}
    assert {o["source_market"] for o in plan.observations} == {"NG"}


def test_a_gossip_post_is_scoped_only_when_its_creator_is_on_a_curated_list():
    market = "ZA"
    listed = sorted(curated_creators.listed_handles(MANIFEST, market))
    gossip = gossip_fetch(market).params["items"]
    handles = [e["handle"] for e in gossip]
    assert not any(("instagram", h.casefold()) in listed for h in handles)
    plan = plan_of([raw("prism/profiles", market, at(DAY, 1), prism_body(gossip, "g"))])
    assert plan.observations and {o["source_market"] for o in plan.observations} == {None}
    platform, handle = listed[0]
    body = prism_body(gossip, "g")
    body["data"]["results"][0]["posts"]["items"][0]["post"]["author"]["username"] = handle
    body["data"]["results"][0]["platform"] = platform
    plan = plan_of([raw("prism/profiles", market, at(DAY, 1), body)])
    scoped = [o for o in plan.observations if o["source_market"] == market]
    assert len(scoped) == 1 and len(plan.observations) == 2 * len(gossip)


def test_a_panel_that_matches_no_list_claims_no_source_market():
    plan = plan_of([raw("prism/profiles", "NG", at(DAY, 1),
                        prism_body([{"platform": "instagram", "handle": "not-in-any-list"}], "odd"))])
    assert plan.calls[0]["class"] == "unclassified"
    assert {o["source_market"] for o in plan.observations} == {None}
    assert {o["protocol"] for o in plan.observations} == {
        bf.panel_protocol([{"platform": "instagram", "handle": "not-in-any-list"}])}


def test_a_panel_whose_rows_name_no_target_is_unclassified():
    _, items = desk_items("ZA")
    plan = plan_of([raw("prism/profiles", "ZA", at(DAY, 1), prism_body(items, "nt", target=False))])
    assert plan.calls[0]["class"] == "unclassified"
    assert {o["source_market"] for o in plan.observations} == {None}


# parse and window rules

def test_since_is_the_day_before_the_markets_fetch_day():
    _, items = desk_items("ZA")
    old, new = "2026-10-01T10:00:00Z", "2026-10-02T09:00:00Z"
    body = prism_body(items, "s", posts_each=1, published=old)
    body["data"]["results"][0]["posts"]["items"].append(
        {"post": {"id": "fresh", "url": "https://example.invalid/fresh", "published_at": new,
                  "author": {"username": items[0]["handle"]}}})
    plan = plan_of([raw("prism/profiles", "ZA", at(DAY, 0, 30), body)])
    assert "fresh" in {p["native_id"] for p in plan.posts}
    assert not any(p["native_id"].startswith("s-") for p in plan.posts)


def test_posts_creators_and_observations_line_up_per_call():
    plan = plan_of(scenario())
    ids = {p["post_id"] for p in plan.posts}
    assert {o["post_id"] for o in plan.observations} == ids
    assert {c["creator_id"] for c in plan.creators} >= {p["creator_id"] for p in plan.posts}
    assert {p["run_id"] for p in plan.posts} == {"bf-test"}


def test_a_call_the_parser_refuses_is_skipped_and_counted():
    plan = plan_of([raw("prism/profiles", "GLOBAL", at(DAY, 1), prism_body([{"platform": "instagram", "handle": "h"}], "g"))])
    assert plan.posts == [] and plan.calls[0]["class"] == "skipped" and plan.skipped == 1


def test_a_failed_body_gives_no_rows_but_a_receipt():
    plan = plan_of([raw("prism/profiles", "ZA", at(DAY, 1), {"success": False, "error": "x"})])
    assert plan.posts == [] and plan.calls[0]["posts"] == 0


# receipts

def test_apply_writes_a_receipt_per_stored_call_and_one_per_statement(tmp_path):
    bq = DuckBQ(scenario())
    run_cli(bq, *apply_args(), receipts=tmp_path / "r")
    calls = [json.loads(line) for line in (tmp_path / "r" / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == len(scenario())
    expected = plan_of(scenario(), run_id="bf-run-1")
    assert sum(c["posts"] for c in calls) == len(expected.posts)
    assert sum(c["observations"] for c in calls) == len(expected.observations)
    assert sum(c["counters"] for c in calls) == len([c for c in expected.counters if c["unit"] == "total"])
    assert sum(c["creators"] for c in calls) == len(expected.creators)
    assert {c["run_id"] for c in calls} == {"bf-run-1"}
    assert {"raw_run_id", "route", "market", "fetched_at", "class", "protocol"} <= set(calls[0])
    statements = [json.loads(line) for line in (tmp_path / "r" / "statements.jsonl").read_text().splitlines()]
    assert {s["table"] for s in statements} == set(TABLE_TYPES)
    assert all({"job_id", "bytes_estimated", "bytes_billed", "rows", "inserted"} <= set(s) for s in statements)
    assert {t: sum(s["inserted"] for s in statements if s["table"] == t) for t in TABLE_TYPES} == {
        t: bq.count(t) for t in TABLE_TYPES}
    summary = json.loads((tmp_path / "r" / "summary.json").read_text())
    assert summary["run_id"] == "bf-run-1" and summary["inserted"] == {t: bq.count(t) for t in TABLE_TYPES}


def test_the_receipt_files_hold_counts_and_no_text_handles_or_urls(tmp_path):
    bq = DuckBQ(scenario())
    run_cli(bq, *apply_args(), receipts=tmp_path / "r")
    text = "".join(p.read_text() for p in (tmp_path / "r").iterdir())
    assert "example.invalid" not in text and "caption" not in text and "handle" not in text.replace('"handle"', "")
    assert not re.search(r"\bnot-in-any-list\b", text)


def test_the_call_receipts_are_written_before_the_first_insert(tmp_path):
    bq = DuckBQ(scenario())
    seen = {}
    original = bq.query

    def query(sql, job_config=None, **kw):
        if sql.lstrip().upper().startswith("INSERT") and not job_config.dry_run and "calls" not in seen:
            seen["calls"] = (tmp_path / "r" / "calls.jsonl").exists()
        return original(sql, job_config=job_config, **kw)
    bq.query = query
    run_cli(bq, *apply_args(), receipts=tmp_path / "r")
    assert seen["calls"] is True


def test_a_failed_statement_stops_the_run_and_is_receipted(tmp_path):
    bq = DuckBQ(scenario())
    original = bq.query

    def query(sql, job_config=None, **kw):
        if sql.lstrip().upper().startswith("INSERT") and "post_observations" in sql and not job_config.dry_run:
            raise RuntimeError("boom")
        return original(sql, job_config=job_config, **kw)
    bq.query = query
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r") != 0
    assert bq.count("item_counter_daily") == 0 and bq.count("posts") > 0
    summary = json.loads((tmp_path / "r" / "summary.json").read_text())
    assert summary["status"] == "failed" and "boom" in summary["error"]


# batches and read

def test_rows_go_in_batches_of_the_writers_batch_size(tmp_path, monkeypatch):
    monkeypatch.setattr(bf, "BATCH", 5)
    bq = DuckBQ(scenario())
    run_cli(bq, *apply_args(), receipts=tmp_path / "r")
    inserts = [e for e in bq.log if e["sql"].lstrip().upper().startswith("INSERT") and not e["dry_run"]]
    assert len(inserts) > 4
    assert bq.count("posts") == len(writers.dedupe_posts(plan_of(scenario()).posts))


def test_the_read_is_bounded_to_the_two_routes_the_window_and_http_200():
    assert "r.http_status = 200" in bf.READ_SQL and "DATE(r.fetched_at) BETWEEN @since AND @until" in bf.READ_SQL
    assert "r.route IN UNNEST(@routes)" in bf.READ_SQL and "r.job IN UNNEST(@jobs)" in bf.READ_SQL
    assert bf.ROUTES == ("prism/profiles", "tiktok/song") and bf.WINDOW == (date(2026, 9, 28), date(2026, 10, 7))
    assert bf.JOBS == job.BACKFILL_JOBS


# columns

def _ddl_columns(table):
    text = (Path(__file__).resolve().parents[2] / "schema" / "core.sql").read_text(encoding="utf-8")
    block = re.search(rf"CREATE TABLE IF NOT EXISTS `[^`]*\.{table}` \((.*?)\)\s*(?:PARTITION|CLUSTER|;)", text, re.S).group(1)
    return [m.group(1) for m in re.finditer(r"^\s*(\w+)\s+[A-Z]", block.replace(", ", ",\n"), re.M)]


def test_the_column_maps_match_the_tables_ddl():
    assert list(bf.OBSERVATION_TYPES) == list(OBSERVATION_COLUMNS) and set(bf.OBSERVATION_TYPES) <= set(_ddl_columns("post_observations"))
    assert list(bf.COUNTER_TYPES) == list(COUNTER_COLUMNS) and set(bf.COUNTER_TYPES) <= set(_ddl_columns("item_counter_daily"))
    assert list(writers.POST_TYPES) == list(POST_COLUMNS) and set(writers.POST_TYPES) <= set(_ddl_columns("posts"))
    assert set(writers.CREATOR_TYPES) <= set(_ddl_columns("creators"))
    assert list(writers.CREATOR_TYPES) == list(CREATOR_COLUMNS)
    assert bf.OBSERVATION_TYPES["observed_at"] == "TIMESTAMP" and bf.COUNTER_TYPES["value"] == "FLOAT64"
