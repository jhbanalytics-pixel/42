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
from core.collect.socialcrawl_client import PRICED, params_hash
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


def raw(route, market, fetched, body, *, run_id="r-raw", seed_key=None, lane="panel", job_name="collect", params=None):
    method = PRICED[route].method
    return {"run_id": run_id, "job": job_name, "market": market, "route": route, "lane": lane, "seed_key": seed_key,
            "fetched_at": fetched, "body": json.dumps(body),
            "params_hash": None if params is None else params_hash(method, params)}


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
        rows.append(raw("prism/profiles", market, base, prism_body(items, f"desk{market}"), run_id=f"r-{market}",
                        params=call.params))
        gossip = gossip_fetch(market)
        rows.append(raw("prism/profiles", market, base + timedelta(minutes=5),
                        prism_body(gossip.params["items"], f"gos{market}"), run_id=f"r-{market}",
                        params=gossip.params))
        for n, c in enumerate(curated_calls(market)):
            rows.append(raw("prism/profiles", market, base + timedelta(minutes=10 + n),
                            prism_body(c.params["items"], f"cur{market}{n}"), run_id=f"r-{market}", params=c.params))
    # The same desk read again the next night: the same posts, a new observation each.
    rows.append(raw("prism/profiles", "ZA", at(DAY + timedelta(days=1), 0, 30), prism_body(desk_items("ZA")[1], "deskZA"),
                    run_id="r-ZA-next", params={**desk_items("ZA")[0].params, "since": "2026-10-03"}))
    odd = [{"platform": "instagram", "handle": "not-in-any-list"}]
    rows.append(raw("prism/profiles", "NG", at(DAY, 0, 50), prism_body(odd, "odd"), run_id="r-NG"))
    rows.append(raw("tiktok/song", "ZA", at(date(2026, 9, 30), 17, 49), song_body("S1", 100), seed_key="S1",
                    lane="watchlist", run_id="r-s1", params={"clipId": "S1"}))
    rows.append(raw("tiktok/song", "NG", at(date(2026, 10, 1), 17, 49), song_body("S1", 130), seed_key="S1",
                    lane="watchlist", run_id="r-s2", params={"clipId": "S1"}))
    rows.append(raw("tiktok/song", "KE", at(date(2026, 10, 1), 17, 50), song_body("S2", 50), seed_key="S2",
                    lane="watchlist", run_id="r-s3", params={"clipId": "S2"}))
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


SAST = timezone(timedelta(hours=2))


def sast(hour, minute=0, second=0):
    return lambda: datetime(2026, 10, 10, hour, minute, second, tzinfo=SAST)


NOON = sast(12)


def run_cli(bq, *args, receipts=None, clock=NOON):
    argv = list(args)
    if receipts is not None:
        argv += ["--receipts-dir", str(receipts)]
    return bf.main(argv, client=bq, fns=(fake_item_id, FakeGeo()), clock=clock)


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
               receipts_dir=tmp_path / "r", fns=(fake_item_id, FakeGeo()), clock=NOON)
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
               receipts_dir=tmp_path / "r", fns=(fake_item_id, FakeGeo()), clock=NOON)


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


# the safe time

@pytest.mark.parametrize("clock", [sast(2), sast(4, 15), sast(6, 30), sast(2, 0, 1), sast(6, 29, 59),
                                   lambda: datetime(2026, 10, 10, 0, 30, tzinfo=timezone.utc),
                                   lambda: datetime(2026, 10, 10, 4, 30, tzinfo=timezone.utc)])
def test_apply_inside_the_collect_window_is_refused_before_anything_runs(clock, tmp_path):
    bq = DuckBQ(scenario())
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r", clock=clock) != 0
    assert bq.log == [] and not (tmp_path / "r").exists()


@pytest.mark.parametrize("clock", [sast(1, 59, 59), sast(6, 30, 1), sast(12), sast(0), sast(23, 59, 59),
                                   lambda: datetime(2026, 10, 9, 23, 59, 59, tzinfo=timezone.utc),
                                   lambda: datetime(2026, 10, 10, 4, 30, 1, tzinfo=timezone.utc)])
