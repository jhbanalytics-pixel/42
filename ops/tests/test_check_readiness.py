"""The morning readiness report on fixtures: no BigQuery, a fake client that answers each named query."""

import datetime as dt
import json
import re

import pytest

from core.collect import chain
from core.detect.tests import duck
from ops.runners import check_readiness as cr

D = dt.date(2026, 10, 3)
D1, D2 = D - dt.timedelta(days=1), D - dt.timedelta(days=2)
NOW = dt.datetime(2026, 10, 3, 7, 5, tzinfo=cr.SAST)
SECRET_HANDLE, SECRET_TEXT = "secret_handle_x", "secret post text here"


def held(item_id, rule, reason, title="#tag"):
    return {"item_id": item_id, "rule": rule, "reason": reason, "title": title, "reason_text": SECRET_TEXT,
            "evidence": [{"id": "p1", "handle": SECRET_HANDLE, "text": SECRET_TEXT, "platform": "tiktok"}]}


def health(day, market, series, platform, lane_class="unbiased_rank", calls=(5, 5), units=(5, 5), valid=(1, 1),
           reasons=None):
    return {"day": day, "market": market, "platform": platform, "series": series, "lane_class": lane_class,
            "protocols": valid[1], "valid_protocols": valid[0], "calls_ok": calls[0], "calls": calls[1],
            "units_ok": units[0], "units_planned": units[1], "reasons": reasons}


ZA_PAYLOAD = {"cards": [], "more": [], "held_back": {"count": 4, "items": [
    held("i1", "G1", "data_issue"), held("i2", "G1", "data_issue"), held("i3", "G1", "data_issue"),
    held("i4", "G10", "support_check")]}}
NG_PAYLOAD = {"cards": [{"item_id": "n1", "title": "#ng", "evidence": []}], "more": [], "held_back": {"items": []}}

ROWS = {
    "brief runs": [{"run_id": "brief-20261003-a", "published_at": None, "run_status": "ok"},
                   {"run_id": "brief-20261003-b", "published_at": None, "run_status": "ok"}],
    "brief payloads": [{"market": "ZA", "status": "data_issue", "p": json.dumps(ZA_PAYLOAD)},
                       {"market": "NG", "status": "published", "p": json.dumps(NG_PAYLOAD)}],
    "main series": [{"item_id": "i1", "market": "ZA", "main_series_id": "s1", "platform": "youtube"},
                    {"item_id": "i2", "market": "ZA", "main_series_id": "s2", "platform": "youtube"},
                    {"item_id": "i3", "market": "ZA", "main_series_id": None, "platform": None}],
    "sightings": [{"item_id": "i3", "market": "ZA", "platform": "tiktok", "n": 4},
                  {"item_id": "i3", "market": "ZA", "platform": "instagram", "n": 2},
                  {"item_id": "i3", "market": "NG", "platform": "youtube", "n": 99}],
    "collection health": [
        health(D, "ZA", "board_youtube", "youtube"),
        health(D1, "ZA", "board_youtube", "youtube", calls=(0, 5), units=(0, 5), valid=(0, 5),
               reasons="all_calls_failed"),
        health(D, "ZA", "feed_tiktok", "tiktok"),
        health(D, "NG", "board_audiomack", "audiomack", calls=(1, 1), units=(0, 1), valid=(0, 1),
               reasons="no_entries"),
        health(D, "NG", "board_nairaland", "nairaland", calls=(0, 1), units=(0, 1), valid=(1, 1)),
        health(D, "KE", "news_rss", "news", lane_class="context"),
    ],
    "collect runs": [{"run_date": D, "status": "ok", "runs_that_day": 1, "sc_states": '{"ZA":"ok"}',
                      "bq_states": '{"ZA:top":"ok"}', "sc_rows": 20, "bq_rows": 50, "sc_error": False,
                      "bq_error": False, "local_error": True, "public_feed_error": False}],
    "search signals": [{"day": D, "market": "ZA", "source": "google_bq", "n": 25},
                       {"day": D, "market": "ZA", "source": "google_trending", "n": 10},
                       {"day": D, "market": "NG", "source": "google_bq", "n": 25}],
    "enrichment": [{"day": D, "market": "ZA", "sighted": 200, "enriched": 150},
                   {"day": D, "market": "NG", "sighted": 100, "enriched": 0}],
    "understand runs": [{"run_date": D, "status": "ok", "enriched": 150, "enrich_error": False,
                         "video": json.dumps({"read": 4, "failed": 1, "credits": 0})}],
    "transcript calls": [],
    "detect runs": [
        {"run_date": D, "status": "ok", "runs_that_day": 2, "spread_status": "ok", "spread_type": None,
         "agent_views_status": "failed", "agent_views_type": "BadRequest", "news_status": "failed",
         "news_type": "ValueError", "item_centroids_status": None, "item_centroids_type": None,
         "coaction_status": None, "coaction_type": None, "breakout_status": None, "breakout_type": None,
         "watch_status": "failed", "watch_type": "NotFound", "seeds_status": None, "seeds_type": None,
         "forecast_status": None, "forecast_type": None},
        {"run_date": D1, "status": "ok", "runs_that_day": 1, "spread_status": "ok", "spread_type": None,
         "agent_views_status": "ok", "agent_views_type": None, "news_status": "ok", "news_type": None,
         "item_centroids_status": None, "item_centroids_type": None, "coaction_status": "skipped",
         "coaction_type": "ImportError", "breakout_status": None, "breakout_type": None, "watch_status": None,
         "watch_type": None, "seeds_status": None, "seeds_type": None, "forecast_status": None,
         "forecast_type": None},
        {"run_date": D2, "status": "failed", "runs_that_day": 1, **{f"{k}_{c}": None for k in (
            "spread", "agent_views", "news", "item_centroids", "coaction", "breakout", "watch", "seeds", "forecast")
            for c in ("status", "type")}}],
    "clip reads": [{"read_from": "thumbnail", "n": 30}, {"read_from": "video", "n": 12}],
}

