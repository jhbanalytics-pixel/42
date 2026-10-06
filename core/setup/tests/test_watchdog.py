"""Unit tests for core/setup/watchdog.py. No cloud access: a fake store answers each rule's question, and a
fake BigQuery client records every query and its parameters."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from core.collect import chain
from core.setup import watchdog as wd

DAY = date(2026, 10, 1)
SAST = timezone(timedelta(hours=2))


def at(hh, mm=0, day=DAY):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=SAST).astimezone(timezone.utc)


class FakeStore:
    """A healthy day unless a test says otherwise."""

    def __init__(self, collect_ok=True, observations=None, published=("ZA", "NG", "KE"), usd=0.0, keys=None,
                 ask=(0, 0), runs=None, seeds=None, detect=None):
        self.runs = runs
        self._seeds = seeds
        self._detect = detect
        self._collect_ok = collect_ok
        self._observations = {"ZA": 120, "NG": 90, "KE": 75} if observations is None else observations
        self._published = set(published)
        self._usd = usd
        self._keys = keys or []
        self._ask = ask
        self.asked = []

    def collect_ok(self, d):
        self.asked.append(("collect_ok", d))
        return self._collect_ok

    def observations(self, d):
        self.asked.append(("observations", d))
        return dict(self._observations)

    def published_markets(self, d):
        self.asked.append(("published_markets", d))
        return set(self._published)

    def model_usd(self, d):
        self.asked.append(("model_usd", d))
        return self._usd

    def route_keys(self, d):
        self.asked.append(("route_keys", d))
        return list(self._keys)

    def ask_runs(self, d):
        self.asked.append(("ask_runs", d))
        return self._ask

    def seeds_latest(self, d):
        self.asked.append(("seeds_latest", d))
        return self._seeds

    def detect_latest(self, d):
        self.asked.append(("detect_latest", d))
        return self._detect

    def fired_today(self, d):
        """Read back from the watchdog rows main() appended, as BigQueryStore does from the runs table."""
        rows = self.runs.rows if self.runs is not None else []
        return {n for r in rows if r["stage"] == "watchdog" and r["run_date"] == d.isoformat()
                for n in (r["counts"] or {}).get("fired", [])}


def names(alerts):
    return [a.name for a in alerts]


def keyrow(route, key, today, prior, today_total, prior_total):
    return {"route": route, "key": key, "today_rows": today, "prior_rows": prior,
            "today_total": today_total, "prior_total": prior_total}


STABLE = [keyrow("tiktok/trending", k, 10, 70, 10, 70) for k in ("success", "data", "credits_used")]


def test_a_healthy_morning_fires_nothing():
    assert wd.check(at(7, 0), FakeStore(keys=STABLE)) == []


@pytest.mark.parametrize("hh,mm,fires", [(3, 45, False), (4, 0, True), (7, 30, True)])
def test_collection_missing_fires_from_0400_sast_without_an_ok_collect(hh, mm, fires):
    alerts = wd.check(at(hh, mm), FakeStore(collect_ok=False, observations={}, published=()))
    assert ("collection_missing" in names(alerts)) is fires


def test_collection_missing_stays_quiet_when_collect_ran_ok():
    assert "collection_missing" not in names(wd.check(at(5, 0), FakeStore()))


def test_zero_rows_fires_for_an_ok_collect_with_a_market_that_has_no_observations():
    alerts = wd.check(at(4, 30), FakeStore(observations={"ZA": 120, "KE": 0}))
    assert names(alerts) == ["zero_rows"]
    assert "NG" in alerts[0].reason and "KE" in alerts[0].reason and "ZA" not in alerts[0].reason


def test_zero_rows_stays_quiet_when_every_market_has_rows_or_collect_is_not_ok():
    assert "zero_rows" not in names(wd.check(at(4, 30), FakeStore()))
    assert "zero_rows" not in names(wd.check(at(3, 0), FakeStore(collect_ok=False, observations={})))


@pytest.mark.parametrize("hh,mm,fires", [(6, 15, False), (6, 29, False), (6, 30, True), (9, 0, True)])
def test_brief_late_fires_from_0630_sast_for_a_market_without_a_published_brief(hh, mm, fires):
    alerts = wd.check(at(hh, mm), FakeStore(published=("ZA", "KE")))
    assert ("brief_late" in names(alerts)) is fires
    if fires:
        reason = [a for a in alerts if a.name == "brief_late"][0].reason
        assert "NG" in reason and "ZA" not in reason


def test_brief_late_stays_quiet_when_every_market_published():
    assert "brief_late" not in names(wd.check(at(8, 0), FakeStore()))


@pytest.fixture
def old_model_cap_schedule(monkeypatch, tmp_path):
    """MODEL_DAILY_USD as it stood until 3 October 2026 (USD 20, USD 80 on 1 and 2 October SAST), for the tests
    of the window and its expiry; the live core/config/caps.yaml has no window now."""
    from core.config import caps as caps_config

    path = tmp_path / "caps.yaml"
    path.write_text('MODEL_DAILY_USD:\n  default: 20\n  temporary:\n    amount: 80\n'
                    '    starts_on: "2026-10-01"\n    ends_on: "2026-10-02"\n', encoding="utf-8")
    monkeypatch.setattr(wd, "model_daily_usd", lambda *, now=None: caps_config.model_daily_usd(path, now=now))


@pytest.mark.parametrize("now,usd,fires", [
    (at(12, day=date(2026, 10, 3)), 40.0, False),
    (at(12, day=date(2026, 10, 3)), 40.01, True),
])
def test_model_spend_fires_over_80_percent_of_the_live_daily_cap(now, usd, fires):
    alerts = wd.check(now, FakeStore(usd=usd))
    assert ("model_spend" in names(alerts)) is fires


@pytest.mark.usefixtures("old_model_cap_schedule")
@pytest.mark.parametrize("now,usd,fires", [
    (at(12, day=date(2026, 10, 1)), 64.0, False),
    (at(12, day=date(2026, 10, 1)), 64.01, True),
    (at(12, day=date(2026, 10, 3)), 16.0, False),
    (at(12, day=date(2026, 10, 3)), 16.01, True),
])
def test_model_spend_fires_over_80_percent_of_the_active_daily_cap(now, usd, fires):
    alerts = wd.check(now, FakeStore(usd=usd))
    assert ("model_spend" in names(alerts)) is fires


@pytest.mark.usefixtures("old_model_cap_schedule")
def test_model_spend_uses_the_cap_at_check_time_across_sast_midnight():
    store = FakeStore(usd=20.0)
    before_expiry = datetime(2026, 10, 2, 21, 59, 59, tzinfo=timezone.utc)
    after_expiry = datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)
    assert "model_spend" not in names(wd.check(before_expiry, store))
    assert "model_spend" in names(wd.check(after_expiry, store))


def test_schema_drift_fires_on_a_new_top_level_key():
    keys = STABLE + [keyrow("tiktok/trending", "warning", 3, 0, 10, 70)]
    alerts = wd.check(at(7, 0), FakeStore(keys=keys))
    assert names(alerts) == ["schema_drift"]
    assert "tiktok/trending" in alerts[0].reason and "warning" in alerts[0].reason


def test_schema_drift_fires_when_a_key_every_prior_response_carried_is_gone():
    keys = [keyrow("x/search", "success", 5, 40, 5, 40), keyrow("x/search", "data", 0, 40, 5, 40)]
    alerts = wd.check(at(7, 5), FakeStore(keys=keys))
    assert names(alerts) == ["schema_drift"]
    assert "x/search" in alerts[0].reason and "data" in alerts[0].reason


def test_schema_drift_stays_quiet_on_a_sometimes_key_a_new_route_or_a_route_not_called_today():
    keys = STABLE + [
        keyrow("tiktok/trending", "next_cursor", 0, 30, 10, 70),  # only some prior responses carried it
        keyrow("youtube/new", "items", 4, 0, 4, 0),  # first day of a new route: nothing to compare with
        keyrow("reddit/hot", "posts", 0, 50, 0, 50),  # not called today: collection rules cover that
    ]
    assert wd.check(at(7, 0), FakeStore(keys=keys)) == []


@pytest.mark.parametrize("hh,mm,checked", [(6, 45, False), (7, 0, True), (7, 14, True), (7, 15, False), (13, 0, False)])
def test_schema_drift_runs_once_a_day_in_the_0700_run(hh, mm, checked):
    store = FakeStore(keys=STABLE + [keyrow("tiktok/trending", "warning", 3, 0, 10, 70)])
    alerts = wd.check(at(hh, mm), store)
    assert ("schema_drift" in names(alerts)) is checked
    assert (("route_keys", DAY) in store.asked) is checked


@pytest.mark.parametrize("ask,fires", [((19, 19), False), ((20, 1), False), ((20, 2), True), ((100, 5), False),
                                       ((100, 6), True), ((0, 0), False)])
def test_agent_error_rate_fires_over_5_percent_once_there_are_20_runs(ask, fires):
    alerts = wd.check(at(15, 0), FakeStore(ask=ask))
    assert ("agent_error_rate" in names(alerts)) is fires


def seeds(status, run_id="seeds-20261001-abc123"):
    return {"run_id": run_id, "status": status}


def test_seeds_failed_fires_when_the_days_latest_seeds_row_failed():
    [alert] = wd.check(at(3, 0), FakeStore(seeds=seeds("failed")))
    assert alert.name == "seeds_failed"
    assert "2026-10-01" in alert.reason and "seeds-20261001-abc123" in alert.reason and "failed" in alert.reason


@pytest.mark.parametrize("latest", [None, seeds("ok")])
def test_seeds_failed_stays_quiet_without_a_seeds_row_or_when_the_latest_is_ok(latest):
    assert wd.check(at(7, 0), FakeStore(keys=STABLE, seeds=latest)) == []


def test_seeds_failed_writes_the_shared_alert_line_and_fires_once_a_day(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(seeds=seeds("failed"), runs=runs)
    assert wd.main(now=at(3, 0), store=store, runs=runs) == 0
    [line] = lines(capsys.readouterr().out)
    assert line["severity"] == "ERROR" and line["alert"] == "seeds_failed"
    assert line["message"].startswith("42 ALERT seeds_failed: ")
    assert wd.main(now=at(3, 15), store=store, runs=runs) == 0
    [again] = lines(capsys.readouterr().out)
    assert again["severity"] == "INFO" and "seeds_failed" in again["message"]
    assert [r["counts"]["fired"] for r in runs.rows if r["status"] != "running"] == [["seeds_failed"], []]


def test_bigquery_store_reads_the_latest_seeds_row_of_the_day():
    client = FakeClient({"stage = 'seeds'": [{"run_id": "seeds-20261001-b", "status": "failed"}]})
    store = wd.BigQueryStore(client)
    assert store.seeds_latest(DAY) == {"run_id": "seeds-20261001-b", "status": "failed"}
    [(sql, params)] = client.queries
    assert params == {"d": DAY}
    assert "`ogilvy-trends-v2.intelligence_42_agent.runs`" in sql
    assert "run_date = @d" in sql and "stage = 'seeds'" in sql
    assert "ORDER BY COALESCE(finished_at, started_at) DESC" in sql and "LIMIT 1" in sql
    for word in ("INSERT", "UPDATE", "MERGE", "DELETE", "CREATE", "DROP"):
        assert word not in sql.upper().split()
    assert wd.BigQueryStore(FakeClient({"stage = 'seeds'": []})).seeds_latest(DAY) is None


def detect(status, error=None, run_id="detect-20261001-def456"):
    return {"run_id": run_id, "agent_views_status": status, "agent_views_error": error}


def test_agent_views_failed_fires_with_the_error_when_the_days_latest_detect_row_failed_the_views():
    [alert] = wd.check(at(3, 0), FakeStore(detect=detect("failed", "BadRequest: 400 Unrecognized name: gate")))
    assert alert.name == "agent_views_failed"
    assert "2026-10-01" in alert.reason and "detect-20261001-def456" in alert.reason
    assert alert.reason.endswith(": BadRequest: 400 Unrecognized name: gate")


def test_agent_views_failed_fires_without_error_text_when_counts_carries_none():
    [alert] = wd.check(at(3, 0), FakeStore(detect=detect("failed")))
    assert alert.name == "agent_views_failed" and "detect-20261001-def456" in alert.reason
    assert "None" not in alert.reason


@pytest.mark.parametrize("latest", [None, detect("ok"), detect(None)])
def test_agent_views_failed_stays_quiet_without_a_detect_row_when_ok_or_when_counts_has_no_agent_views(latest):
    assert wd.check(at(7, 0), FakeStore(keys=STABLE, detect=latest)) == []


def test_agent_views_failed_writes_the_shared_alert_line_and_fires_once_a_day(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(detect=detect("failed", "NotFound: 404 v_item_gate_current"), runs=runs)
    assert wd.main(now=at(3, 0), store=store, runs=runs) == 0
    [line] = lines(capsys.readouterr().out)
    assert line["severity"] == "ERROR" and line["alert"] == "agent_views_failed"
    assert line["message"].startswith("42 ALERT agent_views_failed: ")
    assert line["message"].endswith("NotFound: 404 v_item_gate_current")
    assert wd.main(now=at(3, 15), store=store, runs=runs) == 0
    [again] = lines(capsys.readouterr().out)
    assert again["severity"] == "INFO" and "agent_views_failed" in again["message"]
    assert [r["counts"]["fired"] for r in runs.rows if r["status"] != "running"] == [["agent_views_failed"], []]
    store.asked.clear()
    wd.main(now=at(3, 30), store=store, runs=runs)
    assert ("detect_latest", DAY) not in store.asked


def test_bigquery_store_reads_the_agent_views_outcome_of_the_latest_detect_row_of_the_day():
    row = {"run_id": "detect-20261001-b", "agent_views_status": "failed", "agent_views_error": "NotFound: x"}
    client = FakeClient({"stage = 'detect'": [row]})
    store = wd.BigQueryStore(client)
    assert store.detect_latest(DAY) == row
    [(sql, params)] = client.queries
    assert params == {"d": DAY}
    assert "`ogilvy-trends-v2.intelligence_42_agent.runs`" in sql
    assert "run_date = @d" in sql and "stage = 'detect'" in sql
    assert "JSON_VALUE(counts, '$.agent_views.status')" in sql and "JSON_VALUE(counts, '$.agent_views.error')" in sql
    assert "ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC LIMIT 1" in sql
    for word in ("INSERT", "UPDATE", "MERGE", "DELETE", "CREATE", "DROP"):
        assert word not in sql.upper().split()
    assert wd.BigQueryStore(FakeClient({"stage = 'detect'": []})).detect_latest(DAY) is None


def test_every_rule_reads_the_sast_day_not_the_utc_day():
    store = FakeStore()
    wd.check(datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc), store)  # 00:30 SAST on 1 October
    assert {d for _, d in store.asked} == {DAY}


def test_run_date_in_the_environment_overrides_the_day(monkeypatch):
    monkeypatch.setenv("RUN_DATE", "2026-09-29")
    store = FakeStore()
    wd.check(at(9, 0), store)
    assert {d for _, d in store.asked} == {date(2026, 9, 29)}


def test_every_alert_name_is_one_of_the_rules():
    assert wd.ALERTS == ("collection_missing", "zero_rows", "brief_late", "model_spend", "schema_drift",
                         "agent_error_rate", "seeds_failed", "agent_views_failed")


def lines(out):
    return [json.loads(line) for line in out.strip().splitlines()]


def test_a_forced_zero_rows_run_writes_one_error_line_named_for_the_alert(capsys):
    store = FakeStore(observations={"ZA": 0, "NG": 90, "KE": 75})
    assert wd.main(now=at(4, 30), store=store, runs=chain.MemoryRunsStore()) == 0
    out = lines(capsys.readouterr().out)
    alerts = [o for o in out if o["severity"] == "ERROR"]
    assert len(alerts) == 1
    assert alerts[0]["message"].startswith("42 ALERT zero_rows: ")
    assert "ZA" in alerts[0]["message"]
    assert alerts[0]["alert"] == "zero_rows"


def test_one_line_per_firing_alert_and_an_info_line_when_all_is_clear(capsys):
    runs = chain.MemoryRunsStore()
    assert wd.main(now=at(7, 0), store=FakeStore(collect_ok=False, observations={}, published=()), runs=runs) == 0
    out = lines(capsys.readouterr().out)
    assert [o["alert"] for o in out if o["severity"] == "ERROR"] == ["collection_missing", "brief_late"]
    assert wd.main(now=at(7, 0), store=FakeStore(keys=STABLE), runs=chain.MemoryRunsStore()) == 0
    out = lines(capsys.readouterr().out)
    assert [o["severity"] for o in out] == ["INFO"]
    assert "42 ALERT" not in out[0]["message"]


class BrokenStore(FakeStore):
    def model_usd(self, d):
        raise RuntimeError("query failed")


def test_a_rule_that_cannot_run_does_not_stop_the_others_and_fails_the_job(capsys):
    store, runs = BrokenStore(published=("ZA",)), chain.MemoryRunsStore()
    assert wd.main(now=at(7, 0), store=store, runs=runs) == 1
    out = lines(capsys.readouterr().out)
    assert [o["alert"] for o in out if o.get("alert")] == ["brief_late"]
    assert any("model_spend" in o["message"] and "query failed" in o["message"] for o in out if o["severity"] == "ERROR"
               and not o.get("alert"))
    assert ("ask_runs", DAY) in store.asked
    [row] = runs.rows
    assert row["status"] == "failed" and "model_spend" in row["error"]
    assert row["counts"]["fired"] == ["brief_late"] and row["counts"]["errors"] == ["model_spend"]


def test_each_run_appends_one_watchdog_runs_row_listing_what_fired(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(observations={"ZA": 0, "NG": 90, "KE": 75}, runs=runs)
    assert wd.main(now=at(4, 30), store=store, runs=runs) == 0
    [row] = runs.rows
    assert (row["stage"], row["run_date"], row["status"]) == ("watchdog", "2026-10-01", "ok")
    assert row["run_id"].startswith("watchdog-20261001-")
    assert row["finished_at"] and row["error"] is None
    assert row["counts"]["fired"] == ["zero_rows"]


def test_an_alert_fires_at_most_once_a_day(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(observations={"ZA": 0, "NG": 90, "KE": 75}, published=("NG",), runs=runs)
    for hh, mm in ((4, 30), (4, 45), (6, 30), (6, 45), (9, 0)):
        assert wd.main(now=at(hh, mm), store=store, runs=runs) == 0
    fired = [o["alert"] for o in lines(capsys.readouterr().out) if o.get("alert")]
    assert fired == ["zero_rows", "brief_late"]
    assert [r["counts"]["fired"] for r in runs.rows] == [["zero_rows"], [], ["brief_late"], [], []]
    store.asked.clear()
    wd.main(now=at(9, 0), store=store, runs=runs)
    assert ("observations", DAY) not in store.asked and ("published_markets", DAY) not in store.asked


def test_the_same_alert_fires_again_the_next_day(capsys):
    runs = chain.MemoryRunsStore()
    store = FakeStore(observations={"ZA": 0, "NG": 90, "KE": 75}, runs=runs)
    wd.main(now=at(4, 30), store=store, runs=runs)
    wd.main(now=at(4, 30, day=DAY + timedelta(days=1)), store=store, runs=runs)
    fired = [o["alert"] for o in lines(capsys.readouterr().out) if o.get("alert")]
    assert fired == ["zero_rows", "zero_rows"]


class NoMemory(FakeStore):
    def fired_today(self, d):
        raise RuntimeError("runs unreadable")


def test_when_todays_rows_cannot_be_read_every_rule_still_runs_and_the_job_fails(capsys):
    runs = chain.MemoryRunsStore()
    assert wd.main(now=at(4, 30), store=NoMemory(observations={"ZA": 0}), runs=runs) == 1
    out = lines(capsys.readouterr().out)
    assert [o["alert"] for o in out if o.get("alert")] == ["zero_rows"]
    assert any("runs unreadable" in o["message"] for o in out if not o.get("alert"))
    assert runs.rows[0]["status"] == "failed"


class FakeRow(dict):
    def items(self):
        return super().items()


class FakeClient:
    def __init__(self, answers):
        self.answers = answers
        self.queries = []

    def query(self, sql, job_config=None):
        params = {p.name: p.value for p in job_config.query_parameters}
        self.queries.append((sql, params))
        for marker, rows in self.answers.items():
            if marker in sql:
                return FakeJob(rows)
        raise AssertionError(f"unexpected query {sql}")


class FakeJob:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return [FakeRow(r) for r in self.rows]


def test_bigquery_store_asks_parameterised_questions_of_the_right_tables():
    client = FakeClient({
        "stage = 'watchdog'": [{"name": "zero_rows"}, {"name": "brief_late"}],
        "stage = 'collect'": [{"n": 1}],
        "post_observations": [{"market": "ZA", "n": 3}, {"market": "NG", "n": 0}],
        ".briefs": [{"market": "ZA"}, {"market": "KE"}],
        "model_usd": [{"usd": 4.5}],
        "raw_responses": [keyrow("tiktok/trending", "data", 1, 7, 1, 7)],
        "stage = 'ask'": [{"total": 21, "failed": 2}],
    })
    store = wd.BigQueryStore(client)
    assert store.collect_ok(DAY) is True
    assert store.observations(DAY) == {"ZA": 3, "NG": 0}
    assert store.published_markets(DAY) == {"ZA", "KE"}
    assert store.model_usd(DAY) == 4.5
    assert store.route_keys(DAY) == [keyrow("tiktok/trending", "data", 1, 7, 1, 7)]
    assert store.ask_runs(DAY) == (21, 2)
    assert store.fired_today(DAY) == {"zero_rows", "brief_late"}
    assert len(client.queries) == 7
    for sql, params in client.queries:
        assert params == {"d": DAY}
        assert "2026-10-01" not in sql
        assert "`ogilvy-trends-v2.intelligence_42_" in sql
        for word in ("INSERT", "UPDATE", "MERGE", "DELETE", "CREATE", "DROP"):
            assert word not in sql.upper().split()


def test_model_spend_sums_model_usd_over_every_stage_plus_ask_records():
    sql = wd.SQL["model_usd"]
    assert "stage IN" not in sql and "stage = 'brief'" not in sql
    assert "JSON_VALUE(r.counts, '$.model_usd')" in sql
    assert "r.stage = 'ask'" in sql and "JSON_VALUE(r.record, '$.run.model_usd')" in sql
    assert "GROUP BY r.run_id" in sql


def test_bigquery_store_reads_an_empty_spend_as_zero():
    client = FakeClient({"model_usd": [{"usd": None}], "stage = 'collect'": [{"n": 0}]})
    store = wd.BigQueryStore(client)
    assert store.model_usd(DAY) == 0.0
    assert store.collect_ok(DAY) is False


def test_job_and_schedule_constants_for_deploy_jobs_and_schedule():
    job = wd.JOB
    assert (job["name"], job["module"], job["identity"]) == ("f42-watchdog", "core.setup.watchdog", "f42-brief")
    assert job["retries"] == 0 and job["secret"] is False and job["timeout"] <= timedelta(minutes=10)
    crons = dict(wd.SCHEDULES)
    assert crons == {"f42-watchdog-quarter": "*/15 2-7 * * *", "f42-watchdog-hourly": "0 0,1,8-23 * * *"}