def test_apply_outside_the_collect_window_goes_ahead(clock, tmp_path):
    bq = DuckBQ(scenario())
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r", clock=clock) == 0
    assert bq.count("posts") > 0


def _closing_clock(bq, *, after_dry_inserts=None, after_real_inserts=None):
    """Noon until the given number of INSERT statements has been seen, then 03:00 SAST."""
    def clock():
        dry = sum(e["sql"].lstrip().upper().startswith("INSERT") and e["dry_run"] for e in bq.log)
        real = sum(e["sql"].lstrip().upper().startswith("INSERT") and not e["dry_run"] for e in bq.log)
        closed = (after_dry_inserts is not None and dry > after_dry_inserts) or                  (after_real_inserts is not None and real >= after_real_inserts)
        return datetime(2026, 10, 10, 3, 0, tzinfo=SAST) if closed else datetime(2026, 10, 10, 12, 0, tzinfo=SAST)
    return clock


def test_the_window_closing_during_the_apply_stops_it_before_the_next_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(bf, "BATCH", 5)
    bq = DuckBQ(scenario())
    clock = _closing_clock(bq, after_real_inserts=1)
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r", clock=clock) == 2
    real = [e for e in bq.log if e["sql"].lstrip().upper().startswith("INSERT") and not e["dry_run"]]
    assert len(real) == 1
    statements = [json.loads(line) for line in (tmp_path / "r" / "statements.jsonl").read_text().splitlines()]
    assert len(statements) == 1
    summary = json.loads((tmp_path / "r" / "summary.json").read_text())
    assert summary["status"] == "stopped" and "02:00 to 06:30 SAST" in summary["error"]
    assert sum(summary["inserted"].values()) == statements[0]["inserted"]


def test_the_window_closing_before_the_first_insert_writes_nothing(tmp_path):
    bq = DuckBQ(scenario())
    clock = _closing_clock(bq, after_dry_inserts=0)
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r", clock=clock) == 2
    assert not any(e["sql"].lstrip().upper().startswith("INSERT") and not e["dry_run"] for e in bq.log)
    assert all(bq.count(t) == 0 for t in TABLE_TYPES)
    summary = json.loads((tmp_path / "r" / "summary.json").read_text())
    assert summary["status"] == "stopped" and sum(summary["inserted"].values()) == 0


def test_a_stopped_apply_is_finished_by_a_rerun_outside_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(bf, "BATCH", 5)
    bq = DuckBQ(scenario())
    run_cli(bq, *apply_args("bf-run-1"), receipts=tmp_path / "one", clock=_closing_clock(bq, after_real_inserts=2))
    assert 0 < sum(bq.count(t) for t in TABLE_TYPES)
    assert run_cli(bq, *apply_args("bf-run-2"), receipts=tmp_path / "two") == 0
    clean = DuckBQ(scenario())
    run_cli(clean, *apply_args("bf-run-3"), receipts=tmp_path / "three")
    assert {t: bq.count(t) for t in TABLE_TYPES} == {t: clean.count(t) for t in TABLE_TYPES}


def test_a_dry_run_inside_the_collect_window_is_allowed():
    bq = DuckBQ(scenario())
    assert run_cli(bq, clock=sast(4)) == 0
    assert all(bq.count(t) == 0 for t in TABLE_TYPES)


def test_the_clock_is_the_real_one_when_none_is_given(monkeypatch, tmp_path):
    bq = DuckBQ(scenario())
    inside = datetime(2026, 10, 10, 3, 0, tzinfo=SAST)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return inside.astimezone(tz) if tz else inside.replace(tzinfo=None)
    monkeypatch.setattr(bf, "datetime", Frozen)
    assert bf.main(["--apply", "--run-id", "bf-run-1", "--receipts-dir", str(tmp_path / "r")], client=bq,
                   fns=(fake_item_id, FakeGeo())) != 0
    assert bq.log == []


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
               receipts_dir=tmp_path / "r", fns=(fake_item_id, FakeGeo()), clock=NOON)
    assert not any(not e["dry_run"] for e in bq.log)
    assert bq.count("posts") == 0