SOURCES = {
    ("ZA", "board_youtube"): {"platform": "youtube", "lane_class": "unbiased_rank", "days": {D, D1, D2}},
    ("ZA", "feed_tiktok"): {"platform": "tiktok", "lane_class": "unbiased_rank", "days": {D, D1}},
    ("NG", "board_audiomack"): {"platform": "audiomack", "lane_class": "unbiased_rank", "days": {D}},
    ("NG", "board_nairaland"): {"platform": "nairaland", "lane_class": "unbiased_rank", "days": {D}},
    ("KE", "board_shazam"): {"platform": "shazam", "lane_class": "unbiased_rank", "days": {D}},
    ("KE", "board_google_play"): {"platform": "google_play", "lane_class": "unbiased_rank", "days": {D2}},
}


class Row(dict):
    def items(self):
        return super().items()


class Job:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        if isinstance(self.rows, Exception):
            raise self.rows
        return [Row(r) for r in self.rows]


class FakeClient:
    def __init__(self, rows, fail=()):
        self.rows, self.fail, self.calls = rows, set(fail), []

    def query(self, sql, job_config=None):
        label = next(k for k, v in cr.SQL.items() if v == sql)
        self.calls.append((label, sql, job_config))
        return Job(RuntimeError("boom secret") if label in self.fail else self.rows.get(label, []))


def run(rows=ROWS, fail=(), sources=SOURCES):
    client, printed = FakeClient(rows, fail), []
    data = cr.fetch(cr.Reader(client, printed.append), D, sources_fn=lambda days: sources, now=NOW)
    return client, printed + cr.report(data)


@pytest.mark.parametrize("stage", ["collect", "understand", "detect"])
@pytest.mark.parametrize("with_skip", [False, True])
def test_run_queries_select_terminal_ties_and_ignore_skipped_duplicates(stage, with_skip):
    con = duck.connect(views=False)
    start = NOW.astimezone(dt.timezone.utc) - dt.timedelta(minutes=10)
    rows = [
        {"run_id": "real", "stage": stage, "run_date": D, "status": "running", "started_at": start},
        {"run_id": "real", "stage": stage, "run_date": D, "status": "ok", "started_at": start,
         "finished_at": start, "counts": '{"enriched":7}'},
    ]
    if with_skip:
        rows.append({"run_id": "duplicate", "stage": stage, "run_date": D, "status": "skipped_duplicate",
                     "started_at": NOW, "finished_at": NOW})
    duck.load(con, "agent.runs", rows)
    sql = cr.SQL[f"{stage} runs"].replace(cr.AGENT, "{agent}")
    rows = duck.query(con, sql, {"d": D})

    assert len(rows) == 1 and rows[0]["status"] == "ok"
    if stage in ("collect", "detect"):
        assert rows[0]["runs_that_day"] == 1
    if stage == "understand":
        assert rows[0]["enriched"] == 7


