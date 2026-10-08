"""ops/runners/check_pages.py against a local f42-api on fixtures, with a fake passcode set in this process only."""

import datetime as dt
import importlib.util
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_pages", ROOT / "ops" / "runners" / "check_pages.py")
check_pages = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_pages
SPEC.loader.exec_module(check_pages)

FAKE_PASS = "fake-passcode-for-this-test-only"
BASE = "http://localhost:8765"
FIXTURE_DAY = dt.datetime(2026, 9, 30, 9, 0, tzinfo=check_pages.SAST)


@pytest.fixture
def local_api(monkeypatch, tmp_path):
    """The real f42-api app on core/api/fixtures, every request it receives recorded."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("UI_PASSCODE", FAKE_PASS)
    monkeypatch.setenv("F42_DATA", "fixtures")
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.setenv("WEB_DIST", str(tmp_path / "no-build"))
    monkeypatch.setenv("F42_SMOKE_PASSCODE", FAKE_PASS)
    monkeypatch.delenv("AGENT_URL", raising=False)
    from core.api import app as api_mod

    seen = []
    client = TestClient(api_mod.app, raise_server_exceptions=False)
    client.event_hooks = {"request": [lambda request: seen.append(request)], "response": []}
    yield client, seen
    client.close()


def run(client, argv=(), now=FIXTURE_DAY, bq=None):
    lines = []
    code = check_pages.main([BASE, *argv], client=client, now=now, bq=bq, out=lines.append)
    return code, lines


def test_every_read_is_a_get_and_the_passcode_goes_only_in_its_header(local_api, capsys):
    client, seen = local_api
    code, lines = run(client)
    text = "\n".join(lines) + capsys.readouterr().out
    assert seen, "the checker read nothing"
    assert {r.method for r in seen} == {"GET"}
    assert all(str(r.url).startswith(BASE + "/") for r in seen)
    assert FAKE_PASS not in text
    for r in seen:
        assert FAKE_PASS not in str(r.url)
        sent = r.headers.get("X-Passcode")
        assert sent in (None, FAKE_PASS)
    # Nothing that writes or spends: no ask, spike, investigation start, watch, schedule or dossier write.
    assert not any(r.url.path in ("/api/ask", "/api/spikes", "/api/feedback") for r in seen)
    assert code == 1  # fixture data is reported as fake, so the run does not pass


def test_fixture_payloads_are_flagged_fake_and_moved_pages_named_by_their_42_page(local_api):
    client, seen = local_api
    _code, lines = run(client)
    today = next(line for line in lines if line.startswith("Today") and "/api/today" in line)
    assert " 200 " in today and "FAKE" in today and "2026-09-30 (0d)" in today
    assert any(line.startswith("Gate") and " 401 " in line and " OK " in line for line in lines)
    # 3 October 2026: the desk pages' links open 42 pages (legacyRoutes.js), so their desk API is not probed and
    # nothing reads as "not served"; each is named with how the 42 page it lands on read.
    desk = ("/api/desk", "/api/intel/mentions", "/api/voices", "/api/research/recent", "/api/v2/source-lab")
    assert not any(r.url.path.startswith(desk) for r in seen)
    assert not any("not served" in line or line.startswith("More (legacy)") for line in lines)
    summary = lines[lines.index("Summary"):]
    moved = next(line for line in summary if line.startswith("  moved to 42 pages"))
    assert "Browse to Discover fake" in moved and "Listen to Seed path " in moved
    assert "Source Lab to Fieldwork " in moved and "Build console to Investigations/Dossiers/Schedules/History" in moved
    assert any(line.startswith("  fake") and "Today" in line and "Discover" in line for line in summary)
    assert len(lines) < 60


def test_a_brief_three_days_old_reads_as_stale(local_api):
    client, _seen = local_api
    _code, lines = run(client, now=FIXTURE_DAY + dt.timedelta(days=3))
    coverage = next(line for line in lines if line.startswith("Coverage"))
    fieldwork = next(line for line in lines if line.startswith("Fieldwork"))
    assert "2026-09-30 (3d)" in coverage
    assert "STALE" in fieldwork


def test_no_passcode_exits_2_and_reads_nothing(monkeypatch, capsys):
    monkeypatch.delenv("F42_SMOKE_PASSCODE", raising=False)

    class Refuse:
        def request(self, *args, **kwargs):
            raise AssertionError("no request may be made without a passcode")

    assert check_pages.main([BASE], client=Refuse(), out=lambda _line: None) == 2
    assert "F42_SMOKE_PASSCODE is not set" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://example.com", "https://x.run.app/api", "https://u:p@x.run.app",
                                 "ftp://x.run.app", "https://x.run.app?a=1"])
def test_base_urls_other_than_an_origin_are_refused(url, monkeypatch, capsys):
    monkeypatch.setenv("F42_SMOKE_PASSCODE", FAKE_PASS)
    assert check_pages.main([url], client=object(), out=lambda _line: None) == 2
    assert FAKE_PASS not in capsys.readouterr().err


def test_the_reader_has_no_way_to_send_anything_but_get():
    reader = check_pages.Reader(BASE, FAKE_PASS, client=object())
    assert not any(hasattr(reader, name) for name in ("post", "put", "delete", "patch"))
    with pytest.raises(check_pages.Refused):
        reader.get("//evil.example/api/today")
    with pytest.raises(check_pages.Refused):
        reader.get("https://evil.example/api/today")


class FakeBQ:
    def __init__(self):
        self.sql = []

    def rows(self, sql, **params):
        check_pages.select_only(sql)
        self.sql.append(sql)
        if "GROUP BY stage" in sql:
            return [{"stage": "collect", "last_ok": dt.date(2026, 9, 30), "last_any": dt.date(2026, 9, 30),
                     "last_status": "ok"},
                    {"stage": "coaction", "last_ok": None, "last_any": dt.date(2026, 9, 30),
                     "last_status": "skipped"}]
        if "INFORMATION_SCHEMA" in sql:
            names = [o for o in check_pages.API_OBJECTS if o not in ("v_sensitive_items_complete", "v_item_spread")]
            return [{"n": o} for o in names if ("intelligence_42_core" in sql) == (o != "v_briefs_current")]
        if "test != 'none'" in sql:
            return [{"tested": 0, "total": 120}]
        if "COUNT(*) AS total" in sql:
            return [{"latest": None, "total": 0}]
        if "item_hourly" in sql or "breaking_signals" in sql:
            return [{"latest": None, "last3": 0}]
        return [{"latest": dt.date(2026, 9, 30), "last3": 1234}]


def test_bq_section_runs_select_only_and_keeps_the_output_short(local_api):
    client, _seen = local_api
    fake = FakeBQ()
    _code, lines = run(client, argv=("--bq",), bq=fake)
    assert fake.sql and all(s.lstrip().upper().startswith(("SELECT", "WITH")) for s in fake.sql)
    assert all("`ogilvy-trends-v2." in s for s in fake.sql)
    text = "\n".join(lines)
    assert "BigQuery freshness" in text and "series_test tested     0 of 120" in text
    assert "coaction no ok! (last skipped)" in text and "understand never!" in text and "learn never;" in text
    assert "item_hourly" in text and " off " in text  # switched-off tables are not called stale
    assert "API views missing (their page parts stay empty): v_item_spread, v_sensitive_items_complete" in text
    assert len(lines) < 70


def test_reconcile_two_days_back_is_current_but_three_days_back_is_marked():
    """Reconcile runs at 23:30 SAST for yesterday, so through the day its newest ok run_date is two days back."""
    today = dt.date(2026, 10, 3)

    class StagesBQ(FakeBQ):
        def __init__(self, reconcile_ok, collect_ok):
            super().__init__()
            self.stages = [{"stage": "reconcile", "last_ok": reconcile_ok, "last_any": reconcile_ok,
                            "last_status": "ok"},
                           {"stage": "collect", "last_ok": collect_ok, "last_any": collect_ok, "last_status": "ok"}]

        def rows(self, sql, **params):
            return self.stages if "GROUP BY stage" in sql else super().rows(sql, **params)

    def stage_text(bq):
        lines = []
        check_pages.run_bq(bq, today, out=lines.append)
        return "\n".join(lines[lines.index(next(line for line in lines if line.startswith("Latest run per stage"))):])

    current = stage_text(StagesBQ(dt.date(2026, 10, 1), dt.date(2026, 10, 2)))
    assert "reconcile 10-01" in current and "reconcile 10-01!" not in current
    assert "collect 10-02;" in current
    assert "since 2026-10-02 (reconcile 2026-10-01)" in current
    late = stage_text(StagesBQ(dt.date(2026, 9, 30), dt.date(2026, 10, 1)))
    assert "reconcile 09-30!" in late
    assert "collect 10-01!" in late  # the extra day is reconcile's alone


def test_select_only_refuses_anything_else():
    for sql in ("DELETE FROM t WHERE TRUE", "INSERT INTO t VALUES (1)", "  drop table t", ""):
        with pytest.raises(check_pages.Refused):
            check_pages.select_only(sql)


def test_bq_queries_carry_the_one_gigabyte_ceiling():
    bigquery = pytest.importorskip("google.cloud.bigquery")
    configs = []

    class Job:
        def result(self):
            return []

    class Client:
        def query(self, sql, job_config=None):
            configs.append(job_config)
            return Job()

    bq = check_pages.BQ(client=Client())
    bq.rows("SELECT 1 AS x WHERE @since IS NOT NULL", since=dt.date(2026, 9, 30))
    assert isinstance(configs[0], bigquery.QueryJobConfig)
    assert configs[0].maximum_bytes_billed == 1024 ** 3
    with pytest.raises(check_pages.Refused):
        bq.rows("DELETE FROM t WHERE TRUE")


def test_skin_today_is_counted_from_its_markets():
    body = {"markets": [{"market": "ZA", "cards": [{}, {}], "more": [{}]}, {"market": "NG", "cards": [], "more": []}]}
    assert check_pages.s_skin_today(body) == ("2 markets, 3 cards kept", False)


def test_seeds_is_read_for_the_market_and_its_fixture_queue_is_flagged_fake(local_api):
    client, seen = local_api
    _code, lines = run(client, argv=("--market", "NG"))
    assert any(r.url.path == "/api/seeds" and r.url.params.get("market") == "NG" for r in seen)
    line = next(line for line in lines if line.startswith("Seeds"))
    assert " 200 " in line and "FAKE" in line and "queued 2026-10-01 2 searches" in line


def test_seeds_summary_tags_an_empty_market_empty():
    body = {"status": "empty", "queue": None, "results": None}
    detail, empty = check_pages.s_seeds(body)
    assert empty and detail == "empty, queued - 0 searches, results - 0 posts"
    tag, _detail = check_pages.judge("Seeds", "/api/seeds?market=KE", 200, body, check_pages.s_seeds, True,
                                     dt.date(2026, 9, 30), None)
    assert tag == "EMPTY"
    ok = {"status": "ok", "queue": {"seed_date": "2026-10-01", "seeds_total": {"value": 7}},
          "results": {"seed_date": "2026-09-30", "totals": {"posts": {"value": 23}}}}
    tag, detail = check_pages.judge("Seeds", "/api/seeds?market=ZA", 200, ok, check_pages.s_seeds, True,
                                    dt.date(2026, 9, 30), None)
    assert tag == "OK" and detail.startswith("2026-09-30 (0d); ok, queued 2026-10-01 7 searches")
    tag, _detail = check_pages.judge("Seeds", "/api/seeds?market=ZA", 200, ok, check_pages.s_seeds, True,
                                     dt.date(2026, 10, 3), None)
    assert tag == "STALE"


def test_lexicon_reads_its_own_route_per_market(local_api):
    """Page port, 3 October 2026: Lexicon reads /api/lexicon, so it is checked like the other pages, not as legacy."""
    client, seen = local_api
    _code, lines = run(client, ["--market", "NG"])
    lexicon = next(line for line in lines if line.startswith("Lexicon"))
    assert "/api/lexicon?market=NG" in lexicon and " 200 " in lexicon and "FAKE" in lexicon and "2 terms" in lexicon
    assert not any("lexicon" in line.lower() for line in lines if line.startswith("  moved to 42 pages"))
    assert any(r.url.path == "/api/lexicon" and r.url.params.get("market") == "NG" for r in seen)


def test_an_empty_or_unready_lexicon_is_empty():
    assert check_pages.s_lexicon({"status": "empty", "terms": []}) == ("empty, 0 terms", True)
    assert check_pages.s_lexicon({"status": "ok", "terms": [{}, {}]}) == ("ok, 2 terms", False)
    tag, _detail = check_pages.judge("Lexicon", "/api/lexicon?market=ZA", 409, {"error": "not_ready"},
                                     check_pages.s_lexicon, True, dt.date(2026, 9, 30), None)
    assert tag == "EMPTY"


DAY = dt.date(2026, 10, 3)


def test_seed_path_is_probed_with_the_lexicons_word_not_a_discover_title(local_api):
    """Staging, 3 October 2026: Seed path read empty because it was probed with a Discover title, often a topic's
    label (42's name for a theme, rarely written in a post). It now takes the Lexicon's top seed_term, a hashtag
    first, and reads after Lexicon."""
    order = [page for page, *_ in check_pages.PAGES]
    assert order.index("Seed path") > order.index("Lexicon")
    template = next(t for page, t, *_ in check_pages.PAGES if page == "Seed path")
    assert "{seed_term}" in template and "{term}" not in template
    ids = {"market": "ZA"}
    body = {"terms": [{"kind": "meme", "seed_term": "sharp sharp"}, {"kind": "hashtag", "seed_term": None},
                      {"kind": "hashtag", "seed_term": "amapiano"}]}
    check_pages.learn_ids("/api/lexicon?market=ZA", body, ids)
    assert ids["seed_term"] == "amapiano"
    words_only = {"market": "ZA"}
    check_pages.learn_ids("/api/lexicon?market=ZA", {"terms": [{"kind": "meme", "seed_term": "sharp sharp"}]},
                          words_only)
    assert words_only["seed_term"] == "sharp sharp"
    nothing = {"market": "ZA"}
    check_pages.learn_ids("/api/lexicon?market=ZA", {"terms": [{"kind": "hashtag", "seed_term": None}]}, nothing)
    assert check_pages.fill(template, nothing) is None  # not opened, rather than probed with a made-up word
    client, seen = local_api
    _code, lines = run(client)
    lexicon_at = next(i for i, r in enumerate(seen) if r.url.path == "/api/lexicon")
    seed = [(i, r) for i, r in enumerate(seen) if r.url.path == "/api/seed-path"]
    assert seed and seed[0][0] > lexicon_at and seed[0][1].url.params.get("keyword")
    # A probe that still finds nothing is a real disagreement between Lexicon and Seed path, so it stays empty.
    assert check_pages.judge("Seed path", "/api/seed-path?keyword=amapiano&market=ZA", 200,
                             {"status": "no_match"}, check_pages.s_seed_path, True, DAY, None)[0] == "EMPTY"


def test_history_search_skips_a_stand_in_title_and_tries_a_hashtag_first():
    """Staging KE, 3 October 2026: History search was probed with "TikTok post", the stand-in title of an item with no
    readable label, and read empty; no item is called that. A real label is used, a hashtag first."""
    template = next(t for page, t, *_ in check_pages.PAGES if t.startswith("/api/history/search"))

    def term(items):
        ids = {"market": "KE"}
        check_pages.learn_ids("/api/discover?market=all&limit=200", {"items": items}, ids)
        return ids.get("term"), check_pages.fill(template, ids)

    assert term([{"item_id": "a", "market": "KE", "kind": "topic", "title": "TikTok post"},
                 {"item_id": "b", "market": "KE", "kind": "topic", "title": "Social media post"},
                 {"item_id": "c", "market": "KE", "kind": "meme", "title": "Instagram post by @someone"},
                 {"item_id": "d", "market": "KE", "kind": "topic", "title": "Matatu art"},
                 {"item_id": "e", "market": "KE", "kind": "hashtag", "title": "#kenyantiktok"}]) == (
        "kenyantiktok", "/api/history/search?q=kenyantiktok")
    assert term([{"item_id": "a", "market": "KE", "kind": "topic", "title": "TikTok post"},
                 {"item_id": "c", "market": "KE", "kind": "creator", "title": "Some Creator"},
                 {"item_id": "d", "market": "KE", "kind": "topic", "title": "Matatu art"}])[0] == "Matatu art"
    # Only stand-ins: the probe is not opened, rather than run with a title no item has.
    assert term([{"item_id": "a", "market": "KE", "kind": "topic", "title": "TikTok post"},
                 {"item_id": "b", "market": "KE", "kind": "hashtag", "title": "Hashtag item"}]) == (None, None)


def test_history_with_no_saved_finding_is_not_empty_but_its_other_reads_still_are():
    for body in ({"findings": []}, {"findings": None, "note": "Whether a finding still holds is not checked yet"}):
        tag, detail = check_pages.judge("History", "/api/history/findings", 200, body, check_pages.s_findings, False,
                                        DAY, None)
        assert tag == "OK" and detail == "0 findings (none saved yet)"
    asks = next(s for page, path, s, *_ in check_pages.PAGES if path == "/api/history/asks")
    assert check_pages.judge("History", "/api/history/asks", 200, {"asks": []}, asks, True, DAY, None)[0] == "EMPTY"
    assert check_pages.judge("History", "/api/history/briefs", 200, {"dates": []}, check_pages.s_history_briefs,
                             True, DAY, None)[0] == "EMPTY"
    assert check_pages.judge("History", "/api/history/findings", 500, {"error": "internal"},
                             check_pages.s_findings, False, DAY, None)[0] == "FAIL"


def test_brief_days_say_what_the_newest_brief_showed_and_held():
    def fig(v):
        return {"value": v, "unit": "u", "query_id": "q_history_briefs", "run_id": "r", "result_hash": "x"}

    body = {"dates": [{"date": "2026-10-03", "markets": [
        {"market": "ZA", "status": "published", "cards": fig(0), "held": fig(12)},
        {"market": "KE", "status": "data_issue"}]}, {"date": "2026-10-02", "markets": []}]}
    assert check_pages.s_history_briefs(body) == ("2 brief days; newest ZA 0c/12h KE -c/-h", False)
    tag, detail = check_pages.judge("History", "/api/history/briefs", 200, body, check_pages.s_history_briefs, True,
                                    DAY, None)
    assert tag == "OK" and detail.startswith("2026-10-03 (0d); 2 brief days")


def test_moved_desk_pages_follow_legacy_routes_and_carry_their_42_pages_result():
    source = (ROOT / "app" / "frontend" / "src" / "legacyRoutes.js").read_text(encoding="utf-8")
    moved = dict(re.findall(r"'?([a-z-]+)'?: '(#/[a-z]+)'", source.split("const MOVED", 1)[1].split("});", 1)[0]))
    for _old, route, target, _pages in check_pages.MOVED:
        if route in moved:
            assert moved[route] == target, route
        else:
            assert f"'{target}'" in source, route  # listen and the console are routed by code, not the table
    assert len(moved) == 5 and set(moved) <= {route for _o, route, _t, _p in check_pages.MOVED}
    pages = {page for page, *_ in check_pages.PAGES}
    assert all(set(p) <= pages for *_x, p in check_pages.MOVED)
    results = [("Discover", "OK", ""), ("Communities", "EMPTY", ""), ("Communities", "OK", ""),
               ("Alerts", "FAIL", "")]
    line = check_pages.moved_line(results)
    assert "Browse to Discover live" in line and "Network to Communities empty" in line
    assert "Board to Alerts broken" in line and "Listen to Seed path not read" in line
    # The moved line never decides the exit code: the pages it names are judged, and counted, on their own.
    assert check_pages.summarise(results, out=lambda _l: None) == 1


class TimedReader:
    """A stand-in reader: every read answers 200 with an empty body, and the reads named in slow take that long."""

    def __init__(self, slow):
        self.slow = slow

    def get(self, path, gated=True):
        if not gated:
            return 401, {}, 12, None
        return 200, {}, self.slow.get(path.split("?")[0], 300), None


def test_a_read_over_five_seconds_is_marked_slow_and_named_in_the_summary_with_each_pages_seconds():
    """4 October 2026: a 57 s Today read printed only its milliseconds and still read as live."""
    lines = []
    results = check_pages.run_pages(TimedReader({"/api/today": 57_000, "/api/coverage": 5_400}), "ZA", DAY,
                                    out=lines.append)
    today = next(line for line in lines if line.startswith("Today") and "/api/today" in line)
    coverage = next(line for line in lines if line.startswith("Coverage"))
    health = next(line for line in lines if line.startswith("Health"))
    assert "SLOW 57.0 s" in today and "SLOW 5.4 s" in coverage and "SLOW" not in health
    summary = []
    code = check_pages.summarise(results, out=summary.append)
    slow = next(line for line in summary if line.startswith("  slow"))
    assert "Today 57.0 s" in slow and "Coverage 5.4 s" in slow and "Health" not in slow
    seconds = next(line for line in summary if line.startswith("  seconds per page"))
    # Every read of the page added up: Today reads /api/today and /api/alerts here (its item read has no id yet).
    assert "Today 57.3 s" in seconds and "Discover 0.6 s" in seconds
    # Slow is reported, not judged: the exit code still says only whether a page is broken, empty, stale or fake.
    fast = check_pages.run_pages(TimedReader({}), "ZA", DAY, out=lambda _l: None)
    assert code == check_pages.summarise(fast, out=lambda _l: None)
    assert not any(line.startswith("  slow") for line in summary if "Today" not in line)


def test_no_slow_line_when_every_read_is_under_five_seconds():
    summary = []
    check_pages.summarise(check_pages.run_pages(TimedReader({"/api/today": 4_999}), "ZA", DAY, out=lambda _l: None),
                          out=summary.append)
    assert not any(line.startswith("  slow") for line in summary)
    assert any(line.startswith("  seconds per page") for line in summary)


class RunsBQ(FakeBQ):
    def __init__(self, stages):
        super().__init__()
        self.stages = stages

    def rows(self, sql, **params):
        return self.stages if "GROUP BY stage" in sql else super().rows(sql, **params)


def stage_lines(bq, today, now=None):
    lines = []
    check_pages.run_bq(bq, today, out=lines.append, now=now)
    return "\n".join(lines[lines.index(next(line for line in lines if line.startswith("Latest run per stage"))):])


def reconcile_bq(last_ok):
    return RunsBQ([{"stage": "reconcile", "last_ok": last_ok, "last_any": last_ok, "last_status": "ok"}])


def test_reconcile_two_days_back_is_current_in_the_morning_but_late_once_tonights_run_is_due():
    """Reconcile starts at 23:30 SAST for yesterday and times out after 15 minutes, so from 23:45 SAST its newest
    ok run_date should be yesterday, and two days back is late."""
    morning = dt.datetime(2026, 10, 4, 9, 0, tzinfo=check_pages.SAST)
    text = stage_lines(reconcile_bq(dt.date(2026, 10, 2)), morning.date(), now=morning)
    assert "reconcile 10-02" in text and "reconcile 10-02!" not in text
    assert "(reconcile 2026-10-02)" in text
    before = dt.datetime(2026, 10, 4, 23, 44, tzinfo=check_pages.SAST)
    assert "reconcile 10-02!" not in stage_lines(reconcile_bq(dt.date(2026, 10, 2)), before.date(), now=before)
    night = dt.datetime(2026, 10, 4, 23, 59, tzinfo=check_pages.SAST)
    late = stage_lines(reconcile_bq(dt.date(2026, 10, 2)), night.date(), now=night)
    assert "reconcile 10-02!" in late and "(reconcile 2026-10-03)" in late
    assert "reconcile 10-03!" not in stage_lines(reconcile_bq(dt.date(2026, 10, 3)), night.date(), now=night)


def test_the_page_check_passes_its_clock_to_the_reconcile_check(local_api):
    client, _seen = local_api
    night = dt.datetime(2026, 9, 30, 23, 59, tzinfo=check_pages.SAST)
    _code, lines = run(client, argv=("--bq",), now=night, bq=reconcile_bq(dt.date(2026, 9, 28)))
    assert "reconcile 09-28!" in "\n".join(lines)


def test_a_complete_ask_or_investigation_counts_as_its_ok_run():
    """Ask and investigation runs finish with status complete, never ok, so "no ok (last complete)" was wrong."""
    day = dt.date(2026, 10, 4)
    bq = RunsBQ([{"stage": "ask", "last_ok": None, "last_complete": dt.date(2026, 10, 3), "last_any": day,
                  "last_status": "complete"},
                 {"stage": "investigation", "last_ok": dt.date(2026, 9, 29), "last_complete": dt.date(2026, 10, 1),
                  "last_any": dt.date(2026, 10, 1), "last_status": "complete"},
                 {"stage": "scheduled", "last_ok": None, "last_complete": None, "last_any": day,
                  "last_status": "skipped"},
                 {"stage": "brief", "last_ok": dt.date(2026, 10, 3), "last_complete": dt.date(2026, 10, 4),
                  "last_any": day, "last_status": "complete"}])
    text = stage_lines(bq, day)
    assert re.search(r"\bask 10-03(;|\n|$)", text)
    assert "no ok (last complete)" not in text
    assert "investigation 10-01" in text and "investigation 10-01 (last" not in text
    assert "scheduled no ok (last skipped)" in text
    # A daily stage is ok only on ok: a brief recorded complete is not taken as its ok run.
    assert "brief 10-03 (last complete)" in text


def test_the_runs_query_reads_the_last_complete_run_date():
    bq = RunsBQ([])
    seen = []
    original = bq.rows

    def rows(sql, **params):
        seen.append(sql)
        return original(sql, **params)

    bq.rows = rows
    check_pages.run_bq(bq, DAY, out=lambda _l: None)
    runs = next(s for s in seen if "GROUP BY stage" in s)
    assert "MAX(IF(status = 'complete', run_date, NULL)) AS last_complete" in runs


class CreatorsBQ(FakeBQ):
    def rows(self, sql, **params):
        if "COUNT(DISTINCT p.creator_id)" in sql:
            self.sql.append(sql)
            self.params = params
            if "post_observations" in sql:
                return [{"market": "ZA", "creators": 410, "own_feed_creators": 37, "posts": 900},
                        {"market": "KE", "creators": 95, "own_feed_creators": 0, "posts": 120}]
            return [{"market": "ZA", "creators": 1234, "posts": 5678}, {"market": "NG", "creators": 88, "posts": 140},
                    {"market": None, "creators": 7, "posts": 9}]
        return super().rows(sql, **params)


def test_bq_section_counts_distinct_tiktok_creators_per_market_for_the_last_seven_days():
    """CO-8: Albert asked for 2,000 TikTok accounts a market a week; the paste shows how many are coming in."""
    bq = CreatorsBQ()
    lines = []
    check_pages.run_bq(bq, DAY, out=lines.append)
    text = "\n".join(lines)
    creator_sql = [s for s in bq.sql if "COUNT(DISTINCT p.creator_id)" in s]
    assert len(creator_sql) == 2 and all("platform = 'tiktok'" in s for s in creator_sql)
    assert all("BETWEEN @start AND @today" in s for s in creator_sql)
    # Own feeds by the project's rule (TRUST.md market by source): the sighting's source_market is the market.
    assert "IF(o.source_market = o.market, p.creator_id, NULL)" in creator_sql[1] and "'panel'" not in creator_sql[1]
    assert bq.params == {"start": dt.date(2026, 9, 26), "today": DAY}
    assert "TikTok creators 2026-09-26 to 2026-10-03" in text
    assert "by post market: ZA 1,234 (5,678 posts); NG 88 (140 posts); unlocated 7 (9 posts)" in text
    assert "by collected market: ZA 410, 37 from own feeds (900 posts); KE 95, 0 from own feeds (120 posts)" in text


def test_a_failed_creator_count_is_reported_and_does_not_stop_the_section():
    class Broken(FakeBQ):
        def rows(self, sql, **params):
            if "COUNT(DISTINCT p.creator_id)" in sql:
                raise RuntimeError("boom")
            return super().rows(sql, **params)

    lines = []
    check_pages.run_bq(Broken(), DAY, out=lines.append)
    text = "\n".join(lines)
    assert "TikTok creators read failed (RuntimeError)" in text and "boom" not in text
    assert any(line.startswith("Latest run per stage") for line in lines)


def test_series_test_line_tells_warm_up_from_a_key_not_switched_on(local_api):
    # 0 tested is warm-up only when no series is past it; series past warm-up stay untested until backtest --apply
    # writes their key's test_switch row (core/detect/stats.py _test_for), so the line counts both.
    from core.detect import stats

    class SwitchOff(FakeBQ):
        def rows(self, sql, **params):
            if "test != 'none'" in sql:
                assert "baseline_state IN ('short', 'ok')" in sql
                return [{"tested": 0, "total": 22024, "testable": 310}]
            return super().rows(sql, **params)

    assert check_pages.TESTABLE_STATES == "(" + ", ".join(f"'{s}'" for s in stats.TESTABLE) + ")"
    client, _seen = local_api
    _code, lines = run(client, argv=("--bq",), bq=SwitchOff())
    line = next(x for x in lines if "series_test tested" in x)
    assert "0 of 22024" in line and "310 were past warm-up (short or ok)" in line
    assert "test_switch" in line and "backtest --apply" in line


def _health_judge(body):
    page, path, summary, _gated, dated = next(p for p in check_pages.PAGES if p[0] == "Health")
    return check_pages.judge(page, path, 200, body, summary, dated, DAY, None)


def test_a_health_read_that_says_ok_false_fails_and_the_run_exits_1():
    tag, detail = _health_judge({"ok": False, "version": "v1",
                                 "checks": {"auth": "ok", "bigquery": "failed", "agent": "ok"}})
    assert tag == "FAIL" and "bigquery=failed" in detail
    results = [("Gate", "OK", "", 5), ("Health", tag, "/api/health", 5), ("Today", "OK", "/api/today", 5)]
    lines = []
    assert check_pages.summarise(results, out=lines.append) == 1
    assert any(line.startswith("  broken") and "Health" in line for line in lines)
    assert _health_judge({"version": "v1", "checks": {}})[0] == "FAIL"  # no ok at all is not a pass
    assert _health_judge("not a health body")[0] == "FAIL"
    tag, _detail = _health_judge({"ok": True, "version": "v1",
                                  "checks": {"auth": "ok", "bigquery": "ok", "agent": "ok"}})
    assert tag == "OK"
    assert check_pages.summarise([("Gate", "OK", "", 5), ("Health", tag, "/api/health", 5)],
                                 out=lambda _line: None) == 0


@pytest.mark.parametrize("dataset", ["intelligence_42_core", "intelligence_42_agent"])
def test_an_unreadable_api_object_inventory_counts_as_a_failure(dataset):
    class AllPresent(FakeBQ):
        def rows(self, sql, **params):
            if "INFORMATION_SCHEMA" in sql:
                if dataset in sql:
                    raise RuntimeError("denied")
                return [{"n": o} for o in check_pages.API_OBJECTS]
            if "GROUP BY stage" in sql:
                return [{"stage": s, "last_ok": DAY, "last_any": DAY, "last_status": "ok"}
                        for s in check_pages.STAGES]
            if "AS last3" in sql:
                return [{"latest": DAY, "last3": 10}]
            return super().rows(sql, **params)

    class Readable(AllPresent):
        def rows(self, sql, **params):
            if "INFORMATION_SCHEMA" in sql:
                return [{"n": o} for o in check_pages.API_OBJECTS]
            return super().rows(sql, **params)

    lines = []
    assert check_pages.run_bq(AllPresent(), DAY, out=lines.append) > 0
    assert any("API views read failed (RuntimeError)" in line for line in lines)
    assert not any("denied" in line for line in lines)
    assert check_pages.run_bq(Readable(), DAY, out=lambda _line: None) == 0


def today_body(*markets, date="2026-10-03"):
    return {"status": "partial", "date": date, "markets": [
        {"market": m, "cards": [{}] * c, "more": [], "breaking": [],
         "held_back": {"count": h, "items": [{}] * h}} for m, c, h in markets]}


def test_a_today_where_every_item_was_held_is_not_empty():
    """An honest held-only Today (the checks held every topic) is a read of real data, not an empty page."""
    body = today_body(("ZA", 0, 3), ("NG", 0, 2), ("KE", 0, 0))
    detail, empty = check_pages.s_today(body)
    assert empty is False
    assert "ZA 0c/3h" in detail and "NG 0c/2h" in detail
    tag, _ = check_pages.judge("Today", "/api/today", 200, body, check_pages.s_today, True, DAY, None)
    assert tag == "OK"


def test_a_today_with_no_cards_and_nothing_held_is_still_empty():
    body = today_body(("ZA", 0, 0), ("NG", 0, 0), ("KE", 0, 0))
    detail, empty = check_pages.s_today(body)
    assert empty is True
    tag, _ = check_pages.judge("Today", "/api/today", 200, body, check_pages.s_today, True, DAY, None)
    assert tag == "EMPTY"


def test_a_today_with_a_card_is_not_empty_whatever_was_held():
    body = today_body(("ZA", 1, 0), ("NG", 0, 0), ("KE", 0, 4))
    assert check_pages.s_today(body)[1] is False