def test_a_dry_run_that_reports_no_size_is_refused(tmp_path):
    bq = DuckBQ(scenario(), dry_bytes=None)
    with pytest.raises(bf.Refused):
        bf.run(bq, since=date(2026, 9, 28), until=date(2026, 10, 7), apply=False, run_id=None, receipts_dir=None,
               fns=(fake_item_id, FakeGeo()))


def test_the_two_byte_caps_are_pinned_to_their_literal_values():
    """Every other cap test compares against the constants themselves, so only a literal can notice one change."""
    assert bf.READ_CAP == 536_870_912 == 512 * 1024 ** 2
    assert bf.WRITE_CAP == 1_073_741_824 == 1024 ** 3


def test_the_client_is_set_up_as_the_held_reasons_reader_does(monkeypatch):
    import google.auth
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    seen = {}
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: ("source", None))

    class Creds:
        def __init__(self, **kw):
            seen["creds"] = kw
            seen["instance"] = self
    monkeypatch.setattr(impersonated_credentials, "Credentials", Creds)
    monkeypatch.setattr(bigquery, "Client", lambda **kw: seen.setdefault("client", kw) and "client")
    assert bf.make_client() == "client"
    assert seen["creds"]["source_credentials"] == "source"
    assert seen["client"]["credentials"] is seen["instance"]
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
    bf.assert_insert_only(bf.READ_SQL.format(table=writers.table("raw_responses")))


BAD_STATEMENTS = ["INSERT INTO t SELECT 1; SELECT 2", "CALL p()", "SELECT 1; SELECT 2", "SELECT 1", "INSERT INTO t VALUES (1)"]
WINDOW_ARGS = dict(since=date(2026, 9, 28), until=date(2026, 10, 7), receipts_dir=None, fns=(fake_item_id, FakeGeo()),
                   clock=NOON)


@pytest.mark.parametrize("sql", BAD_STATEMENTS)
def test_the_guard_refuses_a_statement_that_is_not_one_of_the_modules_own(sql):
    with pytest.raises(bf.Refused):
        bf.assert_insert_only(sql)


@pytest.mark.parametrize("sql", BAD_STATEMENTS)
@pytest.mark.parametrize("path", ["_dry", "_run"])
def test_the_dry_run_and_the_acting_run_each_call_the_guard(path, sql):
    bq = DuckBQ()
    with pytest.raises(bf.Refused):
        getattr(bf, path)(bq, sql, [], bf.WRITE_CAP)
    assert bq.log == []


@pytest.mark.parametrize("sql", BAD_STATEMENTS[:2])
@pytest.mark.parametrize("apply", [True, False])
def test_a_planner_statement_the_guard_refuses_never_reaches_the_client(monkeypatch, tmp_path, sql, apply):
    bq = DuckBQ(scenario())
    monkeypatch.setattr(bf, "insert_sql", lambda table: sql)
    with pytest.raises(bf.Refused):
        bf.run(bq, apply=apply, run_id="bf-run-1" if apply else None, **{**WINDOW_ARGS, "receipts_dir": (
            tmp_path / "r" if apply else None)})
    assert all(e["sql"] != sql for e in bq.log)
    assert all(bq.count(t) == 0 for t in TABLE_TYPES)


@pytest.mark.parametrize("sql", BAD_STATEMENTS[:2])
def test_a_count_statement_the_guard_refuses_never_reaches_the_client(monkeypatch, sql):
    bq = DuckBQ(scenario())
    monkeypatch.setattr(bf, "count_sql", lambda table: sql)
    with pytest.raises(bf.Refused):
        bf.run(bq, apply=False, run_id=None, **WINDOW_ARGS)
    assert all(e["sql"] != sql for e in bq.log)


@pytest.mark.parametrize("sql", BAD_STATEMENTS[:3])
def test_a_read_statement_the_guard_refuses_never_reaches_the_client(monkeypatch, sql):
    bq = DuckBQ(scenario())
    monkeypatch.setattr(bf, "READ_SQL", sql)
    with pytest.raises(bf.Refused):
        bf.run(bq, apply=False, run_id=None, **WINDOW_ARGS)
    assert bq.log == []


def test_the_guard_passes_exactly_the_texts_the_module_builds():
    bf.assert_insert_only(bf.READ_SQL.format(table=writers.table("raw_responses")))
    for table in bf.SPECS:
        bf.assert_insert_only(bf.insert_sql(table))
        bf.assert_insert_only(bf.count_sql(table))