@pytest.mark.parametrize("stage", ["collect", "understand", "detect"])
@pytest.mark.parametrize("offset,status", [(-1, "running"), (0, "dead"), (1, "dead")])
def test_readiness_marks_a_running_stage_dead_at_its_existing_timeout(stage, offset, status):
    label = f"{stage} runs"
    row = {**ROWS[label][0], "status": "running", "finished_at": None,
           "started_at": NOW - chain.TIMEOUTS[stage] - dt.timedelta(microseconds=offset)}
    client = FakeClient({**ROWS, label: [row]})

    data = cr.fetch(cr.Reader(client), D, sources_fn=lambda days: SOURCES, now=NOW)

    assert data[stage][0]["status"] == status
    assert row["status"] == "running"


@pytest.mark.parametrize("stage", ["collect", "understand", "detect"])
def test_a_genuine_later_running_attempt_is_the_effective_readiness_state(stage):
    con = duck.connect(views=False)
    start = NOW.astimezone(dt.timezone.utc) - dt.timedelta(minutes=10)
    duck.load(con, "agent.runs", [
        {"run_id": "old", "stage": stage, "run_date": D, "status": "ok", "started_at": start,
         "finished_at": start},
        {"run_id": "new", "stage": stage, "run_date": D, "status": "running", "started_at": NOW},
    ])

    rows = duck.query(con, cr.SQL[f"{stage} runs"].replace(cr.AGENT, "{agent}"), {"d": D})

    assert len(rows) == 1 and rows[0]["status"] == "running"
    if stage in ("collect", "detect"):
        assert rows[0]["runs_that_day"] == 2


@pytest.mark.parametrize("stage", ["collect", "understand", "detect"])
@pytest.mark.parametrize("status", ["ok", "failed", "blocked"])
def test_old_terminal_states_never_become_dead(stage, status):
    label = f"{stage} runs"
    row = {**ROWS[label][0], "status": status, "started_at": NOW - dt.timedelta(days=1), "finished_at": NOW}
    client = FakeClient({**ROWS, label: [row]})

    data = cr.fetch(cr.Reader(client), D, sources_fn=lambda days: SOURCES, now=NOW)

    assert data[stage][0]["status"] == status


def test_summary_comes_first_and_names_holds_g1_platforms_and_failing_feeds():
    _, lines = run()
    top = lines[lines.index("== SUMMARY") + 1:]
    assert top[0].startswith("ZA: data_issue, 0 cards, 4 held (G1 3, G10 1); G1 by main platform tiktok 1, youtube 2")
    assert "invalid baseline feeds d-2..d 1" in top[0]
    assert top[1].startswith("NG: published, 1 cards, 0 held (none)")
    assert top[2].startswith("KE: no brief, 0 cards")
    assert any(x.startswith("google trends 10-03:") and "FLAG no rows from KE google_trending, NG google_trending" in x for x in top)
    assert any("transcript calls 0 (FLAG: no transcripts, frames only)" in x for x in top)


def test_the_latest_run_is_read_unless_one_is_named():
    client, _ = run()
    payload_call = next(c for c in client.calls if c[0] == "brief payloads")
    rid = {p.name: p.value for p in payload_call[2].query_parameters}["rid"]
    assert rid == "brief-20261003-b"


def test_g1_platform_follows_the_gate_and_shows_the_bad_days_on_it():
    _, lines = run()
    assert "  G1 main platform youtube: 2 items; invalid baseline days on it: 10-02" in lines
    assert "  G1 main platform tiktok: 1 items; invalid baseline days on it: none" in lines
    assert any(x.startswith("    10-02 board_youtube [youtube] calls 0/5 units 0/5 valid 0/5: all_calls_failed")
               for x in lines)


def test_main_platform_prefers_the_main_series_then_most_sightings_with_ties_alphabetical():
    g1 = [("ZA", "a"), ("ZA", "b"), ("ZA", "c")]
    series = [{"item_id": "a", "market": "ZA", "platform": "x"}]
    sightings = [{"item_id": "a", "market": "ZA", "platform": "y", "n": 50},
                 {"item_id": "b", "market": "ZA", "platform": "reddit", "n": 3},
                 {"item_id": "b", "market": "ZA", "platform": "instagram", "n": 3}]
    assert cr.main_platforms(g1, series, sightings) == {("ZA", "a"): "x", ("ZA", "b"): "instagram", ("ZA", "c"): None}


def test_sources_flag_invalid_no_success_and_missing_rows_but_not_unplanned_days():
    _, lines = run()
    joined = "\n".join(lines)
    assert "  NOT PULLING board_audiomack [audiomack, baseline]: c1/1 u0/1 BAD" in joined
    assert "  NOT PULLING board_nairaland [nairaland, baseline]: c0/1 u0/1 ok" in joined
    assert "  NOT PULLING board_shazam [shazam, baseline]: no row" in joined
    assert re.search(r"\n  board_google_play \[google_play, baseline\]: not planned \| not planned \| no row", joined)
    assert re.search(r"\n  board_youtube \[youtube, baseline\]: c5/5 u5/5 ok \| c0/5 u0/5 BAD \| no row", joined)
    assert "    invalid: 10-02 all_calls_failed" in joined
    assert "health rows for series not in the plan: KE news_rss" in joined
    assert "not pulling on d: NG board_audiomack, board_nairaland; KE board_shazam" in joined


def test_understand_and_trends_detail():
    _, lines = run()
    assert "  10-03 enriched/sighted: ZA 150/200 75% | NG 0/100 0% | KE 0/0" in lines
    assert any(x.startswith("  10-03 understand run ok: enriched 150; video read 4, failed 1") for x in lines)
    assert "  clips read so far: thumbnail frame only 30, linked clip (frames and sound) 12" in lines
    assert "  10-03 rows: NG google_bq 25, ZA google_bq 25, ZA google_trending 10" in lines
    assert any(x.startswith("  10-03 collect run ok: trending read 20 rows") and "errors set: local_error" in x
               for x in lines)
    assert "  10-02 collect run: none" in lines


def test_an_understand_run_with_null_or_missing_video_counts_reads_as_not_run():
    # TO_JSON_STRING of a missing $.video comes back as the string "null".
    for video in ("null", None, "", "[]", "not json"):
        runs = [{"run_date": D, "status": "ok", "enriched": 150, "enrich_error": False, "video": video},
                {"run_date": D1, "status": "ok", "enriched": 3, "enrich_error": False}]
        _, lines = run({**ROWS, "understand runs": runs})
        assert any(x.startswith("understand 10-03: run ok,") and "clips read 0;" in x for x in lines), video
        assert any(x.startswith("  10-03 understand run ok: enriched 150; video not run;") for x in lines), video
        assert any(x.startswith("  10-02 understand run ok: enriched 3; video not run;") for x in lines), video


def test_the_summary_flags_failed_view_builds_in_the_days_detect_run():
    _, lines = run()
    top = lines[lines.index("== SUMMARY") + 1:]
    assert any(x == "detect 10-03: run ok (latest of 2); FLAG view builds failed: agent_views (BadRequest), "
               "news (ValueError), so the pages reading those views can be empty or stale; other steps failed: "
               "watch (NotFound)" for x in top)


def test_the_summary_says_when_every_view_build_passed_or_no_detect_ran():
    rows = dict(ROWS, **{"detect runs": [r for r in ROWS["detect runs"] if r["run_date"] != D]})
    _, lines = run(rows)
    assert "detect 10-03: no run" in lines
    ok = dict(ROWS["detect runs"][1], run_date=D)
    _, lines = run(dict(ROWS, **{"detect runs": [ok]}))
    assert "detect 10-03: run ok; view builds ok; other steps failed: coaction skipped (ImportError)" in lines


def test_detect_detail_shows_seven_days_of_view_builds_and_steps():
    _, lines = run()
    i = lines.index("== DETECT (view builds with no runs row of their own, then the other steps)")
    detail = lines[i + 1:i + 1 + cr.DAYS]
    assert detail[0] == ("  10-03 detect run ok (latest of 2): view builds failed agent_views (BadRequest), "
                         "news (ValueError); steps failed watch (NotFound)")
    assert detail[1] == "  10-02 detect run ok: view builds ok; steps failed coaction skipped (ImportError)"
    assert detail[2] == "  10-01 detect run failed: view builds not run; steps failed none"
    assert detail[3] == "  09-30 detect run: none"


def test_a_failed_detect_read_shows_a_question_mark():
    _, lines = run(fail=("detect runs",))
    assert "detect: ?" in lines and "  detect runs: ?" in lines


def test_the_detect_read_takes_only_the_error_type_never_its_text():
    sql = cr.SQL["detect runs"]
    for step in cr.DETECT_VIEW_STEPS + cr.DETECT_OTHER_STEPS:
        assert f"JSON_VALUE(counts, '$.{step}.status') {step}_status" in sql
        assert f"REGEXP_EXTRACT(JSON_VALUE(counts, '$.{step}.error'), r'^([A-Za-z_][A-Za-z0-9_.]*):') {step}_type" in sql
        assert sql.count(f"'$.{step}.error'") == 1
    assert cr.DETECT_VIEW_STEPS == ("spread", "agent_views", "news", "item_centroids")


def test_a_second_start_after_an_ok_run_does_not_hide_that_runs_flags():
    sql = cr.SQL["detect runs"]
    assert "status != 'skipped_duplicate'" in sql.split("QUALIFY")[0]