def _variants():
    posts = bf.insert_sql("posts")
    yield "no NOT EXISTS", posts[:posts.index("WHERE NOT EXISTS")]
    yield "into collection_health", posts.replace(writers.table("posts"), writers.table("collection_health"))
    yield "into raw_responses", posts.replace(writers.table("posts"), writers.table("raw_responses"))
    yield "values", f"INSERT INTO `{writers.table('posts')}` (post_id) VALUES ('x')"
    yield "trailing semicolon", posts + ";"
    yield "extra space", posts + " "
    yield "another table's read", bf.READ_SQL.format(table=writers.table("posts"))
    yield "count over a made-up table", bf.count_sql("posts").replace(writers.table("posts"), "p.d.other")


@pytest.mark.parametrize("name,sql", list(_variants()), ids=[n for n, _ in _variants()])
def test_the_guard_refuses_an_insert_or_select_that_is_not_one_of_the_modules_own_texts(name, sql):
    with pytest.raises(bf.Refused):
        bf.assert_insert_only(sql)


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


# a panel claims a list only when it is that list

def _unlisted_member(market, key):
    """One member of the market's desk or gossip list that is on no curated list, as a panel item."""
    refs = bf._references(MANIFEST, CONFIG, bf.WINDOW[1])[market]
    items = refs["desk_items"] if key == "desk" else refs["gossip_items"]
    return next((i for i in items if (i["platform"].casefold(), i["handle"].casefold()) not in refs["curated"]), None)


def _curated_member(market):
    return curated_calls(market)[0].params["items"][0]


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_a_panel_that_only_overlaps_the_curated_list_is_not_curated(market):
    panel = [_curated_member(market), {"platform": "instagram", "handle": "not-in-any-list"}]
    plan = plan_of([raw("prism/profiles", market, at(DAY, 1), prism_body(panel, "ov"))])
    assert plan.calls[0]["class"] == "unclassified" and plan.observations
    assert {o["source_market"] for o in plan.observations} == {None}


@pytest.mark.parametrize("key,klass", [("desk", "desk"), ("gossip", "gossip")])
def test_part_of_the_desk_or_gossip_list_is_not_that_list(key, klass):
    tried = 0
    for market in ("ZA", "NG", "KE"):
        member = _unlisted_member(market, key)
        if member is None:
            continue
        tried += 1
        plan = plan_of([raw("prism/profiles", market, at(DAY, 1), prism_body([member], "sub"))])
        assert plan.calls[0]["class"] == "unclassified" and plan.observations
        assert {o["source_market"] for o in plan.observations} == {None}
        assert {o["series"] for o in plan.observations} == {"panel_culture_desk"}
    assert tried


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


# the stored params_hash is the independent check on what the module reconstructs

def _verified(plan, klass):
    return [c["params_verified"] for c in plan.calls if c["class"] == klass]


def test_the_stored_params_hash_confirms_the_desk_gossip_and_song_calls_as_reconstructed():
    plan = plan_of(scenario())
    assert _verified(plan, "desk") == [True] * 4
    assert _verified(plan, "gossip") == [True] * 3
    assert _verified(plan, "song") == [True] * 3
    assert _verified(plan, "unclassified") == [None]


def test_a_curated_batch_is_confirmed_when_the_day_read_the_whole_list():
    market = "ZA"
    calls = curated_calls(market, limit=10 ** 6)
    assert len(calls) > 1
    rows = [raw("prism/profiles", market, at(DAY, 0, 40 + n), prism_body(c.params["items"], f"c{n}"), run_id="r-c",
                params=c.params) for n, c in enumerate(calls)]
    plan = plan_of(rows)
    assert _verified(plan, "curated") == [True] * len(calls)


def test_a_stored_hash_that_the_reconstruction_does_not_reproduce_is_reported_false():
    call, items = desk_items("ZA")
    wrong_day = {**call.params, "since": "2026-10-01"}
    plan = plan_of([raw("prism/profiles", "ZA", at(DAY, 0, 30), prism_body(items, "d"), params=wrong_day),
                    raw("tiktok/song", "ZA", at(date(2026, 9, 30), 17, 49), song_body("S1", 100), seed_key="S1",
                        lane="watchlist", params={"clipId": "OTHER"})])
    assert [c["params_verified"] for c in plan.calls] == [False, False]