def test_no_handles_post_text_or_error_text_reach_the_output():
    _, lines = run(fail=("sightings",))
    joined = "\n".join(lines)
    assert SECRET_HANDLE not in joined and SECRET_TEXT not in joined and "boom" not in joined
    assert "[sightings: query failed (RuntimeError); shown as ?]" in joined


def test_a_failed_query_shows_question_marks_and_the_rest_still_prints():
    _, lines = run(fail=("collection health", "enrichment", "search signals"))
    joined = "\n".join(lines)
    assert "invalid baseline feeds d-2..d ?" in joined and "== SOURCES" in joined
    assert "understand: ?" in joined and "google trends: ?" in joined and "  ?" in joined


def test_every_query_is_a_capped_select():
    client, _ = run()
    assert {c[0] for c in client.calls} == set(cr.SQL)
    for label, sql, cfg in client.calls:
        assert cfg.maximum_bytes_billed == 10**9, label
        assert re.match(r"\s*(SELECT|WITH)\b", sql), label
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|TRUNCATE|ALTER)\b", sql), label


def test_planned_sources_come_from_the_real_collect_plan():
    sources = cr.planned_sources([D])
    for key in [("ZA", "board_youtube"), ("NG", "board_boomplay"), ("KE", "board_shazam"),
                ("ZA", "news_rss"), ("NG", "board_music_country"), ("ZA", "panel_ig_gossip"), ("ZA", "search")]:
        assert key in sources and D in sources[key]["days"], key
    assert sources[("ZA", "board_youtube")]["lane_class"] == "unbiased_rank"
    # retired: kworb KE (6057eb85), the Nairaland front page and the Audiomack pages (b57a21ab)
    for key in [("KE", "board_kworb_spotify"), ("NG", "board_nairaland"), ("KE", "board_audiomack")]:
        assert key not in sources, key


def test_a_series_that_pulled_every_call_but_failed_the_items_check_is_not_called_not_pulling():
    # 3 Oct 2026: KE search made 42 of 42 calls and read posts, but the day's items fell outside half to twice
    # the reference (writers.judge "items"). That is a judged-invalid day, not a source that is not pulling.
    rows = dict(ROWS, **{"collection health": ROWS["collection health"] + [
        health(D, "KE", "search", "tiktok", lane_class="search_presence", calls=(42, 42), units=(42, 42),
               valid=(2, 3), reasons="items")]})
    sources = {**SOURCES, ("KE", "search"): {"platform": "tiktok", "lane_class": "search_presence", "days": {D}}}
    _, lines = run(rows, sources=sources)
    joined = "\n".join(lines)
    assert "  INVALID ON items search [tiktok, search_presence]: c42/42 u42/42 BAD" in joined
    assert "NOT PULLING search" not in joined
    assert "not pulling on d: NG board_audiomack, board_nairaland; KE board_shazam" in joined
    assert "pulled but judged invalid on d: KE search (items)" in joined


def test_protocols_planned_but_never_called_are_counted_on_the_invalid_line():
    # 3 Oct 2026: every news_rss fetch that was made came back, but feeds the robots.txt check refused were
    # recorded with 0 calls, which writers.judge marks invalid on calls; c counts only the calls made.
    row = health(D, "ZA", "news_rss", "news", lane_class="context", calls=(12, 12), units=(12, 15),
                 valid=(12, 15), reasons="calls: robots")
    row["not_called"] = 3
    rows = dict(ROWS, **{"collection health": ROWS["collection health"] + [row]})
    sources = {**SOURCES, ("ZA", "news_rss"): {"platform": "news", "lane_class": "context", "days": {D}}}
    _, lines = run(rows, sources=sources)
    joined = "\n".join(lines)
    assert "  NOT PULLING news_rss [news, context]: c12/12 u12/15 BAD" in joined
    assert "    invalid: 10-03 calls: robots (3 of 15 protocols not called)" in joined
    assert "COUNTIF(IFNULL(h.calls, 0) = 0) not_called" in cr.SQL["collection health"]


def test_a_title_a_cp1252_console_cannot_show_does_not_stop_the_report(monkeypatch):
    """Albert's PC, piped: stdout is cp1252, and an item title such as 昕昕 would raise UnicodeEncodeError."""
    import io
    import sys

    monkeypatch.setattr(cr, "fetch", lambda reader, d, rid: {})
    monkeypatch.setattr(cr, "report", lambda data: ["ZA | 昕昕 | held G10", "== END"])
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    assert cr.main([D.isoformat()], client=object()) == 0
    sys.stdout.flush()
    assert raw.getvalue().decode("cp1252").splitlines() == ["ZA | ?? | held G10", "== END"]