def test_a_row_without_a_stored_hash_is_unchecked_not_confirmed():
    call, items = desk_items("ZA")
    plan = plan_of([raw("prism/profiles", "ZA", at(DAY, 0, 30), prism_body(items, "d"))])
    assert _verified(plan, "desk") == [None]


def test_the_report_counts_confirmed_unconfirmed_and_unchecked_calls_per_class(capsys):
    """The totals of the first version could not say which class an unconfirmed call belonged to, so the runbook's
    stop rule (an unconfirmed desk, gossip or song call) could not be read from the report."""
    bq = DuckBQ(scenario())
    run_cli(bq)
    report = json.loads(capsys.readouterr().out)
    got = report["params_verified"]
    assert got["desk"] == {"confirmed": 4, "unconfirmed": 0, "unchecked": 0}
    assert got["gossip"] == {"confirmed": 3, "unconfirmed": 0, "unchecked": 0}
    assert got["song"] == {"confirmed": 3, "unconfirmed": 0, "unchecked": 0}
    assert got["unclassified"] == {"confirmed": 0, "unconfirmed": 0, "unchecked": 1}
    # A curated batch is confirmed only where the day's rotation starts at the head of the list, so it may split.
    curated = got["curated"]
    assert curated["unchecked"] == 0 and curated["confirmed"] + curated["unconfirmed"] == sum(CURATED.values())
    assert set(got) == {"desk", "gossip", "song", "unclassified", "curated"}


def _hash_wrong(rows, index):
    rows = [dict(r) for r in rows]
    rows[index]["params_hash"] = params_hash(PRICED[rows[index]["route"]].method, {"not": "the params"})
    return rows


def _index_of(rows, klass):
    plan = plan_of(rows)
    ordered = sorted(range(len(rows)), key=lambda i: (rows[i]["fetched_at"], rows[i]["run_id"], rows[i]["route"],
                                                      str(rows[i]["market"]), str(rows[i].get("seed_key"))))
    return next(ordered[n] for n, call in enumerate(plan.calls) if call["class"] == klass)


@pytest.mark.parametrize("klass", ["desk", "gossip", "song"])
def test_the_report_names_the_class_of_an_unconfirmed_call(klass, capsys):
    rows = _hash_wrong(scenario(), _index_of(scenario(), klass))
    run_cli(DuckBQ(rows))
    got = json.loads(capsys.readouterr().out)["params_verified"]
    assert got[klass]["unconfirmed"] == 1
    assert [k for k in ("desk", "gossip", "song") if got[k]["unconfirmed"]] == [klass]


@pytest.mark.parametrize("klass", ["desk", "gossip", "song"])
def test_apply_refuses_while_a_desk_gossip_or_song_call_is_unconfirmed(klass, tmp_path):
    rows = _hash_wrong(scenario(), _index_of(scenario(), klass))
    bq = DuckBQ(rows)
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r") != 0
    assert not any(e["sql"].lstrip().upper().startswith("INSERT") for e in bq.log)
    assert all(bq.count(t) == 0 for t in TABLE_TYPES) and not (tmp_path / "r").exists()


@pytest.mark.parametrize("klass", ["desk", "gossip", "song"])
def test_apply_goes_ahead_on_an_unconfirmed_call_when_it_is_accepted_by_name(klass, tmp_path):
    rows = _hash_wrong(scenario(), _index_of(scenario(), klass))
    bq = DuckBQ(rows)
    assert run_cli(bq, *apply_args(), "--accept-unconfirmed", receipts=tmp_path / "r") == 0
    assert all(bq.count(t) > 0 for t in TABLE_TYPES)
    assert json.loads((tmp_path / "r" / "summary.json").read_text())["accept_unconfirmed"] is True


def _hash_removed(rows, index):
    rows = [dict(r) for r in rows]
    rows[index]["params_hash"] = None
    return rows


@pytest.mark.parametrize("klass", ["desk", "gossip", "song"])
def test_apply_refuses_while_a_desk_gossip_or_song_call_is_unchecked(klass, tmp_path):
    """A call stored without a params_hash cannot be compared with anything, so it is no better than a mismatch for
    the three classes the stop rule covers: it must not slip through without --accept-unconfirmed."""
    rows = _hash_removed(scenario(), _index_of(scenario(), klass))
    bq = DuckBQ(rows)
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r") == 2
    assert not any(e["sql"].lstrip().upper().startswith("INSERT") for e in bq.log)
    assert all(bq.count(t) == 0 for t in TABLE_TYPES) and not (tmp_path / "r").exists()


@pytest.mark.parametrize("klass", ["desk", "gossip", "song"])
def test_apply_goes_ahead_on_an_unchecked_call_when_it_is_accepted_by_name(klass, tmp_path):
    rows = _hash_removed(scenario(), _index_of(scenario(), klass))
    bq = DuckBQ(rows)
    assert run_cli(bq, *apply_args(), "--accept-unconfirmed", receipts=tmp_path / "r") == 0
    assert all(bq.count(t) > 0 for t in TABLE_TYPES)


def test_an_unchecked_call_is_still_reported_as_unchecked_and_not_as_unconfirmed(capsys):
    rows = _hash_removed(scenario(), _index_of(scenario(), "desk"))
    assert run_cli(DuckBQ(rows)) == 0
    desk = json.loads(capsys.readouterr().out)["params_verified"]["desk"]
    assert desk == {"confirmed": 3, "unconfirmed": 0, "unchecked": 1}


def test_a_dry_run_reports_an_unconfirmed_call_and_does_not_refuse(capsys):
    rows = _hash_wrong(scenario(), _index_of(scenario(), "desk"))
    assert run_cli(DuckBQ(rows)) == 0
    assert json.loads(capsys.readouterr().out)["params_verified"]["desk"]["unconfirmed"] == 1


def test_an_unconfirmed_curated_batch_or_an_unchecked_panel_does_not_stop_the_apply(tmp_path):
    rows = _hash_wrong(scenario(), _index_of(scenario(), "curated"))
    bq = DuckBQ(rows)
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r") == 0
    assert all(bq.count(t) > 0 for t in TABLE_TYPES)


# keys and repeats inside one batch

def test_creators_are_keyed_by_platform_as_well_as_creator_id(tmp_path):
    creator = writers.dedupe_creators(plan_of(scenario()).creators)[0]
    bq = DuckBQ(scenario())
    bq.put("creators", creator_id=creator["creator_id"], platform="another-platform", handle="old", followers=1)
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r") == 0
    mine = [r for r in bq.table("creators") if r["creator_id"] == creator["creator_id"]]
    assert {r["platform"] for r in mine} == {"another-platform", creator["platform"]}
    assert bq.count("creators") == len(writers.dedupe_creators(plan_of(scenario()).creators)) + 1


@pytest.mark.parametrize("table", ["post_observations", "item_counter_daily"])
def test_a_stored_row_under_another_protocol_does_not_stop_the_same_row_under_this_one(table, tmp_path):
    """The protocol is part of the NOT EXISTS key: a row that differs from a stored one only in its protocol is a
    different row, as the live writers key it, and must land."""
    bq = DuckBQ(scenario())
    assert run_cli(bq, *apply_args("bf-run-1"), receipts=tmp_path / "one") == 0
    first = bq.count(table)
    assert first > 0
    bq.con.execute(f"UPDATE core.{table} SET protocol = 'legacy:v1'")
    assert run_cli(bq, *apply_args("bf-run-2"), receipts=tmp_path / "two") == 0
    assert bq.count(table) == 2 * first
    assert {r["protocol"] for r in bq.table(table)} >= {"legacy:v1"} and len({r["protocol"] for r in bq.table(table)}) > 1


@pytest.mark.parametrize("table", ["post_observations", "item_counter_daily"])
def test_the_not_exists_key_of_the_observations_and_counters_names_the_protocol(table):
    assert "protocol" in bf.SPECS[table][1]
    assert "T.protocol = S.protocol" in bf.insert_sql(table) and "T.protocol = S.protocol" in bf.count_sql(table)


def test_a_post_listed_under_two_profiles_of_one_body_gives_one_observation():
    """parse._post does not dedupe, so a collab post under two profiles yields two rows with the same key."""
    _, items = desk_items("ZA")
    body = prism_body(items[:2], "collab")
    shared = body["data"]["results"][0]["posts"]["items"][0]
    body["data"]["results"][1]["posts"]["items"].append(json.loads(json.dumps(shared)))
    plan = plan_of([raw("prism/profiles", "ZA", at(DAY, 1), body)])
    keys = [(o["post_id"], o["observed_at"], o["route"], o["market"], o["protocol"]) for o in plan.observations]
    assert len(keys) == len(set(keys))
    shared_id = next(p["post_id"] for p in plan.posts if p["native_id"] == shared["post"]["id"])
    assert sum(o["post_id"] == shared_id for o in plan.observations) == 1


def test_the_same_song_read_twice_gives_one_counter_row():
    row = raw("tiktok/song", "ZA", at(date(2026, 9, 30), 17, 49), song_body("S1", 100), seed_key="S1", lane="watchlist")
    plan = plan_of([row, dict(row, run_id="r-again")])
    assert [c["unit"] for c in plan.counters] == ["total"]


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


# evidence only (Albert, batch 6, 9 Oct 2026): posts and creators land, no series and no closed day is rewritten

WRITTEN = {"posts", "creators", "post_observations", "item_counter_daily"}
NOT_TOUCHED = ("collection_health", "item_daily", "post_items", "series", "cultural_map", "runs")


def _tables_named(sql):
    return {name.rsplit(".", 1)[-1] for name in re.findall(r"`([^`]+)`", sql)}


def test_no_statement_the_apply_runs_touches_a_health_item_or_series_table(tmp_path):
    bq = DuckBQ(scenario())
    assert run_cli(bq, *apply_args(), receipts=tmp_path / "r") == 0
    seen = set()
    for entry in bq.log:
        seen |= _tables_named(entry["sql"])
    assert seen == WRITTEN | {"raw_responses"}
    for name in seen:
        assert not any(word in name for word in NOT_TOUCHED), name
    for entry in bq.log:
        for text in ("collection_health", "item_daily", "post_items"):
            assert text not in entry["sql"], (text, entry["sql"][:80])


def test_every_statement_the_guard_allows_names_only_the_four_tables_and_the_raw_read():
    allowed = set(bf.ALLOWED)
    assert len(allowed) == 9
    for sql in allowed:
        names = _tables_named(sql)
        assert names and names <= WRITTEN | {"raw_responses"}, sql[:80]
        if sql.lstrip().upper().startswith("INSERT"):
            assert _tables_named(sql.split("\n", 1)[0]) <= WRITTEN
    assert {n for sql in allowed if sql.lstrip().upper().startswith("INSERT") for n in _tables_named(sql)} == WRITTEN


def test_the_tables_the_command_writes_are_the_four_and_no_other():
    assert set(bf.SPECS) == WRITTEN


# RC3-3: the backfill writes days on which the live jobs stored kept creators under panel_culture_desk, so its curated class stays under that
# series and never takes the curated panel's own series, which would give the new series a history that stops on 7 Oct and a gap before the split.

def test_rc3_3_the_curated_class_stays_under_the_series_the_history_of_those_days_uses_and_not_the_curated_panel_series():
    from core.collect import parse

    plan = plan_of(scenario())
    curated_posts = {pid for c in plan.calls if c["class"] == "curated" for pid in c["post_ids"]}
    assert curated_posts
    series = {o["series"] for o in plan.observations if o["post_id"] in curated_posts}
    assert series == {"panel_culture_desk"}
    assert parse.CURATED_PANEL_SERIES == "panel_curated_creators" and parse.CURATED_PANEL_SERIES not in {o["series"] for o in plan.observations}


def test_rc3_3_no_backfill_code_passes_curated_true_to_a_call():
    import ast
    from pathlib import Path

    source = Path(bf.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "curated":
            raise AssertionError("backfill_prism passes a curated keyword; RC3-3 says the backfill does not")
        if isinstance(node, ast.Attribute) and node.attr == "curated" and isinstance(node.ctx, ast.Store):
            raise AssertionError("backfill_prism sets a curated attribute; RC3-3 says the backfill does not")
